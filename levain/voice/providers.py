"""The premium voice providers: one adapter per provider, each a pure translation between Levain's two calls (speak,
transcribe) and the provider's HTTP API. The trust boundary is here, so what these adapters refuse matters more than what
they send.

What an adapter holds and never holds:

* **The key** arrives as a parameter of :meth:`speak` / :meth:`transcribe`, goes into one request's headers and is not
  kept. It is not an attribute of an adapter, a request's ``repr`` (headers and body are excluded from it), an exception
  or a log line. The method's first act moves it into a :class:`_Key` (fixed ``repr``) and deletes its own name for it;
  the ``_Key`` is emptied when the call ends and the request's ``Authorization`` header is removed as soon as the
  transport returns or raises, so no frame of this module on a :class:`VoiceError`'s (or a ValueError's) traceback
  holds the key, even for a tool that captures frame locals. The CALLER's own frame is the caller's to keep clean. A
  key the provider cannot be sent (empty, or anything but printable ASCII with no space: a control character would
  split a header, a pasted curly quote cannot be encoded) is refused as a rejected key, naming nothing.
* **A provider's response body** is never surfaced. An OpenAI 401 body quotes part of the key it refused (design threat
  T2, still to be confirmed against a real dead key), so :func:`urllib_transport` reads an error body only for a 429,
  and only for its ``error.code`` and ``error.type`` (an empty balance, a spend limit, or a rate limit); the adapter
  drops every non-2xx body before it raises. What the operator sees is :class:`VoiceError`: a fixed code
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
* Deepgram speech, read from Deepgram's pages on 2026-11-05 (the Speak reference
  https://developers.deepgram.com/reference/text-to-speech/speak-request, the guide
  https://developers.deepgram.com/docs/text-to-speech, the output settings
  https://developers.deepgram.com/docs/tts-media-output-settings, the voices https://developers.deepgram.com/docs/tts-models
  and the errors https://developers.deepgram.com/docs/errors): ``POST https://api.deepgram.com/v1/speak``, ``Authorization:
  Token <key>``, a JSON body ``{"text": ...}`` (exactly one of ``text`` or ``url``), the voice as the ``model`` query
  parameter (``aura-2-thalia-en`` is a listed Aura-2 voice), ``encoding=mp3`` valid on REST (the response is ``audio/mpeg``;
  the pages disagree on the default encoding, so mp3 is always asked for by name), at most 2000 characters per request for
  Aura-2 (:data:`DEEPGRAM_INPUT_CAP`; above it Deepgram answers 413, which the adapter refuses before sending), and
  ``mip_opt_out=true`` keeping the request out of the model-improvement programme. Speed is the ``speed`` query parameter,
  0.7 to 1.5 for Aura-2 ("Aura-2: speed runs 0.7 - 1.5", https://developers.deepgram.com/docs/tts-voice-controls; the Speak
  reference above gives the parameter and no range, and says "Not yet supported in all languages", the voice-controls page
  lists Aura-2 speed for English and Spanish); a speed outside that range is refused here before sending, and a speed
  other than 1.0 is sent. The documented error statuses are mapped by status
  alone (:func:`_code_for`); Deepgram's TTS pages show no 402 body, only the general "402 Payment Required: The project
  has insufficient funds". The Nova-3 pre-recorded path (``/v1/listen``), its parameters and the transcript's place in the
  response are from the design, not fetched: UNVERIFIED (and out of scope for the OpenAI TTS shutdown, which does not
  touch transcription). No Deepgram call has been made with a real key (the slice's named residue). Deepgram bills Aura-2
  as one family, so :attr:`DeepgramProvider.tts_model` is the priced family (``aura-2``) and the voice sent as Deepgram's
  ``model`` must be one of that family's voices.
* Inworld speech, read from Inworld's pages on 2026-11-05 (the OpenAI-shaped route
  https://docs.inworld.ai/tts/openai-compatibility.md and its reference
  https://docs.inworld.ai/api-reference/ttsAPI/texttospeech/create-speech.md, the models
  https://docs.inworld.ai/tts/tts-models.md, the key header https://docs.inworld.ai/portal/api-key-auth.md): ``POST
  https://api.inworld.ai/v1/audio/speech`` with an OpenAI-shaped JSON body (``model``, ``input``, ``voice``,
  ``response_format``, ``speed``) and raw audio back, mp3 by default and asked for by name. The key goes in ``Authorization``;
  the route page says ``Bearer`` (what the OpenAI SDKs send) and that ``Basic`` is also accepted, the key page says ``Basic``
  for a direct request and that the same key also works as ``Bearer`` on inference endpoints, so both forms are documented
  for this route and :data:`INWORLD_AUTH_SCHEME` is the one word that picks (UNVERIFIED against a live call). ``model`` and
  ``voice`` must be Inworld ids ("OpenAI model names such as ``tts-1`` return ``400``", "OpenAI voice names such as ``alloy``
  return ``404``"; voice ids are case-sensitive). The Flash model is ``inworld-tts-2-flash``. ``input`` is "up to
  **4,000 characters**" (:data:`INWORLD_INPUT_CAP`) and ``speed`` runs 0.5 to 1.5; a text or a speed outside either is refused
  here before sending. No page lists the built-in voices without an account (the list is the authenticated ``GET
  /tts/v1/voices``), so :data:`INWORLD_TTS_VOICES` holds only the ids a page prints. The documented errors are 400 (a bad
  parameter), 401 and 403 (the key), 402 (``insufficient_quota``: no credits), 404 (the voice), 429 (``rate_limit_error``,
  a rate or a concurrency limit), 5xx; they are mapped by status alone (:func:`_code_for`; a 429 body is read only for the
  OpenAI-format codes, so a body of any other shape is a rate limit), and no spend-limit 429 is documented. The pages do not state the response
  content type, so the adapter checks only that a 2xx body is not empty, as the others do. Inworld's speech-to-text is not
  built (premium dictation is off in Levain 1.0). No Inworld call has been made with a real key (the slice's named
  residue).
* ElevenLabs is not built in this slice.

No claim above about a provider's behaviour is tested against the provider: the fixtures under ``tests/fixtures/voice``
are written in the documented response shape, not recorded from a live call.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from levain.voice.text import API_CAP, CHUNK

HOSTS = {"openai": "api.openai.com", "deepgram": "api.deepgram.com", "inworld": "api.inworld.ai"}
ALLOWED_HOSTS = frozenset(HOSTS.values())

OPENAI_TTS_MODEL = "tts-1"                      # DATA: announced to shut down 2027-01-06 (module docstring)
OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
# Live (streamed) dictation: the LEDGER model each provider's stream is reserved, settled and priced under. It is not
# always the model sent on the wire. Deepgram's live socket sends the same model as its pre-recorded POST ("nova-3",
# DEEPGRAM_STT_MODEL below) but Deepgram bills it at a separate streaming rate, so its ledger key is "nova-3-stream"
# (https://deepgram.com/pricing, read 2026-11-08). Past: a stream sent as gpt-live-transcribe was reserved as gpt-4o-mini-transcribe ($0.003 / min against
# $0.017 / min, 2026-11-07, https://developers.openai.com/api/docs/pricing).
STREAM_STT_MODELS = {"openai": "gpt-live-transcribe", "deepgram": "nova-3-stream"}
OPENAI_DEFAULT_VOICE = "nova"
DEEPGRAM_TTS_MODEL = "aura-2"                   # the priced family (levain.voice.meter.DEFAULT_PRICES)
DEEPGRAM_TTS_VOICE = "aura-2-thalia-en"         # a model id, the default; one of DEEPGRAM_TTS_VOICES
# Every English Aura-2 voice the table "All Available Voices" lists on https://developers.deepgram.com/docs/tts-models (read
# 2026-11-05), by model id; the default is one of them. The other languages' ids on that page (`-es`, `-nl`, ...) are left
# out on purpose: the cockpit's voice picker is English-only. How many there are is asserted by a test, not stated here.
# Sorted, so a reader and a test can compare it by eye. The cockpit's route refuses a voice outside this tuple; the adapter
# refuses only an id that is not an English voice of its model family, so a voice Deepgram adds later needs this tuple
# updated to be OFFERED and nothing else.
DEEPGRAM_TTS_VOICES = (
    "aura-2-amalthea-en", "aura-2-andromeda-en", "aura-2-apollo-en", "aura-2-arcas-en", "aura-2-aries-en",
    "aura-2-asteria-en", "aura-2-athena-en", "aura-2-atlas-en", "aura-2-aurora-en", "aura-2-callista-en",
    "aura-2-cora-en", "aura-2-cordelia-en", "aura-2-delia-en", "aura-2-draco-en", "aura-2-electra-en",
    "aura-2-harmonia-en", "aura-2-helena-en", "aura-2-hera-en", "aura-2-hermes-en", "aura-2-hyperion-en",
    "aura-2-iris-en", "aura-2-janus-en", "aura-2-juno-en", "aura-2-jupiter-en", "aura-2-luna-en",
    "aura-2-mars-en", "aura-2-minerva-en", "aura-2-neptune-en", "aura-2-odysseus-en", "aura-2-ophelia-en",
    "aura-2-orion-en", "aura-2-orpheus-en", "aura-2-pandora-en", "aura-2-phoebe-en", "aura-2-pluto-en",
    "aura-2-saturn-en", "aura-2-selene-en", "aura-2-thalia-en", "aura-2-theia-en", "aura-2-vesta-en",
    "aura-2-zeus-en",
)
DEEPGRAM_STT_MODEL = "nova-3"                   # the wire model, for the pre-recorded POST and the live socket alike
INWORLD_TTS_MODEL = "inworld-tts-2-flash"       # the priced model (levain.voice.meter.DEFAULT_PRICES); the Flash tier
INWORLD_AUTH_SCHEME = "Bearer"                  # "Basic" is the other documented form for the same key (module docstring)
# The built-in voice ids a page of Inworld's docs prints (Dennis: the route page's example; Alex, Ashley and Dennis: the list
# voices reference's example response; Ashley: the quickstart's), sorted and case as printed. The full list of built-in voices
# is behind the authenticated list-voices call, so a voice Inworld has and no page names is not OFFERED here until it is read
# from an account and added to this tuple; the adapter accepts any well-formed id (a custom voice id is one).
INWORLD_TTS_VOICES = ("Alex", "Ashley", "Dennis")
INWORLD_TTS_VOICE = "Dennis"                    # one of INWORLD_TTS_VOICES
# "The text to synthesize, up to 4,000 characters" (the route page and the reference above, read 2026-11-05). The route
# pages leave the unit unstated; the streaming reference counts input and billing in UTF-16 code units, so the cap and the
# meter use :func:`inworld_units` (UNVERIFIED against a live call on THIS route); a text Inworld counts as longer is
# answered 400, which maps to ``rejected``, so a wrong unit still fails closed.
INWORLD_INPUT_CAP = 4000
INWORLD_SPEED_MIN, INWORLD_SPEED_MAX = 0.5, 1.5     # "Values outside 0.5 to 1.5 are rejected" (the reference above)
OPENAI_INPUT_CAP = API_CAP                      # OpenAI's speech input limit, the number the cap was first written for
# Aura-2 and Aura-1: the table "Max Characters" on https://developers.deepgram.com/docs/text-to-speech (read 2026-11-05).
# The unit those pages name is "characters"; whether Deepgram counts code points, bytes or UTF-16 units was not read from
# any page, so len() of the Python string is the unit used here, UNVERIFIED. A text Deepgram counts as longer is answered
# 413, which maps to ``rejected`` (:func:`_code_for`), so the unverified unit fails closed.
DEEPGRAM_INPUT_CAP = 2000
if CHUNK > DEEPGRAM_INPUT_CAP:      # a real check, not an assert: it must hold under ``python -O`` too
    raise ImportError("levain.voice.text.CHUNK is over the smallest voice adapter input cap")

# Seconds. TIMEOUT is urllib's per-socket-operation inactivity timeout (the connect, a send, each blocking recv): a
# provider sending a byte before each one fires never trips it, and a call holds one of the server's two voice slots, so
# the whole exchange also has TOTAL_TIMEOUT, a monotonic wall-clock budget that urllib_transport checks between reads.
# A spent budget is an exchange that failed after the request went out, so it settles like any provider failure. What it
# does not bound: a provider trickling the response HEADERS (urllib's open() returns only after them); a recv blocked
# when the budget runs out waits at most TIMEOUT longer.
TIMEOUT = 30.0
TOTAL_TIMEOUT = 90.0
_READ_CHUNK = 64 * 1024
_monotonic = time.monotonic     # one name for the exchange clock, so a test can drive it
MAX_RESPONSE = 16 * 1024 * 1024                 # a response past this is not read: it is not an answer
MIME_ALLOW = {"audio/webm": "webm", "audio/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"}
SPEED_MIN, SPEED_MAX = 0.25, 4.0                # OpenAI's range
DEEPGRAM_SPEED_MIN, DEEPGRAM_SPEED_MAX = 0.7, 1.5   # Aura-2's own: https://developers.deepgram.com/docs/tts-voice-controls

# The fixed sentences. ``{p}`` is the provider in use. A provider body is never one of them. "rejected" (a 4xx that is
# neither the key nor a limit) has no sentence in the design; this is a placeholder for that gap.
WORDS = {
    "refused": "{p} refused the key. It may have been revoked or mistyped; set it again on the laptop.",
    "out_of_credit": "Your {p} account is out of credit.",
    "spend_limited": "Your {p} spend limit is reached. Raise it on {p}'s site, or wait for it to reset.",
    "rate_limited": "{p} is limiting requests right now; try again in a moment.",
    "unavailable": "{p} did not answer. Nothing was charged that Levain knows of.",
    "rejected": "{p} could not use that request.",
}
NAMES = {"openai": "OpenAI", "deepgram": "Deepgram", "inworld": "Inworld"}


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
    timeout: float = TIMEOUT                        # per socket operation
    total: float = TOTAL_TIMEOUT                    # the whole exchange, wall clock

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


def _read_until(resp: Any, deadline: float, limit: int) -> bytes:
    """Up to ``limit`` bytes of ``resp``, in single reads (``read1`` returns what one recv gave, where ``read(n)`` would
    wait for all ``n``), with the monotonic ``deadline`` checked before each. A spent deadline raises TimeoutError; the
    caller's ``with`` (or ``finally``) closes the connection. One growing buffer, never an object per read: a body
    sent in millions of tiny reads costs about its own size, which ``limit`` bounds."""
    buf = bytearray()
    while len(buf) < limit:
        if _monotonic() >= deadline:
            raise TimeoutError("the voice exchange outlasted its deadline")
        piece = resp.read1(min(_READ_CHUNK, limit - len(buf)))
        if not piece:
            break
        buf += piece
    return bytes(buf)


def urllib_transport(req: HttpRequest) -> HttpResponse:
    """The real transport: https to one of :data:`ALLOWED_HOSTS` only, a redirect answered as its 3xx status and never
    followed, a response read to at most :data:`MAX_RESPONSE` bytes and within ``req.total`` seconds (a spent budget
    raises, and the adapter maps that to ``unavailable``). An error status is a response, not an exception;
    an unreachable host raises, and the adapter maps that to ``unavailable``. An error body is read only for a 429 (its
    ``error.code`` and ``error.type``); any other error status comes back with an empty body, and the error response is always closed."""
    parts = urllib.parse.urlsplit(req.url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password:
        raise ValueError("destination is not an allowed voice provider")
    request = urllib.request.Request(req.url, data=req.body, headers=dict(req.headers), method=req.method)
    deadline = _monotonic() + req.total
    try:
        with _OPENER.open(request, timeout=min(req.timeout, req.total)) as resp:
            body = _read_until(resp, deadline, MAX_RESPONSE + 1)
            status = int(resp.status)
    except urllib.error.HTTPError as err:
        body = b""
        try:
            if err.code == 429:
                body = _read_until(err, deadline, 64 * 1024)
        except Exception:   # noqa: BLE001 - an unreadable error body is an empty one
            body = b""
        finally:
            err.close()
        return HttpResponse(int(err.code), body)
    if len(body) > MAX_RESPONSE:
        raise ValueError("response too large")
    return HttpResponse(status, body)


# OpenAI's 429 billing rows, by ``error.code`` (https://developers.openai.com/api/docs/guides/error-codes, read
# 2026-11-05): "For billing-related errors, inspect error.code to identify the specific cause. The broader error.type
# can still be insufficient_quota." So a code naming a limit the account holder set wins over that broad type, and a
# spend or usage limit is not told as an empty balance (the fix there is a dashboard limit, not money).
_SPEND_LIMIT_CODES = frozenset({"organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                                "organization_usage_limit_exceeded"})
_OUT_OF_CREDIT_CODES = frozenset({"credit_balance_exhausted", "insufficient_quota"})


def _code_of_429(body: bytes) -> str:
    """The :data:`WORDS` code a 429's JSON body names: ``spend_limited`` for a spend or usage limit code,
    ``out_of_credit`` for an empty balance (``error.code`` in :data:`_OUT_OF_CREDIT_CODES`, or ``error.type`` of
    ``insufficient_quota`` with no limit code), else ``rate_limited``. ``error.code`` and ``error.type`` are the only
    things a body is read for, and only as strings: a provider body is untrusted, and any other shape (a list, an
    object, a number) is an ordinary rate limit."""
    try:
        err = json.loads(body[:64 * 1024].decode("utf-8")).get("error")
    except Exception:   # noqa: BLE001 - not JSON, not an object: an ordinary rate limit
        return "rate_limited"
    if not isinstance(err, dict):
        return "rate_limited"
    code, kind = err.get("code"), err.get("type")
    if not isinstance(code, str):
        code = None
    if not isinstance(kind, str):
        kind = None
    if code in _SPEND_LIMIT_CODES:
        return "spend_limited"
    if code in _OUT_OF_CREDIT_CODES or kind == "insufficient_quota":
        return "out_of_credit"
    return "rate_limited"


def _code_for(status: int, body: bytes) -> str | None:
    """The :data:`WORDS` code for a status, None for success."""
    if 200 <= status < 300:
        return None
    if status in (401, 403):
        return "refused"
    if status == 402:
        return "out_of_credit"
    if status == 429:
        return _code_of_429(body)
    if status in (408, 425) or status < 200 or 300 <= status < 400 or status >= 500:
        return "unavailable"     # a timeout, a redirect (never followed), a server fault: the provider did not answer
    return "rejected"


class _Key:
    """One call's key. Its ``repr`` is fixed, it holds nothing unless the key can be sent (printable ASCII, no space),
    and leaving its ``with`` block empties it, so a frame that still names it after the call holds no key."""

    __slots__ = ("_value",)

    def __init__(self, key: object) -> None:
        sendable = isinstance(key, str) and bool(key) and all(32 < ord(c) < 127 for c in key)
        self._value: str | None = key if sendable else None    # type: ignore[assignment]

    def __bool__(self) -> bool:
        return self._value is not None

    def __repr__(self) -> str:
        return "<voice key>"

    def __enter__(self) -> _Key:
        return self

    def __exit__(self, *exc: object) -> None:
        self._value = None

    def header(self, scheme: str) -> str:
        return f"{scheme} {self._value}"


def _exchange(transport: Transport, req: HttpRequest) -> tuple[str | None, bytes]:
    """(the :data:`WORDS` code or None, the 2xx body or b""). Never raises: whatever the transport raises is dropped
    here (it may quote a header), a non-2xx body is dropped here (it may quote the key), and the request's
    ``Authorization`` header is removed the moment the transport is done with it."""
    try:
        resp = transport(req)
    except Exception:   # noqa: BLE001 - timeout, DNS, TLS, a redirect loop: all one fixed answer
        return "unavailable", b""
    finally:
        req.headers.pop("Authorization", None)
    if not isinstance(resp, HttpResponse):
        return "unavailable", b""
    code = _code_for(resp.status, resp.body)
    return code, (resp.body if code is None else b"")


def _send(transport: Transport, provider: str, key: _Key, scheme: str, url: str, headers: dict[str, str],
          body: bytes) -> bytes:
    """The body of a 2xx answer, or a :class:`VoiceError` raised outside any ``except`` (no chained context) from a
    frame that holds no key and no provider body."""
    if not key:
        raise VoiceError("refused", provider)
    req = HttpRequest("POST", url, {"Authorization": key.header(scheme), **headers}, body)
    code, answer = _exchange(transport, req)
    if code is not None:
        raise VoiceError(code, provider)
    return answer


def inworld_units(text: str) -> int:
    """``text`` in UTF-16 code units, the unit Inworld counts its input limit and its billing in (a character outside the
    Basic Multilingual Plane, an emoji, is two): https://docs.inworld.ai/api-reference/ttsAPI/texttospeech/synthesize-speech-stream
    (read by the L3 inworld r1 codex seat, 2026-11-05). A lone surrogate counts as one."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _need_speech(text: str, speed: float, cap: int = API_CAP,
                 speed_range: tuple[float, float] = (SPEED_MIN, SPEED_MAX)) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("nothing to speak")
    if len(text) > cap:
        raise ValueError("text is over the provider's input cap; chunk it first")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not speed_range[0] <= speed <= speed_range[1]:
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
        secret = _Key(key)
        del key                                             # from here only the emptied-on-exit _Key names it
        with secret:
            _need_speech(text, speed, OPENAI_INPUT_CAP)
            body = json.dumps({"model": self.tts_model, "input": text, "voice": _need_voice(voice), "speed": speed,
                               "response_format": "mp3"}).encode("utf-8")
            audio = _send(self.transport, self.name, secret, "Bearer", f"https://{HOSTS['openai']}/v1/audio/speech",
                          {"Content-Type": "application/json", "Accept": "audio/mpeg"}, body)
        if not audio:
            raise VoiceError("unavailable", self.name)      # a 200 with no audio is not an answer
        return audio

    def transcribe(self, key: str, audio: bytes, mime: str, lang: str = "") -> str:
        """The text of ``audio``. An answer with no text is the empty string (the caller says "Heard nothing")."""
        secret = _Key(key)
        del key
        with secret:
            base = _need_audio(audio, mime)
            fields = {"model": self.stt_model, "response_format": "json"}
            if _need_lang(lang):
                fields["language"] = lang.split("-")[0].lower()     # OpenAI takes an ISO-639-1 code
            body, boundary = _multipart(fields, f"audio.{MIME_ALLOW[base]}", base, audio)
            answer = _send(self.transport, self.name, secret, "Bearer",
                           f"https://{HOSTS['openai']}/v1/audio/transcriptions",
                           {"Content-Type": f"multipart/form-data; boundary={boundary}"}, body)
        return _text_of(answer, self.name, lambda j: j["text"])


