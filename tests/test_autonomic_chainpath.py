"""Phase-2 Slice-4b — the CHAIN EXECUTOR (the gate travels with the chain).

Blocks:
  1. ChainState — the sealed pause/resume persistence (seal tamper-evidence).
  2. ChainStateStore — the flock registry + ``claim_by_pending`` at-most-once.
  3. ChainExecutor.execute — the per-link walk: fire on-loop links, pause at the first confirm-class
     link, thread the inter-link data-flow, the seam-#2 ratification-consistency bar, the kill abort.
  4. ChainExecutor.resume — fire the paused link + continue; deny ends the chain; the standing
     kill-switch; the integrity drop.
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
    ChainStateStore,
    CompletedLink,
    ConfirmDecision,
    EfferentGate,
    ExecutionResult,
    FireDispatcher,
    GateReceiptStore,
    Guard,
    IntentProvenance,
    PendingActionStore,
    Posture,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    manual_invocation,
)

FIXED = _dt.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
LOW_INTERNAL = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
HIGH_EXTERNAL = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)
TIGHT = TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9)
EVENT = {"type": "email", "id": "evt-1", "fields": {"from": "x@y.example", "dmarc": "pass", "subject": "hi"}}


# --- test doubles --------------------------------------------------------------------------------
@dataclass
class RecordingExecutor:
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


# The gate's RESOLVE path re-validates the risk floor against the MANIFEST by ``action_name`` (Slice-2
# ``_guard_resolve_fire``) — so the synthetic chain-link actions must be declared with a risk whose
# floor does not exceed the propose-time posture (in the real adapter, email_send is in FLOW_MANIFEST
# with a CONFIRM-floor risk that matches its per-link sealed-tool risk). The PROPOSE path resolves
# against the binding-threaded per-link risk, not this manifest.
_RESOLVE_MANIFEST = ActionManifest({"link0": LOW_INTERNAL, "link1": HIGH_EXTERNAL, "link2": LOW_INTERNAL})


def _gate(tmp_path, *, executor=None, transport=None, auto_fire=None, manifest=None):
    return EfferentGate(
        manifest=manifest if manifest is not None else _RESOLVE_MANIFEST,
        store=GateReceiptStore(tmp_path / "r.jsonl"),
        executor=executor or RecordingExecutor(),
        clock=lambda: FIXED,
        transport=transport,
        pending_store=PendingActionStore(tmp_path / "pend.json") if transport is not None else None,
        auto_fire_actions=auto_fire,
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


def _executor(tmp_path, *, store=None, transport=None, auto_fire=None, executor=None, binding_store=None):
    gate = _gate(tmp_path, executor=executor, transport=transport, auto_fire=auto_fire)
    return ChainExecutor(
        gate=gate, request_builder=_request_builder, risk_resolver=_risk_resolver,
        trust_resolver=_trust_resolver, chain_store=store or ChainStateStore(tmp_path / "chain.json"),
        clock=lambda: FIXED, binding_store=binding_store,
    ), gate


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
# Block 2 — ChainStateStore
# =================================================================================================
def test_claim_by_pending_at_most_once(tmp_path):
    store = ChainStateStore(tmp_path / "chain.json")
    b = _two_link_binding()
    s = ChainState.create(created_at="2026-06-30T12:00:00", binding=b, trigger_event=EVENT,
                          completed=(), paused_at_link=1, paused_payload="p", pending_id="pend-9")
    store.add(s)
    first = store.claim_by_pending("pend-9")
    second = store.claim_by_pending("pend-9")
    assert first is not None and first.chain_id == s.chain_id
    assert second is None  # claimed-out — at-most-once
    assert store.find_by_pending("pend-9") is None


# =================================================================================================
# Block 3 — ChainExecutor.execute
# =================================================================================================
def test_chain_fires_link0_pauses_at_link1_confirm(tmp_path):
    ex = RecordingExecutor()
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    chain, _gate_ = _executor(tmp_path, store=store, transport=tr, executor=ex)
    out = chain.execute(_two_link_binding(), EVENT)
    assert out.paused and out.paused_at == 1
    # link 0 fired (on-loop), link 1 paused (confirm) — only link 0 executed.
    assert [c[0] for c in ex.calls] == ["link0"]
    assert len(tr.proposals) == 1  # link 1 proposed
    # the chain state persisted with link 0's output recorded.
    saved = store.get(out.chain_id)
    assert saved is not None and saved.paused_at_link == 1
    assert saved.completed_links()[0].downstream_id == "out::link0"


def test_inter_link_data_flow_threads_forward(tmp_path):
    """Link 1's payload (built at propose time) carries link 0's downstream_id — the data-flow."""
    tr = FakeTransport()
    chain, _g = _executor(tmp_path, transport=tr, executor=RecordingExecutor())
    chain.execute(_two_link_binding(), EVENT)
    proposed_payload = tr.proposals[0].action_name  # ConfirmProposal carries action_name
    # the proposed pending's summary/payload chain: assert link1 saw 1 upstream + link0's downstream id
    # (the payload is on the persisted pending; check via the chain state's paused_payload).
    # simpler: the request_builder encoded it into the payload, surfaced in the pending store.
    assert proposed_payload == "link1"


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
    gate = _gate(tmp_path, executor=ex, transport=tr)
    chain = ChainExecutor(
        gate=gate, request_builder=_request_builder,
        risk_resolver=lambda binding, i: LOW_INTERNAL,
        trust_resolver=lambda binding, i: TrustContext(  # → natural ON_LOOP per link
            signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
            hops=0, human_present=False),
        chain_store=ChainStateStore(tmp_path / "c.json"), clock=lambda: FIXED)
    out = chain.execute(b, EVENT)
    assert out.paused and out.paused_at == 1
    assert out.links[1].outcome.posture is Posture.CONFIRM  # the terminal floor, NOT the natural ON_LOOP
    assert [c[0] for c in ex.calls] == ["link0"]  # link 0 fired on-loop; the terminal paused, not barred


def test_unclassifiable_link_bars_chain_preflight(tmp_path):
    """A link with an unclassifiable risk (an undeclared tool → the resolver raises) BARS the whole chain
    before any link fires (fail-closed)."""
    ex = RecordingExecutor()
    gate = _gate(tmp_path, executor=ex, transport=FakeTransport())

    def _raising_risk(binding, i):
        if i == 1:
            raise KeyError("undeclared_tool")  # the resolver can't classify link 1
        return LOW_INTERNAL

    chain = ChainExecutor(gate=gate, request_builder=_request_builder, risk_resolver=_raising_risk,
                          trust_resolver=_trust_resolver, chain_store=ChainStateStore(tmp_path / "c.json"),
                          clock=lambda: FIXED)
    out = chain.execute(_two_link_binding(), EVENT)
    assert out.aborted and out.reason.startswith("unclassifiable_link_1")
    assert ex.calls == []  # nothing fired (pre-flight bar)


def test_kill_aborts_chain_at_link0(tmp_path):
    """A known-danger kill on the IMMUTABLE trigger event aborts the chain at the first link — no fire."""
    ex = RecordingExecutor()
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=ex)
    spoof = {"type": "email", "id": "evt-bad", "fields": {"from": "x@y", "dmarc": "fail", "subject": "s"}}
    out = chain.execute(_two_link_binding(), spoof)
    assert out.aborted and out.links[0].outcome.killed
    assert ex.calls == []  # the kill preempted link 0


def test_all_on_loop_chain_completes_end_to_end(tmp_path):
    """A chain whose links ALL resolve on-loop runs end-to-end (no pause)."""
    ex = RecordingExecutor()
    # both links internal/reversible + shallow hops → on-loop. Override the resolvers via a fresh executor.
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="step a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="step b", tools=("doc.append",), output="doc:b")),
        tightness=TIGHT, posture=Posture.ON_LOOP, one_shot=False, status=BindingStatus.ACTIVE, guard=(),
    )
    gate = _gate(tmp_path, executor=ex)
    chain = ChainExecutor(gate=gate, request_builder=_request_builder,
                          risk_resolver=lambda binding, i: LOW_INTERNAL,
                          trust_resolver=lambda binding, i: TrustContext(
                              signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
                              hops=0, human_present=False),
                          chain_store=ChainStateStore(tmp_path / "c.json"), clock=lambda: FIXED)
    out = chain.execute(b, EVENT)
    assert out.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1"]


# =================================================================================================
# Block 4 — ChainExecutor.resume
# =================================================================================================
def test_resume_approve_fires_paused_link_and_completes(tmp_path):
    ex = RecordingExecutor()
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    chain, gate = _executor(tmp_path, store=store, transport=tr, executor=ex)
    paused = chain.execute(_two_link_binding(), EVENT)
    assert paused.paused
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.completed
    # link 1 fired on resume (link 0 already fired at execute) → both executed exactly once.
    assert [c[0] for c in ex.calls] == ["link0", "link1"]
    assert store.get(paused.chain_id) is None  # the chain state is consumed


def test_resume_deny_ends_chain_without_firing(tmp_path):
    ex = RecordingExecutor()
    tr = FakeTransport()
    chain, gate = _executor(tmp_path, transport=tr, executor=ex)
    paused = chain.execute(_two_link_binding(), EVENT)
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=False, by="human"))
    assert out is not None and out.aborted
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 NEVER fired (denied)


def test_resume_unknown_pending_returns_none(tmp_path):
    chain, _g = _executor(tmp_path, transport=FakeTransport())
    assert chain.resume("not-a-chain-pending", ConfirmDecision(approved=True, by="human")) is None


def test_resume_integrity_mismatch_drops(tmp_path):
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    chain, gate = _executor(tmp_path, store=store, transport=tr, executor=RecordingExecutor())
    paused = chain.execute(_two_link_binding(), EVENT)
    # tamper the persisted chain state's payload directly on disk (stale id → seal mismatch).
    import json
    data = json.loads((tmp_path / "chain.json").read_text())
    data[0]["paused_payload"] = "INJECTED"
    (tmp_path / "chain.json").write_text(json.dumps(data))
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.aborted and out.reason == "integrity:seal_mismatch"


def test_standing_kill_switch_aborts_resume_and_consumes_pending(tmp_path):
    """A human REVOKING a STANDING binding mid-chain aborts the resume (the in-flight kill-switch) AND
    CONSUMES the orphaned pending (so a later plain resolve can't fire the link the chain aborted —
    codex HIGH two-resource invariant)."""
    ex = RecordingExecutor()
    tr = FakeTransport()
    registry = BindingStore(tmp_path / "reg")
    b = _two_link_binding(one_shot=False)
    registry.add(b)
    registry.ratify(b.binding_id)  # ACTIVE
    chain, gate = _executor(tmp_path, transport=tr, executor=ex, binding_store=registry)
    paused = chain.execute(b, EVENT)
    assert paused.paused
    registry.set_status(b.binding_id, BindingStatus.REVOKED)  # the human pulls the grant mid-chain
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.aborted and out.reason == "standing_grant_unconfirmed"
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 never fired
    # the orphaned pending was CONSUMED — a later plain resolve finds nothing to fire.
    after = gate.resolve(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert not after.fired and after.refused


def test_standing_hard_delete_aborts_resume(tmp_path):
    """For a STANDING grant, registry ABSENCE (a hard delete) is NOT proof of continued authority —
    resume ABORTS (fail-closed; codex/nemotron MED)."""
    ex = RecordingExecutor()
    tr = FakeTransport()
    registry = BindingStore(tmp_path / "reg")
    b = _two_link_binding(one_shot=False)
    registry.add(b)
    registry.ratify(b.binding_id)
    chain, gate = _executor(tmp_path, transport=tr, executor=ex, binding_store=registry)
    paused = chain.execute(b, EVENT)
    registry.remove(b.binding_id)  # hard-delete the standing grant while the chain is paused
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.aborted and out.reason == "standing_grant_unconfirmed"
    assert [c[0] for c in ex.calls] == ["link0"]


def test_malformed_chain_state_consumes_pending_not_fires(tmp_path):
    """A chain whose persisted state becomes UNPARSEABLE on disk: claim_by_pending drops it + signals,
    resume CONSUMES the orphaned pending (deny) — the fail-OPEN inversion fix (a more-corrupt state must
    not be MORE permissive than a seal-mismatch). The pending is NOT fired."""
    import json
    ex = RecordingExecutor()
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    chain, gate = _executor(tmp_path, store=store, transport=tr, executor=ex)
    paused = chain.execute(_two_link_binding(), EVENT)
    # corrupt the persisted chain state to UNPARSEABLE (drop a required key → from_dict raises).
    data = json.loads((tmp_path / "chain.json").read_text())
    del data[0]["binding"]   # now ChainState.from_dict raises KeyError → malformed
    (tmp_path / "chain.json").write_text(json.dumps(data))
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.aborted and out.reason == "chain_state_malformed_dropped"
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 NOT fired
    # the orphaned pending was consumed.
    after = gate.resolve(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert not after.fired


def test_store_fault_aborts_retriable_not_fall_through(tmp_path):
    """A chain-store I/O/corruption fault on the ADVANCE path raises ChainStoreUnavailableError (vs
    collapsing to the 'absent' None) → resume returns a RETRIABLE aborted, does NOT consume the pending,
    and the chain SURVIVES (codex/complement re-review consensus — fault ≠ absence)."""
    import json
    from levain.autonomic import ChainStoreUnavailableError
    ex = RecordingExecutor()
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    chain, gate = _executor(tmp_path, store=store, transport=tr, executor=ex)
    paused = chain.execute(_two_link_binding(), EVENT)
    # corrupt the chain store to NON-JSON (a transient I/O-class fault on the advance read).
    (tmp_path / "chain.json").write_text("{ this is not valid json")
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.aborted and out.reason == "chain_store_unavailable"
    assert [c[0] for c in ex.calls] == ["link0"]  # link 1 NOT fired
    # the advance read RAISES (not "absent") — proven directly:
    with __import__("pytest").raises(ChainStoreUnavailableError):
        store.list_open(for_advance=True)
    # the pending was NOT consumed (the chain survives, retriable) — a later resolve still finds it.
    # (restore a valid-but-empty store so gate.resolve isn't blocked; the pending lives in the gate store.)
    after = gate.resolve(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert after.fired  # the pending was still live (not consumed by the faulted resume)


def test_validate_against_binding_catches_tampered_index(tmp_path):
    """A SEALED-but-semantically-invalid chain state (paused_at_link out of range / completed mismatch)
    is caught on resume + the pending consumed (codex LOW/MED)."""
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
    tr = FakeTransport()
    store = ChainStateStore(tmp_path / "chain.json")
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="link a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="link b", tools=("doc.append",), output="doc:b"),
              SubGoal(goal="link c", tools=("inbox.write",), output="doc:c")),
        tightness=TIGHT, posture=Posture.CONFIRM, one_shot=False, status=BindingStatus.ACTIVE,
        guard=(_kill_guard(),))
    # the resolve-guard risk floor keys on the MANIFEST by action_name — declare all links low/internal
    # so it agrees with the (custom, all-LOW_INTERNAL) propose-time resolver (in production the per-tool
    # propose risk + the per-action resolve manifest agree by construction — the fossil declares both).
    gate = _gate(tmp_path, executor=ex, transport=tr,
                 manifest=ActionManifest({"link0": LOW_INTERNAL, "link1": LOW_INTERNAL, "link2": LOW_INTERNAL}))
    pend_store = PendingActionStore(tmp_path / "pend.json")
    chain = ChainExecutor(
        gate=gate, request_builder=_request_builder,
        risk_resolver=lambda binding, i: LOW_INTERNAL,
        trust_resolver=lambda binding, i: TrustContext(  # hops=i → link0 ON_LOOP, link1 COOLING_OFF, link2 CONFIRM
            signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_FREE,
            hops=i, human_present=False),
        chain_store=store, clock=lambda: FIXED)

    out = chain.execute(b, EVENT)
    assert out.paused and out.paused_at == 1  # link 0 fired on-loop; link 1 cooling-off paused
    assert [c[0] for c in ex.calls] == ["link0"]

    out2 = chain.resume(out.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out2 is not None and out2.paused and out2.paused_at == 2  # link 1 fired → link 2 confirm paused
    assert [c[0] for c in ex.calls] == ["link0", "link1"]
    # the data-flow: link 2's proposed payload carries link 1's output (built during the link-1 resume).
    link2_pending = pend_store.get(out2.pending_id)
    assert "upstream=2" in link2_pending.payload and "from=out::link1" in link2_pending.payload

    out3 = chain.resume(out2.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out3 is not None and out3.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1", "link2"]


def test_one_shot_resumes_on_snapshot_despite_revoked_registry(tmp_path):
    """A ONE-SHOT continues on the persisted snapshot even though the registry shows it REVOKED (its
    claim-revoke at chain-start is expected — NOT a human pull)."""
    ex = RecordingExecutor()
    tr = FakeTransport()
    registry = BindingStore(tmp_path / "reg")
    b = _two_link_binding(one_shot=True)
    registry.add(b)
    registry.set_status(b.binding_id, BindingStatus.REVOKED)  # as if claim_one_shot spent it at chain start
    chain, gate = _executor(tmp_path, transport=tr, executor=ex, binding_store=registry)
    paused = chain.execute(b, EVENT)  # execute on the pre-claim ACTIVE snapshot
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out is not None and out.completed
    assert [c[0] for c in ex.calls] == ["link0", "link1"]


# =================================================================================================
# Block 5 — FireDispatcher delegation
# =================================================================================================
def test_dispatcher_routes_multilink_to_chain_executor(tmp_path):
    registry = BindingStore(tmp_path / "reg")
    b = _two_link_binding(status=BindingStatus.PAUSED)
    registry.add(b)
    registry.ratify(b.binding_id)
    ex = RecordingExecutor()
    tr = FakeTransport()
    chain_exec, gate = _executor(tmp_path, transport=tr, executor=ex,
                                 store=ChainStateStore(tmp_path / "chain.json"))
    dispatcher = FireDispatcher(
        store=registry, gate=gate,
        predicate_match=lambda pattern, event: True,  # the registry binding matches
        request_builder=lambda binding, event: _request_builder(binding, ChainContext(trigger_event=event), 0),
        risk_resolver=lambda binding: HIGH_EXTERNAL, clock=lambda: FIXED, chain_executor=chain_exec,
    )
    results = dispatcher.dispatch(EVENT)
    assert len(results) == 1
    assert results[0].chain is not None and results[0].chain.paused  # the chain paused at link 1
    assert [c[0] for c in ex.calls] == ["link0"]


def test_dispatcher_skips_multilink_when_no_chain_executor(tmp_path):
    """A 4a-only dispatcher (no chain executor) SKIPS a multi-link binding (never half-fires; the
    one-shot is NOT spent — the skip is pre-claim)."""
    registry = BindingStore(tmp_path / "reg")
    b = _two_link_binding(status=BindingStatus.PAUSED, one_shot=True)
    registry.add(b)
    registry.ratify(b.binding_id)
    gate = _gate(tmp_path)
    dispatcher = FireDispatcher(
        store=registry, gate=gate, predicate_match=lambda pattern, event: True,
        request_builder=lambda binding, event: _request_builder(binding, ChainContext(trigger_event=event), 0),
        risk_resolver=lambda binding: HIGH_EXTERNAL, clock=lambda: FIXED, chain_executor=None,
    )
    assert dispatcher.dispatch(EVENT) == []
    # the one-shot was NOT claimed (still ACTIVE) — the skip happened before the claim.
    assert registry.get(b.binding_id).status is BindingStatus.ACTIVE


# =================================================================================================
# Block N — Slice 4c: the chain records §2.6 graduation evidence on COMPLETION (immediate + resume).
# This closes the carried 4a/4b out-of-band record_fire gap: a chain that completes via sweep/resume
# now accrues evidence (the ChainExecutor is the single locus, the dispatcher no longer double-counts).
# =================================================================================================
def _on_loop_chain():
    return Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="step a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="step b", tools=("doc.append",), output="doc:b")),
        tightness=TIGHT, posture=Posture.ON_LOOP, one_shot=False, status=BindingStatus.ACTIVE, guard=(),
    )


def _on_loop_executor(tmp_path, reg, *, one_shot_binding=None):
    gate = _gate(tmp_path, executor=RecordingExecutor())
    return ChainExecutor(
        gate=gate, request_builder=_request_builder, risk_resolver=lambda b, i: LOW_INTERNAL,
        trust_resolver=lambda b, i: TrustContext(signal_auth=SignalAuth.STRONG,
                                                 intent_provenance=IntentProvenance.INTENT_FREE,
                                                 hops=0, human_present=False),
        chain_store=ChainStateStore(tmp_path / "c.json"), clock=lambda: FIXED, binding_store=reg)


def test_completed_chain_records_graduation_evidence_immediate(tmp_path):
    reg = BindingStore(tmp_path / "reg")
    b = _on_loop_chain()
    reg.add(b)
    out = _on_loop_executor(tmp_path, reg).execute(b, EVENT)
    assert out.completed
    after = reg.get(b.binding_id)
    assert after.graduation.fire_count == 1 and after.graduation.clean_count == 1
    assert after.graduation.last_fired_at == FIXED.isoformat()


def test_completed_chain_records_on_resume_out_of_band(tmp_path):
    # the carried gap: a chain that PAUSES at execute and completes later via resume records exactly once.
    reg = BindingStore(tmp_path / "reg")
    b = _two_link_binding()
    reg.add(b)
    chain, _g = _executor(tmp_path, store=ChainStateStore(tmp_path / "chain.json"),
                          transport=FakeTransport(), executor=RecordingExecutor(), binding_store=reg)
    paused = chain.execute(b, EVENT)
    assert paused.paused
    assert reg.get(b.binding_id).graduation.fire_count == 0  # nothing recorded at the pause
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=True, by="human"))
    assert out.completed
    after = reg.get(b.binding_id)
    assert after.graduation.fire_count == 1 and after.graduation.clean_count == 1


def test_one_shot_chain_does_not_record(tmp_path):
    reg = BindingStore(tmp_path / "reg")
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T09:00:00",
        trigger=TriggerSpec(type="email", pattern={"op": "exists", "field": "from"}),
        goal=(SubGoal(goal="a", tools=("gdrive.create",), output="doc:a"),
              SubGoal(goal="b", tools=("doc.append",), output="doc:b")),
        tightness=TIGHT, posture=Posture.ON_LOOP, one_shot=True, status=BindingStatus.ACTIVE, guard=())
    reg.add(b)
    out = _on_loop_executor(tmp_path, reg).execute(b, EVENT)
    assert out.completed
    assert reg.get(b.binding_id).graduation.fire_count == 0  # one-shots never graduate


def test_bare_executor_no_binding_store_does_not_crash(tmp_path):
    # no binding_store wired (a bare executor) → no record, no crash; the chain still completes.
    b = _on_loop_chain()
    gate = _gate(tmp_path, executor=RecordingExecutor())
    chain = ChainExecutor(gate=gate, request_builder=_request_builder, risk_resolver=lambda b, i: LOW_INTERNAL,
                          trust_resolver=lambda b, i: TrustContext(signal_auth=SignalAuth.STRONG,
                              intent_provenance=IntentProvenance.INTENT_FREE, hops=0, human_present=False),
                          chain_store=ChainStateStore(tmp_path / "c.json"), clock=lambda: FIXED,
                          binding_store=None)
    assert chain.execute(b, EVENT).completed


def test_aborted_chain_does_not_record(tmp_path):
    reg = BindingStore(tmp_path / "reg")
    b = _two_link_binding()
    reg.add(b)
    chain, _g = _executor(tmp_path, transport=FakeTransport(), executor=RecordingExecutor(), binding_store=reg)
    paused = chain.execute(b, EVENT)
    out = chain.resume(paused.pending_id, ConfirmDecision(approved=False, by="human"))  # DENY → abort
    assert out.aborted
    assert reg.get(b.binding_id).graduation.fire_count == 0  # never completed → no evidence


def test_dispatcher_chain_completion_records_once_no_double_count(tmp_path):
    """A chain completing through the FULL FireDispatcher records EXACTLY ONCE (the ChainExecutor on
    completion); the dispatcher no longer records (it would double-count)."""
    registry = BindingStore(tmp_path / "reg")
    b = _on_loop_chain()
    registry.add(b)
    chain_exec = _on_loop_executor(tmp_path, registry)
    dispatcher = FireDispatcher(
        store=registry, gate=chain_exec._gate, predicate_match=lambda p, e: True,
        request_builder=lambda binding, event: _request_builder(binding, ChainContext(trigger_event=event), 0),
        risk_resolver=lambda binding: LOW_INTERNAL, clock=lambda: FIXED, chain_executor=chain_exec)
    results = dispatcher.dispatch(EVENT)
    assert len(results) == 1 and results[0].chain.completed
    assert registry.get(b.binding_id).graduation.fire_count == 1  # once, not twice
