"""CH-D2 S9 session 2a: the TLS and CONNECT probes of R15-12-B9 and the TLS half of R15-21-7, against the production
spawn (`tls_rig.TlsLaunch`). Each asserts what the origin received (zero bytes where the row says) beside the client's
reply. The SOCKS rows run against Levain's own refuser on the launch's SOCKS port.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from .rig import autoanswer
from .tls_rig import KEYED, UNKEYED, read_response

pytestmark = pytest.mark.chat_egress_srt


def _origin_quiet(L, settle: float = 0.3) -> None:
    time.sleep(settle)
    assert L.origin.reqs == [] , [(r.method, r.target) for r in L.origin.reqs]


def _status(resp: bytes) -> bytes:
    return resp.split(b"\r\n", 1)[0]


# --- SOCKS: Levain's refuser, not srt's server -------------------------------------------------------------------

def test_a_socks5_greeting_and_request_get_reply_0x02(make_tls_launch) -> None:
    L = make_tls_launch()
    s = socket.create_connection(("127.0.0.1", L.socks_port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(UNKEYED)]) + UNKEYED.encode() + (443).to_bytes(2, "big"))
        got = s.recv(64)
    finally:
        s.close()
    print("RUN R15-12-B9 SOCKS5 request:", got)
    assert got[:2] == b"\x05\x02"
    _origin_quiet(L)


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_curl_socks5_hostname_is_refused_by_the_refuser_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    r = subprocess.run(["/usr/bin/curl" if shutil.which("curl") == "/usr/bin/curl" else "curl", "-sS", "--max-time", "8",
                        "--socks5-hostname", f"127.0.0.1:{L.socks_port}", f"http://{UNKEYED}:80/x"],
                       capture_output=True, text=True, env={})
    print("RUN R15-12-B9 curl --socks5-hostname:", r.returncode, r.stderr.strip())
    assert r.returncode == 97 and "(2)" in r.stderr         # curl 97: SOCKS5 reply 0x02, the refuser's answer
    _origin_quiet(L)


def test_the_http_listener_does_not_speak_socks(make_tls_launch) -> None:
    L = make_tls_launch()
    s = socket.create_connection(("127.0.0.1", L.http_port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x00")
        got = read_response(s)
    finally:
        s.close()
    print("RUN R15-12-B9 SOCKS5 to the HTTP listener:", got[:60])
    assert got.startswith(b"HTTP/1.1 400") or got == b""
    _origin_quiet(L)


@pytest.mark.xfail(strict=True, reason="OBSERVATION (S9 s2a): design C-1 says the SOCKS answer 'records a wall'; socks.py records "
                   "nothing by its docstring and the report reads 0 walls after a refused SOCKS5 request "
                   "(assert ((0 or 0) + 0) >= 1). Routes to the head: a design/implementation disagreement.")
def test_a_socks_refusal_records_a_wall(make_tls_launch) -> None:
    """The design's C-1 says the SOCKS answer 'records a wall'; this runs it and reads the report."""
    from levain.chat_egress import report
    L = make_tls_launch()
    s = socket.create_connection(("127.0.0.1", L.socks_port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x00")
        s.recv(2)
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(UNKEYED)]) + UNKEYED.encode() + (443).to_bytes(2, "big"))
        s.recv(64)
    finally:
        s.close()
    L.stop()
    r = report.build_from_files(L.e.records.rows(L.e.launch_id), L.e.events)
    print("RUN C-1 SOCKS wall in the report:", r.text())
    assert (r.walls.total or 0) + sum(r.decider_walls.values()) >= 1


# --- CONNECT corners -----------------------------------------------------------------------------------------------

def test_non_tls_bytes_after_a_connect_reach_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    s = socket.create_connection(("127.0.0.1", L.http_port), timeout=5)
    try:
        s.sendall(f"CONNECT {UNKEYED}:{L.origin_port} HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\n\r\n".encode())
        head = read_response(s)
        s.sendall(b"GET /plain-after-connect HTTP/1.1\r\nHost: x\r\n\r\n")
        after = read_response(s)
    finally:
        s.close()
    print("RUN R15-12-B9 non-TLS after CONNECT:", _status(head), after[:60])
    assert head.startswith(b"HTTP/1.1 200")
    assert not after.startswith(b"HTTP/1.1 200")
    _origin_quiet(L)


