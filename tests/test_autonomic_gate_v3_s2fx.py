"""R15-5 S2 fix round 1 (levain project_memory 1027-18/r155s2fx/BRIEF.md): the L3 r1 + r2 findings on the gate over a
``levain-autonomic/3`` store, each with the finding it closes."""
from __future__ import annotations

import pytest

from levain.autonomic import BindingStore, RunJournal
from levain.autonomic import schema3 as s

pytestmark = pytest.mark.usefixtures("test_only_confined_executor", "test_only_every_effect_covered")


# ---- R2 [codex r1 MED 4 = codex r2 HIGH 2]: the caller's path is compared before the /3 journal's db is adopted -----
def test_r2_a_store_path_that_is_not_the_v3_journals_directory_is_refused(tmp_path):
    with pytest.raises(ValueError, match="same store directory"):
        BindingStore(tmp_path / "a", journal=RunJournal(s.AutonomicStore3(tmp_path / "b")))
    assert not (tmp_path / "a").exists()   # nothing was opened at the refused path
    BindingStore(tmp_path / "b", journal=RunJournal(s.AutonomicStore3(tmp_path / "b")))


# ---- shared ---------------------------------------------------------------------------------------------------------
import datetime as _dt  # noqa: E402
import json  # noqa: E402

from levain.autonomic import ActionRequest, ConfirmDecision, Posture, RunRef  # noqa: E402
from levain.autonomic import journal as J  # noqa: E402
from levain.autonomic.store import gate_record  # noqa: E402
from tests.test_autonomic_gate_v3_s2 import _binding  # noqa: E402
from tests.test_autonomic_gate_v3_s2f import V3World, _gate_outcomes, _run_close  # noqa: E402
from tests.test_autonomic_journal_wiring import FIXED, HIGH, World, _trust  # noqa: E402


def _cooling_world(world_cls, tmp_path):
    w = world_cls(tmp_path)
    b = _binding(Posture.COOLING_OFF, at="2026-10-27T09:00:00co")
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.gate._auto_fire_actions = frozenset({"link0"})
    w.dispatch("co1")
    [p] = w.gate.open_pendings()
    return w, p, w.journal.find_pending(p.pending_id)


def _legacy_decide(w, hold: dict, posture: str) -> None:
    # a standing unsigned approval recorded at ``posture`` past the journal API's check (a record from before it)
    with w.journal.db.write() as c:
        assert J.RunJournal._decide_in(c, hold["hold_id"], approve=True, by=J.UNSIGNED_DECIDER, posture=posture,
                                       signer=None, signature=None, challenge=None, fence=None) == 1


# ---- R1 [codex r1 HIGH 3]: a decided rung of REFUSE_ESCALATE never executes ---------------------------------------
def test_r1_a_standing_approval_recorded_at_refuse_escalate_never_fires_on_v2(tmp_path):
    w, p, hold = _cooling_world(World, tmp_path)
    _legacy_decide(w, hold, "REFUSE_ESCALATE")
    w.gate._clock = lambda: FIXED + _dt.timedelta(days=1)      # past the silence window
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    assert not out.fired and out.refused and out.reason == "decided_rung_refuses", out
    assert w.outbox() == []
    with w.journal.db.read() as c:
        assert c.execute("SELECT reason FROM cancels").fetchall() == [("decided_rung_refuses",)]


def test_r1_an_approving_decide_names_only_an_executing_posture(tmp_path):
    for world_cls in (World, V3World):
        w, p, hold = _cooling_world(world_cls, tmp_path / world_cls.__name__)
        for bad in ("REFUSE_ESCALATE", "NOT_A_RUNG"):
            with pytest.raises(ValueError, match="executing Posture"):
                w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                                 posture=bad)
        assert w.journal.get_hold(hold["hold_id"])["decided"] is None
        assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                                posture="COOLING_OFF").ok


# ---- F1 [codex r2 MED 3]: a subject receipt's id basis includes its posture; the close says WON/LOST itself ---------
def test_f1_two_closes_of_one_subject_at_two_postures_get_two_ids():
    def rid(posture: Posture) -> str:
        face = {"context_id": "c", "gate": {"verdict": "denied", "by": "on-loop"}, "terminal_state": "refused"}
        return gate_record(created_at="2026-10-27T12:00:00+00:00", action_name="link0", proposal_id=None,
                           posture=posture, fired=False, action_face=face, subject="run:r1")[0]
    assert rid(Posture.COOLING_OFF) != rid(Posture.CONFIRM)


def test_f1_end_unstarted_returns_its_close_won_then_lost(tmp_path):
    w, p, hold = _cooling_world(V3World, tmp_path)
    build = w.gate._pending_cancels(_pending_of(hold), hold, FIXED.isoformat(), Posture.COOLING_OFF)
    first = w.journal.end_unstarted(hold["run_id"], hold["effect_id"], reason="x", receipt=build("x"))
    assert isinstance(first, J.Closed) and first.terminal is J.Terminal.WON
    later = w.gate._pending_cancels(_pending_of(hold), hold, FIXED.isoformat(), Posture.CONFIRM)("y")
    second = w.journal.end_unstarted(hold["run_id"], hold["effect_id"], reason="y", receipt=later)
    assert isinstance(second, J.Closed) and second.terminal is J.Terminal.LOST and second.receipt_id == first.receipt_id


