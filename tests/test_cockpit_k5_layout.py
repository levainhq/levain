"""K5, collapsible panels: a card folds, kept per browser under one localStorage key (levain#172 retired the reorder arrows and the saved
order). The page draws every card unfolded when storage is absent, garbage or throws; the fold survives a re-render and a tab switch; an F7
empty line gets no controls; an order saved by an older browser is ignored."""
from __future__ import annotations

import json

import pytest
from pathlib import Path

from tests._cockpit_reread import REREAD_SETTLED
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


# a reload whose fresh page never drew: CI (levain#236, shard 0) kept a page whose scripts and styles had not loaded ("Cannot read properties of
# undefined (reading 'fromManifest')", the board never drawn a .pbody) and waited out the whole bound on it. A load that is fine draws well inside
# the 8000 ms first wait below, so a page still not fresh by then is reloaded ONCE and waited on for the usual bound; a fresh page is left alone.
RETRY_IF_STALE = "(() => { if (" + FRESH + ") return 'fresh'; window.__old = 1; location.reload(); return 'reloaded'; })()"


def _seed_once_more(value: str) -> list:
    """`_seed`, with the one bounded reload for a fresh page that never drew: five steps, the first three as `_seed`'s read."""
    return [{"eval": f"localStorage.setItem('{KEY}', {value}); {RELOAD}"}, {"until": FRESH, "ms": 8000, "may_timeout": True},
            {"eval": RETRY_IF_STALE}, {"until": FRESH}, SETTLE]


@needs_browser
def test_the_default_board_is_the_kernel_order_with_controls_on_cards_and_none_on_the_f7_line(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": STACKS},
        {"eval": "[...document.querySelectorAll('.ck-zgrid .panel.ck-dash')].map(p => [p.dataset.ckPanel, p.querySelectorAll('.ck-lay-btn').length])"},
        {"eval": "document.querySelectorAll('.ck-zgrid[data-ck-zone=\"operate\"] .panel[data-ck-panel=\"tray\"] .ck-lay-btn').length"},
        {"eval": "[...document.querySelectorAll('.ck-hero .ck-lay-btn, #ck-skycol .ck-lay-btn, #ck-band .ck-lay-btn, #ck-widgets .ck-lay-btn')].length"},
    ])
    stacks, dashes, tray_btns, fixed = out
    assert stacks["left"] == ["tray"] and stacks["right"] == [], stacks   # the default the CSS order rules used to draw (no Now, no Keep)
    assert dashes and all(n == 0 for _id, n in dashes), dashes        # an F7 empty line gets no controls
    assert tray_btns == 1, tray_btns                                    # collapse only: no move arrows
    assert fixed == 0, fixed                                            # the now band, the sky and the widgets are fixed


@needs_browser
def test_garbage_in_storage_is_ignored_and_the_board_draws_the_kernel_order(tmp_path: Path) -> None:
    junk = ["'not json{'", "'[]'", "'null'", "'7'", "JSON.stringify({v: 2, zones: {operate: {order: ['now','tray']}}})",
            "JSON.stringify({v: 1, zones: {operate: {order: 'now'}}, collapsed: 5})",
            "JSON.stringify({v: 1, zones: {operate: {order: [1, null, {}]}}, collapsed: [3]})"]
    steps: list = []
    per = len(_seed_once_more("0")) + 2                 # one junk's values: the seed's, then the stacks, then the collapsed/controls read
    for j in junk:
        steps += _seed_once_more(j) + [{"eval": STACKS}, {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('.ck-lay-btn').length > 0]"}]
    out = _run(tmp_path, steps)
    for i in range(len(junk)):
        stacks, (collapsed, has_controls) = out[i * per + per - 2], out[i * per + per - 1]
        assert stacks["left"] == ["tray"] and stacks["right"] == [], (junk[i], stacks)
        assert collapsed is False and has_controls, (junk[i], collapsed, has_controls)


@needs_browser
def test_storage_that_throws_still_renders_and_a_click_does_not_break_the_page(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": "Storage.prototype.getItem = () => { throw new Error('denied'); }; Storage.prototype.setItem = () => { throw new Error('denied'); }; "
                 + REREAD_SETTLED},
        {"eval": STACKS},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE,
        {"eval": f"[{PANEL % 'tray'} !== null, document.querySelectorAll('.ck-zgrid .panel .pbody').length > 3]"},
    ])
    assert out[1]["left"] == ["tray"], out[1]
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
    assert stored1 == {"v": 1, "collapsed": ["tray"]}, stored1
    assert open1["expanded"] == "true" and open1["bodyShown"], open1
    assert stored2["collapsed"] == [], stored2


