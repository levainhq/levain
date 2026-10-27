"""R15-5 S1: :class:`RunJournal` over a ``levain-autonomic/3`` store (levain project_memory 1027-11/RULINGS.md "R15-5",
1027-11/r155scout/SCOUT.md section 2). A /3 mirror of the journal-wiring World scenarios, at the journal (no gate: S2):
a single link, a chain, a held link approved and denied, a not_started attempt then its retry (attempt 2 carries
attempt 1's authority), and a digest_changed cancel. Every terminal is written with its receipt (the caller builds it,
the journal never decides authority: by / posture / grant / cover arrive from the caller and the store's triggers
judge them)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic.journal import (AUTHORIZED, EffectEnd, EffectNotStarted, EffectStatus, JournalConflictError,
                                      Receipt, RunJournal, V3Fire, hold_id_for, run_seal)

GUARD = [{"rationale": "spoofed sender", "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
          "kill_drill": {"dmarc": "fail"}}]
GRANT = {"born": 1, "id": "g1", "incarnation": "1:inc",
         "terms_text": json.dumps({"kind": "graduation", "id": "g1", "guard": GUARD})}
COVER = "1:inc@c1"
FENCE = "fence-1"


class Receipts:
    """The caller's receipt builders: each receipt carries the guard its subject sealed (receipts_carry_guard)."""

    def __init__(self) -> None:
        self.n = 0
        self.ends: list[EffectEnd] = []

    def _id(self) -> str:
        self.n += 1
        return f"rcpt-{self.n}"

    def end(self, e: EffectEnd) -> Receipt:
        if not e.dry_run:
            self.ends.append(e)
        rid = self._id()
        return Receipt(rid, {"id": rid, "kind": "gate", "subject": e.subject, "fired": e.terminal_state == "fired",
                             "action_face": {"terminal_state": e.terminal_state, "guard": e.seal["floor"] or None,
                                             "grant_guard": e.seal["grant_guard"] or None}})

    def run(self, run_id: str, guard=None):
        def build(reason: str) -> Receipt:
            rid = self._id()
            return Receipt(rid, {"id": rid, "kind": "gate", "subject": f"run:{run_id}", "fired": False,
                                 "action_face": {"terminal_state": "refused", "guard": guard, "grant_guard": None}})
        return build


@pytest.fixture
def world(tmp_path):
    return RunJournal(s.AutonomicStore3(tmp_path / "store")), Receipts()


def fire(rc: Receipts, run_id: str, *, by="on-loop", posture="ON_LOOP", grant=None, cover=None, revalidate=None):
    return V3Fire(by=by, posture=posture, grant=grant, cover=cover, revalidate=revalidate, end_receipt=rc.end,
                  run_receipt=rc.run(run_id))


def effect(j, rc, run_id, effect_id="link-0", *, fn=lambda: "sent", digest="d", needs_decision=False,
           authorize=None, **kw):
    return j.effect(run_id, effect_id, digest=digest, fence=FENCE, fence_now=lambda conn: FENCE, fn=fn,
                    needs_decision=needs_decision, authorize=authorize, v3=fire(rc, run_id, **kw))


def rows(j, run_id="r1"):
    with j.db.read() as c:
        return c.execute("SELECT effect_id, attempt, state, receipt_id FROM effects WHERE run_id = ? "
                         "ORDER BY effect_id, attempt", (run_id,)).fetchall()


def receipt_mode(j, receipt_id):
    with j.db.read() as c:
        rec = c.execute("SELECT record FROM gate_receipts WHERE receipt_id = ?", (receipt_id,)).fetchone()
    return json.loads(rec[0])["action_face"]["terminal_state"]


def cancel_of(j, run_id="r1"):
    with j.db.read() as c:
        return c.execute("SELECT reason, receipt_id FROM cancels WHERE run_id = ?", (run_id,)).fetchone()


def start(j, run_id="r1", *, links=1, confirm_class=False, guard=()):
    j.start(run_id, binding_id="b", seal=run_seal(confirm_class=confirm_class, guard=list(guard), links=links))


