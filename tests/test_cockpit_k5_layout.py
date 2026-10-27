"""K5, re-arrangeable panels: collapse and reorder cards within a zone, kept per browser under one localStorage key. The page draws the
kernel order when storage is absent, garbage or throws; a stack side is kept; the layout survives a re-render and a tab switch; an F7
empty line gets no controls. K5_DESIGN.md is the spec."""
from __future__ import annotations

import json

import pytest
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


def _run(tmp_path: Path, steps: list, width: int = 1440, frag: str = BOARD) -> list:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/{frag}", [{"emulate": {"w": width, "h": 900, "dpr": 1}}, READY, SETTLE] + steps)["values"][2:]


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
        {"eval": "const b = [...document.querySelectorAll('[data-layout-reset]')].filter(x => x.getClientRects().length > 0)[0]; "
                 "const r = [b.textContent.trim(), !!b.closest('.zone-head'), !!b.closest('.masthead')]; b.click(); r"},
        SETTLE, {"eval": STACKS}, {"eval": STORED}, {"eval": f"{PANEL % 'tray'}.classList.contains('ck-collapsed')"},
        {"eval": "document.querySelectorAll('[data-layout-reset]').length"},
    ])
    moved, stored, reset_ui, after, stored_after, folded, left = out[2], out[3], out[4], out[6], out[7], out[8], out[9]
    assert moved["left"] == ["now", "tray"] and stored["collapsed"] == ["tray"], (moved, stored)
    assert reset_ui == ["Reset layout", True, False], reset_ui     # in a zone head, never in the bar
    assert after["left"] == ["tray", "now"] and stored_after is None and folded is False, (after, stored_after, folded)
    assert left == 0, left     # nothing saved any more: no reset control anywhere


@needs_browser
def test_no_saved_layout_draws_no_reset_control_and_a_saved_one_draws_it_in_the_zone_head_only(tmp_path: Path) -> None:
    ALL = "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).map(b => [!!b.closest('.zone-head'), !!b.closest('.masthead')])"
    for width in (1440, 390):
        none = _run(tmp_path / f"n{width}", [{"eval": ALL}, {"eval": "document.querySelectorAll('.masthead [data-layout-reset]').length"}], width=width)
        assert none == [[], 0], (width, none)
        saved = _run(tmp_path / f"s{width}", _seed("JSON.stringify({v: 1, zones: {operate: {order: ['now', 'tray']}}, collapsed: []})") + [{"eval": ALL}], width=width)[-1]
        assert saved and all(z and not m for z, m in saved), (width, saved)
    folded = _run(tmp_path / "f", [{"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE, {"eval": ALL}])[-1]
    assert folded and all(z and not m for z, m in folded), folded       # a collapse alone is a saved layout


@needs_browser
def test_the_entity_name_in_the_bar_is_not_truncated_at_1440_with_a_saved_layout(tmp_path: Path) -> None:
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, zones: {operate: {order: ['now', 'tray']}}, collapsed: ['loops']})") + [
        {"eval": "const e = document.getElementById('entity'); [e.scrollWidth, e.clientWidth, e.textContent]"}])[-1]
    assert out[0] <= out[1], out


