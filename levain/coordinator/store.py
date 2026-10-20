"""levain.coordinator.store — the drop and thread store (coordinator C1).

A DROP is your words, kept verbatim, never authority: a message typed at the CLI (or, later, the phone's drop sheet)
for the coordinator to act on. The coordinator reads it and SPLITS it into one or more THREADS, one intent each (T1),
each with a kind (T2: work, ask, note, relay) and a state (T7). A thread's membership is a numbered REVISION holding
the complete sorted list of its drop ids and their sha256, so a later signature can bind to exactly one revision and
any regroup ends that binding by derivation (C0 §3.2). One drop can sit in several threads (a drop naming two problems
to fix is two work threads over one drop).

The store answers each drop with a DROP RECEIPT (the reply grammar's RECEIPT, named so ``levain consent receipts`` keeps
one meaning; C0 §8): a ``delivered`` row the moment the drop is accepted, and a ``split`` row naming its threads once it
is split; a later regroup writes a ``resplit`` row. A drop receipt is silent and attests nothing about any gated call.

The OUTBOX contract (gap 6): every drop carries an ``outbox_id`` chosen by the surface that sent it. Accepting a drop
whose ``outbox_id`` is already stored, with the same words from the same surface, writes nothing and returns the stored
drop, so a surface may resend until it sees the delivered receipt; the same id with different words is refused.

Where: the coordinator's tables live in ``dispatch.db`` beside the dispatch jobs (C0 §2: one file, so a later envelope's
liveness and the write it authorises share one transaction). The file is opened through
:class:`levain.dispatch.jobs.JobStore`, which owns its format marker, its ``store_id`` and its directory rule; this
module adds its tables as ``CREATE TABLE IF NOT EXISTS`` and never writes the format. Every table is append-only
(triggers refuse UPDATE, DELETE and a replacing INSERT); current state is the latest row.

Who: every row records the actor its writer named (``operator``, ``coordinator``, ``service``). Until S1 places the
control principal, any process of your uid can write this file, so nothing here is read as authority (C0 §2); the
store enforces only the shape of the grammar (who may close or reopen a thread, which kinds may join which threads).
Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.contracts.canon import canonical, canonical_bytes
from levain.dispatch.jobs import JobStore

__all__ = [
    "ACTORS", "KINDS", "REPLY_KINDS", "SCHEMA", "SOURCE_HOW", "STATES", "SURFACES", "CoordError", "CoordStore",
    "Drop", "DropConflict", "Part", "Reply", "Source", "Thread", "members_sha256",
]

SURFACES = ("cli", "phone", "cockpit")
SOURCE_HOW = ("none", "cwd", "picker")
KINDS = ("work", "ask", "note", "relay")
STATES = ("open", "working", "done", "closed")
ACTORS = ("operator", "coordinator", "service")
REPLY_KINDS = ("drop_receipt", "sign", "answer", "know")
_REVISION_CAUSES = ("split", "attach", "resplit")
_EVENT_FIELDS = ("state", "kind", "title", "repo")

WORDS_MAX = 20_000
_TITLE_MAX = 200
_CAUSE_MAX = 200
_REPO_MAX = 4096
# how far a surface's sent_at may run ahead of the store's clock (the phone's clock is its own)
_SENT_SKEW = timedelta(minutes=2)

_DROP_ID = re.compile(r"dr-[0-9a-f]{32}")
_THREAD_ID = re.compile(r"th-[0-9a-f]{32}")
_OUTBOX_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_SHA1 = re.compile(r"[0-9a-f]{40}")

# (from, to) -> who may take the step. The coordinator claims work and its end; you close (from the review queue)
# and reopen; the service closes a thread that is not work once it is done, and one a regroup left empty
_EDGES: dict[tuple[str, str], frozenset[str]] = {
    ("open", "working"): frozenset({"coordinator"}),
    ("open", "done"): frozenset({"coordinator"}),
    ("working", "done"): frozenset({"coordinator"}),
    ("done", "closed"): frozenset({"operator", "service"}),
    ("open", "closed"): frozenset({"operator", "service"}),
    ("working", "closed"): frozenset({"operator", "service"}),
    ("done", "open"): frozenset({"operator"}),
    ("closed", "open"): frozenset({"operator"}),
}


class CoordError(Exception):
    """A request the store refuses: a malformed drop, an unknown thread, a step the grammar does not allow."""


class DropConflict(CoordError):
    """An ``outbox_id`` already stored for different words or another surface: never a silent overwrite."""


def _append_only(table: str, what: str) -> tuple[str, ...]:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{what} is append-only'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{what} is append-only'); END")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


SCHEMA: tuple[str, ...] = (
    # a drop: your words verbatim; envelope_id arrives only with a structured envelope signed at send (C2), and
    # prior_envelope_id is written only by the S1 copy, as history
    "CREATE TABLE IF NOT EXISTS drops (drop_id TEXT PRIMARY KEY, "
    f"surface TEXT NOT NULL CHECK ({_in('surface', SURFACES)}), outbox_id TEXT NOT NULL UNIQUE, "
    "words TEXT NOT NULL, words_sha256 TEXT NOT NULL, sent_at TEXT NOT NULL, received_at TEXT NOT NULL, "
    "source_repo TEXT, source_ref TEXT, source_head TEXT, "
    f"source_how TEXT NOT NULL CHECK ({_in('source_how', SOURCE_HOW)}), "
    "envelope_id TEXT, prior_envelope_id TEXT)",
    "CREATE TABLE IF NOT EXISTS threads (thread_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
    "about TEXT REFERENCES threads(thread_id))",
    "CREATE TABLE IF NOT EXISTS thread_revisions (thread_id TEXT NOT NULL REFERENCES threads(thread_id), "
    "rev INTEGER NOT NULL, members TEXT NOT NULL, members_sha256 TEXT NOT NULL, "
    f"cause TEXT NOT NULL CHECK ({_in('cause', _REVISION_CAUSES)}), at TEXT NOT NULL, PRIMARY KEY (thread_id, rev))",
    "CREATE TABLE IF NOT EXISTS thread_events (thread_id TEXT NOT NULL REFERENCES threads(thread_id), "
    f"seq INTEGER NOT NULL, field TEXT NOT NULL CHECK ({_in('field', _EVENT_FIELDS)}), value TEXT NOT NULL, "
    f"cause TEXT NOT NULL, by TEXT NOT NULL CHECK ({_in('by', ACTORS)}), at TEXT NOT NULL, "
    "PRIMARY KEY (thread_id, seq))",
    # ref_id is a plain pointer (a drop id, later a queue request or a question), never a foreign key (C0 §2)
    "CREATE TABLE IF NOT EXISTS replies (reply_id TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(thread_id), "
    f"kind TEXT NOT NULL CHECK ({_in('kind', REPLY_KINDS)}), ref_store TEXT NOT NULL, ref_id TEXT NOT NULL, "
    "body TEXT NOT NULL, at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS replies_ref ON replies (ref_store, ref_id)",
    *_append_only("drops", "a drop"),
    *_append_only("threads", "a thread"),
    *_append_only("thread_revisions", "a thread revision"),
    *_append_only("thread_events", "a thread event"),
    *_append_only("replies", "a reply"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS drops_no_replace BEFORE INSERT ON drops WHEN EXISTS (SELECT 1 FROM drops WHERE "
    "drop_id = NEW.drop_id OR outbox_id = NEW.outbox_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a drop is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS threads_no_replace BEFORE INSERT ON threads WHEN EXISTS (SELECT 1 FROM threads "
    "WHERE thread_id = NEW.thread_id OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a thread is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS replies_no_replace BEFORE INSERT ON replies WHEN EXISTS (SELECT 1 FROM replies "
    "WHERE reply_id = NEW.reply_id OR rowid = NEW.rowid) BEGIN SELECT RAISE(ABORT, 'a reply is never replaced'); END",
    # a revision or an event is the next number of its thread: never a gap, never a replacement
    "CREATE TRIGGER IF NOT EXISTS thread_revisions_next BEFORE INSERT ON thread_revisions "
    "WHEN NEW.rev != COALESCE((SELECT MAX(rev) FROM thread_revisions WHERE thread_id = NEW.thread_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM thread_revisions WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a revision is the next rev of its thread'); END",
    "CREATE TRIGGER IF NOT EXISTS thread_events_next BEFORE INSERT ON thread_events "
    "WHEN NEW.seq != COALESCE((SELECT MAX(seq) FROM thread_events WHERE thread_id = NEW.thread_id), 0) + 1 "
    "OR EXISTS (SELECT 1 FROM thread_events WHERE rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an event is the next seq of its thread'); END",
)


def members_sha256(members: Iterable[str]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the sorted drop ids: what a revision's signature will bind."""
    return "sha256:" + hashlib.sha256(canonical_bytes(sorted(members))).hexdigest()