def ok(hold, live):
    return AUTHORIZED


# ---- construction ----------------------------------------------------------------------------------------------------
def test_a_journal_runs_over_a_v3_store_and_its_runs_are_born_sealed(world):
    j, rc = world
    assert j.v3 and j.store_id.startswith("store-")
    with pytest.raises(ValueError, match="born sealed"):
        j.start("r1", binding_id="b")
    start(j)
    with j.db.read() as c:
        seal, started = c.execute("SELECT seal, started_at FROM runs WHERE run_id = 'r1'").fetchone()
    assert json.loads(seal) == {"confirm_class": False, "guard": [], "links": 1}
    assert started is None    # D71: started at its first intent, not at admission


def test_the_run_seal_builder_refuses_a_malformed_admission():
    for bad in ({"confirm_class": 1, "guard": [], "links": 1}, {"confirm_class": False, "guard": {}, "links": 1},
                {"confirm_class": False, "guard": [], "links": 0}, {"confirm_class": False, "guard": [], "links": True},
                {"confirm_class": False, "guard": ["x"], "links": 1}):
        with pytest.raises(ValueError):
            run_seal(**bad)


def test_a_confirm_class_run_without_its_complete_guard_is_refused_by_the_store(world):
    j, _ = world
    with pytest.raises(JournalConflictError, match="D68"):
        start(j, confirm_class=True)
    start(j, "r2", confirm_class=True, guard=GUARD)


def test_a_v3_effect_without_its_fire_and_a_v2_effect_with_one_are_refused(world, tmp_path):
    j, rc = world
    start(j)
    with pytest.raises(ValueError, match="V3Fire"):
        j.effect("r1", "link-0", digest="d", fence=FENCE, fence_now=lambda c: FENCE, fn=lambda: 1)
    v2 = RunJournal(tmp_path / "v2")
    v2.start("r1", binding_id="b")
    with pytest.raises(ValueError, match="V3Fire"):
        v2.effect("r1", "link-0", digest="d", fence=FENCE, fence_now=lambda c: FENCE, fn=lambda: 1, v3=fire(rc, "r1"))
    assert rows(j) == []


# ---- a single link, a chain ------------------------------------------------------------------------------------------
def test_a_single_link_fires_once_with_its_receipt_and_replays_on_redelivery(world):
    j, rc = world
    start(j)
    calls = []
    out = effect(j, rc, "r1", fn=lambda: calls.append(1) or "sent")
    assert out.status is EffectStatus.DONE and out.result == "sent"
    [(eid, attempt, state, rid)] = rows(j)
    assert (eid, attempt, state, rid) == ("link-0", 1, "done", out.receipt_id)
    assert receipt_mode(j, rid) == "fired"
    with j.db.read() as c:
        assert c.execute("SELECT started_at FROM runs WHERE run_id = 'r1'").fetchone()[0] is not None
    again = effect(j, rc, "r1", fn=lambda: calls.append(1) or "again")
    assert again.status is EffectStatus.REPLAYED and again.result == "sent" and again.receipt_id == rid
    assert calls == [1]
    j.note_receipt("r1", "link-0", rid)          # the receipt is already the row's: a no-op
    with pytest.raises(JournalConflictError):
        j.note_receipt("r1", "link-0", "another")


def test_a_chain_fires_each_link_once_and_replays_every_link(world):
    j, rc = world
    start(j, links=2)
    assert effect(j, rc, "r1", "link-0", fn=lambda: 0).status is EffectStatus.DONE
    assert effect(j, rc, "r1", "link-1", fn=lambda: 1, digest="d1").status is EffectStatus.DONE
    assert [r[:3] for r in rows(j)] == [("link-0", 1, "done"), ("link-1", 1, "done")]
    assert effect(j, rc, "r1", "link-0", fn=lambda: 9).status is EffectStatus.REPLAYED
    assert effect(j, rc, "r1", "link-1", fn=lambda: 9, digest="d1").status is EffectStatus.REPLAYED


