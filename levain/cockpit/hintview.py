"""levain.cockpit.hintview (Q16): the one place a decision surface asks for shell hints.

Rendering only. ``levain.consent.listing.entry_body`` calls :func:`hints_for` to put a ``hints`` list on the body both
the cockpit row and the phone page draw (the phone page draws it for a request listed on its own and for the push an
open question's option leads to, which is the same body); nothing that decides a request (queue, policy, grants,
adapters, sign, answering, push_gate) imports this module or ``levain.consent.hints``. A hint informs; it never
authorises. :func:`visible` is the same layer's guard on agent-controlled text: invisible format characters are shown as
markers, so a bidi override cannot reorder what the human reads.
"""
from __future__ import annotations

import re
import shlex
import unicodedata
from typing import Any

from levain.authority import _consent_shell_name
from levain.consent.hints import CUT_HINT, NONE_HINT, UNREAD_HINT, shell_hints

__all__ = ["hints_for", "is_bash_like", "visible"]

_KEYS = ("command", "cmd")
_CUT_FIELD = re.compile(r'"(?:command|cmd)": "((?:[^"\\]|\\.)*)')
_UNESCAPE = re.compile(r'\\(["\\nt])')
_UNESCAPED = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


def is_bash_like(tool: str) -> bool:
    """A shell request by the authority's own judgement (``CONSENT_SHELL_TOOLS``, any case, bare or an MCP tool's name)."""
    return isinstance(tool, str) and _consent_shell_name(tool)


def visible(text: Any) -> Any:
    """``text`` with every Unicode format or control character (Cf, Cc except newline and tab, U+2028/2029, Zs spaces other than U+0020, U+3164, U+2800) shown as a
    visible ``<U+XXXX>`` marker. Not a string: returned as is."""
    if not isinstance(text, str):
        return text
    return "".join(
        f"⟨U+{ord(ch):04X}⟩" if _hidden(ch) else ch
        for ch in text)


def _hidden(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return ((cat in ("Cf", "Cc") and ch not in "\n\t") or ch in "\u2028\u2029\u3164\u2800"
            or (cat == "Zs" and ch != " "))


def _from_cut_display(display: str, tool: str) -> str:
    """The shown part of a cut display: the command's JSON string, up to its closing quote (or the cut), un-escaped in one
    pass. A hint reading aid only, so a crude read is enough."""
    m = _CUT_FIELD.search(display[len(tool) + 1:].rstrip("…"))
    return _UNESCAPE.sub(lambda g: _UNESCAPED[g.group(1)], m.group(1)) if m else ""


def hints_for(request: Any, tool_input: Any, cut: bool) -> list[str]:
    """Hint lines for a shell request (never empty: "no pattern matched" when nothing did), ``[]`` for any other tool."""
    if not is_bash_like(request.tool_name):
        return []
    if cut:
        shown = _from_cut_display(request.display, request.tool_name)
        got = shell_hints(shown, request.cwd) if shown else []
        return [CUT_HINT, *(got or ([NONE_HINT] if shown else []))]
    command = next((tool_input[k] for k in _KEYS if isinstance(tool_input, dict) and tool_input.get(k) is not None), None)
    if isinstance(command, list) and command and all(isinstance(x, str) for x in command):
        command = shlex.join(command)
    if not isinstance(command, str):
        return [UNREAD_HINT]
    return shell_hints(command, request.cwd) or [NONE_HINT]
