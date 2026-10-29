"""The authority ledger (Q15, DESIGN section 3.5): one row per tool call an agent made, and on whose authority.

A store ``<levain home>/consent/ledger.db`` beside ``queue.db`` (same directory checks, WAL through
:func:`levain.autonomic.durable_sqlite.open_durable`, format :data:`STORE_FORMAT`). A row is written by the hooks
(the gate's PreToolUse of a call it answered, PostToolUse, PostToolUseFailure) and carries the call's identity, a
DIGEST of its tool input (never the input, the command text or any file content), and the authority STAMP.

The stamp is read back from the agent's own record (:mod:`levain.consent.stamps`), which is often not written yet
when the hook fires, so a row may be filed ``pending`` and stamped once, later (:meth:`Ledger.fill_stamp`, or
:func:`reconcile` over every pending row). That is the ONLY update a row ever takes: a trigger refuses every other
UPDATE and every DELETE statement, and another refuses an INSERT of an id that exists (so ``INSERT OR REPLACE`` cannot
rewrite a row by id). The file itself is only as safe as its 0600 mode and the uid: the triggers guard the statements
levain writes, not an owner who drops them. Levain's own authority (grant, answer, ruling) is stamped at filing, with the queue
request id that answered the call. A stamp other than levain's is the agent's own record of itself: the transcript,
the rollout and the hook scripts are writable by the agent's uid, so such a stamp is its account, not proof.

A GAP is a window on this ledger (:meth:`Ledger.window`): the rows of one session and its subagents from the
undecided time to the ruling. It lists calls that ran or that the gate denied; a call the agent's own rules or sandbox
refused before it ran fires no hook and has no row. The weekly unseen digest (:meth:`Ledger.digest`) is the rows a
standing grant or a late ruling answered. :meth:`Ledger.counts` counts by agent, stamp and outcome (row D counts
spent-vs-authorized from the same rows, outcome ``accepted`` only). All three read ONE row per call: a call with a
``pre`` row (levain answered it at the gate) is that row. The STAMP says who authorised the call; its OUTCOME there
comes from the post row of the same call (same tool and input digest): ``accepted`` when one exists, even still
pending, since a post event means the call ran; ``refused`` when it says refused; ``unknown`` when there is none. So
this view can show an authorised call that did not run (another hook, a settings rule or the sandbox stopped it after
levain's allow): the write-time CHECK binds stored rows, not this reading of them.

The hooks write through :func:`hook_record`, :func:`hook_fill` and :func:`gate_row`, which catch every ``Exception``
(an interrupt still ends the hook): a ledger that cannot be written is a missing record, never a changed answer or a
blocked agent. They wait at most :data:`HOOK_BUSY_SECONDS` for the store's lock, and a fill stops at its budget
(:data:`HOOK_FILL_BUDGET`): what it leaves pending is stamped by the next hook, Stop or ``reconcile``.

OUTCOME beside STAMP: what the authority did (``accepted`` / ``refused`` / ``unknown``). A refusal authorised nothing,
so a refused row's stamp is only ever ``unknown`` or ``pending`` (a CHECK, not a convention), and ``counts`` splits by
outcome. ``stamp_detail`` is WHITELISTED (:func:`clean_detail`): the decision's ``decision``/``source``/``reasonType``,
a Codex turn's ``approval_policy``/``approvals_reviewer``/sandbox ``type``/profile ``type``, and levain's own short
reasons; anything else a caller or a newer agent version adds is dropped, never stored.

BOUND beside STAMP and OUTCOME: what the vendor's bound refused INSIDE a call that ran (:mod:`levain.consent.bound`).
It informs and never authorises; it moves no stamp and no outcome. Three nullable columns (``bound``,
``bound_denials``, ``bound_classes``) hold a class and a count, never text, and only a post row carries them. NULL
means not recorded: a row filed by a levain before these columns, a pre or denied_by_gate row, or a hook that did not
classify; it is never read as clean. The columns are added IN PLACE to a released store and the format marker stays
the same, so an older levain (without them) still opens the store, writes NULL for them and stamps rows; this levain
reads the rows an older levain wrote after the migration as ``bound=None``. A store of another format, and a
pre-``outcome`` store, are refused as before. Known edges: Codex's one-string output cannot tell stdout from stderr,
and WebFetch outside a slot is outside the vendor sandbox (see the bound module).

Known edges (one line each): the input digest is unsalted, so a SHORT input is recoverable by dictionary; a Codex
policy that changes mid-turn is read from the turn's FIRST ``turn_context``; a call levain allowed that something
after it refused (the Codex sandbox, which fires no PostToolUseFailure) reads as levain's stamp with outcome
``unknown``; rows are placed by when the hook fired, which for a post row is the call's END, so a call still running at
the ruling is not in its window; each hook fill scans its transcript from the start (under the cap), as reconcile
does; ``INSERT OR REPLACE`` by the (agent, session, tool_use_id, phase) tuple, not the id, is not refused by a trigger; a ledger.db made by fedba7b1 (before the ``outcome`` column) is refused, delete it.
A transcript longer than the scan cap ends ``unknown`` with reason ``result_not_written`` for the calls past the cap; an id
merely quoted inside a skipped oversized line marks its row ``result_oversized`` (still ``unknown``, never allow).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback
from levain.cockpit.consent import utc_stamp
from levain.consent import bound as _bound
from levain.consent import stamps as _stamps
from levain.consent.bound import BoundRecord
from levain.consent.queue import QueueStoreError, default_queue_dir
from levain.consent.record import AGENTS, tool_input_digest

__all__ = ["DB_NAME", "DIGEST_STAMPS", "HOOK_BUSY_SECONDS", "HOOK_FILL_BUDGET", "LEDGER_ADDED_COLUMNS", "PHASES",
           "STORE_FORMAT",
           "SUBAGENT_MARK", "WINDOW_SCOPE", "Call", "Ledger", "LedgerStoreError",
           "clean_detail", "gate_row", "hook_fill", "hook_record", "parse_when", "reconcile", "request_outcomes"]

DB_NAME = "ledger.db"
STORE_FORMAT = "levain-authority-ledger/1"
PHASES: tuple[str, ...] = ("pre", "post", "post_failure", "denied_by_gate")
FINAL_STAMPS: tuple[str, ...] = tuple(s for s in _stamps.STAMPS if s != "pending")
# the rows the weekly unseen digest lists: a standing grant or a late ruling answered them, and he was not asked
DIGEST_STAMPS: tuple[str, ...] = ("grant", "ruling")
_ACCESS_BY_OTHERS = 0o077
_BASE_COLUMNS = ("id", "agent", "agent_session_id", "tool_use_id", "received_at", "tool_name", "tool_input_digest",
                 "cwd", "phase", "stamp", "outcome", "stamp_detail", "request_id", "stamped_at", "source_path",
                 "turn_id", "permission_mode")
_BOUND_COLUMNS = ("bound", "bound_denials", "bound_classes")
_COLUMNS = _BASE_COLUMNS + _BOUND_COLUMNS
# the only free text the ledger keeps about a stamp: levain's own reasons, from this list
_REASONS = frozenset({"transcript_missing", "transcript_unreadable", "rollout_missing", "rollout_unreadable",
                      "result_not_written", "result_without_decision", "no_tool_use_id", "no_source", "no_turn_id",
                      "turn_not_written", "turn_policy_unreadable", "result_oversized"})
_DETAIL_STR_KEYS = ("decision", "source", "reasonType", "approval_policy", "approvals_reviewer")
_DETAIL_TYPE_KEYS = ("sandbox_policy", "permission_profile")
_MAX_DETAIL_STR = 64
# a hook waits at most this long for the store's lock: its answer, or its host's timeout, never waits on the record
HOOK_BUSY_SECONDS = 2.0
# the most a hook spends stamping pending rows; the rest are left for the next hook, Stop or reconcile
HOOK_FILL_BUDGET = 3.0
# a Claude Code subagent's rows file under its session's key plus this and the subagent's id
SUBAGENT_MARK = "#agent:"
# what a window lists, printed under one: a row exists only for a call some hook saw
WINDOW_SCOPE = ("calls that ran or that the gate denied; calls the agent's own rules or sandbox refused are not "
                "listed")


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


# (column, declaration): the columns a released calls table gains in place (format unchanged; an older levain writes
# NULL). Order matters: each CHECK may name only columns added before it. NULL = not recorded, never read as clean.
LEDGER_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("bound", f"TEXT CHECK (bound IS NULL OR bound IN ({_sql_list(_bound.BOUNDS)}))"),
    ("bound_denials", "INTEGER CHECK ((bound IS NULL) = (bound_denials IS NULL) AND (bound_denials IS NULL OR "
                      "(typeof(bound_denials) = 'integer' "
                      f"AND bound_denials BETWEEN 0 AND {_bound.MAX_DENIALS} "
                      "AND (bound = 'touched') = (bound_denials > 0))))"),
    ("bound_classes", "TEXT CHECK ((bound IS 'touched') = (bound_classes IS NOT NULL) AND (bound_classes IS NULL OR "
                      "(length(bound_classes) BETWEEN 1 AND 80 AND bound_classes NOT GLOB '*[^a-z_,]*')))"),
)


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
    + ", ".join(f"{col} {decl}" for col, decl in LEDGER_ADDED_COLUMNS) + ", "
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
    "AND NEW.permission_mode IS OLD.permission_mode "
    "AND NEW.bound IS OLD.bound AND NEW.bound_denials IS OLD.bound_denials "
    "AND NEW.bound_classes IS OLD.bound_classes) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row moves only from pending to a final stamp, once'); END",
    "CREATE TRIGGER IF NOT EXISTS calls_kept BEFORE DELETE ON calls "
    "BEGIN SELECT RAISE(ABORT, 'ledger rows are never deleted'); END",
    # INSERT OR REPLACE deletes the old row without firing a delete trigger; refuse a new row that reuses an id
    "CREATE TRIGGER IF NOT EXISTS calls_no_replace BEFORE INSERT ON calls "
    "WHEN EXISTS (SELECT 1 FROM calls WHERE id = NEW.id) "
    "BEGIN SELECT RAISE(ABORT, 'a ledger row id is never reused'); END",
)

# ONE CALL, ONE AUTHORITY: a call levain answered at its gate has a ``pre`` row (levain's own stamp) and, once it ran,
# a ``post`` / ``post_failure`` row (the agent's record, which can only say "a hook allowed it"). The readers that
# count or list calls read this instead of ``calls``: the post row of the SAME call (same tool and input digest, so an
# input another hook rewrote is not covered by the ruling for the original) is dropped and gives the pre row its
# outcome; with no post row the outcome is ``unknown``. Raw rows (``get``, ``pending``) still read ``calls``.
_POST_PHASES = "('post', 'post_failure')"
_SAME_CALL = ("p.agent = c.agent AND p.agent_session_id = c.agent_session_id AND p.tool_use_id = c.tool_use_id "
              "AND p.tool_name = c.tool_name AND p.tool_input_digest = c.tool_input_digest")
_POST_OUTCOME = (f"CASE WHEN c.phase = 'pre' THEN COALESCE((SELECT CASE WHEN p.outcome = 'refused' THEN 'refused' "
                 f"ELSE 'accepted' END FROM calls p WHERE {_SAME_CALL} AND p.phase IN {_POST_PHASES} "
                 "ORDER BY p.rowid LIMIT 1), 'unknown') ELSE c.outcome END AS outcome")
_PER_CALL_WHERE = (f", c.rowid AS rowid FROM calls c WHERE NOT (c.phase IN {_POST_PHASES} AND c.tool_use_id IS NOT NULL "
                   f"AND EXISTS (SELECT 1 FROM calls p WHERE {_SAME_CALL} AND p.phase = 'pre'))) AS per_call")
# the view the read-only readers import (request_outcomes, the listing page): it opens a store WITHOUT migrating it,
# so it must never name a bound column an unmigrated store lacks
_PER_CALL = (
    "(SELECT " + ", ".join(_POST_OUTCOME if col == "outcome" else f"c.{col} AS {col}" for col in _BASE_COLUMNS)
    + _PER_CALL_WHERE
)


def _lift_bound(col: str) -> str:
    # a gated call's per-call row is its pre row, so the bound its post row recorded is lifted onto it, as outcome is
    return (f"CASE WHEN c.phase = 'pre' THEN (SELECT p.{col} FROM calls p WHERE {_SAME_CALL} "
            f"AND p.phase IN {_POST_PHASES} ORDER BY p.rowid LIMIT 1) ELSE c.{col} END AS {col}")


# the same view over every column, for the Ledger itself (it always migrates before it reads)
_PER_CALL_FULL = (
    "(SELECT " + ", ".join(_POST_OUTCOME if col == "outcome" else _lift_bound(col) if col in _BOUND_COLUMNS
                           else f"c.{col} AS {col}" for col in _COLUMNS)
    + _PER_CALL_WHERE
)


@lru_cache(maxsize=1)
def _frozen_trigger_text() -> str:
    """The current ``calls_frozen`` text as SQLite stores it (read back, never hand-normalised)."""
    scratch = sqlite3.connect(":memory:")
    try:
        for stmt in _SCHEMA:
            scratch.execute(stmt)
        return scratch.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                               "AND name = 'calls_frozen'").fetchone()[0]
    finally:
        scratch.close()


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
    bound: str | None = None
    bound_denials: int | None = None
    bound_classes: tuple[str, ...] | None = None

    @staticmethod
    def _of(row: sqlite3.Row | tuple[Any, ...]) -> Call:
        values = dict(zip(_COLUMNS, row))
        detail = values["stamp_detail"]
        values["stamp_detail"] = json.loads(detail) if detail else None
        classes = values["bound_classes"]
        values["bound_classes"] = tuple(classes.split(",")) if classes else None
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
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 busy_seconds: float | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_queue_dir()
        self.path = self.directory / DB_NAME
        self._clock = clock
        self._ready = False
        # how long a statement waits for another writer's lock (None: the durable store's own default)
        self._busy_ms = None if busy_seconds is None else max(0, int(busy_seconds * 1000))

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
            if self._busy_ms is not None:
                conn.execute(f"PRAGMA busy_timeout = {self._busy_ms}")
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
            if "calls" in tables:
                have = {r[1] for r in conn.execute("PRAGMA table_info(calls)")}
                for column, decl in LEDGER_ADDED_COLUMNS:
                    if column not in have:
                        conn.execute(f"ALTER TABLE calls ADD COLUMN {column} {decl}")
                # the trigger must name the new columns: replace a stored one whose text is not the current text
                stored = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                                      "AND name = 'calls_frozen'").fetchone()
                if stored is not None and stored[0] != _frozen_trigger_text():
                    conn.execute("DROP TRIGGER calls_frozen")
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
                    outcome: str | None = None, bound: BoundRecord | None = None) -> str:
        """File one call and return its row id. Only the DIGEST of ``tool_input`` is kept, and ``stamp_detail`` is
        whitelisted (:func:`clean_detail`). Idempotent per ``(agent, agent_session_id, tool_use_id, phase)``: filing
        the same call again returns the first row and changes nothing (a call without a ``tool_use_id`` is never
        matched, so it is always a new row). Every NOT NULL field is validated up front (``ValueError``); a store
        failure other than that one dedup conflict raises, so a call is never silently dropped.

        ``stamp`` defaults to ``pending`` (read ``source_path`` later); a final stamp (levain's own grant, answer or
        ruling, with the answering ``request_id``) is stamped now. ``outcome`` defaults from the phase and stamp
        (``denied_by_gate`` -> refused; pending or unknown -> unknown; else accepted). ``bound`` (a
        :class:`~levain.consent.bound.BoundRecord`) is what the vendor's bound refused inside the call, and belongs
        only on a post row."""
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
        if bound is not None:
            if not isinstance(bound, BoundRecord):
                raise ValueError(f"bound must be a BoundRecord (got {type(bound).__name__})")
            if phase not in ("post", "post_failure"):
                raise ValueError("a bound is recorded only on a post row")
        if outcome is None:
            outcome = ("refused" if phase == "denied_by_gate" and stamp in ("pending", "unknown")
                       else "unknown" if stamp in ("pending", "unknown") else "accepted")
        _check_outcome(stamp, outcome)
        now = self._now()
        row_id = "lc-" + os.urandom(12).hex()
        values = (row_id, agent, agent_session_id, tool_use_id, _stamp_of(received_at, now, now=now), tool_name,
                  tool_input_digest(tool_input), cwd, phase, stamp, outcome, _dump(stamp_detail), request_id,
                  None if stamp == "pending" else utc_stamp(now), source_path, turn_id, permission_mode,
                  *((bound.bound, bound.denials, bound.classes_text()) if bound is not None else (None, None, None)))
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
                # the first filing's bound stands, and a later one is not attached (the trigger pins it). Unlike the
                # stamp, a bound has no later source: it is read from the same payload the row is filed from, so a
                # retry of that payload carries the same evidence. Known edge: a row filed by a hook that does not
                # classify (an older levain, a stale hook script) keeps NULL ("not recorded"), never clean.
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

    def _select(self, where: str, args: tuple[Any, ...], *, per_call: bool = False) -> list[Call]:
        source = _PER_CALL_FULL if per_call else "calls"
        with self._read() as conn:
            rows = conn.execute(f"SELECT {','.join(_COLUMNS)} FROM {source} WHERE {where} "
                                "ORDER BY received_at, rowid", args).fetchall()
        return [Call._of(r) for r in rows]

    def pending_for(self, agent: str, agent_session_id: str, *, subagents: bool = False) -> list[Call]:
        """The session's rows still waiting for their stamp, oldest first; with ``subagents``, its subagents' rows
        too (keys ``<session>#agent:<id>``)."""
        where, args = _session_clause(agent_session_id, subagents)
        return self._select(f"agent = ? AND {where} AND stamp = 'pending'", (agent, *args))

    def pending(self) -> list[Call]:
        """Every row still waiting for its stamp, oldest first."""
        return self._select("stamp = 'pending'", ())

    def window(self, agent: str, agent_session_id: str, since: str | datetime, until: str | datetime | None = None
               ) -> list[Call]:
        """The rows of the session's family with ``since <= received_at <= until`` (``until`` None = now): a gap is
        the window from its undecided time to its ruling. The family is the root session and all its subagents
        (``<root>#agent:<id>``), whichever of them ``agent_session_id`` names; each row's own key says which it is.
        Only calls that ran or that the gate denied have rows (the module docstring)."""
        now = self._now()
        lo, hi = _need(since, now), _stamp_of(until, now, now=now)
        where, args = _session_clause(agent_session_id.split(SUBAGENT_MARK, 1)[0], True)
        return self._select(f"agent = ? AND {where} AND received_at >= ? AND received_at <= ?",
                            (agent, *args, lo, hi), per_call=True)

    def digest(self, since: str | datetime, until: str | datetime | None = None) -> list[Call]:
        """The unseen digest: rows in the window a standing grant or a late ruling answered (a signed answer was
        seen by the person who signed it)."""
        now = self._now()
        lo, hi = _need(since, now), _stamp_of(until, now, now=now)
        marks = ",".join("?" * len(DIGEST_STAMPS))
        return self._select(f"received_at >= ? AND received_at <= ? AND stamp IN ({marks})", (lo, hi, *DIGEST_STAMPS),
                            per_call=True)

    def counts(self, since: str | datetime | None = None, until: str | datetime | None = None
               ) -> list[tuple[str, str, str, int]]:
        """``(agent, stamp, outcome, calls)`` over the window, sorted, one per call (``window``'s rule). No
        ``since`` counts from the start."""
        now = self._now()
        lo = _stamp_of(since, now=now) if since is not None else ""
        hi = _stamp_of(until, now, now=now)
        with self._read() as conn:
            rows = conn.execute(f"SELECT agent, stamp, outcome, COUNT(*) FROM {_PER_CALL} WHERE received_at >= ? "
                                "AND received_at <= ? GROUP BY agent, stamp, outcome ORDER BY agent, stamp, outcome",
                                (lo, hi)).fetchall()
        return [(a, s, o, n) for a, s, o, n in rows]


def _session_clause(session: str, subagents: bool) -> tuple[str, tuple[Any, ...]]:
    """SQL for rows of ``session``, and with ``subagents`` of every ``<session>#agent:<id>`` key (a prefix test,
    not GLOB or LIKE, so no character of a session id is a pattern)."""
    if not subagents:
        return "agent_session_id = ?", (session,)
    prefix = session + SUBAGENT_MARK
    return "(agent_session_id = ? OR substr(agent_session_id, 1, ?) = ?)", (session, len(prefix), prefix)


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


def request_outcomes(directory: Path | str, request_ids: "list[str]") -> dict[str, str]:
    """``{queue request id: outcome}`` for the calls levain answered at the gate with those requests (their ``pre``
    rows, one per call, ``window``'s rule): what each authorised call then did. A request with no row is absent.
    A true read: the ledger in ``directory`` is opened read-only (no schema, no permission change, no lock taken
    for writing), and none there is no outcome at all. The read waits a short, bounded time for a writer; a ledger
    still locked after that answers ``{}`` (an outcome is decoration, so a hook holding the write lock costs the
    caller the outcomes, never the read). A ledger of another format raises :class:`LedgerStoreError`."""
    path = Path(directory) / DB_NAME
    ids = sorted(set(request_ids))
    if not ids or not path.is_file():
        return {}
    # a short busy wait, then give up: a page's read does not hang on a hook holding the database
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=0.25)
    try:
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        fmt = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() if "meta" in tables else None
        if fmt is None or fmt[0] != STORE_FORMAT or "calls" not in tables or "outcome" not in {
                r[1] for r in conn.execute("PRAGMA table_info(calls)")}:
            raise LedgerStoreError(f"store format {None if fmt is None else fmt[0]!r} is not {STORE_FORMAT!r}")
        out: dict[str, str] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            out.update(conn.execute(
                f"SELECT request_id, outcome FROM {_PER_CALL} WHERE phase = 'pre' AND request_id IN "
                f"({', '.join('?' for _ in chunk)})", chunk).fetchall())
        conn.execute("COMMIT")
        return out
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return {}
        raise
    finally:
        conn.close()


