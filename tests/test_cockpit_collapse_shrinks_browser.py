"""levain#182: folding a Home card with its one chevron leaves the card the height of its header, and what is below it moves up.

#172 hid a folded card's body but left the card its full height on Home: the Home height rules (`!important`, per card id) and the Board's
pair rule outranked the fold's `height: auto`, so a folded card was a blank panel the size it had been. Measured on the served page at 390, 820
and 1440 (a tall card, a card in the stack column, a short bar): the card is within a few px of its header, the next card below sits the
same gap under it as before (the card below moves up by what was freed), and unfolding gives the first height back. Looking at it
is his (the installed build)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ck-zgrid .panel .ck-lay-fold') && 1"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"}
CARDS = ("work_board", "tray", "edits")   # tall, in a stack column, a short bar
HEIGHT_SLACK = 6      # px a folded card may exceed its header by (its own border)
GAP_SLACK = 4         # px the gap under the card may shrink by after the fold
RESET_ROW = 50        # px the gap may GROW by: the Reset-layout row a saved fold shows between the card and what is below (measured 43 at 390)
RESTORE_SLACK = 2     # px the unfolded card may differ from its first height
PANEL = "document.querySelector('.ck-zgrid .panel[data-ck-panel=\"%s\"]')"
MEASURE = """((id) => { const p = document.querySelector('.ck-zgrid .panel[data-ck-panel="' + id + '"]'); const r = p.getBoundingClientRect();
  const head = p.querySelector(':scope > .phead').getBoundingClientRect().height;
  const below = [...document.querySelectorAll('.ck-zgrid .panel, .ck-zone')].filter((n) => n !== p && n.getClientRects().length && !p.contains(n) && !n.contains(p)
      && n.getBoundingClientRect().top > r.top + 1 && n.getBoundingClientRect().left < r.right && n.getBoundingClientRect().right > r.left)
    .map((n) => n.getBoundingClientRect().top).sort((a, b) => a - b)[0];
  return JSON.stringify({ m: id, h: r.height, head: head, gap: below === undefined ? null : below - r.bottom, collapsed: p.classList.contains('ck-collapsed') }); })"""


def _run(tmp_path: Path, width: int) -> dict:
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED, SETTLE]
    for cid in CARDS:
        click = {"eval": f"{PANEL % cid}.querySelector('.ck-lay-fold').click(); 1"}
        steps += [{"eval": f"{MEASURE}('{cid}')"}, click, SETTLE, {"eval": f"{MEASURE}('{cid}')"}, click, SETTLE, {"eval": f"{MEASURE}('{cid}')"}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"m"')]
    assert len(rows) == 3 * len(CARDS), out["values"]
    return {cid: rows[3 * i:3 * i + 3] for i, cid in enumerate(CARDS)}


@needs_browser
@pytest.mark.parametrize("width", [390, 820, 1440])
def test_a_folded_home_card_is_its_header_tall_and_what_is_below_moves_up(tmp_path: Path, width: int) -> None:
    for cid, (before, folded, after) in _run(tmp_path, width).items():
        assert not before["collapsed"] and folded["collapsed"] and not after["collapsed"], (width, cid)
        assert before["h"] > before["head"] + 40, (width, cid, "the card has no body to fold on the fixture", before)
        assert folded["h"] <= folded["head"] + HEIGHT_SLACK, (width, cid, "the folded card is not its header's height (a blank panel)", before, folded)
        assert before["gap"] is not None and folded["gap"] is not None, (width, cid, "nothing below the card to move up")
        assert -GAP_SLACK <= folded["gap"] - before["gap"] <= RESET_ROW, (width, cid, "the card below did not move up with the card", before, folded)
        assert abs(after["h"] - before["h"]) <= RESTORE_SLACK, (width, cid, "unfolding did not restore the height", before, after)
