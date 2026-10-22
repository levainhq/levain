#!/usr/bin/env python3
"""Levain activation — PostToolUse hook (Claude Code): the transcript's `tool` line (Q8).

Claude Code also runs it for PostToolUseFailure (a tool that ran and failed fires only that event; measured on
2.1.301 in Q8_RUN), with the same tool_name / tool_input, so a failed tool has its line too.

Records each tool the agent ran as `<tool_name>` or `<tool_name>: <summary>`, the summary being a
shell tool's `command` (first line only), an apply_patch patch's first file header, or a file
tool's `file_path` (a notebook tool's `notebook_path`). `tool_response` is NEVER read: its
shape differs per agent and it can carry anything. A DISPLAY for the cockpit, never an authority.

First, whether or not anything else here fires: a consent request levain filed for this same call and
left to the agent's own dialog is recorded, unsigned, as run (`levain.consent.dialog`), and the call is filed in
the authority ledger, its stamp pending: the transcript's `permissionDecision` is not on disk yet at this event, so the
consent gate's next PreToolUse (or Stop) stamps it (`levain.consent.ledger`).

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


# An apply_patch can touch many files: every path is logged, one per line (the first after `apply_patch: `, as a one-file patch
# always was, so an older reader still sees the first), at most _PATCH_PATHS_MAX of them and about _PATCH_TEXT_BUDGET characters
# (the log cuts a line at _LOG_TEXT_MAX and a cut line names no whole path). The rest are counted, never silently dropped.
_PATCH_PATHS_MAX = 32
_PATCH_TEXT_BUDGET = 3500


def _patch_summary(paths) -> str:
    kept, used = [], 0
    for p in paths[:_PATCH_PATHS_MAX]:
        if kept and used + len(p) + 1 > _PATCH_TEXT_BUDGET:
            break
        kept.append(p)
        used += len(p) + 1
    rest = len(paths) - len(kept)
    return "\n".join(kept) + (f"\n(+{rest} more)" if rest else "")


def _summary(tool_name, tool_input) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if tool_name in _SHELL_TOOLS and isinstance(command, str) and command.strip():
        return command.strip().splitlines()[0]
    if tool_name == "apply_patch" and isinstance(command, str):
        paths = [p for p in _PATCH_HEADER.findall(command) if p]
        if paths:
            return _patch_summary(paths)
    for key in ("file_path", "notebook_path"):
        path = tool_input.get(key)
        if isinstance(path, str) and path:
            return path
    return None


def main() -> int:
    try:
        payload = hook.read_stdin()
        if hasattr(hook, "consent_observe"):  # an older helper beside a newer script: no record, the hook runs
            hook.consent_observe("claude_code", "PostToolUse", payload)
        if hasattr(hook, "consent_ledger"):
            hook.consent_ledger("claude_code", payload, record=True)
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
