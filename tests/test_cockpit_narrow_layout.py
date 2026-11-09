"""OS-3, the narrow layout: at a phone's width (390, touch) the cockpit draws the strip that jumps to each phone view, puts what waits
on him before the graph, keeps every view's content one tap away and never scrolls sideways; at a desktop's width (1440) the strip
the strip is drawn too (S4.2) and the order is the wide page's own. Browser tests: each drives the page once over the narrow-layout rig."""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import (DESKTOP, NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, _drive, drive_touch,
                                                  needs_browser, needs_ssh)

CSS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.css"
HTML = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard.html"

TABS = ["needs", "board", "notifications", "grants", "drop", "room"]

# where things sit and how the strip draws, as one JSON string
LAYOUT = """(() => { const q = (s) => document.querySelector(s);
  const heroBox = (e) => { const c = getComputedStyle(e); return [c.paddingTop, c.paddingRight, c.paddingBottom, c.paddingLeft, c.marginTop, c.marginBottom, c.borderTopWidth, c.borderBottomWidth].join(' ') + ' ' + c.backgroundColor + ' ' + c.backgroundImage; }; const top = (e) => e ? Math.round(e.getBoundingClientRect().top + window.scrollY) : null;
  const strip = q('#ck-phonetabs'); const cs = getComputedStyle(strip);
  return JSON.stringify({ display: cs.display, position: cs.position, tabs: [...strip.querySelectorAll('button')].map(b => b.dataset.phoneJump),
    tabH: Math.min(...[...strip.querySelectorAll('button')].map(b => Math.round(b.getBoundingClientRect().height))),
    tabsWhole: [...strip.querySelectorAll('button')].every(b => b.scrollWidth <= b.clientWidth), stripTop: top(strip), band: top(q('#ck-band')), needs: top(q('#ck-needs')), drop: top(q('#ck-drop')), sky: top(q('#ck-skycard')), scrollW: document.documentElement.scrollWidth,
    vw: document.documentElement.clientWidth, heroDisplay: getComputedStyle(q('#ck-hero')).display, heroBox: heroBox(q('#ck-hero')) }); })()"""


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
             {"eval": "document.querySelector('[data-phone-jump=notifications]').click(); 1"},
             {"until": "document.querySelector('#levain-modal .nc-root') && 1", "ms": 20000},
             {"eval": "JSON.stringify({ which: 'notifications', modal: !!document.querySelector('#levain-modal .nc-root') })"}]
    with rig.serving():
        values = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    layout, jumps = values[0], {v["which"]: v for v in values[1:]}
    assert layout["display"] == "flex" and layout["position"] == "sticky" and layout["tabs"] == TABS
    assert layout["tabH"] >= 40, layout   # a thumb's height
    assert layout["tabsWhole"], layout    # every tab shows its whole label at 390 (the strip scrolls for the rest)
    assert layout["scrollW"] <= layout["vw"], layout   # no sideways scroll
    # the hero's two children join the deck's column without the hero's own box: it carries none for the flatten to lose
    assert layout["heroDisplay"] == "contents" and layout["heroBox"] == "0px 0px 0px 0px 0px 0px 0px 0px rgba(0, 0, 0, 0) none", layout
    # the state line (the band), then what waits on him, then the graph
    assert layout["stripTop"] < layout["band"] < layout["needs"] < layout["sky"], layout
    # the drop sits directly under the strip (Phill 10-30), before the graph
    assert layout["needs"] < layout["drop"] < layout["sky"], layout
    # the board tab lands its block below the sticky strip; needs is at the top already; Notifications opens the center's focus view (S4.2)
    j = jumps["board"]
    assert j["top"] is not None and 40 <= j["top"] < 140 and j["y"] > 0, j
    assert jumps["notifications"]["modal"] is True, jumps["notifications"]
    assert jumps["needs"]["top"] is not None and 40 <= jumps["needs"]["top"] < 140, jumps["needs"]


# the Decisions view's reads answer late: the page's own fetch is held 4.5 s for the two the strip's tabs wait on, longer than a
# short fixed window would wait. It runs as an init script (the page is loaded again under it), so the page's first render and its
# cached reads already pass through it; each held read keeps its start and the moment it was let go.
SLOW_READS = ("(() => { const f = window.fetch.bind(window); window.__slow = [];"
              " window.fetch = (u, o) => { if (!/\\/cockpit\\/(consent\\/grants|coord\\/drops)\\.json/.test(String(u))) return f(u, o);"
              " const rec = { start: Date.now(), release: 0 }; window.__slow.push(rec);"
              " return new Promise(r => setTimeout(r, 4500)).then(() => { rec.release = Date.now(); return f(u, o); }); }; })();")


