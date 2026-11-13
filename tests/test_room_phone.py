"""RM-H (CH-U2): the room on a phone and its foreground voice. STRUCTURAL ONLY: headless Chrome clamps the window width
(intention-1066), so a 390 emulation proves the rules apply and nothing overflows, never how it looks. Residue (his iPhone): the layout in
the Home Screen app, and whether any dictation control appears there at all."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.cockpit import room as cockpit_room
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_room import _serve_room, w  # noqa: F401  (fixture)
from tests.test_cockpit_k2a import TOKEN
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

WEB = Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web"
PROSE = {"autocapitalize": "sentences", "autocorrect": "on", "spellcheck": "true", "inputmode": "text", "autocomplete": "off"}

_STYLED = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<link rel="stylesheet" href="/dashboard.css"><body class="ke"><div id="host"></div>'
           '<script src="/mic.js"></script><script src="/room_view.js"></script><script src="/rvc.js"></script>')


def _assets(boot):
    return {"/rvc.html": ("text/html; charset=utf-8", _STYLED.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}


def _boot(speech, pre="", extra=""):
    return ('%s %s window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: %s,%s'
            ' headers: () => ({"X-Levain-Write-Token": "%s"})});' % (_FAKE, pre, speech, extra, TOKEN))


_FAKE = """
window.__started = [];
class Local { start() { window.__started.push({local: this.processLocally}); } stop() {} }
Local.prototype.processLocally = false;
class Cloud { start() { window.__started.push({cloud: true}); } stop() {} }
window.__recs = [];
function res(t) { return {results: [[{transcript: t}]]}; }
class Live {
  constructor() { window.__recs.push(this); this.stops = 0; this.lang = ""; }
  start() { window.__started.push({live: true}); if (window.__err) this.onerror({error: window.__err}); }
  stop() { this.stops++; }
  emit(t) { if (this.onresult) this.onresult(res(t)); }
  fire(t) { this.__r = res(t); }
}
Object.defineProperty(Live.prototype, "processLocally", {get() { return this._pl === true; }, set(v) { this._pl = v; }, configurable: true});
class Ignores extends Live {}
class Thrower extends Live { start() { throw new DOMException("x", "InvalidStateError"); } }
Object.defineProperty(Ignores.prototype, "processLocally", {get() { return false; }, set(v) {}, configurable: true});
class Unavailable extends Live { static available() { return Promise.resolve("unavailable"); } }
class Downloadable extends Live { static available() { return Promise.resolve("downloadable"); } }
class Installed extends Live { static available() { return Promise.resolve("available"); } }
// the pack is not on this browser: available() answers whatever window.__state holds, install() is a promise the test settles
window.__state = "downloadable"; window.__inst = []; window.__asks = 0; window.__done = null;
class Setup extends Live {
  static available() { window.__asks++; return Promise.resolve(window.__state); }
  static install(o) { window.__inst.push(JSON.stringify(o)); return new Promise((res, rej) => { window.__done = {res, rej}; }); }
}
// the browser masks the pack: start() ends window.__code (service-not-allowed) until install() has resolved true
window.__code = "service-not-allowed"; window.__ok = false;
class Masked extends Setup {
  start() { window.__started.push({live: true}); if (!window.__ok) this.onerror({error: window.__code}); }
}
// a poll that fails
class FlakyPoll extends Setup {
  static available() { window.__asks++; return window.__asks === 1 ? Promise.resolve("downloading") : Promise.reject(new Error("x")); }
}
"""


def test_the_room_boxes_are_prose_with_dash_free_names():
    """The same pin as tests/test_drop_box_is_prose.py, for the room's two text boxes (the draft and a card's words)."""
    js = (WEB / "room_view.js").read_text()
    assert 'prose(ta, "roomdraft")' in js and 'prose(ta, "roomcardwords")' in js
    body = js[js.index("function prose("):][:400]
    for k, v in PROSE.items():
        assert f'["{k}", "{v}"]' in body, k
    assert '["name", name]' in body
    assert "one-time-code" not in js and 'inputmode="none"' not in js and '["inputmode", "none"]' not in js
    assert '"none"' not in js[js.index("function prose("):][:400]


def test_the_phone_rules_exist_and_use_tokens_only():
    css = (WEB / "dashboard.css").read_text()
    i = css.index("RM-H, the phone")
    block = css[i:css.index(".rv .rv-jump", i)]
    assert "font-size: 16px" in block and "env(safe-area-inset-bottom" in block and "position: sticky" not in block   # the dock sits under the conversation, never over it (1104-29)
    assert ".rv-send { grid-column" not in block   # Send sits beside the draft (1106-16); 1106-51: one composer row, no button grid
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)


def test_voice_never_names_a_cloud_recognizer_or_a_transcribe_route():
    js = (WEB / "room_view.js").read_text()
    for word in ("getUserMedia", "MediaRecorder", "audio/transcriptions", "api.openai.com"):
        assert word not in js, word
    assert 'LevainMic.localSpeech(Speech)' in js and "r.processLocally = true" in js   # the on-device test is mic.js's one function
    assert "dictateReason" in js and "Heard nothing" not in js and "not installed for this language" not in js   # the words come from mic.js's one table


@needs_browser
def test_a_recognizer_that_cannot_stay_on_the_device_gets_no_control(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Cloud"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"},
            {"eval": "document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length"},
        ])
    assert out["values"][1] == 0


@needs_browser
def test_an_on_device_recognizer_gets_a_control_that_asks_for_local_processing(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-mic:not(.rv-mic-stream)')"},
            {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); JSON.stringify(window.__started)"},
        ])
    assert json.loads(out["values"][1]) == [{"local": True}]


_GEOM = """(() => { const r = n => n.getBoundingClientRect();
const acts = [...document.querySelectorAll('.rv-mic:not(.rv-mic-stream), .rv-send')], dock = r(document.querySelector('.rv-dock'));
return JSON.stringify({noSideScroll: document.documentElement.scrollWidth <= window.innerWidth,
  inside: acts.every(b => r(b).left >= dock.left - 0.5 && r(b).right <= dock.right + 0.5),
  tall: acts.every(b => r(b).height >= 43.5), unclipped: acts.every(b => b.scrollWidth <= b.clientWidth + 1),
  send44: r(document.querySelector('.rv-send')).width >= 43.5 && r(document.querySelector('.rv-send')).height >= 43.5,
  rows: new Set(acts.map(b => Math.round(r(b).top))).size, n: acts.length,
  input16: parseFloat(getComputedStyle(document.querySelector('.rv-input')).fontSize),
  sticky: getComputedStyle(document.querySelector('.rv-dock')).position}); })()"""


@needs_browser
def test_the_dock_fits_a_390_phone_with_every_control_whole(tmp_path, w):
    """Structural only. The dock is in the flow below the conversation (1104-29), no longer sticky."""
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"emulate": {"w": 390, "h": 800, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('.rv-mic:not(.rv-mic-stream)') && document.querySelector('.rv-rotate')"},
            {"eval": _GEOM},
        ])
    g = json.loads(out["values"][1])
    assert g["noSideScroll"] and g["inside"] and g["tall"] and g["unclipped"] and g["send44"], g
    assert g["n"] == 2 and g["rows"] <= 2 and g["input16"] >= 16 and g["sticky"] != "sticky", g


# --- the voice, F1-F4 of the r1 findings -----------------------------------------------------------------------------

_N = [0]


def _run(tmp_path, w, speech, steps, pre="", extra=""):
    _N[0] += 1
    with _serve_room(tmp_path, w, name="e%d" % _N[0], extra_assets=_assets(_boot(speech, pre, extra))) as (base, _h):
        return _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"}] + steps), w


@needs_browser
def test_a_late_result_after_send_does_not_bring_the_sent_line_back(tmp_path, w):
    """F1, reproduced first: Send while dictating, then the recognizer fires a late result with the pre-send base."""
    out, w = _run(tmp_path, w, "Live", [
        {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); __recs[0].emit('hello there');"
                 " const ta = document.querySelector('.rv-input'); const was = ta.value;"
                 " document.querySelector('.rv-send').click(); __recs[0].emit('hello there again'); JSON.stringify({was, stops: __recs[0].stops})"},
        {"until": "document.querySelector('.rv-sendnote').textContent === 'Sent.'"},
        {"eval": "__recs[0].emit('late and unwanted'); JSON.stringify({v: document.querySelector('.rv-input').value,"
                 " label: document.querySelector('.rv-mic:not(.rv-mic-stream)').getAttribute('aria-label')})"},
    ])
    assert json.loads(out["values"][1]) == {"was": "hello there", "stops": 1}
    assert json.loads(out["values"][3]) == {"v": "", "label": "Dictate"}
    assert [r[3] for r in w.lines()] == ["hello there"]


@needs_browser
def test_a_recognizer_that_ignores_processlocally_never_starts(tmp_path, w):
    """F2: the property exists on the prototype but the setter does nothing, so reading it back is the only proof."""
    out, _w = _run(tmp_path, w, "Ignores", [
        {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); JSON.stringify({started: __started.length,"
                 " note: document.querySelector('.rv-sendnote').textContent, label: document.querySelector('.rv-mic:not(.rv-mic-stream)').getAttribute('aria-label')})"},
    ])
    got = json.loads(out["values"][1])
    assert got["started"] == 0 and got["label"] == "Dictate" and "keep dictation on this device" in got["note"], got


@needs_browser
def test_the_mic_follows_availability_not_capability(tmp_path, w):
    out, _w = _run(tmp_path, w, "Unavailable", [{"eval": "document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length"}])
    assert out["values"][1] == 0
    # a pack the browser can fetch is offered, not mourned (see the Setup tests below); a browser that lists the pack but has no
    # install() cannot fetch it from this page: no dead button, Dictate gone, the one table's words up front
    out, _w = _run(tmp_path, w, "Downloadable", [
        {"until": "document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length === 0 && document.querySelector('.rv-sendnote').textContent"},
        {"eval": "JSON.stringify({hidden: document.querySelector('.rv-setup').hidden, started: __started.length,"
                 " note: document.querySelector('.rv-sendnote').textContent, want: LevainMic.dictateSetup('noinstall')})"}])
    got = json.loads(out["values"][2])
    assert got["hidden"] and got["started"] == 0 and got["note"] == got["want"] != "", got
    out, _w = _run(tmp_path, w, "Installed", [
        {"until": "document.querySelector('.rv-mic:not(.rv-mic-stream)') && document.querySelector('.rv-mic:not(.rv-mic-stream)').style.display !== 'none'"},
        {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); __recs[0].lang"}])
    assert out["values"][2] != ""            # lang is set explicitly from the document / browser language


# what the room says for each error name: the same words the card boxes say (mic.js holds the one table), pinned here by the
# distinctive phrase of each so a room that kept its own table, or lost the shared one, reads red
ROOM_WORDS = {"not-allowed": "allow it for this site in the address bar", "service-not-allowed": "on-device pack may be missing",
              "language-not-supported": "on-device pack may be missing", "audio-capture": "No microphone was found",
              "no-speech": "No speech was heard; tap Dictate and try again.", "network": "Speech recognition failed; tap Dictate to try again.",
              "aborted": "Dictation was interrupted; tap Dictate to try again.", "something-new": "Dictation stopped (something-new)."}


@needs_browser
def test_dictation_errors_read_by_code(tmp_path, w):
    """Red with: the room keeping its own error table (the old words for network and aborted, no reason for a throwing start())."""
    for code, words in ROOM_WORDS.items():
        out, _w = _run(tmp_path, w, "Live", [
            {"eval": "window.__err = '%s'; document.querySelector('.rv-mic:not(.rv-mic-stream)').click();"
                     " JSON.stringify({n: document.querySelector('.rv-sendnote').textContent, l: document.querySelector('.rv-mic:not(.rv-mic-stream)').getAttribute('aria-label'),"
                     " same: document.querySelector('.rv-sendnote').textContent === LevainMic.dictateReason('%s')})" % (code, code)}])
        got = json.loads(out["values"][1])
        assert words in got["n"] and got["l"] == "Dictate" and got["same"], (code, got)


@needs_browser
def test_a_dictation_start_that_throws_says_so_in_the_room(tmp_path, w):
    """Red with: the start() catch freeing the control with no note (the tap does nothing, silently)."""
    out, _w = _run(tmp_path, w, "Thrower", [
        {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); JSON.stringify({n: document.querySelector('.rv-sendnote').textContent,"
                 " l: document.querySelector('.rv-mic:not(.rv-mic-stream)').getAttribute('aria-label'), recs: __recs.length})"}])
    assert json.loads(out["values"][1]) == {"n": "Dictation could not start; tap Dictate again.", "l": "Dictate", "recs": 1}


@needs_browser
def test_a_new_dictation_attempt_takes_back_its_own_last_reason_and_nobody_elses(tmp_path, w):
    """Red with: the tap never clearing the last reason (the note says "No speech was heard" while dictation works); or clearing
    a note that is not the dictation's own (the note is shared with Send)."""
    out, _w = _run(tmp_path, w, "Live", [
        {"eval": "window.__err = 'no-speech'; document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); document.querySelector('.rv-sendnote').textContent"},
        {"eval": "window.__err = ''; document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); document.querySelector('.rv-sendnote').textContent"},
        {"eval": "document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); document.querySelector('.rv-sendnote').textContent = 'Sent.';"
                 " document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); document.querySelector('.rv-sendnote').textContent"},
    ])
    assert out["values"][1] == ROOM_WORDS["no-speech"]
    assert out["values"][2] == ""
    assert out["values"][3] == "Sent."


