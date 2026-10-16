#!/usr/bin/env python3
"""Levain activation — PostToolUse hook (Codex): the transcript's `tool` line (Q8).

Records each tool the agent ran as `<tool_name>` or `<tool_name>: <summary>`, the summary being a
Bash `command` (first line only) or a file tool's `file_path`. `tool_response` is NEVER read: its
shape differs per agent and it can carry anything. A DISPLAY for the cockpit, never an authority.

Prints NOTHING and exits 0 always. FAIL-OPEN — the hook import is guarded and main() swallows
everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    sys.exit(0)


def _summary(tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if isinstance(command, str) and command.strip():
        return command.strip().splitlines()[0]
    path = tool_input.get("file_path")
    if isinstance(path, str) and path:
        return path
    return None


def main() -> int:
    try:
        payload = hook.read_stdin()
        if not hook.should_fire():
            return 0
        name = payload.get("tool_name")
        if not isinstance(name, str) or not name:
            return 0
        summary = _summary(payload.get("tool_input"))
        hook.session_log(payload.get("session_id"), "tool", f"{name}: {summary}" if summary else name)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
