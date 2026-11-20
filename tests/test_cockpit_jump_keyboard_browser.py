"""levain#181 S1a, the Jump list reached and driven by keyboard alone (headless Chrome on a scratch home, the real cockpit).

The page's Tab order is not fixed globally: panels are reached through Jump, so Jump lists every panel and answers every key.
What these hold: (1) the list is not cut at 14 panels, (2) the selected row follows ArrowDown into view and the input names it
(combobox, aria-activedescendant), (3) Esc, a click outside and Ctrl+K again give focus back to where it was, a choice moves it to
the panel, (4) the Jump button is in the Tab order, carries its shortcut for assistive tech and opens on Enter and Space with the
input focused, at 1280 and 390 wide, (5) the panel a choice lands on shows a focus ring (a pin of behaviour the page already had),
(6) Tab never leaves the input and Esc closes the palette from wherever focus sits in it, (7) a list with no match holds no row that is not
an option and says so outside the list, (8) the list starts at its top when the query changes."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import READY, SETTLE

CTRL = 2
ENTER = {"key": "Enter", "vk": 13, "text": "\r"}
ESC = {"key": "Escape", "vk": 27}
CTRL_K = {"key": "k", "modifiers": CTRL, "vk": 75}
SPACE = {"key": " ", "vk": 32, "text": " "}
TAB = {"key": "Tab", "vk": 9}
SHIFT = 8
SHIFT_TAB = {"key": "Tab", "vk": 9, "modifiers": SHIFT}


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(r, {ms})).then(() => 1)"}


# put scratch panels after the real ones until the board holds 24, so the list is far past 14 whatever the real count is
PAD = ("(() => { const b = document.getElementById('board'); let n = b.querySelectorAll('.panel').length; let k = 0;"
       " while (n < 24) { const p = document.createElement('section'); p.className = 'panel'; p.dataset.ckPanel = 'zz-scratch-' + (k++);"
       " const h = document.createElement('div'); h.className = 'phead'; const t = document.createElement('h2'); t.textContent = 'Scratch panel ' + (k);"
       " h.appendChild(t); p.appendChild(h); b.appendChild(p); n++; } return 1; })()")
# the panel titles the board draws now, in draw order, minus the two the list leaves out
DRAWN = ("[...document.querySelectorAll('#board .panel > .phead h2')].filter(h => !['notifications', 'topology'].includes(h.closest('.panel').dataset.ckPanel))"
         ".map(h => h.textContent)")
LISTED = "[...document.querySelectorAll('.palette-list li .pl')].map(n => n.textContent)"
# the rows of one kind (the .pk column), in list order
LISTED_OF = "(k) => [...document.querySelectorAll('.palette-list li')].filter(li => li.querySelector('.pk').textContent === k).map(li => li.querySelector('.pl').textContent)"
PLACE_BTNS = "document.querySelectorAll('[data-ia-place-btn]').length"
ARROW = "document.querySelector('.palette-input').dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));"
STATE = ("JSON.stringify({probe: 'jump', open: !!document.querySelector('.palette'), "
         " active: document.activeElement === document.querySelector('.palette-input') ? 'input' : (document.activeElement ? (document.activeElement.id || document.activeElement.className || document.activeElement.tagName) : null)})")


def _vals(out: dict) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe"')]


def _run(tmp_path: Path, steps: list[dict], w: int = 1280, h: int = 900) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        return _drive(tmp_path, f"{url}/#write_token=T",
                      [{"emulate": {"w": w, "h": h, "dpr": 1}}, READY, SETTLE, {"eval": PAD}, *steps])


@needs_browser
def test_an_empty_query_lists_every_panel_past_the_fourteenth(tmp_path: Path) -> None:
    steps = [{"eval": "document.getElementById('jumpbtn').click(); 1"},
             {"eval": f"JSON.stringify({{probe: 'cap', listed: ({LISTED_OF})('panel'), all: {LISTED}, places: ({LISTED_OF})('place'), nplaces: {PLACE_BTNS}, drawn: {DRAWN},"
                      " at: Object.fromEntries(window.LevainCockpitKinds.jumpTargets().filter(t => t.kind === 'panel').map(t => [t.label, t.place]))})"}]
    (got,) = _vals(_run(tmp_path, steps))
    assert len(got["drawn"]) > 14, got
    assert [t for t in got["drawn"] if t not in got["all"]] == [], got               # every board panel title is listed, none cut
    # the list groups by place now (the current place's panels first), so the board's draw order holds inside each place (the board's own titles only)
    for place in set(got["at"].values()):
        assert [t for t in got["listed"] if t in got["drawn"] and got["at"].get(t) == place] == [t for t in got["drawn"] if got["at"].get(t) == place], (place, got)
    assert got["nplaces"] > 0 and len(got["places"]) == got["nplaces"], got         # and the places are listed too


READ_SEL = ("(() => { const i = document.querySelector('.palette-input'), l = document.querySelector('.palette-list'),"
            " s = l.querySelector('li[aria-selected=\"true\"]'), a = s.getBoundingClientRect(), b = l.getBoundingClientRect();"
            " return JSON.stringify({probe: 'sel', n: l.querySelectorAll('li').length, id: s.id, ad: i.getAttribute('aria-activedescendant'),"
            " role: i.getAttribute('role'), expanded: i.getAttribute('aria-expanded'), controls: i.getAttribute('aria-controls'), listId: l.id,"
            " opt: s.getAttribute('role'), inView: a.top >= b.top - 1 && a.bottom <= b.bottom + 1, scrolled: l.scrollTop,"
            " scrollable: l.scrollHeight > l.clientHeight}); })()")


@needs_browser
def test_arrowing_down_a_long_list_scrolls_the_selected_row_into_view_and_names_it(tmp_path: Path) -> None:
    steps = [{"eval": "document.getElementById('jumpbtn').click(); 1"}, {"eval": READ_SEL},
             {"eval": f"(() => {{ for (let k = 0; k < 20; k++) {{ {ARROW} }} return 1; }})()"}, _wait(100), {"eval": READ_SEL}]
    first, deep = _vals(_run(tmp_path, steps))
    assert first["role"] == "combobox" and first["expanded"] == "true", first
    assert first["controls"] and first["controls"] == first["listId"], first
    assert first["opt"] == "option" and first["id"] and first["ad"] == first["id"], first
    assert deep["scrollable"] and deep["scrolled"] > 0, deep                       # the list did scroll
    assert deep["inView"], deep                                                    # the selected row is inside the visible list
    assert deep["ad"] == deep["id"] and deep["id"] != first["id"], deep


@needs_browser
def test_escape_and_a_click_outside_and_ctrl_k_again_return_focus(tmp_path: Path) -> None:
    focus_a = {"eval": "window.__a = document.querySelector('.ia-places [data-ia-place-btn]'); window.__a.focus(); 1"}
    back = {"eval": "JSON.stringify({probe: 'jump', open: !!document.querySelector('.palette'), back: document.activeElement === window.__a})"}
    steps = [focus_a, CTRL_K, {"eval": STATE}, ESC, _wait(50), back,                                   # Ctrl+K opens, Esc returns
             focus_a, CTRL_K, {"eval": "document.querySelector('.palette').dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); 1"},
             _wait(50), back,                                                                           # a click outside returns
             focus_a, CTRL_K, CTRL_K, _wait(50), back]                                                  # Ctrl+K again closes and returns
    opened, esc, outside, toggled = _vals(_run(tmp_path, steps))
    assert opened["open"] and opened["active"] == "input", opened
    assert not esc["open"] and esc["back"], esc
    assert not outside["open"] and outside["back"], outside
    assert not toggled["open"] and toggled["back"], toggled


@needs_browser
def test_choosing_a_panel_moves_focus_to_the_panel_and_it_shows_a_focus_ring(tmp_path: Path) -> None:
    """A pin of behaviour the page already had (a chosen panel takes focus and the page draws a ring on it), kept so a change cannot lose it."""
    probe = ("(() => { const a = document.activeElement, cs = a ? getComputedStyle(a) : null;"
             " return JSON.stringify({probe: 'ring', panel: !!a && a.classList.contains('panel'), open: !!document.querySelector('.palette'),"
             " style: cs && cs.outlineStyle, width: cs && cs.outlineWidth, shadow: cs && cs.boxShadow, fv: !!a && a.matches(':focus-visible')}); })()")
    steps = [{"eval": "window.__a = document.querySelector('.ia-places [data-ia-place-btn]'); window.__a.focus(); 1"}, CTRL_K, ENTER, _wait(500), {"eval": probe}]
    (got,) = _vals(_run(tmp_path, steps))
    assert got["panel"] and not got["open"], got                                   # focus moved to the panel, not back to where it was
    assert got["style"] != "none" and got["width"] not in ("0px", "0"), got        # a ring is drawn on it


@needs_browser
def test_the_jump_button_is_tabbable_carries_its_shortcut_and_opens_on_enter_and_space(tmp_path: Path) -> None:
    facts = ("(() => { const b = document.getElementById('jumpbtn'); b.focus(); const r = b.getBoundingClientRect(), cs = getComputedStyle(b);"
             " return JSON.stringify({probe: 'btn', w: window.innerWidth, focused: document.activeElement === b, shown: r.width > 0 && r.height > 0,"
             " display: cs.display, vis: cs.visibility, tab: b.tabIndex, keys: b.getAttribute('aria-keyshortcuts'), label: b.getAttribute('aria-label')}); })()")
    for w in (1280, 390):
        steps = [{"eval": facts}, ENTER, {"eval": STATE}, ESC, _wait(50), {"eval": "document.getElementById('jumpbtn').focus(); 1"}, SPACE, _wait(100), {"eval": STATE}]
        facts_got, by_enter, by_space = _vals(_run(tmp_path / f"w{w}", steps, w=w, h=900 if w > 600 else 800))
        assert facts_got["w"] == w, facts_got
        assert facts_got["focused"] and facts_got["shown"] and facts_got["tab"] >= 0 and facts_got["display"] != "none", facts_got
        assert facts_got["keys"] == "Control+K Meta+K", facts_got
        assert facts_got["label"] == "Jump to a place, a panel or a row", facts_got   # the name a reader hears has no key glyph in it
        assert by_enter["open"] and by_enter["active"] == "input", (w, by_enter)
        assert by_space["open"] and by_space["active"] == "input", (w, by_space)


def shoot_the_open_palette_with_a_long_list(tmp_path: Path, out_dir: str) -> None:
    """Not a test (no test_ prefix, nothing collects it): run by hand to look at the open palette, ``shoot_the_open_palette_with_a_long_list(tmp, dir)``."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for w, h in ((1280, 900), (390, 800)):
        steps = [{"eval": "document.getElementById('jumpbtn').click(); 1"}, {"eval": f"(() => {{ for (let k = 0; k < 17; k++) {{ {ARROW} }} return 1; }})()"},
                 _wait(150), {"shot": str(Path(out_dir) / f"palette_{w}.png")}]
        _run(tmp_path / f"s{w}", steps, w=w, h=h)


