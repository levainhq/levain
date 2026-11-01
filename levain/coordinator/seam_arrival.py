"""Arrival signals (C15 S4): what makes a ``coord open --light`` build, and nothing that builds it.

* **A verified away OFF.** :func:`verified_leaves` asks the consent queue for each agent's latest away MARK without
  creating or migrating the queue file (``claude_code.away_mark_of``: no file is no mark; the store is read ``as_is``)
  and keeps the marks whose ``left`` is true, which it is only when a stored leave re-verifies against the authority log
  now. A leave signed by a key revoked since is not ``left``, so it opens nothing. The mark is the LATEST mark only: a
  leave superseded by a new away before the open runs opens nothing (named in the design, s8).
* **Silence.** :func:`silence` measures the gap between the two newest OPERATOR activity records (drops and operator
  chat messages), never the time since an open. At or over the gap it names the newest record as the signal.
* :func:`ingest` writes each verified leave as an ``arrival_signals`` row under the coordinator's fence.

Nothing here watches: the leave is consumed whenever ``coord open --light`` runs. Stdlib plus the consent adapters.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.coordinator import seam_open
from levain.coordinator.morning import AGENTS
from levain.coordinator.store import CoordError

__all__ = ["SILENCE_GAP_HOURS", "Leave", "Silence", "ingest", "silence", "verified_leaves"]

#: the gap, in hours, between two operator activity records at or over which the newer one is an arrival; Levain's own
#: pick, tuned on real use (design s4 and s6: the residue)
SILENCE_GAP_HOURS = 4.0


@dataclass(frozen=True)
class Leave:
    """One agent's verified away OFF: the mark it left and the queue it was read from."""
    agent: str
    mark_id: str
    queue_id: str
    set_at: str


@dataclass(frozen=True)
class Silence:
    """The gap rule's answer. ``fired`` whether the newest record is an arrival; ``key`` the signal key when it is
    (``drop:<id>`` or ``chat:<id>``); ``why`` the line the verb prints when it is not."""
    fired: bool
    key: str | None
    gap_hours: float | None
    why: str


def _queue_id(queue_dir: Path) -> str:
    """The queue's id, read-only (never creating the file): a leave names the queue it was verified in."""
    uri = (queue_dir / "queue.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()
    if row is None:
        raise CoordError(f"the consent queue in {queue_dir} records no queue id")
    return str(row[0])


def verified_leaves(queue_dir: Path | str, agents: tuple[str, ...] = AGENTS) -> tuple[list[Leave], list[str]]:
    """``(leaves, notes)``: each agent in ``agents`` whose latest away mark has a leave that verifies now, and one
    line per agent whose queue could not be read (an unreadable store is never read as a leave). No queue file, no
    mark and a mark still away are all quietly no leave. The queue file is never created or migrated."""
    from levain.consent import away
    from levain.consent.adapters import claude_code

    qdir = Path(queue_dir)
    leaves: list[Leave] = []
    notes: list[str] = []
    for agent in agents:
        try:
            settings = claude_code.settings_from(away.read(qdir, agent))
            mark = claude_code.away_mark_of(qdir, settings, agent)
            if mark is None or not mark.left:
                continue
            leaves.append(Leave(agent, mark.id, _queue_id(qdir), mark.set_at))
        except Exception as exc:  # noqa: BLE001 - an unreadable queue opens nothing and says so
            notes.append(f"{agent}: the away mark could not be read ({type(exc).__name__}: {str(exc)[:120]})")
    return leaves, notes


def ingest(store: Any, token: Any, leaves: list[Leave]) -> list[Leave]:
    """Record each verified leave as an ``arrival_signals`` row under the coordinator's fence, in one write. A leave
    already recorded is the same signal (no new row). Returns the leaves, so the caller builds one open per leave."""
    if not leaves:
        return []
    with store._write("coordinator", token) as (conn, now):
        seam_open.active_coordinator_fence(conn, now, token, "an arrival signal")
        for lv in leaves:
            seam_open.record_arrival(conn, now, agent=lv.agent, mark_id=lv.mark_id, queue_id=lv.queue_id)
    return leaves


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _activity(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """The two newest operator activity records as ``(key, time)``, newest first: drops (the operator's words) and
    operator chat messages, ordered by their own time."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    rows: list[tuple[datetime, str, str]] = []
    if "drops" in tables:
        rows += [(_when(t), f"drop:{k}", t) for k, t in
                 conn.execute("SELECT drop_id, received_at FROM drops ORDER BY received_at DESC, rowid DESC LIMIT 2")]
    if "chat_messages" in tables:
        rows += [(_when(t), f"chat:{k}", t) for k, t in
                 conn.execute("SELECT message_id, at FROM chat_messages WHERE author_kind = 'operator' "
                              "ORDER BY at DESC, rowid DESC LIMIT 2")]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [(key, t) for _w, key, t in rows[:2]]


def silence(store: Any, *, gap_hours: float = SILENCE_GAP_HOURS) -> Silence:
    """The gap rule. The newest operator activity record fires when the record before it is at least ``gap_hours``
    older. With fewer than two records there is no gap to measure, and the answer says so rather than firing."""
    if gap_hours < 0:
        raise CoordError("the gap is a number of hours, zero or more")
    with closing(store._connect()) as conn:
        recent = _activity(conn)
    if not recent:
        return Silence(False, None, None, "no light open needed: no operator activity record yet")
    if len(recent) < 2:
        return Silence(False, None, None, "no light open needed: this is the only operator activity record, so there "
                                          "is no previous one to measure a gap from")
    (key, newest), (_prev_key, previous) = recent
    gap = (_when(newest) - _when(previous)).total_seconds() / 3600.0
    if gap >= gap_hours:
        return Silence(True, key, gap, f"operator activity {key} came {gap:.1f} h after the one before it")
    return Silence(False, None, gap, f"no light open needed: previous activity {gap:.1f} h ago "
                                     f"(under {gap_hours:g} h)")