@needs_browser
def test_dictation_into_an_empty_draft_enables_send(tmp_path, w):
    """L3 r1 (codex 5): a recognizer result sets the box's value, which fires no input event; Send stayed aria-disabled."""
    out, _w = _run(tmp_path, w, "Live", [
        {"eval": "const ta = document.querySelector('.rv-input'); ta.value = ''; ta.dispatchEvent(new Event('input'));"
                 " const before = document.querySelector('.rv-send').getAttribute('aria-disabled');"
                 " document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); __recs[0].emit('send this to the desk');"
                 " JSON.stringify({before, after: document.querySelector('.rv-send').getAttribute('aria-disabled'), v: ta.value})"},
    ])
    assert json.loads(out["values"][1]) == {"before": "true", "after": None, "v": "send this to the desk"}


@needs_browser
def test_typing_during_dictation_is_kept_and_the_next_result_extends_it(tmp_path, w):
    """F4: the next result must not overwrite what was typed in between."""
    out, _w = _run(tmp_path, w, "Live", [
        {"eval": "const ta = document.querySelector('.rv-input'); document.querySelector('.rv-mic:not(.rv-mic-stream)').click(); __recs[0].emit('one two');"
                 " ta.value = ta.value + ' typed'; ta.dispatchEvent(new Event('input')); __recs[0].emit('one two three'); ta.value"},
    ])
    assert out["values"][1] == "one two typed three"


