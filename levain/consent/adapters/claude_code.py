"""Claude Code's ``PermissionRequest`` hook on the consent queue (Q2, first slice).

Claude Code runs this hook when it is about to show its permission dialog, and shows the dialog AT ONCE while the
hook runs (measured, Q0 item 2); a later ``allow`` from the hook dismisses it and the tool runs. So the hook:

1. reads the event (``tool_name``, ``tool_input``, ``session_id``, ``agent_id`` for a subagent, ``cwd``) and files
   one request on the queue, with Claude Code's ``tool_use_id`` as its event id when the event carries one (a
   ``PermissionRequest`` measured in Q0 does not; the core then mints a random one);
2. asks the core's policy seam (:func:`levain.consent.decide_by_policy`, which decides nothing today), except for
   a request that targets agent config or hooks (:func:`targets_agent_config`), which is never eligible for one;
3. when away mode is on (an adapter setting, default off, default hold 0 s) waits up to the hold for a signed
   answer the core verifies, and withdraws the request if none came;
4. prints ``allow`` only for a verified allow and ``deny`` only for a verified deny. Anything else, including any
   exception, prints nothing and exits 0: the native dialog stands and the human answers it.

Nothing read from stdin is ever a decision: the event only says what is being asked. Bash commands are never
parsed (ruling 1): a Bash request is just a Bash request.

Settings live in ``<levain home>/consent/claude_code.json`` (:data:`SETTINGS_NAME`)::

    {"cockpit_data_root": "/path/with/cockpit.db", "away_mode": false, "away_hold_seconds": 0}

Missing or unreadable settings mean away mode off and no authority (so no answer can verify). :func:`install`
writes the hook script (:data:`HOOK_TEMPLATE`) and the ``PermissionRequest`` entry into ONE workspace's project
settings; wiring into user settings is a later slice.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from levain.authority import anchor_path
from levain.consent.queue import ConsentQueue, Decision, authority_source, decide_by_policy, default_queue_dir
from levain.consent.record import ExternalGateRequest

AGENT = "claude_code"
EVENT = "PermissionRequest"
SETTINGS_NAME = "claude_code.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
# A request is filed for at least this long even when nobody is waiting on it, so the policy seam has a live
# request to look at; it is withdrawn as soon as the hook stops waiting.
MIN_FILE_HOLD = timedelta(seconds=5)
# The hook never waits longer than the hold it was installed with: Claude Code's ``timeout`` for the hook is
# that hold plus this margin, so Claude Code never cancels the hook mid-wait (a cancelled hook is no decision).
TIMEOUT_MARGIN = 30
DEFAULT_MAX_HOLD = 900.0
# A request on one of these tools whose target path is agent config or a hook is tagged agent_config.
FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# tier an agent_config request is filed at: an allow then needs a key class covering T3 (fail toward the human).
AGENT_CONFIG_TIER = "T3"
DEFAULT_TIER = "T2"


class PolicyNotEligible(AssertionError):
    """The policy seam was reached for a request that may never have a policy answer (ruling 3b item 1)."""


@dataclass(frozen=True)
class AdapterSettings:
    away_mode: bool = False
    away_hold_seconds: float = 0.0
    cockpit_data_root: str | None = None


def load_settings(queue_dir: Path) -> AdapterSettings:
    """The adapter's settings from ``queue_dir/claude_code.json``; defaults (away off, no authority) when the
    file is missing or any field is malformed."""
    try:
        data = json.loads((queue_dir / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AdapterSettings()
    if not isinstance(data, dict):
        return AdapterSettings()
    away = data.get("away_mode") is True
    hold = data.get("away_hold_seconds", 0)
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not math.isfinite(hold) or hold < 0:
        hold = 0.0
    root = data.get("cockpit_data_root")
    return AdapterSettings(away_mode=away, away_hold_seconds=float(hold),
                           cockpit_data_root=root if isinstance(root, str) and root else None)


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

def _is_config_path(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    for i, part in enumerate(parts[:-1]):
        rest = parts[i + 1:]
        if part == ".claude":
            if rest[0] == "hooks" or (len(rest) == 1 and rest[0].startswith("settings") and rest[0].endswith(".json")):
                return True
        if part == ".codex" and len(rest) == 1 and rest[0] in ("hooks.json", "config.toml"):
            return True
    return False


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path | None = None) -> bool:
    """True when an Edit/Write/MultiEdit/NotebookEdit request targets an agent's config or hooks: a
    ``.claude/settings*.json``, anything under a ``.claude/hooks/``, a ``.codex/hooks.json`` or ``.codex/config.toml``,
    in the workspace or the home directory. The path is checked as written (``~`` expanded, joined to ``cwd``,
    normalised) and as resolved through symlinks; either match tags it. A file tool whose target cannot be read
    is tagged too. Other tools are never tagged: Bash is not parsed (ruling 1)."""
    if tool_name not in FILE_TOOLS:
        return False
    target = tool_input.get("file_path", tool_input.get("notebook_path")) if isinstance(tool_input, Mapping) else None
    if not isinstance(target, str) or not target:
        return True
    home = home if home is not None else Path.home()
    raw = Path(str(home) + target[1:]) if target == "~" or target.startswith("~/") else Path(target)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    written = Path(os.path.normpath(raw))
    try:
        resolved = Path(os.path.realpath(raw))
    except (OSError, ValueError):
        return True
    return _is_config_path(written) or _is_config_path(resolved)


# --- the decision ------------------------------------------------------------------------------------------

def _verified(decision: Any, request: ExternalGateRequest) -> Decision | None:
    """``decision`` only if it is the core's Decision for THIS request with a known verdict."""
    if isinstance(decision, Decision) and decision.request_id == request.id and decision.decision in ("allow", "deny"):
        return decision
    return None


