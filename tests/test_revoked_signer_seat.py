"""Levain board row 190, the seat kinds: revoking a signing key stops the seat-root lease and the seat-egress grant it
signed (``levain.authority.grants_in_force``), at the use a launch or a push makes of them.

A grant is stopped when the key that signed its born link, or the key that last renewed it, was revoked. It stays in
the policy (it can be revoked, listed, and it still counts against the one-live append rule), and a launch or a push
reads it as not there, in words that say why. Throwaway ed25519 keys, a throwaway cockpit, a real local git and bare
remote (the rigs of ``test_seat_lease`` and ``test_seat_push``). Three laptop-presence keys are enrolled: A (the
world's own) and B sign and renew, C signs nothing of the grant.
"""
from __future__ import annotations

import io
import os
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
import levain.authority as auth  # noqa: E402
from levain import seat_lease as sl  # noqa: E402
from levain import seat_push as sp  # noqa: E402
from tests.test_consent_queue import Key  # noqa: E402
from tests.test_seat_lease import rig  # noqa: E402,F401  (rig is a fixture)
from tests.test_seat_push import Egress  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("git") is None,
                                reason="needs ssh-keygen and git")

LEASE_STOPPED = "No seat can launch: the seat-root lease stopped when {why} ({gid}). Revoke it, then sign a new one: "
EGRESS_STOPPED = "nothing is pushed: the seat-egress grant stopped when {why} ({gid}); commits stay in the clone"
BORN = auth.STOPPED_BORN          # "the key that signed it was revoked"
CURRENT = auth.STOPPED_CURRENT    # "the key that last renewed it was revoked"


@pytest.fixture
def three(rig):
    """The rig's world with keys ``a`` (its laptop key), ``b`` and ``c`` all laptop-presence, so any one can be
    revoked without leaving the log without a laptop key."""
    world = rig.world
    world.keys["a"] = world.keys["laptop"]
    for name in ("b", "c"):
        k = Key(rig.tmp / "keys", name)
        world.link({"op": "enrol", "fingerprint": k.fp, "public_key": k.public_key, "class": "laptop-presence",
                    "custody": "passphrase"})
        world.keys[name] = (k.fp, lambda m, k=k: k.sign(m, auth.NAMESPACE))
    return world


def _lease(rig, signer: str) -> dict:
    rig.world.clock.now += timedelta(minutes=1)
    fp, sign = rig.world.keys[signer]
    return sl.create_lease(rig.world.root, lanes_parent=rig.lanes, returns_parent=rig.returns, key_dir=rig.key_dir,
                           signer_fp=fp, sign=sign, clock=rig.world.clock)


def _renew(rig, grant_id: str, signer: str, *, egress: bool = False) -> None:
    rig.world.clock.now += timedelta(minutes=1)
    fp, sign = rig.world.keys[signer]
    fn = sp.renew_egress if egress else sl.renew_lease
    fn(rig.world.root, grant_id, signer_fp=fp, sign=sign, clock=rig.world.clock)


def _revoke_key(world, victim: str, by: str) -> None:
    world.link({"op": "revoke_key", "fingerprint": world.fp(victim)}, signer=by)


# --- the seat-root lease, at the launch --------------------------------------------------------------------------

def test_t12_control_a_lease_whose_signers_are_active_launches(rig, three) -> None:
    lease = _lease(rig, "a")
    _renew(rig, lease["id"], "b")
    assert auth.grant_stop_reason(three.state(), lease["id"]) is None
    assert Path(rig.launch("lane-a").profile.clone).is_dir()


def test_t12_a_lease_whose_last_renewer_was_revoked_refuses_the_launch_in_the_stopped_words(rig, three) -> None:
    lease = _lease(rig, "a")
    _renew(rig, lease["id"], "b")
    _revoke_key(three, "b", by="a")
    with pytest.raises(sl.SeatLaunchRefused) as exc:
        rig.launch("lane-b")
    assert str(exc.value) == LEASE_STOPPED.format(why=CURRENT, gid=lease["id"]) + sl.SIGN_COMMAND
    assert not (rig.lanes / "lane-b").exists()                          # refused before anything was minted
    assert sl.live_lease(three.state(), three.clock.now) is None


def test_t12_a_lease_whose_creator_was_revoked_refuses_the_launch_though_a_held_key_renewed_it(rig, three) -> None:
    lease = _lease(rig, "a")
    _renew(rig, lease["id"], "b")
    _revoke_key(three, "a", by="b")
    with pytest.raises(sl.SeatLaunchRefused) as exc:
        rig.launch("lane-b")
    assert str(exc.value) == LEASE_STOPPED.format(why=BORN, gid=lease["id"]) + sl.SIGN_COMMAND
    assert not (rig.lanes / "lane-b").exists()


def test_t12_a_key_that_signed_nothing_of_the_lease_revoked_leaves_it_launching(rig, three) -> None:
    lease = _lease(rig, "a")
    _renew(rig, lease["id"], "b")
    _revoke_key(three, "c", by="a")
    assert auth.grant_stop_reason(three.state(), lease["id"]) is None
    assert Path(rig.launch("lane-a").profile.clone).is_dir()


