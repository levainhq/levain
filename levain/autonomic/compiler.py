"""levain.autonomic.compiler — the bind-time NL→`P` compiler core (Slice 3c).

The keystone of the binding language (``awareness_bus_binding_language.md``): interpretation moves to
BIND-TIME. An NL binding ("when I get home from a trip, summarize my unread AI newsletters into a
Doc") has a fatal version (stored as text, an agent re-interprets it at every fire — ungrounded
autonomy) and a safe version (compiled ONCE, under the human's eye, into a tight declarative form the
human ratifies; fire-time is deterministic). This module is the safe version's engine:

    model PROPOSES (NL → a candidate predicate ``P`` + goal/tools/output) — upstream, the adapter's
        provider seam; NOT here (the safety-critical part is everything AFTER the candidate).
    structure DISPOSES (validate ``P`` is deterministic-or-refuse · score tightness · resolve posture
        via ``policy(risk, trust)`` · classification-integrity · the confirm-class guard invariants) —
        THIS module: pure, deterministic, no model, no corpus, no clock.
    the human APPROVES (ratifies the compiled, sealed grant) — the adapter persists what compiles.

SCHEMA-FIRST INVERSION: the compiler is LAST. It rides shipped infra — the 3a ``Binding`` schema
(what it compiles TO), the ``events.py`` deterministic-``P`` evaluator (the injected
:class:`PredicateValidator` + :class:`Backtester` — *behavioral* grounding, not syntactic: "would
have fired 4× last month; here's each"), and the Slice-1/2 ``policy()`` resolver. It NEVER imports
the corpus (``events.py``) — the anti-cycle rule; both corpus seams are INJECTED and adapter-wired.

The ``RECEIPT_VERSION=4`` compiler invariants (``decision_influence_receipt_contract.md`` §2.1), each
enforced here:
  - **P must reduce to a deterministic predicate** or the compile REFUSES (offers DECOMPOSITION — the
    "build 'important' as a curated union of proxies" move, never a fire-time LLM judgment).
  - **classification-integrity:** posture is COMPUTED (``policy``), never declared — a requested
    self-downgrade below the floor is DENIED + surfaced (the binding seals at the resolved posture),
    so a confirm-class action can never seal as on-loop.
  - **binding-driven confirm-class ⇒ a mandatory, well-formed guard:** ≥1 guard carrying a kill;
    rationale + ``kill_predicate`` + ``kill_drill`` non-null (the drill's PRESENCE — "no drill → no
    compile"); the kill is a DETERMINISTIC predicate (validated, like the trigger); the dissent is
    **adversarially authored** (a substrate DIFFERENT from the compiler — the
    ``cross_substrate_review_codex_nonreplaceable`` pattern); the human PRODUCES the kill.

Slice 3a.5 hardening LANDED here (3c proved PRESENCE + DETERMINISM + CLASSIFICATION; 3a.5 proves the
kill WORKS): kill-PURITY (``kill.assert_kill_pure`` — read-only + evaluable by the DIVERSE kill
substrate) and the kill-DRILL compile gate (``kill.kill_outcome`` — the drill must ACTUALLY trip the
kill, verified on the diverse substrate, not events.py). The rest of 3a.5 lives in its natural home:
monotonic-editability (tighten-free / loosen-re-ratifies) in ``binding`` (the unsealed
``guard_additions`` tier + ``BindingStore.tighten_guard``); the runtime prediction-error monitor in
``monitor`` + the gate; liveness telemetry in ``liveness``. Together they add the tighten-free
ergonomics + the known/unknown-danger kills that make GRADUATION to on-loop safe.

Stdlib + ``levain.autonomic`` only. Pure: same (candidate, validator output, backtest report) → same
result. No model in the compile decision (``structural_invariants_beat_discipline``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from levain.autonomic.binding import (
    Binding,
    BindingStatus,
    Guard,
    PredicateValidator,
    SubGoal,
    TightnessVector,
    TriggerSpec,
)
from levain.autonomic.kill import Kleene, KillImpurityError, assert_kill_pure, kill_outcome
from levain.autonomic.monitor import assert_trajectory_pure
from levain.autonomic.policy import policy
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk
from levain.autonomic.trust import TrustContext

__all__ = [
    "Backtester",
    "CandidateBinding",
    "CompileSurfacing",
    "CompileResult",
    "REFUSE_CONSTITUTIONAL",
    "REFUSE_PRUDENTIAL",
    "compile_binding",
    "refuse_undecidable",
]

# refuse_class vocabulary, shared with RECEIPT_VERSION=4 (`decision_influence_receipt_contract.md`
# §2.1): a constitutional refuse is NEVER-PERMITTED (a structural floor — the bind-time §1.5 analog);
# a prudential refuse is permitted-but-judged-unwise. (`kill_triggered` is a FIRE-time terminal_state,
# never a compile outcome.) A chronic prudential-refuse rate is the mis-calibration signal §2.1 names.
REFUSE_CONSTITUTIONAL = "constitutional"
REFUSE_PRUDENTIAL = "prudential"


@runtime_checkable
class Backtester(Protocol):
    """The injected BEHAVIORAL-grounding seam (the corpus-aware half; the adapter wires
    ``events.replay``). Replays the candidate predicate over the archived corpus for ``trigger_type``
    across the last ``window_days`` and returns the ``events.replay`` report dict — at minimum:
    ``fire_count``, ``fire_count_exact`` (bool), ``fire_count_by_coverage`` (attested/unknown/
    unattested), ``scanned`` (int), ``field_engagement`` ({field: {present, scanned,
    present_fraction, ops}}), ``warnings`` (list[str]), ``coverage`` ({attested_fraction, honest_note,
    ...}). This is the ``RECEIPT_VERSION=4`` "backtest-grounded" half — ratification grounds on
    BEHAVIOR ("would have fired 4× last month"), not on reading the predicate. The compiler does NOT
    import the corpus; the report is data."""

    def replay(self, pattern: dict[str, Any], trigger_type: str, *, window_days: int) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class CandidateBinding:
    """The "model proposes" output, ready for "structure disposes" — everything the compiler needs to
    turn an NL ask into a sealed grant, EXCEPT what the compiler itself computes (``pattern_precision``
    + the posture). The adapter's provider seam produces the candidate ``pattern`` (P) and the
    author-proposed tightness for the three dimensions the corpus cannot measure; the human's authored
    guard is already attached (the guard is a human PRODUCT, not a compile output).

    - ``pattern`` = the candidate deterministic ``P`` (an ``events.py`` JSON-AST). Validated here.
    - ``risk`` = the DECLARED risk (a manifest lookup, adapter-supplied — never inferred; for a chain,
      the MAX-risk member governs the binding's posture, Slice 4 does per-link).
    - ``trust`` = the FIRE-TIME trust shape (a binding fires intent-free + human-ABSENT; the adapter
      sets ``signal_auth`` per trigger type, ``intent_provenance``, and the chain's deepest ``hops``).
    - ``requested_posture`` = the author's ASK (e.g. "I want this on-loop"), or ``None`` for no ask.
      The compiler DENIES a downgrade below the resolved floor (classification-integrity).
    - ``goal_spec`` / ``tool_min`` / ``output_bound`` = the three author-proposed-and-ratified
      tightness dimensions the corpus can't measure (``pattern_precision`` is COMPUTED from the
      backtest). Each in [0,1]; the binding seal records them as the ratification anchor.
    - ``guard`` = the human-authored bet-slip(s). Required (with a kill) at confirm-class — enforced
      against the RESOLVED posture, not declared."""

    created_by: str
    created_at: str
    trigger_type: str
    pattern: dict[str, Any]
    goal: tuple[SubGoal, ...]
    risk: ActionRisk
    trust: TrustContext
    requested_posture: Posture | None = None
    goal_spec: float = 1.0
    tool_min: float = 1.0
    output_bound: float = 1.0
    one_shot: bool = False
    guard: tuple[Guard, ...] = ()
    window_days: int = 30


@dataclass(frozen=True)
class CompileSurfacing:
    """The "priced looseness" the compiler shows at ratification — the governance consequence, made
    visible so the human tightens to comfort with full information (the binding-language UX keystone:
    the compiler is a tightness-NEGOTIATION partner, not a linter). Composes with the 3b would-fire
    cockpit (``binding_view.py``) — the same backtest shape it renders."""

    resolved_posture: Posture
    requested_posture: Posture | None
    downgrade_denied: bool
    tightness: TightnessVector
    would_fire_count: int
    would_fire_exact: bool
    fire_count_by_coverage: dict[str, int]
    scanned: int
    field_engagement: dict[str, Any]
    backtest_warnings: tuple[str, ...]
    confirm_cost_note: str
    coverage_note: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompileResult:
    """The outcome: a sealed ratified-ready :class:`Binding` (``ok=True``), or a REFUSAL with a
    ``refuse_class`` + ``reasons`` (+ a ``decomposition`` offer when ``P`` can't reduce). The adapter
    persists ``binding`` on success (the human's ratification) and renders ``surfacing``; on refusal it
    surfaces the reasons + the decomposition path. A refusal NEVER carries a binding (fail-closed)."""

    ok: bool
    binding: Binding | None = None
    surfacing: CompileSurfacing | None = None
    refuse_class: str | None = None
    reasons: tuple[str, ...] = ()
    decomposition: tuple[str, ...] = ()

    @classmethod
    def refused(cls, refuse_class: str, reasons: tuple[str, ...] | list[str],
                *, decomposition: tuple[str, ...] | list[str] = ()) -> "CompileResult":
        return cls(ok=False, refuse_class=refuse_class, reasons=tuple(reasons),
                   decomposition=tuple(decomposition))

    @classmethod
    def compiled(cls, binding: Binding, surfacing: CompileSurfacing) -> "CompileResult":
        return cls(ok=True, binding=binding, surfacing=surfacing)


# --- pure AST helpers (the compiler is corpus-agnostic — it cannot import events.py) ---------

# leaf ops that return False on an ABSENT field (positive-assertion floor — `events._eval_leaf`); a
# KILL using one at positive depth silently FAILS TO TRIP on a missing-field event (the H2 footgun).
_ABSENT_BLIND_OPS = frozenset({"!=", "in", ">", ">=", "<", "<="})


def _referenced_fields_polarity(node: Any, *, negated: bool = False) -> set[tuple[str, bool]]:
    """Walk a (pre-validated) predicate AST → ``{(field, is_negated)}`` where ``is_negated`` = under an
    ODD number of ``not``s. Pure; mirrors ``events._referenced_fields``' polarity tracking WITHOUT
    importing the corpus. Used for the polarity-aware dead-field check: a 0%-present field is DEAD only
    when asserted POSITIVELY (it never matches → blind); under ``not`` a 0%-present field fires on
    ABSENCE (intentional — e.g. ``not(dmarc == pass)`` over no-dmarc mail), so it is NOT dead."""
    out: set[tuple[str, bool]] = set()
    if not isinstance(node, dict):
        return out
    op = node.get("op")
    if op in ("and", "or"):
        for c in node.get("clauses", []) or []:
            out |= _referenced_fields_polarity(c, negated=negated)
    elif op == "not":
        clause = node.get("clause")
        if clause is not None:
            out |= _referenced_fields_polarity(clause, negated=not negated)
    else:
        f = node.get("field")
        if isinstance(f, str) and f:
            out.add((f, negated))
    return out


def _absent_blind_kill_leaves(node: Any, *, negated: bool = False) -> list[tuple[str, str]]:
    """Walk a (pre-validated) KILL predicate AST → ``[(field, op)]`` for POSITIVE-depth leaves using an
    absent-blind op — one that returns False on a MISSING field, so the kill silently FAILS TO TRIP on
    absent-field events. The classic footgun (``awareness_bus_binding_language.md``): a kill
    ``dmarc != pass`` does NOT trip on no-dmarc mail → the action fires on exactly the worst
    (unauthenticated) case; the fail-safe spelling ``not(dmarc == pass)`` trips on absent too. Only
    POSITIVE-depth leaves are flagged (under a ``not`` the absent-behavior inverts — it tends to trip on
    absence, the safe direction). The compiler SURFACES these (the doc-named "surface the
    absent-handling choice") rather than refusing — the absent intent is the author's call, made at the
    human-gated ratification; the 3a.5 kill-purity pass hardens it structurally."""
    out: list[tuple[str, str]] = []
    if not isinstance(node, dict):
        return out
    op = node.get("op")
    if op in ("and", "or"):
        for c in node.get("clauses", []) or []:
            out += _absent_blind_kill_leaves(c, negated=negated)
    elif op == "not":
        clause = node.get("clause")
        if clause is not None:
            out += _absent_blind_kill_leaves(clause, negated=not negated)
    elif op in _ABSENT_BLIND_OPS and not negated:
        f = node.get("field")
        if isinstance(f, str) and f:
            out.append((f, op))
    return out


# --- pattern_precision (the COMPUTED tightness dimension) -------------------------------------


def _score_pattern_precision(report: dict[str, Any], pattern: dict[str, Any]) -> tuple[float, list[str]]:
    """Score ``pattern_precision`` ∈ [0,1] from the backtest report — the ONE tightness dimension the
    corpus can measure, and the safety-critical one (the deterministic-predicate floor + the dead-
    predicate honesty floor). A validated deterministic predicate is precise BY CONSTRUCTION (it
    reduced to a deterministic AST), so the score STARTS at 1.0 and is docked by what the corpus
    reveals:

      - **COMPLETENESS (the M3 floor):** every field the predicate references MUST be enumerated in the
        backtest ``field_engagement``; a referenced field the report did NOT cover (absent, or
        ``present_fraction`` null on a non-empty scan) is UNMEASURED — capped at 0.5, never silently
        "precise". The compiler does not trust the backtester to have measured what it didn't report.
      - **DEAD field (polarity-aware, the load-bearing catch):** a referenced field that resolves
        PRESENT in 0% of a non-empty scan AND is asserted POSITIVELY (not under a ``not``) ⇒ precision
        0.0 — it never matches, so its "0 fires" is BLINDNESS, not safety. A 0%-present field used ONLY
        under ``not`` fires on ABSENCE (intentional — e.g. ``not(dmarc == pass)`` over no-dmarc mail) and
        is NOT dead (the polarity fix — matches ``events._engagement_warnings``).
      - Each remaining backtest WARNING docks 0.25 — a flagged ambiguity is the opposite of precision.
      - A COLD START (scanned == 0) is UNMEASURED, not precise: 0.5 + a loud note; a cold-start binding
        still lands confirm-heavy via the risk floor and graduates as real fires accrue (§2.1).

    Returns (score, notes). precision does NOT feed posture in 3c-i — posture is ``policy(risk, trust)``.
    It is the ratification anchor + the graduation input + a surfaced honesty signal."""
    notes: list[str] = []
    scanned = int(report.get("scanned", 0) or 0)
    field_engagement = report.get("field_engagement", {}) or {}
    warnings = report.get("warnings", []) or []

    if not scanned:
        notes.append("pattern_precision UNMEASURED — cold start (no corpus in the backtest window); "
                     "scored 0.5 conservatively. It graduates as real fires accrue.")
        return 0.5, notes

    referenced = _referenced_fields_polarity(pattern)
    fields = {f for f, _neg in referenced}

    # M3 — completeness: a referenced field the backtest didn't enumerate (or reported with a null
    # present_fraction) is UNMEASURED, not precise. Without this a backtester that under-reports a
    # field would launder a blind predicate's tightness into the SEALED ratification anchor.
    unenumerated = sorted(
        f for f in fields
        if f not in field_engagement or (field_engagement.get(f) or {}).get("present_fraction") is None
    )
    if unenumerated:
        notes.append(f"referenced field(s) {unenumerated} were NOT enumerated by the backtest "
                     "(present_fraction missing) — pattern_precision UNMEASURED for them, capped at "
                     "0.5. The backtester must report field_engagement for every referenced field.")
        return 0.5, notes

    # DEAD (polarity-aware): 0%-present AND used in at least one POSITIVE assertion → blind.
    dead = sorted(
        f for f in fields
        if (field_engagement.get(f) or {}).get("present_fraction") == 0.0
        and any((not neg) for (ff, neg) in referenced if ff == f)
    )
    if dead:
        notes.append(f"DEAD predicate field(s) {dead} — resolve PRESENT in 0% of the scanned corpus in "
                     "a POSITIVE assertion (typo / non-retained field). pattern_precision = 0.0: a "
                     "'0 fires' here is BLINDNESS, not safety. Do not ratify as tight.")
        return 0.0, notes

    score = 1.0
    if warnings:
        score = max(0.0, round(1.0 - 0.25 * len(warnings), 4))
        notes.append(f"{len(warnings)} backtest warning(s) docked pattern_precision to {score:.2f}.")
    return score, notes


def _confirm_class(posture: Posture) -> bool:
    """True iff the resolved posture requires a human approval BEFORE the fire (cooling_off / confirm /
    confirm_elevated). At confirm-class a binding-driven (human-ABSENT, autonomous) fire needs the
    mandatory guard; the fast-lane on-loop / above-loop rungs do not (calibration-only guards are
    null-ok there)."""
    return posture.needs_confirm


def _norm_substrate(s: str | None) -> str:
    """Normalize a substrate / author id for the identity comparisons that gate the adversarial-dissent
    and human-produces-the-kill invariants — strip + casefold, so ``"Claude"`` / ``" claude "`` cannot
    slip the cross-substrate check as if they were a DIFFERENT lineage (complement-MED1/LOW3). The
    exact-string equality these comparisons rest on is the weakest link in the deepest human-safety
    primitive here; normalizing closes the case + whitespace evasion classes (true human-ness of an
    arbitrary string is still unverifiable — that is the unpackaged-human boundary)."""
    return (s or "").strip().casefold()


# --- the compiler -----------------------------------------------------------------------------


def compile_binding(
    candidate: CandidateBinding,
    *,
    validator: PredicateValidator,
    backtester: Backtester,
    compiler_substrate: str,
) -> CompileResult:
    """Compile a candidate into a sealed, ratification-ready :class:`Binding`, or REFUSE.

    ``validator`` enforces the deterministic-predicate floor on the trigger ``P`` AND on every guard's
    ``kill_predicate`` (the adapter wires ``events.validate_predicate``); it MUST raise on a
    non-deterministic / malformed predicate. ``backtester`` grounds tightness + the surfacing on
    BEHAVIOR. ``compiler_substrate`` is this compiler's own lineage id — a guard's dissent authored on
    the SAME substrate is NOT adversarial (the cross-substrate invariant) and is refused at
    confirm-class.

    Order (fail-closed, cheapest-floor-first): (1) ``P`` deterministic-or-refuse(+decompose); (2)
    resolve posture via ``policy``; (3) classification-integrity (deny + surface a downgrade); (4) the
    confirm-class guard invariants; (5) backtest → ``pattern_precision`` + surfacing; (6) mint the
    sealed binding."""
    reasons: list[str] = []

    # (1) the deterministic-predicate FLOOR — P must reduce to a deterministic predicate, or refuse +
    # offer decomposition (refusal → DECOMPOSITION, never a fire-time LLM judgment). This is the
    # binding-language keystone's deepest sub-rule, enforced at the AST boundary.
    try:
        validator.validate(candidate.pattern)
    except Exception as e:  # the validator raises ANY exception on a bad predicate (Protocol contract)
        return CompileResult.refused(
            REFUSE_CONSTITUTIONAL,
            [f"trigger predicate is not a deterministic predicate: {e}",
             "a binding cannot ground a fire-time judgment — the trigger must reduce to a "
             "deterministic predicate over the event corpus."],
            decomposition=[
                "Decompose the intent into a UNION of deterministic proxies, each ratified "
                "separately (e.g. `sender in [VIP]` OR `subject contains 'urgent'` OR "
                "`first email from a new domain in 90d`), and surface the approximation gap — "
                "importance becomes a first-class, inspectable, evolving predicate-object, never a "
                "fire-time LLM judgment.",
            ],
        )

    # (2) posture is COMPUTED, never declared (structural_invariants_beat_discipline).
    resolved = policy(candidate.risk, candidate.trust)

    # (3) classification-integrity: a requested DOWNGRADE below the floor is DENIED, not honored. The
    # binding seals at the resolved (or higher-if-requested) posture; the denial is SURFACED loudly so
    # the self-trust attack (mark a confirm-class action on-loop to dodge the guard) is visible, not
    # silent. The human may ask for MORE involvement (requested > resolved) — that is allowed.
    requested = candidate.requested_posture
    downgrade_denied = requested is not None and requested < resolved
    if requested is not None and requested < resolved:
        reasons.append(
            f"classification-integrity: requested posture {requested.name} is more autonomous than "
            f"the resolved floor {resolved.name} — DENIED. The binding seals at {resolved.name} "
            "(risk + grounding distance demand it). Tighten the ACTION (lower its declared risk) to "
            "legitimately earn a lower rung; you cannot declare it down.")
    final_posture = max(resolved, requested) if requested is not None else resolved

    # A binding at refuse_escalate has NO execution path — it is not a fireable grant. Checked on the
    # FINAL posture (not just ``resolved``): a requested escalation TO refuse_escalate would otherwise
    # mint an incoherent guard-less grant whose surfacing falsely reads as a fast-lane (L1-LOW6).
    if final_posture == Posture.REFUSE_ESCALATE:
        return CompileResult.refused(
            REFUSE_CONSTITUTIONAL,
            reasons + ["resolved posture is refuse_escalate — no execution path (the trigger could "
                       "not be classified / authenticated, or refuse_escalate was requested). This is "
                       "not a compilable binding; surface it as an actionable item instead."],
        )

    # (4a) UNCONDITIONAL kill hardening — every guard's kill (if present), at ANY posture (a bad kill is
    # no kill on ANY binding, not only confirm-class — codex-MED1). Three gates, fail-closed:
    #   (i)   DETERMINISTIC — the corpus-aware injected validator accepts it (a fire-time judgment is
    #         no kill). The compiler must not return ok=True on an un-evaluatable kill.
    #   (ii)  PURE — read-only + evaluable by the DIVERSE kill substrate (Slice 3a.5 kill-purity). The
    #         diverse substrate validates its OWN input rather than trusting the validator's grammar
    #         matched (cross_substrate_review_codex_nonreplaceable at the validation layer).
    #   (iii) DRILL-VERIFIED — the kill_drill must ACTUALLY trip the kill (Slice 3a.5 kill-drill compile
    #         gate). 3c proved the drill was PRESENT; this proves it WORKS. Verified on the DIVERSE
    #         substrate (the one that runs the kill at fire-time) — verifying on events.predicate_match
    #         would recreate the exact common-mode coupling the diverse substrate exists to break.
    kill_drill_notes: list[str] = []
    for g in candidate.guard:
        # the prediction-error monitor's envelope (predicted_trajectory.bound), if any, must be a pure
        # predicate the diverse substrate can evaluate — checked for EVERY guard, kill-bearing OR
        # calibration-only (a kill-less guard may still carry a forward-sim; codex L3 — the purity claim
        # must hold regardless of the kill). An impure bound would compile a silently-broken monitor.
        try:
            assert_trajectory_pure(g.predicted_trajectory)
        except KillImpurityError as e:
            return CompileResult.refused(
                REFUSE_CONSTITUTIONAL,
                reasons + [
                    f"guard predicted_trajectory.bound is not a pure predicate the monitor can "
                    f"evaluate: {e}. The bound must be a read-only deterministic predicate."],
            )
        if g.kill_predicate is None:
            continue
        try:
            validator.validate(g.kill_predicate)
        except Exception as e:
            return CompileResult.refused(
                REFUSE_CONSTITUTIONAL,
                reasons + [
                    f"guard kill_predicate is not a deterministic predicate: {e}. The kill must "
                    "reduce to a deterministic 'stop if X' (it is evaluated, never judged)."],
            )
        try:
            assert_kill_pure(g.kill_predicate)
        except KillImpurityError as e:
            return CompileResult.refused(
                REFUSE_CONSTITUTIONAL,
                reasons + [
                    f"guard kill is not PURE — the diverse kill substrate cannot evaluate it: {e}. A "
                    "kill must be a read-only deterministic predicate."],
            )
        # (4a-drill) the kill-drill compile gate, by Kleene verdict on the diverse substrate:
        outcome = kill_outcome(g.kill_predicate, g.kill_drill)
        if outcome is Kleene.FALSE:
            return CompileResult.refused(
                REFUSE_CONSTITUTIONAL,
                reasons + [
                    f"kill_drill does NOT trip the kill (spike {g.spike_id or '?'}): the drill "
                    f"{g.kill_drill!r} is a case the kill lets THROUGH. A drill must be a concrete event "
                    "that TRIPS the kill — an unverified kill is no kill. Supply a drill carrying the "
                    "dangerous field values the kill is meant to catch."],
            )
        if outcome is Kleene.UNKNOWN:
            kill_drill_notes.append(
                f"⚠ kill-drill (spike {g.spike_id or '?'}): the drill trips the kill only via FAIL-SAFE "
                "ambiguity (an absent / unevaluable field), not the kill's positive logic. It WILL trip "
                "at fire-time (the diverse substrate fail-safes to trip), but supply a drill with the "
                "dangerous field PRESENT to demonstrate the kill's real catch (or, for an absence-kill, "
                "spell it `not(field exists)`, which trips confidently on absence).")

    # (4b) the confirm-class guard invariants (the RECEIPT_VERSION=4 mandate). Binding-driven means
    # human-ABSENT at fire-time, so at confirm-class the guard IS the bet-slip the absent human leaves.
    if _confirm_class(final_posture):
        # The adversarial-dissent floor applies to EVERY guard on a confirm-class binding (not only the
        # kill-bearing ones, L1-N4): a self-authored CALIBRATION guard riding alongside a valid kill
        # would pollute the sealed dissent record with a defanged (non-adversarial) bet-slip.
        # Substrate-id comparison is NORMALIZED (strip + casefold, complement-MED1/LOW3): "Claude" /
        # " claude " must not slip the cross-substrate check as if they were a different lineage.
        for g in candidate.guard:
            if _norm_substrate(g.dissent_author) == _norm_substrate(compiler_substrate):
                return CompileResult.refused(
                    REFUSE_CONSTITUTIONAL,
                    reasons + [
                        f"guard dissent is NOT adversarial — authored by {g.dissent_author!r}, the "
                        f"same substrate as the compiler ({compiler_substrate!r}). The dissent must "
                        "come from a DIFFERENT substrate (the cross-substrate non-replaceability "
                        "pattern); a self-authored dissent cannot defeat the operator's bind-time "
                        "self-trust."],
                )
        kill_guards = [g for g in candidate.guard if g.has_kill]
        if not kill_guards:
            return CompileResult.refused(
                REFUSE_CONSTITUTIONAL,
                reasons + [
                    f"binding-driven {final_posture.name} fire requires a guard with a kill "
                    "(rationale + kill_predicate + kill_drill) — none authored. The guard is the "
                    "bet-slip the absent human leaves; presence is mandatory at confirm-class "
                    "(no dissent-without-kill). [3c proves presence; the drill-actually-trips-the-kill "
                    "semantic gate is Slice 3a.5.]"],
            )
        for g in kill_guards:
            # the human PRODUCES the kill (fork-3): it must NOT be authored by the compiling agent
            # (compiler_substrate) NOR by the dissenting agent (dissent_author) — otherwise an agent
            # both raises the objection and writes its own backstop, which is exactly the bind-time
            # self-trust the guard exists to defeat (L1-HIGH1). Normalized comparison (complement-MED1).
            if _norm_substrate(g.kill_authored_by) in (
                    _norm_substrate(compiler_substrate), _norm_substrate(g.dissent_author)):
                return CompileResult.refused(
                    REFUSE_CONSTITUTIONAL,
                    reasons + [
                        f"kill is NOT operator-produced — kill_authored_by={g.kill_authored_by!r} is the "
                        f"compiling agent ({compiler_substrate!r}) or the dissenting agent "
                        f"({g.dissent_author!r}). The human must PRODUCE the kill (not accept a "
                        "generated one); it must come from a party distinct from both agents."],
                )

    # (4b) absent-handling of the KILL — SURFACE (don't refuse) any kill leaf using an absent-blind op
    # at positive depth (e.g. `dmarc != pass`), which fails to trip on a MISSING field → the action
    # fires on the worst (absent-field) case. The doc names this a compiler responsibility ("surface
    # the absent-handling choice"); the author decides at the human-gated ratification, and 3a.5's
    # kill-purity pass hardens it structurally (L2-H2).
    kill_absent_notes: list[str] = []
    for g in candidate.guard:
        if g.kill_predicate is None:
            continue
        blind = _absent_blind_kill_leaves(g.kill_predicate)
        if blind:
            pairs = ", ".join(f"`{f} {op}`" for f, op in blind)
            kill_absent_notes.append(
                f"⚠ KILL absent-handling: leaf(s) {pairs} do NOT trip on a MISSING field — the kill "
                "silently FAILS on absent-field events (e.g. `dmarc != pass` skips no-DMARC mail). If "
                "the worst case (missing field) should trip the kill, spell it `not(field == x)`, which "
                "trips on absent too. [Surfaced for ratification; 3a.5 hardens it.]")

    # (5) behavioral grounding: backtest the trigger → pattern_precision + the priced-looseness
    # surfacing. Always run (scanned == 0 is the cold-start case, flagged, not a refusal).
    report = backtester.replay(candidate.pattern, candidate.trigger_type, window_days=candidate.window_days)
    precision, precision_notes = _score_pattern_precision(report, candidate.pattern)
    tightness = TightnessVector(
        goal_spec=candidate.goal_spec,
        tool_min=candidate.tool_min,
        pattern_precision=precision,
        output_bound=candidate.output_bound,
    )

    fire_count = int(report.get("fire_count", 0))
    fire_exact = bool(report.get("fire_count_exact", False))
    by_cov = dict(report.get("fire_count_by_coverage", {}) or {})
    scanned = int(report.get("scanned", 0))
    warnings = tuple(report.get("warnings", []) or [])
    coverage = report.get("coverage", {}) or {}
    coverage_note = str(coverage.get("honest_note", "")) or (
        "no coverage ledger for this window — fire counts cannot be read as no-fire.")

    if _confirm_class(final_posture):
        approx = "" if fire_exact else " (≥, lower bound — coverage gaps / truncated scan)"
        confirm_cost = (
            f"fires at {final_posture.name}: ~{fire_count}{approx} human confirm(s) over the last "
            f"{candidate.window_days}d of corpus. Looseness is PRICED here — tighten the trigger to "
            "cut confirms, or accept the cost (conservation law: the cost relocates, it does not vanish).")
    else:
        confirm_cost = (
            f"fires at {final_posture.name} (the on-loop fast-lane — reversible + create-only + "
            f"well-grounded): ~{fire_count}{'' if fire_exact else ' (≥)'} autonomous fire(s) over the "
            f"last {candidate.window_days}d of corpus, each notified with cheap undo. No confirms.")

    surfacing = CompileSurfacing(
        resolved_posture=resolved,
        requested_posture=requested,
        downgrade_denied=downgrade_denied,
        tightness=tightness,
        would_fire_count=fire_count,
        would_fire_exact=fire_exact,
        fire_count_by_coverage=by_cov,
        scanned=scanned,
        field_engagement=dict(report.get("field_engagement", {}) or {}),
        backtest_warnings=warnings,
        confirm_cost_note=confirm_cost,
        coverage_note=coverage_note,
        notes=tuple(reasons + kill_absent_notes + kill_drill_notes + precision_notes),
    )

    # (6) mint the SEALED grant (with the guard inside the seal — the anti-defanged-dissent invariant).
    # Born PAUSED, NEVER ACTIVE (L1-MED2, fail-CLOSED): a compiled binding is ratification-READY, not
    # ratified — there is no fire-path yet (Slice 4), and PAUSED→ACTIVE is the explicit governed
    # ratify-write. Minting the fail-closed rung in the CORE means a non-adapter caller (a seed, a
    # test, a future seam) that persists ``result.binding`` directly gets a non-firing grant, not a
    # live autonomous one — the safe default is structural, not adapter discipline. Binding.create
    # re-validates types + the goal-chain floor; the store's PredicateValidator seam re-validates P AND
    # each kill at persist (defense in depth — the compiler validated, the store does not trust it).
    binding = Binding.create(
        created_by=candidate.created_by,
        created_at=candidate.created_at,
        trigger=TriggerSpec(type=candidate.trigger_type, pattern=candidate.pattern),
        goal=candidate.goal,
        tightness=tightness,
        posture=final_posture,
        one_shot=candidate.one_shot,
        guard=candidate.guard,
        status=BindingStatus.PAUSED,
    )
    return CompileResult.compiled(binding, surfacing)


def refuse_undecidable(nl_text: str, *, reason: str = "") -> CompileResult:
    """The UPSTREAM refusal: the proposer could not reduce the NL to ANY candidate predicate (vs a
    malformed candidate, which ``compile_binding`` refuses). Same refuse_class + decomposition path, so
    the adapter has ONE refusal shape regardless of where the predicate failed to form. (The actual
    NL-decomposition — inducing the proxy union — is the conversational authoring layer, iterate-later;
    this surfaces the OFFER.)"""
    why = f": {reason}" if reason else ""
    return CompileResult.refused(
        REFUSE_CONSTITUTIONAL,
        [f"the NL ask {nl_text!r} did not reduce to a deterministic predicate{why}.",
         "a binding cannot ground a fire-time LLM judgment — the trigger must be deterministic."],
        decomposition=[
            "Decompose into a curated UNION of deterministic proxies, each ratified separately, with "
            "the approximation gap surfaced (govern-not-trust: the approximation is governed, never a "
            "fire-time judgment).",
        ],
    )
