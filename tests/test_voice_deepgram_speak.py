"""The Deepgram Aura-2 speech adapter against an in-process fake Deepgram: a real HTTP server on the loopback interface.

``tests/test_voice_providers.py`` replays recorded responses at the transport seam. This file goes one layer down: the
adapter's request is sent over a real socket to a fake server that records the bytes it received, so what is asserted is
the request as it left the process (method, path and query, every header, the JSON body) and every error status the
Deepgram pages document (https://developers.deepgram.com/docs/errors, read 2026-11-05) is answered with the body shape
the page shows. No test here reaches a real host and none holds a real key: a socket connect to anything but 127.0.0.1
is refused for the whole module, and ``FAKE_KEY`` is an obvious placeholder.

``loopback`` is a stand-in for :func:`levain.voice.providers.urllib_transport`, which is https-only to the two provider
hosts (that transport has its own tests in ``test_voice_providers.py``). The stand-in sends exactly the method, URL path
and query, headers and body the adapter built, and hands back every body it is given, error bodies included: the adapter
must drop them, so a leak is visible here and not hidden by the real transport's own 429-only reading."""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
from pathlib import Path
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from levain.voice import providers as P

FAKE_KEY = "FAKE-KEY-NOT-A-CREDENTIAL-0002"
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


class FakeDeepgram:
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

    def wire_text(self, i: int = 0) -> str:
        """Everything of request ``i`` that is text: the target, every header line and the body."""
        r = self.received[i]
        return "\n".join([r["method"], r["target"], r["raw_headers"], r["body"].decode("utf-8", "replace")])


@pytest.fixture
def server():
    fake = FakeDeepgram()
    yield fake
    fake.close()


class Loopback:
    """A transport that sends the adapter's request to the fake server over a real socket. Keeps the request object it
    was handed, so a test can look at it after the adapter is done with it."""

    def __init__(self, fake: FakeDeepgram) -> None:
        self.fake = fake
        self.requests: list[P.HttpRequest] = []

    def __call__(self, req: P.HttpRequest) -> P.HttpResponse:
        self.requests.append(req)
        parts = urlsplit(req.url)
        assert parts.scheme == "https" and parts.hostname == "api.deepgram.com"      # what the real transport allows
        conn = http.client.HTTPConnection("127.0.0.1", self.fake.port, timeout=10)
        try:
            conn.request(req.method, f"{parts.path}?{parts.query}", body=req.body, headers=dict(req.headers))
            resp = conn.getresponse()
            return P.HttpResponse(resp.status, resp.read())
        finally:
            conn.close()


def speaker(fake: FakeDeepgram) -> tuple[P.DeepgramProvider, Loopback]:
    transport = Loopback(fake)
    return P.DeepgramProvider(transport=transport), transport


def surfaces(exc: BaseException) -> str:
    return " | ".join([str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))])


# ---- the request as it left the process ----------------------------------------------------------------------------

def test_the_request_on_the_wire_is_the_documented_speak_request(server):
    prov, _ = speaker(server)
    audio = prov.speak(FAKE_KEY, "Hello there.")
    assert audio == MP3                                                  # binary bytes come back untouched
    (got,) = server.received
    parts = urlsplit(got["target"])
    assert got["method"] == "POST" and parts.path == "/v1/speak"
    assert parse_qs(parts.query, keep_blank_values=True) == {
        "model": [P.DEEPGRAM_TTS_VOICE], "encoding": ["mp3"], "mip_opt_out": ["true"]}
    assert got["headers"]["Authorization"] == f"Token {FAKE_KEY}"
    assert got["headers"]["Content-Type"] == "application/json" and got["headers"]["Accept"] == "audio/mpeg"
    assert json.loads(got["body"]) == {"text": "Hello there."}           # exactly one of text or url: text only


def test_the_key_is_in_the_authorization_header_and_nowhere_else_on_the_wire(server):
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hello there.")
    r = server.received[0]
    assert r["raw_headers"].count(FAKE_KEY) == 1                          # one header line carries it
    assert FAKE_KEY not in r["target"] and FAKE_KEY.encode() not in r["body"]
    others = {k: v for k, v in r["headers"].items() if k != "Authorization"}
    assert FAKE_KEY not in json.dumps(others)


