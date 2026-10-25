"""ROUND 3 B9 (the paths srt delegates: SOCKS, smuggling shapes, SNI / Host / request-target agreement).

Recorded (ROUND3.md B9, every row PASS, patched B2 column; each: no origin byte, no DNS lookup): SOCKS5/SOCKS4a speak to
nothing (curl 97); raw SOCKS5 greeting -> 400 bad_request; non-TLS after CONNECT -> empty reply; missing SNI -> 403 decider
`wall: http on port`; SNI != CONNECT host / Host != SNI / abs-form target in TLS / Host != abs-form plain -> 421; CL+TE,
duplicate CL, obs-fold -> 400 before the decider; `Transfer-Encoding: xchunked` -> the decider IS asked (allow) then 400
(plain) / empty reply (TLS), zero bytes upstream. Leaf-mint failure pass-through was not probed in round 3 either."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ..fixtures.clients import Raw, has_header, status_line
from ._util import facts

pytestmark = pytest.mark.chat_egress_srt

SMUGGLE = b"0\r\n\r\nGET /SMUGGLED HTTP/1.1\r\nHost: s.rb.test\r\n\r\n"


def _smuggle_req(kind: str, hostport: bytes, target: bytes) -> bytes:
    """The round-3 BODIES, with the request target spelled by the caller (`/x?allow=1` in TLS, absolute-form on plain)."""
    t = target
    if kind == "clte":
        return (b"POST " + t + b" HTTP/1.1\r\nHost: " + hostport + b"\r\nContent-Length: " + str(len(SMUGGLE)).encode()
                + b"\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n" + SMUGGLE)
    if kind == "dupcl":
        return (b"POST " + t + b" HTTP/1.1\r\nHost: " + hostport + b"\r\nContent-Length: 5\r\nContent-Length: 50\r\n"
                b"Connection: close\r\n\r\nhello" + b"GET /SMUGGLED HTTP/1.1\r\nHost: s\r\n\r\n")
    if kind == "xchunked":
        return (b"POST " + t + b" HTTP/1.1\r\nHost: " + hostport + b"\r\nTransfer-Encoding: xchunked\r\n"
                b"Connection: close\r\n\r\n5\r\nhello\r\n" + SMUGGLE)
    if kind == "obsfold":
        return (b"GET " + t + b" HTTP/1.1\r\nHost: " + hostport + b"\r\nX-Note: first\r\n FOLDED-CONT\r\n"
                b"X-Api-Key: a\r\n LEAKFOLD-SECRET\r\nConnection: close\r\n\r\n")
    raise KeyError(kind)


def _zero_upstream(seen):
    assert seen.origin_bytes() == 0 and seen.dns == [], (seen.origin, seen.dns)


@pytest.mark.parametrize("pid,flag,note", [("R3 B9 socks5", "--socks5-hostname", "invalid version in initial SOCKS5 response"),
                                           ("R3 B9 socks4a", "--socks4a", "SOCKS4 reply has wrong version")])
def test_b9_socks_speaks_to_nothing(r3, r3_glue, verdict, pid, flag, note):
    m = r3.mark(r3_glue)
    res = clients.curl(0, r3_glue.ca, f"http://s.rb.test:{r3.ports.http}/socks", "--socks5-hostname" if flag == "--socks5-hostname" else flag,
                       f"127.0.0.1:{r3_glue.port}", max_time=5, proxy=False, include=False)
    seen = r3.since(m, r3_glue)
    verdict(pid, f"PASS: curl 97 \"{note}\"; no frame, no DNS, origin 0", f"curl rc={res.rc} {res.err.strip()!r} | {facts(seen)}")
    assert res.rc == 97 and seen.req_frames() == []
    _zero_upstream(seen)


def test_b9_socks5_greeting_is_answered_by_the_http_parser(r3, r3_glue, verdict):
    m = r3.mark(r3_glue)
    reply = Raw(r3_glue.port, r3_glue.ca).one_shot(b"\x05\x01\x00")
    seen = r3.since(m, r3_glue)
    verdict("R3 B9 SOCKS5 raw", "PASS: `HTTP/1.1 400 Bad Request` X-Deny-Reason bad_request (the HTTP parser answers; no 0x02 refusal yet)",
            f"{status_line(reply)!r} bad_request={has_header(reply, 'X-Deny-Reason', 'bad_request')} | {facts(seen)}")
    assert status_line(reply) == "HTTP/1.1 400 Bad Request" and has_header(reply, "X-Deny-Reason", "bad_request")
    _zero_upstream(seen)


def test_b9_non_tls_after_connect_443(r3, r3_glue, verdict):
    raw = Raw(r3_glue.port, r3_glue.ca)
    m = r3.mark(r3_glue)
    s, head = raw.connect("s.rb.test", r3.ports.tls)
    reply = raw.exchange(s, f"GET / HTTP/1.1\r\nHost: s.rb.test:{r3.ports.tls}\r\nConnection: close\r\n\r\n".encode())
    s.close()
    seen = r3.since(m, r3_glue)
    verdict("R3 B9 non-TLS after CONNECT 443", "PASS: CONNECT 200 then closed, empty reply; no frame, no DNS",
            f"{status_line(head)!r} reply={reply!r} | {facts(seen)}")
    assert status_line(head) == "HTTP/1.1 200 Connection Established" and reply == b"" and seen.req_frames() == []
    _zero_upstream(seen)


def test_b9_missing_sni_fails_closed(r3, r3_glue, verdict):
    """Round 3 recorded the DECIDER's wall (`wall: http on port N`, a frame, X-Deny-Reason: decider). The round-4 glue
    (R4-b, the scheme check) catches the same request one layer earlier: the TLS-terminated request carries an https
    target but no SNI, so the glue walls it before the decider is asked (X-Deny-Reason: denied, no frame). The recorded
    VERDICT (403, fail-closed, zero bytes upstream) is the same; the layer that answers moved. Either layer passes here;
    the verdict line says which one answered."""
    raw, p = Raw(r3_glue.port, r3_glue.ca), r3.ports
    m = r3.mark(r3_glue)
    t, head = raw.tunnel("s.rb.test", p.tls, sni=None)
    reply = raw.exchange(t, f"GET /nosni HTTP/1.1\r\nHost: s.rb.test:{p.tls}\r\nConnection: close\r\n\r\n".encode())
    seen = r3.since(m, r3_glue)
    by_decider = has_header(reply, "X-Deny-Reason", "decider")
    layer = "decider wall" if by_decider else "glue scheme-check wall (R4-b), decider not asked"
    verdict("R3 B9 missing SNI", "PASS (B2): 403 X-Deny-Reason decider `wall: http on port 9443` (fail-closed)",
            f"{status_line(reply)!r} answered by {layer}; body={reply.split(b'\r\n\r\n', 1)[-1][:60]!r} | {facts(seen)}")
    assert status_line(reply) == "HTTP/1.1 403 Forbidden" and b"wall:" in reply
    if by_decider:
        assert f"wall: http on port {p.tls}".encode() in reply and [v["action"] for v in seen.verdicts()] == ["deny"]
    else:
        assert has_header(reply, "X-Deny-Reason", "denied") and seen.req_frames() == [] and seen.event_types("scheme-deny")
    _zero_upstream(seen)


@pytest.mark.parametrize("pid,connect_host,sni,req_fmt,msg", [
    ("R3 B9 SNI != CONNECT host", "a.rb.test", "b.rb.test", "GET /sni-ne HTTP/1.1\r\nHost: a.rb.test:{p}\r\nConnection: close\r\n\r\n",
     b"TLS server name does not match the request target"),
    ("R3 B9 Host != SNI", "a.rb.test", "a.rb.test", "GET /host-ne HTTP/1.1\r\nHost: b.rb.test:{p}\r\nConnection: close\r\n\r\n",
     b"Host header does not match the request target"),
    ("R3 B9 abs-form target in TLS", "a.rb.test", "a.rb.test",
     "GET https://b.rb.test:{p}/abs HTTP/1.1\r\nHost: a.rb.test:{p}\r\nConnection: close\r\n\r\n",
     b"request-target authority does not match the request target"),
])
def test_b9_tls_disagreement_is_421(r3, r3_glue, verdict, pid, connect_host, sni, req_fmt, msg):
    raw, p = Raw(r3_glue.port, r3_glue.ca), r3.ports
    m = r3.mark(r3_glue)
    s, _ = raw.connect(connect_host, p.tls)
    t = raw.tls(s, sni)
    reply = raw.exchange(t, req_fmt.format(p=p.tls).encode())
    seen = r3.since(m, r3_glue)
    verdict(pid, f"PASS: 421 misdirected `{msg.decode()}`", f"{status_line(reply)!r} misdirected={has_header(reply, 'X-Deny-Reason', 'misdirected')} | {facts(seen)}")
    assert status_line(reply) == "HTTP/1.1 421 Misdirected Request" and has_header(reply, "X-Deny-Reason", "misdirected") and msg in reply
    _zero_upstream(seen)


def test_b9_host_ne_absolute_form_plain(r3, r3_glue, verdict):
    p = r3.ports
    m = r3.mark(r3_glue)
    reply = Raw(r3_glue.port, r3_glue.ca).one_shot(
        f"GET http://a.rb.test:{p.http}/h HTTP/1.1\r\nHost: b.rb.test:{p.http}\r\nConnection: close\r\n\r\n".encode())
    seen = r3.since(m, r3_glue)
    verdict("R3 B9 Host != abs-form, plain", "PASS: 421 `Host header does not match`", f"{status_line(reply)!r} | {facts(seen)}")
    assert status_line(reply) == "HTTP/1.1 421 Misdirected Request" and b"Host header does not match the request target" in reply
    _zero_upstream(seen)


@pytest.mark.parametrize("kind,decider_asked_plain,tls_reply", [
    ("clte", False, b"Transfer-Encoding can't be present with Content-Length"),
    ("dupcl", False, b"Duplicate Content-Length"),
    ("obsfold", False, b"Unexpected whitespace after header value"),
    ("xchunked", True, None),
])
@pytest.mark.parametrize("path", ["plain", "tls"])
def test_b9_smuggling_shapes_send_zero_bytes_upstream(r3, r3_glue, verdict, kind, decider_asked_plain, tls_reply, path):
    raw, p = Raw(r3_glue.port, r3_glue.ca), r3.ports
    m = r3.mark(r3_glue)
    if path == "plain":
        hp = f"s.rb.test:{p.http}".encode()
        reply = raw.one_shot(_smuggle_req(kind, hp, b"http://" + hp + b"/x?allow=1"))
    else:
        hp = f"s.rb.test:{p.tls}".encode()
        t, _ = raw.tunnel("s.rb.test", p.tls, sni="s.rb.test")
        reply = raw.exchange(t, _smuggle_req(kind, hp, b"/x?allow=1"))
    seen = r3.since(m, r3_glue)
    asked = len(seen.req_frames())
    recorded = ("PASS: 400 bad_request before the decider" if kind != "xchunked" else
                "PASS (zero bytes): the decider WAS asked (allow); then 400 (plain) / empty reply (TLS)")
    verdict(f"R3 B9 {path} {kind}", recorded + "; no DNS, origin 0", f"{status_line(reply)!r} decider-asked={asked} | {facts(seen)}")
    if kind == "xchunked":
        assert asked == 1 and [v["action"] for v in seen.verdicts()] == ["allow"]
        assert (status_line(reply) == "HTTP/1.1 400 Bad Request") if path == "plain" else reply == b""
    else:
        assert asked == 0 and status_line(reply) == "HTTP/1.1 400 Bad Request" and has_header(reply, "X-Deny-Reason", "bad_request")
        if path == "tls":
            assert tls_reply in reply
    _zero_upstream(seen)
    assert b"SMUGGLED" not in b"".join(d["raw"].encode() for d in seen.origin) and b"LEAKFOLD" not in b"".join(d["raw"].encode() for d in seen.origin)
