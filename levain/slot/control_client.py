"""The client half of ``levain-control/1`` (CONTRACT §3.1, §3.3, §11 V23): the runtime helper, the
supervisor and the reaper speak to CONTROL through it.

One :class:`ControlClient` is one unix-socket connection, and one request is in flight on it at a time
(the wire has no pipelining). The framing is :mod:`levain.slot.control`'s own (:func:`encode_frame`,
:func:`read_frame`), so the two halves cannot disagree on it.

- An answer ``{"ok":true,…}`` returns its fields without ``id`` / ``ok``.
- ``{"ok":false,"refused":<name>}`` raises :class:`levain.slot.Refused` with that name, and the connection
  stays usable, except ``locked`` (§3.3: RETRYABLE by its caller), which is retried on the same
  connection with backoff until V23 has passed since the first attempt, and only then raised.
- A connection that cannot be made, is closed, times out mid-answer or answers off the wire's shape is
  ``Refused("control-down")``, and the client closes it (a stream that lost an answer cannot be
  resynchronised). On a lease connection that close IS the lease's end (§3.1): the session it bound is
  revoked by CONTROL.

The CONNECTION is the lease (§3.1): :meth:`ControlClient.open_lease` answers the nonce and the caller
keeps this object open for the session's life; :meth:`ControlClient.fileno` lets the supervisor assert
that the descriptor never reaches a child.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Union

from levain.slot import Refused
from levain.slot.control import FrameMalformed, FrameTooLarge, encode_frame, read_frame

#: CONTRACT §11 V23: the window a ``locked`` refusal is retried in.
V23_SECONDS = 10.0

#: DECISION: the ``locked`` retry's backoff (§3.3 fixes only "retryable"; V23 bounds the total).
LOCKED_RETRY_FIRST_S = 0.01
LOCKED_RETRY_MAX_S = 0.5

_RESERVED = frozenset({"v", "op", "id"})


class ControlClient:
    """One connection to CONTROL at ``path``. ``timeout`` bounds the connect and each answer.

    ``retry_window_s`` (V23), ``clock`` and ``sleep`` are attributes so a test can shorten the window
    without the signature growing."""

    retry_window_s: float = V23_SECONDS
    clock: Callable[[], float] = staticmethod(time.monotonic)
    sleep: Callable[[float], None] = staticmethod(time.sleep)

    def __init__(self, path: Path | str, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.lease_nonce: str | None = None
        self._mu = threading.Lock()
        self._next_id = 0
        # SOCK_CLOEXEC sets close-on-exec atomically where the OS has it (Linux). macOS has no such flag:
        # Python marks every socket non-inheritable right after socket() (PEP 446), and the supervisor's
        # fileno() assertion is the check that holds on both.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0))
        try:
            sock.settimeout(timeout)
            sock.connect(str(self.path))
        except OSError as e:
            sock.close()
            raise Refused("control-down", f"{self.path}: {e.strerror or e}") from None
        self._sock: socket.socket | None = sock

    # -- the connection --------------------------------------------------------------------------------

    def fileno(self) -> int:
        sock = self._sock
        if sock is None:
            raise Refused("control-down", "closed")
        return sock.fileno()

    def close(self) -> None:
        """Close the connection (on a lease connection: end the lease). Idempotent."""
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def __enter__(self) -> "ControlClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _down(self, detail: str) -> Refused:
        self.close()
        return Refused("control-down", detail)

    # -- one exchange ----------------------------------------------------------------------------------

    def _exchange(self, op: str, fields: dict[str, object]) -> dict:
        """Send one request and read its answer (caller holds ``_mu``). Any break = ``control-down``."""
        sock = self._sock
        if sock is None:
            raise Refused("control-down", "closed")
        self._next_id += 1
        rid = self._next_id
        try:
            frame = encode_frame({**fields, "v": 1, "op": op, "id": rid})
        except FrameTooLarge:
            raise Refused("frame-too-large", op) from None   # never sent: the connection stays good
        try:
            sock.sendall(frame)
            ans = read_frame(sock)
        except (OSError, FrameMalformed, FrameTooLarge) as e:
            raise self._down(f"{op}: {e}") from None
        if ans is None:
            raise self._down(f"{op}: EOF")
        okv = ans.get("ok")
        if okv is True and ans.get("id") == rid:
            return ans
        if okv is False and type(ans.get("refused")) is str and ans.get("id") in (rid, None):
            # An over-cap frame is refused with id null (its id was never read), then the server closes.
            if ans.get("id") is None:
                self.close()
            return ans
        raise self._down(f"{op}: an answer off the wire's shape")

    def call(self, op: str, **fields: object) -> dict:
        """One verb. Returns the answer's fields; raises ``Refused(<name>)`` on a refusal (``locked``
        only after V23 of retries) and ``Refused("control-down")`` on a broken connection."""
        bad = _RESERVED & set(fields)
        if bad:
            raise Refused("bad-request", f"{sorted(bad)} are the frame's own keys")
        with self._mu:
            deadline = self.clock() + self.retry_window_s
            delay = LOCKED_RETRY_FIRST_S
            while True:
                ans = self._exchange(op, fields)
                if ans["ok"] is True:
                    return {k: v for k, v in ans.items() if k not in ("id", "ok")}
                name = ans["refused"]
                left = deadline - self.clock()
                if name != "locked" or left <= 0:
                    raise Refused(name, op)
                self.sleep(min(delay, left))
                delay = min(delay * 2, LOCKED_RETRY_MAX_S)

    def open_lease(self) -> str:
        """``LEASE-OPEN`` (§3.1 step 1): this connection becomes the lease; returns its nonce. Keep this
        object open for as long as the session lives: its close is the lease's EOF."""
        ans = self.call("LEASE-OPEN")
        nonce = ans.get("lease_nonce")
        if type(nonce) is not str or not nonce:
            raise self._down("LEASE-OPEN answered no lease_nonce")
        self.lease_nonce = nonce
        return nonce


