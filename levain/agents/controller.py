"""levain.agents.controller — the occurrence claim and the controller tick (A0 r7 §2.5, §3.9; BUILD_PLAN P6-1).

Later slices add their code here in the owner order P6-2, P6-3a, P6-5, P6-4, D1-2.

``claim`` is one plain ``INSERT``. ``agent_occurrences_one_fire`` raises ABORT, which no ``OR`` clause turns into a
skip, so a second delivery of one key is refused by the trigger, writes nothing, and is answered from the row that is
there. The ``INSERT`` is the claim's first statement, so it takes the write lock before it reads: a caller in a
deferred ``BEGIN`` that has read nothing yet gets the same answer as one in ``BEGIN IMMEDIATE``. A deferred caller
that read first holds a snapshot and gets SQLite's busy error, which writes nothing.

``tick`` walks every registered, active agent's slots since its stored cursor, in the agent's stored zone, with an
injected clock. Before the walk it ends every admitted occurrence the full cap has held past its grace
(:func:`close_no_slot`); ``launch_over`` and ``session_alive`` are the coordinator-lease probes (see :mod:`slots`). What it needs from outside the store comes in as ``binding_of`` (``(agent_id, spec_hash) ->
binding_id | None``; ``None`` means the agent is not armed), because arming is derived from the authority log and a
binding is minted by the launch slice, neither of which is this module's.
"""
from __future__ import annotations

import bisect
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from levain import authority as _auth
from levain.agent_authority import AgentCoverMoved
from levain.agents import accounting, schedule, slots
from levain.agents.spec import _check_row
from levain.agents.store import AgentStore, open_agent_store

_log = logging.getLogger("levain.agents.controller")

__all__ = ["CLAIMED", "DUPLICATE", "OVERLAP", "LEASE_S", "TickResult", "occurrence_id", "run_id", "claim",
           "close_no_slot", "tick", "heartbeat", "takeover", "Window", "LaunchResult", "launch",
           "record_session_key", "Started", "start_run", "dispatch_probes", "SESSION_KEY_RE", "SESSION_GRACE_S",
           "Ended", "settle_terminal", "GatedChild", "GATE_REFUSED", "fire_argv", "fire_record_path", "spawn_fire",
           "claim_fire", "fire", "miss_streak"]

CLAIMED = "claimed"
DUPLICATE = "duplicate"
OVERLAP = "skipped-overlap"

LEASE_S = 900  # A0 r7 section 2.3 [judged]: the coordinator slot's lease

CURSOR_PREFIX = "tick_cursor:"
STREAK_PREFIX = "miss_streak:"
STREAK_CAP = 16  # P6-2b r2 [judged]: the counted windows kept per agent; a longer streak reads as this many
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
    no_slot: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, key): admitted, past grace, no slot
    missed_windows: dict[str, list[str]] = field(default_factory=dict)  # agent_id -> windows with a missed slot, once each
    silent: list[str] = field(default_factory=list)                  # agents with a miss this pass that fired nothing
    reaped: object = None                                            # what the reaper returned this pass (P6-4b)
    reaper_error: str | None = None                                  # the exception type of a reaper that raised


def _agents(conn: sqlite3.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT a.agent_id, a.name, a.operation, a.current_version, s.spec_hash, s.spec FROM agents a"
        " JOIN agent_specs s ON s.agent_id = a.agent_id AND s.version = a.current_version"
        " WHERE a.registration = 'registered' ORDER BY a.agent_id").fetchall()


def _state(conn: sqlite3.Connection, agent_id: str) -> tuple | None:
    """What the tick's decision rested on, read again inside the write that acts on it."""
    return conn.execute(
        "SELECT a.registration, a.operation, a.current_version, s.spec_hash, a.name, s.spec FROM agents a"
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


def _admitted_since(kind: str, key: str, admitted_at: str) -> datetime | None:
    """When an admitted occurrence's grace began: a scheduled key is its slot's instant (the grace
    :func:`schedule.classify` already measures), any other key has none, so its own admission. A stamp that does
    not parse gives ``None``: the row is never closed on a time nobody can read."""
    try:
        admitted = datetime.strptime(admitted_at, _STAMP).replace(tzinfo=timezone.utc)
        return datetime.strptime(key, _STAMP).replace(tzinfo=timezone.utc) if kind == "scheduled" else admitted
    except (ValueError, TypeError):
        return None


def _miss_identity(oid: str, kind: str, window: str) -> str:
    # a scheduled miss is counted by its schedule window, a manual one by its own occurrence
    return window if kind == "scheduled" else "occurrence:" + oid


def close_no_slot(conn: sqlite3.Connection, now: datetime, over: Collection[str] = ()) -> list[tuple[str, str]]:
    """End as ``missed`` / ``no_slot`` every ``admitted`` occurrence past its grace while the cap is full (A0 §2.4:
    a named occurrence is retried each tick, and only the grace running out ends it). Returns ``(agent_id, key)``;
    each close reaches the miss streak in this same transaction.
    It reclaims the expired coordinator leases in ``over`` (probed by the caller, :func:`slots.probe_over`) first, and
    does nothing while a slot is free: an occurrence still admitted past grace with a slot free was not left by the
    cap, so ``no_slot`` would be a false reason. A cap nobody can read decides nothing here: the tick goes on for
    the other agents and the occurrences stay admitted. Runs in the caller's write transaction."""
    return [(agent_id, key) for agent_id, key, _, _ in _close_no_slot_detailed(conn, now, over)]


def _close_no_slot_detailed(conn: sqlite3.Connection, now: datetime,
                            over: Collection[str] = ()) -> list[tuple[str, str, str, str]]:
    """:func:`close_no_slot`, returning ``(agent_id, key, window_key, marker)`` for :func:`tick`'s report, ``marker``
    being the instant the occurrence's grace began (:func:`_admitted_since`)."""
    try:
        if slots.free(conn, now, over) > 0:
            return []
    except ValueError:
        return []
    at = _stamp(now)
    closed = []
    misses: dict[str, list[tuple[str, str]]] = {}
    for oid, agent_id, kind, key, admitted_at, window in conn.execute(
            "SELECT occurrence_id, agent_id, kind, key, admitted_at, window_key FROM agent_occurrences"
            " WHERE state = 'admitted' ORDER BY admitted_at, occurrence_id").fetchall():
        since = _admitted_since(kind, key, admitted_at)
        if since is None or schedule.classify(since, now) != schedule.MISSED:
            continue
        conn.execute("UPDATE agent_occurrences SET state = 'missed', reason = 'no_slot', ended_at = ?"
                     " WHERE occurrence_id = ? AND state = 'admitted'", (at, oid))
        slots.release(conn, oid)  # an admitted occurrence holds nothing (A0 §2.4), so this frees nothing today
        closed.append((agent_id, key, window, _stamp(since)))
        misses.setdefault(agent_id, []).append((_miss_identity(oid, kind, window), _stamp(since)))
    for agent_id, theirs in misses.items():
        _record_misses(conn, agent_id, theirs)
    return closed


def tick(store: AgentStore | Path | str | None, clock: Callable[[], datetime],
         binding_of: Callable[[str, str], str | None],
         session_alive: Callable[[str], bool | None] | None = None,
         launch_over: Callable[[str], bool | None] | None = None,
         reaper: Callable[[], object] | None = None) -> TickResult:
    """One controller pass. ``reaper`` (P6-4b: ``Supervisor.reap`` with its executor and attention callbacks bound) runs
    first, so the leases of launched rows are renewed before anything that could take long; one that raises is
    logged and recorded in ``reaper_error`` and never stops the claims. The clock is read again after it, so the
    claims and the expiry pass judge the time it is, not the time before the stops.

    ``store`` is an opened :class:`AgentStore` or a store directory; a directory is opened through
    :func:`open_agent_store`, so a refused agent schema raises ``AgentStoreRefused`` with its named reason before
    anything is read or written.

    An agent that does not fire this tick (no cursor yet, unarmed, paused, or a stored spec that fails its integrity
    check) has its cursor moved to ``now`` without firing, so arming, resuming or repairing it later releases no
    backlog. Every write re-reads the agent's registration, operation, version and spec hash and does nothing if they
    moved since the snapshot; that agent is left to the next tick. A long walk commits one bounded chunk per
    transaction, and missed slots are counted per agent, not listed."""
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    now = schedule._utc(clock())  # a naive clock reading is refused up front
    out = TickResult()
    if reaper is not None:
        try:
            out.reaped = reaper()
        except Exception as exc:  # noqa: BLE001 - the reaper's failure must not cost the pass its claims
            _log.warning("agent tick: the reaper raised %s: %s", type(exc).__name__, exc)
            out.reaper_error = type(exc).__name__
        now = schedule._utc(clock())  # the stops may have taken long: the claims judge the clock as it reads now
    floor = now.replace(second=0, microsecond=0)
    # first, so an occurrence the cap held past its grace no longer reads as a live previous one to this tick's claims
    with store.read() as conn:
        expired = slots.expired_launches(conn, now)
    over = slots.probe_over(expired, launch_over, session_alive)  # outside any write lock: a probe can be slow
    with store.write() as conn:
        for agent_id, key, window, _ in _close_no_slot_detailed(conn, now, over):
            out.no_slot.append((agent_id, key))
            out.missed[agent_id] = out.missed.get(agent_id, 0) + 1
            _report_windows(out, agent_id, [window])
    with store.read() as conn:
        agents = _agents(conn)
    for agent_id, name, operation, version, digest, text in agents:
        snap = ("registered", operation, version, digest, name, text)
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
    # judged once per agent, after every close and walk of this pass: a whole stretch went by and this one pass is
    # the first to see it
    # an agent whose state moved mid-pass may have fired in a chunk that never ran, so it is not judged silent
    fired = {a for a, _ in out.claimed + out.overlapped + out.duplicates}
    moved = set(out.changed)
    out.silent.extend(a for a in out.missed_windows if out.missed_windows[a] and a not in fired and a not in moved)
    return out


def _report_windows(out: TickResult, agent_id: str, windows: list[str]) -> None:
    if not windows:
        return  # nothing missed: never create an empty key
    seen = out.missed_windows.setdefault(agent_id, [])
    known = set(seen)
    for window in dict.fromkeys(windows):
        if window not in known:
            seen.append(window)
            known.add(window)


def _read_streak(conn: sqlite3.Connection, agent_id: str) -> list[tuple[str, str]] | None:
    """The counted misses as ``(marker, identity)`` in time order, or ``None`` when absent or unreadable: no streak is
    claimed on a value nobody can read."""
    row = conn.execute("SELECT value FROM agent_settings WHERE key = ?", (STREAK_PREFIX + agent_id,)).fetchone()
    if row is None:
        return None
    try:
        entries = []
        for marker, identity in json.loads(row[0]):
            if not isinstance(marker, str) or not isinstance(identity, str):
                raise ValueError("a streak entry is two strings")
            entries.append((marker, identity))
        return sorted(entries)
    except (ValueError, TypeError, AttributeError):
        return None


def _last_done(conn: sqlite3.Connection, agent_id: str) -> str | None:
    """The latest ``done`` run's admission; a ``skipped-no-input`` neither counts nor clears (BUILD_PLAN gate
    counting)."""
    return conn.execute("SELECT MAX(admitted_at) FROM agent_occurrences WHERE agent_id = ? AND state = 'done'",
                        (agent_id,)).fetchone()[0]


def _since_done(entries: list[tuple[str, str]], last_done: str | None) -> list[tuple[str, str]]:
    # a done run admitted at or after a miss's marker ends the streak up to that miss
    return entries if last_done is None else [e for e in entries if e[0] > last_done]


def _record_misses(conn: sqlite3.Connection, agent_id: str, misses: list[tuple[str, str]]) -> None:
    """``misses`` is ``(identity, marker)``: ``identity`` is a scheduled miss's window key or a manual occurrence's
    own, ``marker`` the instant the miss is dated by (a scheduled slot's own key, a manual occurrence's admission). An
    identity counts once however many of its slots went by (a closed-lid night is one miss) and whichever path saw it
    first: a walk, or a ``no_slot`` close arriving after later windows were counted.

    Design (P6-2b r2): the record is the bounded, time-ordered set of counted ``(marker, identity)`` pairs, and the
    streak is derived from it when read: the pairs whose marker is after the latest ``done`` run's admission. A late
    marker goes in at its place in time if its identity is absent and is ignored if present, so it never reorders or
    re-counts the streak (r1's ``count + last`` could not tell an older window from a new one). The set keeps the
    newest :data:`STREAK_CAP` pairs, so a longer streak reads as that many; the gauge fires at 2. It stays a stored
    record because a walk's missed slots are never written as rows (only ``no_slot`` closes are), and it is read and
    written inside the BEGIN IMMEDIATE transaction of the walk chunk or close that produced the misses."""
    if not misses:
        return
    if not conn.in_transaction:
        raise RuntimeError("the miss streak is written inside the caller's write transaction")
    last_done = _last_done(conn, agent_id)
    entries = _since_done(_read_streak(conn, agent_id) or [], last_done)  # what a done cleared is dropped here
    known = {identity for _, identity in entries}
    for identity, marker in sorted(misses, key=lambda m: m[1]):  # a window's earliest slot in this batch dates it
        if identity in known or (last_done is not None and marker <= last_done):
            continue
        bisect.insort(entries, (marker, identity))
        known.add(identity)
    conn.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (STREAK_PREFIX + agent_id, json.dumps([list(e) for e in entries[-STREAK_CAP:]])))


