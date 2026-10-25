"""The chat launch's SOCKS port: a Levain listener that refuses everything (design §3.8.5(o) item 1; CH-D2 S8).

The design's `chat_sandbox_block(egress="proxy")` points the sandbox's SOCKS port here. A SOCKS5
client is offered no-auth, its request read, and answered with reply `0x02` (connection not allowed by ruleset),
then closed; a SOCKS5 client not offering no-auth is answered "no acceptable methods" (`0xff`) and closed; anything
else (SOCKS4, garbage) is closed with nothing answered. One connection is served at a time,
and the whole exchange (draining included) has one absolute deadline, `IO_TIMEOUT_S` from the accept, so a client
trickling bytes holds the listener no longer than that; `close()` shuts the connection being served. No connection
is ever made and nothing the client sent is recorded or logged.
"""

from __future__ import annotations

import socket
import threading
import time


IO_TIMEOUT_S = 5.0                   # one connection's whole exchange, from its accept
_ACCEPT_POLL_S = 0.2
_DRAIN_S = 0.5                       # after our FIN, how long the client's unread bytes are read off (inside the deadline)
_REFUSED = b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00"   # VER, REP=0x02, RSV, ATYP=IPv4, 0.0.0.0:0


def _recv(conn: socket.socket, n: int, deadline: float) -> bytes:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("connection deadline")
    conn.settimeout(left)
    return conn.recv(n)


def _exact(conn: socket.socket, n: int, deadline: float) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = _recv(conn, n - len(buf), deadline)
        if not chunk:
            return None
        buf += chunk
    return buf


def _serve(conn: socket.socket, deadline: float) -> None:
    head = _exact(conn, 2, deadline)
    methods = None if head is None or head[0] != 5 or head[1] == 0 else _exact(conn, head[1], deadline)
    if methods is None:
        return
    if 0 not in methods:                     # no-auth not offered: "no acceptable methods", then closed
        conn.sendall(b"\x05\xff")
        return
    conn.sendall(b"\x05\x00")
    req = _exact(conn, 4, deadline)
    if req is None or req[0] != 5:
        return
    atyp = req[3]
    if atyp == 1:
        rest = _exact(conn, 4 + 2, deadline)
    elif atyp == 4:
        rest = _exact(conn, 16 + 2, deadline)
    elif atyp == 3:
        n = _exact(conn, 1, deadline)
        rest = None if n is None else _exact(conn, n[0] + 2, deadline)
    else:
        rest = b""
    if rest is not None:
        conn.sendall(_REFUSED)


def _drain(conn: socket.socket, deadline: float) -> None:
    """Our FIN, then the client's unread bytes read off until its EOF, so the close sends no RST over data the client
    is still to read (a SOCKS4 client must see end of stream)."""
    conn.shutdown(socket.SHUT_WR)
    until = min(deadline, time.monotonic() + _DRAIN_S)
    while _recv(conn, 4096, until):
        pass


class SocksRefuser:
    """One launch's SOCKS listener on 127.0.0.1. `start()` binds and serves; `close()` stops accepting and joins.
    `served` counts the connections accepted."""

    def __init__(self, sock: socket.socket | None = None) -> None:
        if sock is None:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen(16)
        self._sock = sock
        self.port: int = sock.getsockname()[1]
        self.served = 0
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active: socket.socket | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="levain-egress-socks", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self._sock.settimeout(_ACCEPT_POLL_S)        # close() is seen inside one poll, whatever a platform does to a blocked accept()
        while not self._closed.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.served += 1
            deadline = time.monotonic() + IO_TIMEOUT_S
            with self._lock:
                self._active = conn
                closing = self._closed.is_set()
            try:
                if not closing:
                    _serve(conn, deadline)
                    _drain(conn, deadline)
            except OSError:
                pass
            finally:
                with self._lock:
                    self._active = None
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()

    def close(self, timeout_s: float = IO_TIMEOUT_S + 1.0) -> bool:
        """Stop accepting and shut the connection being served; True when the serving thread ended inside
        `timeout_s`."""
        with self._lock:
            self._closed.set()
            if self._active is not None:
                try:
                    self._active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout_s)
            return not self._thread.is_alive()
        return True
