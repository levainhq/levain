"""Phase-2 Slice-3a tests — the compiled binding + the delegated-authority registry.

Deterministic units (tmp_path stores, fixed strings) cover: the schema round-trip
(to_dict/from_dict for every nested type, incl. INTEGER tightness round-tripping seal-stable — the
L1-H1 regression), the content-fingerprint SEAL (deterministic id; seal_matches True clean; tamper of
each immutable-core field incl. the newly-sealed `one_shot` → mismatch; status/graduation OUTSIDE the
seal → id stable across record_fire/set_status), STRICT parsing (bad bool / out-of-range tightness /
NaN / clean>fire / unknown posture·status / empty goal / non-object pattern / non-string tools all
rejected), the registry (add/get/list_all/list_active/set_status/record_fire/replace_atomic/remove),
the GOVERNED lifecycle (set_status transition policy: REVOKED terminal, no inert→active, L2-H1; add
preserves on-disk status+graduation so a re-add can't resurrect a revoked grant or wipe evidence,
L2-H1/L1-M3; replace_atomic re-ratifies atomically, L2-M3), the FIRE-PATH guard (list_active excludes
inactive AND seal-mismatched, fail-closed), the PredicateValidator seam at construction (L2-M1), the
binding_invocation → AuthorityScope bridge incl. its non-active/negative-hops refusal (L2-M4/L1), and
the fail-soft store reads (missing/corrupt/malformed).
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from levain.autonomic import (
    AuthorityScope,
    Binding,
    BindingStatus,
    BindingStore,
    Graduation,
    Posture,
    PredicateValidator,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    binding_invocation,
    seal_binding_id,
)

# --- builders -----------------------------------------------------------------------

PATTERN = {"field": "from_domain", "op": "==", "value": "substack.com"}
FIRED_AT = "2026-06-26T14:00:00"


def make_trigger(type_: str = "email", pattern: dict | None = None) -> TriggerSpec:
    return TriggerSpec(type=type_, pattern=dict(pattern if pattern is not None else PATTERN))


def make_goal(goal: str = "summarize into a Doc", tools=("mail.read", "gdrive.create"),
              output: str = "drive:NewslettersDoc") -> tuple[SubGoal, ...]:
    return (SubGoal(goal=goal, tools=tuple(tools), output=output),)


def make_tightness(g=0.9, t=0.8, p=1.0, o=0.9) -> TightnessVector:
    return TightnessVector(goal_spec=g, tool_min=t, pattern_precision=p, output_bound=o)


def make_binding(*, posture: Posture = Posture.CONFIRM, status: BindingStatus = BindingStatus.ACTIVE,
                 one_shot: bool = False, created_at: str = "2026-06-26T13:00:00",
                 trigger: TriggerSpec | None = None, goal=None,
                 tightness: TightnessVector | None = None) -> Binding:
    return Binding.create(
        created_by="phill",
        created_at=created_at,
        trigger=trigger if trigger is not None else make_trigger(),
        goal=goal if goal is not None else make_goal(),
        tightness=tightness if tightness is not None else make_tightness(),
        posture=posture,
        one_shot=one_shot,
        status=status,
    )


def store(tmp_path, **kw):
    return BindingStore(tmp_path / "bindings.json", **kw)


# --- schema round-trip --------------------------------------------------------------

def test_binding_round_trips_through_dict():
    b = make_binding()
    again = Binding.from_dict(b.to_dict())
    assert again == b
    assert again.binding_id == b.binding_id
    assert again.trigger.pattern == PATTERN
    assert again.goal[0].tools == ("mail.read", "gdrive.create")
    assert again.posture is Posture.CONFIRM
    assert again.status is BindingStatus.ACTIVE
    assert again.one_shot is False


def test_nested_types_round_trip():
    assert TriggerSpec.from_dict(make_trigger().to_dict()) == make_trigger()
    sg = SubGoal(goal="g", tools=("a", "b"), output="o")
    assert SubGoal.from_dict(sg.to_dict()) == sg
    tv = make_tightness()
    assert TightnessVector.from_dict(tv.to_dict()) == tv
    gr = Graduation(fire_count=3, clean_count=2, last_fired_at=FIRED_AT)
    assert Graduation.from_dict(gr.to_dict()) == gr


def test_chain_of_multiple_subgoals_round_trips():
    goal = (
        SubGoal(goal="research R", tools=("web.search",), output="scratch"),
        SubGoal(goal="email the result", tools=("mail.send",), output="alice@x.com"),
    )
    b = make_binding(goal=goal)
    assert Binding.from_dict(b.to_dict()).goal == goal


def test_integer_tightness_round_trips_seal_stable(tmp_path):
    """L1-H1 regression: integer scores (0/1 — the endpoints a compiler emits) must normalize to
    float at construction so create-time and reload-time seals AGREE; otherwise a clean grant silently
    drops out of list_active with a false SEAL MISMATCH."""
    # ON_LOOP so the (confirm-class) guard-mandate doesn't bar it — this test is about the integer
    # tightness seal round-trip, not the guard.
    b = make_binding(posture=Posture.ON_LOOP,
                     tightness=TightnessVector(goal_spec=1, tool_min=0, pattern_precision=1, output_bound=1))
    assert b.tightness.goal_spec == 1.0 and isinstance(b.tightness.goal_spec, float)
    assert Binding.from_dict(b.to_dict()).seal_matches()
    s = store(tmp_path)
    s.add(b)
    assert [x.binding_id for x in s.list_active()] == [b.binding_id]  # NOT barred after round-trip


# --- the seal -----------------------------------------------------------------------

def test_seal_is_deterministic_in_core():
    b1 = make_binding()
    b2 = make_binding()
    assert b1.binding_id == b2.binding_id
    assert b1.binding_id.startswith("bind-2026-06-26T13:00:00-")
    assert b1.seal_matches()


@pytest.mark.parametrize("mutate", [
    {"posture": Posture.ON_LOOP},
    {"one_shot": True},                                                      # cardinality is sealed
    {"trigger": make_trigger(pattern={"field": "from_domain", "op": "==", "value": "evil.com"})},
    {"goal": make_goal(tools=("mail.read", "gdrive.create", "mail.send"))},  # tool widening
    {"goal": make_goal(output="drive:OtherDoc")},                            # output redirect
    {"tightness": make_tightness(p=0.1)},                                    # loosened pattern score
    {"created_by": "someone-else"},
])
def test_seal_detects_tamper_of_each_core_field(mutate):
    b = make_binding()
    tampered = dataclasses.replace(b, **mutate)   # keep the OLD id, change a sealed field
    assert not tampered.seal_matches()
    assert not tampered.is_active                 # tamper bars firing even with ACTIVE status


def test_status_and_graduation_are_outside_the_seal():
    b = make_binding()
    with_grad = dataclasses.replace(b, graduation=Graduation(fire_count=9, clean_count=9, last_fired_at=FIRED_AT))
    with_status = dataclasses.replace(b, status=BindingStatus.PAUSED)
    assert with_grad.seal_matches()
    assert with_status.seal_matches()
    assert with_grad.binding_id == b.binding_id


def test_from_dict_preserves_stale_id_so_disk_tamper_is_detectable():
    b = make_binding()
    d = b.to_dict()
    d["posture"] = "ON_LOOP"           # edit the core on disk, leave binding_id stale
    loaded = Binding.from_dict(d)
    assert loaded.posture is Posture.ON_LOOP
    assert loaded.binding_id == b.binding_id   # id NOT re-derived
    assert not loaded.seal_matches()           # → the tamper signature


def test_seal_binding_id_excludes_status_and_graduation():
    b = make_binding()
    direct = seal_binding_id(
        created_at=b.created_at, created_by=b.created_by, trigger=b.trigger,
        goal=b.goal, tightness=b.tightness, posture=b.posture, one_shot=b.one_shot,
    )
    assert direct == b.binding_id


# --- create guards ------------------------------------------------------------------

def test_create_refuses_empty_goal_chain():
    with pytest.raises(ValueError):
        Binding.create(created_by="phill", created_at="t", trigger=make_trigger(),
                       goal=(), tightness=make_tightness(), posture=Posture.CONFIRM)


def test_create_rejects_non_posture_and_empty_identity():
    with pytest.raises(TypeError):
        Binding.create(created_by="phill", created_at="t", trigger=make_trigger(),
                       goal=make_goal(), tightness=make_tightness(), posture="confirm")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Binding.create(created_by="", created_at="t", trigger=make_trigger(),
                       goal=make_goal(), tightness=make_tightness(), posture=Posture.CONFIRM)
    with pytest.raises(ValueError):
        Binding.create(created_by="phill", created_at="", trigger=make_trigger(),
                       goal=make_goal(), tightness=make_tightness(), posture=Posture.CONFIRM)


# --- strict parsing -----------------------------------------------------------------

def test_tightness_rejects_out_of_range_and_nan_and_bool():
    base = make_tightness().to_dict()
    for bad in (1.5, -0.1, float("nan")):
        with pytest.raises(ValueError):
            TightnessVector.from_dict(dict(base, goal_spec=bad))
    with pytest.raises(TypeError):
        TightnessVector.from_dict(dict(base, tool_min=True))  # bool is not a score


def test_graduation_rejects_bad_counters_and_clean_exceeding_fire():
    base = Graduation().to_dict()
    with pytest.raises(ValueError):
        Graduation.from_dict(dict(base, fire_count=-1))
    with pytest.raises(TypeError):
        Graduation.from_dict(dict(base, clean_count=True))
    with pytest.raises(ValueError):
        Graduation.from_dict({"fire_count": 1, "clean_count": 5, "last_fired_at": None})  # L1-M1
    with pytest.raises(TypeError):
        Graduation.from_dict(dict(base, last_fired_at=123))   # must be str|null


def test_trigger_rejects_empty_type_and_non_object_pattern():
    with pytest.raises(ValueError):
        TriggerSpec.from_dict({"type": "", "pattern": PATTERN})
    with pytest.raises(TypeError):
        TriggerSpec.from_dict({"type": "email", "pattern": "not-an-object"})


def test_subgoal_rejects_non_string_tools_and_empty_goal():
    with pytest.raises(TypeError):
        SubGoal.from_dict({"goal": "g", "tools": [1, 2], "output": "o"})
    with pytest.raises(ValueError):
        SubGoal.from_dict({"goal": "", "tools": [], "output": "o"})


def test_binding_from_dict_rejects_unknown_posture_status_and_nonbool_oneshot():
    d = make_binding().to_dict()
    with pytest.raises(ValueError):
        Binding.from_dict(dict(d, posture="SUPER_LOOP"))
    with pytest.raises(ValueError):
        Binding.from_dict(dict(d, status="frozen"))
    with pytest.raises(TypeError):
        Binding.from_dict(dict(d, one_shot="true"))   # truthy string must not coerce


# --- binding_invocation → AuthorityScope -------------------------------------------

def test_binding_invocation_produces_binding_grantor_scope():
    b = make_binding(posture=Posture.CONFIRM)
    scope = binding_invocation(b, hops=2)
    assert isinstance(scope, AuthorityScope)
    assert scope.grantor == "binding"
    assert scope.binding_id == b.binding_id
    assert scope.hops == 2
    assert "email" in scope.grant and "confirm" in scope.grant
    assert scope.to_dict()["binding_id"] == b.binding_id


def test_binding_invocation_refuses_non_active_and_negative_hops():
    revoked = dataclasses.replace(make_binding(), status=BindingStatus.REVOKED)
    with pytest.raises(ValueError):
        binding_invocation(revoked)                       # L2-M4: a revoked grant mints no authority
    tampered = dataclasses.replace(make_binding(), posture=Posture.ON_LOOP)  # seal-broken
    with pytest.raises(ValueError):
        binding_invocation(tampered)
    with pytest.raises(ValueError):
        binding_invocation(make_binding(), hops=-1)       # L2-L1


# --- the registry: basic --------------------------------------------------------------

def test_add_get_and_idempotent_replace(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    s.add(b)
    assert s.get(b.binding_id) == b
    assert len(s.list_all()) == 1
    assert s.get("bind-nope") is None


def test_add_refuses_seal_broken_binding(tmp_path):
    s = store(tmp_path)
    broken = dataclasses.replace(make_binding(), posture=Posture.ON_LOOP)  # stale id
    with pytest.raises(ValueError):
        s.add(broken)
    assert s.get(broken.binding_id) is None


def test_list_filters_by_status_and_trigger_type(tmp_path):
    s = store(tmp_path)
    email = make_binding(created_at="2026-06-26T13:00:00")
    loc = make_binding(created_at="2026-06-26T13:01:00", trigger=make_trigger("location.fix"))
    s.add(email)
    s.add(loc)
    s.set_status(loc.binding_id, BindingStatus.PAUSED)
    assert {b.binding_id for b in s.list_all()} == {email.binding_id, loc.binding_id}
    assert [b.binding_id for b in s.list_all(trigger_type="email")] == [email.binding_id]
    assert [b.binding_id for b in s.list_all(status=BindingStatus.PAUSED)] == [loc.binding_id]


def test_record_fire_bumps_counters_sets_last_fired_and_keeps_id(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    s.record_fire(b.binding_id, clean=True, fired_at="2026-06-26T14:00:00")
    u2 = s.record_fire(b.binding_id, clean=False, fired_at="2026-06-26T15:00:00")
    assert u2 is not None
    assert u2.graduation.fire_count == 2
    assert u2.graduation.clean_count == 1
    assert u2.graduation.last_fired_at == "2026-06-26T15:00:00"   # dormancy sensor (L2-M2)
    assert u2.binding_id == b.binding_id and u2.seal_matches()
    assert s.record_fire("bind-nope", clean=True, fired_at=FIRED_AT) is None


def test_remove_hard_deletes(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    assert s.remove(b.binding_id) is True
    assert s.get(b.binding_id) is None
    assert s.remove(b.binding_id) is False


# --- the registry: the governed lifecycle (L2-H1) -------------------------------------

def test_set_status_legal_transition_keeps_seal(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    assert s.set_status(b.binding_id, BindingStatus.PAUSED) is True
    assert s.set_status(b.binding_id, BindingStatus.ACTIVE) is True   # resume is legal
    reread = s.get(b.binding_id)
    assert reread is not None and reread.status is BindingStatus.ACTIVE
    assert reread.seal_matches() and reread.binding_id == b.binding_id
    assert s.set_status("bind-nope", BindingStatus.PAUSED) is False


def test_revoked_is_terminal_and_inert_cannot_reactivate(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    s.set_status(b.binding_id, BindingStatus.REVOKED)
    with pytest.raises(ValueError):
        s.set_status(b.binding_id, BindingStatus.ACTIVE)     # revoked is terminal
    with pytest.raises(ValueError):
        s.set_status(b.binding_id, BindingStatus.PAUSED)
    # expired only cleans up to revoked
    b2 = make_binding(created_at="2026-06-26T13:09:00")
    s.add(b2)
    s.set_status(b2.binding_id, BindingStatus.EXPIRED)
    with pytest.raises(ValueError):
        s.set_status(b2.binding_id, BindingStatus.ACTIVE)
    assert s.set_status(b2.binding_id, BindingStatus.REVOKED) is True


def test_add_preserves_status_so_a_re_add_cannot_resurrect_a_revoked_grant(tmp_path):
    """L2-H1: re-create()-ing a revoked grant's core yields the same id with default-ACTIVE status; a
    naive replace would un-revoke it. add must PRESERVE the on-disk REVOKED status."""
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    s.set_status(b.binding_id, BindingStatus.REVOKED)
    s.add(make_binding())                      # same core → same id, default ACTIVE
    got = s.get(b.binding_id)
    assert got is not None and got.status is BindingStatus.REVOKED   # stayed dead
    assert s.list_active() == []


def test_add_preserves_graduation_so_a_stale_re_add_cannot_wipe_evidence(tmp_path):
    """L1-M3: a benign reconcile re-adding a known binding (with graduation=0) must not wipe the
    accumulated on-disk counters."""
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    s.record_fire(b.binding_id, clean=True, fired_at=FIRED_AT)
    s.record_fire(b.binding_id, clean=True, fired_at=FIRED_AT)
    s.add(make_binding())                      # stale in-memory object, graduation 0
    got = s.get(b.binding_id)
    assert got is not None and got.graduation.fire_count == 2   # evidence preserved


# --- the registry: re-ratification (L2-M3) --------------------------------------------

def test_replace_atomic_adds_new_and_revokes_old(tmp_path):
    s = store(tmp_path)
    old = make_binding(posture=Posture.CONFIRM)
    s.add(old)
    s.record_fire(old.binding_id, clean=True, fired_at=FIRED_AT)
    promoted = make_binding(posture=Posture.ON_LOOP)   # a re-ratification = different core = new id
    assert promoted.binding_id != old.binding_id
    assert s.replace_atomic(old.binding_id, promoted) is True
    old_got = s.get(old.binding_id)
    assert old_got is not None and old_got.status is BindingStatus.REVOKED   # superseded
    active = {x.binding_id for x in s.list_active()}
    assert active == {promoted.binding_id}             # exactly one active, no crash-window overlap


# --- the FIRE-PATH guard --------------------------------------------------------------

def test_list_active_excludes_inactive_status(tmp_path):
    s = store(tmp_path)
    # ON_LOOP (guard-free) so this isolates the STATUS exclusion, not the confirm-class guard-mandate.
    active = make_binding(created_at="t1", posture=Posture.ON_LOOP)
    paused = make_binding(created_at="t2", posture=Posture.ON_LOOP, status=BindingStatus.PAUSED)
    one_shot = make_binding(created_at="t3", posture=Posture.ON_LOOP, one_shot=True)  # ACTIVE until it fires
    for b in (active, paused, one_shot):
        s.add(b)
    assert {b.binding_id for b in s.list_active()} == {active.binding_id, one_shot.binding_id}


def test_list_active_excludes_seal_mismatched_binding(tmp_path, caplog):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    raw = json.loads(s.path.read_text())
    raw[0]["posture"] = "ON_LOOP"              # tamper on disk, leave binding_id stale
    s.path.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        assert s.list_active() == []           # fail-closed: tampered grant cannot fire
    assert "SEAL MISMATCH" in caplog.text
    got = s.get(b.binding_id)                  # inspection path still sees it
    assert got is not None and not got.seal_matches()


# --- the PredicateValidator seam (injected at construction, L2-M1) --------------------

class _RejectingValidator:
    def validate(self, pattern: dict) -> None:
        raise ValueError("not a deterministic predicate")


class _AcceptingValidator:
    def __init__(self):
        self.seen = []

    def validate(self, pattern: dict) -> None:
        self.seen.append(pattern)


def test_store_validator_enforces_and_passes_through(tmp_path):
    b = make_binding()
    s_rej = store(tmp_path, validator=_RejectingValidator())
    with pytest.raises(ValueError):
        s_rej.add(b)
    assert s_rej.get(b.binding_id) is None     # rejected binding was NOT persisted
    acc = _AcceptingValidator()
    s_acc = BindingStore(tmp_path / "ok.json", validator=acc)
    s_acc.add(b)
    assert acc.seen == [PATTERN]               # the validator saw the opaque pattern
    assert s_acc.get(b.binding_id) == b


def test_validator_protocol_is_runtime_checkable():
    assert isinstance(_AcceptingValidator(), PredicateValidator)
    assert not isinstance(object(), PredicateValidator)


# --- fail-soft store reads ----------------------------------------------------------

def test_missing_file_reads_empty(tmp_path):
    assert store(tmp_path).list_all() == []
    assert store(tmp_path).list_active() == []
    assert store(tmp_path).get("x") is None


def test_corrupt_json_reads_empty(tmp_path, caplog):
    s = store(tmp_path)
    s.path.write_text("{not json")
    with caplog.at_level("WARNING"):
        assert s.list_all() == []
    assert "corrupt JSON" in caplog.text


def test_non_list_top_level_reads_empty(tmp_path, caplog):
    s = store(tmp_path)
    s.path.write_text('{"binding_id": "x"}')
    with caplog.at_level("WARNING"):
        assert s.list_all() == []
    assert "not a list" in caplog.text


def test_malformed_record_is_skipped_loudly(tmp_path, caplog):
    s = store(tmp_path)
    good = make_binding()
    s.add(good)
    raw = json.loads(s.path.read_text())
    raw.append({"binding_id": "bind-bad", "posture": "NONSENSE"})  # malformed
    s.path.write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        listed = s.list_all()
    assert [b.binding_id for b in listed] == [good.binding_id]
    assert "malformed" in caplog.text


# === L3 fixes ========================================================================

# --- seal canonicalization (codex-H2): the opaque pattern must be canonical-JSON-unambiguous ---

def test_trigger_rejects_non_string_keys_and_non_finite_floats():
    with pytest.raises(TypeError):
        TriggerSpec(type="email", pattern={1: "x"})            # json.dumps would coerce 1→"1" → ambiguous
    with pytest.raises(TypeError):
        TriggerSpec(type="email", pattern={"v": {1: "deep"}})  # nested non-str key
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            TriggerSpec(type="email", pattern={"threshold": bad})
    # a valid nested predicate passes
    TriggerSpec(type="email", pattern={"and": [{"field": "x", "op": "==", "value": 3}]})


def test_two_cores_differing_only_by_key_type_do_not_collide():
    """Without canonical validation, {1:'x'} and {'1':'x'} would seal identically. They are now both
    rejected at construction, so a same-id-different-core collision is impossible by construction."""
    with pytest.raises(TypeError):
        make_binding(trigger=TriggerSpec(type="email", pattern={1: "x"}))


# --- replace_atomic edges (codex-H1 / nemotron-H1) -----------------------------------

def test_replace_atomic_rejects_same_id(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    with pytest.raises(ValueError):
        s.replace_atomic(b.binding_id, b)      # identical core = identical id = not a re-ratification


def test_replace_atomic_aborts_without_write_when_old_absent(tmp_path):
    s = store(tmp_path)
    existing = make_binding(created_at="t-existing")
    s.add(existing)
    promoted = make_binding(posture=Posture.ON_LOOP)
    assert s.replace_atomic("bind-does-not-exist", promoted) is False
    assert s.get(promoted.binding_id) is None                       # NOTHING written — no orphan grant
    assert {x.binding_id for x in s.list_all()} == {existing.binding_id}


# --- record_fire type check (codex-M1) ----------------------------------------------

def test_record_fire_rejects_truthy_string_clean_and_empty_fired_at(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    with pytest.raises(TypeError):
        s.record_fire(b.binding_id, clean="false", fired_at=FIRED_AT)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        s.record_fire(b.binding_id, clean=True, fired_at="")


# --- bookkeeping-preserve: most-restrictive valid wins / malformed ignored (codex-M2/cmpl/nemo) ---

def test_duplicate_active_cannot_override_a_revoked_tombstone(tmp_path):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    rec = json.loads(s.path.read_text())[0]
    s.path.write_text(json.dumps([dict(rec, status="active"), dict(rec, status="revoked")]))
    s.add(make_binding())                          # re-add the same core
    got = s.get(b.binding_id)
    assert got is not None and got.status is BindingStatus.REVOKED  # most-restrictive valid wins
    assert len(s.list_all()) == 1                  # duplicates collapsed


def test_re_add_over_only_malformed_record_uses_incoming_not_entomb(tmp_path, caplog):
    s = store(tmp_path)
    b = make_binding()
    s.add(b)
    rec = json.loads(s.path.read_text())[0]
    s.path.write_text(json.dumps([dict(rec, status="frozen")]))   # malformed status, same id
    with caplog.at_level("WARNING"):
        s.add(make_binding())
    got = s.get(b.binding_id)
    assert got is not None and got.status is BindingStatus.ACTIVE  # not entombed; incoming used
    assert "malformed" in caplog.text


# --- read failure UNDER MUTATION must fail loud, never write-on-empty (nemotron-MED-1) ---

def test_read_failure_under_mutation_does_not_wipe_the_store(tmp_path):
    s = store(tmp_path)
    s.add(make_binding())
    s.path.unlink()
    s.path.mkdir()                                 # data path is now a dir → read_text raises OSError
    with pytest.raises(OSError):
        s.add(make_binding(created_at="t2"))       # mutation fails loud, does NOT delete the registry
    assert s.list_all() == []                       # read-only path still degrades soft


# --- non-empty disk-boundary fields (complement-LOW-2 / nemotron-MED-4) ---------------

def test_from_dict_rejects_empty_identity_and_output():
    d = make_binding().to_dict()
    with pytest.raises(ValueError):
        Binding.from_dict(dict(d, created_at=""))
    with pytest.raises(ValueError):
        Binding.from_dict(dict(d, created_by=""))
    with pytest.raises(ValueError):
        SubGoal.from_dict({"goal": "g", "tools": [], "output": ""})
