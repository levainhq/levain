"""No cockpit stylesheet runs an infinite animation (10-15: one infinite trace animation kept the page producing a
frame every vsync, a full core on the operator's tab, measured by the flow cockpit-cpu probe)."""
from __future__ import annotations

import re
from pathlib import Path

import levain

WEB = Path(levain.__file__).parent / "templates" / "web"
# a declaration of either property, read across lines and in any case, comments removed first
DECL = re.compile(r"\banimation(?:-iteration-count)?\s*:[^;{}]*", re.IGNORECASE)


def _infinite_decls(css: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [d for d in DECL.findall(text) if re.search(r"\binfinite\b", d, re.IGNORECASE)]


def test_no_stylesheet_declares_an_infinite_animation():
    sheets = sorted(WEB.glob("*.css"))
    assert sheets, WEB
    hits = {css.name: found for css in sheets if (found := _infinite_decls(css.read_text(encoding="utf-8")))}
    assert not hits, hits


def test_the_reader_sees_a_split_or_uppercase_declaration():
    assert _infinite_decls("a { animation:\n  x 1s\n  INFINITE; }")
    assert _infinite_decls("a { animation-iteration-count:\n infinite }")
    assert not _infinite_decls("a { animation: x 1s 3; /* not infinite */ }")
