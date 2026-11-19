"""The fixtures allocate ports, release them, and a child binds later: another process can take one in between (CI hit
EADDRINUSE on the rawlisten port). A start that loses the race re-picks fresh ports and tries again, bounded."""

import socket

import pytest

from .fixtures.procs import tcp_listening
from .fixtures.stack import START_TRIES, Rig


def _hold_tcp(port: int) -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def test_raw_listener_repicks_when_its_port_was_taken(tmp_path):
    rig = Rig(tmp_path / "rig")
    taken = rig.ports.raw
    holder = _hold_tcp(taken)
    try:
        port = rig.start_raw_listener()
        assert port != taken and port == rig.ports.raw
        assert tcp_listening(port)
    finally:
        rig.stop()
        holder.close()


def test_origin_repicks_when_a_port_was_taken(tmp_path):
    rig = Rig(tmp_path / "rig")
    taken = rig.ports.off_tls
    holder = _hold_tcp(taken)
    try:
        rig.start_pki()
        rig.start_origin("bytes")
        assert taken not in rig.ports.tcp()
        assert all(tcp_listening(p) for p in (rig.ports.http, rig.ports.tls, rig.ports.off_http, rig.ports.off_tls))
    finally:
        rig.stop()
        holder.close()


def test_dns_repicks_when_its_udp_port_was_taken(tmp_path):
    rig = Rig(tmp_path / "rig")
    taken = rig.ports.dns
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", taken))
    try:
        rig.start_dns()
        assert rig.ports.dns != taken
    finally:
        rig.stop()
        holder.close()


def test_the_retry_is_bounded_and_an_explicit_port_is_not_re_picked(tmp_path):
    rig = Rig(tmp_path / "rig")
    holder = _hold_tcp(0)
    held = holder.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match="address already in use"):
            rig.start_raw_listener(port=held)      # the caller named the port: one try, the error stands
    finally:
        rig.stop()
        holder.close()


def test_the_retry_gives_up_after_start_tries_and_re_raises(tmp_path):
    rig = Rig(tmp_path / "rig")
    calls = []

    def always_taken():
        calls.append(1)
        raise RuntimeError("rawlisten: no 'RAWLISTEN READY' line (rc=1); stderr tail: OSError: Address already in use")

    with pytest.raises(RuntimeError, match="(?i)address already in use"):
        rig._start_fresh_ports(("raw",), always_taken)
    assert len(calls) == START_TRIES


def test_a_non_port_failure_is_not_retried(tmp_path):
    rig = Rig(tmp_path / "rig")
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("rawlisten: no 'RAWLISTEN READY' line (rc=1); stderr tail: ImportError")

    with pytest.raises(RuntimeError, match="ImportError"):
        rig._start_fresh_ports(("raw",), broken)
    assert len(calls) == 1


def test_a_re_pick_never_hands_back_a_kept_sibling_port(tmp_path, monkeypatch):
    from .fixtures import stack
    rig = Rig(tmp_path / "rig")
    kept = rig.ports.raw                                    # allocated, not bound: the OS may offer it again
    real = stack.free_tcp_ports
    monkeypatch.setattr(stack, "free_tcp_ports", lambda n: [kept] + real(n - 1))
    rig.ports.repick("http", "tls")
    assert kept not in (rig.ports.http, rig.ports.tls) and rig.ports.raw == kept
    assert len(set(rig.ports.tcp())) == 5
