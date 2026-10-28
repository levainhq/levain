"""The shipped Codex ``subscription_passthrough`` route: the base map the install helper merges carries one for Codex
beside Claude's, the supervisor's route pick finds it for a ``codex`` passthrough session, and every per-vendor rule
(an operator's own route wins, never two per vendor) holds for it as it does for Claude's."""
from __future__ import annotations

import pytest

from levain.broker import policy as P
from levain.slot import Refused, supervisor as SV, vendor_routes as VR

CODEX_HOSTS = ["api.openai.com", "auth.openai.com", "chatgpt.com"]
OPERATOR_CODEX = {"route": "operator-codex-pass", "type": "subscription_passthrough", "hosts": ["chatgpt.com"]}


def _without_codex_route():
    out, _, _ = VR.merge(None)
    return [r for r in out["routes"] if r["route"] != "codex-subscription"]


def _codex_passes(routes):
    return [r for r in routes if r["type"] == "subscription_passthrough" and SV._hosts(r) & SV.VENDOR_HOSTS["codex"]]


def test_without_the_route_a_codex_passthrough_pick_is_refused_and_with_it_the_pick_succeeds():
    with pytest.raises(Refused) as e:
        SV.pick_routes(_without_codex_route(), "codex", "passthrough")
    assert e.value.name == "no-route:passthrough"
    out, ids, _ = VR.merge(None)
    assert "codex-subscription" in ids
    assert SV.pick_routes(out["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "codex-subscription")


def test_the_route_is_the_measured_shape_with_sorted_hosts_and_only_codex_hosts():
    assert VR.codex_passthrough_route() == {"route": "codex-subscription", "type": "subscription_passthrough",
                                            "hosts": CODEX_HOSTS}
    assert list(VR.CODEX_PASSTHROUGH_HOSTS) == CODEX_HOSTS == sorted(CODEX_HOSTS)
    assert set(VR.CODEX_PASSTHROUGH_HOSTS) <= SV.VENDOR_HOSTS["codex"]
    assert not set(VR.CODEX_PASSTHROUGH_HOSTS) & (SV.VENDOR_HOSTS["claude"] | set(VR.EGRESS_DEFAULT_HOSTS))
    assert [r["route"] for r in VR.vendor_routes()] == ["anthropic", "claude-subscription", "codex-subscription"]


def test_the_merged_map_parses_with_no_clash_and_each_vendor_picks_only_its_own_route():
    out, _, notes = VR.merge(None)
    parsed = P.parse_base_map(out)
    assert parsed["codex-subscription"].type == "subscription_passthrough" and notes == []
    assert VR.connect_host_clashes(parsed) == []
    assert SV.pick_routes(out["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "codex-subscription")
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "claude-subscription")
    assert SV.pick_routes(out["routes"], "claude", "brokered") == (VR.EGRESS_DEFAULT, "anthropic")
    with pytest.raises(Refused) as e:           # no shipped Codex brokered route: that pick is unchanged
        SV.pick_routes(out["routes"], "codex", "brokered")
    assert e.value.name == "no-route:brokered"


def test_merge_keeps_an_operators_codex_passthrough_route_and_adds_no_second_one():
    out, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [dict(OPERATOR_CODEX)]})
    assert _codex_passes(out["routes"]) == [OPERATOR_CODEX]
    assert notes == ["route codex-subscription not added: your own operator-codex-pass is kept as the passthrough "
                     "route for its vendor (two would make every passthrough launch route-ambiguous)"]
    assert "codex-subscription" not in ids and "claude-subscription" in ids
    P.parse_base_map(out)
    assert SV.pick_routes(out["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "operator-codex-pass")
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "claude-subscription")


def test_the_merge_names_the_operators_codex_route_it_kept():
    doc = {"format": VR.BASE_FORMAT, "routes": [dict(OPERATOR_CODEX)]}
    assert VR.yielded_routes(doc) == {"codex-subscription": ["operator-codex-pass"]}


def test_an_operators_codex_route_under_the_shipped_id_is_still_replaced():
    mine = {"route": "codex-subscription", "type": "subscription_passthrough", "hosts": ["chatgpt.com"]}
    doc = {"format": VR.BASE_FORMAT, "routes": [mine]}
    out, ids, notes = VR.merge(doc)
    assert "codex-subscription" in ids and notes == [] and VR.yielded_routes(doc) == {}
    assert _codex_passes(out["routes"]) == [VR.codex_passthrough_route()]


def test_two_operator_routes_for_codex_are_named_and_left_as_written():
    other = {"route": "other-codex-pass", "type": "subscription_passthrough", "hosts": ["auth.openai.com"]}
    two = {"format": VR.BASE_FORMAT, "routes": [dict(OPERATOR_CODEX), other]}
    assert VR.yielded_routes(two) == {"codex-subscription": ["operator-codex-pass", "other-codex-pass"]}
    out, _, notes = VR.merge(two)
    assert notes == ["route codex-subscription not added: your routes operator-codex-pass, other-codex-pass are all "
                     "passthrough routes for its vendor; launches will refuse route-ambiguous until one is removed"]
    with pytest.raises(Refused) as e:
        SV.pick_routes(out["routes"], "codex", "passthrough")
    assert e.value.name == "route-ambiguous:passthrough"


def test_an_operators_claude_route_does_not_stop_the_codex_default_and_the_reverse():
    claude = {"route": "operator-claude-pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}
    out, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [claude]})
    assert "codex-subscription" in ids and "claude-subscription" not in ids
    assert notes == ["route claude-subscription not added: your own operator-claude-pass is kept as the passthrough "
                     "route for its vendor (two would make every passthrough launch route-ambiguous)"]
    assert SV.pick_routes(out["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "codex-subscription")
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "operator-claude-pass")


def test_a_merge_of_its_own_output_is_a_fixed_point_with_the_operators_codex_route():
    out, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [dict(OPERATOR_CODEX)]})
    again, ids2, notes2 = VR.merge(out)
    assert again == out and ids2 == ids and notes2 == notes


