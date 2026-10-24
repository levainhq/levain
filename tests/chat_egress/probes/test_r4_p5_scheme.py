"""ROUND 4 probe 5 (R15-17-5): an absolute-form non-http target on the plain path.

Recorded (ROUND4.md probe 5): BEFORE (round-3 glue: run F/G, scheme check off): `GET https://s.rb.test:9080/` on the plain
path -> frame WITHOUT sni, the decider reads it as http on an HTTP port, ALLOWS; srt resolved and dialed 9080 (client timed
out); to a raw byte listener on 9081 (in the HTTP set) the listener got a 0-byte vetting conn then **1,584 B starting
`16 03 01`, a TLS ClientHello**. AFTER (R4-b, runs E/H): `403 wall: https target on the plain path`, decider not asked, no
DNS, origin 0, **no connection** at the listener. `ws://` and `ftp://` targets: srt refuses them itself, 400 bad_request
\"destination host is not plain ASCII\", no frame."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ..fixtures.clients import Raw, has_header, status_line
from ._util import facts

pytestmark = pytest.mark.chat_egress_srt


def _abs_get(scheme: str, port: int) -> bytes:
    return f"GET {scheme}://s.rb.test:{port}/p5-{scheme} HTTP/1.1\r\nHost: s.rb.test:{port}\r\nConnection: close\r\n\r\n".encode()


def test_p5_after_https_target_on_the_plain_path_is_walled_before_the_decider(r4, r4_glue, verdict):
    m = r4.mark(r4_glue)
    reply = Raw(r4_glue.port, r4_glue.ca).one_shot(_abs_get("https", r4.ports.http))
    seen = r4.since(m, r4_glue)
    verdict("R4 P5 https on plain, AFTER (E)", "PASS: 403 `wall: https target on the plain path`, decider not asked, no DNS, origin 0",
            f"{status_line(reply)!r} wall={b'wall: https target on the plain path' in reply} | {facts(seen)} scheme-deny={len(seen.event_types('scheme-deny'))}")
    assert status_line(reply) == "HTTP/1.1 403 Forbidden" and b"wall: https target on the plain path" in reply
    assert seen.req_frames() == [] and seen.dns == [] and seen.origin == [] and len(seen.event_types("scheme-deny")) == 1


@pytest.mark.parametrize("scheme", ["ws", "ftp"])
def test_p5_ws_and_ftp_targets_are_refused_by_srt(r4, r4_glue, verdict, scheme):
    m = r4.mark(r4_glue)
    reply = Raw(r4_glue.port, r4_glue.ca).one_shot(_abs_get(scheme, r4.ports.http))
    seen = r4.since(m, r4_glue)
    verdict(f"R4 P5 {scheme}:// on plain", "PASS: srt `400 bad_request` (a misleading reason), no frame", f"{status_line(reply)!r} | {facts(seen)}")
    assert status_line(reply) == "HTTP/1.1 400 Bad Request" and has_header(reply, "X-Deny-Reason", "bad_request")
    assert seen.req_frames() == [] and seen.origin_bytes() == 0 and seen.dns == []


@pytest.fixture(scope="module")
def r4_raw(r4):
    r4.start_raw_listener()
    return r4.ports.raw


def test_p5_before_https_target_reaches_a_raw_listener_as_a_tls_clienthello_NEGATIVE_CONTROL(r4, r4_raw, verdict):
    """Round 3's glue (scheme check OFF), the raw listener in the HTTP set: the recorded BEFORE failure, reproduced."""
    g = r4.start_glue("G", scheme_check="off", log="raw", extra_http_ports=(r4_raw,))
    try:
        m = r4.mark(g)
        reply = Raw(g.port, g.ca, timeout=4).one_shot(_abs_get("https", r4_raw))
        seen = r4.since(m, g, settle=1.5)
        conns = seen.raw
        hello = [c for c in conns if c["bytes"] > 0]
        verdict("R4 P5 https on plain, BEFORE (G, raw listener)", "FAIL: listener got a 0-byte vetting conn then 1,584 B starting 16 03 01 (a TLS ClientHello); client 502",
                f"reply={status_line(reply)!r} listener conns={[(c['bytes'], c['tls_handshake']) for c in conns]} decider verdicts={[v['action'] for v in seen.verdicts()]}")
        assert len(hello) == 1 and hello[0]["tls_handshake"] is True and hello[0]["bytes"] > 1000
        assert [v["action"] for v in seen.verdicts()] == ["allow"] and not seen.event_types("scheme-deny")
    finally:
        g.stop()


def test_p5_after_the_raw_listener_sees_no_connection(r4, r4_raw, verdict):
    g = r4.start_glue("H", scheme_check="on", extra_http_ports=(r4_raw,))
    try:
        m = r4.mark(g)
        reply = Raw(g.port, g.ca, timeout=4).one_shot(_abs_get("https", r4_raw))
        seen = r4.since(m, g, settle=1.5)
        verdict("R4 P5 https on plain, AFTER (H, raw listener)", "PASS: 403 as above; **no connection** at the listener",
                f"reply={status_line(reply)!r} listener conns={len(seen.raw)} | {facts(seen)}")
        assert status_line(reply) == "HTTP/1.1 403 Forbidden" and seen.raw == [] and seen.dns == [] and seen.req_frames() == []
    finally:
        g.stop()
