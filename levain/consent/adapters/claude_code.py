"""Claude Code's ``PermissionRequest`` hook on the consent queue (Q2, first slice).

Claude Code runs this hook when it is about to show its permission dialog, and shows the dialog AT ONCE while the
hook runs (measured, Q0 item 2); a later ``allow`` from the hook dismisses it and the tool runs. So the hook:

1. reads the event (``tool_name``, ``tool_input``, ``session_id``, ``agent_id`` for a subagent, ``cwd``) and files
   one request on the queue, with Claude Code's ``tool_use_id`` as its event id when the event carries one (a
   ``PermissionRequest`` measured in Q0 does not; the core then mints a random one);
2. asks the core's policy seam (:func:`levain.consent.policy.guarded_policy`) with the session's own posture (the
   event's ``permission_mode``) and the tool input the request was filed for; it answers only with a standing
   consent grant the queue verified. A request that targets agent config or hooks (:func:`targets_agent_config`)
   is never eligible for one;
3. when away mode is on (an adapter setting, default off, default hold 0 s) waits up to the hold for a signed
   answer the core verifies, and withdraws the request if none came (one whose hold ran out is expired);
4. prints ``allow`` only for a verified allow (with a line naming the grant when a grant answered) and ``deny``
   only for a verified deny. Anything else, including any exception, prints nothing and exits 0: the native
   dialog stands and the human answers it.

Nothing read from stdin is ever a decision: the event only says what is being asked. Bash commands are never
parsed (ruling 1): a Bash request is just a Bash request.

Settings live in ``<levain home>/consent/claude_code.json`` (:data:`SETTINGS_NAME`)::

    {"cockpit_data_root": "/absolute/cockpit/data/root", "away_mode": false, "away_hold_seconds": 0}

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
import site
import sys
import sysconfig
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import IO, Any

from levain.authority import anchor_path
# FILE_TOOLS, MCP_PATH_FIELDS, PolicyNotEligible and guarded_policy are re-exported: this module's names since Q2
from levain.consent.policy import AGENT_CONFIG_TIER, Policy
from levain.consent.policy import FILE_TOOLS as FILE_TOOLS
from levain.consent.policy import MCP_PATH_FIELDS as MCP_PATH_FIELDS
from levain.consent.policy import PolicyNotEligible as PolicyNotEligible
from levain.consent.policy import guarded_policy as guarded_policy
from levain.consent.policy import targets as _targets
from levain.consent.queue import ConsentQueue, Decision, authority_source, default_queue_dir
from levain.consent.record import ExternalGateRequest

AGENT = "claude_code"
EVENT = "PermissionRequest"
SETTINGS_NAME = "claude_code.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
# A request is filed for at least this long even when nobody is waiting on it, so the policy seam has a live
# request to look at; it is withdrawn as soon as the hook stops waiting.
MIN_FILE_HOLD = timedelta(seconds=5)
# ``max_hold`` (the installed ``--max-hold``) is the hook's TOTAL internal deadline, counted from the moment
# ``decide`` starts. Before it waits, the hook reserves this many seconds of it for what follows the wait: the
# withdraw (one sqlite write, busy timeout 30 s) and the re-read of a decision that landed meanwhile (one
# signature verification, 10 s, ``levain.authority`` verify timeout), plus 20 s of slack: 30 + 10 + 20 = 60. So
# the wait is at most ``max_hold - CLEANUP_RESERVE`` (0 when that is negative) and the hook ends by
# ``max(max_hold, CLEANUP_RESERVE)``.
CLEANUP_RESERVE = 60.0
# Claude Code's ``timeout`` for the hook is :func:`hook_timeout`: ``ceil(max(max_hold, CLEANUP_RESERVE))`` plus
# this margin (interpreter start, imports, settings read), so it always exceeds wait + CLEANUP_RESERVE and Claude
# Code never cancels the hook while a verified answer is on its way out (a cancelled hook's output is discarded).
TIMEOUT_MARGIN = 30
DEFAULT_MAX_HOLD = 900.0
# A request on one of FILE_TOOLS (or an MCP tool, by its MCP_PATH_FIELDS) whose target path is agent config or a
# hook is tagged agent_config and filed at AGENT_CONFIG_TIER (all three from levain.consent.policy): an allow then
# needs a key class covering T3, and no policy is ever asked (fail toward the human).
DEFAULT_TIER = "T2"

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
    return AdapterSettings(away_mode=away, away_hold_seconds=float(hold),
                           cockpit_data_root=_absolute_root(data.get("cockpit_data_root")))


def _absolute_root(root: Any) -> str | None:
    """``root`` canonicalized (``~`` expanded, symlinks resolved) when it is ABSOLUTE after ``~`` expansion; else
    ``None`` (no authority). A relative path is never resolved: the hook runs in the project's directory, and a
    repository must not be able to supply the authority it is judged by."""
    if not isinstance(root, str) or not root or "\x00" in root:
        return None
    try:
        expanded = Path(root).expanduser()
        if not expanded.is_absolute():
            return None
        return os.path.realpath(expanded)
    except (OSError, ValueError, RuntimeError):
        return None


def protected_paths(queue_dir: Path, settings: AdapterSettings) -> tuple[Path, ...]:
    """What the adapter itself trusts, so a file tool writing any of it is agent config (codex Q2 r1 #1): the
    queue directory (the adapter settings and the queue database live in it) and, when a cockpit data root is
    configured, its authority database (and sqlite's sidecars) and the authority anchor."""
    out = [Path(os.path.abspath(queue_dir))]
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        db = root / "cockpit.db"
        out += [db, *(Path(f"{db}{s}") for s in ("-wal", "-shm", "-journal")), anchor_path(root).parent]
    return tuple(out)


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

# Under a ``.claude`` directory (the workspace's or ~'s), what Claude Code loads and runs: hooks, subagents, skills,
# slash commands, and the plugin install tree (``~/.claude/plugins``: each plugin's hooks, MCP servers, agents).
CLAUDE_CONFIG_DIRS = frozenset({"hooks", "agents", "skills", "commands", "plugins"})
# MCP server definitions: a ``.mcp.json`` anywhere (a workspace's). The user's ``~/.claude.json`` is matched by
# its full path in :func:`targets_agent_config`: a ``.claude.json`` elsewhere (a test fixture) is not config.
CONFIG_FILE_NAMES = frozenset({".mcp.json"})
USER_CONFIG_NAME = ".claude.json"


def _is_config_path(path: Path) -> bool:
    parts = [p.lower() for p in path.parts]
    if parts and parts[-1] in CONFIG_FILE_NAMES:
        return True
    for i, part in enumerate(parts[:-1]):
        rest = parts[i + 1:]
        if part == ".claude":
            if rest[0] in CLAUDE_CONFIG_DIRS or (len(rest) == 1 and rest[0].startswith("settings")
                                                  and rest[0].endswith(".json")):
                return True
        if part == ".codex" and len(rest) == 1 and rest[0] in ("hooks.json", "config.toml"):
            return True
        if part == ".claude-plugin":
            return True  # a plugin's manifest directory (plugin.json can declare hooks and MCP servers inline)
        if part == "hooks" and i > 0 and os.path.isdir(Path(*path.parts[:i]) / ".claude-plugin"):
            return True  # a plugin outside ~/.claude/plugins (a local marketplace, --plugin-dir): its hooks/
    return False


def _under(path: Path, roots: tuple[str, ...]) -> bool:
    p = str(path).lower()
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def levain_package_dir() -> str:
    """The directory of the levain package this interpreter imports (realpath'd): the installed hook script runs
    ``levain.consent.adapters.claude_code`` from it, so an edit there changes what the consent hook does."""
    import levain
    return os.path.realpath(os.path.dirname(levain.__file__))


def import_site_dirs() -> tuple[str, ...]:
    """This interpreter's site directories (realpath'd), where the entries that redirect ``import levain`` live."""
    dirs = [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return tuple({os.path.realpath(d).lower() for d in dirs if d})


def _redirects_the_import(path: Path, sites: tuple[str, ...]) -> bool:
    """A site-directory entry that decides what ``import levain`` loads: a ``levain`` package or module, a
    ``levain-*.dist-info``, an ``__editable__`` levain finder, or any ``.pth`` / ``sitecustomize.py`` /
    ``usercustomize.py`` (run at interpreter start, whatever their name). Nothing else in site-packages."""
    p = str(path).lower()
    for s in sites:
        prefix = s.rstrip(os.sep) + os.sep
        if not p.startswith(prefix):
            continue
        first, _, rest = p[len(prefix):].partition(os.sep)
        if first in ("levain", "levain.py") or (first.startswith("levain-") and first.endswith(".dist-info")):
            return True
        if first.startswith("__editable__") and "levain" in first:
            return True
        if not rest and (first.endswith(".pth") or first in ("sitecustomize.py", "usercustomize.py")):
            return True
    return False


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path | None = None,
                         protected: Iterable[Path] = ()) -> bool:
    """True when a file tool (Edit/Write/MultiEdit/NotebookEdit) or an MCP tool targets an agent's config or hooks,
    in the workspace or the home directory: a ``.claude/settings*.json``; anything under a ``.claude/`` ``hooks``,
    ``agents``, ``skills``, ``commands`` or ``plugins`` directory (the adapter's own installed hook script is in
    ``.claude/hooks/``); a ``.mcp.json``, or the home directory's ``.claude.json``; a ``.codex/hooks.json`` or
    ``.codex/config.toml``; anything under a ``.claude-plugin/`` directory, or under ``hooks/`` beside one (a
    plugin); anything under the levain package this interpreter imports (:func:`levain_package_dir`, the hook runs
    it) or a site-directory entry that redirects that import (:func:`_redirects_the_import`); or any of
    ``protected`` (a file, or anything under a directory; the adapter passes :func:`protected_paths`). A file
    tool's target is its ``file_path``/``notebook_path``; an MCP tool's are its :data:`MCP_PATH_FIELDS` and nothing
    else. Each path is checked as written (``~`` expanded, joined to ``cwd``, normalised) and as resolved through
    symlinks; either match tags it. A target that is not a non-empty string is tagged (for a file tool, so is a
    missing one). Other tools are never tagged: Bash is not parsed (ruling 1)."""
    targets = _targets(tool_name, tool_input)
    if targets is None:
        return False
    home = home if home is not None else Path.home()
    try:
        roots = tuple({s for p in protected for s in (os.path.normpath(str(p)).lower(), os.path.realpath(p).lower())}
                      | {levain_package_dir().lower()})
        sites = import_site_dirs()
        user_config = Path(home) / USER_CONFIG_NAME
        user_configs = {os.path.normpath(user_config).lower(), os.path.realpath(user_config).lower()}
    except (OSError, ValueError, ImportError, TypeError):
        return True
    for target in targets:
        if not isinstance(target, str) or not target:
            return True
        raw = Path(str(home) + target[1:]) if target == "~" or target.startswith("~/") else Path(target)
        if not raw.is_absolute():
            raw = Path(cwd) / raw
        written = Path(os.path.normpath(raw))
        try:
            resolved = Path(os.path.realpath(raw))
        except (OSError, ValueError):
            return True
        if _is_config_path(written) or _is_config_path(resolved) or _under(written, roots) or _under(resolved, roots):
            return True
        if str(written).lower() in user_configs or str(resolved).lower() in user_configs:
            return True
        if _redirects_the_import(written, sites) or _redirects_the_import(resolved, sites):
            return True
    return False


# --- the decision ------------------------------------------------------------------------------------------

def _verified(decision: Any, request: ExternalGateRequest) -> Decision | None:
    """``decision`` only if it is the core's Decision for THIS request with a known verdict, over the digest
    of the request this hook filed (``Decision.request_digest`` is required since Q1 round 2)."""
    if not (isinstance(decision, Decision) and decision.request_id == request.id
            and decision.decision in ("allow", "deny") and decision.request_digest == request.digest):
        return None
    return decision


def hook_output(decision: Decision) -> dict[str, Any]:
    """What Claude Code reads from a ``PermissionRequest`` hook for a verified decision. A grant's allow also
    carries a ``systemMessage`` naming the grant, so an answer the operator did not give in the moment says where
    it came from. Claude Code decides whether to display it (a real 2.1.300 session showed only its own "Allowed by
    PermissionRequest hook", Q4 RUN 2026-10-15); the receipt is the record."""
    verdict: dict[str, Any] = {"behavior": decision.decision}
    if decision.decision == "deny":
        verdict["message"] = f"Denied by a signed levain answer (key {decision.signer_fp})."
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": EVENT, "decision": verdict}}
    if decision.via == "grant":
        out["systemMessage"] = (f"levain: allowed by your standing consent grant {decision.grant_id} "
                                f"(posture {decision.posture}); `levain consent grant revoke {decision.grant_id}` "
                                "ends it.")
    return out


def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, max_hold: float = DEFAULT_MAX_HOLD,
           policy: Policy | None = None,
           home: Path | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision. Raises on anything unexpected (after
    withdrawing the request it filed, so no ghost stays pending); the caller (:func:`main`) turns that into no
    decision. ``max_hold`` is a total deadline: the wait ends :data:`CLEANUP_RESERVE` seconds before it. ``policy``
    (default :func:`levain.consent.policy.decide`) is only ever reached through :func:`guarded_policy`, called with
    the event's ``permission_mode`` as ``posture`` (``None`` when absent: no grant matches) and the tool input the
    request was filed for, and never for a request filed at :data:`AGENT_CONFIG_TIER`: a direct caller cannot
    bypass ruling 3b item 1. What
    the adapter trusts (:func:`protected_paths`) is taken from ``queue.directory`` and ``settings`` here, never
    from the caller, so no call can leave it out; a queue without a directory raises before anything is filed."""
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
        tool_input = {}  # an MCP tool with no arguments arrives without tool_input (Q0 item 4)
    agent_id = event.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        session = f"{session}#agent:{agent_id}"
    tool_use_id = event.get("tool_use_id")
    mode = event.get("permission_mode")
    posture = mode if isinstance(mode, str) and mode else None
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home, protected=protected)
    hold = min(float(settings.away_hold_seconds), max(0.0, deadline - clock())) if settings.away_mode else 0.0
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER,
                         event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None)
    settled = False
    try:
        got: Decision | None = None
        if not agent_config and request.tier != AGENT_CONFIG_TIER:
            got = _verified(guarded_policy(request, queue, posture=posture, tool_input=tool_input, policy=policy),
                            request)
        wait = min(hold, max(0.0, deadline - clock()))
        if got is None and wait > 0:
            got = _verified(queue.await_decision(request.id, wait, tool_input=tool_input, posture=posture),
                            request)
        if got is None:
            # `settled` is set only after this withdraw returns: if the withdraw itself raises, the finally retries it
            if not queue.withdraw(request.id, note="the hook stopped waiting; the native dialog stands"):
                # Not pending any more: an answer may have landed between the wait and the withdraw.
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
        settled = True
        return hook_output(got) if got is not None else None
    finally:
        if not settled:
            # An exception between file() and the withdraw: withdraw now so no ghost request stays answerable
            # after the hook has gone. Its own failure is swallowed; the original exception propagates.
            try:
                queue.withdraw(request.id, note="the hook failed; the native dialog stands")
            except Exception:
                pass


def _queue_for(queue_dir: Path, settings: AdapterSettings) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        # No cockpit data root: a source over the queue DIRECTORY, which sqlite cannot open as a database, so
        # every replay fails and no answer can verify (never a file path a writer could plant a log at).
        source = authority_source(queue_dir, anchor=None)
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


# --- install (one workspace's project settings) ------------------------------------------------------------

# The first lines of every hook script levain writes (this adapter's, Codex's and the canary's): Python puts a
# script's own directory first on sys.path, so a package planted beside the script (``.claude/hooks/levain/``) would
# be imported instead of levain. Unless already running with -E -P (no PYTHON* variables, no script directory on
# sys.path), the script re-executes itself so; ``os`` is already loaded by then (site imports it), so nothing is read
# from the workspace first. ``-EP``, not ``-I``: ``-I`` also drops the user site, where levain may be installed.
ISOLATE_PRELUDE = '''import os
import sys

if not (sys.flags.safe_path and sys.flags.ignore_environment):
    try:
        os.execv(sys.executable, [sys.executable, "-EP", os.path.abspath(__file__), *sys.argv[1:]])
    except BaseException:
        pass
    sys.exit(0)
'''

HOOK_TEMPLATE = '''#!{python}
# Installed by levain (levain.consent.adapters.claude_code.install). Claude Code's PermissionRequest hook:
# it reports a decision only when levain verified a signed answer or a standing consent grant; otherwise it
# prints nothing and the native permission dialog stands. Any failure here, including levain missing, is no
# decision.
''' + ISOLATE_PRELUDE.replace("{", "{{").replace("}", "}}") + '''
try:
    from levain.consent.adapters.claude_code import main
except BaseException:
    sys.exit(0)
sys.exit(main({argv}))
'''


def install(workspace: Path | str, *, python: str | None = None, queue_dir: Path | str | None = None,
            max_hold: float = DEFAULT_MAX_HOLD, allow_untested_agent_version: bool = False) -> tuple[Path, Path]:
    """Install the hook into ``workspace``: ``.claude/hooks/levain_consent.py`` and a ``PermissionRequest``
    entry (matcher ``*``, ``timeout`` = :func:`hook_timeout`) in ``.claude/settings.json``, other
    settings kept and any earlier levain entry replaced. Raises
    :class:`levain.consent.canary.UntestedAgentVersion` when the installed ``claude`` is not a tested version
    (:data:`levain.consent.canary.TESTED_VERSIONS`), unless ``allow_untested_agent_version``. Returns
    (script, settings)."""
    if not (isinstance(max_hold, (int, float)) and math.isfinite(max_hold) and max_hold >= 0):
        raise ValueError("max_hold must be a finite number of seconds >= 0")
    from levain.consent.canary import require_tested_version
    require_tested_version(AGENT, allow_untested=allow_untested_agent_version)
    ws = Path(workspace).resolve()
    claude_dir = ws / ".claude"
    hooks_dir = claude_dir / "hooks"
    script = hooks_dir / HOOK_SCRIPT_NAME
    settings_path = claude_dir / "settings.json"
    for directory in (claude_dir, hooks_dir):
        _refuse_symlink(directory)
        if not os.path.lexists(directory):
            os.mkdir(directory)
        _refuse_symlink(directory)
    _refuse_symlink(script)
    _refuse_symlink(settings_path)
    for target in (script, settings_path):
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
        before = _stat_key(settings_path)
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict):
            raise ValueError(f"{settings_path} is not a JSON object")
        _merge_handler(settings, command, handler, script=script)
        if _stat_key(settings_path) != before:
            continue  # changed while we merged: merge again onto the newer file
        _atomic_write(settings_path, json.dumps(settings, indent=2) + "\n", mode=0o644, keep_mode=True)
        return script, settings_path
    raise RuntimeError(f"{settings_path} kept changing; not updated")


def hook_timeout(max_hold: float) -> int:
    """Claude Code's ``timeout`` for the installed hook: ``ceil(max(max_hold, CLEANUP_RESERVE))`` +
    :data:`TIMEOUT_MARGIN`. The wait ends :data:`CLEANUP_RESERVE` before ``max_hold`` (or is 0), so this always
    exceeds wait + that reserve."""
    return int(math.ceil(max(max_hold, CLEANUP_RESERVE))) + TIMEOUT_MARGIN


# The flags the installed command starts the interpreter with: -E -P from the FIRST process, so no PYTHON* variable
# (a sitecustomize on PYTHONPATH) and no script directory runs before the hook; the script's ISOLATE_PRELUDE stays for
# a direct invocation. Releases before this one installed the command without them (:func:`_is_ours` still knows it).
HOOK_FLAGS = "-EP"


def hook_command(python: str, script: Path | str) -> str:
    """The command levain installs for a consent hook (Claude Code's and Codex's): ``<python> -EP <script>``."""
    return f"{shlex.quote(python)} {HOOK_FLAGS} {shlex.quote(str(script))}"


def _is_ours(h: Any, command: str, script: Path | None) -> bool:
    """A handler this installation owns: its command is exactly ``command``, or it is exactly
    ``<one interpreter> -EP <this installation's absolute script path>`` or the earlier releases'
    ``<one interpreter> <that path>`` (a reinstall under another Python, or after an upgrade, must replace the old
    handler, not run beside it). Lookalike paths and flags, and any other command shape, are never ours."""
    if not isinstance(h, dict) or not isinstance(h.get("command"), str):
        return False
    if h["command"] == command:
        return True
    if script is None:
        return False
    try:
        tokens = shlex.split(h["command"])
    except ValueError:
        return False
    return len(tokens) in (2, 3) and tokens[-1] == str(script) and tokens[1:-1] in ([], [HOOK_FLAGS])


def _merge_handler(settings: dict[str, Any], command: str, handler: dict[str, Any],
                   script: Path | None = None) -> None:
    """Remove ONLY the handlers this installation owns (:func:`_is_ours`; other handlers and every group field
    kept; a group dropped only when that leaves it with no handlers), then add ``handler`` in its own group."""
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("settings 'hooks' is not a JSON object")
    groups = hooks.get(EVENT, [])
    if not isinstance(groups, list):
        raise ValueError(f"settings 'hooks.{EVENT}' is not a list")
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [h for h in group["hooks"] if not _is_ours(h, command, script)]
        if handlers or not group["hooks"]:
            kept.append({**group, "hooks": handlers})
    kept.append({"matcher": "*", "hooks": [handler]})
    hooks[EVENT] = kept


def _refuse_symlink(path: Path) -> None:
    if os.path.islink(path):
        raise ValueError(f"refusing to install through a symlink: {path}")


def _stat_key(path: Path) -> tuple[int, int, int, int] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _atomic_write(path: Path, text: str, *, mode: int, keep_mode: bool = False) -> None:
    """Write ``text`` to a new temp file beside ``path`` (created exclusively, never through a symlink),
    fsync it, and rename it over ``path``: a rename replaces a symlink leaf itself, never its target, and a
    reader sees the old file or the new one, never a partial write."""
    if keep_mode:
        try:
            mode = os.lstat(path).st_mode & 0o777
        except FileNotFoundError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
