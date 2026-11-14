"""levain.intake — THE LIST's one-line intake grammar (pure, deterministic, no model).

Ported from flow's ``intentions.parse_intake`` (task_surfaces_1107.md, THE LIST). Parity
contract: ``tests/vectors/list_intake_vectors.json``.

  ``@<when> <words>``                      -> ``{"kind": "drop", "when": "YYYY-MM-DDTHH:MM", "text": words}``
  ``remind [me] <when|every ...> [to] words`` -> ``{"kind": "remind", "when": ..., "text": words[, "recur": canon]}``
  ``? <words>``                            -> ``{"kind": "talk", "text": words}``
  anything else                            -> ``{"kind": "sort", "text": line}``

The ``<when>`` grammar (and the repeating phrases only a ``remind`` may use) is :mod:`levain.when_parse`. A ``<when>`` that does
not read, a moment not in the future, an empty line, or no words after the prefix raises :class:`IntakeRefused` with the
reason: a parser informs the human, it never guesses and never authorises.

Pure grammar: no store writes, no cockpit; the parse reads no clock (``now`` is a parameter). A receipt names the year of a moment outside ``now``'s year, and reads the local clock only when no ``now`` is given.
"""

from __future__ import annotations

from datetime import datetime

from levain.when_parse import DEFAULT_HOUR as DEFAULT_DROP_HOUR, WhenRefused, is_remind, parse_remind, read_when_text  # noqa: F401

_FMT = "%Y-%m-%dT%H:%M"


class IntakeRefused(ValueError):
    """The line cannot be read as written; the message says why. An intake never guesses."""


def parse_intake(line, now):
    """One typed line into what it asks for. Deterministic, writes nothing. ``now`` is local time.
    Raises :class:`IntakeRefused` (with the reason) for a line that cannot be read as written. The time grammar lives in
    :mod:`levain.when_parse`; this reads the line's prefix and words."""
    now = now.replace(second=0, microsecond=0)
    line = (line or "").strip()
    if not line:
        raise IntakeRefused("an empty line")
    try:
        if line.startswith("@"):
            if not line[1:].strip():
                raise IntakeRefused("no time after '@'. Try @09:00, @tomorrow 9am, @mon 14:30, @11-09 19:30 or @+2h")
            when, words = read_when_text(line[1:], now, allow_recur=False)
            if not words:
                raise IntakeRefused("no words after the time: say what the drop should tell the desk")
            return {"kind": "drop", "when": when.iso, "text": words}
        remind = parse_remind(line, now)
    except WhenRefused as exc:
        raise IntakeRefused(str(exc)) from None
    if remind is not None:
        return remind
    if line.startswith("?"):
        words = line[1:].strip()
        if not words:
            raise IntakeRefused("no words after '?'")
        return {"kind": "talk", "text": words}
    return {"kind": "sort", "text": line}


def when_text(fires, now=None):
    """A stored minute (``YYYY-MM-DDTHH:MM`` or a datetime) as ``Mon 11-09 09:00``; the year is named (``Sun 2027-11-14 09:00``)
    when it is not ``now``'s year. Why: a date typed without a year means its next occurrence, so one typed at 09:01 for 09:00
    today lands next year, and a receipt that omits the year reads as set for today (levain#1729). ``now`` defaults to the
    local clock: a receipt is shown at the moment it is made."""
    dt = fires if isinstance(fires, datetime) else datetime.strptime(fires, _FMT)
    now = now or datetime.now()
    if now.tzinfo is not None:
        now = now.astimezone()              # the stored minute is local time: so is the year it is compared with
    if dt.year != now.year:
        return f"{dt:%a %Y-%m-%d %H:%M}"
    return f"{dt:%a %m-%d %H:%M}"


def receipt(parsed, id=None, now=None):
    """The intake receipt for a parsed line: ``(line, dict)``.

    dict is ``{kind, id, fires, text}`` (``id`` is the intention id the caller planted, or None;
    ``fires`` is the ISO minute for a drop or a reminder, else None; a reminder's dict also carries ``recur``, its canon rule or
    None). line is the one-line form:
    ``scheduled drop <id> fires Mon 11-09 09:00`` · ``reminder <id> rings Thu 11-12 15:00[, then weekly SUN 21:00]`` · ``talk <id>`` (``<id>`` is ``intention-N``, or ``spore-N`` on a row made before anneal 1.0.7) · ``sent to the desk to sort``.
    """
    kind = parsed["kind"]
    if kind == "drop":
        r = {"kind": "drop", "id": id, "fires": parsed["when"], "text": parsed["text"]}
        return f"scheduled drop {id} fires {when_text(r['fires'], now)}", r
    if kind == "talk":
        return f"talk {id}", {"kind": "talk", "id": id, "fires": None, "text": parsed["text"]}
    if kind == "remind":
        r = {"kind": "remind", "id": id, "fires": parsed["when"], "recur": parsed.get("recur"), "text": parsed["text"]}
        return receipt_line(r, now), r
    return "sent to the desk to sort", {"kind": "sort", "id": None, "fires": None, "text": parsed["text"]}


def receipt_line(r, now=None):
    """One-line receipt from the receipt dict."""
    if r["kind"] == "drop":
        return f"scheduled drop {r['id']} fires {when_text(r['fires'], now)}"
    if r["kind"] == "remind":
        return (f"reminder {r['id']} rings {when_text(r['fires'], now)}"
                + (f", then {r['recur']}" if r.get("recur") else ""))
    if r["kind"] == "talk":
        return f"talk {r['id']}"
    return "sent to the desk to sort"
