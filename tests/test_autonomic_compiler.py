"""Phase-2 Slice-3c tests — the bind-time NL→`P` compiler core + the Guard sealed-core addition.

Deterministic units (fake injected seams — the vagus core is corpus-agnostic; the REAL
``events.validate_predicate`` / ``events.replay`` integration is tested flow-side). Cover:

  - the ``Guard`` dataclass: rationale/dissent_author non-empty; the kill travels as a UNIT
    (predicate+drill+author all-or-none); opaque dicts canonical-JSON; ``has_kill``; to/from_dict
    round-trip; calibration-only (no-kill) guards.
  - the SEAL now covers the guard: a guard add / strip / edit after ratification → seal MISMATCH
    (the anti-defanged-dissent invariant); status/graduation still outside the seal; backward-compat
    (a pre-3c record with no ``guard`` key loads + seals stable).
  - ``compile_binding`` — every RECEIPT_VERSION=4 compiler invariant: P-deterministic-or-refuse(+
    decompose); posture COMPUTED via policy; classification-integrity (a downgrade DENIED + surfaced,
    a confirm-class action can't seal on-loop); confirm-class ⇒ mandatory well-formed guard;
    adversarial dissent (self-authored refused); deterministic kill; the on-loop fast-lane;
    pattern_precision COMPUTED (dead-field → 0.0, warnings docked, cold-start flagged); the priced-
    looseness surfacing; ``refuse_undecidable``.
"""
from __future__ import annotations

import dataclasses

import pytest

from levain.autonomic import (
    ActionRisk,
    Binding,
    BindingStatus,
    BindingStore,
    CandidateBinding,
    CompileResult,
    Guard,
    IntentProvenance,
    Posture,
    REFUSE_CONSTITUTIONAL,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    compile_binding,
    refuse_undecidable,
    seal_binding_id,
)

# --- fake injected seams ------------------------------------------------------------------

_LEAF_OPS = {"==", "!=", "contains", "in", ">", ">=", "<", "<=", "exists"}


class FakeValidator:
    """Faithful to ``events.validate_predicate``'s CONTRACT (raises on a non-deterministic /
    malformed / empty-composite / unknown-op predicate). Duck-typed to the PredicateValidator seam."""

    def validate(self, pattern):
        self._node(pattern)

    def _node(self, node):
        if not isinstance(node, dict):
            raise ValueError("predicate node must be a JSON object")
        op = node.get("op")
        if op in ("and", "or"):
            cl = node.get("clauses")
            if not isinstance(cl, list) or not cl:
                raise ValueError(f"{op!r} requires a NON-EMPTY clauses list (empty fires on everything)")
            for c in cl:
                self._node(c)
            return
        if op == "not":
            if "clause" not in node:
                raise ValueError("'not' requires a clause")
            self._node(node["clause"])
            return
        if op not in _LEAF_OPS:
            raise ValueError(f"unknown/non-deterministic op {op!r}")
        if not isinstance(node.get("field"), str) or not node.get("field"):
            raise ValueError("leaf requires a non-empty field")


class FakeBacktester:
    """Configurable ``events.replay``-shaped report. ``dead`` → the referenced field resolves 0%;
    ``scanned=0`` → the cold-start (no-corpus) case; ``warnings`` → docks pattern_precision."""

    def __init__(self, *, scanned=100, fire=4, dead=False, warnings=None, exact=True, fe=None):
        self.scanned, self.fire, self.dead = scanned, fire, dead
        self.warnings = list(warnings or [])
        self.exact = exact
        self.fe = fe          # explicit field_engagement override (for the polarity / M3 tests)
        self.calls = []

    def replay(self, pattern, trigger_type, *, window_days):
        self.calls.append((pattern, trigger_type, window_days))
        pf = None if not self.scanned else (0.0 if self.dead else 0.8)
        fe = self.fe if self.fe is not None else \
            {"from_domain": {"present": 0 if self.dead else 80, "scanned": self.scanned,
                             "present_fraction": pf, "ops": ["=="]}}
        return {
            "fire_count": self.fire, "fire_count_exact": self.exact,
            "fire_count_by_coverage": {"attested": self.fire, "unknown": 0, "unattested": 0},
            "scanned": self.scanned,
            "field_engagement": (fe if self.scanned else {}),
            "warnings": list(self.warnings),
            "coverage": {"honest_note": "fires are OBSERVED; unknown != no-fire",
                         "attested_fraction": 1.0 if self.scanned else None},
        }


