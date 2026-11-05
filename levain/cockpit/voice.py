"""Premium voice, the routes (voice V1-4; design ``project_memory/1105-7/DESIGN_voice_v14.md``).

Three routes, served only beside a room host and a broker: ``GET /cockpit/voice.json`` (which providers have a key,
the caps and the estimated spend), ``POST /cockpit/voice/speak`` (one chunk of a finished room reply, as mp3) and
``POST /cockpit/voice/transcribe?provider=`` (an audio upload, as text). This module is the orchestration with no socket:
each function returns ``(status, content type, headers, body bytes)``; ``levain.web_server`` runs the gates (design
section 2) and writes the answer.

The call sequence is the design's section 3 and its order is the point:

1. validate the request (nothing read or written on a refusal);
2. (speak) the room thread must be the open room, and the words are the RECORD's reply, never the page's;
   then, both routes: the consent hooks must name ``data_root`` as their one cockpit (:func:`revoke_reaches`), so the
   grant page's revoke reaches the key about to be read; else ``revoke_unreachable``, nothing reserved or read;
3. ``meter.reserve`` (a cap, an unpriced model or a meter that cannot be used refuses here, and no key is read);
4. ``custody.read_key`` (a fresh verified replay, then the keychain): a :class:`~levain.voice.custody.Secret`;
5. the adapter call, with ``secret.reveal()`` written INLINE in its argument list, so no name in any frame of this
   module ever holds the key as a str (the adapter's own ``_Key`` takes it from there);
6. settle or release, in a ``finally`` around 5. Release is for a call that never went out: a key that could not be
   read (4, :class:`~levain.voice.custody.VoiceKeyUnavailable`) or an adapter ``ValueError`` (raised before it sends).
   Every other outcome once the adapter was called settles, a provider refusal and an unexpected exception included,
   because the provider may have billed it. A reader fault custody does not name (4 raising anything else) is neither:
   the row stays ``reserved``, which counts, so the estimate errs high.

What the operator sees is fixed words from :data:`WORDS` (or :data:`levain.voice.providers.WORDS` by code): never a
provider body, a status line, an exception's text or a record field. Nothing here logs an exception's text or its
traceback; a log line names a type at most.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.parse
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from levain.authority import voice_key_for
from levain.cockpit import room as cockpit_room
from levain.voice import custody
from levain.voice import meter as vmeter
from levain.voice import providers as vprov
from levain.voice.text import reply_text, speakable

_log = logging.getLogger(__name__)

VOICE_JSON = "/cockpit/voice.json"
SPEAK_PATH = "/cockpit/voice/speak"
TRANSCRIBE_PATH = "/cockpit/voice/transcribe"
VOICE_PATHS = (SPEAK_PATH, TRANSCRIBE_PATH)
VOICE_INFLIGHT = 2                      # voice POSTs in flight at once (the server's voice_gate), apart from request_gate
SPEAK_MAX = 4 * 1024                    # a speak body is a few ids and numbers; the words come from the record
UPLOAD_MAX = 4 * 1024 * 1024            # the transcribe body cap, checked on the declared length before a byte is read
# Estimated seconds of an upload: its bytes at a 32 kbps floor (4000 bytes a second). The browser picks the bitrate, so
# the length cannot be known from the size (design F2); a floor this low prices the common Opus and AAC rates high.
UPLOAD_BYTES_PER_SECOND = 4000
LEDGER_NAME = vmeter.DB_NAME            # the ledger file, in the store directory beside dispatch.db (parent design 6)
SPEAK_KEYS = frozenset({"room_thread_id", "reply_gseq", "chunk", "provider", "voice", "speed"})
LAST_READ_STATES = frozenset({"keychain_unreadable", "unusable", "refused", "authority_unreadable"})


@dataclass(frozen=True)
class ProviderData:
    """What the routes serve for one provider. Data, beside the adapters' own constants: a model or a voice list
    changing is an edit here (OpenAI's tts-1 shuts down 2027-01-06; its successor is a research slice, Q-P4)."""
    name: str
    tts_model: str
    stt_model: str
    voices: tuple[str, ...]
    default_voice: str
    speed_min: float
    speed_max: float
    retention: str


PROVIDERS: dict[str, ProviderData] = {
    # tts-1's nine voices: OpenAI's speech reference (read by the design lane, 2026-11-05) lists 13 built-in voices
    # without saying which tts-1 takes, so this list is inferred and is data. The speed range is the adapter's own.
    "openai": ProviderData("openai", vprov.OPENAI_TTS_MODEL, vprov.OPENAI_STT_MODEL,
                           ("alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"),
                           vprov.OPENAI_DEFAULT_VOICE, vprov.SPEED_MIN, vprov.SPEED_MAX,
                           "Speech: abuse logs kept up to 30 days. Transcription: no abuse-monitoring retention. "
                           "Not used to train unless opted in."),
    # one voice, speed fixed: the adapter sends no speed (providers.py), so no other speed may be asked for
    "deepgram": ProviderData("deepgram", vprov.DEEPGRAM_TTS_MODEL, vprov.DEEPGRAM_STT_MODEL,
                             (vprov.DEEPGRAM_TTS_VOICE,), vprov.DEEPGRAM_TTS_VOICE, 1.0, 1.0,
                             "Every call opts out of Deepgram's model-improvement programme (mip_opt_out). Retention "
                             "periods are not stated on Deepgram's pages."),
}
# a record kind exists for these (authority.VOICE_KEY_PROVIDERS) but no adapter does
UNSUPPORTED = {"elevenlabs": "ElevenLabs"}

# The fixed words. ``{p}`` is the provider's display name (providers.NAMES), ``{cap}`` a dollar amount.
WORDS = {
    "no_owner": "Premium voice needs a named credential.",
    "too_long": "That recording is too long to transcribe (the limit is two minutes).",
    "voice_busy": "Voice is busy; try again in a moment.",
    "unsupported_media_type": "Send the recording as audio/webm, audio/mp4, audio/mpeg or audio/wav.",
    "not_found": "Premium voice is not served here.",
    "bad_json": "The request is not valid JSON.",
    "bad_keys": "A speak request names room_thread_id, reply_gseq, chunk, provider, voice and speed, and nothing else.",
    "bad_provider": "That is not a voice provider.",
    "provider_unsupported": "{p} is not supported in this version.",
    "bad_reply": "reply_gseq names the reply by its number.",
    "bad_chunk": "chunk is a whole number, 0 or more.",
    "bad_voice": "That voice is not offered for {p}.",
    "bad_speed": "That speed is outside what {p} accepts.",
    "bad_query": "Name the provider, and only the provider, in the query.",
    "bad_room": "The request names the room thread the page shows (room_thread_id).",
    "no_room": "No conversation is recorded.",
    "room_rotated": "This conversation was replaced; nothing more is read from it.",
    "room_unreadable": "The room record could not be read.",
    "not_a_reply": "That reply is not finished, or is not in this conversation.",
    "nothing_to_read": "That reply has nothing to read aloud.",
    "no_such_chunk": "That part of the reply does not exist.",
    "no_audio": "No recording was sent.",
    "meter_unavailable": "Levain could not check the voice budget, so premium voice is off for now.",
    "month_cap": "Premium voice is paused: this month's {cap} cap is reached. Reading uses the on-device voice.",
    "day_cap": "Premium voice is paused until tomorrow: today's {cap} cap is reached. Reading uses the on-device voice.",
    "voice_unpriced": "This voice model has no price in Levain, so it is not used.",
    "no_key": "No {p} key is set on this Levain. Reading uses the on-device voice.",
    "no_tts_key": "The {p} key is not set for reading aloud.",
    "no_stt_key": "The {p} key is not set for dictation.",
    "authority_unreadable": "Levain could not verify its authority log just now; premium voice is off until it can.",
    "revoke_unreachable": ("Premium voice is off: the phone's grant page revokes keys in a different cockpit from the "
                           "one this Levain reads voice keys from."),
    "keychain_unreadable": "Levain could not read the key from this Mac's keychain (it may be locked).",
    "key_unusable": "The keychain item for {p} does not hold a usable key; set it again on the laptop.",
    "bad_call": "Premium voice could not use that request.",
    "internal": "Premium voice failed; reading uses the on-device voice.",
    "unreadable": "Premium voice settings could not be read.",
}

# A provider failure (providers.VoiceError.code) as (HTTP status, error name). Its words are providers.WORDS[code].
PROVIDER_FAILURES = {
    "refused": (502, "provider_refused"),
    "out_of_credit": (402, "provider_out_of_credit"),
    "spend_limited": (402, "provider_spend_limit"),
    "rate_limited": (503, "provider_rate_limited"),
    "unavailable": (504, "provider_unavailable"),
    "rejected": (502, "provider_rejected"),
}

# A key that could not be read (custody.VoiceKeyUnavailable.code) as (HTTP status, error name, words key).
# ``use_not_granted`` is answered by use: speak's no_key words differ from transcribe's.
KEY_FAILURES = {
    "not_set": (409, "no_key", "no_key"),
    "authority_unreadable": (503, "authority_unreadable", "authority_unreadable"),
    "keychain_unreadable": (503, "keychain_unreadable", "keychain_unreadable"),
    "unusable": (409, "key_unusable", "key_unusable"),
}


class Answer(NamedTuple):
    status: int
    content_type: str
    headers: list[tuple[str, str]]
    body: bytes


_JSON = "application/json; charset=utf-8"


def _name(provider: str) -> str:
    return vprov.NAMES.get(provider) or UNSUPPORTED.get(provider) or "The voice provider"


def _json(status: int, body: dict[str, Any], headers: list[tuple[str, str]] | None = None) -> Answer:
    return Answer(status, _JSON, list(headers or ()), json.dumps(body).encode("utf-8"))


def refusal(status: int, error: str, words: str, *, provider: str = "", fallback: bool = False,
            **extra: Any) -> Answer:
    """A refusal in fixed words: ``words`` is a key of :data:`WORDS`, formatted with the provider's display name."""
    body: dict[str, Any] = {"error": error, "message": WORDS[words].format(p=_name(provider), cap=extra.pop("cap", ""))}
    if fallback:
        body["fallback"] = "on_device"
    body.update(extra)
    return _json(status, body)


def internal_answer() -> Answer:
    """The one answer for something that broke where no other answer was decided (the server's outer catch)."""
    return refusal(500, "internal", "internal", fallback=True)


def _dollars(micros: int) -> str:
    """A cap in words: ``$2`` for whole dollars, else ``$2.50`` (rounded up to the cent)."""
    cents = -(-micros // 10_000)
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents // 100}.{cents % 100:02d}"


def _usd(micros: int) -> str:
    """Micro-dollars as a dollar string, rounded UP to the cent, so a shown spend never reads low."""
    cents = -(-micros // 10_000)
    return f"{cents // 100}.{cents % 100:02d}"


def upload_units(n_bytes: int) -> int:
    """Estimated seconds of an ``n_bytes`` upload, at the :data:`UPLOAD_BYTES_PER_SECOND` floor, rounded up."""
    return math.ceil(n_bytes / UPLOAD_BYTES_PER_SECOND)


def base_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


# --- the read ---------------------------------------------------------------------------------------------------------

def serve_voice(data_root: Path | str, meter: vmeter.Meter, *, last_read: MutableMapping[str, str | None]
                ) -> dict[str, Any]:
    """The body of ``GET /cockpit/voice.json``. Whether a provider has a key comes from ONE verified replay of the
    authority log at ``data_root``; the keychain is never read here (Q-D3), so a page's poll never reaches it. Only a
    record's id and uses are shown (no keychain names, no part of a value). ``last_read`` is the outcome of this
    process's last key read or provider call per provider, kept in memory only. The spend is read without creating
    the ledger."""
    try:
        grants = custody.authority_for(data_root)().policy.grants
        authority = "ok"
    except Exception:       # noqa: BLE001 - any failure is "the log could not be verified now", said, not raised
        grants, authority = {}, "unreadable"
    rows = []
    for name, data in PROVIDERS.items():
        record = voice_key_for(grants, name) if grants else None
        rows.append({"name": name,
                     "uses": sorted(record["uses"]) if record is not None else [],
                     "key": "set" if record is not None else "not_set",
                     "record_id": record["id"] if record is not None else None,
                     "last_read": last_read.get(name),
                     "voices": list(data.voices), "default_voice": data.default_voice, "tts_model": data.tts_model,
                     "speed": [data.speed_min, data.speed_max], "retention": data.retention})
    caps = meter.caps
    body: dict[str, Any] = {"providers": rows, "prices_as_of": meter.prices.as_of, "estimate": True,
                            "authority": authority}
    try:
        spend = meter.spent_if_present()
        body["meter"] = "ok"
        body["caps"] = {"month_usd": _usd(caps.month_micros), "day_usd": _usd(caps.day_micros),
                        "spent_month_est": _usd(spend.month_micros), "spent_day_est": _usd(spend.day_micros),
                        "month_start": spend.month_start, "day_start": spend.day_start}
    except vmeter.MeterError:
        body["meter"] = "unreadable"
        body["caps"] = {"month_usd": _usd(caps.month_micros), "day_usd": _usd(caps.day_micros),
                        "spent_month_est": None, "spent_day_est": None, "month_start": None, "day_start": None}
    return body


# --- the shared tail: reserve, read the key, call, settle or release ---------------------------------------------------

def _refuse_check(check: vmeter.Check, provider: str) -> Answer:
    if check.reason == vmeter.MONTH_CAP:
        return refusal(402, "month_cap", "month_cap", fallback=True, cap=_dollars(check.caps.month_micros))
    if check.reason == vmeter.DAY_CAP:
        return refusal(402, "day_cap", "day_cap", fallback=True, cap=_dollars(check.caps.day_micros))
    return refusal(503, "voice_unpriced", "voice_unpriced", provider=provider, fallback=True)


def _key_refusal(code: str, provider: str, use: str) -> Answer:
    if code == "use_not_granted":
        return refusal(409, "no_key" if use == "tts" else "no_stt_key", "no_tts_key" if use == "tts" else "no_stt_key",
                       provider=provider, fallback=True)
    status, error, words = KEY_FAILURES.get(code, KEY_FAILURES["not_set"])
    return refusal(status, error, words, provider=provider, fallback=True)


def _end(meter: vmeter.Meter, reservation_id: int, release: bool) -> None:
    """Settle or release one reservation. A meter that fails here leaves the row ``reserved``, which still counts, so
    the estimate errs high; the provider's outcome is still answered. One fixed log line, the type name only."""
    failed = None
    try:
        if release:
            meter.release(reservation_id)
        else:
            meter.settle(reservation_id)
    except Exception as exc:     # noqa: BLE001 - the meter's own failure never replaces the provider's answer
        failed = type(exc).__name__
    if failed is not None:
        _log.warning("voice: a reservation could not be %s (%s); it stays counted",
                     "released" if release else "settled", failed)


def revoke_reaches(directory: Path | str, data_root: Path | str) -> bool:
    """Whether a revoke from the grant page reaches the voice keys at ``data_root``: the consent hooks of the store
    directory ``directory`` name exactly one cockpit (what :func:`levain.consent.sign.sign_root` takes, so what the
    grant routes and ``grants.json`` act on) and it is ``data_root``. Re-read on every call: the hook file can change
    while the server runs. Any fault reading it is False."""
    from levain.consent.answering import hook_roots

    try:
        roots = hook_roots(Path(directory))
        return len(roots) == 1 and roots[0].resolve() == Path(data_root).resolve()
    except Exception:            # noqa: BLE001 - a hook that cannot be read names no cockpit: fail closed
        return False


def _call(provider: str, use: str, units: int, data_root: Path | str, *, directory: Path | str, meter: vmeter.Meter,
          read_key: Callable[..., Any], last_read: MutableMapping[str, str | None],
          send: Callable[[Any], Any]) -> tuple[Any, Answer | None]:
    """Steps 3 to 6 of the module docstring for one call, after the revoke-reach check (:func:`revoke_reaches`): a
    key whose revoke from the phone could not reach it is never read, and nothing is reserved. ``send(secret)`` makes
    the adapter call and must write ``secret.reveal()`` inline in the adapter's argument list. Returns
    ``(result, None)`` or ``(None, refusal)``."""
    if not revoke_reaches(directory, data_root):
        return None, refusal(503, "revoke_unreachable", "revoke_unreachable", fallback=True)
    model = PROVIDERS[provider].tts_model if use == "tts" else PROVIDERS[provider].stt_model
    try:
        check = meter.reserve(provider, use, model, units)
    except vmeter.MeterError:
        return None, refusal(503, "meter_unavailable", "meter_unavailable", fallback=True)
    if not check.allowed or check.reservation_id is None:
        return None, _refuse_check(check, provider)
    reservation = check.reservation_id
    key_failure = None
    try:
        secret = read_key(provider, use, authority=custody.authority_for(data_root))
    except custody.VoiceKeyUnavailable as exc:
        key_failure = exc.code
    except Exception:            # noqa: BLE001 - a reader fault custody does not name: no call went out, and the
        # reservation is neither released nor settled, so it stays counted (the meter errs high, never low)
        key_failure = "unexpected"
    if key_failure == "unexpected":
        return None, internal_answer()
    if key_failure is not None:
        # no request went out: the reservation is released (counts nothing), and the page reads on device
        _end(meter, reservation, release=True)
        if key_failure in LAST_READ_STATES:
            last_read[provider] = key_failure
        return None, _key_refusal(key_failure, provider, use)
    # From here the adapter is called, so the reservation is settled on every outcome but one: an adapter ValueError,
    # which every adapter raises before it sends (the route's own checks are meant to make it unreachable), is released.
    release = False
    failure = None
    result = None
    try:
        try:
            result = send(secret)
        finally:
            del secret
    except vprov.VoiceError as exc:
        failure = exc.code
    except ValueError:
        release, failure = True, "bad_call"
    except Exception:            # noqa: BLE001 - it may have been billed: settled, answered in fixed words
        failure = "internal"
    finally:
        _end(meter, reservation, release=release)
    if failure is None:
        last_read[provider] = None
        return result, None
    if failure in LAST_READ_STATES:
        last_read[provider] = failure
    if failure == "bad_call":
        return None, refusal(500, "internal", "bad_call")
    if failure == "internal":
        return None, internal_answer()
    status, error = PROVIDER_FAILURES.get(failure, (502, "provider_rejected"))
    words = vprov.WORDS.get(failure, vprov.WORDS["rejected"]).format(p=_name(provider))
    return None, _json(status, {"error": error, "message": words, "fallback": "on_device"})


# --- speak ------------------------------------------------------------------------------------------------------------

def _number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _parse_speak(raw: bytes) -> tuple[dict[str, Any] | None, Answer | None]:
    try:
        req = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, refusal(400, "bad_json", "bad_json")
    if not isinstance(req, dict) or set(req) != SPEAK_KEYS:
        return None, refusal(400, "bad_request", "bad_keys")
    provider = req["provider"]
    if isinstance(provider, str) and provider in UNSUPPORTED:
        return None, refusal(409, "provider_unsupported", "provider_unsupported", provider=provider)
    if not isinstance(provider, str) or provider not in PROVIDERS:
        return None, refusal(400, "bad_request", "bad_provider")
    data = PROVIDERS[provider]
    if isinstance(req["reply_gseq"], bool) or not isinstance(req["reply_gseq"], int):
        return None, refusal(400, "bad_request", "bad_reply")
    chunk = req["chunk"]
    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 0:
        return None, refusal(400, "bad_request", "bad_chunk")
    if not isinstance(req["voice"], str) or req["voice"] not in data.voices:
        return None, refusal(400, "bad_request", "bad_voice", provider=provider)
    speed = req["speed"]
    if not _number(speed) or not data.speed_min <= speed <= data.speed_max:
        return None, refusal(400, "bad_request", "bad_speed", provider=provider)
    return req, None


def speak(directory: Path | str, data_root: Path | str, service: Any, raw: bytes, *, meter: vmeter.Meter,
          transport: vprov.Transport, read_key: Callable[..., Any],
          last_read: MutableMapping[str, str | None]) -> Answer:
    """``POST /cockpit/voice/speak``: chunk ``chunk`` of the room reply that ended at ``reply_gseq``, as mp3. The words
    are only the record's (``reply_text`` over the open room's stream, then ``speakable``); a body naming any other key
    is refused."""
    req, refused = _parse_speak(raw)
    if refused is not None or req is None:
        return refused or internal_answer()
    provider = req["provider"]
    thread, need = cockpit_room._need_room(directory, req)
    if need is not None:
        status = need[0]
        if status == 409:
            return refusal(409, "room_rotated", "room_rotated")
        if status == 404:
            return refusal(404, "not_found", "no_room")
        return refusal(400, "bad_request", "bad_room")
    try:
        room = cockpit_room.room_read(directory, service)
    except Exception:            # noqa: BLE001 - a record that cannot be read is fixed words, never its detail
        return refusal(500, "internal", "room_unreadable")
    if room.get("room_thread") != thread:       # rotated between the check and the read
        return refusal(409, "room_rotated", "room_rotated")
    text = reply_text(room.get("stream") or [], req["reply_gseq"])
    if text is None:
        return refusal(404, "not_a_reply", "not_a_reply")
    chunks = speakable(text)
    if not chunks:
        return refusal(404, "nothing_to_read", "nothing_to_read")
    if not req["chunk"] < len(chunks):
        return refusal(404, "no_such_chunk", "no_such_chunk", chunks=len(chunks))
    words = chunks[req["chunk"]]
    adapter = vprov.provider_for(provider, transport)
    voice, speed = req["voice"], req["speed"]
    audio, refused = _call(provider, "tts", len(words), data_root, directory=directory, meter=meter,
                           read_key=read_key, last_read=last_read,
                           send=lambda secret: adapter.speak(secret.reveal(), words, voice, speed))
    if refused is not None:
        return refused
    return Answer(200, "audio/mpeg", [("X-Voice-Chunks", str(len(chunks))), ("X-Voice-Chunk", str(req["chunk"]))],
                  bytes(audio))


# --- transcribe -------------------------------------------------------------------------------------------------------

def _query_provider(query: str) -> tuple[str | None, Answer | None]:
    try:
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None, refusal(400, "bad_request", "bad_query")
    if len(pairs) != 1 or pairs[0][0] != "provider":
        return None, refusal(400, "bad_request", "bad_query")
    provider = pairs[0][1]
    if provider in UNSUPPORTED:
        return None, refusal(409, "provider_unsupported", "provider_unsupported", provider=provider)
    if provider not in PROVIDERS:
        return None, refusal(400, "bad_request", "bad_provider")
    return provider, None


def transcribe(data_root: Path | str, query: str, content_type: str, audio: bytes, *, directory: Path | str,
               meter: vmeter.Meter, transport: vprov.Transport, read_key: Callable[..., Any],
               last_read: MutableMapping[str, str | None]) -> Answer:
    """``POST /cockpit/voice/transcribe?provider=``: ``{"text": ...}`` for the audio body. The audio is never written
    or logged; the ledger holds the estimated seconds only (:func:`upload_units`). ``directory`` is the store
    directory, whose consent hooks :func:`revoke_reaches` reads."""
    provider, refused = _query_provider(query)
    if refused is not None or provider is None:
        return refused or internal_answer()
    mime = base_type(content_type)
    if mime not in vprov.MIME_ALLOW:
        return refusal(415, "unsupported_media_type", "unsupported_media_type")
    if not audio:
        return refusal(400, "no_audio", "no_audio")
    adapter = vprov.provider_for(provider, transport)
    text, refused = _call(provider, "stt", upload_units(len(audio)), data_root, directory=directory, meter=meter,
                          read_key=read_key, last_read=last_read,
                          send=lambda secret: adapter.transcribe(secret.reveal(), audio, mime, ""))
    del audio
    if refused is not None:
        return refused
    return _json(200, {"text": text if isinstance(text, str) else ""})
