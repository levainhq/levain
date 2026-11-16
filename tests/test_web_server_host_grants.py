"""The cockpit's host-grant routes (board levain#66, S5a): the mount condition, the gate chain, Phill's rule (c) and the
log line, over a real `make_server` and a FAKE root spawn (no sudo, no network, never his live apps)."""
from __future__ import annotations

import http.client
import json
import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from levain import web_server
from levain.cockpit.firejournal import BOX_MINTER
from levain.consent import host_grants as HG
from levain.dashboard import SubstrateSource
from levain.web_server import make_server
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _pair, _serve
from tests.test_host_grants_surface import G1, G2, G3, S1, S2, FakeRoot

RELAY = "pairing-relay-token-" + "r" * 20
LIST = "/cockpit/hostgrant/list.json"
REVOKE = "/cockpit/hostgrant/revoke"


@pytest.fixture
def src(tmp_path: Any) -> SubstrateSource:
    return _install(tmp_path)[1]


@pytest.fixture
def root() -> FakeRoot:
    return FakeRoot()


def _mounted(src: SubstrateSource, root: FakeRoot):
    return _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=HG.HostGrantSurface(spawn=root))


def test_the_page_token_lists_and_revokes_and_the_reply_carries_a_fresh_listing(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        st, got, hdrs = _call(port, "GET", LIST, token=T)
        assert st == 200 and [s["session"] for s in got["slots"]] == [S1, S2]
        assert hdrs["cache-control"] == "private, no-store"
        st, out, _ = _call(port, "POST", REVOKE, {"session": S1, "target": G1}, token=T)
        assert st == 200 and out["result"] == "revoked" and out["count"] == 1
        assert [g["grant"] for g in out["listing"]["slots"][0]["grants"]] == [G2]
        st, out, _ = _call(port, "POST", REVOKE, {"session": S1, "target": G1}, token=T)
        assert st == 200 and out["result"] == "gone"


def test_a_writable_server_with_a_token_but_no_host_grants_argument_is_404_on_every_prefix_path(src, root) -> None:
    """T10 / X2: the mount is an explicit capability, never "a token exists" and never "a Signer exists"."""
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}) as (port, httpd):
        assert httpd.host_grants is None
        for path in (LIST, REVOKE, "/cockpit/hostgrant/", "/cockpit/hostgrant/x/y.json"):
            assert _call(port, "GET", path, token=T)[0] == 404, path
            assert _call(port, "POST", path, {"session": S1, "target": "all"}, token=T)[0] == 404, path
    assert root.calls == []


def test_the_prefix_is_reserved_so_no_extra_route_can_claim_a_path_under_it(src) -> None:
    assert web_server._HOSTGRANT_LIST_PATH in web_server._RESERVED_PATHS
    assert web_server._HOSTGRANT_REVOKE_PATH in web_server._RESERVED_PATHS
    assert web_server._HOSTGRANT_PREFIX in web_server._RESERVED_PREFIXES
    with pytest.raises(ValueError):
        make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": T},
                    extra_json={"/cockpit/hostgrant/mine.json": lambda *a, **k: {}})


def test_make_server_refuses_host_grants_it_cannot_serve(src, tmp_path) -> None:
    with pytest.raises(ValueError, match="not a HostGrantSurface"):
        make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": T}, host_grants=lambda: None)
    import dataclasses

    ro = dataclasses.replace(src, write_scope=None)
    with pytest.raises(ValueError, match="refusing host_grants"):
        make_server(ro, host="127.0.0.1", port=0, host_grants=HG.HostGrantSurface(spawn=FakeRoot()))


def test_no_credential_is_403_and_starts_no_helper(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        assert _call(port, "GET", LIST)[0] == 403
        assert _call(port, "GET", LIST, token="x" * 20)[0] == 403
        assert _call(port, "POST", REVOKE, {"session": S1, "target": "all"})[0] == 403
        assert _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token="x" * 20)[0] == 403
    assert root.calls == []


def test_a_wrong_host_is_refused_and_starts_no_helper(src, root) -> None:
    """C3: the Host allowlist runs before routing (DNS rebinding)."""
    with _mounted(src, root) as (port, _h):
        st, _o, _ = _call(port, "GET", LIST, token=T, headers={"Host": "evil.example.com"})
        assert st in (403, 421)
        st, _o, _ = _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=T,
                          headers={"Host": "evil.example.com"})
        assert st in (403, 421)
    assert root.calls == []


