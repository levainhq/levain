"""Phase-2 Slice-4b — the CHAIN EXECUTOR (the gate travels with the chain).

Blocks:
  1. ChainState — the sealed pause/resume continuation (seal tamper-evidence).
  3. ChainExecutor.execute — the per-link walk: fire on-loop links, pause at the first confirm-class
     link, thread the inter-link data-flow, the seam-#2 ratification-consistency bar, the kill abort.
  4. ChainExecutor.resume — fire the paused link + continue; deny ends the chain; a revoke or removal
     fences the run; an altered or unreadable continuation rejects the hold.

Every chain here is a journaled run (a binding fire has no other mode): the rig shares one store
between the registry and the run journal, and ``_run`` admits the run the way the dispatcher does.
  5. FireDispatcher delegation — a multi-link binding routes to the chain executor (vs single-link 4a).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import pytest

from levain.autonomic import (
    ActionManifest,
    ActionRequest,
    ActionRisk,
    Binding,
    BindingStatus,
    BindingStore,
    ChainContext,
    ChainExecutor,
    ChainState,
    CompletedLink,
    ConfirmDecision,
    EfferentGate,
    ExecutionResult,
    FireDispatcher,
    GateReceiptStore,
    Guard,
    IntentProvenance,
    PendingActionStore,
    RunJournal,
    Posture,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    manual_invocation,
    run_id_for,
)
from tests.autonomic_confirm_keys import confirm_signers, signed_yes
from tests.test_autonomic_rawstore import rewrite_holds

FIXED = _dt.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
LOW_INTERNAL = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
HIGH_EXTERNAL = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)
TIGHT = TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9)
EVENT = {"type": "email", "id": "evt-1", "fields": {"from": "x@y.example", "dmarc": "pass", "subject": "hi"}}


# --- test doubles --------------------------------------------------------------------------------
@dataclass
class RecordingExecutor:
    confined = True   # a test double: declares the floor a real binding executor runs under
    name: str = "recording"
    ok: bool = True
    calls: list = field(default_factory=list)

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult:
        self.calls.append((action_name, payload, context_id))
        return ExecutionResult(ok=self.ok, detail=f"did {action_name}", downstream_id=f"out::{action_name}",
                               error=None if self.ok else "boom")


@dataclass
class FakeTransport:
    name: str = "fake"
    proposals: list = field(default_factory=list)

    def propose(self, proposal) -> bool:
        self.proposals.append(proposal)
        return True


def _kill_guard(value="pass"):
    return Guard(
        rationale="a from-domain check trusts the envelope; a spoof could auto-fire the chain.",
        dissent_author="codex",
        kill_predicate={"op": "not", "clause": {"op": "==", "field": "dmarc", "value": value}},
        kill_drill={"dmarc": "fail"},
        kill_authored_by="phill",
    )


def _two_link_binding(*, posture=Posture.CONFIRM, one_shot=False, status=BindingStatus.ACTIVE, guard=None):
    """A 2-link chain: link 0 deliver (on-loop) → link 1 email (confirm). Sealed at the whole-chain
    posture (CONFIRM by default — the §2.3 set-risk max == link 1)."""
    return Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="summarize the email to a doc", tools=("mail.read", "gdrive.create"), output="doc:summary"),
              SubGoal(goal="email the summary to the sender", tools=("mail.send",), output="email:self")),
        tightness=TIGHT, posture=posture, one_shot=one_shot, status=status,
        guard=(guard if guard is not None else (_kill_guard(),)),
    )


# The gate's RESOLVE path re-validates a binding's pending against the risk floor sealed at propose
# (from the per-link sealed-tool risk) and the manifest's current floor for the action name when it
# declares one, so a declared link here must not sit above the floor it was proposed at.
_RESOLVE_MANIFEST = ActionManifest({"link0": LOW_INTERNAL, "link1": HIGH_EXTERNAL, "link2": LOW_INTERNAL})


_RIGS: dict[str, tuple[RunJournal, BindingStore]] = {}


def _rig(tmp_path) -> tuple[RunJournal, BindingStore]:
    """One run journal and the binding registry over it, per test directory (the dispatcher requires
    the gate's journal to BE the store's)."""
    key = str(tmp_path)
    if key not in _RIGS:
        journal = RunJournal(tmp_path / "store")
        _RIGS[key] = (journal, BindingStore(tmp_path / "store", journal=journal))
    return _RIGS[key]


def _journal(tmp_path) -> RunJournal:
    return _rig(tmp_path)[0]


def _registry(tmp_path) -> BindingStore:
    """The binding registry, in the same store as the gate's run journal."""
    return _rig(tmp_path)[1]


