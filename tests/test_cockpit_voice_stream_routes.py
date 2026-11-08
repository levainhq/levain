"""Cockpit mic S3: the live-dictation routes (open / chunk / close), the ``stream`` flag on voice.json and the
Permissions-Policy header, through the real ``levain serve`` envelope.

The rig is tests/test_cockpit_voice_routes.py's (a real signed deepgram key record, a fake ``security``, a fake provider
HTTP); the provider STREAM is a fake set on the server (``voice_stream_factory``), so nothing here reaches a network, a
person's keychain or a microphone. ``VALUE`` is a key no code in the repo spells: finding it anywhere is this file's leak.

Each test names the line whose revert turns it red."""
from __future__ import annotations

import http.client
import json
import logging
import time

import pytest

pytest.importorskip("cryptography")

from levain.cockpit import voice as cockpit_voice  # noqa: E402
from levain.voice import stream_session  # noqa: E402
from tests.test_cockpit_k2a import TOKEN  # noqa: E402
from tests.test_cockpit_pairing_http import PROXY  # noqa: E402
from tests.test_cockpit_voice_routes import (MESH_TOKEN, VALUE, call, head, js, no_leak, raw, rig,  # noqa: E402,F401
                                             served, pytestmark, _clock_before_the_openai_cutoff)
from tests.test_cockpit_voice_routes import Rig  # noqa: E402,F401
from tests.test_cockpit_threads_mount import qdir  # noqa: E402,F401  (fixture: the consent directory the server reads)
from levain.webpush import notify_tokens, presence_tokens  # noqa: E402

OPEN = cockpit_voice.STREAM_OPEN
CHUNK = cockpit_voice.STREAM_CHUNK
CLOSE = cockpit_voice.STREAM_CLOSE
PCM = b"\x01\x00" * 4000                 # 250 ms: 8000 bytes
FINAL = "hello there"


class FakeStream:
    def __init__(self, secret_value: str) -> None:
        self.key_seen = secret_value
        self.sent: list[bytes] = []
        self.closed = 0

    def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    def poll(self) -> stream_session.StreamUpdate:
        return stream_session.StreamUpdate(partial="par", final=FINAL if self.sent else "")

    def close(self) -> str:
        self.closed += 1
        return FINAL


class Factory:
    def __init__(self) -> None:
        self.streams: list[FakeStream] = []
        self.raises: Exception | None = None

    def __call__(self, secret) -> FakeStream:
        if self.raises is not None:
            raise self.raises
        stream = FakeStream(secret.reveal())
        self.streams.append(stream)
        return stream


def fx(httpd) -> Factory:
    httpd.voice_stream_factory = Factory()
    return httpd.voice_stream_factory


def post(port, path, body=b"", *, ctype=None, token=TOKEN, headers=None):
    return call(port, "POST", path, body, ctype=ctype, token=token, headers=headers)


def open_session(port, provider="deepgram"):
    status, _h, body = post(port, f"{OPEN}?provider={provider}")
    return status, js(body)


def chunk(port, sid, seq, pcm=PCM, *, ctype="application/octet-stream", token=TOKEN, headers=None):
    return post(port, f"{CHUNK}?sid={sid}&seq={seq}", pcm, ctype=ctype, token=token, headers=headers)


def keyed(rig, provider="deepgram", uses=("stt", "tts")):
    rig.add_key(provider=provider, uses=uses, source=f"levain-voice-{provider}-s3")


def statuses(rig):
    return [r.status for r in rig.rows()]


# --- the flag on voice.json ----------------------------------------------------------------------------------------------

def test_voice_json_carries_stream_true_only_for_a_keyed_deepgram_stt_with_premium_stt_on(rig):
    """Red with: the `chosen = stream_provider(rows) if premium_stt else None` line in serve_voice dropped or inverted (the key then shows
    wrong in one of the four cases), or stream_offered ignoring `key == "set"` / `"stt" in uses`."""
    with served(rig) as (port, _h):
        assert "stream" not in js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])          # no key at all
    keyed(rig)
    with served(rig) as (port, _h):
        got = js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])
        assert got["stream"] is True
    with served(rig, premium_stt=False) as (port, _h):                                    # the switch off
        assert "stream" not in js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])


def test_voice_json_flag_absent_for_a_tts_only_key(rig):
    """A deepgram key granted tts only offers no dictation. Red with: stream_offered ignoring `"stt" in uses`."""
    keyed(rig, uses=("tts",))
    with served(rig) as (port, _h):
        assert "stream" not in js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])


def test_voice_json_names_openai_first_when_both_are_keyed_for_stt(rig):
    """OpenAI is listed before Deepgram and both are keyed for stt, both have stream adapters, so the server names the
    first (openai) and the key is in no part of the body. Red with: "openai" out of STREAM_PROVIDERS (it then names
    deepgram), or `body["stream_provider"] = chosen` dropped."""
    keyed(rig, "openai")
    keyed(rig)
    with served(rig) as (port, _h):
        body_raw = call(port, "GET", cockpit_voice.VOICE_JSON)[2]
        got = js(body_raw)
        names = [r["name"] for r in got["providers"] if r["key"] == "set" and "stt" in r["uses"]]
        assert names == ["openai", "deepgram"]
        assert got["stream"] is True and got["stream_provider"] == "openai"
        assert VALUE.encode() not in body_raw


