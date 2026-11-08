"""The whole cockpit mic chain, run end to end with ONLY the provider faked (spore-938: the first spend on a never-run path is a RUN).

Real: ``levain serve`` (``make_server`` with premium_stt ON, a signed custody-held deepgram key in a fake keychain), the S3 routes,
the S1 session registry + meter, the S2 websocket client, the S2b Deepgram adapter, and the shipped page (``mic.js`` and the REAL
``mic_worklet.js`` processor class, driven by test PCM) in headless Chrome. Fake: the provider, an in-process websocket server on
127.0.0.1 (the Deepgram tests' ``Server``), reached because the adapter is built with its test-injectable ``base_url`` /
``connect_opts``; the microphone (``getUserMedia`` answers a track that counts its ``stop()``) and the audio context. The fallback's
provider HTTP is the voice routes' fake transport. No real key, provider, device or app of his is touched. What only a real iPhone and a
real Deepgram key can show is residue (see RETURN_micE2E.md).

Each test names the line whose revert turns it red."""
from __future__ import annotations

import json
import struct
import threading
import time

import pytest

pytest.importorskip("cryptography")

from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.voice import custody, deepgram_stream  # noqa: E402
from tests.test_cockpit_k2a import TOKEN  # noqa: E402
from tests.test_cockpit_voice_routes import (VALUE, _clock_before_the_openai_cutoff, call, js, rig, served,  # noqa: E402,F401
                                             pytestmark)
from tests.test_cockpit_flip_parity import _drive, needs_browser  # noqa: E402
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401  (fixture: the consent directory the server reads)
from tests.test_voice_deepgram_stream import METADATA, results  # noqa: E402
from tests.test_voice_room_hook import HEAD, INIT, _to_room  # noqa: E402
from tests.test_voice_wsclient import Server  # noqa: E402

# The page's browser, everything except the network: the microphone, the audio context and the AudioWorklet. addModule FETCHES the
# shipped /mic_worklet.js from the real server and evaluates it against a stub of the worklet global scope, so the REAL processor
# class runs; __feed hands it 128-frame blocks of a 440 Hz tone at 48 kHz exactly as the engine would. Every fetch answer and
# request header the page sees is logged to window.__net (the page-visible bytes the key must never be in).
BROWSER = """
(() => {
  const sc = window.__sc = { gum: 0, tracks: [], ctxClosed: 0, procs: [], workletSrc: '', t: 0, node: null };
  window.__net = [];
  const f0 = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const entry = { url: String(u), method: (o && o.method) || 'GET', hdr: JSON.stringify((o && o.headers) || {}), status: 0, text: '' };
    window.__net.push(entry);
    return f0(u, o).then((r) => { entry.status = r.status; r.clone().text().then((t) => { entry.text = t; }); return r; });
  };
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: () => {
    sc.gum++;
    const t = { stopped: 0, stop() { this.stopped++; } }; sc.tracks.push(t);
    return Promise.resolve({ getTracks: () => [t] });
  } } });
  let Cls = null;
  window.AudioContext = class {
    constructor() { this.sampleRate = 48000; this.destination = {}; sc.ctx = this;
      this.audioWorklet = { addModule: (u) => new Promise((res, rej) => {
        // the page's CSP forbids eval, so the SHIPPED worklet file is loaded as a same-origin script against a stub of the worklet scope
        window.AudioWorkletProcessor = class { constructor() { this.port = { postMessage() {} }; } };
        window.registerProcessor = (n, c) => { Cls = c; sc.registered = n; };
        const el = document.createElement('script'); el.src = u;
        el.onload = () => { fetch(u).then((r) => r.text()).then((t) => { sc.workletSrc = t; res(); }); };
        el.onerror = () => rej(new Error('worklet load'));
        document.head.appendChild(el);
      }) }; }
    resume() { return Promise.resolve(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    close() { sc.ctxClosed++; return Promise.resolve(); }
  };
  window.AudioWorkletNode = class {
    constructor(ctx, name) {
      this.port = { onmessage: null }; sc.node = this;
      this.proc = new Cls(); sc.procs.push(this.proc);
      this.proc.port = { postMessage: (d) => { if (this.port.onmessage) this.port.onmessage({ data: d }); } };
    }
    connect() {} disconnect() {}
  };
  window.__feed = (n) => {
    let t = sc.t;
    for (let off = 0; off < n; off += 128) {
      const len = Math.min(128, n - off), b = new Float32Array(len);
      for (let i = 0; i < len; i++, t++) b[i] = 0.5 * Math.sin(2 * Math.PI * 440 * t / 48000);
      sc.node.proc.process([[b]]);
    }
    sc.t = t; return 1;
  };
})();
"""
MIC = "document.querySelector('.rv-mic-stream')"
SHOWN = f"!!{MIC} && !{MIC}.hidden"
TA = "document.querySelector('.rv-input')"
NOTE = "(document.querySelector('.rv-sendnote') || {}).textContent"
SNAP = ("JSON.stringify({ta: " + TA + ".value, label: (" + MIC + " || {}).textContent, note: " + NOTE + ", shown: " + SHOWN
        + ", net: window.__net.map((n) => ({ url: n.url, method: n.method, status: n.status, text: n.text.length > 600 ? '' : n.text })),"
        " leak: [document.documentElement.outerHTML, JSON.stringify(Object.assign({}, localStorage)), JSON.stringify(window.__net), JSON.stringify(window.__sc.tracks)].map((x) => x.indexOf(" + json.dumps(VALUE) + ") >= 0),"
        " gum: window.__sc.gum, stopped: window.__sc.tracks.map((t) => t.stopped), ctxClosed: window.__sc.ctxClosed,"
        " reg: window.__sc.registered, worklet: window.__sc.workletSrc.length})")
