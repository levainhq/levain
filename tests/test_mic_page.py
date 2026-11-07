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
    hang: { open: false, chunk: false, close: false, tr: false }, trs: [], chunkRevoked: false, tStatus: 500, tText: 'words from the recording', trDelay: 0, chunkStatus: 0, closeDelay: 0, gumHold: false, modHold: false, resumeMode: '', vjFail: __VJFAIL__, sends: [], srStarts: 0, realKeys: null };
  const hang = (o) => new Promise((res, rej) => { if (o && o.signal) o.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))); });
  const j = (o, s) => Promise.resolve(new Response(JSON.stringify(o), { status: s || 200, headers: { 'Content-Type': 'application/json' } }));
  const f0 = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const url = new URL(String(u), location.href), p = url.pathname, tok = (o && o.headers && o.headers['X-Levain-Write-Token']) || '';
    if (p === '/cockpit/voice.json') {
      sc.vj++;
      if (sc.vjFail > 0) { sc.vjFail--; return Promise.reject(new TypeError('net')); }
      if (REAL) return f0(u, o).then((r) => { r.clone().json().then((b) => { sc.realKeys = Object.keys(b); }); return r; });
      // openai is the FIRST keyed stt row but the server names deepgram: the page must send stream_provider, not row one.
      // Red with: the `j.stream_provider` branch in mic.js probe() removed (the page then sends provider=openai).
      return j({ stream: FLAG, stream_provider: 'deepgram', premium_stt: FLAG, authority: 'ok', estimate: true, prices_as_of: 'x', caps: {}, providers: [
        { name: 'openai', uses: ['stt'], key: 'set', record_id: 'vk-0', last_read: null, voices: [], default_voice: '', tts_model: '', speed: [0.5, 1.5], retention: 'r' },
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
      if (sc.chunkStatus) return j(sc.chunkRevoked ? { error: 'revoke_unreachable', revoked: true } : { error: 'x' }, sc.chunkStatus);
      return j({ seq: n, partial: 'live ' + n + ' <b>x</b>', final: 'said' });
    }
    if (p === '/cockpit/voice/stream/close') { sc.closes.push({ sid: url.searchParams.get('sid'), tok: tok });
      if (sc.hang.close) return hang(o);
      if (sc.closeDelay) return new Promise((r) => setTimeout(() => r(j({ text: 'the final text' })), sc.closeDelay));
      return j({ text: 'the final text' }); }
    if (p === '/cockpit/voice/transcribe') {
      const dv = new DataView(o.body), tag = (at) => String.fromCharCode(dv.getUint8(at), dv.getUint8(at + 1), dv.getUint8(at + 2), dv.getUint8(at + 3));
      sc.trs.push({ q: url.search, tok: tok, ct: o.headers['Content-Type'], len: o.body.byteLength, riff: tag(0), wave: tag(8), fmt: tag(12), data: tag(36),
        riffSize: dv.getUint32(4, true), fmtLen: dv.getUint32(16, true), pcmTag: dv.getUint16(20, true), ch: dv.getUint16(22, true), rate: dv.getUint32(24, true),
        byteRate: dv.getUint32(28, true), align: dv.getUint16(32, true), bits: dv.getUint16(34, true), dlen: dv.getUint32(40, true),
        nonzero: new Int16Array(o.body.slice(44)).some((x) => x !== 0) });
      if (sc.hang.tr) return hang(o);
      const answer = () => (sc.tStatus === 200 ? j({ text: sc.tText }) : j({ error: 'x' }, sc.tStatus));
      return sc.trDelay ? new Promise((r) => setTimeout(() => r(answer()), sc.trDelay)) : answer();
    }
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


@needs_browser
def test_send_while_dictating_sends_nothing_when_the_close_fails(tmp_path, qdir) -> None:
    """Revert: the LM.closeFailed() check in room_view's Send handler (a truncated partial went out with no note)."""
    v = _go(tmp_path, qdir, [
        *LIVE, *ROOM, {"eval": "window.__sc.hang.close = true; LevainMic.deadlines.close = 300; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(12000)"}, {"until": TA + ".value.indexOf('live 0') >= 0", "ms": 20000},
        {"eval": "document.querySelector('.rv-send').click(); 1"},
        {"until": "window.__sc.closes.length === 1 && !LevainMic.active()", "ms": 5000},
        _wait(300),
    ])
    st = _state(v[-1])
    assert st["sc"]["sends"] == [] and "nothing was sent" in (st["note"] or ""), st

WAV_OK = "window.__sc.tStatus = 200; "
RECOVERED = "window.__sc.trs.length === 1 && !LevainMic.active() && " + MIC + ".textContent === 'Mic'"


def _tr_ok(st, data_bytes=16000):
    t = st["sc"]["trs"][0]
    assert t["q"] == "?provider=deepgram" and t["ct"] == "audio/wav" and t["tok"] == TOKEN, t
    assert (t["riff"], t["wave"], t["fmt"], t["data"]) == ("RIFF", "WAVE", "fmt ", "data"), t
    assert (t["fmtLen"], t["pcmTag"], t["ch"], t["rate"], t["byteRate"], t["align"], t["bits"]) == (16, 1, 1, 16000, 32000, 2, 16), t
    assert t["dlen"] == data_bytes and t["len"] == 44 + data_bytes and t["riffSize"] == 36 + data_bytes and t["nonzero"], t


@needs_browser
def test_a_chunk_that_fails_mid_session_is_transcribed_from_the_kept_recording_as_one_wav(tmp_path, qdir) -> None:
    """28800 samples at 48 kHz = 16000 bytes of PCM16 at 16 kHz, so the WAV is 44 + 16000 bytes with a 16 kHz mono PCM16 header.
    Red with: keepPcm() removed from enqueue (canWav false, no POST, the note says the words so far), wavOf's header fields changed
    (the header asserts), the Content-Type left octet-stream in recover (ct), the token header dropped, or recover() not called
    from the enqueue catch (no transcribe request, the box stays empty)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.chunkStatus = 500; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": RECOVERED, "ms": 5000},
        _wait(200),
    ])
    st = _state(v[-1])
    _tr_ok(st)
    assert st["ta"] == "words from the recording" and st["note"].startswith("Streaming dictation stopped; these words come from the recording"), st
    assert "start of a long recording" not in st["note"] and [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["sc"]["ctxClosed"] == 1, st


@needs_browser
def test_a_revoked_chunk_is_a_final_stop_with_the_fixed_words_and_never_a_wav(tmp_path, qdir) -> None:
    """Chunks fail with the server's `revoked: true` refusal after 16000 bytes were captured and a WAV could be built. The
    session ends with the revoked sentence, no /transcribe request is made (the kept audio is dropped, not sent), the mic is
    released and the label is back. Red with: the `err.revoked` branch in enqueue's catch removed (the plain-failure path
    posts the WAV: trs has 1 entry and the note says the recording), or postJson not reading `revoked` off the refusal body
    (same), or `s.pcm = null` dropped from the branch (nothing observable here, covered by the trs assert only through
    canWav, so the branch itself is the line)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.chunkStatus = 503; window.__sc.chunkRevoked = true; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": "!LevainMic.active() && " + MIC + ".textContent === 'Mic'", "ms": 5000},
        _wait(300),
    ])
    st = _state(v[-1])
    assert st["sc"]["trs"] == [], st
    assert st["note"] == "Premium voice was turned off, so the dictation stopped; the words so far are in the box.", st
    assert [t["stopped"] for t in st["sc"]["tracks"]] == [1] and st["sc"]["ctxClosed"] == 1, st