def _gate(tmp_path, *, executor=None, transport=None, auto_fire=None, manifest=None, binding_risk=None):
    return EfferentGate(confirm_signers=confirm_signers(),
        manifest=manifest if manifest is not None else _RESOLVE_MANIFEST,
        store=GateReceiptStore(tmp_path / "r.jsonl"),
        executor=executor or RecordingExecutor(),
        clock=lambda: FIXED,
        transport=transport,
        pending_store=PendingActionStore(tmp_path / "pend.json") if transport is not None else None,
        auto_fire_actions=auto_fire,
        journal=_journal(tmp_path),
        binding_risk=binding_risk or _risk_resolver,
    )


def _risk_resolver(binding, i):
    return [LOW_INTERNAL, HIGH_EXTERNAL][i]


def _trust_resolver(binding, i):
    # STRONG + INTENT_FREE: hops=0 → ABOVE_LOOP+intent_free(1) = ON_LOOP (fires); hops=1 + external floor → CONFIRM.
    return TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                        hops=i, human_present=False)


def _request_builder(binding, ctx: ChainContext, i):
    """A test builder that ENCODES the upstream data-flow into the payload (so we can assert threading):
    link i's payload names how many upstream links completed + the last upstream's downstream_id."""
    upstream = ctx.completed[-1].downstream_id if ctx.completed else "NONE"
    return ActionRequest(
        action_name=f"link{i}", payload=f"link{i}|upstream={len(ctx.completed)}|from={upstream}",
        context_id=f"ctx-{i}", query_text="q", query_date="2026-06-30",
        trust=TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                           hops=i, human_present=False),
        grounded=True, authority=manual_invocation(), overall_confidence=1.0, directive_confidence=1.0,
    )


def _executor(tmp_path, *, transport=None, auto_fire=None, executor=None, binding_store=None,
              risk_resolver=_risk_resolver, trust_resolver=_trust_resolver, manifest=None):
    gate = _gate(tmp_path, executor=executor, transport=transport, auto_fire=auto_fire, manifest=manifest,
                 binding_risk=risk_resolver)
    return ChainExecutor(
        gate=gate, request_builder=_request_builder,
        trust_resolver=trust_resolver, clock=lambda: FIXED, binding_store=binding_store,
    ), gate


def _run(tmp_path, chain, b, event=EVENT):
    """Execute ``b`` on ``event`` as the dispatcher does: the binding is in the registry and the run is
    admitted (a one-shot's admission claims it), then the chain walks."""
    reg = _registry(tmp_path)
    if reg.get(b.binding_id) is None:
        reg.add(b)
    assert reg.admit(b.binding_id, run_id_for(b.binding_id, event)) is not None
    return chain.execute(b, event)


# =================================================================================================
# Block 1 — ChainState seal
# =================================================================================================
def test_chainstate_seal_round_trips():
    b = _two_link_binding()
    s = ChainState.create(created_at="2026-06-30T12:00:00", binding=b, trigger_event=EVENT,
                          completed=(CompletedLink.of(0, b.goal[0], "p0", ExecutionResult(ok=True, downstream_id="d0")),),
                          paused_at_link=1, paused_payload="email-body", pending_id="pend-1")
    assert s.seal_matches()
    assert ChainState.from_dict(s.to_dict()).seal_matches()


def test_chainstate_seal_detects_tamper():
    b = _two_link_binding()
    s = ChainState.create(created_at="2026-06-30T12:00:00", binding=b, trigger_event=EVENT,
                          completed=(), paused_at_link=1, paused_payload="email-body", pending_id="pend-1")
    tampered = ChainState.from_dict({**s.to_dict(), "paused_payload": "INJECTED"})
    assert not tampered.seal_matches()  # the id no longer matches the (altered) content


