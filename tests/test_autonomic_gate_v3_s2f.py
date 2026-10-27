"""R15-5 S2, second slice (levain project_memory 1027-18/r155s2f/BRIEF.md items f and e): the gate on
``levain-autonomic/3`` receipts. Every receipt of a /3 subject is built at admission through the store's builder
(``gate_record`` with its subject) and written as that subject's close; the gate writes no file receipt for it."""
from __future__ import annotations

from levain.autonomic import Posture
from levain.autonomic.store import gate_record


# ---- f: a fresh id per record ---------------------------------------------------------------------------------------
def test_f_two_ends_of_one_subject_at_one_instant_get_two_ids():
    # the outcome_unknown and the refused receipt of one attempt are built together at admission, at one created_at,
    # with one gate verdict: the id basis must tell them apart
    def rid(terminal_state: str) -> str:
        face = {"context_id": "c", "gate": {"verdict": "auto", "by": "binding"}, "terminal_state": terminal_state}
        return gate_record(created_at="2026-10-27T12:00:00+00:00", action_name="link0", proposal_id=None,
                           posture=Posture.ON_LOOP, fired=False, action_face=face, subject="effect:r1:link-0")[0]
    assert rid("outcome_unknown") != rid("refused")


def test_f_a_receipt_without_a_subject_keeps_its_id():
    # the /2 file receipt's id basis is unchanged (the terminal_state joins the basis only with a subject)
    face = {"context_id": "c", "gate": {"verdict": "auto", "by": "on-loop"}, "terminal_state": "refused"}
    rid, _ = gate_record(created_at="2026-10-27T12:00:00+00:00", action_name="link0", proposal_id=None,
                         posture=Posture.ON_LOOP, fired=False, action_face=face)
    assert rid == "2026-10-27T12:00:00+00:00-" + __import__("hashlib").sha256(
        "2026-10-27T12:00:00+00:00\x00c\x00link0\x00auto\x00on-loop\x000".encode()).hexdigest()[:8]


# ---- f: the gate over a /3 store, end to end --------------------------------------------------------------------------
import datetime as _dt  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from levain.autonomic import (ActionManifest, ActionRequest, Binding, BindingStatus, BindingStore,  # noqa: E402
                              ChainExecutor, ConfirmDecision, EfferentGate, FireDispatcher, GateReceiptStore, Guard,
                              PendingActionStore, RunJournal, RunRef, SubGoal, TriggerSpec)
from levain.autonomic import journal as J  # noqa: E402
from levain.autonomic import schema3 as s  # noqa: E402
from tests.autonomic_confirm_keys import confirm_signers, signed_yes  # noqa: E402
from tests.autonomic_test_only_coverage import EVERY_EFFECT_GUARD  # noqa: E402
from tests.test_autonomic_journal_wiring import (FIXED, HIGH, LOW, TIGHT, OutboxExecutor, World,  # noqa: E402
                                                 _chain_builder, _single_builder, _Transport, _trust)

pytestmark = pytest.mark.usefixtures("test_only_confined_executor", "test_only_every_effect_covered")

KILL = Guard(rationale="spoofed sender", dissent_author="codex",
             kill_predicate={"op": "==", "field": "dmarc", "value": "fail"}, kill_drill={"dmarc": "fail"},
             kill_authored_by="operator")


class V3World(World):
    """:class:`World`'s whole fire path (dispatcher, chain executor, gate, journal, binding registry) over one
    ``levain-autonomic/3`` store."""

    def __init__(self, work) -> None:   # noqa: D107 — World's, with the /3 store
        work.mkdir(parents=True, exist_ok=True)
        self.work = work
        self.journal = RunJournal(s.AutonomicStore3(work / "store"))
        self.store = BindingStore(work / "store", journal=self.journal)
        self.pending = PendingActionStore(work / "pending.json")
        self.receipts = GateReceiptStore(work / "receipts.jsonl")
        self.tool_risk = {0: LOW, 1: HIGH}
        self.gate = EfferentGate(confirm_signers=confirm_signers(),
                                 manifest=ActionManifest({"link0": LOW, "link1": HIGH}), store=self.receipts,
                                 executor=OutboxExecutor(work / "outbox.jsonl"), clock=lambda: FIXED,
                                 transport=_Transport(), pending_store=self.pending, journal=self.journal,
                                 binding_risk=lambda b, i: self.tool_risk[i])
        self.chains = ChainExecutor(gate=self.gate, request_builder=_chain_builder, trust_resolver=_trust,
                                    clock=lambda: FIXED, binding_store=self.store)
        self.dispatcher = FireDispatcher(
            store=self.store, gate=self.gate,
            predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
            request_builder=_single_builder, clock=lambda: FIXED, chain_executor=self.chains)

    def dispatch(self, event_id: str, dmarc: str = "pass"):
        [d] = self.dispatcher.dispatch({"type": "email", "id": event_id,
                                        "fields": {"from": "a@x.example", "dmarc": dmarc}})
        return d

    def q(self, sql: str, *args):
        with self.journal.db.read() as c:
            return c.execute(sql, args).fetchall()

    def receipt(self, receipt_id: str) -> dict:
        [(rec,)] = self.q("SELECT record FROM gate_receipts WHERE receipt_id = ?", receipt_id)
        return json.loads(rec)

    def only_run(self) -> str:
        [(run_id,)] = self.q("SELECT run_id FROM runs")
        return run_id


