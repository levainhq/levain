"""K5, re-arrangeable panels: collapse and reorder cards within a zone, kept per browser under one localStorage key. The page draws the
kernel order when storage is absent, garbage or throws; a stack side is kept; the layout survives a re-render and a tab switch; an F7
empty line gets no controls. K5_DESIGN.md is the spec."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

KEY = "levain_cockpit_layout_v1"
BOARD = "#write_token=T&board"
DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
READY = {"until": "document.querySelector('.ck-zgrid[data-ck-zone=\"operate\"] .panel[data-ck-panel=\"tray\"] .pbody')"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 500)).then(() => 1)"}

# the Board's two stacks, as the page drew them: [left ids, right ids, loose ids]
STACKS = """(() => { const g = document.querySelector('.ck-zgrid[data-ck-zone="operate"]');
  const ids = (n) => [...n.querySelectorAll(':scope > .panel')].map(p => p.dataset.ckPanel);
  const cols = [...g.querySelectorAll(':scope > .ck-col')];
  return { left: ids(cols[0]), right: ids(cols[1]), loose: ids(g) }; })()"""
PANEL = "document.querySelector('.ck-zgrid[data-ck-zone=\"operate\"] .panel[data-ck-panel=\"%s\"]')"
STORED = f"(() => {{ try {{ return JSON.parse(localStorage.getItem('{KEY}')); }} catch (e) {{ return 'err'; }} }})()"
# a reload that the next `until` can tell from the page it replaces
RELOAD = "window.__old = 1; location.reload(); 1"
FRESH = "!window.__old && " + READY["until"]


def _run(tmp_path: Path, steps: list, width: int = 1440) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{BOARD}", [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, SETTLE] + steps)["values"][2:]


def _seed(value: str) -> list:
    """Set the stored layout (a JS expression for the string), reload, wait for the fresh page."""
    return [{"eval": f"localStorage.setItem('{KEY}', {value}); {RELOAD}"}, {"until": FRESH}, SETTLE]


@needs_browser
def test_the_default_board_is_the_kernel_order_with_controls_on_cards_and_none_on_the_f7_line(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": STACKS},
        {"eval": "[...document.querySelectorAll('.ck-zgrid .panel.ck-dash')].map(p => [p.dataset.ckPanel, p.querySelectorAll('.ck-lay-btn').length])"},
        {"eval": "document.querySelectorAll('.ck-zgrid[data-ck-zone=\"operate\"] .panel[data-ck-panel=\"tray\"] .ck-lay-btn').length"},
        {"eval": "[...document.querySelectorAll('.ck-hero .ck-lay-btn, #ck-skycol .ck-lay-btn, #ck-band .ck-lay-btn, #ck-widgets .ck-lay-btn, #ck-brief .ck-lay-btn')].length"},
    ])
    stacks, dashes, tray_btns, fixed = out
    assert stacks["left"] == ["tray", "now"] and stacks["right"] == ["loops", "keep"], stacks   # the default the CSS order rules used to draw
    assert dashes and all(n == 0 for _id, n in dashes), dashes        # an F7 empty line gets no controls
    assert tray_btns == 3, tray_btns                                    # earlier, later, collapse
    assert fixed == 0, fixed                                            # the now band, the sky, the widgets and the brief are fixed


@needs_browser
def test_a_stored_order_is_applied_within_each_stack(tmp_path: Path) -> None:
    order = json.dumps({"v": 1, "zones": {"operate": {"order": ["now", "tray", "keep", "loops"]}}, "collapsed": []})
    out = _run(tmp_path, _seed(json.dumps(order)) + [{"eval": STACKS}])
    assert out[-1]["left"] == ["now", "tray"] and out[-1]["right"] == ["keep", "loops"], out[-1]


@needs_browser
def test_cards_the_stored_order_does_not_name_follow_in_kernel_order_and_unknown_names_are_ignored(tmp_path: Path) -> None:
    order = json.dumps({"v": 1, "zones": {"operate": {"order": ["ghost", "keep"]}}, "collapsed": []})
    out = _run(tmp_path, [{"eval": STACKS}] + _seed(json.dumps(order)) + [{"eval": STACKS}])
    before, after = out[0], out[-1]
    assert after["right"] == ["keep", "loops"], after                   # keep named first; the unnamed loops follows
    assert after["left"] == ["tray", "now"], after                      # unnamed: kernel order, nothing vanished
    assert sorted(after["loose"] + after["left"] + after["right"]) == sorted(before["loose"] + before["left"] + before["right"])


@needs_browser
def test_garbage_in_storage_is_ignored_and_the_board_draws_the_kernel_order(tmp_path: Path) -> None:
    junk = ["'not json{'", "'[]'", "'null'", "'7'", "JSON.stringify({v: 2, zones: {operate: {order: ['now','tray']}}})",
            "JSON.stringify({v: 1, zones: {operate: {order: 'now'}}, collapsed: 5})",
            "JSON.stringify({v: 1, zones: {operate: {order: [1, null, {}]}}, collapsed: [3]})"]
    steps: list = []
    for j in junk:
        steps += _seed(j) + [{"eval": STACKS}, {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('.ck-lay-btn').length > 0]"}]
    out = _run(tmp_path, steps)
    for i in range(len(junk)):
        stacks, (collapsed, has_controls) = out[i * 5 + 3], out[i * 5 + 4]
        assert stacks["left"] == ["tray", "now"] and stacks["right"] == ["loops", "keep"], (junk[i], stacks)
        assert collapsed is False and has_controls, (junk[i], collapsed, has_controls)


@needs_browser
def test_storage_that_throws_still_renders_and_a_click_does_not_break_the_page(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": "Storage.prototype.getItem = () => { throw new Error('denied'); }; Storage.prototype.setItem = () => { throw new Error('denied'); }; "
                 "document.getElementById('refresh').click(); new Promise(r => setTimeout(r, 800)).then(() => 1)"},
        {"eval": STACKS},
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); {PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE,
        {"eval": f"[{PANEL % 'tray'} !== null, document.querySelectorAll('.ck-zgrid .panel .pbody').length > 3]"},
    ])
    assert out[1]["left"] == ["tray", "now"], out[1]
    assert out[-1] == [True, True], out[-1]


@needs_browser
def test_collapse_hides_the_body_keeps_the_head_and_round_trips_through_storage(tmp_path: Path) -> None:
    fold = f"{PANEL % 'tray'}.querySelector('.ck-lay-fold')"
    state = (f"(() => {{ const p = {PANEL % 'tray'}, b = p.querySelector(':scope > .pbody'), f = p.querySelector('.ck-lay-fold');"
             " return { expanded: f.getAttribute('aria-expanded'), controls: f.getAttribute('aria-controls'), bodyId: b.id,"
             " bodyShown: b.getClientRects().length > 0, headShown: p.querySelector(':scope > .phead').getClientRects().length > 0,"
             " title: p.querySelector('h2').textContent }; })()")
    out = _run(tmp_path, [
        {"eval": state}, {"eval": f"{fold}.click(); 1"}, {"eval": state}, {"eval": STORED},
        {"eval": f"{fold}.click(); 1"}, {"eval": state}, {"eval": STORED},
    ])
    open0, _c, shut, stored1, _c2, open1, stored2 = out
    assert open0["expanded"] == "true" and open0["bodyShown"] and open0["controls"] == open0["bodyId"] != "", open0
    assert shut["expanded"] == "false" and not shut["bodyShown"] and shut["headShown"] and shut["title"], shut
    assert stored1 == {"v": 1, "zones": {}, "collapsed": ["tray"]}, stored1
    assert open1["expanded"] == "true" and open1["bodyShown"], open1
    assert stored2["collapsed"] == [], stored2


@needs_browser
def test_a_reorder_and_a_collapse_survive_the_live_refresh_and_a_tab_switch(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); {PANEL % 'keep'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE, {"eval": STACKS},
        {"eval": "document.getElementById('refresh').click(); new Promise(r => setTimeout(r, 800)).then(() => 1)"},
        {"eval": STACKS},
        {"eval": "document.querySelector('[data-mode-tab=\"morning\"]').click(); new Promise(r => setTimeout(r, 300)).then(() => 1)"},
        {"eval": "document.querySelector('[data-mode-tab=\"board\"]').click(); new Promise(r => setTimeout(r, 300)).then(() => 1)"},
        {"eval": STACKS}, {"eval": f"{PANEL % 'keep'}.classList.contains('ck-collapsed')"},
        {"eval": f"{PANEL % 'keep'}.querySelector('.ck-lay-fold').getAttribute('aria-expanded')"},
    ])
    first, refreshed, back, keep_folded, keep_aria = out[2], out[4], out[7], out[8], out[9]
    assert first["left"] == ["now", "tray"], first
    assert refreshed["left"] == ["now", "tray"], refreshed
    assert back["left"] == ["now", "tray"] and keep_folded is True and keep_aria == "false", (back, keep_folded, keep_aria)


@needs_browser
def test_a_card_moves_within_its_own_stack_side_and_the_ends_do_not_wrap(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"[{PANEL % 'tray'}.querySelector('.ck-lay-up').disabled, {PANEL % 'tray'}.querySelector('.ck-lay-down').disabled, {PANEL % 'loops'}.querySelector('.ck-lay-up').disabled]"},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-down').click(); 1"}, SETTLE, {"eval": STACKS},
        {"eval": f"{PANEL % 'loops'}.querySelector('.ck-lay-down').click(); 1"}, SETTLE, {"eval": STACKS},
        {"eval": f"{PANEL % 'loops'}.querySelector('.ck-lay-up').click(); 1"}, SETTLE, {"eval": STACKS}, {"eval": STORED},
    ])
    ends, s1, s2, s3, stored = out[0], out[3], out[6], out[9], out[10]
    assert ends == [True, False, True], ends                            # first card of a side cannot move earlier
    assert s1["left"] == ["now", "tray"] and s1["right"] == ["loops", "keep"], s1
    assert s2["left"] == ["now", "tray"] and s2["right"] == ["keep", "loops"], s2     # neither move crossed to the other side
    assert s3["right"] == ["loops", "keep"], s3
    assert stored["v"] == 1 and "operate" in stored["zones"] and isinstance(stored["zones"]["operate"]["order"], list), stored


@needs_browser
def test_reset_layout_clears_the_key_and_redraws_the_kernel_order(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); {PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE, {"eval": STACKS}, {"eval": STORED},
        {"eval": "const b = document.querySelector('.themesw ~ [data-layout-reset], [data-layout-reset]'); const r = b.getBoundingClientRect(); "
                 "const t = document.querySelector('.themesw').getBoundingClientRect(); b.click(); [b.textContent.trim(), Math.abs(r.top - t.top) < 40]"},
        SETTLE, {"eval": STACKS}, {"eval": STORED}, {"eval": f"{PANEL % 'tray'}.classList.contains('ck-collapsed')"},
    ])
    moved, stored, reset_ui, after, stored_after, folded = out[2], out[3], out[4], out[6], out[7], out[8]
    assert moved["left"] == ["now", "tray"] and stored["collapsed"] == ["tray"], (moved, stored)
    assert reset_ui[0] == "Reset layout" and reset_ui[1] is True, reset_ui     # beside the theme switch
    assert after["left"] == ["tray", "now"] and stored_after is None and folded is False, (after, stored_after, folded)


@needs_browser
def test_the_controls_are_keyboard_reachable_buttons_with_names_at_phone_width(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"[...{PANEL % 'tray'}.querySelectorAll('.ck-lay-btn')].map(b => [b.tagName, b.type, b.getAttribute('aria-label'), b.getClientRects().length > 0])"},
        {"eval": "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"},
        {"eval": "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).map(b => [b.textContent.trim(), !!b.closest('.ck-zone')])"},
    ], width=390)
    btns, no_hscroll, resets = out
    assert len(btns) == 3 and all(t == "BUTTON" and ty == "button" and label and shown for t, ty, label, shown in btns), btns
    assert no_hscroll is True, "the controls must not widen the page at 390"
    assert resets == [["Reset layout", True]], resets      # the bar is full at 390: one reset, in the zone head
