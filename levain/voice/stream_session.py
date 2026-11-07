"""levain.voice.stream_session: the server-side core of one live-dictation stream (cockpit mic slice S1; no HTTP here).

A page that streams microphone audio holds a *session*: a 120 s ceiling reserved on the voice meter at open, a strictly
sequenced intake of PCM16 16 kHz mono chunks, a provider-stream object that forwards them, and a mailbox the route reads
(newest partial, final text, and once ended the reason). Six rules carry the module.

1. The ceiling is reserved first and settled last, exactly once. Every end (close, idle timeout, the age limit, a
   provider that fails, shutdown) closes the provider stream and then settles the meter in a ``finally`` with the ACTUAL
   seconds streamed (bytes accepted / :data:`BYTES_PER_SECOND`, rounded up), never the ceiling. What Levain sent is what
   it counts: a session that streamed no audio is released. The one place Levain cannot know what reached the provider
   is the factory: a factory that raises :class:`StreamNotStarted` is released, any other factory failure (or a result
   that is not a stream) settles one unit. The rule is that provider billing starts at audio: a stream that was built
   but carried no audio (a setup failure after the factory) is released, while a factory that failed some other way may
   have sent before failing, which Levain cannot see, so it counts one unit. A settle that fails is retried once, then
   handed to the registry, which retries the oldest first on every reap and never drops one; until then the row stays
   reserved, which errs toward the cap, and while :data:`MAX_UNSETTLED` are waiting every new open is refused
   (``settle_backlog``): a ledger that cannot record spend stops new spend.
2. The key never becomes an attribute of anything the page can name. :meth:`StreamRegistry.open` reads it once through
   ``custody.read_key`` (the path ``transcribe`` uses), hands the :class:`~levain.voice.custody.Secret` to the provider
   factory and drops it; the session holds the provider-stream, not the key.
3. A session is addressable only by the owner that opened it, by an unguessable id; a wrong owner and an unknown id get
   the same refusal, so the id space is no oracle. The registry has a hard cap on live sessions; an open in progress
   holds a slot from before the meter is asked until the session is published or refused.
4. An ended session stays readable by its owner until the owner calls :meth:`StreamSession.close` or
   :data:`ENDED_RETAIN_S` passes, whichever is first, so a session that ended on its own still shows its final text and
   why it ended. Ended sessions hold no live slot; at most ``max_sessions`` of them are kept (the oldest goes first), and
   shutdown drops them all. A dropped session's audio and text are dropped with it. The audio buffer (bounded at the
   ceiling) outlives the end for the stop-time WAV fallback and is handed out once by :meth:`StreamSession.take_audio`.
5. Nothing spoken and no key leaves through a log line or an exception: lines carry event names, exception TYPE names
   and seconds only, and every :class:`StreamRefused` is raised outside any ``except`` block, from a frame that holds
   no key and no provider text, so it chains nothing.
6. Once :meth:`StreamRegistry.shutdown` has begun, no session is published: an open that was connecting ends its new
   stream and gives its reservation back.

Known edge: provider calls (send, poll, close) run under the session's lock, so chunks reach the provider in sequence
order and the provider object is never used from two threads at once; a slow provider call therefore delays another
call on the SAME session (a mailbox read waits behind a send). Reap (and so every open, which reaps first) only tries
each session's lock and skips a busy one, which is mid-call and so not idle, so a hung provider call on one session
never blocks an open. Shutdown does wait for each live session's lock, since it must end every one. Shutdown does not
wait for an open that is still connecting; that open cleans up after itself by rule 6.

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
MAX_AGE_S = CEILING_SECONDS + IDLE_TIMEOUT_S   # wall-clock life: a real-time speaker reaches the byte ceiling first
ENDED_RETAIN_S = 60.0
MAX_SESSIONS = 4
MAX_UNSETTLED = 64             # settles waiting for a retry; at this many, new opens are refused until it drains
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
    "settle_backlog": "Levain could not record what earlier dictations cost, so premium voice is off for now.",
    "month_cap": "The monthly voice budget is used up.",
    "day_cap": "Today's voice budget is used up.",
    "unpriced": "That voice model has no price on file, so it is not used.",
    "key_unavailable": "The voice key could not be read, so premium voice is off for now.",
    "stopping": "Levain is stopping, so the dictation ended.",
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


class StreamNotStarted(Exception):
    """A factory raises this when it failed before any byte reached the provider (no connection was made), so the
    reservation is released. Any other exception from a factory settles one unit: the handshake may have happened."""


@dataclass(frozen=True)
class StreamUpdate:
    """What the provider-stream has so far: the newest unfinished ``partial``, the text finalised so far, and whether
    the provider side has failed. ``ended`` is set by the session, never the provider: the :data:`WORDS` code the
    session ended with, empty while it is live."""
    partial: str = ""
    final: str = ""
    failed: bool = False
    ended: str = ""


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


def _owner_bytes(owner: object) -> bytes | None:
    # surrogatepass is injective, so two owners that differ only in a lone surrogate never compare equal
    return owner.encode("utf-8", "surrogatepass") if isinstance(owner, str) else None


def _is_stream(obj: object) -> bool:
    try:
        return all(callable(getattr(obj, name, None)) for name in ("send_audio", "poll", "close"))
    except Exception:     # noqa: BLE001 - an object whose attribute lookup raises is not a stream
        return False


def _valid_update(up: object) -> bool:
    return (isinstance(up, StreamUpdate) and isinstance(up.partial, str) and isinstance(up.final, str)
            and isinstance(up.failed, bool))


class StreamSession:
    """One session. Built by :meth:`StreamRegistry.open`; all methods are thread-safe."""

    def __init__(self, sid: str, owner: str, provider: str, meter: vmeter.Meter, reservation_id: int,
                 stream: ProviderStream, *, clock: Callable[[], float], idle_timeout: float, max_age: float,
                 on_end: Callable[[str], None], on_collect: Callable[[str], None],
                 on_unsettled: Callable[[int, int], None]) -> None:
        self.sid = sid
        self._owner = _owner_bytes(owner) or b""
        self.provider = provider
        self._meter = meter
        self._reservation = reservation_id
        self._stream = stream
        self._clock = clock
        self._idle_timeout = idle_timeout
        self._max_age = max_age
        self._on_end = on_end
        self._on_collect = on_collect
        self._on_unsettled = on_unsettled
        self._lock = threading.RLock()
        self._opened = clock()
        self._last_activity = self._opened
        self._next_seq = 0
        self._bytes = 0
        self._buffer = bytearray()          # bounded at the ceiling; kept past the end for the stop-time WAV fallback
        self._closed = False
        self._final = ""
        self._partial = ""
        self._end_reason = ""

    # --- ownership --------------------------------------------------------------------------------------------------
    def owned_by(self, owner: object) -> bool:
        other = _owner_bytes(owner)
        return bool(self._owner) and other is not None and hmac.compare_digest(self._owner, other)

    # --- state ------------------------------------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def end_reason(self) -> str:
        return self._end_reason

    @property
    def streamed_bytes(self) -> int:
        return self._bytes

    def _limit(self, now: float) -> str:
        """The time limit this session has passed at ``now``, else ''."""
        if now - self._last_activity > self._idle_timeout:
            return "idle_timeout"
        if now - self._opened > self._max_age:
            return "ceiling"
        return ""

    # --- intake -----------------------------------------------------------------------------------------------------
    def feed(self, seq: int, pcm: bytes) -> StreamUpdate:
        """Accept chunk ``seq`` (strictly 0, 1, 2, ...). A gap, a repeat or a replay refuses and ends nothing but the
        chunk; a limit breach (rate, ceiling, age, idle, provider failure) ends the session, settling the meter."""
        with self._lock:
            code, update = self._feed(seq, pcm)
        if update is None:
            raise StreamRefused(code)
        return update

    def _feed(self, seq: object, pcm: object) -> tuple[str, StreamUpdate | None]:
        if self._closed:
            return "closed", None
        now = self._clock()
        limit = self._limit(now)
        if limit:
            self._end(limit)
            return limit, None
        if type(seq) is not int or seq != self._next_seq:     # exact int: a subclass's __ne__ could lie
            return "bad_sequence", None
        if not isinstance(pcm, (bytes, bytearray)):
            return "bad_chunk", None
        # measured and copied through the buffer protocol, so a subclass's __len__ or __bytes__ cannot lie
        with memoryview(pcm) as view:
            n = view.nbytes
            if not n or n > MAX_CHUNK_BYTES or n % BYTES_PER_SAMPLE:
                return "bad_chunk", None
            payload = view.tobytes()
        if self._bytes + n > CEILING_SECONDS * BYTES_PER_SECOND:
            self._end("ceiling")
            return "ceiling", None
        allowed = (now - self._opened + BURST_SECONDS) * BYTES_PER_SECOND * RATE_FACTOR
        if self._bytes + n > allowed:
            self._end("too_fast")
            return "too_fast", None
        self._next_seq += 1
        self._last_activity = now
        self._bytes += n                 # counted before the send: audio that reached a provider may be billed
        self._buffer += payload
        failed = ""
        try:
            self._stream.send_audio(payload)
        except Exception as exc:     # noqa: BLE001 - any provider fault ends the session in fixed words
            failed = type(exc).__name__
        if failed:
            _log.warning("voice stream: the provider stream failed on send (%s)", failed)
            self._end("provider_failed")
            return "provider_failed", None
        update = self._poll()
        return ("provider_failed", None) if update is None else ("", update)

    def _poll(self) -> StreamUpdate | None:
        """Read the provider's state into the mailbox; on any fault (a raise, a malformed result, a reported failure)
        end the session and return None. Returning, not raising, keeps provider text out of every refusal's frames."""
        failed = ""
        try:
            up = self._stream.poll()
            if not _valid_update(up):
                failed = "malformed"
            elif up.failed:
                failed = "reported"
            else:
                self._partial, self._final = up.partial, up.final
        except Exception as exc:     # noqa: BLE001
            failed = type(exc).__name__
        if failed:
            _log.warning("voice stream: the provider stream failed on poll (%s)", failed)
            self._end("provider_failed")
            return None
        return StreamUpdate(partial=self._partial, final=self._final)

    def mailbox(self) -> StreamUpdate:
        """The newest partial and the final text; once ended, the final text and the reason it ended."""
        with self._lock:
            if self._closed:
                return StreamUpdate(partial="", final=self._final, ended=self._end_reason)
            code = self._limit(self._clock())
            if code:
                self._end(code)
                update = None
            else:
                update = self._poll()
                code = "provider_failed"
        if update is None:
            raise StreamRefused(code)
        return update

    def take_audio(self) -> bytes:
        """The audio accepted so far (bounded at the ceiling), for the stop-time fallback. While live it is a copy;
        after the end it is handed out once and then dropped."""
        with self._lock:
            data = bytes(self._buffer)
            if self._closed:
                self._buffer = bytearray()
            return data

    def expired(self) -> bool:
        """True when the session is live and past its idle or age limit (a read; :meth:`StreamRegistry.reap` ends)."""
        with self._lock:
            return not self._closed and bool(self._limit(self._clock()))

    # --- end --------------------------------------------------------------------------------------------------------
    def close(self) -> str:
        """End the session if it is live, return the final text (empty when the provider gave none), and stop it
        being addressable through the registry (the owner has collected it). Idempotent."""
        with self._lock:
            if not self._closed:
                self._end("closed", retain=False)     # collected next, so never counted among the retained ended
            final = self._final
        self._on_collect(self.sid)
        return final

    def _end_if_expired(self) -> bool:
        """End with the limit's reason if, checked under the lock, the session is live and past a limit. A session
        whose lock is held is mid-call, so not idle: it is skipped, never waited for."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._closed:
                return False
            code = self._limit(self._clock())
            if not code:
                return False
            self._end(code)
            return True
        finally:
            self._lock.release()

    def _end_now(self, reason: str) -> None:
        with self._lock:
            self._end(reason)

    def _end(self, reason: str, *, retain: bool = True) -> None:
        """Close the provider stream, then settle the meter in a ``finally``: every exit path ends the reservation,
        once. ``retain`` False (the owner is collecting it) skips the registry's retained-ended list. Caller holds
        the lock."""
        if self._closed:
            return
        self._closed = True
        self._end_reason = reason
        try:
            failed = ""
            try:
                text = self._stream.close()
                if isinstance(text, str) and text:
                    self._final = text
            except Exception as exc:     # noqa: BLE001 - a provider that fails to close never blocks the settle
                failed = type(exc).__name__
            if failed:
                _log.warning("voice stream: the provider stream failed on close (%s)", failed)
        finally:
            self._partial = ""
            self._settle()
            if retain:
                self._on_end(self.sid)
            _log.info("voice stream: ended (%s) after %d s", reason, seconds_for(self._bytes))

    def _settle(self) -> None:
        seconds = seconds_for(self._bytes)
        failed = ""
        for _attempt in range(2):        # one retry: a ledger lock held past the busy wait is the likely fault
            try:
                end_reservation(self._meter, self._reservation, seconds)
                return
            except Exception as exc:     # noqa: BLE001
                failed = type(exc).__name__
        _log.warning("voice stream: the meter could not end the reservation (%s); it stays counted until a retry", failed)
        self._on_unsettled(self._reservation, seconds)

    def _discard(self) -> None:
        """Drop the audio and the text (the registry let go of an ended session). No lock: these are single attribute
        rebinds, and taking this session's lock here could deadlock against another session ending."""
        self._buffer = bytearray()
        self._final = ""
        self._partial = ""


