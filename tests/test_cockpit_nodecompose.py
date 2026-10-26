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
    # (the card no longer draws seat chips at all; this removes any that a provider-less enum would still draw)
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
    assert pressed == ["fanin"] and refetched is True, out["values"][-2:]      # the box holds, and the panel is asked for the roster as it is now



@needs_browser
def test_the_compose_card_offers_no_seat_and_the_seats_page_does(tmp_path, monkeypatch):
    # "the separate card stays only for messages to the desk or everyone" (the option Phill chose, 61a)
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    pick = "[...document.querySelector('[data-ck-compose=recipient-pick]').options].filter(o => !o.disabled).map(o => o.value)"
    for size in (DESKTOP, NARROW):
        out = _out(tmp_path, src, [{"eval": "JSON.stringify([%s, %s])" % (CHIPS + ".map(c => c.dataset.value)", pick)}, *_open("n3"), {"eval": ADDR}], first=size)
        chips, opts = next(json.loads(x) for x in out["values"] if isinstance(x, str) and x.startswith("[["))
        assert chips == ["cli", "fanin"] and opts == ["", "cli", "fanin"], out["values"]
        assert out["values"][-1] == "to seat:ledgerline", out["values"]

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


CARD_BOX = "document.querySelector('[data-ck-compose=recipient]').closest('.tray-dump')"
CARD_SEND = ("(() => { const b = %s; b.querySelector('.tray-dump-ta').value = 'hi'; b.querySelector('.tray-dump-btn').click(); "
             "const y = document.querySelector('.verb-confirm'); if (y) y.click(); return 1; })()") % CARD_BOX
CARD_MSG = "(%s.querySelector('.edit-msg') || {}).textContent" % CARD_BOX


@needs_browser
def test_a_card_whose_declared_default_is_a_seat_fails_closed_and_posts_nothing(tmp_path, monkeypatch):
    # N1 (codex HIGH): the card draws no seat chip, so a default naming a seat left nothing pressed = an everyone-send
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    declared = {"default": ["seat:ledgerline"], "transient_prefix": "seat:", "gone_match": "no_live_seat"}
    with _serve_inbox(src, ENUM, None, declared) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [DESKTOP, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                {"eval": CARD_SEND}, {"eval": "JSON.stringify([%s, %s])" % (CARD_MSG, PRESSED)}])
    said, pressed = json.loads(out["values"][-1])
    assert _verb_posts(out) == [] and pressed == [] and said and "seat" in said and "nothing was sent" in said.lower(), out["values"][-1]


@needs_browser
def test_a_card_with_a_seat_default_still_sends_to_a_reader_the_person_presses(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    declared = {"default": ["seat:ledgerline"], "transient_prefix": "seat:", "gone_match": "no_live_seat"}
    press = CHIPS + ".find(c => c.dataset.value === 'cli').click(); 1"
    with _serve_inbox(src, ENUM, None, declared) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [DESKTOP, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                {"eval": press}, {"eval": "window.__old = %s; 1" % CARD_BOX}, {"eval": CARD_SEND}, {"until": "%s !== window.__old" % CARD_BOX}])
    real = [p for p in _verb_posts(out) if not p.get("dry_run")]
    assert len(real) == 1 and real[0]["params"]["for"] == ["cli"], _verb_posts(out)


@needs_browser
def test_a_sent_stays_until_the_next_keystroke(tmp_path, monkeypatch):
    # N6: "sent" must not sit beside a new, unsent draft
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    out = _out(tmp_path, src, [*_open("n3"), {"eval": TYPE}, {"until": MSG + " === 'sent'"},
                               {"eval": "(() => { const ta = %s.querySelector('.tray-dump-ta'); ta.value = 'x'; ta.dispatchEvent(new Event('input', {bubbles: true})); return %s; })()" % (MBOX, MSG)}])
    assert out["values"][-1] == "", out["values"][-2:]


