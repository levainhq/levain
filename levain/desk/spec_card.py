"""levain.desk.spec_card — the tap on a room's ENVELOPE card (H1-4 shape A): the one transaction that turns the words he
posted into a draft revision. Nothing here signs: the draft is an unsigned revision he opens and signs on the thread's own
card, by the passkey ceremony, as for any draft.

The tap is ONE write transaction, all of it or none of it:

  1. the thread's series is opened when it has none (an opener revision), BEFORE the drop, so the drop is "newer than the
     series" and the cite check holds without attaching the drop to the thread (an attach moves the thread's revision,
     which would stop a signed grant at the tap; and a worker reads every member drop, so a spec drop would reach it);
  2. the drop (the bytes he posted), the proposal's ``committed`` event and the room's labelled note
     (:func:`levain.desk.room._commit_rows`), with no desk wake, since the drop is settled in this same transaction;
  3. the draft revision: ``spec/1``'s write over :func:`~levain.desk.envelope_drafts.parse_spec` of THOSE bytes, every
     value it names cited to that drop; everything else is the store's (carried, bound, clocked);
  4. the ``noted`` receipt, so the desk never files a JSON spec as free text;
  5. the link row (``spec_card_taps``): proposal, drop, the bytes' hash, draft and revision.

A failure anywhere rolls back everything, so there is no half-written tap to retry and no second revision N+1. The
proposal's ``proposal_events`` row and the link's UNIQUE keys make the tap exactly-once across two devices.

A MISSION card (H1-5 lane 2) names several threads. Its tap is the same one transaction with two more writes at the
front: the mission is created from the card's thread set (his tap is what makes it; the room never does, and neither
does the door), then the mission's draft series is opened over every child (:func:`drafts._write_mission_revision`, which
reads the children under the lock and refuses what a mission envelope cannot cover), and only then the drop and the spec
revision. The tap still signs nothing and writes no envelope."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Sequence

from levain.autonomic.durable_sqlite import read_transaction
from levain.desk import envelope as env
from levain.desk import envelope_drafts as drafts
from levain.desk import missions as ms
from levain.desk import room
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import (SURFACES, CoordError, CoordStore, DropConflict, Source, _check_words, _OUTBOX_ID,
                               words_sha256)

__all__ = ["TapRefused", "Tapped", "check_mission_threads", "leaves_grant_running", "preview", "standing", "tap"]


class TapRefused(CoordError):
    """A tap the store refused with nothing written. ``code`` names why, for a surface to word:
    ``stale_preview`` (the bytes are not the reading he saw), ``thread_mismatch`` (the card's thread is not the one
    the client showed), ``not_a_spec_card``, ``settled`` (committed or dismissed already), ``expired``, ``replaced``
    (a newer card for the thread), ``thread_closed``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Tapped:
    """What a tap wrote (or, for a resend, what it had already written): the drop, the draft revision and the thread it
    is on (the stored one, never the request's echo)."""
    drop: Any
    draft_id: str
    revision: int
    created: bool
    thread_id: str
    mission_id: str | None = None       # set for a mission card's tap: the mission this tap made (or, on a resend, made)


def leaves_grant_running() -> bool:
    """THE one product call in this build (the plan's Q1): on a thread with a live signed grant, "Draft it" leaves that
    grant running until the new draft is signed (True, the recommendation) rather than stopping it at the tap. It is
    a function so that a change is one place: the tap attaches nothing, so True is what the store does; a False answer
    would add one explicit, labelled narrowing write to :func:`tap`, never an attach."""
    return True


