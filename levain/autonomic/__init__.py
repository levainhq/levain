"""levain.autonomic — the governed EFFERENT edge (the gate).

Folded into Levain on 2026-10-06 from the vagus repo's ``vagus/efferent/`` at vagus commit
7fdfddd (Phill: vagus "will no longer be Vagus and just be part of Levain itself"), the second
installment after ``levain.firing`` (2026-07-07). The modules were copied as they were, with
``vagus.efferent`` renamed to ``levain.autonomic``; the one afferent import (the receipt's
``build_action_face``) now lives in ``levain.autonomic.receipt``. Docstrings that say "the vagus"
mean this engine.

The active membrane's hard half: where an afferent proposal (or a bound trigger) becomes an action
on the world, and EVERY such action passes the posture gate. The gate may fire without a human only
on the two rungs that say so (``ABOVE_LOOP`` / ``ON_LOOP``, ``Posture.fires_immediately``), and only
where the unbuyable risk floor allows it. ``govern-not-trust`` compiled into a runtime invariant —
the augmentation thesis stops being positioning and becomes the binary's behavior. The gate's trace
is the action-face of the FROZEN DecisionInfluenceReceipt (the vagus is its PRODUCER).

Clean-layer, dogfood-discriminator-sorted out of flow's two-context-proven gate
(``email_classifier.gate_directive`` + ``approved_actions.json`` + C1b): the YEAST is here (the risk
manifest SCHEMA, the ``policy()`` resolver, the §1.5 refuse stack, the posture ladder, the
``Executor``/``ConfirmTransport`` protocols, later the binding compiler); flow's FOSSIL (the
concrete actions, the flowConnect wire, the specific manifest content) stays in flow's adapter.

The membrane (load-bearing — the anti-grab-bag guard):
  - It NEVER renders (the surface layer's job).
  - It decides HOW MUCH human an action needs; it does not decide that no human is needed by
    default. Which rungs exist and when each fires is ``levain.autonomic.posture``.
  - It is a LEAF: it imports the standard library and ``levain.autonomic`` only, never another
    Levain module, anneal or a control-plane surface; adapters inject what it needs. The
    anti-cycle rule; the dependency arrow stays down (asserted by ``tests/test_autonomic_leaf.py``).

Architecture:
  The governance CORE (pure, no I/O, no model):
  - ``Posture``                         — the 6-rung operator-involvement ladder (§2.5).
  - ``RiskClass`` / ``ActionRisk`` / ``ActionManifest`` — risk DECLARED-not-inferred, fail-closed.
  - ``SignalAuth`` / ``IntentProvenance`` / ``TrustContext`` / ``earned_posture`` — the trust axis.
  - ``screen`` (+ ``sane_confidence`` / ``substring_grounded`` / ``injection_flagged``) — the §1.5
    refuse stack (confidence + groundedness + injection), upstream of posture.
  - ``risk_floor`` / ``policy`` — ``posture = max(risk_floor, earned_posture)`` (floors unbuyable).
  The orchestration layer (the gated edge + its trace):
  - ``AuthorityScope`` / ``manual_invocation`` — the grant in force (the receipt's authority_scope).
  - ``Executor`` (Protocol) / ``ActionRequest`` / ``ExecutionResult`` — the trusted action seam.
  - ``ConfirmTransport`` (Protocol) / ``ConfirmProposal`` / ``ConfirmDecision`` — the confirm-rung
    propose→reply seam (the human gate, injected; the decision returns out-of-band via ``resolve``).
  - ``PendingAction`` / ``PendingActionStore`` — the durable pending-action queue (the confirm-map's
    record that survives the propose→reply gap).
  - ``build_gate_face`` / ``build_gate_verdict`` — FILL the frozen receipt's efferent fields.
  - ``GateReceiptStore`` / ``StoredGateReceipt`` — the append-only efferent decision trace.
  - ``EfferentGate`` / ``GateOutcome`` — the orchestrator (risk → §1.5 → policy → route → receipt;
    + ``resolve`` / ``sweep_timeouts`` for the confirm round-trip).
  The Slice-3a.5 guard hardening (what makes GRADUATION to on-loop safe):
  - ``kill`` (``kill_outcome`` / ``kill_trips`` / ``assert_kill_pure``) — the DIVERSE, fail-safe kill
    substrate (Kleene three-valued; absent/unevaluable → trip), the kill-purity gate, and the
    substrate the compiler's kill-drill gate verifies against.
  - ``monitor`` (``prediction_diverged`` / ``TrajectoryObserver``) — the runtime prediction-error
    monitor (the unknown-danger backstop; diffs actual-vs-``predicted_trajectory`` → ``killed``).
  - ``liveness`` (``binding_liveness`` / ``gate_liveness``) — binding + kill/gate telemetry.
  The run journal (S8):
  - ``RunJournal`` / ``RunRef`` — a binding fire's effects, each at most once: held while a decision on
    the binding is open, cancelled by a rejection, fenced by a pause/revoke/tighten, poisoned (never
    retried) when the outcome is unknown, replayed (never re-run) when delivered again. The journal is
    the only durable home of a journaled decision: open pendings and paused chains are read from it.
"""
from __future__ import annotations

