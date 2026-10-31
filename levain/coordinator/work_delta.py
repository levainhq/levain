"""C11 S3: the work delta in the coordinator's reply block (1030-2-c11/DESIGN.md section 3.4).

What changed in the coordinator store since the operator's last VERIFIED reply, rendered by Levain from records that
carry the store's ``gseq`` ("one store-wide order for every authority and event row: never wall-clock"): ``replies``,
``launch_events``, ``lease_events``, ``ready_journal`` and ``chat_messages``. Never model-written. Thread splits and
state changes carry no ``gseq`` and are not in it; a reply written before ``replies`` had the column (NULL ``gseq``) has
no place in the order, so it is excluded and the header counts it.

The block is at most :data:`MAX_LINES` lines plus ``+N more`` and the command that lists the rest. The work watermark
is the operator's cursor: an append-only ``operator_views(viewer, gseq, at)`` in the coordinator store (D2), advanced by
the Stop hook only after the reply reprinted the block (:func:`advance`). The band delta stays separate in
``questions.db``; nothing orders across the two databases. Stdlib only."""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from levain.cockpit.consent import utc_stamp
from levain.coordinator.store import CoordStore, _append_only

__all__ = ["HEAD", "MAX_LINES", "OPERATOR_VIEWS_SCHEMA", "WorkBlock", "WorkDelta", "advance", "read_block", "render",
           "watermark", "work_delta"]

MAX_LINES = 5
_FIELD_MAX = 60
HEAD = "since your last message (Levain's record"     # every block's first line starts so (the Stop's retry names it)
UNREAD = "! not read (the coordinator store could not be read just now)"

OPERATOR_VIEWS_SCHEMA: tuple[str, ...] = (
    # the operator's work cursor: one row per verified reply that covered new records. Additive (IF NOT EXISTS) like the
    # lifecycle tables, so a store from before it is still current for every reader and reads as watermark 0
    "CREATE TABLE IF NOT EXISTS operator_views (viewer TEXT NOT NULL CHECK (length(viewer) > 0), "
    "gseq INTEGER NOT NULL CHECK (gseq >= 0), at TEXT NOT NULL)",
    *_append_only("operator_views", "an operator view"),
    # a cursor never moves back (two coordinator sessions share the viewer: the later, lower write is refused)
    "CREATE TRIGGER IF NOT EXISTS operator_views_forward BEFORE INSERT ON operator_views WHEN NEW.gseq <= "
    "COALESCE((SELECT MAX(gseq) FROM operator_views WHERE viewer = NEW.viewer), -1) "
    "BEGIN SELECT RAISE(ABORT, 'the operator work cursor only moves forward'); END",
)