def _gate_outcomes(d):
    # a dispatch's outcome per link (a chain's links, or the single binding fire)
    return list(getattr(d, "outcomes", None) or [d.outcome])


def test_f_run_one_v3_effect_fires_end_to_end_with_the_journals_receipt(tmp_path):
    # RUN (slice 1 left it UNDISCHARGED): a binding's event, dispatched through the real FireDispatcher, gate, run
    # journal and AutonomicStore3: the effect runs once, ends done with its fired receipt in the store, and the gate
    # writes nothing to the receipt file
    w = V3World(tmp_path)
    w.mint(chain=False)
    d = w.dispatch("e1")
    [out] = _gate_outcomes(d)
    assert out.fired and out.receipt_id, out
    assert len(w.outbox()) == 1
    run_id = w.only_run()
    [(state, rid, seal)] = w.q("SELECT state, receipt_id, seal FROM effects WHERE run_id = ?", run_id)
    assert state == "done" and rid == out.receipt_id
    rec = w.receipt(rid)
    assert rec["subject"] == f"effect:{run_id}:link-0" and rec["fired"] is True
    assert rec["action_face"]["terminal_state"] == "fired"
    assert rec["action_face"]["guard"] == [KILL.to_dict()]
    assert rec["action_face"]["grant_guard"] == [dict(g) for g in EVERY_EFFECT_GUARD]
    seal = json.loads(seal)
    assert seal["by"] == "binding" and seal["posture"] == "ON_LOOP" and seal["grant"]["id"] == "gp-test-only-every"
    assert w.receipts.read() == []
    # a re-delivery replays: nothing runs again, no receipt is written again
    [again] = _gate_outcomes(w.dispatch("e1"))
    assert again.replayed and again.receipt_id == rid
    assert len(w.outbox()) == 1 and w.receipts.read() == []


