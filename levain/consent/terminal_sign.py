"""levain.consent.terminal_sign: the signer core of TERMSIGN, a band answer typed at the terminal and signed with
the laptop's Touch ID key (design: project_memory/1102-5/TERMSIGN/DESIGN.md).

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
import stat
import subprocess
import threading
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
from levain.cockpit.custody import SC_AUTH, TOUCH_ID, ctk_status, list_identities, read_sidecar
from levain.cockpit.keys import KeyError_, Signer, signing_public_key
from levain.trust_anchor import _VERIFY_ENV, DEFAULT_SSH_KEYGEN, trust_anchor_problem

__all__ = ["AnswerClass", "Entry", "KEY_LABEL", "MessageError", "NAMESPACE", "FORMAT", "ParsedMessage",
           "REASONS", "SignOutcome", "StatusReport", "TerminalKey", "VerifyResult", "answer_class", "build_message",
           "default_signer_factory", "find_key", "fresh", "is_remote", "parse_verified", "sign", "status", "verify"]

FORMAT = "levain-terminal-answer/1"
NAMESPACE = "levain-terminal-answer"
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
_CTRL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")


class MessageError(ValueError):
    """A message that cannot be built, or bytes that are not a ``levain-terminal-answer/1`` message."""


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


def _line(value: Any, what: str, *, limit: int = LINE_MAX) -> str:
    if not isinstance(value, str) or not value:
        raise MessageError(f"{what} is empty or not text")
    if _CTRL.search(value):
        raise MessageError(f"{what} holds a line break or control character")
    if len(value) > limit:
        raise MessageError(f"{what} is longer than {limit} characters")
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
    signed). ``nonce`` and ``at`` default to fresh values; tests pass them to pin the bytes. Raises
    :class:`MessageError` on a field that is out of form, no entries, or a message over :data:`MAX_BYTES`."""
    entries = tuple(entries)
    if not entries:
        raise MessageError("a message answers at least one entry")
    typed = unicodedata.normalize("NFC", _line(typed, "typed"))
    nonce = secrets.token_hex(8) if nonce is None else nonce
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
    if not value or (rx is not None and rx.fullmatch(value) is None) or _CTRL.search(value):
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
        if i < len(lines) and lines[i].startswith("  note: "):
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
    return ParsedMessage(store, install, session, view, typed_sha, typed, nonce, at, tuple(entries), counter,
                         hashlib.sha256(bytes(signed)).hexdigest())


def fresh(parsed: ParsedMessage, event_ts: datetime) -> bool:
    """`at` lies within :data:`STALE_S` before the resolve event, never after it. A bound on staleness only."""
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


def find_key(key_dir: Path | None = None) -> TerminalKey | None:
    """The enrolled terminal key, found by the fixed label :data:`KEY_LABEL` in the key directory, or None when
    there is none with a ``touch-id`` sidecar. Read-only: nothing is created. A directory open to other users or
    owned by someone else, or a sidecar that is there but malformed, raises ``KeyError_``. ``key_dir`` is for
    tests; the default is the real home's, whatever the environment says."""
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
    if not path.exists() or not (d / (KEY_LABEL + ".pub")).exists():
        return None
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
    """``terminal-key status``: read-only (``sc_auth list-ctk-identities`` and a look at the key directory)."""
    try:
        key = find_key(key_dir)
    except KeyError_ as exc:
        return StatusReport("warning", f"WARNING: the terminal key cannot be read: {exc}")
    if key is None:
        return StatusReport("not enrolled", "not enrolled: answers at the terminal are recorded unsigned")
    try:
        fp = _keys.fingerprint_of(key.public_key)
        ok, note = ctk_status(key.public_key, sc_auth=sc_auth, identities=identities, platform=platform)
    except (KeyError_, VerifierUnavailable) as exc:
        return StatusReport("warning", f"WARNING: the terminal key's identity cannot be checked: {exc}", key.public_key)
    if ok is True:
        return StatusReport("enrolled", f"terminal key: enrolled ({fp}), {note}", key.public_key)
    return StatusReport("warning", f"WARNING: terminal key ({fp}): {note}", key.public_key)


# ---------------------------------------------------------------------------------------------------
# signing


