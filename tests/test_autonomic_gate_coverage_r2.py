"""14b S3a L3 r2 (RULINGS_R2 items 1-3): one adjudication time per manual resolve, coverage read only for a pending
that exists, and a resolve rung that fails closed without an ``assert``.

The world of ``tests/test_autonomic_gate_coverage.py`` (a real run journal, binding registry, pending store and gate);
the coverage source is the test-only in-memory one.
"""
from __future__ import annotations

import pytest

from levain.autonomic import ConfirmDecision, Posture
from levain.autonomic.handle import as_handle
from tests.test_autonomic_gate_coverage import FIXED, LATER, CWorld, _covering, _manual
from tests.test_autonomic_gate_coverage_r1 import PAST_GRACE
from tests.test_autonomic_journal_wiring import LOW

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")

SILENCE_YES = ConfirmDecision(approved=True, by="on-loop", reason="cooling_off_window_elapsed")


class _Clock:
    """A clock that answers ``good`` reads (at ``at``) and raises on every read after them."""

    def __init__(self, good: int, at=LATER) -> None:
        self.good, self.at, self.reads = good, at, 0

    def __call__(self):
        self.reads += 1
        if self.reads > self.good:
            raise OSError("clock gone")
        return self.at


# --- item 1: the resolve reads its adjudication time once ------------------------------------------------------

def test_a_sweep_past_the_grace_denies_unreadable_coverage_without_reading_the_clock(tmp_path):
    # the sweep's own time is the adjudication time: a raising clock no longer leaves the pending open forever
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.fail_refresh = True
    w.gate._clock = clock = _Clock(0)
    [out] = as_handle(w.gate).sweep_timeouts(PAST_GRACE)
    assert not out.fired and out.refused and out.reason == "cover:unreadable" and w.outbox() == []
    assert w.pending.get(p.pending_id) is None and clock.reads == 0


def test_a_covered_sweep_fires_on_its_own_time_with_a_raising_clock(tmp_path):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    w.gate._clock = clock = _Clock(0)
    [out] = as_handle(w.gate).sweep_timeouts(LATER)
    assert out.fired and w.outbox() == ["link0"] and clock.reads == 0


def test_a_clock_failing_only_after_the_claim_does_not_spend_the_pending(tmp_path):
    # a direct resolve reads the clock once, before the claim; nothing after the claim reads it again
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    w.gate._clock = clock = _Clock(1)
    out = w.gate.resolve(p.pending_id, SILENCE_YES)
    assert out.fired and w.outbox() == ["link0"] and clock.reads == 1


def test_with_no_time_no_coverage_refresh_is_attempted_and_the_pending_stays_open(tmp_path):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    w.gate._clock = _Clock(0)
    out = w.gate.resolve(p.pending_id, SILENCE_YES)
    assert out.refused and out.reason == "cover:unavailable" and not out.fired
    assert src.refreshes == 0 and w.pending.get(p.pending_id) is not None and w.receipts.read() == []


@pytest.mark.parametrize("decision", [ConfirmDecision(approved=False, by="human"),
                                      ConfirmDecision(approved=False, by="on-loop", reason="confirm_window_elapsed")])
def test_a_deny_with_no_time_leaves_the_pending_open(tmp_path, decision):
    # the receipt's time is read before the claim too: a clock that fails cannot spend the pending on a crash
    w, p = _manual(tmp_path)
    w.gate._clock = _Clock(0)
    out = w.gate.resolve(p.pending_id, decision)
    assert out.refused and out.reason == "clock:unavailable" and w.pending.get(p.pending_id) is not None


# --- item 2: coverage is read only for a pending that exists ---------------------------------------------------

def test_an_unknown_pending_with_unreadable_coverage_is_unknown_and_reads_nothing(tmp_path):
    src = _covering("link0")
    w, _ = _manual(tmp_path, src)
    src.fail_refresh = True
    out = w.gate.resolve("no-such-pending", SILENCE_YES)
    assert out.refused and out.reason == "unknown_pending" and src.refreshes == 0


# --- item 3: the resolve rung fails closed without an assert ----------------------------------------------------

def test_a_risk_without_its_binding_resolves_refused_by_name(tmp_path, monkeypatch):
    # ``_binding_risk_now`` gives a risk with no binding only by a fault; that must be a named refusal, never an
    # ``assert`` (gone under ``python -O``: an AttributeError on ``None``)
    w = CWorld(tmp_path, coverage=_covering("link0"))
    b = w.mint(Posture.CONFIRM)
    p = w.plant_hold(b, "link0", posture="CONFIRM")
    monkeypatch.setattr(w.gate, "_binding_risk_now", lambda *a, **k: (None, LOW, ""))
    hold = w.journal.find_pending(p.pending_id)
    rung, why, fence, clamped = w.gate._resolve_rung(p, Posture.CONFIRM, hold)
    assert (rung, why, fence, clamped) == (Posture.REFUSE_ESCALATE, "revalidate:binding_risk_unavailable:binding",
                                           None, False)
    w.now = FIXED
    assert not w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="human")).fired
    assert w.outbox() == []
