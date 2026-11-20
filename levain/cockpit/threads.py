"""The thread record's shared readers: what the room's read (:mod:`levain.cockpit.room`) still takes from the desk's
``dispatch.db``.

The thread view's own routes (``GET /cockpit/threads.json``, ``/cockpit/thread/<id>.json`` and ``POST
/cockpit/thread/<id>/stop``) are gone with the Threads zone that drew them; what stays is read-only helpers over
``<directory>/dispatch.db`` opened ``mode=ro`` (this module never creates the file, a table or a trigger, and writes
nothing): the connection and the table probe, the per-event facts, one timeline row as the room's stream item, and the
model a session reported. A store missing a table degrades only the part that table feeds."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from levain.desk import chat
from levain.desk.chat_approvals import APPROVAL_EVENT_KINDS

_APPROVAL_KINDS = ("vendor_approval_request", "vendor_approval_resolved")
# chat_timeline's own approval arms (chat_approvals.APPROVAL_VIEW_ARMS): 'approval', and 'approval_' + each event kind
_VIEW_APPROVAL_KINDS = frozenset(("approval", *("approval_" + k for k in APPROVAL_EVENT_KINDS)))


def _open(directory: Path | str) -> sqlite3.Connection | None:
    path = Path(directory) / "dispatch.db"
    if not path.is_file():
        return None
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2.0, isolation_level=None)


def _has(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None



def model_in_use(service: Any, launch: str | None) -> str | None:
    """The model the attached session reported in its init (``SessionMeta.model``), or None: no service, no launch, no
    session, a meta that was not read at attach, or a driver that named none. A dict read: the service caches the meta
    once at attach (``ChatService.session_meta``) and no driver method runs here, so a poll never reaches a session."""
    if service is None or launch is None:
        return None
    try:
        meta = service.session_meta(launch)
    except Exception:       # noqa: BLE001 - a model that cannot be read is shown as absent, never guessed
        return None
    model = getattr(meta, "model", None)
    return model if isinstance(model, str) and model else None



_Fact = tuple[str | None, int, int, str | None]       # (call_id, truncated, backfill, tool_name)


def _event_facts(conn: sqlite3.Connection, launches: set[str]) -> tuple[dict[tuple[str, str], _Fact], bool]:
    """``({(launch_id, key): (call_id, truncated, backfill, tool_name)}, gap)``; ``gap`` is true when any launch's latest
    cursor row says the driver could not catch up, or the stream holds a ``gap`` event. A table the store lacks adds
    nothing, and a store this reader opens read-only before the writer has added ``tool_name`` gives every name None."""
    facts: dict[tuple[str, str], _Fact] = {}
    gap = False
    has_events, has_cursors = _has(conn, "chat_events"), _has(conn, "chat_cursors")
    named = has_events and any(r[1] == "tool_name" for r in conn.execute("PRAGMA table_info(chat_events)"))
    for launch in launches:
        if has_events:
            for key, call_id, truncated, backfill, kind, tool_name in conn.execute(
                    "SELECT vendor_event_key, call_id, truncated, backfill, kind, %s FROM chat_events WHERE launch_id = ?"
                    % ("tool_name" if named else "NULL"), (launch,)):
                facts[(launch, key)] = (call_id, truncated, backfill, tool_name)
                gap = gap or kind == "gap"
        if has_cursors:
            last = conn.execute("SELECT gap FROM chat_cursors WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                                (launch,)).fetchone()
            gap = gap or bool(last and last[0])
    return facts, gap


def _item(row: chat.TimelineRow, facts: Mapping[tuple[str, str], _Fact],
          done: set[tuple[str, str]]) -> dict[str, Any]:
    call_id, truncated, backfill, tool_name = facts.get((row.launch_id or "", row.ref_id), (None, 0, 0, None))
    # an approval's ref is the kind alone and its body is dropped: the vendor's control-request id is not a queue request
    # id, and a driver may put the tool name, its input or that id in the body, so none of it is offered
    view_approval = row.kind in _VIEW_APPROVAL_KINDS
    approval = view_approval or row.kind in _APPROVAL_KINDS
    # the view arms' ref is the whole durable key (spawn nonce, vendor tool_use_id): a constant, never derived from it
    ref = row.kind if view_approval else row.ref_id.partition(":")[0] if approval else row.ref_id
    item: dict[str, Any] = {"gseq": row.gseq, "kind": row.kind, "source": row.source, "ref": ref,
                            "launch_id": row.launch_id, "status": row.status, "body": None if approval else row.body,
                            "at": row.at,
                            "echo_of": row.echo_of, "truncated": bool(truncated), "backfill": bool(backfill)}
    if row.kind == "message" and row.source == "operator":
        item["unverified"] = True       # no signature check exists on the record, so the view never says "verified"
    if row.kind == "vendor_tool_call" and call_id:
        item["tool"] = {"call_id": call_id, "open": (row.launch_id or "", call_id) not in done}
    if row.kind in ("vendor_tool_call", "vendor_tool_result"):
        # the vendor's own name for the tool (None for a row stored before the column existed): a page strips a Read's line
        # numbers only for "Read", and keeps its shape rule for None
        item["tool_name"] = tool_name
    return item

