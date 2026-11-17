"""levain.desk.effect_attempts: the night desk's effect journal (ND-3b, board levain#12; night desk design 2.6, 8.1).

TRUST-BOUNDARY CODE, beside :mod:`levain.desk.effects`, the only caller. One file (``effect_attempts.db``), its own
tables: NOT the autonomic journal and not the coord store (this row owns neither).

One ATTEMPT row is the reservation for one effect on one event, whichever order asks: its idempotence key is the digest
of ``event id | verb | args digest`` (:func:`idem_key`), ``UNIQUE``, so a second reservation for the same effect on the
same event, under the same order or another, never makes a second row (one effect per event across all orders; a
double ack or a double restart is the worse failure). The row is written, and committed durably
(``synchronous = FULL``), BEFORE the effect runs, and is
written as ``unknown``: a process that dies after the reservation and before the result leaves exactly that row, and
nothing has to run for it to read ``unknown``. The result is a later, single transition out of ``unknown``: to ``ok`` or
``failed`` (the effect was settled), or to ``refused`` (the order stopped answering between the reservation and the run:
nothing ran). A retry of an ``ok``, ``failed`` or ``unknown`` attempt is answered with its recorded status and runs
nothing; only a ``refused`` row can be taken again (``refused`` to ``unknown``), and then it is a fresh reservation,
counted and charged in the incident and night it is taken in, under the order that takes it (that edge alone may move
the row's scopes, order id, version, incarnation and limits).

BUDGETS are checked and consumed in the same ``BEGIN IMMEDIATE`` transaction that inserts the reservation (never
check-then-act): the count of the verb's rows holding the same incident scope, or the same night scope, that are not
``refused``, under ANY order, must be under the calling order line's budget AND under the lowest budget any of those
rows was reserved under (each row keeps its line's limits), or the reservation is not written. The budget bounds the
box and the address, not the order (night desk design 3.1: "2 restarts per night across all incidents"; 3.2: "1 send
per address per night"), so a second order, or a widening, cannot double it, and a looser order cannot lift the
ceiling a stricter order's effects were held to. A row consumes budget whatever its outcome (a failed or an unknown
attempt may have done its work).

The triggers refuse DELETE, an UPDATE that changes any identity column (the scopes, order id, version, incarnation and
limits too, except on the retake edge), and every status edge but the ones above.
Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction

__all__ = ["Attempt", "BudgetSpent", "JOURNAL_FILE", "Journal", "ReadOnlyJournal", "STATUSES", "idem_key", "night_key"]

JOURNAL_FILE = "effect_attempts.db"     # the one name of the journal file inside a store directory

STATUSES = ("unknown", "ok", "failed", "refused")
SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS effect_attempts (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "idem_key TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL, order_sha256 TEXT NOT NULL, incarnation INTEGER NOT NULL, "
    "verb TEXT NOT NULL, event_id TEXT NOT NULL, args_digest TEXT NOT NULL, recipient TEXT, "
    "incident_scope TEXT NOT NULL, night_scope TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK (status IN ('unknown', 'ok', 'failed', 'refused')), "
    "reserved_at TEXT NOT NULL, settled_at TEXT, per_incident INTEGER, per_night INTEGER)",
    "DROP INDEX IF EXISTS effect_attempts_incident",
    "DROP INDEX IF EXISTS effect_attempts_night",
    "CREATE INDEX IF NOT EXISTS effect_attempts_verb_incident ON effect_attempts (verb, incident_scope)",
    "CREATE INDEX IF NOT EXISTS effect_attempts_verb_night ON effect_attempts (verb, night_scope)",
    "CREATE TRIGGER IF NOT EXISTS effect_attempts_no_delete BEFORE DELETE ON effect_attempts "
    "BEGIN SELECT RAISE(ABORT, 'an effect attempt is never deleted'); END",
    # dropped and created again in the schema's one transaction, so a file made before this version of the edges gets
    # this trigger and never keeps the older one
    "DROP TRIGGER IF EXISTS effect_attempts_edges",
    "CREATE TRIGGER effect_attempts_edges BEFORE UPDATE ON effect_attempts WHEN "
    "NEW.attempt_id IS NOT OLD.attempt_id OR NEW.idem_key IS NOT OLD.idem_key OR "
    "NEW.verb IS NOT OLD.verb OR NEW.event_id IS NOT OLD.event_id OR NEW.args_digest IS NOT OLD.args_digest OR "
    "((NEW.incident_scope IS NOT OLD.incident_scope OR NEW.night_scope IS NOT OLD.night_scope OR "
    "NEW.order_id IS NOT OLD.order_id OR NEW.order_sha256 IS NOT OLD.order_sha256 OR "
    "NEW.incarnation IS NOT OLD.incarnation OR NEW.per_incident IS NOT OLD.per_incident OR "
    "NEW.per_night IS NOT OLD.per_night) "
    "AND NOT (OLD.status = 'refused' AND NEW.status = 'unknown')) OR "
    "NEW.recipient IS NOT OLD.recipient OR NOT ((OLD.status = 'unknown' AND NEW.status IN ('ok', 'failed', 'refused')) "
    "OR (OLD.status = 'refused' AND NEW.status = 'unknown')) "
    "BEGIN SELECT RAISE(ABORT, 'an effect attempt changes only along its edges'); END",
    "CREATE TABLE IF NOT EXISTS effect_refusals (order_id TEXT NOT NULL, verb TEXT NOT NULL, event_id TEXT NOT NULL, "
    "reason_sha256 TEXT NOT NULL, reason TEXT NOT NULL, times INTEGER NOT NULL, first_at TEXT NOT NULL, "
    "last_at TEXT NOT NULL, PRIMARY KEY (order_id, verb, event_id, reason_sha256))",
)
#: the most refusal rows kept; past it the least recently seen row is dropped for the new one, so a caller varying the
#: event id can neither grow the file without bound nor stop a later refusal being recorded
_REFUSALS_MAX = 5000
#: the budget each row was reserved under (the calling line's), kept so a later reservation is held to the strictest
_LIMIT_COLUMNS = ("per_incident", "per_night")


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
    """The idempotence key: event id, effect and the digest of its arguments (length-framed, so no two triples join).
    No order id: one effect per event, whichever order asks."""
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


class ReadOnlyJournal:
    """The attempts file at ``path`` read and nothing else: opened ``mode=ro`` (never created, no schema step, no
    migration, no write lock, no journal-mode change), so a caller that must not touch a live night journal can read
    it. Reads through the same column list and row mapping as :class:`Journal`. Nothing is opened until :meth:`rows`,
    and any fault (a missing or wrong-type file, a corrupt file, a table without a selected column) is raised there."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _uri(self) -> str:
        # An idle file (no ``-wal``, ``-shm`` or ``-journal`` beside it: a live WAL writer keeps the first two, a rollback-mode
        # writer's hot journal is the third) is opened ``mode=ro`` plus ``immutable`` (``immutable`` alone would create a
        # missing file), so reading it makes no sidecar; one with a sidecar is read ``mode=ro`` through the existing ones.
        idle = not any(os.path.lexists(f"{self.path}{s}") for s in ("-wal", "-shm", "-journal"))
        return f"file:{quote(str(self.path))}?mode=ro{'&immutable=1' if idle else ''}"

    def rows(self) -> list[Attempt]:
        conn = sqlite3.connect(self._uri(), uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA query_only = ON")
            return [_attempt(r) for r in conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts ORDER BY attempt_id")]
        finally:
            conn.close()


class Journal:
    """The attempts file at ``path`` (its directory must exist). Every method opens its own connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        with write_transaction(self._connect) as conn:
            conn.execute(SCHEMA[0])
            have = {r[1] for r in conn.execute("PRAGMA table_info(effect_attempts)")}
            for column in _LIMIT_COLUMNS:                   # a file made before the rows carried their limits
                if column not in have:
                    conn.execute(f"ALTER TABLE effect_attempts ADD COLUMN {column} INTEGER")
            for statement in SCHEMA[1:]:
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
        ``refused``, which is taken again as ``unknown`` in ``incident_scope`` and ``night_scope`` under ``order_id``),
        else a new ``unknown`` row. A new or re-taken reservation first counts the verb's non-refused rows, under every
        order, in its incident scope and in its night scope; a count at or over the budget (the lower of the caller's
        and the lowest a counted row was reserved under) raises :class:`BudgetSpent` and writes nothing.
        Returns ``(row, True)`` when THIS call holds the reservation (and so may run the effect), ``(row, False)`` when
        an earlier one does."""
        with write_transaction(self._connect) as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts WHERE idem_key = ?", (key,)).fetchone()
            if row is not None and row[8] != "refused":
                return _attempt(row), False
            # the limit is the calling line's or, when lower, the lowest any counted row was reserved under: a looser
            # order never lifts the ceiling a stricter order's effects in the same scope were held to
            for which, column, scope, limit in (("incident", "incident_scope", incident_scope, per_incident),
                                                ("night", "night_scope", night_scope, per_night)):
                used, lowest = conn.execute(
                    f"SELECT count(*), min(per_{which}) FROM effect_attempts WHERE verb = ? AND {column} = ? "
                    "AND status != 'refused'", (verb, scope)).fetchone()
                if used >= (limit if lowest is None else min(limit, lowest)):
                    raise BudgetSpent(which)
            if row is None:
                conn.execute("INSERT INTO effect_attempts (idem_key, order_id, order_sha256, incarnation, verb, "
                             "event_id, args_digest, recipient, incident_scope, night_scope, status, reserved_at, "
                             "per_incident, per_night) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?)",
                             (key, order_id, order_sha256, incarnation, verb, event_id, args_digest, recipient,
                              incident_scope, night_scope, now, per_incident, per_night))
            else:
                # the scopes the budget was just counted in are the scopes the row is charged to (a retake after the
                # cut is the new night's effect; an event since grouped into an incident is that incident's), and the
                # order that takes it, with its line's limits, is the order it runs under
                conn.execute("UPDATE effect_attempts SET status = 'unknown', order_id = ?, order_sha256 = ?, "
                             "incarnation = ?, incident_scope = ?, night_scope = ?, per_incident = ?, per_night = ?, "
                             "reserved_at = ?, settled_at = NULL WHERE attempt_id = ?",
                             (order_id, order_sha256, incarnation, incident_scope, night_scope, per_incident,
                              per_night, now, row[0]))
            fresh = conn.execute(f"SELECT {_COLUMNS} FROM effect_attempts WHERE idem_key = ?", (key,)).fetchone()
        return _attempt(fresh), True

    def settle(self, attempt_id: int, status: str, now: str) -> bool:
        """The one transition out of ``unknown``: ``ok``, ``failed`` or ``refused`` (any other status raises
        ``ValueError``). Returns True when the row was ``unknown`` and is now settled; a row that is not ``unknown`` (already
        settled, or no such row) is left as it is and the answer is False: a second settle writes nothing."""
        if status not in ("ok", "failed", "refused"):
            raise ValueError("an attempt settles as ok, failed or refused")
        with write_transaction(self._connect) as conn:
            return conn.execute("UPDATE effect_attempts SET status = ?, settled_at = ? WHERE attempt_id = ? "
                                "AND status = 'unknown'", (status, now, attempt_id)).rowcount == 1

    def note_refusal(self, order_id: str, verb: str, event_id: str, reason: str, now: str) -> None:
        """Record that a call was refused, and why (a fixed sentence): one row per (order, verb, event, reason), counted.
        Best effort and bounded: a fault here never changes the refusal, and the table holds at most
        :data:`_REFUSALS_MAX` rows: a new combination past it drops the least recently seen rows, so the newest refusal
        is always kept."""
        try:
            digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()
            with write_transaction(self._connect) as conn:
                hit = conn.execute("UPDATE effect_refusals SET times = times + 1, last_at = ? WHERE order_id = ? AND "
                                   "verb = ? AND event_id = ? AND reason_sha256 = ?",
                                   (now, order_id, verb, event_id, digest)).rowcount
                if not hit:
                    over = conn.execute("SELECT count(*) FROM effect_refusals").fetchone()[0] - _REFUSALS_MAX + 1
                    if over > 0:
                        conn.execute("DELETE FROM effect_refusals WHERE rowid IN (SELECT rowid FROM effect_refusals "
                                     "ORDER BY last_at, rowid LIMIT ?)", (over,))
                    conn.execute("INSERT INTO effect_refusals VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                                 (order_id, verb, event_id, digest, reason, now, now))
        except Exception:                                   # noqa: BLE001 - the record never changes the refusal
            pass

    def refusals(self) -> list[tuple[str, str, str, str, int]]:
        with read_transaction(self._connect) as conn:
            return [tuple(r) for r in conn.execute("SELECT order_id, verb, event_id, reason, times FROM effect_refusals "
                                                   "ORDER BY first_at, reason")]