def reconcile(ledger: Ledger, *, give_up_after: timedelta | None = timedelta(hours=1),
              agent: str | None = None, agent_session_id: str | None = None, subagents: bool = False,
              codex_last_turn: bool = False, deadline: float | None = None) -> dict[str, int]:
    """For every pending row (of ``agent`` and/or ``agent_session_id`` when given, each applied on its own; with
    ``subagents``, the session's subagents' rows too),
    re-read its stamp source and fill it. A source that is not there yet (no file yet, a transient read error, the
    result or the turn not written) leaves the row pending; only a source that can never answer (no path, no
    ``tool_use_id``) or answers with no decision stamps it ``unknown`` at once (never allow/deny). A row still
    undecided after ``give_up_after`` (None: never) is stamped ``unknown`` with the real cause as its reason. Each
    transcript is scanned ONCE per pass for all its pending rows. A Codex row with no ``turn_id`` is never given
    "the last turn's" policy; ``codex_last_turn=True`` (a test/CLI option) allows exactly that. Past ``deadline``
    (a :func:`time.monotonic` value) no further row is stamped: those count as still pending.

    Returns ``{"filled", "unknown", "still_pending", "already_stamped"}``: ``unknown`` is a subset of ``filled``;
    ``already_stamped`` counts rows another process stamped between the read and the write."""
    out = {"filled": 0, "unknown": 0, "still_pending": 0, "already_stamped": 0}
    now = ledger._now()
    if agent is not None and agent_session_id is not None:
        rows = ledger.pending_for(agent, agent_session_id, subagents=subagents)
    else:
        family = agent_session_id + SUBAGENT_MARK if agent_session_id is not None and subagents else None
        rows = [c for c in ledger.pending() if (agent is None or c.agent == agent)
                and (agent_session_id is None or c.agent_session_id == agent_session_id
                     or (family is not None and c.agent_session_id.startswith(family)))]
    by_path: dict[str, list[str]] = {}
    for c in rows:
        if c.agent == "claude_code" and c.source_path and c.tool_use_id:
            by_path.setdefault(c.source_path, []).append(c.tool_use_id)
    # each source is read on first need, after the deadline check, so a family of many transcripts stops at its budget
    scans: dict[str, Any] = {}
    policies: dict[str, Mapping[str, Mapping[str, Any]] | None] = {}
    for i, c in enumerate(rows):
        if deadline is not None and time.monotonic() >= deadline:
            out["still_pending"] += len(rows) - i
            break
        if c.agent == "claude_code" and c.source_path in by_path and c.source_path not in scans:
            scans[c.source_path] = _stamps.claude_scan(c.source_path, by_path[c.source_path])
        elif c.agent == "codex" and c.source_path and c.source_path not in policies \
                and (c.turn_id is not None or codex_last_turn) and os.path.isfile(c.source_path):
            policies[c.source_path] = _stamps.codex_turn_policies(c.source_path)
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
        if c.outcome == "refused":                  # filed refused: reconcile may fill the stamp (unknown), never the outcome
            stamp, outcome = "unknown", "refused"
        if ledger.fill_stamp(c.id, stamp, detail, outcome):
            out["filled"] += 1
            out["unknown"] += stamp == "unknown"
        else:
            out["already_stamped"] += 1
    return out


