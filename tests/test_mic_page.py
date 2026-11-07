"""Cockpit mic S4: streaming dictation capture (levain/templates/web/mic.js + mic_worklet.js, the Mic control in room_view.js).

Everything runs on the REAL cockpit page served by ``levain serve`` over the room rig (``tests/test_voice_room_hook.py``). The
microphone, the audio context and the AudioWorklet are FAKES installed before the page's scripts (``getUserMedia`` hands back a
stream whose tracks count their ``stop()``; the worklet node's port is driven by the test with Float32 blocks at 48 kHz) and the
page's fetch answers ``GET /cockpit/voice.json`` and the three ``/cockpit/voice/stream/*`` routes from a recorder. No audio
device, no provider and no server stream route is reached. What only a real iPhone can show (the permission prompt in a Safari tab
and in the Home Screen app, worklet cadence, screen lock, a call, a route change) is residue these tests cannot judge.

Each test names the line whose revert turns it red."""
from __future__ import annotations

import http.client
import json

from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_voice_room_hook import _op, _room, _run, _set, _to_room, needs_browser

FAKE = """
(() => {
  const FLAG = __FLAG__, DENY = __DENY__, OPEN_STATUS = __OPEN__, WORKLET = __WORKLET__, REAL = __REAL__;
  const sc = window.__sc = { vj: 0, open: [], chunks: [], closes: [], gum: 0, tracks: [], ctxClosed: 0, mods: [], node: null, sp: null,
    hang: { open: false, chunk: false, close: false }, chunkStatus: 0, closeDelay: 0, gumHold: false, modHold: false, resumeMode: '', vjFail: __VJFAIL__, sends: [], srStarts: 0, realKeys: null };
  const hang = (o) => new Promise((res, rej) => { if (o && o.signal) o.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); });
  const j = (o, s) => Promise.resolve(new Response(JSON.stringify(o), { status: s || 200, headers: { 'Content-Type': 'application/json' } }));
  const f0 = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const url = new URL(String(u), location.href), p = url.pathname, tok = (o && o.headers && o.headers['X-Levain-Write-Token']) || '';
    if (p === '/cockpit/voice.json') {
      sc.vj++;
      if (sc.vjFail > 0) { sc.vjFail--; return Promise.reject(new TypeError('net')); }
      if (REAL) return f0(u, o).then((r) => { r.clone().json().then((b) => { sc.realKeys = Object.keys(b); }); return r; });
      return j({ stream: FLAG, premium_stt: FLAG, authority: 'ok', estimate: true, prices_as_of: 'x', caps: {}, providers: [
        { name: 'deepgram', uses: ['stt'], key: 'set', record_id: 'vk-1', last_read: null, voices: [], default_voice: '', tts_model: '', speed: [0.5, 1.5], retention: 'r' }] });
    }
    if (p === '/cockpit/voice/stream/open') {
      sc.open.push({ q: url.search, tok: tok, body: o && o.body !== undefined ? 'has-body' : null });
      if (sc.hang.open) return hang(o);
      return OPEN_STATUS === 200 ? j({ sid: 'sid-1' }) : j({ error: 'no' }, OPEN_STATUS);
    }
    if (p === '/cockpit/voice/stream/chunk') {
      const n = sc.chunks.length;
      sc.chunks.push({ seq: url.searchParams.get('seq'), sid: url.searchParams.get('sid'), bytes: o.body.byteLength, isBuf: o.body instanceof ArrayBuffer, tok: tok,
        nonzero: new Int16Array(o.body).some((x) => x !== 0), ct: o.headers['Content-Type'] });
      if (sc.hang.chunk) return hang(o);
      if (sc.chunkStatus) return j({ error: 'x' }, sc.chunkStatus);
      return j({ seq: n, partial: 'live ' + n + ' <b>x</b>', final: 'said' });
    }
    if (p === '/cockpit/voice/stream/close') { sc.closes.push({ sid: url.searchParams.get('sid'), tok: tok });
      if (sc.hang.close) return hang(o);
      if (sc.closeDelay) return new Promise((r) => setTimeout(() => r(j({ text: 'the final text' })), sc.closeDelay));
      return j({ text: 'the final text' }); }
    if (p === '/cockpit/room/send') { sc.sends.push(JSON.parse(o.body).body); return j({ receipt: { id: 1 } }); }
    return f0(u, o);
  };
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: (c) => {
    sc.gum++; sc.constraints = c;
    if (DENY) return Promise.reject(new DOMException('denied', 'NotAllowedError'));
    const mk = () => { const t = { stopped: 0, stop() { this.stopped++; } }; sc.tracks.push(t); return { getTracks: () => [t] }; };
    if (sc.gumHold) return new Promise((res) => { sc.gumResolve = () => res(mk()); });
    return Promise.resolve(mk());
  } } });
  window.AudioContext = class {
    constructor() { this.sampleRate = 48000; this.destination = {}; sc.ctx = this;
      if (WORKLET) this.audioWorklet = { addModule: (u) => { sc.mods.push(u); return sc.modHold ? new Promise((r) => { sc.modResolve = r; }) : Promise.resolve(); } }; }
    resume() { if (sc.resumeMode === 'throw') throw new Error('x'); return sc.resumeMode === 'reject' ? Promise.reject(new Error('x')) : Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createScriptProcessor() { const n = { connect() {}, disconnect() {}, onaudioprocess: null }; sc.sp = n; return n; }
    close() { sc.ctxClosed++; return Promise.resolve(); }
  };
  if (WORKLET) window.AudioWorkletNode = class { constructor(c, name) { this.port = { onmessage: null }; sc.node = this; sc.nodeName = name; } connect() {} disconnect() {} };
  else delete window.AudioWorkletNode;
  // feed n samples of a 440 Hz tone at 48 kHz, in 128-frame blocks (worklet) or 4096-frame buffers (script processor)
  window.__feed = (n) => {
    const size = WORKLET ? 128 : 4096; let t = sc.t || 0;
    for (let off = 0; off < n; off += size) {
      const len = Math.min(size, n - off), b = new Float32Array(len);
      for (let i = 0; i < len; i++, t++) b[i] = 0.5 * Math.sin(2 * Math.PI * 440 * t / 48000);
      if (WORKLET) sc.node.port.onmessage({ data: b }); else sc.sp.onaudioprocess({ inputBuffer: { getChannelData: () => b } });
    }
    sc.t = t; return 1;
  };
  window.SpeechRecognition = class { get processLocally() { return true; } set processLocally(v) {} start() { sc.srStarts++; } stop() {} };
  window.__vis = (v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v }); document.dispatchEvent(new Event('visibilitychange')); return 1; };
})();
"""


