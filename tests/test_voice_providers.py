"""Contract tests for the voice provider adapters, against fixtures in the providers' documented response shape.

No test reaches the network (a socket connect is refused for the whole module) and none holds a real key: ``FAKE_KEY``
is an obvious placeholder, and the fixture files hold only a ``{KEY}`` marker the test fills in, so a leak of the key
into an error body, an exception, a repr or a log is something the tests can see."""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import re
import socket
import traceback
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from levain.voice import providers as P

FAKE_KEY = "FAKE-KEY-NOT-A-CREDENTIAL-0001"
MARKER = "SECRET-BODY-MARKER"          # in every error fixture body: it must never reach the operator
FIXTURES = Path(__file__).parent / "fixtures" / "voice"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a voice test tried to open a socket")
    monkeypatch.setattr(socket.socket, "connect", refuse)


def fixture(name: str) -> P.HttpResponse:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    body = bytes.fromhex(data["body_hex"]) if "body_hex" in data else data["body"].replace("{KEY}", FAKE_KEY).encode()
    return P.HttpResponse(data["status"], body)


class Replay:
    """A transport that answers from a fixture (or raises) and keeps the requests it saw."""

    def __init__(self, name: str | None = None, *, raises: Exception | None = None) -> None:
        self.response = fixture(name) if name else None
        self.raises = raises
        self.seen: list[P.HttpRequest] = []

    def __call__(self, req: P.HttpRequest) -> P.HttpResponse:
        # a copy: the adapter removes the key from the request's own headers once the transport returns
        self.seen.append(dataclasses.replace(req, headers=dict(req.headers)))
        if self.raises is not None:
            raise self.raises
        return self.response


def surfaces(exc: BaseException) -> str:
    """Everything an exception could show a person or a log."""
    return " | ".join([str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))])


def assert_clean(exc: BaseException) -> None:
    text = surfaces(exc)
    assert FAKE_KEY not in text and MARKER not in text
    assert exc.__cause__ is None and exc.__context__ is None


def test_fixture_files_hold_no_key_and_no_key_shaped_string():
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert FAKE_KEY not in text
        assert not re.search(r"sk-[A-Za-z0-9]{8,}", text)
        assert json.loads(text)["_provenance"].startswith("Written in the provider")


# ---- OpenAI speech ----------------------------------------------------------------------------------------------

def test_openai_speak_sends_the_documented_request_and_returns_the_audio():
    t = Replay("speech_200")
    audio = P.OpenAIProvider(transport=t).speak(FAKE_KEY, "Hello there.", "nova", 1.25)
    assert audio == fixture("speech_200").body and audio.startswith(b"ID3")
    (req,) = t.seen
    assert (req.method, req.url) == ("POST", "https://api.openai.com/v1/audio/speech")
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(req.body) == {"model": P.OPENAI_TTS_MODEL, "input": "Hello there.", "voice": "nova", "speed": 1.25,
                                    "response_format": "mp3"}


def test_the_openai_model_id_is_data_not_a_branch():
    t = Replay("speech_200")
    P.OpenAIProvider(transport=t, tts_model="some-future-model").speak(FAKE_KEY, "Hi.")
    assert json.loads(t.seen[0].body)["model"] == "some-future-model"
    assert P.OpenAIProvider().tts_model == P.OPENAI_TTS_MODEL


def test_the_key_is_in_no_repr_and_no_attribute():
    t = Replay("speech_200")
    prov = P.OpenAIProvider(transport=t)
    prov.speak(FAKE_KEY, "Hi.")
    for obj in (prov, t.seen[0], t.response):
        assert FAKE_KEY not in repr(obj)
    assert FAKE_KEY not in repr(vars(prov)) and FAKE_KEY not in str(prov)
    assert repr(t.seen[0]) == "HttpRequest(POST https://api.openai.com/v1/audio/speech)"


def test_nothing_is_logged_by_a_call(caplog):
    caplog.set_level(logging.DEBUG)
    P.OpenAIProvider(transport=Replay("speech_200")).speak(FAKE_KEY, "Hi.")
    with pytest.raises(P.VoiceError):
        P.OpenAIProvider(transport=Replay("openai_401")).speak(FAKE_KEY, "Hi.")
    assert FAKE_KEY not in caplog.text and MARKER not in caplog.text


# ---- status -> fixed words, on both providers ---------------------------------------------------------------------

