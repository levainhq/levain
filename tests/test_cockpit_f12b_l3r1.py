"""F12b L3 r1 fixes (K10 S2). Each test was written against the unfixed renderer and failed there first: the finding it closes is
named in its docstring. A literal value is handed to the renderer in the real page, the way test_cockpit_f12b_topology_view.py does."""
from __future__ import annotations

import copy
import json
from typing import Any

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import _sessions_fixture
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
    # K10 S6 (c90269cd) drew the Agents lens as a graph: a node at risk wears a .tv-gnode-risk dot, and the words ("not tracked") live in the
    # Work chip
    probe_g = ("JSON.stringify(Object.fromEntries([...%s.querySelectorAll('svg.tv-graph g[data-node]')].map(n => [n.dataset.node, "
               "!!n.querySelector('.tv-gnode-risk')])))") % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}, {"eval": _paint(v, "agents")}, {"eval": probe_g}])
    work, agents = json.loads(out[-3]), json.loads(out[-1])
    assert work.get("n1") == "2 dirty · 1 unpushed · 3 running", work                      # the worker chip on the task it works
    assert work.get("n2") == "dirty not tracked · 4 unpushed · running not tracked", work   # null is "not tracked", never 0
    assert work.get("n3") is None, work                                                  # nothing to lose: no mark
    assert agents["n1"] is True and agents["n2"] is True, agents                           # the node's dot: n2's null dirty is unknown, its 4 unpushed are not
    assert agents["n3"] is False and agents["n4"] is False, agents                         # all zero, or all unknown: no mark


# --- codex MED + complement MED-2: a faulted provider hidden -------------------------------------------------


# --- codex MED: a waiting task with no card ------------------------------------------------------------------


@needs_browser
def test_a_waiting_task_with_no_card_is_never_listed_and_no_header_counts_it(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["tasks"] = [t for t in v["tasks"] if t["id"] not in ("t-wait",)]
    for t in v["tasks"]:
        if t["id"] == "t-sign":
            t["card"] = None                                                                                          # the real provider: a contract waiting on a signature has no card
    probe = ("JSON.stringify({recs: %s.querySelectorAll('.tv-rec, .tv-wl').length, jump: %s.querySelectorAll('.tv-jump').length, "
             "hdr: [...%s.querySelectorAll('.tv-stat, .tv-m')].map(s => s.textContent.replace(/\\s+/g, ' '))})") % ((TV,) * 3)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["recs"] == 0, got                                                  # never listed: the Work lens has no left column of records
    assert not [h for h in got["hdr"] if "waiting" in h] and got["jump"] == 0, got   # the header says nothing of how many wait on him, and offers no way to the Needs strip: the band's number


# --- codex MED + glm LOW + complement 7: one ResizeObserver --------------------------------------------------


# --- codex MED: parent cycles --------------------------------------------------------------------------------


@needs_browser
def test_a_parent_cycle_inside_a_group_still_draws_every_node_once(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["nodes"][0]["parent"] = "n2"                                                                                   # n1 -> n2 -> n1
    # K10 S6 (c90269cd) drew the Agents lens as a graph: every node is one g[data-node] in the svg, no longer a .tv-node in a group column
    probe = "JSON.stringify([...%s.querySelectorAll('svg.tv-graph g[data-node]')].map(n => n.dataset.node).sort())" % TV
    out = _run(tmp_path, src, [{"eval": _paint(v, "agents")}, {"eval": probe}])
    assert json.loads(out[-1]) == ["n1", "n2", "n3", "n4", "n5"], out


def test_the_provider_value_is_refused_when_parents_form_a_cycle():
    v = copy.deepcopy(_good())
    v["nodes"][0]["parent"] = "s1a"                                                                                  # s1 -> s1a -> s1
    p = _panel(v)
    assert p["status"] == "error" and "parent" in p["error"] and "cycle" in p["error"], p


# --- complement MED-1: a throw kills the card ----------------------------------------------------------------


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
def test_stacked_no_blank_gap_leads_the_task_heading(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    # the records column is gone: the task heading is the first thing in the lens body, so the gap is its distance from the body's top
    probe = ("JSON.stringify({gap: Math.round(%s.querySelector('.tv-colh1').getBoundingClientRect().top - %s.querySelector('.tv-lensbody').getBoundingClientRect().top)})") % (TV, TV)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}], first=NARROW)
    assert json.loads(out[-1])["gap"] < 40, out
