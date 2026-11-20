"""levain#182 follow-on: with one Home card folded the "Reset layout" pill is drawn whole, inside the viewport.

A saved fold shows the Reset layout row above the List card. At 390 wide the row (the operate zone head, a grid item of the Home deck because its
zone is `display: contents`) was placed by `grid-column: auto`, one of the twelve columns (0 to 9 px wide at 320 to 430), so the pill's label overflowed to the
left of the viewport and read "yout". From 721 px the row is placed in the Work head's row; below it nothing placed it. Looking at it is his."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ck-zgrid .panel .ck-lay-fold') && 1"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"}
FOLD = {"eval": "document.querySelector('.ck-zgrid .panel[data-ck-panel=\"work_board\"] .ck-lay-fold').click(); 1"}
PILL_READY = {"until": "document.querySelector('[data-layout-reset]') && document.querySelector('[data-layout-reset]').getClientRects().length > 0 && 1"}
MEASURE = """(() => { const b = document.querySelector('[data-layout-reset]'); const z = b.parentElement; const r = (e) => { const q = e.getBoundingClientRect();
  return { left: q.left, right: q.right, width: q.width, top: q.top, height: q.height }; };
  const cs = (e) => { const c = getComputedStyle(e); return { display: c.display, position: c.position, overflow: c.overflowX + '/' + c.overflowY, margin: c.margin,
    transform: c.transform, gridColumn: c.gridColumnStart + ' / ' + c.gridColumnEnd, justify: c.justifyContent, whiteSpace: c.whiteSpace, wc: c.width }; };
  let clip = null; for (let n = b.parentElement; n; n = n.parentElement) { const c = getComputedStyle(n); if (c.overflowX !== 'visible') { clip = n.tagName + '.' + n.className + '#' + n.id; break; } }
  return JSON.stringify({ vw: document.documentElement.clientWidth, text: b.textContent.trim(), pill: r(b), pillCss: cs(b), box: r(z), boxCss: cs(z),
    scroll: [b.scrollWidth, b.clientWidth], docScroll: document.documentElement.scrollWidth, clipper: clip }); })()"""
WIDTHS = (320, 360, 390, 430, 820, 1440)


def pill(tmp_path: Path, width: int, shot: str | None = None) -> dict:
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED, SETTLE, FOLD, SETTLE, PILL_READY, {"eval": MEASURE}]
    shots = os.environ.get("LEVAIN_RESET_PILL_SHOTS")   # a directory: the folded page, whole, as reset_pill_<width>.png
    if shot or shots:
        steps.append({"shot": shot or f"{shots}/reset_pill_{width}.png", "full": True})
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    row = next(json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"vw"'))
    dump = os.environ.get("LEVAIN_RESET_PILL_DUMP")
    if dump:
        with open(dump, "a") as fh:
            fh.write(json.dumps({"width": width, **row}) + "\n")
    return row


@needs_browser
@pytest.mark.parametrize("width", WIDTHS)
def test_the_reset_layout_pill_is_whole_and_inside_the_viewport(tmp_path: Path, width: int) -> None:
    row = pill(tmp_path, width)
    p = row["pill"]
    assert row["text"] == "Reset layout", row
    assert p["left"] >= 0 and p["right"] <= row["vw"], (width, "the pill is outside the viewport", row)
    assert row["scroll"][0] <= row["scroll"][1], (width, "the pill's label is wider than the pill", row)
