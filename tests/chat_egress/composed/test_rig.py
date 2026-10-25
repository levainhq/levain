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
    made: list[RecordingOrigin] = []

    class Spy(RecordingOrigin):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            made.append(self)

    def boom(**kw):
        raise RuntimeError("start_egress failed")
    monkeypatch.setattr(tls_rig, "RecordingOrigin", Spy)
    monkeypatch.setattr(lm, "start_egress", boom)
    with pytest.raises(RuntimeError, match="start_egress failed"):
        tls_rig.TlsLaunch(tmp_path / "T", monkeypatch)
    assert len(made) == 1 and not made[0]._t.is_alive()


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
            TlsLaunch._no_sni(ssl.create_default_context(), c, time.monotonic() + 5)
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
