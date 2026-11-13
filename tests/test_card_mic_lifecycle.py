"""Board row 140, fix round cmr1: the card mic's gate, lifecycle and reach.

A small page holds ``LevainMic.attach`` boxes on a real cockpit server in headless Chrome (the fake microphone and the
fetch recorder of ``tests/test_mic_page.py``, a recording speech recognizer of this file); the cockpit sign card's write-in
and the Needs card run on the real cockpit page. Each test names the line whose revert turns it red."""
from __future__ import annotations

import json
import re
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_room import _serve_room, w  # noqa: F401  (fixture)
from tests.test_cockpit_sign_card import DESKTOP, QRID, TOK, _Rig, needs_ssh
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_mic_page import _fake

MIC_JS = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "mic.js"
FEED = {"eval": "window.__feed(12000)"}

# a recognizer the page can watch: the on-device kind (processLocally lives on the prototype) that records its life, and
# delivers one last phrase when it is told to stop (as a real engine does)
RECOG = """
(() => {
  window.__srs = [];
  window.SpeechRecognition = class {
    get processLocally() { return true; } set processLocally(v) {}
    constructor() { window.__srs.push(this); this.starts = 0; this.stops = 0; }
    start() { this.starts++; }
    stop() { this.stops++; if (window.__tail) setTimeout(() => { this.onresult && this.onresult(window.__res([...(this.heard || []).filter((p) => p[1]), [window.__tail, true]])); this.onend && this.onend(); }, 60); }
  };
  delete window.webkitSpeechRecognition;
  // a result event as an engine sends it: [transcript, isFinal] per result, in order
  window.__res = (pairs) => ({ results: pairs.map(([t, f]) => Object.assign([{ transcript: t }], { isFinal: f })) });
  // recognizer r delivers a result event; its results list is the whole session's so far (as SpeechRecognitionEvent.results is)
  window.__say = (r, pairs) => { r.heard = pairs; r.onresult && r.onresult(window.__res(pairs)); };
  // the visibilitychange listeners mic.js holds on the document (every attach() with Dictate holds one until it is let go)
  window.__visN = 0;
  const add = Document.prototype.addEventListener, rm = Document.prototype.removeEventListener, held = new Set();
  Document.prototype.addEventListener = function (k, f, o) { if (k === "visibilitychange" && !held.has(f) && String(new Error().stack).indexOf("/mic.js") >= 0) { held.add(f); window.__visN++; } return add.call(this, k, f, o); };
  Document.prototype.removeEventListener = function (k, f, o) { if (k === "visibilitychange" && held.has(f)) { held.delete(f); window.__visN--; } return rm.call(this, k, f, o); };
})();
"""
# a Safari-shaped recognizer: no processLocally on the prototype (the engine ignores the property)
SAFARI = """
(() => { window.SpeechRecognition = class { start() {} stop() {} }; delete window.webkitSpeechRecognition; })();
"""

PAGE = ('<!doctype html><meta charset="utf-8"><body><div id="host"></div><script src="/mic.js"></script><script src="/cm.js"></script>')
BOOT = """
window.__m = {}; window.__notes = [];
window.__mk = (id) => {
  const t = document.createElement("textarea"); t.id = id; t.maxLength = 4000; document.getElementById("host").appendChild(t);
  const m = LevainMic.attach(t, { cls: "x", note: (s) => window.__notes.push(s) });
  for (const b of m.buttons) document.getElementById("host").appendChild(b);
  window.__m[id] = m; return 1;
};
"""


def _assets():
    return {"/cm.html": ("text/html; charset=utf-8", PAGE.encode()), "/cm.js": ("text/javascript; charset=utf-8", BOOT.encode())}


def _boxes(*ids):
    return [{"eval": "window.__mk(" + json.dumps(i) + ")"} for i in ids]


def _stream(i: int = 0) -> str:
    return f"document.querySelectorAll('.card-mic-stream')[{i}]"


def _dict(i: int = 0) -> str:
    return f"document.querySelectorAll('.card-mic-dictate')[{i}]"


def _page(tmp_path, w, steps, init=RECOG):
    with _serve_room(tmp_path, w, extra_assets=_assets()) as (base, _h):
        return _drive(tmp_path, base + "/cm.html", [{"init": _fake() + init}, *steps])