def _cooling_binding(w: V3World) -> Binding:
    b = Binding.create(created_by="operator", created_at="2026-10-27T09:00:00co",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
                       posture=Posture.COOLING_OFF, guard=(KILL,), status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    return b


def _run_close(w: V3World, run_id: str):
    return w.q("SELECT reason, receipt_id FROM cancels WHERE run_id = ?", run_id)


def test_f_a_signed_approval_fires_its_held_link_with_the_journals_receipt(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    out = w.resolve_open(True)
    run_id = w.only_run()
    [(state, rid, seal)] = w.q("SELECT state, receipt_id, seal FROM effects WHERE run_id = ? AND effect_id = "
                               "'link-1'", run_id)
    assert state == "done" and json.loads(seal)["by"] == "human"
    rec = w.receipt(rid)
    assert rec["fired"] is True and rec["action_face"]["gate"]["verdict"] == "approved"
    assert rec["action_face"]["guard"] == [KILL.to_dict()] and rec["action_face"]["grant_guard"] is None
    assert w.receipts.read() == [], out


def test_f_a_rejection_closes_the_run_with_its_own_receipt_and_writes_no_file_receipt(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    out = w.resolve_open(False).links[-1].outcome
    run_id = w.only_run()
    [(reason, rid)] = _run_close(w, run_id)
    assert out.refused and out.receipt_id == rid and reason == "rejected"
    rec = w.receipt(rid)
    assert rec["subject"] == f"run:{run_id}" and rec["fired"] is False
    assert rec["action_face"]["terminal_state"] == "refused" and rec["action_face"]["gate"]["by"] == "human"
    assert rec["action_face"]["guard"] == [KILL.to_dict()] and rec["action_face"]["grant_guard"] is None
    assert w.receipts.read() == [] and len(w.outbox()) == 1   # link-0 ran before the hold


def test_f_a_kill_closes_the_run_with_its_killed_receipt(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=False)
    [out] = _gate_outcomes(w.dispatch("k1", dmarc="fail"))
    run_id = w.only_run()
    [(reason, rid)] = _run_close(w, run_id)
    assert out.killed and out.receipt_id == rid and reason.startswith("killed:")
    rec = w.receipt(rid)
    assert rec["action_face"]["terminal_state"] == "killed" and rec["action_face"]["refuse_class"] == "kill_triggered"
    assert rec["action_face"]["grant"] is None if "grant" in rec["action_face"] else True
    assert w.receipts.read() == [] and w.outbox() == [] and w.q("SELECT * FROM effects") == []


def test_f_a_gate_deny_of_a_run_closes_it_with_the_denys_receipt(tmp_path):
    w = V3World(tmp_path)
    b = w.mint(chain=False)
    w.store.admit(b.binding_id, "r1")
    from levain.autonomic.binding import binding_invocation
    req = ActionRequest(action_name="undeclared", payload="p", context_id="c", query_text="q",
                        query_date="2026-10-27", trust=_trust(None, 0), grounded=True,
                        authority=binding_invocation(w.store.get(b.binding_id)), run=RunRef("r1", "link-0"))
    out = w.gate.gate(req)
    [(reason, rid)] = _run_close(w, "r1")
    assert out.refused and out.reason == "unknown_action" and out.receipt_id == rid
    assert reason == "denied:unknown_action" and w.receipt(rid)["subject"] == "run:r1"
    assert w.receipts.read() == []


def test_f_a_changed_replay_cancels_the_run_with_a_receipt(tmp_path):
    # peek's replay_digest_changed cancel on /3 carries the gate's run receipt (without one, _cancel_in raises). A chain
    # whose link-0 is done and link-1 held: the run is not complete, so the cancel closes it
    w = V3World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("e1")
    run_id = w.only_run()
    from levain.autonomic.binding import binding_invocation
    req = ActionRequest(action_name="link0", payload="OTHER BYTES", context_id="c", query_text="q",
                        query_date="2026-10-27", trust=_trust(None, 0), grounded=True,
                        authority=binding_invocation(w.store.get(b.binding_id)), run=RunRef(run_id, "link-0"))
    out = w.gate.gate(req)
    [(reason, rid)] = _run_close(w, run_id)
    assert out.refused and reason == "replay_digest_changed" and w.receipt(rid)["subject"] == f"run:{run_id}"


# ---- O2: a silence fire at a confirm rung with no sealed grant is a refusal on /3 -----------------------------------
def test_o2_a_silence_approval_at_a_confirm_rung_is_refused_with_its_receipt_and_never_fires(tmp_path):
    w = V3World(tmp_path)
    _cooling_binding(w)
    w.gate._auto_fire_actions = frozenset({"link0"})
    w.dispatch("co1")
    [p] = w.gate.open_pendings()
    w.gate._clock = lambda: FIXED + _dt.timedelta(days=1)      # past the silence window
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    run_id = w.only_run()
    [(reason, rid)] = _run_close(w, run_id)
    assert out.refused and not out.fired and out.reason == "unattended_confirm_without_grant", out
    assert out.receipt_id == rid and reason == "rejected"
    rec = w.receipt(rid)
    assert rec["subject"] == f"run:{run_id}" and rec["action_face"]["gate"]["by"] == "on-loop"
    assert w.outbox() == [] and w.q("SELECT * FROM effects") == [] and w.receipts.read() == []


def test_o2_a_standing_silence_approval_at_a_confirm_rung_closes_the_run_unstarted(tmp_path):
    w = V3World(tmp_path)
    _cooling_binding(w)
    w.gate._auto_fire_actions = frozenset({"link0"})
    w.dispatch("co1")
    [p] = w.gate.open_pendings()
    hold = w.journal.find_pending(p.pending_id)
    assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                            posture="COOLING_OFF").ok
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    [(reason, rid)] = _run_close(w, w.only_run())
    assert out.refused and out.receipt_id == rid and reason == "unattended_confirm_without_grant", out
    assert w.outbox() == [] and w.q("SELECT * FROM effects") == []


# ---- O3: a deny after a fence closes nothing in the /3 store ---------------------------------------------------------
def test_o3_a_deny_after_a_fence_closes_nothing_and_persists_file_only(tmp_path):
    w = V3World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("c1")
    pending_id = w.open_pending_id()
    decision = signed_yes(w.gate, pending_id)
    w.journal.fence(b.binding_id)
    out = w.resolve_open_as(decision).links[-1].outcome
    run_id = w.only_run()
    assert out.refused and out.reason == "journal:fenced", out
    assert _run_close(w, run_id) == []                                  # a fence is not a close
    [rec] = w.receipts.read()
    assert rec.id == out.receipt_id and rec.action_face["terminal_state"] == "refused"
    assert w.q("SELECT COUNT(*) FROM gate_receipts WHERE subject LIKE 'run:%'") == [(0,)]


def test_f_a_silence_approval_whose_rung_rose_closes_the_run_unstarted_with_its_receipt(tmp_path):
    w = V3World(tmp_path)
    _cooling_binding(w)
    w.gate._auto_fire_actions = frozenset({"link0"})
    w.dispatch("co1")
    [p] = w.gate.open_pendings()
    hold = w.journal.find_pending(p.pending_id)
    assert w.journal.decide(hold["hold_id"], approve=True, digest=hold["digest"], by=J.UNSIGNED_DECIDER,
                            posture="COOLING_OFF").ok
    w.tool_risk[0] = HIGH                                          # the rung needed now is above the one approved
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by=J.UNSIGNED_DECIDER))
    [(reason, rid)] = _run_close(w, w.only_run())
    assert out.refused and out.receipt_id == rid and reason.startswith("revalidate:"), out
    assert w.receipt(rid)["action_face"]["gate"]["by"] == "on-loop"
    assert w.outbox() == [] and w.receipts.read() == []


