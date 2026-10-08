"""levain.autonomic.executor — the action-execution seam + the request/result shapes.

The ``Executor`` is the injected seam the concrete action lives behind (mirrors the afferent
``Completer``): the clean vagus gate decides the posture; a flow-side / adopter-side Executor
performs the actual effect (``deliver_as_document``, ``email_send``, …). The seam is NARROW by
design — ``execute`` receives only the action name + the payload to act on + the receipt key; it
does NOT see trust / risk / confidence (the gate already decided — the executor ACTS, it never
re-decides). Keeping it narrow keeps the concrete actions (the fossil) cleanly swappable and unable
to smuggle policy back in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from levain.autonomic.authority import AuthorityScope
from levain.autonomic.gates import DEFAULT_CONFIDENCE_FLOOR
from levain.autonomic.journal import RunRef
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk
from levain.autonomic.trust import TrustContext

__all__ = ["ActionRequest", "ExecutionResult", "Executor"]


@dataclass(frozen=True)
class ActionRequest:
    """One efferent action proposed to the gate, with the trust/grounding context the gate resolves
    against and the provenance the receipt records. Frozen — a request is a fact handed to the gate.

    Load-bearing: ``action_name`` (→ the declared risk), ``payload`` (the action content — both what
    the executor acts on AND the §1.5 injection-scan target), ``trust`` (→ posture), ``grounded``
    (the grounding outcome the caller computes per regime — see ``gates.screen``). The rest are
    receipt provenance: ``context_id`` keys the influence event; ``query_text``/``query_date`` are
    the replayable bucket axis; ``producers`` are the source proposal's lineage(s); ``proposal_id``
    links back to the afferent proposal this acts on (``None`` for a non-proposal trigger).
    """

    action_name: str
    payload: str
    context_id: str
    query_text: str
    query_date: str
    trust: TrustContext
    grounded: bool
    authority: AuthorityScope
    producers: tuple[str, ...] = ()
    proposal_id: str | None = None
    overall_confidence: object | None = None
    directive_confidence: object | None = None
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR
    # the human's pre-truth read, when a human-present confirm routed through (the forced-first-
    # estimate primitive); JSON-serializable into the receipt's actor_first_estimate. None until a
    # human supplies it (Slice 2 confirm).
    actor_first_estimate: Any | None = None
    # the firing binding's OWN forward-simulation (the Guard's ``predicted_trajectory``), carried so the
    # gate's prediction-error monitor (Slice 3a.5) can diff actual-vs-predicted on an AUTONOMOUS fire and
    # KILL on divergence (the unknown-danger backstop). None for a manual / non-binding fire ⇒ the
    # monitor is inert (Slices 1-3 unchanged). Slice 4 (the binding fire-path) populates it.
    predicted_trajectory: dict[str, Any] | None = None
    # ----- Slice 4 (the binding fire-path) — governance the FIRE-PATH supplies (None/empty ⇒ a manual
    # fire, every field inert; Slices 1-3 unchanged). The fire-path DERIVES each from the SEALED binding
    # (the trusted core path), so a binding's grant content drives the gate, never a forgeable label.
    #
    # ``risk`` — the binding's risk DERIVED from its sealed goal-chain tools (seam #1: ONE source of
    # truth = the sealed tools, via the SAME ``_aggregate_risk`` the binding was ratified at). When set,
    # the gate resolves ``policy(risk, trust)`` against THIS instead of a manifest ``action_name`` lookup
    # — so the binding fires at the risk it was ratified on, not whatever a per-action manifest happens
    # to say. The fail-CLOSED property moves to the fire-path's derivation (an undeclared tool can't
    # produce a risk → no request → no fire). ``action_name`` survives only as the executor/receipt label.
    risk: ActionRisk | None = None
    # ``ratified_posture`` — the binding's SEALED posture (seam #2: fail-UP floor). The gate fires at
    # ``max(policy(risk, trust), ratified_posture)`` — re-resolve catches a risk declaration that ROSE
    # since ratification, the floor refuses to fire BELOW the rung the human ratified if one was lowered.
    ratified_posture: Posture | None = None
    # ``kill_predicates`` + ``trigger_event`` — the KNOWN-danger kill (Slice 3a.5's kill substrate gets
    # its first runtime consumer here, closing codex's 3a.5 L3 finding #1). The fire-path puts the
    # firing binding's ``effective_guard`` kill predicates here + the live trigger event the predicate
    # matched; the gate evaluates each on the DIVERSE fail-safe substrate (``kill_trips`` — UNKNOWN
    # trips) and KILLS (no fire/propose) on any trip, regardless of posture. Empty/None ⇒ no kill check
    # (a manual fire carries no guard).
    kill_predicates: tuple[dict[str, Any], ...] = ()
    trigger_event: dict[str, Any] | None = None
    # ``run`` — the journaled run + effect this action is (S8). The fire path sets it for every binding
    # fire; the gate then runs the effect through the run journal (at most once, held while a decision
    # on the binding is open, stopped by a fence or a cancel). None ⇒ a manual action, not journaled.
    run: RunRef | None = None
    # ``continuation`` — for a link of a journaled chain: the chain state to resume from if this link
    # pauses for a decision. The gate writes it INTO the run's hold, the same row as the
    # pending, so the decision and the way to continue after it live in one record.
    continuation: dict[str, Any] | None = None
    # ``risk_revision`` — the run journal's risk-catalog revision, read BEFORE ``risk`` was derived (the
    # fire path stamps it). The journal admits the effect only if the catalog has not been revised since,
    # so a reclassification committed through ``RunJournal.revise_risk`` between this decision and the
    # effect's admission never runs it at the old rung. A journaled request without one runs nothing.
    risk_revision: int | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of an Executor performing an action. ``ok`` False (with ``error``) = the effect
    failed AFTER the gate approved it — a real-world failure, not a gate refusal (the receipt still
    records the gate's APPROVAL; ``fired`` on the GateOutcome reflects ``ok``). ``detail`` is a
    human-readable result (e.g. the written file path); ``downstream_id`` is the id of the thing
    created/sent — the receipt's ``downstream_claim_or_action_id`` (the action the influence
    changed)."""

    ok: bool
    detail: str = ""
    error: str | None = None
    downstream_id: str | None = None


@runtime_checkable
class Executor(Protocol):
    """The action-execution seam. ``name`` is the executor's identity (for logging/audit);
    ``execute`` performs the effect and returns an :class:`ExecutionResult`. A concrete Executor is
    a TRUSTED adapter (flow-side / adopter-side) — the gate only ever routes an APPROVED action to
    it. It must NEVER raise into the gate: any failure is an ``ExecutionResult(ok=False, error=…)``
    ("a failed effect beats a crashed gate" — the fail-soft analog).

    A binding's effect (a ratified grant acting with no session open) runs only on an executor that
    declares ``confined = True``: one that runs the effect through levain's confinement floor, as the
    entity's separate hands user, never unconfined in the operator's process. The gate refuses to run a
    binding's effect on any other executor. The attribute is the executor's own declaration; a manual
    (human-present) action does not need it."""

    name: str

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult: ...
