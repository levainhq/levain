"""levain.agents.accounting: launch-time counters, reservations and the executor contract (A0 r6 §2.4, §2.6).

Every executor reports through this one module: the :class:`Usage` it leaves, the :class:`Executor` it implements and
the frozen :class:`StopResult` its ``stop()`` returns. :func:`reserve` and :func:`settle` take the CALLER's connection
and open no transaction of their own, so the launch and terminal transactions (A0 §2.4's table) run them inside
the one ``BEGIN IMMEDIATE`` that also writes the occurrence's state and the slot.

Counts are derived from ``agent_occurrences`` and ``agent_reservations`` at the moment of the check; there is no counter
table. Money is integer micro-dollars. Nothing here reads a clock or the signed link: the caller (the launch) reads the
link's signed fields and the window's bounds and hands them in as :class:`Limits`.

Inside a ``BEGIN IMMEDIATE`` the write lock is the serialization: a second launch's check runs after the first has
committed, so two launches that contend at a ceiling cannot both pass it.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["Usage", "StopResult", "Executor", "Limits", "Reserved", "Refused", "Settled", "reserve", "settle",
           "open_reservations", "WINDOW_KEY_FORMS"]

# the three forms of ``agent_occurrences.window_key`` (A0 §2.6); the first two are scheduled, the third is manual
WINDOW_KEY_FORMS = ("w:<zone date of window start>T<HH:MM>", "d:<YYYY-MM-DD in the agent's zone>", "m:<YYYY-MM-DD>")


@dataclass(frozen=True)
class Usage:
    """What a run spent. ``usd_micros`` is ``None`` when the executor could not read it: the whole reservation is
    then debited (fail-closed). ``source`` names where the figure came from (e.g. ``dream:ollama``)."""
    usd_micros: int | None
    input_tokens: int | None
    output_tokens: int | None
    source: str


@dataclass(frozen=True)
class StopResult:
    """A stop is a command; this is the fact. ``terminated`` is True only when the executor itself observed the
    process end, or found it already ended; it is False whenever the executor cannot tell. ``detail`` says what it
    observed, for the attention row raised while ``terminated`` is False."""
    terminated: bool
    detail: str


@runtime_checkable
class Executor(Protocol):
    """What a run is launched through. Two obligations beyond the signatures:

    * ``max_exposure_usd_micros`` is a BOUND THE EXECUTOR'S OWN CODE ENFORCES, never a sampled margin. An executor
      whose spend control is checked only after a step states that step's enforced ceiling inside the figure. A
      figure that is a measurement is for a first run, not for a reservation.
    * every outward act of the run goes through ``RunJournal.effect`` with the agent ``fence_now``, or through the
      per-call predicate of the session executor. An executor that cannot route its acts so is not an Executor.

    ``usage()`` is read at the end and never raises (unreadable is ``Usage(usd_micros=None, ...)``).
    ``stop(deadline_s)`` is the supervisor's call: idempotent, waits at most ``deadline_s``, never raises, and
    returns ``StopResult(terminated=False, ...)`` whenever it cannot observe the process end. The supervisor settles a
    row only on ``terminated=True``, so an executor that cannot observe its process holds its slot until the run ends
    on its own: closed for the cap, never for authority.
    """
    max_exposure_usd_micros: int

    def usage(self) -> Usage: ...

    def stop(self, deadline_s: float) -> StopResult: ...


@dataclass(frozen=True)
class Limits:
    """What the launch check is held to, read by the caller from the signed link and the schedule.

    ``run_ceiling`` and ``budget_window_usd_micros`` are the link's signed fields. ``scheduled_window_key`` is read
    for a MANUAL launch only: the window that holds now, or None when none does. A scheduled launch always counts its
    own occurrence's window key, whatever is passed here. ``span_start``/``span_end`` bound, as UTC ISO text with
    start inclusive and end exclusive, the manual runs that count (the window for a scheduled launch, today in the
    agent's zone for a manual one). One signed limit covers both kinds: no second allowance for manual runs.
    """
    run_ceiling: int
    budget_window_usd_micros: int
    scheduled_window_key: str | None
    span_start: str
    span_end: str


@dataclass(frozen=True)
class Reserved:
    reserved_usd_micros: int


@dataclass(frozen=True)
class Refused:
    """The launch is refused and nothing was taken. The occurrence's terminal state is ``skipped-budget`` (A0 §2.5)
    and ``reason`` is one of ``no_exposure``, ``ceiling``, ``budget_window`` or ``already_reserved``."""
    reason: str
    state: str = "skipped-budget"


@dataclass(frozen=True)
class Settled:
    """``debited_usd_micros`` is what the window is charged. ``breach`` is True when the actual passed the reservation:
    the caller ends the row ``failed`` with ``reason=budget_breach`` (the agent is already paused). ``basis`` is
    ``actual``, ``unreadable``, ``breach``, ``already_settled`` or ``no_reservation``."""
    debited_usd_micros: int
    breach: bool
    basis: str


def _field(occurrence: Mapping | sqlite3.Row, name: str) -> object:
    return occurrence[name]


def _is_amount(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# the largest amount a SQLite INTEGER holds: an exposure above it is refused, an actual above it is debited at it
_MAX_MICROS = 2**63 - 1

# the runs of one agent that count against its signed limits: launched (a reservation exists, or launched_at is set),
# scheduled ones by their window key, manual ones by when they launched. Times compare as instants (julianday), so a
# stamp written with an offset or fractional seconds is placed where it happened, not where its text sorts.
# A manual run is left out only when it is PROVABLY outside the span: julianday() is NULL for a stamp (or bound) it
# cannot parse, the comparison is then NULL, COALESCE makes it 0, and the run counts (fail-closed, never dropped).
# julianday is a double (~ms resolution): a stamp within about a millisecond of a bound may land on either side.
# Per A0 §2.6 a manual launch with no window holding now (:swk NULL) counts manual runs only: that is the contract's
# basis, not an omission here; a scheduled launch's :swk is its own window key, read from the occurrence (reserve).
_COUNTED = ("FROM agent_occurrences o WHERE o.agent_id = :agent AND o.occurrence_id != :me"
            " AND (o.launched_at IS NOT NULL OR EXISTS (SELECT 1 FROM agent_reservations r"
            " WHERE r.occurrence_id = o.occurrence_id))"
            " AND ((o.kind = 'scheduled' AND o.window_key = :swk)"
            " OR (o.kind = 'manual'"
            " AND COALESCE(julianday(COALESCE(o.launched_at, o.admitted_at)) < julianday(:start), 0) = 0"
            " AND COALESCE(julianday(COALESCE(o.launched_at, o.admitted_at)) >= julianday(:end), 0) = 0))")


def reserve(conn: sqlite3.Connection, occurrence: Mapping | sqlite3.Row, executor: Executor, *,
            limits: Limits) -> Reserved | Refused:
    """Take the hard maximum exposure for ``occurrence``, in the caller's open write transaction.

    Refused (fail-closed, nothing written) when the executor states no non-negative integer exposure, when the
    agent's launched runs in the window already meet the signed ceiling, or when the window's spend, its open
    reservations and this exposure would pass the signed window budget. Spend is what settled reservations debited
    plus what open ones hold, over the same runs the ceiling counts.

    Raises ``RuntimeError`` when ``conn`` is not inside a transaction. Its first statement takes the write lock (a
    no-op write), so the counts below are read under it even in a deferred ``BEGIN``; a deferred transaction that
    already read a snapshot another writer has since moved past fails there with ``database is locked``.
    """
    if not conn.in_transaction:
        raise RuntimeError("reserve runs inside the launch transaction (BEGIN IMMEDIATE); this connection has none")
    conn.execute("UPDATE agent_reservations SET state = state WHERE 0")
    me = str(_field(occurrence, "occurrence_id"))
    agent = str(_field(occurrence, "agent_id"))
    exposure = getattr(executor, "max_exposure_usd_micros", None)
    if not _is_amount(exposure) or exposure > _MAX_MICROS:
        return Refused("no_exposure")
    # a reservation commits only with its launch (one transaction), so a reserved occurrence is launched, and the
    # transition trigger refuses launched -> skipped-budget: a caller applying this Refused.state cannot end a live row
    if conn.execute("SELECT 1 FROM agent_reservations WHERE occurrence_id = ?", (me,)).fetchone() is not None:
        return Refused("already_reserved")
    # a scheduled launch counts its OWN window (A0 §2.6), so the key comes from the occurrence and no caller value
    # (a None included) can switch scheduled counting off; Limits.scheduled_window_key is read for a manual launch only
    if _field(occurrence, "kind") == "scheduled":
        swk = _field(occurrence, "window_key")
    else:
        swk = limits.scheduled_window_key
    args = {"agent": agent, "me": me, "swk": swk, "start": limits.span_start, "end": limits.span_end}
    launched = conn.execute(f"SELECT COUNT(*) {_COUNTED}", args).fetchone()[0]
    if launched >= limits.run_ceiling:
        return Refused("ceiling")
    # TOTAL, not SUM: SUM raises "integer overflow" once breaches past 2**63 accumulate; TOTAL is exact below 2**53,
    # and a signed window budget is at most 10**12 (authority._AGENT_INT_MAX), so rounding cannot move this check
    held = conn.execute(
        "SELECT TOTAL(CASE WHEN r.state = 'open' THEN r.reserved_usd_micros"
        " ELSE COALESCE(r.debited_usd_micros, r.reserved_usd_micros) END)"
        f" FROM agent_reservations r WHERE r.occurrence_id IN (SELECT o.occurrence_id {_COUNTED})", args).fetchone()[0]
    if held + exposure > limits.budget_window_usd_micros:
        return Refused("budget_window")
    conn.execute("INSERT INTO agent_reservations (occurrence_id, agent_id, window_key, reserved_usd_micros, state)"
                 " VALUES (?, ?, ?, ?, 'open')", (me, agent, _field(occurrence, "window_key"), exposure))
    return Reserved(exposure)


def settle(conn: sqlite3.Connection, occurrence: Mapping | sqlite3.Row, usage: Usage) -> Settled:
    """Close ``occurrence``'s reservation, in the caller's open write transaction, and report what was debited.

    Readable usage within the reservation debits the actual and releases the rest. Unreadable usage (``None``, or
    anything that is not a non-negative integer) debits the whole reservation. An actual above the reservation is
    debited in full (saturated at 2**63-1, the most a SQLite INTEGER holds) and is a breach: the agent is set
    ``paused`` here, in the same transaction, and the caller ends the row ``failed``. Settling twice changes nothing:
    the second call reports ``already_settled``.

    Raises ``RuntimeError`` when ``conn`` is not inside a transaction, and takes the write lock (a no-op write) before
    it reads the reservation, as :func:`reserve` does: two settlements cannot both read ``open`` and both report.
    """
    if not conn.in_transaction:
        raise RuntimeError("settle runs inside the terminal transaction (BEGIN IMMEDIATE); this connection has none")
    conn.execute("UPDATE agent_reservations SET state = state WHERE 0")
    me = str(_field(occurrence, "occurrence_id"))
    row = conn.execute("SELECT agent_id, reserved_usd_micros, debited_usd_micros, state FROM agent_reservations"
                       " WHERE occurrence_id = ?", (me,)).fetchone()
    if row is None:    # a launch writes its reservation in its own transaction, so only a never-launched row lands here
        return Settled(0, False, "no_reservation")
    agent, reserved, debited, state = row[0], row[1], row[2], row[3]
    if state != "open":    # the CHECK allows only open/settled; anything not open is not ours to settle again
        return Settled(int(debited or 0), False, "already_settled")
    actual = getattr(usage, "usd_micros", None)
    if not _is_amount(actual):
        charge, basis, breach = reserved, "unreadable", False
    elif actual > reserved:    # an actual past what SQLite holds is debited at the most it holds: still a breach
        charge, basis, breach = min(actual, _MAX_MICROS), "breach", True
    else:
        charge, basis, breach = actual, "actual", False
    conn.execute("UPDATE agent_reservations SET state = 'settled', debited_usd_micros = ?, settled_reason = ?"
                 " WHERE occurrence_id = ? AND state = 'open'", (charge, basis, me))
    if breach:
        # operation is only active|paused (CHECK) and no levain code deletes an agent row, so this cannot demote
        # another state or miss a live agent (occurrences carry no FK to agents: an occurrence written for an agent row
        # that never existed would pause nothing here; the occurrence writer, not yet built, owns that invariant)
        conn.execute("UPDATE agents SET operation = 'paused' WHERE agent_id = ?", (agent,))
    return Settled(charge, breach, basis)


def open_reservations(conn: sqlite3.Connection) -> list[str]:
    """The occurrence ids whose reservation is still open: what recovery walks after a crash, settling each as
    unreadable unless the executor's usage record is readable by then."""
    return [r[0] for r in conn.execute(
        "SELECT occurrence_id FROM agent_reservations WHERE state = 'open' ORDER BY occurrence_id")]
