"""levain.voice.deepgram_stream against an in-process fake Deepgram websocket server (loopback ws:// via the test flag)."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
import urllib.parse
from dataclasses import dataclass

import pytest

try:                                                    # the head merges S1 first; until then a stand-in stands in
    import levain.voice.stream_session  # noqa: F401
except ModuleNotFoundError as _exc:                     # pragma: no cover - only in the S2b worktree before the merge
    if _exc.name != "levain.voice.stream_session":      # a broken real module (or its imports) must fail loudly
        raise
    _m = types.ModuleType("levain.voice.stream_session")

    class StreamNotStarted(Exception):
        pass

    @dataclass(frozen=True)
    class StreamUpdate:
        partial: str = ""
        final: str = ""
        failed: bool = False
        ended: str = ""

    class ProviderStream:
        pass

    _m.StreamNotStarted, _m.StreamUpdate, _m.ProviderStream = StreamNotStarted, StreamUpdate, ProviderStream
    sys.modules["levain.voice.stream_session"] = _m

from levain.voice import custody, deepgram_stream as dg  # noqa: E402
from levain.voice.stream_session import StreamNotStarted  # noqa: E402
from tests.test_voice_wsclient import Server  # noqa: E402

KEY = "dgSECRETkey123"
OPEN = {"insecure_for_test": True, "allowed_hosts": None}


def results(text, final, speech_final=False):
    return json.dumps({"type": "Results", "is_final": final, "speech_final": speech_final,
                       "channel": {"alternatives": [{"transcript": text, "confidence": 0.9, "words": []}]}})


METADATA = json.dumps({"type": "Metadata", "request_id": "r", "duration": 1.0, "channels": 1})


@pytest.fixture
def serve():
    servers = []

    def make(script):
        s = Server(script)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()
        assert s.error is None


def open_stream(srv, **kw):
    base = f"ws://127.0.0.1:{srv.port}/v1/listen"
    kw.setdefault("close_drain", 2.0)
    return dg.DeepgramStream(custody.Secret(KEY), base_url=base, connect_opts=dict(OPEN), **kw)


def wait_for(pred, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def read_text(peer):
    while True:
        fin, op, masked, data, key = peer.read_frame()
        assert masked
        if op == 1:
            return data.decode()
        if op == 8:
            return None


def collect(peer, out, until, stop_on_close=True):
    """Server side: append every client frame as (op, data) to ``out`` until ``until`` (an Event) is set or the client
    closes. Short socket timeouts only pace the loop; nothing is asserted on timing."""
    peer.conn.settimeout(0.05)
    while not until.is_set():
        try:
            fin, op, masked, data, key = peer.read_frame()
        except (TimeoutError, OSError):
            continue
        except EOFError:
            return
        out.append((op, data))
        if op == 8 and stop_on_close:
            return


def kinds(frames):
    return [json.loads(d)["type"] if op == 1 else ("audio" if op == 2 else "close") for op, d in frames]


def test_query_string_and_header(serve):
    seen = {}

    def script(p):
        p.handshake()
        seen["line"] = p.request.split(b"\r\n")[0].decode()
        seen["auth"] = p.header("Authorization")
        p.read_frame()

    srv = serve(script)
    s = open_stream(srv)
    assert wait_for(lambda: "auth" in seen)
    s.close()
    path = seen["line"].split(" ")[1]
    assert path == ("/v1/listen?model=nova-3&encoding=linear16&sample_rate=16000&channels=1&interim_results=true"
                    "&endpointing=300&smart_format=true&mip_opt_out=true")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    assert q["encoding"] == ["linear16"] and q["sample_rate"] == ["16000"] and q["channels"] == ["1"]
    assert seen["auth"] == f"Token {KEY}"
    assert dg.URL_BASE == "wss://api.deepgram.com/v1/listen"


def test_interim_then_final_assembly(serve):
    got = {}

    def script(p):
        p.handshake()
        fin, op, masked, data, key = p.read_frame()
        got["audio"] = (op, data)
        p.send(1, results("hel", False).encode())
        p.send(1, results("hello wor", False).encode())
        p.send(1, results("hello world", True).encode())
        p.send(1, results("how are", False).encode())
        p.send(1, json.dumps({"type": "UtteranceEnd", "channel": [0], "last_word_end": 1.0}).encode())
        time.sleep(1.5)

    srv = serve(script)
    s = open_stream(srv)
    s.send_audio(b"\x01\x00" * 100)
    assert wait_for(lambda: s.poll().partial == "how are")
    up = s.poll()
    assert up.final == "hello world" and up.partial == "how are" and not up.failed
    assert got["audio"] == (2, b"\x01\x00" * 100)
    s.close()


def test_is_final_clears_partial_and_empty_final_ignored(serve):
    def script(p):
        p.handshake()
        p.send(1, results("one", False).encode())
        p.send(1, results("one", True).encode())
        p.send(1, results("", True).encode())
        p.send(1, results("two", True).encode())
        time.sleep(1.0)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "one two")
    assert s.poll().partial == ""
    s.close()


def test_close_drains_last_final(serve):
    seen = {}

    def script(p):
        p.handshake()
        p.send(1, results("early", True).encode())
        msg = read_text(p)
        seen["msg"] = msg
        p.send(1, results("the last words", True).encode())
        p.send(1, METADATA.encode())
        p.read_frame()      # the client's close frame
        p.send(8, b"\x03\xe8")

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "early")
    text = s.close()
    assert json.loads(seen["msg"]) == {"type": "CloseStream"}
    assert text == "early the last words"
    assert s.close() == text                      # idempotent
    with pytest.raises(dg.DeepgramStreamError):
        s.send_audio(b"\x00\x00")


def test_close_keeps_unfinalised_partial_at_deadline(serve):
    def script(p):
        p.handshake()
        p.send(1, results("said", True).encode())
        p.send(1, results("tail", False).encode())
        time.sleep(1.2)

    s = open_stream(serve(script), close_drain=0.3)
    assert wait_for(lambda: s.poll().partial == "tail")
    assert s.close() == "said tail"
    up = s.poll()                                 # the provider never confirmed the end: the text is kept, and so is
    assert up.failed and s.reason == "incomplete"  # the fact that it may be short


def test_provider_error_close_is_failed_with_fixed_reason(serve):
    def script(p):
        p.handshake()
        p.send(8, b"\x03\xf3" + b"SECRET provider words")
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    assert s.reason == "provider_closed"
    with pytest.raises(dg.DeepgramStreamError) as e:
        s.send_audio(b"\x00\x00")
    assert "SECRET" not in str(e.value)
    s.close()


@pytest.mark.parametrize("msg", [b"not json {", b"[1, 2]", b'{"type": 5}',
                                 b'{"type": "Results", "is_final": true}',
                                 b'{"type": "Results", "is_final": "yes", "channel": {"alternatives": [{"transcript": "x"}]}}',
                                 b'{"type": "Results", "is_final": true, "channel": {"alternatives": []}}'])
def test_malformed_message_fails(serve, msg):
    def script(p):
        p.handshake()
        p.send(1, msg)
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    assert s.reason == "malformed" and s.poll().final == ""
    s.close()


def test_binary_and_error_messages_fail(serve):
    def bin_script(p):
        p.handshake()
        p.send(2, b"\x00\x01")
        time.sleep(0.5)

    s = open_stream(serve(bin_script))
    assert wait_for(lambda: s.poll().failed) and s.reason == "malformed"
    s.close()

    def err_script(p):
        p.handshake()
        p.send(1, json.dumps({"type": "Error", "description": "SECRET leak words"}).encode())
        time.sleep(0.5)

    s = open_stream(serve(err_script))
    assert wait_for(lambda: s.poll().failed) and s.reason == "provider_error"
    s.close()


def test_normal_close_from_provider_before_we_ask_is_failed(serve):
    def script(p):
        p.handshake()
        p.send(8, b"\x03\xe8")
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    s.close()


def test_keepalive_bounded_then_reset_by_audio_and_never_after_closestream(serve, monkeypatch):
    monkeypatch.setattr(dg, "MAX_KEEPALIVES", 2)
    frames, done = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, done)

    srv = serve(script)
    s = open_stream(srv, keepalive_after=0.1, tick=0.05, close_drain=0.3)
    count = lambda: kinds(frames).count("KeepAlive")  # noqa: E731
    assert wait_for(lambda: count() == 2)
    time.sleep(0.5)                                # five more idle windows: the cap holds (an absence check; load can
    assert count() == 2                            # only make it pass more easily, never fail it falsely)
    s.send_audio(b"\x00\x00" * 10)                 # a successful audio send resets the run
    assert wait_for(lambda: count() == 4)
    s.close()
    done.set()
    k = kinds(frames)
    assert "CloseStream" in k
    assert "KeepAlive" not in k[k.index("CloseStream"):]
    assert set(k) <= {"KeepAlive", "CloseStream", "audio", "close"}


def test_overlong_final_text_fails(serve, monkeypatch):
    monkeypatch.setattr(dg, "MAX_TEXT_CHARS", 20)

    def script(p):
        p.handshake()
        p.send(1, results("x" * 30, True).encode())
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    assert s.reason == "overflow"
    s.close()


def test_connect_refusals_are_fixed_and_not_started():
    with pytest.raises(StreamNotStarted):               # plain ws:// without the test flag
        dg.DeepgramStream(custody.Secret(KEY), base_url="ws://127.0.0.1:9/x", connect_opts={})
    with pytest.raises(StreamNotStarted):               # host outside the allow-list
        dg.DeepgramStream(custody.Secret(KEY), base_url="wss://evil.example/v1/listen")
    with pytest.raises(StreamNotStarted):               # a key that cannot be a header
        dg.DeepgramStream(custody.Secret(KEY + "\r\nX: 1"))


def test_handshake_refused_after_connect_is_provider_failure(serve):
    srv = serve(lambda p: p.handshake(status="401 Unauthorized"))
    with pytest.raises(dg.DeepgramStreamError) as e:
        open_stream(srv)
    assert e.value.code == "connect" and not isinstance(e.value, StreamNotStarted)


def _holds(v, depth=0) -> bool:
    if depth > 6:
        return False
    if isinstance(v, (bytes, bytearray)):
        return b"SECRET" in bytes(v)
    if isinstance(v, str):
        return "SECRET" in v
    if isinstance(v, dict):
        return any(_holds(k, depth + 1) or _holds(x, depth + 1) for k, x in v.items())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_holds(x, depth + 1) for x in v)
    try:
        return "SECRET" in repr(v)
    except Exception:  # noqa: BLE001
        return False


def _walk_tb(err):
    tb = err.__traceback__
    while tb is not None:
        leaked = [k for k, v in tb.tb_frame.f_locals.items() if _holds(v)]
        assert not leaked, (tb.tb_frame.f_code.co_name, leaked)
        tb = tb.tb_next


def test_key_in_no_attribute_repr_or_exception_frame(serve):
    def script(p):
        p.handshake()
        p.send(1, results("hi", True).encode())
        time.sleep(0.6)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "hi")
    assert "SECRET" not in repr(s)
    for name, val in vars(s).items():
        assert not _holds(val), name
    ws = s._ws
    assert not any(_holds(v) for v in vars(ws).values())
    s.close()

    # exception paths: refused factory, failed handshake, send on a dead stream
    for call in (lambda: dg.DeepgramStream(custody.Secret(KEY + "\r\nX: 1")),
                 lambda: dg.DeepgramStream(custody.Secret(KEY), base_url="wss://evil.example/v1/listen"),
                 lambda: open_stream(serve(lambda p: p.handshake(status="401 Unauthorized")))):
        with pytest.raises(Exception) as e:
            call()
        assert e.value.__context__ is None and e.value.__cause__ is None
        _walk_tb(e.value)

    def dead(p):
        p.handshake()
        p.conn.close()

    s = open_stream(serve(dead))
    assert wait_for(lambda: s.poll().failed)
    with pytest.raises(dg.DeepgramStreamError) as e:
        s.send_audio(b"\x00\x00")
    _walk_tb(e.value)
    s.close()


def test_factory_returns_stream_interface(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, secret, **kw):
            seen["secret"] = secret

    monkeypatch.setattr(dg, "DeepgramStream", Fake)
    sec = custody.Secret(KEY)
    assert isinstance(dg.factory(sec), Fake) and seen["secret"] is sec


# --- L3 r1 fixes -------------------------------------------------------------------------------------------------------

def test_partials_coalesce_and_never_fill_a_queue(serve):
    """400 partials with nobody polling: only the newest is kept, nothing overflows (r1 #1)."""
    sent = threading.Event()

    def script(p):
        p.handshake()
        for i in range(400):
            p.send(1, results(f"word {i}", False).encode())
        p.send(1, results("done", True).encode())
        p.send(1, results("tail", False).encode())
        sent.set()
        time.sleep(2.5)

    s = open_stream(serve(script))
    assert sent.wait(5)
    time.sleep(1.3)                               # longer than any put timeout a bounded queue would block for
    up = s.poll()
    assert not up.failed and up.final == "done" and up.partial == "tail"
    s.close()


def test_oversized_partial_fails_and_is_not_kept(serve, monkeypatch):
    monkeypatch.setattr(dg, "MAX_TEXT_CHARS", 20)

    def script(p):
        p.handshake()
        p.send(1, results("ok", True).encode())
        p.send(1, results("y" * 30, False).encode())
        time.sleep(0.8)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    up = s.poll()
    assert s.reason == "overflow" and up.partial == "" and up.final == "ok"
    s.close()


def test_finals_before_an_overflow_are_kept(serve, monkeypatch):
    monkeypatch.setattr(dg, "MAX_TEXT_CHARS", 20)

    def script(p):
        p.handshake()
        p.send(1, results("first", True).encode())
        p.send(1, results("second", True).encode())
        p.send(1, results("z" * 30, True).encode())
        time.sleep(0.8)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    assert s.reason == "overflow"
    assert s.close() == "first second"


def test_socket_is_closed_only_after_the_reader_stopped(serve):
    """wsclient.close() reads (and drops) frames it takes while the reader is between recv calls, so the stream must
    stop its reader before closing the socket; a final that arrived before the drain deadline is in the text (r1 #2)."""
    seen = {}

    def script(p):
        p.handshake()
        read_text(p)                                    # CloseStream
        p.send(1, results("last words", True).encode())  # no Metadata: the drain deadline ends it
        time.sleep(2.0)

    s = open_stream(serve(script), close_drain=0.4)
    real = s._ws.close

    def spy(*a, **kw):
        seen["reader_alive"] = s._reader.is_alive()
        return real(*a, **kw)

    s._ws.close = spy
    assert s.close() == "last words"
    assert seen["reader_alive"] is False


def test_reset_after_closestream_is_incomplete_not_success(serve):
    def script(p):
        p.handshake()
        p.send(1, results("early", True).encode())
        read_text(p)                                    # CloseStream
        p.send(1, results("partly", True).encode())
        p.conn.shutdown(2)                              # reset: no Metadata, no close frame

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "early")
    text = s.close()
    assert text == "early partly"
    up = s.poll()
    assert up.failed and s.reason == "incomplete"


