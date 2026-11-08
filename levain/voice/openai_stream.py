"""levain.voice.openai_stream: the OpenAI Realtime transcription adapter for the cockpit mic (slice S5a).

:class:`OpenAIStream` implements ``stream_session.ProviderStream`` (send_audio / poll / close) over one
:mod:`levain.voice.wsclient` connection to ``wss://api.openai.com/v1/realtime?intent=transcription``. The page sends PCM16
16 kHz mono; the adapter resamples it to 24 kHz (what the Realtime API takes), base64-encodes it into
``input_audio_buffer.append`` events and folds the transcription events back into a final-so-far text plus the newest partial.

Differences from the Deepgram adapter that shape the code (everything else is modelled on it, rule for rule):

* **No server-side turns.** The session is configured with ``turn_detection: null`` (the transcription model supports
  neither server_vad nor semantic_vad), so nothing tells the server where an utterance ends. The adapter COMMITS the buffer
  itself every :data:`COMMIT_EVERY_BYTES` of audio sent (about 2 s) and once more at close(). Each commit makes one
  "item"; its ``...transcription.delta`` events build that item's partial and its ``...transcription.completed`` event is
  its final. A commit can cut a word in two (accepted, a 2 s window is the price of having partials at all).
* **Completions may arrive out of order** (the docs say to match them by ``item_id``). Items are kept in COMMIT order and
  :meth:`OpenAIStream.poll` joins the completed ones in that order; the partial is the delta text of the newest item still
  waiting. A completed item that arrives before an older pending one shows up in ``final`` ahead of it being complete, the
  older one is slotted into place once it completes (``final`` is "completed so far", not append-only).
* **Resampling 16 kHz to 24 kHz** is linear interpolation (ratio 3/2, exact integer arithmetic), streamed across chunk
  boundaries (a stray odd byte and the last sample are carried). Linear is fine for speech recognition here; it is not an
  audio-quality resampler.
* **No keepalive** (nothing in the docs asks for one) and no CloseStream message: close() commits whatever is buffered
  (zero-padded up to the 100 ms the API requires, so a short tail is not dropped), waits up to ``close_drain`` for every
  commit to be completed, then closes the socket. Only "every commit resolved" is success; anything else is
  ``failed=True`` with reason ``incomplete`` and the text so far is still returned.

Rules, kept from the Deepgram adapter:

1. **The key is revealed once**, into the ``Authorization: Bearer`` header handed to ``wsclient.connect``, in a frame that
   lets go of it before returning. It is never an attribute, never in a ``repr``, an exception frame, a log line or a
   frame sent to OpenAI. ``StreamNotStarted`` means nothing could have reached OpenAI (a refused URL or header, or a refused
   TCP connect); every other open failure is an :class:`OpenAIStreamError` and counts as started (wsclient reports a
   timeout at either phase with the same code, so a timeout settles one unit, the conservative side).
2. **One reader thread** is the only socket reader. It folds each message straight into the text state under a short lock;
   text is bounded (:data:`MAX_TEXT_CHARS` for the finals together and for any one partial, :data:`MAX_ITEMS` items).
3. **One send discipline.** Every write and the ``closing`` flip happen under one send lock, so no audio goes out after the
   final commit. An empty or over-:data:`SEND_MAX` chunk is refused with ``code="chunk"`` and the stream lives on.
4. **Nothing from the provider is echoed.** Malformed JSON, an unexpected shape, a binary frame, an ``error`` event, a
   ``...transcription.failed`` event, a non-normal close all end in ``failed=True`` with a fixed code in :data:`WORDS`.
5. **close()** returns within :func:`close_bound` seconds, is idempotent, concurrent calls return the same text, and stops
   the reader before it closes the socket.

Sources (read 2026-11-07): https://developers.openai.com/api/docs/guides/realtime-transcription (URL with
``intent=transcription``; ``session.update`` with ``session.type "transcription"``, ``audio.input.format
{type: "audio/pcm", rate: 24000}``, ``audio.input.transcription.model``, ``audio.input.turn_detection: null``;
``input_audio_buffer.append {audio}`` / ``.commit``; the delta / completed events and item_id matching) and
https://developers.openai.com/api/reference/resources/realtime/server-events (the ``error`` event shape). The full list of
what could not be verified is in ``project_memory/1107-15-voice/OPENAI_REALTIME_NOTES.md`` and RETURN_micS5oa.md.
"""
from __future__ import annotations

