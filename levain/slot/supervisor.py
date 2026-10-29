"""The interactive supervisor: the operator uid's process for ONE session (CONTRACT §1 "interactive supervisor", §3.1,
§5.1, §5.4 step 1; BUILD_PLAN §2c 5, §2c 9, §2d 5 + C5, §6).

It holds two things the slot must never reach: the LEASE (the CONTROL connection, CLOEXEC, never passed to a child,
§3.1 4) and the operator's TERMINAL (the slot's bytes reach it only through :class:`TermFilter`). In order:

1. the repo layer, before any lease or staging write: ``--repo`` is a git work tree the operator owns on a named
   branch; :func:`operator_clone` makes the §2c 5 clone (tracked files only, no shared inode, remotes and their
   credentials stripped) into a private temp dir; :func:`repo_layer_docs` reads its ``.claude/settings.json`` +
   ``.claude/settings.local.json`` from the commit and :func:`levain.slot.render.repo_loosening_refusals` refuses by
   key name. The slot clones exactly the bundle made from THIS clone, so what was checked is what launches.
2. the render blob's inputs (base-map routes, the operator's git identity), so a launch that cannot render is refused
   before it takes a slot.
3. ``LEASE-OPEN`` (§3.1 1), then the runtime helper through sudo: ``prepare`` (stdin ``{lease_nonce}``), the bundle
   into the staging dir prepare returned (:func:`levain.slot.stage.make_bundle`), ``render`` (stdin the §5.3 blob),
   ``start`` INSIDE a pty this process allocates (§2d 5), relayed both ways, then ``teardown``. A refusal at any step
   is printed by name and, once a session exists, teardown still runs. SIGINT / SIGTERM / SIGHUP end the session the
   same way (teardown, then the lease closes).

Every helper call answers exactly one canonical JSON line (§5 preamble) that :func:`parse_helper_line` reads; ``start``
answers on the pty after the vendor's own bytes, so :func:`start_result` reads it from the stream's tail.

The firewall self-probe (§2c 9, V13) is the helper's ``probe`` verb: root connects to the vendor's own addresses as
the slot uid inside its wall. It runs between ``render`` and ``start`` and the launch proceeds only on ``closed``
(:meth:`Supervisor._launch_probe`). On macOS it runs again inside the relay's own select tick (:class:`_Reprobe` keeps
the schedule and the streak, :class:`_ProbeRun` drives one helper run without blocking), and an open wall, or two
unknowns in a row, end the session through :attr:`Supervisor.end_reason` and the one teardown.
"""
from __future__ import annotations

import errno
import fcntl
import ipaddress
import json
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from levain import unshowable
from levain.launch import child_env
from levain.contracts import ContractError, canon
from levain.slot import SUDO, Refused, layout
from levain.slot.grammar import SESSION_RE
from levain.slot.runtime_grammar import RENDER_FORMAT, parse_stdin
from levain.slot.vendor_hosts import VENDOR_HOSTS, _hosts

# --- the terminal filter (BUILD_PLAN §2e 14, §6: titles pass, OSC 52 and report sequences are stripped) ---------------
#
# The invariant: everything the filter EMITS, the operator's terminal parses exactly as the filter did, so the terminal
# is in its ground state between any two emissions. It emits only ground text (no ESC, no ENQ, no C1 control in either
# form), and COMPLETE sequences it parsed and approved. Whatever it drops, the terminal never sees, so a dropped prefix
# cannot leave the terminal mid-sequence. A UTF-8 character is emitted whole, never split across two writes.

_G, _ESC, _ESC_I, _CSI, _CSI_IGN, _OSC, _OSC_ESC, _STR, _STR_ESC = range(9)

ESC, BEL, CAN, SUB, ENQ = 0x1B, 0x07, 0x18, 0x1A, 0x05

#: CSI requests that make the operator's terminal ANSWER into the slot: (private prefix, intermediates, final).
_CSI_REPORTS = frozenset({
    ("", "", "c"), (">", "", "c"), ("=", "", "c"),     # DA1, DA2, DA3
    ("", "", "n"), ("?", "", "n"),                      # DSR, CPR, DECXCPR, the DEC private status reports
    ("", "$", "p"), ("?", "$", "p"),                    # DECRQM (ANSI, DEC)
    (">", "", "q"),                                     # XTVERSION
    ("", "", "x"),                                      # DECREQTPARM
    ("?", "", "u"),                                     # the kitty keyboard flags query
    ("", "$", "u"), ("", "$", "w"),                     # DECRQTSR, DECRQPSR
    ("?", "", "S"), ("?", "", "m"),                     # XTSMGRAPHICS, XTQMODKEYS
    ("", "*", "y"),                                     # DECRQCRA (a checksum of screen contents)
    ("", "'", "|"), ("", "#", "|"),                     # DECRQLP (the locator report), XTREPORTSGR
    ("", "&", "u"), ("", '"', "v"),                     # DECRQUPSS, DECRQDE
})
#: XTWINOPS (``CSI Ps t``) with these first parameters REPORTS (window state, sizes, the icon label, the title).
_WINOPS_REPORTS = frozenset({11, 13, 14, 15, 16, 18, 19, 20, 21})
#: DECISION: OSC is an ALLOWLIST (fail closed: an unknown OSC is a terminal-specific feature, and some of those write
#: the clipboard or move files, iTerm2's 1337 among them). Titles (0, 1, 2), cwd (7), hyperlinks (8), notifications
#: (9, 777) and prompt marks (133) pass; colour SETS pass, colour QUERIES (a ``?`` field) do not.
_OSC_PASS = frozenset({0, 1, 2, 7, 8, 9, 133, 777})
#: DEC private modes that make the terminal SEND (mouse, focus, bracketed paste). They pass (what they send is the
#: operator's own input, which the slot gets anyway), and every one passed is reset when the session ends
#: (:meth:`TermFilter.reset`). DECISION: 9 (X10 mouse) is the same class as 1000-1006.
_REPORTING_MODES = frozenset({9, 1000, 1001, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 2004})
_OSC_COLOUR = frozenset({4, 104, *range(10, 20), *range(110, 120)})
_ASCII_RUN = re.compile(rb"[\x00-\x04\x06-\x1a\x1c-\x7f]+")
_CSI_MAX = 256        # DECISION: a CSI longer than this is no sequence a terminal answers; it is dropped whole
_OSC_MAX = 8192       # DECISION: an OSC payload past this is discarded to its terminator, never buffered further
_ESC_I_MAX = 8


