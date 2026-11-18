"""The dream's status for a desk (D1-L3): what the brief says of the last dream, and what a running desk is told when a
dream saved memory after it started.

READ ONLY. This module writes nothing and rotates nothing. The one record it reads is the ``agent`` feed of the desk's
own review store (``review_items``, inside the read the caller already holds): the cards ``levain.dream.card`` files
for a night, keyed ``dream:<occurrence>`` (kind ``consolidated`` or ``failed``), ``dream-night:<agent>:<day>`` (kind
``did-not-consolidate``) and ``dream-rollback:<occurrence>`` (kind ``rolled-back``). A card is filed after the run's
effect, so its ``at`` is the moment the dream's save was KNOWN, a few seconds after the save itself.

Invariants this relies on, so a change to one of them is a change here:

* a ``consolidated`` card is filed only for a save (``agent._file_run_card``: outcome ``saved``), once per dedupe key;
* a card's ``at`` is a fixed-width UTC stamp, so stamps compare as text and parse with ``consent.parse_stamp``;
* a SHADOW night files a ``consolidated`` card too (``shadow.shadow_card``), with the remedy :data:`SHADOW_REMEDY`:
  nothing was saved to a real store, so it never counts as a dream that wrote memory ("a shadow install emits no dream
  signal"); a test holds the constant equal to the shadow card's remedy;
* a desk's launch row ``at`` is the stamp the launch was REQUESTED (``launches.at``, written with the pending event),
  before the process is spawned or activated, and a desk loads the memory text when it starts, so a save after that
  stamp may be memory it does not hold. Earlier than the load, never later: a dream that saves between the request
  and the load reads as a seam (:func:`rotation_due`) for memory the desk already holds: the cost is an extra rotation,
  and none is missed;
* a rollback card is keyed by the occurrence of the save it undoes (``dream-rollback:<occurrence>``), so a rollback
  cancels exactly that occurrence's save, whatever stamp it carries.

The rotation itself is the desk's own act through :func:`levain.desk.dream_rotation.rotate` (called by the ``coord open`` verb that takes a dream body), at a
seam it is not in. Nothing here starts it, fences it or authorises it: :func:`rotation_due` only SAYS whether a desk is
at such a seam with a dream to rotate onto, and writes nothing. Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from levain.cockpit.consent import parse_stamp
from levain.desk.review import FEEDS

__all__ = ["DREAM_NOTE", "DREAM_UNREAD", "SHADOW_REMEDY", "STALE_HOURS", "DreamRecord", "records", "rotation_due",
           "seam_check", "seam_note", "status_lines"]

assert "agent" in FEEDS

#: the remedy ``levain.dream.shadow.shadow_card`` files: how a shadow night's card is told from a real save
SHADOW_REMEDY = "nothing to undo: the shadow store is a scratch copy"
#: hours since the last saved dream past which the brief says "no dream since". The design names a threshold and no
#: number; a daily dream's gap is 24 hours plus its retry window, so this is a daily dream plus six hours of slack
STALE_HOURS = 30
_KINDS = ("consolidated", "failed", "did-not-consolidate", "rolled-back")
_PREFIXES = ("dream:", "dream-night:", "dream-rollback:")

#: what the degraded brief says in place of the dream status: the store was not read
DREAM_UNREAD = "Dream: cannot-derive (no store was read, so the last dream and the memory's freshness are unknown)"
#: the constant line ``coord open --verbose`` prints (the brief's own Dream line is read from the store)
DREAM_NOTE = ("Dream: the brief's Dream line reads this store's dream cards: the last dream that saved, a later night "
              "that failed or did not consolidate, and \"no dream since X\" when nothing has saved for more than "
              f"{STALE_HOURS} hours; a running desk keeps the memory it loaded and is told, on its next open, when a "
              "dream saved after it started")


@dataclass(frozen=True)
class DreamRecord:
    kind: str          # one of the card kinds
    at: str            # the card's filing stamp
    occurrence: str


def records(conn: sqlite3.Connection, strict: bool = False) -> list[DreamRecord]:
    """The dream cards of the ``agent`` feed, newest first, a shadow night's save left out. A store with no
    ``review_items`` table has none. A card whose body cannot be read is skipped (the rest stand): the brief's
    display. ``strict`` (the rotation's read, :func:`seam_check`) raises :class:`ValueError` instead, naming the
    card, for a body that is not a JSON object with a text ``kind``, a stamp that is not text, and a save or rollback
    card with no occurrence id: a rollback that cannot be paired with its save would leave that save live, so the
    predicate fails closed where the display skips."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_items'").fetchone() is None:
        return []
    marks = " OR ".join("dedupe_key LIKE ?" for _ in _PREFIXES)
    out: list[DreamRecord] = []
    for at, body, key in conn.execute(f"SELECT at, body, dedupe_key FROM review_items WHERE feed = 'agent' AND "
                                      f"({marks}) ORDER BY at DESC, rowid DESC LIMIT 200",
                                      [p + "%" for p in _PREFIXES]):
        try:
            doc = json.loads(body)
            kind = doc["kind"]
            occ = doc.get("occurrence")
            occurrence = str(occ.get("id")) if isinstance(occ, dict) else ""
            shadow = str(doc.get("remedy", "")).startswith(SHADOW_REMEDY)
            unpaired = kind in ("consolidated", "rolled-back") and (not isinstance(occ, dict)
                                                                    or occ.get("id") in (None, ""))
            if strict and (not isinstance(kind, str) or not isinstance(at, str) or unpaired):
                raise ValueError("no text kind, no text stamp, or a save or rollback with no occurrence id")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            if strict:
                raise ValueError(f"dream card {key} cannot be read ({type(exc).__name__}: {exc})") from None
            continue
        if kind in _KINDS and not (kind == "consolidated" and shadow):
            out.append(DreamRecord(kind, at, occurrence))
    return out