# =================================================================================================
# Block 3 — ChainExecutor.execute
# =================================================================================================
def test_chain_fires_link0_pauses_at_link1_confirm(tmp_path):
    ex = RecordingExecutor()
    tr = FakeTransport()
    chain, gate = _executor(tmp_path, transport=tr, executor=ex)
    out = _run(tmp_path, chain, _two_link_binding())
    assert out.paused and out.paused_at == 1
    # link 0 fired (on-loop), link 1 paused (confirm) — only link 0 executed.
    assert [c[0] for c in ex.calls] == ["link0"]
    assert len(tr.proposals) == 1  # link 1 proposed
    # the chain state went into link 1's hold, with link 0's output recorded
    saved = ChainState.from_dict(gate.journal.find_pending(out.pending_id)["chain"])
    assert saved.chain_id == out.chain_id and saved.paused_at_link == 1
    assert saved.completed_links()[0].downstream_id == "out::link0"


def test_inter_link_data_flow_threads_forward(tmp_path):
    """Link 1's payload (built at propose time) carries link 0's downstream_id — the data-flow."""
    tr = FakeTransport()
    chain, gate = _executor(tmp_path, transport=tr, executor=RecordingExecutor())
    out = _run(tmp_path, chain, _two_link_binding())
    assert tr.proposals[0].action_name == "link1"
    assert gate.get_pending(out.pending_id).payload == "link1|upstream=1|from=out::link0"


def test_terminal_floor_honors_requested_upgrade(tmp_path):
    """Seam #2 (the terminal-link floor, replacing the old spurious pre-flight bar): a chain whose links
    NATURALLY resolve on-loop but is sealed at CONFIRM (a requested-posture UPGRADE — the compiler seals
    max(resolved, requested)) is NOT barred; the TERMINAL link is floored at CONFIRM and PAUSES there,
    honoring the upgrade. (The old behavior barred it spuriously — L2/codex MED.)"""
    ex = RecordingExecutor()
    tr = FakeTransport()
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="step a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="step b", tools=("doc.append",), output="doc:b")),
        tightness=TIGHT, posture=Posture.CONFIRM, one_shot=False, status=BindingStatus.ACTIVE,
        guard=(_kill_guard(),),  # confirm-class needs a sealed-floor kill
    )
    chain, _g = _executor(tmp_path, transport=tr, executor=ex,
                          risk_resolver=lambda binding, i: LOW_INTERNAL,
                          trust_resolver=lambda binding, i: TrustContext(  # → natural ON_LOOP per link
                              signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                              hops=0, human_present=False))
    out = _run(tmp_path, chain, b)
    assert out.paused and out.paused_at == 1
    assert out.links[1].outcome.posture is Posture.CONFIRM  # the terminal floor, NOT the natural ON_LOOP
    assert [c[0] for c in ex.calls] == ["link0"]  # link 0 fired on-loop; the terminal paused, not barred


def test_unclassifiable_link_bars_chain_preflight(tmp_path):
    """A link with an unclassifiable risk (an undeclared tool → the resolver raises) BARS the whole chain
    before any link fires (fail-closed)."""
    ex = RecordingExecutor()

    def _raising_risk(binding, i):
        if i == 1:
            raise KeyError("undeclared_tool")  # the resolver can't classify link 1
        return LOW_INTERNAL

    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=ex, risk_resolver=_raising_risk)
    out = _run(tmp_path, chain, _two_link_binding())
    assert out.aborted and out.reason.startswith("unclassifiable_link_1")
    assert ex.calls == []  # nothing fired (pre-flight bar)


def test_kill_aborts_chain_at_link0(tmp_path):
    """A known-danger kill on the IMMUTABLE trigger event aborts the chain at the first link — no fire."""
    ex = RecordingExecutor()
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    spoof = {"type": "email", "id": "evt-bad", "fields": {"from": "x@y", "dmarc": "fail", "subject": "s"}}
    out = _run(tmp_path, chain, _two_link_binding(), spoof)
    assert out.aborted and out.links[0].outcome.killed
    assert ex.calls == []  # the kill preempted link 0


def _on_loop_chain(*, one_shot=False):
    return Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="step a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="step b", tools=("doc.append",), output="doc:b")),
        tightness=TIGHT, posture=Posture.ON_LOOP, one_shot=one_shot, status=BindingStatus.ACTIVE, guard=(),
    )


def _on_loop_executor(tmp_path, reg=None, *, executor=None):
    # every link's tools are low-risk, and the manifest agrees (a manifest that declared a link higher
    # would raise its rung at propose, as the resolve would)
    return _executor(tmp_path, executor=executor or RecordingExecutor(), binding_store=reg,
                     manifest=ActionManifest({"link0": LOW_INTERNAL, "link1": LOW_INTERNAL}),
                     risk_resolver=lambda b, i: LOW_INTERNAL,
                     trust_resolver=lambda b, i: TrustContext(signal_auth=SignalAuth.STRONG,
                                                              intent_provenance=IntentProvenance.INTENT_FREE,
                                                              hops=0, human_present=False))[0]


