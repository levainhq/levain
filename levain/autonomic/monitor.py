"""levain.autonomic.monitor — the runtime PREDICTION-ERROR monitor (Slice 3a.5).

The kill (``levain.autonomic.kill``) is the KNOWN-danger backstop: a deterministic "stop if X" the
human authored for a danger they could NAME at bind-time. The prediction-error monitor is the
UNKNOWN-danger backstop: the binding carries its OWN forward-simulation (the Guard's
``predicted_trajectory``), and at fire-time the gate diffs the ACTUAL trajectory against it. Anything
the binding did NOT predict is, by definition, outside what the human ratified — so a divergence trips
``terminal_state=killed`` (``decision_influence_receipt_contract.md`` §2.1).

This is load-bearing precisely WHEN a binding GRADUATES to on-loop (autonomous, human-absent at fire):
a confirm-class binding is human-gated, so the human IS the unknown-danger backstop; an on-loop
binding has no one in the room, so the prediction-error monitor takes that seat.

Two design commitments:
  - **DIVERSE substrate.** The monitor reuses the diverse, fail-safe Kleene evaluator from
    ``levain.autonomic.kill`` — NOT the executor, NOT flow's ``events.py``. The same common-mode argument
    as the kill: the thing that judges "did the world match the prediction" must fail differently from
    the thing that produced the action.
  - **FAIL-SAFE.** Divergence is declared unless the actual is PROVABLY within the predicted envelope.
    ``within_envelope == Kleene.TRUE`` ⇒ no divergence; ``FALSE`` (a predicted invariant is violated)
    OR ``UNKNOWN`` (the observation is missing/partial, so the prediction can't be confirmed) ⇒
    diverged → kill. "I cannot confirm the world matches what I predicted" must STOP an autonomous
    fire, never permit it.

The ``predicted_trajectory`` is an OPAQUE dict on the schema (the binding's own forward-simulation).
The monitor reads ONE optional machine-checkable key, ``bound`` — a deterministic predicate AST (the
same grammar a kill uses) describing the envelope the actual post-state must satisfy. A
``predicted_trajectory`` with NO ``bound`` is DESCRIPTIVE-only (free-form text/structure for the human);
the monitor is INERT for it (there is no envelope to violate) — prediction-error protection is OPT-IN
via the ``bound``, the same way a kill is opt-in via ``kill_predicate``. Opting in is the KEY, not its
value: a ``bound`` key that is present but holds no predicate (``null``, a string, a list) is not
"no bound". It is an envelope that cannot be evaluated, and an absent envelope is not consent to fire
unwatched, so it is refused at bind time (:func:`assert_trajectory_pure`) and, if one reaches the
fire path anyway, reads as diverged (:func:`within_envelope` returns ``UNKNOWN``).

Stdlib-only; corpus-agnostic; pure. The ``TrajectoryObserver`` seam is INJECTED (Slice 4 wires the
real world-observation; the core never observes the world directly — the anti-cycle rule).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from levain.autonomic.kill import Kleene, KillImpurityError, assert_kill_pure, kill_outcome

__all__ = [
    "TrajectoryObserver",
    "trajectory_bound",
    "bound_declared",
    "assert_trajectory_pure",
    "within_envelope",
    "prediction_diverged",
]


@runtime_checkable
class TrajectoryObserver(Protocol):
    """The injected seam that snapshots the ACTUAL trajectory for an about-to-fire action — the
    diverse counterpart to the ``Executor`` (which ACTS; this OBSERVES). Returns a flat/event-shaped
    dict the monitor evaluates the ``bound`` predicate against (the same shape ``kill_outcome``
    accepts). Slice 4 wires the real observer (reading the world-state the binding's prediction is
    about); the gate runs the monitor only when an observer is wired AND the firing binding carries a
    ``predicted_trajectory`` with a ``bound`` (inert otherwise — no behavior change for Slices 1-3)."""

    name: str

    def observe(self, action_name: str, payload: str, *, context_id: str) -> dict[str, Any]: ...


def trajectory_bound(predicted_trajectory: Any) -> dict[str, Any] | None:
    """Extract the machine-checkable envelope predicate from a ``predicted_trajectory``, or ``None``
    if there is none to evaluate (no ``bound`` key, a non-dict trajectory, or a ``bound`` that is not a
    predicate — :func:`bound_declared` tells the last case apart). Centralizes the opt-in contract so
    the compiler (purity-check) and the gate (run-time) read the SAME key."""
    if not isinstance(predicted_trajectory, dict):
        return None
    bound = predicted_trajectory.get("bound")
    return bound if isinstance(bound, dict) else None


def bound_declared(predicted_trajectory: Any) -> bool:
    """True iff the trajectory carries a ``bound`` KEY, whatever its value. A declared bound opts the
    binding into the monitor; one that is not a predicate dict cannot be evaluated and fails closed."""
    return isinstance(predicted_trajectory, dict) and "bound" in predicted_trajectory


def assert_trajectory_pure(predicted_trajectory: Any) -> None:
    """COMPILE-time gate: if a ``predicted_trajectory`` carries a ``bound``, that bound must be a pure,
    deterministic predicate the monitor can evaluate (same purity contract as a kill — the monitor
    reuses the kill evaluator). RAISES :class:`~levain.autonomic.kill.KillImpurityError` on an impure
    bound. A descriptive-only (bound-less) trajectory passes (nothing to check). The compiler calls
    this so a binding whose monitor CANNOT evaluate its own envelope is refused, not minted with a
    silently-inert (always-diverging) monitor."""
    if bound_declared(predicted_trajectory) and not isinstance(predicted_trajectory["bound"], dict):
        # A declared bound that is not a predicate (``null`` included) is an envelope nobody can check.
        # Treating it as "no bound" would leave the monitor inert while the grant asks for one.
        raise KillImpurityError(
            f"predicted_trajectory 'bound' must be a predicate dict, got {type(predicted_trajectory['bound']).__name__}"
            " (omit the key for a descriptive trajectory)")
    bound = trajectory_bound(predicted_trajectory)
    if bound is not None:
        assert_kill_pure(bound)


def within_envelope(predicted_trajectory: Any, actual: Any) -> Kleene:
    """Three-valued: is the ``actual`` observation PROVABLY within the predicted envelope? Evaluates
    the trajectory's ``bound`` predicate against ``actual`` on the diverse Kleene substrate.
    ``Kleene.TRUE`` = provably within; ``FALSE`` = a predicted invariant is violated; ``UNKNOWN`` =
    can't confirm (missing/partial observation). A bound-LESS (descriptive) trajectory returns
    ``TRUE`` — there is no envelope to fall outside of, so it is vacuously "within" (the monitor is
    inert; the caller :func:`prediction_diverged` reports no divergence). A DECLARED bound that is not a
    predicate returns ``UNKNOWN``: it cannot confirm anything, so it fails closed."""
    bound = trajectory_bound(predicted_trajectory)
    if bound is None:
        return Kleene.UNKNOWN if bound_declared(predicted_trajectory) else Kleene.TRUE
    return kill_outcome(bound, actual)


def prediction_diverged(predicted_trajectory: Any, actual: Any) -> tuple[bool, str]:
    """The monitor's verdict: ``(diverged, reason)``. FAIL-SAFE — ``diverged`` is True unless the
    actual is PROVABLY within the predicted envelope (``within_envelope`` is exactly ``Kleene.TRUE``).

      - no ``bound`` (descriptive trajectory) → ``within_envelope`` is TRUE → ``(False, ...)`` (inert).
      - bound satisfied → TRUE → ``(False, ...)`` (the actual matched the prediction).
      - bound violated → FALSE → ``(True, ...)`` (a predicted invariant broke — diverged → KILL).
      - bound unconfirmable → UNKNOWN → ``(True, ...)`` (can't confirm the world matched → KILL).
      - a declared ``bound`` that is not a predicate → UNKNOWN → ``(True, ...)`` (KILL).
    """
    outcome = within_envelope(predicted_trajectory, actual)
    if outcome is Kleene.TRUE:
        if trajectory_bound(predicted_trajectory) is None:
            return False, "no machine-checkable bound — descriptive trajectory; monitor inert"
        return False, "actual observation is within the predicted envelope"
    if outcome is Kleene.FALSE:
        return True, "actual observation VIOLATES the predicted envelope (a predicted invariant broke)"
    if trajectory_bound(predicted_trajectory) is None:
        return True, "the declared bound is not a predicate, so nothing can be confirmed — fail-safe kill"
    return True, ("actual observation cannot be confirmed within the predicted envelope "
                  "(missing/partial observation) — fail-safe kill")
