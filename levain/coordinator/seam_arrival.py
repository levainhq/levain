"""Arrival signals (C15 S4): what makes a ``coord open --light`` build, and nothing that builds it.

* **A verified away OFF.** :func:`verified_leaves` asks the consent queue for each agent's latest away MARK without
  creating or migrating the queue file (``claude_code.away_mark_of``: no file is no mark; the store is read ``as_is``)
  and keeps the marks whose ``left`` is true, which it is only when a stored leave re-verifies against the authority log
  now. A leave signed by a key revoked since is not ``left``, so it opens nothing. The mark is the LATEST mark only: a
  leave superseded by a new away before it was ever recorded opens nothing (named in the design, s8).
* **Silence.** :func:`silence` measures the gap between the two newest OPERATOR activity records (drops and operator
  chat messages), never the time since an open. At or over the gap it names the newest record as the signal. It reads
  the coordinator's file ``mode=ro`` and makes nothing: no file, no table, no migration.
* :func:`ingest` writes each verified leave and a fired silence as an ``arrival_signals`` row under the coordinator's
  fence, the moment they are seen and before any build. ``arrival_signals`` is the durable to-do list: every recorded
  signal without an acknowledged build is built (or offered again) by every ``--light`` run, whether or not it is still
  the latest mark or the newest gap, so a run that died between recording and building loses nothing.

A silence row is ``agent`` :data:`SILENCE_AGENT`, ``mark_id`` the gap-ending record's key (``drop:<id>`` or
``chat:<id>``), ``queue_id`` :data:`SILENCE_QUEUE` (the record is the coordinator store's own, not a queue's).

Nothing here watches: the signals are consumed whenever ``coord open --light`` runs. Stdlib plus the consent adapters.
"""
from __future__ import annotations

import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction
from levain.coordinator import seam_open
from levain.coordinator.morning import AGENTS
from levain.coordinator.seam import instant
from levain.coordinator.store import CoordError, StoreAbsent

__all__ = ["SILENCE_AGENT", "SILENCE_GAP_HOURS", "SILENCE_QUEUE", "Leave", "Silence", "ingest", "pending", "signal_of",
           "silence", "verified_leaves"]

#: the gap, in hours, between two operator activity records at or over which the newer one is an arrival; Levain's own
#: pick, tuned on real use (design s4 and s6: the residue)
SILENCE_GAP_HOURS = 4.0
#: the ``agent`` of a silence row in ``arrival_signals`` (no consent agent is named this)
SILENCE_AGENT = "silence"
#: the ``queue_id`` of a silence row: the record it names is in the coordinator store, not a consent queue
SILENCE_QUEUE = "coordinator-store"
#: how many rows of each activity table the gap rule reads, by its time column and by insertion order, before it sorts
#: them by parsed instant (a string order of mixed-precision stamps is not their instants' order)
_FETCH = 32

