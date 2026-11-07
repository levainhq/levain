"""A small stdlib-only RFC 6455 WebSocket client, for one outbound provider stream (cockpit mic slice S2).

It carries a provider key in a handshake header, so what it refuses matters more than what it sends:

* **wss only.** ``ws://`` is refused unless the caller passes ``insecure_for_test=True`` (the test suite's loopback fake
  server; no product code passes it). TLS is the default ``ssl`` context: certificate and hostname verified.
* **The host is the URL's host.** The connection goes to the URL's host and port and nowhere else. The handshake answer
  is accepted only as ``101``; a redirect (or any other status) is a refusal and is never followed. ``allowed_hosts``
  lets the caller pin the URL's host to a set (``providers.ALLOWED_HOSTS``) before any socket opens.
* **Header values are never logged or raised.** Caller headers (the ``Authorization`` one) go into the request bytes and
  nowhere else: no attribute of the client, no exception text, no ``repr``. :class:`WsError` carries a fixed code and a
  fixed sentence. A header name or value that could split the request (CR, LF, NUL, other controls) is refused, and so is
  a caller header that would override one the client owns (``Host``, ``Upgrade``, ``Connection``, ``Sec-WebSocket-*``).
* **The answer is checked:** status ``101``, ``Upgrade: websocket``, ``Connection`` containing ``upgrade``,
  ``Sec-WebSocket-Accept`` equal to the base64 SHA-1 of our key plus the RFC GUID, and no extension or sub-protocol
  (we offered none). The handshake has a header-size cap and a wall-clock deadline.
* **Frames:** client frames are masked with a fresh random key each; server frames must be unmasked, RSV bits zero, control
  frames <= 125 bytes and unfragmented, continuation rules obeyed, text valid UTF-8. A message past ``max_message``
  is refused from its declared length, before its payload is read. A protocol violation sends a close frame with the
  matching code and closes the socket.

``recv()`` returns ``(kind, data)``: ``("text", str)``, ``("binary", bytes)`` or ``("close", (code, reason))``. Ping is
answered with a pong inside ``recv()`` and pong is swallowed. One reader and one writer thread may use a client at once
(sends are serialised by a lock; ``recv`` is for a single reader).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
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
_OWNED = frozenset({"host", "upgrade", "connection", "content-length", "transfer-encoding"})
_VALID_CLOSE = lambda c: 1000 <= c <= 1003 or 1007 <= c <= 1011 or 3000 <= c <= 4999  # noqa: E731


class WsError(Exception):
    """A websocket failure. ``code`` is a key of :data:`WORDS`; the message is that fixed sentence and nothing else (no
    URL, header, key, status line or peer text)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(WORDS[code])


class WsTimeout(WsError):
    """No frame arrived within the idle timeout and the stream is still intact (``recv`` may be called again)."""

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
        self._buf = bytearray(leftover)
        self._max = max_message
        self._idle = idle_timeout
        self._send_lock = threading.Lock()
        self._closed = False            # the socket is shut
        self._close_sent = False
        self._close_seen = False
        self._frag_op: int | None = None
        self._frag = bytearray()

    def __repr__(self) -> str:
        return "WebSocketClient(open)" if not self._closed else "WebSocketClient(closed)"

    # ---- send side
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        key = os.urandom(4)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        frame = head + key + _mask(payload, key)
        with self._send_lock:
            if self._closed:
                raise WsError("closed")
            if self._close_sent and opcode != OP_CLOSE:
                raise WsError("closed")
            try:
                self._sock.settimeout(self._idle)
                self._sock.sendall(frame)
            except (OSError, ssl.SSLError):
                self._shut()
                raise WsError("closed") from None
            if opcode == OP_CLOSE:
                self._close_sent = True

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

    # ---- receive side
    def _fill(self, n: int, started: bool) -> bytes:
        """Exactly ``n`` bytes. A timeout before any byte of a frame (``started`` False, buffer empty) is a
        recoverable :class:`WsTimeout`; one mid-frame leaves the stream unusable, so it closes."""
        while len(self._buf) < n:
            try:
                self._sock.settimeout(self._idle)
                chunk = self._sock.recv(65536)
            except (socket.timeout, TimeoutError):
                if not started and not self._buf:
                    raise WsTimeout() from None
                self._shut()
                raise WsError("timeout") from None
            except (OSError, ssl.SSLError):
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
            if not self._close_sent and not self._closed:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
        except WsError:
            pass
        self._shut()
        return WsError(err)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._fill(2, False)
        fin, rsv, op = bool(b0 & 0x80), b0 & 0x70, b0 & 0x0F
        if rsv or (b1 & 0x80) or op in (3, 4, 5, 6, 7) or op > 0xA:
            raise self._fail(1002, "protocol")
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._fill(2, True))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._fill(8, True))
            if n >> 63:
                raise self._fail(1002, "protocol")
        if op >= 0x8:
            if n > 125 or not fin:
                raise self._fail(1002, "protocol")
        elif n > self._max:
            raise self._fail(1009, "too_big")
        return fin, op, self._fill(n, True) if n else b""

    def recv(self) -> tuple[str, object]:
        if self._closed and not self._buf:
            raise WsError("closed")
        while True:
            fin, op, payload = self._read_frame()
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
            if op == OP_CONT:
                if self._frag_op is None:
                    raise self._fail(1002, "protocol")
            else:
                if self._frag_op is not None:
                    raise self._fail(1002, "protocol")
                self._frag_op = op
            self._frag += payload
            if len(self._frag) > self._max:
                raise self._fail(1009, "too_big")
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
            if not _VALID_CLOSE(code):
                raise self._fail(1002, "protocol")
            try:
                reason = payload[2:].decode("utf-8")
            except UnicodeDecodeError:
                raise self._fail(1007, "protocol") from None
        self._close_seen = True
        if not self._close_sent:
            try:
                self._send_frame(OP_CLOSE, payload[:2] if len(payload) >= 2 else b"")
            except WsError:
                pass
        self._shut()
        return "close", (code, reason)

    # ---- close
    def _shut(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def close(self, code: int = 1000, wait: float = CLOSE_WAIT) -> None:
        """Send a close frame, wait up to ``wait`` seconds for the peer's, then drop the socket. Idempotent; never raises."""
        if self._closed:
            return
        try:
            if not self._close_sent:
                self._send_frame(OP_CLOSE, struct.pack("!H", code))
            deadline = time.monotonic() + wait
            self._idle = max(0.05, min(self._idle, wait))
            while not self._close_seen and not self._closed and time.monotonic() < deadline:
                try:
                    self.recv()
                except WsTimeout:
                    break
                except WsError:
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
    """Open ``url`` (``wss://``) and complete the handshake. ``headers`` may hold the provider's ``Authorization``."""
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

    started = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
    except (OSError, ValueError):
        raise WsError("connect") from None
    try:
        if tls:
            try:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            except (OSError, ssl.SSLError):
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
            sock.settimeout(connect_timeout)
            sock.sendall(request)
        except (OSError, ssl.SSLError):
            raise WsError("handshake") from None
        del request, lines
        raw = _read_head(sock, started + handshake_deadline)
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
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0 or len(buf) > MAX_HANDSHAKE:
            raise WsError("handshake")
        try:
            sock.settimeout(left)
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError):
            raise WsError("timeout") from None
        except (OSError, ssl.SSLError):
            raise WsError("handshake") from None
        if not chunk:
            raise WsError("handshake")
        buf += chunk
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
        if low in seen:
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