def _fake(flag="true", deny="false", open_status="200", worklet="true", real="false", vjfail="0"):
    return FAKE.replace("__REAL__", real).replace("__VJFAIL__", vjfail).replace("__FLAG__", flag).replace("__DENY__", deny).replace("__OPEN__", open_status).replace("__WORKLET__", worklet)


MIC = "document.querySelector('.rv-mic-stream')"
SHOWN = f"!!{MIC} && !{MIC}.hidden"
TA = "document.querySelector('.rv-input')"
STATE = "JSON.stringify({sc: window.__sc, ta: " + TA + ".value, label: (" + MIC + " || {}).textContent, note: (document.querySelector('.rv-sendnote') || {}).textContent})"


def _go(tmp_path, qdir, steps, **kw):  # noqa: F811
    return _run(tmp_path, qdir, [_to_room(), *steps], init_extra=_fake(**kw))


def _state(v):
    return json.loads(v)


@needs_browser
def test_the_assets_are_served_and_the_page_loads_the_module(tmp_path) -> None:
    """Revert: remove the "/mic.js" or "/mic_worklet.js" row of web_server._ASSETS (404: the worklet cannot load), or the
    script tag from dashboard.html (no LevainMic, no button)."""
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        host = base.split("//")[1]
        c = http.client.HTTPConnection(host, timeout=20)
        for path, needle in (("/mic.js", "/cockpit/voice/stream/"), ("/mic_worklet.js", "registerProcessor")):
            c.request("GET", path)
            r = c.getresponse(); body = r.read().decode()
            assert r.status == 200 and r.getheader("Content-Type").startswith("text/javascript") and needle in body, (path, r.status)
        c.request("GET", "/")
        r = c.getresponse(); page = r.read().decode()
        assert "/mic.js" in page