def test_fetch_metadata_a_cross_site_read_is_403_and_a_curl_shaped_request_with_a_credential_is_admitted(src, root) -> None:
    """T10/X4: valid Host + an admitted credential + (same-origin or no fetch metadata) + JSON."""
    with _mounted(src, root) as (port, _h):
        assert _call(port, "GET", LIST, token=T, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=T,
                     headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert root.calls == []
        assert _call(port, "GET", LIST, token=T, headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
        assert _call(port, "GET", LIST, token=T)[0] == 200                      # no Sec-Fetch-Site: curl-shaped
        assert _call(port, "GET", LIST)[0] == 403                               # the same, without a credential


def test_a_post_that_is_not_json_is_refused_before_any_helper(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        c.request("POST", REVOKE, body=b'{"session": "x"}', headers={"X-Levain-Write-Token": T, "Content-Type": "text/plain"})
        r = c.getresponse()
        r.read()
        assert r.status in (400, 415)
        st, out, _ = _call(port, "POST", REVOKE, ["not", "an", "object"], token=T)
        assert st == 400 and out["error"] == "bad_request"
    assert root.calls == []


def test_a_malformed_revoke_is_400_over_http_with_no_helper_beyond_none(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        for body in ({"session": "s-1", "target": "all"}, {"session": S1, "target": "-x"}, {"session": S1}, {}):
            st, out, _ = _call(port, "POST", REVOKE, body, token=T)
            assert st == 400 and out["error"] == "bad_request", (body, out)
    assert root.calls == []


def test_a_wrong_path_under_the_prefix_is_404_even_mounted(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        assert _call(port, "GET", "/cockpit/hostgrant/other.json", token=T)[0] == 404
        assert _call(port, "POST", "/cockpit/hostgrant/other", {"session": S1, "target": "all"}, token=T)[0] == 404
    assert root.calls == []


def test_the_revoke_never_reaches_the_passkey_ceremony_class(src, root, monkeypatch) -> None:
    """T11: a revoke only narrows; GrantPhone.step is never called and the authority-grant routes stay as they were."""
    from levain.consent.grant_phone import GrantPhone

    called: list = []
    monkeypatch.setattr(GrantPhone, "step", lambda *a, **k: called.append(a))
    with _mounted(src, root) as (port, httpd):
        assert httpd.grant_phone is None
        assert _call(port, "POST", REVOKE, {"session": S1, "target": G1}, token=T)[0] == 200
        assert _call(port, "GET", "/cockpit/grant/list.json", token=T)[0] == 404
    assert called == []


# -- Phill's rule (c), T23 ------------------------------------------------------------------------------------------------


def test_only_the_page_token_and_devices_paired_from_the_cockpit_or_the_laptop_may_list_and_revoke(src, root) -> None:
    """T23 (np-053498e2 (c), his 11-08 launch-grant rule): the page's own token, a device paired from it (and its
    children) and a box-rooted device are admitted; a device a relay paired, its child and the relay's own token are
    403 on list and revoke, and nothing is spawned for them."""
    with _mounted(src, root) as (port, httpd):
        _dev, page_dev = _pair(port)
        _kid, page_kid = _pair(port, token=page_dev, label="child", proxied=True)
        code = httpd.broker.active_db().mint_offer(label="iphone", minted_by=BOX_MINTER, now=datetime.now(timezone.utc))
        st, cl, _ = _call(port, "POST", "/cockpit/pair/claim", {"code": code})
        assert st == 200
        _rdev, relay_dev = _pair(port, token=RELAY, label="relay-made")
        _rkid, relay_kid = _pair(port, token=relay_dev, label="relay-child", proxied=True)
        for tok in (T, page_dev, page_kid, cl["token"]):
            assert _call(port, "GET", LIST, token=tok, headers=PROXY if tok != T else None)[0] == 200, tok
            st, out, _ = _call(port, "POST", REVOKE, {"session": S2, "target": "eh-" + "9" * 24}, token=tok,
                               headers=PROXY if tok != T else None)
            assert st == 200 and out["result"] == "gone", (tok, out)
        spawned = len(root.calls)
        for tok in (RELAY, relay_dev, relay_kid):
            st, out, _ = _call(port, "GET", LIST, token=tok, headers=PROXY if tok != RELAY else None)
            assert st == 403 and out["error"] == "forbidden", (tok, st, out)
            st, out, _ = _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=tok,
                               headers=PROXY if tok != RELAY else None)
            assert st == 403 and out["error"] == "forbidden", (tok, st, out)
        assert len(root.calls) == spawned, "a refused device must start no helper"
        assert "egress-grant-revoke" not in root.verbs()


# -- the rate limit and the log (T20 / T22 over HTTP) ------------------------------------------------------------------------


def test_the_eleventh_revoke_in_a_minute_is_429_over_http_and_reaches_no_root_revoke(src, root) -> None:
    with _mounted(src, root) as (port, _h):
        for _ in range(HG.RATE_LIMIT):
            assert _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=T)[0] == 200
        n = root.verbs().count("egress-grant-revoke")
        st, out, _ = _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=T)
        assert st == 429 and out["error"] == "rate_limited" and root.verbs().count("egress-grant-revoke") == n


def test_the_wall_counts_from_the_requests_start_not_after_its_gates(src, root, monkeypatch) -> None:
    """L3 r1 (codex 61): a paired request can wait up to _PAIR_WAIT_S on the broker before the route runs; the wall
    began after that, so a revoke could still run when the page's 15 s fetch had given up. Reproduced on the first
    cut: the deadline handed to the surface was the gate's delay later than the request's start + the wall."""
    import time

    seen: list = []

    class Seen(HG.HostGrantSurface):
        def listing(self, deadline: float, release_gate: Any = None) -> Any:
            seen.append(deadline)
            return super().listing(deadline, release_gate)

        def revoke(self, session: Any, target: Any, credential: str, deadline: float) -> Any:
            seen.append(deadline)
            return super().revoke(session, target, credential, deadline)

    slow = web_server._Handler._coord_may_launch

    def gate(self: Any, device: Any) -> bool:
        time.sleep(0.6)                                     # a gate that takes its time (the pair wait's stand-in)
        return slow(self, device)

    monkeypatch.setattr(web_server._Handler, "_coord_may_launch", gate)
    with _serve(src, surface_tokens={"browser": T, "relay": RELAY}, host_grants=Seen(spawn=root)) as (port, _h):
        for method, path, body in (("GET", LIST, None), ("POST", REVOKE, {"session": S1, "target": G1})):
            t0 = time.monotonic()
            assert _call(port, method, path, body, token=T)[0] == 200
            assert seen[-1] <= t0 + HG.HOSTGRANT_WALL_S + 0.3, (method, seen[-1] - t0)


def test_the_revoke_log_line_names_the_credential_and_never_holds_a_token(src, root, caplog) -> None:
    """T22: the credential's NAME, the session, the target and root's answer; no token value anywhere in the log."""
    with caplog.at_level(logging.DEBUG):
        with _mounted(src, root) as (port, _h):
            _dev, page_dev = _pair(port)
            assert _call(port, "POST", REVOKE, {"session": S1, "target": G1}, token=T)[0] == 200
            assert _call(port, "POST", REVOKE, {"session": S1, "target": "all"}, token=page_dev)[0] == 200
    lines = [r.getMessage() for r in caplog.records if "host grant revoke" in r.getMessage()]
    assert any("'browser'" in l and S1 in l and G1 in l and "removed=1" in l for l in lines), lines
    assert any(f"'paired-{_dev}'" in l and "target=all" in l for l in lines), lines
    for secret in (T, page_dev, RELAY):
        assert secret not in caplog.text and all(secret not in l for l in lines)


# -- run_web_server's wiring ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("write, present, mounted", [(True, True, True), (True, False, False), (False, True, False)])
def test_run_web_server_passes_host_grants_only_with_write_and_a_helper_on_disk(src, monkeypatch, write, present, mounted) -> None:
    """The mount is passed ONLY by run_web_server and only with --write and a runtime helper on disk."""
    seen: dict = {}

    def stop(*a: Any, **kw: Any):
        seen.update(kw)
        raise ValueError("stop here")

    monkeypatch.setattr(web_server, "make_server", stop)
    monkeypatch.setattr(web_server, "_runtime_helper_present", lambda: present)
    assert web_server.run_web_server(src.install_root, open_browser=False, write=write) == 1
    assert ("host_grants" in seen) is mounted
    if mounted:
        assert isinstance(seen["host_grants"], HG.HostGrantSurface)


def test_the_runtime_helper_probe_reads_the_layout_path_and_fails_closed(monkeypatch) -> None:
    from levain.slot import layout

    monkeypatch.setattr(layout, "runtime_helper_path", lambda host: (_ for _ in ()).throw(KeyError(host)))
    assert web_server._runtime_helper_present() is False