def end_reservation(meter: vmeter.Meter, reservation_id: int, seconds: int) -> None:
    """Settle ``reservation_id`` at ``seconds`` streamed, or release it when nothing was streamed."""
    if seconds == 0:
        meter.release(reservation_id)
    else:
        meter.settle_actual(reservation_id, seconds)


class StreamRegistry:
    """The sessions: a hard cap on live ones, owner binding, unguessable ids, ended ones kept briefly (rule 4)."""

    def __init__(self, meter: vmeter.Meter, *, max_sessions: int = MAX_SESSIONS, idle_timeout: float = IDLE_TIMEOUT_S,
                 max_age: float = MAX_AGE_S, retain: float = ENDED_RETAIN_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._meter = meter
        self._max = max_sessions
        self._idle = idle_timeout
        self._max_age = max_age
        self._retain = retain
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, StreamSession] = {}     # live and retained-ended
        self._ended_at: dict[str, float] = {}             # sid -> when it ended, oldest first
        self._unsettled: list[tuple[int, int]] = []       # (reservation_id, seconds) whose settle failed
        self._pending = 0                                  # slots taken by an open in progress
        self._closed = False

    def _live(self) -> int:
        return len(self._sessions) - len(self._ended_at)

    # --- open -------------------------------------------------------------------------------------------------------
    def open(self, owner: str, provider: str, model: str, *, data_root: Any,
             read_key: Callable[..., custody.Secret], factory: StreamFactory) -> StreamSession:
        """Take a slot, reserve the :data:`CEILING_SECONDS` ceiling, read the key once, build the provider stream and
        publish the session. Any failure gives back the slot, ends the reservation (rule 1), closes a stream that was
        built, and raises :class:`StreamRefused`."""
        if not owner or not isinstance(owner, str):
            raise StreamRefused("unknown_session")
        self.reap()
        with self._lock:
            if self._closed:
                code = "stopping"
            elif len(self._unsettled) >= MAX_UNSETTLED:
                code = "settle_backlog"
            elif self._live() + self._pending >= self._max:
                code = "too_many_sessions"
            else:
                code = ""
                self._pending += 1
        if code:
            raise StreamRefused(code)
        session, code, key_code = self._build(owner, provider, model, data_root, read_key, factory)
        if session is None:
            raise StreamRefused(code, key_code)
        return session

    def _build(self, owner: str, provider: str, model: str, data_root: Any, read_key: Callable[..., custody.Secret],
               factory: StreamFactory) -> tuple[StreamSession | None, str, str]:
        """The body of :meth:`open`, holding the slot open took. Returns (session, '', '') or (None, code, key_code);
        it raises no refusal itself, so no refusal is ever raised inside a handler (rule 5)."""
        slot_held = True
        reservation: int | None = None
        settle_units = 0                 # what a reservation ends at if this open fails: 0 releases it
        stream: Any = None               # a built provider stream this open still owns (closed on failure)
        try:
            meter_failed = False
            try:
                check = self._meter.reserve(provider, "stt", model, CEILING_SECONDS)
            except vmeter.MeterError:
                meter_failed = True
            if meter_failed:
                return None, "meter_unavailable", ""
            if not check.allowed or check.reservation_id is None:
                return None, (check.reason if check.reason in WORDS else "meter_unavailable"), ""
            reservation = check.reservation_id

            secret: Any = None
            key_code = ""
            try:
                secret = read_key(provider, "stt", authority=custody.authority_for(data_root))
            except custody.VoiceKeyUnavailable as exc:
                key_code = exc.code if isinstance(exc.code, str) else "unexpected"
            except Exception as exc:     # noqa: BLE001
                key_code = "unexpected"
                _log.warning("voice stream: the key reader failed (%s); nothing was sent", type(exc).__name__)
            if not isinstance(secret, custody.Secret):
                return None, "key_unavailable", key_code or "unexpected"

            failed = ""
            try:
                stream = factory(secret)
            except StreamNotStarted:
                failed = "StreamNotStarted"
            except Exception as exc:     # noqa: BLE001
                failed, settle_units = type(exc).__name__, 1
            finally:
                del secret
            if not failed and not _is_stream(stream):
                failed, settle_units = "not a stream", 1     # kept in ``stream``: the cleanup closes it if it can
            if failed:
                _log.warning("voice stream: the provider stream could not start (%s)", failed)
                return None, "provider_failed", ""

            failed = ""
            try:
                sid = secrets.token_urlsafe(SID_BYTES)
                session = StreamSession(sid, owner, provider, self._meter, reservation, stream, clock=self._clock,
                                        idle_timeout=self._idle, max_age=self._max_age, on_end=self._ended,
                                        on_collect=self._collect, on_unsettled=self._add_unsettled)
            except Exception as exc:     # noqa: BLE001
                failed = type(exc).__name__
            if failed:
                _log.warning("voice stream: the session could not be set up (%s)", failed)
                return None, "internal", ""

            # the slot moves from pending to published in ONE locked step, so the cap never counts an open twice or
            # not at all, and shutdown either sees this session or this open sees shutdown
            with self._lock:
                if not self._closed:
                    self._pending -= 1
                    slot_held = False
                    self._sessions[sid] = session
            if slot_held:
                return None, "stopping", ""
            stream, reservation = None, None    # the session now owns both
            return session, "", ""
        finally:
            if slot_held:
                with self._lock:
                    self._pending -= 1
            if stream is not None:
                failed = ""
                try:
                    close = getattr(stream, "close", None)     # best effort: a non-stream result may have no close
                    if callable(close):
                        close()
                except Exception as exc:     # noqa: BLE001
                    failed = type(exc).__name__
                if failed:
                    _log.warning("voice stream: the unused provider stream failed on close (%s)", failed)
            if reservation is not None:
                failed = ""
                try:
                    end_reservation(self._meter, reservation, settle_units)
                except Exception as exc:     # noqa: BLE001
                    failed = type(exc).__name__
                if failed:
                    _log.warning("voice stream: the meter could not end the reservation (%s)", failed)
                    self._add_unsettled(reservation, settle_units)

    # --- lookup -----------------------------------------------------------------------------------------------------
    def get(self, sid: str, owner: str) -> StreamSession:
        """The session ``sid`` for ``owner`` (live, or ended and not yet collected or dropped); a missing id, a wrong
        owner and a collected or dropped session alike raise ``unknown_session``."""
        self._sweep()
        with self._lock:
            session = self._sessions.get(sid) if isinstance(sid, str) else None
        if session is None or not session.owned_by(owner):
            raise StreamRefused("unknown_session")
        return session

    def __len__(self) -> int:
        """Live sessions (ended ones kept for reading are not counted)."""
        with self._lock:
            return self._live()

    # --- session callbacks ------------------------------------------------------------------------------------------
    def _ended(self, sid: str) -> None:
        with self._lock:
            if sid not in self._sessions:
                return
            self._ended_at[sid] = self._clock()
            dropped = []
            while len(self._ended_at) > self._max:
                old = next(iter(self._ended_at))
                del self._ended_at[old]
                dropped.append(self._sessions.pop(old))
        for session in dropped:
            session._discard()

    def _collect(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)
            self._ended_at.pop(sid, None)

    def _add_unsettled(self, reservation_id: int, seconds: int) -> None:
        with self._lock:     # never dropped: a full backlog refuses new opens instead (bounded by that refusal)
            self._unsettled.append((reservation_id, seconds))

    # --- maintenance ------------------------------------------------------------------------------------------------
    def _sweep(self) -> None:
        """Drop ended sessions kept past the retention time."""
        now = self._clock()
        with self._lock:
            old = [sid for sid, at in self._ended_at.items() if now - at > self._retain]
            dropped = [self._sessions.pop(sid) for sid in old]
            for sid in old:
                del self._ended_at[sid]
        for session in dropped:
            session._discard()

    def _retry_unsettled(self) -> None:
        """Retry failed settles oldest first; the first that fails again stops the pass (the ledger is still unusable)
        and it and everything after it wait, in order, for the next reap."""
        with self._lock:
            todo, self._unsettled = self._unsettled, []
        done = 0
        for reservation_id, seconds in todo:
            try:
                end_reservation(self._meter, reservation_id, seconds)
            except Exception:     # noqa: BLE001 - logged once when it first failed; it waits for the next reap
                break
            done += 1
        if done < len(todo):
            with self._lock:
                self._unsettled[:0] = todo[done:]

    def reap(self) -> int:
        """End every live session past its idle or age limit (each re-checked under its own lock, settling each), drop
        ended sessions kept past the retention time, and retry failed settles; returns how many sessions it ended."""
        with self._lock:
            live = [s for sid, s in self._sessions.items() if sid not in self._ended_at]
        n = 0
        for session in live:
            try:
                n += session._end_if_expired()
            except Exception as exc:     # noqa: BLE001
                _log.warning("voice stream: reap failed (%s)", type(exc).__name__)
        self._sweep()
        self._retry_unsettled()
        return n

    def shutdown(self) -> None:
        """The server is stopping: refuse new opens, end every live session (settling each), retry failed settles once,
        and drop every session."""
        with self._lock:
            self._closed = True
            live = [s for sid, s in self._sessions.items() if sid not in self._ended_at]
        for session in live:
            try:
                session._end_now("stopping")
            except Exception as exc:     # noqa: BLE001
                _log.warning("voice stream: shutdown close failed (%s)", type(exc).__name__)
        self._retry_unsettled()
        with self._lock:
            dropped = list(self._sessions.values())
            self._sessions.clear()
            self._ended_at.clear()
        for session in dropped:
            session._discard()
