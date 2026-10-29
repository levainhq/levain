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
    out, ids, _ = VR.merge(None)
    parsed = P.parse_base_map(out)
    (route,) = _passthrough("claude")
    assert parsed[route["route"]].type == "subscription_passthrough"
    assert route["route"] in ids
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, route["route"])
    assert SV.pick_routes(out["routes"], "claude", "brokered") == (VR.EGRESS_DEFAULT, "anthropic")


def test_codex_passthrough_is_still_refused_by_name():
    # R3: Codex's route is the same shape and comes after Claude's; until then its pick names the gap
    out, _, _ = VR.merge(None)
    with pytest.raises(Refused) as e:
        SV.pick_routes(out["routes"], "codex", "passthrough")
    assert e.value.name == "no-route:passthrough"


#: an operator's own Claude passthrough route under another id, hand-written before one shipped (1026-10 R6)
OPERATOR_PASS = {"route": "operator-claude-pass", "type": "subscription_passthrough",
                 "hosts": ["api.anthropic.com", "platform.claude.com"]}


def _upgraded(*extra):
    return {"format": VR.BASE_FORMAT, "routes": [dict(OPERATOR_PASS), *extra]}


def test_merge_keeps_an_operators_claude_passthrough_route_and_adds_no_second_one():
    out, ids, notes = VR.merge(_upgraded())
    passes = [r for r in out["routes"] if r["type"] == "subscription_passthrough"]
    assert passes == [OPERATOR_PASS]
    assert notes == ["route claude-subscription not added: your own operator-claude-pass is kept as the passthrough "
                     "route for its vendor (two would make every passthrough launch route-ambiguous)"]
    assert "claude-subscription" not in ids
    assert "anthropic" in ids
    P.parse_base_map(out)
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "operator-claude-pass")
    assert SV.pick_routes(out["routes"], "claude", "brokered") == (VR.EGRESS_DEFAULT, "anthropic")


def test_the_merge_names_the_operators_route_it_kept_in_place_of_the_shipped_one():
    assert VR.yielded_routes(_upgraded()) == {"claude-subscription": ["operator-claude-pass"]}
    assert VR.yielded_routes(None) == {}
    assert VR.yielded_routes({"format": VR.BASE_FORMAT, "routes": []}) == {}


def test_an_operators_passthrough_route_under_the_shipped_id_is_still_replaced():
    mine = {"route": "claude-subscription", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}
    out, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [mine]})
    assert "claude-subscription" in ids and notes == []
    assert VR.yielded_routes({"format": VR.BASE_FORMAT, "routes": [mine]}) == {}
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "claude-subscription")


def test_another_vendors_passthrough_route_does_not_stop_the_claude_default():
    codex = {"route": "operator-codex-pass", "type": "subscription_passthrough", "hosts": ["chatgpt.com"]}
    out, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [codex]})
    assert "claude-subscription" in ids and notes == []
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "claude-subscription")
    assert SV.pick_routes(out["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "operator-codex-pass")


def test_a_merge_of_its_own_output_is_a_fixed_point_with_the_operators_route():
    out, ids, notes = VR.merge(_upgraded())
    again, ids2, notes2 = VR.merge(out)
    assert again == out and ids2 == ids and notes2 == notes    # the choice is named on every run, written or not


def test_a_map_holding_both_the_shipped_and_the_operators_route_is_merged_to_the_operators_alone():
    # the map an earlier merge wrote (the default added beside his route): the merge resolves it to his route
    out, ids, _ = VR.merge(_upgraded(VR.claude_passthrough_route()))
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "operator-claude-pass")
    assert "claude-subscription" not in ids


def test_two_operator_routes_for_claude_are_named_and_left_as_written():
    two = _upgraded({"route": "other-claude-pass", "type": "subscription_passthrough", "hosts": ["claude.ai"]})
    assert VR.yielded_routes(two) == {"claude-subscription": ["operator-claude-pass", "other-claude-pass"]}
    out, _, notes = VR.merge(two)
    assert notes == ["route claude-subscription not added: your routes operator-claude-pass, other-claude-pass are "
                     "all passthrough routes for its vendor; launches will refuse route-ambiguous until one is removed"]
    assert [r["route"] for r in out["routes"] if r["type"] == "subscription_passthrough"] == [
        "operator-claude-pass", "other-claude-pass"]


def test_the_brokered_and_passthrough_routes_sharing_the_api_host_are_not_a_clash():
    # connect_host_clashes flags only hosts of an egress route; a session carries one of these two by its auth
    out, _, _ = VR.merge(None)
    parsed = P.parse_base_map(out)
    assert "api.anthropic.com" in parsed["anthropic"].hosts and "api.anthropic.com" in parsed["claude-subscription"].hosts
    assert VR.connect_host_clashes(parsed) == []


def test_egress_default_and_the_passthrough_route_share_no_host():
    # every session carries egress-default; a shared host would be a clash refused at every merge
    assert not set(VR.EGRESS_DEFAULT_HOSTS) & set(VR.CLAUDE_PASSTHROUGH_HOSTS)


def test_an_operator_egress_route_on_a_passthrough_host_is_a_clash_the_merge_refuses():
    mine = {"route": "egress-operator", "type": "egress", "hosts": ["claude.ai"]}
    with pytest.raises(VR.MergeRefused) as e:
        VR.merge({"format": VR.BASE_FORMAT, "routes": [mine]})
    assert e.value.code == "base-map-unmergeable"
    assert "host claude.ai is on routes claude-subscription and egress-operator" in e.value.detail
