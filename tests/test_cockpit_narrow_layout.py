"""OS-3, the narrow layout, on the one nav: at a phone's width (390, touch) the page puts what waits on him before the graph, keeps
every view's content one tap away through the nav's destinations (Board, Inbox, Grants) and never scrolls sideways; at a desktop's
width the order is the wide page's own. The strip of jumps this file once held is retired (W1c): the destinations in `#ck-nav` are
what reaches each view, and tests/test_cockpit_one_nav.py holds that every destination is reachable at each width. Browser tests:
each drives the page once over the narrow-layout rig."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import (DESKTOP, NEEDS, PHONE_TOUCH, SETTLE, TOK, FullRig, _drive, drive_touch,
                                                  needs_browser, needs_ssh)

WEB = Path(__file__).resolve().parent.parent / "levain" / "templates" / "web"

# where things sit, as one JSON string
LAYOUT = """(() => { const q = (s) => document.querySelector(s);
  const heroBox = (e) => { const c = getComputedStyle(e); return [c.paddingTop, c.paddingRight, c.paddingBottom, c.paddingLeft, c.marginTop, c.marginBottom, c.borderTopWidth, c.borderBottomWidth].join(' ') + ' ' + c.backgroundColor + ' ' + c.backgroundImage; }; const top = (e) => e ? Math.round(e.getBoundingClientRect().top + window.scrollY) : null;
  return JSON.stringify({ strip: !!q('#ck-phonetabs'), band: top(q('#ck-band')), needs: top(q('#ck-needs')), drop: top(q('#ck-drop')), sky: top(q('#ck-skycard')), scrollW: document.documentElement.scrollWidth,
    vw: document.documentElement.clientWidth, heroDisplay: getComputedStyle(q('#ck-hero')).display, heroBox: heroBox(q('#ck-hero')) }); })()"""

# the block the nav's Inbox destination goes to: the Inbox panel, else the notifications
INBOX_BLOCK = """(document.querySelector('.panel[data-ck-panel="inbox"]') || document.querySelector('.panel[data-ck-panel="notifications"]'))"""


def _dest(which: str) -> list:
    """Tap one nav destination from the page's top and read where its block landed (px below the viewport's top) and how far the page moved."""
    return [{"eval": "window.scrollTo(0, 0); 1"},
            {"eval": f"document.querySelector('.ck-dest[data-dest={which}]').click(); 1"},
            {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = {INBOX_BLOCK}; return JSON.stringify({{ which: '{which}', top: e ? Math.round(e.getBoundingClientRect().top) : null, y: Math.round(window.scrollY) }}); }})()"}]


@needs_browser
@needs_ssh
def test_a_phone_puts_the_needs_first_and_the_inbox_destination_goes_to_its_block(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": LAYOUT}, *_dest("inbox")]
    with rig.serving():
        values = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    layout, inbox = values[0], values[1]
    assert layout["strip"] is False, layout   # the strip is gone: the nav is the one way to a view
    assert layout["scrollW"] <= layout["vw"], layout   # no sideways scroll
    # the hero's two children join the deck's column without the hero's own box: it carries none for the flatten to lose
    assert layout["heroDisplay"] == "contents" and layout["heroBox"] == "0px 0px 0px 0px 0px 0px 0px 0px rgba(0, 0, 0, 0) none", layout
    # the state line (the band), then what waits on him, then the graph
    assert layout["band"] < layout["needs"] < layout["sky"], layout
    # the drop sits directly under the needs (Phill 10-30), before the graph
    assert layout["needs"] < layout["drop"] < layout["sky"], layout
    # the Inbox destination lands its block near the top of the screen (it was the notifications tab's property), and the page moved to get there
    assert inbox["top"] is not None and 0 <= inbox["top"] < 140 and inbox["y"] > 0, inbox


# the Decisions view's reads answer late: the page's own fetch is held 4.5 s for the two the Grants destination waits on, longer than a
# short fixed window would wait. It runs as an init script (the page is loaded again under it), so the page's first render and its
# cached reads already pass through it; each held read keeps its start and the moment it was let go.
SLOW_READS = ("(() => { const f = window.fetch.bind(window); window.__slow = [];"
              " window.fetch = (u, o) => { if (!/\\/cockpit\\/(consent\\/grants|coord\\/drops)\\.json/.test(String(u))) return f(u, o);"
              " const rec = { start: Date.now(), release: 0 }; window.__slow.push(rec);"
              " return new Promise(r => setTimeout(r, 4500)).then(() => { rec.release = Date.now(); return f(u, o); }); }; })();")


def _in_view(which: str, find: str) -> list:
    """Tap one of the nav destinations that opens the Decisions view with the reads held, wait for the POPULATED node `find` (a JS expression),
    give the jump time to settle, and read where that node sits in the view's scrolling body, with how many held reads were still held at the tap."""
    return [SETTLE, {"eval": f"window.__tap = Date.now(); document.querySelector('.ck-dest[data-dest={which}]').click(); 1"},
            {"until": find, "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = {find}; const bodyEl = document.querySelector('#levain-modal .modal-body'); const body = bodyEl.getBoundingClientRect();"
                     f" const r = e.getBoundingClientRect(); return JSON.stringify({{ which: '{which}', open: !!document.getElementById('levain-modal'),"
                     f" populated: r.height > 0, inBody: r.top >= body.top - 1 && r.top < body.bottom - 40, top: Math.round(r.top), bodyTop: Math.round(body.top),"
                     f" bodyH: Math.round(body.height), scrolled: Math.round(bodyEl.scrollTop), scrollH: bodyEl.scrollHeight,"
                     f" heldAtTap: window.__slow.filter(s => s.release - window.__tap >= 3000).length }}); }})()"}]