@dataclass(frozen=True)
class SignOutcome:
    state: str                       # "signed" | "unsigned"
    reason: str | None = None        # one of REASONS when unsigned
    detail: str = ""
    signature: str | None = None
    fingerprint: str | None = None


SignerFactory = Callable[[Path], tuple[str, Signer]]


def default_signer_factory(key_path: Path) -> tuple[str, Signer]:
    """The one door to the real key: ``ssh_signer`` in :data:`NAMESPACE`. Calling the ``Signer`` it returns
    raises the Touch ID prompt."""
    return _keys.ssh_signer(key_path, namespace=NAMESPACE)


def is_remote(env: Mapping[str, str] | None = None, presence: str | None = None) -> bool:
    """Routing only (never evidence): the desk is remote when the presence says so or the hook runs in an ssh
    session. A false "local" costs a prompt nobody touches, which times out."""
    env = os.environ if env is None else env
    return bool(presence and "remote" in presence.lower()) or bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY"))


_bound_lock = threading.Lock()


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
    """Sign ``message`` with the terminal key, or say exactly why not (:data:`REASONS`); never raises for an
    unavailable key. ``remote`` routes to ``unsigned/remote`` before any key is looked at, so no prompt reaches an
    unattended screen. The default factory is bounded to ``bound_s`` seconds by narrowing ``ssh_signer``'s own
    timeout for the duration of the call (process-wide, under a lock: the hook is a one-shot process)."""
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
        fp, signer = factory(key.path)
        with _bound_lock:
            old = _keys._SIGN_TIMEOUT
            _keys._SIGN_TIMEOUT = min(old, bound_s)
            try:
                signature = signer(bytes(message))
            finally:
                _keys._SIGN_TIMEOUT = old
    except KeyError_ as exc:
        reason, detail = _classify(exc)
        return SignOutcome("unsigned", reason, detail)
    except VerifierUnavailable as exc:
        return SignOutcome("unsigned", "signer-error", str(exc).splitlines()[0] if str(exc) else "verifier unavailable")
    except subprocess.TimeoutExpired:
        return SignOutcome("unsigned", "timeout", "the sign step ran out of time")
    except OSError as exc:
        return SignOutcome("unsigned", "signer-error", f"{type(exc).__name__}: {exc.strerror or exc}")
    return SignOutcome("signed", None, "", signature, fp)


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
           ctk: Callable[[str], tuple[bool | None, str]] | None = None, timeout: float = 10.0) -> VerifyResult:
    """Whether ``sig`` is an SSHSIG over ``message`` in :data:`NAMESPACE` by a key ``signers`` (the text of an
    allowed-signers file) lists for ``principal``. Stock ``ssh-keygen -Y verify``; the signers and the signature
    reach it through pipes. A FIDO signature must carry the user-presence flag. The key's live CTK status on this
    Mac (``ctk``, default :func:`ctk_status`) is read from the signature's own key: False refuses, None only
    weakens the claim. Raises :class:`VerifierUnavailable` when ssh-keygen cannot run (never a verdict)."""
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
                             "-n", NAMESPACE, "-s", f"/dev/fd/{fds[1]}"], input=bytes(message),
                            capture_output=True, timeout=timeout, env=dict(_VERIFY_ENV), pass_fds=tuple(fds))
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise VerifierUnavailable(f"ssh-keygen could not verify a terminal signature ({type(exc).__name__})") from None
    finally:
        for fd in fds:
            os.close(fd)
    if cp.returncode != 0:
        return _refuse("the signature does not verify for this principal in this namespace", flags=flags,
                       public_key=public_key)
    good = re.compile(r'Good "' + re.escape(NAMESPACE) + r'" signature for ' + re.escape(principal)
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
                        "signed: Touch ID, laptop")


# ---------------------------------------------------------------------------------------------------
# the classifier (DESIGN section 5), for Levain's own store


@dataclass(frozen=True)
class AnswerClass:
    state: str          # signed-terminal | unsigned-terminal | agent-resolved | legacy | failed
    signer_fp: str | None = None
    claim: str = ""
    detail: str = ""
    late: bool = False


@dataclass
class _Ctx:
    signers: str = ""
    principal: str = "phill"
    ssh_keygen: Path | str = DEFAULT_SSH_KEYGEN
    ctk: Callable[[str], tuple[bool | None, str]] | None = None
    countersigns: Sequence[Mapping[str, Any]] = field(default_factory=tuple)


