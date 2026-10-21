"""F12b L3 r1 fixes (K10 S2). Each test was written against the unfixed renderer and failed there first: the finding it closes is
named in its docstring. A literal value is handed to the renderer in the real page, the way test_cockpit_f12b_topology_view.py does."""
from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from levain.cockpit import Cockpit, ProviderSpec, Read
from levain.cockpit.results import Fault
from tests.test_cockpit_f12b_topology_view import MID, NARROW, TV, _paint, _run, _spore, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import DESKTOP, _sessions_fixture
from tests.test_cockpit_topology import _good, _panel


def _lost(**by_node: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"node": n, "dirty": 0, "dirty_names": [], "unpushed": 0, "branches": [], "uncaptured_episodes": None, "running_children": 0, **o}
            for n, o in by_node.items()]


# --- codex HIGH: neither lens reads lost_if_closed -----------------------------------------------------------


@needs_browser
def test_a_node_with_work_to_lose_carries_an_at_risk_mark_in_both_lenses_and_unknown_is_not_zero(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["lost_if_closed"] = _lost(n1={"dirty": 2, "unpushed": 1, "running_children": 3}, n2={"dirty": None, "unpushed": 4, "running_children": None},
                                n3={"dirty": 0, "unpushed": 0, "running_children": 0}, n4={"dirty": None, "unpushed": None, "running_children": None})
    probe = ("JSON.stringify(Object.fromEntries([...%s.querySelectorAll('[data-node]')].map(n => [n.dataset.node, "
             "(n.querySelector('.tv-risk') || {title: null}).title])))") % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}, {"eval": _paint(v, "agents")}, {"eval": probe}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work.get("n1") == "2 dirty · 1 unpushed · 3 running", work                      # the worker chip on the task it works
    assert work.get("n3") is None, work                                                    # nothing to lose: no mark
    assert agents["n1"] == "2 dirty · 1 unpushed · 3 running", agents                      # the node face
    assert agents["n2"] == "dirty not tracked · 4 unpushed · running not tracked", agents  # null is "not tracked", never 0
    assert agents["n3"] is None and agents["n4"] is None, agents                           # all zero, or all unknown: no mark


# --- codex MED + complement MED-2: a faulted provider hidden -------------------------------------------------


