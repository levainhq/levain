"""Row 190, the authority half: a standing grant is stopped at USE when the key that signed its born link, or the key
that signed its current link, has been revoked since (:func:`levain.authority.grant_stop_reason`).

Real logs (ssh-keygen laptop-presence keys A, B and C, WebAuthn stand-ins for the display-phone class) through the
replay. The helpers read a snapshot alone: nothing is stored, no link changes, and the stopped grant stays in
``policy.grants`` (so it can still be revoked). A renew of a stopped grant is refused at append, because a renew by a
key still held would otherwise move the current link back to an active signer."""
from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

import levain.authority as auth
from levain.cockpit.consent import utc_stamp

pytest.importorskip("cryptography")
from tests.test_authority_graduation_kind import GP, OPT, World, grad, opt_in  # noqa: E402
from tests.test_authority_graduation_kind import renew as grad_renew  # noqa: E402
from tests.test_authority_seat_lease import lease  # noqa: E402
from tests.test_consent_queue import Key  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

LID = "sl-000000000001"


class Laptops(World):
    """The graduation tests' world, with two more laptop-presence keys: ``a`` (its genesis key, also ``laptop``), ``b``
    and ``c``. Every extra key is enrolled by A."""

    def __init__(self, tmp) -> None:
        super().__init__(tmp)
        self.keys["a"] = self.keys["laptop"]
        for name in ("b", "c"):
            key = Key(tmp / "keys", f"key-{name}")
            self.keys[name] = (key.fp, lambda m, key=key: key.sign(m, auth.NAMESPACE))
            self.link({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key,
                       "class": "laptop-presence", "custody": "passphrase"}, "a")

    def revoke(self, name: str, by: str = "a", **extra) -> auth.AuthoritySnapshot:
        return self.link({"op": "revoke_key", "fingerprint": self.fp(name), **extra}, by)

    def renew_lease(self, signer: str, days: int = 14) -> dict:
        at = self.clock.now + timedelta(minutes=1)        # the stamp of the link World.link is about to write
        return {"op": "renew", "grant_id": LID, "expires_at": utc_stamp(at + timedelta(days=days)),
                "signed_at": utc_stamp(at)}


@pytest.fixture
def world(tmp_path) -> Laptops:
    return Laptops(tmp_path)


def signed_by_a(world: Laptops, tmp_path) -> auth.AuthoritySnapshot:
    return world.link({"op": "create_grant", "grant": lease(world, tmp_path)}, "a")


# --- the rule, and T5: a signer that signed nothing of the grant stops nothing ---------------------------------------

def test_control_a_grant_whose_signers_are_active_answers_and_names_its_signers(world, tmp_path):
    state = signed_by_a(world, tmp_path)
    assert auth.grant_signers(state, LID) == (world.fp("a"), world.fp("a"))
    assert auth.grant_stop_reason(state, LID) is None
    assert LID in auth.grants_in_force(state)
    assert auth.grants_stopped_by(state, world.fp("a")) == [LID]
    assert auth.grants_stopped_by(state, world.fp("b")) == []


@pytest.mark.parametrize("revoker", ["a", "c"])
def test_t5_revoking_a_key_that_signed_neither_link_leaves_the_grant_in_force(world, tmp_path, revoker):
    """A creates, B renews, C is revoked (once by A, once by C itself: its own revoke link is then the link after the
    renew, so a signer index off by one lands on a revoked key)."""
    signed_by_a(world, tmp_path)
    world.link(world.renew_lease("b"), "b")
    state = world.revoke("c", by=revoker)
    assert not state.key(world.fp("c")).active
    assert auth.grant_signers(state, LID) == (world.fp("a"), world.fp("b"))
    assert auth.grant_stop_reason(state, LID) is None
    assert LID in auth.grants_in_force(state)
    assert auth.grants_stopped_by(state, world.fp("c")) == []


