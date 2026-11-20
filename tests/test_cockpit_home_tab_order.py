"""levain#165: Tab and screen-reader order on Home, pinned as it is (Phill signed np-602a980d (c), 11-19: keep the layout and the page build).

Home is ordered by CSS `order` over `display: contents` containers, so document order (the Tab order, and what a screen reader reads) can
differ from the drawn order. Measured on the served page at 1440 in a real browser, ONE block differs: the Machine sky-and-controls block
(`#ck-skycol`, inside the hero) is the first Tab stop but is drawn after the Drop box, the glance tiles and the Work panels. That is the
known out-of-order stop (project_memory/1119-46_review/known_edge_tab_order_165.md).

What this pins: every OTHER Home block's first Tab stop follows the drawn order top to bottom, and the sky block is the one named exception
(first in the walk). A new block that lands out of order fails the first assertion; the sky block moving into its drawn place (the fix, if he
ever asks for it) fails the second, which is the cue to delete the exception from this test and the known-edge file together."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ia-places [data-ia-place-btn]') && 1"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}

# the Home blocks that hold Tab stops (the page's own `order` rules in dashboard.css place them)
BLOCKS = ["#ck-needs", "#ck-drop", '.panel[data-ck-panel="flow-desk"]', "#ck-notes", '.panel[data-ck-panel="tray"]', '.panel[data-ck-panel="board"]',
          '.panel[data-ck-panel="inbox"]', "#ck-skycol", '.panel[data-ck-panel="roster"]', '.panel[data-ck-panel="agents"]', '.panel[data-ck-panel="agent_calls"]',
          '.panel[data-ck-panel="stream"]', '.panel[data-ck-panel="scheduled_jobs"]', '.panel[data-ck-panel="jobs"]', '.panel[data-ck-panel="edits"]', "#ck-widgets"]
KNOWN_OUT_OF_ORDER = "#ck-skycol"

# every Tab stop in document order, filed under the Home block that holds it; each block is listed once, at its first stop
WALK = ("(() => { const blocks = %s; const stops = [...document.querySelectorAll('a[href],button,input,select,textarea,[tabindex]')]"
        ".filter((e) => e.tabIndex >= 0 && [...e.getClientRects()].some((r) => r.height > 0) && getComputedStyle(e).visibility !== 'hidden');"
        " const seen = []; for (const e of stops) { const b = blocks.find((s) => { const n = document.querySelector(s); return n && n.contains(e); });"
        " if (b && !seen.some((x) => x.block === b)) { const n = document.querySelector(b).getBoundingClientRect(); seen.push({ block: b, top: Math.round(n.top + window.scrollY), left: Math.round(n.left) }); } }"
        " return JSON.stringify({ walk: seen }); })()") % json.dumps(BLOCKS)


@needs_browser
def test_tab_walks_home_in_the_drawn_order_except_the_one_named_block(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}},
            READY, SETTLE, SETTLE,
            {"eval": WALK},
        ])
    (walk,) = [json.loads(v)["walk"] for v in out["values"] if isinstance(v, str) and v.startswith('{"walk"')]
    assert len(walk) >= 3, walk                                   # the fixture draws enough blocks for an order to mean something
    rest = [b for b in walk if b["block"] != KNOWN_OUT_OF_ORDER]
    drawn = sorted(rest, key=lambda b: (b["top"], b["left"]))
    assert [b["block"] for b in rest] == [b["block"] for b in drawn], walk    # everything else walks in the order it is drawn
    # the named exception is still the exception: first in the walk, and drawn below the block that follows it
    assert walk[0]["block"] == KNOWN_OUT_OF_ORDER, walk
    assert walk[0]["top"] > walk[1]["top"], walk
