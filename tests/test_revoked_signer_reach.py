"""Row 190, round-1 review: the paths by which a grant a revoked key stopped could still act.

1. The graduation opt-in (its own section, below, once ported).
2. A change that is not a ``renew``. A ``loosen`` that extends a stopped grant (and adds another, so it computes to
   LOOSEN) would move the grant's current link to a held key and bring it back with its incarnation unchanged. The
   stop is absorbing: a change to a grant stopped in the predecessor does not move its current link, at append or at
   replay (a foreign writer's link included), so the grant stays stopped until it is revoked and created again.

Real logs through the replay (ssh-keygen laptop-presence keys A, B, C and WebAuthn passkey stand-ins), the shapes of
``tests/test_grant_stop.py``."""
from __future__ import annotations

import shutil
from datetime import timedelta

import pytest

import levain.authority as auth
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import GP, V1, consent, grad  # noqa: E402
from tests.test_grant_stop import Laptops  # noqa: E402

CG, CG2 = "cg-000000000001", "cg-000000000002"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def world(tmp_path) -> Laptops:
    return Laptops(tmp_path)


def _policy_with(state: auth.AuthoritySnapshot, **grants) -> dict:
    p = state.policy
    return {"raise_to": auth._thaw(p.raise_to), "timeouts": auth._thaw(p.timeouts),
            "grants": {**auth._thaw(p.grants), **grants}}


def test_graduation_links_fails_closed_on_a_snapshot_with_no_signer_list(world):
    from dataclasses import replace

    state = world.link({"op": "create_grant", "grant": grad(world)}, "a")
    assert auth.graduation_links(state, GP) == (world.fp("a"), world.fp("a"))
    assert auth.graduation_links(replace(state, link_signers=None), GP) is None


# --- 2: a stopped grant stays stopped whatever op changes it -----------------------------------------------------------

def _renew(world: Laptops, gid: str, days: int) -> dict:
    at = world.clock.now + timedelta(minutes=1)        # the stamp of the link World.link is about to write
    return {"op": "renew", "grant_id": gid, "expires_at": utc_stamp(at + timedelta(days=days)), "signed_at": utc_stamp(at)}


def _extended(state: auth.AuthoritySnapshot, world: Laptops, gid: str = CG, days: int = 3) -> dict:
    return {**auth._thaw(state.policy.grants[gid]), "expires_at": utc_stamp(world.clock.now + timedelta(days=days))}


def _stopped_current(world) -> auth.AuthoritySnapshot:
    """A creates consent grant CG, B renews it, B is revoked: CG stopped at its current link."""
    world.link({"op": "create_grant", "grant": consent(world, CG)}, "a")
    world.link(_renew(world, CG, 2), "b")
    state = world.revoke("b")
    assert auth.grant_stop_reason(state, CG) == auth.STOPPED_CURRENT
    return state


def test_a_loosen_that_extends_a_stopped_grant_and_adds_another_is_refused_at_append(world):
    """Codex's shape: the extension rides beside a new grant, so the link computes to LOOSEN, not RENEW."""
    state = _stopped_current(world)
    loosen = {"op": "loosen", "policy": _policy_with(state, **{CG: _extended(state, world), CG2: consent(world, CG2)})}
    assert auth.classify(state.policy, auth.StandingPolicy.from_dict(loosen["policy"])) == auth.LOOSEN
    said = world.refused(loosen, "a")
    assert f"grant {CG} stopped when {auth.STOPPED_CURRENT}; a loosen does not bring it back" in said
    assert world.state().seq == state.seq                            # nothing was written


def test_the_replay_keeps_a_stopped_grant_stopped_under_a_planted_loosen(world):
    """The same loosen a foreign writer planted past the append: the replay takes the link (refusing it would wedge
    the log) but the grant's current link does not move, so it stays stopped; the grant the link added is in force."""
    state = _stopped_current(world)
    db = world.plant({"op": "loosen", "policy": _policy_with(state, **{CG: _extended(state, world),
                                                                      CG2: consent(world, CG2)})}, "a", fmt=V1)
    after = world.state(db)
    assert after.seq == state.seq + 1
    assert after.grant_seqs[CG] == state.grant_seqs[CG]              # the current link stayed B's renew
    assert auth.grant_stop_reason(after, CG) == auth.STOPPED_CURRENT
    assert CG not in auth.grants_in_force(after) and CG2 in auth.grants_in_force(after)


def test_the_replay_keeps_a_stopped_grant_stopped_under_a_planted_renew(world):
    state = _stopped_current(world)
    db = world.plant(_renew(world, CG, 4), "a", fmt=V1)
    after = world.state(db)
    assert after.seq == state.seq + 1
    assert auth.grant_stop_reason(after, CG) == auth.STOPPED_CURRENT


def test_control_a_loosen_that_extends_a_live_grant_still_moves_its_current_link(world):
    """The bound touches only a grant stopped in the predecessor: with every signer active the same loosen makes A's
    link the grant's current one, as before."""
    world.link({"op": "create_grant", "grant": consent(world, CG)}, "a")
    state = world.link(_renew(world, CG, 2), "b")
    after = world.link({"op": "loosen", "policy": _policy_with(state, **{CG: _extended(state, world),
                                                                         CG2: consent(world, CG2)})}, "a")
    assert after.grant_seqs[CG] == after.seq
    assert auth.grant_signers(after, CG) == (world.fp("a"), world.fp("a"))
    assert auth.grant_stop_reason(after, CG) is None

