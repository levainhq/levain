"""levain.agents.slots — the slot table (A0 r7 §2.4; BUILD_PLAN P6-3a).

Desk launches and named fires share one cap through ``agent_slots``. The cap is the ``agent_settings`` row
``max_slots`` (default 2). Every write function runs inside the caller's write transaction, so a slot commits or rolls
back with whatever else the caller writes (the launch transaction, P6-5a; the terminal transaction that releases it).

An occurrence slot has no ``expires_at``: it is released by the transaction that writes the occurrence's terminal
state, never by a clock. A desk slot is a lease (``owner_kind='launch'``, ``expires_at = taken + LEASE_S``)
that its watcher renews; it is reclaimed only when it has expired AND its launch is over or its session is not alive.

The launches live in the desk's store, a different file from the agent tables, so this module never reads
them. What it needs comes in as two injected callables, probed by :func:`probe_over` BEFORE the write transaction
opens (a probe can be slow, and a write lock is not held across it): ``launch_over(launch_id)`` says whether the
launch is gone or terminal, and ``session_alive(launch_id)`` whether its session still runs. ``None``, a missing
callable or a callable that raises all mean "cannot tell", and that keeps the slot (fail-closed for the cap).

Left open on purpose, P6-2b's: an occurrence admitted past its grace while a slot is free is not ended here, so it
is not recorded as a ``no_slot`` miss that the cap did not cause.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection, Iterable
from datetime import datetime, timedelta, timezone

__all__ = ["MAX_SLOTS_KEY", "DEFAULT_MAX_SLOTS", "LEASE_S", "OCCURRENCE", "LAUNCH", "max_slots", "held",
           "expired_launches", "probe_over", "reclaim", "take", "release", "renew", "free"]

MAX_SLOTS_KEY = "max_slots"
DEFAULT_MAX_SLOTS = 2  # judged; a default, not a measurement (A0 §2.4)
LEASE_S = 900
OCCURRENCE = "occurrence"
LAUNCH = "launch"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


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


def expired_launches(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """The launch ids whose desk lease has expired at ``now``: what to probe before the write transaction."""
    return [r[0] for r in conn.execute("SELECT owner_id FROM agent_slots WHERE owner_kind = ? AND expires_at IS NOT NULL"
                                       " AND expires_at <= ? ORDER BY slot", (LAUNCH, _stamp(now))).fetchall()]


def _ask(probe: Callable[[str], bool | None] | None, launch_id: str) -> bool | None:
    if probe is None:
        return None
    try:
        answer = probe(launch_id)
    except Exception:  # noqa: BLE001 - a probe that cannot answer is "cannot tell", never a reason to free a slot
        return None
    return answer if isinstance(answer, bool) else None


def probe_over(launch_ids: Iterable[str], launch_over: Callable[[str], bool | None] | None = None,
               session_alive: Callable[[str], bool | None] | None = None) -> frozenset[str]:
    """The launch ids whose slot may be freed: the launch is over, or it is not over and its session is not alive.
    Run it outside any write transaction. Anything unknown keeps the slot."""
    over = set()
    for launch_id in launch_ids:
        ended = _ask(launch_over, launch_id)
        if ended is True or (ended is False and _ask(session_alive, launch_id) is False):
            over.add(launch_id)
    return frozenset(over)


def reclaim(conn: sqlite3.Connection, now: datetime, over: Collection[str] = ()) -> list[int]:
    """Free every expired desk lease whose launch id is in ``over`` (A0 §2.4; see :func:`probe_over`);
    returns the slots freed. The row is re-checked in this transaction: a lease renewed since the probe is no longer
    expired and stays. An occurrence slot is never reclaimed here, whatever ``over`` holds."""
    _need_txn(conn)
    at = _stamp(now)
    freed = []
    for slot, owner in conn.execute("SELECT slot, owner_id FROM agent_slots WHERE owner_kind = ? AND expires_at IS NOT"
                                    " NULL AND expires_at <= ? ORDER BY slot", (LAUNCH, at)).fetchall():
        if owner in over and conn.execute("DELETE FROM agent_slots WHERE slot = ? AND owner_id = ? AND owner_kind = ?"
                                          " AND expires_at <= ?", (slot, owner, LAUNCH, at)).rowcount:
            freed.append(slot)
    return freed


def free(conn: sqlite3.Connection, now: datetime, over: Collection[str] = ()) -> int:
    """How many slots a :func:`take` could get right now, after a reclaim (which this call performs)."""
    reclaim(conn, now, over)
    return max(0, max_slots(conn) - conn.execute("SELECT COUNT(*) FROM agent_slots").fetchone()[0])


def take(conn: sqlite3.Connection, kind: str, owner_id: str, now: datetime,
         over: Collection[str] = ()) -> int | None:
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
    if free(conn, now, over) < 1:
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
    """The desk watcher's per-pass renewal: ``expires_at = now + LEASE_S``, never moving an expiry backward
    (a late or replayed renewal cannot shorten a lease). False when the lease is gone (reclaimed meanwhile), so the
    watcher knows its launch no longer holds a slot."""
    _need_txn(conn)
    return conn.execute("UPDATE agent_slots SET expires_at = MAX(COALESCE(expires_at, ''), ?)"
                        " WHERE owner_id = ? AND owner_kind = ?",
                        (_stamp(now + timedelta(seconds=LEASE_S)), launch_id, LAUNCH)).rowcount > 0
