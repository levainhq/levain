"""levain.autonomic.chainpath — the CHAIN EXECUTOR (Slice 4b, multi-link).

4a fires a length-1 grant end-to-end (``firepath.FireDispatcher``). 4b is the genuinely-new
structure: a binding whose ``goal`` is an ordered chain of ``SubGoal``s walks its sequence with **the
GATE TRAVELLING WITH THE CHAIN** — on-loop links fire immediately, the chain PAUSES at the first
confirm-class link, and link N-1's output threads into link N. This module is (a) the per-link LOOP,
(b) the per-link GOVERNANCE derivation, (c) the inter-link DATA-FLOW, and (d) the pause/resume
PERSISTENCE. The gate (``EfferentGate``) stays a single-action engine — this orchestrates it
link-by-link; no gate change.

The §A sequential twist (``awareness_bus_governance_design.md``): you cannot pre-approve a chain whose
later steps you haven't seen, because each step's output determines the next step's inputs. So a chain
with a ``confirm`` link **pauses AT that link** — the gate does NOT resolve the whole chain at
``max(member postures)`` up front. Each link resolves at its OWN posture; the chain runs forward,
firing on-loop links, until the first confirm-class link, which PROPOSES (PENDING) + persists a
:class:`ChainState`; on approve the link fires and the chain RESUMES at the next link (which may pause
again). The §2.3 set-risk ``max`` is honoured NATURALLY: ``binding.posture`` (sealed) ==
``max`` over per-link postures, so the chain pauses at the riskiest link organically.

§B (trigger-distance-degrades-grounding): link N is N agentic hops from the trigger — distance from a
human is distance from grounding. Each link's trust carries ``hops=link_index`` (the per-link
``trust_resolver``), so the posture climbs the deeper the chain, and the outbound tail confirms.

The seams (committed in ``projects/vagus/slice4_scope.md`` § "4b — the chain executor"):
  1. **Per-link risk** — an injected ``risk_resolver(binding, link_index)`` derives link i's risk from
     its SEALED tools alone (``aggregate_risk((goal[i],), manifest)``). Core-overlaid; fail-closed.
  2. **Per-link posture + the seam-#2 fail-UP (the TERMINAL-link ratified floor)** — INTERMEDIATE links
     fire at their natural ``policy(risk_i, trust_i)`` (no floor — §A's early-links-fire); the LAST link
     carries ``ratified_posture = binding.posture`` (the sealed whole-chain rung), so the gate fires it
     at ``max(policy, binding.posture)``. Since ``binding.posture >= every per-link posture`` (the §2.3
     set-risk max — proven EXACT), the chain can never COMPLETE below the ratified rung. This HONORS a
     requested-posture upgrade (the compiler seals ``max(resolved, requested)``; the terminal pauses at
     the requested rung — vs the old pre-flight ``max_per_link < binding.posture`` bar, which spuriously
     barred an upgrade), catches WHOLESALE drift at the completion point, and unifies with 4a (a single
     link IS the terminal link). A fail-closed pre-flight still bars an UNCLASSIFIABLE link (undeclared
     tool) before any link fires. ⚠ Residual (a single MIDDLE link's manifest lowering, masked by
     another link supplying the max) is a schema limitation — per-link sealed postures are a 4c/forward
     evolution; per-link risk derives from SEALED tools so the only drift vector is a reviewed code edit.
  3. **Inter-link data-flow** — a typed :class:`ChainContext` threads the trigger event + each
     completed link's ``{payload, downstream_id, detail}`` forward; the adapter's ``request_builder``
     reads what link i needs (the OUTPUT-routing is the adapter's fossil; the core threads it opaque).
  4. **Pause/resume persistence** — :class:`ChainState` (the PRE-CLAIM ACTIVE binding snapshot + the
     completed outputs + the paused link + the hold it belongs to), content-SEALED and written INTO the
     pausing link's hold in the run journal (see below). ``resume`` reads it from the hold →
     ``gate.resolve`` (one write-once decision) → continue from ``paused_at+1``. A completed link is
     NEVER re-run; a tampered state rejects the hold.
  5. **hops per link** — an injected ``trust_resolver(binding, link_index)`` carries ``hops=link_index``
     (§B); authority per link = ``binding_invocation(snapshot, hops=link_index)``.

⚠ CHAIN-OF-DISSENT DELEGATION GAP (forward-compat): 4b's chains are DIRECT-ACTION chains — every link
carries the WRAPPER binding's own ``effective_guard`` kills (evaluated against the IMMUTABLE original
trigger event per link — idempotent + safe; a kill aborts the chain). A link that DELEGATES to another
binding's authority is OUT OF SCOPE and the adapter's action-resolver BARS it (never run at the
wrapper's guard with the inner binding's guard stripped). A future delegation slice MUST union the
inner binding's ``effective_guard`` onto that link's kill set — never defang it.

Carve-outs (the May-3 cut-at-seams): NO graduation (4c — incl. the carried cooling-off/confirm
sweep-evidence ``record_fire`` gap), NO staleness (4d). The per-link ``predicted_trajectory`` stays
4a-equivalent (the binding's first guard's envelope) — link-indexed trajectories need a guard-schema
evolution (flagged, not built).

**The run journal (S8).** The gate must carry a run journal (:class:`ChainExecutor` refuses one without):
a chain is ONE journaled run (the dispatcher
admits it; its id is :func:`~levain.autonomic.journal.run_id_for` over the binding and the trigger
event, so a resume and a re-delivery address the same run) and link ``i`` is its effect ``link-<i>``. A pausing link's chain state is written INTO the link's
hold with its pending (the request's ``continuation``); resuming reads it from there, so a chain state
cannot be lost apart from its decision.
A re-delivered event walks the chain again: links that already ran replay their recorded output into
the data flow without running, and the first link that has not run continues the chain. A link HELD by
an open decision on the binding ends this walk in state ``held`` (re-deliver after the decision).

Stdlib-only core; pure of I/O beyond the injected stores' own reads/writes; imports NOTHING from flow
(the anti-cycle rule).
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from levain.autonomic.binding import Binding, BindingStore, binding_invocation
from levain.autonomic.executor import ActionRequest, ExecutionResult
from levain.autonomic.gate import EfferentGate, GateOutcome
from levain.autonomic.journal import RunRef, hold_id_for, run_id_for
from levain.autonomic.monitor import guard_trajectory
from levain.autonomic.risk import ActionRisk
from levain.autonomic.transport import ConfirmDecision
from levain.autonomic.trust import TrustContext

__all__ = [
    "CompletedLink",
    "ChainContext",
    "ChainLinkResult",
    "ChainOutcome",
    "ChainState",
    "ChainExecutor",
    "seal_chain_id",
    # the injected seams (the adapter wires them)
    "ChainRequestBuilder",
    "ChainRiskResolver",
    "ChainTrustResolver",
]

_log = logging.getLogger("levain.autonomic.chainpath")

# Gate refusals of a journaled link's resolve that RECORD NOTHING: the hold is still open, so the chain
# is still paused at that link (a typed proof or a person may still come).
_STILL_OPEN = frozenset({"unattended_approval_not_allowed", "elevated_requires_typed_proof",
                         "run_not_admitted"})


# The seams the adapter injects (per-link variants of the 4a single-link seams).
#   ChainRequestBuilder: (binding, chain_context, link_index) -> ActionRequest. Builds link i's
#     ACTION-specific shape (action_name, the payload rendered from the trigger event + the upstream
#     outputs in the context, the receipt provenance, the deterministic-match confidence). The executor
#     OVERLAYS the governance-critical fields afterward (risk / trust / authority / kills / trajectory /
#     trigger event), so the builder cannot forge them.
#   ChainRiskResolver: (binding, link_index) -> ActionRisk. DERIVES link i's risk from its SEALED tools
#     alone (the adapter wires aggregate_risk((goal[i],), manifest)). Core-overlaid (seam #1).
#   ChainTrustResolver: (binding, link_index) -> TrustContext. DERIVES link i's trust structurally from
#     the trigger type + hops=link_index (§B), human-absent, intent-free. Core-overlaid (seam #5) — so
#     trust is NOT adapter-payload-sourced; it is a pure (binding, index) function the executor controls.
ChainRequestBuilder = Callable[[Binding, "ChainContext", int], ActionRequest]
ChainRiskResolver = Callable[[Binding, int], ActionRisk]
ChainTrustResolver = Callable[[Binding, int], TrustContext]


# =================================================================================================
# The inter-link data-flow (seam #3) — a typed context threaded forward across the chain.
# =================================================================================================
@dataclass(frozen=True)
class CompletedLink:
    """One link that has FIRED, captured for the data-flow + the resume bookkeeping. ``payload`` is
    what the link acted on (e.g. link 0's rendered doc — link 1 emails it); ``downstream_id`` / ``detail``
    are the executor's result (the created/sent thing). All JSON-scalar so the :class:`ChainState` seal
    is canonical."""

    link_index: int
    goal: str
    tools: tuple[str, ...]
    output: str
    payload: str
    downstream_id: str | None
    detail: str

    @classmethod
    def of(cls, link_index: int, subgoal: Any, payload: str, execution: ExecutionResult | None) -> "CompletedLink":
        return cls(
            link_index=link_index,
            goal=subgoal.goal,
            tools=tuple(subgoal.tools),
            output=subgoal.output,
            payload=payload,
            downstream_id=(execution.downstream_id if execution is not None else None),
            detail=(execution.detail if execution is not None else ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_index": self.link_index,
            "goal": self.goal,
            "tools": list(self.tools),
            "output": self.output,
            "payload": self.payload,
            "downstream_id": self.downstream_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CompletedLink":
        idx = d["link_index"]
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            raise ValueError("CompletedLink.link_index must be a non-negative int")
        tools = d["tools"]
        if not isinstance(tools, (list, tuple)) or not all(isinstance(t, str) for t in tools):
            raise TypeError("CompletedLink.tools must be a list of strings")
        return cls(
            link_index=idx,
            goal=str(d["goal"]),
            tools=tuple(tools),
            output=str(d["output"]),
            payload=str(d["payload"]),
            downstream_id=(d.get("downstream_id") if d.get("downstream_id") is None
                           else str(d.get("downstream_id"))),
            detail=str(d.get("detail", "")),
        )


@dataclass(frozen=True)
class ChainContext:
    """The data threaded forward as the chain walks: the immutable original ``trigger_event`` + the
    ordered tuple of links that have FIRED. The adapter's ``request_builder`` reads ``completed`` (e.g.
    ``completed[-1].payload``) to render the next link's input. Immutable — :meth:`with_link` returns a
    new context (never mutates)."""

    trigger_event: dict[str, Any]
    completed: tuple[CompletedLink, ...] = ()

    def with_link(self, link: CompletedLink) -> "ChainContext":
        return ChainContext(trigger_event=self.trigger_event, completed=self.completed + (link,))


# =================================================================================================
# The per-link + chain results.
# =================================================================================================
@dataclass(frozen=True)
class ChainLinkResult:
    """The gate's ruling on ONE link of the chain."""

    link_index: int
    outcome: GateOutcome


