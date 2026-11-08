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

A worker is real when its report names a ``launch_id`` present in the store's ``launches`` table, or a
``harness_session_id`` in the coordinator's own live session listing (:func:`live_session_ids`, the listing the watcher
uses to end launches). Both are deterministic; neither needs a model.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import BUSY_WAIT_S, open_durable
from levain.coordinator.store import _OID, CoordError, CoordStore, _append_only, _in, next_gseq

__all__ = ["ApplyResult", "watch_apply", "EVENTS", "REFUSAL_REASONS", "REPORT_SCHEMA", "WORKER_KINDS", "WORK_SCHEMA", "apply_reports",
           "attach_launch", "board_store_dir", "enqueue_report", "live_session_ids", "queue_report",
           "reports_path"]

EVENTS = ("attach", "progress", "blocked", "landed", "ended")
WORKER_KINDS = ("seat", "lane", "launch")
REFUSAL_REASONS = ("unknown-item", "worker-not-a-launch", "malformed-report", "no-open-attach", "already-attached",
                   "item-refused")
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


def enqueue_report(directory: Path | str, *, report_id: str, item_id: str, worker: str, worker_kind: str, event: str,
                   at: str, launch_id: str | None = None, harness_session_id: str | None = None,
                   sha: str | None = None, branch: str | None = None, why: str | None = None) -> bool:
    """Append one report to ``<directory>/reports.db`` (made on first use). Never touches ``dispatch.db`` or the lease.
    False when ``report_id`` is already queued (a resend is a no-op), True when this call queued it. The directory
    must exist: this never makes a board store."""
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


def _queued(directory: Path) -> list[dict[str, Any]]:
    """Every queued report, in arrival order, read-only; none when there is no queue yet."""
    path = reports_path(directory)
    if not path.exists():
        return []
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_WAIT_S, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
        try:
            rows = conn.execute(f"SELECT {', '.join(_REPORT_COLS)} FROM reports ORDER BY seq").fetchall()
        except sqlite3.OperationalError:          # a queue file that was made empty: nothing queued
            return []
        return [dict(zip(_REPORT_COLS, r)) for r in rows]
    finally:
        conn.close()


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


# ------------------------------------------------------------------------------------------------ the applier
@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)                       # report ids copied into worker_attach
    refused: list[tuple[str, str]] = field(default_factory=list)           # (report id, reason) written to report_refusals
    deferred: list[str] = field(default_factory=list)                      # left queued: the live listing was unreadable


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


def _check(r: Mapping[str, Any]) -> dict[str, Any]:
    """The report's fields, checked mechanically, or ValueError naming the first bad one."""
    out = {"report_id": _text(r.get("report_id"), "report_id", 100, required=True),
           "item_id": _text(r.get("item_id"), "item_id", _FIELD_MAX, required=True),
           "worker": _text(r.get("worker"), "worker", _FIELD_MAX, required=True),
           "launch_id": _text(r.get("launch_id"), "launch_id", _FIELD_MAX),
           "harness_session_id": _text(r.get("harness_session_id"), "harness_session_id", _FIELD_MAX),
           "why": None if r.get("why") is None else _text(r["why"], "why", _WHY_MAX),
           "at": _text(r.get("at"), "at", 64, required=True)}
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