# -- SESSION-EVENTS: the stream (CONTRACT §3.6) ---------------------------------------------------------------------

@dataclass(frozen=True)
class Hello:
    """The stream's opening. ``same_journal`` is None when the caller named no ``journal_id``, else whether the hello's
    is the one named (False: the caller's cursor is another journal's and must restart from 0)."""
    journal_id: str
    oldest_seq: int
    newest_seq: int
    same_journal: bool | None


@dataclass(frozen=True)
class Gap:
    """Events past the cursor were pruned before they could be sent; the stream continues from ``oldest_seq``."""
    oldest_seq: int


@dataclass(frozen=True)
class Reset:
    """The cursor is past this journal's newest seq (another journal's): the stream sent this and ended."""
    newest_seq: int


@dataclass(frozen=True)
class StreamError:
    """CONTROL ended the stream with ``{kind:"error", error}`` (a journal poisoned under the stream)."""
    error: str


@dataclass(frozen=True)
class Event:
    """One journal event (``levain-session-event/1``): ``body`` is the whole frame, ``seq`` and ``kind`` read from it."""
    seq: int
    kind: str
    body: dict = field(compare=False)


Frame = Union[Hello, Gap, Reset, StreamError, Event]

#: how an :class:`EventStream` ended (``outcome``); ``None`` while it can still yield
CLOSED = "closed"          # CONTROL closed the connection between frames (its stop, a stalled reader dropped)
RESET = "reset"            # a :class:`Reset` frame ended it
ERROR = "error"            # a :class:`StreamError` frame ended it
DEADLINE = "deadline"      # no frame within the caller's ``read_deadline``
BROKEN = "broken"          # a frame off the wire's shape, or a socket error mid-frame
CANCELLED = "cancelled"    # the caller closed it


