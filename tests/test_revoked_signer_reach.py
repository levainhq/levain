"""Row 190, round-1 review: the paths by which a grant a revoked key stopped could still act.

1. The graduation opt-in. A passkey creates a graduation policy only while the live opt-in names it, and the opt-in
   is read IN FORCE (:func:`levain.authority.grant_stop_reason`). This levain appends no passkey create at all (the
   binding cut); a create a log already holds, made while the opt-in was stopped, is taken by the replay (refusing it
   would wedge the log) and held stopped (``grant_born_stopped``, :data:`levain.authority.STOPPED_OPT_IN`).
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
from tests.test_authority_graduation_kind import GP, GP2, OPT, V1, V2, consent, grad, opt_in, renew  # noqa: E402
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


# --- 1: the opt-in a passkey's create reads is the opt-in in force ----------------------------------------------------

def test_control_a_passkey_the_live_opt_in_names_creates_a_graduation_policy(world):
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")}, "b")
    created = world.link({"op": "create_grant", "grant": grad(world)}, "pk")
    assert GP in auth.grants_in_force(created)


def test_an_opt_in_whose_signer_was_revoked_admits_no_new_passkey_create(world):
    """A new link: this levain refuses every passkey create (the binding cut), so the stopped opt-in admits none."""
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")}, "b")
    stopped = world.revoke("b")
    assert auth.grant_stop_reason(stopped, OPT) == auth.STOPPED_BORN
    said = world.refused({"op": "create_grant", "grant": grad(world)}, "pk", today=True)
    assert "never a renew and never a create" in said
    assert world.state().seq == stopped.seq                          # nothing was written


def _covering(state: auth.AuthoritySnapshot) -> list[str]:
    from levain.graduation_policy import graduation_policies

    return [p.grant_id for p in graduation_policies(state)]


def _stored_create_after_the_stop(world: Laptops):
    """A log an older levain (or a foreign writer) left: opt-in signed by B, B revoked, then a passkey create of GP no
    append of this version judged, written raw into the world's own log (so later appends extend it). Returns
    ``(stopped, db)``."""
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")}, "b")
    stopped = world.revoke("b")
    return stopped, world.plant({"op": "create_grant", "grant": grad(world)}, "pk", fmt=auth.FORMAT_V3, db=world.db)


def test_the_replay_takes_a_stored_passkey_create_after_the_opt_in_stopped_and_keeps_it_out_of_force(world):
    """L3 round 2 (codex + deepseek HIGH, afc259645): before it, main took this link and the policy it created was IN
    FORCE (the opt-in was read raw), and the branch's first fix refused the link, so the WHOLE authority was
    unavailable. The replay takes the link (refusing it wedges the log) and the policy it created answers nothing."""
    stopped, db = _stored_create_after_the_stop(world)
    after = world.state(db)                                          # the replay completes
    assert after.seq == stopped.seq + 1 and GP in after.policy.grants
    assert auth.grant_stop_reason(after, GP) == auth.STOPPED_OPT_IN
    assert GP not in auth.grants_in_force(after)
    assert GP not in _covering(after)                                # the engine's reader covers nothing with it
    assert auth.grants_stopped_by(after, world.fp("pk")) == []       # stopped before any revoke: never named by one
    # a NEW create after the revoke is still refused at append (item 1)
    said = world.refused({"op": "create_grant", "grant": grad(world, GP2)}, "pk", today=True)
    assert "never a renew and never a create" in said


@pytest.mark.parametrize("renewer, fmt", [("pk", V2), ("a", V1)])
def test_a_stored_renew_does_not_bring_the_stored_create_back(world, renewer, fmt):
    """The stop is absorbing: a renew already in the log is taken and the policy stays stopped; a new renew is
    refused, by the passkey that created it or by a held laptop key (judged as a levain from before the binding cut
    appends, so the row-190 rule is what refuses it)."""
    _stored_create_after_the_stop(world)
    after = world.state(world.plant(renew(world), renewer, fmt=fmt, db=world.db))
    assert auth.grant_stop_reason(after, GP) == auth.STOPPED_OPT_IN and GP not in auth.grants_in_force(after)
    said = world.refused(renew(world), renewer)
    assert f"grant {GP} stopped when {auth.STOPPED_OPT_IN}; a renew does not bring it back" in said


def test_a_fresh_opt_in_does_not_bring_the_stored_create_back_and_only_a_new_incarnation_answers(world):
    """A later historical change upstream (the stopped opt-in revoked, a new one a held key signs) leaves the stored
    create stopped; revoking it and creating it again under the live opt-in is the one way back."""
    _stored_create_after_the_stop(world)
    world.link({"op": "revoke_grant", "grant_id": OPT}, "a")
    relit = world.link({"op": "create_grant", "grant": opt_in(world, "pk")}, "a")
    assert auth.grant_stop_reason(relit, OPT) is None
    assert auth.grant_stop_reason(relit, GP) == auth.STOPPED_OPT_IN
    world.link({"op": "revoke_grant", "grant_id": GP}, "a")
    again = world.link({"op": "create_grant", "grant": grad(world)}, "pk")
    assert auth.grant_stop_reason(again, GP) is None and GP in auth.grants_in_force(again)
    assert GP in _covering(again)


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


def test_the_refusal_shows_a_hostile_grant_id_through_markers(world):
    hostile = "cg-\x1b[2Jx"
    world.link({"op": "create_grant", "grant": consent(world, hostile)}, "a")
    world.link(_renew(world, hostile, 2), "b")
    world.revoke("b")
    said = world.refused(_renew(world, hostile, 4), "a")
    assert "\x1b" not in said and "grant cg-⟨U+001B⟩[2Jx stopped when" in said


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

