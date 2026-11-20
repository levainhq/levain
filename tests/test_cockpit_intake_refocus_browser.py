"""A board redraw that puts the keyboard back in the List's intake box does not move the page (real headless Chrome, a served cockpit
with a long board, trusted mouse presses). The page is drawn again when the tab comes back to the front; a person who had the box
focused still has it focused after the draw, and the page stays where they scrolled it. A focus the person gives the box on a phone
still brings the box to the middle of what the keyboard leaves."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anneal_memory.spores import IntentionStore

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TODAY, _install, _serve
from tests.test_list_page_browser import _reload, _seed_more

HOME = ".ia-places [data-ia-place-btn=home]"
FIELD = "document.querySelector('[data-ck-panel=tray] .list-intake-input')"
PLACE = "document.body.dataset.iaPlace"
LATER = {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"}
PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 1, "mobile": True}}
SCROLL_TO = 200
READ = f"JSON.stringify({{y: Math.round(window.scrollY), place: {PLACE}, drawn: window.__drawn === true, focused: document.activeElement === {FIELD}, " \
       f"max: Math.round(document.documentElement.scrollHeight - innerHeight)}})"
# the draw is over when the intake box on the page is not the one that was there before it
REDRAWN = f"(() => {{ const n = {FIELD}; return !!n && n !== window.__before && (window.__drawn = true) && 1; }})()"
# the driver's press scrolls its target to the middle of the window first: the Home button's own scrollIntoView is switched off, so the
# page's reaction to the press is the only thing that can move the page
PRESS_HOME = {"tap": f"document.querySelector('{HOME}')",
              "before": f"document.querySelector('{HOME}').scrollIntoView = () => {{}}; 1"}


def _seed_long_board(root: Path, n: int = 40) -> None:
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    for i in range(n):
        store.add(type="task", text=f"hot loop {i} with some words that make the line a little longer than a short one", tier="hot", today=TODAY)
        store.add(type="task", text=f"tray seed {i} with a fairly long description so that it wraps on a phone", disposition="seed", today=TODAY)


def _drive_page(tmp_path: Path, tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    _seed_long_board(root)
    steps = [PHONE, {"until": "document.querySelector('[data-ck-panel=tray]') && 1"}, *_reload(),
             {"until": f"{FIELD} && {PLACE} === 'home' && 1"}, LATER, *tail]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]


@needs_browser
def test_the_tab_coming_back_then_a_tap_on_home_leaves_the_page_where_it_was(tmp_path: Path) -> None:
    got = _drive_page(tmp_path, [
        {"tap": FIELD},                                  # the person put the keyboard in the box
        LATER, LATER,                                    # its move to the middle of the screen is over
        {"eval": f"window.scrollTo(0, {SCROLL_TO}); 1"}, LATER,
        {"eval": f"window.__before = {FIELD}; window.__drawn = false; 1"},
        {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"},   # the tab is in front again: the board is read and drawn
        {"until": REDRAWN},
        PRESS_HOME, LATER, LATER,
        {"eval": READ}])
    after = got[-1]
    assert after["drawn"] and after["place"] == "home", after
    assert after["max"] > SCROLL_TO + 300, after         # the page really has somewhere to move to
    assert after["y"] == SCROLL_TO, after


@needs_browser
def test_a_focus_the_person_gives_the_box_still_brings_it_to_the_middle(tmp_path: Path) -> None:
    got = _drive_page(tmp_path, [
        {"eval": f"window.scrollTo(0, {SCROLL_TO}); 1"}, LATER,
        {"eval": READ},
        {"eval": f"{FIELD}.focus({{preventScroll: true}}); 1"}, LATER, LATER,
        {"eval": READ}])
    before, after = got[-2], got[-1]
    assert before["y"] == SCROLL_TO, before
    assert after["y"] > before["y"] + 100, (before, after)
