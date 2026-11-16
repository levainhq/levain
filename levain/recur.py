"""levain.recur — the agenda recurrence rule: parse a canon rule, find its next occurrence (pure, no clock read, no store).

The same four-rule grammar flow's ``scripts/intentions.py`` reads (``RECUR_GRAMMAR``), ported rather than imported because that
module is a script (``sys.exit`` on a bad argument). ``when_parse`` compiles a typed phrase to one of these canon strings; an
agenda stores it as ``recur`` (with ``recur_anchor`` = the occurrence last advanced to); ``done`` on a recurring agenda moves
``next`` to the first occurrence STRICTLY AFTER now instead of resolving it, and ``clear`` ends the series. An occurrence is a
calendar date plus a wall-clock time, never 24 h added to an instant, so a daylight-saving week keeps its hour. Agreement with
flow's functions is pinned by ``tests/vectors/recur_vectors.json`` (generated from flow's own, sha-pinned) and, where flow is
on the machine, by ``tests/test_recur.py``'s live comparison.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Optional

RECUR_GRAMMAR = ("daily HH:MM | weekly MON[,WED,...] HH:MM (days MON TUE WED THU FRI SAT SUN) | monthly D HH:MM "
                 "(D past the month's end = its last day) | every N days HH:MM")
RECURRING_TARGETS = ("band", "nudge")       # where a repeating agenda may ring: a desk drop composts at germination, so it has no done to advance
# The three things an operator may do with a repeating reminder, named ONCE for every surface (the List row, the band card, the phone):
# the words on the buttons live here and nowhere else (the page reads them from a row's ``series_labels`` facet). Placeholders that
# say what each does in plain words: the operator picks the final ones, and changing a string here renames it everywhere.
SERIES_VERBS = {
    "done": "Done this time",           # this occurrence is over; the series moves to its next one (also before this one is due)
    "snooze": "Snooze this time",       # this occurrence moves to a later time; the series is unchanged
    "stop": "Stop repeating",           # the one verb that ends the series
}
_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_TIME = re.compile(r"([0-9]{1,2}):([0-9]{2})")
_FMT = "%Y-%m-%dT%H:%M"


def _time(tok: str, rule: Any) -> tuple[int, int]:
    m = _TIME.fullmatch(tok or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"bad recurrence rule {rule!r}: {tok!r} is not a time HH:MM (00:00-23:59). Grammar: {RECUR_GRAMMAR}")
    return int(m.group(1)), int(m.group(2))


def parse_recur(rule: Any) -> dict[str, Any]:
    """A rule as ``{"kind", "hh", "mm", ..., "canon"}``, or ValueError carrying the grammar. ``canon`` is the normalised
    string, the form an agenda stores."""
    toks = str(rule or "").split()
    bad = ValueError(f"bad recurrence rule {rule!r}. Grammar: {RECUR_GRAMMAR}")
    if not toks:
        raise bad
    kind = toks[0].lower()
    if kind == "daily" and len(toks) == 2:
        hh, mm = _time(toks[1], rule)
        return {"kind": "daily", "hh": hh, "mm": mm, "canon": f"daily {hh:02d}:{mm:02d}"}
    if kind == "weekly" and len(toks) == 3:
        days = [d.upper() for d in toks[1].split(",")]
        if not days or any(d not in _DAYS for d in days):
            raise bad
        days = sorted(set(days), key=_DAYS.index)
        hh, mm = _time(toks[2], rule)
        return {"kind": "weekly", "days": [_DAYS.index(d) for d in days], "hh": hh, "mm": mm,
                "canon": f"weekly {','.join(days)} {hh:02d}:{mm:02d}"}
    if kind == "monthly" and len(toks) == 3:
        if not re.fullmatch(r"[0-9]{1,2}", toks[1]) or not 1 <= int(toks[1]) <= 31:
            raise bad
        hh, mm = _time(toks[2], rule)
        return {"kind": "monthly", "day": int(toks[1]), "hh": hh, "mm": mm, "canon": f"monthly {int(toks[1])} {hh:02d}:{mm:02d}"}
    if kind == "every" and len(toks) == 4 and toks[2].lower() in ("day", "days"):
        if not re.fullmatch(r"[0-9]{1,6}", toks[1]) or not 1 <= int(toks[1]) <= 366:
            raise bad
        hh, mm = _time(toks[3], rule)
        return {"kind": "every", "n": int(toks[1]), "hh": hh, "mm": mm, "canon": f"every {int(toks[1])} days {hh:02d}:{mm:02d}"}
    raise bad


def _parse_when(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    for fmt in (_FMT, "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def next_occurrence(rule: Any, after: datetime, prev_next: Any = None) -> datetime:
    """The first occurrence of ``rule`` STRICTLY AFTER ``after`` (a naive local datetime). ``every N days`` is anchored: N days
    from ``prev_next`` (the occurrence being replaced) when it reads, else from ``after``'s day. Raises ValueError for a bad rule
    or a date past the supported range."""
    try:
        return _next(rule, after, prev_next)
    except OverflowError as exc:
        raise ValueError(f"recurrence {rule!r} runs past the supported dates ({exc})") from exc


def _next(rule: Any, after: datetime, prev_next: Any) -> datetime:
    p = parse_recur(rule)

    def at(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, p["hh"], p["mm"])     # wall clock on a calendar date: DST-proof

    day = after.date()
    if p["kind"] == "daily":
        return next(c for c in (at(day + timedelta(days=k)) for k in range(3)) if c > after)
    if p["kind"] == "weekly":
        return next(c for c in (at(day + timedelta(days=k)) for k in range(9))
                    if c.date().weekday() in p["days"] and c > after)
    if p["kind"] == "monthly":
        y, m = day.year, day.month
        for _ in range(3):
            c = at(date(y, m, min(p["day"], calendar.monthrange(y, m)[1])))
            if c > after:
                return c
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    anchor = _parse_when(prev_next)
    base = anchor.date() if anchor else day
    k = max(0, -(-(day - base).days // p["n"]))
    while at(base + timedelta(days=k * p["n"])) <= after:
        k += 1
    return at(base + timedelta(days=k * p["n"]))


def rule_of(item: Mapping[str, Any]) -> Optional[str]:
    """The valid canon rule on an open agenda, else None (a hand-edited bad rule reads as no rule: it never crashes a close)."""
    rule = item.get("recur")
    if not isinstance(rule, str):
        return None
    try:
        return parse_recur(rule)["canon"]
    except ValueError:
        return None


def advance_changes(item: Mapping[str, Any], now: datetime, *, before_due: bool = False) -> Optional[dict[str, Any]]:
    """The update that ``done`` on a recurring agenda applies instead of resolving it: ``next`` and ``recur_anchor`` move to the
    first occurrence strictly after ``now`` and the germinated stamp clears so it files again then. None when the agenda carries
    no readable rule, or its next occurrence cannot be computed (the caller then resolves it as it always did: a series that
    cannot advance is closed, never left stamped and stuck). ``before_due``: the operator says "done this time" BEFORE the
    pending occurrence is due, so the next one is the first after that pending occurrence too (without it, the pending one is
    the first strictly after ``now`` and nothing would move); a ringing or overdue one is already behind ``now``, so it is the same.
    A DATE-ONLY pending occurrence (a snooze to a day) covers that whole calendar day, with or without ``before_due``: the
    occurrence done is that day's, so the next one is after the day's last minute, never later the same day."""
    rule = rule_of(item)
    if rule is None:
        return None
    after = now.replace(second=0, microsecond=0)
    nxt_raw = item.get("next")
    pending = _parse_when(nxt_raw)
    if pending is not None and isinstance(nxt_raw, str) and "T" not in nxt_raw:
        pending = pending.replace(hour=23, minute=59)       # the day's last wall-clock minute: the exclusive lower bound
        # Known edge (reachability line): seconds stay 0 here, as in ``after``; at 23:59:xx of that day ``after`` already equals it
        if pending > after:
            after = pending
    elif before_due and pending is not None and pending > after:
        after = pending
    try:
        nxt = next_occurrence(rule, after, item.get("recur_anchor") or item.get("next")).strftime(_FMT)
    except ValueError:
        return None
    return {"next": nxt, "recur_anchor": nxt, "germinated": {}}


def is_series(item: Mapping[str, Any]) -> bool:
    """An open agenda that repeats (a rule that reads): its close goes through ``SERIES_VERBS``, never a plain resolve."""
    return item.get("disposition") == "agenda" and rule_of(item) is not None


def series_refusal(intention_id: Any) -> str:
    """The ONE refusal text for a write that would end a repeating agenda without saying so (a plain resolve, a Keep, a promote):
    it names the three verbs by the table, on every surface that refuses it (the write route, the phone effect, the store)."""
    return (f"{intention_id} repeats: resolving it would end the series. Use '{SERIES_VERBS['done']}' to move to the next one, "
            f"'{SERIES_VERBS['snooze']}' to move this one, or '{SERIES_VERBS['stop']}' to end it")


def not_series_refusal(intention_id: Any) -> str:
    """The refusal text for a series verb on a row that does not repeat."""
    return f"{intention_id} does not repeat: there is no series to stop or move on"


def repeats_words(item: Mapping[str, Any]) -> str:
    """`` (repeats <canon>)`` for a recurring agenda's filed row, else the empty string."""
    rule = rule_of(item)
    return f" (repeats {rule})" if rule else ""
