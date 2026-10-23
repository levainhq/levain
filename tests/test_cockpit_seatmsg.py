"""Message a seat from the cockpit (Phill 10-23, "2a"): a tap on a seat in the agents graph presses that seat's chip on the inbox compose
card and focuses its message box; the desk (`fanin`) is pressed by default; at a phone width the chips are a picker holding the same
values. Real page in headless Chrome over the real kernel: the inbox panel declares a typed compose (the roster's chips are its enum),
and a literal topology value is painted into the header the way the Agents-lens tests do."""
from __future__ import annotations

import json

from levain.cockpit import ProviderSpec, Read, RowIn
from levain.cockpit.providers import build_default_cockpit
from levain.writes import ActionVerb
from levain.cockpit.effects import DownstreamEffect
from tests._inbox_fake import FakeInbox, send_inbox_effect
from tests.test_cockpit_f12b_topology_view import NARROW, TV, _paint, _value
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _serve
from tests.test_cockpit_k7_build import BUILD, DESKTOP, _sessions_fixture

ENUM = ["cli", "fanin", "seat:ledgerline"]          # the group label of n1..n3 is "ledgerline"; "tally" (n4, n5) is a lane: not in the enum

def _compose(enum):
    return {"verb": "send_inbox", "fields": [{"name": "message", "type": "text", "label": "Message"},
                                             {"name": "for", "type": "recipient", "label": "To (none = everyone)", "enum": enum}]}

CHIPS = "[...document.querySelectorAll('[data-ck-compose=recipient]')]"
PRESSED = CHIPS + ".filter(c => c.getAttribute('aria-pressed') === 'true').map(c => c.dataset.value)"
PICK = "document.querySelector('[data-ck-compose=recipient-pick]')"
PRIME = [{"eval": "document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"}, {"until": TV + ".dataset.lens == 'agents'"}]


def _message_effect(params, *, origin_key):
    return send_inbox_effect({"body": params.get("message"), "for": params.get("for") or "cli"}, origin_key=origin_key)


def _serve_inbox(src, enum=ENUM, handler=None):
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec(id="inbox", kind="list", title="Inbox", priority="feed", order="time.desc", version_fields=("id",),
                             facets=frozenset({"source", "for", "at"}), verbs=("send_inbox",), compose=_compose(enum), read=lambda c: Read(rows=(RowIn("a", "hello", facets={"source": "bridge", "for": "cli", "at": "2026-10-23T11:00:00+00:00"}, stored={"id": "a"}),))))
    verb = ActionVerb(handler=handler or (lambda p: {"summary": "sent"}), label="Send to inbox", fields=("message", "for"))
    return _serve(src, cockpit=ck, extra_verbs={"send_inbox": verb}, extra_effects={"send_inbox": DownstreamEffect(FakeInbox(), _message_effect)}, surface_tokens={"browser": "T"}, install_class="reported")


def _run(tmp_path, src, steps, first=DESKTOP, page=BUILD, enum=ENUM, handler=None):
    with _serve_inbox(src, enum, handler) as (url, _httpd):
        return _drive(tmp_path, f"{url}/{page}", [first, {"until": "document.querySelector('[data-ck-compose=recipient]')"}, *steps])["values"]


def _tap(node: str) -> str:
    return "%s.querySelector('.tv-gnode[data-node=%s]').dispatchEvent(new MouseEvent('click', {bubbles: true})); 1" % (TV, node)