CASES = [
    ("openai_401", "refused"), ("openai_403", "refused"), ("deepgram_401", "refused"),
    ("openai_429_quota", "out_of_credit"), ("deepgram_402", "out_of_credit"),
    ("openai_429_rate", "rate_limited"),
    ("openai_500", "unavailable"), ("openai_503", "unavailable"), ("redirect_302", "unavailable"),
    ("openai_400", "rejected"),
]


@pytest.mark.parametrize("name,code", CASES)
@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
@pytest.mark.parametrize("call", ["speak", "transcribe"])
def test_every_failure_status_is_a_fixed_code_and_no_body_is_surfaced(name, code, make, call):
    prov = make(Replay(name))
    with pytest.raises(P.VoiceError) as info:
        if call == "speak":
            prov.speak(FAKE_KEY, "Hello.")
        else:
            prov.transcribe(FAKE_KEY, b"\x00\x01audio", "audio/webm;codecs=opus", "en-US")
    exc = info.value
    assert exc.code == code
    assert str(exc) == P.WORDS[code].format(p=P.NAMES[prov.name])
    assert_clean(exc)


def test_the_fixed_words_are_the_designs_for_the_codes_it_names():
    assert P.WORDS["refused"].format(p="OpenAI") == \
        "OpenAI refused the key. It may have been revoked or mistyped; set it again on the laptop."
    assert P.WORDS["out_of_credit"].format(p="OpenAI") == "Your OpenAI account is out of credit."
    assert P.WORDS["rate_limited"].format(p="OpenAI") == "OpenAI is limiting requests right now; try again in a moment."
    assert P.WORDS["unavailable"].format(p="OpenAI") == "OpenAI did not answer. Nothing was charged that Levain knows of."


def test_the_429s_are_told_apart_by_the_error_code_and_type_fields_only():
    for body, code in [(b'{"error": {"code": "insufficient_quota"}}', "out_of_credit"),
                       (b'{"error": {"code": "rate_limit_exceeded"}}', "rate_limited"),
                       (b'{"error": {"message": "insufficient_quota"}}', "rate_limited"),   # the message is not the code
                       (b"insufficient_quota", "rate_limited"),                              # not JSON
                       (b'["insufficient_quota"]', "rate_limited"), (b"", "rate_limited"), (b"\xff\xfe", "rate_limited")]:
        assert P._code_for(429, body) == code


def test_every_status_class_maps_to_a_code():
    assert P._code_for(200, b"") is None and P._code_for(204, b"") is None
    for status, code in [(401, "refused"), (403, "refused"), (402, "out_of_credit"), (408, "unavailable"),
                         (500, "unavailable"), (502, "unavailable"), (504, "unavailable"), (599, "unavailable"),
                         (301, "unavailable"), (307, "unavailable"), (404, "rejected"), (413, "rejected"),
                         (422, "rejected")]:
        assert P._code_for(status, b"") == code, status
    assert {P._code_for(s, b"") for s in range(100, 600)} - {None} <= set(P.WORDS)


@pytest.mark.parametrize("exc", [TimeoutError(f"timed out sending {FAKE_KEY}"), ConnectionError(FAKE_KEY),
                                 OSError(MARKER), ValueError(FAKE_KEY)])
def test_a_transport_failure_is_unavailable_and_chains_nothing(exc):
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=Replay(raises=exc)).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"
    assert_clean(info.value)


def test_a_transport_that_answers_with_a_non_response_is_unavailable():
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=lambda req: "not a response").speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"


def test_a_200_with_no_audio_is_not_an_answer():
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=lambda req: P.HttpResponse(200, b"")).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"


# ---- refusals before any request ---------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["", "has space", "line\nbreak", "tab\there", "nul\x00", "del\x7f", None, 123,
                                 "sk-curly’quote", "sk-nbsp x", "sk-é"])
def test_an_unusable_key_is_refused_and_nothing_is_sent(key):
    t = Replay("speech_200")
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=t).speak(key, "Hi.")
    assert info.value.code == "refused" and t.seen == []
    assert_clean(info.value)


@pytest.mark.parametrize("args", [("", "nova", 1.0), ("   ", "nova", 1.0), ("x" * (P.API_CAP + 1), "nova", 1.0),
                                  ("Hi.", "nova", 0.1), ("Hi.", "nova", 4.5), ("Hi.", "nova/../x", 1.0),
                                  ("Hi.", "", 1.0), ("Hi.", "no va", 1.0), ("Hi.", "nova", True),
                                  ("Hi.", "nova", "1.0"), ("Hi.", "nova", None), ("Hi.", "nova", float("nan"))])
