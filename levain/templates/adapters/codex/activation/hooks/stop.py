#!/usr/bin/env python3
"""Levain activation — Stop hook (Codex): the transcript's `reply` line (Q8).

Records the agent's final reply for the turn (`last_assistant_message`) in the session's
transcript log, a DISPLAY for the cockpit and never an authority. It does NOT touch the session
registry: the registry's `last_event` allows only SessionStart | UserPromptSubmit.

Prints NOTHING and exits 0 always: Codex's Stop output schema rejects `hookSpecificOutput.additionalContext`. FAIL-OPEN — the hook import is guarded and main()
swallows everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    sys.exit(0)


def main() -> int:
    try:
        payload = hook.read_stdin()
        if not hook.should_fire():
            return 0
        message = payload.get("last_assistant_message")
        if isinstance(message, str) and message:
            hook.session_log(payload.get("session_id"), "reply", message)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