def _pending_of(hold):
    from levain.autonomic.gate import PendingAction
    return PendingAction.from_dict(hold["pending"])


# ---- F2 [complement r2 MED 1]: the unattended-confirm refusal runs only on the silence branch -----------------------
def test_f2_a_rung_rose_cancel_that_found_the_effect_started_closes_nothing_more(tmp_path):
    # the stop-reason branch found the effect started (it ran elsewhere): the fire below reports it, and the
    # unattended-confirm refusal (whose close would be a second "refused" end over a started effect) does not run
    w, p, hold = _cooling_world(V3World, tmp_path)
    assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                            posture="COOLING_OFF").ok
    started = J.EffectOutcome(J.EffectStatus.REPLAYED, None, receipt_id="rid-x")
    calls = []

    def ended(*a, **kw):
        calls.append(kw.get("reason"))
        return started                                             # the effect already began
    w.journal.end_unstarted = ended
    w.journal.cancel_unstarted = ended
    w.gate._reject_v3 = lambda *a, **kw: pytest.fail("a refusal after the stop-reason branch")
    w.tool_risk[0] = HIGH                                          # the rung needed now is above the one approved
    w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    assert calls == ["revalidate:risk_floor_rose"] and _run_close(w, w.only_run()) == []


# ---- F3 [deepseek r2 MED 1]: an integrity reject with an unreadable pending says what it recorded -------------------
def test_f3_an_unreadable_pending_reject_reports_the_receipt_that_closed_the_run(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    hold = w.journal.find_pending(w.open_pending_id())
    broken = {**hold, "pending": None}
    out = w.gate._resolve_hold(broken, ConfirmDecision(approved=True, by="human"), chain_owned=True)
    closes = _run_close(w, w.only_run())
    assert out.refused and out.reason == "integrity:unreadable_pending", out
    # the outcome and the store agree: the run's close is this rejection's receipt, and the outcome names it
    assert closes == [("rejected", out.receipt_id)] and out.receipt_id is not None
    assert w.receipts.read() == []


# ---- R3 [complement r1 MED 1, RULED]: a run's confirm_class is the class its links can fire at -----------------------
def test_r3_a_floor_raised_on_loop_binding_is_sealed_confirm_class(tmp_path):
    w = V3World(tmp_path)
    w.tool_risk[0] = HIGH                     # the tool floor raises the ON_LOOP link to a confirm rung at fire
    w.mint(chain=False)
    w.dispatch("e1")
    [(seal,)] = w.q("SELECT seal FROM runs")
    assert json.loads(seal)["confirm_class"] is True


# ---- R5 [RULINGS]: on /3 a deny on an already-closed run writes no file receipt ------------------------------------
def test_r5_an_approval_whose_run_was_cancelled_since_writes_no_file_receipt(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    pending_id = w.open_pending_id()
    hold = w.journal.find_pending(pending_id)
    from tests.autonomic_confirm_keys import signed_yes
    yes = signed_yes(w.gate, pending_id)
    build = w.gate._pending_cancels(_pending_of(hold), hold, FIXED.isoformat(), Posture.CONFIRM)
    closed = w.journal.cancel(hold["run_id"], reason="operator_stop", receipt=build("operator_stop"))
    out = w.gate.resolve(pending_id, yes, chain_owned=True)
    assert out.refused and not out.fired and out.reason.endswith("; run_closed_before"), out
    assert w.receipts.read() == [] and out.receipt_id is None
    assert _run_close(w, hold["run_id"]) == [("operator_stop", closed.receipt_id)]   # the run's close is its receipt


# ---- F4 [deepseek r2 MED 2]: a deny on a completed run says so; run_closed_before only when another close exists ----
def test_f4_a_deny_on_a_completed_run_says_completed(tmp_path):
    w = V3World(tmp_path)
    b = w.mint(chain=False)
    [first] = _gate_outcomes(w.dispatch("e1"))
    assert first.fired
    run_id = w.only_run()
    from levain.autonomic.binding import binding_invocation
    req = ActionRequest(action_name="link0", payload="p", context_id="c", query_text="q", query_date="2026-10-27",
                        trust=_trust(None, 0), grounded=True,
                        authority=binding_invocation(w.store.get(b.binding_id)), run=RunRef(run_id, "link-0"))
    out = w.gate._deny(req, FIXED.isoformat(), Posture.REFUSE_ESCALATE, "late")
    assert out.refused and not out.held and out.receipt_id is None, out
    assert out.reason == "late; run_completed" and _run_close(w, run_id) == []


def test_f4_a_deny_that_loses_the_close_says_closed_before(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    hold = w.journal.find_pending(w.open_pending_id())
    build = w.gate._pending_cancels(_pending_of(hold), hold, FIXED.isoformat(), Posture.CONFIRM)
    w.journal.cancel(hold["run_id"], reason="first", receipt=build("first"))
    out = w.gate._deny(_link1_request(w, hold), FIXED.isoformat(), Posture.REFUSE_ESCALATE, "late")
    assert out.reason == "late; run_closed_before" and out.receipt_id is None


def _link1_request(w, hold) -> ActionRequest:
    p = _pending_of(hold)
    return ActionRequest(action_name=p.action_name, payload=p.payload, context_id=p.context_id,
                         query_text=p.query_text, query_date=p.query_date, trust=_trust(None, 0), grounded=True,
                         authority=w.gate._authority_of(p), run=RunRef(hold["run_id"], hold["effect_id"]))


# ---- F5 [complement r2 LOW 2 + LOW 4, deepseek r2 LOW 3] ----------------------------------------------------------
def test_f5_a_deny_on_a_never_admitted_run_says_run_not_admitted(tmp_path):
    w = V3World(tmp_path)
    b = w.mint(chain=False)
    from levain.autonomic.binding import binding_invocation
    req = ActionRequest(action_name="link0", payload="p", context_id="c", query_text="q", query_date="2026-10-27",
                        trust=_trust(None, 0), grounded=True,
                        authority=binding_invocation(w.store.get(b.binding_id)), run=RunRef("never", "link-0"))
    out = w.gate._deny(req, FIXED.isoformat(), Posture.REFUSE_ESCALATE, "x")
    assert out.refused and not out.held and out.reason == "run_not_admitted", out


def test_f5_pending_cancels_on_v2_needs_no_pending(tmp_path):
    w = World(tmp_path)
    assert w.gate._pending_cancels(None, {"run_id": "r"}, FIXED.isoformat(), Posture.CONFIRM) is None


def test_f5_revalidate_refuses_a_grant_whose_terms_changed_under_its_incarnation(tmp_path):
    import copy
    from tests.autonomic_test_only_coverage import TestOnlyCoverage, policy as grant_policy
    from tests.test_autonomic_gate_v3_s2f import _LATER
    from tests.test_autonomic_journal_wiring import LOW
    granted = grant_policy("g1", {"link0": LOW}, expires_at=_LATER)
    src = TestOnlyCoverage([granted])
    src.refresh()
    w = V3World(tmp_path)
    b = w.mint(chain=False)
    gate = w.gate
    gate._coverage = src
    effect = gate._effect("link0", LOW, LOW, w.store.get(b.binding_id))
    assert gate._grant_refusal(granted, effect) is None
    # GraduationPolicy refuses terms its incarnation does not name, so a sealed grant with other terms under the same
    # incarnation is one built past that check (defense in depth: the incarnation already binds the terms)
    other = copy.copy(granted)
    object.__setattr__(other, "terms_text", granted.terms_text + " ")
    assert gate._grant_refusal(other, effect) == "bad:grant_terms_changed"


# ---- R4 [complement r1 LOW 2]: the resolve guard is rung-monotone, and the silence path runs it at max(eff, decided) --
def test_r4_the_resolve_guard_never_refuses_at_a_higher_rung_what_it_passes_at_a_lower_one(tmp_path):
    # its one rung-sensitive check is a manual pending's "risk floor rose above the approved rung" (a binding's pending
    # carries its sealed floor and is checked by _resolve_rung instead): raising the rung can only clear it
    from types import SimpleNamespace
    w = World(tmp_path)
    from levain.autonomic import ActionManifest
    from tests.test_autonomic_journal_wiring import LOW
    w.gate._manifest = ActionManifest({"link0": LOW, "link1": HIGH})
    for action in ("link0", "link1"):
        pending = SimpleNamespace(risk_floor=None, action_name=action, payload="p", query_text="q")
        refused = [r for r in Posture if w.gate._guard_resolve_fire(pending, r, ConfirmDecision(approved=True,
                                                                                               by="human"))]
        # the refused rungs are a prefix of the ladder (downward closed)
        assert refused == list(Posture)[:len(refused)], (action, refused)


def test_r4_a_silence_decision_recorded_above_the_rung_needed_now_meets_the_guard_at_its_decided_rung(tmp_path):
    # a rung-sensitive guard (refuses below CONFIRM) on the silence path whose standing decision was recorded at
    # CONFIRM: the guard is asked at max(effective, decided) = CONFIRM, passes, and the effect fires there (/2: the /3
    # resolve path refuses an unattended confirm rung, O2)
    w, p, hold = _cooling_world(World, tmp_path)
    assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                            posture="CONFIRM").ok
    asked = []

    def guard(pending, posture, decision):
        asked.append(posture)
        return None if posture >= Posture.CONFIRM else "revalidate:rung_sensitive"
    w.gate._guard_resolve_fire = guard
    w.gate._clock = lambda: FIXED + _dt.timedelta(days=1)
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    assert asked == [Posture.CONFIRM] and out.fired and out.posture is Posture.CONFIRM, (asked, out)
