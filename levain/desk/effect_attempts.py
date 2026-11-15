"""levain.desk.effect_attempts: the night desk's effect journal (ND-3b, board levain#12; night desk design 2.6, 8.1).

TRUST-BOUNDARY CODE, beside :mod:`levain.desk.effects`, the only caller. One file (``effect_attempts.db``), its own
tables: NOT the autonomic journal and not the coord store (this row owns neither).

One ATTEMPT row is the reservation for one effect on one event: its idempotence key is the digest of
``event id | verb | args digest``, ``UNIQUE``, so a second reservation for the same effect on the same event never
makes a second row. The row is written, and committed durably (``synchronous = FULL``), BEFORE the effect runs, and is
written as ``unknown``: a process that dies after the reservation and before the result leaves exactly that row, and
nothing has to run for it to read ``unknown``. The result is a later, single transition out of ``unknown``: to ``ok`` or
``failed`` (the effect was settled), or to ``refused`` (the order stopped answering between the reservation and the run:
nothing ran). A retry of an ``ok``, ``failed`` or ``unknown`` attempt is answered with its recorded status and runs
nothing; only a ``refused`` row can be taken again (``refused`` to ``unknown``), and then it is a fresh reservation.

BUDGETS are checked and consumed in the same ``BEGIN IMMEDIATE`` transaction that inserts the reservation (never
check-then-act): the count of rows holding the same incident scope, or the same night scope, that are not ``refused`` must be
under the order line's budget, or the reservation is not written. A row consumes budget whatever its outcome (a failed or an
unknown attempt may have done its work).

The triggers refuse DELETE, an UPDATE that changes any identity column, and every status edge but the three above.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction

__all__ = ["Attempt", "BudgetSpent", "Journal", "STATUSES", "idem_key", "night_key"]

STATUSES = ("unknown", "ok", "failed", "refused")
SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS effect_attempts (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "idem_key TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL, order_sha256 TEXT NOT NULL, incarnation INTEGER NOT NULL, "
    "verb TEXT NOT NULL, event_id TEXT NOT NULL, args_digest TEXT NOT NULL, recipient TEXT, "
    "incident_scope TEXT NOT NULL, night_scope TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK (status IN ('unknown', 'ok', 'failed', 'refused')), "
    "reserved_at TEXT NOT NULL, settled_at TEXT)",
    "CREATE INDEX IF NOT EXISTS effect_attempts_incident ON effect_attempts (order_id, verb, incident_scope)",
    "CREATE INDEX IF NOT EXISTS effect_attempts_night ON effect_attempts (order_id, verb, night_scope)",
    "CREATE TRIGGER IF NOT EXISTS effect_attempts_no_delete BEFORE DELETE ON effect_attempts "
    "BEGIN SELECT RAISE(ABORT, 'an effect attempt is never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS effect_attempts_edges BEFORE UPDATE ON effect_attempts WHEN "
    "NEW.attempt_id IS NOT OLD.attempt_id OR NEW.idem_key IS NOT OLD.idem_key OR NEW.order_id IS NOT OLD.order_id OR "
    "NEW.verb IS NOT OLD.verb OR NEW.event_id IS NOT OLD.event_id OR NEW.args_digest IS NOT OLD.args_digest OR "
    "NEW.incident_scope IS NOT OLD.incident_scope OR NEW.night_scope IS NOT OLD.night_scope OR "
    "NEW.recipient IS NOT OLD.recipient OR NOT ((OLD.status = 'unknown' AND NEW.status IN ('ok', 'failed', 'refused')) "
    "OR (OLD.status = 'refused' AND NEW.status = 'unknown')) "
    "BEGIN SELECT RAISE(ABORT, 'an effect attempt changes only along its edges'); END",
    "CREATE TABLE IF NOT EXISTS effect_refusals (order_id TEXT NOT NULL, verb TEXT NOT NULL, event_id TEXT NOT NULL, "
    "reason_sha256 TEXT NOT NULL, reason TEXT NOT NULL, times INTEGER NOT NULL, first_at TEXT NOT NULL, "
    "last_at TEXT NOT NULL, PRIMARY KEY (order_id, verb, event_id, reason_sha256))",
)
#: the most distinct refusal rows kept: a hostile caller cannot grow the file without bound by varying the event id
_REFUSALS_MAX = 5000


class BudgetSpent(Exception):
    """The reservation was not written: ``which`` is ``"incident"`` or ``"night"``."""

    def __init__(self, which: str) -> None:
        super().__init__(which)
        self.which = which


@dataclass(frozen=True)
class Attempt:
    attempt_id: int
    idem_key: str
    order_id: str
    order_sha256: str
    incarnation: int
    verb: str
    event_id: str
    recipient: str | None
    status: str


_COLUMNS = "attempt_id, idem_key, order_id, order_sha256, incarnation, verb, event_id, recipient, status"


def _attempt(row: tuple) -> Attempt:
    return Attempt(*row)


def idem_key(event_id: str, verb: str, args_digest: str) -> str:
    """The idempotence key: event id, effect and the digest of its arguments (length-framed, so no two triples join)."""
    framed = "".join(f"{len(p)}:{p}|" for p in (event_id, verb, args_digest))
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def night_key(stamp: object, cut_hour_utc: int) -> str:
    """The night an aware ``datetime`` falls in: the UTC date of the instant ``cut_hour_utc`` hours before it, so a night
    runs from one cut to the next and never splits at midnight. (The arm record, O1, is the real window; until it lands this
    is the day-boundary a night budget counts in.)"""
    from datetime import datetime, timedelta, timezone
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError("a night key needs an aware datetime")
    return (stamp.astimezone(timezone.utc) - timedelta(hours=cut_hour_utc)).date().isoformat()


class Journal:
    """The attempts file at ``path`` (its directory must exist). Every method opens its own connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        with write_transaction(self._connect) as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        return open_durable(self.path, needs="the effect journal")

    def lookup(self, key: str) -> Attempt | None:
        with read_transaction(self._connect) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts WHERE idem_key = ?", (key,)).fetchone()
        return _attempt(row) if row else None

    def rows(self) -> list[Attempt]:
        with read_transaction(self._connect) as conn:
            return [_attempt(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts ORDER BY attempt_id")]

    def reserve(self, *, key: str, order_id: str, order_sha256: str, incarnation: int, verb: str, event_id: str,
                args_digest: str, recipient: str | None, incident_scope: str, night_scope: str, per_incident: int,
                per_night: int, now: str) -> tuple[Attempt, bool]:
        """ONE transaction: the existing row for ``key`` (returned as ``(row, False)`` with nothing written, unless it is
        ``refused``, which is taken again as ``unknown``), else a new ``unknown`` row. A new or re-taken reservation
        first counts the order's non-refused rows in its incident scope and in its night scope; a count at or over the
        budget raises :class:`BudgetSpent` and writes nothing. Returns ``(row, True)`` when THIS call holds the
        reservation (and so may run the effect), ``(row, False)`` when an earlier one does."""
        with write_transaction(self._connect) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts WHERE idem_key = ?", (key,)).fetchone()
            if row is not None and row[8] != "refused":
                return _attempt(row), False
            used_incident = conn.execute(
                "SELECT count(*) FROM effect_attempts WHERE order_id = ? AND verb = ? AND incident_scope = ? "
                "AND status != 'refused'", (order_id, verb, incident_scope)).fetchone()[0]
            if used_incident >= per_incident:
                raise BudgetSpent("incident")
            used_night = conn.execute(
                "SELECT count(*) FROM effect_attempts WHERE order_id = ? AND verb = ? AND night_scope = ? "
                "AND status != 'refused'", (order_id, verb, night_scope)).fetchone()[0]
            if used_night >= per_night:
                raise BudgetSpent("night")
            if row is None:
                conn.execute("INSERT INTO effect_attempts (idem_key, order_id, order_sha256, incarnation, verb, "
                             "event_id, args_digest, recipient, incident_scope, night_scope, status, reserved_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)",
                             (key, order_id, order_sha256, incarnation, verb, event_id, args_digest, recipient,
                              incident_scope, night_scope, now))
            else:
                conn.execute("UPDATE effect_attempts SET status = 'unknown', order_sha256 = ?, incarnation = ?, "
                             "reserved_at = ?, settled_at = NULL WHERE attempt_id = ?",
                             (order_sha256, incarnation, now, row[0]))
            fresh = conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts WHERE idem_key = ?", (key,)).fetchone()
        return _attempt(fresh), True

    def settle(self, attempt_id: int, status: str, now: str) -> None:
        """The one transition out of ``unknown``: ``ok``, ``failed`` or ``refused``. Anything else, or a row not
        ``unknown``, raises (the trigger)."""
        if status not in ("ok", "failed", "refused"):
            raise ValueError("an attempt settles as ok, failed or refused")
        with write_transaction(self._connect) as conn:
            conn.execute("UPDATE effect_attempts SET status = ?, settled_at = ? WHERE attempt_id = ? "
                         "AND status = 'unknown'", (status, now, attempt_id))

    def note_refusal(self, order_id: str, verb: str, event_id: str, reason: str, now: str) -> None:
        """Record that a call was refused, and why (a fixed sentence): one row per (order, verb, event, reason), counted.
        Best effort and bounded: a fault here never changes the refusal, and past :data:`_REFUSALS_MAX` rows a new
        combination is not kept."""
        try:
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
            with write_transaction(self._connect) as conn:
                hit = conn.execute("UPDATE effect_refusals SET times = times + 1, last_at = ? WHERE order_id = ? AND "
                                   "verb = ? AND event_id = ? AND reason_sha256 = ?",
                                   (now, order_id, verb, event_id, digest)).rowcount
                if not hit and conn.execute("SELECT count(*) FROM effect_refusals").fetchone()[0] < _REFUSALS_MAX:
                    conn.execute("INSERT INTO effect_refusals VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                                 (order_id, verb, event_id, digest, reason, now, now))
        except Exception:                                   # noqa: BLE001 - the record never changes the refusal
            pass

    def refusals(self) -> list[tuple[str, str, str, str, int]]:
        with read_transaction(self._connect) as conn:
            return [tuple(r) for r in conn.execute("SELECT order_id, verb, event_id, reason, times FROM effect_refusals "
                                                   "ORDER BY first_at, reason")]