def test_an_executor_that_raises_ends_unknown_with_its_receipt_and_is_poisoned(world):
    j, rc = world
    start(j)

    def boom():
        raise RuntimeError("mid-send")

    with pytest.raises(RuntimeError):
        effect(j, rc, "r1", fn=boom)
    [(_, _, state, rid)] = rows(j)
    assert state == "unknown" and receipt_mode(j, rid) == "outcome_unknown"
    assert effect(j, rc, "r1").status is EffectStatus.POISONED


def test_a_revalidate_refusal_writes_nothing_and_does_not_start_the_run(world):
    j, rc = world
    start(j)
    out = effect(j, rc, "r1", revalidate=lambda conn: "bad:grant_revoked")
    assert out.status is EffectStatus.STALE and out.why == "grant_revoked"
    out = effect(j, rc, "r1", revalidate=lambda conn: "unavailable:verifier_down")
    assert out.status is EffectStatus.HELD and out.why == "unavailable:verifier_down"
    assert rows(j) == []
    with j.db.read() as c:
        assert c.execute("SELECT started_at FROM runs").fetchone()[0] is None
    assert effect(j, rc, "r1", revalidate=lambda conn: None).status is EffectStatus.DONE


def test_an_effect_whose_receipt_cannot_be_built_is_refused_before_it_fires(world):
    j, rc = world
    start(j)
    calls = []

    def bad_end(e: EffectEnd) -> Receipt:
        if e.terminal_state == "outcome_unknown":
            raise ValueError("no unknown face")
        return rc.end(e)

    with pytest.raises(s.CloseUnbuildable, match="outcome_unknown"):
        j.effect("r1", "link-0", digest="d", fence=FENCE, fence_now=lambda c: FENCE, fn=lambda: calls.append(1),
                 v3=V3Fire(by="on-loop", posture="ON_LOOP", end_receipt=bad_end, run_receipt=rc.run("r1")))
    assert calls == [] and rows(j) == []


def test_a_fired_effect_whose_receipt_cannot_be_built_is_poisoned_never_fired_again(world):
    j, rc = world
    start(j)

    def end(e: EffectEnd) -> Receipt:
        if not e.dry_run and e.terminal_state == "fired":
            raise RuntimeError("receipt store down")
        return rc.end(e)

    calls = []
    v3 = V3Fire(by="on-loop", posture="ON_LOOP", end_receipt=end, run_receipt=rc.run("r1"))
    with pytest.raises(RuntimeError, match="receipt store down"):
        j.effect("r1", "link-0", digest="d", fence=FENCE, fence_now=lambda c: FENCE, fn=lambda: calls.append(1), v3=v3)
    assert rows(j) == [("link-0", 1, "intent", None)]
    assert effect(j, rc, "r1", fn=lambda: calls.append(1)).status is EffectStatus.POISONED
    assert calls == [1]
    with pytest.raises(JournalConflictError, match="I2"):
        start(j, "r2")
        effect(j, rc, "r2")
        j.cancel("r2", reason="late", receipt=rc.run("r2")("late"))


# ---- a held link: approve, deny ----------------------------------------------------------------------------------------
def test_a_held_link_approved_by_a_person_fires_with_the_decision_in_its_seal(world):
    j, rc = world
    start(j)
    out = effect(j, rc, "r1", needs_decision=True, by="human", posture="CONFIRM")
    assert out.status is EffectStatus.HELD and rows(j) == []
    h = hold_id_for("r1", "link-0")
    assert j.decide(h, approve=True, digest="d", by="human", posture="CONFIRM", signer="k", signature="sig",
                    challenge="ch", fence=FENCE).ok
    out = effect(j, rc, "r1", needs_decision=True, authorize=ok, by="human", posture="CONFIRM")
    assert out.status is EffectStatus.DONE
    with j.db.read() as c:
        seal = json.loads(c.execute("SELECT seal FROM effects").fetchone()[0])
    assert seal["decision"] == {"generation": 1, "by": "human", "posture": "CONFIRM"} and seal["by"] == "human"


