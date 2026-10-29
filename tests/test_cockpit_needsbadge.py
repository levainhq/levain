"""The decision mark on the agents graph (Phill 10-23: "when the desk is waiting for a decision the agent graph should indicate that somehow"):
the hub and a seat node that carry `needs_open` > 0 are drawn with a red ring and a "!" badge holding the count; 0, absent, negative and
non-integer draw nothing; a tap on the badge opens `needs_first_card`'s card and nothing else (no card id: a label, not a button); a tap elsewhere on the node opens its page (a
seat's holds its message box). Real page in headless Chrome, a literal topology value painted the way the seat-message tests do."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _value
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_seatmsg import CHIPS, PRESSED, PRIME, _run, _tap

from tests.test_cockpit_k7_build import DESKTOP

# a tap on a seat's node opens its page holding a message box addressed to it (Phill 10-25, "61a")
SEAT_PAGE = "(document.querySelector('#levain-modal .agent-msg .ck-addr') || {}).textContent == 'to seat:ledgerline'"
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
            "b.querySelector('title').textContent, !!%s.querySelector('.tv-ghub .tv-hubmark')]))") % (TV, TV)
ALL = "JSON.stringify({hub: %s.querySelectorAll('.tv-needbadge[data-needfor=hub]').length, nodes: Object.fromEntries(['n1','n2','n3','n4','n5'].map(i => [i, %s.querySelectorAll('.tv-needbadge[data-needfor=\"node:' + i + '\"]').length]))})" % (TV, TV)
HIT = "%s.querySelector('.tv-needhit[data-needhitfor=\"%%s\"]')" % TV                       # the transparent tap pad: a separate layer BENEATH the nodes


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
                               {"eval": tap("node:n3")}])
    assert json.loads(out[-3]) == ["np:np-aaaa1111", ["fanin"], False], out      # the CARD id, not the bare row id; no chip pressed, no page opened
    assert json.loads(out[-1]) == ["np:np-bbbb2222", ["fanin"], False], out      # the seat's: its box was not messaged either


@needs_browser
def test_a_badge_with_needs_first_but_no_card_id_is_display_only_and_a_tap_messages_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    node = {n["id"]: n for n in v["nodes"]}
    del node["n3"]["needs_first_card"]
    probe = "JSON.stringify([%s.getAttribute('role'), %s.getAttribute('tabindex'), %s, !!%s.closest('.tv-gnode')])" % (
        BADGE % "node:n3", BADGE % "node:n3", HIT % "node:n3", BADGE % "node:n3")
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": probe},
                               {"eval": "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); 1" % (BADGE % "node:n3")},
                               {"until": SEAT_PAGE},
                               {"eval": "window.__opened === undefined || window.__opened === null"}])
    assert json.loads(out[-4]) == [None, None, None, True], out        # no role, no tabindex, no hit rect; still inside its node
    assert out[-1] is True, out


@needs_browser
def test_a_focused_badge_keeps_its_focus_through_a_repaint_and_enter_still_opens_its_card(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    key = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); JSON.stringify([window.__opened, %s])" % PRESSED
    ident = "JSON.stringify([document.activeElement.getAttribute('data-needfor'), document.activeElement.classList.contains('tv-needbadge')])"
    out = _run(tmp_path, src, [{"focus": True}, *PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": "%s.focus(); 1" % (BADGE % "node:n3")}, {"eval": "new Promise(r => setTimeout(r, 50))"},
                               {"eval": _paint(_needy(), "agents")}, {"until": ident + " == '[\"node:n3\",true]'"}, {"eval": key},
                               {"eval": "window.__opened = null; %s.focus(); 1" % (BADGE % "hub")}, {"eval": "new Promise(r => setTimeout(r, 50))"},
                               {"eval": _paint(_needy(), "agents")}, {"until": ident + " == '[\"hub\",true]'"}, {"eval": key}])
    assert json.loads(out[-6]) == ["np:np-bbbb2222", ["fanin"]], out    # the seat's badge, not its node (which would message the seat)
    assert json.loads(out[-1]) == ["np:np-aaaa1111", ["fanin"]], out    # the hub badge is tracked too


@needs_browser
def test_the_badge_sits_outside_its_node_and_a_tap_in_the_padding_opens_the_card_not_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    b = BADGE % "node:n3"
    away = ("(() => { const b = %s; b.scrollIntoView({block: 'center', inline: 'center'}); const h = %s.getBoundingClientRect(), "
            "p = b.querySelector('.tv-needpill').getBoundingClientRect(), c = %s.querySelector('.tv-gnode[data-node=n3] .tv-hit').getBoundingClientRect(), "
            "px = p.left + p.width / 2, py = p.top + p.height / 2, dx = px - (c.left + c.width / 2), dy = py - (c.top + c.height / 2), d = Math.hypot(dx, dy), "
            "e = document.elementFromPoint(Math.round(px + dx / d * 20), Math.round(py + dy / d * 20)); "
            "const cls = e && e.getAttribute('class'); if (e) e.dispatchEvent(new MouseEvent('click', {bubbles: true})); "
            "return JSON.stringify([!!b.closest('[role=button]:not(.tv-needbadge)'), !!b.closest('.tv-gnode'), h.width >= 43.5, h.height >= 43.5, cls, window.__opened, %s]); })()") % (b, HIT % "node:n3", TV, PRESSED)
    for size in (DESKTOP, NARROW):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"until": HIT % "node:n3" + ".getBoundingClientRect().width >= 43.5"}, {"eval": away}], first=size)
        nested, innode, wok, hok, cls, opened, pressed = json.loads(out[-1])
        assert (nested, innode, wok, hok) == (False, False, True, True), out
        assert (cls, opened, pressed) == ("tv-needhit", "np:np-bbbb2222", ["fanin"]), out      # 20px past the pill, AWAY from the node, inside the pad: the card, not the seat's box


@needs_browser
def test_a_tap_on_the_node_centre_messages_the_seat_and_the_pill_itself_opens_the_card(tmp_path, monkeypatch):
    """codex L3 r2 MED: the 44px pad covered the node's centre and, drawn after the node, won the hit test. The pad is now a layer beneath every node."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    centre = ("(() => { const g = %s.querySelector('.tv-gnode[data-node=n3]'); g.scrollIntoView({block: 'center', inline: 'center'}); const r = g.querySelector('.tv-ring').getBoundingClientRect(), "
              "e = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)), "
              "hc = %s.querySelector('.tv-hubcore'), hr = (hc.scrollIntoView({block: 'center', inline: 'center'}), hc.getBoundingClientRect()), he = document.elementFromPoint(Math.round(hr.left + hr.width / 2), Math.round(hr.top + hr.height / 2)); "
              "const own = !!(e && e.closest('.tv-gnode[data-node=n3]')), hubOwn = !!(he && he.closest('.tv-ghub')), onPad = !!(e && e.classList.contains('tv-needhit')) || !!(he && he.classList.contains('tv-needhit')); "
              "if (e) e.dispatchEvent(new MouseEvent('click', {bubbles: true})); return JSON.stringify([own, hubOwn, onPad, window.__opened === undefined || window.__opened === null, e && e.tagName + '.' + e.getAttribute('class'), he && he.tagName + '.' + he.getAttribute('class')]); })()") % (TV, TV)
    pill = ("(() => { const bb = %s; bb.scrollIntoView({block: 'center', inline: 'center'}); const p = bb.querySelector('.tv-needpill').getBoundingClientRect(), e = document.elementFromPoint(Math.round(p.left + p.width / 2), Math.round(p.top + p.height / 2)); "
            "const own = !!(e && e.closest('.tv-needbadge')); if (e) e.dispatchEvent(new MouseEvent('click', {bubbles: true})); return JSON.stringify([own, window.__opened]); })()") % (BADGE % "node:n3")
    for size in (DESKTOP, NARROW):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"until": HIT % "node:n3" + ".getBoundingClientRect().width >= 43.5"}, {"eval": centre},
                                   {"until": SEAT_PAGE}, {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
                                   {"eval": "window.__opened = null; 1"}, {"eval": pill}], first=size)
        assert json.loads(out[-5])[:4] == [True, True, False, True], out       # the node's centre and the hub's centre hit the node and the hub, never a pad; the card stayed shut
        assert json.loads(out[-1]) == [True, "np:np-bbbb2222"], out        # the pill is on top and opens the card