@dataclass(frozen=True)
class DeepgramProvider:
    """Deepgram Aura-2 speech and Nova-3 pre-recorded transcription. Every URL carries ``mip_opt_out=true``, which keeps
    the audio and text out of Deepgram's model-improvement programme; no setting turns it off."""

    transport: Transport = field(default=urllib_transport, repr=False)
    stt_model: str = DEEPGRAM_STT_MODEL
    name: str = "deepgram"
    uses: frozenset[str] = frozenset({"tts", "stt"})
    tts_model: str = DEEPGRAM_TTS_MODEL      # the priced family; the voice sent is one of its voices. Last, so positional
                                             # construction of the older fields is unchanged

    @staticmethod
    def _url(path: str, **query: str) -> str:
        return f"https://{HOSTS['deepgram']}{path}?" + urllib.parse.urlencode({**query, "mip_opt_out": "true"})

    def speak(self, key: str, text: str, voice: str = DEEPGRAM_TTS_VOICE, speed: float = 1.0) -> bytes:
        """mp3 bytes for ``text`` (at most :data:`DEEPGRAM_INPUT_CAP` characters). ``voice`` is an English voice of
        :attr:`tts_model`'s family (``<tts_model>-<name>-en``, so the family the meter priced is the one billed; another
        language's, another family's or a malformed id is refused before sending). Which voices are OFFERED is the
        cockpit's list (:data:`DEEPGRAM_TTS_VOICES`), checked by its route; this check is the wire's. ``speed`` is
        refused outside Aura-2's 0.7-1.5 and sent as the ``speed`` query parameter unless it is 1.0 (Deepgram's default)."""
        secret = _Key(key)
        del key
        with secret:
            _need_speech(text, speed, DEEPGRAM_INPUT_CAP, (DEEPGRAM_SPEED_MIN, DEEPGRAM_SPEED_MAX))
            voice = _need_voice(voice)
            if re.fullmatch(re.escape(self.tts_model) + r"-[a-z]+-en", voice) is None:
                raise ValueError("voice is not an English voice of the provider's model family")
            query = {"model": voice, "encoding": "mp3"}
            if speed != 1.0:
                query["speed"] = repr(float(speed))     # round-trips: a six-digit format would send 1.0000001 as the default
            audio = _send(self.transport, self.name, secret, "Token",
                          self._url("/v1/speak", **query),
                          {"Content-Type": "application/json", "Accept": "audio/mpeg"},
                          json.dumps({"text": text}).encode("utf-8"))
        if not audio:
            raise VoiceError("unavailable", self.name)
        return audio

    def transcribe(self, key: str, audio: bytes, mime: str, lang: str = "") -> str:
        secret = _Key(key)
        del key
        with secret:
            base = _need_audio(audio, mime)
            query = {"model": self.stt_model, "smart_format": "true"}
            if _need_lang(lang):
                query["language"] = lang
            answer = _send(self.transport, self.name, secret, "Token", self._url("/v1/listen", **query),
                           {"Content-Type": base}, audio)
        return _text_of(answer, self.name, lambda j: j["results"]["channels"][0]["alternatives"][0]["transcript"])