def test_voice_json_names_deepgram_when_only_deepgram_is_keyed_and_openai_tts_only(rig):
    """A keyed OpenAI row without stt never qualifies. Red with: stream_provider ignoring `"stt" in uses`."""
    keyed(rig, "openai", uses=("tts",))
    keyed(rig)
    with served(rig) as (port, _h):
        got = js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])
        assert got["stream"] is True and got["stream_provider"] == "deepgram"


def test_stream_provider_skips_a_keyed_row_with_no_adapter_and_is_none_when_none_qualifies():
    """Pure rows: a keyed stt row outside STREAM_PROVIDERS is skipped for the next qualifying one; none left is None.
    Red with: the `row.get("name") in STREAM_PROVIDERS` clause dropped (it names the adapter-less row)."""
    rows = [{"name": "inworld", "key": "set", "uses": ["stt"]}, {"name": "deepgram", "key": "set", "uses": ["stt"]}]
    assert cockpit_voice.stream_provider(rows) == "deepgram"
    assert cockpit_voice.stream_provider(rows[:1]) is None
    assert cockpit_voice.stream_provider([{"name": "openai", "key": "not_set", "uses": []}]) is None


def test_stream_providers_are_openai_and_deepgram_and_each_has_a_model_and_a_price(rig):
    """Every stream provider has a row in STREAM_STT_MODELS and that model has a price row, so a stream cannot be sent
    under a model the ledger does not price. Red with: a provider added to STREAM_PROVIDERS without both rows."""
    from levain.voice import deepgram_stream, meter, openai_stream, providers
    assert tuple(cockpit_voice.STREAM_PROVIDERS) == ("openai", "deepgram")
    assert deepgram_stream.factory and openai_stream.factory
    for name in cockpit_voice.STREAM_PROVIDERS:
        assert meter.DEFAULT_PRICES.find(name, "stt", providers.STREAM_STT_MODELS[name]) is not None


def test_open_builds_each_providers_factory_with_the_key_reaching_only_it(rig, monkeypatch):
    """No test factory set: the route builds the named provider's adapter and only that one gets the key. Red with: a key
    read for the wrong provider, or the factory lookup mixed up."""
    from levain.voice import deepgram_stream, openai_stream
    seen = []

    def make(name):
        def f(secret):
            seen.append((name, secret.reveal()))
            return FakeStream(secret.reveal())
        return f
    monkeypatch.setattr(deepgram_stream, "factory", make("deepgram"))
    monkeypatch.setattr(openai_stream, "factory", make("openai"))
    keyed(rig, "openai")
    keyed(rig)
    with served(rig) as (port, _h):
        status, body = open_session(port, "openai")
        assert status == 200 and [n for n, _k in seen] == ["openai"]
        post(port, f"{CLOSE}?sid={body['sid']}")
        status, body = open_session(port, "deepgram")
        assert status == 200 and [n for n, _k in seen] == ["openai", "deepgram"]
        assert all(k not in str(body) for _n, k in seen) and seen[0][1] == VALUE


# --- premium_stt off -----------------------------------------------------------------------------------------------------

def test_off_is_404_on_all_three_before_any_read_even_with_a_key(rig):
    """Nothing is reserved, read or built. Red with: the OPEN gate and the forced-off refusal in _voice_stream_answer dropped
    (the routes then open a session) or moved after the length / registry lines (a body or meter is touched)."""
    keyed(rig)
    with served(rig, premium_stt=False) as (port, httpd):
        factory = fx(httpd)
        for path in (f"{OPEN}?provider=deepgram", f"{CHUNK}?sid=x&seq=0", f"{CLOSE}?sid=x"):
            status, hdrs, body = post(port, path, PCM if path.startswith(CHUNK) else b"",
                                      ctype="application/octet-stream")
            assert (status, js(body)["error"], js(body)["message"]) == (
                404, "not_found", cockpit_voice.WORDS["premium_stt_off"]), path
        got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid=x&seq=0", ctype="application/octet-stream", clen="99999999"))
        assert (got, body["message"]) == (404, cockpit_voice.WORDS["premium_stt_off"]) and hdrs["connection"] == "close"
        assert httpd.voice_streams is None and factory.streams == []
    assert rig.rows() == []


# --- auth parity with transcribe -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bind", ["loopback", "mesh", "proxied"])
def test_every_stream_route_needs_the_surface_token_on_every_bind(rig, bind):
    """Presence and notify tokens are not the credential; a refused request reserves nothing. Red with: the token check in
    _voice_stream_answer dropped, or the carve-out in do_POST reached after a generic gate."""
    tap = presence_tokens.create(rig.qdir, "tap")[1]
    notice = notify_tokens.create(rig.qdir, "svc")[1]
    keyed(rig)
    with served(rig, mesh=bind == "mesh") as (port, httpd):
        factory = fx(httpd)
        base = dict(PROXY) if bind == "proxied" else {}
        for cred in ("none", "wrong", "presence", "notify") + (() if bind == "loopback" else ("owner_key",)):
            hdrs, token = dict(base), None
            if cred == "wrong":
                token = "not-the-token"
            elif cred == "owner_key":
                token = TOKEN
            elif cred == "presence":
                hdrs[presence_tokens.HEADER] = tap
            elif cred == "notify":
                hdrs[notify_tokens.HEADER] = notice
                hdrs["Authorization"] = f"Bearer {notice}"
            for path in (f"{OPEN}?provider=deepgram", f"{CHUNK}?sid=x&seq=0", f"{CLOSE}?sid=x"):
                status, _h, body = post(port, path, PCM if "chunk" in path else b"", ctype="application/octet-stream",
                                        token=token, headers=hdrs)
                assert (status, js(body)["error"]) == (403, "credential_required"), (bind, cred, path, body)
        token = TOKEN if bind == "loopback" else MESH_TOKEN
        assert open_session_with(port, token, base)[0] == 200
        assert len(factory.streams) == 1
    assert statuses(rig) == ["ok"] or statuses(rig) == ["released"]


