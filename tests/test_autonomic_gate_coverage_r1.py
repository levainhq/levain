"""14b S3a L3 r1 (RULINGS_R1 items 1-3): the sealed rung of a binding's terminal link, one coverage read per
unattended manual resolve, and a binding whose family cannot be computed.

Everything runs through a real run journal, binding registry, pending store, fire dispatcher and gate (the world of
``tests/test_autonomic_gate_coverage.py``); the coverage source is the test-only in-memory one.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json

import pytest

import levain.autonomic.gate as gate_module
from levain.autonomic import ConfirmDecision, Posture, RunRef
from levain.autonomic.binding import binding_invocation
from levain.autonomic.confirm import challenge as confirm_challenge_bytes
from tests.autonomic_confirm_keys import SIGNER, sign, signed_yes
from tests.test_autonomic_gate_coverage import FIXED, LATER, CWorld, _covering, _manual
from tests.test_autonomic_journal_wiring import LOW, _single_builder

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")

ABOVE_CONFIRM = [Posture.CONFIRM_ELEVATED, Posture.REFUSE_ESCALATE]


# --- item 1: a terminal link runs at no rung below its sealed binding's -------------------------------------

def _forged(w: CWorld, b, ratified: Posture, *, continuation=None):
    w.journal.start("run-forged", binding_id=b.binding_id)
    return dataclasses.replace(
        _single_builder(b, {"id": "z1"}), risk=LOW, authority=binding_invocation(w.store.get(b.binding_id)),
        ratified_posture=ratified, run=RunRef("run-forged", "link-0"), continuation=continuation)


@pytest.mark.parametrize("sealed", ABOVE_CONFIRM)
def test_a_terminal_link_carrying_confirm_for_a_binding_sealed_above_it_is_refused(tmp_path, sealed):
    w = CWorld(tmp_path)
    b = w.mint(sealed)
    out = w.gate.gate(_forged(w, b, Posture.CONFIRM))
    if out.pending:   # what a signed approval of that proposal does
        w.gate.resolve(out.pending_id, signed_yes(w.gate, out.pending_id))
    assert w.outbox() == []
    assert (out.refused, out.reason, out.posture) == (True, "ratified_posture_mismatch", Posture.REFUSE_ESCALATE)


def test_a_ratified_posture_on_a_link_other_than_the_terminal_one_is_refused(tmp_path):
    # the sealed rung binds the chain's last link; an intermediate link carries none
    w = CWorld(tmp_path)
    b = w.mint(Posture.CONFIRM, chain=True)
    out = w.gate.gate(_forged(w, b, Posture.CONFIRM))                       # link-0 of two
    assert out.refused and out.reason == "ratified_posture_mismatch" and w.outbox() == []


def test_a_kill_worthy_trigger_with_a_forged_rung_still_gets_its_kill_receipt(tmp_path):
    # L3 r1 complement LOW 3: the refusal of the rung comes after the known-danger kill
    w = CWorld(tmp_path)
    b = w.mint(Posture.CONFIRM_ELEVATED)
    request = dataclasses.replace(_forged(w, b, Posture.CONFIRM), trigger_event={"dmarc": "fail"},
                                  kill_predicates=tuple(g.kill_predicate for g in b.guard))
    out = w.gate.gate(request)
    assert out.killed and not out.fired and w.outbox() == []


def test_a_ratified_posture_whose_binding_cannot_be_read_is_refused(tmp_path):
    # the continuation a chain link's binding is read from names no sealed binding
    w = CWorld(tmp_path)
    b = w.mint(Posture.CONFIRM_ELEVATED)
    out = w.gate.gate(_forged(w, b, Posture.CONFIRM, continuation={"binding": {}}))
    assert out.refused and out.reason == "ratified_posture_unverifiable" and w.outbox() == []


def _confirm_rung_yes(w: CWorld, p) -> ConfirmDecision:
    """A person's yes signed over the challenge of the planted pending at its own rung, CONFIRM."""
    message = confirm_challenge_bytes(pending=p, fence=gate_module._risk_fence(LOW, LOW), rung="CONFIRM",
                                      store_id=w.journal.store_id, hold_id=p.hold_id)
    return ConfirmDecision(approved=True, by="human", signer=SIGNER, signature=sign(message))


