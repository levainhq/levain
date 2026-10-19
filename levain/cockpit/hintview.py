"""levain.cockpit.hintview (Q16): the one place a decision surface asks for shell hints.

Rendering only. ``levain.consent.listing.entry_body`` calls :func:`hints_for` to put a ``hints`` list on the body both
the cockpit row and the phone page draw; nothing that decides a request (queue, policy, grants, adapters, sign,
answering, push_gate) imports this module or ``levain.consent.hints``. A hint informs; it never authorises.
"""
from __future__ import annotations

from typing import Any

from levain.consent.hints import CUT_HINT, UNREAD_HINT, shell_hints

__all__ = ["BASH_LIKE", "hints_for"]

BASH_LIKE = frozenset({"Bash", "exec_command", "shell", "local_shell", "shell_command"})
_KEYS = ("command", "cmd")


def _from_cut_display(display: str, tool: str) -> str:
    """The shown part of a cut display: the text after the tool name, un-quoted by the common JSON escapes. A hint
    reading aid only, so a crude read is enough."""
    body = display[len(tool) + 1:].rstrip("…")
    for key in _KEYS:
        marker = f'"{key}": "'
        at = body.find(marker)
        if at >= 0:
            text = body[at + len(marker):]
            return text.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    return ""


def hints_for(request: Any, tool_input: Any, cut: bool) -> list[str]:
    """Hint lines for a Bash-like request, ``[]`` for any other tool."""
    if request.tool_name not in BASH_LIKE:
        return []
    if cut:
        shown = _from_cut_display(request.display, request.tool_name)
        return [CUT_HINT, *shell_hints(shown, request.cwd)] if shown else [CUT_HINT]
    command = next((tool_input[k] for k in _KEYS if isinstance(tool_input, dict) and isinstance(tool_input.get(k), str)), None)
    if command is None:
        return [UNREAD_HINT]
    return shell_hints(command, request.cwd)