def test_an_install_whose_egress_route_carries_an_openai_host_merges_without_the_codex_route():
    # a legal map before codex-subscription shipped: the merge leaves the shipped route out, names why, and still
    # writes every other shipped route (it used to refuse the whole map, base-map-unmergeable)
    mine = {"route": "egress-operator", "type": "egress", "hosts": ["api.openai.com", "docs.example.org"]}
    doc = {"format": VR.BASE_FORMAT, "routes": [mine]}
    out, ids, notes = VR.merge(doc)
    assert ids == ["anthropic", "claude-subscription", "egress-default"]
    assert [r["route"] for r in out["routes"]] == ["egress-operator", "anthropic", "claude-subscription",
                                                   "egress-default"]
    assert notes == ["route codex-subscription not added: your route egress-operator already egresses "
                     "api.openai.com; remove that host from egress-operator to enable Codex passthrough"]
    assert VR.connect_host_clashes(P.parse_base_map(out)) == []
    with pytest.raises(Refused) as e:
        SV.pick_routes(out["routes"], "codex", "passthrough")
    assert e.value.name == "no-route:passthrough"
    assert SV.pick_routes(out["routes"], "claude", "passthrough") == (VR.EGRESS_DEFAULT, "egress-operator",
                                                                       "claude-subscription")
    again, ids2, notes2 = VR.merge(out)
    assert (again, ids2, notes2) == (out, ids, notes)


def test_the_note_names_every_host_and_each_egress_route_that_holds_one():
    a = {"route": "egress-operator", "type": "egress", "hosts": ["auth.openai.com", "chatgpt.com"]}
    b = {"route": "mine", "type": "egress", "hosts": ["api.openai.com"]}
    _, ids, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [a, b]})
    assert "codex-subscription" not in ids
    assert notes == ["route codex-subscription not added: your route egress-operator already egresses "
                     "auth.openai.com, chatgpt.com; remove those hosts from egress-operator to enable Codex passthrough",
                     "route codex-subscription not added: your route mine already egresses api.openai.com; remove "
                     "that host from mine to enable Codex passthrough"]