@needs_browser
def test_needs_first_is_capped_and_a_card_id_over_the_bound_is_refused_never_cut(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _needy()
    v["hub"].update(needs_first="f" * 400, needs_first_card="np:" + "c" * 117, needs_first_what="w" * 400)          # the card id is exactly 120: kept whole
    v["nodes"][2].update(needs_first_card="np:" + "c" * 118)                                                         # one over (n3): refused
    probe = ("(() => { const b = %s, o = %s; return JSON.stringify([b.getAttribute('data-needfirst').length, b.querySelector('title').textContent.length, "
             "o.getAttribute('role'), !!%s]); })()") % (BADGE % "hub", BADGE % "node:n3", HIT % "node:n3")
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, {"eval": probe},
                               {"eval": "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); window.__opened.length" % (BADGE % "hub")}])
    assert json.loads(out[-2]) == [120, 120, None, False] and out[-1] == 120, out


@needs_browser
def test_a_tap_on_the_marked_node_itself_still_messages_the_seat(tmp_path, monkeypatch):
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": _tap("n3")},
                               {"until": SEAT_PAGE},
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
    anim = "JSON.stringify(getComputedStyle(%s.querySelector('.tv-gnode[data-node=n3] .tv-needring')).animationName)" % TV     # a seat's ring: the desk's mark has its own cadence (a direction of its own)
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


