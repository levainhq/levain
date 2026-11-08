"""levain.voice.deepgram_stream: the Deepgram live-streaming adapter for the cockpit mic (slice S2b).

:class:`DeepgramStream` implements ``stream_session.ProviderStream`` (send_audio / poll / close) over one
:mod:`levain.voice.wsclient` connection to ``wss://api.deepgram.com/v1/listen``. Raw PCM16 16 kHz mono goes up as binary
frames; Deepgram's JSON ``Results`` come back and are folded into a final-so-far text plus the newest partial.

Rules, in the order they matter:

1. **The key is revealed once**, into the ``Authorization: Token`` header handed to ``wsclient.connect``, in a frame that
   lets go of it before returning. It is never an attribute of the stream, never in a ``repr``, never in an exception
   frame. :func:`factory` is what S3 passes to ``StreamRegistry.open``; it receives the Secret and calls ``reveal()``
   inline. ``StreamNotStarted`` means nothing could have reached Deepgram (a refused URL or header, or a refused TCP
   connect); every other open failure is a :class:`DeepgramStreamError` and counts as started. wsclient reports a TCP
   connect timeout and a timeout after the request went out with the same code, so a timeout counts as started: it
   settles one unit, the conservative side.
2. **One reader thread** is the only socket reader, start to end. It parses each message and folds it straight into the
   text state under a short lock: a partial replaces the previous partial (only the newest is kept), a final is appended.
   Both are bounded: the finals together by :data:`MAX_TEXT_CHARS`, a single partial by the same number; past either the
   stream fails. Nothing is queued, so memory is bounded by text, not by message count. :meth:`DeepgramStream.poll` reads
   the state and never blocks for longer than that fold. The reader wakes at least every ``tick`` seconds (the socket
   idle timeout) and sends ``KeepAlive`` when no audio has gone out for ``keepalive_after``, at most
   :data:`MAX_KEEPALIVES` in a row; a successful audio send resets the run.
3. **One send discipline.** Every write (audio, KeepAlive, CloseStream) and the ``closing`` flip happen under one send
   lock, so no audio and no KeepAlive goes out after CloseStream. An empty or over-:data:`SEND_MAX` chunk is refused with
   ``code="chunk"`` and the stream lives on.
4. **Nothing from the provider is echoed.** Malformed JSON, an unexpected shape, a binary frame, an error message, a
   close with a non-normal code or an over-long transcript all end in ``failed=True`` with a fixed reason code in
   :data:`WORDS`; no provider text reaches a log line, an exception or a reason.
5. **close()** sends ``CloseStream`` and waits up to ``close_drain`` for Deepgram's closing ``Metadata`` or its normal
   close frame (1000/1005): only those two are success. A reset, a stall inside a frame, a send that could not go out or
   the deadline passing first leaves ``failed=True`` with reason ``incomplete``. Either way close() returns every final
   plus a partial that never got finalised (speech is not dropped because the deadline hit); ``poll().failed`` says
   whether the provider confirmed the end. wsclient already serialises socket reads (one receive lock), but its close()
   reads, and drops, any frame it takes while the reader is between recv calls; so close() stops the reader first and
   only then closes the socket. Concurrent and repeated calls return the same text. close() returns within
   :func:`close_bound` seconds.

Sources (read 2026-11-07): https://developers.deepgram.com/docs/lower-level-websockets (Results fields is_final /
speech_final, channel.alternatives[].transcript; "no audio for roughly 10 seconds" ends the connection; KeepAlive is a
control, not a data, message) and https://developers.deepgram.com/reference/speech-to-text/listen-streaming (query
parameters and their defaults; the Finalize / CloseStream / KeepAlive messages are ``{"type": "<Name>"}``). UNVERIFIED
against a live key: the ``Token <key>`` header value form (the repo's prerecorded adapter uses it), the exact close code
Deepgram sends after CloseStream, whether the closing ``Metadata`` always arrives, the value ``endpointing=300``, and
the literal ``Error`` message type name (any unknown type that carries ``"error"`` in its type is treated as a failure).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from typing import Any

from levain.voice import providers, wsclient
from levain.voice.stream_session import ProviderStream, StreamNotStarted, StreamUpdate  # noqa: F401 - S1 (head merges first)

_log = logging.getLogger("levain.voice.deepgram_stream")

HOST = "api.deepgram.com"
URL_BASE = f"wss://{HOST}/v1/listen"
# Order is part of the contract (a test pins the string). mip_opt_out matches the prerecorded adapter.
QUERY = (
    ("model", providers.DEEPGRAM_STT_MODEL),
    ("encoding", "linear16"),
    ("sample_rate", "16000"),
    ("channels", "1"),
    ("interim_results", "true"),
    ("endpointing", "300"),
    ("smart_format", "true"),
    ("mip_opt_out", "true"),
)
KEEPALIVE_AFTER_S = 4.0         # idle this long (no audio sent) => KeepAlive; Deepgram drops the line near 10 s
MAX_KEEPALIVES = 6              # in a row without audio; past it the line is left to lapse (the session's idle limit ends it)
TICK_S = 1.0                    # the socket idle timeout: the reader wakes at least this often (also wsclient's mid-frame budget)
CLOSE_DRAIN_S = 3.0
CLOSE_WAIT_S = 0.5              # wsclient.close(): our close frame out, the peer's back
MAX_TEXT_CHARS = 64 * 1024      # finals kept, and the longest single partial; past either the stream fails rather than grow
SEND_MAX = 64 * 1024

WORDS = {
    "malformed": "The voice provider sent a message Levain could not read.",
    "provider_error": "The voice provider reported an error.",
    "provider_closed": "The voice provider closed the stream.",
    "overflow": "The voice provider sent more than Levain keeps.",
    "send": "The audio could not be sent to the voice provider.",
    "connect": "Levain could not open the voice provider's stream.",
    "closed": "That stream has ended.",
    "chunk": "That audio chunk was empty or too large.",
    "incomplete": "The voice provider's stream ended before it confirmed its last words.",
}

_KEEPALIVE = '{"type": "KeepAlive"}'
_CLOSESTREAM = '{"type": "CloseStream"}'


def close_bound(close_drain: float = CLOSE_DRAIN_S, tick: float = TICK_S) -> float:
    """The longest close() takes: the send-lock wait, the drain, the reader's stop (one idle wait, or a started frame's
    body budget plus wsclient's overrun of one more), the socket close and a last join."""
    return 2 * tick + close_drain + (2 * tick + 0.5) + 2 * CLOSE_WAIT_S