import array
import base64
import json
import logging
import sys
import threading
import time
from typing import Any

from levain.voice import providers, wsclient
from levain.voice.stream_session import ProviderStream, StreamNotStarted, StreamUpdate  # noqa: F401

_log = logging.getLogger("levain.voice.openai_stream")

HOST = providers.HOSTS["openai"]
URL_BASE = f"wss://{HOST}/v1/realtime?intent=transcription"
TRANSCRIBE_MODEL = "gpt-live-transcribe"      # UNVERIFIED against a live key (named on the docs page)
IN_RATE, OUT_RATE = 16000, 24000
COMMIT_EVERY_BYTES = 2 * OUT_RATE * 2         # ~2 s of 24 kHz PCM16 between commits
MIN_COMMIT_BYTES = OUT_RATE * 2 // 10         # the API refuses a commit under 100 ms
TICK_S = 1.0                                  # the socket idle timeout: the reader wakes at least this often
CLOSE_DRAIN_S = 5.0                           # the last commit's transcription is awaited this long
CLOSE_WAIT_S = 0.5
MAX_TEXT_CHARS = 64 * 1024
MAX_ITEMS = 1024
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


def close_bound(close_drain: float = CLOSE_DRAIN_S, tick: float = TICK_S) -> float:
    """The longest close() takes: the send-lock wait, the drain, the reader's stop, the socket close and a last join."""
    return 2 * tick + close_drain + (2 * tick + 0.5) + 2 * CLOSE_WAIT_S