# --- the hooks' writers ------------------------------------------------------------------------------------------
# Called from inside the agents' hooks (the module docstring: Exception caught, the lock wait and the fill bounded).

_TOOL_PHASES = {"PostToolUse": "post", "PostToolUseFailure": "post_failure"}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _session_key(agent: str, payload: Mapping[str, Any]) -> str | None:
    """The session a row files under: the gate's own key, so a call's pre and post rows meet. Claude Code appends
    ``#agent:<agent_id>`` for a subagent (its adapter's rule); the Codex gate keys by ``session_id`` alone."""
    session = _text(payload.get("session_id"))
    agent_id = _text(payload.get("agent_id"))
    if session is not None and agent == "claude_code" and agent_id is not None:
        return f"{session}{SUBAGENT_MARK}{agent_id}"
    return session


def _hook_failed(what: str, exc: BaseException) -> None:
    try:
        print(f"levain ledger: {what} ({type(exc).__name__}: {exc})"[:300], file=sys.stderr)
    except Exception:
        pass


def _hook_ledger(queue_dir: Path | str | None) -> Ledger | None:
    """The ledger in the consent directory, only when that directory already exists: a hook never creates
    levain's home."""
    directory = Path(queue_dir) if queue_dir is not None else default_queue_dir()
    return Ledger(directory, busy_seconds=HOOK_BUSY_SECONDS) if directory.is_dir() else None


