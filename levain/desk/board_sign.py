"""levain.desk.board_sign — operator-signed board records (P2): the signature, the verifier and the fixed 1.0 policy.

The signature. The bytes signed are :func:`signing_bytes` of a record: ``board_store.canonical_bytes`` of the record WITHOUT its
``sig`` key. ``prev`` and ``grade`` are inside them, so a signature also commits to the record's place in the chain and to the
grade it claims. The SSHSIG is made under the namespace :data:`NAMESPACE` and rides in the record as
``sig: {"sshsig": <armored>, "fingerprint": <fp>, "via": "terminal"|"phone"}``, the last key of the record.

The grade is the writer's. ``signed-terminal`` or ``signed-phone`` is derived from ``via`` here and nowhere else; a caller's
body carrying ``sig`` or ``grade`` is refused by ``board_store.build_record``. ``author.kind`` must be ``operator`` on a signed
record.

Two ways to sign, both reaching the store through one ``seal`` hook that ``board_store._append`` calls UNDER the exclusive
lock, after ``prev`` is stamped (``prev`` is known only there):

* :func:`terminal_seal`: Touch ID through ``levain.consent.terminal_sign`` (its key finder, its ``sign``, its remote and agent
  session refusals). A refused, cancelled or failed signature raises :class:`~levain.desk.board_store.BoardError` and nothing
  is appended.
* :func:`prepare` then :func:`commit_phone`: the cockpit's passkey ceremony signs the exact bytes ``prepare`` returns (the
  door that mints that signature is not here), and ``commit_phone`` re-builds the record under the lock, refuses if the chain
  head moved or the bytes differ from what was signed, verifies the signature against the authority log's keys, and appends.

:func:`verify_record` is pure over ``(record, authority snapshot)``. :func:`require_policy` is the one table of the fixed 1.0
policy; the writer calls it before it signs or writes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from levain.desk import board_store as bs

__all__ = [
    "ASSERTED_OK", "DECISION_CLASS", "NAMESPACE", "POLICY", "SIGNED_GRADES", "Prepared", "Seal", "Verdict", "commit_phone",
    "prepare", "require_policy", "signing_bytes", "terminal_seal", "verify_record",
]

NAMESPACE = "levain-board"
VIAS = ("terminal", "phone")
VIA_GRADE = {"terminal": "signed-terminal", "phone": "signed-phone"}
SIGNED_GRADES = tuple(VIA_GRADE.values())
GRADE_RELAYED = "relayed"                 # P3's grade: this module accepts the value, it never writes it
GRADE_LEGACY = "legacy"
CHAIN_MOVED = "the board changed while you signed: sign again"
SIG_KEYS = ("sshsig", "fingerprint", "via")

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


def _cause_problem(record: Mapping[str, Any], lines: Sequence[bytes | str]) -> str | None:
    """Why ``record``'s ``cause`` is not the sha256 (of the line as written) of a signed or relayed ``abandon-track`` of the
    same repo ``M`` as the item (an item ``M#n`` is keyed to ``M``), or None when it is."""
    cause = record.get("cause")
    if not isinstance(cause, str) or not bs.HASH_RE.fullmatch(cause):
        return "cause must be the lowercase sha256 of an abandon-track record"
    item_repo = str(record.get("id", "")).rpartition("#")[0]
    for line in lines:
        if _record_hash(line) != cause:
            continue
        try:
            held = json.loads(line)
        except ValueError:
            return "cause names a line that is not a record"
        if not isinstance(held, dict) or held.get("op") != "abandon-track":
            return "cause names a record that is not an abandon-track"
        if held.get("grade") not in SIGNED_GRADES + (GRADE_RELAYED,):
            return f"cause names an abandon-track graded {held.get('grade')!r}: it must be signed or relayed"
        if held.get("repo") != item_repo:
            return f"cause names an abandon-track of {held.get('repo')!r}, and {record.get('id')} is keyed to {item_repo!r}"
        return None
    return "cause names no abandon-track record in the store"


def require_policy(op: str, grade: str, record: Mapping[str, Any] | None = None, *,
                   lines: Sequence[bytes | str] = ()) -> None:
    """Refuse (:class:`~levain.desk.board_store.BoardError`) a record the fixed 1.0 policy does not accept at ``grade``. An op
    not in :data:`POLICY` is refused (fail closed). A decision-class op needs ``signed-*`` or ``relayed``; the one exception
    (P2-d) is a ``decide`` of ``cut`` carrying a ``cause`` that is the sha256 of a signed or relayed ``abandon-track`` of the
    item's own repo among ``lines`` (every store line, as written: the abandon's repo file and the items file).
    ``cause`` is refused anywhere else: on a non-cut decide, or on any other op."""
    record = record or {}
    if op not in POLICY:
        raise bs.BoardError(f"op {op!r} is not in the board's write policy")
    if "cause" in record and not (op == "decide" and record.get("decision") == "cut"):
        raise bs.BoardError("cause belongs to a decide of cut only")
    if POLICY[op] == ASSERTED_OK:
        return
    if grade in SIGNED_GRADES or grade == GRADE_RELAYED:
        return
    if op == "decide" and record.get("decision") == "cut" and "cause" in record:
        problem = _cause_problem(record, lines)
        if problem is None:
            return
        raise bs.BoardError(f"{_refusal(op)}; the cause exception does not hold: {problem}")
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
    """What a store verb takes as ``seal``: the grade it earns (from ``via``, never from a caller), a ``preflight`` that runs
    BEFORE the lock, and ``__call__(record) -> record`` that runs UNDER the exclusive lock on the chained record."""

    via = ""

    @property
    def grade(self) -> str:
        return VIA_GRADE[self.via]

    def preflight(self) -> None:
        """Refuse before the lock is taken. Default: nothing."""

    def __call__(self, record: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


class _TerminalSeal(Seal):
    via = "terminal"

    def __init__(self, key_dir=None, signer_factory=None, remote: Callable[[], str | None] | None = None,
                 in_agent_session: Callable[[], bool] | None = None, bound_s: float | None = None):
        self._key_dir, self._factory, self._remote, self._agent, self._bound = key_dir, signer_factory, remote, in_agent_session, bound_s
        self._key = None
        self._why: str | None = None

    def preflight(self) -> None:
        from levain.consent import terminal_sign as ts
        from levain.cockpit.keys import KeyError_
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


def terminal_seal(*, key_dir=None, signer_factory=None, remote=None, in_agent_session=None, bound_s=None) -> Seal:
    """The Touch ID seal. Every argument is for tests (a scratch key directory, an injected signer factory
    ``(key path, bound) -> (fingerprint, sign)``, a remote check, an agent-session check); the defaults are the real ceremony.
    The signer is ``cockpit.keys.ssh_signer`` in :data:`NAMESPACE`, the same door ``terminal_sign`` uses for its own."""
    return _TerminalSeal(key_dir, signer_factory, remote, in_agent_session, bound_s)


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
    ``AuthorityKey``) only. A revoked key's signature is reported as signed by a revoked key, never silently valid and never
    erased (the record's own time is the signer's claim, so no "valid at its time" is asserted). Raises
    ``VerifierUnavailable`` when ssh-keygen cannot run: that is no verdict on the record."""
    from levain.authority import DISPLAY_PHONE, LAPTOP, quarantined
    from levain.authority_sig import verify_link_sig
    grade = record.get("grade")
    sig = record.get("sig")
    if grade == GRADE_LEGACY or ("grade" not in record and "prev" not in record and sig is None):
        return Verdict("legacy", "legacy")
    if grade == GRADE_RELAYED and sig is None:
        return Verdict("relayed", "relayed: the citation is checked by the trail verifier")
    if grade not in SIGNED_GRADES:
        if sig is not None:
            return _bad(f"a record graded {grade!r} carries a signature")
        author = record.get("author") if isinstance(record.get("author"), Mapping) else {}
        return Verdict("asserted", f"asserted {author.get('kind', '?')} {author.get('name', '?')}, unsigned")
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

    def __call__(self, record: dict) -> dict:
        raise _Captured(record)


def _verb(name: str) -> Callable[..., Any]:
    verbs = {"add": bs.add, "amend": bs.amend, "decide": bs.decide}
    if name not in verbs:
        raise bs.BoardError(f"{name!r} has no store verb to sign here ({', '.join(verbs)})")
    return verbs[name]


def prepare(target: bs.Target, verb: str, **kwargs: Any) -> Prepared:
    """Build, under the lock and without writing, the record ``verb`` would write now, with grade ``signed-phone`` and its
    ``prev``; the caller hands ``message`` to the passkey ceremony. ``kwargs`` are the verb's own arguments (``author`` must be
    the operator). ``now`` is fixed here so the same bytes can be re-built. Nothing is written."""
    kwargs = {"now": bs._utc_now(), **kwargs}
    try:
        _verb(verb)(target, **kwargs, seal=_CaptureSeal())
    except _Captured as got:
        return Prepared(verb, kwargs, got.record, signing_bytes(got.record), got.record["prev"])
    raise bs.BoardError(f"{verb} would write nothing: there is nothing to sign")


class _PhoneSeal(Seal):
    via = "phone"

    def __init__(self, prepared: Prepared, sshsig: str, snapshot: Any):
        self._p, self._sig, self._snapshot = prepared, sshsig, snapshot

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
        sealed = _attach(record, self._sig, fp, "phone")
        verdict = verify_record(sealed, self._snapshot)
        if verdict.state != "valid":
            raise bs.BoardError(f"not appended: {verdict.text}")
        return sealed


def commit_phone(target: bs.Target, prepared: Prepared, sshsig: str, snapshot: Any) -> Any:
    """Append ``prepared``'s record with an externally produced SSHSIG over ``prepared.message``, graded ``signed-phone``.
    Under the lock the record is re-built; it is REFUSED (nothing written) when the chain head is no longer
    ``prepared.prev`` ("the board changed while you signed: sign again"), when its bytes differ from what was signed, or when
    the signature does not verify against ``snapshot``'s authority keys. Returns what the verb returns."""
    return _verb(prepared.verb)(target, **prepared.kwargs, seal=_PhoneSeal(prepared, sshsig, snapshot))
