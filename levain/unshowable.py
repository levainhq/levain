"""levain.unshowable: the one rule for "a character a person cannot see".

Every surface that shows agent-controlled text to the operator (a display marker, a terminal escape) and every rule
that refuses such text before it is signed or stored asks this module, so no two of them can disagree about which
characters hide. Each caller keeps its own output form (a marker, an escape, a refusal message, a strip) and its own
``allow`` set (which of newline and tab it tolerates); the set itself lives only here.

A code point is UNSHOWABLE when its general category is control, format, surrogate, line or paragraph separator,
private use or unassigned; when it is a space other than U+0020; when it is a Default_Ignorable_Code_Point; or when it
is one of the few assigned characters that draw nothing (``_DRAWS_NOTHING``).

Two contextual rules, applied by :func:`hidden`:

- Presentation selectors (U+FE0E text, U+FE0F emoji). In an identifier or a path (``selectors=False``) none is ever
  showable: UTS #39's identifier profile excludes every Default_Ignorable_Code_Point, and two names that differ only
  by a selector read alike. In prose (``selectors=True``, the default) a single selector directly after a base listed
  in Unicode's emoji-variation-sequences.txt (``_VARIATION_BASES``) is showable only when it CHANGES that base's
  default presentation (emoji-data.txt Emoji_Presentation): U+FE0F after a base that defaults to text
  (``"⚠️"``, ``"↔️"``), U+FE0E after one that defaults to emoji (``_EMOJI_DEFAULT``, ``"⌚︎"``). A selector that
  restates the default (``"↔︎"``, ``"⌚️"``) draws exactly what the bare base draws, so it is unshowable, as is a
  selector anywhere else (after any other character, at the start of the text, after another selector, after an
  unshowable character, newline and tab included even where ``allow`` tolerates them): selectors there could carry
  bits the reader never sees.
- A combining mark (category Mn or Me) is unshowable at the start of the text, right after a U+0020 space, or right
  after an unshowable character (newline and tab included): with no visible base it draws on nothing, or on a
  neighbour it was not written for. On one base, every mark past the ``_MAX_MARKS``-th is unshowable: a stack that
  deep draws over the line instead of decorating a letter. A presentation selector accepted by the first rule keeps
  its base, so a keycap ("1️⃣") passes in prose.

Linear time: table membership is a bisect, a run of printable ASCII is skipped in one regex step, and all-ASCII text
is one regex scan for its controls.

Stdlib only. It imports nothing from levain, so a module that must never reach ``levain.consent.hints`` (the cockpit's
display guard) can use it.
"""
from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from typing import Callable, Iterator

__all__ = ["describe", "first_hidden", "hidden", "is_unshowable", "mark"]

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

_TEXT_VS = "\ufe0e"     # VARIATION SELECTOR-15, text presentation
_EMOJI_VS = "\ufe0f"    # VARIATION SELECTOR-16, emoji presentation

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

