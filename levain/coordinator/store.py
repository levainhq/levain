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

Who: every row records the actor its writer NAMED (``operator``, ``coordinator``, ``service``): a label, never a
credential. Until S1 places the control principal, any process of your uid can write this file, so nothing here is read
as authority (C0 §2). A drop's intent (work, ask, ...) is the coordinator's inference, recorded as its thread's kind and
never verified. What the store holds structurally, on every path that changes a membership (:meth:`CoordStore._revise`):
a drop that asks about a thread is never added to it; ``about`` names an open work thread; a re-kind never crosses the work
boundary; a kind or repo change writes a new revision, so anything bound to the old one ends by derivation.

[seat] choices the design does not state (C1, 1020+19): the T7 edges in :data:`_EDGES`; merge, detach and add as the
regroup operations; merges only between threads of one kind, one ``about`` and compatible repos; a drop is never left
in no open thread; the size and skew limits below. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import BUSY_WAIT_S, _set_wal, open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, parse_stamp, utc_stamp
from levain.contracts.canon import canonical, canonical_bytes
from levain.dispatch.jobs import JobStore

__all__ = [
    "ACTORS", "KINDS", "REPLY_KINDS", "SCHEMA", "SOURCE_HOW", "STATES", "SURFACES", "CoordError", "CoordStore",
    "Drop", "DropConflict", "Part", "Reply", "Source", "Thread", "check_source", "check_words", "members_sha256",
    "words_sha256",
]

SURFACES = ("cli", "phone", "cockpit")
SOURCE_HOW = ("none", "cwd", "picker")
KINDS = ("work", "ask", "note", "relay")
STATES = ("open", "working", "done", "closed")
ACTORS = ("operator", "coordinator", "service")
REPLY_KINDS = ("drop_receipt", "sign", "answer", "know")
_REVISION_CAUSES = ("split", "attach", "resplit", "rescope")
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
_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")      # a SHA-1 or a SHA-256 repository

# (from, to) -> who may take the step. The coordinator claims work and its end; you close (from the review queue)
# and reopen; the service closes a thread that is not work once it is done, and one a regroup left empty
_EDGES: dict[tuple[str, str], frozenset[str]] = {
    # the service takes it only in a merge, moving the claim on the merged-away thread's work to the one it joins
    ("open", "working"): frozenset({"coordinator", "service"}),
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


class StoreAbsent(CoordError):
    """A read-only open found no coordinator store to read: no directory, no file, or a file without the coordinator's
    current tables. Nothing was created."""


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
    # one store-wide order for every authority and event row: never wall-clock (C0 §3.4)
    "CREATE TABLE IF NOT EXISTS gseq (gseq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL)",
    *_append_only("gseq", "the sequence"),
    "CREATE TRIGGER IF NOT EXISTS gseq_no_replace BEFORE INSERT ON gseq WHEN EXISTS (SELECT 1 FROM gseq WHERE "
    "gseq = NEW.gseq) BEGIN SELECT RAISE(ABORT, 'the sequence is never replaced'); END",
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


def next_gseq(conn: sqlite3.Connection, now: str) -> int:
    """The next number of the store-wide sequence, taken inside the caller's writing transaction."""
    return conn.execute("INSERT INTO gseq (at) VALUES (?)", (now,)).lastrowid


def _replies_gseq(conn: sqlite3.Connection) -> None:
    """The additive migration (CH-D1 §2.8): ``replies`` gains ``gseq``, NULL on every reply written before it, so the
    chat timeline can merge a reply into the store-wide order. SQLite adds no UNIQUE column, so uniqueness is an index,
    and a replacing INSERT that reuses a gseq is refused by its own trigger (an INSERT OR REPLACE on a unique index
    deletes the other row without firing a delete trigger). Runs in the opener's schema transaction."""
    if "gseq" not in {r[1] for r in conn.execute("PRAGMA table_info(replies)")}:
        conn.execute("ALTER TABLE replies ADD COLUMN gseq INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS replies_gseq ON replies (gseq)")
    conn.execute("CREATE TRIGGER IF NOT EXISTS replies_gseq_no_replace BEFORE INSERT ON replies WHEN NEW.gseq IS NOT "
                 "NULL AND EXISTS (SELECT 1 FROM replies WHERE gseq = NEW.gseq) "
                 "BEGIN SELECT RAISE(ABORT, 'a reply is never replaced'); END")


_CREATES = re.compile(r"CREATE (?:UNIQUE )?(?:TABLE|INDEX|TRIGGER|VIEW) IF NOT EXISTS (\w+)")


def _names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}


def schema_present(conn: sqlite3.Connection, statements: Iterable[str]) -> bool:
    """Whether every object ``statements`` creates is already in the file, by name: a reader that finds them all runs
    none of them, so it never takes the write lock to create nothing. False for a statement this cannot name. A
    present object is not re-checked against its statement, as ``CREATE ... IF NOT EXISTS`` itself never is."""
    have = _names(conn)
    for stmt in statements:
        m = _CREATES.match(stmt)
        if m is None or m.group(1) not in have:
            return False
    return True


def members_sha256(members: Iterable[str]) -> str:
    """``sha256:<hex>`` over the canonical JSON of the sorted drop ids: what a revision's signature will bind."""
    return "sha256:" + hashlib.sha256(canonical_bytes(sorted(members))).hexdigest()


def words_sha256(words: str) -> str:
    """``sha256:<hex>`` of a drop's exact words, as stored and as an envelope body names them."""
    return "sha256:" + hashlib.sha256(words.encode("utf-8")).hexdigest()


_words_sha256 = words_sha256


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
    work thread; in a split, ``"@N"`` names the split's own N-th part) or an EXISTING one (``attach`` = its id, ``kind``
    = the intent the drop adds, which must be the thread's own kind)."""
    kind: str | None = None
    title: str | None = None
    about: str | None = None
    attach: str | None = None


def _sibling(part: Any) -> bool:
    return isinstance(part, Part) and isinstance(part.about, str) and part.about.startswith("@")


def _sibling_index(part: Part, n: int) -> int:
    """``about="@N"`` (1-based) names the N-th part of the same split: the work thread an ask in one drop concerns."""
    raw = part.about[1:]
    if re.fullmatch(r"[0-9]{1,4}", raw) is None or not 1 <= int(raw) <= n:
        raise CoordError(f"about @N names a part of this split, 1 to {n} (got {part.about[:20]!r})")
    return int(raw) - 1


