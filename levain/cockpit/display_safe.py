"""levain.cockpit.display_safe: the guard on agent-controlled text any decision surface shows.

Invisible and format characters are shown as visible ``<U+XXXX>`` markers, so a bidi override cannot reorder or
impersonate what the human reads. This module imports nothing from ``levain.consent.hints``: the cockpit engine uses it
for the compact Decisions row, and that module must never reach the hints (a hint informs, never authorises).
"""
from __future__ import annotations

from typing import Any

from levain import unshowable

__all__ = ["visible"]


def visible(text: Any) -> Any:
    """``text`` with every unshowable character (:mod:`levain.unshowable`; newline and tab are kept) shown as a
    visible ``⟨U+XXXX⟩`` marker. Not a string: returned as is."""
    if not isinstance(text, str):
        return text
    return unshowable.mark(text, "⟨U+{:04X}⟩", allow="\n\t")
