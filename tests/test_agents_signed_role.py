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
    """The evidence for an agent whose spec pins ``role``, and the verified link's terms that name the same role."""
    spec = {"name": "a", "role": {"name": role["name"], "role_hash": R.role_hash(role)}}
    return (SR.SignedRole(AGENT, _canonical(spec), R.canonical_role(role)),
            SR.SignedLink(AGENT, SP.spec_hash(AGENT, spec), R.role_hash(role)))


def test_the_prefixes_have_one_home():
    assert SP.SPEC_HASH_PREFIX is SR.SPEC_HASH_PREFIX and R.ROLE_HASH_PREFIX is SR.ROLE_HASH_PREFIX   # revert: a copy drifts


def test_the_chain_derives_the_roles_withhold_against_the_verified_link():
    signed, h = chain()
    assert SR.signed_withhold(signed, h) == ("docs/**",)
    with pytest.raises(SR.SignedRoleRefused) as exc:             # revert: the `type(link) is not SignedLink` refusal
        SR.signed_withhold(signed, None)                         # no side derives a withhold without a verified link
    assert exc.value.name == "link"
    plain = copy.deepcopy(ROLE)
    del plain["ceiling"]["withhold"]
    s2, h2 = chain(plain)
    assert SR.signed_withhold(s2, h2) == ()


@pytest.mark.parametrize("tamper,name", [
    (lambda s: dataclasses.replace(s, spec=s.spec.replace('"a"', '"b"')), "spec-hash"),              # revert: spec hash compare
    (lambda s: dataclasses.replace(s, role=s.role.replace("scribe", "scribf")), "role-hash"),         # revert: role hash compare
    (lambda s: dataclasses.replace(s, agent_id="ag-" + "3" * 32), "link-agent"),                     # not the link's agent
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
    rh = SR.hashlib.sha256(SR.ROLE_HASH_PREFIX + _canonical(bad).encode()).hexdigest()
    spec = {"role": {"name": "scribe", "role_hash": rh}}
    signed = SR.SignedRole(AGENT, _canonical(spec), _canonical(bad))
    with pytest.raises(SR.SignedRoleRefused) as exc:
        SR.signed_withhold(signed, SR.SignedLink(AGENT, SP.spec_hash(AGENT, spec), rh))
    assert exc.value.name == "withhold"


# --- L3 W2 r1 HIGH (codex): the role the withhold comes from is the role the verified link NAMES ------------------------------

LOCKED = copy.deepcopy(ROLE)
LOCKED["ceiling"]["withhold"] = ["secrets/**"]
OPEN = copy.deepcopy(ROLE)
del OPEN["ceiling"]["withhold"]


def test_a_link_naming_a_locked_role_whose_spec_pins_an_open_one_derives_nothing():
    """The exact scenario: a hand-authored link signs ``role_hash`` = R_locked (withholds secrets/**) beside a
    ``spec_hash`` whose canonical spec pins R_open (no withhold). The authority check accepts each hash on its own; the
    chain through the spec alone would derive R_open's empty withhold and the snapshot would carry secrets/token."""
    signed, link = chain(OPEN)
    assert SR.signed_withhold(signed, link) == ()                                   # the honest open link: nothing withheld
    forged = dataclasses.replace(link, role_hash=R.role_hash(LOCKED))               # same spec_hash, the LOCKED role named
    with pytest.raises(SR.SignedRoleRefused) as exc:     # revert: the `role_hash != link.role_hash` line in signed_withhold
        SR.signed_withhold(signed, forged)
    assert exc.value.name == "link-role" and "verified link" in str(exc.value)


def test_the_agent_id_is_hashed_into_the_spec_hash():
    signed, link = chain()
    other = "ag-" + "3" * 32
    with pytest.raises(SR.SignedRoleRefused) as exc:     # both claim agent 3; the spec_hash was computed for AGENT
        SR.signed_withhold(dataclasses.replace(signed, agent_id=other), dataclasses.replace(link, agent_id=other))
    assert exc.value.name == "spec-hash"


def test_evidence_of_another_agent_than_the_link_names_derives_nothing():
    signed, link = chain()
    other = "ag-" + "3" * 32
    with pytest.raises(SR.SignedRoleRefused) as exc:     # revert: the `signed.agent_id != link.agent_id` line
        SR.signed_withhold(signed, dataclasses.replace(link, agent_id=other))
    assert exc.value.name == "link-agent"


def test_the_link_terms_come_from_a_grant_or_refuse_by_name():
    signed, link = chain()
    grant = {"agent_id": link.agent_id, "spec_hash": link.spec_hash, "role_hash": link.role_hash, "kind": "agent-link"}
    assert SR.SignedLink.of_grant(grant) == link
    for bad in (None, {}, {**grant, "role_hash": "c"}, {**grant, "agent_id": 1}):
        with pytest.raises(SR.SignedRoleRefused) as exc:
            SR.SignedLink.of_grant(bad)
        assert exc.value.name == "link"


# --- L3 W2 r1 MED (complement, deepseek): a lone surrogate is invalid text, never a transient failure ----------------------------

@pytest.mark.parametrize("member", ["spec", "role"])
def test_a_lone_surrogate_in_the_evidence_is_refused_as_invalid_text_at_construction(member):
    signed, _ = chain()
    with pytest.raises(ValueError) as exc:               # revert: the strict encode in SignedRole.__post_init__
        dataclasses.replace(signed, **{member: getattr(signed, member)[:-1] + '\ud800"}'})
    assert type(exc.value) is ValueError and str(exc.value) == f"signed.{member} is not UTF-8 text"


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
