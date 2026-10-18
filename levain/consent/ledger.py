"""The authority ledger (Q15, DESIGN section 3.5): one row per tool call an agent made, and on whose authority.

A store ``<levain home>/consent/ledger.db`` beside ``queue.db`` (same directory checks, WAL through
:func:`levain.autonomic.durable_sqlite.open_durable`, format :data:`STORE_FORMAT`). A row is written by the hooks
(PreToolUse of a call the gate itself denied, PostToolUse, PostToolUseFailure) and carries the call's identity, a
DIGEST of its tool input (never the input, the command text or any file content), and the authority STAMP.

The stamp is read back from the agent's own record (:mod:`levain.consent.stamps`), which is often not written yet
when the hook fires, so a row may be filed ``pending`` and stamped once, later (:meth:`Ledger.fill_stamp`, or
:func:`reconcile` over every pending row). That is the ONLY update a row ever takes: a trigger refuses every other
change and every delete. Levain's own authority (grant, answer, ruling) is stamped at filing, with the queue
request id that answered the call.

A GAP is a window on this ledger (:meth:`Ledger.window`): the rows of one session from the undecided time to the
ruling. The weekly unseen digest (:meth:`Ledger.digest`) is the rows a standing grant or a late ruling answered.
:meth:`Ledger.counts` counts by agent and stamp (row D reads spent-vs-authorized from the same rows).
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import utc_stamp
from levain.consent import stamps as _stamps
from levain.consent.queue import QueueStoreError, default_queue_dir
from levain.consent.record import AGENTS, tool_input_digest

__all__ = ["DB_NAME", "DIGEST_STAMPS", "PHASES", "STORE_FORMAT", "Call", "Ledger", "LedgerStoreError",
           "parse_when", "reconcile"]

DB_NAME = "ledger.db"
STORE_FORMAT = "levain-authority-ledger/1"
PHASES: tuple[str, ...] = ("pre", "post", "post_failure", "denied_by_gate")
FINAL_STAMPS: tuple[str, ...] = tuple(s for s in _stamps.STAMPS if s != "pending")
# the rows the weekly unseen digest lists: a standing grant or a late ruling answered them, and he was not asked
DIGEST_STAMPS: tuple[str, ...] = ("grant", "ruling")
_ACCESS_BY_OTHERS = 0o077
_COLUMNS = ("id", "agent", "agent_session_id", "tool_use_id", "received_at", "tool_name", "tool_input_digest", "cwd",
            "phase", "stamp", "stamp_detail", "request_id", "stamped_at", "source_path", "turn_id", "permission_mode")


class LedgerStoreError(QueueStoreError):
    """The ledger store cannot be trusted: another format, a directory others can reach, one that vanished."""


def _sql_list(values: tuple[str, ...]) -> str:
    return ",".join("'" + v + "'" for v in values)


_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS calls ("
    "id TEXT PRIMARY KEY, "
    f"agent TEXT NOT NULL CHECK (agent IN ({_sql_list(AGENTS)})), "
    "agent_session_id TEXT NOT NULL, tool_use_id TEXT, received_at TEXT NOT NULL, tool_name TEXT NOT NULL, "
    "tool_input_digest TEXT NOT NULL, cwd TEXT NOT NULL, "
    f"phase TEXT NOT NULL CHECK (phase IN ({_sql_list(PHASES)})), "
    f"stamp TEXT NOT NULL CHECK (stamp IN ({_sql_list(_stamps.STAMPS)})), "
    "stamp_detail TEXT, request_id TEXT, stamped_at TEXT, "
    # where the stamp is read from, and the mode the hook reported: a path and two ids, never content
    "source_path TEXT, turn_id TEXT, permission_mode TEXT, "
    "CHECK ((stamp = 'pending') = (stamped_at IS NULL)))",
    # one row per call per phase: a hook that fires twice (a retry) files it once
    "CREATE UNIQUE INDEX IF NOT EXISTS calls_once ON calls (agent, agent_session_id, tool_use_id, phase) "
    "WHERE tool_use_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS calls_window ON calls (agent, agent_session_id, received_at)",
    "CREATE INDEX IF NOT EXISTS calls_time ON calls (received_at)",
    "CREATE INDEX IF NOT EXISTS calls_pending ON calls (agent, agent_session_id) WHERE stamp = 'pending'",
    # append-only: the one move is pending -> a final stamp, once, with its detail and time; nothing else changes
    "CREATE TRIGGER IF NOT EXISTS calls_frozen BEFORE UPDATE ON calls WHEN NOT ("
    "OLD.stamp = 'pending' AND NEW.stamp <> 'pending' AND NEW.stamped_at IS NOT NULL "
    "AND NEW.id IS OLD.id AND NEW.agent IS OLD.agent AND NEW.agent_session_id IS OLD.agent_session_id "
    "AND NEW.tool_use_id IS OLD.tool_use_id AND NEW.received_at IS OLD.received_at "
    "AND NEW.tool_name IS OLD.tool_name AND NEW.tool_input_digest IS OLD.tool_input_digest "
    "AND NEW.cwd IS OLD.cwd AND NEW.phase IS OLD.phase AND NEW.request_id IS OLD.request_id "
    "AND NEW.source_path IS OLD.source_path AND NEW.turn_id IS OLD.turn_id "
    "AND NEW.permission_mode IS OLD.permission_mode) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row moves only from pending to a final stamp, once'); END",
    "CREATE TRIGGER IF NOT EXISTS calls_kept BEFORE DELETE ON calls "
    "BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END",
)


@dataclass(frozen=True, slots=True)
class Call:
    """One ledger row."""
    id: str
    agent: str
    agent_session_id: str
    tool_use_id: str | None
    received_at: str
    tool_name: str
    tool_input_digest: str
    cwd: str
    phase: str
    stamp: str
    stamp_detail: Mapping[str, Any] | None
    request_id: str | None
    stamped_at: str | None
    source_path: str | None
    turn_id: str | None
    permission_mode: str | None

    @staticmethod
    def _of(row: sqlite3.Row | tuple[Any, ...]) -> Call:
        values = dict(zip(_COLUMNS, row))
        detail = values["stamp_detail"]
        values["stamp_detail"] = json.loads(detail) if detail else None
        return Call(**values)


def parse_when(text: str | datetime, *, now: datetime | None = None) -> datetime:
    """An aware UTC datetime from an ISO stamp, a bare ``YYYY-MM-DD`` (UTC midnight), or a relative age such as
    ``7d``, ``36h`` or ``90m`` (that long before ``now``)."""
    if isinstance(text, datetime):
        if text.tzinfo is None:
            raise ValueError("a naive datetime has no zone; pass an aware one")
        return text.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    text = text.strip()
    if len(text) >= 2 and text[-1] in "dhm" and text[:-1].isdigit():
        n = int(text[:-1])
        return now - {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[text[-1]]
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _stamp_of(when: str | datetime | None, default: datetime | None = None) -> str | None:
    if when is None:
        return utc_stamp(default) if default is not None else None
    return utc_stamp(parse_when(when))


class Ledger:
    """The ledger in ``directory`` (default :func:`levain.consent.queue.default_queue_dir`, beside ``queue.db``).
    Every operation opens its own connection, so one object is safe across threads and processes."""

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
            raise LedgerStoreError(f"consent directory {self.directory} is not a directory")
        if st.st_uid != os.getuid():
            raise LedgerStoreError(f"consent directory {self.directory} is not owned by uid {os.getuid()}")
        if st.st_mode & _ACCESS_BY_OTHERS:
            raise LedgerStoreError(f"consent directory {self.directory} is reachable by group or others "
                                   f"(mode {st.st_mode & 0o777:o}); it must be 0700")

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise LedgerStoreError(f"authority ledger {self.path} has disappeared")
        else:
            self._check_directory()
        conn = open_durable(self.path)
        try:
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
                raise LedgerStoreError("the database holds tables but no authority-ledger format marker")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if tables else None
            if row is not None and row[0] != STORE_FORMAT:
                raise LedgerStoreError(f"store format {row[0]!r} is not {STORE_FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('ledger_id', ?)",
                         ("al-" + os.urandom(16).hex(),))
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with read_transaction(self._connect) as conn:
            yield conn

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the ledger clock must return an aware datetime")
        return now

    # --- writes --------------------------------------------------------------------------------------
    def record_call(self, *, agent: str, agent_session_id: str, tool_name: str, tool_input: Any, cwd: str,
                    phase: str, tool_use_id: str | None = None, stamp: str = "pending",
                    stamp_detail: Mapping[str, Any] | None = None, request_id: str | None = None,
                    received_at: str | datetime | None = None, source_path: str | None = None,
                    turn_id: str | None = None, permission_mode: str | None = None) -> str:
        """File one call and return its row id. Only the DIGEST of ``tool_input`` is kept. Idempotent per
        ``(agent, agent_session_id, tool_use_id, phase)``: filing the same call again returns the first row and
        changes nothing (a call without a ``tool_use_id`` is never matched, so it is always a new row).

        ``stamp`` defaults to ``pending`` (read ``source_path`` later); a final stamp (levain's own grant, answer or
        ruling, with the answering ``request_id``) is stamped now."""
        if agent not in AGENTS:
            raise ValueError(f"agent {agent!r} is not one of {AGENTS}")
        if phase not in PHASES:
            raise ValueError(f"phase {phase!r} is not one of {PHASES}")
        if stamp not in _stamps.STAMPS:
            raise ValueError(f"stamp {stamp!r} is not one of {_stamps.STAMPS}")
        if not agent_session_id or not tool_name:
            raise ValueError("a ledger row needs a session id and a tool name")
        now = self._now()
        row_id = "lc-" + os.urandom(12).hex()
        detail = json.dumps(dict(stamp_detail), sort_keys=True, separators=(",", ":")) if stamp_detail else None
        values = (row_id, agent, agent_session_id, tool_use_id, _stamp_of(received_at, now), tool_name,
                  tool_input_digest(tool_input), cwd, phase, stamp, detail, request_id,
                  None if stamp == "pending" else utc_stamp(now), source_path, turn_id, permission_mode)
        with self._write() as conn:
            conn.execute(f"INSERT OR IGNORE INTO calls ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})",
                         values)
            if tool_use_id is None:
                return row_id
            found = conn.execute("SELECT id FROM calls WHERE agent = ? AND agent_session_id = ? AND tool_use_id = ? "
                                 "AND phase = ?", (agent, agent_session_id, tool_use_id, phase)).fetchone()
            return found[0]

    def fill_stamp(self, call_id: str, stamp: str, stamp_detail: Mapping[str, Any] | None = None) -> bool:
        """Stamp a ``pending`` row, once. False when the row is not pending (already stamped, or no such row);
        the trigger refuses any other change."""
        if stamp not in FINAL_STAMPS:
            raise ValueError(f"stamp {stamp!r} is not a final stamp ({FINAL_STAMPS})")
        detail = json.dumps(dict(stamp_detail), sort_keys=True, separators=(",", ":")) if stamp_detail else None
        with self._write() as conn:
            cur = conn.execute("UPDATE calls SET stamp = ?, stamp_detail = ?, stamped_at = ? "
                               "WHERE id = ? AND stamp = 'pending'", (stamp, detail, utc_stamp(self._now()), call_id))
            return cur.rowcount == 1

    # --- reads ---------------------------------------------------------------------------------------
    def get(self, call_id: str) -> Call | None:
        with self._read() as conn:
            row = conn.execute(f"SELECT {','.join(_COLUMNS)} FROM calls WHERE id = ?", (call_id,)).fetchone()
        return Call._of(row) if row else None

    def _select(self, where: str, args: tuple[Any, ...]) -> list[Call]:
        with self._read() as conn:
            rows = conn.execute(f"SELECT {','.join(_COLUMNS)} FROM calls WHERE {where} "
                                "ORDER BY received_at, rowid", args).fetchall()
        return [Call._of(r) for r in rows]

    def pending_for(self, agent: str, agent_session_id: str) -> list[Call]:
        """The session's rows still waiting for their stamp, oldest first."""
        return self._select("agent = ? AND agent_session_id = ? AND stamp = 'pending'", (agent, agent_session_id))

    def pending(self) -> list[Call]:
        """Every row still waiting for its stamp, oldest first."""
        return self._select("stamp = 'pending'", ())

    def window(self, agent: str, agent_session_id: str, since: str | datetime, until: str | datetime | None = None
               ) -> list[Call]:
        """The session's rows with ``since <= received_at <= until`` (``until`` None = now): a gap is the window
        from its undecided time to its ruling."""
        lo, hi = _stamp_of(since), _stamp_of(until, self._now())
        return self._select("agent = ? AND agent_session_id = ? AND received_at >= ? AND received_at <= ?",
                            (agent, agent_session_id, lo, hi))

    def digest(self, since: str | datetime, until: str | datetime | None = None) -> list[Call]:
        """The unseen digest: rows in the window a standing grant or a late ruling answered (a signed answer was
        seen by the person who signed it)."""
        lo, hi = _stamp_of(since), _stamp_of(until, self._now())
        marks = ",".join("?" * len(DIGEST_STAMPS))
        return self._select(f"received_at >= ? AND received_at <= ? AND stamp IN ({marks})", (lo, hi, *DIGEST_STAMPS))

    def counts(self, since: str | datetime | None = None, until: str | datetime | None = None
               ) -> list[tuple[str, str, int]]:
        """``(agent, stamp, rows)`` over the window, sorted. No ``since`` counts from the start."""
        lo = _stamp_of(since) if since is not None else ""
        hi = _stamp_of(until, self._now())
        with self._read() as conn:
            rows = conn.execute("SELECT agent, stamp, COUNT(*) FROM calls WHERE received_at >= ? "
                                "AND received_at <= ? GROUP BY agent, stamp ORDER BY agent, stamp", (lo, hi)).fetchall()
        return [(a, s, n) for a, s, n in rows]


