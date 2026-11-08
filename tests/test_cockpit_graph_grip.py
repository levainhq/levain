"""Row ui480: the Agents graph's box is user-adjustable. A full-width grip under .tv-stage (pointer drag, Arrow keys, double-click resets), the height kept
per device in localStorage, clamped to [12rem, 90vh], no grip on a phone, and the particle canvas stays on the SVG after a resize.
Real headless Chrome through the topology renderer; `LEVAIN_GRIP_SHOTS=<dir>` keeps before/after PNGs for the desk to look at."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _run
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_graphbox_fixed import _many
from tests.test_cockpit_k10_agents import PRIME
from tests.test_cockpit_k7_build import DESKTOP, _sessions_fixture

KEY = "levain_cockpit_graph_height"
ST = TV + ".querySelector('.tv-stage')"
GRIP = TV + ".querySelector('.tv-grip')"
WAIT = {"eval": "new Promise(r => setTimeout(r, 600)).then(() => 1)"}
STATE = ("JSON.stringify({kind: 'st', box: %s.getBoundingClientRect().height, h: %s.style.height, mh: %s.style.maxHeight, vh: window.innerHeight, "
         "rem: parseFloat(getComputedStyle(document.documentElement).fontSize), stored: localStorage.getItem('%s'), "
         "grip: !!%s && getComputedStyle(%s).display !== 'none', phone: %s.classList.contains('tv-phone'), "
         "cv: (() => { const c = %s.querySelector('.tv-particles'), s = %s.querySelector('svg.tv-graph'); "
         "if (!c || !s) return null; const a = c.getBoundingClientRect(), b = s.getBoundingClientRect(); "
         "return {dl: a.left - b.left, dt: a.top - b.top, dw: a.width - b.width, dh: a.height - b.height}; })()})") % (ST, ST, ST, KEY, GRIP, GRIP, ST, ST, ST)


def _drag(dy: int) -> str:
    return ("(() => { const g = %s, r = g.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2; "
            "const e = (t, yy) => g.dispatchEvent(new PointerEvent(t, {bubbles: true, pointerId: 7, button: 0, clientX: x, clientY: yy})); "
            "e('pointerdown', y); e('pointermove', y + %d / 2); e('pointermove', y + %d); e('pointerup', y + %d); return 1; })()") % (GRIP, dy, dy, dy)


def _shot(name: str) -> list:
    d = os.environ.get("LEVAIN_GRIP_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    return [{"eval": "document.querySelector('#ck-hlens').scrollIntoView({block: 'start'}); 1"},
            {"eval": "new Promise(r => setTimeout(r, 400)).then(() => 1)"}, {"shot": str(Path(d) / f"{name}.png"), "full": False}]


def _states(out) -> list:
    return [json.loads(v) for v in out if isinstance(v, str) and v.startswith('{"kind":"st"')]


def _go(tmp_path, monkeypatch, steps, first=DESKTOP):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    return _run(tmp_path, src, [*PRIME, {"eval": "localStorage.removeItem('%s'); 1" % KEY}, {"eval": _paint(_many(80), "agents")}, WAIT, *steps], first=first)


def _mark() -> dict:
    return {"eval": STATE}


@needs_browser
def test_a_drag_sets_the_height_and_the_canvas_stays_on_the_svg(tmp_path, monkeypatch):
    out = _go(tmp_path, monkeypatch, [_mark(), *_shot("before"), {"eval": _drag(-150)}, WAIT, _mark(), *_shot("shrunk"),
                                      {"eval": _drag(+300)}, WAIT, _mark(), *_shot("grown")])
    d, a, b = _states(out)
    assert d["grip"] and d["h"] == "" and d["stored"] is None, d                       # the default: CSS max-height, no stored height
    assert abs(a["box"] - (d["box"] - 150)) <= 2 and a["mh"] == "none" and a["stored"] == str(round(a["box"])), (d, a)
    assert b["box"] > a["box"] + 100 and b["stored"] == str(round(b["box"])), (a, b)
    for s in (d, a, b):                                                                # the particle canvas sits on the SVG, whatever the box's height
        assert s["cv"] and all(abs(v) <= 2 for v in s["cv"].values()), s


@needs_browser
def test_the_height_is_clamped_to_a_minimum_and_ninety_vh(tmp_path, monkeypatch):
    out = _go(tmp_path, monkeypatch, [_mark(), {"eval": _drag(-3000)}, _mark(), {"eval": _drag(+5000)}, _mark()])
    d, lo, hi = _states(out)
    assert abs(lo["box"] - 12 * lo["rem"]) <= 2, lo
    assert abs(hi["box"] - 0.9 * hi["vh"]) <= 2, hi


@needs_browser
def test_a_stored_height_survives_a_repaint_and_is_reclamped_on_load_and_resize(tmp_path, monkeypatch):
    out = _go(tmp_path, monkeypatch, [{"eval": _drag(-120)}, _mark(), {"eval": _paint(_many(80), "agents")}, WAIT, _mark(),
                                      {"eval": "localStorage.setItem('%s', '9000'); 1" % KEY}, {"eval": _paint(_many(80), "agents")}, WAIT, _mark(),
                                      {"eval": "localStorage.setItem('%s', '9000'); 1" % KEY}, {"eval": "window.dispatchEvent(new Event('resize')); 1"}, _mark()])
    first, again, load, resized = _states(out)
    assert again["stored"] == first["stored"] and abs(again["box"] - first["box"]) <= 2, (first, again)    # a fresh stage reads the kept height
    assert abs(load["box"] - 0.9 * load["vh"]) <= 2, load                                                   # a stored 9000 clamps on load
    assert abs(resized["box"] - 0.9 * resized["vh"]) <= 2, resized                                          # and on a window resize


@needs_browser
def test_double_click_resets_to_the_default(tmp_path, monkeypatch):
    out = _go(tmp_path, monkeypatch, [_mark(), {"eval": _drag(-150)}, _mark(),
                                      {"eval": "%s.dispatchEvent(new MouseEvent('dblclick', {bubbles: true})); 1" % GRIP}, _mark()])
    d, a, r = _states(out)
    assert a["stored"] is not None and r["stored"] is None and r["h"] == "" and r["mh"] == "", (a, r)
    assert abs(r["box"] - d["box"]) <= 2, (d, r)


@needs_browser
def test_arrow_keys_on_the_focused_grip_resize_it(tmp_path, monkeypatch):
    key = "(k) => %s.dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true, cancelable: true}))" % GRIP
    out = _go(tmp_path, monkeypatch, [_mark(), {"eval": "(%s)('ArrowUp'); 1" % key}, _mark(), {"eval": "(%s)('ArrowDown'); (%s)('ArrowDown'); 1" % (key, key)}, _mark()])
    d, up, down = _states(out)
    assert up["box"] < d["box"] - 10 and down["box"] > up["box"] + 10, (d, up, down)


@needs_browser
def test_a_phone_has_no_grip_and_ignores_a_stored_height(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": "localStorage.setItem('%s', '300'); 1" % KEY}, {"eval": _paint(_many(80), "agents")}, WAIT, _mark()], first=NARROW)
    (m,) = _states(out)
    assert m["phone"] and not m["grip"] and m["h"] == "", m


# --- L3 r1 + desk cosmetic: centred after a resize, a click pins nothing, one resize listener ---------------------------------------------

CENTRE = ("JSON.stringify({kind: 'st', off: (() => { const s = %s, g = s.querySelector('svg.tv-graph'), a = s.getBoundingClientRect(), b = g.getBoundingClientRect(); "
          "return (b.top + b.height / 2) - (a.top + s.clientTop + s.clientHeight / 2); })(), box: %s.clientHeight, scroll: %s.scrollTop})") % (ST, ST, ST)


@needs_browser
def test_a_shrunk_box_keeps_the_graph_centred(tmp_path, monkeypatch):
    """Desk cosmetic: shrinking with the grip must not leave the content scrolled off its middle (a node cut at the bottom); a stored height centres on load."""
    out = _go(tmp_path, monkeypatch, [{"eval": _drag(-200)}, WAIT, {"eval": CENTRE},
                                      {"eval": "localStorage.setItem('%s', '300'); 1" % KEY}, {"eval": _paint(_many(80), "agents")}, WAIT, {"eval": CENTRE}])
    shrunk, loaded = _states(out)
    assert abs(shrunk["off"]) <= 6 and abs(loaded["off"]) <= 6, (shrunk, loaded)


@needs_browser
def test_a_small_graph_in_a_tall_box_is_centred(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": "localStorage.setItem('%s', '700'); 1" % KEY}, {"eval": _paint(_many(1), "agents")}, WAIT, {"eval": CENTRE}], first=DESKTOP)
    (c,) = _states(out)
    assert abs(c["off"]) <= 6, c


@needs_browser
def test_a_click_without_movement_pins_no_height(tmp_path, monkeypatch):
    """L3 r1 (4 seats, MED): a plain click on the grip stored the current height and fixed the box until a double-click."""
    click = ("(() => { const g = %s, r = g.getBoundingClientRect(), x = r.left + 5, y = r.top + 5; "
             "const e = (t) => g.dispatchEvent(new PointerEvent(t, {bubbles: true, pointerId: 7, button: 0, clientX: x, clientY: y})); e('pointerdown'); e('pointerup'); return 1; })()") % GRIP
    out = _go(tmp_path, monkeypatch, [{"eval": click}, _mark()])
    (m,) = _states(out)
    assert m["stored"] is None and m["h"] == "" and m["mh"] == "", m


@needs_browser
def test_repaints_do_not_stack_resize_listeners(tmp_path, monkeypatch):
    """L3 r1 (4 seats, HIGH/MED): each Agents repaint added a window resize listener only a resize removed."""
    count = ("(() => { let n = 0; const add = window.addEventListener, rm = window.removeEventListener; "
             "window.__rs = 0; window.addEventListener = function (t, ...a) { if (t === 'resize') window.__rs++; return add.call(this, t, ...a); }; "
             "window.removeEventListener = function (t, ...a) { if (t === 'resize') window.__rs--; return rm.call(this, t, ...a); }; return 1; })()")
    out = _go(tmp_path, monkeypatch, [{"eval": count}, {"eval": _paint(_many(80), "agents")}, {"eval": _paint(_many(80), "agents")}, {"eval": _paint(_many(80), "agents")},
                                      WAIT, {"eval": "JSON.stringify({kind: 'st', n: window.__rs})"}])
    (m,) = _states(out)
    assert m["n"] <= 1, m
