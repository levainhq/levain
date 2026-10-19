"""levain.cockpit.display_safe: the guard on agent-controlled text any decision surface shows.

Invisible and format characters are shown as visible ``<U+XXXX>`` markers, so a bidi override cannot reorder or
impersonate what the human reads. This module imports nothing from ``levain.consent.hints``: the cockpit engine uses it
for the compact Decisions row, and that module must never reach the hints (a hint informs, never authorises).
"""
from __future__ import annotations

import unicodedata
from typing import Any

__all__ = ["visible"]

# blank-looking characters outside Cf/Cc/Zs: line and paragraph separators, Hangul fillers, braille blank,
# combining grapheme joiner, Mongolian free variation selectors
_BLANKS = "  ㅤ⠀ᅟᅠﾠ͏᠋᠌᠍"


def visible(text: Any) -> Any:
    """``text`` with every Unicode format or control character (Cf, Cc except newline and tab, Zs spaces other than
    U+0020, and the blank-looking ones in ``_BLANKS``) shown as a visible ``<U+XXXX>`` marker. Not a string: returned as is."""
    if not isinstance(text, str):
        return text
    return "".join(f"⟨U+{ord(ch):04X}⟩" if _hidden(ch) else ch for ch in text)


def _hidden(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return (cat in ("Cf", "Cc") and ch not in "\n\t") or ch in _BLANKS or (cat == "Zs" and ch != " ")
