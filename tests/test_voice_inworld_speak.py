"""The Inworld speech adapter against an in-process fake Inworld: a real HTTP server on the loopback interface.

Same rig as ``tests/test_voice_deepgram_speak.py``: the adapter's request is sent over a real socket to a fake server that
records the bytes it received, so what is asserted is the request as it left the process (method, path, every header, the
JSON body) and every error status Inworld's OpenAI-compatibility page documents (https://docs.inworld.ai/tts/openai-compatibility.md,
read 2026-11-05) is answered with the body shape that page shows. No test here reaches a real host and none holds a real
key: a socket connect to anything but 127.0.0.1 is refused for the whole module, and ``FAKE_KEY`` is a placeholder.

``loopback`` stands in for :func:`levain.voice.providers.urllib_transport`, which is https-only to the provider hosts (its
own tests are in ``test_voice_providers.py``). It hands back every body it is given, error bodies included: the adapter must
drop them, so a leak is visible here and not hidden by the real transport's 429-only reading.

Each test names, in its docstring, the line whose revert turns it red (the lane's RETURN records each run)."""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

import pytest

from levain.voice import meter as voice_meter
from levain.voice import providers as P
from levain.voice.text import CHUNK

FAKE_KEY = "FAKE-INWORLD-KEY-NOT-A-CREDENTIAL-0003"
MARKER = "SECRET-BODY-MARKER"
MP3 = bytes.fromhex("494433030000000000fffb9000fffb9000fffb9000fffb9000") + b"\xff\xfe\x80"   # not valid UTF-8


@pytest.fixture(autouse=True)
def _loopback_only(monkeypatch):
    real = socket.socket.connect

    def guarded(self, address, *a, **k):
        if not (isinstance(address, tuple) and address[0] == "127.0.0.1"):
            raise AssertionError("a voice test tried to open a socket to something other than the loopback")
        return real(self, address, *a, **k)
    monkeypatch.setattr(socket.socket, "connect", guarded)


class FakeInworld:
    """One loopback HTTP server. ``answer`` is the (status, headers, body) the next request gets; ``received`` holds the
    raw request line, headers and body of every request, as the server read them off the wire."""

    def __init__(self) -> None:
        self.answer: tuple[int, dict[str, str], bytes] = (200, {"Content-Type": "audio/mpeg"}, MP3)
        self.received: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.received.append({"method": self.command, "target": self.path,
                                       "headers": {k: v for k, v in self.headers.items()},
                                       "raw_headers": str(self.headers), "body": self.rfile.read(length)})
                status, headers, body = outer.answer
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:       # the server's own access log is silent
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def server():
    fake = FakeInworld()
    yield fake
    fake.close()


class Loopback:
    """A transport that sends the adapter's request to the fake server over a real socket. Keeps the request object it
    was handed, so a test can look at it after the adapter is done with it."""

    def __init__(self, fake: FakeInworld) -> None:
        self.fake = fake
        self.requests: list[P.HttpRequest] = []

    def __call__(self, req: P.HttpRequest) -> P.HttpResponse:
        self.requests.append(req)
        parts = urlsplit(req.url)
        assert parts.scheme == "https" and parts.hostname == "api.inworld.ai"        # what the real transport allows
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
        try:
            conn.request(req.method, parts.path + (f"?{parts.query}" if parts.query else ""), body=req.body,
                         headers=dict(req.headers))
            resp = conn.getresponse()
            return P.HttpResponse(resp.status, resp.read())
        finally:
            conn.close()


def speaker(fake: FakeInworld) -> tuple[P.InworldProvider, Loopback]:
    transport = Loopback(fake)
    return P.InworldProvider(transport=transport), transport


def surfaces(exc: BaseException) -> str:
    return " | ".join([str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))])


# ---- the request as it left the process ----------------------------------------------------------------------------

