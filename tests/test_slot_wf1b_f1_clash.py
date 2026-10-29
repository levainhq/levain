"""WF-1b fix round 1 (head rulings W1, W2 on the L3 r1 review): a user-written egress route that admits a shipped
``egress-default`` host wins (the merge omits that host from ``egress-default`` and says so); a clash between two
routes the merge does not ship is refused naming both routes and the ``base.json`` remedy, by the merge and by
``routes-egress`` alike; a CONNECT route naming one host twice is refused by keys' own parser."""
from __future__ import annotations

import io
import json

import pytest

from levain.broker import front as F
from levain.broker import policy as P
from levain.contracts import canon
from levain.slot import Refused, install_helper as IH, vendor_routes as VR
from tests.test_slot_wf1b_egress import F3, L, ME, _base, _egress, _Lock, _read_map, _rs  # noqa: F401 (L: fixture)

#: the pre-WF-1b test_slot_routes_verbs fixture (cf2e19bb: ``EGRESS = {"route": "pypi", "type": "egress", "hosts":
#: ["pypi.org"]}``): a hand-written egress route on a host egress-default now ships
OLD_PYPI = {"route": "pypi", "type": "egress", "hosts": ["pypi.org"]}


def _write_map(L, routes, fmt=VR.BASE_FORMAT):
    (L / "routes" / "base.json").write_bytes(canon.canonical_bytes({"format": fmt, "routes": routes}))


def _routes_base(L):
    err = io.StringIO()
    got = IH.run_routes_base(ME, "darwin", lock=_Lock(), store_seams=_rs(L), err=err)
    return got, err.getvalue()


# --- W1: the user's egress route wins over egress-default ---------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["levain-routes-base/1", VR.BASE_FORMAT])
def test_the_pre_wf1b_map_with_a_users_pypi_route_merges(L, fmt):
    _write_map(L, [OLD_PYPI], fmt)
    got, err = _routes_base(L)
    assert got == {"verb": "routes-base", "routes": ["anthropic", "claude-subscription", "codex-subscription", "egress-default"], "written": True}
    parsed = P.parse_base_map(_read_map(L))
    assert list(parsed["pypi"].hosts) == ["pypi.org"]
    assert list(parsed["egress-default"].hosts) == [h for h in F3 if h != "pypi.org"]
    assert VR.connect_host_clashes(parsed) == []
    # one line naming the host and the route that keeps it
    lines = [x for x in err.splitlines() if "pypi.org" in x]
    assert len(lines) == 1 and "route pypi" in lines[0] and "egress-default" in lines[0]
    # and no reverted-field warning for egress-default (its entry is the one the merge ships)
    assert "replaced by the shipped entry" not in err
    # the next run holds it, and says so again
    got, err = _routes_base(L)
    assert got["written"] is False and "pypi.org" in err
    # every session carries both egress routes: the front loads them
    pol = {"format": F.POLICY_FORMAT, "kind": "interactive", "session": "s-0123456789abcdef",
           "routes": [OLD_PYPI, {"route": "egress-default", "type": "egress",
                                 "hosts": list(parsed["egress-default"].hosts)}]}
    F.load_policy(json.dumps(pol).encode())


def test_the_setup_step_merges_the_old_map_and_names_the_omission(L):
    _write_map(L, [OLD_PYPI])
    step, = IH.base_routes_merge_steps("darwin", store=L)
    ok, line = step.call()
    assert ok, line
    assert line.startswith("merged the shipped routes anthropic, claude-subscription, codex-subscription, egress-default")
    assert "pypi.org" in line and "route pypi" in line


def test_an_operator_host_a_later_release_ships_stays_on_egress_operator(L):
    _base(L)
    _egress(L, ["crates.io"])
    doc = _read_map(L)
    # a later release whose egress-default ships crates.io: the operator's route keeps it
    shipped = [*VR.vendor_routes(), {"route": VR.EGRESS_DEFAULT, "type": "egress", "hosts": [*F3, "crates.io"]}]
    orig = VR.shipped_routes
    try:
        VR.shipped_routes = lambda: shipped
        out, ids, notes = VR.merge(doc)
    finally:
        VR.shipped_routes = orig
    parsed = P.parse_base_map(out)
    assert "crates.io" not in parsed["egress-default"].hosts and list(parsed["egress-operator"].hosts) == ["crates.io"]
    assert len(notes) == 1 and "crates.io" in notes[0] and "egress-operator" in notes[0]


