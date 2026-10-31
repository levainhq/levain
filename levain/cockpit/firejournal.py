"""levain.cockpit.firejournal — ``cockpit.db``, the one durable store of every cockpit decision (K2b
design §0, §2, §3, §4.1.2, §5).

A separate SQLite file in the broker's data root with its own schema and format marker, opened with
the durability of :mod:`levain.autonomic.durable_sqlite`. ``AutonomicDB`` is not reused: its schema would add
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
- :meth:`CockpitDB.expire_due` — expiry (§3.1), whose one definition is ``_expire``: off the signed record.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from levain.authority import SCHEMA as AUTHORITY_SCHEMA
from levain.authority import AuthoritySnapshot, conn_init_hint
from levain.autonomic.journal import JournalCorruptError
from levain.cockpit.consent import (
    ConsentError,
    encode,
    parse_stamp,
    record_sha256,
    utc_stamp,
)
from levain.cockpit.shortcode import (SHORT_ALPHABET, SHORT_LENGTH, SHORT_MAX_FAILURES, SHORT_PAIRING_TTL,
                                       display_short, normalize_short)
from levain.autonomic.durable_sqlite import MIN_SQLITE, open_durable, read_transaction, write_transaction

_OPERATION_ID = re.compile(r"cp[0-9a-f]{32}")

__all__ = [
    "CAP_ATTEMPTS", "CockpitDB", "CockpitFormatError", "DB_NAME", "DecideOutcome", "DecisionRows", "DeviceRevoked",
    "OFFER_TTL", "OFFERS_LIVE", "PAIRED_PREFIX", "PairingError",
    "EditRow", "EditsPage", "EffectRow", "FaultOutcome", "FIRST_RETRY_DELAY", "IdempotencyConflict",
    "IssueResult", "JournalCorruptError", "LabelRow", "OpView", "Outcome", "OUTCOME_WINDOW",
    "OUTCOMES_LIMIT", "OutcomeRow", "PENDING_CAP", "PendingCapExceeded", "PendingRow", "STORE_FORMAT", "UNINITIALISED", "authority_fence",
    "UnitResult", "UnitOutcome", "UnknownPending", "backoff_seconds", "conn_init_hint", "new_pending_id",
]

STORE_FORMAT = "levain-cockpit/3"
# the format a store is migrated FROM at first open (DESIGN §3.7); any other format is refused
_MIGRATES_FROM = "levain-cockpit/2"
# set by the migration until `levain cockpit ledger init` re-enrols the keys: every signed act refuses
UNINITIALISED = "authority_uninitialised"
DB_NAME = "cockpit.db"
PENDING_CAP = 8                        # WAITING pendings per principal generation (§3.1)
CAP_ATTEMPTS = 10                      # recorded transient faults before a unit ends `store error` (§4.1.2)
FIRST_RETRY_DELAY = timedelta(seconds=5)   # decided_at + this is a never-faulted unit's due time (§4.2)
_MAX_BACKOFF = 120
# the `outcome` facet's enum is the embedder's (cockpit_manifest_DESIGN_1009 §3.2.1 "COMMITTED-INCOMPLETE/STALE/REFUSED/
# EXPIRED/cancelled/deleted"), not the journal's state names
_OUTCOME_NAMES = {"COMMITTED_INCOMPLETE": "COMMITTED-INCOMPLETE", "CANCELLED": "cancelled"}
OUTCOME_WINDOW = timedelta(days=7)     # the decisions panel's outcomes reach back this far (K2b-3b §3)
OUTCOMES_LIMIT = 50                    # and show at most this many, newest first
# per-device pairing (PAIR REV 3 RQ-1): its own constants, nothing borrowed from webpush's pairing
PAIRED_PREFIX = "paired-"              # a paired device's credential name is PAIRED_PREFIX + its id (RP-1)
OFFER_TTL = timedelta(minutes=10)      # a pairing offer's life
OFFERS_LIVE = 3                        # live offers at once; a new one past this voids the oldest
LAST_SEEN_EVERY = timedelta(minutes=1)  # a device's last_seen is stamped at most this often (fail-soft)
_DEVICE_ID = re.compile(r"d-[0-9a-f]{12}")
_LABEL_MAX = 64

TERMINAL = ("COMMITTED", "COMMITTED_INCOMPLETE", "STALE", "REFUSED", "EXPIRED", "CANCELLED")
_PLANNED_STATES = ("DECIDED", "COMMITTED", "COMMITTED_INCOMPLETE")

# Key classes in ascending authority, and the least class each tier needs (flow design §9 K2b: T2 fires
# on laptop-presence or display-phone, T3 on laptop-presence only, presence on neither)
_CLASS_RANK = {"presence": 1, "display-phone": 2, "laptop-presence": 3}
# the tiers a consent request and a signed pending carry (levain.consent.queue reads this one: it takes no C1)
_TIER_NEEDS = {"T2": 2, "T3": 3}
# a pending's tier as a decision reads it: a C1 reaches the journal only as a device's signed pending (K8), which
# any enrolled key covers
_RECORD_TIER_NEEDS = {"C1": 1, **_TIER_NEEDS}

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
    "CREATE TABLE IF NOT EXISTS tokens (name TEXT PRIMARY KEY, generation INTEGER NOT NULL)",
    # per-device cockpit pairing (PAIR, 1024+18 REV 3 RQ-1): a paired device's transport token, stored as its SHA-256
    # only, and the one-time offers that mint one. Additive: an existing /3 journal gains them at its next open, and
    # nothing else moves. ``paired_by`` / ``minted_by`` name the credential that minted the row (a surface name,
    # ``paired-<id>`` or ``box``): the family a revoke takes whole (RQ-2, RS-5)
    "CREATE TABLE IF NOT EXISTS paired_devices ("
    " id TEXT PRIMARY KEY, label TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,"
    " created TEXT NOT NULL, last_seen TEXT, revoked_at TEXT, paired_by TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS pair_offers ("
    " code_hash TEXT PRIMARY KEY, label TEXT NOT NULL, minted_by TEXT NOT NULL,"
    " expires TEXT NOT NULL, used_at TEXT)",
    # the typed short code (W1b): at most ONE row, so "newest only" and "a new mint clears the misses" are one statement,
    # and a short code is never a row ``redeem_offer`` can read. ``label`` is NOT NULL: a redemption copies it into
    # ``paired_devices.label``
    "CREATE TABLE IF NOT EXISTS pair_short_offer ("
    " slot INTEGER PRIMARY KEY CHECK (slot = 1), code_hash TEXT NOT NULL, label TEXT NOT NULL,"
    " minted_by TEXT NOT NULL, expires TEXT NOT NULL, used_at TEXT, misses INTEGER NOT NULL DEFAULT 0)",
    # who may sign is a verified replay of this log (levain.authority), never a stored projection
    AUTHORITY_SCHEMA,
    "CREATE INDEX IF NOT EXISTS pendings_waiting ON pendings (state, expires_at)",
    "CREATE INDEX IF NOT EXISTS labels_lookup ON labels (resource_key, field, value_sha256)",
    # the edits reader pages APPLIED effects newest first (K2b-3b §0.6)
    "CREATE INDEX IF NOT EXISTS effects_applied ON effects (applied_at DESC, operation_id, idx) "
    "WHERE status = 'APPLIED'",
)

# ``meta.label_rev`` moves whenever anything a label snapshot reads may have changed, by TRIGGER, never
# by a call site (K2b-3b §0.3); what the snapshot also reads and is not watched is made immutable here
# instead. ``meta.value`` is TEXT, so every comparison and bump CASTs to INTEGER: a text compare reads
# '10' < '9' and would abort the tenth labelled fire (§8d row 2)
_BUMP = ("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'label_rev';")
_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS label_rev_on_label AFTER INSERT ON labels BEGIN {_BUMP} END",
    # a label reads its signer's key from the authority snapshot, which moves only by an appended link
    f"CREATE TRIGGER IF NOT EXISTS label_rev_on_authority AFTER INSERT ON authority_log BEGIN {_BUMP} END",
    "CREATE TRIGGER IF NOT EXISTS label_rev_on_meta_insert AFTER INSERT ON meta "
    f"WHEN NEW.key IN ('epoch', 'install_class') BEGIN {_BUMP} END",
    "CREATE TRIGGER IF NOT EXISTS label_rev_on_meta_update AFTER UPDATE ON meta "
    f"WHEN NEW.key IN ('epoch', 'install_class') OR OLD.key IN ('epoch', 'install_class') BEGIN {_BUMP} END",
    # label_rev only rises, and only as a canonical integer text ('07' would compare equal to 7)
    "CREATE TRIGGER IF NOT EXISTS label_rev_rises BEFORE UPDATE ON meta WHEN OLD.key = 'label_rev' BEGIN "
    "SELECT RAISE(ABORT, 'meta.label_rev only rises, as a canonical integer') "
    "WHERE NEW.key IS NOT 'label_rev' OR typeof(NEW.value) IS NOT 'text' "
    "OR CAST(NEW.value AS INTEGER) || '' IS NOT NEW.value "
    "OR CAST(NEW.value AS INTEGER) <= CAST(OLD.value AS INTEGER); END",
    "CREATE TRIGGER IF NOT EXISTS meta_keeps_its_authority BEFORE DELETE ON meta "
    "WHEN OLD.key IN ('epoch', 'install_class', 'label_rev') BEGIN "
    "SELECT RAISE(ABORT, 'meta epoch, install_class and label_rev are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS labels_immutable_update BEFORE UPDATE ON labels BEGIN "
    "SELECT RAISE(ABORT, 'a label row is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS labels_immutable_delete BEFORE DELETE ON labels BEGIN "
    "SELECT RAISE(ABORT, 'a label row is immutable'); END",
    # the code writes each authority column of a pending once, in decide (record and its sha at issue)
    "CREATE TRIGGER IF NOT EXISTS pendings_authority_immutable BEFORE UPDATE ON pendings "
    "WHEN NEW.record IS NOT OLD.record OR NEW.record_sha256 IS NOT OLD.record_sha256 "
    "OR (OLD.decision_kind IS NOT NULL AND NEW.decision_kind IS NOT OLD.decision_kind) "
    "OR (OLD.signer_fp IS NOT NULL AND NEW.signer_fp IS NOT OLD.signer_fp) "
    "OR (OLD.credential IS NOT NULL AND NEW.credential IS NOT OLD.credential) "
    "OR (OLD.decided_at IS NOT NULL AND NEW.decided_at IS NOT OLD.decided_at) BEGIN "
    "SELECT RAISE(ABORT, 'a pending''s record and decision are written once'); END",
)

# an op the settler found corrupt is marked in ``meta`` (``corrupt:<id>``) and leaves the due set; its
# rows are left as they are, as evidence
_NOT_CORRUPT = "NOT EXISTS (SELECT 1 FROM meta WHERE key = 'corrupt:' || p.pending_id)"


class CockpitFormatError(RuntimeError):
    """The database is not a cockpit journal of this format."""


class UnknownPending(LookupError):
    """No pending has this id."""


class IdempotencyConflict(ValueError):
    """The idempotency key is already bound to a request with other params (§2: a 409)."""


@dataclass(frozen=True)
class Occupant:
    """A WAITING pending counted against a principal's cap."""
    pending_id: str
    idempotency_key: str
    verb: str
    issued_at: str


