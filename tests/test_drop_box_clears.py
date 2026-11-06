"""The cockpit's drop box lets its words go only once the server accepted the drop, on both ways of sending.

Phill, Home Screen app: after sending a signed drop and returning to the home screen the sent message was still in the drop field.
The "Add an envelope" button hands the box's words to the sheet; the sheet delivers them, and the box under it must let them go on
the server's acceptance (2xx) and keep them when the send failed or the answer was not a clear acceptance. "Returning home" is the
sheet closing, a visibilitychange to visible, and a reload of the page. The server and the passkey are faked inside the page."""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import NEEDS, TOK, FullRig, drive_touch, needs_browser, needs_ssh
from tests.test_envelope_sheet import FD, SIGN_ON, _in, _tag, make_init
from tests.test_msgslot_hero import BOX, READY, TXT, _tagged, _type

WORDS = "tidy the changelog"
SERVER = """(() => {
  const prior = window.fetch.bind(window);
  const reply = (s, o) => new Response(JSON.stringify(o), {status: s, headers: {'Content-Type': 'application/json'}});
  const buf = (n) => new Uint8Array(n).fill(7).buffer;
  try { navigator.credentials.get = () => Promise.resolve({rawId: buf(16), response: {authenticatorData: buf(37), clientDataJSON: buf(60), signature: buf(70)}}); } catch (e) { /* none */ }
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.indexOf('/cockpit/coord/envelope-challenge') >= 0) {
      return Promise.resolve(reply(200, {challenge: 'AAAA', payload: {}, rp_id: location.hostname, line: 'signing', store_id: 'st-1', store_created: false}));
    }
    if (url.indexOf('/cockpit/coord/drop') >= 0 && o && o.method === 'POST') {
      const b = JSON.parse(o.body);
      if (__STATUS__ !== 200) return Promise.resolve(reply(__STATUS__, {error: 'x', message: 'no'}));
      return Promise.resolve(reply(200, {created: true, drop: {drop_id: 'dr-1', words: b.words, sent_at: b.sent_at, receipts: []},
        envelope: b.envelope ? {taken: true, envelope_id: 'ev-1'} : null, woken: null, store_id: 'st-1'}));
    }
    return prior(u, o);
  };
})()"""

HOME = [{"eval": "document.dispatchEvent(new Event('visibilitychange')); new Promise((r) => setTimeout(() => r(1), 700))"}]


def _state(t: str) -> dict:
    return {"eval": f"JSON.stringify({{t: {json.dumps(t)}, box: {TXT}, kept: Object.keys(localStorage).filter((k) => k.indexOf('levain-drop-pending') === 0).length}})"}


def _run(tmp_path: Path, monkeypatch, status: int, signed: bool) -> dict:
    init = make_init() + ";" + SERVER.replace("__STATUS__", str(status))
    send = ([{"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"},
             {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);"), "ms": 30000},
             SIGN_ON, {"tap": f"{FD}.querySelector('#drop-sign')"},
             {"until": _in(FD, "return /delivered|Not sure|not delivered/.test(d.getElementById('sheet-note').textContent);"), "ms": 20000},
             {"eval": "new Promise((r) => setTimeout(() => r(1), 500))"},
             {"eval": "(document.getElementById('levain-sign-sheet-frame') ? 1 : 0)"},
             {"eval": _in(FD, "const c = d.getElementById('sheet-close'); if (c) c.click(); return 1;")},
             {"eval": "new Promise((r) => setTimeout(() => r(1), 1500))"}] if signed
            else [{"eval": "document.querySelector('#ck-drop [data-coord-drop=send]').click(); new Promise((r) => setTimeout(() => r(1), 1500))"}])
    steps = [{"emulate": {"w": 430, "h": 932, "dpr": 3, "mobile": True}}, {"touch": True}, {"init": init}, {"until": NEEDS, "ms": 30000},
             {"until": READY, "ms": 30000}, _type(WORDS), *send, *HOME, _state("home"),
             {"eval": "location.reload(); 1"}, {"eval": "new Promise((r) => setTimeout(() => r(1), 2500))"}, {"until": READY, "ms": 30000}, _state("reload")]
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return _tagged(drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))