def test_the_request_on_the_wire_is_the_documented_openai_shaped_speech_request(server):
    """Red with: the path, the model, the response_format or the Authorization scheme changed in InworldProvider.speak."""
    prov, _ = speaker(server)
    audio = prov.speak(FAKE_KEY, "Hello there.")
    assert audio == MP3                                                  # binary bytes come back untouched
    (got,) = server.received
    assert got["method"] == "POST" and got["target"] == "/v1/audio/speech"       # no query: nothing rides in the URL
    assert got["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert got["headers"]["Content-Type"] == "application/json" and got["headers"]["Accept"] == "audio/mpeg"
    assert json.loads(got["body"]) == {"model": "inworld-tts-2-flash", "input": "Hello there.", "voice": "Dennis",
                                       "speed": 1.0, "response_format": "mp3"}


def test_the_model_is_the_flash_tier_the_meter_prices_and_never_an_openai_id(server):
    """Red with: INWORLD_TTS_MODEL changed (the documented 400 for an OpenAI model id would follow)."""
    assert P.INWORLD_TTS_MODEL == "inworld-tts-2-flash"
    assert voice_meter.DEFAULT_PRICES.find("inworld", "tts", P.InworldProvider().tts_model) is not None
    assert not P.InworldProvider().tts_model.startswith("tts-")


def test_the_key_is_in_the_authorization_header_and_nowhere_else_on_the_wire(server):
    """Red with: the key put in the URL, the body or a second header."""
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hello there.")
    r = server.received[0]
    assert r["raw_headers"].count(FAKE_KEY) == 1                          # one header line carries it
    assert FAKE_KEY not in r["target"] and FAKE_KEY.encode() not in r["body"]
    others = {k: v for k, v in r["headers"].items() if k != "Authorization"}
    assert FAKE_KEY not in json.dumps(others)


@pytest.mark.parametrize("voice", ["Dennis", "Ashley", "Alex", "my_workspace__my_custom_voice"])
def test_the_chosen_voice_is_sent_as_given_case_kept(server, voice):
    """Red with: the voice lowercased or replaced by the default (Inworld voice ids are case-sensitive)."""
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", voice)
    assert json.loads(server.received[0]["body"])["voice"] == voice


@pytest.mark.parametrize("voice", ["", "a b", "Den/nis", "x" * 65, "Dennis\n", None])
def test_a_malformed_voice_is_refused_before_sending(server, voice):
    """Red with: _need_voice dropped from InworldProvider.speak."""
    prov, _ = speaker(server)
    with pytest.raises(ValueError):
        prov.speak(FAKE_KEY, "Hi.", voice)
    assert server.received == []


def test_the_offered_voices_are_the_ids_a_docs_page_prints_sorted_and_hold_the_default():
    """Red with: a voice id added that no page prints (the list is behind an authenticated call), or the default moved
    off the tuple. The ids are those of the list-voices reference and the quickstart, read 2026-11-05."""
    assert P.INWORLD_TTS_VOICES == ("Alex", "Ashley", "Dennis")
    assert P.INWORLD_TTS_VOICE in P.INWORLD_TTS_VOICES == tuple(sorted(P.INWORLD_TTS_VOICES))


@pytest.mark.parametrize("speed,wire", [(0.5, 0.5), (1.0, 1.0), (1.5, 1.5), (1, 1), (0.75, 0.75)])
def test_a_speed_in_inworlds_range_is_sent(server, speed, wire):
    """Red with: the speed key dropped from the body."""
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", speed=speed)
    assert json.loads(server.received[0]["body"])["speed"] == wire


@pytest.mark.parametrize("speed", [0.49, 0.25, 1.51, 2.0, 4.0, 0, -1, float("nan"), float("inf"), True, "1.2", None])
def test_a_speed_outside_inworlds_range_is_refused_before_sending_not_dropped(server, speed):
    """Red with: the adapter checking OpenAI's 0.25-4.0 range instead of 0.5-1.5."""
    prov, _ = speaker(server)
    with pytest.raises(ValueError) as info:       # a pre-send ValueError: the route releases the reservation
        prov.speak(FAKE_KEY, "Hi.", speed=speed)
    assert not isinstance(info.value, P.VoiceError) and FAKE_KEY not in surfaces(info.value)
    assert server.received == []


def test_the_authorization_header_is_removed_from_the_request_once_the_transport_returns(server):
    """Red with: _exchange not removing the header (shared code, asserted here for this adapter's request)."""
    prov, transport = speaker(server)
    prov.speak(FAKE_KEY, "Hi.")
    (req,) = transport.requests
    assert "Authorization" not in req.headers and FAKE_KEY not in repr(req) and FAKE_KEY not in req.url


# ---- the host, the input cap ----------------------------------------------------------------------------------------

def test_the_host_list_gains_api_inworld_ai_and_nothing_else():
    """Red with: a second host added, or the inworld host spelled differently."""
    assert P.HOSTS["inworld"] == "api.inworld.ai"
    assert P.ALLOWED_HOSTS == {"api.openai.com", "api.deepgram.com", "api.inworld.ai"}


def test_the_real_transport_refuses_a_lookalike_inworld_destination_before_opening(monkeypatch):
    """Red with: urllib_transport matching the host by suffix or prefix."""
    class Boom:
        def open(self, *a, **k):
            raise AssertionError("the transport opened a connection")
    monkeypatch.setattr(P, "_OPENER", Boom())
    for url in ("http://api.inworld.ai/v1/audio/speech", "https://api.inworld.ai.evil.example/x",
                "https://user:pw@api.inworld.ai/x", "https://api.eu.inworld.ai/v1/audio/speech"):
        with pytest.raises(ValueError):
            P.urllib_transport(P.HttpRequest("POST", url, {}, b""))


def test_the_input_cap_is_inworlds_4000_characters_and_inclusive(server):
    """Red with: INWORLD_INPUT_CAP changed, or the speech check using another adapter's cap."""
    assert P.INWORLD_INPUT_CAP == 4000
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "x" * 4000)
    assert len(json.loads(server.received[0]["body"])["input"]) == 4000
    server.received.clear()
    with pytest.raises(ValueError) as info:
        prov.speak(FAKE_KEY, "x" * 4001)
    assert type(info.value) is ValueError and FAKE_KEY not in surfaces(info.value)
    assert server.received == []                                         # refused before anything was sent


