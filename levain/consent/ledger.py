"""The authority ledger (Q15, DESIGN section 3.5): one row per tool call an agent made, and on whose authority.

A store ``<levain home>/consent/ledger.db`` beside ``queue.db`` (same directory checks, WAL through
:func:`levain.autonomic.durable_sqlite.open_durable`, format :data:`STORE_FORMAT`). A row is written by the hooks
(PreToolUse of a call the gate itself denied, PostToolUse, PostToolUseFailure) and carries the call's identity, a
DIGEST of its tool input (never the input, the command text or any file content), and the authority STAMP.

The stamp is read back from the agent's own record (:mod:`levain.consent.stamps`), which is often not written yet
when the hook fires, so a row may be filed ``pending`` and stamped once, later (:meth:`Ledger.fill_stamp`, or
:func:`reconcile` over every pending row). That is the ONLY update a row ever takes: a trigger refuses every other
UPDATE and every DELETE statement, and another refuses an INSERT of an id that exists (so ``INSERT OR REPLACE`` cannot
rewrite a row by id). The file itself is only as safe as its 0600 mode and the uid: the triggers guard the statements
levain writes, not an owner who drops them. Levain's own authority (grant, answer, ruling) is stamped at filing, with the queue
request id that answered the call.

A GAP is a window on this ledger (:meth:`Ledger.window`): the rows of one session from the undecided time to the
ruling. The weekly unseen digest (:meth:`Ledger.digest`) is the rows a standing grant or a late ruling answered.
:meth:`Ledger.counts` counts by agent and stamp (row D reads spent-vs-authorized from the same rows).

OUTCOME beside STAMP: what the authority did (``accepted`` / ``refused`` / ``unknown``). A refusal authorised nothing,
so a refused row's stamp is only ever ``unknown`` or ``pending`` (a CHECK, not a convention), and ``counts`` splits by
outcome. ``stamp_detail`` is WHITELISTED (:func:`clean_detail`): the decision's ``decision``/``source``/``reasonType``,
a Codex turn's ``approval_policy``/``approvals_reviewer``/sandbox ``type``/profile ``type``, and levain's own short
reasons; anything else a caller or a newer agent version adds is dropped, never stored.

Known edges (one line each): the input digest is unsalted, so a SHORT input is recoverable by dictionary; a Codex
policy that changes mid-turn is read from the turn's FIRST ``turn_context``; ``counts`` counts the pre and the post
row of one call separately; ``INSERT OR REPLACE`` by the (agent, session, tool_use_id, phase) tuple, not the id, is
not refused by a trigger; a ledger.db made by fedba7b1 (before the ``outcome`` column) is refused, delete it.
"""
from __future__ import annotations

import json
import os
import re
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
           "clean_detail", "parse_when", "reconcile"]

DB_NAME = "ledger.db"
STORE_FORMAT = "levain-authority-ledger/1"
PHASES: tuple[str, ...] = ("pre", "post", "post_failure", "denied_by_gate")
FINAL_STAMPS: tuple[str, ...] = tuple(s for s in _stamps.STAMPS if s != "pending")
# the rows the weekly unseen digest lists: a standing grant or a late ruling answered them, and he was not asked
DIGEST_STAMPS: tuple[str, ...] = ("grant", "ruling")
_ACCESS_BY_OTHERS = 0o077
_COLUMNS = ("id", "agent", "agent_session_id", "tool_use_id", "received_at", "tool_name", "tool_input_digest", "cwd",
            "phase", "stamp", "outcome", "stamp_detail", "request_id", "stamped_at", "source_path", "turn_id",
            "permission_mode")
# the only free text the ledger keeps about a stamp: levain's own reasons, from this list
_REASONS = frozenset({"transcript_missing", "transcript_unreadable", "rollout_missing", "rollout_unreadable",
                      "result_not_written", "result_without_decision", "no_tool_use_id", "no_source", "no_turn_id",
                      "turn_not_written", "turn_policy_unreadable", "result_oversized"})