@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
def test_speak_refuses_bad_input_before_sending(args, make):
    t = Replay("speech_200")
    with pytest.raises(ValueError) as info:
        make(t).speak(FAKE_KEY, *args)
    assert t.seen == [] and FAKE_KEY not in str(info.value)


def test_the_input_cap_is_inclusive():
    t = Replay("speech_200")
    P.OpenAIProvider(transport=t).speak(FAKE_KEY, "x" * P.API_CAP)
    assert len(t.seen) == 1


@pytest.mark.parametrize("audio,mime,lang", [(b"", "audio/webm", "en"), (b"a", "video/mp4", "en"), (b"a", "", "en"),
                                             (b"a", "audio/webm", "en US"), (b"a", "audio/webm", "english-language-tag")])
def test_transcribe_refuses_bad_input_before_sending(audio, mime, lang):
    t = Replay("openai_transcribe_200")
    with pytest.raises(ValueError):
        P.OpenAIProvider(transport=t).transcribe(FAKE_KEY, audio, mime, lang)
    assert t.seen == []


# ---- OpenAI transcription ----------------------------------------------------------------------------------------

def test_openai_transcribe_is_a_multipart_request_and_returns_stripped_text():
    audio = b"\x1aE\xdf\xa3 pretend webm bytes \x00\xff"
    t = Replay("openai_transcribe_200")
    text = P.OpenAIProvider(transport=t).transcribe(FAKE_KEY, audio, "audio/webm;codecs=opus", "en-US")
    assert text == "Hello from the room."
    (req,) = t.seen
    assert req.url == "https://api.openai.com/v1/audio/transcriptions"
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    ctype = req.headers["Content-Type"]
    boundary = ctype.split("boundary=")[1]
    assert ctype.startswith("multipart/form-data; ")
    assert req.body.startswith(f"--{boundary}\r\n".encode()) and req.body.endswith(f"\r\n--{boundary}--\r\n".encode())
    assert audio in req.body
    assert f'name="model"\r\n\r\n{P.OPENAI_STT_MODEL}'.encode() in req.body
    assert b'name="language"\r\n\r\nen\r\n' in req.body                # BCP-47 narrowed to the ISO code
    assert b'filename="audio.webm"' in req.body and b"Content-Type: audio/webm\r\n" in req.body
    assert FAKE_KEY.encode() not in req.body


def test_the_multipart_body_is_deterministic_and_the_boundary_is_not_in_the_audio():
    audio = b"same audio"
    first, second = Replay("openai_transcribe_200"), Replay("openai_transcribe_200")
    P.OpenAIProvider(transport=first).transcribe(FAKE_KEY, audio, "audio/wav")
    P.OpenAIProvider(transport=second).transcribe(FAKE_KEY, audio, "audio/wav")
    assert first.seen[0].body == second.seen[0].body
    b1 = P._multipart({}, "a.wav", "audio/wav", audio)[1]
    hostile = f"xx{b1}yy".encode()
    b2 = P._multipart({}, "a.wav", "audio/wav", hostile)[1]
    assert b2.encode() not in hostile


def test_no_language_means_no_language_field():
    t = Replay("openai_transcribe_200")
    P.OpenAIProvider(transport=t).transcribe(FAKE_KEY, b"a", "audio/mpeg")
    assert b'name="language"' not in t.seen[0].body


def test_an_answer_with_no_text_is_the_empty_string():
    assert P.OpenAIProvider(transport=Replay("openai_transcribe_empty")).transcribe(FAKE_KEY, b"a", "audio/mp4") == ""


@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
def test_a_200_in_the_wrong_shape_is_unavailable_and_not_echoed(make):
    with pytest.raises(P.VoiceError) as info:
        make(Replay("wrong_shape_200")).transcribe(FAKE_KEY, b"a", "audio/wav")
    assert info.value.code == "unavailable"
    assert_clean(info.value)


# ---- Deepgram ----------------------------------------------------------------------------------------------------

def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def test_deepgram_speak_asserts_mip_opt_out_and_uses_the_token_scheme():
    t = Replay("speech_200")
    audio = P.DeepgramProvider(transport=t).speak(FAKE_KEY, "Hello there.", speed=1.5)
    (req,) = t.seen
    parts = urlsplit(req.url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "api.deepgram.com", "/v1/speak")
    assert _query(req.url)["mip_opt_out"] == ["true"]
    assert _query(req.url)["model"] == [P.DEEPGRAM_TTS_VOICE] and _query(req.url)["encoding"] == ["mp3"]
    assert req.headers["Authorization"] == f"Token {FAKE_KEY}" and FAKE_KEY not in req.url
    assert json.loads(req.body) == {"text": "Hello there."}
    assert audio.startswith(b"ID3")


