"""Calm: one page control sets ``html.calm`` and the page reads as if the OS asked for reduced motion.

The class is the one predicate: dashboard.css, topology_view.js's particle loop, sky.js's ambient meteors and the age timer all test it. The
choice is kept per browser (absent storage = full). The page tests run the real cockpit in headless Chrome (the agents graph with live edges,
as test_cockpit_k10_agents does); the age-timer test uses the page's fake DOM."""
from __future__ import annotations

import json

from tests._ke_dom import NODE, run_node
from tests.test_cockpit_f12b_topology_view import TV, _paint, _run
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k10_agents import PRIME, _graph_value, _probe
from tests.test_cockpit_k7_build import _sessions_fixture

import pytest

WAIT = {"eval": "new Promise(r => setTimeout(r, 300))"}
HTML = "document.documentElement.classList.contains('calm')"
BTN = "(() => { const b = document.getElementById('calmbtn'); return JSON.stringify([b.getAttribute('aria-pressed'), b.textContent]); })()"
CLICK = {"eval": "document.getElementById('calmbtn').click(); 1"}
# a probe element that asks for an infinite animation: under Calm the page's one rule must win over it
ANIM = ("(() => { const d = document.createElement('div'); d.style.animation = 'spin 1s linear infinite'; document.body.appendChild(d); "
        "const n = getComputedStyle(d).animationName; d.remove(); return n; })()")


@needs_browser
def test_absent_storage_is_full_motion_with_the_control_off(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": HTML}, {"eval": BTN}, {"eval": ANIM}, {"eval": _probe()}])
    assert out[-4] is False and json.loads(out[-3]) == ["false", "Calm off"], out
    assert out[-2] == "spin", out
    assert json.loads(out[-1])["stage"]["motion"] == "on", out


@needs_browser
def test_the_control_stills_the_graph_the_sky_rule_and_every_animation_at_once(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": _probe()},
                               CLICK, WAIT, {"eval": HTML}, {"eval": BTN}, {"eval": ANIM}, {"eval": _probe()}])
    before, after = json.loads(out[-7])["stage"], json.loads(out[-1])["stage"]
    assert before["motion"] == "on" and int(before["particles"]) > 0, before
    assert out[-4] is True and json.loads(out[-3]) == ["true", "Calm on"], out
    assert out[-2] == "none", out                                  # the CSS reader
    assert after["motion"] == "off" and after["particles"] == "0" and after["streams"] == "2", after   # the graph reader: running loop stops, edges still count


@needs_browser
def test_turning_calm_off_again_resumes_the_stream(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, CLICK, WAIT, CLICK, WAIT, {"eval": HTML}, {"eval": _probe()}])
    got = json.loads(out[-1])["stage"]
    assert out[-2] is False and got["motion"] == "on" and int(got["particles"]) > 0, out


@needs_browser
def test_the_choice_is_remembered_across_a_reload_and_a_page_painted_under_it_draws_no_stream(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, {"eval": "localStorage.getItem('levain_cockpit_calm')"},
                               {"eval": "location.reload(); 1"}, {"until": HTML + " && document.getElementById('calmbtn') !== null"},
                               *PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": BTN}, {"eval": _probe()}])
    assert out[2] == "1", out
    assert json.loads(out[-2]) == ["true", "Calm on"], out
    assert json.loads(out[-1])["stage"]["motion"] == "off", out


@needs_browser
def test_a_graph_drawn_under_calm_starts_flowing_when_the_control_turns_it_off(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, {"eval": "location.reload(); 1"}, {"until": HTML + " && document.getElementById('calmbtn') !== null"},
                               *PRIME, {"eval": _paint(_graph_value(), "agents")}, WAIT, {"eval": _probe()}, CLICK, WAIT, {"eval": _probe()}])
    still, flowing = json.loads(out[-4])["stage"], json.loads(out[-1])["stage"]
    assert still["motion"] == "off" and still["particles"] == "0", still
    assert flowing["motion"] == "on" and int(flowing["particles"]) > 0, flowing