@needs_browser
def test_a_badge_tap_runs_the_real_open_path_and_lights_the_row_that_carries_the_card_id(tmp_path, monkeypatch):
    """codex L3 r2 OPEN 1: the other tests stub openCard. This one leaves the page's own openNeedsYouCard in place (the lens is the page's, not _paint's), feeds the real
    renderer a hub carrying flow's `needs_first_card` ("np:<id>", the exact id a Needs-you card carries in data-needs-id), and checks the one row with that id is lit."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    patch = ("(() => { const T = window.LevainTopologyView, orig = T.render; T.render = function (v, o) { const w = JSON.parse(JSON.stringify(v)); "
             "w.hub = Object.assign({}, w.hub, {needs_open: 1, needs_first: 'np-aaaa1111', needs_first_card: 'np:np-aaaa1111', needs_first_what: 'ship or hold?'}); return orig.call(T, w, o); }; return 1; })()")
    rows = ("(() => { let h = document.getElementById('ck-needs'); if (!h) { h = document.createElement('div'); h.id = 'ck-needs'; document.body.appendChild(h); } h.hidden = false; "
            "h.replaceChildren(...['np:np-zzzz9999', 'np:np-aaaa1111', 'np-aaaa1111'].map(id => { const d = document.createElement('div'); d.className = 'needs-row'; d.dataset.needsId = id; d.style.height = '10px'; return d; })); return 1; })()")
    lit = "JSON.stringify([...document.querySelectorAll('#ck-needs .needs-lit')].map(n => n.dataset.needsId))"
    out = _run(tmp_path, src, [{"eval": patch}, {"eval": "document.querySelector('#ck-hlens [data-tv=work]').click(); 1"}, {"until": TV + ".dataset.lens == 'work'"}, *PRIME, {"until": BADGE % "hub" + " && " + BADGE % "hub" + ".getAttribute('role') == 'button'"}, {"eval": rows},
                               {"eval": "%s.dispatchEvent(new MouseEvent('click', {bubbles: true})); 1" % (BADGE % "hub")}, {"eval": lit}])
    assert json.loads(out[-1]) == ["np:np-aaaa1111"], out       # the row carrying the card id is lit; the decoy and the bare row id are not


@needs_browser
def test_a_node_whose_id_is_hub_does_not_share_the_desks_badge_key(tmp_path, monkeypatch):
    """codex L3 r2 LOW: owners are typed ("hub", "node:<id>"), so a seat node named "hub" restores to its own badge, not the desk's."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = json.loads(json.dumps(_needy()).replace('"n3"', '"hub"'))
    key = "document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); JSON.stringify([window.__opened, %s])" % PRESSED
    ident = "JSON.stringify([document.activeElement.getAttribute('data-needfor'), document.activeElement.classList.contains('tv-needbadge')])"
    out = _run(tmp_path, src, [{"focus": True}, *PRIME, {"eval": _paint(v, "agents")}, {"eval": "%s.focus(); 1" % (BADGE % "node:hub")}, {"eval": "new Promise(r => setTimeout(r, 50))"},
                               {"eval": _paint(v, "agents")}, {"until": ident + " == '[\"node:hub\",true]'"}, {"eval": key}])
    assert json.loads(out[-1])[0] == "np:np-bbbb2222", out      # the seat's card, not the desk's np:np-aaaa1111