IN_PALETTE = ("(() => { const a = document.activeElement; return JSON.stringify({probe: 'tab', open: !!document.querySelector('.palette'),"
              " input: a === document.querySelector('.palette-input'), body: a === document.body, opener: a === window.__a}); })()")


@needs_browser
def test_tab_never_leaves_the_input_and_escape_closes_from_anywhere_in_the_palette(tmp_path: Path) -> None:
    focus_a = {"eval": "window.__a = document.getElementById('jumpbtn'); window.__a.focus(); 1"}
    steps = [focus_a, CTRL_K, _wait(100), TAB, _wait(50), {"eval": IN_PALETTE}, SHIFT_TAB, _wait(50), {"eval": IN_PALETTE},
             ESC, _wait(50), {"eval": IN_PALETTE},
             # focus put on the dialog's own box (a click on the list can do this): Esc still closes
             CTRL_K, _wait(100), {"eval": "(() => { const b = document.querySelector('.palette-box'); b.tabIndex = -1; b.focus(); return 1; })()"},
             ESC, _wait(50), {"eval": IN_PALETTE}]
    tab, shift_tab, esc, esc_box = _vals(_run(tmp_path, steps))
    assert tab["open"] and tab["input"], tab
    assert shift_tab["open"] and shift_tab["input"], shift_tab
    assert not esc["open"] and esc["opener"], esc                                  # closed, and focus back where it was
    assert not esc_box["open"] and esc_box["opener"], esc_box