class OpenAIStreamError(Exception):
    """A fixed-words failure. ``code`` is a key of :data:`WORDS`; the message is that sentence and nothing else."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(WORDS[code])


class Resampler16to24:
    """Streaming linear 16 kHz -> 24 kHz PCM16 mono resampler. Output sample k sits at input position 2k/3, so the
    fraction is 0, 1/3 or 2/3 and the arithmetic is exact integers. Carries one odd byte and the last sample across calls."""

    def __init__(self) -> None:
        self._odd = b""
        self._last = 0
        self._n = 0          # input samples consumed
        self._k = 0          # next output index

    def feed(self, pcm: bytes) -> bytes:
        data = self._odd + bytes(pcm)
        usable = len(data) - (len(data) % 2)
        self._odd = data[usable:]
        if not usable:
            return b""
        s = array.array("h")
        s.frombytes(data[:usable])
        if sys.byteorder == "big":
            s.byteswap()
        n0, m = self._n, len(s)
        out = array.array("h")
        k, last = self._k, self._last
        while True:
            i0, f = (2 * k) // 3, (2 * k) % 3
            if i0 + (1 if f else 0) > n0 + m - 1:
                break
            a = last if i0 == n0 - 1 else s[i0 - n0]
            if f:
                b = s[i0 + 1 - n0]
                v = (a * (3 - f) + b * f) // 3
            else:
                v = a
            out.append(v)
            k += 1
        self._k, self._last, self._n = k, s[-1], n0 + m
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


def _parse(raw: str) -> tuple[str, str, str]:
    """One server text message -> (event, item_id, text). Events: committed, delta, completed, ignore. Raises ValueError on
    any malformed message and LookupError on a reported error (an ``error`` event or a failed transcription); the caller
    maps both to fixed codes."""
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError
    kind = msg["type"]
    if kind == "error" or kind.endswith(".failed"):
        raise LookupError
    if kind == "input_audio_buffer.committed":
        item = msg.get("item_id")
        if not isinstance(item, str):
            raise ValueError
        return "committed", item, ""
    if kind == "conversation.item.input_audio_transcription.delta":
        item, text = msg.get("item_id"), msg.get("delta")
        if not isinstance(item, str) or not isinstance(text, str):
            raise ValueError
        return "delta", item, text
    if kind == "conversation.item.input_audio_transcription.completed":
        item, text = msg.get("item_id"), msg.get("transcript")
        if not isinstance(item, str) or not isinstance(text, str):
            raise ValueError
        return "completed", item, text.strip()
    return "ignore", "", ""        # session.created / updated, speech events, and anything newer


class OpenAIStream:
    """One live OpenAI Realtime transcription stream. Build it with :func:`factory`; the secret is not kept."""

    def __init__(self, secret: Any, *, base_url: str = URL_BASE, connect_opts: dict[str, Any] | None = None,
                 close_drain: float = CLOSE_DRAIN_S, tick: float = TICK_S,
                 commit_every: int = COMMIT_EVERY_BYTES) -> None:
        self._close_drain = close_drain
        self._tick = tick
        self._commit_every = commit_every
        self._order: list[str] = []                   # item ids in commit order
        self._items: dict[str, list[Any]] = {}        # id -> [delta text, completed text or None]
        self._chars = 0
        self._resolved = 0
        self.reason = ""
        self._failed = threading.Event()
        self._ended = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()                 # the text state + reason + resolved
        self._send_lock = threading.Lock()            # every write, the commit counters and the closing flip
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._final_committed = False                 # the closing commit is counted (or none was needed)
        self._commits = 0
        self._uncommitted = 0                         # bytes of 24 kHz audio appended since the last commit
        self._resampler = Resampler16to24()
        self._ws: wsclient.WebSocketClient | None = None
        opts = dict(connect_opts or {})
        opts.setdefault("allowed_hosts", providers.ALLOWED_HOSTS)
        opts.setdefault("idle_timeout", self._tick)
        err = self._open(secret, base_url, opts)
        del secret
        if err:
            raise StreamNotStarted() if err == "not_started" else OpenAIStreamError("connect")
        if not self._configure():
            raise OpenAIStreamError("connect")
        self._reader = threading.Thread(target=self._read_loop, name="openai-stream-reader", daemon=True)
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
            raise OpenAIStreamError("connect")

    def __repr__(self) -> str:
        return "<OpenAIStream>"

    def _open(self, secret: Any, url: str, opts: dict[str, Any]) -> str:
        """Connect with the key in the header. Returns '' on success, else 'not_started' or 'failed'. Never raises, so no
        frame of a raise holds the header."""
        code = ""
        try:
            headers = {"Authorization": f"Bearer {secret.reveal()}"}
            self._ws = wsclient.connect(url, headers, **opts)
        except wsclient.WsError as e:
            code = "not_started" if e.code in ("url", "header", "connect") else "failed"
        except Exception:  # noqa: BLE001
            code = "failed"
        headers = None  # noqa: F841 - the header dict (and the key) leave this frame
        return code

    def _configure(self) -> bool:
        """Send the session.update that makes this a PCM16 24 kHz, no-turn-detection transcription session."""
        update = {"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": OUT_RATE},
            "transcription": {"model": TRANSCRIBE_MODEL},
            "turn_detection": None}}}}
        assert self._ws is not None
        try:
            self._ws.send_text(json.dumps(update))
            return True
        except Exception:  # noqa: BLE001
            ws, self._ws = self._ws, None
            try:
                ws.close(wait=0)
            except Exception:  # noqa: BLE001
                pass
            return False

    # --- reader thread -------------------------------------------------------------------------------------------------
    def _fail(self, reason: str) -> None:
        with self._lock:
            if not self.reason:
                self.reason = reason
        self._failed.set()
        self._ended.set()
        self._stop.set()
        _log.warning("voice stream: openai stream failed (%s)", reason)

    def _take(self, event: str, item: str, text: str) -> bool:
        """Fold one event into the item state. False (and the stream failed) past a bound."""
        with self._lock:
            rec = self._items.get(item)
            if rec is None:
                if len(self._order) >= MAX_ITEMS:
                    ok = False
                else:
                    rec = self._items[item] = ["", None]
                    self._order.append(item)
                    ok = True
            else:
                ok = True
            if ok and event == "delta" and rec[1] is None:
                ok = len(rec[0]) + len(text) <= MAX_TEXT_CHARS
                if ok:
                    rec[0] += text
            elif ok and event == "completed" and rec[1] is None:
                ok = self._chars + len(text) + 1 <= MAX_TEXT_CHARS
                if ok:
                    self._chars += len(text) + 1
                    rec[1] = text
                    rec[0] = ""
                    self._resolved += 1
        if not ok:
            self._fail("overflow")
        return ok

    def _check_end(self) -> None:
        if self._final_committed:
            with self._lock:
                done = self._resolved >= self._commits
            if done:
                self._ended.set()

    def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        while not self._stop.is_set():
            try:
                kind, data = ws.recv()
            except wsclient.WsTimeout:
                continue
            except wsclient.WsError:
                if not self._ended.is_set():
                    self._fail("incomplete" if self._closing else "provider_closed")
                return
            if kind == "close":
                if self._ended.is_set():
                    return
                self._fail("provider_closed")
                return
            if kind != "text" or not isinstance(data, str):
                self._fail("malformed")
                return
            try:
                event, item, text = _parse(data)
            except ValueError:
                self._fail("malformed")
                return
            except LookupError:
                self._fail("provider_error")
                return
            if event != "ignore" and not self._take(event, item, text):
                return
            self._check_end()

    # --- ProviderStream ------------------------------------------------------------------------------------------------
    def _commit_locked(self) -> None:
        """Send one commit (send lock held). Raises WsError."""
        assert self._ws is not None
        self._commits += 1
        self._ws.send_text('{"type": "input_audio_buffer.commit"}')
        self._uncommitted = 0

    def send_audio(self, pcm: bytes) -> None:
        with self._send_lock:
            if self._closing:
                raise OpenAIStreamError("closed")
            if self._failed.is_set():
                raise OpenAIStreamError(self.reason or "provider_closed")
            if not pcm or len(pcm) > SEND_MAX:
                raise OpenAIStreamError("chunk")
            assert self._ws is not None
            ok = True
            try:
                out = self._resampler.feed(bytes(pcm))
                if out:
                    frame = {"type": "input_audio_buffer.append", "audio": base64.b64encode(out).decode("ascii")}
                    self._ws.send_text(json.dumps(frame))
                    self._uncommitted += len(out)
                    if self._uncommitted >= self._commit_every:
                        self._commit_locked()
            except Exception:  # noqa: BLE001 - nothing from the exception is kept
                ok = False
            if ok:
                return
        self._fail("send")
        raise OpenAIStreamError("send")

    def poll(self) -> StreamUpdate:
        with self._lock:
            done = [self._items[i][1] for i in self._order if self._items[i][1]]
            partial = ""
            for i in reversed(self._order):
                if self._items[i][1] is None:
                    partial = self._items[i][0]
                    break
            return StreamUpdate(partial=partial, final=" ".join(done), failed=self._failed.is_set())

    def close(self) -> str:
        with self._close_lock:
            if self._closed:
                return self._text()
            ws = self._ws
            assert ws is not None
            locked = self._send_lock.acquire(timeout=2 * self._tick)
            try:
                self._closing = True
                if locked and not self._failed.is_set():
                    try:
                        if self._uncommitted:
                            if self._uncommitted < MIN_COMMIT_BYTES:     # the API refuses under 100 ms: pad with silence
                                pad = bytes(MIN_COMMIT_BYTES - self._uncommitted)
                                ws.send_text(json.dumps({"type": "input_audio_buffer.append",
                                                         "audio": base64.b64encode(pad).decode("ascii")}))
                            self._commit_locked()
                        self._final_committed = True
                    except wsclient.WsError:
                        pass
            finally:
                if locked:
                    self._send_lock.release()
            self._check_end()
            if not self._final_committed or not self._ended.wait(self._close_drain):
                if not self._failed.is_set():
                    self._fail("incomplete")
            self._stop.set()
            self._reader.join(2 * self._tick + 0.5)
            ws.close(wait=CLOSE_WAIT_S)
            self._reader.join(CLOSE_WAIT_S)
            self._closed = True
            return self._text()

    def _text(self) -> str:
        with self._lock:
            parts = [self._items[i][1] for i in self._order if self._items[i][1]]
            for i in reversed(self._order):
                if self._items[i][1] is None:
                    if self._items[i][0]:
                        parts.append(self._items[i][0])
                    break
            return " ".join(parts)


def factory(secret: Any) -> OpenAIStream:
    """What the wiring step passes to ``StreamRegistry.open``: ``factory(secret) -> OpenAIStream``. Raises
    :class:`StreamNotStarted` when nothing could have reached OpenAI, :class:`OpenAIStreamError` otherwise."""
    return OpenAIStream(secret)
