"""The cockpit's thread view, read half (CH-U1a): the coordinator's threads and one thread's stream as JSON.

Pure readers over ``<directory>/dispatch.db`` opened ``mode=ro``: this module never creates the file, a table or a
trigger, and writes nothing. A thread's stream is :func:`levain.coordinator.chat.timeline`: the "before the chat
record" block, then the merged stream in ``gseq`` order, the only order a thread view promises (no ordering by
``at`` across it). A tool call stays open until its own completion event. A driver without ``catch_up`` shows as a
``gap``. A card is the ``queue.db`` request an approval event names; it is a link to the consent body route, never a
copy of the request. An operator line is ``unverified``: no signature check exists on the record.

``serve_threads`` is the handler a server mount calls; it holds no HTTP of its own."""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain.cockpit.consent import utc_stamp
from levain.coordinator import chat
from levain.coordinator.store import CoordError

THREADS_LIST_PATH = "/cockpit/threads.json"
THREAD_PREFIX = "/cockpit/thread/"
THREAD_SUFFIX = ".json"
CONSENT_PREFIX = "/cockpit/consent/"        # the existing consent body route a card links to
VIEW_OWNER = "cockpit:thread-view"          # the owner whose composer the read-only view reports
LIST_MAX = 200
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_CARD_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
_CARD_KINDS = ("vendor_approval_request", "vendor_approval_resolved")


def is_threads_path(path: str) -> bool:
    """Whether ``path`` is one of this module's routes (the mount's dispatch test)."""
    return path == THREADS_LIST_PATH or (path.startswith(THREAD_PREFIX) and path.endswith(THREAD_SUFFIX))


def _open(directory: Path | str) -> sqlite3.Connection | None:
    path = Path(directory) / "dispatch.db"
    if not path.is_file():
        return None
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0, isolation_level=None)


