"""A small stdlib-only RFC 6455 WebSocket client, for one outbound provider stream (cockpit mic slice S2).

It carries a provider key in a handshake header, so what it refuses matters more than what it sends:

* **wss only.** ``ws://`` is refused unless the caller passes ``insecure_for_test=True`` (the test suite's loopback fake
  server; no product code passes it). TLS is the default ``ssl`` context: certificate and hostname verified.
* **The host is the URL's host.** The connection goes to the URL's host and port and nowhere else. The handshake answer
  is accepted only as ``101``; a redirect (or any other status) is a refusal and is never followed. ``allowed_hosts``
  lets the caller pin the URL's host to a set (``providers.ALLOWED_HOSTS``) before any socket opens.
* **Header values are never logged or raised.** Caller headers (the ``Authorization`` one) go into the request bytes and
  nowhere else: no attribute of the client, no exception text, no ``repr``, and no frame reachable from a raised error's
  traceback (:func:`connect` does the work in an inner frame that is gone before it raises a fresh error with no
  ``__context__``). :class:`WsError` carries a fixed code and a fixed sentence. A header name or value that could split
  the request (CR, LF, NUL, other controls) is refused, and so is a caller header that would override one the client
  owns (``Host``, ``Upgrade``, ``Connection``, ``Sec-WebSocket-*``).
* **The answer is checked:** status ``101``, ``Upgrade: websocket``, ``Connection`` containing ``upgrade``,
  ``Sec-WebSocket-Accept`` equal to the base64 SHA-1 of our key plus the RFC GUID, and no extension or sub-protocol
  (we offered none). The head must end within ``MAX_HANDSHAKE`` bytes. ``handshake_deadline`` is one wall-clock deadline
  over the whole open: TCP connect (each attempt also capped by ``connect_timeout``), TLS handshake, request send and
  head read. Running out of it anywhere is ``WsError("timeout")``. Known edge: name resolution (``getaddrinfo``) has no
  timeout in the stdlib and is not covered.
* **Frames:** client frames are masked with a fresh random key each; server frames must be unmasked, RSV bits zero,
  lengths minimally encoded, control frames <= 125 bytes and unfragmented, continuation rules obeyed, text valid UTF-8,
  close codes in the RFC 6455 / IANA sendable range. A message past ``max_message`` is refused from the declared length
  of the frame that would cross it, before that payload is read. A protocol violation sends a close frame with the
  matching code and closes the socket.

``recv()`` returns ``(kind, data)``: ``("text", str)``, ``("binary", bytes)`` or ``("close", (code, reason))``. Ping is
answered with a pong inside ``recv()`` and pong is swallowed. ``idle_timeout`` bounds the wait for a frame to start
(:class:`WsTimeout`, recoverable) and, once it has started, the time for the rest of it (fatal; enforced between socket
reads, so it can overrun by up to one more ``idle_timeout``).

Threads: one reader and any number of writers (sends are serialised by a lock). There is only ever one socket reader:
``recv`` holds a receive lock, and ``close()`` (callable from any thread) sends its close frame once and then either waits
for the active reader to see the peer's close or, when no reader is active, reads it itself.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections.abc import Iterable

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 30.0
HANDSHAKE_DEADLINE = 15.0
CLOSE_WAIT = 2.0
MAX_MESSAGE = 1 * 1024 * 1024
MAX_HANDSHAKE = 16 * 1024

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

WORDS = {
    "url": "The websocket address is not allowed.",
    "header": "A websocket header was refused.",
    "connect": "The websocket could not connect.",
    "handshake": "The websocket handshake was refused.",
    "protocol": "The websocket peer broke the protocol.",
    "too_big": "A websocket message was too large.",
    "timeout": "The websocket timed out.",
    "closed": "The websocket is closed.",
}

_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# the 101 headers _validate reads: a repeat of one of these is refused; any other header may repeat (Set-Cookie does, RFC 6265)
_READ_HEADERS = frozenset({"upgrade", "connection", "sec-websocket-accept", "sec-websocket-extensions", "sec-websocket-protocol"})
_OWNED = frozenset({"host", "upgrade", "connection", "content-length", "transfer-encoding"})


def _valid_close(code: int) -> bool:
    """Close codes a peer may put on the wire (RFC 6455 7.4 + the IANA registry): 1004-1006 and 1015 are reserved."""
    return 1000 <= code <= 1003 or 1007 <= code <= 1014 or 3000 <= code <= 4999


class WsError(Exception):
    """A websocket failure. ``code`` is a key of :data:`WORDS`; the message is that fixed sentence and nothing else (no
    URL, header, key, status line or peer text)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(WORDS[code])


