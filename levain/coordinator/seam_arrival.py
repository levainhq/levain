"""Arrival signals for the open (C15 S4): the verified leave of an away mark, and the silence gap of the operator's
activity.

* **A verified leave** is read from the consent queue through ``ConsentQueue.away_mark`` (``as_is``: a read never
  creates or migrates the queue file) and counts only when ``AwayMark.left`` is true, which the queue sets only when the
  stored leave re-verifies against the authority log (a key revoked since leaves the mark standing). Any state this
  module cannot read or does not recognise is NO leave and a note saying why: the consent boundary fails closed, and a
  failed read never reads as "the operator is back".
* **The gap** is measured between the operator's two latest activity records, never since an open: an open the
  coordinator built or skipped says nothing about when the operator last spoke.

Reads only the stores it is handed; the writes are :func:`levain.coordinator.seam_open.record_arrival` (inside the
caller's fenced transaction) and :func:`levain.coordinator.seam_build.build`.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.coordinator.seam_build import build
from levain.coordinator.seam_open import (OpenBuild, active_coordinator_fence, open_tables_present, record_arrival,
                                         stored_build)
from levain.coordinator.store import CoordError, CoordStore

__all__ = ["ACTIVITY_SOURCES", "Activity", "LIGHT_GAP_HOURS", "Leave", "gap_threshold", "ingest_leaves", "is_acked",
           "light_gap", "operator_activity", "verified_leaves"]

#: the gap between two operator-activity records under which a light open is not needed, in hours. Levain's own pick,
#: a flag on the verb (``--gap-hours``) until real use has tuned it
LIGHT_GAP_HOURS = 4.0
#: the agents whose away marks are read (the two the consent queue keeps marks for)
_AGENTS = ("claude_code", "codex")
#: operator activity: a drop, or an operator-authored chat message
ACTIVITY_SOURCES = ("drops", "chat_messages")


@dataclass(frozen=True)
class Leave:
    agent: str
    mark_id: str
    queue_id: str
    set_at: str = ""     # when the verified mark was set: the leaves' order, oldest first

    @property
    def key(self) -> str:
        return f"{self.agent}:{self.mark_id}"


@dataclass(frozen=True)
class Activity:
    gseq: int
    at: str
    ref: str          # "drop:<id>" or "message:<id>": the signal key of a light open


def _queue_id(queue_dir: Path) -> str:
    """The queue's own id, read-only (the mark's leave was signed over it). Raises when it cannot be read."""
    path = (queue_dir / "queue.db").resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'queue_id'").fetchone()
    finally:
        conn.close()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise ValueError("the queue holds no id")
    return row[0]


def verified_leaves(queue_dir: Path | str) -> tuple[list[Leave], list[str]]:
    """``(leaves, notes)``: each agent whose latest away mark has a leave that verifies now, the oldest mark first. An
    agent whose settings, store or authority cannot be read contributes no leave and one note; a mark whose leave does
    not verify (``left`` false: no leave, a revoked key, a changed row) contributes neither."""
    from levain.cockpit.consent import parse_stamp
    from levain.consent import away
    from levain.consent.adapters.claude_code import away_mark_of

    qdir = Path(queue_dir)
    got: list[tuple[Any, Leave]] = []
    notes: list[str] = []
    for agent in _AGENTS:
        try:
            mark = away_mark_of(qdir, away.claude_code.settings_from(away.read(qdir, agent)), agent)
            if mark is None or mark.left is not True:
                continue
            # known edge: the queue id is a second read of the path, so a queue.db swapped between the two reads
            # pairs one queue's mark with another's id; only a hand-replaced queue file does that, never real use
            got.append((parse_stamp(mark.set_at), Leave(agent, str(mark.id), _queue_id(qdir), mark.set_at)))
        except Exception as exc:  # noqa: BLE001 - an unreadable consent state is never a leave
            notes.append(f"{agent}: the away mark could not be read ({type(exc).__name__}), so no leave was consumed")
    got.sort(key=lambda pair: pair[0])
    return [lv for _, lv in got], notes


def is_acked(store: CoordStore, open_id: int) -> bool:
    with closing(store._connect()) as conn:
        return (open_tables_present(conn) and
                conn.execute("SELECT 1 FROM open_acks WHERE open_id = ?", (open_id,)).fetchone() is not None)


def ingest_leaves(store: CoordStore, queue_dir: Path | str | None, token: Any) -> tuple[list[OpenBuild], list[str]]:
    """Consume every verified leave: record its arrival signal and build its operator open, keyed ``agent:mark_id``.
    ``(builds, notes)``, builds oldest away mark first. A leave already built returns its stored build and writes
    nothing; a leave whose build is refused (a degraded or oversized brief) is a note, and the next call tries again.
    Fenced by the coordinator's token: a caller with none ingests nothing."""
    leaves, notes = verified_leaves(Path(queue_dir) if queue_dir is not None else Path(store.directory))
    builds: list[OpenBuild] = []
    for lv in leaves:
        try:
            with closing(store._connect()) as conn:
                have = stored_build(conn, "operator", "away_off", lv.key)
            if have is None:
                with store._write("coordinator", token) as (conn, now):
                    active_coordinator_fence(conn, now, token, "an arrival signal")
                    # looked up again under the fence: a racing coordinator may have consumed the leave since
                    if stored_build(conn, "operator", "away_off", lv.key) is None:
                        record_arrival(conn, now, agent=lv.agent, mark_id=lv.mark_id, queue_id=lv.queue_id)
                have = build(store, kind="operator", signal_kind="away_off", signal_key=lv.key, queue_dir=queue_dir,
                             token=token)
            if have is None:
                notes.append(f"{lv.agent}: the leave of mark {lv.mark_id} opened nothing yet (no build was returned)")
                continue
            builds.append(have)
        except CoordError as exc:
            notes.append(f"{lv.agent}: the leave of mark {lv.mark_id} opened nothing yet ({str(exc)[:160]})")
    return builds, notes


