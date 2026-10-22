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
# a launch your own take or release voided, or one asked for under a lease its asker lost, is not a seat that died
_NOT_A_DEATH = ("lease lost before active", "takeover", "operator release")
_KEY_MAX = 300
_TEXT_MAX = 4000
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


def file_item(store: CoordStore, feed: str, body: Mapping[str, Any], *, dedupe_key: str,
              thread_id: str | None = None, authority_relevant: bool = False, by: str = "service",
              token: object = None) -> tuple[Item, bool]:
    """File one review item, once per ``dedupe_key``: a second call with the same key returns the stored item and
    ``False``, and writes nothing (so a feed run again, or a cleared item's condition still holding, files nothing)."""
    if feed not in FEEDS:
        raise CoordError(f"a review feed is one of {', '.join(FEEDS)}")
    if by not in ACTORS:
        raise CoordError(f"an item is filed by one of {', '.join(ACTORS)}")
    key = _text("a dedupe key", dedupe_key, _KEY_MAX)
    text = canonical(dict(body))
    if len(text) > 2 * _TEXT_MAX:
        raise CoordError("an item's body is too long")
    with store._write(by, token) as (conn, now):
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
    with store._write(by, token) as (conn, now):
        if thread_id is not None and conn.execute("SELECT 1 FROM threads WHERE thread_id = ?",
                                                  (thread_id,)).fetchone() is None:
            raise CoordError(f"no thread {str(thread_id)[:40]}")
        same = conn.execute("SELECT reply_id FROM replies WHERE kind = ? AND ref_store = ? AND ref_id = ?",
                            (kind, _REF_STORE[kind], ref)).fetchone()
        if same is not None:
            return same[0], False
        onto = extra.get("onto")
        if kind == "sign" and onto is not None and thread_id is not None and still_open is not None:
            # C0 §3.3: one land SIGN is open per (thread, onto); a re-file withdraws the older request first
            for rid, old_ref, old_body in conn.execute(
                    "SELECT reply_id, ref_id, body FROM replies WHERE kind = 'sign' AND thread_id = ?",
                    (thread_id,)).fetchall():
                if json.loads(old_body).get("onto") == onto and still_open(old_ref):
                    raise CoordError(f"thread {thread_id} already has an open land SIGN onto {onto} ({old_ref}); "
                                     "withdraw it before filing another")
        reply_id = store._reply(conn, thread_id, kind, _REF_STORE[kind], ref, body, now)
        return reply_id, True


def push_sign(store: CoordStore, thread_id: str | None, request_id: str, text: str, *, onto: str | None = None,
              by: str = "service", token: object = None,
              still_open: Callable[[str], bool | None] | None = None) -> tuple[str, bool]:
    """Record a SIGN: a pointer to the consent-queue request ``request_id``. ``(reply_id, created)``; the same request
    again returns the first record. With ``onto`` and ``still_open`` (a request id -> open?), a second open land SIGN
    for the same (thread, onto) is refused."""
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
def queue_request_open(queue_dir: Path | str) -> Callable[[str], bool | None]:
    """A request id -> open? read from ``queue.db`` with ``mode=ro``: open while ``pending`` or ``undecided``, closed in
    any other state, ``None`` when the store or the row cannot be read. A raw read: this is information, and the seal on
    a row is the queue's own business (an edited row can at worst make a card show or hide)."""
    path = Path(queue_dir) / "queue.db"

    def read(request_id: str) -> bool | None:
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
        except (OSError, sqlite3.Error):
            return None
        try:
            row = conn.execute("SELECT state FROM requests WHERE id = ?", (request_id,)).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return None if row is None else row[0] in ("pending", "undecided")
    return read


def question_open(queue_dir: Path | str) -> Callable[[str], bool | None]:
    """A question id -> open? read through the band's own read-only path: open while it has no answer, ``None`` when
    there is no such question."""
    from levain.consent.questions import QuestionStore

    qs = QuestionStore(Path(queue_dir))

    def read(question_id: str) -> bool | None:
        q = qs.get(question_id)
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
def _now(store: CoordStore) -> datetime:
    return parse_stamp(store._stamp())


def feed_no_split(store: CoordStore, *, after: timedelta = NO_SPLIT_AFTER) -> list[Item]:
    """A drop the store accepted that has no ``split`` receipt after ``after`` (C0 §3.3)."""
    now = _now(store)
    with read_transaction(store._connect) as conn:
        stuck = []
        for drop_id, received in conn.execute("SELECT drop_id, received_at FROM drops ORDER BY rowid").fetchall():
            stages = {json.loads(b).get("stage") for (b,) in conn.execute(
                "SELECT body FROM replies WHERE kind = 'drop_receipt' AND ref_id = ?", (drop_id,)).fetchall()}
            if "split" not in stages and now - parse_stamp(received) >= after:
                stuck.append((drop_id, received))
    return [file_item(store, "receipt", {"drop_id": d, "received_at": r,
                                         "text": f"drop {d} was stored at {r} and has no split receipt"},
                      dedupe_key=f"receipt:{d}")[0] for d, r in stuck]


