"""dashboard.css closes every block it opens.

A merge that keeps two appended blocks can drop one shared closing brace; the browser then reads every rule after it as part of the
unclosed @media and the page silently loses them (11-19, levain#141 polish: the Home order rules sat inside a 1101-1180 px query)."""

from __future__ import annotations

from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css"


def _depths(src: str) -> tuple[int, list[int]]:
    """End depth, and every line where a top-level section comment ('/* ──') opens inside a block."""
    depth, i, line, inside = 0, 0, 1, []
    while i < len(src):
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = len(src) if j < 0 else j + 2
            if src.startswith("/* ──", i) and depth:
                inside.append(line)
            line += src.count("\n", i, j)
            i = j
            continue
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < len(src) and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            line += src.count("\n", i, j)
            i = j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            assert depth >= 0, f"dashboard.css closes a block it never opened at line {line}"
        elif c == "\n":
            line += 1
        i += 1
    return depth, inside


def test_dashboard_css_closes_every_block_and_no_section_opens_inside_one() -> None:
    depth, inside = _depths(CSS.read_text())
    assert depth == 0, f"dashboard.css ends {depth} block(s) deep"
    assert not inside, f"a '/* ──' section opens inside an unclosed block at line(s) {inside}"


def test_the_check_sees_a_dropped_brace() -> None:
    depth, inside = _depths("@media (x) {\n  a { b: c; }\n/* ── next ── */\nd { e: f; }\n")
    assert depth == 1 and inside == [3]
