"""ROUND 3 B1 (port walls), PATCHED column: the strict glue against the ports the as-is round-2 glue let through.

Recorded (ROUND3.md B1, patched column B2): every wall row is `403 host_not_allowed` with a `filter-deny` event and
NO decider frame, NO DNS lookup, NO origin byte; the control row (keyed host on the 443 stand-in) is a 200 with
`authorization: Bearer SPIKE-KEY-B1` at the origin. The as-is column (the round-2 glue: key re-attached on 8443, TLS
ClientHello at a plain port, an https absolute-form dial) is the BEFORE measurement and is not reproducible on the
round-4 glue, which has no asis mode; the walls here are the ones that closed those failures."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt


def _walled(rig, glue, seen, res, *, connect: bool, port: int):
    assert seen.origin_bytes() == 0, "a walled request reached the origin"
    assert seen.dns == [] and seen.req_frames() == [] and seen.verdicts() == [], "a walled request reached srt's dial or the decider"
    denies = seen.event_types("filter-deny")
    assert [(e["port"], e["connect"]) for e in denies] == [(port, connect)], denies


def _connect_case(r3, r3_glue, verdict, pid, recorded, url, *curl_args, port, extra=()):
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, url, *curl_args)
    seen = r3.since(m, r3_glue)
    verdict(pid, recorded, f"{client(res)} | {facts(seen)} | filter-deny {[(e['port'], e['connect']) for e in seen.event_types('filter-deny')]}")
    assert res.rc == 56 and res.status == 0, res
    assert "CONNECT tunnel failed, response 403" in res.err and res.deny_reason == "host_not_allowed", res
    _walled(r3, r3_glue, seen, res, connect=True, port=port)


def test_b1_1_connect_to_off_tls_port_keyed_host(r3, r3_glue, verdict):
    p = r3.ports
    _connect_case(r3, r3_glue, verdict, "R3 B1.1 (patched)",
                  "CONNECT 403 X-Deny-Reason host_not_allowed; filter-deny port 8443 connect:true; no frame, no DNS, origin 0",
                  f"https://api.rb.test:{p.off_tls}/b1-8443", "-H", "Authorization: Bearer CLIENT-SECRET", port=p.off_tls)


def test_b1_2_tls_to_the_plain_port_standin(r3, r3_glue, verdict):
    p = r3.ports
    _connect_case(r3, r3_glue, verdict, "R3 B1.2 (patched)",
                  "CONNECT 403, filter-deny port 9080; origin 0",
                  f"https://api.rb.test:{p.http}/b1-80-tls", port=p.http)


def test_b1_3_connect_to_the_plain_port_without_tls(r3, r3_glue, verdict):
    p = r3.ports
    _connect_case(r3, r3_glue, verdict, "R3 B1.3 (patched)",
                  "CONNECT 403, filter-deny", f"http://api.rb.test:{p.http}/b1-80-notls", "-p", port=p.http)


def test_b1_5_connect_to_off_tls_port_unkeyed(r3, r3_glue, verdict):
    p = r3.ports
    _connect_case(r3, r3_glue, verdict, "R3 B1.5 (patched)",
                  "CONNECT 403, origin 0", f"https://s.rb.test:{p.off_tls}/b1-8443-unkeyed", port=p.off_tls)


@pytest.mark.parametrize("pid,host,recorded", [
    ("R3 B1.4 (patched)", "api.rb.test", "403 host_not_allowed, filter-deny port 8080 connect:false (keyed host)"),
    ("R3 B1.4b (patched)", "s.rb.test", "403 host_not_allowed, origin 0 (unkeyed host)"),
])
def test_b1_4_absolute_form_http_to_off_port(r3, r3_glue, verdict, pid, host, recorded):
    p = r3.ports
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"http://{host}:{p.off_http}/b1-8080", "-H", "Authorization: Bearer CLIENT-SECRET")
    seen = r3.since(m, r3_glue)
    verdict(pid, recorded, f"{client(res)} | {facts(seen)}")
    assert res.status == 403 and res.deny_reason == "host_not_allowed", res
    _walled(r3, r3_glue, seen, res, connect=False, port=p.off_http)


@pytest.mark.parametrize("pid,host,recorded", [
    ("R3 B1.6 raw (patched)", "api.rb.test", "403 host_not_allowed, filter-deny port 9443 connect:false (https absolute-form, keyed, no CONNECT)"),
    ("R3 B1.7 raw (patched)", "s.rb.test", "403, origin 0, no DNS (https absolute-form, unkeyed: the second egress path)"),
])
def test_b1_6_7_absolute_form_https_without_connect(r3, r3_glue, verdict, pid, host, recorded):
    p = r3.ports
    m = r3.mark(r3_glue)
    cred = b"Authorization: Bearer CLIENT-SECRET\r\n" if host.startswith("api") else b""
    reply = clients.Raw(r3_glue.port, r3_glue.ca).one_shot(
        f"GET https://{host}:{p.tls}/abs-https HTTP/1.1\r\nHost: {host}:{p.tls}\r\n".encode() + cred + b"Connection: close\r\n\r\n")
    seen = r3.since(m, r3_glue)
    verdict(pid, recorded, f"{clients.status_line(reply)!r} deny={clients.has_header(reply, 'X-Deny-Reason', 'host_not_allowed')} | {facts(seen)}")
    assert clients.status_line(reply) == "HTTP/1.1 403 Forbidden" and clients.has_header(reply, "X-Deny-Reason", "host_not_allowed"), reply
    _walled(r3, r3_glue, seen, None, connect=False, port=p.tls)


def test_b1_ctl_keyed_host_on_the_tls_standin_gets_the_key(r3, r3_glue, verdict):
    p = r3.ports
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"https://api.rb.test:{p.tls}/b1-ctl", "-H", "Authorization: Bearer CLIENT-SECRET")
    seen = r3.since(m, r3_glue)
    raw = " ".join(d["raw"] for d in seen.origin if d.get("bytes"))
    verdict("R3 B1.ctl (patched)", "200, `authorization: Bearer SPIKE-KEY-B1` at origin (key on (https, host, 443-class) only)",
            f"status={res.status} key_at_origin={'SPIKE-KEY-B1' in raw} client_secret_at_origin={'CLIENT-SECRET' in raw} | {facts(seen)}")
    assert res.status == 200 and "origin-ok" in res.out
    assert "authorization: Bearer SPIKE-KEY-B1" in raw and "CLIENT-SECRET" not in raw
    assert len(seen.req_frames()) == 1 and [v["action"] for v in seen.verdicts()] == ["allow"]


def test_negative_control_an_admitted_off_port_is_seen_to_reach_the_origin(r3, verdict):
    """The port probes must be able to FAIL: a glue whose HTTP set admits the off port lets the same request through,
    and the observers (origin bytes, decider frame) see it. Without this a green wall row could be a blind probe."""
    p = r3.ports
    g = r3.start_glue("B1-neg", extra_http_ports=(p.off_http,))
    try:
        m = r3.mark(g)
        res = clients.curl(g.port, g.ca, f"http://s.rb.test:{p.off_http}/b1-8080-unkeyed")
        seen = r3.since(m, g)
        verdict("R3 B1.4b negative control (off-port admitted)", "(the as-is column: 200, origin :8080 got the request 111 B)",
                f"{client(res)} | {facts(seen)}")
        assert res.status == 200 and seen.origin_bytes() > 0 and len(seen.req_frames()) == 1 and not seen.event_types("filter-deny")
    finally:
        g.stop()
