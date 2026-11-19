"""Row 480 (b): on a desktop the Board's cards sit three to a row.

cdd3bcbe made the Board's right stack empty (Open loops was Build-only), layoutGrid then set data-cols="1", and the single-column rule
stacked every card full width. Since the loop fold there is no Open loops panel at all, so the right stack is empty on every tab: the page still
makes it (hidden), and the Board keeps its three-up grid with the List as one of the cards. Real headless Chrome against a served cockpit in the scratch home
(the conftest isolates HOME). ``LEVAIN_BOARD3_SHOTS=<dir>`` and ``LEVAIN_BOARD3_PREFIX`` (before / after) keep a full-page PNG per width."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_list_page_browser import _reload, _seed_more

# the cards that are a real panel in the Board's grid (a thin dashed empty card or a folded one is not a cell); the grid is the List's own
MEASURE = """(() => {
  const g = document.querySelector('[data-ck-panel=tray]').closest('.ck-zgrid');
  const cells = [...g.querySelectorAll('.panel')].filter((p) => p.getClientRects().length > 0 && !p.classList.contains('ck-dash')
    && !p.classList.contains('ck-is-empty') && !p.classList.contains('ck-fold') && !p.classList.contains('ck-tab-off')
    && (p.parentElement === g || getComputedStyle(p.parentElement).display === 'contents'));
  const rows = {};
  for (const p of cells) { const t = Math.round(p.getBoundingClientRect().top); rows[t] = (rows[t] || 0) + 1; }
  const gw = g.clientWidth;
  const spill = cells.filter((p) => p.scrollWidth > p.clientWidth + 1).length;
  return JSON.stringify({stacked: !!document.querySelector('.ck-twocol, .ck-col'), cells: cells.length, perRow: Math.max(0, ...Object.values(rows)),
    widest: Math.max(0, ...cells.map((p) => p.getBoundingClientRect().width / gw)), narrowest: Math.min(...cells.map((p) => p.getBoundingClientRect().width)),
    spill, hs: document.documentElement.scrollWidth <= window.innerWidth + 1, tile: 1});
})()"""


def _shot(name: str) -> list[dict[str, Any]]:
    d = os.environ.get("LEVAIN_BOARD3_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    pre = os.environ.get("LEVAIN_BOARD3_PREFIX", "after")
    return [{"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"shot": str(Path(d) / f"{pre}-{name}.png"), "full": True}]


def _board(tmp_path: Path, w: int, h: int, name: str) -> dict[str, Any]:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": w, "h": h, "dpr": 1}},
            *_reload(),
            {"eval": "location.hash = '#board'; 1"},
            {"until": "document.body.dataset.mode === 'board' && !!document.querySelector('[data-ck-panel=tray]') && 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": MEASURE},
            *_shot(name),
        ])
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"tile"' in v))


# Home lays the Work row out by named spans (the List, the Board and the Inbox take four of twelve columns each from 1101 px), not by the Board's
# own grid: the List's share of the page's width is what says a row holds three, since this fixture has no Board or Inbox panel to put beside it.
HOME_TRAY = """(() => { const t = document.querySelector('[data-ck-panel=tray]'), d = document.querySelector('.deck'), cs = getComputedStyle(d);
  const inner = d.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  return JSON.stringify({place: document.body.dataset.iaPlace, share: t.getBoundingClientRect().width / inner, width: Math.round(t.getBoundingClientRect().width),
    hs: document.documentElement.scrollWidth <= window.innerWidth + 1, tile: 1}); })()"""


def _home(tmp_path: Path, w: int, h: int) -> dict[str, Any]:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": w, "h": h, "dpr": 1}},
            *_reload(),
            {"until": "document.body.dataset.iaPlace === 'home' && !!document.querySelector('[data-ck-panel=tray]') && 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": HOME_TRAY},
        ])
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"tile"' in v))


@needs_browser
def test_home_gives_the_list_a_third_of_the_row_on_a_desktop(tmp_path: Path) -> None:
    m = _home(tmp_path, 1440, 900)
    assert m["place"] == "home", m
    assert 0.28 < m["share"] < 0.38, m              # a third of the row, not the whole stack
    assert m["hs"] is True, m


@needs_browser
def test_home_keeps_the_list_a_third_of_the_row_at_the_home_breakpoint(tmp_path: Path) -> None:
    m = _home(tmp_path, 1101, 900)                  # 1101 is where Home goes three across (1100 and under it is two)
    assert 0.28 < m["share"] < 0.38 and m["width"] >= 280 and m["hs"] is True, m


@needs_browser
@pytest.mark.parametrize("w,h", [(820, 1180), (390, 844)])
def test_a_tablet_and_a_phone_keep_one_card_per_row(tmp_path: Path, w: int, h: int) -> None:
    m = _board(tmp_path, w, h, f"w{w}")
    assert m["perRow"] == 1 and m["hs"] is True, m


@needs_browser
def test_no_board_card_is_narrower_than_280px_at_the_desktop_breakpoint(tmp_path: Path) -> None:
    m = _board(tmp_path, 1100, 900, "narrowest1100")
    assert m["cells"] >= 3, m
    assert m["narrowest"] >= 280, m
    assert m["spill"] == 0, m                       # no card's content runs past its own box
