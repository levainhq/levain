"""levain.consent.questions — the question band: an agent asks the operator something, the operator answers once.

An agent files a question (:meth:`QuestionStore.file`): what it asks, two to six options (a key and a label each),
the option it recommends, and what the answer blocks. Every one of those is required: a question with no
recommendation or no stated block is refused, since the operator reads it between other things and needs both to
rule fast. The operator answers once (:meth:`QuestionStore.answer`), by an option key or by free text kept
VERBATIM, from the CLI, the phone or the cockpit. The answer's text is then delivered back to the agent's session
exactly once, by the session's next hook (:meth:`QuestionStore.claim_answers_for`) or by a resume that carries it as
its prompt (:meth:`QuestionStore.lease_answers_for`, settled by :meth:`QuestionStore.settle_resume`): the same claim
model as a consent ruling's text, without any of its verification.

NOTHING HERE IS SIGNED AND NOTHING HERE IS AUTHORITY. An answer authorises no action: it is information the agent
is told, in the operator's words, and whatever the agent then wants to do still goes through the consent queue like
any other action. For the same reason there is no key, no authority replay and no signature check anywhere in this
module: a stored answer is what was written, delivered as written. Stated as known edges, none built against:
  - any process of the operator's uid can file a question or answer one, since the store is the operator's own
    file and an answer is not a decision; an answer a raw writer forges is text the agent reads, no more.
  - there is no expiry in v1: an unanswered question stays open until it is answered.
  - delivery is at most once per claim; a claimer that dies between its claim and the agent reading the text loses
    it (a lease with no settle stays ``unconfirmed``, visible, never re-sent: the resume may have run).

Tables are append-only beside each other: a question, its one answer, its one delivery claim and the resume steps
of that claim are rows written once, never updated or deleted (SQL triggers refuse both, and an INSERT OR REPLACE
that would drop a row without a delete trigger); the guard is against a buggy writer, since there is no authority
to protect.

It is its own file (``questions.db``, beside ``queue.db``) and not part of the consent queue store on purpose: a
question carries no authority, so it stays out of the signed store whose triggers, format marker and receipts exist
to guard consent. A bug or a raw write here can never touch a decision there, and the queue's format and
migrations stay about consent alone.

The directory is the operator's alone (0700, refused otherwise), as the queue's. A hook claims on every tool call,
so the claim first reads the store READ-ONLY (:meth:`QuestionStore._peek`: never created or set up, an empty file
read as no store) and takes the write lock only when an answer is due, waiting at most :data:`HOOK_BUSY_MS` on
another writer; the other reads answer empty with no ``questions.db`` and never create it. Stdlib only.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import sqlite3
import time
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
from levain.consent.record import AGENTS

__all__ = ["ANSWER_CHANNELS", "DB_NAME", "FORMAT", "Answer", "Link", "Question", "QuestionStore", "QuestionStoreError"]

DB_NAME = "questions.db"
FORMAT = "levain-questions/1"
ANSWER_CHANNELS: tuple[str, ...] = ("cli", "phone", "cockpit")
_ACCESS_BY_OTHERS = 0o077
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,15}")
_OPTIONS_MIN, _OPTIONS_MAX = 2, 6
_SESSION_MAX = 256
_WHAT_MAX = 1000
# bounds, not a style: the longest option label and "stopped" analysis in the 991 needs-phill rows flow filed are 525
# and 4,933 characters, and every surface shows a question's whole text (the push body alone is fitted)
_LABEL_MAX = 600
_BLOCKS_MAX = 6000
_TEXT_MAX = 4000
_N_MAX = 2 ** 63 - 1                     # SQLite's INTEGER
# how long the hook's read and its claim together wait on a lock another writer holds, by the wall clock: past it
# the answers wait for the session's next hook, so a stuck writer never holds a tool call up (the gate's own hold is
# not this). One deadline, not SQLite's busy_timeout per step: that counts the sleeps it asked for, not time passed
HOOK_BUSY_MS = 2000
_RETRY_SECONDS = 0.025
# the deadline (``time.monotonic()``) a caller on a bound (the band's hook) sets for every read under it: a snapshot
# then waits at most what is left of it, never a fresh HOOK_BUSY_MS per read (:func:`read_deadline`)
_READ_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("levain_questions_read_deadline",
                                                                             default=None)


@contextmanager
def read_deadline(deadline: float | None) -> Iterator[None]:
    """Every :meth:`QuestionStore._peek` under this block waits on a lock at most until ``deadline``
    (``time.monotonic()``; ``None`` leaves the ordinary HOOK_BUSY_MS each). An expired deadline still reads a store no
    one holds: it only stops the waiting."""
    token = _READ_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _READ_DEADLINE.reset(token)


def current_read_deadline() -> float | None:
    """The deadline a :func:`read_deadline` block set, or ``None``: other readers on the same bound (the band's
    authority sources) cap their own waits to it."""
    return _READ_DEADLINE.get()


def _read_wait_ms() -> int:
    deadline = _READ_DEADLINE.get()
    if deadline is None:
        return HOOK_BUSY_MS
    return max(0, min(HOOK_BUSY_MS, int((deadline - time.monotonic()) * 1000)))


class QuestionStoreError(RuntimeError):
    """The store cannot be trusted: another format, a directory others can reach, one that vanished."""


@dataclass(frozen=True)
class Answer:
    question_id: str
    kind: str          # 'option' (value = an option key) or 'text' (value = the operator's words, verbatim)
    value: str
    channel: str
    answered_at: str


@dataclass(frozen=True)
class Question:
    n: int
    id: str
    agent: str
    agent_session_id: str
    what: str
    options: tuple[tuple[str, str], ...]     # (key, label)
    rec: str
    blocks: str
    cwd: str
    filed_at: str
    answer: Answer | None
    link: Link | None = None
    # the levain sign request (levain.consent.sign) a caller filed for this question, by id: the cockpit row then
    # carries `sign: {"request": <id>}` and the page signs the request's own options. The kernel's store sets none
    sign_request: str | None = None


@dataclass(frozen=True)
class Link:
    """An option of a question tied to the consent request levain filed, undecided, for the call that option leads
    to (``levain ask --would-push``): an id, never authority. Only a late ruling signed on that request allows the
    call; answering the option allows nothing."""
    option: str
    kind: str          # 'push': the request is a ``levain push`` of exactly the call levain derived
    request_id: str
    queue_id: str


def _append_only(table: str, message: str) -> tuple[str, str]:
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_frozen BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            f"CREATE TRIGGER IF NOT EXISTS {table}_kept BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{message}'); END")


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS questions (n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, "
    "agent TEXT NOT NULL, agent_session_id TEXT NOT NULL, what TEXT NOT NULL, options TEXT NOT NULL, "
    "rec TEXT NOT NULL, blocks TEXT NOT NULL, cwd TEXT NOT NULL, filed_at TEXT NOT NULL, resume TEXT)",
    "CREATE INDEX IF NOT EXISTS questions_session ON questions (agent, agent_session_id)",
    # one answer per question, written once; the operator's words are the value
    "CREATE TABLE IF NOT EXISTS answers (question_id TEXT PRIMARY KEY REFERENCES questions(id), "
    "kind TEXT NOT NULL CHECK (kind IN ('option','text')), value TEXT NOT NULL, "
    "channel TEXT NOT NULL CHECK (channel IN ('cli','phone','cockpit')), answered_at TEXT NOT NULL)",
    # an answer's text delivered to its session (a hook, or a resume claim): once
    "CREATE TABLE IF NOT EXISTS answer_deliveries (question_id TEXT PRIMARY KEY REFERENCES answers(question_id), "
    "agent_session_id TEXT NOT NULL, delivered_at TEXT NOT NULL)",
    # what became of a claim made for a RESUME, one row per step, the latest counting: ``launching`` (unconfirmed
    # while left alone), ``started`` (delivered), ``failed`` (deliverable again), ``hook`` (a failed one delivered
    # by the session's next hook). A claim with no row here was delivered by a hook.
    "CREATE TABLE IF NOT EXISTS answer_resume_steps (seq INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT NOT NULL "
    "REFERENCES answer_deliveries(question_id), agent_session_id TEXT NOT NULL, "
    "outcome TEXT NOT NULL CHECK (outcome IN ('launching','started','failed','hook')), argv TEXT, cwd TEXT, "
    "detail TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS answer_resume_steps_question ON answer_resume_steps (question_id, seq)",
    # an option tied to the consent request filed for its call (Q-3), written with the question; a store set up
    # before it has no such table, and a reader then sees no links
    "CREATE TABLE IF NOT EXISTS question_links (question_id TEXT PRIMARY KEY REFERENCES questions(id), "
    "option TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('push')), request_id TEXT NOT NULL, "
    "queue_id TEXT NOT NULL, created_at TEXT NOT NULL)",
    *_append_only("questions", "questions are append-only"),
    *_append_only("question_links", "a question link is written once"),
    "CREATE TRIGGER IF NOT EXISTS question_links_no_replace BEFORE INSERT ON question_links "
    "WHEN EXISTS (SELECT 1 FROM question_links WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a link is never replaced'); END",
    *_append_only("answers", "an answer is written once"),
    *_append_only("answer_deliveries", "an answer is delivered once"),
    *_append_only("answer_resume_steps", "answer resume steps are append-only"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row that reuses a key or
    # an explicit rowid (one not given reads as -1 here, never a real row)
    "CREATE TRIGGER IF NOT EXISTS questions_no_replace BEFORE INSERT ON questions "
    "WHEN EXISTS (SELECT 1 FROM questions WHERE id = NEW.id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a question is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS answers_no_replace BEFORE INSERT ON answers "
    "WHEN EXISTS (SELECT 1 FROM answers WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'an answer is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS answer_deliveries_no_replace BEFORE INSERT ON answer_deliveries "
    "WHEN EXISTS (SELECT 1 FROM answer_deliveries WHERE question_id = NEW.question_id OR rowid = NEW.rowid) "
    "BEGIN SELECT RAISE(ABORT, 'a delivery is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS answer_resume_steps_no_replace BEFORE INSERT ON answer_resume_steps "
    "WHEN EXISTS (SELECT 1 FROM answer_resume_steps WHERE seq = NEW.seq) "
    "BEGIN SELECT RAISE(ABORT, 'a resume step is never replaced'); END",
)

_Q_COLS = "q.n, q.id, q.agent, q.agent_session_id, q.what, q.options, q.rec, q.blocks, q.cwd, q.filed_at"
_A_COLS = "a.question_id, a.kind, a.value, a.channel, a.answered_at"
_L_COLS = "l.option, l.kind, l.request_id, l.queue_id"
_SELECT = (f"SELECT {_Q_COLS}, {_A_COLS}, {_L_COLS} FROM questions q LEFT JOIN answers a ON a.question_id = q.id "
           "LEFT JOIN question_links l ON l.question_id = q.id")
# the same, for a store set up before ``question_links`` existed (read-only readers never add it)
_SELECT_UNLINKED = (f"SELECT {_Q_COLS}, {_A_COLS}, NULL, NULL, NULL, NULL FROM questions q "
                    "LEFT JOIN answers a ON a.question_id = q.id")
# the latest resume step of an answered question ``q`` (NULL when it is unclaimed or a hook claimed it)
_LAST_STEP = ("(SELECT e.outcome FROM answer_resume_steps e WHERE e.question_id = q.id ORDER BY e.seq DESC LIMIT 1)")
# an answer whose text has not reached its session: never claimed, or claimed for a resume that failed
_DELIVERABLE = ("(NOT EXISTS (SELECT 1 FROM answer_deliveries d WHERE d.question_id = q.id) OR "
                + _LAST_STEP + " = 'failed')")


def _select(conn: sqlite3.Connection) -> str:
    """The question query for this store: with its links, or with none when its ``question_links`` table was never
    set up (a store only a levain before Q-3 wrote, read by a reader that never writes)."""
    have = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'question_links'").fetchone()
    return _SELECT if have is not None else _SELECT_UNLINKED


def _now_utc(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConsentError("the question store's clock returned a naive datetime")
    return now


def _has_control(text: str, selectors: bool = True) -> bool:
    # any unshowable character (levain.unshowable): control and format characters, the line and paragraph
    # separators a model or a renderer can read as a line break, invisible spaces and fillers. selectors=False for an
    # id or a path (no presentation selector at all); the default is the rule for text a person reads
    return unshowable.first_hidden(text, allow="", selectors=selectors) is not None


def _line(name: str, value: Any, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise ConsentError(f"{name} must be a string")
    hidden = unshowable.describe(value)
    if hidden is not None:
        raise ConsentError(f"{name} must be one line of plain text that shows as itself: it holds {hidden}")
    value = value.strip()
    if not low <= len(value) <= high:
        raise ConsentError(f"{name} must be {low} to {high} characters")
    return value


def _options_of(raw: Any, fewest: int = _OPTIONS_MIN) -> list[dict[str, str]]:
    if not isinstance(raw, (list, tuple)) or not fewest <= len(raw) <= _OPTIONS_MAX:
        raise ConsentError(f"options must be a list of {fewest} to {_OPTIONS_MAX} options")
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            if set(item) != {"key", "label"}:
                raise ConsentError("an option is exactly a key and a label")
            key, label = item["key"], item["label"]
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            key, label = item
        else:
            raise ConsentError("an option is a {key, label} object or a (key, label) pair")
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise ConsentError(f"option key {key!r} must match {_KEY.pattern}")
        out.append({"key": key, "label": _line(f"the label of option {key}", label, 1, _LABEL_MAX)})
    if len({o["key"] for o in out}) != len(out):
        raise ConsentError("option keys must be unique")
    return out


def _answer_of(option: Any, text: Any) -> tuple[str, str]:
    """``(kind, value)`` of an answer given as exactly one of an option key or free text, or the refusal: the rules
    every answering path holds before it reads the question."""
    if (option is None) == (text is None):
        raise ConsentError("answer with exactly one of an option key or free text")
    if text is not None:
        if not isinstance(text, str) or not text.strip() or len(text) > _TEXT_MAX or "\x00" in text:
            raise ConsentError(f"text must be 1 to {_TEXT_MAX} characters, not only whitespace, with no NUL")
        return "text", text
    if not isinstance(option, str):
        raise ConsentError("option must be a key")
    return "option", option


def _parse_ref(ref: Any) -> tuple[str, Any] | None:
    """``('id', 'q-…')`` or ``('n', int)`` for a question reference, else ``None``."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ("n", ref) if 1 <= ref <= _N_MAX else None
    if isinstance(ref, str):
        text = ref.strip()
        if text.startswith("q-"):
            return ("id", text)
        digits = text[1:] if text.startswith("#") else text
        if digits.isascii() and digits.isdigit() and len(digits) <= 19:
            return ("n", int(digits)) if 1 <= int(digits) <= _N_MAX else None
    return None