def test_f_an_integrity_reject_closes_the_run_with_its_receipt(tmp_path):
    w = V3World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    hold = w.journal.find_pending(w.open_pending_id())
    tampered = {**hold, "pending": {**hold["pending"], "payload": "tampered"}}
    out = w.gate._resolve_hold(tampered, ConfirmDecision(approved=True, by="human"), chain_owned=True)
    [(reason, rid)] = _run_close(w, w.only_run())
    assert out.refused and out.reason == "integrity:seal_mismatch" and out.receipt_id == rid and reason == "rejected"
    assert w.receipts.read() == []


# ---- e: the grant is revalidated inside the admission's transaction ---------------------------------------------------
from tests.autonomic_test_only_coverage import TestOnlyCoverage, policy as grant_policy  # noqa: E402

_LATER = FIXED + _dt.timedelta(days=10)


class _FenceBlindCoverage(TestOnlyCoverage):
    """A source whose cover VALUE (what the risk fence digests at admission, ``peek``) stays the granted policy's
    after a revoke, while ``peek_cover`` (the policy) moves: the interleaving only the grant's own revalidation sees."""

    __test__ = False

    def __init__(self, p) -> None:
        super().__init__([p])
        self._pinned = p.cover_value

    def peek(self, effect, now):
        return self._pinned


def _granted_world(tmp_path, src) -> V3World:
    w = V3World(tmp_path)
    w.gate = EfferentGate(confirm_signers=confirm_signers(), manifest=w.gate._manifest, store=w.receipts,
                          executor=w.gate._executor, clock=lambda: FIXED, transport=_Transport(),
                          pending_store=w.pending, journal=w.journal, binding_risk=lambda b, i: w.tool_risk[i],
                          coverage=src)
    w.chains = ChainExecutor(gate=w.gate, request_builder=_chain_builder, trust_resolver=_trust,
                             clock=lambda: FIXED, binding_store=w.store)
    w.dispatcher = FireDispatcher(
        store=w.store, gate=w.gate, predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
        request_builder=_single_builder, clock=lambda: FIXED, chain_executor=w.chains)
    return w


def _revoke_after_the_verify(w: V3World, src: TestOnlyCoverage, gid: str) -> None:
    # the journal's _authorized (the check outside the write transaction) runs, then the revoke lands, then the
    # admission's transaction
    real = w.journal._authorized

    def authorized(*a, **kw):
        got = real(*a, **kw)
        src.revoke(gid)
        return got
    w.journal._authorized = authorized


def test_e_a_revoke_between_the_verify_and_the_transaction_never_fires(tmp_path):
    src = TestOnlyCoverage([grant_policy("g1", {"link0": LOW}, expires_at=_LATER)])
    w = _granted_world(tmp_path, src)
    w.mint(chain=False)
    _revoke_after_the_verify(w, src, "g1")
    [out] = _gate_outcomes(w.dispatch("e1"))
    assert not out.fired and out.held, out
    assert w.outbox() == [] and w.q("SELECT * FROM effects") == []


def test_e_a_revoke_the_fence_cannot_see_is_refused_by_the_revalidation(tmp_path):
    src = _FenceBlindCoverage(grant_policy("g1", {"link0": LOW}, expires_at=_LATER))
    w = _granted_world(tmp_path, src)
    w.mint(chain=False)
    _revoke_after_the_verify(w, src, "g1")
    [out] = _gate_outcomes(w.dispatch("e1"))
    assert not out.fired and out.reason == "journal:stale:grant_revoked", out
    assert w.outbox() == [] and w.q("SELECT * FROM effects") == []


def test_e_an_unrevoked_grant_fires_and_is_sealed(tmp_path):
    src = TestOnlyCoverage([grant_policy("g1", {"link0": LOW}, expires_at=_LATER)])
    w = _granted_world(tmp_path, src)
    w.mint(chain=False)
    [out] = _gate_outcomes(w.dispatch("e1"))
    assert out.fired, out
    [(seal,)] = w.q("SELECT seal FROM effects")
    assert json.loads(seal)["grant"]["id"] == "g1"
