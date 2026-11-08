"""Board row 140: the streaming mic (and on-device Dictate) on the card reply boxes, not only the Room composer.

Two surfaces, both on a REAL cockpit server in headless Chrome with the fake microphone of ``tests/test_mic_page.py`` (no audio
device, no provider): the sign sheet's write-in (``/sign/<id>``, the words that become the signed ``words``) and the Needs
card's open-question box (``.question-words``). The mic control is drawn only when the server offers streaming; a partial then a
final fill the box; and a tap that SIGNS or SENDS the box's words is refused while the mic is open or still closing, so a
partial is never signed. What only a real iPhone shows (the permission prompt, worklet cadence) is residue.

Each test names the line whose revert turns it red."""
from __future__ import annotations

import json
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_sign_card import DESKTOP, QRID, _Rig, needs_ssh
from tests.test_mic_page import _fake
from tests.test_sign_sheet_page import NOTE, STATE, WRITE_IN_BOX, WRITE_IN_BTN, _open, _signed_message

BOX = WRITE_IN_BOX
MIC = "document.querySelector('#sheet-actions .card-mic-stream')"
SHOWN = f"!!{MIC} && !{MIC}.hidden"
FEED = {"eval": "window.__feed(12000)"}


def _fill(box: str) -> str:
    return f"{box}.value.indexOf('live 0') >= 0"


@needs_browser
@needs_ssh
def test_a_write_in_gets_the_mic_when_the_server_offers_streaming(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `attach(...)` call in sign.js withMic removed, or `<script src="/mic.js">` out of sign.html (no button)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake()}, rig.authenticator(), *_open(rig),
            {"until": f"{SHOWN} && 1", "ms": 30000},
            {"eval": f"JSON.stringify([{MIC}.textContent, [...{BOX}.parentNode.children].indexOf({MIC}) > [...{BOX}.parentNode.children].indexOf({BOX})])"},
        ])
    assert json.loads(out["values"][-1]) == ["Mic", True], out["values"]


@needs_browser
@needs_ssh
def test_the_mic_is_not_drawn_when_the_server_does_not_offer_streaming(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `ok === true && available()` test in mic.js attach() relaxed (the button shows without the offer)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake(flag="false")}, rig.authenticator(), *_open(rig),
            {"until": f"{BOX} && window.__sc.vj >= 1 && 1", "ms": 30000},
            {"eval": "new Promise((r) => setTimeout(r, 600)).then(() => " + SHOWN + ")"},
        ])
    assert out["values"][-1] is False, out["values"]


@needs_browser
@needs_ssh
def test_a_partial_then_a_final_fill_the_write_in_and_the_words_are_signed_after_the_mic_ended(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `draw` listener of attach() not writing `box.value` (the box stays empty, nothing is signed)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake()}, rig.authenticator(), *_open(rig),
            {"until": f"{SHOWN} && 1", "ms": 30000},
            {"eval": f"{BOX}.value = 'so '; {MIC}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": _fill(BOX), "ms": 20000},
            {"eval": f"JSON.stringify([{BOX}.value, {BOX}.readOnly, {MIC}.textContent])"},
            {"eval": f"{MIC}.click(); 1"},
            {"until": f"{BOX}.value === 'so the final text' && !{BOX}.readOnly && 1", "ms": 20000},
            {"tap": WRITE_IN_BTN},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    live = json.loads([v for v in out["values"] if isinstance(v, str) and v.startswith("[\"so ")][0])
    assert live[0].startswith("so ") and "live 0" in live[0] and live[1] is True and live[2] == "Stop mic", live       # the partial, box held, as <b>x</b> text
    assert _signed_message(rig).endswith("(*): so the final text")
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_sign_tap_during_live_dictation_signs_nothing_until_the_mic_has_ended(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `micOpen(wmic)` refusal removed from the Sign my words handler (the partial is signed at once)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake()}, rig.authenticator(), *_open(rig),
            {"until": f"{SHOWN} && 1", "ms": 30000},
            {"eval": f"{MIC}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": _fill(BOX), "ms": 20000},
            {"tap": WRITE_IN_BTN},
            {"eval": f"new Promise((r) => setTimeout(r, 800)).then(() => JSON.stringify([{NOTE}, {STATE}, {BOX}.value, {BOX}.disabled]))"},
        ])
    note, state, value, disabled = json.loads(out["values"][-1])
    assert "Stop the mic first" in note and state == "open" and "live 0" in value and disabled is False, (note, state, value)
    assert not any("/answer" in p or "/challenge" in p for p in out["posts"]), out["posts"]
    assert rig.nothing_signed(QRID)


