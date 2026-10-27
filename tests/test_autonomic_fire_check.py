"""A1c2: the fire check (1026-20 RULINGS R13-2, R13-11; 1026-21 RULINGS G1-1, G1-2).

A binding's effect that a graduation policy lowers below CONFIRM fires only after the policy that covers it is read
again (``peek_cover``: the value and the policy of one cached view) and that policy's signed guard is evaluated against
the trigger event. The outcomes are typed, in R13-2's order: the cover moved (refused, the run left open), the cover
is not the decided one (refused, the run left open: it is decided again), no policy authorizes the covered value or
its guard is incomplete or cannot be evaluated (refused, the run ended), a guard kill trips (killed), else it fires.

The real-fire tests run the Journey rig (``tests/test_graduation_journey.py``): a real authority log, the production
coverage source, ``FireDispatcher`` -> ``EfferentGate`` -> run journal. On the base (c0b55b96) the guard-kill arms, the
no-policy arm, the unevaluable arm and the no-event arm FIRED the covered link (the chain arm its link0); the moved
and stale arms were already held, at admission (``journal:stale``). The not-started pin passed on the base.
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3

import pytest

import levain.autonomic.gate as gate_module
from levain.autonomic import Posture, RunRef
from levain.autonomic.binding import binding_invocation
from levain.autonomic.coverage import COVER_MOVED, EffectiveCover
from levain.autonomic.journal import EffectStatus, EffectOutcome

pytest.importorskip("cryptography")
from tests.autonomic_test_only_coverage import TEST_GUARD  # noqa: E402
from tests.test_autonomic_gate_coverage import CWorld, _covering  # noqa: E402
from tests.test_autonomic_journal_wiring import LOW, _single_builder  # noqa: E402
from tests.test_graduation_journey import Journey  # noqa: E402

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")
needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

# a guard whose kill trips on the rig's events (dmarc "pass"), where the binding's own kill (dmarc "fail") does not
TRIPS = ({"rationale": "a passing dmarc is the case this could be wrong about",
          "kill_predicate": {"op": "==", "field": "dmarc", "value": "pass"},
          "kill_drill": {"fields": {"dmarc": "pass"}}},)


def _policy(j: Journey, tmp, guard) -> None:
    path = tmp / "policy_guard.json"
    path.write_text(json.dumps([dict(e) for e in guard]))
    assert j.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--days", "30",
                 "--id", "gp-0123456789ab", "--guard", str(path)) == 0


# --- G1-1: the covering policy's signed guard stops the fire (real fires) -------------------------------------------

@needs_keygen
@pytest.mark.parametrize("chain", [False, True])
def test_a_covering_policys_guard_kill_that_trips_stops_the_fire(tmp_path, monkeypatch, chain):
    j = Journey(tmp_path, monkeypatch)
    _policy(j, tmp_path, TRIPS)
    j.fire.mint(Posture.ON_LOOP, chain=chain)
    o = j.dispatch("a covered link whose policy guard trips")
    assert not o.fired and j.fire.outbox() == []
    assert o.reason.startswith("policy_guard_kill:gp-0123456789ab:guard_0_tripped"), o.reason
    again = j.dispatch("the next event")
    assert not again.fired and j.fire.outbox() == []


@needs_keygen
@pytest.mark.parametrize("chain", [False, True])
def test_the_same_link_fires_when_the_policy_guard_does_not_trip(tmp_path, monkeypatch, chain):
    # the control: the arm above reaches the covered rung (without the policy the link holds at CONFIRM)
    j = Journey(tmp_path, monkeypatch)
    _policy(j, tmp_path, TEST_GUARD)
    j.fire.mint(Posture.ON_LOOP, chain=chain)
    o = j.dispatch("a covered link whose policy guard does not trip")
    assert j.fire.outbox() == ["link0"] and o.posture is not Posture.REFUSE_ESCALATE


# --- R13-2's typed outcomes at the gate (the test-only source; its peek is replaced to inject each case) ----------

# the gate-level arms' policy guard: a kill of its own (the binding's kill is dmarc "fail") that the events do not trip
QUIET = ({"rationale": "a rejecting dmarc is the case this could be wrong about",
          "kill_predicate": {"op": "==", "field": "dmarc", "value": "reject"},
          "kill_drill": {"fields": {"dmarc": "reject"}}},)


def _world(tmp_path):
    src = _covering("link0", guard=QUIET)
    w = CWorld(tmp_path, coverage=src)
    w.binding = w.mint(Posture.ON_LOOP)
    return w, src


def _cancels(w) -> list[tuple[str, str]]:
    """The run journal's cancel rows (run, reason): a run a refusal ended has one."""
    conn = sqlite3.connect(w.work / "store" / "autonomic.db")
    try:
        return conn.execute("SELECT run_id, reason FROM cancels").fetchall()
    finally:
        conn.close()


