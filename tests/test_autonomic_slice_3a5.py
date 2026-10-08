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

from tests.test_autonomic_rawstore import dump, registry_of, write_raw

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
    store = BindingStore(tmp_path / "b")
    b = a_binding()
    store.add(b)
    store.tighten_guard(b.binding_id, guard(spike_id="later-kill"))
    store.add(b)
    assert [g.spike_id for g in store.get(b.binding_id).guard_additions] == ["later-kill"]


def test_a_re_add_carrying_tightenings_refuses_rather_than_dropping_them(tmp_path):
    # add no longer merges (S1h-3). A no-op would silently drop the incoming kill, so it raises.
    store = BindingStore(tmp_path / "b")
    b = a_binding()
    assert store.add(b) is True
    store.tighten_guard(b.binding_id, guard(spike_id="on-disk"))
    before = dump(store)
    incoming = replace(b, guard_additions=(guard(spike_id="on-disk"), guard(spike_id="incoming")))
    with pytest.raises(ValueError, match="use tighten_guard"):
        store.add(incoming)
    assert dump(store) == before
    assert store.add(b) is False                     # a bare re-add is a no-op
    assert [g.spike_id for g in store.get(b.binding_id).guard_additions] == ["on-disk"]


def test_admit_refuses_what_is_fireable_refuses(tmp_path):
    # Reproduced 2026-10-06 (codex, code L3 r1 of the fold): an ACTIVE confirm-class one-shot with no
    # sealed kill is excluded by list_active, yet the one-shot claim returned it, and that snapshot could
    # mint authority. The claim (now admit's) must use the same fire-view predicate.
    from levain.autonomic import RunJournal
    store = BindingStore(tmp_path / "b", journal=RunJournal(tmp_path / "b"))
    b = a_binding(posture=Posture.CONFIRM, one_shot=True, guard=(), status=BindingStatus.ACTIVE)
    store.add(b)
    assert not BindingStore.is_fireable(store.get(b.binding_id))
    assert store.admit(b.binding_id, "run-1") is None
    assert store.get(b.binding_id).status is BindingStatus.ACTIVE   # not spent by a refused claim


def _raw(store):
    import json
    return list(registry_of(store).values())


def _write(store, records):
    write_raw(store, {r["binding_id"]: r for r in records})


def test_migrating_a_legacy_list_with_a_duplicate_is_refused(tmp_path):
    # Reproduced 2026-10-06 (S1h): with two valid same-id records [PAUSED, REVOKED], ratify acted on
    # the first and resurrected the revoked grant. A vagus-format list that holds two records for one
    # id is refused at migration; the store cannot represent it.
    import json
    b = a_binding(guard=(guard(),))
    rec = b.to_dict()
    for second in (dict(rec, status="revoked"), dict(rec, status="revoked", goal="not-a-goal"),
                   dict(rec, status="garbage")):
        src = tmp_path / "legacy.json"
        src.write_text(json.dumps([rec, second]))
        store = BindingStore(tmp_path / f"s-{second['status']}-{len(str(second))}")
        with pytest.raises(ValueError, match=f"duplicate records for .*{b.binding_id}.*nothing imported"):
            store.migrate_json(src)
        assert store.list_all() == []


def test_add_creates_only_and_never_merges_into_a_stored_record(tmp_path):
    # codex S1h r2 (a): add() of an existing id "repaired" a record. add is create-only: an existing id
    # is never written over, whatever state its record is in.
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    store.set_status(b.binding_id, BindingStatus.REVOKED)
    before = dump(store)
    assert store.add(b) is False
    assert dump(store) == before and store.list_active() == []