@needs_browser
def test_a_topology_provider_faulted_at_load_is_said_in_the_sessions_card_in_either_view(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    import levain.cockpit.topology as topo
    monkeypatch.setattr(topo, "topology_reader", lambda **kw: (lambda ctx: Fault("the session records are gone")))
    probe = ("JSON.stringify({line: [...document.querySelectorAll('#ck-hlens .k7-none.err, #ck-hlens .tv-fault')].map(n => n.textContent), "
             "rows: document.querySelectorAll('#k7-sessions .k7-srow').length})")
    out = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": "new Promise(r => setTimeout(r, 600))"}, {"eval": probe}], first=MID)
    got = json.loads(out[-1])
    assert any("Topology unavailable: " in t and "the session records are gone" in t for t in got["line"]), got   # the list view, not only topology
    assert got["rows"] == 3, got                                                                                  # the list still draws
    panel = ("(() => { const p = document.querySelector('[data-ck-panel=\"topology\"]'); return p ? getComputedStyle(p).display : 'absent'; })()")
    out = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-srow')"}, {"eval": "new Promise(r => setTimeout(r, 600))"}, {"eval": panel}], first=MID)
    assert out[-1] != "none", out                                                                                  # a faulted panel is not hidden by a broad selector


# --- codex MED: a waiting task with no card ------------------------------------------------------------------


@needs_browser
def test_a_waiting_task_with_no_card_is_a_record_and_the_header_agrees_with_the_left_column(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["tasks"] = [t for t in v["tasks"] if t["id"] not in ("t-wait",)]
    for t in v["tasks"]:
        if t["id"] == "t-sign":
            t["card"] = None                                                                                          # the real provider: a contract waiting on a signature has no card
    probe = ("JSON.stringify({recs: [...%s.querySelectorAll('.tv-rec')].map(r => [r.dataset.rec, r.classList.contains('tv-open')]), "
             "none: !!%s.querySelector('.tv-wl .tv-none'), hdr: %s.querySelector('[data-k=waiting]').textContent.replace(/\\s+/g, ' ')})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["hdr"] == "1 waiting on you" and got["recs"] == [["t-sign", False]] and got["none"] is False, got       # a record, but only a card can be clicked


# --- codex MED + glm LOW + complement 7: one ResizeObserver --------------------------------------------------


@needs_browser
def test_a_second_render_leaves_one_live_resize_observer_not_two(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    js = ("(() => { const Real = window.ResizeObserver; const made = [];"
          "window.ResizeObserver = class { constructor(cb) { this.r = new Real(cb); this.live = false; made.push(this); }"
          "  observe(n) { this.live = true; this.r.observe(n); } disconnect() { this.live = false; this.r.disconnect(); } };"
          "const c = document.querySelector('#k7-sessions .k7-card'); const v = %s;"
          "for (let i = 0; i < 3; i++) { for (const o of c.querySelectorAll('.tv')) o.remove(); c.appendChild(window.LevainTopologyView.render(v, {})); }"
          "const live = made.filter(m => m.live).length; window.ResizeObserver = Real; return JSON.stringify({made: made.length, live}); })()") % json.dumps(_value())
    out = _run(tmp_path, src, [{"eval": js}])
    got = json.loads(out[-1])
    assert got["made"] >= 1 and got["live"] == 1, got


# --- codex MED: parent cycles --------------------------------------------------------------------------------


@needs_browser
def test_a_parent_cycle_inside_a_group_still_draws_every_node_once(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["nodes"][0]["parent"] = "n2"                                                                                   # n1 -> n2 -> n1
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-col[data-group=g1] .tv-node')].map(n => n.dataset.node).sort())" % TV
    out = _run(tmp_path, src, [{"eval": _paint(v, "agents")}, {"eval": probe}])
    assert json.loads(out[-1]) == ["n1", "n2", "n3"], out


def test_the_provider_value_is_refused_when_parents_form_a_cycle():
    v = copy.deepcopy(_good())
    v["nodes"][0]["parent"] = "s1a"                                                                                  # s1 -> s1a -> s1
    p = _panel(v)
    assert p["status"] == "error" and "parent" in p["error"] and "cycle" in p["error"], p


# --- complement MED-1: a throw kills the card ----------------------------------------------------------------


@needs_browser
def test_a_renderer_that_throws_leaves_the_list_and_one_line_not_a_dead_card(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({rows: document.querySelectorAll('#k7-sessions .k7-srow').length, tv: !!%s, "
             "text: document.querySelector('#ck-hlens').innerText.replace(/\\s+/g, ' ')})") % TV
    out = _run(tmp_path, src, [{"until": TV + " && " + TV + ".querySelector('.tv-task')"}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"},
                               {"until": TV + ".querySelector('.tv-task')"},
                               {"eval": "window.LevainTopologyView.render = () => { throw new Error('boom'); }; document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"},
                               {"eval": "new Promise(r => setTimeout(r, 400))"}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["tv"] is False and got["rows"] == 3 and "topology could not be drawn" in got["text"], got


# --- complement MED-3: the signature carries only what is drawn ---------------------------------------------


@needs_browser
def test_the_repaint_signature_ignores_the_clock_and_jitter_but_follows_what_is_drawn(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    a = _value()
    b = copy.deepcopy(a)
    b["as_of"] = "2026-10-19T15:14:00Z"
    b["nodes"][0]["last_event"] = "2026-10-19T15:11:00Z"
    b["nodes"][0]["rate"] = 3.31                                                                                     # 3.25 -> 3.31: jitter, below the rounding of the display
    b["nodes"][0]["tokens"] = 412400                                                                                 # 412K both
    c = copy.deepcopy(a)
    c["nodes"][0]["state"] = "running"
    sig = "(v) => window.LevainTopologyView.signature(v, 'work')"
    js = "JSON.stringify([(%s)(%s) === (%s)(%s), (%s)(%s) === (%s)(%s)])" % (sig, json.dumps(a), sig, json.dumps(b), sig, json.dumps(a), sig, json.dumps(c))
    out = _run(tmp_path, src, [{"eval": js}])
    assert json.loads(out[-1]) == [True, False], out


# --- glm MED: v.conflicts read without arr() ----------------------------------------------------------------


@needs_browser
def test_a_conflicts_value_that_is_not_a_list_reads_as_not_tracked_not_as_zero(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["conflicts"] = {"path": "money.py"}
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": TV + ".querySelector('[data-k=conflicts]').textContent"}])
    assert out[-1] == "conflicts not tracked", out


# --- complement LOW-6: maps keyed by provider ids ------------------------------------------------------------


@needs_browser
def test_a_worker_id_that_is_an_object_property_name_is_not_a_node(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    for t in v["tasks"]:
        if t["id"] == "t-run":
            t["workers"] = ["constructor", "__proto__"]
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-task[data-task=t-run] .tv-chip')].map(c => c.textContent))" % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    assert json.loads(out[-1]) == [], out


# --- complement MED/LOW-4: the stacked card leaves no blank rows ---------------------------------------------


@needs_browser
def test_stacked_the_records_column_leaves_no_blank_gap_before_the_task_heading(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    probe = ("JSON.stringify({gap: Math.round(%s.querySelector('.tv-colh1').getBoundingClientRect().top - %s.querySelector('.tv-wl').getBoundingClientRect().bottom)})") % (TV, TV)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}], first=NARROW)
    assert json.loads(out[-1])["gap"] < 40, out