@needs_browser
def test_the_needs_card_reply_box_gets_the_mic_and_send_words_waits_for_it(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `wmic.busy()` refusal removed from the send-words handler in dashboard_core.js (the partial is sent)."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    what = "What should the release note say about the sweep?"
    QuestionStore(home / "consent").file(agent="codex", agent_session_id="s2", what=what, blocks="the release note waits", cwd="/w", open_ended=True)
    _root, src, _ids = _install(tmp_path)
    rec = ("(() => { window.__posts = []; const f1 = window.fetch.bind(window); window.fetch = (u, o) => { "
           "if (o && o.method === 'POST' && /question|answer|verb/.test(String(u))) window.__posts.push(String(u)); return f1(u, o); }; })();")
    qmic = "document.querySelector('#levain-modal .needs-question .card-mic-stream')"
    qbox = "document.querySelector('#levain-modal .needs-question .question-words')"
    send = "document.querySelector('#levain-modal .needs-question .question-send')"
    msg = "document.querySelector('#levain-modal .needs-question .edit-msg')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"init": _fake() + rec},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-question')].length === 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-question')][0].click(); 1"},
            {"until": f"{qmic} && !{qmic}.hidden && 1", "ms": 30000},
            {"eval": f"{qmic}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": _fill(qbox), "ms": 20000},
            {"eval": f"{send}.click(); 1"},
            {"eval": f"new Promise((r) => setTimeout(r, 600)).then(() => JSON.stringify([{msg}.textContent, window.__posts, {qbox}.value]))"},
            {"eval": f"{qmic}.click(); 1"},
            {"until": f"{qbox}.value === 'the final text' && !{qbox}.readOnly && 1", "ms": 20000},
            {"eval": f"{send}.click(); 1"},
            {"until": "window.__posts.length >= 1", "ms": 20000},
            {"eval": "JSON.stringify(window.__posts)"},
        ])
    held = json.loads([v for v in out["values"] if isinstance(v, str) and v.startswith("[\"Stop")][0])
    assert "Stop the mic first" in held[0] and held[1] == [] and "live 0" in held[2], held
    assert json.loads(out["values"][-1]), out["values"]


# --- cmr2 (L3 r1): the signed-words boundary on the sheet ------------------------------------------------------------

DICT = "document.querySelector('#sheet-actions .card-mic-dictate')"
# the passkey call, watched: what the microphone was doing when the ceremony asked for the passkey
WATCH_GET = ("(() => { const c = navigator.credentials, g = c.get.bind(c); window.__atGet = [];"
             " c.get = (o) => { window.__atGet.push({ tracks: window.__sc.tracks.map((t) => t.stopped),"
             " srs: (window.__srs || []).map((r) => r.stops) }); return g(o); }; })();")
# Stop mic disabled while a capture is live, watched from before the tap
WATCH_STOP = ("(() => { const m = " + MIC + "; window.__deadStop = 0; new MutationObserver(() => {"
              " if (m.disabled && window.__sc.tracks.some((t) => !t.stopped)) window.__deadStop++; })"
              ".observe(m, { attributes: true, attributeFilter: ['disabled'] }); return 1; })()")


@needs_browser
@needs_ssh
def test_an_interim_that_dictate_ends_on_is_never_signed(tmp_path: Path, monkeypatch) -> None:
    """Red with: Dictate leaving its interim words in the box when the engine ends (they are then signed as his words)."""
    from tests.test_card_mic_lifecycle import RECOG
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake() + RECOG}, rig.authenticator(), *_open(rig),
            {"until": f"!!{DICT} && !{DICT}.hidden && 1", "ms": 30000},
            {"eval": f"{BOX}.value = 'mine'; {DICT}.click(); window.__srs[0].onresult(window.__res([['ghost', false]])); window.__srs[0].onend(); 1"},
            {"tap": WRITE_IN_BTN},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
        ])
    assert _signed_message(rig).endswith("(*): mine")
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_an_option_tap_under_a_live_sheet_mic_ends_the_mic_before_the_passkey_and_never_disables_a_live_stop(tmp_path: Path, monkeypatch) -> None:
    """Red with: the sheet's run() locking every control (Stop mic too) and starting the ceremony under a live mic."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake() + WATCH_GET}, rig.authenticator(), *_open(rig),
            {"until": f"{SHOWN} && 1", "ms": 30000},
            {"eval": f"{MIC}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": _fill(BOX), "ms": 20000},
            {"eval": WATCH_STOP},
            {"tap": "document.querySelector('#sheet-actions [data-choice=\"a\"]')"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "JSON.stringify([window.__atGet, window.__deadStop])"},
        ])
    at, dead = json.loads(out["values"][-1])
    assert at and all(n >= 1 for n in at[0]["tracks"]), at       # every track stopped before the passkey call
    assert dead == 0, dead
    assert rig.verifies(QRID)


@needs_browser
@needs_ssh
def test_a_signed_sheet_lets_go_of_its_idle_mics(tmp_path: Path, monkeypatch) -> None:
    """Red with: the sheet never calling off() on its mics (an idle one keeps its document listener after the actions go)."""
    from tests.test_card_mic_lifecycle import RECOG
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/nope", [
            DESKTOP, {"init": _fake() + RECOG}, rig.authenticator(), *_open(rig),
            {"until": f"!!{DICT} && 1", "ms": 30000},
            {"eval": "window.__visN"},
            {"tap": "document.querySelector('#sheet-actions [data-choice=\"a\"]')"},
            {"until": f"/^signed/.test({NOTE}) && {NOTE}", "ms": 30000},
            {"eval": "window.__visN"},
        ])
    assert out["values"][-4] == 1 and out["values"][-1] == 0, out["values"]
