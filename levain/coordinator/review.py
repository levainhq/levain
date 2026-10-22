"""levain.coordinator.review: the review queue and the push records (C4 backend; C0 §3.3 and §3.6).

What it holds, in the coordinator's own store (``dispatch.db``):

- **The review queue** (§3.6): ``review_items`` and ``review_events``. An item is something that waits for a look and
  never pushes (the grammar's KNOW, *"review queue, never a push"*). Its state is derived: open until a ``cleared``
  event, open again after a ``reopened`` one. Tables are append-only like the rest of the store.
- **Three review modes** (RULED, *"also on review modes"*): ``manual`` (you), ``fresh`` (a fresh session you asked,
  recorded as you) and ``coordinator`` (the actor reviewing itself). An item marked ``authority_relevant`` can never be
  cleared in ``coordinator`` mode, and the SQL refuses it, so no caller has to remember.
- **The push records**: a SIGN reply points at a request in ``queue.db``, an ANSWER reply at a question in
  ``questions.db``. Writing one is what a delivery layer does when it files the request; whether it is still WAITING is
  read from the queue's own state (:func:`waiting_on_you`), never stored here ("waiting on you" is derived).
- **The feeds**, each reading data already on disk and filing an item once per ``dedupe_key`` (a feed run twice files
  nothing the second time, and a cleared item is not re-filed by the condition that made it): a drop with no split
  receipt, a launch that died, an envelope near its stop, a Q13 End, the unseen digest (Q15), every KNOW, and the
  coordinator's own flagged doubts and contradictions.

NOT here: any panel (cockpit and phone belong to the K lanes), the push itself (the notification layer), and the thread
budget gauge (C6). The envelope feed reads ``stop_at`` because the meter is C6's.

NOTHING HERE IS AUTHORITY: an item or a reply is a record that something waits, never a decision.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator.store import ACTORS, CoordError, CoordStore, _append_only, _in

__all__ = ["FEEDS", "MODES", "NEAR_STOP", "NO_SPLIT_AFTER", "REVIEW_SCHEMA", "Item", "clear_item", "collect_feeds",
           "feed_dead_seats", "feed_digest", "feed_end", "feed_envelopes", "feed_no_split", "file_item", "file_know",
           "get_item", "list_items", "push_answer", "push_sign", "reopen_item", "waiting_on_you"]

FEEDS = ("receipt", "seat", "envelope", "end", "digest", "know", "doubt", "contradiction")
MODES = ("manual", "fresh", "coordinator")
# C0 §3.3 [seat: N = 10]: a drop with no split receipt after this long is a review item
NO_SPLIT_AFTER = timedelta(minutes=10)
# [judged by c4, 1022+18, against nothing measured]: an envelope whose stop is this near is "near its limit"
NEAR_STOP = timedelta(minutes=30)
# a launch your own take or release voided is not a seat that died (the lease's death rule, lease.launch_death, covers the
# rest: a launch asked for under a lease its asker lost, one nobody tried to start)
_NOT_A_DEATH = ("takeover", "operator release")
_KEY_MAX = 300
_TEXT_MAX = 4000
# a free-text field a feed copies out of another store is cut to this, so one long value cannot sink its row
_FIELD_MAX = 500
# the dead-seat scan reads terminal launches that have no item yet, oldest first from its watermark, a page at a time, at
# most this many pages a run: the watermark moves only past launches evaluated, so what is left is the next run's
_PAGE = 100
_MAX_PAGES = 20
_PUSH_KINDS = ("sign", "answer")
_REF_STORE = {"sign": "queue.db", "answer": "questions.db"}

REVIEW_SCHEMA: tuple[str, ...] = (
    # dedupe_key is what makes a feed idempotent; by is who filed it (a feed is the service, a KNOW the coordinator)
    "CREATE TABLE IF NOT EXISTS review_items (item_id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(thread_id), "
    f"feed TEXT NOT NULL CHECK ({_in('feed', FEEDS)}), dedupe_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, "
    "authority_relevant INTEGER NOT NULL CHECK (authority_relevant IN (0, 1)), "
    f"by TEXT NOT NULL CHECK ({_in('by', ACTORS)}), at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS review_events (item_id TEXT NOT NULL REFERENCES review_items(item_id), "
    f"seq INTEGER NOT NULL, op TEXT NOT NULL CHECK (op IN ('cleared', 'reopened')), "
    f"mode TEXT NOT NULL CHECK ({_in('mode', MODES)}), by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator')), "
    "at TEXT NOT NULL, PRIMARY KEY (item_id, seq))",
    # a feed's watermark (the dead-seat scan): append-only rows, the latest is the mark, and it never moves back
    "CREATE TABLE IF NOT EXISTS review_marks (feed TEXT NOT NULL, seq INTEGER NOT NULL, value INTEGER NOT NULL, "
    "at TEXT NOT NULL, PRIMARY KEY (feed, seq))",
    *_append_only("review_marks", "a review mark"),
    "CREATE TRIGGER IF NOT EXISTS review_marks_forward BEFORE INSERT ON review_marks WHEN NEW.seq != COALESCE((SELECT "
    "MAX(seq) FROM review_marks WHERE feed = NEW.feed), 0) + 1 OR NEW.value < COALESCE((SELECT MAX(value) FROM "
    "review_marks WHERE feed = NEW.feed), 0) BEGIN SELECT RAISE(ABORT, 'a review mark is the next one and moves forward'); END",
    *_append_only("review_items", "a review item"),
    *_append_only("review_events", "a review event"),
    "CREATE TRIGGER IF NOT EXISTS review_items_no_replace BEFORE INSERT ON review_items WHEN EXISTS (SELECT 1 FROM "
    "review_items WHERE item_id = NEW.item_id OR dedupe_key = NEW.dedupe_key OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a review item is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS review_events_next BEFORE INSERT ON review_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM review_events WHERE item_id = NEW.item_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM review_events WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a review event is the next seq of its item'); END",
    # an item is cleared only while open and reopened only while cleared
    "CREATE TRIGGER IF NOT EXISTS review_events_alternate BEFORE INSERT ON review_events "
    "WHEN NEW.op = COALESCE((SELECT op FROM review_events WHERE item_id = NEW.item_id ORDER BY seq DESC LIMIT 1), "
    "'reopened') BEGIN SELECT RAISE(ABORT, 'a review item alternates cleared and reopened'); END",
    # the mode names who reviewed: the coordinator reviews only in its own mode, and you never do
    "CREATE TRIGGER IF NOT EXISTS review_events_mode BEFORE INSERT ON review_events "
    "WHEN (NEW.mode = 'coordinator') != (NEW.by = 'coordinator') "
    "BEGIN SELECT RAISE(ABORT, 'the coordinator mode is the coordinator''s and nobody else''s'); END",
    # C0 §3.6: an authority-relevant item is never cleared by the actor reviewing itself
    "CREATE TRIGGER IF NOT EXISTS review_events_authority BEFORE INSERT ON review_events "
    "WHEN NEW.op = 'cleared' AND NEW.by = 'coordinator' AND (SELECT authority_relevant FROM review_items WHERE "
    "item_id = NEW.item_id) = 1 "
    "BEGIN SELECT RAISE(ABORT, 'an authority-relevant item is not cleared in coordinator mode'); END",
)


@dataclass(frozen=True)
class Item:
    item_id: str
    thread_id: str | None
    feed: str
    body: dict[str, Any]
    authority_relevant: bool
    by: str
    at: str
    state: str          # open or cleared, derived from the latest event
    last: tuple[str, str, str, str] | None   # (op, mode, by, at) of the latest event


def _text(name: str, value: Any, high: int = _TEXT_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CoordError(f"{name} must be 1 to {high} characters of text")
    return value


def _item(conn: sqlite3.Connection, row: tuple[Any, ...]) -> Item:
    last = conn.execute("SELECT op, mode, by, at FROM review_events WHERE item_id = ? ORDER BY seq DESC LIMIT 1",
                        (row[0],)).fetchone()
    return Item(row[0], row[1], row[2], json.loads(row[4]), bool(row[5]), row[6], row[7],
                "cleared" if last is not None and last[0] == "cleared" else "open",
                None if last is None else tuple(last))


_COLS = "item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at"


def _insert(conn: sqlite3.Connection, now: str, feed: str, body: Mapping[str, Any], *, dedupe_key: str,
            thread_id: str | None = None, authority_relevant: bool = False, by: str = "service") -> tuple[Item, bool]:
    """The one insert, inside a write transaction the caller holds: the stored item and ``False`` when the key is
    taken, else the new item and ``True``. Nothing here opens a transaction, so a feed can revalidate and insert in one."""
    if feed not in FEEDS:
        raise CoordError(f"a review feed is one of {', '.join(FEEDS)}")
    if by not in ACTORS:
        raise CoordError(f"an item is filed by one of {', '.join(ACTORS)}")
    key = _text("a dedupe key", dedupe_key, _KEY_MAX)
    text = canonical(dict(body))
    if len(text) > 2 * _TEXT_MAX:
        raise CoordError("an item's body is too long")
    row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE dedupe_key = ?", (key,)).fetchone()
    if row is not None:
        return _item(conn, row), False
    if thread_id is not None and conn.execute("SELECT 1 FROM threads WHERE thread_id = ?",
                                              (thread_id,)).fetchone() is None:
        raise CoordError(f"no thread {str(thread_id)[:40]}")
    item_id = "rv-" + secrets.token_hex(16)
    conn.execute("INSERT INTO review_items (item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (item_id, thread_id, feed, key, text, int(authority_relevant), by, now))
    row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (item_id,)).fetchone()
    return _item(conn, row), True


def file_item(store: CoordStore, feed: str, body: Mapping[str, Any], *, dedupe_key: str,
              thread_id: str | None = None, authority_relevant: bool = False, by: str = "service",
              token: object = None) -> tuple[Item, bool]:
    """File one review item, once per ``dedupe_key``: a second call with the same key returns the stored item and
    ``False``, and writes nothing (so a feed run again, or a cleared item's condition still holding, files nothing)."""
    with store._write(by, token) as (conn, now):
        return _insert(conn, now, feed, body, dedupe_key=dedupe_key, thread_id=thread_id,
                       authority_relevant=authority_relevant, by=by)


def _file_checked(store: CoordStore, feed: str, body: Mapping[str, Any], *, dedupe_key: str, thread_id: str | None = None,
                  authority_relevant: bool = False, still: Callable[[sqlite3.Connection, str], bool]) -> Item | None:
    """File a feed's item only if ``still(conn, now)`` holds INSIDE the write transaction that inserts it: a feed's scan
    is a read, and the world can move between that read and this write. ``None`` when it no longer holds."""
    with store._write() as (conn, now):
        if not still(conn, now):
            return None
        return _insert(conn, now, feed, body, dedupe_key=dedupe_key, thread_id=thread_id,
                       authority_relevant=authority_relevant)[0]


def get_item(store: CoordStore, item_id: str) -> Item:
    with read_transaction(store._connect) as conn:
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise CoordError(f"no review item {str(item_id)[:40]}")
        return _item(conn, row)


def list_items(store: CoordStore, *, include_cleared: bool = False, feed: str | None = None,
               thread_id: str | None = None) -> list[Item]:
    """The queue, oldest first. Open items only unless ``include_cleared``."""
    with read_transaction(store._connect) as conn:
        rows = conn.execute(f"SELECT {_COLS} FROM review_items ORDER BY rowid").fetchall()
        items = [_item(conn, r) for r in rows]
    return [i for i in items if (include_cleared or i.state == "open") and (feed is None or i.feed == feed)
            and (thread_id is None or i.thread_id == thread_id)]


def _review(store: CoordStore, item_id: str, op: str, mode: str, token: object) -> Item:
    if mode not in MODES:
        raise CoordError(f"a review mode is one of {', '.join(MODES)}")
    by = "coordinator" if mode == "coordinator" else "operator"
    with store._write(by, token) as (conn, now):
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise CoordError(f"no review item {str(item_id)[:40]}")
        state = _item(conn, row).state
        if op == "cleared" and state != "open" or op == "reopened" and state != "cleared":
            raise CoordError(f"item {item_id} is already {state}")
        if op == "cleared" and by == "coordinator" and row[5]:
            raise CoordError("an authority-relevant item is cleared by you or a fresh session you asked, never by "
                             "the coordinator reviewing itself")
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events WHERE item_id = ?",
                           (item_id,)).fetchone()[0]
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, ?, ?, ?, ?, ?)",
                     (item_id, seq, op, mode, by, now))
        return _item(conn, row)      # read after the insert: the state it now has


