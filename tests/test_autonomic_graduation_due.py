"""``graduate_due``: the engine step that takes a graduation proposal under a signed policy (14b DESIGN_REV2 §5,
DESIGN_REV3 §6, RULINGS_S1 item 1). Coverage here is the test-only in-memory source; the same step over a real
authority log and root file runs in ``tests/test_graduation_journey.py``."""
from __future__ import annotations

import datetime as _dt
import sqlite3

from levain.autonomic import (ActionManifest, ActionRisk, Binding, BindingStatus, BindingStore, Graduation, Guard,
                              Posture, RiskClass, SubGoal, TightnessVector, TriggerSpec)
from levain.autonomic.binding import binding_family
from levain.autonomic.graduation import graduate_due
from levain.graduation_policy import TermedGraduationPolicy
from tests.autonomic_test_only_coverage import TestOnlyCoverage
from tests.autonomic_test_only_coverage import policy as _policy

FIXED = _dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=_dt.timezone.utc)
LATER = FIXED + _dt.timedelta(days=10)
LOW = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
MANIFEST = ActionManifest({"deliver": LOW, "file.note": LOW})
_KILL = Guard(rationale="a spoofed sender", dissent_author="codex",
              kill_predicate={"op": "!=", "field": "dmarc", "value": "pass"}, kill_drill={"dmarc": "fail"},
              kill_authored_by="phill")


def policy(gid, actions, *, min_clean=20, **kw) -> TermedGraduationPolicy:
    """The replayed policy with its signed ``min_clean_fires`` (the log requires the field on every policy)."""
    return TermedGraduationPolicy.of(_policy(gid, actions, **kw), min_clean_fires=min_clean)


def _binding(posture=Posture.COOLING_OFF, *, links=1, clean=20, fires=20) -> Binding:
    return Binding.create(
        created_by="phill", created_at="2026-10-01T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=tuple(SubGoal(goal=f"g{i}", tools=("doc.append",), output="drive:x") for i in range(links)),
        tightness=TightnessVector(1, 1, 1, 1), posture=posture, status=BindingStatus.ACTIVE,
        guard=(_KILL,) if posture > Posture.ON_LOOP else (), graduation=Graduation(fire_count=fires, clean_count=clean))


def _store(tmp_path, b: Binding) -> BindingStore:
    s = BindingStore(tmp_path / "store")
    s.add(b)
    return s


def _due(store, coverage, *, link_action=lambda b, i: "deliver", **kw):
    return graduate_due(store, coverage, clock=lambda: FIXED, manifest=MANIFEST, link_action=link_action,
                        risk_resolver=lambda b: LOW, link_risk_resolver=lambda b, i: LOW, **kw)


def _steps(store) -> list[tuple]:
    conn = sqlite3.connect(store.db.path)
    try:
        return conn.execute("SELECT old_id, new_id, family, from_posture, to_posture, clean_count, fire_count, "
                            "policy_id, incarnation, authority_head FROM graduation_steps").fetchall()
    finally:
        conn.close()


def test_a_covered_step_replaces_the_grant_and_its_audit_row_commits_with_it(tmp_path):
    b = _binding()
    s = _store(tmp_path, b)
    p1 = policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")
    src = TestOnlyCoverage([p1])
    [step] = _due(s, src)
    assert step.outcome == "graduated" and src.refreshes == 1
    assert s.get(b.binding_id).status is BindingStatus.REVOKED
    new = s.get(step.new_id)
    assert new.status is BindingStatus.ACTIVE and new.posture is Posture.ON_LOOP and new.graduation.fire_count == 0
    head = src.head
    assert _steps(s) == [(b.binding_id, step.new_id, binding_family(b), "COOLING_OFF", "ON_LOOP", 20, 20, "gp-1",
                          p1.incarnation, f"{head[0]}:{head[1]}")]


def test_with_no_policy_the_step_is_uncovered_and_nothing_is_written(tmp_path):
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([]))
    assert step.outcome == "uncovered" and [l.action_name for l in step.uncovered] == ["deliver"]
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE and _steps(s) == []


