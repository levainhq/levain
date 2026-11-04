"""levain.consent.terminal_sign: the signer core of TERMSIGN, a band answer typed at the terminal and signed with
the laptop's Touch ID key (design: project_memory/1102-5/TERMSIGN/DESIGN.md). A click in the cockpit at the laptop
is the same key in a sibling format and namespace (``levain-desk-answer/1``, :data:`DESK_NAMESPACE`; design
project_memory/1103-19/L5_DESIGN.md): the two are told apart by the format line and the namespace, and one
:func:`_signed` binds either to its row.

The signature is the only evidence; every other field is a label. One typed line is one message
(``levain-terminal-answer/1``, SSHSIG namespace :data:`NAMESPACE`) that names, per entry, the row it answers and
the row's version, so a signature cannot move between rows, stores, installs or protocols. The message is built by
code from the parsed plan and the rows as read at sign time; no agent supplies its bytes.

What this module does and does not claim (DESIGN section 4): a verified signature shows that the enrolled laptop
key signed these bytes, and, where this Mac can be asked, that the key's Secure Enclave identity is
biometric-protected. It does not show that the operator read the bytes, that this particular signature had its own
fresh touch, or whose finger it was.

Reaching the real Secure Enclave key happens in exactly one place: the ``signer_factory`` that :func:`sign` calls
(default :func:`default_signer_factory`, a thin wrap of ``levain.cockpit.keys.ssh_signer``). Everything else
here is pure or read-only. The key is found by the fixed label :data:`KEY_LABEL` under the real levain home, never
by a path an environment variable names.
"""
from __future__ import annotations

import hashlib
import os
import pwd
import re
import secrets
import shlex
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.authority import _pipe_reading
from levain.authority_sig import (VerifierUnavailable, _sshsig_type_and_flags, presence_missing,
                                  sshsig_public_key)
from levain.cockpit import keys as _keys
from levain.cockpit.custody import SC_AUTH, SIDECAR_SUFFIX, TOUCH_ID, ctk_status, read_sidecar
from levain.cockpit.keys import KeyError_, Signer, signing_public_key
from levain.trust_anchor import _VERIFY_ENV, DEFAULT_SSH_KEYGEN, trust_anchor_problem

__all__ = ["AnswerClass", "DESK_FORMAT", "DESK_NAMESPACE", "DESK_SCOPE_FIELDS", "DESK_VIAS", "Entry", "FAILED_REASONS",
           "KEY_LABEL", "MessageError", "NAMESPACE", "FORMAT", "ParsedDesk", "ParsedMessage", "REASONS",
           "SCOPE_FIELDS", "SignOutcome", "StatusReport", "TERMINAL_VIAS", "TerminalKey", "VerifyResult", "answer_class",
           "build_desk_message", "build_message", "create", "default_desk_signer_factory", "default_signer_factory",
           "desk_origin", "desk_surface",
           "enroll_command", "event_time", "find_key", "fresh", "is_remote", "parse_desk_verified", "parse_verified",
           "session_refusal", "sign", "status", "target_id", "verify"]

FORMAT = "levain-terminal-answer/1"
NAMESPACE = "levain-terminal-answer"
DESK_FORMAT = "levain-desk-answer/1"
DESK_NAMESPACE = "levain-desk-answer"
DESK_VIAS = ("cockpit", "cockpit-desk")    # Levain's answer_context.via 'cockpit'; flow's resolve-event label
TERMINAL_VIAS = ("terminal", "seat")       # the seat hook records 'seat'; a resolve event labels it 'terminal'
GESTURES = ("click", "typed")
COUNTERSIGNS = ("confirm", "disown")
SIGNERS_NAMESPACES = f"{NAMESPACE},{DESK_NAMESPACE}"
KEY_LABEL = "levain-terminal"
MAX_BYTES = 8 * 1024
LINE_MAX = 2000                    # one typed line
SIGN_BOUND_S = 20.0                # the hook's own bound on the sign step (DESIGN section 6)
STALE_S = 600                      # `at` must lie this long before the resolve event at most
REASONS = ("no-key", "remote", "unavailable", "cancelled", "timeout", "signer-error")
KINDS = ("information", "authority")

_STORE = re.compile(r"flow-needs-phill|levain-questions:[A-Za-z0-9._-]{1,64}")
_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_ITEM = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_VERSION = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_NONCE = re.compile(r"[0-9a-f]{16}")
_SHA = re.compile(r"[0-9a-f]{64}")
_AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FP = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
_PRINCIPAL = re.compile(r"[A-Za-z0-9._-]{1,64}")
_ORIGIN = re.compile(r"http://(localhost|127\.0\.0\.1)(:[0-9]{1,5})?")
_DESK_VIEW = re.compile(r"[A-Za-z0-9._-]{1,128}")


class MessageError(ValueError):
    """A message that cannot be built, or bytes that are not a ``levain-terminal-answer/1`` message."""


def _one_line(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text.splitlines()[0]}" if text else type(exc).__name__


# ---------------------------------------------------------------------------------------------------
# the message


@dataclass(frozen=True)
class Entry:
    """One answered row. Exactly one of ``answer`` (an option's full text) and ``text`` (his free words)."""
    item: str
    row_version: str
    asked: str
    kind: str
    answer: str | None = None
    text: str | None = None
    note: str | None = None
    ruling: str | None = None


# The fields the OPERATOR writes (the line he typed, his free-text answer, his note) and the ruling built for his
# pick. In these alone the two joiners are accepted: U+200C ZERO WIDTH NON-JOINER (required in Persian and several
# Indic scripts) and U+200D ZERO WIDTH JOINER (emoji sequences such as a family or a profession). The agent-controlled
# fields (the question asked and the option label in ``answer``) and the fixed-form fields take no exception.
_OPERATOR_FIELDS = frozenset({"typed", "text", "note", "ruling"})
_OPERATOR_ALLOW = "‌‍"


def _allow(field: str) -> str:
    """The unshowable characters field ``field`` accepts: the two joiners in an operator field, none elsewhere."""
    return _OPERATOR_ALLOW if field in _OPERATOR_FIELDS else ""


def _hidden(value: str, field: str) -> str | None:
    """The first character of ``value`` a person cannot see, named (``U+202E (RIGHT-TO-LEFT OVERRIDE) at character
    3``), or ``None``. The one rule, :mod:`levain.unshowable`, the question store already holds every label and
    question to when it is filed: every control (C0, DEL, C1, newline and tab included), every format character (bidi
    embeddings, overrides and isolates, zero-width characters, U+FEFF), the line and paragraph separators, surrogates,
    invisible spaces, with only :func:`_allow`'s joiners accepted in an operator field. Held here again, at build and
    at parse, so that a row which never passed filing (written before that rule, or straight into the file) is
    refused rather than signed: the verifier binds an entry to its row by equality, so the words signed must be the
    words shown."""
    from levain import unshowable

    return unshowable.describe(value, allow=_allow(field))


def _line(value: Any, what: str, *, limit: int = LINE_MAX) -> str:
    if not isinstance(value, str) or not value:
        raise MessageError(f"{what} is empty or not text")
    if len(value) > limit:
        raise MessageError(f"{what} is longer than {limit} characters")
    hidden = _hidden(value, what)
    if hidden is not None:
        raise MessageError(f"{what} holds a character that does not show as itself: {hidden}")
    return value


