"""RETIRED as a writer (T7, levain#101): the v1 queue takes no more rows. ``apply_reports``, ``watch_apply`` and ``desk_apply``
write no ``worker_attach`` row, no refusal and no sighting; a report still queued at the switch is drained once, read-only,
into one log line (:func:`drain_reports`). The tables and their append triggers stay, so what they hold stays readable. What
follows is the design as it was.

Row 156, step 2A: the worker-attach table and the service applier (design ``DESIGN_worker_attach.md``, ruled 11-08).

Who works on which board item is a FACT, not a decision, and it is recorded on its own store file (``<levain
home>/board/``, never the consent directory). This module holds:

* ``worker_attach``: one append-only table, one row per fact about one worker on one item (``attach``, ``progress``,
  ``blocked``, ``landed``, ``ended``). No row stores a status: the current worker, the silence since its last event and
  the stuck state are worked out by the reader.
* ``report_refusals``: one append-only row per report the applier could not validate mechanically, so the desk and the
  Work lens can see it (the ruling's condition 2).
* ``reports.db``: the seat-writable queue beside the board store, append-only, one row per report with a unique
  ``report_id``. A seat appends to it (:func:`enqueue_report`) and never touches ``dispatch.db`` or the lease; the
  applier copies each report into ``worker_attach`` or refuses it with a record, once.
* :func:`apply_reports`, the applier. It records facts only (the ruling's condition 1): the ``service`` role
  (``CoordStore(..., role="service")``) is refused every write that DECIDES, in :meth:`CoordStore._write` itself, and the
  triggers below keep a ``service`` row of ``ready_items`` / ``ready_journal`` from changing anything but a landed sha
  and branch.

Who the worker is, and whether it is real (row 156 L1+L2 round 1, the head's rulings), judged on evidence the store
holds, never on the worker name a report carries:

* A report whose ``at`` is more than :data:`FUTURE_SKEW` ahead of the board's clock is refused ``malformed-report``.
* A report whose ``launch_id``, or whose ``harness_session_id``, resolves to a launch row of this store is that
  launch's: its ``worker`` must be the launch's recorded worker (the worker of the launch's attach, else the launch id),
  and, once the launch's ``active`` row records its session, the report must come from that session; else it is
  refused ``worker-mismatch``. It is recorded as that launch, kind ``launch``, whatever kind it names. An ``attach``
  also needs the launch pending or active; every other event needs it to have recorded its session, in any state (L3 R3-B).
* Any other report rests on its session (a flow seat). The FIRST accepted attach binds worker and session while that
  attach holds its item: a later report naming that worker from another session, or that session under another name,
  is refused ``worker-mismatch``. Its later events keep the kind its attach was recorded with (else ``kind-mismatch``),
  and it never names the kind ``launch``. The session is real when its observed interval (``sessions_seen``, written
  by every service pass and every desk pass on a board with a queue file, whose listing it could read, from its
  first sighting to its last, this pass's listing (:func:`live_session_ids`) counting as a sighting now) reaches the report's ``at``: its last sighting within :data:`GRACE_PASSES` pass
  intervals after it, its first no more than :data:`DEFER_DEADLINE` later: a seat whose last act is ``landed`` or ``ended`` and which then exits is still judged on what
  was true when it spoke. An unreadable listing defers such a report, and one deferred past :data:`DEFER_DEADLINE`
  after its ``at`` is refused ``no-evidence-timeout``.
* An ``attach`` on an item another worker holds is refused ``held-by-other``: only a desk reassigns. Holding is
  derived when it is read: an open attach holds while its launch is pending (within its ``until``) or active, or,
  resting on a session, while that session was seen within :data:`DEFER_DEADLINE`. A dead launch or a long-unseen
  session holds nothing, and nothing is written to say so.
* A seen session last seen more than :data:`SEEN_KEEP` before the pass, and before the newest sighting the table
  holds, is pruned from ``sessions_seen`` in the pass's own write, unless a report still waiting names it.

Known edges (ruled, one line each):

* A ``landed`` sha is what the seat reported, checked only for its shape; landing truth is the land waiter's (L2 F5).
* A seat-chosen ``--report-id`` is the seat's own; one that repeats another seat's id is queued once (L2 F17).
* A worker name is bound to the session of its open attach: once that attach has ended, or no longer holds its item,
  an attach from a new session rebinds the name.
* A report written more than :data:`DEFER_DEADLINE` before the service first saw its session is refused
  ``worker-not-a-launch``; within it, a seat that attached while no watch ran is accepted.
* A session attach that stopped holding (its session long unseen) and whose session is seen again holds again; its
  worker's reports still apply to the attach it opened.
* While a desk or the operator holds the board lease the service skips its pass; the holder applies the queue
  itself with :func:`desk_apply` (``levain desk apply-reports --token N``, and at the end of ``coord
  import-status``), as the desk, facts only, through the same judging. A holder that runs neither leaves the
  reports waiting for its lease to end.
* The desk's own sightings are only as frequent as its passes (``apply-reports``, ``import-status``); the
  watch service records them continuously.
* A stale lease of someone other than the service is left for its holder or a desk to lapse: the service's pass
  is skipped until then.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import BUSY_WAIT_S, open_durable
from levain.cockpit.consent import utc_stamp
from levain.desk.store import _OID, CoordError, CoordStore, _append_only, _in, next_gseq
from levain.desk.legacy import ROLE_DESK, SQL_ROLE_DESK

__all__ = ["ApplyResult", "watch_apply", "DEFER_DEADLINE", "DRAINED_NAME", "EVENTS", "FUTURE_SKEW", "GRACE_PASSES",
           "PASS_INTERVAL_S", "REFUSAL_REASONS", "SEEN_KEEP", "desk_apply", "drain_reports", "migrate_seen",
           "REPORT_SCHEMA", "WORKER_KINDS", "WORK_SCHEMA", "apply_reports", "board_store_dir",
           "enqueue_report", "report_status", "reports_path"]

EVENTS = ("attach", "progress", "blocked", "landed", "ended")
WORKER_KINDS = ("seat", "lane", "launch")
REFUSAL_REASONS = ("unknown-item", "worker-not-a-launch", "malformed-report", "no-open-attach", "already-attached",
                   "item-refused", "held-by-other", "worker-mismatch", "no-evidence-timeout", "kind-mismatch")
# ruled (row 156 L2 F7): how long after its ``at`` a report resting on a session waits for a readable listing
DEFER_DEADLINE = timedelta(minutes=30)
# the watch's pass interval, in seconds, when the caller gives none: a session seen live at a pass this long before a
# report's ``at`` was live when the report was written
PASS_INTERVAL_S = 30.0
# ruled (row 156 L3 C1): a session sighting within this many pass intervals of a report's ``at`` covers it, since the
# gap between two sightings is a pass interval plus the pass's own runtime
GRACE_PASSES = 2
# ruled (L3 X3): how far ahead of the board's clock a report's ``at`` may be before it is refused
FUTURE_SKEW = timedelta(minutes=5)
# ruled (L3 C4): a seen session unseen for longer than this is pruned from ``sessions_seen``
SEEN_KEEP = 2 * DEFER_DEADLINE
_ROLES = (ROLE_DESK, "service")
_WHY_MAX = 1000
_FIELD_MAX = 200
_DETAIL_MAX = 500
# the julian day before which a sighting is long unseen: SEEN_KEEP before the newest sighting in ``sessions_seen``
_PRUNE_LINE = f"julianday((SELECT MAX(last_seen) FROM sessions_seen)) - {SEEN_KEEP.total_seconds() / 86400!r}"

# ------------------------------------------------------------------------------------------------ the board tables
WORK_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS worker_attach (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
    "attach_id TEXT NOT NULL, item_id TEXT NOT NULL, item_ver INTEGER NOT NULL CHECK (item_ver >= 1), "
    f"kind TEXT NOT NULL CHECK ({_in('kind', EVENTS)}), worker TEXT NOT NULL CHECK (length(trim(worker)) > 0), "
    f"worker_kind TEXT NOT NULL CHECK ({_in('worker_kind', WORKER_KINDS)}), launch_id TEXT, harness_session_id TEXT, "
    f"report_id TEXT UNIQUE, why TEXT CHECK (why IS NULL OR length(why) <= {_WHY_MAX}), at TEXT NOT NULL, "
    "applied_at TEXT NOT NULL, lease_token INTEGER NOT NULL, "
    f"by TEXT NOT NULL CHECK ({_in('by', _ROLES)}))",
    *_append_only("worker_attach", "a worker-attach row"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS worker_attach_no_replace BEFORE INSERT ON worker_attach WHEN EXISTS (SELECT 1 FROM "
    "worker_attach WHERE seq = NEW.seq OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a worker-attach row is never replaced'); END",
    # an attach names an item that exists and a fresh attach id; every other row follows its own attach (same item,
    # same worker) and nothing follows ``ended``
    "CREATE TRIGGER IF NOT EXISTS worker_attach_order BEFORE INSERT ON worker_attach "
    "WHEN (NEW.kind = 'attach' AND (NOT EXISTS (SELECT 1 FROM ready_items WHERE id = NEW.item_id) "
    "OR EXISTS (SELECT 1 FROM worker_attach WHERE attach_id = NEW.attach_id))) "
    "OR (NEW.kind != 'attach' AND (NOT EXISTS (SELECT 1 FROM worker_attach WHERE attach_id = NEW.attach_id "
    "AND kind = 'attach' AND item_id = NEW.item_id AND worker = NEW.worker) "
    "OR EXISTS (SELECT 1 FROM worker_attach WHERE attach_id = NEW.attach_id AND kind = 'ended'))) "
    "BEGIN SELECT RAISE(ABORT, 'a worker-attach row follows its attach, and nothing follows ended'); END",
    "CREATE TRIGGER IF NOT EXISTS worker_attach_not_refused BEFORE INSERT ON worker_attach WHEN NEW.report_id IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM report_refusals WHERE report_id = NEW.report_id) "
    "BEGIN SELECT RAISE(ABORT, 'a refused report is never applied'); END",
    "CREATE TABLE IF NOT EXISTS report_refusals (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
    "report_id TEXT NOT NULL UNIQUE CHECK (length(report_id) BETWEEN 1 AND 100), item_id TEXT, worker TEXT, "
    f"reason TEXT NOT NULL CHECK ({_in('reason', REFUSAL_REASONS)}), "
    f"detail TEXT NOT NULL CHECK (length(detail) <= {_DETAIL_MAX}), at TEXT NOT NULL, "
    f"by TEXT NOT NULL CHECK ({_in('by', _ROLES)}))",
    *_append_only("report_refusals", "a report refusal"),
    "CREATE TRIGGER IF NOT EXISTS report_refusals_no_replace BEFORE INSERT ON report_refusals WHEN EXISTS (SELECT 1 FROM "
    "report_refusals WHERE seq = NEW.seq OR gseq = NEW.gseq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a report refusal is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS report_refusals_not_applied BEFORE INSERT ON report_refusals WHEN EXISTS (SELECT 1 "
    "FROM worker_attach WHERE report_id = NEW.report_id) "
    "BEGIN SELECT RAISE(ABORT, 'an applied report is never refused'); END",
    # the sessions the service's listing showed, one row per session: when it was first and last seen live (L2 F6).
    # Written by every pass that applies (the service's, or the desk's under its lease); its last sighting only moves forward, and a row is deleted only once it was
    # last seen more than SEEN_KEEP before the newest sighting the table holds (L3 C4: the pass prunes it)
    "CREATE TABLE IF NOT EXISTS sessions_seen (session_id TEXT PRIMARY KEY CHECK (length(session_id) BETWEEN 1 AND "
    f"{_FIELD_MAX}), first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, CHECK (last_seen >= first_seen))",
    "CREATE TRIGGER IF NOT EXISTS sessions_seen_kept_recent BEFORE DELETE ON sessions_seen "
    f"WHEN julianday(OLD.last_seen) > {_PRUNE_LINE} "
    "BEGIN SELECT RAISE(ABORT, 'a seen session is deleted only once it is long unseen'); END",
    "CREATE TRIGGER IF NOT EXISTS sessions_seen_forward BEFORE UPDATE ON sessions_seen WHEN NEW.session_id IS NOT "
    "OLD.session_id OR NEW.first_seen IS NOT OLD.first_seen OR NEW.last_seen < OLD.last_seen "
    "BEGIN SELECT RAISE(ABORT, 'a seen session only moves its last sighting forward'); END",
)

# ------------------------------------------------------------------------------------------------ the report queue
# reports.db: seat-writable, append-only. Nothing in it is checked beyond its key: a seat must never block on
# bookkeeping, and a malformed report is refused visibly by the applier, not lost here.
REPORT_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS reports (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "report_id TEXT NOT NULL UNIQUE, item_id TEXT NOT NULL, worker TEXT NOT NULL, worker_kind TEXT NOT NULL, "
    "event TEXT NOT NULL, launch_id TEXT, harness_session_id TEXT, sha TEXT, branch TEXT, why TEXT, at TEXT NOT NULL)",
    *_append_only("reports", "a report"),
    "CREATE TRIGGER IF NOT EXISTS reports_no_replace BEFORE INSERT ON reports WHEN EXISTS (SELECT 1 FROM reports "
    "WHERE report_id = NEW.report_id OR seq = NEW.seq OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a report is never replaced'); END",
)
_REPORT_COLS = ("report_id", "item_id", "worker", "worker_kind", "event", "launch_id", "harness_session_id", "sha",
                "branch", "why", "at")


def board_store_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``<levain home>/board``: the board store's directory, derived from the Levain home the way the consent directory
    is (``$LEVAIN_HOME`` if set, else ``~/.levain``), and never the consent directory itself."""
    return live_consent_dir(environ).parent / "board"


