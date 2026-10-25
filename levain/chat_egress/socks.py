"""The chat launch's SOCKS port: a Levain listener that refuses everything (design §3.8.5(o) item 1; CH-D2 S8).

The design's `chat_sandbox_block(egress="proxy")` points the sandbox's SOCKS port here. A SOCKS5
client is offered no-auth, its request read, and answered with reply `0x02` (connection not allowed by ruleset),
then closed; anything else (SOCKS4, garbage, a client that stalls past `IO_TIMEOUT_S`) is closed with nothing
answered. No connection is ever made and nothing the client sent is recorded or logged.
"""

from __future__ import annotations

import socket
import threading


IO_TIMEOUT_S = 5.0
_ACCEPT_POLL_S = 0.2
_REFUSED = b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00"   # VER, REP=0x02, RSV, ATYP=IPv4, 0.0.0.0:0


def _exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _serve(conn: socket.socket) -> None:
    head = _exact(conn, 2)
    if head is None or head[0] != 5 or head[1] == 0 or _exact(conn, head[1]) is None:
        return
    conn.sendall(b"\x05\x00")
    req = _exact(conn, 4)
    if req is None or req[0] != 5:
        return
    atyp = req[3]
    if atyp == 1:
        rest = _exact(conn, 4 + 2)
    elif atyp == 4:
        rest = _exact(conn, 16 + 2)
    elif atyp == 3:
        n = _exact(conn, 1)
        rest = None if n is None else _exact(conn, n[0] + 2)
    else:
        rest = b""
    if rest is not None:
        conn.sendall(_REFUSED)


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
            conn.settimeout(IO_TIMEOUT_S)
            try:
                _serve(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()

    def close(self, timeout_s: float = IO_TIMEOUT_S + 1.0) -> bool:
        """Stop accepting; True when the serving thread ended inside `timeout_s`."""
        self._closed.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout_s)
            return not self._thread.is_alive()
        return True
