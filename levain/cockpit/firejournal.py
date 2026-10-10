"""levain.cockpit.firejournal — ``cockpit.db``, the one durable store of every cockpit decision (K2b
design §0, §2, §3, §4.1.2, §5).

A separate SQLite file in the broker's data root with its own schema and format marker, opened with
the durability of :mod:`levain.durable_sqlite`. ``AutonomicDB`` is not reused: its schema would add
the autonomic tables, and both schemas name an ``effects`` table (§0.1).

Every state transition is one method and one ``BEGIN IMMEDIATE`` transaction:

- :meth:`CockpitDB.issue` — expire what is due, the idempotency lookup, the per-principal cap, the
  ``WAITING`` insert (§3.1 step 2);
- :meth:`CockpitDB.decide` — the §3.2.2 checks in order and, on success, ``DECIDED`` plus one
  ``PLANNED`` effects row per effect: the commit point;
- :meth:`CockpitDB.record_unit` — one apply unit's results and its labels (§3.3);
- :meth:`CockpitDB.record_fault` — a transient fault on one unit, terminal at the tenth (§4.1.2);
- :meth:`CockpitDB.finish` — ``COMMITTED`` / ``COMMITTED_INCOMPLETE`` once no unit is planned (§3.4);
- :meth:`CockpitDB.cancel` — the ``WAITING``-predicate cancel (§3.5);
- :meth:`CockpitDB.expire_due` — the one definition of expiry (§3.1).

The effect plan is never copied out of the signed record: every reader parses it from ``record`` and
fails closed with :class:`JournalCorruptError` on a sha mismatch, an ``effects`` row disagreeing with
``record.effects[idx]``, or a column that does not parse (§2, integrity framing).

Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from levain.autonomic.journal import JournalCorruptError
from levain.cockpit.consent import (
    ConsentError,
    encode,
    parse_stamp,
    record_sha256,
    utc_stamp,
)
from levain.durable_sqlite import open_durable, read_transaction, write_transaction

_OPERATION_ID = re.compile(r"cp[0-9a-f]{32}")

__all__ = [
    "CAP_ATTEMPTS", "CockpitDB", "CockpitFormatError", "DB_NAME", "DecideOutcome", "EffectRow",
    "FaultOutcome", "FIRST_RETRY_DELAY", "IdempotencyConflict", "IssueResult", "JournalCorruptError",
    "LabelRow", "OpView", "Outcome", "PENDING_CAP", "PendingCapExceeded", "STORE_FORMAT",
    "UnitResult", "UnitOutcome", "UnknownPending", "backoff_seconds", "new_pending_id",
]

STORE_FORMAT = "levain-cockpit/1"
DB_NAME = "cockpit.db"
PENDING_CAP = 8                        # WAITING pendings per principal generation (§3.1)
CAP_ATTEMPTS = 10                      # recorded transient faults before a unit ends `store error` (§4.1.2)
FIRST_RETRY_DELAY = timedelta(seconds=5)   # decided_at + this is a never-faulted unit's due time (§4.2)
_MAX_BACKOFF = 120

TERMINAL = ("COMMITTED", "COMMITTED_INCOMPLETE", "STALE", "REFUSED", "EXPIRED", "CANCELLED")
_PLANNED_STATES = ("DECIDED", "COMMITTED", "COMMITTED_INCOMPLETE")

# Key classes in ascending authority, and the least class each tier needs (flow design §9 K2b: T2 fires
# on laptop-presence or display-phone, T3 on laptop-presence only, presence on neither)
_CLASS_RANK = {"presence": 1, "display-phone": 2, "laptop-presence": 3}
_TIER_NEEDS = {"T2": 2, "T3": 3}

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS pendings ("
    " pending_id TEXT PRIMARY KEY,"
    " harness TEXT NOT NULL DEFAULT 'cockpit',"
    " principal TEXT NOT NULL,"
    " principal_base TEXT NOT NULL,"
    " idempotency_key TEXT NOT NULL,"
    " verb TEXT NOT NULL, tier TEXT NOT NULL,"
    " intent TEXT NOT NULL CHECK (intent IN ('sign','reported')),"
    " record BLOB NOT NULL,"
    " record_sha256 TEXT NOT NULL,"
    " issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('WAITING','DECIDED','COMMITTED','COMMITTED_INCOMPLETE',"
    "  'STALE','REFUSED','EXPIRED','CANCELLED')),"
    " decision_kind TEXT CHECK (decision_kind IN ('ALLOWED','ALLOWED_REPORTED')),"
    " decided_at TEXT, signer_fp TEXT, signer_enrolled_at TEXT, signature TEXT, credential TEXT,"
    " reason TEXT, cancelled_by TEXT, terminal_at TEXT,"
    " UNIQUE (principal_base, idempotency_key))",
    "CREATE TABLE IF NOT EXISTS effects ("
    " operation_id TEXT NOT NULL REFERENCES pendings(pending_id), idx INTEGER NOT NULL,"
    " store TEXT NOT NULL, resource_key TEXT NOT NULL,"
    " pre_image TEXT,"
    " native_id TEXT,"
    " status TEXT NOT NULL CHECK (status IN ('PLANNED','APPLIED','UNAPPLIED')),"
    " reason TEXT, applied_at TEXT,"
    " attempts INTEGER NOT NULL DEFAULT 0,"
    " next_attempt_at TEXT, last_fault TEXT,"
    " PRIMARY KEY (operation_id, idx))",
    "CREATE TABLE IF NOT EXISTS labels ("
    " resource_key TEXT NOT NULL, field TEXT NOT NULL, operation_id TEXT NOT NULL,"
    " value_sha256 TEXT NOT NULL, decision_kind TEXT NOT NULL, signer_fp TEXT, credential TEXT,"
    " fired_at TEXT NOT NULL, epoch INTEGER NOT NULL,"
    " PRIMARY KEY (resource_key, field, operation_id))",
    "CREATE TABLE IF NOT EXISTS policy (key TEXT PRIMARY KEY, value TEXT NOT NULL, rev INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS tokens (name TEXT PRIMARY KEY, generation INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS keys ("
    " fingerprint TEXT PRIMARY KEY, principal TEXT NOT NULL UNIQUE, public_key TEXT NOT NULL,"
    " class TEXT NOT NULL, enrolled_at TEXT NOT NULL, enrolled_via TEXT NOT NULL,"
    " revoked_at TEXT, compromised_since TEXT)",
    "CREATE INDEX IF NOT EXISTS pendings_waiting ON pendings (state, expires_at)",
    "CREATE INDEX IF NOT EXISTS labels_lookup ON labels (resource_key, field, value_sha256)",
)

# §3.1: the one SQL definition of expiry; every caller runs this statement and no other
_EXPIRE_SQL = ("UPDATE pendings SET state = 'EXPIRED', reason = 'expired', terminal_at = expires_at "
               "WHERE state = 'WAITING' AND expires_at <= ?")
_HAS_DUE_SQL = "SELECT 1 FROM pendings WHERE state = 'WAITING' AND expires_at <= ? LIMIT 1"


class CockpitFormatError(RuntimeError):
    """The database is not a cockpit journal of this format."""


class UnknownPending(LookupError):
    """No pending has this id."""


class IdempotencyConflict(ValueError):
    """The idempotency key is already bound to a request with other params (§2: a 409)."""


class PendingCapExceeded(RuntimeError):
    """The principal already holds its cap of WAITING pendings (§3.1)."""


def new_pending_id() -> str:
    """``cp`` + 32 lowercase hex from ``os.urandom(16)``: unguessable (§2)."""
    return "cp" + os.urandom(16).hex()


def backoff_seconds(attempts: int) -> int:
    """The wait after the ``attempts``-th recorded fault: 5 s doubling, capped at 120 s (§4.1.2)."""
    return min(5 * 2 ** (attempts - 1), _MAX_BACKOFF)


# ---------------------------------------------------------------------------------------------------
# result types

Status = Literal["PLANNED", "APPLIED", "UNAPPLIED"]


@dataclass(frozen=True)
class Outcome:
    """A pending's recorded state. ``state`` is ``applying`` for a ``DECIDED`` op (§3.4, §4.2)."""
    operation_id: str
    state: str
    reason: str | None
    decision_kind: str | None


