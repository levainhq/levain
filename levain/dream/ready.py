"""levain.dream.ready (D1-1c): the day-ready predicate (D1_EXECUTOR §4.2).

``evaluate`` answers ``ready``, ``quiet`` or ``unreadable`` for one dream-day and records ALL six checks, whatever the
answer. It is fail-closed: a source that cannot be read is ``unreadable`` (never a pass), and ``unreadable`` is never a
reason to launch. A definite ``quiet`` outranks an ``unreadable`` (the reason it names is a fact), and among several the
lowest check number names the verdict; every check is still in ``checks``.

The predicate reads sources through zero-argument callables (:class:`Sources`), so a test (or the controller) injects
exactly what it means each source to say: a callable that raises means that source was UNREADABLE. :func:`sources_for`
builds the real ones from an install's directories. A directory that was not given is an ABSENT source (no occurrence
history, no coordinator, no agent store), recorded as such in the check's detail, so the verb can run against a bare
store copy; the controller passes every directory it owns.

The six checks, each mapped from flow's ``dream.py`` ``gate``:

1. no ``done`` occurrence for the window, and fewer than :data:`RUN_CEILING` launched (a backstop only)
2. no wrap open, and the baton held by this install's session (when the store requires the baton)
3. no wrap since the dream-day's start, apart from the one wrap each of this agent's ``done`` occurrences for OTHER
   dream-days made
4. something to consolidate
5. no live session: no live coordinator launch, no work thread in ``awake.json``, no other session in anneal's
   session registry (a hooked Claude Code session heartbeats there). Waived for a manual run
6. the window's one scheduled attempt is not already spent (a launched run that failed or was stopped). Waived for a
   manual run: the catch-up is the remedy for exactly that failure, and check 1's ceiling bounds it
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
    #                        nothing-to-consolidate, live-seat, attempt-spent

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
    # a manual run has its own window key (``m:<date>``), so a manual run in flight is looked for by kind or key prefix,
    # for a scheduled evaluation as much as a manual one: either overlapping the other is the double launch
    inflight = [r for r in rows if r.get("state") in ("admitted", "launched")
                and (r.get("window_key") == key or r.get("kind") == "manual"
                     or str(r.get("window_key") or "").startswith("m:"))]
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


def _check2(src: Sources) -> Check:
    name = "baton free, no open wrap"
    if _guard(src.wrap_open, "the wrap state"):
        return Check(2, name, QUIET, "a wrap is open on the store (a stale orphan is healed inside the effect, not "
                                     "judged here)", "wrap-open")
    if not _guard(src.requires_baton, "the baton policy"):
        return Check(2, name, "pass", "no wrap open; the store does not require the baton")
    holder = _guard(src.baton_holder, "the baton")
    if holder != SESSION_ID:
        return Check(2, name, QUIET, f"the baton is {'held by ' + repr(holder) if holder else 'unheld'}, not by "
                                     f"{SESSION_ID!r}", "baton")
    return Check(2, name, "pass", f"no wrap open; the baton is held by {SESSION_ID!r}")


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
    # an occurrence made ONE wrap, the last inside its span (the baton keeps any other writer out of the span, and a
    # wrap just after it is somebody else's); everything else since the day's start is foreign
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


def _check5(src: Sources, manual: bool) -> Check:
    name = "no live session"
    if manual:
        return Check(5, name, "waived", "a manual run waives the live-session check (flow's --now fallback)")
    found: list[str] = []
    bad: list[str] = []
    for label, fn in (("coordinator launch", src.launches), ("awake.json work thread", src.awake_threads),
                      ("hooked session", src.sessions)):
        try:
            ids = _guard(fn, label)
        except SourceUnreadable as exc:
            bad.append(str(exc))
            continue
        if ids:
            found.append(f"{label}: {', '.join(str(i) for i in ids)[:120]}")
    if found:
        return Check(5, name, QUIET, "; ".join(found), "live-seat")
    if bad:
        return Check(5, name, UNREADABLE, "; ".join(bad))
    return Check(5, name, "pass", "no live coordinator launch, work thread or hooked session"
                 + (f" ({'; '.join(src.absent)})" if src.absent else ""))


def _check6(src: Sources, agent: str, key: str, manual: bool) -> Check:
    name = "the window's attempt is not spent"
    if manual:
        return Check(6, name, "waived", "a manual run is the catch-up after a spent scheduled attempt; check 1's "
                                        "ceiling bounds it")
    rows = [r for r in _guard(src.occurrences, "the occurrence table") if r.get("agent_id") == agent
            and r.get("window_key") == key and r.get("launched_at") and r.get("state") in _SPENT_STATES]
    if rows:
        r = rows[0]
        return Check(6, name, QUIET, f"{r.get('occurrence_id')} launched in this window and {r.get('state')}: one "
                                     "scheduled launch per window", "attempt-spent")
    return Check(6, name, "pass", "no launched run of this window has failed")


def evaluate(src: Sources, *, day: date, now: datetime, zone: str, agent_id: str = "dream",
             manual: bool = False) -> Verdict:
    """Run all six checks for dream-day ``day`` at ``now`` and return the verdict. Never raises for a bad source."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    ZoneInfo(zone)
    key = window_key_for(day)
    jobs: list[tuple[int, str, Callable[[], Check]]] = [
        (1, "not already dreamed", lambda: _check1(src, agent_id, key, day, zone, now, manual)),
        (2, "baton free, no open wrap", lambda: _check2(src)),
        (3, "no consolidate yet for the day", lambda: _check3(src, agent_id, key, day, zone)),
        (4, "something to consolidate", lambda: _check4(src)),
        (5, "no live session", lambda: _check5(src, manual)),
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
        absent.append("no coordinator dir given: no launches or awake.json")

    def launches() -> list[str]:
        if coord is None:
            return []
        from levain.autonomic.durable_sqlite import read_transaction
        from levain.coordinator import lease
        from levain.coordinator.door import readonly_store

        ro = readonly_store(coord)
        if ro is None:
            return []
        # every pending launch (a claimed spawn is no longer "unspawned" and is not yet "active") and every active
        # one, in one read transaction
        with read_transaction(ro._connect) as conn:
            return [ln.launch_id for ln in [*lease._in_state(conn, "pending"), *lease._in_state(conn, "active")]]

    def other_sessions() -> list[str]:
        return [s["session_id"] for s in sessions.live_sessions(store.continuity_path) if s["session_id"] != SESSION_ID]

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
        absent=tuple(absent))
    return src, store.close