def test_the_chunker_stays_under_the_cap():
    """Red with: the cap lowered under the chunker's size."""
    assert CHUNK <= P.INWORLD_INPUT_CAP


@pytest.mark.parametrize("text", ["", "   ", None, b"bytes"])
def test_nothing_to_speak_is_refused_before_sending(server, text):
    """Red with: _need_speech dropped from InworldProvider.speak."""
    prov, _ = speaker(server)
    with pytest.raises(ValueError):
        prov.speak(FAKE_KEY, text)
    assert server.received == []


# ---- every documented error status, in the body shape the page shows ------------------------------------------------

def _openai_error(kind: str, message: str, param: str | None = None, code: str | None = None) -> bytes:
    return json.dumps({"error": {"message": f"{message} {MARKER} {FAKE_KEY}", "type": kind, "param": param,
                                 "code": code}}).encode()


# (status, body, the WORDS code), built from the Errors table of https://docs.inworld.ai/tts/openai-compatibility.md and
# the example bodies of its reference page. The pages document no spend-limit 429: the OpenAI-format billing codes the
# shared 429 reader knows are an empty balance or a spend limit, and everything else, plain text included, a rate limit.
ERRORS = [
    (400, _openai_error("invalid_request_error", "speed must be between 0.5 and 1.5", "speed"), "rejected"),
    (400, _openai_error("invalid_request_error", "model not found", "model"), "rejected"),
    (401, _openai_error("authentication_error", "authentication is required"), "refused"),
    (402, _openai_error("insufficient_quota", "no credits remain"), "out_of_credit"),
    (402, _openai_error("invalid_request_error", "string", "string", "string"), "out_of_credit"),
    (403, _openai_error("permission_error", "invalid credentials"), "refused"),
    (404, _openai_error("invalid_request_error", "Unknown voice: alloy not found!"), "rejected"),
    (429, _openai_error("rate_limit_error", "rate limit exceeded"), "rate_limited"),
    (429, b"Rate limit exceeded " + FAKE_KEY.encode(), "rate_limited"),
    (429, _openai_error("insufficient_quota", "out of credit", code="insufficient_quota"), "out_of_credit"),
    (429, _openai_error("invalid_request_error", "spend limit", code="organization_spend_limit_exceeded"),
     "spend_limited"),
    (500, _openai_error("api_error", "boom"), "unavailable"),
    (503, _openai_error("api_error", "busy"), "unavailable"),
]


