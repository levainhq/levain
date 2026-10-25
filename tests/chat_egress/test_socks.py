"""CH-D2 S8 r1 (RULINGS_R1 items 2 + 5): the SOCKS refuser's per-connection bound. The client is the sandboxed agent,
so a client that trickles bytes must not hold the single accept thread past one absolute deadline, and `close()`
must end the connection it is serving rather than wait on it.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from levain.chat_egress import socks

DEADLINE_S = 1.0                    # the test's per-connection deadline (patched over socks.IO_TIMEOUT_S)
TRICKLE_EVERY_S = 0.3               # under the deadline, so a per-recv timeout never fires
TRICKLE_FOR_S = 8.0


def _trickler(port: int, stop: threading.Event) -> tuple[socket.socket, threading.Thread]:
    """A SOCKS5 client advertising 255 methods and sending them one byte at a time."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(b"\x05\xff")

    def run() -> None:
        end = time.monotonic() + TRICKLE_FOR_S
        while not stop.is_set() and time.monotonic() < end:
            try:
                s.sendall(b"\x00")
            except OSError:
                return
            stop.wait(TRICKLE_EVERY_S)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return s, t


def _refused(port: int) -> tuple[bytes, float]:
    t0 = time.monotonic()
    s = socket.create_connection(("127.0.0.1", port), timeout=TRICKLE_FOR_S + 5)
    try:
        s.sendall(b"\x05\x01\x00\x05\x01\x00\x01\x7f\x00\x00\x01\x00\x50")
        buf = b""
        while len(buf) < 12:
            chunk = s.recv(12 - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf, time.monotonic() - t0
    finally:
        s.close()


@pytest.fixture
def refuser(monkeypatch):
    monkeypatch.setattr(socks, "IO_TIMEOUT_S", DEADLINE_S)
    r = socks.SocksRefuser()
    r.start()
    yield r
    r.close()


def test_a_trickling_client_is_cut_at_the_deadline_and_the_next_client_is_answered(refuser) -> None:
    stop = threading.Event()
    s, t = _trickler(refuser.port, stop)
    try:
        time.sleep(0.1)
        got, took = _refused(refuser.port)
    finally:
        stop.set()
        t.join(2)
        s.close()
    print(f"RUN S8C socks: next client answered {got!r} after {took:.2f}s (deadline {DEADLINE_S}s, "
          f"trickle every {TRICKLE_EVERY_S}s for up to {TRICKLE_FOR_S}s)")
    assert got == b"\x05\x00" + b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00"
    assert took < DEADLINE_S + 1.5


def test_close_ends_the_connection_it_is_serving(refuser) -> None:
    stop = threading.Event()
    s, t = _trickler(refuser.port, stop)
    try:
        time.sleep(0.2)                                        # the refuser is inside the trickler's exchange
        t0 = time.monotonic()
        clean = refuser.close(timeout_s=DEADLINE_S / 2)       # under the deadline: only close() can end it in time
        took = time.monotonic() - t0
    finally:
        stop.set()
        t.join(2)
        s.close()
    print(f"RUN S8C socks: close() during a trickle -> {clean} in {took:.2f}s")
    assert clean is True and took < DEADLINE_S / 2


def test_a_socks4_client_reads_end_of_stream_not_a_reset(refuser) -> None:
    for _ in range(50):
        s = socket.create_connection(("127.0.0.1", refuser.port), timeout=5)
        try:
            s.sendall(b"\x04\x01\x01\xbb\x7f\x00\x00\x01\x00")    # 7 bytes left unread by the refuser
            assert s.recv(16) == b""
        finally:
            s.close()


def test_a_client_that_does_not_offer_no_auth_gets_no_acceptable_method(refuser) -> None:
    s = socket.create_connection(("127.0.0.1", refuser.port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x02")                             # username/password only
        assert s.recv(2) == b"\x05\xff"
        assert s.recv(1) == b""
    finally:
        s.close()


# --- the wall channel (C-1, R15-12-B9): one byte per attempt, before the answer ---------------------------------------

def _attempt(port: int, payload: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        if payload:
            s.sendall(payload)
        s.shutdown(socket.SHUT_WR)
        buf = b""
        while chunk := s.recv(64):
            buf += chunk
        return buf
    finally:
        s.close()


def test_each_connection_that_sends_a_byte_reports_one_refusal_and_a_silent_one_reports_none() -> None:
    ours, theirs = socket.socketpair()
    r = socks.SocksRefuser(refusals=ours)
    r.start()
    try:
        _attempt(r.port, b"\x05\x01\x00\x05\x01\x00\x01\x7f\x00\x00\x01\x00\x50")      # SOCKS5 no-auth request
        _attempt(r.port, b"\x05\x01\x02")                                              # no acceptable method
        _attempt(r.port, b"\x04\x01\x00\x50\x7f\x00\x00\x01\x00")                      # SOCKS4: closed, still an attempt
        _attempt(r.port, b"")                                                          # connect and close: none
    finally:
        assert r.close()
    theirs.settimeout(2)
    got = b""
    while chunk := theirs.recv(16):
        got += chunk                                   # r.close() closed `ours`, so this ends at EOF
    theirs.close()
    assert got == b"\x01\x01\x01"


def test_the_refusal_is_reported_before_the_answer_reaches_the_client() -> None:
    ours, theirs = socket.socketpair()
    r = socks.SocksRefuser(refusals=ours)
    r.start()
    try:
        s = socket.create_connection(("127.0.0.1", r.port), timeout=5)
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        theirs.settimeout(2)
        assert theirs.recv(8) == b"\x01"                 # already there once the method answer is read
        s.close()
    finally:
        r.close()
        theirs.close()


def test_a_dead_wall_channel_does_not_end_the_refusal() -> None:
    ours, theirs = socket.socketpair()
    theirs.close()
    r = socks.SocksRefuser(refusals=ours)
    r.start()
    try:
        assert _attempt(r.port, b"\x05\x01\x00\x05\x01\x00\x01\x7f\x00\x00\x01\x00\x50")[:2] == b"\x05\x00"
    finally:
        r.close()