# a full bar just above the stacked-layout breakpoint, in a font wider than the design's: Verdana forced on every element (inline, a <style> is blocked)
FULL_BAR = ("(() => { %s"
            " const n = document.getElementById('entity'); n.textContent = 'WWWWWWWWWWWWWW'; %s"
            " const q = (s) => document.querySelector(s).getBoundingClientRect(), vw = document.documentElement, ue = document.querySelector('.masthead .unit');"
            " ue.style.flex = 'none'; const nat = Math.round(q('.masthead .unit').width), natE = n.scrollWidth; ue.style.flex = '';"
            " return new Promise(r => setTimeout(r, 200)).then(() => { const m = q('.masthead'), t = q('.masthead .seg'), u = q('.masthead .unit'), ro = q('.masthead .readout');"
            " const jl = document.querySelector('.jar-label');"
            " return { sw: vw.scrollWidth, cw: vw.clientWidth, unitLeft: Math.round(u.left), tabsRight: Math.round(t.right), readoutRight: Math.round(ro.right), mastRight: Math.round(m.right),"
            " nameCut: n.scrollWidth > n.clientWidth, unitW: Math.round(u.width), nat, nameShrunk: n.clientWidth < Math.min(natE, n.clientWidth + 1) && Math.round(u.width) < nat, dbg: [Math.round(u.width), Math.round(ro.width), Math.round(ro.left), Math.round(q('.masthead .indicators').width), Math.round(q('.masthead .themesw').width), Math.round(q('.masthead .brand').width), Math.round(t.left)], nameW: n.clientWidth, jarCut: jl ? jl.scrollWidth > jl.clientWidth : null }; }); })()")
VERDANA = "for (const e of document.querySelectorAll('*')) e.style.fontFamily = 'Verdana, sans-serif';"
NO_JAR = "document.getElementById('jar').hidden = true;"
LONG_JAR = ("const j = document.getElementById('jar'); j.hidden = false; j.innerHTML = '<svg viewBox=\"0 0 16 20\"></svg><span class=\"jar-label\">'"
            " + 'a jar label that is far too long to fit beside the name'.repeat(3) + '</span>';")


@needs_browser
@pytest.mark.parametrize("font, width", [("", 1180), ("", 1240), (VERDANA, 1280), (VERDANA, 1340)], ids=["native-1180", "native-1240", "verdana-1280", "verdana-1340"])
def test_a_full_bar_truncates_the_name_with_an_ellipsis_and_never_overflows_into_the_tabs(tmp_path: Path, width: int, font: str) -> None:
    out = _run(tmp_path, [{"eval": FULL_BAR % (font, NO_JAR)}], width=width)[-1]
    assert out["sw"] <= out["cw"], out                                     # nothing widens the page
    assert out["unitLeft"] >= out["tabsRight"], out                        # the readout stays right of the tabs
    assert out["readoutRight"] <= out["mastRight"], out
    assert out["unitW"] <= out["nat"], out
    assert out["unitW"] == out["nat"] or out["nameCut"], out               # a name the bar cannot hold whole ends in an ellipsis rather than being clipped off the bar


@needs_browser
@pytest.mark.parametrize("font, width", [("", 1400), ("", 1500), (VERDANA, 1460), (VERDANA, 1560)], ids=["native-1400", "native-1500", "verdana-1460", "verdana-1560"])
def test_the_jar_label_gives_way_before_the_name_does(tmp_path: Path, width: int, font: str) -> None:
    got = _run(tmp_path, [{"eval": FULL_BAR % (font, LONG_JAR)}], width=width)[-1]
    assert got["sw"] <= got["cw"] and got["unitLeft"] >= got["tabsRight"], got
    assert got["jarCut"] is True, got                                      # the label is what shrank (its text overflows its box: ellipsized)
    assert got["nat"] - got["unitW"] <= 1, got                                 # while the name kept its whole width


