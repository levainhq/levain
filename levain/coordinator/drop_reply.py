"""C11 S4: the coordinator's reply to a drop (1030-2-c11/DESIGN.md sections 3.3 and 3.4, D3 = a).

Words the operator sent from the cockpit or the phone have no terminal reply to carry Levain's block, so the
coordinator answers them with ``levain coord reply DROP_ID --text ...``: a ``coordinator_reply`` row on ``replies``,
bound to the drop. The binding (a drop that exists, one reply per drop, inside the window) is the store's own trigger
(:data:`levain.coordinator.store.REPLY_BOUND`), so no writer can step around it; this module only writes the row.

The reply carries a block FROZEN at write: the work delta of :mod:`levain.coordinator.work_delta` (the same renderer and
the same operator cursor the terminal reply uses), then the question band as it stood, and the gseq range the block
covers. Every later view of the reply shows that stored text, never a re-render. Writing it advances the operator's
work cursor to the last record the block covered: a reply on these surfaces has no receipt, so it counts as delivered
when written (DESIGN section 8). The reply's own row stays after the cursor, so the next terminal block names it.

"No reply yet" is derived, never stored: a drop inside the window with no coordinator reply after
:data:`UNREPLIED_AFTER_MINUTES` (:func:`unreplied`)."""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from levain.coordinator import work_delta
from levain.coordinator.store import (REPLY_WINDOW_HOURS, UNREPLIED_AFTER_MINUTES, CoordError, CoordStore, next_gseq)

__all__ = ["REPLY_MAX", "DropReply", "VIEWER", "replies", "unreplied", "unreplied_line", "write_reply"]

#: the operator's work cursor is the coordinator viewer's: the one the terminal reply's block advances
VIEWER = "coordinator"
REPLY_MAX = 4000


@dataclass(frozen=True)
class DropReply:
    reply_id: str
    drop_id: str
    gseq: int
    at: str
    surface: str
    received_at: str
    text: str
    block: str                    # frozen at write: the work delta, then the band
    after: int                    # the block covers gseq (after, through]
    through: int


def _band_text(directory: Any) -> str:
    """The question band as the operator would read it now, rendered without taking a view (no cursor moves)."""
    try:
        from levain.band_view import render_full
        from levain.consent.questions import QuestionStore

        return render_full(QuestionStore(directory)).text or ""
    except Exception:                                      # noqa: BLE001 - said in the block, never an empty claim
        return "NEEDS YOU: ! not read (the question band could not be read just now)"


def _from_row(row: tuple[Any, ...]) -> DropReply:
    reply_id, ref_id, at, body, gseq = row
    b = json.loads(body)
    covers = b.get("covers") or {}
    return DropReply(reply_id, ref_id, gseq, at, b.get("surface", ""), b.get("received_at", ""), b.get("text", ""),
                     b.get("block", ""), covers.get("after", 0), covers.get("through", 0))


def write_reply(store: CoordStore, drop_id: str, text: str, *, token: object) -> DropReply:
    """The coordinator's reply to ``drop_id``, lease-fenced by ``token``. The store's trigger refuses a missing drop, a
    second reply and an expired drop; its message is raised as :class:`CoordError`."""
    if not isinstance(text, str) or not text.strip():
        raise CoordError("say what you reply: --text TEXT, --text-file PATH or --stdin")
    if len(text) > REPLY_MAX:
        raise CoordError(f"a reply is at most {REPLY_MAX} characters (got {len(text)})")
    band = _band_text(store.directory)                     # a second database: read before the write lock is taken
    try:
        with store._write("coordinator", token) as (conn, now):
            drop = conn.execute("SELECT surface, received_at FROM drops WHERE drop_id = ?", (drop_id,)).fetchone()
            since = work_delta.watermark(conn, VIEWER)
            delta = work_delta.work_delta(conn, since)     # inside this write's transaction: one snapshot
            block = "\n".join(p for p in (work_delta.render(delta), band) if p)
            gseq = next_gseq(conn, now)
            body = {"text": text, "drop_id": drop_id, "surface": drop[0] if drop else "",
                    "received_at": drop[1] if drop else "", "block": block,
                    "covers": {"after": since, "through": gseq}}
            threads = store._threads_of(conn, drop_id) if drop else []
            reply_id = "rp-" + secrets.token_hex(16)
            # the trigger decides here: nothing is checked first, so the refusal is the store's own
            conn.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq) "
                         "VALUES (?, ?, 'coordinator_reply', 'drops', ?, ?, ?, ?)",
                         (reply_id, threads[0] if threads else None, drop_id, json.dumps(body, sort_keys=True,
                                                                                         separators=(",", ":")),
                          now, gseq))
            if delta.total and delta.through > since:
                conn.execute("INSERT INTO operator_views (viewer, gseq, at) VALUES (?, ?, ?)",
                             (VIEWER, delta.through, now))
    except sqlite3.IntegrityError as exc:
        raise CoordError(f"refused: {exc}") from exc
    return DropReply(reply_id, drop_id, gseq, now, body["surface"], body["received_at"], text, block, since, gseq)


def replies(conn: sqlite3.Connection, limit: int = 5) -> list[DropReply]:
    """The newest ``limit`` coordinator replies, newest first, each as it was stored (the block is never re-rendered)."""
    return [_from_row(r) for r in conn.execute(
        "SELECT reply_id, ref_id, at, body, gseq FROM replies WHERE kind = 'coordinator_reply' "
        "ORDER BY gseq DESC LIMIT ?", (limit,))]


def unreplied(conn: sqlite3.Connection, now: str) -> list[tuple[str, str, str]]:
    """``(drop_id, surface, received_at)`` of each drop with no coordinator reply that is past
    :data:`UNREPLIED_AFTER_MINUTES` and still inside the reply window (after it the store would refuse a reply, so
    the line would name something nobody can do), oldest first. ``now``: a store stamp."""
    return conn.execute(
        "SELECT d.drop_id, d.surface, d.received_at FROM drops d WHERE NOT EXISTS (SELECT 1 FROM replies r WHERE "
        "r.kind = 'coordinator_reply' AND r.ref_store = 'drops' AND r.ref_id = d.drop_id) "
        "AND (julianday(?) - julianday(d.received_at)) * 1440 > ? "
        "AND (julianday(?) - julianday(d.received_at)) * 24 <= ? ORDER BY d.rowid",
        (now, UNREPLIED_AFTER_MINUTES, now, REPLY_WINDOW_HOURS)).fetchall()


def unreplied_line(drop_id: str, surface: str, received_at: str) -> str:
    """``drop dr-.. (14:02, phone): no reply yet``: the silence made visible (DESIGN 3.3)."""
    return f"drop {drop_id} ({received_at[11:16]}, {surface}): no reply yet"