@needs_browser
def test_a_safari_shaped_recognizer_gets_no_dictate_button(tmp_path, w) -> None:
    """Red with: the shared on-device test left out of attach() (Safari's recognizer, which ignores processLocally, gets Dictate)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream()} && !{_stream()}.hidden && 1", "ms": 20000},
        {"eval": "document.querySelectorAll('.card-mic-dictate').length === 0 || [...document.querySelectorAll('.card-mic-dictate')].every((b) => b.hidden)"},
    ], init=SAFARI)
    assert out["values"][-1] is True, out["values"]


@needs_browser
def test_an_on_device_recognizer_still_gets_dictate(tmp_path, w) -> None:
    """The control for the test above: the gate keeps the on-device kind."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": "!!document.querySelector('.card-mic-dictate') && !document.querySelector('.card-mic-dictate').hidden"},
    ])
    assert out["values"][-1] is True, out["values"]


@needs_browser
def test_a_box_that_leaves_the_page_ends_its_mic_without_waiting_for_a_partial(tmp_path, w) -> None:
    """Red with: the detach check of attach() run only inside a partial (a silent room keeps the stream open to the ceiling)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream()} && !{_stream()}.hidden && 1", "ms": 20000},
        {"eval": f"{_stream()}.click(); 1"},
        {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
        FEED, {"until": "document.getElementById('a').value.indexOf('live 0') >= 0 && 1", "ms": 20000},
        {"eval": "document.getElementById('host').replaceChildren(); 1"},
        {"until": "window.__sc.closes.length === 1 && window.__sc.tracks.every((t) => t.stopped > 0) && 1", "ms": 6000},
        {"eval": "JSON.stringify([window.__sc.closes.length, window.__sc.tracks.map((t) => t.stopped)])"},
    ])
    assert json.loads(out["values"][-1])[0] == 1, out["values"]


@needs_browser
def test_off_ends_a_live_mic_and_its_box_takes_no_more_words(tmp_path, w) -> None:
    """Red with: off() only removing the listeners (the session it started keeps capturing)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream()} && !{_stream()}.hidden && 1", "ms": 20000},
        {"eval": f"{_stream()}.click(); 1"},
        {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
        FEED, {"until": "document.getElementById('a').value.indexOf('live 0') >= 0 && 1", "ms": 20000},
        {"eval": "window.__m.a.off(); 1"},
        {"until": "window.__sc.closes.length === 1 && 1", "ms": 6000},
        {"eval": "new Promise((r) => setTimeout(r, 400)).then(() => JSON.stringify([window.__m.a.busy(), window.__sc.tracks.every((t) => t.stopped > 0)]))"},
    ])
    assert json.loads(out["values"][-1]) == [False, True], out["values"]


@needs_browser
def test_locking_the_controls_keeps_a_live_stop_button_enabled(tmp_path, w) -> None:
    """Red with: lock() missing from the attach api (a caller disabling every button disables Stop mic under a live mic)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream()} && !{_stream()}.hidden && 1", "ms": 20000},
        {"eval": f"{_stream()}.click(); 1"},
        {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
        {"eval": f"window.__m.a.lock(true); JSON.stringify([{_stream()}.disabled, {_stream()}.getAttribute('aria-label')])"},
        {"eval": f"{_stream()}.click(); 1"},
        {"until": "window.__sc.closes.length === 1 && !window.__m.a.busy() && 1", "ms": 20000},
        {"eval": f"JSON.stringify([{_stream()}.disabled, {_stream()}.getAttribute('aria-label')])"},
    ])
    assert json.loads(out["values"][-4]) == [False, "Stop mic"], out["values"]
    assert json.loads(out["values"][-1]) == [True, "Mic"], out["values"]


@needs_browser
def test_stop_dictating_keeps_the_last_phrase_the_engine_delivers_after_stop(tmp_path, w) -> None:
    """Red with: the handlers of the recognizer cleared before stop() (the tail is dropped)."""
    out = _page(tmp_path, w, [
        {"eval": "window.__tail = ' and the tail'; 1"},
        *_boxes("a"),
        {"eval": f"{_dict()}.click(); window.__say(window.__srs[0], [['hello', true]]); 1"},
        {"eval": f"{_dict()}.click(); JSON.stringify([window.__m.a.busy(), document.getElementById('a').value])"},
        {"until": "document.getElementById('a').value.indexOf('and the tail') >= 0 && !window.__m.a.busy() && 1", "ms": 5000},
        {"eval": "document.getElementById('a').value"},
    ])
    assert json.loads(out["values"][-3]) == [True, "hello"], out["values"]      # still busy until the tail lands
    assert out["values"][-1] == "hello and the tail", out["values"]


@needs_browser
def test_stop_dictating_ends_even_if_the_engine_never_ends(tmp_path, w) -> None:
    """The wait for the tail is bounded: a recognizer that never fires onend does not hold busy() for ever."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": f"{_dict()}.click(); 1"},
        {"eval": f"{_dict()}.click(); 1"},
        {"until": "!window.__m.a.busy() && 1", "ms": 8000},
        {"eval": f"{_dict()}.getAttribute('aria-label')"},
    ])
    assert out["values"][-1] == "Dictate", out["values"]


