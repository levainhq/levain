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

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
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

__all__ = ["CLAIMED", "DUPLICATE", "OVERLAP", "LEASE_S", "TickResult", "occurrence_id", "run_id", "claim",
           "close_no_slot", "tick", "heartbeat", "takeover", "Window", "LaunchResult", "launch",
           "record_session_key", "Started", "start_run", "dispatch_probes", "SESSION_KEY_RE", "SESSION_GRACE_S",
           "Ended", "settle_terminal", "GatedChild", "GATE_REFUSED", "fire_argv", "fire_record_path", "spawn_fire",
           "claim_fire", "fire"]

CLAIMED = "claimed"
DUPLICATE = "duplicate"
OVERLAP = "skipped-overlap"

LEASE_S = 900  # A0 r7 section 2.3 [judged]: the coordinator slot's lease

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
    no_slot: list[tuple[str, str]] = field(default_factory=list)     # (agent_id, key): admitted, past grace, no slot


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


def close_no_slot(conn: sqlite3.Connection, now: datetime, over: Collection[str] = ()) -> list[tuple[str, str]]:
    """End as ``missed`` / ``no_slot`` every ``admitted`` occurrence past its grace while the cap is full (A0 §2.4:
    a named occurrence is retried each tick, and only the grace running out ends it). Returns ``(agent_id, key)``.
    It reclaims the expired coordinator leases in ``over`` (probed by the caller, :func:`slots.probe_over`) first, and
    does nothing while a slot is free: an occurrence still admitted past grace with a slot free was not left by the
    cap, so ``no_slot`` would be a false reason. A cap nobody can read decides nothing here: the tick goes on for
    the other agents and the occurrences stay admitted. Runs in the caller's write transaction."""
    try:
        if slots.free(conn, now, over) > 0:
            return []
    except ValueError:
        return []
    at = _stamp(now)
    closed = []
    for oid, agent_id, kind, key, admitted_at in conn.execute(
            "SELECT occurrence_id, agent_id, kind, key, admitted_at FROM agent_occurrences WHERE state = 'admitted'"
            " ORDER BY admitted_at, occurrence_id").fetchall():
        since = _admitted_since(kind, key, admitted_at)
        if since is None or schedule.classify(since, now) != schedule.MISSED:
            continue
        conn.execute("UPDATE agent_occurrences SET state = 'missed', reason = 'no_slot', ended_at = ?"
                     " WHERE occurrence_id = ? AND state = 'admitted'", (at, oid))
        slots.release(conn, oid)  # an admitted occurrence holds nothing (A0 §2.4), so this frees nothing today
        closed.append((agent_id, key))
    return closed


def tick(store: AgentStore | Path | str | None, clock: Callable[[], datetime],
         binding_of: Callable[[str, str], str | None],
         session_alive: Callable[[str], bool | None] | None = None,
         launch_over: Callable[[str], bool | None] | None = None) -> TickResult:
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
    # first, so an occurrence the cap held past its grace no longer reads as a live previous one to this tick's claims
    with store.read() as conn:
        expired = slots.expired_launches(conn, now)
    over = slots.probe_over(expired, launch_over, session_alive)  # outside any write lock: a probe can be slow
    with store.write() as conn:
        for agent_id, key in close_no_slot(conn, now, over):
            out.no_slot.append((agent_id, key))
            out.missed[agent_id] = out.missed.get(agent_id, 0) + 1  # the miss streak (P6-2b) counts it
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
             usage: accounting.Usage | None = None) -> bool:
    """Fenced takeover (A0 r7 section 2.3, lapse recovery): end a ``launched`` row whose lease has lapsed, writing
    ``epoch + 1`` and ``crash_recovered`` in one statement, settling its reservation and freeing its slot in the same
    transaction (A0 §2.4's lapse row, through :func:`_end_launched`, the one end :func:`settle_terminal` uses). The
    lease is read here, inside the write transaction, and the ``UPDATE`` is guarded by the exact value read, so a
    heartbeat that landed first or a takeover that already ran leaves it with nothing to change (``False``), and
    nothing is settled or freed. A live lease is never taken over; a lease that is absent or unreadable is not
    evidence of a lapse, so it is refused too. ``usage`` is unreadable unless the caller can read the executor's
    record by now (§2.4: then all of the reservation is debited, or the caps recorded on a subscription run). It takes
    the caller's write lock first, as :func:`heartbeat` does."""
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
    ended = _end_launched(conn, occurrence_id, "stopped", "crash_recovered",
                          usage if usage is not None else _UNREAD, at, "lease_until = ?", (row[0],), bump=True)
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
    """``outcome`` is :data:`LAUNCHED`, :data:`STOPPED` or :data:`SKIPPED_BUDGET` (the row's new state; ``reason`` is the
    one written), or :data:`NO_SLOT`, :data:`UNCLASSIFIED` or :data:`NOT_ADMITTED` (nothing written; ``reason`` says
    why). ``launch_epoch`` is what a fire hands :func:`start_run`."""
    outcome: str
    reason: str | None = None
    slot: int | None = None
    link_id: str | None = None
    launch_epoch: int | None = None


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


