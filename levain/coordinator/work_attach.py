"""Row 156, step 2A: the worker-attach table and the service applier (design ``DESIGN_worker_attach.md``, ruled 11-08).

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

* A report whose ``launch_id``, or whose ``harness_session_id``, resolves to a launch row of this store is that
  launch's: its ``worker`` must be the launch's recorded worker (the worker of the launch's attach, else the launch id),
  or it is refused ``worker-mismatch``. An ``attach`` also needs the launch pending or active.
* Any other report rests on its session (a flow seat). The FIRST accepted attach binds worker and session: a later report
  naming that worker from another session, or that session under another name, is refused ``worker-mismatch``. The
  session is real when this pass's live listing (:func:`live_session_ids`) shows it, or the service saw it live
  (``sessions_seen``, written by the service on every pass whose listing it could read) at a time no earlier than one
  pass interval before the report's ``at``: a seat whose last act is ``landed`` or ``ended`` and which then exits is
  still judged on what was true when it spoke. An unreadable listing defers such a report, and one deferred past
  :data:`DEFER_DEADLINE` after its ``at`` is refused ``no-evidence-timeout``.
* An ``attach`` on an item another worker holds (an open attach) is refused ``held-by-other``: only a coordinator
  reassigns.

Known edges (ruled, one line each):

* A ``landed`` sha is what the seat reported, checked only for its shape; landing truth is the land waiter's (L2 F5).
* A seat-chosen ``--report-id`` is the seat's own; one that repeats another seat's id is queued once (L2 F17).
* A worker name is bound to the session of its open attach: once that attach has ended, an attach from a new session
  rebinds the name.
* No coordinator verb applies the board's queue: while a coordinator or the operator holds the board lease the service
  skips its pass and the reports wait for the lease to end (the coordinator role of :func:`apply_reports` is there for a
  holder to call; no per-wake hook of the coordinator calls it).
* A stale lease of someone other than the service is left for its holder or a coordinator to lapse: the service's pass
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
from levain.coordinator.store import _OID, CoordError, CoordStore, _append_only, _in, next_gseq

__all__ = ["ApplyResult", "watch_apply", "DEFER_DEADLINE", "EVENTS", "PASS_INTERVAL_S", "REFUSAL_REASONS",
           "REPORT_SCHEMA", "WORKER_KINDS", "WORK_SCHEMA", "apply_reports", "attach_launch", "board_store_dir",
           "enqueue_report", "live_session_ids", "queue_report", "report_line", "report_status", "reports_path"]

EVENTS = ("attach", "progress", "blocked", "landed", "ended")
WORKER_KINDS = ("seat", "lane", "launch")
REFUSAL_REASONS = ("unknown-item", "worker-not-a-launch", "malformed-report", "no-open-attach", "already-attached",
                   "item-refused", "held-by-other", "worker-mismatch", "no-evidence-timeout")
# ruled (row 156 L2 F7): how long after its ``at`` a report resting on a session waits for a readable listing
DEFER_DEADLINE = timedelta(minutes=30)
# the watch's pass interval, in seconds, when the caller gives none: a session seen live at a pass this long before a
# report's ``at`` was live when the report was written
PASS_INTERVAL_S = 30.0
_ROLES = ("coordinator", "service")
_WHY_MAX = 1000
_FIELD_MAX = 200
_DETAIL_MAX = 500

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
    # Written only by the service's pass; a row is never deleted and its last sighting only moves forward
    "CREATE TABLE IF NOT EXISTS sessions_seen (session_id TEXT PRIMARY KEY CHECK (length(session_id) BETWEEN 1 AND "
    f"{_FIELD_MAX}), first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, CHECK (last_seen >= first_seen))",
    "CREATE TRIGGER IF NOT EXISTS sessions_seen_kept BEFORE DELETE ON sessions_seen "
    "BEGIN SELECT RAISE(ABORT, 'a seen session is never deleted'); END",
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
    from levain.consent.queue import queue_dir_for

    return queue_dir_for(os.environ if environ is None else environ).parent / "board"


def reports_path(directory: Path | str) -> Path:
    """The queue file beside a board store's ``dispatch.db``."""
    return Path(directory) / "reports.db"


