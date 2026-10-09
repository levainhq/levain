"""levain.autonomic.trust — invocation_trust = f(signal_auth, intent_provenance, hops).

The CONTEXTUAL / live half of ``posture = f(risk, trust)`` (agent_authority_model.md §2.1). The
trigger taxonomy decomposes invocation_trust into THREE components that must stay UNFUSED (the
no-fuse discipline): signal authentication (is the trigger real / from whom it claims), intent
provenance (where did the *do-X* come from — sets the grounding regime), and hops (each agentic
chain-hop removes the action further from a human anchor — *distance from a human is distance from
grounding*). Per the governance doc, hops lives INSIDE the trust axis (it attenuates trust), NOT as
a third fused number — so :func:`earned_posture` folds all three here and emits ONE rung the policy
resolver clamps against the risk floor.

This is the trust→posture half only. The grounding/confidence/injection REFUSE stack (§1.5) is a
SEPARATE constitutional axis upstream of posture — it lives in ``gates.py`` and answers a different
question ("did we understand the ask at all"), which no trust tier buys back.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from levain.autonomic.posture import Posture

__all__ = ["SignalAuth", "IntentProvenance", "TrustContext", "earned_posture"]


class SignalAuth(IntEnum):
    """How strongly the trigger is authenticated (trigger taxonomy Axis 1), weak→strong.
    Authentication is necessary-not-sufficient (a compromised-but-real account passes every check —
    §1.4), which is why reversibility (the risk floor) is the backstop auth cannot provide."""

    UNAUTHENTICATED = 0   # unknown / unsigned → refuse at the entry (§1.3), don't even classify
    WEAK = 1              # handle-based (iMessage); no DMARC-equivalent alignment check
    AUTHENTICATED = 2     # DMARC-pass email, genuinely-Phill's-phone GPS — strong but not sufficient
    STRONG = 3            # Phill's own authenticated device+app (flowConnect NL); self-clock (cron)


class IntentProvenance(IntEnum):
    """Where the *do-X* came from (trigger taxonomy Axis 2) — sets the grounding regime.

    ``INTENT_BEARING`` — a human/agent requested something AT fire-time (email/message/NL); fire-time
    text exists → the §1.5 ``trigger_quote`` substring check anchors it.
    ``INTENT_FREE`` — a condition became true (location/time/sensor/an afferent proposal); nobody
    requested anything at fire-time → the grounding anchor moves to the BINDING (authored earlier),
    which is structurally weaker than fire-time human text, so it attenuates trust by one rung.
    """

    INTENT_FREE = 0
    INTENT_BEARING = 1


@dataclass(frozen=True)
class TrustContext:
    """The live trust of one invocation.

    ``hops`` = agentic hops from the authenticated human trigger (0 = the human acted directly on
    this; each hop manufactures its own fabricatable "source text," attenuating grounding).
    ``human_present`` = the human is at the fan-in right now (a manual gate invocation / an
    in-session confirm) — the strongest grounding there is, because the human IS the trigger. (It
    suppresses the intent-free penalty: a human choosing to act on a proposal SUPPLIES the
    fire-time intent the proposal lacked. Hops still attenuate even when present — a human approving
    a deep agent chain is grounding a chain that is still deep.)"""

    signal_auth: SignalAuth
    intent_provenance: IntentProvenance
    hops: int = 0
    human_present: bool = False


def _climb(base: Posture, rungs: int) -> Posture:
    """Climb the ladder ``rungs`` steps toward more involvement; negative ``rungs`` never DESCEND
    (trust attenuation only ever raises involvement).

    Trust attenuation TOPS OUT at ``CONFIRM_ELEVATED`` — the most-involved rung that STILL HAS an
    execution path (the human approves with re-auth, SUPPLYING the grounding a deep / intent-free
    chain lost; governance §"deep-chain confirm is epistemically necessary — the operator approving
    IS the grounding the chain lost"). ``REFUSE_ESCALATE`` (no execution path) is reserved for the
    ENTRY failures — ``UNAUTHENTICATED`` (the early return below) and unknown-action / §1.5-screen
    (the gate) — where the trigger cannot be CLASSIFIED at all, NOT for a known authenticated action
    whose risk is merely high. Auto-refusing a deep authenticated chain would deny the human the
    approval the canon's worked example (arrive→research→cross-ref→email) explicitly routes to them
    (L2-H1, 2026-06-24)."""
    return Posture(min(base.value + max(0, rungs), Posture.CONFIRM_ELEVATED.value))


def earned_posture(trust: TrustContext) -> Posture:
    """Map trust → the most-autonomous rung this invocation EARNS, BEFORE the risk floor clamps it.

    Trust earns DOWN toward the floor; ``policy()`` then takes ``max(floor, earned)`` so a
    constitutional floor can never be punched through (§1.4). Hops + intent-free provenance attenuate
    INSIDE this axis (governance doc: not a third fused number). Low signal-auth refuses at the entry
    (§1.3 fail-toward-involvement — you don't classify a trigger you can't authenticate).

    - ``UNAUTHENTICATED`` → ``REFUSE_ESCALATE`` (entry refusal).
    - ``human_present`` → base ``ABOVE_LOOP`` (the human is trigger + fan-in; maximal grounding),
      then climbed by hops alone (the intent-free penalty is suppressed — the human supplies intent).
    - else → a base rung from ``signal_auth``, climbed by the intent-free penalty + each hop.
    """
    if trust.signal_auth == SignalAuth.UNAUTHENTICATED:
        return Posture.REFUSE_ESCALATE

    if trust.human_present:
        base = Posture.ABOVE_LOOP
        climb = max(0, trust.hops)  # human supplies intent → no intent-free penalty; hops still bite
    else:
        base = {
            SignalAuth.STRONG: Posture.ABOVE_LOOP,
            SignalAuth.AUTHENTICATED: Posture.ON_LOOP,
            SignalAuth.WEAK: Posture.CONFIRM,
        }[trust.signal_auth]
        climb = max(0, trust.hops)
        if trust.intent_provenance == IntentProvenance.INTENT_FREE:
            climb += 1  # grounding anchored in the binding, not fire-time text → weaker

    return _climb(base, climb)