@dataclass(frozen=True)
class IssueResult:
    pending_id: str
    record: bytes
    replay: bool              # an earlier request with this idempotency key made the pending
    outcome: Outcome


@dataclass(frozen=True)
class DecideOutcome:
    """``state`` is ``DECIDED`` (the commit point), a terminal state, or ``WAITING`` when a reported
    decision's sources faulted on a signed op, which leaves it unspent."""
    operation_id: str
    state: str
    reason: str | None
    written: bool             # this call changed the pending

    @property
    def decided(self) -> bool:
        return self.state == "DECIDED" and self.written


@dataclass(frozen=True)
class UnitResult:
    """One member of an apply unit as ``settle`` classified it (§4.1). ``labels`` is the
    ``(field, value_sha256)`` of every field whose stored value equals its signed one."""
    idx: int
    status: Literal["APPLIED", "UNAPPLIED"]
    reason: str | None = None
    native_id: str | None = None
    labels: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class EffectRow:
    idx: int
    store: str
    resource_key: str
    pre_image: Any
    native_id: str | None
    status: Status
    reason: str | None
    applied_at: str | None
    attempts: int
    next_attempt_at: str | None
    last_fault: str | None


@dataclass(frozen=True)
class UnitOutcome:
    """``recorded``: this call wrote the unit. Otherwise a concurrent settler had, and ``effects`` is
    its stored result."""
    recorded: bool
    effects: tuple[EffectRow, ...]


@dataclass(frozen=True)
class FaultOutcome:
    """``RETRY`` (still planned, due at ``next_attempt_at``), ``CAPPED`` (every member now
    ``UNAPPLIED store error``) or ``SETTLED`` (a concurrent settler had already recorded the unit)."""
    kind: Literal["RETRY", "CAPPED", "SETTLED"]
    attempts: int | None
    next_attempt_at: str | None


