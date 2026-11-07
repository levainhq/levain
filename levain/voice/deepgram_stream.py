"""levain.voice.deepgram_stream: the Deepgram live-streaming adapter for the cockpit mic (slice S2b).

:class:`DeepgramStream` implements ``stream_session.ProviderStream`` (send_audio / poll / close) over one
:mod:`levain.voice.wsclient` connection to ``wss://api.deepgram.com/v1/listen``. Raw PCM16 16 kHz mono goes up as binary
frames; Deepgram's JSON ``Results`` come back and are folded into a final-so-far text plus the newest partial.

Rules, in the order they matter:

1. **The key is revealed once**, into the ``Authorization: Token`` header handed to ``wsclient.connect``, in a frame that
   lets go of it before returning. It is never an attribute of the stream, never in a ``repr``, never in an exception
   frame. :func:`factory` is what S3 passes to ``StreamRegistry.open``; it receives the Secret and calls ``reveal()``
   inline.
2. **One reader thread** is the only socket reader. It parses each message and puts a small tuple on a bounded queue;
   :meth:`DeepgramStream.poll` (called by the session, under its lock) drains the queue into the text state and never
   blocks. The same thread sends ``KeepAlive`` when the line has been idle, at most :data:`MAX_KEEPALIVES` times in a row.
3. **Nothing from the provider is echoed.** Malformed JSON, an unexpected shape, a binary frame, an error message, a
   close with a non-normal code, an over-long transcript or a full queue all end in ``failed=True`` with a fixed reason
   code in :data:`WORDS`; no provider text reaches a log line, an exception or a reason.
4. **close()** sends ``CloseStream``, waits for Deepgram's closing ``Metadata`` (or its close frame) up to
   :data:`CLOSE_DRAIN_S`, folds what arrived, closes the socket, joins the reader and returns the text: every final plus
   a partial that never got finalised (speech is not dropped because the deadline hit). Idempotent.

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
import queue
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
CLOSE_DRAIN_S = 3.0
QUEUE_MAX = 256
MAX_TEXT_CHARS = 64 * 1024      # finals kept; past it the stream fails rather than grow
SEND_MAX = 64 * 1024

WORDS = {
    "malformed": "The voice provider sent a message Levain could not read.",
    "provider_error": "The voice provider reported an error.",
    "provider_closed": "The voice provider closed the stream.",
    "overflow": "The voice provider sent more than Levain keeps.",
    "send": "The audio could not be sent to the voice provider.",
    "connect": "Levain could not open the voice provider's stream.",
    "closed": "That stream has ended.",
}


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
                 keepalive_after: float = KEEPALIVE_AFTER_S, close_drain: float = CLOSE_DRAIN_S) -> None:
        self._keepalive_after = keepalive_after
        self._close_drain = close_drain
        self._q: queue.Queue[tuple[str, str]] = queue.Queue(QUEUE_MAX)
        self._finals: list[str] = []
        self._partial = ""
        self._chars = 0
        self.reason = ""                      # a key of WORDS once failed, else ""
        self._failed = threading.Event()
        self._ended = threading.Event()       # Deepgram's Metadata / close was seen
        self._stop = threading.Event()
        self._closing = False
        self._closed = False
        self._lock = threading.Lock()         # guards the text state + reason
        self._last_send = time.monotonic()
        self._ws: wsclient.WebSocketClient | None = None
        opts = dict(connect_opts or {})
        opts.setdefault("allowed_hosts", providers.ALLOWED_HOSTS)
        opts.setdefault("idle_timeout", keepalive_after)
        err = self._open(secret, stream_url(base_url), opts)
        del secret
        if err:
            raise StreamNotStarted() if err == "not_started" else DeepgramStreamError("connect")
        self._reader = threading.Thread(target=self._read_loop, name="deepgram-stream-reader", daemon=True)
        self._reader.start()

    def __repr__(self) -> str:
        return "<DeepgramStream>"

    def _open(self, secret: Any, url: str, opts: dict[str, Any]) -> str:
        """Connect with the key in the header. Returns '' on success, else 'not_started' (refused before any byte was
        sent) or 'failed' (a connection may have been made). Never raises, so no frame of a raise holds the header."""
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

    def _put(self, item: tuple[str, str]) -> bool:
        try:
            self._q.put(item, timeout=1.0)
            return True
        except queue.Full:
            self._fail("overflow")
            return False

    def _read_loop(self) -> None:
        ws = self._ws
        keepalives = 0
        while not self._stop.is_set() and ws is not None:
            fail = ""
            event = ("ignore", "")
            try:
                kind, data = ws.recv()
            except wsclient.WsTimeout:
                kind, data = "idle", None
            except wsclient.WsError:
                kind, data = "gone", None
            if kind == "idle":
                if not self._closing and keepalives < MAX_KEEPALIVES and \
                        time.monotonic() - self._last_send >= self._keepalive_after:
                    try:
                        ws.send_text('{"type": "KeepAlive"}')
                        keepalives += 1
                    except wsclient.WsError:
                        pass
                continue
            if kind == "gone":
                if not self._closing:
                    fail = "provider_closed"
                else:
                    self._ended.set()
                    return
            elif kind == "close":
                code = data[0] if isinstance(data, tuple) and data else 0
                if self._closing and code in (1000, 1005):
                    self._ended.set()
                    return
                fail = "provider_closed"
            elif kind != "text" or not isinstance(data, str):
                fail = "malformed"
            else:
                try:
                    event = _parse(data)
                except ValueError:
                    fail = "malformed"
                except LookupError:
                    fail = "provider_error"
            if fail:
                self._fail(fail)
                return
            keepalives = 0 if event[0] in ("partial", "final") else keepalives
            if event[0] == "end":
                if self._closing:
                    self._put(event)
                    self._ended.set()
                    return
            elif event[0] != "ignore":
                if not self._put(event):
                    return

    # --- ProviderStream ------------------------------------------------------------------------------------------------
    def _fold(self) -> None:
        """Move queued messages into the text state (caller: poll/close, any thread; the lock serialises)."""
        with self._lock:
            while True:
                try:
                    ev, text = self._q.get_nowait()
                except queue.Empty:
                    return
                if ev == "partial":
                    self._partial = text
                elif ev == "final":
                    self._partial = ""
                    if text:
                        self._chars += len(text) + 1
                        if self._chars > MAX_TEXT_CHARS:
                            if not self.reason:
                                self.reason = "overflow"
                            self._failed.set()
                            self._stop.set()
                            return
                        self._finals.append(text)

    def send_audio(self, pcm: bytes) -> None:
        if self._closed or self._closing:
            raise DeepgramStreamError("closed")
        if self._failed.is_set():
            raise DeepgramStreamError("provider_closed" if not self.reason else self.reason)
        ok = False
        try:
            if not pcm or len(pcm) > SEND_MAX:
                raise wsclient.WsError("too_big")
            assert self._ws is not None
            self._ws.send_binary(bytes(pcm))
            ok = True
        except Exception:  # noqa: BLE001 - nothing from the exception is kept
            ok = False
        if not ok:
            self._fail("send")
            raise DeepgramStreamError("send")
        self._last_send = time.monotonic()

    def poll(self) -> StreamUpdate:
        self._fold()
        with self._lock:
            return StreamUpdate(partial=self._partial, final=" ".join(self._finals),
                                failed=self._failed.is_set())

    def close(self) -> str:
        if self._closed:
            return self._text()
        self._closed = True
        ws = self._ws
        if ws is not None and not self._failed.is_set():
            self._closing = True
            try:
                ws.send_text('{"type": "CloseStream"}')
                self._ended.wait(self._close_drain)
            except Exception:  # noqa: BLE001
                pass
        self._stop.set()
        self._fold()
        if ws is not None:
            ws.close(wait=0.5)
        self._reader.join(2.0)
        self._fold()
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