def _in_view(which: str, find: str) -> list:
    """Click one of the Decisions-view tabs with the reads held, wait for the POPULATED node `find` (a JS expression), give the jump
    time to settle, and read where that node sits in the view's scrolling body, with how many held reads were still held at the tap."""
    return [SETTLE, {"eval": f"window.__tap = Date.now(); document.querySelector('[data-phone-jump={which}]').click(); 1"},
            {"until": find, "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = {find}; const bodyEl = document.querySelector('#levain-modal .modal-body'); const body = bodyEl.getBoundingClientRect();"
                     f" const r = e.getBoundingClientRect(); return JSON.stringify({{ which: '{which}', open: !!document.getElementById('levain-modal'),"
                     f" populated: r.height > 0, inBody: r.top >= body.top - 1 && r.top < body.bottom - 40, top: Math.round(r.top), bodyTop: Math.round(body.top),"
                     f" bodyH: Math.round(body.height), scrolled: Math.round(bodyEl.scrollTop), scrollH: bodyEl.scrollHeight,"
                     f" heldAtTap: window.__slow.filter(s => s.release - window.__tap >= 3000).length }}); }})()"}]


@needs_browser
@needs_ssh
def test_the_grants_tab_opens_the_decisions_view_at_the_standing_grants(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    find = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => /standing grants/.test(h.textContent))"
    steps = [*PHONE_TOUCH, {"init": SLOW_READS}, {"until": NEEDS, "ms": 30000}, *_in_view("grants", find)]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[-1]["heldAtTap"] >= 1, json.dumps(got[-1])   # a read was still held when the tab was tapped: the jump had to wait for it
    assert got[-1]["open"] and got[-1]["populated"] and got[-1]["inBody"] and got[-1]["scrolled"] > 0, json.dumps(got[-1])


