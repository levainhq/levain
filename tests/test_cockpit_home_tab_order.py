"""levain#165: Tab and screen-reader order on Home, pinned as it is (Phill signed np-602a980d (c), 11-19: keep the layout and the page build).

Home is ordered by CSS `order` over `display: contents` containers, so document order (the Tab order, and what a screen reader reads) can
differ from the drawn order. On the served fixture at 1440 two blocks differ (KNOWN_OUT_OF_ORDER): the Machine sky-and-controls block
(`#ck-skycol`, first in the DOM, drawn last), which Phill ruled the known out-of-order stop, and the Work board, which the first version of this
test could not see (project_memory/1119-46_review/known_edge_tab_order_165.md; the page, not the fixture, is for his look).

What this pins, in the order the failures fire:
  1. the page is settled (BOARD_SETTLED, then two walks a beat apart that agree), so a late panel cannot make the order flap;
  2. no Tab stop has a positive tabindex (real Tab order puts those first, which this walk does not model);
  3. every Tab stop on Home belongs to a block (a `[data-ck-panel]` or one of the hero blocks) or to the masthead / phone tabs: a stop in
     NO block fails and names itself, so a new block can never be silently ignored (the block is found from the page, not from a list here);
  4. the set of blocks that hold stops is EXPECTED_BLOCKS exactly: a new block fails (add it), a vanished one fails (the walk proved nothing);
  5. every block except the named ones walks in the order it is drawn, each block placed by its own rect (the first stop's rect for a
     `display: contents` container, which has none), rows grouped by top within ROW_TOLERANCE_PX, left to right inside a row;
  6. each named exception is still displaced (see KNOWN_OUT_OF_ORDER). A block moving into
     its drawn place (the fix, if he ever asks for it) fails this, which is the cue to delete its entry here and in the known-edge file."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ia-places [data-ia-place-btn]') && 1"}
# block -> why it is out of order on the served fixture. The sky block is the one ruled in np-602a980d (c); work_board is the SECOND, found when
# the walk stopped using a fixed block list that never named it (levain-cockpit 8, 11-19): the fixture draws the board above the glance tiles
# that the DOM puts before it. Whether the live page has more is for his look (see the band row filed with this fix).
KNOWN_OUT_OF_ORDER = {"#ck-skycol": "first in the DOM, drawn below the Drop box, tiles and Work", "panel:work_board": "after the glance tiles in the DOM, drawn above them"}
# the blocks that hold Tab stops on the served fixture (measured 11-19; a new block fails the set check, which is the point)
EXPECTED_BLOCKS = {'#ck-skycol', 'panel:state', 'panel:work_board', 'panel:tray', 'panel:edits', 'panel:decisions', 'panel:doctor', 'panel:sessions', 'panel:team-status'}
ROW_TOLERANCE_PX = 12

# every Tab stop in document order, filed under the innermost Home block that holds it (found from the page); chrome (header, phone tabs)
# is named and skipped; a stop in neither lands in `unfiled`. Positions come from each block's first stop.
WALK = """(() => {
  const BLOCK = '[data-ck-panel], #ck-needs, #ck-drop, #ck-notes, #ck-skycol, #ck-widgets';
  const CHROME = 'header.masthead, #ck-phonetabs';
  const stops = [...document.querySelectorAll('a[href],button,input,select,textarea,[tabindex]')]
    .filter((e) => e.tabIndex >= 0 && !e.matches(':disabled') && !e.closest('[inert]') && [...e.getClientRects()].some((r) => r.height > 0) && getComputedStyle(e).visibility !== 'hidden');
  const key = (b) => (b.matches('[data-ck-panel]') ? 'panel:' + b.dataset.ckPanel : '#' + b.id);
  const seen = [], unfiled = [], positive = [];
  for (const e of stops) {
    if (e.tabIndex > 0) positive.push(e.tagName + '#' + e.id);
    const b = e.closest(BLOCK);
    if (!b) { if (!e.closest(CHROME)) unfiled.push(e.tagName + '.' + String(e.className).slice(0, 40) + '#' + e.id); continue; }
    const k = key(b);
    if (!seen.some((x) => x.block === k)) { let r = b.getBoundingClientRect(); if (!(r.width > 0 && r.height > 0)) r = e.getBoundingClientRect(); seen.push({ block: k, top: Math.round(r.top + window.scrollY), left: Math.round(r.left) }); }
  }
  return JSON.stringify({ walk: seen, unfiled, positive });
})()"""


def _drawn_order(blocks: list[dict]) -> list[str]:
    """The blocks in drawn order: rows by vertical overlap of the first stops (within ROW_TOLERANCE_PX), left to right inside a row."""
    rows: list[list[dict]] = []
    for b in sorted(blocks, key=lambda b: (b["top"], b["left"])):
        if rows and b["top"] - rows[-1][0]["top"] <= ROW_TOLERANCE_PX:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [b["block"] for row in rows for b in sorted(row, key=lambda b: b["left"])]


@needs_browser
def test_tab_walks_home_in_the_drawn_order_except_the_one_named_block(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    beat = {"eval": "new Promise(r => setTimeout(r, 300)).then(() => 1)"}
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, BOARD_SETTLED,
            {"eval": WALK}, beat, {"eval": WALK},
        ])
    walks = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"walk"')]
    assert len(walks) == 2, out["values"]                                 # both walks answered (a throw inside one would show here)
    first, second = walks
    stable = lambda w: ([b["block"] for b in w["walk"]], w["unfiled"], w["positive"])   # the order and the sets, not pixels: a 1 px reflow is not a flake
    assert stable(first) == stable(second), (first, second)               # settled: a second walk a beat later agrees
    assert not first["positive"], first["positive"]                       # no positive tabindex (this walk does not model it)
    assert not first["unfiled"], first["unfiled"]                         # every stop is in a block or the chrome: a new block is seen, never ignored
    walk = first["walk"]
    present = {b["block"] for b in walk}
    assert present == EXPECTED_BLOCKS, (sorted(present - EXPECTED_BLOCKS), sorted(EXPECTED_BLOCKS - present))   # new block / vanished block
    rest = [b for b in walk if b["block"] not in KNOWN_OUT_OF_ORDER]
    assert [b["block"] for b in rest] == _drawn_order(rest), walk         # everything else walks in the order it is drawn
    for known in KNOWN_OUT_OF_ORDER:                                      # each named exception is still displaced (else delete it from the dict)
        others = [b for b in walk if b["block"] == known or b["block"] not in KNOWN_OUT_OF_ORDER]
        assert [b["block"] for b in others] != _drawn_order(others), (known, walk)