# The variation bases above whose emoji-data.txt Emoji_Presentation is Yes (they draw as emoji by default, so U+FE0E
# is the selector that changes them); every other base defaults to text, so U+FE0F changes it.
# Source: emoji-data.txt, "# Version: 18.0.0" (Date: 2026-01-30), intersected with the bases above.
_EMOJI_DEFAULT = frozenset({
    0x231A, 0x231B, 0x23E9, 0x23EA, 0x23EB, 0x23EC, 0x23F0, 0x23F3, 0x25FD, 0x25FE, 0x2614, 0x2615, 0x2648, 0x2649,
    0x264A, 0x264B, 0x264C, 0x264D, 0x264E, 0x264F, 0x2650, 0x2651, 0x2652, 0x2653, 0x267F, 0x2693, 0x26A1, 0x26AA,
    0x26AB, 0x26BD, 0x26BE, 0x26C4, 0x26C5, 0x26CE, 0x26D4, 0x26EA, 0x26F2, 0x26F3, 0x26F5, 0x26FA, 0x26FD, 0x2705,
    0x270A, 0x270B, 0x2728, 0x274C, 0x274E, 0x2753, 0x2754, 0x2755, 0x2757, 0x2795, 0x2796, 0x2797, 0x27B0, 0x27BF,
    0x2B1B, 0x2B1C, 0x2B50, 0x2B55, 0x1F004, 0x1F21A, 0x1F22F, 0x1F30D, 0x1F30E, 0x1F30F, 0x1F315, 0x1F31C, 0x1F378,
    0x1F393, 0x1F3A7, 0x1F3AC, 0x1F3AD, 0x1F3AE, 0x1F3C2, 0x1F3C4, 0x1F3C6, 0x1F3CA, 0x1F3E0, 0x1F3ED, 0x1F408,
    0x1F415, 0x1F41F, 0x1F426, 0x1F442, 0x1F446, 0x1F447, 0x1F448, 0x1F449, 0x1F44D, 0x1F44E, 0x1F453, 0x1F46A,
    0x1F47D, 0x1F4A3, 0x1F4B0, 0x1F4B3, 0x1F4BB, 0x1F4BF, 0x1F4CB, 0x1F4DA, 0x1F4DF, 0x1F4E4, 0x1F4E5, 0x1F4E6,
    0x1F4EA, 0x1F4EB, 0x1F4EC, 0x1F4ED, 0x1F4F7, 0x1F4F9, 0x1F4FA, 0x1F4FB, 0x1F508, 0x1F50D, 0x1F512, 0x1F513,
    0x1F550, 0x1F551, 0x1F552, 0x1F553, 0x1F554, 0x1F555, 0x1F556, 0x1F557, 0x1F558, 0x1F559, 0x1F55A, 0x1F55B,
    0x1F55C, 0x1F55D, 0x1F55E, 0x1F55F, 0x1F560, 0x1F561, 0x1F562, 0x1F563, 0x1F564, 0x1F565, 0x1F566, 0x1F567,
    0x1F610, 0x1F687, 0x1F68D, 0x1F691, 0x1F694, 0x1F698, 0x1F6AD, 0x1F6B2, 0x1F6B9, 0x1F6BA, 0x1F6BC,
})

_MARKS = ("Mn", "Me")
_MAX_MARKS = 4

# A run of printable ASCII (U+0020..U+007E): every one shows, none is a mark or a selector, so the scan steps over it
# whole. The only unshowable ASCII characters are the C0 controls and DEL (category Cc).
_PLAIN_RUN = re.compile("[\x20-\x7e]+")
_ASCII_HIDDEN = re.compile("[\x00-\x1f\x7f]")
_ASCII_BASES = frozenset("#*0123456789")     # the ASCII variation bases (all default to text presentation)


def _in(table: tuple[tuple[int, int], ...]) -> Callable[[int], bool]:
    """Membership in a sorted table of inclusive, disjoint (lo, hi) ranges, by bisect."""
    los = tuple(lo for lo, _ in table)
    his = tuple(hi for _, hi in table)

    def contains(cp: int) -> bool:
        i = bisect_right(los, cp) - 1
        return i >= 0 and cp <= his[i]
    return contains


_ignorable = _in(_DEFAULT_IGNORABLE)
_variation_base = _in(_VARIATION_BASES)


def _alone(ch: str, cp: int, cat: str) -> bool:
    return (cat in _CATEGORIES or (cat == "Zs" and ch != " ") or cp in _DRAWS_NOTHING or _ignorable(cp))


def is_unshowable(ch: str) -> bool:
    """Whether the one character ``ch`` hides, on its own (no context: a presentation selector is always ``True``
    here, and an ordinary combining mark is ``False``; :func:`hidden` applies the contextual rules)."""
    return _alone(ch, ord(ch), unicodedata.category(ch))


