"""Message a seat from its node (Phill 10-25, "61a"): a tap on a seat's own node opens its agent page, which holds the seat's status
rows and a message box addressed to that seat. The box is the compose panel's own verb, sent the same way the compose card sends,
with the recipient looked up in the page's view (never in a chip on the compose card). A seat the page has no recipient for says so.
Real page in headless Chrome over the real kernel, the same serve the seat-message tests use."""
from __future__ import annotations

import json

from tests.test_cockpit_f12b_topology_view import NARROW, _paint, _value
from tests.test_cockpit_flip_parity import _drive, _verb_posts, needs_browser
from tests.test_cockpit_k7_build import BUILD, DESKTOP, _sessions_fixture
from tests.test_cockpit_seatmsg import CHIPS, ENUM, PRESSED, PRIME, _serve_inbox, _tap

PAGE = "document.querySelector('#levain-modal .agent-page')"
MBOX = "document.querySelector('#levain-modal .agent-page .agent-msg')"
ADDR = "(document.querySelector('#levain-modal .agent-msg .ck-addr') || {}).textContent"
NOTE = "(document.querySelector('#levain-modal .agent-msg .agent-msg-none') || {}).textContent"
TYPE = ("(() => { const b = %s; const ta = b.querySelector('.tray-dump-ta'); ta.value = 'hello seat'; ta.dispatchEvent(new Event('input')); "
        "b.querySelector('.tray-dump-btn').click(); const y = b.querySelector('.verb-confirm'); if (y) y.click(); return 1; })()") % MBOX
MSG = "(%s.querySelector('.edit-msg') || {}).textContent" % MBOX


def _out(tmp_path, src, steps, first=DESKTOP, enum=ENUM):
    with _serve_inbox(src, enum) as (url, _httpd):
        return _drive(tmp_path, f"{url}/{BUILD}", [first, {"until": "document.querySelector('[data-ck-compose=recipient]')"}, *steps])


def _open(node):
    return [*PRIME, {"eval": _paint(_value(), "agents")}, {"eval": "window.__y = window.scrollY; 1"}, {"eval": _tap(node)}, {"until": PAGE}]