class TermFilter:
    """An incremental filter over the slot's output bytes. :meth:`feed` returns what may reach the terminal now;
    :meth:`flush` (at EOF) releases a held partial UTF-8 character and drops any unfinished sequence."""

    def __init__(self) -> None:
        self._st = _G
        self._seq = bytearray()      # the sequence being parsed (dropped or emitted whole)
        self._u8 = bytearray()       # a multibyte UTF-8 character still arriving
        self._need = 0
        self._cp = 0
        self._lo, self._hi = 0x80, 0xBF   # the range of the character's NEXT continuation byte
        self._taint = False          # the OSC being parsed is dropped at its terminator
        self._over = False           # the OSC payload passed _OSC_MAX: discard to the terminator
        self._modes: set = set()     # _REPORTING_MODES the filter passed set and not since reset

    # -- the UTF-8 layer: whole code points, or one stray byte ------------------------------------------------------

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            if self._st == _G and self._need == 0:
                m = _ASCII_RUN.match(data, i)
                if m:
                    out += m.group()
                    i = m.end()
                    continue
            self._byte(data[i], out)
            i += 1
        return bytes(out)

    def flush(self) -> bytes:
        out = bytearray()
        if self._need:
            self._broken(out)
        self._st, self._seq = _G, bytearray()
        return bytes(out)

    def reset(self) -> bytes:
        """The sequence that turns off every reporting mode this filter let the slot set (empty when none). Sent at
        the session's end, after :meth:`flush`, so the operator's shell is never left sending mouse or focus
        reports."""
        modes, self._modes = sorted(self._modes), set()
        return b"\x1b[?" + ";".join(map(str, modes)).encode("ascii") + b"l" if modes else b""

    def _byte(self, b: int, out: bytearray) -> None:
        if self._need:
            if self._lo <= b <= self._hi:
                self._lo, self._hi = 0x80, 0xBF
                self._u8.append(b)
                self._cp = (self._cp << 6) | (b & 0x3F)
                self._need -= 1
                if self._need == 0:
                    raw, cp = bytes(self._u8), self._cp
                    self._u8 = bytearray()
                    self._unit(cp, raw, out)
                return
            self._broken(out)
        if b < 0x80:
            self._unit(b, bytes((b,)), out)
        elif 0xC2 <= b <= 0xDF:
            self._u8, self._need, self._cp = bytearray((b,)), 1, b & 0x1F
        elif 0xE0 <= b <= 0xEF:
            self._u8, self._need, self._cp = bytearray((b,)), 2, b & 0x0F
            # An overlong (E0 80-9F) or a surrogate (ED A0-BF) is cut at its second byte: both are stray bytes.
            self._lo, self._hi = {0xE0: (0xA0, 0xBF), 0xED: (0x80, 0x9F)}.get(b, (0x80, 0xBF))
        elif 0xF0 <= b <= 0xF4:
            self._u8, self._need, self._cp = bytearray((b,)), 3, b & 0x07
            # An overlong (F0 80-8F) or past U+10FFFF (F4 90-BF): the same.
            self._lo, self._hi = {0xF0: (0x90, 0xBF), 0xF4: (0x80, 0x8F)}.get(b, (0x80, 0xBF))
        else:
            self._stray(b, out)

    def _broken(self, out: bytearray) -> None:
        """A multibyte character cut short: its bytes are stray, one by one (the terminal shows U+FFFD)."""
        held, self._u8, self._need = bytes(self._u8), bytearray(), 0
        self._lo, self._hi = 0x80, 0xBF
        for x in held:
            self._stray(x, out)

    def _stray(self, b: int, out: bytearray) -> None:
        # A raw 0x80-0x9F is an 8-bit C1 control to a terminal not decoding UTF-8: dropped. Any other stray byte is
        # text (the terminal draws U+FFFD), so it is a text unit in ground and taints whatever sequence it lands in.
        if 0x80 <= b <= 0x9F:
            return
        self._unit(-1, bytes((b,)), out)

    # -- the sequence layer -----------------------------------------------------------------------------------------

    def _unit(self, cp: int, raw: bytes, out: bytearray) -> None:
        if 0x80 <= cp <= 0x9F:
            return                    # a C1 control as a code point: dropped in every state (never a string end)
        if cp >= 0:
            # The filter emits only bytes it built: a decoded character is re-encoded, never passed in the input's
            # own form, so no spelling of ESC it did not parse as ESC can reach the terminal. A stray byte (cp -1)
            # is 0xA0-0xFF, never a control.
            raw = chr(cp).encode("utf-8")
        st = self._st
        if st == _G:
            if cp == ESC:
                self._st, self._seq = _ESC, bytearray(raw)
            elif cp != ENQ:           # ENQ asks for the answerback string
                out += raw
        elif st == _ESC:
            self._esc(cp, raw, out)
        elif st == _ESC_I:
            if 0x20 <= cp <= 0x2F and len(self._seq) < _ESC_I_MAX:
                self._seq += raw
            elif 0x30 <= cp <= 0x7E:
                out += self._seq + raw
                self._ground()
            else:
                self._abort(cp, raw, out)
        elif st in (_CSI, _CSI_IGN):
            self._csi(cp, raw, out)
        elif st in (_OSC, _OSC_ESC):
            self._osc(cp, raw, out)
        else:
            self._string(cp, raw, out)

    def _ground(self) -> None:
        self._st, self._seq = _G, bytearray()

    def _abort(self, cp: int, raw: bytes, out: bytearray) -> None:
        """The sequence so far is dropped; ESC starts a new one, CAN / SUB cancel, anything else is ground again."""
        self._ground()
        if cp in (CAN, SUB):
            return
        self._unit(cp, raw, out)

    def _esc(self, cp: int, raw: bytes, out: bytearray) -> None:
        if cp == 0x5B:                                  # [
            self._st, self._seq = _CSI, bytearray(b"\x1b[")
        elif cp == 0x5D:                                # ]
            self._st, self._seq, self._taint, self._over = _OSC, bytearray(b"\x1b]"), False, False
        elif cp in (0x50, 0x58, 0x5E, 0x5F):            # DCS (DECRQSS, XTGETTCAP, DECUDK, tmux passthrough), SOS, PM, APC
            self._st, self._seq = _STR, bytearray()
        elif 0x20 <= cp <= 0x2F:
            self._st = _ESC_I
            self._seq += raw
        elif 0x30 <= cp <= 0x7E:
            if cp != 0x5A:                              # ESC Z = DECID, the old DA request
                out += self._seq + raw
            self._ground()
        else:
            self._abort(cp, raw, out)

    def _csi(self, cp: int, raw: bytes, out: bytearray) -> None:
        if 0x40 <= cp <= 0x7E:
            seq = bytes(self._seq) + raw
            keep = self._st == _CSI and not _csi_is_report(seq)
            self._ground()
            if keep:
                self._track_modes(seq)
                out += seq
            return
        if not 0x20 <= cp <= 0x3F:
            self._abort(cp, raw, out)
            return
        if self._st == _CSI_IGN:
            return
        self._seq += raw
        body = bytes(self._seq[2:])
        if len(self._seq) > _CSI_MAX or not _csi_shape(body):
            self._st = _CSI_IGN                         # malformed: consumed to its final byte and dropped

    def _track_modes(self, seq: bytes) -> None:
        # DECSET / DECRST (``CSI ? Pm h|l``) and XTRESTORE (``CSI ? Pm r``, which may set any mode it restores).
        if seq[2:3] != b"?" or seq[-1:] not in (b"h", b"l", b"r"):
            return
        for p in seq[3:-1].split(b";"):
            if p.isdigit() and int(p) in _REPORTING_MODES:
                if seq[-1:] == b"l":
                    self._modes.discard(int(p))
                else:
                    self._modes.add(int(p))

    def _osc(self, cp: int, raw: bytes, out: bytearray) -> None:
        if self._st == _OSC_ESC:
            if cp == 0x5C:                              # ESC \ = ST
                self._osc_end(b"\x1b\\", out)
            else:
                self._ground()
                self._unit(ESC, b"\x1b", out)
                self._unit(cp, raw, out)
            return
        if cp == BEL:
            self._osc_end(b"\x07", out)
        elif cp == ESC:
            self._st = _OSC_ESC
        elif cp in (CAN, SUB):
            self._ground()
        else:
            if cp < 0x20 or cp == 0x7F or cp < 0:
                self._taint = True                      # DECISION: a control or a broken byte in the payload
            if self._over:
                return
            if len(self._seq) + len(raw) > _OSC_MAX:
                self._over = self._taint = True
                self._seq = bytearray(b"\x1b]")
                return
            self._seq += raw

    def _osc_end(self, term: bytes, out: bytearray) -> None:
        seq, keep = bytes(self._seq), not self._taint and _osc_allowed(bytes(self._seq[2:]))
        self._ground()
        if keep:
            out += seq + term

    def _string(self, cp: int, raw: bytes, out: bytearray) -> None:
        """DCS / SOS / PM / APC: every byte dropped as it arrives (nothing buffered), to ST."""
        if self._st == _STR_ESC:
            if cp == 0x5C:
                self._ground()
            else:
                self._st = _STR
                if cp == ESC:
                    self._st = _STR_ESC
            return
        if cp == ESC:
            self._st = _STR_ESC
        elif cp in (CAN, SUB):
            self._ground()


def _csi_shape(body: bytes) -> bool:
    """A CSI body so far: an optional private marker FIRST, then parameter bytes, then intermediates."""
    seen_inter = False
    for k, b in enumerate(body):
        if 0x20 <= b <= 0x2F:
            seen_inter = True
        elif seen_inter:
            return False
        elif b in b"<=>?" and k != 0:
            return False
    return True


def _csi_is_report(seq: bytes) -> bool:
    body, final = seq[2:-1], chr(seq[-1])
    prefix = chr(body[0]) if body[:1] in (b"<", b"=", b">", b"?") else ""
    rest = body[1:] if prefix else body
    inter = bytes(b for b in rest if 0x20 <= b <= 0x2F).decode("ascii")
    params = bytes(b for b in rest if 0x30 <= b <= 0x3F).decode("ascii")
    if (prefix, inter, final) in _CSI_REPORTS:
        return True
    if (prefix, inter, final) == ("", "", "t"):
        first = params.split(";")[0].split(":")[0]
        return (int(first) if first.isdigit() else 0) in _WINOPS_REPORTS
    return False


def _osc_allowed(payload: bytes) -> bool:
    num, _, rest = payload.partition(b";")
    if not num.isdigit() or len(num) > 5:
        return False
    n = int(num)
    if n in _OSC_PASS:
        return True
    if n in _OSC_COLOUR:
        return b"?" not in rest.split(b";")
    return False


# --- spawning: the lease fd never reaches a child (§3.1 4) ------------------------------------------------------------

def _take_ctty() -> None:   # pragma: no cover - runs in the forked child, between fork and exec
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def spawn(argv: Sequence[str], *, stdin: Any, stdout: Any, stderr: Any, lease_fds: Collection[int],
          ctty: bool = False, env: Optional[Mapping[str, str]] = None) -> "subprocess.Popen[bytes]":
    """Every child gets EXACTLY fds 0, 1, 2 (``close_fds`` with no ``pass_fds``; for ``start`` those three are its pty
    slave) in its own session (``ctty``: the slave becomes its controlling terminal, so sudo's ``use_pty`` sees a
    terminal). Refused before any fork when a lease fd would be one of the three or could be inherited. With no
    ``env`` the child gets :func:`levain.launch.child_env` (the allowlist), never this process's whole environment."""
    for fd in lease_fds:
        if fd in (stdin, stdout, stderr):
            raise Refused("lease-fd-to-child", f"fd {fd}")
        if os.get_inheritable(fd):
            raise Refused("lease-fd-inheritable", f"fd {fd}")
    return subprocess.Popen(list(argv), stdin=stdin, stdout=stdout, stderr=stderr, close_fds=True, pass_fds=(),
                            start_new_session=True, preexec_fn=_take_ctty if ctty else None,
                            env=child_env() if env is None else dict(env))


def run_child(argv: Sequence[str], *, env: Mapping[str, str], **kw: Any) -> subprocess.CompletedProcess:
    """:attr:`Seams.run`'s default: ``subprocess.run`` with ``env`` required, so no child the supervisor runs can
    inherit this process's whole environment by leaving it out."""
    return subprocess.run(list(argv), env=dict(env), **kw)


# --- the helper's answers (§5 preamble: one canonical JSON line) -----------------------------------------------------

def _answer(line: bytes, verb: str) -> Dict[str, object]:
    try:
        obj = canon.parse(line)
    except ContractError:
        raise Refused("helper-output-malformed", verb) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != line:
        raise Refused("helper-output-malformed", verb)
    if obj.get("ok") is True:
        return {k: v for k, v in obj.items() if k != "ok"}
    if obj.get("ok") is False and set(obj) == {"ok", "refused"} and type(obj["refused"]) is str:
        raise Refused(obj["refused"], f"runtime helper {verb}")
    raise Refused("helper-output-malformed", verb)


def parse_helper_line(data: bytes, verb: str) -> Dict[str, object]:
    """Exactly one newline-terminated canonical JSON line: ``{"ok":true,…}`` -> its fields; ``{"ok":false,"refused":
    <name>}`` -> ``Refused(<name>)``; anything else (none, two, not canonical) -> ``helper-output-malformed``."""
    if not data.endswith(b"\n") or data.count(b"\n") != 1:
        raise Refused("helper-output-malformed" if data else "helper-silent", verb)
    return _answer(data[:-1], verb)


def identity_answer(ans: Mapping[str, object]) -> Tuple[Optional[str], Optional[str]]:
    """``status <session>``'s ``identity`` (CONTROL's state-record field) as (observed id, result), checked by
    records' own rules before it decides or is shown; absent is (None, None). Anything else is
    ``identity-answer-malformed``."""
    from levain.slot.records import _REASON_RE, OBSERVED_ID_RE

    ident = ans.get("identity")
    if ident is None:
        return None, None
    if type(ident) is not dict or not set(ident) <= {"observed_id", "result"}:
        raise Refused("identity-answer-malformed", "identity")
    oid, result = ident.get("observed_id"), ident.get("result")
    if oid is not None and (type(oid) is not str or not OBSERVED_ID_RE.fullmatch(oid)):
        raise Refused("identity-answer-malformed", "identity.observed_id")
    if result is not None and (type(result) is not str or not _REASON_RE.fullmatch(result)):
        raise Refused("identity-answer-malformed", "identity.result")
    return oid, result


#: §5.1 prepare's answer for an egress session: ``levain-prepare-result/1 {session, slot:{class,index}, gwport,
#: staging}`` (the supervisor only ever prepares egress, so ``gwport`` is required).
PREPARE_KEYS = frozenset({"format", "session", "slot", "staging", "gwport"})