def standing(conn: sqlite3.Connection, thread_id: str, now: str) -> dict[str, Any] | None:
    """The thread's signed head as it stands in force by the STORE's own facts (the effective body: the signed body less
    its dropped effects), or None when it has none or it is not live by every store fact the live check reads
    (:meth:`EnvelopeStore._store_refusal`: its thread moved past the revision it was signed for, it was killed or ended
    with its thread, it has stopped). Read-only and verifier-free: whether the signing key still holds is the sign
    step's check, so this is the card's diff base and its "a signed grant is running" line, never a grant decision."""
    try:
        head = EnvelopeStore._head(conn, thread_id)
        if head is None or EnvelopeStore._store_refusal(conn, now, head.envelope_id, thread_id) is not None:
            return None
        return EnvelopeStore._effective(conn, head.envelope_id, thread_id)
    except sqlite3.OperationalError:
        return None


def check_mission_threads(store: EnvelopeStore, conn: sqlite3.Connection, thread_ids: Sequence[str]) -> None:
    """Why ``thread_ids`` cannot be drafted together under one signature, as a :class:`CoordError` in plain words, or
    nothing. Read-only, on the caller's connection: the door runs it under its write lock so a card is never made for a
    set a tap could only refuse, and the tap's own writer (:func:`drafts._write_mission_revision`) holds the same
    rules again under the tap's lock. Each thread must be an open work thread outside any mission, hold at least one
    drop, hold no signed envelope yet, and have no draft series of its own open; and the threads must share a drop."""
    ids = sorted(set(thread_ids))
    if len(ids) != len(thread_ids) or len(ids) < 2:
        raise CoordError("a mission names two or more different threads")
    ms._member_check(conn, ids, mission_id=None)
    threads = [store._must_thread(conn, t) for t in ids]
    for t in threads:
        if t.kind != "work" or t.state != "open":
            raise CoordError(f"thread {t.thread_id} is {t.state}; a mission envelope covers open work threads")
        head = store._head(conn, t.thread_id)
        if head is not None:
            raise CoordError(f"thread {t.thread_id} already has envelope {head.envelope_id}: a mission envelope "
                             "covers threads that hold none yet")
        if not t.members:
            raise CoordError(f"thread {t.thread_id} holds no drops; nothing to bind")
        own = drafts._unclosed(conn, t.thread_id)
        if own is not None:
            raise CoordError(f"thread {t.thread_id} already holds its own unclosed draft ({own.draft_id}), and one "
                             "signature for a mission cannot be drafted beside it; sign or withdraw that draft first")
    drafts._shared_drop(conn, threads)


def plain_budget(budget: Any, n: int) -> tuple[str, str] | None:
    """``(each, together)`` in plain words for a signed budget shared by ``n`` threads: what ONE thread may use, and what
    ``n`` threads may use together (every set value times ``n``). None when the budget sets no value at all. The sign
    sheet and the room card say the same words, so both read them here."""
    if not isinstance(budget, dict):
        return None

    def say(k: str, v: int) -> str:
        return {"tool_calls": f"{v} tool calls", "tokens": f"{v} tokens", "dollars_cents": f"${v / 100:.2f}",
                "hours": f"{v} hours"}[k]

    keys = [k for k in env.BUDGET_KEYS if type(budget.get(k)) is int]
    if not keys:
        return None
    return ", ".join(say(k, budget[k]) for k in keys), ", ".join(say(k, budget[k] * n) for k in keys)


def _mission_lines(conn: sqlite3.Connection, store: EnvelopeStore, thread_ids: Sequence[str],
                   fields: dict[str, Any]) -> list[dict[str, Any]]:
    """What a mission card says in plain words beyond the spec's own lines: every child thread by title, that ONE
    signature covers exactly these threads, the budget each may use and what they may use together, and that a thread
    attached to the mission later is not covered. Pure over what it reads."""
    titles = [f"{store._must_thread(conn, t).title} ({t})" for t in thread_ids]
    n = len(titles)
    lines = [{"field": "mission", "wider": False,
              "text": f"one signature covers these {n} threads: " + "; ".join(titles)}]
    budget = fields.get("budget")
    if budget:
        each = ", ".join(f"{k} {budget[k]}" for k in env.BUDGET_KEYS if k in budget)
        total = ", ".join(f"{k} {budget[k] * n}" for k in env.BUDGET_KEYS if k in budget)
        lines.append({"field": "budget_together", "wider": False,
                      "text": f"each up to {each}, up to {n} x ({each}) together: {total}"})
    lines.append({"field": "mission_later", "wider": False,
                  "text": "a thread attached to this mission later is NOT covered: it needs a signature of its own"})
    return lines