@needs_browser
def test_a_form_panel_declaring_a_seat_is_never_the_seats_box(tmp_path, monkeypatch):
    # N2: a panel's action that is not the compose card (a form with a recipient field naming the seat) is not the seat's message box
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spy = ("window.__orig = window.LevainDashboard.render; window.LevainDashboard.render = (v, o) => { window.__v = v; window.__o = o; return window.__orig(v, o); }; 1")
    notcard = "const v = window.__v; for (const d of Object.values(v.extra_panels)) { if (d && d.action) d.action.card = false; } window.__orig(v, window.__o); 1"
    reopen = "document.querySelector('#levain-modal .modal-close').click(); 1"
    out = _out(tmp_path, src, [{"eval": spy}, {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, {"until": "window.__v"},
                               {"eval": notcard}, *_open("n3"), {"eval": "JSON.stringify([!!%s, !!%s.querySelector('textarea'), %s])" % (MBOX, MBOX, NOTE)}])
    has, ta, note = json.loads(out["values"][-1])
    assert has is True and ta is False and note == "can't message this seat right now", out["values"][-1]


@needs_browser
def test_a_page_that_turned_read_only_drops_the_send_with_words_not_a_type_error(tmp_path, monkeypatch):
    # N4: a page opened writable whose view is then rendered read-only (no commitAction) must not throw on Send
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spy = ("window.__orig = window.LevainDashboard.render; window.LevainDashboard.render = (v, o) => { window.__v = v; return window.__orig(v, o); }; "
           "window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); "
           "window.addEventListener('unhandledrejection', e => window.__errs.push(String(e.reason))); 1")
    refresh = "document.dispatchEvent(new Event('visibilitychange')); 1"
    readonly = "window.__orig(window.__v, {}); 1"
    probe = ("(() => { const b = %s; const ta = b && b.querySelector('.tray-dump-ta'); if (ta) { ta.value = 'x'; ta.dispatchEvent(new Event('input')); "
             "b.querySelector('.tray-dump-btn').click(); const y = b.querySelector('.verb-confirm'); if (y) y.click(); } "
             "return JSON.stringify([!!b, !!ta, (b && b.querySelector('.edit-msg') || {}).textContent, window.__errs]); })()") % MBOX
    out = _out(tmp_path, src, [*_open("n3"), {"eval": spy}, {"eval": refresh}, {"until": "window.__v"}, {"eval": readonly}, {"until": "1", "ms": 500}, {"eval": probe}])
    got, has_ta, said, errs = json.loads(out["values"][-1])
    posts = [p for p in _verb_posts(out) if not p.get("dry_run")]
    assert not [e for e in errs if "ResizeObserver" not in e] and posts == [] and (not has_ta or (said and "nothing was sent" in said)), out["values"][-1]


SEAT_DEFAULT = {"default": ["seat:ledgerline"], "transient_prefix": "seat:", "gone_match": "no_live_seat"}
PICK = "document.querySelector('[data-ck-compose=recipient-pick]')"


@needs_browser
def test_an_everyone_the_person_chose_on_a_seat_default_card_is_sent(tmp_path, monkeypatch):
    # R1 (codex MED, fix r1 verify): only the untouched seat default is refused; a reader pressed and let go is a choice (the picker: below)
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    by_chip = CHIPS + ".find(c => c.dataset.value === 'cli').click(); " + CHIPS + ".find(c => c.dataset.value === 'cli').click(); 1"
    with _serve_inbox(src, ENUM, None, SEAT_DEFAULT) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [DESKTOP, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                {"eval": by_chip}, {"eval": "window.__old = %s; 1" % CARD_BOX}, {"eval": CARD_SEND},
                                                                {"until": "%s !== window.__old" % CARD_BOX}])
    real = [p for p in _verb_posts(out) if not p.get("dry_run")]
    assert len(real) == 1 and not real[0]["params"].get("for"), (_verb_posts(out), out["values"])