SETTLE = "new Promise((r) => setTimeout(r, 600)).then(() => " + SNAP + ")"


class Dg:
    """The fake Deepgram: what the handshake and the wire carried, and a script per scenario."""

    def __init__(self) -> None:
        self.seen: dict = {}
        self.audio: list[bytes] = []
        self.texts: list[str] = []
        self.eof = threading.Event()

    def happy(self, p) -> None:
        p.handshake()
        self.seen["line"] = p.request.split(b"\r\n")[0].decode()
        self.seen["auth"] = p.header("Authorization")
        self.seen["request"] = p.request
        p.conn.settimeout(0.05)
        said = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                fin, op, masked, data, key = p.read_frame()
            except (TimeoutError, OSError):
                continue
            except EOFError:
                break
            if op == 2:
                self.audio.append(data)
                if not said:
                    said = True
                    p.send(1, results("live words", False).encode())
            elif op == 1:
                self.texts.append(data.decode())
                if json.loads(data).get("type") == "CloseStream":
                    p.send(1, results("the final words", True).encode())
                    p.send(1, METADATA.encode())
            elif op == 8:
                p.send(8, b"\x03\xe8")
                break
        self.eof.set()

    def drop_after_first_audio(self, p) -> None:
        p.handshake()
        self.seen["auth"] = p.header("Authorization")
        p.conn.settimeout(10)
        fin, op, masked, data, key = p.read_frame()
        self.audio.append(data)
        p.send(1, results("partial then gone", False).encode())
        time.sleep(2.0)
        p.send(8, b"\x03\xf3" + b"provider words that must not reach the page")
        self.eof.set()


def pcm_stats(blob: bytes) -> dict:
    n = len(blob) // 2
    s = struct.unpack("<%dh" % n, blob[: 2 * n])
    crossings = sum(1 for a, b in zip(s, s[1:]) if (a < 0) != (b < 0))
    return {"samples": n, "peak": max(abs(x) for x in s) if s else 0, "crossings": crossings}