@needs_browser
@needs_ssh
def test_the_drop_tab_scrolls_to_the_hero_box_and_focuses_it_with_no_view_opened(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, {"until": "document.querySelector('#ck-drop-words:not([aria-disabled=true])')", "ms": 30000}, SETTLE,
             {"eval": "window.scrollTo(0, 2500); 1"}, {"eval": "document.querySelector('[data-phone-jump=drop]').click(); 1"},
             {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
             {"eval": "JSON.stringify({ top: Math.round(document.getElementById('ck-drop').getBoundingClientRect().top), modal: !!document.getElementById('levain-modal'),"
                      " focused: document.activeElement === document.getElementById('ck-drop-words') })"}]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")][-1]
    assert got["modal"] is False and got["focused"] is True and 40 <= got["top"] < 140, got   # below the sticky strip, box focused


@needs_browser
@needs_ssh
def test_a_view_closed_and_opened_again_is_not_scrolled_by_the_wait_of_the_one_before(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    row = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => /standing grants/.test(h.textContent))"
    reopen = ("(() => { const first = document.getElementById('levain-modal');"
              " first.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));"
              " const closed = !document.getElementById('levain-modal'); window.LevainNeeds.history(null);"
              " const second = document.getElementById('levain-modal');"
              " return JSON.stringify({ step: 'reopen', closed, opened: !!second, another: !!second && second !== first }); })()")
    steps = [*PHONE_TOUCH, {"init": SLOW_READS}, {"until": NEEDS, "ms": 30000}, SETTLE,
             {"eval": "document.querySelector('[data-phone-jump=grants]').click(); 1"},   # the tap opens the view; its wait for the held read begins
             {"eval": reopen}, {"until": row, "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
             {"eval": "JSON.stringify({ step: 'after', scrolled: Math.round(document.querySelector('#levain-modal .modal-body').scrollTop) })"}]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[0] == {"step": "reopen", "closed": True, "opened": True, "another": True}, got   # the second view really is another one
    # the row is in the second view, and the first tap's wait, which saw it fill, left that view where it opened
    assert got[1] == {"step": "after", "scrolled": 0}, got


def _hidden_jump(mode: str, which: str, panel: str) -> list:
    """Save `mode` as the page's tab (the way a reader's last tab is kept), click one strip tab and read the panel's state before and after."""
    return [{"eval": f"window.LevainCockpitKinds.applyMode('{mode}', true); window.scrollTo(0, 0); 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": f"window.__before = !!document.querySelector('{panel}').getClientRects().length; document.querySelector('[data-phone-jump={which}]').click(); 1"},
            {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = document.querySelector('{panel}'); return JSON.stringify({{ mode: '{mode}', which: '{which}', drawnBefore: window.__before,"
                     f" drawnAfter: !!e.getClientRects().length, top: Math.round(e.getBoundingClientRect().top), tab: document.body.dataset.mode }}); }})()"}]


@needs_browser
@needs_ssh
def test_the_board_tab_reveals_a_panel_the_saved_tab_hides(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE]
    for mode in ("build", "memory"):
        steps += _hidden_jump(mode, "board", ".panel[data-ck-panel=\"tray\"]")
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert len(got) == 2, got
    for g in got:
        assert not g["drawnBefore"], g   # the saved tab really hid it
        assert g["drawnAfter"] and g["tab"] == "board" and 40 <= g["top"] < 140, g   # the tap revealed its tab and landed under the strip


@needs_browser
@needs_ssh
def test_a_desktop_draws_the_strip_and_keeps_the_graph_above_the_needs(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    with rig.serving():
        out = _drive(tmp_path, url, [DESKTOP, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": LAYOUT},
                                     {"emulate": {"w": 721, "h": 900, "dpr": 1}}, SETTLE, {"eval": LAYOUT}])["values"]
    layouts = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{")]
    assert len(layouts) == 2, layouts
    for layout in layouts:   # the widest width the narrow block does not reach draws what the desktop does, and the strip at every width (S4.2)
        assert layout["display"] == "flex" and layout["position"] == "sticky" and layout["heroDisplay"] != "contents", layout
        assert layout["band"] < layout["sky"] < layout["needs"], layout   # the wide page's own order


@needs_browser
@needs_ssh
def test_a_held_summarys_tap_opens_the_needs_strip_and_changes_nothing_and_an_unknown_view_stays_home(tmp_path: Path, monkeypatch) -> None:
    """A held summary's push carries /?view=needs. The page takes the reader to the Needs-you strip the way the strip's Needs tab does (the
    wide page draws it below the graph, so the move is a real scroll), cleans the address, and writes nothing: both requests still wait
    and no view opens over the page. An unknown `view` value opens at home as before, address untouched."""
    rig = FullRig(tmp_path, monkeypatch)
    waiting = "document.querySelector('#ck-needs[data-waiting]')"
    read = (f"JSON.stringify({{ y: Math.round(window.scrollY), top: Math.round({waiting}.getBoundingClientRect().top), search: location.search,"
            " modal: !!document.getElementById('levain-modal') })")
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/?view=needs#write_token={TOK}", [
            DESKTOP, {"until": f"{waiting} && window.scrollY > 100 && 1", "ms": 40000}, SETTLE, {"eval": read}])["values"]
        after = (rig.state(rig.rid), rig.state(rig.second))
        home = _drive(tmp_path, f"{rig.origin}/?view=bogus#write_token={TOK}", [
            DESKTOP, {"until": f"{waiting} && 1", "ms": 40000}, {"eval": "new Promise(r => setTimeout(r, 2500)).then(() => 1)"}, {"eval": read}])["values"]
    got = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{")][0]
    stay = [json.loads(v) for v in home if isinstance(v, str) and v.startswith("{")][0]
    assert 0 <= got["top"] < 140 and got["y"] > 100 and got["search"] == "" and got["modal"] is False, got
    assert after == ("pending", "pending"), after                       # a deep link only opens the view
    assert stay["y"] == 0 and stay["search"] == "?view=bogus" and stay["modal"] is False and stay["top"] > 140, stay


def test_the_strip_is_hidden_by_default_and_drawn_in_the_narrow_block_and_the_wide_one() -> None:
    css = CSS.read_text()
    assert re.search(r"(^|\n)\.ck-phonetabs\s*\{\s*display:\s*none\s*;?\s*\}", css)
    narrow = css.split("OS-3: THE NARROW LAYOUT", 1)[1]
    blocks = narrow.split("@media")[1:]
    drawn = [b for b in blocks if re.search(r"\.ck-phonetabs\s*\{[^}]*display:\s*flex", b)]
    assert len(drawn) == 2 and {d.lstrip().split(")", 1)[0] for d in drawn} == {"(max-width: 720px", "(min-width: 721px"}, "the strip is drawn at every width: the 720px break and the one above it"
    html = HTML.read_text()
    assert [html.count(f'data-phone-jump="{t}"') for t in TABS] == [1] * len(TABS)