@needs_browser
def test_everyone_picked_on_the_phone_picker_of_a_seat_default_card_is_a_choice_and_is_sent(tmp_path, monkeypatch):
    # V1 (codex R1 OPEN, fix r2 verify): on the phone's picker an untouched seat-default card must not already show "everyone", or
    # picking "everyone" moves nothing, fires no change, and the send stays refused. The picker is driven as a person drives it: focused,
    # then a typed letter moves its selection (a trusted key press; nothing here sets the value or dispatches the change).
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    shown = ("(() => { const p = %s; return JSON.stringify([p.options[p.selectedIndex].textContent, getComputedStyle(p).display, "
             "document.activeElement === p]); })()") % PICK
    with _serve_inbox(src, ENUM, None, SEAT_DEFAULT) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [NARROW, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                {"focus": True}, {"eval": shown}, {"eval": PICK + ".focus(); 1"}, {"key": "e"},
                                                                {"eval": shown}, {"eval": "window.__old = %s; 1" % CARD_BOX}, {"eval": CARD_SEND},
                                                                {"until": "%s !== window.__old" % CARD_BOX, "ms": 8000, "may_timeout": True}])
    before, after = json.loads(out["values"][1]), json.loads(out["values"][3])
    real = [p for p in _verb_posts(out) if not p.get("dry_run")]
    assert len(real) == 1 and not real[0]["params"].get("for"), (_verb_posts(out), out["values"])
    assert before[0] == "choose a reader" and before[1] != "none", out["values"]
    assert after == ["everyone", before[1], True], out["values"]


@needs_browser
def test_a_rebuild_keeps_the_persons_own_everyone_and_keeps_asking_an_untouched_card(tmp_path, monkeypatch):
    # V2 (deepseek MED 2+3, fix r2 verify): "the person chose everyone" is its own record, made only by their pick, so it survives a rebuild
    # of the box (a refetch), and a rebuild of a card nobody touched still asks for a reader.
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    by_chip = CHIPS + ".find(c => c.dataset.value === 'cli').click(); " + CHIPS + ".find(c => c.dataset.value === 'cli').click(); 1"
    rebuild = "window.__old = %s; document.dispatchEvent(new Event('visibilitychange')); 1" % CARD_BOX
    shown = "JSON.stringify([%s, (p => p.options[p.selectedIndex].textContent)(%s)])" % (CARD_MSG, PICK)
    for touch, want_msg, want_pick, sends in ((by_chip, "", "everyone", 1), ("1", "choose a reader", "choose a reader", 0)):
        with _serve_inbox(src, ENUM, None, SEAT_DEFAULT) as (url, _httpd):
            out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [DESKTOP, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                    {"eval": touch}, {"eval": rebuild}, {"until": "%s !== window.__old" % CARD_BOX},
                                                                    {"eval": shown}, {"eval": "window.__old = %s; 1" % CARD_BOX}, {"eval": CARD_SEND},
                                                                    {"until": "%s !== window.__old" % CARD_BOX, "ms": 5000, "may_timeout": True}])
        msg, pick = json.loads(out["values"][4])
        assert (want_msg in msg if want_msg else msg == "") and pick == want_pick, (touch, out["values"])
        real = [p for p in _verb_posts(out) if not p.get("dry_run")]
        assert len(real) == sends and all(not p["params"].get("for") for p in real), (touch, _verb_posts(out), out["values"])


@needs_browser
def test_the_seat_default_words_ask_for_a_reader_and_go_once_one_is_chosen(tmp_path, monkeypatch):
    # R3 (deepseek MED 2 + LOW 6): before any send the words ask for a reader (nothing was attempted); a refused send says nothing
    # was sent; a reader pressed afterwards clears them
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    press = CHIPS + ".find(c => c.dataset.value === 'cli').click(); " + CARD_MSG
    with _serve_inbox(src, ENUM, None, SEAT_DEFAULT) as (url, _httpd):
        out = _drive(tmp_path, f"{url}/{BUILD}#write_token=T", [DESKTOP, {"until": "document.querySelector('[data-ck-compose=recipient]')"},
                                                                {"eval": CARD_MSG}, {"eval": CARD_SEND}, {"eval": CARD_MSG}, {"eval": press}])
    first, refused, after = out["values"][-4], out["values"][-2], out["values"][-1]
    assert "choose a reader" in first and "nothing was sent" not in first.lower(), out["values"]
    assert "nothing was sent" in refused.lower() and after == "", out["values"]
    assert _verb_posts(out) == [], _verb_posts(out)


