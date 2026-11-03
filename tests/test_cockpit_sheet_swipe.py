"""Every cockpit sheet closes by dragging it down (phone). One delegated helper in dashboard_core.js; the presence sheet and any
`.modal-overlay[role=dialog]` take it with no wiring of their own. Touch events are dispatched through the headless driver on a
430x932 phone rig; a short drag springs back, a long one calls the sheet's own close path, a scrolled body is not dismissed."""
from __future__ import annotations

import json

from tests.test_cockpit_presence import _CHIP, _CHIP_TEXT, _SHOWN, _asset, _opened, _signed, rig  # noqa: F401  (rig is a fixture)
from tests.test_cockpit_presence import T, _as_operator  # noqa: F401  (autouse fixture)
from tests.test_pair_card_w1b import _drive, needs_browser

PHONE = {"emulate": {"w": 430, "h": 932, "dpr": 2, "mobile": True}}
SHEET = "document.getElementById('pr-sheet')"
# a one-finger drag from `el` straight down by each dy in turn (one touchstart, a touchmove per dy, one touchend). The events are
# dispatched back to back, so the helper's Date.now()-based flick speed would otherwise depend on which millisecond each event
# lands in (a short drag read as a fast flick about 1 run in 40); Date.now is held to a 100 ms step per event for the drag.
DRAG = r"""window.__drag = (el, dys, x) => {
  const r = el.getBoundingClientRect(); const cx = x == null ? r.left + 20 : x; const y0 = r.top + 6;
  const mk = (type, y) => { const t = new Touch({identifier: 1, target: el, clientX: cx, clientY: y});
    return new TouchEvent(type, {touches: type === 'touchend' ? [] : [t], changedTouches: [t], bubbles: true, cancelable: true}); };
  const realNow = Date.now; let clock = realNow.call(Date); Date.now = () => clock;
  try {
    el.dispatchEvent(mk('touchstart', y0)); let prevented = 0;
    for (const dy of dys) { clock += 100; const ev = mk('touchmove', y0 + dy); el.dispatchEvent(ev); if (ev.defaultPrevented) prevented++; }
    clock += 100; el.dispatchEvent(mk('touchend', y0 + dys[dys.length - 1])); return prevented;
  } finally { Date.now = realNow; } }; 1"""


def test_the_helper_is_shared_and_the_presence_sheet_opts_in():
    core, dev = _asset("dashboard_core.js"), _asset("devices_panel.js")
    assert "levain:sheet-dismiss" in core and "touchmove" in core and "data-sheet-dismiss" in core
    assert "data-sheet-dismiss" in dev and "sheet-grabber" in dev
    assert "touchmove" not in dev                       # the gesture lives in the one helper, not per sheet


@needs_browser
def test_the_presence_sheet_is_dragged_down_to_close_and_a_short_drag_or_a_scrolled_body_does_not(rig, tmp_path, monkeypatch):
    src, q = rig
    shots = tmp_path
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            PHONE,
            {"until": _SHOWN, "ms": 20000},
            {"eval": DRAG},
            _opened(),
            {"shot": str(shots / "open.png")},
            # a short drag springs back
            {"eval": f"window.__drag({SHEET}.querySelector('.sheet-grabber'), [10, 30, 40])"},
            {"eval": f"[!{SHEET}.hidden, {SHEET}.style.transform]"},
            # a long drag from the body (scrolled to the top) closes it, with the chip's aria-expanded back to false
            {"eval": f"window.__drag({SHEET}.querySelector('.pr-sheet-word'), [20, 60, 120, 220])"},
            {"eval": f"[{SHEET}.hidden, {_CHIP}.getAttribute('aria-expanded')]"},
            # reopen; cap the sheet's height so it scrolls, scroll it, and a downward drag from the body must not dismiss
            _opened(),
            {"eval": f"{SHEET}.style.maxHeight = '300px'; {SHEET}.scrollTop = 120; [{SHEET}.scrollTop > 0, window.__drag({SHEET}.querySelector('.pr-sheet-word'), [20, 60, 120, 260])]"},
            {"eval": f"[!{SHEET}.hidden, {SHEET}.style.transform]"},
            {"shot": str(shots / "scrolled.png")},
            # back at the top of the body the same drag closes it
            {"eval": f"{SHEET}.scrollTop = 0; window.__drag({SHEET}.querySelector('.pr-sheet-word'), [20, 60, 120, 260])"},
            {"eval": f"{SHEET}.hidden"},
            # a drag UP never dismisses
            _opened(),
            {"eval": f"window.__drag({SHEET}.querySelector('.sheet-grabber'), [-20, -80, -200])"},
            {"eval": f"!{SHEET}.hidden"},
            # the X-less sheet still closes with Escape
            {"do": "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))", "until": f"{SHEET}.hidden", "ms": 4000},
        ])
    v = out["values"]
    assert v[5] == [True, ""] and v[4] == 3                     # short drag: taken (default prevented), open, transform cleared
    assert v[7] == [True, "false"]                              # long drag from the body: closed, chip collapsed
    assert v[9][0] is True and v[9][1] == 0                     # the body was scrolled: the drag was never taken over
    assert v[10] == [True, ""]                                  # still open, never moved (and a closed sheet's drag offset is gone)
    assert v[13] is True                                        # back at the top the same drag closes it
    assert v[16] is True                                        # a drag up: still open


@needs_browser
def test_a_modal_overlay_takes_the_gesture_through_its_own_x_and_keeps_its_guard(rig, tmp_path, monkeypatch):
    src, q = rig
    make = r"""(() => { window.__x = 0; const v = document.createElement('div'); v.className = 'modal-overlay'; v.id = 'swipe-test';
      v.setAttribute('role', 'dialog'); v.setAttribute('aria-modal', 'true'); const b = document.createElement('div'); b.className = 'modal';
      const h = document.createElement('div'); h.className = 'modal-head'; h.innerHTML = '<h2>T</h2>';
      const x = document.createElement('button'); x.className = 'modal-close'; x.textContent = '×';
      x.addEventListener('click', () => { window.__x++; if (window.__guard) return; v.remove(); }); h.appendChild(x);
      b.appendChild(h); v.appendChild(b); document.body.appendChild(v); return 1; })()"""
    with _signed(src, q) as (port, _):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            PHONE, {"until": _SHOWN, "ms": 20000}, {"eval": DRAG}, {"eval": make},
            {"eval": "window.__guard = true; window.__drag(document.querySelector('#swipe-test .modal-head h2'), [60, 160, 320])"},
            {"until": "window.__x === 1", "ms": 3000},
            {"until": "document.querySelector('#swipe-test .modal').style.transform === ''", "ms": 3000},   # guarded: the sheet is given back
            {"eval": "window.__guard = false; window.__drag(document.querySelector('#swipe-test .modal-head h2'), [60, 160, 320])"},
            {"until": "!document.getElementById('swipe-test')", "ms": 3000},
            {"eval": "window.__x"},
        ])
    assert out["values"][-1] == 2        # each dismiss went through the X's own handler, once
