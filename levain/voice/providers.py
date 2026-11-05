"""The premium voice providers: one adapter per provider, each a pure translation between Levain's two calls (speak,
transcribe) and the provider's HTTP API. The trust boundary is here, so what these adapters refuse matters more than what
they send.

What an adapter holds and never holds:

* **The key** arrives as a parameter of :meth:`speak` / :meth:`transcribe`, goes into one request's headers and is not
  kept. It is not an attribute of an adapter, a request's ``repr`` (headers and body are excluded from it), an exception
  or a log line. A key the provider cannot be sent (empty, or holding a control character that would split a header) is
  refused as a rejected key, naming nothing.
* **A provider's response body** is never surfaced. An OpenAI 401 body quotes part of the key it refused (design threat
  T2, still to be confirmed against a real dead key), so the body is read for exactly one thing, whether a 429's
  ``error.code`` is ``insufficient_quota``, and then dropped. What the operator sees is :class:`VoiceError`: a fixed code
  and a fixed sentence (:data:`WORDS`), raised outside the ``except`` that caught the failure so no transport exception
  (which may quote a header) is chained to it.
* **The host** is a constant per provider (:data:`HOSTS`), https only, and a redirect is never followed (the
  ``levain/webpush/sender.py`` precedent). There is no base-URL setting: a caller cannot point an adapter, and so a key,
  somewhere else.

HTTP goes through a transport the caller passes, ``Callable[[HttpRequest], HttpResponse]``, so a test replays a recorded
response and no test reaches the network. :func:`urllib_transport` is the real one.

Facts about the providers, and where each comes from (the design is ``project_memory/1104-voice/V1_DESIGN.md`` section 3.6):

* OpenAI speech is ``POST /v1/audio/speech`` and transcription ``POST /v1/audio/transcriptions``; the speech page was read
  by the planning lane on 2026-11-05. It also says ``tts-1``, ``tts-1-hd`` and ``gpt-4o-mini-tts`` are announced to shut
  down on 2027-01-06, so the model id is DATA (:data:`OPENAI_TTS_MODEL`, a constructor argument), never a code branch;
  which model replaces it is a ruling that is not made here. The transcription model id and the transcription endpoint
  path are from the design and OpenAI's usual API shape, not fetched.
* Deepgram: ``mip_opt_out=true`` as a query parameter on the request URL is from Deepgram's model-improvement-programme page
  (read by the planning lane). The Aura-2 speech path (``/v1/speak``) and the Nova-3 pre-recorded path (``/v1/listen``),
  the ``Token`` authorization scheme, the ``encoding=mp3`` parameter and the transcript's place in the response are from
  the design, not fetched: UNVERIFIED until a real call (the slice's named residue). Deepgram speech takes no speed here.
* ElevenLabs is not built in this slice.

No claim above about a provider's behaviour is tested against the provider: the fixtures under ``tests/fixtures/voice``
are written in the documented response shape, not recorded from a live call.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from levain.voice.text import API_CAP

HOSTS = {"openai": "api.openai.com", "deepgram": "api.deepgram.com"}
ALLOWED_HOSTS = frozenset(HOSTS.values())

OPENAI_TTS_MODEL = "tts-1"                      # DATA: announced to shut down 2027-01-06 (module docstring)
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
OPENAI_DEFAULT_VOICE = "nova"
DEEPGRAM_TTS_VOICE = "aura-2-thalia-en"         # a model id; from the design's "Aura-2", the voice name is not fetched
DEEPGRAM_STT_MODEL = "nova-3"

TIMEOUT = 30.0                                  # seconds, total; urllib has no separate connect timeout
MAX_RESPONSE = 16 * 1024 * 1024                 # a response past this is not read: it is not an answer
MIME_ALLOW = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
SPEED_MIN, SPEED_MAX = 0.25, 4.0

# The fixed sentences. ``{p}`` is the provider in use. A provider body is never one of them. "rejected" (a 4xx that is
# neither the key nor a limit) has no sentence in the design; this is a placeholder for that gap.
WORDS = {
    "refused": "{p} refused the key. It may have been revoked or mistyped; set it again on the laptop.",
    "out_of_credit": "Your {p} account is out of credit.",
    "rate_limited": "{p} is limiting requests right now; try again in a moment.",
    "unavailable": "{p} did not answer. Nothing was charged that Levain knows of.",
    "rejected": "{p} could not use that request.",
}
NAMES = {"openai": "OpenAI", "deepgram": "Deepgram"}


class VoiceError(Exception):
    """A provider call failed. ``code`` is a key of :data:`WORDS`; the message is that fixed sentence for ``provider``
    and nothing else: no status, body, header or key is ever part of it."""

    def __init__(self, code: str, provider: str) -> None:
        self.code = code
        self.provider = provider
        super().__init__(WORDS[code].format(p=NAMES.get(provider, "The voice provider")))


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(repr=False)     # holds the key
    body: bytes = field(repr=False)
    timeout: float = TIMEOUT

    def __repr__(self) -> str:
        return f"HttpRequest({self.method} {self.url})"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)

    def __repr__(self) -> str:
        return f"HttpResponse({self.status})"


Transport = Callable[[HttpRequest], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # a provider answers in place; a redirect is not followed
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(req: HttpRequest) -> HttpResponse:
    """The real transport: https to one of :data:`ALLOWED_HOSTS` only, a redirect answered as its 3xx status and never
    followed, a response read to at most :data:`MAX_RESPONSE` bytes. An error status is a response, not an exception;
    an unreachable host raises, and the adapter maps that to ``unavailable``."""
    parts = urllib.parse.urlsplit(req.url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password:
        raise ValueError("destination is not an allowed voice provider")
    request = urllib.request.Request(req.url, data=req.body, headers=dict(req.headers), method=req.method)
    try:
        with _OPENER.open(request, timeout=req.timeout) as resp:
            body = resp.read(MAX_RESPONSE + 1)
            status = int(resp.status)
    except urllib.error.HTTPError as err:
        try:
            body = err.read(64 * 1024)
        except Exception:   # noqa: BLE001 - an unreadable error body is an empty one
            body = b""
        return HttpResponse(int(err.code), body)
    if len(body) > MAX_RESPONSE:
        raise ValueError("response too large")
    return HttpResponse(status, body)


def _is_insufficient_quota(body: bytes) -> bool:
    """True when a 429's JSON body says ``error.code == "insufficient_quota"``. The only thing a body is read for."""
    try:
        err = json.loads(body[:64 * 1024].decode("utf-8")).get("error")
        return isinstance(err, dict) and err.get("code") == "insufficient_quota"
    except Exception:   # noqa: BLE001 - not JSON, not an object: an ordinary rate limit
        return False