def _key_ok(value: Any) -> bool:
    """A report id the refusal record can hold: one printable line of 1 to 100 characters."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 100 and value.isprintable()


def enqueue_report(directory: Path | str, *, report_id: str, item_id: str, worker: str, worker_kind: str, event: str,
                   at: str, launch_id: str | None = None, harness_session_id: str | None = None,
                   sha: str | None = None, branch: str | None = None, why: str | None = None) -> bool:
    """Append one report to ``<directory>/reports.db`` (made on first use). Never touches ``dispatch.db`` or the lease.
    False when ``report_id`` is already queued (a resend is a no-op), True when this call queued it. The directory
    must exist: this never makes a board store. An empty or malformed ``report_id`` is a :class:`CoordError` and
    queues nothing: it is the one field the queue keys on."""
    if not _key_ok(report_id):
        raise CoordError("report not queued: report_id is one printable line of at most 100 characters")
    d = Path(directory)
    if not d.is_dir():
        raise CoordError(f"no board store directory at {d}; no report was queued")
    conn = open_durable(reports_path(d))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in REPORT_SCHEMA:
                conn.execute(stmt)
            if conn.execute("SELECT 1 FROM reports WHERE report_id = ?", (report_id,)).fetchone() is not None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(f"INSERT INTO reports ({', '.join(_REPORT_COLS)}) VALUES ({', '.join('?' * len(_REPORT_COLS))})",
                         (report_id, item_id, worker, worker_kind, event, launch_id, harness_session_id, sha, branch,
                          why, at))
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_WAIT_S, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _queued(directory: Path) -> list[dict[str, Any]]:
    """Every queued report, in arrival order, with its queue ``seq``, read-only; none when there is no queue yet."""
    path = reports_path(directory)
    if not path.exists():
        return []
    conn = _ro(path)
    try:
        try:
            rows = conn.execute(f"SELECT seq, {', '.join(_REPORT_COLS)} FROM reports ORDER BY seq").fetchall()
        except sqlite3.OperationalError:          # a queue file that was made empty: nothing queued
            return []
        return [dict(zip(("seq", *_REPORT_COLS), r)) for r in rows]
    finally:
        conn.close()


def _key(raw: Mapping[str, Any]) -> str:
    """The key a queued row is applied or refused under: its own id, or, when the refusal record could not hold that id
    (empty, too long, not one printable line), ``bad-seq-<its queue seq>``, so one bad row never stops the queue."""
    rid = raw.get("report_id")
    return rid if _key_ok(rid) else f"bad-seq-{raw.get('seq')}"


def live_session_ids(store: CoordStore) -> frozenset[str] | None:
    """The harness session ids the coordinator's own live listing shows (``claude agents --json``, the list
    :func:`levain.coordinator.launch._watch_pass` reads to end launches), or None when it cannot be read. Pass the result
    as ``apply_reports(live_sessions=...)``: None defers a report that rests on a session id, never refuses it."""
    from levain.coordinator import launch

    exe, env = launch._harness()
    if exe is None:
        return None
    rows = launch._observed(exe, env, str(store.directory))
    return None if rows is None else frozenset(launch._by_session(rows))


def _record_seen(conn: sqlite3.Connection, now: str, live: Collection[str]) -> None:
    """The service's fact: each session in a listing it read was live at ``now`` (``sessions_seen``)."""
    for sid in live:
        if isinstance(sid, str) and 0 < len(sid) <= _FIELD_MAX:
            conn.execute("INSERT INTO sessions_seen (session_id, first_seen, last_seen) VALUES (?, ?, ?) "
                         "ON CONFLICT (session_id) DO UPDATE SET last_seen = excluded.last_seen "
                         "WHERE excluded.last_seen > sessions_seen.last_seen", (sid, now, now))


# ------------------------------------------------------------------------------------------------ the applier
@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)                       # report ids copied into worker_attach
    refused: list[tuple[str, str]] = field(default_factory=list)           # (report id, reason) written to report_refusals
    deferred: list[str] = field(default_factory=list)                      # left queued: no evidence yet either way
    faults: list[tuple[str, str]] = field(default_factory=list)            # (report id, why): its refusal could not be
                                                                           # recorded; left queued, the pass went on