@needs_browser
def test_starting_dictate_on_one_box_stops_the_capture_of_another(tmp_path, w) -> None:
    """Red with: the other boxes not halted at a Dictate start (two recognizers capture at once)."""
    out = _page(tmp_path, w, [
        *_boxes("a", "b"),
        {"eval": f"{_dict(0)}.click(); 1"},
        {"eval": f"{_dict(1)}.click(); JSON.stringify([window.__srs.length, window.__srs[0].stops, window.__srs[1].starts])"},
    ])
    assert json.loads(out["values"][-1]) == [2, 1, 1], out["values"]


@needs_browser
def test_starting_dictate_stops_another_boxs_live_stream(tmp_path, w) -> None:
    """Red with: Dictate starting under a live stream of another box (the stream keeps the microphone)."""
    out = _page(tmp_path, w, [
        *_boxes("a", "b"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream(0)} && !{_stream(0)}.hidden && 1", "ms": 20000},
        {"eval": f"{_stream(0)}.click(); 1"},
        {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
        {"eval": f"{_dict(1)}.click(); 1"},
        {"until": "window.__sc.closes.length === 1 && 1", "ms": 20000},
    ])
    assert out["values"][-1] == 1, out["values"]


# --- the cockpit sign card's own write-in -----------------------------------------------------------------------

SIGN_BOX = "document.querySelector('#levain-modal .needs-question .sign-write-in')"
SIGN_MIC = "document.querySelector('#levain-modal .needs-question .card-mic-stream')"
SIGN_WORDS = ("[...document.querySelectorAll('#levain-modal .needs-question .sign-option')]"
              ".find(b => /sign my words/.test(b.textContent))")
OPEN_Q = ("(document.querySelector('#levain-modal .needs-question') || "
          "((r) => r && (r.click(), null))(document.querySelector('#ck-needs .needs-row.needs-question')))")
SIGN_NOTE = f"(({OPEN_Q}, document.querySelector('#levain-modal .needs-question .sign-note')) || {{}}).textContent"


@needs_browser
@needs_ssh
def test_the_cockpit_sign_cards_write_in_gets_the_mic_and_refuses_a_tap_while_it_is_open(tmp_path: Path, monkeypatch) -> None:
    """Red with: the `attach(...)` call missing from the sign card's write-in block, or its click not refusing while busy()."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, {"init": _fake()}, rig.authenticator(),
            {"until": f"{OPEN_Q} && {SIGN_BOX} && !!{SIGN_MIC} && !{SIGN_MIC}.hidden && 1", "ms": 30000},
            {"eval": f"{SIGN_MIC}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": f"{SIGN_BOX}.value.indexOf('live 0') >= 0 && 1", "ms": 20000},
            {"tap": SIGN_WORDS},
            {"eval": f"new Promise((r) => setTimeout(r, 800)).then(() => JSON.stringify([{SIGN_NOTE}, {SIGN_BOX}.value]))"},
        ])
    note, value = json.loads(out["values"][-1])
    assert "Stop the mic first" in note and "live 0" in value, (note, value)
    assert not any("/answer" in p or "/challenge" in p for p in out["posts"]), out["posts"]
    assert rig.nothing_signed(QRID)


# --- the Needs card: an option tap under a live mic ------------------------------------------------------------

@needs_browser
def test_an_option_tap_under_a_live_mic_ends_the_mic_and_leaves_stop_enabled(tmp_path: Path, monkeypatch) -> None:
    """Red with: an option tap disabling every control, the mic buttons too (Stop mic dead under a live mic)."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    QuestionStore(home / "consent").file(agent="codex", agent_session_id="s3", what="Which way for the sweep?", blocks="the sweep waits", cwd="/w",
                                         options=[{"key": "a", "label": "left"}, {"key": "b", "label": "right"}], rec="a")
    _root, src, _ids = _install(tmp_path)
    qmic = "document.querySelector('#levain-modal .needs-question .card-mic-stream')"
    qbox = "document.querySelector('#levain-modal .needs-question .question-words')"
    opt = "document.querySelector('#levain-modal .needs-question .question-option')"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            DESKTOP, {"init": _fake()},
            {"until": "[...document.querySelectorAll('#ck-needs .needs-question')].length === 1", "ms": 30000},
            {"eval": "[...document.querySelectorAll('#ck-needs .needs-question')][0].click(); 1"},
            {"until": f"{qmic} && !{qmic}.hidden && 1", "ms": 30000},
            {"eval": f"{qmic}.click(); 1"},
            {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
            FEED, {"until": f"{qbox}.value.indexOf('live 0') >= 0 && 1", "ms": 20000},
            {"eval": f"(() => {{ const m = {qmic}, o = {opt}; o.click(); return JSON.stringify([m ? m.disabled : null]); }})()"},
            {"until": "window.__sc.closes.length === 1 && 1", "ms": 20000},
        ])
    assert json.loads(out["values"][-2]) == [False], out["values"]
    assert out["values"][-1] == 1


