"""Tapping the Home place while already on Home leaves the page where it is (real headless Chrome, a served cockpit, a trusted
mouse press on the Home button of the places bar). Away from Home the same tap still goes Home."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_list_page_browser import _reload, _seed_more

HOME = ".ia-places [data-ia-place-btn=home]"
SHORT = {"emulate": {"w": 1440, "h": 420, "dpr": 1}}   # a window shorter than the page, so the page has somewhere to scroll
SCROLL_TO = 300
PLACE = "document.body.dataset.iaPlace"
READ = f"JSON.stringify({{y: Math.round(window.scrollY), place: {PLACE}, max: Math.round(document.documentElement.scrollHeight - innerHeight)}})"
LATER = {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"}
# the driver's press scrolls its target to the middle of the window first, which moves a pinned button's page by itself: the button's
# own scrollIntoView is switched off so the page's reaction to the press is the only thing that can move the page
PRESS_HOME = {"tap": f"document.querySelector('{HOME}')",
              "before": f"document.querySelector('{HOME}').scrollIntoView = () => {{}}; 1"}


def _steps(tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [SHORT, {"until": "document.querySelector('[data-ck-panel=tray]') && 1"}, *_reload(),
            {"until": f"document.querySelector('{HOME}') && {PLACE} === 'home' && 1"}, LATER, *tail]


def _drive_page(tmp_path: Path, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _h):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]


@needs_browser
def test_a_tap_on_home_while_on_home_does_not_scroll_the_page(tmp_path: Path) -> None:
    got = _drive_page(tmp_path, _steps([
        {"eval": f"window.scrollTo(0, {SCROLL_TO}); 1"}, LATER, {"eval": READ},
        PRESS_HOME, LATER, {"eval": READ}]))
    before, after = got[-2], got[-1]
    assert before["place"] == "home" and before["y"] >= 100, before   # the page really is scrolled down before the tap
    assert after["place"] == "home" and after["y"] == before["y"], (before, after)


@needs_browser
def test_a_tap_on_home_from_another_place_still_goes_home(tmp_path: Path) -> None:
    got = _drive_page(tmp_path, _steps([
        {"tap": "document.querySelector('.ia-places [data-ia-place-btn=memory]')"}, LATER, {"eval": READ},
        PRESS_HOME, LATER, {"eval": READ}]))
    away, back = got[-2], got[-1]
    assert away["place"] == "memory", away
    assert back["place"] == "home", back