def test_all_on_loop_chain_completes_end_to_end(tmp_path):
    """A chain whose links ALL resolve on-loop runs end-to-end (no pause)."""
    ex = RecordingExecutor()
    out = _run(tmp_path, _on_loop_executor(tmp_path, executor=ex), _on_loop_chain())
    assert out.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1"]


def test_a_chain_whose_run_was_never_admitted_fires_nothing(tmp_path):
    ex = RecordingExecutor()
    chain = _on_loop_executor(tmp_path, executor=ex)
    out = chain.execute(_on_loop_chain(), EVENT)               # no admission: not the dispatcher's path
    assert out.aborted and out.reason == "run_not_admitted" and ex.calls == []


def test_a_chain_executor_needs_a_journaled_gate(tmp_path):
    bare = EfferentGate(confirm_signers=confirm_signers(), manifest=_RESOLVE_MANIFEST, store=GateReceiptStore(tmp_path / "r.jsonl"),
                        executor=RecordingExecutor(), clock=lambda: FIXED)
    with pytest.raises(ValueError, match="run journal"):
        ChainExecutor(gate=bare, request_builder=_request_builder,
                      trust_resolver=_trust_resolver, clock=lambda: FIXED)


# =================================================================================================
# Block 4 — ChainExecutor.resume
# =================================================================================================
def test_resume_approve_fires_paused_link_and_completes(tmp_path):
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    paused = _run(tmp_path, chain, _two_link_binding())
    assert paused.paused
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.completed
    # link 1 fired on resume (link 0 already fired at execute) → both executed exactly once.
    assert [c[0] for c in ex.calls] == ["link0", "link1"]
    assert gate.open_pendings() == []  # the decision is made
    chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))   # a second reply
    assert [c[0] for c in ex.calls] == ["link0", "link1"]                         # runs nothing again


def test_resume_deny_ends_chain_without_firing(tmp_path):
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    paused = _run(tmp_path, chain, _two_link_binding())
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=False, by="human"))
    assert out is not None and out.aborted
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 NEVER fired (denied)


def test_resume_unknown_pending_returns_none(tmp_path):
    chain, _g = _executor(tmp_path, transport=FakeTransport())
    assert chain.resume("not-a-chain-pending", signed_yes(chain.gate, "not-a-chain-pending")) is None


def test_resume_integrity_mismatch_rejects(tmp_path):
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    paused = _run(tmp_path, chain, _two_link_binding())
    # alter the continuation in the hold on disk (stale id → seal mismatch)
    rewrite_holds(gate.journal, lambda h: dict(h, chain=dict(h["chain"], paused_payload="INJECTED"))
                  if h["chain"] else h)
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.aborted and out.reason == "integrity:seal_mismatch"
    assert [c[0] for c in ex.calls] == ["link0"] and gate.open_pendings() == []   # rejected, never fired


def test_a_revoke_mid_chain_fences_the_resume(tmp_path):
    """A human REVOKING a STANDING binding mid-chain stops the paused link: the revoke fences the run,
    and the decision cannot fire it afterwards, through the chain or plainly."""
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    b = _two_link_binding(one_shot=False)
    paused = _run(tmp_path, chain, b)
    assert paused.paused
    _registry(tmp_path).set_status(b.binding_id, BindingStatus.REVOKED)  # the human pulls the grant
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.aborted and out.reason == "journal:fenced"
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 never fired
    after = gate.resolve(paused.pending_id, signed_yes(gate, paused.pending_id))
    assert not after.fired


def test_a_removed_grant_fences_the_resume(tmp_path):
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    b = _two_link_binding(one_shot=False)
    paused = _run(tmp_path, chain, b)
    _registry(tmp_path).remove(b.binding_id)  # hard-delete the standing grant while the chain is paused
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.aborted
    assert [c[0] for c in ex.calls] == ["link0"]