# --- builders -----------------------------------------------------------------------------

PAT = {"op": "==", "field": "from_domain", "value": "substack.com"}
KILL = {"op": "==", "field": "from_domain", "value": "blocked.example"}
GOAL = (SubGoal(goal="summarize into a Doc", tools=("mail.read", "gdrive.create"), output="drive:Doc"),)


def adversarial_guard(**kw):
    base = dict(rationale="this domain also sends marketing blasts — may over-fire on promo runs",
                dissent_author="codex", kill_predicate=dict(KILL),
                kill_drill={"from_domain": "blocked.example"}, kill_authored_by="phill")
    base.update(kw)
    return Guard(**base)


def candidate(**kw):
    base = dict(
        created_by="phill", created_at="2026-06-30T09:00:00", trigger_type="email",
        pattern=dict(PAT), goal=GOAL,
        risk=ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False),
        trust=TrustContext(signal_auth=SignalAuth.AUTHENTICATED,
                           intent_provenance=IntentProvenance.INTENT_FREE, hops=0, human_present=False),
    )
    base.update(kw)
    return CandidateBinding(**base)


def compile_(c, **kw):
    opts = dict(validator=FakeValidator(), backtester=FakeBacktester(), compiler_substrate="claude")
    opts.update(kw)
    return compile_binding(c, **opts)


# --- Guard dataclass --------------------------------------------------------------------

def test_guard_rationale_required():
    with pytest.raises(ValueError):
        Guard(rationale="   ", dissent_author="codex")


def test_guard_dissent_author_required():
    with pytest.raises(ValueError):
        Guard(rationale="why", dissent_author="")


def test_guard_kill_travels_as_a_unit():
    # predicate without drill/author → malformed
    with pytest.raises(ValueError):
        Guard(rationale="why", dissent_author="codex", kill_predicate=dict(KILL))
    # drill without predicate → malformed
    with pytest.raises(ValueError):
        Guard(rationale="why", dissent_author="codex", kill_drill={"x": 1}, kill_authored_by="phill")


def test_guard_calibration_only_no_kill_is_valid():
    g = Guard(rationale="watch this one", dissent_author="codex")
    assert g.has_kill is False


def test_guard_full_kill_has_kill():
    assert adversarial_guard().has_kill is True


def test_guard_opaque_dicts_canonical_json():
    # a non-string key in the kill predicate makes the seal ambiguous → rejected at construction
    with pytest.raises(TypeError):
        Guard(rationale="why", dissent_author="codex", kill_predicate={1: "x"},
              kill_drill={"a": 1}, kill_authored_by="phill")


def test_guard_round_trip():
    g = adversarial_guard(spike_id="spike-1", predicted_trajectory={"step": 1})
    again = Guard.from_dict(g.to_dict())
    assert again == g


# --- the seal now covers the guard ------------------------------------------------------

def _binding(**kw):
    base = dict(created_by="phill", created_at="2026-06-30T09:00:00",
                trigger=TriggerSpec(type="email", pattern=dict(PAT)), goal=GOAL,
                tightness=TightnessVector(0.9, 0.9, 1.0, 0.9), posture=Posture.CONFIRM)
    base.update(kw)
    return Binding.create(**base)


def test_guard_inside_seal_add_breaks_it():
    # two bindings identical except one carries a guard → DIFFERENT ids (guard is sealed)
    plain = _binding()
    guarded = _binding(guard=(adversarial_guard(),))
    assert plain.binding_id != guarded.binding_id


def test_guard_strip_after_ratification_breaks_seal():
    b = _binding(guard=(adversarial_guard(),))
    assert b.seal_matches()
    tampered = dataclasses.replace(b, guard=())
    assert not tampered.seal_matches()   # anti-defanged-dissent: stripping the guard is detectable


def test_guard_edit_after_ratification_breaks_seal():
    b = _binding(guard=(adversarial_guard(),))
    weakened = dataclasses.replace(b, guard=(adversarial_guard(rationale="actually it's fine"),))
    assert not weakened.seal_matches()


