"""R15-5 S2, first slice (levain project_memory 1027-14/r155s2/BRIEF.md items a-d and g; e-f are handed off in its
RETURN.md): the run seal at :meth:`BindingStore.admit` on a ``levain-autonomic/3`` store, the silence path firing at
``max(effective, decided)`` (the store's ``effects_held_tied`` not relaxed), a signed approval always recording its
rung, a link's seal naming only its own hold, and no receipt written again on a ``/3`` replay."""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import pytest

from levain.autonomic import (ActionManifest, ActionRequest, Binding, BindingStatus, BindingStore, ConfirmDecision,
                              EfferentGate, GateReceiptStore, Guard, Posture, RunJournal, RunRef, SubGoal,
                              TriggerSpec, hold_id_for, manual_invocation)
from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic.binding import run_seal_for
from levain.autonomic.journal import EffectOutcome, EffectStatus, run_seal
from tests.autonomic_confirm_keys import signed_yes
from tests.test_autonomic_attempts_int_fix import (COVER, GRANT, TIED, intent, open_hold, run as start_run,
                                                   silence_approve)
from tests.test_autonomic_journal_wiring import FIXED, LOW, TIGHT, World, _trust

pytestmark = pytest.mark.usefixtures("test_only_confined_executor", "test_only_every_effect_covered")

KILL = Guard(rationale="spoofed sender", dissent_author="codex",
             kill_predicate={"op": "==", "field": "dmarc", "value": "fail"}, kill_drill={"dmarc": "fail"},
             kill_authored_by="operator")


def _binding(posture: Posture, *, links: int = 1, at: str = "2026-10-27T09:00:00") -> Binding:
    goal = (SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),
            SubGoal(goal="email it", tools=("mail.send",), output="email:self"))[:links]
    return Binding.create(created_by="operator", created_at=at,
                          trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                          goal=goal, tightness=TIGHT, posture=posture, guard=(KILL,), status=BindingStatus.PAUSED)


def _v3_store(tmp_path) -> tuple[RunJournal, BindingStore]:
    st = s.AutonomicStore3(tmp_path / "store")
    j = RunJournal(st)
    return j, BindingStore(tmp_path / "store", journal=j)


# ---- a: BindingStore.admit seals a /3 run through the one run-seal builder ------------------------------------------
@pytest.mark.parametrize("posture, links, confirm_class", [(Posture.ON_LOOP, 1, False), (Posture.CONFIRM, 2, True)])
def test_a_v3_admission_seals_the_run_from_the_admitted_binding(tmp_path, posture, links, confirm_class):
    j, store = _v3_store(tmp_path)
    b = _binding(posture, links=links)
    store.add(b)
    store.ratify(b.binding_id)
    assert store.admit(b.binding_id, "r1") is not None
    with j.db.read() as c:
        (seal,) = c.execute("SELECT seal FROM runs WHERE run_id = 'r1'").fetchone()
    assert seal == run_seal_for(b) == run_seal(confirm_class=confirm_class, guard=[KILL.to_dict()], links=links)
    assert store.admit(b.binding_id, "r1") is not None     # a re-delivery keeps its first admission
    with j.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_a_v2_admission_still_starts_an_unsealed_run(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)
    assert w.store.admit(b.binding_id, "r1") is not None
    with w.journal.db.read() as c:
        assert c.execute("SELECT run_id FROM runs").fetchall() == [("r1",)]


# ---- b: the silence path fires at max(effective, decided) -------------------------------------------------------
def test_b_a_silence_decision_recorded_above_the_rung_needed_now_fires_at_its_decided_rung(tmp_path):
    # the silence branch fired at the re-derived rung even when the standing decision was recorded higher; a /3 store
    # refuses that attempt (effects_held_tied), so the gate fires at max(effective, decided)
    w = World(tmp_path)
    b = _binding(Posture.COOLING_OFF, at="2026-10-27T09:00:00co")
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.gate._auto_fire_actions = frozenset({"link0"})
    w.dispatch("co1")
    [p] = w.gate.open_pendings()
    hold = w.journal.find_pending(p.pending_id)
    assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                            posture="CONFIRM").ok           # recorded above the COOLING_OFF needed now
    w.gate._clock = lambda: FIXED + _dt.timedelta(days=1)      # past the silence window
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    assert out.fired and out.posture is Posture.CONFIRM, out
    [rec] = [r for r in w.receipts.read() if r.fired]
    assert rec.posture == "CONFIRM"


