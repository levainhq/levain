"""levain.voice.openai_stream against an in-process fake OpenAI Realtime websocket server (loopback ws:// via the test flag).

Each test names, in its docstring, the line of levain/voice/openai_stream.py whose revert turns it red."""

from __future__ import annotations

import base64
import json
import struct
import threading
import time

import pytest

from levain.voice import custody, openai_stream as oa
from levain.voice.stream_session import StreamNotStarted
from tests.test_voice_wsclient import Server

KEY = "oaSECRETkey123"
OPEN = {"insecure_for_test": True, "allowed_hosts": None}


def ev(**kw):
    return json.dumps(kw).encode()


def delta(item, text):
    return ev(type="conversation.item.input_audio_transcription.delta", item_id=item, content_index=0, delta=text)


def done(item, text):
    return ev(type="conversation.item.input_audio_transcription.completed", item_id=item, content_index=0, transcript=text)


def committed(item):
    return ev(type="input_audio_buffer.committed", item_id=item)


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
    base = f"ws://127.0.0.1:{srv.port}/v1/realtime?intent=transcription"
    kw.setdefault("close_drain", 2.0)
    return oa.OpenAIStream(custody.Secret(KEY), base_url=base, connect_opts=dict(OPEN), **kw)


def wait_for(pred, t=3.0):
    end = time.monotonic() + t
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def collect(peer, out, until):
    peer.conn.settimeout(0.05)
    while not until.is_set():
        try:
            fin, op, masked, data, key = peer.read_frame()
        except (TimeoutError, OSError):
            continue
        except EOFError:
            return
        out.append((op, data))
        if op == 8:
            return


def msgs(frames):
    return [json.loads(d) for op, d in frames if op == 1]


def pcm16k(n):
    return struct.pack(f"<{n}h", *range(n))


def test_handshake_url_header_and_session_update(serve):
    """Revert: URL_BASE's `?intent=transcription`, the `Bearer` header, or the `_configure` session.update body."""
    seen, frames, stop = {}, [], threading.Event()

    def script(p):
        p.handshake()
        seen["line"] = p.request.split(b"\r\n")[0].decode()
        seen["auth"] = p.header("Authorization")
        collect(p, frames, stop)

    s = open_stream(serve(script))
    assert wait_for(lambda: frames)
    stop.set()
    s.close()
    assert seen["line"].split(" ")[1] == "/v1/realtime?intent=transcription"
    assert seen["auth"] == f"Bearer {KEY}"
    assert oa.URL_BASE == "wss://api.openai.com/v1/realtime?intent=transcription"
    first = msgs(frames)[0]
    assert first == {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
        "format": {"type": "audio/pcm", "rate": 24000},
        "transcription": {"model": oa.TRANSCRIBE_MODEL}, "turn_detection": None}}}}


def test_audio_is_resampled_to_24k_and_commits_on_the_byte_count(serve):
    """Revert: Resampler16to24.feed's step (`(2 * k) // 3`) or the `>= self._commit_every` commit trigger in send_audio."""
    frames, stop = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, stop)

    s = open_stream(serve(script), commit_every=1000)
    s.send_audio(pcm16k(400))            # 800 B in -> 1200 B out; over 1000 so a commit follows
    assert wait_for(lambda: len(msgs(frames)) >= 3)
    m = msgs(frames)
    appended = [x for x in m if x["type"] == "input_audio_buffer.append"]
    pcm = base64.b64decode(appended[0]["audio"])
    assert len(pcm) == 1198              # 600 samples, the last waits for its right neighbour
    out = struct.unpack("<599h", pcm)
    assert out[0] == 0 and out[1] == 0 and out[2] == 1 and out[3] == 2     # positions 0, 2/3, 4/3, 2: 0, 0.67, 1.33, 2
    assert [x["type"] for x in m[1:]] == ["input_audio_buffer.append", "input_audio_buffer.commit"]
    stop.set()
    s.close()


def test_resampler_is_chunking_independent():
    """Revert: the carried `_odd` byte or `_last` sample in Resampler16to24."""
    data = pcm16k(1000)
    whole = oa.Resampler16to24().feed(data)
    r = oa.Resampler16to24()
    pieces = b"".join(r.feed(data[i:i + 7]) for i in range(0, len(data), 7))   # odd sizes split samples
    assert pieces == whole and len(whole) // 2 == 1499


