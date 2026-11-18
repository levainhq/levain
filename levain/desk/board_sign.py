"""levain.desk.board_sign — operator-signed board records (P2): the signature, the verifier and the fixed 1.0 policy.

The signature. The bytes signed are :func:`signing_bytes` of a record: ``board_store.canonical_bytes`` of the record WITHOUT its
``sig`` key. ``prev`` and ``grade`` are inside them, so a signature also commits to the record's place in the chain and to the
grade it claims. The SSHSIG is made under the namespace :data:`NAMESPACE` and rides in the record as
``sig: {"sshsig": <armored>, "fingerprint": <fp>, "via": "terminal"|"phone"}``, the last key of the record.

The grade is the writer's, and it is EARNED by a check, never by a label. A seal's ``via`` only says which grade the record
claims; :func:`earn` (which ``board_store._append`` calls on every sealed record, under the lock, after the ceremony and
before the write) replays the authority log at that moment from the seal's authority SOURCE and refuses the write unless
:func:`verify_record` says ``valid``: an unenrolled, revoked, quarantined or wrong-class key writes nothing. A write path
with no authority log to read is refused: a signature nobody can verify is not a signed grade. A caller's body carrying
``sig`` or ``grade`` is refused by ``board_store.build_record``. ``author.kind`` must be ``operator`` on a signed record.

A signed record names its store: ``store`` (the store's ``store.id``) is in the signed bytes (a registry record carries it
already; ``board_store._graded`` puts it on a signed item record), and :func:`earn` refuses a ``store`` that is not the
store being written. A signature made for one store never lands in another, even one whose chain is byte-identical (two
empty item files have the same ``prev``).

Two ways to sign, both reaching the store through one ``seal`` hook. ``board_store._append`` builds and chains the record
under the exclusive lock (``prev`` is known only there), RELEASES the lock for the ceremony (:func:`ceremony`: a prompt is
human time, and no reader or other writer waits on it), then takes it again and refuses ("the board changed while you
signed: sign again") unless the store is exactly as it was; the prompt is never shown twice for one write.

* :func:`terminal_seal`: Touch ID through ``levain.consent.terminal_sign`` (its key finder, its ``sign``, its remote and agent
  session refusals; the remote check is made at the prompt, not before). A refused, cancelled or failed signature raises
  :class:`~levain.desk.board_store.BoardError` and nothing is appended.
* :func:`prepare` then :func:`commit_phone`: the cockpit's passkey ceremony signs the exact bytes ``prepare`` returns (the
  door that mints that signature is not here), and ``commit_phone`` refuses a store other than the one prepared for,
  re-builds the record under the lock and refuses if the chain head moved, the bytes differ from what was signed, or the
  verb would write nothing now (an idempotent ``add`` that another writer landed: nothing was signed); :func:`earn` then
  verifies it against the authority log replayed after the ceremony.

The cause (P2-d). A ``decide`` of ``cut`` may carry ``cause``: the lowercase hex sha256 of the cited ``abandon-track`` line's
bytes exactly as written in the store file, without its trailing newline (the same byte range the chain's ``prev`` hashes).
:func:`verify_cause` holds it: the cited record is an ``abandon-track`` whose signature verifies ``valid`` against the
authority log (a ``relayed`` one is refused until P3's verifier lands), a line of THIS store's ``repos.jsonl`` naming this
store, at the place in the registry chain its signed ``prev`` names, in a registry that is not torn, of a migration ``M`` the
item is keyed to (its ``add`` carries key ``M`` or ``M:...``), of the item's own repo, and the item's ``add`` predates the
abandon (it is at or before the ``items.jsonl`` line the abandon names in ``items_last``). Each file is searched on its
own; no position is ever compared across the two files. A present cause is checked on every decide, signed or not.

:func:`verify_record` is pure over ``(record, authority snapshot)``. :func:`require_policy` is the one table of the fixed 1.0
policy; the writer calls it before it signs or writes.

Known edges:
* a process that changes a store file in the instant between the writer's last check and its write is not excluded (the
  writer re-checks both files' identity, size and mtime right before it writes; a lock-free writer is outside the protocol).
* a cited line nested deeper than the JSON parser's recursion limit: the cause check refuses it as "not a record"
  (RecursionError is caught here, never a crash).
* an idempotent ``add --sign`` that writes nothing still needs a findable terminal key and an authority log (preflight runs
  before the store is read).
* two signed writes prepared at once: the second to finish its ceremony finds the store changed and is refused ("sign
  again"); with the lock released during the prompt, that is the price of never keeping a reader waiting on the operator.
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
    "ASSERTED_OK", "DECISION_CLASS", "ITEMS_NONE", "NAMESPACE", "POLICY", "SIGNED_GRADES", "Prepared", "Seal", "Verdict",
    "abandon_problem", "admit", "ceremony", "commit_phone", "cut_cause_problem", "earn", "move_is_decision", "prepare", "require_policy", "resolve_authority",
    "signing_bytes", "terminal_seal", "verify_cause", "verify_record",
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
    log NOW; the log itself is not read here. Refused (BoardError) when there is no single cockpit holding a log, and when the
    consent hooks or the cockpit directory cannot be read (an OSError or ValueError there is a refusal, never a traceback)."""
    try:
        return _resolve_authority(data_root, queue_dir)
    except (OSError, ValueError) as exc:
        raise bs.BoardError(f"{NO_AUTHORITY} (the consent hooks or the cockpit could not be read: {exc})") from None


