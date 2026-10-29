"""levain.coordinator.review: the review queue and the push records (C4 backend; C0 §3.3 and §3.6).

What it holds, in the coordinator's own store (``dispatch.db``):

- **The review queue** (§3.6): ``review_items`` and ``review_events``. An item is something that waits for a look and
  never pushes (the grammar's KNOW, *"review queue, never a push"*). Its state is derived: open until a ``cleared``
  event, open again after a ``reopened`` one. Tables are append-only like the rest of the store.
- **Three review modes** (RULED, *"also on review modes"*): ``manual`` (you), ``fresh`` (a fresh session you asked,
  recorded as you) and ``coordinator`` (the actor reviewing itself). An item marked ``authority_relevant`` can never be
  cleared in ``coordinator`` mode, and the SQL refuses it, so no caller has to remember. A fourth mode, ``signed``, is
  no caller's to pick: it is the clear the operator's passkey signed (:func:`clear_signed`), and its event names the
  sign request, so a reader tells it from the laptop's unsigned ``manual`` clear.
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
from contextvars import ContextVar
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator.store import ACTORS, CoordError, CoordStore, _append_only, _in

__all__ = ["FEEDS", "FEED_LABELS", "MODES", "NEAR_STOP", "NO_SPLIT_AFTER", "REVIEW_SCHEMA", "SIGNED", "SIGN_REQUEST_TTL",
           "SIGN_TIER", "CoordNotFound", "Item", "SignedClearRefused", "body_fields", "clear_item", "clear_message",
           "clear_signed", "collect_feeds", "feed_dead_seats", "feed_digest", "feed_end", "feed_envelopes", "feed_no_split",
           "file_clear_request", "file_item", "file_know", "QueuePage", "get_item", "last_pass", "list_items",
           "push_answer", "push_sign", "queue_page", "record_pass", "reopen_item", "waiting_on_you"]

FEEDS = ("receipt", "seat", "envelope", "end", "digest", "know", "doubt", "contradiction")
# a seat's words for each feed: the ONE table every surface draws from (the cockpit panel, the phone route); the
# stored name stays the item's ``feed``
FEED_LABELS = {"receipt": "Drop not split", "seat": "Seat died", "envelope": "Envelope near its stop",
               "end": "Session ended at a limit", "digest": "Unseen digest", "know": "Know", "doubt": "Doubt",
               "contradiction": "Contradiction"}
MODES = ("manual", "fresh", "coordinator")
# c4-mut R4: the mode of a clear the operator's passkey signed. Never a caller's to pick (MODES are): only
# clear_signed writes it, and the table holds a signed event to the sign request it names (and only a signed one
# names one)
SIGNED = "signed"
# what the passkey of a signed clear must cover: the class a thread envelope's signature needs (envelope.SIGN_TIER),
# since an authority-relevant item is filed over a signed envelope [judged by lane-c4mut2, 10-25, against that constant]
SIGN_TIER = "T2"
# how long a signed clear's sign request stays open: one ceremony, read and signed on the spot
SIGN_REQUEST_TTL = timedelta(minutes=10)
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
#: the rec a record carries when nobody advised on it (the delivery pass files a raw gate request's SIGN this way): it
#: renders as ``rec: none (authority item)`` on the band
NO_REC = "none (authority item)"
_REC_MAX = 500
_REF_STORE = {"sign": "queue.db", "answer": "questions.db"}

# review_events as this levain makes it: a store made before R4 holds the first six columns and the narrower mode CHECK,
# and migrate_events rebuilds it (the store's ensure-schema path), every row kept as it was
_EVENTS_TABLE = (
    "CREATE TABLE IF NOT EXISTS review_events (item_id TEXT NOT NULL REFERENCES review_items(item_id), "
    "seq INTEGER NOT NULL, op TEXT NOT NULL CHECK (op IN ('cleared', 'reopened')), "
    f"mode TEXT NOT NULL CHECK ({_in('mode', (*MODES, SIGNED))}), "
    "by TEXT NOT NULL CHECK (by IN ('operator', 'coordinator')), at TEXT NOT NULL, sign_request_id TEXT, "
    f"PRIMARY KEY (item_id, seq), CHECK ((mode = '{SIGNED}') = (sign_request_id IS NOT NULL)), "
    f"CHECK (mode != '{SIGNED}' OR op = 'cleared'))")
# (name, type, notnull, pk) of review_events before R4, then the column R4 adds
_EVENTS_BEFORE = [("item_id", "TEXT", 1, 1), ("seq", "INTEGER", 1, 2), ("op", "TEXT", 1, 0), ("mode", "TEXT", 1, 0),
                  ("by", "TEXT", 1, 0), ("at", "TEXT", 1, 0)]
_EVENTS_NOW = [*_EVENTS_BEFORE, ("sign_request_id", "TEXT", 0, 0)]

REVIEW_SCHEMA: tuple[str, ...] = (
    # dedupe_key is what makes a feed idempotent; by is who filed it (a feed is the service, a KNOW the coordinator)
    "CREATE TABLE IF NOT EXISTS review_items (item_id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(thread_id), "
    f"feed TEXT NOT NULL CHECK ({_in('feed', FEEDS)}), dedupe_key TEXT NOT NULL UNIQUE, body TEXT NOT NULL, "
    "authority_relevant INTEGER NOT NULL CHECK (authority_relevant IN (0, 1)), "
    f"by TEXT NOT NULL CHECK ({_in('by', ACTORS)}), at TEXT NOT NULL)",
    _EVENTS_TABLE,
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


def ensure_one_signature(conn: sqlite3.Connection) -> None:
    """One passkey signature clears at most once: a unique index on ``review_events.sign_request_id`` where it is set,
    made by the store's ensure path (every writer runs it before it writes), after the schema's statements and only on
    a table that has the column (an event without the column cannot be a signed clear). Not in
    :data:`REVIEW_SCHEMA`: a reader finds a store current by the names that schema creates
    (:func:`levain.coordinator.store.schema_present`), and a store made before this index is still read."""
    if "sign_request_id" in {r[1] for r in conn.execute("PRAGMA table_info(review_events)")}:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS review_events_one_signature ON review_events "
                     "(sign_request_id) WHERE sign_request_id IS NOT NULL")


class CoordNotFound(CoordError):
    """No review item has the id asked for. A surface classifies it by this type (404), never by its words."""


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
    last: tuple[str, str, str, str, int] | None   # (op, mode, by, at, seq) of the latest event


def _text(name: str, value: Any, high: int = _TEXT_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CoordError(f"{name} must be 1 to {high} characters of text")
    return value


def _item(conn: sqlite3.Connection, row: tuple[Any, ...]) -> Item:
    last = conn.execute("SELECT op, mode, by, at, seq FROM review_events WHERE item_id = ? ORDER BY seq DESC LIMIT 1",
                        (row[0],)).fetchone()
    return Item(row[0], row[1], row[2], json.loads(row[4]), bool(row[5]), row[6], row[7],
                "cleared" if last is not None and last[0] == "cleared" else "open",
                None if last is None else tuple(last))


def body_fields(item: Item) -> dict[str, Any]:
    """An item's body as fields, for every surface that draws one: ``{}`` when the stored body is not a JSON object
    (the store holds whatever a feed wrote; a row is drawn either way)."""
    return item.body if isinstance(item.body, dict) else {}


_COLS = "item_id, thread_id, feed, dedupe_key, body, authority_relevant, by, at"


# the ids this thread's current :func:`collect_feeds` run inserted (None outside one): what "created" means to a counter
_CREATED: ContextVar[list[str] | None] = ContextVar("review_created", default=None)


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


def _note_created(item_id: str) -> None:
    """Record an inserted id for the running :func:`collect_feeds`; called only once the write that inserted it has
    committed, so a failed COMMIT never reports a new item."""
    made = _CREATED.get()
    if made is not None:
        made.append(item_id)


def file_item(store: CoordStore, feed: str, body: Mapping[str, Any], *, dedupe_key: str,
              thread_id: str | None = None, authority_relevant: bool = False, by: str = "service",
              token: object = None) -> tuple[Item, bool]:
    """File one review item, once per ``dedupe_key``: a second call with the same key returns the stored item and
    ``False``, and writes nothing (so a feed run again, or a cleared item's condition still holding, files nothing)."""
    with store._write(by, token) as (conn, now):
        item, new = _insert(conn, now, feed, body, dedupe_key=dedupe_key, thread_id=thread_id,
                            authority_relevant=authority_relevant, by=by)
    if new:
        _note_created(item.item_id)
    return item, new


def _file_checked(store: CoordStore, feed: str, body: Mapping[str, Any], *, dedupe_key: str, thread_id: str | None = None,
                  authority_relevant: bool = False, still: Callable[[sqlite3.Connection, str], bool]) -> Item | None:
    """File a feed's item only if ``still(conn, now)`` holds INSIDE the write transaction that inserts it: a feed's scan
    is a read, and the world can move between that read and this write. ``None`` when it no longer holds."""
    with store._write() as (conn, now):
        if not still(conn, now):
            return None
        item, new = _insert(conn, now, feed, body, dedupe_key=dedupe_key, thread_id=thread_id,
                            authority_relevant=authority_relevant)
    if new:
        _note_created(item.item_id)
    return item


def get_item(store: CoordStore, item_id: str) -> Item:
    """One item, as it is now. Like every review transition it makes no store: no store is :class:`StoreAbsent`, a
    missing item :class:`CoordNotFound`."""
    with read_transaction(lambda: store._connect(create=False)) as conn:
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise CoordNotFound(f"no review item {str(item_id)[:40]}")
        return _item(conn, row)


def list_items(store: CoordStore, *, include_cleared: bool = False, feed: str | None = None,
               thread_id: str | None = None, conn: sqlite3.Connection | None = None) -> list[Item]:
    """The queue, oldest first. Open items only unless ``include_cleared``. ``conn`` is the read transaction a caller
    already holds (the read is then one of its snapshot's); without it the read opens its own."""
    def read(c: sqlite3.Connection) -> list[Item]:
        return [_item(c, r) for r in c.execute(f"SELECT {_COLS} FROM review_items ORDER BY rowid").fetchall()]

    if conn is not None:
        items = read(conn)
    else:
        with read_transaction(store.open_readonly) as own:
            items = read(own)
    return [i for i in items if (include_cleared or i.state == "open") and (feed is None or i.feed == feed)
            and (thread_id is None or i.thread_id == thread_id)]


# an item with its latest event, in one statement: the item is open unless that event cleared it (as :func:`_item` reads it)
_LATEST = ("FROM review_items i LEFT JOIN review_events e ON e.item_id = i.item_id AND e.seq = "
           "(SELECT MAX(seq) FROM review_events WHERE item_id = i.item_id)")
_LATEST_COLS = ("i.item_id, i.thread_id, i.feed, i.dedupe_key, i.body, i.authority_relevant, i.by, i.at, e.op, e.mode, "
                "e.by, e.at, e.seq")


@dataclass(frozen=True)
class QueuePage:
    """A bounded look at the queue: how many items are open, the oldest ``open_limit`` of them (oldest first), and the
    most recently cleared ``cleared_limit``, newest clear first."""
    open_total: int
    open: list[Item]
    cleared: list[Item]


def _joined(row: tuple[Any, ...]) -> Item:
    last = None if row[8] is None else (row[8], row[9], row[10], row[11], row[12])
    return Item(row[0], row[1], row[2], json.loads(row[4]), bool(row[5]), row[6], row[7],
                "cleared" if row[8] == "cleared" else "open", last)


def queue_page(conn: sqlite3.Connection, *, open_limit: int, cleared_limit: int) -> QueuePage:
    """The queue's open count and the bounded open and cleared pages, read in three statements on ``conn`` whatever the
    queue's history (a long-lived store keeps every item it ever held). Cleared items order by the clearing event's
    rowid, the order they were cleared in."""
    total = conn.execute(f"SELECT COUNT(*) {_LATEST} WHERE e.op IS NULL OR e.op != 'cleared'").fetchone()[0]
    opened = conn.execute(f"SELECT {_LATEST_COLS} {_LATEST} WHERE e.op IS NULL OR e.op != 'cleared' "
                          "ORDER BY i.rowid LIMIT ?", (int(open_limit),)).fetchall()
    cleared = conn.execute(f"SELECT {_LATEST_COLS} {_LATEST} WHERE e.op = 'cleared' ORDER BY e.rowid DESC LIMIT ?",
                           (int(cleared_limit),)).fetchall()
    return QueuePage(total, [_joined(r) for r in opened], [_joined(r) for r in cleared])


def _review(store: CoordStore, item_id: str, op: str, mode: str, token: object, plain_only: bool = False,
            still: Callable[[Item], None] | None = None) -> Item:
    if mode not in MODES:
        raise CoordError(f"a review mode is one of {', '.join(MODES)}")
    by = "coordinator" if mode == "coordinator" else "operator"
    with store._write(by, token, create=False) as (conn, now):
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise CoordNotFound(f"no review item {str(item_id)[:40]}")
        item = _item(conn, row)
        if still is not None:
            still(item)          # the caller's own check, on the item as this transaction sees it; a raise writes nothing
        if op == "cleared" and item.state != "open" or op == "reopened" and item.state != "cleared":
            raise CoordError(f"item {item_id} is already {item.state}")
        if op == "cleared" and plain_only and row[5]:
            raise CoordError(f"item {item_id} is yours to clear: it is cleared on your passkey's signature "
                             f"(the signed clear), or on the laptop: levain coord review clear {item_id}")
        if op == "cleared" and by == "coordinator" and row[5]:
            raise CoordError("an authority-relevant item is cleared by you or a fresh session you asked, never by "
                             "the coordinator reviewing itself")
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events WHERE item_id = ?",
                           (item_id,)).fetchone()[0]
        conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at) VALUES (?, ?, ?, ?, ?, ?)",
                     (item_id, seq, op, mode, by, now))
        return _item(conn, row)      # read after the insert: the state it now has