def hook_output(decision: Decision) -> dict[str, Any]:
    """What Claude Code reads from a ``PermissionRequest`` hook for a verified decision."""
    verdict: dict[str, Any] = {"behavior": decision.decision}
    if decision.decision == "deny":
        verdict["message"] = f"Denied by a signed levain answer (key {decision.signer_fp})."
    return {"hookSpecificOutput": {"hookEventName": EVENT, "decision": verdict}}


def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, max_hold: float = DEFAULT_MAX_HOLD,
           policy: Callable[[ExternalGateRequest], Decision | None] = decide_by_policy,
           home: Path | None = None) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision. Raises on anything unexpected; the
    caller (:func:`main`) turns that into no decision."""
    if not isinstance(event, Mapping) or event.get("hook_event_name") != EVENT:
        return None
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = event.get("session_id")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not cwd \
            or not isinstance(session, str) or not session:
        return None
    if tool_input is None:
        tool_input = {}  # an MCP tool with no arguments arrives without tool_input (Q0 item 4)
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        session = f"{session}#agent:{agent_id}"
    tool_use_id = event.get("tool_use_id")
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home)
    hold = min(float(settings.away_hold_seconds), max(0.0, float(max_hold))) if settings.away_mode else 0.0
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER,
                         event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None)
    got: Decision | None = None
    if not agent_config:
        got = _verified(policy(request), request)
    if got is None and hold > 0:
        got = _verified(queue.await_decision(request.id, hold), request)
    if got is None and not queue.withdraw(request.id, note="the hook stopped waiting; the native dialog stands"):
        # Not pending any more: an answer may have landed between the wait and the withdraw.
        got = _verified(queue.decision(request.id), request)
    return hook_output(got) if got is not None else None


def guarded_policy(request: ExternalGateRequest,
                   policy: Callable[[ExternalGateRequest], Decision | None] | None = None) -> Decision | None:
    """The policy seam (default :func:`levain.consent.decide_by_policy`) with ruling 3b item 1 asserted at it:
    a request filed as agent_config (a file tool at :data:`AGENT_CONFIG_TIER`) never reaches a policy."""
    if request.tier == AGENT_CONFIG_TIER and request.tool_name in FILE_TOOLS:
        raise PolicyNotEligible(f"request {request.id} targets agent config: never eligible for a policy answer")
    return (policy or decide_by_policy)(request)


def _queue_for(queue_dir: Path, settings: AdapterSettings) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        def source():
            raise OSError("no cockpit data root is set: no answer can verify")
    return ConsentQueue(queue_dir, authority=source)


def main(argv: list[str] | None = None, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """The hook entry point. Always exits 0; prints a decision only when the core verified one."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        parser = argparse.ArgumentParser(prog="levain-consent-claude-code")
        parser.add_argument("--queue-dir", default=None)
        parser.add_argument("--max-hold", type=float, default=DEFAULT_MAX_HOLD)
        args = parser.parse_args(argv)
        queue_dir = Path(args.queue_dir) if args.queue_dir else default_queue_dir()
        settings = load_settings(queue_dir)
        event = json.loads(stdin.read())
        out = decide(event, _queue_for(queue_dir, settings), settings, max_hold=args.max_hold,
                     policy=guarded_policy)
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow
        try:
            print(f"levain consent hook: no decision ({type(exc).__name__}: {exc})"[:500], file=sys.stderr)
        except Exception:
            pass
        return 0
    if out is not None:
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
    return 0