def test_removing_the_host_brings_the_codex_route_back():
    mine = {"route": "egress-operator", "type": "egress", "hosts": ["chatgpt.com"]}
    out, _, _ = VR.merge({"format": VR.BASE_FORMAT, "routes": [mine]})
    out = VR.set_operator_hosts(out, ["docs.example.org"])
    merged, ids, notes = VR.merge(out)
    assert "codex-subscription" in ids and notes == []
    assert SV.pick_routes(merged["routes"], "codex", "passthrough") == (VR.EGRESS_DEFAULT, "egress-operator",
                                                                         "codex-subscription")


def test_a_clash_between_two_operator_routes_on_a_codex_host_is_still_refused():
    egress = {"route": "egress-operator", "type": "egress", "hosts": ["chatgpt.com"]}
    other = {"route": "mine", "type": "egress", "hosts": ["chatgpt.com"]}
    with pytest.raises(VR.MergeRefused) as e:
        VR.merge({"format": VR.BASE_FORMAT, "routes": [egress, other]})
    assert e.value.code == "base-map-unmergeable"
    assert "host chatgpt.com is on routes egress-operator and mine" in e.value.detail


def test_an_operator_passthrough_route_on_an_egressed_codex_host_is_still_refused():
    # the operator's own passthrough route wins over the shipped one, but its clash with their egress route is theirs
    egress = {"route": "egress-operator", "type": "egress", "hosts": ["chatgpt.com"]}
    with pytest.raises(VR.MergeRefused) as e:
        VR.merge({"format": VR.BASE_FORMAT, "routes": [egress, dict(OPERATOR_CODEX)]})
    assert e.value.code == "base-map-unmergeable"
    assert "host chatgpt.com is on routes egress-operator and operator-codex-pass" in e.value.detail


def test_routes_egress_refusing_an_openai_host_names_the_remedy():
    base, _, _ = VR.merge(None)
    for host in CODEX_HOSTS:
        with pytest.raises(VR.MergeRefused) as e:
            VR.set_operator_hosts(base, [host])
        assert e.value.code == "routes-egress-host-on-route"
        assert e.value.detail == (f"{host} (on route codex-subscription); to egress a host of codex-subscription "
                                  "instead of Codex passthrough, delete route codex-subscription from base.json and "
                                  "run routes-egress again (the merge then leaves codex-subscription out)")
    with pytest.raises(VR.MergeRefused) as e:          # other shipped routes' refusals carry no such remedy
        VR.set_operator_hosts(base, ["claude.ai"])
    assert e.value.detail == "claude.ai (on route claude-subscription)"


def test_the_routes_egress_remedy_works():
    base, _, _ = VR.merge(None)
    without = {**base, "routes": [r for r in base["routes"] if r["route"] != "codex-subscription"]}
    out, ids, notes = VR.merge(VR.set_operator_hosts(without, ["api.openai.com"]))
    assert "codex-subscription" not in ids and len(notes) == 1 and "egress-operator" in notes[0]


def test_the_claude_route_and_its_notes_are_unchanged():
    assert VR.claude_passthrough_route() == {"route": "claude-subscription", "type": "subscription_passthrough",
                                             "hosts": ["api.anthropic.com", "claude.ai", "platform.claude.com"]}
    operator = {"route": "operator-claude-pass", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]}
    _, _, notes = VR.merge({"format": VR.BASE_FORMAT, "routes": [operator]})
    assert notes == ["route claude-subscription not added: your own operator-claude-pass is kept as the passthrough "
                     "route for its vendor (two would make every passthrough launch route-ambiguous)"]
