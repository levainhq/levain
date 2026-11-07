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
except ImportError:                                     # pragma: no cover - only in the S2b worktree before the merge
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


def test_keepalive_when_idle_and_bounded(serve):
    msgs = []

    def script(p):
        p.handshake()
        end = time.monotonic() + 1.6
        p.conn.settimeout(0.3)
        while time.monotonic() < end:
            try:
                msgs.append(read_text(p))
            except OSError:
                pass

    s = open_stream(serve(script), keepalive_after=0.2)
    time.sleep(1.7)
    s.close()
    kinds = [json.loads(m)["type"] for m in msgs if m]
    assert kinds.count("KeepAlive") >= 2 and kinds.count("KeepAlive") <= dg.MAX_KEEPALIVES
    assert set(kinds) <= {"KeepAlive", "CloseStream"}


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