class DeepgramStreamError(Exception):
    """A fixed-words failure. ``code`` is a key of :data:`WORDS`; the message is that sentence and nothing else."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(WORDS[code])


def stream_url(base: str = URL_BASE) -> str:
    return base + "?" + urllib.parse.urlencode(QUERY)


def _parse(raw: str) -> tuple[str, str]:
    """One server text message -> (event, text). Events: partial, final, end, ignore. Raises ValueError on any
    malformed message and LookupError on a reported provider error; the caller maps both to fixed codes."""
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError
    kind = msg["type"]
    if kind == "Results":
        ch = msg.get("channel")
        alts = ch.get("alternatives") if isinstance(ch, dict) else None
        if not isinstance(alts, list) or not alts or not isinstance(alts[0], dict):
            raise ValueError
        text = alts[0].get("transcript")
        is_final = msg.get("is_final")
        if not isinstance(text, str) or not isinstance(is_final, bool):
            raise ValueError
        return ("final" if is_final else "partial"), text.strip()
    if kind == "Metadata":
        return "end", ""
    if "error" in kind.lower():
        raise LookupError
    return "ignore", ""      # UtteranceEnd, SpeechStarted, and anything newer: no text in them


class DeepgramStream:
    """One live Deepgram stream. Build it with :func:`factory`; the secret is not kept."""

    def __init__(self, secret: Any, *, base_url: str = URL_BASE, connect_opts: dict[str, Any] | None = None,
                 keepalive_after: float = KEEPALIVE_AFTER_S, close_drain: float = CLOSE_DRAIN_S,
                 tick: float = TICK_S) -> None:
        self._keepalive_after = keepalive_after
        self._close_drain = close_drain
        self._tick = min(tick, keepalive_after)
        self._finals: list[str] = []
        self._partial = ""
        self._chars = 0
        self.reason = ""                      # a key of WORDS once failed, else ""
        self._failed = threading.Event()
        self._ended = threading.Event()       # Deepgram's Metadata / normal close was seen, or the stream failed
        self._stop = threading.Event()        # the reader leaves at its next wake
        self._lock = threading.Lock()         # the text state + reason
        self._send_lock = threading.Lock()    # every write, the keepalive run and the closing flip
        self._close_lock = threading.Lock()   # one close() at a time; the rest wait and return the same text
        self._closing = False
        self._closed = False
        self._keepalives = 0
        self._last_write = time.monotonic()
        self._ws: wsclient.WebSocketClient | None = None
        opts = dict(connect_opts or {})
        opts.setdefault("allowed_hosts", providers.ALLOWED_HOSTS)
        opts.setdefault("idle_timeout", self._tick)
        err = self._open(secret, stream_url(base_url), opts)
        del secret
        if err:
            raise StreamNotStarted() if err == "not_started" else DeepgramStreamError("connect")
        self._reader = threading.Thread(target=self._read_loop, name="deepgram-stream-reader", daemon=True)
        started = False
        try:
            self._reader.start()
            started = True
        except Exception:  # noqa: BLE001 - e.g. RuntimeError("can't start new thread"); the socket must not leak
            pass
        if not started:
            ws, self._ws = self._ws, None
            if ws is not None:
                ws.close(wait=0)
            raise DeepgramStreamError("connect")

    def __repr__(self) -> str:
        return "<DeepgramStream>"

    def _open(self, secret: Any, url: str, opts: dict[str, Any]) -> str:
        """Connect with the key in the header. Returns '' on success, else 'not_started' (refused before any byte was
        sent) or 'failed' (a connection may have been made; a timeout is here, see rule 1). Never raises, so no frame of
        a raise holds the header."""
        code = ""
        try:
            headers = {"Authorization": f"Token {secret.reveal()}"}
            self._ws = wsclient.connect(url, headers, **opts)
        except wsclient.WsError as e:
            code = "not_started" if e.code in ("url", "header", "connect") else "failed"
        except Exception:  # noqa: BLE001
            code = "failed"
        headers = None  # noqa: F841 - the header dict (and the key) leave this frame
        return code

    # --- reader thread -------------------------------------------------------------------------------------------------
    def _fail(self, reason: str) -> None:
        with self._lock:
            if not self.reason:
                self.reason = reason
        self._failed.set()
        self._ended.set()
        self._stop.set()
        _log.warning("voice stream: deepgram stream failed (%s)", reason)

    def _take(self, event: str, text: str) -> bool:
        """Fold one transcript into the text state. False (and the stream failed) past the text bound."""
        with self._lock:
            if event == "partial":
                ok = len(text) <= MAX_TEXT_CHARS
                if ok:
                    self._partial = text
            else:
                self._partial = ""
                ok = not text or self._chars + len(text) + 1 <= MAX_TEXT_CHARS
                if ok and text:
                    self._chars += len(text) + 1
                    self._finals.append(text)
        if not ok:
            self._fail("overflow")
        return ok

    def _keepalive(self) -> None:
        if self._closing or not self._send_lock.acquire(blocking=False):
            return                                   # a send in progress is not an idle line
        try:
            if self._closing or self._keepalives >= MAX_KEEPALIVES or \
                    time.monotonic() - self._last_write < self._keepalive_after:
                return
            assert self._ws is not None
            try:
                self._ws.send_text(_KEEPALIVE)
            except wsclient.WsError:
                return
            self._keepalives += 1
            self._last_write = time.monotonic()
        finally:
            self._send_lock.release()

    def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        while not self._stop.is_set():
            try:
                kind, data = ws.recv()
            except wsclient.WsTimeout:
                self._keepalive()
                continue
            except wsclient.WsError:
                self._fail("incomplete" if self._closing else "provider_closed")
                return
            if kind == "close":
                code = data[0] if isinstance(data, tuple) and data else 0
                if self._closing and code in (1000, 1005):
                    self._ended.set()
                    return
                self._fail("provider_closed")
                return
            if kind != "text" or not isinstance(data, str):
                self._fail("malformed")
                return
            try:
                event, text = _parse(data)
            except ValueError:
                self._fail("malformed")
                return
            except LookupError:
                self._fail("provider_error")
                return
            if event == "end":
                if self._closing:
                    self._ended.set()
                    return
            elif event != "ignore" and not self._take(event, text):
                return
            self._keepalive()

    # --- ProviderStream ------------------------------------------------------------------------------------------------
    def send_audio(self, pcm: bytes) -> None:
        with self._send_lock:
            if self._closing:
                raise DeepgramStreamError("closed")
            if self._failed.is_set():
                raise DeepgramStreamError(self.reason or "provider_closed")
            if not pcm or len(pcm) > SEND_MAX:
                raise DeepgramStreamError("chunk")
            assert self._ws is not None
            try:
                self._ws.send_binary(bytes(pcm))
                ok = True
            except Exception:  # noqa: BLE001 - nothing from the exception is kept
                ok = False
            if ok:
                self._keepalives = 0
                self._last_write = time.monotonic()
                return
        self._fail("send")
        raise DeepgramStreamError("send")

    def poll(self) -> StreamUpdate:
        with self._lock:
            return StreamUpdate(partial=self._partial, final=" ".join(self._finals),
                                failed=self._failed.is_set())

    def close(self) -> str:
        with self._close_lock:
            if self._closed:
                return self._text()
            ws = self._ws
            assert ws is not None
            sent = False
            # A writer stuck in a send holds the lock for at most about one socket timeout (tick).
            locked = self._send_lock.acquire(timeout=2 * self._tick)
            try:
                self._closing = True
                if locked and not self._failed.is_set():
                    try:
                        ws.send_text(_CLOSESTREAM)
                        sent = True
                    except wsclient.WsError:
                        pass
            finally:
                if locked:
                    self._send_lock.release()
            if not sent or not self._ended.wait(self._close_drain):
                if not self._failed.is_set():
                    self._fail("incomplete")
            self._stop.set()
            self._reader.join(2 * self._tick + 0.5)   # it wakes within one idle wait or one frame's budget
            ws.close(wait=CLOSE_WAIT_S)               # the reader is gone: nothing it would have read is taken
            self._reader.join(CLOSE_WAIT_S)
            self._closed = True
            return self._text()

    def _text(self) -> str:
        with self._lock:
            parts = list(self._finals)
            if self._partial:
                parts.append(self._partial)
            return " ".join(parts)


def factory(secret: Any) -> DeepgramStream:
    """What S3 passes to ``StreamRegistry.open``: ``factory(secret) -> DeepgramStream``. Raises
    :class:`StreamNotStarted` when nothing could have reached Deepgram, :class:`DeepgramStreamError` otherwise."""
    return DeepgramStream(secret)