def _hm(stamp: str) -> str:
    return parse_stamp(stamp).strftime("%H:%M")


def _age(now: datetime, stamp: str) -> str:
    secs = int((now - parse_stamp(stamp)).total_seconds())
    if secs < 0:
        return "in the future"
    hours, rem = divmod(secs, 3600)
    return f"{hours}h{rem // 60:02d}m ago" if hours else f"{rem // 60}m ago"


def _current(recs: list[DreamRecord]) -> tuple[DreamRecord | None, DreamRecord | None]:
    """``(live, back)``: the newest save no rollback of the same occurrence undid, and the rollback of the NEWEST save
    when it was undone (None when the newest save stands). A rollback of an older occurrence says nothing of the
    memory now held."""
    undone = {r.occurrence: r for r in recs if r.kind == "rolled-back"}
    saves = [r for r in recs if r.kind == "consolidated"]
    live = next((r for r in saves if r.occurrence not in undone), None)
    return live, (undone.get(saves[0].occurrence) if saves else None)


def status_lines(recs: list[DreamRecord], now: datetime) -> list[str]:
    """The brief's Dream lines at ``now`` (aware), or none when no dream card was ever filed here (an install that
    never dreamed says nothing it cannot know). ``no dream since X`` when nothing has saved for more than
    :data:`STALE_HOURS`, or when a later night failed or did not consolidate."""
    if not recs:
        return []
    saved, reverted = _current(recs)
    later = [r for r in recs if r.kind in ("failed", "did-not-consolidate") and (saved is None or r.at > saved.at)]
    lines: list[str] = []
    if saved is None:
        last = recs[0]
        lines.append(f"Dream: no dream has saved memory in this store; the last outcome is {last.kind}, {last.at} "
                     f"({_age(now, last.at)})")
    else:
        stale = (now - parse_stamp(saved.at)).total_seconds() > STALE_HOURS * 3600
        if stale or later:
            # recs is newest first: the night after the save is the OLDEST later outcome
            why = f"; the night after it {later[-1].kind} ({later[-1].at})" if later else ""
            lines.append(f"Dream: no dream since {saved.at} ({_age(now, saved.at)}){why}")
        else:
            lines.append(f"Dream: memory refreshed by the {_hm(saved.at)} dream, {saved.at} ({_age(now, saved.at)})")
    if reverted is not None:
        lines.append(f"Dream: memory reverted at {_hm(reverted.at)} ({reverted.at}); a revert asks for no rotation")
    return lines


