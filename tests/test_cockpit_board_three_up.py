"""Row 480 (b): on a desktop the Board's cards sit three to a row, whatever the grid's data-cols says.

cdd3bcbe made the Board's right stack empty (`loops: "build"`), layoutGrid then sets data-cols="1", and the single-column rule stacked every
card full width. Real headless Chrome against a served cockpit in the scratch home (the conftest isolates HOME). The grid's data-cols is
forced to 1 and to 2 so the layout is pinned for both, not only for the state the page happens to be in.
``LEVAIN_BOARD3_SHOTS=<dir>`` and ``LEVAIN_BOARD3_PREFIX`` (before / after) keep a full-page PNG per width."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_list_page_browser import _reload, _seed_more

# the cards that are a real panel in the grid (a thin dashed empty card or a folded one is not a cell)
MEASURE = """(() => {
  const g = document.querySelector('.ck-zgrid.ck-twocol');
  const force = %s;
  if (force) g.dataset.cols = String(force);
  const cells = [...g.querySelectorAll('.panel')].filter((p) => p.getClientRects().length > 0 && !p.classList.contains('ck-dash')
    && !p.classList.contains('ck-is-empty') && !p.classList.contains('ck-fold') && !p.classList.contains('ck-tab-off')
    && (p.parentElement === g || getComputedStyle(p.parentElement).display === 'contents'));
  const rows = {};
  for (const p of cells) { const t = Math.round(p.getBoundingClientRect().top); rows[t] = (rows[t] || 0) + 1; }
  const gw = g.clientWidth;
  return JSON.stringify({cols: g.dataset.cols, cells: cells.length, perRow: Math.max(0, ...Object.values(rows)),
    widest: Math.max(0, ...cells.map((p) => p.getBoundingClientRect().width / gw)), hs: document.documentElement.scrollWidth <= window.innerWidth + 1});
})()"""


def _shot(name: str) -> list[dict[str, Any]]:
    d = os.environ.get("LEVAIN_BOARD3_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    pre = os.environ.get("LEVAIN_BOARD3_PREFIX", "after")
    return [{"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"shot": str(Path(d) / f"{pre}-{name}.png"), "full": True}]


def _board(tmp_path: Path, w: int, h: int, force: int | None, name: str) -> dict[str, Any]:
    root, src, _ids = _install(tmp_path)
    _seed_more(root)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": w, "h": h, "dpr": 1}},
            *_reload(),
            {"eval": "location.hash = '#board'; 1"},
            {"until": "document.body.dataset.mode === 'board' && !!document.querySelector('.ck-zgrid.ck-twocol [data-ck-panel=tray]') && 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": MEASURE % ("null" if force is None else force)},
            *_shot(name),
        ])
    return json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith('{"cols"')))


@needs_browser
@pytest.mark.parametrize("force", [None, 1, 2])
def test_the_board_is_three_to_a_row_on_a_desktop(tmp_path: Path, force: int | None) -> None:
    m = _board(tmp_path, 1440, 900, force, "desktop" if force is None else f"desktop-cols{force}")
    assert m["cells"] >= 3, m
    assert m["perRow"] == 3, m                      # three cards share a row, not one full-width stack
    assert m["widest"] < 0.5, m                     # no card is the whole row
    assert m["hs"] is True, m


@needs_browser
def test_the_board_is_three_to_a_row_at_the_desktop_breakpoint(tmp_path: Path) -> None:
    m = _board(tmp_path, 1100, 900, 1, "bp1100")
    assert m["perRow"] == 3 and m["hs"] is True, m


@needs_browser
@pytest.mark.parametrize("w,h", [(820, 1180), (390, 844)])
def test_a_tablet_and_a_phone_keep_one_card_per_row(tmp_path: Path, w: int, h: int) -> None:
    m = _board(tmp_path, w, h, None, f"w{w}")
    assert m["perRow"] == 1 and m["hs"] is True, m