def preview(store: EnvelopeStore, thread_id: str, words: str, *, threads: Sequence[str] | None = None) -> dict[str, Any]:
    """The plain-words reading of ``words`` as a tap would read them, for the card's thread: ``{"input_sha256", "lines",
    "signed_head"}``. The hash is the one :func:`tap` demands back. Writes nothing. Refused (CoordError) with the grammar's
    own sentence, the same one a tap would refuse with. For a MISSION card (``threads``, the card's whole set) the lines
    are the spec's own then the mission's (:func:`_mission_lines`), and ``signed_head`` is False: a mission is drafted
    only over threads that hold no envelope."""
    words = _check_words(words)
    fields, _named, stop_span = drafts.parse_spec(words, Source(), store)
    with read_transaction(store._connect) as conn:
        if threads is None:
            base = standing(conn, thread_id, store._stamp())
            extra: list[dict[str, Any]] = []
        else:
            base = None
            extra = _mission_lines(conn, store, threads, fields)
    return {"input_sha256": words_sha256(words), "lines": [*drafts.render_spec(fields, stop_span, base), *extra],
            "signed_head": base is not None}


def _link(conn: sqlite3.Connection, now: str, proposal_id: str, thread_id: str, words_sha256_: str, drop_id: str,
          draft_id: str, revision: int) -> None:
    try:
        conn.execute("INSERT INTO spec_card_taps (proposal_id, thread_id, words_sha256, drop_id, draft_id, revision, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (proposal_id, thread_id, words_sha256_, drop_id, draft_id, revision, now))
    except sqlite3.IntegrityError as exc:
        raise CoordError(str(exc)) from None


def _stored(store: CoordStore, proposal_id: str) -> tuple[str, str, int, str] | None:
    conn = store._connect()
    try:
        room.ensure(conn)
        row = conn.execute("SELECT drop_id, draft_id, revision, thread_id FROM spec_card_taps WHERE proposal_id = ?",
                           (proposal_id,)).fetchone()
    finally:
        conn.close()
    return None if row is None else (row[0], row[1], row[2], row[3])


def _mission_of(store: CoordStore, draft_id: str) -> str | None:
    """The mission a tapped draft's series was written for (its ``mission_drafts`` row), or None for a thread's draft."""
    conn = store._connect()
    try:
        row = conn.execute("SELECT mission_id FROM mission_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    except sqlite3.OperationalError:          # a store that never held a mission draft has no such table
        return None
    finally:
        conn.close()
    return None if row is None else row[0]


def tap(store: EnvelopeStore, proposal_id: str, words: str, outbox_id: str, surface: str, *, thread_echo: str,
        input_sha256: str) -> Tapped:
    """His tap on an envelope card: ``words`` are the bytes as the card's field held them (the room's prefill, or his
    edit), ``thread_echo`` the thread the page showed, ``input_sha256`` the hash of the reading he was shown for
    those bytes. Refused (:class:`TapRefused` or :class:`CoordError`) with nothing written when the bytes are not that
    reading, the card is not an open, un-replaced envelope card for ``thread_echo``, the thread is not open, or the
    grammar refuses the bytes. A resend of a stored ``outbox_id`` writes nothing and answers the stored tap."""
    if surface not in SURFACES:
        raise CoordError(f"a drop's surface is one of {', '.join(SURFACES)}")
    if not isinstance(outbox_id, str) or _OUTBOX_ID.fullmatch(outbox_id) is None:
        raise CoordError("an outbox_id is 8 to 128 of A-Z a-z 0-9 . _ - (starting with a letter or digit)")
    # outside the lock, as a rule reads its one non-store input: the bytes, parsed ONCE, and the hash he was shown
    words = _check_words(words)
    if input_sha256 != words_sha256(words):
        raise TapRefused("stale_preview", "these words are not the reading you were shown; read the card again")
    fields, named, stop_span = drafts.parse_spec(words, Source(), store)
    box: dict[str, Any] = {}

    def carry(conn: sqlite3.Connection, now: str, **drop: Any) -> Any:
        room.ensure(conn)
        p = room._proposal(conn, proposal_id)
        spec = room.spec_of(conn, proposal_id)
        if spec is None:
            raise TapRefused("not_a_spec_card", f"proposal {proposal_id} is not an envelope card")
        if p.state in room.PROPOSAL_KINDS:
            raise TapRefused("settled", f"proposal {proposal_id} is already {p.state}")
        if p.state != "open":
            raise TapRefused("expired", f"proposal {proposal_id} is {p.state}")
        newer = room.replaced_by(conn, proposal_id)
        if newer is not None:
            raise TapRefused("replaced", f"proposal {proposal_id} was replaced by the newer card {newer}")
        if thread_echo != spec.thread_id:
            raise TapRefused("thread_mismatch", f"card {proposal_id} is for another thread than the one shown")
        children = room.mission_threads(conn, proposal_id)
        for name in (spec.thread_id,) if children is None else children:
            thread = store._must_thread(conn, name)
            if thread.kind != "work" or thread.state != "open":
                raise TapRefused("thread_closed", f"thread {name} is {thread.state}; nothing to draft for")
        if children is None:
            series = drafts._unclosed(conn, spec.thread_id)
            if series is None:
                series = drafts._write_revision(store, conn, now, thread_id=spec.thread_id, draft_id=None, fields={},
                                                cites=[], parent=None, by="operator")
            write = lambda **kw: drafts._write_revision(store, conn, now, thread_id=spec.thread_id, **kw)   # noqa: E731
        else:
            # his tap makes the mission, here and nowhere else, inside this transaction: a fault anywhere below takes the
            # mission row back with the rest. The series is opened over the children as the lock reads them now
            box["mission_id"] = ms._create(conn, now, list(children), "operator").mission_id
            series = drafts._write_mission_revision(store, conn, now, box["mission_id"], draft_id=None, fields={},
                                                    cites=[], parent=None, by="operator")
            write = lambda **kw: drafts._write_mission_revision(store, conn, now, box["mission_id"], **kw)  # noqa: E731
        made = room._commit_rows(conn, now, store, proposal_id, wake=False, **drop)
        cites = [{"field": k, "kind": "drop", "ref": made.drop_id, "digest": made.words_sha256} for k in named]
        d = write(draft_id=series.draft_id, fields=fields, cites=cites, parent=series.digest, by="operator",
                  carry=True, stop_span=stop_span)
        store.note_in(conn, now, made.drop_id,
                      f"an envelope spec from room card {proposal_id}, drafted as {d.draft_id}#{d.revision}")
        _link(conn, now, proposal_id, spec.thread_id, made.words_sha256, made.drop_id, d.draft_id, d.revision)
        box["draft"] = d
        return made

    # CoordStore's own method: an EnvelopeStore's override takes no carrier, and a spec drop carries no envelope
    drop, created = CoordStore.accept_drop(store, words, surface=surface, outbox_id=outbox_id, _carry=carry)
    if created:
        d = box["draft"]
        return Tapped(drop, d.draft_id, d.revision, True, d.thread_id, box.get("mission_id"))
    stored = _stored(store, proposal_id)
    if stored is None or stored[0] != drop.drop_id:
        raise DropConflict(f"outbox_id {outbox_id} is the drop {drop.drop_id}, not this card's; nothing was written")
    if thread_echo != stored[3]:          # a resend is held to the thread check a first tap is (L3 r1 codex LOW)
        raise TapRefused("thread_mismatch", f"card {proposal_id} is for another thread than the one shown")
    return Tapped(drop, stored[1], stored[2], False, stored[3], _mission_of(store, stored[1]))
