"""levain.firing.openhands.gate — mounting the efferent gate on the OpenHands runtime (**K3**).

:mod:`levain.firing.gate` decides WHAT an action is. This module is the adapter that makes that
decision *bite*: it drives the SDK's confirmation seam so an efferent action is **halted before
it executes** and handed to the human, and it verifies its own wiring rather than assuming it.

**The seam we mount on (all of it already exists — K3 builds a classifier and a report, not a
state machine).** Before executing a batch of tool calls the agent asks
``state.security_analyzer`` for each action's risk, passes those to
``state.confirmation_policy``, and if any says confirm it sets
``ConversationExecutionStatus.WAITING_FOR_CONFIRMATION`` and returns **with the actions
un-executed**. The operator then either lets the next ``run()`` execute them, or calls
``reject_pending_actions(reason)`` — which emits a ``UserRejectObservation`` back into the
conversation, so the entity LEARNS the action did not happen instead of narrating success over a
world that never changed.

**Why we do not use the SDK's own ``LLMSecurityAnalyzer``.** It asks the model to predict its own
risk (``ActionEvent.tool_call`` can carry a model-supplied ``security_risk`` field). That is the
false-all-clear class one layer below the exit codes: a gate the entity can talk its way through
is not a gate. :class:`LevainEfferentAnalyzer` reads the TOOL, never the model's opinion of
itself.

**Riding the risk channel, said out loud so nobody mistakes it for a risk assessment.** The
confirmation policy is handed a ``SecurityRisk`` and nothing else, so an efferent/afferent
classification has to travel on that channel to reach the gate. We map efferent → ``HIGH`` and
afferent/inert → ``LOW``, and pair it with :class:`ConfirmEfferent` — named in OUR vocabulary
precisely so a future reader does not conclude Levain graded these actions for danger. It did
not. ``git push`` is efferent and routine; the gate has no opinion about how bad it would be.

**The wiring is verified, not trusted** (:func:`arm_efferent_gate`). The two halves fail in
opposite directions: with the policy set but our analyzer missing, every risk arrives ``UNKNOWN``
and :class:`ConfirmEfferent` gates everything — over-gating, loud and safe. With the analyzer set
but the policy missing, the SDK's default ``NeverConfirm`` waves **everything** through: a
session that reports itself gated and is not. That second one is the whole failure mode this
keystone exists to prevent, so arming reads both values back off the state and refuses to
continue if either did not take. ``Conversation.__init__`` swallowing unknown kwargs
(``**_: object``) is the concrete reason a constructor-kwarg attempt would have failed silently.
"""
from __future__ import annotations

from typing import Any

from openhands.sdk.security.analyzer import SecurityAnalyzerBase
from openhands.sdk.security.confirmation_policy import ConfirmationPolicyBase
from openhands.sdk.security.risk import SecurityRisk

from levain.firing.gate import (
    ActionClass,
    Classification,
    PendingEfferent,
    classify_action,
)

__all__ = [
    "COMMAND_DISPLAY_LIMIT",
    "ConfirmEfferent",
    "GateArmingError",
    "LevainEfferentAnalyzer",
    "PendingEfferent",
    "arm_efferent_gate",
    "awaiting_confirmation",
    "disarm_efferent_gate",
    "pending_gate_report",
    "reject_pending",
]

COMMAND_DISPLAY_LIMIT = 240
"""How much of a proposed command the gate report shows before eliding.

Long enough that the operator sees the verb, the target and the dangerous flag (which is the
whole judgment); short enough that a here-doc payload cannot bury the decision in scrollback."""


class GateArmingError(RuntimeError):
    """The efferent gate could not be armed on this conversation.

    Raised only when a readback proves the runtime did not accept the wiring. A caller must treat
    this as fatal for a gated session — continuing would run an entity that BELIEVES it is
    governed and is not, which is strictly worse than an ungoverned one that says so."""


class LevainEfferentAnalyzer(SecurityAnalyzerBase):
    """Classifies each pending action STRUCTURALLY, by tool identity.

    Carries no configuration by design: the classification rule lives in the pure
    :mod:`levain.firing.gate` leaf where it can be exercised exhaustively without a conversation,
    and a per-session knob here would be the first step toward the per-domain policy curve that
    is explicitly ``spore-417`` / v2.x.
    """

    def security_risk(self, action: Any) -> SecurityRisk:
        """Map one ``ActionEvent`` onto the risk channel the confirmation gate reads.

        Never raises: an action whose fields cannot be read at all is unrecognized and therefore
        efferent (fail-closed). The SDK additionally coerces an analyzer exception to ``HIGH``,
        so both layers fail the same direction — but relying on that would be relying on someone
        else's error handling for our invariant.
        """
        return (
            SecurityRisk.HIGH
            if classify_event(action).is_efferent
            else SecurityRisk.LOW
        )


