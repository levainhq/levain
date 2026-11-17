"""levain.webpush.open_ping: a ping as a state that stays open until he acknowledges it (ND-6a, board levain#37; night
desk design 2.8).

``OPEN -> pushed -> (tap or answer = ACKED)``; no ack by the retry time and the ping is pushed again; after the
ceiling's pushes, the ping row closes as ``did_not_wake`` and a band row is written (``did_not_wake``, or
``unreachable`` when no push was delivered); the ping is over. The table is the state: a new process
over the same file resumes every open ping where the last left it.

WHAT CLOSES A PING is :data:`ACK_SOURCES` and nothing else: a tap (a notificationclick, an ``opens`` row) or his answer
on the sheet. A ``shown`` or ``received`` receipt says a worker ran, not that he saw it (``sw.js``), so
:meth:`OpenPings.ack` answers False for them and changes nothing. Any other source is a ValueError.

THE PUSH is the push sender's, injected (``sender``, default :func:`levain.webpush.sender.send`). Every push of a ping
carries the same ``ref``, so a phone shows one notification and an unreachable phone's queue holds one
(:func:`levain.webpush.sender.topic_for`). The title and body are what :mod:`levain.webpush.notify` lets through, the
url its fixed notices link, and the kind the priority's (:func:`levain.webpush.notify.kind_for`): no action, decision
or link of the caller's. A push is CLAIMED (counted, the next due time set) in a write transaction BEFORE it is
sent, ONE row at a time immediately before its own send, so a crash loses at most the one in-flight attempt and never
repeats one; a pass is bounded (:data:`MAX_PER_PASS` rows, :data:`PASS_WALL` seconds) so one tick never holds a night's
attempts hostage. A failed push counts toward the ceiling like any other, and each ping keeps how many sends the sender
reported as a 2xx: a ping that reaches its ceiling with none delivered gets an ``unreachable`` band row, not a
"he did not wake" one. The ping ROW's own state is ``did_not_wake`` for any ping closed unacknowledged; the band
row's kind is what tells the two apart.

NOT HERE: the next channel after N re-pushes (the design's Q4 follow-up), the heartbeat (:mod:`levain.desk.night_audit`)
and every production caller (levain#16); nothing calls :meth:`OpenPings.tick` on a timer yet. Stdlib plus the repo's
durable sqlite helper.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.webpush import notify

__all__ = ["ACK_SOURCES", "BACKOFF", "CEILING", "MAX_PER_PASS", "PASS_WALL", "OpenPings"]

#: the only sources that close a ping: a tap, or his answer. ``shown`` and ``received`` never do
ACK_SOURCES = ("tap", "answer")
_RECEIPTS = ("shown", "received")
#: [seat] the wait after the Nth push before the next one; after the last entry the last repeats
BACKOFF = (timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=20),
           timedelta(minutes=30))
#: [seat] pushes in all before a ping gives up, unless the order's signed ceiling says otherwise
CEILING = 8
#: [seat] most pushes one tick pass sends, and the most seconds it keeps starting new ones (a send itself is bounded by the
#: sender's own ``WALL_BUDGET``): what is left stays due for the next pass
MAX_PER_PASS = 10
PASS_WALL = 60.0
_ERROR_CHARS = 200
_WAKE_TEXT = "he did not wake: pushed {pushes} times, no tap or answer"
_UNREACHABLE_TEXT = "unreachable: no push was delivered ({pushes} tried)"

SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS open_pings (ping_id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL, "
    "priority TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, ceiling INTEGER NOT NULL, "
    "state TEXT NOT NULL CHECK (state IN ('open', 'acked', 'did_not_wake')), pushes INTEGER NOT NULL, "
    "opened_at TEXT NOT NULL, last_push_at TEXT, next_due_at TEXT NOT NULL, closed_at TEXT, ack_source TEXT, "
    "delivered INTEGER NOT NULL DEFAULT 0, last_error TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS open_pings_one_open ON open_pings (ref) WHERE state = 'open'",
    "CREATE TABLE IF NOT EXISTS ping_band_rows (row_id INTEGER PRIMARY KEY AUTOINCREMENT, ping_id INTEGER NOT NULL UNIQUE "
    "REFERENCES open_pings (ping_id), kind TEXT NOT NULL CHECK (kind IN ('did_not_wake', 'unreachable')), "
    "ref TEXT NOT NULL, pushes INTEGER NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '', "
    "opened_at TEXT NOT NULL, at TEXT NOT NULL)",
)
_COLUMNS = ("ping_id, ref, priority, title, body, ceiling, state, pushes, opened_at, last_push_at, next_due_at, "
            "closed_at, ack_source, delivered, last_error")
_KEYS = _COLUMNS.split(", ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _delivered(answer: Any) -> int:
    """How many of the sender's ``(subscription, status)`` answers are a 2xx: 0 for anything else (no answer, an unknown,
    a refusal, or a shape this does not know)."""
    try:
        return sum(1 for item in answer if isinstance(item[1], int) and not isinstance(item[1], bool)
                   and 200 <= item[1] < 300)
    except Exception:                               # noqa: BLE001 - an answer this cannot read delivered nothing
        return 0


class OpenPings:
    """The open-ping file at ``path`` (its directory must exist). ``queue_dir`` is the push queue the pings go to;
    ``sender`` and ``clock`` are injectable (a fake of each makes a night a test); ``ceiling`` is the default number of
    pushes before a ping gives up, kept with each ping so a restart under another default does not move it."""

    def __init__(self, path: Path | str, *, queue_dir: Path | str, sender: Callable[..., Any] | None = None,
                 clock: Callable[[], datetime] = _utc_now, ceiling: int = CEILING) -> None:
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("the ceiling is a whole number of pushes, at least 1")
        self.path = Path(path)
        self.queue_dir = Path(queue_dir)
        if sender is None:
            from levain.webpush import sender as push_sender
            sender = push_sender.send
        self.sender = sender
        self.clock = clock
        self.ceiling = ceiling
        with write_transaction(self._connect) as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        return open_durable(self.path, needs="the open-ping record")

    # --- writing ---------------------------------------------------------------------------------------------
    def open(self, ref: str, *, priority: str, title: str, body: str, ceiling: int | None = None) -> int:
        """Open the ping for ``ref`` (a source's :func:`levain.webpush.notify.notice_ref`) and push it now; returns its
        id. One ping per ref is open at a time: opening an open ref again is that ping, and pushes nothing. A priority
        outside :data:`levain.webpush.notify.PRIORITIES` is a ValueError and a title or body the notice sanitiser refuses
        is :class:`levain.webpush.notify.NotifyRefused`; a ref that is not a non-empty str is a ValueError too; nothing is
        written for any of them. Opening an open ref again with a higher priority or a new title or body raises the
        priority (never lowers it), takes the new words and makes the ping due now; the same words change nothing."""
        if not isinstance(ref, str) or not ref:
            raise ValueError("a ping's ref is a non-empty string")
        notify.kind_for(priority)
        title, body = notify.clean_title(title), notify.clean_body(body)
        limit = self.ceiling if ceiling is None else ceiling
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("the ceiling is a whole number of pushes, at least 1")
        now = utc_stamp(self.clock())
        with write_transaction(self._connect) as conn:
            held = conn.execute("SELECT ping_id, priority, title, body FROM open_pings WHERE ref = ? AND state = 'open'",
                                (ref,)).fetchone()
            if held:
                top = max(priority, held[1], key=notify.PRIORITIES.index)
                if (top, title, body) == tuple(held[1:]):
                    return int(held[0])
                conn.execute("UPDATE open_pings SET priority = ?, title = ?, body = ?, next_due_at = ? "
                             "WHERE ping_id = ?", (top, title, body, now, held[0]))
                ping_id = held[0]
            else:
                ping_id = conn.execute(
                    "INSERT INTO open_pings (ref, priority, title, body, ceiling, state, pushes, opened_at, next_due_at) "
                    "VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?)", (ref, priority, title, body, limit, now, now)).lastrowid
        self.tick()
        return int(ping_id)

    def ack(self, ref: str, source: str) -> bool:
        """Close the open ping for ``ref`` on ``source`` (:data:`ACK_SOURCES`). True when it closed; False when ``source``
        is a receipt (``shown``, ``received``: not evidence he saw it), or no ping is open for ``ref``."""
        if source in _RECEIPTS:
            return False
        if source not in ACK_SOURCES:
            raise ValueError(f"an ack source is one of {ACK_SOURCES}, not {source!r}")
        with write_transaction(self._connect) as conn:
            return conn.execute("UPDATE open_pings SET state = 'acked', closed_at = ?, ack_source = ? "
                                "WHERE ref = ? AND state = 'open'", (utc_stamp(self.clock()), source, ref)).rowcount == 1

    def _still_open(self, ping_id: int) -> bool:
        with read_transaction(self._connect) as conn:
            return conn.execute("SELECT 1 FROM open_pings WHERE ping_id = ? AND state = 'open'",
                                (ping_id,)).fetchone() is not None

    def _claim(self, ping_id: int) -> tuple[Any, ...] | None:
        """In one write transaction: re-read ``ping_id`` and, when it is still open and due, either write its band row
        (its ceiling's pushes are made; returns None) or count the next push and return the row to send."""
        at = self.clock()
        stamp = utc_stamp(at)
        with write_transaction(self._connect) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM open_pings WHERE ping_id = ? AND state = 'open' "
                               "AND next_due_at <= ?", (ping_id, stamp)).fetchone()
            if row is None:
                return None
            pushes, ceiling, opened, delivered = row[7], row[5], row[8], row[13]
            if pushes >= ceiling:
                kind, text = ("did_not_wake", _WAKE_TEXT) if delivered else ("unreachable", _UNREACHABLE_TEXT)
                conn.execute("UPDATE open_pings SET state = 'did_not_wake', closed_at = ? WHERE ping_id = ?",
                             (stamp, ping_id))
                conn.execute("INSERT INTO ping_band_rows (ping_id, kind, ref, pushes, delivered, text, opened_at, at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (ping_id, kind, row[1], pushes, delivered, text.format(pushes=pushes), opened, stamp))
                return None
            wait = BACKOFF[min(pushes, len(BACKOFF) - 1)]
            conn.execute("UPDATE open_pings SET pushes = pushes + 1, last_push_at = ?, next_due_at = ? "
                         "WHERE ping_id = ?", (stamp, utc_stamp(at + wait), ping_id))
            return row

    def tick(self) -> int:
        """Run what is due: push each open ping whose time has come, and write the band row (``did_not_wake``, or
        ``unreachable`` when no push of it was delivered) of each that is due with its ceiling's pushes already made.
        Returns the number of pushes tried. A pass tries at most :data:`MAX_PER_PASS` pushes and starts no new one past
        :data:`PASS_WALL` seconds; the rest stay due for the next pass. Each row is claimed just before its own send,
        and re-read open (by its ping id) right before it: a ping he tapped meanwhile is skipped. That window cannot be
        fully closed, the push is outside any transaction: a tap landing after the re-read still gets that one push.
        Meant to be called on a timer (the production caller is levain#16's); safe from a fresh process."""
        with read_transaction(self._connect) as conn:
            due = [r[0] for r in conn.execute("SELECT ping_id FROM open_pings WHERE state = 'open' AND next_due_at <= ? "
                                              "ORDER BY ping_id", (utc_stamp(self.clock()),)).fetchall()]
        deadline = time.monotonic() + PASS_WALL
        sent = tried = 0
        for ping_id in due:
            if sent and (sent >= MAX_PER_PASS or time.monotonic() >= deadline):
                break
            row = self._claim(ping_id)
            if row is None:
                continue
            sent += 1                               # claimed: counts toward the bound even when the send is skipped
            if not self._still_open(ping_id):
                continue
            tried += 1
            error, delivered = None, 0
            try:
                delivered = _delivered(self.sender(self.queue_dir, kind=notify.kind_for(row[2]), ref=row[1], title=row[3],
                                                   body=row[4], url=notify.NOTICES_URL))
            except Exception as exc:                # noqa: BLE001 - a push that fails is a counted attempt, never a raise
                error = f"{type(exc).__name__}: {exc}"[:_ERROR_CHARS]
            with write_transaction(self._connect) as conn:
                conn.execute("UPDATE open_pings SET delivered = delivered + ?, last_error = ? WHERE ping_id = ?",
                             (1 if delivered else 0, error, ping_id))
        return tried

    # --- reading ---------------------------------------------------------------------------------------------
    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        return dict(zip(_KEYS, r))

    def ping(self, ref: str) -> dict[str, Any] | None:
        """The latest ping opened for ``ref`` (any state), or None."""
        with read_transaction(self._connect) as conn:
            r = conn.execute(f"SELECT {_COLUMNS} FROM open_pings WHERE ref = ? ORDER BY ping_id DESC LIMIT 1",
                             (ref,)).fetchone()
        return self._row(r) if r else None

    def open_pings(self) -> list[dict[str, Any]]:
        """The pings still open, oldest first, read-only."""
        with read_transaction(self._connect) as conn:
            rows = conn.execute(f"SELECT {_COLUMNS} FROM open_pings WHERE state = 'open' ORDER BY ping_id").fetchall()
        return [self._row(r) for r in rows]

    def band_rows(self) -> list[dict[str, Any]]:
        """The band rows written (``did_not_wake``, or ``unreachable`` for a ping no push of which was delivered), oldest
        first: the morning's caller (levain#16) takes them."""
        with read_transaction(self._connect) as conn:
            rows = conn.execute("SELECT ping_id, kind, ref, pushes, delivered, text, opened_at, at FROM ping_band_rows "
                                "ORDER BY row_id").fetchall()
        return [dict(zip(("ping_id", "kind", "ref", "pushes", "delivered", "text", "opened_at", "at"), r)) for r in rows]