class WsTimeout(WsError):
    """No frame started within the idle timeout and the stream is still intact (``recv`` may be called again)."""

    def __init__(self) -> None:
        super().__init__("timeout")


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _check_headers(headers: dict[str, str] | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not _TOKEN.match(name):
            raise WsError("header")
        low = name.lower()
        if low in _OWNED or low.startswith("sec-websocket-"):
            raise WsError("header")
        if any(ord(c) < 0x20 or ord(c) == 0x7F or ord(c) > 0xFF for c in value):
            raise WsError("header")
        out.append((name, value))
    return out


class WebSocketClient:
    """Use :func:`connect`; the constructor is not a public entry."""

    def __init__(self, sock: socket.socket, leftover: bytes, max_message: int, idle_timeout: float) -> None:
        self._sock = sock
        self._max = max_message
        self._idle = idle_timeout
        # The socket timeout stays at idle while writers may send: a socket timeout is shared by every thread using the
        # socket, so the reader may change it only once _close_sent is set (after that no writer touches the socket).
        sock.settimeout(idle_timeout)
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()   # held by the one socket reader; guards _buf, _frag_op, _frag
        self._state_lock = threading.Lock()  # guards the _closed transition
        self._done = threading.Event()       # set when the peer's close is seen or the socket is shut
        self._closed = False                 # the socket is shut
        self._close_sent = False
        self._close_seen = False
        self._buf = bytearray(leftover)
        self._frag_op: int | None = None
        self._frag = bytearray()

    def __repr__(self) -> str:
        return "WebSocketClient(open)" if not self._closed else "WebSocketClient(closed)"

    # ---- send side
    def _send_frame(self, opcode: int, payload: bytes, deadline: float | None = None) -> None:
        """Send one frame. A second close frame (from any thread) is a silent no-op; data after the close is refused.
        With ``deadline`` (``close()`` only) both the wait for the send lock and the wait for the socket to take the
        frame end by it."""
        n = len(payload)
        key = os.urandom(4)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        frame = head + key + _mask(payload, key)
        if deadline is None:
            self._send_lock.acquire()
        elif not self._send_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise WsError("timeout")                 # a writer is stuck in sendall; the caller drops the socket
        try:
            if self._closed:
                raise WsError("closed")
            if self._close_sent:
                if opcode == OP_CLOSE:
                    return
                raise WsError("closed")
            try:
                if deadline is not None:
                    # Never change the socket timeout here: a reader may be inside recv() on another thread, and the
                    # timeout is shared. With the send lock held no writer is mid-send, so the close frame (a few bytes)
                    # goes out at once once the socket is writable; wait for that by the deadline instead.
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([], [self._sock], [], left)[1]:
                        raise WsError("timeout")     # the peer stopped reading; the caller drops the socket
                self._sock.sendall(frame)
            except (OSError, ssl.SSLError, ValueError):
                self._shut()
                raise WsError("closed") from None
            if opcode == OP_CLOSE:
                self._close_sent = True
        finally:
            self._send_lock.release()

    def send_binary(self, data: bytes) -> None:
        if len(data) > self._max:
            raise WsError("too_big")
        self._send_frame(OP_BINARY, bytes(data))

    def send_text(self, text: str) -> None:
        raw = text.encode("utf-8")
        if len(raw) > self._max:
            raise WsError("too_big")
        self._send_frame(OP_TEXT, raw)

    def ping(self, data: bytes = b"") -> None:
        if len(data) > 125:
            raise WsError("too_big")
        self._send_frame(OP_PING, bytes(data))

    # ---- receive side (every method below runs with _recv_lock held)
    def _timed_out(self, started: bool) -> WsError:
        if not started:
            return WsTimeout()
        self._shut()
        return WsError("timeout")

    def _fill(self, n: int, started: bool, deadline: float) -> bytes:
        """Exactly ``n`` bytes by ``deadline``. A timeout before a frame has started (``started`` False: only header
        bytes are wanted and none consumed, any partial header stays in ``_buf``) is a recoverable :class:`WsTimeout`;
        one mid-frame leaves the stream unusable, so it closes."""
        while len(self._buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise self._timed_out(started)
            try:
                if self._close_sent:
                    self._sock.settimeout(min(self._idle, left))
                chunk = self._sock.recv(65536)
            except (socket.timeout, TimeoutError):
                raise self._timed_out(started) from None
            except (OSError, ssl.SSLError, ValueError):
                self._shut()
                raise WsError("closed") from None
            if not chunk:
                self._shut()
                raise WsError("closed")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _fail(self, code: int, err: str) -> WsError:
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except WsError:
            pass
        self._shut()
        return WsError(err)

    def _read_frame(self, until: float | None) -> tuple[bool, int, bytes]:
        b0, b1 = self._fill(2, False, until if until is not None else time.monotonic() + self._idle)
        deadline = until if until is not None else time.monotonic() + self._idle
        fin, rsv, op = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        if rsv or (b1 & 0x80) or op in (3, 4, 5, 6, 7) or op > 0xA:
            raise self._fail(1002, "protocol")
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._fill(2, True, deadline))
            if n < 126:
                raise self._fail(1002, "protocol")
        elif n == 127:
            (n,) = struct.unpack("!Q", self._fill(8, True, deadline))
            if n >> 63 or n < 65536:
                raise self._fail(1002, "protocol")
        if op >= 0x8:
            if n > 125 or not fin:
                raise self._fail(1002, "protocol")
        else:
            # Fragmentation state and the message budget are checked from the header, before any payload is read.
            if op == OP_CONT:
                if self._frag_op is None:
                    raise self._fail(1002, "protocol")
                room = self._max - len(self._frag)
            else:
                if self._frag_op is not None:
                    raise self._fail(1002, "protocol")
                room = self._max
            if n > room:
                raise self._fail(1009, "too_big")
        return fin, op, self._fill(n, True, deadline) if n else b""

    def recv(self) -> tuple[str, object]:
        with self._recv_lock:
            return self._recv(None)

    def _recv(self, until: float | None) -> tuple[str, object]:
        if self._closed:
            raise WsError("closed")
        while True:
            fin, op, payload = self._read_frame(until)
            if op == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except WsError:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                return self._on_close(payload)
            if op != OP_CONT:
                self._frag_op = op
            self._frag += payload
            if not fin:
                continue
            kind, data = self._frag_op, bytes(self._frag)
            self._frag_op, self._frag = None, bytearray()
            if kind == OP_TEXT:
                try:
                    return "text", data.decode("utf-8")
                except UnicodeDecodeError:
                    raise self._fail(1007, "protocol") from None
            return "binary", data

    def _on_close(self, payload: bytes) -> tuple[str, object]:
        code, reason = 1005, ""
        if len(payload) == 1:
            raise self._fail(1002, "protocol")
        if len(payload) >= 2:
            (code,) = struct.unpack("!H", payload[:2])
            if not _valid_close(code):
                raise self._fail(1002, "protocol")
            try:
                reason = payload[2:].decode("utf-8")
            except UnicodeDecodeError:
                raise self._fail(1007, "protocol") from None
        self._close_seen = True
        try:
            self._send_frame(OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")   # no-op if ours already went
        except WsError:
            pass
        self._shut()
        return "close", (code, reason)

    # ---- close
    def _shut(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        # shutdown() wakes a reader blocked in recv() on another thread; the base-class call skips SSLSocket.shutdown,
        # which would drop the SSL object out from under that reader.
        try:
            socket.socket.shutdown(self._sock, socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._done.set()

    def close(self, code: int = 1000, wait: float = CLOSE_WAIT) -> None:
        """Send a close frame (once, from any thread) and wait for the peer's, all within ``wait`` seconds, then drop the
        socket. If a reader is in ``recv()`` it stays the only socket reader and gets the peer's close; otherwise this
        reads it. A writer stuck in a send past ``wait`` gets ``WsError("closed")`` when the socket drops. After close()
        starts, data frames not yet read may be dropped. A ``code`` a peer may not send (1005, 1006, 1015, out of
        range) goes out as 1000. Idempotent; never raises."""
        if self._closed:
            return
        if not _valid_close(code):
            code = 1000
        deadline = time.monotonic() + wait
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code), deadline)
            while not self._done.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                if self._recv_lock.acquire(timeout=min(left, 0.05)):
                    try:
                        while not self._close_seen and not self._closed and time.monotonic() < deadline:
                            try:
                                self._recv(deadline)
                            except WsTimeout:
                                continue             # idle before a frame started: the close wait is not over
                            except WsError:
                                break
                    finally:
                        self._recv_lock.release()
                    break
        except Exception:  # noqa: BLE001 - close must not raise; the socket is dropped below
            pass
        finally:
            self._shut()


def connect(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    connect_timeout: float = CONNECT_TIMEOUT,
    idle_timeout: float = IDLE_TIMEOUT,
    handshake_deadline: float = HANDSHAKE_DEADLINE,
    max_message: int = MAX_MESSAGE,
    allowed_hosts: Iterable[str] | None = None,
    insecure_for_test: bool = False,
) -> WebSocketClient:
    """Open ``url`` (``wss://``) and complete the handshake. ``headers`` may hold the provider's ``Authorization``.

    The work happens in :func:`_open`. On failure its frames (which hold the headers and the request bytes) are dropped
    and a fresh :class:`WsError` is raised from here, outside the ``except`` block (no ``__context__``), after this frame
    has let go of ``headers``. An unexpected non-``WsError`` exception is reported as ``connect`` for the same reason."""
    failed = "connect"
    try:
        return _open(url, headers, connect_timeout, idle_timeout, handshake_deadline, max_message, allowed_hosts,
                     insecure_for_test)
    except WsError as e:
        failed = e.code
    except Exception:  # noqa: BLE001 - its traceback would carry the secret; see the docstring
        failed = "connect"
    headers = None  # noqa: F841 - drop the caller's dict from this frame before raising
    raise WsError(failed)


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise WsError("timeout")
    return left


def _dial(host: str, port: int, connect_timeout: float, deadline: float) -> socket.socket:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise WsError("connect") from None
    timed_out = False
    for family, kind, proto, _, addr in infos:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(min(connect_timeout, left))
            sock.connect(addr)
            return sock
        except (socket.timeout, TimeoutError):
            timed_out = True
        except OSError:
            pass
        sock.close()
    raise WsError("timeout" if timed_out else "connect")


def _open(url: str, headers: dict[str, str] | None, connect_timeout: float, idle_timeout: float,
          handshake_deadline: float, max_message: int, allowed_hosts: Iterable[str] | None,
          insecure_for_test: bool) -> WebSocketClient:
    extra = _check_headers(headers)
    try:
        u = urllib.parse.urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:
        raise WsError("url") from None
    scheme_ok = u.scheme == "wss" or (insecure_for_test and u.scheme == "ws")
    if not scheme_ok or not host or u.username is not None or u.password is not None or u.fragment:
        raise WsError("url")
    if allowed_hosts is not None and host.lower() not in {h.lower() for h in allowed_hosts}:
        raise WsError("url")
    tls = u.scheme == "wss"
    port = port or (443 if tls else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F for c in path):
        raise WsError("url")

    deadline = time.monotonic() + handshake_deadline
    sock = _dial(host, port, connect_timeout, deadline)
    try:
        if tls:
            try:
                ctx = ssl.create_default_context()
                sock.settimeout(_left(deadline))        # sampled after the context exists; the handshake runs under it
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except (socket.timeout, TimeoutError):
                raise WsError("timeout") from None
            except (OSError, ssl.SSLError, ValueError):
                raise WsError("connect") from None
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        hostline = host if ":" not in host else f"[{host}]"
        if (u.port is not None) and u.port != (443 if tls else 80):
            hostline += f":{u.port}"
        lines = [f"GET {path} HTTP/1.1", f"Host: {hostline}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{n}: {v}" for n, v in extra]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        try:
            sock.settimeout(_left(deadline))
            sock.sendall(request)
        except (socket.timeout, TimeoutError):
            raise WsError("timeout") from None
        except (OSError, ssl.SSLError):
            raise WsError("handshake") from None
        del request, lines, extra
        raw = _read_head(sock, deadline)
        head, _, leftover = raw.partition(b"\r\n\r\n")
        _validate(head, key)
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise
    return WebSocketClient(sock, leftover, max_message, idle_timeout)


def _read_head(sock: socket.socket, deadline: float) -> bytes:
    """Read until the blank line that ends the head. The head (terminator included) must end within MAX_HANDSHAKE
    bytes: each read is bounded to the remaining allowance, so the buffer never grows past it."""
    buf = bytearray()
    while True:
        room = MAX_HANDSHAKE - len(buf)
        if room <= 0:
            raise WsError("handshake")
        left = deadline - time.monotonic()
        if left <= 0:
            raise WsError("timeout")
        try:
            sock.settimeout(left)
            chunk = sock.recv(min(4096, room))
        except (socket.timeout, TimeoutError):
            raise WsError("timeout") from None
        except (OSError, ssl.SSLError):
            raise WsError("handshake") from None
        if not chunk:
            raise WsError("handshake")
        start = max(0, len(buf) - 3)
        buf += chunk
        if buf.find(b"\r\n\r\n", start) >= 0:
            return bytes(buf)


def _validate(head: bytes, key: str) -> None:
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes everything
        raise WsError("handshake") from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or parts[0] != "HTTP/1.1" or parts[1] != "101":
        raise WsError("handshake")
    seen: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN.match(name):
            raise WsError("handshake")
        low = name.lower()
        if low in _READ_HEADERS and low in seen:     # an ambiguous answer to a header this check reads; any other may repeat
            raise WsError("handshake")
        seen[low] = value.strip()
    if seen.get("upgrade", "").lower() != "websocket":
        raise WsError("handshake")
    if "upgrade" not in [t.strip().lower() for t in seen.get("connection", "").split(",")]:
        raise WsError("handshake")
    if "sec-websocket-extensions" in seen or "sec-websocket-protocol" in seen:
        raise WsError("handshake")
    want = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    if seen.get("sec-websocket-accept") != want:
        raise WsError("handshake")
