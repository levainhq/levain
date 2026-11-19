"""The page's header lens (#ck-hlens, drawn by LevainTopologyView.render): three behaviours the retired Build Sessions card's tests also
guarded on the shared topology view, re-homed here so they are graded where the page still draws it (levain#148). Each test drives
#ck-hlens in the real page and fails without the behaviour: the fault line, one live ResizeObserver, the fallback when render throws."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import MID, TV, _intention, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import _sessions_fixture

# a faulted or throwing topology draws no .tv: the lens's own error line is what such a test waits for
LINE = "document.querySelector('#ck-hlens .k7-none.err')"


@needs_browser
def test_a_topology_provider_faulted_at_load_is_said_in_the_header_lens_in_either_lens(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    import levain.cockpit.topology as topo
    from levain.cockpit.results import Fault
    monkeypatch.setattr(topo, "topology_reader", lambda **kw: (lambda ctx: Fault("the session records are gone")))
    probe = ("JSON.stringify({line: [...document.querySelectorAll('#ck-hlens .k7-none.err')].map(n => n.textContent), tv: !!%s, "
             "pressed: [...document.querySelectorAll('#ck-hlens [data-tv][aria-pressed=true]')].map(b => b.dataset.tv), "
             "switch: document.querySelectorAll('#ck-hlens [data-tv]').length})") % TV
    for lens in ("agents", "work"):
        # the stored lens is read when the page paints: store it, reload, and wait for the fault line again, so the paint under test is the one
        # drawn with the lens in force (a lens that is not the pressed one means the test would be grading the other lens)
        steps = [{"eval": "localStorage.setItem('levain_cockpit_header_lens', '%s'); setTimeout(() => location.reload(), 0); 1" % lens},
                 {"eval": "new Promise(r => setTimeout(r, 1500))"}, {"until": LINE}, {"eval": probe}]
        out = _run(tmp_path, src, steps, first=MID, until=LINE)
        got = json.loads(out[-1])
        assert got["pressed"] == [lens], (lens, got)
        assert any("Topology unavailable: " in t and "the session records are gone" in t for t in got["line"]), (lens, got)
        assert got["tv"] is False and got["switch"] >= 1, (lens, got)             # no graph is drawn, and the switch is still there: the Event graph is one click away


@needs_browser
def test_a_second_render_into_the_header_lens_leaves_one_live_resize_observer_not_two(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    js = ("(() => { const Real = window.ResizeObserver; const made = [];"
          "window.ResizeObserver = class { constructor(cb) { this.r = new Real(cb); this.live = false; made.push(this); }"
          "  observe(n) { this.live = true; this.r.observe(n); } unobserve(n) { this.r.unobserve(n); }"
          "  disconnect() { this.live = false; this.r.disconnect(); } };"
          "try { const h = document.getElementById('ck-hlens'); const v = %s;"
          "  for (const lens of ['work', 'agents', 'work']) { h.replaceChildren(window.LevainTopologyView.render(v, {lens})); }"
          "  return JSON.stringify({made: made.length, live: made.filter(m => m.live).length}); }"
          " finally { window.ResizeObserver = Real; } })()") % json.dumps(_value())
    out = _run(tmp_path, src, [{"eval": js}])
    got = json.loads(out[-1])
    assert got["made"] >= 3 and got["live"] == 1, got


@needs_browser
def test_a_renderer_that_throws_leaves_the_header_lens_its_switch_and_one_line_not_a_dead_lens(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _intention(root)
    probe = ("JSON.stringify({tv: !!%s, switch: document.querySelectorAll('#ck-hlens [data-tv]').length, "
             "text: document.querySelector('#ck-hlens').textContent.replace(/\\s+/g, ' ')})") % TV
    out = _run(tmp_path, src, [{"eval": "window.__orig = window.LevainTopologyView.render; document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                               {"until": TV + ".querySelector('.tv-task')"},
                               {"eval": "window.LevainTopologyView.render = () => { throw new Error('boom'); }; document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"},
                               {"until": LINE}, {"eval": probe},
                               {"eval": "window.LevainTopologyView.render = window.__orig; 1"}])
    got = json.loads(out[-2])
    assert got["tv"] is False and got["switch"] >= 1 and "topology could not be drawn" in got["text"], got