class QuestionStore:
    """The question band's store: ``<directory>/questions.db``, ``directory`` the consent directory by default.
    Every write is one ``BEGIN IMMEDIATE`` through :mod:`levain.autonomic.durable_sqlite`."""

    def __init__(self, directory: Path | str | None = None, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._clock = clock
        self._ready = False

    # --- store ---------------------------------------------------------------------------------------
    def _check_directory(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        st = os.lstat(self.directory)
        if not os.path.isdir(self.directory) or os.path.islink(self.directory):
            raise QuestionStoreError(f"question store directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise QuestionStoreError(f"question store directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise QuestionStoreError(f"question store directory {self.directory} is reachable by group or others "
                                     f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self, busy_ms: int | None = None, open_s: float | None = None,
                 deadline: float | None = None) -> sqlite3.Connection:
        """A durable connection; ``open_s`` bounds the open's own waits (:func:`open_durable`'s ``busy_s``) and
        ``busy_ms`` the busy timeout after, the first-time setup included. ``deadline`` (``time.monotonic()``) replaces
        both: the open gets what is left of it, and the setup's wait what the open left, never a fresh budget each."""
        if self._ready:
            if not self.path.exists():
                raise QuestionStoreError(f"question store {self.path} has disappeared")
        else:
            self._check_directory()
        if deadline is not None:      # after the directory checks: the open gets only what is left now
            open_s = max(0.0, deadline - time.monotonic())
        conn = open_durable(self.path, busy_s=open_s)
        try:
            if deadline is not None:
                busy_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if busy_ms is not None:
                conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
            if not self._ready:
                os.chmod(self.path, 0o600)
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if tables and ("meta" not in tables or conn.execute(
                    "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is None):
                raise QuestionStoreError("the database holds tables but no question-store format marker")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if tables else None
            if row is not None and row[0] != FORMAT:
                raise QuestionStoreError(f"store format {row[0]!r} is not {FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('store_id', ?)",
                         ("qs-" + os.urandom(16).hex(),))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    def ready_by(self, deadline: float) -> None:
        """Set the store up (a first number on a store no question was filed in) with every wait bounded by
        ``deadline`` (``time.monotonic()``): the open, the WAL switch and the setup's ``BEGIN IMMEDIATE``. A lock held
        past it raises SQLite's busy error and sets nothing up (the next caller tries again)."""
        if self._ready:
            return
        self._connect(deadline=deadline).close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _write_by(self, deadline: float) -> Iterator[sqlite3.Connection]:
        """As :meth:`_write`, but the write lock is waited for until ``deadline`` (``time.monotonic()``) by the wall
        clock: SQLite is told not to wait at all, and ``BEGIN IMMEDIATE`` is retried here. Past the deadline the
        lock's own ``OperationalError`` is raised."""
        # Only ever called after _peek found a set-up store: _connect's own setup would BEGIN IMMEDIATE at busy 0,
        # outside the retry loop below.
        assert self._ready
        conn = self._connect(busy_ms=0, open_s=max(0.0, deadline - time.monotonic()))
        try:
            while True:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc) and "busy" not in str(exc):
                        raise
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise
                    time.sleep(min(_RETRY_SECONDS, left))
            try:
                yield conn
            except BaseException:
                _rollback(conn)
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def _peek(self) -> Iterator[sqlite3.Connection | None]:
        """Every read: a snapshot opened read-only, waiting at most :data:`HOOK_BUSY_MS` on a lock (less under a
        :func:`read_deadline`); it never
        creates, sets up or writes the store, so a tool call with nothing to deliver takes no write lock. ``None``
        when there is no store yet (no file, an empty one, or one a filer has not set up); a store of another
        format raises :class:`QuestionStoreError`."""
        try:
            empty = self.path.stat().st_size == 0
        except FileNotFoundError:
            empty = True
        if empty:
            yield None
            return
        self._check_directory()
        wait_ms = _read_wait_ms()
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=wait_ms / 1000,
                               isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout = {wait_ms}")
            conn.execute("BEGIN")
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in have else None
            if row is None and not have - {"meta"}:
                conn.execute("COMMIT")
                yield None
                return
            if row is None or row[0] != FORMAT or "answer_resume_steps" not in have:
                raise QuestionStoreError(f"{self.path} is not a question store of format {FORMAT!r}")
            self._ready = True                # set up and of this format: a write here need not set it up again
            yield conn
            conn.execute("COMMIT")
        finally:
            conn.close()

    @property
    def store_id(self) -> str:
        with self._write() as conn:
            return conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]

    @staticmethod
    def _question_of(row: tuple[Any, ...]) -> Question:
        options = tuple((o["key"], o["label"]) for o in json.loads(row[5]))
        answer = None if row[10] is None else Answer(row[10], row[11], row[12], row[13], row[14])
        link = None if row[15] is None else Link(row[15], row[16], row[17], row[18])
        return Question(row[0], row[1], row[2], row[3], row[4], options, row[6], row[7], row[8], row[9], answer, link)

    @staticmethod
    def _by_ref(conn: sqlite3.Connection, ref: Any) -> Question | None:
        parsed = _parse_ref(ref)
        if parsed is None:
            return None
        row = conn.execute(f"{_select(conn)} WHERE q.{parsed[0]} = ?", (parsed[1],)).fetchone()
        return None if row is None else QuestionStore._question_of(row)

    # --- filing and reading --------------------------------------------------------------------------
    @staticmethod
    def check(*, agent: str, agent_session_id: str, what: Any, options: Any, rec: Any, blocks: Any, cwd: Any,
              fewest: int = _OPTIONS_MIN) -> tuple[str, list[dict[str, str]], str]:
        """:meth:`file`'s refusals, run alone (``levain ask`` runs them before it files anything else): raises
        :class:`ConsentError`, else the cleaned ``what``, options and ``blocks``. ``fewest``: the least number of
        options (a system source's acknowledgement is one; an agent's question offers a choice)."""
        if agent not in AGENTS:
            raise ConsentError(f"agent must be one of {', '.join(AGENTS)}: {agent!r}")
        if (not isinstance(agent_session_id, str) or not 0 < len(agent_session_id) <= _SESSION_MAX
                or _has_control(agent_session_id, selectors=False)):
            raise ConsentError(f"agent_session_id must be 1 to {_SESSION_MAX} characters that show as themselves")
        if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd) or _has_control(cwd, selectors=False):
            raise ConsentError("cwd must be an absolute path")
        what = _line("what", what, 1, _WHAT_MAX)
        opts = _options_of(options, fewest)
        if not isinstance(rec, str) or rec not in {o["key"] for o in opts}:
            raise ConsentError("rec is required and must be one of the option keys")
        return what, opts, _line("blocks", blocks, 1, _BLOCKS_MAX)

    def file(self, *, agent: str, agent_session_id: str, what: str, options: Any, rec: str, blocks: str, cwd: str,
             resume: dict[str, Any] | None = None, link: Link | None = None) -> Question:
        """File a question, refusing anything malformed with nothing written. ``resume`` is what a later resume of
        the agent's session needs, stored as given: unsigned, never authority. ``link`` ties one of its options to
        the consent request already filed for that option's call (its ``question_id`` is ignored: it is this
        question), written in the same transaction, so a question never names a link it does not have."""
        return self._file(False, agent=agent, agent_session_id=agent_session_id, what=what, options=options, rec=rec,
                          blocks=blocks, cwd=cwd, resume=resume, link=link)[0]

    def file_once(self, *, agent: str, key: str, what: str, options: Any, rec: str, blocks: str, cwd: str,
                  held_by: tuple[str, ...] = ()) -> tuple[Question, bool]:
        """File a question a system source raises for a standing condition, once while it is open. ``key`` names the
        source in the question's session field (no session's hook ever claims it); the check and the insert run in one
        write transaction, so two writers racing file one. ``held_by``: answer channels whose answer under ``key``
        holds it as an open question does (a person who acknowledged the condition is not asked again under the same
        key). A system source may offer a single option (an acknowledgement). A read-only snapshot is asked first, so
        a key already held takes no write lock. Returns ``(question, filed)``: the question that holds ``key``
        already when there is one (``filed`` False), else the one just filed."""
        self.check(agent=agent, agent_session_id=key, what=what, options=options, rec=rec, blocks=blocks, cwd=cwd,
                   fewest=1)
        with self._peek() as conn:
            held = None if conn is None else self._holding(conn, key, held_by)
        if held is not None:
            return held, False
        return self._file(True, agent=agent, agent_session_id=key, what=what, options=options, rec=rec, blocks=blocks,
                          cwd=cwd, resume=None, link=None, held_by=held_by, fewest=1)

    def _holding(self, conn: sqlite3.Connection, key: str, held_by: tuple[str, ...]) -> Question | None:
        """The question that holds ``key`` (see :meth:`file_once`): an open one first, else one answered through a
        channel in ``held_by``."""
        marks = ", ".join("?" for _ in held_by)
        answered = f" OR a.channel IN ({marks})" if held_by else ""
        row = conn.execute(f"{_select(conn)} WHERE q.agent_session_id = ? AND (a.question_id IS NULL{answered}) "
                           "ORDER BY a.question_id IS NOT NULL, q.n LIMIT 1", (key, *held_by)).fetchone()
        return None if row is None else self._question_of(row)

    def close_open(self, key: str, *, option: str | None = None, text: str | None = None, channel: str
                   ) -> list[Question]:
        """Answer every open question :meth:`file_once` filed under ``key`` or under ``key:<anything>`` (one source,
        one key per episode of its condition) with exactly one of ``option`` or ``text``, by the rules :meth:`answer`
        holds: a person's act on ``channel``. A read-only snapshot is asked first, so with none open nothing takes the
        write lock; returns the questions closed (none: nothing written)."""
        kind, value = _answer_of(option, text)
        if channel not in ANSWER_CHANNELS:
            raise ConsentError(f"channel must be one of {', '.join(ANSWER_CHANNELS)}: {channel!r}")
        where = ("a.question_id IS NULL AND (q.agent_session_id = ? OR substr(q.agent_session_id, 1, ?) = ?) "
                 "ORDER BY q.n")
        args = (key, len(key) + 1, key + ":")
        with self._peek() as conn:
            if conn is None or conn.execute(f"{_select(conn)} WHERE {where} LIMIT 1", args).fetchone() is None:
                return []
        with self._write() as conn:
            closed = [self._question_of(r) for r in conn.execute(f"{_select(conn)} WHERE {where}", args).fetchall()]
            stamp = utc_stamp(_now_utc(self._clock))
            for q in closed:
                self._record(conn, q, kind, value, channel, stamp)
        return closed

    @staticmethod
    def _record(conn: sqlite3.Connection, q: Question, kind: str, value: str, channel: str, stamp: str) -> None:
        """Write ``q``'s one answer, refusing a second one and an option key it does not offer: the one answer write
        every answering path shares."""
        if q.answer is not None:
            raise ConsentError(f"question #{q.n} was already answered at {q.answer.answered_at}")
        if kind == "option" and value not in {k for k, _ in q.options}:
            raise ConsentError(f"{value!r} is not an option of question #{q.n}")
        conn.execute("INSERT INTO answers (question_id, kind, value, channel, answered_at) VALUES (?, ?, ?, ?, ?)",
                     (q.id, kind, value, channel, stamp))

    def _file(self, once: bool, *, agent: str, agent_session_id: str, what: str, options: Any, rec: str, blocks: str,
              cwd: str, resume: dict[str, Any] | None, link: Link | None, held_by: tuple[str, ...] = (),
              fewest: int = _OPTIONS_MIN) -> tuple[Question, bool]:
        what, opts, blocks = self.check(agent=agent, agent_session_id=agent_session_id, what=what, options=options,
                                        rec=rec, blocks=blocks, cwd=cwd, fewest=fewest)
        try:
            resume_json = None if resume is None else json.dumps(resume, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ConsentError(f"resume must be JSON-serialisable: {exc}") from exc
        if resume is not None and not isinstance(resume, dict):
            raise ConsentError("resume must be an object or None")
        if link is not None:
            if not isinstance(link, Link) or link.option not in {o["key"] for o in opts}:
                raise ConsentError("a link must name one of the question's option keys")
            if link.kind != "push":
                raise ConsentError(f"a link's kind must be push: {link.kind!r}")
            if not all(isinstance(v, str) and v and not _has_control(v, selectors=False)
                       for v in (link.request_id, link.queue_id)):
                raise ConsentError("a link names its request and queue by id")
        qid = "q-" + os.urandom(12).hex()
        stamp = utc_stamp(_now_utc(self._clock))
        with self._write() as conn:
            if once:
                held = self._holding(conn, agent_session_id, held_by)
                if held is not None:
                    return held, False
            conn.execute("INSERT INTO questions (id, agent, agent_session_id, what, options, rec, blocks, cwd, "
                         "filed_at, resume) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (qid, agent, agent_session_id, what, json.dumps(opts, ensure_ascii=False), rec, blocks,
                          cwd, stamp, resume_json))
            if link is not None:
                conn.execute("INSERT INTO question_links (question_id, option, kind, request_id, queue_id, "
                             "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (qid, link.option, link.kind, link.request_id, link.queue_id, stamp))
            made = self._by_ref(conn, qid)
        assert made is not None
        return made, True

    def get(self, ref: Any) -> Question | None:
        """A question by its id (``q-…``), its number, or ``#n`` / ``n`` as a string; ``None`` if there is none."""
        with self._peek() as conn:
            if conn is None:
                return None
            return self._by_ref(conn, ref)

    def open_questions(self, agent: str | None = None) -> list[Question]:
        """The unanswered questions (of ``agent``, if given), oldest first."""
        where, args = ("AND q.agent = ?", (agent,)) if agent is not None else ("", ())
        with self._peek() as conn:
            if conn is None:
                return []
            rows = conn.execute(f"{_select(conn)} WHERE a.question_id IS NULL {where} ORDER BY q.n", args).fetchall()
        return [self._question_of(r) for r in rows]

    def recent(self, limit: int = 50) -> list[Question]:
        """The newest ``limit`` questions, answered or not, newest first."""
        with self._peek() as conn:
            if conn is None:
                return []
            rows = conn.execute(f"{_select(conn)} ORDER BY q.n DESC LIMIT ?", (min(max(int(limit), 0), _N_MAX),)).fetchall()
        return [self._question_of(r) for r in rows]

    # --- answering -----------------------------------------------------------------------------------
    def answer(self, ref: Any, *, option: str | None = None, text: str | None = None, channel: str) -> Answer:
        """Answer a question once: exactly one of ``option`` (one of its keys) or ``text`` (kept verbatim, any
        characters but NUL). Nothing here authorises anything: it is what the agent will be told."""
        if (option is None) == (text is None):
            raise ConsentError("answer with exactly one of an option key or free text")
        if channel not in ANSWER_CHANNELS:
            raise ConsentError(f"channel must be one of {', '.join(ANSWER_CHANNELS)}: {channel!r}")
        kind, value = _answer_of(option, text)
        with self._write() as conn:
            q = self._by_ref(conn, ref)
            if q is None:
                raise ConsentError(f"no such question: {ref!r}")
            stamp = utc_stamp(_now_utc(self._clock))
            self._record(conn, q, kind, value, channel, stamp)
        return Answer(q.id, kind, value, channel, stamp)

    def answer_with_context(self, entries: list[dict[str, Any]], *, channel: str, schema: tuple[str, ...] = (),
                            deadline: float | None = None,
                            require: Callable[[sqlite3.Connection], None] | None = None
                            ) -> list[Answer | ConsentError]:
        """Answer several questions, each with its ``answer_context`` row, in ONE write transaction (Q17-ALL S2,
        D14): an entry's answer and its context are written together or not at all (a savepoint each), so no answer
        is ever stored, or seen by a delivery reader, without its provenance. ``entries``: dicts of ``ref``, exactly
        one of ``option`` / ``text`` (as :meth:`answer`), ``via`` (``seat`` / ``cli`` / ``cockpit`` / ``phone``),
        ``viewer``, ``seen_through``, ``note``. ``schema``: statements run first in the same transaction (the band's
        tables, ``answer_context`` among them). ``deadline`` (``time.monotonic()``): the write lock is waited for
        until then (:meth:`_write_by`), else the ordinary wait. Returns one result per entry, in order: its
        :class:`Answer`, or the :class:`ConsentError` that refused it (nothing written for it; the others proceed).
        The lock still busy at the deadline, or any other store failure: raised, and nothing is written.
        ``require``: a precondition run inside the same write transaction, after ``schema`` and before any entry (the
        band's surfaces check there that the answer's view is still the viewer's newest, S5a): whatever it raises
        rolls the transaction back whole and is raised."""
        if channel not in ANSWER_CHANNELS:
            raise ConsentError(f"channel must be one of {', '.join(ANSWER_CHANNELS)}: {channel!r}")
        out: list[Answer | ConsentError] = []
        with (self._write_by(deadline) if deadline is not None else self._write()) as conn:
            for stmt in schema:
                conn.execute(stmt)
            if require is not None:
                require(conn)
            stamp = utc_stamp(_now_utc(self._clock))
            for e in entries:
                conn.execute("SAVEPOINT answer_entry")
                try:
                    option, text, via, note = e.get("option"), e.get("text"), e.get("via"), e.get("note")
                    viewer, seen = e.get("viewer"), e.get("seen_through")
                    kind, value = _answer_of(option, text)
                    if via not in ("seat", "cli", "cockpit", "phone"):
                        raise ConsentError(f"via must be seat, cli, cockpit or phone: {via!r}")
                    if viewer is not None and (not isinstance(viewer, str) or "\x00" in viewer):
                        raise ConsentError("viewer must be a string with no NUL")
                    if seen is not None and (not isinstance(seen, int) or isinstance(seen, bool) or seen < 0):
                        raise ConsentError("seen_through must be a band number")
                    if note is not None and (not isinstance(note, str) or len(note) > _TEXT_MAX or "\x00" in note):
                        raise ConsentError(f"note must be at most {_TEXT_MAX} characters with no NUL")
                    q = self._by_ref(conn, e.get("ref"))
                    if q is None:
                        raise ConsentError(f"no such question: {e.get('ref')!r}")
                    self._record(conn, q, kind, value, channel, stamp)
                    conn.execute("INSERT INTO answer_context (question_id, via, viewer, seen_through, note) "
                                 "VALUES (?, ?, ?, ?, ?)", (q.id, via, viewer, seen, note))
                except (ConsentError, sqlite3.IntegrityError) as exc:
                    conn.execute("ROLLBACK TO answer_entry")
                    conn.execute("RELEASE answer_entry")
                    out.append(exc if isinstance(exc, ConsentError) else ConsentError(str(exc)))
                    continue
                conn.execute("RELEASE answer_entry")
                out.append(Answer(q.id, kind, value, channel, stamp))
        return out

    # --- delivery ------------------------------------------------------------------------------------
    def claim_answers_for(self, agent: str, agent_session_id: str) -> list[Question]:
        """This session's answered questions whose text has not reached it yet (never claimed, or claimed for a
        resume that failed: :meth:`settle_resume`), oldest answer first, each marked delivered under one write lock:
        an answer's text reaches its session once. The session match is exact; the caller passes the base session.
        With no store this is empty and creates nothing."""
        return self._claim(agent, agent_session_id, step=None)

    def lease_answers_for(self, agent: str, agent_session_id: str) -> list[Question]:
        """As :meth:`claim_answers_for`, for a RESUME that carries the texts as its prompt: each is claimed with a
        ``launching`` step, so the resumed session's own hooks find nothing to deliver again, and is delivered only
        when :meth:`settle_resume` confirms the resume (``started``); a ``failed`` one is deliverable again. A
        ``launching`` step nobody settles stays UNCONFIRMED (:meth:`delivery_state`): never counted delivered, never
        retried, since the resume may have run."""
        return self._claim(agent, agent_session_id, step="launching")

    def _claim(self, agent: str, agent_session_id: str, *, step: str | None) -> list[Question]:
        key = (agent, agent_session_id)
        where = (" WHERE q.agent = ? AND q.agent_session_id = ? AND a.question_id IS NOT NULL AND "
                 + _DELIVERABLE + " ORDER BY a.rowid")
        deadline = time.monotonic() + HOOK_BUSY_MS / 1000      # the read and the claim share it
        with self._peek() as conn:                            # read-only: no write lock when nothing is due
            if conn is None or not conn.execute(_select(conn) + where, key).fetchall():
                return []
        out: list[Question] = []
        with self._write_by(deadline) as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            for row in conn.execute(_select(conn) + where, key).fetchall():   # re-read under the lock
                q = self._question_of(row)
                if conn.execute("SELECT 1 FROM answer_deliveries WHERE question_id = ?", (q.id,)).fetchone() is None:
                    conn.execute("INSERT INTO answer_deliveries (question_id, agent_session_id, delivered_at) "
                                 "VALUES (?, ?, ?)", (q.id, agent_session_id, stamp))
                    if step is not None:
                        self._insert_step(conn, q.id, agent_session_id, step, stamp)
                else:
                    last = self._last_step(conn, q.id)
                    if last is None or last[0] != "failed":
                        continue
                    self._insert_step(conn, q.id, agent_session_id, step or "hook", stamp)
                out.append(q)
        return out

    @staticmethod
    def _last_step(conn: sqlite3.Connection, question_id: str) -> tuple[str, str] | None:
        return conn.execute("SELECT outcome, created_at FROM answer_resume_steps WHERE question_id = ? "
                            "ORDER BY seq DESC LIMIT 1", (question_id,)).fetchone()

    @staticmethod
    def _insert_step(conn: sqlite3.Connection, question_id: str, agent_session_id: str, outcome: str, stamp: str,
                     argv: list[str] | None = None, cwd: str | None = None, detail: str | None = None) -> None:
        conn.execute("INSERT INTO answer_resume_steps (question_id, agent_session_id, outcome, argv, cwd, detail, "
                     "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (question_id, agent_session_id, outcome,
                      None if argv is None else json.dumps(argv, ensure_ascii=False), cwd, detail, stamp))

    def settle_resume(self, question_ids: list[str], *, agent_session_id: str, outcome: str, argv: list[str],
                      cwd: str, detail: str | None = None) -> None:
        """Record what became of the resume that leased these answers (:meth:`lease_answers_for`): ``started``
        (confirmed: the texts are delivered) or ``failed`` (deliverable again), with what was run. Only a question
        whose latest step is still ``launching``, leased for this same session, is settled. Unsigned."""
        if outcome not in ("started", "failed"):
            raise ConsentError(f"outcome must be started or failed: {outcome!r}")
        with self._write() as conn:
            stamp = utc_stamp(_now_utc(self._clock))
            for qid in question_ids:
                last = conn.execute("SELECT outcome, agent_session_id FROM answer_resume_steps WHERE question_id = ? "
                                    "ORDER BY seq DESC LIMIT 1", (qid,)).fetchone()
                if last is not None and last[0] == "launching" and last[1] == agent_session_id:
                    self._insert_step(conn, qid, agent_session_id, outcome, stamp, argv, cwd, detail)

    def delivery_state(self, ref: Any) -> tuple[str, str | None]:
        """Where the question's answer text is, with when it got there: ``undelivered`` (never claimed),
        ``delivered`` (claimed by a hook, or by a resume that was confirmed), ``failed`` (its resume failed:
        deliverable again) or ``unconfirmed`` (claimed for a resume that was never settled: it may have run)."""
        with self._peek() as conn:
            if conn is None:
                return "undelivered", None
            q = self._by_ref(conn, ref)
            if q is None:
                return "undelivered", None
            claimed = conn.execute("SELECT delivered_at FROM answer_deliveries WHERE question_id = ?",
                                   (q.id,)).fetchone()
            last = None if claimed is None else self._last_step(conn, q.id)
        if claimed is None:
            return "undelivered", None
        if last is None:
            return "delivered", claimed[0]
        return {"launching": "unconfirmed", "started": "delivered", "hook": "delivered"}.get(last[0], "failed"), last[1]

    def undelivered(self) -> list[Question]:
        """The answered questions whose text is not known to have reached their session (undelivered, failed or
        unconfirmed: :meth:`delivery_state`), oldest answer first."""
        with self._peek() as conn:
            if conn is None:
                return []
            rows = conn.execute(f"{_select(conn)} WHERE a.question_id IS NOT NULL AND (" + _DELIVERABLE + " OR "
                                + _LAST_STEP + " = 'launching') ORDER BY a.rowid").fetchall()
        return [self._question_of(r) for r in rows]


    def resume_meta(self, ref: Any) -> dict[str, Any] | None:
        """What ``levain ask`` recorded for a later resume of the asking session (unsigned, never authority), or
        ``None`` when nothing was recorded, the store does not exist or there is no such question."""
        parsed = _parse_ref(ref)
        if parsed is None:
            return None
        with self._peek() as conn:
            if conn is None:
                return None
            row = conn.execute(f"SELECT resume FROM questions WHERE {parsed[0]} = ?", (parsed[1],)).fetchone()
        if row is None or row[0] is None:
            return None
        got = json.loads(row[0])
        return got if isinstance(got, dict) else None

    def session_resume_meta(self, agent: str, agent_session_id: str) -> dict[str, Any] | None:
        """The newest resume record any question of this session carries, or ``None``."""
        with self._peek() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT resume FROM questions WHERE agent = ? AND agent_session_id = ? AND resume IS "
                               "NOT NULL ORDER BY n DESC LIMIT 1", (agent, agent_session_id)).fetchone()
        got = None if row is None else json.loads(row[0])
        return got if isinstance(got, dict) else None
