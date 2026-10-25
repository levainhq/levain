"""CH-D2 S8 (second slice): a held key is placed as its setup record says (R15-9's closed set: bearer, or Basic with a
named user; srt's `credentialPlacements`, set once at launch from `keys.credential_placements`).

Through `start_egress` and the real glue to a local TLS origin (trusted by the `--upstream-ca` seam) that records the
Authorization header it got. `keys.decide_key` is replaced so a key is chosen on the origin's stand-in port: the real
one attaches only on port 443, which a non-root test cannot bind; what is under test is the placement, srt's half.
"""

from __future__ import annotations

import base64
import http.client
import ssl

import pytest

from levain.chat_egress import classify, keys
from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm
from levain.coordinator.envelopes import EnvelopeStore

from .decider_rig import Desk, Live, Lookup, NoStore
from .fixtures import env as envmod
from .test_launch import real_launch
from .test_launch_tls import HOST, TlsOrigin, bad_pki, spike

SECRET = "s3cret-value"


class AuthOrigin(TlsOrigin):
    def __init__(self, cert, key) -> None:
        super().__init__(cert, key)
        self.auth: list[str | None] = []
        handler = self.srv.RequestHandlerClass
        outer = self
        orig = handler.do_GET

        def do_GET(h):
            outer.auth.append(h.headers.get("Authorization"))
            orig(h)
        handler.do_GET = do_GET


def _run(tmp_path, monkeypatch, placement: keys.Placement) -> list[str | None]:
    ca, leaf, key = bad_pki(tmp_path / "pki")
    origin = AuthOrigin(leaf, key)
    real_decide = keys.decide_key

    def decide(route, parsed, **kw):
        if route is None:
            return real_decide(route, parsed, **kw)
        return keys.KeyDecision(route=route, attach=keys.ATTACHED, cred=route.record_id, credential=SECRET)
    monkeypatch.setattr(keys, "decide_key", decide)
    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    route = keys.RouteSnapshot(record_id="rec-1", canonical_host=HOST, credential_source="svc",
                               declared_repos=frozenset(), attach=True, credential_account="acct", incarnation="1:x")
    store = EnvelopeStore(tmp_path / "dispatch")
    P = origin.port
    key_pem, cert_pem = spike.make_ca()
    e = lm.start_egress(store=store, launch_id=real_launch(store), node=envmod.node_bin(),
                        srt_prefix=envmod.srt_prefix(), events=tmp_path / "events.jsonl",
                        ca_pem=key_pem + cert_pem, routes=keys.RouteSet.of([route], {"rec-1": placement}),
                        liveness=Live(), credentials=keys.LaunchCredentials(NoStore()), cards=Desk(),
                        mode=dm.Fixed(classify.GATED),
                        limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0),
                        seams=lm.TestSeams(glue_args=("--tls-ports", f"443,{P}", "--carve", f"127.0.0.1:{P}",
                                                      "--resolve-map", f"{HOST}=127.0.0.1", "--upstream-ca", str(ca)),
                                           lookup=Lookup()))
    try:
        c = http.client.HTTPSConnection("127.0.0.1", e.http_port, timeout=20,
                                        context=ssl.create_default_context(cadata=cert_pem.decode()))
        c.set_tunnel(HOST, P)
        c.request("GET", "/k", headers={"Authorization": "Bearer from-the-sandbox"})
        r = c.getresponse()
        print(f"RUN S8B placement {placement}: {r.status} {r.read()!r}; origin saw Authorization {origin.auth}")
        c.close()
    finally:
        lm.stop_egress(e)
        origin.close()
    return origin.auth


@pytest.mark.chat_egress_srt
def test_a_basic_record_is_placed_as_basic_with_its_user(tmp_path, monkeypatch) -> None:
    got = _run(tmp_path, monkeypatch, keys.Placement("basic", "x-access-token"))
    assert got == ["Basic " + base64.b64encode(f"x-access-token:{SECRET}".encode()).decode()]


@pytest.mark.chat_egress_srt
def test_a_bearer_record_is_placed_as_bearer(tmp_path, monkeypatch) -> None:
    assert _run(tmp_path, monkeypatch, keys.Placement("bearer")) == [f"Bearer {SECRET}"]


@pytest.mark.chat_egress_srt
def test_a_route_without_a_placement_is_refused_with_nothing_left_open(tmp_path, monkeypatch) -> None:
    """S8b r1 item 4 (complement MED 1): the placements line refuses before the first allocation, so a refusal there
    leaves no socket, pipe or glue behind. `RouteSet.of` gives every route a placement (bearer by default), so only a
    RouteSet built directly reaches this line."""
    import os
    from types import MappingProxyType

    from .test_launch import _node_children

    def fds() -> set[int]:
        return {int(x) for x in os.listdir("/dev/fd")} - {max(int(x) for x in os.listdir("/dev/fd"))}

    monkeypatch.setenv(lm.TEST_SEAMS_ENV, "1")
    route = keys.RouteSnapshot(record_id="rec-1", canonical_host=HOST, credential_source="svc",
                               declared_repos=frozenset(), attach=True, credential_account="acct", incarnation="1:x")
    store = EnvelopeStore(tmp_path / "dispatch")
    lid = real_launch(store)
    key_pem, cert_pem = spike.make_ca()
    before, kids = fds(), _node_children()
    with pytest.raises(KeyError) as got:
        lm.start_egress(store=store, launch_id=lid, node=envmod.node_bin(), srt_prefix=envmod.srt_prefix(),
                        events=tmp_path / "events.jsonl", ca_pem=key_pem + cert_pem,
                        routes=keys.RouteSet(MappingProxyType({HOST: route}), MappingProxyType({})), liveness=Live(),
                        credentials=keys.LaunchCredentials(NoStore()), cards=Desk(), mode=dm.Fixed(classify.GATED),
                        seams=lm.TestSeams(lookup=Lookup()))
    left = fds() - before
    print(f"RUN S8D item 4: refused with {type(got.value).__name__} {got.value}; fds left {sorted(left)}")
    assert not left and len(_node_children()) == len(kids)
    assert not (tmp_path / "events.jsonl.glue-stderr").exists()