from levain.autonomic.authority import AuthorityScope, manual_invocation
from levain.autonomic.compiler import (
    Backtester,
    CandidateBinding,
    CompileResult,
    CompileSurfacing,
    REFUSE_CONSTITUTIONAL,
    REFUSE_PRUDENTIAL,
    compile_binding,
    refuse_undecidable,
)
from levain.autonomic.binding import (
    Binding,
    BindingStatus,
    BindingStore,
    Graduation,
    Guard,
    PredicateValidator,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    FenceNotRecordedError,
    ReplaceResult,
    binding_invocation,
    derived_binding_id,
    seal_binding_id,
)
from levain.autonomic.chainpath import (
    ChainContext,
    ChainExecutor,
    ChainLinkResult,
    ChainOutcome,
    ChainState,
    ChainStateStore,
    ChainStoreUnavailableError,
    CompletedLink,
    MalformedChainStateError,
    seal_chain_id,
)
from levain.autonomic.executor import ActionRequest, ExecutionResult, Executor
from levain.autonomic.firepath import FireDispatch, FireDispatcher
from levain.autonomic.gate import EfferentGate, GateOutcome
from levain.autonomic.graduation import (
    DEEP_OUTBOUND_MIN_HOPS,
    DEFAULT_MIN_CLEAN_FIRES,
    GraduationProposal,
    is_depth_bounded,
    one_rung_looser,
    propose_graduation,
)
from levain.autonomic.kill import (
    Kleene,
    KillImpurityError,
    assert_kill_pure,
    kill_outcome,
    kill_purity_error,
    kill_trips,
)
from levain.autonomic.journal import (
    EffectOutcome,
    EffectStatus,
    HoldResult,
    JournalCorruptError,
    RunJournal,
    RunRef,
    effect_digest,
    hold_id_for,
    run_id_for,
)
from levain.autonomic.liveness import binding_liveness, gate_liveness
from levain.autonomic.monitor import (
    TrajectoryObserver,
    assert_trajectory_pure,
    bound_declared,
    prediction_diverged,
    trajectory_bound,
    within_envelope,
)
from levain.autonomic.gates import (
    DEFAULT_CONFIDENCE_FLOOR,
    RefuseVerdict,
    injection_flagged,
    sane_confidence,
    screen,
    substring_grounded,
)
from levain.autonomic.pending import PendingAction, PendingActionStore
from levain.autonomic.policy import policy, risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.receipt import build_gate_face, build_gate_verdict
from levain.autonomic.risk import ActionManifest, ActionRisk, RiskClass, UnknownAction
from levain.autonomic.store import GateReceiptStore, StoredGateReceipt
from levain.autonomic.transport import ConfirmDecision, ConfirmProposal, ConfirmTransport
from levain.autonomic.trust import (
    IntentProvenance,
    SignalAuth,
    TrustContext,
    earned_posture,
)

__all__ = [
    # posture
    "Posture",
    # risk
    "RiskClass",
    "ActionRisk",
    "ActionManifest",
    "UnknownAction",
    # trust
    "SignalAuth",
    "IntentProvenance",
    "TrustContext",
    "earned_posture",
    # gates (§1.5)
    "DEFAULT_CONFIDENCE_FLOOR",
    "RefuseVerdict",
    "sane_confidence",
    "substring_grounded",
    "injection_flagged",
    "screen",
    # policy
    "risk_floor",
    "policy",
    # authority
    "AuthorityScope",
    "manual_invocation",
    # binding (the delegated-authority registry — Slice 3a)
    "BindingStatus",
    "TriggerSpec",
    "SubGoal",
    "TightnessVector",
    "Graduation",
    "Guard",
    "PredicateValidator",
    "Binding",
    "binding_invocation",
    "seal_binding_id",
    "derived_binding_id",
    "BindingStore",
    "ReplaceResult",
    "FenceNotRecordedError",
    # the run journal (S8: an unattended run's effects, at most once)
    "RunJournal",
    "RunRef",
    "EffectStatus",
    "EffectOutcome",
    "HoldResult",
    "JournalCorruptError",
    "run_id_for",
    "hold_id_for",
    "effect_digest",
    # executor seam
    "Executor",
    "ActionRequest",
    "ExecutionResult",
    # confirm transport seam (the human gate)
    "ConfirmTransport",
    "ConfirmProposal",
    "ConfirmDecision",
    # pending-action queue
    "PendingAction",
    "PendingActionStore",
    # receipt
    "build_gate_face",
    "build_gate_verdict",
    # store
    "GateReceiptStore",
    "StoredGateReceipt",
    # orchestrator
    "EfferentGate",
    "GateOutcome",
    # the binding fire-path dispatcher (Slice 4a — single-link)
    "FireDispatcher",
    "FireDispatch",
    # the chain executor (Slice 4b — multi-link; the gate travels with the chain)
    "ChainExecutor",
    "ChainContext",
    "CompletedLink",
    "ChainLinkResult",
    "ChainOutcome",
    "ChainState",
    "ChainStateStore",
    "ChainStoreUnavailableError",
    "MalformedChainStateError",
    "seal_chain_id",
    # graduation (§2.6 the meta-loop — Slice 4c)
    "GraduationProposal",
    "propose_graduation",
    "is_depth_bounded",
    "one_rung_looser",
    "DEEP_OUTBOUND_MIN_HOPS",
    "DEFAULT_MIN_CLEAN_FIRES",
    # the bind-time NL→P compiler (Slice 3c)
    "Backtester",
    "CandidateBinding",
    "CompileSurfacing",
    "CompileResult",
    "REFUSE_CONSTITUTIONAL",
    "REFUSE_PRUDENTIAL",
    "compile_binding",
    "refuse_undecidable",
    # the diverse, fail-safe kill substrate (Slice 3a.5 — kill-purity + the kill-drill gate)
    "Kleene",
    "KillImpurityError",
    "assert_kill_pure",
    "kill_purity_error",
    "kill_outcome",
    "kill_trips",
    # the prediction-error monitor (Slice 3a.5 — the unknown-danger backstop)
    "TrajectoryObserver",
    "assert_trajectory_pure",
    "trajectory_bound",
    "bound_declared",
    "within_envelope",
    "prediction_diverged",
    # binding + gate liveness telemetry (Slice 3a.5)
    "binding_liveness",
    "gate_liveness",
]