# --- cmr2 (L3 r1): only FINAL words stay in a box once its capture ends ---------------------------------------------

VAL = "document.getElementById('{}').value"


@needs_browser
def test_dictate_that_ends_on_an_interim_leaves_only_the_typed_and_final_words(tmp_path, w) -> None:
    """Red with: Dictate's onresult writing every result into the box with no isFinal test (the interim stays, busy() false)."""
    out = _page(tmp_path, w, [
        *_boxes("a", "b"),
        {"eval": "document.getElementById('a').value = 'typed'; document.getElementById('b').value = 'typed'; 1"},
        {"eval": f"{_dict(0)}.click(); window.__srs[0].onresult(window.__res([['ghost', false]])); {VAL.format('a')}"},
        {"eval": f"window.__srs[0].onend(); JSON.stringify([window.__m.a.busy(), {VAL.format('a')}])"},
        {"eval": f"{_dict(1)}.click(); window.__srs[1].onresult(window.__res([['kept', true], [' ghost', false]])); {VAL.format('b')}"},
        {"eval": f"window.__srs[1].onerror({{ error: 'network' }}); JSON.stringify([window.__m.b.busy(), {VAL.format('b')}])"},
    ])
    assert out["values"][-4] == "typed ghost", out["values"]                     # shown while capturing
    assert json.loads(out["values"][-3]) == [False, "typed"], out["values"]      # an interim-only end: the typed words alone
    assert out["values"][-2] == "typed kept ghost", out["values"]
    assert json.loads(out["values"][-1]) == [False, "typed kept"], out["values"]   # an error: typed + the final segment


