"""Pure, duck-typed extraction of an agent's reply text from OpenHands events.

ONE source of truth for the two consumers that must never diverge on the SDK's event
shapes: ``capture.render_turn`` (the persisted memory episode) and ``run._latest_agent_text``
(the on-screen REPL reply). Duck-typed — this module imports NO ``openhands`` — so
``levain.run`` keeps its SDK-free test tier and a single edit here moves both consumers in
lockstep (the DRY the apparatus flagged: display and capture drifting apart is a silent
memory-vs-screen mismatch).

Two SDK realities it encodes (verified against OpenHands 1.26.0, 2026-07-08):
  - a no-tool agent reply arrives as an ``ActionEvent`` whose ``.action`` is a
    ``FinishAction`` carrying ``.message`` — NOT a ``MessageEvent`` (:func:`finish_message`);
  - when a weak/open model returns an empty/reasoning-only response, the SDK injects a
    SYNTHETIC ``MessageEvent(source="user")`` corrective nudge
    (``agent/response_dispatch.py:_send_corrective_nudge``). It is shaped exactly like a
    human turn, so a naive ``source=="user"`` turn-boundary would capture the NUDGE as the
    human question and drop the real one (:func:`is_corrective_nudge`).
"""
from __future__ import annotations

# The discriminator of the built-in ``finish`` tool's action (a stable pydantic ``.kind``).
FINISH_ACTION_KIND = "FinishAction"

# A stable, SPAN-of-two-sentences fragment of the SDK's corrective-nudge text
# (agent/response_dispatch.py). Long enough that a real human is vanishingly unlikely to type
# it verbatim (so a genuine turn is never mis-excluded), yet not the full string (trivial
# rewording of the tail won't break the guard). A drift-guard test (test_firing_capture)
# asserts the installed SDK still emits it, turning a future SDK change from a silent
# regression into a loud test failure.
CORRECTIVE_NUDGE_MARKER = "did not include a function call or a message. Please use a tool"


def _message_text(msg) -> str | None:
    """Join a Message's TextContent parts into one stripped string, or ``None`` if empty."""
    if msg is None:
        return None
    text = " ".join(
        c.text for c in (getattr(msg, "content", None) or []) if getattr(c, "text", None)
    ).strip()
    return text or None


def message_event_text(event) -> str | None:
    """The text of a ``MessageEvent`` (``event.llm_message`` content), or ``None``."""
    return _message_text(getattr(event, "llm_message", None))


def finish_message(event) -> str | None:
    """The agent's answer when it responded via the built-in ``finish`` tool —
    ``event.action.message`` iff ``event.action`` is a ``FinishAction``. ``None`` for any
    other action (a real bash/file tool call) or a non-action event, so real tool actions
    are never mistaken for assistant text."""
    action = getattr(event, "action", None)
    if action is None or getattr(action, "kind", None) != FINISH_ACTION_KIND:
        return None
    message = getattr(action, "message", None)
    return message.strip() if isinstance(message, str) and message.strip() else None


def is_corrective_nudge(event) -> bool:
    """True iff ``event`` is the SDK's synthetic ``source="user"`` corrective nudge — which
    must NOT be treated as a genuine human turn (it would fabricate the wrong ``[user]`` line
    and drop the real question). Recognized by source + the stable text fragment."""
    if getattr(event, "source", None) != "user":
        return False
    text = message_event_text(event)
    return text is not None and CORRECTIVE_NUDGE_MARKER in text
