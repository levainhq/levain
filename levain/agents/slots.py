"""levain.agents.slots — the slot table (A0 r7 §2.4; BUILD_PLAN P6-3a).

Coordinator launches and named fires share one cap through ``agent_slots``. The cap is the ``agent_settings`` row
``max_slots`` (default 2). Every function runs inside the caller's write transaction, so a slot commits or rolls back
with whatever else the caller writes (the launch transaction, P6-5a; the terminal transaction that releases it).

An occurrence slot has no ``expires_at``: it is released by the transaction that writes the occurrence's terminal
state, never by a clock. A coordinator slot is a lease (``owner_kind='launch'``, ``expires_at = taken + LEASE_S``)
that its watcher renews; it is reclaimed only when it has expired AND its ``launches`` row is terminal or its
``session_key`` is not alive. Liveness is not this module's to know, so it comes in as ``session_alive``; ``None``
means "cannot tell", which keeps the slot (fail-closed for the cap).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

__all__ = ["MAX_SLOTS_KEY", "DEFAULT_MAX_SLOTS", "LEASE_S", "OCCURRENCE", "LAUNCH", "max_slots", "held", "reclaim",
           "take", "release", "renew", "free"]

MAX_SLOTS_KEY = "max_slots"
DEFAULT_MAX_SLOTS = 2  # judged; a default, not a measurement (A0 §2.4)
LEASE_S = 900
OCCURRENCE = "occurrence"
LAUNCH = "launch"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_TERMINAL = ("failed", "expired", "ended")


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("a slot clock reading is timezone-aware")
    return moment.astimezone(timezone.utc).strftime(_STAMP)


def _need_txn(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("a slot is taken or released inside the caller's write transaction")


def max_slots(conn: sqlite3.Connection) -> int:
    """The cap. An absent row is the default; a stored value that is not a positive integer fails closed (raises)."""
    row = conn.execute("SELECT value FROM agent_settings WHERE key = ?", (MAX_SLOTS_KEY,)).fetchone()
    if row is None:
        return DEFAULT_MAX_SLOTS
    value = int(row[0])  # ValueError on garbage: a cap nobody can read is not a cap of zero or of infinity
    if value < 1:
        raise ValueError(f"{MAX_SLOTS_KEY} is a positive integer")
    return value


def held(conn: sqlite3.Connection) -> list[tuple[int, str, str, str | None]]:
    """``(slot, owner_kind, owner_id, expires_at)`` for every held slot, lowest first."""
    return conn.execute("SELECT slot, owner_kind, owner_id, expires_at FROM agent_slots ORDER BY slot").fetchall()


def _launch_over(conn: sqlite3.Connection, launch_id: str, session_alive: Callable[[str], bool] | None) -> bool:
    """True when a coordinator slot's launch no longer needs it: no such launch, a terminal one, or a session that
    is not alive. A store that never held the coordinator's tables has no launches, so none is alive."""
    try:
        row = conn.execute("SELECT session_key FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()
        if row is None:
            return True
        last = conn.execute("SELECT kind FROM launch_events WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                            (launch_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return True
    if last is not None and last[0] in _TERMINAL:
        return True
    return session_alive is not None and not session_alive(row[0])


def reclaim(conn: sqlite3.Connection, now: datetime, session_alive: Callable[[str], bool] | None = None) -> list[int]:
    """Free every expired coordinator lease whose launch is over (A0 §2.4); returns the slots freed. An occurrence
    slot is never reclaimed here, and an unexpired lease is never reclaimed, whatever its launch looks like."""
    _need_txn(conn)
    at = _stamp(now)
    freed = []
    rows = conn.execute("SELECT slot, owner_id FROM agent_slots WHERE owner_kind = ? AND expires_at IS NOT NULL"
                        " AND expires_at <= ? ORDER BY slot", (LAUNCH, at)).fetchall()
    for slot, owner in rows:
        if _launch_over(conn, owner, session_alive):
            conn.execute("DELETE FROM agent_slots WHERE slot = ?", (slot,))
            freed.append(slot)
    return freed


def free(conn: sqlite3.Connection, now: datetime, session_alive: Callable[[str], bool] | None = None) -> int:
    """How many slots a :func:`take` could get right now, after a reclaim (which this call performs)."""
    reclaim(conn, now, session_alive)
    return max(0, max_slots(conn) - conn.execute("SELECT COUNT(*) FROM agent_slots").fetchone()[0])


def take(conn: sqlite3.Connection, kind: str, owner_id: str, now: datetime,
         session_alive: Callable[[str], bool] | None = None) -> int | None:
    """Take the lowest free slot for ``owner_id``, or return ``None`` when the cap is full (the caller keeps its
    refusal path). Reclaims first. A second take by the same owner returns the slot it already holds, unchanged.
    A launch's slot is a lease of :data:`LEASE_S` seconds; an occurrence's has no expiry."""
    if kind not in (OCCURRENCE, LAUNCH):
        raise ValueError(f"a slot owner is {OCCURRENCE!r} or {LAUNCH!r}")
    _need_txn(conn)
    mine = conn.execute("SELECT slot, owner_kind FROM agent_slots WHERE owner_id = ?", (owner_id,)).fetchone()
    if mine is not None:
        if mine[1] != kind:
            raise ValueError("an owner id holds a slot under one kind only")
        return mine[0]
    if free(conn, now, session_alive) < 1:
        return None
    used = {r[0] for r in conn.execute("SELECT slot FROM agent_slots")}
    slot = next(n for n in range(1, len(used) + 2) if n not in used)
    expires = _stamp(now + timedelta(seconds=LEASE_S)) if kind == LAUNCH else None
    conn.execute("INSERT INTO agent_slots (slot, owner_kind, owner_id, taken_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                 (slot, kind, owner_id, _stamp(now), expires))
    return slot


def release(conn: sqlite3.Connection, owner_id: str) -> bool:
    """Free the slot ``owner_id`` holds, in the transaction that writes its terminal state. False when it held none."""
    _need_txn(conn)
    return conn.execute("DELETE FROM agent_slots WHERE owner_id = ?", (owner_id,)).rowcount > 0


def renew(conn: sqlite3.Connection, launch_id: str, now: datetime) -> bool:
    """The coordinator watcher's per-pass renewal: ``expires_at = now + LEASE_S``. False when the lease is gone
    (reclaimed meanwhile), so the watcher knows its launch no longer holds a slot."""
    _need_txn(conn)
    return conn.execute("UPDATE agent_slots SET expires_at = ? WHERE owner_id = ? AND owner_kind = ?",
                        (_stamp(now + timedelta(seconds=LEASE_S)), launch_id, LAUNCH)).rowcount > 0