_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)


def _source_path(agent: str, payload: Mapping[str, Any]) -> str | None:
    """Where the call's stamp is read. A Claude Code subagent's hook events carry its PARENT's ``transcript_path``
    while its own tool results go to ``<parent transcript minus .jsonl>/subagents/agent-<agent_id>.jsonl`` (measured
    on Claude Code 2.1.302, B3h fix r1), so a subagent row reads that file; a path already under ``subagents/`` is
    kept as given."""
    path = _text(payload.get("transcript_path"))
    agent_id = _text(payload.get("agent_id"))
    if path is None or agent != "claude_code" or agent_id is None or not _AGENT_ID.fullmatch(agent_id):
        return path
    given = Path(path)
    if given.parent.name == "subagents" or given.suffix != ".jsonl":
        return path
    return str(given.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl")


def _common(agent: str, payload: Mapping[str, Any], session: str, tool_name: str, tool_input: Any,
            cwd: str) -> dict[str, Any]:
    return {"agent": agent, "agent_session_id": session, "tool_name": tool_name, "tool_input": tool_input, "cwd": cwd,
            "tool_use_id": _text(payload.get("tool_use_id")), "source_path": _source_path(agent, payload),
            # a Codex row reads its turn's policy; Claude Code has no turns in its record
            "turn_id": _text(payload.get("turn_id")) if agent == "codex" else None,
            "permission_mode": _text(payload.get("permission_mode"))}


def hook_record(agent: str, payload: Any, *, queue_dir: Path | str | None = None) -> str | None:
    """File the call a ``PostToolUse`` / ``PostToolUseFailure`` payload reports, ``pending`` (phase ``post`` /
    ``post_failure``), and return its row id. Its stamp is read later from the agent's record (:func:`hook_fill`).
    None when the payload is not such an event, lacks a session, tool or cwd, when the consent directory does not
    exist, or on any failure."""
    try:
        if agent not in AGENTS or not isinstance(payload, Mapping):
            return None
        phase = _TOOL_PHASES.get(payload.get("hook_event_name"))  # type: ignore[arg-type]
        session, name, cwd = _session_key(agent, payload), _text(payload.get("tool_name")), _text(payload.get("cwd"))
        if phase is None or session is None or name is None or cwd is None:
            return None
        ledger = _hook_ledger(queue_dir)
        if ledger is None:
            return None
        return ledger.record_call(**_common(agent, payload, session, name, payload.get("tool_input"), cwd),
                                  phase=phase)
    except Exception as exc:  # noqa: BLE001 - the ledger is a record, never a gate
        _hook_failed("row not written", exc)
        return None


def hook_fill(agent: str, payload: Any, *, queue_dir: Path | str | None = None,
              budget: float = HOOK_FILL_BUDGET) -> dict[str, int] | None:
    """Stamp this session's pending rows from the agent's own record (:func:`reconcile` scoped to the payload's
    session; each transcript or rollout is read once, under the readers' byte caps), for at most ``budget`` seconds.
    A ``Stop`` stamps the session's subagents' rows too: no later hook of a subagent may come. A row whose source
    still says nothing stays pending until :func:`reconcile`'s default give-up age, then ``unknown``, never allow or
    deny. None when there is no ledger yet, no session, or on any failure."""
    try:
        if agent not in AGENTS or not isinstance(payload, Mapping):
            return None
        session = _session_key(agent, payload)
        ledger = _hook_ledger(queue_dir)
        if session is None or ledger is None or not ledger.path.is_file():
            return None
        family = payload.get("hook_event_name") == "Stop" and SUBAGENT_MARK not in session
        return reconcile(ledger, agent=agent, agent_session_id=session, subagents=family,
                         deadline=time.monotonic() + budget)
    except Exception as exc:  # noqa: BLE001 - the ledger is a record, never a gate
        _hook_failed("rows not stamped", exc)
        return None


def gate_row(agent: str, queue_dir: Path | str, event: Mapping[str, Any], session: str, tool_name: str,
             tool_input: Any, cwd: str, *, decision: Any = None, refused: str | None = None,
             request_id: str | None = None) -> str | None:
    """File a gated call's row at decision time, from the gate's own answer: a verified allow is phase ``pre``,
    stamped with its authority (``answer`` / ``grant`` / ``ruling``, the answering request; a contained-shell
    grant's allow is ``grant``); anything the gate
    denied is ``denied_by_gate`` (stamp ``unknown``, outcome ``refused``), its ``decision`` saying how (``deny``,
    ``undecided``, ``halted``, ``failed``, or ``floor`` for the T3 floor, where no enrolled key could approve it).
    Every row carries the event's ``permission_mode`` (:func:`_common`). ``session`` is the gate's own key. None on
    any failure: the queue's receipts still record every decision."""
    try:
        common = _common(agent, event, session, tool_name, tool_input, cwd)
        ledger = Ledger(queue_dir, busy_seconds=HOOK_BUSY_SECONDS)
        if decision is not None and getattr(decision, "decision", None) == "allow":
            via = getattr(decision, "via", None)
            # a contained-shell grant is a standing grant: its allow is no operator's answer
            stamp = "grant" if via in ("grant", "contained_shell") else "ruling" if via == "ruling" else "answer"
            return ledger.record_call(**common, phase="pre", stamp=stamp,
                                      request_id=getattr(decision, "request_id", None),
                                      stamp_detail={"decision": "allow", "source": "levain_gate"})
        how = "deny" if decision is not None else (refused or "deny")
        return ledger.record_call(**common, phase="denied_by_gate", stamp="unknown", outcome="refused",
                                  request_id=getattr(decision, "request_id", None) if decision is not None
                                  else request_id,
                                  stamp_detail={"decision": how, "source": "levain_gate"})
    except Exception as exc:  # noqa: BLE001 - the ledger is a record, never the gate
        _hook_failed("gate row not written", exc)
        return None
