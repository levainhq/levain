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
    """Red with: the `if premium_stt and stream_offered(rows)` line in serve_voice dropped or inverted (the key then shows
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


def test_voice_json_flag_absent_when_the_first_keyed_stt_provider_has_no_stream(rig):
    """A keyed OpenAI row comes before deepgram in the offered order, and the page sends the FIRST keyed stt row, so the
    flag stays absent rather than send the page to a provider with no stream. Red with: stream_offered returning on any
    row (not the first) or ignoring STREAM_PROVIDERS."""
    keyed(rig, "openai")
    keyed(rig)
    with served(rig) as (port, _h):
        rows = js(call(port, "GET", cockpit_voice.VOICE_JSON)[2])
        names = [r["name"] for r in rows["providers"] if r["key"] == "set" and "stt" in r["uses"]]
        assert names[0] != "deepgram" and "deepgram" in names
        assert "stream" not in rows


# --- premium_stt off -----------------------------------------------------------------------------------------------------

def test_off_is_404_on_all_three_before_any_read_even_with_a_key(rig):
    """Nothing is reserved, read or built. Red with: the `if not srv.premium_stt` refusal in _voice_stream_answer dropped
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


def test_open_refuses_a_provider_with_no_stream_a_bad_query_and_a_body(rig):
    """Red with: the STREAM_PROVIDERS test dropped (openai opens a deepgram stream), `_query_provider` skipped, or the
    zero-length check on open dropped."""
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