class ConfirmEfferent(ConfirmationPolicyBase):
    """Confirm anything not positively classified afferent/inert.

    Deliberately NOT the shipped ``ConfirmRisky(threshold=HIGH)``, which behaves identically
    today: that name asserts a danger judgement Levain never made, and a policy's name is what a
    future reader trusts when the code is three refactors away. This one also states the
    fail-closed rule where it can be read — ``UNKNOWN`` (our analyzer absent) and ``MEDIUM``
    (never emitted) both confirm, so the only value that runs free is the one we explicitly
    assign to perception.
    """

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        return risk != SecurityRisk.LOW


def classify_event(action_event: Any) -> Classification:
    """Classify an ``ActionEvent`` by delegating to the pure leaf. Never raises."""
    return classify_action(_safe_attr(action_event, "tool_name"), _action_fields(action_event))


def _safe_attr(obj: Any, name: str) -> Any:
    """``getattr`` that survives an attribute which RAISES, not merely one that is absent.

    ``getattr(obj, name, default)`` only swallows ``AttributeError`` — a property that raises
    anything else propagates straight through the default. That is a real hole under a
    "never raises" contract, and it is the kind that only shows up on the malformed input you
    most want the gate to survive."""
    try:
        return getattr(obj, name, None)
    except Exception:  # noqa: BLE001 — an unreadable attribute must gate, not crash the turn
        return None


def _action_fields(action_event: Any) -> dict[str, Any]:
    """The action's arguments as a plain mapping, fail-soft to ``{}``.

    ``{}`` is the SAFE degradation: the classifier reads only the file editor's ``command``, and
    an absent command classifies as a write. Unreadable fields therefore gate."""
    action = _safe_attr(action_event, "action")
    if action is None:
        return {}
    try:
        return dict(action.model_dump())
    except Exception:  # noqa: BLE001 — an unreadable action must gate, not crash the turn
        pass
    try:
        return dict(vars(action))
    except Exception:  # noqa: BLE001
        return {}


def _detail_for(tool_name: str, fields: dict[str, Any], action: Any) -> str:
    """The judgeable content of a proposed action.

    Both the terminal and the file editor call their primary field ``command``, but they mean
    different things by it — a whole shell string vs. an edit verb that needs its ``path`` to be
    meaningful. Render each as what it IS."""
    command = fields.get("command")
    path = fields.get("path")
    if isinstance(command, str) and isinstance(path, str) and path:
        return f"{command} {path}"
    if isinstance(command, str) and command:
        return _elide(command)
    if isinstance(command, str):
        return "(empty command)"
    kind = fields.get("kind") or getattr(action, "kind", "") or tool_name
    return str(kind)


def _elide(text: str) -> str:
    """One line, bounded. A multi-line command is collapsed so one proposal stays one entry."""
    flat = " ".join(text.split())
    if len(flat) <= COMMAND_DISPLAY_LIMIT:
        return flat
    return flat[: COMMAND_DISPLAY_LIMIT - 1] + "…"


def arm_efferent_gate(conversation: Any) -> None:
    """Arm the gate on ``conversation``, then PROVE it armed.

    Sets both halves and reads both back off the conversation state. Raises
    :class:`GateArmingError` if either did not take — see the module docstring for why the
    analyzer-present/policy-absent combination is the one that must never pass silently.
    """
    try:
        conversation.set_security_analyzer(LevainEfferentAnalyzer())
        conversation.set_confirmation_policy(ConfirmEfferent())
    except Exception as exc:  # noqa: BLE001 — arming failure is fatal for a gated session
        raise GateArmingError(f"the runtime refused the gate wiring ({exc})") from exc

    state = getattr(conversation, "state", None)
    analyzer = getattr(state, "security_analyzer", None)
    policy = getattr(state, "confirmation_policy", None)
    if not isinstance(policy, ConfirmEfferent):
        raise GateArmingError(
            "the confirmation policy did not take "
            f"(state carries {type(policy).__name__}) — refusing to run a session that would "
            "report itself gated while executing every efferent action."
        )
    if not isinstance(analyzer, LevainEfferentAnalyzer):
        raise GateArmingError(
            f"the security analyzer did not take (state carries {type(analyzer).__name__})."
        )


