"""levain.autonomic.firepath — the binding FIRE-PATH dispatcher (Slice 4a, single-link).

The binding layer (3a-3c) compiles, seals, reviews, and HARDENS a standing grant — but nothing
FIRES it. This is the dispatcher that closes the loop: an event arrives → matching ACTIVE bindings
re-validate their predicate against it → fire the SAME efferent gate Slices 1-2 fire manually, now
with NO human in the room. It is where everything built so far gets CONSUMED — ``list_active``, the
sealed posture, ``binding_invocation``, ``effective_guard``, the kill substrate, the prediction
monitor, the receipt.

**Slice 4a is SINGLE-LINK only.** A binding whose goal is a chain (>1 ``SubGoal``) is DEFERRED — the
per-link chain executor (the gate travelling WITH the chain, pausing at the riskiest link) is Slice
4b. 4a fires a length-1 grant end-to-end and stops there (the May-3 cut-at-seams watch-item).

The membrane (the anti-cycle rule, held by construction): this module imports anneal-free vagus
internals + the INJECTED seams ONLY. The deterministic predicate evaluator is INJECTED
(``predicate_match`` — flow wires ``events.predicate_match``); the core NEVER imports flow's
``events.py`` (the dependency arrow stays down). The request's action-specific shape (the action name,
the payload rendered from the event, the trust fossil) is the adapter's ``request_builder`` seam — but
the GOVERNANCE-critical fields (the authority, the kill predicates, the predicted trajectory, the
ratified-posture floor, the trigger event, AND the risk) are OVERLAID here from the SEALED binding, so
the adapter's request_builder cannot forge them. The ONE field still adapter-sourced is ``trust`` (the
fire-time fossil — the core is corpus-agnostic, it cannot derive flow's signal-auth mapping); it sits
in the adapter's TCB, and the unforgeable safety backstop for any too-trusting value is the
``ratified_posture`` floor (overlaid from the SEALED binding) — no adapter value can fire BELOW the
rung the human ratified.

The four design seams (resolved at the 4a build, `projects/vagus/slice4_scope.md`):
  1. **Risk = the sealed tools, OVERLAID by the core.** The gate's ``binding_risk`` derives the
     binding's risk from its SEALED binding ALONE (the SAME aggregation it was ratified at); the
     dispatcher OVERLAYS ``risk = gate.binding_risk(binding, 0)`` (the request_builder's risk is ignored), so
     the gate resolves against the sealed-tool risk — structurally BOUND to the seal, never an
     adapter-chosen label (codex/complement/nemotron L3 made this an overlay, not a convention).
  2. **Fail-UP.** The dispatcher floors the gate at the binding's SEALED posture
     (``ActionRequest.ratified_posture``) — the gate fires at ``max(re-resolve, ratified)``, never
     below the rung the human ratified. The structural backstop for the adapter-sourced ``trust``.
  3. **Trust fossil.** The adapter builds the request trust the way the compiler did (intent-free,
     human-absent); the dispatcher overlays authority/kill/trajectory/ratified/trigger/risk, leaving
     trust as the adapter's (TCB) — backstopped by seam #2.
  4. **Injected seams.** ``predicate_match`` + ``request_builder`` + the ``BindingStore`` + the
     ``EfferentGate`` + a ``clock`` are all injected; the live event SOURCE (Argus's stream) is an
     adapter/adoption concern — 4a dispatches a single supplied event.

**The run journal (S8).** The gate must carry a :class:`~levain.autonomic.journal.RunJournal`, the
binding store's own (:class:`FireDispatcher` refuses anything else): every dispatch of a binding on an event is a journaled RUN: its id is a content address over the binding and
the exact event (:func:`~levain.autonomic.journal.run_id_for`), it is ADMITTED by
:meth:`~levain.autonomic.binding.BindingStore.admit` (the fireability check, the admission under the
binding's current generation and a one-shot's claim, in one step under the store lock that every fencing
verb also takes), and each link's effect runs at most once. The contract with the event source: deliver
at least once, and give two distinct events distinct content (an ``id``): the run id is the event's
content address, so two deliveries with identical content are ONE run and the second does not fire. Delivering an event again is safe (a done effect replays its recorded result
and records no new graduation evidence), and it is how a HELD dispatch (a decision open on the
binding, see :attr:`GateOutcome.held`) is resumed once the decision is made.

Stdlib-only core; pure of I/O beyond the injected stores' own reads/writes.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import logging
from collections.abc import Callable
from typing import Any

from levain.autonomic.binding import Binding, BindingStore, binding_invocation
from levain.autonomic.chainpath import ChainExecutor, ChainOutcome
from levain.autonomic.executor import ActionRequest
from levain.autonomic.gate import EfferentGate, GateOutcome
from levain.autonomic.journal import RunRef, run_id_for
from levain.autonomic.monitor import guard_trajectory
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk

__all__ = ["FireDispatch", "FireDispatcher"]

_log = logging.getLogger("levain.autonomic.firepath")

# The seams the adapter injects.
#   PredicateMatch: (pattern, event) -> bool. MUST re-validate the (untrusted, stored) predicate and
#     raise on a malformed one — the dispatcher cannot trust a stored predicate was ever validated, so
#     a raise = "this binding's predicate is not evaluable" → the binding is SKIPPED (fail-closed,
#     never fires on an unvalidatable predicate). flow wires ``events.predicate_match``.
#   RequestBuilder: (binding, event) -> ActionRequest. Builds the ACTION-specific shape (action_name,
#     the payload rendered from the event, the trust fossil, the receipt provenance). The dispatcher
#     OVERLAYS the governance-critical fields afterward — including ``risk`` (below), so the
#     request_builder's ``risk`` is IGNORED (the binding's risk resolver derives it, structurally).
#   The binding's risk resolver is the GATE's ``binding_risk``: (binding, link_index) -> ActionRisk,
#     from the SEALED binding ALONE (flow wires ``_aggregate_risk(binding.goal, TOOL_RISK_MANIFEST)`` —
#     the same aggregation the binding was ratified at). A SEPARATE seam from the request_builder (which
#     also sees the event) so risk derivation is structurally BOUND to the sealed binding and cannot be
#     event-derived/forged (seam #1, codex/complement/nemotron L3). It is the gate's, not the
#     dispatcher's, so a proposal is made from the same resolver its resolve re-derives with.
PredicateMatch = Callable[[dict[str, Any], dict[str, Any]], bool]
RequestBuilder = Callable[[Binding, dict[str, Any]], ActionRequest]


@dataclasses.dataclass(frozen=True)
class FireDispatch:
    """The result of the dispatch deciding on ONE binding that MATCHED a dispatched event. ``outcome`` is
    the REPRESENTATIVE :class:`GateOutcome` — for a SINGLE-link binding (4a) it is the gate's full ruling
    (``fired`` / ``killed`` / ``refused`` / ``deferred`` / ``pending``); for a MULTI-link binding (4b
    chain) it is the LAST link the chain ruled on (the paused link's pending, the terminal link's fire,
    or the aborting link's outcome), and ``chain`` carries the full :class:`ChainOutcome` (the per-link
    rulings + the chain state). ``chain`` is ``None`` for a single-link binding. Bindings that did NOT
    match (or were skipped — a one-shot that lost the claim race, a binding whose predicate failed to
    validate, a chain with no executor wired) are LOGGED but NOT returned here."""

    binding_id: str
    outcome: GateOutcome
    chain: ChainOutcome | None = None


def _kill_predicates(binding: Binding) -> tuple[dict[str, Any], ...]:
    """Every KNOWN-danger kill the runtime evaluates: the ``effective_guard`` (sealed floor + unsealed
    tightening additions) kill predicates. More "stop if X" is strictly safer, so the fire-path honors
    the additions too (the confirm-class MANDATE is the sealed floor's job — ``list_active`` — not
    this; this is the runtime evaluation set)."""
    return tuple(g.kill_predicate for g in binding.effective_guard if g.kill_predicate is not None)


class FireDispatcher:
    """Dispatch an event to the bindings it could fire (Slice 4a, single-link). Construct with the
    binding registry, the wired efferent gate, the injected ``predicate_match`` (the deterministic
    evaluator the core never imports), ``request_builder`` (the adapter's action fossil), and a ``clock`` (for the
    ``record_fire`` dormancy timestamp — there is no ambient clock in the core)."""

    def __init__(
        self,
        *,
        store: BindingStore,
        gate: EfferentGate,
        predicate_match: PredicateMatch,
        request_builder: RequestBuilder,
        clock: Callable[[], _dt.datetime],
        chain_executor: ChainExecutor | None = None,
    ) -> None:
        self._store = store
        self._gate = gate
        self._predicate_match = predicate_match
        self._request_builder = request_builder
        self._clock = clock
        # The Slice-4b chain executor: a MULTI-link binding (``len(goal) > 1``) is delegated to it AFTER
        # the dispatcher re-acquires the fresh fireable snapshot + claims a one-shot (the chain IS the
        # one fire). None ⇒ multi-link bindings are SKIPPED + logged (a 4a-only wiring) — never
        # half-fired (the May-3 cut-at-seams: 4a fires only single-link).
        self._chain_executor = chain_executor
        # A binding fire is journaled, and the gate fences and admits through the store: both must use
        # the SAME journal, or a pause would fence a journal the runs are not admitted into.
        if gate.journal is None or getattr(store, "journal", None) is not gate.journal:
            raise ValueError("FireDispatcher: the gate needs a run journal, and it must be the binding "
                             "store's journal")
        if gate.binding_risk is None:
            raise ValueError("FireDispatcher: the gate needs binding_risk (the binding's risk resolver), "
                             "to derive a binding's risk when it fires and when its pending is resolved")
        if chain_executor is not None and chain_executor.gate is not gate:
            raise ValueError("FireDispatcher: the chain executor must fire through the same gate")

    def _binding_risk(self, binding: Binding) -> ActionRisk:
        """The single link's risk, from the gate's ``binding_risk`` (non-None: checked at construction)."""
        resolver = self._gate.binding_risk
        assert resolver is not None
        return resolver(binding, 0)

    def dispatch(self, event: dict[str, Any]) -> list[FireDispatch]:
        """Fire every ACTIVE binding the ``event`` matches. For each candidate from
        ``list_active(trigger_type=event.type)``: RE-VALIDATE the stored predicate against the event
        (a malformed/non-matching predicate skips the binding, fail-closed); for a single-link match,
        build the request (adapter) + overlay the sealed-binding governance (authority / kill / trajectory
        / ratified-posture floor / trigger event), CLAIM a one-shot atomically before firing, gate it,
        and bookkeep the fire. Returns the per-binding gate outcomes (matched bindings only).

        NEVER raises — each binding is dispatched in its own fail-soft frame (one bad binding never
        stops the rest, mirroring the gate's ``sweep_timeouts``); the gate itself never raises."""
        if not isinstance(event, dict):
            _log.warning("firepath dispatch: event is %s, not a dict — nothing to dispatch", type(event).__name__)
            return []
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            _log.warning("firepath dispatch: event has no string 'type' — cannot match bindings")
            return []

        try:
            candidates = self._store.list_active(trigger_type=event_type)
        except Exception as e:  # noqa: BLE001 — a store read fault must not crash the dispatch
            _log.error("firepath dispatch: list_active(%r) FAILED (%s): %s", event_type, type(e).__name__, e)
            return []

        candidates += self._resumable_one_shots(event, event_type)

        results: list[FireDispatch] = []
        for binding in candidates:
            try:
                dispatched = self._dispatch_one(binding, event)
            except Exception as e:  # noqa: BLE001 — one bad binding never stops the dispatch
                _log.error("firepath dispatch: binding %s FAILED (%s): %s — skipped",
                           binding.binding_id, type(e).__name__, e)
                continue
            if dispatched is not None:
                results.append(dispatched)
        return results

    def _resumable_one_shots(self, event: dict[str, Any], event_type: str) -> list[Binding]:
        """One-shots already CLAIMED for a run of THIS event: ``list_active`` leaves
        them out (a claimed one-shot is REVOKED), but a re-delivery must reach them so the run can
        finish (done effects replay; an approved effect runs once). Admission re-checks everything
        (:meth:`BindingStore.admit`); a person's revoke after the claim fences the run."""
        out: list[Binding] = []
        try:
            for b in self._store.list_all(trigger_type=event_type):
                if (b.one_shot and not b.status.is_active
                        and self._store.claimed_run(b.binding_id) == run_id_for(b.binding_id, event)):
                    out.append(b)
        except Exception as e:  # noqa: BLE001 — a lookup fault resumes nothing (fail closed)
            _log.error("firepath dispatch: resumable one-shot lookup FAILED (%s): %s", type(e).__name__, e)
            return []
        return out

    def _dispatch_one(self, binding: Binding, event: dict[str, Any]) -> FireDispatch | None:
        """Dispatch ONE candidate binding against the event, or ``None`` if it is skipped (no match, a
        chain with no executor wired, a one-shot that lost the claim race). Single-link → the gate fires
        directly (4a); multi-link → the chain executor walks it (4b). Raises only on a genuine internal
        fault (caught by ``dispatch``'s per-binding net)."""
        # 1. RE-VALIDATE the stored predicate against the event (the dispatcher cannot trust a stored
        # predicate was ever validated — cut 2). A raise = the predicate is not evaluable → SKIP
        # (fail-closed: never fire on an unvalidatable predicate). A non-match → SKIP (no fire).
        try:
            matched = self._predicate_match(binding.trigger.pattern, event)
        except Exception as e:  # noqa: BLE001 — a malformed stored predicate bars the binding, fail-closed
            _log.warning("firepath: binding %s predicate did not VALIDATE (%s): %s — barred from firing",
                         binding.binding_id, type(e).__name__, e)
            return None
        if not matched:
            return None

        # 2. a CHAIN (>1 SubGoal) with NO executor wired is SKIPPED *before* the claim — don't spend a
        # one-shot on a chain a 4a-only dispatcher can't run (the goal is sealed → the candidate's
        # link-count is stable; checking it pre-claim preserves the 4a "don't spend on what you skip").
        if len(binding.goal) > 1 and self._chain_executor is None:
            _log.info("firepath: binding %s matched but is a %d-link CHAIN and no chain executor is wired "
                      "— skipped (this dispatcher fires only single-link)", binding.binding_id, len(binding.goal))
            return None

        # 3. RE-ACQUIRE a FRESH fireable snapshot IMMEDIATELY before firing (L3 stale-snapshot close).
        # ``list_active`` was a lockless, possibly-stale read; a concurrent pause/revoke/seal-break (the
        # human STOPPING the grant) or a tighten (ADDING a kill) could have committed since. Re-acquire
        # the current binding and build EVERYTHING governance-critical from it, so a revoked grant does
        # not fire and a freshly-added kill IS evaluated. The P match above used the SEALED pattern
        # (stable for the id; a pattern change is a re-ratification → a different id → this re-acquire
        # returns None on the old id), so matching the candidate snapshot first is safe — and it means a
        # one-shot is NOT claimed/spent on a non-matching event. For a CHAIN, the claim here spends the
        # one-shot as the chain's ONE fire (the whole chain is one fire of the grant). (The residual
        # window — this re-acquire → the gate fire — is irreducible; the lock can't span the gate's I/O.)
        # 3a. the run journal: derive the run id (pure, before anything is spent: an event that cannot be
        # addressed skips the binding without claiming a one-shot), then ADMIT it through the store:
        # the fresh fireability check, the admission under the current generation and a one-shot's
        # claim happen in one step under the store lock, which every fencing verb also holds, so a
        # pause or tighten is ordered entirely before the admission or entirely after it.
        # 2b. no binding effect can run until an executor can prove it runs through the confinement floor
        # (none can yet): refused HERE, before the run is admitted and before a one-shot is claimed, so
        # nothing is spent on an effect that cannot run.
        refusal = self._gate.binding_effect_refusal()
        if refusal is not None:
            _log.error("firepath: binding %s matched but not admitted (%s)", binding.binding_id, refusal)
            return FireDispatch(binding_id=binding.binding_id, outcome=GateOutcome(
                posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False, reason=refusal,
                receipt_id=None, execution=None, binding_id=binding.binding_id))

        one_shot = binding.one_shot
        try:
            run_id = run_id_for(binding.binding_id, event)
        except ValueError as e:
            _log.warning("firepath: binding %s — the event is not canonical JSON (%s); a run cannot be "
                         "addressed, so it does not fire", binding.binding_id, e)
            return None
        fresh = self._store.admit(binding.binding_id, run_id)
        if fresh is None:
            _log.info("firepath: binding %s no longer fireable at fire-time (revoked/paused/seal-broken/"
                      "spent since list_active) — not firing", binding.binding_id)
            return None

        # 4. branch: MULTI-link → the chain executor (the gate travels with the chain); SINGLE-link →
        # the gate fires directly (4a). Both operate on the FRESH (claimed) snapshot.
        if len(fresh.goal) > 1:
            return self._dispatch_chain(fresh, event)
        return self._dispatch_single_link(fresh, event, one_shot=one_shot, run_id=run_id)

    def _dispatch_single_link(self, fresh: Binding, event: dict[str, Any], *, one_shot: bool,
                              run_id: str) -> FireDispatch:
        """The Slice-4a single-link fire-path: build the ACTION-specific request (adapter), OVERLAY the
        governance-critical fields from the FRESH SEALED binding so the adapter cannot forge them
        (``risk`` seam #1, the authority, the effective_guard kills, the prediction trajectory, the
        ratified-posture floor, the trigger event), gate it, and bookkeep an immediate standing fire.
        ``binding_invocation`` refuses a non-active binding (defense in depth); ``fresh`` is the active
        snapshot (the one-shot claim returns its pre-claim ACTIVE form), so it mints cleanly; hops=0 for
        a single link (the binding fires directly on the trigger)."""
        base = self._request_builder(fresh, event)
        request = dataclasses.replace(
            base,
            risk=self._binding_risk(fresh),
            authority=binding_invocation(fresh, hops=0),
            kill_predicates=_kill_predicates(fresh),
            predicted_trajectory=guard_trajectory(fresh.effective_guard),
            ratified_posture=fresh.posture,
            trigger_event=event,
            run=RunRef(run_id, "link-0"),
        )

        # fire the gate (it decides FIRE / KILL / PROPOSE / REFUSE and writes the receipt; it never
        # raises). The known-danger kill (request.kill_predicates vs the trigger event) + the
        # prediction-error monitor (request.predicted_trajectory vs the injected observer) run INSIDE it.
        outcome = self._gate.gate(request)

        # bookkeeping: record an IMMEDIATE FIRE on a STANDING binding (the §2.6 graduation evidence + the
        # dormancy sensor; UNSEALED, untrusted). NOT for a one-shot (revoked/spent), NOT for a non-fire.
        #   ✅ Slice 4c: ``clean`` is now DERIVED from the terminal outcome (``outcome.is_clean_fire`` —
        #   fired with no kill/deny/refuse), not hardcoded (gap #2). The OUT-OF-BAND counterpart — a
        #   COOLING_OFF/CONFIRM binding whose actual fire happens at the sweep/resolve (where the gate
        #   holds no BindingStore) — is recorded by the ADAPTER via ``GateOutcome.binding_id`` (gap #1);
        #   a CHAIN's completion is recorded by the chain executor. This immediate path is the on-loop
        #   (``fires_immediately``) single-link fire. Fail-soft.
        # A REPLAYED outcome is the record of an earlier fire, not a new one: counting it again would
        # inflate the unsealed evidence that loosens autonomy.
        if outcome.fired and not one_shot and not outcome.replayed:
            try:
                self._store.record_fire(fresh.binding_id, clean=outcome.is_clean_fire,
                                        fired_at=self._clock().isoformat())
            except Exception as e:  # noqa: BLE001 — the effect fired; a bookkeeping fault is not fatal
                _log.error("firepath: record_fire(%s) FAILED (%s): %s — fired, bookkeeping not recorded",
                           fresh.binding_id, type(e).__name__, e)

        return FireDispatch(binding_id=fresh.binding_id, outcome=outcome)

    def _dispatch_chain(self, fresh: Binding, event: dict[str, Any]) -> FireDispatch:
        """The Slice-4b chain fire-path: delegate the FRESH (claimed) multi-link binding to the injected
        chain executor, which walks the goal sequence with the gate travelling with it (per-link posture,
        pause-at-the-first-confirm-class-link, the inter-link data-flow). The chain executor NEVER raises.

        Slice 4c — the §2.6 graduation evidence for a COMPLETED chain is recorded by the CHAIN EXECUTOR
        (``ChainExecutor._walk``'s completed branch), which is the SINGLE locus for both THIS immediate
        completion AND an out-of-band ``resume`` completion (so a chain that pauses at cooling-off/confirm
        and completes later via sweep/resume accrues evidence too — the carried 4a/4b out-of-band gap,
        now closed). The dispatcher no longer records here — that would DOUBLE-count. The executor derives
        ``clean`` from the per-link outcomes + skips one-shots itself."""
        assert self._chain_executor is not None  # _dispatch_one only routes here with an executor wired
        chain_outcome = self._chain_executor.execute(fresh, event)
        return FireDispatch(binding_id=fresh.binding_id, outcome=self._chain_representative(chain_outcome),
                            chain=chain_outcome)

    @staticmethod
    def _chain_representative(chain: ChainOutcome) -> GateOutcome:
        """The representative :class:`GateOutcome` for a chain dispatch (FireDispatch.outcome) — the LAST
        link the chain ruled on (the paused link's pending / the terminal link's fire / the aborting
        link's outcome). When NO link ran (a consistency-barred pre-flight), synthesize a refused
        representative carrying the bar reason; the full per-link detail lives in ``FireDispatch.chain``."""
        if chain.links:
            return chain.links[-1].outcome
        return GateOutcome(posture=Posture.REFUSE_ESCALATE, fired=False, refused=True, deferred=False,
                           reason=chain.reason, receipt_id=None, execution=None)