def test_stall_inside_the_closing_results_frame_is_incomplete_and_close_is_bounded(serve):
    release = threading.Event()

    def script(p):
        p.handshake()
        p.send(1, results("kept", True).encode())
        read_text(p)                                    # CloseStream
        p.raw(b"\x81\x7e\x01\x00" + b"{" * 10)          # a 256-byte text frame, 10 bytes of it, then nothing
        release.wait(5)

    tick, drain = 0.1, 0.4
    s = open_stream(serve(script), close_drain=drain, tick=tick)
    assert wait_for(lambda: s.poll().final == "kept")
    t0 = time.monotonic()
    text = s.close()
    took = time.monotonic() - t0
    release.set()
    assert text == "kept" and s.poll().failed and s.reason == "incomplete"
    assert took <= dg.close_bound(drain, tick) + 0.5


def test_bad_chunk_is_refused_and_the_stream_lives(serve):
    frames, done = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, done)

    s = open_stream(serve(script))
    for bad in (b"", b"\x00" * (dg.SEND_MAX + 1)):
        with pytest.raises(dg.DeepgramStreamError) as e:
            s.send_audio(bad)
        assert e.value.code == "chunk"
    assert not s.poll().failed
    s.send_audio(b"\x02\x00" * 4)
    assert wait_for(lambda: (2, b"\x02\x00" * 4) in frames)
    s.close()
    done.set()


