"""levain.coordinator.codex_wire — one demultiplexing JSON-RPC client for a Codex app-server (CH-D3 rev 2 §3.2; S1).

Codex offers a long-lived session only through ``codex app-server``: JSON-RPC objects WITHOUT the ``"jsonrpc"`` field,
either as WebSocket messages on the user's daemon control socket (:class:`WsFraming`, the framing
:mod:`levain.coordinator.codex_launch` has spoken since C3c, moved here) or one object per line over a pipe pair to a
process Levain spawns (:class:`LineFraming`). :class:`CodexWire` is the one client over either:

- **one reader thread** reads every inbound message and sorts it: a response (an id, no method) goes to the call waiting
  on it; a notification (a method, no id) and a server request (a method AND an id) go to ONE inbound queue, in arrival
  order, as :class:`Inbound` items (``obs`` is the arrival ordinal). A response must hold exactly one of ``result`` and
  ``error``; one that does not fails its call and is queued as ``unparseable``. A call made FROM the reader thread raises
  at once: its answer could only be read by the thread that is waiting on it. ``on_inbound`` runs only on the reader
  thread, after the call a frame unblocked is released. If the reader ends for ANY reason (an unexpected exception
  included) the connection is dead: every waiting call fails and a ``closed`` item is queued;
- **bounded writes**: every byte written (a message, a pong, the close frame) goes out under the framing's ONE write
  lock and within the sender's deadline (a call's own timeout covers its send), checked before the first byte and
  after every partial write. A send whose deadline passes before its first byte fails alone (the stream is intact; the
  reader's own ``-32600`` answer still closes the connection); one cut off part-way fails and closes the connection,
  since a partial message cannot be resumed. :meth:`CodexWire.close` is bounded the same way, and closes the
  connection's descriptors even under a stuck ``on_inbound``;
- **typed ids**: an id is kept as the exact JSON value (``0`` and ``"0"`` are different requests) and "has an id" is a
  key-presence test, never truthiness. A reply echoes the id as it came;
- **``gen``**: every connection mints a fresh random hex string, stamped on every item it queues. It is an opaque
  connect id: an item from another ``gen`` is never answered on this one. It carries NO order (the r2 review moves
  ordering to a persisted ordinal in rev 3);
- **the thread filter**: the wire serves ONE thread. A notification or server request naming another thread (its
  ``threadId``; the legacy ``applyPatchApproval`` / ``execCommandApproval`` name it ``conversationId``) is queued as a
  ``foreign`` item and can never be answered (answering could decide another thread's request). A method whose pinned
  schema requires that field and arrives without it is ``unparseable``. While ``thread/start`` is outstanding
  (:meth:`CodexWire.start_thread`, one at a time) the wire has no thread yet, so EVERY item that arrives is HELD with its
  arrival ordinal (at most :data:`BOOTSTRAP_FRAME_CAP`, and only until that call's deadline): the returned id is bound
  and the held items are released in arrival order, another thread's as ``foreign``. Past the cap a notification is
  dropped and counted; a server request closes the connection rather than lose its id;
- a message over :data:`_MAX_MESSAGE` has no recoverable id or kind, so it closes the connection as a protocol fault
  (as the bootstrap cap does) as soon as its size is known, never read through; so does a WebSocket frame that breaks
  RFC 6455 section 5 (:meth:`WsFraming.recv_message`); on the socket a frame that is not a JSON object is an ``unparseable`` item and
  the reader goes on; on a pipe a line that is not JSON is a log line (``codex app-server`` writes ANSI log lines to
  its stdout, measured over stdio in runs72), counted in :attr:`CodexWire.log_lines` and a running digest, never an item;
- :meth:`CodexWire.open` sends ``initialize`` (``experimentalApi: false`` and :data:`OPT_OUT_NOTIFICATIONS`), then the
  ``initialized`` notification; the wire sends no other request before that.

The module also holds, as data, the server-request registry (:data:`SERVER_REQUESTS`: what the session reads of each
request and the schema-valid reply that denies it, rev 2 §6.2 / §6.6) and the method surface pinned for the one tested
Codex version (:data:`PINNED_SURFACE`). The wire decides nothing with the registry: it never answers a request on its
own. Stdlib only.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import select
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

__all__ = ["BOOTSTRAP_FRAME_CAP", "CATCH_ALL", "NO_ID", "OPT_OUT_NOTIFICATIONS", "PINNED_SURFACE", "PINNED_VERSION",
           "RELIED_NOTIFICATIONS", "SERVER_REQUESTS", "CodexError", "CodexWire", "Inbound", "LineFraming", "ServerRequestRow", "WsFraming",
           "server_request_row"]

_CONNECT_TIMEOUT = 10.0
_CALL_TIMEOUT = 60.0
_MAX_MESSAGE = 16 * 1024 * 1024 + 4096     # the daemon's own header names 16777216 bytes unfragmented
_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BOOTSTRAP_FRAME_CAP = 256
_LATE_IDS_CAP = 1024
_READER_SEND_TIMEOUT = 2.0  # the reader's sends (pong, -32600): a pong whose lock was not got is skipped, any other failure ends the wire
_CLOSE_TIMEOUT = 1.0       # shutdown's wait for the write lock, then for its close frame
_WRITE_SLICE = 0.05         # a blocked write re-checks for shutdown this often


class CodexError(Exception):
    """The daemon could not be reached, or answered with an error (``.error`` holds its error object then)."""

    def __init__(self, message: str, error: Any = None) -> None:
        super().__init__(message)
        self.error = error


class _NotStarted(CodexError):
    """A send that ended before one byte of it went out (its deadline passed before or while it waited for the write
    lock or for the peer to take a first byte): the stream is intact."""


class _Oversize(CodexError):
    """A message over :data:`_MAX_MESSAGE`, raised as soon as its size is known: its bytes are NOT read past, so the
    stream is out of step and the connection must close."""

    def __init__(self, size: int) -> None:
        super().__init__(f"a message of at least {size} bytes, over {_MAX_MESSAGE}")
        self.size = size


# --- framings ----------------------------------------------------------------------------------------------------
def _masked(payload: bytes, mask: bytes) -> bytes:
    """``payload`` XORed with the 4-byte ``mask`` repeated (RFC 6455 section 5.3), as one big-integer XOR."""
    n = len(payload)
    if not n:
        return b""
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")


def _poll(fds: list[tuple[int, int]], timeout: float | None) -> set[int]:
    """The fds of ``fds`` (``(fd, events)``) that are ready within ``timeout`` seconds (None: no limit)."""
    p = select.poll()
    for fd, events in fds:
        p.register(fd, events)
    return {fd for fd, _ in p.poll(None if timeout is None else max(0, int(timeout * 1000)))}


class _Framing:
    """What both framings share once :meth:`arm`-ed by a :class:`CodexWire`: every byte written goes out under ONE
    write lock (:attr:`_wlock`) and within the writer's deadline; every read-side descriptor operation (the wait, the
    wake drain, the read) runs under ONE read lock (:attr:`_rlock`). The reader waits on the data and on a wake pipe,
    so :meth:`wake` runs the wire's ``on_wake`` on the reader thread and :meth:`shutdown` ends a reader whose peer
    never closes. Each side's descriptors are closed only by a thread holding that side's lock: :meth:`_side_close`
    closes them at once when the lock frees in time, else the holder closes them on its way out, so a descriptor is
    never closed under a thread that is using it and a late operation finds it gone instead of a reused number. A
    reader stuck in a callback holds neither lock, so it cannot keep the connection's descriptors open. Unarmed
    (C3c's single-shot client) a framing blocks as a plain socket or file does."""

    def __init__(self) -> None:
        self._wlock = threading.Lock()
        self._rlock = threading.Lock()
        self._closing = False
        self._broken = False
        self._wake_r: int | None = None
        self._wake_w: int | None = None
        self._wake_lock = threading.Lock()      # wake() and _close_wake() never interleave: no write to a reused fd
        self._shut = False                      # guarded by _wake_lock: shutdown() runs its body once
        self._on_wake: Callable[[], None] = lambda: None
        # a side's close: asked for (pending), then done by a holder of that side's lock (r: _rlock, w: _wlock)
        self._pending = {"r": False, "w": False}
        self._done = {"r": False, "w": False}
        self._armed = False

    def arm(self, on_wake: Callable[[], None]) -> None:
        self._wake_r, self._wake_w = os.pipe()
        self._armed = True
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._on_wake = on_wake

    def wake(self) -> None:
        """Make the reader run ``on_wake`` (on its own thread) as soon as it is waiting for data."""
        with self._wake_lock:
            if self._wake_w is not None:
                with contextlib.suppress(OSError):     # a full pipe is a wake already pending
                    os.write(self._wake_w, b"w")

    def _armed_read(self, fd: Callable[[], int], read: Callable[[], bytes]) -> bytes:
        """Wait for data on ``fd()`` (or its peer's close) and ``read()`` it, under the read lock, running ``on_wake``
        (outside the lock) for each wake on the way. Once levain closes, it raises before touching a descriptor."""
        while True:
            with self._rlock:
                try:
                    if self._closing or self._done["r"]:
                        raise CodexError("the connection was closed by levain")
                    ready = _poll([(fd(), select.POLLIN), (self._wake_r, select.POLLIN)], None)
                    woke = self._wake_r in ready
                    if woke:
                        with contextlib.suppress(OSError):
                            while os.read(self._wake_r, 512):
                                pass
                    elif ready:
                        try:
                            return read()
                        except BlockingIOError:
                            continue
                finally:
                    self._settle_locked("r")
            if woke:
                self._on_wake()                 # may run long (a session callback): it holds no lock

    def _locked_write(self, data: bytes, deadline: float | None, write: Callable[[memoryview], int], fd: int, *,
                      closing_ok: bool = False) -> None:
        """Write all of ``data`` under the write lock by the deadline (monotonic seconds; None: no limit). The deadline
        is checked once the lock is held and after every write: one that passes before the first byte goes out raises
        :class:`_NotStarted` (nothing sent, the stream intact); one that passes part-way raises :class:`CodexError`
        and marks the stream broken (a partial message cannot be resumed)."""
        left = None if deadline is None else deadline - time.monotonic()
        if not self._wlock.acquire(timeout=-1 if left is None else max(0.0, left)):
            raise _NotStarted("a send could not start within its deadline (another send holds the connection)")
        try:
            if self._broken:
                raise CodexError("an earlier send failed part-way; the connection's output is unusable")
            if self._done["w"]:
                raise CodexError("the connection was closed by levain")
            view = memoryview(data)
            sent = 0
            try:
                while view:
                    if self._closing and not closing_ok:
                        raise CodexError("the connection was closed by levain")
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        if not sent:
                            raise _NotStarted("a send's deadline passed before its first byte went out")
                        raise CodexError(f"a send did not complete within its deadline ({sent} of {len(data)} "
                                         "bytes out; the peer is reading too slowly)")
                    try:
                        n = write(view)
                    except BlockingIOError:
                        n = 0
                    if n:
                        view = view[n:]
                        sent += n
                        continue
                    _poll([(fd, select.POLLOUT)], _WRITE_SLICE if left is None else min(left, _WRITE_SLICE))
            except _NotStarted:
                raise
            except BaseException:
                self._broken = True
                raise
        finally:
            try:
                self._settle_locked("w")
            finally:
                self._wlock.release()

    def _side_close(self, side: str, timeout: float) -> None:
        """Close one side's descriptors (``"r"`` or ``"w"``): now, if that side's lock frees within ``timeout``; else
        the thread holding it closes them as it lets go."""
        lock = self._rlock if side == "r" else self._wlock
        self._pending[side] = True
        if lock.acquire(timeout=timeout):
            try:
                self._settle_locked(side)
            finally:
                lock.release()

    def _settle_locked(self, side: str) -> None:
        """With ``side``'s lock held: close its descriptors if a close was asked for and has not run."""
        if self._pending[side] and not self._done[side]:
            self._done[side] = True
            with contextlib.suppress(OSError, ValueError):
                self._close_side(side)

    def _close_side(self, side: str) -> None:
        raise NotImplementedError

    def _first_shutdown(self) -> bool:
        with self._wake_lock:
            first, self._shut = not self._shut, True
        return first

    def _close_wake(self) -> None:
        with self._wake_lock:
            for fd in (self._wake_r, self._wake_w):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            self._wake_r = self._wake_w = None


class WsFraming(_Framing):
    """WebSocket (RFC 6455) client framing over a unix socket after a plain HTTP/1.1 Upgrade (measured on the Codex
    daemon's control socket, codex-cli 0.162.1). :meth:`recv_message` returns one whole message's bytes."""

    kind = "ws"

    def __init__(self, path: Path, timeout: float = _CONNECT_TIMEOUT) -> None:
        super().__init__()
        self._ws: socket.socket | None = None
        self._s = socket.socket(socket.AF_UNIX)
        try:
            self._s.settimeout(timeout)
            self._s.connect(str(path))
            key = base64.b64encode(os.urandom(16))
            self._s.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                            b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n")
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self._s.recv(4096)
                if not chunk or len(buf) > 65536:
                    raise CodexError("the daemon closed the connection during the WebSocket handshake")
                buf += chunk
            head, self._buf = buf.split(b"\r\n\r\n", 1)
            lines = head.split(b"\r\n")
            if b" 101 " not in lines[0] + b" ":
                raise CodexError(f"the daemon refused the WebSocket upgrade: {lines[0][:120].decode(errors='replace')}")
            want = base64.b64encode(hashlib.sha1(key + _GUID).digest())
            accept = next((ln.split(b":", 1)[1].strip() for ln in lines[1:]
                           if ln.lower().startswith(b"sec-websocket-accept:")), None)
            if accept != want:
                raise CodexError("the daemon's WebSocket accept key does not match")
        except BaseException:
            self._s.close()
            raise

    def arm(self, on_wake: Callable[[], None]) -> None:
        """The wire's mode: the socket goes non-blocking; reads go through :meth:`_armed_read` on the socket, writes
        (bounded) through a duplicate of it, so each side's descriptor has one owner (the read lock, the write lock)."""
        self._s.setblocking(False)
        self._ws = self._s.dup()
        self._ws.setblocking(False)
        super().arm(on_wake)

    def close(self) -> None:
        self.shutdown()
        if not self._armed:
            if self._ws is not None:                # an arm that failed part-way
                self._ws.close()
            self._s.close()
            return
        self._side_close("w", _CLOSE_TIMEOUT)
        self._side_close("r", _CLOSE_TIMEOUT)

    def shutdown(self) -> None:
        """Unblock a reader waiting in :meth:`recv_message` (it then sees the connection closed) and any blocked writer;
        bounded: a close frame goes out only if the write lock frees within :data:`_CLOSE_TIMEOUT`, and the write side
        (the socket shut both ways, then closed) is closed by whoever holds that lock. Once only."""
        if not self._first_shutdown():
            return
        if not self._armed:
            with contextlib.suppress(OSError, CodexError):
                self._send(8, b"")
            with contextlib.suppress(OSError):
                self._s.shutdown(socket.SHUT_RDWR)
            return
        self._closing = True
        self.wake()
        with contextlib.suppress(OSError, CodexError):
            self._send(8, b"", time.monotonic() + _CLOSE_TIMEOUT, closing_ok=True)
        self._side_close("w", _CLOSE_TIMEOUT)

    def _close_side(self, side: str) -> None:
        if side == "w":
            with contextlib.suppress(OSError):
                self._ws.shutdown(socket.SHUT_RDWR)     # the shared socket: the reader sees end of input
            self._ws.close()
        else:
            try:
                self._s.close()
            finally:
                self._close_wake()

    def settimeout(self, timeout: float | None) -> None:
        self._s.settimeout(timeout)

    def send_text(self, data: bytes, deadline: float | None = None) -> None:
        self._send(1, data, deadline)

    def _send(self, op: int, payload: bytes, deadline: float | None = None, *, closing_ok: bool = False) -> None:
        n = len(payload)
        head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                     bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                     bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        frame = head + mask + _masked(payload, mask)
        if not self._armed:
            with self._wlock:
                self._s.sendall(frame)
            return
        self._locked_write(frame, deadline, self._ws.send, self._ws.fileno(), closing_ok=closing_ok)

    def _recv_some(self) -> bytes:
        if not self._armed:
            return self._s.recv(65536)
        return self._armed_read(self._s.fileno, lambda: self._s.recv(65536))

    def _need(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._recv_some()
            if not chunk:
                raise CodexError("the daemon closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_message(self) -> bytes:
        """One whole data message (fragments joined; pings answered, pongs ignored). A frame that breaks RFC 6455
        section 5 raises :class:`CodexError` (the connection must close): nonzero RSV bits, an opcode outside
        0/1/2/8/9/10 or a 64-bit length with its top bit set (5.2), a masked frame (5.1), a continuation with no
        message open or a new data message inside a fragmented one (5.4), a control frame fragmented or over 125 bytes
        (5.5). A message over :data:`_MAX_MESSAGE` raises :class:`_Oversize` as soon as a frame header declares it,
        before any of that frame's payload is read."""
        msg = bytearray()
        open_ = False                       # a fragmented message has begun and not ended
        while True:
            h = self._need(2)
            fin, rsv, op, masked, n = h[0] & 0x80, h[0] & 0x70, h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
            if rsv:
                raise CodexError("the daemon sent a frame with nonzero RSV bits and no extension agreed (RFC 6455 "
                                 "section 5.2)")
            if op not in (0, 1, 2, 8, 9, 10):
                raise CodexError(f"the daemon sent a frame with the reserved opcode {op:#x} (RFC 6455 section 5.2)")
            if masked:
                raise CodexError("the daemon sent a masked frame (RFC 6455 section 5.1)")
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
                if n >> 63:
                    raise CodexError("the daemon sent a 64-bit frame length with its top bit set (RFC 6455 "
                                     "section 5.2)")
            if op & 8:
                if n > 125 or not fin:
                    raise CodexError("the daemon sent a control frame that is fragmented or over 125 bytes (RFC 6455 "
                                     "section 5.5)")
            else:
                if op == 0 and not open_:
                    raise CodexError("the daemon sent a continuation frame with no message open (RFC 6455 "
                                     "section 5.4)")
                if op != 0 and open_:
                    raise CodexError("the daemon began a new data message inside a fragmented one (RFC 6455 "
                                     "section 5.4)")
                if len(msg) + n > _MAX_MESSAGE:
                    raise _Oversize(len(msg) + n)
            payload = self._need(n)
            if op == 9:
                try:
                    self._send(10, payload, time.monotonic() + _READER_SEND_TIMEOUT)
                except _NotStarted:
                    pass                    # a writer holds the lock: nothing went out, a later ping gets its own try
            elif op == 8:
                raise CodexError("the daemon closed the connection")
            elif op != 10:
                msg += payload
                open_ = not fin
                if fin:
                    return bytes(msg)


class LineFraming(_Framing):
    """One JSON object per line over a pipe pair (``codex app-server`` on stdio): ``rfile`` is the child's stdout,
    ``wfile`` its stdin, both binary with a file descriptor. Lines are read from ``rfile``'s descriptor (its own buffer
    is never used) and, once armed, written to ``wfile``'s. Closing ends only Levain's ends."""

    kind = "line"

    def __init__(self, rfile: BinaryIO, wfile: BinaryIO) -> None:
        super().__init__()
        self._r = rfile
        self._w = wfile
        self._rbuf = bytearray()
        self._scan = 0

    def arm(self, on_wake: Callable[[], None]) -> None:
        """The wire's mode: the write end goes non-blocking (writes are bounded); reads go through
        :meth:`_armed_read`."""
        self._w.flush()
        os.set_blocking(self._w.fileno(), False)
        super().arm(on_wake)

    def send_text(self, data: bytes, deadline: float | None = None) -> None:
        if not self._armed:
            with self._wlock:
                self._w.write(data + b"\n")
                self._w.flush()
            return
        fd = self._w.fileno()       # read before the lock: _locked_write refuses a closed side under the lock
        self._locked_write(data + b"\n", deadline, lambda view: os.write(fd, view), fd)

    def _read_some(self) -> bytes:
        if not self._armed:
            return os.read(self._r.fileno(), 65536)
        return self._armed_read(self._r.fileno, lambda: os.read(self._r.fileno(), 65536))

    def recv_message(self) -> bytes:
        """One line without its line ending. A line over :data:`_MAX_MESSAGE` bytes raises :class:`_Oversize` as soon
        as that many bytes have arrived without a line end; the rest of it is never read."""
        while True:
            i = self._rbuf.find(b"\n", self._scan)
            if i >= 0:
                if i > _MAX_MESSAGE:
                    raise _Oversize(i)
                line = bytes(self._rbuf[:i])
                del self._rbuf[:i + 1]
                self._scan = 0
                return line.rstrip(b"\r")
            if len(self._rbuf) > _MAX_MESSAGE:
                raise _Oversize(len(self._rbuf))
            self._scan = len(self._rbuf)
            chunk = self._read_some()
            if not chunk:
                if self._rbuf:
                    line = bytes(self._rbuf)
                    self._rbuf.clear()
                    self._scan = 0
                    return line.rstrip(b"\r")
                raise CodexError("the app-server closed its output")
            self._rbuf += chunk

    def shutdown(self) -> None:
        """End Levain's write end (the peer sees end of input) and the reader; bounded like :meth:`WsFraming.shutdown`:
        the write end is closed by whoever holds the write lock, never under a writer."""
        if not self._first_shutdown():
            return
        if not self._armed:
            with contextlib.suppress(OSError, ValueError):
                self._w.close()
            return
        self._closing = True
        self.wake()
        self._side_close("w", _CLOSE_TIMEOUT)

    def close(self) -> None:
        self.shutdown()
        if not self._armed:
            with contextlib.suppress(OSError, ValueError):
                self._r.close()
            return
        self._side_close("r", _CLOSE_TIMEOUT)

    def _close_side(self, side: str) -> None:
        if side == "w":
            self._w.close()
        else:
            try:
                self._r.close()
            finally:
                self._close_wake()


# --- the pinned surface and the server-request registry ------------------------------------------------------------
PINNED_VERSION = "0.162.1"
# Each method this module or the CH-D3 session relies on -> its params' REQUIRED fields, as
# `codex app-server generate-json-schema` writes them for the pinned version (test_coordinator_codex_wire regenerates
# the schema with the installed codex and compares).
PINNED_SURFACE: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "0.162.1": {
        "ClientRequest": {
            "initialize": ("clientInfo",),
            "thread/start": (),
            "thread/resume": ("threadId",),
            "thread/unsubscribe": ("threadId",),
            "thread/read": ("threadId",),
            "thread/turns/list": ("threadId",),
            "thread/items/list": ("threadId",),
            "hooks/list": (),
            "turn/start": ("input", "threadId"),
            "turn/steer": ("expectedTurnId", "input", "threadId"),
            "turn/interrupt": ("threadId", "turnId"),
        },
        "ServerRequest": {
            "item/commandExecution/requestApproval": ("itemId", "startedAtMs", "threadId", "turnId"),
            "item/fileChange/requestApproval": ("itemId", "startedAtMs", "threadId", "turnId"),
            "item/tool/requestUserInput": ("isBlocking", "itemId", "questions", "threadId", "turnId"),
            "mcpServer/elicitation/request": ("serverName", "threadId"),
            "item/permissions/requestApproval": ("cwd", "itemId", "permissions", "startedAtMs", "threadId",
                                                 "turnId"),
            "item/tool/call": ("arguments", "callId", "threadId", "tool", "turnId"),
            "account/chatgptAuthTokens/refresh": ("reason",),
            "attestation/generate": (),
            "applyPatchApproval": ("callId", "conversationId", "fileChanges"),
            "execCommandApproval": ("callId", "command", "conversationId", "cwd", "parsedCmd"),
        },
        "ServerNotification": {
            "thread/started": ("thread",),
            "thread/status/changed": ("status", "threadId"),
            "thread/closed": ("threadId",),
            "thread/settings/updated": ("threadId", "threadSettings"),
            "turn/started": ("threadId", "turn"),
            "turn/completed": ("threadId", "turn"),
            "item/started": ("item", "startedAtMs", "threadId", "turnId"),
            "item/completed": ("completedAtMs", "item", "threadId", "turnId"),
            "item/fileChange/patchUpdated": ("changes", "itemId", "threadId", "turnId"),
            "item/commandExecution/terminalInteraction": ("itemId", "processId", "stdin", "threadId", "turnId"),
            "serverRequest/resolved": ("requestId", "threadId"),
            "hook/started": ("run", "threadId"),
            "hook/completed": ("run", "threadId"),
            "item/autoApprovalReview/started": ("action", "review", "reviewId", "startedAtMs", "threadId",
                                                "turnId"),
            "item/autoApprovalReview/completed": ("action", "completedAtMs", "decisionSource", "review", "reviewId",
                                                  "startedAtMs", "threadId", "turnId"),
            "error": ("error", "threadId", "turnId", "willRetry"),
            "item/agentMessage/delta": ("delta", "itemId", "threadId", "turnId"),
            "item/reasoning/textDelta": ("contentIndex", "delta", "itemId", "threadId", "turnId"),
            "item/reasoning/summaryTextDelta": ("delta", "itemId", "summaryIndex", "threadId", "turnId"),
            "item/reasoning/summaryPartAdded": ("itemId", "summaryIndex", "threadId", "turnId"),
            "item/commandExecution/outputDelta": ("delta", "itemId", "threadId", "turnId"),
            "item/fileChange/outputDelta": ("delta", "itemId", "threadId", "turnId"),
            "command/exec/outputDelta": ("capReached", "deltaBase64", "processId", "stream"),
            "thread/tokenUsage/updated": ("threadId", "tokenUsage", "turnId"),
            "account/rateLimits/updated": ("rateLimits",),
            "mcpServer/startupStatus/updated": ("name", "status"),
        },
    },
}

# The notifications the CH-D3 session relies on (rev 3 §4 / §8.2), per pinned version: each is mapped to an event or read
# as state. `thread/settings/updated` is in the table above but NOT here: it is experimental in the Codex source and
# never reaches a client that sent `experimentalApi: false`; one that arrives is an anomaly, not evidence. Adding a
# version means re-reading each method's `#[experimental]` mark in that tag's source, which the schema test cannot see.
RELIED_NOTIFICATIONS: Mapping[str, frozenset[str]] = {
    "0.162.1": frozenset((
        "thread/started", "thread/status/changed", "thread/closed", "turn/started", "turn/completed", "item/started",
        "item/completed", "item/fileChange/patchUpdated", "item/commandExecution/terminalInteraction",
        "serverRequest/resolved", "hook/started", "hook/completed", "item/autoApprovalReview/started",
        "item/autoApprovalReview/completed", "error")),
}

# exactly the ten methods of CH-D3 §3.2: the streaming deltas and the token / rate-limit / startup-status updates no
# event is made from (a test holds the list disjoint from RELIED_NOTIFICATIONS)
OPT_OUT_NOTIFICATIONS: tuple[str, ...] = (
    "item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    "command/exec/outputDelta", "thread/tokenUsage/updated", "account/rateLimits/updated",
    "mcpServer/startupStatus/updated",
)

# the params field naming a server method's thread: the legacy approvals carry it as conversationId
_THREAD_FIELD: Mapping[str, str] = {"applyPatchApproval": "conversationId", "execCommandApproval": "conversationId"}

# server methods whose pinned params REQUIRE their thread field: one that arrives without it is unparseable
_THREAD_REQUIRED: frozenset[str] = frozenset(
    m for table in ("ServerRequest", "ServerNotification")
    for m, req in PINNED_SURFACE[PINNED_VERSION][table].items() if _THREAD_FIELD.get(m, "threadId") in req)


@dataclass(frozen=True)
class ServerRequestRow:
    """One server-request method the session knows (rev 2 §6.2 / §6.6). ``reads``: version -> the closed list of params
    fields the session reads (an approval with a field outside it fails closed, §6.2). The deny is ``reply`` (a
    schema-valid result) or, where the schema has no deny form, ``error`` (a JSON-RPC ``(code, message)``).
    ``response`` names the response definition ``reply`` must satisfy. ``replayable`` is unknown (None) until R-E."""

    method: str
    reads: Mapping[str, frozenset[str]]
    reply: Mapping[str, Any] | None
    error: tuple[int, str] | None
    requires_card: bool
    latches: bool
    response: str | None = None
    replayable: bool | None = None

    def deny(self) -> dict[str, Any]:
        """The deny as a frame body without its id: ``{"result": ...}`` or ``{"error": {...}}``."""
        if self.reply is not None:
            return {"result": json.loads(json.dumps(self.reply))}
        code, message = self.error or (-32601, "levain answers no such request")
        return {"error": {"code": code, "message": message}}


def _row(method: str, reads: tuple[str, ...], *, reply: Mapping[str, Any] | None = None,
         error: tuple[int, str] | None = None, card: bool = False, latches: bool = False,
         response: str | None = None) -> ServerRequestRow:
    return ServerRequestRow(method, {PINNED_VERSION: frozenset(reads)}, reply, error, card, latches, response)


_NO_METHOD = (-32601, "levain answers no such request in chat")
SERVER_REQUESTS: Mapping[str, ServerRequestRow] = {r.method: r for r in (
    _row("item/commandExecution/requestApproval",
         ("kind", "command", "cwd", "networkApprovalContext", "reason", "commandActions",
          "proposedExecpolicyAmendment", "proposedNetworkPolicyAmendments", "availableDecisions",
          "additionalPermissions", "approvalId", "environmentId", "itemId", "startedAtMs", "threadId", "turnId"),
         reply={"decision": "decline"}, card=True, response="CommandExecutionRequestApprovalResponse"),
    _row("item/fileChange/requestApproval", ("grantRoot", "itemId", "reason", "startedAtMs", "threadId", "turnId"),
         reply={"decision": "decline"}, card=True, response="FileChangeRequestApprovalResponse"),
    _row("item/permissions/requestApproval", ("cwd", "itemId", "permissions", "startedAtMs", "threadId", "turnId"),
         reply={"permissions": {}}, response="PermissionsRequestApprovalResponse"),
    _row("mcpServer/elicitation/request", ("serverName", "threadId"), reply={"action": "decline"},
         response="McpServerElicitationRequestResponse"),
    _row("item/tool/requestUserInput", ("isBlocking", "itemId", "questions", "threadId", "turnId"),
         error=(-32603, "levain answers no user-input request in chat")),
    _row("item/tool/call", ("arguments", "callId", "threadId", "tool", "turnId"),
         reply={"contentItems": [], "success": False}, latches=True, response="DynamicToolCallResponse"),
    _row("account/chatgptAuthTokens/refresh", ("reason",), error=_NO_METHOD, latches=True),
    _row("attestation/generate", (), error=_NO_METHOD, latches=True),
    _row("applyPatchApproval", ("callId", "conversationId", "fileChanges"), error=_NO_METHOD, latches=True),
    _row("execCommandApproval", ("callId", "command", "conversationId", "cwd", "parsedCmd"), error=_NO_METHOD,
         latches=True),
)}
CATCH_ALL = ServerRequestRow("*", {}, None, _NO_METHOD, False, True)


def server_request_row(method: str) -> ServerRequestRow:
    """The registry row for ``method``; an unknown method takes :data:`CATCH_ALL`."""
    return SERVER_REQUESTS.get(method, CATCH_ALL)


# --- the wire ----------------------------------------------------------------------------------------------------
class _NoId:
    def __repr__(self) -> str:
        return "NO_ID"


NO_ID: Any = _NoId()


@dataclass(frozen=True)
class Inbound:
    """One queued inbound message. ``kind``: ``notification``, ``request`` (answerable once, on its own ``gen``),
    ``foreign`` (names another thread: never answered), ``unparseable`` (``detail`` says why) or ``closed`` (the
    connection ended; the last item of a ``gen``). ``id`` is the JSON-RPC id exactly as sent, :data:`NO_ID` when
    absent. ``obs`` is the item's arrival ordinal on its ``gen``."""

    kind: str
    gen: str
    obs: int
    method: str | None = None
    id: Any = NO_ID
    params: Any = None
    thread_id: str | None = None
    raw_sha256: str = ""
    detail: str = ""
    answered: bool = False          # an ``unparseable`` request the wire itself answered with -32600

    @property
    def token(self) -> str:
        return f"{self.gen}:{self.obs}"


@dataclass
class _Waiter:
    method: str
    refuse: bool
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: CodexError | None = None


@dataclass
class _Boot:
    """The bootstrap state: every item that arrives while ``thread/start`` is outstanding, held with its arrival
    ordinal as ``(obs, kind, fields, refused)`` (``refused``: a thread-less request that already failed the
    ``refuse`` calls)."""

    key: Any
    frames: list[tuple[int, str, dict[str, Any], bool]] = field(default_factory=list)
    dropped: int = 0


class _ReaderFault(Exception):
    """The reader ends the connection on purpose (fail closed); the message is the reason."""


class CodexWire:
    """One connection to a Codex app-server (see the module docstring). Build it with :meth:`open` (or
    :meth:`connect_socket` / :meth:`over_pipes`); read with :meth:`get` / :meth:`drain`; send with :meth:`call`,
    :meth:`start_thread`, :meth:`notify` and :meth:`reply`; end with :meth:`close`. ``on_inbound`` is called only on
    the reader thread, after each item is queued, in arrival order, and after the call an item's frame unblocked was
    released (a wake-up for the session); it must not :meth:`call` (that raises)."""

    def __init__(self, framing: Any, *, on_inbound: Callable[[Inbound], None] | None = None,
                 bootstrap_cap: int = BOOTSTRAP_FRAME_CAP) -> None:
        self._framing = framing
        self.gen = secrets.token_hex(8)
        self.init: dict[str, Any] = {}
        self.log_lines = 0
        self.callback_errors = 0
        self._log_sha = hashlib.sha256()
        self._on_inbound = on_inbound
        self._cap = bootstrap_cap
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: deque[Inbound] = deque()
        self._obs = 0
        self._next_id = 0
        self._waiters: dict[Any, _Waiter] = {}
        self._late: deque[Any] = deque(maxlen=_LATE_IDS_CAP)
        self._pending: dict[str, Inbound] = {}
        self._deferred: list[Inbound] = []
        self._thread_id: str | None = None
        self._boot: _Boot | None = None
        self._initialized = False
        self._closed: str | None = None
        self._closed_queued = False
        self._reader = threading.Thread(target=self._run, name=f"codex-wire-{self.gen}", daemon=True)

    # -- construction --
    @classmethod
    def open(cls, framing: Any, *, client_version: str | None = None,
             opt_out: tuple[str, ...] = OPT_OUT_NOTIFICATIONS, timeout: float = _CALL_TIMEOUT,
             on_inbound: Callable[[Inbound], None] | None = None,
             bootstrap_cap: int = BOOTSTRAP_FRAME_CAP) -> CodexWire:
        """Start the reader on ``framing``, then ``initialize`` and ``initialized``. Any fault closes the framing and
        raises :class:`CodexError`."""
        if client_version is None:
            from levain import __version__ as client_version
        wire = cls(framing, on_inbound=on_inbound, bootstrap_cap=bootstrap_cap)
        try:
            framing.arm(wire._after_deferred)   # bounded writes; the reader waits on data and on wire wakes
            wire._reader.start()
            deadline = time.monotonic() + timeout
            init = wire._call("initialize", {
                "clientInfo": {"name": "levain-chat", "title": "Levain chat", "version": client_version},
                "capabilities": {"experimentalApi": False, "optOutNotificationMethods": list(opt_out)},
            }, timeout, refuse=False, boot=False)
            wire._send({"method": "initialized", "params": {}}, deadline)
            wire._initialized = True
        except BaseException:
            wire.close()
            raise
        wire.init = init if isinstance(init, dict) else {}
        return wire

    @classmethod
    def connect_socket(cls, path: Path, **kw: Any) -> CodexWire:
        try:
            framing = WsFraming(path)
        except OSError as exc:
            raise CodexError(f"the Codex daemon is not answering on {path} ({type(exc).__name__}: {exc})") from None
        return cls.open(framing, **kw)

    @classmethod
    def over_pipes(cls, rfile: BinaryIO, wfile: BinaryIO, **kw: Any) -> CodexWire:
        return cls.open(LineFraming(rfile, wfile), **kw)

    # -- state --
    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def closed(self) -> str | None:
        """Why the connection ended, or None while it is open."""
        return self._closed

    @property
    def log_digest(self) -> str:
        return self._log_sha.hexdigest()

    def bind_thread(self, thread_id: str) -> None:
        """Make ``thread_id`` this wire's thread (an attach, before ``thread/resume``). A wire has one thread."""
        with self._lock:
            if self._thread_id is not None and self._thread_id != thread_id:
                raise CodexError(f"this connection is already bound to thread {self._thread_id}")
            if self._boot is not None:
                raise CodexError("a thread/start is outstanding on this connection")
            self._thread_id = thread_id

    # -- reading --
    def get(self, timeout: float | None = None) -> Inbound | None:
        """The next queued item, waiting up to ``timeout`` seconds (None: until one arrives); None on timeout, and at
        once when the queue is empty after the ``closed`` item was queued (nothing more can arrive)."""
        with self._cond:
            if not self._queue:
                self._cond.wait_for(lambda: bool(self._queue) or self._closed_queued, timeout)
            return self._queue.popleft() if self._queue else None

    def drain(self) -> list[Inbound]:
        """Every queued item, in arrival order."""
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
            return out

    # -- sending --
    def call(self, method: str, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT,
             on_server_request: str = "queue") -> Any:
        """Send one request and return its ``result``; an error response raises :class:`CodexError` (``.error`` holds
        it). The deadline covers the send too: a send not begun in it fails the call alone (nothing went out); one
        cut off part-way fails the call and closes the connection. Server traffic in between is queued as always; with ``on_server_request="refuse"`` a server
        request arriving while this call is outstanding (the wire's thread, or no thread) also makes the call raise
        :class:`CodexError` naming that method (the request stays queued, unanswered; the late response is
        dropped). Called from the reader thread, it raises at once."""
        if on_server_request not in ("queue", "refuse"):
            raise ValueError(f"on_server_request must be 'queue' or 'refuse', not {on_server_request!r}")
        if method == "thread/start":
            raise CodexError("thread/start goes through start_thread(), which binds the thread it starts")
        return self._call(method, params, timeout, refuse=on_server_request == "refuse", boot=False)

    def start_thread(self, params: dict[str, Any], *, timeout: float = _CALL_TIMEOUT) -> dict[str, Any]:
        """``thread/start`` with the bootstrap state (see the module docstring); returns its result, the wire then
        bound to the returned ``thread.id``. The wire serves one thread: while one ``thread/start`` is outstanding, or
        once a thread is bound, this raises :class:`CodexError` at once. A result without a thread id raises too."""
        got = self._call("thread/start", params, timeout, refuse=False, boot=True)
        if self._thread_id is None:
            raise CodexError("thread/start answered without a thread id")
        return got

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._check_ready(method)
        self._send({"method": method, "params": params}, time.monotonic() + _CALL_TIMEOUT)

    def reply(self, item: Inbound, *, result: Any = NO_ID, error: tuple[int, str] | None = None) -> None:
        """Answer server request ``item`` once, with ``result`` or ``error`` ``(code, message)``, echoing its id as it
        came. Refused (:class:`CodexError`): an item that is not a request (a foreign thread's included), one from
        another ``gen``, one already answered, or a closed connection. A send that fails part-way closes the
        connection; one not begun by its deadline leaves the request unanswered, to be answered again."""
        if (result is NO_ID) == (error is None):
            raise ValueError("reply takes exactly one of result= or error=")
        with self._lock:
            if item.kind == "foreign":
                raise CodexError(f"{item.method}: a request on thread {item.thread_id} is never answered here")
            if item.kind != "request":
                raise CodexError(f"item {item.token} is a {item.kind}, not a request")
            if item.gen != self.gen:
                raise CodexError(f"{item.method}: the request is from a dead connection ({item.gen})")
            if self._closed is not None:
                raise CodexError(f"{item.method}: the connection is closed ({self._closed})")
            if item.token not in self._pending:
                raise CodexError(f"{item.method}: request {item.token} was already answered")
        body: dict[str, Any] = {"id": item.id}
        if error is None:
            body["result"] = result
        else:
            body["error"] = {"code": error[0], "message": error[1]}
        data = json.dumps(body).encode()                    # a body that cannot be serialised leaves the item pending
        with self._lock:
            if self._pending.pop(item.token, None) is None:
                raise CodexError(f"{item.method}: request {item.token} was already answered")
        try:
            self._send_data(data, time.monotonic() + _CALL_TIMEOUT)
        except CodexError:
            with self._lock:
                if self._closed is None:            # not one byte went out: the request is still unanswered
                    self._pending[item.token] = item
            raise

    def close(self) -> None:
        """Close the connection; calls still waiting raise :class:`CodexError`. Bounded: the framing's shutdown waits
        at most :data:`_CLOSE_TIMEOUT` for a writer, and the reader is given 2s to end. The framing is closed even when
        the reader has not ended (an ``on_inbound`` that never returns): a stuck callback holds no descriptor, and a
        writer still holding the write lock closes the write side as it lets go."""
        self._fail("closed by levain")
        if self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(2.0)
        with contextlib.suppress(OSError, ValueError, CodexError):
            self._framing.close()

    # -- internals: sending --
    def _check_ready(self, method: str) -> None:
        if method != "initialize" and not self._initialized:
            raise CodexError(f"{method}: nothing but initialize goes out before initialized")

    def _fail(self, why: str) -> None:
        """End the connection from a sending thread: record ``why`` (unless already closed), fail every waiting call
        and shut the framing (the reader then ends and runs the close epilogue on its own thread)."""
        with self._lock:
            if self._closed is None:
                self._closed = why
            self._fail_waiters_locked("the connection was closed by levain" if self._closed == "closed by levain"
                                      else f"the connection ended ({self._closed})")
        with contextlib.suppress(OSError, ValueError, CodexError):
            self._framing.shutdown()

    def _send(self, body: dict[str, Any], deadline: float, *, fail_unsent: bool = False) -> None:
        self._send_data(json.dumps(body).encode(), deadline, fail_unsent=fail_unsent)

    def _send_data(self, data: bytes, deadline: float, *, fail_unsent: bool = False) -> None:
        """Send one message. A send that ended before its first byte (:class:`_NotStarted`) fails only this send (the
        stream is intact) unless ``fail_unsent``; any other failure closes the connection."""
        with self._lock:
            if self._closed is not None:
                raise CodexError(f"the connection is closed ({self._closed})")
        try:
            self._framing.send_text(data, deadline)
        except _NotStarted as exc:
            if not fail_unsent:
                raise CodexError(f"not sent: {exc}") from None
            why = f"a send failed ({type(exc).__name__}: {exc})"
            self._fail(why)
            raise CodexError(f"the Codex connection failed: {why}") from None
        except (CodexError, OSError, ValueError) as exc:
            why = f"a send failed ({type(exc).__name__}: {exc})"
            self._fail(why)
            raise CodexError(f"the Codex connection failed: {why}") from None

    def _call(self, method: str, params: dict[str, Any], timeout: float, *, refuse: bool, boot: bool) -> Any:
        if self._on_reader_thread():
            raise CodexError(f"{method}: called from the wire's reader thread, the one thread that could read its "
                             "answer")
        self._check_ready(method)
        deadline = time.monotonic() + timeout
        params_json = json.dumps(params)                    # before anything is reserved: a TypeError leaves no state
        with self._lock:
            if self._closed is not None:
                raise CodexError(f"{method}: the connection is closed ({self._closed})")
            if boot and self._thread_id is not None:
                raise CodexError(f"this connection is already bound to thread {self._thread_id}")
            if boot and self._boot is not None:
                raise CodexError("a thread/start is outstanding on this connection: the wire serves one thread")
            rid = self._next_id
            self._next_id += 1
            key = self._id_key(rid)
            waiter = _Waiter(method, refuse)
            self._waiters[key] = waiter
            if boot:
                self._boot = _Boot(key)
        try:
            self._send_data(f'{{"id": {rid}, "method": {json.dumps(method)}, "params": {params_json}}}'.encode(),
                            deadline)
        except CodexError:
            self._abandon(key, boot)
            raise
        if not waiter.event.wait(max(0.0, deadline - time.monotonic())):
            if self._abandon(key, boot):
                raise CodexError(f"the daemon did not answer {method} in {timeout:g}s")
            waiter.event.wait()             # answered as the deadline passed: the reader set it under the lock
        if waiter.error is not None:
            raise waiter.error
        m = waiter.response or {}
        if "error" in m:
            err = m["error"]
            text = err.get("message") if isinstance(err, dict) else err
            raise CodexError(f"{method}: {str(text)[:300]}", err)
        return m.get("result")

    def _abandon(self, key: Any, boot: bool) -> bool:
        """Drop a call nobody answered; True when it was still waiting (False: its answer came in meanwhile). A
        ``thread/start`` leaves the bootstrap state here; the callbacks for what it held run on the reader thread."""
        wake = False
        with self._lock:
            if self._waiters.pop(key, None) is None:
                return False
            self._late.append(key)
            if boot and self._boot is not None and self._boot.key == key:
                self._deferred += self._end_boot_locked(None)
                wake = bool(self._deferred)
        if wake:
            self._framing.wake()
        return True

    # -- internals: reading (the reader thread) --
    def _run(self) -> None:
        reason = "the connection ended"
        try:
            while True:
                try:
                    raw = self._framing.recv_message()
                except _Oversize as exc:
                    raise _ReaderFault(f"a message of at least {exc.size} bytes, over {_MAX_MESSAGE}") from None
                self._dispatch(raw)
        except (CodexError, _ReaderFault, OSError, ValueError) as exc:
            reason = str(exc) or type(exc).__name__
        except BaseException as exc:  # noqa: BLE001 - any other end of the reader is surfaced, never silent
            reason = f"the wire's reader failed ({type(exc).__name__}: {exc})"
        finally:
            self._epilogue(reason)

    def _epilogue(self, reason: str) -> None:
        """The reader's last act: the wire goes dead, every waiter fails, what bootstrap held is released and the
        ``closed`` item is queued, then the framing is shut and closed."""
        fresh: list[Inbound] = []
        with self._lock:
            if self._closed is None:
                self._closed = reason
            if self._boot is not None:
                fresh += self._end_boot_locked(None)
            self._fail_waiters_locked(f"the connection ended ({self._closed})")
            fresh.append(self._enqueue_new_locked("closed", detail=self._closed))
            self._closed_queued = True
            self._cond.notify_all()
        with contextlib.suppress(OSError, ValueError, CodexError):
            self._framing.shutdown()
        self._after(fresh)
        with contextlib.suppress(OSError, ValueError, CodexError):
            self._framing.close()

    def _dispatch(self, raw: bytes) -> None:
        sha = hashlib.sha256(raw).hexdigest()
        try:
            msg = json.loads(raw.decode("utf-8"))
        except RecursionError:
            self._push(self._item("unparseable", raw_sha256=sha, detail="a message nested too deeply to parse"))
            return
        except (UnicodeDecodeError, ValueError):
            if self._framing.kind == "line" and not raw.lstrip()[:1] == b"{":
                with self._lock:
                    self.log_lines += 1
                    self._log_sha.update(hashlib.sha256(raw).digest())
                return
            self._push(self._item("unparseable", raw_sha256=sha, detail="a message that is not JSON"))
            return
        if not isinstance(msg, dict):
            self._push(self._item("unparseable", raw_sha256=sha, detail="a message that is not a JSON object"))
            return
        has_id = self._has_id(msg)
        rid = msg.get("id", NO_ID)
        if "method" not in msg:
            if has_id and self._usable(rid):
                self._deliver(msg, sha)
            else:
                self._push(self._item("unparseable", raw_sha256=sha,
                                      detail="a message with neither a method nor a usable id"))
            return
        method = msg["method"]
        if not isinstance(method, str):
            self._push(self._item("unparseable", raw_sha256=sha, detail="a method that is not a string"))
            return
        if has_id and not self._usable(rid):
            self._push(self._item("unparseable", method=method, raw_sha256=sha,
                                  detail="a server request with no usable id"))
            return
        kind = "request" if has_id else "notification"
        params = msg.get("params")
        tid, why = self._thread_of(method, params)
        if why:
            answered = False
            if has_id:
                try:
                    self._send({"id": rid, "error": {"code": -32600, "message": f"invalid request: {why}"}},
                               time.monotonic() + _READER_SEND_TIMEOUT, fail_unsent=True)
                    answered = True
                except CodexError:
                    pass                    # the send failed and closed the wire; the item still says unanswered
            self._push(self._item("unparseable", method=method, id=self._wire_id(rid) if has_id else NO_ID,
                                  raw_sha256=sha, detail=why, answered=answered))
            return
        rid = self._wire_id(rid) if has_id else NO_ID
        fields = {"method": method, "id": rid, "params": params, "thread_id": tid, "raw_sha256": sha}
        fresh: list[Inbound] = []
        with self._lock:
            if self._boot is not None:
                self._hold_locked(kind, fields)
            elif tid is None or tid == self._thread_id:
                fresh.append(self._enqueue_new_locked(kind, **fields))
            else:
                fresh.append(self._enqueue_new_locked("foreign", **fields))
        self._after(fresh)

    def _hold_locked(self, kind: str, fields: dict[str, Any]) -> None:
        """Hold an item that arrived during bootstrap, with its arrival ordinal. Past the cap a notification is counted
        and dropped; a server request ends the connection (its id is never discarded)."""
        boot = self._boot
        if len(boot.frames) >= self._cap:
            if kind == "request":
                raise _ReaderFault(f"the server request {fields.get('method')} arrived past the bootstrap cap of "
                                   f"{self._cap} while thread/start was outstanding; the connection is closed rather "
                                   "than drop its id")
            boot.dropped += 1
            return
        self._obs += 1
        refused = kind == "request" and fields.get("thread_id") is None
        if refused:
            self._refuse_waiters_locked(fields.get("method"))
        boot.frames.append((self._obs, kind, fields, refused))

    def _deliver(self, msg: dict[str, Any], sha: str) -> None:
        key = self._id_key(msg["id"])
        fault = ("result" in msg) == ("error" in msg)
        fresh: list[Inbound] = []
        with self._lock:
            waiter = self._waiters.pop(key, None)
            if waiter is None:
                if key not in self._late:
                    fresh.append(self._emit_locked("unparseable", raw_sha256=sha,
                                                   detail="a response no call is waiting on"))
            else:
                if fault:
                    waiter.error = CodexError(f"{waiter.method}: the daemon's response holds "
                                              f"{'both' if 'result' in msg else 'neither'} result and error")
                else:
                    waiter.response = msg
                if self._boot is not None and self._boot.key == key:
                    result = msg.get("result")
                    thread = result.get("thread") if isinstance(result, dict) else None
                    tid = thread.get("id") if isinstance(thread, dict) else None
                    fresh = self._end_boot_locked(tid if isinstance(tid, str) and not fault and "error" not in msg
                                                  else None)
                if fault:
                    fresh.append(self._emit_locked(
                        "unparseable", raw_sha256=sha,
                        detail=f"a response to {waiter.method} without exactly one of result and error"))
                waiter.event.set()          # the call is released before any callback below runs
        self._after(fresh)

    def _end_boot_locked(self, thread_id: str | None) -> list[Inbound]:
        """Leave the bootstrap state: bind ``thread_id`` (None: the start failed) and release what it held, in arrival
        order with the arrival ordinals, the bound thread's and thread-less items as themselves and every other as
        ``foreign``."""
        boot, self._boot = self._boot, None
        if thread_id is not None:
            self._thread_id = thread_id
        out: list[Inbound] = []
        for obs, kind, fields, refused in boot.frames if boot else ():
            tid = fields.get("thread_id")
            if tid is not None and tid != self._thread_id:
                kind = "foreign"
            out.append(self._enqueue_new_locked(kind, obs=obs, refuse=not refused, **fields))
        if boot and boot.dropped:
            out.append(self._enqueue_new_locked(
                "unparseable", detail=f"{boot.dropped} message(s) past the bootstrap cap of {self._cap} were dropped "
                                      "before thread/start answered"))
        return out

    def _thread_of(self, method: str, params: Any) -> tuple[str | None, str]:
        """``(thread id or None, why unparseable or "")``."""
        if method == "thread/started":
            thread = params.get("thread") if isinstance(params, dict) else None
            tid = thread.get("id") if isinstance(thread, dict) else None
            return (tid, "") if isinstance(tid, str) else (None, "thread/started without a thread id")
        name = _THREAD_FIELD.get(method, "threadId")
        tid = params.get(name, NO_ID) if isinstance(params, dict) else NO_ID
        if tid is None and method not in _THREAD_REQUIRED:
            tid = NO_ID                     # an explicit null where the schema does not require the field: absent
        if tid is NO_ID:
            return (None, f"{method} without the {name} its schema requires") if method in _THREAD_REQUIRED \
                else (None, "")
        return (tid, "") if isinstance(tid, str) else (None, f"{method} with a {name} that is not a string")

    def _item(self, kind: str, **kw: Any) -> dict[str, Any]:
        return {"kind": kind, **kw}

    def _push(self, spec: dict[str, Any]) -> None:
        with self._lock:
            got = self._emit_locked(spec.pop("kind"), **spec)
        self._after([got] if got is not None else [])

    def _emit_locked(self, kind: str, **kw: Any) -> Inbound | None:
        """Queue a reader-made item, or hold it while bootstrap is (None then): the queue keeps arrival order."""
        if self._boot is not None:
            self._hold_locked(kind, kw)
            return None
        return self._enqueue_new_locked(kind, **kw)

    def _enqueue_new_locked(self, kind: str, *, obs: int | None = None, refuse: bool = True, **kw: Any) -> Inbound:
        if obs is None:
            self._obs += 1
            obs = self._obs
        item = Inbound(kind, self.gen, obs, **kw)
        if kind == "request":
            self._pending[item.token] = item
            if refuse:
                self._refuse_waiters_locked(item.method)
        self._enqueue_locked(item)
        self._cond.notify_all()
        return item

    def _refuse_waiters_locked(self, method: str | None) -> None:
        for key, w in list(self._waiters.items()):
            if w.refuse:
                del self._waiters[key]
                self._late.append(key)
                w.error = CodexError(f"{w.method}: the daemon sent the server request {method} while it was "
                                     "outstanding")
                w.event.set()

    def _after_deferred(self) -> None:
        """The framing's ``on_wake``, on the reader thread: run the callbacks another thread deferred."""
        self._after([])

    def _after(self, items: list[Inbound]) -> None:
        """Run ``on_inbound`` for ``items`` and for any deferred ones, in arrival order. Reader thread only."""
        with self._lock:
            deferred, self._deferred = self._deferred, []
        if self._on_inbound is None:
            return
        for item in sorted(deferred + [i for i in items if i is not None], key=lambda i: i.obs):
            try:
                self._on_inbound(item)
            except Exception:  # noqa: BLE001 - a session callback fault never stops the reader; it is counted
                with self._lock:
                    self.callback_errors += 1

    def _fail_waiters_locked(self, why: str) -> None:
        for key, w in list(self._waiters.items()):
            w.error = CodexError(f"{w.method}: {why}")
            w.event.set()
            self._late.append(key)
        self._waiters.clear()

    # -- the behaviours the S1 tests break on purpose (overridden only there) --
    def _on_reader_thread(self) -> bool:
        return threading.current_thread() is self._reader

    def _enqueue_locked(self, item: Inbound) -> None:
        self._queue.append(item)

    @staticmethod
    def _has_id(msg: dict[str, Any]) -> bool:
        return "id" in msg

    @staticmethod
    def _usable(rid: Any) -> bool:
        return type(rid) is int or type(rid) is str

    @staticmethod
    def _id_key(rid: Any) -> tuple[str, Any]:
        return (type(rid).__name__, rid)

    @staticmethod
    def _wire_id(rid: Any) -> Any:
        return rid
