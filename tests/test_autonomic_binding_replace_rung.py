"""``BindingStore.replace_atomic``'s two rung checks (14b DESIGN_REV2 §5, HEAD R1 Q6): a re-ratification below
CONFIRM that loosens the grant needs the caller's word that a graduation policy covers the new grant at that rung
(``replace_not_covered``), and the new posture never sits below its injected risk floor (``below_risk_floor``).
Honest-caller errors: the gate's clamp at every fire is the enforcement, tested in
``test_autonomic_gate_coverage.py`` and the chain and fire path tests."""
from __future__ import annotations

import pytest

from levain.autonomic import BindingStatus, BindingStore, Guard, Posture
from tests.test_autonomic_binding import make_binding

_KILL = Guard(rationale="a spoofed sender", dissent_author="codex",
              kill_predicate={"op": "!=", "field": "dmarc", "value": "pass"}, kill_drill={"dmarc": "fail"},
              kill_authored_by="phill")


def _grant(posture, created_at="2026-06-26T13:00:00"):
    return make_binding(posture=posture, created_at=created_at,
                        guard=(_KILL,) if posture > Posture.ON_LOOP else ())


def _stored(tmp_path, posture):
    s = BindingStore(tmp_path / "bindings")
    old = _grant(posture)
    s.add(old)
    return s, old


def _covered(b, rung):
    return True


def _written(s, old, new):
    return s.get(new.binding_id) is not None or s.get(old.binding_id).status is not BindingStatus.ACTIVE


@pytest.mark.parametrize("coverage", [None, lambda b, rung: False, lambda b, rung: 1,
                                      lambda b, rung: (_ for _ in ()).throw(RuntimeError("no replay"))],
                         ids=["absent", "false", "truthy_not_true", "raises"])
def test_a_looser_rung_below_confirm_without_coverage_is_refused_and_nothing_is_written(tmp_path, coverage):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, coverage=coverage)
    assert not result and result.reason == "replace_not_covered"
    assert not _written(s, old, new)


def test_a_covered_looser_rung_is_written_and_coverage_is_asked_about_the_new_grant_at_its_rung(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    asked = []
    result = s.replace_atomic(old, new, coverage=lambda b, rung: asked.append((b.binding_id, rung)) or True)
    assert result and asked == [(new.binding_id, Posture.ON_LOOP)]
    assert s.get(old.binding_id).status is BindingStatus.REVOKED


@pytest.mark.parametrize("old_rung,new_rung", [(Posture.ON_LOOP, Posture.COOLING_OFF),       # tighter
                                               (Posture.CONFIRM_ELEVATED, Posture.CONFIRM),   # looser, >= CONFIRM
                                               (Posture.ON_LOOP, Posture.CONFIRM)])
def test_a_tighter_rung_or_one_at_or_above_confirm_needs_no_coverage(tmp_path, old_rung, new_rung):
    s, old = _stored(tmp_path, old_rung)
    assert s.replace_atomic(old, _grant(new_rung, created_at="2026-06-27T13:00:00"))


def test_a_rung_below_the_injected_risk_floor_is_refused(tmp_path):
    s, old = _stored(tmp_path, Posture.CONFIRM)
    new = _grant(Posture.COOLING_OFF, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, coverage=_covered, risk_floor_of=lambda b: Posture.CONFIRM)
    assert not result and result.reason == "below_risk_floor" and not _written(s, old, new)
    assert s.replace_atomic(old, new, coverage=_covered, risk_floor_of=lambda b: Posture.COOLING_OFF)


@pytest.mark.parametrize("floor", [lambda b: (_ for _ in ()).throw(KeyError("undeclared tool")),
                                   lambda b: 3], ids=["raises", "not_a_posture"])
def test_a_risk_floor_that_cannot_be_read_refuses(tmp_path, floor):
    s, old = _stored(tmp_path, Posture.CONFIRM)
    new = _grant(Posture.CONFIRM_ELEVATED, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, risk_floor_of=floor)
    assert not result and result.reason == "below_risk_floor" and not _written(s, old, new)


def test_a_store_check_that_refuses_first_is_the_reason_reported(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    s.set_status(old.binding_id, BindingStatus.PAUSED)
    result = s.replace_atomic(old, _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00"))
    assert not result and result.reason == "old_not_fireable"
