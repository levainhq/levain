"""levain.intake — THE LIST's one-line intake grammar (pure, deterministic, no model).

Ported from flow's ``spores.parse_intake`` (task_surfaces_1107.md, THE LIST). Parity
contract: ``tests/vectors/list_intake_vectors.json``.

  ``@<when> <words>`` -> ``{"kind": "drop", "when": "YYYY-MM-DDTHH:MM", "text": words}``
  ``? <words>``       -> ``{"kind": "talk", "text": words}``
  anything else       -> ``{"kind": "sort", "text": line}``

``<when>``: ``HH:MM`` / ``9am`` / ``5:30 pm`` (today if still ahead, else tomorrow) ·
``tomorrow [time]`` · ``mon..sun`` or full weekday ``[time]`` (the next one) ·
``MM-DD [time]`` · ``YYYY-MM-DD [time]`` · ``+Nm`` / ``+Nh`` / ``+Nd``. No time given
means 09:00. A ``<when>`` that does not read, a moment not in the future, an empty line,
or no words after the prefix raises :class:`IntakeRefused` with the reason: a parser
informs the human, it never guesses and never authorises.

Pure grammar: no store writes, no clock reads (``now`` is a parameter), no cockpit.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DEFAULT_DROP_HOUR = 9
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_WEEKDAY_FULL = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday",
                 "fri": "friday", "sat": "saturday", "sun": "sunday"}
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
_FMT = "%Y-%m-%dT%H:%M"


class IntakeRefused(ValueError):
    """The line cannot be read as written; the message says why. An intake never guesses."""


def _clock(token):
    m = _TIME_RE.match(token.strip())
    if not m or (m.group(2) is None and m.group(3) is None):
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    if h > 23 or mi > 59:
        return None
    return h, mi


def _take_clock(tokens, i):
    """((h, m), index after) for a time of day at tokens[i]; None if not time-shaped;
    IntakeRefused if time-shaped but unreadable. A bare number is a word, not a time."""
    if i >= len(tokens):
        return None
    tok = tokens[i].rstrip(",")
    after = tokens[i + 1].lower().rstrip(",") if i + 1 < len(tokens) else ""
    used = i + 1
    if after in ("am", "pm") and re.match(r"^\d{1,2}(:\d{2})?$", tok):
        tok, used = tok + after, i + 2
    if not (re.match(r"^\d", tok) and (":" in tok or tok.lower().endswith(("am", "pm")))):
        return None
    c = _clock(tok)
    if c is None:
        raise IntakeRefused(f"{tok!r} does not read as a time of day (use 14:30 or 2:30pm)")
    return c, used


def _resolve_when(tokens, now):
    if not tokens:
        raise IntakeRefused("no time after '@'. Try @09:00, @tomorrow 9am, @mon 14:30, @11-09 19:30 or @+2h")
    head = tokens[0].lower().rstrip(",")

    def with_clock(day, used):
        got = _take_clock(tokens, used)
        (h, mi), used = (got[0], got[1]) if got else ((DEFAULT_DROP_HOUR, 0), used)
        return datetime(day.year, day.month, day.day, h, mi), used

    m = re.match(r"^\+(\d+)([mhd])$", head)
    if m:
        n = int(m.group(1))
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[m.group(2)]
        return (now + delta).replace(second=0, microsecond=0), 1
    got = _take_clock(tokens, 0)
    if got:
        when = datetime(now.year, now.month, now.day, got[0][0], got[0][1])
        return (when if when > now else when + timedelta(days=1)), got[1]
    if head == "tomorrow":
        return with_clock((now + timedelta(days=1)).date(), 1)
    if head[:3] in _WEEKDAYS and head in (head[:3], _WEEKDAY_FULL[head[:3]]):
        ahead = (_WEEKDAYS.index(head[:3]) - now.weekday()) % 7
        when, used = with_clock((now + timedelta(days=ahead)).date(), 1)
        return (when if when > now else when + timedelta(days=7)), used
    m = re.match(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$", head)
    if m:
        try:
            day = date(int(m.group(1) or now.year), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise IntakeRefused(f"{tokens[0]!r} is not a calendar date") from None
        return with_clock(day, 1)
    raise IntakeRefused(f"cannot read {tokens[0]!r} as a time. Forms: HH:MM, 9am, tomorrow [time], mon..sun [time], "
                        f"MM-DD [time], YYYY-MM-DD [time], +30m, +2h, +3d (no time means {DEFAULT_DROP_HOUR:02d}:00)")


def parse_intake(line, now):
    """One typed line into what it asks for. Deterministic, writes nothing. ``now`` is local time.
    Raises :class:`IntakeRefused` (with the reason) for a line that cannot be read as written."""
    now = now.replace(second=0, microsecond=0)
    line = (line or "").strip()
    if not line:
        raise IntakeRefused("an empty line")
    if line.startswith("@"):
        body = line[1:]
        spans = [m.span() for m in re.finditer(r"\S+", body)]
        tokens = [body[a:b] for a, b in spans]
        when, used = _resolve_when(tokens, now)
        words = body[spans[used - 1][1]:].strip() if used <= len(spans) else ""
        if when <= now:
            raise IntakeRefused(f"{when:%Y-%m-%d %H:%M} is not in the future; a drop needs a moment still ahead")
        if not words:
            raise IntakeRefused("no words after the time: say what the drop should tell the desk")
        return {"kind": "drop", "when": when.strftime(_FMT), "text": words}
    if line.startswith("?"):
        words = line[1:].strip()
        if not words:
            raise IntakeRefused("no words after '?'")
        return {"kind": "talk", "text": words}
    return {"kind": "sort", "text": line}


def receipt(parsed, id=None):
    """The intake receipt for a parsed line: ``(line, dict)``.

    dict is ``{kind, id, fires, text}`` (``id`` is the spore id the caller planted, or None;
    ``fires`` is the ISO minute for a drop, else None). line is the one-line form:
    ``scheduled drop spore-N fires Mon 11-09 09:00`` · ``talk spore-N`` · ``sent to the desk to sort``.
    """
    kind = parsed["kind"]
    if kind == "drop":
        r = {"kind": "drop", "id": id, "fires": parsed["when"], "text": parsed["text"]}
        return f"scheduled drop {id} fires {datetime.strptime(r['fires'], _FMT):%a %m-%d %H:%M}", r
    if kind == "talk":
        return f"talk {id}", {"kind": "talk", "id": id, "fires": None, "text": parsed["text"]}
    return "sent to the desk to sort", {"kind": "sort", "id": None, "fires": None, "text": parsed["text"]}


def receipt_line(r):
    """One-line receipt from the receipt dict."""
    if r["kind"] == "drop":
        return f"scheduled drop {r['id']} fires {datetime.strptime(r['fires'], _FMT):%a %m-%d %H:%M}"
    if r["kind"] == "talk":
        return f"talk {r['id']}"
    return "sent to the desk to sort"