def launch(store: AgentStore | Path | str | None, occurrence_id: str, *, authority, executor: accounting.Executor,
           window: Window, now: datetime,
           launch_over: Callable[[str], bool | None] | None = None,
           session_alive: Callable[[str], bool | None] | None = None) -> LaunchResult:
    """The launch transaction (A0 §2.5 ``admitted -> launched``). ``authority`` is an
    :class:`~levain.agent_authority.AgentAuthority`; it is refreshed here, before the write lock. In one
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
    try:
        with store.write() as conn:
            row = conn.execute("SELECT agent_id, kind, key, state, epoch, window_key, spec_hash, binding_id"
                               " FROM agent_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if row is None:
                raise _Retry(NOT_ADMITTED, "no such occurrence")
            occ = dict(zip(("agent_id", "kind", "key", "state", "epoch", "window_key", "spec_hash", "binding_id"), row),
                       occurrence_id=occurrence_id)
            if occ["state"] != "admitted":
                raise _Retry(NOT_ADMITTED, f"the occurrence is {occ['state']}")
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
_GATE = ("import os, sys\n"
         "fd = int(sys.argv[1])\n"
         "go = os.read(fd, 1)\n"
         "os.close(fd)\n"
         f"if go != b'1':\n    sys.exit({GATE_REFUSED})\n"
         "os.execvp(sys.argv[2], sys.argv[2:])\n")


class GatedChild:
    """A child started blocked: a ``python -I`` gate holds the read end of a pipe and ``execvp``\\ s ``argv`` only after
    it reads the release byte; EOF (the holder closed its end, or died) exits :data:`GATE_REFUSED` without exec. The
    child leads its own session (``start_new_session``), and this object holds its unreaped ``Popen``, so the holder
    may signal it (BUILD_PLAN §2, rule (a)); nobody signals it by a stored pid."""

    def __init__(self, argv: Sequence[str], *, stdin=subprocess.DEVNULL, stdout=None, stderr=None,
                 env: Mapping[str, str] | None = None, cwd: str | Path | None = None) -> None:
        if not argv or any(type(a) is not str for a in argv):
            raise ValueError("a gated child's argv is a non-empty list of strings")
        read, write = os.pipe()             # both non-inheritable; pass_fds hands the child the read end only
        self._write: int | None = write
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
        """Let the child exec. Once only."""
        if self._write is None:
            raise RuntimeError("the gated child was already released or aborted")
        fd, self._write = self._write, None
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)

    def abort(self, timeout: float = 5.0) -> int:
        """Never release it: close the pipe (the gate exits :data:`GATE_REFUSED`) and reap it. A gate that does not
        exit in ``timeout`` is killed through this unreaped handle. Returns its exit status."""
        if self._write is not None:
            fd, self._write = self._write, None
            os.close(fd)
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
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
    ``spawn`` returned aborts the child before it propagates. A caller whose ``spawn`` raised ends the run itself
    (:func:`settle_terminal`): a retry with another key is refused (the row names one)."""
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
    finally:
        if not released:
            child.abort()
    if not released:
        return Started(False, "the occurrence was ended, fenced or its lease lapsed before the run was released; "
                              "the child never ran", child)
    return Started(True, None, child)


