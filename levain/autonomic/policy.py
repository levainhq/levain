"""levain.autonomic.policy — posture = policy(risk, trust) (agent_authority_model.md §2 + the
awareness-bus governance resolver).

The two-stage resolver's STAGE 2. Stage 1 (the §1.5 refuse stack, ``gates.py``) runs UPSTREAM — a
refused trigger never reaches posture ("did we understand the ask" ⊥ "how dangerous is the ask").
This module computes the constitutional risk FLOOR first (max-only, unbuyable — §1.4 "no matter how
trusted"), lets trust earn DOWN toward it (``earned_posture``, with hops attenuating inside the
trust axis), and never below it. Pure function, manifest-driven, NO model in the posture decision —
the load-bearing ``structural_invariants_beat_discipline`` boundary (model proposes the action;
structure disposes the danger).
"""
from __future__ import annotations

from levain.autonomic.posture import Posture
from levain.autonomic.risk import ActionRisk, RiskClass
from levain.autonomic.trust import TrustContext, earned_posture

__all__ = ["risk_floor", "policy"]


def risk_floor(risk: ActionRisk) -> Posture:
    """The constitutional floor for an action's declared risk — the MINIMUM involvement, computed
    BEFORE trust and only ever RAISING it (§1.4). Floors compose by ``max`` (the strictest wins).

    - any real side-effect is ≥ ``on_loop`` (never blind ``above_loop`` for a thing that DOES
      something — above-loop is earned by track record at the meta-loop, not a default).
    - ``external`` → ≥ ``confirm`` (§1.4 corollary: outbound collapses to irreversible — the
      recipient saw it).
    - not ``reversible`` → ≥ ``confirm`` (§1.4: irreversible, no matter how trusted).
    - ``financial`` → ≥ ``confirm_elevated`` (money is the hardest floor).
    - ``CRITICAL`` class → ≥ ``confirm_elevated``.
    """
    floor = Posture.ON_LOOP
    if risk.external:
        floor = max(floor, Posture.CONFIRM)
    if not risk.reversible:
        floor = max(floor, Posture.CONFIRM)
    if risk.financial:
        floor = max(floor, Posture.CONFIRM_ELEVATED)
    if risk.cls == RiskClass.CRITICAL:
        floor = max(floor, Posture.CONFIRM_ELEVATED)
    return floor


def policy(risk: ActionRisk, trust: TrustContext) -> Posture:
    """Resolve the posture: ``max(risk_floor(risk), earned_posture(trust))``.

    Trust earns DOWN toward the floor (hops + intent-free attenuate inside ``earned_posture``); the
    floor clamps it UP. The ``max`` is why trust can NEVER punch through a constitutional floor
    (§1.4) — an external/irreversible/financial action stays ≥ confirm even at maximal trust, and a
    grounding-poor multi-hop trigger climbs ABOVE a low floor. Pure: same inputs → same rung,
    auditable, no model in the loop."""
    return max(risk_floor(risk), earned_posture(trust))
