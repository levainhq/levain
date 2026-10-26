"""CH-D2 S9 session 2a: the TLS and CONNECT probes of R15-12-B9 and the TLS half of R15-21-7, against the production
spawn (`tls_rig.TlsLaunch`). Each "reaches nothing" probe asserts the client's reply beside `RecordingOrigin.assert_untouched`
(zero accepted connections and zero bytes at the origin, counted before any TLS wrap or parse). The oracle is shown able to
fail: every such probe also runs against `tls_rig.LeakyTls` (an open relay to the origin) in `test_the_oracle_is_red_...`.
The SOCKS rows run against Levain's own refuser on the launch's SOCKS port.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from .rig import autoanswer
from .tls_rig import KEYED, UNKEYED, ConnectRefused, read_response

pytestmark = pytest.mark.chat_egress_srt


def _status(resp: bytes) -> bytes:
    return resp.split(b"\r\n", 1)[0]


def _read_n(s, n: int, timeout: float = 8.0) -> bytes:
    """Reads until `n` bytes arrived, the peer closed or `timeout` passed; a short answer is returned short."""
    end = time.monotonic() + timeout
    out = b""
    try:
        while len(out) < n and time.monotonic() < end:
            d = s.recv(n - len(out))
            if not d:
                break
            out += d
    except OSError:
        pass
    return out


def _socks_request(L) -> bytes:
    s = socket.create_connection(("127.0.0.1", L.socks_port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x00")
        assert _read_n(s, 2) == b"\x05\x00"
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(UNKEYED)]) + UNKEYED.encode() + (443).to_bytes(2, "big"))
        return _read_n(s, 10)                            # VER REP RSV ATYP + IPv4 + port: the refuser's whole reply
    finally:
        s.close()


# --- the probes: each drives a launch and returns what the client saw; the oracle is asserted by the caller ---------

def probe_socks5(L):
    return _socks_request(L)


def probe_curl_socks5(L):
    return subprocess.run([shutil.which("curl"), "-sS", "--max-time", "8", "--socks5-hostname",
                           f"127.0.0.1:{L.socks_port}", f"http://{UNKEYED}:80/x"],
                          capture_output=True, text=True, env={})


def probe_socks_to_http_listener(L):
    s = socket.create_connection(("127.0.0.1", L.http_port), timeout=5)
    try:
        s.sendall(b"\x05\x01\x00")
        return read_response(s)
    finally:
        s.close()


def probe_plain_after_connect(L):
    s = socket.create_connection(("127.0.0.1", L.http_port), timeout=5)
    try:
        s.sendall(f"CONNECT {UNKEYED}:{L.origin_port} HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\n\r\n".encode())
        head = read_response(s)
        s.sendall(b"GET /plain-after-connect HTTP/1.1\r\nHost: x\r\n\r\n")
        return head, read_response(s)
    finally:
        s.close()


def probe_missing_sni(L):
    req = f"GET /nosni HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    return L.raw(req, connect_host=UNKEYED, sni=None)


def probe_sni_not_the_connect_host(L):
    req = f"GET /sni HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    return L.raw(req, connect_host=UNKEYED, sni=KEYED)


def probe_host_not_the_sni(L):
    req = f"GET /h HTTP/1.1\r\nHost: {KEYED}:{L.origin_port}\r\nConnection: close\r\n\r\n".encode()
    return L.raw(req, connect_host=UNKEYED, sni=UNKEYED)


def probe_absolute_form_other_authority(L):
    req = (f"GET https://{KEYED}:{L.origin_port}/abs HTTP/1.1\r\nHost: {UNKEYED}:{L.origin_port}\r\n"
           f"Connection: close\r\n\r\n").encode()
    return L.raw(req, connect_host=UNKEYED, sni=UNKEYED)


# --- SOCKS: Levain's refuser, not srt's server -------------------------------------------------------------------

def test_a_socks5_greeting_and_request_get_reply_0x02(make_tls_launch) -> None:
    L = make_tls_launch()
    got = probe_socks5(L)
    print("RUN R15-12-B9 SOCKS5 request:", got)
    assert got[:2] == b"\x05\x02" and len(got) == 10, got
    L.origin.assert_untouched()


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_curl_socks5_hostname_is_refused_by_the_refuser_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    r = probe_curl_socks5(L)
    print("RUN R15-12-B9 curl --socks5-hostname:", r.returncode, r.stderr.strip())
    assert r.returncode == 97                            # curl 97: the proxy handshake failed (the refuser's reply 0x02)
    L.origin.assert_untouched()


def test_the_http_listener_does_not_speak_socks(make_tls_launch) -> None:
    L = make_tls_launch()
    got = probe_socks_to_http_listener(L)
    print("RUN R15-12-B9 SOCKS5 to the HTTP listener:", got[:60])
    assert got.startswith(b"HTTP/1.1 400") or got == b""
    L.origin.assert_untouched()


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
    deadline = time.monotonic() + 5.0                    # the wall appears in the report, bounded; not a stop-order wait
    while True:
        r = report.build_from_files(L.e.records.rows(L.e.launch_id), L.e.events)
        if "at least 1" in r.walls.text or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert "at least 1" in r.walls.text, r.walls.text          # the wall is in the report before any stop
    L.stop()
    r = report.build_from_files(L.e.records.rows(L.e.launch_id), L.e.events)
    print("RUN C-1 SOCKS wall in the report:", r.text())
    assert r.walls.total == 1 and r.by_reason == {"socks": 1}


# --- CONNECT corners -----------------------------------------------------------------------------------------------

def test_non_tls_bytes_after_a_connect_reach_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    head, after = probe_plain_after_connect(L)
    print("RUN R15-12-B9 non-TLS after CONNECT:", _status(head), after[:60])
    assert head.startswith(b"HTTP/1.1 200")
    assert not after.startswith(b"HTTP/1.1 200")
    L.origin.assert_untouched()


def test_a_missing_sni_is_walled_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    resp = probe_missing_sni(L)
    print("RUN R15-12-B9 missing SNI:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 403"), resp[:200]
    L.origin.assert_untouched()


def test_an_sni_other_than_the_connect_host_gets_421_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    resp = probe_sni_not_the_connect_host(L)
    print("RUN R15-12-B9 SNI != CONNECT host:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    L.origin.assert_untouched()


def test_a_host_header_other_than_the_sni_gets_421_and_reaches_nothing(make_tls_launch) -> None:
    L = make_tls_launch(secret="HELDKEYVALUE55xx")
    resp = probe_host_not_the_sni(L)
    print("RUN R15-12-B9 Host != SNI (the keyed host named under an unkeyed SNI):", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    L.origin.assert_untouched()


def test_an_absolute_form_target_in_tls_naming_another_authority_gets_421(make_tls_launch) -> None:
    L = make_tls_launch(secret="HELDKEYVALUE55xx")
    resp = probe_absolute_form_other_authority(L)
    print("RUN R15-12-B9 abs-form target in TLS naming another authority:", _status(resp))
    assert resp.startswith(b"HTTP/1.1 421"), resp[:200]
    L.origin.assert_untouched()


def test_control_a_well_formed_request_over_the_same_tunnel_is_served(make_tls_launch) -> None:
    """So the walls above can fail: the same rig, nothing wrong with the request, reaches the origin."""
    L = make_tls_launch()
    assert L.get("/ok").startswith(b"HTTP/1.1 200")
    assert [r.target for r in L.origin.reqs] == ["/ok"]
    with pytest.raises(AssertionError):
        L.origin.assert_untouched(settle=0)


# --- the oracle can fail: every "reaches nothing" probe, against a deliberately leaky stand-in -------------------------

PROBES = {
    "socks5": probe_socks5,
    "curl_socks5": probe_curl_socks5,
    "socks_to_http_listener": probe_socks_to_http_listener,
    "plain_after_connect": probe_plain_after_connect,
    "missing_sni": probe_missing_sni,
    "sni_not_the_connect_host": probe_sni_not_the_connect_host,
    "host_not_the_sni": probe_host_not_the_sni,
    "absolute_form_other_authority": probe_absolute_form_other_authority,
}


@pytest.mark.parametrize("name", sorted(PROBES))
def test_the_oracle_is_red_when_the_proxy_leaks(make_leaky_tls, name) -> None:
    """The probe runs unchanged against an open relay to the origin; its client-side result is whatever it is, and the
    origin oracle must fail, or the green runs above prove nothing."""
    if name == "curl_socks5" and shutil.which("curl") is None:
        pytest.skip("needs curl")
    L = make_leaky_tls()
    try:
        PROBES[name](L)
    except (OSError, AssertionError, ConnectRefused):    # the leak may break the probe's own client side; the oracle is the point
        pass
    with pytest.raises(AssertionError) as red:
        L.origin.assert_untouched()
    print(f"RUN RED CONTROL {name}:", str(red.value)[:200])


def test_the_byte_oracle_sees_plaintext_forwarded_into_the_tls_origin(make_leaky_tls) -> None:
    """The parsed-request recorder alone passes here: the origin's TLS wrap fails first, so no request is ever parsed."""
    L = make_leaky_tls()
    s = socket.create_connection(("127.0.0.1", L.origin_port), timeout=5)
    try:
        s.sendall(b"GET /plaintext-into-tls HTTP/1.1\r\nHost: x\r\n\r\n")
        read_response(s)
    finally:
        s.close()
    time.sleep(0.3)
    t = L.origin.touched()
    print("RUN plaintext into the TLS origin:", t)
    assert t["reqs"] == [] and t["conns"] == 1 and t["bytes"] > 0 and t["wrap_failures"]
    with pytest.raises(AssertionError):
        L.origin.assert_untouched(settle=0)


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
