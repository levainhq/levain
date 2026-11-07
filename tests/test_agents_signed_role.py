"""levain.agents.signed_role (levain 1107, W2): the signed role's withhold, re-derived from a hash chain.

Each refusal below names the link of the chain it breaks; each test names the line whose revert turns it red."""
from __future__ import annotations

import copy
import dataclasses

import pytest

from levain.agents import role as R
from levain.agents import spec as SP
from levain.agents import signed_role as SR
from levain.authority import _canonical

ROLE = {"name": "scribe", "brief": {"template": "w", "slots": {}}, "ceiling": {"lands": ["flow"], "withhold": ["docs/**"]},
        "model": "m", "harness": "claude", "executor": "session"}
AGENT = "ag-" + "2" * 32


def chain(role=ROLE):
    spec = {"name": "a", "role": {"name": role["name"], "role_hash": R.role_hash(role)}}
    return SR.SignedRole(AGENT, _canonical(spec), R.canonical_role(role)), SP.spec_hash(AGENT, spec)


def test_the_prefixes_have_one_home():
    assert SP.SPEC_HASH_PREFIX is SR.SPEC_HASH_PREFIX and R.ROLE_HASH_PREFIX is SR.ROLE_HASH_PREFIX   # revert: a copy drifts


def test_the_chain_derives_the_roles_withhold_against_the_links_spec_hash():
    signed, h = chain()
    assert SR.signed_withhold(signed, h) == ("docs/**",)
    assert SR.signed_withhold(signed, None) == ("docs/**",)                         # the operator side compares no link
    plain = copy.deepcopy(ROLE)
    del plain["ceiling"]["withhold"]
    s2, h2 = chain(plain)
    assert SR.signed_withhold(s2, h2) == ()


@pytest.mark.parametrize("tamper,name", [
    (lambda s: dataclasses.replace(s, spec=s.spec.replace('"a"', '"b"')), "spec-hash"),              # revert: spec hash compare
    (lambda s: dataclasses.replace(s, role=s.role.replace("scribe", "scribf")), "role-hash"),         # revert: role hash compare
    (lambda s: dataclasses.replace(s, agent_id="ag-" + "3" * 32), "spec-hash"),                      # the agent id is hashed
    (lambda s: dataclasses.replace(s, spec=s.spec + " "), "spec-not-canonical"),
    (lambda s: dataclasses.replace(s, spec=s.spec.replace('{"', '{ "', 1)), "spec-not-canonical"),     # revert: canonical round trip
    (lambda s: dataclasses.replace(s, spec="[1]"), "spec-unreadable"),
])
def test_a_broken_link_of_the_chain_is_refused_by_name(tamper, name):
    signed, h = chain()
    with pytest.raises(SR.SignedRoleRefused) as exc:
        SR.signed_withhold(tamper(signed), h)
    assert exc.value.name == name, exc.value


def test_a_withhold_that_is_not_a_list_of_strings_is_refused_even_under_a_valid_hash():
    # a role text can only hash to the pin if it IS the signed one, so forge BOTH ends consistently
    bad = copy.deepcopy(ROLE)
    bad["ceiling"]["withhold"] = "docs/**"
    spec = {"role": {"name": "scribe", "role_hash": SR.hashlib.sha256(SR.ROLE_HASH_PREFIX + _canonical(bad).encode()).hexdigest()}}
    signed = SR.SignedRole(AGENT, _canonical(spec), _canonical(bad))
    with pytest.raises(SR.SignedRoleRefused) as exc:
        SR.signed_withhold(signed, None)
    assert exc.value.name == "withhold"


def test_withhold_problem_names_a_loosening_apart_from_a_mismatch():
    assert SR.withhold_problem(("a", "b"), ("a", "b")) is None
    assert SR.withhold_problem(("a", "b"), ("a",))[0] == "withhold-loosened"            # revert: want - have branch
    assert SR.withhold_problem(("a",), ("a", "c"))[0] == "withhold-mismatch"            # revert: have - want branch
    assert SR.withhold_problem(("a", "b"), ("c",))[0] == "withhold-loosened"            # fewer AND different: loosening wins
    assert SR.withhold_problem((), ()) is None


def test_the_signed_role_shape_is_checked_at_construction():
    for kw in ({"agent_id": "x"}, {"spec": ""}, {"role": "x" * (SR.SIGNED_TEXT_MAX + 1)}):
        with pytest.raises(ValueError):
            SR.SignedRole(**{"agent_id": AGENT, "spec": "{}", "role": "{}", **kw})
