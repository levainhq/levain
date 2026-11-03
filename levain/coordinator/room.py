"""The room's own record (DESIGN_ROOM §2.3, §4.3, §4.4): which room thread is open, what the room proposed as a drop,
and what became of each proposal.

One ensure-schema path of its own (:func:`ensure`, IF NOT EXISTS, run by every function here before it reads), so no
edit to ``store.py`` is needed; the tables are additive beside the coordinator's. Every table is append-only.

Authority, in one place: a proposal is information. A drop exists only when :func:`commit` runs, with words the
operator sent; the room's summary rides beside the drop as a labelled note (``drop_notes``) and never enters the
drop's ``words`` or ``words_sha256``. The proposal's words are COPIED from the operator's own ``chat_messages`` rows by
id (the caller supplies ids, never bytes), so the entity's phrasing cannot become a drop."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from levain.cockpit.consent import utc_stamp
from levain.coordinator.store import (SURFACES, WORDS_MAX, CoordError, CoordStore, _append_only, _check_words, _in,
                                      next_gseq, schema_present, words_sha256)

ROOM_EVENT_KINDS = ("opened", "rotated")
PROPOSAL_KINDS = ("committed", "dismissed")
_BY = ("operator", "service")
# [seat] the summary's cap is flowConnect's NOTE_CHARS (a note points, it does not report)
SUMMARY_MAX = 600
# [seat] how many of his messages one proposal quotes
QUOTE_MAX = 20

_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_MESSAGE_ID = re.compile(r"cm-[0-9a-f]{32}")
_PROPOSAL_ID = re.compile(r"pr-[0-9a-f]{32}")

# the open room: the latest room event is an 'opened' (a room that rotated is closed; a store with no event has none)
_LATEST_KIND = "(SELECT kind FROM room_events ORDER BY seq DESC LIMIT 1)"
_LATEST_THREAD = "(SELECT room_thread FROM room_events ORDER BY seq DESC LIMIT 1)"
_ROOM_IS_OPEN = f"({_LATEST_KIND} = 'opened' AND {_LATEST_THREAD} = NEW.room_thread)"

ROOM_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS room_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, gseq INTEGER NOT NULL UNIQUE, "
    "room_thread TEXT NOT NULL REFERENCES threads(thread_id), "
    f"kind TEXT NOT NULL CHECK ({_in('kind', ROOM_EVENT_KINDS)}), at TEXT NOT NULL, "
    f"by TEXT NOT NULL CHECK ({_in('by', _BY)}))",
    *_append_only("room_events", "a room event"),
    "CREATE TRIGGER IF NOT EXISTS room_events_one_open BEFORE INSERT ON room_events WHEN NEW.kind = 'opened' AND "
    f"COALESCE({_LATEST_KIND}, 'rotated') = 'opened' BEGIN SELECT RAISE(ABORT, 'one room is open at a time'); END",
    "CREATE TRIGGER IF NOT EXISTS room_events_opened_once BEFORE INSERT ON room_events WHEN NEW.kind = 'opened' AND "
    "EXISTS (SELECT 1 FROM room_events WHERE room_thread = NEW.room_thread) "
    "BEGIN SELECT RAISE(ABORT, 'a room thread is opened once'); END",
    "CREATE TRIGGER IF NOT EXISTS room_events_rotates_open BEFORE INSERT ON room_events WHEN NEW.kind = 'rotated' AND "
    f"NOT {_ROOM_IS_OPEN} BEGIN SELECT RAISE(ABORT, 'only the open room rotates'); END",

    "CREATE TABLE IF NOT EXISTS drop_proposals (proposal_id TEXT PRIMARY KEY, gseq INTEGER NOT NULL UNIQUE, "
    "room_thread TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "quoted TEXT NOT NULL, words TEXT NOT NULL, words_sha256 TEXT NOT NULL, summary TEXT NOT NULL, "
    "summary_sha256 TEXT NOT NULL, about TEXT REFERENCES threads(thread_id), at TEXT NOT NULL, "
    f"CHECK (length(words) <= {WORDS_MAX}), CHECK (length(summary) <= {SUMMARY_MAX}))",
    *_append_only("drop_proposals", "a drop proposal"),
    "CREATE TRIGGER IF NOT EXISTS drop_proposals_room BEFORE INSERT ON drop_proposals WHEN NOT "
    f"{_ROOM_IS_OPEN} BEGIN SELECT RAISE(ABORT, 'a proposal is made in the open room'); END",
    "CREATE TRIGGER IF NOT EXISTS drop_proposals_launch BEFORE INSERT ON drop_proposals WHEN NOT EXISTS (SELECT 1 FROM "
    "launches l WHERE l.launch_id = NEW.launch_id AND l.role = 'room') "
    "BEGIN SELECT RAISE(ABORT, 'a proposal comes from a room launch'); END",

    "CREATE TABLE IF NOT EXISTS proposal_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "proposal_id TEXT NOT NULL UNIQUE REFERENCES drop_proposals(proposal_id), "
    f"kind TEXT NOT NULL CHECK ({_in('kind', PROPOSAL_KINDS)}), drop_id TEXT REFERENCES drops(drop_id), "
    f"at TEXT NOT NULL, by TEXT NOT NULL CHECK ({_in('by', _BY)}), "
    "CHECK ((kind = 'committed') = (drop_id IS NOT NULL)))",
    *_append_only("proposal_events", "a proposal event"),
    # a proposal is settled once, and a commit lands only in its own room while that room is still the open one
    "CREATE TRIGGER IF NOT EXISTS proposal_events_commits_open BEFORE INSERT ON proposal_events WHEN "
    "NEW.kind = 'committed' AND NOT EXISTS (SELECT 1 FROM drop_proposals p WHERE p.proposal_id = NEW.proposal_id "
    f"AND {_LATEST_KIND} = 'opened' AND {_LATEST_THREAD} = p.room_thread) "
    "BEGIN SELECT RAISE(ABORT, 'a proposal expires when its room rotates'); END",

    "CREATE TABLE IF NOT EXISTS drop_notes (drop_id TEXT PRIMARY KEY REFERENCES drops(drop_id), gseq INTEGER NOT NULL "
    "UNIQUE, author TEXT NOT NULL CHECK (author = 'room'), "
    "room_thread TEXT NOT NULL REFERENCES threads(thread_id), launch_id TEXT NOT NULL REFERENCES launches(launch_id), "
    "proposal_id TEXT NOT NULL UNIQUE REFERENCES drop_proposals(proposal_id), body TEXT NOT NULL, "
    "body_sha256 TEXT NOT NULL, at TEXT NOT NULL)",
    *_append_only("drop_notes", "a drop note"),
    # the note is exactly the proposal's summary, filed for the drop that proposal's commit made
    "CREATE TRIGGER IF NOT EXISTS drop_notes_is_the_summary BEFORE INSERT ON drop_notes WHEN NOT EXISTS (SELECT 1 FROM "
    "drop_proposals p JOIN proposal_events e ON e.proposal_id = p.proposal_id WHERE p.proposal_id = NEW.proposal_id "
    "AND e.kind = 'committed' AND e.drop_id = NEW.drop_id AND p.summary = NEW.body AND p.room_thread = NEW.room_thread "
    "AND p.launch_id = NEW.launch_id) "
    "BEGIN SELECT RAISE(ABORT, 'a drop note is the summary of the proposal that made the drop'); END",
)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    room_thread: str
    launch_id: str
    quoted: tuple[str, ...]
    words: str
    words_sha256: str
    summary: str
    about: str | None
    at: str
    state: str          # open | committed | dismissed | expired


def ensure(conn: sqlite3.Connection) -> None:
    """The room's tables, triggers and nothing else, in the caller's connection (inside its transaction when it has
    one). A file already holding every object runs nothing."""
    if not schema_present(conn, ROOM_SCHEMA):
        for stmt in ROOM_SCHEMA:
            conn.execute(stmt)


def _stamp(now: str | None) -> str:
    return now if now is not None else utc_stamp(datetime.now(timezone.utc))


def _thread_id(conn: sqlite3.Connection, value: Any, what: str) -> str:
    if not isinstance(value, str) or _THREAD_ID.fullmatch(value) is None or conn.execute(
            "SELECT 1 FROM threads WHERE thread_id = ?", (value,)).fetchone() is None:
        raise CoordError(f"{what} names no thread ({str(value)[:40]!r})")
    return value


def current_room(conn: sqlite3.Connection) -> str | None:
    """The open room thread, or None when none was opened or the last one rotated."""
    ensure(conn)
    row = conn.execute("SELECT room_thread, kind FROM room_events ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row is not None and row[1] == "opened" else None


def open_room_thread(conn: sqlite3.Connection, room_thread: str, *, by: str = "operator", now: str | None = None) -> str:
    """Record that ``room_thread`` (an existing thread) is the open room; refused while another room is open. The
    thread's creation is not this module's: a bare ``threads`` row has no revision for the store's readers, so the
    caller makes it the store's own way and names it here."""
    ensure(conn)
    _thread_id(conn, room_thread, "the room")
    if by not in _BY:
        raise CoordError(f"a room event is by {' or '.join(_BY)}")
    held = current_room(conn)
    if held is not None:
        raise CoordError(f"room {held} is open; one room is open at a time (rotate it first)")
    at = _stamp(now)
    try:
        conn.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES (?, ?, 'opened', ?, ?)",
                     (next_gseq(conn, at), room_thread, at, by))
    except sqlite3.IntegrityError as exc:
        raise CoordError(str(exc)) from None
    return room_thread


