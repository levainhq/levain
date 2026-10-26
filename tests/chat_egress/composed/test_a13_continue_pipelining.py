"""CH-D2 S9 session 2b: R15-10-A13's last two reds, complement's *"`Expect: 100-continue` and pipelining (finding 7)."*,
on the real glue and decider (the plain path, `test_decider_glue.Launch`). Round 2 measured `100-continue` and held-card
concurrency; pipelining behind a held request was never run, so this is its first run.

The safety property for both: nothing of a carded write reaches the origin before his answer, and a held card stalls
no other connection of the launch (complement 7: *"one pending card stalls every other request for the launch."* must
not hold). What the client sees meanwhile (an interim `100 Continue`, the pipelined GET's fate) is printed as measured.
"""

from __future__ import annotations

import socket
import time

import pytest

pytestmark = pytest.mark.chat_egress_srt


def _wait(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _read_for(s: socket.socket, seconds: float) -> bytes:
    """Everything the socket delivers in `seconds` (or until the peer closes)."""
    end = time.monotonic() + seconds
    out = b""
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return out
        s.settimeout(left)
        try:
            d = s.recv(65536)
        except (socket.timeout, TimeoutError):
            return out
        except OSError:
            return out
        if not d:
            return out
        out += d


def test_expect_100_continue_sends_no_byte_upstream_before_his_allow(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    P = L.origin_port
    s = socket.create_connection(("127.0.0.1", L.port), timeout=10)
    try:
        s.sendall(f"POST http://pub.rb.test:{P}/c HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nContent-Type: text/plain\r\n"
                  "Content-Length: 5\r\nExpect: 100-continue\r\n\r\n".encode())
        interim = _read_for(s, 1.5)                      # what a client waiting on 100 sees before sending its body
        s.sendall(b"hello")
        filed = _wait(lambda: len(L.desk.filed) == 1)
        early = origin.touched()                         # the card is pending: nothing may have reached the origin
        held_get = L.request("GET", "/other", timeout=10)   # another connection, while the card is held
        L.desk.answer["card-1"] = "allowed"
        reply = interim + _read_for(s, 8.0)
    finally:
        s.close()
    print("RUN R15-10-A13 100-continue: interim", interim[:40], "filed", filed, "early", early, "other GET",
          held_get[0], f"{held_get[2]:.2f}s", "reply", reply[:80])
    assert filed, L.desk.filed
    assert early["conns"] == 0 and early["bytes"] == 0 and not early["reqs"], early
    assert held_get[0] == 200 and held_get[2] < 5.0      # the held card stalls no other connection
    assert [(r.method, r.target, r.body) for r in origin.reqs if r.method == "POST"] == [("POST", "/c", b"hello")]
    assert b"HTTP/1.1 200" in reply
    assert L.stop() == 0


def test_expect_100_continue_denied_sends_no_byte_upstream(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    P = L.origin_port
    s = socket.create_connection(("127.0.0.1", L.port), timeout=10)
    try:
        s.sendall(f"POST http://pub.rb.test:{P}/c HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nContent-Type: text/plain\r\n"
                  "Content-Length: 5\r\nExpect: 100-continue\r\n\r\n".encode())
        _read_for(s, 1.0)
        s.sendall(b"hello")
        assert _wait(lambda: len(L.desk.filed) == 1), L.desk.filed
        L.desk.answer["card-1"] = "denied"
        reply = _read_for(s, 6.0)
    finally:
        s.close()
    print("RUN R15-10-A13 100-continue denied:", reply[:80])
    assert b"HTTP/1.1 403" in reply, reply[:300]
    assert L.stop() == 0
    origin.assert_untouched()


@pytest.mark.parametrize("answer", ["denied", "allowed"])
def test_a_get_pipelined_behind_a_held_post(make_origin, make_launch_, answer) -> None:
    """First run (round 2 never ran it): POST (carded) and GET in one write on one connection. Before his answer the
    origin has nothing of the POST and another connection is served; after it, the POST reaches the origin only if he
    allowed it. The pipelined GET's fate (when it reaches the origin, which responses the client gets) is printed."""
    origin = make_origin()
    L = make_launch_(origin)
    P = L.origin_port
    s = socket.create_connection(("127.0.0.1", L.port), timeout=10)
    try:
        s.sendall(f"POST http://pub.rb.test:{P}/w HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nContent-Type: text/plain\r\n"
                  f"Content-Length: 5\r\n\r\nhello"
                  f"GET http://pub.rb.test:{P}/after HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nConnection: close\r\n\r\n"
                  .encode())
        filed = _wait(lambda: len(L.desk.filed) == 1)
        time.sleep(0.5)
        early = origin.touched()
        other = L.request("GET", "/other", timeout=10)
        L.desk.answer["card-1"] = answer
        reply = _read_for(s, 8.0)
    finally:
        s.close()
    targets = [(r.method, r.target) for r in origin.reqs]
    lines = [ln for ln in reply.split(b"\r\n") if ln.startswith(b"HTTP/1.1")]
    print(f"RUN R15-10-A13 pipelining ({answer}): filed {filed} early {early} other {other[0]} {other[2]:.2f}s; "
          f"status lines {lines}; "
          f"origin {targets}")
    # the carded POST is not at the origin before his answer (/other is asked for after this read); the pipelined GET
    # is a silent read and is NOT held behind it (first run, 10-25: forwarded before the POST's verdict)
    assert filed and ("POST", "/w") not in early["reqs"], early
    assert other[0] == 200 and other[2] < 5.0
    assert (("POST", "/w") in targets) == (answer == "allowed"), targets
    assert reply.startswith(b"HTTP/1.1 " + (b"200" if answer == "allowed" else b"403")), reply[:200]
    assert L.stop() == 0


def test_the_oracle_is_red_when_the_proxy_forwards_the_post_unheld(make_origin, make_leaky_plain) -> None:
    """The negative control: the same pipelined bytes through an open relay (`LeakyPlain`) put the POST at the origin
    at once, which the "not before his answer" read above would catch."""
    origin = make_origin()
    L = make_leaky_plain(origin)
    P = L.origin_port
    s = socket.create_connection(("127.0.0.1", L.port), timeout=10)
    try:
        s.sendall(f"POST /w HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nContent-Length: 5\r\n\r\nhello"
                  f"GET /after HTTP/1.1\r\nHost: pub.rb.test:{P}\r\nConnection: close\r\n\r\n".encode())
        assert _wait(lambda: ("POST", "/w") in origin.touched()["reqs"], 5.0), origin.touched()
    finally:
        s.close()