@needs_browser
def test_a_collapse_survives_the_live_refresh_and_a_tab_switch(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE, {"eval": STACKS},
        {"eval": REREAD_SETTLED},
        {"eval": STACKS},
        {"eval": "document.querySelector('[data-mode-tab=\"memory\"]').click(); new Promise(r => setTimeout(r, 300)).then(() => 1)"},
        {"eval": "document.querySelector('[data-mode-tab=\"board\"]').click(); new Promise(r => setTimeout(r, 300)).then(() => 1)"},
        {"eval": STACKS}, {"eval": f"{PANEL % 'tray'}.classList.contains('ck-collapsed')"},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').getAttribute('aria-expanded')"},
    ])
    first, refreshed, back, tray_folded, tray_aria = out[2], out[4], out[7], out[8], out[9]
    assert first["left"] == ["tray"] and first["right"] == [], first
    assert refreshed["left"] == ["tray"] and refreshed["right"] == [], refreshed
    assert back["left"] == ["tray"] and back["right"] == [] and tray_folded is True and tray_aria == "false", (back, tray_folded, tray_aria)


@needs_browser
def test_reset_layout_clears_the_key_and_unfolds_every_panel(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE, {"eval": STORED},
        {"eval": "const b = [...document.querySelectorAll('[data-layout-reset]')].filter(x => x.getClientRects().length > 0)[0]; "
                 "const r = [b.textContent.trim(), !!b.closest('.zone-head'), !!b.closest('.masthead'), b.title]; b.click(); r"},
        SETTLE, {"eval": STORED}, {"eval": f"{PANEL % 'tray'}.classList.contains('ck-collapsed')"},
        {"eval": "document.querySelectorAll('[data-layout-reset]').length"},
    ])
    stored, reset_ui, stored_after, folded, left = out[2], out[3], out[5], out[6], out[7]
    assert stored["collapsed"] == ["tray"], stored
    assert reset_ui[:3] == ["Reset layout", True, False] and "nfold" in reset_ui[3], reset_ui     # in a zone head, never in the bar; it says it unfolds
    assert stored_after is None and folded is False, (stored_after, folded)
    assert left == 0, left     # nothing folded any more: no reset control anywhere


@needs_browser
def test_no_saved_layout_draws_no_reset_control_and_a_saved_one_draws_it_in_the_zone_head_only(tmp_path: Path) -> None:
    ALL = "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).map(b => [!!b.closest('.zone-head'), !!b.closest('.masthead')])"
    for width in (1440, 390):
        none = _run(tmp_path / f"n{width}", [{"eval": ALL}, {"eval": "document.querySelectorAll('.masthead [data-layout-reset]').length"}], width=width)
        assert none == [[], 0], (width, none)
        saved = _run(tmp_path / f"s{width}", _seed("JSON.stringify({v: 1, collapsed: ['tray']})") + [{"eval": ALL}], width=width)[-1]
        assert saved and all(z and not m for z, m in saved), (width, saved)
    folded = _run(tmp_path / "f", [{"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE, {"eval": ALL}])[-1]
    assert folded and all(z and not m for z, m in folded), folded       # a fold is the saved layout


@needs_browser
def test_a_layout_that_only_names_the_retired_now_is_no_saved_layout(tmp_path: Path) -> None:
    ALL = "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).length"
    stale = "JSON.stringify({v: 1, zones: {operate: {order: ['now']}}, collapsed: ['now']})"
    out = _run(tmp_path, _seed(stale) + [{"eval": ALL}, {"eval": STACKS}])
    assert out[-2] == 0, out[-2]                                         # no Reset layout control for a layout that is only stale
    assert out[-1]["left"] == ["tray"] and out[-1]["right"] == [], out[-1]


@needs_browser
def test_an_old_saved_order_alone_is_no_saved_layout_and_draws_no_reset_control(tmp_path: Path) -> None:
    ALL = "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).length"
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, zones: {operate: {order: ['tray', 'edits']}}, collapsed: []})") + [{"eval": ALL}])
    assert out[-1] == 0, out[-1]


@needs_browser
def test_the_entity_name_is_drawn_once_in_the_bar_by_the_brand_at_1440_with_a_saved_layout(tmp_path: Path) -> None:
    """levain#141 polish: the masthead names the entity once: #entity is hidden only where it repeats the wordmark, and stays in the markup either way."""
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, collapsed: ['loops']})") + [
        {"eval": "const e = document.getElementById('entity'), w = document.querySelector('.masthead .wordmark'); [!!e, getComputedStyle(e.closest('.unit')).display, e.getClientRects().length > 0, w.getClientRects().length > 0, e.textContent.trim().toLowerCase() === w.textContent.trim().toLowerCase()]"}])[-1]
    drawn = not out[4]                                                   # drawn only where it differs from the wordmark (a repeat is hidden)
    assert out[0] and out[3] and out[2] is drawn and (out[1] != "none") is drawn, out


