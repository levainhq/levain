"""levain.autonomic.posture — the operator-involvement ladder (agent_authority_model.md §2.5).

The six rungs of how much the operator sits in the loop, ordered by INCREASING involvement. The
ordering is load-bearing: posture resolution is ``max(floor, earned)`` over this ladder — higher =
more involvement, so ``max`` is "the more-involved rung wins" (the constitutional floor can only
ever RAISE involvement, never lower it; §1.4 "no matter how trusted"). Climbing the ladder = toward
the human; descending = toward autonomy. ``refuse_escalate`` is the top: no execution path at all.

The ladder collapses to ONE transport with two knobs (the governance doc): the rungs differ only in
(1) where the fire-point sits vs the human-visibility window, and (2) the silence/timeout default.
This enum is the ordered vocabulary; the two knobs are the ``fail_open`` / ``requires_typed``
properties below, and the transport that realizes the rungs landed at Slice 2
(``levain.autonomic.transport`` + ``levain.autonomic.pending``).
"""
from __future__ import annotations

from enum import IntEnum

__all__ = ["Posture"]


class Posture(IntEnum):
    """Increasing operator involvement (agent_authority_model.md §2.5). The int value IS the
    involvement rank, so ``max()`` selects the more-involved rung (fail-toward-involvement, §1.3)."""

    ABOVE_LOOP = 0        # act; aggregate digest only (full delegation)
    ON_LOOP = 1           # act; per-action notify + cheap undo (the augmentation sweet spot)
    COOLING_OFF = 2       # queue + visible cancel window; auto-fires if not cancelled
    CONFIRM = 3           # propose; act only on approval (in the loop)
    CONFIRM_ELEVATED = 4  # critical: typed confirm / re-auth / multi-step
    REFUSE_ESCALATE = 5   # no execution path; surface as an actionable item

    @property
    def fires_immediately(self) -> bool:
        """True iff this rung may fire WITHOUT a pre-execution human approval — ``above_loop`` and
        ``on_loop`` only (on-loop = act-then-notify-with-undo; a confirm-BEFORE-fire is, by
        definition, in-the-loop). The gate FIRES these immediately; ``needs_confirm`` rungs route
        through the confirm transport; ``refuse_escalate`` has no execution path."""
        return self in (Posture.ABOVE_LOOP, Posture.ON_LOOP)

    @property
    def needs_confirm(self) -> bool:
        """True iff this rung routes through the ConfirmTransport (a propose→reply round-trip):
        ``cooling_off`` / ``confirm`` / ``confirm_elevated``.

        ✅ L2-M2 RESOLVED (Slice 2): ``cooling_off`` is now in the confirm-class set — NOT lumped with
        the fail-CLOSED deferred drop as the Slice-1 ``fires_immediately`` simplification did. Its
        §2.5 contract (FAIL-OPEN — "queue + visible cancel window; AUTO-FIRES if not cancelled") is
        carried by ``fail_open`` below: the gate PROPOSES it like any confirm-class action, and the
        timeout sweep AUTO-FIRES it on silence rather than dropping it. The inversion is closed."""
        return self in (Posture.COOLING_OFF, Posture.CONFIRM, Posture.CONFIRM_ELEVATED)

    @property
    def fail_open(self) -> bool:
        """Knob 1 — the silence/timeout default. ``cooling_off`` AUTO-FIRES on silence (fail-open:
        "probably fine, but visible"); ``confirm`` / ``confirm_elevated`` DROP on silence (fail-CLOSED,
        §1.4 — the more irreversible, the more silence defaults to NOT firing). ``cooling_off`` and
        ``confirm`` are the same transport code with this one bit flipped."""
        return self is Posture.COOLING_OFF

    @property
    def requires_typed(self) -> bool:
        """Knob 2 — ``confirm_elevated`` needs a typed / re-auth confirm (critical / public-broadcast /
        deep-chain-outbound), not a one-tap approve. Surfaced to the transport so it can demand the
        stronger affordance; ``confirm`` is the one-tap rung."""
        return self is Posture.CONFIRM_ELEVATED