@pytest.mark.parametrize("voice", [P.DEEPGRAM_TTS_VOICE, "aura-2-andromeda-en", "aura-2-zeus-en"])
def test_the_chosen_voice_is_the_model_parameter(server, voice):
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", voice)
    assert parse_qs(urlsplit(server.received[0]["target"]).query)["model"] == [voice]


def test_the_offered_voices_are_english_aura_2_ids_sorted_without_duplicates_and_hold_the_default():
    voices = P.DEEPGRAM_TTS_VOICES
    assert all(re.fullmatch(r"aura-2-[a-z]+-en", v) for v in voices)
    assert len(set(voices)) == len(voices) and list(voices) == sorted(voices)
    assert P.DEEPGRAM_TTS_VOICE in voices
    # the count of English Aura-2 rows in the "All Available Voices" table of https://developers.deepgram.com/docs/tts-models,
    # read 2026-11-05; a number that moves when the page does is a reason to re-read the page, not to edit this line blind
    assert len(voices) == 41


def test_a_second_listed_voice_is_sent_as_the_model_and_a_voice_of_another_language_or_family_is_refused_before_any_network_use(server):
    prov, transport = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", "aura-2-zeus-en")
    assert parse_qs(urlsplit(server.received[0]["target"]).query)["model"] == ["aura-2-zeus-en"]
    # L3 r1 (voices): the wire's check is the family and the English shape, not a frozen list, so a voice Deepgram adds
    # later is sendable once the cockpit offers it; it refuses what is another language's, another family's or malformed
    for bad in ("aura-2-sirio-es", "aura-1-asteria-en", "aura-2-", "nova", "aura-2-ZEUS-en", "aura-2-zeus-en ", " aura-2-zeus-en",
                "aura-2-zeus-en\n", "aura-2-zeus-en&speed=2"):
        with pytest.raises(ValueError):
            prov.speak(FAKE_KEY, "Hi.", bad)
    assert len(server.received) == 1 and len(transport.requests) == 1       # only the English Aura-2 voice ever left the process


def test_a_new_english_voice_of_the_family_is_sendable_and_the_family_follows_the_provider_s_model(server):
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", "aura-2-newvoice-en")                      # not offered by the cockpit yet, still a valid wire id
    assert parse_qs(urlsplit(server.received[0]["target"]).query)["model"] == ["aura-2-newvoice-en"]
    other = P.DeepgramProvider(tts_model="aura-3", transport=prov.transport)
    with pytest.raises(ValueError):                                         # an aura-2 id on an aura-3 provider is the wrong family
        other.speak(FAKE_KEY, "Hi.", "aura-2-zeus-en")
    assert len(server.received) == 1


@pytest.mark.parametrize("speed,wire", [(0.7, "0.7"), (1.5, "1.5"), (0.85, "0.85"), (1, None), (1.0, None),
                                        (1.0000001, "1.0000001"), (1.23456789, "1.23456789")])    # L3 r2 codex: a six-digit format sent the first as "1"
def test_speed_is_sent_on_the_wire_unless_it_is_the_default(server, speed, wire):
    # Aura-2 speed runs 0.7 - 1.5 (https://developers.deepgram.com/docs/tts-voice-controls); 1.0 is Deepgram's default
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", speed=speed)
    query = parse_qs(urlsplit(server.received[0]["target"]).query)
    assert query.get("speed") == ([wire] if wire else None)
    assert query["model"] == [P.DEEPGRAM_TTS_VOICE] and query["encoding"] == ["mp3"] and query["mip_opt_out"] == ["true"]
    assert b"speed" not in server.received[0]["body"]


@pytest.mark.parametrize("speed", [0.69, 0.5, 0.25, 1.51, 2.0, 4.0, 0, -1, float("nan"), float("inf"), True, "1.2", None])
def test_a_speed_outside_aura_2s_range_is_refused_before_sending_not_dropped(server, speed):
    prov, _ = speaker(server)
    with pytest.raises(ValueError) as info:       # a pre-send ValueError: the route releases the reservation
        prov.speak(FAKE_KEY, "Hi.", speed=speed)
    assert not isinstance(info.value, P.VoiceError) and FAKE_KEY not in surfaces(info.value)
    assert server.received == []