def test_a_covered_value_no_policy_authorizes_is_refused_and_ends_the_run(tmp_path, monkeypatch):
    # G1-2: posture set, policy None -> refused by name, never fired
    w, src = _world(tmp_path)
    real = src.peek_cover
    monkeypatch.setattr(src, "peek_cover", lambda e, now: dataclasses.replace(real(e, now), policy=None))
    d = w.dispatch("g1-2")
    o = d.outcome
    assert not o.fired and o.refused and o.reason == "no_authorizing_policy" and w.outbox() == []
    assert [r for _, r in _cancels(w)] == ["denied:%s" % d.outcome.reason]


def test_a_cover_that_moved_at_the_fire_refuses_and_leaves_the_run_open(tmp_path, monkeypatch):
    w, src = _world(tmp_path)
    real = src.peek_cover
    monkeypatch.setattr(src, "peek_cover", lambda e, now: EffectiveCover(COVER_MOVED, None, None))
    d = w.dispatch("moved")
    assert d.outcome.held and not d.outcome.refused and d.outcome.reason == "cover:moved" and w.outbox() == []
    assert _cancels(w) == []
    monkeypatch.setattr(src, "peek_cover", real)   # the cover reads again: the next delivery fires
    assert w.dispatch("after").outcome.fired and w.outbox() == ["link0"]


def test_a_cover_other_than_the_decided_one_is_stale_and_leaves_the_run_open(tmp_path, monkeypatch):
    w, src = _world(tmp_path)
    real = src.peek_cover
    monkeypatch.setattr(src, "peek_cover",
                        lambda e, now: dataclasses.replace(real(e, now), value="1:other@2026-10-17T09:00:00.000000Z"))
    d = w.dispatch("stale")
    assert d.outcome.held and not d.outcome.refused and d.outcome.reason == "cover:stale" and w.outbox() == []
    assert _cancels(w) == []


def test_a_guard_kill_whose_evaluation_raises_is_unevaluable_and_ends_the_run(tmp_path, monkeypatch):
    w, _ = _world(tmp_path)

    def boom(predicate, event):
        if predicate == QUIET[0]["kill_predicate"]:
            raise RuntimeError("an evaluator that cannot answer")
        return False
    monkeypatch.setattr(gate_module, "kill_trips", boom)
    d = w.dispatch("unevaluable")
    assert not d.outcome.fired and d.outcome.reason == "guard_unevaluable:predicate" and w.outbox() == []
    assert [r for _, r in _cancels(w)] == ["denied:%s" % d.outcome.reason]


def test_a_covered_binding_effect_with_no_trigger_event_is_unevaluable(tmp_path):
    w, _ = _world(tmp_path)
    b = w.binding
    w.journal.start("run-noevent", binding_id=b.binding_id)
    request = dataclasses.replace(
        _single_builder(b, {"id": "z1"}), risk=LOW, authority=binding_invocation(w.store.get(b.binding_id)),
        ratified_posture=Posture.ON_LOOP, run=RunRef("run-noevent", "link-0"), trigger_event=None)
    out = w.gate.gate(request)
    assert not out.fired and out.reason == "guard_unevaluable:no_event" and w.outbox() == []


# --- O-p2-2: a HELD not_started stop is held, not refused -----------------------------------------------------------

def test_journal_stop_maps_a_held_not_started_to_held(tmp_path):
    w, _ = _world(tmp_path)
    b = w.binding
    request = dataclasses.replace(_single_builder(b, {"id": "z1"}),
                                  authority=binding_invocation(w.store.get(b.binding_id)))
    out = w.gate._journal_stop(request, EffectOutcome(EffectStatus.HELD, hold_id="h-1", why="not_started:busy"),
                               Posture.ON_LOOP)
    assert out.held and not out.refused and not out.fired and out.hold_id == "h-1"
    assert out.reason == "journal:held:not_started:busy"
