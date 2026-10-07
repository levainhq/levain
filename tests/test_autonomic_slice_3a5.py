"""Slice 3a.5 tests — the GUARD HARDENING (what makes GRADUATION to on-loop safe).

Five pieces, each carved from RECEIPT_VERSION=4 §2.1 (`decision_influence_receipt_contract.md`):

  1. monotonic-editability — the unsealed ``guard_additions`` tightening tier + ``tighten_guard``
     (tighten-free / loosen-re-ratifies, the id + graduation preserved; loosening touches the sealed
     floor → barred → re-ratify).
  2. the kill-DRILL compile gate — the drill must ACTUALLY trip the kill (on the diverse substrate).
  3. kill-PURITY + the DIVERSE, fail-safe Kleene evaluator (absent/unevaluable → trip — the inversion
     of events.py's absent→False that breaks the common-mode H2 footgun).
  4. the runtime prediction-error MONITOR — the gate diffs actual-vs-predicted_trajectory → KILLED.
  5. binding + gate liveness telemetry.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field, replace

import pytest

from levain.autonomic import (
    ActionManifest,
    ActionRequest,
    ActionRisk,
    Binding,
    BindingStatus,
    BindingStore,
    EfferentGate,
    ExecutionResult,
    GateReceiptStore,
    Guard,
    IntentProvenance,
    Kleene,
    KillImpurityError,
    Posture,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    assert_kill_pure,
    binding_liveness,
    compile_binding,
    gate_liveness,
    kill_outcome,
    kill_trips,
    manual_invocation,
    prediction_diverged,
    within_envelope,
)

# ===============================================================================================
# shared doubles + builders
# ===============================================================================================

_LEAF_OPS = {"==", "!=", "contains", "in", ">", ">=", "<", "<=", "exists"}


class FakeValidator:
    """events.validate_predicate's CONTRACT (raises on a non-deterministic / empty-composite / unknown
    op). Faithful enough that the determinism gate fires before purity in the compiler."""

    def validate(self, pattern):
        self._node(pattern)

    def _node(self, node):
        if not isinstance(node, dict):
            raise ValueError("predicate node must be a JSON object")
        op = node.get("op")
        if op in ("and", "or"):
            cl = node.get("clauses")
            if not isinstance(cl, list) or not cl:
                raise ValueError(f"{op!r} requires a NON-EMPTY clauses list")
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


class PermissiveValidator:
    """Accepts EVERYTHING — used to prove the compile-time kill-PURITY gate fires INDEPENDENTLY of the
    injected validator (substrate independence: the diverse substrate validates its own input even when
    the corpus validator was too lax / drifted)."""

    def validate(self, pattern):
        return None


class FakeBacktester:
    def replay(self, pattern, trigger_type, *, window_days):
        return {
            "fire_count": 4, "fire_count_exact": True,
            "fire_count_by_coverage": {"attested": 4, "unknown": 0, "unattested": 0},
            "scanned": 100,
            "field_engagement": {"from_domain": {"present": 80, "scanned": 100,
                                                 "present_fraction": 0.8, "ops": ["=="]}},
            "warnings": [], "coverage": {"honest_note": "ok", "attested_fraction": 1.0},
        }


PAT = {"op": "==", "field": "from_domain", "value": "substack.com"}
KILL = {"op": "==", "field": "from_domain", "value": "blocked.example"}
GOAL = (SubGoal(goal="summarize into a Doc", tools=("mail.read", "gdrive.create"), output="drive:Doc"),)


def guard(**kw) -> Guard:
    base = dict(rationale="this domain also sends marketing blasts — may over-fire",
                dissent_author="codex", kill_predicate=dict(KILL),
                kill_drill={"from_domain": "blocked.example"}, kill_authored_by="phill")
    base.update(kw)
    return Guard(**base)


def candidate(**kw):
    from levain.autonomic import CandidateBinding
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


def a_binding(**kw) -> Binding:
    base = dict(created_by="phill", created_at="2026-06-30T09:00:00",
                trigger=TriggerSpec(type="email", pattern=dict(PAT)), goal=GOAL,
                tightness=TightnessVector(0.9, 0.9, 1.0, 0.9), posture=Posture.CONFIRM)
    base.update(kw)
    return Binding.create(**base)


# ===============================================================================================
# PIECE 3 — the diverse, fail-safe kill evaluator (built first; pieces 2+4 ride on it)
# ===============================================================================================

def test_kleene_present_field_confident():
    assert kill_outcome({"op": "==", "field": "x", "value": "a"}, {"x": "a"}) is Kleene.TRUE
    assert kill_outcome({"op": "==", "field": "x", "value": "a"}, {"x": "b"}) is Kleene.FALSE


def test_kleene_absent_binary_op_is_unknown_and_trips():
    # THE H2 FOOTGUN, fixed: `dmarc != pass` on a no-dmarc event is UNKNOWN here (events.py returns
    # False → the kill silently fails to trip → the action fires on the worst case). UNKNOWN → trips.
    out = kill_outcome({"op": "!=", "field": "dmarc", "value": "pass"}, {})
    assert out is Kleene.UNKNOWN
    assert kill_trips({"op": "!=", "field": "dmarc", "value": "pass"}, {}) is True


def test_kleene_absent_blind_kill_trips_both_spellings():
    no_dmarc = {"from_domain": "x.example"}
    assert kill_trips({"op": "!=", "field": "dmarc", "value": "pass"}, no_dmarc) is True  # blind spelling
    assert kill_trips({"op": "not", "clause": {"op": "==", "field": "dmarc", "value": "pass"}},
                      no_dmarc) is True                                                    # safe spelling


def test_kleene_present_dmarc_resolves_correctly():
    k = {"op": "!=", "field": "dmarc", "value": "pass"}
    assert kill_outcome(k, {"dmarc": "fail"}) is Kleene.TRUE    # fail != pass → trips (the danger)
    assert kill_outcome(k, {"dmarc": "pass"}) is Kleene.FALSE   # pass != pass is False → lets through


def test_kleene_exists_is_well_defined_on_absence():
    assert kill_outcome({"op": "exists", "field": "dmarc"}, {}) is Kleene.FALSE
    assert kill_outcome({"op": "exists", "field": "dmarc"}, {"dmarc": "x"}) is Kleene.TRUE
    # the recommended absence-kill spelling trips CONFIDENTLY (not via fail-safe) on absence:
    assert kill_outcome({"op": "not", "clause": {"op": "exists", "field": "dmarc"}}, {}) is Kleene.TRUE


def test_kleene_and_or_not_three_valued():
    unk = {"op": "!=", "field": "missing", "value": "x"}   # UNKNOWN on an absent field
    tru = {"op": "==", "field": "a", "value": "1"}
    fls = {"op": "==", "field": "a", "value": "2"}
    ev = {"a": "1"}
    # and: a FALSE clause makes it FALSE even amid UNKNOWN; else UNKNOWN dominates a TRUE.
    assert kill_outcome({"op": "and", "clauses": [fls, unk]}, ev) is Kleene.FALSE
    assert kill_outcome({"op": "and", "clauses": [tru, unk]}, ev) is Kleene.UNKNOWN
    # or: a TRUE clause makes it TRUE even amid UNKNOWN; else UNKNOWN dominates a FALSE.
    assert kill_outcome({"op": "or", "clauses": [tru, unk]}, ev) is Kleene.TRUE
    assert kill_outcome({"op": "or", "clauses": [fls, unk]}, ev) is Kleene.UNKNOWN
    # not(UNKNOWN) is UNKNOWN
    assert kill_outcome({"op": "not", "clause": unk}, ev) is Kleene.UNKNOWN


def test_kleene_resolves_flat_and_fields_wrapped_drills():
    k = {"op": "==", "field": "dmarc", "value": "fail"}
    assert kill_outcome(k, {"dmarc": "fail"}) is Kleene.TRUE                 # flat drill
    assert kill_outcome(k, {"fields": {"dmarc": "fail"}}) is Kleene.TRUE      # event-shaped drill


def test_kleene_numeric_type_mismatch_is_unknown():
    # a numeric comparison against a non-numeric present field is UNKNOWN (fail-safe), not a silent
    # False (the events.py choice this substrate diverges from).
    assert kill_outcome({"op": ">", "field": "amount", "value": 100}, {"amount": "abc"}) is Kleene.UNKNOWN
    assert kill_outcome({"op": ">", "field": "amount", "value": 100}, {"amount": 500}) is Kleene.TRUE


def test_kleene_equality_type_mismatch_is_unknown():
    # codex L3: a present-but-type-INCOMPATIBLE equality compare is UNKNOWN, SYMMETRICALLY for == and
    # != (events.py returns False → a `==`-kill would fail-OPEN). A coercible string stays confident.
    eq = {"op": "==", "field": "amount", "value": 100}
    assert kill_outcome(eq, {"amount": "abc"}) is Kleene.UNKNOWN          # non-coercible string vs num
    assert kill_trips(eq, {"amount": "abc"}) is True                       # → trips (fail-safe)
    assert kill_outcome(eq, {"amount": "100"}) is Kleene.TRUE              # coercible string stays confident
    assert kill_outcome({"op": "!=", "field": "amount", "value": 100}, {"amount": "abc"}) is Kleene.UNKNOWN


def test_kleene_contains_type_mismatch_is_unknown():
    # codex L3: `contains` of a non-string value against a STRING field is unevaluable → UNKNOWN (not a
    # confident False); `contains` on a number field is UNKNOWN; a list field is a confident membership.
    assert kill_outcome({"op": "contains", "field": "subject", "value": 5}, {"subject": "hi"}) is Kleene.UNKNOWN
    assert kill_outcome({"op": "contains", "field": "n", "value": "x"}, {"n": 42}) is Kleene.UNKNOWN
    assert kill_outcome({"op": "contains", "field": "subject", "value": "inv"},
                        {"subject": "invoice"}) is Kleene.TRUE
    assert kill_outcome({"op": "contains", "field": "tags", "value": "vip"},
                        {"tags": ["vip", "x"]}) is Kleene.TRUE


def test_kill_outcome_never_raises_on_garbage():
    assert kill_outcome({"op": "bogus", "field": "x", "value": 1}, {"x": 1}) is Kleene.UNKNOWN
    assert kill_outcome("not even a dict", {"x": 1}) is Kleene.UNKNOWN
    assert kill_outcome({"op": "and", "clauses": []}, {"x": 1}) is Kleene.UNKNOWN  # empty composite


def test_assert_kill_pure_accepts_a_clean_kill():
    assert_kill_pure(KILL)
    assert_kill_pure({"op": "and", "clauses": [KILL, {"op": "exists", "field": "y"}]})


@pytest.mark.parametrize("bad", [
    {"op": "regex_match", "field": "x", "value": ".*"},   # unknown op
    {"op": "and", "clauses": []},                          # empty composite
    {"op": "==", "field": "", "value": "a"},               # empty field
    {"op": "==", "field": "x"},                            # missing value
    {"op": "==", "field": "x", "value": None},             # null value
    {"op": ">", "field": "x", "value": "abc"},             # non-numeric numeric threshold
    {1: "x"},                                              # non-string key (also no op)
])
def test_assert_kill_pure_rejects_impure(bad):
    with pytest.raises(KillImpurityError):
        assert_kill_pure(bad)


def test_assert_kill_pure_rejects_overdeep():
    node = {"op": "==", "field": "x", "value": "a"}
    for _ in range(40):
        node = {"op": "not", "clause": node}
    with pytest.raises(KillImpurityError):
        assert_kill_pure(node)


# ===============================================================================================
# PIECE 2 — the kill-drill compile gate
# ===============================================================================================

def test_drill_that_trips_confidently_compiles_no_nudge():
    r = compile_(candidate(guard=(guard(),)))   # KILL drill {from_domain: blocked.example} → TRUE
    assert r.ok
    assert not any("fail-safe" in n.lower() for n in r.surfacing.notes)


def test_drill_that_does_not_trip_is_refused():
    # the drill is the SAFE case (from_domain != blocked.example) → the kill lets it through → REFUSE.
    g = guard(kill_drill={"from_domain": "totally.fine.example"})
    r = compile_(candidate(guard=(g,)))
    assert not r.ok
    assert any("does not trip" in x.lower() or "lets through" in x.lower() for x in r.reasons)


def test_drill_that_trips_only_via_fail_safe_compiles_with_a_nudge():
    # kill `dmarc != pass`, drill with NO dmarc → trips via UNKNOWN (fail-safe), not positive logic.
    g = guard(kill_predicate={"op": "!=", "field": "dmarc", "value": "pass"},
              kill_drill={"from_domain": "x.example"})
    r = compile_(candidate(guard=(g,)))
    assert r.ok
    assert any("fail-safe" in n.lower() for n in r.surfacing.notes)


def test_impure_kill_refused_independently_of_validator():
    # PROVE the purity gate fires even when the injected validator is fully permissive (substrate
    # independence): an unknown-op kill the validator accepts is still refused at the purity gate.
    g = guard(kill_predicate={"op": "regex_match", "field": "x", "value": ".*"},
              kill_drill={"x": "anything"})
    r = compile_(candidate(guard=(g,)), validator=PermissiveValidator())
    assert not r.ok
    assert any("pure" in x.lower() for x in r.reasons)


def test_impure_predicted_trajectory_bound_refused():
    g = guard(predicted_trajectory={"bound": {"op": "regex_match", "field": "x", "value": ".*"}})
    r = compile_(candidate(guard=(g,)), validator=PermissiveValidator())
    assert not r.ok
    assert any("pure" in x.lower() or "trajectory" in x.lower() for x in r.reasons)


def test_impure_trajectory_bound_refused_even_without_kill():
    # codex L3 #4 / complement L3 LOW-1: a CALIBRATION-only guard (NO kill) carrying an impure
    # predicted_trajectory bound must STILL be refused — trajectory purity runs for EVERY guard. Use an
    # on-loop (fast-lane, no-kill-mandate) candidate so the refusal isolates the trajectory gate.
    g = Guard(rationale="watch", dissent_author="codex",
              predicted_trajectory={"bound": {"op": "regex_match", "field": "x", "value": ".*"}})
    strong = TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                          hops=0, human_present=False)
    r = compile_(candidate(guard=(g,), trust=strong), validator=PermissiveValidator())
    assert not r.ok
    assert any("trajectory" in x.lower() for x in r.reasons)


def test_empty_in_list_kill_rejected_as_vacuous():
    # complement L3 LOW-2: a kill `x in []` matches nothing → never trips → refused at the purity gate.
    with pytest.raises(KillImpurityError):
        assert_kill_pure({"op": "in", "field": "x", "value": []})


def test_stale_readd_does_not_strip_a_tightening(tmp_path):
    # Reproduced at the 2026-10-06 fold: a writer holding the sealed grant from before a
    # tighten_guard re-adds it; the kill added since must survive (tightening is monotone).
    store = BindingStore(tmp_path / "b.json")
    b = a_binding()
    store.add(b)
    store.tighten_guard(b.binding_id, guard(spike_id="later-kill"))
    store.add(b)
    assert [g.spike_id for g in store.get(b.binding_id).guard_additions] == ["later-kill"]


def test_readd_unions_incoming_and_existing_tightenings(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding()
    store.add(b)
    store.tighten_guard(b.binding_id, guard(spike_id="on-disk"))
    incoming = replace(b, guard_additions=(guard(spike_id="on-disk"), guard(spike_id="incoming")))
    store.add(incoming)
    assert [g.spike_id for g in store.get(b.binding_id).guard_additions] == ["on-disk", "incoming"]


def test_claim_one_shot_refuses_what_is_fireable_refuses(tmp_path):
    # Reproduced 2026-10-06 (codex, code L3 r1 of the fold): an ACTIVE confirm-class one-shot with no
    # sealed kill is excluded by list_active, yet claim_one_shot returned it, and that snapshot could
    # mint authority. The claim must use the same fire-view predicate.
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(posture=Posture.CONFIRM, one_shot=True, guard=(), status=BindingStatus.ACTIVE)
    store.add(b)
    assert not BindingStore.is_fireable(store.get(b.binding_id))
    assert store.claim_one_shot(b.binding_id) is None
    assert store.get(b.binding_id).status is BindingStatus.ACTIVE   # not spent by a refused claim


def test_tighten_guard_rejects_untripping_drill(tmp_path):
    # complement L3 MED-1: the tighten path is NOT a second-class compile citizen — a tightening kill
    # whose drill does NOT trip it is refused (the drill-trip gate, parity with compile_binding).
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    bad = guard(kill_predicate={"op": "==", "field": "status", "value": "danger"},
                kill_drill={"status": "safe"})   # drill is the SAFE case → kill lets it through
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, bad)


# ===============================================================================================
# PIECE 1 — monotonic-editability (the unsealed tightening tier + tighten_guard)
# ===============================================================================================

def test_tighten_guard_preserves_id_and_seal(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    extra = guard(spike_id="extra", rationale="a second, tighter watch", kill_drill={"from_domain": "blocked.example"})
    updated = store.tighten_guard(b.binding_id, extra)
    assert updated is not None
    assert updated.binding_id == b.binding_id              # TIGHTEN-FREE: id stable (no re-mint)
    assert updated.seal_matches()                          # the floor is untouched → seal holds
    assert updated.guard == b.guard                        # floor unchanged
    assert updated.guard_additions == (extra,)             # the addition landed in the unsealed tier
    assert updated.effective_guard == b.guard + (extra,)
    # persisted + reloaded identically
    reloaded = store.get(b.binding_id)
    assert reloaded.guard_additions == (extra,) and reloaded.seal_matches()


def test_tighten_guard_preserves_graduation(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    store.record_fire(b.binding_id, clean=True, fired_at="2026-06-30T10:00:00")
    store.record_fire(b.binding_id, clean=True, fired_at="2026-06-30T11:00:00")
    updated = store.tighten_guard(b.binding_id, guard(spike_id="extra"))
    assert updated.graduation.fire_count == 2 and updated.graduation.clean_count == 2  # evidence kept


def test_guard_additions_do_not_touch_the_seal():
    b = a_binding(guard=(guard(),))
    with_add = dataclasses.replace(b, guard_additions=(guard(spike_id="extra"),))
    assert with_add.seal_matches()                         # additions are UNSEALED → seal still holds
    assert with_add.binding_id == b.binding_id
    # and stripping an addition stays within the floor → seal STILL holds (never below the floor)
    stripped = dataclasses.replace(with_add, guard_additions=())
    assert stripped.seal_matches()


def test_loosening_the_floor_breaks_the_seal():
    # the asymmetry: removing/weakening a FLOOR guard touches the sealed basis → mismatch → barred.
    b = a_binding(guard=(guard(),))
    assert not dataclasses.replace(b, guard=()).seal_matches()


def test_guard_additions_round_trip_and_backward_compat():
    b = dataclasses.replace(a_binding(guard=(guard(),)), guard_additions=(guard(spike_id="extra"),))
    again = Binding.from_dict(b.to_dict())
    assert again.guard_additions == b.guard_additions and again.seal_matches()
    # a pre-3a.5 record (no guard_additions key) loads as () + seals stable
    d = b.to_dict()
    d.pop("guard_additions")
    legacy = Binding.from_dict(d)
    assert legacy.guard_additions == () and legacy.seal_matches()


def test_tighten_guard_refuses_inert_binding(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.REVOKED)
    store.add(b)
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, guard(spike_id="extra"))


def test_tighten_guard_rejects_impure_addition_kill(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    bad = guard(kill_predicate={"op": "regex_match", "field": "x", "value": ".*"},
                kill_drill={"x": "y"})
    with pytest.raises(KillImpurityError):
        store.tighten_guard(b.binding_id, bad)


def test_tighten_guard_absent_and_empty(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    assert store.tighten_guard("bind-nope", guard()) is None        # absent → None
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    assert store.tighten_guard(b.binding_id).binding_id == b.binding_id  # empty tighten → no-op


def test_confirm_class_kill_must_be_in_the_sealed_floor(tmp_path):
    # an addition CANNOT satisfy the confirm-class mandatory-kill gate (a mandatory kill must be sealed).
    store = BindingStore(tmp_path / "b.json")
    # a confirm-class binding with NO sealed kill, then a kill added ONLY to the unsealed tier:
    bare = a_binding(guard=(Guard(rationale="watch", dissent_author="codex"),),  # calibration-only floor
                     status=BindingStatus.ACTIVE, posture=Posture.CONFIRM)
    store.add(bare)
    store.tighten_guard(bare.binding_id, guard(spike_id="late-kill"))   # kill only in additions
    assert bare.binding_id not in {x.binding_id for x in store.list_active()}  # STILL barred
    # whereas a sealed-floor kill makes it fireable
    sealed = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM)
    store.add(sealed)
    assert sealed.binding_id in {x.binding_id for x in store.list_active()}


# ===============================================================================================
# PIECE 4 — the prediction-error monitor (the primitive + the gate-runtime KILL)
# ===============================================================================================

TRAJ = {"bound": {"op": "==", "field": "status", "value": "ok"}}


def test_prediction_diverged_primitive():
    assert prediction_diverged(TRAJ, {"status": "ok"})[0] is False        # within envelope
    assert prediction_diverged(TRAJ, {"status": "bad"})[0] is True        # violated → diverged
    assert prediction_diverged(TRAJ, {})[0] is True                       # unconfirmable → diverged
    assert prediction_diverged({"note": "descriptive only"}, {})[0] is False  # no bound → inert
    assert within_envelope(TRAJ, {"status": "ok"}) is Kleene.TRUE
    assert within_envelope(TRAJ, {"status": "bad"}) is Kleene.FALSE
    assert within_envelope(TRAJ, {}) is Kleene.UNKNOWN


MANIFEST = ActionManifest({
    "deliver_as_document": ActionRisk(RiskClass.LOW, reversible=True, external=False, financial=False),
})


@dataclass
class RecordingExecutor:
    name: str = "recording"
    result: ExecutionResult = field(
        default_factory=lambda: ExecutionResult(ok=True, detail="wrote doc", downstream_id="doc-1"))
    calls: list = field(default_factory=list)

    def execute(self, action_name, payload, *, context_id):
        self.calls.append((action_name, payload, context_id))
        return self.result


@dataclass
class StubObserver:
    name: str = "stub"
    actual: dict = field(default_factory=dict)
    raise_exc: bool = False
    bad_shape: bool = False
    calls: list = field(default_factory=list)

    def observe(self, action_name, payload, *, context_id):
        self.calls.append((action_name, payload, context_id))
        if self.raise_exc:
            raise RuntimeError("observer boom")
        if self.bad_shape:
            return "not a dict"
        return self.actual


def _clock():
    return _dt.datetime(2026, 6, 30, 12, 0, 0)


def make_gate(tmp_path, observer=None):
    ex = RecordingExecutor()
    store = GateReceiptStore(tmp_path / "receipts.jsonl")
    gate = EfferentGate(manifest=MANIFEST, store=store, executor=ex, clock=_clock,
                        trajectory_observer=observer)
    return gate, ex, store


def autonomous_request(**kw) -> ActionRequest:
    # STRONG auth + intent_bearing + human_present → on-loop fire (by=on-loop, the monitor path).
    base = dict(action_name="deliver_as_document", payload="deliver the digest", context_id="ctx-1",
                query_text="raw signals", query_date="2026-06-30",
                trust=TrustContext(signal_auth=SignalAuth.STRONG,
                                   intent_provenance=IntentProvenance.INTENT_BEARING, human_present=True),
                grounded=True, authority=manual_invocation(), producers=("minimax-m3",))
    base.update(kw)
    return ActionRequest(**base)


def test_monitor_within_envelope_fires(tmp_path):
    gate, ex, store = make_gate(tmp_path, observer=StubObserver(actual={"status": "ok"}))
    out = gate.gate(autonomous_request(predicted_trajectory=TRAJ))
    assert out.fired and not out.killed and out.approved
    assert ex.calls and store.read()[0].action_face["terminal_state"] == "fired"


def test_monitor_divergence_kills_pre_execute(tmp_path):
    obs = StubObserver(actual={"status": "DRIFTED"})
    gate, ex, store = make_gate(tmp_path, observer=obs)
    out = gate.gate(autonomous_request(predicted_trajectory=TRAJ))
    assert out.killed and not out.fired and not out.approved and not out.refused
    assert ex.calls == []                                  # the effect NEVER fired
    face = store.read()[0].action_face
    assert face["terminal_state"] == "killed" and face["refuse_class"] == "kill_triggered"
    assert face["gate"]["verdict"] == "denied"             # killed mode lives in terminal_state, not verdict


def test_monitor_unconfirmable_observation_kills(tmp_path):
    gate, ex, store = make_gate(tmp_path, observer=StubObserver(actual={}))  # no `status` → UNKNOWN
    out = gate.gate(autonomous_request(predicted_trajectory=TRAJ))
    assert out.killed and ex.calls == []


def test_monitor_observer_fault_fails_safe_to_kill(tmp_path):
    gate, ex, _ = make_gate(tmp_path, observer=StubObserver(raise_exc=True))
    assert gate.gate(autonomous_request(predicted_trajectory=TRAJ)).killed and ex.calls == []
    gate2, ex2, _ = make_gate(tmp_path, observer=StubObserver(bad_shape=True))
    assert gate2.gate(autonomous_request(predicted_trajectory=TRAJ)).killed and ex2.calls == []


def test_monitor_inert_without_observer_or_trajectory(tmp_path):
    # no observer wired → inert (fires) even with a trajectory present
    g1, ex1, _ = make_gate(tmp_path, observer=None)
    assert g1.gate(autonomous_request(predicted_trajectory=TRAJ)).fired
    # observer wired but NO trajectory → inert (fires); the observer is never consulted
    obs = StubObserver(actual={"status": "DRIFTED"})
    g2, ex2, _ = make_gate(tmp_path, observer=obs)
    assert g2.gate(autonomous_request()).fired and obs.calls == []


def test_monitor_skipped_for_confirm_class_fire(tmp_path):
    # the monitor gates on posture.fires_immediately (codex L3) — a CONFIRM-class fire is human-gated
    # (the propose/cancel window per §2.1) and does NOT run it, even with a diverging observer. White-box
    # on _fire with a confirm-class posture + by=human (the resolve-route shape).
    obs = StubObserver(actual={"status": "DRIFTED"})
    gate, ex, _ = make_gate(tmp_path, observer=obs)
    out = gate._fire(request=autonomous_request(predicted_trajectory=TRAJ), created_at=_clock().isoformat(),
                     posture=Posture.CONFIRM, verdict="approved", by="human", actor_first_estimate=None)
    assert out.fired and not out.killed and obs.calls == []   # confirm-class → monitor never ran


def test_monitor_gates_on_posture_not_by(tmp_path):
    # codex L3 findings 2+5: a cooling-off TIMEOUT auto-fire is by="on-loop" but POSTURE=COOLING_OFF
    # (confirm-class, fires_immediately=False) → the monitor SKIPS it (gated on posture, NOT by). So the
    # predicted_trajectory dropped through the pending is moot, and §2.1 (confirm-class is human-gated
    # via the cancel window) holds. Proves the gate is the posture, not the `by` string.
    obs = StubObserver(actual={"status": "DRIFTED"})
    gate, ex, _ = make_gate(tmp_path, observer=obs)
    out = gate._fire(request=autonomous_request(predicted_trajectory=TRAJ), created_at=_clock().isoformat(),
                     posture=Posture.COOLING_OFF, verdict="auto", by="on-loop", actor_first_estimate=None)
    assert out.fired and not out.killed and obs.calls == []   # confirm-class posture → monitor skipped


def test_normal_deny_carries_refused_terminal_state(tmp_path):
    gate, _, store = make_gate(tmp_path)
    gate.gate(autonomous_request(action_name="unknown_action"))   # unknown → deny
    assert store.read()[0].action_face["terminal_state"] == "refused"


# ===============================================================================================
# PIECE 5 — liveness telemetry
# ===============================================================================================

def test_binding_liveness_counts_the_live_shape(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    active = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(active)
    store.tighten_guard(active.binding_id, guard(spike_id="extra",
                                                 predicted_trajectory=TRAJ))   # an addition w/ a monitor
    store.add(a_binding(created_at="2026-06-30T09:00:01", guard=(guard(),), status=BindingStatus.PAUSED))
    store.add(a_binding(created_at="2026-06-30T09:00:02", guard=(guard(),), status=BindingStatus.REVOKED))
    stats = binding_liveness(store)
    assert stats["total"] == 3
    assert stats["by_status"]["active"] == 1 and stats["by_status"]["revoked"] == 1
    assert stats["fireable"] == 1                                  # only the active+sealed+kill one
    assert stats["guards_floor"] == 3 and stats["kill_bearing_floor"] == 3
    assert stats["guards_additions"] == 1 and stats["kill_bearing_additions"] == 1
    assert stats["with_predicted_trajectory"] == 1 and stats["with_monitor_bound"] == 1


def test_binding_liveness_flags_barred_seal_mismatch(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    # tamper the on-disk floor (drop the guard) WITHOUT recomputing the id → an active seal-mismatch
    import json
    p = tmp_path / "b.json"
    recs = json.loads(p.read_text())
    recs[0]["guard"] = []
    p.write_text(json.dumps(recs))
    stats = binding_liveness(store)
    assert stats["barred_seal_mismatch"] == 1 and stats["fireable"] == 0


def test_binding_liveness_flags_confirm_without_sealed_kill(tmp_path):
    store = BindingStore(tmp_path / "b.json")
    bare = a_binding(guard=(Guard(rationale="watch", dissent_author="codex"),),
                     status=BindingStatus.ACTIVE, posture=Posture.CONFIRM)
    store.add(bare)
    stats = binding_liveness(store)
    assert stats["barred_confirm_no_kill"] == 1 and stats["fireable"] == 0


def test_gate_liveness_counts_terminal_states(tmp_path):
    obs_div = StubObserver(actual={"status": "DRIFTED"})
    gate, ex, store = make_gate(tmp_path, observer=obs_div)
    gate.gate(autonomous_request(predicted_trajectory=TRAJ))           # → killed
    gate.gate(autonomous_request(action_name="unknown_action"))       # → refused
    gate2, _, _ = make_gate(tmp_path, observer=StubObserver(actual={"status": "ok"}))
    gate2._store = store
    gate2.gate(autonomous_request(predicted_trajectory=TRAJ))          # → fired
    stats = gate_liveness(store)
    assert stats["total"] == 3
    assert stats["killed"] == 1 and stats["fired"] == 1
    assert stats["by_terminal_state"]["killed"] == 1
    assert stats["by_terminal_state"]["refused"] == 1
    assert stats["by_terminal_state"]["fired"] == 1
    assert stats["by_refuse_class"].get("kill_triggered") == 1
    assert stats["by_verdict"]["denied"] >= 2 and stats["by_verdict"]["auto"] == 1
