"""Phase-2 Slice-3a L4 integration-semantics — does a stored binding actually COMPOSE with the
existing efferent kernel to produce the FROZEN DecisionInfluenceReceipt action-face UNCHANGED?

Unit tests (test_efferent_binding.py) prove the schema + registry in isolation. This file proves the
cross-module SEMANTICS the slice claims: (1) a binding fired through the real `receipt.build_gate_face`
+ `build_gate_verdict` producer yields EXACTLY the frozen 12-key action-face — no new keys, the freeze
holds — with `authority_scope` carrying the binding's grant + id and `gate.by == "binding"`; (2) the
full governed lifecycle over a real on-disk store does the right thing at each step (fire-set
membership, tamper barring, atomic re-ratification, dormancy bookkeeping).
"""
from __future__ import annotations

import dataclasses

from levain.autonomic.receipt import build_action_face
from levain.autonomic import (
    Binding,
    BindingStatus,
    BindingStore,
    Guard,
    Posture,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    binding_invocation,
)
from levain.autonomic.receipt import build_gate_face, build_gate_verdict


def _guard() -> Guard:
    """An adversarial, kill-bearing guard — what a confirm-class binding needs to be fireable
    (RECEIPT_VERSION=4; list_active bars a guardless confirm-class grant)."""
    return Guard(rationale="this domain also blasts marketing — may over-fire", dissent_author="codex",
                 kill_predicate={"op": "==", "field": "from_domain", "value": "blocked.example"},
                 kill_drill={"from_domain": "blocked.example"}, kill_authored_by="phill")

# the FROZEN DecisionInfluenceReceipt action-face shape (must not grow when a binding produces it)
FROZEN_FACE_KEYS = set(
    build_action_face(context_id="c", query_text="q", query_date="2026-06-26", producers=["afferent"]).keys()
)


def _binding(**kw) -> Binding:
    defaults = dict(
        created_by="phill", created_at="2026-06-26T13:00:00",
        trigger=TriggerSpec(type="email", pattern={"field": "from_domain", "op": "==", "value": "substack.com"}),
        goal=(SubGoal(goal="summarize into a Doc", tools=("mail.read", "gdrive.create"), output="drive:Doc"),),
        tightness=TightnessVector(goal_spec=0.9, tool_min=0.8, pattern_precision=1.0, output_bound=0.9),
        posture=Posture.ON_LOOP,
        status=BindingStatus.ACTIVE,   # these integration tests exercise the fire-path (Binding.create
        # now defaults PAUSED, fail-closed; the integration suite wants live bindings explicitly)
    )
    defaults.update(kw)
    return Binding.create(**defaults)


def test_a_fired_binding_produces_the_frozen_action_face_unchanged(tmp_path):
    """The freeze watch-edge: a binding PRODUCING a receipt must not mutate the contract shape."""
    store = BindingStore(tmp_path / "bindings.json")
    b = _binding()
    store.add(b)
    fired = store.list_active(trigger_type="email")
    assert [x.binding_id for x in fired] == [b.binding_id]

    # the binding authorizes an on-loop autonomous fire → produce the real action-face
    scope = binding_invocation(b, hops=1)
    face = build_gate_face(
        context_id="evt-123",
        query_text="substack newsletter arrived",
        query_date="2026-06-26",
        producers=["afferent"],
        gate=build_gate_verdict(verdict="auto", by="binding", binding_id=b.binding_id),
        authority=scope,
        terminal_state="fired",
        downstream_id="doc:abc",
    )
    # 1. the freeze holds — no new keys beyond the frozen contract (now RECEIPT_VERSION=4: the v3
    #    action-face keys + the v4 terminal_state/refuse_class, both derived from build_action_face)
    assert set(face.keys()) == FROZEN_FACE_KEYS
    # 2. the grant flows through into the receipt's authority_scope
    assert face["authority_scope"]["grantor"] == "binding"
    assert face["authority_scope"]["binding_id"] == b.binding_id
    assert face["authority_scope"]["hops"] == 1
    # 3. the gate verdict names the binding as the decider
    assert face["gate"] == {"verdict": "auto", "by": "binding", "binding_id": b.binding_id}
    assert face["downstream_claim_or_action_id"] == "doc:abc"


def test_tampered_or_revoked_grant_cannot_produce_authority(tmp_path):
    """Fail-closed end-to-end: a grant barred from the fire-set also cannot mint a receipt's authority."""
    store = BindingStore(tmp_path / "bindings.json")
    b = _binding()
    store.add(b)
    store.set_status(b.binding_id, BindingStatus.REVOKED)
    assert store.list_active() == []                       # not in the fire-set
    revoked = store.get(b.binding_id)
    assert revoked is not None
    try:
        binding_invocation(revoked)                        # and cannot mint authority
        raise AssertionError("revoked grant minted authority")
    except ValueError:
        pass


def test_governed_lifecycle_over_a_real_store(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    # CONFIRM + a kill-guard → fireable (the guard-mandate); differs from the ON_LOOP `promoted` below
    # (so the re-ratification produces a different sealed id).
    b = _binding(posture=Posture.CONFIRM, guard=(_guard(),))
    store.add(b)

    # fire twice, one clean one not → bookkeeping accrues, dormancy stamped, id stable, still fireable
    store.record_fire(b.binding_id, clean=True, fired_at="2026-06-26T14:00:00")
    after = store.record_fire(b.binding_id, clean=False, fired_at="2026-06-26T15:30:00")
    assert after is not None
    assert (after.graduation.fire_count, after.graduation.clean_count) == (2, 1)
    assert after.graduation.last_fired_at == "2026-06-26T15:30:00"
    assert [x.binding_id for x in store.list_active()] == [b.binding_id]

    # re-ratify (promote posture) atomically → old revoked, new active, exactly one fireable
    promoted = _binding(posture=Posture.ON_LOOP)
    assert promoted.binding_id != b.binding_id
    assert store.replace_atomic(b.binding_id, promoted) is True
    assert store.get(b.binding_id).status is BindingStatus.REVOKED   # type: ignore[union-attr]
    assert {x.binding_id for x in store.list_active()} == {promoted.binding_id}

    # a disk tamper of the promoted grant's core bars it (fail-closed), leaving an EMPTY fire-set
    import json
    raw = json.loads(store.path.read_text())
    for rec in raw:
        if rec["binding_id"] == promoted.binding_id:
            rec["posture"] = "ABOVE_LOOP"     # widen autonomy without re-ratifying
    store.path.write_text(json.dumps(raw))
    assert store.list_active() == []          # the widened grant cannot fire