def test_the_signer_that_last_renewed_a_grant_stops_it_when_revoked(world, tmp_path):
    signed_by_a(world, tmp_path)
    world.link(world.renew_lease("b"), "b")
    by_b = world.revoke("b")
    assert auth.grant_stop_reason(by_b, LID) == auth.STOPPED_CURRENT
    assert LID not in auth.grants_in_force(by_b) and LID in by_b.policy.grants      # still in the policy
    assert auth.grants_stopped_by(by_b, world.fp("b")) == [LID]


def test_the_born_signer_wins_when_both_are_revoked(world, tmp_path):
    signed_by_a(world, tmp_path)
    world.link(world.renew_lease("b"), "b")
    world.revoke("b", by="c")
    state = world.revoke("a", by="c")
    assert auth.grant_stop_reason(state, LID) == auth.STOPPED_BORN


def test_a_restatement_that_leaves_a_grant_unchanged_does_not_make_its_signer_current(world, tmp_path):
    """A loosen by B that re-states the whole policy and adds another grant: the first grant is untouched, so its
    current link stays A's create and revoking B stops nothing of it."""
    state = signed_by_a(world, tmp_path)
    p = state.policy
    policy = {"raise_to": auth._thaw(p.raise_to), "timeouts": auth._thaw(p.timeouts),
              "grants": {**auth._thaw(p.grants), GP: grad(world)}}
    restated = world.link({"op": "loosen", "policy": policy}, "b")
    assert restated.grant_seqs[LID] == state.grant_seqs[LID]
    assert auth.grant_signers(restated, LID) == (world.fp("a"), world.fp("a"))
    assert auth.grant_signers(restated, GP) == (world.fp("b"), world.fp("b"))
    after = world.revoke("b")
    assert auth.grant_stop_reason(after, LID) is None
    assert auth.grant_stop_reason(after, GP) == auth.STOPPED_BORN


# --- T7, the append half: a stopped grant is not renewed back ------------------------------------------------------

def test_t7_a_renew_of_a_live_grant_still_lands(world, tmp_path):
    signed_by_a(world, tmp_path)
    world.link(world.renew_lease("b"), "b")
    state = world.link(world.renew_lease("a"), "a")
    assert auth.grant_signers(state, LID) == (world.fp("a"), world.fp("a"))
    assert auth.grant_stop_reason(state, LID) is None


def test_t7_a_renew_by_a_held_key_does_not_bring_back_a_grant_the_last_renewer_lost(world, tmp_path):
    signed_by_a(world, tmp_path)
    world.link(world.renew_lease("b"), "b")
    stopped = world.revoke("b")
    said = world.refused(world.renew_lease("a"), "a")
    assert f"grant {LID} stopped when {auth.STOPPED_CURRENT}" in said
    # a lease has a one-live rule that still counts it, so the way back revokes it first (L4 F1)
    assert "a renew does not bring it back: revoke it, then create it again with a key you hold" in said
    assert world.state().seq == stopped.seq                      # nothing was written
    assert auth.grant_stop_reason(world.state(), LID) == auth.STOPPED_CURRENT


def test_t7_a_renew_of_a_grant_whose_creator_is_revoked_is_refused_whoever_renews(world, tmp_path):
    signed_by_a(world, tmp_path)
    world.revoke("a", by="b")
    for signer in ("b", "c"):
        said = world.refused(world.renew_lease(signer), signer)
        assert f"grant {LID} stopped when {auth.STOPPED_BORN}" in said
    assert LID in world.state().policy.grants                   # stopped, not gone: it can still be revoked
    world.link({"op": "revoke_grant", "grant_id": LID}, "b")
    assert LID not in world.state().policy.grants


def test_t7_the_rule_is_kind_blind_a_graduation_policy_is_held_to_it_too(world):
    """The one append rule covers every renew writer: a graduation policy a passkey renewed, the passkey revoked, the
    laptop's renew refused."""
    world.link({"op": "create_grant", "grant": grad(world)}, "a")
    world.link(grad_renew(world), "pk")
    state = world.revoke("pk")
    assert auth.grant_stop_reason(state, GP) == auth.STOPPED_CURRENT
    assert f"grant {GP} stopped when {auth.STOPPED_CURRENT}" in world.refused(grad_renew(world), "a")


