"""levain#144 (Cockpit Home layout polish): the widths its DONE line names, 390, 480, 820, 1180, 1440 and 1920, measured on the served page.

The polish links (levain#151, on main) pinned each item at the widths it was found at: the pinned masthead (test_cockpit_polish_header, 390 to
1440), the Work row with no orphan (740 to 1100), the Machine row (the same file) and the 1101-1180 tiles (test_cockpit_polish_machine), Calm in
the Machine row at 390. None of them walks 480 (the Calm control's own width) or 1920, and none puts all of the row's claims at one width. This
does, with the facts that do not need a person: no sideways scroll, every masthead control and the Calm control drawn inside the viewport, and
no At-a-glance row that leaves one lone tile. What it cannot hold (his eyes): whether it reads well."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ia-places [data-ia-place-btn]') && 1"}
WIDTHS = (390, 480, 820, 1180, 1440, 1920)

FACTS = """(() => {
  const vis = (n) => n && [...n.getClientRects()].some((r) => r.width > 0 && r.height > 0);
  const box = (n) => { const r = n.getBoundingClientRect(); return { left: Math.round(r.left), right: Math.round(r.right), top: Math.round(r.top + scrollY), h: Math.round(r.height) }; };
  const calm = document.querySelector('#calmbtn');
  const mast = [...document.querySelectorAll('header.masthead button, header.masthead a[href]')].filter(vis).map((n) => ({ id: n.id || n.className, ...box(n) }));
  const tiles = [...document.querySelectorAll('#ck-widgets > .ck-card.w')].filter(vis).map(box);
  return JSON.stringify({ facts: true, w: innerWidth, docW: document.documentElement.scrollWidth, calm: vis(calm) ? box(calm) : null, mast, tiles });
})()"""


@needs_browser
def test_home_has_no_sideways_scroll_and_draws_its_controls_inside_the_viewport_at_the_named_widths(tmp_path: Path) -> None:
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED]
    for w in WIDTHS:
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}, {"eval": FACTS}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"facts"')]
    assert [r["w"] for r in rows] == list(WIDTHS), out["values"]
    for r in rows:
        w = r["w"]
        assert r["docW"] <= w, (w, "the page scrolls sideways", r["docW"])
        assert r["mast"], (w, "no masthead control is drawn")
        for m in r["mast"]:
            assert 0 <= m["left"] and m["right"] <= w, (w, "a masthead control leaves the viewport", m)
        assert r["calm"] is not None, (w, "the Calm control is not drawn on Home")
        assert 0 <= r["calm"]["left"] and r["calm"]["right"] <= w and r["calm"]["h"] > 0, (w, r["calm"])
        if len(r["tiles"]) > 1:                                                     # At a glance: every row fills the row above it, so no tile sits beside empty space
            rows_by_top: dict[int, list[dict]] = {}
            for t in r["tiles"]:
                rows_by_top.setdefault(t["top"], []).append(t)
            spans = {(min(t["left"] for t in row), max(t["right"] for t in row)) for row in rows_by_top.values()}
            assert max(s[0] for s in spans) - min(s[0] for s in spans) <= 4 and max(s[1] for s in spans) - min(s[1] for s in spans) <= 4, (w, "a row does not fill the width of the others", sorted(spans), r["tiles"])