# --- the one-tap set up of the on-device pack (SpeechRecognition.install; a stub, never a real uninstalled Chrome) ------------

_SB = "document.querySelector('.rv-setup')"
_MB = "document.querySelector('.rv-mic:not(.rv-mic-stream)')"
_NOTE = "document.querySelector('.rv-sendnote').textContent"
_OFFER = "(%s && !%s.hidden && !%s.disabled)" % (_SB, _SB, _SB)
_WORKING = "(%s && !%s.hidden && %s.disabled)" % (_SB, _SB, _SB)
_DICTATE = "(%s && %s.style.display !== 'none' && %s.hidden)" % (_MB, _MB, _SB)
_WORD = "LevainMic.dictateSetup"


def test_install_is_called_in_one_place_and_the_words_live_in_mic_js():
    """Red with: a second install() call site (a call outside the tap), or the room keeping its own set up sentences."""
    js = (WEB / "room_view.js").read_text()
    assert js.count("Speech.install(") == 1 and "dictateSetup" in js
    assert "Set up on-device dictation" not in js and "Setting up" not in js and "could not be set up" not in js
    mic = (WEB / "mic.js").read_text()
    assert "Set up on-device dictation" in mic and "dictateSetup: dictateSetup" in mic


@needs_browser
def test_a_downloadable_pack_offers_one_button_and_a_tap_installs_it_once(tmp_path, w):
    """The page never calls install() on its own (no tap, no call); the tap calls it once with the page language and
    processLocally; the button reads Setting up and is disabled while it runs; success shows Dictate in its place."""
    out, _w = _run(tmp_path, w, "Setup", [
        {"until": _OFFER},
        {"eval": "JSON.stringify({text: %s.textContent, want: %s('offer'), mic: %s.style.display, inst: __inst.length, note: %s})" % (_SB, _WORD, _MB, _NOTE)},
        {"eval": "%s.click(); %s.dispatchEvent(new MouseEvent('click', {bubbles: true}));"
                 " JSON.stringify({inst: __inst, text: %s.textContent, want: %s('working'), off: %s.disabled, lang: document.documentElement.lang})" % (_SB, _SB, _SB, _WORD, _SB)},
        {"eval": "window.__state = 'available'; __done.res(true); 1"},
        {"until": _DICTATE},
        {"eval": "JSON.stringify({inst: __inst.length, note: %s, want: %s('done'), started: __started.length})" % (_NOTE, _WORD)},
        {"eval": "%s.click(); JSON.stringify({recs: __recs.length, local: __recs[0].processLocally, started: __started.length})" % _MB},
    ])
    before = json.loads(out["values"][2])
    assert before["text"] == before["want"] == "Set up on-device dictation" and before["mic"] == "none" and before["inst"] == 0 and before["note"] == "", before
    tap = json.loads(out["values"][3])
    assert tap["text"] == tap["want"] and tap["off"] is True and len(tap["inst"]) == 1, tap
    opts = json.loads(tap["inst"][0])
    assert opts["processLocally"] is True and len(opts["langs"]) == 1 and opts["langs"][0], opts
    after = json.loads(out["values"][6])
    assert after["inst"] == 1 and after["note"] == after["want"] and after["started"] == 0, after
    assert json.loads(out["values"][7]) == {"recs": 1, "local": True, "started": 1}