def test_an_unreadable_continuation_rejects_and_never_fires(tmp_path):
    """A continuation that cannot be read (a required key gone) rejects the hold: the link does not
    fire, and the decision is made, so nothing can fire it later."""
    ex = RecordingExecutor()
    chain, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    paused = _run(tmp_path, chain, _two_link_binding())

    def drop_binding(h):
        if h["chain"]:
            h = dict(h, chain={k: v for k, v in h["chain"].items() if k != "binding"})
        return h

    rewrite_holds(gate.journal, drop_binding)
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.aborted and out.reason.startswith("chain_state_malformed")
    assert [c[0] for c in ex.calls] == ["link0"]
    after = gate.resolve(paused.pending_id, signed_yes(gate, paused.pending_id), chain_owned=True)
    assert not after.fired


def test_validate_against_binding_catches_tampered_index(tmp_path):
    """A SEALED-but-semantically-invalid chain state (paused_at_link out of range / completed mismatch)
    is caught by the semantic check (codex LOW/MED)."""
    b = _two_link_binding()
    # a self-consistent sealed state whose paused_at_link is out of range for the 2-link binding.
    bad = ChainState.create(created_at="2026-06-30T12:00:00", binding=b, trigger_event=EVENT,
                            completed=(), paused_at_link=5, paused_payload="p", pending_id="pend-x")
    assert bad.seal_matches()  # the SHAPE seal holds...
    assert bad.validate_against_binding(b) is not None  # ...but the SEMANTIC check catches it


def test_three_link_chain_multiple_pauses_and_resumed_data_flow(tmp_path):
    """A 3-link chain pauses TWICE (link 1 cooling-off, link 2 terminal-confirm), and the data-flow
    threads through a RESUMED link into the next (L1's untested gap): link 2's payload carries link 1's
    output (built at link 2's propose, during the link-1 resume continuation)."""
    ex = RecordingExecutor()
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="link a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="link b", tools=("doc.append",), output="doc:b"),
              SubGoal(goal="link c", tools=("inbox.write",), output="doc:c")),
        tightness=TIGHT, posture=Posture.CONFIRM, one_shot=False, status=BindingStatus.ACTIVE,
        guard=(_kill_guard(),))
    chain, gate = _executor(
        tmp_path, transport=FakeTransport(), executor=ex,
        manifest=ActionManifest({"link0": LOW_INTERNAL, "link1": LOW_INTERNAL, "link2": LOW_INTERNAL}),
        risk_resolver=lambda binding, i: LOW_INTERNAL,
        trust_resolver=lambda binding, i: TrustContext(  # hops=i → link0 ON_LOOP, link1 COOLING_OFF, link2 CONFIRM
            signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
            hops=i, human_present=False))

    out = _run(tmp_path, chain, b)
    assert out.paused and out.paused_at == 1  # link 0 fired on-loop; link 1 cooling-off paused
    assert [c[0] for c in ex.calls] == ["link0"]

    out2 = chain.resume(out.pending_id, signed_yes(chain.gate, out.pending_id))
    assert out2 is not None and out2.paused and out2.paused_at == 2  # link 1 fired → link 2 confirm paused
    assert [c[0] for c in ex.calls] == ["link0", "link1"]
    # the data-flow: link 2's proposed payload carries link 1's output (built during the link-1 resume).
    link2_pending = gate.get_pending(out2.pending_id)
    assert "upstream=2" in link2_pending.payload and "from=out::link1" in link2_pending.payload

    out3 = chain.resume(out2.pending_id, signed_yes(chain.gate, out2.pending_id))
    assert out3 is not None and out3.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1", "link2"]


def test_a_claimed_one_shot_resumes_its_own_run(tmp_path):
    """A ONE-SHOT is REVOKED by its claim at admission; the registry still grants the ONE run it was
    claimed for, so its paused chain resumes and completes."""
    ex = RecordingExecutor()
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    b = _two_link_binding(one_shot=True)
    paused = _run(tmp_path, chain, b)                     # the admission claims (revokes) the one-shot
    assert _registry(tmp_path).get(b.binding_id).status is BindingStatus.REVOKED
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out is not None and out.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1"]


# =================================================================================================
# Block 5 — FireDispatcher delegation
# =================================================================================================
def _dispatcher(tmp_path, gate, chain_exec):
    return FireDispatcher(
        store=_registry(tmp_path), gate=gate,
        predicate_match=lambda pattern, event: True,  # the registry binding matches
        request_builder=lambda binding, event: _request_builder(binding, ChainContext(trigger_event=event), 0),
        clock=lambda: FIXED, chain_executor=chain_exec,
    )


