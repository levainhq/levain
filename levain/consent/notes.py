"""levain.consent.notes — the note card: an agent tells the operator something that needs no answer; he dismisses it.

An agent files a NOTE (:meth:`NoteStore.file`): what it is telling him (one line) and, if useful, a detail (lines
allowed). The operator reads it and dismisses it (:meth:`NoteStore.dismiss`), once, from the cockpit or the phone (no
``levain note`` command exists yet; ``cli`` is a channel a caller of :meth:`dismiss` may name). A note carries no options, no recommendation and no answer (Phill, 10-22: "no-option cards as replies ... requiring
a reply to these is just a waste of tokens AND breaks the spirit of the workflow here which is drop-and-go").

NOTHING HERE IS SIGNED, AUTHORITY OR DELIVERED. A dismissal tells the agent nothing: this store has no claim, lease,
delivery or resume step and no table for one, so no hook, resume or thread can be started from it, and a dismiss spends
no agent's tokens. It is its own file (``notes.db``, beside ``questions.db`` and ``queue.db``) on purpose: every path
that answers a question, delivers an answer, signs a ruling or decides a request reads another store, so none of them
can reach a note, by type rather than by a check each would have to remember.

The question store's rules hold here: the directory is the operator's alone (0700, refused otherwise), writes are one
``BEGIN IMMEDIATE`` through :mod:`levain.autonomic.durable_sqlite`, reads are read-only snapshots that never create the
file, and both tables are append-only (SQL triggers refuse UPDATE, DELETE and an INSERT OR REPLACE). Agent text is
refused at filing when it holds a character a person cannot see (:mod:`levain.unshowable`, the one rule every surface
asks); ``detail`` may hold newlines and nothing else unshowable. Known edges, not built against:
  - any process of the operator's uid can file or dismiss a note (the store is his own file and a note is no decision).
  - notes are kept, never pruned: the history of what an agent told him and when he read it is the point.
Stdlib only.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from levain import unshowable
from levain.autonomic.durable_sqlite import open_durable, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import ConsentError, utc_stamp
from levain.consent.queue import default_queue_dir
from levain.consent.questions import ANSWER_CHANNELS, _SESSION_MAX, _has_control, _parse_ref
from levain.consent.record import AGENTS

__all__ = ["DB_NAME", "DISMISS_CHANNELS", "FORMAT", "Dismissal", "Note", "NoteStore", "NoteStoreError"]

DB_NAME = "notes.db"
FORMAT = "levain-notes/1"
DISMISS_CHANNELS: tuple[str, ...] = ANSWER_CHANNELS           # cli, phone, cockpit
_ACCESS_BY_OTHERS = 0o077
WHAT_MAX = 200
DETAIL_MAX = 4000
_N_MAX = 2 ** 63 - 1


class NoteStoreError(RuntimeError):
    """The store cannot be trusted: another format, a directory others can reach."""


@dataclass(frozen=True)
class Dismissal:
    note_id: str
    channel: str
    dismissed_at: str


@dataclass(frozen=True)
class Note:
    n: int
    id: str
    agent: str
    agent_session_id: str
    what: str
    detail: str | None
    cwd: str
    filed_at: str
    dismissed: Dismissal | None


def _append_only(table: str, message: str, key: str) -> tuple[str, ...]:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a reused key or rowid
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
            f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {key} = NEW.{key} OR rowid = NEW.rowid) "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS notes (n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
    "agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, what TEXT NOT NULL, detail TEXT, cwd TEXT NOT NULL, "
    "filed_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS note_dismissals (note_id TEXT PRIMARY KEY REFERENCES notes(id), "
    "channel TEXT NOT NULL CHECK (channel IN ('cli','phone','cockpit')), dismissed_at TEXT NOT NULL)",
    *_append_only("notes", "notes are append-only", "id"),
    *_append_only("note_dismissals", "a note is dismissed once", "note_id"),
)
_SELECT = ("SELECT n.n, n.id, n.agent, n.agent_session_id, n.what, n.detail, n.cwd, n.filed_at, d.note_id, d.channel, "
           "d.dismissed_at FROM notes n LEFT JOIN note_dismissals d ON d.note_id = n.id")


def _text(name: str, value: Any, high: int, allow: str) -> str:
    if not isinstance(value, str):
        raise ConsentError(f"{name} must be a string")
    hidden = unshowable.describe(value, allow=allow)
    if hidden is not None:
        lines = "plain text (line breaks allowed)" if allow else "one line of plain text"
        raise ConsentError(f"{name} must be {lines} that shows as itself: it holds {hidden}")
    value = value.strip()
    if not 1 <= len(value) <= high:
        raise ConsentError(f"{name} must be 1 to {high} characters")
    return value


def _ref(ref: Any) -> tuple[str, Any] | None:
    """``('id', 'n-…')`` or ``('n', int)``: a note's own id or number (a question's ``q-…`` id is none of these)."""
    if isinstance(ref, str) and ref.strip().startswith("n-"):
        return ("id", ref.strip())
    if isinstance(ref, str) and ref.strip().startswith("q-"):
        return None
    return _parse_ref(ref)


class NoteStore:
    """``<directory>/notes.db``, ``directory`` the consent directory by default."""

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._clock = clock

    def _check_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = os.lstat(self.directory)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise NoteStoreError(f"note store directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise NoteStoreError(f"note store directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise NoteStoreError(f"note store directory {self.directory} is reachable by group or others "
                                 f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        self._check_directory()
        conn = open_durable(self.path)
        try:
            os.chmod(self.path, 0o600)
            conn.execute("BEGIN IMMEDIATE")
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in tables else None
                if tables and row is None:
                    raise NoteStoreError("the database holds tables but no note-store format marker")
                if row is not None and row[0] != FORMAT:
                    raise NoteStoreError(f"store format {row[0]!r} is not {FORMAT!r}")
                for stmt in _SCHEMA:
                    conn.execute(stmt)
                if row is None:
                    conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _peek(self) -> Iterator[sqlite3.Connection | None]:
        """A read-only snapshot, or ``None`` when there is no store yet; it never creates or sets up the file."""
        try:
            empty = self.path.stat().st_size == 0
        except FileNotFoundError:
            empty = True
        if empty:
            yield None
            return
        self._check_directory()
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2, isolation_level=None)
        try:
            conn.execute("BEGIN")
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in have else None
            if row is None and not have - {"meta"}:
                conn.execute("COMMIT")
                yield None
                return
            if row is None or row[0] != FORMAT or "note_dismissals" not in have:
                raise NoteStoreError(f"{self.path} is not a note store of format {FORMAT!r}")
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def _note_of(row: tuple[Any, ...]) -> Note:
        d = None if row[8] is None else Dismissal(row[8], row[9], row[10])
        return Note(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], d)

    @staticmethod
    def _by_ref(conn: sqlite3.Connection, ref: Any) -> Note | None:
        parsed = _ref(ref)
        if parsed is None:
            return None
        row = conn.execute(f"{_SELECT} WHERE n.{parsed[0]} = ?", (parsed[1],)).fetchone()
        return None if row is None else NoteStore._note_of(row)

    # --- filing and reading ------------------------------------------------------------------------------------
    @staticmethod
    def check(*, agent: str, agent_session_id: str, what: Any, detail: Any, cwd: Any) -> tuple[str, str | None]:
        """:meth:`file`'s refusals, run alone: raises :class:`ConsentError`, else the cleaned ``what`` and ``detail``."""
        if agent not in AGENTS:
            raise ConsentError(f"agent must be one of {', '.join(AGENTS)}: {agent!r}")
        if (not isinstance(agent_session_id, str) or not 0 < len(agent_session_id) <= _SESSION_MAX
                or _has_control(agent_session_id, selectors=False)):
            raise ConsentError(f"agent_session_id must be 1 to {_SESSION_MAX} characters that show as themselves")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd) or _has_control(cwd, selectors=False):
            raise ConsentError("cwd must be an absolute path")
        what = _text("what", what, WHAT_MAX, "")
        return what, None if detail is None else _text("detail", detail, DETAIL_MAX, "\n")

    def file(self, *, agent: str, agent_session_id: str, what: str, detail: str | None, cwd: str) -> Note:
        """File a note, refusing anything malformed with nothing written."""
        what, detail = self.check(agent=agent, agent_session_id=agent_session_id, what=what, detail=detail, cwd=cwd)
        nid = "n-" + os.urandom(12).hex()
        stamp = utc_stamp(self._now())
        with self._write() as conn:
            conn.execute("INSERT INTO notes (id, agent, agent_session_id, what, detail, cwd, filed_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (nid, agent, agent_session_id, what, detail, cwd, stamp))
            made = self._by_ref(conn, nid)
        assert made is not None
        return made

    def get(self, ref: Any) -> Note | None:
        """A note by its id (``n-…``) or its number (``7`` / ``"#7"``); ``None`` if there is none."""
        with self._peek() as conn:
            return None if conn is None else self._by_ref(conn, ref)

    def open_notes(self) -> list[Note]:
        """The notes not yet dismissed, oldest first."""
        with self._peek() as conn:
            if conn is None:
                return []
            rows = conn.execute(f"{_SELECT} WHERE d.note_id IS NULL ORDER BY n.n").fetchall()
        return [self._note_of(r) for r in rows]

    def recent(self, limit: int = 50) -> list[Note]:
        """The newest ``limit`` notes, dismissed or not, newest first."""
        with self._peek() as conn:
            if conn is None:
                return []
            rows = conn.execute(f"{_SELECT} ORDER BY n.n DESC LIMIT ?", (min(max(int(limit), 0), _N_MAX),)).fetchall()
        return [self._note_of(r) for r in rows]

    # --- dismissing ----------------------------------------------------------------------------------------------
    def dismiss(self, ref: Any, *, channel: str) -> Dismissal:
        """Mark a note read, once. It tells the agent nothing and starts nothing."""
        if channel not in DISMISS_CHANNELS:
            raise ConsentError(f"channel must be one of {', '.join(DISMISS_CHANNELS)}: {channel!r}")
        stamp = utc_stamp(self._now())
        with self._write() as conn:
            note = self._by_ref(conn, ref)
            if note is None:
                raise ConsentError(f"no such note: {ref!r}")
            if note.dismissed is not None:
                raise ConsentError(f"note #{note.n} was already dismissed at {note.dismissed.dismissed_at}")
            conn.execute("INSERT INTO note_dismissals (note_id, channel, dismissed_at) VALUES (?, ?, ?)",
                         (note.id, channel, stamp))
        return Dismissal(note.id, channel, stamp)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ConsentError("the note store's clock returned a naive datetime")
        return now