@pytest.mark.parametrize("status,body,code", ERRORS, ids=[f"{s}-{c}-{i}" for i, (s, _, c) in enumerate(ERRORS)])
def test_each_documented_error_is_a_fixed_code_and_no_body_or_key_is_surfaced(server, status, body, code):
    """Red with: _code_for changed for these statuses, or a provider body surfaced in the VoiceError."""
    server.answer = (status, {"Content-Type": "application/json"}, body)
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        # the loopback hands the 429 body back whole, as urllib_transport does for a 429 (and drops it for the rest)
        prov.speak(FAKE_KEY, "Hello there.")
    exc = info.value
    assert exc.code == code and exc.provider == "inworld"
    assert str(exc) == P.WORDS[code].format(p="Inworld")                  # the fixed sentence and nothing else
    text = surfaces(exc)
    assert FAKE_KEY not in text and MARKER not in text
    assert exc.__cause__ is None and exc.__context__ is None


def test_the_provider_is_named_inworld_in_its_fixed_words():
    """Red with: NAMES lacking the inworld entry (the words would read 'The voice provider')."""
    assert P.NAMES["inworld"] == "Inworld"
    assert str(P.VoiceError("out_of_credit", "inworld")) == "Your Inworld account is out of credit."


def test_a_200_with_no_audio_is_unavailable(server):
    """Red with: the empty-audio check dropped from InworldProvider.speak."""
    server.answer = (200, {"Content-Type": "audio/mpeg"}, b"")
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"


def test_a_redirect_is_unavailable_and_the_key_goes_nowhere_else(server):
    """Red with: _code_for treating a 3xx as success."""
    server.answer = (302, {"Location": "http://127.0.0.1:1/elsewhere"}, b"")
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable" and len(server.received) == 1


# ---- the key is never logged, printed or echoed ---------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 401, 402, 429, 503])
def test_nothing_is_logged_or_printed_with_the_key_in_it(server, caplog, capsys, status):
    """Red with: the adapter logging or printing a request, a body or the key."""
    body = MP3 if status == 200 else _openai_error("authentication_error", "authentication is required")
    server.answer = (status, {"Content-Type": "application/json"}, body)
    prov, _ = speaker(server)
    with caplog.at_level(logging.DEBUG):
        try:
            prov.speak(FAKE_KEY, "Hello there.")
        except P.VoiceError:
            pass
    seen = "\n".join(f"{r.getMessage()} {r.args!r} {r.exc_text}" for r in caplog.records)
    out = capsys.readouterr()
    assert FAKE_KEY not in seen and MARKER not in seen
    assert FAKE_KEY not in out.out + out.err and MARKER not in out.out + out.err


def test_the_adapter_and_its_request_name_no_key_in_a_repr_or_attribute(server):
    """Red with: the key stored on the adapter, or put into a request's repr."""
    prov, transport = speaker(server)
    prov.speak(FAKE_KEY, "Hello there.")
    assert FAKE_KEY not in repr(prov) and FAKE_KEY not in repr(vars(prov))
    assert all(FAKE_KEY not in repr(r) for r in transport.requests)


def test_an_unsendable_key_is_refused_and_nothing_reaches_the_server(server):
    """Red with: the _Key check dropped (a control character would split a header)."""
    prov, _ = speaker(server)
    for key in ("", "has space", "bad\r\nX-Injected: 1", "“curly”"):
        with pytest.raises(P.VoiceError) as info:
            prov.speak(key, "Hello there.")
        assert info.value.code == "refused"
    assert server.received == []


# ---- the factory, and what the adapter is not -----------------------------------------------------------------------

def test_the_factory_resolves_inworld_and_the_adapter_is_speech_only():
    """Red with: provider_for lacking the branch, or the adapter claiming dictation."""
    prov = P.provider_for("inworld")
    assert isinstance(prov, P.InworldProvider) and prov.uses == frozenset({"tts"})
    assert not hasattr(prov, "transcribe") and not hasattr(prov, "stt_model")
    assert set(P.NAMES) == set(P.HOSTS)


def test_inworld_counts_its_input_in_utf16_code_units_so_an_emoji_is_two(server):
    """L3 inworld r1 codex HIGH: Inworld counts its input limit (and bills) in UTF-16 code units, so 2,001 emoji are under 4,000
    Python characters and over 4,000 units. Red with: the cap or the unit count using len(text)."""
    assert P.inworld_units("a\U0001F600") == 3 and P.inworld_units("x" * 5) == 5 and P.inworld_units("\ud800") == 1
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "\U0001F600" * 2000)                              # exactly 4,000 units: sent
    assert len(server.received) == 1
    server.received.clear()
    with pytest.raises(ValueError):
        prov.speak(FAKE_KEY, "\U0001F600" * 2001)                          # 4,002 units though only 2,001 characters
    assert server.received == []
