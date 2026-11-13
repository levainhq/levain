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
    # a pack the browser can fetch is offered, not mourned: Dictate stays hidden (never started) and a button offers the set up;
    # a browser that lists the pack but has no install() says so in the one table's words, and the button goes
    out, _w = _run(tmp_path, w, "Downloadable", [
        {"until": "document.querySelector('.rv-setup') && !document.querySelector('.rv-setup').hidden"},
        {"eval": "document.querySelector('.rv-setup').click(); JSON.stringify({mic: document.querySelector('.rv-mic:not(.rv-mic-stream)').style.display,"
                 " started: __started.length, hidden: document.querySelector('.rv-setup').hidden,"
                 " note: document.querySelector('.rv-sendnote').textContent, want: LevainMic.dictateSetup('noinstall')})"}])
    got = json.loads(out["values"][2])
    assert got["mic"] == "none" and got["started"] == 0 and got["hidden"] and got["note"] == got["want"] != "", got
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