_DETAIL_STR_KEYS = ("decision", "source", "reasonType", "approval_policy", "approvals_reviewer")
_DETAIL_TYPE_KEYS = ("sandbox_policy", "permission_profile")
_MAX_DETAIL_STR = 64


def clean_detail(detail: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The whitelisted part of a ``stamp_detail`` (None when nothing is left). Claude Code: ``decision``, ``source``,
    ``reasonType``. Codex: ``approval_policy``, ``approvals_reviewer``, and the ``type`` of ``sandbox_policy`` and
    ``permission_profile``. Levain's own: ``reason`` (from a fixed list), ``skipped_lines`` (counts), ``gave_up``.
    Every other key, and any value that is not a short string, is dropped: a free-text field must never reach the db."""
    if not isinstance(detail, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in _DETAIL_STR_KEYS:
        value = detail.get(key)
        if isinstance(value, str) and len(value) <= _MAX_DETAIL_STR:
            out[key] = value
    for key in _DETAIL_TYPE_KEYS:
        value = detail.get(key)
        kind = value.get("type") if isinstance(value, Mapping) else value
        if isinstance(kind, str) and len(kind) <= _MAX_DETAIL_STR:
            out[key] = {"type": kind}
    if isinstance(detail.get("reason"), str) and detail["reason"] in _REASONS:
        out["reason"] = detail["reason"]
    skipped = detail.get("skipped_lines")
    if isinstance(skipped, Mapping):
        kept = {k: int(v) for k, v in skipped.items() if k in _stamps.SKIP_REASONS and isinstance(v, int) and v > 0}
        if kept:
            out["skipped_lines"] = kept
    if detail.get("gave_up") is True:
        out["gave_up"] = True
    return out or None


def _dump(detail: Mapping[str, Any] | None) -> str | None:
    cleaned = clean_detail(detail)
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":")) if cleaned else None


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
    f"outcome TEXT NOT NULL CHECK (outcome IN ({_sql_list(_stamps.OUTCOMES)})), "
    "stamp_detail TEXT, request_id TEXT, stamped_at TEXT, "
    # where the stamp is read from, and the mode the hook reported: a path and two ids, never content
    "source_path TEXT, turn_id TEXT, permission_mode TEXT, "
    "CHECK ((stamp = 'pending') = (stamped_at IS NULL)), "
    # a refusal authorised nothing: it can never carry an authority stamp
    "CHECK (outcome <> 'refused' OR stamp IN ('unknown', 'pending')))",
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
    # INSERT OR REPLACE deletes the old row without firing a delete trigger; refuse a new row that reuses an id
    "CREATE TRIGGER IF NOT EXISTS calls_no_replace BEFORE INSERT ON calls "
    "WHEN EXISTS (SELECT 1 FROM calls WHERE id = NEW.id) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row id is never reused'); END",
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
    outcome: str
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


_AGE = re.compile(r"[0-9]{1,9}[dhm]", re.ASCII)


def parse_when(text: str | datetime, *, now: datetime | None = None) -> datetime:
    """An aware UTC datetime from an ISO stamp, a bare ``YYYY-MM-DD`` (UTC midnight), or a relative age such as
    ``7d``, ``36h`` or ``90m`` (that long before ``now``)."""
    if isinstance(text, datetime):
        if text.tzinfo is None:
            raise ValueError("a naive datetime has no zone; pass an aware one")
        return text.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    text = text.strip()
    if _AGE.fullmatch(text):
        unit = {"d": "days", "h": "hours", "m": "minutes"}[text[-1]]
        try:
            return now - timedelta(**{unit: int(text[:-1])})
        except OverflowError:
            raise ValueError(f"age {text!r} is out of range") from None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _stamp_of(when: str | datetime | None, default: datetime | None = None, *, now: datetime | None = None
              ) -> str | None:
    if when is None:
        return utc_stamp(default) if default is not None else None
    return utc_stamp(parse_when(when, now=now))


def _check_outcome(stamp: str, outcome: str) -> None:
    if outcome not in _stamps.OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {_stamps.OUTCOMES}")
    if outcome == "refused" and stamp not in ("unknown", "pending"):
        raise ValueError(f"a refused call authorised nothing: its stamp is unknown, not {stamp!r}")


def _need(when: str | datetime | None, now: datetime) -> str:
    if when is None:
        raise ValueError("this window needs a start time")
    return _stamp_of(when, now=now)  # type: ignore[return-value]


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
            if "calls" in tables and "outcome" not in {r[1] for r in conn.execute("PRAGMA table_info(calls)")}:
                raise LedgerStoreError(f"store format {STORE_FORMAT!r} calls table lacks the `outcome` column "
                                       "(a pre-outcome ledger.db): delete the file")
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
                    turn_id: str | None = None, permission_mode: str | None = None,
                    outcome: str | None = None) -> str:
        """File one call and return its row id. Only the DIGEST of ``tool_input`` is kept, and ``stamp_detail`` is
        whitelisted (:func:`clean_detail`). Idempotent per ``(agent, agent_session_id, tool_use_id, phase)``: filing
        the same call again returns the first row and changes nothing (a call without a ``tool_use_id`` is never
        matched, so it is always a new row). Every NOT NULL field is validated up front (``ValueError``); a store
        failure other than that one dedup conflict raises, so a call is never silently dropped.

        ``stamp`` defaults to ``pending`` (read ``source_path`` later); a final stamp (levain's own grant, answer or
        ruling, with the answering ``request_id``) is stamped now. ``outcome`` defaults from the phase and stamp
        (``denied_by_gate`` -> refused; pending or unknown -> unknown; else accepted)."""
        if agent not in AGENTS:
            raise ValueError(f"agent {agent!r} is not one of {AGENTS}")
        if phase not in PHASES:
            raise ValueError(f"phase {phase!r} is not one of {PHASES}")
        if stamp not in _stamps.STAMPS:
            raise ValueError(f"stamp {stamp!r} is not one of {_stamps.STAMPS}")
        for name, value in (("agent_session_id", agent_session_id), ("tool_name", tool_name), ("cwd", cwd)):
            if not isinstance(value, str) or not value:
                raise ValueError(f"a ledger row needs a non-empty {name} (got {value!r})")
        if tool_use_id is not None and (not isinstance(tool_use_id, str) or not tool_use_id):
            raise ValueError(f"tool_use_id must be a non-empty string or None (got {tool_use_id!r})")
        if outcome is None:
            outcome = ("refused" if phase == "denied_by_gate" and stamp in ("pending", "unknown")
                       else "unknown" if stamp in ("pending", "unknown") else "accepted")
        _check_outcome(stamp, outcome)
        now = self._now()
        row_id = "lc-" + os.urandom(12).hex()
        values = (row_id, agent, agent_session_id, tool_use_id, _stamp_of(received_at, now, now=now), tool_name,
                  tool_input_digest(tool_input), cwd, phase, stamp, outcome, _dump(stamp_detail), request_id,
                  None if stamp == "pending" else utc_stamp(now), source_path, turn_id, permission_mode)
        with self._write() as conn:
            try:
                conn.execute(f"INSERT INTO calls ({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})",
                             values)
            except sqlite3.IntegrityError as e:
                # ONLY the idempotency index (calls_once) is a dedup; any other constraint failure is a real error
                if tool_use_id is None or "UNIQUE constraint failed" not in str(e) \
                        or "calls.tool_use_id" not in str(e):
                    raise
                found = conn.execute("SELECT id FROM calls WHERE agent = ? AND agent_session_id = ? "
                                     "AND tool_use_id = ? AND phase = ?",
                                     (agent, agent_session_id, tool_use_id, phase)).fetchone()
                if found is None:
                    raise
                return found[0]
        return row_id

    def fill_stamp(self, call_id: str, stamp: str, stamp_detail: Mapping[str, Any] | None = None,
                   outcome: str | None = None) -> bool:
        """Stamp a ``pending`` row, once. False when the row is not pending (already stamped, or no such row);
        the trigger refuses any other change. ``outcome`` defaults to ``unknown`` for an ``unknown`` stamp, else
        ``accepted``; a ``refused`` outcome needs the ``unknown`` stamp."""
        if stamp not in FINAL_STAMPS:
            raise ValueError(f"stamp {stamp!r} is not a final stamp ({FINAL_STAMPS})")
        if outcome is None:
            outcome = "unknown" if stamp == "unknown" else "accepted"
        _check_outcome(stamp, outcome)
        with self._write() as conn:
            cur = conn.execute("UPDATE calls SET stamp = ?, outcome = ?, stamp_detail = ?, stamped_at = ? "
                               "WHERE id = ? AND stamp = 'pending'",
                               (stamp, outcome, _dump(stamp_detail), utc_stamp(self._now()), call_id))
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
        now = self._now()
        lo, hi = _need(since, now), _stamp_of(until, now, now=now)
        return self._select("agent = ? AND agent_session_id = ? AND received_at >= ? AND received_at <= ?",
                            (agent, agent_session_id, lo, hi))

    def digest(self, since: str | datetime, until: str | datetime | None = None) -> list[Call]:
        """The unseen digest: rows in the window a standing grant or a late ruling answered (a signed answer was
        seen by the person who signed it)."""
        now = self._now()
        lo, hi = _need(since, now), _stamp_of(until, now, now=now)
        marks = ",".join("?" * len(DIGEST_STAMPS))
        return self._select(f"received_at >= ? AND received_at <= ? AND stamp IN ({marks})", (lo, hi, *DIGEST_STAMPS))

    def counts(self, since: str | datetime | None = None, until: str | datetime | None = None
               ) -> list[tuple[str, str, str, int]]:
        """``(agent, stamp, outcome, rows)`` over the window, sorted. No ``since`` counts from the start. A call's
        pre and post rows are counted separately."""
        now = self._now()
        lo = _stamp_of(since, now=now) if since is not None else ""
        hi = _stamp_of(until, now, now=now)
        with self._read() as conn:
            rows = conn.execute("SELECT agent, stamp, outcome, COUNT(*) FROM calls WHERE received_at >= ? "
                                "AND received_at <= ? GROUP BY agent, stamp, outcome ORDER BY agent, stamp, outcome",
                                (lo, hi)).fetchall()
        return [(a, s, o, n) for a, s, o, n in rows]


# a resolution is ("final", stamp, detail, outcome) or ("wait", cause, detail): leave the row pending for now
_Resolution = tuple[Any, ...]


def _final(stamp: str, detail: Mapping[str, Any] | None, outcome: str) -> _Resolution:
    return ("final", stamp, detail, outcome)


def _wait(cause: str, detail: Mapping[str, Any] | None = None) -> _Resolution:
    return ("wait", cause, detail)


def _claude_resolution(call: Call, scans: Mapping[str, _stamps.ClaudeScan]) -> _Resolution:
    if not call.tool_use_id:
        return _final("unknown", {"reason": "no_tool_use_id"}, "unknown")      # the join can never be made
    if not call.source_path:
        return _final("unknown", {"reason": "no_source"}, "unknown")
    scan = scans[call.source_path]
    state, decision = scan.states[call.tool_use_id]
    skipped = {"skipped_lines": dict(scan.skipped)}
    if state == "decided":
        return _final(_stamps.claude_stamp(decision, call.permission_mode), decision, _stamps.claude_outcome(decision))
    if state == "no_decision":
        return _final("unknown", {"reason": "result_without_decision", **skipped}, "unknown")
    # the transcript may not exist yet, may be briefly unreadable, may not have the result yet: wait, don't conclude
    cause = {"missing_file": "transcript_missing", "unreadable": "transcript_unreadable",
             "result_oversized": "result_oversized"}.get(state, "result_not_written")
    return _wait(cause, skipped)


def _codex_resolution(call: Call, policies: Mapping[str, Mapping[str, Any] | None], last_turn: bool) -> _Resolution:
    if not call.source_path:
        return _final("unknown", {"reason": "no_source"}, "unknown")
    if call.turn_id is None and not last_turn:
        # never "the last turn": a later turn may have changed the policy, and the call would borrow it
        return _wait("no_turn_id")
    if not os.path.isfile(call.source_path):
        return _wait("rollout_missing")
    turns = policies.get(call.source_path)
    if turns is None:
        return _wait("rollout_unreadable")
    if call.turn_id is None:
        policy = list(turns.values())[-1] if turns else None
    else:
        policy = turns.get(call.turn_id)
    if policy is None:
        return _wait("turn_not_written")
    stamp = _stamps.codex_stamp(policy)
    return _final(stamp, policy, "unknown" if stamp in ("pending", "unknown") else "accepted")


def reconcile(ledger: Ledger, *, give_up_after: timedelta | None = timedelta(hours=1),
              agent: str | None = None, agent_session_id: str | None = None,
              codex_last_turn: bool = False) -> dict[str, int]:
    """For every pending row (of ``agent`` and/or ``agent_session_id`` when given, each applied on its own),
    re-read its stamp source and fill it. A source that is not there yet (no file yet, a transient read error, the
    result or the turn not written) leaves the row pending; only a source that can never answer (no path, no
    ``tool_use_id``) or answers with no decision stamps it ``unknown`` at once (never allow/deny). A row still
    undecided after ``give_up_after`` (None: never) is stamped ``unknown`` with the real cause as its reason. Each
    transcript is scanned ONCE per pass for all its pending rows. A Codex row with no ``turn_id`` is never given
    "the last turn's" policy; ``codex_last_turn=True`` (a test/CLI option) allows exactly that.

    Returns ``{"filled", "unknown", "still_pending", "already_stamped"}``: ``unknown`` is a subset of ``filled``;
    ``already_stamped`` counts rows another process stamped between the read and the write."""
    out = {"filled": 0, "unknown": 0, "still_pending": 0, "already_stamped": 0}
    now = ledger._now()
    rows = [c for c in ledger.pending() if (agent is None or c.agent == agent)
            and (agent_session_id is None or c.agent_session_id == agent_session_id)]
    by_path: dict[str, list[str]] = {}
    for c in rows:
        if c.agent == "claude_code" and c.source_path and c.tool_use_id:
            by_path.setdefault(c.source_path, []).append(c.tool_use_id)
    scans = {path: _stamps.claude_scan(path, ids) for path, ids in by_path.items()}
    policies: dict[str, Mapping[str, Mapping[str, Any]] | None] = {}
    for c in rows:
        if c.agent == "codex" and c.source_path and c.source_path not in policies \
                and (c.turn_id is not None or codex_last_turn) and os.path.isfile(c.source_path):
            policies[c.source_path] = _stamps.codex_turn_policies(c.source_path)
    for c in rows:
        if c.agent == "claude_code":
            res = _claude_resolution(c, scans)
        else:
            res = _codex_resolution(c, policies, codex_last_turn) if c.agent == "codex" else _wait("no_source")
        if res[0] == "wait":
            if give_up_after is not None and now - parse_when(c.received_at) >= give_up_after:
                res = _final("unknown", {"reason": res[1], "gave_up": True, **(res[2] or {})}, "unknown")
            else:
                out["still_pending"] += 1
                continue
        _, stamp, detail, outcome = res
        if ledger.fill_stamp(c.id, stamp, detail, outcome):
            out["filled"] += 1
            out["unknown"] += stamp == "unknown"
        else:
            out["already_stamped"] += 1
    return out
