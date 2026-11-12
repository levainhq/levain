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
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

__all__ = ["Usage", "StopResult", "Executor", "ActionExposure", "SubscriptionExecutor", "FRONT_READ_BUFFER", "BILLINGS",
           "Limits", "Reserved", "Refused", "Settled", "reserve", "settle",
           "open_reservations", "WINDOW_KEY_FORMS", "CountSpan", "count_span"]

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
    # a subscription run's usage (P7-A §3): tool calls (or turns, for the dream), the front's byte total, wall-clock
    # seconds. None is unreadable. Ignored on an api_key route, and ``usd_micros`` is ignored on a subscription one.
    actions: int | None = None
    tunnel_bytes: int | None = None
    seconds: int | None = None


# the two billings a link carries (P7-A §2.2); a reservation row with no billing (written before /3) is an api_key one
BILLINGS = ("api_key", "subscription")
_UNITS = ("tool_calls", "turns")

# the bytes the gateway front reads at a time: a run's byte total can pass its cap by one read before the front stops
# the tunnel, so the breach line sits one buffer above the cap (P7-A §2.2's breach row). Intent: the front's chunk size.
FRONT_READ_BUFFER = 65536


@dataclass(frozen=True)
class ActionExposure:
    """The per-run caps a subscription run's enforcers hold (P7-A §4): ``actions`` (tool calls for a session, turns
    for the dream), ``seconds`` (the deadline, the spec's ``stop_at``) and ``tunnel_bytes`` (None only for an executor
    that grants no process-starting tool)."""
    actions: int
    unit: str
    seconds: int
    tunnel_bytes: int | None


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


@runtime_checkable
class SubscriptionExecutor(Executor, Protocol):
    """An Executor that can run on a subscription route: it states ``max_exposure_usd_micros = 0`` and the per-run
    caps its enforcers hold. A separate protocol rather than a member of :class:`Executor`, so an executor written for
    the API-key route stays an Executor."""
    action_exposure: ActionExposure | None


@dataclass(frozen=True)
class Limits:
    """What the launch check is held to, read by the caller from the signed link and the schedule.

    ``run_ceiling`` and ``budget_window_usd_micros`` are the link's signed fields. ``scheduled_window_key`` is read
    for a MANUAL launch only: the window that holds now, or None when none does. A scheduled launch always counts its
    own occurrence's window key, whatever is passed here. ``span_start``/``span_end`` bound, as UTC ISO text with
    start inclusive and end exclusive, the manual runs that count. All three come from ONE rule, :func:`count_span`
    of the launch's instant: a run launched inside a window counts against that window (a hand start at 23:30 or 00:10
    against that night, with the night's scheduled runs), a run launched outside every window against the daytime it
    falls in, untouched by any night's runs. One signed limit covers both kinds: no second allowance for manual runs.

    ``billing`` and the two per-run action caps are the link's (P7-A §2.2): an ``api_key`` link carries zero caps and a
    ``subscription`` link carries positive ones and zero dollar budgets. The defaults are the shipped link's reading.

    ``budget_run_usd_micros`` is the link's signed per-run dollar figure, the most ONE run's exposure may be on an
    ``api_key`` link. Its default is 0 on purpose: a caller that does not read the signed figure refuses every positive
    dollar exposure rather than leaving the figure he signed unenforced. A subscription link signs 0 and reserves no dollars.
    """
    run_ceiling: int
    budget_window_usd_micros: int
    scheduled_window_key: str | None
    span_start: str
    span_end: str
    billing: str = "api_key"
    budget_run_calls: int = 0
    budget_run_tunnel_bytes: int = 0
    budget_run_usd_micros: int = 0


