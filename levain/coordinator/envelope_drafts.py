"""levain.coordinator.envelope_drafts — the signed draft envelope's store half (H1-1): write, read, lapse, bind.

Functions over an :class:`~levain.coordinator.envelopes.EnvelopeStore`, as :mod:`levain.coordinator.missions` does; the
pure half (tables, shapes, digest, diffs) is :mod:`levain.coordinator.envelope_draft`. A SERIES is one attempt to get
one envelope signed for one thread; a REVISION is one row of it. Every write is one ``BEGIN IMMEDIATE`` through the
store's ``_write``, the clock read under the lock, and every check that reads the store runs inside it (a rule's one
non-store input, a drop's words parsed, is read before the lock and handed in).

State is DERIVED except a withdrawal (design r3 §2.2): signed (an envelope row names the revision), superseded (a later
revision exists), lapsed (the thread moved, the head moved or was narrowed since, or too little time is left), open.
Rules are pure over what the lock holds: ``drop/1`` and ``spec/1`` here, no model call. A cite is a CLAIM OF RELEVANCE
the drafter makes, never proof a record supports a field; the writer principal (``by``) is neither signed nor shown.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from levain.authority import AuthoritySnapshot
from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator import envelope_draft as ed
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, next_gseq

__all__ = [
    "AgainstHead", "Draft", "DraftStale", "DraftState", "RULES", "admit_check", "cite_label", "create",
    "draft_sign_bytes", "drop_rule", "latest", "lapse_reason", "rederive_check", "revise", "spec_rule", "state_of",
    "unclosed_series", "withdraw",
]

RULES = ("drop/1", "spec/1")
_UNAUTH = "as reported by the sender; not authenticated before S1"
_EDIT_SPEC = ("where", "lands", "pushes", "publishes", "budget")


class DraftStale(CoordError):
    """The draft moved under the caller: a later revision, a lapse, a withdrawal, a signature, a digest that is not
    the stored one. The remedy is to re-render; nothing was built, signed or written."""


@dataclass(frozen=True)
class Draft:
    """One stored revision, read back (its proposal and cites are each their own canonical encoding, checked)."""
    draft_id: str
    revision: int
    thread_id: str
    gseq: int
    parent: str | None
    proposal: dict[str, Any]
    cites: list[dict[str, str]]
    digest: str
    by: str
    at: str


@dataclass(frozen=True)
class DraftState:
    """``kind`` is open, superseded, signed, lapsed or withdrawn; ``reason`` says why for lapsed; ``complete`` is False
    while an editable value is still null (shown, never signable)."""
    kind: str
    reason: str | None
    complete: bool


@dataclass(frozen=True)
class AgainstHead:
    """The diff against the thread's head, as the signer is shown it: ``base_kind`` is ``none`` (no head), ``effective``
    (the head as narrowed) or ``revoked`` (the head no longer authorises anything: killed, ended, or its key no longer
    holds, so every value is a new grant); ``reason`` says why for revoked."""
    base_kind: str
    reason: str | None
    diff: dict[str, Any]
    members_added: list[str]
    members_removed: list[str]
    wider: bool


# --- reading rows -----------------------------------------------------------------------------------------------
_COLS = "draft_id, revision, thread_id, gseq, parent, proposal, cites, digest, by, at"


def _has_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelope_drafts'").fetchone() \
        is not None


def _draft(row: tuple[Any, ...]) -> Draft:
    try:
        proposal = ed.load_proposal(row[5])
        cites = ed.load_cites(row[6], proposal)
    except ed.DraftError as exc:
        raise CoordError(f"draft {row[0]}#{row[1]} is not a revision this reader reads: {exc}") from None
    return Draft(row[0], row[1], row[2], row[3], row[4], proposal, cites, row[7], row[8], row[9])


def _row(conn: sqlite3.Connection, draft_id: str, revision: int | None = None) -> Draft | None:
    """Revision ``revision`` of a series, or its latest when ``revision`` is None; None when there is none."""
    if revision is None:
        row = conn.execute(f"SELECT {_COLS} FROM envelope_drafts WHERE draft_id = ? ORDER BY revision DESC LIMIT 1",
                           (draft_id,)).fetchone()
    else:
        row = conn.execute(f"SELECT {_COLS} FROM envelope_drafts WHERE draft_id = ? AND revision = ?",
                           (draft_id, revision)).fetchone()
    return None if row is None else _draft(row)


def _withdrawn(conn: sqlite3.Connection, draft_id: str) -> bool:
    return conn.execute("SELECT 1 FROM envelope_draft_withdrawals WHERE draft_id = ?", (draft_id,)).fetchone() \
        is not None


def _signed_by(conn: sqlite3.Connection, draft_id: str, revision: int | None = None) -> str | None:
    """The id of the envelope that names this series (and revision, when given), or None."""
    if revision is None:
        row = conn.execute("SELECT envelope_id FROM envelopes WHERE json_extract(body, '$.draft.draft_id') = ?",
                           (draft_id,)).fetchone()
    else:
        row = conn.execute("SELECT envelope_id FROM envelopes WHERE json_extract(body, '$.draft.draft_id') = ? AND "
                           "json_extract(body, '$.draft.revision') = ?", (draft_id, revision)).fetchone()
    return None if row is None else row[0]


def _unclosed(conn: sqlite3.Connection, thread_id: str) -> Draft | None:
    """The latest revision of the thread's one unclosed series (not signed, not withdrawn), or None."""
    if not _has_tables(conn):
        return None
    row = conn.execute(
        "SELECT d.draft_id FROM envelope_drafts d WHERE d.thread_id = ? AND d.revision = 1 AND NOT EXISTS (SELECT 1 "
        "FROM envelope_draft_withdrawals w WHERE w.draft_id = d.draft_id) AND NOT EXISTS (SELECT 1 FROM envelopes e "
        "WHERE json_extract(e.body, '$.draft.draft_id') = d.draft_id) ORDER BY d.gseq LIMIT 1", (thread_id,)).fetchone()
    return None if row is None else _row(conn, row[0])


