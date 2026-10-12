"""levain.autonomic.graduation — the §2.6 meta-loop DECISION (Slice 4c).

The binding layer (3a-4b) compiles, seals, fires, and chains a standing grant, and the fire-path
ACCRUES a track record (``Graduation.fire_count`` / ``clean_count`` — bumped by ``record_fire`` on
every clean fire, in-band AND out-of-band). This module closes the loop: it reads that track record
and PROPOSES a posture loosening — "fired N× clean → promote one rung toward autonomy?".

This is **Memory-Is-Governance at the action layer** (``agent_authority_model.md`` §2.6): the operator
governs the GRADIENT (ratifies the looser posture against the evidence), the agent earns its way DOWN it
with evidence, and the constitutional risk FLOOR is the limit graduation can never punch. The decision
NEVER promotes autonomously — it returns a :class:`GraduationProposal` the operator ratifies
(govern-not-trust; the §2.6 "approve POLICY changes, not individual actions").

**The mechanism (why a re-ratification, not an in-place edit).** A binding's posture is SEALED. A
posture promotion is a DIFFERENT governance core → a different id → a re-ratification: mint a NEW sealed
grant at the looser posture and ``replace_atomic`` it for the old. The fire-path's seam #2 (Slice 4c)
fires a binding at ``max(risk_floor(risk), ratified_posture)`` — the operator-ratified posture down to
the unbuyable floor — so a promoted grant ACTUALLY fires looser (under the 4a/4b ``max(policy, ratified)``
it would have been re-floored back up to the static ``earned_posture`` → silent theater).

**The three gates (all must hold to propose).**
  (a) EVIDENCE — ``clean_count >= min_clean_fires`` AND ``clean_count == fire_count`` (ALL-clean). Today
      ``clean == fire`` always (no post-hoc demotion channel — that is 4d); the equality is checked so
      4c is forward-correct for when 4d's demotion makes the two diverge.
  (b) NOT DEPTH-BOUNDED — no EXTERNAL link at hops >= :data:`DEEP_OUTBOUND_MIN_HOPS`. A deep-outbound
      chain's confirm does real EPISTEMIC work ("the operator approving IS the grounding the chain lost",
      ``awareness_bus_governance_design.md`` § "deep-chain confirm is epistemically necessary, not
      trust-tax"), so it canNOT be graduated away by track record the way a shallow lane can. DERIVED at
      decision time from the SEALED goal (per-link ``external`` from the sealed tools, hops = link_index)
      — NEVER a stored flippable bool (the 3a ``Graduation`` comment: storing it would let a tamper
      re-enable autonomy escalation on the most dangerous chains, seal-blind).
  (c) ROOM TO LOOSEN — the current posture is STRICTLY above the unbuyable risk floor, so one rung looser
      is still >= the floor (graduation earns DOWN toward the floor, never below — §1.4).

Stdlib-only; corpus-agnostic; PURE (the per-binding + per-link risk are INJECTED resolvers, structurally
bound to the sealed tools — the core never imports flow's manifest). The decision is deterministic.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from levain.autonomic.binding import Binding, BindingStatus, BindingStore
from levain.autonomic.policy import risk_floor
from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk

__all__ = [
    "DEEP_OUTBOUND_MIN_HOPS",
    "DEFAULT_MIN_CLEAN_FIRES",
    "RiskResolver",
    "LinkRiskResolver",
    "GraduationProposal",
    "one_rung_looser",
    "is_depth_bounded",
    "propose_graduation",
]

_log = logging.getLogger("levain.autonomic.graduation")

# The §2.6 chain-depth bound: a chain is "deep-outbound" (does NOT graduate) iff it has an EXTERNAL link
# at hops >= this — an outbound link whose content is grounded in >= 1 hop of upstream AGENT OUTPUT (link 0
# fires directly on the authenticated trigger; link 1+ is grounded in agent processing the human never
# saw). 1 is the principled minimum (any agent-output-grounded outbound). A single-link / hops-0 outbound
# is NOT depth-bounded here — its external risk floor (>= CONFIRM, §1.4) already bars graduation below
# confirm, so the floor handles it; the depth-bound is the EXPLICIT governance refusal on top, for the
# DEEP case where the confirm does epistemic work the floor alone wouldn't name.
DEEP_OUTBOUND_MIN_HOPS = 1

# The §2.6 worked-example evidence bar ("fired 20× at confirm, all clean → promote"). The flow adapter's
# ``graduate`` CLI exposes a ``--min-clean`` override (e.g. for a demo); production defaults to this.
DEFAULT_MIN_CLEAN_FIRES = 20

# The injected risk seams (structurally bound to the SEALED tools — the core stays manifest-agnostic).
#   RiskResolver:     (binding) -> ActionRisk     — the WHOLE-binding aggregate risk, for the floor (the
#                                                   SAME ``aggregate_risk`` the binding was ratified at).
#   LinkRiskResolver: (binding, link_index) -> ActionRisk — link i's risk from its sealed tools (the SAME
#                                                   per-link resolver the chain executor uses), for the
#                                                   depth-bound's per-link ``external`` check.
RiskResolver = Callable[[Binding], ActionRisk]
LinkRiskResolver = Callable[[Binding, int], ActionRisk]


def one_rung_looser(posture: Posture) -> Posture | None:
    """The next rung TOWARD autonomy (one less operator involvement), or ``None`` if already at the
    most-autonomous rung (``ABOVE_LOOP``). The :class:`Posture` IntEnum is ordered by INCREASING
    involvement, so 'looser' = ``value - 1``."""
    if posture.value <= Posture.ABOVE_LOOP.value:
        return None
    return Posture(posture.value - 1)


def is_depth_bounded(binding: Binding, link_risk_resolver: LinkRiskResolver) -> bool:
    """True iff ``binding`` is a DEEP-OUTBOUND chain that must NOT graduate (§2.6 bounded by chain-depth).
    It is depth-bounded iff it has an EXTERNAL (outbound) link at hops >= :data:`DEEP_OUTBOUND_MIN_HOPS`
    — an outbound link grounded in >= 1 hop of upstream AGENT OUTPUT, where the confirm does real
    epistemic work track record cannot retire. DERIVED from the SEALED goal (per-link ``external`` from
    the sealed tools, hops = link_index). FAIL-CLOSED: a per-link risk-resolution fault (an undeclared
    tool — e.g. a manifest that dropped one post-ratification) is treated as DEPTH-BOUNDED (an
    unclassifiable link must never be assumed shallow-and-graduatable).

    ⚠ This keys on ``external`` ALONE — narrower than its own "agentic distance degrades grounding"
    rationale (an IRREVERSIBLE-but-internal link at hops>=1 also does epistemic work the confirm earns).
    It is CORRECT today only as a COMPOSITION with the unbuyable floor: ``risk_floor`` independently lifts
    ``reversible=False`` / ``financial`` / ``CRITICAL`` to >= CONFIRM (policy.py), and graduation's
    room-check (``current <= floor``) blocks graduating below it — so the floor covers the irreversible-
    internal case the depth-bound misses. The current manifest has NO irreversible-internal tool
    (``mail.send`` is the only irreversible one, and it is external), so the gap is inert. **If an
    irreversible-internal tool is ever added, extend this to ``external OR not reversible at hops>=1``**
    (L2 L3) — don't let the depth-bound silently lean entirely on the floor for that case."""
    for i in range(len(binding.goal)):
        if i < DEEP_OUTBOUND_MIN_HOPS:
            continue  # link 0 (hops 0) fires on the authenticated trigger — not "deep" agent output
        try:
            risk = link_risk_resolver(binding, i)
        except Exception:  # noqa: BLE001 — an unclassifiable link bars graduation, fail-closed
            _log.warning("graduation: binding %s link %d risk is unclassifiable — treating as "
                         "DEPTH-BOUNDED (fail-closed, no graduation)", binding.binding_id, i)
            return True
        if risk.external:
            return True
    return False


