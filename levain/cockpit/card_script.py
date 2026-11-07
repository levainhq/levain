"""The words a Needs-you card is read aloud with (voice V1-7): the NPCardVoice script, ported (flowConnect's
``Shared/NPCardVoice.swift`` before 1105-13 removed it; its pins were ``tools/verify_np_voice.swift``).

The headline verbatim (the card's first sentence: ``headline``, the same rule as ``card_format.js``), then each option as
``"<key>, <its full text>"``, and the rec's line ending ``", recommended"``. Those are the only words added. No options reads
the headline alone; an empty ``what`` reads nothing. Pure: no store, no network, no clock.

``levain/templates/web/card_voice.js`` builds the SAME script for the on-device voice; ``tests/test_card_voice.py`` pins the two
against each other on fixed cards (a node run of the page's file), so the premium route and the device cannot read two cards.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

CARD_ID = re.compile(r"question:([1-9][0-9]{0,11})")      # the cockpit's own id for a filed question (needsIdOf)
HEAD_MIN, HEAD_MAX, HEAD_CUT = 20, 200, 160               # card_format.js headline: a sentence end 20+ chars in, within 200

_SENTENCE_END = re.compile(r"[.!?](?=\s)")


def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def headline(text: str) -> str:
    """The first sentence of ``text`` (whitespace collapsed): up to its first ``.!?`` followed by a space that falls 20 or
    more characters in, looking no further than 200; none: a text over 200 characters is cut at a space near 160, any other
    is all headline."""
    t = _collapse(text)
    for m in _SENTENCE_END.finditer(t):
        cut = m.start() + 1
        if cut > HEAD_MAX:
            break
        if cut >= HEAD_MIN:
            return t[:cut]
    if len(t) > HEAD_MAX:
        r = t[:HEAD_CUT].rfind(" ")
        if r >= 0:
            return t[:r]
    return t


def script(what: str, options: Iterable[tuple[str, str]], rec: str | None) -> str:
    """The card's read-aloud script (see the module words). ``options`` are ``(key, text)`` pairs in the card's order."""
    lines: list[str] = []
    head = headline(what).strip()
    if head:
        lines.append(head)
    for key, text in options:
        text = str(text).strip()
        if not text:
            continue
        line = f"{key}, {text}"
        if rec and key == rec:
            line += ", recommended"
        lines.append(line)
    return "\n".join(lines)


def question_script(q: object) -> str:
    """The script of a filed question (``levain.consent.questions.Question``): its ``what``, ``options`` and ``rec``."""
    return script(getattr(q, "what", ""), getattr(q, "options", ()), getattr(q, "rec", "") or None)