def live_consent_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``<levain home>/consent``, the live consent store's directory, as :func:`board_store_dir` derives the home."""
    from levain.consent.queue import queue_dir_for

    return queue_dir_for(os.environ if environ is None else environ)


def is_live_consent(directory: Path | str, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``directory`` resolves to the live consent store's directory: never a board, whatever it holds."""
    return os.path.realpath(directory) == os.path.realpath(live_consent_dir(environ))


def watch_board(watched: Path | str, environ: Mapping[str, str] | None = None) -> Path:
    """The board a ``coord watch`` over the store in ``watched`` applies reports in (row 156 L3 R3-A), named positively:
    the home's board (:func:`board_store_dir`) when ``watched`` is the live consent store (the default watch, or its
    ``--queue-dir`` spelled out), else ``watched`` itself, the directory the watch was started with. Nothing about the
    directory's contents decides it, so a queue file planted in the consent store never makes it a board."""
    if is_live_consent(watched, environ):
        return board_store_dir(environ)
    return Path(os.path.realpath(watched))


def reports_path(directory: Path | str) -> Path:
    """The queue file beside a board store's ``dispatch.db``."""
    return Path(directory) / "reports.db"


def _key_ok(value: Any) -> bool:
    """A report id the refusal record can hold: one printable line of 1 to 100 characters."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 100 and value.isprintable()


def enqueue_report(directory: Path | str, *, report_id: str, item_id: str, worker: str, worker_kind: str, event: str,
                   at: str, launch_id: str | None = None, harness_session_id: str | None = None,
                   sha: str | None = None, branch: str | None = None, why: str | None = None) -> bool:
    """RETIRED (T7): the report queue takes no more rows, because nothing would apply them. Raises
    :class:`levain.desk.ready.V1Retired`; nothing is queued."""
    from levain.desk.ready import RETIRED, V1Retired

    raise V1Retired(f"report not queued: {RETIRED}")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_WAIT_S, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _unresolved(directory: Path | str) -> list[dict[str, Any]]:
    """The queued reports neither applied nor refused yet, in arrival order, with their queue ``seq``, read-only; none
    when there is no queue yet (L3 X7: a pass never reads the history it already wrote). The board's ``dispatch.db`` is
    attached read-only and each row is matched on the unique keys of ``worker_attach`` and ``report_refusals``, under
    the key the applier records it by (:func:`_key`). A board without those tables resolves nothing."""
    d = Path(directory)
    path = reports_path(d)
    if not path.exists():
        return []
    conn = _ro(path)
    cols = ", ".join(f"r.{c}" for c in _REPORT_COLS)
    try:
        sql = f"SELECT r.seq, {cols} FROM reports r"
        db = d / "dispatch.db"
        if db.exists():
            conn.execute("ATTACH DATABASE ? AS board", (db.resolve().as_uri() + "?mode=ro",))
            tables = {r[0] for r in conn.execute("SELECT name FROM board.sqlite_master WHERE type = 'table' AND name IN "
                                                 "('worker_attach', 'report_refusals')")}
            if len(tables) == 2:
                sql += (" WHERE NOT EXISTS (SELECT 1 FROM board.worker_attach w WHERE w.report_id = r.report_id) "
                        "AND NOT EXISTS (SELECT 1 FROM board.report_refusals f WHERE f.report_id = r.report_id) "
                        "AND NOT EXISTS (SELECT 1 FROM board.report_refusals f WHERE f.report_id = 'bad-seq-' || r.seq)")
        try:
            rows = conn.execute(sql + " ORDER BY r.seq").fetchall()
        except sqlite3.OperationalError:          # a queue file that was made empty: nothing queued
            return []
        return [dict(zip(("seq", *_REPORT_COLS), r)) for r in rows]
    finally:
        conn.close()


def _queued_one(directory: Path, report_id: str) -> dict[str, Any] | None:
    """The queued report ``report_id``, looked up by its key, read-only; None when it is not in the queue."""
    path = reports_path(directory)
    if not path.exists():
        return None
    conn = _ro(path)
    try:
        try:
            row = conn.execute(f"SELECT seq, {', '.join(_REPORT_COLS)} FROM reports WHERE report_id = ?",
                               (report_id,)).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else dict(zip(("seq", *_REPORT_COLS), row))
    finally:
        conn.close()


def _resolved(conn: sqlite3.Connection, key: str) -> bool:
    """Whether the report keyed ``key`` already has its ``worker_attach`` row or its refusal."""
    return conn.execute("SELECT 1 FROM worker_attach WHERE report_id = ? UNION ALL SELECT 1 FROM report_refusals "
                        "WHERE report_id = ? LIMIT 1", (key, key)).fetchone() is not None


def _key(raw: Mapping[str, Any]) -> str:
    """The key a queued row is applied or refused under: its own id, or, when the refusal record could not hold that id
    (empty, too long, not one printable line), ``bad-seq-<its queue seq>``, so one bad row never stops the queue."""
    rid = raw.get("report_id")
    return rid if _key_ok(rid) else f"bad-seq-{raw.get('seq')}"


def migrate_seen(conn: sqlite3.Connection) -> None:
    """The ensure path's step for ``sessions_seen``: the trigger that refused every delete gives way to the one that
    refuses deleting a recent sighting (L3 C4). Idempotent."""
    conn.execute("DROP TRIGGER IF EXISTS sessions_seen_kept")


# ------------------------------------------------------------------------------------------------ the drain (T7)
# The v1 queue takes no more rows (levain#101), so no pass applies a report: it writes no ``worker_attach`` row, no
# refusal, no landed sha, no sighting. A report still queued when the switch came is DRAINED once: read, named in one log
# line, and its key recorded in ``reports.drained`` beside the queue, so the next pass does not name it again. The
# queue and the v1 tables are never written.
DRAINED_NAME = "reports.drained"


@dataclass
class ApplyResult:
    drained: list[str] = field(default_factory=list)       # report ids named in the log line this pass, never applied


def drained_path(directory: Path | str) -> Path:
    """The record of the reports already drained, one key a line, beside the queue file."""
    return Path(directory) / DRAINED_NAME


def _drained_keys(directory: Path | str) -> set[str]:
    try:
        return set(drained_path(directory).read_text(encoding="utf-8").split("\n"))
    except FileNotFoundError:
        return set()


def drain_reports(directory: Path | str) -> ApplyResult:
    """Read the reports still queued in ``<directory>/reports.db`` that no earlier drain named, and record their keys as
    drained. Read-only toward the queue and the v1 tables; the one write is the key record. A queue that cannot be read
    drains nothing. Never applies a report."""
    d = Path(directory)
    result = ApplyResult()
    queued = [r for r in _unresolved(d) if _key(r) not in _drained_keys(d)]
    if not queued:
        return result
    keys = list(dict.fromkeys(_key(r) for r in queued))
    with open(drained_path(d), "a", encoding="utf-8") as fh:
        fh.write("".join(k + "\n" for k in keys))
        fh.flush()
        os.fsync(fh.fileno())
    result.drained = keys
    return result


def apply_reports(store: CoordStore, token: Any, *, live_sessions: Collection[str] | None = None,
                  every: float = PASS_INTERVAL_S) -> ApplyResult:
    """RETIRED as an applier (T7): drains the queue once, read-only (:func:`drain_reports`), and applies nothing. It still
    refuses a token that is not an integer, as it did, and it records no sighting (``live_sessions`` and ``every`` are
    kept so a caller's call still binds, and are not read)."""
    if not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("applying reports needs the live lease token, an integer")
    return drain_reports(store.directory)


def _drained_line(got: ApplyResult) -> str:
    return (f"board reports: {len(got.drained)} report(s) still queued at the switch drained, never applied (the v1 "
            "queue is retired; the board is `levain board`): " + ", ".join(got.drained))


def watch_apply(board: Path | str | None = None, *, sessions: Any = None, every: float = PASS_INTERVAL_S) -> list[str]:
    """The ``levain desk watch`` pass over the board store: it applies nothing (T7). A report still queued at the switch
    is drained once into one log line; every later pass says nothing. No lease is taken and no store is opened for
    writing. Never raises. ``sessions`` and ``every`` are kept so the watch's call still binds."""
    directory = Path(board) if board is not None else board_store_dir()
    if is_live_consent(directory):
        return [f"board reports: not drained, {directory} is the live consent store and never a board"]
    if not reports_path(directory).exists():
        return []
    try:
        got = drain_reports(directory)
        return [_drained_line(got)] if got.drained else []
    except Exception as exc:    # noqa: BLE001 - the watch's pass goes on whatever this does
        return [f"board reports: not drained this pass ({exc})"]


def desk_apply(store: CoordStore, token: Any, *, sessions: Any = None,
               every: float = PASS_INTERVAL_S) -> list[str]:
    """The desk's own pass over the board's queue: the same drain as the watch's (:func:`watch_apply`), after the same
    check that ``token`` is the live lease (a wrong one is refused whether or not a report waits). Returns ``[]`` when
    nothing is left to drain, else one line. A directory that is the live consent store is never a board."""
    directory = Path(store.directory)
    if is_live_consent(directory):
        return [f"board reports: not drained, {directory} is the live consent store and never a board"]
    if store.role != ROLE_DESK:
        raise CoordError("the desk's pass runs on a desk-role store")
    held = store.coord_lease() if (directory / "dispatch.db").exists() else None
    if held is None or not held.live or held.token != token:
        raise CoordError("the desk lease is not held by this token; no report was drained")
    if not reports_path(directory).exists():
        return []
    got = drain_reports(directory)
    return [_drained_line(got)] if got.drained else []


# ------------------------------------------------------------------------------------------------ a report's outcome
def report_status(directory: Path | str, report_id: str) -> dict[str, Any]:
    """What became of one report, read-only (``levain desk reports ID``): ``state`` is ``applied`` (with its
    ``kind`` and ``attach_id``), ``refused`` (with ``reason`` and ``detail``), ``queued`` (waiting for a pass), or
    ``unknown`` (not in this board's queue). Opens nothing for writing and makes nothing."""
    d = Path(directory)
    out: dict[str, Any] = {"report_id": report_id, "state": "unknown", "item_id": None, "reason": None,
                           "detail": None, "kind": None, "attach_id": None}
    row = _queued_one(d, report_id)
    if row is not None:
        out.update(state="queued", item_id=row["item_id"])
    db = d / "dispatch.db"
    if not db.exists():
        return out
    conn = _ro(db)
    try:
        try:
            got = conn.execute("SELECT item_id, kind, attach_id FROM worker_attach WHERE report_id = ?",
                               (report_id,)).fetchone()
            no = conn.execute("SELECT item_id, reason, detail FROM report_refusals WHERE report_id = ?",
                              (report_id,)).fetchone()
        except sqlite3.OperationalError:          # a store made before the board tables: nothing applied there
            return out
    finally:
        conn.close()
    if got is not None:
        out.update(state="applied", item_id=got[0], kind=got[1], attach_id=got[2])
    elif no is not None:
        out.update(state="refused", item_id=no[0], reason=no[1], detail=no[2])
    return out