@needs_browser
def test_a_refused_install_says_why_keeps_the_button_and_retries(tmp_path, w):
    """Red with: a rejection that leaves the button gone or the page stuck on Setting up, or a retry that never calls install again.
    One tap each: NotAllowedError, then false, then another rejection, then success."""
    def tap(finish):
        return [{"eval": "%s.click(); 1" % _SB}, {"eval": finish + "; 1"}, {"until": "%s && %s" % (_OFFER, _NOTE)},
                {"eval": "JSON.stringify({inst: __inst.length, note: %s, mic: %s.style.display})" % (_NOTE, _MB)}]
    out, _w = _run(tmp_path, w, "Setup", [{"until": _OFFER}]
                   + tap("__done.rej(new DOMException('x', 'NotAllowedError'))")
                   + tap("__done.res(false)")
                   + tap("__done.rej(new TypeError('boom'))")
                   + [{"eval": "JSON.stringify({declined: %s('declined'), failed: %s('failed'), unlisted: %s('unlisted', 'TypeError')})" % (_WORD, _WORD, _WORD)},
                      {"eval": "%s.click(); window.__state = 'available'; __done.res(true); 1" % _SB}, {"until": _DICTATE},
                      {"eval": "__inst.length"}])
    v = out["values"]
    a, b, c, words = json.loads(v[5]), json.loads(v[9]), json.loads(v[13]), json.loads(v[14])
    assert (a["inst"], a["note"], a["mic"]) == (1, words["declined"], "none"), a
    assert (b["inst"], b["note"]) == (2, words["failed"]), b
    assert (c["inst"], c["note"]) == (3, words["unlisted"]) and "(TypeError)" in c["note"], c
    assert v[17] == 4


