"""The decision mark on the agents graph (Phill 10-23: "when the desk is waiting for a decision the agent graph should indicate that somehow"):
the hub and a seat node that carry `needs_open` > 0 are drawn with a red ring and a "!" badge holding the count; 0, absent, negative and
non-integer draw nothing; a tap on the badge opens `needs_first`'s card and nothing else; a tap elsewhere on the node keeps its page / its
message box. Real page in headless Chrome, a literal topology value painted the way the seat-message tests do."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_seatmsg import CHIPS, PRESSED, PRIME, _run, _tap
from tests.test_cockpit_k7_build import DESKTOP

WHAT = "ship 0.11.21 or hold for the CI sharding?"


def _needy() -> dict:
    v = _value()
    v["hub"].update(needs_open=2, needs_first="np-aaaa1111", needs_first_what=WHAT)
    node = {n["id"]: n for n in v["nodes"]}
    node["n3"].update(needs_open=1, needs_first="np-bbbb2222", needs_first_what="seat question")   # a seat's own node (group ledgerline)
    node["n4"].update(needs_open=0, needs_first="np-cccc3333")                                    # zero: nothing drawn
    node["n1"].update(needs_open=-1)                                                              # negative
    node["n2"].update(needs_open=1.5)                                                             # non-integer
    node["n5"].update(needs_open="2")                                                             # not a number; n5 otherwise has no needs keys
    return v


def _marks(scope: str) -> str:
    return "[...%s.querySelectorAll('%s .tv-need')].map(m => [m.dataset.needs, m.querySelector('.tv-needtxt').textContent, m.querySelector('.tv-needbadge title').textContent, !!m.querySelector('.tv-needring')])" % (TV, scope)


ALL = "JSON.stringify({hub: %s, nodes: Object.fromEntries(['n1','n2','n3','n4','n5'].map(i => [i, %s]))})" % (
    _marks(".tv-ghub"), "[...%s.querySelectorAll('.tv-gnode[data-node=' + i + '] .tv-need')].length" % TV)


@needs_browser
def test_the_hub_and_a_seat_with_open_needs_are_marked_and_zero_absent_and_invalid_are_not(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": ALL}])
    got = json.loads(out[-1])
    assert got["hub"] == [["2", "!2", WHAT, True]], got
    assert got["nodes"] == {"n1": 0, "n2": 0, "n3": 1, "n4": 0, "n5": 0}, got


@needs_browser
def test_a_value_with_no_needs_keys_draws_no_mark_and_no_error(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": "%s.querySelectorAll('.tv-need').length" % TV}])
    assert out[-1] == 0, out


@needs_browser
def test_the_badge_tap_opens_the_named_card_and_only_that(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    tap = lambda scope: "%s.querySelector('%s .tv-needbadge').dispatchEvent(new MouseEvent('click', {bubbles: true})); JSON.stringify([window.__opened, %s, !!document.getElementById('levain-modal')])" % (TV, scope, PRESSED)  # noqa: E731
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": tap(".tv-ghub")}, {"eval": "window.__opened = null; 1"},
                               {"eval": tap(".tv-gnode[data-node=n3]")}])
    assert json.loads(out[-3]) == ["np-aaaa1111", ["fanin"], False], out         # the desk's card; no chip pressed, no page opened
    assert json.loads(out[-1]) == ["np-bbbb2222", ["fanin"], False], out         # the seat's: its box was not messaged either


@needs_browser
def test_a_tap_on_the_marked_node_itself_still_messages_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": "window.__opened === undefined || window.__opened === null"}])
    assert out[-1] is True, out


@needs_browser
def test_a_badge_with_no_first_is_a_label_not_a_button(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    del v["hub"]["needs_first"], v["hub"]["needs_first_what"]
    probe = "JSON.stringify([%s.querySelector('.tv-ghub .tv-needbadge').getAttribute('role'), %s.querySelector('.tv-ghub .tv-needbadge title').textContent])" % (TV, TV)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": probe}])
    assert json.loads(out[-1]) == [None, "2 decisions waiting on you"], out


@needs_browser
def test_the_ring_flashes_unless_the_phone_asks_for_reduced_motion(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    anim = "JSON.stringify(getComputedStyle(%s.querySelector('.tv-ghub .tv-needring')).animationName)" % TV
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"motion": "no-preference"}, {"eval": anim},
                               {"motion": "reduce"}, {"eval": anim}])
    assert json.loads(out[-2]) == "tv-needflash" and json.loads(out[-1]) == "none", out


@needs_browser
def test_at_phone_width_the_marks_sit_inside_the_graph_and_the_page_does_not_scroll_sideways(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify([...%s.querySelectorAll('.tv-needpill')].map(p => { const r = p.getBoundingClientRect(), g = %s.querySelector('.tv-graph').getBoundingClientRect(); "
             "return [r.width > 0, r.left >= g.left - 1, r.right <= g.right + 1, r.top >= g.top - 1]; }).concat([[document.documentElement.scrollWidth <= window.innerWidth]]))") % (TV, TV)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": probe}], first=NARROW)
    got = json.loads(out[-1])
    assert len(got) == 3 and all(all(r) for r in got), got