@contextmanager
def _reading(store: EnvelopeStore) -> Iterator[sqlite3.Connection]:
    """The read-only door: creates nothing, so a store older than drafts is read as holding none."""
    conn = store.open_readonly()
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


# --- lapse (L1-L3), shared by every reader and writer ---------------------------------------------------------------
def lapse_reason(conn: sqlite3.Connection, now: str, thread_id: str, proposal: Mapping[str, Any]) -> str | None:
    """Why a proposal is lapsed at ``now``, or None. L1: its thread is done or closed, or is not at the revision and
    members the proposal binds. L2: the thread's head is not the one the proposal supersedes (id and digest; none = no
    head), or a narrowing that applies to that head for this thread was written after the proposal's ``seen`` (the
    predicate acceptance runs for every superseding body). L3: a stop with less than :data:`~envelope_draft.MIN_LEFT`
    left. Shared by the writers (born open), the readers, the sign builder and acceptance."""
    t = EnvelopeStore._thread(conn, thread_id)
    bind = proposal["binds"][0]
    if t.state in ("done", "closed"):
        return f"thread {thread_id} is {t.state}"
    if t.rev != bind["rev"] or t.members_sha256 != bind["members_sha256"]:
        return f"thread {thread_id} changed since this revision was written (revision {t.rev}, not {bind['rev']})"
    head = EnvelopeStore._head(conn, thread_id)
    sup = proposal["supersedes"]
    if sup is None:
        if head is not None:
            return f"thread {thread_id} has envelope {head.envelope_id} now"
    elif head is None or head.envelope_id != sup["envelope_id"] or head.digest != sup["digest"]:
        return f"the envelope this revision supersedes ({sup['envelope_id']}) is no longer thread {thread_id}'s head"
    elif EnvelopeStore._narrowed_after(conn, head.envelope_id, thread_id, sup["seen"]):
        return f"envelope {head.envelope_id} was narrowed after this revision was written"
    stop = proposal["stop_at"]
    if stop is not None and parse_stamp(stop) - parse_stamp(now) < ed.MIN_LEFT:
        return "the stop time is under 5 minutes away or has passed"
    return None


