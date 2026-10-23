"""The sealed run's root side, piece 5c-ii-a: the ONE classifier and the publish transaction (S1 session 5 DESIGN §1
steps 10-11, §6.0; CONTRACT_EDITS E12, E20; cut: ``project_memory/1022-18/adopt/CUT.md``).

Root classifies a run exactly ONCE (:func:`classify`), from its own observations (:class:`RunObservation`) and the
result line read from fd 4 as DATA, in the total order of DESIGN §1 step 10: (1) memory (an unreadable Linux
``memory.events.local`` takes that slot as ``export-image-unavailable`` and ranks first), (2) the host volume short,
(3) the strict-boolean ``enospc: true`` ONLY on a plain non-zero exit, (4) ``<role>-failed:<reason>``. SUCCESS is exit
0, no root failure observation and a fully valid line with ``enospc`` false; every other outcome is a DENIAL that
publishes nothing. :func:`publish_if_success` is the one call site that reads the answer: only SUCCESS reaches
:func:`check_outputs` and :func:`transact`, so an importer's ``out/`` pair left by a failed, killed or line-less run is
never published (the 5b2 carry in CONTRACT_EDITS, head 1022+8).

The transaction (r11 S14-1, r12 S15-1/S15-3, r13 S16-6): copy each output into a fresh ``<s>.new/`` while hashing (the
bundle's header parsed by :mod:`levain.slot.bundlefmt` as it streams), fsync each file, fsync ``<s>.new``, then COMMIT
by the no-replace rename where no ``<s>`` exists or by the atomic EXCHANGE where one does, fsync the class dir, and only
then delete the older tree (now at ``<s>.new``). The exchange is the only commit form: a filesystem that refuses it (or
the no-replace call) fails CLOSED as ``<role>-failed:publish`` with detail ``exchange-unsupported``; there is no
plain-rename fallback. Any OSError before the commit removes ``<s>.new`` and leaves ``<s>`` as it was.

Known edge (L3 r1 F8): on darwin these are plain ``fsync``s, not ``F_FULLFSYNC``, so a power loss can still drop
writes in the drive's cache. DESIGN r12 S15-3 rules ``F_FULLFSYNC`` NOT required here: a torn tree is caught by the
pinned digest (``export-changed``) and the adopt is re-run. (The s3b credential writer flushes fully because a lost
credential has no such re-run.)

Known edge (L3 r2): an importer refusal whose base is ``time``, ``signal`` or ``exit`` rides the wire under the same
name as root's own observation (``importer-failed:time``); the whole refusal stays in the answer's detail.

Root code: runs under ``/usr/bin/python3 -IS`` (3.9). Seams are injected parameters (:class:`PublishSeams`), never
env flags. The image, the leaf, the watchdog and the domain proof are piece 5c-ii-b; the wiring into adopt-open is
5c-ii-c.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

from levain.contracts import ContractError, canon
from levain.slot import Refused, write_all
from levain.slot import bundlefmt as BF
from levain.slot import fsck_ids as FI
#: The importer's cap on its one line, the newline included: ONE constant, the writer's.
from levain.slot.importer import RESULT_CAP
from levain.slot.proc import V12K_SECONDS

# --- the two commit calls ---------------------------------------------------------------------------------------

if sys.platform == "darwin":
    #: ``renameatx_np`` flags (<stdio.h>): RENAME_SWAP, RENAME_EXCL.
    EXCHANGE, NOREPLACE, _SYMBOL = 0x2, 0x4, "renameatx_np"
else:
    #: ``renameat2`` flags (<linux/fs.h>): RENAME_EXCHANGE, RENAME_NOREPLACE.
    EXCHANGE, NOREPLACE, _SYMBOL = 0x2, 0x1, "renameat2"

#: The errnos that mean "this filesystem does not do it" (DESIGN §1 step 11, r12 S15-1).
UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS})
SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


class ExchangeUnsupported(Exception):
    """The exchange or the no-replace rename is refused by the filesystem, or the libc symbol is missing."""


_FN: list = []


def _rename_fn():
    """The libc commit call, or None when this libc has no such symbol (a missing symbol is ``exchange-unsupported``,
    never a reason to reach for another form)."""
    if not _FN:
        fn = None
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            fn = getattr(libc, _SYMBOL)
            fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            fn.restype = ctypes.c_int
        except (OSError, AttributeError, TypeError):
            fn = None
        _FN.append(fn)
    return _FN[0]


def rename2(fd1: int, name1: str, fd2: int, name2: str, flag: int) -> None:
    """One ``renameatx_np`` / ``renameat2`` call: an unsupported refusal raises :class:`ExchangeUnsupported`, any
    other errno ``OSError``."""
    fn = _rename_fn()
    if fn is None:
        raise ExchangeUnsupported(f"no {_SYMBOL} in this libc")
    if fn(fd1, os.fsencode(name1), fd2, os.fsencode(name2), flag) != 0:
        e = ctypes.get_errno()
        if e in UNSUPPORTED_ERRNOS:
            raise ExchangeUnsupported(errno.errorcode.get(e, str(e)))
        raise OSError(e, os.strerror(e), name1)


# --- roles ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Role:
    """The names one sealed run answers with (CONTRACT_EDITS E20): the importer's under adopt-open, the verifier's
    under adopt-verify. ``missing`` is the reason a line that is valid but incomplete gets (the importer's terminal
    frame, the verifier's outputs). ``verbatim``: the role's own verdict names, answered AS THEY ARE (head ruling
    1022+20 Q1); empty for the importer, whose refusals ride as ``importer-failed:<base>`` (A1b)."""

    failed: str
    memory: str
    space: str
    work_cap: str
    missing: str
    staged_help: str
    verbatim: frozenset = frozenset()


IMPORTER = Role("importer-failed", "adopt-cap:memory", "adopt-space-exhausted", "adopt-work-cap", "terminal-missing",
                "L/export/<class>/<s>.new")
#: The verifier's §6.1 verdict names (head ruling 1022+20 Q1, from 5d-i S1's Q1): every name
#: ``adopt_verify.quarantine_verify`` raises (derive: ``grep -on 'Refused([^,]*' levain/slot/adopt_verify.py``, plus
#: ``bundlefmt.MISMATCH`` from its header reads), ``fsck:<id>`` over :func:`fsck_ids.closed_ids` and ``fsck:other``.
#: A refusal on the verifier's line outside this set is ``verifier-failed:result-malformed``: the set is closed.
VERIFIER_NAMES = frozenset({
    "base-bundle-mismatch", "base-mismatch", BF.MISMATCH, "bundle-extra-objects", "bundle-special-unsent",
    "bundle-thin", "bundle-tip-in-base", "bundle-tip-not-commit", "export-result-mismatch", "fsck:other",
    "preview-cap:commits", "preview-cap:output", "preview-cap:tree-depth", "preview-graph-unresolved"}) | frozenset(
        f"fsck:{i}" for i in FI.closed_ids())
VERIFIER = Role("verifier-failed", "preview-cap:memory", "preview-space-exhausted", "preview-space-exhausted",
                "output-missing", "<s>/verify.new", VERIFIER_NAMES)
#: The verifier's three outputs, in publish order (``verify_run.OUTPUTS``: the writer's names, one tuple).
VERIFIER_OUTPUTS = ("verified.json", "q.idx", "preview.txt")

# --- the result line --------------------------------------------------------------------------------------------

LINE_KEYS = frozenset({"v", "terminal", "enospc", "refused", "result", "detail", "walk", "ignored_git_paths"})
RESULTS = frozenset({"delta", "unrelated", "adopt-nothing-new"})
#: An importer refusal name root surfaces verbatim as ``importer-failed:<name>`` (5b1 carry 1): printable ASCII, no
#: space, so a hostile importer cannot put a control byte into the operator's terminal through it.
_NAME = re.compile(r"[a-z][a-z0-9-]*(?::[\x21-\x7e]{1,512})?")
#: The line's nesting is two (the line, then ``walk``); deeper is malformed BEFORE any decode (L3 r1 F3: a ~60K-deep
#: line under :data:`RESULT_CAP` made ``json.loads`` raise ``RecursionError``).
LINE_MAX_DEPTH = 4
#: Ruling A1b (L3 r1 F2): the refusal line carries ``<role>-failed:<base>``, the base being the importer's refusal up
#: to its first ``:``; the whole refusal rides in the answer's detail. ``runtime_grammar.refusal_line`` turns any name
#: off ``runtime_grammar._NAME_RE`` (or over 64 chars) into ``internal``; the BASE there is ``[a-z0-9]+(-[a-z0-9]+)*``
#: (only the ``:`` qualifier may carry uppercase and ``.``), so a base that cannot ride is spelled by this ONE table,
#: and one off the table by :data:`WIRE_FALLBACK`.
WIRE_BASES: Mapping[str, str] = {}
WIRE_FALLBACK = "refused"
_WIRE_BASE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)
_WIRE_MAX = 64


def wire_reason(role_failed: str, refused: str) -> str:
    """The grammar-safe reason for an importer (or verifier) refusal ``refused`` (ruling A1b)."""
    base = refused.split(":", 1)[0]
    base = WIRE_BASES.get(base, base)
    if not _WIRE_BASE.fullmatch(base) or len(role_failed) + 1 + len(base) > _WIRE_MAX:
        return WIRE_FALLBACK
    return base


def _depth_over(raw: bytes, bound: int) -> bool:
    """True when ``raw``'s JSON nesting (brackets outside strings) passes ``bound``; iterative, no decode."""
    depth, in_str, esc = 0, False, False
    for c in raw:
        if in_str:
            if esc:
                esc = False
            elif c == 0x5C:
                esc = True
            elif c == 0x22:
                in_str = False
        elif c == 0x22:
            in_str = True
        elif c in (0x5B, 0x7B):
            depth += 1
            if depth > bound:
                return True
        elif c in (0x5D, 0x7D):
            depth -= 1
    return False


class LineBad(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason      # "result-oversize" | "result-malformed"
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class Line:
    terminal: bool
    enospc: bool
    refused: Optional[str]
    result: Optional[str]
    walk: Tuple[int, int]


def _nonneg_int(x: object) -> bool:
    return type(x) is int and x >= 0


def _no_dup(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _no_constant(name):
    raise ValueError(f"non-finite number {name}")


def parse_importer_line(raw: Optional[bytes]) -> Line:
    """The importer's one line (CONTRACT_EDITS, appended 10-22 by head 1022+8) as data, or :class:`LineBad`. Strict:
    exactly one newline-terminated line within :data:`RESULT_CAP`, byte-equal to its own canonical JSON, the exact
    key set, strict booleans, non-bool integers. Consistency with the exit status is :func:`classify`'s."""
    if raw is None or raw == b"":
        raise LineBad("result-malformed", "no result line")
    if len(raw) > RESULT_CAP:
        raise LineBad("result-oversize", f"more than {RESULT_CAP} bytes")
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise LineBad("result-malformed", "not one newline-terminated line")
    if _depth_over(raw, LINE_MAX_DEPTH):
        raise LineBad("result-malformed", f"nested deeper than {LINE_MAX_DEPTH}")
    try:
        obj = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_no_dup, parse_constant=_no_constant)
        if not isinstance(obj, dict) or canon.canonical_bytes(obj) + b"\n" != raw:
            raise ValueError("not canonical JSON")
    except (ValueError, UnicodeDecodeError, ContractError, TypeError, RecursionError, MemoryError) as e:
        raise LineBad("result-malformed", f"{type(e).__name__}: {e}"[:200]) from None
    if set(obj) != LINE_KEYS:
        raise LineBad("result-malformed", "the key set")
    walk = obj["walk"]
    ok = (type(obj["v"]) is int and obj["v"] == 1
          and type(obj["terminal"]) is bool and type(obj["enospc"]) is bool
          and (obj["refused"] is None or (isinstance(obj["refused"], str) and _NAME.fullmatch(obj["refused"])))
          and (obj["result"] is None or obj["result"] in RESULTS)
          and isinstance(obj["detail"], str)
          and isinstance(walk, dict) and set(walk) == {"entries", "bytes"}
          and _nonneg_int(walk["entries"]) and _nonneg_int(walk["bytes"])
          and (obj["ignored_git_paths"] is None or _nonneg_int(obj["ignored_git_paths"])))
    if not ok:
        raise LineBad("result-malformed", "a field's type or value")
    return Line(obj["terminal"], obj["enospc"], obj["refused"], obj["result"], (walk["entries"], walk["bytes"]))


# --- the one classifier -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunObservation:
    """Everything root observed of one run, read at seal-out once the domain is proven empty (Linux: after the leaf
    reads ``populated 0``; macOS: after the watchdog stopped), so a late kill is in it (r11 S14-5).

    ``exit``: ``"exit"`` (a plain exit, ``code`` its status), ``"signal"`` or ``"time"`` (root's own observations,
    never overridden by the line). ``memory_killed``: the leaf's ``oom_kill > 0`` or the watchdog's kill flag.
    ``memory_unreadable``: the leaf's ``memory.events.local`` could not be read. ``host_short``: root's host-side
    reading (``f_bavail x f_frsize`` below the image's unallocated remainder). ``line``: the bytes read from fd 4,
    at most :data:`RESULT_CAP` + 1 (None = none)."""

    exit: str
    code: Optional[int]
    memory_killed: bool = False
    memory_unreadable: bool = False
    host_short: bool = False
    line: Optional[bytes] = None


@dataclass(frozen=True)
class Outcome:
    success: bool
    name: Optional[str]           # the denial's refusal name; None on SUCCESS
    detail: str
    line: Optional[Line]


def classify(obs: RunObservation, role: Role = IMPORTER,
             parse: Callable[[Optional[bytes]], Line] = parse_importer_line) -> Outcome:
    """The run's ONE classification (DESIGN §1 step 10, r10 S13-1). Order: (1) ``export-image-unavailable`` for an
    unreadable leaf, then ``role.memory``; SUCCESS or not; (2) ``role.space`` on a short host reading; (3)
    ``role.work_cap`` only on a plain non-zero exit with a valid line carrying ``enospc: true``; (4)
    ``role.failed:<reason>``, reason ``signal``/``time`` > ``result-oversize`` > ``result-malformed`` >
    ``role.missing`` > the importer's refusal (its base, :func:`wire_reason`; the whole refusal in the detail, A1b)
    > ``exit``. A role with a ``verbatim`` set (the verifier) answers a listed refusal as ITSELF in that slot, and
    one off the set as ``result-malformed`` (head ruling 1022+20 Q1)."""
    if obs.memory_unreadable:
        return Outcome(False, "export-image-unavailable", "the run leaf's memory.events.local could not be read", None)
    if obs.memory_killed:
        return Outcome(False, role.memory, "", None)
    plain = obs.exit == "exit" and type(obs.code) is int
    line: Optional[Line] = None
    bad: Optional[LineBad] = None
    try:
        line = parse(obs.line)
    except LineBad as e:
        bad = e
    if line is not None and plain:
        if obs.code == 0 and not (line.result is not None and line.refused is None and not line.enospc
                                  and line.terminal):
            bad = LineBad("result-malformed", "an exit 0 line must carry a result, no refusal, enospc false")
        elif obs.code != 0 and line.result is not None:
            bad = LineBad("result-malformed", "a result on a non-zero exit")
        if bad is not None:
            line = None
    if plain and obs.code == 0 and line is not None:
        return Outcome(True, None, "", line)
    if obs.host_short:
        return Outcome(False, role.space, "", line)
    if plain and obs.code != 0 and line is not None and line.enospc:
        return Outcome(False, role.work_cap, line.refused or "", line)
    if not plain:
        reason, detail = (obs.exit if obs.exit in ("signal", "time") else "exit"), ""
    elif bad is not None:
        reason, detail = bad.reason, bad.detail
    elif line is not None and not line.terminal:
        reason, detail = role.missing, line.refused or ""
    elif line is not None and line.refused and role.verbatim:
        if line.refused in role.verbatim:
            return Outcome(False, line.refused, "", line)      # Q1: the verifier's verdict, verbatim
        reason, detail = "result-malformed", f"a refusal off the verifier's closed set: {line.refused[:200]}"
    elif line is not None and line.refused:
        reason, detail = wire_reason(role.failed, line.refused), line.refused     # A1b
    else:
        reason, detail = "exit", f"status {obs.code}"
    return Outcome(False, f"{role.failed}:{reason}", detail, line)


# --- seams ------------------------------------------------------------------------------------------------------

def _no_fault(point: str) -> None:
    return None


def _no_log(event: str) -> None:
    return None


def _store_delete(dfd: int, name: str) -> bool:
    from levain.slot import store_verbs as SV

    def _no_cas(*_a, **_k):
        raise AssertionError("delete_at writes no record")

    return SV.delete_at(dfd, name, SV.StoreSeams(cas=_no_cas), root_tree=True)


@dataclass(frozen=True)
class PublishSeams:
    """``rename2``: the commit call (:func:`rename2`). ``fsync``. ``delete(dfd, name)``: purge's delete (flags cleared,
    by dirfd ``unlinkat``, :func:`levain.slot.store_verbs.delete_at`). ``fault(point)``: may raise ``OSError`` at a
    named point (``copy:<name>``, ``fsync:<name>``, ``fsync:staged``, ``commit``, ``fsync:class``, ``delete-older``).
    ``log(event)``: the order log the DONE rows read."""

    rename2: Callable[[int, str, int, str, int], None] = rename2
    fsync: Callable[[int], None] = os.fsync
    delete: Callable[[int, str], bool] = _store_delete
    fault: Callable[[str], None] = _no_fault
    log: Callable[[str], None] = _no_log


_DIR_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
PROBE_NAMES = (".xchg-probe.a", ".xchg-probe.b")
STAGING_SUFFIX = ".new"
_CHUNK = 1 << 20


def _errname(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, str(e.errno))


def _publish_refusal(role: Role, detail: str) -> Refused:
    return Refused(f"{role.failed}:publish", detail)


# --- seal-in's two publish checks -------------------------------------------------------------------------------

def clear_staging(class_dfd: int, staged: str, role: Role, seams: PublishSeams) -> None:
    """DESIGN §1 step 6 (r12 S15-2, r13 S16-4): delete a stale ``<s>.new`` (or ``verify.new``) BEFORE the run, flags
    cleared first; one that cannot be deleted refuses here, never after the import."""
    try:
        if seams.delete(class_dfd, staged):
            seams.log(f"stale-staging deleted {staged}")
    except (OSError, Refused) as e:
        raise _publish_refusal(role, "stale-staging") from Refused(
            "stale-staging", f"{role.staged_help} could not be deleted ({e}); purge the session, or clear the fault "
            f"at that path as root")


def probe_exchange(class_dfd: int, role: Role, seams: PublishSeams) -> None:
    """DESIGN §1 step 6, the early probe (r13 S16-3; HEAD_r16 Q-r13-2): run on EVERY seal-in, a first publish
    included. Two scratch dirs whose names no session id can be, a crash's pair deleted first, the SAME exchange,
    then the SAME no-replace rename of one onto the other, which must refuse EEXIST; both removed whatever the result.
    Either call refused, or a no-replace rename that does not refuse the existing name = ``<role>-failed:publish`` /
    ``exchange-unsupported`` before the import, not at the commit."""
    a, b = PROBE_NAMES
    for n in PROBE_NAMES:
        try:
            if seams.delete(class_dfd, n):
                seams.log(f"probe residue deleted {n}")
        except (OSError, Refused) as e:          # L3 r1 F6: clear_staging's mapping
            raise _publish_refusal(role, "stale-staging") from Refused(
                "stale-staging", f"the probe residue L/export/<class>/{n} could not be deleted ({e}); purge the "
                f"session, or clear the fault at that path as root")
    try:
        os.mkdir(a, 0o700, dir_fd=class_dfd)
        os.mkdir(b, 0o700, dir_fd=class_dfd)
        seams.rename2(class_dfd, a, class_dfd, b, EXCHANGE)
        seams.log("probe exchange ok")
        try:
            seams.rename2(class_dfd, a, class_dfd, b, NOREPLACE)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            seams.log("probe noreplace ok")
        else:   # the flag was dropped: the commit's existence check would not be one call
            raise ExchangeUnsupported("the no-replace rename did not refuse an existing name")
    except ExchangeUnsupported as e:
        raise _publish_refusal(role, "exchange-unsupported") from Refused(
            "exchange-unsupported", f"the export volume does not support atomic exchange ({e})")
    except OSError as e:
        raise _publish_refusal(role, _errname(e)) from e
    finally:
        for n in PROBE_NAMES:
            try:
                seams.delete(class_dfd, n)
            except (OSError, Refused):
                pass
    # L3 r1 F6: fail closed unless both names are gone (on the success path; a refusal above already stands)
    left = [n for n in PROBE_NAMES if _lexists_at(class_dfd, n)]
    if left:
        raise _publish_refusal(role, "stale-staging") from Refused(
            "stale-staging", f"the probe left {', '.join(left)} in L/export/<class>/; purge the session, or clear the "
            f"fault at that path as root")


def _lexists_at(dfd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return False
    except OSError:
        return True   # L3 r2 (5c-ii-a): absence not proven counts as left, so the caller refuses stale-staging
    return True


# --- the outputs ------------------------------------------------------------------------------------------------

def check_outputs(work_dfd: int, names: Sequence[str], absent_ok: Sequence[str], *, owner_uid: int,
                  caps: Mapping[str, int]) -> Mapping[str, int]:
    """DESIGN §1 step 11: open ``out`` under ``work_dfd`` and each named output ``O_NOFOLLOW|O_NONBLOCK``; each must be
    regular, owned by ``owner_uid``, ``nlink == 1`` and within its cap (``export-output-shape:<check>``, closed:
    ``symlink``, ``nlink``, ``owner``, ``type``, ``size``). ``absent_ok``: the names that must be ABSENT (the bundle
    on ``adopt-nothing-new``); one present, or a required one absent, = ``export-result-mismatch``. Returns name ->
    an open fd (the caller closes them)."""
    fds: dict = {}

    def shape(check: str, what: str) -> Refused:
        return Refused(f"export-output-shape:{check}", what)    # the handler below closes every fd, once

    try:
        out = os.open("out", _DIR_OPEN, dir_fd=work_dfd)
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise Refused("export-result-mismatch", "no out/") from None
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            # a symlink under O_DIRECTORY|O_NOFOLLOW: ENOTDIR measured on darwin (test_output_shapes); ELOOP per
            # open(2)'s O_NOFOLLOW text elsewhere [judged by adopt, 10-22, against open(2)]: lstat names either
            st = os.lstat("out", dir_fd=work_dfd)
            raise shape("symlink" if stat.S_ISLNK(st.st_mode) else "type", "out") from None
        raise
    try:
        for name in tuple(names) + tuple(absent_ok):
            try:
                fd = os.open(name, _LEAF_OPEN, dir_fd=out)
            except OSError as e:
                if e.errno == errno.ENOENT and name in absent_ok:
                    continue
                if e.errno == errno.ENOENT:
                    raise Refused("export-result-mismatch", f"out/{name} missing") from None
                # L3 r1 F4: a socket (ENXIO on Linux, EOPNOTSUPP on darwin), a symlink (ELOOP) or any other entry
                # that would not open is named by lstat; only an entry that IS a regular file re-raises
                try:
                    st = os.lstat(name, dir_fd=out)
                except OSError:
                    raise e from None
                if stat.S_ISLNK(st.st_mode):
                    raise shape("symlink", name) from None
                if not stat.S_ISREG(st.st_mode):
                    raise shape("type", name) from None
                raise
            fds[name] = fd
            if name in absent_ok:
                raise Refused("export-result-mismatch", f"out/{name} present") from None
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise shape("type", name)
            if st.st_nlink != 1:
                raise shape("nlink", name)
            if st.st_uid != owner_uid:
                raise shape("owner", name)
            if st.st_size > caps[name]:
                raise shape("size", name)
    except BaseException:
        for fd in fds.values():
            os.close(fd)
        fds.clear()
        raise
    finally:
        os.close(out)
    return fds


def importer_bundle_check(session: str, object_format: str) -> Callable[[BF.BundleHeader], None]:
    """Step 11's header check (§4.12): the signature of ``alloc.base.object_format``; refs only
    ``refs/levain/<s>/{head,wip}``, each once, at least one (``bundle-header-mismatch``). Prerequisites are already
    well-formed oids of the format by the parser."""
    allowed = {f"refs/levain/{session}/head".encode(), f"refs/levain/{session}/wip".encode()}

    def check(h: BF.BundleHeader) -> None:
        if h.object_format != object_format:
            raise Refused(BF.MISMATCH, f"object format {h.object_format}, alloc.base says {object_format}")
        names = [n for _oid, n in h.refs]
        if not names or len(set(names)) != len(names) or not set(names) <= allowed:
            raise Refused(BF.MISMATCH, "refs other than refs/levain/<s>/{head,wip}, each once, at least one")
    return check


# --- the transaction --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Published:
    digests: Mapping[str, str]          # name -> sha256 hex of the bytes root copied
    header: Optional[BF.BundleHeader]   # the bundle's, when one was copied
    exchanged: bool                     # True = an older tree was replaced
    #: Head ruling A2 (1022-18): what the answer must say beside ``ok: true`` (a class-dir fsync that failed after
    #: the commit call returned: the publish stands, and a crash may undo it).
    warnings: Tuple[str, ...] = ()


def transact(class_dfd: int, session: str, sources: Sequence[Tuple[str, int]], role: Role, seams: PublishSeams, *,
             gid: Optional[int] = None, bundle_check: Optional[Callable[[BF.BundleHeader], None]] = None,
             before_commit: Optional[Callable[[], None]] = None) -> Published:
    """The PUBLISH TRANSACTION, entered only after SUCCESS (DESIGN §1 step 11, r11 S14-1): the copy into a fresh
    ``<s>.new`` (root, ``gid``, files 0640 ``O_CREAT|O_EXCL``), the fsyncs, the commit. ENOSPC or EDQUOT before the
    commit = ``role.space``; any other errno = ``role.failed:publish`` (detail: the errno name; ``exists`` for the
    no-replace call's EEXIST; ``exchange-unsupported`` for a refused commit call); a bundle header off the check =
    ``bundle-header-mismatch``. In each, ``<s>.new`` is removed and ``<s>`` is as it was.

    Order (r12 S15-3), asserted by the log: ``fsync <name>`` per file, ``fsync staged``, ``exchange`` or
    ``noreplace``, ``fsync class``, ``delete older``. ``before_commit`` runs between ``fsync staged`` and the commit
    (5c-ii-b's detach); a :class:`Refused` it raises is a pre-commit refusal like any other (``<s>.new`` removed,
    ``<s>`` as it was): adopt-open's raises ``export-image-unavailable`` when the detach failed. After the commit call returns, the run is COMMITTED: a
    failed class-dir fsync or older-tree delete is logged and does not withhold the answer (an older tree left at
    ``<s>.new`` is the next seal-in's stale delete); the failed fsync also puts a warning in ``warnings`` (A2) and
    skips the older-tree delete, so a crash that reverts the exchange still finds the older tree whole (B-7)."""
    staged = session + STAGING_SUFFIX
    stage = "staging"
    made = False
    new_dfd = -1
    digests: dict = {}
    header: Optional[BF.BundleHeader] = None
    exchanged = False
    try:
        os.mkdir(staged, 0o750, dir_fd=class_dfd)
        made = True
        new_dfd = os.open(staged, _DIR_OPEN, dir_fd=class_dfd)
        if gid is not None:
            os.fchown(new_dfd, 0, gid)
        os.fchmod(new_dfd, 0o750)               # L3 r1 F1: never root's umask (sudoers does not pin it)
        for name, src in sources:
            seams.fault(f"copy:{name}")
            dst = os.open(name, _NEW_FILE, 0o640, dir_fd=new_dfd)
            try:
                if gid is not None:
                    os.fchown(dst, 0, gid)
                os.fchmod(dst, 0o640)           # L3 r1 F1
                h = hashlib.sha256()
                obs = BF.HeaderObserver(bundle_check) if name == "bundle" else None
                while True:
                    chunk = os.read(src, _CHUNK)
                    if obs is not None:
                        obs.feed(chunk)
                    if not chunk:
                        break
                    h.update(chunk)
                    write_all(dst, chunk)
                if obs is not None:
                    header = obs.finish()
                seams.fault(f"fsync:{name}")
                seams.fsync(dst)
                seams.log(f"fsync {name}")
            finally:
                os.close(dst)
            digests[name] = h.hexdigest()
        seams.fault("fsync:staged")
        seams.fsync(new_dfd)
        seams.log("fsync staged")
        fd, new_dfd = new_dfd, -1
        os.close(fd)                            # L3 r1 F5: closed BEFORE the commit, never after it
        if before_commit is not None:
            before_commit()
        stage = "commit"
        seams.fault("commit")
        try:
            older = os.lstat(session, dir_fd=class_dfd)
        except FileNotFoundError:
            older = None
        if older is None:
            seams.rename2(class_dfd, staged, class_dfd, session, NOREPLACE)
        else:
            seams.rename2(class_dfd, staged, class_dfd, session, EXCHANGE)
            exchanged = True
        stage = "committed"                     # L3 r1 F5: from here nothing deletes or fails the answer
    except ExchangeUnsupported as e:
        _remove_staged(class_dfd, staged, made, seams)
        raise _publish_refusal(role, "exchange-unsupported") from Refused(
            "exchange-unsupported", f"the export volume does not support atomic exchange ({e})")
    except Refused:
        _remove_staged(class_dfd, staged, made, seams)
        raise
    except OSError as e:
        _remove_staged(class_dfd, staged, made, seams)
        if stage == "commit" and e.errno == errno.EEXIST:
            raise _publish_refusal(role, "exists") from e
        if e.errno in SPACE_ERRNOS:
            raise Refused(role.space, _errname(e)) from e
        raise _publish_refusal(role, _errname(e)) from e
    finally:
        if new_dfd >= 0:
            os.close(new_dfd)
    # COMMITTED. Every step below is caught whole (Exception, a seam's included): a failure is logged and becomes a
    # warning at most, never a failed answer and never a delete of the tree that just went live (L3 r1 F5, A2).
    warnings: list = []

    def after(event: str) -> None:
        try:
            seams.log(event)
        except Exception as e:      # noqa: BLE001
            warnings.append(f"a step after the commit failed ({event}: {type(e).__name__}); the publish stands")

    after("exchange" if exchanged else "noreplace")
    synced = False
    try:
        seams.fault("fsync:class")
        seams.fsync(class_dfd)
        synced = True
        after("fsync class")
    except Exception as e:          # noqa: BLE001
        # A2: the rename is the commit point, so ok: true stands; the operator is told it is not yet durable.
        what = _errname(e) if isinstance(e, OSError) else type(e).__name__
        after(f"fsync class failed {what}")
        warnings.append(f"the published export's directory entry could not be synced ({what}): a crash may undo "
                        f"the publish" + (f"; the older export is kept at {role.staged_help} until the next run "
                                          f"deletes it" if exchanged else ""))
    if exchanged and not synced:
        # L3 5c-ii-b r1 B-7: a crash may still revert the exchange, and <s> would then name the older tree; it is
        # left whole at the staged name for the next seal-in's stale delete.
        after("delete older skipped: the class dir is not synced")
    elif exchanged:
        try:
            seams.fault("delete-older")
            seams.delete(class_dfd, staged)
            after("delete older")
        except Exception as e:      # noqa: BLE001
            after(f"delete older failed {e}")
    return Published(digests, header, exchanged, tuple(warnings))


def _remove_staged(class_dfd: int, staged: str, made: bool, seams: PublishSeams) -> None:
    if not made:
        return
    try:
        seams.delete(class_dfd, staged)
        seams.log("staged removed")
    except (OSError, Refused) as e:
        seams.log(f"staged left {e}")


def publish_if_success(outcome: Outcome, work_dfd: int, class_dfd: int, session: str, *, object_format: str,
                       owner_uid: int, caps: Mapping[str, int], seams: PublishSeams, gid: Optional[int] = None,
                       before_commit: Optional[Callable[[], None]] = None,
                       role: Role = IMPORTER) -> Tuple[str, Published]:
    """Seal-out's ONE gate into the publish (DESIGN §1 step 11, r10 S13-1): a DENIAL raises its name and touches
    nothing in the class dir; SUCCESS runs the output checks and the transaction. Returns ``(result, published)``,
    ``result`` derived from the copied header (prerequisites = ``delta``, none = ``unrelated``; root never
    evaluates ancestry) or ``adopt-nothing-new``.

    The VERIFIER (5d-i S2, DESIGN §6.0): ``class_dfd`` is ``<class>/<s>/`` and ``session`` is ``verify``, so the same
    transaction stages ``verify.new`` and commits ``verify`` (the exchange over an older one); its outputs are
    :data:`VERIFIER_OUTPUTS`, all required (one absent = ``verifier-failed:output-missing``), no header check; the
    result is the line's. The output fds (files inside the image) are closed before ``before_commit``, the detach, so
    DESIGN step 11's detach-then-commit order holds on Linux too, where an ``umount`` with an open file is EBUSY
    (D2)."""
    if not outcome.success or outcome.line is None:
        raise Refused(outcome.name or f"{role.failed}:exit", outcome.detail)
    verifier = role == VERIFIER                    # by value: a copy of a role is that role (L3 s2 r1 LOW 3)
    if role != IMPORTER and not verifier:
        raise _publish_refusal(role, "role-unknown")
    nothing_new = not verifier and outcome.line.result == "adopt-nothing-new"
    if verifier:
        names: Tuple[str, ...] = VERIFIER_OUTPUTS
        try:
            fds = check_outputs(work_dfd, names, (), owner_uid=owner_uid, caps=caps)
        except Refused as e:
            if e.name == "export-result-mismatch":      # check_outputs' "absent" name; the verifier's is its own
                raise Refused(f"{role.failed}:{role.missing}", e.detail) from None
            raise
    else:
        names = ("report.json",) if nothing_new else ("bundle", "report.json")
        fds = check_outputs(work_dfd, names, ("bundle",) if nothing_new else (), owner_uid=owner_uid, caps=caps)

    def close_outputs() -> None:
        while fds:
            os.close(fds.popitem()[1])

    def close_then_detach() -> None:
        close_outputs()             # D2: the copies are made and fsynced; transact reads the sources no more
        if before_commit is not None:
            before_commit()

    try:
        pub = transact(class_dfd, session, [(n, fds[n]) for n in names], role, seams, gid=gid,
                       bundle_check=None if verifier else importer_bundle_check(session, object_format),
                       before_commit=close_then_detach)
    finally:
        close_outputs()
    if verifier:
        return str(outcome.line.result), pub
    if nothing_new:
        return "adopt-nothing-new", pub
    assert pub.header is not None
    return ("delta" if pub.header.prerequisites else "unrelated"), pub


# =================================================================================================================
# Piece 5c-ii-b: the per-run image, the Linux run leaf, the macOS footprint watchdog, the domain proof, SealedRun
# (DESIGN §1 steps 6, 7, 10, 11; §4.1; §4.9 "The per-run sized image" and "Memory"; §9's 5c-ii build notes).
# =================================================================================================================

UNAVAILABLE = "export-image-unavailable"
UNPROVEN = "export-domain-unproven"
_IMAGE_STEM = {"importer": "work", "verifier": "verify"}
#: The per-run image's commands, by absolute path (root code runs no PATH lookup). ``/sbin`` and ``/bin`` resolve on
#: a merged-/usr Linux and an unmerged one alike [judged by adopt-b, 10-22, against Debian and Ubuntu layouts].
HDIUTIL = "/usr/bin/hdiutil"
MKFS_EXT4 = "/sbin/mkfs.ext4"
MOUNT = "/bin/mount"
UMOUNT = "/bin/umount"
#: V31's RAM bound (HEAD_r7 Q-r5-4, judged): a Linux image at or under it is a tmpfs, above it always the loop image.
TMPFS_MAX_BYTES = 256 << 20
#: One image command's wall [judged by adopt-b, 10-22: R4 measured create + attach + detach of 8 GiB in 1.47 s].
IMAGE_CMD_TIMEOUT_S = 120.0
NEVER_INDEX = ".metadata_never_index"
_MIB = 1 << 20


def _role_key(role_key: str) -> str:
    if role_key not in _IMAGE_STEM:
        raise ValueError(f"role {role_key!r}")
    return role_key


def image_path(export_dir: str, role_key: str, host: str) -> str:
    """The ONE path function (§4.1, r6 C complement F2) for the image's create, the stale scan and the unlink: macOS
    ``work.sparseimage`` / ``verify.sparseimage`` (``hdiutil create -type SPARSE`` appends ``.sparseimage`` to any
    other name), Linux ``work.img`` / ``verify.img``."""
    stem = _IMAGE_STEM[_role_key(role_key)]
    return os.path.join(export_dir, stem + (".sparseimage" if host == "darwin" else ".img"))


def mount_path(export_dir: str, role_key: str) -> str:
    """The image's mount point: the role's ONE writable path, the same constant the wall's profile and view are built
    from (``wall.EXPORT_WRITABLE`` / ``wall.VERIFY_WRITABLE``), so the seal and the wall cannot disagree."""
    from levain.slot import wall
    names = {"importer": wall.EXPORT_WRITABLE, "verifier": wall.VERIFY_WRITABLE}[_role_key(role_key)]
    assert len(names) == 1
    return os.path.join(export_dir, names[0])


def _run_cmd(argv: Sequence[str], umask: Optional[int]) -> Tuple[int, str]:
    """One image command: no stdin, a fixed environment, a bounded wall; ``umask`` set for THIS child only (Popen's
    ``umask``, 3.9+), never process-wide (r7 C' umask). Answers ``(rc, output tail)``; 124 = timeout, 127 = spawn."""
    import subprocess
    try:
        p = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=IMAGE_CMD_TIMEOUT_S, close_fds=True, umask=-1 if umask is None else umask,
                           env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, f"spawn: {e}"
    return p.returncode, p.stdout.decode("utf-8", "replace")[-2000:]


@dataclass(frozen=True)
class ImageSeams:
    """``run(argv, umask)`` -> ``(rc, output)`` (:func:`_run_cmd`); ``is_mounted(path)``; ``log(event)``."""

    run: Callable[[Sequence[str], Optional[int]], Tuple[int, str]] = _run_cmd
    is_mounted: Callable[[str], bool] = os.path.ismount
    log: Callable[[str], None] = _no_log


@dataclass(frozen=True)
class Image:
    host: str
    path: Optional[str]     # the backing file; None = a Linux tmpfs (nothing to unlink)
    mount: str
    size: int               # the capacity it was created at, bytes


class _ImageFail(Exception):
    pass


def _check(rc: int, out: str, what: str) -> None:
    if rc != 0:
        raise _ImageFail(f"{what} rc {rc}: {out.strip()[-300:]}")


def detach_image(img: Image, seams: ImageSeams) -> bool:
    """Force-detach (macOS ``hdiutil detach -force``; Linux ``umount``, no ``-l``: an EBUSY is the detach-failure
    path, r6 C codex MED teardown). True when nothing is mounted at the mount point afterwards."""
    if not seams.is_mounted(img.mount):
        return True
    argv = ([HDIUTIL, "detach", "-quiet", "-force", img.mount] if img.host == "darwin" else [UMOUNT, img.mount])
    rc, out = seams.run(argv, None)
    if rc != 0 or seams.is_mounted(img.mount):
        seams.log(f"detach failed rc {rc}: {out.strip()[-200:]}")
        return False
    seams.log("image detached")
    return True


def _unlink_backing(img: Image, seams: ImageSeams) -> bool:
    if img.path is None:
        return True
    try:
        os.unlink(img.path)
    except FileNotFoundError:
        return True
    except OSError as e:
        seams.log(f"image unlink failed {_errname(e)}")
        return False
    seams.log("image unlinked")
    return True


def clear_stale_image(export_dir: str, role_key: str, host: str, seams: ImageSeams) -> None:
    """Seal-in (§1 step 6, r6 C complement F2): anything attached at the mount point is force-detached and an image
    file at the path function's name is unlinked; either failing = ``export-image-unavailable``."""
    img = Image(host, image_path(export_dir, role_key, host), mount_path(export_dir, role_key), 0)
    if not detach_image(img, seams):
        raise Refused(UNAVAILABLE, f"a stale image at {img.mount} could not be detached")
    if os.path.lexists(img.path or ""):
        if not _unlink_backing(img, seams):
            raise Refused(UNAVAILABLE, f"a stale image file at {img.path} could not be removed")
        seams.log("stale image removed")


def create_image(export_dir: str, role_key: str, host: str, *, size: int, inodes: int, owner_uid: int,
                 seams: ImageSeams) -> Image:
    """Seal-in (§4.9 S1): create the per-run image at V31's ``size`` (bytes; Linux ``inodes`` = ``-N``) and attach
    it at :func:`mount_path`; then the image root is chowned to ``owner_uid`` 0700 and (macOS) root writes
    ``.metadata_never_index`` at it. macOS: ``hdiutil create -fs APFS -type SPARSE`` under ``umask 077`` for that
    child only, the created file's NAME and MODE (0600) asserted, then ``attach -nobrowse -owners on -mountpoint``.
    Linux: a tmpfs (``size=``, ``nr_inodes=``) at or under :data:`TMPFS_MAX_BYTES`, else a sparse ``O_EXCL`` 0600
    file, ``mkfs.ext4 -m 0 -O ^has_journal -I 256 -N``, ``mount -o loop,nodev,nosuid,noexec``. Any failure =
    ``export-image-unavailable``, and what was made is detached and unlinked first."""
    path = image_path(export_dir, role_key, host)
    mnt = mount_path(export_dir, role_key)
    img = Image(host, None, mnt, size)
    try:
        try:
            os.mkdir(mnt, 0o700)
        except FileExistsError:
            if not stat.S_ISDIR(os.lstat(mnt).st_mode):
                raise _ImageFail(f"{mnt} is not a directory") from None
        if host == "darwin":
            mib = -(-size // _MIB)
            img = Image(host, path, mnt, mib * _MIB)
            stem = _IMAGE_STEM[role_key]
            _check(*seams.run([HDIUTIL, "create", "-quiet", "-size", f"{mib}m", "-fs", "APFS", "-type", "SPARSE",
                               "-volname", f"levain-{stem}", path], 0o077), "hdiutil create")
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.geteuid():
                raise _ImageFail(f"the created image {path} is mode {stat.S_IMODE(st.st_mode):o} uid {st.st_uid}, "
                                 f"not a 0600 file of uid {os.geteuid()}")
            seams.log(f"image created {os.path.basename(path)} 0600")
            _check(*seams.run([HDIUTIL, "attach", "-quiet", "-nobrowse", "-noautoopen", "-owners", "on",
                               "-mountpoint", mnt, path], None), "hdiutil attach")
        elif size <= TMPFS_MAX_BYTES:
            _check(*seams.run([MOUNT, "-t", "tmpfs", "-o",
                               f"size={size},nr_inodes={inodes},mode=0700,nodev,nosuid,noexec", "tmpfs", mnt], None),
                   "mount tmpfs")
        else:
            img = Image(host, path, mnt, size)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.ftruncate(fd, size)
            finally:
                os.close(fd)
            seams.log(f"image created {os.path.basename(path)} 0600")
            _check(*seams.run([MKFS_EXT4, "-q", "-F", "-m", "0", "-O", "^has_journal", "-I", "256", "-N", str(inodes),
                               path], None), "mkfs.ext4")
            _check(*seams.run([MOUNT, "-o", "loop,nodev,nosuid,noexec", path, mnt], None), "mount loop")
        if not seams.is_mounted(mnt):
            raise _ImageFail(f"nothing is mounted at {mnt} after the attach")
        seams.log("image attached")
        root = os.open(mnt, _DIR_OPEN)
        try:
            os.fchown(root, owner_uid, -1)
            os.fchmod(root, 0o700)
            if host == "darwin":
                os.close(os.open(NEVER_INDEX, _NEW_FILE, 0o644, dir_fd=root))
        finally:
            os.close(root)
        return img
    except (OSError, _ImageFail) as e:
        detached = detach_image(img, seams)
        if detached and os.path.lexists(path) and img.path is not None:
            _unlink_backing(img, seams)
        what = _errname(e) if isinstance(e, OSError) else str(e)
        raise Refused(UNAVAILABLE, f"the per-run image: {what}") from e


def host_short(export_dir: str, img: Optional[Image], *, statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
               ) -> bool:
    """Root's host-side reading (§4.9, r7 C' host predicate), read after the run and before the unlink: host bytes
    ``f_bavail x f_frsize`` of the volume holding ``L/export`` below the image's unallocated remainder (its size
    minus the backing file's ``st_blocks x 512``). A tmpfs has no backing file on the host volume: never short."""
    if img is None or img.path is None:
        return False
    try:
        allocated = os.stat(img.path).st_blocks * 512
    except FileNotFoundError:
        return False
    st = statvfs(export_dir)
    return st.f_bavail * st.f_frsize < max(img.size - allocated, 0)


# --- the Linux run leaf (§4.9 Memory, r7 B' Q-r7-3 .. r10 S13-7) --------------------------------------------------

#: The stale leaf's ``rmdir`` budget, its poll, and the attempts it makes however late it wakes (r9 S12-7, r10 S13-7).
LEAF_RMDIR_BUDGET_S = 2.0
LEAF_RMDIR_POLL_S = 0.25
LEAF_RMDIR_MIN_ATTEMPTS = 3
RUN_LEAF = "run"


def _cg_read(path: str) -> str:
    with open(path, encoding="ascii") as f:
        return f.read()


def _cg_write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        write_all(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _cg_mkdir(path: str) -> None:
    os.mkdir(path, 0o755)


@dataclass(frozen=True)
class LeafSeams:
    """The cgroupfs acts, as paths: ``read`` / ``write`` (``FileNotFoundError`` when absent), ``mkdir``, ``rmdir``,
    ``exists``; ``clock`` / ``sleep`` for the bounded ``rmdir``; ``log``."""

    read: Callable[[str], str] = _cg_read
    write: Callable[[str, str], None] = _cg_write
    mkdir: Callable[[str], None] = _cg_mkdir
    rmdir: Callable[[str], None] = os.rmdir
    exists: Callable[[str], bool] = os.path.exists
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = _no_log


def scope_path(role_key: str, cgroup_root: Optional[str] = None) -> str:
    """``levain.slice/export.scope`` (``verify.scope``): the scope the domain kill and proof run on, hierarchically."""
    from levain.slot import wall
    return f"{cgroup_root or wall.CGROUP_ROOT}/{wall._EXPORT_SCOPE[_role_key(role_key)]}"


def leaf_path(role_key: str, cgroup_root: Optional[str] = None) -> str:
    """This run's leaf, ONE fixed path per scope (r10 S13-7): the cgroup the launch joins
    (``wall.export_linux_launch_argv``)."""
    return f"{scope_path(role_key, cgroup_root)}/{RUN_LEAF}"


def remove_stale_leaf(leaf: str, seams: LeafSeams) -> int:
    """Seal-in, after the domain proof: remove a leaf a crashed helper left. The ``rmdir`` is retried under its OWN
    budget, :data:`LEAF_RMDIR_BUDGET_S` polled every :data:`LEAF_RMDIR_POLL_S` (an EBUSY right after ``cgroup.kill``
    is transient while the killed tasks are reaped), with at least :data:`LEAF_RMDIR_MIN_ATTEMPTS` attempts and one
    more after the deadline, however late a loaded helper wakes. Still there = ``export-image-unavailable``, naming
    the leaf ("stale run leaf at <path>"). Returns the attempts made (0 = no stale leaf)."""
    if not seams.exists(leaf):
        return 0
    start = seams.clock()
    deadline = start + LEAF_RMDIR_BUDGET_S
    attempts = 0
    while True:
        late = seams.clock() >= deadline
        attempts += 1
        try:
            seams.rmdir(leaf)
            seams.log(f"stale leaf removed after {attempts} attempt(s)")
            return attempts
        except FileNotFoundError:
            return attempts
        except OSError as e:
            last = _errname(e)
            seams.log(f"stale leaf rmdir attempt {attempts} {last}")
        if late and attempts >= LEAF_RMDIR_MIN_ATTEMPTS:
            raise Refused(UNAVAILABLE, f"stale run leaf at {leaf}: {last} after {attempts} attempts in "
                                       f"{seams.clock() - start:.2f} s")
        seams.sleep(LEAF_RMDIR_POLL_S)


def enable_controllers(role_key: str, seams: LeafSeams, cgroup_root: Optional[str] = None) -> None:
    """Seal-in (r9 S12-7): ``+memory +pids`` written idempotently into ``cgroup.subtree_control`` along the
    ``levain.slice`` -> scope chain (systemd may drop a slice's controllers when no unit needs them), each directory
    made when absent; any failure = ``export-image-unavailable``."""
    from levain.slot import wall
    for d in (cgroup_root or wall.CGROUP_ROOT, scope_path(role_key, cgroup_root)):
        try:
            if not seams.exists(d):
                seams.mkdir(d)
            seams.write(f"{d}/cgroup.subtree_control", "+memory +pids")
        except OSError as e:
            raise Refused(UNAVAILABLE, f"{d}/cgroup.subtree_control: {_errname(e)}") from e
        seams.log(f"subtree_control {d}")


def make_leaf(leaf: str, *, memory_max: int, pids_max: int, seams: LeafSeams) -> None:
    """Seal-in (§4.9 Memory): create this run's leaf and write, in this order, ``memory.max`` FIRST (its failure fatal
    whatever the swap test then finds, r9 S12-7), ``memory.swap.max = 0`` (SKIPPED when the write finds no such file,
    a kernel without swap accounting, r8; any other failure fatal), then ``pids.max``; all before the child joins. A
    failure = ``export-image-unavailable``."""
    def write(name: str, value: str) -> None:
        try:
            seams.write(f"{leaf}/{name}", value)
        except OSError as e:
            raise Refused(UNAVAILABLE, f"{leaf}/{name}: {_errname(e)}") from e
        seams.log(f"{name}={value}")

    try:
        seams.mkdir(leaf)
    except OSError as e:
        raise Refused(UNAVAILABLE, f"{leaf}: {_errname(e)}") from e
    write("memory.max", str(memory_max))
    try:        # written directly (L3 5c-ii-b r1 B-3): an existence test would read EACCES as "absent"
        seams.write(f"{leaf}/memory.swap.max", "0")
        seams.log("memory.swap.max=0")
    except FileNotFoundError:
        seams.log("memory.swap.max absent, skipped")
    except OSError as e:
        raise Refused(UNAVAILABLE, f"{leaf}/memory.swap.max: {_errname(e)}") from e
    write("pids.max", str(pids_max))


def read_oom_kill(leaf: str, seams: LeafSeams) -> Optional[int]:
    """The leaf's ``memory.events.local`` ``oom_kill`` count, read after ``populated 0``; None when it cannot be read
    (the file gone, or no ``oom_kill`` line): the caller's ``memory_unreadable``, never ``oom_kill 0`` (r10 S13-7)."""
    try:
        text = seams.read(f"{leaf}/memory.events.local")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "oom_kill" and parts[1].isdigit():
            return int(parts[1])
    return None


# --- the macOS footprint watchdog (§4.9 Memory, r6 B complement F1; r7 B' complement F10) -----------------------

#: V31's watchdog interval (r4's monitor value, judged by s5-fold-r5b; 5g measures overshoot at 250 ms and 50 ms).
WATCHDOG_INTERVAL_S = 0.25
RUSAGE_INFO_V4 = 4
#: ``struct rusage_info_v4`` (<sys/resource.h>): ``ri_uuid[16]``, then uint64 ``ri_user_time``, ``ri_system_time``,
#: ``ri_pkg_idle_wkups``, ``ri_interrupt_wkups``, ``ri_pageins``, ``ri_wired_size``, ``ri_resident_size``,
#: ``ri_phys_footprint``: the footprint is the eighth uint64 after the uuid.
_FOOTPRINT_OFFSET = 16 + 7 * 8
_RUSAGE_BUF = 512                    # >= sizeof(struct rusage_info_v4)
_LIBPROC: list = []


def phys_footprint(pid: int) -> Optional[int]:
    """``proc_pid_rusage(RUSAGE_INFO_V4)`` ``ri_phys_footprint`` (the ledger jetsam enforces; never ``ps`` text), or
    None when the pid is gone (ESRCH). Any other failure raises ``OSError`` with its errno (L3 5c-ii-b r1 B-4): a pid
    root cannot read is a blind sample, never a skipped one."""
    if not _LIBPROC:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pid_rusage.restype = ctypes.c_int
        _LIBPROC.append(lib)
    buf = ctypes.create_string_buffer(_RUSAGE_BUF)
    if _LIBPROC[0].proc_pid_rusage(pid, RUSAGE_INFO_V4, buf) != 0:
        e = ctypes.get_errno()
        if e == errno.ESRCH:
            return None
        raise OSError(e, f"proc_pid_rusage({pid}): {os.strerror(e)}")
    return int.from_bytes(buf.raw[_FOOTPRINT_OFFSET:_FOOTPRINT_OFFSET + 8], sys.byteorder)


class WatchdogBlind(Exception):
    """A sampling pass cannot see the whole domain (a process whose owner could not be read)."""


def uid_pids(uid: int) -> Sequence[int]:
    """Every pid of ``uid`` (real, effective or saved), and only those (B-4: a foreign process is never a kill
    candidate). A process whose owner could not be read (``UNKNOWN``) may be the uid's, so it raises
    :class:`WatchdogBlind`: blindness, never a candidate and never skipped. Known edge: a pid reused by another
    process between this listing and the footprint read can over-count one pass. That fails closed (a false cap at
    worst, never a missed one) [judged by levain head 1022+18, 10-22, against codex L3 r2 B-4]."""
    from levain.slot import proc as P
    out = []
    for p in P.default_seams("darwin").listing():
        if p.state == P.UNKNOWN:
            raise WatchdogBlind(f"pid {p.pid}: owner unreadable")
        if p.of(uid):
            out.append(p.pid)
    return out


#: The join bound on :meth:`FootprintWatchdog.stop` (B-2): its ``end()`` may wait for seal-out's proof to release
#: the one lock and then run its own, two ``end_domain`` bounds in a row, plus a margin [judged by adopt-c, 10-22,
#: against that worst case]. A join that times out marks the watchdog blind.
WATCHDOG_JOIN_MARGIN_S = 5.0
WATCHDOG_JOIN_S = 2 * V12K_SECONDS + WATCHDOG_JOIN_MARGIN_S


class FootprintWatchdog:
    """Samples ``footprint`` of every pid ``pids(uid)`` names every ``interval`` from its OWN thread (never from
    root's walker loop, which blocks) and calls ``end()`` (the domain kill) once a pass's SUM passes ``cap`` (DESIGN
    V30: "``ri_phys_footprint``, summed over the verify domain"): "a sampled termination threshold with measured
    overshoot", never "enforced" (§8). After :meth:`stop`, ``killed`` is the kill flag seal-out reads; ``blind`` is
    True when a sampling pass raised (an unreadable footprint or owner included) or the thread did not stop, so the
    memory observation cannot be established (the macOS twin of an unreadable ``memory.events.local``; head ruling
    B1, 1022-18: never SUCCESS)."""

    def __init__(self, uid: int, cap: int, end: Callable[[], object], *, interval: float = WATCHDOG_INTERVAL_S,
                 pids: Callable[[int], Sequence[int]] = uid_pids,
                 footprint: Callable[[int], Optional[int]] = phys_footprint,
                 log: Callable[[str], None] = _no_log) -> None:
        import threading
        self.uid, self.cap, self._end, self.interval = uid, cap, end, interval
        self._pids, self._footprint, self._log = pids, footprint, log
        self.killed = False
        self.blind = False
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="levain-footprint-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                total, seen = 0, 0
                for pid in self._pids(self.uid):
                    fp = self._footprint(pid)
                    if fp is None:          # ESRCH: gone since the listing
                        continue
                    total += fp
                    seen += 1
                self.peak = max(self.peak, total)
                if total > self.cap:
                    self.killed = True
                    self._log(f"watchdog: {seen} pid(s) footprint {total} > cap {self.cap}")
                    self._end()
                    return
            except Exception as e:      # noqa: BLE001 - a blind watchdog must say so, never read as "no kill"
                self.blind = True
                self._log(f"watchdog blind: {type(e).__name__}: {e}"[:300])
                return
            self._stop.wait(self.interval)

    def stop(self, timeout: float = WATCHDOG_JOIN_S) -> None:
        self._stop.set()
        if self._thread.ident is not None:      # started (a finished thread's join returns at once)
            self._thread.join(timeout)
        if self._thread.is_alive():
            self.blind = True
        self._log("watchdog stopped")


# --- the domain proof (§1 steps 6 and 11; §4.9 Mechanism) --------------------------------------------------------

def export_domain_prover(host: str, role_key: str, uid: int, *, bound_s: Optional[float] = None,
                         seams: object = None) -> Callable[[Sequence[int]], object]:
    """``prove(children) -> proc.Domain``: kill and prove the export domain empty. macOS
    ``end_domain(uid, V12k)`` (freeze, kill, bootout, kill, settle, proof); Linux the scope ``cgroup.freeze`` +
    ``cgroup.kill`` + ``populated 0`` (hierarchical, so the run leaf too), then the scope THAWED (``cgroup.freeze``
    0, confirmed by ``frozen 0``; L3 5c-ii-b r1 B-1: a scope left frozen freezes the next run's child as it joins
    the leaf), then the uid sweep by ``end_domain``. A thaw that cannot be confirmed raises
    ``export-image-unavailable``: the next run could not execute. So does a ``cgroup.events`` read that fails with any
    errno but ENOENT at the freeze, kill or thaw poll (B-1 class); ENOENT is the scope gone, as everywhere."""
    from levain.slot import proc as P
    bound = P.V12K_SECONDS if bound_s is None else bound_s

    if host == "darwin":
        def prove_darwin(children: Sequence[int]) -> object:
            return P.end_domain(uid, bound, children, seams=seams)[0]
        return prove_darwin

    def prove_linux(children: Sequence[int]) -> object:
        c = P.CgroupProcs(scope_path(role_key), seams)
        step = "freeze poll"
        try:
            c.freeze(uid, bound * P.FREEZE_SHARE)
            step = "kill poll"
            if not c.kill(uid, bound):
                return P.Domain(False, "kill-unproven", f"{c.cgroup}: populated is not 0")   # left frozen: refused
            step = "thaw poll"
            _thaw_scope(c, bound * P.FREEZE_SHARE)
        except OSError as e:
            # B-1 class (head ruling 1022+19): ONE guard for every read of the scope's state this prover makes. The
            # cgroup writes in this block never raise OSError here (CgroupProcs._act and _thaw_scope answer each
            # write's OSError themselves), so what reaches this line is a CgroupProcs.events() read, which has
            # already turned ENOENT into None ("the scope is gone"). Any other errno means the scope's state
            # cannot be read, so the domain cannot be proven and the next run cannot execute.
            # (Derive the reads: grep -n "events()" levain/slot/proc.py; each sits under _poll or no_process.)
            where = e.filename or f"{c.cgroup}/cgroup.events"
            raise Refused(UNAVAILABLE, f"{where}: the {step} could not read the scope ({_errname(e)})") from e
        return P.end_domain(uid, bound, children, seams=c.seams)[0]
    return prove_linux


def _thaw_scope(c: object, bound_s: float) -> None:
    """``cgroup.freeze`` 0 on a scope ``populated 0`` already reads empty (so the thaw releases nothing), then
    ``frozen 0`` polled within ``bound_s``. A scope that does not exist held nothing to thaw."""
    path = f"{c.cgroup}/cgroup.freeze"                 # type: ignore[attr-defined]
    try:
        c.seams.write(path, "0")                       # type: ignore[attr-defined]
    except FileNotFoundError:
        return
    except OSError as e:
        raise Refused(UNAVAILABLE, f"{path}: the thaw failed ({_errname(e)})") from e
    # None = the scope is gone. An unreadable cgroup.events here raises OSError to prove_linux's one guard (B-1 class).
    got = c._poll("frozen", "0", bound_s)              # type: ignore[attr-defined]
    if got is False:
        raise Refused(UNAVAILABLE, f"{path}: written 0, cgroup.events still reads frozen 1")


# --- the sealed run ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SealFacts:
    """One sealed run's facts. ``role_key`` ``importer`` | ``verifier``; ``uid``: the export principal
    (``_levain_export`` / ``_levain_verify``); ``image_bytes`` / ``image_inodes``: V31's size; ``memory_cap``: the
    Linux leaf's ``memory.max`` and the macOS watchdog's cap (V30); ``pids_max``: V31."""

    host: str
    role_key: str
    export_dir: str
    uid: int
    image_bytes: int
    image_inodes: int
    memory_cap: int
    pids_max: int

    @property
    def role(self) -> Role:
        return IMPORTER if self.role_key == "importer" else VERIFIER


@dataclass(frozen=True)
class SealSeams:
    """``prove(children) -> Domain`` (:func:`export_domain_prover`); the image, leaf and publish seams; ``watchdog``:
    the factory ``(uid, cap, end, log) ->`` an object with ``start`` / ``stop`` / ``killed`` / ``blind``;
    ``statvfs``; ``log``: the order log the DONE rows read."""

    prove: Callable[[Sequence[int]], object]
    image: ImageSeams = ImageSeams()
    leaf: LeafSeams = LeafSeams()
    publish: PublishSeams = PublishSeams()
    watchdog: Callable[..., object] = None  # type: ignore[assignment]  (FootprintWatchdog when None)
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs
    log: Callable[[str], None] = _no_log


@dataclass(frozen=True)
class SealReading:
    """What seal-out read, for :class:`RunObservation`: ``memory_killed``, ``memory_unreadable``, ``host_short``."""

    memory_killed: bool
    memory_unreadable: bool
    host_short: bool


class SealedRun:
    """One sealed run's OS side (§1 steps 6, 7, 10, 11). :meth:`seal_in` in step 6's order: the domain proven empty
    (``export-domain-unproven``), a stale image detached and unlinked, (Linux) a stale leaf removed under its bound,
    the controller chain, this run's leaf with its limits, the per-run image created and attached, a stale staged
    name deleted (``<role>-failed:publish`` / ``stale-staging``), the early probe, always (D1); a refusal after
    anything was made tears it down first. :meth:`start_watchdog` (macOS, after the spawn). :meth:`seal_out`: the
    domain proven empty again while the watchdog still samples, the watchdog's kill and this proof under one lock,
    then the watchdog stopped and joined (an unproven domain = ``export-domain-unproven``, the image LEFT attached
    for the next seal-in), then the memory observation (Linux the leaf's ``oom_kill`` after ``populated 0``; macOS
    the watchdog's flags), the leaf removed (a failure leaves it for the next seal-in), the host reading.
    :meth:`teardown`: the order domain empty -> fds closed -> umount -> backing unlinked (r6 build note).
    :meth:`release_image` is idempotent and never raises, so it serves inside :func:`transact`'s ``before_commit``
    (the outputs already closed, D2) AND as teardown's last step: a detach that fails there (any fd root still holds
    into the image, for one) is retried by teardown once every fd in :attr:`fds` is closed. A publish path must not
    commit on that failure: it reads :attr:`released` after the call and refuses (adopt-open: L3 r1 H1)."""

    def __init__(self, facts: SealFacts, seams: SealSeams) -> None:
        self.f, self.s = facts, seams
        self.image: Optional[Image] = None
        self.leaf: Optional[str] = None
        self.watchdog: Optional[object] = None
        self.fds: list = []
        self.released = False
        self.proven_out = False
        import threading
        #: B-2: the watchdog's kill and seal-out's proof take this ONE lock: never two end_domain calls on one uid.
        self._prove_lock = threading.Lock()

    def _prove(self, children: Sequence[int]) -> object:
        with self._prove_lock:
            return self.s.prove(tuple(children))

    # seal-in -------------------------------------------------------------------------------------------------------
    def seal_in(self, class_dfd: int, staged: str) -> None:
        f, s = self.f, self.s
        d = self._prove(())
        if not getattr(d, "proven", False):
            raise Refused(UNPROVEN, f"seal-in: {getattr(d, 'name', d)} {getattr(d, 'detail', '')}".strip())
        s.log("seal-in domain proven empty")
        clear_stale_image(f.export_dir, f.role_key, f.host, s.image)
        try:
            if f.host == "linux":
                leaf = leaf_path(f.role_key)
                remove_stale_leaf(leaf, s.leaf)
                enable_controllers(f.role_key, s.leaf)
                self.leaf = leaf    # B-6: owned BEFORE it is configured, so teardown removes a partly made leaf
                make_leaf(leaf, memory_max=f.memory_cap, pids_max=f.pids_max, seams=s.leaf)
                s.log("leaf made")
            self.image = create_image(f.export_dir, f.role_key, f.host, size=f.image_bytes, inodes=f.image_inodes,
                                      owner_uid=f.uid, seams=s.image)
            clear_staging(class_dfd, staged, f.role, s.publish)
            probe_exchange(class_dfd, f.role, s.publish)     # D1 (HEAD_r16 Q-r13-2): a first publish too
        except BaseException:
            self.proven_out = True          # nothing ran in the domain: seal-in's own proof stands
            self.teardown()
            raise
        s.log("seal-in done")

    def start_watchdog(self) -> None:
        if self.f.host != "darwin":
            return
        factory = self.s.watchdog or FootprintWatchdog
        self.watchdog = factory(self.f.uid, self.f.memory_cap, lambda: self._prove(()), log=self.s.log)
        self.watchdog.start()           # type: ignore[union-attr]
        self.s.log("watchdog started")

    # seal-out ------------------------------------------------------------------------------------------------------
    def seal_out(self, children: Sequence[int] = ()) -> SealReading:
        f, s = self.f, self.s
        # B-2: the watchdog keeps sampling through the domain kill (a process can cross the cap during it); only
        # once the proof is in is it stopped and joined, and its flags read below.
        try:
            d = self._prove(children)
        finally:
            if self.watchdog is not None:
                self.watchdog.stop()    # type: ignore[union-attr]
        if not getattr(d, "proven", False):
            raise Refused(UNPROVEN, f"seal-out: {getattr(d, 'name', d)} {getattr(d, 'detail', '')}".strip())
        self.proven_out = True
        s.log("seal-out domain proven empty")
        if f.host == "linux":
            oom = read_oom_kill(self.leaf, s.leaf) if self.leaf else None
            killed, unreadable = bool(oom), oom is None
            s.log(f"events read oom_kill={oom}")
            if self.leaf is not None:
                try:
                    s.leaf.rmdir(self.leaf)
                    s.log("leaf removed")
                except OSError as e:
                    s.log(f"leaf left {_errname(e)}")
        else:
            w = self.watchdog
            killed = bool(getattr(w, "killed", False))
            unreadable = w is None or bool(getattr(w, "blind", False))
            s.log(f"watchdog flag killed={killed} blind={unreadable}")
        short = host_short(f.export_dir, self.image, statvfs=s.statvfs)
        s.log(f"host reading short={short}")
        return SealReading(killed, unreadable, short)

    # teardown ------------------------------------------------------------------------------------------------------
    def release_image(self) -> None:
        if self.released or self.image is None:
            return
        if not self.proven_out:
            self.s.log("image left attached: the domain is not proven empty")
            return
        if detach_image(self.image, self.s.image) and _unlink_backing(self.image, self.s.image):
            self.released = True
            return
        self.s.log("image left for the next seal-in")

    def teardown(self) -> None:
        """Domain empty (seal-out's proof, or seal-in's when nothing ran) -> the fds root holds into the image closed
        -> umount -> backing unlinked; then a leaf seal-out did not remove (a seal-in refusal after the leaf was
        made). Without a proof the image and the leaf are LEFT for the next seal-in."""
        while self.fds:
            fd = self.fds.pop()
            try:
                os.close(fd)
            except OSError:
                pass
        self.s.log("fds closed")
        self.release_image()
        if self.leaf is not None and self.proven_out and self.s.leaf.exists(self.leaf):
            try:
                self.s.leaf.rmdir(self.leaf)
                self.s.log("leaf removed")
            except OSError as e:
                self.s.log(f"leaf left {_errname(e)}")