class PendingCapExceeded(RuntimeError):
    """The principal already holds its cap of WAITING pendings (§3.1); ``occupying`` names them."""

    def __init__(self, message: str, occupying: Sequence[Occupant] = ()) -> None:
        super().__init__(message)
        self.occupying = tuple(occupying)


class DeviceRevoked(Exception):
    """A ``paired-*`` principal whose device row is revoked (or gone) asked to file a pending or mint an offer
    (PAIR RS-4): refused inside the journal's own write transaction, so a request authenticated just before a revoke
    cannot act after it. A sibling of :class:`PairingError`, never an ``OSError``: no ``except OSError`` on the way
    up may mistake it for an I/O fault."""


class PairingError(ValueError):
    """A pairing call's argument is malformed (a label, a device id)."""


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


@dataclass(frozen=True)
class EditRow:
    """One APPLIED effect of a COMMITTED or COMMITTED_INCOMPLETE op with non-empty ``changes`` (K2b-3b
    §0.6): ``verb``, ``resource_key``, ``store``, ``changes`` and ``target`` from the validated signed
    record, ``credential`` from the op's pending row (the record carries none), ``applied_at`` the
    broker's observation time at settle."""
    operation_id: str
    idx: int
    state: str
    verb: str | None
    resource_key: str
    store: str
    changes: Mapping[str, Any]
    target: Mapping[str, Any]
    decision_kind: str | None
    credential: str | None
    applied_at: str
    # the signed effect's projection and the fields it may label (``writes`` + ``binds``): what a
    # Recent edits row's label key names, bound to this op (K2b-3b §0.6)
    projection: Mapping[str, Any] | None = None
    label_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class EditsPage:
    """``rows`` newest first; ``skipped`` the corrupt ops passed over."""
    rows: tuple[EditRow, ...]
    skipped: int


@dataclass(frozen=True)
class PendingRow:
    """A pending whose effective state is WAITING; every fact but ``principal`` from its signed record."""
    pending_id: str
    verb: str | None
    target: Mapping[str, Any]
    principal: str
    tier: str
    issued_at: str
    expires_at: str


@dataclass(frozen=True)
class OutcomeRow:
    """An op terminal inside the outcome window (K2b-3b §3). ``outcome`` is its effective state, or
    ``deleted`` for a COMMITTED op whose plan holds a ``{"deleted": true}`` change; ``unapplied`` the
    resource keys of its UNAPPLIED effects."""
    operation_id: str
    state: str
    outcome: str
    terminal_at: str
    verb: str | None
    reason: str | None
    unapplied: tuple[str, ...]
    cancelled_by: str | None
    record_sha256: str


@dataclass(frozen=True)
class DecisionRows:
    pendings: tuple[PendingRow, ...]
    outcomes: tuple[OutcomeRow, ...]
    skipped: int


# ---------------------------------------------------------------------------------------------------
# parsing helpers: every failure is JournalCorruptError for the op (§2)

def _corrupt(op: str | None, why: str) -> JournalCorruptError:
    return JournalCorruptError(f"cockpit op {op}: {why}")


# every way a stored JSON column can fail to parse; the one list the three parse sites catch
_UNPARSEABLE = (TypeError, UnicodeDecodeError, ValueError, RecursionError)   # ConsentError is a ValueError


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
    except _UNPARSEABLE as exc:
        raise _corrupt(op, f"record does not parse: {type(exc).__name__}: {str(exc)[:200]}") from exc
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
    # the fields a decision reads off the record (its authority), each checked here, at the one parse
    if record.get("intent") not in ("sign", "reported"):
        raise _corrupt(op, f"record intent {record.get('intent')!r} is neither sign nor reported")
    # a C1 is journalled only as a device's signed pending (K8): never a reported one
    if record.get("tier") not in ("T2", "T3") and not (record.get("tier") == "C1" and record.get("intent") == "sign"):
        raise _corrupt(op, f"record tier {record.get('tier')!r} is neither T2 nor T3 (nor a signed C1)")
    for name in ("issued_at", "expires_at"):
        _check_stamp(str(op), f"record {name}", record.get(name), nullable=False)
    if parse_stamp(record["issued_at"]) >= parse_stamp(record["expires_at"]):
        raise _corrupt(op, "record expires_at is not after its issued_at")
    return record