def _members_at(conn: sqlite3.Connection, thread_id: str, rev: int) -> list[str]:
    row = conn.execute("SELECT members FROM thread_revisions WHERE thread_id = ? AND rev = ?",
                       (thread_id, rev)).fetchone()
    return [] if row is None else list(json.loads(row[0]))


def _series_gseq(conn: sqlite3.Connection, draft_id: str | None) -> int | None:
    if draft_id is None:
        return None
    row = conn.execute("SELECT gseq FROM envelope_drafts WHERE draft_id = ? AND revision = 1", (draft_id,)).fetchone()
    return None if row is None else row[0]


# --- cites ------------------------------------------------------------------------------------------------------
def _cite_problem(conn: sqlite3.Connection, thread_id: str, bind_rev: int, series_gseq: int | None,
                  cite: Mapping[str, str]) -> str | None:
    """Why a cite does not hold against its record, or None. A drop is cited by its words' digest and must be a member
    of the thread at the proposal's bound revision, or newer than the series (a correction sent after it began; a
    series not written yet has none); an envelope by its stored digest and must be bound to this thread."""
    ref, digest = cite["ref"], cite["digest"]
    if cite["kind"] == "drop":
        row = conn.execute("SELECT words_sha256, gseq FROM drops WHERE drop_id = ?", (ref,)).fetchone()
        if row is None:
            return f"cites drop {ref}, which this store does not hold"
        if row[0] != digest:
            return f"cites drop {ref} with a digest that is not its words'"
        if ref in _members_at(conn, thread_id, bind_rev):
            return None
        if row[1] is not None and series_gseq is not None and row[1] > series_gseq:
            return None
        return f"cites drop {ref}, which is not in this thread and is not newer than this draft series"
    row = conn.execute("SELECT digest FROM envelopes WHERE envelope_id = ?", (ref,)).fetchone()
    if row is None:
        return f"cites envelope {ref}, which this store does not hold"
    if row[0] != digest:
        return f"cites envelope {ref} with a digest that is not its own"
    if conn.execute("SELECT 1 FROM envelope_binds WHERE envelope_id = ? AND thread_id = ?",
                    (ref, thread_id)).fetchone() is None:
        return f"cites envelope {ref}, which is not bound to this thread"
    return None


def _check_cites_against(conn: sqlite3.Connection, d_thread: str, proposal: Mapping[str, Any],
                         series_gseq: int | None, cites: list[dict[str, str]]) -> str | None:
    rev = proposal["binds"][0]["rev"]
    for c in cites:
        problem = _cite_problem(conn, d_thread, rev, series_gseq, c)
        if problem is not None:
            return problem
    return None


def cite_label(conn: sqlite3.Connection, d: Draft, cite: Mapping[str, str]) -> dict[str, Any]:
    """What a card may say about a cited record: only what the store holds, derived from append-only rows, so a label
    cannot move under a revision. A drop: its words, whether it is in this thread, its stored surface and time labelled
    as the sender's report; never "your words" and never the writer principal. An envelope: the key that signed it."""
    if cite["kind"] == "drop":
        row = conn.execute("SELECT words, surface, received_at FROM drops WHERE drop_id = ?", (cite["ref"],)).fetchone()
        if row is None:
            return {"kind": "drop", "ref": cite["ref"], "line": "a drop this store does not hold"}
        inside = cite["ref"] in _members_at(conn, d.thread_id, d.proposal["binds"][0]["rev"])
        return {"kind": "drop", "ref": cite["ref"], "words": row[0], "in_thread": inside,
                "line": "in this thread" if inside else "not in this thread, sent after this draft began",
                "surface": f"{row[1]} at {row[2]}, {_UNAUTH}"}
    row = conn.execute("SELECT signer_fp FROM envelopes WHERE envelope_id = ?", (cite["ref"],)).fetchone()
    return {"kind": "envelope", "ref": cite["ref"],
            "line": "an envelope this store does not hold" if row is None else f"signed by key {row[0]}"}