def test_a_held_link_sealed_below_its_decided_rung_is_refused_by_the_store(world):
    j, rc = world
    start(j)
    effect(j, rc, "r1", needs_decision=True)
    h = hold_id_for("r1", "link-0")
    assert j.decide(h, approve=True, digest="d", by="on-loop", posture="COOLING_OFF").ok
    with pytest.raises(JournalConflictError, match="int-I1"):
        effect(j, rc, "r1", authorize=ok, by="on-loop", posture="ON_LOOP")   # the journal passes; the store judges
    assert rows(j) == []


def test_a_denied_link_closes_its_run_with_its_receipt_and_never_fires(world):
    j, rc = world
    start(j)
    effect(j, rc, "r1", needs_decision=True)
    h = hold_id_for("r1", "link-0")
    with pytest.raises(ValueError, match="receipt"):
        j.decide(h, approve=False, digest="d", by="human")
    out = j.decide(h, approve=False, digest="d", by="human", receipt=rc.run("r1")("rejected"))
    assert out.ok and out.reason == "rejected" and out.closed.terminal is J.Terminal.WON
    reason, rid = cancel_of(j)
    assert reason == "rejected" and receipt_mode(j, rid) == "refused"
    calls = []
    assert effect(j, rc, "r1", fn=lambda: calls.append(1)).status is EffectStatus.CANCELLED
    assert calls == [] and rows(j) == []


def test_a_cancel_and_a_cancel_unstarted_close_with_their_receipts(world):
    j, rc = world
    start(j)
    start(j, "r2", links=2)
    effect(j, rc, "r2", "link-0")
    assert j.cancel_unstarted("r2", "link-0", reason="kill", receipt=rc.run("r2")("kill")).status \
        is EffectStatus.REPLAYED                                                 # started: nothing closed
    assert cancel_of(j, "r2") is None
    assert j.cancel_unstarted("r2", "link-1", reason="kill", receipt=rc.run("r2")("kill")) is None
    assert cancel_of(j, "r2")[0] == "kill"
    closed = j.cancel("r1", reason="refused", receipt=rc.run("r1")("refused"))
    assert closed.terminal is J.Terminal.WON and cancel_of(j)[0] == "refused"
    assert j.cancel("r1", reason="again", receipt=rc.run("r1")("again")).terminal is J.Terminal.LOST


# ---- not_started, then the retry --------------------------------------------------------------------------------------
def _not_yet():
    raise EffectNotStarted("spawn refused")


def test_a_not_started_attempt_closes_receipted_and_the_retry_carries_its_held_authority(world):
    j, rc = world
    start(j)
    effect(j, rc, "r1", needs_decision=True)
    h = hold_id_for("r1", "link-0")
    assert j.decide(h, approve=True, digest="d", by="on-loop", posture="COOLING_OFF").ok
    auth = dict(authorize=ok, by="binding", posture="COOLING_OFF", grant=GRANT, cover=COVER)
    out = effect(j, rc, "r1", fn=_not_yet, **auth)
    assert out.status is EffectStatus.HELD and out.why == "not_started:spawn refused" and out.hold_id == h
    [(_, attempt, state, rid)] = rows(j)
    assert (attempt, state) == (1, "not_started") and receipt_mode(j, rid) == "refused"
    with j.db.read() as c:
        assert c.execute("SELECT n, attempt FROM held_counts").fetchone() == (1, 1)
        assert s.not_started_receipts(c) == frozenset({rid})
    assert j.approved_unrun()[0]["hold_id"] == h        # the approval still stands
    out = effect(j, rc, "r1", fn=lambda: "sent", **auth)
    assert out.status is EffectStatus.DONE
    assert [r[1:3] for r in rows(j)] == [(1, "not_started"), (2, "done")]
    with j.db.read() as c:
        seals = [json.loads(x) for (x,) in c.execute("SELECT seal FROM effects ORDER BY attempt")]
    assert seals[0] == seals[1] and seals[1]["decision"] == {"generation": 1, "by": "on-loop",
                                                             "posture": "COOLING_OFF"}
    assert [e.subject for e in rc.ends] == ["effect:r1:link-0", "effect:r1:link-0#2"]
    again = effect(j, rc, "r1", fn=lambda: "twice", **auth)      # the link reads at its latest attempt: done
    assert again.status is EffectStatus.REPLAYED and again.result == "sent" and again.receipt_id == out.receipt_id