@needs_browser
def test_install_that_succeeds_while_the_browser_still_says_downloadable_does_not_lie(tmp_path, w):
    """Red with: Dictate shown on install() == true alone (the page re-asks available() and follows its answer)."""
    out, _w = _run(tmp_path, w, "Setup", [
        {"until": _OFFER},
        {"eval": "%s.click(); __done.res(true); 1" % _SB},
        {"until": "__asks >= 2 && %s && %s" % (_OFFER, _NOTE)},
        {"eval": "JSON.stringify({note: %s, want: %s('stillnot'), mic: %s.style.display, inst: __inst.length})" % (_NOTE, _WORD, _MB)},
    ])
    got = json.loads(out["values"][4])
    assert got["note"] == got["want"] != "" and got["mic"] == "none" and got["inst"] == 1, got


@needs_browser
def test_a_pack_that_is_downloading_says_setting_up_and_never_installs_again(tmp_path, w):
    """Red with: a second install() call for "downloading", or a button the operator can press while it runs, or no Dictate when
    the download ends. The page re-asks available() on a timer (setupPollMs) because the API has no progress event."""
    out, _w = _run(tmp_path, w, "Setup", [
        {"until": _WORKING},
        {"eval": "JSON.stringify({text: %s.textContent, want: %s('working'), inst: __inst.length, mic: %s.style.display})" % (_SB, _WORD, _MB)},
        {"until": "__asks >= 4"},
        {"eval": "JSON.stringify({inst: __inst.length, working: %s})" % _WORKING},
        {"eval": "window.__state = 'available'; 1"},
        {"until": _DICTATE},
        {"eval": "__inst.length"},
    ], pre="window.__state = 'downloading';", extra=" setupPollMs: 20,")
    v = out["values"]
    first, mid = json.loads(v[2]), json.loads(v[4])
    assert first["text"] == first["want"] == "Setting up on-device dictation..." and first["inst"] == 0 and first["mic"] == "none", first
    assert mid == {"inst": 0, "working": True}, mid
    assert v[7] == 0


@needs_browser
def test_a_pack_the_browser_cannot_offer_stays_gone(tmp_path, w):
    """Unavailable: no Dictate and no button (nothing to set up), as before."""
    out, _w = _run(tmp_path, w, "Setup", [
        {"until": "__asks >= 1"},
        {"eval": "JSON.stringify({mic: document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length, setup: %s.hidden})" % _SB}],
        pre="window.__state = 'unavailable';")
    assert json.loads(out["values"][2]) == {"mic": 0, "setup": True}


# --- the production mount: probe:false (dashboard_boot.js; available() froze the renderer, RM-G2) -------------------------------
# With the probe off the page cannot ask whether the pack is there, so a Dictate start that ends service-not-allowed is the signal.

_NOPROBE = " probe: false,"