def open_session_with(port, token, headers):
    status, _h, body = post(port, f"{OPEN}?provider=deepgram", token=token, headers=headers)
    return status, js(body)


def test_a_credential_that_names_no_client_is_403_and_no_room_host_is_404(rig, monkeypatch):
    """Red with: the owner check dropped from _voice_stream_answer; the room-host condition dropped from gate 1."""
    from levain import web_server
    keyed(rig)
    with served(rig, room=False) as (port, httpd):
        fx(httpd)
        assert post(port, f"{OPEN}?provider=deepgram")[0] == 404
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        monkeypatch.setattr(web_server._Handler, "_thread_owner", lambda self: None)
        status, hdrs, body = post(port, f"{OPEN}?provider=deepgram")
        assert (status, js(body)["error"], js(body)["message"]) == (403, "forbidden", cockpit_voice.WORDS["no_owner"])
        assert hdrs.get("connection") == "close" and factory.streams == []
    assert rig.rows() == []


def test_a_cross_site_post_is_refused_before_the_stream_gates(rig):
    """Red with: the carve-out in do_POST placed before `_refuse_write_origin`."""
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        status, _h, _b = post(port, f"{OPEN}?provider=deepgram", headers={"Sec-Fetch-Site": "cross-site"})
        assert status == 403 and factory.streams == []
    assert rig.rows() == []


# --- open ----------------------------------------------------------------------------------------------------------------

def test_open_returns_a_sid_hands_the_key_to_the_factory_once_and_leaks_it_nowhere(rig, caplog):
    """The key reaches the factory (and only there); the reply, the reservation row and every log line hold no value.
    Red with: read_key not passed to registry.open (no key reaches the factory), or the sid reply carrying more."""
    keyed(rig)
    caplog.set_level(logging.DEBUG)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        status, body = open_session(port)
        assert status == 200 and set(body) == {"sid"} and 1 <= len(body["sid"]) <= 128
        assert [s.key_seen for s in factory.streams] == [VALUE]
        assert statuses(rig) == ["reserved"]
        assert rig.rows()[0].units == stream_session.CEILING_SECONDS and rig.rows()[0].use == "stt"
        no_leak(json.dumps(body), caplog.text)
    no_leak(caplog.text)


def test_open_refuses_a_provider_with_no_stream_a_bad_query_and_a_body(rig, monkeypatch):
    """Red with: the STREAM_PROVIDERS test dropped (openai opens a deepgram stream), `_query_provider` skipped, or the
    zero-length check on open dropped. (openai is made adapter-less by narrowing STREAM_PROVIDERS.)"""
    monkeypatch.setattr(cockpit_voice, "STREAM_PROVIDERS", ("deepgram",))
    keyed(rig, "openai")
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        s, b = open_session(port, "openai")
        assert (s, b["error"]) == (409, "no_stream")
        assert post(port, f"{OPEN}?provider=deepgram&x=1")[0] == 400
        assert post(port, OPEN)[0] == 400
        assert post(port, f"{OPEN}?provider=nope")[0] == 400
        s, _h, body = post(port, f"{OPEN}?provider=deepgram", b"{}", ctype="application/json")
        assert (s, js(body)["error"]) == (413, "too_large")
        assert factory.streams == []
    assert rig.rows() == []


def test_the_revoke_refusal_runs_before_the_open_reserves_or_reads_a_key(rig, monkeypatch):
    """Red with: `revoke_refusal` moved after `registry.open` (a row is reserved and the factory built) or dropped."""
    keyed(rig)
    monkeypatch.setattr(cockpit_voice, "revoke_refusal", lambda q, root: "revoke_unreachable")
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        status, _h, body = post(port, f"{OPEN}?provider=deepgram")
        assert (status, js(body)["error"], js(body)["message"]) == (
            503, "revoke_unreachable", cockpit_voice.WORDS["revoke_unreachable"])
        assert factory.streams == []
    assert rig.rows() == []


def test_a_missing_key_is_refused_as_transcribe_refuses_it_and_nothing_stays_reserved(rig):
    """Red with: stream_refusal not mapping key_unavailable through _key_refusal (409 no_stt_key)."""
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        status, _h, body = post(port, f"{OPEN}?provider=deepgram")
        assert status == 409 and js(body)["error"] in {"no_stt_key", "no_key"} and factory.streams == []
    assert statuses(rig) in ([], ["released"])


def test_a_factory_that_raises_with_the_key_in_its_text_leaks_nothing(rig, caplog):
    """Red with: a log line or reply carrying `str(exc)` anywhere on the open path."""
    keyed(rig)
    caplog.set_level(logging.DEBUG)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        factory.raises = RuntimeError("connect failed with " + VALUE)
        status, _h, body = post(port, f"{OPEN}?provider=deepgram")
        assert (status, js(body)["error"]) == (502, "provider_failed")
        no_leak(body.decode(), caplog.text)
    no_leak(caplog.text)
    assert "reserved" not in statuses(rig)


