"""The cockpit live-dictation chain for OpenAI, end to end with ONLY the provider faked (beside test_cockpit_mic_chain_e2e.py).

Real: ``levain serve`` (premium_stt ON, a signed custody-held openai key in the fake keychain), the stream open / chunk / close
routes, the session registry and meter, the websocket client and the REAL :class:`OpenAIStream` adapter. Fake: the provider, an
in-process websocket server on 127.0.0.1 reached through the adapter's test-injectable ``base_url`` / ``connect_opts``. No
browser, no real key, no network. Each test names the line whose revert turns it red."""
from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("cryptography")

from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.voice import meter as vmeter, openai_stream  # noqa: E402
from tests.test_cockpit_voice_routes import (_clock_before_the_openai_cutoff, js, rig, served, pytestmark)  # noqa: E402,F401
from tests.test_cockpit_voice_stream_routes import CLOSE, CHUNK, PCM, keyed, open_session, post  # noqa: E402
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401
from tests.test_voice_openai_stream import committed, delta, done  # noqa: E402
from tests.test_voice_wsclient import Server  # noqa: E402


class Oa:
    """The fake OpenAI Realtime server: what the session.update named, and the scripted events."""

    def __init__(self, stray: bool) -> None:
        self.stray = stray
        self.model = ""
        self.types: list[str] = []

    def script(self, p) -> None:
        p.handshake()
        p.conn.settimeout(0.05)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                fin, op, masked, data, key = p.read_frame()
            except (TimeoutError, OSError):
                continue
            except EOFError:
                break
            if op == 8:
                break
            if op != 1:
                continue
            msg = json.loads(data)
            self.types.append(msg["type"])
            if msg["type"] == "session.update":
                self.model = msg["session"]["audio"]["input"]["transcription"]["model"]
            elif msg["type"] == "input_audio_buffer.commit":
                p.send(1, committed("a"))
                if self.stray:
                    p.send(1, done("ghost", "stray words"))
                    time.sleep(0.3)
                p.send(1, delta("a", "hel"))
                p.send(1, done("a", "hello there"))


def run(rig, tmp_path, stray):
    oa = Oa(stray)
    srv = Server(oa.script)
    keyed(rig, "openai", uses=("stt",))
    base = f"ws://127.0.0.1:{srv.port}/v1/realtime?intent=transcription"
    try:
        with served(rig) as (port, httpd):
            httpd.voice_stream_factory = lambda secret: openai_stream.OpenAIStream(
                secret, base_url=base, connect_opts={"insecure_for_test": True, "allowed_hosts": None}, close_drain=3.0)
            status, body = open_session(port, "openai")
            assert status == 200, body
            sid = body["sid"]
            assert post(port, f"{CHUNK}?sid={sid}&seq=0", PCM, ctype="application/octet-stream")[0] == 200
            status, _h, out = post(port, f"{CLOSE}?sid={sid}")
            return oa, status, js(out), rig.rows()
    finally:
        srv.stop()


def test_openai_is_a_stream_provider_and_the_chain_settles_the_live_model_at_its_price(rig, tmp_path):
    """The adapter sends gpt-live-transcribe and the ledger reserves and settles that same model at the $0.017 / minute row.
    Red with: "openai" out of STREAM_PROVIDERS (409 no_stream), the route reserving the batch model (cost 3x short), or the
    adapter's model literal and the price table disagreeing."""
    oa, status, out, rows = run(rig, tmp_path, stray=False)
    assert (status, out) == (200, {"text": "hello there"})
    assert oa.model == "gpt-live-transcribe"
    assert [(r.provider, r.use, r.model, r.status) for r in rows] == [("openai", "stt", "gpt-live-transcribe", "ok")]
    entry = vmeter.DEFAULT_PRICES.find("openai", "stt", "gpt-live-transcribe")
    assert entry is not None and (entry.micros, entry.per) == (17_000, 60)
    assert rows[0].units == 1 and rows[0].cost_micros == -(-17_000 // 60)     # one second at the live row, rounded up


def test_a_stray_completion_for_an_uncommitted_item_never_reaches_the_text(rig, tmp_path):
    """Red with: `_take` recording item ids from any event instead of from input_audio_buffer.committed only."""
    oa, status, out, rows = run(rig, tmp_path, stray=True)
    assert (status, out) == (200, {"text": "hello there"})
    assert [r.status for r in rows] == ["ok"]