def feed_dead_seats(store: CoordStore) -> list[Item]:
    """A launch that died: failed or expired after a spawn was claimed, or ended at ``0 turns`` (the same reading of
    "died" the coordinator's backoff uses). One that carried an envelope-bound thread is authority-relevant."""
    from levain.coordinator import lease

    with read_transaction(store._connect) as conn:
        found = []
        for ln in lease.launches(conn, limit=500):
            died = ln.state in ("failed", "expired") and ln.cause not in _NOT_A_DEATH or (
                ln.state == "ended" and ln.cause == "0 turns")
            if not died:
                continue
            if ln.state in ("failed", "expired") and conn.execute(
                    "SELECT 1 FROM launch_spawns WHERE launch_id = ?", (ln.launch_id,)).fetchone() is None:
                continue        # nobody tried to start it: a watcher down, not a seat that died
            found.append(ln)
    out = []
    for ln in found:
        one = ln.threads[0] if len(ln.threads) == 1 else None
        out.append(file_item(store, "seat", {
            "launch_id": ln.launch_id, "harness": ln.harness, "role": ln.role, "state": ln.state, "cause": ln.cause,
            "at": ln.at, "threads": list(ln.threads),
            "text": f"{ln.role} launch {ln.launch_id} ({ln.harness}) {ln.state}: {ln.cause}"},
            dedupe_key=f"seat:{ln.launch_id}", thread_id=one,
            authority_relevant=ln.pinned_envelope_id is not None)[0])
    return out


def feed_envelopes(store: CoordStore, *, near: timedelta = NEAR_STOP) -> list[Item]:
    """A live envelope whose effective stop is within ``near`` of now and still ahead. Live: bound as a thread's head,
    not killed and its thread not done. The stop is the signed one or the earliest ``stop_earlier``. Authority-relevant."""
    now = _now(store)
    with read_transaction(store._connect) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'envelope_binds'").fetchone() is None:
            return []
        due = []
        heads = conn.execute("SELECT b.thread_id, b.envelope_id, e.stop_at FROM envelope_binds b JOIN envelopes e "
                             "ON e.envelope_id = b.envelope_id WHERE b.gseq = (SELECT MAX(gseq) FROM envelope_binds "
                             "WHERE thread_id = b.thread_id)").fetchall()
        for thread_id, envelope_id, stop_at in heads:
            rows = conn.execute("SELECT kind, value FROM envelope_attenuations WHERE envelope_id = ? AND "
                                "(thread_id IS NULL OR thread_id IS ?)", (envelope_id, thread_id)).fetchall()
            if any(k in ("kill", "thread_done") for k, _v in rows):
                continue
            stop = min([parse_stamp(stop_at)] + [parse_stamp(v) for k, v in rows if k == "stop_earlier"])
            if now < stop <= now + near:
                due.append((thread_id, envelope_id, stop))
    return [file_item(store, "envelope", {
        "envelope_id": e, "thread_id": t, "stop_at": utc_stamp(s),
        "text": f"envelope {e} for thread {t} stops at {utc_stamp(s)}"},
        dedupe_key=f"envelope:{e}:{t}:{utc_stamp(s)}", thread_id=t, authority_relevant=True)[0]
        for t, e, s in due]


def feed_end(store: CoordStore, queue_dir: Path | str) -> list[Item]:
    """Every Q13 End the operator chose: an open halt in ``limits.db`` with no gap number (an undecided halt has a gap and
    is a SIGN's business). Unreadable or absent ``limits.db`` reads as none."""
    path = Path(queue_dir) / "limits.db"
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)
    except (OSError, sqlite3.Error):
        return []
    try:
        halts = conn.execute("SELECT seq, agent, root, kind, at FROM halts WHERE released_at IS NULL AND gap IS NULL "
                             "ORDER BY seq").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [file_item(store, "end", {
        "agent": a, "root": r, "kind": k, "halt": s,
        "text": f"you ended {a} session {r} at its {k} limit; it stays stopped until you extend"},
        dedupe_key=f"end:{a}:{r}:{s}")[0] for s, a, r, k, _t in halts]


def feed_digest(store: CoordStore, queue_dir: Path | str, *, since: timedelta = timedelta(days=7),
                until: datetime | None = None) -> Item | None:
    """Q15's unseen digest as one item: the calls a standing grant or a late ruling answered in the window, counted by
    agent and stamp. One item per window start, none when the window is empty or no ledger exists yet."""
    from levain.consent.ledger import DB_NAME, Ledger

    if not (Path(queue_dir) / DB_NAME).exists():
        return None
    end = until if until is not None else datetime.now(timezone.utc)
    start = end - since
    calls = Ledger(Path(queue_dir)).digest(start, end)
    if not calls:
        return None
    counts: dict[str, int] = {}
    for c in calls:
        counts[f"{c.agent}/{c.stamp}"] = counts.get(f"{c.agent}/{c.stamp}", 0) + 1
    lo = utc_stamp(start)
    item, _new = file_item(store, "digest", {
        "since": lo, "until": utc_stamp(end), "counts": counts, "calls": len(calls),
        "text": f"{len(calls)} calls ran under a standing grant or a late ruling since {lo}, and you were not asked"},
        dedupe_key=f"digest:{lo[:10]}")
    return item


def collect_feeds(store: CoordStore, *, queue_dir: Path | str | None = None) -> dict[str, Any]:
    """Run every mechanical feed once. ``{feed: [item ids]}``, and a feed that raised is ``{feed: "error: ..."}``: one
    failing feed never hides the others, and is never read as an empty one. ``queue_dir`` defaults to the store's own
    directory (the consent directory), where ``limits.db`` and ``ledger.db`` sit."""
    qdir = Path(queue_dir) if queue_dir is not None else store.directory
    runs: dict[str, Callable[[], Any]] = {
        "receipt": lambda: feed_no_split(store), "seat": lambda: feed_dead_seats(store),
        "envelope": lambda: feed_envelopes(store), "end": lambda: feed_end(store, qdir),
        "digest": lambda: feed_digest(store, qdir)}
    out: dict[str, Any] = {}
    for name, run in runs.items():
        try:
            got = run()
        except Exception as exc:  # noqa: BLE001 - reported under the feed's name, never swallowed
            out[name] = f"error: {type(exc).__name__}: {exc}"[:300]
            continue
        out[name] = [] if got is None else [i.item_id for i in (got if isinstance(got, list) else [got])]
    return out