def _has(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _composer(conn: sqlite3.Connection, thread_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """``(launch_id, composer)`` for the thread's latest worker launch; nothing is attached, so a live launch reads
    ``no_session``. ``(None, None)`` when the thread has no worker launch."""
    if not _has(conn, "launches"):
        return None, None
    row = conn.execute(
        "SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ? AND l.role = 'worker' "
        "ORDER BY l.gseq DESC LIMIT 1", (thread_id,)).fetchone()
    if row is None:
        return None, None
    c = chat.composer_state(conn, utc_stamp(datetime.now(timezone.utc)), row[0], VIEW_OWNER, attached=False)
    return row[0], {"state": c.state, "why": c.why, "holder": c.holder, "reenable": c.reenable,
                    "unresolved": list(c.unresolved)}


def thread_list(directory: Path | str) -> dict[str, Any]:
    """Every thread with a worker launch or a chat record, newest activity first (at most ``LIST_MAX``)."""
    conn = _open(directory)
    if conn is None:
        return {"threads": [], "truncated": False}
    try:
        conn.execute("BEGIN")
        try:
            if not (_has(conn, "threads") and _has(conn, "chat_timeline")):
                return {"threads": [], "truncated": False}
            rows = conn.execute(
                "SELECT t.thread_id, (SELECT MAX(gseq) FROM chat_timeline c WHERE c.thread_id = t.thread_id) AS g "
                "FROM threads t ORDER BY g IS NULL, g DESC, t.thread_id LIMIT ?", (LIST_MAX + 1,)).fetchall()
            out = []
            for thread_id, latest in rows[:LIST_MAX]:
                at = conn.execute("SELECT at FROM chat_timeline WHERE thread_id = ? AND gseq = ?",
                                  (thread_id, latest)).fetchone() if latest is not None else None
                launch, composer = _composer(conn, thread_id)
                out.append({"thread_id": thread_id, "latest_gseq": latest, "latest_at": at[0] if at else None,
                            "launch_id": launch, "composer": composer})
            return {"threads": out, "truncated": len(rows) > LIST_MAX}
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


def _event_facts(conn: sqlite3.Connection, launches: set[str]) -> tuple[dict[tuple[str, str], tuple[str | None, int, int]], bool]:
    """``({(launch_id, key): (call_id, truncated, backfill)}, gap)``; ``gap`` is true when any launch's latest cursor
    row says the driver could not catch up, or the stream holds a ``gap`` event."""
    facts: dict[tuple[str, str], tuple[str | None, int, int]] = {}
    gap = False
    for launch in launches:
        for key, call_id, truncated, backfill, kind in conn.execute(
                "SELECT vendor_event_key, call_id, truncated, backfill, kind FROM chat_events WHERE launch_id = ?",
                (launch,)):
            facts[(launch, key)] = (call_id, truncated, backfill)
            gap = gap or kind == "gap"
        if _has(conn, "chat_cursors"):
            last = conn.execute("SELECT gap FROM chat_cursors WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                                (launch,)).fetchone()
            gap = gap or bool(last and last[0])
    return facts, gap


def _item(row: chat.TimelineRow, facts: Mapping[tuple[str, str], tuple[str | None, int, int]],
          done: set[str]) -> dict[str, Any]:
    call_id, truncated, backfill = facts.get((row.launch_id or "", row.ref_id), (None, 0, 0))
    item: dict[str, Any] = {"gseq": row.gseq, "kind": row.kind, "source": row.source, "ref": row.ref_id,
                            "launch_id": row.launch_id, "status": row.status, "body": row.body, "at": row.at,
                            "echo_of": row.echo_of, "truncated": bool(truncated), "backfill": bool(backfill)}
    if row.kind == "message" and row.source == "operator":
        item["unverified"] = True       # no signature check exists on the record, so the view never says "verified"
    if row.kind == "vendor_tool_call" and call_id:
        item["tool"] = {"call_id": call_id, "open": call_id not in done}
    if row.kind in _CARD_KINDS:
        card = row.ref_id.partition(":")[2]
        if _CARD_ID.fullmatch(card):
            item["card"] = {"id": card, "href": CONSENT_PREFIX + urllib.parse.quote(card, safe="") + ".json"}
    return item


def thread_stream(directory: Path | str, thread_id: str) -> dict[str, Any] | None:
    """One thread: ``before`` (older replies, by ``at`` among themselves), ``stream`` (``gseq`` order), the ``gap``
    flag and the composer; None for a thread the store does not hold."""
    if _THREAD_ID.fullmatch(thread_id) is None:
        return None
    conn = _open(directory)
    if conn is None:
        return None
    try:
        conn.execute("BEGIN")
        try:
            if not (_has(conn, "threads") and _has(conn, "chat_timeline")):
                return None
            try:
                before, merged = chat.timeline(conn, thread_id)
            except CoordError:
                return None
            launches = {r[0] for r in conn.execute(
                "SELECT l.launch_id FROM launches l, json_each(l.threads) j WHERE j.value = ?", (thread_id,))}
            facts, gap = _event_facts(conn, launches)
            done = {c for (_l, k), (c, _t, _b) in facts.items() if k.startswith("tool_result:") and c}
            launch, composer = _composer(conn, thread_id)
            return {"thread_id": thread_id, "launch_id": launch, "composer": composer, "gap": gap,
                    "before": [_item(r, facts, done) for r in before],
                    "stream": [_item(r, facts, done) for r in merged]}
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()


def serve_threads(path: str, query: str, *, directory: Path | str) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` for a GET on one of this module's routes. ``query`` is accepted and ignored: the routes
    take none. A store that cannot be read is a 500 with a fixed sentence, never a traceback."""
    del query
    try:
        if path == THREADS_LIST_PATH:
            return 200, thread_list(directory)
        if path.startswith(THREAD_PREFIX) and path.endswith(THREAD_SUFFIX):
            body = thread_stream(directory, urllib.parse.unquote(path[len(THREAD_PREFIX):-len(THREAD_SUFFIX)]))
            if body is None:
                return 404, {"error": "not_found", "message": "no such thread recorded"}
            return 200, body
    except (sqlite3.Error, CoordError):
        return 500, {"error": "internal", "message": "the thread record could not be read"}
    return 404, {"error": "not_found", "message": "no such thread route"}


def encode(body: Mapping[str, Any]) -> bytes:
    """The bytes a mount sends: JSON a browser's ``JSON.parse`` accepts."""
    return json.dumps(body, allow_nan=False).encode("utf-8")
