"""R15-5 S3: the callers of a ``levain-autonomic/3`` store's upkeep (levain/autonomic/upkeep3.py) and doctor's read of
it (levain project_memory 1027-11/RULINGS.md "R15-5", 1027-11/r155scout/SCOUT.md W8, W19):
  - the projection of the store's receipts to the receipt file, marked only for the lines the file holds;
  - the /3-wired liveness call passes the store's not_started set (R2-4);
  - the reaper over a real lease: an owner SIGKILLed inside ``fn`` leaves an intent with no end, and the reaper closes
    it ``unknown`` with an ``outcome_unknown`` receipt, while a live owner's attempt is left in flight;
  - doctor shows every INVARIANT row, A1's second row (``held_at_bound_unclosed``) among them."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from levain.autonomic import schema3 as s
from levain.autonomic import upkeep3
from levain.autonomic.journal import (EffectNotStarted, EffectStatus, Receipt, RunJournal, V3Fire, run_seal,
                                      unprojected_receipts)
from levain.autonomic.liveness import gate_liveness
from levain.autonomic.posture import Posture
from levain.autonomic.store import GateReceiptStore, gate_record
from levain import doctor


def _receipt(subject: str, terminal_state: str, *, guard=None, grant_guard=None) -> Receipt:
    # a complete gate record (GateReceiptStore.read needs every _GATE_KEYS field); the nonce keeps every id fresh
    rid, record = gate_record(created_at="2026-10-27T12:00:00+00:00", action_name="send", proposal_id=None,
                              posture=Posture.ON_LOOP, fired=terminal_state == "fired",
                              action_face={"terminal_state": terminal_state, "guard": guard,
                                           "grant_guard": grant_guard, "nonce": time.monotonic_ns()},
                              subject=subject)
    return Receipt(rid, record)


def _fire(run_id: str) -> V3Fire:
    return V3Fire(by="on-loop", posture="ON_LOOP",
                  end_receipt=lambda e: _receipt(e.subject, e.terminal_state, guard=e.seal["floor"] or None,
                                                 grant_guard=e.seal["grant_guard"] or None),
                  run_receipt=lambda reason: _receipt(f"run:{run_id}", "refused"))


def _effect(j: RunJournal, fn, run_id="r1", effect_id="link-0"):
    return j.effect(run_id, effect_id, digest="d", fence="f", fence_now=lambda c: "f", fn=fn, needs_decision=False,
                    authorize=None, v3=_fire(run_id))


def _not_yet():
    raise EffectNotStarted("spawn refused")


@pytest.fixture
def j(tmp_path):
    journal = RunJournal(s.AutonomicStore3(tmp_path / "store"))
    journal.start("r1", binding_id="b", seal=run_seal(confirm_class=False, guard=[], links=1))
    return journal


def _unprojected(j):
    with j.db.read() as c:
        return [rid for rid, _ in unprojected_receipts(c)]


# ---- the projector ---------------------------------------------------------------------------------------------------
def test_project_copies_every_committed_receipt_to_the_file_and_marks_it(j, tmp_path):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE
    ids = _unprojected(j)
    assert len(ids) == 2
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    assert upkeep3.project(j, trace) == ids
    assert _unprojected(j) == []
    assert sorted(r.id for r in trace.read()) == sorted(ids)
    assert upkeep3.project(j, trace) == []                       # nothing left: the file is not appended again
    assert len((tmp_path / "receipts.jsonl").read_text().splitlines()) == 2


def test_project_leaves_unmarked_a_receipt_the_file_holds_another_record_for(j, tmp_path):
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE
    [rid] = _unprojected(j)
    path = tmp_path / "receipts.jsonl"
    other = {"id": rid, "kind": "gate", "created_at": "x", "action_name": "other", "proposal_id": None,
             "posture": "ON_LOOP", "fired": True, "action_face": {}}
    path.write_text(json.dumps(other) + "\n")
    assert upkeep3.project(j, GateReceiptStore(path)) == []
    assert _unprojected(j) == [rid]                              # doctor reads it unprojected


def test_the_upkeep_callers_refuse_a_v2_journal(tmp_path):
    v2 = RunJournal(tmp_path / "v2")
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    for call in (lambda: upkeep3.project(v2, trace), lambda: upkeep3.gate_liveness(v2, trace),
                 lambda: upkeep3.reap(v2)):
        with pytest.raises(ValueError, match="levain-autonomic/3"):
            call()


# ---- R2-4: the /3-wired liveness call passes the not_started set -----------------------------------------------------
def test_the_v3_wired_liveness_call_passes_the_not_started_set(j, tmp_path):
    assert _effect(j, _not_yet).status is EffectStatus.HELD      # attempt 1 ends not_started (receipt: refused)
    assert _effect(j, lambda: "sent").status is EffectStatus.DONE  # attempt 2 fires
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    stats = upkeep3.gate_liveness(j, trace)
    assert stats["total"] == 2 and stats["fired"] == 1
    assert stats["by_terminal_state"] == {"not_started": 1, "fired": 1}
    # the same trace without the set: the not_started receipt counts as refused (R2-4's silent misreport)
    assert gate_liveness(trace)["by_terminal_state"] == {"refused": 1, "fired": 1}


def test_the_v3_wired_liveness_call_reads_the_set_after_the_trace(j, tmp_path, monkeypatch):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    order = []
    read = trace.read
    monkeypatch.setattr(trace, "read", lambda **kw: order.append("trace") or read(**kw))
    real = s.not_started_receipts
    monkeypatch.setattr(s, "not_started_receipts", lambda c: order.append("set") or real(c))
    assert upkeep3.gate_liveness(j, trace)["by_terminal_state"] == {"not_started": 1}
    assert order == ["trace", "set"]


def test_a_failed_projection_does_not_stop_the_liveness_read(j, tmp_path, monkeypatch):
    assert _effect(j, _not_yet).status is EffectStatus.HELD
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")

    def full(rows):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(trace, "project_rows", full)
    assert upkeep3.gate_liveness(j, trace)["total"] == 0          # the file is as it was: empty
    assert len(_unprojected(j)) == 1


# ---- the reaper over a real lease, after a real crash ----------------------------------------------------------------
_CHILD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    sys.path.insert(0, {tests!r})
    from test_autonomic_upkeep3 import _effect
    from levain.autonomic import schema3 as s
    from levain.autonomic.journal import RunJournal
    root = Path({root!r})
    j = RunJournal(s.AutonomicStore3(root / "store"))

    def fn():
        (root / "inside").write_text("1")
        time.sleep(60)
        return "sent"
    _effect(j, fn)
""")


