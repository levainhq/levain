"""levain.unshowable: the one rule for "a character a person cannot see".

Every surface that shows agent-controlled text to the operator (a display marker, a terminal escape) and every rule
that refuses such text before it is signed or stored asks this module, so no two of them can disagree about which
characters hide. Each caller keeps its own output form (a marker, an escape, a refusal message, a strip) and its own
``allow`` set (which of newline and tab it tolerates); the set itself lives only here.

A code point is UNSHOWABLE when its general category is control, format, surrogate, line or paragraph separator,
private use or unassigned; when it is a space other than U+0020; when it is a Default_Ignorable_Code_Point; or when it
is one of the few assigned characters that draw nothing (``_DRAWS_NOTHING``).

Two contextual rules, applied by :func:`hidden`:

- A single U+FE0E or U+FE0F (text / emoji presentation) directly after a base listed in Unicode's
  emoji-variation-sequences.txt (``_VARIATION_BASES``) is showable, so ordinary text such as "⚠️" passes: there the
  selector visibly changes the glyph. Anywhere else (after any other character, at the start of the text, after
  another selector, after an unshowable character, newline and tab included even where ``allow`` tolerates them) it is
  unshowable: selectors there could carry bits the reader never sees.
- A combining mark (category Mn or Me) is unshowable at the start of the text, right after a U+0020 space, or right
  after an unshowable character (newline and tab included): with no visible base it draws on nothing, or on a
  neighbour it was not written for. On one base, every mark past the ``_MAX_MARKS``-th is unshowable: a stack that
  deep draws over the line instead of decorating a letter. A presentation selector accepted by the first rule keeps
  its base, so a keycap ("1️⃣") passes.

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

# Assigned characters outside every category above that still draw nothing.
_DRAWS_NOTHING = frozenset({
    0x2800,     # BRAILLE PATTERN BLANK
    0x16FE4,    # KHITAN SMALL SCRIPT FILLER
    0x1D159,    # MUSICAL SYMBOL NULL NOTEHEAD
})

_PRESENTATION = frozenset("︎️")

# Unicode emoji-variation-sequences.txt, "# Version: 18.0.0" (Date: 2026-01-09): every base that has a text and an
# emoji form (each has both an FE0E and an FE0F sequence there). Inclusive (lo, hi) ranges.
_VARIATION_BASES = (
    (0x0023, 0x0023), (0x002A, 0x002A), (0x0030, 0x0039), (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C),
    (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B),
    (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2604), (0x260E, 0x260E), (0x2611, 0x2611),
    (0x2614, 0x2615), (0x2618, 0x2618), (0x261D, 0x261D), (0x2620, 0x2620), (0x2622, 0x2623), (0x2626, 0x2626),
    (0x262A, 0x262A), (0x262E, 0x262F), (0x2638, 0x263A), (0x2640, 0x2640), (0x2642, 0x2642), (0x2648, 0x2653),
    (0x265F, 0x2660), (0x2663, 0x2663), (0x2665, 0x2666), (0x2668, 0x2668), (0x267B, 0x267B), (0x267E, 0x267F),
    (0x2692, 0x2697), (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1), (0x26A7, 0x26A7), (0x26AA, 0x26AB),
    (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5), (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1),
    (0x26D3, 0x26D4), (0x26E9, 0x26EA), (0x26F0, 0x26F5), (0x26F7, 0x26FA), (0x26FD, 0x26FD), (0x2702, 0x2702),
    (0x2705, 0x2705), (0x2708, 0x270D), (0x270F, 0x270F), (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716),
    (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2763, 0x2764), (0x2795, 0x2797),
    (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F004, 0x1F004), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F), (0x1F202, 0x1F202), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F237, 0x1F237), (0x1F30D, 0x1F30F), (0x1F315, 0x1F315), (0x1F31C, 0x1F31C),
    (0x1F321, 0x1F321), (0x1F324, 0x1F32C), (0x1F336, 0x1F336), (0x1F378, 0x1F378), (0x1F37D, 0x1F37D),
    (0x1F393, 0x1F393), (0x1F396, 0x1F397), (0x1F399, 0x1F39B), (0x1F39E, 0x1F39F), (0x1F3A7, 0x1F3A7),
    (0x1F3AC, 0x1F3AE), (0x1F3C2, 0x1F3C2), (0x1F3C4, 0x1F3C4), (0x1F3C6, 0x1F3C6), (0x1F3CA, 0x1F3CE),
    (0x1F3D4, 0x1F3E0), (0x1F3ED, 0x1F3ED), (0x1F3F3, 0x1F3F3), (0x1F3F5, 0x1F3F5), (0x1F3F7, 0x1F3F7),
    (0x1F408, 0x1F408), (0x1F415, 0x1F415), (0x1F41F, 0x1F41F), (0x1F426, 0x1F426), (0x1F43F, 0x1F43F),
    (0x1F441, 0x1F442), (0x1F446, 0x1F449), (0x1F44D, 0x1F44E), (0x1F453, 0x1F453), (0x1F46A, 0x1F46A),
    (0x1F47D, 0x1F47D), (0x1F4A3, 0x1F4A3), (0x1F4B0, 0x1F4B0), (0x1F4B3, 0x1F4B3), (0x1F4BB, 0x1F4BB),
    (0x1F4BF, 0x1F4BF), (0x1F4CB, 0x1F4CB), (0x1F4DA, 0x1F4DA), (0x1F4DF, 0x1F4DF), (0x1F4E4, 0x1F4E6),
    (0x1F4EA, 0x1F4ED), (0x1F4F7, 0x1F4F7), (0x1F4F9, 0x1F4FB), (0x1F4FD, 0x1F4FD), (0x1F508, 0x1F508),
    (0x1F50D, 0x1F50D), (0x1F512, 0x1F513), (0x1F549, 0x1F54A), (0x1F550, 0x1F567), (0x1F56F, 0x1F570),
    (0x1F573, 0x1F579), (0x1F587, 0x1F587), (0x1F58A, 0x1F58D), (0x1F590, 0x1F590), (0x1F5A5, 0x1F5A5),
    (0x1F5A8, 0x1F5A8), (0x1F5B1, 0x1F5B2), (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4), (0x1F5D1, 0x1F5D3),
    (0x1F5DC, 0x1F5DE), (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3), (0x1F5E8, 0x1F5E8), (0x1F5EF, 0x1F5EF),
    (0x1F5F3, 0x1F5F3), (0x1F5FA, 0x1F5FA), (0x1F610, 0x1F610), (0x1F687, 0x1F687), (0x1F68D, 0x1F68D),
    (0x1F691, 0x1F691), (0x1F694, 0x1F694), (0x1F698, 0x1F698), (0x1F6AD, 0x1F6AD), (0x1F6B2, 0x1F6B2),
    (0x1F6B9, 0x1F6BA), (0x1F6BC, 0x1F6BC), (0x1F6CB, 0x1F6CB), (0x1F6CD, 0x1F6CF), (0x1F6E0, 0x1F6E5),
    (0x1F6E9, 0x1F6E9), (0x1F6F0, 0x1F6F0), (0x1F6F3, 0x1F6F3),
)

_MARKS = ("Mn", "Me")
_MAX_MARKS = 4


def _variation_base(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _VARIATION_BASES)


def is_unshowable(ch: str) -> bool:
    """Whether the one character ``ch`` hides, on its own (no context: a presentation selector is always ``True``
    here, and an ordinary combining mark is ``False``; :func:`hidden` applies the contextual rules)."""
    cat = unicodedata.category(ch)
    if cat in _CATEGORIES or (cat == "Zs" and ch != " "):
        return True
    cp = ord(ch)
    if cp in _DRAWS_NOTHING:
        return True
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def hidden(text: str, allow: str = "") -> list[int]:
    """The indices in ``text`` of every unshowable character, in order. A character in ``allow`` (for example
    ``"\\n\\t"``) is never reported, but it still counts as unshowable for both contextual rules."""
    out: list[int] = []
    prev_base = False       # the previous character may take one presentation selector
    prev_bad = True         # the previous character hid (the start of the text counts as hidden)
    prev_space = False      # the previous character was U+0020
    marks = 0               # combining marks already drawn on the current base
    for i, ch in enumerate(text):
        if ch in _PRESENTATION and prev_base:
            prev_base = False               # the one selector is shown; a second in the run is not
            continue                        # the base, its mark count and prev_bad carry over
        bad = is_unshowable(ch)
        if not bad and unicodedata.category(ch) in _MARKS:
            if prev_bad or prev_space or marks >= _MAX_MARKS:
                bad = True
            else:
                marks += 1
        elif not bad:
            marks = 0
        prev_base = not bad and _variation_base(ord(ch))
        prev_bad = bad
        prev_space = ch == " "
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