def test_guardless_seal_unchanged_by_guard_param_backward_compat():
    # Option B (L1-MED5): adding `guard` to the seal basis must NOT change a GUARDLESS binding's id —
    # else a genuine pre-3c (3a-basis) record would seal-MISMATCH and be barred as "tampered". The key
    # is OMITTED when the guard is empty, so seal(no-guard-arg) == seal(guard=()) == the 3a basis.
    core = dict(created_at="2026-06-30T09:00:00", created_by="phill",
                trigger=TriggerSpec(type="email", pattern=dict(PAT)), goal=GOAL,
                tightness=TightnessVector(0.9, 0.9, 1.0, 0.9), posture=Posture.CONFIRM, one_shot=False)
    assert seal_binding_id(**core) == seal_binding_id(**core, guard=())
    # and a stored record with NO `guard` key (the genuine 3a on-disk shape) loads + seals stable
    b = _binding()
    d = b.to_dict()
    d.pop("guard")
    reloaded = Binding.from_dict(d)
    assert reloaded.seal_matches() and reloaded.binding_id == b.binding_id and reloaded.guard == ()


def test_guarded_binding_persists_and_lists_active():
    g = adversarial_guard()
    b = _binding(guard=(g,), status=BindingStatus.ACTIVE)   # active + guarded → in the fire-set
    import io
    import tempfile
    import os
    d = tempfile.mkdtemp()
    store = BindingStore(os.path.join(d, "bindings.json"))
    store.add(b)
    got = store.get(b.binding_id)
    assert got is not None and got.guard == (g,) and got.seal_matches()
    assert b.binding_id in {x.binding_id for x in store.list_active()}


def test_seal_excludes_guard_is_false_helper():
    # explicit: seal_binding_id with vs without a guard differ
    args = dict(created_at="2026-06-30T09:00:00", created_by="phill",
                trigger=TriggerSpec(type="email", pattern=dict(PAT)), goal=GOAL,
                tightness=TightnessVector(0.9, 0.9, 1.0, 0.9), posture=Posture.CONFIRM, one_shot=False)
    assert seal_binding_id(**args) != seal_binding_id(**args, guard=(adversarial_guard(),))


# --- compile_binding: the deterministic-predicate floor ---------------------------------

def test_refuse_non_deterministic_predicate_offers_decomposition():
    r = compile_(candidate(pattern={"op": "and", "clauses": []}))
    assert not r.ok and r.refuse_class == REFUSE_CONSTITUTIONAL
    assert r.binding is None
    assert r.decomposition and any("proxies" in d.lower() for d in r.decomposition)


def test_refuse_unknown_op():
    r = compile_(candidate(pattern={"op": "regex_match", "field": "subject", "value": ".*"}))
    assert not r.ok and r.refuse_class == REFUSE_CONSTITUTIONAL


def test_refuse_undecidable_upstream():
    r = refuse_undecidable("when something important happens", reason="no deterministic reduction")
    assert not r.ok and r.refuse_class == REFUSE_CONSTITUTIONAL and r.decomposition


# --- compile_binding: posture computed + classification-integrity -----------------------

def test_confirm_class_no_guard_refused():
    r = compile_(candidate())  # AUTHENTICATED + intent_free → COOLING_OFF (confirm-class)
    assert not r.ok and r.refuse_class == REFUSE_CONSTITUTIONAL
    assert any("guard" in x.lower() for x in r.reasons)


def test_confirm_class_with_adversarial_guard_compiles():
    r = compile_(candidate(guard=(adversarial_guard(),)))
    assert r.ok and r.binding is not None
    assert r.binding.posture.needs_confirm
    assert r.binding.seal_matches() and len(r.binding.guard) == 1


def test_classification_integrity_downgrade_denied():
    # ask for on_loop on a confirm-class action → denied + surfaced; with no guard → refuse
    r = compile_(candidate(requested_posture=Posture.ON_LOOP))
    assert not r.ok
    assert any("classification-integrity" in x.lower() for x in r.reasons)


