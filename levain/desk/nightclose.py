"""levain.desk.nightclose: the morning quiescence (ND-10a events, ND-10b started effects; night desk design 8.1 ND-10).

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
readable stop.

A STARTED EFFECT (ND-10b): the design's row reads "lets a started effect finish or records it unknown", and its DONE
is "no event or effect is left claimed and unowned after the stop". A started effect is an ``effect_attempts`` row still
``unknown`` (reserved, so it may have run; :mod:`levain.desk.effect_attempts`). The stop never settles, retries, re-sends
or deletes one: the journal is not its to write, so the effect FINISHES by itself (its runner settles the row when it
returns) and the stop only RECORDS it. When ``journal`` is given, the journal is read ONCE, before the fenced write opens (a read of every
historical attempt never holds the store's write lock), keeping the rows whose status is ``unknown`` at that moment; each
one then gets one write-once ``meta`` row ``nightclose_effect:<attempt id>:<owner launch>`` naming the owner, the event
and the verb, in the same fenced transaction as the events handed. The guarantee is exactly: EVERY ``unknown`` attempt in
the journal at the moment of that read is recorded. An effect reserved after the read (by a night launch still alive:
the stop gates event claims, not :meth:`levain.desk.effect_attempts.Journal.reserve`) is recorded by the NEXT close
(``test_an_effect_reserved_after_the_first_close_is_recorded_by_the_next`` pins it), so the day desk repeats the close
until its ``effects`` is empty. OWNED, for an effect, means exactly that record naming the lease holder's launch (the
effect's event, if still open, is handed to that same launch by the events half above): the day desk finds the effect
by the record and reads the journal's CURRENT status for it, so a record may describe a row that has since settled, and
a row that settled after the stop is its settled status, never the stop's guess. Known edge: a record is never retired
(bounded by owners x ``unknown`` rows). An effect the journal cannot be read for is no quiet: the call refuses and
writes nothing. NOT here (design silent): what the day desk does with a still-``unknown`` row, and its budget charge.
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

__all__ = ["NightClose", "NightCloseOwnerRefused", "NightCloseRefused", "EFFECT_PREFIX", "STOP_PREFIX", "nightclose", "stopped"]

STOP_PREFIX = "nightclose_stop:"
EFFECT_PREFIX = "nightclose_effect:"
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
    effects: tuple[int, ...] = ()   # attempt ids of the ``unknown`` effects this call recorded for the owner


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


def _effect_key(attempt_id: int, who: str) -> str:
    return f"{EFFECT_PREFIX}{attempt_id:0{_STOP_DIGITS}d}:{who}"


def _read_unknown(journal: Any) -> list[Any]:
    """The journal's attempts still ``unknown`` now, read outside any store transaction. A journal that cannot be
    read refuses."""
    if journal is None:
        return []
    try:
        return [a for a in journal.rows() if a.status == "unknown"]
    except Exception as exc:                            # noqa: BLE001 - any fault: no claim of quiescence
        raise NightCloseRefused("the effect journal could not be read, so no effect can be called owned: the nightclose "
                                f"writes nothing ({type(exc).__name__})") from exc


def _unrecorded(conn: sqlite3.Connection, unknown: list[Any], who: str) -> list[Any]:
    """The ``unknown`` attempts the owner has no record of yet (meta only: no journal read in the transaction)."""
    return [a for a in unknown
            if conn.execute("SELECT 1 FROM meta WHERE key = ?", (_effect_key(a.attempt_id, who),)).fetchone() is None]


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


def nightclose(store: Any, now: datetime, *, token: object, owner: str | None = None,
               journal: Any = None) -> NightClose:
    """Stop the night's claims and hand its open events to the lease holder's launch, under its lease ``token``
    (the write is fenced: a token that is not the live lease's refuses it). ``owner``, when given, must name that
    launch. ``now`` stamps the stop's record; the event states take the store's own clock, as every other write
    does. ``journal`` (an :class:`levain.desk.effect_attempts.Journal`) is where the started effects are read; with
    none, the effects half is not run (the events half is)."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise NightCloseRefused("the nightclose's time is an aware datetime")
    unknown = _read_unknown(journal)                    # before the fenced write: never under the store's write lock
    try:
        with store._write(ROLE_DESK, token) as (conn, stamp):
            return _close(conn, stamp, now, owner, unknown)
    except lease.LeaseRefused as exc:
        raise NightCloseRefused(f"the nightclose runs under the live lease, and nothing was written: {exc}") from exc


def _close(conn: sqlite3.Connection, stamp: str, now: datetime, owner: str | None, unknown: list[Any]) -> NightClose:
    who = _holder(conn, stamp, owner)
    started = _unrecorded(conn, unknown, who)
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
    if not todo and not new_stop and not started:
        return NightClose(who, False)
    if new_stop:
        g = next_gseq(conn, stamp)
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     (f"{STOP_PREFIX}{g:0{_STOP_DIGITS}d}", json.dumps({"owner": who, "at": utc_stamp(now)})))
    until = events._plus(stamp, events.CLAIM_TTL)
    for ev in todo:
        events._state(conn, stamp, ev.event_id, "claimed", launch_id=who, claim_until=until,
                      note="handed to the day desk at nightclose")
    for a in started:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                     (_effect_key(a.attempt_id, who), json.dumps({"owner": who, "event_id": a.event_id, "verb": a.verb,
                                                                  "at": utc_stamp(now)})))
    return NightClose(who, new_stop, tuple(e.event_id for e in todo), tuple(a.attempt_id for a in started))