OTHER = "words typed only in the sheet"
OPEN_SHEET = [{"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"},
              {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);"), "ms": 30000}]
DELIVERED = [SIGN_ON, {"tap": f"{FD}.querySelector('#drop-sign')"},
             {"until": _in(FD, "return /delivered|Not sure|not delivered/.test(d.getElementById('sheet-note').textContent);"), "ms": 20000},
             {"eval": "new Promise((r) => setTimeout(() => r(1), 800))"}]
CLOSE_SHEET = [{"eval": _in(FD, "const c = d.getElementById('sheet-close'); if (c) c.click(); return 1;")},
               {"eval": "new Promise((r) => setTimeout(() => r(1), 1500))"}]


def _scene(tmp_path: Path, monkeypatch, middle: list) -> dict:
    steps = [{"emulate": {"w": 430, "h": 932, "dpr": 3, "mobile": True}}, {"touch": True},
             {"init": make_init() + ";" + SERVER.replace("__STATUS__", "200")}, {"until": NEEDS, "ms": 30000},
             {"until": READY, "ms": 30000}, _type(WORDS), *middle, _state("end")]
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return _tagged(drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps))


def _sheet_words(text: str) -> dict:
    return {"eval": _in(FD, f"const e = d.getElementById('drop-words'); e.value = {json.dumps(text)}; "
                            "e.dispatchEvent(new Event('input', {bubbles: true})); return 1;")}


@needs_browser
@needs_ssh
def test_words_edited_in_the_sheet_leave_the_box_holding_what_was_never_sent(tmp_path: Path, monkeypatch) -> None:
    t = _scene(tmp_path, monkeypatch, [*OPEN_SHEET, _sheet_words(OTHER), *DELIVERED, *CLOSE_SHEET])
    assert t["end"]["box"] == WORDS, t


@needs_browser
@needs_ssh
def test_an_abandoned_handoff_does_not_let_a_later_delivery_wipe_the_box(tmp_path: Path, monkeypatch) -> None:
    reopen = [{"eval": "window.LevainSignSheet.openDrop(''); 1"},
              {"until": _in(FD, "return !!d.getElementById('drop-words');"), "ms": 30000},
              _sheet_words(OTHER),
              {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);"), "ms": 30000}]
    t = _scene(tmp_path, monkeypatch, [*OPEN_SHEET, *CLOSE_SHEET, *reopen, *DELIVERED, *CLOSE_SHEET])
    assert t["end"]["box"] == WORDS, t


@needs_browser
@needs_ssh
def test_a_delivered_message_from_anything_but_the_sheet_is_ignored(tmp_path: Path, monkeypatch) -> None:
    forged = [{"eval": f"window.postMessage({{levainSheet: 'signed', dropDelivered: true, words: {json.dumps(WORDS)}}}, window.location.origin); "
                       "new Promise((r) => setTimeout(() => r(1), 800))"}]
    t = _scene(tmp_path, monkeypatch, [*OPEN_SHEET, *forged])
    assert t["end"]["box"] == WORDS, t


def _gone(t: dict) -> None:
    assert t["home"]["box"] == "" and t["reload"]["box"] == "" and t["reload"]["kept"] == 0, t


def _kept(t: dict) -> None:
    assert t["home"]["box"] == WORDS and t["reload"]["box"] == WORDS, t


@needs_browser
@needs_ssh
def test_a_plain_drop_the_server_accepted_leaves_the_box_empty_after_home_and_reload(tmp_path: Path, monkeypatch) -> None:
    _gone(_run(tmp_path, monkeypatch, 200, False))


@needs_browser
@needs_ssh
def test_a_plain_drop_that_failed_keeps_its_words_after_home_and_reload(tmp_path: Path, monkeypatch) -> None:
    _kept(_run(tmp_path, monkeypatch, 500, False))


@needs_browser
@needs_ssh
def test_a_signed_drop_the_server_accepted_leaves_the_box_empty_after_home_and_reload(tmp_path: Path, monkeypatch) -> None:
    _gone(_run(tmp_path, monkeypatch, 200, True))


@needs_browser
@needs_ssh
def test_a_signed_drop_that_failed_keeps_its_words_after_home_and_reload(tmp_path: Path, monkeypatch) -> None:
    _kept(_run(tmp_path, monkeypatch, 500, True))