def test_every_shipped_host_on_a_users_route_ships_no_egress_default(L):
    _write_map(L, [{"route": "mine", "type": "egress", "hosts": list(F3)}])
    got, err = _routes_base(L)
    assert got["routes"] == ["anthropic", "claude-subscription", "codex-subscription"]
    parsed = P.parse_base_map(_read_map(L))
    assert "egress-default" not in parsed and list(parsed["mine"].hosts) == F3
    assert sum("route mine" in x for x in err.splitlines()) == len(F3)


def test_a_users_passthrough_route_on_a_shipped_host_is_refused(L):
    # head ruling on f1 observation 9: only an egress route (every session carries it) wins a shipped host; a
    # passthrough route reaches only the sessions that pick it, so trimming would take pypi.org from the others
    _write_map(L, [{"route": "sub", "type": "subscription_passthrough", "hosts": ["pypi.org"]}])
    before = (L / "routes" / "base.json").read_bytes()
    with pytest.raises(Refused) as e:
        _routes_base(L)
    assert e.value.name == "base-map-unmergeable"
    d = e.value.detail
    assert "host pypi.org is on routes egress-default and sub" in d and "edit route sub in base.json" in d
    assert (L / "routes" / "base.json").read_bytes() == before


# --- W1: a clash the merge cannot resolve is refused naming both routes and base.json ------------------------------------

def test_a_clash_between_two_users_routes_is_refused_naming_both_and_base_json(L):
    routes = [{"route": "mine", "type": "egress", "hosts": ["crates.io"]},
              {"route": "yours", "type": "egress", "hosts": ["crates.io"]}]
    _write_map(L, routes)
    before = (L / "routes" / "base.json").read_bytes()
    with pytest.raises(Refused) as e:
        _routes_base(L)
    assert e.value.name == "base-map-unmergeable"
    d = e.value.detail
    assert "crates.io" in d and "edit route mine or yours in base.json" in d and "routes-egress" not in d
    assert (L / "routes" / "base.json").read_bytes() == before


def test_a_users_egress_route_on_the_vendor_host_is_still_refused(L):
    _write_map(L, [{"route": "mine", "type": "egress", "hosts": ["api.anthropic.com"]}])
    with pytest.raises(Refused) as e:
        _routes_base(L)
    d = e.value.detail
    assert e.value.name == "base-map-unmergeable" and "routes anthropic and mine" in d
    assert "edit route mine in base.json" in d


def test_routes_egress_refuses_to_write_over_a_map_that_already_clashes(L):
    _base(L)
    doc = _read_map(L)
    doc["routes"] += [{"route": "mine", "type": "egress", "hosts": ["crates.io"]},
                      {"route": "yours", "type": "egress", "hosts": ["crates.io"]}]
    (L / "routes" / "base.json").write_bytes(canon.canonical_bytes(doc))
    before = (L / "routes" / "base.json").read_bytes()
    with pytest.raises(Refused) as e:
        _egress(L, ["docs.example.org"])
    assert e.value.name == "base-map-unmergeable"
    d = e.value.detail
    assert "edit route mine or yours in base.json" in d and "routes-egress" not in d
    assert (L / "routes" / "base.json").read_bytes() == before


def test_connect_host_clashes_are_tuples():
    parsed = P.parse_base_map({"format": VR.BASE_FORMAT, "routes": [
        VR.egress_default_route(), {"route": "mine", "type": "egress", "hosts": ["github.com"]}]})
    assert VR.connect_host_clashes(parsed) == [("github.com", "egress-default", "mine"),
                                               ("github.com", "mine", "egress-default")]


# --- W2: one host twice on one CONNECT route ------------------------------------------------------------------------------

def test_keys_parser_refuses_a_route_naming_a_host_twice():
    for typ in ("egress", "subscription_passthrough"):
        with pytest.raises(P.PolicyError, match="twice"):
            P.parse_base_map({"format": VR.BASE_FORMAT,
                              "routes": [{"route": "mine", "type": typ, "hosts": ["crates.io", "crates.io"]}]})


def test_routes_base_refuses_a_route_naming_a_host_twice(L):
    _write_map(L, [{"route": "mine", "type": "egress", "hosts": ["crates.io", "crates.io"]}])
    before = (L / "routes" / "base.json").read_bytes()
    with pytest.raises(Refused) as e:
        _routes_base(L)
    assert e.value.name == "base-map-malformed" and "crates.io" in e.value.detail
    assert (L / "routes" / "base.json").read_bytes() == before


def test_routes_egress_refuses_a_host_your_route_already_admits(L):
    _write_map(L, [OLD_PYPI])
    _routes_base(L)
    with pytest.raises(Refused) as e:
        _egress(L, ["pypi.org"])
    assert e.value.name == "routes-egress-host-on-route" and "pypi.org (on route pypi)" in e.value.detail
