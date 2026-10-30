#!/usr/bin/env python3
"""Levain activation — Stop hook (Claude Code): the transcript's `reply` line (Q8).

Records the agent's final reply for the turn (`last_assistant_message`) in the session's
transcript log, a DISPLAY for the cockpit and never an authority. It does NOT touch the session
registry: the registry's `last_event` allows only SessionStart | UserPromptSubmit.

First, whether or not anything else here fires: the consent requests levain left to the agent's own
dialog in this session that did not run are recorded, unsigned, as not run (`levain.consent.dialog`), and the
session's pending authority-ledger rows are stamped from the agent's own record (`levain.consent.ledger`).

Last, the NEEDS YOU band's verifier (`levain.band_view.hook_stop`): a reply that dropped a band item shown to this
seat is blocked ONCE with "reprint the band" (the harness's `stop_hook_active` and a per-view claim bound it to one
retry; it never loops). That is the only output. Otherwise prints NOTHING. Exits 0 always. FAIL-OPEN — the hook
import is guarded and main() swallows everything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _levain_hook as hook
except Exception:
    sys.exit(0)


def main() -> int:
    payload = {}
    try:
        payload = hook.read_stdin()
        if hasattr(hook, "consent_observe"):  # an older helper beside a newer script: no record, the hook runs
            hook.consent_observe("claude_code", "Stop", payload)
        if hasattr(hook, "consent_ledger"):
            hook.consent_ledger("claude_code", payload, fill=True)
        if not hook.should_fire():
            return 0
        message = payload.get("last_assistant_message")
        if isinstance(message, str) and message:
            kind = "reply"
            try:
                from levain.band_view import turn_log_kind
                kind = turn_log_kind(payload, "reply")      # "wake_reply" for a coordinator wake
            except Exception:
                pass
            hook.session_log(payload.get("session_id"), kind, message)
    except Exception:
        pass
    try:
        from levain.band_view import hook_stop
        reason = hook_stop(payload)
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")
            sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