def chain(tmp_path, rig, steps, script, *, premium_stt=True, key=True, before=None):  # before(rig, httpd): runs with the server up
    """The real server over the keyed rig with the real Deepgram adapter pointed at the fake provider; the page driven through
    ``steps``. Returns (values, dg, httpd-after-shutdown, the fake provider server)."""
    dg = Dg()
    srv = Server(getattr(dg, script))
    if key:
        rig.add_key(provider="deepgram", uses=("stt",), source="levain-voice-deepgram-e2e")
    base = f"ws://127.0.0.1:{srv.port}/v1/listen"
    try:
        with served(rig, premium_stt=premium_stt) as (port, httpd):
            httpd.voice_stream_factory = lambda secret: deepgram_stream.DeepgramStream(
                secret, base_url=base, connect_opts={"insecure_for_test": True, "allowed_hosts": None}, close_drain=3.0)
            if before:
                before(rig, httpd)
            out = _drive(tmp_path, f"http://127.0.0.1:{port}/", [{"init": INIT % BROWSER}, *HEAD, _to_room(), *steps])
            vals = out["values"]
        return vals, dg, httpd, srv
    finally:
        srv.stop()


def snap(v):
    return json.loads(v)


def assert_key_nowhere_the_page_can_see(s):
    blob = json.dumps(s)
    assert s["leak"] == [False] * 4, "the key reached something the page can see: " + repr(s["leak"])
    assert VALUE not in blob, "the key reached something the page can see"
    assert "Token " + VALUE not in blob