def _code_for(status: int, body: bytes) -> str | None:
    """The :data:`WORDS` code for a status, None for success."""
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return "refused"
    if status == 402:
        return "out_of_credit"
    if status == 429:
        return "out_of_credit" if _is_insufficient_quota(body) else "rate_limited"
    if status in (408, 425) or status < 200 or 300 <= status < 400 or status >= 500:
        return "unavailable"     # a timeout, a redirect (never followed), a server fault: the provider did not answer
    return "rejected"


def _send(transport: Transport, provider: str, req: HttpRequest) -> bytes:
    """The body of a 2xx answer, or a :class:`VoiceError`. Whatever the transport raises is dropped here (it may quote a
    header) and the error is raised OUTSIDE the ``except``, so it carries no chained context."""
    failed = False
    resp: HttpResponse | None = None
    try:
        resp = transport(req)
    except Exception:   # noqa: BLE001 - timeout, DNS, TLS, a redirect loop: all one fixed answer
        failed = True
    if failed or not isinstance(resp, HttpResponse):
        raise VoiceError("unavailable", provider)
    code = _code_for(resp.status, resp.body)
    if code is not None:
        raise VoiceError(code, provider)
    return resp.body


def _need_key(key: str, provider: str) -> str:
    if not isinstance(key, str) or not key or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise VoiceError("refused", provider)
    return key


def _need_speech(text: str, speed: float) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("nothing to speak")
    if len(text) > API_CAP:
        raise ValueError("text is over the provider's input cap; chunk it first")
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise ValueError("speed is outside the provider's range")


def _need_audio(audio: bytes, mime: str) -> str:
    base = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    if base not in MIME_ALLOW:
        raise ValueError("audio type is not allowed")
    if not audio:
        raise ValueError("no audio")
    return base


_VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LANG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")


def _need_voice(voice: str) -> str:
    if not isinstance(voice, str) or _VOICE_ID.fullmatch(voice) is None:
        raise ValueError("voice id is malformed")
    return voice


def _need_lang(lang: str) -> str:
    if lang and (not isinstance(lang, str) or _LANG.fullmatch(lang) is None):
        raise ValueError("language tag is malformed")
    return lang or ""


