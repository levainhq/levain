"""levain.voice.stream_session: the server-side core of one live-dictation stream (cockpit mic slice S1; no HTTP here).

A page that streams microphone audio holds a *session*: a 120 s ceiling reserved on the voice meter at open, a strictly
sequenced intake of PCM16 16 kHz mono chunks, a provider-stream object that forwards them, and a mailbox the route reads
(newest partial, final text). Four rules carry the module.

1. The ceiling is reserved first and settled last. :meth:`StreamSession.close` runs the provider close and then settles
   the meter in a ``finally``, so every exit (close, idle timeout, a provider that fails, shutdown) ends the reservation
   with the ACTUAL seconds streamed (bytes accepted / :data:`BYTES_PER_SECOND`, rounded up), never the ceiling. A session
   that streamed no audio is released (it cost nothing Levain knows of).
2. The key never becomes an attribute of anything the page can name. :meth:`StreamRegistry.open` reads it once through
   ``custody.read_key`` (the path ``transcribe`` uses), hands the :class:`~levain.voice.custody.Secret` to the provider
   factory and drops it; the session holds the provider-stream, not the key.
3. A session is addressable only by the owner that opened it, by an unguessable id; a wrong owner and an unknown id get
   the same refusal, so the id space is no oracle. The registry has a hard cap on live sessions.
4. Nothing spoken is logged: audio and text appear in no log line; lines carry event names and seconds only.

The provider side is :class:`ProviderStream` (send_audio / poll / close). The Deepgram adapter (S2b) implements it; tests
use a fake.
"""
from __future__ import annotations

import hmac
import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from levain.voice import custody
from levain.voice import meter as vmeter

_log = logging.getLogger("levain.voice.stream_session")

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE          # PCM16 mono 16 kHz: 32,000 B/s
CEILING_SECONDS = 120
MAX_CHUNK_BYTES = 64 * 1024
RATE_FACTOR = 2.0              # audio may arrive at most this many times faster than the wall clock ...
BURST_SECONDS = 4.0            # ... plus this much head start (a phone that stalled then flushes)
IDLE_TIMEOUT_S = 15.0
MAX_SESSIONS = 4
SID_BYTES = 24

# Fixed words: the code is the contract, the words are what the route may show. Nothing here carries a value from outside.
WORDS: dict[str, str] = {
    "bad_sequence": "That audio chunk arrived out of order, so the dictation was stopped.",
    "bad_chunk": "That audio chunk was empty, too large or not whole samples.",
    "too_fast": "Audio arrived faster than it can be spoken, so the dictation was stopped.",
    "ceiling": "The dictation reached its time limit.",
    "idle_timeout": "The dictation stopped because no audio arrived.",
    "closed": "That dictation has already ended.",
    "unknown_session": "There is no such dictation.",
    "too_many_sessions": "Too many dictations are running; try again in a moment.",
    "provider_failed": "The voice provider stopped, so the dictation ended.",
    "meter_unavailable": "Levain could not check the voice budget, so premium voice is off for now.",
    "month_cap": "The monthly voice budget is used up.",
    "day_cap": "Today's voice budget is used up.",
    "unpriced": "That voice model has no price on file, so it is not used.",
    "key_unavailable": "The voice key could not be read, so premium voice is off for now.",
    "internal": "Something went wrong inside Levain; the dictation ended.",
}