def test_partial_and_final_folding_in_commit_order(serve):
    """Revert: `rec[0] += text` (delta folding), the `rec[1] = text` completion, or the `_order` join in poll()."""
    def script(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, committed("b"))
        p.send(1, delta("a", "hel"))
        p.send(1, delta("a", "lo"))
        p.send(1, delta("b", "wor"))
        p.send(1, done("b", "world"))        # out of order: b completes before a
        time.sleep(1.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "world")
    up = s.poll()
    assert up.partial == "hello" and not up.failed        # a is the newest pending item
    s.close()


def test_final_replaces_partial_and_orders_by_item(serve):
    """Revert: `rec[0] = ""` on completion or the commit-order join."""
    def script(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, committed("b"))
        p.send(1, done("b", "second"))
        p.send(1, done("a", "first"))
        time.sleep(1.0)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "first second")
    assert s.poll().partial == ""
    s.close()


def test_error_event_and_failed_transcription_fail_with_fixed_reason(serve):
    """Revert: the `kind == "error" or kind.endswith(".failed")` line in _parse."""
    for body in (ev(type="error", error={"type": "server_error", "message": "SECRET words"}),
                 ev(type="conversation.item.input_audio_transcription.failed", item_id="a",
                    error={"message": "SECRET words"})):
        def script(p, body=body):
            p.handshake()
            p.send(1, body)
            time.sleep(0.5)

        s = open_stream(serve(script))
        assert wait_for(lambda: s.poll().failed)
        assert s.reason == "provider_error"
        with pytest.raises(oa.OpenAIStreamError) as e:
            s.send_audio(b"\x00\x00")
        assert "SECRET" not in str(e.value)
        s.close()


@pytest.mark.parametrize("msg", [b"not json {", b"[1]", b'{"type": 5}',
                                 b'{"type": "conversation.item.input_audio_transcription.delta", "item_id": 1, "delta": "x"}',
                                 b'{"type": "conversation.item.input_audio_transcription.completed", "item_id": "a"}',
                                 b'{"type": "input_audio_buffer.committed"}'])
def test_malformed_fails(serve, msg):
    """Revert: any isinstance guard in _parse."""
    def script(p):
        p.handshake()
        p.send(1, msg)
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed)
    assert s.reason == "malformed"
    s.close()


def test_close_flushes_the_tail_and_waits_for_the_last_completion(serve):
    """Revert: the `_commit_locked()` call in close(), or the `_check_end` completion count."""
    seen = {}

    def script(p):
        p.handshake()
        frames = []
        while True:
            fin, op, masked, data, key = p.read_frame()
            if op == 1 and json.loads(data)["type"] == "input_audio_buffer.commit":
                break
            frames.append((op, data))
        seen["types"] = [json.loads(d)["type"] for op, d in frames if op == 1]
        p.send(1, committed("z"))
        time.sleep(0.2)
        p.send(1, done("z", "the tail"))
        time.sleep(1.0)

    s = open_stream(serve(script))
    s.send_audio(pcm16k(3000))          # 9000 B out: over 100 ms, under commit_every, so only close() commits
    text = s.close()
    assert text == "the tail" and not s.poll().failed
    assert seen["types"] == ["session.update", "input_audio_buffer.append"]
    assert s.close() == text
    with pytest.raises(oa.OpenAIStreamError):
        s.send_audio(b"\x00\x00")


def test_close_pads_a_tail_under_100ms_and_with_no_audio_sends_nothing(serve):
    """Revert: the zero-pad branch in close(), or the `if self._uncommitted` guard."""
    frames, stop = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, stop)

    s = open_stream(serve(script))
    assert s.close() == "" and not s.poll().failed        # no audio: no commit, nothing to wait for
    stop.set()
    assert [m["type"] for m in msgs(frames)] == ["session.update"]

    frames2, stop2 = [], threading.Event()

    def script2(p):
        p.handshake()
        collect(p, frames2, stop2)

    s = open_stream(serve(script2), close_drain=0.3)
    s.send_audio(pcm16k(10))                                # 30 B out: far under 100 ms
    s.close()
    stop2.set()
    audio = sum(len(base64.b64decode(m["audio"])) for m in msgs(frames2) if m["type"] == "input_audio_buffer.append")
    assert audio == oa.MIN_COMMIT_BYTES
    assert msgs(frames2)[-1]["type"] == "input_audio_buffer.commit"


