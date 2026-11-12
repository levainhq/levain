"""levain.when_parse — the List's natural-language reminder time, as pure grammar (no model, no clock read, no store).

Row 188 (``project_memory/1110-19_row188_grammar_design.md``). Two entry points share one reading of a ``<when>``:

* :func:`parse_remind` — ``remind [me] <when> [to] <words>``: the operator-reminder line. Returns
  ``{"kind": "remind", "when": "YYYY-MM-DDTHH:MM", "text": words}`` plus ``"recur": <canon>`` when the phrase repeats, or
  ``None`` when the line is not a remind (so prose that starts with the word, ``remind me to call Jim``, still sorts).
* :func:`read_when_text` — a ``<when>`` at the head of a body (the ``@`` desk drop uses it with ``allow_recur=False``),
  returning the reading and the words after it.

``<when>``: ``HH:MM`` / ``9am`` / ``5:30 pm`` / ``noon`` / ``midnight`` (today if still ahead, else tomorrow) ·
``tomorrow|today|tonight [time]`` · ``mon..sun`` or a full weekday ``[time]`` (the coming one, today if its time is still
ahead) · ``MM-DD [time]`` · ``YYYY-MM-DD [time]`` · ``+Nm`` / ``+Nh`` / ``+Nd``; an ``at`` before a time is dropped. No time
means 09:00 (``tonight`` alone means 20:00). Only for ``remind``: ``every day|weekday|<days>|other <day>|N days|N weeks|month
[on the] D``, ``daily``, ``weekly <days>``, ``monthly D``, each with an optional time. A repeating phrase compiles to one of
the four canon strings the agenda store reads (``daily HH:MM`` · ``weekly MON[,WED] HH:MM`` · ``monthly D HH:MM`` ·
``every N days HH:MM``) and ``when`` is its first occurrence strictly after ``now``.

A parser informs the human, it never guesses and never authorises: anything that reads two ways, or does not read, raises
:class:`WhenRefused` with the reason and the fix, and the line stays in the box. ``now`` is a naive local datetime (the
broker's clock, passed in); nothing here reads the system clock.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

DEFAULT_HOUR = 9
TONIGHT_HOUR = 20
MAX_EVERY_DAYS = 366
_FMT = "%Y-%m-%dT%H:%M"
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_FULL = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
             "fri": "friday", "sat": "saturday", "sun": "sunday"}
_DAY_CANON = [d.upper() for d in _DAYS]
_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", re.I)
_BARE_RE = re.compile(r"^\d{1,2}$")
_ORDINAL_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?$", re.I)
_HEADS = frozenset({"today", "tonight", "tomorrow", "every", "daily", "weekly", "monthly", "next", "noon", "midnight"})
FORMS = ("HH:MM, 9am, noon, today|tonight|tomorrow [time], mon..sun [time], MM-DD [time], YYYY-MM-DD [time], "
         "+30m, +2h, +3d (no time means 09:00)")
REPEAT_FORMS = ("every day|weekday|mon,wed|other thu|N days|N weeks [time], every month on the 15th [time], "
                "daily [time], weekly mon [time], monthly 15 [time]")


class WhenRefused(ValueError):
    """The text cannot be read as written; the message says why and what to write instead."""


@dataclass(frozen=True)
class When:
    """One reading: the first moment (naive local, minute precision) and the canon recurrence rule when it repeats."""
    minute: datetime
    recur: Optional[str] = None

    @property
    def iso(self) -> str:
        return self.minute.strftime(_FMT)


# --- tokens and clock ---------------------------------------------------------------------------------------------

def _spans(text: str) -> List[Tuple[int, int]]:
    return [m.span() for m in re.finditer(r"\S+", text)]


def _low(tok: str) -> str:
    return tok.lower().rstrip(",")


def _day_index(tok: str) -> Optional[int]:
    t = _low(tok)
    for i, d in enumerate(_DAYS):
        if t in (d, _DAY_FULL[d]):
            return i
    return None


def _timeish(tok: str) -> bool:
    """A token that begins a time of day: a digit-led token, or noon / midnight."""
    t = _low(tok)
    return bool(t[:1].isdigit()) or t in ("noon", "midnight")


def _bare_hour_refusal(tok: str) -> WhenRefused:
    return WhenRefused(f"{tok!r} reads as an hour without am/pm: write {tok}pm, {tok}am or {int(tok):02d}:00")


def _take_clock(tokens: List[str], i: int, *, after_at: bool = False) -> Optional[Tuple[Tuple[int, int], int]]:
    """``((h, m), index after)`` for a time of day at ``tokens[i]``; None when no time is there; refuses a bare hour
    (``3``) and a time-shaped token that does not read. A number too large to be an hour is a word, not a time."""
    if i >= len(tokens):
        return None
    tok = tokens[i].rstrip(",")
    low = tok.lower()
    if low == "noon":
        return (12, 0), i + 1
    if low == "midnight":
        return (0, 0), i + 1
    used = i + 1
    nxt = _low(tokens[i + 1]) if i + 1 < len(tokens) else ""
    if nxt in ("am", "pm") and re.match(r"^\d{1,2}(:\d{2})?$", tok):
        tok, used = tok + nxt, i + 2
        low = tok.lower()
    if _BARE_RE.match(tok):
        if int(tok) <= 23:
            raise _bare_hour_refusal(tok)
        if after_at:
            raise WhenRefused(f"'at {tok}' does not read as a time of day (use 14:30 or 2:30pm)")
        return None
    if not (low[:1].isdigit() and (":" in low or low.endswith(("am", "pm")))):
        if after_at:
            raise WhenRefused(f"'at {tok}' does not read as a time of day (use 14:30 or 2:30pm)")
        return None
    m = _CLOCK_RE.match(low)
    if not m:
        raise WhenRefused(f"{tok!r} does not read as a time of day (use 14:30 or 2:30pm)")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            raise WhenRefused(f"{tok!r} does not read as a time of day (use 14:30 or 2:30pm)")
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        raise WhenRefused(f"{tok!r} does not read as a time of day (use 14:30 or 2:30pm)")
    return (h, mi), used


def _clock_after(tokens: List[str], i: int) -> Tuple[Optional[Tuple[int, int]], int]:
    """The time slot after a day word: an optional ``at``, then a time. ``(None, i)`` when there is none."""
    if i < len(tokens) and _low(tokens[i]) == "at" and i + 1 < len(tokens) and _timeish(tokens[i + 1]):
        got = _take_clock(tokens, i + 1, after_at=True)
        return got[0], got[1]
    got = _take_clock(tokens, i)
    return (got[0], got[1]) if got else (None, i)


def _at(day: date, hm: Tuple[int, int]) -> datetime:
    """Wall clock on a calendar date, never 24 h added, so a DST week keeps its hour."""
    return datetime(day.year, day.month, day.day, hm[0], hm[1])


# --- repeating phrases --------------------------------------------------------------------------------------------

def _first_after(kind: str, now: datetime, hm: Tuple[int, int], *, days=None, n: int = 0, dom: int = 0) -> datetime:
    """The first occurrence of a canon rule strictly after ``now`` (``every N days`` counts from today)."""
    today = now.date()
    if kind == "daily":
        return next(c for c in (_at(today + timedelta(days=k), hm) for k in range(3)) if c > now)
    if kind == "weekly":
        return next(c for c in (_at(today + timedelta(days=k), hm) for k in range(9))
                    if c.date().weekday() in days and c > now)
    if kind == "monthly":
        y, m = today.year, today.month
        for _ in range(3):
            c = _at(date(y, m, min(dom, calendar.monthrange(y, m)[1])), hm)
            if c > now:
                return c
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    k = 0
    while _at(today + timedelta(days=k * n), hm) <= now:
        k += 1
    return _at(today + timedelta(days=k * n), hm)


def _coming_day(now: datetime, weekday: int, hm: Tuple[int, int]) -> datetime:
    """The bare-weekday rule: the coming one, today counting while its time is still ahead."""
    when = _at((now + timedelta(days=(weekday - now.weekday()) % 7)).date(), hm)
    return when if when > now else when + timedelta(days=7)


def _take_days(tokens: List[str], i: int) -> Tuple[List[int], int]:
    """A day list at ``tokens[i]``: ``mon``, ``mon,wed``, ``mon, wed``, ``mon and wed``. ``([], i)`` when none."""
    days: List[int] = []
    j = i
    while j < len(tokens):
        parts = [p for p in tokens[j].lower().split(",") if p]
        idx = [_day_index(p) for p in parts]
        if not parts or any(x is None for x in idx):
            break
        days += [x for x in idx if x is not None]
        trailing_comma = tokens[j].endswith(",")
        j += 1
        if j + 1 < len(tokens) and tokens[j].lower() in ("and", "&") and _day_index(tokens[j + 1].split(",")[0]) is not None:
            j += 1
        elif not (trailing_comma and j < len(tokens) and _day_index(tokens[j].split(",")[0]) is not None):
            break
    return days, j


def _time_or_default(tokens: List[str], i: int) -> Tuple[Tuple[int, int], int]:
    hm, used = _clock_after(tokens, i)
    return (hm if hm else (DEFAULT_HOUR, 0)), used


def _every_n(rule_n: int, hm: Tuple[int, int], now: datetime) -> When:
    first = _first_after("every", now, hm, n=rule_n)
    return When(first, f"every {rule_n} days {hm[0]:02d}:{hm[1]:02d}")


def _monthly(tokens: List[str], i: int, now: datetime) -> Tuple[When, int]:
    """The day-of-month after ``every month`` / ``monthly``: ``[on] [the] 15[th]``."""
    j = i
    if j < len(tokens) and _low(tokens[j]) == "on":
        j += 1
    if j < len(tokens) and _low(tokens[j]) == "the":
        j += 1
    m = _ORDINAL_RE.match(_low(tokens[j])) if j < len(tokens) else None
    if not m:
        raise WhenRefused("which day of the month? Write 'every month on the 15th [time]' or 'monthly 15 [time]'")
    dom = int(m.group(1))
    if not 1 <= dom <= 31:
        raise WhenRefused(f"day {dom} is not a day of the month (1-31; past the month's end it rings on its last day)")
    hm, used = _time_or_default(tokens, j + 1)
    return When(_first_after("monthly", now, hm, dom=dom), f"monthly {dom} {hm[0]:02d}:{hm[1]:02d}"), used


def _weekly(days: List[int], hm: Tuple[int, int], now: datetime) -> When:
    uniq = sorted(set(days))
    canon = f"weekly {','.join(_DAY_CANON[d] for d in uniq)} {hm[0]:02d}:{hm[1]:02d}"
    return When(_first_after("weekly", now, hm, days=uniq), canon)


def _unsupported(what: str) -> WhenRefused:
    return WhenRefused(f"{what} is not a repeat this can store. Forms: {REPEAT_FORMS}")


def _read_repeat(tokens: List[str], now: datetime) -> Tuple[When, int]:
    head = _low(tokens[0])
    if head == "daily":
        hm, used = _time_or_default(tokens, 1)
        return When(_first_after("daily", now, hm), f"daily {hm[0]:02d}:{hm[1]:02d}"), used
    if head == "monthly":
        return _monthly(tokens, 1, now)
    if head == "weekly":
        days, j = _take_days(tokens, 1)
        if not days:
            raise WhenRefused("weekly on which day? Write 'weekly mon 9am' or 'every mon 9am'")
        hm, used = _time_or_default(tokens, j)
        return _weekly(days, hm, now), used
    if len(tokens) < 2:
        raise WhenRefused(f"every what? Forms: {REPEAT_FORMS}")
    sec = _low(tokens[1])
    if sec == "day":
        hm, used = _time_or_default(tokens, 2)
        return When(_first_after("daily", now, hm), f"daily {hm[0]:02d}:{hm[1]:02d}"), used
    if sec == "weekday":
        hm, used = _time_or_default(tokens, 2)
        return _weekly([0, 1, 2, 3, 4], hm, now), used
    if sec == "month":
        return _monthly(tokens, 2, now)
    if sec == "week":
        raise WhenRefused("every week on which day? Write 'every mon 9am' (or 'every 2 weeks 9am' to count from today)")
    if sec == "other":
        days, j = _take_days(tokens, 2)
        if len(days) != 1:
            raise WhenRefused("'every other' needs one weekday: write 'every other thu [time]'")
        hm, used = _time_or_default(tokens, j)
        return When(_coming_day(now, days[0], hm), f"every 14 days {hm[0]:02d}:{hm[1]:02d}"), used
    if _day_index(sec.split(",")[0]) is not None:
        days, j = _take_days(tokens, 1)
        hm, used = _time_or_default(tokens, j)
        return _weekly(days, hm, now), used
    if sec.isdigit():
        if len(tokens) < 3:
            raise WhenRefused(f"every {sec} what? Write 'every {sec} days [time]' or 'every {sec} weeks [time]'")
        unit = _low(tokens[2])
        n = int(sec)
        if unit in ("day", "days"):
            step = n
        elif unit in ("week", "weeks"):
            step = n * 7
        else:
            raise _unsupported(f"'every {sec} {tokens[2]}'")
        if not 1 <= step <= MAX_EVERY_DAYS:
            raise WhenRefused(f"every {step} days is out of range: 1 to {MAX_EVERY_DAYS} days")
        hm, used = _time_or_default(tokens, 3)
        return _every_n(step, hm, now), used
    if _ORDINAL_RE.match(sec) and not sec.isdigit():
        raise _unsupported(f"'every {tokens[1]}' (an nth weekday)")
    raise _unsupported(f"'every {tokens[1]}'")


# --- one-off readings ---------------------------------------------------------------------------------------------

def _read_date(head: str, tokens: List[str], now: datetime) -> Optional[Tuple[datetime, int]]:
    m = re.match(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$", head)
    if not m:
        return None
    month, dom = int(m.group(2)), int(m.group(3))
    hm, used = _time_or_default(tokens, 1)
    # a written year is held to (a past moment in it is refused by the caller, never moved); no year means the next
    # occurrence: this year if still ahead, else the next year that has the date (a leap day can be up to 8 years off)
    years = [int(m.group(1))] if m.group(1) else range(now.year, now.year + 9)
    found = None
    for year in years:
        try:
            day = date(year, month, dom)
        except ValueError:
            continue
        found = _at(day, hm)
        if m.group(1) or found > now:
            break
    if found is None:
        raise WhenRefused(f"{tokens[0]!r} is not a calendar date")
    return found, used


def _read_one(tokens: List[str], now: datetime, allow_recur: bool) -> Tuple[When, int]:
    head = _low(tokens[0])
    if head in ("every", "daily", "weekly", "monthly"):
        if not allow_recur:
            raise WhenRefused("a repeating line is a reminder: write 'remind every sun 9pm ...'; a desk drop cannot repeat")
        return _read_repeat(tokens, now)
    m = re.match(r"^\+(\d+)([mhd])$", head)
    if m:
        n = int(m.group(1))
        try:
            delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[m.group(2)]
            return When((now + delta).replace(second=0, microsecond=0)), 1
        except OverflowError:
            raise WhenRefused(f"{tokens[0]!r} runs past the supported dates")
    if head == "next":
        if len(tokens) > 1 and _day_index(tokens[1]) is not None:
            raise WhenRefused(f"'next {tokens[1]}' is read two ways: write '{_DAYS[_day_index(tokens[1]) or 0]}' (the coming one) "
                              f"or a date")
        raise WhenRefused(f"'next' is not a time. Forms: {FORMS}")
    if head == "at" and len(tokens) > 1 and _timeish(tokens[1]):
        got = _take_clock(tokens, 1, after_at=True)
        assert got is not None
        when = _at(now.date(), got[0])
        return When(when if when > now else when + timedelta(days=1)), got[1]
    got = _take_clock(tokens, 0)
    if got:
        when = _at(now.date(), got[0])
        return When(when if when > now else when + timedelta(days=1)), got[1]
    if head == "tomorrow":
        hm, used = _time_or_default(tokens, 1)
        return When(_at((now + timedelta(days=1)).date(), hm)), used
    if head == "today":
        hm, used = _time_or_default(tokens, 1)
        when = _at(now.date(), hm)
        if when <= now:
            raise WhenRefused(f"{hm[0]:02d}:{hm[1]:02d} today has passed: write a later time, or 'tomorrow'")
        return When(when), used
    if head == "tonight":
        hm, used = _clock_after(tokens, 1)
        if hm is None:
            hm = (TONIGHT_HOUR, 0)
        elif hm[0] < 12:
            raise WhenRefused(f"'tonight' is the evening: write {hm[0]:02d}:{hm[1]:02d} as an evening time (9pm or 21:00), "
                              f"or 'tomorrow' for the morning")
        when = _at(now.date(), hm)
        if when <= now:
            raise WhenRefused(f"{hm[0]:02d}:{hm[1]:02d} tonight has passed: write a later time, or 'tomorrow'")
        return When(when), used
    dow = _day_index(head)
    if dow is not None:
        hm, used = _time_or_default(tokens, 1)
        return When(_coming_day(now, dow, hm)), used
    dated = _read_date(head, tokens, now)
    if dated:
        return When(dated[0]), dated[1]
    raise WhenRefused(f"cannot read {tokens[0]!r} as a time. Forms: {FORMS}")


def read_when_text(body: str, now: datetime, *, allow_recur: bool = False) -> Tuple[When, str]:
    """A ``<when>`` at the head of ``body``: ``(When, the words after it)``. Raises :class:`WhenRefused` with the reason for
    a head that does not read, reads two ways, or names a moment that is not in the future. ``now`` is naive local time."""
    if now.tzinfo is not None:
        raise TypeError("now must be a naive local datetime (the clock the minute is stored in)")
    now = now.replace(second=0, microsecond=0)
    spans = _spans(body or "")
    tokens = [body[a:b] for a, b in spans]
    if not tokens:
        raise WhenRefused(f"no time given. Forms: {FORMS}")
    try:
        reading, used = _read_one(tokens, now, allow_recur)
    except OverflowError:
        raise WhenRefused("that date runs past the supported dates")
    if reading.minute <= now:
        raise WhenRefused(f"{reading.minute:%Y-%m-%d %H:%M} is not in the future; give a moment still ahead")
    words = body[spans[used - 1][1]:].strip() if used <= len(spans) else ""
    return reading, words


# --- the remind line ----------------------------------------------------------------------------------------------

def _remind_body(line: str) -> Optional[str]:
    """The text after ``remind [me]`` when it begins like a time (the clock-free shape test); else None."""
    line = (line or "").strip()
    spans = _spans(line)
    tokens = [line[a:b] for a, b in spans]
    if not tokens or _low(tokens[0]) != "remind":
        return None
    i = 2 if len(tokens) > 1 and _low(tokens[1]) == "me" else 1
    if i >= len(tokens):
        return None
    head = _low(tokens[i])
    shaped = (head[:1].isdigit() or head.startswith("+") or head in _HEADS or _day_index(head) is not None
              or (head == "at" and i + 1 < len(tokens) and _timeish(tokens[i + 1])))
    return line[spans[i][0]:] if shaped else None


def is_remind(line: str) -> bool:
    """True when ``line`` is a remind line: ``remind [me]`` straight into a time word. Needs no clock, so the tier can ask it
    before the line is read; ``parse_remind`` and the tier agree because both call this shape test."""
    return _remind_body(line) is not None


def parse_remind(line: str, now: datetime) -> Optional[dict]:
    """``remind [me] <when> [to] <words>`` into ``{"kind": "remind", "when", "text"[, "recur"]}``; None when the line is not a
    remind (``remind me to call Jim`` is prose and sorts). Raises :class:`WhenRefused` for a remind that cannot be read."""
    body = _remind_body(line)
    if body is None:
        return None
    reading, words = read_when_text(body, now, allow_recur=True)
    parts = words.split(None, 1)
    if parts and parts[0].lower() == "to":
        words = parts[1].strip() if len(parts) > 1 else ""
    if not words:
        raise WhenRefused("no words after the time: say what to remind you of")
    out = {"kind": "remind", "when": reading.iso, "text": words}
    if reading.recur:
        out["recur"] = reading.recur
    return out