def miss_streak(conn: sqlite3.Connection, agent_id: str) -> int:
    """Consecutive windows with a missed slot and no ``done`` run since, at most :data:`STREAK_CAP`; 0 when none or
    the record is unreadable."""
    return len(_since_done(_read_streak(conn, agent_id) or [], _last_done(conn, agent_id)))


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
            misses = []
            for slot in slots:
                key = schedule.slot_key(slot)
                # first seen NOW, whichever chunk the slot sits in
                if schedule.classify(slot, now) != schedule.DUE:
                    out.missed[agent_id] = out.missed.get(agent_id, 0) + 1
                    misses.append((schedule.window_key(trigger, zone, slot), key))
                    continue
                _, outcome = claim(conn, agent_id=agent_id, kind="scheduled", key=key, spec_hash=snap[3],
                                   binding_id=binding, window_key=schedule.window_key(trigger, zone, slot), now=now)
                {CLAIMED: out.claimed, OVERLAP: out.overlapped, DUPLICATE: out.duplicates}[outcome].append(
                    (agent_id, key))
            _record_misses(conn, agent_id, misses)
            _report_windows(out, agent_id, [w for w, _ in misses])
            _set_cursor(conn, agent_id, upto)
        cursor = upto


def _write_lock(conn: sqlite3.Connection) -> None:
    # A write that matches no row still takes SQLite's write lock, so the lease read after it is the one the CAS
    # compares against. A caller in a deferred BEGIN that has read nothing yet gets the same answer as one in BEGIN
    # IMMEDIATE; one that read first holds a snapshot, and if a rival committed since then it gets SQLite's busy error
    # here, before anything is written.
    conn.execute("UPDATE agent_occurrences SET lease_until = lease_until WHERE 0")