def _pattern(value: Any, rx: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or rx.fullmatch(value) is None:
        raise MessageError(f"{what} is not in its form")
    return value


def _sha(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()


def _entry_lines(n: int, e: Entry) -> list[str]:
    if (e.answer is None) == (e.text is None):
        raise MessageError(f"entry {n}: exactly one of answer and text")
    if e.kind not in KINDS:
        raise MessageError(f"entry {n}: kind is one of {', '.join(KINDS)}")
    out = [f"entry: {n}", f"  item: {_pattern(e.item, _ITEM, 'item')}",
           f"  row_version: {_pattern(e.row_version, _VERSION, 'row_version')}",
           f"  asked: {_line(e.asked, 'asked')}", f"  kind: {e.kind}"]
    out.append(f"  answer: {_line(e.answer, 'answer')}" if e.answer is not None
               else f"  text: {_line(e.text, 'text')}")
    if e.note is not None:
        out.append(f"  note: {_line(e.note, 'note')}")
    if e.ruling is not None:
        if e.kind != "authority":
            raise MessageError(f"entry {n}: a ruling belongs to an authority entry")
        out.append(f"  ruling: {_line(e.ruling, 'ruling')}")
    return out


def build_message(*, store: str, install: str, session: str, view: str, typed: str, entries: Sequence[Entry],
                  typed_block: str | None = None, nonce: str | None = None, at: datetime | None = None,
                  countersign_of: str | None = None) -> bytes:
    """The bytes the operator's key signs. ``typed`` is the one line he typed (NFC, at most :data:`LINE_MAX`);
    ``typed_block`` the verbatim prompt block that parsed, when that is more than the line (its NFC sha256 is
    signed). ``nonce`` and ``at`` default to fresh values; tests pass them to pin the bytes; a naive ``at`` is
    refused, never read as local time. ``countersign_of`` (a re-sign of an earlier answer, DESIGN section 7) is
    that answer's ``ts`` as its store writes it. Raises :class:`MessageError` on a field that is out of form, no
    entries, or a message over :data:`MAX_BYTES`."""
    entries = tuple(entries)
    if not entries:
        raise MessageError("a message answers at least one entry")
    typed = _line(unicodedata.normalize("NFC", typed) if isinstance(typed, str) else typed, "typed")
    nonce = secrets.token_hex(8) if nonce is None else nonce
    if at is not None and (not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None):
        raise MessageError("`at` must be an aware datetime")
    when = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(_AT_FORMAT)
    lines = [FORMAT, f"store: {_pattern(store, _STORE, 'store')}", f"install: {_pattern(install, _ID, 'install')}",
             f"session: {_pattern(session, _ID, 'session')}", f"view: {_pattern(view, _ID, 'view')}",
             f"typed-sha256: {_sha(typed if typed_block is None else typed_block)}", f"typed: {typed}",
             f"nonce: {_pattern(nonce, _NONCE, 'nonce')}", f"at: {when}"]
    if countersign_of is not None:
        lines.append(f"countersign-of: {_pattern(countersign_of, _VERSION, 'countersign-of')}")
    for n, e in enumerate(entries, 1):
        lines.extend(_entry_lines(n, e))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise MessageError(f"the message is {len(data)} bytes; the bound is {MAX_BYTES}")
    return data


@dataclass(frozen=True)
class ParsedMessage:
    store: str
    install: str
    session: str
    view: str
    typed_sha256: str
    typed: str
    nonce: str
    at: datetime
    entries: tuple[Entry, ...]
    countersign_of: str | None = None
    message_sha256: str = ""

    @property
    def typed_matches_hash(self) -> bool:
        """True when the signed hash is that of the signed line (it is not when the prompt block was longer)."""
        return _sha(self.typed) == self.typed_sha256


def _field(lines: list[str], i: int, name: str, rx: re.Pattern[str] | None = None, *,
           prefix: str = "") -> tuple[str, int]:
    if i >= len(lines) or not lines[i].startswith(f"{prefix}{name}: "):
        raise MessageError(f"line {i + 1}: {name!r} is missing or out of place")
    value = lines[i][len(prefix) + len(name) + 2:]
    if not value or (rx is not None and rx.fullmatch(value) is None) or _hidden(value, name) is not None:
        raise MessageError(f"line {i + 1}: {name!r} is not in its form")
    return value, i + 1


def parse_verified(signed: bytes) -> ParsedMessage:
    """The fields of bytes that VERIFIED, read strictly: exactly the format :func:`build_message` writes, in its
    order, nothing before, between or after, at most :data:`MAX_BYTES`. A field outside its entry block, a
    repeated or skipped entry number, or any unknown line refuses (:class:`MessageError`)."""
    if not isinstance(signed, (bytes, bytearray)) or len(signed) > MAX_BYTES:
        raise MessageError("not a message of this format, or over the size bound")
    try:
        text = bytes(signed).decode("utf-8")
    except UnicodeDecodeError:
        raise MessageError("the message is not UTF-8") from None
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise MessageError("the message does not end in exactly one newline")
    lines = text[:-1].split("\n")
    if lines[0] != FORMAT:
        raise MessageError("not a levain-terminal-answer/1 message")
    i = 1
    store, i = _field(lines, i, "store", _STORE)
    install, i = _field(lines, i, "install", _ID)
    session, i = _field(lines, i, "session", _ID)
    view, i = _field(lines, i, "view", _ID)
    typed_sha, i = _field(lines, i, "typed-sha256", _SHA)
    typed, i = _field(lines, i, "typed")
    if len(typed) > LINE_MAX or unicodedata.normalize("NFC", typed) != typed:
        raise MessageError("the typed line is over the bound or not NFC")
    nonce, i = _field(lines, i, "nonce", _NONCE)
    at_text, i = _field(lines, i, "at", _AT)
    try:
        at = datetime.strptime(at_text, _AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise MessageError("`at` is not a time") from None
    counter = None
    if i < len(lines) and lines[i].startswith("countersign-of: "):
        counter, i = _field(lines, i, "countersign-of", _VERSION)
    entries = _parse_entries(lines, i, allow_note=True)
    return ParsedMessage(store, install, session, view, typed_sha, typed, nonce, at, entries, counter,
                         hashlib.sha256(bytes(signed)).hexdigest())


def _parse_entries(lines: list[str], i: int, *, allow_note: bool) -> tuple[Entry, ...]:
    """The entry blocks from line ``i`` to the end, strictly (shared by both formats; ``allow_note`` False for the
    desk format, which has no ``note`` line)."""
    entries: list[Entry] = []
    while i < len(lines):
        number, i = _field(lines, i, "entry", re.compile(r"[1-9][0-9]{0,2}"))
        if int(number) != len(entries) + 1:
            raise MessageError("entry numbers are not 1, 2, 3 ...")
        item, i = _field(lines, i, "item", _ITEM, prefix="  ")
        version, i = _field(lines, i, "row_version", _VERSION, prefix="  ")
        asked, i = _field(lines, i, "asked", prefix="  ")
        kind, i = _field(lines, i, "kind", re.compile("|".join(KINDS)), prefix="  ")
        answer = free = note = ruling = None
        if i < len(lines) and lines[i].startswith("  answer: "):
            answer, i = _field(lines, i, "answer", prefix="  ")
        else:
            free, i = _field(lines, i, "text", prefix="  ")
        if allow_note and i < len(lines) and lines[i].startswith("  note: "):
            note, i = _field(lines, i, "note", prefix="  ")
        if i < len(lines) and lines[i].startswith("  ruling: "):
            ruling, i = _field(lines, i, "ruling", prefix="  ")
            if kind != "authority":
                raise MessageError("a ruling on an entry that is not an authority entry")
        for v in (asked, answer, free, note, ruling):
            if v is not None and len(v) > LINE_MAX:
                raise MessageError("a field is over the line bound")
        entries.append(Entry(item, version, asked, kind, answer, free, note, ruling))
    if not entries:
        raise MessageError("the message answers no entry")
    return tuple(entries)


# --- the desk format (TERMSIGN L5a; design 1103-19/L5_DESIGN.md section 1) -------------------------------------


@dataclass(frozen=True)
class ParsedDesk:
    store: str
    install: str
    surface: str
    origin: str
    view: str
    gesture: str
    typed_sha256: str | None
    typed: str | None
    nonce: str
    at: datetime
    entries: tuple[Entry, ...]
    countersign_of: str | None = None
    countersign: str | None = None
    message_sha256: str = ""


def _at_text(at: datetime | None) -> str:
    if at is not None and (not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None):
        raise MessageError("`at` must be an aware datetime")
    return (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime(_AT_FORMAT)


def build_desk_message(*, store: str, install: str, surface: str, origin: str, view: str, gesture: str,
                       entries: Sequence[Entry], typed: str | None = None, typed_block: str | None = None,
                       nonce: str | None = None, at: datetime | None = None, countersign_of: str | None = None,
                       countersign: str | None = None) -> bytes:
    """The bytes the laptop key signs for a click (or a line typed) in the cockpit: ``levain-desk-answer/1``, signed in
    :data:`DESK_NAMESPACE`. There is no ``via`` line: the format line and the namespace are the channel. A ``click``
    names exactly one entry and carries no ``typed`` lines; ``typed`` names both and one entry or more.
    ``countersign_of`` and ``countersign`` (``confirm`` or ``disown``) come together or not at all. ``view`` is the
    band view the card was drawn from, or the literal ``item`` for a one-item click. Raises :class:`MessageError`
    for a field out of form, an entry with a ``note``, or a message over :data:`MAX_BYTES`."""
    entries = tuple(entries)
    if not entries:
        raise MessageError("a message answers at least one entry")
    if gesture not in GESTURES:
        raise MessageError(f"gesture is one of {', '.join(GESTURES)}")
    if (gesture == "click") != (typed is None) or (gesture == "click" and typed_block is not None):
        raise MessageError("a click carries no typed line and a typed gesture carries one")
    if gesture == "click" and len(entries) != 1:
        raise MessageError("a click answers exactly one entry")
    if any(e.note is not None for e in entries):
        raise MessageError("a desk entry has no note")
    if (countersign_of is None) != (countersign is None) or (countersign is not None and countersign not in COUNTERSIGNS):
        raise MessageError("countersign-of and countersign (confirm or disown) come together")
    nonce = secrets.token_hex(8) if nonce is None else nonce
    lines = [DESK_FORMAT, f"store: {_pattern(store, _STORE, 'store')}", f"install: {_pattern(install, _ID, 'install')}",
             f"surface: {_pattern(surface, _ID, 'surface')}", f"origin: {_pattern(origin, _ORIGIN, 'origin')}",
             f"view: {_pattern(view, _DESK_VIEW, 'view')}", f"gesture: {gesture}"]
    if gesture == "typed":
        said = _line(unicodedata.normalize("NFC", typed) if isinstance(typed, str) else typed, "typed")
        lines += [f"typed-sha256: {_sha(said if typed_block is None else typed_block)}", f"typed: {said}"]
    lines += [f"nonce: {_pattern(nonce, _NONCE, 'nonce')}", f"at: {_at_text(at)}"]
    if countersign_of is not None:
        lines += [f"countersign-of: {_pattern(countersign_of, _VERSION, 'countersign-of')}",
                  f"countersign: {countersign}"]
    for n, e in enumerate(entries, 1):
        lines.extend(_entry_lines(n, e))
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise MessageError(f"the message is {len(data)} bytes; the bound is {MAX_BYTES}")
    return data


def parse_desk_verified(signed: bytes) -> ParsedDesk:
    """The fields of desk bytes that VERIFIED, read as strictly as :func:`parse_verified`: the order
    :func:`build_desk_message` writes, nothing before, between or after, a click with its one entry and no typed
    lines, a typed gesture with both typed lines, ``countersign`` present exactly when ``countersign-of`` is."""
    if not isinstance(signed, (bytes, bytearray)) or len(signed) > MAX_BYTES:
        raise MessageError("not a message of this format, or over the size bound")
    try:
        text = bytes(signed).decode("utf-8")
    except UnicodeDecodeError:
        raise MessageError("the message is not UTF-8") from None
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise MessageError("the message does not end in exactly one newline")
    lines = text[:-1].split("\n")
    if lines[0] != DESK_FORMAT:
        raise MessageError("not a levain-desk-answer/1 message")
    i = 1
    store, i = _field(lines, i, "store", _STORE)
    install, i = _field(lines, i, "install", _ID)
    surface, i = _field(lines, i, "surface", _ID)
    origin, i = _field(lines, i, "origin", _ORIGIN)
    view, i = _field(lines, i, "view", _DESK_VIEW)
    gesture, i = _field(lines, i, "gesture", re.compile("|".join(GESTURES)))
    typed_sha = typed = None
    if gesture == "typed":
        typed_sha, i = _field(lines, i, "typed-sha256", _SHA)
        typed, i = _field(lines, i, "typed")
        if len(typed) > LINE_MAX or unicodedata.normalize("NFC", typed) != typed:
            raise MessageError("the typed line is over the bound or not NFC")
    nonce, i = _field(lines, i, "nonce", _NONCE)
    at_text, i = _field(lines, i, "at", _AT)
    try:
        at = datetime.strptime(at_text, _AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        raise MessageError("`at` is not a time") from None
    counter = which = None
    if i < len(lines) and lines[i].startswith("countersign-of: "):
        counter, i = _field(lines, i, "countersign-of", _VERSION)
        which, i = _field(lines, i, "countersign", re.compile("|".join(COUNTERSIGNS)))
    entries = _parse_entries(lines, i, allow_note=False)
    if gesture == "click" and len(entries) != 1:
        raise MessageError("a click answers exactly one entry")
    return ParsedDesk(store, install, surface, origin, view, gesture, typed_sha, typed, nonce, at, entries, counter,
                      which, hashlib.sha256(bytes(signed)).hexdigest())


def fresh(parsed: ParsedMessage, event_ts: datetime) -> bool:
    """`at` lies within :data:`STALE_S` before the resolve event, never after it. A bound on staleness only.
    An ``event_ts`` that is not an aware datetime is never fresh."""
    if not isinstance(event_ts, datetime) or event_ts.tzinfo is None or event_ts.utcoffset() is None:
        return False
    return timedelta(0) <= event_ts - parsed.at <= timedelta(seconds=STALE_S)


# ---------------------------------------------------------------------------------------------------
# the key


@dataclass(frozen=True)
class TerminalKey:
    path: Path
    public_key: str


def _real_key_dir() -> Path:
    """``<real home>/.levain/keys``: the home from the password database, never ``$HOME`` or ``$LEVAIN_HOME``."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".levain" / "keys"


def _own_regular_file(path: Path) -> bool:
    """False when nothing is at ``path``; True for a regular file this user owns; anything else (a link, a
    directory, another owner's file) raises ``KeyError_``. Read with ``lstat``, so a link is never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KeyError_(f"cannot read {path}: {exc.strerror or exc}") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise KeyError_(f"{path} is not a regular file of this user (a link is refused)")
    return True


def find_key(key_dir: Path | None = None) -> TerminalKey | None:
    """The enrolled terminal key, found by the fixed label :data:`KEY_LABEL` in the key directory, or None when
    there is none with a ``touch-id`` sidecar. Read-only: nothing is created. A directory open to other users or
    owned by someone else, a key, ``.pub`` or sidecar that is a link or not this user's regular file, or a sidecar
    that is there but malformed, raises ``KeyError_``. ``key_dir`` is for tests; the default is the real home's,
    whatever the environment says."""
    d = Path(key_dir) if key_dir is not None else _real_key_dir()
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise KeyError_(f"cannot read the key directory {d}: {exc.strerror or exc}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise KeyError_(f"{d} is not a private directory of this user (a link is refused)")
    path = d / KEY_LABEL
    present = [_own_regular_file(p) for p in (path, d / (KEY_LABEL + ".pub"))]
    if not all(present):
        return None
    _own_regular_file(d / (KEY_LABEL + SIDECAR_SUFFIX))
    sidecar = read_sidecar(path)
    if sidecar is None or sidecar.custody != TOUCH_ID:
        return None
    return TerminalKey(path, signing_public_key(path))


@dataclass(frozen=True)
class StatusReport:
    state: str                       # "not enrolled" | "enrolled" | "warning"
    text: str
    public_key: str | None = None


def status(key_dir: Path | None = None, *, sc_auth: Path | str = SC_AUTH, identities: list | None = None,
           platform: str | None = None) -> StatusReport:
    """``terminal-key status``: read-only (``sc_auth list-ctk-identities`` and a look at the key directory).
    Never raises: anything that stops the look is a ``warning`` report naming it."""
    try:
        key = find_key(key_dir)
    except Exception as exc:  # noqa: BLE001 - a status display reports, it never crashes its caller
        return StatusReport("warning", f"WARNING: the terminal key cannot be read: {_one_line(exc)}")
    if key is None:
        return StatusReport("not enrolled", "not enrolled: answers at the terminal are recorded unsigned")
    try:
        fp = _keys.fingerprint_of(key.public_key)
        ok, note = ctk_status(key.public_key, sc_auth=sc_auth, identities=identities, platform=platform)
    except Exception as exc:  # noqa: BLE001 - as above (sc_auth missing is an OSError, a hang a TimeoutExpired)
        return StatusReport("warning", f"WARNING: the terminal key's identity cannot be checked: {_one_line(exc)}",
                            key.public_key)
    if ok is True:
        return StatusReport("enrolled", f"terminal key: enrolled ({fp}), {note}", key.public_key)
    return StatusReport("warning", f"WARNING: terminal key ({fp}): {note}", key.public_key)


@dataclass(frozen=True)
class CreateReport:
    created: bool
    text: str
    public_key: str | None = None


def signers_line(public_key: str, principal: str = "phill") -> str:
    """The line flow's ``scripts/needs_phill_signers`` takes for this key (DESIGN section 3, step 2)."""
    return f'{principal} namespaces="{SIGNERS_NAMESPACES}" {" ".join(public_key.split()[:2])}'


def session_refusal() -> str:
    """The refusal of ``terminal-key create`` inside an agent session: one text, which :func:`create` returns and
    the CLI prints, so the two never say different things."""
    from levain.consent.band import AGENT_ANSWER_REFUSAL
    return f"refused: run it from a fresh terminal, not inside an agent session ({AGENT_ANSWER_REFUSAL})"


def enroll_command(public_path: Path | str) -> str:
    """The ``levain cockpit enroll`` line for the terminal key, shell-quoted: that verb takes the public key FILE."""
    from levain.authority import KEY_WORD
    return shlex.join(["levain", "cockpit", "enroll", str(public_path), "--class", "laptop-presence",
                       "--custody", TOUCH_ID, "--signing-key", KEY_WORD])


def create(key_dir: Path | None = None, *, creator: Callable[..., Any] | None = None,
           in_agent_session: Callable[[], bool] | None = None) -> CreateReport:
    """``terminal-key create``: one Secure Enclave key labelled :data:`KEY_LABEL` whose every signature needs Touch
    ID, in the real levain home's key directory. Refuses (nothing created) inside any agent session, judged by
    ``in_agent_session`` (default ``levain.consent.band.agent_session_caller``, the band's one detector) before the
    key directory is touched, and when a terminal key is already enrolled. ``creator`` is
    ``custody.create_touch_id``'s door and the only place a real key is made; tests inject theirs.
    Never raises: a refusal, a failed creation, any exception on the way and a Ctrl-C are a report. ``created`` is
    True once ``creator`` has returned a key, even when printing the instructions then fails; an interrupt at the
    moment ``creator`` returns is told apart by looking for the key again."""
    progress = _Progress()
    try:
        return _create(key_dir, creator, in_agent_session, progress)
    except KeyboardInterrupt:
        return _after_failure(progress, "cancelled")
    except Exception as exc:  # noqa: BLE001 - the CLI prints a report, never a traceback
        return _after_failure(progress, _one_line(exc))


@dataclass
class _Progress:
    """How far :func:`_create` got, read by :func:`create` when something escapes it."""
    key_dir: Path | None = None
    started: bool = False            # the creator was called
    made: Any = None                 # what it returned: the key is persisted from here on


def _after_failure(progress: _Progress, why: str) -> CreateReport:
    if progress.made is not None:
        m = progress.made
        return _created_report(m.path, m.public_path, m.public_key, f"stopped while printing ({why})")
    if progress.started:
        # the interrupt or failure came at the creator's return: custody removed its key, or the key is committed
        try:
            key = find_key(progress.key_dir)
        except Exception as exc:  # noqa: BLE001 - neither answer is known, so neither is claimed
            return CreateReport(False, f"{why}; whether the key was created cannot be told ({_one_line(exc)}); "
                                       "`levain band terminal-key status` shows it")
        if key is not None:
            return _created_report(key.path, key.path.with_name(key.path.name + ".pub"), key.public_key,
                                   f"stopped as creation finished ({why})")
    return CreateReport(False, f"not created: {why}")


#: The third printed step: the ledger is not done at genesis. A Secure Enclave key dies with its laptop and the build has
#: no re-root, so an independent offline passphrase key is enrolled by the genesis key in the same sitting
#: (``levain doctor`` FAILS at exactly one signing laptop-presence key). Both commands are the ones run on a scratch root.
_CO_ROOT_STEP = [
    "3. Co-root, the same sitting, BEFORE the ledger counts as done (a lost laptop must not lose the ledger):",
    "   first: levain cockpit key create --custody passphrase --label laptop-presence",
    "   then, signed by the key above: levain cockpit enroll <that key's .pub> --class laptop-presence --custody passphrase --signing-key "
    "<the enrolled key>",
    "   keep that passphrase and key file off this laptop; the runbook is project_memory/1103-19/ENROL_SITTING.md",
]


def _created_report(path: Path, public_path: Path, public_key: str, note: str | None = None) -> CreateReport:
    """The created report, built piece by piece: a piece that cannot be built is named, never a lost key."""
    lines = [f"terminal key created: {path}"]
    unprinted: list[str] = []
    try:
        lines += ["1. Flow: commit this line to scripts/needs_phill_signers (a reviewed commit):",
                  f"   {signers_line(public_key)}"]
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - the key exists; say what is missing
        unprinted.append(f"the signers line ({_one_line(exc)})")
    try:
        lines += ["2. Levain (only for ruling compose and the desk click path; replace the signing key word):",
                  f"   {enroll_command(public_path)}"]
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - as above
        unprinted.append(f"the enroll command ({_one_line(exc)})")
    lines += _CO_ROOT_STEP
    if note:
        lines.append(f"note: {note}")
    if unprinted:
        lines.append(f"could not print {' and '.join(unprinted)}; the public key file is {public_path}, and "
                     "`levain band terminal-key status` shows the key")
    return CreateReport(True, "\n".join(lines), public_key)


_STALE = "; choose another --label"


def _not_created(exc: KeyError_) -> str:
    """Custody's refusal, except where it advises a ``--label`` this verb does not take (its label is fixed)."""
    text = _one_line(exc)
    if _STALE not in text:
        return f"not created: {text}"
    what = text.split(": ", 1)[-1].split(_STALE, 1)[0]
    return (f"not created: {what}, a leftover of an earlier terminal key; remove it, then run create again "
            "(`levain band terminal-key status` shows the detail)")


def _create(key_dir: Path | None, creator: Callable[..., Any] | None,
            in_agent_session: Callable[[], bool] | None, progress: _Progress) -> CreateReport:
    if in_agent_session is None:
        from levain.consent.band import agent_session_caller as in_agent_session
    try:
        inside = in_agent_session()
    except Exception as exc:  # noqa: BLE001 - a session that cannot be told apart is refused (fail-closed)
        return CreateReport(False, "refused: whether this runs inside an agent session cannot be told: "
                                   f"{_one_line(exc)} (nothing was created)")
    if inside:
        return CreateReport(False, f"{session_refusal()} (nothing was created)")
    try:
        d = Path(key_dir) if key_dir is not None else _real_key_dir()
        if find_key(d) is not None:
            return CreateReport(False, "refused: a terminal key is already enrolled; `levain band terminal-key "
                                       "status` shows it (nothing was created)")
    except Exception as exc:  # noqa: BLE001 - a key directory that cannot be read is a refusal, never a creation
        return CreateReport(False, f"refused: the key directory cannot be read: {_one_line(exc)} "
                                   "(nothing was created)")
    if creator is None:
        from levain.cockpit.custody import create_touch_id as creator
    progress.key_dir, progress.started = d, True
    try:
        progress.made = creator(d, KEY_LABEL)
    except KeyError_ as exc:
        return CreateReport(False, _not_created(exc))
    except Exception as exc:  # noqa: BLE001 - as above
        return CreateReport(False, f"not created: {_one_line(exc)}")
    m = progress.made
    return _created_report(m.path, m.public_path, m.public_key)


# ---------------------------------------------------------------------------------------------------
# signing


@dataclass(frozen=True)
class SignOutcome:
    state: str                       # "signed" | "unsigned"
    reason: str | None = None        # one of REASONS when unsigned
    detail: str = ""
    signature: str | None = None
    fingerprint: str | None = None


SignerFactory = Callable[[Path, float], tuple[str, Signer]]    # (key path, the sign step's bound in seconds)


def default_signer_factory(key_path: Path, timeout: float | None = None) -> tuple[str, Signer]:
    """The one door to the real key: ``ssh_signer`` in :data:`NAMESPACE`, bounded by ``timeout`` seconds.
    Calling the ``Signer`` it returns raises the Touch ID prompt."""
    return _keys.ssh_signer(key_path, namespace=NAMESPACE, timeout=timeout)


def default_desk_signer_factory(key_path: Path, timeout: float | None = None) -> tuple[str, Signer]:
    """:func:`default_signer_factory` for the desk format: the same key, signing in :data:`DESK_NAMESPACE`. The only
    other door to the real key."""
    return _keys.ssh_signer(key_path, namespace=DESK_NAMESPACE, timeout=timeout)


def is_remote(env: Mapping[str, str] | None = None, presence: str | None = None) -> bool:
    """Routing only (never evidence): the desk is remote when the presence says so or the hook runs in an ssh
    session. A false "local" costs a prompt nobody touches, which times out."""
    env = os.environ if env is None else env
    return bool(presence and "remote" in presence.lower()) or bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))


def _classify(exc: KeyError_) -> tuple[str, str]:
    msg = str(exc)
    first = msg.splitlines()[0] if msg else "signer failed"
    if "Touch ID was cancelled" in msg:
        return "cancelled", first
    if "TimeoutExpired" in msg:
        return "timeout", first
    # a fast non-zero exit from ssh-keygen (R4 will say whether this is also the lid-closed case)
    if re.search(r"could not sign with .*\(exit \d+\)", msg):
        return "unavailable", first
    return "signer-error", first


def sign(message: bytes, *, key: TerminalKey | None = None, key_dir: Path | None = None,
         signer_factory: SignerFactory | None = None, remote: bool = False,
         bound_s: float = SIGN_BOUND_S) -> SignOutcome:
    """Sign ``message`` with the terminal key, or say exactly why not (:data:`REASONS`); never raises (an
    unexpected failure is ``signer-error``). ``remote`` routes to ``unsigned/remote`` before any key is looked
    at, so no prompt reaches an unattended screen.

    ``bound_s`` goes to the signer factory, which hands it to the signer as its own per-call timeout, so one
    ``subprocess.run`` timeout ends the ssh-keygen child (and with it the Touch ID sheet) and no process-wide
    value is touched."""
    if remote:
        return SignOutcome("unsigned", "remote", "the desk is remote")
    if not isinstance(message, (bytes, bytearray)) or len(message) > MAX_BYTES:
        return SignOutcome("unsigned", "signer-error", "message over the size bound")
    try:
        if key is None:
            key = find_key(key_dir)
        if key is None:
            return SignOutcome("unsigned", "no-key", "no terminal key with a touch-id sidecar")
        factory = signer_factory or default_signer_factory
        fp, signer = factory(key.path, bound_s)
        signature = signer(bytes(message))
    except KeyError_ as exc:
        reason, detail = _classify(exc)
        return SignOutcome("unsigned", reason, detail)
    except VerifierUnavailable as exc:
        return SignOutcome("unsigned", "signer-error", str(exc).splitlines()[0] if str(exc) else "verifier unavailable")
    except subprocess.TimeoutExpired:
        return SignOutcome("unsigned", "timeout", "the sign step ran out of time")
    except OSError as exc:
        return SignOutcome("unsigned", "signer-error", f"{type(exc).__name__}: {exc.strerror or exc}")
    except Exception as exc:  # noqa: BLE001 - the hook records an answer whatever the signer did
        return SignOutcome("unsigned", "signer-error", _one_line(exc))
    if not isinstance(signature, str) or not signature:
        return SignOutcome("unsigned", "signer-error", "the signer returned no signature")
    return SignOutcome("signed", None, "", signature, fp)


# ---------------------------------------------------------------------------------------------------
# the notice that names what the Touch ID sheet will sign (DESIGN section 5.4)

NOTICE_TITLE = "Levain: Touch ID"
NOTICE_CUT = 80                    # characters of the first entry's answer or text
_OSASCRIPT = "/usr/bin/osascript"


def notice_text(entries: Sequence[Entry], *, channel: str = "terminal") -> str:
    """The one line the notification shows: ``Touch ID to sign: <item> <answer or text>`` for the first entry (cut at
    :data:`NOTICE_CUT` characters) plus `` (+N more)``; channel ``desk`` prefixes ``Desk: ``. Pure, and rendered from
    the entries the message holds, never from anything the agent says. It always begins with a letter, so as an
    ``osascript`` argument it can never read as an option."""
    prefix = "Desk: " if channel == "desk" else ""
    if not entries:
        return f"{prefix}Touch ID to sign"
    first = entries[0]
    said = (_shown(first.answer, "answer", cut=NOTICE_CUT) if first.answer is not None
            else _shown(first.text or "", "text", cut=NOTICE_CUT))
    more = f" (+{len(entries) - 1} more)" if len(entries) > 1 else ""
    return f"{prefix}Touch ID to sign: {_shown(first.item, 'item')} {said}".rstrip() + more


_MARKER = "⟨U+{:04X}⟩"
_ELLIPSIS = "…"


def _shown(value: Any, field: str, *, cut: int | None = None) -> str:
    """``value`` as the notice displays it: the field's own text, character for character, with every character
    :mod:`levain.unshowable` calls unshowable in that field (:func:`_allow`, the same rule :func:`build_message` and
    :func:`parse_verified` apply to it) shown as a visible ``⟨U+XXXX⟩`` marker, never dropped. Every entry that
    reaches the notice on the signing path has passed :func:`build_message`, so there no marker is ever needed and the
    display is the signed words.

    ``cut``: when the display would be longer than ``cut`` characters, the RAW text is cut first, at the last
    boundary between two user-perceived characters (:func:`_cluster_ends`) whose display fits in ``cut - 1``, and
    ``…`` is appended. So a cut never lands inside a marker, never parts a base from its combining marks, variation
    selector or joined sequence, and the display is always a prefix of the signed text plus an explicit ellipsis."""
    from levain import unshowable

    text = str(value)
    allow = _allow(field)
    hidden = unshowable.hidden(text, allow=allow)
    marked = len(text) + sum(len(_MARKER.format(ord(text[i]))) - 1 for i in hidden)
    if cut is None or marked <= cut:
        return unshowable.mark(text, _MARKER, allow=allow)
    extra = {i: len(_MARKER.format(ord(text[i]))) - 1 for i in hidden}
    keep, width, start = 0, 0, 0
    for end in _cluster_ends(text):
        width += (end - start) + sum(extra.get(i, 0) for i in range(start, end))
        if width > cut - 1:
            break
        keep, start = end, end
    return unshowable.mark(text[:keep], _MARKER, allow=allow) + _ELLIPSIS


def _extends(cp: int, cat: str) -> bool:
    """Whether a character joins the one before it in a user-perceived character (UAX #29 Extend, SpacingMark, the
    joiners, and Hangul vowel and final jamo), approximately: combining marks (Mn, Me, Mc), U+200C and U+200D,
    variation selectors, emoji modifiers, tag characters."""
    return (cat in ("Mn", "Me", "Mc") or cp in (0x200C, 0x200D) or 0xFE00 <= cp <= 0xFE0F
            or 0xE0100 <= cp <= 0xE01EF or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F
            or 0x1160 <= cp <= 0x11FF or 0xD7B0 <= cp <= 0xD7FF)


def _cluster_ends(text: str) -> list[int]:
    """The end index of each user-perceived character of ``text``, in order (the last is ``len(text)``). A stdlib
    approximation of UAX #29 extended grapheme clusters, conservative in one direction only: it may keep two clusters
    together, never part one. No break before an extending character (:func:`_extends`), after U+200D (an emoji ZWJ
    sequence), or inside a pair of regional indicators (a flag)."""
    ends: list[int] = []
    ri_run = 0                  # regional indicators in a row ending at the previous character
    for i, ch in enumerate(text):
        cp = ord(ch)
        ri = 0x1F1E6 <= cp <= 0x1F1FF
        joined = i > 0 and (_extends(cp, unicodedata.category(ch)) or text[i - 1] == "‍"
                            or (ri and ri_run % 2 == 1))
        if i > 0 and not joined:
            ends.append(i)
        ri_run = ri_run + 1 if ri else 0
    if text:
        ends.append(len(text))
    return ends


def post_notice(text: str, *, runner: Callable[..., Any] = subprocess.Popen) -> Any:
    """Show ``text`` as a macOS notification, macOS only; the process, or None when nothing was started. The text
    travels as an ``osascript`` argument and never inside the script, so a row's words cannot become AppleScript.
    The child is detached with no stdio and never waited on: the hook's time budget is not spent on a display aid.
    Never raises."""
    if sys.platform != "darwin":
        return None
    try:
        return runner([_OSASCRIPT, "-e", "on run argv",
                       "-e", f'display notification (item 1 of argv) with title "{NOTICE_TITLE}"',
                       "-e", "end run", str(text)],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      start_new_session=True)
    except Exception:  # noqa: BLE001 - a notice is never worth the answer
        return None


# ---------------------------------------------------------------------------------------------------
# verifying


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    fingerprint: str | None = None
    flags: int | None = None
    user_present: bool = False
    user_verified: bool = False
    ctk: bool | None = None
    ctk_detail: str = ""
    public_key: str | None = None
    claim: str = ""
    badge: str = ""
    problem: str = ""


def _refuse(problem: str, **kw: Any) -> VerifyResult:
    return VerifyResult(False, problem=problem, **kw)


def verify(message: bytes, sig: str, signers: str, principal: str, *, ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN,
           ctk: Callable[[str], tuple[bool | None, str]] | None = None, timeout: float = 10.0,
           namespace: str = NAMESPACE) -> VerifyResult:
    """Whether ``sig`` is an SSHSIG over ``message`` in ``namespace`` (:data:`NAMESPACE`, or :data:`DESK_NAMESPACE`
    for a desk click; any other is refused) by a key ``signers`` (the text of an
    allowed-signers file) lists for ``principal``. Stock ``ssh-keygen -Y verify``; the signers and the signature
    reach it through pipes. A FIDO signature must carry the user-presence flag. The key's live CTK status on this
    Mac (``ctk``, default :func:`ctk_status`) is read from the signature's own key: False refuses, None only
    weakens the claim. Raises :class:`VerifierUnavailable` when ssh-keygen cannot run (never a verdict)."""
    if namespace not in (NAMESPACE, DESK_NAMESPACE):
        return _refuse("the namespace is not one this module signs in")
    if not isinstance(message, (bytes, bytearray)) or len(message) > MAX_BYTES:
        return _refuse("the message is over the size bound")
    if not isinstance(principal, str) or _PRINCIPAL.fullmatch(principal) is None:
        return _refuse("the principal is not in its form")
    if not isinstance(sig, str) or not isinstance(signers, str):
        return _refuse("signature or signers is not text")
    try:
        public_key, fp_claimed, _sig_type = sshsig_public_key(sig)
        _, flags = _sshsig_type_and_flags(sig)
        sig_bytes = (sig.strip() + "\n").encode("ascii")
        signers_bytes = signers.encode("utf-8")
    except (ValueError, UnicodeError):
        return _refuse("not a signature levain reads")
    if presence_missing(sig, public_key):
        return _refuse("the signature lacks the user-presence flag", flags=flags)
    problem, keygen = trust_anchor_problem(ssh_keygen, what="ssh-keygen")
    if problem is not None:
        raise VerifierUnavailable(f"cannot verify a terminal signature: {problem}")
    fds: list[int] = []
    try:
        fds.append(_pipe_reading(signers_bytes))
        fds.append(_pipe_reading(sig_bytes))
        cp = subprocess.run([str(keygen), "-Y", "verify", "-f", f"/dev/fd/{fds[0]}", "-I", principal,
                             "-n", namespace, "-s", f"/dev/fd/{fds[1]}"], input=bytes(message),
                            capture_output=True, timeout=timeout, env=dict(_VERIFY_ENV), pass_fds=tuple(fds))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise VerifierUnavailable(f"ssh-keygen could not verify a terminal signature ({type(exc).__name__})") from None
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    if cp.returncode != 0:
        return _refuse("the signature does not verify for this principal in this namespace", flags=flags,
                       public_key=public_key)
    good = re.compile(r'Good "' + re.escape(namespace) + r'" signature for ' + re.escape(principal)
                      + r' with \S+ key (SHA256:[A-Za-z0-9+/]{43})')
    out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
    found = [m.group(1) for line in out.splitlines() if (m := good.match(line.strip()))]
    if found != [fp_claimed]:
        return _refuse("the verifier named a different key than the signature embeds", flags=flags,
                       public_key=public_key)
    try:
        live, detail = (ctk or ctk_status)(public_key)
    except KeyError_ as exc:
        live, detail = None, f"the CTK check could not run: {exc}"
    present = bool(flags is not None and flags & 0x01)
    verified = bool(flags is not None and flags & 0x04)
    if live is False:
        return VerifyResult(False, fp_claimed, flags, present, verified, False, detail, public_key,
                            problem=f"the key's Secure Enclave identity is not biometric-protected: {detail}")
    if live is True:
        claim = (f"signed by the enrolled laptop Touch ID key ({fp_claimed}); its Secure Enclave identity on this "
                 "Mac is biometric-protected")
    else:
        claim = f"signed by the enrolled laptop key ({fp_claimed}), custody touch-id as recorded"
    return VerifyResult(True, fp_claimed, flags, present, verified, live, detail, public_key, claim,
                        "signed: Touch ID, laptop" + (" (desk)" if namespace == DESK_NAMESPACE else ""))


# ---------------------------------------------------------------------------------------------------
# the classifier (DESIGN section 5), for Levain's own store


@dataclass(frozen=True)
class AnswerClass:
    state: str          # signed-terminal | disowned | unsigned-terminal | agent-resolved | legacy | failed
    signer_fp: str | None = None
    claim: str = ""
    detail: str = ""
    late: bool = False
    reason: str = ""    # failed only: one of FAILED_REASONS
    channel: str = ""   # signed-terminal and disowned: where he signed, "terminal" or "desk"

    @property
    def badge(self) -> str:
        """The words a reader shows for this state (empty when there is nothing to show)."""
        if self.state == "signed-terminal":
            return "signed: Touch ID, laptop" + (" (desk)" if self.channel == "desk" else "")
        if self.state == "disowned":
            return "you said this answer was not you"
        return ""


def desk_origin(host: Any, port: int) -> str | None:
    """The one origin a desk message signs: ``http://<host>`` for the request's validated ``Host`` when it names
    this server's own port on ``localhost`` or ``127.0.0.1`` (case folded), else None (the route refuses). The port
    is 1..65535 and the assembled origin is one :func:`build_desk_message` accepts (``_ORIGIN``), so an origin this
    returns never fails later inside the click as a ``MessageError``."""
    if not isinstance(host, str) or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    h = host.lower()
    if h not in (f"localhost:{port}", f"127.0.0.1:{port}"):
        return None
    origin = f"http://{h}"
    return origin if _ORIGIN.fullmatch(origin) else None


def desk_surface(name: Any) -> str:
    """The surface a desk message signs for a cockpit credential ``name``: the name itself when it is in the
    message's form and does not start with ``x--``, else ``x--`` and the SHA-256 of its UTF-8 bytes (so two names
    never sign as one, and a name outside the form is never collapsed to another credential's)."""
    text = name if isinstance(name, str) else ""
    if _ID.fullmatch(text) and not text.startswith("x--"):
        return text
    return "x--" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


# why a signed block is `failed` (machine-readable; `detail` says it in words)
FAILED_REASONS = ("malformed", "verifier-unavailable", "bad-signature", "scope-mismatch", "countersign-mismatch",
                  "not-this-row", "answer-mismatch", "stale", "classifier-error", "channel-mismatch")
SCOPE_FIELDS = ("store", "install", "session", "view")
DESK_SCOPE_FIELDS = ("store", "install", "surface", "origin", "view")


@dataclass(frozen=True)
class _Spec:
    """What differs between the two signed formats; every binding check is the one :func:`_signed`."""
    format: str
    namespace: str
    parse: Callable[[bytes], Any]
    scope_fields: tuple[str, ...]
    channel: str


_TERMINAL = _Spec(FORMAT, NAMESPACE, parse_verified, SCOPE_FIELDS, "terminal")
_DESK = _Spec(DESK_FORMAT, DESK_NAMESPACE, parse_desk_verified, DESK_SCOPE_FIELDS, "desk")


@dataclass
class _Ctx:
    signers: str = ""
    principal: str = "phill"
    ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN
    ctk: Callable[[str], tuple[bool | None, str]] | None = None
    countersigns: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    store: Any = None
    install: Any = None


def _failed(reason: str, detail: str) -> AnswerClass:
    return AnswerClass("failed", detail=detail, reason=reason)


def event_time(value: Any) -> datetime | None:
    """A stored event's ``ts`` as an aware datetime: an aware datetime, an ISO 8601 text with an offset or ``Z``,
    or a finite epoch number. None for anything else (absent, naive, malformed), which no freshness check
    passes."""
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None and value.utcoffset() is not None else None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str) and len(value) <= 64:
            got = datetime.fromisoformat(value)
            return got if got.tzinfo is not None and got.utcoffset() is not None else None
    except (ValueError, OverflowError, OSError):
        return None
    return None


def target_id(answer: Mapping[str, Any]) -> str | None:
    """The identifier a countersign's signed ``countersign-of`` line must name: the answer's ``ts`` as its store
    wrote it (a text verbatim, a number in Python's ``str`` form). None when the answer has no usable ``ts``."""
    v = answer.get("ts")
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = str(v)
    if isinstance(v, str) and _VERSION.fullmatch(v):
        return v
    return None


def _recorded(answer: Mapping[str, Any]) -> tuple[str, str] | None:
    """``("answer", v)`` or ``("text", v)``: exactly one of the two present (not None) and text, else None."""
    a, t = answer.get("answer"), answer.get("text")
    if a is not None and t is None and isinstance(a, str):
        return "answer", a
    if t is not None and a is None and isinstance(t, str):
        return "text", t
    return None


def _signed(spec: _Spec, signed: Any, recorded: Mapping[str, Any], row: Mapping[str, Any] | None, ctx: _Ctx,
            scope: Mapping[str, Any], event_ts: Any, countersign_of: str | None,
            want: str | None = None) -> tuple[AnswerClass, Any]:
    """Good only if the signature verifies in ``spec``'s namespace, the bytes parse as ``spec``'s format, the
    signed scope fields equal ``scope``, the signed ``countersign-of`` is exactly ``countersign_of`` (absent on a
    direct answer), a signed ``countersign`` value (desk format) agrees with ``want`` when the event names one, the
    entry the block names is the row it is attached to (item, row_version, asked), the answer ``recorded`` holds is
    the signed one in kind and value, and ``at`` is fresh against ``event_ts``. Returns the class and, when the
    bytes parsed, the parse (so a caller can read the signed ``countersign`` value)."""
    noun = f"{spec.channel} signature"
    if not isinstance(signed, Mapping) or signed.get("format") != spec.format:
        return _failed("malformed", f"not a {noun}"), None
    message, sig = signed.get("message"), signed.get("sig")
    if not isinstance(message, str) or not isinstance(sig, str):
        return _failed("malformed", "the signed block carries no message and signature"), None
    try:
        raw = message.encode("utf-8")
    except UnicodeError:
        return _failed("malformed", "the signed message is not UTF-8 encodable"), None
    try:
        got = verify(raw, sig, ctx.signers, ctx.principal, ssh_keygen=ctx.ssh_keygen, ctk=ctx.ctk,
                     namespace=spec.namespace)
    except (VerifierUnavailable, KeyError_, OSError, subprocess.SubprocessError) as exc:
        return _failed("verifier-unavailable", f"the signature could not be checked: {_one_line(exc)}"), None
    if not got.ok:
        return _failed("bad-signature", got.problem), None
    try:
        parsed = spec.parse(raw)
    except MessageError as exc:
        return _failed("malformed", f"signed bytes are not a {spec.channel} answer: {exc}"), None
    if signed.get("message_sha256") not in (None, parsed.message_sha256):
        return _failed("malformed", "message_sha256 is not the message's"), parsed
    for name in spec.scope_fields:
        have = scope.get(name)
        if not isinstance(have, str) or not have or getattr(parsed, name) != have:
            return _failed("scope-mismatch", f"the signed {name} is not this answer's (or the reader named none)"), parsed
    if countersign_of is None and parsed.countersign_of is not None:
        return _failed("countersign-mismatch", "a countersign message presented as a direct answer"), parsed
    if countersign_of is not None and parsed.countersign_of != countersign_of:
        return _failed("countersign-mismatch", "the signed countersign-of does not name this answer"), parsed
    if countersign_of is not None:
        # the kind a countersign SIGNED: the desk format names it; the terminal format can only confirm
        kind = parsed.countersign if spec.channel == "desk" else "confirm"
        if kind not in COUNTERSIGNS or (want is not None and want != kind):
            return _failed("countersign-mismatch", "the signed countersign is not the kind this event claims"), parsed
    n = signed.get("entry")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(parsed.entries):
        return _failed("not-this-row", "the event names no entry of the signed message"), parsed
    entry = parsed.entries[n - 1]
    if not isinstance(row, Mapping):
        return _failed("not-this-row", "no row to bind the entry to"), parsed
    if (entry.item != row.get("id") or entry.row_version != row.get("row_version")
            or entry.asked != row.get("what")):
        return _failed("not-this-row", "the signed entry is not this row (item, version or question)"), parsed
    kept = _recorded(recorded)
    signed_answer = ("answer", entry.answer) if entry.answer is not None else ("text", entry.text)
    if kept is None or kept != signed_answer:
        return _failed("answer-mismatch", "the recorded answer is not the signed one (kind or words)"), parsed
    when = event_time(event_ts)
    if when is None:
        return _failed("stale", "the event has no aware time to bound the signature by"), parsed
    if not fresh(parsed, when):
        return _failed("stale", "the signature is not within ten minutes before the event"), parsed
    return AnswerClass("signed-terminal", got.fingerprint, got.claim, channel=spec.channel), parsed


def answer_class(answer: Mapping[str, Any], row: Mapping[str, Any] | None = None, context: Mapping[str, Any] | None = None
                 ) -> AnswerClass:
    """The one function every reader of a Levain answer goes through; it never raises (anything unexpected is
    ``failed``/``classifier-error``). ``answer`` is the resolve event as stored (``signed`` / ``via`` /
    ``unsigned`` / ``answer`` / ``text`` / ``ts``); ``row`` the row it is attached to (``id``, ``row_version``,
    ``what``); ``context`` holds ``signers`` (allowed-signers text), ``principal``, ``ctk``, ``ssh_keygen``, the
    scope the reader expects (``store``, ``install``, ``session``, ``view``) and ``countersigns``.

    A direct signature must carry all four scope fields as the context names them, no ``countersign-of`` line,
    and an ``at`` fresh against the answer's own ``ts``. A countersign is a later event ``{of_ts, ts, session,
    view, signed}``: its signed ``countersign-of`` must name the answer (:func:`target_id`), its store and install
    are the context's, its session and view its own event's, and its ``at`` is bounded by its own ``ts``; it
    confirms the answer the original event recorded. A desk countersign ``{of_ts, ts, surface, origin, view, signed}``
    is scoped like a direct answer: every field from the context (the reader's origin, surface and view), and the
    event's own surface, origin and view must each equal them. Any missing expected field fails closed.

    Order: a ``signed`` block that re-verifies (its format picks the namespace and the parser; a format that
    disagrees with ``via`` is ``channel-mismatch``), else a verifying countersign (marked late; a verified desk
    ``disown`` makes the state ``disowned``), else the labels,
    which never confer anything. No field a hook or CLI writes is read as evidence."""
    try:
        return _answer_class(answer, row, context)
    except Exception as exc:  # noqa: BLE001 - the chokepoint classifies, it never crashes a reader
        return _failed("classifier-error", f"the answer could not be classified: {_one_line(exc)}")


_DESK_READER_FIELDS = ("surface", "origin", "view")


def _scope_for(spec: _Spec, c: Mapping[str, Any], cs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The scope a reader expects: from its context for a direct answer and for a desk countersign (the reader's own
    origin, surface and view: the event's claims are only compared against them, :func:`_desk_claims_agree`); for a
    terminal countersign the reader's store and install and the event's own session and view."""
    if cs is None or spec.channel == "desk":
        return {name: c.get(name) for name in spec.scope_fields}
    return {"store": c.get("store"), "install": c.get("install"), "session": cs.get("session"), "view": cs.get("view")}


def _desk_claims_agree(cs: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    """A desk countersign event's own surface, origin and view each name what the reader expects (an absent claim
    does not agree)."""
    return all(isinstance(cs.get(n), str) and cs.get(n) == scope.get(n) for n in _DESK_READER_FIELDS)


def _answer_class(answer: Any, row: Any, context: Any) -> AnswerClass:
    if not isinstance(answer, Mapping):
        return _failed("malformed", "the answer is not an event")
    c = dict(context or {})
    ctx = _Ctx(c.get("signers", ""), c.get("principal", "phill"), c.get("ssh_keygen", DEFAULT_SSH_KEYGEN),
               c.get("ctk"), tuple(c.get("countersigns") or ()), c.get("store"), c.get("install"))
    via = answer.get("via")
    signed = answer.get("signed")
    if signed is not None:
        fmt = signed.get("format") if isinstance(signed, Mapping) else None
        if fmt == FORMAT:
            # a via naming another channel is a transplant; an event with no via is judged by its namespace (flow's
            # needs_phill_sign reader passes the terminal block without the event's label)
            if via is not None and via not in TERMINAL_VIAS:
                return _failed("channel-mismatch", "a terminal-namespace signature on an answer not made at a terminal")
            spec = _TERMINAL
        elif fmt == DESK_FORMAT:
            if via not in DESK_VIAS:
                return _failed("channel-mismatch", "a desk-namespace signature on an answer that was not made at the desk")
            spec = _DESK
        else:
            return _failed("malformed", "not a terminal signature")
        return _signed(spec, signed, answer, row, ctx, _scope_for(spec, c), answer.get("ts"), None)[0]
    target = target_id(answer)
    confirmed: AnswerClass | None = None
    for cs in ctx.countersigns:
        if target is None or not isinstance(cs, Mapping) or cs.get("of_ts") != answer.get("ts") \
                or cs.get("signed") is None:
            continue
        block = cs.get("signed")
        fmt = block.get("format") if isinstance(block, Mapping) else None
        # a countersign has its own channel: one that names its via must name a label of its format's channel; one
        # that names none is judged by its namespace alone
        cs_via = cs.get("via")
        if fmt == FORMAT and (cs_via is None or cs_via in TERMINAL_VIAS):
            spec = _TERMINAL
        elif fmt == DESK_FORMAT and (cs_via is None or cs_via in DESK_VIAS):
            spec = _DESK
        else:
            continue
        scope = _scope_for(spec, c, cs)
        if spec.channel == "desk" and not _desk_claims_agree(cs, scope):
            continue
        got, parsed = _signed(spec, block, answer, row, ctx, scope, cs.get("ts"), target, want=cs.get("countersign"))
        if got.state != "signed-terminal":
            continue
        if getattr(parsed, "countersign", None) == "disown":      # a verified disown outranks every confirm
            return AnswerClass("disowned", got.signer_fp, got.claim, "disowned later", late=True, channel=spec.channel)
        if confirmed is None:
            confirmed = AnswerClass(got.state, got.signer_fp, got.claim, "countersigned later", late=True,
                                    channel=spec.channel)
    if confirmed is not None:
        return confirmed
    if via == "cockpit-desk":      # only a signed desk write carries this label; a bare one is a forgery signal
        return _failed("channel-mismatch", "the desk label without a desk signature")
    if via == "terminal":
        un = answer.get("unsigned")
        reason = un.get("reason") if isinstance(un, Mapping) else None
        return AnswerClass("unsigned-terminal", detail=str(reason) if reason else "")
    if via:
        return AnswerClass("agent-resolved")
    return AnswerClass("legacy", detail="unsigned (before signing existed)")
