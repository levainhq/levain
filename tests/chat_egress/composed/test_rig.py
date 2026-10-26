"""The composed suite's own rig: RecordingOrigin's shutdown leaves no accept or worker thread behind."""

from __future__ import annotations

import socket
import threading

import pytest

from .rig import RecordingOrigin


def test_close_joins_the_accept_thread_and_every_worker_even_with_connects_racing_it() -> None:
    for _ in range(20):
        o = RecordingOrigin()
        stop = threading.Event()
        socks: list[socket.socket] = []

        def hammer() -> None:
            while not stop.is_set():
                try:
                    socks.append(socket.create_connection(("127.0.0.1", o.port), timeout=1))
                except OSError:
                    return

        h = threading.Thread(target=hammer, daemon=True)
        h.start()
        while o.conns < 3:
            pass
        o.close()
        stop.set()
        h.join(5)
        assert not o._t.is_alive()
        assert not [t for t in o._threads if t.is_alive()]
        for s in socks:
            s.close()


def test_close_shuts_down_a_connection_the_tls_wrap_took_over(tmp_path) -> None:
    """The wrap detaches the accepted fd; close() must reach the wrapped socket, or a worker blocked reading it outlives
    the join."""
    import ssl
    import time
    from .tls_rig import UNKEYED, make_pki
    ca, leaf, key = make_pki(tmp_path / "pki", [UNKEYED])
    o = RecordingOrigin(tls=(str(leaf), str(key)))
    ctx = ssl.create_default_context(cafile=str(ca))
    c = ctx.wrap_socket(socket.create_connection(("127.0.0.1", o.port), timeout=5), server_hostname=UNKEYED)
    try:
        t0 = time.monotonic()
        o.close()                                            # the worker is blocked reading the idle TLS connection
        took = time.monotonic() - t0
        assert not [t for t in o._threads if t.is_alive()]
        assert took < 4, took
    finally:
        c.close()


def test_a_failed_tls_launch_constructor_closes_the_origin_it_started(tmp_path, monkeypatch) -> None:
    from levain.chat_egress import launch as lm
    from . import tls_rig
    from ..fixtures import env as envmod
    made: list[RecordingOrigin] = []

    class Spy(RecordingOrigin):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            made.append(self)

    def boom(**kw):
        raise RuntimeError("start_egress failed")
    monkeypatch.setattr(tls_rig, "RecordingOrigin", Spy)
    monkeypatch.setattr(lm, "start_egress", boom)
    monkeypatch.setattr(envmod, "srt_prefix", lambda: tmp_path)   # the constructor reads it before start_egress runs
    with pytest.raises(RuntimeError, match="start_egress failed"):
        tls_rig.TlsLaunch(tmp_path / "T", monkeypatch)
    assert len(made) == 1 and not made[0]._t.is_alive()


def _no_name_ctx():
    """The context `TlsLaunch.tunnel` hands `_no_sni`: no name check (a memory-BIO handshake has no server_hostname,
    and `wrap_bio` refuses `check_hostname=True` without one)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    return ctx


def test_the_no_sni_handshake_fails_on_a_peer_eof_inside_the_deadline() -> None:
    """A peer that closes during the handshake used to spin the loop forever (an empty recv written to the BIO)."""
    import ssl
    import time
    from .tls_rig import TlsLaunch
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=5)
    peer, _ = srv.accept()
    peer.shutdown(socket.SHUT_WR)                          # EOF to the client, the peer still open: the old loop spun
    srv.close()
    t0 = time.monotonic()
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, TimeoutError)):
            TlsLaunch._no_sni(_no_name_ctx(), c, time.monotonic() + 5)
    finally:
        c.close()
        peer.close()
    assert time.monotonic() - t0 < 4


def test_tls_launch_close_refuses_a_stop_that_was_not_clean(tmp_path) -> None:
    from levain.chat_egress import launch as lm
    from .tls_rig import TlsLaunch
    L = object.__new__(TlsLaunch)
    closed: list[int] = []
    L.stopped = lm.StopResult(glue_exit=None, decider_clean=True, socks_clean=True, final_flush=None, order=(),
                              failures=("glue: TimeoutExpired",))
    L.origin = type("O", (), {"close": lambda self: closed.append(1)})()
    with pytest.raises(AssertionError):
        L.close()
    assert closed == [1]


def _silent_peer(client_timeout: float):
    """A connected pair whose peer never speaks: a blocking read on the client side waits out the socket's own timeout."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=client_timeout)
    peer, _ = srv.accept()
    srv.close()
    return c, peer


def test_the_no_sni_handshake_is_bounded_by_its_deadline_not_the_sockets_timeout() -> None:
    """codex measured a 0.2 s deadline returning after ~0.97 s: the loop checked the deadline, then blocked in a recv
    whose own timeout was longer."""
    import ssl
    import time
    from .tls_rig import TlsLaunch
    c, peer = _silent_peer(5.0)
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            TlsLaunch._no_sni(_no_name_ctx(), c, t0 + 0.2)
    finally:
        c.close()
        peer.close()
    assert time.monotonic() - t0 < 0.7, time.monotonic() - t0