# --- the checks every reader of "is this revision signable" shares -------------------------------------------------
def _stale_reason(conn: sqlite3.Connection, now: str, d: Draft, digest: str, *, cites: bool) -> str | None:
    """Why revision ``d`` is not the signable one, or None: not its series' latest, another digest than the one named,
    withdrawn, signed, lapsed, incomplete, or (``cites``) a cite that no longer holds against its record."""
    newest = _row(conn, d.draft_id)
    if newest is None or newest.revision != d.revision:
        return f"draft {d.draft_id} has a later revision than #{d.revision}; render it again"
    if d.digest != digest:
        return f"draft {d.draft_id}#{d.revision} is not the digest named; render it again"
    if _withdrawn(conn, d.draft_id):
        return f"draft {d.draft_id} was withdrawn"
    if _signed_by(conn, d.draft_id) is not None:
        return f"draft {d.draft_id} is already signed"
    lapsed = lapse_reason(conn, now, d.thread_id, d.proposal)
    if lapsed is not None:
        return f"draft {d.draft_id}#{d.revision} has lapsed: {lapsed}"
    if not ed.is_complete(d.proposal):
        return f"draft {d.draft_id}#{d.revision} is incomplete: a value is still unset"
    if cites:
        problem = _check_cites_against(conn, d.thread_id, d.proposal, _series_gseq(conn, d.draft_id), d.cites)
        if problem is not None:
            return f"draft {d.draft_id}#{d.revision} {problem}"
    return None


def _named(body: Mapping[str, Any]) -> tuple[str, int, str]:
    ref = body["draft"]
    return ref["draft_id"], ref["revision"], ref["digest"]


def admit_check(conn: sqlite3.Connection, now: str, body: Mapping[str, Any], *, cites: bool) -> None:
    """What acceptance asks of a drafted body (the preflight, before any signature check, and again under the write
    lock with ``cites``): the row exists, is the latest, is not withdrawn or signed by another envelope, its stored
    digest is the one the body names, it is not lapsed and it is complete; with ``cites`` every cite is re-verified.
    A revision already signed by THIS body's own envelope returns, and the store's conflict check words that case.
    Raises :class:`DraftStale`."""
    draft_id, revision, digest = _named(body)
    d = _row(conn, draft_id, revision)
    if d is None:
        raise DraftStale(f"there is no draft {draft_id}#{revision}")
    if _signed_by(conn, draft_id, revision) == body["envelope_id"]:
        return
    reason = _stale_reason(conn, now, d, digest, cites=cites)
    if reason is not None:
        raise DraftStale(reason)


def rederive_check(conn: sqlite3.Connection, body: Mapping[str, Any]) -> None:
    """Outside the lock, after the signature: the stored row's digest is its own record's, and the body is byte for
    byte the stored proposal and its draft reference, so a hand-edited file that names a real revision is refused."""
    draft_id, revision, _digest = _named(body)
    d = _row(conn, draft_id, revision)
    if d is None:
        raise DraftStale(f"there is no draft {draft_id}#{revision}")
    if ed.draft_digest(d.proposal["store_id"], d.draft_id, d.thread_id, d.revision, d.parent, d.proposal,
                       d.cites) != d.digest:
        raise CoordError(f"draft {draft_id}#{revision} does not hash to its stored digest")
    shown = {k: v for k, v in body.items() if k != "draft"}
    if canonical(shown) != canonical(d.proposal) or dict(body["draft"]) != {
            "draft_id": d.draft_id, "revision": d.revision, "digest": d.digest}:
        raise CoordError(f"this body names draft {draft_id}#{revision} but is not that revision's envelope")


# --- writing ----------------------------------------------------------------------------------------------------
def _anchor(conn: sqlite3.Connection, members: tuple[str, ...]) -> tuple[str, str] | None:
    """The thread's latest member drop (by its place in the store's order): the anchor every rule sets, which
    acceptance requires to be a member. It is the thread's anchor, not the author of any field."""
    if not members:
        return None
    marks = ",".join("?" for _ in members)
    row = conn.execute(f"SELECT drop_id, words_sha256 FROM drops WHERE drop_id IN ({marks}) "
                       "ORDER BY gseq DESC, rowid DESC LIMIT 1", tuple(members)).fetchone()
    return None if row is None else (row[0], row[1])