@dataclass(frozen=True)
class OpView:
    """One op as every reader sees it: the pending's columns, the parsed record, the effects rows."""
    pending_id: str
    harness: str
    principal: str
    principal_base: str
    state: str
    decision_kind: str | None
    decided_at: str | None
    signer_fp: str | None
    credential: str | None
    reason: str | None
    record_bytes: bytes
    record: dict[str, Any]
    effects: tuple[EffectRow, ...]

    def due_at(self, row: EffectRow) -> datetime | None:
        """When a ``PLANNED`` member is next due: its ``next_attempt_at``, else ``decided_at`` + 5 s
        (§4.2). ``None`` for a member that is not planned."""
        if row.status != "PLANNED" or self.decided_at is None:
            return None
        if row.next_attempt_at is not None:
            return parse_stamp(row.next_attempt_at)
        return parse_stamp(self.decided_at) + FIRST_RETRY_DELAY


@dataclass(frozen=True)
class LabelRow:
    resource_key: str
    field: str
    operation_id: str
    value_sha256: str
    decision_kind: str
    signer_fp: str | None
    credential: str | None
    fired_at: str
    epoch: int


# ---------------------------------------------------------------------------------------------------
# parsing helpers: every failure is JournalCorruptError for the op (§2)

def _corrupt(op: str | None, why: str) -> JournalCorruptError:
    return JournalCorruptError(f"cockpit op {op}: {why}")


def _parse_record(op: str | None, blob: Any, sha: Any) -> dict[str, Any]:
    """The record a pending holds, or JournalCorruptError. ``op`` None (at issue, before the row
    exists) checks the id's form instead of its equality."""
    if not isinstance(blob, bytes) or not isinstance(sha, str):
        raise _corrupt(op, "record or its sha256 is not stored as bytes/text")
    if record_sha256(blob) != sha:
        raise _corrupt(op, "record bytes do not match record_sha256")
    try:
        record = json.loads(blob.decode("utf-8"))
        canonical = encode(record)
    except (UnicodeDecodeError, ValueError) as exc:     # ConsentError is a ValueError
        raise _corrupt(op, f"record does not parse: {exc}") from exc
    if canonical != blob:
        raise _corrupt(op, "record is not in canonical form")
    if not isinstance(record, dict):
        raise _corrupt(op, "record is not an object")
    if op is None:
        if not isinstance(record.get("operation_id"), str) \
                or _OPERATION_ID.fullmatch(record["operation_id"]) is None:
            raise _corrupt(op, "record operation_id is not a pending id")
    elif record.get("operation_id") != op:
        raise _corrupt(op, "record names another operation")
    effects = record.get("effects")
    if not isinstance(effects, list) or not all(
            isinstance(e, dict) and isinstance(e.get("store"), str)
            and isinstance(e.get("resource_key"), str) for e in effects):
        raise _corrupt(op, "record.effects is not a list of effects")
    return record