# --- T10: it fails closed ---------------------------------------------------------------------------------------------

def test_t10_a_snapshot_that_cannot_name_a_grants_signers_stops_the_grant(world, tmp_path):
    state = signed_by_a(world, tmp_path)
    assert auth.grant_stop_reason(state, LID) is None
    short = replace(state, link_signers=state.link_signers[:-1])           # not one signer per link
    longer = replace(state, link_signers=state.link_signers + (world.fp("a"),))
    no_born = replace(state, grant_born=MappingProxyType({}))
    no_current = replace(state, grant_seqs=MappingProxyType({}))
    zero = replace(state, grant_born=MappingProxyType({**state.grant_born, LID: 0}))
    past_head = replace(state, grant_seqs=MappingProxyType({**state.grant_seqs, LID: state.seq + 1}))
    for broken in (short, longer, no_born, no_current, zero, past_head):
        assert auth.grant_signers(broken, LID) is None
        assert auth.grant_stop_reason(broken, LID) is not None
        assert LID not in auth.grants_in_force(broken)
        assert LID in broken.policy.grants


def test_t10_a_signer_the_snapshot_has_no_key_for_stops_the_grant(world, tmp_path):
    state = signed_by_a(world, tmp_path)
    keys = {fp: k for fp, k in state.keys.items() if fp != world.fp("a")}
    assert auth.grant_stop_reason(replace(state, keys=MappingProxyType(keys)), LID) == auth.STOPPED_BORN


def test_t10_an_id_the_policy_does_not_hold_has_no_reason_and_no_signers(world, tmp_path):
    state = signed_by_a(world, tmp_path)
    assert auth.grant_stop_reason(state, "sl-ffffffffffff") is None
    assert auth.grant_signers(state, "sl-ffffffffffff") is None


# --- T11: class-blind --------------------------------------------------------------------------------------------------

def test_t11_a_revoked_display_phone_passkey_stops_what_it_created(world):
    """A passkey (display-phone class) the opt-in names creates a graduation policy; revoking the passkey stops it."""
    world.link({"op": "create_grant", "grant": opt_in(world, "pk")}, "a")
    created = world.link({"op": "create_grant", "grant": grad(world)}, "pk")
    assert auth.grant_stop_reason(created, GP) is None
    state = world.revoke("pk")
    assert state.key(world.fp("pk")).key_class == "display-phone"
    assert auth.grant_stop_reason(state, GP) == auth.STOPPED_BORN
    assert auth.grant_stop_reason(state, OPT) is None            # the laptop's opt-in is untouched


@pytest.mark.parametrize("key_class", ["display-phone", "presence", "team"])
def test_t11_the_stop_reads_active_and_never_a_key_class(world, tmp_path, key_class):
    state = signed_by_a(world, tmp_path)
    a = state.key(world.fp("a"))
    retyped = replace(state, keys=MappingProxyType({**state.keys, a.fingerprint: replace(a, key_class=key_class,
                                                                                         revoked_seq=state.seq)}))
    assert auth.grant_stop_reason(retyped, LID) == auth.STOPPED_BORN
    alive = replace(state, keys=MappingProxyType({**state.keys, a.fingerprint: replace(a, key_class=key_class)}))
    assert auth.grant_stop_reason(alive, LID) is None


# --- T19: a quarantined signer is still an active key ------------------------------------------------------------------

def test_t19_a_quarantined_signer_does_not_stop_its_grants(world, tmp_path):
    """Quarantine is not a revoke: the key signs nothing new, its old grants stand until it is revoked."""
    state = signed_by_a(world, tmp_path)
    a = state.key(world.fp("a"))
    held = replace(state, keys=MappingProxyType({**state.keys, a.fingerprint: replace(a, custody="passkey")}))
    assert [k.fingerprint for k, _why in held.quarantined_keys()] == [a.fingerprint]       # the precondition
    assert held.key(a.fingerprint).active
    assert auth.grant_stop_reason(held, LID) is None
    assert LID in auth.grants_in_force(held)
    assert auth.grants_stopped_by(held, a.fingerprint) == [LID]       # named as its signer, not as stopped
