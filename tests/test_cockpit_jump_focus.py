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
         " inPanel: !!p && p.contains(a), active: a.tagName, shown: !!p && p.getClientRects().length > 0}); })()")


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
