"""levain#107 round 2: Rotate Desk asks the live desk to rotate itself through a SIGNED drop, and offers Take over (the signed /fanin sheet)
when no rotation is seen or the desk reads stalled.

The signature is the cockpit sheet's own (LevainSignSheet.openDrop, the drop path the New Desk button already uses); the page learns that a
signed drop was delivered from the cockpit's `levain:drop-delivered` event, which dashboard_core.js raises for a message from the
sheet's own frame (display only, not an authentication: the frame is same-origin with no sandbox attribute, so a script of the page can
raise the event itself, and the server verifies an envelope on its own). The state machine is the pure `rotateView` of desk_tab_view.js (clock and lines passed in), driven here under node with
no browser, and then on the real cockpit page behind the desk stub of tests/test_desk_pane_page.py.
LEVAIN_DESK_SHOTS=<dir> saves the screenshots.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_desk_pane_page import _OPEN, _TAP_DESK, _TOKENED, WEB, desk_page, put, until, wired

PHONE = {"emulate": {"w": 390, "h": 844, "dpr": 3, "mobile": True}}
DESKTOP = {"emulate": {"w": 1280, "h": 900, "dpr": 1}}
NONE = {"state": "none"}
JS = WEB / "desk_tab_view.js"
MIN = 60_000


def _shot(name: str) -> list[dict]:
    d = os.environ.get("LEVAIN_DESK_SHOTS")
    if not d:
        return []
    Path(d).mkdir(parents=True, exist_ok=True)
    return [{"shot": str(Path(d) / name), "full": False}]


# --- the pure state machine, under node -------------------------------------------------------------------------------------------------

_NODE = r"""
const D = require(process.argv[2]);
const held = (o) => Object.assign({state: "held", holder: "desk 25", ageS: 60}, o || {});
const W = D.ROTATE_WAIT_MS, S = D.STALL_AGE_S, T0 = 1000000;
const ask = {at: T0, holder: "desk 25", wentNone: false, kind: "rotate"};
const r = {};
const v = (rec, lease, now) => { const x = D.rotateView(rec, lease, now); return {rec: x.rec, phase: x.phase, button: x.button, words: x.words, age: x.age}; };
r.fresh = v(null, held(), T0);
r.waiting = v(ask, held(), T0 + 3 * 60000);
r.justUnder = v(ask, held(), T0 + W - 1);
r.timedOut = v(ask, held(), T0 + W);
r.stalledNoAsk = v(null, held({ageS: S}), T0);
r.stalledJustUnder = v(null, held({ageS: S - 1}), T0);
r.stalledWhileWaiting = v(ask, held({ageS: S}), T0 + 60000);
r.newHolder = v(ask, held({holder: "desk 26"}), T0 + 60000);
r.sameHolder = v(ask, held(), T0 + 60000);
r.noNameKnown = v({at: T0, holder: null, wentNone: false, kind: "rotate"}, held({holder: "desk 26"}), T0 + 60000);
r.wentNone = v(ask, {state: "none", holder: null, ageS: null}, T0 + 60000);
r.afterNoneSame = v(r.wentNone.rec, held(), T0 + 120000);                                   // the same named desk holds it after the gap
r.afterNoneSameTimedOut = v(r.wentNone.rec, held(), T0 + W);
r.afterNoneOther = v(r.wentNone.rec, held({holder: "desk 26"}), T0 + 120000);
r.afterNoneNoNameNow = v(r.wentNone.rec, held({holder: null}), T0 + 120000);
r.afterNoneNoNameRecorded = v({at: T0, holder: null, wentNone: true, kind: "rotate"}, held({holder: "desk 25"}), T0 + 120000);
r.hoursLater = v(ask, held(), T0 + 3 * 60 * 60000);
r.takeoverHoursLater = v({at: T0, holder: "desk 25", wentNone: false, kind: "takeover"}, held(), T0 + 3 * 60 * 60000);
r.unknown = v(ask, {state: "unknown"}, T0 + 60000);
r.clockBack = v(ask, held(), T0 - 5 * 60000);
r.takeoverWaiting = v({at: T0, holder: "desk 25", wentNone: false, kind: "takeover"}, held(), T0 + 60000);
r.takeoverTimedOut = v({at: T0, holder: "desk 25", wentNone: false, kind: "takeover"}, held(), T0 + W);
r.W = W; r.S = S;
r.words = {rotate: D.rotateWords("desk 25"), unnamed: D.rotateWords(null), blank: D.rotateWords("  "), spaced: D.rotateWords("desk\n 25 "), mark: D.ROTATE_MARK, desk: D.DESK_WORDS};
console.log(JSON.stringify(r));
"""


def _views(tmp_path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("no node")
    f = tmp_path / "rv.js"
    f.write_text(_NODE)
    return json.loads(subprocess.run([node, str(f), str(JS)], capture_output=True, text=True, timeout=30, check=True).stdout)


def test_a_live_desk_with_no_ask_offers_rotate_and_nothing_else(tmp_path):
    r = _views(tmp_path)["fresh"]
    assert (r["phase"], r["button"], r["rec"]) == ("rotate", "Rotate Desk", None), r


def test_an_ask_waits_and_offers_no_button_until_the_wait_is_over(tmp_path):
    g = _views(tmp_path)
    assert g["waiting"]["phase"] == "waiting" and g["waiting"]["button"] is None and g["waiting"]["rec"], g["waiting"]
    assert "Asked the desk to rotate" in g["waiting"]["words"] and g["waiting"]["age"] == "Asked 3 min ago.", g["waiting"]
    assert "min ago" not in g["waiting"]["words"] and "just now" not in g["waiting"]["words"], g["waiting"]      # the counter is not in the announced state words
    assert g["justUnder"]["button"] is None and g["justUnder"]["phase"] == "waiting", g["justUnder"]
    assert g["clockBack"]["phase"] == "waiting", g["clockBack"]                    # a clock that went back is not a rotation that never came


def test_no_rotation_by_the_wait_offers_take_over(tmp_path):
    g = _views(tmp_path)
    assert g["W"] == 25 * MIN, g["W"]
    assert g["timedOut"]["phase"] == "takeover" and g["timedOut"]["button"] == "Take over" and g["timedOut"]["rec"], g["timedOut"]
    assert "No rotation was seen" in g["timedOut"]["words"] and "this tab's lease reads" in g["timedOut"]["words"], g["timedOut"]
    assert g["timedOut"]["age"] == "Asked 25 min ago.", g["timedOut"]
    assert g["hoursLater"]["age"] == "Asked 180 min ago." and g["hoursLater"]["phase"] == "takeover", g["hoursLater"]      # the real minutes since the ask, not the constant
    assert "since you asked" in g["takeoverHoursLater"]["words"] and "No new desk was seen" in g["takeoverHoursLater"]["words"], g["takeoverHoursLater"]


def test_a_stalled_lease_offers_take_over_at_once_with_or_without_an_ask(tmp_path):
    g = _views(tmp_path)
    assert g["S"] == 15 * 60 and g["stalledNoAsk"]["button"] == "Take over" and g["stalledNoAsk"]["rec"] is None, g["stalledNoAsk"]
    assert "has not renewed" in g["stalledNoAsk"]["words"] and g["stalledNoAsk"]["age"] == "Not renewed for 15 min.", g["stalledNoAsk"]
    assert g["stalledJustUnder"]["button"] == "Rotate Desk", g["stalledJustUnder"]
    assert g["stalledWhileWaiting"]["button"] == "Take over", g["stalledWhileWaiting"]


def test_a_rotation_seen_clears_the_ask(tmp_path):
    g = _views(tmp_path)
    assert g["newHolder"]["rec"] is None and g["newHolder"]["button"] == "Rotate Desk", g["newHolder"]      # another desk holds the lease
    assert g["sameHolder"]["rec"] and g["sameHolder"]["phase"] == "waiting", g["sameHolder"]                 # the same desk is not a rotation
    assert g["noNameKnown"]["rec"] is not None, g["noNameKnown"]                                             # no name to compare: only a gap in the lease shows one
    assert g["wentNone"]["rec"]["wentNone"] is True and g["wentNone"]["phase"] == "none", g["wentNone"]     # the old desk let go; the ask is kept for the successor
    # after a gap in the lease: another desk, or no name to tell it by, is a rotation seen; the SAME named desk is not (a lapse, not a rotation)
    assert g["afterNoneOther"]["rec"] is None and g["afterNoneOther"]["button"] == "Rotate Desk", g["afterNoneOther"]
    assert g["afterNoneNoNameNow"]["rec"] is None and g["afterNoneNoNameRecorded"]["rec"] is None, (g["afterNoneNoNameNow"], g["afterNoneNoNameRecorded"])
    same = g["afterNoneSame"]
    assert same["rec"] and same["phase"] == "waiting" and same["button"] is None, same
    assert "The lease was seen empty and then held again by the same desk." in same["words"], same
    assert same["age"] == "Asked 2 min ago.", same                                                              # its timer runs on from the ask
    assert g["afterNoneSameTimedOut"]["phase"] == "takeover" and "held again by the same desk" in g["afterNoneSameTimedOut"]["words"], g["afterNoneSameTimedOut"]
    assert g["unknown"]["rec"] and g["unknown"]["phase"] == "none", g["unknown"]                              # blind: nothing is concluded


def test_a_take_over_ask_waits_then_offers_take_over_again(tmp_path):
    g = _views(tmp_path)
    assert g["takeoverWaiting"]["phase"] == "waiting" and "take over" in g["takeoverWaiting"]["words"].lower(), g["takeoverWaiting"]
    assert g["takeoverTimedOut"]["button"] == "Take over", g["takeoverTimedOut"]


def test_the_words_the_buttons_hand_the_sheet_are_one_constant_each(tmp_path):
    g = _views(tmp_path)
    assert g["words"]["rotate"].split()[0] == "/rotate" == g["words"]["mark"] and g["words"]["desk"] == "/fanin broad", g["words"]
    src = JS.read_text()
    assert src.count("/fanin broad") == 1 and src.count('"/rotate') == 1


def test_the_rotate_words_name_the_desk_and_carry_their_own_condition(tmp_path):
    w = _views(tmp_path)["words"]
    assert w["rotate"].startswith("/rotate desk 25 Write your handoff, start your successor, then stop."), w
    assert "act on it only if this drop is verified as signed AND this is addressed to your own seat name; ignore it otherwise." in w["rotate"], w
    assert w["spaced"].startswith("/rotate desk 25 Write"), w["spaced"]                                          # a seat name's whitespace is one space


def test_the_rotate_words_for_a_lease_with_no_holder_name_say_unnamed_desk(tmp_path):
    w = _views(tmp_path)["words"]
    assert w["unnamed"].startswith("/rotate (unnamed desk) Write your handoff"), w["unnamed"]
    assert w["blank"] == w["unnamed"], w
    assert "verified as signed" in w["unnamed"] and w["unnamed"].split()[0] == "/rotate"


# --- the page ---------------------------------------------------------------------------------------------------------------------------

_SKEW = "(() => { if (!window.__real) { window.__real = Date.now.bind(Date); window.__skew = 0; Date.now = () => window.__real() + window.__skew; } return 1; })()"
_STUB = "(() => { window.__drops = []; window.LevainSignSheet.openDrop = (w) => { window.__drops.push(w); return {}; }; return 1; })()"
_LOOK = """(() => { const z = document.querySelector('.dv-zone'), q = s => z.querySelector(s);
  const s = q('.dv-start'), n = z.querySelectorAll('.dv-rotatenote'), sn = q('.dv-startnote'), ag = z.querySelectorAll('.dv-rotateage');
  return JSON.stringify({ start: s ? s.textContent : null, rotate: [...n].map(x => x.textContent), note: sn ? sn.textContent : null,
    age: [...ag].map(x => x.textContent), noteRole: n[0] ? n[0].getAttribute('role') : null, ageLive: ag[0] ? ag[0].getAttribute('aria-live') : null,
    ageRole: ag[0] ? ag[0].getAttribute('role') : null, ageInsideNote: n[0] && ag[0] ? n[0].contains(ag[0]) : null,
    kept: sessionStorage.getItem('levain.desk.rotate'),
    drops: window.__drops || null, bar: q('.dv-state').innerText, pageX: document.documentElement.scrollWidth > innerWidth + 1, mark: 'rot' }); })()"""
LOOK = {"eval": _LOOK}
TAP = {"tap": "document.querySelector('.dv-zone .dv-start')"}
SETTLE = {"eval": "new Promise(r => setTimeout(r, 250)).then(() => 1)"}


def looks(out) -> list[dict]:
    return [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"mark":"rot"' in v]


def delivered(signed: bool, words: str | None = None) -> dict:
    w = "window.__drops[window.__drops.length - 1]" if words is None else json.dumps(words)
    return {"eval": f"(() => {{ document.dispatchEvent(new CustomEvent('levain:drop-delivered', {{detail: {{words: {w}, signed: {json.dumps(signed)}}}}})); return 1; }})()"}


def skew(minutes: int) -> dict:
    return {"eval": f"(() => {{ window.__skew = {minutes} * 60000; return 1; }})()"}


def _drive_desk(tmp_path, emulate, steps):
    with desk_page(tmp_path) as (stub, base):
        out = _drive(tmp_path, base + "/", [emulate, *_TOKENED, _TAP_DESK, _OPEN, {"eval": _SKEW}, *steps])
        out["sends"] = list(stub.sends)
    return out


HELD_BTN = until("document.querySelector('.dv-lease-held .dv-start')")


@needs_browser
def test_rotate_hands_the_sheet_the_rotate_words_and_asks_nothing_until_a_signed_drop_is_delivered(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, LOOK])
    g = looks(out)[-1]
    assert g["start"] == "Rotate Desk" and len(g["drops"]) == 1 and g["drops"][0].startswith("/rotate desk 25 "), g      # the drop names the desk it is for
    assert "verified as signed AND this is addressed to your own seat name" in g["drops"][0], g
    assert "rotates nothing" in g["note"] and g["rotate"] == [], g                 # opened is not asked
    assert out["sends"] == []                                                       # nothing went through the unsigned desk port


@needs_browser
def test_a_signed_rotate_delivered_shows_asked_the_desk_to_rotate_once_and_draws_no_second_button(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE, LOOK, *_shot("desk_rotate_asked_1280.png"),
        delivered(True), SETTLE, LOOK])
    a, again = looks(out)
    assert a["start"] is None and len(a["rotate"]) == 1 and "Asked the desk to rotate" in a["rotate"][0], a
    assert a["note"] is None, a                                                     # the open-sheet note is over once the ask is made
    assert again["rotate"] == a["rotate"] and again["start"] is None, again         # a second delivery changes nothing
    assert out["sends"] == []


@needs_browser
def test_words_only_delivery_is_not_an_ask(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(False), SETTLE, LOOK])
    g = looks(out)[-1]
    assert g["start"] == "Rotate Desk" and g["rotate"] == [] and g["kept"] is None, g
    assert "went as words only, or its signed envelope was not taken, so it is not counted as asked here" in g["note"], g
    assert "no signature" not in g["note"], g


@needs_browser
def test_a_delivered_drop_the_button_did_not_open_is_not_an_ask(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, delivered(True, "/rotate now"), SETTLE, LOOK,
        {"eval": _STUB}, TAP, SETTLE, delivered(True, "something else entirely"), SETTLE, LOOK])
    never_opened, other_words = looks(out)
    assert never_opened["rotate"] == [] and never_opened["start"] == "Rotate Desk", never_opened
    assert other_words["rotate"] == [] and other_words["start"] == "Rotate Desk", other_words


@needs_browser
def test_no_rotation_within_the_wait_offers_take_over_and_take_over_opens_the_signed_fanin_sheet(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE,
        skew(24), put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk 25", "age_s": 30})}),
        until("document.querySelector('.dv-rotatenote')"), LOOK,
        skew(26), until("document.querySelector('.dv-start')"), LOOK, *_shot("desk_takeover_offered_1280.png"),
        {"eval": _STUB}, TAP, SETTLE, LOOK])
    waiting, offered, opened = looks(out)
    assert waiting["start"] is None, waiting
    assert offered["start"] == "Take over" and "No rotation was seen" in offered["rotate"][0], offered
    assert opened["drops"] == ["/fanin broad"] and "starts nothing itself" in opened["note"], opened
    # the host answers a /fanin over a live day desk "went to <holder>" and starts nothing: the note must not promise a fresh desk
    assert "may answer that a desk is live and start nothing" in opened["note"] and "fresh desk" not in opened["note"], opened
    assert "Send words only files the words but is not counted" in opened["note"], opened
    assert out["sends"] == []


@needs_browser
def test_a_stalled_lease_offers_take_over_at_once(tmp_path):
    out = _drive_desk(tmp_path, PHONE, [
        put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk 25", "age_s": 16 * 60})}),
        until("document.querySelector('.dv-lease-held .dv-start')"), LOOK, *_shot("desk_stalled_390.png")])
    g = looks(out)[-1]
    assert g["start"] == "Take over" and "has not renewed" in g["rotate"][0] and not g["pageX"], g


@needs_browser
def test_a_new_holder_clears_the_ask_and_the_button_goes_back_to_rotate(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE, LOOK,
        put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk 26", "age_s": 5})}),
        until("document.querySelector('.dv-lease-held .dv-start')"), LOOK])
    asked, rotated = looks(out)
    assert asked["start"] is None and asked["rotate"], asked
    assert rotated["start"] == "Rotate Desk" and rotated["rotate"] == [], rotated


@needs_browser
def test_the_ask_is_kept_while_no_desk_holds_the_lease_and_a_desk_after_the_gap_clears_it(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE,
        {"eval": "JSON.stringify({kept: JSON.parse(sessionStorage.getItem('levain.desk.rotate')), mark: 'rot'})"},
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none .dv-start')"), LOOK,
        put(get={"status": 200, "json": wired()}), until("document.querySelector('.dv-lease-held .dv-rotatenote')"), LOOK])
    kept, none, back = looks(out)
    assert kept["kept"]["kind"] == "rotate" and kept["kept"]["holder"] == "desk 25", kept               # the ask is held in this tab's session storage
    assert none["start"] == "New Desk" and none["rotate"] == [], none                                   # no desk live: New Desk, unchanged
    assert back["start"] is None and "held again by the same desk" in back["rotate"][0], back          # the SAME desk after the gap is not a rotation: the ask stays
    assert back["kept"] is not None, back


@needs_browser
def test_another_desk_after_a_gap_in_the_lease_is_a_rotation_seen(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE,
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none .dv-start')"),
        put(get={"status": 200, "json": wired(lease={"state": "held", "holder": "desk 26", "age_s": 5})}),
        until("document.querySelector('.dv-lease-held .dv-start')"), LOOK])
    g = looks(out)[-1]
    assert g["start"] == "Rotate Desk" and g["rotate"] == [] and g["kept"] is None, g


@needs_browser
def test_a_signed_delivery_after_the_lease_left_held_says_so_and_records_no_ask(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE,
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-lease-none .dv-start')"),
        delivered(True), SETTLE, LOOK])
    g = looks(out)[-1]
    assert g["note"] == "Delivered and signed, but no desk holds the lease now; a desk started later may read it.", g
    assert g["kept"] is None and g["rotate"] == [] and g["age"] == [], g                  # no ask record, no timer


@needs_browser
def test_a_signed_delivery_while_the_lease_cannot_be_read_is_not_said_to_be_a_missing_desk(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE,
        put(get={"status": 200, "json": wired(lease={"state": "unknown"})}), until("document.querySelector('.dv-lease-unknown')"),
        delivered(True), SETTLE, LOOK])
    g = looks(out)[-1]
    assert g["note"] and "could not be checked" in g["note"] and "no desk holds" not in g["note"] and g["kept"] is None, g


@needs_browser
def test_a_lease_with_no_holder_name_hands_the_sheet_unnamed_desk(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease={"state": "held", "holder": None, "age_s": 60})}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, LOOK])
    g = looks(out)[-1]
    assert len(g["drops"]) == 1 and g["drops"][0].startswith("/rotate (unnamed desk) Write your handoff"), g


@needs_browser
def test_the_minute_counter_is_outside_the_live_region_so_a_minute_passing_says_nothing_new(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired()}), HELD_BTN, {"eval": _STUB}, TAP, SETTLE, delivered(True), SETTLE, LOOK,
        skew(3), until("document.querySelector('.dv-rotateage') && document.querySelector('.dv-rotateage').textContent === 'Asked 3 min ago.'"), LOOK])
    first, later = looks(out)
    assert first["noteRole"] == "status" and first["ageLive"] == "off" and first["ageRole"] is None, first
    assert first["ageInsideNote"] is False, first                                       # a sibling, not inside the live region
    assert first["age"] == ["Asked just now."] and later["age"] == ["Asked 3 min ago."], (first, later)
    assert later["rotate"] == first["rotate"], (first, later)                           # the announced text did not change when the minute did


def test_the_view_reads_the_delivery_event_and_never_the_unsigned_port_for_a_rotation():
    src = JS.read_text()
    assert "levain:drop-delivered" in src
    # every piece of the rotate code, not one stretch of the file: the pure view, the ask record and the delivered handler, and the
    # button's click handler (the unsigned desk port is the one SEND_PATH send, in the composer, which is none of these)
    segs = {
        "rotateView": src[src.index("function rotateView"):src.index("// One event the desk")],
        "onDelivered": src[src.index("function onDelivered"):src.index("doc.addEventListener(DELIVERED_EVENT")],
        "rotateWords": src[src.index("function rotateWords"):src.index("var ROTATE_OPEN_WORDS")],
        "button": src[src.index("var mode = canStart"):src.index("bar.appendChild(st);")],
    }
    for name, seg in segs.items():
        assert seg.strip(), name
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "createContextualFragment", "SEND_PATH", "fetch(", "XMLHttpRequest", "sendBeacon", "postMessage"):
            assert banned not in seg, (name, banned)
    assert src.count("SEND_PATH") == 2, src.count("SEND_PATH")                              # the constant and the composer's one send, no other use


# --- the cockpit raises the event for the sheet's own message and for nothing else ----------------------------------------------------------

_LISTEN = "(() => { window.__got = []; document.addEventListener('levain:drop-delivered', (e) => window.__got.push(e.detail)); return 1; })()"
# the page's own message handler takes a message only when its source is the sheet frame's window: a message event built here carries
# that window as its source, or the page's own window as a forgery. The frame is same-origin with no sandbox attribute, so a script of
# this page could reach it: the source check stops another frame, and the event is display only (the server verifies the envelope)
_MSG = ("(() => { const f = document.getElementById('levain-sign-sheet-frame'); if (!f) return 'noframe'; "
        "window.dispatchEvent(new MessageEvent('message', {data: {levainSheet: 'signed', dropDelivered: true, words: '/rotate %s', signed: true}, origin: location.origin, source: %s})); return 'sent'; })()")
_FROM_FRAME = _MSG % ("x", "f.contentWindow")
_FORGED = _MSG % ("y", "window")


@needs_browser
def test_the_event_is_raised_for_a_message_from_the_sheet_frame_and_not_for_a_forged_one(tmp_path):
    out = _drive_desk(tmp_path, DESKTOP, [
        put(get={"status": 200, "json": wired(lease=NONE)}), until("document.querySelector('.dv-start')"), {"eval": _LISTEN},
        TAP, {"until": "document.getElementById('levain-sign-sheet-frame')", "ms": 20000},
        {"eval": _FORGED}, SETTLE, {"eval": "JSON.stringify({got: window.__got, mark: 'ev'})"},
        {"eval": _FROM_FRAME}, SETTLE, {"eval": "JSON.stringify({got: window.__got, mark: 'ev'})"}])
    forged, real = [json.loads(v) for v in out["values"] if isinstance(v, str) and v.startswith("{") and '"mark":"ev"' in v]
    sent = [v for v in out["values"] if v in ("sent", "noframe")]
    assert sent == ["sent", "sent"], out["values"]
    assert forged["got"] == [], forged
    assert real["got"] == [{"words": "/rotate x", "signed": True}], real


def test_the_sheet_says_whether_an_envelope_was_taken_with_the_delivered_drop():
    src = (WEB / "sign.js").read_text()
    # words only (plain) is never "signed", nor is a delivery by a different store than the sheet was opened for
    assert "dropDelivered: true, words: req.words, signed: !plain && !other && !!(e && e.taken)" in src