@needs_browser
def test_a_card_box_that_outlived_a_read_only_render_says_so_on_click_and_on_enter(tmp_path, monkeypatch):
    # R2 (deepseek MED 1 + LOW 4): the read-only refusal is said by every action box, not only the seat page's. The box is kept across the
    # read-only render by moving it out of the board first (the render replaces the board), so its handler runs with no commit transport.
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spy = ("window.__orig = window.LevainDashboard.render; window.LevainDashboard.render = (v, o) => { window.__v = v; return window.__orig(v, o); }; "
           "window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); "
           "window.addEventListener('unhandledrejection', e => window.__errs.push(String(e.reason))); 1")
    keep = "window.__b = %s; window.__b.querySelector('.tray-dump-ta').value = 'hi'; document.body.appendChild(window.__b); 1" % CARD_BOX
    readonly = "window.__orig(window.__v, {}); 1"
    click = "window.__b.querySelector('.tray-dump-btn').click(); 1"
    enter = ("const t = window.__b.querySelector('.tray-dump-ta'); t.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', metaKey: true, bubbles: true})); "
             "window.__b.querySelector('.tray-dump-ta').tagName")
    said = "JSON.stringify([(window.__b.querySelector('.edit-msg') || {}).textContent, window.__errs])"
    for act in (click, enter):
        out = _out(tmp_path, src, [{"eval": spy}, {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, {"until": "window.__v"},
                                   {"eval": keep}, {"eval": readonly}, {"until": "1", "ms": 500}, {"eval": act}, {"until": "1", "ms": 500}, {"eval": said}],
                   first=DESKTOP)
        text, errs = json.loads(out["values"][-1])
        assert text and "nothing was sent" in text.lower(), (act, out["values"])
        assert not [e for e in errs if "ResizeObserver" not in e] and [p for p in _verb_posts(out) if not p.get("dry_run")] == [], (act, out["values"])


@needs_browser
def test_a_job_verb_confirmed_after_the_page_turned_read_only_says_nothing_was_sent_and_posts_nothing(tmp_path, monkeypatch):
    # W1 (deepseek MED 1, fix r3 verify): the confirm step of a job verb sends through commitJob, which a read-only render takes away. No
    # shipped verb is both a job and a seat/compose card, so the page's view is turned into that stand-in: the compose verb declared job + confirm.
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    spy = ("window.__orig = window.LevainDashboard.render; window.LevainDashboard.render = (v, o) => { window.__v = v; window.__o = o; return window.__orig(v, o); }; "
           "window.__errs = []; window.addEventListener('error', e => window.__errs.push(String(e.message))); "
           "window.addEventListener('unhandledrejection', e => window.__errs.push(String(e.reason))); 1")
    keep = ("(() => { for (const k of Object.keys(window.__v.action_verbs || {})) { const s = window.__v.action_verbs[k]; "
            "if (s) { s.job = true; s.confirm_required = true; } } "
            "window.__jobs = 0; window.__orig(window.__v, Object.assign({}, window.__o, {commitJob: async () => { window.__jobs += 1; return {ok: true, job_id: 'j1'}; }})); window.__b = %s; window.__b.querySelector('.tray-dump-ta').value = 'hi'; "
            "document.body.appendChild(window.__b); window.__b.querySelector('.tray-dump-btn').click(); "
            "return window.__b.querySelector('.verb-confirm') ? 'confirming' : 'no-confirm'; })()") % CARD_BOX
    readonly = "window.__orig(window.__v, {}); 1"
    yes = "window.__b.querySelector('.verb-confirm').click(); 1"
    said = "JSON.stringify([(window.__b.querySelector('.edit-msg') || {}).textContent, window.__errs, window.__jobs])"
    out = _out(tmp_path, src, [{"eval": spy}, {"eval": "document.dispatchEvent(new Event('visibilitychange')); 1"}, {"until": "window.__v"},
                               {"eval": keep}, {"eval": readonly}, {"until": "1", "ms": 500}, {"eval": yes}, {"until": "1", "ms": 500}, {"eval": said}],
               first=DESKTOP)
    assert out["values"][-6] == "confirming", out["values"]
    text, errs, jobs = json.loads(out["values"][-1])
    assert text and "nothing was sent" in text.lower(), out["values"]
    assert jobs == 0, out["values"]
    assert not [e for e in errs if "ResizeObserver" not in e] and [p for p in _verb_posts(out) if not p.get("dry_run")] == [], out["values"]