@needs_browser
def test_the_happy_path_end_to_end(tmp_path, rig) -> None:
    """open -> chunks -> a linear16 16 kHz stream and the Token header at the provider -> a partial in the draft -> stop -> final text
    -> meter settled -> clean shutdown. Red with: the resampler removed from mic.js (the provider gets 48 kHz-sized audio: 28800
    samples, not 9600), `Authorization` not set by the adapter's connect (the handshake line shows no key), the key written into any
    route answer (the page-visible scan), the settle in stream_close dropped (a `reserved` row stays), or the worklet class not
    registered (the node never builds)."""
    vals, dg, httpd, srv = chain(tmp_path, rig, [
        {"until": SHOWN, "ms": 30000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.node && window.__net.some((n) => n.url.indexOf('/stream/open') >= 0 && n.status === 200)", "ms": 20000},
        {"eval": "window.__feed(28800)"},
        {"until": TA + ".value.indexOf('live words') >= 0", "ms": 30000},
        {"eval": SNAP},
        {"eval": MIC + ".click(); 1"},
        {"until": TA + ".value === 'the final words' && " + MIC + ".textContent === 'Mic'", "ms": 30000},
        {"eval": SETTLE},
    ], "happy", before=None)
    mid, end = snap(vals[-4]), snap(vals[-1])
    posts = [(n["url"].split("?")[0], n["status"]) for n in end["net"] if n["method"] == "POST" and "/voice/" in n["url"]]
    assert posts[0] == ("/cockpit/voice/stream/open", 200), posts
    chunk_posts = [p for p in posts if p[0].endswith("/chunk")]
    assert len(chunk_posts) == 3 and all(s == 200 for _u, s in chunk_posts), posts      # 8000 + 8000 + the tail on stop
    assert posts[-1] == ("/cockpit/voice/stream/close", 200), posts
    seqs = [n["url"].split("seq=")[1] for n in end["net"] if "/chunk" in n["url"]]
    assert seqs == ["0", "1", "2"], seqs
    # the provider's side of the wire
    assert dg.seen["auth"] == "Token " + VALUE
    assert "encoding=linear16" in dg.seen["line"] and "sample_rate=16000" in dg.seen["line"] and "channels=1" in dg.seen["line"], dg.seen["line"]
    blob = b"".join(dg.audio)
    st = pcm_stats(blob)
    assert 9500 <= st["samples"] <= 9600, st                          # 28800 samples at 48 kHz, resampled to 16 kHz
    assert 14000 <= st["peak"] <= 18000, st                           # the 0.5-amplitude tone, as PCM16
    assert 500 <= st["crossings"] <= 560, st                          # 440 Hz over 0.6 s = 528 crossings: it is the tone, at 16 kHz
    assert any(json.loads(t).get("type") == "CloseStream" for t in dg.texts), dg.texts
    # the page
    assert mid["label"] == "Stop mic" and "live words" in mid["ta"], mid["ta"]
    assert end["ta"] == "the final words" and end["label"] == "Mic", end["ta"]
    assert end["stopped"] == [1] and end["ctxClosed"] == 1 and end["gum"] == 1 and end["reg"] == "levain-mic-tap" and end["worklet"] > 100
    assert_key_nowhere_the_page_can_see(end)
    # the meter: every row settled, none still reserved
    statuses = [r.status for r in rig.rows()]
    assert statuses and "reserved" not in statuses, statuses
    # shutdown was clean: the fake provider saw the socket end, nothing is still registered
    assert dg.eof.wait(5) and srv.error is None
    reg = httpd.voice_streams
    assert reg is not None and reg._closed and reg._live() == 0 and reg._unsettled == [] and reg._pending == 0, vars(reg)
    assert httpd.voice_reaper is None or not httpd.voice_reaper.is_alive(), "the reaper thread outlived the server"
    assert not [t for t in threading.enumerate() if t.is_alive() and "voice" in t.name.lower() and t is not threading.current_thread()], \
        [t.name for t in threading.enumerate()]


@needs_browser
def test_the_provider_drops_the_socket_mid_session_and_the_page_falls_back_to_the_recording(tmp_path, rig) -> None:
    """Red with: the page's chunk-failure branch not calling recover() (no /transcribe POST, only the fixed 'stopped' words), the WAV
    not built from the kept PCM, or the provider's close text (carrying 'provider words') quoted into any page-visible answer."""
    rig.transport.body = json.dumps({"results": {"channels": [{"alternatives": [{"transcript": "words from the recording"}]}]}}).encode()
    vals, dg, httpd, srv = chain(tmp_path, rig, [
        {"until": SHOWN, "ms": 30000},
        {"do": MIC + ".click(); 1", "until": "window.__sc.node && window.__net.some((n) => n.url.indexOf('/stream/open') >= 0 && n.status === 200)", "ms": 20000},
        {"eval": "window.__feed(14400)"},
        {"eval": "new Promise((r) => setTimeout(r, 500)).then(() => window.__feed(14400))"},
        {"until": TA + ".value.indexOf('partial then gone') >= 0", "ms": 30000},
        {"eval": "new Promise((r) => setTimeout(r, 2600)).then(() => window.__feed(57600))"},
        {"until": "window.__net.some((n) => n.url.indexOf('/voice/transcribe') >= 0)", "ms": 30000},
        {"until": TA + ".value.indexOf('words from the recording') >= 0", "ms": 30000},
        {"eval": SETTLE},
    ], "drop_after_first_audio")
    end = snap(vals[-1])
    assert "words from the recording" in end["ta"], end["ta"]
    assert "come from the recording instead" in (end["note"] or ""), end["note"]
    assert any("/voice/transcribe" in n["url"] and n["status"] == 200 for n in end["net"])
    assert "provider words" not in json.dumps(end) and dg.seen["auth"] == "Token " + VALUE
    assert end["stopped"] == [1] and end["label"] == "Mic"
    assert_key_nowhere_the_page_can_see(end)
    statuses = [r.status for r in rig.rows()]
    assert "reserved" not in statuses, statuses
    assert len(rig.transport.seen) == 1 and rig.transport.seen[0]["url"].startswith("https://api.deepgram.com/v1/listen")


@needs_browser
def test_a_key_revoked_at_open_is_refused_before_any_socket(tmp_path, rig, monkeypatch) -> None:
    """The page offers the Mic (the key is set); the key is revoked (a real signed revoke_grant) between the tap and the key read
    inside the open route; the open must refuse before the factory builds a socket. Red with: stream_open building the stream before
    read_key (the factory runs and the fake provider accepts), the refusal not mapped to the fixed open-failed sentence, or the
    meter row left reserved."""
    record = {}
    real = custody.read_key

    def revoke_then_read(*a, **kw):
        if not record.get("done"):
            record["done"] = True
            custody.revoke_key(rig.data_root, record["id"], **rig.ck.kw())
        return real(*a, **kw)

    def before(rg, httpd):
        built = record.setdefault("built", [])
        inner = httpd.voice_stream_factory
        httpd.voice_stream_factory = lambda secret: built.append(1) or inner(secret)
        monkeypatch.setattr(custody, "read_key", revoke_then_read)

    rec = rig.add_key(provider="deepgram", uses=("stt",), source="levain-voice-deepgram-e2e")
    record["id"] = rec["id"]
    dg = Dg()
    srv = Server(dg.happy)
    base = f"ws://127.0.0.1:{srv.port}/v1/listen"
    try:
        with served(rig) as (port, httpd):
            httpd.voice_stream_factory = lambda secret: record.setdefault("built", []).append(1) or deepgram_stream.DeepgramStream(
                secret, base_url=base, connect_opts={"insecure_for_test": True, "allowed_hosts": None})
            monkeypatch.setattr(custody, "read_key", revoke_then_read)
            out = _drive(tmp_path, f"http://127.0.0.1:{port}/", [{"init": INIT % BROWSER}, *HEAD, _to_room(),
                {"until": SHOWN, "ms": 30000},
                {"do": MIC + ".click(); 1", "until": "window.__net.some((n) => n.url.indexOf('/stream/open') >= 0 && n.status > 0)", "ms": 20000},
                {"eval": SETTLE}])
    finally:
        srv.stop()
    end = snap(out["values"][-1])
    opened = [n for n in end["net"] if "/stream/open" in n["url"]]
    assert len(opened) == 1 and opened[0]["status"] in (409, 403, 503), opened
    assert record["done"] and record.get("built", []) == [] and dg.seen == {}, "a socket was built for a revoked key"
    assert "could not start" in (end["note"] or ""), end["note"]
    assert end["label"] == "Mic" and end["stopped"] == [1]
    assert_key_nowhere_the_page_can_see(end)
    assert "reserved" not in [r.status for r in rig.rows()]


@needs_browser
def test_premium_stt_off_is_404_and_the_button_is_hidden(tmp_path, rig) -> None:
    """Red with: the `premium_stt` refusal dropped from _voice_stream_answer (open answers 200 and a socket opens), or `stream` still
    advertised on voice.json with the switch off (the Mic is drawn)."""
    direct = {}

    def poke(rg, httpd):       # the three routes on the real server with the switch off: 404, before a key is read or a body is
        port = httpd.server_address[1]
        for name, path in (("open", cockpit_voice.STREAM_OPEN + "?provider=deepgram"), ("chunk", cockpit_voice.STREAM_CHUNK + "?sid=x&seq=0"),
                           ("close", cockpit_voice.STREAM_CLOSE + "?sid=x")):
            status, _h, body = call(port, "POST", path, b"\x01\x00" * 4000, ctype="application/octet-stream")
            direct[name] = (status, js(body)["message"])

    vals, dg, httpd, srv = chain(tmp_path, rig, [
        {"until": "document.querySelector('.rv .rv-input')", "ms": 30000},
        {"eval": "new Promise((r) => setTimeout(r, 1500)).then(() => " + SNAP + ")"},
    ], "happy", premium_stt=False, before=poke)
    assert direct == {k: (404, cockpit_voice.WORDS["premium_stt_off"]) for k in ("open", "chunk", "close")}, direct
    end = snap(vals[-1])
    assert end["shown"] is False and end["gum"] == 0
    assert not any("/stream/" in n["url"] for n in end["net"])
    assert dg.seen == {} and rig.rows() == []
