#!/usr/bin/env python3
# Levain's consent hook for Claude Code, wired by `levain init` and `levain update` for two events. PermissionRequest:
# it reports a decision only when levain verified a signed answer or a standing consent grant; otherwise it prints
# nothing and Claude Code's own permission dialog stands. PreToolUse (the gate): a call that edits agent config or
# hooks is held for a signed answer and, unanswered, denied as UNDECIDED with the session stopped; its entry blocks
# on failure. It takes no arguments: the queue is levain's default one and the holds are the adapter's settings.
import os
import sys

if not (sys.flags.safe_path and sys.flags.ignore_environment):
    try:
        os.execv(sys.executable, [sys.executable, "-EP", os.path.abspath(__file__), *sys.argv[1:]])
    except BaseException:
        pass
    sys.exit(0)

try:
    from levain.consent.adapters.claude_code import main
except BaseException:
    sys.exit(0)
sys.exit(main([]))