def seam_note(recs: list[DreamRecord], started_at: str, lv: str, tail: str) -> str | None:
    """What a running desk that started at ``started_at`` is told: the newest dream that saved memory after it
    started and was not rolled back, or None when none did. ``lv`` and ``tail`` spell the close command as the launch brief spells it."""
    saved, _ = _current(recs)
    if saved is None or saved.at <= started_at:
        return None
    return (f"Dream seam: the {_hm(saved.at)} dream saved new memory at {saved.at}, after this desk started "
            f"({started_at}); this desk holds the memory from before it. Hand over onto it only at a seam you are "
            "not in: never while he is typing, never in the middle of work. At that seam write the handoff body "
            f"(JSON) to PATH and run `{lv} coord open --dream-body-file PATH --token N{tail}` (no --light, no --ack): "
            "it writes the dream handoff and asks for the next desk, which loads the new memory; "
            f"`{lv} coord close --kind dream --body-file PATH --request-id ID --prev-handoff-id H --token N{tail}` "
            "writes the handoff alone. Nothing rotates you on its own.")


def loaded_at(conn: sqlite3.Connection, launch_id: str, requested_at: str) -> str:
    """When the launch loaded its memory: the stamp of its ``active`` event (the session starts and reads memory at
    activation). ``launches.at`` is the REQUEST stamp, earlier than activation by however long the successor waited; a
    launch with no ``active`` event yet falls back to the request stamp, the earliest it could have loaded."""
    row = conn.execute("SELECT at FROM launch_events WHERE launch_id = ? AND kind = 'active' ORDER BY seq LIMIT 1",
                       (launch_id,)).fetchone()
    return requested_at if row is None or not isinstance(row[0], str) else row[0]


def rollback_notes(conn: sqlite3.Connection, recs: list[DreamRecord], started_at: str) -> list[str]:
    """The rollback-after-rotation notice (levain#86): one line per dream a rotation handed over onto that was rolled
    back after the desk loaded it. ``started_at`` is the LOAD stamp (:func:`loaded_at`), not the launch's request
    stamp. READ ONLY, and derived, not stored: the notice is the pairing of three records the store already holds, so
    there is nothing to write once and nothing to acknowledge.

    A line is told for an occurrence when (1) a ``consolidated`` card of it was filed no later than ``started_at``
    (this desk's memory included that save; the newest such card decides), (2) a ``rolled-back`` card of it was filed
    after ``started_at`` (the text this desk loaded was undone while it ran), and (3) the rotation wrote a handoff
    under ``dream-rotation:<occurrence>`` (:data:`levain.desk.dream_rotation.KEY_PREFIX`). A rollback of a dream no
    rotation used says nothing here. A desk that loaded after the rollback holds the restored text and is told
    nothing. The line tells; it opens, ends and launches nothing, and asks nothing of the desk. Lines come newest
    rollback first, the order :func:`records` returns its cards (filing stamp, then insertion order, descending)."""
    from levain.desk.dream_rotation import KEY_PREFIX

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'seam_handoffs'").fetchone() is None:
        return []
    saves: dict[str, DreamRecord] = {}
    for r in recs:  # newest first: the first save at or before the load is the newest such
        if r.kind == "consolidated" and r.at <= started_at:
            saves.setdefault(r.occurrence, r)
    out: list[str] = []
    told: set[str] = set()  # newest first: only the newest rollback of an occurrence is told
    for back in (r for r in recs if r.kind == "rolled-back"):
        save = saves.get(back.occurrence)
        if save is None or back.at <= started_at or back.occurrence in told:
            continue
        told.add(back.occurrence)
        if conn.execute("SELECT 1 FROM seam_handoffs WHERE close_key = ?", (KEY_PREFIX + back.occurrence,)).fetchone() is None:
            continue
        out.append(f"Dream rolled back: the dream of occurrence {back.occurrence} (saved {save.at}) was rolled back at "
                   f"{back.at}, after the dream rotation that handed over onto it; this "
                   f"desk loaded its memory at {started_at} and holds the memory that dream saved, which is now undone. This is a "
                   "notice only: nothing was opened, ended or launched for you.")
    return out


