"""No cockpit stylesheet runs an infinite animation (10-15: one infinite trace animation kept the page producing a
frame every vsync, a full core on the operator's tab, measured by the flow cockpit-cpu probe)."""
from __future__ import annotations

import re
from pathlib import Path

import levain

WEB = Path(levain.__file__).parent / "templates" / "web"


def test_no_stylesheet_declares_an_infinite_animation():
    hits = [f"{css.name}:{n}" for css in sorted(WEB.glob("*.css"))
            for n, line in enumerate(css.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\banimation(-iteration-count)?\s*:[^;]*\binfinite\b", line)]
    assert sorted(WEB.glob("*.css")), WEB
    assert not hits, hits