def _multipart(fields: dict[str, str], filename: str, mime: str, audio: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body and its boundary. The boundary is derived from the audio so the same input is the same
    bytes, and is lengthened until it appears nowhere in the audio."""
    boundary = "levain" + hashlib.sha256(audio).hexdigest()[:32]
    while boundary.encode() in audio:
        boundary += "x"
    head = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in fields.items())
    file_part = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f"Content-Type: {mime}\r\n\r\n").encode()
    return head + file_part + audio + f"\r\n--{boundary}--\r\n".encode(), boundary


@dataclass(frozen=True)
class OpenAIProvider:
    """OpenAI speech (mp3) and transcription. ``tts_model`` and ``stt_model`` are data, see the module docstring."""

    transport: Transport = field(default=urllib_transport, repr=False)
    tts_model: str = OPENAI_TTS_MODEL
    stt_model: str = OPENAI_STT_MODEL
    name: str = "openai"
    uses: frozenset[str] = frozenset({"tts", "stt"})

    def speak(self, key: str, text: str, voice: str = OPENAI_DEFAULT_VOICE, speed: float = 1.0) -> bytes:
        """mp3 bytes for ``text`` (at most :data:`API_CAP` characters) in ``voice`` at ``speed``."""
        _need_speech(text, speed)
        body = json.dumps({"model": self.tts_model, "input": text, "voice": _need_voice(voice), "speed": speed,
                           "response_format": "mp3"}).encode("utf-8")
        req = HttpRequest("POST", f"https://{HOSTS['openai']}/v1/audio/speech",
                          {"Authorization": f"Bearer {_need_key(key, self.name)}", "Content-Type": "application/json",
                           "Accept": "audio/mpeg"}, body)
        audio = _send(self.transport, self.name, req)
        if not audio:
            raise VoiceError("unavailable", self.name)      # a 200 with no audio is not an answer
        return audio

    def transcribe(self, key: str, audio: bytes, mime: str, lang: str = "") -> str:
        """The text of ``audio``. An answer with no text is the empty string (the caller says "Heard nothing")."""
        base = _need_audio(audio, mime)
        fields = {"model": self.stt_model, "response_format": "json"}
        if _need_lang(lang):
            fields["language"] = lang.split("-")[0].lower()     # OpenAI takes an ISO-639-1 code
        body, boundary = _multipart(fields, f"audio.{MIME_ALLOW[base]}", base, audio)
        req = HttpRequest("POST", f"https://{HOSTS['openai']}/v1/audio/transcriptions",
                          {"Authorization": f"Bearer {_need_key(key, self.name)}",
                           "Content-Type": f"multipart/form-data; boundary={boundary}"}, body)
        return _text_of(_send(self.transport, self.name, req), self.name, lambda j: j["text"])


@dataclass(frozen=True)
class DeepgramProvider:
    """Deepgram Aura-2 speech and Nova-3 pre-recorded transcription. Every URL carries ``mip_opt_out=true``, which keeps
    the audio and text out of Deepgram's model-improvement programme; no setting turns it off."""

    transport: Transport = field(default=urllib_transport, repr=False)
    stt_model: str = DEEPGRAM_STT_MODEL
    name: str = "deepgram"
    uses: frozenset[str] = frozenset({"tts", "stt"})

    @staticmethod
    def _url(path: str, **query: str) -> str:
        return f"https://{HOSTS['deepgram']}{path}?" + urllib.parse.urlencode({**query, "mip_opt_out": "true"})

    def speak(self, key: str, text: str, voice: str = DEEPGRAM_TTS_VOICE, speed: float = 1.0) -> bytes:
        """mp3 bytes for ``text``. ``voice`` is an Aura-2 model id. ``speed`` is validated and not sent."""
        _need_speech(text, speed)
        req = HttpRequest("POST", self._url("/v1/speak", model=_need_voice(voice), encoding="mp3"),
                          {"Authorization": f"Token {_need_key(key, self.name)}", "Content-Type": "application/json",
                           "Accept": "audio/mpeg"}, json.dumps({"text": text}).encode("utf-8"))
        audio = _send(self.transport, self.name, req)
        if not audio:
            raise VoiceError("unavailable", self.name)
        return audio

    def transcribe(self, key: str, audio: bytes, mime: str, lang: str = "") -> str:
        base = _need_audio(audio, mime)
        query = {"model": self.stt_model, "smart_format": "true"}
        if _need_lang(lang):
            query["language"] = lang
        req = HttpRequest("POST", self._url("/v1/listen", **query),
                          {"Authorization": f"Token {_need_key(key, self.name)}", "Content-Type": base}, audio)
        return _text_of(_send(self.transport, self.name, req), self.name,
                        lambda j: j["results"]["channels"][0]["alternatives"][0]["transcript"])


def _text_of(body: bytes, provider: str, pick: Callable[[dict], object]) -> str:
    """The transcript out of a 2xx JSON body. A body that is not the documented shape is ``unavailable``, never echoed."""
    ok = False
    text: object = None
    try:
        text = pick(json.loads(body.decode("utf-8")))
        ok = isinstance(text, str)
    except Exception:   # noqa: BLE001 - any other shape is no answer
        ok = False
    if not ok:
        raise VoiceError("unavailable", provider)
    return str(text).strip()


def provider_for(name: str, transport: Transport = urllib_transport) -> OpenAIProvider | DeepgramProvider:
    """The adapter for ``name`` (a key of :data:`HOSTS`); a ValueError for any other, so an unknown provider can never be
    reached."""
    if name == "openai":
        return OpenAIProvider(transport=transport)
    if name == "deepgram":
        return DeepgramProvider(transport=transport)
    raise ValueError("unknown voice provider")
