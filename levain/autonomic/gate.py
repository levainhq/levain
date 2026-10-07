"""levain.autonomic.gate — the EfferentGate orchestrator (the membrane's gated edge).

Ties the governance core into one decision: an :class:`ActionRequest` →
  1. risk lookup (fail-closed — an unknown action refuses, §1.3),
  2. the §1.5 refuse stack (``gates.screen`` — confidence + groundedness + injection, upstream of
     posture),
  3. ``policy(risk, trust)`` → a posture,
  4. ROUTE:
     - ``above_loop``/``on_loop`` FIRE via the injected Executor (act-then-notify, the receipt is
       the notify);
     - ``cooling_off``/``confirm``/``confirm_elevated`` (``needs_confirm``) PROPOSE via the injected
       ConfirmTransport: persist a :class:`PendingAction`, surface it (push), return PENDING — the
       fire-or-drop happens out-of-band at ``resolve`` (the operator's reply) or ``sweep_timeouts``
       (the silence default). With NO transport wired (a Slice-1 gate) they DEFER (fire nothing you
       can't gate);
     - ``refuse_escalate`` REFUSE;
  5. emit the gate-receipt (the FILLED action-face) on every TERMINAL decision (fire/deny); a PROPOSE
     writes NO receipt (no decision yet — the pending record carries the proposed action), and the
     receipt lands at ``resolve``.

With a :class:`~levain.autonomic.journal.RunJournal` wired, every BINDING fire is a journaled effect
(its request carries a ``run``): the executor runs inside ``RunJournal.effect``, so it runs at most
once per run and effect, never while another decision on the binding is open, and never after a fence
or a cancel. The journal is the ONLY durable home of a journaled decision: a confirm-class proposal
records its pending AS the run's hold (one appended line, with a chain link's continuation), the
open pendings are read from the open holds, and ``resolve`` is one write-once ``decide``. A binding
fire without a run is refused once a journal is wired. A manual fire (human authority, no run) is
not journaled and keeps the pending store; a gate without a journal is the unjournaled (pre-S8)
mode, where those guarantees do not apply.

The anti-cycle rule, held by construction: this module imports anneal-free vagus internals + the
injected Executor/ConfirmTransport/Store seams ONLY — never a control-plane surface (Levain/Bridge).
The dependency arrow stays down. Stdlib-only core.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass

from levain.autonomic.authority import AuthorityScope
from levain.autonomic.executor import ActionRequest, ExecutionResult, Executor
from levain.autonomic.gates import screen
from levain.autonomic.journal import (
    EffectOutcome, EffectStatus, JournalCorruptError, RunJournal, RunRef, effect_digest, hold_id_for,
)
from levain.autonomic.kill import kill_trips
from levain.autonomic.monitor import TrajectoryObserver, prediction_diverged
from levain.autonomic.pending import PendingAction, PendingActionStore
from levain.autonomic.policy import policy, risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.receipt import build_gate_face, build_gate_verdict
from levain.autonomic.risk import ActionManifest, UnknownAction
from levain.autonomic.store import GateReceiptStore
from levain.autonomic.transport import ConfirmDecision, ConfirmProposal, ConfirmTransport
from levain.autonomic.trust import IntentProvenance, SignalAuth, TrustContext

__all__ = ["GateOutcome", "EfferentGate"]

_log = logging.getLogger("levain.autonomic.gate")

# The confirm window: how long a proposed confirm-class action waits before the silence default
# (sweep) resolves it (cooling-off auto-fires; confirm drops). Generous default — the operator may
# be away; a real action that needs a tighter window sets one per-gate.
DEFAULT_CONFIRM_WINDOW_S = 3600

# A synthetic trust context for the resolve-fire path. The action ALREADY passed §1.5 + policy at
# propose time and the human has now explicitly approved it, so the resolve fire does NOT re-screen;
# this value is only carried into the rebuilt ActionRequest for the receipt face (which reads only
# authority + the provenance fields — it never re-runs trust).
_RESOLVED_TRUST = TrustContext(
    signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_BEARING,
    hops=0, human_present=True,
)


def _default_summary(request: ActionRequest) -> str:
    """A generic operator-facing summary when the adapter injects none. The adapter (which knows the
    action shape — recipient, subject, …) should inject a richer, §1.2-attributed ``summarize``."""
    head = request.payload.strip().replace("\n", " ")[:160]
    return f"{request.action_name}: {head}"


def _execution_record(result: ExecutionResult) -> dict[str, object]:
    return {"ok": result.ok, "detail": result.detail, "error": result.error,
            "downstream_id": result.downstream_id}


def _execution_from_record(rec: object) -> ExecutionResult:
    """Rebuild a recorded :class:`ExecutionResult`; a record that does not read is a failure."""
    if not isinstance(rec, dict) or not isinstance(rec.get("ok"), bool):
        return ExecutionResult(ok=False, error="recorded_result_unreadable")
    detail, error, downstream = rec.get("detail"), rec.get("error"), rec.get("downstream_id")
    return ExecutionResult(ok=rec["ok"], detail=detail if isinstance(detail, str) else "",
                           error=error if isinstance(error, str) else None,
                           downstream_id=downstream if isinstance(downstream, str) else None)


@dataclass(frozen=True)
class GateOutcome:
    """The result of gating one action. Exactly one of ``fired`` / ``refused`` / ``deferred`` /
    ``pending`` is the operative state (``fired`` may be False with none of the others set iff the
    executor failed AFTER an approved fire — see ``reason``). ``pending`` = PROPOSED to the operator,
    awaiting a ``resolve``; ``deferred`` = confirm-class but NO transport wired (the Slice-1
    fallback). ``receipt_id`` is the emitted gate-receipt (``None`` for a PENDING/DEFERRED — no
    decision was recorded yet — or a fail-soft persist failure)."""

    posture: Posture
    fired: bool
    refused: bool
    deferred: bool
    reason: str
    receipt_id: str | None
    execution: ExecutionResult | None
    pending: bool = False
    pending_id: str | None = None
    # KILLED (Slice 3a.5): an AUTONOMOUS fire was stopped by the prediction-error monitor (the actual
    # trajectory diverged from / could not be confirmed within the binding's predicted_trajectory). A
    # terminal non-fire distinct from a policy/screen ``refused`` — the receipt records it as
    # ``terminal_state=killed`` (``refuse_class=kill_triggered``). Mutually exclusive with the others.
    killed: bool = False
    # The binding that authorized this fire, surfaced from ``request.authority.binding_id`` (Slice 4c).
    # ``None`` for a manual/human fire (no standing grant). This is the RUNTIME outcome object, NOT the
    # frozen DecisionInfluenceReceipt — it lets a binding-AWARE caller (the adapter's out-of-band
    # sweep/resolve, which holds the BindingStore the gate deliberately does not) correlate an
    # out-of-band cooling-off/confirm fire back to its grant to record the §2.6 graduation evidence,
    # WITHOUT coupling the gate to the binding registry / graduation (it only READS the id off the
    # authority it already carries).
    binding_id: str | None = None
    # HELD (S8, journaled runs only): the effect did not run and is not decided — a decision is open on
    # the binding (``hold_id`` names it), or another live process is inside this effect. Not terminal:
    # delivering the same event again after the decision resumes the run.
    held: bool = False
    hold_id: str | None = None
    # REPLAYED (S8): the effect had already run in this run; ``fired``/``execution`` are the RECORDED
    # result and the executor was NOT called. Not a new fire: evidence is not recorded for it again.
    replayed: bool = False

    @property
    def is_clean_fire(self) -> bool:
        """Slice 4c gap #2 — the §2.6 graduation ``clean`` signal DERIVED from the terminal state (never
        hardcoded ``True``): a fire is CLEAN iff it reached terminal_state=fired with no kill / deny /
        refuse (the human/monitor did not have to STOP it). A killed/refused/deferred/pending outcome has
        ``fired=False``, so this reduces to ``fired`` — but stating it makes the derivation EXPLICIT and is
        the SINGLE source of truth every recorder shares (the dispatcher's immediate single-link fire, the
        chain executor's per-link completion, and the adapter's out-of-band sweep/resolve fire). The
        POST-HOC 'that fire was wrong' DEMOTION of a recorded clean fire needs a correction channel that
        does NOT exist yet — that inverse direction is 4d, not 4c."""
        return self.fired and not self.killed and not self.refused

    @property
    def approved(self) -> bool:
        """True iff the gate APPROVED the action (the executor was invoked) — names the state the
        booleans otherwise leave implicit: ``approved=True, fired=False`` = "approved but the EFFECT
        failed" (vs a no-op deny/defer/pending/kill). A consumer detecting "nothing happened" must
        check ``deferred``/``refused``/``pending``/``killed`` (or ``not approved``), NOT ``not fired``
        — a fired-then-failed action is not a no-op (L1-LOW-3). A PENDING is not yet approved (no
        decision has been made); a KILLED action was stopped pre-execute by the monitor."""
        return (not self.refused and not self.deferred and not self.pending and not self.killed
                and not self.held)


class EfferentGate:
    """The governed efferent edge. Construct with the DECLARED manifest, the receipt store, and the
    injected Executor (the trusted action seam). For the confirm rung, also inject a ``transport``
    (the propose channel) + a ``pending_store`` (the durable pending-action queue); without them,
    confirm-class actions DEFER (the Slice-1 behavior). ``clock`` is injected for deterministic
    receipts; ``summarize`` renders the operator-facing proposal summary; ``confirm_window_s`` is the
    silence-default window."""

    def __init__(
        self,
        *,
        manifest: ActionManifest,
        store: GateReceiptStore,
        executor: Executor,
        clock: Callable[[], _dt.datetime] | None = None,
        transport: ConfirmTransport | None = None,
        pending_store: PendingActionStore | None = None,
        summarize: Callable[[ActionRequest], str] | None = None,
        confirm_window_s: int = DEFAULT_CONFIRM_WINDOW_S,
        auto_fire_actions: frozenset[str] | None = None,
        trajectory_observer: TrajectoryObserver | None = None,
        journal: RunJournal | None = None,
        binding_generation: Callable[[str], int | None] | None = None,
    ) -> None:
        self._manifest = manifest
        # The run journal (S8). None ⇒ nothing is journaled (a request carrying a ``run`` is refused).
        # With a journal, ``binding_generation`` is REQUIRED: the registry is the authority that fences
        # a binding (``BindingStore.generation``), and the gate reads it before every journaled effect,
        # so a fence the journal failed to record still stops the run.
        if journal is not None and binding_generation is None:
            raise ValueError("EfferentGate: a journal needs binding_generation (BindingStore.generation)")
        self._journal = journal
        self._binding_generation = binding_generation
        self._store = store
        self._executor = executor
        # The injected diverse-substrate world-observer for the Slice-3a.5 prediction-error monitor.
        # None ⇒ the monitor is INERT (Slices 1-3 behavior unchanged): an autonomous fire proceeds with
        # no actual-vs-predicted check. When wired, an AUTONOMOUS fire (on-loop / binding) whose request
        # carries a ``predicted_trajectory`` is checked pre-execute and KILLED on divergence. A human-
        # gated fire never runs it (the human is the unknown-danger backstop). Slice 4 wires the real one.
        self._trajectory_observer = trajectory_observer
        # The OPT-IN allowlist of actions a cooling-off timeout may AUTONOMOUSLY fire (no human at the
        # moment of fire). EMPTY by default: nothing auto-fires until an action is DELIBERATELY opted in
        # here, in CODE (L3 codex ship-gate HIGH-1) — a code-side gate-origin control a JSON writer of
        # the pending store cannot mint, so a forged/recomputed cooling-off record is DROPPED at sweep,
        # never auto-fired. (cooling-off isn't reachable via policy() either; a binding assigns it in
        # Slice 3, and that binding opts its action into this set.)
        self._auto_fire_actions = auto_fire_actions or frozenset()
        # Default clock is UTC-AWARE (complement L3 LOW-1): a bare ``datetime.now`` is naive-local, so
        # a gate built without an explicit clock (a test, a future adopter) would write naive
        # created_at strings that string-sort BACKWARD against the adapter's aware ``+00:00`` ones —
        # corrupting the store's newest-first read. One aware default keeps every receipt comparable.
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._transport = transport
        self._pending_store = pending_store
        self._summarize = summarize or _default_summary
        self._confirm_window_s = confirm_window_s

    def get_pending(self, pending_id: str) -> PendingAction | None:
        """The OPEN pending action by id (no claim), or ``None``: a journaled run's open hold, else the
        manual pending store."""
        if self._journal is not None:
            hold = self._journal.find_pending(pending_id)
            if hold is not None:
                return self._hold_pending(hold["hold_id"]) if hold.get("decided") is None else None
        return self._pending_store.get(pending_id) if self._pending_store is not None else None

    def open_pendings(self) -> list[PendingAction]:
        """Every open decision: the journal's open holds (derived, the journal is their only home) plus
        the manual pending store. A hold whose pending record does not read is left out (the sweep
        rejects it)."""
        out: list[PendingAction] = []
        if self._journal is not None:
            for h in self._journal.open_holds():
                try:
                    out.append(PendingAction.from_dict(h["pending"]))
                except (KeyError, TypeError, ValueError, AttributeError):
                    continue
        if self._pending_store is not None:
            out.extend(self._pending_store.list_open())
        return out

    @property
    def journal(self) -> RunJournal | None:
        """The run journal this gate runs binding effects through, or ``None``. The fire path admits
        its runs into this same journal."""
        return self._journal

    # =================================================================================================
    # The forward path: gate one action (FIRE / PROPOSE / DEFER / REFUSE).
    # =================================================================================================
    def gate(self, request: ActionRequest) -> GateOutcome:
        """Resolve + route one action. NEVER raises into the caller — the outer fail-closed net here
        is the structural guarantee (codex L3): any unexpected error in resolution (a raising clock,
        a malformed TrustContext, a manifest/policy fault) fails toward involvement (REFUSE_ESCALATE),
        never propagates. The post-execute receipt-face/persist failures are handled inside ``_resolve``
        so they preserve the already-fired state; this net is the backstop for the rest."""
        try:
            return self._resolve(request)
        except Exception as e:  # noqa: BLE001 — the gate's "never raises" contract, made structural
            _log.error(
                "efferent gate UNEXPECTED error (%s): %s — failing closed (refuse, no receipt)",
                type(e).__name__, e,
            )
            return GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                reason=f"gate_error:{type(e).__name__}", receipt_id=None, execution=None,
            )

    def _resolve(self, request: ActionRequest) -> GateOutcome:
        """The gate body (wrapped by ``gate``'s fail-closed net). Pre-execute raises propagate to the
        net (→ refuse); the post-execute paths catch their own faults to preserve the fired state."""
        created_at = self._clock().isoformat()

        # 00. the run journal (S8). A binding fire must be a journaled effect once a journal is wired,
        # and a journaled effect that already ran, was cancelled or fenced, or is poisoned or in flight
        # stops HERE, before any decision is made again: a replay re-decides nothing.
        stop = self._journal_entry(request, created_at)
        if stop is not None:
            return stop

        # 0. consistency guard (codex L3 MED-5 + complement MED-2): ``human_present`` grants two
        # bypasses (absent-confidence passes §1.5; ABOVE_LOOP base in earned_posture). It is a free
        # field on the request, so a future autonomous caller / binding that mis-sets it would forge
        # human-grade trust. Tie it STRUCTURALLY to a human authority: a present human implies a human
        # grant. An inconsistent request (human_present without grantor=="human") fails closed —
        # `structural_invariants_beat_discipline`, not a Slice-3 discipline note. (Slice-3 invariant:
        # no autonomous binding may assert BOTH human_present AND grantor="human".)
        if request.trust.human_present and request.authority.grantor != "human":
            _log.warning("efferent gate REFUSE: human_present asserted with non-human authority %r",
                         request.authority.grantor)
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "human_present_without_human_authority")

        # 0b. ``ratified_posture`` is a LOOSENING lever (Slice 4c — step 2 fires at max(risk_floor,
        # ratified), so a ratified rung BELOW the static earned_posture loosens the fire down to the
        # floor). Under 4a/4b's max(policy, ratified) it could only RAISE involvement, so it was safe
        # ungated; now it can LOWER, so it MUST be tied to a genuine BINDING authority (the only thing
        # that derives ratified_posture from a SEALED binding.posture). A manual/forged request that sets
        # it without binding authority fails CLOSED — structural_invariants_beat_discipline, not a
        # discipline note (codex L3 HIGH-1). A binding fire always carries grantor=="binding" + a
        # binding_id (binding_invocation); an intermediate chain link sets ratified_posture=None and is
        # unaffected.
        if request.ratified_posture is not None and (
                request.authority.grantor != "binding" or not request.authority.binding_id):
            _log.warning("efferent gate REFUSE: ratified_posture set with non-binding authority %r "
                         "(the loosening lever requires a sealed binding grant)", request.authority.grantor)
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "ratified_posture_without_binding_authority")

        # 1. risk lookup. A BINDING fire (Slice 4) carries its risk DERIVED from the sealed goal-chain
        # tools (seam #1: ONE source of truth = the sealed tools, the same ``_aggregate_risk`` it was
        # ratified at) — resolve against THAT, not a manifest ``action_name`` lookup. The fire-path
        # already fail-CLOSED at derivation (an undeclared tool can't produce a risk → no request →
        # never reaches here). A MANUAL fire (no ``request.risk``) keeps the §1.3 fail-closed manifest
        # lookup: an action you never declared is maximally untrusted.
        if request.risk is not None:
            risk = request.risk
        else:
            try:
                risk = self._manifest.risk_of(request.action_name)
            except UnknownAction:
                _log.warning("efferent gate REFUSE: unknown action %r (not in manifest)", request.action_name)
                return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "unknown_action")

        # 2. posture = max(risk_floor, earned) — floors unbuyable. For a BINDING fire (Slice 4 seam #2)
        # the binding's SEALED ``ratified_posture`` is the OPERATIVE rung, clamped UP only by the unbuyable
        # risk FLOOR — ``max(risk_floor(risk), ratified_posture)``, NOT ``max(policy, ratified)``.
        #   • Slice 4a/4b shipped ``max(policy, ratified)``; Slice 4c REPLACES the ``policy`` term with
        #     ``risk_floor`` so a §2.6-GRADUATED binding (whose operator-ratified posture sits BELOW the
        #     static ``earned_posture`` — it earned OFF the intent-free/trust penalty with a clean track
        #     record) actually fires looser. Under the old form ``policy`` (= max(floor, earned)) would
        #     re-floor a graduated rung back UP to ``earned`` → graduation would be SILENT THEATER.
        #   • SAFE + behavior-IDENTICAL for every non-graduated binding: it sealed
        #     ``posture = max(floor, earned, requested) ≥ earned``, so ``ratified ≥ earned`` and
        #     ``max(risk_floor, ratified) == max(policy, ratified)`` — ``ratified`` dominates ``earned``.
        #   • RISK-ROSE still caught: a risen risk class/dimension RAISES ``risk_floor`` → climbs UP,
        #     overriding the looser graduated rung (the floor is unbuyable, §1.4 — graduation can never
        #     punch an external/irreversible/financial floor). ``earned_posture`` is a stable fossil
        #     (trigger+goal, both sealed) — it never rises post-ratification, so dropping it loses NO
        #     safety. govern-not-trust: the operator governs the gradient (ratifies the rung against the
        #     evidence, §2.6), the floor is the constitutional limit. Resolved BEFORE the §1.5 screen so
        #     the known-danger kill (step 3) can stamp it on the kill receipt + PREEMPT the screen.
        #   • ⚠ FORWARD-HAZARD (L2 L3 — load-bearing for the next maintainer): dropping the live
        #     ``policy`` term is safe ONLY WHILE trust is FULLY FOSSILIZED — ``earned_posture`` a pure
        #     function of SEALED inputs (``fossil_trust``: signal_auth from the trigger TYPE, INTENT_FREE,
        #     structural hops, human_present=False). If a future slice makes trust read a LIVE fire-time
        #     signal (an actual DMARC check on THIS email, an INTENT_BEARING trigger, a live reputation
        #     signal), a fire whose grounding DEGRADED would fire at the STALE ratified rung instead of
        #     climbing — a fail-OPEN the old ``max(policy, ratified)`` would have caught. At that point:
        #     restore the live ``policy`` term here (or re-resolve trust per-fire). This invariant lives
        #     in ``vagus_compiler.fossil_trust`` + is NOT asserted here — keep them in lockstep.
        posture = policy(risk, request.trust)
        if request.ratified_posture is not None:
            posture = max(risk_floor(risk), request.ratified_posture)

        # 3. the KNOWN-danger kill (Slice 4 — the first runtime consumer of the 3a.5 kill substrate,
        # closing codex's 3a.5 L3 finding #1) — evaluated BEFORE the §1.5 screen (complement L3): a
        # kill-worthy trigger must ALWAYS produce a KILL receipt, never be MASKED by a confidence/
        # grounding REFUSE — the kill is the operator's authored, most-specific danger signal, so it
        # preempts the generic screen. A binding fire carries its ``effective_guard`` kills + the trigger
        # event; the gate evaluates them on the DIVERSE fail-safe substrate (``kill_trips``) regardless
        # of posture — a known-dangerous trigger never fires NOR proposes. A trip → the existing
        # ``_kill`` (a uniform killed receipt). The decider names the binding's authored kill (the
        # autonomous actor) → ``by`` = binding / on-loop. Inert for a manual fire (no kill_predicates).
        kill_reason = self._known_danger_kill(request)
        if kill_reason is not None:
            kill_by = "binding" if request.authority.grantor == "binding" else "on-loop"
            _log.warning("efferent gate KILL: known-danger kill on %r — %s", request.action_name, kill_reason)
            return self._kill(request, created_at, posture, kill_by, request.actor_first_estimate,
                              reason=kill_reason)

        # 4. the §1.5 refuse stack (confidence + groundedness + injection) — require confidence iff
        # autonomous. Scan the FULL trigger surface (the action payload AND the proposal/query text that
        # drove it) — "ANY trigger payload entering the loop" (gates §), not just the rendered action
        # (L1-LOW-4). Runs AFTER the kill so a non-killed trigger is still screened for injection.
        verdict = screen(
            payload=f"{request.payload}\n{request.query_text}",
            grounded=request.grounded,
            require_confidence=not request.trust.human_present,
            overall_confidence=request.overall_confidence,
            directive_confidence=request.directive_confidence,
            confidence_floor=request.confidence_floor,
        )
        if not verdict.passed:
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, f"screen:{verdict.reason}")

        # 5. route.
        if posture is Posture.REFUSE_ESCALATE:
            return self._deny(request, created_at, posture, "policy_refuse")

        if posture.fires_immediately:
            # above_loop / on_loop → FIRE now (act-then-notify; the receipt is the trace). The gate
            # itself is the decider → verdict=auto. ``by`` names the decider: a standing BINDING fire
            # records ``by=binding`` (the 3a integration-test contract — the binding authorized it,
            # carried via authority.grantor); a manual/human-present on-loop fire stays ``by=on-loop``.
            by = "binding" if request.authority.grantor == "binding" else "on-loop"
            return self._fire(
                request=request, created_at=created_at, posture=posture,
                verdict="auto", by=by, actor_first_estimate=request.actor_first_estimate,
            )

        # confirm-class (cooling_off / confirm / confirm_elevated). PROPOSE via the transport if one
        # is wired; else DEFER (the Slice-1 fallback — fire nothing you can't gate, record no decision).
        if self._transport is None or (self._pending_store is None and request.run is None):
            _log.info(
                "efferent gate DEFER: %r resolved to %s but no confirm transport is wired",
                request.action_name, posture.name,
            )
            return GateOutcome(
                posture=posture, fired=False, refused=False, deferred=True,
                reason=f"posture {posture.name} requires a confirm transport (none wired)",
                receipt_id=None, execution=None, binding_id=request.authority.binding_id,
            )
        return self._propose(request, created_at, posture)

    # =================================================================================================
    # The confirm round-trip: propose → (out-of-band) resolve → fire-or-drop.
    # =================================================================================================
    def _propose(self, request: ActionRequest, created_at: str, posture: Posture) -> GateOutcome:
        """Record the decision a person is asked for + surface it via the transport. Returns PENDING (no
        receipt — no decision yet); the push is best-effort notification.

        A JOURNALED request (a binding run) records its pending as the run's HOLD in the run journal: one
        appended line holds the sealed pending, the chain continuation of a chain link, and the barrier
        that stops the binding's undecided effects. The journal is the only durable home of that decision;
        the pending store is not written. Proposing the same effect again finds the same hold (and its
        pending), so a re-delivered event never asks twice.

        A manual request persists a :class:`PendingAction` in the pending store. Fail-CLOSED on a persist
        failure: if we cannot durably record the decision we cannot later resolve it safely, so we DO NOT
        propose and we fire nothing. A TRANSPORT failure does NOT undo the recorded decision; it is
        logged, not fatal."""
        assert self._transport is not None
        run = request.run
        expires_at = self._expires_at(created_at)
        # Build a SEALED pending — PendingAction.create computes the id as a content FINGERPRINT over
        # every governance field, so any later alteration is caught by seal_matches() at resolve/sweep
        # (L3 codex HIGH-1/2 + complement MED-1). This also fixes the MED-6 id collision.
        pending = PendingAction.create(
            created_at=created_at, action_name=request.action_name, payload=request.payload,
            context_id=request.context_id, query_text=request.query_text, query_date=request.query_date,
            posture=posture.name, fail_open=posture.fail_open, requires_typed=posture.requires_typed,
            authority=request.authority.to_dict(), producers=tuple(request.producers),
            proposal_id=request.proposal_id, expires_at=expires_at,
        )
        if run is not None:
            assert self._journal is not None   # _journal_entry refused a run without a journal
            gen = self._current_generation(request)
            if gen is None:
                return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "binding_generation_unknown",
                                  cancel=False)
            try:
                h = self._journal.hold(run.run_id, run.effect_id, digest=self._digest_of(request),
                                       pending=pending.to_dict(), at=created_at, chain=request.continuation,
                                       chained=run.chained, current_generation=gen)
            except Exception as e:  # noqa: BLE001 — nothing was recorded: nothing proposed, nothing fired
                _log.error("efferent gate: journal hold FAILED (%s): %s — not proposed", type(e).__name__, e)
                return GateOutcome(
                    posture=posture, fired=False, refused=True, deferred=False,
                    reason=f"journal_error:{type(e).__name__}", receipt_id=None, execution=None,
                    binding_id=request.authority.binding_id,
                )
            if h.status is EffectStatus.APPROVED:
                # Decided and approved before (the process stopped between the decision and the
                # effect): run it now under that approval instead of asking again.
                by = h.decided_by if h.decided_by == "human" else (
                    "binding" if request.authority.grantor == "binding" else "on-loop")
                return self._fire(request=request, created_at=created_at, posture=posture,
                                  verdict="approved" if by == "human" else "auto", by=by,
                                  actor_first_estimate=request.actor_first_estimate, decided=True)
            if h.status is EffectStatus.REPLAYED:
                return self._replayed(request, h, created_at)
            if h.status is not EffectStatus.HELD:
                return self._journal_stop(request, h, posture)
            if not h.new_hold:
                # the decision is already open: its pending IS this proposal (asked once, pushed once)
                open_pending = self._hold_pending(h.hold_id or "")
                return GateOutcome(
                    posture=posture, fired=False, refused=False, deferred=False, pending=True,
                    pending_id=open_pending.pending_id if open_pending is not None else None,
                    reason="proposed earlier; awaiting confirm", receipt_id=None, execution=None,
                    binding_id=request.authority.binding_id,
                )
        else:
            assert self._pending_store is not None
            try:
                self._pending_store.add(pending)
            except Exception as e:  # noqa: BLE001 — fail-closed: can't persist → can't gate → don't propose
                _log.error("efferent gate: pending persist FAILED (%s): %s — refusing (cannot gate)",
                           type(e).__name__, e)
                return GateOutcome(
                    posture=posture, fired=False, refused=True, deferred=False,
                    reason=f"pending_persist_failed:{type(e).__name__}", receipt_id=None, execution=None,
                    binding_id=request.authority.binding_id,
                )
        pending_id = pending.pending_id
        proposal = ConfirmProposal(
            pending_id=pending_id,
            action_name=request.action_name,
            summary=self._safe_summary(request),
            posture=posture.name,
            fail_open=posture.fail_open,
            requires_typed=posture.requires_typed,
            expires_at=expires_at,
        )
        surfaced = self._safe_propose(proposal)
        reason = "proposed; awaiting confirm" if surfaced else "proposed (push not confirmed delivered); awaiting confirm"
        _log.info("efferent gate PROPOSE: %r → %s (pending %s, surfaced=%s)",
                  request.action_name, posture.name, pending_id, surfaced)
        return GateOutcome(
            posture=posture, fired=False, refused=False, deferred=False, pending=True,
            pending_id=pending_id, reason=reason, receipt_id=None, execution=None,
            binding_id=request.authority.binding_id,
        )

    def resolve(self, pending_id: str, decision: ConfirmDecision, *, chain_owned: bool = False) -> GateOutcome:
        """Resolve a proposed confirm-class action from the operator's reply (or a timeout sweep).
        NEVER raises (the gate contract); an unknown/already-resolved id refuses cleanly.

        A JOURNALED pending (the pending of a run's hold) is decided by ONE write-once ``decide`` in
        the run journal, the only durable home of that decision (:meth:`_resolve_hold`); a link of a
        chain is resolved only through its chain (``chain_owned``, ``ChainExecutor.resume``). A
        manual pending is claimed from the pending store (:meth:`_resolve_manual`).

        ``decision.approved`` → fire (verdict ``approved`` for a human, ``auto`` for a timeout
        auto-fire); else → a terminal deny (verdict ``denied``). ``decision.first_estimate`` is the
        operator's forced pre-truth read → the receipt's ``actor_first_estimate``."""
        try:
            hold = self._journal.find_pending(pending_id) if self._journal is not None else None
            if hold is not None:
                return self._resolve_hold(hold, decision, chain_owned=chain_owned)
            return self._resolve_manual(pending_id, decision)
        except Exception as e:  # noqa: BLE001 — resolve, like gate, never raises into the caller
            _log.error("efferent gate resolve UNEXPECTED error (%s): %s — failing closed",
                       type(e).__name__, e)
            return GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                reason=f"resolve_error:{type(e).__name__}", receipt_id=None, execution=None,
            )

    def _resolve_hold(self, hold: dict[str, Any], decision: ConfirmDecision, *,
                      chain_owned: bool) -> GateOutcome:
        """Resolve a journaled pending: its run's hold. The ONLY state change is one write-once
        ``decide`` in the run journal (which also cancels the run on a rejection, in the same record),
        so there is nothing else to keep in step with it and nothing to release. Of any number of
        resolvers, exactly one decision counts; every other resolver finds it and changes nothing.

        A refusal that is not a decision (a missing typed proof, an approval nobody may give
        unattended, an unreadable registry) records NOTHING: the decision stays open for a retry."""
        assert self._journal is not None
        created_at = self._clock().isoformat()
        hold_id = hold["hold_id"]
        if hold.get("chained") and not chain_owned:
            return self._refuse_open("chained_pending_resolves_through_its_chain", None)
        try:
            pending = PendingAction.from_dict(hold["pending"])
        except (KeyError, TypeError, ValueError, AttributeError):
            pending = None
        binding_id = pending.authority.get("binding_id") if pending is not None else hold.get("binding_id")
        if hold.get("decided") is not None:
            if hold["decided"] and decision.approved and pending is not None:
                pass   # an approval already recorded and maybe not yet run: fire it (once) below
            else:
                return self._refuse_open("journal:already_decided", binding_id)

        if pending is None or not pending.seal_matches():
            # the record a person was asked about is unreadable or altered: never fire it. Reject the
            # hold (with the digest it was opened with; the record's own bytes are not trusted).
            _log.error("efferent gate resolve: INTEGRITY failure on hold %s — REJECTED (no fire)", hold_id)
            self._journal.decide(hold_id, approve=False, digest=hold["digest"], by=decision.by)
            if pending is None:
                return self._refuse_open("integrity:unreadable_pending", binding_id)
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=self._authority_of(pending),
                posture=Posture.REFUSE_ESCALATE, verdict="denied", by=decision.by,
                reason="integrity:seal_mismatch", actor_first_estimate=decision.first_estimate,
            )

        def deny(posture: Posture, reason: str, *, by: str = decision.by,
                 terminal: str | None = None) -> GateOutcome:
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=self._authority_of(pending), posture=posture,
                verdict="denied", by=by, reason=reason, actor_first_estimate=decision.first_estimate,
                **({"terminal_state": terminal} if terminal else {}),
            )

        def reject(posture: Posture, reason: str, **kw: Any) -> GateOutcome:
            d = self._journal.decide(hold_id, approve=False, digest=hold["digest"], by=decision.by)  # type: ignore[union-attr]
            if not d.ok:
                return self._refuse_open(f"journal:{d.reason}", binding_id)
            return deny(posture, reason, **kw)

        posture = self._posture_of(pending)
        if posture is None:
            return reject(Posture.REFUSE_ESCALATE, "corrupt_posture")
        if not decision.approved:
            deny_terminal = "timed_out" if decision.by == "on-loop" else "refused"
            return reject(posture, f"denied:{decision.reason or decision.by}", terminal=deny_terminal)

        guard = self._guard_resolve_fire(pending, posture, decision)
        if guard is None and decision.by != "human" and not self._unattended_approval_allowed(pending, posture):
            guard = "unattended_approval_not_allowed"
        if guard in ("elevated_requires_typed_proof", "unattended_approval_not_allowed"):
            return self._refuse_open(guard, binding_id)   # not a decision: the hold stays open
        if guard is not None:
            return reject(posture, guard)                  # a re-validation failure IS a decision

        authority = self._authority_of(pending)
        verdict = "approved" if decision.by == "human" else "auto"
        by = decision.by if decision.by == "human" else (
            "binding" if authority.grantor == "binding" else "on-loop")
        run = RunRef(hold["run_id"], hold["effect_id"], chained=bool(hold.get("chained")))
        request = ActionRequest(
            action_name=pending.action_name, payload=pending.payload, context_id=pending.context_id,
            query_text=pending.query_text, query_date=pending.query_date,
            trust=_RESOLVED_TRUST, grounded=True, authority=authority,
            producers=pending.producers, proposal_id=pending.proposal_id,
            actor_first_estimate=decision.first_estimate, run=run,
        )
        if hold.get("decided") is None:
            d = self._journal.decide(hold_id, approve=True, digest=self._digest_of(request), by=decision.by)
            if not d.ok:
                if d.reason == "already_decided" and self._journal_hold_approved(hold_id):
                    pass   # another resolver approved first: the journal runs the effect at most once
                elif d.reason == "already_decided":
                    return self._refuse_open("journal:already_decided", binding_id)
                else:   # digest_mismatch: the bytes about to fire are not the ones the hold was opened on
                    return reject(posture, f"journal:{d.reason}")
        fired = self._fire(
            request=request, created_at=created_at, posture=posture,
            verdict=verdict, by=by, actor_first_estimate=decision.first_estimate, decided=True,
        )
        if fired.receipt_id is None and fired.refused and fired.reason.startswith("journal:"):
            # an approval the journal stopped (the run was cancelled or fenced since it was proposed):
            # the resolve still ends in a terminal decision, so it gets a receipt like every other one.
            return deny(posture, fired.reason, by="on-loop")
        return fired

    def _journal_hold_approved(self, hold_id: str) -> bool:
        rec = self._journal.get_hold(hold_id) if self._journal is not None else None
        return bool(rec is not None and rec.get("decided") is True)

    @staticmethod
    def _refuse_open(reason: str, binding_id: str | None) -> GateOutcome:
        """A resolve that records nothing (the decision stays as it is) and writes no receipt."""
        return GateOutcome(
            posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
            reason=reason, receipt_id=None, execution=None, binding_id=binding_id,
        )

    def _hold_pending(self, hold_id: str) -> PendingAction | None:
        rec = self._journal.get_hold(hold_id) if self._journal is not None else None
        try:
            return PendingAction.from_dict(rec["pending"]) if rec is not None else None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    def _resolve_manual(self, pending_id: str, decision: ConfirmDecision) -> GateOutcome:
        """A manual (non-journaled) pending: claimed from the pending store FIRST (the claim is the
        at-most-once gate), then fired or dropped."""
        if self._pending_store is None:
            return GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                reason="no_pending_store", receipt_id=None, execution=None,
            )
        # ATOMICALLY CLAIM the pending (at-most-once — L1-HIGH-1/2). The claim removes-and-returns in
        # ONE locked op, so the resolver OWNS the record BEFORE any fire: a CLI resolve racing the
        # daemon sweep (or two resolves) can never both fire the same irreversible action, and a crash
        # mid-fire DROPS it (it is already out of the open set) rather than leaving it re-fireable.
        # There is NO post-fire remove (the get→fire→remove TOCTOU is gone).
        pending = self._pending_store.claim(pending_id)
        if pending is None:
            # already claimed/resolved/swept, or never existed — nothing to fire; surface, don't crash.
            _log.warning("efferent gate resolve: no claimable pending %r (resolved/swept/unknown)", pending_id)
            return GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                reason="unknown_pending", receipt_id=None, execution=None,
            )
        created_at = self._clock().isoformat()  # the receipt records WHEN the decision was made

        # INTEGRITY: the claimed record must still match its content-fingerprint id (L3 codex H1/H2 +
        # complement MED-1). A mismatch ⇒ a field was altered after propose (tampered/corrupt/drifted) ⇒
        # REFUSE + record a denied receipt; the record is already claimed-out, so it cannot re-fire.
        if not pending.seal_matches():
            _log.error("efferent gate resolve: INTEGRITY mismatch on %s — record altered since propose; "
                       "REFUSED (no fire)", pending_id)
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=self._authority_of(pending),
                posture=Posture.REFUSE_ESCALATE, verdict="denied", by=decision.by,
                reason="integrity:seal_mismatch", actor_first_estimate=decision.first_estimate,
            )

        posture = self._posture_of(pending)
        if posture is None:
            # a corrupt posture name on a record whose seal otherwise verified (a re-sealed corruption,
            # or an enum that was retired) — DROP into a refuse, never an executable rung (codex MED).
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=self._authority_of(pending),
                posture=Posture.REFUSE_ESCALATE, verdict="denied", by=decision.by,
                reason="corrupt_posture", actor_first_estimate=decision.first_estimate,
            )
        authority = self._authority_of(pending)

        if not decision.approved:
            # a silence-window DROP (the sweep, by=on-loop) is a ``timed_out`` MODE; an explicit human
            # deny is ``refused`` — orthogonal to the ``denied`` verdict either way (Slice 3a.5 §2.1).
            deny_terminal = "timed_out" if decision.by == "on-loop" else "refused"
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=authority, posture=posture,
                verdict="denied", by=decision.by, reason=f"denied:{decision.reason or decision.by}",
                actor_first_estimate=decision.first_estimate, terminal_state=deny_terminal,
            )

        # APPROVE: re-validate the (untrusted, durable) pending at the moment of fire (L1-HIGH-4 +
        # MED-7). The pending file lives across the propose→resolve gap and is editable — so re-check
        # the elevated-typed proof, that the action is still declared, that its risk floor hasn't
        # RISEN above the posture the human saw, and that the payload still passes the §1.5 screen (a
        # tampered/injection payload fires NOTHING). A guard failure denies (records a denied receipt).
        guard = self._guard_resolve_fire(pending, posture, decision)
        if guard is None and decision.by != "human" and not self._unattended_approval_allowed(pending, posture):
            guard = "unattended_approval_not_allowed"
        if guard is not None:
            return self._deny_fields(
                created_at=created_at, action_name=pending.action_name, proposal_id=pending.proposal_id,
                context_id=pending.context_id, query_text=pending.query_text, query_date=pending.query_date,
                producers=pending.producers, authority=authority, posture=posture,
                verdict="denied", by=decision.by, reason=guard,
                actor_first_estimate=decision.first_estimate,
            )
        verdict = "approved" if decision.by == "human" else "auto"  # human reply vs timeout auto-fire
        # ``by`` names the decider/actor: a human reply = "human"; a silence-timeout auto-fire of a
        # BINDING-authored cooling-off action records "binding" (the standing grant is the autonomous
        # actor — the cooling-off cancel window is its posture, not a separate human-in-loop decider),
        # mirroring the immediate-fire derivation so ANY binding-authored fire reads as by=binding, never
        # by=on-loop (L3 review — the receipt's `by` was inconsistent between a binding's immediate fire
        # and its cooling-off sweep fire). A human-authored cooling-off sweep stays by=on-loop.
        by = decision.by if decision.by == "human" else (
            "binding" if authority.grantor == "binding" else "on-loop")
        # The resolve-fire request carries NO kill_predicates/trigger_event (a binding's known-danger
        # kill was a PROPOSE-time gate, evaluated once at gate(): the §2.1 dispatch-time kill — Slice 4).
        # This is sound ONLY because the known-danger kill is DETERMINISTIC over the IMMUTABLE trigger
        # event — re-evaluating it at the sweep would yield the same verdict (L2-L3 invariant). If a
        # future kill grammar ever referenced fire-time/world state, the cooling-off auto-fire would
        # need to re-evaluate it here.
        request = ActionRequest(
            action_name=pending.action_name, payload=pending.payload, context_id=pending.context_id,
            query_text=pending.query_text, query_date=pending.query_date,
            trust=_RESOLVED_TRUST, grounded=True, authority=authority,
            producers=pending.producers, proposal_id=pending.proposal_id,
            actor_first_estimate=decision.first_estimate,
        )
        return self._fire(
            request=request, created_at=created_at, posture=posture,
            verdict=verdict, by=by, actor_first_estimate=decision.first_estimate,
        )

    def _unattended_approval_allowed(self, pending: PendingAction, posture: Posture) -> bool:
        """An approval no human gave (``by != "human"``) is the silence default of a cooling-off rung,
        and nothing else: the posture must fail open, the action must be in the code-side auto-fire
        allowlist, and the pending must have expired. Checked in ``resolve`` itself, so a caller other
        than ``sweep_timeouts`` cannot approve a confirm-class pending on nobody's behalf."""
        try:
            now = self._clock()
        except Exception:  # noqa: BLE001 — no clock, no proof of expiry
            return False
        return (posture.fail_open and pending.action_name in self._auto_fire_actions
                and self._expired(pending, now))

    def _guard_resolve_fire(self, pending: PendingAction, posture: Posture,
                            decision: ConfirmDecision) -> str | None:
        """Re-validate a claimed pending at the moment of fire — the pending file is untrusted durable
        input across the propose→resolve gap (L1-HIGH-4 / MED-7). Returns a refuse-reason, or ``None``
        if the fire may proceed. Cheap structural defenses (the deeper file-integrity HMAC is a later
        hardening):

        - MED-7: an elevated rung (``requires_typed``) needs a typed / re-auth proof — the gate
          ENFORCES the stronger affordance it promises, not just renders it.
        - HIGH-4: the action must still be DECLARED (a manifest that dropped it → refuse); its risk
          floor must not have RISEN above the posture the human approved (a manifest tightening must
          not let a stale, less-involved approval through); and the payload must still pass the §1.5
          injection/grounding screen (an injection smuggled into a tampered payload fires NOTHING)."""
        if posture.requires_typed and not decision.typed_proof:
            return "elevated_requires_typed_proof"
        try:
            risk = self._manifest.risk_of(pending.action_name)
        except UnknownAction:
            return "revalidate:unknown_action"
        if risk_floor(risk) > posture:
            return "revalidate:risk_floor_rose"
        v = screen(payload=f"{pending.payload}\n{pending.query_text}", grounded=True,
                   require_confidence=False)  # human/binding is the anchor at resolve; re-scan injection
        if not v.passed:
            return f"revalidate:{v.reason}"
        return None

    def sweep_timeouts(self, now: _dt.datetime | None = None,
                       *, skip: Callable[[PendingAction], bool] | None = None) -> list[GateOutcome]:
        """Resolve every pending action past its ``expires_at`` by the silence default: cooling-off
        (``fail_open``) AUTO-FIRES (verdict ``auto``, by ``on-loop``); confirm / confirm-elevated DROP
        (verdict ``denied``, by ``on-loop``). Idempotent + fail-soft per item (one bad pending never
        stops the sweep). Returns the resolved outcomes. NEVER raises.

        ``skip`` (Slice 4c — L1 L3) is an optional caller-supplied predicate: a pending for which
        ``skip(pending)`` is True is LEFT UNTOUCHED (not swept). The gate is deliberately chain-AGNOSTIC,
        so the adapter passes ``skip=_is_chain_pending`` to keep this sweep from PLAIN-FIRING a
        chain-shaped pending standalone — a crash-orphaned chain link (its ChainState lost before persist)
        whose deliver action is allowlisted would otherwise auto-fire here, stranding the chain (the 4b
        "never plain-fire a chain-shaped pending" rule the chain-resume path already enforces). A
        ``skip`` fault on one item is fail-soft (treated as not-skipped is unsafe → treated as SKIPPED)."""
        # NEVER raises (L3 codex ship-gate MED): the clock + EACH item body are fail-soft, so a raising
        # clock or one malformed-but-loaded record can't crash the sweep (resolve has its own net; the
        # sweep's per-item work — seal_matches / _posture_of / claim — runs OUTSIDE resolve and so needs
        # its own).
        try:
            now = now or self._clock()
        except Exception as e:  # noqa: BLE001 — a raising clock fails the whole sweep closed (no fires)
            _log.error("efferent gate sweep: clock FAILED (%s): %s — no sweep", type(e).__name__, e)
            return []
        outcomes: list[GateOutcome] = self._sweep_holds(now)
        if self._pending_store is None:
            return outcomes
        try:
            pendings = self._pending_store.list_open()
        except Exception as e:  # noqa: BLE001 — defensive; list_open is already fail-soft
            _log.error("efferent gate sweep: list_open FAILED (%s): %s", type(e).__name__, e)
            return []
        for pending in pendings:
            if skip is not None:
                try:
                    if skip(pending):
                        continue   # the caller owns this pending (a chain-shaped one) — don't plain-fire it
                except Exception as e:  # noqa: BLE001 — a skip-predicate fault fails toward NOT firing (safe)
                    _log.error("efferent gate sweep: skip predicate FAILED on %s (%s): %s — skipping (safe)",
                               getattr(pending, "pending_id", "?"), type(e).__name__, e)
                    continue
            try:
                outcome = self._sweep_one(pending, now)
            except Exception as e:  # noqa: BLE001 — one bad item never stops the sweep
                _log.error("efferent gate sweep: item %s FAILED (%s): %s — skipped",
                           getattr(pending, "pending_id", "?"), type(e).__name__, e)
                continue
            # complement LOW-1: a CLI resolve racing the sweep can claim a pending between the snapshot
            # and resolve()'s claim → a NON-decision (unknown_pending). _sweep_one returns None for a
            # drop / non-decision, so only ACTUAL sweep decisions are appended.
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _sweep_one(self, pending: PendingAction, now: _dt.datetime) -> GateOutcome | None:
        """Resolve one expired pending by its silence default — or DROP it. Returns the resolved
        outcome, or ``None`` for a drop / non-decision (not-expired, dropped-as-tampered, or a
        snapshot-vs-claim race). Each guard is structural defence behind the seal."""
        if not self._expired(pending, now):
            return None
        # INTEGRITY first (codex H1/H2): a tampered/corrupt record is claimed-out + DROPPED, never
        # auto-fired — don't derive a decision from untrusted fields.
        if not pending.seal_matches():
            if self._pending_store.claim(pending.pending_id) is not None:  # type: ignore[union-attr]
                _log.error("efferent gate sweep: INTEGRITY mismatch on %s — DROPPED, not auto-fired",
                           pending.pending_id)
            return None
        posture = self._posture_of(pending)
        if posture is None:   # corrupt posture (codex MED) → DROP, no execution path
            if self._pending_store.claim(pending.pending_id) is not None:  # type: ignore[union-attr]
                _log.error("efferent gate sweep: corrupt posture on %s — DROPPED", pending.pending_id)
            return None
        # HIGH-3 (defence-in-depth behind the seal): the silence default is the VALIDATED posture's,
        # never the raw stored fail_open. A disagreement → DROP, never auto-fire.
        if pending.fail_open != posture.fail_open:
            if self._pending_store.claim(pending.pending_id) is not None:  # type: ignore[union-attr]
                _log.error("efferent gate sweep: fail_open/posture mismatch on %s (stored=%s, %s.fail_open=%s)"
                           " — DROPPED, NOT auto-fired", pending.pending_id, pending.fail_open,
                           posture.name, posture.fail_open)
            return None
        if posture.fail_open:
            # AUTO-FIRE (no human at fire-time) is ALLOWLIST-GATED (codex ship-gate HIGH-1): only an
            # action DELIBERATELY opted into auto_fire_actions (in code) may autonomously fire — a
            # forged/recomputed cooling-off record over a non-allowlisted action is DROPPED.
            if pending.action_name not in self._auto_fire_actions:
                if self._pending_store.claim(pending.pending_id) is not None:  # type: ignore[union-attr]
                    _log.error("efferent gate sweep: %r not in the auto-fire allowlist — cooling-off %s "
                               "DROPPED, not auto-fired", pending.action_name, pending.pending_id)
                return None
            decision = ConfirmDecision(approved=True, by="on-loop", reason="cooling_off_window_elapsed")
        else:
            decision = ConfirmDecision(approved=False, by="on-loop", reason="confirm_window_elapsed")
        outcome = self.resolve(pending.pending_id, decision)  # resolve CLAIMS + re-verifies the seal
        if outcome.refused and outcome.reason in ("unknown_pending", "journal:already_decided"):
            return None   # snapshot-vs-claim race (another resolver won) — a non-decision
        return outcome

    def _sweep_holds(self, now: _dt.datetime) -> list[GateOutcome]:
        """The silence default for JOURNALED pendings (the open holds): a hold whose pending has expired
        is decided by ``resolve`` — cooling-off on the auto-fire allowlist approves (``by=on-loop``),
        everything else rejects. A chain link's hold is left to ``ChainExecutor.sweep_timeouts`` (the
        chain resumes from it). Fail-soft per hold."""
        if self._journal is None:
            return []
        try:
            holds = self._journal.open_holds()
        except Exception as e:  # noqa: BLE001
            _log.error("efferent gate sweep: open holds unreadable (%s): %s", type(e).__name__, e)
            return []
        out: list[GateOutcome] = []
        for h in holds:
            if h.get("chained") or not isinstance(h.get("pending"), dict):
                continue
            try:
                decision = self.silence_decision(h, now)
                if decision is None:
                    continue
                outcome = self._resolve_hold(h, decision, chain_owned=False)
            except Exception as e:  # noqa: BLE001 — one bad hold never stops the sweep
                _log.error("efferent gate sweep: hold %s FAILED (%s): %s", h.get("hold_id"), type(e).__name__, e)
                continue
            if not (outcome.refused and outcome.reason == "journal:already_decided"):
                out.append(outcome)
        return out

    def silence_decision(self, hold: dict[str, Any], now: _dt.datetime) -> ConfirmDecision | None:
        """The silence-default decision for an expired journaled pending, or ``None`` (not expired). A
        record that does not read, a posture that does not parse, or a fail-open flag that disagrees with
        the posture is a DROP; cooling-off auto-fires only for an allowlisted action."""
        try:
            pending = PendingAction.from_dict(hold["pending"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return ConfirmDecision(approved=False, by="on-loop", reason="unreadable_pending")
        if not self._expired(pending, now):
            return None
        posture = self._posture_of(pending)
        if (not pending.seal_matches() or posture is None or pending.fail_open != posture.fail_open
                or not posture.fail_open or pending.action_name not in self._auto_fire_actions):
            reason = "cooling_off_not_allowlisted" if (posture is not None and posture.fail_open) else "confirm_window_elapsed"
            return ConfirmDecision(approved=False, by="on-loop", reason=reason)
        return ConfirmDecision(approved=True, by="on-loop", reason="cooling_off_window_elapsed")

    # =================================================================================================
    # Shared terminal paths (FIRE / DENY) — used by both the immediate route and the resolve route.
    # =================================================================================================
    def _fire(self, *, request: ActionRequest, created_at: str, posture: Posture,
              verdict: str, by: str, actor_first_estimate: object | None,
              decided: bool = False) -> GateOutcome:
        """FIRE the action then build + persist the FILLED receipt. The effect fires FIRST; a
        face-build fault thereafter (codex L3 HIGH-2 / complement MED-1) must NOT raise into the
        caller NOR relabel the outcome as refused — preserve fired-state, drop the receipt.

        Slice 3a.5: BEFORE the effect, an autonomous FAST-LANE fire (``posture.fires_immediately`` —
        on-loop / above-loop, the act-then-notify rungs with no per-action human approval) that carries
        a ``predicted_trajectory`` runs the diverse prediction-error monitor — a divergence KILLS the
        action (no effect, ``terminal_state=killed``). Gated on the POSTURE, not ``by`` (codex L3): this
        deliberately EXCLUDES every confirm-class fire — a human-approved confirm AND a cooling-off
        timeout auto-fire alike — because confirm-class is human-gated (the visible cancel window is the
        gate), exactly the §2.1 boundary ("the prediction-error kill is load-bearing when a binding
        loosens to on-loop, NOT while confirm-class is human-gated"). Inert with no observer / no
        trajectory wired (Slices 1-3 unchanged)."""
        if (posture.fires_immediately and request.predicted_trajectory is not None
                and self._trajectory_observer is not None):
            killed = self._run_prediction_monitor(request, created_at, posture, by, actor_first_estimate)
            if killed is not None:
                return killed       # the monitor stopped the fire — no effect, killed receipt persisted
        if request.run is not None:
            # A journaled effect (S8): it runs inside the journal, at most once. ``decided`` = it runs
            # under its own approved hold (the resolve path); an undecided effect is held while any
            # decision on its binding is open.
            ran = self._journaled_execute(request, created_at=created_at, posture=posture,
                                          verdict=verdict, by=by, decided=decided)
            if isinstance(ran, GateOutcome):
                return ran
            execution = ran
        else:
            execution = self._safe_execute(request.action_name, request.payload, request.context_id)
        try:
            face = build_gate_face(
                context_id=request.context_id,
                query_text=request.query_text,
                query_date=request.query_date,
                producers=request.producers,
                gate=build_gate_verdict(verdict=verdict, by=by, binding_id=request.authority.binding_id),
                authority=request.authority,
                terminal_state="fired",
                downstream_id=execution.downstream_id,
                actor_first_estimate=actor_first_estimate,
            )
        except Exception as e:  # noqa: BLE001 — effect already fired; never raise, never relabel
            _log.error("efferent gate: receipt face build FAILED post-execute (%s): %s — fired, no receipt",
                       type(e).__name__, e)
            return GateOutcome(
                posture=posture, fired=execution.ok, refused=False, deferred=False,
                reason=f"receipt_face_failed:{type(e).__name__}", receipt_id=None, execution=execution,
                binding_id=request.authority.binding_id,
            )
        receipt_id = self._persist(
            created_at=created_at, action_name=request.action_name, proposal_id=request.proposal_id,
            posture=posture, fired=execution.ok, face=face,
        )
        if receipt_id is not None and request.run is not None:
            self._note_receipt(request.run, receipt_id)
        reason = "" if execution.ok else f"execute_failed:{execution.error}"
        if receipt_id is None:  # the effect may already have fired; the receipt just didn't persist
            reason = f"{reason}; receipt_persist_failed" if reason else "receipt_persist_failed"
        return GateOutcome(
            posture=posture, fired=execution.ok, refused=False, deferred=False,
            reason=reason, receipt_id=receipt_id, execution=execution,
            binding_id=request.authority.binding_id,
        )

    def _deny(self, request: ActionRequest, created_at: str, posture: Posture, reason: str,
              *, cancel: bool = True) -> GateOutcome:
        """Record a terminal gate-side DENY (unknown action / §1.5 refuse / policy refuse). The
        decider is the AUTONOMOUS gate (``by=on-loop``) — see ``_deny_fields`` for why ``on-loop``
        stands against ``verdict=denied`` (the frozen enum has no ``constitution`` value). A deny of a
        journaled effect ends its run (:meth:`_cancel_run`), unless ``cancel=False``: a refusal caused
        by a condition that can clear (the registry could not be read, the run is not admitted yet)
        must not end a run a re-delivery could finish."""
        if cancel:
            self._cancel_run(request, f"denied:{reason}")
        return self._deny_fields(
            created_at=created_at, action_name=request.action_name, proposal_id=request.proposal_id,
            context_id=request.context_id, query_text=request.query_text, query_date=request.query_date,
            producers=request.producers, authority=request.authority, posture=posture,
            verdict="denied", by="on-loop", reason=reason,
            actor_first_estimate=request.actor_first_estimate,
        )

    def _deny_fields(self, *, created_at, action_name, proposal_id, context_id, query_text, query_date,
                     producers, authority: AuthorityScope, posture: Posture, verdict: str, by: str,
                     reason: str, actor_first_estimate: object | None,
                     terminal_state: str = "refused") -> GateOutcome:
        """Build + persist a terminal DENY receipt (no effect fired) and return a refused outcome.
        Shared by the gate-side refuse (``by=on-loop``) and the human deny at resolve (``by=human``).

        ``by`` against a ``verdict=denied``: ``on-loop`` names the autonomous decider; a §1.5/policy
        refuse has no human, and the frozen ``by`` enum is exactly ``{human, binding, on-loop}`` (no
        ``constitution`` value), so ``on-loop`` stands — sharpening refuse-vs-deny is receipt-contract
        checkpoint #2 (L2-M1, 2026-06-24). A human deny carries ``by=human`` (the operator decided).

        ``terminal_state`` (Slice 3a.5) is the MODE — ``refused`` for a gate/human deny (the default),
        ``timed_out`` for a silence-window DROP at sweep (the caller passes it). Orthogonal to
        ``verdict`` (always ``denied`` here)."""
        try:
            face = build_gate_face(
                context_id=context_id, query_text=query_text, query_date=query_date, producers=producers,
                gate=build_gate_verdict(verdict=verdict, by=by, binding_id=authority.binding_id),
                authority=authority, terminal_state=terminal_state, downstream_id=None,
                actor_first_estimate=actor_first_estimate,
            )
        except Exception as e:  # noqa: BLE001 — no effect fired; still never raise into the caller
            _log.error("efferent gate: deny receipt face build FAILED (%s): %s — refused, no receipt",
                       type(e).__name__, e)
            return GateOutcome(
                posture=posture, fired=False, refused=True, deferred=False,
                reason=f"{reason}; receipt_face_failed", receipt_id=None, execution=None,
                binding_id=authority.binding_id,
            )
        receipt_id = self._persist(
            created_at=created_at, action_name=action_name, proposal_id=proposal_id,
            posture=posture, fired=False, face=face,
        )
        return GateOutcome(
            posture=posture, fired=False, refused=True, deferred=False,
            reason=reason, receipt_id=receipt_id, execution=None,
            binding_id=authority.binding_id,
        )

    # =================================================================================================
    # The KNOWN-danger kill (Slice 4 — the first runtime consumer of the 3a.5 kill substrate).
    # =================================================================================================
    def _known_danger_kill(self, request: ActionRequest) -> str | None:
        """Evaluate the firing binding's KNOWN-danger kills (the human's authored "stop if X",
        ``effective_guard`` = sealed floor + tightening additions) against the live trigger event on the
        DIVERSE fail-safe substrate (``kill_trips``: UNKNOWN trips — "I cannot confirm this is safe" must
        STOP an autonomous fire). Returns a kill-reason naming the tripped kill, or ``None`` if no kill
        trips (or this is a manual fire with no kill_predicates / no trigger_event ⇒ inert).

        The diversity is load-bearing (3a.5): the kill is re-implemented in ``levain.autonomic.kill``, NOT
        flow's ``events.py`` that drove the trigger match — so a common-mode absent-field event that the
        trigger fired on still TRIPS the kill. Pure read over the event; ``kill_trips`` never raises (the
        outer gate net is the backstop regardless)."""
        if not request.kill_predicates or request.trigger_event is None:
            return None
        for i, kp in enumerate(request.kill_predicates):
            if kill_trips(kp, request.trigger_event):
                return f"known_danger_kill:guard_kill_{i}_tripped"
        return None

    # =================================================================================================
    # The prediction-error KILL path (Slice 3a.5 — the autonomous unknown-danger backstop).
    # =================================================================================================
    def _run_prediction_monitor(self, request: ActionRequest, created_at: str, posture: Posture,
                                by: str, actor_first_estimate: object | None) -> GateOutcome | None:
        """Diff the ACTUAL trajectory (from the injected observer) against the binding's
        ``predicted_trajectory`` BEFORE an autonomous fire. Returns a KILLED outcome on divergence (the
        caller aborts the fire), or ``None`` to proceed. FAIL-SAFE on an observer fault: a raising or
        non-dict observer means we cannot confirm safety → KILL, never fire blind."""
        try:
            actual = self._trajectory_observer.observe(  # type: ignore[union-attr]  # guarded by caller
                request.action_name, request.payload, context_id=request.context_id)
        except Exception as e:  # noqa: BLE001 — a bad observer must not crash the gate; fail-safe to KILL
            _log.error("efferent gate: trajectory observer RAISED (%s): %s — KILL (cannot confirm safety)",
                       type(e).__name__, e)
            return self._kill(request, created_at, posture, by, actor_first_estimate,
                              reason=f"monitor:observer_error:{type(e).__name__}")
        if not isinstance(actual, dict):
            _log.error("efferent gate: trajectory observer returned %s, not a dict — KILL (fail-safe)",
                       type(actual).__name__)
            return self._kill(request, created_at, posture, by, actor_first_estimate,
                              reason="monitor:observer_bad_shape")
        diverged, why = prediction_diverged(request.predicted_trajectory, actual)
        if diverged:
            _log.warning("efferent gate KILL: prediction-error on %r — %s", request.action_name, why)
            return self._kill(request, created_at, posture, by, actor_first_estimate,
                              reason=f"prediction_error:{why}")
        return None

    def _kill(self, request: ActionRequest, created_at: str, posture: Posture, by: str,
              actor_first_estimate: object | None, *, reason: str) -> GateOutcome:
        """Build + persist a KILLED receipt (``terminal_state=killed``, ``refuse_class=kill_triggered``;
        NO effect fired) and return a killed outcome. The autonomous monitor is the decider → the frozen
        ``gate.verdict`` is ``denied`` (the verdict enum has no ``killed`` — the MODE lives in
        ``terminal_state``, §2.1); ``by`` names the autonomous decider (on-loop / binding). Fail-soft on
        a face/persist fault (like ``_fire``/``_deny_fields``): the kill stands, the trace just drops.
        A kill of a journaled effect ends its run (:meth:`_cancel_run`)."""
        self._cancel_run(request, f"killed:{reason}")
        try:
            face = build_gate_face(
                context_id=request.context_id, query_text=request.query_text,
                query_date=request.query_date, producers=request.producers,
                gate=build_gate_verdict(verdict="denied", by=by, binding_id=request.authority.binding_id),
                authority=request.authority, terminal_state="killed", downstream_id=None,
                actor_first_estimate=actor_first_estimate, refuse_class="kill_triggered",
            )
        except Exception as e:  # noqa: BLE001 — never raise into the caller; the kill stood, drop the trace
            _log.error("efferent gate: kill receipt face build FAILED (%s): %s — killed, no receipt",
                       type(e).__name__, e)
            return GateOutcome(
                posture=posture, fired=False, refused=False, deferred=False, killed=True,
                reason=f"{reason}; receipt_face_failed", receipt_id=None, execution=None,
                binding_id=request.authority.binding_id,
            )
        receipt_id = self._persist(
            created_at=created_at, action_name=request.action_name, proposal_id=request.proposal_id,
            posture=posture, fired=False, face=face,
        )
        return GateOutcome(
            posture=posture, fired=False, refused=False, deferred=False, killed=True,
            reason=reason, receipt_id=receipt_id, execution=None,
            binding_id=request.authority.binding_id,
        )

    # =================================================================================================
    # internals
    # =================================================================================================
    # =================================================================================================
    # The run journal (S8): binding effects at most once.
    # =================================================================================================
    @staticmethod
    def _digest_of(request: ActionRequest) -> str:
        return effect_digest(action_name=request.action_name, payload=request.payload,
                             context_id=request.context_id)

    def _journal_bar(self, run: RunRef | None, authority: AuthorityScope) -> str | None:
        """Why a request or pending may not proceed at all given the journal wiring, or ``None``: a
        binding fire must be journaled once a journal is wired, and a run cannot be journaled without
        one."""
        if run is None:
            if self._journal is not None and authority.grantor == "binding":
                return "unjournaled_binding_fire"
            return None
        if self._journal is None:
            return "run_without_journal"
        return None

    def _journal_entry(self, request: ActionRequest, created_at: str) -> GateOutcome | None:
        """The gate's first step for a journaled request: refuse a wiring mismatch, and stop an effect
        the journal already settled (done → replayed; cancelled, fenced, poisoned → refused; in flight
        elsewhere → held) before any decision is made again. ``None`` = proceed."""
        bar = self._journal_bar(request.run, request.authority)
        if bar is not None:
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, bar)
        if request.run is None:
            return None
        assert self._journal is not None
        gen = self._current_generation(request)
        if gen is None:
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "binding_generation_unknown",
                              cancel=False)
        try:
            barrier = self._journal.peek(request.run.run_id, request.run.effect_id, current_generation=gen,
                                         digest=self._digest_of(request))
        except KeyError:
            return self._deny(request, created_at, Posture.REFUSE_ESCALATE, "run_not_admitted", cancel=False)
        except (JournalCorruptError, OSError) as e:
            _log.error("efferent gate: run journal unreadable (%s): %s — refusing", type(e).__name__, e)
            return GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                reason=f"journal_unreadable:{type(e).__name__}", receipt_id=None, execution=None,
                binding_id=request.authority.binding_id,
            )
        if barrier is None:
            return None
        if barrier.status is EffectStatus.REPLAYED:
            return self._replayed(request, barrier, created_at)
        return self._journal_stop(request, barrier, Posture.REFUSE_ESCALATE)

    def _current_generation(self, request: ActionRequest) -> int | None:
        """The firing binding's governance generation from the registry, read immediately before the
        journal barrier, or ``None`` (absent, malformed, unreadable: no authority, refuse). A pause
        committed after this read is ordered after the barrier: it stops the NEXT effect, the same
        residual as a fence landing while an effect is inside its call."""
        bid = request.authority.binding_id
        if not bid or self._binding_generation is None:
            return None
        try:
            return self._binding_generation(bid)
        except Exception as e:  # noqa: BLE001
            _log.error("efferent gate: binding generation for %r unreadable (%s): %s", bid,
                       type(e).__name__, e)
            return None

    def _journal_stop(self, request: ActionRequest, out: EffectOutcome, posture: Posture) -> GateOutcome:
        """The outcome for an effect the journal did not let run. HELD and IN_FLIGHT are not terminal
        (``held``: deliver the event again later); CANCELLED, FENCED and POISONED are (``refused``).
        No receipt here: the journal line that stopped it (the cancel, the fence, the unknown outcome)
        is the record, and writing one per re-delivery would repeat it. (A RESOLVE that ends here is
        the exception: ``_resolve_pending`` writes its receipt, since every resolve is a decision.)"""
        held = out.status in (EffectStatus.HELD, EffectStatus.IN_FLIGHT)
        _log.info("efferent gate: %r stopped by the run journal (%s)", request.action_name, out.status.value)
        return GateOutcome(
            posture=posture, fired=False, refused=not held, deferred=False,
            reason=f"journal:{out.status.value}", receipt_id=None, execution=None,
            binding_id=request.authority.binding_id, held=held, hold_id=out.hold_id,
        )

    def _journaled_execute(self, request: ActionRequest, *, created_at: str, posture: Posture,
                           verdict: str, by: str, decided: bool) -> ExecutionResult | GateOutcome:
        """Run the executor through the journal. Returns the :class:`ExecutionResult` of an effect that
        ran now, or the :class:`GateOutcome` when the journal stopped or replayed it.

        Unlike :meth:`_safe_execute`, an executor that RAISES, or returns something that is not an
        ``ExecutionResult``, has an UNKNOWN outcome here: the journal poisons the effect (it is never
        run again) and the fire reports ``ok=False``. What the receipt needs is recorded with the
        result, so a replay can write a receipt that never landed."""
        assert self._journal is not None and request.run is not None
        run = request.run
        called = False

        def call() -> dict[str, object]:
            nonlocal called
            called = True
            result = self._executor.execute(request.action_name, request.payload, context_id=request.context_id)
            if not isinstance(result, ExecutionResult):
                raise TypeError(f"executor returned {type(result).__name__}, not an ExecutionResult")
            return {"execution": _execution_record(result), "posture": posture.name,
                    "verdict": verdict, "by": by}

        gen = self._current_generation(request)
        if gen is None:
            return GateOutcome(
                posture=posture, fired=False, refused=True, deferred=False,
                reason="binding_generation_unknown", receipt_id=None, execution=None,
                binding_id=request.authority.binding_id,
            )
        try:
            out = self._journal.effect(run.run_id, run.effect_id, digest=self._digest_of(request),
                                       fn=call, needs_decision=decided, current_generation=gen)
        except Exception as e:  # noqa: BLE001 — the gate never raises
            if not called:
                _log.error("efferent gate: run journal FAILED before the effect (%s): %s — nothing ran",
                           type(e).__name__, e)
                return GateOutcome(
                    posture=posture, fired=False, refused=True, deferred=False,
                    reason=f"journal_error:{type(e).__name__}", receipt_id=None, execution=None,
                    binding_id=request.authority.binding_id,
                )
            _log.error("executor %s on %r: outcome UNKNOWN (%s: %s) — poisoned, never retried",
                       getattr(self._executor, "name", "?"), request.action_name, type(e).__name__, e)
            return ExecutionResult(ok=False, error=f"outcome_unknown:{type(e).__name__}: {e}")
        if out.status is EffectStatus.DONE:
            return _execution_from_record(out.result.get("execution") if isinstance(out.result, dict) else None)
        if out.status is EffectStatus.REPLAYED:
            return self._replayed(request, out, created_at)
        return self._journal_stop(request, out, posture)

    def _replayed(self, request: ActionRequest, out: EffectOutcome, created_at: str) -> GateOutcome:
        """The outcome for an effect that already ran in this run: the RECORDED result, the executor
        not called. If no receipt was noted for it (the process stopped between the effect and its
        receipt, or the receipt persist failed), the receipt is written now from the recorded fields:
        a receipt that did not land is written on the next delivery (at least once, not exactly once:
        two concurrent deliveries can both write it)."""
        rec = out.result if isinstance(out.result, dict) else {}
        execution = _execution_from_record(rec.get("execution"))
        recorded_posture = rec.get("posture")
        posture = (Posture[recorded_posture] if isinstance(recorded_posture, str)
                   and recorded_posture in Posture.__members__ else Posture.REFUSE_ESCALATE)
        receipt_id = out.receipt_id
        if receipt_id is None and request.run is not None:
            try:
                face = build_gate_face(
                    context_id=request.context_id, query_text=request.query_text,
                    query_date=request.query_date, producers=request.producers,
                    gate=build_gate_verdict(verdict=str(rec.get("verdict")), by=str(rec.get("by")),
                                            binding_id=request.authority.binding_id),
                    authority=request.authority, terminal_state="fired",
                    downstream_id=execution.downstream_id, actor_first_estimate=request.actor_first_estimate,
                )
            except Exception as e:  # noqa: BLE001 — the effect already ran; a receipt fault is not fatal
                _log.error("efferent gate: replay receipt face FAILED (%s): %s", type(e).__name__, e)
            else:
                receipt_id = self._persist(
                    created_at=created_at, action_name=request.action_name, proposal_id=request.proposal_id,
                    posture=posture, fired=execution.ok, face=face,
                )
                if receipt_id is not None:
                    self._note_receipt(request.run, receipt_id)
        return GateOutcome(
            posture=posture, fired=execution.ok, refused=False, deferred=False,
            reason="replayed: the effect already ran in this run", receipt_id=receipt_id,
            execution=execution, binding_id=request.authority.binding_id, replayed=True,
        )

    def _note_receipt(self, run: RunRef, receipt_id: str) -> None:
        try:
            self._journal.note_receipt(run.run_id, run.effect_id, receipt_id)  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001 — fail-soft: a replay writes the receipt again at worst
            _log.error("efferent gate: could not note receipt %s in the run journal (%s): %s",
                       receipt_id, type(e).__name__, e)

    def _cancel_run(self, request: ActionRequest, reason: str) -> None:
        """End the run of a journaled effect the gate decided not to fire, so a re-delivered event
        cannot reach a different decision for it (the monitor reads world state). Fail-soft: a journal
        that cannot be written runs no effect either."""
        if request.run is None or self._journal is None:
            return
        try:
            self._journal.cancel(request.run.run_id, reason=reason)
        except Exception as e:  # noqa: BLE001
            _log.error("efferent gate: could not cancel run %s (%s): %s", request.run.run_id,
                       type(e).__name__, e)

    def _persist(self, *, created_at, action_name, proposal_id, posture, fired, face) -> str | None:
        """Append the gate-receipt FAIL-SOFT (L1-HIGH-1). A receipt-persist failure (a disk error, or
        a non-serializable value that slipped past ``build_gate_face``'s coercion) must NOT raise into
        the caller (the gate's "never raises" contract) NOR undo an already-fired effect — it degrades
        to ``receipt_id=None`` (the decision was made; only the trace didn't land). Logged at ERROR so
        a silent loss of the audit trace is visible."""
        try:
            return self._store.append(
                created_at=created_at, action_name=action_name, proposal_id=proposal_id,
                posture=posture, fired=fired, action_face=face,
            )
        except Exception as e:  # noqa: BLE001 — fail-soft: the receipt persist is never the thing that crashes the gate
            _log.error(
                "efferent gate: receipt persist FAILED (%s): %s — decision MADE, trace NOT recorded",
                type(e).__name__, e,
            )
            return None

    def _safe_execute(self, action_name: str, payload: str, context_id: str) -> ExecutionResult:
        """Run the injected Executor, catching ANY exception AND validating the return SHAPE
        (defense-in-depth — the Executor contract forbids both, but a buggy adapter must never crash
        the gate; fail-soft). A non-:class:`ExecutionResult` return (e.g. ``None``) is coerced to a
        failure rather than left to ``AttributeError`` on ``.downstream_id`` downstream (codex L3 HIGH-3)."""
        try:
            result = self._executor.execute(action_name, payload, context_id=context_id)
        except Exception as e:  # noqa: BLE001 — defense-in-depth fail-soft
            _log.error(
                "executor %s RAISED on %r (%s): %s",
                getattr(self._executor, "name", "?"), action_name, type(e).__name__, e,
            )
            return ExecutionResult(ok=False, error=f"{type(e).__name__}: {e}")
        if not isinstance(result, ExecutionResult):
            _log.error(
                "executor %s returned a non-ExecutionResult (%s) — coercing to failure",
                getattr(self._executor, "name", "?"), type(result).__name__,
            )
            return ExecutionResult(ok=False, error=f"invalid_executor_result:{type(result).__name__}")
        return result

    def _safe_propose(self, proposal: ConfirmProposal) -> bool:
        """Surface a proposal via the transport, fail-soft (the transport contract forbids raising,
        but a buggy adapter must never crash the gate; a raise is treated as "did not surface")."""
        try:
            return bool(self._transport.propose(proposal))  # type: ignore[union-attr]  # guarded by caller
        except Exception as e:  # noqa: BLE001 — defense-in-depth fail-soft
            _log.error("confirm transport %s RAISED on propose (%s): %s",
                       getattr(self._transport, "name", "?"), type(e).__name__, e)
            return False

    def _safe_summary(self, request: ActionRequest) -> str:
        """Render the operator-facing summary, fail-soft to the generic default (a buggy injected
        ``summarize`` must never crash a propose)."""
        try:
            return self._summarize(request)
        except Exception as e:  # noqa: BLE001 — fail-soft: a bad summarizer degrades, never crashes
            _log.error("efferent gate: summarize RAISED (%s): %s — using default", type(e).__name__, e)
            return _default_summary(request)

    def _expires_at(self, created_at: str) -> str | None:
        """Compute the silence-default instant = ``created_at`` + the confirm window. Fail-soft: if
        ``created_at`` can't be parsed (a custom clock), return ``None`` (wait indefinitely) rather
        than crash the propose."""
        try:
            base = _dt.datetime.fromisoformat(created_at)
        except ValueError:
            return None
        return (base + _dt.timedelta(seconds=self._confirm_window_s)).isoformat()

    def _expired(self, pending: PendingAction, now: _dt.datetime) -> bool:
        """True iff ``pending`` is past its ``expires_at``. No ``expires_at`` ⇒ never auto-resolves.
        Compares aware-to-aware / naive-to-naive defensively (a parse/compare failure ⇒ NOT expired —
        fail toward leaving it for an explicit human reply, never auto-fire on a clock fault)."""
        if not pending.expires_at:
            return False
        try:
            exp = _dt.datetime.fromisoformat(pending.expires_at)
            # align tz-awareness so the comparison never raises (TypeError on aware-vs-naive)
            if (exp.tzinfo is None) != (now.tzinfo is None):
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=now.tzinfo)
                else:
                    now = now.replace(tzinfo=exp.tzinfo)
            return now >= exp
        except (ValueError, TypeError) as e:
            _log.warning("efferent gate: bad expires_at %r on pending %s (%s) — not expiring",
                         pending.expires_at, pending.pending_id, type(e).__name__)
            return False

    def _posture_of(self, pending: PendingAction) -> Posture | None:
        """Reconstruct the resolved Posture from the pending record, or ``None`` for an unknown/corrupt
        posture name (L3 codex MED). A corrupt governance record gets NO execution path — the caller
        DROPS/refuses it; it is NOT normalized into an executable rung (the prior fail-up to
        ``CONFIRM_ELEVATED`` would, with a typed proof, still execute a record whose governance state is
        broken). Distinct from §1.3 fail-toward-involvement, which is for a VALID-but-uncertain trigger;
        a corrupt record is not uncertain, it is invalid."""
        try:
            return Posture[pending.posture]
        except KeyError:
            _log.warning("efferent gate: unknown/corrupt posture %r on pending %s — DROP (no execution path)",
                         pending.posture, pending.pending_id)
            return None

    def _authority_of(self, pending: PendingAction) -> AuthorityScope:
        """Reconstruct the AuthorityScope from the pending record; fail-soft to a manual human grant
        on a malformed record (the proposed action was human-grantable by construction — it reached
        the confirm rung)."""
        try:
            a = pending.authority
            return AuthorityScope(
                grantor=str(a["grantor"]), grant=str(a["grant"]),
                binding_id=a.get("binding_id"), hops=int(a.get("hops", 0)),
            )
        except (KeyError, TypeError, ValueError) as e:
            _log.warning("efferent gate: malformed authority on pending %s (%s) — using manual grant",
                         pending.pending_id, type(e).__name__)
            return AuthorityScope(grantor="human", grant="manual-invocation", binding_id=None, hops=0)