def test_only_one_stream_is_live_at_a_time(rig):
    """Red with: `max_sessions=cockpit_voice.STREAM_MAX_SESSIONS` dropped from the registry build (the default is 4)."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        assert open_session(port)[0] == 200
        status, body = open_session(port)
        assert (status, body["error"], body["message"]) == (503, "too_many_sessions",
                                                           stream_session.WORDS["too_many_sessions"])
        assert statuses(rig) == ["reserved"]


# --- chunk ---------------------------------------------------------------------------------------------------------------

def test_chunks_in_sequence_return_the_mailbox_and_a_repeat_or_gap_is_a_fixed_words_refusal(rig):
    """Red with: the seq check removed from the session path, or `seq` echoed from the query not parsed (the first reply)."""
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        sid = open_session(port)[1]["sid"]
        status, _h, body = chunk(port, sid, 0)
        assert status == 200 and js(body) == {"seq": 0, "partial": "par", "final": FINAL}
        for seq in (0, 5, "x", "-1", "01x", "1" * 12):
            status, _h, body = chunk(port, sid, seq)
            assert (status, js(body)["error"], js(body)["message"]) == (409, "bad_sequence",
                                                                       stream_session.WORDS["bad_sequence"]), seq
        assert chunk(port, sid, 1)[0] == 200
        assert factory.streams[0].sent == [PCM, PCM]


def test_chunk_body_rules_size_type_and_whole_samples(rig):
    """Red with: the `clen > STREAM_CHUNK_MAX` test dropped (the 64 KiB + 2 body is read), the content-type test dropped,
    or the whole-samples rule dropped from the session."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        big = b"\x00\x00" * (cockpit_voice.STREAM_CHUNK_MAX // 2 + 1)
        got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid={sid}&seq=0", ctype="application/octet-stream",
                                         clen=str(len(big))))
        assert (got, body["error"]) == (413, "too_large") and hdrs["connection"] == "close"
        assert chunk(port, sid, 0, PCM, ctype="audio/webm")[0] == 415
        assert chunk(port, sid, 0, PCM, ctype="application/json")[0] == 415
        s, _h, b = chunk(port, sid, 0, b"\x01\x00\x02")
        assert (s, js(b)["error"]) == (400, "bad_chunk")
        s, _h, b = chunk(port, sid, 0, b"")
        assert (s, js(b)["error"]) == (400, "bad_chunk")
        top = b"\x01\x00" * (cockpit_voice.STREAM_CHUNK_MAX // 2)           # exactly 64 KiB passes the cap
        assert chunk(port, sid, 0, top)[0] == 200


def test_chunks_do_not_take_a_voice_slot(rig):
    """Chunks bypass voice_gate (open and close do too). Red with: a `voice_gate.acquire` added to the stream path."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert httpd.voice_gate.acquire(blocking=False) and httpd.voice_gate.acquire(blocking=False)
        try:
            assert chunk(port, sid, 0)[0] == 200
            assert post(port, f"{CLOSE}?sid={sid}")[0] == 200
        finally:
            httpd.voice_gate.release()
            httpd.voice_gate.release()


def test_a_wrong_owner_and_an_unknown_sid_get_the_same_404(rig):
    """The ipad surface token is another owner (`cockpit:ipad`). Red with: ownership not checked in registry.get (200), or
    the unknown and wrong-owner answers differing."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        other = chunk(port, sid, 0, token=MESH_TOKEN)
        unknown = chunk(port, "no-such-sid", 0)
        assert other[0] == unknown[0] == 404 and other[2] == unknown[2]
        assert js(other[2])["error"] == "unknown_session"
        assert post(port, f"{CLOSE}?sid={sid}", token=MESH_TOKEN)[0] == 404
        assert chunk(port, "s" * 129, 0)[0] == 404
        assert chunk(port, sid, 0)[0] == 200                 # the owner is unharmed


# --- close ---------------------------------------------------------------------------------------------------------------

def test_close_returns_the_text_settles_the_meter_and_collects_the_session(rig):
    """Red with: close not calling session.close (the row stays reserved), or `{"text": ...}` renamed."""
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        status, _h, body = post(port, f"{CLOSE}?sid={sid}")
        assert (status, js(body)) == (200, {"text": FINAL})
        assert factory.streams[0].closed == 1
        assert statuses(rig) == ["ok"] and rig.rows()[0].units == 1       # 8000 bytes = 1 second, rounded up
        assert post(port, f"{CLOSE}?sid={sid}")[0] == 404                 # collected
        assert open_session(port)[0] == 200                               # the slot is free again


# --- the reaper, the opportunistic reap, shutdown --------------------------------------------------------------------------

def _wait(pred, seconds=8.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_the_servers_timer_reaps_an_idle_stream_with_no_request(rig):
    """Red with: `_voice_reap_loop` not started (or `thread.start()` dropped): the row stays reserved."""
    keyed(rig)
    with served(rig) as (port, httpd):
        httpd.voice_stream_kwargs = {"idle_timeout": 0.2}
        httpd.voice_reap_interval = 0.05
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        assert _wait(lambda: statuses(rig) == ["ok"]), statuses(rig)


def test_every_stream_request_reaps_opportunistically(rig):
    """The timer is parked (interval 999 s); an unrelated request ends the idle stream. Red with: the `registry.reap()`
    line in _voice_stream_answer dropped."""
    keyed(rig)
    with served(rig) as (port, httpd):
        httpd.voice_stream_kwargs = {"idle_timeout": 0.2}
        httpd.voice_reap_interval = 999.0
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        time.sleep(0.4)
        assert statuses(rig) == ["reserved"]
        assert post(port, f"{CLOSE}?sid=nobody")[0] == 404
        assert statuses(rig) == ["ok"]


def test_server_close_stops_the_reaper_and_settles_every_stream(rig):
    """No `reserved` row survives the server's close. Red with: `_close_voice_streams` not called from server_close."""
    keyed(rig)
    holder = {}
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        assert statuses(rig) == ["reserved"]
        holder["httpd"], holder["factory"] = httpd, factory
    assert statuses(rig) == ["ok"]
    assert holder["factory"].streams[0].closed == 1
    assert not holder["httpd"].voice_reaper.is_alive()


# --- S3 L3 r1 fixes ------------------------------------------------------------------------------------------------------

def test_no_registry_or_reaper_is_built_after_server_close(rig):
    """codex 1 / complement 1: a first stream request that reaches _voice_stream_registry after server_close must build
    nothing (no registry, no reaper nothing would ever stop). Red with: the `voice_streams_closed` check in
    _voice_stream_registry dropped, or `_close_voice_streams` not setting it."""
    from levain.voice import meter as vmeter

    keyed(rig)
    with served(rig) as (_port, httpd):
        pass                                                           # no stream request: voice_streams stays None
    assert httpd.voice_streams is None
    assert httpd._voice_stream_registry(vmeter.Meter(rig.ledger_path)) is None
    assert httpd.voice_streams is None and httpd.voice_reaper is None


def test_a_reaper_that_cannot_start_publishes_no_registry(rig, monkeypatch):
    """codex 5: the reaper starts before the registry is published, so a failed start leaves nothing a later request
    would open sessions on, and close still runs. Red with: `thread.start()` moved back after the publish."""
    import threading as _threading

    from levain.voice import meter as vmeter

    keyed(rig)
    with served(rig) as (_port, httpd):
        real = _threading.Thread.start

        def boom(self):
            if self.name == "levain-voice-reaper":
                raise RuntimeError("can't start new thread")
            return real(self)

        monkeypatch.setattr(_threading.Thread, "start", boom)
        with pytest.raises(RuntimeError):
            httpd._voice_stream_registry(vmeter.Meter(rig.ledger_path))
        assert httpd.voice_streams is None and httpd.voice_reaper is None
        monkeypatch.setattr(_threading.Thread, "start", real)
        assert httpd._voice_stream_registry(vmeter.Meter(rig.ledger_path)) is httpd.voice_streams is not None


def test_a_meter_rebind_rebuilds_the_registry_over_the_new_ledger(rig, tmp_path):
    """codex 3 / complement 5: the registry is keyed on the meter's path. When the store directory moves, the next stream
    request gets a registry over the NEW ledger (its caps and spend), and the old one is shut (it refuses opens, its
    sessions are settled). Red with: `_voice_stream_registry` returning the held registry without comparing the path."""
    from levain.voice import meter as vmeter

    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        old = httpd.voice_streams
        other = vmeter.Meter(tmp_path / "elsewhere" / "voice.db")
        new = httpd._voice_stream_registry(other)
        assert new is not old and new is httpd.voice_streams and new._meter is other
        assert statuses(rig) == ["ok"]                                # the old ledger's stream was settled
        with pytest.raises(stream_session.StreamRefused, match="stopping"):
            old.open("o", "deepgram", "nova-3", data_root=rig.data_root, read_key=lambda *a, **k: None,
                     factory=lambda s: None)
        assert httpd._voice_stream_registry(other) is new             # same path: the same registry


def test_an_unknown_sid_chunk_is_refused_before_its_body_is_read(rig):
    """codex 4: the sid and its owner are checked before a byte of the chunk body is read, and the connection closes (the
    body is never drained). The head declares 8000 bytes and sends none: an answer must come at once, not after the 10 s
    body deadline. Red with: the `stream_chunk_precheck` call moved after `_read_body`."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid=no-such-sid&seq=0", ctype="application/octet-stream",
                                         clen=str(len(PCM))), wait=3.0)
        assert (got, body["error"]) == (404, "unknown_session") and hdrs["connection"] == "close"
        got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid={sid}&seq=0", ctype="application/octet-stream",
                                         clen=str(len(PCM)), token=MESH_TOKEN), wait=3.0)
        assert (got, body["error"]) == (404, "unknown_session") and hdrs["connection"] == "close"
        got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid={sid}&seq=x", ctype="application/octet-stream",
                                         clen=str(len(PCM))), wait=3.0)
        assert (got, body["error"]) == (409, "bad_sequence") and hdrs["connection"] == "close"
        assert chunk(port, sid, 0)[0] == 200                          # the owner is unharmed


def test_chunk_body_reads_are_bounded_by_their_own_gate(rig):
    """codex 4: concurrent chunk body reads take a slot of voice_chunk_gate without waiting; past it, a 503 before a byte
    is read. Red with: the `voice_chunk_gate.acquire(blocking=False)` in the chunk path dropped."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        held = 0
        while httpd.voice_chunk_gate.acquire(blocking=False):
            held += 1
        try:
            assert held == cockpit_voice.STREAM_CHUNK_INFLIGHT
            got, body, hdrs = raw(port, head(port, f"{CHUNK}?sid={sid}&seq=0", ctype="application/octet-stream",
                                             clen=str(len(PCM))), wait=3.0)
            assert (got, body["error"]) == (503, "voice_busy") and hdrs["connection"] == "close"
        finally:
            for _ in range(held):
                httpd.voice_chunk_gate.release()
        assert chunk(port, sid, 0)[0] == 200


def test_a_chunk_with_no_content_type_is_415(rig):
    """Pins a refuted finding (deepseek 3): base_type(None) is "", so a missing Content-Type is the 415, never a 500."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        sid = open_session(port)[1]["sid"]
        status, _h, body = chunk(port, sid, 0, ctype=None)
        assert (status, js(body)["error"]) == (415, "unsupported_media_type")


def test_voice_json_with_an_unreadable_meter_and_premium_stt_on_is_200(rig):
    """Pins a refuted finding (deepseek 1): `rows` is bound before the meter is read, so the stream flag is computed on an
    unreadable meter too."""
    from tests.test_cockpit_voice_routes import _foreign

    keyed(rig)
    rig.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    _foreign(rig.ledger_path)
    with served(rig) as (port, _h):
        status, _hh, body = call(port, "GET", cockpit_voice.VOICE_JSON)
        out = js(body)
        assert status == 200 and out["meter"] == "unreadable" and out["stream"] is True


# --- the header, the status table ------------------------------------------------------------------------------------------

def test_the_cockpit_page_carries_permissions_policy_microphone_self(rig):
    """Pins the intent. Red with: the `headers=[("Permissions-Policy", ...)]` argument dropped from the cockpit-page send
    in web_server, or the value widened."""
    with served(rig) as (port, _h):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        try:
            c.request("GET", "/")
            r = c.getresponse()
            r.read()
            assert r.status == 200
            assert r.getheader("Permissions-Policy") == "microphone=(self)"
            from levain.http_guards import _CSP
            assert "microphone" not in _CSP and r.getheader("Content-Security-Policy")     # no CSP change
        finally:
            c.close()


def test_status_table_every_stream_refusal_code_has_one_status_and_its_fixed_words():
    """The documented mapping (code -> HTTP status), pinned row by row. Red with: a row changed in STREAM_STATUS, or a code
    added to stream_session.WORDS without a row (it would answer 500)."""
    expected = {
        "bad_sequence": 409, "bad_chunk": 400, "too_fast": 429, "ceiling": 409, "idle_timeout": 409, "closed": 409,
        "unknown_session": 404, "too_many_sessions": 503, "provider_failed": 502, "meter_unavailable": 503,
        "settle_backlog": 503, "month_cap": 402, "day_cap": 402, "unpriced": 503, "stopping": 503, "internal": 500,
    }
    assert cockpit_voice.STREAM_STATUS == expected
    assert set(stream_session.WORDS) - set(expected) == {"key_unavailable"}
    for code, status in expected.items():
        ans = cockpit_voice.stream_refusal(code)
        body = json.loads(ans.body)
        assert (ans.status, body["error"], body["message"]) == (status, code, stream_session.WORDS[code])
    # key_unavailable follows transcribe's key words; an unknown custody code is internal, never its text
    assert cockpit_voice.stream_refusal("key_unavailable", "not_set", provider="deepgram").status == 409
    assert cockpit_voice.stream_refusal("key_unavailable", "keychain_unreadable", provider="deepgram").status == 503
    assert cockpit_voice.stream_refusal("key_unavailable", "weird", provider="deepgram").status == 500
    assert cockpit_voice.stream_refusal("not-a-code").status == 500


# --- a revoke mid-dictation (cockpit mic: the S3 known edge) -----------------------------------------------------------------

class SessionClock:
    """The registry's clock, moved by the test; chunk spacing is session time, never the wall."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def revoke_key(rig, record) -> None:
    from levain.voice import custody as voice_custody
    voice_custody.revoke_key(rig.data_root, record["id"], **rig.ck.kw())


def stream_with_clock(httpd) -> SessionClock:
    clock = SessionClock()
    httpd.voice_stream_kwargs = {"clock": clock}        # read when the registry is first built (the first stream request)
    return clock


def test_a_key_revoked_between_chunk_3_and_4_ends_the_session_at_the_next_check(rig):
    """Chunks 0-3 at 0.25 s each pass; the key is revoked; the first chunk past STREAM_REVOKE_RECHECK_S of session time is
    answered with the open path's no-key words plus `revoked`, its audio never reaches the provider, the provider stream is
    closed once, nothing stays `reserved` and the key value is in no response. Red with: the `session.revoke_check_due`
    block in stream_chunk dropped (chunk 8 is 200 and the stream keeps its 9 chunks), `_stream_revoked`'s key-record
    check returning None, or `session.end_revoked()` dropped (the provider stream is never closed and the row stays
    reserved)."""
    record = rig.add_key(provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3")
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        bodies = []
        for seq in range(4):
            clock.t += 0.25
            status, _h, body = chunk(port, sid, seq)
            assert status == 200, body
            bodies.append(body)
        revoke_key(rig, record)
        clock.t += 0.25
        status, _h, body = chunk(port, sid, 4)             # inside the cadence: not yet looked at
        assert status == 200
        bodies.append(body)
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S
        status, _h, body = chunk(port, sid, 5)             # due: the check finds the key gone
        bodies.append(body)
        got = js(body)
        assert (status, got["error"], got["revoked"], got["fallback"]) == (409, "no_key", True, "on_device"), got
        assert got["message"] == cockpit_voice.WORDS["no_key"].format(p="Deepgram")
        assert len(factory.streams[0].sent) == 5 and factory.streams[0].closed == 1       # chunk 5 was never sent
        assert statuses(rig) == ["ok"]                                  # settled at 5 chunks, nothing `reserved`
        assert rig.rows()[0].units == seconds_for_chunks(5)
        assert chunk(port, sid, 6)[0] == 409                           # the ended session answers `closed`
        no_leak(*(b.decode() for b in bodies))
    assert "reserved" not in statuses(rig)


def seconds_for_chunks(n: int) -> int:
    return stream_session.seconds_for(n * len(PCM))


def test_an_unrevoked_session_streams_on_through_every_check(rig):
    """The checks run (clock past the cadence every chunk) and pass: 12 chunks all 200, the stream sees all 12. Red with:
    `_stream_revoked` returning a refusal for a granted key (e.g. the `"stt" not in record["uses"]` test inverted)."""
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        for seq in range(12):
            clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S + 0.01
            assert chunk(port, sid, seq)[0] == 200
        assert len(factory.streams[0].sent) == 12 and factory.streams[0].closed == 0


def test_the_authority_log_is_read_on_a_cadence_not_per_chunk(rig, monkeypatch):
    """16 chunks 0.25 s apart (4 s of session time) call the check exactly twice. Red with: the cadence test in
    `revoke_check_due` dropped (16 checks) or its interval not restarted after a check (every chunk after the first
    due one checks)."""
    keyed(rig)
    calls = []
    real = cockpit_voice._stream_revoked
    monkeypatch.setattr(cockpit_voice, "_stream_revoked", lambda *a, **k: calls.append(1) or real(*a, **k))
    with served(rig) as (port, httpd):
        fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        for seq in range(16):
            clock.t += 0.25
            assert chunk(port, sid, seq)[0] == 200
    assert len(calls) == 2, calls


def test_a_revoke_check_that_raises_is_retried_on_the_next_chunk_not_a_full_cadence_later(rig, monkeypatch):
    """The first due check raises; the very next chunk (0.25 s on) asks again. Red with: `revoke_check_failed()` dropped
    from stream_chunk's except (the clock stays advanced and the second chunk never checks: 1 call)."""
    keyed(rig)
    calls = []
    real = cockpit_voice._stream_revoked

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(*a, **k)
    monkeypatch.setattr(cockpit_voice, "_stream_revoked", flaky)
    with served(rig) as (port, httpd):
        fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S + 0.01
        try:
            chunk(port, sid, 0)
        except Exception:
            pass
        clock.t += 0.25
        assert chunk(port, sid, 0)[0] == 200
    assert len(calls) == 2, calls


def test_a_revoke_path_that_goes_away_mid_session_ends_it_in_the_open_paths_words(rig, monkeypatch):
    """The recheck is `revoke_refusal` too: when it starts refusing, the session ends with its words and `revoked`. Red
    with: the `revoke_refusal` call dropped from `_stream_revoked` (chunk 200)."""
    keyed(rig)
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        clock.t += 0.25
        assert chunk(port, sid, 0)[0] == 200
        monkeypatch.setattr(cockpit_voice, "revoke_refusal", lambda q, root: "revoke_unreachable")
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S
        status, _h, body = chunk(port, sid, 1)
        got = js(body)
        assert (status, got["error"], got["revoked"]) == (503, "revoke_unreachable", True)
        assert got["message"] == cockpit_voice.WORDS["revoke_unreachable"]
        assert factory.streams[0].closed == 1 and len(factory.streams[0].sent) == 1
        assert statuses(rig) == ["ok"]


def test_a_log_that_cannot_be_replayed_mid_session_ends_it_closed(rig, monkeypatch):
    """As at open, an unknown is a refusal (authority_unreadable, 503, `revoked`). Red with: the `except Exception`
    branch of `_stream_revoked` answering None."""
    keyed(rig)
    with served(rig) as (port, httpd):
        fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S + 0.01

        def boom(_root):
            raise OSError("log gone")
        monkeypatch.setattr(cockpit_voice.custody, "authority_for", boom)
        status, _h, body = chunk(port, sid, 0)
        assert (status, js(body)["error"], js(body)["revoked"]) == (503, "authority_unreadable", True)


def test_one_revoke_check_costs_little_enough_for_the_cadence(rig):
    """MEASURED: one `_stream_revoked` (two fresh log replays) on the rig's signed log. The cadence is 2 s of session
    time; a check must be a small fraction of that or a chunk holds its connection. The bound is 0.5 s (the measured
    cost is in RETURN_micrevoke.md). Red with: a check slower than that."""
    record = rig.add_key(provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3")
    bound = (record["id"], frozenset(record["uses"]))          # the session's key_record (L3 4c r1 F1)
    cockpit_voice._stream_revoked("deepgram", rig.data_root, rig.qdir, bound=bound)         # warm
    t0 = time.perf_counter()
    for _ in range(5):
        assert cockpit_voice._stream_revoked("deepgram", rig.data_root, rig.qdir, bound=bound) is None
    per = (time.perf_counter() - t0) / 5
    print(f"revoke check: {per * 1000:.1f} ms")
    assert per < 0.5 and cockpit_voice.STREAM_REVOKE_RECHECK_S + 0.25 < 3.0


# --- L3 batch4c r1 fixes: the exact key binding (codex F1) and a close that did not finish (codex F2) ----------------------

def replace_key(rig, provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3-new") -> dict:
    from levain.chat_egress.key_setup import KeychainItems
    from levain.chat_egress.keys import KeychainStore
    from levain.voice import custody as voice_custody
    record, _warning = voice_custody.add_key(
        rig.data_root, provider=provider, uses=uses, account="levain-voice", value=voice_custody.Secret(VALUE),
        source=source, confirm=lambda preview: True, writer=voice_custody.SecurityStdinWriter(rig.sec.path),
        items=KeychainItems(rig.sec.path), store=KeychainStore(rig.sec.path), replace=True, **rig.ck.kw())
    return record


def test_a_key_replaced_mid_dictation_ends_the_stream_opened_with_the_old_one(rig):
    """The stream opened with record A; A is replaced by B (one `loosen`: A gone, B live for the same provider and
    uses). The next due check must end the session: the provider socket is still authenticated with A's revoked key.
    Red with: `_stream_revoked` looking up the provider's live record (`voice_key_for`) instead of the record id bound
    at open (chunk 200, the stream keeps going)."""
    old = rig.add_key(provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3")
    with served(rig) as (port, httpd):
        factory = fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        clock.t += 0.25
        assert chunk(port, sid, 0)[0] == 200
        new = replace_key(rig)
        assert new["id"] != old["id"]
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S
        status, _h, body = chunk(port, sid, 1)
        got = js(body)
        assert (status, got.get("error"), got.get("revoked")) == (409, "no_key", True), got
        assert len(factory.streams[0].sent) == 1 and factory.streams[0].closed == 1
        no_leak(body.decode())
    assert "reserved" not in statuses(rig)


def test_a_key_whose_uses_changed_mid_dictation_ends_the_stream(rig):
    """Same record id is the binding AND its uses set: a replace that keeps `stt` but changes the set still ends it
    (a new record id). Red with: the bound uses not compared."""
    rig.add_key(provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3")
    with served(rig) as (port, httpd):
        fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        replace_key(rig, uses=("stt",))
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S + 0.01
        status, _h, body = chunk(port, sid, 0)
        assert (status, js(body).get("revoked")) == (409, True), body


class CloseFailsStream(FakeStream):
    """A provider whose close returns words but did not finish: the final commit timed out / no closing metadata, so the
    adapter's `poll().failed` is true after close (as both real adapters report it)."""

    def __init__(self, secret_value: str) -> None:
        super().__init__(secret_value)
        self.failed_after_close = True

    def poll(self) -> stream_session.StreamUpdate:
        up = super().poll()
        return stream_session.StreamUpdate(partial=up.partial, final=up.final,
                                           failed=bool(self.closed) and self.failed_after_close)


def test_a_close_the_provider_did_not_finish_is_a_non_2xx_so_the_page_recovers(rig):
    """Red with: `_end` ignoring the adapter's post-close failure (close answers 200 with the partial text, the page
    skips its WAV recovery). The meter is settled all the same."""
    keyed(rig)
    with served(rig) as (port, httpd):
        streams = []

        def factory(secret):
            s = CloseFailsStream(secret.reveal())
            streams.append(s)
            return s
        httpd.voice_stream_factory = factory
        sid = open_session(port)[1]["sid"]
        assert chunk(port, sid, 0)[0] == 200
        status, _h, body = post(port, f"{CLOSE}?sid={sid}")
        assert (status, js(body)["error"]) == (502, "provider_failed"), body
        assert "text" not in js(body)
        assert streams[0].closed == 1
        assert statuses(rig) == ["ok"] and rig.rows()[0].units == 1
        assert post(port, f"{CLOSE}?sid={sid}")[0] == 404                 # collected all the same


# --- AUTO premium_stt: the switch gates OPEN only (levain 72 sttfix R1/R2) ------------------------------------------------

def test_auto_a_key_revoked_mid_stream_still_answers_revoked_and_close_settles(rig):
    """The AUTO decision gates stream OPEN only. A session exists only because open passed; when its only stt key is revoked
    mid-dictation the next due chunk answers the revoked refusal (`revoked: true`, the page stops for good), the provider
    stream is closed and the meter settled, and close still returns the collected text. Red with: `premium_stt_on` called
    again for chunk or close in `_voice_stream_answer` (the chunk and close answer a bare 404 `premium_stt_off`, no
    `revoked`, nothing ended or settled)."""
    record = rig.add_key(provider="deepgram", uses=("stt", "tts"), source="levain-voice-deepgram-s3")
    with served(rig, premium_stt=None) as (port, httpd):
        factory = fx(httpd)
        clock = stream_with_clock(httpd)
        sid = open_session(port)[1]["sid"]
        for seq in range(2):
            clock.t += 0.25
            assert chunk(port, sid, seq)[0] == 200
        revoke_key(rig, record)
        clock.t += cockpit_voice.STREAM_REVOKE_RECHECK_S
        status, _h, body = chunk(port, sid, 2)
        got = js(body)
        assert (status, got["error"], got["revoked"]) == (409, "no_key", True), got
        assert factory.streams[0].closed == 1 and statuses(rig) == ["ok"]
        status, _h, body = post(port, f"{CLOSE}?sid={sid}")
        assert (status, js(body)["text"]) == (200, FINAL), (status, body)
    assert "reserved" not in statuses(rig)


def test_auto_open_with_the_authority_log_unreadable_says_so_not_that_a_key_is_needed(rig, monkeypatch):
    """AUTO with a log that cannot be replayed refuses open (still closed) in the existing `authority_unreadable` words
    and 503, never "needs a speech-to-text key". Red with: the gate answering `premium_stt_off` for an unreadable log."""
    keyed(rig)
    with served(rig, premium_stt=None) as (port, httpd):
        factory = fx(httpd)

        def boom(_root):
            raise OSError("log gone")
        monkeypatch.setattr(cockpit_voice.custody, "authority_for", boom)
        status, body = open_session(port)
        assert (status, body["error"], body["message"]) == (
            503, "authority_unreadable", cockpit_voice.WORDS["authority_unreadable"]), body
        assert factory.streams == []
    assert rig.rows() == []