# --- the fire: a separate session-leader process per launched occurrence (BUILD_PLAN P6-5, §2) ------------------------

FIRES = "fires"
FIRE_UNWIRED = 3       # the module entry's exit after its claim: no run body exists until P6-4a
FIRE_NO_IDENTITY = 4   # the fire cannot read its own identity, or its command line does not name its occurrence
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


def spawn_fire(store_dir: Path | str, occurrence_id: str, *, argv: Sequence[str] | None = None,
               log: Path | str | None = None, env: Mapping[str, str] | None = None) -> subprocess.Popen:
    """Start the fire for an occurrence whose launch transaction has COMMITTED (the caller calls this after
    :func:`launch` returned ``launched``): a separate OS process leading its own session, stdin closed, output
    appended to ``log`` (default ``<store>/fires/<occurrence_id>.log``). The caller does not wait on it and never
    signals it; stopping a fire is an executor's ``stop()``, through the identity record the fire writes. ``argv``
    defaults to :func:`fire_argv`."""
    occurrence_id = _occurrence(occurrence_id)
    fires = Path(store_dir) / FIRES
    fires.mkdir(mode=0o700, exist_ok=True)
    with open(log if log is not None else fires / f"{occurrence_id}.log", "ab") as out:
        return subprocess.Popen(list(argv) if argv is not None else fire_argv(store_dir, occurrence_id),
                                start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, env=None if env is None else dict(env))


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


def fire(store: AgentStore | Path | str, occurrence_id: str, *,
         run: Callable[[AgentStore, str, int], int], clock: Callable[[], datetime]) -> int:
    """The fire process's body, in order: write the identity record (pid, ``ps`` start time and command line, read
    on itself with :func:`levain.consent.delivery.process_identity`; temp, then an exclusive link: a fire that finds
    a record already there returns 0 before claiming, so no fire ever replaces another's identity), then
    :func:`claim_fire`, then
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
        os.link(tmp, record)    # exclusive: a second fire never overwrites the identity the first one's stop() reads
    except FileExistsError:
        return 0
    finally:
        tmp.unlink()
    with store.write() as conn:
        launch_epoch = claim_fire(conn, occurrence_id, pid, clock())
    if launch_epoch is None:
        return 0
    return run(store, occurrence_id, launch_epoch)


def _unwired_run(store: AgentStore, occurrence_id: str, launch_epoch: int) -> int:
    print(f"levain agents _fire {occurrence_id}: no run body is wired yet (P6-4a); the occurrence ends by lapse "
          "recovery", file=sys.stderr)
    return FIRE_UNWIRED


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="levain agents _fire")
    parser.add_argument("verb", choices=["_fire"])
    parser.add_argument("--store", required=True)
    parser.add_argument("occurrence_id")
    args = parser.parse_args(argv)
    return fire(args.store, args.occurrence_id, run=_unwired_run, clock=lambda: datetime.now(timezone.utc))


# --- the coordinator-lease probes over dispatch.db (levain.agents.slots.probe_over) -----------------------------------

DISPATCH_TIMEOUT = 0.25   # [judged] seconds a probe waits on a locked dispatch.db; a timeout is "cannot tell"
_OVER_KINDS = ("failed", "expired", "ended")


def dispatch_probes(directory: Path | str) -> tuple[Callable[[str], bool | None], Callable[[str], bool | None]]:
    """``(launch_over, session_alive)`` reading ``<directory>/dispatch.db`` read-only, one short connection per call.

    ``launch_over``: True when the launch row is gone or its latest event is failed, expired or ended; False while it
    is pending or active; None when it has no event. "Gone" is safe to answer only because :func:`slots.probe_over`
    is fed :func:`slots.expired_launches`: a launch is probed only after its own slot lease (taken plus
    ``slots.LEASE_S``, renewed by its watcher) expired, never while its coordinator may not have committed the row. ``session_alive``: False only when the coordinator's watcher swept
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