@needs_browser
def test_with_the_probe_off_a_masked_pack_swaps_dictate_for_setup_and_a_tap_makes_dictate_work(tmp_path, w):
    """THE integration test, with the production mount options (probe:false): Dictate is shown, a start ends service-not-allowed
    -> Dictate is hidden and Setup appears; one tap -> install() once (page language, processLocally); true -> Dictate again,
    and it works. available() is never asked (it froze the renderer). Red with: no offer after the error, Dictate left beside
    Setup, a second install site, or the success path asking available()."""
    out, _w = _run(tmp_path, w, "Masked", [
        {"until": _DICTATE},
        {"eval": "JSON.stringify({asks: __asks, inst: __inst.length, setup: %s.hidden})" % _SB},
        {"eval": "%s.click(); JSON.stringify({offer: %s, mic: %s.style.display, inst: __inst.length, text: %s.textContent, want: %s('offer'),"
                 " note: %s, started: __started.length})" % (_MB, _OFFER, _MB, _SB, _WORD, _NOTE)},
        {"eval": "%s.click(); %s.dispatchEvent(new MouseEvent('click', {bubbles: true}));"
                 " JSON.stringify({inst: __inst, working: %s, mic: %s.style.display, want: %s('working'), text: %s.textContent})" % (_SB, _SB, _WORKING, _MB, _WORD, _SB)},
        {"eval": "window.__ok = true; __done.res(true); 1"},
        {"until": _DICTATE},
        {"eval": "JSON.stringify({asks: __asks, inst: __inst.length, note: %s, want: %s('done')})" % (_NOTE, _WORD)},
        {"eval": "%s.click(); __recs[__recs.length - 1].emit('hello there'); JSON.stringify({recs: __recs.length, started: __started.length,"
                 " text: document.querySelector('.rv-input').value, setup: %s.hidden, mic: %s.style.display})" % (_MB, _SB, _MB)},
    ], extra=_NOPROBE)
    v = out["values"]
    assert json.loads(v[2]) == {"asks": 0, "inst": 0, "setup": True}, v[2]          # nothing asked and nothing installed on its own
    fail = json.loads(v[3])
    assert fail["offer"] is True and fail["mic"] == "none" and fail["inst"] == 0 and fail["started"] == 1, fail
    assert fail["text"] == fail["want"] == "Set up on-device dictation" and "pack may be missing" in fail["note"], fail
    tap = json.loads(v[4])
    assert len(tap["inst"]) == 1 and tap["working"] is True and tap["mic"] == "none" and tap["text"] == tap["want"], tap
    opts = json.loads(tap["inst"][0])
    assert opts["processLocally"] is True and len(opts["langs"]) == 1 and opts["langs"][0], opts
    done = json.loads(v[7])
    assert done == {"asks": 0, "inst": 1, "note": done["want"], "want": done["want"]} and done["want"], done
    end = json.loads(v[8])
    assert end == {"recs": 2, "started": 2, "text": "hello there", "setup": True, "mic": ""}, end


@needs_browser
def test_a_failed_start_offers_setup_only_for_a_pack_error_and_only_when_install_exists(tmp_path, w):
    """language-not-supported is the same cause in the one table; not-allowed (the microphone) is not a pack problem; a browser
    with no install() has nothing to offer."""
    def once(speech, code):
        out, _w = _run(tmp_path, w, speech, [
            {"until": _DICTATE},
            {"eval": "%s.click(); JSON.stringify({setup: %s.hidden, mic: %s.style.display, note: %s})" % (_MB, _SB, _MB, _NOTE)}],
            pre="window.__code = %r; window.__err = %r;" % (code, code), extra=_NOPROBE)
        return json.loads(out["values"][2])
    assert once("Masked", "language-not-supported")["setup"] is False
    mic = once("Masked", "not-allowed")
    assert mic["setup"] is True and mic["mic"] == "" and "microphone" in mic["note"], mic
    bare = once("Live", "service-not-allowed")
    assert bare["setup"] is True and bare["mic"] == "" and "pack may be missing" in bare["note"], bare


def test_the_dashboard_boot_still_mounts_the_room_with_the_probe_off_and_the_room_never_asks_available_on_that_path():
    """Pins the premise the test above stands on: the cockpit mounts with probe:false (its renderer-freeze reason in
    test_cockpit_room_join), so the Setup offer must be reachable from a failed start alone."""
    boot = (WEB / "dashboard_boot.js").read_text()
    assert re.search(r"LevainRoomView\.mount\([^\n]*probe: false\s*\}\)", boot)
    js = (WEB / "room_view.js").read_text()
    assert "if (!probing) { settleAvail(" in js and "offerSetupAfter(ev && ev.error)" in js


# --- smaller fixes from the L3 pass --------------------------------------------------------------------------------------

@needs_browser
def test_a_button_with_no_words_is_not_drawn(tmp_path, w):
    """mic.js missing its set up words must not leave a blank tappable button: the plain note takes over."""
    out, _w = _run(tmp_path, w, "Setup", [
        {"until": "__asks >= 1"},
        {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({setup: %s.hidden, mics: document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length})), 150))" % _SB}],
        pre="LevainMic.dictateSetup = undefined;")
    assert json.loads(out["values"][2]) == {"setup": True, "mics": 0}