def _check_stamp(op: str, name: str, value: Any, *, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    try:
        parse_stamp(value)
    except ConsentError as exc:
        raise _corrupt(op, f"{name} does not parse: {value!r}") from exc


def _expire(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """§3.1, the one definition of expiry, run inside the caller's write transaction: every
    ``WAITING`` pending whose SIGNED ``expires_at`` has come ends ``EXPIRED`` at that instant; the
    ids it ended. The ``expires_at`` column is a mirror a writer of cockpit.db can move, so it is
    never read here (codex L3 r5 #1). A row whose record does not parse is skipped, not raised: its
    own reads fail closed, and one corrupt row must not stop every issue, cancel and start."""
    cutoff = parse_stamp(utc_stamp(now))        # a naive ``now`` is refused before any row is read
    ended: list[str] = []
    for row in conn.execute("SELECT pending_id, record, record_sha256 FROM pendings "
                            "WHERE state = 'WAITING'").fetchall():
        try:
            record = _parse_record(row[0], row[1], row[2])
        except JournalCorruptError:
            continue
        if parse_stamp(record["expires_at"]) <= cutoff:
            conn.execute("UPDATE pendings SET state = 'EXPIRED', reason = 'expired', terminal_at = ? "
                         "WHERE pending_id = ? AND state = 'WAITING'", (record["expires_at"], row[0]))
            ended.append(row[0])
    return ended


def _cancel_waiting(conn: sqlite3.Connection, principal: str, target_id: Any) -> bool:
    """Whether ``principal`` already holds a ``WAITING`` ``pending_cancel`` of ``target_id``, read off the signed
    records (the ``verb`` column is a mirror, as ``expires_at`` is in :func:`_expire`). A row whose record does not
    parse counts as one, so the cap applies (fail closed)."""
    for row in conn.execute("SELECT pending_id, record, record_sha256 FROM pendings "
                            "WHERE principal = ? AND state = 'WAITING'", (principal,)).fetchall():
        try:
            record = _parse_record(row[0], row[1], row[2])
        except JournalCorruptError:
            return True
        target = record.get("target") if isinstance(record.get("target"), dict) else {}
        if record.get("verb") == "pending_cancel" and target.get("pending_id") == target_id:
            return True
    return False


def _frees_a_write(conn: sqlite3.Connection, target_id: Any, principal: str) -> bool:
    """Whether ``principal`` cancelling ``target_id`` frees a held write of its own: the target is ``WAITING``, was
    issued by this same FULL principal (a device's transport included, K8 B5 condition 4: a cancel from another
    transport claiming the same key is never own, so it meets its own cap), and its signed record is not itself a
    ``pending_cancel``. A cancel of a cancel, or of a pending already ended, frees nothing, so it gets no reserved slot
    (else each would be "the first for its target" and a chain of them would pass the cap). A target whose record does
    not parse frees nothing either (fail closed: the cap applies)."""
    if not isinstance(target_id, str):
        return False
    row = conn.execute("SELECT pending_id, record, record_sha256 FROM pendings "
                       "WHERE pending_id = ? AND state = 'WAITING' AND principal = ?",
                       (target_id, principal)).fetchone()
    if row is None:
        return False
    try:
        record = _parse_record(row[0], row[1], row[2])
    except JournalCorruptError:
        return False
    return record.get("verb") != "pending_cancel"


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
        except _UNPARSEABLE as exc:
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


TARGET_IDENTITY_KEYS = ("kind", "panel_id", "row_id", "pending_id", "name")


def target_identity(target: Mapping[str, Any] | None) -> dict[str, Any]:
    """What a write is AIMED AT, without its versions: one key reused for another row is another
    request, while a replay after its own fire (which moved the row's version) is the same one."""
    target = target or {}
    return {k: target[k] for k in TARGET_IDENTITY_KEYS if target.get(k) is not None}


def request_shape(*, verb: Any, intent: Any, params: Any, target: Mapping[str, Any] | None) -> bytes:
    """What makes two POSTs "the same request" for idempotency (§2): the verb, its intent, its params
    and its target's identity; the effects' expect versions and the row version may move between a
    POST and its replay."""
    return encode({"verb": verb, "intent": intent, "params": params, "target": target_identity(target)})


def _request_shape(record: Mapping[str, Any]) -> bytes:
    return request_shape(verb=record.get("verb"), intent=record.get("intent"), params=record.get("params"),
                         target=record.get("target"))


# ---------------------------------------------------------------------------------------------------

class CockpitDB:
    """``cockpit.db`` in ``data_root``. Every operation opens its own connection; every write is one
    ``BEGIN IMMEDIATE`` transaction. ``data_root`` must exist: the broker's start sequence checks and
    owns it (§4.2, §6), so this never creates it."""

    def __init__(self, data_root: Path | str, *, install_class: str | None = None, owns_lock: bool = False) -> None:
        """``install_class`` is the class this opener serves (None: it migrates nothing) and
        ``owns_lock`` says it already holds ``<data_root>/cockpit.lock`` (the broker, from its start
        sequence). Both matter only to a ``levain-cockpit/2`` journal's one-way migration
        (:meth:`_ensure_schema`)."""
        self.data_root = Path(data_root)
        self.path = self.data_root / DB_NAME
        self._ready = False
        self._install_class = install_class
        self._owns_lock = owns_lock

    # -- connection -----------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            if not self.path.exists():
                raise CockpitFormatError(f"journal {self.path} has disappeared")
        elif not self.data_root.is_dir():
            raise CockpitFormatError(f"data root {self.data_root} is not a directory")
        conn = open_durable(self.path, min_sqlite=MIN_SQLITE, needs="the cockpit journal's RETURNING")
        conn.row_factory = sqlite3.Row
        try:
            if not self._ready:
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    def _connect_nowait(self) -> sqlite3.Connection:
        """:meth:`_connect` with ``busy_timeout = 0``: a write on it fails at once on a held lock (the fail-soft
        ``last_seen`` stamp, F1)."""
        conn = self._connect()
        try:
            conn.execute("PRAGMA busy_timeout = 0")
        except BaseException:
            conn.close()
            raise
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create or check the schema. A ``levain-cockpit/2`` journal migrates to /3 (one way) only
        when, before anything is written, its stored install class is this opener's (an unset one only
        for the lock's owner, which sets it next), and only under ``cockpit.lock``: held by the broker
        already, else taken here non-blocking for the migrating transaction, so a server still running
        (an old ``levain serve``) refuses the migration rather than losing its journal under it."""
        lock_fds: list[int] = []        # the lock this opener took, held until the migration commits
        try:
            self._ensure_schema_txn(conn, lock_fds)
        finally:
            for fd in lock_fds:
                os.close(fd)

    def _ensure_schema_txn(self, conn: sqlite3.Connection, lock_fds: list[int]) -> None:
        with _txn(conn):
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            marked = "meta" in tables and conn.execute(
                "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is not None
            if tables and not marked:
                raise CockpitFormatError("the database holds tables but no format marker: it is not "
                                         "a cockpit journal, and is not taken over")
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone() \
                if marked else None
            migrate = row is not None and row[0] == _MIGRATES_FROM
            if row is not None and row[0] != STORE_FORMAT and not migrate:
                raise CockpitFormatError(f"journal format {row[0]!r} is not {STORE_FORMAT!r}")
            if migrate:
                stored = _meta(conn, "install_class")
                if self._install_class is None \
                        or (stored != self._install_class and not (stored is None and self._owns_lock)):
                    raise CockpitFormatError(f"this {_MIGRATES_FROM} journal's install class is {stored!r}, "
                                             f"not {self._install_class!r}: it is not migrated here")
                if not self._owns_lock:
                    lock_fds.append(_take_store_lock(self.data_root))
                _migrate_to_3(conn)
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            # the install step sets the epoch and moves it (§6); a fresh journal starts at 1 so a
            # label row always has one to carry
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', '1')")
            # the triggers are created AFTER these inserts, so seeding epoch and label_rev bumps nothing and a
            # fresh journal's label_rev starts at 0 (an INSERT OR IGNORE that ignores fires no trigger later)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('label_rev', '0')")
            for stmt in _TRIGGERS:
                conn.execute(stmt)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction."""
        with write_transaction(self._connect) as conn:
            yield conn

    @contextmanager
    def autocommit(self) -> Iterator[sqlite3.Connection]:
        """A connection with no transaction open, closed after: for a caller that owns its own
        transactions (an authority-log append)."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

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

    def token_generation(self, name: str) -> int | None:
        with self.read() as conn:
            row = conn.execute("SELECT generation FROM tokens WHERE name = ?", (name,)).fetchone()
        return None if row is None else int(row[0])

    # -- expiry (§3.1) -------------------------------------------------------------------------

    def expire_due(self, now: datetime) -> int:
        """Mark every WAITING pending whose signed ``expires_at`` has come ``EXPIRED``; the count."""
        with self.write() as conn:
            return len(_expire(conn, now))

    # -- issue (§3.1 step 2) -------------------------------------------------------------------

    def issue(self, *, record_bytes: bytes, principal: str, idempotency_key: str, now: datetime,
              harness: str = "cockpit", cap_exempt: bool = False, own_cancel: bool = False) -> IssueResult:
        """Record a new ``WAITING`` pending holding ``record_bytes`` (the JCS bytes of a
        :func:`~levain.cockpit.consent.build_record` record), or return the pending an earlier
        request with this idempotency key made. ``cap_exempt`` is for a pending that takes
        ALLOWED-REPORTED in this same request, which never counts toward the cap. ``own_cancel`` is for a
        device's cancel of its own pending (K8): the FIRST waiting ``pending_cancel`` this principal holds for a
        ``WAITING`` target this same principal issued (transport included) that is not itself a cancel is outside the cap, checked in this transaction
        (:func:`_frees_a_write`); a second for the same target, or a cancel that frees no write, meets the cap."""
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
            _expire(conn, now)
            # PAIR RS-4: a paired device revoked since its request authenticated files nothing, checked in this
            # transaction (the revoke is one transaction too, so the two are ordered), beside the cap below
            _refuse_revoked_device(conn, principal)
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
            target = record.get("target") if isinstance(record.get("target"), dict) else {}
            reserved = own_cancel and record.get("verb") == "pending_cancel" \
                and _frees_a_write(conn, target.get("pending_id"), principal) \
                and not _cancel_waiting(conn, principal, target.get("pending_id"))
            if not (cap_exempt or reserved):
                held = conn.execute(
                    "SELECT pending_id, idempotency_key, verb, issued_at FROM pendings "
                    "WHERE principal = ? AND state = 'WAITING' ORDER BY issued_at, rowid",
                    (principal,)).fetchall()
                if len(held) >= PENDING_CAP:
                    raise PendingCapExceeded(
                        f"{principal} holds {len(held)} waiting pendings (cap {PENDING_CAP})",
                        [Occupant(*r) for r in held])
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
               recomputed: bytes | None, now: datetime, authority: AuthoritySnapshot | None,
               signer_fp: str | None = None, signature: str | None = None,
               pre_images: Sequence[Any] = ()) -> DecideOutcome:
        """The decision transaction. ``signer_fp`` is the fingerprint the caller VERIFIED over the
        stored bytes (§6.5), never a request field; ``recomputed`` is the record the caller rebuilt
        from a fresh snapshot (§3.2.1), or ``None`` when that snapshot faulted. ``pre_images`` holds
        one JSON value per effect, kept for the ``outcomes`` render.

        ``authority`` is the request's captured snapshot (``RequestAuthority.capture``), None when the
        log is empty; the signer is judged against it, never against a second read.

        Checks, in order: WAITING → not expired → credential current → the authority log's head is
        still the captured one → the signer → the record's fence names that head → the recompute
        equals the stored bytes. The first that fails ends the op
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
            # intent, tier and expiry are read off the signed record, never its mirror columns (they
            # index the journal; a writer of cockpit.db can move them, a signature binds the record).
            # The principal is a column: the record carries none (declared, L3 r4)
            want_kind = {"sign": "ALLOWED", "reported": "ALLOWED_REPORTED"}[record["intent"]]
            if decision_kind != want_kind:
                raise ValueError(f"a {record.get('intent')!r} pending cannot take {decision_kind}")
            if operation_id in _expire(conn, now):
                return DecideOutcome(operation_id, "EXPIRED", "expired", written=True)

            def terminal(state: str, reason: str) -> DecideOutcome:
                conn.execute("UPDATE pendings SET state = ?, reason = ?, terminal_at = ? "
                             "WHERE pending_id = ? AND state = 'WAITING'",
                             (state, reason, stamp, operation_id))
                return DecideOutcome(operation_id, state, reason, written=True)

            refusal = _credential_problem(conn, row["principal"])
            if refusal is not None:
                return terminal("REFUSED", refusal)
            # the snapshot was verified outside this transaction (no ssh-keygen under the write lock);
            # a head that moved since is a re-sign, as every append is for a waiting pending (§3.4)
            if _authority_head(conn) != _head_of(authority):
                return terminal("STALE", "authority changed")
            enrolled_at: str | None = None
            if decision_kind == "ALLOWED":
                refusal, enrolled_at = _key_problem(conn, authority, signer_fp, record["tier"])
                if refusal is not None:
                    return terminal("REFUSED", refusal)
            if record.get("fence") != authority_fence(authority):
                # seat ruling R1: a policy change is a re-sign, never an authority refusal (§9)
                return terminal("STALE", "authority changed")
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
            _expire(conn, now)
            row = _pending_row(conn, operation_id)
            _parse_record(operation_id, row["record"], row["record_sha256"])
            changed = conn.execute(
                "UPDATE pendings SET state = 'CANCELLED', reason = 'cancelled', cancelled_by = ?, "
                "terminal_at = ? WHERE pending_id = ? AND state = 'WAITING'",
                (by, stamp, operation_id)).rowcount
            if changed:
                return Outcome(operation_id, "CANCELLED", "cancelled", None)
            return _outcome(row)

    # -- cockpit.db's own objects as effects (§3.5, §4.1.4, §13) -------------------------------
    #
    # Each is one transaction under the store's own lock (SQLite's write lock), in §4.1.1's order:
    # the postcondition already holds -> "already"; the precondition holds -> mutate -> "applied";
    # else "precondition_lost".

    def ensure_token(self, name: str) -> int:
        """The token's current generation, creating it at 1 when the journal has never seen it."""
        if not isinstance(name, str) or not name or ":" in name:
            raise ValueError(f"token name {name!r} is empty or holds ':'")
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO tokens (name, generation) VALUES (?, 1)", (name,))
            (gen,) = conn.execute("SELECT generation FROM tokens WHERE name = ?", (name,)).fetchone()
        return int(gen)

    def rotate_token(self, name: str, *, expect_generation: int, new_generation: int, by: str,
                     now: datetime) -> str:
        """Token rotation as ONE transaction (§13): ``generation`` moves to the ABSOLUTE
        ``new_generation`` only from ``expect_generation``, and every WAITING pending of the old
        generation is cancelled in the same transaction."""
        if new_generation != expect_generation + 1:
            raise ValueError("a rotation moves the generation by exactly one")
        stamp = utc_stamp(now)
        with self.write() as conn:
            _expire(conn, now)
            return _rotate_in(conn, name, expect_generation=expect_generation, new_generation=new_generation,
                              by=by, stamp=stamp)

    # -- per-device pairing (PAIR REV 3 RQ-1..RQ-3, REV 4 RS-3..RS-5) ----------------------------
    #
    # Every pairing transition is one method and one ``BEGIN IMMEDIATE`` transaction of this journal, so a revoke's
    # cascade, its offers' expiry and each member's generation rotation (which cancels its WAITING pendings) commit
    # together or not at all. Codes and tokens are never stored: only their SHA-256.

    def mint_offer(self, *, label: Any, minted_by: str, now: datetime) -> str:
        """A one-time pairing code (128-bit, ``OFFER_TTL``), minted by the credential named ``minted_by`` (a surface
        name, ``paired-<id>`` or ``box``). Past ``OFFERS_LIVE`` live offers the oldest is voided. A ``paired-*``
        minter whose device is revoked is refused (:class:`DeviceRevoked`, RS-4)."""
        import secrets

        clean = _clean_label(label)
        if not isinstance(minted_by, str) or not minted_by or ":" in minted_by:
            raise PairingError(f"minted_by {minted_by!r} is empty or holds ':'")
        code = secrets.token_urlsafe(16)
        stamp = utc_stamp(now)
        with self.write() as conn:
            if _device_revoked(conn, minted_by):
                raise DeviceRevoked(f"{minted_by} is revoked: it mints no offer")
            live = conn.execute("SELECT code_hash FROM pair_offers WHERE used_at IS NULL AND expires > ? "
                                "ORDER BY expires, rowid", (stamp,)).fetchall()
            for (old,) in live[:max(0, len(live) - OFFERS_LIVE + 1)]:
                conn.execute("UPDATE pair_offers SET expires = ? WHERE code_hash = ?", (stamp, old))
            conn.execute("INSERT INTO pair_offers (code_hash, label, minted_by, expires, used_at) "
                         "VALUES (?, ?, ?, ?, NULL)", (_sha256(code), clean, minted_by, utc_stamp(now + OFFER_TTL)))
        return code

    def redeem_offer(self, code: Any, *, now: datetime) -> "tuple[str, str] | None":
        """Spend a live code: ``(device_id, token)`` for the new device, the token returned only here, or None for a
        code that is unknown, used, expired, or minted by a device since revoked (RQ-2). The spend and the device's
        insert (with ``paired_by`` = the offer's ``minted_by``) are one transaction: two claims of one code yield one
        device, and no device exists without its lineage."""
        import secrets

        if not isinstance(code, str) or not code or len(code) > 128:
            return None
        stamp = utc_stamp(now)
        # F3: a code that names no live-looking row is answered from a read, so a credential-less junk claim never
        # takes the journal's write lock; a live-looking one is re-checked below inside the write (single spend)
        with self.read() as conn:
            seen = conn.execute("SELECT used_at, expires FROM pair_offers WHERE code_hash = ?",
                                (_sha256(code),)).fetchone()
        if seen is None or seen["used_at"] is not None or seen["expires"] <= stamp:
            return None
        with self.write() as conn:
            row = conn.execute("SELECT label, minted_by, expires, used_at FROM pair_offers WHERE code_hash = ?",
                               (_sha256(code),)).fetchone()
            if row is None or row["used_at"] is not None or row["expires"] <= stamp:
                return None
            if _device_revoked(conn, row["minted_by"]):
                return None
            changed = conn.execute("UPDATE pair_offers SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                                   (stamp, _sha256(code))).rowcount
            if not changed:
                return None
            token = secrets.token_urlsafe(32)
            device_id = "d-" + secrets.token_hex(6)
            conn.execute("INSERT INTO paired_devices (id, label, token_hash, created, last_seen, revoked_at, "
                         "paired_by) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                         (device_id, row["label"], _sha256(token), stamp, row["minted_by"]))
        return device_id, token

    def mint_short_offer(self, *, label: Any, minted_by: str, now: datetime) -> str:
        """A typed pairing code (8 Crockford characters, ``SHORT_PAIRING_TTL``) minted by ``minted_by``, returned in
        its display form ``XXXX-XXXX``. It replaces the one row of ``pair_short_offer``, so only the newest redeems and
        its misses restart; ``pair_offers`` is never touched. The label is cleaned as :meth:`mint_offer` cleans it."""
        import secrets

        clean = _clean_label(label)
        if not isinstance(minted_by, str) or not minted_by or ":" in minted_by:
            raise PairingError(f"minted_by {minted_by!r} is empty or holds ':'")
        code = "".join(secrets.choice(SHORT_ALPHABET) for _ in range(SHORT_LENGTH))
        with self.write() as conn:
            if _device_revoked(conn, minted_by):
                raise DeviceRevoked(f"{minted_by} is revoked: it mints no offer")
            conn.execute("INSERT OR REPLACE INTO pair_short_offer (slot, code_hash, label, minted_by, expires, used_at, "
                         "misses) VALUES (1, ?, ?, ?, ?, NULL, 0)",
                         (_sha256(code), clean, minted_by, utc_stamp(now + SHORT_PAIRING_TTL)))
        return display_short(code)

    def redeem_short(self, code: Any, *, now: datetime) -> "tuple[str, str] | None":
        """Spend the live short code: ``(device_id, token)``, or None for anything else. A malformed code, and a
        journal with no live short row, are answered from a read (no write lock). Under the write lock the row is
        re-read and re-checked; a wrong code adds one miss, and the ``SHORT_MAX_FAILURES``th kills the code. A row
        whose code changed between the read and the lock (a newer mint replaced it) takes no miss: the try was made
        against the code it replaced."""
        import hmac
        import secrets

        typed = normalize_short(code)
        if typed is None:
            return None
        stamp = utc_stamp(now)
        with self.read() as conn:
            seen = conn.execute("SELECT code_hash, used_at, expires, misses FROM pair_short_offer "
                                "WHERE slot = 1").fetchone()
        if seen is None or seen["used_at"] is not None or seen["expires"] <= stamp or seen["misses"] >= SHORT_MAX_FAILURES:
            return None
        with self.write() as conn:
            row = conn.execute("SELECT code_hash, label, minted_by, expires, used_at, misses FROM pair_short_offer "
                               "WHERE slot = 1").fetchone()
            if (row is None or row["code_hash"] != seen["code_hash"] or row["used_at"] is not None
                    or row["expires"] <= stamp or row["misses"] >= SHORT_MAX_FAILURES):
                return None
            if _device_revoked(conn, row["minted_by"]):
                return None
            if not hmac.compare_digest(_sha256(typed), row["code_hash"]):
                conn.execute("UPDATE pair_short_offer SET misses = misses + 1 WHERE slot = 1 AND code_hash = ?",
                             (row["code_hash"],))
                return None
            conn.execute("UPDATE pair_short_offer SET used_at = ? WHERE slot = 1", (stamp,))
            token = secrets.token_urlsafe(32)
            device_id = "d-" + secrets.token_hex(6)
            conn.execute("INSERT INTO paired_devices (id, label, token_hash, created, last_seen, revoked_at, "
                         "paired_by) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                         (device_id, row["label"], _sha256(token), stamp, row["minted_by"]))
        return device_id, token

    def short_offer_state(self, *, now: datetime) -> str:
        """Why the short code is not live, or that it is, for a refusal's words only (a read; it decides nothing):
        ``"none"`` (never minted), ``"used"``, ``"guessed_out"`` (killed by ``SHORT_MAX_FAILURES`` wrong tries),
        ``"expired"`` (its ``SHORT_PAIRING_TTL`` ran out, or a revoke of its minter expired it) or ``"live"``."""
        stamp = utc_stamp(now)
        with self.read() as conn:
            row = conn.execute("SELECT used_at, expires, misses FROM pair_short_offer WHERE slot = 1").fetchone()
        if row is None:
            return "none"
        if row["used_at"] is not None:
            return "used"
        if row["misses"] >= SHORT_MAX_FAILURES:
            return "guessed_out"
        if row["expires"] <= stamp:
            return "expired"
        return "live"

    def verify_device(self, token: str, *, now: datetime) -> str | None:
        """The id of the unrevoked device ``token`` belongs to, or None (unknown or revoked): one indexed lookup of
        its SHA-256, a read and nothing else. ``now`` is unused here (kept for the caller's one clock); the
        ``last_seen`` stamp is :meth:`stamp_last_seen`, which the caller runs AFTER this answer (F1)."""
        del now
        with self.read() as conn:
            row = conn.execute("SELECT id, revoked_at FROM paired_devices WHERE token_hash = ?",
                               (_sha256(token),)).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        return str(row["id"])

    def stamp_last_seen(self, device_id: str, *, now: datetime) -> bool:
        """Stamp ``device_id``'s ``last_seen`` when it is due (at most once per ``LAST_SEEN_EVERY``), fail-soft: the
        write runs on a connection with ``busy_timeout = 0``, so a journal whose write lock is held skips the stamp
        at once instead of waiting (F1). True iff a row was stamped. Never part of any answer."""
        stamp = utc_stamp(now)
        due = utc_stamp(now - LAST_SEEN_EVERY)
        try:
            with self.read() as conn:
                row = conn.execute("SELECT last_seen FROM paired_devices WHERE id = ? AND revoked_at IS NULL",
                                   (device_id,)).fetchone()
            if row is None or (row[0] is not None and row[0] > due):
                return False
            with write_transaction(self._connect_nowait) as conn:
                return bool(conn.execute(
                    "UPDATE paired_devices SET last_seen = ? WHERE id = ? AND revoked_at IS NULL "
                    "AND (last_seen IS NULL OR last_seen <= ?)", (stamp, device_id, due)).rowcount)
        except sqlite3.OperationalError:
            return False        # busy (the lock is held) or locked: the stamp is skipped, nothing else moves

    def device_live(self, device_id: str) -> bool:
        """Whether ``device_id`` names an unrevoked device (the change stream's re-check, RU-3). Stamps nothing."""
        with self.read() as conn:
            row = conn.execute("SELECT revoked_at FROM paired_devices WHERE id = ?", (device_id,)).fetchone()
        return row is not None and row[0] is None

    def revoke_devices(self, device_ids: "Sequence[str] | None", *, by: str, now: datetime) -> list[str]:
        """Revoke each named device (None: every device) and its whole family, in ONE transaction (RQ-2, RS-3):
        the descendant closure over ``paired_by = 'paired-' || id``; each member not yet revoked gets
        ``revoked_at`` (monotonic), every unused offer it minted expires, and its broker generation rotates by one,
        which cancels its WAITING pendings (a member that never wrote gets its ``tokens`` row first). A member
        already revoked is skipped silently: revoked and rotated commit together, so it was rotated then. Returns
        the ids this call revoked. An unknown id raises :class:`PairingError` and nothing moves."""
        stamp = utc_stamp(now)
        roots: list[str] | None = None if device_ids is None else list(device_ids)
        for d in roots or ():
            if not isinstance(d, str) or _DEVICE_ID.fullmatch(d) is None:
                raise PairingError(f"not a device id: {d!r}")
        with self.write() as conn:
            # G6: every id is validated before _expire, so a refusal writes nothing
            if roots is None:
                roots = [r[0] for r in conn.execute("SELECT id FROM paired_devices ORDER BY rowid")]
            else:
                for d in roots:
                    if conn.execute("SELECT 1 FROM paired_devices WHERE id = ?", (d,)).fetchone() is None:
                        raise PairingError(f"no such device: {d}")
            _expire(conn, now)
            # RS-5: the closure's join is the credential-name rule itself, stated once here
            assert PAIRED_PREFIX == "paired-", "the closure below joins on the literal 'paired-' prefix"
            family: list[str] = []
            for root in roots:
                for (member,) in conn.execute(
                        "WITH RECURSIVE fam(id) AS (SELECT id FROM paired_devices WHERE id = ? "
                        "UNION SELECT d.id FROM paired_devices d JOIN fam ON d.paired_by = 'paired-' || fam.id) "
                        "SELECT id FROM fam", (root,)).fetchall():
                    if member not in family:
                        family.append(member)
            revoked: list[str] = []
            for member in family:
                if not conn.execute("UPDATE paired_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                                    (stamp, member)).rowcount:
                    continue
                name = PAIRED_PREFIX + member
                conn.execute("UPDATE pair_offers SET expires = ? WHERE minted_by = ? AND used_at IS NULL "
                             "AND expires > ?", (stamp, name, stamp))
                conn.execute("UPDATE pair_short_offer SET expires = ? WHERE minted_by = ? AND used_at IS NULL "
                             "AND expires > ?", (stamp, name, stamp))
                conn.execute("INSERT OR IGNORE INTO tokens (name, generation) VALUES (?, 1)", (name,))
                (gen,) = conn.execute("SELECT generation FROM tokens WHERE name = ?", (name,)).fetchone()
                _rotate_in(conn, name, expect_generation=int(gen), new_generation=int(gen) + 1, by=by, stamp=stamp)
                revoked.append(member)
        return revoked

    def list_devices(self) -> list[dict[str, Any]]:
        """Every paired device, oldest first: id, label, created, last_seen, revoked_at, paired_by. Never a hash."""
        with self.read() as conn:
            rows = conn.execute("SELECT id, label, created, last_seen, revoked_at, paired_by FROM paired_devices "
                                "ORDER BY created, rowid").fetchall()
        return [dict(r) for r in rows]

    def cancel_as_effect(self, target_id: str, *, by: str, now: datetime) -> str:
        """The cross-principal cancel's single effect on ``pend:<id>`` (§3.5): ``state`` WAITING ->
        CANCELLED, recording the cancelling operation."""
        stamp = utc_stamp(now)
        with self.write() as conn:
            _expire(conn, now)
            row = conn.execute("SELECT state FROM pendings WHERE pending_id = ?", (target_id,)).fetchone()
            if row is not None and row[0] == "CANCELLED":
                return "already"
            if row is None or row[0] != "WAITING":
                return "precondition_lost"
            conn.execute("UPDATE pendings SET state = 'CANCELLED', reason = 'cancelled', "
                         "cancelled_by = ?, terminal_at = ? WHERE pending_id = ? AND state = 'WAITING'",
                         (by, stamp, target_id))
        return "applied"

    def set_install_class(self, install_class: str) -> str:
        """The install class this journal was started under (§6); a journal never changes class
        here (a reported->governed move is the root helper's, with a new epoch). Returns the stored
        class, which the caller compares."""
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('install_class', ?)",
                         (install_class,))
            return str(_meta(conn, "install_class"))

    # -- readers -------------------------------------------------------------------------------

    def find_idempotent(self, principal_base: str, idempotency_key: str) -> str | None:
        """The pending an earlier request with this key made, if any (§2)."""
        with self.read() as conn:
            row = conn.execute("SELECT pending_id FROM pendings WHERE principal_base = ? AND "
                               "idempotency_key = ?", (principal_base, idempotency_key)).fetchone()
        return None if row is None else row[0]

    def decided_ops(self) -> list[str]:
        """Every ``DECIDED`` op not marked corrupt, oldest decision first: start recovery's input
        (§4.2)."""
        with self.read() as conn:
            rows = conn.execute("SELECT pending_id FROM pendings p WHERE state = 'DECIDED' AND "
                                f"{_NOT_CORRUPT} ORDER BY decided_at, rowid").fetchall()
        return [r[0] for r in rows]

    def mark_corrupt(self, operation_id: str, why: str, now: datetime) -> None:
        """Set a corrupt op aside: it leaves the due set and start recovery, and stays DECIDED."""
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                         (f"corrupt:{operation_id}", f"{utc_stamp(now)} {why}"))

    def load(self, operation_id: str) -> OpView:
        with self.read() as conn:
            return _load(conn, operation_id)

    def outcome(self, operation_id: str) -> Outcome:
        with self.read() as conn:
            row = _pending_row(conn, operation_id)
            _parse_record(operation_id, row["record"], row["record_sha256"])
            return _outcome(row)

    def earliest_due(self) -> datetime | None:
        """The earliest time a ``DECIDED`` op is due: a ``PLANNED`` unit at
        ``COALESCE(next_attempt_at, decided_at + 5 s)``, computed here so a NULL never hides a unit
        (§4.2), and an op with no planned unit left (its finish is owed) at once."""
        due = [d for _, d in self._due_rows()]
        return min(due) if due else None

    def due_ops(self, now: datetime) -> list[str]:
        """``DECIDED`` ops with a ``PLANNED`` unit due at ``now`` or a finish owed, oldest decision
        first."""
        out: list[str] = []
        for op, due in self._due_rows():
            if op not in out and due <= now:
                out.append(op)
        return out

    def _due_rows(self) -> list[tuple[str, datetime]]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT p.pending_id AS operation_id, e.next_attempt_at, p.decided_at, "
                "e.operation_id IS NULL AS finish_owed FROM pendings p "
                "LEFT JOIN effects e ON e.operation_id = p.pending_id AND e.status = 'PLANNED' "
                f"WHERE p.state = 'DECIDED' AND {_NOT_CORRUPT} "
                "ORDER BY p.decided_at, p.rowid").fetchall()
        return [(r["operation_id"], _finish_due(r) if r["finish_owed"] else _due(r)) for r in rows]

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

    def edits(self, limit: int) -> EditsPage:
        """The newest ``limit`` broker edits (K2b-3b §0.6, §8d row 7), in one read transaction: pages
        the APPLIED effects newest first, keeps those of a COMMITTED or COMMITTED_INCOMPLETE op whose
        signed effect has non-empty ``changes``, and stops at ``limit`` KEPT rows. An op that fails
        its integrity checks is skipped and counted once."""
        if limit <= 0:
            return EditsPage((), 0)
        kept: list[EditRow] = []
        views: dict[str, OpView | None] = {}
        corrupt: set[str] = set()
        with self.read() as conn:
            cur = conn.execute("SELECT operation_id, idx FROM effects WHERE status = 'APPLIED' "
                               "ORDER BY applied_at DESC, operation_id, idx")
            for op, idx in cur:
                if op not in views:
                    state = conn.execute("SELECT state FROM pendings WHERE pending_id = ?",
                                         (op,)).fetchone()
                    if state is None or state[0] not in ("COMMITTED", "COMMITTED_INCOMPLETE"):
                        views[op] = None
                        continue
                    try:
                        views[op] = _load(conn, op)
                    except JournalCorruptError:
                        views[op] = None
                        corrupt.add(op)
                        continue
                view = views[op]
                if view is None:
                    continue
                eff = view.effects[idx]
                changes = view.record["effects"][idx].get("changes")
                if eff.status != "APPLIED" or eff.applied_at is None \
                        or not isinstance(changes, dict) or not changes:
                    continue
                target = view.record.get("target")
                rec_eff = view.record["effects"][idx]
                proj = rec_eff.get("projection")
                kept.append(EditRow(
                    operation_id=op, idx=idx, state=view.state, verb=view.record.get("verb"),
                    resource_key=eff.resource_key, store=eff.store, changes=changes,
                    target=target if isinstance(target, dict) else {},
                    decision_kind=view.decision_kind, credential=view.credential,
                    applied_at=eff.applied_at, projection=proj if isinstance(proj, dict) else None,
                    label_fields=tuple(str(f) for f in (*rec_eff.get("writes", ()), *rec_eff.get("binds", ())))))
                if len(kept) >= limit:
                    break
        return EditsPage(tuple(kept), len(corrupt))

    def decision_rows(self, now: datetime) -> DecisionRows:
        """The broker's rows of the ``decisions`` panel (K2b-3b §3), in one read transaction.
        ``pendings``: every pending whose EFFECTIVE state is WAITING, soonest signed expiry first.
        ``outcomes``: ops terminal inside ``OUTCOME_WINDOW`` whose effective state is
        COMMITTED_INCOMPLETE, STALE, REFUSED, EXPIRED or CANCELLED, and COMMITTED ops whose plan
        deletes, merged and cut ONCE at the newest ``OUTCOMES_LIMIT`` (design §3: "newest 50" over both). One ``effective_state`` serves both: a stored WAITING row
        past its signed ``expires_at`` IS EXPIRED at that instant. A corrupt op is skipped and
        counted."""
        cutoff = parse_stamp(utc_stamp(now))       # a naive ``now`` is refused before any row is read
        since = cutoff - OUTCOME_WINDOW
        pendings: list[tuple[datetime, int, PendingRow]] = []
        outcomes: list[tuple[datetime, int, OutcomeRow]] = []
        skipped = 0
        with self.read() as conn:
            rows = conn.execute(
                "SELECT rowid, * FROM pendings WHERE state = 'WAITING' OR (state IN "
                "('COMMITTED', 'COMMITTED_INCOMPLETE', 'STALE', 'REFUSED', 'EXPIRED', 'CANCELLED') "
                "AND terminal_at >= ?)", (utc_stamp(since),)).fetchall()
            for row in rows:
                op = row["pending_id"]
                try:
                    if row["state"] in _PLANNED_STATES:
                        view = _load(conn, op)
                        record, effects = view.record, view.effects
                    else:
                        record, effects = _parse_record(op, row["record"], row["record_sha256"]), ()
                    if row["state"] == "WAITING":
                        expires = parse_stamp(record["expires_at"])
                        if expires > cutoff:
                            pendings.append((expires, row["rowid"], PendingRow(
                                pending_id=op, verb=record.get("verb"),
                                target=record["target"] if isinstance(record.get("target"), dict) else {},
                                principal=row["principal"], tier=record["tier"],
                                issued_at=record["issued_at"], expires_at=record["expires_at"])))
                            continue
                        state, reason, terminal_at = "EXPIRED", "expired", record["expires_at"]
                    else:
                        state, reason, terminal_at = row["state"], row["reason"], row["terminal_at"]
                    at = parse_stamp(terminal_at)
                except (JournalCorruptError, ConsentError):
                    skipped += 1
                    continue
                if at < since:
                    continue
                outcome = _OUTCOME_NAMES.get(state, state)
                if state == "COMMITTED":
                    if not any(isinstance(e.get("changes"), dict) and e["changes"] == {"deleted": True}
                               for e in record["effects"]):
                        continue
                    outcome = "deleted"
                outcomes.append((at, row["rowid"], OutcomeRow(
                    operation_id=op, state=state, outcome=outcome, terminal_at=terminal_at,
                    verb=record.get("verb"), reason=reason,
                    unapplied=tuple(e.resource_key for e in effects if e.status == "UNAPPLIED"),
                    cancelled_by=row["cancelled_by"], record_sha256=row["record_sha256"])))
        pendings.sort(key=lambda t: (t[0], t[1]))
        outcomes.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return DecisionRows(pendings=tuple(p for *_, p in pendings),
                            outcomes=tuple(o for *_, o in outcomes[:OUTCOMES_LIMIT]), skipped=skipped)


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


def _finish_due(row: sqlite3.Row) -> datetime:
    """An op whose units are all recorded owes its finish since its decision: already due."""
    try:
        return parse_stamp(row["decided_at"])
    except ConsentError as exc:
        raise _corrupt(row["operation_id"], "a due time does not parse") from exc


def _rotate_in(conn: sqlite3.Connection, name: str, *, expect_generation: int, new_generation: int, by: str,
               stamp: str) -> str:
    """Token rotation inside the caller's open write transaction (§13; PAIR RS-3): ``generation`` moves to
    ``new_generation`` only from ``expect_generation``, and every WAITING pending of the old generation is cancelled.
    ``already`` / ``precondition_lost`` / ``applied``, as :meth:`CockpitDB.rotate_token` answers."""
    row = conn.execute("SELECT generation FROM tokens WHERE name = ?", (name,)).fetchone()
    if row is not None and row[0] == new_generation:
        return "already"
    if row is None or row[0] != expect_generation:
        return "precondition_lost"
    conn.execute("UPDATE tokens SET generation = ? WHERE name = ? AND generation = ?",
                 (new_generation, name, expect_generation))
    conn.execute("UPDATE pendings SET state = 'CANCELLED', reason = 'token rotated', "
                 "cancelled_by = ?, terminal_at = ? WHERE principal = ? AND state = 'WAITING'",
                 (by, stamp, f"token:{name}:{expect_generation}"))
    return "applied"


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_label(label: Any) -> str:
    """A device label as typed on the offering device (RP-6): 1..64 printable characters once stripped; absent is
    ``paired device``."""
    if label is None:
        return "paired device"
    if not isinstance(label, str):
        raise PairingError("a label is a string")
    clean = " ".join(label.split())
    if not clean or len(clean) > _LABEL_MAX or not clean.isprintable():
        raise PairingError(f"a label is 1..{_LABEL_MAX} printable characters")
    return clean


def _device_revoked(conn: sqlite3.Connection, name: str) -> bool:
    """Whether the credential ``name`` is a ``paired-*`` one whose device is revoked or gone (any other name: False)."""
    if not name.startswith(PAIRED_PREFIX):
        return False
    row = conn.execute("SELECT revoked_at FROM paired_devices WHERE id = ?",
                       (name[len(PAIRED_PREFIX):],)).fetchone()
    return row is None or row[0] is not None


def _refuse_revoked_device(conn: sqlite3.Connection, principal: str) -> None:
    """PAIR RS-4: a ``token:paired-<id>:<gen>`` principal whose device is revoked raises :class:`DeviceRevoked`."""
    tok = _token_parts(principal) if principal.startswith("token:") else None
    if tok is not None and _device_revoked(conn, tok[0]):
        raise DeviceRevoked(f"{tok[0]} is revoked: it files no pending")


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


def _key_problem(conn: sqlite3.Connection, authority: AuthoritySnapshot | None, fingerprint: str | None,
                 tier: str) -> tuple[str | None, str | None]:
    """The verified signer is an enrolled, unrevoked key of the captured authority whose class covers
    the tier (invariant 8), in a store whose authority is initialised. Returns (refusal, enrolled_at)."""
    if authority is None or _meta(conn, UNINITIALISED) is not None:
        return f"authority not initialised: run {conn_init_hint(conn)}", None
    key = authority.key(fingerprint) if isinstance(fingerprint, str) else None
    if key is None:
        return "key not enrolled", None
    if not key.active:
        return "key revoked", None
    need = _RECORD_TIER_NEEDS.get(tier)
    have = _CLASS_RANK.get(key.key_class)
    if need is None or have is None or have < need:
        return f"key class {key.key_class} does not cover {tier}", None
    return None, key.legacy_enrolled_at or key.enrolled_at


def authority_fence(authority: AuthoritySnapshot | None) -> dict[str, Any]:
    """The fence a record carries and a decision compares: the authority log's head it was issued
    under, its hash included (a replaced suffix holds another link at the same seq). An empty log (no
    authority yet) is store "" at seq 0 with hash ""."""
    if authority is None:
        return {"authority_store_id": "", "authority_seq": 0, "authority_head_hash": ""}
    return {"authority_store_id": authority.store_id, "authority_seq": authority.seq,
            "authority_head_hash": authority.head_hash}


def _head_of(authority: AuthoritySnapshot | None) -> tuple[int, str]:
    return (0, "") if authority is None else (authority.seq, authority.head_hash)


def _authority_head(conn: sqlite3.Connection) -> tuple[int, str]:
    row = conn.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
    return (0, "") if row is None else (row[0], row[1])


def _take_store_lock(data_root: Path) -> int:
    """The exclusive non-blocking ``flock`` on ``<data_root>/cockpit.lock`` a live broker holds for its
    whole active life (``levain.cockpit.broker.LOCK_NAME``); the fd, which the caller closes. Held
    elsewhere, a /2 journal's migration is refused, naming the server."""
    import fcntl

    from levain.cockpit.broker import LOCK_NAME
    lock = data_root / LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise CockpitFormatError(f"this journal is {_MIGRATES_FROM} and a cockpit server holds {lock}: stop "
                                 "`levain serve`, upgrade it, and restart it (the restarted server migrates "
                                 "the journal)") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """``levain-cockpit/2`` -> ``/3`` inside the open transaction (DESIGN §3.7). The ``keys`` rows are
    kept as ``legacy_keys``, read only by ``levain cockpit ledger init`` and authorising nothing; the
    ``keys`` and ``policy`` tables and ``meta.policy_rev`` are dropped; the store is marked
    uninitialised, so every signed act refuses until init; every WAITING pending ends STALE."""
    stamp = utc_stamp(datetime.now(timezone.utc))
    conn.execute("CREATE TABLE IF NOT EXISTS legacy_keys (fingerprint TEXT PRIMARY KEY, principal TEXT, "
                 "public_key TEXT, class TEXT, enrolled_at TEXT, enrolled_via TEXT, revoked_at TEXT, "
                 "compromised_since TEXT)")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "keys" in tables:
        conn.execute("INSERT OR IGNORE INTO legacy_keys SELECT fingerprint, principal, public_key, class, "
                     "enrolled_at, enrolled_via, revoked_at, compromised_since FROM keys")
    conn.execute("DROP TABLE IF EXISTS keys")        # its label_rev triggers go with it
    conn.execute("DROP TABLE IF EXISTS policy")
    conn.execute("DELETE FROM meta WHERE key = 'policy_rev'")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (STORE_FORMAT,))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, 'authority migrated')", (UNINITIALISED,))
    if "pendings" in tables:
        conn.execute("UPDATE pendings SET state = 'STALE', reason = 'authority migrated', terminal_at = ? "
                     "WHERE state = 'WAITING'", (stamp,))
    if "meta" in tables and conn.execute("SELECT 1 FROM meta WHERE key = 'label_rev'").fetchone():
        # labels read keys from the authority now: a memo built over the dropped table is stale
        conn.execute(_BUMP)


def _classify_difference(stored: dict[str, Any], recomputed: bytes) -> tuple[str, str]:
    """Why a fresh recompute differs from the signed record (§3.2.2, seat ruling R2): an effect count
    or any effect's ``expect`` moved is ``STALE "changed"`` (the state moved; re-sign); with every
    ``expect`` equal, any other difference, a raised tier included, is the normaliser's and is
    ``REFUSED "normaliser"``."""
    try:
        fresh = json.loads(recomputed.decode("utf-8"))
    except _UNPARSEABLE:
        return "REFUSED", "normaliser"
    if not isinstance(fresh, dict):
        return "REFUSED", "normaliser"
    old_fx, new_fx = stored.get("effects"), fresh.get("effects")
    if not isinstance(new_fx, list) or len(new_fx) != len(old_fx or []):
        return "STALE", "changed"
    if any(not isinstance(n, dict) or n.get("expect") != o.get("expect")
           for o, n in zip(old_fx or [], new_fx)):
        return "STALE", "changed"
    return "REFUSED", "normaliser"
