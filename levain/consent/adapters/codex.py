"""Codex's ``PermissionRequest`` hook on the consent queue (Q3).

Codex runs this hook when it is about to ask for approval, and shows NO dialog while the hook runs (measured, Q0
item 6 and Q3): the TUI says "Running hook" and the approval dialog appears only after the hook returns. So a hold
delays the dialog, and away mode stays opt-in with a default hold of 0 s. The hook:

1. reads the event (``tool_name``, ``tool_input``, ``session_id``, ``cwd``, ``permission_mode``) and files one
   request on the queue. A Codex ``PermissionRequest`` carries no per-event id (only ``turn_id``, which one turn
   shares across its requests), so the core mints a random one;
2. asks the policy seam (:func:`levain.consent.policy.guarded_policy`, with the tool input it filed and posture =
   the event's ``permission_mode``), except for a request that targets agent config or hooks
   (:func:`targets_agent_config`), which is never eligible. A standing consent grant can answer there; its targets
   are read by :func:`policy_targets`, registered with the policy when this module is imported;
3. when away mode is on waits up to the hold for a signed answer the core verifies, and withdraws the request if
   none came;
4. prints ``allow`` only for a verified allow and ``deny`` only for a verified deny. Anything else, including any
   exception, prints nothing and exits 0: Codex's own approval dialog appears and the human answers it.

Measured on codex-cli 0.162.1, the approvals that reach this event include a shell escalation (``tool_name``
``Bash``, the command in ``tool_input.command``, Codex's model-written reason in ``tool_input.description``), input
sent to a terminal launched outside the sandbox (``tool_name`` ``write_stdin``, the text in ``tool_input.chars``), an
``apply_patch`` outside what the sandbox may write, the workspace's ``.codex/`` included (``tool_name``
``apply_patch``, the patch text in ``tool_input.command``), and an MCP tool call (``mcp__<server>__<tool>``;
1017-shellnames). A shell command or its input is never parsed (ruling 1). A patch's own file headers are read to
tag agent config (a patch naming no file is tagged) and to match a grant's workspace (an unreadable patch matches no
grant).

Codex fires project hooks only in an INTERACTIVE session whose process cwd is the project: ``codex exec`` never asks
(its approval policy is ``never``) and ``codex -C <dir>`` does not load ``<dir>/.codex/hooks.json`` (Q0 item 6).

Settings live in ``<levain home>/consent/codex.json`` (:data:`SETTINGS_NAME`), the Claude Code adapter's shape::

    {"cockpit_data_root": "/absolute/cockpit/data/root", "away_mode": false, "away_hold_seconds": 0}

:func:`install` writes the hook into ONE workspace's ``.codex/``. Levain never writes Codex's trust: Codex shows
"Hooks need review" at the next start and the user trusts the hook there; :func:`trust_status` reports what Codex
itself says about it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from levain.authority import anchor_path
from levain.consent import policy as consent_policy
# _absolute_root is re-exported: the confinement floor reads it as `adapter._absolute_root` for either adapter
from levain.consent.adapters.claude_code import (  # noqa: F401
    CLEANUP_RESERVE, DEFAULT_MAX_HOLD, DEFAULT_TIER, ISOLATE_PRELUDE, MIN_FILE_HOLD, AdapterSettings,
    _absolute_root, _atomic_write, _is_ours, _merge_handler, _refuse_symlink, _stat_key, _verified, hook_command,
    read_settings_file,
    hook_timeout, nothing_can_decide,
    protected_paths,
)
from levain.consent.adapters.claude_code import targets_agent_config as _claude_targets_agent_config
from levain.consent.policy import AGENT_CONFIG_TIER
from levain.consent.queue import ConsentQueue, Decision, authority_source, default_queue_dir
from levain.consent.record import ExternalGateRequest

AGENT = "codex"
EVENT = "PermissionRequest"
SETTINGS_NAME = "codex.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
PATCH_TOOL = "apply_patch"
# The file headers of Codex's patch format; their paths are the files a patch touches. Indentation is allowed, in
# case Codex's parser trims it: any other line starting ``***`` that is not a patch marker makes the patch unreadable.
_PATCH_HEADER = re.compile(r"\s*\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*")
_PATCH_MARKERS = frozenset({"*** Begin Patch", "*** End Patch", "*** End of File"})
CODEX_DIR_NAME = ".codex"
CODEX_CONFIG_NAMES = ("hooks.json", "config.toml")
# The hooks.json key Codex files a PermissionRequest hook's trust under (``<source>:<event>:<group>:<handler>``).
TRUST_EVENT_KEY = "permission_request"
TRUST_TIMEOUT = 30.0


def load_settings(queue_dir: Path) -> AdapterSettings:
    """The adapter's settings from ``queue_dir/codex.json``; defaults (away off, no authority) when the file is
    missing or any field is malformed."""
    return read_settings_file(queue_dir / SETTINGS_NAME)


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """The CODEX_HOME Codex itself uses: ``$CODEX_HOME`` when set, else ``~/.codex``."""
    env = os.environ if environ is None else environ
    return Path(env.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

def patch_targets(patch: Any) -> list[str] | None:
    """The file paths a Codex ``apply_patch`` names in its headers (``*** Add File:``, ``Update File:``,
    ``Delete File:``, ``Move to:``), or ``None`` when ``patch`` is not a string, names no file, or has a line starting
    ``***`` (indented or not) that is neither a header nor a patch marker: what it names cannot be told."""
    if not isinstance(patch, str):
        return None
    found: list[str] = []
    for line in patch.splitlines():
        m = _PATCH_HEADER.fullmatch(line)
        if m:
            found.append(m.group(1))
        elif line.strip().startswith("***") and line.strip() not in _PATCH_MARKERS:
            return None
    return found or None


def policy_targets(tool_name: str, tool_input: Any) -> list[Any] | None:
    """The paths a Codex request names, for the consent policy's grant criteria: any tool's path fields
    (:func:`levain.consent.policy.grant_targets`), plus an ``apply_patch``'s headers (:func:`patch_targets` of
    ``tool_input.command``; ``None``, which matches nothing, when unreadable). A path field beside a patch is
    checked too, so an inside header never covers an outside field."""
    fields = consent_policy.grant_targets(tool_name, tool_input)
    if tool_name != PATCH_TOOL:
        return fields
    headers = patch_targets(tool_input.get("command") if isinstance(tool_input, Mapping) else None)
    return None if headers is None else [*headers, *fields]


consent_policy.register_targets(AGENT, policy_targets)


def _in_codex_config(path: str, cwd: str, home: Path, codex_dir: Path) -> bool:
    raw = Path(str(home) + path[1:]) if path == "~" or path.startswith("~/") else Path(path)
    if not raw.is_absolute():
        raw = Path(cwd) / raw
    roots = {os.path.normpath(codex_dir).lower(), os.path.realpath(codex_dir).lower()}
    for p in (os.path.normpath(raw), os.path.realpath(raw)):
        low = p.lower()
        if CODEX_DIR_NAME in Path(low).parts[:-1]:
            return True  # anything under any .codex/ directory (the workspace's or ~/.codex)
        if any(low == r or low.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            return True  # anything under CODEX_HOME, whatever its name
    return False


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path | None = None,
                         codex_dir: Path | None = None, protected: tuple[Path, ...] = ()) -> bool:
    """True when a Codex ``apply_patch`` or an MCP tool targets an agent's config or hooks: every path the Claude
    Code adapter tags (:func:`levain.consent.adapters.claude_code.targets_agent_config`: Claude Code's config,
    ``.codex/hooks.json`` and ``config.toml``, the levain package, the adapter's own queue and authority), plus
    anything under a ``.codex/`` directory or under CODEX_HOME (``codex_dir``). An MCP tool's targets are its
    :data:`MCP_PATH_FIELDS` and every other string in its input (:func:`levain.consent.policy.mcp_config_candidates`). A patch whose headers name no file
    is tagged, as is a non-string MCP path. Shell requests (:data:`levain.authority.CONSENT_SHELL_TOOLS`) and every
    other tool are never tagged: a command is not parsed (ruling 1)."""
    home = home if home is not None else Path.home()
    codex_dir = codex_dir if codex_dir is not None else codex_home()
    if tool_name == PATCH_TOOL:
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        targets: list[Any] | None = patch_targets(command)
        if targets is None:
            return True
    elif tool_name.startswith("mcp__"):
        # the Claude Code check reads the whole MCP input once (named fields, every-string candidates, config
        # directories); what is left per path is CODEX_HOME and any .codex/
        if _claude_targets_agent_config(tool_name, tool_input, cwd, home=home, protected=protected):
            return True
        targets = consent_policy.mcp_config_candidates(tool_input) or []
        try:
            return any(_in_codex_config(t, cwd, home, codex_dir) for t in targets)
        except (OSError, ValueError):
            return True
    else:
        return False
    for target in targets:
        if not isinstance(target, str) or not target:
            return True
        try:
            if _claude_targets_agent_config("Write", {"file_path": target}, cwd, home=home, protected=protected):
                return True
            if _in_codex_config(target, cwd, home, codex_dir):
                return True
        except (OSError, ValueError):
            return True
    return False


# --- the decision ------------------------------------------------------------------------------------------

def hook_output(decision: Decision) -> dict[str, Any]:
    """What Codex reads from a ``PermissionRequest`` hook for a verified decision (Codex docs; measured on 0.162.1:
    allow -> no dialog and the command runs; deny -> "Blocked by hook" with the message, the command not run)."""
    verdict: dict[str, Any] = {"behavior": decision.decision}
    if decision.decision == "deny":
        verdict["message"] = f"Denied by a signed levain answer (key {decision.signer_fp})."
    return {"hookSpecificOutput": {"hookEventName": EVENT, "decision": verdict}}


def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, max_hold: float = DEFAULT_MAX_HOLD,
           policy: consent_policy.Policy | None = None, home: Path | None = None, codex_dir: Path | None = None,
           clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision. Raises on anything unexpected (after
    withdrawing the request it filed); the caller (:func:`main`) turns that into no decision. ``max_hold`` is a
    total deadline: the wait ends :data:`CLEANUP_RESERVE` seconds before it. ``policy`` is only ever reached
    through :func:`levain.consent.policy.guarded_policy`, never for a request filed at ``AGENT_CONFIG_TIER``."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    protected = protected_paths(Path(queue.directory), settings)
    deadline = clock() + max(0.0, float(max_hold)) - CLEANUP_RESERVE
    if not isinstance(event, Mapping) or event.get("hook_event_name") != EVENT:
        return None
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = event.get("session_id")
    if not isinstance(tool_name, str) or not tool_name or not isinstance(cwd, str) or not cwd \
            or not isinstance(session, str) or not session:
        return None
    if tool_input is None:
        tool_input = {}
    posture = event.get("permission_mode")
    posture = posture if isinstance(posture, str) and posture else None
    if policy is None and nothing_can_decide(settings):
        return None
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home, codex_dir=codex_dir,
                                        protected=protected)
    hold = min(float(settings.away_hold_seconds), max(0.0, deadline - clock())) if settings.away_mode else 0.0
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER)
    settled = False
    try:
        got: Decision | None = None
        if not agent_config and request.tier != AGENT_CONFIG_TIER:
            got = _verified(consent_policy.guarded_policy(request, queue, posture=posture, tool_input=tool_input,
                                                          policy=policy), request)
        wait = min(hold, max(0.0, deadline - clock()))
        if got is None and wait > 0:
            got = _verified(queue.await_decision(request.id, wait, tool_input=tool_input, posture=posture), request)
        if got is None:
            if not queue.withdraw(request.id, note="the hook stopped waiting; Codex's approval dialog stands"):
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
        settled = True
        return hook_output(got) if got is not None else None
    finally:
        if not settled:
            try:
                queue.withdraw(request.id, note="the hook failed; Codex's approval dialog stands")
            except Exception:
                pass


def _queue_for(queue_dir: Path, settings: AdapterSettings) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        source = authority_source(queue_dir, anchor=None)  # no authority: nothing can verify
    return ConsentQueue(queue_dir, authority=source)


def main(argv: list[str] | None = None, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """The hook entry point. Always exits 0; prints a decision only when the core verified one."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    try:
        parser = argparse.ArgumentParser(prog="levain-consent-codex")
        parser.add_argument("--queue-dir", default=None)
        parser.add_argument("--max-hold", type=float, default=DEFAULT_MAX_HOLD)
        args = parser.parse_args(argv)
        queue_dir = Path(args.queue_dir) if args.queue_dir else default_queue_dir()
        settings = load_settings(queue_dir)
        event = json.loads(stdin.read())
        out = decide(event, _queue_for(queue_dir, settings), settings, max_hold=args.max_hold)
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow
        try:
            print(f"levain consent hook: no decision ({type(exc).__name__}: {exc})"[:500], file=sys.stderr)
        except Exception:
            pass
        return 0
    return 0


# --- install (one workspace's .codex/) ---------------------------------------------------------------------

HOOK_TEMPLATE = '''#!{python}
# Installed by levain (levain.consent.adapters.codex.install). Codex's PermissionRequest hook: it reports a
# decision only when levain verified a signed answer or a standing consent grant; otherwise it prints nothing and
# Codex's approval dialog appears. Any failure here, including levain missing, is no decision.
''' + ISOLATE_PRELUDE.replace("{", "{{").replace("}", "}}") + '''
try:
    from levain.consent.adapters.codex import main
except BaseException:
    sys.exit(0)
sys.exit(main({argv}))
'''


def _paths(workspace: Path | str) -> tuple[Path, Path, Path, Path]:
    ws = Path(workspace).resolve()
    codex_dir = ws / CODEX_DIR_NAME
    hooks_dir = codex_dir / "hooks"
    return ws, codex_dir, hooks_dir / HOOK_SCRIPT_NAME, codex_dir / "hooks.json"


def install(workspace: Path | str, *, python: str | None = None, queue_dir: Path | str | None = None,
            max_hold: float = DEFAULT_MAX_HOLD, allow_untested_agent_version: bool = False) -> tuple[Path, Path]:
    """Install the hook into ``workspace``: ``.codex/hooks/levain_consent.py`` and a ``PermissionRequest`` entry
    (matcher ``*``, ``timeout`` = :func:`levain.consent.adapters.claude_code.hook_timeout`) in ``.codex/hooks.json``,
    other hooks kept and any earlier levain entry replaced. Codex will not run it until the user trusts it at
    Codex's "Hooks need review" screen; levain never writes that trust. Refuses (``UntestedAgentVersion``) when the
    installed ``codex`` is not a tested version (:data:`levain.consent.canary.TESTED_VERSIONS`), unless
    ``allow_untested_agent_version``. Returns (script, hooks.json)."""
    if not (isinstance(max_hold, (int, float)) and math.isfinite(max_hold) and max_hold >= 0):
        raise ValueError("max_hold must be a finite number of seconds >= 0")
    from levain.consent.canary import require_tested_version
    require_tested_version(AGENT, allow_untested=allow_untested_agent_version)
    ws, codex_dir, script, hooks_path = _paths(workspace)
    hooks_dir = script.parent
    for directory in (codex_dir, hooks_dir):
        _refuse_symlink(directory)
        if not os.path.lexists(directory):
            os.mkdir(directory)
        _refuse_symlink(directory)
    _refuse_symlink(script)
    _refuse_symlink(hooks_path)
    for target in (script, hooks_path):
        if not os.path.realpath(target.parent).startswith(str(ws) + os.sep):
            raise ValueError(f"refusing to install outside the workspace: {target}")
    python = python or sys.executable
    argv = ["--max-hold", repr(float(max_hold))]
    if queue_dir is not None:
        argv += ["--queue-dir", str(Path(queue_dir).resolve())]
    _atomic_write(script, HOOK_TEMPLATE.format(python=python, argv=repr(argv)), mode=0o755)
    command = hook_command(python, script)
    handler = {"type": "command", "command": command, "timeout": hook_timeout(max_hold)}
    for _ in range(5):
        before = _stat_key(hooks_path)
        try:
            config = json.loads(hooks_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            config = {}
        if not isinstance(config, dict):
            raise ValueError(f"{hooks_path} is not a JSON object")
        _merge_handler(config, command, handler, script=script)
        if _stat_key(hooks_path) != before:
            continue
        _atomic_write(hooks_path, json.dumps(config, indent=2) + "\n", mode=0o644, keep_mode=True)
        return script, hooks_path
    raise RuntimeError(f"{hooks_path} kept changing; not updated")


# --- trust: what Codex itself says about the installed hook --------------------------------------------------

@dataclass(frozen=True)
class TrustStatus:
    """``status``: ``trusted`` (Codex will run it), ``untrusted`` (never trusted: Codex shows "Hooks need review"
    and skips it until the user trusts it), ``changed`` (trusted once, the definition changed since: Codex shows
    the review screen again and skips it), ``disabled`` (trusted, but Codex lists it as not enabled, so it does not
    run it), ``absent`` (no levain hook in the workspace's hooks.json),
    ``unlisted`` (installed, but Codex does not load the workspace's hooks: the project is not trusted in Codex,
    or the hooks.json is invalid) or ``unknown`` (Codex could not be asked; ``detail`` says why). ``key`` is the
    hooks.state key and ``trusted_hash`` what CODEX_HOME's config.toml holds under it; ``current_hash`` is Codex's
    hash of the definition on disk. A hook SCRIPT edit is not a definition change (Codex hashes the definition,
    Q0 item 8): ``script_intact`` is levain's own check that the script is exactly what :func:`install` writes."""
    status: str
    detail: str = ""
    key: str | None = None
    trusted_hash: str | None = None
    current_hash: str | None = None
    script_intact: bool | None = None


def _our_handler(hooks_path: Path, script: Path) -> tuple[str, int, int] | None:
    """(command, group index, handler index) of levain's PermissionRequest handler in ``hooks_path``."""
    try:
        config = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    groups = (config.get("hooks") or {}).get(EVENT) if isinstance(config, dict) and isinstance(
        config.get("hooks"), dict) else None
    for g, group in enumerate(groups if isinstance(groups, list) else []):
        handlers = group.get("hooks") if isinstance(group, dict) else None
        for h, handler in enumerate(handlers if isinstance(handlers, list) else []):
            if _is_ours(handler, "", script):
                return handler["command"], g, h
    return None


def script_intact(script: Path, *, queue_dir: Path | str | None = None) -> bool:
    """True when ``script`` is exactly a rendering of :data:`HOOK_TEMPLATE` by :func:`install`: any interpreter,
    and an argv of ``--max-hold <finite seconds >= 0>`` alone (the default queue) or followed by ``--queue-dir`` naming
    exactly ``queue_dir`` (default :func:`levain.consent.queue.default_queue_dir`). A script pointed at any other
    queue is not intact: the queue's settings name the authority whose answers it trusts."""
    try:
        text = script.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    first, _, _ = text.partition("\n")
    m = re.search(r"^sys\.exit\(main\((\[.*\])\)\)$", text, re.MULTILINE)
    if not first.startswith("#!") or m is None:
        return False
    try:
        import ast
        argv = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return False
    if not (isinstance(argv, list) and all(isinstance(a, str) for a in argv)):
        return False
    expected_queue = str(Path(queue_dir).resolve()) if queue_dir is not None else str(default_queue_dir())
    if argv[:1] != ["--max-hold"] or argv[2:] not in ([], ["--queue-dir", expected_queue]):
        return False
    try:
        hold = float(argv[1])
    except (IndexError, ValueError):
        return False
    if not (math.isfinite(hold) and hold >= 0 and argv[1] == repr(hold)):
        return False
    return text == HOOK_TEMPLATE.format(python=first[2:], argv=repr(argv))


def _hooks_list(ws: Path, *, home: Path | None, codex_bin: str, timeout: float) -> dict[str, Any]:
    """Codex's own ``hooks/list`` for ``ws``, asked of a ``codex app-server`` started in it (stdio JSON-RPC)."""
    from levain.launch import child_env
    env = child_env()
    if home is not None:
        env["CODEX_HOME"] = str(home)
    msgs = [{"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "levain-doctor", "version": "1"}, "capabilities": None}},
            {"method": "initialized"},
            {"id": 2, "method": "hooks/list", "params": {"cwds": [str(ws)]}}]
    # The server exits at stdin EOF before answering, so stdin stays open until the answer is read.
    proc = subprocess.Popen([codex_bin, "app-server"], cwd=ws, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    buf = b""
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs).encode())
        proc.stdin.flush()
        fd = proc.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    if "result" not in msg:
                        raise RuntimeError(f"hooks/list failed: {msg.get('error')}")
                    return msg["result"]
    finally:
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()  # type: ignore[union-attr]
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    raise RuntimeError("codex app-server gave no hooks/list answer in time")


def trust_status(workspace: Path | str, *, home: Path | None = None, codex_bin: str | None = None,
                 timeout: float = TRUST_TIMEOUT, queue_dir: Path | str | None = None,
                 hooks_path: Path | None = None, script: Path | None = None) -> TrustStatus:
    """What Codex says about levain's hook in ``workspace``: Codex's own ``hooks/list`` (``trustStatus`` and
    ``enabled``) for the handler levain installed, with the ``trusted_hash`` from CODEX_HOME's ``config.toml``
    (``home``, default :func:`codex_home`) beside it. ``queue_dir`` is the queue the script must name
    (:func:`script_intact`). ``hooks_path`` and ``script`` name another hooks file and script than
    :func:`install`'s workspace pair: `levain init` wires a static script through CODEX_HOME's ``hooks.json``. Such a
    script is not :data:`HOOK_TEMPLATE`'s rendering, so ``script_intact`` is ``None`` for it. Never writes
    anything."""
    ws, _, default_script, default_hooks = _paths(workspace)
    intact_checked = script is None and hooks_path is None
    script = script if script is not None else default_script
    hooks_path = hooks_path if hooks_path is not None else default_hooks
    ours = _our_handler(hooks_path, script)
    if ours is None:
        return TrustStatus("absent", f"no levain PermissionRequest hook in {hooks_path}")
    command, g, h = ours
    key = f"{os.path.realpath(hooks_path)}:{TRUST_EVENT_KEY}:{g}:{h}"
    intact = script_intact(script, queue_dir=queue_dir) if intact_checked else None
    chome = home if home is not None else codex_home()
    trusted_hash = None
    try:
        with open(Path(chome) / "config.toml", "rb") as fh:
            state = tomllib.load(fh).get("hooks", {}).get("state", {}).get(key, {})
        trusted_hash = state.get("trusted_hash") if isinstance(state, dict) else None
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        pass
    codex_bin = codex_bin or shutil.which("codex")
    if not codex_bin:
        return TrustStatus("unknown", "codex is not on PATH", key, trusted_hash, None, intact)
    try:
        listed = _hooks_list(ws, home=home, codex_bin=codex_bin, timeout=timeout)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return TrustStatus("unknown", f"could not ask codex: {exc}"[:300], key, trusted_hash, None, intact)
    for entry in listed.get("data", []) if isinstance(listed, dict) else []:
        for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if not isinstance(hook, dict) or hook.get("key") != key or hook.get("command") != command:
                continue
            got, enabled = hook.get("trustStatus"), hook.get("enabled")
            status = {"trusted": "trusted", "untrusted": "untrusted", "modified": "changed"}.get(got, "unknown")
            if status == "trusted" and enabled is not True:
                status = "disabled"
            return TrustStatus(status, f"codex trustStatus {got!r}, enabled {enabled!r}", key, trusted_hash,
                               hook.get("currentHash"),
                               intact)
    return TrustStatus("unlisted", "codex does not list levain's hook (is the project trusted in Codex?)", key,
                       trusted_hash, None, intact)


if __name__ == "__main__":
    sys.exit(main())
