"""The Grants tab lists a seat-egress grant, so Phill's one-tap revoke exists (PATHZERO Z2a)."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

from levain import authority as auth
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.consent import utc_stamp
from levain.cockpit.keys import _append, _journal
from levain.consent.grants import grant_view, revoke_grant
from tests.test_grant_phone import _now, world  # noqa: F401  (a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _egress(world, tmp_path, days: int = 14) -> dict:
    g = {"id": "se-000000000001", "kind": auth.SEAT_EGRESS_KIND, "agent": "claude_code",
         "lanes_parent": str(tmp_path / "lanes"), "push_remote": "file:///srv/r.git", "allowed_domains": [],
         "expires_at": utc_stamp(_now() + timedelta(days=days))}
    _append(_journal(world.root), {"op": "create_grant", "grant": g}, signer_fp=world.lfp, sign=world.laptop_sign,
            clock=_now, ssh_keygen=DEFAULT_SSH_KEYGEN)
    return g


def test_a_live_egress_grant_is_listed_with_its_revoke_and_a_revoked_one_is_not(world, tmp_path):
    g = _egress(world, tmp_path)
    rows = {r["id"]: r for r in grant_view(world.qdir, now=_now())["grants"]}
    row = rows[g["id"]]
    assert row["kind"] == auth.SEAT_EGRESS_KIND and row["state"] == "live" and row["instance"]
    assert row["revoke"].startswith("levain consent grant revoke") and g["id"] in row["revoke"]
    assert row["workspace"] == g["push_remote"] and row["answered"]["count"] == 0
    revoke_grant(world.root, g["id"], signer_fp=world.lfp, sign=world.laptop_sign, clock=_now)
    assert g["id"] not in {r["id"] for r in grant_view(world.qdir, now=_now())["grants"]}