@dataclass(frozen=True)
class GraduationProposal:
    """A §2.6 graduation proposal: ``source`` has earned a one-rung-looser posture (``to_posture``). NOT
    applied — a proposal the operator RATIFIES (govern-not-trust). :meth:`build_promoted` mints the
    re-ratified grant; the adapter applies it via ``BindingStore.replace_atomic`` (atomic: persist the new
    grant ACTIVE + revoke the old)."""

    source: Binding
    to_posture: Posture

    @property
    def old_binding_id(self) -> str:
        return self.source.binding_id

    @property
    def from_posture(self) -> Posture:
        return self.source.posture

    @property
    def clean_count(self) -> int:
        return self.source.graduation.clean_count

    @property
    def fire_count(self) -> int:
        return self.source.graduation.fire_count

    def build_promoted(self, *, status: BindingStatus = BindingStatus.PAUSED,
                       created_at: str | None = None) -> Binding:
        """Mint the re-ratified (promoted) binding: SAME trigger / goal / tightness / one_shot + the
        SAME sealed-floor ``guard`` AND ``guard_additions`` (a graduation must NEVER drop a safety kill),
        the looser ``to_posture``, and a FRESH ``Graduation()`` (each rung earns its OWN track record —
        seam 5: clean fires earned at a stricter posture don't prove clean fires at a looser one, and it
        blocks graduation-laundering). A DIFFERENT sealed core (the looser posture) → a DIFFERENT id →
        ``replace_atomic`` supersedes the old grant.

        Defaults to ``PAUSED`` (codex L3 / L2 — FAIL-CLOSED, matching ``Binding.create``'s deliberate
        PAUSED default: a caller that forgets the lifecycle gets a NON-firing grant, never a live looser
        autonomous one — the graduation human-gate must be STRUCTURE, not discipline). The graduation
        APPLY path passes ``status=ACTIVE`` EXPLICITLY — the operator's ``graduate --yes`` IS the
        policy-change ratification (§2.6), and the adapter asserts ``is_fireable`` before ``replace_atomic``
        persists it ACTIVE. ``created_at`` defaults to the source's (PURE — no clock in the core; the id
        still differs by posture); the adapter passes a fresh timestamp to stamp the re-ratification time."""
        promoted = Binding.create(
            created_by=self.source.created_by,
            created_at=created_at if created_at is not None else self.source.created_at,
            trigger=self.source.trigger,
            goal=self.source.goal,
            tightness=self.source.tightness,
            posture=self.to_posture,
            one_shot=self.source.one_shot,
            status=status,
            guard=self.source.guard,
            graduation=None,  # FRESH (seam 5) — Binding.create defaults to Graduation()
        )
        # carry the UNSEALED tightening additions forward (extra kills the operator hardened in — a
        # graduation that loosens the posture must not also DROP safety). Additions are outside the seal,
        # so ``replace`` leaves the new id + floor-seal intact; the store re-validates each kill on persist.
        if self.source.guard_additions:
            promoted = replace(promoted, guard_additions=self.source.guard_additions)
        return promoted


