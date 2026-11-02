"""levain.qbroker.fence — the production fence source for the queue broker's GATE (P7-2).

Kept out of :mod:`levain.qbroker.broker` so the broker's import closure does not take the authority replay with it;
whoever wires a broker with a fence imports this module. A0 section 2.3's predicate is
:func:`levain.agent_authority.agent_fence`, which this module only calls."""
from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain.agent_authority import AgentCoverMoved, agent_fence
from levain.qbroker.broker import Resolved

__all__ = ["FENCE_DB_TIMEOUT", "AgentFences"]

# how long the fence waits on autonomic.db before the read fails (and GATE answers limits-failed)
FENCE_DB_TIMEOUT = 0.25


class AgentFences:
    """The production :data:`FenceSource`: A0 section 2.3's agent fence, evaluated per call on a read-only handle to
    ``autonomic.db`` (``db``) through :func:`levain.agent_authority.agent_fence`, which this class only calls.

    A run whose ``run_id`` no agent occurrence carries is not an agent's and is current. The authority replay is
    refreshed when the peek answers cover moved, at most once per ``refresh_interval`` seconds across all slots
    (one shared snapshot), and the check is made again once; a still unreadable cover raises, so GATE refuses."""

    def __init__(self, db: Path | str, authority: Any, store_id: str, *, refresh_interval: float = 1.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._db, self._authority, self._store_id = Path(db), authority, store_id
        self._interval, self._clock, self._monotonic = refresh_interval, clock, monotonic
        self._lock = threading.Lock()
        self._refreshed: float | None = None

    def __call__(self, who: Resolved) -> str | None:
        if who.run is None:
            return None
        conn = sqlite3.connect(f"file:{quote(str(self._db))}?mode=ro", uri=True, isolation_level=None,
                               timeout=FENCE_DB_TIMEOUT)
        try:
            row = conn.execute("SELECT occurrence_id, agent_id, link_id, link_incarnation, spec_hash, binding_id,"
                               " launch_epoch FROM agent_occurrences WHERE run_id = ?",
                               (who.run["run_id"],)).fetchone()
            if row is None:
                return None
            occurrence = dict(zip(("occurrence_id", "agent_id", "link_id", "link_incarnation", "spec_hash",
                                   "binding_id", "launch_epoch"), row))
            fence, fence_now = agent_fence(occurrence, authority=self._authority, store_id=self._store_id,
                                           now=self._clock)
            try:
                current = fence_now(conn)
            except AgentCoverMoved:
                self._refresh()
                current = fence_now(conn)
            if current == fence:
                return None
            return self._reason(conn, occurrence)
        finally:
            conn.close()

    def _refresh(self) -> None:
        with self._lock:
            now = self._monotonic()
            if self._refreshed is None or now - self._refreshed >= self._interval:
                self._authority.refresh()
                self._refreshed = now

    def _reason(self, conn: Any, occurrence: Mapping[str, Any]) -> str:
        """The current side's reason, read from the same columns the fence digests; the link's liveness is the
        remainder (revoked, expired or replaced)."""
        state, epoch = conn.execute("SELECT state, epoch FROM agent_occurrences WHERE occurrence_id = ?",
                                    (occurrence["occurrence_id"],)).fetchone()
        if state != "launched":
            return f"the occurrence left launched (now {state})"
        if epoch != occurrence["launch_epoch"]:
            return "the occurrence's epoch moved"
        if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                        (occurrence["agent_id"],)).fetchone() is not None:
            return "the agent is barred"
        return "the agent's authority link is no longer live (revoked, expired or replaced)"