def _sorted(cites: Any) -> Any:
    if isinstance(cites, list) and all(isinstance(c, dict) and {"field", "kind", "ref"} <= set(c) for c in cites):
        return sorted(cites, key=lambda c: (c["field"], c["kind"], c["ref"]))
    return cites


def _write_revision(store: EnvelopeStore, conn: sqlite3.Connection, now: str, *, thread_id: str,
                    draft_id: str | None, fields: Mapping[str, Any], cites: Any, parent: str | None, by: str,
                    carry: bool = False, stop_span: timedelta | None = None) -> Draft:
    """One revision, inside the caller's write transaction. ``draft_id`` None starts a series (revision 1); otherwise
    the next revision of that series, whose latest digest must be ``parent``. ``carry`` keeps the parent's values and
    cites for every field ``fields`` leaves unnamed (a rule's rebase). Everything but the editable values is filled
    from what the lock holds; the revision is checked to its shape, to every cite and to being born open."""
    t = store._must_thread(conn, thread_id)
    newest: Draft | None = None
    if draft_id is not None:
        newest = _row(conn, draft_id)
        if newest is None:
            raise CoordError(f"no draft {draft_id}")
        if newest.thread_id != thread_id:
            raise CoordError(f"draft {draft_id} belongs to another thread")
        if newest.digest != parent:
            raise DraftStale(f"draft {draft_id} moved since it was rendered (its latest revision is "
                             f"#{newest.revision}); render it again")
    sid = store._jobs._store_id(conn)
    anchor = _anchor(conn, t.members)
    if anchor is None:
        raise CoordError(f"thread {thread_id} holds no drops; nothing to draft for")
    head = store._head(conn, thread_id)
    seen = conn.execute("SELECT COALESCE(MAX(gseq), 0) FROM gseq").fetchone()[0]   # before this write takes its own
    given = dict(fields)
    if stop_span is not None:
        given["stop_at"] = utc_stamp(parse_stamp(now) + stop_span)
    try:
        values = ed.check_fields({**({k: newest.proposal[k] for k in ed.EDITABLE} if carry and newest else {}),
                                  **given})
        wanted = _sorted(cites)
        if carry and newest is not None:
            kept = [c for c in newest.cites if c["field"] not in given]
            wanted = _sorted([*kept, *(wanted if isinstance(wanted, list) else [])])
        proposal = {
            "format": env.BODY_FORMAT, "store_id": sid, "envelope_id": "ev-" + secrets.token_hex(16),
            "origin": "worded", "drop_id": anchor[0], "words_sha256": anchor[1],
            "binds": [{"thread_id": thread_id, "rev": t.rev, "members_sha256": t.members_sha256}],
            "supersedes": None if head is None else {"envelope_id": head.envelope_id, "digest": head.digest,
                                                     "seen": seen},
            "issued_at": now, **values}
        ed.check_proposal(proposal)
        wanted = ed.check_cites(wanted, proposal)
    except ed.DraftError as exc:
        raise CoordError(str(exc)) from None
    series = _series_gseq(conn, draft_id)
    problem = _check_cites_against(conn, thread_id, proposal, series, wanted)
    if problem is not None:
        raise CoordError(f"this draft {problem}")
    lapsed = lapse_reason(conn, now, thread_id, proposal)
    if lapsed is not None:
        raise CoordError(f"a revision is born open, and this one would not be: {lapsed}")
    did = draft_id or "ed-" + secrets.token_hex(16)
    revision = 1 if newest is None else newest.revision + 1
    parent_digest = None if newest is None else newest.digest
    digest = ed.draft_digest(sid, did, thread_id, revision, parent_digest, proposal, wanted)
    gseq = next_gseq(conn, now)
    try:
        conn.execute("INSERT INTO envelope_drafts (draft_id, revision, thread_id, gseq, parent, proposal, cites, "
                     "digest, by, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (did, revision, thread_id, gseq, parent_digest, canonical(proposal), canonical(wanted), digest,
                      by, now))
    except sqlite3.IntegrityError as exc:
        # a trigger's refusal (a closed series, a second unclosed series, a stale parent) is the store's line
        raise CoordError(str(exc)) from None
    return Draft(did, revision, thread_id, gseq, parent_digest, proposal, wanted, digest, by, now)


