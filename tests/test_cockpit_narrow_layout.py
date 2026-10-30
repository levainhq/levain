"""OS-3, the narrow layout: at a phone's width (390, touch) the cockpit draws the strip that jumps to each phone view, puts what waits
on him before the graph, keeps every view's content one tap away and never scrolls sideways; at a desktop's width (1440) the strip
is not drawn and the order is the wide page's own. Browser tests: each drives the page once over the narrow-layout rig."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import (DESKTOP, NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, _drive, drive_touch,
                                                  needs_browser, needs_ssh)

CSS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css"
HTML = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.html"

TABS = ["needs", "board", "notes", "grants", "drop"]

# where things sit and how the strip draws, as one JSON string
LAYOUT = """(() => { const q = (s) => document.querySelector(s); const top = (e) => e ? Math.round(e.getBoundingClientRect().top + window.scrollY) : null;
  const strip = q('#ck-phonetabs'); const cs = getComputedStyle(strip);
  return JSON.stringify({ display: cs.display, position: cs.position, tabs: [...strip.querySelectorAll('button')].map(b => b.dataset.phoneJump),
    tabH: Math.min(...[...strip.querySelectorAll('button')].map(b => Math.round(b.getBoundingClientRect().height))),
    stripFits: strip.scrollWidth <= strip.clientWidth, stripTop: top(strip), band: top(q('#ck-band')), needs: top(q('#ck-needs')), sky: top(q('#ck-skycard')), scrollW: document.documentElement.scrollWidth,
    vw: document.documentElement.clientWidth }); })()"""


def _jump(which: str, panel: str) -> list:
    """Click one strip tab from the page's top and read where its target landed (px below the viewport's top) and how far the page moved."""
    return [{"eval": "window.scrollTo(0, 0); 1"},
            {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); 1"},
            {"eval": "new Promise(r => setTimeout(r, 700)).then(() => 1)"},
            {"eval": f"(() => {{ const e = document.querySelector('{panel}'); return JSON.stringify({{ which: '{which}', top: e ? Math.round(e.getBoundingClientRect().top) : null, y: Math.round(window.scrollY) }}); }})()"}]


@needs_browser
@needs_ssh
def test_a_phone_gets_the_strip_the_needs_first_and_a_jump_to_each_page_view(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": LAYOUT},
             *_jump("needs", "#ck-needs"), *_jump("board", ".panel[data-ck-panel=\"tray\"]"),
             *_jump("notes", ".panel[data-ck-panel=\"notifications\"]")]
    with rig.serving():
        values = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    layout, jumps = values[0], {v["which"]: v for v in values[1:]}
    assert layout["display"] == "flex" and layout["position"] == "sticky" and layout["tabs"] == TABS
    assert layout["tabH"] >= 40, layout   # a thumb's height
    assert layout["stripFits"], layout    # all five tabs show at 390, none cut at the edge
    assert layout["scrollW"] <= layout["vw"], layout   # no sideways scroll
    # the state line (the band), then what waits on him, then the graph
    assert layout["stripTop"] < layout["band"] < layout["needs"] < layout["sky"], layout
    # the board and notification tabs land their block below the sticky strip; needs is at the top already
    for which in ("board", "notes"):
        j = jumps[which]
        assert j["top"] is not None and 40 <= j["top"] < 140 and j["y"] > 0, j
    assert jumps["needs"]["top"] is not None and 40 <= jumps["needs"]["top"] < 140, jumps["needs"]


def _in_view(which: str, find: str) -> list:
    return [SETTLE, {"eval": f"document.querySelector('[data-phone-jump={which}]').click(); 1"},
            {"until": find, "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = {find}; const body = document.querySelector('#levain-modal .modal-body').getBoundingClientRect();"
                     f" const r = e.getBoundingClientRect(); return JSON.stringify({{ which: '{which}', open: !!document.getElementById('levain-modal'),"
                     f" inBody: r.top >= body.top - 1 && r.top < body.bottom - 40, top: Math.round(r.top), bodyTop: Math.round(body.top),"
                     f" bodyH: Math.round(body.height), scrolled: Math.round(document.querySelector('#levain-modal .modal-body').scrollTop),"
                     f" scrollH: document.querySelector('#levain-modal .modal-body').scrollHeight, cls: document.querySelector('#levain-modal .modal-body').className }}); }})()"}]


@needs_browser
@needs_ssh
def test_the_grants_tab_opens_the_decisions_view_at_the_standing_grants(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    find = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => /standing grants/.test(h.textContent))"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, *_in_view("grants", find)]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[-1]["open"] and got[-1]["inBody"] and got[-1]["scrolled"] > 0, json.dumps(got[-1])


@needs_browser
@needs_ssh
def test_the_drop_tab_opens_the_decisions_view_at_the_coordinator_drop(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    find = "document.querySelector('#levain-modal .coord-drop-slot')"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, *_in_view("drop", find)]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[-1]["open"] and got[-1]["inBody"] and got[-1]["scrolled"] > 0, json.dumps(got[-1])


@needs_browser
@needs_ssh
def test_a_desktop_draws_no_strip_and_keeps_the_graph_above_the_needs(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    with rig.serving():
        out = _drive(tmp_path, url, [DESKTOP, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": LAYOUT}])["values"]
    layout = json.loads([v for v in out if isinstance(v, str) and v.startswith("{")][-1])
    assert layout["display"] == "none", layout
    assert layout["band"] < layout["sky"] < layout["needs"], layout   # the wide page's own order


def test_the_strip_is_hidden_by_default_and_drawn_only_in_the_narrow_block() -> None:
    css = CSS.read_text()
    assert ".ck-phonetabs { display: none; }" in css
    narrow = css.split("OS-3: THE NARROW LAYOUT", 1)[1]
    flex_at = narrow.index("display: flex; gap: 0; position: sticky")
    assert "@media (max-width: 720px)" in narrow[:flex_at] and "@media" not in narrow[narrow.index("@media (max-width: 720px)") + 1:flex_at]
    html = HTML.read_text()
    assert [html.count(f'data-phone-jump="{t}"') for t in TABS] == [1] * len(TABS)