@needs_browser
def test_a_tap_on_a_seat_opens_its_page_with_a_box_addressed_to_it_and_presses_no_chip(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    probe = ("JSON.stringify([%s, %s, !!%s.querySelector('textarea.tray-dump-ta'), %s.querySelectorAll('[data-ck-compose=recipient]').length, "
             "!!%s.querySelector('dl.consent-kv'), window.scrollY === window.__y, document.activeElement && document.activeElement.closest('.ck-card') !== null])") % (
        ADDR, PRESSED, MBOX, MBOX, PAGE)
    for size in (DESKTOP, NARROW):
        got = json.loads(_out(tmp_path, src, [*_open("n3"), {"eval": probe}], first=size)["values"][-1])
        addr, pressed, ta, chips_in_box, rows, still, focus_on_card = got
        assert addr == "to seat:ledgerline" and ta is True and chips_in_box == 0 and rows is True, got   # status rows + one fixed reader
        assert pressed == ["fanin"] and still is True and focus_on_card is False, got                     # the compose card is untouched


@needs_browser
def test_the_box_sends_through_the_panels_verb_to_that_seat_and_clears_on_landing(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _out(tmp_path, src, [*_open("n3"), {"eval": TYPE}, {"until": MSG + " && " + MSG + " !== '…'"},
                               {"eval": "JSON.stringify([%s, %s.querySelector('.tray-dump-ta').value, %s.querySelector('.tray-dump-btn').disabled, %s, %s])" % (
                                   MSG, MBOX, MBOX, ADDR, PRESSED)}])
    said, text, frozen, addr, pressed = json.loads(out["values"][-1])
    assert said == "sent" and text == "" and frozen is False and addr == "to seat:ledgerline", out["values"][-1]   # the page stays, ready for the next
    assert pressed == ["fanin"], out["values"][-1]                     # the compose card's own pick is not the box's
    posts = _verb_posts(out)                                           # the kernel's preview, then the send: both the compose panel's own offer
    real = [p for p in posts if not p.get("dry_run")]
    assert all(p["panel_id"] == "inbox" and p["params"]["for"] == ["seat:ledgerline"] for p in posts) and len(real) == 1, posts
    assert real[0]["params"]["message"] == "hello seat", posts


@needs_browser
def test_the_box_does_not_need_the_seats_chip_on_the_compose_card(tmp_path, monkeypatch):
    # the defect (lane-nodetap): the chip was missing from the card at tap time, and the tap fell into a page with nothing to type into
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    drop = CHIPS + ".filter(c => c.dataset.value === 'seat:ledgerline').forEach(c => c.remove()); 1"
    out = _out(tmp_path, src, [{"eval": drop}, *_open("n3"), {"eval": "JSON.stringify([%s, !!%s.querySelector('textarea.tray-dump-ta')])" % (ADDR, MBOX)}])
    assert json.loads(out["values"][-1]) == ["to seat:ledgerline", True], out["values"]


@needs_browser
def test_a_seat_the_page_has_no_recipient_for_says_so(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _out(tmp_path, src, [*_open("n3"), {"eval": "JSON.stringify([%s, !!%s.querySelector('textarea'), %s])" % (NOTE, MBOX, PRESSED)}], enum=["cli", "fanin"])
    note, ta, pressed = json.loads(out["values"][-1])
    assert note == "can't message this seat right now" and ta is False and pressed == ["fanin"], out["values"]


@needs_browser
def test_a_lane_page_has_no_box_and_a_seat_with_no_recipient_says_why(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    for node in ("n2", "n4"):          # n2: a lane in the seat's own group; n4: the own node of group "tally", which has no id in the enum
        out = _out(tmp_path, src, [*_open(node), {"eval": "JSON.stringify([!!%s, %s])" % (MBOX, NOTE)}])
        has, note = json.loads(out["values"][-1])
        if node == "n2":
            assert has is False, (node, out["values"])
        else:
            assert has is True and note == "can't message this seat right now", (node, out["values"])


@needs_browser
def test_a_departed_seat_refusal_holds_the_box_says_so_and_never_falls_back_to_everyone(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    refuse = ("window.LevainCockpitView.refetchPanel = () => { window.__refetched = true; return Promise.resolve(false); }; const f = window.fetch; "
              "window.fetch = (u, o) => (o && o.method === 'POST' && /cockpit\\/verb/.test(String(u && u.url || u))) "
              "? Promise.resolve(new Response(JSON.stringify({error: 'no_live_seat'}), {status: 422, headers: {'content-type': 'application/json'}})) : f(u, o); 1")
    out = _out(tmp_path, src, [*_open("n3"), {"eval": refuse}, {"eval": TYPE}, {"until": "/gone/.test(%s) && %s" % (MSG, MSG)},
                               {"eval": "JSON.stringify([%s.querySelector('.tray-dump-btn').disabled, %s.querySelector('.tray-dump-ta').value, %s, %s, !!window.__refetched])" % (
                                   MBOX, MBOX, ADDR, PRESSED)}])
    frozen, draft, addr, pressed, refetched = json.loads(out["values"][-1])
    assert frozen is True and draft == "hello seat" and addr == "to seat:ledgerline", out["values"][-2:]
    assert pressed == ["fanin"] and refetched is False, out["values"][-2:]     # the compose card's recovery is the card's, not the box's


# --- the signed path, end to end over the sign card's rig (a virtual passkey, a real Signer, the door that applies only what was signed)

from tests.test_cockpit_sign_card import TOK, _Rig, needs_ssh  # noqa: E402
import tests.test_cockpit_sign_card as _sign  # noqa: E402


def _seat_card(enum):
    return {"verb": "send_inbox", "fields": [{"name": "message", "type": "text", "label": "Message"},
                                             {"name": "for", "type": "recipient", "label": "To", "enum": ["cli", "fanin", "seat:ledgerline"],
                                              "default": ["fanin"], "transient_prefix": "seat:", "gone_match": "no_live_seat"}]}


@needs_browser
@needs_ssh
def test_the_box_on_a_signed_verb_signs_through_the_passkey_and_the_door_applies_it_to_the_seat(tmp_path, monkeypatch):
    monkeypatch.setattr(_sign, "_compose_card", _seat_card)
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving() as (_url, _httpd):
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, rig.authenticator(),
            {"until": "document.querySelector('[data-ck-compose=recipient]') && document.getElementById('ck-hlens')", "ms": 30000},
            {"eval": _paint(_value(), "agents")}, {"eval": _tap("n3")}, {"until": MBOX + " && 1"},
            # a cancelled passkey: nothing signed, nothing posted, the draft kept in the box
            {"user_verified": False}, {"eval": TYPE},
            {"until": "/did not sign|nothing was signed/.test(%s) && %s" % (MSG, MSG), "ms": 30000},
            {"eval": "%s.querySelector('.tray-dump-ta').value" % MBOX},
            # verified: the request filed for these words is signed and the verb posts with its id
            {"user_verified": True},
            {"eval": TYPE},
            {"until": "%s === 'sent' && 1" % MSG, "ms": 30000},
            {"eval": "JSON.stringify([%s.querySelector('.tray-dump-ta').value, %s])" % (MBOX, ADDR)},
        ])
    v = out["values"]
    i = next(k for k, x in enumerate(v) if isinstance(x, str) and "nothing was signed" in x)
    assert "nothing was sent" in v[i] and v[i + 1] == "hello seat", v       # the cancelled passkey kept the words
    assert json.loads(v[-1]) == ["", "to seat:ledgerline"], v
    assert rig.notes() == ["hello seat"] and rig.refused == [], (rig.notes(), rig.refused)
    # every reach of the door carried exactly the signed words, the seat and the request (read as test_cockpit_sign_card reads it); one note landed
    assert rig.applied and all(a == {"message": "hello seat", "for": ["seat:ledgerline"], "sign_request": "np-compose-0001"} for a in rig.applied), rig.applied
    assert [c[0] for c in rig.composed] == ["send_inbox"], rig.composed      # one draft, one request, filed once across the retry
    assert rig.verifies("np-compose-0001")