def _check_stamp(op: str, name: str, value: Any, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    try:
        parse_stamp(value)
    except ConsentError as exc:
        raise _corrupt(op, f"{name} does not parse: {value!r}") from exc


def _effect_rows(op: str, record: dict[str, Any], rows: list[sqlite3.Row],
                 state: str) -> tuple[EffectRow, ...]:
    plan = record["effects"]
    if state in _PLANNED_STATES:
        if [r["idx"] for r in rows] != list(range(len(plan))):
            raise _corrupt(op, "effects rows do not match record.effects one for one")
    elif rows:
        raise _corrupt(op, f"a {state} op has effects rows")
    out = []
    for r in rows:
        idx = r["idx"]
        if r["store"] != plan[idx]["store"] or r["resource_key"] != plan[idx]["resource_key"]:
            raise _corrupt(op, f"effects row {idx} disagrees with record.effects[{idx}]")
        attempts = r["attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) \
                or not 0 <= attempts <= CAP_ATTEMPTS:
            raise _corrupt(op, f"effects row {idx} attempts does not parse: {attempts!r}")
        _check_stamp(op, f"effects[{idx}].next_attempt_at", r["next_attempt_at"])
        _check_stamp(op, f"effects[{idx}].applied_at", r["applied_at"])
        try:
            pre = None if r["pre_image"] is None else json.loads(r["pre_image"])
        except (TypeError, ValueError) as exc:
            raise _corrupt(op, f"effects row {idx} pre_image does not parse") from exc
        if r["status"] == "PLANNED" and r["applied_at"] is not None:
            raise _corrupt(op, f"effects row {idx} is planned with an applied_at")
        out.append(EffectRow(idx=idx, store=r["store"], resource_key=r["resource_key"],
                             pre_image=pre, native_id=r["native_id"], status=r["status"],
                             reason=r["reason"], applied_at=r["applied_at"], attempts=attempts,
                             next_attempt_at=r["next_attempt_at"], last_fault=r["last_fault"]))
    return tuple(out)


def _outcome(row: sqlite3.Row) -> Outcome:
    state = row["state"]
    return Outcome(operation_id=row["pending_id"],
                   state="applying" if state == "DECIDED" else state,
                   reason=row["reason"], decision_kind=row["decision_kind"])


def _int_meta(op: str, value: str | None, name: str) -> int:
    try:
        return int(value)                    # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _corrupt(op, f"meta {name} does not parse: {value!r}") from exc


def _token_parts(principal: str) -> tuple[str, int] | None:
    """``token:<name>:<gen>`` → (name, gen); any other principal → None."""
    if not principal.startswith("token:"):
        return None
    name, _, gen = principal[len("token:"):].rpartition(":")
    if not name or not gen.isdigit():
        raise ValueError(f"malformed token principal: {principal!r}")
    return name, int(gen)


def _principal_base(principal: str) -> str:
    tok = _token_parts(principal)
    if tok is not None:
        return f"token:{tok[0]}"
    if principal.startswith("device:") and len(principal) > len("device:"):
        return principal
    raise ValueError(f"principal is neither token:<name>:<gen> nor device:<fingerprint>: {principal!r}")


def _request_shape(record: Mapping[str, Any]) -> bytes:
    # what makes two POSTs "the same request" for idempotency (§2): the verb, its intent and its
    # params; the effects' expect versions may move between a POST and its replay
    return encode({"verb": record.get("verb"), "intent": record.get("intent"),
                   "params": record.get("params")})


# ---------------------------------------------------------------------------------------------------

class CockpitDB:
    """``cockpit.db`` in ``data_root``. Every operation opens its own connection; every write is one
    ``BEGIN IMMEDIATE`` transaction. ``data_root`` must exist: the broker's start sequence checks and
    owns it (§4.2, §6), so this never creates it."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / DB_NAME
        self._ready = False

    # -- connection -----------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise CockpitFormatError(f"journal {self.path} has disappeared")
        elif not self.data_root.is_dir():
            raise CockpitFormatError(f"data root {self.data_root} is not a directory")
        conn = open_durable(self.path)
        conn.row_factory = sqlite3.Row
        try:
            if not self._ready:
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        with _txn(conn):
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            marked = "meta" in tables and conn.execute(
                "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is not None
            if tables and not marked:
                raise CockpitFormatError("the database holds tables but no format marker: it is not "
                                         "a cockpit journal, and is not taken over")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() \
                if marked else None
            if row is not None and row[0] != STORE_FORMAT:
                raise CockpitFormatError(f"journal format {row[0]!r} is not {STORE_FORMAT!r}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            # the install step sets the epoch and moves it (§6); a fresh journal starts at 1 so a
            # label row always has one to carry
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', '1')")
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('policy_rev', '0')")

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction."""
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot."""
        with read_transaction(self._connect) as conn:
            yield conn

    # -- meta ----------------------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def policy_rev(self) -> int:
        """The policy revision a record's fence names and a render caches on (§2 meta)."""
        return _int_meta("-", self.meta("policy_rev"), "policy_rev")

    def bump_policy_rev(self) -> int:
        with self.write() as conn:
            rev = _int_meta("-", _meta(conn, "policy_rev"), "policy_rev") + 1
            conn.execute("UPDATE meta SET value = ? WHERE key = 'policy_rev'", (str(rev),))
        return rev

    def token_generation(self, name: str) -> int | None:
        with self.read() as conn:
            row = conn.execute("SELECT generation FROM tokens WHERE name = ?", (name,)).fetchone()
        return None if row is None else int(row[0])

    # -- expiry (§3.1) -------------------------------------------------------------------------

    def has_due(self, now: datetime) -> bool:
        """The read-only probe a listing runs before it opens a write transaction (§3.1)."""
        with self.read() as conn:
            return conn.execute(_HAS_DUE_SQL, (utc_stamp(now),)).fetchone() is not None

    def expire_due(self, now: datetime) -> int:
        """Mark every WAITING pending whose ``expires_at`` has come ``EXPIRED``; the count."""
        with self.write() as conn:
            return conn.execute(_EXPIRE_SQL, (utc_stamp(now),)).rowcount

    # -- issue (§3.1 step 2) -------------------------------------------------------------------

    def issue(self, *, record_bytes: bytes, principal: str, idempotency_key: str, now: datetime,
              harness: str = "cockpit", cap_exempt: bool = False) -> IssueResult:
        """Record a new ``WAITING`` pending holding ``record_bytes`` (the JCS bytes of a
        :func:`~levain.cockpit.consent.build_record` record), or return the pending an earlier
        request with this idempotency key made. ``cap_exempt`` is for a pending that takes
        ALLOWED-REPORTED in this same request, which never counts toward the cap."""
        record = _parse_record(None, record_bytes, record_sha256(record_bytes))
        pid = record["operation_id"]
        intent = record.get("intent")
        if intent not in ("sign", "reported"):
            raise ValueError(f"record intent is not sign/reported: {intent!r}")
        _check_stamp(pid, "issued_at", record.get("issued_at"), nullable=False)
        _check_stamp(pid, "expires_at", record.get("expires_at"), nullable=False)
        base = _principal_base(principal)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        with self.write() as conn:
            conn.execute(_EXPIRE_SQL, (utc_stamp(now),))
            prior = conn.execute(
                "SELECT * FROM pendings WHERE principal_base = ? AND idempotency_key = ?",
                (base, idempotency_key)).fetchone()
            if prior is not None:
                old = _parse_record(prior["pending_id"], prior["record"], prior["record_sha256"])
                if _request_shape(old) != _request_shape(record) or prior["harness"] != harness:
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} is bound to another request")
                return IssueResult(pending_id=prior["pending_id"], record=prior["record"],
                                   replay=True, outcome=_outcome(prior))
            if not cap_exempt:
                (waiting,) = conn.execute(
                    "SELECT COUNT(*) FROM pendings WHERE principal = ? AND state = 'WAITING'",
                    (principal,)).fetchone()
                if waiting >= PENDING_CAP:
                    raise PendingCapExceeded(
                        f"{principal} holds {waiting} waiting pendings (cap {PENDING_CAP})")
            conn.execute(
                "INSERT INTO pendings (pending_id, harness, principal, principal_base, "
                "idempotency_key, verb, tier, intent, record, record_sha256, issued_at, expires_at, "
                "state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WAITING')",
                (pid, harness, principal, base, idempotency_key, record.get("verb"),
                 record.get("tier"), intent, record_bytes, record_sha256(record_bytes),
                 record["issued_at"], record["expires_at"]))
        return IssueResult(pending_id=pid, record=record_bytes, replay=False,
                           outcome=Outcome(pid, "WAITING", None, None))

    # -- decide (§3.2.2) -----------------------------------------------------------------------

    def decide(self, *, operation_id: str, decision_kind: Literal["ALLOWED", "ALLOWED_REPORTED"],
               recomputed: bytes | None, now: datetime, signer_fp: str | None = None,
               signature: str | None = None,
               pre_images: Sequence[Any] = ()) -> DecideOutcome:
        """The decision transaction. ``signer_fp`` is the fingerprint the caller VERIFIED over the
        stored bytes (§6.5), never a request field; ``recomputed`` is the record the caller rebuilt
        from a fresh snapshot (§3.2.1), or ``None`` when that snapshot faulted. ``pre_images`` holds
        one JSON value per effect, kept for the ``outcomes`` render.

        Checks, in order: WAITING → not expired → credential current → policy revision equals the
        record's fence → the recompute equals the stored bytes. The first that fails ends the op
        ``STALE`` (state moved) or ``REFUSED`` (authority, policy, normaliser), nothing written to
        any store. A faulted snapshot leaves a signed op ``WAITING`` (its signature unspent) and
        refuses a reported one."""
        if decision_kind not in ("ALLOWED", "ALLOWED_REPORTED"):
            raise ValueError(f"unknown decision kind {decision_kind!r}")
        if decision_kind == "ALLOWED" and (not signer_fp or not signature):
            raise ValueError("a signed decision needs the verified signer and the signature")
        stamp = utc_stamp(now)
        with self.write() as conn:
            row = _pending_row(conn, operation_id)
            record = _parse_record(operation_id, row["record"], row["record_sha256"])
            if row["state"] != "WAITING":
                return DecideOutcome(operation_id, row["state"], row["reason"], written=False)
            want_kind = "ALLOWED" if row["intent"] == "sign" else "ALLOWED_REPORTED"
            if decision_kind != want_kind:
                raise ValueError(f"a {row['intent']!r} pending cannot take {decision_kind}")
            conn.execute(_EXPIRE_SQL, (stamp,))
            if _pending_row(conn, operation_id)["state"] == "EXPIRED":
                return DecideOutcome(operation_id, "EXPIRED", "expired", written=True)

            def terminal(state: str, reason: str) -> DecideOutcome:
                conn.execute("UPDATE pendings SET state = ?, reason = ?, terminal_at = ? "
                             "WHERE pending_id = ? AND state = 'WAITING'",
                             (state, reason, stamp, operation_id))
                return DecideOutcome(operation_id, state, reason, written=True)

            refusal = _credential_problem(conn, row["principal"])
            enrolled_at: str | None = None
            if refusal is None and decision_kind == "ALLOWED":
                refusal, enrolled_at = _key_problem(conn, signer_fp, row["tier"])
            if refusal is not None:
                return terminal("REFUSED", refusal)
            fence = record.get("fence")
            rev = _int_meta(operation_id, _meta(conn, "policy_rev"), "policy_rev")
            if not isinstance(fence, dict) or fence.get("policy_rev") != rev:
                return terminal("REFUSED", "policy changed")
            if recomputed is None:
                if decision_kind == "ALLOWED":
                    return DecideOutcome(operation_id, "WAITING", "source unavailable",
                                         written=False)
                return terminal("REFUSED", "source unavailable")
            if recomputed != row["record"]:
                state, reason = _classify_difference(record, recomputed)
                return terminal(state, reason)
            plan = record["effects"]
            if len(pre_images) != len(plan):
                raise ValueError(f"{len(pre_images)} pre-images for {len(plan)} effects")
            conn.execute(
                "UPDATE pendings SET state = 'DECIDED', decision_kind = ?, decided_at = ?, "
                "signer_fp = ?, signer_enrolled_at = ?, signature = ?, credential = ? "
                "WHERE pending_id = ? AND state = 'WAITING'",
                (decision_kind, stamp, signer_fp if decision_kind == "ALLOWED" else None,
                 enrolled_at, signature if decision_kind == "ALLOWED" else None,
                 row["principal"], operation_id))
            for idx, (eff, pre) in enumerate(zip(plan, pre_images)):
                conn.execute(
                    "INSERT INTO effects (operation_id, idx, store, resource_key, pre_image, status) "
                    "VALUES (?, ?, ?, ?, ?, 'PLANNED')",
                    (operation_id, idx, eff["store"], eff["resource_key"],
                     None if pre is None else encode(pre).decode("utf-8")))
        return DecideOutcome(operation_id, "DECIDED", None, written=True)

    # -- apply (§3.3) --------------------------------------------------------------------------

    def record_unit(self, operation_id: str, results: Sequence[UnitResult],
                    now: datetime) -> UnitOutcome:
        """One apply unit's results in one transaction: one guarded UPDATE per member, the row
        counts summed. All members → labels inserted, committed. None → rolled back, a concurrent
        settler won, its stored result returned. Some → rolled back, the op is corrupt."""
        if not results:
            raise ValueError("an apply unit has at least one member")
        idxs = [r.idx for r in results]
        if len(set(idxs)) != len(idxs):
            raise ValueError("a unit names a member twice")
        for r in results:
            if r.status not in ("APPLIED", "UNAPPLIED"):
                raise ValueError(f"member {r.idx}: status {r.status!r}")
            if r.status == "UNAPPLIED" and (r.labels or not r.reason):
                raise ValueError(f"member {r.idx}: an unapplied effect has a reason and no labels")
        stamp = utc_stamp(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                view = _load(conn, operation_id)
                plan = view.record["effects"]
                for r in results:
                    if not 0 <= r.idx < len(plan):
                        raise ValueError(f"member {r.idx} is not an effect of {operation_id}")
                total = 0
                for r in results:
                    total += conn.execute(
                        "UPDATE effects SET status = ?, reason = ?, applied_at = ?, native_id = ? "
                        "WHERE operation_id = ? AND idx = ? AND status = 'PLANNED'",
                        (r.status, r.reason, stamp, r.native_id, operation_id, r.idx)).rowcount
                if total == 0:
                    conn.execute("ROLLBACK")
                    return UnitOutcome(recorded=False, effects=tuple(
                        e for e in view.effects if e.idx in set(idxs)))
                if total != len(results):
                    raise _corrupt(operation_id, f"{total} of {len(results)} unit members were "
                                                 "still planned")
                epoch = _int_meta(operation_id, _meta(conn, "epoch"), "epoch")
                for r in results:
                    for field, digest in r.labels:
                        conn.execute(
                            "INSERT INTO labels (resource_key, field, operation_id, value_sha256, "
                            "decision_kind, signer_fp, credential, fired_at, epoch) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (plan[r.idx]["resource_key"], field, operation_id, digest,
                             view.decision_kind, view.signer_fp, view.credential,
                             view.decided_at, epoch))
                conn.execute("COMMIT")
            except BaseException:
                _rollback_if_open(conn)
                raise
            return UnitOutcome(recorded=True, effects=())
        finally:
            conn.close()

    def record_fault(self, operation_id: str, unit_idxs: Sequence[int], fault_text: str,
                     now: datetime) -> FaultOutcome:
        """A ``TransientApplyFault`` on one unit, in one transaction (§4.1.2): every member's
        ``attempts`` + 1 and ``last_fault``; at the tenth, every member ``UNAPPLIED`` ``store error``
        naming this fault, in the same UPDATE, so no kill leaves ten attempts still planned."""
        idxs = list(unit_idxs)
        if not idxs or len(set(idxs)) != len(idxs):
            raise ValueError("a unit names each member once, and at least one")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                view = _load(conn, operation_id)
                members = [e for e in view.effects if e.idx in set(idxs)]
                if len(members) != len(idxs):
                    raise ValueError(f"unit {idxs} is not a set of effects of {operation_id}")
                planned = [e for e in members if e.status == "PLANNED"]
                if not planned:
                    conn.execute("ROLLBACK")
                    return FaultOutcome("SETTLED", None, None)
                if len({e.attempts for e in planned}) != 1:
                    raise _corrupt(operation_id, f"unit {idxs} members carry different attempts")
                attempts = planned[0].attempts + 1
                next_at = utc_stamp(now + timedelta(seconds=backoff_seconds(attempts)))
                terminal_reason = f"store error: {fault_text}"
                pairs = []
                for idx in idxs:
                    pairs.extend(conn.execute(
                        "UPDATE effects SET attempts = attempts + 1, last_fault = ?, "
                        "status = CASE WHEN attempts + 1 >= ? THEN 'UNAPPLIED' ELSE status END, "
                        "reason = CASE WHEN attempts + 1 >= ? THEN ? ELSE reason END, "
                        "next_attempt_at = CASE WHEN attempts + 1 >= ? THEN NULL ELSE ? END "
                        "WHERE operation_id = ? AND idx = ? AND status = 'PLANNED' "
                        "RETURNING attempts, status",
                        (fault_text, CAP_ATTEMPTS, CAP_ATTEMPTS, terminal_reason, CAP_ATTEMPTS,
                         next_at, operation_id, idx)).fetchall())
                if len(pairs) != len(idxs):
                    raise _corrupt(operation_id, f"{len(pairs)} of {len(idxs)} unit members were "
                                                 "still planned")
                if len({(p[0], p[1]) for p in pairs}) != 1:
                    raise _corrupt(operation_id, f"unit {idxs} members diverged on a fault")
                got_attempts, got_status = pairs[0][0], pairs[0][1]
                conn.execute("COMMIT")
            except BaseException:
                _rollback_if_open(conn)
                raise
        finally:
            conn.close()
        if got_status == "UNAPPLIED":
            return FaultOutcome("CAPPED", got_attempts, None)
        return FaultOutcome("RETRY", got_attempts, next_at)

    # -- finish (§3.4) and cancel (§3.5) -------------------------------------------------------

    def finish(self, operation_id: str, now: datetime) -> Outcome:
        """``COMMITTED`` when every effect applied, else ``COMMITTED_INCOMPLETE`` naming each
        unapplied resource key and its reason. A ``DECIDED`` op with a planned unit stays
        ``applying``; a terminal op returns its outcome."""
        with self.write() as conn:
            view = _load(conn, operation_id)
            unapplied = [e for e in view.effects if e.status == "UNAPPLIED"]
            state = "COMMITTED_INCOMPLETE" if unapplied else "COMMITTED"
            reason = "; ".join(f"{e.resource_key}: {e.reason}" for e in unapplied) or None
            changed = conn.execute(
                "UPDATE pendings SET state = ?, reason = ?, terminal_at = ? "
                "WHERE pending_id = ? AND state = 'DECIDED' AND NOT EXISTS "
                "(SELECT 1 FROM effects WHERE operation_id = ? AND status = 'PLANNED')",
                (state, reason, utc_stamp(now), operation_id, operation_id)).rowcount
            if changed:
                return Outcome(operation_id, state, reason, view.decision_kind)
            return _outcome(_pending_row(conn, operation_id))

    def cancel(self, operation_id: str, by: str, now: datetime) -> Outcome:
        """Cancel a ``WAITING`` pending; any other state is returned as recorded (invariant 6).
        The broker checks the canceller's tier before calling."""
        stamp = utc_stamp(now)
        with self.write() as conn:
            conn.execute(_EXPIRE_SQL, (stamp,))
            row = _pending_row(conn, operation_id)
            _parse_record(operation_id, row["record"], row["record_sha256"])
            changed = conn.execute(
                "UPDATE pendings SET state = 'CANCELLED', reason = 'cancelled', cancelled_by = ?, "
                "terminal_at = ? WHERE pending_id = ? AND state = 'WAITING'",
                (by, stamp, operation_id)).rowcount
            if changed:
                return Outcome(operation_id, "CANCELLED", "cancelled", None)
            return _outcome(row)

    # -- readers -------------------------------------------------------------------------------

    def load(self, operation_id: str) -> OpView:
        with self.read() as conn:
            return _load(conn, operation_id)

    def outcome(self, operation_id: str) -> Outcome:
        with self.read() as conn:
            row = _pending_row(conn, operation_id)
            _parse_record(operation_id, row["record"], row["record_sha256"])
            return _outcome(row)

    def earliest_due(self) -> datetime | None:
        """The earliest time a ``PLANNED`` unit of a ``DECIDED`` op is due:
        ``MIN(COALESCE(next_attempt_at, decided_at + 5 s))``, computed here so a NULL never hides a
        unit (§4.2)."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT e.operation_id, e.next_attempt_at, p.decided_at FROM effects e "
                "JOIN pendings p ON p.pending_id = e.operation_id "
                "WHERE p.state = 'DECIDED' AND e.status = 'PLANNED'").fetchall()
        due = [_due(r) for r in rows]
        return min(due) if due else None

    def due_ops(self, now: datetime) -> list[str]:
        """``DECIDED`` ops with a ``PLANNED`` unit due at ``now``, oldest decision first."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT e.operation_id, e.next_attempt_at, p.decided_at FROM effects e "
                "JOIN pendings p ON p.pending_id = e.operation_id "
                "WHERE p.state = 'DECIDED' AND e.status = 'PLANNED' "
                "ORDER BY p.decided_at, p.rowid").fetchall()
        out: list[str] = []
        for r in rows:
            if r["operation_id"] not in out and _due(r) <= now:
                out.append(r["operation_id"])
        return out

    def label_for(self, resource_key: str, field: str, value_sha256: str) -> LabelRow | None:
        """The label that describes a field's current value (§5): in the current epoch, the row whose
        value hash matches with the latest ``fired_at``, ties to the later-inserted pending."""
        with self.read() as conn:
            epoch = _int_meta("-", _meta(conn, "epoch"), "epoch")
            row = conn.execute(
                "SELECT l.*, p.record, p.record_sha256 FROM labels l "
                "JOIN pendings p ON p.pending_id = l.operation_id "
                "WHERE l.resource_key = ? AND l.field = ? AND l.value_sha256 = ? AND l.epoch = ? "
                "ORDER BY l.fired_at DESC, p.rowid DESC LIMIT 1",
                (resource_key, field, value_sha256, epoch)).fetchone()
        if row is None:
            return None
        _parse_record(row["operation_id"], row["record"], row["record_sha256"])
        _check_stamp(row["operation_id"], "label fired_at", row["fired_at"], nullable=False)
        return LabelRow(resource_key=row["resource_key"], field=row["field"],
                        operation_id=row["operation_id"], value_sha256=row["value_sha256"],
                        decision_kind=row["decision_kind"], signer_fp=row["signer_fp"],
                        credential=row["credential"], fired_at=row["fired_at"], epoch=row["epoch"])