@needs_browser
def test_a_remembered_badge_whose_owner_lost_its_needs_does_not_steal_focus_when_it_returns(tmp_path, monkeypatch):
    """complement L3 r2 LOW: pending is cleared when no element matches after a rebuild, so a later rebuild that re-adds the badge, focus on the body, leaves focus alone."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    gone = _needy()
    {n["id"]: n for n in gone["nodes"]}["n3"].update(needs_open=0)
    wait = {"eval": "new Promise(r => setTimeout(r, 120))"}
    active = "JSON.stringify([document.activeElement === document.body, document.activeElement.getAttribute('data-needfor')])"
    out = _run(tmp_path, src, [{"focus": True}, *PRIME, {"eval": _paint(_needy(), "agents")}, {"eval": "%s.focus(); 1" % (BADGE % "node:n3")}, wait,
                               {"eval": _paint(gone, "agents")}, wait, {"eval": active},
                               {"eval": _paint(_needy(), "agents")}, wait, {"eval": active}])
    assert json.loads(out[-4]) == [True, None], out      # the badge left; focus is on the body
    assert json.loads(out[-1]) == [True, None], out      # the badge came back; it did not take the focus


@needs_browser
def test_the_hit_pad_is_44_css_px_on_the_first_phone_width_build(tmp_path, monkeypatch):
    """complement L3 r2 LOW: scale is set before the first drawGraph at phone width; the pad is measured at the first build that carries the phone class."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    first = "(() => { const h = %s; if (!h || !%s.querySelector('.tv-phone')) return false; window.__w = h.getBoundingClientRect().width; return true; })()" % (HIT % "node:n3", TV)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, {"until": first}, {"eval": "window.__w"}], first=NARROW)
    assert out[-1] >= 43.5, out


SWEEPS = "%s.querySelectorAll('.tv-bezel.sweep').length" % TV
WAIT = {"eval": "new Promise(r => setTimeout(r, 200))"}