class EventStream:
    """``SESSION-EVENTS`` on its own connection: iterating yields the :class:`Hello`, then :class:`Gap` /
    :class:`Reset` / :class:`StreamError` / :class:`Event` values in the order CONTROL sent them. The iterator ends on
    a reset, an error frame, EOF, the read deadline, a broken frame or :meth:`close`, and ``outcome`` names which.
    The caller never writes on the connection (CONTROL ends a stream on any byte from its peer); closing it cancels.
    There is no heartbeat: an idle stream sends nothing, so ``read_deadline`` (None: wait for ever) is the only
    timeout, and a caller reconnects from its own persisted cursor."""

    def __init__(self, sock: socket.socket, hello: Hello) -> None:
        self._sock: socket.socket | None = sock
        self.hello = hello
        self.outcome: str | None = None
        self._hello_due = True

    def __iter__(self) -> Iterator[Frame]:
        return self

    def __next__(self) -> Frame:
        if self._hello_due:
            self._hello_due = False
            return self.hello
        sock = self._sock
        if sock is None:
            raise StopIteration
        try:
            obj = read_frame(sock)
        except socket.timeout:
            self._end(DEADLINE)
            raise StopIteration from None
        except (OSError, FrameMalformed, FrameTooLarge):
            self._end(BROKEN)
            raise StopIteration from None
        if obj is None:
            self._end(CLOSED)
            raise StopIteration
        frame = _stream_frame(obj)
        if frame is None:
            self._end(BROKEN)
            raise StopIteration
        if isinstance(frame, Reset):
            self._end(RESET)
        elif isinstance(frame, StreamError):
            self._end(ERROR)
        return frame

    def _end(self, outcome: str) -> None:
        if self.outcome is None:
            self.outcome = outcome
        sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def close(self) -> None:
        """Cancel the stream (CONTROL sees the EOF). Idempotent; a stream already ended keeps its outcome."""
        self._hello_due = False
        self._end(CANCELLED)

    def __enter__(self) -> "EventStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _int(v: object) -> bool:
    return type(v) is int and v >= 0


def _stream_frame(obj: dict) -> Frame | None:
    """A frame read after the hello as its typed value; None for one off the wire's shape."""
    kind = obj.get("kind")
    if kind == "gap" and _int(obj.get("oldest_seq")):
        return Gap(obj["oldest_seq"])
    if kind == "reset" and _int(obj.get("newest_seq")):
        return Reset(obj["newest_seq"])
    if kind == "error" and type(obj.get("error")) is str:
        return StreamError(obj["error"])
    if type(kind) is str and kind not in ("hello", "gap", "reset", "error") and _int(obj.get("seq")):
        return Event(obj["seq"], kind, obj)
    return None


def follow_events(path: Path | str, after_seq: int, journal_id: str | None = None, *, timeout: float = 10.0,
                  read_deadline: float | None = None) -> EventStream:
    """Open ``SESSION-EVENTS {after_seq}`` on a new connection to CONTROL at ``path`` and read its hello (``timeout``
    bounds the connect and the hello). A refusal of the verb raises :class:`Refused` by its name, a connection that
    cannot be made or answers off the wire's shape ``Refused("control-down")``; either way the connection is closed.
    ``journal_id``: the journal the caller's cursor belongs to, compared with the hello's (:attr:`Hello.same_journal`).
    The connection then belongs to the returned :class:`EventStream`."""
    client = ControlClient(path, timeout=timeout)
    try:
        with client._mu:
            ans = client._exchange("SESSION-EVENTS", {"after_seq": after_seq})
        if ans["ok"] is not True:
            raise Refused(ans["refused"], "SESSION-EVENTS")
        jid, oldest, newest = ans.get("journal_id"), ans.get("oldest_seq"), ans.get("newest_seq")
        if ans.get("kind") != "hello" or type(jid) is not str or not _int(oldest) or not _int(newest):
            raise client._down("SESSION-EVENTS answered no hello")
        sock, client._sock = client._sock, None
        assert sock is not None
    except BaseException:
        client.close()
        raise
    sock.settimeout(read_deadline)
    return EventStream(sock, Hello(jid, oldest, newest, None if journal_id is None else jid == journal_id))


__all__ = ["BROKEN", "CANCELLED", "CLOSED", "ControlClient", "DEADLINE", "ERROR", "Event", "EventStream", "Frame",
           "Gap", "Hello", "LOCKED_RETRY_FIRST_S", "LOCKED_RETRY_MAX_S", "RESET", "Reset", "StreamError",
           "V23_SECONDS", "follow_events"]