# a full bar just above the stacked-layout breakpoint, in a font wider than the design's: Verdana forced on every element (inline, a <style> is blocked)
FULL_BAR = ("(() => { %s"
            " const q = (s) => document.querySelector(s).getBoundingClientRect(), vw = document.documentElement;"
            " return new Promise(r => setTimeout(r, 200)).then(() => { const m = q('.masthead'), t = q('.masthead .brand'), ro = q('.masthead .readout');"
            " return { sw: vw.scrollWidth, cw: vw.clientWidth, readoutLeft: Math.round(ro.left), brandRight: Math.round(t.right), readoutRight: Math.round(ro.right), mastRight: Math.round(m.right) }; }); })()")
VERDANA = "for (const e of document.querySelectorAll('*')) e.style.fontFamily = 'Verdana, sans-serif';"


@needs_browser
@pytest.mark.parametrize("font, width", [("", 1180), ("", 1240), (VERDANA, 1280), (VERDANA, 1340)], ids=["native-1180", "native-1240", "verdana-1280", "verdana-1340"])
def test_a_full_bar_never_widens_the_page_and_keeps_the_readout_right_of_the_brand(tmp_path: Path, width: int, font: str) -> None:
    out = _run(tmp_path, [{"eval": FULL_BAR % font}], width=width)[-1]
    assert out["sw"] <= out["cw"], out                                     # nothing widens the page
    assert out["readoutLeft"] >= out["brandRight"], out                    # the readout stays right of the brand
    assert out["readoutRight"] <= out["mastRight"], out


# the masthead from the stacked layout to a wide bar, in the font that crowds it most. Each drawn block of the readout is
# tested against the brand's box on both axes (a stacked layout puts them in different rows, so only a real overlap counts).
# (levain#141 polish: the entity name and the jar left the readout; this holds what is left of it.)
MID_BAR = ("(() => { %s"
           " const R = (e) => { const r = e.getBoundingClientRect(); return { l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width, h: r.height }; };"
           " return new Promise(r => setTimeout(r, 250)).then(() => { const br = R(document.querySelector('.masthead .brand'));"
           " const drawn = (e) => e.getClientRects().length > 0 && R(e).w > 0 && R(e).h > 0;"
           " const hit = [...document.querySelectorAll('.masthead .indicators > *, .masthead .ia-act, .masthead .themesw')]"
           "   .filter(drawn).filter(e => { const r = R(e); return r.l < br.r - 0.5 && r.r > br.l + 0.5 && r.t < br.b - 0.5 && r.b > br.t + 0.5; }).map(e => e.id || e.className || e.tagName);"
           " const acts = [...document.querySelectorAll('.ia-actions .ia-act')];"
           " return { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, hit,"
           " wordW: Math.round(R(document.querySelector('.masthead .wordmark')).w), brandRight: Math.round(br.r),"
           " labels: acts.map(a => drawn(a.querySelector('.ia-lbl'))), named: acts.map(a => !!a.getAttribute('aria-label') && !!a.title) }; }); })()")


@needs_browser
@pytest.mark.parametrize("font, width", [
    ("", 820), ("", 1024), ("", 1101), ("", 1140), ("", 1180), ("", 1340), ("", 1440), ("", 1501), ("", 1920),
    (VERDANA, 1101), (VERDANA, 1180), (VERDANA, 1340), (VERDANA, 1501)],
    ids=lambda v: v if isinstance(v, int) else ("verdana" if v == VERDANA else "native"))
