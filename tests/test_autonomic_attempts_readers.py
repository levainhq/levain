"""O-p2-1 slice B2: the ``/3`` readers of a link's attempts, the seal builder, the not_started close and the retry's
admission order (levain project_memory ``1026-27/O-p2-1_DESIGN.md``: R2-5 the five readers, R3-2 + R4-1 the seal's
``$.decision``, R2-6 revalidation before the insert, 10.Q1 + R2-4 ``gate_liveness``).

/3 is unwired (R15-5): no ``levain`` command reaches this. The tests write a /3 run and its hold by SQL, as the other
/3 test files do. Every effect row here is written by :func:`journal.intent_in` through the seal
builder, and every decision by :func:`journal.decide_in`; no effect seal is hand-built."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.autonomic import journal as J
from levain.autonomic import schema3 as s
from levain.autonomic.gate import receipt_by
from levain.autonomic.journal import Closed, Receipt, RunJournal, Terminal, hold_id_for
from levain.autonomic.liveness import gate_liveness
from levain.autonomic.store import StoredGateReceipt

GUARD = [{"rationale": "spoofed sender", "kill_predicate": {"op": "==", "field": "dmarc", "value": "fail"},
          "kill_drill": {"dmarc": "fail"}}]
GRANT = {"born": 1, "id": "g1", "incarnation": "1:inc",
         "terms_text": json.dumps({"kind": "graduation", "id": "g1", "guard": GUARD})}
COVER = "1:inc@c1"
H = hold_id_for("r", "link-0")
AUTHORITY = "R3-1/R3-2: a retry carries"


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


@pytest.fixture
def store(tmp_path):
    st = s.AutonomicStore3(tmp_path / "store")
    st.meta("format")
    return st


def rec(rid, subject, ts, *, grant_guard=None) -> dict:
    return {"id": rid, "kind": "gate", "subject": subject, "fired": ts == "fired",
            "action_face": {"terminal_state": ts, "guard": None, "grant_guard": grant_guard}}


def receipt(rid, subject, ts, **kw) -> Receipt:
    return Receipt(rid, rec(rid, subject, ts, **kw))


def subj(n=1) -> str:
    return s.effect_subject("r", "link-0", n)


def run(c, links=1):
    c.execute("INSERT INTO runs (run_id, binding_id, generation, seal) VALUES ('r', 'b', 0, ?)",
              (canon({"context_id": "c", "confirm_class": False, "guard": [], "links": links}),))
    c.execute("UPDATE runs SET started_at = 't' WHERE run_id = 'r'")


def open_hold(c):
    c.execute("INSERT INTO holds (hold_id, run_id, effect_id, binding_id, digest, seq) "
              "VALUES (?, 'r', 'link-0', 'b', 'd', 1)", (H,))


def silence_approve(c):
    # the gate's silence default: the hold records its decider, on-loop (UNSIGNED_DECIDER), at the cooling-off rung
    out = J.decide_in(c, H, approve=True, digest="d", by=J.UNSIGNED_DECIDER, posture="COOLING_OFF")
    assert out.ok, out


def held_intent(c):
    # a binding's silence-approved, grant-covered effect: the receipt actor is the gate's mapping of that decision
    return J.intent_in(c, "r", "link-0", digest="d", by=receipt_by(J.UNSIGNED_DECIDER, "binding"),
                       posture="COOLING_OFF", grant=GRANT, cover=COVER)


def plain_intent(c, **kw):
    return J.intent_in(c, "r", "link-0", digest="d", by="on-loop", posture="ON_LOOP", **kw)


def not_started(c, n, *, grant_guard=None):
    def run_receipt(reason):
        return receipt(f"run{n}", "run:r", "refused")
    return J.close_not_started_in(c, "r", "link-0", n, why="spawn",
                                  receipt=receipt(f"ns{n}", subj(n), "refused", grant_guard=grant_guard),
                                  run_receipt=run_receipt)


def rows(c):
    return c.execute("SELECT attempt, state FROM effects ORDER BY attempt").fetchall()


def two_attempts(store, *, end2=None):
    # attempt 1 ended not_started and counted; attempt 2 live (intent), or ended done when end2 = "done"
    with store.write() as c:
        run(c)
        assert plain_intent(c) == 1
        not_started(c, 1)
    with store.write() as c:
        assert plain_intent(c) == 2
        if end2 == "done":
            assert J.close_effect_in(c, "r", "link-0", state="done", receipt=receipt("e2", subj(2), "fired"),
                                     result={"ok": True}).terminal is Terminal.WON


# ---- R2-5: the five readers, each with attempts 1 and 2 in different states ------------------------------------------
def test_closer_of_reads_the_attempt_its_subject_names(store):
    two_attempts(store)
    with store.read() as c:
        assert J._closer_of(c, subj(1)) == Closed(Terminal.LOST, "ns1")   # attempt 1 ended not_started
        assert J._closer_of(c, subj(2)) is None                            # attempt 2 is live
    with store.write() as c:
        J.close_effect_in(c, "r", "link-0", state="done", receipt=receipt("e2", subj(2), "fired"), result=1)
    with store.read() as c:
        assert J._closer_of(c, subj(1)) == Closed(Terminal.LOST, "ns1")
        assert J._closer_of(c, subj(2)) == Closed(Terminal.LOST, "e2")


def test_close_unstarted_reads_a_link_of_only_not_started_attempts_as_unstarted(store):
    with store.write() as c:
        run(c)
        plain_intent(c)
        not_started(c, 1)
    with store.write() as c:
        assert J.close_unstarted(c, "r", "link-0", reason="denied:x", receipt=receipt("g", "run:r", "refused")) \
            == Closed(Terminal.WON, "g", "denied:x")


def test_close_unstarted_reads_a_live_retry_as_started(store):
    two_attempts(store)
    with store.write() as c:
        assert J.close_unstarted(c, "r", "link-0", reason="denied:x", receipt=receipt("g", "run:r", "refused")) is None
        assert c.execute("SELECT COUNT(*) FROM cancels").fetchone() == (0,)


def test_close_effect_in_ends_the_live_attempt_not_the_first(store):
    two_attempts(store, end2="done")
    with store.read() as c:
        assert rows(c) == [(1, "not_started"), (2, "done")]
        assert c.execute("SELECT subject FROM gate_receipts WHERE receipt_id = 'e2'").fetchone() == (subj(2),)
        assert s.invariant(c) == []
    with store.write() as c:   # attempt 2 ended: a second end is LOST with ITS receipt; attempt 1's is its own
        assert J.close_effect_in(c, "r", "link-0", state="unknown", receipt=receipt("u", subj(2), "outcome_unknown")) \
            == Closed(Terminal.LOST, "e2")
        assert J.close_effect_in(c, "r", "link-0", state="done", attempt=1,
                                 receipt=receipt("x", subj(1), "fired")) == Closed(Terminal.LOST, "ns1")


def test_approved_unrun_lists_a_link_whose_attempts_all_did_not_start(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        held_intent(c)
        not_started(c, 1, grant_guard=GUARD)
    with store.read() as c:
        assert J.approved_unrun_in(c) == [("r", "link-0")]   # the approval stands, nothing has started
    with store.write() as c:
        assert held_intent(c) == 2
    with store.read() as c:
        assert J.approved_unrun_in(c) == []                    # attempt 2 started


def test_poisoned_lists_a_dead_live_attempt_once_and_never_a_not_started_one(store):
    with store.write() as c:
        run(c)
        plain_intent(c)
        not_started(c, 1)
        assert J.poisoned_in(c, lambda r, e: False) == []     # attempt 1 began nothing
    with store.write() as c:
        plain_intent(c)
        assert J.poisoned_in(c, lambda r, e: False) == [("r", "link-0")]   # attempt 2's owner is gone
        assert J.poisoned_in(c, lambda r, e: True) == []                    # in flight


# ---- R3-2: the end-to-end through the seal builder, a binding's silence approval ------------------------------------
def test_a_binding_silence_approval_retries_through_the_seal_builder(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        assert held_intent(c) == 1
        [(sl,)] = c.execute("SELECT seal FROM effects").fetchall()
        sealed = json.loads(sl)
        # the receipt actor and the decision's decider differ for this decision, and the seal keeps both
        assert sealed["by"] == "binding" and sealed["posture"] == "COOLING_OFF"
        assert sealed["decision"] == {"generation": 1, "by": "on-loop", "posture": "COOLING_OFF"}
        out = not_started(c, 1, grant_guard=GUARD)
        assert (out.closed, out.held, out.run_closed) == (Closed(Terminal.WON, "ns1"), 1, None)
    with store.write() as c:
        assert held_intent(c) == 2                               # admitted by effects_attempt_authority
        assert J.close_effect_in(c, "r", "link-0", state="done", result=1,
                                 receipt=receipt("e2", subj(2), "fired", grant_guard=GUARD)).terminal is Terminal.WON
    with store.read() as c:
        assert rows(c) == [(1, "not_started"), (2, "done")]
        assert s.not_started_receipts(c) == frozenset({"ns1"})
        assert s.invariant(c) == []


def test_a_retry_sealed_with_the_receipt_actor_as_its_decider_is_refused(store):
    # the overload R3-2 removed: $.decision.by must be the hold's decided_by (on-loop), not the actor (binding)
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        held_intent(c)
        not_started(c, 1, grant_guard=GUARD)
    with store.write() as c:
        sl = json.loads(J.effect_seal_in(c, "r", "link-0", by="binding", posture="COOLING_OFF", grant=GRANT,
                                         cover=COVER))
        sl["decision"]["by"] = "binding"
        with pytest.raises(sqlite3.IntegrityError, match=AUTHORITY):
            c.execute("INSERT INTO effects (run_id, effect_id, attempt, digest, state, seal) "
                      "VALUES ('r', 'link-0', 2, 'd', 'intent', ?)", (canon(sl),))


def test_a_link_without_a_hold_seals_no_decision():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        st = s.AutonomicStore3(Path(d) / "store")
        with st.write() as c:
            run(c)
            assert "decision" not in json.loads(J.effect_seal_in(c, "r", "link-0", by="on-loop", posture="ON_LOOP"))


def test_not_started_attempts_at_the_bound_close_the_run(store):
    limit = int(store.meta("held_limit"))
    with store.write() as c:
        run(c)
    for n in range(1, limit + 1):
        with store.write() as c:
            assert plain_intent(c) == n
            out = not_started(c, n)
        assert out.held == n
    assert out.run_closed == Closed(Terminal.WON, f"run{limit}", "held_limit:not_started:spawn")
    with store.write() as c:
        with pytest.raises(sqlite3.IntegrityError, match="I2: no effect begins in a closed run"):
            plain_intent(c)
    with store.read() as c:
        assert s.invariant(c) == []


# ---- R2-6: the retry's admission is revalidated BEFORE the attempt-n+1 insert ---------------------------------------
def test_a_stale_retry_is_refused_before_its_insert_and_its_hold_reopens(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        held_intent(c)
        not_started(c, 1, grant_guard=GUARD)
    calls = []
    with store.write() as c:   # delivery 2: the admission finds the cover moved
        stale = J.intent_in(c, "r", "link-0", digest="d", by="binding", posture="COOLING_OFF", grant=GRANT,
                            cover=COVER, revalidate=lambda conn: calls.append(1) or "stale")
        assert stale == "stale" and calls == [1]
        assert rows(c) == [(1, "not_started")]           # nothing inserted
        RunJournal._reopen_in(c, H)                       # legal: no row of the link has started
        assert c.execute("SELECT decided FROM holds").fetchone() == (None,)
        silence_approve(c)                                # re-decided: generation 2
    with store.write() as c:   # delivery 3
        assert held_intent(c) == 2
        assert json.loads(c.execute("SELECT seal FROM effects WHERE attempt = 2").fetchone()[0])["decision"] \
            == {"generation": 2, "by": "on-loop", "posture": "COOLING_OFF"}
    with store.read() as c:
        assert s.invariant(c) == []


def test_a_refusal_found_after_the_insert_closes_that_attempt_before_the_hold_reopens(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        held_intent(c)
        not_started(c, 1, grant_guard=GUARD)
    with store.write() as c:
        assert held_intent(c) == 2
    with pytest.raises(sqlite3.IntegrityError, match="I3: an illegal hold transition"):
        with store.write() as c:   # the live attempt 2 blocks the reopen: that is why the order matters
            RunJournal._reopen_in(c, H)
    with store.write() as c:   # the refusal closes attempt 2 not_started (counted); the reopen is then legal
        assert not_started(c, 2, grant_guard=GUARD).held == 2
        RunJournal._reopen_in(c, H)
        silence_approve(c)
    with store.write() as c:
        assert held_intent(c) == 3
    with store.read() as c:
        assert rows(c) == [(1, "not_started"), (2, "not_started"), (3, "intent")]


# ---- 10.Q1 + R2-4: gate_liveness tells a not_started refusal from another -------------------------------------------
class _Trace:
    def __init__(self, receipts, log):
        self._receipts, self._log = receipts, log

    def read(self, limit=None):
        self._log.append("read")
        return self._receipts if limit is None else self._receipts[-limit:]


def _stored(rid, ts, fired=False):
    return StoredGateReceipt(id=rid, created_at="t", action_name="a", proposal_id=None, posture="ON_LOOP",
                             fired=fired, action_face={"terminal_state": ts, "gate": {"verdict": "auto"}})


TRACE = [_stored("a", "refused"), _stored("b", "refused"), _stored("c", "fired", True), _stored("d", "killed")]


def test_gate_liveness_counts_a_listed_receipt_as_not_started_after_the_read():
    log: list[str] = []
    stats = gate_liveness(_Trace(TRACE, log), not_started=lambda: log.append("set") or frozenset({"b"}))
    assert log == ["read", "set"]
    assert stats["by_terminal_state"] == {"refused": 1, "not_started": 1, "fired": 1, "killed": 1}
    plain = gate_liveness(_Trace(TRACE, []))
    assert {k: v for k, v in stats.items() if k != "by_terminal_state"} == \
        {k: v for k, v in plain.items() if k != "by_terminal_state"}


def test_gate_liveness_without_the_set_is_unchanged():
    assert gate_liveness(_Trace(TRACE, []), not_started=None) == gate_liveness(_Trace(TRACE, [])) == {
        "total": 4, "fired": 1, "killed": 1, "by_terminal_state": {"refused": 2, "fired": 1, "killed": 1},
        "by_verdict": {"auto": 4}, "by_refuse_class": {}}


def test_gate_liveness_reads_a_v3_store_set_through_its_reader(store):
    two_attempts(store, end2="done")
    trace = [_stored("ns1", "refused"), _stored("e2", "fired", True)]
    stats = gate_liveness(_Trace(trace, []), not_started=lambda: _read_set(store))
    assert stats["by_terminal_state"] == {"not_started": 1, "fired": 1}


def _read_set(store):
    with store.read() as c:
        return s.not_started_receipts(c)


def test_receipt_by_maps_a_decider_to_the_receipt_actor():
    assert receipt_by("human", "binding") == "human"
    assert receipt_by("on-loop", "binding") == "binding"
    assert receipt_by("on-loop", "operator") == "on-loop"
    assert receipt_by(None, None) == "on-loop"


# ---- R4-1: the /2 path is unchanged (a real Gate -> RunJournal.effect run, against be0af189's) ------------------------
# The digest of `_v2_run`'s dump, made by running the same function on be0af189's code (levain-wt worktree at
# be0af18939bc, 10-27, by lane op21c): any change to what a /2 run returns or writes changes it.
V2_RUN_DIGEST = "2ad5f9299a5768d5f3c3fc43bf7ded82dc66a67418284ace00d860a530dc1b5c"


def _v2_run(tmp_path) -> str:
    """A real /2 run through the fire path (tests.test_autonomic_journal_wiring.World): a single-link fire, a chain
    whose confirm link is held and resolved by a person's signed approval (the shared hold reads, ``decision_key``
    and the admission), and a redelivery that replays. Returns every outcome and every row of the store, the receipt
    trace and the outbox, with the two values that differ on every run replaced: the store's random id (and the
    signature and challenge, which name it) and the pid."""
    import hashlib
    from tests.test_autonomic_journal_wiring import World
    single, chain = World(tmp_path / "single"), World(tmp_path / "chain")
    single.mint(chain=False)
    chain.mint(chain=True)
    out = [repr(single.dispatch("s1").outcome), repr(single.dispatch("s1").outcome), repr(chain.dispatch("c1").chain)]
    out.append(repr(chain.resolve_open(approve=True)))
    out.append(repr(chain.dispatch("c1").chain))
    secrets = []
    for w in (single, chain):
        with w.journal.db.read() as c:
            for t in ("runs", "effects", "holds", "cancels", "fences"):
                out.append(f"{t}: {c.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall()!r}")
            secrets += [x for row in c.execute("SELECT signature, challenge FROM holds").fetchall() for x in row if x]
        secrets.append(w.journal.store_id)
        out.append((w.work / "receipts.jsonl").read_text())
        out.append(repr(w.outbox()))
    text = "\n".join(out)
    for secret in secrets:   # as written and as repr() escapes it (a signature holds newlines)
        text = text.replace(secret, "<random>").replace(repr(secret)[1:-1], "<random>")
    import os
    text = text.replace(f", {os.getpid()}, ", ", <pid>, ")
    return hashlib.sha256(text.encode("utf-8")).hexdigest() + "\n" + text


@pytest.mark.usefixtures("test_only_confined_executor", "test_only_every_effect_covered")
def test_a_v2_run_through_the_gate_is_what_it_was_at_be0af189(tmp_path):
    assert _v2_run(tmp_path).split("\n", 1)[0] == V2_RUN_DIGEST


# ---- fix round 1 (levain project_memory 1027-9/op21c/FIX_r1.md) ------------------------------------------------------
def test_g1_an_attempt_of_a_link_whose_hold_is_open_is_refused_and_nothing_is_written(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        assert J.intent_in(c, "r", "link-0", digest="d", by="human", posture="CONFIRM") == "hold_open"
        assert rows(c) == []


def test_g1_an_attempt_whose_digest_is_not_the_approved_one_is_refused_and_nothing_is_written(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        assert J.intent_in(c, "r", "link-0", digest="B", by="binding", posture="COOLING_OFF", grant=GRANT,
                           cover=COVER) == "digest_changed"
        assert rows(c) == []


def test_g1_a_retry_whose_digest_is_not_the_approved_one_is_refused_before_the_store_sees_it(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        silence_approve(c)
        held_intent(c)
        not_started(c, 1, grant_guard=GUARD)
    with store.write() as c:
        assert J.intent_in(c, "r", "link-0", digest="B", by="binding", posture="COOLING_OFF", grant=GRANT,
                           cover=COVER) == "digest_changed"
        assert rows(c) == [(1, "not_started")]


def test_g2_a_not_started_close_after_the_run_was_cancelled_commits_without_a_count(store):
    with store.write() as c:
        run(c)
        assert plain_intent(c) == 1
    with store.write() as c:   # another transaction cancels the run while the attempt is in flight
        assert J.close_run_in(c, "r", reason="cancelled", receipt=receipt("cx", "run:r", "killed")).terminal \
            is Terminal.WON
    with store.write() as c:   # the executor then reports EffectNotStarted
        out = not_started(c, 1)
        assert (out.closed, out.held, out.run_closed) == (Closed(Terminal.WON, "ns1"), 0, None)
    with store.read() as c:
        assert rows(c) == [(1, "not_started")]
        assert c.execute("SELECT COUNT(*) FROM held_counts").fetchone() == (0,)
        assert J.poisoned_in(c, lambda r, e: False) == []
        assert s.invariant(c) == []


def test_g4_a_not_started_close_of_an_attempt_that_does_not_exist_is_a_key_error(store):
    with store.write() as c:
        run(c)
        plain_intent(c)
        with pytest.raises(KeyError):
            J.close_not_started_in(c, "r", "link-0", 3, why="spawn", receipt=receipt("ns3", subj(3), "refused"),
                                   run_receipt=lambda reason: receipt("run3", "run:r", "refused"))
        assert c.execute("SELECT COUNT(*) FROM gate_receipts").fetchone() == (0,)


def test_g4_a_not_started_close_of_an_ended_attempt_is_lost_and_uncounted(store):
    with store.write() as c:
        run(c, links=2)
        plain_intent(c)
        J.close_effect_in(c, "r", "link-0", state="done", receipt=receipt("e1", subj(1), "fired"), result=1)
        out = not_started(c, 1)
        assert (out.closed, out.held, out.run_closed) == (Closed(Terminal.LOST, "e1"), 0, None)
        assert c.execute("SELECT COUNT(*) FROM held_counts").fetchone() == (0,)


def test_g5_alternating_not_started_reasons_still_reach_the_bound(store):
    # the count's subject is the link's request; the reason is only its last_reason, so a varying why cannot dodge it
    limit = int(store.meta("held_limit"))
    with store.write() as c:
        run(c)
    for n in range(1, limit + 1):
        why = ("spawn", "no_route")[n % 2]
        with store.write() as c:
            assert plain_intent(c) == n
            out = J.close_not_started_in(c, "r", "link-0", n, why=why,
                                         receipt=receipt(f"ns{n}", subj(n), "refused"),
                                         run_receipt=lambda reason: receipt(f"run{n}", "run:r", "refused"))
        assert out.held == n
    assert out.run_closed is not None and out.run_closed.terminal is Terminal.WON
    with store.read() as c:
        assert s.invariant(c) == []


def test_g6_a_seal_of_a_link_whose_hold_is_undecided_is_refused(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        with pytest.raises(ValueError, match="undecided"):
            J.effect_seal_in(c, "r", "link-0", by="human", posture="CONFIRM")


@pytest.mark.parametrize("terms", ["[1]", '"text"', "null", "3"])
def test_g7_grant_terms_that_are_json_but_not_an_object_are_a_clear_error(store, terms):
    with store.write() as c:
        run(c)
        with pytest.raises(ValueError, match="terms_text"):
            J.effect_seal_in(c, "r", "link-0", by="binding", posture="ON_LOOP", grant={**GRANT, "terms_text": terms},
                             cover=COVER)


@pytest.mark.parametrize("run_id, effect_id", [("a:b", "link-0"), ("r", "link#0")])
def test_g8_intent_in_refuses_an_id_no_effect_subject_can_name_before_any_read(store, run_id, effect_id):
    reads = []
    with store.write() as c:
        c.set_trace_callback(reads.append)
        with pytest.raises(ValueError, match="split ambiguously"):
            J.intent_in(c, run_id, effect_id, digest="d", by="on-loop", posture="ON_LOOP")
        c.set_trace_callback(None)
        assert reads == []


def test_g1_an_attempt_of_a_link_whose_hold_was_denied_is_refused_and_nothing_is_written(store):
    with store.write() as c:
        run(c)
        open_hold(c)
        assert J.decide_in(c, H, approve=False, digest="d", by="human",
                           receipt=receipt("rj", "run:r", "refused")).ok
        assert J.intent_in(c, "r", "link-0", digest="d", by="human", posture="CONFIRM") == "hold_denied"
        assert rows(c) == []


def test_g2_a_retry_closed_after_its_run_was_cancelled_keeps_the_count_as_it_stood(store):
    two_attempts(store)                     # attempt 1 counted (held 1); attempt 2 in flight
    with store.write() as c:
        J.close_run_in(c, "r", reason="cancelled", receipt=receipt("cx", "run:r", "killed"))
    with store.write() as c:
        out = not_started(c, 2)
        assert (out.closed, out.held, out.run_closed) == (Closed(Terminal.WON, "ns2"), 1, None)
        assert c.execute("SELECT n, attempt FROM held_counts").fetchall() == [(1, 1)]
    with store.read() as c:
        assert rows(c) == [(1, "not_started"), (2, "not_started")]
        assert s.invariant(c) == []