def prepare_answer(prep: Mapping[str, object], *, host: str, store: Path) -> Tuple[str, int]:
    """``prepare``'s answer checked whole, before the session is taken as known: the exact format and key set, the
    session id, an egress slot (not necessarily the one asked for: §5.1 prepare falls back from a named slot that
    is not REUSABLE), the gateway port §3.4 gives that slot on this host, and the staging dir under ``store``.
    Anything else is ``prepare-answer-malformed`` naming the field. Returns (session, slot index)."""
    from levain.slot.ids import SlotId
    from levain.slot.render import gwport_for
    from levain.slot.runtime_helper import PREPARE_RESULT_FORMAT

    def bad(what: str) -> Refused:
        return Refused("prepare-answer-malformed", what)

    if set(prep) != PREPARE_KEYS:
        raise bad("keys: " + ",".join(sorted(set(prep) ^ PREPARE_KEYS))[:128])
    if prep["format"] != PREPARE_RESULT_FORMAT:
        raise bad("format")
    session = prep["session"]
    if type(session) is not str or not SESSION_RE.fullmatch(session):
        raise bad("session")
    sl = prep["slot"]
    if type(sl) is not dict or set(sl) != {"class", "index"} or sl["class"] != "egress" or type(sl["index"]) is not int:
        raise bad("slot")
    try:
        sid = SlotId("egress", sl["index"])
    except Refused:
        raise bad("slot") from None
    if type(prep["gwport"]) is not int or prep["gwport"] != gwport_for(host, sid):   # type: ignore[arg-type]
        raise bad("gwport")
    if prep["staging"] != str(Path(store) / "staging" / session):
        raise bad("staging")
    return session, sid.index


def start_result(tail: bytes) -> Dict[str, object]:
    """``start``'s line, read off the END of the pty stream: it follows the vendor's last byte (which may not end
    its own line), and the ptys between add ``\\r``. The rightmost ``{`` on the last non-empty line whose suffix is
    one canonical object is the answer. DECISION: only the printed outcome rests on it; teardown runs either way."""
    lines = [ln.strip() for ln in tail.replace(b"\r", b"").split(b"\n") if ln.strip()]
    if lines:
        last = lines[-1]
        i = last.rfind(b"{")
        while i >= 0:
            try:
                return _answer(last[i:], "start")
            except Refused as e:
                if e.name != "helper-output-malformed":
                    raise
            i = last.rfind(b"{", 0, i)
    raise Refused("start-result-unreadable", "start")


# --- the repo layer (§5.4 1, BUILD_PLAN §2c 5) -------------------------------------------------------------------------

#: The repo-relative names CC reads as the project's settings layers (render.repo_loosening_refusals' keys).
REPO_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
SETTINGS_CAP = 1 << 20          # DECISION: a settings file past 1 MiB is refused malformed, never parsed
_REGULAR = (b"100644", b"100755")


#: The refusal the repo-layer check's git runs under: that git reads only the clone levain made, so it reads no
#: global or system config (the caller's own config cannot break or steer the check).
_LAYER_REFUSAL = "repo-settings-malformed"


def _git(argv: Sequence[str], *, git: str, run: Callable[..., subprocess.CompletedProcess], refusal: str,
         check: bool = True) -> subprocess.CompletedProcess:
    # the bundle's own scrubbed git environment (one definition) and the stage's own bound on its git
    from levain.slot.stage import STAGE_DEADLINE_S, _git_env as env

    try:
        p = run([git, "-c", "core.hooksPath=/dev/null", *argv], env=env(user_config=refusal != _LAYER_REFUSAL),
                capture_output=True, timeout=STAGE_DEADLINE_S)
    except subprocess.TimeoutExpired:
        raise Refused(refusal, f"git passed its {STAGE_DEADLINE_S}s timeout") from None
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else (p.stderr or "")
        raise Refused(refusal, f"{argv[0] if argv[0] != '-C' else argv[2]}: {err.strip()[:200]}")
    return p


def _out(p: subprocess.CompletedProcess) -> str:
    return (p.stdout.decode("utf-8", "replace") if isinstance(p.stdout, bytes) else p.stdout).strip()


def repo_toplevel(path: Path, *, uid: int, git: str = "git",
                  run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                  stat: Callable[[str], os.stat_result] = os.stat) -> Tuple[Path, str]:
    """``--repo``'s work-tree top level and its branch. Refused ``repo-not-a-work-tree`` (bare, absent, not git),
    ``repo-not-owned`` (the top level's owner is not the operator), ``repo-detached-head`` (DECISION: the bundle names
    a branch, so a detached HEAD has none to launch)."""
    if not Path(path).is_dir():
        raise Refused("repo-not-a-work-tree", str(path)[:200])
    p = _git(["-C", str(path), "rev-parse", "--is-inside-work-tree", "--show-toplevel"], git=git, run=run,
             refusal="repo-not-a-work-tree", check=False)
    lines = _out(p).splitlines()
    if p.returncode != 0 or len(lines) != 2 or lines[0] != "true":
        raise Refused("repo-not-a-work-tree", str(path)[:200])
    top = Path(lines[1])
    if stat(str(top)).st_uid != uid:
        raise Refused("repo-not-owned", str(top)[:200])
    b = _git(["-C", str(top), "symbolic-ref", "--quiet", "--short", "HEAD"], git=git, run=run,
             refusal="repo-detached-head", check=False)
    branch = _out(b)
    if b.returncode != 0 or not branch:
        raise Refused("repo-detached-head", str(top)[:200])
    return top, branch