def test_classification_integrity_downgrade_denied_clamps_with_guard():
    # the downgrade is denied but a guard is present → compiles AT the resolved (higher) posture
    r = compile_(candidate(requested_posture=Posture.ON_LOOP, guard=(adversarial_guard(),)))
    assert r.ok
    assert r.surfacing.downgrade_denied is True
    assert r.binding.posture.needs_confirm  # sealed at the resolved floor, NOT on_loop


def test_request_more_involvement_is_honored():
    r = compile_(candidate(requested_posture=Posture.CONFIRM_ELEVATED, guard=(adversarial_guard(),)))
    assert r.ok and r.binding.posture == Posture.CONFIRM_ELEVATED


def test_on_loop_fast_lane_strong_auth_no_guard():
    # STRONG auth + reversible + create-only → ON_LOOP fast-lane; no guard required
    r = compile_(candidate(trust=TrustContext(signal_auth=SignalAuth.STRONG,
                                              intent_provenance=IntentProvenance.INTENT_FREE,
                                              hops=0, human_present=False)))
    assert r.ok and r.binding.posture == Posture.ON_LOOP and r.binding.guard == ()


def test_refuse_escalate_not_compilable():
    # UNAUTHENTICATED → earned_posture refuse_escalate → no execution path → refuse
    r = compile_(candidate(trust=TrustContext(signal_auth=SignalAuth.UNAUTHENTICATED,
                                             intent_provenance=IntentProvenance.INTENT_FREE,
                                             hops=0, human_present=False)))
    assert not r.ok and r.refuse_class == REFUSE_CONSTITUTIONAL


# --- compile_binding: the guard invariants ----------------------------------------------

def test_self_authored_dissent_refused():
    g = adversarial_guard(dissent_author="claude")  # same as compiler_substrate
    r = compile_(candidate(guard=(g,)), compiler_substrate="claude")
    assert not r.ok and any("adversarial" in x.lower() for x in r.reasons)


def test_non_deterministic_kill_refused():
    g = adversarial_guard(kill_predicate={"op": "and", "clauses": []},
                          kill_drill={"x": 1})  # drill present, but kill is non-det
    r = compile_(candidate(guard=(g,)))
    assert not r.ok and any("kill_predicate" in x.lower() or "kill" in x.lower() for x in r.reasons)


def test_kill_authored_by_compiler_substrate_refused():
    # HIGH-1: the human PRODUCES the kill — it cannot be authored by the COMPILING agent.
    g = adversarial_guard(kill_authored_by="claude")  # == compiler_substrate
    r = compile_(candidate(guard=(g,)), compiler_substrate="claude")
    assert not r.ok and any("operator-produced" in x.lower() or "produce" in x.lower() for x in r.reasons)


def test_kill_authored_by_dissent_author_refused():
    # HIGH-1: the dissenting agent cannot also write its own kill (both-raises-and-answers).
    g = adversarial_guard(dissent_author="codex", kill_authored_by="codex")
    r = compile_(candidate(guard=(g,)), compiler_substrate="claude")
    assert not r.ok and any("produce" in x.lower() for x in r.reasons)


def test_self_authored_calibration_guard_refused_at_confirm_class():
    # N4: a self-authored CALIBRATION (no-kill) guard riding alongside a valid adversarial kill-guard
    # still pollutes the dissent record → the adversarial check applies to ALL confirm-class guards.
    calib = Guard(rationale="just watch", dissent_author="claude")   # self-authored, no kill
    r = compile_(candidate(guard=(adversarial_guard(), calib)), compiler_substrate="claude")
    assert not r.ok and any("adversarial" in x.lower() for x in r.reasons)


def test_requested_refuse_escalate_refused():
    # LOW-6: a requested escalation TO refuse_escalate must refuse (no execution path), not mint an
    # incoherent guard-less grant with a false fast-lane surfacing.
    r = compile_(candidate(requested_posture=Posture.REFUSE_ESCALATE, guard=(adversarial_guard(),)))
    assert not r.ok and any("refuse_escalate" in x.lower() or "no execution path" in x.lower()
                            for x in r.reasons)


def test_calibration_guard_alone_does_not_satisfy_confirm_class():
    # a guard WITHOUT a kill (calibration-only) does not satisfy the confirm-class kill mandate
    g = Guard(rationale="watch this", dissent_author="codex")
    r = compile_(candidate(guard=(g,)))
    assert not r.ok and any("kill" in x.lower() for x in r.reasons)


