"""ROUND 4 probe 7 (R15-17-11): multi-valued and mixed-spelling credential headers.

Recorded (ROUND4.md probe 7): one request carrying Authorization x4 (mixed case), Proxy-Authorization, Cookie x4 on separate
lines, X-Api-Key / x-api-key / X_Api_Key / X.Api.Key, Api-Key, X-Auth-Token x2 (16 credential values tagged DUP-*):
plain `?allow=1` -> origin head `host` + `Connection` only; TLS `?allow=1` -> `host` + `Connection` only; the KEYED host ->
`host`, **one** `authorization: Bearer SPIKE-KEY-B1` (the held key), `Connection`; `Authorization : x` (a space before the
colon) -> 400 `Invalid header token`, never reached the decider. `grep -c DUP-` over the origin log = 0."""

from __future__ import annotations

import re

import pytest

from ..fixtures.clients import Raw, has_header, status_line
from ._git import origin_reqs

pytestmark = pytest.mark.chat_egress_srt

DUP = (b"Authorization: Bearer DUP-A1\r\nAuthorization: Bearer DUP-A2\r\nauthorization: Bearer DUP-A3\r\nAUTHORIZATION: Bearer DUP-A4\r\n"
       b"Proxy-Authorization: Basic DUP-P1\r\nCookie: c1=DUP-C1\r\nCookie: c2=DUP-C2\r\ncookie: c3=DUP-C3\r\nCOOKIE: c4=DUP-C4\r\n"
       b"X-Api-Key: DUP-K1\r\nx-api-key: DUP-K2\r\nX_Api_Key: DUP-K3\r\nX.Api.Key: DUP-K4\r\nApi-Key: DUP-K5\r\n"
       b"X-Auth-Token: DUP-T1\r\nx-auth-token: DUP-T2\r\n")


def _names(entry) -> list[str]:
    head = entry["raw_head"].strip("b'\"").encode().decode("unicode_escape")
    return [ln.split(":")[0].strip().lower() for ln in head.split("\r\n")[1:] if ":" in ln]


def _case(r4, g, which: str):
    raw, p = Raw(g.port, g.ca), r4.ports
    m = r4.mark(g)
    if which == "plain":
        reply = raw.one_shot(f"GET http://s.rb.test:{p.http}/p7-plain?allow=1 HTTP/1.1\r\nHost: s.rb.test:{p.http}\r\n".encode() + DUP + b"Connection: close\r\n\r\n")
    else:
        host, path = ("s.rb.test", "/p7-tls?allow=1") if which == "tls" else ("api.rb.test", "/p7-keyed")
        t, _ = raw.tunnel(host, p.tls)
        reply = raw.exchange(t, f"GET {path} HTTP/1.1\r\nHost: {host}:{p.tls}\r\n".encode() + DUP + b"Connection: close\r\n\r\n")
    return reply, r4.since(m, g)


@pytest.mark.parametrize("which,expect_names,recorded", [
    ("plain", {"host", "connection"}, "PASS: allow + removeHeaders; origin `host`, `Connection` only"),
    ("tls", {"host", "connection"}, "PASS: origin `host`, `Connection` only"),
    ("keyed", {"host", "authorization", "connection"}, "PASS: origin `host`, ONE `authorization: Bearer SPIKE-KEY-B1` (the held key), `Connection`"),
])
def test_p7_no_client_credential_survives(r4, r4_glue, verdict, which, expect_names, recorded):
    reply, seen = _case(r4, r4_glue, which)
    reqs = origin_reqs(seen)
    assert len(reqs) == 1, (status_line(reply), seen.origin)
    names = _names(reqs[0])
    heads = reqs[0]["raw_head"]
    verdict(f"R4 P7 {which}", recorded, f"{status_line(reply)!r} origin header names={names} DUP-bytes-at-origin={heads.count('DUP-')} "
            f"authorization-lines={names.count('authorization')}")
    assert status_line(reply).startswith("HTTP/1.1 200") and "DUP-" not in heads
    assert set(names) == expect_names and names.count("authorization") == (1 if which == "keyed" else 0)
    if which == "keyed":
        assert "authorization: Bearer SPIKE-KEY-B1" in heads


def test_p7_space_before_the_colon_is_refused_before_the_decider(r4, r4_glue, verdict):
    raw, p = Raw(r4_glue.port, r4_glue.ca), r4.ports
    m = r4.mark(r4_glue)
    t, _ = raw.tunnel("s.rb.test", p.tls)
    reply = raw.exchange(t, f"GET /p7-space?allow=1 HTTP/1.1\r\nHost: s.rb.test:{p.tls}\r\nAuthorization : Bearer DUP-SP1\r\nConnection: close\r\n\r\n".encode())
    seen = r4.since(m, r4_glue)
    verdict("R4 P7 `Authorization : x`", "400 `Parse Error: Invalid header token`; never reached the decider",
            f"{status_line(reply)!r} invalid-token={b'Invalid header token' in reply} decider-asked={len(seen.req_frames())} origin={seen.origin_bytes()}B")
    assert status_line(reply) == "HTTP/1.1 400 Bad Request" and b"Invalid header token" in reply and seen.req_frames() == [] and seen.origin_bytes() == 0


def test_p7_no_dup_byte_in_the_whole_origin_log(r4, r4_glue, verdict):
    text = r4.origin_log.path.read_text()
    verdict("R4 P7 `grep -c DUP- origin.jsonl`", "0", str(text.count("DUP-")))
    assert "DUP-" not in text