def test_deepgram_transcribe_asserts_mip_opt_out_and_returns_the_transcript():
    audio = b"pretend mp4 bytes"
    t = Replay("deepgram_listen_200")
    text = P.DeepgramProvider(transport=t).transcribe(FAKE_KEY, audio, "audio/mp4", "en-US")
    (req,) = t.seen
    parts = urlsplit(req.url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "api.deepgram.com", "/v1/listen")
    q = _query(req.url)
    assert q["mip_opt_out"] == ["true"] and q["model"] == [P.DEEPGRAM_STT_MODEL] and q["language"] == ["en-US"]
    assert req.headers["Content-Type"] == "audio/mp4" and req.body == audio
    assert text == "Hello from the room."


def test_mip_opt_out_cannot_be_overridden_by_any_query_argument():
    assert _query(P.DeepgramProvider._url("/v1/listen", mip_opt_out="false"))["mip_opt_out"] == ["true"]
    t = Replay("deepgram_listen_200")
    P.DeepgramProvider(transport=t).transcribe(FAKE_KEY, b"a", "audio/wav")
    assert "mip_opt_out=true" in t.seen[0].url                                   # even with no language given


# ---- hosts, redirects, the real transport -----------------------------------------------------------------------

def test_every_request_goes_to_an_allowed_https_host_and_carries_no_userinfo():
    seen: list[P.HttpRequest] = []
    for prov in (P.OpenAIProvider(transport=Replay("speech_200")), P.DeepgramProvider(transport=Replay("speech_200"))):
        t = prov.transport
        prov.speak(FAKE_KEY, "Hi.")
        seen += t.seen
    for prov in (P.OpenAIProvider(transport=Replay("openai_transcribe_200")),
                 P.DeepgramProvider(transport=Replay("deepgram_listen_200"))):
        prov.transcribe(FAKE_KEY, b"a", "audio/wav")
        seen += prov.transport.seen
    assert len(seen) == 4
    for req in seen:
        parts = urlsplit(req.url)
        assert parts.scheme == "https" and parts.hostname in P.ALLOWED_HOSTS and not parts.username


def test_the_hosts_are_the_designs_constants():
    assert P.HOSTS == {"openai": "api.openai.com", "deepgram": "api.deepgram.com", "inworld": "api.inworld.ai"}


@pytest.mark.parametrize("url", ["http://api.openai.com/v1/audio/speech", "https://evil.example/v1/audio/speech",
                                 "https://api.openai.com.evil.example/x", "https://user:pw@api.openai.com/x",
                                 "ftp://api.openai.com/x", "https://api.openai.com@evil.example/x"])
def test_the_real_transport_refuses_any_other_destination_before_opening(url, monkeypatch):
    class Boom:
        def open(self, *a, **k):
            raise AssertionError("the opener was reached")
    monkeypatch.setattr(P, "_OPENER", Boom())
    with pytest.raises(ValueError):
        P.urllib_transport(P.HttpRequest("POST", url, {"Authorization": f"Bearer {FAKE_KEY}"}, b""))


def test_a_redirect_is_never_followed():
    assert P._NoRedirect().redirect_request(object(), object(), 302, "Found", {}, "https://elsewhere.invalid/") is None
    assert any(isinstance(h, P._NoRedirect) for h in P._OPENER.handlers)


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.openai.com/v1/audio/speech", status, "x", {}, io.BytesIO(body))


def test_the_real_transport_returns_an_error_status_as_a_response(monkeypatch):
    class Opener:
        def open(self, req, timeout=None):
            assert timeout == P.TIMEOUT
            raise _http_error(302, b"")
    monkeypatch.setattr(P, "_OPENER", Opener())
    resp = P.urllib_transport(P.HttpRequest("POST", "https://api.openai.com/v1/audio/speech", {}, b""))
    assert resp.status == 302
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=P.urllib_transport).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"
    assert_clean(info.value)