@needs_browser
def test_a_pinned_card_gets_no_move_control_and_every_move_control_shown_changes_the_order(tmp_path: Path) -> None:
    ORDER = "[...document.querySelectorAll('.ck-zgrid .panel')].map(p => p.dataset.ckPanel).join(',')"
    out = _run(tmp_path, [
        {"eval": "[...document.querySelectorAll('.ck-zgrid [data-ck-panel=\"episodes\"] .ck-lay-up, .ck-zgrid [data-ck-panel=\"episodes\"] .ck-lay-down')].filter(b => !b.disabled && b.getClientRects().length > 0).length"},
        {"eval": "document.querySelectorAll('.ck-zgrid [data-ck-panel=\"episodes\"] .ck-lay-fold').length"},
        {"eval": "(async () => { const ord = () => " + ORDER + "; const res = [];"
                 " const n = [...document.querySelectorAll('.ck-zgrid .ck-lay-up, .ck-zgrid .ck-lay-down')].filter(b => !b.disabled && b.getClientRects().length > 0).length;"
                 " for (let i = 0; i < n; i++) { const b = [...document.querySelectorAll('.ck-zgrid .ck-lay-up, .ck-zgrid .ck-lay-down')].filter(b => !b.disabled && b.getClientRects().length > 0)[i]; if (!b) break;"
                 " const before = ord(); b.click(); await new Promise(r => setTimeout(r, 400)); res.push([b.getAttribute('aria-label'), before !== ord()]); } return res; })()"},
    ])
    episodes_moves, episodes_fold, moves = out
    assert episodes_moves == 0, episodes_moves           # CSS `order` pins it: a move would be a no-op
    assert episodes_fold == 1                            # collapse stays
    assert moves and all(ch for _l, ch in moves), moves


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
    assert resets == [], resets      # nothing saved: no reset control at all


# ---- fix2 (L3 round 1: N1 to N6) ----
TAB = "(m) => { document.querySelector('[data-mode-tab=\"' + m + '\"]').click(); return new Promise(r => setTimeout(r, 300)).then(() => 1); }"
SEQ = ("(() => [...document.querySelectorAll('.ck-zgrid .panel')].filter(p => p.getClientRects().length > 0)"
       ".map(p => { const r = p.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.left), p.dataset.ckPanel]; })"
       ".sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(x => x[2]).join(','))()")
DRAFT = "(() => { const ta = document.querySelector('.tray-dump-ta'); ta.value = 'half a thought'; return !!ta; })()"
SHOWN_MOVES = ("[...document.querySelectorAll('.ck-zgrid .ck-lay-up, .ck-zgrid .ck-lay-down')]"
               ".filter(b => !b.disabled && b.getClientRects().length > 0)")


@needs_browser
def test_no_enabled_move_is_a_no_op_in_any_tab_by_the_visible_sequence(tmp_path: Path) -> None:
    """N1: a neighbour is a VISIBLE, MOVABLE card in rendered order. Morning shows the Tray alone (Now is another tab's card)."""
    steps: list = []
    for m in ("morning", "board", "build"):
        steps += [{"eval": f"({TAB})('{m}')"},
                  {"eval": "(async () => { const seq = () => " + SEQ + "; const res = [];"
                           f" const n = {SHOWN_MOVES}.length;"
                           f" for (let i = 0; i < n; i++) {{ const b = {SHOWN_MOVES}[0]; if (!b) break;"
                           " const before = seq(); b.click(); await new Promise(r => setTimeout(r, 400));"
                           f" res.push(['{m}', b.getAttribute('aria-label'), before !== seq()]); break; }}"
                           f" return [res, {SHOWN_MOVES}.length]; }})()"}]
    out = _run(tmp_path, steps)
    morning, board, build = out[1], out[3], out[5]
    assert morning[1] == 0, morning                                      # the Tray alone: it has nothing visible to swap with
    assert board[0] and all(ch for _m, _l, ch in board[0]), board        # a move shown on the Board moves something you can see
    assert build[1] == 0, build