def rotate(conn: sqlite3.Connection, new_room_thread: str, *, by: str = "operator", now: str | None = None) -> str:
    """The operator's "new conversation": the open room is rotated (its proposals expire) and ``new_room_thread`` is
    opened, in the caller's one transaction. Returns the thread that rotated."""
    ensure(conn)
    old = current_room(conn)
    if old is None:
        raise CoordError("no room is open to rotate")
    _thread_id(conn, new_room_thread, "the new room")
    if by not in _BY:
        raise CoordError(f"a room event is by {' or '.join(_BY)}")
    at = _stamp(now)
    try:
        conn.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES (?, ?, 'rotated', ?, ?)",
                     (next_gseq(conn, at), old, at, by))
        conn.execute("INSERT INTO room_events (gseq, room_thread, kind, at, by) VALUES (?, ?, 'opened', ?, ?)",
                     (next_gseq(conn, at), new_room_thread, at, by))
    except sqlite3.IntegrityError as exc:
        raise CoordError(str(exc)) from None
    return old


def _state(conn: sqlite3.Connection, proposal_id: str, room_thread: str) -> str:
    row = conn.execute("SELECT kind FROM proposal_events WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if row is not None:
        return row[0]
    return "open" if current_room(conn) == room_thread else "expired"


def _proposal(conn: sqlite3.Connection, proposal_id: Any) -> Proposal:
    row = None
    if isinstance(proposal_id, str) and _PROPOSAL_ID.fullmatch(proposal_id) is not None:
        row = conn.execute("SELECT proposal_id, room_thread, launch_id, quoted, words, words_sha256, summary, about, "
                           "at FROM drop_proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise CoordError(f"no proposal {str(proposal_id)[:40]!r}")
    return Proposal(row[0], row[1], row[2], tuple(json.loads(row[3])), row[4], row[5], row[6], row[7], row[8],
                    _state(conn, row[0], row[1]))


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> Proposal:
    ensure(conn)
    return _proposal(conn, proposal_id)


def propose(conn: sqlite3.Connection, launch_id: str, quote: Sequence[str], summary: str, about: str | None = None, *,
            now: str | None = None) -> Proposal:
    """A proposal from a ROOM launch (its role compared as text) in the open room. ``quote`` is 1..:data:`QUOTE_MAX`
    ids of the OPERATOR's own messages in that room thread; the proposal's words are those messages' stored bodies,
    in the order given, joined by one blank line (a lone quote is its body byte for byte). ``summary`` is the room's own
    words, kept apart as the labelled note. Nothing is a drop yet."""
    ensure(conn)
    row = conn.execute("SELECT role, threads FROM launches WHERE launch_id = ?", (launch_id,)).fetchone() \
        if isinstance(launch_id, str) else None
    if row is None:
        raise CoordError(f"no launch {str(launch_id)[:40]!r}")
    if row[0] != "room":
        raise CoordError(f"launch {launch_id} is not a room launch; only the room proposes a drop")
    room = current_room(conn)
    if room is None or room not in json.loads(row[1]):
        raise CoordError(f"launch {launch_id} is not a launch of the open room")
    if isinstance(quote, (str, bytes)) or not isinstance(quote, Sequence) or not 1 <= len(quote) <= QUOTE_MAX \
            or len(set(quote)) != len(quote):
        raise CoordError(f"a proposal quotes 1 to {QUOTE_MAX} distinct messages of his")
    bodies = []
    for mid in quote:
        found = conn.execute("SELECT body, thread_id, author_kind FROM chat_messages WHERE message_id = ?", (mid,)).fetchone() \
            if isinstance(mid, str) and _MESSAGE_ID.fullmatch(mid) is not None else None
        if found is None:
            raise CoordError(f"no message {str(mid)[:40]!r}")
        if found[1] != room or found[2] != "operator":
            raise CoordError(f"message {mid} is not the operator's own message in the open room; the room quotes "
                             "only his words")
        bodies.append(found[0])
    words = _check_words(bodies[0] if len(bodies) == 1 else "\n\n".join(bodies))
    if not isinstance(summary, str) or not summary.strip() or len(summary) > SUMMARY_MAX or "\x00" in summary:
        raise CoordError(f"a summary is 1 to {SUMMARY_MAX} characters of text")
    try:
        summary.encode("utf-8")
    except UnicodeEncodeError:
        raise CoordError("a summary must be text UTF-8 can hold") from None
    if about is not None:
        _thread_id(conn, about, "about")
    at = _stamp(now)
    proposal_id = "pr-" + secrets.token_hex(16)
    try:
        conn.execute("INSERT INTO drop_proposals (proposal_id, gseq, room_thread, launch_id, quoted, words, "
                     "words_sha256, summary, summary_sha256, about, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (proposal_id, next_gseq(conn, at), room, launch_id, json.dumps(list(quote)), words,
                      words_sha256(words), summary, words_sha256(summary), about, at))
    except sqlite3.IntegrityError as exc:
        raise CoordError(str(exc)) from None
    return _proposal(conn, proposal_id)


def dismiss(conn: sqlite3.Connection, proposal_id: str, *, by: str = "operator", now: str | None = None) -> Proposal:
    """His "no" on a card. Refused for a proposal already committed or dismissed; an expired one may still be
    dismissed (it is shown, then put away)."""
    ensure(conn)
    p = _proposal(conn, proposal_id)
    if p.state in PROPOSAL_KINDS:
        raise CoordError(f"proposal {proposal_id} is already {p.state}")
    if by not in _BY:
        raise CoordError(f"a proposal event is by {' or '.join(_BY)}")
    conn.execute("INSERT INTO proposal_events (proposal_id, kind, drop_id, at, by) VALUES (?, 'dismissed', NULL, ?, ?)",
                 (proposal_id, _stamp(now), by))
    return _proposal(conn, proposal_id)


def commit(store: CoordStore, proposal_id: str, words: str, outbox_id: str, surface: str) -> tuple[Any, bool]:
    """His tap: the drop, from ``words`` as the card's field held them (his adopted quote, or his edit of it), through
    the store's own ``accept_drop`` carry, so the drop, the proposal's ``committed`` event and the labelled note commit
    together or not at all. ``(drop, created)``. A second commit of a settled or expired proposal refuses with nothing
    written. A resend of a stored ``outbox_id`` writes nothing and answers the stored drop, provided that drop is this
    proposal's. The note never enters the drop's words or hash."""
    if surface not in SURFACES:
        raise CoordError(f"a drop's surface is one of {', '.join(SURFACES)}")

    def carry(conn: sqlite3.Connection, now: str, **drop: Any) -> Any:
        ensure(conn)
        p = _proposal(conn, proposal_id)
        if p.state != "open":
            raise CoordError(f"proposal {proposal_id} is {p.state}; only an open proposal is committed")
        made = store._insert_drop(conn, now, **drop)
        try:
            conn.execute("INSERT INTO proposal_events (proposal_id, kind, drop_id, at, by) "
                         "VALUES (?, 'committed', ?, ?, 'operator')", (proposal_id, made.drop_id, now))
            conn.execute("INSERT INTO drop_notes (drop_id, gseq, author, room_thread, launch_id, proposal_id, body, "
                         "body_sha256, at) VALUES (?, ?, 'room', ?, ?, ?, ?, ?, ?)",
                         (made.drop_id, next_gseq(conn, now), p.room_thread, p.launch_id, proposal_id, p.summary,
                          words_sha256(p.summary), now))
        except sqlite3.IntegrityError as exc:
            raise CoordError(str(exc)) from None
        return made

    # CoordStore's own method: an EnvelopeStore's override takes no carrier, and a room drop carries no envelope
    drop, created = CoordStore.accept_drop(store, words, surface=surface, outbox_id=outbox_id, _carry=carry)
    if not created:
        conn = store._connect()
        try:
            ensure(conn)
            row = conn.execute("SELECT drop_id FROM proposal_events WHERE proposal_id = ? AND kind = 'committed'",
                               (proposal_id,)).fetchone()
        finally:
            conn.close()
        if row is None or row[0] != drop.drop_id:
            raise CoordError(f"outbox_id {outbox_id} is the drop {drop.drop_id}, not this proposal's; nothing was written")
    return drop, created