def clear_item(store: CoordStore, item_id: str, *, mode: str = "manual", token: object = None) -> Item:
    """Clear an open item in ``mode``. ``coordinator`` mode writes as the coordinator, so it needs the lease token, and
    it is refused for an authority-relevant item."""
    return _review(store, item_id, "cleared", mode, token)


def reopen_item(store: CoordStore, item_id: str, *, mode: str = "manual", token: object = None) -> Item:
    return _review(store, item_id, "reopened", mode, token)


# --- the push records (C0 §3.3) ----------------------------------------------------------------------------------
def _push(store: CoordStore, kind: str, thread_id: str | None, ref_id: str, text: str, extra: Mapping[str, Any],
          by: str, token: object, still_open: Callable[[str], bool | None] | None) -> tuple[str, bool]:
    if kind not in _PUSH_KINDS:
        raise CoordError("a push record is a sign or an answer")
    if by not in ACTORS:
        raise CoordError(f"a record is written by one of {', '.join(ACTORS)}")
    ref = _text("a reference", ref_id, 200)
    body = {"text": _text("the text", text), **extra}
    onto = extra.get("onto")
    land = kind == "sign" and onto is not None and thread_id is not None
    closed: dict[str, bool | None] = {}
    if land:
        # C0 §3.3: one land SIGN is open per (thread, onto). The queue reads happen HERE, before the dispatch.db write
        # transaction opens, and are re-checked inside it; a reader is required whenever such a SIGN exists
        with read_transaction(store._connect) as conn:
            if conn.execute("SELECT 1 FROM replies WHERE kind = ? AND ref_store = ? AND ref_id = ?",
                            (kind, _REF_STORE[kind], ref)).fetchone() is not None:
                land = False
            else:
                for old_ref in _land_refs(conn, thread_id, onto):
                    if still_open is None:
                        raise CoordError(f"thread {thread_id} has a land SIGN onto {onto} ({old_ref}); a state reader "
                                         "(still_open) is required to file another")
                    closed[old_ref] = still_open(old_ref)
    with store._write(by, token) as (conn, now):
        if thread_id is not None and conn.execute("SELECT 1 FROM threads WHERE thread_id = ?",
                                                  (thread_id,)).fetchone() is None:
            raise CoordError(f"no thread {str(thread_id)[:40]}")
        same = conn.execute("SELECT reply_id FROM replies WHERE kind = ? AND ref_store = ? AND ref_id = ?",
                            (kind, _REF_STORE[kind], ref)).fetchone()
        if same is not None:
            return same[0], False
        if land:
            for old_ref in _land_refs(conn, thread_id, onto):
                if closed.get(old_ref, None) is not False:      # open, unreadable, or filed since the read
                    raise CoordError(f"thread {thread_id} already has an open land SIGN onto {onto} ({old_ref}); "
                                     "withdraw it before filing another")
        reply_id = store._reply(conn, thread_id, kind, _REF_STORE[kind], ref, body, now)
        return reply_id, True