@needs_browser
def test_stop_dictating_that_times_out_on_an_interim_drops_the_interim(tmp_path, w) -> None:
    """Red with: the tail timeout (finishDict) freeing the box without restoring it (the interim stays and can be signed)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": "document.getElementById('a').value = 'typed'; 1"},
        {"eval": f"{_dict()}.click(); window.__srs[0].onresult(window.__res([['ghost', false]])); 1"},
        {"eval": f"{_dict()}.click(); 1"},
        {"until": "!window.__m.a.busy() && 1", "ms": 8000},
        {"eval": VAL.format("a")},
    ])
    assert out["values"][-1] == "typed", out["values"]


def _break_after_a_partial(revoked: bool) -> list[dict]:
    status = 403 if revoked else 500
    return [
        *_boxes("a"),
        {"until": f"window.__sc.vj >= 1 && !!{_stream()} && !{_stream()}.hidden && 1", "ms": 20000},
        {"eval": f"document.getElementById('a').value = 'typed'; {_stream()}.click(); 1"},
        {"until": "window.__sc.gum >= 1 && !!window.__sc.node", "ms": 20000},
        FEED, {"until": "document.getElementById('a').value.indexOf('live 0') >= 0 && 1", "ms": 20000},
        {"eval": f"window.__sc.chunkRevoked = {str(revoked).lower()}; window.__sc.chunkStatus = {status}; window.__feed(12000)"},
        {"until": "!window.__m.a.busy() && 1", "ms": 20000},
        {"eval": f"JSON.stringify([{VAL.format('a')}, window.__notes])"},
    ]


@needs_browser
def test_a_stream_that_breaks_after_a_partial_keeps_only_the_committed_words(tmp_path, w) -> None:
    """Red with: the stream's `end` listener freeing the box as it stands (the partial's live words stay and can be signed)."""
    value, notes = json.loads(_page(tmp_path, w, _break_after_a_partial(False))["values"][-1])
    assert value == "typed said" and "live" not in value, (value, notes)   # `said` is what the server had committed
    assert any(n.startswith("Streaming dictation stopped") for n in notes), notes


@needs_browser
def test_a_revoked_stream_after_a_partial_keeps_only_the_committed_words(tmp_path, w) -> None:
    """Red with: the same `end` listener: a revoke ends the session with the partial left in the box."""
    value, notes = json.loads(_page(tmp_path, w, _break_after_a_partial(True))["values"][-1])
    assert value == "typed said" and "live" not in value, (value, notes)
    assert any(n.startswith("Premium voice was turned off") for n in notes), notes


# --- cmr2: an idle mic is let go with its box ----------------------------------------------------------------------

@needs_browser
def test_an_idle_box_that_leaves_the_page_lets_go_of_its_listeners(tmp_path, w) -> None:
    """Red with: the detach watch run only while a capture is live (an idle box keeps its document listener for ever)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": "window.__visN"},
        {"eval": "document.getElementById('host').replaceChildren(); 1"},
        {"eval": "new Promise((r) => setTimeout(r, 1500)).then(() => window.__visN)"},
    ])
    assert out["values"][-3] == 1 and out["values"][-1] == 0, out["values"]


@needs_browser
def test_a_box_that_never_reaches_the_page_lets_go_of_its_listeners(tmp_path, w) -> None:
    """Red with: no bound on a box that is never connected (a draw that lands after its card closed keeps its listeners)."""
    out = _page(tmp_path, w, [
        {"eval": "window.__orph = LevainMic.attach(document.createElement('textarea'), {}); window.__visN"},
        {"eval": "new Promise((r) => setTimeout(r, 7000)).then(() => window.__visN)"},
    ])
    assert out["values"][-2] == 1 and out["values"][-1] == 0, out["values"]


SIGN_DICT = "document.querySelector('#levain-modal .needs-question .card-mic-dictate')"
SIGN_OPT = ("[...document.querySelectorAll('#levain-modal .needs-question .sign-option')]"
            ".find(b => !/sign my words/.test(b.textContent))")


