"""The cockpit page's places bar (levain#141): Home, Desk, Room, Memory, and nothing else in the bar.

What these hold: the page's one bar holds places only, the Needs-you count rides Home on that bar at every width, the page runs its
scripts in order with dashboard_places.js last, and a real browser lands on a place, moves between places, takes the 1-4 keys, and
sends a stored retired mode (Morning, Build) to Home. What they cannot hold (judged by looking at the page on his phone and laptop):
whether the arrangement of Home feels right on his real data."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"
PLACES = {"home", "desk", "room", "memory"}


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


def _parse() -> _Tags:
    p = _Tags()
    p.feed((WEB / "dashboard.html").read_text())
    return p


def _in_bar(stack: tuple) -> bool:
    return any(s[1].get("id") == "ck-phonetabs" for s in stack)


def test_the_page_runs_the_places_script_after_the_board_painters() -> None:
    """The places script reads the page's own state, so it loads after every script that draws or wires it; the page links one stylesheet."""
    p = _parse()
    scripts = [a["src"] for t, a, _ in p.tags if t == "script" and a.get("src")]
    assert scripts[-2:] == ["/board_view.js", "/dashboard_places.js"]
    assert scripts.count("/dashboard_places.js") == 1
    sheets = [a["href"] for t, a, _ in p.tags if t == "link" and a.get("rel") == "stylesheet"]
    assert sheets == ["/dashboard.css"]


def test_the_tagline_is_static_text_in_the_visible_element() -> None:
    """The line under the wordmark sits in its own element (the page hides `.model`, which a manifest's brand also overwrites)."""
    p = _parse()
    html = (WEB / "dashboard.html").read_text()
    assert [a for t, a, _ in p.tags if "ia-tagline" in a.get("class", "").split()] == [{"class": "ia-tagline", "id": "ia-tagline"}]
    assert '<div class="ia-tagline" id="ia-tagline">what needs you, what is moving, what it remembers</div>' in html


def test_the_bar_holds_places_only() -> None:
    """One bar, places only; the Modes bar is gone; Drop, Grants and Notifications are buttons outside the bar."""
    p = _parse()
    in_bar = [a for t, a, stack in p.tags if t == "button" and _in_bar(stack)]
    assert {a.get("data-ia-place-btn") for a in in_bar} == PLACES
    assert not [a for a in in_bar if "data-phone-jump" in a or "data-mode-tab" in a]
    assert not [a for t, a, _ in p.tags if t == "nav" and "seg" in a.get("class", "").split()], "the Modes bar is back"
    visible_mode_tabs = [a for t, a, stack in p.tags if "data-mode-tab" in a and not any("hidden" in s[1] for s in stack)]
    assert not visible_mode_tabs, "a mode tab is drawn outside the hidden drivers"
    actions = {a.get("data-phone-jump") for t, a, stack in p.tags
               if t == "button" and any("ia-actions" in s[1].get("class", "") for s in stack)}
    assert actions == {"drop", "grants", "notifications"}


def test_needs_count_rides_home_at_every_width() -> None:
    """The Needs-you count is on the Home place in the bar, and the bar is drawn at every width (no media query hides it)."""
    p = _parse()
    home = [a for t, a, stack in p.tags if a.get("data-ia-place-btn") == "home" and _in_bar(stack)]
    assert len(home) == 1
    counts = [stack for t, a, stack in p.tags if "data-nc-needs" in a]
    assert len(counts) == 1 and any(s[1].get("data-ia-place-btn") == "home" for s in counts[0])
    css = (WEB / "dashboard.css").read_text()
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "body.ia .ia-places { display: flex !important; }" in re.sub(r"@media[^{]*\{(?:[^{}]*\{[^}]*\})*[^{}]*\}", "", bare)
    assert not re.search(r"\.ia-places[^{]*\{[^}]*display:\s*none", bare)
    # the count's own pill hides only when the count itself is hidden (nothing waits), never by width
    hides = [m.group(0) for m in re.finditer(r"[^{}]*(?:\.ia-needs|data-nc-needs)[^{]*\{[^}]*display:\s*none[^}]*\}", bare)]
    assert all(":has([data-nc-needs][hidden])" in h for h in hides), hides


