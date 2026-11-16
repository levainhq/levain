"""levain.desk.nightclose: the events half of the morning quiescence (ND-10a; night desk design 8.1 ND-10).

The fresh day desk runs :func:`nightclose` under its own lease token once the night is over. It does two things, in
one fenced transaction:

* STOPS new claims by every launch created before the stop: it writes one ``nightclose_stop:<gseq>`` row in the
  store's write-once ``meta`` table, and :func:`levain.desk.events.claim_for_launch` asks :func:`stopped` first. A
  launch is placed by its CREATION (its ``launches.gseq``), not its spawn: one asked for before the stop and spawned
  after it is stopped (the conservative reading). A launch created after the stop (the next night's) claims as before,
  and so does the owner.
* HANDS every open trusted event nobody named holds to the day desk's launch: an event ``claimed`` or ``delivered`` by
  another launch, and an event ``retried`` (back to available, so owned by none), is written ``claimed`` for the owner.
  The owner is the lease holder's launch, proven by the token the write is fenced with; ``owner``, when given, must
  name that same launch. Then no event is left claimed, delivered or retried without a named owner. A ``received``
  event is held by no one and stays for the owner's brief; an untrusted one is never handed to a desk (it is alerted,
  :mod:`levain.desk.events`).

Each call is one episode with its own cutoff: a call writes a fresh stop unless the latest one is the same owner's and
no other launch was created since it. So a launch created after a stop and holding events is stopped by the next call
before its events are handed away, and a retry where nothing changed writes no stop and no event state (the fenced
write still records its lease ``beat``, :func:`levain.desk.lease.record_write`). A ``meta`` row that names a stop
but whose record cannot be read stops every launch created before it, the owner too, until a fresh call writes a
readable stop. NOT here: a started effect (finish or record unknown) needs ``effect_attempts`` and is levain#19.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.desk import events, lease
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, next_gseq

__all__ = ["NightClose", "NightCloseOwnerRefused", "NightCloseRefused", "STOP_PREFIX", "nightclose", "stopped"]

STOP_PREFIX = "nightclose_stop:"
_STOP_DIGITS = 20
_STOP_GLOB = STOP_PREFIX + "[0-9]" * _STOP_DIGITS     # GLOB matches the prefix literally, case and _ included


class NightCloseRefused(CoordError):
    """The nightclose has no day desk to hand to; nothing was written."""


class NightCloseOwnerRefused(NightCloseRefused):
    """``owner`` names a launch that is not the live lease holder's; nothing was written."""


@dataclass(frozen=True)
class NightClose:
    owner: str                      # the launch the open events are handed to
    stopped: bool                   # this call wrote a stop (False: the latest one already covers every launch)
    handed: tuple[str, ...] = ()    # event ids this call wrote ``claimed`` for the owner


def _latest_stop(conn: sqlite3.Connection) -> tuple[int, str | None] | None:
    """The latest stop's ``(cutoff gseq, owner)``; owner None when its record cannot be read (fail closed)."""
    row = conn.execute("SELECT key, value FROM meta WHERE key GLOB ? ORDER BY key DESC LIMIT 1",
                       (_STOP_GLOB,)).fetchone()
    if row is None:
        return None
    cutoff = int(row[0][len(STOP_PREFIX):])          # the GLOB admits only the fixed-width digits
    try:
        owner = json.loads(row[1])["owner"]
    except (TypeError, ValueError, KeyError):
        owner = None
    return cutoff, owner if isinstance(owner, str) else None


def _created(conn: sqlite3.Connection, launch_id: str) -> int | None:
    row = conn.execute("SELECT gseq FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()
    return None if row is None else row[0]


def stopped(conn: sqlite3.Connection, launch_id: str) -> bool:
    """True when ``launch_id`` was created before the latest stop (or is no launch) and is not that stop's owner: it
    claims nothing."""
    got = _latest_stop(conn)
    if got is None:
        return False
    cutoff, owner = got
    if launch_id == owner:
        return False
    created = _created(conn, launch_id)
    return created is None or created < cutoff


def _holder(conn: sqlite3.Connection, now: str, owner: str | None) -> str:
    """The launch the fenced lease's holder is; ``owner``, when given, must be it."""
    held = lease.lease(conn, now)       # the fence let the token through: a live lease, its holder active (or you)
    if held is None or held.holder == "operator":
        raise NightCloseRefused("no desk launch holds the lease: the nightclose is the day desk's, run under its own "
                                "lease, and nothing was written")
    ln = lease._launch_by_key(conn, held.holder)
    if ln is None or ln.role != ROLE_DESK:
        raise NightCloseRefused("the lease holder is no desk launch: nothing was written")
    if owner is not None and owner != ln.launch_id:
        raise NightCloseOwnerRefused(f"launch {str(owner)[:40]!r} is not the lease holder's launch ({ln.launch_id}); "
                                     "the night's events go only to the desk holding the lease, and nothing was "
                                     "written")
    return ln.launch_id


def nightclose(store: Any, now: datetime, *, token: object, owner: str | None = None) -> NightClose:
    """Stop the night's claims and hand its open events to the lease holder's launch, under its lease ``token``
    (the write is fenced: a token that is not the live lease's refuses it). ``owner``, when given, must name that
    launch. ``now`` stamps the stop's record; the event states take the store's own clock, as every other write
    does."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise NightCloseRefused("the nightclose's time is an aware datetime")
    try:
        with store._write(ROLE_DESK, token) as (conn, stamp):
            return _close(conn, stamp, now, owner)
    except lease.LeaseRefused as exc:
        raise NightCloseRefused(f"the nightclose runs under the live lease, and nothing was written: {exc}") from exc


def _close(conn: sqlite3.Connection, stamp: str, now: datetime, owner: str | None) -> NightClose:
    who = _holder(conn, stamp, owner)
    prior = _latest_stop(conn)
    # a fresh cutoff per episode: unless the latest stop is this owner's and no other launch was created since it.
    # Every launch but the owner then sits before the cutoff in force, so every event handed below is a stopped one's
    new_stop = prior is None or prior[1] != who or conn.execute(
        "SELECT 1 FROM launches WHERE gseq > ? AND launch_id != ?", (prior[0], who)).fetchone() is not None
    todo = []
    for (eid,) in conn.execute("SELECT event_id FROM events WHERE trust != 'untrusted' ORDER BY gseq").fetchall():
        ev = events._event(conn, eid)
        if ev.state == "retried" or (ev.state in ("claimed", "delivered") and ev.launch_id != who):
            todo.append(ev)
    if not todo and not new_stop:
        return NightClose(who, False)
    if new_stop:
        g = next_gseq(conn, stamp)
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     (f"{STOP_PREFIX}{g:0{_STOP_DIGITS}d}", json.dumps({"owner": who, "at": utc_stamp(now)})))
    until = events._plus(stamp, events.CLAIM_TTL)
    for ev in todo:
        events._state(conn, stamp, ev.event_id, "claimed", launch_id=who, claim_until=until,
                      note="handed to the day desk at nightclose")
    return NightClose(who, new_stop, tuple(e.event_id for e in todo))