def _lease(value: object) -> datetime | None:
    """A stored lease, or ``None`` when it is absent or unreadable (neither is evidence of a lapse or of life)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _expiry(at: datetime, lease_s: int) -> datetime:
    # Stamps hold whole seconds and the lapse test compares the untruncated clock, so the expiry is rounded up:
    # a lease is never shorter than asked for, and heartbeat and takeover share the boundary ``at > lease``.
    moment = at + timedelta(seconds=lease_s)
    return moment.replace(microsecond=0) + timedelta(seconds=1) if moment.microsecond else moment


SESSION_GRACE_S = 120  # [judged, against nothing measured]: what a session run's supervisor passes heartbeat


def heartbeat(conn: sqlite3.Connection, occurrence_id: str, epoch: int, now: datetime, lease_s: int = LEASE_S, *,
              session_grace_s: int | None = None) -> bool:
    """The supervisor's heartbeat: extend a ``launched`` row's lease to ``now + lease_s``. It is a compare-and-set on
    the epoch the holder launched or last renewed under and on the exact lease it read, so a holder that a takeover
    fenced (the epoch moved, the row left ``launched``) writes nothing and is told ``False``. A lease that has already
    lapsed (``now`` past it) is refused too: the row belongs to takeover from that moment, and a late holder must not
    postpone crash recovery. An absent or unreadable lease is refused, as takeover refuses it. The lease never moves
    backwards, so a renew computed from an earlier clock reading, or a wall clock set back, leaves the later lease in
    place. Runs inside the caller's write transaction and takes its write lock first.

    ``session_grace_s`` is for a run that opens an S1 session (the supervisor knows its executor; the row does not):
    a row that has recorded no ``session_key`` by ``launched_at + session_grace_s`` is refused, so a fire that never
    opened its session lapses to takeover instead of being renewed for ever. ``None`` (a sessionless run, the dream)
    applies no such bound. A ``launched_at`` that cannot be read is refused under the bound."""
    if not conn.in_transaction:
        raise RuntimeError("a heartbeat runs inside the caller's write transaction")
    if session_grace_s is not None and (type(session_grace_s) is not int or session_grace_s < 0):
        raise ValueError("session_grace_s is a non-negative integer of seconds, or None")
    at = schedule._utc(now)
    _write_lock(conn)
    row = conn.execute("SELECT lease_until, session_key, launched_at FROM agent_occurrences WHERE occurrence_id = ?"
                       " AND state = 'launched' AND epoch = ?", (occurrence_id, epoch)).fetchone()
    if row is None:
        return False
    lease = _lease(row[0])
    if lease is None or at > lease:
        return False
    if session_grace_s is not None and row[1] is None:
        launched = _lease(row[2])
        if launched is None or at >= launched + timedelta(seconds=session_grace_s):
            return False
    until = max(lease, _expiry(at, lease_s))
    cur = conn.execute("UPDATE agent_occurrences SET lease_until = ? WHERE occurrence_id = ? AND state = 'launched'"
                       " AND epoch = ? AND lease_until = ?", (_stamp(until), occurrence_id, epoch, row[0]))
    return cur.rowcount == 1


def takeover(conn: sqlite3.Connection, occurrence_id: str, now: datetime,
             usage: accounting.Usage | None = None, stopped: accounting.StopResult | None = None) -> bool:
    """Fenced takeover (A0 r7 section 2.3, lapse recovery): end a ``launched`` row whose lease has lapsed, writing
    ``epoch + 1`` and ``crash_recovered`` in one statement, settling its reservation and freeing its slot in the same
    transaction (A0 §2.4's lapse row, through :func:`_end_launched`, the one end :func:`settle_terminal` uses). The
    lease is read here, inside the write transaction, and the ``UPDATE`` is guarded by the exact value read, so a
    heartbeat that landed first or a takeover that already ran leaves it with nothing to change (``False``), and
    nothing is settled or freed. A live lease is never taken over; a lease that is absent or unreadable is not
    evidence of a lapse, so it is refused too. ``usage`` is settled as read only together with ``stopped``, the
    executor's own :class:`accounting.StopResult` with ``terminated`` True: a reading taken while the process may
    still run is a snapshot, and settling it would free the slot below what the run goes on to spend. Without that
    proof the end is unreadable whatever ``usage`` says (§2.4: all of the reservation is debited, or the caps recorded
    on a subscription run). It takes the caller's write lock first, as :func:`heartbeat` does."""
    if not conn.in_transaction:
        raise RuntimeError("a takeover runs inside the caller's write transaction")
    at = schedule._utc(now)
    _write_lock(conn)
    row = conn.execute("SELECT lease_until FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched'",
                       (occurrence_id,)).fetchone()
    if row is None:
        return False
    lease = _lease(row[0])
    if lease is None or at <= lease:
        return False
    proven = type(stopped) is accounting.StopResult and stopped.terminated is True
    ended = _end_launched(conn, occurrence_id, "stopped", "crash_recovered",
                          usage if usage is not None and proven else _UNREAD, at, "lease_until = ?", (row[0],),
                          bump=True)
    return ended.ended


# --- the terminal transaction (A0 r8 §2.4's release table, §2.5 launched -> done/failed/stopped; P6-5b) --------------

_UNREAD = accounting.Usage(None, None, None, "lapse-recovery")
_ENDS = ("done", "failed", "stopped")


@dataclass(frozen=True)
class Ended:
    """``ended`` is True when this call wrote the terminal state; then ``state``/``reason`` are what it wrote (a breach
    found at settle writes ``failed``/``budget_breach`` whatever was asked), ``settled`` is the reservation's
    :class:`accounting.Settled` and ``released`` whether a slot was freed. False: nothing was written."""
    ended: bool
    state: str | None = None
    reason: str | None = None
    settled: accounting.Settled | None = None
    released: bool = False


def _end_launched(conn: sqlite3.Connection, occurrence_id: str, state: str, reason: str, usage: accounting.Usage,
                  at: datetime, guard: str, args: tuple, *, bump: bool) -> Ended:
    """The one place a launched occurrence's slot is freed and its reservation settled (an admitted one holds neither,
    A0 §2.4; :func:`close_no_slot`'s release on it is defensive): under the caller's write lock, the
    row is read as ``launched`` under ``guard``; the reservation is settled, a breach turns the end into
    ``failed``/``budget_breach``, the state is written under the same guard (with ``epoch + 1`` for lapse recovery)
    and the slot is released. A row the guard does not find gets nothing written, settled or freed."""
    _write_lock(conn)
    where = f"occurrence_id = ? AND state = 'launched' AND {guard}"
    if conn.execute(f"SELECT 1 FROM agent_occurrences WHERE {where}", (occurrence_id, *args)).fetchone() is None:
        return Ended(False)
    settled = accounting.settle(conn, {"occurrence_id": occurrence_id}, usage)
    if settled.breach:
        state, reason = "failed", "budget_breach"
    cur = conn.execute("UPDATE agent_occurrences SET state = ?, reason = ?, ended_at = ?"
                       + (", epoch = epoch + 1" if bump else "") + f" WHERE {where}",
                       (state, reason, _stamp(at), occurrence_id, *args))
    if cur.rowcount != 1:   # the lock is held since the read: only a trigger gets here; the caller rolls back
        raise RuntimeError(f"occurrence {occurrence_id} moved inside its own terminal transaction")
    return Ended(True, state, reason, settled, slots.release(conn, occurrence_id))


def settle_terminal(conn: sqlite3.Connection, occurrence_id: str, epoch: int, state: str, reason: str,
                    usage: accounting.Usage, now: datetime) -> Ended:
    """End a launched occurrence (A0 §2.5 ``launched -> done/failed/stopped``) with its reservation settled against
    ``usage`` (:func:`accounting.settle`, A0 §2.4's rows) and its slot released, inside the caller's write transaction.
    ``epoch`` is the one the holder launched or last renewed under: a row a revoke or a takeover has fenced since, or
    one already terminal, is not found, and nothing is written, settled or freed (``Ended(False)``), so a second call
    is a no-op. ``usage`` is the executor's own ``usage()`` reading, never computed here. A breach overrides ``state``
    with ``failed``/``budget_breach`` (the agent is paused in the same transaction). The supervisor calls this for a
    run it stopped only after ``stop()`` reported ``terminated`` (§2.4's first row); ``awake.sync`` is the caller's,
    after its commit. Raises on a bad argument before reading anything, and when the write fails: the caller's
    transaction must then roll back."""
    if not conn.in_transaction:
        raise RuntimeError("a terminal state is written inside the caller's write transaction")
    if state not in _ENDS:
        raise ValueError(f"a launched occurrence ends {', '.join(_ENDS)}")
    if type(reason) is not str or not reason or (state == "done") != (reason == "completed"):
        raise ValueError("a terminal reason is a non-empty string, and 'completed' is done's and only done's")
    if type(epoch) is not int:
        raise ValueError("epoch is the integer the holder runs under")
    return _end_launched(conn, occurrence_id, state, reason, usage, schedule._utc(now), "epoch = ?", (epoch,),
                         bump=False)


# --- the launch transaction (A0 r8 §2.4, §2.5; BUILD_PLAN P6-5a) ------------------------------------------------------

LAUNCHED = "launched"
STOPPED = "stopped"
SKIPPED_BUDGET = "skipped-budget"
SKIPPED_NO_INPUT = "skipped-no-input"   # the on_input hook answered "no input": the row ends, holding nothing
MISSED = "missed"                # the on_input hook stayed unanswerable past the row's grace: the row ends, a miss
NO_SLOT = "no-slot"              # the row stays admitted: retried each tick until its grace (close_no_slot)
UNCLASSIFIED = "unclassified"    # the cover could not be read: nothing written, the row stays admitted
NOT_ADMITTED = "not-admitted"    # the row is not admitted (already launched or ended): nothing written

# levain.slot.grammar.SESSION_RE's pattern (the S1 session id AgentFences joins on); not imported, because that module
# brings the slot package's closure into the controller. tests/test_agents_launch.py pins the two patterns equal.
SESSION_KEY_RE = re.compile(r"s-[0-9a-f]{16}")
_STORE_ID = re.compile(r"store-[0-9a-f]{32}")


@dataclass(frozen=True)
class Window:
    """What the launch's ceiling and budget count over, from the schedule (``accounting.Limits``' span fields): UTC ISO
    ``span_start`` inclusive, ``span_end`` exclusive, and, for a MANUAL launch only, the scheduled window holding now
    (``None``: none does). The signed limits themselves are read from the link the launch admits, never from here."""
    span_start: str
    span_end: str
    scheduled_window_key: str | None = None


@dataclass(frozen=True)
class LaunchResult:
    """``outcome`` is :data:`LAUNCHED`, :data:`STOPPED`, :data:`SKIPPED_BUDGET`, :data:`SKIPPED_NO_INPUT` or
    :data:`MISSED` (the row's new state; ``reason`` is the one written), or :data:`NO_SLOT`, :data:`UNCLASSIFIED` or
    :data:`NOT_ADMITTED` (nothing written; ``reason`` says why). ``launch_epoch`` is what a fire hands
    :func:`start_run`."""
    outcome: str
    reason: str | None = None
    slot: int | None = None
    link_id: str | None = None
    launch_epoch: int | None = None


DEFAULT_INPUT: object = object()   # launch(on_input=DEFAULT_INPUT): the agent's own hook; None stays "no check"
InputCheck = Callable[[dict], bool]   # the on_input hook: the occurrence's identity in, "input present" out


class _Refusal(Exception):
    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state, self.reason = state, reason


class _Retry(Exception):
    def __init__(self, outcome: str, reason: str) -> None:
        super().__init__(reason)
        self.outcome, self.reason = outcome, reason


def _store_id_of(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    if row is None or type(row[0]) is not str or _STORE_ID.fullmatch(row[0]) is None:
        raise ValueError("the agent store holds no well-formed store id")
    return row[0]


def _no_link_reason(state: _auth.AuthoritySnapshot, top: str | None, *, agent_id: str, store_id: str,
                    closed: int, now: datetime) -> str:
    """Why no candidate link is live, named from the highest-born one (or from the log when there is none)."""
    if top is None:
        return "revoked" if _auth.agent_last_revoke(state, agent_id, store_id) else "no_link"
    born = state.grant_born.get(top) or 0
    if born <= max(_auth.agent_last_revoke(state, agent_id, store_id), closed):
        return "revoked"
    if not _auth.agent_link_arms(state, top, now=now, closed_bar_revoke_seq=closed):
        return "expired"
    return "signer_revoked"


def _pick_link(conn: sqlite3.Connection, authority, state: _auth.AuthoritySnapshot, occ: dict, *, store_id: str,
               now: datetime) -> tuple[str, str]:
    """The occurrence's live link (A0 §3.2: the highest-born live link of its spec, here also of its Binding) and its
    incarnation, or :class:`_Refusal` ``stopped``. A peek that cannot read the cover raises :class:`_Retry`.

    The candidates and the grant terms the launch applies come from ``state``; ``peek`` answers from whatever replay
    the shared authority holds when it runs, which another refresh may have replaced. So every peek's incarnation must
    be ``state``'s own incarnation of that link: anything else (another incarnation of a revoked and recreated id, no
    link, a value that is no incarnation) means the cover moved under the launch, and it is retried, never ended."""
    closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                          (occ["agent_id"],)).fetchone()[0]
    closed = 0 if closed is None else closed
    candidates = sorted(
        (gid for gid in _auth.agent_links(state, occ["agent_id"], store_id)
         if state.policy.grants[gid]["spec_hash"] == occ["spec_hash"]
         and state.policy.grants[gid]["binding_id"] == occ["binding_id"]),
        key=lambda gid: state.grant_born.get(gid) or 0, reverse=True)
    for gid in candidates:
        try:
            live, incarnation = authority.peek(gid, agent_id=occ["agent_id"], store_id=store_id, now=now,
                                               closed_bar_revoke_seq=closed)
        except AgentCoverMoved as exc:
            raise _Retry(UNCLASSIFIED, f"the authority cover moved: {exc}") from exc
        if incarnation != _auth.agent_link_incarnation(state, gid):
            raise _Retry(UNCLASSIFIED, f"the authority cover moved: link {gid} is another incarnation than the "
                                       "launch's snapshot holds")
        if live is True:
            return gid, incarnation
    raise _Refusal(STOPPED, _no_link_reason(state, candidates[0] if candidates else None, agent_id=occ["agent_id"],
                                            store_id=store_id, closed=closed, now=now))


def _end_admitted(conn: sqlite3.Connection, occurrence_id: str, state: str, reason: str, at: str) -> None:
    cur = conn.execute("UPDATE agent_occurrences SET state = ?, reason = ?, ended_at = ? WHERE occurrence_id = ?"
                       " AND state = 'admitted'", (state, reason, at, occurrence_id))
    if cur.rowcount != 1:
        raise RuntimeError(f"occurrence {occurrence_id} left admitted inside its own launch transaction")


NO_INPUT = "no_input"
INPUT_UNREADABLE = "input_unreadable"
_HOOK_HIDES = ("state", "epoch", "admitted_at")   # read for the check, never handed to the hook


def _end_after_input(store: AgentStore, occ: dict, state: str, reason: str, at: datetime) -> LaunchResult:
    """End the admitted row the hook was asked about with ``state``/``reason``, one write guarded by ``state =
    'admitted' AND epoch = <the epoch the hook was asked at>``. A ``missed`` end reaches the miss streak in the same
    transaction. When the guard matches nothing, the row is read again under the same lock: a row no longer admitted
    is :data:`NOT_ADMITTED`; one still admitted at another epoch (its epoch was raised while the hook ran, as a bar
    raises it) is :data:`UNCLASSIFIED`, written nothing, so the next call decides it."""
    oid = occ["occurrence_id"]
    with store.write() as conn:
        cur = conn.execute("UPDATE agent_occurrences SET state = ?, reason = ?, ended_at = ?"
                           " WHERE occurrence_id = ? AND state = 'admitted' AND epoch = ?",
                           (state, reason, _stamp(at), oid, occ["epoch"]))
        if cur.rowcount != 1:
            now_row = conn.execute("SELECT state, epoch FROM agent_occurrences WHERE occurrence_id = ?",
                                   (oid,)).fetchone()
            if now_row is None or now_row[0] != "admitted":
                return LaunchResult(NOT_ADMITTED, "the occurrence ended while its input was checked")
            return LaunchResult(UNCLASSIFIED, "the occurrence's epoch moved while its input was checked")
        if state == MISSED:
            since = _admitted_since(occ["kind"], occ["key"], occ["admitted_at"])
            _record_misses(conn, occ["agent_id"], [(_miss_identity(oid, occ["kind"], occ["window_key"]),
                                                    _stamp(since))])
    return LaunchResult(state, reason)


def check_input(store: AgentStore, occurrence_id: str, on_input: InputCheck | None, now: datetime, *,
                epoch: int | None = None) -> LaunchResult | None:
    """The ``on_input`` hook point, run between the claim and the launch (BUILD_PLAN §4.0: "claim -> input check ->
    ``skipped-no-input``", its own transaction). :func:`launch` runs it after its cheap refusals and passes the
    ``epoch`` those were judged at. ``on_input(occurrence)`` receives the row's ``agent_id``, ``kind``, ``key``,
    ``window_key``, ``spec_hash``, ``binding_id`` and ``occurrence_id`` and answers ``True`` (input present) or
    ``False`` (none). With no hook the default is "input present". Returns ``None`` to go on to the launch, or the
    :class:`LaunchResult` that ends it:

    * ``False`` ends the ``admitted`` row ``skipped-no-input`` with reason :data:`NO_INPUT`, in one transaction that
      writes nothing else, so the occurrence never held a slot or a reservation (A0 §2.4).
    * Anything but a real ``bool``, or a hook that raises, is dead input, never quiet: within the row's grace
      (:func:`schedule.classify` from :func:`_admitted_since`'s instant) it is :data:`UNCLASSIFIED`,
      nothing is written and the row stays ``admitted`` for the next call; past the grace the row ends ``missed`` with
      reason :data:`INPUT_UNREADABLE`, counted in the miss streak, so a hook that never answers does not hold the
      agent's live row (and so every later claim as an overlap) for ever.
    * A row that is not ``admitted`` is :data:`NOT_ADMITTED`, written nothing; one whose epoch is not ``epoch`` is
      :data:`UNCLASSIFIED` and its hook is not run.

    The hook runs outside any write lock, because it may be slow (it reads whatever the agent is to work on). It is
    given no timeout here: the intent is that the caller bounds it. Each end it causes is one ``UPDATE`` guarded by
    ``state = 'admitted'`` and the epoch read before the hook ran (:func:`_end_after_input`)."""
    cols = ("agent_id", "kind", "key", "state", "epoch", "window_key", "spec_hash", "binding_id", "admitted_at")
    with store.read() as conn:
        row = conn.execute("SELECT " + ", ".join(cols) + " FROM agent_occurrences WHERE occurrence_id = ?",
                           (occurrence_id,)).fetchone()
    if row is None:
        return LaunchResult(NOT_ADMITTED, "no such occurrence")
    occ = dict(zip(cols, row), occurrence_id=occurrence_id)
    if occ["state"] != "admitted":
        return LaunchResult(NOT_ADMITTED, f"the occurrence is {occ['state']}")
    if epoch is not None and occ["epoch"] != epoch:
        return LaunchResult(UNCLASSIFIED, "the occurrence's epoch moved before its input was checked")
    if on_input is None:
        return None
    at = schedule._utc(now)
    try:
        present = on_input({k: v for k, v in occ.items() if k not in _HOOK_HIDES})
    except Exception as exc:  # noqa: BLE001 - a hook that cannot answer is never read as "no input"
        why = f"the input check failed: {type(exc).__name__}"
    else:
        if type(present) is bool:
            return None if present else _end_after_input(store, occ, SKIPPED_NO_INPUT, NO_INPUT, at)
        why = f"the input check answered {type(present).__name__}, not a bool"
    since = _admitted_since(occ["kind"], occ["key"], occ["admitted_at"])
    if since is not None and schedule.classify(since, at) == schedule.MISSED:
        return _end_after_input(store, occ, MISSED, INPUT_UNREADABLE, at)
    return LaunchResult(UNCLASSIFIED, why)


def _refusal_first(conn: sqlite3.Connection, authority, state: _auth.AuthoritySnapshot, occ: dict, *,
                   executor: accounting.Executor, window: Window, store_id: str, now: datetime,
                   over: Collection[str]) -> None:
    """:func:`_admit`'s checks run for their refusals alone, before the input hook (the head's precedence ruling,
    L3 r1: an agent without authority never has its input read, and its refusal is reported as itself). It raises
    :class:`_Refusal`, or :class:`_Retry` for anything but a full cap; whatever it took is rolled back to a savepoint,
    so it writes nothing. A full cap is not a refusal: the input is still checked, and an absent one still ends the
    row quiet rather than leaving it to become a miss."""
    conn.execute("SAVEPOINT agent_input_first")
    try:
        _admit(conn, authority, state, occ, executor=executor, window=window, store_id=store_id, now=now, over=over)
    except _Retry as retry:
        if retry.outcome != NO_SLOT:
            raise
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO agent_input_first")
            conn.execute("RELEASE agent_input_first")


def _admitted_row(conn: sqlite3.Connection, occurrence_id: str) -> dict:
    row = conn.execute("SELECT agent_id, kind, key, state, epoch, window_key, spec_hash, binding_id"
                       " FROM agent_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
    if row is None:
        raise _Retry(NOT_ADMITTED, "no such occurrence")
    occ = dict(zip(("agent_id", "kind", "key", "state", "epoch", "window_key", "spec_hash", "binding_id"), row),
               occurrence_id=occurrence_id)
    if occ["state"] != "admitted":
        raise _Retry(NOT_ADMITTED, f"the occurrence is {occ['state']}")
    return occ


def launch(store: AgentStore | Path | str | None, occurrence_id: str, *, authority, executor: accounting.Executor,
           window: Window, now: datetime,
           launch_over: Callable[[str], bool | None] | None = None,
           session_alive: Callable[[str], bool | None] | None = None,
           on_input: InputCheck | None | object = DEFAULT_INPUT) -> LaunchResult:
    """The launch transaction (A0 §2.5 ``admitted -> launched``). ``authority`` is an
    :class:`~levain.agent_authority.AgentAuthority`; it is refreshed here, before the write lock.

    ``on_input`` left at :data:`DEFAULT_INPUT` is :func:`_default_input_check`: the dream's hook for the dream, none
    for any other agent. An explicit ``None`` is no input check, for any agent.
    With ``on_input``, the launch's refusals are judged first, in their own write transaction that takes nothing
    (:func:`_refusal_first`): a refusal ends the row with its own state and reason and the hook is never run. Only
    then is :func:`check_input` run, at the epoch those refusals were judged at; an answer there ends the call. A
    present input goes on to the transaction below, which judges everything again. In one
    ``BEGIN IMMEDIATE`` the row is read, the agent's operation, spec and bar, the predicate (a live link), the
    reservation (:func:`accounting.reserve`, held to the admitted link's signed terms) and a slot
    (:func:`slots.take`) are checked in that order, and either the row becomes ``launched`` (its launch columns,
    ``launched_at`` and a lease of :data:`LEASE_S`) or it ends with the refusal's state and reason. Every refusal is
    written after the reservation and slot work is rolled back to a savepoint, so a refused launch takes nothing.
    A cover that cannot be read and a full cap write nothing; the row stays ``admitted`` for the next tick.

    The links are matched on the store's own id (its ``meta`` row), never a caller's: a link signed for another store
    never admits here. The session key is not written here: the fire records it with :func:`start_run` once it has
    opened the session."""
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    if on_input is DEFAULT_INPUT:
        on_input = _default_input_check(store, occurrence_id)
    at_dt = schedule._utc(now)
    at = _stamp(at_dt)
    with store.read() as conn:
        expired = slots.expired_launches(conn, at_dt)
        sid = _store_id_of(conn)
    over = slots.probe_over(expired, launch_over, session_alive)   # outside any write lock: a probe can be slow
    try:
        state = authority.refresh()
    except Exception as exc:  # noqa: BLE001 - a replay that cannot finish is never read as "nothing live"
        return LaunchResult(UNCLASSIFIED, f"the authority cannot be replayed: {type(exc).__name__}")
    if on_input is not None:
        try:
            with store.write() as conn:
                occ = _admitted_row(conn, occurrence_id)
                try:
                    _refusal_first(conn, authority, state, occ, executor=executor, window=window, store_id=sid,
                                   now=at_dt, over=over)
                except _Refusal as refusal:
                    _end_admitted(conn, occurrence_id, refusal.state, refusal.reason, at)
                    return LaunchResult(refusal.state, refusal.reason)
        except _Retry as retry:   # raised inside the transaction: it rolled back, so nothing was written
            return LaunchResult(retry.outcome, retry.reason)
        skipped = check_input(store, occurrence_id, on_input, now, epoch=occ["epoch"])
        if skipped is not None:
            return skipped
    try:
        with store.write() as conn:
            occ = _admitted_row(conn, occurrence_id)
            try:
                link_id, incarnation, slot = _admit(conn, authority, state, occ, executor=executor, window=window,
                                                    store_id=sid, now=at_dt, over=over)
            except _Refusal as refusal:
                _end_admitted(conn, occurrence_id, refusal.state, refusal.reason, at)
                return LaunchResult(refusal.state, refusal.reason)
            cur = conn.execute(
                "UPDATE agent_occurrences SET state = 'launched', launch_epoch = epoch, link_id = ?,"
                " link_incarnation = ?, launched_at = ?, lease_until = ? WHERE occurrence_id = ?"
                " AND state = 'admitted' AND epoch = ?",
                (link_id, incarnation, at, _stamp(_expiry(at_dt, LEASE_S)), occurrence_id, occ["epoch"]))
            if cur.rowcount != 1:
                raise RuntimeError(f"occurrence {occurrence_id} moved inside its own launch transaction")
            return LaunchResult(LAUNCHED, None, slot, link_id, occ["epoch"])
    except _Retry as retry:   # raised inside the transaction: it rolled back, so nothing was written
        return LaunchResult(retry.outcome, retry.reason)


def _admit(conn: sqlite3.Connection, authority, state: _auth.AuthoritySnapshot, occ: dict, *,
           executor: accounting.Executor, window: Window, store_id: str, now: datetime,
           over: Collection[str]) -> tuple[str, str, int]:
    """The launch's checks after the row is read: ``(link_id, incarnation, slot)``, or :class:`_Refusal` /
    :class:`_Retry`. The reservation and the slot are taken under a savepoint that is rolled back on any refusal."""
    agent = conn.execute("SELECT a.registration, a.operation, s.spec_hash FROM agents a JOIN agent_specs s"
                         " ON s.agent_id = a.agent_id AND s.version = a.current_version WHERE a.agent_id = ?",
                         (occ["agent_id"],)).fetchone()
    if agent is None or agent[0] != "registered":
        raise _Refusal(STOPPED, "retired")
    if agent[1] != "active":
        raise _Refusal(STOPPED, "paused")
    if agent[2] != occ["spec_hash"]:
        raise _Refusal(STOPPED, "spec_moved")
    if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                    (occ["agent_id"],)).fetchone() is not None:
        raise _Refusal(STOPPED, "barred")
    link_id, incarnation = _pick_link(conn, authority, state, occ, store_id=store_id, now=now)
    grant = state.policy.grants[link_id]
    billing, calls, tunnel = _auth.agent_link_terms(grant)
    limits = accounting.Limits(run_ceiling=grant["run_ceiling"],
                               budget_window_usd_micros=grant["budget_window_usd_micros"],
                               scheduled_window_key=window.scheduled_window_key, span_start=window.span_start,
                               span_end=window.span_end, billing=billing, budget_run_calls=calls,
                               budget_run_tunnel_bytes=tunnel)
    conn.execute("SAVEPOINT agent_launch")
    try:
        taken = accounting.reserve(conn, occ, executor, limits=limits)
        if isinstance(taken, accounting.Refused):
            raise _Refusal(taken.state, taken.reason)
        slot = slots.take(conn, slots.OCCURRENCE, occ["occurrence_id"], now, over)
        if slot is None:
            raise _Retry(NO_SLOT, "every slot is held")
    except BaseException:
        # SQLite may already have rolled the whole transaction back (RAISE(ROLLBACK), SQLITE_FULL), and the savepoint
        # with it: then there is nothing to undo, and the error that did it is the one raised
        if conn.in_transaction:
            conn.execute("ROLLBACK TO agent_launch")
            conn.execute("RELEASE agent_launch")
        raise
    conn.execute("RELEASE agent_launch")
    return link_id, incarnation, slot


# --- the session key and the spawn seam (P7-0 §3.1; the join levain.qbroker.fence.AgentFences reads) -----------------

def record_session_key(conn: sqlite3.Connection, occurrence_id: str, launch_epoch: int, session_key: str,
                       now: datetime) -> bool:
    """Record the S1 session id on a launched occurrence: one compare-and-set, inside the caller's write transaction.
    It writes only on the row still ``launched`` at ``launch_epoch`` (no revoke raised its epoch since), whose lease
    has not lapsed at ``now`` (a lapsed row is lapse recovery's even before :func:`takeover` runs), with no key yet,
    and only when no other occurrence names that key (AgentFences refuses a key two rows name). False when it wrote
    nothing; a key that is no S1 session id raises ``ValueError``."""
    if not conn.in_transaction:
        raise RuntimeError("a session key is recorded inside the caller's write transaction")
    if type(session_key) is not str or SESSION_KEY_RE.fullmatch(session_key) is None:
        raise ValueError("a session key is an S1 session id, s-<16 hex>")
    if type(launch_epoch) is not int:
        raise ValueError("launch_epoch is the integer the launch returned")
    # Stamps hold whole seconds: at a truncated ``now`` equal to the lease the untruncated clock may be past it
    # (takeover's ``at > lease``), so live is strictly before, and the boundary second is refused.
    cur = conn.execute(
        "UPDATE agent_occurrences SET session_key = ? WHERE occurrence_id = ? AND state = 'launched'"
        " AND launch_epoch = ? AND epoch = launch_epoch AND lease_until > ? AND session_key IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM agent_occurrences WHERE session_key = ?)",
        (session_key, occurrence_id, launch_epoch, _stamp(schedule._utc(now)), session_key))
    return cur.rowcount == 1


@dataclass(frozen=True)
class Started:
    """``spawned`` is True when the child ``spawn`` returned was released (``value`` is that child). False with
    ``reason`` otherwise: either the key could not be recorded and ``spawn`` was never called (``value`` None), or the
    row was ended or its lease lapsed before the release and the child was aborted unreleased (``value`` the child)."""
    spawned: bool
    reason: str | None = None
    value: object = None


GATE_REFUSED = 75   # the gate's exit when it reads anything but the release byte (EOF: its holder closed or died)
GATE_NO_EXEC = 127  # the gate's exit when the released argv could not be exec'd
# The gate's exit when it cannot vouch that the executor's group is gone: the group did not drain within
# GATE_DRAIN_S of its SIGKILL (complement D1 r3: on Linux a member reparented to a PID 1 that does not reap stays a
# zombie, and zombies still count), or something after the executor's fork failed (codex MED r3: a thread that could
# not be made or started, a handler that could not be installed). Positive, so it is reserved rather than negative,
# and NEVER a termination proof. An executor that itself exits with this code reads the same: unproven, the safe side.
GATE_UNPROVEN = 76
GATE_DRAIN_S = 10.0
# The gate, then the lifeline (P6-5b r3: codex HIGH r2, complement MED r2). Before the release: EOF exits
# GATE_REFUSED, nothing ran. On the release byte, with SIGTERM/SIGINT/SIGHUP blocked, the gate forks an ANCHOR that
# leads a new process group (setpgid in both processes, so neither order races) and then forks the executor INTO that
# group (setpgid in both again; an executor that cannot join exits GATE_NO_EXEC without exec). The gate stays alone in
# its own group, so nothing the executor's group receives reaches it. The anchor is never reaped before the end, so
# the group's id cannot be reused while anyone signals it; it holds the read end of a pipe only the gate writes, and
# the gate's death by any signal (SIGKILL included) EOFs it and it SIGKILLs the group. The gate then installs handlers
# for the three signals (each records 128 + the signal and SIGKILLs the group) and unblocks them. A thread reaps the
# executor; it then SIGKILLs the group (whatever the executor left there, a backgrounded child), reaps the anchor,
# waits until no member of the group is left, and only then exits with the executor's status (a signal death as 128
# + the signal) or a handled signal's 128 + signal. The main thread reads on the lifeline; EOF (the holder's abort or
# death) SIGKILLs the group and leaves the exit to the reaper. A handled signal records its status only while the
# reaper has not yet taken the executor's (complement D3 r3). The drain waits at most ``drain_s``, then exits
# GATE_UNPROVEN. Everything after the executor's fork sits in one failure path: SIGKILL the group, let the reaper (or,
# if it never started, the main thread) reap the executor and anchor and drain, then exit GATE_UNPROVEN (a reaper
# that had already drained before the failure exits the executor's status, which is then true). Every exit
# of the gate other than GATE_UNPROVEN is >= 0 and follows the group's end; GATE_UNPROVEN and a gate killed outright
# (status < 0) prove nothing about the group.
def _gate(drain_s: float) -> str:
    return ("import os, signal, sys, threading, time\n"
         "fd = int(sys.argv[1])\n"
         f"if os.read(fd, 1) != b'1':\n    sys.exit({GATE_REFUSED})\n"
         "SIGS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)\n"
         "signal.pthread_sigmask(signal.SIG_BLOCK, SIGS)\n"
         "tie_r, tie_w = os.pipe()\n"
         "anchor = os.fork()\n"
         "if anchor == 0:\n"
         "    try:\n"
         "        os.setpgid(0, 0)\n"
         "        os.close(fd)\n"
         "        os.close(tie_w)\n"
         "        while os.read(tie_r, 1):\n"
         "            pass\n"
         "    finally:\n"
         "        if os.getpgrp() == os.getpid():\n"   # never the gate's group
         "            os.killpg(os.getpgrp(), signal.SIGKILL)\n"
         "        os._exit(0)\n"
         "os.close(tie_r)\n"
         "try:\n"
         "    os.setpgid(anchor, anchor)\n"
         "except OSError:\n"
         "    pass\n"
         "pid = os.fork()\n"
         "if pid == 0:\n"
         "    try:\n"
         "        os.setpgid(0, anchor)\n"
         "        signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGS)\n"
         "        os.close(fd)\n"
         "        os.execvp(sys.argv[2], sys.argv[2:])\n"
         "    finally:\n"
         f"        os._exit({GATE_NO_EXEC})\n"
         "try:\n"
         "    os.setpgid(pid, anchor)\n"
         "except OSError:\n"
         "    pass\n"   # EACCES: it exec'd, so it joined first; ESRCH: it is gone
         "ending = threading.Lock()\n"
         "status = []\n"
         "failed = []\n"
         "def kill_group(code=None):\n"
         "    if ending.acquire(blocking=False):\n"   # held for good once the reaper took the executor's status
         "        if code is not None:\n"
         "            status.append(code)\n"
         "        try:\n"
         "            os.killpg(anchor, signal.SIGKILL)\n"
         "        except OSError:\n"
         "            pass\n"
         "        ending.release()\n"
         "def on_signal(signum, frame):\n"
         "    kill_group(128 + signum)\n"
         "def finish():\n"
         "    code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])\n"
         "    ending.acquire()\n"
         "    try:\n"
         "        os.killpg(anchor, signal.SIGKILL)\n"
         "    except OSError:\n"
         "        pass\n"
         "    os.waitpid(anchor, 0)\n"
         f"    deadline = time.monotonic() + {float(drain_s)!r}\n"
         "    while True:\n"
         "        try:\n"
         "            os.killpg(anchor, 0)\n"
         "        except OSError:\n"
         "            break\n"
         "        if time.monotonic() >= deadline:\n"
         f"            return {GATE_UNPROVEN}\n"
         "        time.sleep(0.01)\n"
         "    if failed:\n"
         f"        return {GATE_UNPROVEN}\n"
         "    return status[0] if status else code if code >= 0 else 128 - code\n"
         "def run_reaper():\n"
         f"    code = {GATE_UNPROVEN}\n"
         "    try:\n"
         "        code = finish()\n"
         "    finally:\n"
         "        os._exit(code)\n"
         "started = False\n"
         "try:\n"
         "    reaper = threading.Thread(target=run_reaper, daemon=True)\n"
         "    reaper.start()\n"
         "    started = True\n"
         "    for s in SIGS:\n"
         "        signal.signal(s, on_signal)\n"
         "    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGS)\n"
         "    while os.read(fd, 1):\n"
         "        pass\n"
         "    kill_group()\n"
         "except BaseException:\n"   # codex MED r3: any failure after the executor's fork ends the run here, in order
         "    try:\n"
         "        failed.append(True)\n"
         "        kill_group()\n"
         "        if started:\n"
         "            while True:\n"
         "                reaper.join(3600)\n"
         "        finish()\n"
         "    finally:\n"
         f"        os._exit({GATE_UNPROVEN})\n"
         "while True:\n"
         "    reaper.join(3600)\n")


_GATE = _gate(GATE_DRAIN_S)


def gate_proves_termination(returncode: int | None) -> bool:
    """True only when a gate's ``returncode`` proves its executor's group is gone: an int (``bool`` excluded) that is
    >= 0 and not :data:`GATE_UNPROVEN`. ``None`` (not reaped), a negative status (the gate killed outright) and
    :data:`GATE_UNPROVEN` prove nothing."""
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return False
    return returncode >= 0 and returncode != GATE_UNPROVEN


class GatedChild:
    """A child started blocked: a ``python -I`` gate holds the read end of a pipe and runs ``argv`` only after it
    reads the release byte; EOF (the holder closed its end, or died) exits :data:`GATE_REFUSED` without exec. The gate
    leads its own session (``start_new_session``), and this object holds its unreaped ``Popen``, so the holder may
    signal it (BUILD_PLAN §2, rule (a)); nobody signals it by a stored pid.

    The pipe is also the run's LIFELINE (codex HIGH, P6-5b r1): :meth:`release` writes the byte and KEEPS the write
    end, and the gate keeps the read end. The gate runs ``argv`` in a process group of its own, apart from the gate's
    (P6-5b r3), and SIGKILLs that group when the write end closes (the holder's :meth:`abort`, or the holder's death by
    any signal, SIGKILL included, which the kernel answers by closing its fds), when the executor exits on its own
    (what it left in the group, a backgrounded child), and when the gate takes SIGTERM, SIGINT or SIGHUP (a ``stop()``
    signalling ``process``: it exits 128 + the signal). A gate killed outright (SIGKILL) cannot act; the group's anchor
    sees its death and SIGKILLs the group itself.

    ``process`` is the gate. Every status it exits with is >= 0, and every one other than :data:`GATE_UNPROVEN` comes
    only after no member of the executor's group is left: the executor's own (a signal death as 128 + the signal,
    :data:`GATE_NO_EXEC` when ``argv`` could not be exec'd), or 128 + a signal the gate handled while the executor
    still ran. Two statuses prove nothing about the group: a negative ``returncode`` (the gate killed outright) and
    :data:`GATE_UNPROVEN` (the group did not drain within :data:`GATE_DRAIN_S` of its SIGKILL, or the gate's own setup
    after the executor's fork failed). So an executor's ``stop()`` that reads termination off this child reads it through
    :func:`gate_proves_termination` (never a bare non-negative test), and signals ``process`` with SIGTERM, never
    SIGKILL first.

    What the lifeline cannot reach: a descendant that leaves the group (``setsid``, its own ``setpgid``) or that this
    user may not signal, and a holder that ``fork``\\ s without ``exec``, whose copy then holds the run open."""

    def __init__(self, argv: Sequence[str], *, stdin=subprocess.DEVNULL, stdout=None, stderr=None,
                 env: Mapping[str, str] | None = None, cwd: str | Path | None = None) -> None:
        if not argv or any(type(a) is not str for a in argv):
            raise ValueError("a gated child's argv is a non-empty list of strings")
        read, write = os.pipe()             # both non-inheritable; pass_fds hands the child the read end only
        self._write: int | None = write
        self.released = False
        try:
            self.process = subprocess.Popen([sys.executable, "-I", "-c", _GATE, str(read), *argv], pass_fds=(read,),
                                            start_new_session=True, close_fds=True, stdin=stdin, stdout=stdout,
                                            stderr=stderr, env=None if env is None else dict(env), cwd=cwd)
        except BaseException:
            os.close(write)
            self._write = None
            raise
        finally:
            os.close(read)

    def release(self) -> None:
        """Let the child run. Once only. The write end stays open in this holder: it is the run's lifeline."""
        if self._write is None or self.released:
            raise RuntimeError("the gated child was already released or aborted")
        self.released = True
        os.write(self._write, b"1")

    def abort(self, timeout: float = 5.0) -> int:
        """Close the pipe and reap the gate. Unreleased, the gate exits :data:`GATE_REFUSED` and nothing ran;
        released, the gate kills the executor's group and exits once it is gone. A gate that does not exit in
        ``timeout`` has its own group killed through this unreaped handle (its pid is that group's id and cannot be
        reused while it is unreaped), and the executor group's anchor then kills that group. Returns the gate's exit
        status: negative only in that last case, which proves nothing about the executor's group (nor does
        :data:`GATE_UNPROVEN`)."""
        if self._write is not None:
            fd, self._write = self._write, None
            os.close(fd)
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return self.process.wait()


def _still_ours(conn: sqlite3.Connection, occurrence_id: str, launch_epoch: int, session_key: str,
                now: datetime) -> bool:
    return conn.execute(
        "SELECT 1 FROM agent_occurrences WHERE occurrence_id = ? AND state = 'launched' AND launch_epoch = ?"
        " AND epoch = launch_epoch AND session_key = ? AND lease_until > ?",
        (occurrence_id, launch_epoch, session_key, _stamp(schedule._utc(now)))).fetchone() is not None


def start_run(store: AgentStore | Path | str | None, occurrence_id: str, launch_epoch: int, session_key: str,
              spawn: Callable[[str], GatedChild], *, clock: Callable[[], datetime]) -> Started:
    """The spawn seam (the fire calls it once the S1 session is open, before claude starts). In order:

    1. the key is recorded by :func:`record_session_key` in its own ``BEGIN IMMEDIATE``, which COMMITS first, so the
       per-call fence can join the session to its occurrence from the child's first call;
    2. ``spawn(session_key)`` starts the child GATED (a :class:`GatedChild`, or anything with its ``release`` and
       ``abort``): it exists but has run nothing;
    3. a second ``BEGIN IMMEDIATE`` re-reads the row (still ``launched`` at ``launch_epoch``, naming this key, its
       lease live at ``clock()`` read inside the lock) and releases the child INSIDE that transaction. A takeover or a
       revoke's epoch raise needs the same write lock, so none lands between the check and the release: the child
       starts only while its row is live. A row that moved is answered by ``abort()`` (the child never execs) and
       ``Started(False, reason, child)``.

    A refused compare-and-set in step 1 returns ``Started(False, reason)`` and a store error raises; either way
    ``spawn`` is not called, and the row, holding its slot, ends by lapse recovery (:func:`takeover`). Any error after
    ``spawn`` returned, the release transaction's COMMIT included, aborts the child (released or not) before it
    propagates. A released :class:`GatedChild` runs only while its holder keeps it: the caller holds
    ``Started.value`` for the run's life, and its exit or death ends the run. A caller whose ``spawn`` raised ends the
    run itself (:func:`settle_terminal`): a retry with another key is refused (the row names one)."""
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    with store.write() as conn:
        recorded = record_session_key(conn, occurrence_id, launch_epoch, session_key, clock())
    if not recorded:
        return Started(False, "the session key was not recorded: the occurrence is not launched at that epoch, "
                              "its lease lapsed, it already names a session, or the key names another occurrence")
    child = spawn(session_key)
    released = False
    try:
        with store.write() as conn:
            if _still_ours(conn, occurrence_id, launch_epoch, session_key, clock()):
                child.release()
                released = True
    except BaseException as exc:
        # released or not: a release whose transaction did not end cleanly (its COMMIT raised) is aborted too, and
        # for a GatedChild that ends the run through the lifeline. An abort that fails rides on the error as a note.
        try:
            child.abort()
        except BaseException as also:
            exc.add_note(f"start_run: the child's abort also failed: {also!r}")
        raise
    if not released:
        child.abort()
        return Started(False, "the occurrence was ended, fenced or its lease lapsed before the run was released; "
                              "the child never ran", child)
    return Started(True, None, child)


# --- the fire: a separate session-leader process per launched occurrence (BUILD_PLAN P6-5, §2) ------------------------

FIRES = "fires"
FIRE_UNWIRED = 3       # the module entry's exit after its claim, for an agent no run body is registered for
FIRE_NO_IDENTITY = 4   # the fire cannot read its own identity, or its command line does not name its occurrence
FIRE_NOT_HELD = 5      # the occurrence is not the one this fire holds: no run was admitted, no effect was reached
_OCCURRENCE_RE = re.compile(r"oc-[0-9a-f]{32}")


def _occurrence(occurrence_id: str) -> str:
    if type(occurrence_id) is not str or _OCCURRENCE_RE.fullmatch(occurrence_id) is None:
        raise ValueError("an occurrence id is oc-<32 hex>")
    return occurrence_id


def fire_argv(store_dir: Path | str, occurrence_id: str) -> list[str]:
    """The fire's command line. It names the occurrence (BUILD_PLAN §2: the identity check needs the id on it). The
    ``levain agents _fire`` verb delegates here once ``levain/agents/cli.py``'s owner wires it."""
    return [sys.executable, "-m", "levain.agents.controller", "_fire", "--store", str(Path(store_dir)),
            _occurrence(occurrence_id)]


def fire_record_path(store_dir: Path | str, occurrence_id: str) -> Path:
    """``<store>/fires/<occurrence_id>.json``: the fire's identity record, read by an executor's ``stop()``."""
    return Path(store_dir) / FIRES / f"{_occurrence(occurrence_id)}.json"


_thread_factory = threading.Thread   # spawn_fire's reaper thread; a seam so a test replaces it, not threading's own


def spawn_fire(store_dir: Path | str, occurrence_id: str, *, argv: Sequence[str] | None = None,
               log: Path | str | None = None, env: Mapping[str, str] | None = None) -> subprocess.Popen:
    """Start the fire for an occurrence whose launch transaction has COMMITTED (the caller calls this after
    :func:`launch` returned ``launched``): a separate OS process leading its own session, stdin closed, output
    appended to ``log`` (default ``<store>/fires/<occurrence_id>.log``). The caller need not wait on it and never
    signals it; stopping a fire is an executor's ``stop()``, through the identity record the fire writes. A daemon
    thread of the launcher waits on the returned ``Popen``, so a fire nobody waits on leaves no zombie (its
    ``returncode`` is set as it exits; a caller's own ``wait()`` returns the same status). A launcher that exits
    first leaves the fire to be reaped by init. A reaper that cannot start has the fire's group SIGKILLed and reaped
    before its error propagates. ``argv`` defaults to :func:`fire_argv`."""
    occurrence_id = _occurrence(occurrence_id)
    fires = Path(store_dir) / FIRES
    fires.mkdir(mode=0o700, exist_ok=True)
    with open(log if log is not None else fires / f"{occurrence_id}.log", "ab") as out:
        proc = subprocess.Popen(list(argv) if argv is not None else fire_argv(store_dir, occurrence_id),
                                start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=None if env is None else dict(env))
    try:
        _thread_factory(target=proc.wait, name=f"reap-{occurrence_id}", daemon=True).start()
    except BaseException:
        # codex MED r2: no reaper means no owner; the fire leads its session and is unreaped, so its pid is its
        # group's id here and cannot name another group
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    return proc


def claim_fire(conn: sqlite3.Connection, occurrence_id: str, pid: int, now: datetime) -> int | None:
    """The fire claims its own row: one compare-and-set writing ``pid`` on a row still ``launched`` with no fire yet, at
    its launch epoch, inside its lease at ``now``. Returns the ``launch_epoch`` it runs under, or ``None`` when it
    wrote nothing (a second fire, a late fire on a row lapse recovery ended, a fenced or lapsed row)."""
    if not conn.in_transaction:
        raise RuntimeError("a fire claims its row inside the caller's write transaction")
    if type(pid) is not int or pid < 1:
        raise ValueError("a fire's pid is a positive integer")
    cur = conn.execute("UPDATE agent_occurrences SET pid = ? WHERE occurrence_id = ? AND state = 'launched'"
                       " AND pid IS NULL AND epoch = launch_epoch AND lease_until > ?",
                       (pid, occurrence_id, _stamp(schedule._utc(now))))
    if cur.rowcount != 1:
        return None
    return conn.execute("SELECT launch_epoch FROM agent_occurrences WHERE occurrence_id = ?",
                        (occurrence_id,)).fetchone()[0]


def _record_is_dead(record: Path) -> bool:
    """True only when the fire ``record`` names is known gone (no such pid, or the pid now another process's start
    time). A record that cannot be read, or a process that cannot be told, is not dead: it is kept."""
    from levain.consent.delivery import process_running
    try:
        held = json.loads(record.read_text())
        pid, started = held["pid"], held["started"]
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return type(started) is str and bool(started) and process_running(pid, started) is False


def _claimed_pid(store: AgentStore, occurrence_id: str) -> object:
    with store.read() as conn:
        row = conn.execute("SELECT pid FROM agent_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
    return None if row is None else row[0]


def fire(store: AgentStore | Path | str, occurrence_id: str, *,
         run: Callable[[AgentStore, str, int], int], clock: Callable[[], datetime]) -> int:
    """The fire process's body, in order: write the identity record (pid, ``ps`` start time and command line, read
    on itself with :func:`levain.consent.delivery.process_identity`; temp, then an exclusive link, under the record's
    lock: a fire that finds a record naming a process that may still run returns 0 before claiming, so no fire ever
    replaces a live fire's identity; a record whose process is gone, by
    :func:`levain.consent.delivery.process_running`, on a row no fire claimed, is replaced: that fire died between its
    link and its claim), then :func:`claim_fire`, then
    ``run(store, occurrence_id, launch_epoch)`` (P6-4a's supervisor). A fire that cannot identify itself, or whose
    command line does not name its occurrence, claims nothing and returns :data:`FIRE_NO_IDENTITY`. A fire that does
    not win the claim returns 0 having done nothing else: no effect, no compose, no spend."""
    from levain.consent.delivery import process_identity   # here: the consent package is not the controller's closure
    occurrence_id = _occurrence(occurrence_id)
    if not isinstance(store, AgentStore):
        store = open_agent_store(store)
    pid = os.getpid()
    identity = process_identity(pid)
    if identity is None or occurrence_id not in identity[1].split():
        return FIRE_NO_IDENTITY
    record = fire_record_path(store.directory, occurrence_id)
    record.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = record.with_name(f".{record.name}.{pid}.tmp")
    tmp.write_text(json.dumps({"occurrence_id": occurrence_id, "pid": pid, "started": identity[0],
                               "args": identity[1]}, sort_keys=True))
    try:
        with open(record.with_name(f".{record.name}.lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)    # fires of one occurrence read and replace the record one at a time
            try:
                os.link(tmp, record)    # exclusive: a fire never overwrites the identity another one's stop() reads
            except FileExistsError:
                if not _record_is_dead(record) or _claimed_pid(store, occurrence_id) is not None:
                    return 0    # a fire that may run, or one that claimed: the record stays the row's fire's
                os.replace(tmp, record)   # the recorded fire died between its link and its claim
    finally:
        tmp.unlink(missing_ok=True)
    with store.write() as conn:
        launch_epoch = claim_fire(conn, occurrence_id, pid, clock())
    if launch_epoch is None:
        return 0
    return run(store, occurrence_id, launch_epoch)


def _unwired_run(store: AgentStore, occurrence_id: str, launch_epoch: int) -> int:
    print(f"levain agents _fire {occurrence_id}: no run body is wired for this agent; the occurrence ends by lapse "
          "recovery", file=sys.stderr)
    return FIRE_UNWIRED


RunBody = Callable[[AgentStore, str, int], int]
DREAM = "dream"


def _dream_body() -> RunBody:
    from levain.dream import agent as dream_agent   # here: the dream is not the controller's import closure
    return dream_agent.dream_run


# An agent name -> the resolver of its run body. A name absent here has no run body: its fire refuses unwired.
RUN_BODIES: dict[str, Callable[[], RunBody]] = {DREAM: _dream_body}


def _agent_name(store: AgentStore, agent_id: str) -> str | None:
    with store.read() as conn:
        row = conn.execute("SELECT name FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return None if row is None else row[0]


def admit_run(store: AgentStore, occurrence_id: str, launch_epoch: int, *, journal=None) -> bool:
    """Admit the occurrence's run in the agent ``RunJournal`` before any body runs, once, for the occurrence this fire
    holds: ``launched`` at ``launch_epoch`` with no revoke since (the claim's own epoch guard). Returns False, having
    started nothing, for any other row. Idempotent: ``RunJournal.start`` of a known run id is a no-op, so a replay (or
    another admitter of the same run) keeps the first admission. ``journal`` defaults to the store's own, the one the
    supervisor's default also opens."""
    with store.read() as conn:
        row = conn.execute("SELECT run_id, binding_id FROM agent_occurrences WHERE occurrence_id = ?"
                           " AND state = 'launched' AND launch_epoch = ? AND epoch = launch_epoch",
                           (occurrence_id, launch_epoch)).fetchone()
    if row is None:
        return False
    from levain.autonomic.journal import RunJournal   # here: the journal is not the controller's import closure
    (journal if journal is not None else RunJournal(store.directory)).start(row[0], binding_id=row[1])
    return True


def run_agent(store: AgentStore, occurrence_id: str, launch_epoch: int, *,
              bodies: Mapping[str, Callable[[], RunBody]] | None = None) -> int:
    """The fire's run body: the one registered for the occurrence's agent (:data:`RUN_BODIES`), else
    :func:`_unwired_run`. The agent is read from the occurrence's own row, never from the fire's command line. A row
    or agent that cannot be read has no run body."""
    with store.read() as conn:
        row = conn.execute("SELECT agent_id FROM agent_occurrences WHERE occurrence_id = ?",
                           (occurrence_id,)).fetchone()
    name = None if row is None else _agent_name(store, row[0])
    resolve = (RUN_BODIES if bodies is None else bodies).get(name) if name is not None else None
    if resolve is None:
        return _unwired_run(store, occurrence_id, launch_epoch)
    if not admit_run(store, occurrence_id, launch_epoch):
        print(f"levain agents _fire {occurrence_id}: the occurrence is not launched at this fire's epoch; no run was "
              "admitted and no effect was reached", file=sys.stderr)
        return FIRE_NOT_HELD
    return resolve()(store, occurrence_id, launch_epoch)


def _default_input_check(store: AgentStore, occurrence_id: str) -> InputCheck | None:
    """The ``on_input`` hook for an occurrence whose caller passed none: the dream's, for the dream; ``None`` (input
    present) for every other agent. A dream whose install facts are absent gets a hook that raises when run, which
    :func:`check_input` reads as dead input."""
    with store.read() as conn:
        row = conn.execute("SELECT a.name FROM agent_occurrences o JOIN agents a ON a.agent_id = o.agent_id"
                           " WHERE o.occurrence_id = ?", (occurrence_id,)).fetchone()
    if row is None or row[0] != DREAM:
        return None
    from levain.dream import agent as dream_agent
    return dream_agent.install_on_input(store.directory)


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="levain agents _fire")
    parser.add_argument("verb", choices=["_fire"])
    parser.add_argument("--store", required=True)
    parser.add_argument("occurrence_id")
    args = parser.parse_args(argv)
    return fire(args.store, args.occurrence_id, run=run_agent, clock=lambda: datetime.now(timezone.utc))


# --- the coordinator-lease probes over dispatch.db (levain.agents.slots.probe_over) -----------------------------------

DISPATCH_TIMEOUT = 0.25   # [judged] seconds a probe waits on a locked dispatch.db; a timeout is "cannot tell"
_OVER_KINDS = ("failed", "expired", "ended")


def dispatch_probes(directory: Path | str) -> tuple[Callable[[str], bool | None], Callable[[str], bool | None]]:
    """``(launch_over, session_alive)`` reading ``<directory>/dispatch.db`` read-only, one short connection per call.

    ``launch_over``: True when the launch row is gone or its latest event is failed, expired or ended; False while it
    is pending or active; None when it has no event. "Gone" is safe to answer for both callers, :func:`slots.take`
    (fed :func:`slots.expired_launches`) and the coordinator's sweep (fed EVERY held launch): the coordinator takes a
    launch's slot only after it read the launch row, and launch rows are append-only, so a missing row is a launch of
    another store, never one not yet committed. ``session_alive``: False only when the coordinator's watcher swept
    the launch (``launch_sweeps``: it saw the session not running); otherwise None, because the store holds no live
    process; a dead session of an active launch is the watcher's to end, which ``launch_over`` then reads. A file or
    table that cannot be read raises, which :func:`slots.probe_over` reads as "cannot tell" (the slot is kept)."""
    path = Path(directory) / "dispatch.db"

    def _read(sql: str, launch_id: str) -> list[tuple]:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, isolation_level=None,
                               timeout=DISPATCH_TIMEOUT)
        try:
            conn.execute("BEGIN")
            return conn.execute(sql, (launch_id,)).fetchall()
        finally:
            conn.close()

    def launch_over(launch_id: str) -> bool | None:
        rows = _read("SELECT l.launch_id, (SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id"
                     " ORDER BY e.seq DESC LIMIT 1) FROM launches l WHERE l.launch_id = ?", launch_id)
        if not rows:
            return True
        kind = rows[0][1]
        return None if kind is None else kind in _OVER_KINDS

    def session_alive(launch_id: str) -> bool | None:
        return False if _read("SELECT 1 FROM launch_sweeps WHERE launch_id = ?", launch_id) else None

    return launch_over, session_alive


if __name__ == "__main__":
    sys.exit(_main())
