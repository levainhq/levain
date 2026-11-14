"""S-A L3 r1 finding 2: a graduation policy or opt-in an earlier levain signed is a row on the cockpit page and the phone
(``grant_view``), with its instance and the passkeys that may revoke it, and the phone's passkey revokes either kind.
Before the fix ``grant_view`` dropped both (the README's passkey revoke was unreachable from the cockpit)."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import levain.authority as auth
from levain.authority import ReplayCache, anchor_path
from levain.cockpit.consent import utc_stamp
from levain.consent.grants import grant_view
from tests.test_authority_graduation_kind import LOW, STORE, TEST_GUARD, before_the_cut
from tests.test_grant_phone import ORIGIN, _now, world  # noqa: F401  (world: a fixture)

GP = "gp-0123456789ab"
OPT = "gpo-0123456789ab"


def _old_links(world) -> None:
    """The opt-in and one policy, appended as a levain from before the binding cut would."""
    policy = {"id": GP, "kind": "graduation", "actions": {"doc.append": dict(LOW)}, "lowest_posture": "ON_LOOP",
              "expires_at": utc_stamp(_now() + timedelta(days=20)), "triggers": None, "families": None,
              "store_id": STORE, "min_clean_fires": 20, "guard": [dict(e) for e in TEST_GUARD]}
    opt_in = {"id": OPT, "kind": "graduation-phone", "passkeys": [world.phone.fp]}
    conn = sqlite3.connect(world.root / "cockpit.db", isolation_level=None)
    try:
        with before_the_cut():
            for grant in (opt_in, policy):
                auth.sign_and_append(conn, {"op": "create_grant", "grant": grant}, signer_fp=world.lfp,
                                     sign=world.laptop_sign, clock=_now, cache=ReplayCache(),
                                     anchor=anchor_path(world.root))
    finally:
        conn.close()


def test_the_retired_kinds_are_listed_with_their_instance_and_signers(world) -> None:
    gid = world.grant("cg-0123456789ab")
    _old_links(world)
    rows = {r["id"]: r for r in grant_view(world.qdir, now=_now())["grants"]}
    assert set(rows) == {gid, GP, OPT}
    assert rows[GP]["kind"] == "graduation" and rows[GP]["state"] == "live" and rows[GP]["expires_at"]
    assert rows[OPT]["kind"] == "graduation-phone" and rows[OPT]["state"] == "live" and rows[OPT]["expires_at"] == ""
    for r in (rows[GP], rows[OPT]):
        assert r["instance"] and r["answered"]["count"] == 0 and r["way_back"] == ""
        assert r["revoke"].startswith("levain consent grant revoke")
    phone = {r["id"]: r for r in world.gp.listing()["grants"]}
    assert phone[GP]["signers"] == [world.phone.fp] and phone[OPT]["signers"] == [world.phone.fp]
    assert "revoke" not in phone[GP]


def test_the_phone_passkey_revokes_a_graduation_policy_and_the_opt_in(world) -> None:
    _old_links(world)
    for gid in (GP, OPT):
        c = world.gp.step(gid, "revoke-challenge", world.shown(gid))
        assert json.loads(c["line"]) ["grant_id"] == gid
        got = world.gp.step(gid, "revoke", world.phone.assertion(c["challenge"], origin=ORIGIN))
        assert got["revoked"] == gid
    conn = sqlite3.connect(world.root / "cockpit.db", isolation_level=None)
    try:
        grants = auth.snapshot(conn, cache=ReplayCache()).policy.grants      # a fresh replay of every link
    finally:
        conn.close()
    assert GP not in grants and OPT not in grants
    assert {r["id"] for r in grant_view(world.qdir, now=_now())["grants"]} == set()