@pytest.mark.parametrize("sealed", ABOVE_CONFIRM)
def test_a_planted_confirm_pending_resolves_at_the_sealed_rung(tmp_path, sealed):
    # a hold of binding B (sealed above CONFIRM) whose pending says CONFIRM: what a historical or malformed
    # record, or a writer of the levain home, can leave. The resolve re-derives the terminal link's sealed rung.
    w = CWorld(tmp_path)
    b = w.mint(sealed)
    p = w.plant_hold(b, "link0", posture="CONFIRM")
    challenge = w.gate.confirm_challenge(p.pending_id)
    out = w.gate.resolve(p.pending_id, _confirm_rung_yes(w, p))
    assert not out.fired and w.outbox() == []
    if sealed is Posture.REFUSE_ESCALATE:
        assert challenge is None                                    # no rung may approve it
    else:
        assert json.loads(challenge)["rung"] == "CONFIRM_ELEVATED"
        done = w.gate.resolve(p.pending_id, signed_yes(w.gate, p.pending_id))
        assert done.fired and done.posture is Posture.CONFIRM_ELEVATED and w.outbox() == ["link0"]


# --- item 2: one coverage read per unattended manual resolve -------------------------------------------------

def test_a_refresh_failing_after_a_good_pre_claim_read_does_not_spend_the_pending(tmp_path):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.after_refresh = lambda: setattr(src, "fail_refresh", True)   # every refresh after the first fails
    [out] = w.sweep()
    assert out.fired and w.outbox() == ["link0"]
    assert src.refreshes == 1                                        # one read: the decision and its guard


def test_a_cover_revoked_between_the_read_and_the_fire_is_denied(tmp_path):
    # the guard after the claim re-checks the snapshot read before it (the peek), so authority that moved
    # between them is still caught
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.after_refresh = lambda: src.revoke("gp-a")
    [out] = w.sweep()
    assert not out.fired and out.reason == "cover:moved" and w.outbox() == []


# the manual pending expires at FIXED + 1h (its window is an hour): unreadable coverage leaves it open for one more
# window, then denies it
WITHIN_GRACE = FIXED + _dt.timedelta(minutes=90)
PAST_GRACE = FIXED + _dt.timedelta(hours=2)


def test_unreadable_coverage_leaves_the_pending_open_within_its_grace(tmp_path):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.fail_refresh = True
    assert w.sweep(WITHIN_GRACE) == [] and w.pending.get(p.pending_id) is not None and w.receipts.read() == []


@pytest.mark.parametrize("at", [PAST_GRACE, LATER])
def test_unreadable_coverage_past_the_grace_denies_the_pending(tmp_path, at):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.fail_refresh = True
    [out] = w.sweep(at)
    assert not out.fired and out.refused and out.reason == "cover:unreadable" and w.outbox() == []
    assert w.pending.get(p.pending_id) is None
    [r] = w.receipts.read()
    assert r.fired is False and r.action_face["terminal_state"] == "timed_out"
    src.fail_refresh = False                                         # it reads covered again: nothing fires late
    assert w.sweep(at) == [] and w.outbox() == []


# --- item 3: a binding whose family cannot be computed -------------------------------------------------------

def _family_raising_for(monkeypatch, bad) -> None:
    real = gate_module.binding_family

    def family(binding):
        if binding.binding_id == bad.binding_id:
            raise ValueError("Out of range float values are not JSON compliant")
        return real(binding)
    monkeypatch.setattr(gate_module, "binding_family", family)


def test_a_binding_whose_family_raises_is_denied_by_name_and_the_dispatch_goes_on(tmp_path, monkeypatch):
    w = CWorld(tmp_path, coverage=_covering("link0"))
    bad = w.mint(Posture.ON_LOOP, created_at="2026-10-07T09:00:00a")
    good = w.mint(Posture.ON_LOOP, created_at="2026-10-07T09:00:00b")
    _family_raising_for(monkeypatch, bad)
    by_binding = {d.binding_id: d.outcome for d in w.dispatcher.dispatch(
        {"type": "email", "id": "fam1", "fields": {"from": "a@x.example", "dmarc": "pass"}})}
    assert by_binding[good.binding_id].fired
    out = by_binding[bad.binding_id]
    assert not out.fired and out.refused and out.reason == "clamp:binding_family_unreadable"
    assert out.receipt_id is not None and w.outbox() == ["link0"]


def test_a_held_effect_whose_family_raises_is_decided_by_name(tmp_path, monkeypatch):
    # the sweep decides it (a rejection naming why) instead of skipping it on every sweep
    w = CWorld(tmp_path, coverage=_covering("link0"), auto_fire=frozenset({"link0"}))
    b = w.mint(Posture.ON_LOOP)
    w.plant_hold(b, "link0")
    _family_raising_for(monkeypatch, b)
    [out] = w.sweep()
    assert not out.fired and out.reason == "denied:clamp:binding_family_unreadable" and w.outbox() == []
    assert w.gate.open_pendings() == []