@needs_browser
@needs_ssh
def test_closing_the_cockpit_card_lets_go_of_its_idle_write_in_mic(tmp_path: Path, monkeypatch) -> None:
    """Red with: the detach watch run only while a capture is live (the closed modal's idle mic keeps its listener)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, {"init": _fake() + RECOG}, rig.authenticator(),
            {"until": f"{OPEN_Q} && {SIGN_BOX} && !!{SIGN_DICT} && 1", "ms": 30000},
            {"eval": f"window.__wb = {SIGN_BOX}; window.__visN"},
            {"eval": "document.querySelector('#levain-modal .modal-close').click(); 1"},
            {"eval": "new Promise((r) => setTimeout(r, 1500)).then(() => JSON.stringify([!!document.getElementById('levain-modal'), window.__visN, window.__wb.isConnected]))"},
        ])
    before = out["values"][-3]
    gone, after, connected = json.loads(out["values"][-1])
    assert before >= 1 and not gone and not connected and after == before - 1, out["values"]


@needs_browser
@needs_ssh
def test_a_cockpit_ceremony_drops_dictates_tail_so_the_write_in_is_not_written_after_the_tap(tmp_path: Path, monkeypatch) -> None:
    """Red with: run() calling stop() on a live Dictate (its tail lands in the write-in after the ceremony started)."""
    rig = _Rig(tmp_path, monkeypatch)
    with rig.serving():
        out = _drive(tmp_path, f"{rig.origin}/#write_token={TOK}", [
            DESKTOP, {"init": _fake() + RECOG}, rig.authenticator(),
            {"until": f"{OPEN_Q} && {SIGN_BOX} && !!{SIGN_DICT} && 1", "ms": 30000},
            {"eval": f"window.__tail = ' and the tail'; window.__wb = {SIGN_BOX}; {SIGN_DICT}.click(); "
                     "window.__say(window.__srs[0], [['hello', true]]); window.__wb.value"},
            {"tap": SIGN_OPT},
            {"eval": "new Promise((r) => setTimeout(r, 800)).then(() => JSON.stringify([window.__wb.value, window.__srs[0].stops]))"},
        ])
    assert out["values"][-3] == "hello", out["values"]
    value, stops = json.loads(out["values"][-1])
    assert value == "hello" and stops >= 1, (value, stops)


# --- 11-10 (row 185): a Dictate error says its own reason on the card ----------------------------------------------

SERVICE_LANG = "Speech recognition could not run for this language: its on-device pack may be missing, or this browser blocks it."
DICTATE_WORDS = {
    "not-allowed": "Dictation needs the microphone: allow it for this site in the address bar, then tap Dictate again.",
    "service-not-allowed": SERVICE_LANG,
    "language-not-supported": SERVICE_LANG,
    "audio-capture": "No microphone was found, or it could not be opened.",
    "network": "Speech recognition failed; tap Dictate to try again.",
    "no-speech": "No speech was heard; tap Dictate and try again.",
    "aborted": "Dictation was interrupted; tap Dictate to try again.",
    "bad-grammar": "Dictation could not use its settings.",
    "phrases-not-supported": "Dictation could not use its hint phrases in this browser.",
}
START_WORDS = "Dictation could not start; tap Dictate again."
# the widest sentence the table held before the words were corrected: the card's note must not grow past what 390 was shown to hold
WIDEST_OLD = 124


def test_the_word_table_keys_and_widths_match_the_js_table() -> None:
    """Red with: a reason added to (or dropped from) mic.js's DICTATE_REASONS without this file's table moving; a JS sentence
    wider than the widest the card was shown to hold at 390; a phrase the words were corrected to lose coming back."""
    js = MIC_JS.read_text()
    block = js[js.index("var DICTATE_REASONS = {"):js.index("};", js.index("var DICTATE_REASONS = {"))]
    table = dict(re.findall(r'^\s+"([a-z-]+)": "((?:[^"\\]|\\.)*)",', block, re.M))
    assert set(table) == set(DICTATE_WORDS) | {"start"}, sorted(table)
    for name, words in table.items():
        assert len(words) <= WIDEST_OLD, (name, len(words))
        assert " yet" not in words and "keyboard dictation key" not in words and "turned off" not in words, (name, words)


def test_the_on_device_refusal_sentence_outside_the_table_loses_the_removed_claim() -> None:
    """Red with: the processLocally refusal in the click handler (spoken through tell(), outside DICTATE_REASONS) telling the
    operator the keyboard dictation key works (Dictate shows only on desktop browsers) or that dictation is 'off' in a way the
    browser never said. The table scan above cannot see this sentence."""
    js = MIC_JS.read_text()
    line = next(ln for ln in js.splitlines() if "processLocally !== true" in ln and "tell(" in ln)
    words = re.search(r'tell\("((?:[^"\\]|\\.)*)"\)', line).group(1)
    assert "keyboard dictation key" not in words and " yet" not in words, words
    assert words.endswith("so Dictate is off."), words


LAST = "window.__srs[window.__srs.length - 1]"
LAST_NOTE = "window.__notes[window.__notes.length - 1]"


@needs_browser
def test_each_dictate_error_event_says_its_own_reason(tmp_path, w) -> None:
    """Red with: onerror folding every error but not-allowed into "Dictation stopped." (language-not-supported hidden)."""
    names = list(DICTATE_WORDS) + ["something-new"]
    steps = [*_boxes("a")]
    for n in names:
        steps.append({"eval": f"{_dict()}.click(); {LAST}.onerror({{ error: {json.dumps(n)} }}); JSON.stringify([{LAST_NOTE}, window.__m.a.busy()])"})
    out = _page(tmp_path, w, steps)
    got = [json.loads(v) for v in out["values"][-len(names):]]
    for n, (note, busy) in zip(names, got):
        assert busy is False, (n, got)
        assert note == DICTATE_WORDS.get(n, "Dictation stopped (something-new)."), (n, note)


@needs_browser
def test_a_dictate_start_that_throws_says_so(tmp_path, w) -> None:
    """Red with: the start() catch freeing the box with no note (the tap does nothing, silently); or with no abort() of the
    recognizer that threw (a half-started engine is left live behind a freed box)."""
    thrower = RECOG + "\n(() => { const C = window.SpeechRecognition; window.SpeechRecognition = class extends C { start() { throw new DOMException('x', 'InvalidStateError'); } abort() { window.__aborts = (window.__aborts || 0) + 1; } }; })();"
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": f"{_dict()}.click(); JSON.stringify([{LAST_NOTE}, window.__m.a.busy(), window.__aborts || 0])"},
    ], init=thrower)
    assert json.loads(out["values"][-1]) == [START_WORDS, False, 1], out["values"]


@needs_browser
def test_a_start_that_throws_says_one_thing_even_when_abort_answers_at_once(tmp_path, w) -> None:
    """Red with: the start() catch aborting before it detaches (abort() answers with an error and an end event on the spot, and
    the box says "interrupted" and then "could not start"); or with no abort() at all."""
    answers = RECOG + ("\n(() => { const C = window.SpeechRecognition; window.SpeechRecognition = class extends C { start() { throw new DOMException('x', 'InvalidStateError'); }"
                      " abort() { window.__aborts = (window.__aborts || 0) + 1; this.onerror && this.onerror({ error: 'aborted' }); this.onend && this.onend(); } }; })();")
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": f"{_dict()}.click(); JSON.stringify([window.__notes, window.__m.a.busy(), window.__aborts || 0, document.getElementById('a').readOnly])"},
    ], init=answers)
    notes, busy, aborts, readonly = json.loads(out["values"][-1])
    assert notes == [START_WORDS] and busy is False and aborts == 1 and readonly is False, out["values"]


@needs_browser
def test_a_new_dictate_attempt_clears_the_last_reason_and_only_dictates_own(tmp_path, w) -> None:
    """Red with: the click path never clearing the previous reason (the box says "No speech was heard" while dictation works); or
    clearing the note line blind (a first tap, with nothing of Dictate's on it, would wipe a message that is the host's)."""
    out = _page(tmp_path, w, [
        *_boxes("a"),
        {"eval": f"{_dict()}.click(); window.__notes.length"},
        {"eval": f"{LAST}.onerror({{ error: 'no-speech' }}); {LAST_NOTE}"},
        {"eval": f"{_dict()}.click(); JSON.stringify([window.__srs.length, {LAST_NOTE}, window.__m.a.busy(), window.__notes.length])"},
        {"eval": f"{_dict()}.click(); {_dict()}.click(); window.__notes.length"},
    ])
    assert out["values"][-4] == 0, out["values"]                                  # a first tap says nothing: no blind clear
    assert out["values"][-3] == DICTATE_WORDS["no-speech"], out["values"]
    assert json.loads(out["values"][-2]) == [2, "", True, 2], out["values"]       # the reason, then its clearing
    assert out["values"][-1] == 2, out["values"]                                  # the clearing is said once, not on every tap


@needs_browser
def test_an_error_after_the_operators_own_stop_stays_silent_for_no_speech_and_aborted_only(tmp_path, w) -> None:
    """Red with: the stop-tail guard dropped (Stop answered with no-speech reads as a failure the operator did not cause), or
    widened to every error name (a real failure after Stop goes unsaid). INFERRED browser behaviour, not measured."""
    def after_stop(name: str) -> dict:
        return {"eval": f"(() => {{ {_dict()}.click(); {_dict()}.click(); const n = window.__notes.length; {LAST}.onerror({{ error: {json.dumps(name)} }});"
                        f" return JSON.stringify([window.__notes.length - n, {LAST_NOTE}, window.__m.a.busy()]); }})()"}
    names = ["no-speech", "aborted", "network", "not-allowed", "audio-capture", "service-not-allowed"]
    out = _page(tmp_path, w, [*_boxes("a"), *[after_stop(n) for n in names]])
    got = dict(zip(names, (json.loads(v) for v in out["values"][-len(names):])))
    for n in ("no-speech", "aborted"):
        assert got[n][0] == 0 and got[n][2] is False, (n, got[n])      # nothing said, the box freed
    for n in names[2:]:
        assert got[n][0] == 1 and got[n][1] == DICTATE_WORDS[n] and got[n][2] is False, (n, got[n])