def test_the_masthead_keeps_the_brand_and_the_buttons_apart_at_every_width(tmp_path: Path, font: str, width: int) -> None:
    out = _run(tmp_path, [{"eval": MID_BAR % font}], width=width)[-1]
    assert out["sw"] <= out["cw"], out                                     # nothing widens the page
    assert out["hit"] == [], out                                           # no block of the readout is drawn over the brand
    assert out["wordW"] > 40, out                                          # the brand keeps its name (levain#151: the tagline left the header)
    # the buttons' words show where they fit: in the stacked layout and from 1501 up; between, they are icons and keep their names
    assert out["labels"] == [not (1100 < width <= 1500)] * 3, out
    assert out["named"] == [True] * 3, out


# the Calm control beside the name, from just above the stacked breakpoint to a wide bar, in either font
CALM = ("(() => { const b = document.getElementById('calmbtn'), r = b.getBoundingClientRect(); b.focus();"
        " const cs = getComputedStyle(b), pb = getComputedStyle(b, '::before'), g = document.createElement('span'); g.textContent = '\u2248'; g.style.cssText = 'font:' + pb.font + ';letter-spacing:' + pb.letterSpacing + ';padding:0;margin:0;border:0'; b.appendChild(g); const em = g.getBoundingClientRect().width; g.remove(); return { label: cs.fontSize, w: Math.round(r.width), bound: Math.ceil(parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) + em), name: b.getAttribute('aria-label'), title: b.title, focused: document.activeElement === b, shown: r.width > 0 }; })()")


@needs_browser
@pytest.mark.parametrize("font, width", [("", 1101), ("", 1180), ("", 1280), ("", 1281), ("", 1340), ("", 1440),
                                         (VERDANA, 1101), (VERDANA, 1180), (VERDANA, 1280), (VERDANA, 1340), (VERDANA, 1440)],
                         ids=[f"{n}-{w}" for n, w in [("native", 1101), ("native", 1180), ("native", 1280), ("native", 1281), ("native", 1340), ("native", 1440),
                                                      ("verdana", 1101), ("verdana", 1180), ("verdana", 1280), ("verdana", 1340), ("verdana", 1440)]])
def test_calm_beside_the_name_never_overflows_the_bar_and_keeps_its_name_as_an_icon(tmp_path: Path, width: int, font: str) -> None:
    out = _run(tmp_path, [{"eval": FULL_BAR % font}, {"eval": CALM}], width=width)
    geo, calm = out[-2], out[-1]
    assert geo["sw"] <= geo["cw"] and geo["readoutLeft"] >= geo["brandRight"] and geo["readoutRight"] <= geo["mastRight"], geo
    assert calm["shown"] and calm["focused"] and calm["name"] in ("Calm off", "Calm on") and calm["title"] == calm["name"], calm
    # levain#141 polish: Calm left the masthead for the Machine card's control row, so it is the labelled form at every width (the masthead's icon band is gone)
    assert calm["label"] != "0px" and calm["w"] > 60, calm


@needs_browser
def test_the_controls_are_keyboard_reachable_buttons_with_names_at_phone_width(tmp_path: Path) -> None:
    out = _run(tmp_path, [
        {"eval": f"[...{PANEL % 'tray'}.querySelectorAll('.ck-lay-btn')].map(b => [b.tagName, b.type, b.getAttribute('aria-label'), b.getClientRects().length > 0])"},
        {"eval": "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"},
        {"eval": "[...document.querySelectorAll('[data-layout-reset]')].filter(b => b.getClientRects().length > 0).map(b => [b.textContent.trim(), !!b.closest('.ck-zone')])"},
    ], width=390)
    btns, no_hscroll, resets = out
    assert len(btns) == 1 and all(t == "BUTTON" and ty == "button" and label and shown for t, ty, label, shown in btns), btns
    assert no_hscroll is True, "the controls must not widen the page at 390"
    assert resets == [], resets      # nothing saved: no reset control at all


# ---- fix2 (L3 round 1: N2 to N5) ----
DRAFT = "(() => { const ta = document.querySelector('.tray-dump-ta'); ta.value = 'half a thought'; return !!ta; })()"