def test_the_places_script_is_served_as_app_shell(tmp_path: Path) -> None:
    """The page names the script by the digest of the bytes served (1105-17), as it does every shell script."""
    from tests.test_cockpit_board import TOKEN, _raw
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, hdrs, data = _raw(base, "/dashboard_places.js", token=None)
        _s, _h2, page = _raw(base, "/", token=None)
    assert status == 200 and hdrs["content-type"].startswith("text/javascript")
    assert b'<script src="/dashboard_places.js?v=%s"></script>' % hashlib.sha256(data).hexdigest()[:12].encode() in page
    assert b"innerHTML" not in data


STATE = """(() => ({
  place: document.body.dataset.iaPlace,
  bar: [...document.querySelectorAll('.ia-places [data-ia-place-btn]')].map(b => b.dataset.iaPlaceBtn),
  current: [...document.querySelectorAll('[aria-current="page"]')].map(b => b.dataset.iaPlaceBtn || b.id || b.tagName),
  mode: document.body.dataset.mode || '',
  tagline: Math.round(document.querySelector('.ia-tagline').getBoundingClientRect().height),
  modeTabsShown: [...document.querySelectorAll('[data-mode-tab]')].filter(b => b.getClientRects().length).length,
}))()"""
PRESS = "document.querySelector('.ia-places [data-ia-place-btn=\"%s\"]').click(); 1"
SETTLE = {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}
READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ia-places [data-ia-place-btn]') && 1"}


@needs_browser
def test_a_browser_lands_on_home_moves_between_places_and_takes_the_keys(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE,
            {"eval": STATE},                                    # 0: the landing
            {"eval": PRESS % "memory"}, SETTLE,
            {"eval": STATE},                                    # 1: Memory by the bar
            {"eval": PRESS % "home"}, SETTLE,
            {"eval": STATE},                                    # 2: Home again
            {"eval": "document.dispatchEvent(new KeyboardEvent('keydown', {key: '4', bubbles: true, cancelable: true})); 1"}, SETTLE,
            {"eval": STATE},                                    # 3: key 4 is Memory
            {"eval": "document.dispatchEvent(new KeyboardEvent('keydown', {key: '1', bubbles: true, cancelable: true})); 1"}, SETTLE,
            {"eval": STATE},                                    # 4: key 1 is Home
            {"emulate": {"w": 390, "h": 800, "dpr": 1, "mobile": True}}, SETTLE,
            {"eval": STATE},                                    # 5: the phone width
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert len(states) == 6, out["values"]
    landing, memory, home, key4, key1, phone = states
    assert landing["bar"] == ["home", "desk", "room", "memory"]
    assert landing["place"] == "home" and landing["current"] == ["home"]
    assert landing["modeTabsShown"] == 0 and landing["tagline"] > 0
    assert memory["place"] == "memory" and memory["current"] == ["memory"] and memory["mode"] == "memory"
    assert home["place"] == "home" and home["current"] == ["home"]
    assert key4["place"] == "memory" and key4["mode"] == "memory"
    assert key1["place"] == "home"
    assert phone["tagline"] > 0 and phone["bar"] == ["home", "desk", "room", "memory"], phone


@needs_browser
def test_a_stored_retired_mode_lands_on_home(tmp_path: Path) -> None:
    """Morning and Build are retired; a mode stored by an older page sends the page to Home, not to a mode with no tab."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY,
            {"eval": "localStorage.setItem('levain_cockpit_mode', 'morning'); window.__old = 1; location.reload(); 1"},
            {"until": "!window.__old && document.body.dataset.iaPlace && 1"}, SETTLE,
            {"eval": STATE},
            {"eval": "location.hash = '#build'; 1"}, SETTLE,
            {"eval": STATE},
        ])
    states = [v for v in out["values"] if isinstance(v, dict) and "place" in v]
    assert [s["place"] for s in states] == ["home", "home"], out["values"]
    assert all(s["mode"] not in ("morning", "build") for s in states), states