@needs_browser
def test_a_redraw_of_the_same_lens_at_the_same_count_does_not_replay_the_bezel_sweep(tmp_path, monkeypatch):
    """L1 W1+W2: the sweep plays when the desk's mark arrives or its count changes, never when the same lens redraws at a new width."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_needy(), "agents")}, WAIT, {"eval": SWEEPS},
                               {"eval": "%s.querySelector('.tv-stage').style.width = '700px'; 1" % TV}, WAIT, {"eval": "%s.querySelector('.tv-stage').style.width = '640px'; 1" % TV}, WAIT,
                               {"eval": "JSON.stringify([%s, %s.querySelectorAll('.tv-bezel').length])" % (SWEEPS, TV)}])
    assert out[-6] == 1, out                                  # the first visible build plays it
    assert json.loads(out[-1]) == [0, 1], out                 # a width change redraws the bezel and replays nothing


@needs_browser
def test_a_seat_ring_flashes_when_its_mark_arrives_or_its_count_rises_and_not_on_a_refresh(tmp_path, monkeypatch):
    """L1 W4: the SVG is rebuilt on every refresh, so a ring that flashed on every build would blink for good."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    anim = "JSON.stringify(getComputedStyle(%s.querySelector('.tv-gnode[data-node=n3] .tv-needring')).animationName)" % TV
    more = _needy()
    {n["id"]: n for n in more["nodes"]}["n3"].update(needs_open=2)
    out = _run(tmp_path, src, [*PRIME, {"motion": "no-preference"}, {"eval": _paint(_needy(), "agents")}, WAIT, {"eval": anim},
                               {"eval": _paint(_needy(), "agents")}, WAIT, {"eval": anim},
                               {"eval": _paint(more, "agents")}, WAIT, {"eval": anim}])
    assert [json.loads(out[-7]), json.loads(out[-4]), json.loads(out[-1])] == ["tv-needflash", "none", "tv-needflash"], out


@needs_browser
def test_with_the_desk_marked_no_group_label_or_conflict_chip_sits_on_the_lifted_label_or_the_callout(tmp_path, monkeypatch):
    """L1 W3: a marked desk's label rides higher and its bezel and callout take room; the placer reserves what is drawn."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("(() => { const g = %s.querySelector('.tv-graph'), bb = (e) => { const b = e.getBBox(); return [b.x, b.y, b.x + b.width, b.y + b.height]; }, "
             "hit = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5, "
             "mine = [g.querySelector('.tv-ghub .tv-lblbg'), g.querySelector('.tv-needbadge[data-needfor=hub] .tv-needpill')].map(bb), "
             "others = [...g.querySelectorAll('.tv-ggroup .tv-lblbg, .tv-confchip')].map(bb); "
             "return JSON.stringify(others.map(o => mine.some(m => hit(o, m)))); })()") % TV
    v = _needy()
    v["conflicts"] = [{"path": "f%d.py" % i, "nodes": [a, b]} for i, (a, b) in enumerate([(a, b) for k, a in enumerate(["n1", "n2", "n3", "n4", "n5"]) for b in ["n1", "n2", "n3", "n4", "n5"][k + 1:]])]
    for size in (DESKTOP, NARROW):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, WAIT, {"eval": probe}], first=size)
        got = json.loads(out[-1])
        assert got and not any(got), (size, out)


def _all_needy(per_group: int = 14) -> dict:
    """Two groups of `per_group` seats, every one marked: the pitch is at its tightest."""
    v = _needy()
    proto = v["nodes"][0]
    v["nodes"] = [dict(proto, id="d%d_%d" % (g, i), group="g%d" % g, parent=None, label="seat %d.%d" % (g, i), state=["running", "idle", "waiting"][i % 3], card=None,
                       needs_open=1 + i % 3, needs_first="np-%d-%d" % (g, i), needs_first_card="np:np-%d-%d" % (g, i), needs_first_what="q")
                  for g in (1, 2) for i in range(per_group)]
    v["tasks"], v["lost_if_closed"] = [], []
    return v


@needs_browser
def test_a_conflict_chip_never_sits_on_a_seat_badge_and_the_badges_are_drawn_after_the_chips(tmp_path, monkeypatch):
    """codex L3 r1 MED1: the chip placer knew rings and labels but not the badges, so a chip could cover a pill and win the tap."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("(() => { const g = %s.querySelector('.tv-graph'), bb = (e) => { const b = e.getBBox(); return [b.x, b.y, b.x + b.width, b.y + b.height]; }, "
             "hit = (a, b) => Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.5 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.5, "
             "pills = [...g.querySelectorAll('.tv-needpill')].map(bb), chips = [...g.querySelectorAll('.tv-confchip')].map(bb), "
             "kids = [...g.children], lastChip = Math.max(...[...g.querySelectorAll('.tv-gconf')].map(e => kids.indexOf(e))), firstOut = Math.min(...[...g.querySelectorAll('.tv-needout')].map(e => kids.indexOf(e))); "
             "return JSON.stringify([pills.length, chips.length, chips.map(c => pills.some(p => hit(c, p))), lastChip < firstOut]); })()") % TV
    v = _all_needy()
    ids = [n["id"] for n in v["nodes"]]
    v["conflicts"] = [{"path": "a-long-file-name-%d.py" % i, "nodes": [ids[i], ids[(i * 5 + 3) % len(ids)]]} for i in range(len(ids)) if ids[i] != ids[(i * 5 + 3) % len(ids)]]
    for size in (DESKTOP, NARROW):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(v, "agents")}, WAIT, {"eval": probe}], first=size)
        pills, chips, over, order = json.loads(out[-1])
        assert pills >= 28 and chips >= 10 and not any(over) and order, (size, out)