def _resolve_authority(data_root: Path | str | None, queue_dir: Path | str | None) -> AuthoritySource:
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
#: THE fixed 1.0 policy, keyed by op. Decision-class ops need a signed or relayed record; every other op accepts ``asserted``,
#: save one case keyed on the repo's state too: a ``move`` that :func:`move_is_decision` names (L2 M1, levain 26). The
#: operator setting this per verb is 1.1 and is not here.
POLICY: dict[str, str] = {
    "decide": DECISION_CLASS, "begin-track": DECISION_CLASS, "track": DECISION_CLASS, "abandon-track": DECISION_CLASS,
    "untrack": DECISION_CLASS,
    "add": ASSERTED_OK, "amend": ASSERTED_OK, "register": ASSERTED_OK, "pause": ASSERTED_OK, "resume": ASSERTED_OK,
    "move": ASSERTED_OK, "ack-torn": ASSERTED_OK, "abandon-done": ASSERTED_OK,
}


def _refusal(op: str, why: str = "") -> str:
    return (f"{op}{why} is a decision-class write: it needs --sign (the operator's Touch ID), or --source <np-id> relaying "
            f"his answer; an asserted record is refused")


MOVE_DECISION = " of a tracking or tracked repo that changes its origin or root set"


def move_is_decision(mode: str, old_origin: str, old_root: Sequence[str], new_origin: str, new_root: Sequence[str]) -> bool:
    """A ``move`` is decision-class (head ruling, levain 26, 11-17; L2 M1) when the repo is ``tracking`` or ``tracked`` and the
    move CHANGES the recorded origin or root set: the tracked repo's evidence (its items done on origin/main, its charter
    blob) would then be read from another repository. The same repository relocated on disk (origin and root set kept), and
    any move of an ``untracked`` repo, stay asserted-ok."""
    return mode in ("tracking", "tracked") and (old_origin != new_origin or sorted(old_root) != sorted(new_root))


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


ITEMS_NONE = "none"                       # ``items_last`` of an abandon written when the items file had no line


def abandon_problem(held: Mapping[str, Any], snapshot: Any) -> str | None:
    """Why the ``abandon-track`` record ``held`` cannot be the cause of a cut, or None when it can: it must be signed (a
    ``relayed`` one is P3's verifier's to check and is refused), :func:`verify_record` must find it ``valid`` against
    ``snapshot``, and it must name its migration and ``items_last`` (the items file's last line when it was written; an
    abandon from before that field cannot order the items, so it holds no cut). Raises ``VerifierUnavailable`` when
    ssh-keygen cannot run. It is one step of :func:`cut_cause_problem`, the one judgement of a cut's cause; the recovery for
    any refusal of that is to re-run ``abandon-track`` signed: on an ``aborting`` repo with a cut still owed whose latest
    abandon does not hold, a signed re-run writes a fresh signed abandon (``board_registry.abandon_track``)."""
    grade = held.get("grade")
    if grade == GRADE_RELAYED:
        return ("cause names a relayed abandon-track: a relayed citation is checked by P3's verifier, which this build "
                "does not have, so it is refused, never trusted")
    if grade not in SIGNED_GRADES:
        return f"cause names an abandon-track graded {grade!r}: it must be signed (re-run `abandon-track` with --sign)"
    if snapshot is None:
        return "cause names a signed abandon-track and there is no authority log to verify it against"
    verdict = verify_record(held, snapshot)
    if verdict.state != "valid":
        return f"the cited abandon-track's signature is not valid: {verdict.text} (re-run `abandon-track` with --sign)"
    migration = held.get("migration")
    if not isinstance(migration, str) or not migration:
        return "the cited abandon-track names no migration"
    last = held.get("items_last")
    if not (last == ITEMS_NONE or (isinstance(last, str) and bs.HASH_RE.fullmatch(last))):
        return ("the cited abandon-track does not name the items file's last line (items_last), so no item can be shown to "
                "predate it (re-run `abandon-track` with --sign)")
    return None


