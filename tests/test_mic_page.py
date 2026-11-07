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
from tests.test_voice_room_hook import _run, _to_room, needs_browser

FAKE = """
(() => {
  const FLAG = __FLAG__, DENY = __DENY__, OPEN_STATUS = __OPEN__, WORKLET = __WORKLET__;
  const sc = window.__sc = { vj: 0, open: [], chunks: [], closes: [], gum: 0, tracks: [], ctxClosed: 0, mods: [], node: null, sp: null };
  const j = (o, s) => Promise.resolve(new Response(JSON.stringify(o), { status: s || 200, headers: { 'Content-Type': 'application/json' } }));
  const f0 = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const url = new URL(String(u), location.href), p = url.pathname, tok = (o && o.headers && o.headers['X-Levain-Write-Token']) || '';
    if (p === '/cockpit/voice.json') {
      sc.vj++;
      return j({ stream: FLAG, premium_stt: FLAG, authority: 'ok', estimate: true, prices_as_of: 'x', caps: {}, providers: [
        { name: 'deepgram', uses: ['stt'], key: 'set', record_id: 'vk-1', last_read: null, voices: [], default_voice: '', tts_model: '', speed: [0.5, 1.5], retention: 'r' }] });
    }
    if (p === '/cockpit/voice/stream/open') {
      sc.open.push({ q: url.search, tok: tok, body: o && o.body !== undefined ? 'has-body' : null });
      return OPEN_STATUS === 200 ? j({ sid: 'sid-1' }) : j({ error: 'no' }, OPEN_STATUS);
    }
    if (p === '/cockpit/voice/stream/chunk') {
      const n = sc.chunks.length;
      sc.chunks.push({ seq: url.searchParams.get('seq'), sid: url.searchParams.get('sid'), bytes: o.body.byteLength, isBuf: o.body instanceof ArrayBuffer, tok: tok,
        nonzero: new Int16Array(o.body).some((x) => x !== 0), ct: o.headers['Content-Type'] });
      return j({ seq: n, partial: 'live ' + n + ' <b>x</b>', final: 'said' });
    }
    if (p === '/cockpit/voice/stream/close') { sc.closes.push({ sid: url.searchParams.get('sid'), tok: tok }); return j({ text: 'the final text' }); }
    return f0(u, o);
  };
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: (c) => {
    sc.gum++; sc.constraints = c;
    if (DENY) return Promise.reject(new DOMException('denied', 'NotAllowedError'));
    const t = { stopped: 0, stop() { this.stopped++; } }; sc.tracks.push(t);
    return Promise.resolve({ getTracks: () => [t] });
  } } });
  window.AudioContext = class {
    constructor() { this.sampleRate = 48000; this.destination = {}; sc.ctx = this;
      if (WORKLET) this.audioWorklet = { addModule: (u) => { sc.mods.push(u); return Promise.resolve(); } }; }
    resume() { return Promise.resolve(); }
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
  window.__vis = (v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v }); document.dispatchEvent(new Event('visibilitychange')); return 1; };
})();
"""


def _fake(flag="true", deny="false", open_status="200", worklet="true"):
    return FAKE.replace("__FLAG__", flag).replace("__DENY__", deny).replace("__OPEN__", open_status).replace("__WORKLET__", worklet)


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