@needs_browser
def test_an_unsaved_draft_disables_the_layout_controls_and_nothing_is_written(tmp_path: Path) -> None:
    """N2 + N3: the gate is checked before any write; while a draft is open the controls are disabled with a reason."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE, {"eval": STORED},     # a saved layout, so Reset exists
        {"eval": DRAFT},
        # no input event yet: the click itself must refuse
        {"eval": f"{PANEL % 'edits'}.querySelector('.ck-lay-fold').click(); "
                 "document.querySelector('[data-layout-reset]').click(); 1"},
        SETTLE, {"eval": STORED}, {"eval": STACKS},
        {"eval": "document.querySelector('.tray-dump-ta').dispatchEvent(new Event('input', {bubbles: true})); new Promise(r => setTimeout(r, 200)).then(() => 1)"},
        {"eval": f"[...{PANEL % 'edits'}.querySelectorAll('.ck-lay-btn')].map(b => [b.disabled, b.title])"},
        {"eval": "[...document.querySelectorAll('[data-layout-reset]')].map(b => [b.disabled, b.title])"},
    ])
    saved, after, stacks, btns, resets = out[2], out[6], out[7], out[9], out[10]
    assert saved["collapsed"] == ["tray"], saved
    assert after == saved, (saved, after)                                 # no write: no stale fold, no stale Reset
    assert stacks["left"] == ["tray"] and stacks["right"] == [], stacks
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
def test_the_handler_resolves_the_card_at_click_time(tmp_path: Path) -> None:
    """N4: a surviving (adopted) panel is folded by its key now, not the key of the render that built its button."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'edits'}.dataset.ckCard = 'notifications'; {PANEL % 'edits'}.querySelector('.ck-lay-fold').click(); 1"},
        SETTLE, {"eval": STORED},
    ])
    assert out[-1]["collapsed"] == ["notifications"], out[-1]   # the click folded the card its key NOW names


@needs_browser
def test_storage_that_refuses_writes_still_lets_the_controls_work_for_the_pages_life(tmp_path: Path) -> None:
    """N5: an in-memory layout backs the page when setItem throws."""
    out = _run(tmp_path, [
        {"eval": "Storage.prototype.setItem = () => { throw new Error('quota'); }; 1"},
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('[data-layout-reset]').length > 0]"},
        {"eval": REREAD_SETTLED},
        {"eval": f"{PANEL % 'tray'}.classList.contains('ck-collapsed')"},
    ])
    assert out[-3] == [True, True], out[-3]
    assert out[-1] is True, out[-1]                           # survives a redraw


@needs_browser
def test_a_tabbed_card_folds_as_a_whole_by_its_card_key(tmp_path: Path) -> None:
    """P1: fold is stored by CARD key and every panel of the card is folded, whichever tab shows."""
    out = _run(tmp_path, [
        {"eval": f"{PANEL % 'edits'}.dataset.ckCard = 'tray'; 1"},                      # edits joins tray's card: a two-panel card
        {"eval": f"{PANEL % 'tray'}.querySelector('.ck-lay-fold').click(); 1"},
        # wait on the state the next read asserts (both panels folded, the fold stored), not on a fixed settle
        {"until": f"{PANEL % 'tray'}.classList.contains('ck-collapsed') && {PANEL % 'edits'}.classList.contains('ck-collapsed') && (({STORED}) || {{}}).collapsed?.includes('tray')"},
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), {PANEL % 'edits'}.classList.contains('ck-collapsed'), {STORED}]"},
        {"eval": f"{PANEL % 'edits'}.querySelector('.ck-lay-fold').getAttribute('aria-expanded')"},
        {"eval": f"{PANEL % 'edits'}.querySelector('.ck-lay-fold').click(); 1"},
        {"until": f"!{PANEL % 'tray'}.classList.contains('ck-collapsed') && !{PANEL % 'edits'}.classList.contains('ck-collapsed') && !((({STORED}) || {{}}).collapsed || []).length"},
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), {PANEL % 'edits'}.classList.contains('ck-collapsed'), {STORED}]"},
    ])
    assert out[3] == [True, True, {"v": 1, "collapsed": ["tray"]}], out[3]
    assert out[4] == "false", out[4]
    assert out[7][:2] == [False, False] and out[7][2]["collapsed"] == [], out[7]