@dataclass(frozen=True)
class CountSpan:
    """The one allowance an instant counts against (:func:`count_span`): runs launched in ``[start, end)`` and the
    scheduled runs keyed ``window_key`` share one signed ceiling and one window budget. ``window_key`` is the scheduled
    window the instant falls inside, None for the daytime between two windows (no scheduled run is keyed there)."""
    window_key: str | None
    start: datetime
    end: datetime

    @property
    def span_start(self) -> str:
        return self.start.astimezone(timezone.utc).strftime(_STAMP)

    @property
    def span_end(self) -> str:
        return self.end.astimezone(timezone.utc).strftime(_STAMP)


_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def count_span(window: Mapping[str, str] | None, zone: str, at: datetime) -> CountSpan:
    """THE counting rule (Phill's ruling np-8ec243e5 (a), 11-12): a run counts against the window it launches inside,
    a scheduled one and a hand start alike, and a run launched outside every window counts against the daytime it falls
    in, from the last window's end to the next window's start, so a night's runs never count against a day or the other
    way round. ``window`` is the schedule's ``{"start": "HH:MM", "end": "HH:MM"}`` in ``zone``; None (or a start equal
    to its end) is the whole calendar day, keyed ``d:<date>`` here (the dream's own window never takes this branch).

    A window and its span come from :func:`levain.agents.schedule.window_key` and ``window_span``, the functions the
    scheduled slots are keyed and launched by, so the night a hand start counts against is the very night a scheduled
    slot counts. A pure function of ``at``: it reads no clock. Local times are built per date in ``zone``, so a night
    the clocks change in keeps its 22:00 and 05:00 ends and its true length."""
    from levain.agents import schedule

    if at.tzinfo is None:
        raise ValueError("a counted instant must be timezone-aware")
    trigger = {"window": dict(window) if window is not None else None}
    z = ZoneInfo(zone)
    local = at.astimezone(z)
    whole_day = window is None or window["start"] == window["end"]
    if whole_day or schedule._in_window(trigger["window"], local):
        key = schedule.window_key(trigger, zone, at)
        lo, hi = schedule.window_span(trigger, zone, key)
        return CountSpan(key, lo, hi)
    start, end = schedule._hhmm(window["start"]), schedule._hhmm(window["end"])
    day = datetime(local.year, local.month, local.day)
    if start > end:                       # a window across midnight: the daytime is end..start of the same date
        lo_day, hi_day = day, day
    elif local.hour * 60 + local.minute < start:   # before today's window: since yesterday's ended
        lo_day, hi_day = day - timedelta(days=1), day
    else:                                 # after today's window: until tomorrow's opens
        lo_day, hi_day = day, day + timedelta(days=1)
    lo = (lo_day + timedelta(minutes=end)).replace(tzinfo=z)
    hi = (hi_day + timedelta(minutes=start)).replace(tzinfo=z)
    return CountSpan(None, lo.astimezone(timezone.utc), hi.astimezone(timezone.utc))


@dataclass(frozen=True)
class Reserved:
    reserved_usd_micros: int


@dataclass(frozen=True)
class Refused:
    """The launch is refused and nothing was taken. The occurrence's terminal state is ``skipped-budget`` (A0 §2.5)
    and ``reason`` is one of ``no_exposure``, ``run_budget``, ``ceiling``, ``budget_window`` or ``already_reserved``."""
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


def _is_positive(value: object) -> bool:
    return _is_amount(value) and value > 0  # type: ignore[operator]


def _is_stored(value: object) -> bool:
    """A positive integer a SQLite INTEGER column holds (binding a larger one raises ``OverflowError``)."""
    return _is_positive(value) and value <= _MAX_MICROS  # type: ignore[operator]


