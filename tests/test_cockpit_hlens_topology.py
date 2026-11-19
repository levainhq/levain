"""The page's header lens (#ck-hlens, drawn by LevainTopologyView.render): three behaviours the retired Build Sessions card's tests also
guarded on the shared topology view, re-homed here so they are graded where the page still draws it (levain#148). Each test drives
#ck-hlens in the real page and fails without the behaviour: the fault line, one live ResizeObserver, the fallback when render throws."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import MID, TV, _intention, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import _sessions_fixture

# a faulted or throwing topology draws no .tv: the lens's own error line (or its switch bar) is what such a test waits for
LINE = "document.querySelector('#ck-hlens .k7-none.err')"
SWITCH = "document.querySelector('#ck-hlens [data-tv]') && 1"


@needs_browser
def test_a_topology_provider_faulted_at_load_is_said_in_the_header_lens_in_either_lens(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    import levain.cockpit.topology as topo
    from levain.cockpit.results import Fault
    monkeypatch.setattr(topo, "topology_reader", lambda **kw: (lambda ctx: Fault("the session records are gone")))
    probe = "JSON.stringify({line: [...document.querySelectorAll('#ck-hlens .k7-none.err')].map(n => n.textContent), tv: !!%s, switch: document.querySelectorAll('#ck-hlens [data-tv]').length})" % TV
    pick = lambda lens: {"eval": "localStorage.setItem('levain_cockpit_topology_mode', '%s'); 1" % lens}   # noqa: E731
    for lens in ("agents", "work"):
        # the stored lens is read when the page paints: set it, reload nothing, let the page's own first paint draw the fault with the lens in force
        out = _run(tmp_path, src, [pick(lens), {"eval": "new Promise(r => setTimeout(r, 600))"}, {"eval": probe}], first=MID, until=LINE)
        got = json.loads(out[-1])
        assert any("Topology unavailable: " in t and "the session records are gone" in t for t in got["line"]), (lens, got)
        assert got["tv"] is False and got["switch"] >= 1, (lens, got)             # no graph is drawn, and the switch is still there: the Event graph is one click away


@needs_browser
def test_a_second_render_into_the_header_lens_leaves_one_live_resize_observer_not_two(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    js = ("(() => { const Real = window.ResizeObserver; const made = [];"
          "window.ResizeObserver = class { constructor(cb) { this.r = new Real(cb); this.live = false; made.push(this); }"
          "  observe(n) { this.live = true; this.r.observe(n); } disconnect() { this.live = false; this.r.disconnect(); } };"
          "const h = document.getElementById('ck-hlens'); const v = %s; const out = {};"
          "for (const lens of ['work', 'agents']) { made.length = 0;"
          "  for (let i = 0; i < 3; i++) { h.replaceChildren(window.LevainTopologyView.render(v, {lens})); }"
          "  out[lens] = {made: made.length, live: made.filter(m => m.live).length}; }"
          "window.ResizeObserver = Real; return JSON.stringify(out); })()") % json.dumps(_value())
    out = _run(tmp_path, src, [{"eval": js}])
    got = json.loads(out[-1])
    for lens, g in got.items():
        assert g["made"] >= 1 and g["live"] == 1, (lens, got)


@needs_browser
def test_a_renderer_that_throws_leaves_the_header_lens_its_switch_and_one_line_not_a_dead_lens(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _intention(root)
    probe = ("JSON.stringify({tv: !!%s, switch: document.querySelectorAll('#ck-hlens [data-tv]').length, "
             "text: document.querySelector('#ck-hlens').textContent.replace(/\\s+/g, ' ')})") % TV
    out = _run(tmp_path, src, [{"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                               {"until": TV + ".querySelector('.tv-task')"},
                               {"eval": "window.LevainTopologyView.render = () => { throw new Error('boom'); }; document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"},
                               {"until": LINE}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["tv"] is False and got["switch"] >= 1 and "topology could not be drawn" in got["text"], got