def queue_report(directory: Path | str, *, report_id: str | None = None, **fields: Any) -> tuple[str, bool]:
    """The seat's door: check one report's fields the way the applier will, then queue it (:func:`enqueue_report`).
    Returns ``(report_id, queued)``; ``queued`` is False when ``report_id`` was already in the queue (a resend). A
    malformed field is a :class:`CoordError` and queues nothing. Whether the item exists and the worker is a real launch
    is the applier's to judge, against the store: this never opens ``dispatch.db``. No ``report_id`` mints one."""
    rid = report_id if report_id is not None else "rp-" + os.urandom(16).hex()
    try:
        checked = _check({"report_id": rid, **fields})
    except ValueError as exc:
        raise CoordError(f"report not queued: {exc}") from None
    return rid, enqueue_report(directory, **{k: v for k, v in checked.items()})


def attach_launch(conn: sqlite3.Connection, now: str, token: int, launch_id: str, item_id: str) -> str:
    """Record, in the caller's transaction, that the launch ``launch_id`` works on ``item_id``: one ``attach`` row, the
    launch as its own worker, written by the coordinator under ``token``. Returns the attach id. An item this store does
    not hold is a :class:`CoordError` (the caller's transaction then rolls back, launch and all)."""
    ver = conn.execute("SELECT MAX(ver) FROM ready_items WHERE id = ?", (item_id,)).fetchone()[0]
    if ver is None:
        raise CoordError(f"{item_id} is not a ready item in this store; nothing was asked for")
    attach_id = "wa-" + os.urandom(16).hex()
    conn.execute("INSERT INTO worker_attach (gseq, attach_id, item_id, item_ver, kind, worker, worker_kind, launch_id, "
                 "harness_session_id, report_id, why, at, applied_at, lease_token, by) "
                 "VALUES (?, ?, ?, ?, 'attach', ?, 'launch', ?, NULL, NULL, 'asked for by coord spawn', ?, ?, ?, "
                 "'coordinator')", (next_gseq(conn, now), attach_id, item_id, ver, launch_id, launch_id, now, now, token))
    return attach_id