def _index_of(lines: Sequence[bytes], digest: str) -> int | None:
    return next((i for i, ln in enumerate(lines) if _record_hash(ln) == digest), None)


def verify_cause(record: Mapping[str, Any], store: "bs.StoreLines | None", snapshot: Any) -> str | None:
    """Why ``record``'s ``cause`` does not hold, or None when it does: :func:`cut_cause_problem` of the record's ``cause``
    and ``id``. Raises ``VerifierUnavailable`` when ssh-keygen cannot run."""
    return cut_cause_problem(record.get("cause"), record.get("id"), store, snapshot)


def cut_cause_problem(cause: Any, item_id: Any, store: "bs.StoreLines | None", snapshot: Any) -> str | None:
    """THE one predicate for "can the abandon-track whose line hashes to ``cause`` be the cause of a cut of ``item_id``":
    why not, or None when it can. :func:`verify_cause` (every cut's write and P6's check) and the abandon recovery
    (``board_registry._latest_abandon_problem``: must a signed re-run write a fresh abandon) both call it, so the recovery
    writes a fresh abandon exactly when a cut citing the latest one would be refused (levain 29, fix 3).
    ``store`` is :class:`board_store.StoreLines` (each
    file's lines as written, and the store id; ``board_store._store_lines``). It holds when ``cause`` is the sha256 of a line
    of ``repos.jsonl`` (ONLY that file is searched) that is an ``abandon-track`` naming THIS store (``store`` equals the store
    id), standing at the place in the registry chain its ``prev`` names (``prev`` is the chain hash of the registry bytes
    before it, and is inside the signed bytes), in a registry that is not torn, that :func:`abandon_problem` accepts, and the
    item ``record`` decides (L2 M2, levain 26): was added for the abandon's ``repo``, with key ``M`` or ``M:...`` where ``M``
    is the abandon's ``migration``, and BEFORE the abandon: its ``add`` line in ``items.jsonl`` is at or before the
    ``items.jsonl`` line whose sha256 is the abandon's ``items_last`` (``none``: the items file was empty then, so no item
    predates it). Both positions are in ``items.jsonl``; nothing is compared across the two files. Raises
    ``VerifierUnavailable`` when ssh-keygen cannot run."""
    from levain.desk import repos_fold as rf
    if not isinstance(cause, str) or not bs.HASH_RE.fullmatch(cause):
        return "cause must be the lowercase sha256 of an abandon-track record"
    if store is None:
        return "cause cannot be checked: the store's lines were not read"
    at = _index_of(store.repos, cause)
    if at is None:
        return "cause names no abandon-track record in the store's registry (repos.jsonl)"
    held = _parse(store.repos[at])
    if not isinstance(held, dict):
        return "cause names a line that is not a record"
    if held.get("op") != "abandon-track":
        return "cause names a record that is not an abandon-track"
    if held.get("store") != store.store_id:
        return (f"cause names an abandon-track of another store ({held.get('store')!r}; this store is "
                f"{store.store_id!r})")
    if held.get("prev") != bs.chain_prev(b"".join(ln + b"\n" for ln in store.repos[:at])):
        return ("the cited abandon-track does not stand where its signed prev places it in the registry chain (a line "
                "moved or copied)")
    folded = rf.fold_repos(store.repos, store_id=store.store_id, unterminated_tail=store.repos_unterminated)
    if folded.torn:
        return f"the registry is torn ({folded.torn_why}): no abandon-track in it can be the cause of a cut"
    problem = abandon_problem(held, snapshot)
    if problem is not None:
        return problem
    migration = held["migration"]
    added_at, added = next(((i, r) for i, r in enumerate(map(_parse, store.items))
                            if isinstance(r, dict) and r.get("op") == "add" and r.get("id") == item_id), (None, None))
    if added is None:
        return f"{item_id} has no add record in the store"
    if not _keyed_to(added.get("key"), migration):
        return f"cause names an abandon-track of migration {migration!r}, and {item_id} is not keyed to it"
    if added.get("repo") != held.get("repo"):
        return (f"cause names the abandon-track of repo {held.get('repo')!r}, and {item_id} was added for "
                f"{added.get('repo')!r}")
    last = held["items_last"]
    anchor = None if last == ITEMS_NONE else _index_of(store.items, last)
    if last != ITEMS_NONE and anchor is None:
        return "the items line the cited abandon-track names (items_last) is not in the items file"
    if anchor is None or added_at > anchor:
        return f"{item_id} was added after the cited abandon-track: an abandon cuts only the items that predate it"
    return None