def _refuse(conn: sqlite3.Connection, now: str, by: str, report_id: str, item_id: Any, worker: Any, reason: str,
            detail: str) -> None:
    def keep(v: Any) -> str | None:
        return v[:_FIELD_MAX] if isinstance(v, str) else None
    conn.execute("INSERT INTO report_refusals (gseq, report_id, item_id, worker, reason, detail, at, by) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (next_gseq(conn, now), report_id, keep(item_id), keep(worker), reason, detail[:_DETAIL_MAX], now, by))


def _apply_one(conn: sqlite3.Connection, now: str, token: int, by: str, r: dict[str, Any],
               live: Collection[str] | None) -> str:
    """Apply one checked report inside the caller's savepoint. Returns 'applied' or 'deferred'; a refusal raises
    :class:`_Refused`."""
    from levain.coordinator import ready

    item = conn.execute("SELECT MAX(ver) FROM ready_items WHERE id = ?", (r["item_id"],)).fetchone()[0]
    if item is None:
        raise _Refused("unknown-item", f"{r['item_id']} is not a ready item in this store")
    real = r["launch_id"] is not None and conn.execute("SELECT 1 FROM launches WHERE launch_id = ?",
                                                       (r["launch_id"],)).fetchone() is not None
    if not real and r["harness_session_id"] is not None:
        if live is None:
            return "deferred"
        real = r["harness_session_id"] in live
    if not real:
        raise _Refused("worker-not-a-launch", "the report names no launch of this store and no live session")
    open_id = _open_attach(conn, r["item_id"], r["worker"])
    if r["event"] == "attach":
        if open_id is not None:
            raise _Refused("already-attached", f"{r['worker']} is already attached to {r['item_id']} ({open_id})")
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


class _Refused(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def apply_reports(store: CoordStore, token: Any, *, live_sessions: Collection[str] | None) -> ApplyResult:
    """Copy every unapplied report in ``<store>/reports.db`` into ``worker_attach``, or refuse it with a
    ``report_refusals`` row, in ONE write transaction. Idempotent on ``report_id``: a report already applied or refused
    is skipped, so a re-run writes nothing twice. Facts only, by the store's role: ``service`` writes as the service
    (``token`` must be the live lease's), ``coordinator`` as the lease holder (fenced). A landed report also sets the
    item's ``landed_sha`` and ``branch`` (:func:`levain.coordinator.ready.record_landed`) and nothing else about it.

    ``live_sessions``: the harness session ids the coordinator's own listing shows (:func:`live_session_ids`). None means
    the listing could not be read: a report resting on a session id alone is left queued, never refused on no evidence."""
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
            from levain.coordinator.lease import lease

            held = lease(conn, now)
            if held is None or not held.live or held.token != token:
                raise CoordError(f"lease token {token} is not the live lease's; no report was applied")
        seen = {r[0] for r in conn.execute("SELECT report_id FROM worker_attach WHERE report_id IS NOT NULL")}
        seen |= {r[0] for r in conn.execute("SELECT report_id FROM report_refusals")}
        for raw in queued:
            rid = str(raw["report_id"])[:100]
            if rid in seen:
                continue
            seen.add(rid)
            conn.execute("SAVEPOINT report")
            try:
                try:
                    checked = _check(raw)
                except ValueError as exc:
                    raise _Refused("malformed-report", str(exc)) from exc
                got = _apply_one(conn, now, token, by, checked, live_sessions)
                conn.execute("RELEASE report")
                (result.applied if got == "applied" else result.deferred).append(rid)
            except (_Refused, sqlite3.IntegrityError) as exc:
                conn.execute("ROLLBACK TO report")
                conn.execute("RELEASE report")
                reason, detail = (exc.reason, exc.detail) if isinstance(exc, _Refused) else ("item-refused", str(exc))
                _refuse(conn, now, by, rid, raw.get("item_id"), raw.get("worker"), reason, detail)
                result.refused.append((rid, reason))
    return result


# ------------------------------------------------------------------------------------------------ the watch's pass
def _unapplied(store: CoordStore) -> int:
    """How many queued reports have neither a ``worker_attach`` row nor a refusal yet; nothing written."""
    from levain.autonomic.durable_sqlite import read_transaction

    queued = {str(r["report_id"])[:100] for r in _queued(store.directory)}
    if not queued:
        return 0
    with read_transaction(lambda: store._connect(create=False)) as conn:
        done = {r[0] for r in conn.execute("SELECT report_id FROM worker_attach WHERE report_id IS NOT NULL")}
        done |= {r[0] for r in conn.execute("SELECT report_id FROM report_refusals")}
    return len(queued - done)


def watch_apply(board: Path | str | None = None, *, sessions: Any = None) -> list[str]:
    """The ``levain coord watch`` pass over the board store: apply the queued reports as the ``service``, facts only.

    Nothing happens, and no line is returned, when the board has no ``dispatch.db`` or no report is waiting. A live
    lease (a coordinator's, or the operator's) is never taken from its holder: the pass applies nothing and says so in
    one line, and the reports wait for the next pass. With no live lease the pass claims it as holder ``service``,
    reads the live session listing once (``sessions(store)``, default :func:`live_session_ids`), applies, and
    releases the lease in a ``finally``, so a coordinator that wakes meanwhile is refused only for the length of one
    apply. The service role refuses every write that decides (:meth:`CoordStore._write`), so the only things this
    pass can write are ``worker_attach``, ``report_refusals``, the item's landed sha and branch, and its own lease
    rows. A fault is a line, never a raise: the watch goes on."""
    from levain.coordinator import lease as _lease

    directory = Path(board) if board is not None else board_store_dir()
    if not (directory / "dispatch.db").exists() or not reports_path(directory).exists():
        return []
    try:
        store = CoordStore(directory, role="service")
        waiting = _unapplied(store)
        if not waiting:
            return []
        plural = "report" if waiting == 1 else "reports"
        with store._write("service", None, create=False) as (conn, now):
            token, held = _lease.service_claim(conn, now, cause="watch apply")
            if held is not None:
                how = (f"is held by {held.holder} until {held.until}" if held.live else
                       f"of {held.holder} lapsed at {held.until}, and recording that is the coordinator's")
                return [f"{waiting} {plural} waiting: the board's lease {how}; applied nothing this pass"]
        try:
            live = (sessions or live_session_ids)(store)
            got = apply_reports(store, token, live_sessions=live)
        finally:
            with store._write("service", None, create=False) as (conn, now):
                _lease.service_release(conn, now, token, cause="watch apply done")
        return [f"board reports: applied {len(got.applied)}, refused {len(got.refused)}, deferred {len(got.deferred)}"
                + "".join(f"; {rid} refused: {why}" for rid, why in got.refused)]
    except (CoordError, sqlite3.Error, OSError) as exc:
        return [f"board reports: not applied this pass ({exc})"]