# ---------------------------------------------------------------------------------------------------
# transaction-scoped helpers

@contextmanager
def _txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback_if_open(conn)
        raise
    conn.execute("COMMIT")


def _rollback_if_open(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _pending_row(conn: sqlite3.Connection, operation_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM pendings WHERE pending_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise UnknownPending(operation_id)
    return row


def _load(conn: sqlite3.Connection, operation_id: str) -> OpView:
    row = _pending_row(conn, operation_id)
    record = _parse_record(operation_id, row["record"], row["record_sha256"])
    for name in ("issued_at", "expires_at"):
        _check_stamp(operation_id, name, row[name], nullable=False)
    _check_stamp(operation_id, "decided_at", row["decided_at"],
                 nullable=row["state"] not in _PLANNED_STATES)
    rows = conn.execute("SELECT * FROM effects WHERE operation_id = ? ORDER BY idx",
                        (operation_id,)).fetchall()
    effects = _effect_rows(operation_id, record, rows, row["state"])
    return OpView(pending_id=operation_id, harness=row["harness"], principal=row["principal"],
                  principal_base=row["principal_base"], state=row["state"],
                  decision_kind=row["decision_kind"], decided_at=row["decided_at"],
                  signer_fp=row["signer_fp"], credential=row["credential"], reason=row["reason"],
                  record_bytes=row["record"], record=record, effects=effects)


def _due(row: sqlite3.Row) -> datetime:
    op = row["operation_id"]
    try:
        if row["next_attempt_at"] is not None:
            return parse_stamp(row["next_attempt_at"])
        return parse_stamp(row["decided_at"]) + FIRST_RETRY_DELAY
    except ConsentError as exc:
        raise _corrupt(op, "a due time does not parse") from exc


def _credential_problem(conn: sqlite3.Connection, principal: str) -> str | None:
    """The issuing principal's credential is still current: a token's generation is the one in
    ``tokens`` (§3.2.2)."""
    tok = _token_parts(principal)
    if tok is None:
        return None
    row = conn.execute("SELECT generation FROM tokens WHERE name = ?", (tok[0],)).fetchone()
    if row is None:
        return "token unknown"
    if row[0] != tok[1]:
        return "token rotated"
    return None


def _key_problem(conn: sqlite3.Connection, fingerprint: str | None,
                 tier: str) -> tuple[str | None, str | None]:
    """The verified signer is an enrolled, unrevoked key whose class covers the tier (invariant 8).
    Returns (refusal, enrolled_at)."""
    row = conn.execute("SELECT class, enrolled_at, revoked_at FROM keys WHERE fingerprint = ?",
                       (fingerprint,)).fetchone()
    if row is None:
        return "key not enrolled", None
    if row["revoked_at"] is not None:
        return "key revoked", None
    need = _TIER_NEEDS.get(tier)
    have = _CLASS_RANK.get(row["class"])
    if need is None or have is None or have < need:
        return f"key class {row['class']} does not cover {tier}", None
    return None, row["enrolled_at"]


def _classify_difference(stored: dict[str, Any], recomputed: bytes) -> tuple[str, str]:
    """Why a fresh recompute differs from the signed record (§3.2.2): an effect's precondition moved
    is ``STALE``; a tier raised is ``REFUSED`` (authority); anything else the normaliser produced
    differently is ``REFUSED`` (normaliser)."""
    try:
        fresh = json.loads(recomputed.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "REFUSED", "normaliser"
    if not isinstance(fresh, dict):
        return "REFUSED", "normaliser"
    if fresh.get("tier") != stored.get("tier"):
        return "REFUSED", "tier"
    old_fx, new_fx = stored.get("effects"), fresh.get("effects")
    if not isinstance(new_fx, list) or len(new_fx) != len(old_fx or []):
        return "STALE", "changed"
    if any(not isinstance(n, dict) or n.get("expect") != o.get("expect")
           for o, n in zip(old_fx or [], new_fx)):
        return "STALE", "changed"
    return "REFUSED", "normaliser"
