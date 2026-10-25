"""ROUND 3 B6 (the proxy's environment and upstream trust).

Recorded (ROUND3.md B6): B2 closed env + origin CA only -> bad-CA leaf 502 PASS; C1 closed env while the PARENT holds
NODE_EXTRA_CA_CERTS=bad-ca.pem, system roots -> 502 PASS (public control 200); C2 node INHERITS the hostile var ->
**200, the origin got `GET /b6` over TLS to the bad cert: FAIL** (srt trusts the parent's NODE_EXTRA_CA_CERTS); C3 inherited
env + `tlsTerminateUpstreamCA: tls.rootCertificates` (explicit roots) -> 502 PASS.
C2 is the probe's NEGATIVE CONTROL: it is the failing shape, reproduced through the decider's `SPIKE_ENV=parent` patch."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt


def _b6(rig, glue):
    m = rig.mark(glue)
    res = clients.curl(glue.port, glue.ca, f"https://bad.rb.test:{rig.ports.tls}/b6")
    return res, rig.since(m, glue)


def test_b6_b2_closed_env_origin_ca_only_refuses_the_bad_cert(r3, r3_glue, verdict):
    res, seen = _b6(r3, r3_glue)
    verdict("R3 B6 run B2 (closed env, origin CA only)", "502 PASS (bad cert refused); stderr env-keys=1", f"{client(res)} | origin={seen.origin_bytes()}B | {r3_glue.stderr_first[-60:]!r}")
    assert res.status == 502 and seen.origin_bytes() == 0
    assert "env-keys=1" in r3_glue.stderr_first, r3_glue.stderr_first        # node got the closed env (one key the OS adds)
    m = r3.mark(r3_glue)
    ctl = clients.curl(r3_glue.port, r3_glue.ca, f"https://s.rb.test:{r3.ports.tls}/b6-ctl")
    assert ctl.status == 200 and "origin-ok" in ctl.out          # the origin CA leaf IS trusted: the 502 above is the cert, not the rig


def test_b6_c1_closed_env_ignores_a_hostile_parent_variable(r3, verdict):
    g = r3.start_glue("C1", upstream_ca="none", hostile_extra_ca=True)
    try:
        res, seen = _b6(r3, g)
        verdict("R3 B6 run C1 (closed env, parent has NODE_EXTRA_CA_CERTS=bad-ca)", "502 PASS (stderr shows the var unset)",
                f"{client(res)} | origin={seen.origin_bytes()}B | {g.stderr_first[-60:]!r}")
        assert res.status == 502 and seen.origin_bytes() == 0 and "env-keys=1" in g.stderr_first
    finally:
        g.stop()


def test_b6_c2_inherited_hostile_variable_is_trusted_NEGATIVE_CONTROL(r3, verdict):
    g = r3.start_glue("C2", upstream_ca="none", hostile_extra_ca=True, env_parent=True)
    try:
        res, seen = _b6(r3, g)
        reqs = [d for d in seen.origin if d.get("bytes")]
        verdict("R3 B6 run C2 (node inherits the hostile var)", "FAIL: 200; origin got `GET /b6` (105 B) from srt over TLS to the bad cert",
                f"{client(res)} | origin requests={[(d['bytes'], 'GET /b6' in d['raw']) for d in reqs]} | env-keys="
                f"{g.stderr_first.split('env-keys=')[-1]}")
        assert res.status == 200 and len(reqs) == 1 and "GET /b6 " in reqs[0]["raw"]
        assert int(g.stderr_first.split("env-keys=")[-1]) > 10        # the inherited, full env
    finally:
        g.stop()


def test_b6_c3_explicit_roots_ignore_the_inherited_variable(r3, verdict):
    g = r3.start_glue("C3", upstream_ca="system-explicit", hostile_extra_ca=True, env_parent=True)
    try:
        res, seen = _b6(r3, g)
        verdict("R3 B6 run C3 (inherited env + explicit roots)", "502 PASS (explicit roots ignore NODE_EXTRA_CA_CERTS)",
                f"{client(res)} | origin={seen.origin_bytes()}B")
        assert res.status == 502 and seen.origin_bytes() == 0
    finally:
        g.stop()


@pytest.mark.chat_egress_network
def test_b6_public_control_through_system_roots(r3, verdict):
    """The one public GET of the round: example.com through a glue that trusts the system roots (C1's control)."""
    g = r3.start_glue("C1-net", upstream_ca="none", hostile_extra_ca=True)
    try:
        res = clients.curl(g.port, g.ca, "https://example.com/", max_time=25)
        verdict("R3 B6 run C1 public control", "200 (example.com through system roots)", client(res))
        assert res.status == 200
    finally:
        g.stop()
