"""levain.autonomic.transport — the confirm-rung seam: propose to the human, decide out-of-band.

The EFFERENT membrane's human-gate, made injectable. A confirm-class posture (cooling-off / confirm /
confirm-elevated) does NOT fire at the gate — it SURFACES the proposed action to the operator and
fires (or drops) only on the operator's reply. ``ConfirmTransport`` is that surface, the efferent
sibling of the afferent ``Completer`` + the ``Executor``: the clean gate decides the posture; a
flow-side / adopter-side transport carries the propose→reply round-trip over a concrete channel
(flowConnect push + reply).

The shape is the canon's, decided 2026-06-07 (``awareness_bus_governance_design.md`` §"the §2.5 ladder
= ONE transport, two knobs"): the six rungs are ONE pending-action queue + ONE push/reply channel,
parameterized by two bits — where the fire-point sits vs the visibility window, and the silence
default. The confirm-map is the proven 2b directive-undo map with the decision moved BEFORE the fire
instead of after. So this seam is deliberately NARROW + ASYNC:

  - ``propose(proposal) -> bool`` SURFACES the proposed action (a push) and returns whether the
    surface reached the operator. It does NOT block for the reply and it does NOT return the verdict.
  - the verdict returns OUT-OF-BAND: the operator's approve/deny (or a timeout) re-enters the gate
    via ``EfferentGate.resolve(pending_id, ConfirmDecision)``. The durable record that survives the
    gap is the persisted :class:`~levain.autonomic.pending.PendingAction` (keyed by ``pending_id``).

Why async, not a blocking ``confirm() -> decision``: the reply arrives out-of-band (a phone tap,
possibly much later) and routes back by name through the proven push/reply channel — the same
mechanism 2b uses. A blocking transport would force the gate process to stay alive across the wait
(wrong for the headless/ambient case the autonomic loop is built for). The pending-action store +
the separate ``resolve`` re-entry IS the faithful shape, and it is what the autonomous binding case
(Slice 3) needs unchanged.

The transport sees ONLY a human-readable summary + the two knobs — never trust / risk / confidence
(the gate already resolved the posture; the transport SURFACES, it never re-decides). Keeping it
narrow keeps the concrete channel (the fossil) swappable and unable to smuggle policy back in. A
concrete transport is a TRUSTED adapter; it must NEVER raise into the gate (a failed surface is
``propose() -> False``, not a crash — "a failed surface beats a crashed gate").
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["ConfirmProposal", "ConfirmDecision", "ConfirmTransport"]


@dataclass(frozen=True)
class ConfirmProposal:
    """What the gate hands the transport to SURFACE to the operator — the human-readable face of a
    pending efferent action, plus the two governance knobs. Frozen: a proposal is a fact handed to
    the transport. It carries NO trust / risk / confidence (the gate already resolved the posture);
    the transport renders + delivers it, it never re-decides.

    - ``pending_id`` keys the reply back to the persisted :class:`PendingAction` (the operator's
      approve/deny carries this id into ``EfferentGate.resolve``).
    - ``summary`` is the operator-facing description of what will fire (already §1.2-attributed by
      the adapter — signed as the agent, FOR the operator, never AS them).
    - ``posture`` is the resolved rung name (cooling_off / confirm / confirm_elevated).
    - ``fail_open`` (knob 1, the silence default): True ⇒ silence AUTO-FIRES (cooling-off); False ⇒
      silence DROPS (confirm / confirm-elevated, §1.4 fail-closed).
    - ``requires_typed`` (knob 2): True ⇒ a typed / re-auth confirm, not a one-tap (confirm-elevated).
    - ``expires_at`` is the ISO instant the silence default kicks in (None ⇒ no auto-resolution; the
      proposal waits for an explicit reply).
    """

    pending_id: str
    action_name: str
    summary: str
    posture: str
    fail_open: bool
    requires_typed: bool
    expires_at: str | None = None


@dataclass(frozen=True)
class ConfirmDecision:
    """The verdict fed back into ``EfferentGate.resolve`` — from a human reply OR a timeout sweep.

    - ``approved`` drives fire-or-drop.
    - ``by`` is the decider, constrained to the FROZEN receipt ``gate.by`` enum's relevant values:
      ``"human"`` for an operator reply; ``"on-loop"`` for a timeout-driven auto-resolution (an
      un-cancelled cooling-off fire, or a confirm window-elapsed drop — the autonomous-after-window
      decider; the frozen enum has no ``timeout`` value, so ``on-loop`` stands, the closest
      autonomous decider — refuse-vs-timeout sharpening is receipt-contract checkpoint #2).
    - ``first_estimate`` is the operator's forced PRE-TRUTH read at the confirm (the forced-first-
      estimate primitive) → the receipt's ``actor_first_estimate``. ``None`` when none was supplied
      (a one-tap approve / a timeout).
    - ``typed_proof`` is the typed / re-auth token a ``confirm_elevated`` (``requires_typed``) rung
      demands — the gate REFUSES an approved-elevated resolve without it (the stronger affordance the
      rung promises, enforced not just rendered). ``None`` for the one-tap ``confirm`` rung.
    - ``reason`` is a short human/audit string (e.g. ``"cooling_off_window_elapsed"``).
    - ``withdraw`` (only with ``approved=False``) marks a drop caused by an infrastructure fault, not by
      a decider: the pending is consumed, but a journaled run is NOT cancelled (its hold is withdrawn),
      so re-delivering the event proposes the effect again.
    """

    approved: bool
    by: str = "human"
    first_estimate: Any | None = None
    typed_proof: str | None = None
    reason: str = ""
    withdraw: bool = False

    def __post_init__(self) -> None:
        # A decision is an authority boundary: a truthy string ("false") must never read as approval.
        for name in ("approved", "withdraw"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ConfirmDecision.{name} must be a bool, got {type(getattr(self, name)).__name__}")
        if self.by not in ("human", "on-loop"):
            raise ValueError(f"ConfirmDecision.by must be 'human' or 'on-loop', got {self.by!r}")
        if self.withdraw and (self.approved or self.by != "on-loop"):
            # a withdraw leaves the run open; a person's "no" must cancel it, so only the system's own
            # infrastructure path may withdraw
            raise ValueError("ConfirmDecision.withdraw is an on-loop drop: not approved, not by a human")
        if self.typed_proof is not None and not isinstance(self.typed_proof, str):
            raise TypeError("ConfirmDecision.typed_proof must be a string or None")
        if not isinstance(self.reason, str):
            raise TypeError("ConfirmDecision.reason must be a string")


@runtime_checkable
class ConfirmTransport(Protocol):
    """The propose-half of the confirm channel. ``name`` is the transport's identity (logging/audit);
    ``propose`` SURFACES the proposed action and returns whether the surface reached the operator. A
    concrete transport is a TRUSTED adapter — it never sees the trust/risk inputs, and it must NEVER
    raise into the gate (a failed surface is ``propose() -> False``). The decision returns out-of-band
    through ``EfferentGate.resolve``; the transport does not block for it."""

    name: str

    def propose(self, proposal: ConfirmProposal) -> bool: ...