@needs_browser
@needs_ssh
def test_the_grants_destination_opens_the_decisions_view_at_the_standing_grants(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    find = "[...document.querySelectorAll('#levain-modal .ext-grouphead')].find(h => /standing grants/.test(h.textContent))"
    steps = [*PHONE_TOUCH, {"init": SLOW_READS}, {"until": NEEDS, "ms": 30000}, *_in_view("grants", find)]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[-1]["heldAtTap"] >= 1, json.dumps(got[-1])   # a read was still held when the destination was tapped: the jump had to wait for it
    assert got[-1]["open"] and got[-1]["populated"] and got[-1]["inBody"] and got[-1]["scrolled"] > 0, json.dumps(got[-1])


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
             {"eval": "document.querySelector('.ck-dest[data-dest=grants]').click(); 1"},   # the tap opens the view; its wait for the held read begins
             {"eval": reopen}, {"until": row, "ms": 30000}, {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
             {"eval": "JSON.stringify({ step: 'after', scrolled: Math.round(document.querySelector('#levain-modal .modal-body').scrollTop) })"}]
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert got[0] == {"step": "reopen", "closed": True, "opened": True, "another": True}, got   # the second view really is another one
    # the row is in the second view, and the first tap's wait, which saw it fill, left that view where it opened
    assert got[1] == {"step": "after", "scrolled": 0}, got


def _hidden_dest(mode: str, which: str, panel: str) -> list:
    """Save `mode` as the page's view (the way a reader's last view is kept), tap one destination and read the panel's state before and after."""
    return [{"eval": f"window.LevainCockpitKinds.applyMode('{mode}', true); window.scrollTo(0, 0); 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"},
            {"eval": f"window.__before = !!document.querySelector('{panel}').getClientRects().length; document.querySelector('.ck-dest[data-dest={which}]').click(); 1"},
            {"eval": "new Promise(r => setTimeout(r, 900)).then(() => 1)"},
            {"eval": f"(() => {{ const e = document.querySelector('{panel}'); return JSON.stringify({{ mode: '{mode}', which: '{which}', drawnBefore: window.__before,"
                     f" drawnAfter: !!e.getClientRects().length, top: Math.round(e.getBoundingClientRect().top), view: document.body.dataset.mode }}); }})()"}]


@needs_browser
@needs_ssh
def test_the_board_and_inbox_destinations_reveal_a_panel_the_saved_view_hides(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    steps = [*PHONE_TOUCH, {"until": NEEDS, "ms": 30000}, SETTLE]
    for mode in ("build", "memory"):
        steps += _hidden_dest(mode, "board", ".panel[data-ck-panel=\"tray\"]") + _hidden_dest(mode, "inbox", ".panel[data-ck-panel=\"notifications\"]")
    with rig.serving():
        got = [json.loads(v) for v in drive_touch(tmp_path, url, steps)["values"] if isinstance(v, str) and v.startswith("{")]
    assert len(got) == 4, got
    for g in got:
        assert not g["drawnBefore"], g   # the saved view really hid it
        assert g["drawnAfter"] and g["view"] == "board", g   # the tap revealed its view
    for g in got:
        if g["which"] == "inbox":
            assert 0 <= g["top"] < 140, g   # the Inbox destination also lands its block near the top


@needs_browser
@needs_ssh
def test_a_desktop_keeps_the_graph_above_the_needs(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    url = f"{rig.origin}/#write_token={TOK}"
    with rig.serving():
        out = _drive(tmp_path, url, [DESKTOP, {"until": NEEDS, "ms": 30000}, SETTLE, {"eval": LAYOUT},
                                     {"emulate": {"w": 721, "h": 900, "dpr": 1}}, SETTLE, {"eval": LAYOUT}])["values"]
    layouts = [json.loads(v) for v in out if isinstance(v, str) and v.startswith("{")]
    assert len(layouts) == 2, layouts
    for layout in layouts:   # the widest width the narrow block does not reach draws what the desktop does
        assert layout["heroDisplay"] != "contents", layout
        assert layout["band"] < layout["sky"] < layout["needs"], layout   # the wide page's own order


def test_the_page_holds_no_strip_and_no_phone_jump() -> None:
    """The nav is the one way to a view: nothing of the old strip (its markup, its styles, its jump, its data attribute) is left to drift."""
    for name in ("dashboard.html", "dashboard.css", "dashboard_core.js", "cockpit_view.js", "dashboard_boot.js"):
        text = (WEB / name).read_text()
        for dead in ("ck-phonetabs", "phoneJump", "data-phone-jump", "LevainPhoneJump"):
            assert dead not in text, (name, dead)