# --- compile_binding: pattern_precision (the COMPUTED dimension) ------------------------

def test_pattern_precision_clean_is_one():
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=FakeBacktester())
    assert r.binding.tightness.pattern_precision == 1.0


def test_pattern_precision_dead_field_is_zero():
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=FakeBacktester(dead=True))
    assert r.ok and r.binding.tightness.pattern_precision == 0.0
    assert any("dead" in n.lower() for n in r.surfacing.notes)


def test_pattern_precision_warnings_docked():
    bt = FakeBacktester(warnings=["partial-absence under a negative op"])
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=bt)
    assert 0.0 < r.binding.tightness.pattern_precision < 1.0


def test_pattern_precision_cold_start_flagged():
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=FakeBacktester(scanned=0, fire=0))
    assert r.ok and r.binding.tightness.pattern_precision == 0.5
    assert any("cold start" in n.lower() for n in r.surfacing.notes)


def test_pattern_precision_negated_zero_present_is_not_dead():
    # LOW-8 (polarity-aware): a 0%-present field used ONLY under `not` fires on ABSENCE (intentional —
    # e.g. `not(dmarc == pass)` over no-dmarc mail), so it is NOT dead and stays precise.
    pat = {"op": "not", "clause": {"op": "==", "field": "dmarc", "value": "pass"}}
    fe = {"dmarc": {"present": 0, "scanned": 100, "present_fraction": 0.0, "ops": ["=="]}}
    r = compile_(candidate(pattern=pat, guard=(adversarial_guard(),)), backtester=FakeBacktester(fe=fe))
    assert r.ok and r.binding.tightness.pattern_precision == 1.0


def test_pattern_precision_positive_zero_present_is_dead():
    # the sibling: the SAME 0%-present field asserted POSITIVELY is dead → 0.0 (the catch survives).
    pat = {"op": "==", "field": "dmarc", "value": "pass"}
    fe = {"dmarc": {"present": 0, "scanned": 100, "present_fraction": 0.0, "ops": ["=="]}}
    r = compile_(candidate(pattern=pat, guard=(adversarial_guard(),)), backtester=FakeBacktester(fe=fe))
    assert r.ok and r.binding.tightness.pattern_precision == 0.0


def test_pattern_precision_unenumerated_field_capped():
    # M3: a field referenced in P but NOT enumerated by the backtest is UNMEASURED → capped at 0.5,
    # never silently "precise" (a backtester that under-reports can't launder tightness into the seal).
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=FakeBacktester(fe={}))  # empty FE
    assert r.ok and r.binding.tightness.pattern_precision == 0.5
    assert any("not enumerated" in n.lower() for n in r.surfacing.notes)


def test_absent_blind_kill_surfaced_not_refused():
    # H2: a kill using `!=` (absent-blind) compiles but SURFACES the absent-handling choice (the
    # doc-named compiler responsibility) — not a refusal; the human decides at ratification.
    g = adversarial_guard(kill_predicate={"op": "!=", "field": "dmarc", "value": "pass"},
                          kill_drill={"dmarc": "fail"})
    r = compile_(candidate(guard=(g,)))
    assert r.ok and any("absent" in n.lower() for n in r.surfacing.notes)


def test_compiled_binding_is_born_paused():
    # MED-2: the core mints PAUSED (fail-closed, ratification-ready), never ACTIVE — so a non-adapter
    # caller persisting result.binding directly gets a non-firing grant, not a live autonomous one.
    r = compile_(candidate(guard=(adversarial_guard(),)))
    assert r.ok and r.binding.status == BindingStatus.PAUSED and not r.binding.is_active


# --- compile_binding: the surfacing (priced looseness) ----------------------------------

def test_surfacing_confirm_cost_and_coverage():
    bt = FakeBacktester(fire=7)
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=bt)
    assert "7" in r.surfacing.confirm_cost_note
    assert r.surfacing.would_fire_count == 7 and r.surfacing.would_fire_exact is True
    assert r.surfacing.coverage_note
    assert bt.calls and bt.calls[0][1] == "email"  # the trigger type was replayed