@needs_browser
def test_the_desk_is_pressed_by_default_and_unpressing_it_leaves_none(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    addr = "(document.querySelector('.ck-addr') || {}).textContent"
    out = _run(tmp_path, src, [{"eval": "JSON.stringify([%s, %s, %s])" % (PRESSED, CHIPS + ".map(c => c.dataset.value)", addr)},
                               {"eval": CHIPS + ".find(c => c.dataset.value === 'fanin').click(); JSON.stringify([%s, %s])" % (PRESSED, addr)}], page="#write_token=T")
    pressed, values, text = json.loads(out[-2])
    assert pressed == ["fanin"] and values == ENUM and text == "to fanin", out
    pressed, text = json.loads(out[-1])
    assert pressed == [] and text == "to everyone", out                        # none pressed still means everyone


@needs_browser
def test_a_tap_on_a_seat_presses_its_chip_and_focuses_the_box_but_a_lane_keeps_its_page(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    seat = _tap("n3")      # n3 is in group "ledgerline": chip "seat:ledgerline" is in the enum
    lane = _tap("n4")      # n4 is in group "tally": "seat:tally" is not (a lane)
    state = ("JSON.stringify([%s, document.activeElement && document.activeElement.className, document.body.dataset.mode, "
             "!!document.getElementById('levain-modal'), (document.querySelector('[data-ck-compose=recipient]').closest('[data-ck-panel]') || {dataset: {}}).dataset.modes])") % PRESSED
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": lane}, {"eval": state},
                               {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
                               {"eval": seat},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && " + state}])   # the box lands a frame later, after the tab change redraws it
    pressed, _focus, _mode, modal, _modes = json.loads(out[-4])
    assert pressed == ["fanin"] and modal is True, out                         # the lane: the desk stays pressed, its agent page opens
    pressed, focus, mode, modal, modes = json.loads(out[-1])
    assert pressed == ["seat:ledgerline"] and "tray-dump-ta" in focus and mode == "board" and modal is False, out


@needs_browser
def test_a_phone_width_holds_the_same_values_in_a_picker_that_agrees_with_the_chips(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({w: window.innerWidth, opts: [...%s.options].filter(o => !o.disabled).map(o => o.value), sel: %s.value, "
             "pickShown: getComputedStyle(%s).display !== 'none', chipsShown: %s.some(c => getComputedStyle(c).display !== 'none')})") % (PICK, PICK, PICK, CHIPS)
    choose = "%s.value = 'seat:ledgerline'; %s.dispatchEvent(new Event('change', {bubbles: true})); JSON.stringify([%s, %s.value])" % (PICK, PICK, PRESSED, PICK)
    every = "%s.value = ''; %s.dispatchEvent(new Event('change', {bubbles: true})); %s" % (PICK, PICK, PRESSED)
    out = _run(tmp_path, src, [{"eval": probe}, {"eval": choose}, {"eval": every}], first=NARROW, page="#write_token=T")
    got = json.loads(out[-3])
    assert got["w"] < 720 and got["opts"] == ["", *ENUM] and got["sel"] == "fanin", got          # "everyone" first, the desk chosen
    assert got["pickShown"] is True and got["chipsShown"] is False, got
    assert json.loads(out[-2]) == [["seat:ledgerline"], "seat:ledgerline"], out                   # the picker presses the chip
    assert out[-1] == [], out                                                          # "everyone" presses none


@needs_browser
def test_a_wide_page_shows_the_chips_and_not_the_picker(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = "JSON.stringify([getComputedStyle(%s).display, %s.every(c => getComputedStyle(c).display !== 'none'), window.innerWidth])" % (PICK, CHIPS)
    out = _run(tmp_path, src, [{"eval": probe}], page="#write_token=T")
    none, chips, width = json.loads(out[-1])
    assert none == "none" and chips is True and width >= 1000, out


BOX = "document.querySelector('[data-ck-compose=recipient]').closest('.tray-dump')"
SEND = ("(() => { const b = %s; b.querySelector('.tray-dump-ta').value = 'hi'; b.querySelector('.tray-dump-btn').click(); "
        "const y = document.querySelector('.verb-confirm'); if (y) y.click(); return 1; })()") % BOX


@needs_browser
def test_a_landed_send_rebuilds_the_box_on_the_desk_not_on_the_seat_it_just_used(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": "window.__old = %s; 1" % BOX}, {"eval": SEND},
                               {"until": "%s !== window.__old" % BOX},          # the first fresh box after the send landed, read at once
                               {"eval": "JSON.stringify(%s)" % PRESSED}])
    assert json.loads(out[-1]) == ["fanin"], out


@needs_browser
def test_a_lane_in_the_seats_own_group_keeps_its_page_and_presses_nothing(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    state = "JSON.stringify([%s, !!document.getElementById('levain-modal')])" % PRESSED
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": _tap("n2")}, {"until": "document.getElementById('levain-modal')"}, {"eval": state}])
    pressed, modal = json.loads(out[-1])      # n2 is a lane: group "ledgerline" (the seat's own), parent n1
    assert pressed == ["fanin"] and modal is True, out


@needs_browser
def test_a_label_shared_by_two_groups_names_no_seat(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    value = _value()
    value["groups"][1]["label"] = "ledgerline"          # two groups now carry the seat's name
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(value, "agents")}, {"eval": _tap("n3")}, {"until": "document.getElementById('levain-modal')"},
                               {"eval": "JSON.stringify(%s)" % PRESSED}])
    assert json.loads(out[-1]) == ["fanin"], out


@needs_browser
def test_a_roster_id_equal_to_the_synthetic_option_value_picks_one_reader(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    choose = ("(() => { const p = %s; p.selectedIndex = [...p.options].findIndex(o => o.value === '*several' && !o.disabled); "
              "p.dispatchEvent(new Event('change', {bubbles: true})); return JSON.stringify(%s); })()") % (PICK, PRESSED)
    out = _run(tmp_path, src, [{"eval": choose}], first=NARROW, page="#write_token=T", enum=["cli", "fanin", "*several"])
    assert json.loads(out[-1]) == ["*several"], out        # one reader, never everyone


@needs_browser
def test_a_departed_seat_refusal_unpresses_its_chip_and_keeps_the_draft_when_the_refetch_declines(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)

    # the kernel's own words for a refused write reach the page rewritten (fault() in the boot layer keeps the CODE and swaps the message):
    # answer every write with the refusal code, and the refetch with "declined"
    stub = ("window.LevainCockpitView.refetchPanel = () => Promise.resolve(false); const f = window.fetch; "
            "window.fetch = (u, o) => (o && o.method === 'POST' && /cockpit\\/verb/.test(String(u && u.url || u))) "
            "? Promise.resolve(new Response(JSON.stringify({error: 'no_live_seat'}), {status: 422, headers: {'content-type': 'application/json'}})) : f(u, o); 1")
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": stub}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": SEND},
                               {"until": "(%s.querySelector('.edit-msg') || {}).textContent == 'that seat is gone, choose again'" % BOX},
                               {"eval": "JSON.stringify([%s, %s.querySelector('.tray-dump-ta').value])" % (PRESSED, BOX)}])
    pressed, draft = json.loads(out[-1])
    assert pressed == [] and draft == "hi", out


@needs_browser
def test_a_frozen_box_takes_no_pick_from_the_graph(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("(() => { const b = %s; b.querySelector('.tray-dump-btn').disabled = true; "
             "return JSON.stringify([b.ckPick(['seat:ledgerline']), window.LevainDashboard.messageTo('seat:ledgerline'), %s]); })()") % (BOX, PRESSED)
    out = _run(tmp_path, src, [{"eval": probe}], page="#write_token=T")
    assert json.loads(out[-1]) == [False, False, ["fanin"]], out


# the departed-seat refusal as the kernel's words reach the page, and a refetch the test holds open or answers as it likes
REFUSE = ("const f = window.fetch; window.fetch = (u, o) => (o && o.method === 'POST' && /cockpit\\/verb/.test(String(u && u.url || u))) "
          "? Promise.resolve(new Response(JSON.stringify({error: 'no_live_seat'}), {status: 422, headers: {'content-type': 'application/json'}})) : f(u, o); ")
TWO = ["cli", "fanin", "seat:ledgerline", "seat:other"]
SAY = "(%s.querySelector('.edit-msg') || {}).textContent" % BOX
GONE = "that seat is gone, choose again"


@needs_browser
def test_the_box_stays_frozen_while_the_departed_seat_recovery_waits_and_clears_only_the_failed_sends_seats(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    hold = "window.LevainCockpitView.refetchPanel = () => new Promise((r) => { window.__rel = r; }); " + REFUSE + "1"
    during = ("JSON.stringify([%s.querySelector('.tray-dump-btn').disabled, %s.every(c => c.disabled), %s.disabled, %s.ckPick(['seat:other']), %s])"
              % (BOX, CHIPS, PICK, BOX, PRESSED))
    # a seat that was not in the failed send's snapshot (pressed behind the frozen box's back) must outlive the recovery
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": hold}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": SEND}, {"until": "window.__rel"},
                               {"eval": during},
                               {"eval": CHIPS + ".find(c => c.dataset.value === 'seat:other').setAttribute('aria-pressed', 'true'); window.__rel(false); 1"},
                               {"until": SAY + " == '%s'" % GONE},
                               {"eval": "JSON.stringify([%s, %s.querySelector('.tray-dump-btn').disabled, %s.querySelector('.tray-dump-ta').value])" % (PRESSED, BOX, BOX)}],
               enum=TWO)
    btn, chips, pick, took, pressed = json.loads(out[-4])
    assert btn is True and chips is True and pick is True and took is False and pressed == ["seat:ledgerline"], out     # frozen, and a pick is refused
    pressed, btn, draft = json.loads(out[-1])
    assert pressed == ["seat:other"] and btn is False and draft == "hi", out


@needs_browser
def test_an_applied_refetch_that_did_not_rebuild_the_box_still_unpresses_the_dead_seat(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    stub = "window.LevainCockpitView.refetchPanel = () => Promise.resolve('applied'); " + REFUSE + "1"
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": stub}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": SEND}, {"until": SAY + " == '%s'" % GONE},
                               {"eval": "JSON.stringify(%s)" % PRESSED}])
    assert json.loads(out[-1]) == [], out


@needs_browser
def test_a_recovery_a_later_send_overtook_says_nothing_on_the_fresh_box(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    # the refusal's recovery is held; a later send (the box is unfrozen by hand, as a poll rebuild would hand over a live one) lands and
    # rebuilds the board, then the held recovery answers
    hold = ("window.__f0 = window.fetch; window.LevainCockpitView.refetchPanel = () => new Promise((r) => { window.__rel = r; }); " + REFUSE + "1")
    heal = "window.fetch = window.__f0; %s.querySelector('.tray-dump-btn').disabled = false; 1" % BOX
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": hold}, {"eval": _tap("n3")},
                               {"until": "document.activeElement.classList.contains('tray-dump-ta') && JSON.stringify(%s) == '[\"seat:ledgerline\"]'" % PRESSED},
                               {"eval": SEND}, {"until": "window.__rel"},
                               {"eval": "window.__old = %s; %s" % (BOX, heal)},
                               {"eval": SEND}, {"until": "%s !== window.__old" % BOX},
                               {"eval": "window.__rel(false); 1"},
                               {"eval": "new Promise((r) => setTimeout(() => r(%s), 300))" % SAY}])
    assert out[-1] != GONE, out