@dataclass(frozen=True)
class ChainOutcome:
    """The result of walking a chain (or a resume segment). Exactly one ``state``:
    - ``completed`` — every link fired; the chain is done.
    - ``paused``    — a confirm-class link PROPOSED; ``paused_at`` / ``pending_id`` / ``chain_id`` name
                      the persisted :class:`ChainState` awaiting the operator's resolve.
    - ``aborted``   — a link was KILLED / REFUSED / DEFERRED (no transport) or a link's EFFECT failed, or
                      a denied resume, or a barred ratification-drift; ``reason`` says which, ``paused_at``
                      names the link it stopped at (when applicable). No further links ran.
    - ``held``      — (journaled runs) a link did not run and the chain did not end: a decision is open on
                      the binding, the link's approval stands but its effect has not run yet, or another
                      resolver's decision governs it. ``paused_at`` names it. Re-delivering the event
                      resumes the run.
    ``links`` are the per-link rulings produced in THIS call (the resume segment's rulings on a resume)."""

    binding_id: str
    state: str
    links: tuple[ChainLinkResult, ...]
    paused_at: int | None = None
    pending_id: str | None = None
    chain_id: str | None = None
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.state == "completed"

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @property
    def aborted(self) -> bool:
        return self.state == "aborted"

    @property
    def held(self) -> bool:
        return self.state == "held"


