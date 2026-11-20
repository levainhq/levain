"""levain#182: folding a Home card with its one chevron leaves the card the height of its header, and what is below it moves up.

#172 hid a folded card's body but left the card its full height on Home: the Home height rules (`!important`, per card id) and the Board's
pair rule outranked the fold's `height: auto`, so a folded card was a blank panel the size it had been. Two shapes are held here.
Alone in its row (the fixture's own cards, at three widths): the card is within its border of its header, the card below it has moved up by
what was freed (less the Reset-layout row a saved fold draws above the List, taken from that row's own height), and unfolding gives the
first height back. Beside a taller card (a scratch pair in the Home grid, at two widths): a folded EMPTY card is a slim bar that stretched
to its row-mate's height, so it must still be its header's height with the row-mate untouched. Looking at it is his (the installed build)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import BOARD_SETTLED, _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

READY = {"until": "document.body.dataset.iaPlace && document.querySelector('.ck-zgrid .panel .ck-lay-fold') && 1"}
CARDS = ("work_board", "tray", "edits")   # tall, in a stack column, a short bar
HEIGHT_SLACK = 6      # px a folded card may exceed its header by (its own border)
MOVE_SLACK = HEIGHT_SLACK + 2   # px the card below may sit lower than "moved up by what a header-tall card frees": the border the folded card may keep, a rounding
RESTORE_SLACK = 2     # px the unfolded card may differ from its first height
SAME_ROW = 1          # px two row-mates' tops may differ by
PANEL = "document.querySelector('.ck-zgrid .panel[data-ck-panel=\"%s\"]')"
# The card below is found by geometry once, before the fold, and kept as a selector (a panel by its id, a zone by its name), so the
# after-fold read is the SAME card, not whatever is then under the folded one. The Reset row is the operate zone head that carries the pill;
# resetH is that row's own height plus the grid's row gap, the space it takes from what is below it.
MEASURE = """((id, mark) => { const p = document.querySelector('.ck-zgrid .panel[data-ck-panel="' + id + '"]'); const r = p.getBoundingClientRect();
  const head = p.querySelector(':scope > .phead').getBoundingClientRect().height; window.__below = window.__below || {};
  const key = (n) => n.dataset.ckPanel ? '.ck-zgrid .panel[data-ck-panel="' + n.dataset.ckPanel + '"]' : '.ck-zone[data-zone="' + n.dataset.zone + '"]';
  if (mark) { const n = [...document.querySelectorAll('.ck-zgrid .panel, .ck-zone')].filter((n) => n !== p && n.getClientRects().length && !p.contains(n) && !n.contains(p)
      && (n.dataset.ckPanel || n.dataset.zone) && n.getBoundingClientRect().top > r.top + 1 && n.getBoundingClientRect().left < r.right && n.getBoundingClientRect().right > r.left)
    .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0]; window.__below[id] = n ? key(n) : null; }
  const below = window.__below[id] && document.querySelector(window.__below[id]);
  const pill = document.querySelector('[data-layout-reset]'); const row = pill && pill.parentElement.getClientRects().length ? pill.parentElement.getBoundingClientRect() : null;
  let g = pill && pill.parentElement && pill.parentElement.parentElement; while (g && !getComputedStyle(g).display.includes('grid')) g = g.parentElement;   // the grid the Reset row is an item of
  const gap = g ? parseFloat(getComputedStyle(g).rowGap) : 0;   // NaN when row-gap is `normal`
  return JSON.stringify({ m: id, h: r.height, head: head, collapsed: p.classList.contains('ck-collapsed'), belowSel: window.__below[id],
    belowTop: below ? below.getBoundingClientRect().top : null, resetH: row ? row.height + (Number.isFinite(gap) ? gap : 0) : 0, resetTop: row ? row.top : null }); })"""
# a wait on the condition: the card is in the wanted fold state and neither it, the card below nor the Reset row moved since the last poll
SETTLED = """((id, want) => { const p = document.querySelector('.ck-zgrid .panel[data-ck-panel="' + id + '"]'); if (!p) return null;
  const s = window.__below && window.__below[id]; const b = s && document.querySelector(s); const pill = document.querySelector('[data-layout-reset]');
  const sig = [id, want, p.getBoundingClientRect().height, b && b.getBoundingClientRect().top, pill && pill.parentElement.getBoundingClientRect().height].join('/');
  const drawn = !want || (pill && pill.getClientRects().length);   // a saved fold draws the Reset row; do not settle before it is there
  const ok = p.classList.contains('ck-collapsed') === want && drawn && window.__sig === sig; window.__sig = sig; return ok && 1; })"""


def _wait(cid: str, want: bool) -> dict:
    return {"until": f"{SETTLED}('{cid}', {'true' if want else 'false'})", "diag": f"{MEASURE}('{cid}', false)"}


def _run(tmp_path: Path, width: int) -> dict:
    _root, src, _ids = _install(tmp_path)
    steps: list = [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED]
    for cid in CARDS:
        click = {"eval": f"{PANEL % cid}.querySelector('.ck-lay-fold').click(); 1"}
        steps += [{"eval": f"{MEASURE}('{cid}', true)"}, click, _wait(cid, True), {"eval": f"{MEASURE}('{cid}', false)"},
                  click, _wait(cid, False), {"eval": f"{MEASURE}('{cid}', false)"}]
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
        assert folded["head"] > 0 and abs(folded["h"] - folded["head"]) <= HEIGHT_SLACK, (width, cid, "the folded card is hidden or not its header's height (a blank panel)", before, folded)
        assert before["belowTop"] is not None and folded["belowTop"] is not None, (width, cid, "nothing below the card to move up", before, folded)
        freed = before["h"] - folded["head"]   # what a header-tall card frees; NOT before-minus-folded height, which is 0 for a card that did not shrink
        # the Reset row is drawn once a fold is saved; when it sits above the card below it pushes that card down by its own height
        pushed = (folded["resetH"] - before["resetH"]) if folded["resetTop"] is not None and folded["resetTop"] < folded["belowTop"] else 0
        moved_up = before["belowTop"] - folded["belowTop"]
        assert moved_up >= freed - pushed - MOVE_SLACK, (width, cid, "the card below did not move up by what the fold freed", f"freed={freed} pushed={pushed} moved_up={moved_up}", before, folded)
        assert abs(after["h"] - before["h"]) <= RESTORE_SLACK, (width, cid, "unfolding did not restore the height", before, after)


# A folded EMPTY card keeps `.ck-fold`'s `align-self: stretch`, so with `height: auto` it is stretched to the tallest card of its row. The fixture draws
# no such row-mate, so a scratch pair is put in the Home grid: the first one starts a fresh row, both take the span the Home grid gives their id.
PAIRS = (("scheduled_jobs", "jobs"), ("inbox", "edits"))   # (the taller unfolded row-mate, the slim empty card that is folded)
# the scratch cards reuse the real cards' data-ck-panel ids on purpose: the Home rules that key on the id (heights, spans) must apply to them; the reads go by data-scratch
PAIR = """((ids) => { const grid = document.querySelector('.ck-zgrid'); const mk = (id, cls, order) => { const p = document.createElement('section');
  p.className = 'panel ' + cls; p.dataset.ckPanel = id; p.dataset.scratch = id; p.style.order = String(order);
  p.innerHTML = '<div class="phead"><h2>' + id + '</h2></div><div class="pbody"><p class="empty">nothing here</p></div>'; grid.appendChild(p); return p; };
  const mate = mk(ids[0], '', 1000); mate.style.gridColumnStart = '1'; mk(ids[1], 'ck-fold ck-is-empty', 1001); return 1; })"""
PAIR_MEASURE = """(() => { const r = (id) => document.querySelector('.panel[data-scratch="' + id + '"]'); const out = {};
  for (const id of %s) { const p = r(id); const q = p.getBoundingClientRect(); out[id] = { top: q.top, h: q.height, head: p.querySelector(':scope > .phead').getBoundingClientRect().height,
    collapsed: p.classList.contains('ck-collapsed'), align: getComputedStyle(p).alignSelf }; }
  return JSON.stringify({ pair: out }); })()"""
PAIR_FOLD = "document.querySelector('.panel[data-scratch=\"%s\"]').classList.add('ck-collapsed'); 1"
# the page's own script restyles a card it did not draw (a class, a height) a moment after it is added, so both reads wait until the pair's heights
# and the slim card's fold state have not changed since the last poll
PAIR_SETTLED = """((ids, want) => { const ps = ids.map((id) => document.querySelector('.panel[data-scratch="' + id + '"]'));
  const sig = ps.map((p) => p.getBoundingClientRect().height + p.className).join('/');
  const ok = ps[1].classList.contains('ck-collapsed') === want && window.__psig === sig; window.__psig = sig; return ok && 1; })"""


@needs_browser
@pytest.mark.parametrize("pair", PAIRS, ids=["-".join(p) for p in PAIRS])
@pytest.mark.parametrize("width", [820, 1440])
def test_a_folded_empty_card_beside_a_taller_one_is_its_header_tall_not_the_rows_height(tmp_path: Path, width: int, pair: tuple[str, str]) -> None:
    mate, slim = pair
    _root, src, _ids = _install(tmp_path)
    ids = json.dumps(list(pair))
    measure = {"eval": PAIR_MEASURE % ids}

    def settled(want: bool) -> dict:
        return {"until": f"{PAIR_SETTLED}({ids}, {'true' if want else 'false'})", "diag": PAIR_MEASURE % ids}
    steps: list = [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, BOARD_SETTLED, {"eval": f"{PAIR}({ids})"}, settled(False), measure,
                   {"eval": PAIR_FOLD % slim}, settled(True), measure]
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", steps)
    before, folded = [json.loads(v)["pair"] for v in out["values"] if isinstance(v, str) and v.startswith('{"pair"')]
    assert abs(before[mate]["top"] - before[slim]["top"]) <= SAME_ROW, (width, "the scratch pair is not in one row", before)
    assert before[mate]["h"] > before[slim]["h"] + 40, (width, "the row-mate is not taller than the card, so nothing could stretch it", before)
    assert folded[slim]["collapsed"] and not folded[mate]["collapsed"], (width, folded)
    assert folded[slim]["head"] > 0 and abs(folded[slim]["h"] - folded[slim]["head"]) <= HEIGHT_SLACK, (width, "the folded empty card is hidden or stretched to its row (a blank panel)", before, folded)
    assert abs(folded[mate]["h"] - before[mate]["h"]) <= RESTORE_SLACK, (width, "the row-mate changed height", before, folded)