def create(store: EnvelopeStore, thread_id: str, fields: Mapping[str, Any], cites: Any, *, by: str,
           token: object = None) -> Draft:
    """Revision 1 of a new series for ``thread_id``. ``fields`` names editable values (any left out stay null and the
    draft is incomplete); every named value needs a cite. A thread holds one unclosed series: a second is refused."""
    by = store._actor(by, ("operator", "coordinator"))
    with store._write(by, token) as (conn, now):
        return _write_revision(store, conn, now, thread_id=thread_id, draft_id=None, fields=fields, cites=cites,
                               parent=None, by=by)


def revise(store: EnvelopeStore, draft_id: str, fields: Mapping[str, Any], cites: Any, *, parent: str, by: str,
           token: object = None) -> Draft:
    """The next revision of ``draft_id``, holding exactly ``fields`` (an unnamed value goes null) and ``cites``.
    ``parent`` is the digest of the revision the caller rendered: another revision since is :class:`DraftStale`. A
    lapsed revision may be revised (a rebase onto the thread as it is now); a signed or withdrawn series takes none."""
    by = store._actor(by, ("operator", "coordinator"))
    with store._write(by, token) as (conn, now):
        newest = _row(conn, draft_id)
        if newest is None:
            raise CoordError(f"no draft {draft_id}")
        return _write_revision(store, conn, now, thread_id=newest.thread_id, draft_id=draft_id, fields=fields,
                               cites=cites, parent=parent, by=by)


def withdraw(store: EnvelopeStore, draft_id: str, *, by: str = "operator") -> int:
    """Withdraw the series at its latest revision; the revision number. The one stored terminal, and a narrowing (it
    grants nothing), so only the operator writes it. A signed series is refused by the table."""
    by = store._actor(by, ("operator",))
    with store._write(by) as (conn, now):
        newest = _row(conn, draft_id)
        if newest is None:
            raise CoordError(f"no draft {draft_id}")
        try:
            conn.execute("INSERT INTO envelope_draft_withdrawals (draft_id, revision, gseq, by, at) "
                         "VALUES (?, ?, ?, ?, ?)", (draft_id, newest.revision, next_gseq(conn, now), by, now))
        except sqlite3.IntegrityError as exc:
            raise CoordError(str(exc)) from None
        return newest.revision


# --- the rules (design r3 §2.5): a pure function of what the lock holds and the arguments, never a model call ---------
def _rule_write(store: EnvelopeStore, thread_id: str, fields: Mapping[str, Any], cites: list[dict[str, str]], *,
                by: str, token: object, stop_span: timedelta | None = None,
                expect: tuple[str, str] | None = None) -> Draft:
    """A rule's write: the thread's unclosed series gains revision N+1 (the parent's other values and cites carry), else
    a new series starts. ``expect`` is ``(drop_id, words_sha256)`` of the record the rule read before the lock: it must
    still be that record's words."""
    by = store._actor(by, ("operator", "coordinator"))
    with store._write(by, token) as (conn, now):
        if expect is not None:
            row = conn.execute("SELECT words_sha256 FROM drops WHERE drop_id = ?", (expect[0],)).fetchone()
            if row is None or row[0] != expect[1]:
                raise CoordError(f"drop {expect[0]} is not the record this rule read")
        target = _unclosed(conn, thread_id)
        return _write_revision(store, conn, now, thread_id=thread_id,
                               draft_id=None if target is None else target.draft_id, fields=fields, cites=cites,
                               parent=None if target is None else target.digest, by=by, carry=target is not None,
                               stop_span=stop_span)