def _subscription_exposure(executor: object, limits: Limits) -> ActionExposure | None:
    """The executor's ``ActionExposure`` when it is a valid subscription statement, else None (refused fail-closed):
    no non-zero dollar exposure, an :class:`ActionExposure` whose ``actions`` and ``seconds`` are positive integers,
    whose ``unit`` is a known one, whose ``actions`` and any ``tunnel_bytes`` are within the link's per-run caps, and
    whose every stored field fits a SQLite INTEGER. ``seconds`` has no link cap to sit under (A0 r8: the wall-clock cap
    is the spec's ``stop_at``, signed through ``spec_hash``, not a link field), so the INTEGER bound is its only one."""
    dollars = getattr(executor, "max_exposure_usd_micros", None)
    if type(dollars) is not int or dollars != 0:
        return None
    exp = getattr(executor, "action_exposure", None)
    if not isinstance(exp, ActionExposure) or type(exp.unit) is not str or exp.unit not in _UNITS:
        return None
    if not _is_stored(exp.actions) or not _is_stored(exp.seconds):
        return None
    if not _is_stored(limits.budget_run_calls) or exp.actions > limits.budget_run_calls:
        return None
    if exp.tunnel_bytes is not None and (not _is_stored(exp.tunnel_bytes)
                                         or not _is_amount(limits.budget_run_tunnel_bytes)
                                         or exp.tunnel_bytes > limits.budget_run_tunnel_bytes):
        return None
    return exp


# the largest amount a SQLite INTEGER holds: an exposure above it is refused, an actual above it is debited at it
_MAX_MICROS = 2**63 - 1

# the runs of one agent that count against its signed limits: launched (a reservation exists, or launched_at is set),
# scheduled ones by their window key, manual ones by when they launched. Times compare as instants (julianday), so a
# stamp written with an offset or fractional seconds is placed where it happened, not where its text sorts.
# A manual run is left out only when it is PROVABLY outside the span: julianday() is NULL for a stamp (or bound) it
# cannot parse, the comparison is then NULL, COALESCE makes it 0, and the run counts (fail-closed, never dropped).
# julianday is a double (~ms resolution): a stamp within about a millisecond of a bound may land on either side.
# A manual launch outside every window (:swk NULL, the daytime count_span names) counts manual runs only: no scheduled
# run is keyed in a daytime span; a scheduled launch's :swk is its own window key, read from the occurrence (reserve).
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
    agent's launched runs in the window already meet the signed ceiling, when an ``api_key`` exposure is above the link's
    signed per-run figure, or when the window's spend, its open
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
    if limits.billing not in BILLINGS:
        return Refused("no_exposure")
    subscription = limits.billing == "subscription"
    if subscription:
        # a subscription run reserves no dollars (Levain's keys never charge it): the dollar window is untouched
        action = _subscription_exposure(executor, limits)
        if action is None:
            return Refused("no_exposure")
        exposure = 0
    else:
        action = None
        exposure = getattr(executor, "max_exposure_usd_micros", None)
        if not _is_amount(exposure) or exposure > _MAX_MICROS:
            return Refused("no_exposure")
    # a reservation commits only with its launch (one transaction), so a reserved occurrence is launched, and the
    # transition trigger refuses launched -> skipped-budget: a caller applying this Refused.state cannot end a live row
    if conn.execute("SELECT 1 FROM agent_reservations WHERE occurrence_id = ?", (me,)).fetchone() is not None:
        return Refused("already_reserved")
    if not subscription and (not _is_amount(limits.budget_run_usd_micros) or exposure > limits.budget_run_usd_micros):
        return Refused("run_budget")
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
    if not subscription and held + exposure > limits.budget_window_usd_micros:
        return Refused("budget_window")
    if action is None:
        conn.execute("INSERT INTO agent_reservations (occurrence_id, agent_id, window_key, reserved_usd_micros, state)"
                     " VALUES (?, ?, ?, ?, 'open')", (me, agent, _field(occurrence, "window_key"), exposure))
    else:
        conn.execute("INSERT INTO agent_reservations (occurrence_id, agent_id, window_key, reserved_usd_micros, state,"
                     " billing, cap_actions, cap_tunnel_bytes, cap_seconds) VALUES (?, ?, ?, 0, 'open',"
                     " 'subscription', ?, ?, ?)", (me, agent, _field(occurrence, "window_key"), action.actions,
                                                   action.tunnel_bytes, action.seconds))
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
    row = conn.execute("SELECT agent_id, reserved_usd_micros, debited_usd_micros, state, billing, cap_actions,"
                       " cap_tunnel_bytes, cap_seconds FROM agent_reservations WHERE occurrence_id = ?",
                       (me,)).fetchone()
    if row is None:    # a launch writes its reservation in its own transaction, so only a never-launched row lands here
        return Settled(0, False, "no_reservation")
    agent, reserved, debited, state = row[0], row[1], row[2], row[3]
    if state != "open":    # the CHECK allows only open/settled; anything not open is not ours to settle again
        return Settled(int(debited or 0), False, "already_settled")
    if row[4] == "subscription":
        return _settle_subscription(conn, me, agent, row[5], row[6], row[7], usage)
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