def test_migrating_a_vagus_format_list_imports_it_once(tmp_path):
    import json
    a = a_binding(guard=(guard(),), status=BindingStatus.PAUSED)
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    src = tmp_path / "legacy.json"
    src.write_text(json.dumps([a.to_dict(), b.to_dict()]))
    store = BindingStore(tmp_path / "store")
    assert store.migrate_json(src) == 2
    assert (tmp_path / "legacy.json.migrated-backup").read_text() == src.read_text()
    assert [x.binding_id for x in store.list_all()] == [a.binding_id, b.binding_id]
    assert [x.binding_id for x in store.list_active()] == [b.binding_id]
    assert json.loads(store.db.meta("migrated_from"))["records"] == 2
    with pytest.raises(ValueError, match="holds .or has held. a registry"):
        store.migrate_json(src)                                   # once
    assert store.ratify(a.binding_id) is not None


def test_a_migration_is_once_even_after_every_binding_is_removed(tmp_path):
    # L1 on 4ae4a00: the once-check counted bindings, so a store emptied by removes took a second import
    import json
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    src = tmp_path / "legacy.json"
    src.write_text(json.dumps([a.to_dict()]))
    store = BindingStore(tmp_path / "store")
    assert store.migrate_json(src) == 1
    assert store.remove(a.binding_id)
    assert store.list_all() == []
    with pytest.raises(ValueError, match="registry"):
        store.migrate_json(src)
    assert store.list_all() == []


def test_a_store_with_run_state_refuses_a_migration_and_writes_no_backup(tmp_path):
    import json
    from levain.autonomic import RunJournal
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    src = tmp_path / "legacy.json"
    src.write_text(json.dumps([a.to_dict()]))
    RunJournal(tmp_path / "store").fence("someone")
    store = BindingStore(tmp_path / "store")
    with pytest.raises(ValueError, match="already holds fences"):
        store.migrate_json(src)
    assert not (tmp_path / "legacy.json.migrated-backup").exists()   # refused before anything is written
    assert store.list_all() == []


def test_migrating_the_object_format_imports_it(tmp_path):
    import json
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    src = tmp_path / "registry.json"
    src.write_text(json.dumps({a.binding_id: a.to_dict()}))
    store = BindingStore(tmp_path / "store")
    assert store.migrate_json(src) == 1
    assert [x.binding_id for x in store.list_active()] == [a.binding_id]


@pytest.mark.parametrize("extra", [{"posture": "CONFIRM"}, "not-a-record", 5, None])
def test_migrating_a_list_entry_that_proves_no_identity_is_refused(tmp_path, extra):
    # An entry with no derivable identity cannot be keyed or checked (S1h-4): nothing is imported
    import json
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    src = tmp_path / "legacy.json"
    src.write_text(json.dumps([b.to_dict(), extra]))
    store = BindingStore(tmp_path / "store")
    with pytest.raises(ValueError, match="legacy entry 1.*nothing imported"):
        store.migrate_json(src)
    assert store.list_all() == []


def test_a_re_add_never_writes_over_a_stored_impure_tightening(tmp_path):
    # codex S1h r2 HIGH (b) was a merged record carrying an unvalidated stored kill; with no merge
    # there is no carried record: the re-add writes nothing (S1h-3)
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    rec = _raw(store)[0]
    bad = dict(guard().to_dict(), kill_predicate={"field": "x", "op": "regex_sub", "value": "y"})
    _write(store, [dict(rec, guard_additions=[bad])])
    before = dump(store)
    assert store.add(b) is False
    assert dump(store) == before


def test_supersede_onto_an_existing_new_id_aborts(tmp_path):
    # the target id already exists (here ACTIVE): replace_atomic writes nothing and returns False,
    # so neither grant is stranded and no bookkeeping is chosen between (S1h-3)
    store = BindingStore(tmp_path / "b")
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    b_active = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    store.add(a)
    store.add(b_active)
    before = dump(store)
    b_paused = a_binding(guard=(guard(),), posture=Posture.CONFIRM_ELEVATED)   # same id, default PAUSED
    result = store.replace_atomic(a, b_paused)
    assert not result and result.reason == "target_exists"
    assert dump(store) == before
    assert {x.binding_id for x in store.list_active()} == {a.binding_id, b_active.binding_id}