def test_b_the_store_still_refuses_a_held_attempt_sealed_below_its_silence_decision(tmp_path):
    # effects_held_tied is NOT relaxed for a non-human decision (RULINGS O-p2-1): attempt 1 sealed at a rung below the
    # one the silence decision recorded is refused; at the decided rung it is admitted
    store = s.AutonomicStore3(tmp_path / "store")
    with store.write() as c:
        start_run(c)
        open_hold(c)
        silence_approve(c, posture="CONFIRM")
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match=TIED):
            intent(c, by="binding", posture="COOLING_OFF", grant=GRANT, cover=COVER)
    with store.write() as c:
        assert intent(c, by="binding", posture="CONFIRM", grant=GRANT, cover=COVER) == 1


# ---- c: a signed approval always records the rung it met ----------------------------------------------------------
def test_c_a_signed_approval_records_its_decided_rung(tmp_path):
    # a decision with no recorded rung imposes no rung floor on the store (complement LOW 2, 1027-11/op21intfix), so
    # every signed approval the gate writes carries one
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    pending_id = w.open_pending_id()
    hold_id = w.journal.find_pending(pending_id)["hold_id"]
    w.resolve_open_as(signed_yes(w.gate, pending_id))
    hold = w.journal.get_hold(hold_id)
    assert hold["decided"] is True and hold["by"] == "human"
    assert hold["decided_posture"] in Posture.__members__


# ---- d: an attempt's seal names only its own link's hold (hold_id_for) ---------------------------------------------
def test_d_an_attempt_seals_no_decision_from_another_links_hold(tmp_path):
    store = s.AutonomicStore3(tmp_path / "store")
    with store.write() as c:
        start_run(c, links=2)
        c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
                  "VALUES (?, 'r', 'link-1', 'b', 'd1', 1)", (hold_id_for("r", "link-1"),))
        J.decide_in(c, hold_id_for("r", "link-1"), approve=True, digest="d1", by=J.UNSIGNED_DECIDER,
                    posture="CONFIRM")
        assert intent(c, by="on-loop", posture="ON_LOOP") == 1
        (seal,) = c.execute("SELECT seal FROM effects WHERE effect_id = 'link-0'").fetchone()
    assert "decision" not in json.loads(seal)


# ---- g: a /3 replay never writes its receipt again ----------------------------------------------------------------
def test_g_a_v3_replay_without_a_noted_receipt_writes_none(tmp_path):
    j, _ = _v3_store(tmp_path)
    receipts = GateReceiptStore(tmp_path / "receipts.jsonl")
    gate = EfferentGate(manifest=ActionManifest({"link0": LOW}), store=receipts, executor=object(),  # type: ignore[arg-type]
                        clock=lambda: FIXED, journal=j)
    request = ActionRequest(action_name="link0", payload="p", context_id="c", query_text="q", query_date="2026-10-27",
                            trust=_trust(None, 0), grounded=True, authority=manual_invocation(),
                            run=RunRef("r1", "link-0"))
    recorded = {"execution": {"ok": True, "detail": "", "error": None, "downstream_id": "x"}, "posture": "ON_LOOP",
                "verdict": "auto", "by": "on-loop", "face": {}}
    out = gate._replayed(request, EffectOutcome(EffectStatus.REPLAYED, recorded, receipt_id=None), FIXED.isoformat())
    assert out.replayed and out.fired and out.receipt_id is None
    assert receipts.read() == []