# --- install (one workspace's project settings) ------------------------------------------------------------

HOOK_TEMPLATE = '''#!{python}
# Installed by levain (levain.consent.adapters.claude_code.install). Claude Code's PermissionRequest hook:
# it reports a decision only when levain verified a signed answer; otherwise it prints nothing and the native
# permission dialog stands. Any failure here, including levain missing, is no decision.
import sys

try:
    from levain.consent.adapters.claude_code import main
except BaseException:
    sys.exit(0)
sys.exit(main({argv}))
'''


def install(workspace: Path | str, *, python: str | None = None, queue_dir: Path | str | None = None,
            max_hold: float = DEFAULT_MAX_HOLD) -> tuple[Path, Path]:
    """Install the hook into ``workspace``: ``.claude/hooks/levain_consent.py`` and a ``PermissionRequest``
    entry (matcher ``*``, ``timeout`` = ``max_hold`` + :data:`TIMEOUT_MARGIN`) in ``.claude/settings.json``, other
    settings kept and any earlier levain entry replaced. Returns (script, settings)."""
    if not (isinstance(max_hold, (int, float)) and math.isfinite(max_hold) and max_hold >= 0):
        raise ValueError("max_hold must be a finite number of seconds >= 0")
    ws = Path(workspace).resolve()
    claude_dir = ws / ".claude"
    hooks_dir = claude_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    python = python or sys.executable
    argv = ["--max-hold", repr(float(max_hold))]
    if queue_dir is not None:
        argv += ["--queue-dir", str(Path(queue_dir).resolve())]
    script = hooks_dir / HOOK_SCRIPT_NAME
    script.write_text(HOOK_TEMPLATE.format(python=python, argv=repr(argv)), encoding="utf-8")
    script.chmod(0o755)
    settings_path = claude_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError(f"{settings_path} is not a JSON object")
    hooks = settings.setdefault("hooks", {})
    command = f"{shlex.quote(python)} {shlex.quote(str(script))}"
    entries = [e for e in hooks.get(EVENT, []) if not any(
        isinstance(h, dict) and HOOK_SCRIPT_NAME in str(h.get("command", "")) for h in e.get("hooks", []))]
    entries.append({"matcher": "*", "hooks": [{"type": "command", "command": command,
                                               "timeout": int(math.ceil(max_hold)) + TIMEOUT_MARGIN}]})
    hooks[EVENT] = entries
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return script, settings_path


if __name__ == "__main__":
    sys.exit(main())
