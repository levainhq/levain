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
    tabsBottom: R(document.getElementById('ck-phonetabs')).bottom,
  }))), 400); })()"""


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
def test_the_phone_head_is_three_ordered_groups(tmp_path: Path) -> None:
    """At 390 px on Home: the clock row, the state line under it, the status row under a hairline; no overlap, no sideways scroll."""
    g = _open(tmp_path, [{"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}, {"touch": True}, READY, SETTLE, {"eval": MEASURE}], touch=True)[-1]
    clock, date, box, text, age, meta = g["clock"], g["date"], g["clockbox"], g["text"], g["age"], g["meta"]
    assert g["vw"] == 390, g
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