def drop_rule(store: EnvelopeStore, thread_id: str, *, by: str, token: object = None) -> Draft:
    """``drop/1``: from the thread's latest member drop, ``where`` = the repo it was sent from (``paths`` the whole
    repo) when it was sent from a work tree or picked; nothing else. A drop with no repo leaves every value null."""
    with read_transaction(store._connect) as conn:
        t = store._must_thread(conn, thread_id)
        anchor = _anchor(conn, t.members)
        drop = None if anchor is None else store._drop(conn, anchor[0])
    if drop is None:
        raise CoordError(f"thread {thread_id} holds no drops; nothing to draft for")
    fields: dict[str, Any] = {}
    cites: list[dict[str, str]] = []
    if drop.source.how in ("cwd", "picker") and drop.source.repo:
        fields["where"] = [{"repo": drop.source.repo, "paths": ["."]}]
        cites.append({"field": "where", "kind": "drop", "ref": drop.drop_id, "digest": drop.words_sha256})
    return _rule_write(store, thread_id, fields, cites, by=by, token=token, expect=(drop.drop_id, drop.words_sha256))


def spec_rule(store: EnvelopeStore, thread_id: str, drop_id: str, *, by: str, token: object = None) -> Draft:
    """``spec/1``: a drop whose words are the CLI's envelope spec (a member of the thread, or newer than the series;
    the cite check holds the bound) sets every field the spec names, read through the CLI's own grammar with the
    drop's own source; every other value and its cites carry over from the parent revision. Free words are not a spec
    and are refused (they wait for a translator that writes through :func:`revise`)."""
    from levain.coordinator import cli

    with read_transaction(store._connect) as conn:
        drop = store._must_drop(conn, drop_id)
    try:
        spec = json.loads(drop.words)
    except ValueError:
        raise CoordError(f"drop {drop_id} is not an envelope spec: its words are not JSON") from None
    if not isinstance(spec, dict) or not spec or not set(spec) <= cli._SPEC_KEYS:
        raise CoordError(f"drop {drop_id} is not an envelope spec: a JSON object with keys among "
                         f"{sorted(cli._SPEC_KEYS)}")
    stop_span = cli._parse_stop(spec["stop"]) if "stop" in spec and isinstance(spec["stop"], str) else None
    if "stop" in spec and stop_span is None:
        raise CoordError('stop is a duration text like "2h"')
    body = cli._spec_body({k: v for k, v in spec.items() if k != "stop"}, drop.words, drop.source, store)
    fields = {k: body[k] for k in _EDIT_SPEC if k in spec}
    named = [*fields, *(["stop_at"] if stop_span is not None else [])]
    cites = [{"field": k, "kind": "drop", "ref": drop.drop_id, "digest": drop.words_sha256} for k in named]
    return _rule_write(store, thread_id, fields, cites, by=by, token=token, stop_span=stop_span,
                       expect=(drop.drop_id, drop.words_sha256))


# --- reading, for every surface that only displays ---------------------------------------------------------------
def latest(store: EnvelopeStore, draft_id: str) -> Draft | None:
    """The latest revision of a series, or None (a store older than drafts holds none)."""
    with _reading(store) as conn:
        return _row(conn, draft_id) if _has_tables(conn) else None


def unclosed_series(store: EnvelopeStore, thread_id: str) -> Draft | None:
    """The latest revision of the thread's one unclosed series, or None. Reads through the read-only door."""
    with _reading(store) as conn:
        return _unclosed(conn, thread_id)


def state_of(store: EnvelopeStore, draft_id: str, revision: int | None = None) -> DraftState:
    """A revision's derived state at the store's clock (the series' latest when ``revision`` is None). Raises
    :class:`CoordError` when there is no such draft (a store older than drafts holds none)."""
    with _reading(store) as conn:
        d = _row(conn, draft_id, revision) if _has_tables(conn) else None
        if d is None:
            raise CoordError(f"no draft {draft_id}" + ("" if revision is None else f"#{revision}"))
        complete = ed.is_complete(d.proposal)
        signed = _signed_by(conn, d.draft_id, d.revision)
        if signed is not None:
            return DraftState("signed", signed, complete)
        wd = conn.execute("SELECT revision FROM envelope_draft_withdrawals WHERE draft_id = ?",
                          (d.draft_id,)).fetchone()
        if wd is not None and wd[0] == d.revision:
            return DraftState("withdrawn", None, complete)
        newest = _row(conn, d.draft_id)
        if newest is not None and newest.revision != d.revision:
            return DraftState("superseded", None, complete)
        lapsed = lapse_reason(conn, store._stamp(), d.thread_id, d.proposal)
        return DraftState("lapsed" if lapsed else "open", lapsed, complete)