def _claimed_events(conn: sqlite3.Connection, launch_id: str) -> list[str]:
    """Sensor events whose latest lifecycle row hands them to ``launch_id`` (``claimed`` or ``delivered``): work its
    brief took and it has not acknowledged (``levain.desk.events``). An expired claim still counts until a pass writes
    its ``retried``: the store still says this desk holds it."""
    from levain.desk.events import LIFECYCLE

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_states'").fetchone() is None:
        return []
    kinds = ", ".join(repr(k) for k in LIFECYCLE)
    return [r[0] for r in conn.execute(
        f"SELECT s.event_id FROM event_states s WHERE s.seq = (SELECT MAX(seq) FROM event_states WHERE event_id = "
        f"s.event_id AND kind IN ({kinds})) AND s.kind IN ('claimed', 'delivered') AND s.launch_id = ? ORDER BY s.seq",
        (launch_id,))]


def _unacked_opens(conn: sqlite3.Connection, launch_id: str, session_key: str) -> list[int]:
    """Opens handed to ``launch_id`` (an ``open_deliveries`` row naming it) that ITS OWN session has not acknowledged:
    the desk is still taking in a brief (``levain.desk.seam_open``). An ack is keyed ``(open_id, reader_session)``
    and one build can be handed to several launches (a stored build is returned, never rebuilt, to the next desk), so
    another session's ack of the same open closes nothing here."""
    from levain.desk.seam_open import open_tables_present

    if not open_tables_present(conn):
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT d.open_id FROM open_deliveries d WHERE d.recipient_launch_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM open_acks a WHERE a.open_id = d.open_id AND a.reader_session = ?) ORDER BY d.open_id",
        (launch_id, session_key))]


def seam_check(conn: sqlite3.Connection, launch_id: str, now: str, *, at_turn_boundary: bool) -> tuple[bool, str]:
    """``(due, why)`` on the CALLER's connection, inside the caller's transaction, at the store clock ``now``: the
    one derivation :func:`rotation_due` runs in its own read. The rotation (levain#82) runs it again inside the write
    transaction that would rotate, so the answer it acts on is the store as that transaction sees it. Writes
    nothing; raises when the store cannot be read (:func:`rotation_due` turns that into ``cannot-derive``).

    THE INVARIANT. A desk is at a dream seam iff

    (A) it is the store's live desk: an ``active`` launch of the desk role whose session holds the live lease;
    (B) the store holds a dream save it has not loaded: the newest ``consolidated`` card (never a shadow night's) whose
        occurrence no rollback undid was filed strictly after the launch was requested (``launches.at``);
    (C) the store hands it nothing it has not closed: no sensor event whose latest lifecycle row is ``claimed`` or
        ``delivered`` to it, and no open delivered to it that its own session has not acknowledged;
    (D) its harness is between turns.

    What "open turn" means in the store: nothing. The store records no desk turn (a desk is a terminal session; only
    worker and room launches write ``turn_started`` / ``turn_ended``). It records an open's ARRIVAL (delivered, then
    acknowledged by the reader's session) and the work a turn takes on (an event ``claimed`` until ``events.ack``).
    An acknowledgement is the FIRST act of a turn, never its end, so (C) holding says only that the store hands the
    desk nothing; an acknowledged open whose events are still claimed fails (C) through the claim. (D) is not on the
    store at all, so it is the caller's input ``at_turn_boundary``, read where the caller reads it (the session
    listing, as :func:`levain.desk.lease.replace_done`'s caller reads a done session, or the desk's own ``coord open``
    before it takes anything new). Only ``True`` (the bool) says "between turns".

    Each False reason is one conjunct failing, in this order (first match wins):
    (A) no such launch, not an active desk, or its session does not hold the live lease;
    (B) the save rolled back, all saves rolled back, the save not after the request, or no save at all;
    (C) a claimed or delivered event, then an open this session has not acknowledged ("mid-work");
    (D) ``at_turn_boundary`` is not True ("mid-turn")."""
    from levain.desk import lease as _leases
    from levain.desk.legacy import ROLE_DESK

    ln = _leases.launch(conn, launch_id)
    if ln is None:
        return False, f"no launch {launch_id} in this store"
    if ln.role != ROLE_DESK or ln.state != "active":
        return False, f"launch {launch_id} is a {ln.state} {ln.role} launch, not an active desk"
    held = _leases.lease(conn, now)
    if held is None or not held.live or held.holder != ln.session_key:
        state = "not held" if held is None else "past its until" if not held.live else f"held by {held.holder}"
        return False, f"launch {launch_id} does not hold the live lease (the lease is {state}), so it is not the live desk"
    started = ln.at
    recs = records(conn, strict=True)
    saved, _ = _current(recs)
    if saved is None or saved.at <= started:
        undone = {r.occurrence for r in recs if r.kind == "rolled-back"}
        back = next((r for r in recs if r.kind == "consolidated" and r.at > started and r.occurrence in undone), None)
        if back is not None:
            return False, (f"the dream that saved at {back.at}, after this desk started ({started}), was rolled back: "
                           "a revert asks for no rotation")
        if saved is not None:
            return False, f"the last dream save ({saved.at}) is not after this desk started ({started})"
        if any(r.kind == "consolidated" for r in recs):
            return False, ("every dream save in this store was rolled back, none after this desk started "
                           f"({started}): no memory to rotate onto")
        return False, ("no dream has saved memory in this store (a failed night, a night that did not consolidate or a "
                       "shadow night's save is not one)")
    work = _claimed_events(conn, launch_id)
    if work:
        return False, (f"mid-work: {len(work)} sensor event(s) handed to this desk (claimed or delivered) and not "
                       f"acknowledged ({work[0]})")
    opens = _unacked_opens(conn, launch_id, ln.session_key)
    if opens:
        return False, f"mid-work: open {opens[0]} was handed to this desk and this desk has not acknowledged it"
    if at_turn_boundary is not True:
        return False, ("mid-turn: the store hands this desk nothing open, but the caller has not read a turn boundary "
                       "(the store records no desk turn, and an acknowledgement begins one)")
    return True, (f"at a seam: the dream that saved at {saved.at} (occurrence {saved.occurrence}) saved after this desk "
                  f"started ({started}); it holds the live lease, no claimed event and no open it has not "
                  "acknowledged, and the caller read a turn boundary")


