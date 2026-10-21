"""S1 session 2d: the egress forwarder (CONTRACT §9.1; levain/slot/forwarder.py) over a REAL TCP socket and a REAL
unix socket in a tmp dir. No root, no namespace: the netns + view it runs inside are the wall's (2g VM rows)."""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading

import pytest

from levain.slot import forwarder


@pytest.fixture
def sockdir():
    d = tempfile.mkdtemp(prefix="lvfw", dir="/tmp")   # short: macOS caps a unix socket path at 104 bytes
    yield d
    shutil.rmtree(d, ignore_errors=True)


class UnixServer:
    """A stand-in front: records each accepted connection and answers with what the handler returns."""

    def __init__(self, path, handler):
        self.path, self.handler, self.accepted = path, handler, 0
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(path)
        self.s.listen(16)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self.handler, args=(c,), daemon=True).start()

    def close(self):
        self.s.close()


def _read_all(c):
    out = b""
    while True:
        b = c.recv(65536)
        if not b:
            return out
        out += b


def _echo_after_eof(c):
    """Reads to the client's EOF, THEN answers: proves the half-close reaches the front."""
    data = _read_all(c)
    c.sendall(b"got:" + data)
    c.close()


@pytest.fixture
def relay(sockdir):
    started = []

    def start(handler, **kw):
        path = os.path.join(sockdir, "gw.sock")
        srv = UnixServer(path, handler)
        stop, ready = threading.Event(), threading.Event()
        port = []
        t = threading.Thread(target=forwarder.serve, args=(path,),
                             kwargs=dict(port=0, stop=stop, ready=lambda p: (port.append(p), ready.set()), **kw),
                             daemon=True)
        t.start()
        assert ready.wait(5)
        started.append((srv, stop, t))
        return srv, port[0]
    yield start
    for srv, stop, t in started:
        stop.set()
        t.join(5)
        srv.close()


def _connect(port):
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    return c


def test_relays_bytes_both_ways_unparsed(relay):
    srv, port = relay(_echo_after_eof)
    payload = b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n\x00\xff" + os.urandom(200_000)
    c = _connect(port)
    c.sendall(payload)
    c.shutdown(socket.SHUT_WR)
    assert _read_all(c) == b"got:" + payload
    c.close()


def test_one_tcp_connection_is_one_unix_connection(relay):
    srv, port = relay(_echo_after_eof)
    for i in range(3):
        c = _connect(port)
        c.sendall(b"x%d" % i)
        c.shutdown(socket.SHUT_WR)
        assert _read_all(c) == b"got:x%d" % i
        c.close()
    assert srv.accepted == 3


def test_front_down_closes_the_client(relay, sockdir):
    srv, port = relay(_echo_after_eof)
    srv.close()
    os.unlink(os.path.join(sockdir, "gw.sock"))
    c = _connect(port)
    assert _read_all(c) == b""   # closed, nothing relayed anywhere
    c.close()


def test_listens_on_loopback_only(monkeypatch, sockdir):
    bound = []
    real = socket.socket

    class Spy(real):
        def bind(self, addr):
            bound.append(addr)
            return super().bind(addr)
    monkeypatch.setattr(forwarder.socket, "socket", Spy)
    stop = threading.Event()
    stop.set()
    forwarder.serve(os.path.join(sockdir, "gw.sock"), port=0, stop=stop)
    assert bound == [("127.0.0.1", 0)]


def test_connection_cap_closes_extras(relay):
    hold = threading.Event()

    def hold_open(c):
        hold.wait(5)
        c.close()
    srv, port = relay(hold_open, max_conns=1)
    first = _connect(port)
    first.sendall(b"a")
    for _ in range(50):          # let the first relay take its slot
        if srv.accepted == 1:
            break
        threading.Event().wait(0.05)
    second = _connect(port)
    assert _read_all(second) == b""
    hold.set()
    first.close()
    second.close()
    assert srv.accepted == 1


@pytest.mark.parametrize("argv", [[], ["--port", "47100"], ["--port", "x", "--unix", "/a"],
                                  ["--port", "0", "--unix", "/a"], ["--port", "47100", "--unix", "rel"]])
def test_main_refuses_any_other_argv(argv):
    assert forwarder.main(argv) == 2