@needs_browser
def test_an_unsaved_draft_disables_the_layout_controls_and_nothing_is_written(tmp_path: Path) -> None:
    """N2 + N3: the gate is checked before any write; while a draft is open the controls are disabled with a reason."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE, {"eval": STORED},     # a saved layout, so Reset exists
        {"eval": DRAFT},
        # no input event yet: the click itself must refuse
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); {PANEL % 'keep'}.querySelector('.ck-lay-fold').click(); "
                 "document.querySelector('[data-layout-reset]').click(); 1"},
        SETTLE, {"eval": STORED}, {"eval": STACKS},
        {"eval": "document.querySelector('.tray-dump-ta').dispatchEvent(new Event('input', {bubbles: true})); new Promise(r => setTimeout(r, 200)).then(() => 1)"},
        {"eval": f"[...{PANEL % 'now'}.querySelectorAll('.ck-lay-btn')].map(b => [b.disabled, b.title])"},
        {"eval": "[...document.querySelectorAll('[data-layout-reset]')].map(b => [b.disabled, b.title])"},
    ])
    saved, after, stacks, btns, resets = out[2], out[6], out[7], out[9], out[10]
    assert saved["collapsed"] == ["tray"], saved
    assert after == saved, (saved, after)                                 # no write: no stale order, no stale Reset
    assert stacks["left"] == ["tray", "now"], stacks
    assert btns and all(d and "finish or cancel the edit first" in t for d, t in btns), btns
    assert resets and all(d and "finish or cancel the edit first" in t for d, t in resets), resets


@needs_browser
def test_a_card_with_an_open_edit_row_is_not_folded(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"(() => {{ const b = {PANEL % 'tray'}.querySelector(':scope > .pbody'); const d = document.createElement('div'); d.className = 'text-editor';"
                 " d.innerHTML = '<textarea class=\"edit-ta\" data-orig=\"x\">x</textarea>'; b.appendChild(d); return 1; })()"},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), {STORED}]"},
    ])
    assert out[-1] == [False, None], out[-1]


@needs_browser
def test_the_handlers_resolve_the_card_and_grid_at_click_time(tmp_path: Path) -> None:
    """N4: a surviving (adopted) panel is moved by its key now, not the key of the render that built its buttons."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'now'}.dataset.ckCard = 'keep'; {PANEL % 'now'}.querySelector('.ck-lay-up').disabled = false; "
                 f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); 1"},
        SETTLE, {"eval": STACKS}, {"eval": STORED},
    ])
    stacks = out[-2]
    assert stacks["right"] == ["keep", "loops"] and stacks["left"] == ["tray", "now"], stacks


@needs_browser
def test_storage_that_refuses_writes_still_lets_the_controls_work_for_the_pages_life(tmp_path: Path) -> None:
    """N5: an in-memory layout backs the page when setItem throws."""
    out = _run(tmp_path, [
        {"eval": "Storage.prototype.setItem = () => { throw new Error('quota'); }; 1"},
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-up').click(); 1"}, SETTLE, {"eval": STACKS},
        {"eval": "document.getElementById('refresh').click(); new Promise(r => setTimeout(r, 800)).then(() => 1)"}, {"eval": STACKS},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('[data-layout-reset]').length > 0]"},
    ])
    assert out[3]["left"] == ["now", "tray"], out[3]
    assert out[5]["left"] == ["now", "tray"], out[5]                      # survives a redraw
    assert out[-1] == [True, True], out[-1]


@needs_browser
def test_a_zone_id_is_never_interpolated_into_a_selector(tmp_path: Path) -> None:
    """N6: nodes are found by comparing dataset values."""
    out = _run(tmp_path, [
        {"eval": "window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); 1"},
        {"eval": f"(() => {{ const p = {PANEL % 'now'}; p.closest('.ck-zgrid').dataset.ckZone = 'op\"]x'; p.querySelector('.ck-lay-up').click(); return 1; }})()"},
        SETTLE, {"eval": "window.__errs"},
    ])
    assert out[-1] == [], out[-1]


# every enabled, shown move control must move something on screen: clicked one by one from the state the page is in
EVERY_MOVE = ("(async () => { const seq = () => " + SEQ + "; const res = [];"
              f" const labels = {SHOWN_MOVES}.map(b => b.getAttribute('aria-label'));"
              " for (const l of labels) {"
              f"   const b = {SHOWN_MOVES}.find(x => x.getAttribute('aria-label') === l); if (!b) continue;"
              "   const before = seq(); b.click(); await new Promise(r => setTimeout(r, 400));"
              "   res.push([l, before !== seq()]); }"
              f" return [res, {SHOWN_MOVES}.length]; }})()")