def test_no_coverage_source_covers_nothing(tmp_path):
    s = _store(tmp_path, _binding())
    [step] = _due(s, None)
    assert step.outcome == "uncovered" and _steps(s) == []


def test_a_policy_on_the_bindings_tool_does_not_cover_the_action_the_builder_emits(tmp_path):
    s = _store(tmp_path, _binding())
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"doc.append": LOW}, expires_at=LATER)]))
    assert step.outcome == "uncovered"


def test_a_policy_whose_lowest_rung_is_stricter_than_the_target_does_not_cover_it(tmp_path):
    s = _store(tmp_path, _binding(Posture.COOLING_OFF))            # proposes ON_LOOP
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="COOLING_OFF")]))
    assert step.outcome == "uncovered" and step.proposal.to_posture is Posture.ON_LOOP


def test_every_link_must_be_covered(tmp_path):
    s = _store(tmp_path, _binding(links=2))
    names = {0: "deliver", 1: "file.note"}
    one = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)])
    [step] = _due(s, one, link_action=lambda b, i: names[i])
    assert step.outcome == "uncovered" and [l.action_name for l in step.uncovered] == ["file.note"]
    p1, p2 = policy("gp-1", {"deliver": LOW}, expires_at=LATER), policy("gp-2", {"file.note": LOW}, expires_at=LATER)
    both = TestOnlyCoverage([p1, p2])
    [step] = _due(s, both, link_action=lambda b, i: names[i])
    assert step.outcome == "graduated"
    [row] = _steps(s)
    assert row[7] == "gp-1,gp-2" and row[8] == ",".join(sorted([p1.incarnation, p2.incarnation]))


def test_an_action_the_manifest_does_not_declare_is_uncovered(tmp_path):
    s = _store(tmp_path, _binding())
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"mail.draft": LOW}, expires_at=LATER)]),
                  link_action=lambda b, i: "mail.draft")
    assert step.outcome == "uncovered" and step.links[0].action_risk is None


def test_a_link_whose_action_cannot_be_named_is_unresolved(tmp_path):
    s = _store(tmp_path, _binding())

    def boom(b, i):
        raise KeyError("no builder record")

    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]), link_action=boom)
    assert step.outcome == "unresolved" and _steps(s) == []


def test_preview_writes_nothing(tmp_path):
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]), apply=False)
    assert step.outcome == "preview" and step.new_id is None
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE and _steps(s) == []


def test_a_lapsed_policy_covers_nothing(tmp_path):
    s = _store(tmp_path, _binding())
    lapsed = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=FIXED - _dt.timedelta(seconds=1))])
    [step] = _due(s, lapsed)
    assert step.outcome == "uncovered"


def test_without_the_evidence_there_is_no_step(tmp_path):
    s = _store(tmp_path, _binding(clean=19, fires=19))
    assert _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)])) == []


def test_the_replace_reads_the_view_refreshed_before_it_and_refreshes_once_per_step(tmp_path):
    s = _store(tmp_path, _binding())
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)])
    src.after_refresh = lambda: src.revoke("gp-1")   # a revoke after the refresh is not seen by this step
    [step] = _due(s, src)
    assert step.outcome == "graduated" and src.refreshes == 1


def test_the_policys_min_clean_fires_is_the_walk_not_the_engines_default(tmp_path):
    # RULINGS_R1 item 2: a --min-clean 100 policy and a binding at 20 clean fires: nothing graduates today
    b = _binding(clean=20, fires=20)
    s = _store(tmp_path, b)
    assert _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, min_clean=100)])) == []
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE and _steps(s) == []


def test_a_policy_with_a_shorter_walk_graduates_before_the_engines_default(tmp_path):
    s = _store(tmp_path, _binding(clean=5, fires=5))
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, min_clean=5)]))
    assert step.outcome == "graduated"


def test_without_a_policy_no_card_before_the_engines_default_walk(tmp_path):
    assert _due(_store(tmp_path, _binding(clean=5, fires=5)), TestOnlyCoverage([])) == []