def test_speed_is_checked_against_deepgrams_range_not_openais():
    assert (P.DEEPGRAM_SPEED_MIN, P.DEEPGRAM_SPEED_MAX) == (0.7, 1.5)
    assert (P.SPEED_MIN, P.SPEED_MAX) == (0.25, 4.0)


def test_the_authorization_header_is_removed_from_the_request_once_the_transport_returns(server):
    prov, transport = speaker(server)
    prov.speak(FAKE_KEY, "Hi.")
    (req,) = transport.requests
    assert "Authorization" not in req.headers and FAKE_KEY not in repr(req) and FAKE_KEY not in req.url


# ---- the input cap --------------------------------------------------------------------------------------------------

def test_the_input_cap_is_deepgrams_2000_characters_and_inclusive(server):
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "x" * P.DEEPGRAM_INPUT_CAP)
    assert len(json.loads(server.received[0]["body"])["text"]) == 2000
    server.received.clear()
    with pytest.raises(ValueError) as info:
        prov.speak(FAKE_KEY, "x" * (P.DEEPGRAM_INPUT_CAP + 1))
    assert server.received == [] and FAKE_KEY not in surfaces(info.value)     # refused before anything was sent


def test_the_cap_is_deepgrams_not_the_other_adapters(server):
    assert P.DEEPGRAM_INPUT_CAP == 2000 and P.DEEPGRAM_INPUT_CAP < P.OPENAI_INPUT_CAP
    prov, _ = speaker(server)
    with pytest.raises(ValueError):
        prov.speak(FAKE_KEY, "x" * (P.OPENAI_INPUT_CAP - 1))
    assert server.received == []


def test_the_chunker_stays_under_the_cap():
    from levain.voice.text import CHUNK
    assert CHUNK <= P.DEEPGRAM_INPUT_CAP