def _scan(text: str, allow: str, selectors: bool) -> Iterator[int]:
    """The indices :func:`hidden` reports, lazily, in order."""
    if text.isascii():
        for m in _ASCII_HIDDEN.finditer(text):
            if m.group() not in allow:
                yield m.start()
        return
    n = len(text)
    prev_base = False       # the previous character may take one presentation selector
    prev_emoji = False      # ... and draws as emoji by default (Emoji_Presentation=Yes)
    prev_bad = True         # the previous character hid (the start of the text counts as hidden)
    prev_space = False      # the previous character was U+0020
    marks = 0               # combining marks already drawn on the current base
    plain = _PLAIN_RUN.match
    i = 0
    while i < n:
        run = plain(text, i)
        if run is not None:
            i = run.end()
            last = text[i - 1]
            prev_base, prev_emoji, prev_bad, prev_space, marks = last in _ASCII_BASES, False, False, last == " ", 0
            continue
        ch = text[i]
        if prev_base and selectors and ch == (_TEXT_VS if prev_emoji else _EMOJI_VS):
            prev_base = False               # the one selector changes the glyph; a second in the run does not
            i += 1
            continue                        # the base, its mark count and prev_bad carry over
        cp = ord(ch)
        if cp < 0x80:                       # not in a printable run: a C0 control or DEL
            bad, cat = True, "Cc"
        else:
            cat = unicodedata.category(ch)
            bad = _alone(ch, cp, cat)
        if not bad and cat in _MARKS:
            if prev_bad or prev_space or marks >= _MAX_MARKS:
                bad = True
            else:
                marks += 1
        elif not bad:
            marks = 0
        prev_base = not bad and _variation_base(cp)
        prev_emoji = prev_base and cp in _EMOJI_DEFAULT
        prev_bad = bad
        prev_space = False                  # U+0020 only ever arrives in a printable run
        if bad and ch not in allow:
            yield i
        i += 1


def hidden(text: str, allow: str = "", selectors: bool = True) -> list[int]:
    """The indices in ``text`` of every unshowable character, in order. A character in ``allow`` (for example
    ``"\\n\\t"``) is never reported, but it still counts as unshowable for both contextual rules. ``selectors=False``
    is the identifier and path rule: no presentation selector shows (UTS #39); the default is the prose rule."""
    return list(_scan(text, allow, selectors))


def first_hidden(text: str, allow: str = "", selectors: bool = True) -> tuple[int, str] | None:
    """``(index, character)`` of the first unshowable character in ``text`` (``allow`` and ``selectors`` as in
    :func:`hidden`), or ``None`` when every character shows as itself. Stops at the first one."""
    i = next(_scan(text, allow, selectors), None)
    return None if i is None else (i, text[i])


def mark(text: str, fmt: str | Callable[[int], str] = "⟨U+{:04X}⟩", allow: str = "\n\t",
         selectors: bool = True) -> str:
    """``text`` with every unshowable character (``allow`` and ``selectors`` as in :func:`hidden`) replaced by
    ``fmt``: a format string given the code point, or a callable from the code point to the replacement (``""``
    strips it). One pass over ``text``."""
    render = fmt if callable(fmt) else fmt.format
    parts: list[str] = []
    last = 0
    for i in _scan(text, allow, selectors):
        parts.append(text[last:i])
        parts.append(render(ord(text[i])))
        last = i + 1
    if not parts:
        return text
    parts.append(text[last:])
    return "".join(parts)


def describe(text: str, allow: str = "", selectors: bool = True) -> str | None:
    """The first unshowable character in ``text`` named for a refusal message (``U+202E (RIGHT-TO-LEFT OVERRIDE) at
    character 3``), or ``None``; ``allow`` and ``selectors`` as in :func:`hidden`."""
    found = first_hidden(text, allow, selectors=selectors)
    if found is None:
        return None
    i, ch = found
    return f"U+{ord(ch):04X} ({unicodedata.name(ch, 'unnamed')}) at character {i + 1}"
