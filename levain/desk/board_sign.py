"""levain.desk.board_sign — operator-signed board records (P2): the signature, the verifier and the fixed 1.0 policy.

The signature. The bytes signed are :func:`signing_bytes` of a record: ``board_store.canonical_bytes`` of the record WITHOUT its
``sig`` key. ``prev`` and ``grade`` are inside them, so a signature also commits to the record's place in the chain and to the
grade it claims. The SSHSIG is made under the namespace :data:`NAMESPACE` and rides in the record as
``sig: {"sshsig": <armored>, "fingerprint": <fp>, "via": "terminal"|"phone"}``, the last key of the record.

The grade is the writer's, and it is EARNED by a check, never by a label. A seal's ``via`` only says which grade the record
claims; :func:`admit` (which ``board_store._append`` calls on every sealed record, under the lock, after the ceremony and
before the write) replays the authority log at that moment from the seal's authority SOURCE and refuses the write unless
:func:`verify_record` says ``valid``: an unenrolled, revoked, quarantined or wrong-class key writes nothing. A write path
with no authority log to read is refused: a signature nobody can verify is not a signed grade. A caller's body carrying
``sig`` or ``grade`` is refused by ``board_store.build_record``. ``author.kind`` must be ``operator`` on a signed record.

Two ways to sign, both reaching the store through one ``seal`` hook that ``board_store._append`` calls UNDER the exclusive
lock, after ``prev`` is stamped (``prev`` is known only there):

* :func:`terminal_seal`: Touch ID through ``levain.consent.terminal_sign`` (its key finder, its ``sign``, its remote and agent
  session refusals). A refused, cancelled or failed signature raises :class:`~levain.desk.board_store.BoardError` and nothing
  is appended. If the store file moved during the prompt the write is refused ("the board changed while you signed: sign
  again"); the prompt is never shown twice for one write.
* :func:`prepare` then :func:`commit_phone`: the cockpit's passkey ceremony signs the exact bytes ``prepare`` returns (the
  door that mints that signature is not here), and ``commit_phone`` re-builds the record under the lock and refuses if the
  chain head moved or the bytes differ from what was signed; :func:`admit` then verifies it against the authority log
  replayed after the ceremony.

The cause (P2-d). A ``decide`` of ``cut`` may carry ``cause``: the lowercase hex sha256 of the cited ``abandon-track`` line's
bytes exactly as written in the store file, without its trailing newline (the same byte range the chain's ``prev`` hashes).
:func:`verify_cause` holds it: the cited record is an ``abandon-track`` whose signature verifies ``valid`` against the
authority log (a ``relayed`` one is refused until P3's verifier lands), of a migration ``M`` the item is keyed to (its ``add``
carries key ``M`` or ``M:...``). A present cause is checked on every decide, signed or not.

:func:`verify_record` is pure over ``(record, authority snapshot)``. :func:`require_policy` is the one table of the fixed 1.0
policy; the writer calls it before it signs or writes.

Known edges:
* the cause check reads ``repos.jsonl`` under the items lock; a process that REPLACES that file mid-write is not detected
  (the store dir is not a git checkout).
* a cited line nested deeper than the JSON parser's recursion limit: the cause check refuses it as "not a record"
  (RecursionError is caught here, never a crash).
* an idempotent ``add --sign`` that writes nothing still needs a findable terminal key and an authority log (preflight runs
  before the store is read).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from levain.desk import board_store as bs

__all__ = [
    "ASSERTED_OK", "DECISION_CLASS", "NAMESPACE", "POLICY", "SIGNED_GRADES", "Prepared", "Seal", "Verdict", "admit",
    "commit_phone", "prepare", "require_policy", "resolve_authority", "signing_bytes", "terminal_seal", "verify_cause",
    "verify_record",
]

NAMESPACE = "levain-board"
VIAS = ("terminal", "phone")
VIA_GRADE = {"terminal": "signed-terminal", "phone": "signed-phone"}
SIGNED_GRADES = tuple(VIA_GRADE.values())
GRADE_RELAYED = "relayed"                 # P3's grade: this module accepts the value, it never writes it
GRADE_LEGACY = "legacy"
CHAIN_MOVED = bs.SIGNED_CHAIN_MOVED
SIG_KEYS = ("sshsig", "fingerprint", "via")
NO_AUTHORITY = ("there is no authority log to verify a signature against, and a signature nobody can verify is not a "
                "signed grade: nothing was written")

AuthoritySource = Callable[[], Any]

# ------------------------------------------------------------------------------------------------------------ authority


def resolve_authority(*, data_root: Path | str | None = None, queue_dir: Path | str | None = None) -> AuthoritySource:
    """The authority SOURCE a board signature is verified against
    (``levain.consent.queue.authority_source`` over a cockpit's ``cockpit.db`` and its anchor), at ``data_root``, else the one
    cockpit the consent hooks in ``queue_dir`` (default: this process's consent queue) name. Calling the source replays the
    log NOW; the log itself is not read here. Refused (BoardError) when there is no single cockpit holding a log."""
    from levain.authority import anchor_path
    from levain.consent.answering import hook_roots
    from levain.consent.queue import authority_source, default_queue_dir

    if data_root is not None:
        root = Path(data_root).expanduser()
    else:
        roots = hook_roots(Path(queue_dir) if queue_dir is not None else default_queue_dir())
        if len(roots) != 1:
            named = "none" if not roots else f"{len(roots)}"
            raise bs.BoardError(f"{NO_AUTHORITY} (the consent hooks here name {named} cockpits; one must name the cockpit "
                                f"whose authority log holds the operator's key)")
        root = roots[0]
    if not (root / "cockpit.db").is_file():
        raise bs.BoardError(f"{NO_AUTHORITY} ({root} holds no cockpit.db)")
    return authority_source(root / "cockpit.db", anchor=anchor_path(root))


def read_authority(source: AuthoritySource | None) -> Any:
    """Replay ``source`` now. No source, or a log that cannot be read or verified now, is a refusal (BoardError)."""
    from levain.authority import AuthorityError
    if source is None:
        raise bs.BoardError(NO_AUTHORITY)
    if not callable(source):
        raise bs.BoardError("an authority SOURCE is needed (replayed at the moment of the write), not a snapshot")
    try:
        return source()
    except (AuthorityError, sqlite3.Error, OSError) as exc:          # VerifierUnavailable is an AuthorityError
        raise bs.BoardError(f"the authority log cannot be read now ({exc}): nothing was written") from None


# ------------------------------------------------------------------------------------------------------------- policy

DECISION_CLASS = "decision"
ASSERTED_OK = "asserted-ok"
#: THE fixed 1.0 policy, keyed by op. Decision-class ops need a signed or relayed record; every other op accepts ``asserted``.
#: The registry ops are listed though their verbs are not built on every base: a verb that writes one calls
#: :func:`require_policy`. The operator setting this per verb is 1.1 and is not here.
POLICY: dict[str, str] = {
    "decide": DECISION_CLASS, "begin-track": DECISION_CLASS, "track": DECISION_CLASS, "abandon-track": DECISION_CLASS,
    "untrack": DECISION_CLASS,
    "add": ASSERTED_OK, "amend": ASSERTED_OK, "register": ASSERTED_OK, "pause": ASSERTED_OK, "resume": ASSERTED_OK,
    "move": ASSERTED_OK, "ack-torn": ASSERTED_OK, "abandon-done": ASSERTED_OK,
}


def _refusal(op: str) -> str:
    return (f"{op} is a decision-class write: it needs --sign (the operator's Touch ID), or --source <np-id> relaying his "
            f"answer; an asserted record is refused")


def _record_hash(line: bytes | str) -> str:
    raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    return hashlib.sha256(raw).hexdigest()


def _parse(line: bytes | str) -> Any:
    try:
        return json.loads(line)
    except (ValueError, RecursionError):
        return None


def _keyed_to(item_key: Any, migration: str) -> bool:
    return isinstance(item_key, str) and (item_key == migration or item_key.startswith(migration + ":"))


def verify_cause(record: Mapping[str, Any], lines: Sequence[bytes | str], snapshot: Any) -> str | None:
    """Why ``record``'s ``cause`` does not hold, or None when it does. It holds when ``cause`` is the sha256 of one of
    ``lines`` (every store line as written, no trailing newline), that line is an ``abandon-track`` whose signature
    :func:`verify_record` finds ``valid`` against ``snapshot``, and the item ``record`` decides was added with key ``M`` or
    ``M:...``, where ``M`` is the abandon's ``migration``. A ``relayed`` abandon is refused: its citation is P3's verifier's
    to check and that verifier is not on this base. Raises ``VerifierUnavailable`` when ssh-keygen cannot run."""
    cause = record.get("cause")
    if not isinstance(cause, str) or not bs.HASH_RE.fullmatch(cause):
        return "cause must be the lowercase sha256 of an abandon-track record"
    held = None
    for line in lines:
        if _record_hash(line) == cause:
            held = _parse(line)
            if not isinstance(held, dict):
                return "cause names a line that is not a record"
            break
    else:
        return "cause names no abandon-track record in the store"
    if held.get("op") != "abandon-track":
        return "cause names a record that is not an abandon-track"
    grade = held.get("grade")
    if grade == GRADE_RELAYED:
        return ("cause names a relayed abandon-track: a relayed citation is checked by P3's verifier, which this build "
                "does not have, so it is refused, never trusted")
    if grade not in SIGNED_GRADES:
        return f"cause names an abandon-track graded {grade!r}: it must be signed"
    if snapshot is None:
        return "cause names a signed abandon-track and there is no authority log to verify it against"
    verdict = verify_record(held, snapshot)
    if verdict.state != "valid":
        return f"the cited abandon-track's signature is not valid: {verdict.text}"
    migration = held.get("migration")
    if not isinstance(migration, str) or not migration:
        return "the cited abandon-track names no migration"
    item_id = record.get("id")
    added = next((r for r in map(_parse, lines) if isinstance(r, dict) and r.get("op") == "add" and r.get("id") == item_id),
                 None)
    if added is None:
        return f"{item_id} has no add record in the store"
    if not _keyed_to(added.get("key"), migration):
        return f"cause names an abandon-track of migration {migration!r}, and {item_id} is not keyed to it"
    return None


def require_policy(op: str, grade: str, record: Mapping[str, Any] | None = None, *,
                   lines: Sequence[bytes | str] = (), snapshot: Any = None) -> None:
    """Refuse (:class:`~levain.desk.board_store.BoardError`) a record the fixed 1.0 policy does not accept at ``grade``. An op
    not in :data:`POLICY` is refused (fail closed). ``grade`` is the WRITER's (a signed grade is then earned by :func:`admit`;
    ``relayed`` is P3's). A decision-class op needs ``signed-*`` or ``relayed``; the one exception (P2-d) is a ``decide`` of
    ``cut`` whose ``cause`` holds (:func:`verify_cause` over ``lines``, every store line as written, and ``snapshot``). A
    present ``cause`` is checked whatever the grade, and refused on anything but a cut."""
    from levain.authority_sig import VerifierUnavailable
    record = record or {}
    if op not in POLICY:
        raise bs.BoardError(f"op {op!r} is not in the board's write policy")
    signed = grade in SIGNED_GRADES or grade == GRADE_RELAYED
    if "cause" in record:
        if not (op == "decide" and record.get("decision") == "cut"):
            raise bs.BoardError("cause belongs to a decide of cut only")
        try:
            problem = verify_cause(record, lines, snapshot)
        except VerifierUnavailable as exc:
            raise bs.BoardError(f"the cause cannot be checked now ({exc}): nothing was written") from None
        if problem is not None:
            raise bs.BoardError((f"{_refusal(op)}; the cause exception does not hold: " if not signed
                                 else "the cause does not hold: ") + problem)
        return
    if POLICY[op] == ASSERTED_OK or signed:
        return
    raise bs.BoardError(_refusal(op))


def check_signable(record: Mapping[str, Any]) -> None:
    """A signed record's author is the operator: refuse anything else before any prompt."""
    author = record.get("author")
    kind = author.get("kind") if isinstance(author, Mapping) else None
    if kind != "operator":
        raise bs.BoardError(f"a signed record is the operator's: author kind must be operator, not {kind!r} "
                            f"(--author-kind operator)")


# --------------------------------------------------------------------------------------------------------------- bytes


def signing_bytes(record: Mapping[str, Any]) -> bytes:
    """The bytes signed: ``canonical_bytes`` of ``record`` without its ``sig`` key (``prev`` and ``grade`` included)."""
    return bs.canonical_bytes({k: v for k, v in record.items() if k != "sig"})


def _attach(record: Mapping[str, Any], sshsig: str, fingerprint: str, via: str) -> dict:
    return {**{k: v for k, v in record.items() if k != "sig"},
            "sig": {"sshsig": sshsig, "fingerprint": fingerprint, "via": via}}


# --------------------------------------------------------------------------------------------------------------- seals


class Seal:
    """What a store verb takes as ``seal``: the grade it CLAIMS (from ``via``, never from a caller), the authority SOURCE
    :func:`admit` replays after the ceremony, a ``preflight`` that runs BEFORE the lock, and ``__call__(record) -> record``
    that runs UNDER the exclusive lock on the chained record. A seal's output is never trusted: :func:`admit` verifies it."""

    via = ""
    authority: AuthoritySource | None = None

    @property
    def grade(self) -> str:
        return VIA_GRADE[self.via]

    def preflight(self) -> None:
        """Refuse before the lock is taken. Default: nothing."""

    def __call__(self, record: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


def admit(target: bs.Target, seal: Seal, record: dict) -> dict:
    """Run ``seal`` on the chained ``record`` and earn its grade: called by ``board_store._append`` under the exclusive lock,
    right before the write. After the ceremony the authority log is replayed from ``seal.authority`` (never a snapshot
    taken earlier); the write is REFUSED (BoardError, nothing written) unless the sealed record is ``record`` plus a ``sig``
    and :func:`verify_record` finds it ``valid``, and, when it carries a ``cause``, :func:`verify_cause` holds against the
    same replay."""
    from levain.authority_sig import VerifierUnavailable
    sealed = seal(record)
    if not isinstance(sealed, dict) or {k: v for k, v in sealed.items() if k != "sig"} != record or "sig" not in sealed:
        raise bs.BoardError("the seal returned a record other than the one built: nothing was written")
    snapshot = read_authority(seal.authority)
    try:
        verdict = verify_record(sealed, snapshot)
        problem = (verify_cause(sealed, bs._store_lines(target), snapshot) if "cause" in sealed else None)
    except VerifierUnavailable as exc:
        raise bs.BoardError(f"the signature cannot be checked now ({exc}): nothing was written") from None
    if verdict.state != "valid":
        raise bs.BoardError(f"not written: {verdict.text}")
    if problem is not None:
        raise bs.BoardError(f"not written: the cause does not hold: {problem}")
    return sealed


class _TerminalSeal(Seal):
    via = "terminal"

    def __init__(self, key_dir=None, signer_factory=None, remote: Callable[[], str | None] | None = None,
                 in_agent_session: Callable[[], bool] | None = None, bound_s: float | None = None,
                 authority: AuthoritySource | None = None):
        self._key_dir, self._factory, self._remote, self._agent, self._bound = key_dir, signer_factory, remote, in_agent_session, bound_s
        self.authority = authority
        self._key = None
        self._why: str | None = None

    def preflight(self) -> None:
        from levain.consent import terminal_sign as ts
        from levain.cockpit.keys import KeyError_
        if self.authority is None:
            raise bs.BoardError(NO_AUTHORITY)
        agent = self._agent
        if agent is None:
            from levain.consent.band import agent_session_caller as agent
        if agent():
            raise bs.BoardError(ts.session_refusal())
        try:
            self._key = ts.find_key(self._key_dir)
        except KeyError_ as exc:
            raise bs.BoardError(f"the terminal key cannot be used: {exc}") from None
        if self._key is None:
            raise bs.BoardError("no terminal key with a touch-id sidecar: nothing was written (levain terminal-key create)")
        self._why = (self._remote or ts.remote_reason)()            # a process-table read: kept out of the lock

    def __call__(self, record: dict) -> dict:
        from levain.consent import terminal_sign as ts
        from levain.cockpit import keys as _keys
        if self._key is None:
            self.preflight()
        why = self._why
        factory = self._factory or (lambda path, bound: _keys.ssh_signer(
            _keys.refuse_real_key_in_tests(path, "the signing key"), namespace=NAMESPACE, timeout=bound))
        out = ts.sign(signing_bytes(record), key=self._key, signer_factory=factory, remote=why is not None, remote_why=why,
                      **({} if self._bound is None else {"bound_s": self._bound}))
        if out.state != "signed" or not out.signature or not out.fingerprint:
            raise bs.BoardError(f"not signed ({out.reason}: {out.detail}): nothing was written")
        return _attach(record, out.signature, out.fingerprint, "terminal")


def terminal_seal(*, authority: AuthoritySource | None = None, key_dir=None, signer_factory=None, remote=None,
                  in_agent_session=None, bound_s=None) -> Seal:
    """The Touch ID seal. ``authority`` is the authority SOURCE (:func:`resolve_authority`) replayed after the prompt; with
    none the write is refused before any prompt. The other arguments are for tests (a scratch key directory, an injected
    signer factory ``(key path, bound) -> (fingerprint, sign)``, a remote check, an agent-session check); the defaults are the
    real ceremony. The signer is ``cockpit.keys.ssh_signer`` in :data:`NAMESPACE`, the same door ``terminal_sign`` uses."""
    return _TerminalSeal(key_dir, signer_factory, remote, in_agent_session, bound_s, authority)


# ----------------------------------------------------------------------------------------------------------- verifying


@dataclass(frozen=True)
class Verdict:
    """``state``: ``valid`` | ``revoked`` | ``bad`` | ``asserted`` | ``legacy`` | ``relayed`` (P3's to judge). ``text`` is the
    sentence P6 prints. Only ``valid`` makes ``author.kind: operator`` a verified fact."""
    state: str
    text: str
    fingerprint: str | None = None
    revoked_seq: int | None = None


def _bad(why: str = "") -> Verdict:
    return Verdict("bad", "signature does not verify" + (f" ({why})" if why else ""))


def verify_record(record: Mapping[str, Any], snapshot: Any) -> Verdict:
    """The verdict on one record, from the record and an authority snapshot (``snapshot.keys``, fingerprint to
    ``AuthorityKey``) only. Exact grades only: a pre-cutover record (no ``grade``, no ``prev``) or one graded ``legacy`` is
    ``legacy``; ``asserted`` is ``asserted``; ``relayed`` is P3's to judge; ``signed-*`` is verified; any other grade, and a
    ``sig`` on anything but a signed grade, is ``bad``. A revoked key's signature is reported as signed by a revoked key,
    never silently valid and never erased (the record's own time is the signer's claim, so no "valid at its time" is
    asserted). Raises ``VerifierUnavailable`` when ssh-keygen cannot run: that is no verdict on the record."""
    from levain.authority import DISPLAY_PHONE, LAPTOP, quarantined
    from levain.authority_sig import verify_link_sig
    grade = record.get("grade")
    has_sig = "sig" in record
    if grade not in SIGNED_GRADES and has_sig:
        return _bad(f"a record graded {grade!r} carries a signature")
    if ("grade" not in record and "prev" not in record) or grade == GRADE_LEGACY:
        return Verdict("legacy", "legacy")
    if grade == GRADE_RELAYED:
        return Verdict("relayed", "relayed: the citation is checked by the trail verifier")
    if grade == bs.GRADE_ASSERTED:
        author = record.get("author") if isinstance(record.get("author"), Mapping) else {}
        return Verdict("asserted", f"asserted {author.get('kind', '?')} {author.get('name', '?')}, unsigned")
    if grade not in SIGNED_GRADES:
        return _bad(f"{grade!r} is not a grade")
    sig = record.get("sig")
    if not isinstance(sig, Mapping) or set(sig) != set(SIG_KEYS):
        return _bad("the record claims a signature it does not carry")
    via, fp, armored = sig["via"], sig["fingerprint"], sig["sshsig"]
    if via not in VIAS or VIA_GRADE[via] != grade or not isinstance(fp, str) or not isinstance(armored, str):
        return _bad("the signature's via does not match the grade")
    author = record.get("author")
    if not isinstance(author, Mapping) or author.get("kind") != "operator":
        return _bad("a signed record's author must be the operator")
    key = snapshot.keys.get(fp)
    if key is None:
        return _bad(f"{fp} is not a key in the authority log")
    if key.key_class != (LAPTOP if via == "terminal" else DISPLAY_PHONE):
        return _bad(f"{fp} is not a {via} key")
    if quarantined([key]):
        return _bad(f"{fp} is quarantined")
    if not verify_link_sig(signing_bytes(record), armored, public_key=key.public_key, fingerprint=fp, namespace=NAMESPACE):
        return _bad()
    if key.revoked_seq is not None:
        when = getattr(key, "revoked_at", None)           # the authority log does not carry a time on a key: usually unknown
        return Verdict("revoked", f"signed by {fp}, revoked at seq {key.revoked_seq}" + (f" ({when})" if when else "")
                       + "; the record's own time is the signer's claim", fp, key.revoked_seq)
    return Verdict("valid", f"{grade} by {fp}, valid", fp)


# ------------------------------------------------------------------------------------------------------ the phone half


@dataclass(frozen=True)
class Prepared:
    """A record ready for an external signer: ``message`` is the exact bytes to sign (namespace :data:`NAMESPACE`), ``prev``
    the chain head those bytes commit to. ``verb`` and ``kwargs`` re-build the same record in :func:`commit_phone`."""
    verb: str
    kwargs: Mapping[str, Any] = field(repr=False)
    record: Mapping[str, Any] = field(repr=False)
    message: bytes
    prev: str


class _Captured(Exception):
    def __init__(self, record: dict):
        self.record = record


class _CaptureSeal(Seal):
    via = "phone"

    def __init__(self, authority: AuthoritySource | None):
        self.authority = authority

    def __call__(self, record: dict) -> dict:
        raise _Captured(record)


def _verb(name: str) -> Callable[..., Any]:
    verbs = {"add": bs.add, "amend": bs.amend, "decide": bs.decide}
    if name not in verbs:
        raise bs.BoardError(f"{name!r} has no store verb to sign here ({', '.join(verbs)})")
    return verbs[name]


def prepare(target: bs.Target, verb: str, *, authority: AuthoritySource | None = None, **kwargs: Any) -> Prepared:
    """Build, under the lock and without writing, the record ``verb`` would write now, with grade ``signed-phone`` and its
    ``prev``; the caller hands ``message`` to the passkey ceremony. ``kwargs`` are the verb's own arguments (``author`` must be
    the operator; ``seal`` is refused). ``now`` is frozen here, after the arguments are merged (a ``now`` of None is replaced),
    so :func:`commit_phone` re-builds the same bytes. ``authority`` (a source) is required for a cut that carries a ``cause``: without it the build refuses with NO_AUTHORITY.
    Nothing is written."""
    if "seal" in kwargs:
        raise bs.BoardError("prepare makes its own seal: a seal argument is refused")
    kwargs = {**kwargs, "now": kwargs.get("now") or bs._utc_now()}
    try:
        _verb(verb)(target, **kwargs, seal=_CaptureSeal(authority))
    except _Captured as got:
        return Prepared(verb, kwargs, got.record, signing_bytes(got.record), got.record["prev"])
    raise bs.BoardError(f"{verb} would write nothing: there is nothing to sign")


class _PhoneSeal(Seal):
    via = "phone"

    def __init__(self, prepared: Prepared, sshsig: str, authority: AuthoritySource):
        self._p, self._sig, self.authority = prepared, sshsig, authority

    def __call__(self, record: dict) -> dict:
        if record["prev"] != self._p.prev:
            raise bs.BoardError(CHAIN_MOVED)
        if signing_bytes(record) != self._p.message:
            raise bs.BoardError("the record is not the one that was signed: sign again")
        from levain.authority_sig import sshsig_public_key
        try:
            fp = sshsig_public_key(self._sig)[1]
        except ValueError as exc:
            raise bs.BoardError(f"the signature is not an SSHSIG: {exc}") from None
        return _attach(record, self._sig, fp, "phone")


def commit_phone(target: bs.Target, prepared: Prepared, sshsig: str, authority: AuthoritySource) -> Any:
    """Append ``prepared``'s record with an externally produced SSHSIG over ``prepared.message``, graded ``signed-phone``.
    ``authority`` is an authority SOURCE (:func:`resolve_authority`, or a test's callable), replayed under the lock after the
    ceremony, never a snapshot the caller took before it (a snapshot is refused). Under the lock the record is re-built; it
    is REFUSED (nothing written) when the chain head is no longer ``prepared.prev`` ("the board changed while you signed:
    sign again"), when its bytes differ from what was signed, or when :func:`admit` does not find it valid. Returns what the
    verb returns."""
    if not callable(authority):
        raise bs.BoardError("commit_phone takes an authority SOURCE, replayed after the ceremony, not a snapshot")
    return _verb(prepared.verb)(target, **prepared.kwargs, seal=_PhoneSeal(prepared, sshsig, authority))
