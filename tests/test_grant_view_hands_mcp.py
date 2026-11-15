"""The Grants tab and the phone list a hands MCP entry by its name, and the phone's passkey revoke ends a hands chat
(leg 6 piece 2). Before this the kind was passkey-revocable in the log (levain.authority) but no listing named it, so the
page could not show its instance and the phone's challenge was refused ``grant_changed``. The passkey is the grant-phone
tests' software authenticator; the chat's own liveness read (``hands_mcp_login.launch_check``, what ``hands_chat`` calls
every tick) is run against the real log. No hands account, no vendor, no phone."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone

import pytest

from levain import authority as auth
from levain import hands_mcp as hm
from levain.autonomic.confirm import DEFAULT_SSH_KEYGEN
from levain.cockpit.keys import _append, _journal
from levain.consent.grants import grant_view
from levain.firing import hands_mcp_login as hml
from tests.test_grant_phone import ORIGIN, _now, world  # noqa: F401  (a fixture)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _register(world, name="linear") -> dict:
    e = hm.build_entry(name=name, transport="remote", identity_kind="dedicated",
                       expires_at=hm.lease_expiry(datetime.now(timezone.utc)), url="http://127.0.0.1:49999/mcp")
    _append(_journal(world.root), {"op": "create_grant", "grant": e}, signer_fp=world.lfp, sign=world.laptop_sign,
            clock=_now, ssh_keygen=DEFAULT_SSH_KEYGEN)
    return e


def test_a_hands_entry_is_listed_by_its_name_with_its_revoke(world):
    e = _register(world)
    row = {r["id"]: r for r in grant_view(world.qdir, now=_now())["grants"]}[e["id"]]
    assert row["kind"] == auth.HANDS_MCP_KIND and row["state"] == "live" and row["instance"]
    assert row["agent"] == "hands server linear" and row["workspace"] == e["url"]
    assert row["revoke"].startswith("levain consent grant revoke") and e["id"] in row["revoke"]
    assert row["answered"]["count"] == 0
    listed = {r["id"]: r for r in world.gp.listing()["grants"]}[e["id"]]
    assert listed["signers"] == [world.phone.fp] and "revoke" not in listed


def test_the_phone_revokes_a_hands_entry_and_the_running_chat_sees_it_gone(world):
    e = _register(world)
    born: dict = {}
    live, _ = hml.load_registry(world.root, _now(), born=born)
    assert [x["id"] for x in live] == [e["id"]]
    chat = hml.LaunchFiles("claude.json", "codex.toml", ["linear"], [], {}, ids=[e["id"]], born=born,
                           cockpit_root=world.root)
    assert hml.launch_check(chat) is None
    c = world.gp.step(e["id"], "revoke-challenge", world.shown(e["id"]))
    got = world.gp.step(e["id"], "revoke", world.phone.assertion(c["challenge"], origin=ORIGIN))
    assert got["revoked"] == e["id"]
    assert hml.launch_check(chat) == (hml.GONE, e["id"], "was revoked") # hands_chat ends the chat with HANDS_REVOKED_EXIT on this
    assert e["id"] not in {r["id"] for r in grant_view(world.qdir, now=_now())["grants"]}
