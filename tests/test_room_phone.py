"""RM-H (CH-U2): the room on a phone and its foreground voice. STRUCTURAL ONLY: headless Chrome clamps the window width
(spore-1066), so a 390 emulation proves the rules apply and nothing overflows, never how it looks. Residue (his iPhone): the layout in
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
           '<script src="/room_view.js"></script><script src="/rvc.js"></script>')


def _assets(boot):
    return {"/rvc.html": ("text/html; charset=utf-8", _STYLED.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}


def _boot(speech):
    return ('%s window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: %s,'
            ' headers: () => ({"X-Levain-Write-Token": "%s"})});' % (_FAKE, speech, TOKEN))


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
Object.defineProperty(Ignores.prototype, "processLocally", {get() { return false; }, set(v) {}, configurable: true});
class Unavailable extends Live { static available() { return Promise.resolve("unavailable"); } }
class Downloadable extends Live { static available() { return Promise.resolve("downloadable"); } }
class Installed extends Live { static available() { return Promise.resolve("available"); } }
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
    block = css[i:css.index(".rv .rv-stage", i)]
    assert "font-size: 16px" in block and "env(safe-area-inset-bottom" in block and "position: sticky" not in block   # the dock sits under the conversation, never over it (1104-29)
    assert "grid-template-columns: repeat(2" in block and ".rv-send { grid-column: 1 / -1" in block
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block)


def test_voice_never_names_a_cloud_recognizer_or_a_transcribe_route():
    js = (WEB / "room_view.js").read_text()
    for word in ("getUserMedia", "MediaRecorder", "audio/transcriptions", "api.openai.com"):
        assert word not in js, word
    assert 'Speech.prototype && "processLocally" in Speech.prototype' in js and "r.processLocally = true" in js


@needs_browser
def test_a_recognizer_that_cannot_stay_on_the_device_gets_no_control(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Cloud"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"},
            {"eval": "document.querySelectorAll('.rv-mic').length"},
        ])
    assert out["values"][1] == 0


@needs_browser
def test_an_on_device_recognizer_gets_a_control_that_asks_for_local_processing(tmp_path, w):
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-mic')"},
            {"eval": "document.querySelector('.rv-mic').click(); JSON.stringify(window.__started)"},
        ])
    assert json.loads(out["values"][1]) == [{"local": True}]


_GEOM = """(() => { const r = n => n.getBoundingClientRect();
const acts = [...document.querySelectorAll('.rv-actions > button')], dock = r(document.querySelector('.rv-dock'));
return JSON.stringify({noSideScroll: document.documentElement.scrollWidth <= window.innerWidth,
  inside: acts.every(b => r(b).left >= dock.left - 0.5 && r(b).right <= dock.right + 0.5),
  tall: acts.every(b => r(b).height >= 43.5), unclipped: acts.every(b => b.scrollWidth <= b.clientWidth + 1),
  sendWide: r(document.querySelector('.rv-send')).width > r(document.querySelector('.rv-drop')).width,
  rows: new Set(acts.map(b => Math.round(r(b).top))).size, n: acts.length,
  input16: parseFloat(getComputedStyle(document.querySelector('.rv-input')).fontSize),
  sticky: getComputedStyle(document.querySelector('.rv-dock')).position}); })()"""


@needs_browser
def test_the_dock_fits_a_390_phone_with_every_control_whole(tmp_path, w):
    """Structural only. The dock is in the flow below the conversation (1104-29), no longer sticky."""
    with _serve_room(tmp_path, w, extra_assets=_assets(_boot("Local"))) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [
            {"emulate": {"w": 390, "h": 800, "dpr": 2, "mobile": True}},
            {"until": "document.querySelector('.rv-mic') && document.querySelector('.rv-rotate')"},
            {"eval": _GEOM},
        ])
    g = json.loads(out["values"][1])
    assert g["noSideScroll"] and g["inside"] and g["tall"] and g["unclipped"] and g["sendWide"], g
    assert g["n"] == 5 and g["rows"] <= 3 and g["input16"] >= 16 and g["sticky"] != "sticky", g


# --- the voice, F1-F4 of the r1 findings -----------------------------------------------------------------------------

_N = [0]


def _run(tmp_path, w, speech, steps):
    _N[0] += 1
    with _serve_room(tmp_path, w, name="e%d" % _N[0], extra_assets=_assets(_boot(speech))) as (base, _h):
        return _drive(tmp_path, base + "/rvc.html", [
            {"until": "document.querySelector('.rv-input') && !document.querySelector('.rv-send').disabled"}] + steps), w


@needs_browser
def test_a_late_result_after_send_does_not_bring_the_sent_line_back(tmp_path, w):
    """F1, reproduced first: Send while dictating, then the recognizer fires a late result with the pre-send base."""
    out, w = _run(tmp_path, w, "Live", [
        {"eval": "document.querySelector('.rv-mic').click(); __recs[0].emit('hello there');"
                 " const ta = document.querySelector('.rv-input'); const was = ta.value;"
                 " document.querySelector('.rv-send').click(); __recs[0].emit('hello there again'); JSON.stringify({was, stops: __recs[0].stops})"},
        {"until": "document.querySelector('.rv-sendnote').textContent === 'Sent.'"},
        {"eval": "__recs[0].emit('late and unwanted'); JSON.stringify({v: document.querySelector('.rv-input').value,"
                 " label: document.querySelector('.rv-mic').textContent})"},
    ])
    assert json.loads(out["values"][1]) == {"was": "hello there", "stops": 1}
    assert json.loads(out["values"][3]) == {"v": "", "label": "Dictate"}
    assert [r[3] for r in w.lines()] == ["hello there"]


@needs_browser
def test_drop_also_ends_dictation_first(tmp_path, w):
    out, _w = _run(tmp_path, w, "Live", [
        {"eval": "document.querySelector('.rv-mic').click(); __recs[0].emit('for the desk');"
                 " document.querySelector('.rv-drop').click(); __recs[0].emit('and more'); JSON.stringify({stops: __recs[0].stops,"
                 " v: document.querySelector('.rv-input').value})"},
    ])
    assert json.loads(out["values"][1]) == {"stops": 1, "v": "for the desk"}


@needs_browser
def test_a_recognizer_that_ignores_processlocally_never_starts(tmp_path, w):
    """F2: the property exists on the prototype but the setter does nothing, so reading it back is the only proof."""
    out, _w = _run(tmp_path, w, "Ignores", [
        {"eval": "document.querySelector('.rv-mic').click(); JSON.stringify({started: __started.length,"
                 " note: document.querySelector('.rv-sendnote').textContent, label: document.querySelector('.rv-mic').textContent})"},
    ])
    got = json.loads(out["values"][1])
    assert got["started"] == 0 and got["label"] == "Dictate" and "keep dictation on this device" in got["note"], got


@needs_browser
def test_the_mic_follows_availability_not_capability(tmp_path, w):
    out, _w = _run(tmp_path, w, "Unavailable", [{"eval": "document.querySelectorAll('.rv-mic').length"}])
    assert out["values"][1] == 0
    out, _w = _run(tmp_path, w, "Downloadable", [
        {"until": "document.querySelector('.rv-sendnote').textContent"},
        {"eval": "JSON.stringify({mics: document.querySelectorAll('.rv-mic').length, started: __started.length,"
                 " note: document.querySelector('.rv-sendnote').textContent})"}])
    got = json.loads(out["values"][2])
    assert got["mics"] == 0 and got["started"] == 0 and "not installed" in got["note"] and "keyboard dictation key" in got["note"], got
    out, _w = _run(tmp_path, w, "Installed", [
        {"until": "document.querySelector('.rv-mic') && document.querySelector('.rv-mic').style.display !== 'none'"},
        {"eval": "document.querySelector('.rv-mic').click(); __recs[0].lang"}])
    assert out["values"][2] != ""            # lang is set explicitly from the document / browser language


@needs_browser
def test_dictation_errors_read_by_code(tmp_path, w):
    want = {"not-allowed": "permission", "service-not-allowed": "permission", "audio-capture": "No microphone",
            "no-speech": "Heard nothing", "language-not-supported": "not installed for this language", "network": "Dictation stopped."}
    for code, words in want.items():
        out, _w = _run(tmp_path, w, "Live", [
            {"eval": "window.__err = '%s'; document.querySelector('.rv-mic').click();"
                     " JSON.stringify({n: document.querySelector('.rv-sendnote').textContent, l: document.querySelector('.rv-mic').textContent})" % code}])
        got = json.loads(out["values"][1])
        assert words in got["n"] and got["l"] == "Dictate", (code, got)


@needs_browser
def test_typing_during_dictation_is_kept_and_the_next_result_extends_it(tmp_path, w):
    """F4: the next result must not overwrite what was typed in between."""
    out, _w = _run(tmp_path, w, "Live", [
        {"eval": "const ta = document.querySelector('.rv-input'); document.querySelector('.rv-mic').click(); __recs[0].emit('one two');"
                 " ta.value = ta.value + ' typed'; ta.dispatchEvent(new Event('input')); __recs[0].emit('one two three'); ta.value"},
    ])
    assert out["values"][1] == "one two typed three"