def test_the_strictest_walk_of_the_covering_policies_holds(tmp_path):
    s = _store(tmp_path, _binding(links=2, clean=20, fires=20))
    names = {0: "deliver", 1: "file.note"}
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, min_clean=5),
                            policy("gp-2", {"file.note": LOW}, expires_at=LATER, min_clean=30)])
    assert _due(s, src, link_action=lambda b, i: names[i]) == []
    # two policies covering the same link: the stricter walk holds too
    s2 = _store(tmp_path / "two", _binding(clean=20, fires=20))
    src2 = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, min_clean=5),
                             policy("gp-2", {"deliver": LOW}, expires_at=LATER, min_clean=30)])
    assert _due(s2, src2) == []


def test_a_covering_policy_that_names_no_walk_graduates_nothing(tmp_path):
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, TestOnlyCoverage([_policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert step.outcome == "unattributed" and _steps(s) == []
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE


def test_the_step_the_card_and_the_replace_judge_the_promoted_bindings_actions(tmp_path):
    # RULINGS_R1 item 4: a builder that names another action for the promoted grant than for the old one
    b = _binding()
    s = _store(tmp_path, b)

    def link_action(binding, i):
        return "deliver" if binding.binding_id == b.binding_id else "file.note"
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]), link_action=link_action)
    assert step.outcome == "uncovered" and [l.action_name for l in step.uncovered] == ["file.note"]
    from levain.cockpit.graduation_cards import card_of
    assert "--action file.note=LOW" in card_of(step)["command"]
    p1, p2 = policy("gp-1", {"deliver": LOW}, expires_at=LATER), policy("gp-2", {"file.note": LOW}, expires_at=LATER)
    both = TestOnlyCoverage([p1, p2])
    [step] = _due(s, both, link_action=link_action)
    assert step.outcome == "graduated" and _steps(s)[0][7] == "gp-2"


class _Answers:
    """A coverage source whose one view answers ``cover`` for every effect."""

    def __init__(self, cover) -> None:
        self._cover = cover
        self.token = ((1, "h"), "d", "store")

    def cover(self, effect, now):
        return self._cover

    def refresh(self):
        return self

    def peek(self, effect, now):
        return "none"


import pytest  # noqa: E402

from levain.autonomic.coverage import COVER_NONE, Cover  # noqa: E402


@pytest.mark.parametrize("answer", [Cover(Posture.ON_LOOP, COVER_NONE), Cover(Posture.ON_LOOP, "cover:moved"),
                                    Cover("ON_LOOP", "inc@2026-11-01"), Cover(Posture.ABOVE_LOOP, "inc@2026-11-01"),
                                    "ON_LOOP"])
def test_a_malformed_cover_covers_nothing(tmp_path, answer):
    # RULINGS_R1 item 6: the gate's own invariant (coverage._is_cover) decides what a view's answer covers
    b = _binding()
    s = _store(tmp_path, b)
    [step] = _due(s, _Answers(answer))
    assert step.outcome == "uncovered" and [l.action_name for l in step.uncovered] == ["deliver"]
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE and _steps(s) == []


def _three(tmp_path) -> tuple[BindingStore, list[Binding]]:
    s = BindingStore(tmp_path / "store")
    bs = []
    for i in range(3):
        b = Binding.create(
            created_by="phill", created_at=f"2026-10-0{i + 1}T09:00:00",
            trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": f"f{i}"}),
            goal=(SubGoal(goal=f"g{i}", tools=("doc.append",), output="drive:x"),),
            tightness=TightnessVector(1, 1, 1, 1), posture=Posture.COOLING_OFF, status=BindingStatus.ACTIVE,
            guard=(_KILL,), graduation=Graduation(fire_count=20, clean_count=20))
        s.add(b)
        bs.append(b)
    return s, bs


def test_a_replace_that_raises_is_that_bindings_error_step_and_the_others_still_step(tmp_path, monkeypatch):
    # RULINGS_R1 item 8: never an exception after earlier bindings committed
    s, bs = _three(tmp_path)
    real, calls = s.replace_atomic, []

    def replace_atomic(old, new, **kw):
        calls.append(old.binding_id)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database is locked")
        return real(old, new, **kw)
    monkeypatch.setattr(s, "replace_atomic", replace_atomic)
    steps = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert sorted(st.outcome for st in steps) == ["error:OperationalError", "graduated", "graduated"]
    assert len(_steps(s)) == 2


