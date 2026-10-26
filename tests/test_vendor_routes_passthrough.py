"""The shipped Claude ``subscription_passthrough`` route (1026-10 RULINGS "Passthrough login route" R1-R4): the base map
the install helper merges carries exactly one passthrough route for Claude, its hosts derived from Anthropic's published
Claude Code network requirements (https://code.claude.com/docs/en/network-config, "Network access requirements"),
and the supervisor's route pick finds it for a passthrough session."""
from __future__ import annotations

import pytest

from levain.broker import policy as P
from levain.slot import Refused, supervisor as SV, vendor_routes as VR

#: the doc's rows a CLI subscription login + one turn needs inside the slot: the API, claude.ai account
#: authentication, and the OAuth token exchange/refresh host (the doc: "both Console and claude.ai sign-ins require
#: it"). The browser-side hosts (claude.com, sso.claude.com) run on the operator's own machine, and telemetry /
#: error-reporting / update / plugin hosts are not a login's.
CLAUDE_PASSTHROUGH_HOSTS = {"api.anthropic.com", "claude.ai", "platform.claude.com"}


def _passthrough(vendor):
    return [r for r in VR.vendor_routes()
            if r["type"] == "subscription_passthrough" and SV._hosts(r) & SV.VENDOR_HOSTS[vendor]]


def test_the_base_map_has_exactly_one_claude_passthrough_route_with_exactly_the_doc_hosts():
    mine = _passthrough("claude")
    assert len(mine) == 1
    assert set(mine[0]["hosts"]) == CLAUDE_PASSTHROUGH_HOSTS
    assert len(mine[0]["hosts"]) == len(CLAUDE_PASSTHROUGH_HOSTS)
    assert set(mine[0]) == {"route", "type", "hosts"}      # a CONNECT route carries only these (policy.parse_route)


def test_no_telemetry_or_error_reporting_host_is_shipped():
    hosts = {h for r in VR.vendor_routes() for h in SV._hosts(r)}
    assert not {h for h in hosts if "datadoghq" in h or "sentry" in h or "statsig" in h}


def test_the_passthrough_hosts_are_claude_vendor_hosts_so_no_other_vendor_picks_it():
    (route,) = _passthrough("claude")
    assert set(route["hosts"]) <= SV.VENDOR_HOSTS["claude"]
    assert not set(route["hosts"]) & SV.VENDOR_HOSTS["codex"]


def test_the_merged_map_parses_and_the_supervisor_picks_the_route_for_each_auth():
    out, ids = VR.merge(None)
    parsed = P.parse_base_map(out)
    (route,) = _passthrough("claude")
    assert parsed[route["route"]].type == "subscription_passthrough"
    assert route["route"] in ids
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (route["route"],)
    assert SV.pick_routes(out["routes"], "claude", "brokered") == ("anthropic",)


def test_codex_passthrough_is_still_refused_by_name():
    # R3: Codex's route is the same shape and comes after Claude's; until then its pick names the gap
    out, _ = VR.merge(None)
    with pytest.raises(Refused) as e:
        SV.pick_routes(out["routes"], "codex", "passthrough")
    assert e.value.name == "no-route:passthrough"
