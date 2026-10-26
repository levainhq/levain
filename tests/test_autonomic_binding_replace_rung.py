"""``BindingStore.replace_atomic``'s two rung checks (14b DESIGN_REV2 §5, HEAD R1 Q6): a re-ratification below
CONFIRM needs the caller's word that a graduation policy covers the new grant at that rung
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


def _no_floor(b):
    return Posture.ABOVE_LOOP


def _written(s, old, new):
    return s.get(new.binding_id) is not None or s.get(old.binding_id).status is not BindingStatus.ACTIVE


@pytest.mark.parametrize("coverage", [None, lambda b, rung: False, lambda b, rung: 1,
                                      lambda b, rung: (_ for _ in ()).throw(RuntimeError("no replay"))],
                         ids=["absent", "false", "truthy_not_true", "raises"])
def test_a_looser_rung_below_confirm_without_coverage_is_refused_and_nothing_is_written(tmp_path, coverage):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, coverage=coverage, risk_floor_of=_no_floor)
    assert not result and result.reason == "replace_not_covered"
    assert not _written(s, old, new)


def test_a_covered_looser_rung_is_written_and_coverage_is_asked_about_the_new_grant_at_its_rung(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    asked = []
    result = s.replace_atomic(old, new, coverage=lambda b, rung: asked.append((b.binding_id, rung)) or True,
                              risk_floor_of=_no_floor)
    assert result and asked == [(new.binding_id, Posture.ON_LOOP)]
    assert s.get(old.binding_id).status is BindingStatus.REVOKED


@pytest.mark.parametrize("old_rung,new_rung", [(Posture.CONFIRM_ELEVATED, Posture.CONFIRM),   # looser, >= CONFIRM
                                               (Posture.ON_LOOP, Posture.CONFIRM)])
def test_a_rung_at_or_above_confirm_is_not_asked_for_coverage(tmp_path, old_rung, new_rung):
    s, old = _stored(tmp_path, old_rung)
    asked = []
    assert s.replace_atomic(old, _grant(new_rung, created_at="2026-06-27T13:00:00"),
                            coverage=lambda b, rung: asked.append(rung) and False, risk_floor_of=_no_floor)
    assert asked == []


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
    result = s.replace_atomic(old, new, coverage=_covered, risk_floor_of=floor)
    assert not result and result.reason == "below_risk_floor" and not _written(s, old, new)


def test_a_store_check_that_refuses_first_is_the_reason_reported(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    s.set_status(old.binding_id, BindingStatus.PAUSED)
    result = s.replace_atomic(old, _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00"),
                              coverage=None, risk_floor_of=None)
    assert not result and result.reason == "old_not_fireable"


# --- L3 r1 (14b S3b-fix) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("omitted", ["coverage", "risk_floor_of"])
def test_coverage_and_the_risk_floor_are_required_so_neither_check_is_skipped_by_leaving_it_out(tmp_path, omitted):
    # codex MED 1 / complement LOW 1 / deepseek LOW 2: an omitted risk_floor_of skipped the floor check
    s, old = _stored(tmp_path, Posture.CONFIRM)
    new = _grant(Posture.CONFIRM_ELEVATED, created_at="2026-06-27T13:00:00")
    kwargs = {"coverage": _covered, "risk_floor_of": _no_floor}
    del kwargs[omitted]
    with pytest.raises(TypeError, match=omitted):
        s.replace_atomic(old, new, **kwargs)
    assert not _written(s, old, new)


def test_a_risk_floor_passed_as_none_refuses_rather_than_skipping(tmp_path):
    s, old = _stored(tmp_path, Posture.CONFIRM)
    new = _grant(Posture.CONFIRM_ELEVATED, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, coverage=_covered, risk_floor_of=None)
    assert not result and result.reason == "below_risk_floor" and not _written(s, old, new)


@pytest.mark.parametrize("old_rung,new_rung", [(Posture.ON_LOOP, Posture.ON_LOOP),          # same-rung re-author
                                               (Posture.ON_LOOP, Posture.COOLING_OFF),      # tighter, below CONFIRM
                                               (Posture.CONFIRM, Posture.COOLING_OFF)])
def test_every_new_rung_below_confirm_is_asked_for_coverage(tmp_path, old_rung, new_rung):
    # complement LOW 2: an ON_LOOP -> ON_LOOP re-author with new tools was never coverage-checked
    s, old = _stored(tmp_path, old_rung)
    new = _grant(new_rung, created_at="2026-06-27T13:00:00")
    result = s.replace_atomic(old, new, coverage=lambda b, rung: False, risk_floor_of=_no_floor)
    assert not result and result.reason == "replace_not_covered" and not _written(s, old, new)
    asked = []
    assert s.replace_atomic(old, new, coverage=lambda b, rung: asked.append((b.binding_id, rung)) or True,
                            risk_floor_of=_no_floor)
    assert asked == [(new.binding_id, new_rung)]


def test_the_rung_checks_are_not_run_when_a_store_check_refuses(tmp_path):
    # codex MED 2 / deepseek LOW 1: the rung checks ran before the lock, so a refused write still asked them
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    s.set_status(old.binding_id, BindingStatus.PAUSED)
    asked = []
    result = s.replace_atomic(old, _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00"),
                              coverage=lambda b, rung: asked.append("coverage") or True,
                              risk_floor_of=lambda b: asked.append("floor") or Posture.ABOVE_LOOP)
    assert not result and result.reason == "old_not_fireable"
    assert asked == []


def test_the_rung_checks_run_under_the_store_write_lock(tmp_path):
    # codex MED 2: the coverage read is as late as the commit (inside the transaction that writes)
    import sqlite3
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    held = []

    def _probe():
        conn = sqlite3.connect(s.db.path, timeout=0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            held.append(False)
        except sqlite3.OperationalError:
            held.append(True)
        finally:
            conn.close()

    assert s.replace_atomic(old, _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00"),
                            coverage=lambda b, rung: _probe() or True,
                            risk_floor_of=lambda b: _probe() or Posture.ABOVE_LOOP)
    assert held == [True, True]


def test_record_runs_in_the_replace_transaction_after_the_write_and_commits_with_it(tmp_path):
    # RULINGS_S1 item 1: the audit row's home is replace_atomic's own transaction
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    seen = []

    def record(conn):
        assert conn.in_transaction
        rows = {r[0] for r in conn.execute("SELECT binding_id FROM bindings")}
        seen.append(new.binding_id in rows)
        conn.execute("INSERT INTO meta (key, value) VALUES ('audit-probe', 'x')")

    assert s.replace_atomic(old, new, coverage=_covered, risk_floor_of=_no_floor, record=record)
    assert seen == [True] and s.db.meta("audit-probe") == "x"


def test_a_record_that_raises_rolls_the_replace_back(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")

    def record(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('audit-probe', 'x')")
        raise RuntimeError("audit write failed")

    with pytest.raises(RuntimeError, match="audit write failed"):
        s.replace_atomic(old, new, coverage=_covered, risk_floor_of=_no_floor, record=record)
    assert not _written(s, old, new) and s.db.meta("audit-probe") is None


def test_record_is_not_run_when_a_check_refuses(tmp_path):
    s, old = _stored(tmp_path, Posture.COOLING_OFF)
    new = _grant(Posture.ON_LOOP, created_at="2026-06-27T13:00:00")
    ran = []
    result = s.replace_atomic(old, new, coverage=lambda b, rung: False, risk_floor_of=_no_floor,
                              record=lambda conn: ran.append(1))
    assert not result and ran == []