Signal = tuple[str, str, str]           # (seam_kind, signal_kind, signal_key): what ``build`` takes


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
    (``drop:<id>`` or ``chat:<id>``); ``why`` the line the verb prints when it is not; ``notes`` one line per record
    or table the rule skipped because it could not read it."""
    fired: bool
    key: str | None
    gap_hours: float | None
    why: str
    notes: tuple[str, ...] = ()


def signal_of(agent: str, mark_id: str) -> Signal:
    """The build an ``arrival_signals`` row asks for: a silence row is a ``light`` open keyed by its record, any other
    an ``operator`` open keyed ``agent:mark_id``."""
    if agent == SILENCE_AGENT:
        return ("light", "silence", mark_id)
    return ("operator", "away_off", f"{agent}:{mark_id}")


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


def _todo(conn: sqlite3.Connection) -> tuple[list[Signal], dict[Signal, int]]:
    """``(todo, acked)`` over every recorded signal, in the order recorded: ``todo`` the ones with no acknowledged
    build (unbuilt, or built and never acknowledged: offered again, the same open), ``acked`` the rest, each to its
    open id."""
    if not seam_open.open_tables_present(conn):
        return [], {}
    todo: list[Signal] = []
    acked: dict[Signal, int] = {}
    for agent, mark_id in conn.execute("SELECT agent, mark_id FROM arrival_signals ORDER BY signal_id").fetchall():
        sig = signal_of(str(agent), str(mark_id))
        stored = seam_open.stored_build(conn, *sig)
        if stored is not None and conn.execute("SELECT 1 FROM open_acks WHERE open_id = ? LIMIT 1",
                                               (stored.open_id,)).fetchone() is not None:
            acked[sig] = stored.open_id
        elif sig not in todo:
            todo.append(sig)
    return todo, acked


def pending(store: Any) -> tuple[list[Signal], dict[Signal, int]]:
    """:func:`_todo`, read through the store's read-only accessor (``mode=ro``, ``query_only``): it makes and migrates
    nothing. No store, or one not yet current, has nothing recorded."""
    try:
        with read_transaction(store.open_readonly) as conn:
            return _todo(conn)
    except StoreAbsent:
        return [], {}


def ingest(store: Any, token: Any, leaves: list[Leave], quiet: Silence | None = None
           ) -> tuple[list[Signal], dict[Signal, int]]:
    """Record each verified leave, and ``quiet``'s signal when it fired, as an ``arrival_signals`` row under the
    coordinator's fence, in one write; a signal already recorded is the same row. Returns :func:`_todo` read inside
    that same transaction: what the caller builds (or offers again), and what is already acknowledged."""
    with store._write("coordinator", token) as (conn, now):
        seam_open.active_coordinator_fence(conn, now, token, "an arrival signal")
        for lv in leaves:
            seam_open.record_arrival(conn, now, agent=lv.agent, mark_id=lv.mark_id, queue_id=lv.queue_id)
        if quiet is not None and quiet.fired and quiet.key:
            seam_open.record_arrival(conn, now, agent=SILENCE_AGENT, mark_id=quiet.key, queue_id=SILENCE_QUEUE)
        return _todo(conn)


def _activity(conn: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[str]]:
    """``(records, notes)``: the two newest operator activity records as ``(key, time)``, newest first, from drops (the
    operator's words) and operator chat messages, ordered by their parsed instants (equal ones by insertion order). A
    row whose time does not parse, and a table the query cannot read, is skipped with a note, never an exception."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    reads: list[tuple[str, tuple[str, ...]]] = []
    if "drops" in tables:
        reads.append(("drop", ("SELECT drop_id, received_at, rowid FROM drops ORDER BY received_at DESC LIMIT ?",
                               "SELECT drop_id, received_at, rowid FROM drops ORDER BY rowid DESC LIMIT ?")))
    if "chat_messages" in tables:
        where = "FROM chat_messages WHERE author_kind = 'operator'"
        reads.append(("chat", (f"SELECT message_id, at, rowid {where} ORDER BY at DESC LIMIT ?",
                               f"SELECT message_id, at, rowid {where} ORDER BY rowid DESC LIMIT ?")))
    rows: list[tuple[datetime, int, str, str]] = []
    notes: list[str] = []
    for prefix, queries in reads:
        seen: set[str] = set()
        for q in queries:
            try:
                got = conn.execute(q, (_FETCH,)).fetchall()
            except sqlite3.Error as exc:
                notes.append(f"the {prefix} records could not be read for the gap ({type(exc).__name__}: "
                             f"{unshowable.mark(str(exc)[:120], allow='')})")
                continue
            for k, t, rid in got:
                key = f"{prefix}:{k}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    rows.append((instant(t), int(rid), key, str(t)))
                except (TypeError, ValueError, AttributeError, OverflowError):
                    notes.append(f"{unshowable.mark(key, allow='')} has no readable time; the gap skipped it")
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)       # equal instants: the later-inserted row is newer
    return [(key, t) for _w, _r, key, t in rows[:2]], notes


def _read_conn(store: Any) -> sqlite3.Connection | None:
    """The coordinator's file opened ``mode=ro`` and ``query_only`` (plain sqlite: no schema check, no table made, no
    migration), or None when there is no file."""
    path = Path(store.path)
    if not path.exists():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def silence(store: Any, *, gap_hours: float = SILENCE_GAP_HOURS) -> Silence:
    """The gap rule. The newest operator activity record fires when the record before it is at least ``gap_hours``
    older. With fewer than two records there is no gap to measure, and the answer says so rather than firing. A read
    only: the store file is opened ``mode=ro`` (no file is no record), and nothing in it is created or migrated."""
    if not isinstance(gap_hours, (int, float)) or isinstance(gap_hours, bool) or not math.isfinite(gap_hours) \
            or gap_hours < 0:
        raise CoordError("the gap is a number of hours, zero or more")
    recent: list[tuple[str, str]] = []
    notes: list[str] = []
    try:
        conn = _read_conn(store)
        if conn is not None:
            with closing(conn):
                recent, notes = _activity(conn)
    except sqlite3.Error as exc:
        why = f"{type(exc).__name__}: {unshowable.mark(str(exc)[:120], allow='')}"
        return Silence(False, None, None, f"no light open built for silence: the gap cannot be derived ({why})",
                       (f"the coordinator store could not be read for the gap ({why})",))
    kept = tuple(notes)
    if not recent:
        return Silence(False, None, None, "no light open needed: no operator activity record yet", kept)
    if len(recent) < 2:
        return Silence(False, None, None, "no light open needed: this is the only operator activity record, so there "
                                          "is no previous one to measure a gap from", kept)
    (key, newest), (_prev_key, previous) = recent
    gap = (instant(newest) - instant(previous)).total_seconds() / 3600.0
    if gap >= gap_hours:
        return Silence(True, key, gap, f"operator activity {key} came {gap:.1f} h after the one before it", kept)
    return Silence(False, None, gap, f"no light open needed: previous activity {gap:.1f} h ago "
                                     f"(under {gap_hours:g} h)", kept)