@needs_browser
def test_a_stored_panel_id_that_is_not_a_card_key_folds_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, zones: {}, collapsed: ['no-such-card']})")
               + [{"eval": "document.querySelectorAll('.ck-zgrid .panel.ck-collapsed').length"}])
    assert out[-1] == 0, out[-1]


@needs_browser
def test_a_store_that_refuses_removal_leaves_an_empty_layout_not_the_old_one(tmp_path: Path) -> None:
    """P3: Reset with a read-only store: the page draws the default; the stored copy the page cannot remove is not read back."""
    out = _run(tmp_path, _seed("JSON.stringify({v: 1, collapsed: ['tray']})") + [
        {"eval": "Storage.prototype.setItem = () => { throw new Error('quota'); }; Storage.prototype.removeItem = () => { throw new Error('ro'); }; 1"},
        {"eval": f"{PANEL % 'edits'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": "document.querySelector('[data-layout-reset]').click(); new Promise(r => setTimeout(r, 600)).then(() => 1)"},
        {"eval": STACKS},
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('[data-layout-reset]').length]"},
    ])
    assert out[-2]["left"] == ["tray"] and out[-2]["right"] == [], out[-2]
    assert out[-1] == [False, 0], out[-1]


# Calm "on" in the icon band: a filled key background like the theme switch's pressed button; the labelled form (outside the band) is unchanged
CALM_ON = ("(() => { const b = document.getElementById('calmbtn'), bg = () => getComputedStyle(b).backgroundColor;"
           " const pressedTheme = document.querySelector('.themesw button[aria-pressed=\"true\"]');"
           " const off = bg(), offPressed = b.getAttribute('aria-pressed'); b.click();"
           " return new Promise(r => setTimeout(r, 500)).then(() => ({ off, offPressed, on: bg(), onPressed: b.getAttribute('aria-pressed'),"
           " theme: pressedTheme ? getComputedStyle(pressedTheme).backgroundColor : null })); })()")


@needs_browser
def test_calm_on_in_the_machine_row_has_a_filled_background_at_1180(tmp_path: Path) -> None:
    # levain#141 polish: Calm is in the Machine control row; its pressed state is the row's filled key (--card-2), no longer the masthead icon band's
    got = _run(tmp_path, [{"eval": CALM_ON}], width=1180)[-1]
    assert got["offPressed"] == "false" and got["onPressed"] == "true", got
    assert got["on"] != got["off"], got


@needs_browser
def test_calm_on_at_1440_has_the_same_filled_background_as_at_1180(tmp_path: Path) -> None:
    # was: the masthead's labelled Calm kept an unfilled look at 1440; the Machine row's key is one look at every width
    got = _run(tmp_path, [{"eval": CALM_ON}], width=1440)[-1]
    assert got["offPressed"] == "false" and got["onPressed"] == "true", got
    assert got["on"] != got["off"], got


@needs_browser
def test_a_saved_order_from_an_old_browser_is_ignored_and_no_card_draws_a_reorder_arrow(tmp_path: Path) -> None:
    """levain#172: the reorder arrows and the saved order are retired; collapse stays and an old stored order is shed, never an error."""
    old = json.dumps({"v": 1, "zones": {"operate": {"order": ["tray", "edits"]}}, "collapsed": ["tray"]})
    out = _run(tmp_path, _seed(json.dumps(old)) + [
        {"eval": "window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); 1"},
        {"eval": "document.querySelectorAll('.ck-lay-up, .ck-lay-down').length"},
        {"eval": f"[{PANEL % 'tray'}.classList.contains('ck-collapsed'), document.querySelectorAll('.ck-zgrid .panel .pbody').length > 0]"},
        {"eval": f"{PANEL % 'edits'}.querySelector('.ck-lay-fold').click(); 1"}, SETTLE,
        {"eval": STORED},
        {"eval": "window.__errs"},
    ])
    arrows, (tray_folded, renders), stored, errs = out[4], out[5], out[8], out[9]
    assert arrows == 0, arrows
    assert tray_folded is True and renders, (tray_folded, renders)
    assert stored is not None and "zones" not in stored and "edits" in stored["collapsed"], stored
    assert errs == [], errs