def test_the_real_transport_maps_a_429_body_it_read_to_the_right_code(monkeypatch):
    class Opener:
        def open(self, req, timeout=None):
            raise _http_error(429, b'{"error": {"code": "insufficient_quota", "message": "key ' + FAKE_KEY.encode() + b'"}}')
    monkeypatch.setattr(P, "_OPENER", Opener())
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=P.urllib_transport).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "out_of_credit"
    assert_clean(info.value)


def test_the_real_transport_refuses_an_oversize_response(monkeypatch):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read1(self, n): return b"x" * n
    class Opener:
        def open(self, req, timeout=None): return Resp()
    monkeypatch.setattr(P, "_OPENER", Opener())
    with pytest.raises(ValueError):
        P.urllib_transport(P.HttpRequest("POST", "https://api.openai.com/v1/audio/speech", {}, b""))


def test_the_real_transport_reads_no_401_body_and_closes_every_error_response(monkeypatch):
    class Body(io.BytesIO):
        reads = 0

        def read1(self, *a):
            self.reads += 1
            return super().read(*a)

    fps: list[Body] = []

    class Opener:
        def __init__(self, status: int) -> None:
            self.status = status

        def open(self, req, timeout=None):
            fps.append(Body(b'{"error": {"code": "insufficient_quota", "message": "' + FAKE_KEY.encode() + b'"}}'))
            raise urllib.error.HTTPError("https://api.openai.com/v1/audio/speech", self.status, "x", {}, fps[-1])

    req = P.HttpRequest("POST", "https://api.openai.com/v1/audio/speech", {}, b"")
    for status in (401, 403, 400, 500):
        monkeypatch.setattr(P, "_OPENER", Opener(status))
        resp = P.urllib_transport(req)
        assert (resp.status, resp.body) == (status, b"") and fps[-1].reads == 0     # not even read
    monkeypatch.setattr(P, "_OPENER", Opener(429))
    assert P._code_for(429, P.urllib_transport(req).body) == "out_of_credit"
    assert all(fp.closed for fp in fps) and len(fps) == 5


def _strings(value, seen: set[int], depth: int = 0):
    """Every string reachable from ``value`` through containers, instance dicts and slots (functions skipped)."""
    if depth > 8 or id(value) in seen or callable(value):
        return
    seen.add(id(value))
    if isinstance(value, (str, bytes)):
        yield value if isinstance(value, str) else value.decode("latin-1")
        return
    if isinstance(value, dict):
        children = [*value.keys(), *value.values()]
    elif isinstance(value, (list, tuple, set, frozenset)):
        children = list(value)
    else:
        children = list(getattr(value, "__dict__", {}).values())
        children += [getattr(value, s, None) for s in getattr(type(value), "__slots__", ())]
    for child in children:
        yield from _strings(child, seen, depth + 1)


def _provider_frames_hold_no_key(exc: BaseException) -> int:
    tb, frames, seen = exc.__traceback__, 0, set()
    while tb is not None:
        if tb.tb_frame.f_globals.get("__name__") == P.__name__:
            frames += 1
            for s in _strings(dict(tb.tb_frame.f_locals), seen):
                assert FAKE_KEY not in s and MARKER not in s, tb.tb_frame.f_code.co_name
        tb = tb.tb_next
    return frames


@pytest.mark.parametrize("case", ["status_401", "transport_raises", "bad_shape_200", "bad_speed", "key_with_newline"])
@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
def test_no_frame_on_a_failures_traceback_holds_the_key(case, make):
    def raises(req):
        raise TimeoutError("timed out")
    transport = {"status_401": lambda req: fixture("openai_401"), "transport_raises": raises,
                 "bad_shape_200": lambda req: P.HttpResponse(200, b'{"nope": 1}'),
                 "bad_speed": lambda req: fixture("speech_200"),
                 "key_with_newline": lambda req: fixture("speech_200")}[case]
    prov = make(transport)
    with pytest.raises((P.VoiceError, ValueError)) as info:
        if case == "bad_shape_200":
            prov.transcribe(FAKE_KEY, b"a", "audio/wav")
        else:
            prov.speak(FAKE_KEY + ("\n" if case == "key_with_newline" else ""), "Hi.",
                       speed=9.0 if case == "bad_speed" else 1.0)
    assert _provider_frames_hold_no_key(info.value) >= 1
    rendered = "".join(traceback.TracebackException.from_exception(info.value, capture_locals=True).format())
    assert FAKE_KEY not in rendered and MARKER not in rendered


