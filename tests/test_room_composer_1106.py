"""1106-16: the room is a plain chat. Enter sends on a desk (Shift+Enter is a new line, an input method's Enter is its own, a touch
screen keeps Return as a new line), and the room's own markup has no direct Drop control. Behaviour is driven in headless Chrome;
how it looks is the desk's look at the running page."""
from __future__ import annotations

import json

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_cockpit_room import _assets, _boot, _serve_room, w, _UNTIL_READY  # noqa: F401  (fixture)

_KEY = ("const key = (o) => { const e = new KeyboardEvent('keydown', Object.assign({key: 'Enter', bubbles: true, cancelable: true}, o));"
        " ta.dispatchEvent(e); return e.defaultPrevented; };")


@needs_browser
def test_enter_sends_shift_enter_and_composition_and_touch_do_not(tmp_path, w):
    probe = ("(() => { const ta = document.querySelector('.rv-input'); " + _KEY +
             " ta.value = 'one line'; ta.dispatchEvent(new Event('input', {bubbles: true}));"
             " const out = {shift: key({shiftKey: true}), composing: key({isComposing: true}), ime229: key({keyCode: 229})};"
             " const real = window.matchMedia; window.matchMedia = q => ({matches: /coarse/.test(q)}); out.touch = key({}); window.matchMedia = real;"
             " out.empty = (ta.value = '   ', ta.dispatchEvent(new Event('input', {bubbles: true})), document.querySelector('.rv-send').getAttribute('aria-disabled'));"
             " ta.value = 'one line'; ta.dispatchEvent(new Event('input', {bubbles: true})); out.desk = key({}); return JSON.stringify(out); })()")
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"eval": probe},
            {"until": "document.querySelector('.rv-input').value === '' && document.querySelector('.rv-sendnote').textContent === 'Sent.'"},
        ])
    got = json.loads(out["values"][1])
    # nothing was sent by the first four keys: they were not prevented, and the whole run sent the line exactly once
    assert got == {"shift": False, "composing": False, "ime229": False, "touch": False, "empty": "true", "desk": True}, got
    assert [r[3] for r in w.lines()] == ["one line"]


@needs_browser
def test_the_room_draws_no_direct_drop_control(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot())) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            _UNTIL_READY,
            {"eval": "JSON.stringify({drop: !!document.querySelector('.rv-drop'), "
                     "words: [...document.querySelectorAll('.rv button')].map(b => b.textContent.trim().toLowerCase()).filter(t => t === 'drop'), "
                     "send: !!document.querySelector('.rv-compose .rv-send')})"},
        ])
    assert json.loads(out["values"][1]) == {"drop": False, "words": [], "send": True}
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "room_view.js").read_text()
    assert 'post("drop"' not in js
