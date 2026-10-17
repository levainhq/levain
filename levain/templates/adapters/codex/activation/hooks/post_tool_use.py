#!/usr/bin/env python3
"""Levain activation — PostToolUse hook (Codex): the transcript's `tool` line (Q8).

Records each tool the agent ran as `<tool_name>` or `<tool_name>: <summary>`, the summary being a
shell tool's `command` (first line only), an apply_patch patch's first file header, or a file
tool's `file_path` (a notebook tool's `notebook_path`). `tool_response` is NEVER read: its
shape differs per agent and it can carry anything. A DISPLAY for the cockpit, never an authority.

First, whether or not anything else here fires: a consent request levain filed for this same call and
left to the agent's own dialog is recorded, unsigned, as run (`levain.consent.dialog`).

Prints NOTHING and exits 0 always. FAIL-OPEN — the hook import is guarded and main() swallows
everything.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    sys.exit(0)


# The tools whose `command` is a shell line. Anything else carrying a `command` (Codex apply_patch puts
# the whole patch there) is not summarised by it.
_SHELL_TOOLS = frozenset({"Bash", "shell", "exec_command", "shell_command", "local_shell"})
_PATCH_HEADER = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)


def _summary(tool_name, tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if tool_name in _SHELL_TOOLS and isinstance(command, str) and command.strip():
        return command.strip().splitlines()[0]
    if tool_name == "apply_patch" and isinstance(command, str):
        m = _PATCH_HEADER.search(command)
        if m and m.group(1):
            return m.group(1)
    for key in ("file_path", "notebook_path"):
        path = tool_input.get(key)
        if isinstance(path, str) and path:
            return path
    return None


def main() -> int:
    try:
        payload = hook.read_stdin()
        if hasattr(hook, "consent_observe"):  # an older helper beside a newer script: no record, the hook runs
            hook.consent_observe("codex", "PostToolUse", payload)
        if not hook.should_fire():
            return 0
        name = payload.get("tool_name")
        if not isinstance(name, str) or not name:
            return 0
        summary = _summary(name, payload.get("tool_input"))
        hook.session_log(payload.get("session_id"), "tool", f"{name}: {summary}" if summary else name)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
