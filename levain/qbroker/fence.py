"""levain.qbroker.fence — the production fence source for the queue broker's GATE (P7-2).

Kept out of :mod:`levain.qbroker.broker` so the broker's import closure does not take the authority replay with it;
whoever wires a broker with a fence imports this module. A0 section 2.3's predicate is
:func:`levain.agent_authority.agent_fence`, which this module only calls.

The join (L3 r1, codex HIGH): a GATE names its session by the S1 session id QUEUE-RESOLVE answers (``who.session``,
``s-<16 hex>``). The occurrence carries that id in ``session_key``, which the fire records on its occurrence when it opens
the slot session (P7-0 sections 2.1 step 2, 3.1 and 5, "record key"). The run ids are two namespaces and never join: an
occurrence's ``run_id`` is ``run-<32 hex>`` (:func:`levain.agents.controller.run_id`), a ticket's is CONTROL's
``r-<16 hex>`` (:func:`levain.slot.tickets.new_run_id`)."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from levain.agent_authority import AgentCoverMoved, agent_fence
from levain.qbroker.broker import FenceUnavailable, Resolved

__all__ = ["FENCE_DB_TIMEOUT", "AgentFences", "Unfenceable"]

# how long the fence waits on autonomic.db before the read fails (and GATE answers limits-failed)
FENCE_DB_TIMEOUT = 0.25
# the autonomic store id's form, as the authority log validates an agent link's store_id (levain.agents.bars)
_STORE_ID = re.compile(r"store-[0-9a-f]{32}")
_OCCURRENCE_COLUMNS = ("occurrence_id", "agent_id", "link_id", "link_incarnation", "spec_hash", "binding_id",
                       "launch_epoch")


class Unfenceable:
    """The fence of a broker that cannot read the agent store: every dispatch session's GATE answers
    ``limits-failed`` with ``why``, never go (an unfenced dispatch session would be an agent's run unchecked)."""

    def __init__(self, why: str) -> None:
        self.why = why

    def __call__(self, who: Resolved) -> str | None:
        raise FenceUnavailable(self.why)


class AgentFences:
    """The production :data:`FenceSource`: A0 section 2.3's agent fence, evaluated per call on a read-only handle to
    ``autonomic.db`` (``db``) through :func:`levain.agent_authority.agent_fence`, which this class only calls.

    The session's occurrence is the one row whose ``session_key`` is the session (module docstring). No row: the
    session is no agent's and is current. Two or more rows: the store is inconsistent and the fence refuses
    (:class:`FenceUnavailable`). ``store_id`` is the autonomic store's id agent links name; ``None`` reads it from the
    store's ``meta`` row on each check.

    The occurrence, its bar and the reason are read in one read transaction, so the reason names the snapshot the
    digest came from. When the peek answers cover moved, the replay is refreshed and the check made again once, both
    under one lock: a refresh never runs beside a retry (``AgentAuthority.refresh`` clears its snapshot while it
    replays). Concurrent cover-moved checks share one refresh (a check whose peek failed before a refresh succeeded
    replays nothing more), so a revoke that lands just after a refresh is still read and answered ``fenced``; after a
    refresh that raised, none is tried for ``refresh_interval`` seconds from its end. A still unreadable cover
    raises, so GATE refuses."""

    def __init__(self, db: Path | str, authority: Any, store_id: str | None = None, *,
                 refresh_interval: float = 1.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._db, self._authority, self._store_id = Path(db), authority, store_id
        self._interval, self._clock, self._monotonic = refresh_interval, clock, monotonic
        self._lock = threading.Lock()
        self._generation = 0                      # successful refreshes so far
        self._failed_at: float | None = None      # when the last refresh that raised ended

    def __call__(self, who: Resolved) -> str | None:
        if who.run is None:
            return None
        conn = sqlite3.connect(f"file:{quote(str(self._db))}?mode=ro", uri=True, isolation_level=None,
                               timeout=FENCE_DB_TIMEOUT)
        try:
            rows = conn.execute(f"SELECT {', '.join(_OCCURRENCE_COLUMNS)} FROM agent_occurrences"
                                " WHERE session_key = ? LIMIT 2", (who.session,)).fetchall()
            if not rows:
                return None
            if len(rows) > 1:
                raise FenceUnavailable(f"more than one agent occurrence names session {who.session}")
            occurrence = dict(zip(_OCCURRENCE_COLUMNS, rows[0]))
            fence, fence_now = agent_fence(occurrence, authority=self._authority,
                                           store_id=self._store_id or self._read_store_id(conn), now=self._clock)
            seen = self._generation
            try:
                return self._check(conn, occurrence, fence, fence_now)
            except AgentCoverMoved:
                with self._lock:
                    self._refresh(seen)
                    return self._check(conn, occurrence, fence, fence_now)
        finally:
            conn.close()

    def _check(self, conn: Any, occurrence: Mapping[str, Any], fence: str,
               fence_now: Callable[[Any], str]) -> str | None:
        conn.execute("BEGIN")
        try:
            if fence_now(conn) == fence:
                return None
            return self._reason(conn, occurrence)
        finally:
            conn.execute("ROLLBACK")

    def _refresh(self, seen: int) -> None:
        """Under ``self._lock``. Replays unless one succeeded since the caller's failed peek began (``seen``: the
        generation it read then), or one raised less than ``refresh_interval`` seconds ago (measured from its end)."""
        if self._generation != seen:
            return
        if self._failed_at is not None and self._monotonic() - self._failed_at < self._interval:
            return
        try:
            self._authority.refresh()
        except BaseException:
            self._failed_at = self._monotonic()
            raise
        self._failed_at = None
        self._generation += 1

    @staticmethod
    def _read_store_id(conn: Any) -> str:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
        except sqlite3.Error as exc:
            raise FenceUnavailable(f"the agent store's id cannot be read ({type(exc).__name__})") from exc
        if row is None or type(row[0]) is not str or _STORE_ID.fullmatch(row[0]) is None:
            raise FenceUnavailable("the agent store holds no well-formed store id")
        return row[0]

    @staticmethod
    def _reason(conn: Any, occurrence: Mapping[str, Any]) -> str:
        """The current side's reason, read in the digest's transaction from the columns the fence digests; what is
        left when state, epoch and bar are as launched is the link (the launch columns are immutable by trigger)."""
        row = conn.execute("SELECT state, epoch FROM agent_occurrences WHERE occurrence_id = ?",
                           (occurrence["occurrence_id"],)).fetchone()
        if row is None:
            return "the occurrence is no longer in the agent store"
        state, epoch = row
        if state != "launched":
            return f"the occurrence left launched (now {state})"
        if epoch != occurrence["launch_epoch"]:
            return "the occurrence's epoch moved"
        if conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                        (occurrence["agent_id"],)).fetchone() is not None:
            return "the agent is barred"
        return "the agent's authority link is no longer live or no longer the one it launched under"
