"""levain.agents.controller — the occurrence claim and the controller tick (A0 r7 §2.5, §3.9; BUILD_PLAN P6-1).

Later slices add their code here in the owner order P6-2, P6-3a, P6-5, P6-4, D1-2.

``claim`` is one plain ``INSERT``. ``agent_occurrences_one_fire`` raises ABORT, which no ``OR`` clause turns into a
skip, so a second delivery of one key is refused by the trigger, writes nothing, and is answered from the row that is
there. The ``INSERT`` is the claim's first statement, so it takes the write lock before it reads: a caller in a
deferred ``BEGIN`` that has read nothing yet gets the same answer as one in ``BEGIN IMMEDIATE``. A deferred caller
that read first holds a snapshot and gets SQLite's busy error, which writes nothing.

``tick`` walks every registered, active agent's slots since its stored cursor, in the agent's stored zone, with an
injected clock. What it needs from outside the store comes in as ``binding_of`` (``(agent_id, spec_hash) ->
binding_id | None``; ``None`` means the agent is not armed), because arming is derived from the authority log and a
binding is minted by the launch slice, neither of which is this module's.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from levain.agents import schedule
from levain.agents.spec import _check_row
from levain.agents.store import AgentStore, open_agent_store

__all__ = ["CLAIMED", "DUPLICATE", "OVERLAP", "TickResult", "occurrence_id", "run_id", "claim", "tick"]

CLAIMED = "claimed"
DUPLICATE = "duplicate"
OVERLAP = "skipped-overlap"

CURSOR_PREFIX = "tick_cursor:"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _digest(agent_id: str, kind: str, key: str) -> str:
    # authority._canonical's form
    text = json.dumps({"agent_id": agent_id, "kind": kind, "key": key}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def occurrence_id(agent_id: str, kind: str, key: str) -> str:
    return "oc-" + _digest(agent_id, kind, key)


def run_id(agent_id: str, kind: str, key: str) -> str:
    return "run-" + _digest(agent_id, kind, key)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def claim(conn: sqlite3.Connection, *, agent_id: str, kind: str, key: str, spec_hash: str, binding_id: str,
          window_key: str, now: datetime) -> tuple[str, str]:
    """Claim one key. Returns ``(occurrence_id, outcome)``: ``claimed`` (a new ``admitted`` row), ``skipped-overlap``
    (a new key while the agent's previous occurrence is live: its own row, so the miss count sees it) or
    ``duplicate`` (the key already has a row; nothing is written). It runs inside the caller's transaction so the
    claim commits with whatever else the caller writes. ``now`` must be timezone-aware."""
    if not conn.in_transaction:
        raise RuntimeError("a claim runs inside the caller's write transaction")
    at = _stamp(schedule._utc(now))  # a naive datetime would be read in the host's zone
    oid = occurrence_id(agent_id, kind, key)
    try:
        conn.execute(
            "INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, reason, window_key, spec_hash,"
            " binding_id, run_id, admitted_at, ended_at)"
            " SELECT ?, ?, ?, ?, CASE WHEN live THEN 'skipped-overlap' ELSE 'admitted' END,"
            " CASE WHEN live THEN 'overlap' END, ?, ?, ?, ?, ?, CASE WHEN live THEN ? END"
            " FROM (SELECT EXISTS (SELECT 1 FROM agent_occurrences WHERE agent_id = ?"
            " AND state IN ('admitted','launched')) AS live)",
            (oid, agent_id, kind, key, window_key, spec_hash, binding_id, run_id(agent_id, kind, key), at, at,
             agent_id))
    except sqlite3.IntegrityError:
        if conn.execute("SELECT 1 FROM agent_occurrences WHERE agent_id = ? AND kind = ? AND key = ?",
                        (agent_id, kind, key)).fetchone() is None:
            raise
        return oid, DUPLICATE
    state = conn.execute("SELECT state FROM agent_occurrences WHERE occurrence_id = ?", (oid,)).fetchone()[0]
    return oid, (OVERLAP if state == "skipped-overlap" else CLAIMED)


@dataclass
class TickResult:
    claimed: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, key)
    overlapped: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    missed: dict[str, int] = field(default_factory=dict)             # agent_id -> slots seen past grace (P6-2b)
    unarmed: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, why): a stored spec that fails
    changed: list[str] = field(default_factory=list)                 # lifecycle moved mid-tick: left for next tick
    clamped: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, cursor): a cursor past the clock


def _agents(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT a.agent_id, a.name, a.operation, a.current_version, s.spec_hash, s.spec FROM agents a"
        " JOIN agent_specs s ON s.agent_id = a.agent_id AND s.version = a.current_version"
        " WHERE a.registration = 'registered' ORDER BY a.agent_id").fetchall()


def _state(conn: sqlite3.Connection, agent_id: str) -> tuple | None:
    """What the tick's decision rested on, read again inside the write that acts on it."""
    return conn.execute(
        "SELECT a.registration, a.operation, a.current_version, s.spec_hash FROM agents a"
        " JOIN agent_specs s ON s.agent_id = a.agent_id AND s.version = a.current_version"
        " WHERE a.agent_id = ?", (agent_id,)).fetchone()


def _cursor(conn: sqlite3.Connection, agent_id: str) -> datetime | None:
    row = conn.execute("SELECT value FROM agent_settings WHERE key = ?", (CURSOR_PREFIX + agent_id,)).fetchone()
    if row is None:
        return None
    return datetime.strptime(row[0], _STAMP).replace(tzinfo=timezone.utc)  # unreadable: raises, the tick fails closed


def _set_cursor(conn: sqlite3.Connection, agent_id: str, at: datetime) -> None:
    conn.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (CURSOR_PREFIX + agent_id, _stamp(at)))


