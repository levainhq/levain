"""ROUND 3 B7 (the client closes while the decider holds).

Recorded (ROUND3.md B7): plain and TLS: the client gives up at 1.5 s while the decider holds 4 s; the glue sees
`client-closed` at the client's 1.5 s, the decider's `allow` lands at ~4 s with `aborted:true`; srt dials NOTHING (no DNS
lookup, origin 0). Control (hold=2, client stays): allowed, forwarded, 200, `onResponse` then `client-closed`."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt


def _times(seen):
    req = seen.event_types("req")[-1]["at"]
    return req, {e["t"]: e["at"] - req for e in seen.events if e["t"] != "req"}, seen.event_types("decision")[-1]


@pytest.mark.parametrize("pid,scheme,url_tail", [("R3 B7.1 plain", "http", "b7-plain"), ("R3 B7.2 TLS", "https", "b7-tls")])
def test_b7_client_gives_up_before_the_decider_answers(r3, r3_glue, verdict, pid, scheme, url_tail):
    port = r3.ports.http if scheme == "http" else r3.ports.tls
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"{scheme}://b7.rb.test:{port}/{url_tail}?hold=4", max_time=1.5)
    seen = r3.since(m, r3_glue, settle=4.8)
    _, t, dec = _times(seen)
    verdict(pid, "PASS: client gone at 1.505 s; decision allow aborted:true at ~4.0 s; no DNS, origin 0",
            f"curl rc={res.rc} client-closed@{t['client-closed']:.2f}s decision={dec['action']} aborted={dec['aborted']}@{t['decision']:.2f}s "
            f"dns={len(seen.dns)} origin={seen.origin_bytes()}B")
    assert res.rc == 28                                             # curl's own timeout
    # curl's 1.5 s clock starts before the glue logs `req` (connect and, for TLS, the handshake come first), so on a
    # loaded runner client-closed reads well under 1.5 s from `req`; the bound only has to separate it from the decision
    assert 0.5 < t["client-closed"] < 2.2 and 3.7 < t["decision"] < 5.0
    assert t["client-closed"] < t["decision"]                       # the glue knew BEFORE the decision came back
    assert dec["action"] == "allow" and dec["aborted"] is True
    assert seen.dns == [] and seen.origin_bytes() == 0 and not seen.event_types("onResponse")


def test_b7_3_control_client_stays(r3, r3_glue, verdict):
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"http://b7.rb.test:{r3.ports.http}/b7-ctl?hold=2")
    seen = r3.since(m, r3_glue)
    _, t, dec = _times(seen)
    verdict("R3 B7.3 control", "allowed, forwarded, 200; onResponse then client-closed", f"{client(res)} aborted={dec['aborted']} "
            f"onResponse@{t['onResponse']:.2f}s client-closed@{t['client-closed']:.2f}s origin={seen.origin_bytes()}B")
    assert res.status == 200 and dec["aborted"] is False and t["onResponse"] <= t["client-closed"] and seen.origin_bytes() > 0
