"""Phase-2 Slice-4c — the §2.6 meta-loop: graduation. Four blocks:
  1. the graduation DECISION (``propose_graduation``: evidence / all-clean / floor-room / fail-closed);
  2. the chain-DEPTH bound (``is_depth_bounded``: deep-outbound chains don't graduate);
  3. the re-ratification (``build_promoted`` + ``replace_atomic``: looser posture, FRESH evidence, atomic);
  4. the GATE seam #2 graduation MECHANISM (``max(risk_floor, ratified)`` — a graduated binding fires
     LOOSER; non-graduated is behavior-identical; a risk that ROSE still climbs).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import pytest

from levain.autonomic import (
    ActionManifest,
    ActionRequest,
    ActionRisk,
    AuthorityScope,
    Binding,
    BindingStatus,
    BindingStore,
    EfferentGate,
    ExecutionResult,
    GateReceiptStore,
    Graduation,
    Guard,
    GraduationProposal,
    IntentProvenance,
    Posture,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    is_depth_bounded,
    one_rung_looser,
    propose_graduation,
)

FIXED = _dt.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
LOW_INTERNAL = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
EXTERNAL = ActionRisk(cls=RiskClass.MEDIUM, reversible=False, external=True, financial=False)

_DELIVER = ("deliver_as_document", "doc.append")
_EMAIL = ("mail.send",)


# --- risk resolvers (mirror the flow aggregate: external if any tool is mail.send) ----------------
def _link_risk(b: Binding, i: int) -> ActionRisk:
    return EXTERNAL if "mail.send" in b.goal[i].tools else LOW_INTERNAL


def _agg_risk(b: Binding) -> ActionRisk:
    ext = any("mail.send" in sg.tools for sg in b.goal)
    rev = not ext  # mail.send is the only irreversible tool here
    return ActionRisk(cls=RiskClass.MEDIUM if ext else RiskClass.LOW, reversible=rev,
                      external=ext, financial=False)


def _raises_risk(_b: Binding) -> ActionRisk:  # an unclassifiable grant (a dropped tool)
    raise ValueError("undeclared tool")


# --- builders -------------------------------------------------------------------------------------
def _kill_guard() -> Guard:
    return Guard(
        rationale="a from check trusts the envelope domain — a spoof could trip this",
        dissent_author="codex",
        kill_predicate={"op": "not", "clause": {"op": "==", "field": "dmarc", "value": "pass"}},
        kill_drill={"dmarc": "fail"},
        kill_authored_by="phill",
    )


def _binding(*, posture: Posture, links=(_DELIVER,), clean=0, fires=0, one_shot=False,
             guard=(), status=BindingStatus.ACTIVE, outputs=None) -> Binding:
    outs = outputs or ["drive:x"] * len(links)
    goal = tuple(SubGoal(goal=f"g{i}", tools=t, output=outs[i]) for i, t in enumerate(links))
    return Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=goal, tightness=TightnessVector(1, 1, 1, 1), posture=posture, one_shot=one_shot,
        status=status, guard=guard, graduation=Graduation(fire_count=fires, clean_count=clean),
    )


def _propose(b: Binding, **kw):
    kw.setdefault("min_clean_fires", 3)
    return propose_graduation(b, risk_resolver=_agg_risk, link_risk_resolver=_link_risk, **kw)


# =====================================================================================
# Block 1 — the graduation DECISION
# =====================================================================================
def test_one_rung_looser_ladder():
    assert one_rung_looser(Posture.COOLING_OFF) is Posture.ON_LOOP
    assert one_rung_looser(Posture.CONFIRM) is Posture.COOLING_OFF
    assert one_rung_looser(Posture.ON_LOOP) is Posture.ABOVE_LOOP
    assert one_rung_looser(Posture.ABOVE_LOOP) is None


def test_proposes_when_evidence_met_shallow_internal():
    # cooling_off internal binding, 3 clean fires → propose ON_LOOP (the floor for internal/reversible).
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=3, fires=3)
    p = _propose(b)
    assert p is not None
    assert p.from_posture is Posture.COOLING_OFF and p.to_posture is Posture.ON_LOOP
    assert p.clean_count == 3 and p.fire_count == 3


def test_no_proposal_below_threshold():
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=2, fires=2)
    assert _propose(b) is None


def test_no_proposal_when_not_all_clean():
    # clean < fire (a future 4d-demotion would do this) — all-clean gate blocks it.
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=5, fires=6)
    assert _propose(b) is None


def test_no_proposal_when_already_at_floor():
    # an ON_LOOP internal binding is already AT its floor (on_loop) — nothing to earn.
    b = _binding(posture=Posture.ON_LOOP, clean=50, fires=50)
    assert _propose(b) is None


def test_no_proposal_when_inactive():
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9,
                 status=BindingStatus.PAUSED)
    assert _propose(b) is None


def test_no_proposal_when_seal_broken():
    from dataclasses import replace
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    tampered = replace(b, posture=Posture.ON_LOOP)  # id no longer matches the altered core
    assert not tampered.seal_matches()
    assert _propose(tampered) is None


def test_fail_closed_on_unclassifiable_risk():
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    assert propose_graduation(b, risk_resolver=_raises_risk, link_risk_resolver=_link_risk,
                              min_clean_fires=3) is None


def test_min_clean_fires_must_be_positive():
    b = _binding(posture=Posture.COOLING_OFF, clean=9, fires=9)
    with pytest.raises(ValueError):
        propose_graduation(b, risk_resolver=_agg_risk, link_risk_resolver=_link_risk, min_clean_fires=0)


# =====================================================================================
# Block 2 — the chain-DEPTH bound
# =====================================================================================
def test_single_link_internal_not_depth_bounded():
    b = _binding(posture=Posture.COOLING_OFF, links=(_DELIVER,))
    assert is_depth_bounded(b, _link_risk) is False


def test_single_link_external_not_depth_bounded_hops0():
    # an outbound at hops 0 (forwarding the trigger directly) is NOT "deep" — the FLOOR handles it.
    b = _binding(posture=Posture.CONFIRM, links=(_EMAIL,), outputs=["email:sender"])
    assert is_depth_bounded(b, _link_risk) is False


def test_chain_external_at_hops1_is_depth_bounded():
    # deliver_as_document (link0, internal) → email_send (link1, external, hops1) = deep-outbound.
    b = _binding(posture=Posture.CONFIRM, links=(_DELIVER, _EMAIL), outputs=["drive:x", "email:sender"])
    assert is_depth_bounded(b, _link_risk) is True


def test_internal_chain_not_depth_bounded():
    b = _binding(posture=Posture.COOLING_OFF, links=(_DELIVER, _DELIVER), outputs=["drive:x", "drive:y"])
    assert is_depth_bounded(b, _link_risk) is False


def test_depth_bound_fail_closed_on_unclassifiable_link():
    b = _binding(posture=Posture.CONFIRM, links=(_DELIVER, _EMAIL), outputs=["drive:x", "email:s"])
    def boom(_b, _i):
        raise ValueError("undeclared")
    assert is_depth_bounded(b, boom) is True  # fail-closed


def test_deep_outbound_chain_does_not_graduate():
    # even with rich clean evidence, a deep-outbound chain is REFUSED graduation (the §2.6 bound).
    b = _binding(posture=Posture.CONFIRM, links=(_DELIVER, _EMAIL), guard=(_kill_guard(),),
                 outputs=["drive:x", "email:sender"], clean=50, fires=50)
    assert _propose(b) is None


# =====================================================================================
# Block 3 — the re-ratification (build_promoted + replace_atomic)
# =====================================================================================
def test_build_promoted_loosens_posture_fresh_evidence_carries_guard():
    src = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    p = _propose(src)
    assert p is not None
    promoted = p.build_promoted()
    assert promoted.posture is Posture.ON_LOOP                 # one rung looser
    assert promoted.binding_id != src.binding_id              # a different sealed core → different id
    assert promoted.status is BindingStatus.PAUSED            # FAIL-CLOSED default (L2 L3 — matches Binding.create)
    assert promoted.graduation.fire_count == 0 and promoted.graduation.clean_count == 0  # FRESH (seam 5)
    assert promoted.guard == src.guard                        # safety guard carried
    assert promoted.trigger == src.trigger and promoted.goal == src.goal
    assert promoted.seal_matches()
    # the gated apply passes status=ACTIVE EXPLICITLY (the operator's --yes IS the ratification) — fireable.
    active = p.build_promoted(status=BindingStatus.ACTIVE)
    assert active.status is BindingStatus.ACTIVE and BindingStore.is_fireable(active)


def test_build_promoted_carries_guard_additions():
    src = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    from dataclasses import replace
    extra = Guard(rationale="extra hardening", dissent_author="codex",
                  kill_predicate={"op": "==", "field": "spam", "value": "true"},
                  kill_drill={"spam": "true"}, kill_authored_by="phill")
    src = replace(src, guard_additions=(extra,))
    p = _propose(src)
    promoted = p.build_promoted()
    assert promoted.guard_additions == (extra,)  # a graduation must not DROP safety
    assert promoted.seal_matches()               # additions are unsealed — the floor-seal still holds


def test_replace_atomic_promotion_revokes_old_persists_new(tmp_path):
    store = BindingStore(tmp_path / "b")
    src = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    store.add(src)
    p = _propose(src)
    promoted = p.build_promoted(status=BindingStatus.ACTIVE, created_at="2026-06-30T18:00:00")
    assert BindingStore.is_fireable(promoted)                  # the pre-persist defense-in-depth assert
    assert store.replace_atomic(p.source, promoted)
    # old REVOKED, new ACTIVE + fireable + fresh evidence.
    assert store.get(src.binding_id).status is BindingStatus.REVOKED
    new = store.get(promoted.binding_id)
    assert new is not None and new.status is BindingStatus.ACTIVE and new.posture is Posture.ON_LOOP
    assert new.graduation.fire_count == 0
    assert promoted.binding_id in {b.binding_id for b in store.list_active()}


# =====================================================================================
# Block 4 — the GATE seam #2 graduation MECHANISM (max(risk_floor, ratified))
# =====================================================================================
@dataclass
class _Exec:
    name: str = "rec"
    ok: bool = True
    def execute(self, action_name, payload, *, context_id):
        return ExecutionResult(ok=self.ok, detail="d", downstream_id="o", error=None)


def _gate(tmp_path):
    return EfferentGate(manifest=ActionManifest({}), store=GateReceiptStore(tmp_path / "r.jsonl"),
                        executor=_Exec(), clock=lambda: FIXED)


def _req(*, risk, ratified):
    # AUTHENTICATED + INTENT_FREE → earned_posture = COOLING_OFF (ON_LOOP base + intent-free +1).
    return ActionRequest(
        action_name="deliver", payload="x", context_id="c", query_text="q", query_date="2026-06-30",
        trust=TrustContext(signal_auth=SignalAuth.AUTHENTICATED, intent_provenance=IntentProvenance.INTENT_FREE,
                           hops=0, human_present=False),
        grounded=True, authority=AuthorityScope(grantor="binding", grant="g", binding_id="bind-x", hops=0),
        overall_confidence=1.0, directive_confidence=1.0, risk=risk, ratified_posture=ratified,
    )


def test_graduated_binding_fires_looser_not_refloored(tmp_path):
    # the CRUX: ratified=ON_LOOP (graduated) below earned=COOLING_OFF. Old max(policy,ratified) would
    # re-floor to COOLING_OFF (propose/defer); 4c max(risk_floor=ON_LOOP, ratified=ON_LOOP) FIRES on_loop.
    o = _gate(tmp_path).gate(_req(risk=LOW_INTERNAL, ratified=Posture.ON_LOOP))
    assert o.fired is True and o.posture is Posture.ON_LOOP


def test_non_graduated_binding_behavior_identical(tmp_path):
    # ratified == earned (COOLING_OFF) — max(risk_floor=ON_LOOP, COOLING_OFF) == COOLING_OFF (the old
    # max(policy, ratified) too). NOT fired; confirm-class (no transport → DEFER) at COOLING_OFF.
    o = _gate(tmp_path).gate(_req(risk=LOW_INTERNAL, ratified=Posture.COOLING_OFF))
    assert o.fired is False and o.posture is Posture.COOLING_OFF


def test_risk_rose_climbs_above_graduated_rung(tmp_path):
    # a graduated ON_LOOP rung, but the risk DECLARATION rose to EXTERNAL (floor=CONFIRM) → the floor
    # climbs UP and overrides the looser graduated rung (the critical safety case).
    o = _gate(tmp_path).gate(_req(risk=EXTERNAL, ratified=Posture.ON_LOOP))
    assert o.fired is False and o.posture is Posture.CONFIRM


def test_requested_upgrade_still_honored(tmp_path):
    # a requested-UPGRADE binding (ratified=CONFIRM, above earned=COOLING_OFF) still pauses at CONFIRM —
    # max(risk_floor=ON_LOOP, CONFIRM) == CONFIRM (ratified dominates; behavior-identical to the old form).
    o = _gate(tmp_path).gate(_req(risk=LOW_INTERNAL, ratified=Posture.CONFIRM))
    assert o.fired is False and o.posture is Posture.CONFIRM


# =====================================================================================
# Block 5 — the apparatus-hardening guards (codex/L1/L2 L3 findings)
# =====================================================================================
def test_ratified_posture_refused_for_non_binding_authority(tmp_path):
    # codex L3 HIGH-1: the loosening lever (ratified_posture below earned) must be tied to a binding
    # authority. A non-binding/forged request that sets it fails CLOSED (REFUSE), never fires looser.
    req = _req(risk=LOW_INTERNAL, ratified=Posture.ON_LOOP)
    from dataclasses import replace
    forged = replace(req, authority=AuthorityScope(grantor="human", grant="manual", binding_id=None, hops=0))
    o = _gate(tmp_path).gate(forged)
    assert o.refused and not o.fired and o.reason == "ratified_posture_without_binding_authority"


def test_ratified_posture_refused_for_binding_without_id(tmp_path):
    req = _req(risk=LOW_INTERNAL, ratified=Posture.ON_LOOP)
    from dataclasses import replace
    forged = replace(req, authority=AuthorityScope(grantor="binding", grant="g", binding_id=None, hops=0))
    o = _gate(tmp_path).gate(forged)
    assert o.refused and o.reason == "ratified_posture_without_binding_authority"


def test_kill_less_confirm_class_binding_does_not_graduate():
    # codex L3 MED: a confirm-class (cooling_off) ACTIVE binding with NO sealed kill is BARRED from the
    # fire view; with injected evidence it must NOT graduate into a fireable ON_LOOP grant.
    b = _binding(posture=Posture.COOLING_OFF, guard=(), clean=50, fires=50)  # no kill guard
    assert b.is_active and not BindingStore.is_fireable(b)   # active but barred (kill mandate)
    assert _propose(b) is None


def test_one_shot_does_not_graduate_even_with_evidence():
    b = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), one_shot=True, clean=50, fires=50)
    assert _propose(b) is None


def test_replace_atomic_precondition_aborts_on_change(tmp_path):
    # codex L3 HIGH-2 / L1 TOCTOU: an extra caller precondition is still evaluated under the lock.
    store = BindingStore(tmp_path / "b")
    src = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    store.add(src)
    p = _propose(src)
    promoted = p.build_promoted(status=BindingStatus.ACTIVE, created_at="2026-06-30T18:00:00")
    # a precondition that always fails → abort, write NOTHING, old untouched.
    assert not store.replace_atomic(p.source, promoted, precondition=lambda cur: False)
    assert store.get(src.binding_id).status is BindingStatus.ACTIVE   # NOT revoked
    assert store.get(promoted.binding_id) is None                     # NOT persisted
    # a precondition that passes → applies.
    assert store.replace_atomic(p.source, promoted, precondition=lambda cur: True)
    assert store.get(src.binding_id).status is BindingStatus.REVOKED
    assert store.get(promoted.binding_id).status is BindingStatus.ACTIVE


def test_replace_atomic_precondition_catches_concurrent_revoke(tmp_path):
    # the concrete govern-not-trust case: a revoke between the proposal read and the apply must NOT be
    # resurrected looser. The live-old check is part of the write now, so no precondition is needed.
    store = BindingStore(tmp_path / "b")
    src = _binding(posture=Posture.COOLING_OFF, guard=(_kill_guard(),), clean=9, fires=9)
    store.add(src)
    p = _propose(src)
    promoted = p.build_promoted(status=BindingStatus.ACTIVE, created_at="2026-06-30T18:00:00")
    store.set_status(src.binding_id, BindingStatus.REVOKED)   # concurrent revoke
    result = store.replace_atomic(p.source, promoted)
    assert not result and result.reason == "old_not_fireable"
    assert store.get(promoted.binding_id) is None             # the revoked grant was NOT resurrected looser