@needs_browser
def test_a_poll_that_fails_counts_toward_the_limit_and_ends_in_the_slow_note(tmp_path, w):
    """Red with: a failed poll that resets the count and drops to a bare button (the limit is never reached)."""
    out, _w = _run(tmp_path, w, "FlakyPoll", [
        {"until": "%s && %s === %s('slow')" % (_OFFER, _NOTE, _WORD)},
        {"eval": "JSON.stringify({asks: __asks, inst: __inst.length})"}],
        extra=" setupPollMs: 2,")
    got = json.loads(out["values"][2])
    assert got["inst"] == 0 and got["asks"] >= 100, got


# --- L3 r2: a pack error that install() cannot fix (a non-secure page, a policy block) must not loop -----------------------------
# available() that throws synchronously
_SYNC_THROW = """
class SyncPoll extends Setup {
  static available() { window.__asks++; if (window.__asks === 1) return Promise.resolve("downloading"); throw new Error("sync"); }
}
class SyncAfterInstall extends Setup {
  static available() { window.__asks++; if (window.__asks === 1) return Promise.resolve("downloadable"); throw new Error("sync"); }
}
"""
_INSECURE = "Object.defineProperty(window, 'isSecureContext', {value: false, configurable: true});"


def _run_x(tmp_path, w, speech, steps, pre="", extra=""):
    return _run(tmp_path, w, speech, steps, pre=_SYNC_THROW + pre, extra=extra)


@needs_browser
def test_after_a_successful_install_the_same_pack_error_gets_the_refused_line_and_no_setup(tmp_path, w):
    """The loop: probe:false, start fails service-not-allowed -> Setup -> install true -> Dictate -> start fails the SAME way. Red
    with: Setup offered again (every lap one more install()), or the 'installed, browser still refuses' line missing."""
    out, _w = _run(tmp_path, w, "Masked", [
        {"until": _DICTATE},
        {"eval": "%s.click(); %s.click(); __done.res(true); 1" % (_MB, _SB)},       # fail -> Setup -> tap (the pack is NOT the cause: __ok stays false)
        {"until": _DICTATE},
        {"eval": "%s.click(); JSON.stringify({setup: %s.hidden, mic: %s.style.display, inst: __inst.length, note: %s, want: %s('refused'),"
                 " reason: LevainMic.dictateReason('service-not-allowed')})" % (_MB, _SB, _MB, _NOTE, _WORD)},
    ], extra=_NOPROBE)
    got = json.loads(out["values"][4])
    assert got["setup"] is True and got["mic"] == "" and got["inst"] == 1, got           # Dictate stays, no second Setup, one install ever
    assert got["want"] and got["note"] == got["reason"] + " " + got["want"], got
    assert "https" in got["want"] and "localhost" in got["want"] and "microphone" in got["want"], got


@needs_browser
def test_a_dictate_start_that_gets_going_clears_the_installed_flag(tmp_path, w):
    """Red with: a flag that is never reset (after a good dictation a later pack error would still be called 'installed but
    refused' and never offer Setup)."""
    out, _w = _run(tmp_path, w, "Masked", [
        {"until": _DICTATE},
        {"eval": "%s.click(); %s.click(); window.__ok = true; __done.res(true); 1" % (_MB, _SB)},
        {"until": _DICTATE},
        {"eval": "%s.click(); __recs[__recs.length - 1].emit('hello'); %s.click(); window.__ok = false; %s.click();"
                 " JSON.stringify({setup: %s.hidden, mic: %s.style.display, note: %s, refused: %s('refused')})" % (_MB, _MB, _MB, _SB, _MB, _NOTE, _WORD)},
    ], extra=_NOPROBE)
    got = json.loads(out["values"][4])
    assert got["setup"] is False and got["mic"] == "none" and got["refused"] not in got["note"], got


# a recognizer that fires `start` and THEN ends with a pack error (a cause install() cannot fix: an insecure origin, a policy block)
_START_THEN_REFUSE = """
window.__refuse = true;
class StartThenRefuse extends Setup {
  start() { window.__started.push({live: true}); if (this.onstart) this.onstart({}); if (window.__refuse) this.onerror({error: "service-not-allowed"}); }
}
"""


