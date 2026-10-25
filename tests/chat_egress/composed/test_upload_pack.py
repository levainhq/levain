"""CH-D2 S9: R15-17-2 (the cap is enforced by spooling to it in DECODED entity bytes, before the allow) and R15-21-7
(chunk trailers and zero-padded chunk sizes) on the PLAIN path, through the real glue, srt and decider.

The origin answers the discovery GET `<prefix>/info/refs?service=git-upload-pack` with the advertisement Content-Type, so
the launch's advertisement set holds the prefix and an upload-pack POST is read-shaped when its body fits UPLOAD_PACK_BODY_CAP.
The TLS half of R15-21-7 needs a TLS origin the real glue trusts (`--upstream-ca`): not in this file.
"""

from __future__ import annotations

import gzip
import socket
import time

import pytest

from levain.chat_egress.advert import UPLOAD_PACK_BODY_CAP

from .rig import autoanswer

pytestmark = pytest.mark.chat_egress_srt

ADV = "application/x-git-upload-pack-advertisement"
PREFIX = "/org/repo.git"


def _ct(seen):
    return ADV if "/info/refs" in seen.target else "text/plain"


def _prime(L) -> None:
    st, _, _ = L.request("GET", f"{PREFIX}/info/refs?service=git-upload-pack")
    assert st == 200


def _post(L, body_wire: bytes, *, extra: bytes = b"", length: int | None = None, timeout=30.0) -> bytes:
    """One upload-pack POST on a raw socket: `body_wire` is exactly what follows the head; `length` adds Content-Length."""
    P = L.origin_port
    head = (b"POST http://pub.rb.test:%d%s/git-upload-pack HTTP/1.1\r\nHost: pub.rb.test:%d\r\n"
            b"Content-Type: application/x-git-upload-pack-request\r\nAccept: application/x-git-upload-pack-result\r\n"
            % (P, PREFIX.encode(), P))
    head += (b"Content-Length: %d\r\n" % length) if length is not None else b"Transfer-Encoding: chunked\r\n"
    s = socket.create_connection(("127.0.0.1", L.port), timeout=timeout)
    out = b""
    try:
        s.sendall(head + extra + b"\r\n")
        try:
            for i in range(0, len(body_wire), 65536):
                s.sendall(body_wire[i:i + 65536])
        except OSError:
            pass                                     # the decider answered before the body ended: read what it said
        while b"\r\n\r\n" not in out:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except OSError:
        pass
    finally:
        s.close()
    return out


def _chunked(data: bytes, size: int = 16384) -> bytes:
    out = b""
    for i in range(0, len(data), size):
        c = data[i:i + size]
        out += b"%x\r\n" % len(c) + c + b"\r\n"
    return out + b"0\r\n\r\n"


def _posts(origin):
    return [r for r in origin.reqs if r.method == "POST"]


def test_a_body_under_the_cap_is_read_shaped_and_reaches_the_origin_intact(make_origin, make_launch_) -> None:
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    _prime(L)
    body = b"0032want 0123456789abcdef0123456789abcdef01234567\n" * 40
    resp = _post(L, _chunked(body))
    assert resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert L.stop() == 0
    assert L.desk.filed == [] and [p.body for p in _posts(origin)] == [body]


def test_a_chunked_body_over_the_cap_cards_and_the_origin_receives_zero_bytes(make_origin, make_launch_) -> None:
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    stop = autoanswer(L, "denied")
    try:
        _prime(L)
        resp = _post(L, _chunked(b"A" * (UPLOAD_PACK_BODY_CAP + 4096), 65536))
        assert L.stop() == 0
    finally:
        stop.set()
    print("RUN R15-17-2 chunked over cap:", resp[:40], "cards", len(L.desk.filed))
    assert len(L.desk.filed) == 1 and resp.startswith(b"HTTP/1.1 403"), resp[:200]
    assert _posts(origin) == []


def test_a_content_length_body_over_the_cap_cards_and_the_origin_receives_zero_bytes(make_origin, make_launch_) -> None:
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    stop = autoanswer(L, "denied")
    try:
        _prime(L)
        data = b"B" * (UPLOAD_PACK_BODY_CAP + 4096)
        resp = _post(L, data, length=len(data))
        assert L.stop() == 0
    finally:
        stop.set()
    assert len(L.desk.filed) == 1 and resp.startswith(b"HTTP/1.1 403"), resp[:200]
    assert _posts(origin) == []


def test_a_gzip_body_is_counted_as_srt_counts_it(make_origin, make_launch_) -> None:
    """R15-19-2: a gzip body counted compressed, as srt counts it: a 3 MiB-decoded body that is a few KB on the wire is
    under the cap, so it is read-shaped. The origin receives the compressed bytes untouched."""
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    stop = autoanswer(L, "denied")
    try:
        _prime(L)
        z = gzip.compress(b"\0" * (3 * UPLOAD_PACK_BODY_CAP // 2))
        assert len(z) < UPLOAD_PACK_BODY_CAP
        resp = _post(L, z, length=len(z), extra=b"Content-Encoding: gzip\r\n")
        assert L.stop() == 0
    finally:
        stop.set()
    print("RUN R15-19-2 gzip:", resp[:40], "cards", len(L.desk.filed), "wire", len(z))
    assert resp.startswith(b"HTTP/1.1 200") and L.desk.filed == [], (resp[:200], len(L.desk.filed))
    assert [p.body for p in _posts(origin)] == [z]


def test_a_chunk_trailer_reaches_the_origin_as_no_byte(make_origin, make_launch_) -> None:
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    _prime(L)
    resp = _post(L, b"5\r\nhello\r\n0\r\nX-Trailer: TRAILERVAL9\r\n\r\n", extra=b"Trailer: X-Trailer\r\n")
    assert L.stop() == 0
    assert resp.startswith(b"HTTP/1.1 200"), resp[:200]
    posts = _posts(origin)
    assert len(posts) == 1 and posts[0].body == b"hello"
    assert b"TRAILERVAL9" not in posts[0].raw_body + posts[0].raw_head
    assert b"trailer" not in {k.lower().encode() for k, _ in posts[0].headers}


def test_zero_padded_chunk_sizes_are_re_framed_canonically(make_origin, make_launch_) -> None:
    origin = make_origin(content_type=_ct)
    L = make_launch_(origin)
    _prime(L)
    resp = _post(L, b"0005\r\nhello\r\n0000\r\n\r\n")
    assert L.stop() == 0
    posts = _posts(origin)
    print("RUN R15-21-7 zero-padded: status", resp[:15], "origin wire body", posts[0].raw_body if posts else None)
    assert resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert len(posts) == 1 and posts[0].body == b"hello"
    assert b"0005" not in posts[0].raw_body and b"0000" not in posts[0].raw_body


def test_control_the_origin_rig_records_trailers_and_padded_sizes_when_they_arrive(make_origin) -> None:
    """So the two reds above can fail: sent straight to the origin, the same bytes show up in `raw_body`."""
    origin = make_origin()
    s = socket.create_connection(("127.0.0.1", origin.port), timeout=5)
    s.sendall(b"POST /x HTTP/1.1\r\nHost: o\r\nTransfer-Encoding: chunked\r\n\r\n0005\r\nhello\r\n0\r\nX-Trailer: T9\r\n\r\n")
    s.recv(4096)
    s.close()
    deadline = time.monotonic() + 5
    while not origin.reqs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert origin.reqs[0].body == b"hello" and b"T9" in origin.reqs[0].raw_body and b"0005" in origin.reqs[0].raw_body