def _text(value: Any, name: str, high: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > high or not value.isprintable():
        raise ValueError(f"{name} is one printable line of at most {high} characters")
    return value


def _when(text: str) -> datetime | None:
    """An ISO 8601 time that names its zone, or None."""
    try:
        got = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return got if got.tzinfo is not None and got.utcoffset() is not None else None


def _check(r: Mapping[str, Any]) -> dict[str, Any]:
    """The report's fields, checked mechanically, or ValueError naming the first bad one."""
    out = {"report_id": _text(r.get("report_id"), "report_id", 100, required=True),
           "item_id": _text(r.get("item_id"), "item_id", _FIELD_MAX, required=True),
           "worker": _text(r.get("worker"), "worker", _FIELD_MAX, required=True),
           "launch_id": _text(r.get("launch_id"), "launch_id", _FIELD_MAX),
           "harness_session_id": _text(r.get("harness_session_id"), "harness_session_id", _FIELD_MAX),
           "why": None if r.get("why") is None else _text(r["why"], "why", _WHY_MAX),
           "at": _text(r.get("at"), "at", 64, required=True)}
    if _when(out["at"]) is None:
        raise ValueError("at is an ISO 8601 time with its zone")
    if r.get("worker_kind") not in WORKER_KINDS:
        raise ValueError(f"worker_kind is one of {', '.join(WORKER_KINDS)}")
    if r.get("event") not in EVENTS:
        raise ValueError(f"event is one of {', '.join(EVENTS)}")
    out["worker_kind"], out["event"] = r["worker_kind"], r["event"]
    out["sha"] = out["branch"] = None
    if out["event"] == "landed":
        sha = r.get("sha")
        if not isinstance(sha, str) or _OID.fullmatch(sha) is None:
            raise ValueError("a landed report carries a full 40 or 64 hex sha")
        out["sha"] = sha
        out["branch"] = _text(r.get("branch"), "branch", _FIELD_MAX)
    return out


def _open_attach(conn: sqlite3.Connection, item_id: str, worker: str) -> str | None:
    """The attach id of this worker's latest attach on this item, when no ``ended`` row follows it."""
    row = conn.execute("SELECT attach_id FROM worker_attach WHERE item_id = ? AND worker = ? AND kind = 'attach' "
                       "ORDER BY seq DESC LIMIT 1", (item_id, worker)).fetchone()
    if row is None or conn.execute("SELECT 1 FROM worker_attach WHERE attach_id = ? AND kind = 'ended'",
                                   (row[0],)).fetchone() is not None:
        return None
    return row[0]


def _held_by_other(conn: sqlite3.Connection, item_id: str, worker: str) -> tuple[str, str] | None:
    """``(worker, attach id)`` of another worker's open attach on this item, the latest; None when there is none."""
    return conn.execute("SELECT a.worker, a.attach_id FROM worker_attach a WHERE a.item_id = ? AND a.kind = 'attach' "
                        "AND a.worker != ? AND NOT EXISTS (SELECT 1 FROM worker_attach e WHERE e.attach_id = "
                        "a.attach_id AND e.kind = 'ended') ORDER BY a.seq DESC LIMIT 1", (item_id, worker)).fetchone()


def _refuse(conn: sqlite3.Connection, now: str, by: str, report_id: str, item_id: Any, worker: Any, reason: str,
            detail: str) -> None:
    def keep(v: Any) -> str | None:
        return v[:_FIELD_MAX] if isinstance(v, str) else None
    conn.execute("INSERT INTO report_refusals (gseq, report_id, item_id, worker, reason, detail, at, by) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (next_gseq(conn, now), report_id, keep(item_id), keep(worker), reason, detail[:_DETAIL_MAX], now, by))


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _evidence(conn: sqlite3.Connection, now: str, r: Mapping[str, Any], live: Collection[str] | None,
              every: float) -> str:
    """Is the worker of checked report ``r`` real, and is it the worker it names? ``"real"`` or ``"deferred"`` (no
    evidence either way yet), or :class:`_Refused`. Reads only (the module docstring states the rules)."""
    from levain.coordinator import lease

    ln = lease.launch(conn, r["launch_id"]) if r["launch_id"] is not None else None
    sid = r["harness_session_id"]
    if sid is not None:
        mine = lease.by_harness_session(conn, sid)
        if mine is not None:
            if ln is not None and ln.launch_id != mine.launch_id:
                raise _Refused("worker-mismatch", f"session {sid} is launch {mine.launch_id}, not {ln.launch_id}")
            ln = mine
    if ln is not None:
        row = conn.execute("SELECT worker FROM worker_attach WHERE launch_id = ? AND kind = 'attach' ORDER BY seq "
                           "LIMIT 1", (ln.launch_id,)).fetchone()
        expect = ln.launch_id if row is None else row[0]
        if r["worker"] != expect:
            raise _Refused("worker-mismatch", f"launch {ln.launch_id} reports as {expect}, not as {r['worker']}")
        if r["event"] == "attach" and ln.state not in ("pending", "active"):
            raise _Refused("worker-not-a-launch", f"launch {ln.launch_id} is {ln.state}; an attach needs one that runs")
        return "real"
    if sid is None:
        raise _Refused("worker-not-a-launch", "the report names no launch of this store and no session")
    bound = conn.execute("SELECT harness_session_id FROM worker_attach WHERE worker = ? AND kind = 'attach' AND "
                         "harness_session_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM worker_attach e WHERE "
                         "e.attach_id = worker_attach.attach_id AND e.kind = 'ended') ORDER BY seq LIMIT 1",
                         (r["worker"],)).fetchone()
    if bound is not None and bound[0] != sid:
        raise _Refused("worker-mismatch", f"{r['worker']} attached from another session; its reports come from that one")
    other = conn.execute("SELECT worker FROM worker_attach WHERE harness_session_id = ? AND kind = 'attach' AND "
                         "worker != ? ORDER BY seq LIMIT 1", (sid, r["worker"])).fetchone()
    if other is not None:
        raise _Refused("worker-mismatch", f"session {sid} attached as {other[0]}, not as {r['worker']}")
    if live is not None and sid in live:
        return "real"
    at = _when(r["at"])
    seen = conn.execute("SELECT last_seen FROM sessions_seen WHERE session_id = ?", (sid,)).fetchone()
    if seen is not None and (last := _when(seen[0])) is not None and last >= at - timedelta(seconds=every):
        return "real"
    if live is None:
        if _when(now) - at > DEFER_DEADLINE:
            raise _Refused("no-evidence-timeout", f"no readable session listing showed session {sid} live within "
                                                  f"{int(DEFER_DEADLINE.total_seconds() // 60)} minutes of the report")
        return "deferred"
    raise _Refused("worker-not-a-launch", f"session {sid} was not seen live at the report's time ({r['at']})")


def _judge(conn: sqlite3.Connection, now: str, r: Mapping[str, Any], live: Collection[str] | None, every: float,
           deferred: set[str]) -> str:
    """The checks that come before any write, in order: ``"refuse"`` raises :class:`_Refused`; else ``"deferred"`` (also
    for every later report of a worker already deferred this pass, so a fact never lands before the attach it follows)
    or ``"go"``. Reads only."""
    if conn.execute("SELECT 1 FROM ready_items WHERE id = ?", (r["item_id"],)).fetchone() is None:
        raise _Refused("unknown-item", f"{r['item_id']} is not a ready item in this store")
    if r["worker"] in deferred:
        return "deferred"
    if _evidence(conn, now, r, live, every) == "deferred":
        deferred.add(r["worker"])
        return "deferred"
    return "go"


def _apply_one(conn: sqlite3.Connection, now: str, token: int, by: str, r: dict[str, Any],
               live: Collection[str] | None, *, every: float = PASS_INTERVAL_S,
               deferred: set[str] | None = None) -> str:
    """Apply one checked report inside the caller's savepoint. Returns 'applied' or 'deferred'; a refusal raises
    :class:`_Refused`."""
    from levain.coordinator import ready

    if _judge(conn, now, r, live, every, set() if deferred is None else deferred) == "deferred":
        return "deferred"
    item = conn.execute("SELECT MAX(ver) FROM ready_items WHERE id = ?", (r["item_id"],)).fetchone()[0]
    open_id = _open_attach(conn, r["item_id"], r["worker"])
    if r["event"] == "attach":
        if open_id is not None:
            raise _Refused("already-attached", f"{r['worker']} is already attached to {r['item_id']} ({open_id})")
        holder = _held_by_other(conn, r["item_id"], r["worker"])
        if holder is not None:
            raise _Refused("held-by-other", f"{r['item_id']} is held by {holder[0]} ({holder[1]}); only a coordinator "
                                            "reassigns an item")
        attach_id = "wa-" + os.urandom(16).hex()
    else:
        if open_id is None:
            raise _Refused("no-open-attach", f"{r['worker']} has no open attach on {r['item_id']}")
        attach_id = open_id
    if r["event"] == "landed":
        try:
            item = ready.record_landed(conn, now, token, r["item_id"], r["sha"], r["branch"], by=by,
                                       why=f"landed {r['sha'][:12]} on {r['branch'] or 'no branch named'} "
                                           f"(reported by {r['worker']})")
        except CoordError as exc:
            raise _Refused("item-refused", str(exc)) from exc
    conn.execute("INSERT INTO worker_attach (gseq, attach_id, item_id, item_ver, kind, worker, worker_kind, launch_id, "
                 "harness_session_id, report_id, why, at, applied_at, lease_token, by) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (next_gseq(conn, now), attach_id, r["item_id"], item, r["event"], r["worker"], r["worker_kind"],
                  r["launch_id"], r["harness_session_id"], r["report_id"], r["why"], r["at"], now, token, by))
    return "applied"


def _done_ids(conn: sqlite3.Connection) -> set[str]:
    seen = {r[0] for r in conn.execute("SELECT report_id FROM worker_attach WHERE report_id IS NOT NULL")}
    return seen | {r[0] for r in conn.execute("SELECT report_id FROM report_refusals")}


def apply_reports(store: CoordStore, token: Any, *, live_sessions: Collection[str] | None,
                  every: float = PASS_INTERVAL_S) -> ApplyResult:
    """Copy every unapplied report in ``<store>/reports.db`` into ``worker_attach``, or refuse it with a
    ``report_refusals`` row, in ONE write transaction. Idempotent on ``report_id``: a report already applied or refused
    is skipped, so a re-run writes nothing twice. Facts only, by the store's role: ``service`` writes as the service
    (``token`` must be the live claim the service itself holds, :func:`levain.coordinator.lease.service_claim`; a
    coordinator's claim taking it over makes this refuse, and nothing is applied), ``coordinator`` as the lease holder
    (fenced). A landed report also sets the item's ``landed_sha`` and ``branch``
    (:func:`levain.coordinator.ready.record_landed`) and nothing else about it.

    ``live_sessions``: the harness session ids the coordinator's own listing shows (:func:`live_session_ids`); the
    service records them as seen now. None means the listing could not be read. ``every``: the watch's pass interval in
    seconds (the module docstring says how both judge a report).

    A queued row whose id the refusal record cannot hold is judged under ``bad-seq-<seq>``; a row whose refusal still
    cannot be recorded is left queued and named in ``faults``, and the pass goes on."""
    if not isinstance(token, int) or isinstance(token, bool):
        raise CoordError("applying reports needs the live lease token, an integer")
    queued = _queued(store.directory)
    result = ApplyResult()
    if not queued:
        return result
    service = store.role == "service"
    by = "service" if service else "coordinator"
    with store._write(by, None if service else token, create=False) as (conn, now):
        if service:
            from levain.coordinator.lease import SERVICE_HOLDER, lease

            held = lease(conn, now)
            if held is None or not held.live or held.holder != SERVICE_HOLDER or held.token != token:
                raise CoordError(f"lease token {token} is not the live lease's service claim; no report was applied")
            if live_sessions is not None:
                _record_seen(conn, now, live_sessions)
        seen = _done_ids(conn)
        deferred: set[str] = set()
        for raw in queued:
            rid = _key(raw)
            if rid in seen:
                continue
            seen.add(rid)
            conn.execute("SAVEPOINT report")
            try:
                try:
                    checked = _check(raw)
                except ValueError as exc:
                    raise _Refused("malformed-report", str(exc)) from exc
                got = _apply_one(conn, now, token, by, checked, live_sessions, every=every, deferred=deferred)
                conn.execute("RELEASE report")
                (result.applied if got == "applied" else result.deferred).append(rid)
            except (_Refused, sqlite3.IntegrityError) as exc:
                conn.execute("ROLLBACK TO report")
                reason, detail = (exc.reason, exc.detail) if isinstance(exc, _Refused) else ("item-refused", str(exc))
                try:
                    _refuse(conn, now, by, rid, raw.get("item_id"), raw.get("worker"), reason, detail)
                except sqlite3.Error as err:
                    conn.execute("ROLLBACK TO report")
                    result.faults.append((rid, f"its refusal ({reason}) could not be recorded: {err}"))
                else:
                    result.refused.append((rid, reason))
                conn.execute("RELEASE report")
    return result


# ------------------------------------------------------------------------------------------------ the watch's pass
def _waiting(store: CoordStore, live: Collection[str] | None, every: float) -> tuple[int, int]:
    """``(waiting, applicable)``: how many queued reports have neither a ``worker_attach`` row nor a refusal yet, and
    how many of them a pass would apply or refuse now rather than defer. Nothing written."""
    from levain.autonomic.durable_sqlite import read_transaction

    queued = _queued(store.directory)
    if not queued:
        return 0, 0
    with read_transaction(lambda: store._connect(create=False)) as conn:
        done = _done_ids(conn)
        now = store._stamp()
        waiting = applicable = 0
        deferred: set[str] = set()
        for raw in queued:
            rid = _key(raw)
            if rid in done:
                continue
            done.add(rid)
            waiting += 1
            try:
                if _judge(conn, now, _check(raw), live, every, deferred) == "go":
                    applicable += 1
            except (ValueError, _Refused):
                applicable += 1                     # a refusal to record is work
    return waiting, applicable


def watch_apply(board: Path | str | None = None, *, sessions: Any = None, every: float = PASS_INTERVAL_S) -> list[str]:
    """The ``levain coord watch`` pass over the board store: apply the queued reports as the ``service``, facts only.

    Nothing happens, and no line is returned, when the board has no ``dispatch.db`` or no queue. Otherwise, in order:
    the live session listing is read once (``sessions(store)``, default :func:`live_session_ids`) and, when it could be
    read, recorded as seen (``sessions_seen``; no lease is needed for it). With no report waiting the pass ends there.
    With reports waiting but every one deferred (the listing could not be read, and each rests on a session alone), it
    says so in one line and takes no lease. Else it claims the lease as holder ``service`` for
    :data:`levain.coordinator.lease.SERVICE_TTL`, applies, and releases it in a ``finally``; any other claim standing,
    live or lapsed, is never taken: the pass applies nothing and says why in one line. A coordinator that claims
    meanwhile takes the service's claim, and the apply then refuses as a whole. The service role refuses every write that
    decides (:meth:`CoordStore._write`), so the only things this pass can write are ``worker_attach``,
    ``report_refusals``, ``sessions_seen``, the item's landed sha and branch, and its own lease rows. Every fault is
    caught here and returned as one line: this never raises."""
    from levain.coordinator import lease as _lease

    directory = Path(board) if board is not None else board_store_dir()
    if not (directory / "dispatch.db").exists() or not reports_path(directory).exists():
        return []
    try:
        store = CoordStore(directory, role="service")
        live = (sessions or live_session_ids)(store)
        if live is not None:
            with store._write("service", None, create=False) as (conn, now):
                _record_seen(conn, now, live)
        waiting, applicable = _waiting(store, live, every)
        if not waiting:
            return []
        plural = "report" if waiting == 1 else "reports"
        if not applicable:
            return [f"{waiting} {plural} waiting: deferred, the live session listing could not be read and each rests "
                    "on a session; no lease taken"]
        with store._write("service", None, create=False) as (conn, now):
            token, held = _lease.service_claim(conn, now, cause="watch apply")
            if held is not None:
                how = (f"is held by {held.holder} until {held.until}" if held.live else
                       f"of {held.holder} lapsed at {held.until}, and recording that is the coordinator's")
                return [f"{waiting} {plural} waiting: the board's lease {how}; applied nothing this pass"]
        try:
            got = apply_reports(store, token, live_sessions=live, every=every)
        finally:
            with store._write("service", None, create=False) as (conn, now):
                _lease.service_release(conn, now, token, cause="watch apply done")
        return [f"board reports: applied {len(got.applied)}, refused {len(got.refused)}, deferred {len(got.deferred)}"
                + "".join(f"; {rid} refused: {why}" for rid, why in got.refused)
                + "".join(f"; {rid} not recorded: {why}" for rid, why in got.faults)]
    except Exception as exc:    # noqa: BLE001 - the watch's pass goes on whatever this does
        return [f"board reports: not applied this pass ({exc})"]


# ------------------------------------------------------------------------------------------------ a report's outcome
def report_status(directory: Path | str, report_id: str) -> dict[str, Any]:
    """What became of one report, read-only (``levain coord reports ID``): ``state`` is ``applied`` (with its
    ``kind`` and ``attach_id``), ``refused`` (with ``reason`` and ``detail``), ``queued`` (waiting for a pass), or
    ``unknown`` (not in this board's queue). Opens nothing for writing and makes nothing."""
    d = Path(directory)
    out: dict[str, Any] = {"report_id": report_id, "state": "unknown", "item_id": None, "reason": None,
                           "detail": None, "kind": None, "attach_id": None}
    row = next((r for r in _queued(d) if r["report_id"] == report_id), None)
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


def report_line(directory: Path | str, report_id: str, queued: bool) -> str:
    """The line ``levain coord report`` prints: the report id and what happens next, or, for a resend, what already
    happened to it (:func:`report_status`)."""
    nxt = "the next `levain coord watch` pass applies or refuses it"
    if queued:
        return f"report {report_id} queued; {nxt}"
    st = report_status(directory, report_id)
    if st["state"] == "refused":
        return f"report {report_id} was refused ({st['reason']}: {st['detail']}); nothing was added"
    if st["state"] == "applied":
        return f"report {report_id} was already applied ({st['kind']}); nothing was added"
    return f"report {report_id} is already queued; nothing was added, and {nxt}"