def clear_item(store: CoordStore, item_id: str, *, mode: str = "manual", token: object = None,
               plain_only: bool = False, still: Callable[[Item], None] | None = None) -> Item:
    """Clear an open item in ``mode``. ``coordinator`` mode writes as the coordinator, so it needs the lease token, and
    it is refused for an authority-relevant item. ``plain_only`` is a clear sent on a device's token (the cockpit's and
    the phone's Clear): it is refused for an authority-relevant item too, whose clear the head ruled signed-only
    (1025+25): :func:`clear_signed`, or the laptop's own clear. ``still(item)`` runs inside the write transaction on the
    item as it is then, before anything is written; it refuses by raising. A missing item is :class:`CoordNotFound`, a
    missing store :class:`StoreAbsent` (none is made)."""
    return _review(store, item_id, "cleared", mode, token, plain_only, still)


def reopen_item(store: CoordStore, item_id: str, *, mode: str = "manual", token: object = None,
                plain_only: bool = False, still: Callable[[Item], None] | None = None) -> Item:
    """Reopen a cleared item. A reopen clears nothing, so ``plain_only`` (a device's token) refuses none. ``still`` and
    the refusals are :func:`clear_item`'s."""
    return _review(store, item_id, "reopened", mode, token, plain_only, still)


# --- the signed clear (c4-mut session 2, head rulings R1-R5) ---------------------------------------------------------
class SignedClearRefused(CoordError):
    """A signed clear refused at the door, with the HTTP ``status`` and the ``error`` word a surface answers it with."""

    def __init__(self, status: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status, self.error = status, error


def migrate_events(conn: sqlite3.Connection) -> bool:
    """R4's migration of a ``review_events`` made before it (session 1's six columns and narrower mode CHECK) to
    :data:`_EVENTS_TABLE`, run ONLY by the store's ensure-schema path (:meth:`levain.coordinator.store.CoordStore.
    _ensure`), inside its ``BEGIN IMMEDIATE``, before the schema's statements, which then recreate the table's
    triggers; that path runs ``PRAGMA integrity_check`` before it commits. True when it rebuilt the table.

    The rebuild is SQLite's documented procedure for a constraint change (sqlite.org/lang_altertable.html, "Making
    Other Kinds Of Table Schema Changes"): a new table, every row copied with its ``rowid`` (the cleared page orders by
    it) and a NULL request, the old table dropped, the new one renamed. Every stored value is kept as it was. A table
    of any other shape is left as it is."""
    cols = [(r[1], r[2], r[3], r[5]) for r in conn.execute("PRAGMA table_info(review_events)")]
    if cols != _EVENTS_BEFORE:
        return False
    conn.execute(_EVENTS_TABLE.replace("CREATE TABLE IF NOT EXISTS review_events ", "CREATE TABLE review_events_r4 ", 1))
    conn.execute("INSERT INTO review_events_r4 (rowid, item_id, seq, op, mode, by, at) "
                 "SELECT rowid, item_id, seq, op, mode, by, at FROM review_events")
    conn.execute("DROP TABLE review_events")
    conn.execute("ALTER TABLE review_events_r4 RENAME TO review_events")
    return True


def clear_message(item: Item) -> str:
    """What a signed clear signs (R3): ``levain review clear <item_id> <state> <last event at>#<its seq>`` (``none``
    for an item never cleared or reopened). The row version's own inputs, the event's seq among them, so a signature
    over one version clears no other, even when two transitions share one clock reading."""
    last = f"{item.last[3]}#{item.last[4]}" if item.last is not None else "none"
    return f"levain review clear {item.item_id} {item.state} {last}"


def file_clear_request(store: CoordStore, item_id: str, *, queue_dir: Path, reuse: bool = False) -> str:
    """File levain's own sign request for the signed clear of the open "yours to clear" item ``item_id`` in the sign
    directory of ``queue_dir`` (namespace :data:`levain.contracts.namespaces.REVIEW_CLEAR_NAMESPACE`, open for
    :data:`SIGN_REQUEST_TTL`) and return its id. ``reuse``: the open request over the item as it is now, when one has
    half its time left, is answered instead of a new one (:func:`levain.consent.sign.levain_request_for`; the phone's
    door, which files on every tap). CoordError when there is no such item, it is not open, or it is not yours to
    clear (a plain item is cleared as it is)."""
    from levain.consent.sign import file_levain_request, levain_request_for
    from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE

    item = get_item(store, item_id)
    if item.state != "open":
        raise CoordError(f"item {item_id} is already {item.state}")
    if not item.authority_relevant:
        raise CoordError(f"item {item_id} is not yours to clear: it is cleared as it is, with no signature")
    filing = levain_request_for if reuse else file_levain_request
    return filing(Path(queue_dir), REVIEW_CLEAR_NAMESPACE, clear_message(item),
                  title=f"Clear a review item: {FEED_LABELS.get(item.feed, item.feed)}",
                  ttl_s=SIGN_REQUEST_TTL.total_seconds())


def clear_signed(store: CoordStore, item_id: str, *, queue_dir: Path, request_id: object,
                 keys: Callable[[], Mapping[str, Any]], still: Callable[[], None] | None = None) -> Item:
    """Clear ``item_id`` on the operator's passkey signature (R3, R4): the sign request ``request_id`` in
    ``queue_dir``'s sign directory must be levain's own in :data:`levain.contracts.namespaces.REVIEW_CLEAR_NAMESPACE`,
    signed (not open, not denied) and unexpired; its signature must verify (:func:`levain.authority.verify_link_sig`)
    over its message in that namespace by a key ``keys()`` names (the active passkeys enrolled for the Signer's name),
    whose class covers :data:`SIGN_TIER`. The verify runs before the write; inside the write transaction the message
    is recomputed from the item as it is now (:func:`clear_message`) and compared byte for byte, so a signature over an
    older version (the item cleared or reopened since) clears nothing. The event is mode :data:`SIGNED` with the
    request's id. ``still()`` runs inside the write transaction before anything is read or written there (the
    phone's check that the device is still paired); it refuses by raising. Refusals are :class:`SignedClearRefused`;
    a missing item is :class:`CoordNotFound` and a missing store :class:`StoreAbsent` (none is made), as on every
    other review transition."""
    import hashlib

    from levain.authority import VerifierUnavailable, verify_link_sig
    from levain.consent.queue import class_covers
    from levain.consent.sign import SignRefused, read_request, request_id_ok, sign_dir, state_of
    from levain.contracts.namespaces import REVIEW_CLEAR_NAMESPACE

    def refuse(why: str) -> SignedClearRefused:
        return SignedClearRefused(403, "sign_required", f"item {item_id} is cleared only on your passkey's "
                                  f"signature: {why}; nothing was cleared")

    if not request_id_ok(request_id):
        raise refuse("no sign request was named")
    rid = str(request_id)
    try:
        request = read_request(Path(queue_dir), rid)
    except SignRefused:
        raise refuse(f"there is no sign request {rid}") from None
    if request.namespace != REVIEW_CLEAR_NAMESPACE or request.refused is not None:
        raise refuse(f"sign request {rid} is not a review clear levain filed")
    state = state_of(Path(queue_dir), rid)
    if state != "signed":
        raise refuse(f"sign request {rid} is {'denied' if state == 'denied' else 'not signed'}")
    if request.expires_at is None or request.expires_at <= datetime.now(timezone.utc):
        raise refuse(f"sign request {rid} expired")
    message = request.message.encode("utf-8")
    d = sign_dir(Path(queue_dir))
    try:
        record = json.loads((d / f"{rid}.signed.json").read_text(encoding="utf-8"))
        signature = (d / f"{rid}.sig").read_text(encoding="ascii")
    except (OSError, ValueError):
        raise refuse(f"the signature of sign request {rid} cannot be read") from None
    if (not isinstance(record, dict) or record.get("namespace") != REVIEW_CLEAR_NAMESPACE
            or record.get("message_sha256") != hashlib.sha256(message).hexdigest()):
        raise refuse(f"the record of sign request {rid} is not over its message")
    try:
        active = keys()
    except SignRefused as exc:
        raise SignedClearRefused(503, "unavailable", f"the passkeys cannot be read now ({exc}): nothing was cleared; "
                                 "send it again") from None
    fp = record.get("signer_fp")
    key = active.get(fp) if isinstance(fp, str) else None
    if key is None:
        raise refuse(f"{str(fp)[:80]} is not an active passkey enrolled here")
    if not class_covers(key.key_class, SIGN_TIER):
        raise refuse(f"a {key.key_class} passkey does not cover {SIGN_TIER}")
    try:
        verified = verify_link_sig(message, signature, public_key=key.public_key, fingerprint=key.fingerprint,
                                   namespace=REVIEW_CLEAR_NAMESPACE)
    except VerifierUnavailable as exc:
        raise SignedClearRefused(503, "unavailable", f"the signature could not be verified now ({exc}): nothing was "
                                 "cleared; send it again") from None
    if not verified:
        raise refuse(f"the signature of sign request {rid} does not verify")
    with store._write("operator", create=False) as (conn, now):
        if still is not None:
            still()
        row = conn.execute(f"SELECT {_COLS} FROM review_items WHERE item_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise CoordNotFound(f"no review item {str(item_id)[:40]}")
        if clear_message(_item(conn, row)) != request.message:
            raise SignedClearRefused(409, "stale", f"sign request {rid} was signed for another version of item "
                                     f"{item_id} (it was cleared or reopened since): nothing was cleared; sign again")
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_events WHERE item_id = ?",
                           (str(item_id),)).fetchone()[0]
        try:
            conn.execute("INSERT INTO review_events (item_id, seq, op, mode, by, at, sign_request_id) "
                         "VALUES (?, ?, 'cleared', ?, 'operator', ?, ?)", (str(item_id), seq, SIGNED, now, rid))
        except sqlite3.IntegrityError as exc:
            # only the unique index on the signature is a replay; any other integrity fault stays a store fault
            if "review_events.sign_request_id" not in str(exc):
                raise
            raise SignedClearRefused(409, "stale", f"sign request {rid} already cleared an item: nothing was cleared; "
                                     "sign again") from None
        return _item(conn, row)


# --- the push records (C0 §3.3) ----------------------------------------------------------------------------------
def first_reader_holds(conn: sqlite3.Connection, question_id: str) -> bool:
    """The question is the coordinator's to read first: its filing ping is held and not released, with no first read
    yet, or the coordinator answered it as a KNOW. The one reading of that state; the delivery pass and the ANSWER write
    both ask it."""
    if conn.execute("SELECT 1 FROM first_reads WHERE question_id = ? AND outcome = 'know'", (question_id,)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM held_pings h WHERE h.question_id = ? AND NOT EXISTS (SELECT 1 FROM held_ping_releases r WHERE "
        "r.question_id = h.question_id) AND NOT EXISTS (SELECT 1 FROM first_reads f WHERE f.question_id = h.question_id)",
        (question_id,)).fetchone() is not None


class _Held(Exception):
    """Raised inside the ANSWER write when the first reader holds the question: nothing is written."""


def _rec(kind: str, rec: object) -> str:
    """The rec a SIGN or ANSWER body must carry (Q17-ALL D11): the coordinator's advice on what the operator should
    pick, or :data:`NO_REC` when nobody advised. A body without one is refused."""
    if not isinstance(rec, str) or not rec.strip() or len(rec) > _REC_MAX:
        raise CoordError(f"a {kind.upper()} must carry the coordinator's rec (1 to {_REC_MAX} characters, e.g. "
                         f"'a: tests green, the envelope names main'; {NO_REC!r} when there is none): "
                         "every band item shows a rec")
    return rec.strip()


def _push(store: CoordStore, kind: str, thread_id: str | None, ref_id: str, text: str, extra: Mapping[str, Any],
          by: str, token: object, still_open: Callable[[str], bool | None] | None,
          unless_held: bool = False, rec: str | None = None) -> tuple[str, bool]:
    if kind not in _PUSH_KINDS:
        raise CoordError("a push record is a sign or an answer")
    if by not in ACTORS:
        raise CoordError(f"a record is written by one of {', '.join(ACTORS)}")
    ref = _text("a reference", ref_id, 200)
    body = {"text": _text("the text", text), "rec": _rec(kind, rec), **extra}
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
        if unless_held and first_reader_holds(conn, ref):
            raise _Held()
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


def push_sign(store: CoordStore, thread_id: str | None, request_id: str, text: str, *, rec: str | None = None,
              onto: str | None = None, by: str = "service", token: object = None,
              still_open: Callable[[str], bool | None] | None = None) -> tuple[str, bool]:
    """Record a SIGN: a pointer to the consent-queue request ``request_id``. ``rec`` is REQUIRED (D11): the
    coordinator's advice, or :data:`NO_REC`; a body without one is refused. ``(reply_id, created)``; the same request
    again returns the first record. With ``onto`` and a ``thread_id``, a second land SIGN for the same (thread, onto) needs
    ``still_open`` (a request id -> open?) and is refused unless it says an explicit ``False`` for every earlier one."""
    extra: dict[str, Any] = {} if onto is None else {"onto": _text("onto", onto, 200)}
    return _push(store, "sign", thread_id, request_id, text, extra, by, token, still_open, rec=rec)


def push_answer(store: CoordStore, thread_id: str | None, question_id: str, text: str, *, rec: str | None = None,
                by: str = "service", token: object = None) -> tuple[str, bool]:
    """Record an ANSWER: a pointer to the question ``question_id`` in ``questions.db``. It authorises nothing. ``rec``
    is REQUIRED (D11), as for :func:`push_sign`."""
    return _push(store, "answer", thread_id, question_id, text, {}, by, token, None, rec=rec)


def push_answer_unless_held(store: CoordStore, thread_id: str | None, question_id: str, text: str, *,
                            rec: str | None = None, by: str = "service",
                            token: object = None) -> tuple[str, bool] | None:
    """:func:`push_answer` for the delivery pass: the first-reader check is made INSIDE the write transaction that
    inserts the record, and ``hold_ping`` (``first_reader``) declines a hold once the record exists. Whichever writes
    first decides; the loser is a no-op. None when the first reader holds the question (nothing written)."""
    try:
        return _push(store, "answer", thread_id, question_id, text, {}, by, token, None, unless_held=True, rec=rec)
    except _Held:
        return None


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
def queue_request_open(queue_dir: Path | str, *, clock: Callable[[], datetime] | None = None,
                       conn: sqlite3.Connection | None = None) -> Callable[[str], bool | None]:
    """A request id -> open? read from ``queue.db`` with ``mode=ro``: open while ``undecided`` or ``pending`` and not past
    the queue's own expiry rule (:meth:`ConsentQueue._expiry_due`, the one the queue's read and sweep use: a hold's end
    plus ``UNDECIDED_GRACE``), closed in any other state, ``None`` when the store or the row cannot be read. A raw read:
    this is information, and the seal on a row is the queue's own business. ``conn``: a read-only connection to
    ``queue.db`` the caller holds (one snapshot for every request, its busy wait the caller's), else one per call."""
    from levain.consent.queue import ConsentQueue

    path = Path(queue_dir) / "queue.db"
    now = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def read(request_id: str) -> bool | None:
        try:
            c = conn if conn is not None else sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                                              timeout=2.0)
        except (OSError, sqlite3.Error):
            return None
        try:
            row = c.execute("SELECT state, expires_at FROM requests WHERE id = ?", (request_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            if c is not conn:
                c.close()
        if row is None:
            return None
        if row[0] == "pending" and ConsentQueue._expiry_due(row[1], utc_stamp(now())):
            return False
        return row[0] in ("pending", "undecided")
    return read


def question_open(queue_dir: Path | str, *, conn: sqlite3.Connection | None = None) -> Callable[[str], bool | None]:
    """A question id -> open? read through the band's own read-only path: open while it has no answer, ``None`` when
    there is no such question or the store cannot be read (the record is then ``unresolved``, never dropped).
    ``conn``: a read-only connection to ``questions.db`` the caller holds (one snapshot for every question)."""
    from levain.consent.questions import QuestionStore

    qs = QuestionStore(Path(queue_dir))

    def read(question_id: str) -> bool | None:
        try:
            q = qs.get(question_id) if conn is None else QuestionStore._by_ref(conn, question_id)
        except Exception:  # noqa: BLE001 - a bad store is an unreadable record, not a crashed view
            return None
        return None if q is None else q.answer is None
    return read


def waiting_on_you(store: CoordStore, *, sign_open: Callable[[str], bool | None],
                   answer_open: Callable[[str], bool | None], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """What waits on you, derived from the open SIGN and ANSWER records: ``{"threads": {thread_id: [records]},
    "loose": [records with no thread], "unresolved": [records whose state could not be read]}``. A record is
    ``{"kind", "ref_id", "reply_id", "text"}``; a record whose stored body is not readable JSON is ``unresolved`` with
    ``unreadable`` saying so, and the others are read as ever. A thread with no open record is absent. ``conn`` is the read
    transaction a caller already holds."""
    from levain.coordinator import budget

    def read(c: sqlite3.Connection) -> tuple[list[Any], dict[str, Any]]:
        got = c.execute("SELECT reply_id, thread_id, kind, ref_id, body, ref_store FROM replies WHERE kind IN "
                        "('sign', 'answer') ORDER BY rowid").fetchall()
        # a SIGN to extend a thread's budget (C6) points into this store, not queue.db: read here, in this transaction
        at = store._stamp()
        return got, {r[3]: budget.sign_open(c, r[3], at) for r in got if r[5] == budget.SIGN_REF}

    if conn is not None:
        rows, extend = read(conn)
    else:
        with read_transaction(store.open_readonly) as own:
            rows, extend = read(own)
    out: dict[str, Any] = {"threads": {}, "loose": [], "unresolved": []}
    for reply_id, thread_id, kind, ref_id, body, ref_store in rows:
        try:
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("the body is not an object")
        except (TypeError, ValueError, RecursionError):
            # one record whose body cannot be read: unresolved, that record alone (never every other one lost)
            out["unresolved"].append({"kind": kind, "ref_id": ref_id, "reply_id": reply_id, "text": "", "rec": None,
                                      "ref_store": ref_store, "unreadable": "its body is not readable JSON"})
            continue
        if ref_store == budget.SIGN_REF:
            state = extend[ref_id]
        else:
            state = (sign_open if kind == "sign" else answer_open)(ref_id)
        rec = {"kind": kind, "ref_id": ref_id, "reply_id": reply_id, "text": parsed.get("text", ""),
               "rec": parsed.get("rec"), "ref_store": ref_store}
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


# the feed name under which each run of the feed runner leaves a mark (value = its own seq, which only moves forward):
# "has the delivery pass ever run here" is a display's question, and this is its only answer
PASS_FEED = "pass"


def record_pass(store: CoordStore) -> None:
    """Leave one mark that the feed runner (:func:`levain.coordinator.deliver.run`) ran, at its time. Known edge: one
    ``review_marks`` row per run, append-only."""
    with store._write() as (conn, now):
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM review_marks WHERE feed = ?", (PASS_FEED,)).fetchone()[0]
        conn.execute("INSERT INTO review_marks (feed, seq, value, at) VALUES (?, ?, ?, ?)", (PASS_FEED, seq, seq, now))


def last_pass(conn: sqlite3.Connection) -> str | None:
    """When the feed runner last ran against this store, or None when it never has."""
    row = conn.execute("SELECT at FROM review_marks WHERE feed = ? ORDER BY seq DESC LIMIT 1", (PASS_FEED,)).fetchone()
    return None if row is None else row[0]


_TERMINAL = "('failed', 'expired', 'ended')"


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
    """``(the dead launches without an item as (gseq, launch), the watermark this scan reached)``. The watermark W is a
    consumer offset over the append-only ``launch_events`` log: every terminal event with gseq < W was read. The scan
    runs oldest event first from the stored W, at most ``_MAX_PAGES`` pages, and the new W is the last event read, so
    a launch that is still running never sits below W and pins nothing. A gseq is taken inside the same
    ``BEGIN IMMEDIATE`` transaction that appends its event (``next_gseq`` in ``lease._launch_event``), so commit order is
    gseq order and no event lands below W after W passed it. A terminal event is read once; what the page bound leaves
    is the next run's. Known edge: each run that reads something writes one ``review_marks`` row (a real state change)."""
    from levain.coordinator import lease

    _lazy_expiry(store)
    found: list[tuple[int, Any]] = []
    with read_transaction(store._connect) as conn:
        start = cursor = _mark(conn, "seat")
        reached = None
        for _page in range(_MAX_PAGES):
            ids = conn.execute(
                f"SELECT e.launch_id, e.gseq FROM launch_events e WHERE e.gseq >= ? AND e.kind IN {_TERMINAL} "
                "AND NOT EXISTS (SELECT 1 FROM review_items r WHERE r.dedupe_key = 'seat:' || e.launch_id) "
                "ORDER BY e.gseq ASC LIMIT ?", (cursor, _PAGE)).fetchall()
            for launch_id, gseq in ids:
                ln = lease.launch(conn, launch_id)
                if ln is not None and lease.launch_death(conn, ln) is True and ln.cause not in _NOT_A_DEATH:
                    found.append((gseq, ln))
            if len(ids) < _PAGE:
                top = conn.execute("SELECT MAX(gseq) FROM launch_events").fetchone()[0]
                reached = start if top is None else top + 1
                break
            cursor = reached = ids[-1][1] + 1
    return found, start if reached is None else reached


def feed_dead_seats(store: CoordStore, *, errors: list[str] | None = None) -> list[Item]:
    """A launch that died, by the lease's own rule (:func:`levain.coordinator.lease.launch_death`: failed or expired
    after a spawn was claimed, ended at ``0 turns``, a coordinator that lost the lease without ever writing). One that
    carried an envelope-bound thread is authority-relevant. The scan keeps a persisted watermark (``_dead_launches``):
    it advances past a launch's terminal event only once its item is filed, so a row that could not be filed is tried again."""
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
        digest = hashlib.sha256((str(a) + "\0" + str(r)).encode()).hexdigest()[:24]  # no backslash inside an f-string: 3.9
        return file_item(store, "end", {
            "agent": _clip(a), "root": _clip(r), "kind": _clip(k), "halt": s,
            "text": _clip(f"you ended {a} session {r} at its {k} limit; it stays stopped until you extend")},
            dedupe_key=f"end:{digest}:{s}")[0]
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
    ``"created"`` lists the ids THIS run inserted (a feed also returns an item that already existed), so a count of new
    items is never inferred from the table's size, which other writers move.
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
    created: list[str] = []
    mark = _CREATED.set(created)
    try:
        for name, run in runs.items():
            try:
                got = run()
            except Exception as exc:  # noqa: BLE001 - reported under the feed's name, never swallowed
                out[name] = f"error: {type(exc).__name__}: {exc}"[:300]
                continue
            out[name] = [] if got is None else [i.item_id for i in (got if isinstance(got, list) else [got])]
    finally:
        _CREATED.reset(mark)
    out["created"] = created
    bad = {n: e for n, e in row_errors.items() if e}
    if bad:
        out["errors"] = bad
    return out