def test_a_present_non_dict_trajectory_bound_is_refused(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    bad = guard(kill_predicate=None, kill_drill=None, kill_authored_by=None, spike_id="t",
                predicted_trajectory={"bound": "not-a-predicate"})
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, bad)
    with pytest.raises(ValueError):
        store.add(a_binding(guard=(bad,), posture=Posture.CONFIRM_ELEVATED))


def test_nested_fields_this_version_does_not_know_survive(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(status=BindingStatus.ACTIVE, guard=(guard(),))
    store.add(b)
    store.tighten_guard(b.binding_id, guard(spike_id="first"))
    rec = _raw(store)[0]
    rec["graduation"]["future_counter"] = 7
    rec["guard_additions"][0]["future_safety"] = True
    _write(store, [rec])
    store.record_fire(b.binding_id, clean=True, fired_at="2026-07-01T00:00:00")
    store.tighten_guard(b.binding_id, guard(spike_id="second"))
    rec = _raw(store)[0]
    assert rec["graduation"]["future_counter"] == 7 and rec["graduation"]["fire_count"] == 1
    assert rec["guard_additions"][0]["future_safety"] is True and len(rec["guard_additions"]) == 2
    store.add(b)                                     # a re-add carries them too (S1h r2)
    rec = _raw(store)[0]
    assert rec["graduation"]["future_counter"] == 7 and rec["graduation"]["fire_count"] == 1
    assert rec["guard_additions"][0]["future_safety"] is True and len(rec["guard_additions"]) == 2


def test_trigger_pattern_copies_are_deep(tmp_path):
    # Reproduced 2026-10-06 (S1h): to_dict()/from_dict() copied the predicate shallowly, so editing a
    # nested clause of the output edited the live trigger.
    t = TriggerSpec(type="email", pattern={"op": "and", "clauses": [{"field": "a", "op": "==", "value": 1}]})
    t.to_dict()["pattern"]["clauses"].append({"field": "evil", "op": "==", "value": 2})
    assert len(t.pattern["clauses"]) == 1
    d = {"type": "email", "pattern": {"op": "and", "clauses": [{"field": "a", "op": "==", "value": 1}]}}
    t2 = TriggerSpec.from_dict(d)
    d["pattern"]["clauses"].clear()
    assert len(t2.pattern["clauses"]) == 1


def test_record_fire_and_tighten_keep_fields_this_version_does_not_know(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(status=BindingStatus.ACTIVE, guard=(guard(),))
    store.add(b)
    _write(store, [dict(_raw(store)[0], labels={"conf": ["flow"]})])
    store.record_fire(b.binding_id, clean=True, fired_at="2026-07-01T00:00:00")
    store.tighten_guard(b.binding_id, guard(spike_id="t"))
    rec = _raw(store)[0]
    assert rec["labels"] == {"conf": ["flow"]}
    assert rec["graduation"]["fire_count"] == 1 and len(rec["guard_additions"]) == 1


def test_tighten_guard_refuses_an_impure_trajectory_bound(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(status=BindingStatus.ACTIVE, guard=(guard(),))
    store.add(b)
    bad = guard(kill_predicate=None, kill_drill=None, kill_authored_by=None, spike_id="traj",
                predicted_trajectory={"bound": {"op": "regex_match", "field": "x", "value": ".*"}})
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, bad)
    assert store.get(b.binding_id).guard_additions == ()


def test_replace_back_to_a_revoked_core_aborts_and_keeps_the_live_grant(tmp_path):
    # Reproduced 2026-10-06 (S1h): A -> B -> A returned True and left both REVOKED (nothing live).
    store = BindingStore(tmp_path / "b")
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    store.add(a)
    assert store.replace_atomic(a, b)
    result = store.replace_atomic(b, a)
    assert not result and result.reason == "target_exists"
    assert [x.binding_id for x in store.list_active()] == [b.binding_id]


def test_tighten_guard_rejects_untripping_drill(tmp_path):
    # complement L3 MED-1: the tighten path is NOT a second-class compile citizen — a tightening kill
    # whose drill does NOT trip it is refused (the drill-trip gate, parity with compile_binding).
    store = BindingStore(tmp_path / "b")
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
    store = BindingStore(tmp_path / "b")
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
    store = BindingStore(tmp_path / "b")
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
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.REVOKED)
    store.add(b)
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, guard(spike_id="extra"))