def _words_sha256(words: str) -> str:
    return "sha256:" + hashlib.sha256(words.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Source:
    """Where a drop was made from: ``how`` = ``cwd`` (the CLI's checked-out branch), ``picker`` (the phone sheet's
    pick) or ``none`` (no repo, a detached HEAD, a phone drop with no pick)."""
    how: str = "none"
    repo: str | None = None
    ref: str | None = None
    head: str | None = None


@dataclass(frozen=True)
class Drop:
    drop_id: str
    surface: str
    outbox_id: str
    words: str
    words_sha256: str
    sent_at: str
    received_at: str
    source: Source
    envelope_id: str | None


@dataclass(frozen=True)
class Thread:
    thread_id: str
    created_at: str
    about: str | None
    kind: str
    title: str
    repo: str | None
    state: str
    rev: int
    members: tuple[str, ...]
    members_sha256: str


@dataclass(frozen=True)
class Reply:
    reply_id: str
    thread_id: str | None
    kind: str
    ref_store: str
    ref_id: str
    body: dict[str, Any]
    at: str


@dataclass(frozen=True)
class Part:
    """One thread a drop goes into: a NEW thread (``kind`` + ``title``, ``about`` for an ask/note/relay that concerns a
    work thread) or an EXISTING one (``attach`` = its id, ``kind`` = the intent the drop adds, which must be the
    thread's own kind)."""
    kind: str | None = None
    title: str | None = None
    about: str | None = None
    attach: str | None = None


def _one_line(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CoordError(f"{name} must be 1 to {high} characters")
    if unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise CoordError(f"{name} must be one line of plain text")
    return value


def _check_words(words: Any) -> str:
    if not isinstance(words, str) or not words.strip():
        raise CoordError("a drop needs words")
    if len(words) > WORDS_MAX:
        raise CoordError(f"a drop is at most {WORDS_MAX} characters ({len(words)} given)")
    if "\x00" in words:
        raise CoordError("a drop cannot hold a NUL character")
    try:
        words.encode("utf-8")
    except UnicodeEncodeError:
        raise CoordError("a drop must be text UTF-8 can hold (it holds a lone surrogate)") from None
    return words


def _check_source(source: Source) -> Source:
    if not isinstance(source, Source) or source.how not in SOURCE_HOW:
        raise CoordError(f"a drop's source is one of {', '.join(SOURCE_HOW)}")
    if source.how == "none":
        if (source.repo, source.ref, source.head) != (None, None, None):
            raise CoordError("a drop with no source records no repo, ref or head")
        return source
    if not isinstance(source.repo, str) or not source.repo.startswith("/") or len(source.repo) > _REPO_MAX \
            or "\x00" in source.repo:
        raise CoordError("a drop's source repo is an absolute path")
    if not isinstance(source.ref, str) or not source.ref.startswith("refs/heads/") or len(source.ref) > 1024 \
            or unshowable.first_hidden(source.ref, allow="", selectors=False) is not None:
        raise CoordError("a drop's source ref is a branch, refs/heads/<name>")
    if not isinstance(source.head, str) or _SHA1.fullmatch(source.head) is None:
        raise CoordError("a drop's source head is a full 40-hex commit sha")
    return source


class CoordStore:
    """The coordinator's tables in ``<directory>/dispatch.db`` (the consent directory by default). Every write is one
    ``BEGIN IMMEDIATE``; the clock is read only once the write lock is held."""

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        # the file, its format marker, its store_id and its directory rule are the dispatch store's: one owner
        self._jobs = JobStore(directory, clock=clock)
        self.directory = self._jobs.directory
        self.path = self._jobs.path
        self._clock = clock
        self._ready = False

    # --- store ---------------------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = self._jobs._connect()
        if not self._ready:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in SCHEMA:
                        conn.execute(stmt)
                    conn.execute("COMMIT")
                except BaseException:
                    _rollback(conn)
                    raise
            except BaseException:
                conn.close()
                raise
            self._ready = True
        return conn

    @contextmanager
    def _write(self) -> Iterator[tuple[sqlite3.Connection, str]]:
        with write_transaction(self._connect) as conn:
            yield conn, self._stamp()

    def _stamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CoordError("the coordinator store's clock returned a naive datetime")
        return utc_stamp(now)

    # --- drops ---------------------------------------------------------------------------------------------------
    def accept_drop(self, words: str, *, surface: str, outbox_id: str | None = None, sent_at: str | None = None,
                    source: Source = Source()) -> tuple[Drop, bool]:
        """Store a drop and its ``delivered`` drop receipt in one transaction; ``(drop, True)``. A resend of a stored
        ``outbox_id`` with the same words from the same surface writes nothing: ``(stored drop, False)``."""
        words = _check_words(words)
        if surface not in SURFACES:
            raise CoordError(f"a drop's surface is one of {', '.join(SURFACES)}")
        if outbox_id is None:
            outbox_id = "ob-" + secrets.token_hex(16)
        if not isinstance(outbox_id, str) or _OUTBOX_ID.fullmatch(outbox_id) is None:
            raise CoordError("an outbox_id is 8 to 128 of A-Z a-z 0-9 . _ - (starting with a letter or digit)")
        source = _check_source(source)
        sha = _words_sha256(words)
        with self._write() as (conn, now):
            row = conn.execute("SELECT drop_id FROM drops WHERE outbox_id = ?", (outbox_id,)).fetchone()
            if row is not None:
                stored = self._drop(conn, row[0])
                if stored.words_sha256 != sha or stored.surface != surface:
                    raise DropConflict(f"outbox_id {outbox_id} is already stored for a different drop "
                                       f"({stored.drop_id}); nothing was written")
                return stored, False
            if sent_at is None:
                sent_at = now
            else:
                try:
                    sent = parse_stamp(sent_at)
                except ConsentError as exc:
                    raise CoordError(f"sent_at: {exc}") from None
                if sent > parse_stamp(now) + _SENT_SKEW:
                    raise CoordError(f"sent_at {sent_at} is ahead of the store's clock ({now})")
            drop_id = "dr-" + secrets.token_hex(16)
            conn.execute(
                "INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
                "source_repo, source_ref, source_head, source_how, envelope_id, prior_envelope_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (drop_id, surface, outbox_id, words, sha, sent_at, now, source.repo, source.ref, source.head,
                 source.how))
            self._reply(conn, None, "drop_receipt", "drops", drop_id,
                        {"stage": "delivered", "drop_id": drop_id, "outbox_id": outbox_id}, now)
            return self._drop(conn, drop_id), True

    # --- threads -------------------------------------------------------------------------------------------------
    def split(self, drop_id: str, parts: Sequence[Part], *, by: str = "coordinator") -> list[Thread]:
        """The drop's first split (T1): each part a new thread holding it, or the drop attached to an existing open
        thread. One transaction; writes the drop's ``split`` drop receipt. A drop already split is regrouped with
        :meth:`add`, :meth:`merge` or :meth:`detach` instead."""
        by = self._actor(by, ("coordinator", "operator"))
        if not parts:
            raise CoordError("a split names at least one thread")
        with self._write() as (conn, now):
            self._must_drop(conn, drop_id)
            if self._receipts(conn, drop_id, "split"):
                raise CoordError(f"drop {drop_id} is already split; regroup it with add, merge or detach")
            touched = [self._place(conn, drop_id, part, by, now, first=True) for part in parts]
            if len(set(touched)) != len(touched):
                raise CoordError("a split names the same thread twice")
            self._drop_receipt(conn, drop_id, "split", now)
            return [self._thread(conn, t) for t in touched]

    def add(self, drop_id: str, part: Part, *, by: str = "operator") -> Thread:
        """Put an already-split drop into one more thread (a new one, or an existing open one): the receipt's one-tap
        fix for a split that missed an intent, and how a later drop joins work in flight. Writes a ``resplit``
        drop receipt."""
        by = self._actor(by, ("coordinator", "operator"))
        with self._write() as (conn, now):
            self._must_drop(conn, drop_id)
            if not self._receipts(conn, drop_id, "split"):
                raise CoordError(f"drop {drop_id} has not been split yet; split it first")
            thread_id = self._place(conn, drop_id, part, by, now, first=False)
            self._drop_receipt(conn, drop_id, "resplit", now)
            return self._thread(conn, thread_id)

    def merge(self, into: str, other: str, *, by: str = "operator") -> Thread:
        """Fold ``other`` into ``into`` (the receipt's one-tap fix for a split that cut one intent in two): ``into``
        gets the union of both memberships as a new revision, ``other`` a new, empty revision and is closed by the
        service, its cause naming ``into`` and who merged. Both must be open threads of the same kind."""
        by = self._actor(by, ("coordinator", "operator"))
        if into == other:
            raise CoordError("a thread cannot merge into itself")
        with self._write() as (conn, now):
            a, b = self._must_thread(conn, into), self._must_thread(conn, other)
            for t in (a, b):
                if t.state in ("done", "closed"):
                    raise CoordError(f"thread {t.thread_id} is {t.state}; reopen it first")
            if a.kind != b.kind:
                raise CoordError(f"a {b.kind} thread never joins a {a.kind} thread")
            self._revise(conn, into, set(a.members) | set(b.members), "resplit", now)
            self._revise(conn, other, (), "resplit", now)
            self._event(conn, other, "state", "closed", f"merged into {into} by {by}", "service", now)
            for d in sorted(set(b.members)):
                self._drop_receipt(conn, d, "resplit", now)
            return self._thread(conn, into)

    def detach(self, thread_id: str, drop_id: str, *, by: str = "operator") -> Thread:
        """Take one drop out of a thread (a new revision without it). A thread left with no drops is closed by the
        service, its cause naming who emptied it. The drop must stay in at least one other thread: a drop is never orphaned."""
        by = self._actor(by, ("coordinator", "operator"))
        with self._write() as (conn, now):
            self._must_drop(conn, drop_id)
            t = self._must_thread(conn, thread_id)
            if drop_id not in t.members:
                raise CoordError(f"drop {drop_id} is not in thread {thread_id}")
            if not [x for x in self._threads_of(conn, drop_id) if x != thread_id]:
                raise CoordError(f"drop {drop_id} is in no other thread; add it to one before detaching it")
            left = set(t.members) - {drop_id}
            self._revise(conn, thread_id, left, "resplit", now)
            if not left and t.state != "closed":
                self._event(conn, thread_id, "state", "closed", f"emptied by {by}", "service", now)
            self._drop_receipt(conn, drop_id, "resplit", now)
            return self._thread(conn, thread_id)

    def set_state(self, thread_id: str, state: str, *, by: str, cause: str = "") -> Thread:
        """One T7 step, if :data:`_EDGES` lets ``by`` take it from the state read in this transaction. A thread that
        is not work closes itself (by the service) once done; a thread that holds no drops cannot be reopened."""
        if state not in STATES:
            raise CoordError(f"a thread's state is one of {', '.join(STATES)}")
        by = self._actor(by, ACTORS)
        cause = self._cause(cause)
        with self._write() as (conn, now):
            t = self._must_thread(conn, thread_id)
            allowed = _EDGES.get((t.state, state))
            if allowed is None:
                raise CoordError(f"a thread cannot go from {t.state} to {state}")
            if by not in allowed:
                raise CoordError(f"{by} cannot take a thread from {t.state} to {state} (only {', '.join(sorted(allowed))})")
            if by == "service" and state == "closed" and t.kind == "work":
                raise CoordError("the service never closes a work thread that still holds drops; you do")
            if state == "open" and not t.members:
                raise CoordError(f"thread {thread_id} holds no drops; it cannot be reopened")
            self._event(conn, thread_id, "state", state, cause or state, by, now)
            if state == "done" and t.kind != "work":
                self._event(conn, thread_id, "state", "closed", f"{t.kind} closes itself once done", "service", now)
            return self._thread(conn, thread_id)

    def set_field(self, thread_id: str, field: str, value: str, *, by: str = "coordinator", cause: str = "") -> Thread:
        """Re-kind, re-title or re-scope (repo) a thread, recorded as an event. A thread with ``about`` set (an ask,
        note or relay concerning a work thread) never becomes work."""
        if field not in ("kind", "title", "repo"):
            raise CoordError("set_field changes kind, title or repo")
        by = self._actor(by, ("coordinator", "operator"))
        cause = self._cause(cause)
        with self._write() as (conn, now):
            t = self._must_thread(conn, thread_id)
            if field == "kind":
                if value not in KINDS:
                    raise CoordError(f"a thread's kind is one of {', '.join(KINDS)}")
                if value == "work" and t.about is not None:
                    raise CoordError(f"thread {thread_id} is about {t.about}; it never becomes a work thread")
            elif field == "title":
                value = _one_line("a thread's title", value, _TITLE_MAX)
            elif not isinstance(value, str) or not value.startswith("/") or len(value) > _REPO_MAX \
                    or unshowable.first_hidden(value, allow="", selectors=False) is not None:
                raise CoordError("a thread's repo is an absolute path, one line")
            self._event(conn, thread_id, field, value, cause or f"{field} set", by, now)
            return self._thread(conn, thread_id)

    # --- reading -------------------------------------------------------------------------------------------------
    def drop(self, drop_id: str) -> Drop:
        with read_transaction(self._connect) as conn:
            return self._must_drop(conn, drop_id)

    def drops(self, limit: int = 50) -> list[Drop]:
        with read_transaction(self._connect) as conn:
            ids = [r[0] for r in conn.execute("SELECT drop_id FROM drops ORDER BY rowid DESC LIMIT ?", (limit,))]
            return [self._drop(conn, d) for d in ids]

    def thread(self, thread_id: str) -> Thread:
        with read_transaction(self._connect) as conn:
            return self._must_thread(conn, thread_id)

    def threads(self, *, include_closed: bool = False) -> list[Thread]:
        with read_transaction(self._connect) as conn:
            ids = [r[0] for r in conn.execute("SELECT thread_id FROM threads ORDER BY rowid")]
            out = [self._thread(conn, t) for t in ids]
        return out if include_closed else [t for t in out if t.state != "closed"]

    def threads_of(self, drop_id: str) -> list[Thread]:
        with read_transaction(self._connect) as conn:
            self._must_drop(conn, drop_id)
            return [self._thread(conn, t) for t in self._threads_of(conn, drop_id)]

    def receipts(self, drop_id: str) -> list[Reply]:
        """Every drop receipt of ``drop_id``, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_drop(conn, drop_id)
            return self._receipts(conn, drop_id, None)

    def revisions(self, thread_id: str) -> list[tuple[int, tuple[str, ...], str, str]]:
        """``(rev, members, members_sha256, cause)`` for every revision of a thread, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_thread(conn, thread_id)
            return [(r[0], tuple(json.loads(r[1])), r[2], r[3]) for r in conn.execute(
                "SELECT rev, members, members_sha256, cause FROM thread_revisions WHERE thread_id = ? ORDER BY rev",
                (thread_id,))]

    def events(self, thread_id: str) -> list[tuple[int, str, str, str, str, str]]:
        """``(seq, field, value, cause, by, at)`` for every event of a thread, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_thread(conn, thread_id)
            return list(conn.execute("SELECT seq, field, value, cause, by, at FROM thread_events WHERE thread_id = ? "
                                     "ORDER BY seq", (thread_id,)))

    # --- inside a transaction ------------------------------------------------------------------------------------
    @staticmethod
    def _actor(by: Any, allowed: Iterable[str]) -> str:
        allowed = tuple(allowed)
        if by not in allowed:
            raise CoordError(f"by is one of {', '.join(allowed)}")
        return by

    @staticmethod
    def _cause(cause: Any) -> str:
        if not isinstance(cause, str) or len(cause) > _CAUSE_MAX:
            raise CoordError(f"a cause is text of at most {_CAUSE_MAX} characters")
        if cause and unshowable.first_hidden(cause, allow="", selectors=False) is not None:
            raise CoordError("a cause must be one line of plain text")
        return cause

    @staticmethod
    def _drop(conn: sqlite3.Connection, drop_id: str) -> Drop:
        r = conn.execute("SELECT drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, source_how, "
                         "source_repo, source_ref, source_head, envelope_id FROM drops WHERE drop_id = ?",
                         (drop_id,)).fetchone()
        return Drop(drop_id=r[0], surface=r[1], outbox_id=r[2], words=r[3], words_sha256=r[4], sent_at=r[5],
                    received_at=r[6], source=Source(r[7], r[8], r[9], r[10]), envelope_id=r[11])

    def _must_drop(self, conn: sqlite3.Connection, drop_id: Any) -> Drop:
        if not isinstance(drop_id, str) or _DROP_ID.fullmatch(drop_id) is None or conn.execute(
                "SELECT 1 FROM drops WHERE drop_id = ?", (drop_id,)).fetchone() is None:
            raise CoordError(f"no drop {str(drop_id)[:40]!r}")
        return self._drop(conn, drop_id)

    @staticmethod
    def _thread(conn: sqlite3.Connection, thread_id: str) -> Thread:
        created_at, about = conn.execute("SELECT created_at, about FROM threads WHERE thread_id = ?",
                                         (thread_id,)).fetchone()
        rev, members, msha = conn.execute("SELECT rev, members, members_sha256 FROM thread_revisions "
                                          "WHERE thread_id = ? ORDER BY rev DESC LIMIT 1", (thread_id,)).fetchone()
        latest: dict[str, str] = {}
        for field, value in conn.execute("SELECT field, value FROM thread_events WHERE thread_id = ? ORDER BY seq",
                                         (thread_id,)):
            latest[field] = value
        return Thread(thread_id=thread_id, created_at=created_at, about=about, kind=latest["kind"],
                      title=latest["title"], repo=latest.get("repo") or None, state=latest["state"], rev=rev,
                      members=tuple(json.loads(members)), members_sha256=msha)

    def _must_thread(self, conn: sqlite3.Connection, thread_id: Any) -> Thread:
        if not isinstance(thread_id, str) or _THREAD_ID.fullmatch(thread_id) is None or conn.execute(
                "SELECT 1 FROM threads WHERE thread_id = ?", (thread_id,)).fetchone() is None:
            raise CoordError(f"no thread {str(thread_id)[:40]!r}")
        return self._thread(conn, thread_id)

    @staticmethod
    def _threads_of(conn: sqlite3.Connection, drop_id: str) -> list[str]:
        """The threads whose CURRENT revision holds ``drop_id``, in creation order."""
        out = []
        for thread_id, members in conn.execute(
                "SELECT r.thread_id, r.members FROM thread_revisions r JOIN threads t ON t.thread_id = r.thread_id "
                "WHERE r.rev = (SELECT MAX(rev) FROM thread_revisions l WHERE l.thread_id = r.thread_id) "
                "ORDER BY t.rowid"):
            if drop_id in json.loads(members):
                out.append(thread_id)
        return out

    def _place(self, conn: sqlite3.Connection, drop_id: str, part: Part, by: str, now: str, *, first: bool) -> str:
        if not isinstance(part, Part):
            raise CoordError("a split part is a Part")
        if part.attach is not None:
            if part.about is not None or part.title is not None:
                raise CoordError("a part attaching to an existing thread names no title or about")
            t = self._must_thread(conn, part.attach)
            if t.state in ("done", "closed"):
                raise CoordError(f"thread {t.thread_id} is {t.state}; reopen it first")
            # the attach declares the intent it adds: an ask or note never joins a work thread (C0 §3.2), so its
            # drop never moves that thread's revision, and the drop of an ask ABOUT this thread never joins it either
            if part.kind not in KINDS:
                raise CoordError(f"an attach names the kind of intent it adds, one of {', '.join(KINDS)}")
            if part.kind != t.kind:
                raise CoordError(f"a {part.kind} part never joins a {t.kind} thread")
            if t.kind == "work" and conn.execute(
                    "SELECT 1 FROM threads WHERE about = ? LIMIT 1", (t.thread_id,)).fetchone() is not None \
                    and any(self._thread(conn, x).about == t.thread_id for x in self._threads_of(conn, drop_id)):
                raise CoordError(f"drop {drop_id} asks about thread {t.thread_id}; it never joins it")
            if drop_id in t.members:
                raise CoordError(f"drop {drop_id} is already in thread {t.thread_id}")
            self._revise(conn, t.thread_id, set(t.members) | {drop_id}, "attach", now)
            return t.thread_id
        if part.kind not in KINDS:
            raise CoordError(f"a new thread's kind is one of {', '.join(KINDS)}")
        title = _one_line("a thread's title", part.title, _TITLE_MAX)
        if part.about is not None:
            if part.kind == "work":
                raise CoordError("a work thread is about nothing else; only an ask, note or relay carries about")
            self._must_thread(conn, part.about)
        thread_id = "th-" + secrets.token_hex(16)
        conn.execute("INSERT INTO threads (thread_id, created_at, about) VALUES (?, ?, ?)",
                     (thread_id, now, part.about))
        self._revise(conn, thread_id, {drop_id}, "split" if first else "resplit", now)
        self._event(conn, thread_id, "state", "open", "split", by, now)
        self._event(conn, thread_id, "kind", part.kind, "split", by, now)
        self._event(conn, thread_id, "title", title, "split", by, now)
        return thread_id

    @staticmethod
    def _revise(conn: sqlite3.Connection, thread_id: str, members: Iterable[str], cause: str, now: str) -> None:
        ordered = sorted(members)
        rev = conn.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM thread_revisions WHERE thread_id = ?",
                           (thread_id,)).fetchone()[0]
        conn.execute("INSERT INTO thread_revisions (thread_id, rev, members, members_sha256, cause, at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (thread_id, rev, canonical(ordered), members_sha256(ordered),
                                                   cause, now))

    @staticmethod
    def _event(conn: sqlite3.Connection, thread_id: str, field: str, value: str, cause: str, by: str,
               now: str) -> None:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM thread_events WHERE thread_id = ?",
                           (thread_id,)).fetchone()[0]
        conn.execute("INSERT INTO thread_events (thread_id, seq, field, value, cause, by, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (thread_id, seq, field, value, cause, by, now))

    @staticmethod
    def _reply(conn: sqlite3.Connection, thread_id: str | None, kind: str, ref_store: str, ref_id: str,
               body: dict[str, Any], now: str) -> str:
        reply_id = "rp-" + secrets.token_hex(16)
        conn.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (reply_id, thread_id, kind, ref_store, ref_id, canonical(body),
                                                      now))
        return reply_id

    def _drop_receipt(self, conn: sqlite3.Connection, drop_id: str, stage: str, now: str) -> None:
        threads = [self._thread(conn, t) for t in self._threads_of(conn, drop_id)]
        self._reply(conn, None, "drop_receipt", "drops", drop_id, {
            "stage": stage, "drop_id": drop_id,
            "threads": [{"thread_id": t.thread_id, "kind": t.kind, "title": t.title, "rev": t.rev} for t in threads],
        }, now)

    @staticmethod
    def _receipts(conn: sqlite3.Connection, drop_id: str, stage: str | None) -> list[Reply]:
        out = []
        for r in conn.execute("SELECT reply_id, thread_id, kind, ref_store, ref_id, body, at FROM replies "
                              "WHERE ref_store = 'drops' AND ref_id = ? AND kind = 'drop_receipt' ORDER BY rowid",
                              (drop_id,)):
            body = json.loads(r[5])
            if stage is None or body.get("stage") == stage:
                out.append(Reply(r[0], r[1], r[2], r[3], r[4], body, r[6]))
        return out