def rotation_due(store: object, launch: object, *, at_turn_boundary: bool) -> tuple[bool, str]:
    """``(due, why)``: whether the live desk ``launch`` is at a dream seam (:func:`seam_check` holds THE INVARIANT and
    derives every reason from it). A READ: one read-only transaction (``store.open_readonly``, one consistent
    snapshot), nothing written, nothing rotated. The launch is re-read from the store by its id, so a stale object
    decides nothing. Every refusal is ``(False, why)``; a read or a derivation that fails is
    ``(False, cannot-derive)``, never a seam and never a raise.

    ``at_turn_boundary`` has no default: the store cannot say whether the harness is mid-response, so the caller
    says where it reads the boundary.

    THE CALLER'S CONTRACT (levain#82). True is this snapshot's answer and nothing after it: an event claimed or an
    open delivered after the read is not in it, and no read can fence a later writer. So the rotation never acts on
    this answer alone: inside the write transaction that writes the dream handoff and requests the next launch (under
    the desk's token fence), it runs :func:`seam_check` on that transaction's connection and rotates only on its
    True. That re-check is the answer it acts on; this one is advisory (what to say, whether to try)."""
    from levain.autonomic.durable_sqlite import read_transaction

    launch_id = getattr(launch, "launch_id", None)
    if not isinstance(launch_id, str) or not launch_id:
        return False, "no launch was named, so no seam can be read"
    try:
        with read_transaction(store.open_readonly) as conn:  # type: ignore[attr-defined]
            now = store._stamp()  # type: ignore[attr-defined]
            return seam_check(conn, launch_id, now, at_turn_boundary=at_turn_boundary)
    except Exception as exc:  # noqa: BLE001 - a failed read is never a seam
        return False, f"cannot-derive ({type(exc).__name__}: {str(exc)[:160]})"
