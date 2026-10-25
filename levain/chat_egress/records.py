"""The egress records (CH-D2 S5a-2): `chat_egress_events`, the audit record of what a chat launch sent out.

Design §3.8.5(o) R-10 and *At close*, as amended through R15-30 (rows R15-12-B5, R15-14-4, R15-17-9, R15-26-3,
R15-28-5, R15-29-1, R15-29-3, R15-10-A8). Row classification: `project_memory/1024-20/chd2_s5a/ROWS.md`.

Every request the DECIDER is asked about has its `egress_attempt` row (a glue wall never reaches the decider and has
none) plus at most one END row: `egress_outcome` (`onResponse` fired) or `egress_ended_before_outcome` (the abort
report came first; it names no cause). `lifecycle` decides which, in one transition; this module writes what it asks
for, and the table refuses every other shape written through this module or by any writer with foreign keys on
(`gseq` provenance, from the store's one sequence, is this module's; no trigger checks it):
  * UNIQUE (launch_id, request_id, kind). `request_id` is nullable: the `audit-missing` deny has no id, and SQLite
    treats NULLs as distinct, so two of those are both stored.
  * a second row of a kind for one request, or a second END row of either kind, is REFUSED: never stored, counted in
    the launch's `duplicate_rows` (K1 reads it, R15-29-3) and logged once per launch, at its first occurrence
    (R15-28-5). The outcome-vs-abort race never gets here: C0 absorbs the loser. A second ATTEMPT whose decision
    differs from the stored one is not a duplicate: it raises (the act would disagree with the record). A second
    FAILED mark naming the same effect is a replay: False, not counted, not logged.
  * an outcome joins an ALLOW attempt; an ended row joins an ALLOW or UNDECIDED one (never a deny); `ended` is
    `unknown`, always, in 1.0 (R15-17-9); the row has no transferred-byte field and no raw response-header value
    (R15-12-B5, R15-21-3): `write_outcome` stores the observation's status, its `declared_length` and the decider's
    `content_type_class` enum (one of `CONTENT_TYPE_CLASSES`, the classifier's `advert.CT_*` values), never the
    raw Content-Type or Content-Length.
  * append-only, with the repo's `_frozen` / `_kept` / `_no_replace` triggers, and `gseq` from the dispatch store's one
    sequence.
  * the attempt row is committed (`BEGIN IMMEDIATE`, `synchronous = FULL`) before `write_attempt` returns, and the
    composer sends the allow only after it returns (lifecycle orders WRITE_ATTEMPT before SEND_ALLOW): so a service
    killed between the allow and the outcome leaves an ALLOW attempt with no end row, which `settled` reads
    AMBIGUOUS (R15-10-A8).

FAILED (the head's rulings on S5a-1's L3 F1, 1024+20): `egress_failed` is a fourth row kind, the durable FAILED mark,
at most one per request and beside whatever rows landed. `record_failure` writes it when lifecycle's `Failure` names
a NON-database effect (the card's end, a verdict send, the release) and the store is still writable. When the effect
that raised is one of this module's own row writes, NOTHING more is written: after a restart the rows that landed
read as they are (an ALLOW attempt alone reads AMBIGUOUS; no attempt reads as no row), the honest reading when the
store itself failed. `settled` hands the mark's effect to `lifecycle.settle(..., failed_effect=)`, where a stored end
row beats the mark; the mark stays on its row for the card. This module keeps no reading set.

ERRORS: a refused duplicate RETURNS False (it is counted, except the same FAILED mark again; it is not an effect
failure). Input C0 never produces (a bad verdict or enum, a raw query in the path, an end row with no attempt to
join, a second attempt with a different decision) raises `EgressRecordError`. A store failure (disk, lock timeout)
raises sqlite3's own error. Either raise is an effect failure for lifecycle.

SCHEMA CHECK: `CREATE ... IF NOT EXISTS` keeps whatever table or trigger body a file already holds. So the first
open compares every `chat_egress_events` object in `sqlite_master` (table, triggers, indexes) with the ones
`EGRESS_SCHEMA` makes, and refuses the store with `EgressRecordError` on any difference: a store made by another
revision is refused at open, never half-enforced.

The schema lives here, not in `CHAT_SCHEMA` (plan ruling P-7): `CHAT_SCHEMA` (`levain/coordinator/chat.py`) is on the
unmerged CH-D1a branch, not on this base. `EGRESS_SCHEMA` is one tuple so it folds into it whole.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction, write_transaction
from levain.chat_egress import wire
from levain.chat_egress.advert import CT_ABSENT, CT_ADVERTISEMENT, CT_OTHER
from levain.chat_egress.lifecycle import Effect, Failure, Settled, Verdict, settle
from levain.contracts.canon import canonical
from levain.coordinator.store import _append_only, _in, next_gseq

__all__ = ["ATTEMPT", "Attach", "Attempt", "CONTENT_TYPE_CLASSES", "CT_ABSENT", "CT_ADVERTISEMENT", "CT_OTHER",
           "EGRESS_SCHEMA", "ENDED", "EgressRecordError", "EgressRecords", "EgressRow", "FAILED", "KINDS", "OUTCOME"]

log = logging.getLogger(__name__)

ATTEMPT = "egress_attempt"
OUTCOME = "egress_outcome"
ENDED = "egress_ended_before_outcome"
FAILED = "egress_failed"
KINDS = (ATTEMPT, OUTCOME, ENDED, FAILED)
_ENDS = (OUTCOME, ENDED)
#: this module's own effects: a failure of one of them writes nothing more
_WRITE_EFFECTS = frozenset({Effect.WRITE_ATTEMPT, Effect.WRITE_OUTCOME, Effect.WRITE_ENDED})
#: the effects whose failure the FAILED mark records. Derived, so a new lifecycle effect is markable in a fresh store.
#: A store created before it holds the old CHECK; the schema check refuses that store at open, so the stale CHECK is
#: never met as an IntegrityError inside the failure handler.
_MARKED_EFFECTS = tuple(e for e in Effect if e not in _WRITE_EFFECTS)
#: the one value `ended` takes in 1.0 (R15-17-9): the adopted surface has no body-end signal
ENDED_UNKNOWN = "unknown"
#: R15-21-3: the only thing stored about a response's Content-Type, as the decider classifies it (S3b's
#: "content_type_class"; the CT_* strings are defined once, in advert, and re-exported here). The raw value never
#: reaches this module's rows.
CONTENT_TYPE_CLASSES = (CT_ADVERTISEMENT, CT_OTHER, CT_ABSENT)


class EgressRecordError(ValueError):
    """A row C0 never asks for. Raised before anything is written."""


class Attach(StrEnum):
    """R-10's `attach` enum (R15-26-2). Which value applies when two do is S6's (R15-28-3's precedence)."""
    ATTACHED = "attached"
    NONE = "none"
    REVOKED = "revoked"
    LIVENESS_UNREADABLE = "liveness-unreadable"
    NOT_EVALUATED = "not-evaluated"


@dataclass(frozen=True, slots=True)
class Attempt:
    """The decision-time fields of an `egress_attempt` row, as the decider made them. `path` is the decider's path
    SHAPE (keyed host) or redacted path (unkeyed), never a raw path and never a query; `names` are the NAMES R-5
    removed or matched, as logged. `setup_record_id` is set exactly when `attach` is ATTACHED. Never a credential."""
    host: str
    method: str
    path: str
    names: Sequence[str]
    setup_record_id: str | None
    attach: Attach
    deciding_step: str
    open_network: bool


@dataclass(frozen=True, slots=True)
class EgressRow:
    gseq: int
    launch_id: str
    request_id: str | None
    kind: str
    verdict: Verdict | None
    deciding_step: str | None
    host: str | None
    method: str | None
    path: str | None
    names: str | None
    setup_record_id: str | None
    attach: Attach | None
    open_network: bool | None
    status: int | None
    declared_length: int | None
    ended: str | None
    content_type_class: str | None
    failed_effect: str | None
    at: str


_ATTEMPT_COLS = ("verdict", "deciding_step", "host", "method", "path", "names", "attach", "open_network")
_ALL_ATTEMPT = " AND ".join(f"{c} IS NOT NULL" for c in _ATTEMPT_COLS)
_NO_ATTEMPT = " AND ".join(f"{c} IS NULL" for c in (*_ATTEMPT_COLS, "setup_record_id"))
_SAME_REQUEST = "launch_id = NEW.launch_id AND request_id = NEW.request_id"
_KEY = f"{_SAME_REQUEST} AND kind = NEW.kind"

EGRESS_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS chat_egress_events (gseq INTEGER NOT NULL UNIQUE, "
    "launch_id TEXT NOT NULL REFERENCES launches(launch_id), request_id TEXT, "
    f"kind TEXT NOT NULL CHECK ({_in('kind', KINDS)}), "
    # egress_attempt: the decision, committed before an allow is sent
    f"verdict TEXT CHECK ({_in('verdict', [v.value for v in Verdict])}), deciding_step TEXT, "
    "host TEXT, method TEXT, path TEXT CHECK (instr(path, '?') = 0), names TEXT, setup_record_id TEXT, "
    f"attach TEXT CHECK ({_in('attach', [a.value for a in Attach])}), "
    "open_network INTEGER CHECK (open_network IN (0, 1)), "
    # egress_outcome: what onResponse gives (status, the parsed Content-Length), never a byte count
    "status INTEGER CHECK (status BETWEEN 100 AND 599), declared_length INTEGER CHECK (declared_length >= 0), "
    f"ended TEXT CHECK (ended = '{ENDED_UNKNOWN}'), "
    f"content_type_class TEXT CHECK ({_in('content_type_class', CONTENT_TYPE_CLASSES)}), "
    # egress_failed: which non-database effect raised (S5a-1 L3 F1); never a row write's own failure
    f"failed_effect TEXT CHECK ({_in('failed_effect', [e.value for e in _MARKED_EFFECTS])}), "
    "at TEXT NOT NULL, "
    "UNIQUE (launch_id, request_id, kind), "
    f"CHECK ((kind = '{ATTEMPT}') = ({_ALL_ATTEMPT})), "
    f"CHECK (kind = '{ATTEMPT}' OR ({_NO_ATTEMPT})), "
    "CHECK ((setup_record_id IS NOT NULL) = (attach = 'attached')), "
    f"CHECK ((kind = '{OUTCOME}') = (status IS NOT NULL AND ended IS NOT NULL AND content_type_class IS NOT NULL)), "
    f"CHECK (kind = '{OUTCOME}' OR (status IS NULL AND declared_length IS NULL AND ended IS NULL "
    "AND content_type_class IS NULL)), "
    f"CHECK ((kind = '{FAILED}') = (failed_effect IS NOT NULL)), "
    # a null id is the decider's audit-missing deny, and nothing else (R15-25-1)
    f"CHECK (request_id IS NOT NULL OR (kind = '{ATTEMPT}' AND verdict = 'deny' AND attach = 'not-evaluated' "
    "AND deciding_step = 'audit-missing')))",
    *_append_only("chat_egress_events", "an egress record"),
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    "CREATE TRIGGER IF NOT EXISTS chat_egress_events_no_replace BEFORE INSERT ON chat_egress_events WHEN EXISTS "
    f"(SELECT 1 FROM chat_egress_events WHERE gseq = NEW.gseq OR rowid = NEW.rowid OR ({_KEY})) "
    "BEGIN SELECT RAISE(ABORT, 'an egress record is never replaced'); END",
    # at most one END row per request: outcome XOR ended-before-outcome (the FAILED mark is not an end row)
    f"CREATE TRIGGER IF NOT EXISTS chat_egress_events_one_end BEFORE INSERT ON chat_egress_events WHEN NEW.kind IN "
    f"({', '.join(repr(k) for k in _ENDS)}) AND EXISTS (SELECT 1 FROM chat_egress_events WHERE {_SAME_REQUEST} "
    f"AND kind IN ({', '.join(repr(k) for k in _ENDS)})) "
    "BEGIN SELECT RAISE(ABORT, 'a request has at most one end row'); END",
    # an end row joins its own attempt: an outcome an allow, an ended row an allow or a pending (undecided) one
    "CREATE TRIGGER IF NOT EXISTS chat_egress_events_joined BEFORE INSERT ON chat_egress_events WHEN "
    f"(NEW.kind = '{OUTCOME}' AND NOT EXISTS (SELECT 1 FROM chat_egress_events WHERE {_SAME_REQUEST} "
    f"AND kind = '{ATTEMPT}' AND verdict = 'allow')) OR (NEW.kind = '{ENDED}' AND NOT EXISTS (SELECT 1 FROM "
    f"chat_egress_events WHERE {_SAME_REQUEST} AND kind = '{ATTEMPT}' AND verdict IN ('allow', 'undecided'))) "
    "BEGIN SELECT RAISE(ABORT, 'an end row joins an attempt that was allowed or still pending'); END",
)

_COLUMNS = ("gseq", "launch_id", "request_id", "kind", *_ATTEMPT_COLS[:6], "setup_record_id", "attach",
            "open_network", "status", "declared_length", "ended", "content_type_class", "failed_effect", "at")


def _row(values: tuple[Any, ...]) -> EgressRow:
    # positional, in _COLUMNS order (a stored row, never a frame: test_one_parse's detector reads `["path"]` as one)
    (gseq, launch_id, request_id, kind, verdict, deciding_step, host, method, path, names, setup_record_id, attach,
     open_network, status, declared_length, ended, content_type_class, failed_effect, at) = values
    return EgressRow(gseq, launch_id, request_id, kind, None if verdict is None else Verdict(verdict), deciding_step,
                     host, method, path, names, setup_record_id, None if attach is None else Attach(attach),
                     None if open_network is None else bool(open_network), status, declared_length, ended,
                     content_type_class, failed_effect, at)


def _check_request_id(request_id: object) -> str:
    if not wire.is_request_id(request_id):
        raise EgressRecordError("an egress row's request_id is the wrapper's request id")
    return request_id  # type: ignore[return-value]


def _check_attempt(request_id: str | None, verdict: Verdict, a: Attempt) -> tuple[Any, ...]:
    if type(verdict) is not Verdict:
        raise EgressRecordError(f"not a lifecycle verdict: {verdict!r}")
    if type(a) is not Attempt or type(a.attach) is not Attach:
        raise EgressRecordError("an attempt row takes an Attempt with an Attach value")
    for name in ("host", "method", "path", "deciding_step"):
        if not isinstance(getattr(a, name), str) or not getattr(a, name):
            raise EgressRecordError(f"an attempt row's {name} is a non-empty string")
    if "?" in a.path:
        raise EgressRecordError("an attempt row's path never carries a query (R15-17-8)")
    if isinstance(a.names, str) or not all(isinstance(n, str) for n in a.names):
        raise EgressRecordError("an attempt row's names are a sequence of strings")
    if type(a.open_network) is not bool:
        raise EgressRecordError("an attempt row's open_network is a bool")
    if (a.setup_record_id is not None) != (a.attach is Attach.ATTACHED):
        raise EgressRecordError("an attempt names a setup record exactly when a key was attached")
    if request_id is None:
        if not (verdict is Verdict.DENY and a.attach is Attach.NOT_EVALUATED and a.deciding_step == "audit-missing"):
            raise EgressRecordError("a null request_id is the audit-missing deny only (R15-25-1)")
    else:
        _check_request_id(request_id)
    return (verdict.value, a.deciding_step, a.host, a.method, a.path, canonical(list(a.names)), a.setup_record_id,
            a.attach.value, int(a.open_network))


_OBJECTS_SQL = ("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'chat_egress_events' "
                "ORDER BY type, name")


def _schema_objects(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """Every `sqlite_master` object of the table (the table, its triggers, its indexes, autoindexes included)."""
    return [tuple(r) for r in conn.execute(_OBJECTS_SQL)]  # type: ignore[misc]


_EXPECTED: list[tuple[str, str, str | None]] | None = None


def _expected_objects() -> list[tuple[str, str, str | None]]:
    """The objects `EGRESS_SCHEMA` makes, read back from a scratch in-memory database built by this same SQLite, so
    the comparison is of SQLite's own stored text, never a hand normalisation of it."""
    global _EXPECTED
    if _EXPECTED is None:
        scratch = sqlite3.connect(":memory:")
        try:
            for stmt in EGRESS_SCHEMA:
                scratch.execute(stmt)
            _EXPECTED = _schema_objects(scratch)
        finally:
            scratch.close()
    return _EXPECTED


class EgressRecords:
    """`chat_egress_events` beside a coordinator store's tables, in its `dispatch.db`. `opener` is that store (any
    object with its `_connect()` and `_stamp()`, a `CoordStore` in use): this class adds its table to the file the
    opener owns and never opens the file itself. Every write is one `BEGIN IMMEDIATE` with the clock read under it.

    `duplicate_rows` is the decider's per-launch counter (R15-28-5): it lives in this object, one per decider
    service, so it covers the refusals this process saw."""

    def __init__(self, opener: Any) -> None:
        self._opener = opener
        self._ready = False
        self._lock = threading.Lock()
        self._duplicates: dict[str, int] = {}

    # --- store -------------------------------------------------------------------------------------------------
    def _connect(self):
        conn = self._opener._connect()
        if not self._ready:
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in EGRESS_SCHEMA:
                        conn.execute(stmt)
                    if _schema_objects(conn) != _expected_objects():
                        raise EgressRecordError("this store's chat_egress_events table or triggers differ from this "
                                                "module's schema (another revision made them); refusing the store")
                    conn.execute("COMMIT")
                except BaseException:
                    # a failed COMMIT may already have ended the transaction; the ROLLBACK's own error must not
                    # replace the one being raised
                    with suppress(sqlite3.Error):
                        conn.execute("ROLLBACK")
                    raise
            except BaseException:
                conn.close()
                raise
            self._ready = True
        return conn

    def _insert(self, launch_id: str, request_id: str | None, kind: str, **cols: Any) -> bool:
        if not isinstance(launch_id, str) or not launch_id:
            raise EgressRecordError("an egress row names its launch")
        with write_transaction(self._connect) as conn:
            if request_id is not None:
                taken = (_ENDS if kind in _ENDS else (kind,))
                stored = conn.execute(f"SELECT {', '.join(('kind', *cols))} FROM chat_egress_events WHERE "
                                      f"launch_id = ? AND request_id = ? AND kind IN ({', '.join('?' * len(taken))})",
                                      (launch_id, request_id, *taken)).fetchone()
                if stored is not None:
                    same = stored[0] == kind and tuple(stored[1:]) == tuple(cols.values())
                    if kind == ATTEMPT and not same:
                        # L1-1: a reused request id with another decision; a False here would let the act diverge
                        raise EgressRecordError("a second attempt row for one request with a different decision: "
                                                "the stored decision would disagree with the act")
                    if kind == FAILED and same:
                        return False        # L1-5: the same mark again is a replay, not a glue bug
                    self._count_duplicate(launch_id, kind)
                    return False
                if kind in (OUTCOME, ENDED):
                    allowed = ("allow",) if kind == OUTCOME else ("allow", "undecided")
                    if conn.execute(f"SELECT 1 FROM chat_egress_events WHERE launch_id = ? AND request_id = ? AND "
                                    f"kind = '{ATTEMPT}' AND verdict IN ({', '.join('?' * len(allowed))})",
                                    (launch_id, request_id, *allowed)).fetchone() is None:
                        raise EgressRecordError(f"an {kind} row joins an attempt that was allowed"
                                                f"{'' if kind == OUTCOME else ' or still pending'}; there is none")
            now = self._opener._stamp()
            names = ["gseq", "launch_id", "request_id", "kind", "at", *cols]
            conn.execute(f"INSERT INTO chat_egress_events ({', '.join(names)}) VALUES "
                         f"({', '.join('?' * len(names))})",
                         (next_gseq(conn, now), launch_id, request_id, kind, now, *cols.values()))
        return True

    def _count_duplicate(self, launch_id: str, kind: str) -> None:
        with self._lock:
            n = self._duplicates.get(launch_id, 0) + 1
            self._duplicates[launch_id] = n
        if n == 1:
            log.warning("chat egress: a second %s row for one request of launch %s was refused (a glue bug); later "
                        "refusals in this launch are counted in duplicate_rows, not logged", kind, launch_id)

    # --- writes (the composer runs them for lifecycle's WRITE_* effects, in order) -----------------------------
    def write_attempt(self, launch_id: str, request_id: str | None, verdict: Verdict, attempt: Attempt) -> bool:
        """The `egress_attempt` row (WRITE_ATTEMPT, `Step.verdict`). Committed when this returns True; False: an
        identical replay of the stored attempt, refused and counted. An attempt for the same request with any
        decision field different raises `EgressRecordError`, so the effect fails and the act cannot diverge from the
        record."""
        values = _check_attempt(request_id, verdict, attempt)
        return self._insert(launch_id, request_id, ATTEMPT, **dict(zip(
            ("verdict", "deciding_step", "host", "method", "path", "names", "setup_record_id", "attach",
             "open_network"), values)))

    def write_outcome(self, launch_id: str, obs: wire.ObsResponse, content_type_class: str) -> bool:
        """The `egress_outcome` row (WRITE_OUTCOME) from the glue's `resp` line: its status, `declared_length`,
        `ended = unknown`, and `content_type_class`, R15-21-3's enum as the decider classified the response (S3b's
        "content_type_class" / "AdvertisementSet.observe" give it). The raw `content_type` and `content_length` are
        not stored. The enum must agree with the observation on presence: "absent" exactly when it has no
        Content-Type."""
        if type(obs) is not wire.ObsResponse:
            raise EgressRecordError("an outcome row is written from a wire.ObsResponse")
        rid = _check_request_id(obs.request_id)
        if type(obs.status) is not int or not 100 <= obs.status <= 599:
            raise EgressRecordError("an outcome's status is 100..599")
        if type(content_type_class) is not str or content_type_class not in CONTENT_TYPE_CLASSES:
            raise EgressRecordError(f"an outcome's content_type_class is one of {CONTENT_TYPE_CLASSES} (R15-21-3)")
        if (content_type_class == CT_ABSENT) != (obs.content_type is None):
            raise EgressRecordError("content_type_class is 'absent' exactly when the response has no Content-Type")
        return self._insert(launch_id, rid, OUTCOME, status=obs.status,
                            declared_length=wire.declared_length(obs.content_length), ended=ENDED_UNKNOWN,
                            content_type_class=content_type_class)

    def write_ended(self, launch_id: str, request_id: str) -> bool:
        """The `egress_ended_before_outcome` row (WRITE_ENDED): no cause, no other field (R15-29-1)."""
        return self._insert(launch_id, _check_request_id(request_id), ENDED)

    def write_failed(self, launch_id: str, request_id: str, effect: Effect) -> bool:
        """The `egress_failed` row: the durable FAILED mark for a request whose NON-database `effect` raised. It
        needs no attempt row (a card's end runs before the attempt write). False: a second mark, counted when it
        names a different effect, a silent replay when it names the same one."""
        if type(effect) is not Effect or effect in _WRITE_EFFECTS:
            raise EgressRecordError("the FAILED mark names a non-database effect; a failed row write writes nothing "
                                    "more")
        return self._insert(launch_id, _check_request_id(request_id), FAILED, failed_effect=effect.value)

    def record_failure(self, launch_id: str, request_id: str, failure: Failure) -> bool:
        """The composer's call after `fire` raised (`RequestLifecycle.failure`): the head's FAILED ruling. A failed
        row write -> nothing written, False. Any other effect -> the FAILED mark, True when stored. If the store
        refuses this write too, sqlite3's error propagates, and the rows that landed read as they are."""
        if type(failure) is not Failure:
            raise EgressRecordError("record_failure takes lifecycle's Failure")
        if failure.effect in _WRITE_EFFECTS:
            return False
        return self.write_failed(launch_id, request_id, failure.effect)

    # --- reads ---------------------------------------------------------------------------------------------------
    def rows(self, launch_id: str, request_id: str | None = None) -> list[EgressRow]:
        """A launch's rows in `gseq` order, or one request's."""
        sql = f"SELECT {', '.join(_COLUMNS)} FROM chat_egress_events WHERE launch_id = ?"
        args: tuple[Any, ...] = (launch_id,)
        if request_id is not None:
            sql, args = sql + " AND request_id = ?", (launch_id, request_id)
        with read_transaction(self._connect) as conn:
            return [_row(r) for r in conn.execute(sql + " ORDER BY gseq", args)]

    def settled(self, launch_id: str, request_id: str) -> Settled | None:
        """What one request's stored rows read as, through `lifecycle.settle` (None: the request has no row). The
        FAILED mark's effect is passed as `failed_effect=`, and a stored end row beats it; the mark stays on its row
        (`rows`) for the card. This module keeps no reading set of its own. The `failed_effect` it passes is never
        a WRITE_* effect: a failed row write stores no mark (`record_failure`), and `write_failed` and the
        table's CHECK each refuse one.

        It reads one request by its id, so it never sees the `audit-missing` deny, whose `request_id` is NULL: that
        row is visible only through `rows(launch_id)`."""
        got = self.rows(launch_id, request_id)
        if not got:
            return None
        attempt = next((r for r in got if r.kind == ATTEMPT), None)
        mark = next((r for r in got if r.kind == FAILED), None)
        return settle(None if attempt is None else attempt.verdict, any(r.kind == OUTCOME for r in got),
                      any(r.kind == ENDED for r in got),
                      failed_effect=None if mark is None or mark.failed_effect is None else Effect(mark.failed_effect))

    def duplicate_rows(self, launch_id: str) -> int:
        """R15-28-5: the refused duplicate rows of `launch_id` this object counted (K1 surfaces it, R15-29-3)."""
        with self._lock:
            return self._duplicates.get(launch_id, 0)
