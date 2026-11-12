"""The agent clock: which instants a schedule fires, and whether an instant is still worth firing.

Pure functions. No store, no file, no network, and no ambient clock: the caller passes ``now`` and the agent's STORED
zone name. The host's zone is never consulted, so a laptop that travels does not move an agent's slots.

A slot is a real UTC instant, matched minute by minute against the cron fields read in the stored zone. Walking real
instants (rather than wall-clock times) is what gives the DST behaviour for free: on fall-back the repeated wall hour
fires twice, once per instant, and on spring-forward the wall hour that does not exist fires never (it is skipped, not
shifted). Each slot is keyed by its UTC instant, so one slot is one key whatever the wall clock did.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from levain.agents.spec import _CRON_BOUNDS, _CRON_NAMES

GRACE = timedelta(minutes=15)
_MINUTE = timedelta(minutes=1)
MAX_SPAN = timedelta(days=90)  # the walk is per minute; a caller with an older cursor chunks it

DUE = "due"
MISSED = "missed"
NOT_YET = "not-yet"


@lru_cache(maxsize=256)
def _field_set(text: str, index: int) -> tuple[frozenset[int], bool]:
    """The values a cron field allows, and whether the field is unrestricted (a bare ``*``)."""
    lo, hi = _CRON_BOUNDS[index]
    names = _CRON_NAMES[index]
    out: set[int] = set()
    for item in text.lower().split(","):
        body, _, step_text = item.partition("/")
        step = int(step_text) if step_text else 1
        if body == "*":
            first, last = lo, hi
        else:
            ends = [names.get(e, int(e) if e.isdigit() else -1) for e in body.split("-")]
            first = ends[0]
            last = ends[-1] if len(ends) > 1 else (hi if step_text else first)
        out.update(range(first, last + 1, step))
    if index == 4:  # day-of-week: 7 is Sunday too
        out = {0 if v == 7 else v for v in out}
    # Vixie cron: a field that begins with ``*`` (``*/2`` too) counts as unrestricted for the day-field rule.
    return frozenset(out), text.startswith("*")


def _matches(cron: str, local: datetime) -> bool:
    minute, hour, dom, month, dow = cron.split()
    if local.minute not in _field_set(minute, 0)[0] or local.hour not in _field_set(hour, 1)[0]:
        return False
    if local.month not in _field_set(month, 3)[0]:
        return False
    dom_set, dom_any = _field_set(dom, 2)
    dow_set, dow_any = _field_set(dow, 4)
    dom_ok = local.day in dom_set
    dow_ok = (local.isoweekday() % 7) in dow_set
    # Vixie cron: when both day fields are restricted, either one admits the day.
    if not dom_any and not dow_any:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _hhmm(text: str) -> int:
    return int(text[:2]) * 60 + int(text[3:])


def _in_window(window: dict[str, str] | None, local: datetime) -> bool:
    if window is None:
        return True
    start, end = _hhmm(window["start"]), _hhmm(window["end"])
    at = local.hour * 60 + local.minute
    if start == end:  # no span to cut: the whole day
        return True
    if start < end:
        return start <= at < end
    return at >= start or at < end  # wraps midnight; the end minute itself is outside


def _zone(zone: str | None) -> ZoneInfo:
    if not zone:
        raise ValueError("a schedule needs the agent's stored zone; the host's zone is never used")
    try:
        return ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown schedule zone: {zone!r}") from exc


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("a clock reading must be timezone-aware")
    return moment.astimezone(timezone.utc)


def slots_between(trigger: dict[str, Any], zone: str | None, after: datetime, upto: datetime) -> list[datetime]:
    """Every slot instant with ``after < slot <= upto``, ascending, each a UTC datetime on a whole minute."""
    tz = _zone(zone)
    cursor = _utc(after).replace(second=0, microsecond=0)
    end = _utc(upto)
    if end - cursor > MAX_SPAN:
        raise ValueError("slots_between span is too large; walk it in bounded chunks")
    out: list[datetime] = []
    cursor += _MINUTE
    while cursor <= end:
        local = cursor.astimezone(tz)
        if _matches(trigger["cron"], local) and _in_window(trigger.get("window"), local):
            out.append(cursor)
        cursor += _MINUTE
    return out


def slot_key(slot: datetime) -> str:
    """The occurrence key: the canonical UTC instant."""
    return _utc(slot).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(slot: datetime, first_seen: datetime, grace: timedelta = GRACE) -> str:
    """``due`` when the slot is first seen no later than the grace after it, ``missed`` beyond that."""
    late = _utc(first_seen) - _utc(slot)
    if late < timedelta(0):
        return NOT_YET
    return DUE if late <= grace else MISSED


def window_key(trigger: dict[str, Any], zone: str | None, slot: datetime) -> str:
    """The budget/miss window a slot belongs to: one key across midnight for a windowed schedule."""
    local = _utc(slot).astimezone(_zone(zone))
    window = trigger.get("window")
    if window is None:
        return f"d:{local.date().isoformat()}"
    start = _hhmm(window["start"])
    at = local.hour * 60 + local.minute
    opened = local.date()
    # past midnight but before the end: the window opened the day before
    if _hhmm(window["start"]) > _hhmm(window["end"]) and at < start:
        opened = opened - timedelta(days=1)
    return f"w:{opened.isoformat()}T{window['start']}"


def window_span(trigger: dict[str, Any], zone: str | None, key: str) -> tuple[datetime, datetime]:
    """The inverse of :func:`window_key`: the UTC instants ``[start, end)`` of the window ``key`` names, read in the
    stored zone. ``d:<date>`` is that local day, midnight to midnight; ``w:<date>T<start>`` opens at the window's start
    on that date and closes at its end, the next day when the window wraps midnight (a window whose start is its end is
    a whole day). A key this trigger would not write (another form, another window start, a date that does not parse)
    raises ``ValueError``: a span is never guessed."""
    z = _zone(zone)
    window = trigger.get("window")
    try:
        if window is None:
            if not key.startswith("d:"):
                raise ValueError
            day = datetime.strptime(key[2:], "%Y-%m-%d")
            opens, closes = (day.replace(tzinfo=z), (day + timedelta(days=1)).replace(tzinfo=z))
        else:
            if not key.startswith("w:") or key[12:] != f"T{window['start']}":
                raise ValueError
            day = datetime.strptime(key[2:12], "%Y-%m-%d")
            start, end = _hhmm(window["start"]), _hhmm(window["end"])
            opens = (day + timedelta(minutes=start)).replace(tzinfo=z)
            closes = (day + timedelta(days=0 if start < end else 1, minutes=end)).replace(tzinfo=z)
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{key!r} is not a window key this schedule writes") from None
    return opens.astimezone(timezone.utc), closes.astimezone(timezone.utc)


def due_slots(trigger: dict[str, Any], zone: str | None, since: datetime, now: datetime,
              grace: timedelta = GRACE) -> list[tuple[datetime, str]]:
    """Slots in ``(since, now]`` with their state as first seen at ``now``. ``since`` is the caller's last look."""
    return [(s, classify(s, now, grace)) for s in slots_between(trigger, zone, since, now)]