def operator_clone(repo: Path, branch: str, dest: Path, *, git: str = "git",
                   run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """The §2c 5 clone of ``repo``'s ``branch`` into ``dest`` (which must not exist): ``--no-local --no-hardlinks
    --template= --single-branch`` with hooks off, so tracked files only and no inode shared with the operator's repo;
    then its ``origin`` removed (the remote URL, with any credential in it, and the branch's upstream go with it).
    DECISION: ``--no-checkout`` as :func:`levain.slot.stage.make_bundle`'s own clone (no work tree, so no filter,
    fsmonitor or checkout hook of the operator's config runs). Returns the commit checked and bundled."""
    if type(branch) is not str or not branch or branch.startswith("-"):
        raise Refused("bad-branch", str(branch)[:64])
    _git(["clone", "--no-local", "--no-hardlinks", "--template=", "--single-branch", "--no-checkout",
          "--branch", branch, "--", str(repo), str(dest)], git=git, run=run, refusal="clone-failed")
    _git(["-C", str(dest), "remote", "remove", "origin"], git=git, run=run, refusal="clone-failed")
    return _out(_git(["-C", str(dest), "rev-parse", "--verify", "HEAD^{commit}"], git=git, run=run,
                     refusal="clone-failed"))


def _fold(name: str) -> str:
    # DECISION: a case- and normalization-insensitive store (APFS by default) checks `.Claude/Settings.json` out
    # where CC reads `.claude/settings.json`, so every spelling that folds to a settings name is checked.
    return unicodedata.normalize("NFD", name).casefold()


def _ls_tree(clone: Path, treeish: str, *, git: str, run: Callable[..., subprocess.CompletedProcess]
             ) -> List[Tuple[bytes, bytes, bytes, str]]:
    p = _git(["-C", str(clone), "ls-tree", "-z", treeish], git=git, run=run, refusal="repo-settings-malformed")
    out = []
    for ent in p.stdout.split(b"\0"):
        if not ent:
            continue
        meta, _, name = ent.partition(b"\t")
        mode, typ, sha = meta.split(b" ")
        out.append((mode, typ, sha, name.decode("utf-8", "surrogateescape")))
    return out


def _no_dup(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def repo_layer_docs(clone: Path, *, git: str = "git",
                    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                    treeish: str = "HEAD") -> Dict[str, Any]:
    """The clone's committed settings layers at ``treeish`` (the clone's HEAD unless named), parsed: repo-relative
    name -> JSON value, for every tree entry whose name folds to ``.claude`` / ``settings.json`` / ``settings.local.json``. Refused ``repo-settings-malformed`` when
    such an entry is a link or a submodule (CC would read through it), is over :data:`SETTINGS_CAP`, is not JSON, or
    repeats a key (DECISION: two values for one key is refused, not resolved), or nests too deep to parse."""
    want = {_fold(Path(n).name): n for n in REPO_SETTINGS}
    docs: Dict[str, Any] = {}
    for mode, typ, sha, name in _ls_tree(clone, treeish, git=git, run=run):
        if _fold(name) != _fold(".claude"):
            continue
        if typ != b"tree":
            raise Refused("repo-settings-malformed", f"{name}: not a directory ({mode.decode()})")
        for m2, t2, s2, n2 in _ls_tree(clone, sha.decode(), git=git, run=run):
            if _fold(n2) not in want:
                continue
            path = f"{name}/{n2}"
            if t2 != b"blob" or m2 not in _REGULAR:
                raise Refused("repo-settings-malformed", f"{path}: not a regular file ({m2.decode()})")
            size = _out(_git(["-C", str(clone), "cat-file", "-s", s2.decode()], git=git, run=run,
                             refusal="repo-settings-malformed"))
            if not size.isdigit() or int(size) > SETTINGS_CAP:
                raise Refused("repo-settings-malformed", f"{path}: over {SETTINGS_CAP} bytes")
            blob = _git(["-C", str(clone), "cat-file", "blob", s2.decode()], git=git, run=run,
                        refusal="repo-settings-malformed").stdout
            try:
                docs[path] = json.loads(blob.decode("utf-8"), object_pairs_hook=_no_dup)
            except (UnicodeDecodeError, ValueError, RecursionError):   # nesting past the parser's depth: untrusted
                raise Refused("repo-settings-malformed", f"{path}: not JSON") from None
    return docs


def check_repo_layer(docs: Mapping[str, Any]) -> None:
    from levain.slot.render import repo_loosening_refusals

    bad = repo_loosening_refusals(docs)
    if bad:
        raise bad[0]


# --- the render blob's inputs (§5.3) ---------------------------------------------------------------------------------

_AUTH_ROUTE = {"brokered": "brokered", "passthrough": "subscription_passthrough"}


def read_base_map(path: Path, *, read: Callable[[Path], bytes] = Path.read_bytes) -> List[Mapping[str, Any]]:
    try:
        raw = read(path)
    except OSError as e:
        raise Refused("base-map-unreadable", f"{path}: {e.strerror or e}") from None
    try:
        obj = canon.parse(raw)
    except ContractError:
        raise Refused("base-map-malformed", str(path)) from None
    if type(obj) is not dict or obj.get("format") not in layout.BASE_ROUTES_FORMATS or type(obj.get("routes")) is not list:
        raise Refused("base-map-malformed", str(path))
    return [r for r in obj["routes"] if type(r) is dict]


def pick_routes(base: Sequence[Mapping[str, Any]], vendor: str, auth: str) -> Tuple[str, ...]:
    """Every ``egress`` route of the base map (the git hosts and registries) plus EXACTLY ONE route of the auth's
    type for this vendor (§5.3: ≤ 1 brokered + ≤ 1 passthrough per vendor; render refuses a brokered session without
    its route). Refused ``no-route:<auth>`` / ``route-ambiguous:<auth>`` by name."""
    need = _AUTH_ROUTE[auth]
    egress = [r["route"] for r in base if r.get("type") == "egress" and type(r.get("route")) is str]
    mine = [r["route"] for r in base if r.get("type") == need and type(r.get("route")) is str
            and _hosts(r) & VENDOR_HOSTS[vendor]]
    if not mine:
        raise Refused(f"no-route:{auth}", f"{vendor}: the base map has no {need} route")
    if len(mine) > 1:
        raise Refused(f"route-ambiguous:{auth}", f"{vendor}: {', '.join(sorted(mine))}")
    return tuple(sorted(set(egress))) + (mine[0],)


def git_identity(repo: Path, *, git: str = "git",
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Optional[Dict[str, str]]:
    """The operator's own ``user.name`` / ``user.email`` as git resolves them for ``repo`` (DECISION: repo-local
    config included: it is his file). ``None`` when either is unset (the slot then has no identity to commit with)."""
    vals = []
    for key in ("user.name", "user.email"):
        p = _git(["-C", str(repo), "config", "--get", key], git=git, run=run, refusal="git-identity", check=False)
        if p.returncode != 0 or not _out(p):
            return None
        vals.append(_out(p))
    return {"name": vals[0], "email": vals[1]}


#: DECISION: no hook in a session-2 render: the runtime helper holds no hook templates yet (``Seams.hook_templates``
#: is empty), so naming one is refused ``hook-template-missing``, and Codex hooks are refused ``hooks-not-wired``.
HOOKS_BY_AUTH: Mapping[str, Tuple[str, ...]] = {"brokered": (), "passthrough": ()}


def render_blob(vendor: str, auth: str, routes: Sequence[str], identity: Optional[Mapping[str, str]],
                label: Optional[str]) -> bytes:
    """The §5.3 blob as canonical bytes, checked by the helper's own grammar before it is sent. The operator's two
    inputs are each checked alone, so a refusal names its cause: a git identity the grammar refuses is
    ``git-identity-invalid``; a ``repo_label`` it refuses is dropped (DECISION: it is display only). The whole
    blob is then checked once, and a route or hook it refuses is refused by the grammar's own name."""
    def alone(**extra: object) -> bytes:
        return canon.canonical_bytes({"format": RENDER_FORMAT, "vendor": vendor, "routes": [], **extra})

    blob: Dict[str, object] = {"format": RENDER_FORMAT, "vendor": vendor, "routes": list(routes),
                               "hooks": list(HOOKS_BY_AUTH[auth])}
    if identity is not None:
        try:
            parse_stdin("render", alone(git_identity=dict(identity)))
        except Refused:
            raise Refused("git-identity-invalid", "git config user.name / user.email") from None
        blob["git_identity"] = dict(identity)
    if label is not None:
        try:
            parse_stdin("render", alone(repo_label=label))
            blob["repo_label"] = label
        except Refused:
            pass
    data = canon.canonical_bytes(blob)
    parse_stdin("render", data)
    return data


# --- the session ----------------------------------------------------------------------------------------------------

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_INPUT_MAX = 1 << 20       # DECISION: operator input buffered ahead of the pty; past this, his stdin waits
_OUTPUT_MAX = 1 << 20      # DECISION: filtered output held for a terminal that is not reading; past this the master
#                            is not read (the vendor waits on its pty), and no byte is dropped
START_GRACE_S = 10.0       # DECISION: after teardown, how long `start` gets to return before it is signalled
#: DECISION: the wall each helper verb gets before it is ended (TERM, then KILL) and refused ``helper-timeout``.
#: prepare takes two locks and an ATTACH; render validates, starts the front and mints; teardown is END (freeze,
#: kill, retain), the longest. Generous: they bound a wedge, they do not time normal work.
#: probe: root's bounded DNS, CONTROL before and after, the session lock and the child's reap (V13 §6).
HELPER_DEADLINE_S: Mapping[str, float] = {"prepare": 120.0, "render": 300.0, "probe": 45.0, "teardown": 600.0}
HELPER_DEADLINE_DEFAULT_S = 120.0
_HELPER_OUT_MAX = 1 << 20  # DECISION: a helper answer past this is no single line: refused malformed
_HELPER_ERR_KEEP = 1 << 16
KILL_GRACE_S = 2.0         # DECISION: between TERM and KILL for a helper being ended
_AFTER_EXIT_MAX = 1 << 20  # DECISION: once the helper has exited the master is read past the cap (what the pty still
#                            holds is the stream's end, and `start`'s line); past this much more the bytes are counted, not
#                            kept: something left in the slot is writing, and memory stays bounded
AFTER_EXIT_S = 2.0         # DECISION: how long the master is read after the helper's exit; what the pty held then
#                            takes milliseconds, so a master still written past this is something left in the slot,
#                            and the relay ends (what the pty still holds counted) rather than reading it for ever
FINAL_DRAIN_S = 2.0        # DECISION: at the session's end, how long a terminal may take NOTHING before the rest is
#                            abandoned (counted); a terminal that keeps taking bytes keeps the drain, up to the cap below
FINAL_DRAIN_MAX_S = 30.0   # DECISION: the final drain's hard bound, however slowly the terminal trickles
FINAL_RESET_S = 1.0        # DECISION: the reporting-mode reset's own attempt, after the drain has given up on the rest
SAY_WAIT_S = 1.0           # DECISION: how long a status line waits on stderr before it is held for the end of the run
HELD_DRAIN_S = 2.0         # DECISION: the held status lines' bounded, non-blocking write at the end of the run
_CAN = b"\x18"             # cancels a sequence the abandoned rest may have cut, so the reset that follows is read whole
REPROBE_S = 60.0           # CONTRACT V13: the macOS re-probe's period, start to start
URGENT_DRAIN_S = 1.0       # N4: the final drain's bound when the session ends on ``firewall-open``
_NOTES_MAX = 32            # DECISION: re-probe notes kept for the end of the relay; past this they are counted
REPROBE_SKIPS_MAX = 5      # L1: consecutive skipped re-probes that end the session (at REPROBE_S apart, ~5 min)
CONFIRM_S = 300.0          # DECISION: how long a login session's confirm prompt waits for his answer (then: no answer)
_CONFIRM_LINE_MAX = 64     # DECISION: an answer line longer than this is taken as "no"


def _pty_pending(fd: int) -> int:
    """The bytes ``fd`` (a pty master) holds unread, or 0 when the host does not say."""
    try:
        return struct.unpack("i", fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"))[0]
    except (OSError, struct.error):
        return 0


#: O2 + N1: a re-probe answer that says nothing about the wall (the lock was held, the session is braked, the host
#: reached no vendor address): a skipped tick, never counted.
_SKIPPED = frozenset({"session-busy", "not-probeable:BRAKED"})
_SKIPPED_BASES = frozenset({"firewall-probe-inconclusive"})


def reprobe_class(name: str) -> str:
    """One re-probe's result by name (``closed`` for an ``ok`` closed answer, else the refusal name):
    ``closed`` | ``open`` | ``skipped`` | ``unknown``. Everything not named here is unknown (R5)."""
    if name == "closed":
        return "closed"
    if name == "firewall-open":
        return "open"
    if name in _SKIPPED or name.split(":", 1)[0] in _SKIPPED_BASES:
        return "skipped"
    return "unknown"


class _Reprobe:
    """The macOS re-probe's schedule and streak (R5, O2, N1, L1), on the caller's clock and nothing else. Start to
    start: the first run is due ``every`` after the relay begins, each next one ``every`` after the last one STARTED,
    and one still running when the next is due delays it to its own end (the caller starts none while one runs).
    Two counts: ``unknowns`` (only ``closed`` resets it; a skipped tick leaves it as it is) and ``skips``, the
    current run of consecutive skipped ticks (``closed`` and ``unknown`` end it). The second unknown, the
    :data:`REPROBE_SKIPS_MAX`-th skip in a row, or any ``open`` is the session's end reason. After a run that timed
    out (``helper-timeout``: the ended helper may still hold the session lock, since a KILL ends sudo alone),
    ``session-busy`` counts unknown until a run answers ``closed``. A run of skipped ticks is noted once. The notes
    are for the END of the relay (N5): nothing here writes to the operator's terminal."""

    def __init__(self, now: float, every: float) -> None:
        self.every = every
        self.next_due = now + every
        self.unknowns = 0
        self.skips = 0
        self.timed_out = False
        self.notes: List[str] = []
        self.dropped = 0

    def due(self, now: float) -> bool:
        return now >= self.next_due

    def started(self, now: float) -> None:
        self.next_due = now + self.every

    def _note(self, msg: str) -> None:
        if len(self.notes) < _NOTES_MAX:
            self.notes.append(msg)
        else:
            self.dropped += 1

    def finished(self, name: str, detail: str = "") -> Optional[str]:
        """The run's result: returns the session's end reason, or ``None`` to go on."""
        kind = reprobe_class(name)
        if name == "session-busy" and self.timed_out:
            kind = "unknown"
        if name == "helper-timeout":
            self.timed_out = True
        tail = f": {detail}" if detail else ""
        if kind == "open":
            self._note(f"firewall self-probe: {name}{tail}")
            return "firewall-open"
        if kind == "unknown":
            self.unknowns += 1
            self.skips = 0
            self._note(f"firewall self-probe unknown ({name}), {self.unknowns} in a row{tail}")
            return "firewall-probe-unknown" if self.unknowns >= 2 else None
        if kind == "closed":
            self.unknowns = self.skips = 0
            self.timed_out = False
            return None
        self.skips += 1
        if self.skips >= REPROBE_SKIPS_MAX:
            self._note(f"firewall self-probe skipped ({name}): {self.skips} skipped in a row{tail}")
            return "firewall-probe-unknown"
        if self.skips == 1:
            self._note(f"firewall self-probe skipped ({name}): the session ends at {REPROBE_SKIPS_MAX} in a row{tail}")
        return None


def probe_answer(out: Mapping[str, object], vendor: str) -> Mapping[str, object]:
    """A ``probe`` ok answer checked whole (L2), for the launch and the re-probe alike: exactly ``probe`` =
    ``closed``, ``host`` one of the vendor's probed hosts, ``address`` an IP address string, ``addresses`` an int
    from 1 to the probe's address cap. Anything else is ``firewall-probe-malformed`` naming the field."""
    from levain.slot.probe import MAX_PROBE_ADDRS, PROBE_HOSTS

    def bad(what: str) -> Refused:
        return Refused("firewall-probe-malformed", f"probe: {what}")

    if set(out) != _PROBE_KEYS:
        raise bad("keys: " + ",".join(sorted(str(k) for k in set(out) ^ _PROBE_KEYS))[:128])
    if out["probe"] != "closed":
        raise bad("probe")
    if type(out["host"]) is not str or out["host"] not in PROBE_HOSTS.get(vendor, ()):
        raise bad("host")
    try:
        if type(out["address"]) is not str:
            raise ValueError
        ipaddress.ip_address(out["address"])
    except ValueError:
        raise bad("address") from None
    n = out["addresses"]
    if type(n) is not int or not 1 <= n <= MAX_PROBE_ADDRS:
        raise bad("addresses")
    return out


#: L2: the fields of a ``probe`` ok answer (``ok`` itself is the line's, taken off by :func:`parse_helper_line`).
_PROBE_KEYS = frozenset({"probe", "host", "address", "addresses"})


def _host() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise Refused("unsupported-host", sys.platform)


@dataclass(frozen=True)
class Request:
    vendor: str                  # claude | codex
    repo: Path
    auth: str = "brokered"       # brokered | passthrough
    kind: str = "interactive"    # interactive | login
    slot: Optional[int] = None


@dataclass
class Seams:
    """Every act with a real default. ``helper`` is the argv prefix the verbs follow (tests run a fake helper script
    there, no sudo)."""

    host: str = field(default_factory=_host)
    helper: Optional[Sequence[str]] = None
    client_factory: Optional[Callable[[Path], Any]] = None
    control_socket: Optional[Path] = None
    store: Optional[Path] = None
    base_map: Optional[Path] = None
    git: str = "git"
    run: Callable[..., subprocess.CompletedProcess] = run_child
    spawn: Callable[..., "subprocess.Popen[bytes]"] = spawn
    openpty: Callable[[], Tuple[int, int]] = os.openpty
    stdin: int = 0
    stdout: int = 1
    err: Optional[IO[str]] = None
    uid: Callable[[], int] = os.getuid
    env: Optional[Mapping[str, str]] = None
    tmp_parent: Optional[Path] = None
    start_grace_s: float = START_GRACE_S
    helper_deadline_s: Mapping[str, float] = field(default_factory=lambda: dict(HELPER_DEADLINE_S))
    kill_grace_s: float = KILL_GRACE_S
    reprobe_s: float = REPROBE_S
    clock: Callable[[], float] = time.monotonic
    #: a login session's confirm prompt -> True (yes), False (no) or None (no answer); None = his terminal
    #: (:meth:`Supervisor._ask`)
    confirm: Optional[Callable[[str], Optional[bool]]] = None

    def __post_init__(self) -> None:
        if self.helper is None:
            self.helper = (SUDO, "-n", str(layout.runtime_helper_path(self.host)))   # type: ignore[arg-type]
        if self.control_socket is None:
            self.control_socket = layout.RUN[self.host] / "control" / "control.sock"   # type: ignore[index]
        if self.store is None:
            self.store = layout.STORE[self.host]                                        # type: ignore[index]
        if self.base_map is None:
            self.base_map = layout.base_routes_path(self.host)                          # type: ignore[arg-type]
        if self.client_factory is None:
            from levain.slot.control_client import ControlClient

            self.client_factory = ControlClient


class _ProbeRun:
    """One mid-session ``probe`` driven from the relay's select loop, never blocking it (R5, N5). Not
    :meth:`Supervisor.helper`: its loop reads the wake pipe and would take a winsize poke from the relay. stdin is
    ``/dev/null`` (the helper refuses any stdin for ``probe``); stdout and stderr are non-blocking pipes whose fds the
    relay adds to its select, read bounded as the helper's are. Its stderr is kept for the note, never shown
    mid-session."""

    def __init__(self, sup: "Supervisor", session: str, now: float) -> None:
        self.sup = sup
        self.deadline = now + sup.s.helper_deadline_s.get("probe", HELPER_DEADLINE_DEFAULT_S)
        self.p = sup.s.spawn([*sup.s.helper, "probe", session], stdin=subprocess.DEVNULL,       # type: ignore[misc]
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, lease_fds=sup._lease_fds(),
                             env=sup.s.env)
        self.out, self.err = bytearray(), bytearray()
        self.over = False
        try:
            self.fout, self.ferr = self.p.stdout.fileno(), self.p.stderr.fileno()     # type: ignore[union-attr]
            self.open = [self.fout, self.ferr]
            for fd in self.open:
                os.set_blocking(fd, False)
        except BaseException:
            self.end()                    # L4: a setup failure after the spawn leaves no child and no pipe behind
            raise

    def pump(self, ready: Collection[int]) -> None:
        for fd in list(self.open):
            if fd not in ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                self.open.remove(fd)
            elif fd == self.fout:
                self.out += chunk
                if len(self.out) > _HELPER_OUT_MAX:
                    self.over = True
            else:
                self.err += chunk
                del self.err[:-_HELPER_ERR_KEEP]

    def detail(self) -> str:
        lines = [ln.strip() for ln in self.err.decode("utf-8", "replace").splitlines() if ln.strip()]
        return " | ".join(lines[-3:])

    def result(self, now: float) -> Optional[str]:
        """The run's name once it is over (``closed``, the helper's refusal, or ``helper-output-malformed`` /
        ``firewall-probe-malformed`` / ``helper-timeout``), else ``None``."""
        if self.over:
            return "helper-output-malformed"
        if not self.open and self.p.poll() is not None:
            try:
                probe_answer(parse_helper_line(bytes(self.out), "probe"), self.sup.req.vendor)
            except Refused as e:
                return e.name
            return "closed"
        if now >= self.deadline:
            return "helper-timeout"
        return None

    def end(self) -> None:
        """Ended if still running (TERM, then KILL, reaped: :meth:`Supervisor._end_helper`), pipes closed whatever
        the ending raised (L4)."""
        try:
            if self.p.poll() is None:
                self.sup._end_helper(self.p)
        finally:
            for f in (self.p.stdout, self.p.stderr):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass


class Supervisor:
    def __init__(self, req: Request, seams: Optional[Seams] = None) -> None:
        self.req = req
        self.s = seams or Seams()
        self.err = self.s.err or sys.stderr
        self.lease: Any = None
        self.session: Optional[str] = None
        self.stop_signal: Optional[int] = None
        self.end_reason: Optional[str] = None      # R5: the re-probe's verdict that ends the session (never a signal)
        self._quiet: Optional[List[str]] = None    # N5: status lines held while the relay owns his terminal
        self._relay_notes: List[str] = []
        self._stops = 0                       # stop signals received (a second one ends even a teardown)
        self._wake_r = self._wake_w = -1
        self._old: Dict[int, Any] = {}
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._master = self._slave = -1
        self._held: List[str] = []           # status lines a terminal that was not reading has yet to take
        self._output_failed = False           # the operator's stdout refused a write (not EAGAIN): the relay ended
        self._reset_untaken = False           # the reporting-mode reset did not reach his terminal whole
        self._start_refused: Optional[str] = None    # the refusal name `start` printed (display routing only)
        self._slot_index: Optional[int] = None
        self.unshown_lines = 0                # held status lines stderr never took (the end-of-run drain's count)

    # -- small ------------------------------------------------------------------------------------------------------

    def say(self, msg: str) -> None:
        """A status line on stderr. A terminal that is not reading (its emulator stalled, an ssh link wedged) never
        holds the session's end: past :data:`SAY_WAIT_S` the line, and every one after it, waits for the end of
        :meth:`run`, after teardown and the lease close, and is written there."""
        if self._quiet is not None:
            self._quiet.append(msg)
            return
        line = "levain: " + unshowable.mark(msg, allow="") + "\n"
        if self._held or not self._err_ready():
            self._held.append(line)
            return
        self.err.write(line)
        self.err.flush()

    def _err_ready(self) -> bool:
        try:
            fd = self.err.fileno()
        except (AttributeError, OSError, ValueError):
            return True                       # not an fd (a test's StringIO): it cannot stall
        try:
            return bool(select.select([], [fd], [], SAY_WAIT_S)[1])
        except (OSError, ValueError):
            return True

    def _release_held(self) -> None:
        """The held status lines, after teardown and the lease close, written non-blocking within
        :data:`HELD_DRAIN_S`: a stderr that still is not reading never holds the launcher's return. What it did not
        take is counted (:attr:`unshown_lines`) and the count tried once, without waiting, as the last line."""
        held, self._held = self._held, []
        if not held:
            return
        try:
            fd = self.err.fileno()
        except (AttributeError, OSError, ValueError):
            fd = -1
        if fd < 0:                            # not an fd (a test's StringIO): it cannot stall
            for line in held:
                try:
                    self.err.write(line)
                    self.err.flush()
                except (OSError, ValueError):
                    return
            return
        enc = getattr(self.err, "encoding", None) or "utf-8"
        ends, data = [], bytearray()
        for line in held:
            data += line.encode(enc, "backslashreplace")
            ends.append(len(data))
        total = len(data)
        try:
            was = os.get_blocking(fd)
            os.set_blocking(fd, False)
        except OSError:
            self.unshown_lines = len(held)
            return
        try:
            deadline = time.monotonic() + HELD_DRAIN_S
            if not self._flush_err(fd, deadline):
                self.unshown_lines = len(held)    # what the wrapper still holds goes first, or nothing does
                return
            gone = self._drain(fd, data, deadline, watch_stops=False)
            done = total - len(data)
            self.unshown_lines = sum(1 for e in ends if e > done)
            if self.unshown_lines and gone is None:
                cut = b"" if done in ends or done == 0 else b"\n"
                note = f"levain: {self.unshown_lines} status lines were not shown: stderr did not take them\n"
                try:
                    os.write(fd, cut + note.encode("ascii"))
                except OSError:
                    pass
        finally:
            try:
                os.set_blocking(fd, was)
            except OSError:
                pass

    def _flush_err(self, fd: int, deadline: float) -> bool:
        """Whatever ``self.err``'s own buffer still holds, flushed through its non-blocking ``fd`` before ``deadline``,
        so the held lines written past it never come out ahead of it. ``False`` when it was not taken whole."""
        while True:
            try:
                self.err.flush()
                return True
            except BlockingIOError:
                pass
            except (OSError, ValueError):
                return False
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                select.select([], [fd], [], left)
            except InterruptedError:
                pass
            except (OSError, ValueError):
                return False

    def _drain(self, fd: int, buf: bytearray, deadline: float, *, idle_s: Optional[float] = None,
               watch_stops: bool = True) -> Optional[OSError]:
        """Write ``buf`` (consumed in place) to the non-blocking ``fd`` until it is empty, ``deadline`` passes, ``fd``
        takes nothing for ``idle_s``, or a stop signal arrives during it. Returns the error that ended it when the
        fd refused a write (its reader is gone), else ``None``; what is left in ``buf`` is what it did not take."""
        stops = self._stops
        idle_end = None if idle_s is None else time.monotonic() + idle_s
        while buf:
            now = time.monotonic()
            end = deadline if idle_end is None else min(deadline, idle_end)
            if end <= now or (watch_stops and self._stops > stops):
                return None
            rl = [self._wake_r] if watch_stops and self._wake_r >= 0 else []
            try:
                ready, writable, _ = select.select(rl, [fd], [], end - now)
                if ready:
                    try:
                        os.read(self._wake_r, 64)
                    except OSError:
                        pass
                if writable:
                    n = os.write(fd, buf)
                    del buf[:n]
                    if n and idle_s is not None:
                        idle_end = time.monotonic() + idle_s
            except (BlockingIOError, InterruptedError):
                continue
            except (OSError, ValueError) as e:
                return e if isinstance(e, OSError) else OSError(errno.EBADF, str(e))
        return None

    def _lease_fds(self) -> Tuple[int, ...]:
        return (self.lease.fileno(),) if self.lease is not None else ()

    def _check(self) -> None:
        if self.stop_signal is not None:
            raise Refused("stopped", signal.Signals(self.stop_signal).name)

    def helper(self, *args: str, stdin: Optional[bytes] = b"", stoppable: bool = True) -> Dict[str, object]:
        """One verb off the pty: stdin (``None``: ``/dev/null``) and stdout are pipes, stderr is read and shown
        escaped (root's refusal detail may quote a slot-made name). The pipes are driven in a bounded select loop that also watches the wake pipe:
        a stop signal (``stoppable``; for teardown, which runs AFTER the first stop, only a later one) or the verb's
        :attr:`Seams.helper_deadline_s` ends the helper (TERM, then KILL, reaped) and refuses ``stopped`` /
        ``helper-timeout``. An OS failure to run or talk to it is ``helper-unrunnable``, never a traceback."""
        verb = args[0]
        argv = [*self.s.helper, *args]                                                  # type: ignore[misc]
        err = bytearray()
        try:
            try:
                p = self.s.spawn(argv, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, lease_fds=self._lease_fds(),
                                 env=self.s.env)
            except OSError as e:
                raise Refused("helper-unrunnable", f"{verb}: {e.strerror or e}") from None
            try:
                out = self._drive(p, verb, stdin or b"", err, stoppable)
            except OSError as e:
                raise Refused("helper-unrunnable", f"{verb}: {e.strerror or e}") from None
            finally:
                if p.poll() is None:
                    self._end_helper(p)
                for f in (p.stdin, p.stdout, p.stderr):
                    if f is not None:
                        try:
                            f.close()
                        except OSError:
                            pass
        finally:
            for line in err.decode("utf-8", "replace").splitlines()[-5:]:
                if line.strip():
                    self.say("  " + line.strip())
        return parse_helper_line(bytes(out), verb)

    def _drive(self, p: "subprocess.Popen[bytes]", verb: str, data: bytes, err: bytearray,
               stoppable: bool) -> bytearray:
        """``communicate`` with a deadline and the stop flag: stdin written, stdout and stderr read, until both
        close and the helper exits."""
        deadline = time.monotonic() + self.s.helper_deadline_s.get(verb, HELPER_DEADLINE_DEFAULT_S)
        stops = self._stops
        fout, ferr = p.stdout.fileno(), p.stderr.fileno()                          # type: ignore[union-attr]
        fin = p.stdin.fileno() if p.stdin is not None else -1
        for fd in (fin, fout, ferr):
            if fd >= 0:
                os.set_blocking(fd, False)
        view = memoryview(bytes(data))
        if not view and p.stdin is not None:
            p.stdin.close()
        out = bytearray()
        readers = [fout, ferr]
        while readers or p.poll() is None:
            if (self.stop_signal is not None) if stoppable else (self._stops > stops):
                raise Refused("stopped", f"{verb}: {signal.Signals(self.stop_signal or signal.SIGTERM).name}")
            left = deadline - time.monotonic()
            if left <= 0:
                raise Refused("helper-timeout", f"{verb} did not answer in {self.s.helper_deadline_s.get(verb)}s")
            rl = readers + ([self._wake_r] if self._wake_r >= 0 else [])
            try:
                ready, writable, _ = select.select(rl, [fin] if view else [], [], min(left, 0.2))
            except InterruptedError:
                continue
            if self._wake_r in ready:
                try:
                    os.read(self._wake_r, 64)
                except BlockingIOError:
                    pass
            if view and fin in writable:
                try:
                    view = view[os.write(fin, view):]
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    view = memoryview(b"")    # it stopped reading: its answer says why
                if not view:
                    p.stdin.close()                                                  # type: ignore[union-attr]
            for fd in (fout, ferr):
                if fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        readers.remove(fd)
                    elif fd == fout:
                        out += chunk
                        if len(out) > _HELPER_OUT_MAX:
                            raise Refused("helper-output-malformed", f"{verb}: over {_HELPER_OUT_MAX} bytes")
                    else:
                        err += chunk
                        del err[:-_HELPER_ERR_KEEP]
        return out

    def _end_helper(self, p: "subprocess.Popen[bytes]") -> None:
        """TERM, then KILL after :attr:`Seams.kill_grace_s`, then reaped. (sudo relays the TERM to the root helper;
        a KILL ends sudo alone.)"""
        for send in (p.terminate, p.kill):
            try:
                send()
            except OSError as e:
                self.say(f"could not signal the runtime helper: {e.strerror or e}")
                return
            try:
                p.wait(timeout=self.s.kill_grace_s)
                return
            except subprocess.TimeoutExpired:
                continue
        self.say(f"the runtime helper (pid {p.pid}) did not exit after SIGKILL")

    # -- signals ----------------------------------------------------------------------------------------------------

    def _on_stop(self, signum: int, _frame: object) -> None:
        if self.stop_signal is None:
            self.stop_signal = signum
        self._stops += 1
        self._poke()

    def _on_winch(self, _signum: int, _frame: object) -> None:
        self._poke(b"w")

    def _poke(self, b: bytes = b"s") -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b)
            except OSError:
                pass

    def _install(self) -> None:
        self._wake_r, self._wake_w = os.pipe()
        for fd in (self._wake_r, self._wake_w):
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        for sig in STOP_SIGNALS:
            self._old[sig] = signal.signal(sig, self._on_stop)
        self._old[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, self._on_winch)

    def _restore(self) -> None:
        for sig, h in self._old.items():
            signal.signal(sig, h)
        self._old.clear()
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                os.close(fd)
        self._wake_r = self._wake_w = -1

    # -- the run ------------------------------------------------------------------------------------------------------

    def run(self) -> int:
        self._install()
        tmp = None
        try:
            if self.s.uid() == 0:
                raise Refused("supervisor-is-root", "run `levain session` as yourself; it asks sudo for the helper")
            top, branch = repo_toplevel(self.req.repo, uid=self.s.uid(), git=self.s.git, run=self.s.run)
            tmp = Path(tempfile.mkdtemp(prefix="levain-session-", dir=self.s.tmp_parent))
            clone = tmp / "clone"
            commit = operator_clone(top, branch, clone, git=self.s.git, run=self.s.run)
            check_repo_layer(repo_layer_docs(clone, git=self.s.git, run=self.s.run))
            routes = pick_routes(read_base_map(self.s.base_map), self.req.vendor, self.req.auth)   # type: ignore[arg-type]
            identity = git_identity(top, git=self.s.git, run=self.s.run)
            if identity is None:
                self.say("no git user.name / user.email for this repo: the session cannot commit as you")
            blob = render_blob(self.req.vendor, self.req.auth, routes, identity, top.name)
            self._check()
            self.say(f"{self.req.vendor} on {top} ({branch} at {commit[:12]})")
            return self._leased(clone, branch, blob)
        except Refused as e:
            self.say(f"refused {e.name}" + (f": {e.detail}" if e.detail else ""))
            return 1
        except OSError as e:
            # DECISION: an OS failure no step names (git missing, EMFILE at the pty) is refused `os-error` with its
            # errno text; whatever session it hit was torn down on the way out (`_leased`'s finally).
            self.say(f"refused os-error: {e}")
            return 1
        finally:
            try:
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)
                if self.lease is not None:
                    self.lease.close()
                    self.lease = None
                self._restore()
            finally:
                if self._reset_untaken and not self._output_failed:     # a failed stdout's line already said it
                    # The run's last line: whatever else he missed, this is the one that leaves his shell wrong.
                    self.say("your terminal did not take the reset of the reporting modes the session turned on "
                             "(mouse, focus or paste reports may still be on): run `reset`")
                self._release_held()

    def _leased(self, clone: Path, branch: str, blob: bytes) -> int:
        self.lease = self.s.client_factory(self.s.control_socket)                       # type: ignore[misc]
        if os.get_inheritable(self.lease.fileno()):
            raise Refused("lease-fd-inheritable", "the CONTROL connection")
        nonce = self.lease.open_lease()
        self._check()
        args = ["prepare", "egress", self.req.kind, self.req.vendor, self.req.auth]
        if self.req.slot is not None:
            args.append(str(self.req.slot))
        prep = self.helper(*args, stdin=canon.canonical_bytes({"lease_nonce": nonce}))
        # A malformed answer is refused before the session is taken as known: teardown needs a name we trust, so
        # the recovery is the lease close (CONTROL revokes on EOF).
        session, index = prepare_answer(prep, host=self.s.host, store=self.s.store)   # type: ignore[arg-type]
        staging = self.s.store / "staging" / session                                     # type: ignore[operator]
        self.session = session
        self._slot_index = index
        ok = False
        try:
            self.say(f"session {session} on slot {index}")
            self._check()
            from levain.slot.stage import make_bundle

            make_bundle(clone, branch, staging / "repo.bundle", git=self.s.git, run=self.s.run,
                        tmp_parent=self.s.tmp_parent)
            self._check()
            self.helper("render", session, stdin=blob)
            self._check()
            self._launch_probe(session)
            self._check()
            if self.req.auth == "passthrough" and self.req.kind == "interactive" and self.req.vendor == "codex":
                self.say("Codex has no server-checked identity, so Levain cannot tell which account this slot's "
                         "stored login belongs to (not a check).")
            ok = self._start(session)
            if not ok and self._start_refused == "identity-changed":
                self._identity_refused(session)
            if ok and self.req.kind == "login":
                self._confirm_identity(session)
        except Refused as e:
            self.say(f"refused {e.name}" + (f": {e.detail}" if e.detail else ""))
        finally:
            # Teardown runs while the pty master is still open: closing it first would hang up `start` (sudo relays
            # the SIGHUP) before END has frozen the slot. Only then does `start` get its grace, and the master close,
            # whatever teardown raised.
            try:
                ok = self._teardown(session) and ok
            finally:
                self._end_start()
        return 0 if ok and self.stop_signal is None else 1

    def _launch_probe(self, session: str) -> None:
        """V13 at launch (R2): the session starts only on ``closed``. Every refusal (``firewall-open``, every
        ``inconclusive``, every ``unknown``) raises through :meth:`helper`, and an ``ok`` that :func:`probe_answer`
        does not take whole is refused ``firewall-probe-malformed``. stdin is ``/dev/null``, as the re-probe's
        (L6)."""
        out = probe_answer(self.helper("probe", session, stdin=None), self.req.vendor)
        self.say(f"firewall self-probe: closed ({out['host']} {out['address']} not reachable as the slot; "
                 f"{out['addresses']} addresses)")

    def _teardown(self, session: str) -> bool:
        try:
            out = self.helper("teardown", session, stoppable=False)
        except Refused as e:
            self.say(f"teardown refused {e.name}: session {session} stays leased; nothing in it was removed")
            return False
        state = out.get("state")
        if state == "RETAINED":
            where = self.s.store / "retained" / session / "tree"                         # type: ignore[operator]
            self.say(f"session {session} ended RETAINED: the work is kept at {where}")
            return True
        live = self.s.store / "live" / session                                           # type: ignore[operator]
        self.say(f"session {session} ended {state}: a step did not prove, the slot stays leased and the work stays "
                 f"in {live}")
        return False

    # -- start inside the pty (§2d 5) --------------------------------------------------------------------------------

    def _start(self, session: str) -> bool:
        """``start`` with its stdio = a pty slave this process allocated (§2d 5, C5), relayed until the vendor exits
        or a stop signal. The master and the process are left to :meth:`_end_start`, after teardown."""
        master, slave = self.s.openpty()
        # The supervisor keeps its own slave copy open until after teardown: macOS discards a pty's unread bytes
        # once the last slave closes, which would take `start`'s answer line with it. The relay ends on the
        # helper's exit with the master drained, never on the slave's EOF.
        self._master, self._slave = master, slave
        tty_in = os.isatty(self.s.stdin)
        saved = None
        try:
            if tty_in:
                try:
                    termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(self.s.stdin))
                except termios.error:
                    pass
            self._winsize(master)
            proc = self.s.spawn([*self.s.helper, "start", session], stdin=slave, stdout=slave,  # type: ignore[misc]
                                stderr=slave, lease_fds=self._lease_fds(), ctty=True, env=self.s.env)
            self._proc = proc
            if tty_in:
                saved = termios.tcgetattr(self.s.stdin)
                tty.setraw(self.s.stdin)
            tail = self._relay(master, proc)
        finally:
            try:
                if saved is not None:
                    # TCSANOW, then the typed-ahead input discarded: TCSAFLUSH would wait for the output to drain,
                    # which a terminal that is not reading never does, and teardown is still to come.
                    termios.tcsetattr(self.s.stdin, termios.TCSANOW, saved)
                    termios.tcflush(self.s.stdin, termios.TCIFLUSH)
            finally:
                notes, self._relay_notes = self._relay_notes, []
                for note in notes:            # N5: what the relay held, now that his terminal is his again
                    self.say(note)
        if self.stop_signal is not None:
            self.say(f"{signal.Signals(self.stop_signal).name}: ending session {session}")
            return False
        if self.end_reason is not None:
            self.say(f"{self.end_reason}: ending session {session}")
            return False
        if self._output_failed:
            return False                      # the relay said why; the vendor may still run: teardown ends it
        try:
            code = proc.wait(timeout=self.s.start_grace_s)
        except subprocess.TimeoutExpired:
            self.say("start did not exit after the vendor's terminal closed: ending the session")
            return False
        try:
            res = start_result(tail)
        except Refused as e:
            self.say(f"start refused {e.name}")
            self._start_refused = e.name
            return False
        self.say(f"{self.req.vendor} exited {res.get('exit')}" + ("" if code == 0 else f" (helper exit {code})"))
        return code == 0

    def _identity_refused(self, session: str) -> None:
        """``start`` refused ``identity-changed``: the account shown comes from root's ``status`` record only (its
        ``identity``), never from the pty tail, which the slot can write. The tail's refusal name only routes here;
        the text is said only when the record also says ``identity-changed``. A refusal or any other error is said,
        never raised: teardown still runs. No line says where the login is: start's park-back is best-effort and
        END's park is the backstop (L3 r1), so teardown's own line is the only custody answer."""
        try:
            self._identity_refused_said(session)
        except Exception as e:   # noqa: BLE001 - said, never raised over the teardown that follows
            why = e.name if isinstance(e, Refused) else type(e).__name__
            try:
                self.say(f"start refused identity-changed, but root's record could not be read ({why}); no account shown. "
                         "Nothing was launched.")
            except Exception:   # noqa: BLE001 - stderr itself refused; the teardown still runs
                pass

    def _identity_refused_said(self, session: str) -> None:
        try:
            oid, result = identity_answer(self.helper("status", session, stdin=None))
        except Refused as e:
            self.say(f"start refused identity-changed, but root's record could not be read (refused {e.name}); no account "
                     "shown. Nothing was launched.")
            return
        vendor = self.req.vendor
        name = {"claude": "Claude", "codex": "Codex"}.get(vendor, vendor)
        if result != "identity-changed" or oid is None:
            self.say("identity-changed: the slot's stored login did not match its baseline; root's record does not "
                     "name an account, so none is shown. Nothing was launched.")
            return
        n = self._slot_index
        self.say(f"slot {n}'s stored {name} login now answers as account {oid} (the server's answer), not the "
                 f"account confirmed as this slot's baseline. Nothing was launched. To use another account on "
                 f"this slot, sign in again: levain session {vendor} "
                 f"--auth passthrough --login --slot {n}, and answer yes.")

    # -- a login session's confirm-identity (§5.1, §7.6) ---------------------------------------------------------------

    def _confirm_identity(self, session: str) -> None:
        """After a login session's vendor exited and BEFORE teardown (the lease is live, which the helper's verb
        needs): whether to ask, and the id the prompt shows, come from root's state record, read through ``status
        <session>`` (its ``identity``: CONTROL's field, which the slot cannot write). Start's pty tail is never read
        for it: the slot can write that tail. His answer goes to ``confirm-identity <session> yes|no``; root writes
        the baseline only on ``yes``, only from that record, and says so (``{baseline: written}``), which is the
        only answer reported as written. No answer (a stop signal, EOF, :data:`CONFIRM_S`) sends nothing. A refusal
        is said, never raised: teardown still runs."""
        from levain.slot.identity import BASELINE_WRITTEN, NOT_A_CHECK

        if self.stop_signal is not None:
            return
        try:
            oid, result = identity_answer(self.helper("status", session, stdin=None))
        except Refused as e:
            self.say(f"no baseline written: refused {e.name}")
            return
        if oid is None:
            if result == "identity-call-unbuilt":
                self.say("no baseline written for this slot: the Claude identity call is not built yet "
                         "(identity-call-unbuilt)")
            else:
                self.say("no baseline written for this slot: " + (f"the identity step recorded {result}"
                                                                   if result else "no identity was recorded"))
            return
        name = {"claude": "Claude", "codex": "Codex"}.get(self.req.vendor, self.req.vendor)
        if oid == NOT_A_CHECK:
            prompt = (f"levain: {name} has no server-validated identity call, so this login is recorded as not a "
                      f"check: Levain cannot tell which account it is. Keep it as this slot's {name} baseline? "
                      "[y/N] ")
        else:
            prompt = (f"levain: this login is {name} account {oid} (the server's answer). Keep it as this slot's "
                      f"{name} baseline? [y/N] ")
        answer = (self.s.confirm or self._ask)(prompt)
        if answer is None or self.stop_signal is not None:
            self.say("no answer: no baseline written")
            return
        try:
            res = self.helper("confirm-identity", session, "yes" if answer else "no", stdin=None)
        except Refused as e:
            self.say(f"no baseline written: refused {e.name}")
            return
        if not answer:
            self.say("not confirmed: no baseline written")
        elif res == {"baseline": BASELINE_WRITTEN}:
            self.say(f"baseline written: this slot's {name} login is its baseline"
                     + (" (not a check)" if oid == NOT_A_CHECK else f" (account {oid})"))
        else:
            self.say("no baseline written: root's answer did not say it wrote one")

    def _ask(self, prompt: str) -> Optional[bool]:
        """The prompt on stderr, one line read from his terminal (cooked again once the relay ended), watching the
        wake pipe so a stop signal ends the wait. ``y`` / ``yes`` (any case) is True; any other line False (one past
        :data:`_CONFIRM_LINE_MAX` is False, and the rest of it is flushed from his terminal's input); a stop, EOF, a
        stdin that is not a terminal, or :data:`CONFIRM_S` without a line is None. The deadline starts before the
        prompt is written, and the write is non-blocking within it: a stderr that is not reading cannot hold the
        lease open."""
        fd = self.s.stdin
        if not os.isatty(fd):
            return None
        deadline = self.s.clock() + CONFIRM_S
        if not self._write_prompt(prompt, time.monotonic() + CONFIRM_S):
            return None
        buf = bytearray()
        while self.stop_signal is None:
            left = deadline - self.s.clock()
            if left <= 0:
                return None
            rl = [fd] + ([self._wake_r] if self._wake_r >= 0 else [])
            try:
                ready = select.select(rl, [], [], min(left, 0.5))[0]
            except InterruptedError:
                continue
            if self._wake_r in ready:
                try:
                    os.read(self._wake_r, 64)
                except BlockingIOError:
                    pass
            if fd in ready:
                try:
                    chunk = os.read(fd, _CONFIRM_LINE_MAX)
                except BlockingIOError:
                    continue
                except OSError:
                    return None
                if not chunk:
                    return None
                buf += chunk
                if b"\n" in buf:
                    return buf.split(b"\n", 1)[0].strip().lower() in (b"y", b"yes")
                if len(buf) > _CONFIRM_LINE_MAX:
                    try:
                        termios.tcflush(fd, termios.TCIFLUSH)
                    except termios.error:
                        pass
                    return False
        return None

    def _write_prompt(self, prompt: str, deadline: float) -> bool:
        """``prompt`` on stderr, non-blocking, before ``deadline`` (``time.monotonic``) or a stop signal: ``True``
        only when it was taken whole."""
        try:
            fd = self.err.fileno()
        except (AttributeError, OSError, ValueError):
            fd = -1
        if fd < 0:                            # not an fd (a test's StringIO): it cannot stall
            try:
                self.err.write(prompt)
                self.err.flush()
                return True
            except (OSError, ValueError):
                return False
        data = bytearray(prompt.encode(getattr(self.err, "encoding", None) or "utf-8", "backslashreplace"))
        try:
            was = os.get_blocking(fd)
            os.set_blocking(fd, False)
        except OSError:
            return False
        try:
            if not self._flush_err(fd, deadline):
                return False
            return self._drain(fd, data, deadline) is None and not data
        finally:
            try:
                os.set_blocking(fd, was)
            except OSError:
                pass

    def _winsize(self, master: int) -> None:
        if not os.isatty(self.s.stdin):
            return
        try:
            ws = fcntl.ioctl(self.s.stdin, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master, termios.TIOCSWINSZ, ws)
        except OSError:
            pass

    def _relay(self, master: int, proc: "subprocess.Popen[bytes]") -> bytes:
        """operator stdin -> pty master; pty master -> :class:`TermFilter` -> operator stdout. Nothing in the loop
        blocks: the master and the operator's stdout are non-blocking and both directions are buffered, so a
        terminal that is not reading stops the master being read (past :data:`_OUTPUT_MAX`, while the helper runs),
        never the loop, and the wake pipe is always watched. On macOS the V13 re-probe runs on this loop's tick
        (:class:`_Reprobe`, :class:`_ProbeRun`): its fds join the select, and what it has to say is held until the
        relay ends (N5). Ends on a stop signal, on an :attr:`end_reason`, on a stdout that refused a write
        (its bytes counted, :attr:`_output_failed` set), or as soon as the master is done (EOF / EIO, or the helper
        exited and a read pass found it empty): what the terminal has yet to take then gets :meth:`_last_output`,
        bounded, so a terminal that is not reading never holds teardown. Returns the stream's raw tail (where
        ``start``'s line is)."""
        filt = TermFilter()
        tail = bytearray()
        stdin_open, master_done = True, False
        out_err: Optional[OSError] = None
        pending = bytearray()      # operator input the pty has not taken yet
        outbuf = bytearray()       # filtered output the operator's terminal has not taken yet
        over = 0                   # filtered bytes read after the helper's exit past _AFTER_EXIT_MAX: counted, not kept
        exited_at: Optional[float] = None
        unread = -1                # raw bytes the pty still held when the after-exit read was cut (-1: not cut)
        stdout = self.s.stdout
        # V13 R5: the macOS re-probe, on this loop's own tick (Linux runs none: §2d 10).
        reprobe = (_Reprobe(self.s.clock(), self.s.reprobe_s)
                   if self.s.host == "darwin" and self.session is not None else None)
        run: Optional[_ProbeRun] = None
        # O_NONBLOCK is a property of the open file, which stdout may share with stdin (one tty): stdin's read
        # then answers EAGAIN too, and both are put back as they were on every way out.
        was_blocking = os.get_blocking(stdout)
        os.set_blocking(master, False)
        os.set_blocking(stdout, False)
        self._quiet = []
        try:
            while (self.stop_signal is None and self.end_reason is None and not master_done
                   and out_err is None):
                exited = proc.poll() is not None      # read BEFORE the select: all its output is in the pty
                if reprobe is not None and run is None and not exited and reprobe.due(self.s.clock()):
                    reprobe.started(self.s.clock())
                    try:
                        run = _ProbeRun(self, self.session, self.s.clock())          # type: ignore[arg-type]
                    except Refused as e:
                        self.end_reason = reprobe.finished(e.name, e.detail)
                    except OSError as e:
                        self.end_reason = reprobe.finished("helper-unrunnable", str(e.strerror or e))
                    if self.end_reason is not None:
                        break
                if exited:
                    if exited_at is None:
                        exited_at = time.monotonic()
                    elif time.monotonic() - exited_at > AFTER_EXIT_S:
                        unread = _pty_pending(master)
                        break                         # still written: something left in the slot; teardown ends it
                # Once the helper is gone holding the master back holds nothing up but the end of the stream.
                read_master = exited or len(outbuf) < _OUTPUT_MAX
                rl = [self._wake_r] + ([master] if read_master else [])
                # Once the helper is gone his input has no reader, and the pty would echo it after `start`'s line
                # (sudo puts the slave back as it found it, cooked), making the line unreadable: none is read or
                # forwarded.
                if stdin_open and len(pending) < _INPUT_MAX and not exited:
                    rl.append(self.s.stdin)
                if run is not None:
                    rl += run.open
                wl = ([master] if pending and not exited else []) + ([stdout] if outbuf else [])
                try:
                    ready, writable, _ = select.select(rl, wl, [], 0.5)
                except InterruptedError:
                    continue
                if run is not None:
                    run.pump(ready)
                    name = run.result(self.s.clock())
                    if name is not None:
                        run.end()
                        detail, run = run.detail(), None
                        self.end_reason = reprobe.finished(name, detail)               # type: ignore[union-attr]
                if self._wake_r in ready:
                    try:
                        pokes = os.read(self._wake_r, 64)
                    except BlockingIOError:
                        pokes = b""
                    if b"w" in pokes:
                        self._winsize(master)
                if stdout in writable:
                    try:
                        del outbuf[:os.write(stdout, outbuf)]
                    except (BlockingIOError, InterruptedError):
                        pass
                    except OSError as e:
                        out_err = e               # his terminal is gone: the relay ends, the loss is counted below
                if master in writable and pending and not exited:
                    try:
                        del pending[:os.write(master, pending)]
                    except BlockingIOError:
                        pass
                    except OSError:
                        pending.clear()
                        stdin_open = False
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except BlockingIOError:
                        data = None
                    except OSError as e:
                        if e.errno != errno.EIO:
                            raise
                        data = b""
                    if data == b"":
                        master_done = True
                    elif data:
                        tail = (tail + data)[-8192:]
                        shown = filt.feed(data)
                        room = max(0, _OUTPUT_MAX + _AFTER_EXIT_MAX - len(outbuf))
                        outbuf += shown[:room]
                        over += max(0, len(shown) - room)
                elif read_master and exited:
                    master_done = True            # the helper is gone and nothing was left to read
                if self.s.stdin in ready:
                    try:
                        data = os.read(self.s.stdin, 65536)
                    except BlockingIOError:
                        data = None
                    except OSError:
                        data = b""
                    if data:
                        pending += data
                    elif data == b"":
                        stdin_open = False
        finally:
            try:
                if run is not None:
                    run.end()                     # a probe in flight is ended first (R5), before the drain and teardown
            finally:
                held, self._quiet = self._quiet or [], None
                if reprobe is not None:
                    held += reprobe.notes
                    if reprobe.dropped:
                        held.append(f"{reprobe.dropped} more firewall self-probe notes were not kept")
                self._relay_notes += held
            left = 0
            try:
                rest, reset = outbuf + filt.flush(), filt.reset()
                if out_err is None:
                    left, untaken, out_err = self._last_output(rest, reset)
                else:
                    left, untaken = len(rest), len(reset)
                self._reset_untaken = self._reset_untaken or untaken > 0
            finally:
                os.set_blocking(stdout, was_blocking)
            if out_err is not None:
                self._output_failed = True
                also = (", and the reset of the reporting modes the session turned on did not reach it"
                        if self._reset_untaken else "")
                self.say(f"your terminal refused the session's output ({out_err.strerror or out_err}){also}: "
                         f"ending the session")
            if left:
                self.say(f"your terminal did not take the session's last {left} bytes")
            if over:
                self.say(f"{over} bytes written to the terminal after the vendor's helper exited were not shown")
            if unread >= 0:
                self.say(f"the vendor's terminal was still being written {AFTER_EXIT_S:g} s after its helper exited: "
                         f"the relay stopped reading it ({unread} bytes it held, and whatever came after, not shown)")
        return bytes(tail)

    def _last_output(self, rest: bytearray, reset: bytes) -> Tuple[int, int, Optional[OSError]]:
        """The session's last bytes, then the reporting-mode reset, on the non-blocking stdout. The rest gets the
        drain bounded by :data:`FINAL_DRAIN_S` of the terminal taking nothing (and :data:`FINAL_DRAIN_MAX_S` in all),
        or none at all when a stop signal ended the relay (he asked for the end; the vendor is not kept for it);
        the reset then gets :data:`FINAL_RESET_S` of its own, so abandoning the rest never abandons it (led by CAN
        when the rest was cut, which may have been inside a sequence). A terminal that is not reading does not hold
        teardown. Returns (rest bytes not taken, reset bytes not taken, the error if stdout refused a write)."""
        err = None
        if self.stop_signal is None:          # the relay ended on a stop: the rest is abandoned at once (counted)
            # N4: an open wall's end is not held for a terminal that is slow to take the rest.
            bound = URGENT_DRAIN_S if self.end_reason == "firewall-open" else FINAL_DRAIN_MAX_S
            err = self._drain(self.s.stdout, rest, time.monotonic() + bound, idle_s=FINAL_DRAIN_S)
        left = len(rest)
        if not reset:
            return left, 0, err
        if err is not None:
            return left, len(reset), err
        tail = bytearray((_CAN if left else b"") + reset)
        err = self._drain(self.s.stdout, tail, time.monotonic() + FINAL_RESET_S)
        return left, len(tail), err

    def _end_start(self) -> None:
        """After teardown: teardown ended the vendor (END freezes and kills the slot's processes), so ``start``
        returns on its own; the master is drained (never shown) meanwhile so a full pty cannot block it. Past
        :attr:`Seams.start_grace_s` it gets TERM, past another KILL, and it is reaped. Then the master closes."""
        proc, master, slave = self._proc, self._master, self._slave
        self._proc, self._master, self._slave = None, -1, -1
        try:
            deadline = time.monotonic() + self.s.start_grace_s
            sends = [] if proc is None else [proc.terminate, proc.kill]
            while proc is not None and proc.poll() is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    if not sends:
                        self.say(f"start (pid {proc.pid}) did not exit after SIGKILL")
                        break
                    try:
                        sends.pop(0)()
                    except OSError as e:
                        self.say(f"could not signal start: {e.strerror or e}")
                        break
                    deadline = time.monotonic() + self.s.start_grace_s
                    continue
                if master < 0:
                    try:
                        proc.wait(timeout=min(0.2, left))
                    except subprocess.TimeoutExpired:
                        pass
                elif select.select([master], [], [], min(0.2, left))[0]:
                    try:
                        data = os.read(master, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b""
                    if not data:
                        os.close(master)
                        master = -1
        finally:
            for fd in (slave, master):
                if fd >= 0:
                    os.close(fd)


def run_session(req: Request, seams: Optional[Seams] = None) -> int:
    return Supervisor(req, seams).run()


__all__ = ["HOOKS_BY_AUTH", "REPO_SETTINGS", "Request", "STOP_SIGNALS", "Seams", "Supervisor", "TermFilter",
           "VENDOR_HOSTS", "check_repo_layer", "git_identity", "operator_clone", "parse_helper_line", "pick_routes",
           "read_base_map", "render_blob", "repo_layer_docs", "repo_toplevel", "run_session", "spawn",
           "start_result"]