@needs_browser
def test_a_tabbed_card_folds_as_a_whole_by_its_card_key(tmp_path: Path) -> None:
    """P1: fold is stored by CARD key and every panel of the card is folded, whichever tab shows."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'keep'}.dataset.ckCard = 'now'; 1"},                      # keep joins now's card: a two-panel card
        {"eval": f"{PANEL % 'now'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": f"[{PANEL % 'now'}.classList.contains('ck-collapsed'), {PANEL % 'keep'}.classList.contains('ck-collapsed'), {STORED}]"},
        {"eval": f"{PANEL % 'keep'}.querySelector('.ck-lay-fold').getAttribute('aria-expanded')"},
        {"eval": f"{PANEL % 'keep'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": f"[{PANEL % 'now'}.classList.contains('ck-collapsed'), {PANEL % 'keep'}.classList.contains('ck-collapsed'), {STORED}]"},
    ])
    assert out[3] == [True, True, {"v": 1, "zones": {}, "collapsed": ["now"]}], out[3]
    assert out[4] == "false", out[4]
    assert out[7][:2] == [False, False] and out[7][2]["collapsed"] == [], out[7]


@needs_browser
def test_a_stored_panel_id_that_is_not_a_card_key_folds_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, zones: {}, collapsed: ['no-such-card']})")
               + [{"eval": "document.querySelectorAll('.ck-zgrid .panel.ck-collapsed').length"}])
    assert out[-1] == 0, out[-1]


@needs_browser
def test_a_key_switch_and_an_initial_build_load_leave_no_no_op_move_enabled(tmp_path: Path) -> None:
    """P2: the controls refresh from applyMode, not only from a click."""
    key2 = "document.dispatchEvent(new KeyboardEvent('keydown', {key: '%s', bubbles: true})); new Promise(r => setTimeout(r, 400)).then(() => 1)"
    out = _run(tmp_path, [{"eval": key2 % "1"}, {"eval": EVERY_MOVE}, {"eval": key2 % "3"}, {"eval": EVERY_MOVE}])
    for r in (out[1], out[3]):
        assert all(ch for _l, ch in r[0]), r
    build = _run(tmp_path / "b", [{"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"eval": EVERY_MOVE}], frag="#write_token=T&build")
    assert build[1][0] == [] or all(ch for _l, ch in build[1][0]), build


@needs_browser
def test_every_enabled_move_in_every_tab_moves_something(tmp_path: Path) -> None:
    """P4: the N1 test without its break: every enabled control, tab by tab."""
    steps: list = []
    for m in ("morning", "board", "build"):
        steps += [{"eval": f"({TAB})('{m}')"}, {"eval": EVERY_MOVE}]
    out = _run(tmp_path, steps)
    for i in (1, 3, 5):
        assert all(ch for _l, ch in out[i][0]), out[i]
    assert out[3][0], out[3]                                                  # the Board has moves to test


@needs_browser
def test_a_store_that_refuses_removal_leaves_an_empty_layout_not_the_old_one(tmp_path: Path) -> None:
    """P3: Reset with a read-only store: the page draws the default; the stored copy the page cannot remove is not read back."""
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, zones: {operate: {order: ['now', 'tray']}}, collapsed: ['tray']})") + [
        {"eval": "Storage.prototype.setItem = () => { throw new Error('quota'); }; Storage.prototype.removeItem = () => { throw new Error('ro'); }; 1"},
        {"eval": f"{PANEL % 'keep'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": "document.querySelector('[data-layout-reset]').click(); new Promise(r => setTimeout(r, 600)).then(() => 1)"},
        {"eval": STACKS},
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('[data-layout-reset]').length]"},
    ])
    assert out[-2]["left"] == ["tray", "now"], out[-2]
    assert out[-1] == [False, 0], out[-1]