def _settle_subscription(conn: sqlite3.Connection, me: str, agent: str, cap_actions: int | None,
                         cap_bytes: int | None, cap_seconds: int | None, usage: Usage) -> Settled:
    """Settle a subscription reservation (P7-A §2.2's rows): no dollars are debited whatever ``usd_micros`` says. A
    count above its cap (actions above ``cap_actions``, bytes above ``cap_tunnel_bytes`` plus one front read buffer)
    is a breach: the actual is recorded, the agent is paused in this transaction and the caller ends the row
    ``failed``. Each axis is judged on its own: a known breach on one stays a breach whatever the other reads. An
    unreadable count records its own cap as used (A0 r8's unreadable row) and, with no breach elsewhere, the basis is
    ``unreadable``. Seconds are recorded (the cap when unreadable) and judged by neither rule.

    A run with no byte cap (``cap_tunnel_bytes`` NULL) cannot breach on bytes. A0 r8 allows a None ``tunnel_bytes``
    only for an executor that grants no process-starting tool (§2.6; P7-A §5: nothing it does escapes the action
    limit), so such a run has no front, and the breach row's "bytes above max_tunnel_bytes plus one front read buffer"
    has no cap and no buffer to measure against. A byte total it reports anyway is recorded as evidence, never judged
    against an invented cap; that the run started no process is the executor's grant to keep, not this count's."""
    actions = getattr(usage, "actions", None)
    nbytes = getattr(usage, "tunnel_bytes", None)
    seconds = getattr(usage, "seconds", None)
    cap_a = int(cap_actions or 0)
    cap_b = None if cap_bytes is None else int(cap_bytes)
    a_read, b_read = _is_amount(actions), _is_amount(nbytes)
    used_a = min(actions, _MAX_MICROS) if a_read else cap_a  # type: ignore[type-var]
    used_b = min(nbytes, _MAX_MICROS) if b_read else cap_b  # type: ignore[type-var]
    used_s = min(seconds, _MAX_MICROS) if _is_amount(seconds) else cap_seconds  # type: ignore[type-var]
    breach = (a_read and actions > cap_a) or (  # type: ignore[operator]
        cap_b is not None and b_read and nbytes > cap_b + FRONT_READ_BUFFER)  # type: ignore[operator]
    if breach:
        basis = "breach"
    elif not a_read or (cap_b is not None and not b_read):
        basis = "unreadable"
    else:
        basis = "actual"
    conn.execute("UPDATE agent_reservations SET state = 'settled', debited_usd_micros = 0, settled_reason = ?,"
                 " used_actions = ?, used_tunnel_bytes = ?, used_seconds = ? WHERE occurrence_id = ? AND state = 'open'",
                 (basis, used_a, used_b, used_s, me))
    if breach:
        conn.execute("UPDATE agents SET operation = 'paused' WHERE agent_id = ?", (agent,))
    return Settled(0, breach, basis)


def open_reservations(conn: sqlite3.Connection) -> list[str]:
    """The occurrence ids whose reservation is still open: what recovery walks after a crash, settling each as
    unreadable unless the executor's usage record is readable by then."""
    return [r[0] for r in conn.execute(
        "SELECT occurrence_id FROM agent_reservations WHERE state = 'open' ORDER BY occurrence_id")]
