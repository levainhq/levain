"""levain#182 follow-on: with one Home card folded the "Reset layout" pill is drawn whole, inside the viewport.

A saved fold shows the Reset layout row above the List card. On a phone the row (the operate zone head, a grid item of the Home deck because its
zone is `display: contents`) was placed by `grid-column: auto`, a single narrow column of the deck's grid, so the pill's label overflowed to the
left of the viewport and read "yout". From the tablet width up the row is placed in the Work head's row; below it nothing placed it. The
assertions are the pill's own box inside the viewport and its label not wider than the pill. Looking at it is his."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ck-zgrid .panel .ck-lay-fold') && 1"}
FOLD = {"eval": "document.querySelector('.ck-zgrid .panel[data-ck-panel=\"work_board\"] .ck-lay-fold').click(); 1"}
# the pill is drawn and has not moved since the last poll (the fold has reflowed the deck)
PILL_SETTLED = """(() => { const b = document.querySelector('[data-layout-reset]'); if (!b || !b.getClientRects().length) return null; const q = b.getBoundingClientRect();
  const sig = [q.left, q.top, q.width, q.height].join('/'); const ok = window.__pillSig === sig; window.__pillSig = sig; return ok && 1; })()"""
MEASURE = """(() => { const b = document.querySelector('[data-layout-reset]'); if (!b) return JSON.stringify({ missing: true }); const q = b.getBoundingClientRect();
  const w = document.querySelector('.ia-head[data-ia-head="work"]'); const wq = w && w.getBoundingClientRect();
  return JSON.stringify({ vw: document.documentElement.clientWidth, text: b.textContent.trim(), pill: { left: q.left, right: q.right, top: q.top, bottom: q.bottom },
    work: wq && { top: wq.top, bottom: wq.bottom }, scroll: [b.scrollWidth, b.clientWidth] }); })()"""
WIDTHS = (320, 360, 390, 430, 600, 720, 721, 820, 1440)


def pill(tmp_path: Path, width: int) -> dict:
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED, FOLD, {"until": PILL_SETTLED, "diag": MEASURE}, {"eval": MEASURE}]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    return next(json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"vw"'))


@needs_browser
@pytest.mark.parametrize("width", WIDTHS)
def test_the_reset_layout_pill_is_whole_and_inside_the_viewport(tmp_path: Path, width: int) -> None:
    row = pill(tmp_path, width)
    assert not row.get("missing"), (width, "no Reset pill drawn")
    p = row["pill"]
    assert row["text"] == "Reset layout", row
    assert p["left"] >= 0 and p["right"] <= row["vw"], (width, "the pill is outside the viewport", row)
    assert row["scroll"][0] <= row["scroll"][1], (width, "the pill's label is wider than the pill", row)
    assert row["work"] is not None, (width, "no Work head to place the pill against", row)
    if width >= 721:   # in the Work head's row: the pill's vertical centre is inside the head
        mid = (p["top"] + p["bottom"]) / 2
        assert row["work"]["top"] <= mid <= row["work"]["bottom"], (width, "the pill is not in the Work head's row", row)
    else:   # below 721 a row of its own: its top is at or below the head's bottom
        assert p["top"] >= row["work"]["bottom"] - 1, (width, "the pill does not have a row of its own", row)
