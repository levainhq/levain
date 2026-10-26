"""The inbox compose card's recipient chips (Phill 10-23, "2a"): the desk (`fanin`) pressed by default; at a phone width the chips are a
picker holding the same values. Since Phill 10-25 ("61a") a seat is messaged from its node's page (tests/test_cockpit_nodecompose.py): the
card draws no seat chip (an id under the provider's declared `transient_prefix`), and a lane's or an ambiguous label's page has no box. Real page in headless Chrome over the real kernel: the inbox panel declares a typed compose (the roster's chips are its enum),
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

DECLARED = {"default": ["fanin"], "transient_prefix": "seat:", "gone_match": "no_live_seat"}     # this provider's own words, declared on its field


def _compose(enum, declared=DECLARED):
    return {"verb": "send_inbox", "fields": [{"name": "message", "type": "text", "label": "Message"},
                                             {"name": "for", "type": "recipient", "label": "To (none = everyone)", "enum": enum, **declared}]}

CHIPS = "[...document.querySelectorAll('[data-ck-compose=recipient]')]"
PRESSED = CHIPS + ".filter(c => c.getAttribute('aria-pressed') === 'true').map(c => c.dataset.value)"
PICK = "document.querySelector('[data-ck-compose=recipient-pick]')"
PRIME = [{"eval": "document.querySelector('#ck-hlens [data-tv=agents]').click(); 1"}, {"until": TV + ".dataset.lens == 'agents'"}]


def _message_effect(params, *, origin_key):
    return send_inbox_effect({"body": params.get("message"), "for": params.get("for") or "cli"}, origin_key=origin_key)


def _serve_inbox(src, enum=ENUM, handler=None, declared=DECLARED):
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec(id="inbox", kind="list", title="Inbox", priority="feed", order="time.desc", version_fields=("id",),
                             facets=frozenset({"source", "for", "at"}), verbs=("send_inbox",), compose=_compose(enum, declared), read=lambda c: Read(rows=(RowIn("a", "hello", facets={"source": "bridge", "for": "cli", "at": "2026-10-23T11:00:00+00:00"}, stored={"id": "a"}),))))
    verb = ActionVerb(handler=handler or (lambda p: {"summary": "sent"}), label="Send to inbox", fields=("message", "for"))
    return _serve(src, cockpit=ck, extra_verbs={"send_inbox": verb}, extra_effects={"send_inbox": DownstreamEffect(FakeInbox(), _message_effect)}, surface_tokens={"browser": "T"}, install_class="reported")


def _run(tmp_path, src, steps, first=DESKTOP, page=BUILD, enum=ENUM, handler=None, declared=DECLARED):
    with _serve_inbox(src, enum, handler, declared) as (url, _httpd):
        return _drive(tmp_path, f"{url}/{page}", [first, {"until": "document.querySelector('[data-ck-compose=recipient]')"}, *steps])["values"]


def _tap(node: str) -> str:
    return "%s.querySelector('.tv-gnode[data-node=%s]').dispatchEvent(new MouseEvent('click', {bubbles: true})); 1" % (TV, node)


CARD = ["cli", "fanin"]                              # the enum less its seat: what the card draws
NODE_BOX = "document.querySelector('#levain-modal .agent-msg')"


@needs_browser
def test_the_desk_is_pressed_by_default_and_unpressing_it_leaves_none(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    addr = "(document.querySelector('.ck-addr') || {}).textContent"
    out = _run(tmp_path, src, [{"eval": "JSON.stringify([%s, %s, %s])" % (PRESSED, CHIPS + ".map(c => c.dataset.value)", addr)},
                               {"eval": CHIPS + ".find(c => c.dataset.value === 'fanin').click(); JSON.stringify([%s, %s])" % (PRESSED, addr)}], page="#write_token=T")
    pressed, values, text = json.loads(out[-2])
    assert pressed == ["fanin"] and values == CARD and text == "to fanin", out
    pressed, text = json.loads(out[-1])
    assert pressed == [] and text == "to everyone", out                        # none pressed still means everyone


@needs_browser
def test_a_phone_width_holds_the_same_values_in_a_picker_that_agrees_with_the_chips(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify({w: window.innerWidth, opts: [...%s.options].filter(o => !o.disabled).map(o => o.value), sel: %s.value, "
             "pickShown: getComputedStyle(%s).display !== 'none', chipsShown: %s.some(c => getComputedStyle(c).display !== 'none')})") % (PICK, PICK, PICK, CHIPS)
    choose = "%s.value = 'cli'; %s.dispatchEvent(new Event('change', {bubbles: true})); JSON.stringify([%s, %s.value])" % (PICK, PICK, PRESSED, PICK)
    every = "%s.value = ''; %s.dispatchEvent(new Event('change', {bubbles: true})); %s" % (PICK, PICK, PRESSED)
    out = _run(tmp_path, src, [{"eval": probe}, {"eval": choose}, {"eval": every}], first=NARROW, page="#write_token=T")
    got = json.loads(out[-3])
    assert got["w"] < 720 and got["opts"] == ["", *CARD] and got["sel"] == "fanin", got          # "everyone" first, the desk chosen
    assert got["pickShown"] is True and got["chipsShown"] is False, got
    assert json.loads(out[-2]) == [["cli"], "cli"], out                                         # the picker presses the chip
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
def test_a_landed_send_rebuilds_the_box_on_the_desk_not_on_the_reader_it_just_used(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    pick = "%s.value = 'cli'; %s.dispatchEvent(new Event('change', {bubbles: true})); 1" % (PICK, PICK)
    out = _run(tmp_path, src, [{"eval": pick}, {"until": "JSON.stringify(%s) == '[\"cli\"]'" % PRESSED},
                               {"eval": "window.__old = %s; 1" % BOX}, {"eval": SEND},
                               {"until": "%s !== window.__old" % BOX},          # the first fresh box after the send landed, read at once
                               {"eval": "JSON.stringify(%s)" % PRESSED}])
    assert json.loads(out[-1]) == ["fanin"], out


@needs_browser
def test_a_lane_in_the_seats_own_group_keeps_its_page_and_presses_nothing(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    state = "JSON.stringify([%s, !!document.getElementById('levain-modal'), !!%s])" % (PRESSED, NODE_BOX)
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": _tap("n2")}, {"until": "document.getElementById('levain-modal')"}, {"eval": state}])
    pressed, modal, box = json.loads(out[-1])      # n2 is a lane: group "ledgerline" (the seat's own), parent n1
    assert pressed == ["fanin"] and modal is True and box is False, out


@needs_browser
def test_a_label_shared_by_two_groups_names_no_seat(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    value = _value()
    value["groups"][1]["label"] = "ledgerline"          # two groups now carry the seat's name
    out = _run(tmp_path, src, [*PRIME, {"eval": _paint(value, "agents")}, {"eval": _tap("n3")}, {"until": "document.getElementById('levain-modal')"},
                               {"eval": "JSON.stringify([%s, !!%s])" % (PRESSED, NODE_BOX)}])
    assert json.loads(out[-1]) == [["fanin"], False], out


@needs_browser
def test_a_roster_id_equal_to_the_synthetic_option_value_picks_one_reader(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    choose = ("(() => { const p = %s; p.selectedIndex = [...p.options].findIndex(o => o.value === '*several' && !o.disabled); "
              "p.dispatchEvent(new Event('change', {bubbles: true})); return JSON.stringify(%s); })()") % (PICK, PRESSED)
    out = _run(tmp_path, src, [{"eval": choose}], first=NARROW, page="#write_token=T", enum=["cli", "fanin", "*several"])
    assert json.loads(out[-1]) == ["*several"], out        # one reader, never everyone


@needs_browser
def test_the_page_presses_only_what_the_provider_declares_and_hides_only_its_declared_seats(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    none = {}          # a provider that declares nothing: the page has no default reader of its own
    out = _run(tmp_path, src, [{"eval": "JSON.stringify(%s)" % PRESSED}], page="#write_token=T", declared=none)
    assert json.loads(out[-1]) == [], out                                      # "fanin" is in the enum and is not pressed
    out = _run(tmp_path, src, [{"eval": "JSON.stringify(%s)" % PRESSED}], page="#write_token=T", declared={"default": ["x", "cli"]}, enum=["cli", "x"])
    assert json.loads(out[-1]) == ["cli", "x"], out                            # the declared default, as far as it is in the enum
    # no declared prefix: every id is a chip, "seat:ledgerline" included
    out = _run(tmp_path, src, [{"eval": CHIPS + ".map(c => c.dataset.value)"}], page="#write_token=T", declared={})
    assert out[-1] == ENUM, out
