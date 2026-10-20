"""levain.unshowable: the one rule for "a character a person cannot see".

Every surface that shows agent-controlled text to the operator (a display marker, a terminal escape) and every rule
that refuses such text before it is signed or stored asks this module, so no two of them can disagree about which
characters hide. Each caller keeps its own output form (a marker, an escape, a refusal message, a strip) and its own
``allow`` set (which of newline and tab it tolerates); the set itself lives only here.

A code point is UNSHOWABLE when its general category is control, format, surrogate, line or paragraph separator,
private use or unassigned; when it is a space other than U+0020; when it is a Default_Ignorable_Code_Point; or when it
is U+2800 (the braille blank, a symbol that draws nothing).

One contextual exception: a single U+FE0E or U+FE0F (text / emoji presentation) directly after a showable character
is showable, so ordinary text such as "⚠️" passes. A selector at the start of the text, after another selector, or
after a character that is itself unshowable (newline and tab included, even where ``allow`` tolerates them) is
unshowable: a run of selectors could otherwise carry bits the reader never sees.

Stdlib only. It imports nothing from levain, so a module that must never reach ``levain.consent.hints`` (the cockpit's
display guard) can use it.
"""
from __future__ import annotations

import unicodedata
from typing import Callable

__all__ = ["first_hidden", "hidden", "is_unshowable", "mark"]

_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp", "Co", "Cn"})

# Unicode DerivedCoreProperties.txt, Default_Ignorable_Code_Point: characters a renderer may draw as nothing at all.
# Inclusive (lo, hi) ranges; Python's unicodedata does not expose the property, so it is written out here.
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_BRAILLE_BLANK = 0x2800
_PRESENTATION = frozenset("︎️")


def is_unshowable(ch: str) -> bool:
    """Whether the one character ``ch`` hides, on its own (no context: a presentation selector is always ``True``
    here; :func:`hidden` applies the exception for one after a showable character)."""
    cat = unicodedata.category(ch)
    if cat in _CATEGORIES or (cat == "Zs" and ch != " "):
        return True
    cp = ord(ch)
    if cp == _BRAILLE_BLANK:
        return True
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def hidden(text: str, allow: str = "") -> list[int]:
    """The indices in ``text`` of every unshowable character, in order. A character in ``allow`` (for example
    ``"\\n\\t"``) is never reported, but it still counts as unshowable for the presentation-selector exception."""
    out: list[int] = []
    prev_showable = False
    for i, ch in enumerate(text):
        if ch in _PRESENTATION and prev_showable:
            prev_showable = False           # the one selector is shown; a second in the run is not
            continue
        bad = is_unshowable(ch)
        prev_showable = not bad
        if bad and ch not in allow:
            out.append(i)
    return out


def first_hidden(text: str, allow: str = "") -> tuple[int, str] | None:
    """``(index, character)`` of the first unshowable character in ``text`` (``allow`` as in :func:`hidden`), or
    ``None`` when every character shows as itself."""
    found = hidden(text, allow)
    return None if not found else (found[0], text[found[0]])


def mark(text: str, fmt: str | Callable[[int], str] = "⟨U+{:04X}⟩", allow: str = "\n\t") -> str:
    """``text`` with every unshowable character (``allow`` as in :func:`hidden`) replaced by ``fmt``: a format string
    given the code point, or a callable from the code point to the replacement (``""`` strips it)."""
    found = hidden(text, allow)
    if not found:
        return text
    render = fmt if callable(fmt) else fmt.format
    at = set(found)
    return "".join(render(ord(ch)) if i in at else ch for i, ch in enumerate(text))
