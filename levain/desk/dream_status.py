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
  and the load draws a note the desk did not need (harmless), and none is missed;
* a rollback card is keyed by the occurrence of the save it undoes (``dream-rollback:<occurrence>``), so a rollback
  cancels exactly that occurrence's save, whatever stamp it carries.

The rotation itself is the desk's own act through ``coord close --kind dream`` (:mod:`levain.desk.seam_handoffs`), at a
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
           "seam_note", "status_lines"]

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


def records(conn: sqlite3.Connection) -> list[DreamRecord]:
    """The dream cards of the ``agent`` feed, newest first, a shadow night's save left out. A store with no
    ``review_items`` table has none. A card whose body cannot be read is skipped (the rest stand)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_items'").fetchone() is None:
        return []
    marks = " OR ".join("dedupe_key LIKE ?" for _ in _PREFIXES)
    out: list[DreamRecord] = []
    for at, body in conn.execute(f"SELECT at, body FROM review_items WHERE feed = 'agent' AND ({marks}) "
                                 "ORDER BY at DESC, rowid DESC LIMIT 200", [p + "%" for p in _PREFIXES]):
        try:
            doc = json.loads(body)
            kind = doc["kind"]
            occ = doc.get("occurrence")
            occurrence = str(occ.get("id")) if isinstance(occ, dict) else ""
            shadow = str(doc.get("remedy", "")).startswith(SHADOW_REMEDY)
        except (ValueError, KeyError, TypeError, AttributeError):
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
            "not in: never while he is typing, never in the middle of work. At that seam run "
            f"`{lv} coord close --kind dream --body-file PATH --request-id ID --prev-handoff-id H --token N{tail}`; "
            "the next desk loads the new memory. Nothing rotates you on its own.")


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


def _unacked_opens(conn: sqlite3.Connection, launch_id: str) -> list[int]:
    """Opens handed to ``launch_id`` (an ``open_deliveries`` row naming it) that no acknowledgement closed: the desk is
    still taking in a brief (``levain.desk.seam_open``; acked as :func:`levain.desk.seam_arrival.is_acked` reads it)."""
    from levain.desk.seam_open import open_tables_present

    if not open_tables_present(conn):
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT d.open_id FROM open_deliveries d WHERE d.recipient_launch_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM open_acks a WHERE a.open_id = d.open_id) ORDER BY d.open_id", (launch_id,))]


def rotation_due(store: object, launch: object) -> tuple[bool, str]:
    """``(due, why)``: whether the live desk ``launch`` is at a dream seam, i.e. a dream saved memory after it started
    AND it holds no claimed work and no unacknowledged open. A READ: one read-only transaction
    (``store.open_readonly``), nothing written, nothing rotated. The launch is re-read from the store by its id, so
    a stale object decides nothing. Every refusal is ``(False, why)``; a read that fails is ``(False, cannot-derive)``,
    never a seam.

    Decision, first match wins:

    1. no launch id, no such launch, or not an ``active`` desk launch: False;
    2. the newest save no rollback undid is missing or stamped at or before the launch's ``at`` (its request stamp;
       equal is not after): False, naming a rollback when a save after the start was rolled back; a shadow night's,
       a failed night's or a night that did not consolidate never counts as a save (:func:`records`);
    3. a sensor event is ``claimed`` or ``delivered`` to this launch (claimed work): False;
    4. an open delivered to this launch has no acknowledgement (its open turn: mid-arrival): False;
    5. otherwise True."""
    from levain.autonomic.durable_sqlite import read_transaction
    from levain.desk import lease as _leases
    from levain.desk.legacy import ROLE_DESK

    launch_id = getattr(launch, "launch_id", None)
    if not isinstance(launch_id, str) or not launch_id:
        return False, "no launch was named, so no seam can be read"
    try:
        with read_transaction(store.open_readonly) as conn:  # type: ignore[attr-defined]
            ln = _leases.launch(conn, launch_id)
            if ln is None:
                return False, f"no launch {launch_id} in this store"
            if ln.role != ROLE_DESK or ln.state != "active":
                return False, f"launch {launch_id} is a {ln.state} {ln.role} launch, not an active desk"
            started = ln.at
            recs = records(conn)
            work = _claimed_events(conn, launch_id)
            opens = _unacked_opens(conn, launch_id)
    except Exception as exc:  # noqa: BLE001 - a failed read is never a seam
        return False, f"cannot-derive ({type(exc).__name__}: {str(exc)[:160]})"
    saved, _ = _current(recs)
    if saved is None or saved.at <= started:
        undone = {r.occurrence for r in recs if r.kind == "rolled-back"}
        back = next((r for r in recs if r.kind == "consolidated" and r.at > started and r.occurrence in undone), None)
        if back is not None:
            return False, (f"the dream that saved at {back.at}, after this desk started ({started}), was rolled back: "
                           "a revert asks for no rotation")
        if saved is not None:
            return False, f"the last dream save ({saved.at}) is not after this desk started ({started})"
        return False, ("no dream has saved memory in this store (a failed night, a night that did not consolidate or a "
                       "shadow night's save is not one)")
    if work:
        return False, f"mid-work: {len(work)} sensor event(s) claimed by this desk and not acknowledged ({work[0]})"
    if opens:
        return False, f"mid-work: open {opens[0]} was handed to this desk and is not acknowledged"
    return True, (f"at a seam: the dream that saved at {saved.at} (occurrence {saved.occurrence}) saved after this desk "
                  f"started ({started}); no claimed event and no unacknowledged open")