def test_close_deadline_keeps_text_and_marks_incomplete(serve):
    """Revert: the `self._fail("incomplete")` in close()."""
    def script(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, committed("b"))
        p.send(1, done("a", "said"))
        p.send(1, delta("b", "tail"))
        time.sleep(1.5)

    s = open_stream(serve(script), close_drain=0.3, commit_every=1000)
    s.send_audio(pcm16k(400))           # commit 1 (periodic)
    s.send_audio(pcm16k(300))           # the tail: commit 2 at close, never completed
    assert wait_for(lambda: s.poll().partial == "tail")
    assert s.close() == "said tail"
    assert s.poll().failed and s.reason == "incomplete"


def test_provider_close_before_we_ask_is_failed(serve):
    """Revert: the `self._fail("provider_closed")` after `kind == "close"`."""
    def script(p):
        p.handshake()
        p.send(8, b"\x03\xe8")
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed) and s.reason == "provider_closed"
    s.close()


def test_overflow_is_bounded(serve, monkeypatch):
    """Revert: the `len(rec[0]) + len(text) <= MAX_TEXT_CHARS` or `self._chars + ...` bound in _take."""
    monkeypatch.setattr(oa, "MAX_TEXT_CHARS", 20)

    def script(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, delta("a", "y" * 30))
        time.sleep(0.5)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().failed) and s.reason == "overflow"
    s.close()

    def script2(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, done("a", "z" * 30))
        time.sleep(0.5)

    s = open_stream(serve(script2))
    assert wait_for(lambda: s.poll().failed) and s.reason == "overflow"
    s.close()


def test_bad_chunk_is_refused_and_the_stream_lives(serve):
    """Revert: the `not pcm or len(pcm) > SEND_MAX` guard."""
    s = open_stream(serve(lambda p: (p.handshake(), time.sleep(1.0))))
    for bad in (b"", b"\x00" * (oa.SEND_MAX + 1)):
        with pytest.raises(oa.OpenAIStreamError) as e:
            s.send_audio(bad)
        assert e.value.code == "chunk"
    assert not s.poll().failed
    s.close()


def test_connect_refusals_are_not_started():
    """Revert: the `("url", "header", "connect")` mapping in _open, or the allowed_hosts default."""
    with pytest.raises(StreamNotStarted):
        oa.OpenAIStream(custody.Secret(KEY), base_url="ws://127.0.0.1:9/x", connect_opts={})
    with pytest.raises(StreamNotStarted):
        oa.OpenAIStream(custody.Secret(KEY), base_url="wss://evil.example/v1/realtime")
    with pytest.raises(StreamNotStarted):
        oa.OpenAIStream(custody.Secret(KEY + "\r\nX: 1"))


@pytest.mark.parametrize("code", ["timeout", "handshake", "closed"])
def test_connect_timeout_and_later_failures_settle_one_unit(monkeypatch, code):
    """Revert: the `else "failed"` branch of _open (a timeout must not read as StreamNotStarted)."""
    def fake(*a, **kw):
        raise oa.wsclient.WsError(code)

    monkeypatch.setattr(oa.wsclient, "connect", fake)
    with pytest.raises(oa.OpenAIStreamError) as e:
        oa.OpenAIStream(custody.Secret(KEY))
    assert e.value.code == "connect" and not isinstance(e.value, StreamNotStarted)


def test_connect_timeout_real_socket_is_not_started():
    """A TCP connect timeout is wsclient code "connect" -> StreamNotStarted (nothing reached OpenAI). Revert: same mapping."""
    with pytest.raises(StreamNotStarted):
        oa.OpenAIStream(custody.Secret(KEY), base_url="ws://127.0.0.1:1/x",
                        connect_opts={"insecure_for_test": True, "allowed_hosts": None, "connect_timeout": 0.5})


def test_handshake_refused_after_connect_is_provider_failure(serve):
    """Revert: the OpenAIStreamError("connect") raise after `err`."""
    srv = serve(lambda p: p.handshake(status="401 Unauthorized"))
    with pytest.raises(oa.OpenAIStreamError) as e:
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