def test_t12_a_stopped_lease_still_blocks_a_second_one_until_it_is_revoked(rig, three) -> None:
    """The append rule reads the raw policy: stopping a lease is not removing it, so a new one waits for the revoke the
    refusal words ask for."""
    from levain.consent.grants import revoke_grant
    from levain.cockpit.keys import KeyError_
    lease = _lease(rig, "a")
    _revoke_key(three, "a", by="b")
    assert lease["id"] in three.state().policy.grants and sl.live_lease(three.state(), three.clock.now) is None
    with pytest.raises((KeyError_, auth.AuthorityError)) as exc:
        _lease(rig, "b")
    assert "seat-root lease" in str(exc.value)
    three.clock.now += timedelta(minutes=1)
    fp, sign = three.keys["b"]
    revoke_grant(three.root, lease["id"], signer_fp=fp, sign=sign, clock=three.clock)
    second = _lease(rig, "b")
    assert Path(rig.launch("lane-a").profile.clone).is_dir() and second["id"] != lease["id"]


# --- the seat-egress grant, at the push --------------------------------------------------------------------------

@pytest.fixture
def eg(rig, three, tmp_path, monkeypatch):
    return Egress(rig, tmp_path, monkeypatch)


def _egress(eg, signer: str) -> dict:
    eg.world.clock.now += timedelta(minutes=1)
    fp, sign = eg.world.keys[signer]
    return sp.create_egress(eg.world.root, lanes_parent=eg.rig.lanes, push_remote=eg.url, signer_fp=fp, sign=sign,
                            clock=eg.world.clock)


def _judge(eg, clone):
    return sp.egress_refusal(eg.world.root, eg.rig.key_dir, now=eg.now)(
        path=os.path.realpath(clone), remote=eg.url, branch="seat/lane-a")


def _hook(eg, clone) -> int:
    return sp.run_hook("Stop", "{}", clone=clone, key_dir=eg.rig.key_dir, cockpit_root=eg.world.root, home=eg.home,
                       err=io.StringIO(), now=eg.now)


def _lane_under_a_lease_no_test_key_revokes(eg):
    """The lease is signed by C, which no case below revokes, so the lane launches and the egress grant is the only
    thing a case stops."""
    _lease(eg.rig, "c")
    return eg.lane()


def test_t12_control_an_egress_grant_whose_signers_are_active_pushes(eg) -> None:
    _plan, clone = _lane_under_a_lease_no_test_key_revokes(eg)
    grant = _egress(eg, "a")
    _renew(eg.rig, grant["id"], "b", egress=True)
    tip = eg.commit(clone, "one")
    assert _judge(eg, clone)[0] == grant["id"]
    assert _hook(eg, clone) == 0 and eg.remote_ref("refs/heads/seat/lane-a") == tip


def test_t12_an_egress_grant_whose_last_renewer_was_revoked_pushes_nothing_in_the_stopped_words(eg) -> None:
    _plan, clone = _lane_under_a_lease_no_test_key_revokes(eg)
    grant = _egress(eg, "a")
    _renew(eg.rig, grant["id"], "b", egress=True)
    eg.commit(clone, "one")
    _revoke_key(eg.world, "b", by="a")
    assert _judge(eg, clone) == EGRESS_STOPPED.format(why=CURRENT, gid=grant["id"])
    assert _hook(eg, clone) == 0 and eg.remote_ref("refs/heads/seat/lane-a") is None
    assert eg.log()[-1]["outcome"] == "refused" and "stopped when" in eg.log()[-1]["detail"]


def test_t12_an_egress_grant_whose_creator_was_revoked_pushes_nothing_though_a_held_key_renewed_it(eg) -> None:
    _plan, clone = _lane_under_a_lease_no_test_key_revokes(eg)
    grant = _egress(eg, "a")
    _renew(eg.rig, grant["id"], "b", egress=True)
    eg.commit(clone, "one")
    _revoke_key(eg.world, "a", by="b")
    assert _judge(eg, clone) == EGRESS_STOPPED.format(why=BORN, gid=grant["id"])
    assert _hook(eg, clone) == 0 and eg.remote_ref("refs/heads/seat/lane-a") is None


def test_t12_a_key_that_signed_nothing_of_the_egress_grant_revoked_leaves_it_pushing(eg) -> None:
    _plan, clone = _lane_under_a_lease_no_test_key_revokes(eg)
    grant = _egress(eg, "a")
    tip = eg.commit(clone, "one")
    _revoke_key(eg.world, "b", by="a")
    assert _judge(eg, clone)[0] == grant["id"]
    assert _hook(eg, clone) == 0 and eg.remote_ref("refs/heads/seat/lane-a") == tip


def test_t12_a_stopped_egress_grant_still_blocks_a_second_one_until_it_is_revoked(eg) -> None:
    from levain.cockpit.keys import KeyError_
    _lane_under_a_lease_no_test_key_revokes(eg)
    grant = _egress(eg, "a")
    _revoke_key(eg.world, "a", by="b")
    assert grant["id"] in eg.world.state().policy.grants and sp.live_egress(eg.world.state(), eg.now()) is None
    with pytest.raises((KeyError_, auth.AuthorityError)) as exc:
        _egress(eg, "b")
    assert "seat-egress grant" in str(exc.value)


def test_t12_a_launch_under_a_stopped_egress_grant_builds_the_profile_without_one(eg) -> None:
    """The launch reads egress through ``live_egress``: a stopped grant is no grant, so the seat gets today's profile
    (no push hooks), not a refusal and not the stopped grant's remote."""
    _lease(eg.rig, "c")
    _egress(eg, "a")
    _revoke_key(eg.world, "a", by="b")
    plan = eg.rig.launch("lane-z")
    assert plan.egress_grant is None