def test_every_adapter_model_is_priced_by_the_meter():
    from levain.voice.meter import DEFAULT_PRICES
    for name in P.HOSTS:
        prov = P.provider_for(name)
        assert DEFAULT_PRICES.find(name, "tts", prov.tts_model) is not None, name
        if "stt" in prov.uses:      # a speech-only adapter (inworld) has no transcription model to price
            assert DEFAULT_PRICES.find(name, "stt", prov.stt_model) is not None, name


def test_a_deepgram_voice_outside_the_priced_family_is_refused_before_sending():
    t = Replay("speech_200")
    with pytest.raises(ValueError):
        P.DeepgramProvider(transport=t).speak(FAKE_KEY, "Hi.", "aura-1-asteria-en")
    assert t.seen == []
    P.DeepgramProvider(transport=t).speak(FAKE_KEY, "Hi.", "aura-2-andromeda-en")
    assert _query(t.seen[0].url)["model"] == ["aura-2-andromeda-en"]


def test_an_unknown_provider_is_refused_and_the_known_ones_resolve():
    assert isinstance(P.provider_for("openai"), P.OpenAIProvider)
    assert isinstance(P.provider_for("deepgram"), P.DeepgramProvider)
    assert isinstance(P.provider_for("inworld"), P.InworldProvider)
    with pytest.raises(ValueError):
        P.provider_for("elevenlabs")
    assert set(P.NAMES) == set(P.HOSTS)
    assert {c for c in P.WORDS} >= {P._code_for(s, b"") for s in (401, 402, 404, 429, 500)} - {None}


def test_deepgram_positional_construction_keeps_its_older_field_order():
    # tts_model was added AFTER the fields that existed, so a positional caller's stt_model still lands in stt_model
    prov = P.DeepgramProvider(P.urllib_transport, "nova-custom")
    assert prov.stt_model == "nova-custom"
    assert prov.tts_model == P.DEEPGRAM_TTS_MODEL


# --- V1-4 (B4): OpenAI's 429 billing codes (design DESIGN_voice_v14.md F1) -------------------------------------------

@pytest.mark.parametrize("error,code", [
    # an empty balance: the new code, the broad type alone, and the old code
    ({"type": "insufficient_quota", "code": "credit_balance_exhausted"}, "out_of_credit"),
    ({"code": "credit_balance_exhausted"}, "out_of_credit"),
    ({"type": "insufficient_quota"}, "out_of_credit"),
    ({"code": "insufficient_quota"}, "out_of_credit"),
    # a limit the account holder set: the code wins over the broad type the vendor says may still ride with it
    ({"type": "insufficient_quota", "code": "organization_spend_limit_exceeded"}, "spend_limited"),
    ({"code": "project_spend_limit_exceeded"}, "spend_limited"),
    ({"type": "insufficient_quota", "code": "organization_usage_limit_exceeded"}, "spend_limited"),
    # an ordinary rate limit stays one (a retry can help it)
    ({"code": "rate_limit_exceeded"}, "rate_limited"),
    ({"type": "requests", "code": "slow_down"}, "rate_limited"),
    ({"message": "credit_balance_exhausted"}, "rate_limited"),        # a message is not a code
])
def test_a_429_is_out_of_credit_a_spend_limit_or_a_rate_limit_by_its_code(error, code):
    body = json.dumps({"error": {**error, "message": f"{MARKER} quoting {FAKE_KEY}"}}).encode()
    assert P._code_for(429, body) == code
    t = Replay()
    t.response = P.HttpResponse(429, body)
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=t).speak(FAKE_KEY, "Hi.", "nova", 1.0)
    assert info.value.code == code
    assert str(info.value) == P.WORDS[code].format(p="OpenAI")
    assert_clean(info.value)


def test_the_spend_limit_has_its_own_words_naming_the_dashboard_not_money():
    assert P.WORDS["spend_limited"].format(p="OpenAI") == \
        "Your OpenAI spend limit is reached. Raise it on OpenAI's site, or wait for it to reset."
    assert P.WORDS["spend_limited"] != P.WORDS["out_of_credit"]


# --- voice14 round 1 (FINDINGS_voice14_r1.md): F1 shapes, F3 pin, F4 deadline ----------------------------------------------