def test_an_audit_row_that_cannot_be_built_writes_nothing(tmp_path, monkeypatch):
    import levain.autonomic.graduation as g

    b = _binding()
    s = _store(tmp_path, b)
    called = []
    monkeypatch.setattr(s, "replace_atomic", lambda *a, **kw: called.append(1))

    def boom(*a, **kw):
        raise IndexError("no head")
    monkeypatch.setattr(g, "_audit_row", boom)
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert step.outcome == "error:IndexError" and called == [] and _steps(s) == []


def test_a_step_whose_policy_cannot_be_named_is_never_recorded_as_unknown(tmp_path, monkeypatch):
    # RULINGS_R1 item 11 (complement 5): the audit row names the policies that covered the step, or nothing is written
    import levain.autonomic.graduation as g

    b = _binding()
    s = _store(tmp_path, b)
    monkeypatch.setattr(g, "_walk", lambda links: 1)       # past the walk: the row itself must refuse
    [step] = _due(s, _Answers(Cover(Posture.ON_LOOP, "inc-x@2026-11-01T00:00:00.000000Z")))
    assert step.outcome == "error:ValueError" and _steps(s) == []
    assert s.get(b.binding_id).status is BindingStatus.ACTIVE


def test_a_refresh_that_raises_is_that_bindings_error_step_and_the_others_still_step(tmp_path, monkeypatch):
    # RULINGS_R2 item 1: binding 1 graduates and commits, then the authority log is locked when binding 2 refreshes
    s, bs = _three(tmp_path)
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)])
    real = src.refresh

    def refresh():
        if src.refreshes == 1:
            src.refreshes += 1
            raise sqlite3.OperationalError("database is locked")
        return real()
    monkeypatch.setattr(src, "refresh", refresh)
    steps = _due(s, src)
    assert [st.outcome for st in steps] == ["graduated", "error:OperationalError", "graduated"]
    assert steps[1].links == () and steps[1].new_id is None
    assert len(_steps(s)) == 2


def test_a_promoted_grant_that_cannot_be_built_is_that_bindings_error_step(tmp_path, monkeypatch):
    # RULINGS_R2 item 1 (complement LOW): build_promoted raising on one binding never aborts the run
    from levain.autonomic.graduation import GraduationProposal

    s, bs = _three(tmp_path)
    real, calls = GraduationProposal.build_promoted, []

    def build_promoted(self, **kw):
        calls.append(self.old_binding_id)
        if len(calls) == 2:
            raise ValueError("a grant that cannot be sealed")
        return real(self, **kw)
    monkeypatch.setattr(GraduationProposal, "build_promoted", build_promoted)
    steps = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert [st.outcome for st in steps] == ["graduated", "error:ValueError", "graduated"]
    assert len(_steps(s)) == 2



def test_a_proposal_that_raises_leaves_that_binding_unproposed_and_the_others_still_step(tmp_path, monkeypatch):
    # verify r2 deepseek MED 1: a risk resolver raising inside propose_graduation on binding 2 never aborts the run
    import levain.autonomic.graduation as g

    s, bs = _three(tmp_path)
    real, calls = g.propose_graduation, []

    def propose(binding, **kw):
        calls.append(binding.binding_id)
        if len(calls) == 2:
            raise ValueError("an action with no risk class")
        return real(binding, **kw)
    monkeypatch.setattr(g, "propose_graduation", propose)
    steps = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER)]))
    assert [st.outcome for st in steps] == ["graduated", "graduated"]
    assert len(_steps(s)) == 2


def test_an_unattributed_step_waits_for_the_engines_walk(tmp_path):
    # RULINGS_R2 item 2: a covering policy that names no walk shows no card before the engine's own min_clean_fires
    s = _store(tmp_path, _binding(clean=5, fires=5))
    assert _due(s, TestOnlyCoverage([_policy("gp-1", {"deliver": LOW}, expires_at=LATER)])) == []


