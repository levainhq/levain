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
proposal's ``proposal_events`` row and the link's UNIQUE keys make the tap exactly-once across two devices."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.desk import envelope_drafts as drafts
from levain.desk import room
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import (SURFACES, CoordError, CoordStore, DropConflict, Source, _check_words, _OUTBOX_ID,
                               words_sha256)

__all__ = ["TapRefused", "Tapped", "leaves_grant_running", "preview", "standing", "tap"]


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


def preview(store: EnvelopeStore, thread_id: str, words: str) -> dict[str, Any]:
    """The plain-words reading of ``words`` as a tap would read them, for the card's thread: ``{"input_sha256", "lines",
    "signed_head"}``. The hash is the one :func:`tap` demands back. Writes nothing. Refused (CoordError) with the grammar's
    own sentence, the same one a tap would refuse with."""
    words = _check_words(words)
    fields, _named, stop_span = drafts.parse_spec(words, Source(), store)
    with read_transaction(store._connect) as conn:
        base = standing(conn, thread_id, store._stamp())
    return {"input_sha256": words_sha256(words), "lines": drafts.render_spec(fields, stop_span, base),
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
        thread = store._must_thread(conn, spec.thread_id)
        if thread.kind != "work" or thread.state != "open":
            raise TapRefused("thread_closed", f"thread {spec.thread_id} is {thread.state}; nothing to draft for")
        series = drafts._unclosed(conn, spec.thread_id)
        if series is None:
            series = drafts._write_revision(store, conn, now, thread_id=spec.thread_id, draft_id=None, fields={},
                                            cites=[], parent=None, by="operator")
        made = room._commit_rows(conn, now, store, proposal_id, wake=False, **drop)
        cites = [{"field": k, "kind": "drop", "ref": made.drop_id, "digest": made.words_sha256} for k in named]
        d = drafts._write_revision(store, conn, now, thread_id=spec.thread_id, draft_id=series.draft_id, fields=fields,
                                   cites=cites, parent=series.digest, by="operator", carry=True, stop_span=stop_span)
        store.note_in(conn, now, made.drop_id,
                      f"an envelope spec from room card {proposal_id}, drafted as {d.draft_id}#{d.revision}")
        _link(conn, now, proposal_id, spec.thread_id, made.words_sha256, made.drop_id, d.draft_id, d.revision)
        box["draft"] = d
        return made

    # CoordStore's own method: an EnvelopeStore's override takes no carrier, and a spec drop carries no envelope
    drop, created = CoordStore.accept_drop(store, words, surface=surface, outbox_id=outbox_id, _carry=carry)
    if created:
        d = box["draft"]
        return Tapped(drop, d.draft_id, d.revision, True, d.thread_id)
    stored = _stored(store, proposal_id)
    if stored is None or stored[0] != drop.drop_id:
        raise DropConflict(f"outbox_id {outbox_id} is the drop {drop.drop_id}, not this card's; nothing was written")
    if thread_echo != stored[3]:          # a resend is held to the thread check a first tap is (L3 r1 codex LOW)
        raise TapRefused("thread_mismatch", f"card {proposal_id} is for another thread than the one shown")
    return Tapped(drop, stored[1], stored[2], False, stored[3])
