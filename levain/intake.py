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

Pure grammar: no store writes, no clock reads (``now`` is a parameter), no cockpit.
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


def receipt(parsed, id=None):
    """The intake receipt for a parsed line: ``(line, dict)``.

    dict is ``{kind, id, fires, text}`` (``id`` is the intention id the caller planted, or None;
    ``fires`` is the ISO minute for a drop, else None). line is the one-line form:
    ``scheduled drop <id> fires Mon 11-09 09:00`` · ``talk <id>`` (``<id>`` is ``intention-N``, or ``spore-N`` on a row made before anneal 1.0.7) · ``sent to the desk to sort``.
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