def test_the_chunk_check_is_a_raise_not_an_assert_so_it_survives_python_dash_O():
    # the module is re-imported under ``-O`` (asserts stripped) with CHUNK pushed over the cap: the import must still fail
    import subprocess
    import sys
    import textwrap
    root = Path(P.__file__).resolve().parents[2]
    code = textwrap.dedent(f"""
        import importlib, sys
        sys.path.insert(0, {str(root)!r})
        import levain.voice.text as t
        t.CHUNK = 3000
        sys.modules.pop("levain.voice.providers", None)
        try:
            importlib.import_module("levain.voice.providers")
        except ImportError as exc:
            print(exc, file=sys.stderr)
            sys.exit(7)
        sys.exit(0)
    """)
    done = subprocess.run([sys.executable, "-O", "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 7, done.stderr
    # the exit code alone would also pass on an unrelated ImportError (a missing dependency): the cap's own words must be in it
    assert "over the smallest voice adapter input cap" in done.stderr, done.stderr


def test_an_over_cap_text_is_a_pre_send_valueerror_like_every_other_refusal_before_sending(server):
    # a caller that chunks to 3000 (chunk_text accepts any size under 4096) reaches the same exception type the OpenAI
    # adapter raises for its own cap, which the route releases rather than bills
    prov, _ = speaker(server)
    with pytest.raises(ValueError) as info:
        prov.speak(FAKE_KEY, "x" * 3000)
    assert type(info.value) is ValueError and server.received == []
    with pytest.raises(ValueError) as other:
        P.OpenAIProvider(transport=Loopback(server)).speak(FAKE_KEY, "x" * (P.OPENAI_INPUT_CAP + 1))
    assert type(other.value) is type(info.value)


def test_a_413_is_rejected_so_an_unverified_character_unit_fails_closed(server):
    # Deepgram's pages say "characters" and not code points, bytes or UTF-16 units: a text it counts as over the limit
    # is a 413 (https://developers.deepgram.com/docs/errors) and the adapter turns it into a refusal, not a retry
    server.answer = (413, {"Content-Type": "application/json"}, _legacy("Payload Too Large", "Input text exceeds maximum"))
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "\U0001F600" * P.DEEPGRAM_INPUT_CAP)         # 2000 code points, 8000 bytes
    assert info.value.code == "rejected"


# ---- every documented error status, in the body shape the page shows ------------------------------------------------

def _legacy(code: str, msg: str) -> bytes:
    return json.dumps({"err_code": code, "err_msg": f"{msg} {MARKER} {FAKE_KEY}", "request_id": "r-1"}).encode()


# (status, body, the WORDS code) built from https://developers.deepgram.com/docs/errors: 401 INVALID_AUTH and
# INSUFFICIENT_PERMISSIONS, 403 model access, 413 "Payload Too Large", 429 "Too Many Requests", 503 with the odd
# ``error_code`` key. The page shows no TTS 402 body, only the general "402 Payment Required", so that body is the bare
# legacy shape.
ERRORS = [
    (400, _legacy("Bad Request", "Bad Request: No such model/language/tier combination found."), "rejected"),
    (400, _legacy("INVALID_QUERY_PARAMETER", "Unsupported audio format: x"), "rejected"),
    (400, _legacy("PAYLOAD_ERROR", "Failed to deserialize JSON payload."), "rejected"),
    (401, _legacy("INVALID_AUTH", "Invalid credentials."), "refused"),
    (401, _legacy("INSUFFICIENT_PERMISSIONS", "User does not have sufficient permissions."), "refused"),
    (402, _legacy("Payment Required", "The project has insufficient funds to complete the request."), "out_of_credit"),
    (403, _legacy("INSUFFICIENT_PERMISSIONS", "Project does not have access to the requested model."), "refused"),
    (404, _legacy("PROJECT_NOT_FOUND", "Project not found."), "rejected"),
    (413, _legacy("Payload Too Large", "Input text exceeds maximum character limit of 2000."), "rejected"),
    (415, _legacy("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be text/plain or application/json."), "rejected"),
    (422, _legacy("UNPROCESSABLE_ENTITY", "Failed to handle request."), "rejected"),
    (429, _legacy("Too Many Requests", "Please try again later."), "rate_limited"),
    (500, _legacy("Internal Server Error", "boom"), "unavailable"),
    (503, json.dumps({"error_code": "Service Unavailable", "err_msg": f"Please try again later {MARKER} {FAKE_KEY}",
                      "request_id": "r-2"}).encode(), "unavailable"),
]


@pytest.mark.parametrize("status,body,code", ERRORS, ids=[f"{s}-{c}-{i}" for i, (s, _, c) in enumerate(ERRORS)])
def test_each_documented_error_is_a_fixed_code_and_no_body_or_key_is_surfaced(server, status, body, code):
    server.answer = (status, {"Content-Type": "application/json"}, body)
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "Hello there.")
    exc = info.value
    assert exc.code == code and exc.provider == "deepgram"
    assert str(exc) == P.WORDS[code].format(p="Deepgram")                 # the fixed sentence and nothing else
    text = surfaces(exc)
    assert FAKE_KEY not in text and MARKER not in text and "request_id" not in text
    assert exc.__cause__ is None and exc.__context__ is None


def test_a_200_with_no_audio_is_unavailable(server):
    server.answer = (200, {"Content-Type": "audio/mpeg"}, b"")
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"


def test_a_redirect_is_unavailable_and_the_key_goes_nowhere_else(server):
    server.answer = (302, {"Location": "http://127.0.0.1:1/elsewhere"}, b"")
    prov, _ = speaker(server)
    with pytest.raises(P.VoiceError) as info:
        prov.speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable" and len(server.received) == 1


# ---- the key is never logged, printed or echoed ---------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 401, 402, 413, 429, 503])
def test_nothing_is_logged_or_printed_with_the_key_in_it(server, caplog, capsys, status):
    body = MP3 if status == 200 else _legacy("INVALID_AUTH", "Invalid credentials.")
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
    prov, transport = speaker(server)
    prov.speak(FAKE_KEY, "Hello there.")
    assert FAKE_KEY not in repr(prov) and FAKE_KEY not in repr(vars(prov))
    assert all(FAKE_KEY not in repr(r) for r in transport.requests)


def test_an_unsendable_key_is_refused_and_nothing_reaches_the_server(server):
    prov, _ = speaker(server)
    for key in ("", "has space", "bad\r\nX-Injected: 1", "“curly”"):
        with pytest.raises(P.VoiceError) as info:
            prov.speak(key, "Hello there.")
        assert info.value.code == "refused"
    assert server.received == []
