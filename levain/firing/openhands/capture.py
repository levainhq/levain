"""The Stop→capture half of the OpenHands firing-adapter.

OpenHands' native ``stop`` hook is the SDK's turn-end signal, but it is the WRONG surface to
carry a capture, for two disk-verified reasons: its ``HookEvent`` payload is content-less
(session_id + working_dir + an optional reason — never the transcript), and it fires only a
shell ``command`` whose stdout/stderr the SDK discards; worse, the transcript is on disk only
when the adopter sets ``persistence_dir`` (otherwise OpenHands uses an in-memory store).

So capture rides an IN-PROCESS run-wrapper instead. ``conversation.run()`` RETURNING is the
reliable turn boundary (verified 2026-06-23: the callback event stream carries no turn-end
sentinel event — SystemPrompt → user → assistant, then run() returns FINISHED), and the turn's
events live in ``conversation.state.events`` in-process — full content, no persistence-format
coupling, Python → Python straight into ``firing.capture``.

Afferent-safe substrate-write (the constitution): it appends the RAW turn (the user message +
the agent's response) as an episode. It never summarizes (summarizing is a light metabolize) and
never consolidates — ``firing.capture`` writes append-only, fail-soft-loud.

Wiring (alongside the per-turn condenser + the session_start suffix)::

    from levain.firing.openhands import vagus_run, vagus_agent_context, VagusCondenser
    agent = Agent(llm=llm, tools=[...],
                  agent_context=vagus_agent_context(firing_kind="anneal"),
                  condenser=VagusCondenser.build(inner=..., firing_kind="anneal"))
    conv = Conversation(agent, workspace=wd)
    conv.send_message("...")
    vagus_run(conv, firing_kind="anneal")   # run + capture the completed turn
"""
from __future__ import annotations

from openhands.sdk import MessageEvent

from levain.firing.contract import CaptureRequest, FiringContract, build_firing

# Roles whose text forms the raw captured turn. We deliberately EXCLUDE ``system`` (the vagus
# firing inject is a system-role MessageEvent — capturing our own injected recall would be a
# memory-eats-its-own-tail loop) and any event sent by ``vagus`` (belt-and-suspenders).
# The turn boundary keys on event SOURCE, not message role: OpenHands emits synthetic
# environment messages with role="user" (e.g. denied Stop-hook feedback), so role="user" would
# mis-key the boundary onto hook feedback. ``source == "user"`` is the genuine human turn; a real
# agent response is ``source == "agent"``. Everything else (environment / system / the vagus
# inject, which is source="environment") is excluded from both the boundary and the render.
_GENUINE_SOURCES = ("user", "agent")

# The idempotency marker lives in the persisted ``conversation.state.agent_state`` dict — NOT an
# arbitrary attribute on the conversation. fork() deep-copies agent_state (so a fork inherits the
# marker and won't re-capture the boundary turn) but does NOT carry arbitrary attrs.
_CAPTURED_KEY = "vagus_last_captured_turn"


def _turn_marker(events) -> str | None:
    """The identity of the latest turn — the id of its starting (most-recent ``source == 'user'``)
    message. Two ``vagus_run`` calls with no intervening user turn see the same marker, so the
    second is a no-op (the duplicate-capture guard). ``None`` when there is no genuine user turn."""
    for e in reversed(list(events)):
        if isinstance(e, MessageEvent) and getattr(e, "source", None) == "user":
            return getattr(e, "id", None)
    return None


def render_turn(events) -> str | None:
    """Render the latest completed turn (the last user message + the agent's response) as a RAW
    factual string, or ``None`` if there is nothing capturable (no user turn, or no response yet).
    Never summarizes — the captured episode is the raw exchange, not a metabolized finding.

    Scope: the user + assistant TEXT only. Tool actions/observations, images, and other non-text
    turn material are intentionally NOT rendered — "raw turn" here means the human↔agent exchange,
    not a full event-by-event transcript (a deliberate signal/noise choice for episodic memory).
    """
    evs = list(events)
    # Find the last GENUINE user message (source == "user") — the start of the latest turn.
    last_user = None
    for i in range(len(evs) - 1, -1, -1):
        e = evs[i]
        if isinstance(e, MessageEvent) and getattr(e, "source", None) == "user":
            last_user = i
            break
    if last_user is None:
        return None

    lines: list[str] = []
    for e in evs[last_user:]:
        if not isinstance(e, MessageEvent) or getattr(e, "source", None) not in _GENUINE_SOURCES:
            continue  # excludes environment/system/vagus-injected synthetic messages
        msg = e.llm_message
        role = getattr(msg, "role", None)
        text = " ".join(
            c.text for c in (getattr(msg, "content", None) or []) if getattr(c, "text", None)
        ).strip()
        if text:
            lines.append(f"[{role}] {text}")

    # A capturable turn needs the agent to have RESPONDED — a lone user message (no assistant
    # reply yet) is mid-turn, not a completed turn. Capturing it would write a noise episode.
    if not any(line.startswith("[assistant]") for line in lines):
        return None
    return "\n".join(lines)


def _get_marker(conversation) -> str | None:
    try:
        return (conversation.state.agent_state or {}).get(_CAPTURED_KEY)
    except Exception:  # noqa: BLE001 — best-effort idempotency, never break the run on it
        return None


def _set_marker(conversation, marker: str | None) -> None:
    # Reassign the dict (not in-place mutation) so OpenHands autosave/fork persists + carries it.
    try:
        state = conversation.state
        state.agent_state = {**(state.agent_state or {}), _CAPTURED_KEY: marker}
    except Exception:  # noqa: BLE001 — degrade to no-dedup rather than crash the run
        pass


def vagus_run(
    conversation,
    firing: FiringContract | None = None,
    *,
    firing_kind: str = "anneal",
    session_id: str | None = None,
) -> None:
    """Run the conversation to turn-completion, then CAPTURE the completed turn.

    ``firing`` (an explicit handle — pass the SAME one used for the condenser for coherence) or
    ``firing_kind`` selects the firing. The default kind is ``"anneal"`` — capture is a WRITE,
    so the default must PERSIST (unlike the condenser's ``stub`` default, a stub capture would
    no-op into memory and silently vanish). ``session_id`` groups the run's episodes (defaults
    to the conversation id).
    """
    f = firing if firing is not None else build_firing(firing_kind)
    conversation.run()
    events = conversation.state.events

    # Idempotency: structurally refuse to double-write a turn already captured (a memory-substrate
    # write — ``structural_invariants_beat_discipline``, not a once-per-turn contract handed to the
    # caller). Two ``vagus_run`` calls with no new user turn share a marker → the second no-ops.
    marker = _turn_marker(events)
    if marker is not None and marker == _get_marker(conversation):
        return

    content = render_turn(events)
    if not content:
        return
    written = f.capture(
        CaptureRequest(
            content=content,
            episode_type="observation",
            source="vagus",
            session_id=session_id or str(getattr(conversation, "id", "")) or None,
        )
    )
    # Mark the turn captured ONLY on a confirmed write. capture() fail-soft-swallows write errors
    # (logs, never raises) and returns False — so a transient DB/audit failure leaves the marker
    # unset and the turn RETRYABLE on the next vagus_run, instead of being silently lost.
    if written:
        _set_marker(conversation, marker)