def _settle(conn: sqlite3.Connection, agent_id: str, cursor: datetime | None, floor: datetime,
            out: TickResult) -> None:
    """Bring a cursor that will not be walked to ``floor`` without firing anything."""
    if cursor is not None and cursor > floor:
        # The one backward move, and it is reported. Moving to floor releases nothing: every slot after floor is
        # still in the future and fires when it comes due, and no slot at or before floor is walked again.
        out.clamped.append((agent_id, _stamp(cursor)))
    if cursor != floor:
        _set_cursor(conn, agent_id, floor)


def tick(store: AgentStore | Path | str | None, clock: Callable[[], datetime],
         binding_of: Callable[[str, str], str | None]) -> TickResult:
    """One controller pass. ``store`` is an opened :class:`AgentStore` or a store directory; a directory is opened
    through :func:`open_agent_store`, so a refused agent schema raises ``AgentStoreRefused`` with its named reason
    before anything is read or written.

    An agent that does not fire this tick (no cursor yet, unarmed, paused, or a stored spec that fails its integrity
    check) has its cursor moved to ``now`` without firing, so arming, resuming or repairing it later releases no
    backlog. Every write re-reads the agent's registration, operation, version and spec hash and does nothing if they
    moved since the snapshot; that agent is left to the next tick. A long walk commits one bounded chunk per
    transaction, and missed slots are counted per agent, not listed."""
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    now = schedule._utc(clock())  # a naive clock reading is refused up front
    floor = now.replace(second=0, microsecond=0)
    out = TickResult()
    with store.read() as conn:
        agents = _agents(conn)
    for agent_id, name, operation, version, digest, text in agents:
        snap = ("registered", operation, version, digest)
        trigger = binding = None
        if operation != "active":
            out.paused.append(agent_id)
        else:
            try:
                trigger = _check_row(agent_id, name, digest, text)["trigger"]
                schedule._zone(trigger["zone"])
            except ValueError as exc:  # SpecError is a ValueError; so is a zone this host does not know
                out.refused.append((agent_id, str(exc)))
                trigger = None
            if trigger is not None:
                binding = binding_of(agent_id, digest)
                if binding is None:
                    out.unarmed.append(agent_id)
        with store.write() as conn:
            if _state(conn, agent_id) != snap:
                out.changed.append(agent_id)
                continue
            cursor = _cursor(conn, agent_id)
            if binding is None or cursor is None or cursor >= floor:
                _settle(conn, agent_id, cursor, floor, out)
                continue
        _walk(store, agent_id, snap, trigger, binding, cursor, floor, now, out)
    return out


def _walk(store: AgentStore, agent_id: str, snap: tuple, trigger: dict, binding: str, cursor: datetime,
          floor: datetime, now: datetime, out: TickResult) -> None:
    zone = trigger["zone"]
    while cursor < floor:
        upto = min(floor, cursor + schedule.MAX_SPAN)  # slots_between refuses a longer walk
        slots = schedule.slots_between(trigger, zone, cursor, upto)
        with store.write() as conn:
            # another tick may have walked this chunk, or the agent may have been paused, retired or revised
            if _state(conn, agent_id) != snap or _cursor(conn, agent_id) != cursor:
                out.changed.append(agent_id)
                return
            for slot in slots:
                key = schedule.slot_key(slot)
                # first seen NOW, whichever chunk the slot sits in
                if schedule.classify(slot, now) != schedule.DUE:
                    out.missed[agent_id] = out.missed.get(agent_id, 0) + 1
                    continue
                _, outcome = claim(conn, agent_id=agent_id, kind="scheduled", key=key, spec_hash=snap[3],
                                   binding_id=binding, window_key=schedule.window_key(trigger, zone, slot), now=now)
                {CLAIMED: out.claimed, OVERLAP: out.overlapped, DUPLICATE: out.duplicates}[outcome].append(
                    (agent_id, key))
            _set_cursor(conn, agent_id, upto)
        cursor = upto
