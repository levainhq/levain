"""The cockpit IA previews (levain#141): preview_home.html (A, Home + places) and preview_desk.html (B, Desk-first).

What these hold: each preview runs the live page's own code, its one bar holds places only, and the Needs-you count rides Home
on that bar at every width. What they cannot hold (the residue, judged by Phill on his phone and laptop): whether either page
feels right on his real data; the previews were looked at on his data at 390 and 1920 px by the seat that built them."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
PREVIEWS = {"preview_home.html": {"home", "desk", "room", "memory"}, "preview_desk.html": {"home", "room", "memory"}}


class _Tags(HTMLParser):
    """Every start tag, in order, with its attributes and the stack of open tags around it."""

    VOID = {"meta", "link", "br", "img", "input", "hr", "source", "area", "base", "col", "embed", "param", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict, tuple]] = []
        self.stack: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        self.tags.append((tag, a, tuple(self.stack)))
        if tag not in self.VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _parse(name: str) -> _Tags:
    p = _Tags()
    p.feed((WEB / name).read_text())
    return p


def _scripts(p: _Tags) -> list[str]:
    return [a["src"] for t, a, _ in p.tags if t == "script" and a.get("src")]


@pytest.mark.parametrize("name", sorted(PREVIEWS))
def test_preview_runs_the_live_pages_code_in_its_order(name: str) -> None:
    """A preview renders what the live page renders: every script dashboard.html loads, in the same order, then its own."""
    live = _scripts(_parse("dashboard.html"))
    mine = _scripts(_parse(name))
    assert mine[: len(live)] == live, f"{name} drifted from dashboard.html's scripts; regenerate it"
    assert mine[len(live):] == ["/preview_ia.js"]
    sheets = [a["href"] for t, a, _ in _parse(name).tags if t == "link" and a.get("rel") == "stylesheet"]
    assert sheets == ["/dashboard.css", "/preview_ia.css"]


@pytest.mark.parametrize("name", sorted(PREVIEWS))
def test_the_bar_holds_places_only(name: str) -> None:
    """One bar, places only; the Modes bar is gone; Drop, Grants and Notifications are buttons outside the bar."""
    p = _parse(name)
    in_bar = [a for t, a, stack in p.tags if t == "button" and any(s[1].get("id") == "ck-phonetabs" for s in stack)]
    assert {a.get("data-ia-place-btn") for a in in_bar} == PREVIEWS[name]
    assert not [a for a in in_bar if "data-phone-jump" in a or "data-mode-tab" in a]
    assert not [a for t, a, _ in p.tags if t == "nav" and "seg" in a.get("class", "").split()], "the Modes bar is back"
    visible_mode_tabs = [a for t, a, stack in p.tags if "data-mode-tab" in a and not any("hidden" in s[1] for s in stack)]
    assert not visible_mode_tabs, "a mode tab is drawn outside the hidden drivers"
    actions = {a.get("data-phone-jump") for t, a, stack in p.tags if t == "button" and any("ia-actions" in s[1].get("class", "") for s in stack)}
    assert actions == {"drop", "grants", "notifications"}


@pytest.mark.parametrize("name", sorted(PREVIEWS))
def test_needs_count_rides_home_at_every_width(name: str) -> None:
    """The Needs-you count is on the Home place in the bar, and the bar is drawn at every width (no media query hides it)."""
    p = _parse(name)
    home = [a for t, a, stack in p.tags if a.get("data-ia-place-btn") == "home" and any(s[1].get("id") == "ck-phonetabs" for s in stack)]
    assert len(home) == 1
    counts = [a for t, a, stack in p.tags if "data-nc-needs" in a]
    assert len(counts) == 1 and any(s[1].get("data-ia-place-btn") == "home" for s in
                                    next(stack for t, a, stack in p.tags if "data-nc-needs" in a))
    css = (WEB / "preview_ia.css").read_text()
    top_level = re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", re.sub(r"/\*.*?\*/", "", css, flags=re.S))
    assert re.search(r"body\.ia \.ia-places \{ display: flex !important; \}", top_level)
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert not re.search(r"\.ia-places[^{]*\{[^}]*display:\s*none", bare)
    # the count's own pill hides only when the count itself is hidden (nothing waits), never by width
    hides = [m.group(0) for m in re.finditer(r"[^{}]*(?:\.ia-needs|data-nc-needs)[^{]*\{[^}]*display:\s*none[^}]*\}", bare)]
    assert all(":has([data-nc-needs][hidden])" in h for h in hides), hides