def operator_activity(store: CoordStore, limit: int = 2) -> list[Activity]:
    """The operator's ``limit`` latest activity records, newest first, ordered and timed by the store-wide sequence
    (one clock for a drop and a message). A table the store lacks contributes nothing; an unreadable store raises."""
    with read_transaction(store.open_readonly) as conn:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        parts = []
        if "drops" in have:
            parts.append("SELECT gseq, 'drop:' || drop_id AS ref FROM drops WHERE gseq IS NOT NULL")
        if "chat_messages" in have:
            parts.append("SELECT gseq, 'message:' || message_id FROM chat_messages WHERE author_kind = 'operator' "
                         "AND gseq IS NOT NULL")
        if not parts or "gseq" not in have:
            return []
        rows = conn.execute(f"SELECT a.gseq, g.at, a.ref FROM ({' UNION ALL '.join(parts)}) a JOIN gseq g ON "
                            "g.gseq = a.gseq ORDER BY a.gseq DESC LIMIT ?", (limit,)).fetchall()
    return [Activity(*r) for r in rows]


def gap_threshold(gap_hours: Any) -> timedelta:
    """``gap_hours`` as the gap a light open needs, checked before any read: a positive, finite number of hours that a
    duration can hold."""
    if not isinstance(gap_hours, (int, float)) or isinstance(gap_hours, bool) or not math.isfinite(gap_hours) \
            or gap_hours <= 0:
        raise CoordError("--gap-hours is a positive number of hours")
    try:
        return timedelta(hours=gap_hours)
    except OverflowError:
        raise CoordError(f"--gap-hours is a positive number of hours a duration can hold (at most "
                         f"{timedelta.max.total_seconds() / 3600:.0f})") from None


def light_gap(store: CoordStore, gap_hours: float) -> tuple[bool, str, Activity | None]:
    """``(needed, why, latest)``: whether the operator's latest activity record follows a gap of at least
    ``gap_hours`` after the one before it (no earlier record is an unbounded gap), the sentence for the verb to print
    and the record the light open is keyed by. No activity at all needs nothing."""
    from levain.cockpit.consent import parse_stamp

    threshold = gap_threshold(gap_hours)
    got = operator_activity(store, 2)
    if not got:
        return False, "no light open needed: no operator activity is recorded", None
    if len(got) == 1:
        return True, "the operator's first recorded activity", got[0]
    gap = parse_stamp(got[0].at) - parse_stamp(got[1].at)
    if gap < threshold:
        return False, f"no light open needed: previous activity {gap.total_seconds() / 3600:.1f} h ago", got[0]
    return True, f"previous activity {gap.total_seconds() / 3600:.1f} h before this", got[0]