def _read_stamp(call: Call) -> tuple[str, Mapping[str, Any] | None] | None:
    """The final stamp and its detail for a pending row from its source, or None to leave it pending."""
    if call.agent == "claude_code":
        state, decision = _stamps.claude_result_state(call.source_path, call.tool_use_id)
        if state == "decided":
            return _stamps.claude_stamp(decision, call.permission_mode), decision
        if state == "missing_file":
            return "unknown", {"reason": "transcript_missing"}
        if state == "no_decision":
            return "unknown", {"reason": "result_without_decision"}
        return None
    if call.agent == "codex":
        if not call.source_path or not os.path.isfile(call.source_path):
            return "unknown", {"reason": "rollout_missing"}
        policy = _stamps.codex_turn_policy(call.source_path, call.turn_id)
        if policy is None:
            return None
        stamp = _stamps.codex_stamp(policy)
        return stamp, policy
    return None


def reconcile(ledger: Ledger, *, give_up_after: timedelta | None = timedelta(hours=1),
              agent: str | None = None, agent_session_id: str | None = None) -> dict[str, int]:
    """For every pending row, re-read its stamp source and fill it. A source that is not there yet leaves the row
    pending; one that cannot ever be there (no file) or has no decision stamps it ``unknown`` (never allow/deny);
    a row still undecided after ``give_up_after`` (None: never) is stamped ``unknown`` too. Returns the counts
    ``{"filled", "unknown", "still_pending"}`` (``unknown`` is a subset of ``filled``)."""
    out = {"filled": 0, "unknown": 0, "still_pending": 0}
    now = ledger._now()
    rows = ledger.pending_for(agent, agent_session_id) if agent and agent_session_id else ledger.pending()
    for call in rows:
        if agent and call.agent != agent:
            continue
        found = _read_stamp(call)
        if found is None and give_up_after is not None:
            age = now - datetime.strptime(call.received_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            if age >= give_up_after:
                found = ("unknown", {"reason": "source_never_wrote_it"})
        if found is None:
            out["still_pending"] += 1
            continue
        stamp, detail = found
        if ledger.fill_stamp(call.id, stamp, detail):
            out["filled"] += 1
            out["unknown"] += stamp == "unknown"
    return out