@pytest.mark.parametrize("error", [
    {"code": ["insufficient_quota"]}, {"code": {"x": "insufficient_quota"}}, {"code": 429}, {"code": None},
    {"code": [["organization_spend_limit_exceeded"]]}, {"code": {"a": {"b": ["credit_balance_exhausted"]}}},
    {"code": True}, {"code": 1.5},
    {"type": ["insufficient_quota"]}, {"type": {"insufficient_quota": 1}}, {"type": 7}, {"type": None},
    {"code": ["x"], "type": ["y"]}, {"code": {"a": 1}, "type": {"b": 2}},
], ids=lambda e: json.dumps(e))
def test_f1_a_429_whose_code_or_type_is_not_a_string_is_a_rate_limit_not_a_crash(error):
    """voice14 F1: ``code in <frozenset>`` raised TypeError for a list or dict (unhashable), which escaped
    ``_exchange`` and became a 500 at the route. Every malformed shape is an ordinary ``rate_limited``.
    Red with: the ``isinstance(code, str)`` guard removed from ``_code_of_429``."""
    body = json.dumps({"error": {**error, "message": f"{MARKER} quoting {FAKE_KEY}"}}).encode()
    assert P._code_for(429, body) == "rate_limited"
    t = Replay()
    t.response = P.HttpResponse(429, body)
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=t).speak(FAKE_KEY, "Hi.", "nova", 1.0)
    assert info.value.code == "rate_limited"
    assert_clean(info.value)


def test_f1_a_string_code_still_wins_beside_a_malformed_type():
    body = json.dumps({"error": {"code": "organization_spend_limit_exceeded", "type": ["insufficient_quota"]}}).encode()
    assert P._code_for(429, body) == "spend_limited"


def _responses():
    return [
        P.HttpResponse(200, b""), P.HttpResponse(200, b"not json"), P.HttpResponse(200, b'{"nope": 1}'),
        P.HttpResponse(200, b'{"text": 5}'), P.HttpResponse(200, b"\xff\xfe"), P.HttpResponse(200, b"[]"),
        P.HttpResponse(200, b'{"results": {"channels": []}}'),
        P.HttpResponse(401, b""), P.HttpResponse(429, b'{"error": {"code": ["x"]}}'), P.HttpResponse(500, b"x"),
        "not a response", None,
    ]


@pytest.mark.parametrize("response", _responses(), ids=lambda r: repr(r)[:40])
@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
@pytest.mark.parametrize("use", ["speak", "transcribe"])
def test_f3_no_adapter_raises_a_valueerror_once_the_transport_has_been_called(response, make, use):
    """voice14 F3: the route RELEASES a reservation on a ValueError (nothing went out) and settles on everything else,
    which is only sound while every adapter turns a failure after the send into a VoiceError. Whatever the transport
    returned, the outcome is a result or a VoiceError. Red with: ``_text_of`` or ``_exchange`` letting a ValueError
    (a JSONDecodeError is one) through."""
    prov = make(lambda req: response)
    try:
        if use == "speak":
            prov.speak(FAKE_KEY, "Hi.")
        else:
            prov.transcribe(FAKE_KEY, b"audio", "audio/wav")
    except P.VoiceError:
        pass
    except ValueError as exc:      # the one thing this test exists to refuse
        pytest.fail(f"a ValueError escaped after the transport returned: {type(exc).__name__}")


@pytest.mark.parametrize("make", [lambda t: P.OpenAIProvider(transport=t), lambda t: P.DeepgramProvider(transport=t)],
                         ids=["openai", "deepgram"])
def test_f3_a_transport_that_itself_raises_valueerror_is_unavailable(make):
    """The real transport raises ValueError for 'response too large' AFTER the request went out; the adapter must turn
    it into a VoiceError so the route settles the row. Red with: ``_exchange`` catching narrower than ``Exception``."""
    def transport(req):
        raise ValueError("response too large")
    with pytest.raises(P.VoiceError) as info:
        make(transport).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TrickleResp:
    """A response that gives one byte per ``read1`` and takes ``gap`` fake seconds to do it, ``n`` times, then ends."""
    status = 200

    def __init__(self, clock: _Clock, gap: float, n: int = 10_000) -> None:
        self.clock, self.gap, self.left = clock, gap, n
        self.reads = 0
        self.closed = False

    def __enter__(self): return self
    def __exit__(self, *a):
        self.closed = True
        return False

    def read1(self, n):
        self.reads += 1
        self.clock.now += self.gap
        if self.left <= 0:
            return b""
        self.left -= 1
        return b"x"


def _trickling(monkeypatch, gap: float, n: int = 10_000):
    clock = _Clock()
    resp = _TrickleResp(clock, gap, n)

    class Opener:
        def open(self, req, timeout=None):
            self.timeout = timeout
            return resp
    opener = Opener()
    monkeypatch.setattr(P, "_monotonic", clock)
    monkeypatch.setattr(P, "_OPENER", opener)
    return clock, resp, opener