def _land_refs(conn: sqlite3.Connection, thread_id: str | None, onto: object) -> list[str]:
    return [old_ref for old_ref, old_body in conn.execute(
        "SELECT ref_id, body FROM replies WHERE kind = 'sign' AND thread_id = ? ORDER BY rowid",
        (thread_id,)).fetchall() if json.loads(old_body).get("onto") == onto]


def push_sign(store: CoordStore, thread_id: str | None, request_id: str, text: str, *, onto: str | None = None,
              by: str = "service", token: object = None,
              still_open: Callable[[str], bool | None] | None = None) -> tuple[str, bool]:
    """Record a SIGN: a pointer to the consent-queue request ``request_id``. ``(reply_id, created)``; the same request
    again returns the first record. With ``onto`` and a ``thread_id``, a second land SIGN for the same (thread, onto) needs
    ``still_open`` (a request id -> open?) and is refused unless it says an explicit ``False`` for every earlier one."""
    extra: dict[str, Any] = {} if onto is None else {"onto": _text("onto", onto, 200)}
    return _push(store, "sign", thread_id, request_id, text, extra, by, token, still_open)


def push_answer(store: CoordStore, thread_id: str | None, question_id: str, text: str, *, by: str = "service",
                token: object = None) -> tuple[str, bool]:
    """Record an ANSWER: a pointer to the question ``question_id`` in ``questions.db``. It authorises nothing."""
    return _push(store, "answer", thread_id, question_id, text, {}, by, token, None)