def test_surfacing_lower_bound_when_not_exact():
    bt = FakeBacktester(fire=3, exact=False)
    r = compile_(candidate(guard=(adversarial_guard(),)), backtester=bt)
    assert "≥" in r.surfacing.confirm_cost_note and r.surfacing.would_fire_exact is False


def test_on_loop_surfacing_says_no_confirms():
    r = compile_(candidate(trust=TrustContext(signal_auth=SignalAuth.STRONG,
                                              intent_provenance=IntentProvenance.INTENT_FREE,
                                              hops=0, human_present=False)))
    assert "No confirms" in r.surfacing.confirm_cost_note


# --- L3 cross-substrate fold (codex + complement) ---------------------------------------

def _strong():
    # STRONG auth + intent_free → ON_LOOP fast-lane (not confirm-class)
    return TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                        hops=0, human_present=False)


def test_kill_determinism_validated_even_on_on_loop_binding():
    # codex-MED1: a non-deterministic kill is no kill on ANY binding — validated unconditionally, not
    # only at confirm-class. An on-loop binding carrying a bad-kill guard must REFUSE, not compile.
    g = Guard(rationale="watch", dissent_author="codex",
              kill_predicate={"op": "and", "clauses": []},   # non-deterministic
              kill_drill={"x": 1}, kill_authored_by="phill")
    r = compile_(candidate(trust=_strong(), guard=(g,)))
    assert not r.ok and any("kill_predicate" in x.lower() for x in r.reasons)


def test_dissent_author_case_normalized():
    # complement-MED1: "Claude" must not slip the cross-substrate check vs compiler_substrate "claude".
    g = adversarial_guard(dissent_author="Claude")
    r = compile_(candidate(guard=(g,)), compiler_substrate="claude")
    assert not r.ok and any("adversarial" in x.lower() for x in r.reasons)


def test_kill_author_case_normalized():
    g = adversarial_guard(dissent_author="codex", kill_authored_by="CODEX")  # == dissent (normalized)
    r = compile_(candidate(guard=(g,)), compiler_substrate="claude")
    assert not r.ok and any("produce" in x.lower() for x in r.reasons)


def test_whitespace_author_rejected_at_guard_construction():
    # complement-LOW3: a whitespace-only author is semantically empty → rejected at Guard construction.
    with pytest.raises(ValueError):
        Guard(rationale="x", dissent_author="   ")


def test_guard_to_dict_is_deep_immutable():
    # complement-LOW2: mutating the to_dict() output must NOT reach into the frozen guard's nested AST.
    g = adversarial_guard(kill_predicate={"op": "not", "clause": {"op": "==", "field": "dmarc",
                                                                  "value": "pass"}},
                          kill_drill={"dmarc": "fail"})
    d = g.to_dict()
    d["kill_predicate"]["clause"]["value"] = "TAMPERED"
    assert g.kill_predicate["clause"]["value"] == "pass"   # frozen guard unaffected


def test_binding_create_defaults_paused():
    # complement-LOW1 / codex-HIGH2: Binding.create defaults to PAUSED (fail-closed) — a caller that
    # forgets the lifecycle gets a non-firing grant.
    from levain.autonomic import BindingStatus as BS
    b = _binding()   # no status= passed
    assert b.status == BS.PAUSED and not b.is_active


def test_list_active_bars_confirm_class_without_kill_guard(tmp_path):
    # codex-HIGH2 / complement-LOW1 / L1-MED1: the fire-view structurally bars an ACTIVE confirm-class
    # binding that lacks a kill-guard, even if minted directly (bypassing the compiler).
    from levain.autonomic import BindingStatus as BS
    s = BindingStore(tmp_path / "b.json")
    guardless = _binding(posture=Posture.CONFIRM)   # confirm-class, no guard
    s.add(guardless)
    s.set_status(guardless.binding_id, BS.ACTIVE)    # ratify to active (bypassing the guard mandate)
    assert guardless.binding_id not in {x.binding_id for x in s.list_active()}   # barred from the fire set
    # a guarded confirm-class binding IS in the fire set
    guarded = _binding(posture=Posture.CONFIRM, guard=(adversarial_guard(),))
    s.add(guarded)
    s.set_status(guarded.binding_id, BS.ACTIVE)
    assert guarded.binding_id in {x.binding_id for x in s.list_active()}