def test_a_holdless_retry_with_weaker_authority_is_refused_by_the_store(world):
    j, rc = world
    start(j)
    assert effect(j, rc, "r1", fn=_not_yet, by="binding", posture="COOLING_OFF", grant=GRANT,
                  cover=COVER).status is EffectStatus.HELD
    with pytest.raises(JournalConflictError, match="R3-1/R3-2"):
        effect(j, rc, "r1", by="binding", posture="COOLING_OFF", grant=GRANT, cover="1:inc@c2")
    assert effect(j, rc, "r1", by="binding", posture="COOLING_OFF", grant=GRANT,
                  cover=COVER).status is EffectStatus.DONE


def test_not_started_attempts_at_the_held_bound_close_the_run(world):
    j, rc = world
    start(j)
    with j.db.read() as c:
        limit = s.held_limit(c)
    for n in range(1, limit):
        assert effect(j, rc, "r1", fn=_not_yet).status is EffectStatus.HELD
    out = effect(j, rc, "r1", fn=_not_yet)
    assert out.status is EffectStatus.CANCELLED and out.why == "held_limit:not_started:spawn refused"
    assert cancel_of(j)[0] == "held_limit:not_started:spawn refused"
    assert len(rows(j)) == limit and effect(j, rc, "r1").status is EffectStatus.CANCELLED


def test_an_empty_not_started_reason_still_closes_the_attempt(world):
    j, rc = world
    start(j)

    def silent():
        raise EffectNotStarted()

    assert effect(j, rc, "r1", fn=silent).why == "not_started:EffectNotStarted"
    assert rows(j)[0][2] == "not_started"


# ---- digest_changed ---------------------------------------------------------------------------------------------------
def test_an_approval_for_other_bytes_cancels_the_run_with_its_receipt(world):
    j, rc = world
    start(j)
    effect(j, rc, "r1", needs_decision=True)
    assert j.decide(hold_id_for("r1", "link-0"), approve=True, digest="d", by="on-loop", posture="ON_LOOP").ok
    calls = []
    out = effect(j, rc, "r1", digest="d-other", authorize=ok, fn=lambda: calls.append(1))
    assert out.status is EffectStatus.CANCELLED and calls == [] and rows(j) == []
    reason, rid = cancel_of(j)
    assert reason == "digest_changed" and receipt_mode(j, rid) == "refused"


def test_a_replay_of_other_bytes_cancels_the_run_and_a_completed_run_is_left_complete(world):
    j, rc = world
    start(j, links=2)
    effect(j, rc, "r1", "link-0")
    assert effect(j, rc, "r1", "link-0", digest="d-other").status is EffectStatus.CANCELLED
    assert cancel_of(j)[0] == "replay_digest_changed"
    start(j, "r2")
    effect(j, rc, "r2")
    assert effect(j, rc, "r2", digest="d-other").status is EffectStatus.CANCELLED
    assert cancel_of(j, "r2") is None        # complete (I2): nothing left to close, no receipt written


def test_hold_and_peek_on_a_v3_journal_close_a_changed_run_with_its_receipt(world):
    j, rc = world
    start(j)
    assert j.hold("r1", "link-0", digest="d", pending={"pending_id": "p1"}).new_hold
    with pytest.raises(ValueError, match="receipt"):
        j.hold("r1", "link-0", digest="d2", pending={"pending_id": "p1"})
    out = j.hold("r1", "link-0", digest="d2", pending={"pending_id": "p1"}, run_receipt=rc.run("r1"))
    assert out.status is EffectStatus.CANCELLED and cancel_of(j)[0] == "digest_changed"
    assert j.peek("r1", "link-0").status is EffectStatus.CANCELLED
