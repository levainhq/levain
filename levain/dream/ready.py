"""levain.dream.ready (D1-1c): the day-ready predicate (D1_EXECUTOR §4.2).

``evaluate`` answers ``ready``, ``quiet`` or ``unreadable`` for one dream-day and records ALL six checks, whatever the
answer. Every source that gates is fail-closed: one that cannot be read is ``unreadable`` (never a pass), and
``unreadable`` is never a reason to launch. Check 5's seat telemetry is the one source that does not gate: it is read
fail-open, and what it could not read is named in its detail. A definite ``quiet`` outranks an ``unreadable`` (the
reason it names is a fact), and among several the lowest check number names the verdict; every check is still in
``checks``.

The predicate reads sources through zero-argument callables (:class:`Sources`), so a test (or the controller) injects
exactly what it means each source to say: a callable that raises means that source was UNREADABLE. :func:`sources_for`
builds the real ones from an install's directories. A directory that was not given is an ABSENT source (no occurrence
history, no desk, no agent store), recorded as such in the check's detail, so the verb can run against a bare
store copy; the controller passes every directory it owns.

The six checks, each mapped from flow's ``dream.py`` ``gate``:

1. no ``done`` occurrence for the window, and fewer than :data:`RUN_CEILING` launched (a backstop only)
2. no wrap open, and the baton held by this install's session (when the store requires the baton)
3. no wrap since the dream-day's start, apart from the one wrap each of this agent's ``done`` occurrences for OTHER
   dream-days made
4. something to consolidate
5. live seats are NOTED, never refused (D1-L1, D1_design section 3.4; PLAN D1's 10-30 amendment: the dream runs at
   the operator's time WITH seats live). The check still reads the three places a seat shows (a live desk launch, a
   work thread in ``awake.json``, another session in anneal's session registry, which a hooked Claude Code session
   heartbeats into) and names what it found in its detail, so ``levain dream ready`` can say who is live when it runs
   (nothing records who was live when a scheduled night ran: the card has no seat field and a ready admission's detail is
   not stored); it always passes, and a source it could not read is named in the detail, not turned into a refusal (nothing
   in the verdict rests on this list any more). When seats are live it also reads the baton policy, to name a store
   without it (an unreadable policy is named the same way). A manual run is no different
6. the window's one scheduled attempt is not already spent (a launched run that failed or was stopped, except the
   first run per window that lapse recovery ended before any fire claimed it: it never ran, and Phill ruled it does not
   spend the attempt). Waived for a manual run: the catch-up is the remedy for exactly that failure, and check 1's
   ceiling bounds it

**Why a live seat no longer needs a refusal (what carries the safety instead).**

* *The episode set is frozen at ``prepare_wrap``, never by a clock.* anneal stores the frozen list of episode ids
  (``wrap_episode_ids``) with the wrap; a seat's capture while the wrap is open is recorded with a later session id,
  so it is outside that set, survives the save, and is listed by ``Store.episodes_since_wrap()`` as the next wrap's
  input (a delay, never a loss). This module reads episodes that way and only that way: check 4 counts
  ``episodes_since_wrap()``, and nothing here compares an episode timestamp with a clock.
* *The dream's open wrap keeps a seat's own consolidate out, not the baton.* A Levain seat's anneal server runs under
  this install's session id (:data:`SESSION_ID`), the id the baton is held for, so the baton admits it. What stops it
  while the dream's wrap is open is that wrap: anneal refuses a second ``prepare_wrap`` (``WrapInProgressError``), and
  ``levain wrap`` is also stopped by ``wrap.lock``. Before the dream's ``prepare_wrap`` and after its save a seat may
  consolidate as it always could; checks 2 and 3 see that wrap.
* *The ``consolidate_requires_baton`` policy guards against other session ids only.* On a store with the policy,
  anneal downgrades a ``prepare_wrap`` under any other id, or none, to capture-only, and check 2 refuses a dream
  while the baton is anyone else's. A store created by ``levain init`` has the policy; a store without it is named in
  check 5's detail when seats are live, never refused on (for a Levain seat the policy changes nothing).
* *Check 2 still refuses an open wrap,* except one the executor's own age rule proves dead (D1-O, Phill's Q4 (a)). A
  seat that is itself mid-wrap is a wrap in progress, not a live seat. Admission calls the executor's age function
  (``levain.wrap._orphan_is_stale``, with the bound ``executor.DREAM_BUDGET_S``) and discards nothing: the executor
  needs ``wrap.lock`` and stays the only judge that acts. A wrap the dream's own occurrence recorded (its
  ``progress.json`` names the token) passes on that bound alone. A wrap the dream did not open, a seat's, passes only
  while the operator has set the age floor (``orphan_floor_s``, the install fact ``orphan_floor_hours``; it ships UNSET,
  and unset is the old behaviour: refuse), and only past both the bound and the floor, which can only raise the age.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote
from zoneinfo import ZoneInfo

READY = "ready"
QUIET = "quiet"
UNREADABLE = "unreadable"

#: the backstop on launched runs per window; check 6 is what holds the one-attempt rule
RUN_CEILING = 2
#: flow's ``DAY_START_HOUR``: the dream-day runs from 05:00 to 05:00
DAY_START_HOUR = 5
#: flow's ``MORNING_END_HOUR``: a manual run before this hour consolidates the PREVIOUS dream-day
MORNING_END_HOUR = 14
WINDOW = {"start": "22:00", "end": "05:00"}
#: clock skew allowed between an occurrence's launched..ended stamps and its wrap's ``wrapped_at``
CLOCK_SKEW_S = 5.0
#: anneal's session id for a Levain install (``levain.manifest.ANNEAL_SESSION_ID``), compared by value here so this
#: module imports nothing heavy; a test pins the two together
SESSION_ID = "levain"

_LAUNCHED_STATES = ("launched", "done", "failed", "stopped")
_SPENT_STATES = ("failed", "stopped")


class SourceUnreadable(Exception):
    """A source could not be read; the check that needed it is ``unreadable``."""


@dataclass(frozen=True)
class Check:
    n: int
    name: str
    status: str            # pass | quiet | unreadable | waived
    detail: str
    kind: str = ""         # for a quiet: already-dreamed, ceiling, in-flight, wrap-open, baton, consolidated-today,
    #                        nothing-to-consolidate, attempt-spent (never "live-seat": check 5 no longer answers quiet)

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.n, "name": self.name, "status": self.status, "detail": self.detail, "kind": self.kind}


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str
    check: int | None
    kind: str
    day: str
    window_key: str
    zone: str
    manual: bool
    checks: tuple[Check, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.verdict == READY

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "reason": self.reason, "check": self.check, "kind": self.kind, "day": self.day,
                "window_key": self.window_key, "zone": self.zone, "manual": self.manual,
                "checks": [c.as_dict() for c in self.checks]}


@dataclass
class Sources:
    """What the predicate reads. Every callable may raise: that source is then unreadable.

    ``occurrences`` returns the agent's occurrence rows (dicts with at least ``agent_id``, ``kind``, ``state``,
    ``window_key``, ``launched_at``, ``ended_at``); ``wraps`` returns ``(wrap id, wrapped_at)`` per completed wrap;
    ``launches`` / ``awake_threads`` / ``sessions`` return the ids that are live (``sessions`` already excludes this
    install's own session id)."""
    occurrences: Callable[[], Sequence[Mapping[str, Any]]]
    wrap_open: Callable[[], bool]
    requires_baton: Callable[[], bool]
    baton_holder: Callable[[], str | None]
    wraps: Callable[[], Sequence[tuple[int, str]]]
    episodes: Callable[[], int]
    launches: Callable[[], Sequence[str]]
    awake_threads: Callable[[], Sequence[str]]
    sessions: Callable[[], Sequence[str]]
    #: notes appended to the check details for sources that were not given (the verb's bare-copy mode)
    absent: tuple[str, ...] = ()
    #: D1-O. ``(token, started_at)`` of the wrap now open, read the way the executor reads it
    #: (``levain.wrap._observe_prior_wrap``); ``None`` (not given) means an open wrap can never be judged dead here
    open_wrap: Callable[[], tuple[str | None, str | None]] | None = None
    #: D1-O. ``{wrap token: occurrence id}`` of the wraps this agent's own occurrences recorded in their
    #: ``progress.json``: how a wrap is known to be the dream's own (a dead seat's is not in it)
    dream_wraps: Callable[[], Mapping[str, str]] | None = None


# ------------------------------------------------------------------------------------------------------- time helpers

def _when(text: Any) -> datetime:
    if isinstance(text, datetime):
        dt = text
    else:
        s = str(text)
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def dream_day_at(moment: datetime, zone: str) -> date:
    """The dream-day (05:00 to 05:00 in ``zone``) that ``moment`` falls in."""
    return (moment.astimezone(ZoneInfo(zone)) - timedelta(hours=DAY_START_HOUR)).date()


def day_start(day: date, zone: str) -> datetime:
    """The UTC instant dream-day ``day`` begins (05:00 local)."""
    return datetime(day.year, day.month, day.day, DAY_START_HOUR, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def window_key_for(day: date) -> str:
    """The scheduled window key of the window that opens at 22:00 on ``day`` (agents.schedule.window_key's form)."""
    return f"w:{day.isoformat()}T{WINDOW['start']}"


def window_holding(now: datetime, zone: str) -> str | None:
    """The scheduled window key holding ``now``, or None when ``now`` is outside 22:00-05:00 local."""
    local = now.astimezone(ZoneInfo(zone))
    if 5 <= local.hour < 22:
        return None
    opened = local.date() if local.hour >= 22 else local.date() - timedelta(days=1)
    return window_key_for(opened)


def manual_day_at(moment: datetime, zone: str) -> date:
    """The dream-day a manual run launched at ``moment`` consolidates (flow's ``--now`` fallback): the calendar day
    before when it launches before :data:`MORNING_END_HOUR` local, the calendar day itself after. Taken from the local
    calendar date, NOT from :func:`dream_day_at`, which has already moved the small hours back to the night."""
    local = moment.astimezone(ZoneInfo(zone))
    return local.date() - timedelta(days=1) if local.hour < MORNING_END_HOUR else local.date()


def occurrence_dream_day(row: Mapping[str, Any], zone: str) -> date:
    """The dream-day an occurrence consolidated (or would have). A scheduled one carries it in its window key. A
    manual one consolidates the PREVIOUS dream-day when it launched before 14:00 (flow's ``--now`` fallback) and the
    current one after. Raises :class:`SourceUnreadable` when the row cannot say."""
    if row.get("kind") == "scheduled":
        key = str(row.get("window_key") or "")
        if key.startswith("w:") and len(key) >= 12:
            try:
                return date.fromisoformat(key[2:12])
            except ValueError:
                pass
        raise SourceUnreadable(f"scheduled occurrence {row.get('occurrence_id')!r} has an unreadable window key {key!r}")
    if not row.get("launched_at"):
        raise SourceUnreadable(f"manual occurrence {row.get('occurrence_id')!r} has no launched_at")
    return manual_day_at(_when(row["launched_at"]), zone)


# --------------------------------------------------------------------------------------------------------- the checks

def _guard(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except SourceUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure to read is "unreadable", never a guess
        raise SourceUnreadable(f"{what}: {type(exc).__name__}: {exc}") from exc


def _c_unreadable(n: int, name: str, exc: Exception) -> Check:
    return Check(n, name, UNREADABLE, str(exc))


def _check1(src: Sources, agent: str, key: str, day: date, zone: str, now: datetime, manual: bool) -> Check:
    name = "not already dreamed"
    rows = [r for r in _guard(src.occurrences, "the occurrence table") if r.get("agent_id") == agent]
    for r in rows:
        if r.get("state") == "done" and (occurrence_dream_day(r, zone) == day):
            return Check(1, name, QUIET, f"dream-day {day} was already consolidated by {r.get('occurrence_id')}",
                         "already-dreamed")
    # ANY admitted or launched row of this agent is a dream in flight, whatever its window or kind: a manual run, a
    # scheduled run of the previous window and this window's own all overlap one consolidation. A row a crash left in
    # either state keeps the answer quiet until recovery reaps it, and the detail names the row.
    inflight = [r for r in rows if r.get("state") in ("admitted", "launched")]
    if inflight:
        return Check(1, name, QUIET, f"{inflight[0].get('occurrence_id')} is still in flight", "in-flight")
    launched = [r for r in rows if r.get("window_key") == key and r.get("state") in _LAUNCHED_STATES
                and r.get("launched_at")]
    if manual:
        # A0 §2.6: a manual launch counts the day's launched manual runs plus the scheduled runs of the window
        # that holds now; a run-now at 08:00 has no window holding it
        held = window_holding(now, zone)
        today = f"m:{now.astimezone(ZoneInfo(zone)).date().isoformat()}"
        launched = [r for r in rows if r.get("state") in _LAUNCHED_STATES and r.get("launched_at")
                    and ((r.get("kind") == "manual" and r.get("window_key") == today)
                         or (r.get("kind") == "scheduled" and held is not None and r.get("window_key") == held))]
    if len(launched) >= RUN_CEILING:
        return Check(1, name, QUIET, f"{len(launched)} runs already launched (the ceiling is {RUN_CEILING})", "ceiling")
    return Check(1, name, "pass", f"no done occurrence for dream-day {day}; {len(launched)} launched")


#: check 2's refusal of an open wrap while the operator's age floor is UNSET and the wrap is a seat's (or cannot be
#: judged): byte for byte what it said before D1-O (a test pins the string), because unset is the old behaviour.
OPEN_WRAP_REFUSAL = ("a wrap is open on the store: the dream does not start while one is open, a dead seat's included, "
                     "and nothing on this path clears it (anneal's wrap-status and wrap-cancel do)")
#: the head of every other refusal of an open wrap (D1-O). OPEN_WRAP_REFUSAL says "nothing on this path clears it", which
#: is true only while the floor is unset and the wrap is a seat's; once a floor is set, or the wrap is the dream's own, the
#: run clears a wrap that is old enough, so those refusals carry this head and then the reason.
OPEN_WRAP_HELD = ("a wrap is open on the store: the dream does not start while one is open that is not proven dead "
                  "(anneal's wrap-status and wrap-cancel clear one sooner, once you have confirmed it is dead)")


def executor_orphan_bound_s() -> float:
    """The bound the executor judges an orphan wrap against: ``max_seconds`` of the scheduled consolidate
    (``executor.DREAM_BUDGET_S``, the number ``fire_factory`` passes). Read from the executor, never copied."""
    from levain.dream import executor as ex

    return float(ex.DREAM_BUDGET_S)


def executor_orphan_horizon_s() -> float:
    """The age at which the executor's own rule (``levain.wrap._orphan_is_stale``) calls an open wrap dead: that bound
    plus the hard-exit grace plus the margin. The lowest age floor that can mean anything."""
    from levain import wrap as _w

    return float(_w._orphan_horizon(executor_orphan_bound_s()))


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:g} h"


def _held(why: str) -> tuple[bool, str]:
    """A refusal that is not the old one: the true head, then why this wrap is not proven dead."""
    return False, f"{OPEN_WRAP_HELD} | {why}"


def _orphan_proof(src: Sources, now: datetime, floor_s: float | None) -> tuple[bool, str]:
    """``(dead, text)``: may admission pass the open wrap (D1-O; Phill's Q4 (a)), and the words for either answer.
    ``text`` is the pass note when ``dead``, and otherwise the WHOLE refusal detail. Never raises: any failure at all,
    including one outside the readers below (the lazy import of the executor's bound), is "not proven dead", which is
    QUIET ``wrap-open``, never a class change to UNREADABLE.

    **Admission judges nothing of its own.** It calls the executor's age function (``levain.wrap._orphan_is_stale``,
    the one ``consolidate_store`` calls with the same bound) on the start time the executor itself reads
    (``_observe_prior_wrap``), and it discards nothing: the executor, which takes ``wrap.lock`` first, is the only code
    that acts, and it judges again when it runs. Two judges would drift.

    * a wrap the dream's own occurrence recorded (``progress.json`` names its token): dead when the executor's age
      function says so under the executor's own bound. No number of the operator's is needed.
    * a wrap the dream did NOT open (a seat's: its honest wrap can last longer than the dream's bound): dead only when
      the operator has set an age floor AND the same function says so with that floor, which can only raise the horizon.
      No floor set: never, and the refusal reads exactly :data:`OPEN_WRAP_REFUSAL`, as it always did (so does one whose
      age cannot be read while no floor is set).

    Every failure to judge (a source missing, a read that raised, a start time that will not parse) is "not dead"."""
    try:
        return _orphan_judge(src, now, floor_s)
    except Exception as exc:  # noqa: BLE001 — a bug or a failed read here is "not proven dead", never UNREADABLE
        if floor_s is None:
            return False, OPEN_WRAP_REFUSAL
        return _held(f"it could not be judged ({type(exc).__name__}: {str(exc)[:120]}), so it is not proven dead")


def _orphan_judge(src: Sources, now: datetime, floor_s: float | None) -> tuple[bool, str]:
    from levain import wrap as _w

    token: str | None = None
    started: str | None = None
    try:
        if src.open_wrap is None:
            raise LookupError("this reading cannot see the open wrap's age")
        token, started = src.open_wrap()
    except Exception as exc:  # noqa: BLE001 — a wrap that cannot be read is not proven dead
        if floor_s is None:
            return False, OPEN_WRAP_REFUSAL
        return _held(f"the open wrap could not be read ({type(exc).__name__}: {str(exc)[:120]}), so it is not proven dead")
    mine: Mapping[str, str] = {}
    if src.dream_wraps is not None:
        try:
            mine = src.dream_wraps()
        except Exception:  # noqa: BLE001 — no attribution is the stricter path
            mine = {}
    occ = mine.get(token) if token else None
    if occ is None and floor_s is None:
        return False, OPEN_WRAP_REFUSAL                      # a seat's wrap, no floor: the old answer, in the old words
    bound = executor_orphan_bound_s()
    horizon = executor_orphan_horizon_s()
    if occ is not None:
        if _w._orphan_is_stale(started, bound, now=now):
            # age alone: admission does not see whether that occurrence's fire still holds wrap.lock, so the note claims
            # the age and leaves the proof to the run, which takes the lock before it clears anything
            return True, (f"the dream's own wrap (occurrence {occ} recorded its token), started {started}, is older "
                          f"than the executor's age bound of {_minutes(horizon)}; the run's lock decides whether its "
                          "fire is gone, and the run then clears it and consolidates")
        return _held(f"it is the dream's own wrap (occurrence {occ}), started {started}, and is not yet provably dead: "
                     f"younger than the executor's age bound of {_minutes(horizon)}, past which the dream clears it")
    effective = max(floor_s, horizon)
    if _w._orphan_is_stale(started, bound, floor=floor_s, now=now):
        return True, (f"a wrap the dream did not open, started {started}, is past both the executor's age bound of "
                      f"{_minutes(horizon)} and the operator's floor of {_hours(floor_s)}: proven dead; the run clears "
                      "it and consolidates")
    return _held(f"a wrap the dream did not open, started {started}, is not yet provably dead: it must be older than "
                 f"{_hours(effective)} (the operator's floor is {_hours(floor_s)}, the executor's bound "
                 f"{_minutes(horizon)}); its seat may still be alive")


def _check2(src: Sources, now: datetime, floor_s: float | None) -> Check:
    name = "baton free, no open wrap"
    healed = ""
    if _guard(src.wrap_open, "the wrap state"):
        dead, note = _orphan_proof(src, now, floor_s)
        if not dead:
            return Check(2, name, QUIET, note, "wrap-open")
        healed = f"an open wrap passes admission ({note}); "
    if not _guard(src.requires_baton, "the baton policy"):
        return Check(2, name, "pass", healed + "the store does not require the baton" if healed
                     else "no wrap open; the store does not require the baton")
    holder = _guard(src.baton_holder, "the baton")
    if holder != SESSION_ID:
        return Check(2, name, QUIET, f"the baton is {'held by ' + repr(holder) if holder else 'unheld'}, not by "
                                     f"{SESSION_ID!r}", "baton")
    return Check(2, name, "pass", f"{healed}the baton is held by {SESSION_ID!r}" if healed
                 else f"no wrap open; the baton is held by {SESSION_ID!r}")


def _check3(src: Sources, agent: str, key: str, day: date, zone: str) -> Check:
    name = "no consolidate yet for the day"
    start = day_start(day, zone)
    rows = [r for r in _guard(src.occurrences, "the occurrence table") if r.get("agent_id") == agent
            and r.get("state") == "done"]
    spans: list[tuple[datetime, datetime]] = []
    for r in rows:
        if occurrence_dream_day(r, zone) == day:
            continue                                 # a done occurrence of THIS day is check 1's, and counts here
        if not r.get("launched_at") or not r.get("ended_at"):
            raise SourceUnreadable(f"done occurrence {r.get('occurrence_id')!r} has no launched_at or ended_at")
        skew = timedelta(seconds=CLOCK_SKEW_S)
        spans.append((_when(r["launched_at"]) - skew, _when(r["ended_at"]) + skew))
    since = sorted(((_when(at), wid) for wid, at in _guard(src.wraps, "the wrap history") if _when(at) >= start))
    # an occurrence made ONE wrap, taken as the last inside its span (no other wrap can open while the occurrence's is
    # open, but a seat's may land inside the span before its prepare_wrap or after its save, and the baton does not stop
    # a Levain seat); a wrap just after the span is somebody else's; everything else since the day's start is foreign
    for lo, hi in spans:
        inside = [w for w in since if lo <= w[0] <= hi]
        if inside:
            since.remove(inside[-1])
    foreign = [(wid, t) for t, wid in since]
    if foreign:
        wid, t = foreign[-1]
        return Check(3, name, QUIET, f"wrap #{wid} at {t.isoformat(timespec='seconds')} is since the dream-day's "
                                     f"start {start.isoformat(timespec='seconds')}", "consolidated-today")
    return Check(3, name, "pass", f"no wrap since {start.isoformat(timespec='seconds')}"
                                  " (one wrap per own done occurrence for another day excluded)")


def _check4(src: Sources) -> Check:
    name = "something to consolidate"
    n = _guard(src.episodes, "the episodes")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise SourceUnreadable(f"the episode count is not a count: {n!r}")
    if n == 0:
        return Check(4, name, QUIET, "no episodes since the last wrap", "nothing-to-consolidate")
    return Check(4, name, "pass", f"{n} episode(s) since the last wrap")


CHECK5_NAME = "live seats noted, not a gate"


def _check5(src: Sources) -> Check:
    """Name the live seats and PASS (see the module docstring, check 5): the dream runs with seats live. Never raises
    and never answers quiet or unreadable: nothing in the verdict rests on this list."""
    found: list[str] = []
    bad: list[str] = []
    for label, fn in (("desk launch", src.launches), ("awake.json work thread", src.awake_threads),
                      ("hooked session", src.sessions)):
        try:
            ids = _guard(fn, label)
        except SourceUnreadable as exc:
            bad.append(str(exc))
            continue
        if ids:
            found.append(f"{label}: {', '.join(str(i) for i in ids)[:120]}")
    parts: list[str] = []
    if found:
        parts.append("seats live, the dream runs with them (" + "; ".join(found) + "); a capture made after "
                     "prepare_wrap is outside the frozen episode set and is the next dream's input")
        try:
            if not _guard(src.requires_baton, "the baton policy"):
                parts.append("this store does not require the consolidate baton: a seat's own consolidate is kept out "
                             "while the dream's wrap is open by that open wrap (and `levain wrap` by wrap.lock) either "
                             "way, and the policy would add only a downgrade for a writer under another session id "
                             "than this install's; `levain init` creates a new store with it")
        except SourceUnreadable as exc:
            bad.append(f"the baton policy could not be read ({str(exc)[:120]})")
    elif bad:
        parts.append("no live seat found in the sources that could be read")
    else:
        parts.append("no live desk launch, work thread or hooked session")
    if bad:
        parts.append("not every source could be read, and none gates the dream: " + "; ".join(bad))
    if src.absent:
        parts.append("; ".join(src.absent))
    return Check(5, CHECK5_NAME, "pass", " | ".join(parts))


#: Phill's ruling (np-11fe7532 b): a run taken over before any fire claimed it does not spend the night's attempt, and
#: the extra launch it buys is one per window, inside check 1's ceiling and the window's budget whatever their numbers
TAKEN_OVER_EXTRA_LAUNCHES = 1
_TAKEN_OVER_REASON = "crash_recovered"   # levain.agents.controller.CRASH_RECOVERED, compared by value; a test pins it


def _taken_over_unclaimed(row: Mapping[str, Any]) -> bool:
    """True for a launched row that lapse recovery ended while no fire had ever claimed it: ``stopped`` for the
    takeover's reason with neither ``pid`` nor ``session_key`` written (a claim writes one and nothing clears it). A
    fire that claimed the row and was then lost has a pid, so its run still spends the attempt."""
    return (row.get("state") == "stopped" and row.get("reason") == _TAKEN_OVER_REASON
            and row.get("pid") is None and row.get("session_key") is None)


def _check6(src: Sources, agent: str, key: str, manual: bool) -> Check:
    name = "the window's attempt is not spent"
    if manual:
        return Check(6, name, "waived", "a manual run is the catch-up after a spent scheduled attempt; check 1's "
                                        "ceiling bounds it")
    rows = [r for r in _guard(src.occurrences, "the occurrence table") if r.get("agent_id") == agent
            and r.get("window_key") == key and r.get("launched_at") and r.get("state") in _SPENT_STATES]
    # a run lapse recovery ended before any fire claimed it never ran: the first one per window is not an attempt
    forgiven = [r for r in rows if _taken_over_unclaimed(r)][:TAKEN_OVER_EXTRA_LAUNCHES]
    rows = [r for r in rows if not any(r is f for f in forgiven)]
    if rows:
        r = rows[0]
        return Check(6, name, QUIET, f"{r.get('occurrence_id')} launched in this window and {r.get('state')}: one "
                                     "scheduled launch per window", "attempt-spent")
    if forgiven:
        return Check(6, name, "pass", f"{forgiven[0].get('occurrence_id')} was taken over before any fire claimed it: it "
                                      "did not spend the window's attempt")
    return Check(6, name, "pass", "no launched run of this window has failed")


def evaluate(src: Sources, *, day: date, now: datetime, zone: str, agent_id: str = "dream",
             manual: bool = False, orphan_floor_s: float | None = None) -> Verdict:
    """Run all six checks for dream-day ``day`` at ``now`` and return the verdict. Never raises for a bad source.

    ``orphan_floor_s`` is the operator's age floor (seconds) for an open wrap the dream did not open (D1-O, check 2);
    ``None`` is the shipped state: such a wrap keeps the dream quiet, as it always did."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ZoneInfo(zone)
    key = window_key_for(day)
    jobs: list[tuple[int, str, Callable[[], Check]]] = [
        (1, "not already dreamed", lambda: _check1(src, agent_id, key, day, zone, now, manual)),
        (2, "baton free, no open wrap", lambda: _check2(src, now, orphan_floor_s)),
        (3, "no consolidate yet for the day", lambda: _check3(src, agent_id, key, day, zone)),
        (4, "something to consolidate", lambda: _check4(src)),
        (5, CHECK5_NAME, lambda: _check5(src)),
        (6, "the window's attempt is not spent", lambda: _check6(src, agent_id, key, manual)),
    ]
    checks: list[Check] = []
    for n, name, run in jobs:
        try:
            checks.append(run())
        except SourceUnreadable as exc:
            checks.append(_c_unreadable(n, name, exc))
        except Exception as exc:  # noqa: BLE001 — a bug in a check is unreadable, never ready
            checks.append(_c_unreadable(n, name, SourceUnreadable(f"{type(exc).__name__}: {exc}")))
    quiet = next((c for c in checks if c.status == QUIET), None)
    bad = next((c for c in checks if c.status == UNREADABLE), None)
    chosen = quiet or bad
    if chosen is None:
        return Verdict(READY, "all six checks pass", None, "", day.isoformat(), key, zone, manual, tuple(checks))
    return Verdict(chosen.status, f"check {chosen.n} ({chosen.name}): {chosen.detail}", chosen.n, chosen.kind,
                   day.isoformat(), key, zone, manual, tuple(checks))


# ------------------------------------------------------------------------------------------------- the real sources

def _read_occurrences(agent_dir: Path) -> list[dict[str, Any]]:
    from levain.autonomic.db import DB_NAME

    path = agent_dir / DB_NAME
    try:
        os.lstat(path)
    except FileNotFoundError:
        return []
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=5.0, isolation_level=None)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_occurrences'").fetchone() is None:
            return []
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM agent_occurrences ORDER BY occurrence_id")]
    finally:
        conn.close()


def _awake_threads(coord_dir: Path, now: datetime) -> list[str]:
    path = coord_dir / "awake.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rec = json.loads(raw)
    if not isinstance(rec, dict):
        raise ValueError("awake.json is not an object")
    until = rec.get("until")
    if isinstance(until, (int, float)) and until < now.timestamp():
        return []                                    # an expired hold names nothing live
    threads = rec.get("threads") or []
    if not isinstance(threads, list):
        raise ValueError("awake.json threads is not a list")
    return [str(t) for t in threads]


def _dream_wraps(root: Path) -> dict[str, str]:
    """``{wrap token: occurrence id}`` for every occurrence directory under ``root`` (``<store dir>/dream``) whose
    ``progress.json`` names a wrap token: the wraps the dream's own runs opened. A directory or file that cannot be read,
    is a link, or is not the record's shape names nothing, which leaves its wrap unattributed (the stricter path)."""
    out: dict[str, str] = {}
    try:
        children = sorted(root.iterdir())
    except OSError:
        return out
    for d in children:
        path = d / "progress.json"
        try:
            if d.is_symlink() or not d.is_dir() or path.is_symlink() or not path.is_file():
                continue
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        token = doc.get("wrap_token") if isinstance(doc, dict) else None
        if isinstance(token, str) and token:
            out[token] = d.name
    return out


def sources_for(store_path: Path | str, *, agent_dir: Path | str | None = None, coord_dir: Path | str | None = None,
                now: datetime | None = None) -> tuple[Sources, Callable[[], None]]:
    """The real sources for the store at ``store_path``, and a ``close`` to call when done. The store is opened
    ``read_only`` (no schema init, no migration, no write), so a store whose layout is unknown reads as unreadable. A directory not given is an absent
    source and is named in the check 5 detail."""
    from anneal_memory import Store, sessions

    now = now or datetime.now(timezone.utc)
    store = Store(str(store_path), section_schema=None, read_only=True)
    absent = []
    agent = Path(agent_dir) if agent_dir is not None else None
    coord = Path(coord_dir) if coord_dir is not None else None
    if agent is None:
        absent.append("no agent store given: no occurrence history")
    if coord is None:
        absent.append("no desk dir given: no launches or awake.json")

    def launches() -> list[str]:
        if coord is None:
            return []
        from levain.autonomic.durable_sqlite import read_transaction
        from levain.desk import lease
        from levain.desk.door import readonly_store

        ro = readonly_store(coord)
        if ro is None:
            return []
        # every pending launch (a claimed spawn is no longer "unspawned" and is not yet "active") and every active
        # one, in one read transaction
        with read_transaction(ro._connect) as conn:
            return [ln.launch_id for ln in [*lease._in_state(conn, "pending"), *lease._in_state(conn, "active")]]

    def other_sessions() -> list[str]:
        return [s["session_id"] for s in sessions.live_sessions(store.continuity_path) if s["session_id"] != SESSION_ID]

    def open_wrap() -> tuple[str | None, str | None]:
        from levain import wrap as _w

        return _w._observe_prior_wrap(store)           # the token and start time the executor reads, the token first

    occurrence_root = Path(store_path).expanduser().parent / "dream"

    src = Sources(
        occurrences=(lambda: _read_occurrences(agent)) if agent is not None else (lambda: []),
        wrap_open=lambda: bool(store.get_wrap_started_at()),
        requires_baton=store.consolidate_requires_baton,
        baton_holder=lambda: sessions.baton_holder(store.continuity_path),
        wraps=lambda: [(w.id, w.wrapped_at) for w in store.get_wrap_history()],
        episodes=lambda: len(store.episodes_since_wrap()),
        launches=launches,
        awake_threads=(lambda: _awake_threads(coord, now)) if coord is not None else (lambda: []),
        sessions=other_sessions,
        absent=tuple(absent),
        open_wrap=open_wrap,
        dream_wraps=lambda: _dream_wraps(occurrence_root))
    return src, store.close
