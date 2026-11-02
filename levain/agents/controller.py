"""levain.agents.controller — the occurrence claim and the controller tick (A0 r7 §2.5, §3.9; BUILD_PLAN P6-1).

Later slices add their code here in the owner order P6-2, P6-3a, P6-5, P6-4, D1-2.

``claim`` is SELECT-then-INSERT inside the caller's ``BEGIN IMMEDIATE``. ``agent_occurrences_one_fire`` raises ABORT,
which no ``OR`` clause turns into a skip, so a second delivery of one key is answered by the SELECT and writes nothing.

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
from levain.agents.store import AgentStore, open_agent_store

__all__ = ["CLAIMED", "DUPLICATE", "OVERLAP", "TickResult", "occurrence_id", "run_id", "claim", "tick"]

CLAIMED = "claimed"
DUPLICATE = "duplicate"
OVERLAP = "skipped-overlap"

CURSOR_PREFIX = "tick_cursor:"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _digest(agent_id: str, kind: str, key: str) -> str:
    # authority._canonical's form, written out here so this module imports no authority code
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
    ``duplicate`` (the key already has a row; nothing is written). The caller holds ``BEGIN IMMEDIATE``: the SELECT
    and the INSERT must be one atomic step, or two deliveries race between them."""
    if not conn.in_transaction:
        raise RuntimeError("a claim runs inside the caller's BEGIN IMMEDIATE")
    oid = occurrence_id(agent_id, kind, key)
    if conn.execute("SELECT 1 FROM agent_occurrences WHERE agent_id = ? AND kind = ? AND key = ?",
                    (agent_id, kind, key)).fetchone() is not None:
        return oid, DUPLICATE
    at = _stamp(now)
    live = conn.execute("SELECT 1 FROM agent_occurrences WHERE agent_id = ? AND state IN ('admitted','launched')",
                        (agent_id,)).fetchone() is not None
    state, reason, ended = ("skipped-overlap", "overlap", at) if live else ("admitted", None, None)
    conn.execute(
        "INSERT INTO agent_occurrences (occurrence_id, agent_id, kind, key, state, reason, window_key, spec_hash,"
        " binding_id, run_id, admitted_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, agent_id, kind, key, state, reason, window_key, spec_hash, binding_id,
         run_id(agent_id, kind, key), at, ended))
    return oid, (OVERLAP if live else CLAIMED)


@dataclass
class TickResult:
    claimed: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, key)
    overlapped: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    missed: list[tuple[str, str]] = field(default_factory=list)      # seen past grace: counted, NOT written (P6-2b)
    unarmed: list[str] = field(default_factory=list)


def _agents(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT a.agent_id, s.spec_hash, s.spec FROM agents a JOIN agent_specs s"
        " ON s.agent_id = a.agent_id AND s.version = a.current_version"
        " WHERE a.registration = 'registered' AND a.operation = 'active' ORDER BY a.agent_id").fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _cursor(conn: sqlite3.Connection, agent_id: str) -> datetime | None:
    row = conn.execute("SELECT value FROM agent_settings WHERE key = ?", (CURSOR_PREFIX + agent_id,)).fetchone()
    if row is None:
        return None
    return datetime.strptime(row[0], _STAMP).replace(tzinfo=timezone.utc)  # unreadable: raises, the tick fails closed


def _set_cursor(conn: sqlite3.Connection, agent_id: str, at: datetime) -> None:
    conn.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (CURSOR_PREFIX + agent_id, _stamp(at)))


def tick(store: AgentStore | Path | str | None, clock: Callable[[], datetime],
         binding_of: Callable[[str, str], str | None]) -> TickResult:
    """One controller pass. ``store`` is an opened :class:`AgentStore` or a store directory; a directory is opened
    through :func:`open_agent_store`, so a refused agent schema raises ``AgentStoreRefused`` with its named reason
    before anything is read or written. An agent with no cursor starts at ``now`` (history before its first look is
    not fired); an unarmed agent's cursor moves to ``now`` too, so arming later does not release a backlog."""
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    now = clock()
    schedule._utc(now)  # a naive clock reading is refused up front
    floor = schedule._utc(now).replace(second=0, microsecond=0)
    out = TickResult()
    with store.read() as conn:
        agents = _agents(conn)
    for agent_id, spec_hash, spec_text in agents:
        trigger = json.loads(spec_text)["trigger"]
        zone = trigger.get("zone")
        binding = binding_of(agent_id, spec_hash)
        with store.write() as conn:
            cursor = _cursor(conn, agent_id)
            if binding is None or cursor is None:
                if binding is None:
                    out.unarmed.append(agent_id)
                _set_cursor(conn, agent_id, floor)
                continue
            while cursor < floor:
                upto = min(floor, cursor + schedule.MAX_SPAN)  # slots_between refuses a longer walk
                for slot in schedule.slots_between(trigger, zone, cursor, upto):
                    key = schedule.slot_key(slot)
                    # first seen NOW, whichever chunk the slot sits in
                    if schedule.classify(slot, now) != schedule.DUE:
                        out.missed.append((agent_id, key))
                        continue
                    _, outcome = claim(conn, agent_id=agent_id, kind="scheduled", key=key, spec_hash=spec_hash,
                                       binding_id=binding, window_key=schedule.window_key(trigger, zone, slot),
                                       now=now)
                    {CLAIMED: out.claimed, OVERLAP: out.overlapped, DUPLICATE: out.duplicates}[outcome].append(
                        (agent_id, key))
                cursor = upto
                _set_cursor(conn, agent_id, cursor)
    return out