@needs_browser
def test_a_start_event_before_the_refusal_does_not_reopen_the_setup_loop(tmp_path, w):
    """DICT-FIX3 (L3 r3): probe:false, a recognizer that fires `start` and then ends service-not-allowed even after install()
    resolved true. Red with a `start` handler that clears the installed flag: Setup is offered again (the install -> Dictate ->
    refused -> Setup loop), and the 'installed, browser still refuses' line is missing."""
    out, _w = _run(tmp_path, w, "StartThenRefuse", [
        {"until": _DICTATE},
        {"eval": "%s.click(); %s.click(); __done.res(true); 1" % (_MB, _SB)},       # fail (after start) -> Setup -> install once -> true
        {"until": _DICTATE},
        {"eval": "%s.click(); JSON.stringify({setup: %s.hidden, mic: %s.style.display, inst: __inst.length, recs: __recs.length,"
                 " note: %s, want: %s('refused'), reason: LevainMic.dictateReason('service-not-allowed')})" % (_MB, _SB, _MB, _NOTE, _WORD)},
    ], pre=_START_THEN_REFUSE, extra=_NOPROBE)
    got = json.loads(out["values"][4])
    assert got["recs"] == 2, got                                                          # two Dictate taps, both reached start()
    assert got["setup"] is True and got["mic"] == "" and got["inst"] == 1, got           # Dictate stays, no second Setup, one install ever
    assert got["want"] and got["note"] == got["reason"] + " " + got["want"], got


@needs_browser
def test_a_real_result_clears_the_installed_flag_even_when_start_fires_first(tmp_path, w):
    """The other half: a recognizer that fires `start` then delivers a `result` proves audio flowed, so a later refusal is a new
    question and Setup is offered again. Red with: a flag that is never cleared (the refusal after a good dictation says
    'installed but refused' and offers no Setup)."""
    out, _w = _run(tmp_path, w, "StartThenRefuse", [
        {"until": _DICTATE},
        {"eval": "%s.click(); %s.click(); window.__refuse = false; __done.res(true); 1" % (_MB, _SB)},
        {"until": _DICTATE},
        {"eval": "%s.click(); __recs[__recs.length - 1].emit('hello'); %s.click(); window.__refuse = true; %s.click();"
                 " JSON.stringify({setup: %s.hidden, mic: %s.style.display, note: %s, refused: %s('refused'),"
                 " text: document.querySelector('.rv-input').value})" % (_MB, _MB, _MB, _SB, _MB, _NOTE, _WORD)},
    ], pre=_START_THEN_REFUSE, extra=_NOPROBE)
    got = json.loads(out["values"][4])
    assert got["text"] == "hello", got
    assert got["setup"] is False and got["mic"] == "none" and got["refused"] not in got["note"], got


@needs_browser
def test_an_insecure_page_gets_neither_dictate_nor_setup_and_the_note_says_why(tmp_path, w):
    """SpeechRecognition is [SecureContext] (Web Speech spec) and the microphone is https/localhost only. Red with: Dictate or Setup
    drawn on an insecure page, or no words. The secure page (headless Chrome on 127.0.0.1) is unchanged."""
    for probe in ("", _NOPROBE):
        out, _w = _run(tmp_path, w, "Setup", [
            {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({mic: document.querySelectorAll('.rv-mic:not(.rv-mic-stream)').length,"
                     " setup: document.querySelectorAll('.rv-setup').length, asks: __asks, note: %s, want: %s('insecure')})), 150))" % (_NOTE, _WORD)}],
            pre=_INSECURE + "window.__state = 'available';", extra=probe)
        got = json.loads(out["values"][1])
        assert got["mic"] == 0 and got["setup"] == 0 and got["asks"] == 0 and got["note"] == got["want"] != "", got
        assert "secure address" in got["note"] and "https" in got["note"] and "localhost" in got["note"] and "insecure" in got["note"], got
    out, _w = _run(tmp_path, w, "Setup", [{"until": _DICTATE}, {"eval": "JSON.stringify({note: %s})" % _NOTE}], pre="window.__state = 'available';")
    assert json.loads(out["values"][2]) == {"note": ""}


@needs_browser
def test_available_that_throws_synchronously_is_a_failed_ask_not_an_apparent_success(tmp_path, w):
    """Red with: a synchronous throw turned into a fulfilled null (the poll ends 'done' with Dictate shown, or install() then says
    'ready'). A poll that throws counts toward the limit like a rejection; an ask after a good install says nothing is ready."""
    out, _w = _run_x(tmp_path, w, "SyncPoll", [
        {"until": "%s && %s === %s('slow')" % (_OFFER, _NOTE, _WORD)},
        {"eval": "JSON.stringify({asks: __asks, inst: __inst.length})"}], extra=" setupPollMs: 2,")
    got = json.loads(out["values"][2])
    assert got["inst"] == 0 and got["asks"] >= 100, got
    out, _w = _run_x(tmp_path, w, "SyncAfterInstall", [
        {"until": _OFFER},
        {"eval": "%s.click(); __done.res(true); 1" % _SB},
        {"until": "__asks >= 2"},
        {"eval": "new Promise(r => setTimeout(() => r(JSON.stringify({note: %s, done: %s('done')})), 100))" % (_NOTE, _WORD)}])
    after = json.loads(out["values"][4])
    assert after["done"] and after["note"] != after["done"], after
