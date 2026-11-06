"""The ``seat-root-lease`` grant kind in the authority log (PATHZERO Z1d, ruling D2 a). Real logs: ssh-keygen keys and
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

KEY = "a" * 64


def lease(world: World, tmp_path, n: int = 1, days: int = 14, **over) -> dict:
    g = {"id": f"sl-{n:012x}", "kind": "seat-root-lease", "agent": "claude_code",
         "lanes_parent": str(tmp_path / "lanes"), "returns_parent": str(tmp_path / "returns"),
         "launcher_key_sha256": KEY, "expires_at": utc_stamp(world.clock.now + timedelta(days=days))}
    g.update(over)
    return g


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


def test_a_lease_is_signed_replays_as_its_kind_and_is_neither_consent_nor_inert(world, tmp_path):
    g = lease(world, tmp_path)
    world.link({"op": "create_grant", "grant": g})
    held = world.state().policy.grants[g["id"]]
    assert held["kind"] == auth.SEAT_LEASE_KIND == "seat-root-lease"
    assert not auth.consent_grant(held) and not auth.inert_grant(held)


def test_the_lease_kind_is_in_the_passkey_revocable_set_and_a_passkey_revoke_is_prepared_and_lands(world, tmp_path):
    """The cockpit Grants tab's revoke challenge goes through ``consent.grants.prepare_revoke``, which holds a grant to
    ``passkey_revocable``: the lease passes it, and a passkey-signed revoke removes the lease."""
    from levain.cockpit.firejournal import CockpitDB
    from levain.consent.grants import append_revoke, prepare_revoke
    CockpitDB(world.root).set_install_class("reported")
    g = lease(world, tmp_path)
    world.link({"op": "create_grant", "grant": g})
    assert auth.passkey_revocable(world.state().policy.grants[g["id"]])
    link = prepare_revoke(world.root, g["id"], signer_fp=world.fp("pk"), clock=world.clock)
    append_revoke(world.root, link, world.keys["pk"][1](link.message), clock=world.clock)
    assert g["id"] not in world.state().policy.grants


def test_a_lease_longer_than_14_days_is_refused_at_append(world, tmp_path):
    assert "14 days" in world.refused({"op": "create_grant", "grant": lease(world, tmp_path, days=15)})


@pytest.mark.parametrize("over,said", [
    ({"agent": "codex"}, "claude_code"),
    ({"lanes_parent": "relative/dir"}, "absolute"),
    ({"launcher_key_sha256": "zz"}, "64 lowercase hex"),
    ({"id": "sl-XYZ"}, "sl- and 12"),
    ({"extra": 1}, "exactly"),
])
def test_a_malformed_lease_is_refused(world, tmp_path, over, said):
    assert said in world.refused({"op": "create_grant", "grant": lease(world, tmp_path, **over)})


def test_a_parent_naming_agent_config_or_under_claude_or_overlapping_is_refused(world, tmp_path):
    for over, said in (({"lanes_parent": str(tmp_path / ".claude" / "x")}, "agent-config"),
                       ({"returns_parent": str(tmp_path / "lanes" / "inner")}, "contain each other"),
                       ({"returns_parent": str(tmp_path / "lanes")}, "contain each other")):
        assert said in world.refused({"op": "create_grant", "grant": lease(world, tmp_path, **over)})


def test_terms_are_fixed_and_only_a_renew_moves_the_expiry_by_at_most_14_days(world, tmp_path):
    g = lease(world, tmp_path, days=1)
    world.link({"op": "create_grant", "grant": g})
    now = world.clock.now + timedelta(minutes=1)
    ok = {"op": "renew", "grant_id": g["id"], "expires_at": utc_stamp(now + timedelta(days=14)),
          "signed_at": utc_stamp(now)}
    world.link(ok)
    assert world.state().policy.grants[g["id"]]["expires_at"] == ok["expires_at"]
    late = world.clock.now + timedelta(minutes=1)
    assert "14 days" in world.refused({"op": "renew", "grant_id": g["id"],
                                       "expires_at": utc_stamp(late + timedelta(days=20)), "signed_at": utc_stamp(late)})
    other = {**world.state().policy.grants[g["id"]], "lanes_parent": str(tmp_path / "elsewhere")}
    assert world.refused({"op": "loosen", "policy": {"grants": {g["id"]: other}}})


def test_a_second_live_lease_is_refused_until_the_first_is_revoked(world, tmp_path):
    world.link({"op": "create_grant", "grant": lease(world, tmp_path, 1)})
    assert "one live seat-root lease" in world.refused({"op": "create_grant", "grant": lease(world, tmp_path, 2)})
    world.link({"op": "revoke_grant", "grant_id": "sl-000000000001"})
    world.link({"op": "create_grant", "grant": lease(world, tmp_path, 2)})
