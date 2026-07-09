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

import json

# The discriminator of the built-in ``finish`` tool's action (a stable pydantic ``.kind``).
FINISH_ACTION_KIND = "FinishAction"

# The built-in SDK actions that are NOT executor/workspace tool calls: ``finish`` is the assistant's
# reply (surfaced by ``finish_message``), ``think`` is the model's private scratchpad — the SDK adds
# BOTH to every agent (``think`` is present even with ``tools=None``). Neither is workspace activity,
# so the REPL's tool-activity render skips them; without the ``think`` skip, ``--no-tools`` would
# render ``⚙ think: ThinkAction`` every turn, contradicting the "tools: none" banner.
_BUILTIN_ACTION_KINDS = frozenset({FINISH_ACTION_KIND, "ThinkAction"})

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


def tool_action_summary(event) -> tuple[str, str] | None:
    """``(tool_name, detail)`` for an agent ActionEvent that is a REAL tool call, else ``None``.

    Duck-typed (no ``openhands`` import), so the REPL's tool-activity render stays in the SDK-free
    test tier. Returns ``None`` for a non-action event, a message event, or a built-in control action
    (``finish`` — surfaced as the reply by :func:`finish_message`; ``think`` — the model's private
    scratchpad, on every agent even ``tools=None``); neither is workspace activity. ``detail`` is
    a compact ``"<command> <path>"`` for a file-editor action, else the action ``kind`` — enough for
    the operator to SEE what the entity DID to its workspace (a file op must never be invisible)."""
    tool_name = getattr(event, "tool_name", None)
    action = getattr(event, "action", None)
    if not tool_name or action is None:
        return None
    if getattr(action, "kind", None) in _BUILTIN_ACTION_KINDS:
        return None  # finish is the reply; think is the model's scratchpad — neither is workspace activity
    command = getattr(action, "command", None)
    path = getattr(action, "path", None)
    if command and path:
        return tool_name, f"{command} {path}"
    return tool_name, str(getattr(action, "kind", "") or "")


def humanize_finish_json(text: str) -> str:
    """spore-297: a weak open model (minimax-m3, verified live 2026-07-09) sometimes emits its tool
    calls as JSON TEXT instead of structured tool calls — e.g.::

        {"name": "think", "arguments": {"summary": "...", "thought": "..."}}
        {"name": "finish", "arguments": {"summary": "...", "message": "the real reply"}}

    — so the REPL (and the captured episode) would show raw JSON instead of the reply. If ``text`` is
    one-or-more CONCATENATED tool-call JSON objects, return the ``finish`` call's ``arguments.message``
    (the human reply), dropping ``think`` (the scratchpad). Otherwise return ``text`` UNCHANGED — a
    normal reply that merely contains a brace or a JSON snippet is never mangled: trailing prose after
    a JSON object, a non-dict, or a JSON object that is not a ``finish`` tool call all leave it as-is.
    Conservative by design — it only unwraps a clean, entirely-tool-call-JSON payload that carries a
    ``finish`` message, so it fixes the observed failure without ever eating a legitimate answer."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    decoder = json.JSONDecoder()
    objs: list[dict] = []
    idx, n = 0, len(stripped)
    while idx < n:
        while idx < n and stripped[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, idx = decoder.raw_decode(stripped, idx)
        except ValueError:
            return text  # not a clean, entirely-JSON tool-call payload → leave untouched
        if not isinstance(obj, dict):
            return text
        objs.append(obj)
    for obj in objs:
        if obj.get("name") == "finish":
            message = (obj.get("arguments") or {}).get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return text  # no finish message found → don't fabricate a reply from the scratchpad


def is_corrective_nudge(event) -> bool:
    """True iff ``event`` is the SDK's synthetic ``source="user"`` corrective nudge — which
    must NOT be treated as a genuine human turn (it would fabricate the wrong ``[user]`` line
    and drop the real question). Recognized by source + the stable text fragment."""
    if getattr(event, "source", None) != "user":
        return False
    text = message_event_text(event)
    return text is not None and CORRECTIVE_NUDGE_MARKER in text