def test_tighten_guard_rejects_impure_addition_kill(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    bad = guard(kill_predicate={"op": "regex_match", "field": "x", "value": ".*"},
                kill_drill={"x": "y"})
    with pytest.raises(KillImpurityError):
        store.tighten_guard(b.binding_id, bad)


def test_tighten_guard_absent_and_empty(tmp_path):
    store = BindingStore(tmp_path / "b")
    assert store.tighten_guard("bind-nope", guard()) is None        # absent → None
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    assert store.tighten_guard(b.binding_id).binding_id == b.binding_id  # empty tighten → no-op


def test_confirm_class_kill_must_be_in_the_sealed_floor(tmp_path):
    # an addition CANNOT satisfy the confirm-class mandatory-kill gate (a mandatory kill must be sealed).
    store = BindingStore(tmp_path / "b")
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
    confined = True   # a test double: declares the floor a real binding executor runs under
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
    store = BindingStore(tmp_path / "b")
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


def test_binding_liveness_reports_a_registry_made_inert_by_a_tampered_floor(tmp_path):
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    assert binding_liveness(store)["registry_corrupt"] is None
    recs = registry_of(store)
    recs[b.binding_id]["guard"] = []                 # drop the guard WITHOUT recomputing the id
    write_raw(store, recs)
    stats = binding_liveness(store)
    assert stats["fireable"] == 0 and stats["total"] == 0
    assert "seals to" in stats["registry_corrupt"]     # inert, and it says so (not "no bindings")


def test_binding_liveness_flags_confirm_without_sealed_kill(tmp_path):
    store = BindingStore(tmp_path / "b")
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


def test_replace_atomic_validates_the_proposal_before_any_early_return(tmp_path):
    # L3 S1h-2 (codex LOW, complement LOW): an invalid proposal returned False when the old grant
    # was absent, indistinguishable from a lost race. It is validated first and raises.
    store = BindingStore(tmp_path / "b")
    bad = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    tampered = replace(bad, posture=Posture.ON_LOOP)      # same id, different core: seal-broken
    never_stored = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM)
    with pytest.raises(ValueError, match="seal-broken"):
        store.replace_atomic(never_stored, tampered)
    assert not store.path.exists()


def test_a_declared_bound_that_is_not_a_predicate_fails_closed():
    """codex S1h-2 HIGH 2, deleted: ``{"bound": None}`` was accepted and measured identical to no bound
    (monitor inert). An absent envelope is not consent: a declared bound must be a predicate. Refused at
    bind time; one that reaches the fire path anyway reads as diverged. Only an OMITTED key is
    descriptive."""
    from levain.autonomic.kill import Kleene, KillImpurityError
    from levain.autonomic.monitor import assert_trajectory_pure, prediction_diverged, within_envelope
    for bad in (None, "field == 1", [], 0):
        with pytest.raises(KillImpurityError):
            assert_trajectory_pure({"summary": "x", "bound": bad})
        assert within_envelope({"bound": bad}, {"field": 1}) is Kleene.UNKNOWN
        assert prediction_diverged({"bound": bad}, {"field": 1})[0] is True
    assert_trajectory_pure({"summary": "x"})                     # no key: descriptive, passes
    assert prediction_diverged({"summary": "x"}, {"field": 1})[0] is False


def test_a_null_bound_cannot_be_persisted_or_tightened(tmp_path):
    store = BindingStore(tmp_path / "b")
    null_bound = Guard(rationale="watch", dissent_author="codex", predicted_trajectory={"bound": None})
    with pytest.raises(ValueError):
        store.add(a_binding(guard=(guard(), null_bound), status=BindingStatus.ACTIVE))
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    with pytest.raises(ValueError):
        store.tighten_guard(b.binding_id, null_bound)
    assert store.get(b.binding_id).guard_additions == ()