# --- the builder --------------------------------------------------------------------------------------------------
def draft_sign_bytes(store: EnvelopeStore, draft_id: str, revision: int, draft_digest: str, *,
                     authority: AuthoritySnapshot, signer_fp: str, signed_at: Any
                     ) -> tuple[str, dict[str, str], list[dict[str, Any]], AgainstHead]:
    """``(body_text, payload, since_parent_diff, against_head)``: the bytes a signature over this revision covers and
    the two diffs the signer is shown (a card rendered earlier is a preview; this call's diffs are what the sign
    control renders). In one read transaction it raises :class:`DraftStale`, before any verifier call and before
    :func:`~levain.coordinator.envelope.sign_payload`, when the named revision is not its series' latest, is not the
    digest named, is withdrawn, signed, lapsed, incomplete, or a cite fails against its record. It then makes ONE
    verifier call on the stored head, whose only use is the diff base: a head that is killed, ended or whose key no
    longer verifies is diffed as the empty envelope, so every value shows as a new grant. A verifier that cannot run
    is a fault on this machine and propagates (:class:`~levain.authority.VerifierUnavailable`), never a verdict."""
    with read_transaction(store._connect) as conn:
        now = store._stamp()
        d = _row(conn, draft_id, revision) if _has_tables(conn) else None
        if d is None:
            raise DraftStale(f"there is no draft {draft_id}#{revision}")
        reason = _stale_reason(conn, now, d, draft_digest, cites=True)
        if reason is not None:
            raise DraftStale(reason)
        parent = _row(conn, draft_id, revision - 1) if revision > 1 else None
        since = ed.field_diff(None if parent is None else parent.proposal, d.proposal)
        head = store._head(conn, d.thread_id)
        bind_rev = d.proposal["binds"][0]["rev"]
        ended = effective = None
        added: list[str] = []
        removed: list[str] = []
        if head is not None:
            rows = store._attenuations(conn, head.envelope_id, d.thread_id)
            ended = next((k for k, _v in rows if k in ("kill", "thread_done")), None)
            effective = store._effective(conn, head.envelope_id, d.thread_id)
            from_rev = conn.execute("SELECT rev FROM envelope_binds WHERE envelope_id = ? AND thread_id = ?",
                                    (head.envelope_id, d.thread_id)).fetchone()[0]
            added, removed = store.members_diff(conn, d.thread_id, from_rev, bind_rev)
    base_kind, why = "none", None
    if head is None:
        base = {**ed.empty_base(d.proposal), "budget": {k: 0 for k in env.BUDGET_KEYS}}
    else:
        base_kind = "effective"
        base = effective
        if ended is not None:
            base_kind, why = "revoked", "killed" if ended == "kill" else "its thread ended"
        else:
            # intent: a verifier that cannot run raises out of here as a machine fault; "never refuses" means no
            # verdict refusal, so the head's key state chooses the diff base and nothing else
            refused = env.refusal(head.payload, head.signature, authority, head.body, ssh_keygen=store._ssh_keygen)
            if refused is not None:
                base_kind, why = "revoked", f"the key that signed it no longer holds ({refused})"
        if base_kind == "revoked":
            base = ed.empty_base(head.body)
    assert base is not None
    against = AgainstHead(base_kind, why, ed.against_diff(base, d.proposal), added, removed,
                          env.widens(base, d.proposal) or bool(added))
    body = ed.draft_body(d.proposal, d.draft_id, d.revision, d.digest)
    body_text = canonical(body)
    store.check_body(body_text)          # the shape and the disk checks a body the store can only refuse is never signed over
    payload = env.sign_payload(body, signer_fp=signer_fp, signed_at=signed_at)
    return body_text, payload, since, against