def _one_line(name: str, value: Any, high: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > high:
        raise CoordError(f"{name} must be 1 to {high} characters")
    if unshowable.first_hidden(value, allow="", selectors=False) is not None:
        raise CoordError(f"{name} must be one line of plain text")
    return value


def check_words(words: Any) -> str:
    """The words, if a drop can hold them; raises CoordError saying why not (checked before anything is signed)."""
    return _check_words(words)


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


def check_source(source: Source) -> Source:
    if not isinstance(source, Source) or source.how not in SOURCE_HOW:
        raise CoordError(f"a drop's source is one of {', '.join(SOURCE_HOW)}")
    if source.how == "none":
        if (source.repo, source.ref, source.head) != (None, None, None):
            raise CoordError("a drop with no source records no repo, ref or head")
        return source
    if not isinstance(source.repo, str) or not source.repo.startswith("/") or len(source.repo) > _REPO_MAX \
            or unshowable.first_hidden(source.repo, allow="", selectors=False) is not None:
        raise CoordError("a drop's source repo is an absolute path")
    if not isinstance(source.ref, str) or not source.ref.startswith("refs/heads/") or len(source.ref) > 1024 \
            or source.ref == "refs/heads/" or source.ref.endswith("/") \
            or unshowable.first_hidden(source.ref, allow="", selectors=False) is not None:
        raise CoordError("a drop's source ref is a branch, refs/heads/<name>")
    if not isinstance(source.head, str) or _OID.fullmatch(source.head) is None:
        raise CoordError("a drop's source head is a full commit id (40 or 64 hex)")
    try:
        source.repo.encode("utf-8")
        source.ref.encode("utf-8")
    except UnicodeEncodeError:
        raise CoordError("a drop's source repo and ref must be text UTF-8 can hold") from None
    return source


class CoordStore:
    """The coordinator's tables in ``<directory>/dispatch.db`` (the consent directory by default). Every write is one
    ``BEGIN IMMEDIATE``; the clock is read only once the write lock is held."""

    # a subclass that adds tables (the envelope store, C2) extends this; every statement is IF NOT EXISTS
    _SCHEMA: tuple[str, ...] = SCHEMA

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        # the file, its format marker, its store_id and its directory rule are the dispatch store's: one owner
        self._jobs = JobStore(directory, clock=clock)
        self.directory = self._jobs.directory
        self.path = self._jobs.path
        self._clock = clock
        self._ready = False
        # drop id -> the coordinator launch its own transaction asked for (read by woken_launch, this process only)
        self._woken: dict[str, str] = {}

    # --- store ---------------------------------------------------------------------------------------------------
    def _statements(self, *, base: bool = False, lifecycle: bool = True) -> tuple[str, ...]:
        """The schema this opener makes. ``base`` is the plain coordinator's alone, without a subclass's added tables.
        ``lifecycle=False`` leaves out C13's additive tables (:meth:`_schema_current`'s reader gate)."""
        from levain.coordinator.lease import LEASE_SCHEMA
        from levain.coordinator.first_reader import FIRST_READER_SCHEMA
        from levain.coordinator.ready import LIFECYCLE_SCHEMA
        from levain.coordinator.review import REVIEW_SCHEMA
        from levain.coordinator.teamdrafts import DRAFT_SCHEMA

        # the lease is checked by every opener's coordinator writes, so its tables are every opener's; C13's lifecycle
        # tables (ready items + their journal) are additive, IF NOT EXISTS, like every statement here
        return (*(SCHEMA if base else self._SCHEMA), *LEASE_SCHEMA, *DRAFT_SCHEMA, *REVIEW_SCHEMA, *FIRST_READER_SCHEMA,
                *(LIFECYCLE_SCHEMA if lifecycle else ()))

    def _connect(self, *, create: bool = True) -> sqlite3.Connection:
        """A connection with this opener's schema ensured, once per store object (:meth:`_ensure`). ``create=False``
        never makes the directory, the file or a coordinator store (:meth:`_open_existing`); a coordinator store already
        there is still brought current, as every opener's is."""
        conn = self._jobs._connect() if create else self._open_existing()
        if not self._ready:
            try:
                self._ensure(conn, create=create)
            except BaseException:
                conn.close()
                raise
            self._ready = True
        return conn

    def _ensure(self, conn: sqlite3.Connection, *, create: bool) -> None:
        """The ONE ensure-schema path: in one ``BEGIN IMMEDIATE``, R4's ``review_events`` rebuild
        (:func:`levain.coordinator.review.migrate_events`), every statement of this opener's schema, the review events'
        one-signature index (:func:`levain.coordinator.review.ensure_one_signature`), the replies' sequence; after a
        rebuild, ``PRAGMA integrity_check`` must answer ``ok`` before the transaction commits (any failure rolls all of
        it back). ``create=False``: a file holding no coordinator store is :class:`StoreAbsent`,
        decided under the write lock, and nothing is made."""
        from levain.coordinator.review import ensure_one_signature, migrate_events

        if not create:
            if "threads" not in _names(conn):
                raise StoreAbsent(f"{self.path} holds no coordinator store")
            JobStore._ensure_schema(conn)      # the dispatch store's own format steps, as its own opener runs them
        conn.execute("BEGIN IMMEDIATE")
        try:
            if not create and "threads" not in _names(conn):
                raise StoreAbsent(f"{self.path} holds no coordinator store")
            rebuilt = migrate_events(conn)
            for stmt in self._statements():
                conn.execute(stmt)
            ensure_one_signature(conn)
            _replies_gseq(conn)
            if rebuilt:
                check = conn.execute("PRAGMA integrity_check").fetchall()
                if check != [("ok",)]:
                    raise CoordError(f"the review_events rebuild left {self.path} failing its integrity check "
                                     f"({check[:3]}); nothing was changed")
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    def _open_existing(self) -> sqlite3.Connection:
        """A read-write connection to the coordinator's file that never makes it: the directory rule read with
        ``lstat`` (:meth:`_check_directory_readonly`), the file opened ``mode=rw`` (SQLite creates no file in that
        mode), open_durable's PRAGMAs (it takes no URI). Raises :class:`StoreAbsent` when there is no file to open."""
        self._check_directory_readonly()
        if not self.path.exists():
            raise StoreAbsent(f"no coordinator store at {self.path}")
        try:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=BUSY_WAIT_S,
                                   isolation_level=None)
        except sqlite3.OperationalError:
            if not self.path.exists():          # removed between the check and the open
                raise StoreAbsent(f"no coordinator store at {self.path}") from None
            raise
        try:
            _set_wal(conn)
            conn.execute(f"PRAGMA busy_timeout = {int(BUSY_WAIT_S * 1000)}")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA fullfsync = ON")
            conn.execute("PRAGMA checkpoint_fullfsync = ON")
            conn.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    def _read_connect(self) -> sqlite3.Connection:
        """A connection for a READ that takes no write lock (CH-D1a L3 r1): when every object of this opener's schema
        and the dispatch store's is already in the file, at the dispatch store's current format, nothing is created,
        so a reader (``levain chat show``, a composer's poll, each a fresh store) never waits on the coordinator's
        writer. A file missing any of it is made by :meth:`_connect`, under the lock: a first use."""
        if self._ready or not self.path.exists():
            return self._connect()
        self._jobs._check_directory()
        conn = open_durable(self.path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            current = self._schema_current(conn)
        except BaseException:
            conn.close()
            raise
        if current:
            return conn
        conn.close()
        return self._connect()

    def _check_directory_readonly(self) -> None:
        """:meth:`JobStore._check_directory`'s rule (a directory of this uid's own, not a link, closed to group and
        others) read with ``lstat`` and never made: a directory removed between a check and an open stays removed.
        Raises :class:`StoreAbsent` when there is none."""
        from levain.dispatch.jobs import _ACCESS_BY_OTHERS, JobStoreError

        try:
            st = os.lstat(self.directory)
        except FileNotFoundError:
            raise StoreAbsent(f"no coordinator store at {self.path}") from None
        if not stat.S_ISDIR(st.st_mode):
            raise JobStoreError(f"dispatch store directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise JobStoreError(f"dispatch store directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise JobStoreError(f"dispatch store directory {self.directory} is reachable by group or others "
                                f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def open_readonly(self, busy_s: float | None = None) -> sqlite3.Connection:
        """The ONE read-only connection to the coordinator's file, for every surface that only displays it (the review
        queue, "waiting on you", the morning card, the cockpit panels, the phone route). It runs the write path's
        directory rule (:meth:`_check_directory_readonly`) but never makes the directory, the file or a table: opened
        ``mode=ro`` through ``Path.as_uri()`` (a ``?`` or ``#`` in the path stays in the path) and ``query_only``, and it
        takes no write lock, so a reader never waits on the coordinator's writer. Raises :class:`StoreAbsent` when
        there is nothing current to read; the caller closes the connection. ``busy_s`` (default
        :data:`BUSY_WAIT_S`) is how long it waits on a lock: a caller on a deadline (the band's hook) passes what is
        left of it, and a lock held past it raises SQLite's busy error."""
        self._check_directory_readonly()
        if not self.path.exists():
            raise StoreAbsent(f"no coordinator store at {self.path}")
        wait = BUSY_WAIT_S if busy_s is None else max(0.0, min(float(busy_s), BUSY_WAIT_S))
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=wait,
                               isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout = {int(wait * 1000)}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA query_only = ON")
            # a display reads only the base tables: a store a plain CoordStore made (team drafts) has no envelope tables
            if not self._schema_current(conn, base=True):
                raise StoreAbsent(f"{self.path} does not hold the coordinator's current tables")
        except BaseException:
            conn.close()
            raise
        return conn

    def coordinator_present(self) -> bool:
        """Whether a coordinator store is here, read without making or migrating anything: the directory rule read
        with ``lstat``, the file opened ``mode=ro`` and ``query_only``, True when it holds the coordinator's
        ``threads`` table (an older format still counts: a writer brings it current). False for no directory, no file,
        or a dispatch-only file; a directory that breaks the rule raises, as every opener's does."""
        try:
            self._check_directory_readonly()
        except StoreAbsent:
            return False
        if not self.path.exists():
            return False
        # measured: a mode=ro open of a WAL file with no -wal beside it makes -wal and -shm. With none there, nothing
        # is uncommitted outside the main file, so it is read immutable (no lock, no side file); with one there, a
        # connection is or was open, and the plain read-only open reads through it
        live = Path(str(self.path) + "-wal").exists()
        try:
            conn = sqlite3.connect(self.path.resolve().as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1"),
                                   uri=True, timeout=BUSY_WAIT_S, isolation_level=None)
        except sqlite3.OperationalError:
            if not self.path.exists():          # removed between the check and the open
                return False
            raise
        try:
            conn.execute("PRAGMA query_only = ON")
            return "threads" in _names(conn)
        finally:
            conn.close()

    def _schema_current(self, conn: sqlite3.Connection, *, base: bool = False) -> bool:
        """``base`` (every display's gate, and a non-creating write's): the lifecycle tables are ADDITIVE, so a store
        made before C13 (none of them yet) is current for every reader; a writer's ensure-schema path makes them, and
        the ready reader reads their absence as no items (:mod:`levain.coordinator.ready`)."""
        from levain.dispatch.jobs import _SCHEMA as JOB_SCHEMA, FORMAT

        if not schema_present(conn, (*JOB_SCHEMA, *self._statements(base=base, lifecycle=not base))) or "gseq" not in {
                r[1] for r in conn.execute("PRAGMA table_info(replies)")} \
                or not {"replies_gseq", "replies_gseq_no_replace"} <= _names(conn):
            return False
        meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('format', 'store_id')").fetchall())
        return meta.get("format") == FORMAT and "store_id" in meta

    @contextmanager
    def _write(self, by: str | None = None, token: object = None, *,
               create: bool = True) -> Iterator[tuple[sqlite3.Connection, str]]:
        """One write transaction. A write ``by`` the coordinator is fenced here, inside it: refused unless ``token``
        is the live lease's (C0 §6). Your writes and the service's carry no token.

        ``create=False`` (a review transition: a clear or a reopen that finds no store says so) makes no store: the
        connection is :meth:`_connect`'s non-creating one, and the coordinator's tables are checked INSIDE the
        transaction (:class:`StoreAbsent` when the file does not hold them).

        Every write that commits is followed, outside its transaction, by the ONE awake reconcile (C7), whatever the
        write moved (a thread's state, a detach, a beat of either kind). A write that rolls back reconciles nothing."""
        if by != "coordinator" and token is not None:
            raise CoordError("a lease token is the coordinator's; your own writes carry none")
        with write_transaction(lambda: self._connect(create=create)) as conn:
            if not create and not self._schema_current(conn, base=True):
                raise StoreAbsent(f"{self.path} does not hold the coordinator's current tables")
            now = self._stamp()
            if by == "coordinator":
                from levain.coordinator.lease import fence, record_write

                # the write and its record commit together: a write that rolls back leaves no "it wrote" behind
                record_write(conn, now, fence(conn, now, token))
            yield conn, now
        self._keep_awake()

    def _keep_awake(self) -> None:
        """After a committed write: hold the host awake exactly while a work thread works (C7). Run outside the write
        transaction, and never raises: the write has committed, and a caller told otherwise might write it again."""
        try:
            from levain.coordinator import awake

            awake.sync(self)
        except Exception:      # noqa: BLE001 - see the docstring; `levain doctor` reports the host's sleep state
            pass

    def _stamp(self) -> str:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CoordError("the coordinator store's clock returned a naive datetime")
        return utc_stamp(now)

    # --- drops ---------------------------------------------------------------------------------------------------
    def accept_drop(self, words: str, *, surface: str, outbox_id: str | None = None, sent_at: str | None = None,
                    source: Source = Source(), _carry: Callable[..., Drop] | None = None) -> tuple[Drop, bool]:
        """Store a drop and its ``delivered`` drop receipt in one transaction; ``(drop, True)``. A resend of a stored
        ``outbox_id`` with the same words from the same surface writes nothing: ``(stored drop, False)``."""
        words = _check_words(words)
        if surface not in SURFACES:
            raise CoordError(f"a drop's surface is one of {', '.join(SURFACES)}")
        if outbox_id is None:
            outbox_id = "ob-" + secrets.token_hex(16)
        if not isinstance(outbox_id, str) or _OUTBOX_ID.fullmatch(outbox_id) is None:
            raise CoordError("an outbox_id is 8 to 128 of A-Z a-z 0-9 . _ - (starting with a letter or digit)")
        source = check_source(source)
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
                    gap = round((sent - parse_stamp(now)).total_seconds() / 60)
                    raise CoordError(f"sent_at {sent_at} is ahead of the store's clock ({now}): the sending device's "
                                     f"clock runs about {gap} minutes ahead of this Levain's, so set it right")
            if _carry is not None:
                # a drop carrying an envelope (C2): the carrier inserts it, after its own checks, in this transaction
                return _carry(conn, now, words=words, sha=sha, surface=surface, outbox_id=outbox_id, sent_at=sent_at,
                              source=source), True
            return self._insert_drop(conn, now, words=words, sha=sha, surface=surface, outbox_id=outbox_id,
                                     sent_at=sent_at, source=source), True

    def _insert_drop(self, conn: sqlite3.Connection, now: str, *, words: str, sha: str, surface: str,
                     outbox_id: str, sent_at: str, source: Source, drop_id: str | None = None,
                     envelope_id: str | None = None, receipt: dict[str, Any] | None = None) -> Drop:
        """Insert a checked drop and its ``delivered`` drop receipt inside the caller's write transaction.
        ``drop_id`` and ``envelope_id`` are given only by a drop that carries a structured envelope (C2), whose body
        names the drop id it was signed for; ``envelope_id`` is written in the insert, never set later."""
        if drop_id is None:
            drop_id = "dr-" + secrets.token_hex(16)
        elif not isinstance(drop_id, str) or _DROP_ID.fullmatch(drop_id) is None:
            raise CoordError("a drop id is dr- and 32 hex")
        elif conn.execute("SELECT 1 FROM drops WHERE drop_id = ?", (drop_id,)).fetchone() is not None:
            raise DropConflict(f"drop {drop_id} is already stored; nothing was written")
        if envelope_id is not None and (not isinstance(envelope_id, str)
                                        or re.fullmatch(r"ev-[0-9a-f]{32}", envelope_id) is None):
            raise CoordError("an envelope id is ev- and 32 hex")
        conn.execute(
            "INSERT INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
            "source_repo, source_ref, source_head, source_how, envelope_id, prior_envelope_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (drop_id, surface, outbox_id, words, sha, sent_at, now, source.repo, source.ref, source.head,
             source.how, envelope_id))
        self._reply(conn, None, "drop_receipt", "drops", drop_id,
                    {"stage": "delivered", "drop_id": drop_id, "outbox_id": outbox_id, **(receipt or {})}, now)
        from levain.coordinator.lease import wake

        # a drop with no coordinator asks for one, in its own transaction (C0 §6). A wake that fails rolls back to
        # here and the drop is kept: your words are never lost to the coordinator's bookkeeping
        conn.execute("SAVEPOINT coord_wake")
        woke = None
        try:
            woke = wake(conn, now)
        except Exception:  # noqa: BLE001 - any wake failure, never the drop: the words are kept
            conn.execute("ROLLBACK TO coord_wake")
        conn.execute("RELEASE coord_wake")
        if woke is not None:
            self._woken[drop_id] = woke.launch_id
        return self._drop(conn, drop_id)

    # --- threads -------------------------------------------------------------------------------------------------
    def split(self, drop_id: str, parts: Sequence[Part], *, by: str = "operator", token: object = None) -> list[Thread]:
        """The drop's first split (T1): each part a new thread holding it, or the drop attached to an existing open
        thread. One transaction; writes the drop's ``split`` drop receipt. A drop already split is regrouped with
        :meth:`add`, :meth:`merge` or :meth:`detach` instead."""
        by = self._actor(by, ("coordinator", "operator"))
        if not parts:
            raise CoordError("a split names at least one thread")
        with self._write(by, token) as (conn, now):
            self._must_drop(conn, drop_id)
            if self._receipts(conn, drop_id, "split"):
                raise CoordError(f"drop {drop_id} is already split; regroup it with add, merge or detach")
            if not all(isinstance(p, Part) for p in parts):
                raise CoordError("every split part is a Part")
            touched: list[str] = [""] * len(parts)
            # the parts about no thread first (new threads, attaches), then those about one: the order a caller
            # lists parts in never changes what the split is allowed to do
            for i, part in enumerate(parts):
                if not isinstance(part, Part) or part.about is None:
                    touched[i] = self._place(conn, drop_id, part, by, now, first=True)
            for i, part in enumerate(parts):
                if isinstance(part, Part) and part.about is not None and not _sibling(part):
                    touched[i] = self._place(conn, drop_id, part, by, now, first=True)
            for i, part in enumerate(parts):
                if _sibling(part):
                    if part.attach is not None:
                        raise CoordError("a part about @N is a new thread; it names no attach")
                    n = _sibling_index(part, len(parts))
                    if parts[n].about is not None or parts[n].attach is not None:
                        raise CoordError(f"about @{n + 1} names a part that is not a new thread of this split")
                    touched[i] = self._place(conn, drop_id, Part(part.kind, part.title, about=touched[n]), by, now,
                                             first=True)
            if len(set(touched)) != len(touched):
                raise CoordError("a split names the same thread twice")
            self._drop_receipt(conn, drop_id, "split", now)
            self._split_done(conn, drop_id, touched, now)
            return [self._thread(conn, t) for t in touched]

    def note(self, drop_id: str, why: str, *, token: object) -> None:
        """The coordinator's terminal "read, nothing to do" for a drop: an append-only ``noted`` drop receipt carrying
        ``why`` (one line), written under the lease like a split. It stands in for a split everywhere a drop is
        counted as unseen or queued, and the drop stays visible with its reason. A drop already split or already
        noted is refused; a later split of a noted drop is still allowed (the operator or coordinator changed its
        mind), and shows both receipts."""
        why = _one_line("a drop's why", why, _TITLE_MAX)
        with self._write("coordinator", token) as (conn, now):
            self._must_drop(conn, drop_id)
            if self._receipts(conn, drop_id, "split"):
                raise CoordError(f"drop {drop_id} is already split; a split drop is not noted")
            if self._receipts(conn, drop_id, "noted"):
                raise CoordError(f"drop {drop_id} is already noted")
            self._reply(conn, None, "drop_receipt", "drops", drop_id,
                        {"stage": "noted", "drop_id": drop_id, "why": why, "threads": []}, now)

    def add(self, drop_id: str, part: Part, *, by: str = "operator", token: object = None) -> Thread:
        """Put an already-split drop into one more thread (a new one, or an existing open one): the receipt's one-tap
        fix for a split that missed an intent, and how a later drop joins work in flight. Writes a ``resplit``
        drop receipt."""
        by = self._actor(by, ("coordinator", "operator"))
        with self._write(by, token) as (conn, now):
            self._must_drop(conn, drop_id)
            if not self._receipts(conn, drop_id, "split"):
                raise CoordError(f"drop {drop_id} has not been split yet; split it first")
            if _sibling(part):
                raise CoordError("about @N names a part of the same split; add takes one part")
            thread_id = self._place(conn, drop_id, part, by, now, first=False)
            self._drop_receipt(conn, drop_id, "resplit", now)
            return self._thread(conn, thread_id)

    def merge(self, into: str, other: str, *, by: str = "operator", token: object = None) -> Thread:
        """Fold ``other`` into ``into`` (the receipt's one-tap fix for a split that cut one intent in two): ``into``
        gets the union of both memberships as a new revision, ``other`` a new, empty revision and is closed by the
        service, its cause naming ``into`` and who merged. Neither may be done or closed; they must share a kind and an
        ``about``, their repos may not differ, and no thread may be about ``other`` (it would point at a closed one)."""
        by = self._actor(by, ("coordinator", "operator"))
        if into == other:
            raise CoordError("a thread cannot merge into itself")
        with self._write(by, token) as (conn, now):
            a, b = self._must_thread(conn, into), self._must_thread(conn, other)
            for t in (a, b):
                if t.state in ("done", "closed"):
                    raise CoordError(f"thread {t.thread_id} is {t.state}; reopen it first")
            if a.kind != b.kind:
                raise CoordError(f"a {b.kind} thread never joins a {a.kind} thread")
            if a.about != b.about:
                raise CoordError("threads about different threads never merge")
            if a.repo and b.repo and a.repo != b.repo:
                raise CoordError(f"threads on different repos never merge ({a.repo}, {b.repo})")
            if self._dependents(conn, other):
                raise CoordError(f"other threads are about {other}; it cannot be merged away")
            if b.repo and not a.repo:
                self._event(conn, into, "repo", b.repo, f"merged from {other}", by, now)
            if b.state == "working" and a.state == "open":      # the claim on other's work moves with it (_EDGES)
                assert "service" in _EDGES[("open", "working")]
                self._event(conn, into, "state", "working", f"claim carried from {other} by a merge", "service", now)
            self._revise(conn, into, set(a.members) | set(b.members), "resplit", now)
            self._revise(conn, other, (), "resplit", now)
            self._close(conn, other, f"merged into {into} by {by}", "service", now)
            for d in sorted(set(b.members)):
                self._drop_receipt(conn, d, "resplit", now)
            return self._thread(conn, into)

    def detach(self, thread_id: str, drop_id: str, *, by: str = "operator", token: object = None) -> Thread:
        """Take one drop out of a thread (a new revision without it). A thread left with no drops is closed by the
        service, its cause naming who emptied it. The drop must stay in at least one other open or working thread: a
        drop is never left only in finished ones."""
        by = self._actor(by, ("coordinator", "operator"))
        with self._write(by, token) as (conn, now):
            self._must_drop(conn, drop_id)
            t = self._must_thread(conn, thread_id)
            if t.state in ("done", "closed"):
                raise CoordError(f"thread {thread_id} is {t.state}; reopen it first")
            if drop_id not in t.members:
                raise CoordError(f"drop {drop_id} is not in thread {thread_id}")
            if not [x for x in self._threads_of(conn, drop_id)
                    if x != thread_id and self._thread(conn, x).state in ("open", "working")]:
                raise CoordError(f"drop {drop_id} is in no other open thread; add it to one before detaching it")
            left = set(t.members) - {drop_id}
            self._revise(conn, thread_id, left, "resplit", now)
            if not left:
                self._close(conn, thread_id, f"emptied by {by}", "service", now)
            self._drop_receipt(conn, drop_id, "resplit", now)
            return self._thread(conn, thread_id)

    def set_state(self, thread_id: str, state: str, *, by: str, cause: str = "", token: object = None) -> Thread:
        """One T7 step, if :data:`_EDGES` lets ``by`` take it from the state read in this transaction. A thread that
        is not work closes itself (by the service) once done; a thread that holds no drops cannot be reopened."""
        if state not in STATES:
            raise CoordError(f"a thread's state is one of {', '.join(STATES)}")
        # the service is never a caller: its steps (a merge's carried claim, closing what a regroup emptied, an ask
        # closing itself) are taken only inside the store's own operations
        by = self._actor(by, ("operator", "coordinator"))
        cause = self._cause(cause)
        with self._write(by, token) as (conn, now):
            t = self._must_thread(conn, thread_id)
            allowed = _EDGES.get((t.state, state))
            if allowed is None:
                raise CoordError(f"a thread cannot go from {t.state} to {state}")
            if by not in allowed:
                raise CoordError(f"{by} cannot take a thread from {t.state} to {state} "
                                f"(only {', '.join(sorted(allowed - {'service'}))})")
            if state == "open" and not t.members:
                raise CoordError(f"thread {thread_id} holds no drops; it cannot be reopened")
            if state == "open" and t.about is not None and self._thread(conn, t.about).state == "closed":
                raise CoordError(f"thread {thread_id} is about {t.about}, which is closed; reopen that one first")
            if state == "closed":
                self._close(conn, thread_id, cause or state, by, now)
            else:
                self._event(conn, thread_id, "state", state, cause or state, by, now)
                if state == "done":
                    self._ended(conn, thread_id, now)
            if state == "done" and t.kind != "work":
                self._close(conn, thread_id, f"{t.kind} closes itself once done", "service", now)
            result = self._thread(conn, thread_id)
        return result

    def set_field(self, thread_id: str, field: str, value: str, *, by: str = "operator", cause: str = "",
                  token: object = None) -> Thread:
        """Re-kind, re-title or re-scope (repo) a thread, recorded as an event; a done or closed thread is reopened first.
        A re-kind never crosses the work boundary (work stays work; ask, note and relay move among themselves): an
        intent that was misread is a new split, never a relabel. A kind or repo change also writes a new revision
        (cause ``rescope``) with the same members, so anything bound to the old revision ends."""
        if field not in ("kind", "title", "repo"):
            raise CoordError("set_field changes kind, title or repo")
        by = self._actor(by, ("coordinator", "operator"))
        cause = self._cause(cause)
        with self._write(by, token) as (conn, now):
            t = self._must_thread(conn, thread_id)
            if t.state in ("done", "closed"):
                raise CoordError(f"thread {thread_id} is {t.state}; reopen it first")
            if field == "kind":
                if value not in KINDS:
                    raise CoordError(f"a thread's kind is one of {', '.join(KINDS)}")
                if (value == "work") != (t.kind == "work"):
                    raise CoordError(f"a re-kind never crosses the work boundary ({t.kind} -> {value}); split the "
                                     "drop again instead")
            elif field == "title":
                value = _one_line("a thread's title", value, _TITLE_MAX)
            elif not isinstance(value, str) or not value.startswith("/") or len(value) > _REPO_MAX \
                    or unshowable.first_hidden(value, allow="", selectors=False) is not None:
                raise CoordError("a thread's repo is an absolute path, one line")
            self._event(conn, thread_id, field, value, cause or f"{field} set", by, now)
            if field in ("kind", "repo"):
                self._revise(conn, thread_id, t.members, "rescope", now)
            return self._thread(conn, thread_id)

    # --- the lease and launches (C3a; levain.coordinator.lease holds the rules) -----------------------------------
    def coord_status(self) -> dict[str, Any]:
        """Who holds the coordinator lease, what the next drop would do, and the latest launches. Writes the lapse
        of a lease past its until, so what it shows is what the store holds."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            lease._lapse_if_past(conn, now)
            lease._expire_pending(conn, now)
            return lease.status(conn, now)

    def coord_take(self, hold: timedelta | None = None) -> int:
        """You take the coordinator role: every pending launch is voided and you hold the lease. Your token."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.take(conn, now, hold=lease.LEASE_TTL if hold is None else hold)

    def coord_release(self, token: int | None = None, *, by: str = "operator") -> Any:
        """End the lease: you end whatever is held; the holder ends its own with its token."""
        from levain.coordinator import lease

        by = self._actor(by, ("operator", "coordinator"))
        with self._write() as (conn, now):
            return lease.release(conn, now, token, by=by)

    def coord_beat(self, token: int, *, by: str = "coordinator") -> Any:
        from levain.coordinator import lease

        by = self._actor(by, ("coordinator", "service"))
        with self._write() as (conn, now):
            return lease.beat(conn, now, token, by=by)

    def coord_service_beat(self, token: int) -> Any:
        """The watcher's beat (:func:`levain.coordinator.lease.service_beat`): None when the holder has not written
        as coordinator recently enough to earn one."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.service_beat(conn, now, token)

    def coord_claim(self, launch_id: str, session_key: str) -> int:
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.claim(conn, now, launch_id, session_key)

    def coord_wake(self) -> Any:
        """A wake with no drop (a retry after a backoff): the same rules a drop's transaction runs."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.wake(conn, now)

    def coord_set_agent(self, agent: str, *, model: str | None = None, cwd: str | None = None) -> Any:
        """Your coordinator setting: the agent a wake launches, its model, the (resolved) directory it runs in."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.set_agent(conn, now, agent, model=model, cwd=cwd)

    def coord_setting(self) -> Any:
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.setting(conn)

    def pending_coordinator(self) -> Any:
        """The newest coordinator launch still pending (whatever its until), or None."""
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            pending = lease._in_state(conn, "pending", "coordinator")
            return pending[-1] if pending else None

    def woken_launch(self, drop_id: str) -> str | None:
        """The coordinator launch ``drop_id``'s own transaction asked for, when this store object accepted it. Read
        once: the entry is popped, so a long-lived store holds none it no longer needs (L3 r1 G8)."""
        return self._woken.pop(drop_id, None)

    def launch_claim_spawn(self, launch_id: str, pid: int) -> Any:
        """The one-spawner compare-and-set (:func:`levain.coordinator.lease.claim_spawn`), one transaction. Every
        spawner takes it BEFORE it spawns, so the budget gauge is read here first (C6 L3 r1 B2): a worker for a thread
        whose gauge is crossed is refused with nothing started, written ``failed`` (budget crossed) with its SIGN
        filed, exactly as at activation. Activation reads the gauge again and stays the authoritative check (usage can
        move between the two)."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            if conn.execute("SELECT 1 FROM launch_spawns WHERE launch_id = ?", (launch_id,)).fetchone() is None:
                # a spawn already claimed is another process's, mid-spawn: its activation reads the gauge
                refused = self._budget_refusal(conn, now, launch_id)
                if refused is not None:
                    return refused
            return lease.claim_spawn(conn, now, launch_id, pid)

    def launch_request(self, token: int, *, harness: str, role: str = "worker", threads: Sequence[str] = (),
                       model: str | None = None, cwd: str | None = None) -> Any:
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.request_launch(conn, now, token, harness=harness, role=role, threads=threads, model=model,
                                        cwd=cwd)

    def launch_activate(self, launch_id: str, *, harness_session_id: str, argv_sha256: str | None = None) -> Any:
        from levain.coordinator import lease

        with self._write() as (conn, now):
            # C6, T6: the activation is the pin's transaction, so a leg's budget gauge is read here, in it. This is the
            # authoritative check; the spawn claim read it first so a crossed thread starts nothing (L3 r1 B2)
            refused = self._budget_refusal(conn, now, launch_id)
            if refused is not None:
                return refused
            return lease.activate(conn, now, launch_id, harness_session_id=harness_session_id,
                                  argv_sha256=argv_sha256)

    def _budget_refusal(self, conn: sqlite3.Connection, now: str, launch_id: str) -> Any:
        """A pending one-thread worker whose thread's budget gauge is crossed (:mod:`levain.coordinator.budget`): the
        launch is written ``failed`` (cause ``budget crossed``) and the SIGN to extend filed (not for a gauge that is
        unreadable, which no extension unblocks), both in this transaction,
        and ``(launch, reason)`` returned as :func:`levain.coordinator.lease.activate` returns a refusal. None
        otherwise, and for a launch whose lease token is no longer live (the claim or the activation then decides as
        before). A worker holding several threads is not read
        here: :func:`levain.coordinator.lease.refuse_several` refuses it at both the claim and the activation."""
        from levain.coordinator import budget, lease

        ln = lease.launch(conn, launch_id)
        if ln is None or ln.state != "pending" or ln.role != "worker" or len(ln.threads) != 1 or \
                ln.until is None or now >= ln.until:
            return None
        # the asker's lease is read before the budget (C6 L3 r2): a pending launch whose token is no longer the live
        # lease's is the claim's or the activation's to write `lease lost before active`, with no SIGN to extend
        held = lease.lease(conn, now)
        if held is None or not held.live or held.token != ln.token:
            return None
        g = budget.gauge(conn, self.directory, ln.threads[0])
        if g is None or g.refusal is None:
            return None
        lease._launch_event(conn, now, launch_id, "failed", lease.BUDGET_CROSSED)
        if g.unreadable is None:
            # an unreadable meter is no crossing: a SIGN to extend could not unblock it (L3 r1 U1)
            budget.file_sign(self, conn, now, g)
        return lease.launch(conn, launch_id), g.refusal

    def launch_end(self, launch_id: str, kind: str, cause: str) -> Any:
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.end_launch(conn, now, launch_id, kind, cause)

    def launch(self, launch_id: str) -> Any:
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.launch(conn, launch_id)

    # --- what the watcher reads and writes (levain coord watch) --------------------------------------------------
    def launches_unspawned(self) -> list[Any]:
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.unspawned(conn)

    def launches_active(self) -> list[Any]:
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease._in_state(conn, "active")

    def launch_dirs(self) -> list[str]:
        """Every directory a launch of this store was asked to run in (where its briefs sit)."""
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.launch_dirs(conn)

    def launch_by_harness_session(self, harness_session_id: str) -> Any:
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.by_harness_session(conn, harness_session_id)

    def launch_end_unlisted(self, launch_id: str) -> Any:
        """``launch_id``'s session is gone from its harness's list (:func:`levain.coordinator.lease.end_unlisted`)."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            return lease.end_unlisted(conn, now, launch_id)

    def launch_replace_done(self, launch_id: str, *, asked: bool = False) -> Any:
        """``launch_id``'s session was listed done (:func:`levain.coordinator.lease.replace_done`; ``asked``: your
        ``levain coord wake``, which needs no unseen drop): ``(ended launch, the fresh coordinator launch the same
        transaction asked for or None)``, or None when nothing was replaced. The unseen-drop check, the end and the
        wake are one transaction, so a split or a second drop cannot slip between them."""
        from levain.coordinator import lease

        with self._write() as (conn, now):
            ended = lease.replace_done(conn, now, launch_id, asked=asked)
            if ended is None:
                return None
            return ended, lease.wake(conn, now)

    def coord_lease(self) -> Any:
        """The lease as it stands (live or past its until), or None; nothing written."""
        from levain.coordinator import lease

        with read_transaction(self._connect) as conn:
            return lease.lease(conn, self._stamp())

    # --- reading -------------------------------------------------------------------------------------------------
    def drop(self, drop_id: str) -> Drop:
        with read_transaction(self._connect) as conn:
            return self._must_drop(conn, drop_id)

    def drop_by_outbox(self, outbox_id: str) -> Drop | None:
        """The drop stored under ``outbox_id``, or None."""
        with read_transaction(self._connect) as conn:
            row = conn.execute("SELECT drop_id FROM drops WHERE outbox_id = ?", (outbox_id,)).fetchone()
            return None if row is None else self._drop(conn, row[0])

    def resent_drop(self, outbox_id: str, words: str, *, surface: str) -> Drop | None:
        """C1's outbox rule as a read, in ONE transaction: the drop stored under ``outbox_id`` when it holds these words
        from this surface (a resend: the caller answers with it and checks nothing else), None when the id is unused.
        The same id holding other words or another surface raises :class:`DropConflict`, as :meth:`accept_drop` does."""
        sha = _words_sha256(_check_words(words))
        with read_transaction(self._connect) as conn:
            row = conn.execute("SELECT drop_id FROM drops WHERE outbox_id = ?", (outbox_id,)).fetchone()
            if row is None:
                return None
            stored = self._drop(conn, row[0])
        if stored.words_sha256 != sha or stored.surface != surface:
            raise DropConflict(f"outbox_id {outbox_id} is already stored for a different drop ({stored.drop_id}); "
                               "nothing was written")
        return stored

    def record_wake(self, drop_id: str, launch_id: str, *, started: bool, said: str) -> None:
        """A ``woken`` drop receipt: how the coordinator launch ``drop_id``'s own transaction asked for went when its
        starter ran it (``started``, and the starter's words). Written by a starter that runs after the drop's reply
        (the phone port's), so a failed start is on the drop's receipt, never lost."""
        with self._write() as (conn, now):
            self._must_drop(conn, drop_id)
            self._reply(conn, None, "drop_receipt", "drops", drop_id,
                        {"stage": "woken", "drop_id": drop_id, "launch_id": launch_id, "started": bool(started),
                         "said": str(said)[:800]}, now)

    def drops(self, limit: int = 50) -> list[Drop]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise CoordError("limit is 1 to 1000")
        with read_transaction(self._connect) as conn:
            ids = [r[0] for r in conn.execute("SELECT drop_id FROM drops ORDER BY rowid DESC LIMIT ?", (limit,))]
            return [self._drop(conn, d) for d in ids]

    def drops_noted(self, limit: int = 50) -> list[tuple[Drop, str | None]]:
        """The newest ``limit`` drops, each with the why of its ``noted`` receipt (None when not noted), read in ONE
        transaction: one snapshot, one open."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise CoordError("limit is 1 to 1000")
        with read_transaction(self._connect) as conn:
            ids = [r[0] for r in conn.execute("SELECT drop_id FROM drops ORDER BY rowid DESC LIMIT ?", (limit,))]
            out: list[tuple[Drop, str | None]] = []
            for d in ids:
                why = next((str(r.body.get("why", "")) for r in self._receipts(conn, d, "noted")), None)
                out.append((self._drop(conn, d), why))
            return out

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

    def drop_view(self, drop_id: str) -> tuple[Drop, list[Thread], list[Reply]]:
        """A drop, the threads it is in and its drop receipts, read in ONE transaction (one consistent snapshot)."""
        with read_transaction(self._connect) as conn:
            drop = self._must_drop(conn, drop_id)
            return (drop, [self._thread(conn, t) for t in self._threads_of(conn, drop_id)],
                    self._receipts(conn, drop_id, None))

    def thread_view(self, thread_id: str) -> tuple[Thread, list[tuple[int, tuple[str, ...], str, str]],
                                                   list[tuple[int, str, str, str, str, str]]]:
        """A thread with every revision and event, read in ONE transaction."""
        with read_transaction(self._connect) as conn:
            t = self._must_thread(conn, thread_id)
            return t, self._revisions(conn, thread_id), self._events(conn, thread_id)

    def drop_receipts(self, drop_id: str) -> list[Reply]:
        """Every drop receipt of ``drop_id``, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_drop(conn, drop_id)
            return self._receipts(conn, drop_id, None)

    def revisions(self, thread_id: str) -> list[tuple[int, tuple[str, ...], str, str]]:
        """``(rev, members, members_sha256, cause)`` for every revision of a thread, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_thread(conn, thread_id)
            return self._revisions(conn, thread_id)

    def events(self, thread_id: str) -> list[tuple[int, str, str, str, str, str]]:
        """``(seq, field, value, cause, by, at)`` for every event of a thread, oldest first."""
        with read_transaction(self._connect) as conn:
            self._must_thread(conn, thread_id)
            return self._events(conn, thread_id)

    # --- inside a transaction ------------------------------------------------------------------------------------
    @staticmethod
    def _revisions(conn: sqlite3.Connection, thread_id: str) -> list[tuple[int, tuple[str, ...], str, str]]:
        return [(r[0], tuple(json.loads(r[1])), r[2], r[3]) for r in conn.execute(
            "SELECT rev, members, members_sha256, cause FROM thread_revisions WHERE thread_id = ? ORDER BY rev",
            (thread_id,))]

    @staticmethod
    def _events(conn: sqlite3.Connection, thread_id: str) -> list[tuple[int, str, str, str, str, str]]:
        return list(conn.execute("SELECT seq, field, value, cause, by, at FROM thread_events WHERE thread_id = ? "
                                 "ORDER BY seq", (thread_id,)))

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
        if members_sha256(json.loads(members)) != msha:
            raise CoordError(f"thread {thread_id} revision {rev} does not hash to its members")
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
            # the attach declares the intent it adds (the coordinator's inference): an ask or note never joins a work
            # thread (C0 §3.2); a drop that asks ABOUT the thread is refused by _revise, on every path
            if part.kind not in KINDS:
                raise CoordError(f"an attach names the kind of intent it adds, one of {', '.join(KINDS)}")
            if part.kind != t.kind:
                raise CoordError(f"a {part.kind} part never joins a {t.kind} thread")
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
            target = self._must_thread(conn, part.about)
            if target.kind != "work" or target.state == "closed":
                raise CoordError(f"about names an open work thread ({target.thread_id} is {target.kind}, "
                                 f"{target.state})")
        thread_id = "th-" + secrets.token_hex(16)
        conn.execute("INSERT INTO threads (thread_id, created_at, about) VALUES (?, ?, ?)",
                     (thread_id, now, part.about))
        self._revise(conn, thread_id, {drop_id}, "split" if first else "resplit", now)
        self._event(conn, thread_id, "state", "open", "split", by, now)
        self._event(conn, thread_id, "kind", part.kind, "split", by, now)
        self._event(conn, thread_id, "title", title, "split", by, now)
        return thread_id

    def _close(self, conn: sqlite3.Connection, thread_id: str, cause: str, by: str, now: str) -> None:
        """Close a thread, and with it every thread about it that is not closed yet: an ask or note about closed work
        has nothing left to concern (``about`` never changes, and a closed thread is reopened only by you). Every close
        goes through here, so no path leaves an open thread pointing at a closed one."""
        self._event(conn, thread_id, "state", "closed", cause, by, now)
        self._ended(conn, thread_id, now)
        for dependent in self._dependents(conn, thread_id):
            if self._thread(conn, dependent).state != "closed":
                self._close(conn, dependent, f"the thread it is about ({thread_id}) closed", "service", now)

    def _ended(self, conn: sqlite3.Connection, thread_id: str, now: str) -> None:
        # T7, done kills its envelope: on every path that ends a thread, whichever class opened the file
        from levain.coordinator.envelopes import EnvelopeStore

        EnvelopeStore.end_thread(conn, thread_id, now)

    def _split_done(self, conn: sqlite3.Connection, drop_id: str, thread_ids: list[str], now: str) -> None:
        # a structured envelope the drop carried binds at its first split, whichever class opened the file
        from levain.coordinator.envelopes import EnvelopeStore

        EnvelopeStore.bind_on_split(conn, drop_id, thread_ids, now)

    @staticmethod
    def _dependents(conn: sqlite3.Connection, thread_id: str) -> list[str]:
        return [r[0] for r in conn.execute("SELECT thread_id FROM threads WHERE about = ? ORDER BY rowid",
                                           (thread_id,))]

    @classmethod
    def _current_members(cls, conn: sqlite3.Connection, thread_id: str) -> set[str]:
        row = conn.execute("SELECT members FROM thread_revisions WHERE thread_id = ? ORDER BY rev DESC LIMIT 1",
                           (thread_id,)).fetchone()
        return set() if row is None else set(json.loads(row[0]))

    @classmethod
    def _revise(cls, conn: sqlite3.Connection, thread_id: str, members: Iterable[str], cause: str,
                now: str) -> None:
        """Write ``thread_id``'s next revision. Every membership change goes through here, so this is where the one
        structural rule is held: a drop that sits in a thread ABOUT this one is never ADDED to it, so an ask never
        moves the revision of the work it asks about. (One drop that is both a fix and a request for a report may sit
        in the work thread and in an ask about it: it joined the work as work.)"""
        ordered = sorted(members)
        added = set(ordered) - cls._current_members(conn, thread_id)
        for dependent in cls._dependents(conn, thread_id) if added else ():
            shared = cls._current_members(conn, dependent) & added
            if shared:
                raise CoordError(f"drop {min(shared)} asks about thread {thread_id} (in {dependent}); it never "
                                 "joins it")
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
        # every reply takes its place in the store-wide order (CH-D1 §2.8): the chat timeline merges it by gseq
        conn.execute("INSERT INTO replies (reply_id, thread_id, kind, ref_store, ref_id, body, at, gseq) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (reply_id, thread_id, kind, ref_store, ref_id, canonical(body),
                                                         now, next_gseq(conn, now)))
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