def _malformed_revoked(store, binding):
    recs = {r["binding_id"]: r for r in _raw(store)}
    recs[binding.binding_id] = dict(recs[binding.binding_id], status="revoked", goal="not-a-goal")
    _write(store, list(recs.values()))


def test_add_cannot_revive_a_single_malformed_revoked_record(tmp_path):
    # L1+L2 S1h-2, reproduced on fb93c3b: the class of S1h r2 (a) without a duplicate. A revoked
    # grant whose one record is malformed reads inert; a re-add wrote it back ACTIVE and fireable.
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    store.set_status(b.binding_id, BindingStatus.REVOKED)
    _malformed_revoked(store, b)
    before = dump(store)
    assert store.list_active() == []
    with pytest.raises(ValueError, match="proves no identity"):   # S1h-4: the registry is corrupt
        store.add(b)
    assert dump(store) == before and store.list_active() == []


def test_add_cannot_revive_a_seal_broken_record(tmp_path):
    # L3 S1h-2 codex HIGH, reproduced: add(original) over a tampered ACTIVE record restored the
    # valid core under the stored ACTIVE status, fireable without ratification
    import json
    store = BindingStore(tmp_path / "b")
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(b)
    raw = registry_of(store)
    raw[b.binding_id]["posture"] = "ABOVE_LOOP"
    write_raw(store, raw)
    before = dump(store)
    assert store.list_active() == []
    with pytest.raises(ValueError, match="seals to"):             # S1h-4: the registry is corrupt
        store.add(b)
    assert dump(store) == before and store.list_active() == []


def test_replace_atomic_cannot_revive_a_malformed_revoked_target(tmp_path):
    store = BindingStore(tmp_path / "b")
    old = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    new = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    store.add(old)
    store.add(new)
    store.set_status(new.binding_id, BindingStatus.REVOKED)
    _malformed_revoked(store, new)
    before = dump(store)
    with pytest.raises(ValueError, match="proves no identity"):   # S1h-4: the registry is corrupt
        store.replace_atomic(old, new)
    assert dump(store) == before
    assert store.list_active() == []                              # and inert, whole


def test_a_record_that_would_not_read_back_is_never_written(tmp_path):
    # L3 S1h-3 codex MED, reproduced: Graduation(-1, -1) constructs and seals, but the reader refuses
    # it, so add wrote an unreadable record and replace_atomic revoked a good grant for it
    from levain.autonomic.binding import Graduation
    store = BindingStore(tmp_path / "b")
    bad_grad = Graduation(fire_count=-1, clean_count=-1)
    with pytest.raises(ValueError, match="would not read back"):
        store.add(a_binding(guard=(guard(),), graduation=bad_grad))
    assert not store.path.exists()
    old = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    store.add(old)
    before = dump(store)
    new = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED,
                    graduation=bad_grad)
    with pytest.raises(ValueError, match="would not read back"):
        store.replace_atomic(old, new)
    assert dump(store) == before
    assert [x.binding_id for x in store.list_active()] == [old.binding_id]


def test_a_migration_whose_records_do_not_read_back_imports_nothing(tmp_path, monkeypatch):
    import json
    a = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE)
    b = a_binding(guard=(guard(),), status=BindingStatus.ACTIVE, posture=Posture.CONFIRM_ELEVATED)
    src = tmp_path / "legacy.json"
    src.write_text(json.dumps([a.to_dict(), b.to_dict()]))
    store = BindingStore(tmp_path / "store")
    real = BindingStore._write_raw
    monkeypatch.setattr(BindingStore, "_write_raw", lambda self, recs, conn: real(self, recs[:1], conn))
    with pytest.raises(ValueError, match="read back differ"):
        store.migrate_json(src)
    monkeypatch.undo()
    assert store.list_all() == [] and store.db.meta("migrated_from") is None