def test_key_in_no_attribute_repr_frame_exception_or_sent_frame(serve, caplog):
    """Revert: the `headers = None` / `del secret` lines, or any put of the key into a frame or attribute."""
    frames, stop = [], threading.Event()

    def script(p):
        p.handshake()
        collect(p, frames, stop)

    with caplog.at_level("DEBUG"):
        s = open_stream(serve(script))
        s.send_audio(pcm16k(3000))
        assert "SECRET" not in repr(s)
        for name, val in vars(s).items():
            assert not _holds(val), name
        assert not any(_holds(v) for v in vars(s._ws).values())
        s.close()
    stop.set()
    assert not any(b"SECRET" in d for op, d in frames)
    assert "SECRET" not in caplog.text

    for call in (lambda: oa.OpenAIStream(custody.Secret(KEY + "\r\nX: 1")),
                 lambda: oa.OpenAIStream(custody.Secret(KEY), base_url="wss://evil.example/v1/realtime"),
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
    with pytest.raises(oa.OpenAIStreamError) as e:
        s.send_audio(b"\x00\x00")
    _walk_tb(e.value)
    s.close()


def test_factory_returns_stream_interface(monkeypatch):
    """Revert: `return OpenAIStream(secret)` in factory."""
    seen = {}

    class Fake:
        def __init__(self, secret, **kw):
            seen["secret"] = secret

    monkeypatch.setattr(oa, "OpenAIStream", Fake)
    sec = custody.Secret(KEY)
    assert isinstance(oa.factory(sec), Fake) and seen["secret"] is sec


def test_thread_start_failure_closes_the_socket(serve, monkeypatch):
    """Revert: the `ws.close(wait=0)` in the not-started branch."""
    seen = threading.Event()

    def script(p):
        p.handshake()
        try:
            while True:
                p.read_frame()
        except EOFError:
            pass
        seen.set()

    srv = serve(script)

    def boom(self):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(oa.threading.Thread, "start", boom)
        with pytest.raises(oa.OpenAIStreamError) as e:
            open_stream(srv)
    assert e.value.code == "connect"
    assert seen.wait(3)


def test_a_completion_for_an_item_never_committed_is_ignored(serve):
    """A stray completed / delta for an id the server never acknowledged with input_audio_buffer.committed neither counts
    toward the closing drain nor reaches the text. Revert: the `rec is None` branch in _take creating a record for a
    non-committed event."""
    def script(p):
        p.handshake()
        p.send(1, committed("a"))
        p.send(1, delta("ghost", "stray partial"))
        p.send(1, done("ghost", "stray words"))
        time.sleep(0.4)
        p.send(1, done("a", "real words"))
        time.sleep(1.0)

    s = open_stream(serve(script))
    assert wait_for(lambda: s.poll().final == "real words")
    assert "stray" not in s.poll().final + s.poll().partial
    assert not s.poll().failed
    s.close()


def test_a_stray_completion_cannot_end_the_closing_drain_early(serve):
    """After close() commits, a completion for a never-committed id used to satisfy `_resolved >= _commits` and end the
    drain before the real item completed, returning the stray text. Revert: the same `_take` guard."""
    def script(p):
        p.handshake()
        frames, stop = [], threading.Event()
        th = threading.Thread(target=collect, args=(p, frames, stop), daemon=True)
        th.start()
        assert wait_for(lambda: any(m["type"] == "input_audio_buffer.commit" for m in msgs(frames)))
        p.send(1, committed("a"))
        p.send(1, done("ghost", "stray words"))
        time.sleep(0.5)
        p.send(1, done("a", "real words"))
        time.sleep(0.5)
        stop.set()

    s = open_stream(serve(script))
    s.send_audio(pcm16k(3200))
    assert s.close() == "real words"


def test_the_sent_model_is_the_priced_model():
    """One table feeds the model the adapter sends and the model the ledger reserves and prices. Revert: TRANSCRIBE_MODEL
    back to a literal, or the openai row dropped from providers.STREAM_STT_MODELS."""
    from levain.voice import meter, providers
    assert oa.TRANSCRIBE_MODEL == providers.STREAM_STT_MODELS["openai"]
    for provider, model in providers.STREAM_STT_MODELS.items():
        assert meter.DEFAULT_PRICES.find(provider, "stt", model) is not None, (provider, model)
