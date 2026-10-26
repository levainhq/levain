"""CH-D2 S9 session 2b: R15-10-A13's last two reds, complement's *"`Expect: 100-continue` and pipelining (finding 7)."*,
on the real glue and decider (the plain path, `test_decider_glue.Launch`). Round 2 measured `100-continue` and held-card
concurrency; pipelining behind a held request was never run, so this is its first run.

The safety property for both: nothing of a carded write reaches the origin before his answer, and a held card stalls
no other connection of the launch (complement 7: *"one pending card stalls every other request for the launch."* must
not hold). What the client sees meanwhile (an interim `100 Continue`, the pipelined GET's fate) is printed as measured.
"""

from __future__ import annotations

import re
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


def check_pipelined(answer: str, *, early: dict, final: dict, lines: list) -> None:
    """The A13 pipelining read, one place so the control below can hand it a broken proxy's facts. `early` and `final`
    are `RecordingOrigin.snapshot()` reads (requests and unaccounted bytes from one moment each).

    Held: in `early`, before his answer, no byte of the POST is at the origin (not a parsed request, and no stray
    partial head or body either), whichever way he is about to answer. After: in `final`, the POST is at the origin
    only if he allowed it, with every byte accounted for, and the pipelined `/after` reached the origin exactly once
    whichever way he answered. The client side is today's behaviour: denied -> a 403 then a 200; allowed -> two 200s.
    Both in order (the POST's answer first). The ordering edge stays a known edge: real clients do not pipeline."""
    assert ("POST", "/w") not in early["reqs"] and early["unaccounted"] == 0, early
    assert final["unaccounted"] == 0, final
    assert (("POST", "/w") in final["reqs"]) == (answer == "allowed"), final["reqs"]
    assert final["reqs"].count(("GET", "/after")) == 1, final["reqs"]
    assert lines == [b"HTTP/1.1 200 OK" if answer == "allowed" else b"HTTP/1.1 403 Forbidden", b"HTTP/1.1 200 OK"], lines


@pytest.mark.parametrize("answer", ["denied", "allowed"])
def test_a_get_pipelined_behind_a_held_post(make_origin, make_launch_, answer) -> None:
    """First run (round 2 never ran it): POST (carded) and GET in one write on one connection. Before his answer the
    origin has nothing of the POST and another connection is served; after it, the POST reaches the origin only if he
    allowed it. The pipelined GET is a silent read and is forwarded without waiting on the POST's verdict."""
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
        other = L.request("GET", "/other", timeout=10)
        early = origin.snapshot()                        # one read, after every other request, before his answer
        L.desk.answer["card-1"] = answer
        reply = _read_for(s, 8.0)
    finally:
        s.close()
    time.sleep(0.3)
    final = origin.snapshot()
    lines = re.findall(rb"HTTP/1\.1 \d{3} [A-Za-z ]+?(?=\r\n)", reply)
    print(f"RUN R15-10-A13 pipelining ({answer}): filed {filed} early {early} other {other[0]} {other[2]:.2f}s; "
          f"status lines {lines}; origin {final['reqs']}")
    assert filed and other[0] == 200 and other[2] < 5.0
    check_pipelined(answer, early=early, final=final, lines=lines)
    assert L.stop() == 0


def test_the_pipelining_read_is_red_for_a_proxy_that_drops_the_get_or_leaks_part_of_the_post(make_origin) -> None:
    """The control for the read above: each broken proxy's facts handed to `check_pipelined`, in BOTH answers (one that
    silently drops `/after`, one whose partial POST head reached the origin as bytes only, one that forwarded the POST
    before his answer), and the snapshot itself seeing a partial head sent straight at an origin."""
    def facts(answer, *, early_reqs=(), early_extra=0, final_extra=0, drop_after=False):
        post = [("POST", "/w")] if answer == "allowed" else []
        reqs = [("GET", "/other")] + post + ([] if drop_after else [("GET", "/after")])
        ok = b"HTTP/1.1 200 OK" if answer == "allowed" else b"HTTP/1.1 403 Forbidden"
        return dict(early={"reqs": [("GET", "/other"), *early_reqs], "unaccounted": early_extra},
                    final={"reqs": reqs, "unaccounted": final_extra},
                    lines=[ok] if drop_after else [ok, b"HTTP/1.1 200 OK"])
    for answer in ("denied", "allowed"):
        check_pipelined(answer, **facts(answer))
        for broken in (facts(answer, drop_after=True), facts(answer, early_extra=20), facts(answer, final_extra=20),
                       facts(answer, early_reqs=[("POST", "/w")])):
            with pytest.raises(AssertionError):
                check_pipelined(answer, **broken)
    origin = make_origin()
    s = socket.create_connection(("127.0.0.1", origin.port), timeout=5)
    try:
        s.sendall(b"POST /w HTTP/1.1\r\nHost: x\r\nContent-Le")
        assert _wait(lambda: origin.snapshot()["unaccounted"] > 0, 5.0), origin.snapshot()
    finally:
        s.close()


def test_one_snapshot_sees_a_post_that_arrives_between_two_separate_reads(make_origin) -> None:
    """Why `early` is one snapshot: the earlier read took the request list and the unaccounted byte count at two
    different moments. A POST forwarded between them (here sent by hand at the origin) is absent from the first read and
    fully accounted for by the second, so neither shows it; the one snapshot taken after it does."""
    origin = make_origin()
    reqs_then = origin.touched()["reqs"]
    s = socket.create_connection(("127.0.0.1", origin.port), timeout=5)
    try:
        s.sendall(b"POST /w HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
        assert _wait(lambda: ("POST", "/w") in origin.snapshot()["reqs"], 5.0), origin.snapshot()
        snap = origin.snapshot()
        assert ("POST", "/w") not in reqs_then and snap["unaccounted"] == 0       # the split read: both clean
        with pytest.raises(AssertionError):
            check_pipelined("allowed", early=snap, final=snap, lines=[b"HTTP/1.1 200 OK", b"HTTP/1.1 200 OK"])
    finally:
        s.close()


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
        assert _wait(lambda: ("GET", "/after") in origin.snapshot()["reqs"], 5.0), origin.snapshot()
        snap = origin.snapshot()        # no answer was ever given: the held-until-answer read, in the allowed parameterization
        for answer in ("denied", "allowed"):
            with pytest.raises(AssertionError):
                check_pipelined(answer, early=snap, final=snap,
                                lines=[b"HTTP/1.1 200 OK" if answer == "allowed" else b"HTTP/1.1 403 Forbidden",
                                       b"HTTP/1.1 200 OK"])
    finally:
        s.close()
