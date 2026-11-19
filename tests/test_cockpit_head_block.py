"""levain#167 (Phill 11-19): Home's head block, measured in a real browser.

Desktop: "just a skoosh more padding between the nav menu and clock and state line". Phone: "the clock, state line, and wrap info is hard to
read and smooshed together in a way that feels unorganized". Held here: at phone width the clock and date are one row in one .clockbox; the
state line follows below it at a larger size and takes its row, its age on its own quiet line under it; the wrap line and the events are a
separate row under a hairline, and none of those three groups overlaps another or pushes the page wider than the viewport. From the tablet
width up the band's top padding is the --sp-5 token, so the gap under the places bar is at least that.

Sizes and gaps are read from the stylesheet's own tokens at run time, so a token change moves the assertions with it.

NOT held (a person judges them): whether the phone's head reads as organised, on his real data and a real iPhone's text rendering. The
page here is the fixture install: a short state line and the wrap line the fixture yields."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_narrow_layout_rig import drive_touch
from tests.test_cockpit_places import READY, SETTLE

# the fixture install draws no sky, so the events line is empty and the page hides it; it gets the text the sky would write there (and is
# shown, as the sky shows it), so the status row holds both of its groups as it does on a live page
MEASURE = r"""(() => {
  const band = document.getElementById('ck-band'), $ = (s) => band.querySelector(s);
  const ev = $('[data-ck-hero=events]'); if (ev && !ev.textContent.trim()) { ev.textContent = '3 events today'; ev.hidden = false; }
  const px = (v) => { const p = document.createElement('i'); p.style.cssText = 'position:absolute;visibility:hidden;width:' + v; band.appendChild(p); const w = p.getBoundingClientRect().width; p.remove(); return w; };
  const R = (e) => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, w: r.width, h: r.height }; };
  const fs = (e) => parseFloat(getComputedStyle(e).fontSize);
  const clock = $('.clock'), date = $('.date'), box = $('.clockbox'), text = $('.band-state .ftext'), age = $('.band-state .fage');
  const meta = document.getElementById('ck-wrapline'), wrap = $('.wrapline'), events = $('.events');
  return new Promise((done) => setTimeout(() => done(JSON.stringify({
    vw: document.documentElement.clientWidth, sw: document.documentElement.scrollWidth,
    tokSp5: px('var(--sp-5)'), tokMeta: px('var(--fs-meta)'),
    padTop: parseFloat(getComputedStyle(band).paddingTop),
    clock: R(clock), date: R(date), clockbox: R(box), boxOfClock: clock.closest('.clockbox') === box && date.closest('.clockbox') === box,
    text: text && R(text), age: age && R(age), meta: R(meta), wrap: R(wrap), events: R(events), eventsText: events.textContent.trim(),    wrapText: wrap.textContent.trim(), metaBorderTop: parseFloat(getComputedStyle(meta).borderTopWidth),
    fsText: text && fs(text), fsAge: age && fs(age), fsDate: fs(date), fsClock: fs(clock),
    tabsBottom: R(document.getElementById('ck-phonetabs')).bottom, dir: getComputedStyle(band).flexDirection,
  }))), 400); })()"""

# a bare install (no state line, or a hero render that failed): the slot holds a hidden state line, then nothing, and the wrap line is emptied.
# Each is measured after layout: the band is then the clock row alone, with no gap spent on a row that is not there and no hairline over nothing.
EMPTIED = r"""(() => {
  const band = document.getElementById('ck-band'), slot = document.getElementById('ck-state-slot'), meta = document.getElementById('ck-wrapline');
  const read = () => { const when = document.getElementById('ck-when'), cs = (e) => getComputedStyle(e);
    return { slot: cs(slot).display, meta: cs(meta).display, metaBorder: parseFloat(cs(meta).borderTopWidth), metaPad: parseFloat(cs(meta).paddingTop),
      extra: band.getBoundingClientRect().height - parseFloat(cs(band).paddingTop) - parseFloat(cs(band).paddingBottom) - when.getBoundingClientRect().height,
      sw: document.documentElement.scrollWidth, vw: document.documentElement.clientWidth }; };
  const line = slot.querySelector('.stateline');
  const out = { full: read() };
  meta.replaceChildren();
  if (line) line.hidden = true;
  out.hidden = read();
  slot.replaceChildren();
  out.absent = read();
  return JSON.stringify(out); })()"""


def _open(tmp_path: Path, steps: list, touch: bool) -> list[dict]:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        run = drive_touch if touch else _drive
        out = run(tmp_path, f"{url}/#write_token=T", steps)
    rows = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{")]
    assert rows, out["values"]
    return rows


def _hit(a: dict, b: dict) -> bool:
    """Two rects share area (a shared edge is not an overlap)."""
    return a["left"] < b["right"] - 0.5 and b["left"] < a["right"] - 0.5 and a["top"] < b["bottom"] - 0.5 and b["top"] < a["bottom"] - 0.5


@needs_browser
@pytest.mark.parametrize("width", [390, 360])
def test_the_phone_head_is_three_ordered_groups(tmp_path: Path, width: int) -> None:
    """At 390 and 360 px on Home: the clock row, the state line under it, the status row under a hairline; no overlap, no sideways scroll."""
    g = _open(tmp_path, [{"emulate": {"w": width, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE, {"eval": MEASURE}], touch=True)[-1]
    clock, date, box, text, age, meta = g["clock"], g["date"], g["clockbox"], g["text"], g["age"], g["meta"]
    assert g["vw"] == width, g
    # 1. the clock and the date are one row, in one clockbox
    mid = lambda r: (r["top"] + r["bottom"]) / 2  # noqa: E731
    assert g["boxOfClock"] and abs(mid(clock) - mid(date)) <= 4, g
    # 2. the state text starts below the clock row, takes the row (wider than the clock and date together), and is the bigger type
    assert text is not None and text["top"] >= box["bottom"] - 1, g
    assert text["w"] > (date["right"] - clock["left"]), g
    assert g["fsText"] > g["fsDate"] and g["fsText"] > g["fsAge"], g
    # 3. the age is under the state text, at no more than the meta size
    assert age is not None and age["top"] >= text["bottom"] - 1, g
    assert g["fsAge"] <= g["tokMeta"] + 0.01, g
    # 4. the status row is under the state line, ruled above, holds the wrap line and the events; the groups do not overlap
    assert meta["top"] >= age["bottom"] - 1 and g["metaBorderTop"] > 0, g
    assert g["wrapText"] and g["eventsText"], g
    for part in (g["wrap"], g["events"]):
        assert part["top"] >= meta["top"] - 1 and part["bottom"] <= meta["bottom"] + 1 and part["right"] <= meta["right"] + 1, g
    groups = {"clock": box, "state": {"top": text["top"], "bottom": age["bottom"], "left": min(text["left"], age["left"]), "right": max(text["right"], age["right"])}, "status": meta}
    names = list(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not _hit(groups[a], groups[b]), (a, b, g)
    assert not _hit(g["wrap"], g["events"]), g
    assert g["sw"] <= g["vw"], g


@needs_browser
def test_the_desktop_and_tablet_head_has_room_under_the_places_bar(tmp_path: Path) -> None:
    """From 721 px up the band's top padding is the --sp-5 token and the clock sits at least that far below the places bar; no sideways scroll."""
    steps: list = []
    for w in (1440, 768):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": MEASURE}]
    rows = _open(tmp_path, [{"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE] + steps, touch=False)
    assert len(rows) == 2, rows
    for g, w in zip(rows, (1440, 768)):
        assert g["vw"] == w, g
        assert g["tokSp5"] > 0 and g["padTop"] == g["tokSp5"], (w, g)
        assert g["clock"]["top"] - g["tabsBottom"] >= g["tokSp5"] - 0.5, (w, g)
        assert g["sw"] <= g["vw"], (w, g)


@needs_browser
def test_the_breakpoint_is_between_720_and_721(tmp_path: Path) -> None:
    """At 720 px the band is the phone's column, at 721 the desktop's row; neither scrolls sideways, and both keep the --sp-5 top padding."""
    steps: list = []
    for w in (721, 720):
        steps += [{"emulate": {"w": w, "h": 900, "dpr": 1}}, SETTLE, {"eval": MEASURE}]
    rows = _open(tmp_path, [{"emulate": {"w": 721, "h": 900, "dpr": 1}}, READY, SETTLE] + steps, touch=False)
    assert len(rows) == 2, rows
    for g, w, direction in zip(rows, (721, 720), ("row", "column")):
        assert g["vw"] == w and g["dir"] == direction, (w, g)
        assert g["sw"] <= g["vw"] and g["tokSp5"] > 0 and g["padTop"] == g["tokSp5"], (w, g)


@needs_browser
def test_an_empty_state_slot_and_wrap_line_leave_no_gap_and_no_hairline_on_a_phone(tmp_path: Path) -> None:
    """A bare install has no visible state line or wrap line: the band is the clock row alone, with no 16 px gap spent on the empty slot and no
    hairline or padding drawn over an empty status row."""
    g = _open(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE, {"eval": EMPTIED}], touch=True)[-1]
    assert g["full"]["slot"] != "none" and g["full"]["metaBorder"] > 0 and g["full"]["extra"] > 1, g       # the full head spends rows on both
    for case in ("hidden", "absent"):
        r = g[case]
        assert r["slot"] == "none" and r["meta"] == "none", (case, g)
        assert abs(r["extra"]) <= 1, (case, g)                                                                # the clock row alone: no gap under it
        assert r["sw"] <= r["vw"], (case, g)