def file_know(store: CoordStore, thread_id: str | None, text: str, *, by: str = "coordinator",
              token: object = None, authority_relevant: bool = False) -> tuple[Item, str]:
    """A KNOW: a reply of kind ``know`` and its review item, in ONE transaction (never a push). Returns the item and
    the reply id. ``by`` labels it: the coordinator's own KNOW is written with its lease token."""
    if by not in ACTORS:
        raise CoordError(f"a KNOW is written by one of {', '.join(ACTORS)}")
    body = {"text": _text("the text", text)}
    with store._write(by, token) as (conn, now):
        if thread_id is not None and conn.execute("SELECT 1 FROM threads WHERE thread_id = ?",
                                                  (thread_id,)).fetchone() is None:
            raise CoordError(f"no thread {str(thread_id)[:40]}")
        reply_id = store._reply(conn, thread_id, "know", "review", "", {**body, "by": by}, now)
        item_id = "rv-" + secrets.token_hex(16)
        conn.execute("INSERT INTO review_items (item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at) "
                     "VALUES (?, ?, 'know', ?, ?, ?, ?, ?)", (item_id, thread_id, "know:" + reply_id,
                                                              canonical({**body, "reply_id": reply_id}),
                                                              int(authority_relevant), by, now))
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (item_id,)).fetchone()
        return _item(conn, row), reply_id