class StreamRefused(Exception):
    """A fixed-words refusal. ``code`` is a key of :data:`WORDS`; ``key_code`` carries custody's own code on
    ``key_unavailable`` so the route can map it as ``transcribe`` does."""

    def __init__(self, code: str, key_code: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.key_code = key_code

    @property
    def words(self) -> str:
        return WORDS[self.code]


@dataclass(frozen=True)
class StreamUpdate:
    """What the provider-stream has so far: the newest unfinished ``partial``, the text finalised so far, and whether
    the provider side has failed."""
    partial: str = ""
    final: str = ""
    failed: bool = False


class ProviderStream(Protocol):
    """One outbound provider connection. The S2b Deepgram adapter implements it."""

    def send_audio(self, pcm: bytes) -> None:
        """Forward PCM16 16 kHz mono bytes. Raises on a provider failure."""

    def poll(self) -> StreamUpdate:
        """The newest state; never blocks."""

    def close(self) -> str:
        """End the stream and return the final text. Idempotent; may raise."""


# The factory receives the Secret and must write ``secret.reveal()`` inline where it uses it (as the transcribe adapters do).
StreamFactory = Callable[[custody.Secret], ProviderStream]


def seconds_for(n_bytes: int) -> int:
    """Whole seconds (rounded up) of ``n_bytes`` of PCM16 16 kHz mono."""
    return math.ceil(n_bytes / BYTES_PER_SECOND)


class StreamSession:
    """One live session. Built by :meth:`StreamRegistry.open`; all methods are thread-safe."""

    def __init__(self, sid: str, owner: str, provider: str, meter: vmeter.Meter, reservation_id: int,
                 stream: ProviderStream, *, clock: Callable[[], float], idle_timeout: float,
                 on_end: Callable[[str], None]) -> None:
        self.sid = sid
        self._owner = owner
        self.provider = provider
        self._meter = meter
        self._reservation = reservation_id
        self._stream = stream
        self._clock = clock
        self._idle_timeout = idle_timeout
        self._on_end = on_end
        self._lock = threading.RLock()
        self._opened = clock()
        self._last_activity = self._opened
        self._next_seq = 0
        self._bytes = 0
        self._buffer = bytearray()          # bounded at the ceiling; kept for the stop-time WAV fallback
        self._closed = False
        self._final = ""
        self._partial = ""
        self._end_reason = ""

    # --- ownership --------------------------------------------------------------------------------------------------
    def owned_by(self, owner: str) -> bool:
        return hmac.compare_digest(self._owner.encode(), str(owner).encode())

    # --- intake -----------------------------------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def streamed_bytes(self) -> int:
        return self._bytes

    def feed(self, seq: int, pcm: bytes) -> StreamUpdate:
        """Accept chunk ``seq`` (strictly 0, 1, 2, ...). A gap, a repeat or a replay refuses and ends nothing but the
        chunk; a limit breach (rate, ceiling, idle, provider failure) ends the session, settling the meter."""
        with self._lock:
            if self._closed:
                raise StreamRefused("closed")
            now = self._clock()
            if now - self._last_activity > self._idle_timeout:
                self._end("idle_timeout")
                raise StreamRefused("idle_timeout")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq != self._next_seq:
                raise StreamRefused("bad_sequence")
            if (not isinstance(pcm, (bytes, bytearray)) or not pcm or len(pcm) > MAX_CHUNK_BYTES
                    or len(pcm) % BYTES_PER_SAMPLE):
                raise StreamRefused("bad_chunk")
            if self._bytes + len(pcm) > CEILING_SECONDS * BYTES_PER_SECOND:
                self._end("ceiling")
                raise StreamRefused("ceiling")
            allowed = (now - self._opened + BURST_SECONDS) * BYTES_PER_SECOND * RATE_FACTOR
            if self._bytes + len(pcm) > allowed:
                self._end("too_fast")
                raise StreamRefused("too_fast")
            self._next_seq += 1
            self._last_activity = now
            self._bytes += len(pcm)          # counted before the send: audio that reached a provider may be billed
            self._buffer += pcm
            try:
                self._stream.send_audio(bytes(pcm))
            except Exception as exc:     # noqa: BLE001 - any provider fault ends the session in fixed words
                _log.warning("voice stream: the provider stream failed on send (%s)", type(exc).__name__)
                self._end("provider_failed")
                raise StreamRefused("provider_failed")
            return self._update()

    def _update(self) -> StreamUpdate:
        try:
            up = self._stream.poll()
        except Exception as exc:     # noqa: BLE001
            _log.warning("voice stream: the provider stream failed on poll (%s)", type(exc).__name__)
            self._end("provider_failed")
            raise StreamRefused("provider_failed")
        self._partial, self._final = up.partial, up.final
        if up.failed:
            self._end("provider_failed")
            raise StreamRefused("provider_failed")
        return up

    def mailbox(self) -> StreamUpdate:
        """The newest partial and the final text as last read; after close, the final text."""
        with self._lock:
            if self._closed:
                return StreamUpdate(partial="", final=self._final)
            self._check_idle()
            return self._update()

    def _check_idle(self) -> None:
        if self._clock() - self._last_activity > self._idle_timeout:
            self._end("idle_timeout")
            raise StreamRefused("idle_timeout")

    def take_audio(self) -> bytes:
        """The audio accepted so far (bounded at the ceiling), for the stop-time fallback."""
        with self._lock:
            return bytes(self._buffer)

    def expired(self) -> bool:
        with self._lock:
            return not self._closed and self._clock() - self._last_activity > self._idle_timeout

    # --- end --------------------------------------------------------------------------------------------------------
    def close(self) -> str:
        """End the session and return the final text (empty when the provider gave none). Idempotent."""
        with self._lock:
            if self._closed:
                return self._final
            self._end("closed")
            return self._final

    def _end(self, reason: str) -> None:
        """Close the provider stream, then settle the meter in a ``finally``: every exit path ends the reservation."""
        if self._closed:
            return
        self._closed = True
        self._end_reason = reason
        try:
            try:
                text = self._stream.close()
                if isinstance(text, str) and text:
                    self._final = text
            except Exception as exc:     # noqa: BLE001 - a provider that fails to close never blocks the settle
                _log.warning("voice stream: the provider stream failed on close (%s)", type(exc).__name__)
        finally:
            try:
                if self._bytes == 0:
                    self._meter.release(self._reservation)
                else:
                    self._meter.settle_actual(self._reservation, seconds_for(self._bytes))
            except Exception as exc:     # noqa: BLE001 - a row left reserved still counts: errs toward the cap
                _log.warning("voice stream: the meter could not end the reservation (%s)", type(exc).__name__)
            self._buffer = bytearray()
            self._on_end(self.sid)
            _log.info("voice stream: ended (%s) after %d s", reason, seconds_for(self._bytes))


class StreamRegistry:
    """The live sessions: a hard cap, owner binding, unguessable ids."""

    def __init__(self, meter: vmeter.Meter, *, max_sessions: int = MAX_SESSIONS, idle_timeout: float = IDLE_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._meter = meter
        self._max = max_sessions
        self._idle = idle_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, StreamSession] = {}
        self._pending = 0           # slots taken by an open in progress

    def open(self, owner: str, provider: str, model: str, *, data_root: Any,
             read_key: Callable[..., custody.Secret], factory: StreamFactory) -> StreamSession:
        """Reserve a slot and the :data:`CEILING_SECONDS` ceiling, read the key once, build the provider stream. Any
        failure gives back the slot and the reservation and raises :class:`StreamRefused`."""
        if not owner or not isinstance(owner, str):
            raise StreamRefused("unknown_session")
        self.reap()
        with self._lock:
            if len(self._sessions) + self._pending >= self._max:
                raise StreamRefused("too_many_sessions")
            self._pending += 1
        reservation: int | None = None
        try:
            try:
                check = self._meter.reserve(provider, "stt", model, CEILING_SECONDS)
            except vmeter.MeterError:
                raise StreamRefused("meter_unavailable")
            if not check.allowed or check.reservation_id is None:
                raise StreamRefused(check.reason if check.reason in WORDS else "meter_unavailable")
            reservation = check.reservation_id
            key_code = ""
            try:
                secret = read_key(provider, "stt", authority=custody.authority_for(data_root))
            except custody.VoiceKeyUnavailable as exc:
                secret, key_code = None, exc.code
            except Exception as exc:     # noqa: BLE001
                _log.warning("voice stream: the key reader failed (%s); nothing was sent", type(exc).__name__)
                secret, key_code = None, "unexpected"
            if secret is None:
                raise StreamRefused("key_unavailable", key_code)
            try:
                try:
                    stream = factory(secret)
                finally:
                    del secret
            except Exception as exc:     # noqa: BLE001 - no connection made: nothing was sent
                _log.warning("voice stream: the provider stream could not start (%s)", type(exc).__name__)
                raise StreamRefused("provider_failed")
            sid = secrets.token_urlsafe(SID_BYTES)
            session = StreamSession(sid, owner, provider, self._meter, reservation, stream, clock=self._clock,
                                    idle_timeout=self._idle, on_end=self._forget)
            with self._lock:
                self._sessions[sid] = session
            reservation = None          # the session now owns ending it
            return session
        finally:
            with self._lock:
                self._pending -= 1
            if reservation is not None:
                try:
                    self._meter.release(reservation)
                except Exception as exc:     # noqa: BLE001
                    _log.warning("voice stream: the meter could not release (%s)", type(exc).__name__)

    def get(self, sid: str, owner: str) -> StreamSession:
        """The session ``sid`` for ``owner``; a missing id, a wrong owner and an ended session alike raise
        ``unknown_session``."""
        with self._lock:
            session = self._sessions.get(sid) if isinstance(sid, str) else None
        if session is None or not session.owned_by(owner):
            raise StreamRefused("unknown_session")
        return session

    def _forget(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def reap(self) -> int:
        """Close every idle-expired session (settling each); returns how many."""
        with self._lock:
            live = list(self._sessions.values())
        n = 0
        for session in live:
            if session.expired():
                try:
                    session.close()
                except Exception as exc:     # noqa: BLE001
                    _log.warning("voice stream: reap failed (%s)", type(exc).__name__)
                n += 1
        return n

    def shutdown(self) -> None:
        """Close every session (the server is stopping)."""
        with self._lock:
            live = list(self._sessions.values())
        for session in live:
            try:
                session.close()
            except Exception as exc:     # noqa: BLE001
                _log.warning("voice stream: shutdown close failed (%s)", type(exc).__name__)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