@needs_browser
def test_chunks_arrive_in_order_at_16k_pcm16_with_the_token_and_stop_releases_everything(tmp_path, qdir) -> None:
    """28800 samples at 48 kHz = 9600 at 16 kHz = two 8000-byte chunks (250 ms of PCM16 mono) and a 3200-byte tail on stop.
    Red with: the resampler removed (chunk sizes follow the 48 kHz input: 3 chunks of 8000, not 2), seq not incremented (both
    chunks carry 0), `s.q` chaining removed, the token header dropped from headers(), a track left running (tracks stopped 0),
    ctx.close() removed, the close POST not sent from stop(), or a transcript drawn with innerHTML (the <b>x</b> test)."""
    v = _go(tmp_path, qdir, [
        {"until": SHOWN, "ms": 20000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.node && window.__sc.open.length === 1"},
        {"eval": "window.__feed(28800)"},
        {"until": "window.__sc.chunks.length === 2 && " + TA + ".value.indexOf('live 1') >= 0", "ms": 20000},
        {"eval": STATE},
        {"eval": MIC + ".click(); 1"},
        {"until": "window.__sc.closes.length === 1 && " + TA + ".value === 'the final text'", "ms": 20000},
        {"eval": "new Promise((r) => setTimeout(r, 300)).then(() => " + STATE + ")"},
    ])
    mid, end = _state(v[-4]), _state(v[-1])
    sc = mid["sc"]
    assert sc["open"] == [{"q": "?provider=deepgram", "tok": TOKEN, "body": None}], sc["open"]
    assert [c["seq"] for c in sc["chunks"]] == ["0", "1"] and {c["sid"] for c in sc["chunks"]} == {"sid-1"}, sc["chunks"]
    assert [c["bytes"] for c in sc["chunks"]] == [8000, 8000] and all(c["isBuf"] and c["nonzero"] and c["tok"] == TOKEN for c in sc["chunks"]), sc["chunks"]
    assert sc["chunks"][0]["ct"] == "application/octet-stream" and sc["mods"] == ["/mic_worklet.js"] and sc["nodeName"] == "levain-mic-tap"
    assert "said live 1 <b>x</b>" in mid["ta"] and mid["label"] == "Stop mic", mid       # drawn as text (a value), never markup
    sc = end["sc"]
    assert [c["seq"] for c in sc["chunks"]] == ["0", "1", "2"] and 3190 <= sc["chunks"][2]["bytes"] <= 3200, sc["chunks"]
    assert sc["closes"] == [{"sid": "sid-1", "tok": TOKEN}] and end["ta"] == "the final text" and end["label"] == "Mic", end
    assert [t["stopped"] for t in sc["tracks"]] == [1] and sc["ctxClosed"] == 1 and sc["gum"] == 1, sc
    assert sc["constraints"]["audio"], sc["constraints"]


@needs_browser
def test_a_hidden_page_stops_the_mic_and_closes_the_session(tmp_path, qdir) -> None:
    """Red with: the visibilitychange listener not added in start() (tracks stay running, no close) or not removed in release."""
    v = _go(tmp_path, qdir, [
        {"until": SHOWN, "ms": 20000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.node && window.__sc.open.length === 1"},
        {"eval": "window.__feed(20000)"},
        {"until": "window.__sc.chunks.length >= 1"},
        {"eval": "window.__vis('hidden')"},
        {"until": "window.__sc.closes.length === 1", "ms": 20000},
        {"eval": "new Promise((r) => setTimeout(r, 300)).then(() => " + STATE + ")"},
    ])
    sc = _state(v[-1])["sc"]
    assert [t["stopped"] for t in sc["tracks"]] == [1] and sc["ctxClosed"] == 1 and len(sc["closes"]) == 1, sc


@needs_browser
def test_no_button_when_the_server_does_not_advertise_streaming(tmp_path, qdir) -> None:
    """Red with: the button drawn unhidden (shown without the probe), or `j.stream === true` loosened to truthy-any."""
    v = _go(tmp_path, qdir, [
        {"until": "window.__sc.vj >= 1", "ms": 20000},
        {"eval": "new Promise((r) => setTimeout(r, 800)).then(() => JSON.stringify([!!" + MIC + " && !" + MIC + ".hidden, LevainMic.available()]))"},
    ], flag="false")
    assert json.loads(v[-1]) == [False, False], v[-1]


@needs_browser
def test_a_refused_microphone_says_the_fixed_sentence_opens_nothing_and_leaves_the_button_usable(tmp_path, qdir) -> None:
    """Red with: the open POST sent before the permission is granted (open has 1 entry), the note not emitted on a rejected
    getUserMedia, or the label left at "Stop mic"."""
    v = _go(tmp_path, qdir, [
        {"until": SHOWN, "ms": 20000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.gum === 1"},
        {"until": "(" + MIC + ".textContent === 'Mic') && document.querySelector('.rv-sendnote').textContent.length > 0", "ms": 20000},
        {"eval": STATE},
    ], deny="true")
    st = _state(v[-1])
    assert st["note"] == "Streaming dictation could not start; the Dictate control and the keyboard dictation key still work.", st["note"]
    assert st["sc"]["open"] == [] and st["sc"]["chunks"] == [] and st["sc"]["closes"] == [] and st["label"] == "Mic", st


@needs_browser
def test_a_server_refused_open_releases_the_mic_and_says_the_same_sentence(tmp_path, qdir) -> None:
    """Red with: the open failure not releasing the tracks (stopped 0) or not emitting the note."""
    v = _go(tmp_path, qdir, [
        {"until": SHOWN, "ms": 20000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.open.length === 1"},
        {"until": "window.__sc.tracks.length === 1 && window.__sc.tracks[0].stopped === 1 && " + MIC + ".textContent === 'Mic'", "ms": 20000},
        {"eval": STATE},
    ], open_status="503")
    st = _state(v[-1])
    assert st["note"].startswith("Streaming dictation could not start;") and st["sc"]["chunks"] == [] and st["sc"]["closes"] == [], st
    assert st["sc"]["ctxClosed"] == 1, st


@needs_browser
def test_without_an_audioworklet_the_script_processor_path_gives_the_same_chunks(tmp_path, qdir) -> None:
    """Red with: the fallback branch missing (start() throws on ctx.audioWorklet) or feeding the chunker a live buffer."""
    v = _go(tmp_path, qdir, [
        {"until": SHOWN, "ms": 20000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.sp && window.__sc.open.length === 1"},
        {"eval": "window.__feed(28800)"},
        {"until": "window.__sc.chunks.length === 2", "ms": 20000},
        {"eval": STATE},
    ], worklet="false")
    sc = _state(v[-1])["sc"]
    assert [c["seq"] for c in sc["chunks"]] == ["0", "1"] and [c["bytes"] for c in sc["chunks"]] == [8000, 8000] and sc["mods"] == [], sc


LIVE = [{"until": SHOWN, "ms": 20000}]
ROOM = [_set(_room("T1", _op(1, "hi"))), {"until": TA + " && !" + TA + ".disabled", "ms": 20000}]
CLICK = {"eval": MIC + ".click(); 1"}
DICT = "document.querySelector('.rv-mic:not(.rv-mic-stream)')"


def _wait(ms, expr=STATE):
    return {"eval": "new Promise((r) => setTimeout(r, %d)).then(() => %s)" % (ms, expr)}


@needs_browser
def test_a_permission_that_resolves_after_stop_stops_the_late_tracks_and_opens_nothing(tmp_path, qdir) -> None:
    """Revert: the late-stream branch in start() (the stale check after getUserMedia that stops the new tracks directly).
    Red before the fix: the late track was never stopped (stopped 0) and the mic stayed live."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.gumHold = true; 1"},
        {"do": MIC + ".click(); 1", "until": "window.__sc.gum === 1"},
        CLICK,
        {"until": MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 20000},
        {"eval": "window.__sc.gumResolve(); 1"},
        _wait(500),
    ])
    st = _state(v[-1])
    assert [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["sc"]["open"] == [] and st["sc"]["closes"] == [] and st["label"] == "Mic", st


@needs_browser
def test_a_worklet_module_that_loads_after_stop_arms_no_timer_and_opens_no_server_session(tmp_path, qdir) -> None:
    """Revert: the stale check after addModule in start() (the continuation went on to POST /open although stopped)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.modHold = true; 1"},
        {"do": MIC + ".click(); 1", "until": "window.__sc.mods.length === 1"},
        CLICK,
        {"until": MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 20000},
        {"eval": "window.__sc.modResolve(); 1"},
        _wait(500),
    ])
    st = _state(v[-1])
    assert st["sc"]["open"] == [] and st["sc"]["closes"] == [] and [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["sc"]["node"] is None, st


@needs_browser
def test_an_open_that_never_resolves_ends_the_session_at_its_deadline_and_releases_the_mic(tmp_path, qdir) -> None:
    """Revert: the deadline/abort in postJson (S stays non-null, label stuck on "Stop mic")."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.hang.open = true; LevainMic.deadlines.open = 300; 1"},
        CLICK,
        {"until": "window.__sc.open.length === 1 && " + MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 5000},
        _wait(100),
    ])
    st = _state(v[-1])
    assert [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["note"].startswith("Streaming dictation could not start"), st


@needs_browser
def test_a_chunk_that_never_resolves_ends_the_session_with_exactly_one_close_and_the_label_back(tmp_path, qdir) -> None:
    """Revert: the chunk deadline (the session wedges), or finish waiting on the best-effort close (finding 6: the close here
    hangs for 5 s and the label must come back long before)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.hang.chunk = true; window.__sc.hang.close = true; LevainMic.deadlines.chunk = 300; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(12000)"},
        {"until": MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 2500},
        _wait(200),
    ])
    st = _state(v[-1])
    assert len(st["sc"]["closes"]) == 1 and [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["note"].startswith("Streaming dictation stopped"), st


@needs_browser
def test_a_failed_chunk_ends_without_waiting_for_the_close(tmp_path, qdir) -> None:
    """Revert: finish(s) moved back behind closeSession in the chunk-failure path (label would wait the 5 s close deadline)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.chunkStatus = 500; window.__sc.hang.close = true; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(12000)"},
        {"until": MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 2000},
        _wait(100),
    ])
    st = _state(v[-1])
    assert len(st["sc"]["closes"]) == 1 and st["sc"]["ctxClosed"] == 1, st


@needs_browser
def test_a_close_that_never_resolves_still_ends_the_session(tmp_path, qdir) -> None:
    """Revert: the close deadline (stop() leaves S non-null for ever)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.hang.close = true; LevainMic.deadlines.close = 300; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(12000)"}, {"until": "window.__sc.chunks.length === 1"},
        CLICK,
        {"until": MIC + ".textContent === 'Mic' && !LevainMic.active() && window.__sc.closes.length === 1", "ms": 3000},
        _wait(200),
    ])
    assert len(_state(v[-1])["sc"]["closes"]) == 1


@needs_browser
def test_send_while_dictating_sends_the_final_text_not_the_partial(tmp_path, qdir) -> None:
    """Revert: Send awaiting LM.stop() in room_view (it sent the partial draft at once; the close text was lost)."""
    v = _go(tmp_path, qdir, [
        *LIVE, *ROOM, {"eval": "window.__sc.closeDelay = 400; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(12000)"}, {"until": TA + ".value.indexOf('live 0') >= 0", "ms": 20000},
        {"eval": "document.querySelector('.rv-send').click(); 1"},
        {"until": "window.__sc.sends.length >= 1", "ms": 20000},
        _wait(300),
    ])
    assert _state(v[-1])["sc"]["sends"] == ["the final text"], v[-1]


@needs_browser
def test_the_box_is_read_only_while_streaming_and_while_the_close_is_pending(tmp_path, qdir) -> None:
    """Revert: ta.readOnly in the stream click handler / its restore in onEnd (typing would be erased by the next partial)."""
    ro = "JSON.stringify([" + TA + ".readOnly, " + MIC + ".textContent])"
    v = _go(tmp_path, qdir, [
        *LIVE, *ROOM, {"eval": "window.__sc.closeDelay = 500; 1"},
        {"eval": "JSON.stringify(" + TA + ".readOnly)"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": ro}, CLICK, {"eval": ro},
        {"until": "window.__sc.closes.length === 1 && " + TA + ".value === 'the final text' && " + MIC + ".textContent === 'Mic'", "ms": 20000},
        {"eval": "JSON.stringify(" + TA + ".readOnly)"},
    ])
    assert [json.loads(x) for x in (v[-8], v[-5], v[-3], v[-1])] == [False, [True, "Stop mic"], [True, "Mic"], False], v


@needs_browser
def test_mic_and_dictate_exclude_each_other_both_ways(tmp_path, qdir) -> None:
    """Revert: the `if (live) return` guard on the Dictate click (a recognizer started over the stream) or the endDictation()
    call in the Mic click."""
    v = _go(tmp_path, qdir, [
        *LIVE, *ROOM,
        {"eval": DICT + ".click(); 1"},
        CLICK, {"until": "window.__sc.open.length === 1"},
        {"eval": "JSON.stringify([window.__sc.srStarts, " + DICT + ".textContent, " + MIC + ".textContent])"},
        {"eval": DICT + ".click(); 1"},
        {"eval": "JSON.stringify([window.__sc.srStarts, " + DICT + ".textContent, " + MIC + ".textContent])"},
    ])
    assert json.loads(v[-3]) == [1, "Dictate", "Stop mic"], v[-3]       # Mic ended the dictation and took the box
    assert json.loads(v[-1]) == [1, "Dictate", "Stop mic"], v[-1]       # Dictate while streaming starts nothing


@needs_browser
def test_a_transient_probe_failure_is_retried_by_a_later_probe(tmp_path, qdir) -> None:
    """Revert: probing cleared on failure (the failed promise was cached for the life of the page)."""
    v = _go(tmp_path, qdir, [
        {"until": "window.__sc.vj >= 1", "ms": 20000},
        _wait(300, "JSON.stringify([!!" + MIC + " && !" + MIC + ".hidden])"),
        {"eval": "window.__sc.vjFail = 0; LevainMic.probe().then((ok) => JSON.stringify([ok, window.__sc.vj, LevainMic.available()]))"},
    ], vjfail="99")
    assert json.loads(v[-2]) == [False] and json.loads(v[-1])[0] is True and json.loads(v[-1])[2] is True and json.loads(v[-1])[1] >= 2, v


@needs_browser
def test_the_page_stays_hidden_against_the_real_voice_json_without_a_stream_key(tmp_path, qdir) -> None:
    """The routes and the `stream` flag are slice S3; today's real /cockpit/voice.json has no `stream` key. Red with the gate
    loosened to the provider list alone."""
    v = _go(tmp_path, qdir, [
        {"until": "window.__sc.vj >= 1 && window.__sc.realKeys", "ms": 20000},
        _wait(500, "JSON.stringify([!!" + MIC + " && !" + MIC + ".hidden, LevainMic.available(), window.__sc.realKeys])"),
    ], real="true")
    shown, avail, keys = json.loads(v[-1])
    assert shown is False and avail is False and keys, v[-1]


@needs_browser
def test_a_resume_that_throws_or_rejects_ends_the_session_and_closes_the_context(tmp_path, qdir) -> None:
    """Revert: the resume() catch in start() (a throw leaked the context; a rejection was unhandled and the session ran on)."""
    for mode in ("throw", "reject"):
        sub = tmp_path / mode
        sub.mkdir()
        v = _go(sub, qdir, [
            *LIVE, {"eval": "window.__sc.resumeMode = '%s'; 1" % mode},
            CLICK,
            {"until": MIC + ".textContent === 'Mic' && !LevainMic.active()", "ms": 5000},
            _wait(300),
        ])
        st = _state(v[-1])
        assert st["sc"]["ctxClosed"] >= 1 and st["sc"]["open"] == [] and st["note"].startswith("Streaming dictation could not start"), (mode, st)
        assert all(t["stopped"] == 1 for t in st["sc"]["tracks"]), (mode, st)


def test_the_worklet_posts_one_copy_not_two() -> None:
    """Revert: the transfer list in mic_worklet.js (slice() plus the structured clone is two copies per 128-frame block)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "mic_worklet.js").read_text()
    assert "postMessage(c, [c.buffer])" in src and "postMessage(ch.slice" not in src