def propose_graduation(
    binding: Binding,
    *,
    risk_resolver: RiskResolver,
    link_risk_resolver: LinkRiskResolver,
    min_clean_fires: int = DEFAULT_MIN_CLEAN_FIRES,
) -> GraduationProposal | None:
    """The §2.6 graduation DECISION (pure, deterministic, corpus-agnostic): does this binding's CLEAN
    track record earn a one-rung-looser posture? Returns a :class:`GraduationProposal` (the operator
    ratifies it — NEVER an autonomous promotion) or ``None``. Proposes iff ALL of:
      (a) EVIDENCE — ``clean_count >= min_clean_fires`` AND ``clean_count == fire_count`` (all-clean);
      (b) the binding is currently ACTIVE + valid-seal (``is_active``) — a paused/revoked/tampered grant
          is not earning a looser posture;
      (c) NOT depth-bounded (no external link at hops >= DEEP_OUTBOUND_MIN_HOPS — deep-outbound chains
          don't graduate);
      (d) ROOM — the current posture is STRICTLY above the unbuyable risk FLOOR.
    FAIL-CLOSED: an unclassifiable binding (the whole-binding ``risk_resolver`` raises — a manifest that
    dropped a tool post-ratification) does NOT graduate. ``to_posture`` = one rung looser (guaranteed
    >= the floor by (d))."""
    if min_clean_fires < 1:
        raise ValueError(f"min_clean_fires must be >= 1, got {min_clean_fires}")
    grad = binding.graduation
    # (a) evidence: enough clean fires AND all-clean (clean == fire; forward-correct for 4d's demotion).
    if grad.clean_count < min_clean_fires or grad.clean_count != grad.fire_count:
        return None
    # (b) only a currently-FIREABLE, untampered, repeating grant graduates. Use the canonical FIRE-VIEW
    # predicate (``is_fireable``), NOT just ``is_active`` (codex L3 MED): ``is_active`` is status+seal
    # only, so a confirm-class ACTIVE binding with NO sealed-floor kill — BARRED from firing by
    # ``list_active`` — would otherwise graduate on (unsealed, injectable) evidence into a kill-mandate-
    # free ON_LOOP grant that DOES fire, converting a barred grant into a fireable one. And a ONE-SHOT
    # never graduates (it fires once then revokes; promoting it on injected/stale evidence is meaningless).
    if binding.one_shot or not BindingStore.is_fireable(binding):
        return None
    # (c) the §2.6 chain-depth bound FIRST — a CATEGORICAL refusal (this kind of chain never graduates,
    # the confirm does real epistemic work), checked before the mechanical floor-room limit so a
    # deep-outbound chain surfaces the GOVERNANCE reason even when it also happens to sit at its floor.
    if is_depth_bounded(binding, link_risk_resolver):
        return None
    # (d) ROOM — the unbuyable floor from the SEALED tools (fail-closed on an unclassifiable grant).
    try:
        floor = risk_floor(risk_resolver(binding))
    except Exception:  # noqa: BLE001 — a grant we can no longer classify does NOT earn looser
        _log.warning("graduation: binding %s risk is unclassifiable — NOT graduating (fail-closed)",
                     binding.binding_id)
        return None
    current = binding.posture
    if current.value <= floor.value:
        return None  # already AT (or below) the floor — nothing left to earn
    looser = one_rung_looser(current)
    # current > floor >= ABOVE_LOOP, so one_rung_looser is never None here; assert for the type-narrower.
    assert looser is not None and looser.value >= floor.value
    return GraduationProposal(source=binding, to_posture=looser)