@needs_browser
def test_an_open_that_fails_after_audio_was_captured_falls_back_to_the_wav(tmp_path, qdir) -> None:
    """The open hangs past its deadline while 16000 bytes are captured. Red with: recover() removed from fail() (the old
    "could not start" sentence and no transcribe request), or keepPcm() removed (nothing to send)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.hang.open = true; LevainMic.deadlines.open = 600; 1"},
        CLICK, {"until": "window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": RECOVERED, "ms": 5000},
        _wait(200),
    ])
    st = _state(v[-1])
    _tr_ok(st)
    assert st["ta"] == "words from the recording" and st["sc"]["chunks"] == [] and [t["stopped"] for t in st["sc"]["tracks"]] == [1], st


@needs_browser
def test_a_close_that_cannot_confirm_the_text_falls_back_and_send_is_not_blocked(tmp_path, qdir) -> None:
    """Revert: the recover() call in closeSession's failure handler (closeFailed stays true: Send is refused, the box keeps only the
    partial), or `closeFailed = false` after a good recovery."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.hang.close = true; LevainMic.deadlines.close = 300; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"}, {"until": "window.__sc.chunks.length === 2"},
        CLICK,
        {"until": RECOVERED, "ms": 5000},
        _wait(100, "JSON.stringify({s: " + STATE + ", cf: LevainMic.closeFailed()})"),
    ])
    out = _state(v[-1]); st = _state(out["s"])
    _tr_ok(st, 19200)      # 16000 bytes of chunks plus the 3200-byte tail flushed by the stop
    assert st["ta"] == "words from the recording" and out["cf"] is False, out