# =================================================================================================
# The pause/resume persistence (seam #4) — a sealed, flock-stored partial-chain-in-flight.
# =================================================================================================
def seal_chain_id(
    *,
    created_at: str,
    binding_id: str,
    binding: dict[str, Any],
    trigger_event: dict[str, Any],
    completed: tuple[dict[str, Any], ...],
    paused_at_link: int,
    paused_payload: str,
    pending_id: str,
) -> str:
    """The content-FINGERPRINT chain id: ``chain-<created_at>-<16hex>`` over the full in-flight state —
    the PRE-CLAIM binding snapshot, the trigger event, the completed-link outputs, the paused link +
    its proposed payload, and the gate's pending_id. Recomputing it (:meth:`ChainState.seal_matches`)
    detects ANY post-pause alteration: a re-ordered chain, a skipped confirm, an injected payload, a
    swapped binding. A mismatch ⇒ the resume DROPS (never continues a tampered chain). Structurally-
    encoded canonical JSON (NOT a delimiter-join — a delimiter-bearing field could reshuffle into an
    equal hash for a different tuple), mirroring ``pending.seal_pending_id``. KEYLESS (the pending /
    binding seal boundary): defends accidental corruption / drift / a buggy writer, not a malicious
    local process that can recompute it."""
    body = {
        "created_at": created_at,
        "binding_id": binding_id,
        "binding": binding,
        "trigger_event": trigger_event,
        "completed": list(completed),
        "paused_at_link": paused_at_link,
        "paused_payload": paused_payload,
        "pending_id": pending_id,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"chain-{created_at}-{h}"


@dataclass(frozen=True)
class ChainState:
    """A partial-chain-in-flight, paused at a confirm-class link awaiting the operator's collapse.
    Carries EVERYTHING needed to RESUME from a fresh process (a CLI ``resume`` / a sweep): the PRE-CLAIM
    ACTIVE ``binding`` snapshot (so authority mints on resume even after a one-shot self-revoke, and the
    chain decouples from registry mutation), the immutable ``trigger_event``, the ``completed`` link
    outputs (the data-flow + the never-re-run record), the ``paused_at_link`` index + its
    ``paused_payload`` (so resume records the paused link as completed without re-running the builder),
    and the gate's ``pending_id`` (the link's persisted PendingAction → ``gate.resolve`` fires it).

    ``chain_id`` is the content fingerprint (:func:`seal_chain_id`); :meth:`seal_matches` detects any
    tamper. The ``binding`` snapshot is a sealed-immutable dict (the binding's own seal still holds);
    the ChainState seal covers it too, so a binding-swap is caught."""

    chain_id: str
    created_at: str
    binding_id: str
    binding: dict[str, Any]
    trigger_event: dict[str, Any]
    completed: tuple[dict[str, Any], ...]
    paused_at_link: int
    paused_payload: str
    pending_id: str

    @classmethod
    def create(
        cls, *, created_at: str, binding: Binding, trigger_event: dict[str, Any],
        completed: tuple[CompletedLink, ...], paused_at_link: int, paused_payload: str, pending_id: str,
    ) -> "ChainState":
        """Mint a SEALED chain state. The ``binding`` is serialized to its dict (the PRE-CLAIM ACTIVE
        snapshot). The ``chain_id`` is the fingerprint over all of it — tamper-evident across the
        pause→resume gap."""
        bdict = binding.to_dict()
        completed_dicts = tuple(c.to_dict() for c in completed)
        cid = seal_chain_id(
            created_at=created_at, binding_id=binding.binding_id, binding=bdict,
            trigger_event=trigger_event, completed=completed_dicts, paused_at_link=paused_at_link,
            paused_payload=paused_payload, pending_id=pending_id,
        )
        return cls(
            chain_id=cid, created_at=created_at, binding_id=binding.binding_id, binding=bdict,
            trigger_event=trigger_event, completed=completed_dicts, paused_at_link=paused_at_link,
            paused_payload=paused_payload, pending_id=pending_id,
        )

    def seal_matches(self) -> bool:
        """True iff the in-flight state is UNALTERED since :meth:`create`."""
        return self.chain_id == seal_chain_id(
            created_at=self.created_at, binding_id=self.binding_id, binding=self.binding,
            trigger_event=self.trigger_event, completed=self.completed,
            paused_at_link=self.paused_at_link, paused_payload=self.paused_payload,
            pending_id=self.pending_id,
        )

    def binding_obj(self) -> Binding:
        """Reconstruct the PRE-CLAIM ACTIVE Binding from the snapshot (raises on a malformed snapshot —
        the caller treats it as unresumable)."""
        return Binding.from_dict(self.binding)

    def completed_links(self) -> tuple[CompletedLink, ...]:
        return tuple(CompletedLink.from_dict(c) for c in self.completed)

    def validate_against_binding(self, binding: Binding) -> str | None:
        """Chain-SEMANTIC validation on resume — beyond the seal's SHAPE check, assert the state is a
        coherent point in THIS binding's chain (a buggy writer can mint a self-consistent SEALED-but-
        invalid state; codex). Returns a reason string, or ``None`` if valid. Checks: ``paused_at_link``
        in range; ``completed`` is EXACTLY indices ``0..paused_at_link-1`` (contiguous, no gaps/dupes);
        each completed link's goal/tools/output MATCHES the binding's sealed goal at that index."""
        n = len(binding.goal)
        if not (0 <= self.paused_at_link < n):
            return f"invalid_paused_index:{self.paused_at_link}/{n}"
        try:
            completed = self.completed_links()
        except (KeyError, TypeError, ValueError) as e:
            return f"completed_unreconstructable:{type(e).__name__}"
        if len(completed) != self.paused_at_link:
            return f"completed_count_mismatch:{len(completed)}!=paused_at:{self.paused_at_link}"
        for pos, link in enumerate(completed):
            if link.link_index != pos:
                return f"non_contiguous_completed_at_{pos}:link_index={link.link_index}"
            sg = binding.goal[pos]
            if link.goal != sg.goal or tuple(link.tools) != tuple(sg.tools) or link.output != sg.output:
                return f"completed_link_{pos}_mismatches_sealed_goal"
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "created_at": self.created_at,
            "binding_id": self.binding_id,
            "binding": self.binding,
            "trigger_event": self.trigger_event,
            "completed": list(self.completed),
            "paused_at_link": self.paused_at_link,
            "paused_payload": self.paused_payload,
            "pending_id": self.pending_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChainState":
        """Reconstruct from a stored record WITHOUT re-deriving the id (the stored ``chain_id`` is kept
        so :meth:`seal_matches` can detect an edited record whose id was left stale — the tamper
        signature). Raises ``KeyError``/``TypeError``/``ValueError`` on a malformed record (the store
        read CATCHES all three + skips loudly)."""
        binding = d["binding"]
        trigger_event = d["trigger_event"]
        completed = d["completed"]
        paused = d["paused_at_link"]
        if not isinstance(binding, dict):
            raise TypeError("ChainState.binding must be a dict")
        if not isinstance(trigger_event, dict):
            raise TypeError("ChainState.trigger_event must be a dict")
        if not isinstance(completed, list):
            raise TypeError("ChainState.completed must be a list")
        if not isinstance(paused, int) or isinstance(paused, bool) or paused < 0:
            raise ValueError("ChainState.paused_at_link must be a non-negative int")
        cid = d["chain_id"]
        pid = d["pending_id"]
        if not isinstance(cid, str) or not cid:
            raise ValueError("ChainState.chain_id must be a non-empty string")
        if not isinstance(pid, str) or not pid:
            raise ValueError("ChainState.pending_id must be a non-empty string")
        if not all(isinstance(c, dict) for c in completed):
            raise TypeError("ChainState.completed must contain only dicts")
        return cls(
            chain_id=cid,
            created_at=str(d["created_at"]),
            binding_id=str(d["binding_id"]),
            binding=binding,
            trigger_event=trigger_event,
            completed=tuple(completed),
            paused_at_link=paused,
            paused_payload=str(d["paused_payload"]),
            pending_id=pid,
        )


def _kill_predicates(binding: Binding) -> tuple[dict[str, Any], ...]:
    """Every KNOWN-danger kill the runtime evaluates per link: the binding's ``effective_guard`` (sealed
    floor + tightening additions) kill predicates, evaluated against the IMMUTABLE original trigger
    event (idempotent per link; a kill aborts the chain at the link it trips)."""
    return tuple(g.kill_predicate for g in binding.effective_guard if g.kill_predicate is not None)


class ChainExecutor:
    """Walk a multi-link binding's goal chain with the gate travelling with it (Slice 4b). Construct
    with the wired efferent gate, the injected per-link ``request_builder`` / ``risk_resolver`` /
    ``trust_resolver`` (the adapter's seams), and a ``clock``. The gate must carry a run journal: a chain
    is a journaled run, and its pause/resume state lives in the run's holds."""

    def __init__(
        self,
        *,
        gate: EfferentGate,
        request_builder: ChainRequestBuilder,
        risk_resolver: ChainRiskResolver,
        trust_resolver: ChainTrustResolver,
        clock: Callable[[], _dt.datetime],
        binding_store: BindingStore | None = None,
    ) -> None:
        self._gate = gate
        self._request_builder = request_builder
        self._risk_resolver = risk_resolver
        self._trust_resolver = trust_resolver
        self._clock = clock
        if gate.journal is None:
            raise ValueError("ChainExecutor: the gate must carry a run journal (a binding fire is journaled)")
        # The registry, for the §2.6 graduation evidence of a completed chain (``record_fire``) only. A
        # revoke or pause of the grant mid-chain is stopped by the run journal's fence at the next link.
        self._binding_store = binding_store

    @property
    def gate(self) -> EfferentGate:
        return self._gate

    # ---------------------------------------------------------------------------------------------
    # Entry: walk a fresh (re-acquired, one-shot-claimed) multi-link binding from link 0.
    # ---------------------------------------------------------------------------------------------
    def execute(self, binding: Binding, event: dict[str, Any]) -> ChainOutcome:
        """Walk ``binding.goal`` from link 0. ``binding`` is the PRE-CLAIM ACTIVE snapshot the dispatcher
        already re-acquired + (for a one-shot) claimed — the chain owns this fire. Fire on-loop links
        immediately, pause at the first confirm-class link, thread each link's output forward.

        NEVER raises — any internal fault aborts the chain cleanly (a chain mid-walk that crashes must
        not propagate into the dispatch loop). Pre-flight: an UNCLASSIFIABLE link (an undeclared tool →
        ``UnknownAction``) BARS the whole chain before any link fires (fail-closed). The seam-#2 fail-up
        is the per-fire TERMINAL-link ratified floor in :meth:`_fire_link` (the chain can't COMPLETE
        below ``binding.posture``)."""
        try:
            return self._walk(binding, event, start=0, ctx=ChainContext(trigger_event=event), prior=())
        except Exception as e:  # noqa: BLE001 — a chain fault never crashes the dispatch loop
            _log.error("chainpath execute: binding %s FAILED (%s): %s — chain aborted",
                       binding.binding_id, type(e).__name__, e)
            return ChainOutcome(binding_id=binding.binding_id, state="aborted", links=(),
                                reason=f"chain_error:{type(e).__name__}")

    def _unclassifiable_barred(self, binding: Binding) -> str | None:
        """Pre-flight (chain START only): every link's risk + trust must be RESOLVABLE — an undeclared
        tool → ``UnknownAction`` → the chain is BARRED before ANY link fires (fail-closed: never
        partially execute a chain with an unclassifiable link). Returns a bar-reason or ``None``.

        NOTE — the seam-#2 ratification fail-up is NO LONGER a pre-flight POSTURE comparison. The old
        ``max_per_link < binding.posture`` bar spuriously, permanently barred a binding ratified with a
        REQUESTED-posture upgrade (the compiler seals ``max(resolved, requested)``, so ``binding.posture``
        can exceed the per-link max with no drift — L2/codex MED). It is replaced by the TERMINAL-link
        ratified floor in :meth:`_fire_link` (``ratified_posture=binding.posture`` on the last link), so
        the chain can never COMPLETE below the ratified rung — which HONORS a requested upgrade (the chain
        pauses at the requested rung at completion), catches WHOLESALE drift at the completion point, and
        unifies with 4a (a single link == the terminal link). The middle-link drift residual remains
        (per-link sealed postures are a 4c schema evolution; per-link risk derives from SEALED tools so
        the only drift vector is a reviewed code edit to the manifest)."""
        for i in range(len(binding.goal)):
            try:
                self._risk_resolver(binding, i)
                self._trust_resolver(binding, i)
            except Exception as e:  # noqa: BLE001 — an unclassifiable link (undeclared tool) bars, fail-closed
                return f"unclassifiable_link_{i}:{type(e).__name__}"
        return None

    def _walk(self, binding: Binding, event: dict[str, Any], *, start: int, ctx: ChainContext,
              prior: tuple[ChainLinkResult, ...]) -> ChainOutcome:
        """Walk links [start, len(goal)) over ``ctx``. Shared by ``execute`` (start=0) and a resume
        continuation (start=paused_at+1, ctx carrying the already-fired links). ``prior`` are the
        link-results to PREPEND (the resume's fired paused link), so the returned ChainOutcome's
        ``links`` reflect the segment ruled on."""
        # fail-closed pre-flight (only at the chain START — a resume does not re-run it; the terminal
        # floor in _fire_link applies to the resume continuation too). An unclassifiable link bars the
        # whole chain before any link fires.
        if start == 0:
            bar = self._unclassifiable_barred(binding)
            if bar is not None:
                _log.warning("chainpath: binding %s BARRED pre-flight — %s (a link's risk/trust is "
                             "unclassifiable; fail-closed)", binding.binding_id, bar)
                return ChainOutcome(binding_id=binding.binding_id, state="aborted", links=prior,
                                    paused_at=0, reason=bar)

        results: list[ChainLinkResult] = list(prior)
        for i in range(start, len(binding.goal)):
            request, outcome = self._fire_link(binding, event, ctx, i)
            results.append(ChainLinkResult(link_index=i, outcome=outcome))

            if outcome.fired:
                ctx = ctx.with_link(CompletedLink.of(i, binding.goal[i], request.payload, outcome.execution))
                continue

            if outcome.held:
                _log.info("chainpath: binding %s HELD at link %d — %s", binding.binding_id, i, outcome.reason)
                return ChainOutcome(binding_id=binding.binding_id, state="held", links=tuple(results),
                                    paused_at=i, reason=outcome.reason)

            if outcome.pending:
                # PAUSE, journaled: the chain state to resume from went INTO the run's hold with the
                # pending (the request's continuation), so there is nothing to write here and no second
                # store to disagree. Report the chain the journal holds (the first proposal's, when this
                # is a re-delivery reaching an already-open hold).
                hold = self._gate.journal.get_hold(hold_id_for(request.run.run_id, request.run.effect_id))  # type: ignore[union-attr]
                chain = hold.get("chain") if hold is not None else None
                chain_id = chain.get("chain_id") if isinstance(chain, dict) else None
                _log.info("chainpath: binding %s PAUSED at link %d (%s) — pending %s, chain %s",
                          binding.binding_id, i, outcome.posture.name, outcome.pending_id, chain_id)
                return ChainOutcome(binding_id=binding.binding_id, state="paused", links=tuple(results),
                                    paused_at=i, pending_id=outcome.pending_id, chain_id=chain_id,
                                    reason=outcome.reason)

            # killed / refused / deferred / fired-but-effect-failed → ABORT (no further links).
            _log.info("chainpath: binding %s ABORTED at link %d — %s", binding.binding_id, i, outcome.reason)
            return ChainOutcome(binding_id=binding.binding_id, state="aborted", links=tuple(results),
                                paused_at=i, reason=outcome.reason or f"link_{i}_not_fired")

        # every link fired → the CHAIN COMPLETED = ONE fire of the grant. Record the §2.6 graduation
        # evidence HERE (Slice 4c) — the SINGLE locus for both ``execute`` (immediate) AND ``resume``
        # (out-of-band) completion, closing the carried 4a/4b out-of-band ``record_fire`` gap (the
        # dispatcher only recorded IMMEDIATE single-link fires; a chain completing via sweep/resume
        # accrued no evidence). Fail-soft (the chain already completed).
        self._record_chain_fire(binding, results)
        return ChainOutcome(binding_id=binding.binding_id, state="completed", links=tuple(results))

    def _record_chain_fire(self, binding: Binding, results: list[ChainLinkResult]) -> None:
        """Record the §2.6 graduation evidence for a COMPLETED chain — the chain is ONE fire of the grant
        (Slice 4c). The single locus for both ``execute`` (immediate) + ``resume`` (out-of-band)
        completion. Skips a ONE-SHOT (revoked at claim → it never graduates) + a bare executor (no
        ``binding_store`` wired — production always injects it via ``build_chain_executor``; a bare
        test executor records nothing). ``clean`` is DERIVED, not hardcoded (Slice 4c gap #2): a completed
        chain means every link fired terminally with no kill/deny/refuse. The POST-HOC 'that fire was
        wrong' DEMOTION of ``clean_count`` is 4d (the inverse direction — no post-hoc correction channel
        exists yet). Fail-soft: the chain already completed; a bookkeeping fault never propagates."""
        if self._binding_store is None or binding.one_shot:
            return
        if results and results[-1].outcome.replayed:
            # the terminal link replayed: this chain completed before, and its fire was recorded then
            # (or the process stopped first: an under-count, the safe direction for evidence).
            return
        clean = all(r.outcome.is_clean_fire for r in results)
        try:
            self._binding_store.record_fire(binding.binding_id, clean=clean,
                                            fired_at=self._clock().isoformat())
        except Exception as e:  # noqa: BLE001 — the chain completed; a bookkeeping fault is not fatal
            _log.error("chainpath: record_fire(%s) FAILED (%s): %s — chain completed, bookkeeping not "
                       "recorded", binding.binding_id, type(e).__name__, e)

    def _fire_link(self, binding: Binding, event: dict[str, Any], ctx: ChainContext,
                   i: int) -> tuple[ActionRequest, GateOutcome]:
        """Build link i's request (adapter) then OVERLAY the governance-critical fields from the SEALED
        binding (so the adapter cannot forge them): per-link ``risk`` (seam #1) + per-link ``trust``
        (seam #5, hops=i) + ``authority`` (binding_invocation, hops=i) + the ``effective_guard`` kills +
        the prediction trajectory + the IMMUTABLE original trigger event.

        **The seam-#2 fail-UP = the TERMINAL-link ratified floor.** The LAST link carries
        ``ratified_posture = binding.posture`` (the sealed whole-chain rung); the gate fires it at
        ``max(policy(risk_i, trust_i), binding.posture)``. Since ``binding.posture >= every per-link
        posture`` (the §2.3 set-risk max — proven exact: ``aggregate_risk`` dominates each link's risk
        dims + ``earned_posture`` peaks at the deepest link's hops), the chain can NEVER COMPLETE below
        the ratified rung — which (a) HONORS a requested-posture upgrade (the terminal pauses at the
        requested rung), (b) catches WHOLESALE drift at the completion point, and (c) unifies with 4a (a
        single link IS the terminal link, already floored at ``binding.posture``). On a resume
        continuation the terminal link is the last index, so the floor applies there too. INTERMEDIATE
        links carry ``ratified_posture=None`` — they run at their natural per-link posture (§A: early
        on-loop links fire). The middle-link drift residual remains (per-link sealed postures = 4c)."""
        base = self._request_builder(binding, ctx, i)
        is_terminal = i == len(binding.goal) - 1
        run = RunRef(run_id_for(binding.binding_id, event), f"link-{i}", chained=True)
        # the state this chain resumes from if THIS link pauses: written into the link's hold
        continuation = ChainState.create(
            created_at=self._clock().isoformat(), binding=binding, trigger_event=event,
            completed=ctx.completed, paused_at_link=i, paused_payload=base.payload,
            pending_id=hold_id_for(run.run_id, run.effect_id),
        ).to_dict()
        request = dataclasses.replace(
            base,
            risk=self._risk_resolver(binding, i),
            trust=self._trust_resolver(binding, i),
            authority=binding_invocation(binding, hops=i),
            kill_predicates=_kill_predicates(binding),
            predicted_trajectory=guard_trajectory(binding.effective_guard),
            trigger_event=event,
            ratified_posture=(binding.posture if is_terminal else None),
            run=run,
            continuation=continuation,
        )
        return request, self._gate.gate(request)

    # ---------------------------------------------------------------------------------------------
    # Resume: the operator resolved a paused link → fire it + continue the chain.
    # ---------------------------------------------------------------------------------------------
    def resume(self, pending_id: str, decision: ConfirmDecision) -> ChainOutcome | None:
        """Resume a paused chain from the operator's resolve of its paused link's ``pending_id``. Returns
        ``None`` iff no open chain owns that pending (the caller falls back to a 4a single-link
        ``gate.resolve``). Otherwise: ATOMICALLY claim the chain state (at-most-once advance) → seal-check
        → ``gate.resolve`` fires the paused link (at-most-once for the LINK) → on fired, append its output
        + CONTINUE from the next link (which may pause again → a NEW chain state, or COMPLETE). A denied/
        dropped resolve ENDS the chain. NEVER raises (the contract)."""
        try:
            return self._resume(pending_id, decision)
        except Exception as e:  # noqa: BLE001 — resume, like the gate, never raises into the caller
            _log.error("chainpath resume UNEXPECTED error (%s): %s — failing closed", type(e).__name__, e)
            return ChainOutcome(binding_id="?", state="aborted", links=(),
                                reason=f"resume_error:{type(e).__name__}")

    def _resume(self, pending_id: str, decision: ConfirmDecision) -> ChainOutcome | None:
        hold = self._gate.journal.find_pending(pending_id)  # type: ignore[union-attr]
        if hold is None or not (hold.get("chained") or hold.get("chain") is not None):
            return None   # not a chain link's pending: the caller's plain resolve
        return self._resume_journaled(pending_id, hold, decision)

    def _resume_journaled(self, pending_id: str, hold: dict[str, Any],
                          decision: ConfirmDecision) -> ChainOutcome:
        """Resume a journaled chain from the hold that paused it. The chain state is READ from the hold
        (written with the pending, in one record); nothing is claimed or removed. The decision is the
        gate's one write-once ``decide``; a second resumer finds it, and the walk it continues is
        journaled effect by effect, so it can never run a link twice. A paused or revoked grant is
        stopped by its governance generation at the next effect (no separate registry re-check)."""
        binding_id = str(hold.get("binding_id") or "?")
        try:
            state = ChainState.from_dict(hold["chain"])
            binding = state.binding_obj()
            completed = state.completed_links()
            bad = None if state.seal_matches() else "integrity:seal_mismatch"
            # the continuation must be THIS hold's: its own pending id and binding, a sealed binding
            if bad is None and (state.pending_id != hold.get("hold_id") or state.binding_id != hold.get("binding_id")
                                or not binding.seal_matches()):
                bad = "integrity:continuation_not_this_hold"
            bad = bad or state.validate_against_binding(binding)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            bad, state = f"chain_state_malformed:{type(e).__name__}", None
        if bad is not None:
            # the continuation is unreadable or altered: the link must not fire without its chain
            _log.error("chainpath resume: chain continuation of %s unusable (%s) — rejecting", pending_id, bad)
            rejected = self._gate.resolve(pending_id, ConfirmDecision(approved=False, by="on-loop",
                                                                      reason=f"chain_aborted:{bad}"),
                                          chain_owned=True)
            paused_at = state.paused_at_link if state is not None else None
            if rejected.reason == "journal:already_decided":
                # another resolver decided first: this chain's end is that decision's, not this one's
                return ChainOutcome(binding_id=binding_id, state="held", links=(), paused_at=paused_at,
                                    reason=rejected.reason)
            return ChainOutcome(binding_id=binding_id, state="aborted", links=(), paused_at=paused_at,
                                reason=bad)
        assert state is not None
        link_outcome = self._gate.resolve(pending_id, decision, chain_owned=True)
        results = [ChainLinkResult(link_index=state.paused_at_link, outcome=link_outcome)]
        if not link_outcome.fired:
            if link_outcome.refused and link_outcome.reason in _STILL_OPEN:
                # nothing was decided: the chain is still paused at this link, awaiting a decision
                return ChainOutcome(binding_id=binding.binding_id, state="paused", links=tuple(results),
                                    paused_at=state.paused_at_link, pending_id=pending_id,
                                    chain_id=state.chain_id, reason=link_outcome.reason)
            if link_outcome.held or link_outcome.reason == "journal:already_decided":
                # approved but not yet run, or another resolver's decision governs: not this chain's end
                return ChainOutcome(binding_id=binding.binding_id, state="held", links=tuple(results),
                                    paused_at=state.paused_at_link, reason=link_outcome.reason)
            return ChainOutcome(binding_id=binding.binding_id, state="aborted", links=tuple(results),
                                paused_at=state.paused_at_link,
                                reason=link_outcome.reason or "paused_link_not_fired")
        ctx = ChainContext(trigger_event=state.trigger_event, completed=completed)
        ctx = ctx.with_link(CompletedLink.of(
            state.paused_at_link, binding.goal[state.paused_at_link], state.paused_payload,
            link_outcome.execution))
        return self._walk(binding, state.trigger_event, start=state.paused_at_link + 1, ctx=ctx,
                          prior=tuple(results))

    def sweep_timeouts(self, now: _dt.datetime | None = None) -> list[ChainOutcome]:
        """The silence default for journaled chain links: every open chained hold whose pending has
        expired is resumed with the gate's silence decision (an allowlisted cooling-off link approves
        and the chain continues; anything else rejects and the chain ends). Fail-soft per hold."""
        journal = self._gate.journal
        try:
            now = now or self._clock()
            holds = journal.open_holds()  # type: ignore[union-attr]
        except Exception as e:  # noqa: BLE001
            _log.error("chainpath sweep: FAILED (%s): %s", type(e).__name__, e)
            return []
        out: list[ChainOutcome] = []
        for h in holds:
            if not (h.get("chained") or h.get("chain") is not None) or not isinstance(h.get("pending"), dict):
                continue
            try:
                decision = self._gate.silence_decision(h, now)
                if decision is None:
                    continue
                result = self.resume(str(h["pending"].get("pending_id")), decision)
            except Exception as e:  # noqa: BLE001 — one bad hold never stops the sweep
                _log.error("chainpath sweep: hold %s FAILED (%s): %s", h.get("hold_id"), type(e).__name__, e)
                continue
            if result is not None and result.reason != "journal:already_decided":
                out.append(result)   # (a resolver that lost the race to a person changed nothing)
        return out
