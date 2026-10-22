"""F12c, the Lost if closed panel: per live session, what closing it now would lose, drawn as its own panel beside the Sessions card
(360 px from 1440, below it under that, stacked on a phone). Through the real server for the page (the placement, the lens switch, a
faulted provider) and a literal value for the shapes this install never sends (dirty files, unpushed, null counts)."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import MID, NARROW, TV, _run, _spore, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import DESKTOP, _sessions_fixture

LC = "document.querySelector('#k7-sessions .lc')"
CARD = "document.querySelector('#k7-sessions .k7-card')"


def _paint_lost(value: dict) -> str:
    """Replace the real panel with the renderer's own output for a literal value."""
    return ("(() => { const w = document.querySelector('#k7-sessions .k7-sess-wrap'); for (const o of w.querySelectorAll('.lc')) o.remove(); "
            "w.appendChild(window.LevainTopologyView.renderLost(%s)); w.classList.add('k7-has-lost'); return 1; })()") % json.dumps(value)


def _entry(node: str, **o) -> dict:
    return {"node": node, "dirty": 0, "dirty_names": [], "unpushed": 0, "branches": [], "uncaptured_episodes": None, "running_children": None, **o}


def _lost_value() -> dict:
    v = _value()
    v["lost_if_closed"] = [
        _entry("n1", dirty=3, dirty_names=["money.py", "test_money.py", "CHANGELOG.md"], unpushed=2, branches=["fix/round-cents"], uncaptured_episodes=5, running_children=2),
        _entry("n4", dirty=1, dirty_names=["money.py"], unpushed=1, branches=["main"], uncaptured_episodes=2, running_children=0),
        _entry("n2", dirty=2, dirty_names=["docs/x".replace("/", "_"), "api.md"], unpushed=0, running_children=0),
        _entry("n5", dirty=0, unpushed=0, running_children=0, uncaptured_episodes=0),
        _entry("n3", dirty=None, unpushed=None),
    ]
    return v


@needs_browser
def test_the_panel_sits_right_of_the_sessions_card_at_1440_and_below_it_under_1440_and_on_a_phone(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({card: [Math.round(%s.getBoundingClientRect().right), Math.round(%s.getBoundingClientRect().bottom), Math.round(%s.getBoundingClientRect().top)], "
             "lc: [Math.round(%s.getBoundingClientRect().left), Math.round(%s.getBoundingClientRect().top), Math.round(%s.getBoundingClientRect().width)], "
             "scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth})") % (CARD, CARD, CARD, LC, LC, LC)
    wait = {"until": LC}
    wide = json.loads(_run(tmp_path, src, [wait, {"eval": probe}])[-1])
    assert wide["lc"][0] >= wide["card"][0] and abs(wide["lc"][2] - 360) <= 2 and abs(wide["lc"][1] - wide["card"][2]) <= 2, wide     # right of it, 360 wide, level with it
    for first in (MID, NARROW):
        got = json.loads(_run(tmp_path, src, [wait, {"eval": probe}], first=first)[-1])
        assert got["lc"][1] >= got["card"][1] - 1 and got["scroll"] <= 0, (first, got)                                              # below it, no horizontal scroll


@needs_browser
def test_through_the_real_provider_each_live_session_is_a_line_and_an_unread_tree_never_says_nothing_at_risk(tmp_path, monkeypatch):
    root, src = _sessions_fixture(tmp_path, monkeypatch)
    _spore(root)
    probe = ("JSON.stringify({head: %s.querySelector('.lc-h').innerText.replace(/\\s+/g, ' '), lines: [...%s.querySelectorAll('.lc-line')].map(l => l.innerText.replace(/\\s+/g, ' ')), "
             "foot: (%s.querySelector('.lc-foot') || {textContent: ''}).textContent})") % (LC, LC, LC)
    out = _run(tmp_path, src, [{"until": LC}, {"eval": probe},
                               {"eval": "document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"}, {"until": TV + ".dataset.lens == 'agents'"}, {"eval": probe}])
    first, second = json.loads(out[-4]), json.loads(out[-1])
    assert first["head"].startswith("Lost if closed 2") and "0 would lose work" in first["head"], first
    assert len(first["lines"]) == 2 and all("git state not read" in l and "nothing at risk" not in l for l in first["lines"]), first   # the fixture's cwds are no git tree
    assert "Closing a session never runs anything." in first["foot"], first
    assert second == first, second                                                                                                      # the lens does not change the panel: it is its own


@needs_browser
def test_a_session_with_work_to_lose_shows_its_chips_and_files_and_the_clear_ones_one_line_at_the_bottom(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({head: %s.querySelector('.lc-h').innerText.replace(/\\s+/g, ' '), "
             "sess: [...%s.querySelectorAll('.lc-sess')].map(s => [s.dataset.node, s.querySelector('b').textContent, [...s.querySelectorAll('.lc-chip')].map(c => [c.textContent, c.classList.contains('unk')]), "
             "(s.querySelector('.lc-mono') || {textContent: null}).textContent]), "
             "lines: [...%s.querySelectorAll('.lc-line')].map(l => l.innerText.replace(/\\s+/g, ' ')), "
             "order: [...%s.children].map(c => c.className.split(' ')[0])})") % (LC, LC, LC, LC)
    out = _run(tmp_path, src, [{"until": LC}, {"eval": _paint_lost(_lost_value())}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["head"] == "Lost if closed 5 3 would lose work", got
    s = {x[0]: x for x in got["sess"]}
    assert list(s) == ["n1", "n4", "n2"], got                                                                                           # risk first, in the feed's order
    assert [c[0] for c in s["n1"][2]] == ["3 dirty files", "2 unpushed", "5 uncaptured episodes", "2 running lanes"], s["n1"]
    assert s["n1"][3] == "money.py, test_money.py, CHANGELOG.md · branch fix/round-cents", s["n1"]
    assert [c[0] for c in s["n4"][2]] == ["1 dirty file", "1 unpushed", "2 uncaptured episodes"], s["n4"]                         # singular; a zero is no chip
    assert s["n2"][2] == [["2 dirty files", False], ["uncaptured unknown", True]], s["n2"]                                              # null is a dashed "unknown", never 0
    assert got["lines"] == ["claude_code · tally ✓ nothing at risk", "test-runner git state not read"], got                          # clear ones, then an unread tree: never a false all-clear
    assert got["order"].index("lc-sess") < got["order"].index("lc-quiet") < got["order"].index("lc-foot"), got


@needs_browser
def test_no_live_sessions_is_one_line_in_the_panel(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["lost_if_closed"] = []
    out = _run(tmp_path, src, [{"until": LC}, {"eval": _paint_lost(v)}, {"eval": LC + ".innerText.replace(/\\s+/g, ' ')"}])
    assert out[-1] == "Lost if closed No live sessions, so nothing to lose.", out


@needs_browser
def test_a_faulted_topology_provider_leaves_the_panel_one_unavailable_line(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    import levain.cockpit.topology as topo
    from levain.cockpit.results import Fault
    monkeypatch.setattr(topo, "topology_reader", lambda **kw: (lambda ctx: Fault("the session records are gone")))
    out = _run(tmp_path, src, [{"until": "document.querySelector('#k7-sessions .k7-srow, #k7-sessions .k7-none')"}, {"until": LC},
                               {"eval": LC + ".innerText.replace(/\\s+/g, ' ')"}], first=MID, until=CARD)    # a faulted topology draws no .tv: the card is the signal
    assert out[-1].startswith("Lost if closed Unavailable: ") and "the session records are gone" in out[-1], out