@needs_browser
def test_a_transcribe_that_fails_leaves_the_partial_the_old_sentence_and_close_failed(tmp_path, qdir) -> None:
    """Red with: closeFailed cleared before the recovery result is known (cf false), or the broke note dropped on a failed
    recovery. The fake answers 500 by default."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.hang.close = true; LevainMic.deadlines.close = 300; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"}, {"until": "window.__sc.chunks.length === 2"},
        CLICK,
        {"until": RECOVERED, "ms": 5000},
        _wait(100, "JSON.stringify({s: " + STATE + ", cf: LevainMic.closeFailed()})"),
    ])
    out = _state(v[-1]); st = _state(out["s"])
    assert out["cf"] is True and st["ta"].startswith("said ") and "recording" not in st["ta"], out
    (tmp_path / "two").mkdir()
    v = _go(tmp_path / "two", qdir, [
        *LIVE, {"eval": "window.__sc.chunkStatus = 500; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": RECOVERED, "ms": 5000}, _wait(100),
    ])
    st = _state(v[-1])
    assert st["note"] == "Streaming dictation stopped; the words so far are in the box." and st["ta"] == "", st


@needs_browser
def test_a_transcribe_that_never_answers_ends_at_its_deadline(tmp_path, qdir) -> None:
    """Revert: the deadlines.wav timeout on the recover POST (the session never ends, the mic label stays Stop mic)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.chunkStatus = 500; window.__sc.hang.tr = true; LevainMic.deadlines.wav = 400; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": RECOVERED, "ms": 5000}, _wait(100),
    ])
    st = _state(v[-1])
    assert st["note"].startswith("Streaming dictation stopped; the words so far") and len(st["sc"]["trs"]) == 1, st


@needs_browser
def test_stopping_while_the_wav_is_being_transcribed_does_not_cancel_it(tmp_path, qdir) -> None:
    """The transcribe answers after 700 ms and the operator taps Mic meanwhile. Red with: the `s.wavBusy` guard removed from
    finish() (stop() ends the session, cancelAll aborts the request, the text never lands)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.chunkStatus = 500; window.__sc.trDelay = 700; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": "window.__sc.trs.length === 1", "ms": 5000},
        CLICK,
        {"until": RECOVERED, "ms": 5000}, _wait(100),
    ])
    st = _state(v[-1])
    assert st["ta"] == "words from the recording", st


@needs_browser
def test_the_kept_recording_is_bounded_and_the_oldest_audio_is_dropped_with_a_note(tmp_path, qdir) -> None:
    """With wavMax 12000 and 16000 bytes captured, the first chunk is dropped: the WAV carries 8000 data bytes and the note says
    the start was left out. Red with: the shift loop in keepPcm removed (16000 bytes sent), or pcmDropped not set (wrong note).
    The default cap is the server's UPLOAD_MAX less the header."""
    from levain.cockpit.voice import UPLOAD_MAX
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": WAV_OK + "window.__sc.chunkStatus = 500; LevainMic.limits.wavMax = 12000; 1"},
        CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"},
        {"until": RECOVERED, "ms": 5000}, _wait(100),
    ])
    st = _state(v[-1])
    _tr_ok(st, 8000)
    assert "start of a long recording was left out" in st["note"], st
    (tmp_path / "two").mkdir()
    d = _go(tmp_path / "two", qdir, [*LIVE, {"eval": "LevainMic.limits.wavMax"}])
    assert d[-1] == UPLOAD_MAX - 44


@needs_browser
def test_a_healthy_session_never_posts_a_wav(tmp_path, qdir) -> None:
    """Red with: recover() called on the success path of closeSession."""
    v = _go(tmp_path, qdir, [
        *LIVE, CLICK, {"until": "window.__sc.open.length === 1 && window.__sc.node"},
        {"eval": "window.__feed(28800)"}, {"until": "window.__sc.chunks.length === 2"},
        CLICK, {"until": "window.__sc.closes.length === 1 && " + TA + ".value === 'the final text'", "ms": 20000}, _wait(300),
    ])
    assert _state(v[-1])["sc"]["trs"] == []


@needs_browser
def test_a_worklet_module_that_never_loads_ends_the_session_at_its_deadline(tmp_path, qdir) -> None:
    """Revert: withDeadline around wire() in start() (the microphone stayed open for ever)."""
    v = _go(tmp_path, qdir, [
        *LIVE, {"eval": "window.__sc.modHold = true; LevainMic.deadlines.open = 300; 1"},
        {"do": MIC + ".click(); 1", "until": "window.__sc.mods.length === 1"},
        {"until": "!LevainMic.active()", "ms": 5000},
        _wait(200),
    ])
    st = _state(v[-1])
    assert st["sc"]["open"] == [] and [t["stopped"] for t in st["sc"]["tracks"]] == [1], st
