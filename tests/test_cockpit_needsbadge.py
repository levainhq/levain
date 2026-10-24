"""The decision mark on the agents graph (Phill 10-23: "when the desk is waiting for a decision the agent graph should indicate that somehow"):
the hub and a seat node that carry `needs_open` > 0 are drawn with a red ring and a "!" badge holding the count; 0, absent, negative and
non-integer draw nothing; a tap on the badge opens `needs_first_card`'s card and nothing else (no card id: a label, not a button); a tap elsewhere on the node keeps its page / its
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
    v["hub"].update(needs_open=2, needs_first="np-aaaa1111", needs_first_card="np:np-aaaa1111", needs_first_what=WHAT)
    node = {n["id"]: n for n in v["nodes"]}
    node["n3"].update(needs_open=1, needs_first="np-bbbb2222", needs_first_card="np:np-bbbb2222", needs_first_what="seat question")   # a seat's own node (group ledgerline)
    node["n4"].update(needs_open=0, needs_first="np-cccc3333", needs_first_card="np:np-cccc3333")                                    # zero: nothing drawn
    node["n1"].update(needs_open=-1)                                                              # negative
    node["n2"].update(needs_open=1.5)                                                             # non-integer
    node["n5"].update(needs_open="2")                                                             # not a number; n5 otherwise has no needs keys
    return v


BADGE = "%s.querySelector('.tv-needbadge[data-needfor=\"%%s\"]')" % TV
HUB_MARK = ("JSON.stringify([...%s.querySelectorAll('.tv-needbadge[data-needfor=hub]')].map(b => [b.querySelector('.tv-needtxt').textContent, "
            "b.querySelector('title').textContent, !!%s.querySelector('.tv-ghub .tv-needring')]))") % (TV, TV)
ALL = "JSON.stringify({hub: %s.querySelectorAll('.tv-needbadge[data-needfor=hub]').length, nodes: Object.fromEntries(['n1','n2','n3','n4','n5'].map(i => [i, %s.querySelectorAll('.tv-needbadge[data-needfor=' + i + ']').length]))})" % (TV, TV)


@needs_browser
def test_the_hub_and_a_seat_with_open_needs_are_marked_and_zero_absent_and_invalid_are_not(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": ALL}])
    got = json.loads(out[-1])
    assert got["hub"] == 1, got
    assert json.loads(_run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": HUB_MARK}])[-1]) == [["!2", WHAT, True]]
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
    tap = lambda who: "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); JSON.stringify([window.__opened, %s, !!document.getElementById('levain-modal')])" % (BADGE % who, PRESSED)  # noqa: E731
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": tap("hub")}, {"eval": "window.__opened = null; 1"},
                               {"eval": tap("n3")}])
    assert json.loads(out[-3]) == ["np:np-aaaa1111", ["fanin"], False], out      # the CARD id, not the bare row id; no chip pressed, no page opened
    assert json.loads(out[-1]) == ["np:np-bbbb2222", ["fanin"], False], out      # the seat's: its box was not messaged either


@needs_browser
def test_a_badge_with_needs_first_but_no_card_id_is_display_only_and_a_tap_messages_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    node = {n["id"]: n for n in v["nodes"]}
    del node["n3"]["needs_first_card"]
    probe = "JSON.stringify([%s.getAttribute('role'), %s.getAttribute('tabindex'), %s.querySelector('.tv-needhit'), !!%s.closest('.tv-gnode')])" % (
        BADGE % "n3", BADGE % "n3", BADGE % "n3", BADGE % "n3")
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": probe},
                               {"eval": "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); 1" % (BADGE % "n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": "window.__opened === undefined || window.__opened === null"}])
    assert json.loads(out[-4]) == [None, None, None, True], out        # no role, no tabindex, no hit rect; still inside its node
    assert out[-1] is True, out


@needs_browser
def test_a_focused_badge_keeps_its_focus_through_a_repaint_and_enter_still_opens_its_card(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    key = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); JSON.stringify([window.__opened, %s])" % PRESSED
    ident = "JSON.stringify([document.activeElement.getAttribute('data-needfor'), document.activeElement.classList.contains('tv-needbadge')])"
    out = _run(tmp_path, src, [{"focus": True}, *PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": "%s.focus(); 1" % (BADGE % "n3")}, {"eval": "new Promise(r => setTimeout(r, 50))"},
                               {"eval": _paint(_needy(), "agents")}, {"until": ident + " == '[\"n3\",true]'"}, {"eval": key},
                               {"eval": "window.__opened = null; %s.focus(); 1" % (BADGE % "hub")}, {"eval": "new Promise(r => setTimeout(r, 50))"},
                               {"eval": _paint(_needy(), "agents")}, {"until": ident + " == '[\"hub\",true]'"}, {"eval": key}])
    assert json.loads(out[-6]) == ["np:np-bbbb2222", ["fanin"]], out    # the seat's badge, not its node (which would message the seat)
    assert json.loads(out[-1]) == ["np:np-aaaa1111", ["fanin"]], out    # the hub badge is tracked too


@needs_browser
def test_the_badge_sits_outside_its_node_and_a_tap_in_the_padding_opens_the_card_not_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    b = BADGE % "n3"
    probe = ("(() => { const b = %s; b.scrollIntoView({block: 'center', inline: 'center'}); const h = b.querySelector('.tv-needhit').getBoundingClientRect(), "
             "p = b.querySelector('.tv-needpill').getBoundingClientRect(), x = Math.round(p.right + 10), y = Math.round(p.top + p.height / 2), e = document.elementFromPoint(x, y); "
             "const cls = e && e.getAttribute('class'); if (e) e.dispatchEvent(new MouseEvent('click', {bubbles: true})); "
             "return JSON.stringify([!!b.closest('[role=button]:not(.tv-needbadge)'), !!b.closest('.tv-gnode'), h.width >= 43.5, h.height >= 43.5, cls, window.__opened, %s]); })()") % (b, PRESSED)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"until": b + ".querySelector('.tv-needhit').getBoundingClientRect().width >= 43.5"}, {"eval": probe}], first=NARROW)
    nested, innode, wok, hok, cls, opened, pressed = json.loads(out[-1])
    assert (nested, innode, wok, hok) == (False, False, True, True), out
    assert (cls, opened, pressed) == ("tv-needhit", "np:np-bbbb2222", ["fanin"]), out      # 10px outside the pill, inside the hit rect: the card, not the seat's box


@needs_browser
def test_needs_first_and_the_card_id_are_capped_at_the_same_bound_as_the_title(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    v["hub"].update(needs_first="f" * 400, needs_first_card="c" * 400, needs_first_what="w" * 400)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")},
                               {"eval": "(() => { const b = %s; return JSON.stringify([b.getAttribute('data-needbadge').length, b.querySelector('title').textContent.length]); })()" % (BADGE % "hub")},
                               {"eval": "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); window.__opened.length" % (BADGE % "hub")}])
    assert json.loads(out[-2]) == [120, 120] and out[-1] == 120, out


@needs_browser
def test_a_tap_on_the_marked_node_itself_still_messages_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": "window.__opened === undefined || window.__opened === null"}])
    assert out[-1] is True, out


@needs_browser
def test_a_badge_with_no_card_id_at_all_is_a_label_not_a_button(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    del v["hub"]["needs_first"], v["hub"]["needs_first_card"], v["hub"]["needs_first_what"]
    probe = "JSON.stringify([%s.getAttribute('role'), %s.querySelector('title').textContent])" % (BADGE % "hub", BADGE % "hub")
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