def test_f4_a_provider_trickling_a_byte_inside_every_socket_timeout_is_cut_at_the_total_deadline(monkeypatch):
    """voice14 F4: ``timeout`` is per socket operation, so a byte every 25 s never trips it and a call held a voice slot
    for as long as the provider liked. One monotonic budget now covers the whole exchange, checked between reads, and
    the connection is closed when it is spent; the adapter reports ``unavailable`` (the call went out: it settles).
    Red with: the deadline check removed from ``_read_until`` (the trickle is read to its end and is an 'answer')."""
    _clock, resp, opener = _trickling(monkeypatch, gap=25.0)
    assert 25.0 < P.TIMEOUT < P.TOTAL_TIMEOUT
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=P.urllib_transport).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "unavailable"
    assert_clean(info.value)
    assert resp.closed
    assert resp.reads <= int(P.TOTAL_TIMEOUT / 25.0) + 1, resp.reads
    assert opener.timeout == P.TIMEOUT                      # the per-operation timeout is still the connect's


def test_f4_the_budget_is_the_requests_and_a_connect_never_waits_longer_than_it(monkeypatch):
    _clock, resp, opener = _trickling(monkeypatch, gap=0.5, n=40)
    req = P.HttpRequest("POST", "https://api.openai.com/v1/audio/speech", {}, b"", total=5.0)
    with pytest.raises(TimeoutError):
        P.urllib_transport(req)
    assert resp.closed and resp.reads == 10 and opener.timeout == 5.0
    _clock2, resp2, _o = _trickling(monkeypatch, gap=0.01, n=40)
    got = P.urllib_transport(dataclasses.replace(req, total=5.0))
    assert (got.status, got.body) == (200, b"x" * 40) and resp2.closed


def test_f4_a_429_body_trickled_past_the_deadline_is_an_ordinary_rate_limit_and_is_closed(monkeypatch):
    clock = _Clock()
    reads = []

    class Err(urllib.error.HTTPError):
        def read1(self, n):
            reads.append(n)
            clock.now += 25.0
            return b" "

    errs: list[Err] = []

    class Opener:
        def open(self, req, timeout=None):
            errs.append(Err("https://api.openai.com/v1/audio/speech", 429, "x", {}, io.BytesIO(b"")))
            raise errs[-1]

    monkeypatch.setattr(P, "_monotonic", clock)
    monkeypatch.setattr(P, "_OPENER", Opener())
    with pytest.raises(P.VoiceError) as info:
        P.OpenAIProvider(transport=P.urllib_transport).speak(FAKE_KEY, "Hi.")
    assert info.value.code == "rate_limited"
    assert 1 <= len(reads) <= int(P.TOTAL_TIMEOUT / 25.0) + 1 and errs[0].fp.closed


def test_f4_a_normal_exchange_is_unaffected_by_the_clock(monkeypatch):
    _clock, resp, _o = _trickling(monkeypatch, gap=0.0, n=3)
    got = P.urllib_transport(P.HttpRequest("POST", "https://api.openai.com/v1/audio/speech", {}, b""))
    assert (got.status, got.body) == (200, b"xxx")


def test_r7_a_body_in_tiny_reads_costs_about_its_size_not_an_object_per_read(monkeypatch):
    """voice14 r2 (deepseek MED): ``_read_until`` kept every ``read1`` result as its own bytes object, so a body sent in
    millions of tiny reads inside the budget cost one object (~35 bytes of header) per read on top of its size. One
    buffer now: N two-byte reads of fresh objects peak at a few times the body (the buffer, its over-allocation and the
    final copy), well under the ~20 bytes per read a per-read list costs at the least. Red with: the list of pieces
    back (``pieces.append`` then ``b"".join``)."""
    import tracemalloc

    n = 200_000

    class Tiny:
        left = n

        def read1(self, size):
            if self.left <= 0:
                return b""
            self.left -= 1
            return bytes((65, 66))      # a new object each read (a 1-byte bytes is a cached singleton)

    monkeypatch.setattr(P, "_monotonic", lambda: 0.0)
    tracemalloc.start()
    try:
        got = P._read_until(Tiny(), 1.0, 10 * n)
        _cur, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == b"AB" * n
    assert peak < 3 * 2 * n + 256 * 1024, peak          # bound: 3x the body plus slack; a per-read list is ~8 MB here