# --- waiting on you (derived, never stored; C0 §2) ---------------------------------------------------------------
def queue_request_open(queue_dir: Path | str, *, clock: Callable[[], datetime] | None = None
                       ) -> Callable[[str], bool | None]:
    """A request id -> open? read from ``queue.db`` with ``mode=ro``: open while ``undecided`` or ``pending`` and not past
    the queue's own expiry rule (:meth:`ConsentQueue._expiry_due`, the one the queue's read and sweep use: a hold's end
    plus ``UNDECIDED_GRACE``), closed in any other state, ``None`` when the store or the row cannot be read. A raw read:
    this is information, and the seal on a row is the queue's own business."""
    from levain.consent.queue import ConsentQueue

    path = Path(queue_dir) / "queue.db"
    now = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def read(request_id: str) -> bool | None:
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        except (OSError, sqlite3.Error):
            return None
        try:
            row = conn.execute("SELECT state, expires_at FROM requests WHERE id = ?", (request_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        if row is None:
            return None
        if row[0] == "pending" and ConsentQueue._expiry_due(row[1], utc_stamp(now())):
            return False
        return row[0] in ("pending", "undecided")
    return read


def question_open(queue_dir: Path | str) -> Callable[[str], bool | None]:
    """A question id -> open? read through the band's own read-only path: open while it has no answer, ``None`` when
    there is no such question or the store cannot be read (the record is then ``unresolved``, never dropped)."""
    from levain.consent.questions import QuestionStore

    qs = QuestionStore(Path(queue_dir))

    def read(question_id: str) -> bool | None:
        try:
            q = qs.get(question_id)
        except Exception:  # noqa: BLE001 - a bad store is an unreadable record, not a crashed view
            return None
        return None if q is None else q.answer is None
    return read


def waiting_on_you(store: CoordStore, *, sign_open: Callable[[str], bool | None],
                   answer_open: Callable[[str], bool | None]) -> dict[str, Any]:
    """What waits on you, derived from the open SIGN and ANSWER records: ``{"threads": {thread_id: [records]},
    "loose": [records with no thread], "unresolved": [records whose state could not be read]}``. A record is
    ``{"kind", "ref_id", "reply_id", "text"}``. A thread with no open record is absent."""
    with read_transaction(store._connect) as conn:
        rows = conn.execute("SELECT reply_id, thread_id, kind, ref_id, body FROM replies WHERE kind IN "
                            "('sign', 'answer') ORDER BY rowid").fetchall()
    out: dict[str, Any] = {"threads": {}, "loose": [], "unresolved": []}
    for reply_id, thread_id, kind, ref_id, body in rows:
        state = (sign_open if kind == "sign" else answer_open)(ref_id)
        rec = {"kind": kind, "ref_id": ref_id, "reply_id": reply_id, "text": json.loads(body).get("text", "")}
        if state is None:
            out["unresolved"].append(rec)
        elif state:
            (out["threads"].setdefault(thread_id, []) if thread_id else out["loose"]).append(rec)
    return out


# --- the feeds ---------------------------------------------------------------------------------------------------
# Every feed takes ``errors``: a list that collects one line per row it could not file (the feed goes on with the rest).
# With no list, the rows that could be filed are filed and a CoordError naming the rest is raised AFTER them.
def _now(store: CoordStore) -> datetime:
    return parse_stamp(store._stamp())


def _clip(value: Any) -> str:
    return str(value)[:_FIELD_MAX]


def _each(name: str, rows: list[Any], make: Callable[[Any], Item | None], errors: list[str] | None) -> list[Item]:
    out: list[Item] = []
    failed: list[str] = []
    for row in rows:
        try:
            item = make(row)
        except Exception as exc:  # noqa: BLE001 - recorded under the feed's name below, never swallowed
            failed.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
            continue
        if item is not None:
            out.append(item)
    _report(errors, failed)
    return out


def _report(errors: list[str] | None, failed: list[str]) -> None:
    if errors is not None:
        errors.extend(failed)
    elif failed:
        raise CoordError("; ".join(failed)[:600])


def _drop_stuck(conn: sqlite3.Connection, drop_id: str, now: datetime, after: timedelta) -> bool:
    """No ``split`` receipt for the drop and ``after`` has passed since it was stored: the one test, used by the scan
    and again inside the insert's transaction."""
    row = conn.execute("SELECT received_at FROM drops WHERE drop_id = ?", (drop_id,)).fetchone()
    if row is None or now - parse_stamp(row[0]) < after:
        return False
    return "split" not in {json.loads(b).get("stage") for (b,) in conn.execute(
        "SELECT body FROM replies WHERE kind = 'drop_receipt' AND ref_id = ?", (drop_id,)).fetchall()}


def _stuck_drops(store: CoordStore, after: timedelta) -> list[tuple[str, str]]:
    now = _now(store)
    with read_transaction(store._connect) as conn:
        return [(d, r) for d, r in conn.execute("SELECT drop_id, received_at FROM drops ORDER BY rowid").fetchall()
                if _drop_stuck(conn, d, now, after)]


def feed_no_split(store: CoordStore, *, after: timedelta = NO_SPLIT_AFTER, errors: list[str] | None = None
                  ) -> list[Item]:
    """A drop the store accepted that has no ``split`` receipt after ``after`` (C0 §3.3). The test is run again inside
    the transaction that files the item, so a split that lands after the scan files nothing."""
    def make(row: tuple[str, str]) -> Item | None:
        d, r = row
        return _file_checked(store, "receipt", {"drop_id": d, "received_at": r,
                                                "text": f"drop {d} was stored at {r} and has no split receipt"},
                             dedupe_key=f"receipt:{d}",
                             still=lambda conn, now: _drop_stuck(conn, d, parse_stamp(now), after))
    return _each("receipt", _stuck_drops(store, after), make, errors)


def _mark(conn: sqlite3.Connection, feed: str) -> int:
    row = conn.execute("SELECT value FROM review_marks WHERE feed = ? ORDER BY seq DESC LIMIT 1", (feed,)).fetchone()
    return 0 if row is None else row[0]


_TERMINAL = "('failed', 'expired', 'ended')"
_LAST_KIND = "(SELECT kind FROM launch_events e WHERE e.launch_id = l.launch_id ORDER BY e.seq DESC LIMIT 1)"


def _lazy_expiry(store: CoordStore) -> None:
    """The lazy expiry the read path (coord_status) runs before it reads. A write transaction is opened only when
    something is past its time, so a run with nothing to change writes nothing and wakes nothing."""
    from levain.coordinator import lease

    with read_transaction(store._connect) as conn:
        now = store._stamp()
        held = lease.lease(conn, now)
        due = (held is not None and not held.live) or any(now >= ln.until for ln in lease._in_state(conn, "pending"))
    if due:
        with store._write() as (conn, now):
            lease._lapse_if_past(conn, now)
            lease._expire_pending(conn, now)


def _dead_launches(store: CoordStore) -> tuple[list[tuple[int, Any]], int]:
    """``(the dead launches without an item as (gseq, launch), the watermark this scan reached)``. The watermark W means
    every terminal launch with gseq < W was evaluated: the scan runs oldest first from the stored W, at most
    ``_MAX_PAGES`` pages, and the new W is the last gseq evaluated, never past the lowest launch that is not terminal
    (it can still die). A launch is read once; what the page bound leaves is the next run's."""
    from levain.coordinator import lease

    _lazy_expiry(store)
    found: list[tuple[int, Any]] = []
    with read_transaction(store._connect) as conn:
        start = cursor = _mark(conn, "seat")
        reached = None
        for _page in range(_MAX_PAGES):
            ids = conn.execute(
                f"SELECT l.launch_id, l.gseq FROM launches l WHERE l.gseq >= ? AND {_LAST_KIND} IN {_TERMINAL} "
                "AND NOT EXISTS (SELECT 1 FROM review_items r WHERE r.dedupe_key = 'seat:' || l.launch_id) "
                "ORDER BY l.gseq ASC LIMIT ?", (cursor, _PAGE)).fetchall()
            for launch_id, gseq in ids:
                ln = lease.launch(conn, launch_id)
                if ln is not None and lease.launch_death(conn, ln) is True and ln.cause not in _NOT_A_DEATH:
                    found.append((gseq, ln))
            if len(ids) < _PAGE:
                top = conn.execute("SELECT MAX(gseq) FROM launches").fetchone()[0]
                reached = start if top is None else top + 1
                break
            cursor = reached = ids[-1][1] + 1
        low = conn.execute(f"SELECT MIN(l.gseq) FROM launches l WHERE COALESCE({_LAST_KIND}, '') NOT IN {_TERMINAL}"
                           ).fetchone()[0]
    mark = start if reached is None else reached
    return found, mark if low is None else min(mark, low)


def feed_dead_seats(store: CoordStore, *, errors: list[str] | None = None) -> list[Item]:
    """A launch that died, by the lease's own rule (:func:`levain.coordinator.lease.launch_death`: failed or expired
    after a spawn was claimed, ended at ``0 turns``, a coordinator that lost the lease without ever writing). One that
    carried an envelope-bound thread is authority-relevant. The scan keeps a persisted watermark (``_dead_launches``):
    it advances past a launch only once its item is filed, so a row that could not be filed is tried again."""
    found, mark = _dead_launches(store)
    failed_at: list[int] = []

    def make(row: tuple[int, Any]) -> Item:
        gseq, ln = row
        one = ln.threads[0] if len(ln.threads) == 1 else None
        try:
            return file_item(store, "seat", {
                "launch_id": ln.launch_id, "harness": _clip(ln.harness), "role": _clip(ln.role), "state": ln.state,
                "cause": _clip(ln.cause), "at": ln.at, "threads": list(ln.threads),
                "text": _clip(f"{ln.role} launch {ln.launch_id} ({ln.harness}) {ln.state}: {ln.cause}")},
                dedupe_key=f"seat:{ln.launch_id}", thread_id=one,
                authority_relevant=ln.pinned_envelope_id is not None)[0]
        except Exception:
            failed_at.append(gseq)
            raise
    try:
        items = _each("seat", found, make, errors)
    finally:
        _advance(store, "seat", min([mark, *failed_at]))
    return items


def _advance(store: CoordStore, feed: str, value: int) -> None:
    """Move a feed's watermark forward to ``value``; a mark that would not move writes nothing."""
    with read_transaction(store._connect) as conn:
        if value <= _mark(conn, feed):
            return
    with store._write() as (conn, now):
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_marks WHERE feed = ?", (feed,)).fetchone()[0]
        if value > _mark(conn, feed):
            conn.execute("INSERT INTO review_marks (feed, seq, value, at) VALUES (?, ?, ?, ?)", (feed, seq, value, now))


def _envelope_stop(conn: sqlite3.Connection, thread_id: str, envelope_id: str, errors: list[str] | None = None
                   ) -> datetime | None:
    """The effective stop of the envelope that is the thread's head, or ``None`` when it is no longer the head or was
    killed or its thread is done. A ``stop_earlier`` row with a bad stamp is skipped and named in ``errors``."""
    head = conn.execute("SELECT b.envelope_id, e.stop_at FROM envelope_binds b JOIN envelopes e ON e.envelope_id = "
                        "b.envelope_id WHERE b.thread_id = ? ORDER BY b.gseq DESC LIMIT 1", (thread_id,)).fetchone()
    if head is None or head[0] != envelope_id:
        return None
    rows = conn.execute("SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND "
                        "(thread_id IS NULL OR thread_id IS ?)", (envelope_id, thread_id)).fetchall()
    if any(k in ("kill", "thread_done") for k, _v in rows):
        return None
    stops = [parse_stamp(head[1])]
    for k, v in rows:
        if k != "stop_earlier":
            continue
        try:
            stops.append(parse_stamp(v))
        except Exception as exc:  # noqa: BLE001 - this row is skipped and named, the rest still count
            if errors is not None:
                errors.append(f"envelope {envelope_id}: stop_earlier {str(v)[:40]!r} skipped: {exc}"[:300])
    return min(stops)


def feed_envelopes(store: CoordStore, *, near: timedelta = NEAR_STOP, errors: list[str] | None = None) -> list[Item]:
    """A live envelope whose effective stop is within ``near`` of now and still ahead, or has already passed with no
    item filed for that stop (a lapse is never silent; the key holds a repeat). Live: bound as a thread's head, not
    killed and its thread not done. The stop is the signed one or the earliest ``stop_earlier``. Authority-relevant."""
    now = _now(store)
    bad: list[str] = []
    with read_transaction(store._connect) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelope_binds'").fetchone() is None:
            return []
        due = []
        heads = conn.execute("SELECT thread_id, envelope_id FROM envelope_binds b WHERE b.gseq = (SELECT MAX(gseq) "
                             "FROM envelope_binds WHERE thread_id = b.thread_id)").fetchall()
        for thread_id, envelope_id in heads:
            stop = _envelope_stop(conn, thread_id, envelope_id, bad)
            if stop is None or stop > now + near:
                continue
            due.append((thread_id, envelope_id, stop))

    def make(row: tuple[str, str, datetime]) -> Item | None:
        t, e, s = row

        def still(conn: sqlite3.Connection, stamp: str) -> bool:
            now_ = parse_stamp(stamp)
            cur = _envelope_stop(conn, t, e)
            return cur == s and cur <= now_ + near
        lapsed = s <= now
        return _file_checked(store, "envelope", {
            "envelope_id": e, "thread_id": t, "stop_at": utc_stamp(s), "lapsed": lapsed,
            "text": f"envelope {e} for thread {t} " + ("stopped at " if lapsed else "stops at ") + utc_stamp(s)},
            dedupe_key=f"envelope:{e}:{t}:{utc_stamp(s)}", thread_id=t, authority_relevant=True, still=still)
    items = _each("envelope", due, make, bad)      # row errors join the stop_earlier ones; all are reported AFTER the filing
    _report(errors, bad)
    return items


def feed_end(store: CoordStore, queue_dir: Path | str, *, errors: list[str] | None = None) -> list[Item]:
    """Every Q13 End the operator chose: an open halt in ``limits.db`` with no gap number (an undecided halt has a gap and
    is a SIGN's business). An ABSENT ``limits.db`` reads as none; any error on one that exists propagates, so
    :func:`collect_feeds` reports the feed as an error and never as "no Ends"."""
    path = Path(queue_dir) / "limits.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    try:
        halts = conn.execute("SELECT seq, agent, root, kind, at FROM halts WHERE released_at IS NULL AND gap IS NULL "
                             "ORDER BY seq").fetchall()
    finally:
        conn.close()

    def make(row: tuple[Any, ...]) -> Item:
        s, a, r, k, _t = row
        return file_item(store, "end", {
            "agent": _clip(a), "root": _clip(r), "kind": _clip(k), "halt": s,
            "text": _clip(f"you ended {a} session {r} at its {k} limit; it stays stopped until you extend")},
            dedupe_key=f"end:{hashlib.sha256(f'{a}\0{r}'.encode()).hexdigest()[:24]}:{s}")[0]
    return _each("end", halts, make, errors)


def feed_digest(store: CoordStore, queue_dir: Path | str, *, since: timedelta = timedelta(days=7),
                until: datetime | None = None) -> Item | None:
    """Q15's unseen digest as one item: the calls a standing grant or a late ruling answered in the window, counted by
    agent and stamp. The windows never overlap and are half-open: a new one starts where the last digest item's ended, however
    long ago (the first starts ``since`` before ``until``), and its key is that exact start. None when the window is empty or no ledger exists."""
    from levain.consent.ledger import DB_NAME, Ledger

    if not (Path(queue_dir) / DB_NAME).exists():
        return None
    end = until if until is not None else datetime.now(timezone.utc)
    start = end - since
    with read_transaction(store._connect) as conn:
        last = conn.execute("SELECT body FROM review_items WHERE feed = 'digest' ORDER BY rowid DESC LIMIT 1").fetchone()
    if last is not None:
        start = parse_stamp(json.loads(last[0])["until"])          # unconditionally: a long gap is still one window
    if start >= end:
        return None
    # half-open [start, end): the ledger's window includes both ends, so a call on the boundary is dropped here and
    # counted by the next window, once. KNOWN EDGES: two runs racing on one key file one item (the dedupe returns the
    # first); a call written late with a stamp before the last digest's end is never counted.
    hi = utc_stamp(end)
    calls = [c for c in Ledger(Path(queue_dir)).digest(start, end) if c.received_at < hi]
    if not calls:
        return None
    counts: dict[str, int] = {}
    for c in calls:
        counts[f"{c.agent}/{c.stamp}"] = counts.get(f"{c.agent}/{c.stamp}", 0) + 1
    lo = utc_stamp(start)
    item, _new = file_item(store, "digest", {
        "since": lo, "until": utc_stamp(end), "counts": counts, "calls": len(calls),
        "text": f"{len(calls)} calls ran under a standing grant or a late ruling since {lo}, and you were not asked"},
        dedupe_key=f"digest:{lo}")
    return item


def collect_feeds(store: CoordStore, *, queue_dir: Path | str | None = None) -> dict[str, Any]:
    """Run every mechanical feed once. ``{feed: [item ids]}``, and a feed that raised is ``{feed: "error: ..."}``: one
    failing feed never hides the others, and is never read as an empty one. A row a feed could not file is recorded
    under ``"errors": {feed: [lines]}`` (present only when there are some) and the feed's other rows are filed.
    ``queue_dir`` defaults to the store's own directory (the consent directory), where ``limits.db`` and ``ledger.db``
    sit."""
    qdir = Path(queue_dir) if queue_dir is not None else store.directory
    row_errors: dict[str, list[str]] = {}

    def sink(name: str) -> list[str]:
        return row_errors.setdefault(name, [])
    runs: dict[str, Callable[[], Any]] = {
        "receipt": lambda: feed_no_split(store, errors=sink("receipt")),
        "seat": lambda: feed_dead_seats(store, errors=sink("seat")),
        "envelope": lambda: feed_envelopes(store, errors=sink("envelope")),
        "end": lambda: feed_end(store, qdir, errors=sink("end")),
        "digest": lambda: feed_digest(store, qdir)}
    out: dict[str, Any] = {}
    for name, run in runs.items():
        try:
            got = run()
        except Exception as exc:  # noqa: BLE001 - reported under the feed's name, never swallowed
            out[name] = f"error: {type(exc).__name__}: {exc}"[:300]
            continue
        out[name] = [] if got is None else [i.item_id for i in (got if isinstance(got, list) else [got])]
    bad = {n: e for n, e in row_errors.items() if e}
    if bad:
        out["errors"] = bad
    return out