@needs_browser
def test_calm_off_removes_the_stored_choice(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [CLICK, CLICK, {"eval": "String(localStorage.getItem('levain_cockpit_calm'))"}, {"eval": HTML}])
    assert out[-2] == "null" and out[-1] is False, out


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_age_timer_slows_under_calm_and_is_never_stopped(tmp_path):
    out = run_node(r"""
const timers = [], cleared = [];
globalThis.setInterval = (fn, ms) => { timers.push(ms); return timers.length; };
globalThis.clearInterval = (id) => cleared.push(id);
document.documentElement = new N("html");
N.prototype.isConnected = true;
const regions = { hero: [], brief: [], widgets: ["health"], header: [], zones: [{ id: "operate", title: "Work", grid: 3, panels: [], cards: [] }] };
const panel = { metrics: [{ label: "write path", value: "live", status: "ok" }, { label: "last save at", value: "2026-10-12T11:55:00+00:00", status: "unknown" }] };
const m = fixture({ health: head("health", "status-dots", "Health") }, { health: head("health", "status-dots", "Health", { value: panel }) }, { regions });
K.fill(new N("div"), V.fromManifest(m).extra_panels.health, { entry: { title: "health" } });
out.full = timers.slice(); out.calm0 = K.isCalm();
K.setCalm(true, false);
out.calm1 = K.isCalm(); out.after = timers.slice(); out.armed = K.ageTimerArmed(); out.cleared = cleared.length;
out.tick = [K.AGE_TICK_MS, K.CALM_AGE_TICK_MS];
""", tmp_path)
    assert out["full"] == [out["tick"][0]] and out["calm0"] is False
    assert out["calm1"] is True and out["after"] == [out["tick"][0], out["tick"][1]] and out["armed"] is True and out["cleared"] == 1
    assert out["tick"][1] > out["tick"][0]


@needs_browser
@pytest.mark.parametrize("width", [320, 375, 430])
def test_on_a_phone_calm_is_an_icon_in_the_first_row_beside_search_the_same_size(tmp_path, monkeypatch, width):
    """C5: no third row. Calm is icon-only (its name is its aria-label and title), the size of the search icon, on the first
    row with Sync, search and the theme switch: every one inside the viewport and none overlapping another."""
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = """(() => { const q = (s) => document.querySelector(s), r = (e) => e.getBoundingClientRect();
      const ids = ['#refresh', '#calmbtn', '#jumpbtn', '.masthead .themesw'].map((s) => r(q(s)));
      const c = q('#calmbtn'), j = r(q('#jumpbtn')), cr = r(c), tabs = r(q('.masthead .seg'));
      let overlap = false; for (let a = 0; a < ids.length; a++) for (let b = a + 1; b < ids.length; b++)
        if (ids[a].left < ids[b].right - 0.5 && ids[b].left < ids[a].right - 0.5 && ids[a].top < ids[b].bottom - 0.5 && ids[b].top < ids[a].bottom - 0.5) overlap = true;
      return JSON.stringify({ sameRow: ids.every((b) => Math.abs(b.top - ids[0].top) < 30), belowTabs: cr.top >= tabs.top,
        sameSize: Math.abs(cr.width - j.width) < 1 && Math.abs(cr.height - j.height) < 1, inside: ids.every((b) => b.left >= 0 && b.right <= innerWidth),
        overlap, textHidden: getComputedStyle(c).fontSize === '0px', label: c.getAttribute('aria-label'), title: c.title, pressed: c.getAttribute('aria-pressed'),
        sw: document.documentElement.scrollWidth, vw: innerWidth }); })()"""
    out = _run(tmp_path, src, [{"eval": "new Promise(r => setTimeout(r, 300))"}, {"eval": probe}, CLICK, {"eval": probe}],
               first={"emulate": {"w": width, "h": 800, "dpr": 2, "mobile": True}})
    off, on = json.loads(out[-3]), json.loads(out[-1])
    for got, name, pressed in ((off, "Calm off", "false"), (on, "Calm on", "true")):
        assert got["sameRow"] and not got["belowTabs"] and got["sameSize"] and got["inside"] and not got["overlap"], got
        assert got["textHidden"] and got["label"] == name and got["title"] == name and got["pressed"] == pressed, got
        assert got["sw"] <= got["vw"], got