def test_dispatcher_routes_multilink_to_chain_executor(tmp_path):
    registry = _registry(tmp_path)
    b = _two_link_binding(status=BindingStatus.PAUSED)
    registry.add(b)
    registry.ratify(b.binding_id)
    ex = RecordingExecutor()
    chain_exec, gate = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    results = _dispatcher(tmp_path, gate, chain_exec).dispatch(EVENT)
    assert len(results) == 1
    assert results[0].chain is not None and results[0].chain.paused  # the chain paused at link 1
    assert [c[0] for c in ex.calls] == ["link0"]


def test_dispatcher_skips_multilink_when_no_chain_executor(tmp_path):
    """A 4a-only dispatcher (no chain executor) SKIPS a multi-link binding (never half-fires; the
    one-shot is NOT spent — the skip is pre-claim)."""
    registry = _registry(tmp_path)
    b = _two_link_binding(status=BindingStatus.PAUSED, one_shot=True)
    registry.add(b)
    registry.ratify(b.binding_id)
    assert _dispatcher(tmp_path, _gate(tmp_path), None).dispatch(EVENT) == []
    # the one-shot was NOT claimed (still ACTIVE) — the skip happened before the claim.
    assert registry.get(b.binding_id).status is BindingStatus.ACTIVE


# =================================================================================================
# Block N — Slice 4c: the chain records §2.6 graduation evidence on COMPLETION (immediate + resume).
# This closes the carried 4a/4b out-of-band record_fire gap: a chain that completes via sweep/resume
# now accrues evidence (the ChainExecutor is the single locus, the dispatcher no longer double-counts).
# =================================================================================================
def test_completed_chain_records_graduation_evidence_immediate(tmp_path):
    reg = _registry(tmp_path)
    b = _on_loop_chain()
    reg.add(b)
    out = _run(tmp_path, _on_loop_executor(tmp_path, reg), b)
    assert out.completed
    after = reg.get(b.binding_id)
    assert after.graduation.fire_count == 1 and after.graduation.clean_count == 1
    assert after.graduation.last_fired_at == FIXED.isoformat()


def test_completed_chain_records_on_resume_out_of_band(tmp_path):
    # the carried gap: a chain that PAUSES at execute and completes later via resume records exactly once.
    reg = _registry(tmp_path)
    b = _two_link_binding()
    reg.add(b)
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=RecordingExecutor(), binding_store=reg)
    paused = _run(tmp_path, chain, b)
    assert paused.paused
    assert reg.get(b.binding_id).graduation.fire_count == 0  # nothing recorded at the pause
    out = chain.resume(paused.pending_id, signed_yes(chain.gate, paused.pending_id))
    assert out.completed
    after = reg.get(b.binding_id)
    assert after.graduation.fire_count == 1 and after.graduation.clean_count == 1


def test_one_shot_chain_does_not_record(tmp_path):
    reg = _registry(tmp_path)
    b = _on_loop_chain(one_shot=True)
    reg.add(b)
    out = _run(tmp_path, _on_loop_executor(tmp_path, reg), b)
    assert out.completed
    assert reg.get(b.binding_id).graduation.fire_count == 0  # one-shots never graduate


def test_bare_executor_no_binding_store_does_not_crash(tmp_path):
    # no binding_store wired → no record, no crash; the chain still completes.
    assert _run(tmp_path, _on_loop_executor(tmp_path, None), _on_loop_chain()).completed


def test_aborted_chain_does_not_record(tmp_path):
    reg = _registry(tmp_path)
    b = _two_link_binding()
    reg.add(b)
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=RecordingExecutor(), binding_store=reg)
    paused = _run(tmp_path, chain, b)
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=False, by="human"))  # DENY → abort
    assert out.aborted
    assert reg.get(b.binding_id).graduation.fire_count == 0  # never completed → no evidence


def test_dispatcher_chain_completion_records_once_no_double_count(tmp_path):
    """A chain completing through the FULL FireDispatcher records EXACTLY ONCE (the ChainExecutor on
    completion); the dispatcher no longer records (it would double-count)."""
    registry = _registry(tmp_path)
    b = _on_loop_chain()
    registry.add(b)
    chain_exec = _on_loop_executor(tmp_path, registry)
    results = _dispatcher(tmp_path, chain_exec.gate, chain_exec).dispatch(EVENT)
    assert len(results) == 1 and results[0].chain.completed
    assert registry.get(b.binding_id).graduation.fire_count == 1  # once, not twice
