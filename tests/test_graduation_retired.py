"""The binding cut of 1114 at the authority log: a graduation policy is no longer created or renewed and the opt-in is
no longer signed (append refuses each, a passkey's renew and create included), while a log an earlier levain wrote
replays whole, and the one thing left to do with either kind is revoke it."""
from __future__ import annotations

import pytest

import levain.authority as auth
from levain.cli import main
from tests.test_authority_graduation_kind import GP, GP2, OPT, World, grad, opt_in, renew


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def test_a_graduation_create_is_refused_at_append(world):
    why = world.refused({"op": "create_grant", "grant": grad(world)}, today=True)
    assert "refused at sign" in why and "a binding does not graduate" in why
    assert GP not in world.state().policy.grants


def test_a_graduation_create_inside_a_loosen_is_refused_at_append(world):
    policy = world.state().policy.as_dict()
    policy["grants"][GP] = grad(world)
    assert "refused at sign" in world.refused({"op": "loosen", "policy": policy}, today=True)


def test_the_opt_in_is_refused_at_append(world):
    assert "the graduation opt-in is refused at sign" in world.refused(
        {"op": "create_grant", "grant": opt_in(world, "pk")}, today=True)


def test_the_opt_in_inside_a_loosen_is_refused_at_append(world):
    policy = world.state().policy.as_dict()
    policy["grants"][OPT] = opt_in(world, "pk")
    assert "the graduation opt-in is refused at sign" in world.refused({"op": "loosen", "policy": policy}, today=True)


def test_a_passkey_loosen_carrying_a_policy_is_refused_at_append(world):
    policy = world.state().policy.as_dict()
    policy["grants"][GP] = grad(world)
    assert "never another authority link" in world.refused({"op": "loosen", "policy": policy}, "pk", today=True)


def test_an_old_policy_without_a_guard_replays_covers_nothing_new_and_is_revoked(world):
    from tests.autonomic_test_only_coverage import NO_GUARD
    # a policy from before the guard existed: a /1 link no append of today could write, planted
    world.plant({"op": "create_grant", "grant": grad(world, guard=NO_GUARD)}, db=world.db)
    state = world.state()
    assert GP in state.policy.grants and "guard" not in state.policy.grants[GP]
    after = world.link({"op": "revoke_grant", "grant_id": GP}, today=True)
    assert GP not in after.policy.grants


def test_a_graduation_renew_is_refused_at_append(world):
    world.link({"op": "create_grant", "grant": grad(world)})            # signed by an earlier levain
    assert "is not renewed" in world.refused(renew(world), today=True)


def test_a_passkey_no_longer_renews_or_creates_a_graduation_policy(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    world.link({"op": "create_grant", "grant": grad(world)})
    assert "never a renew and never a create" in world.refused(renew(world), "pk", today=True)
    assert "never a renew and never a create" in world.refused(
        {"op": "create_grant", "grant": grad(world, GP2)}, "pk", today=True)


def test_a_log_holding_both_kinds_and_the_passkey_links_replays_whole(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    world.link({"op": "create_grant", "grant": grad(world)}, "pk")      # a passkey's create, as the opt-in named it
    world.link(renew(world), "pk")                                       # a passkey's renew
    world.link({"op": "create_grant", "grant": grad(world, GP2)})
    state = world.state()                                                # the replay under today's rules
    assert {GP, GP2, OPT} <= set(state.policy.grants)


def test_the_two_kinds_must_stay_known_or_a_log_holding_an_opt_in_stops_replaying(world, monkeypatch):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    assert OPT in world.state().policy.grants
    kinds = {k: v for k, v in auth._GRANT_KINDS.items() if k != auth.GRADUATION_PHONE_KIND}
    monkeypatch.setattr(auth, "_GRANT_KINDS", kinds)
    # an unknown kind is read as inert, whose shape needs expires_at, which the opt-in has none of
    with pytest.raises(auth.AuthorityError, match="grant.expires_at is not a timestamp"):
        world.state()


@pytest.mark.parametrize("name", ["policy", "graduate", "pause"])
def test_the_binding_verbs_are_gone(name, capsys):
    with pytest.raises(SystemExit):
        main(["binding", name])
    assert "invalid choice: 'binding'" in capsys.readouterr().err


def test_either_kind_can_still_be_revoked(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")})
    world.link({"op": "create_grant", "grant": grad(world)})
    after = world.link({"op": "revoke_grant", "grant_id": GP}, today=True)
    assert GP not in after.policy.grants
    after = world.link({"op": "revoke_grant", "grant_id": OPT}, "pk", today=True)   # a passkey may revoke too
    assert OPT not in after.policy.grants