def _terminal_signed(answer: Mapping[str, Any], row: Mapping[str, Any] | None, ctx: _Ctx,
                     event_ts: datetime | None) -> AnswerClass:
    """Good only if the signature verifies in the terminal namespace, the bytes parse, and the entry the event
    names is the row it is attached to (item, row_version, asked, and the answer or text it records)."""
    signed = answer.get("signed")
    if not isinstance(signed, Mapping) or signed.get("format") != FORMAT:
        return AnswerClass("failed", detail="not a terminal signature")
    message, sig = signed.get("message"), signed.get("sig")
    if not isinstance(message, str) or not isinstance(sig, str):
        return AnswerClass("failed", detail="the signed block carries no message and signature")
    raw = message.encode("utf-8")
    try:
        got = verify(raw, sig, ctx.signers, ctx.principal, ssh_keygen=ctx.ssh_keygen, ctk=ctx.ctk)
        parsed = parse_verified(raw)
    except MessageError as exc:
        return AnswerClass("failed", detail=f"signed bytes are not a terminal answer: {exc}")
    if not got.ok:
        return AnswerClass("failed", detail=got.problem)
    if signed.get("message_sha256") not in (None, parsed.message_sha256):
        return AnswerClass("failed", detail="message_sha256 is not the message's")
    n = signed.get("entry")
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(parsed.entries):
        return AnswerClass("failed", detail="the event names no entry of the signed message")
    entry = parsed.entries[n - 1]
    if row is None:
        return AnswerClass("failed", detail="no row to bind the entry to")
    if (entry.item != row.get("id") or entry.row_version != row.get("row_version")
            or entry.asked != row.get("what")):
        return AnswerClass("failed", detail="the signed entry is not this row (item, version or question)")
    if (answer.get("answer") is not None and answer.get("answer") != entry.answer) or \
            (answer.get("text") is not None and answer.get("text") != entry.text):
        return AnswerClass("failed", detail="the recorded answer is not the signed one")
    if event_ts is not None and not fresh(parsed, event_ts):
        return AnswerClass("failed", detail="the signature is not within ten minutes before the answer")
    return AnswerClass("signed-terminal", got.fingerprint, got.claim)


def answer_class(answer: Mapping[str, Any], row: Mapping[str, Any] | None = None, context: Mapping[str, Any] | None = None
                 ) -> AnswerClass:
    """The one function every reader of a Levain answer goes through. ``answer`` is the resolve event as stored
    (``signed`` / ``via`` / ``unsigned`` / ``answer`` / ``text`` / ``ts``); ``row`` the row it is attached to
    (``id``, ``row_version``, ``what``); ``context`` holds ``signers`` (allowed-signers text), ``principal``,
    ``event_ts`` (aware datetime), ``ctk``, ``ssh_keygen`` and ``countersigns`` (later events of the same shape
    with ``of_ts``). Order: a ``signed`` block that re-verifies, else a verifying countersign (marked late), else
    the labels, which never confer anything. No field a hook or CLI writes is read as evidence."""
    c = dict(context or {})
    ctx = _Ctx(c.get("signers", ""), c.get("principal", "phill"), c.get("ssh_keygen", DEFAULT_SSH_KEYGEN),
               c.get("ctk"), tuple(c.get("countersigns") or ()))
    ts = c.get("event_ts")
    if answer.get("signed") is not None:
        return _terminal_signed(answer, row, ctx, ts)
    for cs in ctx.countersigns:
        if cs.get("of_ts") != answer.get("ts") or cs.get("signed") is None:
            continue
        got = _terminal_signed({**cs, "answer": answer.get("answer"), "text": answer.get("text")}, row, ctx, None)
        if got.state == "signed-terminal":
            return AnswerClass(got.state, got.signer_fp, got.claim, "countersigned later", late=True)
    via = answer.get("via")
    if via == "terminal":
        un = answer.get("unsigned")
        reason = un.get("reason") if isinstance(un, Mapping) else None
        return AnswerClass("unsigned-terminal", detail=str(reason) if reason else "")
    if via:
        return AnswerClass("agent-resolved")
    return AnswerClass("legacy", detail="unsigned (before signing existed)")
