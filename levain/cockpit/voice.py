"""Premium voice, the routes (voice V1-4; design ``project_memory/1105-7/DESIGN_voice_v14.md``).

Three routes, served only beside a room host and a broker: ``GET /cockpit/voice.json`` (which providers have a key,
the caps and the estimated spend), ``POST /cockpit/voice/speak`` (one chunk of a finished room reply, as mp3) and
``POST /cockpit/voice/transcribe?provider=`` (an audio upload, as text). This module is the orchestration with no socket:
``speak`` and ``transcribe`` return an :class:`Answer` (status, content type, headers, body bytes) and ``serve_voice``
returns the ``voice.json`` body as a dict; ``levain.web_server`` runs the gates (design section 2) and writes the
answer.

Premium dictation (the transcribe route, and the ``stt`` use in ``voice.json``) is OFF in Levain 1.0 (Phill 11-05): the
server refuses the transcribe POST with :data:`WORDS` ``premium_stt_off`` before it reads a key, reserves a meter row or
reads a body byte, unless it was started with the hidden ``--premium-stt`` switch of ``levain serve``. The code below is kept whole
for a later release; the switch is the only thing that gates it.

The call sequence is the design's section 3 and its order is the point:

1. validate the request (nothing read or written on a refusal);
2. (speak) the room thread must be the open room, and the words are the RECORD's reply, never the page's;
   then, both routes: when the phone's grant routes are served, the consent hooks of THEIR queue must name
   ``data_root`` as their one cockpit (:func:`revoke_refusal`), so the grant page's revoke reaches the key about to be
   read (with no grant route served the revoke is ``levain voice key revoke``, allowed only while the replayed log
   holds an unrevoked laptop key that can sign it); else ``revoke_unreachable`` (or ``revoke_unchecked`` when that
   could not be checked), nothing reserved or read;
3. ``meter.reserve`` (a cap, an unpriced model or a meter that cannot be used refuses here, and no key is read);
4. ``custody.read_key`` (a fresh verified replay, then the keychain): a :class:`~levain.voice.custody.Secret`;
5. the adapter call, with ``secret.reveal()`` written INLINE in its argument list, so no name in any frame of this
   module ever holds the key as a str (the adapter's own ``_Key`` takes it from there);
6. settle or release, in a ``finally`` around 5. Release is for a call that never went out: a key that could not be
   read (4: :class:`~levain.voice.custody.VoiceKeyUnavailable`, or any other fault of the reader, which is logged by
   its type name) or an adapter ``ValueError`` (raised before it sends). Every other outcome once the adapter was
   called settles, a provider refusal and an unexpected exception included, because the provider may have billed it.
   A row left ``reserved`` counts toward the caps and nothing reaps it, so no path may leave one.

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
from datetime import date, datetime, timezone
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
# One wall-clock budget, in seconds, for reading a voice POST's body while its slot is held. The server's socket timeout
# is per recv, so on its own a client sending a byte before each one fires could keep a slot for as long as it liked.
BODY_DEADLINE_S = 20.0
# Estimated seconds of an upload: its bytes at a 32 kbps floor (4000 bytes a second). The browser picks the bitrate, so
# the length cannot be known from the size (design F2); a floor this low prices the common Opus and AAC rates high.
UPLOAD_BYTES_PER_SECOND = 4000
LEDGER_NAME = vmeter.DB_NAME            # the ledger file, in the store directory beside dispatch.db (parent design 6)
SPEAK_KEYS = frozenset({"room_thread_id", "reply_gseq", "chunk", "provider", "voice", "speed"})
# The settings block's premium test: no room, no reply. The words are THIS constant (the page sends none), so the route
# still never speaks a sentence the page chose.
SAMPLE_KEYS = frozenset({"sample", "provider", "voice", "speed"})
SAMPLE_WORDS = "This is how your room will sound."
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
    # the retention words with premium dictation off (an empty string: ``retention`` already says nothing of it)
    retention_speech: str = ""


# The order of this dict is the order ``voice.json`` lists providers in, and the FIRST is the premium default (Phill 11-05:
# Inworld TTS-2 Flash, np-c290888c). It is the one place that says so; a page that picks a provider takes the first row.
PROVIDERS: dict[str, ProviderData] = {
    # Inworld speech only (no transcription: stt_model is empty, so no dictation call can be priced or sent). The voices are
    # the ids Inworld's docs print, since the full list is behind an authenticated call (vprov.INWORLD_TTS_VOICES). The
    # retention words are what the pages say and do not say, read 2026-11-05.
    "inworld": ProviderData("inworld", vprov.INWORLD_TTS_MODEL, "",
                            vprov.INWORLD_TTS_VOICES, vprov.INWORLD_TTS_VOICE,
                            vprov.INWORLD_SPEED_MIN, vprov.INWORLD_SPEED_MAX,
                            "Inworld's pages do not say how long text sent on its pay-as-you-go plan is kept, and its "
                            "privacy notice says it may use information to train AI models. Which plans can turn on "
                            "zero data retention is Inworld's to say: https://inworld.ai/pricing. US servers by default."),
    # tts-1's nine voices: OpenAI's speech reference (read by the design lane, 2026-11-05) lists 13 built-in voices
    # without saying which tts-1 takes, so this list is inferred and is data. The speed range is the adapter's own.
    "openai": ProviderData("openai", vprov.OPENAI_TTS_MODEL, vprov.OPENAI_STT_MODEL,
                           ("alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"),
                           vprov.OPENAI_DEFAULT_VOICE, vprov.SPEED_MIN, vprov.SPEED_MAX,
                           "Speech: abuse logs kept up to 30 days. Transcription: no abuse-monitoring retention. "
                           "Not used to train unless opted in.",
                           "Speech: abuse logs kept up to 30 days. Not used to train unless opted in."),
    # the English Aura-2 voices Deepgram documents (vprov.DEEPGRAM_TTS_VOICES); the speed range is the adapter's own
    # (Aura-2's), and the adapter sends a speed other than 1.0
    "deepgram": ProviderData("deepgram", vprov.DEEPGRAM_TTS_MODEL, vprov.DEEPGRAM_STT_MODEL,
                             vprov.DEEPGRAM_TTS_VOICES, vprov.DEEPGRAM_TTS_VOICE,
                             vprov.DEEPGRAM_SPEED_MIN, vprov.DEEPGRAM_SPEED_MAX,
                             "Every call opts out of Deepgram's model-improvement programme (mip_opt_out). Retention "
                             "periods are not stated on Deepgram's pages."),
}
# a record kind exists for these (authority.VOICE_KEY_PROVIDERS) but no adapter does
UNSUPPORTED = {"elevenlabs": "ElevenLabs"}

# OpenAI announced that tts-1 shuts down on this date (Phill 11-05: OpenAI stays selectable only until then). On and after
# it voice.json does not list openai and a call naming it is refused; before it nothing changes. The clock is a seam so a
# test can set it; ``offered`` is the one function every route asks, so the listing and the refusals cannot disagree.
OPENAI_OFFERED_UNTIL = date(2027, 1, 6)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def offered() -> dict[str, ProviderData]:
    """The providers the routes serve today: :data:`PROVIDERS` in its order, without OpenAI from
    :data:`OPENAI_OFFERED_UNTIL` on."""
    cutoff = _today() >= OPENAI_OFFERED_UNTIL
    return {name: data for name, data in PROVIDERS.items() if not (cutoff and name == "openai")}

# The fixed words. ``{p}`` is the provider's display name (providers.NAMES), ``{cap}`` a dollar amount.
WORDS = {
    "no_owner": "Premium voice needs a named credential.",
    "too_long": "That recording is too large to transcribe.",
    "voice_busy": "Voice is busy; try again in a moment.",
    "body_timeout": "The request did not arrive in time; try again.",
    "unsupported_media_type": "Send the recording as audio/webm, audio/mp4, audio/mpeg or audio/wav.",
    "not_found": "Premium voice is not served here.",
    # premium dictation is hidden for Levain 1.0 (Phill 11-05): the transcribe route answers this, in these words only,
    # whether or not a key exists, until the server is started with the switch (``levain serve --premium-stt``)
    "premium_stt_off": "Premium dictation is not offered in this version.",
    "bad_json": "The request is not valid JSON.",
    "bad_keys": "A speak request names room_thread_id, reply_gseq, chunk, provider, voice and speed, and nothing else.",
    "bad_provider": "That is not a voice provider.",
    "provider_unsupported": "{p} is not supported in this version.",
    "no_dictation": "{p} reads aloud only; it does not offer dictation.",
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
    # each is true in every case that returns it (revoke_refusal): no grant page served here acts on this cockpit,
    # and the command line's revoke needs an unrevoked laptop key, which this Levain may or may not hold
    "revoke_unreachable": ("Premium voice is off: the phone's grant page cannot revoke this Levain's voice keys. "
                           "A revoke from the command line needs an unrevoked laptop key."),
    "revoke_unchecked": ("Premium voice is off: Levain could not check that this Levain's voice keys can be revoked, "
                         "so no key is read."),
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

def serve_voice(data_root: Path | str, meter: vmeter.Meter, *, last_read: MutableMapping[str, str | None],
                premium_stt: bool = False) -> dict[str, Any]:
    """The body of ``GET /cockpit/voice.json``. Whether a provider has a key comes from ONE verified replay of the
    authority log at ``data_root``; the keychain is never read here (Q-D3), so a page's poll never reaches it. Only a
    record's id and uses are shown (no keychain names, no part of a value). ``last_read`` is the outcome of this
    process's last key read or provider call per provider, kept in memory only. The spend is read without creating
    the ledger. ``premium_stt`` is the server's switch (default off): off, no provider lists ``stt`` among its uses and
    its retention words leave transcription out, so a page has nothing to offer a dictation control from; the body
    says ``"premium_stt": false``."""
    try:
        grants = custody.authority_for(data_root)().policy.grants
        authority = "ok"
    except Exception:       # noqa: BLE001 - any failure is "the log could not be verified now", said, not raised
        grants, authority = {}, "unreadable"
    rows = []
    for name, data in offered().items():
        record = voice_key_for(grants, name) if grants else None
        uses = sorted(u for u in record["uses"] if u != "stt" or (premium_stt and data.stt_model)) \
            if record is not None else []
        rows.append({"name": name,
                     "uses": uses,
                     "key": "set" if record is not None else "not_set",
                     "record_id": record["id"] if record is not None else None,
                     "last_read": last_read.get(name),
                     "voices": list(data.voices), "default_voice": data.default_voice, "tts_model": data.tts_model,
                     "speed": [data.speed_min, data.speed_max],
                     "retention": data.retention if premium_stt or not data.retention_speech
                     else data.retention_speech})
    caps = meter.caps
    body: dict[str, Any] = {"providers": rows, "prices_as_of": meter.prices.as_of, "estimate": True,
                            "authority": authority, "premium_stt": bool(premium_stt)}
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


def _cli_revoke_refusal(data_root: Path | str) -> str | None:
    """None when the command line's ``levain voice key revoke`` can be signed for the voice keys at ``data_root``, as
    far as the authority log shows; else ``revoke_unreachable`` (no key the command can sign with) or
    ``revoke_unchecked`` (the log could not be replayed now). The command signs with ``ssh_signer`` (``ssh-keygen -Y
    sign``) on this laptop, so its signer must be a key whose private half the laptop holds: a laptop-presence key.
    A passkey signs only through the phone's grant page, and a display-phone key's private half is on the phone. So
    the check is :meth:`~levain.authority.AuthoritySnapshot.signing_laptop_keys` (unrevoked, not quarantined) of a
    fresh verified replay, non-empty.

    What it cannot check: that the private half of that key is still present. The command takes any
    ``--signing-key`` path (or an agent-held key), so no file Levain could look for decides it, and signing a probe
    would prompt for a touch or passphrase. The replay is public material only."""
    try:
        state = custody.authority_for(data_root)()
        signers = state.signing_laptop_keys()
    except Exception as exc:     # noqa: BLE001 - a log that could not be replayed fails closed, in its own words
        _log.warning("voice: the command-line revoke could not be checked (%s); no key is read", type(exc).__name__)
        return "revoke_unchecked"
    return None if signers else "revoke_unreachable"


def revoke_refusal(revoke_queue: Path | str | None, data_root: Path | str) -> str | None:
    """None when a revoke reaches the voice keys at ``data_root``, else the refusal (a key of :data:`WORDS`, also the
    error code). ``revoke_queue`` is the queue the grant routes are served over (``GrantPhone.queue_dir``), None when
    the server serves no grant routes (no Signer, or its queue is not the consent reader's): then the only revoke is
    the command line's ``levain voice key revoke``, which acts on ``data_root`` directly but needs a laptop key that
    can sign it, so :func:`_cli_revoke_refusal` decides (Phill 11-06: premium voice with no Signer whenever the CLI
    revoke exists). Otherwise that queue's consent hooks must name exactly one cockpit
    (:func:`levain.consent.sign.sign_root`'s rule, so the root the grant routes act on) and it must be ``data_root``;
    any other answer is ``revoke_unreachable``. A fault while checking (the hook file unreadable, an unexpected shape,
    the log not replayable) is ``revoke_unchecked``, a different sentence: it is not known that no revoke reaches.
    That fault is logged by its type name only. Re-read on every call: the hook file and the log can change while the
    server runs."""
    from levain.consent.answering import hook_roots

    if revoke_queue is None:
        return _cli_revoke_refusal(data_root)
    try:
        roots = list(hook_roots(Path(revoke_queue)))
        if len(roots) != 1:
            return "revoke_unreachable"
        same = Path(roots[0]).resolve() == Path(data_root).resolve()
    except Exception as exc:     # noqa: BLE001 - a check that could not be made fails closed, in its own words
        _log.warning("voice: the revoke reach could not be checked (%s); no key is read", type(exc).__name__)
        return "revoke_unchecked"
    return None if same else "revoke_unreachable"


def _call(provider: str, use: str, units: int, data_root: Path | str, *, revoke_queue: Path | str | None,
          meter: vmeter.Meter, read_key: Callable[..., Any], last_read: MutableMapping[str, str | None],
          send: Callable[[Any], Any]) -> tuple[Any, Answer | None]:
    """Steps 3 to 6 of the module docstring for one call, after the revoke-reach check (:func:`revoke_refusal`): a
    key whose revoke from the phone could not reach it is never read, and nothing is reserved. ``send(secret)`` makes
    the adapter call and must write ``secret.reveal()`` inline in the adapter's argument list. Returns
    ``(result, None)`` or ``(None, refusal)``."""
    # known edge, by intent: a hook file edited (or the last laptop key revoked) between this check and read_key lets
    # one more key read through, the same class as any revoke racing a call already in flight
    refused_reach = revoke_refusal(revoke_queue, data_root)
    if refused_reach is not None:
        return None, refusal(503, refused_reach, refused_reach, fallback=True)
    model = PROVIDERS[provider].tts_model if use == "tts" else PROVIDERS[provider].stt_model
    try:
        check = meter.reserve(provider, use, model, units)
    except vmeter.MeterError:
        return None, refusal(503, "meter_unavailable", "meter_unavailable", fallback=True)
    if not check.allowed or check.reservation_id is None:
        return None, _refuse_check(check, provider)
    reservation = check.reservation_id
    key_failure = None
    fault = ""
    try:
        secret = read_key(provider, use, authority=custody.authority_for(data_root))
    except custody.VoiceKeyUnavailable as exc:
        key_failure = exc.code
    except Exception as exc:     # noqa: BLE001 - a reader fault custody does not name: no call went out
        key_failure, fault = "unexpected", type(exc).__name__
    if key_failure is not None:
        # no request went out: the reservation is released (counts nothing; a row left ``reserved`` would count toward
        # both caps for good, the meter has no reaper), and the page reads on device. last_read names the cause now
        # in force, so an earlier one that no longer applies is cleared (the states that are not shown read as None)
        _end(meter, reservation, release=True)
        last_read[provider] = key_failure if key_failure in LAST_READ_STATES else None
        if key_failure == "unexpected":
            _log.warning("voice: the key reader failed (%s); nothing was sent", fault)   # the type name only
            return None, internal_answer()
        return None, _key_refusal(key_failure, provider, use)
    last_read[provider] = None      # the key was read: an earlier read failure no longer applies; a call failure sets its own
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
        return result, None
    if failure in LAST_READ_STATES:
        last_read[provider] = failure
    if failure == "bad_call":
        return None, refusal(500, "internal", "bad_call", fallback=True)
    if failure == "internal":
        return None, internal_answer()
    status, error = PROVIDER_FAILURES.get(failure, (502, "provider_rejected"))
    words = vprov.WORDS.get(failure, vprov.WORDS["rejected"]).format(p=_name(provider))
    return None, _json(status, {"error": error, "message": words, "fallback": "on_device"})


# --- speak ------------------------------------------------------------------------------------------------------------

def _number(value: Any) -> bool:
    """A JSON number that can be range-checked. An int is always finite, and ``math.isfinite`` raises OverflowError on
    one past the float range (a 4 KiB body can carry such a digit string), so only a float is asked."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _parse_speak(raw: bytes) -> tuple[dict[str, Any] | None, Answer | None]:
    try:
        req = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, refusal(400, "bad_json", "bad_json")
    sample = isinstance(req, dict) and set(req) == SAMPLE_KEYS and req["sample"] is True
    if not sample and (not isinstance(req, dict) or set(req) != SPEAK_KEYS):
        return None, refusal(400, "bad_request", "bad_keys")
    provider = req["provider"]
    if isinstance(provider, str) and provider in UNSUPPORTED:
        return None, refusal(409, "provider_unsupported", "provider_unsupported", provider=provider)
    if not isinstance(provider, str) or provider not in PROVIDERS:
        return None, refusal(400, "bad_request", "bad_provider")
    if provider not in offered():
        return None, refusal(409, "provider_unsupported", "provider_unsupported", provider=provider)
    data = PROVIDERS[provider]
    if sample:
        req = {**req, "chunk": 0}
    else:
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


def speak(data_root: Path | str, service: Any, raw: bytes, *, directory: Path | str,
          revoke_queue: Path | str | None, meter: vmeter.Meter, transport: vprov.Transport,
          read_key: Callable[..., Any], last_read: MutableMapping[str, str | None]) -> Answer:
    """``POST /cockpit/voice/speak``: chunk ``chunk`` of the room reply that ended at ``reply_gseq``, as mp3. The words
    are only the record's (``reply_text`` over the open room's stream, then ``speakable``); a body naming any other key
    is refused. ``directory`` is the store directory the room is read from; ``revoke_queue`` the grant routes' queue
    (:func:`revoke_refusal`). Both keyword-only, like :func:`transcribe`'s: two paths never swap by position."""
    req, refused = _parse_speak(raw)
    if refused is not None or req is None:
        return refused or internal_answer()
    provider = req["provider"]
    if req.get("sample") is True:
        chunks = [SAMPLE_WORDS]
    else:
        chunks, refused = _reply_chunks(directory, service, req)
        if refused is not None or chunks is None:
            return refused or internal_answer()
    words = chunks[req["chunk"]]
    adapter = vprov.provider_for(provider, transport)
    voice, speed = req["voice"], req["speed"]
    audio, refused = _call(provider, "tts", vprov.inworld_units(words) if provider == "inworld" else len(words), data_root, revoke_queue=revoke_queue, meter=meter,
                           read_key=read_key, last_read=last_read,
                           send=lambda secret: adapter.speak(secret.reveal(), words, voice, speed))
    if refused is not None:
        return refused
    return Answer(200, "audio/mpeg", [("X-Voice-Chunks", str(len(chunks))), ("X-Voice-Chunk", str(req["chunk"]))],
                  bytes(audio))


def _reply_chunks(directory: Path | str, service: Any, req: dict[str, Any]) -> tuple[list[str] | None, Answer | None]:
    """The speakable chunks of the room reply the request names, or the refusal. The words are only the record's."""
    thread, need = cockpit_room._need_room(directory, req)
    if need is not None:
        status = need[0]
        if status == 409:
            return None, refusal(409, "room_rotated", "room_rotated")
        if status == 404:
            return None, refusal(404, "not_found", "no_room")
        return None, refusal(400, "bad_request", "bad_room")
    try:
        room = cockpit_room.room_read(directory, service)
    except Exception:            # noqa: BLE001 - a record that cannot be read is fixed words, never its detail
        return None, refusal(500, "internal", "room_unreadable")
    if room.get("room_thread") != thread:       # rotated between the check and the read
        return None, refusal(409, "room_rotated", "room_rotated")
    text = reply_text(room.get("stream") or [], req["reply_gseq"])
    if text is None:
        return None, refusal(404, "not_a_reply", "not_a_reply")
    chunks = speakable(text)
    if not chunks:
        return None, refusal(404, "nothing_to_read", "nothing_to_read")
    if not req["chunk"] < len(chunks):
        return None, refusal(404, "no_such_chunk", "no_such_chunk", chunks=len(chunks))
    return chunks, None


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
    if provider not in offered():
        return None, refusal(409, "provider_unsupported", "provider_unsupported", provider=provider)
    if not PROVIDERS[provider].stt_model:
        return None, refusal(409, "no_dictation", "no_dictation", provider=provider)
    return provider, None


def transcribe(data_root: Path | str, query: str, content_type: str, audio: bytes, *,
               revoke_queue: Path | str | None, meter: vmeter.Meter, transport: vprov.Transport,
               read_key: Callable[..., Any], last_read: MutableMapping[str, str | None]) -> Answer:
    """``POST /cockpit/voice/transcribe?provider=``: ``{"text": ...}`` for the audio body. The audio is never written
    or logged; the ledger holds the estimated seconds only (:func:`upload_units`). ``revoke_queue`` is the grant
    routes' queue, whose consent hooks :func:`revoke_refusal` reads (keyword-only, as in :func:`speak`)."""
    provider, refused = _query_provider(query)
    if refused is not None or provider is None:
        return refused or internal_answer()
    mime = base_type(content_type)
    if mime not in vprov.MIME_ALLOW:
        return refusal(415, "unsupported_media_type", "unsupported_media_type")
    if not audio:
        return refusal(400, "no_audio", "no_audio")
    adapter = vprov.provider_for(provider, transport)
    text, refused = _call(provider, "stt", upload_units(len(audio)), data_root, revoke_queue=revoke_queue,
                          meter=meter, read_key=read_key, last_read=last_read,
                          # the lambda runs inside _call, before the del below drops this frame's reference
                          send=lambda secret: adapter.transcribe(secret.reveal(), audio, mime, ""))  # noqa: F821
    del audio
    if refused is not None:
        return refused
    return _json(200, {"text": text if isinstance(text, str) else ""})