def _clean(value: Any, limit: int = _FIELD_MAX) -> str:
    """A stored word as one bounded printable line: whitespace and control characters collapsed, cut at ``limit``."""
    text = re.sub(r"[\x00-\x1f\x7f\s]+", " ", "" if value is None else str(value)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _short_id(value: Any) -> str:
    text = _clean(value, 80)
    return text if len(text) <= 10 else ".." + text[-4:]


def _reply_line(kind: str, ref_store: str, ref_id: str, body: str) -> str:
    try:
        b = json.loads(body)
        b = b if isinstance(b, dict) else {}
    except ValueError:
        b = {}
    if kind == "drop_receipt":
        return f"drop {_short_id(b.get('drop_id') or ref_id)} receipt: {_clean(b.get('stage'), 20) or 'recorded'}"
    if kind == "coordinator_reply":
        # delivered when written, so no terminal reply carried it: the next one names it (DESIGN 3.4)
        at = _clean(b.get("received_at"), 40)
        hhmm = at[11:16] if len(at) >= 16 else at
        return (f"1 reply to your {_clean(b.get('surface'), 12) or 'drop'} message at {hhmm} "
                f"(drop {_short_id(b.get('drop_id') or ref_id)}): on the Coordinator panel")
    if kind == "know":
        n = b.get("question_n")
        if isinstance(n, int):
            return f"KNOW on question #{n} (yours to confirm)"
        return f"KNOW: {_clean(b.get('text'))}"
    if kind == "sign":
        return f"SIGN waits on you: {_clean(b.get('text'))}"
    return f"{_clean(kind)} on {_clean(ref_store, 20)} {_short_id(ref_id)}"


def _launch_line(kind: str, launch_id: str, role: str, cause: str) -> str:
    tail = f": {_clean(cause)}" if kind in ("ended", "failed", "expired") and cause else ""
    return f"{_clean(role, 20)} launch {_short_id(launch_id)} {_clean(kind, 20)}{tail}"


def _lease_line(kind: str, holder: str, cause: str, by: str) -> str:
    return f"lease {_clean(kind, 20)}: {_clean(holder)} ({_clean(cause)}, by {_clean(by, 20)})"


def _ready_line(item_id: str, from_category: str | None, to_category: str, who: str) -> str:
    return f"{_clean(item_id)}: {_clean(from_category, 20) or 'new'} -> {_clean(to_category, 20)} ({_clean(who)})"


def _chat_line(author_kind: str, recipient: str, thread_id: str) -> str:
    return f"chat message from {_clean(author_kind, 20)} to {_clean(recipient, 20)} on thread {_short_id(thread_id)}"


class _Source(NamedTuple):
    table: str                    # the table that must exist
    frm: str                      # the FROM clause
    gseq: str                     # the qualified gseq column
    cols: str                     # the columns the line is made of (after gseq)
    line: Callable[..., str]
    where: str = "1"              # which rows are news


_SOURCES: tuple[_Source, ...] = (
    _Source("replies", "replies", "gseq", "kind, ref_store, ref_id, body", _reply_line),
    _Source("launch_events", "launch_events e JOIN launches l ON l.launch_id = e.launch_id", "e.gseq",
            "e.kind, e.launch_id, l.role, e.cause", _launch_line),
    # a beat is the holder staying alive, a frequent record and never news (the design lists claim, release, lapse, takeover)
    _Source("lease_events", "lease_events", "gseq", "kind, holder, cause, by", _lease_line, "kind != 'beat'"),
    _Source("ready_journal", "ready_journal", "gseq", "id, from_category, to_category, who", _ready_line),
    _Source("chat_messages", "chat_messages", "gseq", "author_kind, recipient, thread_id", _chat_line),
)


class WorkDelta(NamedTuple):
    """``lines``: ``(gseq, text)`` of the earliest ``limit`` records after ``since``, in ``gseq`` order. ``total``: how
    many records are after ``since`` in all. ``through``: the greatest ``gseq`` among them (``since`` when none).
    ``unordered``: replies with no ``gseq``, which no cursor can place."""
    lines: list[tuple[int, str]]
    total: int
    through: int
    unordered: int


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def watermark(conn: sqlite3.Connection, viewer: str) -> int:
    """The viewer's work cursor: the greatest ``gseq`` a verified reply covered (0 when none, or the table is not
    there yet)."""
    if "operator_views" not in _tables(conn):
        return 0
    return conn.execute("SELECT COALESCE(MAX(gseq), 0) FROM operator_views WHERE viewer = ?", (viewer,)).fetchone()[0]


def work_delta(conn: sqlite3.Connection, since: int, limit: int = MAX_LINES) -> WorkDelta:
    """Every source's records after ``since``, read in ONE read snapshot. A source whose table this store lacks (a
    plain coordinator store has no chat tables) contributes nothing. Raises SQLite's errors (a busy store); the caller
    says ``! not read``."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        have = _tables(conn)
        lines: list[tuple[int, str]] = []
        total, through = 0, since
        for s in _SOURCES:
            if s.table not in have:
                continue
            n, top = conn.execute(f"SELECT COUNT(*), MAX({s.gseq}) FROM {s.frm} WHERE {s.gseq} > ? AND {s.where}",
                                  (since,)).fetchone()
            if not n:
                continue
            total += n
            through = max(through, top)
            for row in conn.execute(f"SELECT {s.gseq}, {s.cols} FROM {s.frm} WHERE {s.gseq} > ? AND {s.where} "
                                    f"ORDER BY {s.gseq} LIMIT ?", (since, limit)):
                lines.append((row[0], s.line(*row[1:])))
        unordered = (conn.execute("SELECT COUNT(*) FROM replies WHERE gseq IS NULL").fetchone()[0]
                     if "replies" in have else 0)
    finally:
        if own:
            conn.execute("ROLLBACK")              # a read snapshot: nothing to commit
    lines.sort(key=lambda g: g[0])
    return WorkDelta(lines[:limit], total, through, unordered)


def render(delta: WorkDelta) -> str:
    """The block as the operator reads it; ``""`` when nothing is after the cursor."""
    if not delta.total:
        return ""
    shown = delta.lines
    note = ""
    if delta.unordered:
        note = (f"; {delta.unordered} older reply carries no order and is not listed" if delta.unordered == 1
                else f"; {delta.unordered} older replies carry no order and are not listed")
    out = [f"{HEAD}, {len(shown)} of {delta.total}{note}):"]
    out.extend(f"  {text}" for _g, text in shown)
    more = delta.total - len(shown)
    if more > 0:
        out.append(f"  +{more} more: levain coord log --since {shown[-1][0]}")
    return "\n".join(out)


class WorkBlock(NamedTuple):
    """``text``: the block (``""`` for nothing to say; the ``! not read`` line for a store that could not be read).
    ``through``: the cursor a verified reply would advance to (0 when it must not move). ``store_id``: the
    ``meta.store_id`` of the store it was read from (``""`` when none was read), which :func:`advance` requires."""
    text: str
    through: int
    store_id: str = ""


def _store_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def read_block(directory: Path | str | None, viewer: str, busy_s: float) -> WorkBlock:
    """The viewer's block from the coordinator store in ``directory``, waiting at most ``busy_s`` on a lock. The
    cursor, the store's id and the records come from ONE read snapshot. Never raises and never answers a store it
    could not read with an empty block: that is the ``! not read`` line, and the cursor stays where it was."""
    try:
        conn = CoordStore(directory).open_readonly(busy_s=max(0.0, busy_s))
    except Exception:                                            # noqa: BLE001 - said, not swallowed
        return WorkBlock(f"{HEAD}): {UNREAD}", 0)
    try:
        conn.execute("BEGIN")
        try:
            since = watermark(conn, viewer)
            store_id = _store_id(conn)
            delta = work_delta(conn, since)                      # inside this snapshot (it opens none of its own)
        finally:
            conn.execute("ROLLBACK")
    except Exception:                                            # noqa: BLE001 - a busy store, a table that will not read
        return WorkBlock(f"{HEAD}): {UNREAD}", 0)
    finally:
        conn.close()
    return WorkBlock(render(delta), delta.through if delta.total else 0, store_id)


def advance(directory: Path | str | None, viewer: str, through: int, busy_s: float, store_id: str) -> bool:
    """Append ``operator_views(viewer, through, now)`` when ``through`` is past the viewer's cursor: one bounded write,
    called only after the reply that carried the block verified. ``store_id`` is the one the block was read from
    (:attr:`WorkBlock.store_id`): a store whose ``meta.store_id`` is another (``dispatch.db`` replaced between the read
    and the Stop) is refused, so another store's records are never skipped by this store's cursor. A store from before
    the table gets it here, inside the same ``BEGIN IMMEDIATE`` (:data:`OPERATOR_VIEWS_SCHEMA`, the table and its
    triggers), so its cursor moves at the first verified reply; the reader path never makes it. ``False`` (and nothing
    written) for another store, a cursor already there, a busy store or any other failure: the next reply repeats the
    delta, the cheap error. Never raises."""
    if not isinstance(through, int) or through <= 0 or not isinstance(store_id, str) or not store_id:
        return False
    try:
        store = CoordStore(directory)
        store._check_directory_readonly()
        if not store.path.exists():
            return False
        wait = max(0.0, busy_s)
        conn = sqlite3.connect(store.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=wait, isolation_level=None)
    except Exception:                                            # noqa: BLE001
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(wait * 1000)}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _store_id(conn) != store_id:
                conn.execute("ROLLBACK")
                return False
            if "operator_views" not in _tables(conn):
                for stmt in OPERATOR_VIEWS_SCHEMA:
                    conn.execute(stmt)
            elif watermark(conn, viewer) >= through:
                conn.execute("ROLLBACK")
                return False
            now = utc_stamp(datetime.now(timezone.utc))
            conn.execute("INSERT INTO operator_views (viewer, gseq, at) VALUES (?, ?, ?)", (viewer, through, now))
            conn.execute("COMMIT")
            return True
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except Exception:                                            # noqa: BLE001
        return False
    finally:
        conn.close()
