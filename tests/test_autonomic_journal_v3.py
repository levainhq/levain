"""A1c1 part 2: the run journal's terminals on a ``levain-autonomic/3`` store (autogrant DESIGN r6.6 §r66.1, rev 7
§r7.1-r7.2): every close carries its receipt, WON or LOST, and a hold decision steps its generation (O-a1c1d-1).
/3 stays unwired (R15-5): these drive ``schema3.AutonomicStore3`` directly."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic.journal import Closed, Receipt, RunJournal, Terminal, hold_id_for

RUN_SEAL = json.dumps({"context_id": "c", "confirm_class": False, "guard": [], "links": 2})
EFF_SEAL = json.dumps({"posture": "ON_LOOP", "by": "on-loop", "floor": [], "grant_guard": [], "grant": None,
                       "cover": "none"})


def receipt(rid: str, subject: str, ts: str) -> Receipt:
    return Receipt(rid, {"id": rid, "kind": "gate", "subject": subject, "fired": ts == "fired",
                         "action_face": {"terminal_state": ts, "guard": None, "grant_guard": None}})


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


def run(c, run_id="r1", *, started=True, binding="b"):
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES (?, ?, 0, ?)", (run_id, binding, RUN_SEAL))
    if started:
        c.execute("UPDATE runs SET started_at = 't' WHERE run_id = ?", (run_id,))


def intent(c, run_id="r1", effect_id="link-0"):
    c.execute("INSERT INTO effects (run_id, effect_id, digest, state, seal) VALUES (?, ?, 'd', 'intent', ?)",
              (run_id, effect_id, EFF_SEAL))


def hold(c, run_id="r1", effect_id="link-0", seq=1):
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) VALUES (?, ?, ?, 'b', 'd', ?)",
              (hold_id_for(run_id, effect_id), run_id, effect_id, seq))


# ---- O-a1c1d-1: the hold's decision generation steps in the deciding statement ------------------------------------
def test_a_hold_decision_steps_its_generation_on_a_v3_store(store):
    h = hold_id_for("r1", "link-0")
    with store.write() as c:
        run(c)
        hold(c)
    # RED: V's decide statement (no generation step) is refused by holds_move
    with pytest.raises(sqlite3.IntegrityError, match="I3: an illegal hold transition"):
        with store.write() as c:
            c.execute("UPDATE holds SET decided = 1, decided_by = 'on-loop' WHERE hold_id = ? AND decided IS NULL", (h,))
    with store.write() as c:
        assert RunJournal._decide_in(c, h, approve=True, by="on-loop", posture="ON_LOOP", signer=None,
                                     signature=None, challenge=None, fence=None) == 1
        RunJournal._reopen_in(c, h)
        out = J.decide_in(c, h, approve=True, digest="d", by="on-loop")
        assert (out.ok, out.reason, out.closed) == (True, "approved", None)
    with store.read() as c:
        assert c.execute("SELECT decided, decisions FROM holds").fetchall() == [(1, 2)]
        assert c.execute("SELECT by, n FROM hold_decisions ORDER BY n").fetchall() == [("on-loop", 1), ("on-loop", 2)]
        assert s.invariant(c) == []


def test_a_rejection_closes_its_run_with_its_receipt_first(store):
    h = hold_id_for("r1", "link-0")
    with store.write() as c:
        run(c)
        hold(c)
        with pytest.raises(ValueError, match="only with its receipt"):
            J.decide_in(c, h, approve=False, digest="d", by="human")
        assert J.decide_in(c, h, approve=False, digest="x", by="human",
                           receipt=receipt("g1", "run:r1", "refused")).reason == "digest_mismatch"
        out = J.decide_in(c, h, approve=False, digest="d", by="human", receipt=receipt("g1", "run:r1", "refused"))
    assert (out.ok, out.reason, out.closed) == (True, "rejected", Closed(Terminal.WON, "g1", "rejected"))
    with store.read() as c:
        assert c.execute("SELECT run_id, reason, receipt_id FROM cancels").fetchall() == [("r1", "rejected", "g1")]
        assert c.execute("SELECT decided, decisions FROM holds").fetchall() == [(0, 1)]
        assert s.invariant(c) == []


def test_a_rejection_after_another_close_is_lost_and_still_recorded(store):
    h = hold_id_for("r1", "link-0")
    with store.write() as c:
        run(c)
        hold(c)
        assert J.close_run_in(c, "r1", reason="denied:policy", receipt=receipt("g0", "run:r1", "refused")) \
            == Closed(Terminal.WON, "g0", "denied:policy")
    with store.write() as c:
        out = J.decide_in(c, h, approve=False, digest="d", by="human", receipt=receipt("g1", "run:r1", "refused"))
    assert out.closed == Closed(Terminal.LOST, "g0", "denied:policy") and out.reason == "rejected"
    with store.read() as c:
        assert c.execute("SELECT receipt_id FROM gate_receipts").fetchall() == [("g0",)]
        assert c.execute("SELECT decided, decisions FROM holds").fetchall() == [(0, 1)]
        assert s.invariant(c) == []


def test_an_approval_takes_no_receipt(store):
    with store.write() as c:
        run(c)
        hold(c)
        with pytest.raises(ValueError, match="not terminal"):
            J.decide_in(c, hold_id_for("r1", "link-0"), approve=True, digest="d", by="on-loop",
                        receipt=receipt("g", "run:r1", "refused"))
        assert J.decide_in(c, hold_id_for("r1", "link-0"), approve=True, digest="d",
                           by="human").reason == "unsigned_approval"


# ---- one terminal per subject: WON once, LOST after -----------------------------------------------------------------
def test_two_closers_of_one_run_win_once(store):
    # §r66.1 test_cancel_is_won_once: two writers past a barrier; the write lock serializes their read-then-close
    with store.write() as c:
        run(c)
    gate = threading.Barrier(2)
    results: dict[str, Closed] = {}

    def closer(rid):
        mine = s.AutonomicStore3(store.directory)
        gate.wait()
        with mine.write() as c:
            results[rid] = J.close_run_in(c, "r1", reason=f"denied:{rid}", receipt=receipt(rid, "run:r1", "refused"))

    threads = [threading.Thread(target=closer, args=(rid,)) for rid in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [r for r in results.values() if r.terminal is Terminal.WON]
    lost = [r for r in results.values() if r.terminal is Terminal.LOST]
    assert len(won) == len(lost) == 1
    assert (lost[0].receipt_id, lost[0].reason) == (won[0].receipt_id, won[0].reason)
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM gate_receipts").fetchone() == (1,)
        assert s.invariant(c) == []


def test_a_second_receipt_for_one_subject_rolls_back_its_state_change(store):
    # the backstop under a writer that skips the WON/LOST read: the whole transaction goes
    with store.write() as c:
        run(c)
    with pytest.raises(sqlite3.IntegrityError, match="D69"):
        with store.write() as c:
            J._insert_receipt(c, receipt("g1", "run:r1", "refused"), subject="run:r1", reason="a")
            J._insert_receipt(c, receipt("g2", "run:r1", "refused"), subject="run:r1", reason="b")
    with store.read() as c:
        assert c.execute("SELECT COUNT(*) FROM cancels").fetchone() == (0,)
        assert c.execute("SELECT COUNT(*) FROM gate_receipts").fetchone() == (0,)


def test_close_unstarted_closes_only_a_run_whose_effect_has_no_row(store):
    with store.write() as c:
        run(c, "r1")
        run(c, "r2")
        intent(c, "r2")
        with pytest.raises(KeyError):
            J.close_unstarted(c, "nope", "link-0", reason="x", receipt=receipt("g", "run:nope", "refused"))
        assert J.close_unstarted(c, "r2", "link-0", reason="rung_rose", receipt=receipt("g2", "run:r2", "refused")) is None
        assert J.close_unstarted(c, "r1", "link-0", reason="rung_rose", receipt=receipt("g1", "run:r1", "refused")) \
            == Closed(Terminal.WON, "g1", "rung_rose")
    with store.read() as c:
        assert c.execute("SELECT run_id FROM cancels").fetchall() == [("r1",)]
        assert s.invariant(c) == []


def test_an_effect_ends_once_with_its_receipt(store):
    with store.write() as c:
        run(c)
        intent(c, "r1", "link-0")
        intent(c, "r1", "link-1")
        with pytest.raises(ValueError, match="not done or unknown"):
            J.close_effect_in(c, "r1", "link-0", state="intent", receipt=receipt("x", "effect:r1:link-0", "fired"))
        assert J.close_effect_in(c, "r1", "link-0", state="done", result={"ok": True},
                                 receipt=receipt("e0", "effect:r1:link-0", "fired")) == Closed(Terminal.WON, "e0")
        assert J.close_effect_in(c, "r1", "link-1", state="unknown",
                                 receipt=receipt("e1", "effect:r1:link-1", "outcome_unknown")) == Closed(Terminal.WON, "e1")
        assert J.close_effect_in(c, "r1", "link-0", state="unknown",
                                 receipt=receipt("e2", "effect:r1:link-0", "outcome_unknown")) == Closed(Terminal.LOST, "e0")
    with store.read() as c:
        assert c.execute("SELECT effect_id, state, result, receipt_id FROM effects ORDER BY effect_id").fetchall() \
            == [("link-0", "done", '{"ok": true}', "e0"), ("link-1", "unknown", None, "e1")]
        assert s.invariant(c) == []


def test_a_receipt_that_does_not_name_itself_is_refused_before_any_write():
    with pytest.raises(ValueError, match="does not name it"):
        Receipt("a", {"id": "b"})


# ---- the projection marks -------------------------------------------------------------------------------------------
def test_unprojected_receipts_are_listed_oldest_first_and_marked_once(store):
    with store.write() as c:
        for r in ("r1", "r2", "r3"):
            run(c, r)
        for rid, r in (("g3", "r3"), ("g1", "r1"), ("g2", "r2")):
            J.close_run_in(c, r, reason="x", receipt=receipt(rid, f"run:{r}", "refused"))
    with store.read() as c:
        rows = J.unprojected_receipts(c)
    assert [r for r, _ in rows] == ["g3", "g1", "g2"] and json.loads(rows[0][1])["subject"] == "run:r3"
    with store.write() as c:
        assert J.mark_projected(c, ["g3", "g1"]) == 2
        assert J.mark_projected(c, ["g3"]) == 0
    with store.read() as c:
        assert [r for r, _ in J.unprojected_receipts(c)] == ["g2"]
    with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
        with store.write() as c:
            c.execute("UPDATE gate_receipts SET projected = 0 WHERE receipt_id = 'g3'")
