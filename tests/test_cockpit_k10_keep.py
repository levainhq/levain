"""K10 KEEP (Phill 10-20, his words): "a spore expanded and I am reading it, a page update closes it; the list scrolled down
reloads back to the top". A spore row the reader opens stays open (and visible, and closable) and its body keeps its scroll
across the refresh button and across a real store write seen through the change stream.

The K10 board folds the rows that do not fit a card (`foldRows`); a row that grew past the card on the reader's click used to be
folded away with the rest, so the open spore vanished on the click and on every redraw. Driven in headless Chrome against a
served cockpit with a long Keep: a tall spore at the top and thirty more under it."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

from anneal_memory.spores import SporeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TODAY, _install
from tests.test_cockpit_k4a_needs import DESKTOP
from tests.test_cockpit_scroll_keep_page import _serve_plain

PARA = "The whole situation, what was tried, what failed, who owns it and what would close it. " * 6
GIANT = "GIANTKEEP " + "\n\n".join(f"{i}. {PARA}" for i in range(8))
PANEL = "document.querySelector('[data-ck-panel=keep]')"
BODY = f"{PANEL}.querySelector('.pbody')"
# the tall row, the way a reader sees it: open or not, drawn or folded away, and how far the card's body is scrolled
GIANT_ROW = f"[...{PANEL}.querySelectorAll('.row')].find(r => r.textContent.indexOf('GIANTKEEP') >= 0)"
STATE = (f"(() => {{ const g = {GIANT_ROW}, b = {BODY}; if (!g) return null;"
         " const t = g.querySelector(':scope > .clause-toggle');"
         " return {open: g.classList.contains('row-expanded'), drawn: g.offsetHeight > 0,"
         "  closable: !!t && t.getAttribute('aria-expanded') === 'true' && getComputedStyle(t).display !== 'none',"
         "  scroll: Math.round(b.scrollTop), room: b.scrollHeight - b.clientHeight}; })()")
OPEN_AND_SCROLL = ("(async () => { const sleep = (ms) => new Promise(r => setTimeout(r, ms));"
                   f" const g = {GIANT_ROW}; g.querySelector(':scope > .clause-toggle').click(); await sleep(500);"
                   f" const b = {BODY}; b.scrollTop = 500; await sleep(300);"
                   f" return {{scrolled: Math.round(b.scrollTop), state: ({STATE})}}; }})()")
COUNT_RENDERS = ("window.__r = 0; const L = window.LevainDashboard, o = L.render; "
                 "L.render = function () { try { return o.apply(this, arguments); } finally { window.__r++; } }; 1")


def _long_keep(tmp_path: Path) -> tuple[Path, Any]:
    root, src, _ids = _install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    for i in range(30):
        store.add(type="task", text=f"KEEPBULK {i:02d} a reference note " + PARA, disposition="note", today=TODAY - timedelta(days=i % 5))
    store.add(type="task", text=GIANT, disposition="note", today=TODAY + timedelta(days=2))   # newest: the first row of the Keep
    return root, src


def _assert_held(opened: dict[str, Any], after: Any, why: str) -> None:
    before, scrolled = opened["state"], opened["scrolled"]
    assert before and before["drawn"] and before["open"] and before["closable"], f"the tall row did not stay open and drawn on the click: {before}"
    assert before["room"] > 100, f"the card's body does not scroll, the scroll check proves nothing: {before}"
    assert scrolled > 100, f"the body was never scrolled: {scrolled}"
    assert after and after["drawn"] and after["open"] and after["closable"], f"{why}: the open spore was closed or folded away: {after}"
    assert abs(after["scroll"] - scrolled) <= 3, f"{why}: the scroll moved from {scrolled} to {after['scroll']}"


@needs_browser
def test_the_refresh_button_keeps_a_tall_open_spore_and_its_scroll(tmp_path: Path) -> None:
    _root, src = _long_keep(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            DESKTOP,
            {"until": f"{PANEL} && {GIANT_ROW} && {GIANT_ROW}.classList.contains('has-more') && 1", "ms": 30000},
            {"eval": COUNT_RENDERS},
            {"eval": OPEN_AND_SCROLL},
            {"eval": "document.getElementById('refresh').click(); 1"},
            {"until": "window.__r >= 1 && 1"},
            {"eval": "new Promise(r => setTimeout(r, 800))"},
            {"eval": STATE},
        ])
    v = out["values"]
    _assert_held(v[2], v[-1], "the refresh button")


@needs_browser
def test_a_store_write_keeps_a_tall_open_spore_and_its_scroll(tmp_path: Path) -> None:
    """The change stream path: a seat writes a spore while the page is open, the Keep moves, the board is drawn again."""
    root, src = _long_keep(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    stop = threading.Event()

    def writer() -> None:
        n = 0
        while not stop.wait(1.5):
            n += 1
            store.add(type="task", text=f"written while open {n}", disposition="note", today=TODAY)

    t = threading.Thread(target=writer, daemon=True)
    with _serve_plain(src) as url:
        t.start()
        try:
            out = _drive(tmp_path, f"{url}/", [
                DESKTOP,
                {"until": f"{PANEL} && {GIANT_ROW} && {GIANT_ROW}.classList.contains('has-more') && 1", "ms": 30000},
                {"eval": COUNT_RENDERS},
                {"eval": OPEN_AND_SCROLL},
                {"eval": "window.__r = 0; 1"},   # only a draw AFTER the row was opened and scrolled counts
                {"until": "window.__r >= 1 && 1", "ms": 40000},
                {"eval": "new Promise(r => setTimeout(r, 800))"},
                {"eval": STATE},
            ])
        finally:
            stop.set()
            t.join(timeout=5)
    v = out["values"]
    assert v[4] is True or v[4] == 1, f"the board was never drawn again: {v}"
    _assert_held(v[2], v[-1], "a store write")


AFFORD = (f"(() => {{ const b = {BODY}; return {{tab: b.getAttribute('tabindex'), role: b.getAttribute('role'),"
          " label: b.getAttribute('aria-label') || '', room: b.scrollHeight - b.clientHeight}; })()")
# every other fold trigger the page has: a measure of the body (a card resize, fonts settling, a search, another row opening)
REMEASURE = (f"(async () => {{ const b = {BODY}; window.LevainDashboard.measureOverflow(b);"
             " await new Promise(r => setTimeout(r, 300)); return Math.round(b.scrollTop); })()")


@needs_browser
def test_a_folded_card_whose_open_row_scrolls_stays_keyboard_reachable(tmp_path: Path) -> None:
    """K1: the fold returned early and left the scrolling body with no tabindex, role or name: a keyboard reader could not scroll it."""
    _root, src = _long_keep(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            DESKTOP,
            {"until": f"{PANEL} && {GIANT_ROW} && {GIANT_ROW}.classList.contains('has-more') && 1", "ms": 30000},
            {"eval": OPEN_AND_SCROLL},
            {"eval": AFFORD},
        ])
    a = out["values"][-1]
    assert a["room"] > 100, f"the card's body does not scroll: {a}"
    assert a["tab"] == "0" and a["role"] == "region" and "scrollable" in a["label"], f"a scrolling folded body is not focusable: {a}"


@needs_browser
def test_any_remeasure_keeps_the_reading_place(tmp_path: Path) -> None:
    """K2: a search, another row opening or fonts settling re-fold the card; the reading place must not jump."""
    _root, src = _long_keep(tmp_path)
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            DESKTOP,
            {"until": f"{PANEL} && {GIANT_ROW} && {GIANT_ROW}.classList.contains('has-more') && 1", "ms": 30000},
            {"eval": OPEN_AND_SCROLL},
            {"eval": REMEASURE},
        ])
    v = out["values"]
    assert v[1]["scrolled"] > 100
    assert abs(v[-1] - v[1]["scrolled"]) <= 3, f"the reading place moved from {v[1]['scrolled']} to {v[-1]}"


@needs_browser
def test_opening_another_row_keeps_the_reading_place(tmp_path: Path) -> None:
    """K2: a row above the open one opening re-folds the card (the click path); the scroll must stay where the reader put it."""
    root, src = _long_keep(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    store.add(type="task", text="ABOVEKEEP " + PARA, disposition="note", today=TODAY + timedelta(days=3))   # newest: above the tall one
    above = f"[...{PANEL}.querySelectorAll('.row')].find(r => r.textContent.indexOf('ABOVEKEEP') >= 0)"
    click = (f"(async () => {{ const b = {BODY}; {above}.querySelector(':scope > .clause-toggle').click();"
             " await new Promise(r => setTimeout(r, 400)); return Math.round(b.scrollTop); })()")
    with _serve_plain(src) as url:
        out = _drive(tmp_path, f"{url}/", [
            DESKTOP,
            {"until": f"{PANEL} && {GIANT_ROW} && {GIANT_ROW}.classList.contains('has-more') && 1", "ms": 30000},
            {"eval": OPEN_AND_SCROLL},
            {"eval": click},
        ])
    v = out["values"]
    assert v[1]["scrolled"] > 100
    assert abs(v[-1] - v[1]["scrolled"]) <= 3, f"the reading place moved from {v[1]['scrolled']} to {v[-1]}"
