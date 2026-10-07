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
        self.journal = RunJournal(work / "journal.jsonl")
        self.store = BindingStore(work / "bindings.json", journal=self.journal)
        self.pending = PendingActionStore(work / "pending.json")
        self.receipts = GateReceiptStore(work / "receipts.jsonl")
        self.gate = EfferentGate(
            manifest=ActionManifest({"link0": LOW, "link1": HIGH}), store=self.receipts,
            executor=OutboxExecutor(work / "outbox.jsonl"), clock=lambda: FIXED,
            transport=_Transport(), pending_store=self.pending, journal=self.journal,
            binding_generation=self.store.generation,
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
        [p] = self.pending.list_open()
        return self.chains.resume(p.pending_id, ConfirmDecision(approved=approve, by="human"))

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
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    (tmp_path / "journal.jsonl").write_text(
        "\n".join(l for l in lines if json.loads(l)["t"] != "receipt") + "\n")   # as if it stopped first
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
    [p] = w.pending.list_open()
    assert w.pending.claim(p.pending_id) is not None
    from levain.autonomic import effect_digest
    assert w.journal.decide(hold_id_for(p.run_id, p.effect_id), approve=True, by="human",
                            digest=effect_digest(action_name=p.action_name, payload=p.payload,
                                                 context_id=p.context_id)).ok
    out = World(tmp_path).dispatch("h1")
    assert out.chain.completed and out.chain.links[-1].outcome.fired
    assert w.outbox() == [("link0", "h1-0"), ("link1", "h1-1")]
    assert w.pending.list_open() == [] and w.fire_count(b) == 1


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
    before = w.journal.generation(b.binding_id)
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
    assert w.journal.generation(b.binding_id) == before + 1


def test_activating_verbs_do_not_fence(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)                                       # add + ratify
    w.store.record_fire(b.binding_id, clean=True, fired_at="2026-10-07T12:00:00")
    assert w.journal.generation(b.binding_id) == 0


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
    with pytest.raises(ValueError, match="binding_generation"):
        EfferentGate(manifest=ActionManifest({}), store=GateReceiptStore(tmp_path / "r.jsonl"),
                     executor=OutboxExecutor(tmp_path / "o.jsonl"), journal=RunJournal(tmp_path / "j.jsonl"))
    assert out.refused and out.reason == "run_without_journal"
    assert not (tmp_path / "outbox.jsonl").exists()


def test_a_run_that_was_never_admitted_is_refused(tmp_path):
    w = World(tmp_path)
    b = w.mint(chain=False)
    req = dataclasses.replace(_binding_request(RunRef("run-never", "link-0")),
                              authority=AuthorityScope(grantor="binding", grant="g", binding_id=b.binding_id, hops=0))
    out = w.gate.gate(req)
    assert out.refused and out.reason == "run_not_admitted" and w.outbox() == []


def test_a_fire_for_a_binding_the_registry_does_not_hold_is_refused(tmp_path):
    w = World(tmp_path)
    out = w.gate.gate(_binding_request(RunRef("run-x", "link-0")))
    assert out.refused and out.reason == "binding_generation_unknown" and w.outbox() == []


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
    # L2 P1, reproduced at f9f43c5: a pause wrote its fence before its registry write, and a dispatch
    # whose admission + lockless snapshot ran between the two fired after set_status returned. Here the
    # dispatch is started from INSIDE the pause, after its fence: it must wait for the pause to commit
    # and then see the binding paused.
    import threading
    import time
    w = World(tmp_path)
    b = w.mint(chain=False)
    real_fence = w.journal.fence
    done: list[bool] = []

    def fence_then_dispatch(binding_id, **kw):
        g = real_fence(binding_id, **kw)
        t = threading.Thread(target=lambda: done.append(bool(w.dispatcher.dispatch(
            {"type": "email", "id": "e", "fields": {"from": "a@x.example", "dmarc": "pass"}}))))
        t.start()
        time.sleep(0.3)                                   # the dispatch had every chance to run here
        fence_then_dispatch.thread = t
        return g

    w.journal.fence = fence_then_dispatch
    assert w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    fence_then_dispatch.thread.join()
    assert w.outbox() == [] and done == [False]


def test_a_pause_whose_journal_fence_fails_still_stops_the_admitted_run(tmp_path):
    # L2 P3, reproduced at f9f43c5: the fence append failed once, the pause committed, and the sweep
    # auto-fired the admitted run. The registry generation is the authority now.
    w = World(tmp_path)
    b = w.mint(chain=True)
    w.dispatch("q1")                                               # link1 pending
    real_fence = w.journal.fence

    def failing_fence(*a, **k):
        raise OSError(28, "No space left on device")

    w.journal.fence = failing_fence
    assert w.store.set_status(b.binding_id, BindingStatus.PAUSED)
    w.journal.fence = real_fence
    w.store.ratify(b.binding_id)                                   # even re-activated
    out = w.resolve_open(approve=True)
    assert out.aborted and out.reason == "journal:fenced"
    assert w.outbox() == [("link0", "q1-0")]


def test_an_orphaned_hold_is_rejected_by_the_sweep(tmp_path):
    # L2 P5: the process stopped after the hold and before the pending: nothing would ever decide it
    w = World(tmp_path)
    w.mint(chain=True)

    def crash(pending):
        raise SystemExit("process stopped here")

    w.pending.add = crash
    with pytest.raises(SystemExit):
        w.dispatch("o1")
    w = World(tmp_path)
    assert len(w.journal.open_holds()) == 1 and w.pending.list_open() == []
    w.gate.sweep_timeouts(FIXED + _dt.timedelta(seconds=3599))      # inside twice the window: left alone
    assert len(w.journal.open_holds()) == 1
    w.gate.sweep_timeouts(FIXED + _dt.timedelta(hours=3))
    assert w.journal.open_holds() == []
    assert w.dispatch("o2").chain.paused                           # the binding is not stuck


def test_a_hold_whose_pending_cannot_persist_is_rejected_not_left_open(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)

    def broken_add(pending):
        raise OSError("disk full")

    w.pending.add = broken_add
    out = w.dispatch("d1")
    assert out.chain.aborted and "pending_persist_failed" in out.outcome.reason
    assert w.journal.open_holds() == []                            # no orphaned decision blocks the binding
    w2 = World(tmp_path)
    assert w2.dispatch("d2").chain.paused                          # the binding is not stuck


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
        self.gate._clock = lambda: FIXED + _dt.timedelta(seconds=next(ticks))


def test_a_redelivery_while_a_decision_is_open_reuses_its_pending(tmp_path):
    # L1 F2: a second pending for the same (run, effect) minted a second push and contradictory receipts
    w = TickingWorld(tmp_path)
    w.mint(chain=True)
    first = w.dispatch("c1")
    again = w.dispatch("c1")
    assert again.outcome.pending and again.outcome.pending_id == first.outcome.pending_id
    assert len(w.pending.list_open()) == 1
    assert w.resolve_open(approve=True).completed
    assert w.outbox() == [("link0", "c1-0"), ("link1", "c1-1")]


def test_liveness_surfaces_open_holds_and_poisoned_effects(tmp_path):
    # L1 F3: an open hold stops a binding's undecided effects while every other count reads healthy
    from levain.autonomic import binding_liveness
    w = World(tmp_path)
    b = w.mint(chain=True)
    assert binding_liveness(w.store)["journal"] == {"open_holds": {}, "poisoned": 0, "unreadable": None}
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


def test_an_infrastructure_fault_withdraws_the_decision_without_cancelling_the_run(tmp_path):
    # L1 F6: a transient pending-store fault cancelled the run for good
    w = World(tmp_path)
    w.mint(chain=True)
    real_add = w.pending.add

    def disk_full(pending):
        raise OSError(28, "No space left on device")

    w.pending.add = disk_full
    assert "pending_persist_failed" in w.dispatch("f1").outcome.reason
    assert w.journal.open_holds() == []
    w.pending.add = real_add
    assert w.dispatch("f1").chain.paused                           # proposed again after recovery
    assert w.resolve_open(approve=True).completed
    assert w.outbox() == [("link0", "f1-0"), ("link1", "f1-1")]


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
    [p] = w.pending.list_open()
    assert w.pending.claim(p.pending_id) is not None
    from levain.autonomic import effect_digest
    w.journal.decide(hold_id_for(p.run_id, p.effect_id), approve=True, by="human",
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
