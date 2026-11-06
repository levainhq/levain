"""The ``seat-egress`` grant kind in the authority log (PATHZERO Z2a, ruling H2 a; Q1 stays Phill's: the grant is
REQUIRED for any push and carries no branch, so it can only ever cover lane branches). Real logs: ssh-keygen keys and
WebAuthn stand-ins through the replay (the ``World`` of the graduation kind's tests)."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import World  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def egress(world: World, tmp_path, n: int = 1, days: int = 14, **over) -> dict:
    g = {"id": f"se-{n:012x}", "kind": "seat-egress", "agent": "claude_code", "lanes_parent": str(tmp_path / "lanes"),
         "push_remote": "file:///srv/r.git", "allowed_domains": ["api.example.com"],
         "expires_at": utc_stamp(world.clock.now + timedelta(days=days))}
    g.update(over)
    return g


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def test_a_grant_is_signed_replays_as_its_kind_and_is_neither_consent_nor_inert(world, tmp_path):
    g = egress(world, tmp_path)
    world.link({"op": "create_grant", "grant": g})
    held = world.state().policy.grants[g["id"]]
    assert held["kind"] == auth.SEAT_EGRESS_KIND == "seat-egress"
    assert not auth.consent_grant(held) and not auth.inert_grant(held)
    assert set(held) == {"id", "kind", "agent", "lanes_parent", "push_remote", "allowed_domains", "expires_at"}


def test_the_grant_has_no_branch_field_so_it_cannot_name_main(world, tmp_path):
    """Q1 is unanswered: the build is lane-branch-only BY SHAPE. A branch (or any other) field is refused."""
    for extra in ("branch", "branches", "ref"):
        said = world.refused({"op": "create_grant", "grant": egress(world, tmp_path, **{extra: "main"})})
        assert "exactly" in said, extra


def test_the_kind_is_passkey_revocable_and_a_passkey_revoke_lands(world, tmp_path):
    from levain.cockpit.firejournal import CockpitDB
    from levain.consent.grants import append_revoke, prepare_revoke
    CockpitDB(world.root).set_install_class("reported")
    g = egress(world, tmp_path)
    world.link({"op": "create_grant", "grant": g})
    assert auth.passkey_revocable(world.state().policy.grants[g["id"]])
    link = prepare_revoke(world.root, g["id"], signer_fp=world.fp("pk"), clock=world.clock)
    append_revoke(world.root, link, world.keys["pk"][1](link.message), clock=world.clock)
    assert g["id"] not in world.state().policy.grants


def test_a_grant_longer_than_14_days_is_refused_at_append(world, tmp_path):
    assert "14 days" in world.refused({"op": "create_grant", "grant": egress(world, tmp_path, days=15)})


@pytest.mark.parametrize("over,said", [
    ({"agent": "codex"}, "claude_code"),
    ({"lanes_parent": "relative/dir"}, "absolute"),
    ({"id": "se-XYZ"}, "se- and 12"),
    ({"id": "sl-000000000001"}, "se- and 12"),
    ({"extra": 1}, "exactly"),
    ({"allowed_domains": "api.example.com"}, "list"),
    ({"allowed_domains": ["*.example.com"]}, "host name"),
    ({"allowed_domains": ["10.0.0.1"]}, "host name"),
    ({"allowed_domains": ["host.example.com:443"]}, "host name"),
    ({"allowed_domains": ["Api.Example.com"]}, "host name"),
    ({"allowed_domains": ["a.example.com", "a.example.com"]}, "repeats"),
    ({"allowed_domains": [f"h{i}.example.com" for i in range(17)]}, "at most 16"),
    ({"push_remote": ""}, "push_remote"),
])
def test_a_malformed_grant_is_refused(world, tmp_path, over, said):
    assert said in world.refused({"op": "create_grant", "grant": egress(world, tmp_path, **over)})


@pytest.mark.parametrize("remote,said", [
    ("FILE:///srv/r.git/", "not canonical"),
    ("ext::sh -c x", "push_remote"),
    ("http://h.example/x", "push_remote"),
    ("/abs/path", "push_remote"),
])
def test_a_remote_that_is_not_canonical_or_not_pushable_is_refused_at_append(world, tmp_path, remote, said):
    assert said in world.refused({"op": "create_grant", "grant": egress(world, tmp_path, push_remote=remote)})


def test_a_lanes_parent_in_agent_config_or_under_claude_is_refused(world, tmp_path):
    for over, said in (({"lanes_parent": str(tmp_path / ".claude" / "x")}, "agent-config"),
                       ({"lanes_parent": str(tmp_path / ".git" / "x")}, "agent-config")):
        assert said in world.refused({"op": "create_grant", "grant": egress(world, tmp_path, **over)})


def test_terms_are_fixed_and_only_a_renew_moves_the_expiry_by_at_most_14_days(world, tmp_path):
    g = egress(world, tmp_path, days=1)
    world.link({"op": "create_grant", "grant": g})
    now = world.clock.now + timedelta(minutes=1)
    ok = {"op": "renew", "grant_id": g["id"], "expires_at": utc_stamp(now + timedelta(days=14)),
          "signed_at": utc_stamp(now)}
    world.link(ok)
    assert world.state().policy.grants[g["id"]]["expires_at"] == ok["expires_at"]
    late = world.clock.now + timedelta(minutes=1)
    assert "14 days" in world.refused({"op": "renew", "grant_id": g["id"],
                                       "expires_at": utc_stamp(late + timedelta(days=20)), "signed_at": utc_stamp(late)})
    held = world.state().policy.grants[g["id"]]
    for field, value in (("push_remote", "file:///srv/other.git"), ("lanes_parent", str(tmp_path / "elsewhere")),
                         ("allowed_domains", ["evil.example.com"])):
        assert world.refused({"op": "loosen", "policy": {"grants": {g["id"]: {**held, field: value}}}}), field


def test_a_second_live_grant_is_refused_until_the_first_is_revoked(world, tmp_path):
    world.link({"op": "create_grant", "grant": egress(world, tmp_path, 1)})
    assert "one live seat-egress grant" in world.refused({"op": "create_grant", "grant": egress(world, tmp_path, 2)})
    world.link({"op": "revoke_grant", "grant_id": "se-000000000001"})
    world.link({"op": "create_grant", "grant": egress(world, tmp_path, 2)})


def test_the_lease_unique_rule_still_judges_leases_alone(world, tmp_path):
    """The shared helper keys on the kind: a live egress grant does not count as a second live lease and back."""
    lease = {"id": "sl-000000000001", "kind": "seat-root-lease", "agent": "claude_code",
             "lanes_parent": str(tmp_path / "lanes"), "returns_parent": str(tmp_path / "returns"),
             "launcher_key_sha256": "a" * 64, "expires_at": utc_stamp(world.clock.now + timedelta(days=14))}
    world.link({"op": "create_grant", "grant": egress(world, tmp_path)})
    world.link({"op": "create_grant", "grant": lease})
    assert "one live seat-root lease" in world.refused(
        {"op": "create_grant", "grant": {**lease, "id": "sl-000000000002"}})