def require_policy(op: str, grade: str, record: Mapping[str, Any] | None = None, *,
                   store: "bs.StoreLines | None" = None, snapshot: Any = None, decision: str = "") -> None:
    """Refuse (:class:`~levain.desk.board_store.BoardError`) a record the fixed 1.0 policy does not accept at ``grade``. An op
    not in :data:`POLICY` is refused (fail closed). ``grade`` is the WRITER's (a signed grade is then earned by :func:`earn`;
    ``relayed`` is P3's). A decision-class op needs ``signed-*`` or ``relayed``; the one exception (P2-d) is a ``decide`` of
    ``cut`` whose ``cause`` holds (:func:`verify_cause` over ``store``, the store's lines, and ``snapshot``). A present
    ``cause`` is checked whatever the grade, and refused on anything but a cut. ``decision`` (non-empty: the words naming why)
    makes an asserted-ok op decision-class for THIS record: the writer passes it for a ``move`` that :func:`move_is_decision`
    names."""
    from levain.authority_sig import VerifierUnavailable
    record = record or {}
    if op not in POLICY:
        raise bs.BoardError(f"op {op!r} is not in the board's write policy")
    signed = grade in SIGNED_GRADES or grade == GRADE_RELAYED
    if "cause" in record:
        if not (op == "decide" and record.get("decision") == "cut"):
            raise bs.BoardError("cause belongs to a decide of cut only")
        try:
            problem = verify_cause(record, store, snapshot)
        except VerifierUnavailable as exc:
            raise bs.BoardError(f"the cause cannot be checked now ({exc}): nothing was written") from None
        if problem is not None:
            raise bs.BoardError((f"{_refusal(op)}; the cause exception does not hold: " if not signed
                                 else "the cause does not hold: ") + problem)
        return
    if (POLICY[op] == ASSERTED_OK and not decision) or signed:
        return
    raise bs.BoardError(_refusal(op, decision))


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
    :func:`earn` replays after the ceremony, a ``preflight`` that runs BEFORE the lock, and ``__call__(record) -> record``
    that runs on the chained record with NO lock held (:func:`ceremony`). A seal's output is never trusted: :func:`earn`
    verifies it under the lock."""

    via = ""
    authority: AuthoritySource | None = None

    @property
    def grade(self) -> str:
        return VIA_GRADE[self.via]

    def preflight(self) -> None:
        """Refuse before the lock is taken. Default: nothing."""

    def __call__(self, record: dict) -> dict:  # pragma: no cover - interface
        raise NotImplementedError


def ceremony(seal: Seal, record: dict) -> dict:
    """Run ``seal`` on the chained ``record``: the prompt (or the phone signature's check). ``board_store._append`` calls it
    with NO lock held. The result must be ``record`` plus a ``sig``, or the write is refused (BoardError). Nothing is
    trusted yet: :func:`earn` verifies it under the lock."""
    sealed = seal(record)
    if not isinstance(sealed, dict) or {k: v for k, v in sealed.items() if k != "sig"} != record or "sig" not in sealed:
        raise bs.BoardError("the seal returned a record other than the one built: nothing was written")
    return sealed


def earn(target: bs.Target, seal: Seal, sealed: dict) -> dict:
    """Earn the sealed record's grade: called by ``board_store._append`` under the exclusive lock, after the ceremony and
    right before the write, once it has checked the store is as the record was built from. The authority log is replayed
    from ``seal.authority`` NOW (never a snapshot taken earlier); the write is REFUSED (BoardError, nothing written) unless
    the record names this store (``store``), :func:`verify_record` finds it ``valid``, and, when it carries a ``cause``,
    :func:`verify_cause` holds against the same replay."""
    from levain.authority_sig import VerifierUnavailable
    sid = bs.read_store_id(target.store_dir)
    if sealed.get("store") != sid:
        raise bs.BoardError(f"not written: the signed record names store {sealed.get('store')!r}, and this store is {sid!r}")
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


def admit(target: bs.Target, seal: Seal, record: dict) -> dict:
    """:func:`ceremony` then :func:`earn`, in one call (the caller holds whatever lock it needs; ``board_store._append``
    calls the two halves itself, with the lock released between them)."""
    return earn(target, seal, ceremony(seal, record))


class _TerminalSeal(Seal):
    via = "terminal"

    def __init__(self, key_dir=None, signer_factory=None, remote: Callable[[], str | None] | None = None,
                 in_agent_session: Callable[[], bool] | None = None, bound_s: float | None = None,
                 authority: AuthoritySource | None = None):
        self._key_dir, self._factory, self._remote, self._agent, self._bound = key_dir, signer_factory, remote, in_agent_session, bound_s
        self.authority = authority
        self._key = None

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

    def __call__(self, record: dict) -> dict:
        from levain.consent import terminal_sign as ts
        from levain.cockpit import keys as _keys
        if self._key is None:
            self.preflight()
        why = (self._remote or ts.remote_reason)()          # judged AT the prompt (no lock is held here), never earlier
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
    the chain head those bytes commit to, ``store`` the id of the store it was prepared for (the record names it too, inside
    ``message``). ``verb`` and ``kwargs`` re-build the same record in :func:`commit_phone`."""
    verb: str
    kwargs: Mapping[str, Any] = field(repr=False)
    record: Mapping[str, Any] = field(repr=False)
    message: bytes
    prev: str
    store: str = ""


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
    """Build, under the lock and without writing, the record ``verb`` would write now, with grade ``signed-phone``, its
    ``prev`` and this store's ``store``; the caller hands ``message`` to the passkey ceremony. ``kwargs`` are the verb's own
    arguments (``author`` must be the operator, refused before anything else otherwise; ``seal`` is refused). ``now`` is frozen
    here, after the arguments are merged (a ``now`` of None is replaced), so :func:`commit_phone` re-builds the same bytes.
    ``authority`` (a source) is required for a cut that carries a ``cause``: without it the refusal is :data:`NO_AUTHORITY`.
    Nothing is written."""
    if "seal" in kwargs:
        raise bs.BoardError("prepare makes its own seal: a seal argument is refused")
    if verb == "decide" and kwargs.get("cause") is not None and authority is None:
        raise bs.BoardError(NO_AUTHORITY)
    kwargs = {**kwargs, "now": kwargs.get("now") or bs._utc_now()}
    try:
        _verb(verb)(target, **kwargs, seal=_CaptureSeal(authority))
    except _Captured as got:
        return Prepared(verb, kwargs, got.record, signing_bytes(got.record), got.record["prev"], got.record["store"])
    raise bs.BoardError(f"{verb} would write nothing: there is nothing to sign")


class _PhoneSeal(Seal):
    via = "phone"

    def __init__(self, prepared: Prepared, sshsig: str, authority: AuthoritySource):
        self._p, self._sig, self.authority = prepared, sshsig, authority
        self.used = False

    def __call__(self, record: dict) -> dict:
        self.used = True
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
    ceremony, never a snapshot the caller took before it (a snapshot is refused). Refused before anything is read when
    ``target`` is not the store ``prepared`` was made for. Under the lock the record is re-built; it is REFUSED (nothing
    written) when the chain head is no longer ``prepared.prev`` ("the board changed while you signed: sign again"), when its
    bytes differ from what was signed, when the verb would now write nothing (an idempotent ``add`` another writer landed:
    the signature signed nothing that is written, so the commit is not a success), or when :func:`earn` does not find it
    valid. Returns what the verb returns."""
    if not callable(authority):
        raise bs.BoardError("commit_phone takes an authority SOURCE, replayed after the ceremony, not a snapshot")
    sid = bs.read_store_id(target.store_dir)
    if prepared.store != sid:
        raise bs.BoardError(f"this decision was prepared for store {prepared.store!r}, and this store is {sid!r}: "
                            f"nothing was written")
    seal = _PhoneSeal(prepared, sshsig, authority)
    out = _verb(prepared.verb)(target, **prepared.kwargs, seal=seal)
    if not seal.used:
        raise bs.BoardError(f"{CHAIN_MOVED} (the {prepared.verb} is already on the board, written by another writer: "
                            f"nothing was signed or written)")
    return out
