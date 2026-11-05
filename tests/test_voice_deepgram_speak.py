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
import socket
import threading
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


def test_speed_is_validated_and_never_sent(server):
    prov, _ = speaker(server)
    prov.speak(FAKE_KEY, "Hi.", speed=1.5)
    assert "speed" not in parse_qs(urlsplit(server.received[0]["target"]).query)
    assert b"speed" not in server.received[0]["body"]


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
