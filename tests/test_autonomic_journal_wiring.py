"""The run journal WIRED into the real fire path: FireDispatcher -> ChainExecutor -> EfferentGate ->
RunJournal, over real stores on disk. Every test here reproduces a run made by hand first (the fold's
wiring slice, 2026-10-07); the effect is an append to an outbox file, and the outbox is the oracle for
"did it happen". The crash test kills a real child process right after its effect.

What these runs show, per property:
  - dedup-on-replay: a re-delivered event replays, never re-sends, and records no second evidence;
  - unknown outcome: a process that dies after its effect leaves it poisoned; the restart never re-sends;
  - hold-until-decided: a sibling run's undecided effect waits while the binding has an open decision;
  - reject-cancels: a rejected link cancels its run for every later delivery;
  - fence-on-cancel: a pause fences an admitted run even if the grant is re-activated before it resumes.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from levain.autonomic import (
    ActionManifest, ActionRequest, ActionRisk, AuthorityScope, Binding, BindingStatus, BindingStore,
    ChainExecutor, ChainStateStore, ConfirmDecision, EfferentGate, ExecutionResult, FireDispatcher,
    GateReceiptStore, Guard, IntentProvenance, PendingActionStore, Posture, RiskClass, RunJournal, RunRef,
    SignalAuth, SubGoal, TightnessVector, TriggerSpec, TrustContext, hold_id_for, manual_invocation,
    run_id_for,
)

FIXED = _dt.datetime(2026, 10, 7, 12, 0, 0, tzinfo=_dt.timezone.utc)
LOW = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
HIGH = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)
TIGHT = TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9)


class OutboxExecutor:
    """The effect: append one line to the outbox file. ``CRASH_AFTER_EFFECT=<action>`` in the
    environment makes the process die right after that action's write (a real crash, mid-run)."""

    name = "outbox"

    def __init__(self, outbox: Path) -> None:
        self.outbox = outbox

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult:
        with open(self.outbox, "a") as f:
            f.write(json.dumps({"action": action_name, "ctx": context_id}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if os.environ.get("CRASH_AFTER_EFFECT") == action_name:
            os._exit(9)
        return ExecutionResult(ok=True, detail="sent", downstream_id=f"out:{action_name}:{context_id}")


class _Transport:
    name = "t"

    def propose(self, proposal) -> bool:
        return True


def _trust(binding, i):
    return TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                        hops=i, human_present=False)


def _chain_builder(binding, ctx, i):
    up = ctx.completed[-1].downstream_id if ctx.completed else "NONE"
    return ActionRequest(action_name=f"link{i}", payload=f"link{i} from={up}",
                         context_id=f"{ctx.trigger_event['id']}-{i}", query_text="q", query_date="2026-10-07",
                         trust=_trust(binding, i), grounded=True, authority=manual_invocation(),
                         overall_confidence=1.0, directive_confidence=1.0)


def _single_builder(binding, event):
    return ActionRequest(action_name="link0", payload="link0 from=NONE", context_id=f"{event['id']}-0",
                         query_text="q", query_date="2026-10-07", trust=_trust(binding, 0), grounded=True,
                         authority=manual_invocation(), overall_confidence=1.0, directive_confidence=1.0)


class World:
    """The whole fire path over one working directory. A fresh ``World`` on the same directory is a
    restarted process."""

    def __init__(self, work: Path) -> None:
        work.mkdir(parents=True, exist_ok=True)
        self.work = work
        self.journal = RunJournal(work / "store")
        self.store = BindingStore(work / "store", journal=self.journal)
        self.pending = PendingActionStore(work / "pending.json")
        self.receipts = GateReceiptStore(work / "receipts.jsonl")
        self.gate = EfferentGate(
            manifest=ActionManifest({"link0": LOW, "link1": HIGH}), store=self.receipts,
            executor=OutboxExecutor(work / "outbox.jsonl"), clock=lambda: FIXED,
            transport=_Transport(), pending_store=self.pending, journal=self.journal,
        )
        self.chains = ChainExecutor(gate=self.gate, request_builder=_chain_builder,
                                    risk_resolver=lambda b, i: [LOW, HIGH][i], trust_resolver=_trust,
                                    chain_store=ChainStateStore(work / "chains.json"), clock=lambda: FIXED,
                                    binding_store=self.store)
        self.dispatcher = FireDispatcher(
            store=self.store, gate=self.gate,
            predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
            request_builder=_single_builder, risk_resolver=lambda b: LOW, clock=lambda: FIXED,
            chain_executor=self.chains)

    def mint(self, *, chain: bool) -> Binding:
        goal = ((SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),
                 SubGoal(goal="email it", tools=("mail.send",), output="email:self")) if chain
                else (SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),))
        b = Binding.create(
            created_by="operator", created_at="2026-10-07T09:00:00" + ("c" if chain else "s"),
            trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
            goal=goal, tightness=TIGHT, posture=Posture.CONFIRM if chain else Posture.ON_LOOP,
            guard=(Guard(rationale="spoofed sender", dissent_author="codex",
                         kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                         kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
            status=BindingStatus.PAUSED)
        self.store.add(b)
        self.store.ratify(b.binding_id)
        return b

    def dispatch(self, event_id: str):
        [d] = self.dispatcher.dispatch({"type": "email", "id": event_id,
                                        "fields": {"from": "a@x.example", "dmarc": "pass"}})
        return d

    def resolve_open(self, approve: bool):
        return self.resolve_open_as(ConfirmDecision(approved=approve, by="human"))

    def resolve_open_as(self, decision):
        [p] = self.gate.open_pendings()
        return self.chains.resume(p.pending_id, decision)

    def outbox(self) -> list[tuple[str, str]]:
        p = self.work / "outbox.jsonl"
        return [(r["action"], r["ctx"]) for r in map(json.loads, p.read_text().splitlines())] if p.exists() else []

    def fire_count(self, b: Binding) -> int:
        got = self.store.get(b.binding_id)
        assert got is not None
        return got.graduation.fire_count


def _child(work: Path, body: str, crash_after: str) -> int:
    """Run ``body`` in a fresh interpreter against ``World(work)``; the executor dies after
    ``crash_after``'s effect. Returns the exit code."""
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from pathlib import Path
        from test_autonomic_journal_wiring import World
        w = World(Path({str(work)!r}))
    """) + textwrap.dedent(body)
    env = dict(os.environ, CRASH_AFTER_EFFECT=crash_after)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True).returncode


# --- dedup-on-replay ------------------------------------------------------------------------

def test_a_redelivered_event_replays_and_records_no_second_fire(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)
    first = w.dispatch("e1")
    assert first.outcome.fired and not first.outcome.replayed
    again = World(tmp_path).dispatch("e1")                      # a restarted process, same event
    assert again.outcome.fired and again.outcome.replayed
    assert again.outcome.execution.downstream_id == "out:link0:e1-0"   # the recorded result
    assert w.outbox() == [("link0", "e1-0")]                     # sent once
    assert w.fire_count(b) == 1                                  # evidence counted once
    assert len(list(w.receipts.read())) == 1


def test_a_completed_chain_redelivered_replays_every_link(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("c1")
    assert w.resolve_open(approve=True).completed
    again = w.dispatch("c1")
    assert again.chain.completed and all(l.outcome.replayed for l in again.chain.links)
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]
    assert w.fire_count(b) == 1


def test_a_receipt_that_never_landed_is_written_on_replay(tmp_path):
    w = World(tmp_path)
    w.mint(chain=False)
    w.dispatch("e1")
    with w.journal.db.write() as conn:                              # as if it stopped before the note
        conn.execute("UPDATE effects SET receipt_id = NULL")
    (tmp_path / "receipts.jsonl").unlink()
    again = World(tmp_path).dispatch("e1")
    assert again.outcome.replayed and again.outcome.receipt_id is not None
    assert len(list(w.receipts.read())) == 1 and w.outbox() == [("link0", "e1-0")]
    assert World(tmp_path).dispatch("e1").outcome.receipt_id == again.outcome.receipt_id   # noted, not rewritten


# --- the unknown outcome: a real crash after the effect ----------------------------------------

def test_a_crash_after_the_effect_poisons_it_and_the_restart_never_resends(tmp_path):
    w = World(tmp_path)
    w.mint(chain=False)
    assert _child(tmp_path, "w.dispatch('e2')", crash_after="link0") == 9
    assert w.outbox() == [("link0", "e2-0")]                       # the world changed
    out = World(tmp_path).dispatch("e2").outcome
    assert out.refused and out.reason == "journal:poisoned" and not out.fired
    assert w.outbox() == [("link0", "e2-0")]                       # and nothing sent it again
    assert w.journal.poisoned() == [(run_id_for(w.store.list_all()[0].binding_id,
                                                {"type": "email", "id": "e2",
                                                 "fields": {"from": "a@x.example", "dmarc": "pass"}}),
                                     "link-0")]


def test_a_crash_after_an_approved_effect_poisons_it(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("f1")
    assert _child(tmp_path, "w.resolve_open(True)", crash_after="link1") == 9
    out = World(tmp_path).dispatch("f1")
    assert out.chain.aborted and out.chain.reason == "journal:poisoned"
    assert w.outbox() == [("link0", "f1-0"), ("link1", "f1-1")]


def test_an_approval_whose_effect_never_ran_resumes_on_redelivery(tmp_path):
    # the process stopped between the decision and the effect: the pending was claimed and the hold
    # approved, nothing ran. Re-delivery runs the approved effect once instead of asking again.
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("h1")
    [p] = w.gate.open_pendings()
    from levain.autonomic import effect_digest
    assert w.journal.decide(w.journal.find_pending(p.pending_id)["hold_id"], approve=True, by="human",
                            digest=effect_digest(action_name=p.action_name, payload=p.payload,
                                                 context_id=p.context_id)).ok
    out = World(tmp_path).dispatch("h1")
    assert out.chain.completed and out.chain.links[-1].outcome.fired
    assert w.outbox() == [("link0", "h1-0"), ("link1", "h1-1")]
    assert w.gate.open_pendings() == [] and w.fire_count(b) == 1


# --- hold-until-decided, reject-cancels --------------------------------------------------------

def test_a_sibling_run_waits_while_the_binding_has_an_open_decision(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    assert w.dispatch("c1").chain.paused                          # link0 ran, link1 is the open decision
    sibling = w.dispatch("c2")
    assert sibling.chain.held and sibling.outcome.held and sibling.outcome.hold_id is not None
    assert w.outbox() == [("link0", "c1-0")]                      # the sibling leak is closed
    assert w.resolve_open(approve=True).completed
    assert w.dispatch("c2").chain.paused                          # re-delivered after the decision
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1"), ("link0", "c2-0")]


def test_a_rejected_link_cancels_its_run_for_every_later_delivery(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c2")
    assert w.resolve_open(approve=False).aborted
    again = w.dispatch("c2")
    assert again.chain.aborted and again.outcome.reason == "journal:cancelled"
    assert w.outbox() == [("link0", "c2-0")]
    assert w.journal.open_holds() == []


# --- fence-on-cancel ---------------------------------------------------------------------------

def test_a_pause_fences_an_admitted_run_even_after_the_grant_is_reactivated(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("g1")
    w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.store.ratify(b.binding_id)                                  # live again before the old run resumes
    out = w.resolve_open(approve=True)
    assert out.aborted and out.reason == "journal:fenced"
    assert w.outbox() == [("link0", "g1-0")]
    assert w.dispatch("g2").chain.paused                          # a new run is admitted under the new generation


@pytest.mark.parametrize("verb", ["revoke", "tighten", "replace", "remove"])
def test_every_verb_that_stops_or_tightens_a_grant_fences_it(tmp_path, verb):
    w = World(tmp_path)
    b = w.mint(chain=False)
    before = w.store.generation(b.binding_id)
    if verb == "revoke":
        w.store.set_status(b.binding_id, BindingStatus.REVOKED)
    elif verb == "tighten":
        w.store.tighten_guard(b.binding_id, Guard(rationale="more", dissent_author="codex",
                                                  kill_predicate={"op": "==", "field": "x", "value": 1},
                                                  kill_drill={"x": 1}, kill_authored_by="operator"))
    elif verb == "replace":
        assert w.store.replace_atomic(w.store.get(b.binding_id), Binding.create(
            created_by="operator", created_at="2026-10-07T10:00:00", trigger=b.trigger, goal=b.goal,
            tightness=b.tightness, posture=Posture.ABOVE_LOOP, guard=b.guard, status=BindingStatus.ACTIVE))
    else:
        w.store.remove(b.binding_id)
    assert w.journal.generation(b.binding_id) == before + 1          # mirrored into the journal
    if verb != "remove":
        assert w.store.generation(b.binding_id) == before + 1        # bumped in the registry record


def test_activating_verbs_do_not_fence(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)                                       # add + ratify
    before = w.store.generation(b.binding_id)
    w.store.record_fire(b.binding_id, clean=True, fired_at="2026-10-07T12:00:00")
    assert w.store.generation(b.binding_id) == before and w.journal.generation(b.binding_id) == 0


# --- the wiring is structural -------------------------------------------------------------------

def _binding_request(run: RunRef | None):
    return ActionRequest(
        action_name="link0", payload="p", context_id="c", query_text="q", query_date="2026-10-07",
        trust=_trust(None, 0), grounded=True,
        authority=AuthorityScope(grantor="binding", grant="binding:email@on_loop", binding_id="bind-x", hops=0),
        overall_confidence=1.0, directive_confidence=1.0, risk=LOW, run=run)


def test_a_binding_fire_without_a_run_is_refused_once_a_journal_is_wired(tmp_path):
    w = World(tmp_path)
    out = w.gate.gate(_binding_request(None))
    assert out.refused and out.reason == "unjournaled_binding_fire" and w.outbox() == []


def test_a_run_without_a_journal_is_refused(tmp_path):
    gate = EfferentGate(manifest=ActionManifest({}), store=GateReceiptStore(tmp_path / "r.jsonl"),
                        executor=OutboxExecutor(tmp_path / "outbox.jsonl"), clock=lambda: FIXED)
    out = gate.gate(_binding_request(RunRef("run-x", "link-0")))
    assert out.refused and out.reason == "run_without_journal"
    assert not (tmp_path / "outbox.jsonl").exists()


def test_a_run_that_was_never_admitted_is_refused(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)
    req = dataclasses.replace(_binding_request(RunRef("run-never", "link-0")),
                              authority=AuthorityScope(grantor="binding", grant="g", binding_id=b.binding_id, hops=0))
    out = w.gate.gate(req)
    assert out.refused and out.reason == "run_not_admitted" and w.outbox() == []


def test_the_dispatcher_refuses_a_store_and_gate_on_different_journals(tmp_path):
    w = World(tmp_path)
    with pytest.raises(ValueError, match="same|SAME|must be"):
        FireDispatcher(store=BindingStore(tmp_path / "other.json"), gate=w.gate,
                       predicate_match=lambda p, e: True, request_builder=_single_builder,
                       risk_resolver=lambda b: LOW, clock=lambda: FIXED)


def test_a_killed_effect_cancels_its_run(tmp_path):
    # a terminal no-fire decision ends the run, so a re-delivery cannot reach a different decision
    w = World(tmp_path)
    w.mint(chain=False)
    event = {"type": "email", "id": "k1", "fields": {"from": "a@x.example", "dmarc": "fail"}}
    [first] = w.dispatcher.dispatch(event)
    assert first.outcome.killed
    [again] = w.dispatcher.dispatch(event)
    assert again.outcome.refused and again.outcome.reason == "journal:cancelled" and w.outbox() == []


def test_an_event_that_is_not_canonical_json_does_not_fire(tmp_path):
    w = World(tmp_path)
    w.mint(chain=False)
    out = w.dispatcher.dispatch({"type": "email", "id": "n1", "x": float("nan"),
                                 "fields": {"from": "a@x.example", "dmarc": "pass"}})
    assert out == [] and w.outbox() == []


def test_an_executor_that_raises_is_poisoned_not_retried(tmp_path):
    class Raising(OutboxExecutor):
        def execute(self, action_name, payload, *, context_id):
            super().execute(action_name, payload, context_id=context_id)
            raise TimeoutError("the server may have accepted it")

    w = World(tmp_path)
    w.mint(chain=False)
    w.gate._executor = Raising(tmp_path / "outbox.jsonl")
    first = w.dispatch("r1").outcome
    assert not first.fired and "outcome_unknown" in first.reason
    assert World(tmp_path).dispatch("r1").outcome.reason == "journal:poisoned"
    assert w.outbox() == [("link0", "r1-0")]


def test_a_pause_landing_right_after_admission_stops_the_run(tmp_path):
    # admission (check + start + claim) is one step under the store lock; a pause right after it
    # bumps the generation past the run's, so the run's first effect is fenced
    w = World(tmp_path)
    b = w.mint(chain=False)
    original = w.store.admit

    def admit_then_pause(binding_id, run_id):
        fresh = original(binding_id, run_id)
        w.store.set_status(binding_id, BindingStatus.PAUSED)       # the operator stops it right here
        return fresh

    w.store.admit = admit_then_pause
    out = w.dispatch("p1").outcome
    assert out.refused and out.reason == "journal:fenced" and w.outbox() == []


def test_a_dispatch_inside_a_pause_waits_for_it_and_does_not_fire(tmp_path):
    # L2 P1, reproduced at f9f43c5: a dispatch whose admission ran between a pause's fence and its
    # registry write fired after the pause returned. Here the dispatch starts from INSIDE the pause's
    # transaction, after its fence: it must wait for the commit and then see the binding paused.
    import threading
    import time
    w = World(tmp_path)
    b = w.mint(chain=False)
    real_fence_in = w.journal._fence_in
    done: list[bool] = []
    threads: list[threading.Thread] = []

    def fence_then_dispatch(conn, binding_id, generation):
        real_fence_in(conn, binding_id, generation)
        t = threading.Thread(target=lambda: done.append(bool(w.dispatcher.dispatch(
            {"type": "email", "id": "e", "fields": {"from": "a@x.example", "dmarc": "pass"}}))))
        t.start()
        time.sleep(0.3)                                   # the dispatch had every chance to run here
        threads.append(t)

    w.journal._fence_in = fence_then_dispatch
    assert w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    threads[0].join()
    assert w.outbox() == [] and done == [False]


def test_a_pause_and_its_fence_commit_together_or_not_at_all(tmp_path):
    # L2 P3, reproduced at f9f43c5: the fence write failed once, the pause committed, and the admitted
    # run fired. A pause and its fence are one transaction now: a failure leaves neither.
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("q1")                                               # link1 pending
    real = w.journal._fence_in
    w.journal._fence_in = lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device"))
    with pytest.raises(OSError):
        w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.journal._fence_in = real
    assert w.store.get(b.binding_id).status is BindingStatus.ACTIVE   # nothing committed
    assert w.store.set_status(b.binding_id, BindingStatus.PAUSED)      # and when it commits, it fences
    w.store.ratify(b.binding_id)
    out = w.resolve_open(approve=True)
    assert out.aborted and out.reason == "journal:fenced" and w.outbox() == [("link0", "q1-0")]


def test_an_executor_that_returns_garbage_has_an_unknown_outcome(tmp_path):
    # it may have acted before returning the wrong type: poisoned, never run again
    class Garbage(OutboxExecutor):
        def execute(self, action_name, payload, *, context_id):
            super().execute(action_name, payload, context_id=context_id)
            return None

    w = World(tmp_path)
    w.mint(chain=False)
    w.gate._executor = Garbage(tmp_path / "outbox.jsonl")
    first = w.dispatch("g1").outcome
    assert not first.fired and "outcome_unknown:TypeError" in first.reason
    assert World(tmp_path).dispatch("g1").outcome.reason == "journal:poisoned"
    assert w.outbox() == [("link0", "g1-0")]


# --- L1 round 1 (f9f43c5), each reproduced by a probe first ------------------------------------

class TickingWorld(World):
    """A World whose clock moves, so two proposals get distinct pending ids."""

    def __init__(self, work: Path) -> None:
        super().__init__(work)
        ticks = iter(range(10_000))
        clock = lambda: FIXED + _dt.timedelta(seconds=next(ticks))  # noqa: E731
        self.gate._clock = clock
        self.chains._clock = clock


def test_a_redelivery_while_a_decision_is_open_reuses_its_pending(tmp_path):
    # L1 F2: a second pending for the same (run, effect) minted a second push and contradictory receipts
    w = TickingWorld(tmp_path)
    w.mint(chain=True)
    first = w.dispatch("c1")
    again = w.dispatch("c1")
    assert again.outcome.pending and again.outcome.pending_id == first.outcome.pending_id
    assert len(w.gate.open_pendings()) == 1
    assert w.resolve_open(approve=True).completed
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]


def test_liveness_surfaces_open_holds_and_poisoned_effects(tmp_path):
    # L1 F3: an open hold stops a binding's undecided effects while every other count reads healthy
    from levain.autonomic import binding_liveness
    w = World(tmp_path)
    b = w.mint(chain=True)
    assert binding_liveness(w.store)["journal"] == {"open_holds": {}, "poisoned": 0, "approved_unrun": 0,
                                                    "unreadable": None}
    w.dispatch("c1")
    assert binding_liveness(w.store)["journal"]["open_holds"] == {b.binding_id: 1}
    w.mint(chain=False)
    assert _child(tmp_path, "w.dispatch('e9')", crash_after="link0") == 9   # a single-link run dies
    assert binding_liveness(World(tmp_path).store)["journal"]["poisoned"] == 1


def test_a_missing_receipt_is_written_even_after_the_run_was_fenced(tmp_path):
    # L1 F5: the barrier checked the fence before the recorded result, so the receipt never landed
    w = World(tmp_path)
    b = w.mint(chain=False)
    w.gate._persist = lambda **kw: None                            # the receipt does not land
    assert w.dispatch("e1").outcome.fired
    w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.store.ratify(b.binding_id)
    again = World(tmp_path).dispatch("e1").outcome
    assert again.replayed and again.receipt_id is not None
    assert len(list(w.receipts.read())) == 1 and w.outbox() == [("link0", "e1-0")]


def test_a_claimed_one_shot_resumes_its_own_run_on_redelivery(tmp_path):
    # L1 F8: an approved one-shot effect whose process stopped before it ran could never resume
    w = World(tmp_path)
    b = Binding.create(
        created_by="operator", created_at="2026-10-07T09:00:00o",
        trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
        goal=(SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),
              SubGoal(goal="email it", tools=("mail.send",), output="email:self")),
        tightness=TIGHT, posture=Posture.CONFIRM, one_shot=True,
        guard=(Guard(rationale="spoofed", dissent_author="codex",
                     kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                     kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
        status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.dispatch("o1")                                               # claimed; link0 ran; link1 proposed
    [p] = w.gate.open_pendings()
    from levain.autonomic import effect_digest
    w.journal.decide(w.journal.find_pending(p.pending_id)["hold_id"], approve=True, by="human",
                     digest=effect_digest(action_name=p.action_name, payload=p.payload, context_id=p.context_id))
    assert World(tmp_path).dispatch("o1").chain.completed          # resumed: link0 replays, link1 runs
    assert World(tmp_path).dispatch("o1").chain.completed          # and again: everything replays
    assert w.outbox() == [("link0", "o1-0"), ("link1", "o1-1")]
    assert w.dispatcher.dispatch({"type": "email", "id": "o2",     # a NEW event: the one-shot is spent
                                  "fields": {"from": "a@x.example", "dmarc": "pass"}}) == []
    w.store.set_status(b.binding_id, BindingStatus.REVOKED)        # a person's revoke after the claim
    out = World(tmp_path).dispatch("o1")
    assert out.chain.completed and all(l.outcome.replayed for l in out.chain.links)   # history, not a fire


def test_an_approval_the_journal_stopped_still_gets_a_receipt(tmp_path):
    # L1 F9: a resolve that ended cancelled or fenced left only a journal line
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("g1")
    w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.store.ratify(b.binding_id)
    out = w.resolve_open(approve=True)
    assert out.reason == "journal:fenced" and out.links[-1].outcome.receipt_id is not None
    assert sorted(r.fired for r in w.receipts.read()) == [False, True]   # link0 fired; link1's stop


# --- L3 round 1 (input 87b8ffcaa864c223), each reproduced first (job tmp l3/repro.py) ----------

def test_an_unattended_approval_of_a_confirm_pending_is_refused(tmp_path):
    # codex HIGH 1: resolve(approved, by="on-loop") fired a CONFIRM link no human approved
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    out = w.resolve_open_as(ConfirmDecision(approved=True, by="on-loop"))
    assert out.paused and out.reason == "unattended_approval_not_allowed"   # nothing decided
    assert w.outbox() == [("link0", "c1-0")]


def test_a_decision_with_a_non_bool_approval_cannot_be_built():
    # codex HIGH 3: approved="false" is truthy and approved the effect
    for bad in ("false", 1, None):
        with pytest.raises(TypeError):
            ConfirmDecision(approved=bad)
    with pytest.raises(ValueError):
        ConfirmDecision(approved=True, by="anyone")


def test_a_one_shot_whose_claim_write_failed_fires_for_at_most_one_event(tmp_path):
    # codex HIGH 2: run A admitted, the claim write failed; event B claimed; A re-delivered also fired
    w = World(tmp_path)
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00o",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
                       posture=Posture.ON_LOOP, one_shot=True, status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    real = w.store._write_raw

    def crash(records):
        raise OSError("stopped before the claim was written")

    w.store._write_raw = crash
    assert w.dispatcher.dispatch({"type": "email", "id": "A", "fields": {"from": "a@x.example"}}) == []
    w.store._write_raw = real
    for eid in ("B", "A", "B", "C"):
        w.dispatcher.dispatch({"type": "email", "id": eid, "fields": {"from": "a@x.example"}})
    assert [a for a, _ in w.outbox()] == ["link0"]                 # exactly one event's effect, ever


def test_a_replay_whose_bytes_changed_cancels_instead_of_replaying(tmp_path):
    # codex HIGH 7: link0 replayed its old result while the chain recorded new bytes for it
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("d1")
    w2 = World(tmp_path)
    w2.chains._request_builder = lambda b, ctx, i: dataclasses.replace(
        _chain_builder(b, ctx, i), payload="something else")
    out = w2.dispatch("d1")
    assert out.chain.aborted and len(out.chain.links) == 1                    # stopped AT link0
    assert out.chain.links[0].outcome.reason == "journal:cancelled"
    assert w.outbox() == [("link0", "d1-0")]


def test_a_malformed_manual_pending_record_does_not_stop_the_sweep(tmp_path):
    # codex MED 10 / complement MED 1: a bad record aborted list_open and every sweep
    from levain.autonomic import PendingAction, PendingActionStore
    store = PendingActionStore(tmp_path / "p.json")
    good = PendingAction.create(created_at="2026-10-07T12:00:00+00:00", action_name="a", payload="p",
                                context_id="c", query_text="q", query_date="d", posture="CONFIRM",
                                fail_open=False, requires_typed=False, authority={},
                                expires_at="2026-10-07T13:00:00+00:00")
    store.path.write_text(json.dumps([{"pending_id": "p-bad", "posture": 7}, good.to_dict()]))
    assert [p.pending_id for p in store.list_open()] == [good.pending_id]
    assert store.get("p-bad") is None and store.claim("p-bad") is None
    gate = EfferentGate(manifest=ActionManifest({"a": HIGH}), store=GateReceiptStore(tmp_path / "r.jsonl"),
                        executor=OutboxExecutor(tmp_path / "o.jsonl"), clock=lambda: FIXED,
                        transport=_Transport(), pending_store=store)
    out = gate.sweep_timeouts(FIXED + _dt.timedelta(hours=2))
    assert [o.reason for o in out] == ["denied:confirm_window_elapsed"]


def test_a_chain_link_never_fires_standalone_and_its_state_cannot_be_lost(tmp_path):
    # codex HIGH 5: a chain state lost between claim and resolve let a plain resolve fire the link alone.
    # The continuation now lives IN the hold, so there is no separate state to lose; a plain resolve of a
    # chain link still refuses and changes nothing.
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    hold = w.journal.find_pending(p.pending_id)
    assert hold["chained"] and hold["chain"]["paused_at_link"] == 1 and hold["pending"]["pending_id"] == p.pending_id
    plain = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="human"))
    assert plain.refused and plain.reason == "chained_pending_resolves_through_its_chain"
    assert w.outbox() == [("link0", "c1-0")] and len(w.gate.open_pendings()) == 1
    assert not w.chains._chain_store.path.exists()                # the chain store is never written
    assert w.resolve_open(approve=True).completed
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]


def test_a_redelivered_pause_reports_the_one_chain_the_journal_holds(tmp_path):
    # complement 5: each re-delivery wrote another chain state for the same pending
    w = TickingWorld(tmp_path)
    w.mint(chain=True)
    first = w.dispatch("c1").chain
    again = w.dispatch("c1").chain
    assert again.paused and again.chain_id == first.chain_id and again.pending_id == first.pending_id
    assert len(w.journal.open_holds()) == 1


def test_stores_refuse_to_overwrite_a_file_they_cannot_read(tmp_path):
    # codex MED 9 / glm: a mutation that read an unreadable store as empty rewrote it with one record
    from levain.autonomic import ChainStateStore as CSS, ChainStoreUnavailableError, PendingActionStore
    pend = PendingActionStore(tmp_path / "p.json")
    pend.path.write_text("{not json")
    with pytest.raises(OSError):
        pend.remove("anything")
    assert pend.path.read_text() == "{not json"
    chains = CSS(tmp_path / "c.json")
    chains.path.write_text("{not json")
    with pytest.raises(ChainStoreUnavailableError):
        chains.remove("anything")
    assert chains.path.read_text() == "{not json"


def test_a_pending_that_cannot_be_built_records_nothing(tmp_path):
    # glm MED: a raise between the hold and the pending write left the hold open. The pending is built
    # before the hold now, and the hold carries it, so a failure records nothing at all.
    w = World(tmp_path)
    w.mint(chain=True)
    import levain.autonomic.gate as gate_mod
    real = gate_mod.PendingAction.create
    gate_mod.PendingAction.create = staticmethod(lambda **kw: (_ for _ in ()).throw(ValueError("bad")))
    try:
        out = w.dispatch("x1")
    finally:
        gate_mod.PendingAction.create = real
    assert out.outcome.refused and w.journal.open_holds() == []
    assert w.dispatch("x1").chain.paused                           # not cancelled: proposed again


def test_a_removed_and_readded_grant_fences_every_earlier_run(tmp_path):
    # complement 7: a re-add started at a generation an earlier admitted run could still match
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("r1")                                               # link1 pending under the old record
    w.store.remove(b.binding_id)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    out = w.resolve_open(approve=True)
    assert out.aborted and out.reason == "journal:fenced" and w.outbox() == [("link0", "r1-0")]


def test_a_pause_whose_registry_write_fails_leaves_no_fence(tmp_path):
    # glm MED: a fence written before a registry write that then failed left a live binding fenced
    w = World(tmp_path)
    b = w.mint(chain=False)
    real = w.store._write_raw

    def fail(records, conn):
        raise OSError("disk full")

    w.store._write_raw = fail
    with pytest.raises(OSError):
        w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.store._write_raw = real
    assert w.journal.generation(b.binding_id) == 0
    assert w.dispatch("s1").outcome.fired                         # still live, still fires


def test_admission_lets_a_claimed_one_shot_back_in_only_for_its_own_run(tmp_path):
    # codex HIGH 2 at the store: only the run recorded with the claim may re-enter
    w = World(tmp_path)
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00o",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
                       posture=Posture.ON_LOOP, one_shot=True, status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    assert w.store.admit(b.binding_id, "run-X") is not None and w.store.claimed_run(b.binding_id) == "run-X"
    assert w.store.admit(b.binding_id, "run-Y") is None
    assert w.store.admit(b.binding_id, "run-X") is not None


# --- L3 round 2 (input 35ab0fd2aacd0f38) -------------------------------------------------------

def _one_shot(w, posture=Posture.ON_LOOP):
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00o",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
                       posture=posture, one_shot=True, status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    return b


def test_a_one_shot_claim_and_its_run_commit_together(tmp_path):
    # codex HIGH 1 + complement MED 1: the claim was written, the process stopped before the run was
    # admitted, a person revoked it, and the re-delivered run fired. The claim and the run are one
    # transaction: a failure leaves the one-shot unclaimed, and a revoke then stops it outright.
    w = World(tmp_path)
    b = _one_shot(w)
    real = w.journal._start_in
    w.journal._start_in = lambda *a: (_ for _ in ()).throw(OSError("stopped here"))
    assert w.dispatcher.dispatch({"type": "email", "id": "A", "fields": {"from": "a@x.example"}}) == []
    w.journal._start_in = real
    assert w.store.claimed_run(b.binding_id) is None and w.store.get(b.binding_id).status is BindingStatus.ACTIVE
    w.store.set_status(b.binding_id, BindingStatus.REVOKED)        # the person cancels it
    assert w.dispatcher.dispatch({"type": "email", "id": "A", "fields": {"from": "a@x.example"}}) == []
    assert w.outbox() == []


def test_a_decision_and_its_pending_are_one_record(tmp_path):
    # codex HIGH 2: resolve removed the pending, then the process stopped before the deny reached the
    # journal, and a re-delivery re-proposed the denied action. A pending is now the hold itself: the deny
    # is one appended line, so there is no step between "decided" and "no longer pending".
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    assert w.resolve_open(approve=False).aborted
    assert w.gate.open_pendings() == []
    again = World(tmp_path).dispatch("c1")
    assert again.chain.aborted and again.outcome.reason == "journal:cancelled"
    assert w.outbox() == [("link0", "c1-0")]


def test_a_duplicated_manual_pending_resolves_once(tmp_path):
    # codex MED: two records with one id; each claim removed one copy and fired once
    from levain.autonomic import PendingAction, PendingActionStore
    store = PendingActionStore(tmp_path / "p.json")
    p = PendingAction.create(created_at="t", action_name="a", payload="p", context_id="c", query_text="q",
                             query_date="d", posture="CONFIRM", fail_open=False, requires_typed=False,
                             authority={})
    store.path.write_text(json.dumps([p.to_dict(), p.to_dict()]))
    assert store.claim(p.pending_id) is not None
    assert store.claim(p.pending_id) is None


def test_the_dispatcher_and_chain_executor_share_one_journal_and_one_gate(tmp_path):
    # complement 3: a store with a journal behind a gate without one ran unjournaled
    w = World(tmp_path)
    bare_gate = EfferentGate(manifest=ActionManifest({}), store=w.receipts,
                             executor=OutboxExecutor(tmp_path / "o.jsonl"))
    with pytest.raises(ValueError):
        FireDispatcher(store=w.store, gate=bare_gate, predicate_match=lambda p, e: True,
                       request_builder=_single_builder, risk_resolver=lambda b: LOW, clock=lambda: FIXED)
    other = ChainExecutor(gate=bare_gate, request_builder=_chain_builder, risk_resolver=lambda b, i: LOW,
                          trust_resolver=_trust, chain_store=ChainStateStore(tmp_path / "c2.json"),
                          clock=lambda: FIXED)
    with pytest.raises(ValueError):
        FireDispatcher(store=w.store, gate=w.gate, predicate_match=lambda p, e: True,
                       request_builder=_single_builder, risk_resolver=lambda b: LOW, clock=lambda: FIXED,
                       chain_executor=other)


def test_an_approval_nobody_may_give_unattended_records_nothing(tmp_path):
    # complement 5: the refused unattended approval cancelled the run; it is not a decision, so the
    # hold stays open and a person can still approve it
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    out = w.resolve_open_as(ConfirmDecision(approved=True, by="on-loop"))
    assert "unattended_approval_not_allowed" in out.reason
    assert len(w.gate.open_pendings()) == 1
    assert w.resolve_open(approve=True).completed


# --- L1+L2 on ade957f: one settle construct (head's ruling) ------------------------------------

def test_a_deny_the_journal_could_not_record_changes_nothing(tmp_path):
    # probe1 (round 3): a journal fault on the deny released the pending and wrote a receipt while the
    # hold stayed open. With one record, a decide that faults has changed nothing to reconcile.
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    receipts_before = len(list(w.receipts.read()))
    real = w.journal.decide
    w.journal.decide = lambda *a, **k: (_ for _ in ()).throw(OSError("EIO"))
    out = w.resolve_open(approve=False)
    assert out.aborted and "OSError" in out.reason
    assert len(w.gate.open_pendings()) == 1 and len(list(w.receipts.read())) == receipts_before
    w.journal.decide = real
    retry = w.resolve_open(approve=False)
    assert retry.aborted and retry.reason == "denied:human"
    assert w.dispatch("c1").outcome.reason == "journal:cancelled" and w.outbox() == [("link0", "c1-0")]


def test_a_sweep_drop_the_journal_could_not_record_changes_nothing(tmp_path):
    # probe5 (round 3): the sweep's drop paths ignored a journal fault and removed the pending anyway
    w = World(tmp_path)
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00k",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.read",), output="doc:s"),), tightness=TIGHT,
                       posture=Posture.COOLING_OFF,
                       guard=(Guard(rationale="r", dissent_author="codex",
                                    kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                                    kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
                       status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.dispatch("s1")
    assert len(w.gate.open_pendings()) == 1                        # cooling-off, not on the allowlist
    real = w.journal.decide
    w.journal.decide = lambda *a, **k: (_ for _ in ()).throw(OSError("EIO"))
    w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=3))
    assert len(w.gate.open_pendings()) == 1 and len(w.journal.open_holds()) == 1
    w.journal.decide = real
    w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=3))           # the drop lands once the journal does
    assert w.gate.open_pendings() == [] and w.journal.open_holds() == [] and w.outbox() == []


def test_an_approval_after_a_rejection_fires_nothing_and_writes_no_receipt(tmp_path):
    # probe3: already_decided fell through to _fire after an earlier REJECT and wrote a second receipt
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    from levain.autonomic import effect_digest
    w.journal.decide(w.journal.find_pending(p.pending_id)["hold_id"], approve=False, by="human",
                     digest=effect_digest(action_name=p.action_name, payload=p.payload, context_id=p.context_id))
    before = len(list(w.receipts.read()))
    out = w.chains.resume(p.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out.links[-1].outcome.reason == "journal:already_decided"
    assert len(list(w.receipts.read())) == before and w.outbox() == [("link0", "c1-0")]


def test_a_deny_after_an_approval_writes_no_receipt_and_the_approval_runs(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    from levain.autonomic import effect_digest
    w.journal.decide(w.journal.find_pending(p.pending_id)["hold_id"], approve=True, by="human",
                     digest=effect_digest(action_name=p.action_name, payload=p.payload, context_id=p.context_id))
    before = len(list(w.receipts.read()))
    out = w.chains.resume(p.pending_id, ConfirmDecision(approved=False, by="human"))
    assert out.links[-1].outcome.reason == "journal:already_decided"
    assert len(list(w.receipts.read())) == before
    assert w.dispatch("c1").chain.completed                         # the approval still runs, once
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]


def test_a_hold_that_cannot_be_written_proposes_nothing(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    real = w.journal.hold
    w.journal.hold = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    out = w.dispatch("h1")
    assert out.chain.aborted and out.outcome.reason == "journal_error:OSError"
    assert w.gate.open_pendings() == [] and w.journal.open_holds() == []
    w.journal.hold = real
    assert w.dispatch("h1").chain.paused                           # nothing was decided: proposed again


def test_the_chain_sweep_resumes_an_expired_link_by_its_silence_default(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    assert w.chains.sweep_timeouts(FIXED + _dt.timedelta(minutes=30)) == []    # not expired yet
    assert w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=2)) == []         # the gate leaves chain links
    assert len(w.gate.open_pendings()) == 1
    [out] = w.chains.sweep_timeouts(FIXED + _dt.timedelta(hours=2))
    assert out.aborted and out.reason == "denied:confirm_window_elapsed"      # CONFIRM drops on silence
    assert w.gate.open_pendings() == [] and w.outbox() == [("link0", "c1-0")]
    assert w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=2)) == []         # the gate leaves chain links


def test_a_journaled_run_never_writes_the_pending_or_chain_store(tmp_path):
    # the journal is the only durable home of a decision: open pendings and chain state are read from it
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    w.dispatch("c2")
    assert len(w.gate.open_pendings()) == 1                         # c2 is held behind c1's decision
    assert not w.pending.path.exists() and not w.chains._chain_store.path.exists()
    assert w.resolve_open(approve=True).completed
    assert not w.pending.path.exists() and not w.chains._chain_store.path.exists()


def test_a_hold_whose_pending_was_altered_on_disk_never_fires(tmp_path):
    from tests.test_autonomic_rawstore import rewrite_holds
    w = World(tmp_path)
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00t",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.send",), output="email:x"),), tightness=TIGHT,
                       posture=Posture.CONFIRM,
                       guard=(Guard(rationale="r", dissent_author="codex",
                                    kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                                    kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
                       status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.dispatcher.dispatch({"type": "email", "id": "t1", "fields": {"from": "a@x.example", "dmarc": "pass"}})
    [p] = w.gate.open_pendings()
    rewrite_holds(w.journal, lambda h: dict(h, pending=dict(h["pending"], payload="send everything elsewhere")))
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out.refused and out.reason == "integrity:seal_mismatch" and not out.fired
    assert w.journal.open_holds() == [] and w.outbox() == []        # rejected; nothing was sent


def test_an_approval_racing_a_rejection_fires_nothing(tmp_path):
    # the hold was read open, then another resolver rejected it before this approval's decide
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    stale = w.journal.find_pending(p.pending_id)
    from levain.autonomic import effect_digest
    w.journal.decide(stale["hold_id"], approve=False, by="human",
                     digest=effect_digest(action_name=p.action_name, payload=p.payload, context_id=p.context_id))
    before = len(list(w.receipts.read()))
    w.journal.peek = lambda *a, **k: None                          # the rejection landed after this check
    out = w.gate._resolve_hold(stale, ConfirmDecision(approved=True, by="human"), chain_owned=True)
    assert out.refused and out.reason == "journal:already_decided"
    assert len(list(w.receipts.read())) == before and w.outbox() == [("link0", "c1-0")]


def test_the_unjournaled_chain_store_keeps_one_state_per_paused_link(tmp_path):
    from levain.autonomic import ChainState, ChainStateStore as CSS
    w = World(tmp_path)
    b = w.mint(chain=True)
    store = CSS(tmp_path / "legacy-chains.json")
    mk = lambda at: ChainState.create(created_at=at, binding=b, trigger_event={"id": "e"}, completed=(),  # noqa: E731
                                      paused_at_link=1, paused_payload="p", pending_id="pend-1")
    first, twin = mk("2026-10-07T12:00:00"), mk("2026-10-07T13:00:00")
    assert first.chain_id != twin.chain_id
    assert store.add(first) == first.chain_id and store.add(twin) == first.chain_id
    assert [s.chain_id for s in store.list_open()] == [first.chain_id]



# --- L1+L2 on the single-store commit (20ff4ca), each reproduced by a probe first ------------

def test_an_approved_effect_that_could_not_run_yet_is_held_and_listed(tmp_path):
    # MED: an approval whose effect hit a fault was reported "refused" and listed nowhere
    from levain.autonomic import binding_liveness
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    real = w.journal.effect
    w.journal.effect = lambda *a, **k: (_ for _ in ()).throw(OSError("EIO"))
    out = w.resolve_open(approve=True)
    assert out.held and out.links[-1].outcome.reason.startswith("approved_not_yet_run:")
    assert len(w.journal.approved_unrun()) == 1
    assert binding_liveness(w.store)["journal"]["approved_unrun"] == 1
    w.journal.effect = real
    sweep_by = w.chains.resume(p.pending_id, ConfirmDecision(approved=True, by="human"))   # retried
    assert sweep_by.completed and w.journal.approved_unrun() == []
    receipts = [r for r in w.receipts.read() if r.fired]
    assert len(receipts) == 2 and w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]


def test_a_receipt_names_the_decider_whose_approval_fired(tmp_path):
    # LOW: a later on-loop re-resolve of an earlier human approval wrote a receipt naming itself
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    from levain.autonomic import effect_digest
    w.journal.decide(w.journal.find_pending(p.pending_id)["hold_id"], approve=True, by="human",
                     digest=effect_digest(action_name=p.action_name, payload=p.payload, context_id=p.context_id))
    out = w.gate._resolve_hold(w.journal.find_pending(p.pending_id),
                               ConfirmDecision(approved=True, by="on-loop"), chain_owned=True)
    assert out.fired
    [face] = [r.action_face for r in w.receipts.read() if r.fired and r.action_name == "link1"]
    assert face["gate"]["by"] == "human" and face["gate"]["verdict"] == "approved"


def test_a_late_no_after_a_completed_chain_reports_held_not_aborted(tmp_path):
    # MED: "aborted" for a chain that had completed; the late NO changed nothing
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    assert w.resolve_open(approve=True).completed
    late = w.chains.resume(p.pending_id, ConfirmDecision(approved=False, by="human"))
    assert late.held and late.reason == "journal:already_decided"


def test_the_chain_sweep_drops_a_race_it_lost(tmp_path):
    # MED: the chain sweep reported "aborted already_decided" for a link a person had approved
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    stale = w.journal.open_holds()
    assert w.resolve_open(approve=True).completed
    w.journal.open_holds = lambda: stale                           # the sweep read the holds before the person
    assert w.chains.sweep_timeouts(FIXED + _dt.timedelta(hours=2)) == []


def test_a_chain_link_whose_flag_was_altered_still_cannot_fire_alone(tmp_path):
    # LOW: chained=false on a chain link's hold let a plain resolve fire the link standalone
    from tests.test_autonomic_rawstore import rewrite_holds
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    [p] = w.gate.open_pendings()
    rewrite_holds(w.journal, lambda h: dict(h, chained=False))
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out.reason == "chained_pending_resolves_through_its_chain" and w.outbox() == [("link0", "c1-0")]


def test_a_continuation_from_another_hold_is_refused(tmp_path):
    # LOW: the continuation was not tied to its own hold (pending id, binding); a validly sealed
    # continuation copied from elsewhere must not resume this hold's chain
    from levain.autonomic import ChainState
    from tests.test_autonomic_rawstore import rewrite_holds
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")

    def swap(h):
        st = ChainState.from_dict(h["chain"])
        other = ChainState.create(created_at="2026-10-07T12:30:00", binding=st.binding_obj(),
                                  trigger_event=st.trigger_event, completed=st.completed_links(),
                                  paused_at_link=st.paused_at_link, paused_payload=st.paused_payload,
                                  pending_id="hold:run-other:link-1")
        return dict(h, chain=other.to_dict())

    rewrite_holds(w.journal, swap)
    out = w.resolve_open(approve=True)
    assert out.aborted and out.reason == "integrity:continuation_not_this_hold"
    assert w.outbox() == [("link0", "c1-0")]


def test_a_pause_between_the_approval_and_the_effect_stops_it(tmp_path):
    # the fence is read in the effect's own transaction, so a pause landing after the decision stops it
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("c1")
    real_decide = w.journal.decide

    def decide_then_pause(*a, **k):
        d = real_decide(*a, **k)
        w.store.set_status(b.binding_id, BindingStatus.PAUSED)
        return d

    w.journal.decide = decide_then_pause
    out = w.resolve_open(approve=True)
    assert not out.completed and w.outbox() == [("link0", "c1-0")]
    assert out.links[-1].outcome.receipt_id is not None            # a terminal end: it gets a receipt
    assert [r.fired for r in w.receipts.read() if r.action_name == "link1"] == [False]

def test_a_sweep_keeps_its_journaled_outcomes_when_the_manual_store_faults(tmp_path):
    # LOW: a manual-store read fault discarded decisions the same sweep had already made
    w = World(tmp_path)
    b = Binding.create(created_by="operator", created_at="2026-10-07T09:00:00k",
                       trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
                       goal=(SubGoal(goal="s", tools=("mail.send",), output="email:x"),), tightness=TIGHT,
                       posture=Posture.CONFIRM,
                       guard=(Guard(rationale="r", dissent_author="codex",
                                    kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                                    kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
                       status=BindingStatus.PAUSED)
    w.store.add(b)
    w.store.ratify(b.binding_id)
    w.dispatcher.dispatch({"type": "email", "id": "m1", "fields": {"from": "a@x.example", "dmarc": "pass"}})
    w.pending.list_open = lambda: (_ for _ in ()).throw(OSError("EIO"))
    out = w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=2))
    assert [o.reason for o in out] == ["denied:confirm_window_elapsed"]


# --- the one-database store ---------------------------------------------------------------------

def test_many_processes_delivering_one_event_produce_one_effect(tmp_path):
    w = World(tmp_path)
    w.mint(chain=False)
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from pathlib import Path
        from test_autonomic_journal_wiring import World
        World(Path({str(tmp_path)!r})).dispatch('race')
    """)
    procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for _ in range(8)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    assert w.outbox() == [("link0", "race-0")]                         # exactly once
    assert w.fire_count(w.store.list_all()[0]) == 1


def test_the_store_is_one_private_directory_with_durable_settings(tmp_path):
    import sqlite3
    import stat
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c1")
    store_dir = tmp_path / "store"
    assert stat.S_IMODE(store_dir.stat().st_mode) == 0o700
    outside = [p for p in tmp_path.iterdir() if p.name.startswith("autonomic.db")]
    assert outside == []                                              # nothing of the store outside it
    with w.journal.db.write() as conn:                                # an open connection: sidecars exist
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2     # FULL
        assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
        names = {p.name for p in store_dir.iterdir()}
        assert {"autonomic.db", "autonomic.db-wal", "autonomic.db-shm"} <= names
    assert w.journal.db.meta("format") == "levain-autonomic/1"
    assert sqlite3.connect(store_dir / "autonomic.db").execute(
        "SELECT COUNT(*) FROM holds").fetchone()[0] == 1


def test_a_store_and_a_journal_in_different_directories_are_refused(tmp_path):
    # they must share one database, or a pause and its fence would commit in two places
    with pytest.raises(ValueError, match="same store directory"):
        BindingStore(tmp_path / "a", journal=RunJournal(tmp_path / "b"))
    BindingStore(tmp_path / "a", journal=RunJournal(tmp_path / "a"))