def test_concurrent_close_calls_return_the_same_full_text(serve):
    def script(p):
        p.handshake()
        p.send(1, results("early", True).encode())
        read_text(p)                                    # CloseStream
        time.sleep(0.3)
        p.send(1, results("late", True).encode())
        p.send(1, METADATA.encode())
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "early")
    out = []
    ts = [threading.Thread(target=lambda: out.append(s.close())) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    assert out == ["early late"] * 3
    assert not s.poll().failed


def test_send_audio_racing_close_never_sends_audio_after_closestream(serve):
    frames, done = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, done, stop_on_close=True)

    s = open_stream(serve(script), close_drain=0.3)
    stop = threading.Event()
    errors = []

    def pump():
        while not stop.is_set():
            try:
                s.send_audio(b"\x01\x00" * 160)
            except dg.DeepgramStreamError as e:
                errors.append(e.code)
                return

    t = threading.Thread(target=pump)
    t.start()
    assert wait_for(lambda: len(frames) > 20)
    s.close()
    stop.set()
    t.join(5)
    done.set()
    k = kinds(frames)
    assert "CloseStream" in k and "audio" not in k[k.index("CloseStream"):]
    assert errors == ["closed"]


def test_thread_start_failure_closes_the_socket(serve, monkeypatch):
    seen = threading.Event()

    def script(p):
        p.handshake()
        try:
            p.read_frame()                              # a close frame or EOF: either way the client let go
        except EOFError:
            pass
        seen.set()

    srv = serve(script)

    def boom(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(dg.threading.Thread, "start", boom)
        with pytest.raises(dg.DeepgramStreamError) as e:
            open_stream(srv)
    assert e.value.code == "connect"
    assert seen.wait(3)


@pytest.mark.parametrize("code", ["timeout", "handshake", "closed"])
def test_connect_phase_timeout_settles_one_unit(monkeypatch, code):
    """wsclient reports a TCP-connect timeout and a post-request timeout with the same code, so a timeout is treated as
    possibly started (DeepgramStreamError, one unit), never as StreamNotStarted (r1 #5, documented rule)."""
    def fake(*a, **kw):
        raise dg.wsclient.WsError(code)

    monkeypatch.setattr(dg.wsclient, "connect", fake)
    with pytest.raises(dg.DeepgramStreamError) as e:
        dg.DeepgramStream(custody.Secret(KEY))
    assert e.value.code == "connect" and not isinstance(e.value, StreamNotStarted)
