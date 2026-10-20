"""K10 S6: the Agents lens is a graph as the F12 mock draws it (topology_view.js). A literal value is handed to the renderer in the real page:
a node per wire node with its state class, a waiting node's ring and its click, the streams (none for a null rate, a rate of 0 or an idle node,
none under reduced motion), unknown model / tokens drawn as unknown, the file on a chip between its nodes, and the hover face."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import TV, _paint, _run, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import _sessions_fixture

REDUCED = ("(() => { const m = window.matchMedia.bind(window); window.matchMedia = (q) => (String(q).includes('prefers-reduced-motion') "
           "? { matches: true, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} } : m(q)); return 1; })()")


def _graph_value(rates: bool = True) -> dict:
    v = _value()
    if rates:
        v["nodes"][4]["rate"] = 5.0                       # n5 is idle: a rate on an idle node is still no stream
    else:
        for n in v["nodes"]:
            n["rate"] = None
    return v


# the page repaints once when the lens changes (its signature moves): switch to Agents on the page first, so the literal value painted next is not replaced
PRIME = [{"eval": "document.querySelector('#k7-sessions [data-tv=agents]').click(); 1"}, {"until": TV + ".dataset.lens == 'agents'"}]


def _probe() -> str:
    return ("JSON.stringify({nodes: Object.fromEntries([...%s.querySelectorAll('.tv-gnode')].map(g => [g.dataset.node, [g.dataset.state, g.classList.contains('tv-open'), "
            "!!g.querySelector('.tv-pulse'), !!g.querySelector('.tv-x'), g.dataset.face]])), "
            "edges: Object.fromEntries([...%s.querySelectorAll('.tv-edge')].map(e => [e.dataset.edge, e.dataset.rate])), "
            "stage: {...%s.querySelector('.tv-stage').dataset}, hub: %s.querySelector('.tv-hubname').textContent, hubsub: %s.querySelector('.tv-hubsub').textContent, "
            "groups: [...%s.querySelectorAll('.tv-ggroup .tv-gsub')].map(t => t.textContent), svg: !!%s.querySelector('svg.tv-graph'), "
            "conf: [...%s.querySelectorAll('.tv-gconf .tv-conftext')].map(t => t.textContent), legend: %s.querySelector('.tv-legend').textContent})") % ((TV,) * 9)


@needs_browser
def test_a_node_is_drawn_per_wire_node_with_its_state_ring_face_and_the_hub_and_groups_say_their_counts(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, {"eval": _probe()}])
    got = json.loads(out[-1])
    assert got["svg"] is True and sorted(got["nodes"]) == ["n1", "n2", "n3", "n4", "n5"], got
    assert [got["nodes"][k][0] for k in ("n1", "n2", "n3", "n4", "n5")] == ["waiting", "running", "errored", "stalled", "idle"], got
    assert got["nodes"]["n1"][2] is True and all(got["nodes"][k][2] is False for k in ("n2", "n3", "n4", "n5")), got      # only waiting breathes
    assert got["nodes"]["n3"][3] is True and got["nodes"]["n2"][3] is False, got                                          # errored carries the x
    assert got["hub"] == "Sage" and got["hubsub"] == "5 sessions · 1 running", got
    assert got["groups"] == ["3 agents · 1 running", "2 agents · 0 running"], got
    assert got["nodes"]["n1"][4].startswith("WAITING ON YOU | claude_code · ledgerline | claude-opus-5-5 · 412K tok | no tool · 13 m"), got
    assert got["nodes"]["n4"][4].startswith("STALLED"), got
    assert got["nodes"]["n5"][4].split(" | ")[0] == "IDLE" and "quiet since" in got["nodes"]["n5"][4], got
    assert "waiting on you" in got["legend"] and "streams carry events per minute; none when idle" in got["legend"], got


@needs_browser
def test_a_waiting_node_opens_its_card_and_no_other_node_is_a_button(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    click = "(() => { %s.querySelector('.tv-gnode[data-node=n1]').dispatchEvent(new MouseEvent('click', {bubbles: true})); return window.__opened; })()" % TV
    other = "(() => { %s.querySelector('.tv-gnode[data-node=n4]').dispatchEvent(new MouseEvent('click', {bubbles: true})); return String(window.__opened); })()" % TV
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, {"eval": click}, {"eval": "window.__opened = null; 1"}, {"eval": other},
                               {"eval": "JSON.stringify([...%s.querySelectorAll('.tv-gnode.tv-open')].map(g => g.dataset.node))" % TV}])
    assert out[-4] == "consent:abc", out
    assert out[-2] == "null", out
    assert json.loads(out[-1]) == ["n1"], out


@needs_browser
def test_streams_run_only_on_a_real_positive_rate_of_a_live_node(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    wait = "new Promise(r => setTimeout(r, 300))"
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, {"eval": wait}, {"eval": _probe()}])
    got = json.loads(out[-1])
    # n1 (3.25/min) streams, and so does its group's edge; n2 (a real 0), n3/n4 (null) and n5 (idle, 5.0) do not
    assert got["edges"]["node:n1"] == "3.25" and got["edges"]["hub:g1"] == "3.25", got
    assert [k for k, r in got["edges"].items() if r != "none"] == ["hub:g1", "node:n1"] or sorted(k for k, r in got["edges"].items() if r != "none") == ["hub:g1", "node:n1"], got
    assert got["stage"]["streams"] == "2" and got["stage"]["motion"] == "on" and int(got["stage"]["particles"]) > 0, got["stage"]


@needs_browser
def test_a_wire_with_no_rates_draws_no_stream_at_all(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(False), "agents")}, {"eval": "new Promise(r => setTimeout(r, 300))"}, {"eval": _probe()}])
    got = json.loads(out[-1])
    assert got["stage"]["streams"] == "0" and got["stage"]["particles"] == "0", got["stage"]
    assert set(got["edges"].values()) == {"none"}, got


@needs_browser
def test_under_reduced_motion_nothing_moves(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": REDUCED}, {"eval": _paint(_graph_value(), "agents")}, {"eval": "new Promise(r => setTimeout(r, 300))"}, {"eval": _probe()}])
    got = json.loads(out[-1])
    assert got["stage"]["motion"] == "off" and got["stage"]["particles"] == "0" and got["stage"]["streams"] == "2", got["stage"]


@needs_browser
def test_unknown_model_and_tokens_are_drawn_as_unknown_never_zero(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, {"eval": _probe()}])
    nodes = json.loads(out[-1])["nodes"]
    assert "model unknown · tokens unknown" in nodes["n3"][4], nodes["n3"]
    assert "model unknown · 96K tok" in nodes["n4"][4], nodes["n4"]
    assert "0 tok" not in nodes["n3"][4].replace("tokens unknown", ""), nodes["n3"]


@needs_browser
def test_a_file_conflict_puts_the_file_on_a_chip_between_its_nodes_and_the_hover_shows_the_face(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    hover = ("(() => { const g = %s.querySelector('.tv-gnode[data-node=n1]'); g.dispatchEvent(new PointerEvent('pointermove', {bubbles: true, clientX: 300, clientY: 300})); "
             "const t = %s.querySelector('.tv-tip'); return JSON.stringify([t.hidden, t.textContent]); })()") % (TV, TV)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_graph_value(), "agents")}, {"eval": _probe()}, {"eval": hover}])
    assert json.loads(out[-2])["conf"] == ["money.py"], out
    hidden, text = json.loads(out[-1])
    assert hidden is False and "claude-opus-5-5" in text and "3.3 events/min" in text and "consent:abc" in text, text
    none = _graph_value()
    none["conflicts"] = None
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(none, "agents")}, {"eval": _probe()}])
    assert json.loads(out[-1])["conf"] == [], out