def _crash_inside_fn(root: Path) -> RunJournal:
    """A child process records link-0's intent and is SIGKILLed inside ``fn``, before its end is written. While it
    is inside, the reaper leaves the attempt in flight."""
    p = subprocess.Popen([sys.executable, "-c", _CHILD.format(tests=str(Path(__file__).parent), root=str(root))],
                         env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    j = RunJournal(s.AutonomicStore3(root / "store"))
    try:
        deadline = time.monotonic() + 30
        while not (root / "inside").exists():
            assert p.poll() is None, p.communicate()[1].decode()
            assert time.monotonic() < deadline, "the child never reached fn"
            time.sleep(0.02)
        assert j._lease_held("r1", "link-0")
        assert upkeep3.reap(j) == {"effect:r1:link-0": s.IN_FLIGHT}
    finally:
        os.kill(p.pid, signal.SIGKILL)
        assert p.wait(timeout=10) == -signal.SIGKILL
    return j


def test_a_crash_between_fn_and_its_end_is_closed_unknown_by_the_reaper(j, tmp_path):
    j = _crash_inside_fn(tmp_path)
    with j.db.read() as c:
        assert c.execute("SELECT attempt, state, receipt_id FROM effects").fetchall() == [(1, "intent", None)]
        assert s.invariant(c) == []
    assert not j._lease_held("r1", "link-0") and j.poisoned() == [("r1", "link-0")]
    trace = GateReceiptStore(tmp_path / "receipts.jsonl")
    reaped, projected = upkeep3.tick(j, trace)
    assert reaped == {"effect:r1:link-0": s.CLOSED}
    with j.db.read() as c:
        [(state, rid)] = c.execute("SELECT state, receipt_id FROM effects").fetchall()
        assert state == "unknown" and s.invariant(c) == []
        record = json.loads(c.execute("SELECT record FROM gate_receipts WHERE receipt_id = ?", (rid,)).fetchone()[0])
    assert projected == [rid]
    face = record["action_face"]
    assert (record["subject"], record["fired"], record["action_name"], record["posture"]) == \
        ("effect:r1:link-0", False, upkeep3.REAPER_ACTION, "ON_LOOP")
    assert face["terminal_state"] == "outcome_unknown" and face["gate"] == {"verdict": "auto", "by": "on-loop",
                                                                            "binding_id": "b"}
    assert [r.id for r in trace.read()] == [rid]
    assert upkeep3.reap(j) == {}                                 # a second pass finds nothing
    ran = []
    again = _effect(j, lambda: ran.append(1) or "sent")          # the redelivery never runs it again
    assert again.status is EffectStatus.POISONED and ran == []


def test_the_reaper_closes_a_crashed_attempt_with_the_guard_its_run_sealed(tmp_path):
    guard = [{"rationale": "spoofed sender", "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
              "kill_drill": {"dmarc": "fail"}}]
    j = RunJournal(s.AutonomicStore3(tmp_path / "store"))
    j.start("r1", binding_id="b", seal=run_seal(confirm_class=False, guard=guard, links=1))
    j = _crash_inside_fn(tmp_path)
    assert upkeep3.reap(j) == {"effect:r1:link-0": s.CLOSED}   # receipts_carry_guard admitted the reaper's receipt
    with j.db.read() as c:
        record = json.loads(c.execute("SELECT record FROM gate_receipts").fetchone()[0])
    assert record["action_face"]["guard"] == guard


# ---- doctor: the store's INVARIANT and census --------------------------------------------------------------------------
def _held_at_bound(j: RunJournal) -> None:
    # a held count driven to its bound by attempt-less faults, with no close written (op21b OBS-6)
    with j.db.write() as c:
        for _ in range(s.held_limit(c)):
            s.count_held(c, "request:r1:link-0", "subject:unavailable")


def test_doctor_shows_a_held_count_at_the_bound_as_an_invariant_row(j, tmp_path):
    _held_at_bound(j)
    rows = doctor._check_autonomic_store(tmp_path / "store")
    assert [(r.name, r.ok) for r in rows] == [("autonomic store INVARIANT", False)]
    assert rows[0].detail.startswith("held_at_bound_unclosed request:r1:link-0: ")
    with j.db.write() as c:                                      # the run's close clears it
        s.close_subject(c, subject="run:r1", record=_receipt("run:r1", "refused").record,
                        reason="held_limit:subject:unavailable")
    [ok] = doctor._check_autonomic_store(tmp_path / "store")
    assert ok.ok and not ok.warn and "invariant holds; 0 legacy row(s); 1 receipt(s) not yet in the receipt file" \
        in ok.detail


def test_doctor_reads_the_store_without_writing_it(j, tmp_path):
    _held_at_bound(j)
    db = tmp_path / "store" / "autonomic.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    doctor._check_autonomic_store(tmp_path / "store")
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before


def test_doctor_has_no_autonomic_row_without_a_v3_store(tmp_path):
    assert doctor._check_autonomic_store(tmp_path / "none") == []
    RunJournal(tmp_path / "v2").start("r1", binding_id="b")      # a /2 store: the binding commands report it
    assert doctor._check_autonomic_store(tmp_path / "v2") == []


def test_doctor_fails_a_v3_store_whose_schema_is_not_this_levains(j, tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / "store" / "autonomic.db", isolation_level=None)
    c.execute("CREATE TABLE stray (x)")
    c.close()
    [row] = doctor._check_autonomic_store(tmp_path / "store")
    assert not row.ok and "store-schema-mismatch" in row.detail