def test_a_missing_sni_is_walled_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    req = f"GET /nosni HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    resp = L.raw(req, connect_host=UNKEYED, sni=None)
    print("RUN R15-12-B9 missing SNI:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 403"), resp[:200]
    _origin_quiet(L)


def test_an_sni_other_than_the_connect_host_gets_421_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    req = f"GET /sni HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    resp = L.raw(req, connect_host=UNKEYED, sni=KEYED)
    print("RUN R15-12-B9 SNI != CONNECT host:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    _origin_quiet(L)


def test_a_host_header_other_than_the_sni_gets_421_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch(secret="HELDKEYVALUE55xx")
    req = f"GET /h HTTP/1.1\r\nHost: {KEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    resp = L.raw(req, connect_host=UNKEYED, sni=UNKEYED)
    print("RUN R15-12-B9 Host != SNI (the keyed host named under an unkeyed SNI):", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    _origin_quiet(L)


def test_an_absolute_form_target_in_tls_naming_another_authority_gets_421(make_tls_launch) -> None:
    L = make_tls_launch(secret="HELDKEYVALUE55xx")
    req = (f"GET https://{KEYED}:{L.origin_port}/abs HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\n"
           f"Connection: close\r\n\r\n").encode()
    resp = L.raw(req, connect_host=UNKEYED, sni=UNKEYED)
    print("RUN R15-12-B9 abs-form target in TLS naming another authority:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    _origin_quiet(L)


def test_control_a_well_formed_request_over_the_same_tunnel_is_served(make_tls_launch) -> None:
    """So the walls above can fail: the same rig, nothing wrong with the request, reaches the origin."""
    L = make_tls_launch()
    assert L.get("/ok").startswith(b"HTTP/1.1 200")
    assert [r.target for r in L.origin.reqs] == ["/ok"]


# --- the TLS half of R15-21-7 ---------------------------------------------------------------------------------------

def _post(L, body_wire: bytes, extra: bytes = b"") -> bytes:
    """A write to an unkeyed host files a card in gated mode: the desk answers `allowed` so the bytes reach the origin."""
    stop = autoanswer(L, "allowed")
    try:
        return _post_once(L, body_wire, extra)
    finally:
        stop.set()


def _post_once(L, body_wire: bytes, extra: bytes) -> bytes:
    req = (f"POST /up HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\nTransfer-Encoding: chunked\r\n".encode()
           + extra + b"Connection: close\r\n\r\n" + body_wire)
    return L.raw(req)


def test_tls_a_chunk_trailer_reaches_the_origin_as_no_byte(make_tls_launch) -> None:
    L = make_tls_launch()
    resp = _post(L, b"5\r\nhello\r\n0\r\nX-Trailer: TRAILERVAL9\r\n\r\n", b"Trailer: X-Trailer\r\n")
    print("RUN R15-21-7 TLS trailer:", _status(resp), [(r.method, r.body) for r in L.origin.reqs])
    posts = [r for r in L.origin.reqs if r.method == "POST"]
    assert resp.startswith(b"HTTP/1.1 200") and len(posts) == 1 and posts[0].body == b"hello", resp[:200]
    for r in posts:
        assert b"TRAILERVAL9" not in r.raw_body + r.raw_head
        assert b"trailer" not in {k.lower().encode() for k, _ in r.headers}


def test_tls_zero_padded_chunk_sizes_are_re_framed_canonically(make_tls_launch) -> None:
    L = make_tls_launch()
    resp = _post(L, b"0005\r\nhello\r\n0000\r\n\r\n")
    posts = [r for r in L.origin.reqs if r.method == "POST"]
    print("RUN R15-21-7 TLS zero-padded:", _status(resp), [r.raw_body for r in posts])
    assert resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert len(posts) == 1 and posts[0].body == b"hello"
    assert b"0005" not in posts[0].raw_body and b"0000" not in posts[0].raw_body