def test_a_bio_socket_read_is_bounded_by_its_deadline_not_the_sockets_timeout() -> None:
    import ssl
    import time
    from .tls_rig import _BioSocket
    c, peer = _silent_peer(5.0)
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = _no_name_ctx().wrap_bio(inc, out, server_side=False)
    t0 = time.monotonic()
    b = _BioSocket(obj, inc, out, c, t0 + 0.2)
    try:
        with pytest.raises(TimeoutError):
            b.recv(10)
    finally:
        c.close()
        peer.close()
    assert time.monotonic() - t0 < 0.7, time.monotonic() - t0


def test_tls_launch_close_refuses_a_clean_exit_with_no_final_flush(tmp_path) -> None:
    """glue_exit 0 with no final summary is the case test_launch.py's teardown rejects."""
    from levain.chat_egress import launch as lm
    from .tls_rig import TlsLaunch
    L = object.__new__(TlsLaunch)
    closed: list[int] = []
    L.stopped = lm.StopResult(glue_exit=0, decider_clean=True, socks_clean=True, final_flush=None, order=())
    L.origin = type("O", (), {"close": lambda self: closed.append(1)})()
    with pytest.raises(AssertionError):
        L.close()
    assert closed == [1]


def test_tunnel_closes_its_socket_when_the_no_sni_handshake_fails(tmp_path) -> None:
    """A CONNECT answered 200 whose tunnel then closes: the handshake raises, and the client socket must not leak."""
    import ssl
    from .tls_rig import TlsLaunch, make_pki
    ca, _, _ = make_pki(tmp_path / "pki", ["x.test"])
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    L = object.__new__(TlsLaunch)
    L.http_port, L.origin_port, L.ca_pem = srv.getsockname()[1], 1, ca.read_bytes()
    made: list[socket.socket] = []
    real = socket.create_connection

    def spy(*a, **k):
        s = real(*a, **k)
        made.append(s)
        return s

    def peer() -> None:
        c, _ = srv.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        c.close()                                            # EOF where a ServerHello should be

    th = threading.Thread(target=peer, daemon=True)
    th.start()
    socket.create_connection = spy
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, TimeoutError)):
            L.tunnel("x.test", None, timeout=5.0)
    finally:
        socket.create_connection = real
        th.join(5)
        srv.close()
    assert len(made) == 1 and made[0].fileno() == -1


def test_a_wrapper_registered_after_close_began_is_closed_at_once() -> None:
    """The race codex named: close() snapshots after wrap_socket detached the raw fd and before the worker registered the
    wrapper. A hook parks the worker between the two until close() has snapshotted: no sleeps."""
    import ssl
    import tempfile
    from pathlib import Path
    from .tls_rig import UNKEYED, make_pki
    with tempfile.TemporaryDirectory() as d:
        ca, leaf, key = make_pki(Path(d) / "pki", [UNKEYED])
        o = RecordingOrigin(tls=(str(leaf), str(key)))
        wrapped = threading.Event()

        seen: list = []

        def after_wrap(_c) -> None:
            seen.append(_c)
            wrapped.set()
            assert o.snapshotted.wait(10)

        o.after_wrap = after_wrap
        ctx = ssl.create_default_context(cafile=str(ca))
        box: list[ssl.SSLSocket] = []
        cl = threading.Thread(target=lambda: box.append(ctx.wrap_socket(
            socket.create_connection(("127.0.0.1", o.port), timeout=10), server_hostname=UNKEYED)), daemon=True)
        cl.start()
        assert wrapped.wait(10)
        o.close()                                            # snapshots, releases the worker, joins it
        cl.join(10)
        try:
            assert not [t for t in o._threads if t.is_alive()]
            assert o._socks == [] or all(s.fileno() == -1 for s in o._socks), o._socks
            assert seen and seen[0].fileno() == -1, seen          # the late wrapper itself is closed, not left to refcounting
        finally:
            for s in box:
                s.close()


def test_leaky_relay_close_leaves_no_socket_or_thread_from_an_accept_that_raced_it() -> None:
    from .rig import LeakyRelay
    for dialect in ("raw", "connect"):
        r = LeakyRelay(1, dialect)
        accepted = threading.Event()

        def after_accept(_c) -> None:
            accepted.set()
            assert r.stopping.wait(10)                       # parked in the accept thread until close() has begun

        r.after_accept = after_accept
        c = socket.create_connection(("127.0.0.1", r.port), timeout=10)
        try:
            assert accepted.wait(10)
            r.close()
            assert not [t for t in r._threads if t.is_alive()]
            assert all(s.fileno() == -1 for s in r._socks), r._socks
            c.settimeout(5)
            assert c.recv(1) == b""                          # the raced connection was closed, not parked
        finally:
            c.close()
