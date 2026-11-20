"""levain#151 polish, the jump box's focus: Cmd-K from Settings to Recent episodes ended with focus on <body> about one run in fifteen.

The cause (isolated on 1119-18 by logging focus() and a MutationObserver on the live page, 80 trials): the poll redraws the whole board
(drawView -> render -> board.replaceChildren), which replaces every panel node. A redraw can land (1) between the jump box listing the
panel and the choice, so the node the jump holds is already detached when the frame comes and focus() on it does nothing, or (2) just
after focus() took, when the focused node is removed and focus falls to <body>. Both are timing, so they are made deterministic here by
forcing the redraw (LevainCockpitTransport.reload) at each of the two moments. The fix looks the panel up again by its id and, for a
bounded 3 s, hands the focus to the successor when a redraw drops it to <body>."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_places import PRESS, READY, SETTLE


def _wait(ms: int) -> dict:
    return {"eval": f"new Promise(r => setTimeout(r, {ms})).then(() => 1)"}


# open the jump box from Settings and type; keeps the node it lists so a later probe can say whether a redraw replaced it
OPEN = ("(() => { window.__old = document.querySelector('#board [data-ck-panel=\"episodes\"]'); document.getElementById('jumpbtn').click();"
        " const i = document.querySelector('.palette-input'); i.value = 'recent episodes'; i.dispatchEvent(new Event('input')); return 1; })()")
ENTER = "document.querySelector('.palette-input').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); 1"
REDRAW = "window.LevainCockpitTransport.reload().then(() => 1)"
PROBE = ("(() => { const p = document.querySelector('#board [data-ck-panel=\"episodes\"]'); const a = document.activeElement;"
         " return JSON.stringify({probe: 'focus', place: document.body.dataset.iaPlace, replaced: !!p && p !== window.__old, oldConnected: window.__old.isConnected,"
         " inPanel: !!p && p.contains(a), active: a ? a.tagName : null, shown: !!p && p.getClientRects().length > 0}); })()")


def _probes(out: dict) -> list[dict]:
    got = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith('{"probe"')]
    return [g for g in got if g["probe"] == "focus"]


def _run(tmp_path: Path, tail: list[dict]) -> list[dict]:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            {"emulate": {"w": 1440, "h": 900, "dpr": 1}}, READY, SETTLE,
            {"eval": PRESS % "settings"}, SETTLE, {"eval": OPEN}, _wait(100), *tail])
    return _probes(out)


@needs_browser
def test_focus_lands_on_the_panel_when_a_redraw_replaces_it_after_the_jump_took_focus(tmp_path: Path) -> None:
    """Mode (2): choose from Settings, focus is on the panel, then the poll redraws the board: the successor panel holds the focus."""
    steps = [{"eval": ENTER}, _wait(300), {"eval": PROBE}, {"eval": REDRAW}, _wait(500), {"eval": PROBE}]
    before, after = _run(tmp_path, steps)
    assert before["place"] == "memory" and before["inPanel"] and before["shown"], before
    assert after["replaced"] and not after["oldConnected"], after                 # the redraw did replace the node (the test exercises the mechanism)
    assert after["inPanel"] and after["active"] != "BODY", after                  # and the focus is on the new one, not on <body>


@needs_browser
def test_focus_lands_on_the_panel_when_a_redraw_replaced_it_between_listing_and_choosing(tmp_path: Path) -> None:
    """Mode (1): the jump box listed the panel, a redraw replaced the node, then the choice: focus goes to the panel that is on the page."""
    steps = [{"eval": REDRAW}, _wait(500), {"eval": ENTER}, _wait(400), {"eval": PROBE}]
    (got,) = _run(tmp_path, steps)
    assert got["replaced"] and not got["oldConnected"], got
    assert got["place"] == "memory" and got["shown"] and got["inPanel"] and got["active"] != "BODY", got


# jump to the first panel on the page now (Memory) other than Recent episodes (a second palette pick), remembering its id in window.__b
OPEN_OTHER = ("(() => { document.getElementById('jumpbtn').click(); const i = document.querySelector('.palette-input');"
              " for (const h of document.querySelectorAll('#board .panel > .phead h2')) {"
              "   const id = h.closest('.panel').dataset.ckPanel; if (!id || id === 'episodes' || !h.closest('.panel').getClientRects().length) continue;"
              "   i.value = h.textContent; i.dispatchEvent(new Event('input'));"
              "   const li = document.querySelector('.palette-list li'); if (li && li.querySelector('.pk').textContent === 'panel' && li.querySelector('.pl').textContent === h.textContent) { window.__b = id; return id; } }"
              " return 'none'; })()")
# where focus is, by panel id and as a tag: for the second jump's panel and for the page
PROBE_B = ("JSON.stringify({probe: 'focus', b: window.__b, inB: (() => { const p = document.querySelector('#board [data-ck-panel=\"' + window.__b + '\"]');"
           " return !!p && p.contains(document.activeElement); })(), inA: (() => { const p = document.querySelector('#board [data-ck-panel=\"episodes\"]');"
           " return !!p && p.contains(document.activeElement); })(), active: document.activeElement ? document.activeElement.tagName : null})")
# the reader answers where focus should be: a real press on blank page (the click also blurs what held it), or a key
PRESS_BLANK = ("document.activeElement && document.activeElement.blur(); document.body.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true})); 1")
KEY_AWAY = ("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', bubbles: true})); document.activeElement && document.activeElement.blur(); 1")
BLUR = "document.activeElement && document.activeElement.blur(); 1"


@needs_browser
def test_a_second_jump_inside_the_window_owns_the_hand_off(tmp_path: Path) -> None:
    """Jump to A, then to B within the bound, then a redraw: focus ends on B's successor, not on A's (A's hand-off is cancelled by B's)."""
    steps = [{"eval": ENTER}, _wait(100), {"eval": OPEN_OTHER}, _wait(100), {"eval": ENTER}, _wait(400), {"eval": PROBE_B}, {"eval": REDRAW}, _wait(500), {"eval": PROBE_B}]
    before, after = _run(tmp_path, steps)
    assert before["b"] not in (None, "none") and before["inB"], before            # the second jump took focus on B
    assert after["inB"] and not after["inA"] and after["active"] != "BODY", after


@needs_browser
def test_a_press_on_blank_page_after_a_jump_is_not_undone_by_a_redraw(tmp_path: Path) -> None:
    """Jump, then the reader clicks blank page (focus to <body>), then a redraw: focus stays on <body>."""
    steps = [{"eval": ENTER}, _wait(300), {"eval": PRESS_BLANK}, {"eval": REDRAW}, _wait(500), {"eval": PROBE}]
    (got,) = _run(tmp_path, steps)
    assert got["replaced"] and got["active"] == "BODY" and not got["inPanel"], got


@needs_browser
def test_a_key_after_a_jump_is_not_undone_by_a_redraw(tmp_path: Path) -> None:
    """Jump, then the reader presses a key and focus leaves the panel, then a redraw: focus stays where the reader put it (<body>)."""
    steps = [{"eval": ENTER}, _wait(300), {"eval": KEY_AWAY}, {"eval": REDRAW}, _wait(500), {"eval": PROBE}]
    (got,) = _run(tmp_path, steps)
    assert got["replaced"] and got["active"] == "BODY" and not got["inPanel"], got


@needs_browser
def test_the_hand_off_happens_once(tmp_path: Path) -> None:
    """Jump, a redraw hands focus to the successor; then focus leaves for <body> with no press or key and a second redraw: not handed again."""
    steps = [{"eval": ENTER}, _wait(300), {"eval": REDRAW}, _wait(500), {"eval": PROBE}, {"eval": BLUR}, {"eval": REDRAW}, _wait(500), {"eval": PROBE}]
    first, second = _run(tmp_path, steps)
    assert first["inPanel"] and first["active"] != "BODY", first                  # the first redraw was handed over
    assert second["active"] == "BODY" and not second["inPanel"], second          # the second was not