@needs_browser
def test_a_tap_in_the_overlap_of_two_pads_opens_the_card_of_the_nearer_badge(tmp_path, monkeypatch):
    """codex L3 r1 MED2: pads overlap at small pitch and the later-drawn one took the tap wherever it landed."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    # layout-independent: after the build, slide the second pad half a pad over the first (SVG units), then tap a point inside both that is nearer the first badge
    tap = ("(() => { const pads = [...%s.querySelectorAll('.tv-needhit')].filter(e => (e.dataset.needhitfor || '').startsWith('node:')); if (pads.length < 2) return JSON.stringify(null); const a = pads[0], b = pads[1], "
           "s = parseFloat(a.getAttribute('width')), ax = parseFloat(a.getAttribute('x')), ay = parseFloat(a.getAttribute('y')); "
           "b.setAttribute('width', String(s)); b.setAttribute('height', String(s)); b.setAttribute('x', String(ax + s / 2)); b.setAttribute('y', String(ay)); "
           "const svg = a.ownerSVGElement, pt = svg.createSVGPoint(); pt.x = ax + s * 0.6; pt.y = ay + s / 2; const p = pt.matrixTransform(svg.getScreenCTM()); "
           "window.__opened = null; b.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: p.x, clientY: p.y})); "
           "return JSON.stringify([a.dataset.needhitfor, b.dataset.needhitfor, window.__opened]); })()") % TV
    for size in (DESKTOP, {"emulate": {"w": 640, "h": 900, "dpr": 1, "mobile": False}}, NARROW):
        out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_all_needy(), "agents")}, WAIT, {"eval": tap}], first=size)
        got = json.loads(out[-1])
        assert got is not None, ("fewer than two pads were drawn", size, out)
        assert got[2] == "np:np-" + got[0].split(":d")[1].replace("_", "-"), (size, out)         # the tap was nearer the first pad's badge, though it landed on the later pad


@needs_browser
def test_a_lens_built_hidden_plays_its_entrance_when_first_shown(tmp_path, monkeypatch):
    """complement L3 r1 1 + glm LOW1: only a build that is attached and has a width commits what was seen; a hidden lens built before it must not use it up."""
    from tests.test_cockpit_k7_build import _sessions_fixture
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    mk = ("(() => { const R = window.LevainTopologyView.render, v = %s; window.__boxes = [0, 1].map(() => { const b = document.createElement('div'); b.style.display = 'none'; document.body.appendChild(b); "
          "b.appendChild(R(v, {lens: 'agents', openCard: () => {}})); return b; }); return 1; })()") % json.dumps(_needy())
    show = "(() => { window.__boxes[1].style.display = ''; return 1; })()"
    out = _run(tmp_path, src, [*PRIME, {"eval": mk}, WAIT, {"eval": show}, WAIT, {"eval": "window.__boxes[1].querySelectorAll('.tv-bezel.sweep').length"}], first=DESKTOP)
    assert out[-1] == 1, out
