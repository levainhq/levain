"""R15-5 S2 fix round 2 (levain project_memory 1027-18/r155s2fx2/BRIEF.md): the L3 r3 findings on the gate over a
``levain-autonomic/3`` store, each with the finding it closes."""
from __future__ import annotations

import datetime as _dt

import pytest

from levain.autonomic import ConfirmDecision, Posture
from levain.autonomic import journal as J
from tests.autonomic_confirm_keys import signed_yes
from tests.test_autonomic_gate_v3_s2f import V3World, _run_close
from tests.test_autonomic_gate_v3_s2fx import _cooling_world, _legacy_decide
from tests.test_autonomic_journal_wiring import FIXED, OutboxExecutor

pytestmark = pytest.mark.usefixtures("test_only_confined_executor", "test_only_every_effect_covered")


# ---- P1 [codex r3 MED]: a POISONED effect is effect-terminal, not a closed run ---------------------------------------
def _approved_link1_interrupted(w: V3World, monkeypatch, *, store_takes_the_end: bool):
    # a person's signed yes on the held link-1, whose effect is interrupted mid-call (the process dies there): the
    # attempt ends unknown with the receipt built at admission, or, when the store takes no write, stays intent with no
    # receipt (POISONED once the lease drops). Returns the pending id and the yes, for the redelivery
    w.mint(chain=True)
    w.dispatch("c1")
    pending_id = w.open_pending_id()
    yes = signed_yes(w.gate, pending_id)

    def dies(self, action_name, payload, *, context_id):
        raise KeyboardInterrupt
    with monkeypatch.context() as m:   # (the fixtures' own patches stay)
        m.setattr(OutboxExecutor, "execute", dies)
        if not store_takes_the_end:
            def no_write(*a, **kw):
                raise J.JournalCorruptError("the store takes no write")
            m.setattr(J, "close_effect_in", no_write)
        try:
            w.gate.resolve(pending_id, yes, chain_owned=True)
        except KeyboardInterrupt:
            pass
    return pending_id, yes


def test_p1_a_poisoned_effect_with_its_receipt_reports_that_receipt_on_redelivery(tmp_path, monkeypatch):
    w = V3World(tmp_path)
    pending_id, yes = _approved_link1_interrupted(w, monkeypatch, store_takes_the_end=True)
    run_id = w.only_run()
    [(state, rid)] = w.q("SELECT state, receipt_id FROM effects WHERE run_id = ? AND effect_id = 'link-1'", run_id)
    assert state == "unknown" and rid is not None
    out = w.gate.resolve(pending_id, yes, chain_owned=True)
    assert out.refused and not out.fired and out.reason == "journal:poisoned", out
    assert out.receipt_id == rid and w.receipts.read() == []
    assert _run_close(w, run_id) == []   # the run was never closed: only its effect is terminal


def test_p1_a_poisoned_intent_with_no_receipt_gets_a_file_receipt_on_redelivery(tmp_path, monkeypatch):
    w = V3World(tmp_path)
    pending_id, yes = _approved_link1_interrupted(w, monkeypatch, store_takes_the_end=False)
    run_id = w.only_run()
    [(state, rid)] = w.q("SELECT state, receipt_id FROM effects WHERE run_id = ? AND effect_id = 'link-1'", run_id)
    assert state == "intent" and rid is None
    out = w.gate.resolve(pending_id, yes, chain_owned=True)
    assert out.refused and not out.fired and out.reason == "journal:poisoned", out
    [rec] = w.receipts.read()
    assert out.receipt_id is not None and rec.id == out.receipt_id
    assert _run_close(w, run_id) == []


# ---- P2 [complement r3 LOW 1]: _close_run_v3's KeyError catch covers the cancel only, not the receipt build -----------
def test_p2_a_key_error_building_the_receipt_is_not_run_not_admitted(tmp_path):
    from tests.test_autonomic_gate_v3_s2fx import _link1_request
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    hold = w.journal.find_pending(w.open_pending_id())

    def broken(*a, **kw):
        raise KeyError("a stray lookup in the build")
    w.gate._run_receipt = broken
    out = w.gate._deny(_link1_request(w, hold), "2026-10-27T12:00:00+00:00", Posture.REFUSE_ESCALATE, "late")
    assert out.reason != "run_not_admitted" and out.held, out   # not terminal: nothing durable says the run is over
    assert _run_close(w, hold["run_id"]) == []


# ---- P3 [complement r3 LOW 2]: the R1 block does not end a run whose effect the stop-reason branch found started -----
def test_p3_the_refuse_escalate_block_reports_the_started_effect_without_a_second_end(tmp_path):
    w, p, hold = _cooling_world(V3World, tmp_path)
    _legacy_decide(w, hold, "COOLING_OFF")
    started = J.EffectOutcome(J.EffectStatus.IN_FLIGHT)
    calls = []

    def ended(*a, **kw):
        calls.append(kw.get("reason"))
        return started                                             # the effect already began
    w.journal.end_unstarted = ended
    w.journal.cancel_unstarted = ended
    # not reachable through the real rung helpers: the stop-reason branch needs effective > max(posture, decided), so
    # posture is below REFUSE_ESCALATE there. Both are patched to pin the R1 block's own behaviour behind it
    w.gate._posture_of = lambda pending: Posture.REFUSE_ESCALATE   # a sealed posture that cannot fire
    w.gate._decided_rung = lambda hold, posture: Posture.COOLING_OFF
    w.gate._clock = lambda: FIXED + _dt.timedelta(days=1)          # past the silence window
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    assert len(calls) == 1, (calls, out)                           # one end only, the stop-reason branch's
    # the journal's report of the started effect (in flight: deliver again later), never a refusal of its own
    assert out.held and not out.fired and out.reason == "journal:in_flight", out