@dataclass(frozen=True)
class InworldProvider:
    """Inworld speech (mp3) through its OpenAI-shaped route. Speech only: ``uses`` has no ``stt`` and there is no
    ``transcribe``. ``tts_model`` is data, see the module docstring."""

    transport: Transport = field(default=urllib_transport, repr=False)
    tts_model: str = INWORLD_TTS_MODEL
    name: str = "inworld"
    uses: frozenset[str] = frozenset({"tts"})

    def speak(self, key: str, text: str, voice: str = INWORLD_TTS_VOICE, speed: float = 1.0) -> bytes:
        """mp3 bytes for ``text`` (at most :data:`INWORLD_INPUT_CAP` UTF-16 code units, see :func:`inworld_units`) in ``voice`` at ``speed`` (0.5 to 1.5).
        ``voice`` is any well-formed id; which are OFFERED is the cockpit's list (:data:`INWORLD_TTS_VOICES`), checked by
        its route. The key is sent in the ``Authorization`` header only."""
        secret = _Key(key)
        del key                                             # from here only the emptied-on-exit _Key names it
        with secret:
            _need_speech(text, speed, INWORLD_INPUT_CAP, (INWORLD_SPEED_MIN, INWORLD_SPEED_MAX))
            if inworld_units(text) > INWORLD_INPUT_CAP:
                raise ValueError("text is over the provider's input cap; chunk it first")
            body = json.dumps({"model": self.tts_model, "input": text, "voice": _need_voice(voice), "speed": speed,
                               "response_format": "mp3"}).encode("utf-8")
            audio = _send(self.transport, self.name, secret, INWORLD_AUTH_SCHEME,
                          f"https://{HOSTS['inworld']}/v1/audio/speech",
                          {"Content-Type": "application/json", "Accept": "audio/mpeg"}, body)
        if not audio:
            raise VoiceError("unavailable", self.name)      # a 200 with no audio is not an answer
        return audio


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


def provider_for(name: str, transport: Transport = urllib_transport) -> OpenAIProvider | DeepgramProvider | InworldProvider:
    """The adapter for ``name`` (a key of :data:`HOSTS`); a ValueError for any other, so an unknown provider can never be
    reached."""
    if name == "openai":
        return OpenAIProvider(transport=transport)
    if name == "deepgram":
        return DeepgramProvider(transport=transport)
    if name == "inworld":
        return InworldProvider(transport=transport)
    raise ValueError("unknown voice provider")