def _after_list_active(monkeypatch, store, then) -> None:
    """Run ``then()`` once, right after the engine's ``list_active`` read: the deterministic hook a concurrent writer
    landing between the walk's read and its replace needs (no sleeps)."""
    real = store.list_active

    def list_active(**kw):
        read = real(**kw)
        then()
        return read
    monkeypatch.setattr(store, "list_active", list_active, raising=False)


def test_a_revoke_landing_after_the_walk_read_is_a_stale_step_and_nothing_is_resurrected(tmp_path, monkeypatch):
    # RULINGS_R2 item 4 (deepseek HIGH): a revoke between list_active and the replace never yields a live grant
    b = _binding()
    s = _store(tmp_path, b)
    _after_list_active(monkeypatch, s, lambda: s.set_status(b.binding_id, BindingStatus.REVOKED))
    [step] = _due(s, TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")]))
    assert step.outcome == "stale" and step.new_id is None
    assert [r.binding_id for r in s.list_all()] == [b.binding_id]
    assert s.get(b.binding_id).status is BindingStatus.REVOKED and s.list_active() == [] and _steps(s) == []


def test_two_graduate_runs_on_one_binding_promote_it_once(tmp_path, monkeypatch):
    # RULINGS_R2 item 4: run B steps the binding in full between run A's read and A's replace
    b = _binding()
    s = _store(tmp_path, b)
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")])
    s_b = BindingStore(tmp_path / "store")
    run_b: list = []
    later = FIXED + _dt.timedelta(minutes=1)    # B's clock differs, so its promoted grant has another id

    def other_run() -> None:
        run_b.extend(graduate_due(s_b, src, clock=lambda: later, manifest=MANIFEST, link_action=lambda b, i: "deliver",
                                  risk_resolver=lambda b: LOW, link_risk_resolver=lambda b, i: LOW))
    _after_list_active(monkeypatch, s, other_run)
    [step_a] = _due(s, src)
    assert [st.outcome for st in run_b] == ["graduated"]
    assert step_a.outcome == "stale" and step_a.new_id is None
    assert [r.binding_id for r in s.list_active()] == [run_b[0].new_id]
    assert len(s.list_all()) == 2 and len(_steps(s)) == 1


def test_two_graduate_runs_in_the_same_instant_promote_it_once(tmp_path, monkeypatch):
    # the same race when both runs mint the same promoted id (one clock): the second finds its target written
    b = _binding()
    s = _store(tmp_path, b)
    src = TestOnlyCoverage([policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")])
    run_b: list = []
    _after_list_active(monkeypatch, s, lambda: run_b.extend(_due(BindingStore(tmp_path / "store"), src)))
    [step_a] = _due(s, src)
    assert [st.outcome for st in run_b] == ["graduated"] and step_a.outcome == "stale"
    assert len(s.list_all()) == 2 and len(s.list_active()) == 1 and len(_steps(s)) == 1


class _WithPolicies(_Answers):
    """A view that answers ``cover`` and carries the policies, root file and store id ``_covering`` reads."""

    def __init__(self, cover, policies) -> None:
        super().__init__(cover)
        from levain.autonomic.coverage import parse_root_signers
        from tests.autonomic_test_only_coverage import STORE, root_file
        from tests.autonomic_test_only_coverage import LAPTOP as _laptop
        self.policies, self.signers, self.store_id = tuple(policies), parse_root_signers(root_file(_laptop)), STORE


def test_a_hit_policy_whose_rung_is_a_string_names_nothing_and_the_step_is_unattributed(tmp_path):
    # RULINGS_R2 item 5 (deepseek MED): the rung/cover-value check of _covering is inside its guard
    good = policy("gp-1", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")
    bad = policy("gp-2", {"deliver": LOW}, expires_at=LATER, lowest="ON_LOOP")
    object.__setattr__(bad, "lowest_posture", "ON_LOOP")   # what a JSON round-trip that skipped from_record leaves
    s = _store(tmp_path, _binding())
    [step] = _due(s, _WithPolicies(Cover(Posture.ON_LOOP, good.cover_value), [good, bad]))
    assert step.outcome == "unattributed" and [link.action_name for link in step.links] == ["deliver"]
    assert all(link.policies == () for link in step.links) and _steps(s) == []