@needs_browser
def test_a_list_with_no_match_holds_no_non_option_and_says_so_outside_the_list(tmp_path: Path) -> None:
    typed = lambda q: {"eval": (f"(() => {{ const i = document.querySelector('.palette-input'); i.value = {json.dumps(q)}; i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1; }})()")}
    read = {"eval": ("(() => { const i = document.querySelector('.palette-input'), l = document.querySelector('.palette-list'), s = document.querySelector('.palette [role=status]');"
                     " return JSON.stringify({probe: 'none', kids: l.children.length, nonOption: l.querySelectorAll(':scope > :not([role=option])').length,"
                     " expanded: i.getAttribute('aria-expanded'), ad: i.getAttribute('aria-activedescendant'), say: s ? s.textContent : null, sayInList: !!(s && l.contains(s))}); })()")}
    steps = [{"eval": "document.getElementById('jumpbtn').click(); 1"}, read, typed("zzzqqq"), read, typed(""), read]
    some, none, some_again = _vals(_run(tmp_path, steps))
    assert some["kids"] > 0 and some["expanded"] == "true" and not some["say"], some
    assert none["kids"] == 0 and none["nonOption"] == 0 and none["expanded"] == "false" and none["ad"] is None, none
    assert none["say"] == "nothing matches" and not none["sayInList"], none
    assert some_again["kids"] > 0 and some_again["expanded"] == "true" and not some_again["say"], some_again


@needs_browser
def test_the_list_starts_at_its_top_when_the_query_changes(tmp_path: Path) -> None:
    deep = {"eval": f"(() => {{ for (let k = 0; k < 25; k++) {{ {ARROW} }} return 1; }})()"}
    same_query = {"eval": "document.querySelector('.palette-input').dispatchEvent(new Event('input', {bubbles: true})); 1"}
    read = {"eval": "JSON.stringify({probe: 'top', top: document.querySelector('.palette-list').scrollTop})"}
    steps = [{"eval": "document.getElementById('jumpbtn').click(); 1"}, deep, _wait(100), read, same_query, _wait(50), read]
    scrolled, reset = _vals(_run(tmp_path, steps))
    assert scrolled["top"] > 0, scrolled
    assert reset["top"] == 0, reset