def disarm_efferent_gate(conversation: Any) -> None:
    """Declare the ungated posture explicitly rather than inheriting it.

    The SDK's default IS ``NeverConfirm``, so this is a no-op today — and that is exactly why it
    is written down. An ungated REPL that works only because an upstream default happens to
    match our intent would hang the day that default changes, with no line of ours to point at.
    Fail-soft: ungated is the SDK's own default, so a runtime that refuses the call is already
    in the state we wanted.
    """
    try:
        from openhands.sdk.security.confirmation_policy import NeverConfirm

        conversation.set_confirmation_policy(NeverConfirm())
        conversation.set_security_analyzer(None)
    except Exception:  # noqa: BLE001 — the default already is what we are asking for
        pass


def awaiting_confirmation(conversation: Any) -> bool | None:
    """Did the conversation halt at the gate? ``None`` when the status CANNOT BE READ.

    THREE-VALUED, and the third value is load-bearing (codex L3, HIGH). This previously
    swallowed a status-read failure into ``False``, which meant "I could not tell" and "it is
    not held" left this function as the same answer — so the caller's own three-valued handling
    could never fire and was dead code that read as a safeguard.

    Never raises; an unreadable status becomes ``None``, and deciding what to do about that
    belongs to the caller driving the turn, not here."""
    try:
        from openhands.sdk import ConversationExecutionStatus

        status = conversation.state.execution_status
    except Exception:  # noqa: BLE001 — undeterminable, which is NOT "no"
        return None
    return status == ConversationExecutionStatus.WAITING_FOR_CONFIRMATION


def held_action_ids(conversation: Any) -> set[str]:
    """The event ids of actions the gate is HOLDING — proposed, not executed.

    The runtime emits an ``ActionEvent`` BEFORE the confirmation decision and skips execution
    when confirmation is required, so an event log at a halt contains actions that never ran.
    Anything rendering "what this turn DID" must subtract these, or it reports a force-push as
    completed work on the same screen that says nothing was executed (codex L3). Never raises;
    an empty set degrades to "report everything", which is the pre-existing behaviour."""
    try:
        from openhands.sdk.conversation.state import ConversationState

        pending = ConversationState.get_unmatched_actions(conversation.state.events)
        return {str(i) for i in (_safe_attr(e, "id") for e in pending) if i is not None}
    except Exception:  # noqa: BLE001 — a display filter must never break a turn
        return set()


def pending_gate_report(conversation: Any) -> list[PendingEfferent]:
    """The actions the gate stopped, in the order the agent proposed them.

    Reports EVERY pending action, not only the efferent ones. A batch halts as a unit, so the
    afferent members of that batch are also un-executed and also waiting on the human — listing
    only the efferent ones would under-report what approving actually authorises.
    """
    try:
        from openhands.sdk.conversation.state import ConversationState

        pending = ConversationState.get_unmatched_actions(conversation.state.events)
    except Exception:  # noqa: BLE001 — a report must never break a turn
        return []

    report: list[PendingEfferent] = []
    for event in pending:
        try:
            fields = _action_fields(event)
            raw_name = _safe_attr(event, "tool_name")
            tool_name = str(raw_name or "<unnamed>")
            classification = classify_action(raw_name, fields)
            if classification.action_class is ActionClass.INERT:
                continue
            report.append(
                PendingEfferent(
                    tool_name=tool_name,
                    detail=_detail_for(tool_name, fields, _safe_attr(event, "action")),
                    reason=classification.reason,
                    recognized=classification.recognized,
                )
            )
        except Exception:  # noqa: BLE001 — one undescribable action must not blank the whole
            # report. Losing the other entries would hand the operator a SHORTER decision than
            # the one they are actually making, which is worse than one opaque line.
            report.append(
                PendingEfferent(
                    tool_name="<unreadable>",
                    detail="an action the gate is holding could not be rendered",
                    reason="unreadable action — approve only if you know what this entity was doing",
                    recognized=False,
                )
            )
    return report


def reject_pending(conversation: Any, reason: str) -> None:
    """Refuse the pending actions, telling the entity WHY.

    The reason reaches the model as a ``UserRejectObservation``, which is the difference between
    an entity that learns its action was declined and one that believes it succeeded. Never
    raises — a rejection that fails must not also take down the driver."""
    try:
        conversation.reject_pending_actions(reason)
    except Exception:  # noqa: BLE001
        pass
