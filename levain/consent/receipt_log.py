"""levain.consent.receipt_log — the consent queue's receipts read as an RFC 6962 Merkle log (Q11).

The queue writes one receipt per transition (``receipts`` in ``queue.db``, :mod:`levain.consent.queue`), append-only
by SQL trigger. A trigger stops a buggy writer, never a process holding the operator's uid: it can drop the
triggers and rewrite or delete rows. This module makes such a rewrite evident under the operator's own key, with
no schema change and no stored hash: the log is COMPUTED from the rows every time it is read.

  - leaf ``i`` is the canonical bytes of the receipt row ``i``-th in ``seq`` order (:func:`leaf`): the JSON of
    ``seq``, ``request_id``, ``state``, ``created_at`` and ``face`` (the stored text, as stored), keys sorted, no
    whitespace, non-ASCII kept, as UTF-8;
  - the head of size ``n`` is ``{"size": n, "root": <hex>}``, ``root`` the RFC 6962 Merkle tree hash of leaves
    ``0..n-1`` (:func:`levain.contracts.merkle.root`), the empty tree's for ``n == 0`` (:func:`head`);
  - every signed answer and late ruling carries the head the queue read when it built the record to sign
    (``receipts_head``), and the queue applies one only when that head is still the head of the same size over
    its receipts, checked inside the write transaction that applies it (:func:`head_refusal`). So the operator's
    signature attests every receipt before it: no mediator's signature, no key but his;
  - :func:`verify` recomputes the log and checks every stored signed answer's head against it
    (``levain consent receipts verify``). An answer applied before the chaining (``.../1``) has its signature
    re-verified and is listed as signed before chaining; one signed by a key revoked SINCE (its signature good under
    that key) is listed as such. Neither is a failure: only a head that does not match, a signature that does not
    verify, a key never enrolled or marked compromised, or an unreadable log is.

Known edges (each stated, none built against):
  - a rewrite of receipts after the last signed head (the unsigned tail: grant answers, expiries, undecided marks,
    the last signed answer's own receipt) is not evident until a later signed answer covers it, or ever if no one
    signs again;
  - the heads :func:`verify` checks are read from request rows, which a raw writer can also delete or rewrite:
    deleting the rows of the latest signed answers moves the last signed head back (the tail it reports grows);
  - delivery checks a stored head's shape, never the log: an answer applied before a rewrite still delivers; the
    next apply and :func:`verify` are what see the rewrite;
  - recomputing a head reads and hashes every receipt up to its size: linear in the log, per signed answer
    applied and per distinct head verified (71-136 ms for a head over 10,000 receipts, measured 10-19);
  - an answer signed by a key revoked WITHOUT ``compromised_since`` is listed as signed by a key revoked since,
    whatever its ``signed_at`` says: ``signed_at`` is the signer's own claim, so a check on it cannot hold against
    the key's holder. A lost or stolen key is revoked with ``compromised_since``, and then every answer it signed
    fails :func:`verify`;
  - a signed head of size 0 always matches (the empty tree's root is fixed): it attests nothing;
  - the authority ledger (:mod:`levain.consent.ledger`) is not part of this log;
  - refused_answers rows are not leaves of this log; a raw writer can add or drop them;
  - a writer who also holds an enrolled key can sign a head over anything: the key is the root of trust.

Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from pathlib import Path
from typing import Any

from levain.contracts import merkle

__all__ = ["HEAD_KEYS", "HEAD_SIZE_CEILING", "LogUnreadable", "Report", "Revoked", "SignedHead", "head",
           "head_problem", "head_refusal", "leaf", "leaves", "verify"]

HEAD_KEYS = frozenset({"size", "root"})
# a head's size is a count below this, so it always fits the SQLite integer LIMIT it is read with
HEAD_SIZE_CEILING = 2 ** 62
_LEAF_SQL = "SELECT seq, request_id, state, created_at, face FROM receipts ORDER BY seq"
_HEX = frozenset("0123456789abcdef")
_TEXT_COLUMNS = ("request_id", "state", "created_at", "face")


class LogUnreadable(ValueError):
    """The receipts cannot be read as a log: a receipt row is not an integer ``seq`` and four UTF-8 texts."""


def leaf(row: Sequence[Any]) -> bytes:
    """The canonical bytes of one receipt row ``(seq, request_id, state, created_at, face)``. Raises
    :class:`LogUnreadable` when ``seq`` is not an integer or another column is not text (a BLOB or a number a
    raw writer stored) or not UTF-8 encodable (a lone surrogate)."""
    seq, *texts = row
    if type(seq) is not int:
        raise LogUnreadable(f"a receipt's seq is not an integer: {seq!r}")
    fields: dict[str, Any] = {"seq": seq}
    if len(texts) != len(_TEXT_COLUMNS):   # zip's strict=True is 3.10+; the queue broker runs this under ROOT_PYTHON
        raise ValueError(f"a receipt row has {len(texts) + 1} columns, not {len(_TEXT_COLUMNS) + 1}")
    for name, value in zip(_TEXT_COLUMNS, texts):
        if type(value) is not str:
            raise LogUnreadable(f"receipt {seq}: its {name} is not text")
        fields[name] = value
    try:
        return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise LogUnreadable(f"receipt {seq}: it is not UTF-8 text") from None


def _rows(conn: sqlite3.Connection, size: int | None) -> list[tuple[Any, ...]]:
    """The receipt rows in ``seq`` order, all or the first ``size``. A text column holding bytes that are not UTF-8
    fails in sqlite3's own decode, before :func:`leaf` sees the row: that is :class:`LogUnreadable` too (L3 r2)."""
    try:
        if size is None:
            return conn.execute(_LEAF_SQL).fetchall()
        return conn.execute(_LEAF_SQL + " LIMIT ?", (size,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "decode" not in str(exc).lower():
            raise
        raise LogUnreadable("a receipt is not UTF-8 text") from None


def leaves(conn: sqlite3.Connection, size: int | None = None) -> list[bytes]:
    """The leaves of the log in ``conn`` (inside the caller's transaction), all of them or the first ``size``."""
    return [leaf(r) for r in _rows(conn, size)]


def _head_of(data: list[bytes], size: int) -> dict[str, Any]:
    return {"size": size, "root": merkle.root(data[:size]).hex()}


def head(source: Any, size: int | None = None) -> dict[str, Any]:
    """The head of the log of size ``size`` (default: every receipt). ``source`` is a connection inside the caller's
    transaction, or a :class:`~levain.consent.queue.ConsentQueue`, read in one read transaction of its own.
    Raises ``ValueError`` when ``size`` exceeds the log, :class:`LogUnreadable` (a ``ValueError``) when a receipt it
    reads cannot be a leaf."""
    if isinstance(source, sqlite3.Connection):
        data = leaves(source, size)
    else:
        with source._read() as conn:
            data = leaves(conn, size)
    if size is not None and len(data) < size:
        raise ValueError(f"the log holds {len(data)} receipts, fewer than {size}")
    return _head_of(data, len(data) if size is None else size)


def head_problem(value: Any) -> str | None:
    """Why ``value`` is not a well-formed head, or ``None``. Its shape only, never whether a log has it."""
    if not isinstance(value, Mapping) or set(value) != HEAD_KEYS:
        return "receipts head is not a {size, root} record"
    size, root = value["size"], value["root"]
    if type(size) is not int or not 0 <= size < HEAD_SIZE_CEILING:
        return f"receipts head size {size!r} is not a count"
    if not isinstance(root, str) or len(root) != 2 * merkle.HASH_LEN or not set(root) <= _HEX:
        return "receipts head root is not a lowercase hex sha256"
    return None


def head_refusal(conn: sqlite3.Connection, signed: Mapping[str, Any], *, noun: str) -> str | None:
    """Inside the caller's transaction (the apply's write; the queue's refused-answer evidence read): why the
    signed head ``signed`` (well-formed, :func:`head_problem`) is not the head of its size over the receipts
    ``conn`` holds now, or ``None`` when it is. ``noun`` names the signed record in the reason (``answer``,
    ``ruling``)."""
    size = signed["size"]
    try:
        data = leaves(conn, size)
    except LogUnreadable as exc:
        return f"the receipts cannot be read as a log ({exc})"
    if len(data) < size:
        return f"the {noun} names more receipts than the queue holds"
    if _head_of(data, size)["root"] != signed["root"]:
        return f"the receipts changed under this {noun}"
    return None


@dataclass(frozen=True)
class SignedHead:
    """A stored signed answer (``via`` ``signature``) or ruling (``ruling``) whose head matches the log."""
    request_id: str
    via: str
    size: int
    root: str
    signer_fp: str
    signed_at: str


@dataclass(frozen=True)
class Revoked:
    """A stored signed answer or ruling whose signature verifies under a key the authority log has revoked SINCE
    (revoked, never marked compromised): information, never a chain failure. Delivery still delivers nothing for
    it. ``head_checked``: it carries a head and that head matches (an unchained one has none to check)."""
    request_id: str
    signer_fp: str
    head_checked: bool


@dataclass(frozen=True)
class Report:
    """What :func:`verify` found. ``receipts``: the log's size, ``None`` when the store is not one whose log can be
    read at all (``unreadable`` then says why, and nothing else was read). ``last_signed``: the matching signed
    head of the greatest size (the latest signed among equals) by a key still active, or ``None``.
    ``unsigned_tail``: receipts after it. ``unchained``: requests answered in a format signed before the receipts were chained whose signature
    still verifies (no head to check; listed, never failures). ``revoked``: signed answers whose signer was revoked
    since (:class:`Revoked`; listed, never failures, and never the last signed head). ``mismatches``:
    ``(request_id, reason)`` for every signed answer or ruling that does not verify now (a bad signature, a key never
    enrolled, a key marked compromised) or whose head does not match. ``unreadable``: why the receipts cannot be
    read as a log, or ``None``. ``ok`` only with no mismatch and a readable log."""
    receipts: int | None
    last_signed: SignedHead | None
    unsigned_tail: int
    unchained: tuple[str, ...]
    mismatches: tuple[tuple[str, str], ...]
    signed: int
    revoked: tuple[Revoked, ...] = ()
    unreadable: str | None = None

    @property
    def ok(self) -> bool:
        return not self.mismatches and self.unreadable is None


def _revoked_since(snap: Any, payload: Mapping[str, Any]) -> Any:
    """``snap`` with the payload's signer read as active, when the signer is a key ``snap`` holds REVOKED (and never
    marked compromised); else ``None``. Used only to ask whether the signature itself verifies."""
    fp = payload.get("signer_fp")
    key = snap.key(fp) if isinstance(fp, str) else None
    if key is None or key.active or key.compromised_since is not None:
        return None
    return replace(snap, keys=MappingProxyType({**snap.keys, fp: replace(key, revoked_seq=None)}))


def verify(queue_dir: Path | str, *,
           authority_for: Callable[[Any], Any] | None = None) -> Report:
    """Recompute the log of the queue in ``queue_dir`` and check every signed answer and late ruling stored in it,
    read in one read-only snapshot (the store is never created, migrated or written). Each one is re-verified as
    delivery verifies it: its stored record must be canonical and name the row's decision and signer, and
    :meth:`~levain.consent.queue.ConsentQueue._refusal` (an answer) or ``_ruling_refusal`` (a ruling, for the
    request's gap) must pass under a fresh replay of the authority log ``authority_for(request)`` returns (default:
    the cockpit the request's agent hook verifies against, :func:`levain.consent.answering.answer_root`). Then its
    ``receipts_head`` must be the head of its size over the log as it stands. Only a current-format store holding
    its ``requests`` and ``receipts`` tables (empty or not) has a log: an empty database, a store of
    :data:`~levain.consent.queue.PRIOR_FORMAT` (not migrated: verify never migrates) or one lacking either table
    is reported unreadable, ``receipts`` ``None``."""
    from levain.authority import AuthorityError, VerifierUnavailable, anchor_path
    from levain.consent.queue import (_FIELDS, AnswerRefused, ConsentQueue, QueueStoreError, StoreNotCurrent,
                                      authority_source)
    from levain.consent.record import UNCHAINED_FORMATS

    qdir = Path(queue_dir)
    if authority_for is None:
        from levain.consent.answering import answer_root

        def authority_for(request: Any) -> Any:
            root = answer_root(qdir, request)
            return authority_source(root / "cockpit.db", anchor=anchor_path(root))

    reading = ConsentQueue(qdir, home=Path.home(), authority=authority_source(qdir, anchor=None))
    cols = ", ".join(_FIELDS) + ", state, decided_at, answer, signature, signer_fp, via"
    try:
        with reading._read_as_is(frozenset({"requests", "receipts"}), whole=True) as conn:
            try:
                rows: list[tuple[Any, ...]] | None = _rows(conn, None)
                rows_unreadable = None
            except LogUnreadable as exc:
                rows, rows_unreadable = None, str(exc)
            got = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()
            queue_id = None if got is None else got[0]
            cur = conn.execute(f"SELECT {cols} FROM requests WHERE via IN ('signature', 'ruling') "
                               "ORDER BY decided_at, rowid")
            names = [d[0] for d in cur.description]
            signed_rows = [dict(zip(names, r)) for r in cur.fetchall()]
            gaps = {r["id"]: ConsentQueue._gap_of(conn, r["id"]) for r in signed_rows if r["via"] == "ruling"}
    except StoreNotCurrent as exc:
        return Report(receipts=None, last_signed=None, unsigned_tail=0, unchained=(), mismatches=(), signed=0,
                      unreadable=str(exc))
    mismatches: list[tuple[str, str]] = []
    unreadable = None
    try:
        if rows is None:
            raise LogUnreadable(rows_unreadable)
        data: list[bytes] | None = [leaf(r) for r in rows]
    except LogUnreadable as exc:
        data = None
        unreadable = f"the receipts cannot be read as a log ({exc})"
    roots: dict[int, str] = {}
    snapshots: dict[str, Any] = {}
    unchained: list[str] = []
    revoked: list[Revoked] = []
    good: list[SignedHead] = []
    for row in signed_rows:
        rid = str(row["id"])
        request = ConsentQueue._request_of(row)
        payload = None if request is None else ConsentQueue._stored_record(row)
        if request is None:
            mismatches.append((rid, "the request row's seal does not match"))
            continue
        if payload is None:
            mismatches.append((rid, "the stored answer is not its own canonical record"))
            continue
        noun = "ruling" if row["via"] == "ruling" else "answer"
        fmt = payload.get("format")
        is_unchained = isinstance(fmt, str) and fmt in UNCHAINED_FORMATS     # anything else: the refusal judges it
        named = payload.get("decision")
        if not isinstance(named, str) or {"allow": "allowed", "deny": "denied"}.get(named) != row["state"] \
                or payload.get("signer_fp") != row["signer_fp"]:
            mismatches.append((rid, f"the stored {noun} does not name this row's decision and signer"))
            continue
        if request.agent not in snapshots:
            try:
                snapshots[request.agent] = authority_for(request)()
            except (AnswerRefused, AuthorityError, VerifierUnavailable, QueueStoreError, OSError, ValueError,
                    sqlite3.Error) as exc:
                snapshots[request.agent] = f"its signature cannot be verified: {exc}"
        snap = snapshots[request.agent]
        if isinstance(snap, str):
            mismatches.append((rid, snap))
            continue
        if noun == "ruling" and gaps.get(rid) is None:
            mismatches.append((rid, "the ruled request has no gap number"))
            continue

        def refusal(authority: Any) -> str | None:
            # the queue's own delivery check (an unchained record as its own format), never a copy of it
            if noun == "answer":
                return reading._refusal(request, queue_id, payload, row["signature"], authority, unchained_ok=True)
            return reading._ruling_refusal(request, queue_id, gaps[rid], payload, row["signature"], authority,
                                           unchained_ok=True)

        why = refusal(snap)
        was_revoked = False
        if why is not None:
            # a signer revoked SINCE: is the signature itself good under that key? then it is information
            as_active = _revoked_since(snap, payload)
            why_active = None if as_active is None else refusal(as_active)
            if as_active is None or why_active is not None:
                mismatches.append((rid, why if why_active is None else f"{why_active} (its signer revoked since)"))
                continue
            was_revoked = True
        if is_unchained:
            if was_revoked:
                revoked.append(Revoked(request_id=rid, signer_fp=payload["signer_fp"], head_checked=False))
            unchained.append(rid)
            continue
        signed_head = payload["receipts_head"]
        size = signed_head["size"]
        if data is None:
            mismatches.append((rid, unreadable))
            continue
        if size > len(data):
            mismatches.append((rid, f"the {noun} names more receipts than the queue holds"))
            continue
        if size not in roots:
            roots[size] = _head_of(data, size)["root"]
        if roots[size] != signed_head["root"]:
            mismatches.append((rid, f"the receipts changed under this {noun}"))
            continue
        if was_revoked:
            revoked.append(Revoked(request_id=rid, signer_fp=payload["signer_fp"], head_checked=True))
            continue
        good.append(SignedHead(request_id=rid, via=row["via"], size=size, root=signed_head["root"],
                               signer_fp=payload["signer_fp"], signed_at=payload["signed_at"]))
    last = max(good, key=lambda h: (h.size, h.signed_at), default=None)
    count = None if rows is None else len(rows)   # rows the decode refused: no count, the report says unreadable
    return Report(receipts=count, last_signed=last, unsigned_tail=0 if count is None else count - (last.size if last else 0),
                  unchained=tuple(unchained), mismatches=tuple(mismatches), signed=len(good),
                  revoked=tuple(revoked), unreadable=unreadable)
