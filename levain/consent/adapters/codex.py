"""Codex's ``PermissionRequest`` hook on the consent queue (Q3).

Codex runs this hook when it is about to ask for approval, and shows NO dialog while the hook runs (measured, Q0
item 6 and Q3): the TUI says "Running hook" and the approval dialog appears only after the hook returns. So a hold
delays the dialog, and the hook holds only while the operator is away (their word, or input idle) and stops
holding when they come back. The hook:

1. reads the event (``tool_name``, ``tool_input``, ``session_id``, ``cwd``, ``permission_mode``) and files one
   request on the queue. A Codex ``PermissionRequest`` carries no per-event id (only ``turn_id``, which one turn
   shares across its requests), so the core mints a random one;
2. asks the policy seam (:func:`levain.consent.policy.guarded_policy`, with the tool input it filed and posture =
   the event's ``permission_mode``), except for a request that targets agent config or hooks
   (:func:`targets_agent_config`), which is never eligible. A standing consent grant can answer there; its targets
   are read by :func:`policy_targets`, registered with the policy when this module is imported;
3. when away mode is on (:func:`levain.consent.auto_away.away_state`: the operator's explicit word, else input
   idle on macOS, else off) waits up to the hold for a signed answer the core verifies, re-reading away mode while
   it waits so an operator who comes back gets Codex's dialog within about a second, and withdraws the request if
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

plus ``away_word`` and ``away_idle_seconds`` once ``levain consent away`` has written them.

:func:`install` writes the hook into ONE workspace's ``.codex/`` and never writes Codex's trust for it: Codex shows
"Hooks need review" at the next start and the user trusts the hook there; :func:`trust_status` reports what Codex
itself says about it. ``levain init`` and ``levain update`` wire the machine-wide CODEX_HOME ``hooks.json`` instead,
with the PreToolUse gate below, and DO write Codex's trust for that file's hooks (:func:`write_hook_trust`).

Known edges of the PreToolUse gate (each measured or read, none built against):

- input written into an exec session already open (``write_stdin``) runs no PreToolUse, so a halted session can
  keep typing into a shell it opened before the halt (B7 measures it);
- a patch the model sends through the shell (``apply_patch`` inside an ``exec_command``) reaches the hook as
  ``Bash``, which is never parsed (the R-g class);
- Codex's trust covers the gate's command, not its script: a same-uid edit of the script after install keeps the
  trust and is caught only by ``levain doctor`` (and ``levain.hookrun`` exits 0 when the script is missing);
- a subagent's ``session_id`` as the root's is read from Codex's source, not measured beyond the latch run;
- ``config/batchWrite``'s upsert merge of ``hooks.state`` is unmeasured beyond the keys levain writes;
- a queue store that exists and cannot be read denies every Codex call on the machine (deliberate: a halt cannot
  then be ruled out);
- the gate's last halt read and its allow are two statements, so a sibling call's halt landing between them lets
  that one allow through (the read is the last statement before the return, so the window is the return itself).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
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
    _absolute_root, _atomic_write, _is_ours, _merge_handler, _refuse_symlink, _stat_key, _verified, away_hold,
    effective_away, hold_for_answer, hook_command, read_settings_file, still_away_from, with_away_mark,
    hook_timeout, levain_home_protected, nothing_can_decide,
    protected_paths,
)
from levain.consent.adapters.claude_code import (  # noqa: F401 - GATE_TIMEOUT is the installer's
    ATTENDED_POSTURES, FLOOR_TEXT, GATE_HOLD_CEILING, GATE_POLL, GATE_SLEEP_CAP, GATE_TIMEOUT, HOOK_FLAGS, HOOKRUN,
    RULING_TEXT, UNCONFINED_ENV, U_IRR, U_REV, _pre, _what, floor_text, flush_held_pings, notify, send_repings,
    unconfined, unconfined_state,
)
from levain.consent.adapters.claude_code import DEFAULT_GATE_HOLD, SLOT_HOW, _bound_home, queue_socket_planted
from levain.consent.broker_client import in_slot
from levain.consent.adapters.claude_code import targets_agent_config as _claude_targets_agent_config
from levain.consent.adapters.rules import (  # noqa: F401 - the rules' one home; re-exported under these names
    _PATCH_HEADERS, _PATCH_MARKERS, CODEX_DIR_NAME, PATCH_TOOL, _in_codex_config, patch_targets,
)
from levain.consent.policy import AGENT_CONFIG_TIER
from levain.consent.queue import DB_NAME as QUEUE_DB_NAME
from levain.consent.queue import ConsentQueue, Decision, authority_source, default_queue_dir, queue_dir_for
from levain.consent.record import ExternalGateRequest

AGENT = "codex"
EVENT = "PermissionRequest"
SETTINGS_NAME = "codex.json"
HOOK_SCRIPT_NAME = "levain_consent.py"
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


def _codex_dir_or(codex_dir: Path | None, environ: Mapping[str, str], home: Path) -> Path:
    """``codex_dir``, else :func:`codex_home` as ``environ`` and ``home`` name it (``$CODEX_HOME``, else
    ``home/.codex``): the hook paths' default, read from what the entry point bound, never this process."""
    return codex_dir if codex_dir is not None else Path(environ.get("CODEX_HOME") or Path(home) / ".codex")


# --- ruling 3b item 1: agent config and hooks ----------------------------------------------------------------

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


def targets_agent_config(tool_name: str, tool_input: Any, cwd: str, *, home: Path, codex_dir: Path,
                         environ: Mapping[str, str], protected: tuple[Path, ...] = (),
                         git_cache: dict[Any, Any] | None = None) -> bool:
    """True when a Codex ``apply_patch`` or an MCP tool targets an agent's config or hooks: every path the Claude
    Code adapter tags (:func:`levain.consent.adapters.claude_code.targets_agent_config`: Claude Code's config,
    ``.codex/hooks.json`` and ``config.toml``, the levain package, the adapter's own queue and authority), plus
    anything under a ``.codex/`` directory or under CODEX_HOME (``codex_dir``). An MCP tool's targets are its
    :data:`MCP_PATH_FIELDS` and every other string in its input (:func:`levain.consent.policy.mcp_config_candidates`). A patch whose headers name no file
    is tagged, as is a non-string MCP path. Shell requests (:data:`levain.authority.CONSENT_SHELL_TOOLS`) and every
    other tool are never tagged: a command is not parsed (ruling 1). ``git_cache`` is one gate call's: one git
    budget (:func:`levain.consent.adapters.claude_code._git_answer`) for all its headers, past which a header is
    tagged."""
    if tool_name == PATCH_TOOL:
        command = tool_input.get("command") if isinstance(tool_input, Mapping) else None
        targets: list[Any] | None = patch_targets(command)
        if targets is None:
            return True
    elif tool_name.startswith("mcp__"):
        # the Claude Code check reads the whole MCP input once (named fields, every-string candidates, config
        # directories); what is left per path is CODEX_HOME and any .codex/
        if _claude_targets_agent_config(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected,
                                        git_cache=git_cache):
            return True
        targets = consent_policy.mcp_config_candidates(tool_input) or []
        try:
            return any(_in_codex_config(t, cwd, home, codex_dir) for t in targets)
        except (OSError, ValueError):
            return True
    else:
        return False
    git_cache = git_cache if git_cache is not None else {}   # one budget for the whole patch (and the gate call)
    return any(_header_is_config(target, cwd, home, codex_dir, protected, git_cache, environ=environ)
               for target in targets)


def _header_is_config(target: Any, cwd: str, home: Path, codex_dir: Path, protected: tuple[Path, ...],
                      git_cache: dict[Any, Any] | None = None, *, environ: Mapping[str, str]) -> bool:
    """Whether one patch header names agent config or hooks (an empty or unreadable one is taken to). Where git runs
    hooks from is asked of git (the Claude Code check), shared across one call's headers through ``git_cache``."""
    if not isinstance(target, str) or not target:
        return True
    try:
        return (_claude_targets_agent_config("Write", {"file_path": target}, cwd, home=home, environ=environ,
                                             protected=protected, git_cache=git_cache)
                or _in_codex_config(target, cwd, home, codex_dir))
    except (OSError, ValueError):
        return True


# --- the decision ------------------------------------------------------------------------------------------

def hook_output(decision: Decision) -> dict[str, Any]:
    """What Codex reads from a ``PermissionRequest`` hook for a verified decision (Codex docs; measured on 0.162.1:
    allow -> no dialog and the command runs; deny -> "Blocked by hook" with the message, the command not run)."""
    verdict: dict[str, Any] = {"behavior": decision.decision}
    if decision.decision == "deny":
        verdict["message"] = f"Denied by a signed levain answer (key {decision.signer_fp})."
    return {"hookSpecificOutput": {"hookEventName": EVENT, "decision": verdict}}


def decide(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, max_hold: float = DEFAULT_MAX_HOLD,
           policy: consent_policy.Policy | None = None, environ: Mapping[str, str], home: Path,
           codex_dir: Path | None = None, clock: Callable[[], float] = time.monotonic,
           idle: Callable[[], float | None] | None = None,
           still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    """The hook's output for one event, or ``None`` for no decision. Raises on anything unexpected (after
    withdrawing the request it filed); the caller (:func:`main`) turns that into no decision. ``max_hold`` is a
    total deadline: the wait ends :data:`CLEANUP_RESERVE` seconds before it. The hold and ``idle`` / ``still_away``
    are the Claude Code adapter's (:func:`levain.consent.adapters.claude_code.decide`). ``policy`` is only ever reached
    through :func:`levain.consent.policy.guarded_policy`, never for a request filed at ``AGENT_CONFIG_TIER``."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
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
    protected = protected_paths(Path(queue.directory), settings, home=home, environ=environ, cwd=cwd)
    agent_config = targets_agent_config(tool_name, tool_input, cwd, home=home,
                                        codex_dir=_codex_dir_or(codex_dir, environ, home), environ=environ,
                                        protected=protected)
    if agent_config and unconfined(cwd, home=home, environ=environ, codex_home=codex_dir):
        return None  # an unconfined session (Q10 D2): Codex's approval answers, as the gate left it to the mode
    hold = min(away_hold(settings, AGENT, idle), max(0.0, deadline - clock()))
    request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
                         hold=max(timedelta(seconds=hold), MIN_FILE_HOLD),
                         tier=AGENT_CONFIG_TIER if agent_config else DEFAULT_TIER, model=event.get("model"))
    settled = False
    try:
        got: Decision | None = None
        if not agent_config and request.tier != AGENT_CONFIG_TIER:
            got = _verified(consent_policy.guarded_policy(request, queue, posture=posture, tool_input=tool_input,
                                                          policy=policy), request)
        wait = min(hold, max(0.0, deadline - clock()))
        back = None
        if got is None and wait > 0:
            got, back = hold_for_answer(queue, request, wait, tool_input=tool_input, posture=posture,
                                        still_away=still_away)
        if got is None:
            note = f"away mode ended ({back}); Codex's approval dialog stands" if back else \
                "the hook stopped waiting; Codex's approval dialog stands"
            if not queue.withdraw(request.id, note=note):
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
        settled = True
        return hook_output(got) if got is not None else None
    finally:
        if not settled:
            try:
                queue.withdraw(request.id, note="the hook failed; Codex's approval dialog stands")
            except Exception:
                pass


# --- the PreToolUse gate and its latch (Q14 B5) ---------------------------------------------------------------
#
# ``codex exec`` never asks for approval, so the PermissionRequest hook above never fires there; PreToolUse fires
# for every tool call in every mode. The gate mirrors Claude Code's (levain.consent.adapters.claude_code.gate) with
# what Codex 0.162.1 reads differently (source read at rust-v0.162.1, hooks/src/engine/output_parser.rs):
#
# - a deny is the plain ``permissionDecision: deny`` + reason. NEVER ``continue: false``: Codex rejects it as
#   unsupported and DROPS the deny with it, so the call runs (measured, runs2 BC_1).
# - ``permissionDecision: allow`` is unsupported too (the hook is marked failed and the call proceeds): a verified
#   allow is delivered as no decision, with a line of additionalContext naming who allowed it.
# - no hook output stops a Codex turn. What stops one is the LATCH (runs3-codex RUN 1, C4, n=2): the first undecided
#   deny of the irreversible tier halts the session in the queue store (:meth:`ConsentQueue.mark_undecided` with
#   ``halt``), and every later PreToolUse of that session is denied with :data:`HALT_TEXT` until the late ruling on
#   the halting request releases it. The halt lives in the queue store, never in a file the agent's own sandbox
#   could let it write.
#
# The halt is keyed by the hook's ``session_id`` alone (a subagent's ``agent_id`` is not appended): a halt raised
# inside a subagent latches the whole session, never only the subagent (a subagent's id is read, not measured). When the store exists and cannot be read,
# every call is denied (the halt cannot be ruled out); when it does not exist, no session can be halted.
# A hook Codex kills at its timeout, or one that exits non-zero, FAILS OPEN on Codex (no ``onFailure`` setting
# exists): the entry's timeout is the ceiling's (:data:`GATE_TIMEOUT`), and :func:`main` never raises.
#
# The T3 floor (Q10 D1) and the unconfined session (D2) are Claude Code's (its gate section): with no enrolled key
# that can sign a T3 allow, a gated call in any mode but ``default`` and ``plan`` is denied (a plain
# deny, per the first bullet), and a session started with ``LEVAIN_UNCONFINED=1`` passes gated calls, its halt still
# read first, unless a settings file the session can load (Codex's or Claude Code's) sets the variable.
#
# Codex keeps no per-call approval in its rollout, so the authority ledger cannot read after the fact who allowed
# a gated call: the gate files the call's row itself when levain answered it (:func:`_ledger_note`).

GATE_EVENT = "PreToolUse"
# runs3-codex RUN 1 C4, verbatim as measured (the only form that stopped a Codex run), with the gap number
HALT_TEXT = ("levain: this session is halted on gap #{n}. No further tool calls are allowed. Write a short summary of "
             "what is done and end your turn.")


def _call_what(tool_name: str, tool_input: Any, cwd: str | None = None, *, home: Path, environ: Mapping[str, str],
               codex_dir: Path | None = None, protected: tuple[Path, ...] = (),
               git_cache: dict[Any, Any] | None = None) -> str:
    """The action in a few words: for a readable patch, ``apply_patch of <paths>`` naming the agent-config paths
    FIRST and counting the rest (``apply_patch of .codex/hooks.json (+14 other files)``), so a cut never hides the
    path that made the call gated (the agent orders the headers); else the Claude Code adapter's description."""
    if tool_name == PATCH_TOOL and isinstance(tool_input, Mapping):
        paths = patch_targets(tool_input.get("command"))
        if paths:
            tagged = []
            if cwd:
                codex_dir = _codex_dir_or(codex_dir, environ, home)
                git_cache = git_cache if git_cache is not None else {}
                tagged = [p for p in paths
                          if _header_is_config(p, cwd, home, codex_dir, protected, git_cache, environ=environ)]
            if tagged and len(tagged) < len(paths):
                rest = len(paths) - len(tagged)
                text = f"{PATCH_TOOL} of {', '.join(tagged)} (+{rest} other file{'s' if rest != 1 else ''})"
            else:
                text = f"{PATCH_TOOL} of {', '.join(paths)}"
            return _what(PATCH_TOOL, None, text)
    return _what(tool_name, tool_input)


def gate_class(tool_name: str, tool_input: Any, cwd: str, *, home: Path, environ: Mapping[str, str],
               codex_dir: Path | None = None, protected: tuple[Path, ...] = (),
               git_cache: dict[Any, Any] | None = None) -> str | None:
    """The gate's tier for a Codex call: ``irreversible`` for agent config and hooks (:func:`targets_agent_config`,
    Q10's floor and Q14's irreversible tier at once), else ``None`` (not gated). A shell command is never parsed.
    THE SEAM for B6: the push executor's tool joins here as ``irreversible``."""
    if targets_agent_config(tool_name, tool_input, cwd, home=home, codex_dir=_codex_dir_or(codex_dir, environ, home),
                            environ=environ, protected=protected, git_cache=git_cache):
        return "irreversible"
    return None


def _deny(reason: str, context: str | None = None) -> dict[str, Any]:
    out = _pre("deny", reason)
    if context:
        out["hookSpecificOutput"]["additionalContext"] = context
    return out


def gate_output(decision: Decision) -> dict[str, Any]:
    """What Codex reads for a verified decision: a deny with the authority named, or, for an allow, no decision
    and one line of context (Codex refuses ``permissionDecision: allow``; with no decision the call runs)."""
    if decision.via == "ruling":
        how = f"your ruling on gap #{decision.gap} (key {decision.signer_fp})"
    elif decision.via == "grant":
        how = f"your standing consent grant {decision.grant_id}"
    elif decision.via == "contained_shell":
        how = f"your standing contained-shell grant {decision.grant_id}"
    else:
        how = f"a signed levain answer (key {decision.signer_fp})"
    return decision_output(decision.decision, how)


def decision_output(decision: str, how: str) -> dict[str, Any]:
    """:func:`gate_output`'s words for ``decision`` (``allow`` or ``deny``) by the authority ``how`` names."""
    if decision == "allow":
        return {"hookSpecificOutput": {"hookEventName": GATE_EVENT,
                                       "additionalContext": f"levain: this call is allowed by {how}."}}
    return _deny(f"levain: denied by {how}. Do not retry it and do not achieve it another way.")


def undecided_output(tier: str | None, what: str, gap: int) -> dict[str, Any]:
    """The deny at D: U-IRR for the irreversible tier (the latch is the stop, never ``continue: false``), U-REV
    for the reversible one: a slot's ungated grantable call that a dispatch run queued and nobody answered
    (:func:`_slot_ungated`, W2-c3)."""
    return _deny((U_IRR if tier == "irreversible" else U_REV).format(x=what, n=gap))


def ruling_context(queue: ConsentQueue, session: str) -> str | None:
    """This Codex session's late rulings not yet delivered, as DESIGN §3.6's text, one per line (claimed: each
    reaches the session once), or ``None``."""
    lines = []
    for ruling in queue.claim_rulings_for(AGENT, session):
        got = queue.get(ruling.request_id)
        if got is not None and got[0].tool_name == "levain_limit":   # Q13: a limit has no action to do
            from levain.consent.limits import ruling_text

            text = ruling_text(ruling.ruling, ruling.gap, got[0].display, queue_dir=queue.directory,
                               request_id=ruling.request_id)
            if text:
                lines.append(text)
            continue
        what = _what(got[0].tool_name, None, got[0].display) if got is not None else f"request {ruling.request_id}"
        text = RULING_TEXT.get(ruling.ruling)
        if text is not None:
            lines.append(text.format(n=ruling.gap, x=what))
    return "\n".join(lines) or None


def _ledger_note(queue_dir: Path, event: Mapping[str, Any], session: str, tool_name: str, tool_input: Any, cwd: str,
                 *, decision: Decision | None = None, refused: str | None = None,
                 request_id: str | None = None) -> None:
    """File the gated call's ledger row at decision time (:func:`levain.consent.ledger.gate_row`: a verified allow
    is a ``pre`` row with levain's stamp, anything denied a ``denied_by_gate`` row). A ledger that cannot be written
    never changes the gate's answer: the queue's receipts still record every decision."""
    try:
        from levain.consent.ledger import gate_row

        gate_row(AGENT, queue_dir, event, session, tool_name, tool_input, cwd, decision=decision, refused=refused,
                 request_id=request_id)
    except Exception:  # noqa: BLE001 - the ledger is a record, never the gate
        pass


def resume_meta_of(event: Mapping[str, Any], *, environ: Mapping[str, str]) -> dict[str, Any]:
    """What a later resume of this Codex session needs (:meth:`ConsentQueue.file`'s ``resume``): the hook's parent
    process when it is ``codex`` itself (Codex runs the hook command as its own child, measured on 0.162.1), with
    ``-p`` when that process is a ``codex exec`` and ``interactive`` otherwise; the event's cwd; and the CODEX_HOME
    the session runs under (``environ``'s; ``None``: the default); and that process's option names, which a resume
    refuses on
    (:func:`levain.consent.delivery.codex_unrestored`). Only the fields this
    queue's resume record accepts are kept."""
    from levain.consent import queue as queue_module
    from levain.consent.delivery import option_names

    pid: int | None = None
    flags: str | None = None
    invocation = "interactive"
    ppid = os.getppid()
    try:
        args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(ppid)], capture_output=True, text=True, timeout=2,
                              stdin=subprocess.DEVNULL, env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"}).stdout.split()
    except (OSError, subprocess.SubprocessError, ValueError):
        args = []
    if ppid > 1 and args and os.path.basename(args[0]) == "codex":
        pid = ppid
        if args[1:2] == ["exec"]:
            invocation = "-p"
        flags = option_names(" ".join(args))
    cwd, chome = event.get("cwd"), environ.get("CODEX_HOME", "")
    record = {"invocation": invocation, "codex_pid": pid, "cwd": cwd if isinstance(cwd, str) and cwd else None,
              "config_dir": chome if os.path.isabs(chome) else None, "codex_flags": flags}
    keys = getattr(queue_module, "_RESUME_KEYS", frozenset(record))
    return {k: v for k, v in record.items() if k in keys}


def gate(event: Any, queue: ConsentQueue, settings: AdapterSettings, *, home: Path, environ: Mapping[str, str],
         codex_dir: Path | None = None, sleep: Callable[[float], None] = time.sleep,
         still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    """The gate's output for one Codex ``PreToolUse`` event, or ``None`` (the call goes on). The halt is read
    first, as soon as the event names a session: a session halted on a gap gets :data:`HALT_TEXT` for EVERY call,
    gated or not, until the ruling on that gap releases it. An event missing a required field (``session_id``,
    ``tool_name``, an absolute ``cwd``) is denied. Else a gated call (:func:`gate_class`) gets no decision in a session started
    unconfined (:func:`levain.consent.adapters.claude_code.unconfined_state` ``"on"``; no ruled allow is spent, and a
    halt read above still wins); else is run once on a ruled allow; else, when nothing can decide it
    (:func:`nothing_can_decide` at the gated tier), gets no decision in ``default`` (Codex's approval asks) or ``plan``, and in every other mode, an absent or unknown one
    included, the floor's plain deny (:func:`floor_text`, nothing filed); else,
    when something could decide it and, in ``default`` mode (approvals
    on), only while the operator is away, filed with its hold = D (``gate_hold_seconds``), pinged, held for a
    verified decision (delivered), and at D marked undecided (the irreversible tier halting its session) and denied
    (:func:`undecided_output`). Every allow, and every no-decision return of a gated call (unconfined, the floor's
    ``default``/``plan`` pass, the attended pass), is re-checked against the halt just before it is returned: a
    halt set meanwhile wins (a sibling call that halts the session after that check still races it). Anything that fails once the call is known gated, or while the halt is read, is a deny. Late
    rulings for the session, and the answers to its questions not yet delivered
    (:func:`levain.consent.band.question_context`), ride whatever is returned, as ``additionalContext``."""
    if getattr(queue, "directory", None) is None:
        raise TypeError("the queue has no directory: the adapter's own files cannot be protected")
    if not isinstance(event, Mapping) or event.get("hook_event_name") != GATE_EVENT:
        return None
    flush_held_pings(queue)   # first, so no return below (a halted session, limits, malformed) skips it
    tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input", {}), event.get("cwd")
    session = event.get("session_id")
    has_session = isinstance(session, str) and bool(session)
    if has_session:
        try:
            halted = queue.is_halted(AGENT, session)
        except Exception as exc:
            return _deny(f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken. Do not retry "
                         "it and do not achieve it another way.")
        if halted is not None:
            _ledger_note(Path(queue.directory), event, session, str(tool_name), tool_input,
                         cwd if isinstance(cwd, str) else "", refused="halted")
            return _with_rulings(_deny(HALT_TEXT.format(n=halted)), queue, session, cwd, environ)
    missing = _missing_of(event)
    if missing:
        return _missing_output(missing)
    if tool_input is None:
        tool_input = {}
    limited = _limits(event, queue, settings, session, tool_name, tool_input, cwd, home=home, environ=environ,
                      codex_dir=codex_dir)
    if limited is not None:
        return _with_rulings(limited, queue, session, cwd, environ)   # a late End on the limit reaches a halted Codex session
    try:
        send_repings(queue, settings)
    except Exception:
        pass  # a re-ping is a ping: it never fails the gate
    if still_away is None:
        still_away = still_away_from(Path(queue.directory) / SETTINGS_NAME, settings, queue=queue)
    out = _gate_call(event, queue, settings, tool_name, tool_input, cwd, session, home=home, environ=environ,
                     codex_dir=codex_dir, sleep=sleep, still_away=still_away)
    return _with_rulings(out, queue, session, cwd, environ)


def _missing_of(event: Mapping[str, Any]) -> list[str]:
    session, tool_name, cwd = event.get("session_id"), event.get("tool_name"), event.get("cwd")
    return [k for k, v in (("session_id", session), ("tool_name", tool_name),
                           ("cwd", cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None))
            if not isinstance(v, str) or not v]


def _missing_output(missing: list[str]) -> dict[str, Any]:
    """The deny for an event missing a required field (:func:`gate`'s, and the slot gate's)."""
    return _deny(f"levain: the hook event has no valid {', '.join(missing)}, so the action was NOT taken. Do not "
                 "retry it and do not achieve it another way.")


def _limits(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, session: str, tool_name: str,
            tool_input: Any, cwd: str, *, home: Path, environ: Mapping[str, str],
            codex_dir: Path | None) -> dict[str, Any] | None:
    """Q13 after the queue halt is read (:func:`levain.consent.limits.check`); ``None`` lets the call on to the gate.
    No hook output stops a Codex turn (``continue: false`` would void the deny), so a limit halt is the limits store's
    own: every later call of the session is denied until an Extend lifts it. A GATED call held and then extended is
    denied with a retry line, so the gate's own hold runs in a fresh hook."""
    from levain.consent import limits

    hold = min(settings.gate_hold_seconds, GATE_HOLD_CEILING)
    got = limits.check(AGENT, event, queue, session, tool_name, tool_input, hold=hold, cwd=cwd,
                       notify=lambda text: notify(settings, text, cwd=queue.directory),
                       resume=resume_meta_of(event, environ=environ))
    if got.go:
        if got.extended and gate_class(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                                       protected=protected_paths(Path(queue.directory), settings, home=home,
                                                                 environ=environ, cwd=cwd)) is not None:
            return _deny(limits.retry_text(got.extended))
        return None
    return _deny(got.text)


def _with_rulings(out: dict[str, Any] | None, queue: ConsentQueue, session: str,
                  cwd: Any = None, environ: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    try:
        context = ruling_context(queue, session)
    except Exception:
        context = None  # undelivered rulings stay for the next hook
    try:
        from levain.consent.band import question_context

        answers = question_context(queue.directory, AGENT, session)
    except Exception:
        answers = None  # undelivered answers stay for the next hook
    try:
        from levain.band_view import gate_notice

        notice = gate_notice(queue.directory, AGENT, session, cwd if isinstance(cwd, str) else None, environ)
    except Exception:
        notice = None  # an advisory notice must never fail the consent gate, whatever it raises
    context = "\n".join(c for c in (context, answers, notice) if c) or None
    if context:
        out = out if out is not None else {"hookSpecificOutput": {"hookEventName": GATE_EVENT}}
        spec = out["hookSpecificOutput"]
        spec["additionalContext"] = f"{spec['additionalContext']}\n{context}" if spec.get("additionalContext") \
            else context
    return out


def _gate_call(event: Mapping[str, Any], queue: ConsentQueue, settings: AdapterSettings, tool_name: str,
               tool_input: Any, cwd: str, session: str, *, home: Path, environ: Mapping[str, str],
               codex_dir: Path | None, sleep: Callable[[float], None],
               still_away: Callable[[], tuple[bool, str, str]] | None = None) -> dict[str, Any] | None:
    from datetime import datetime, timezone

    from levain.cockpit.consent import parse_stamp

    qdir = Path(queue.directory)
    note = lambda **kw: _ledger_note(qdir, event, session, tool_name, tool_input, cwd, **kw)  # noqa: E731

    def halt_wins(request_id: str | None) -> dict[str, Any] | None:
        halted = queue.is_halted(AGENT, session)
        if halted is None:
            return None
        note(refused="halted", request_id=request_id)
        return _deny(HALT_TEXT.format(n=halted))

    def deliver(decision: Decision) -> dict[str, Any]:
        # an allow only while the session is still not halted: a sibling call's undecided deny may have halted it
        # while this one waited (L3 r1 codex 4). Read before the ledger note, so the common case logs no allow,
        # and again as the last statement before the return, so the window left is the return itself (r2 codex 4)
        if decision.decision != "allow":
            note(decision=decision)
            return gate_output(decision)
        out = halt_wins(decision.request_id)
        if out is not None:
            return out
        note(decision=decision)
        return halt_wins(decision.request_id) or gate_output(decision)

    tier: str | None = "irreversible"           # until classified: a failure before that denies with the latch's tier
    what = "this call"
    try:
        protected = protected_paths(qdir, settings, home=home, environ=environ, cwd=cwd)
        git_cache: dict[Any, Any] = {}   # one git budget for the gate call (L3 r3 G1), its description included
        what = _call_what(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                          protected=protected,
                          git_cache=git_cache)
        tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                          protected=protected,
                          git_cache=git_cache)
        if tier is None:
            return None
        tool_use_id = event.get("tool_use_id")
        event_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None
        mode = event.get("permission_mode")
        posture = mode if isinstance(mode, str) and mode else None
        loose = unconfined_state(cwd, home=home, environ=environ, codex_home=codex_dir)
        if loose == "on":
            # the operator started this session unconfined (Q10 D2): the mode decides, no ruled allow is spent. A
            # halt was read before this call reached here (:func:`gate`): the env var never releases one. A
            # settings file that sets the variable makes it ignored (L2-1, :func:`unconfined_state`)
            return halt_wins(None)  # and a halt set since the gate read it still wins (L3 r1 G2)
        ruled = queue.take_ruled_allow(AGENT, session, tool_name, tool_input, cwd=cwd, event=event_id)
        if ruled is not None:
            return deliver(ruled)
        if nothing_can_decide(settings, tier=AGENT_CONFIG_TIER):
            if posture in (*ATTENDED_POSTURES, "plan"):
                return halt_wins(None)  # Codex's own approval asks (default), or the mode runs nothing (plan)
            # no approval asks in this mode (an absent or unknown mode counts as none) and no key could answer a
            # request: the floor denies, files nothing (Q10 D1); a plain deny, never ``continue: false`` (above)
            note(refused="floor")
            return _deny(floor_text(what, loose))
        if posture in ATTENDED_POSTURES and not effective_away(settings)[0]:
            # approvals on and the operator present: Codex's own approval asks for an agent-config write (measured
            # B5-fix1, MEASURE_away), so the gate holds only while he is away, as Claude Code's does (head ruling 10-18)
            return halt_wins(None)
        hold = min(settings.gate_hold_seconds, GATE_HOLD_CEILING)  # the installed timeout - the margin
        request = queue.file(agent=AGENT, agent_session_id=session, tool_name=tool_name, tool_input=tool_input,
                             cwd=cwd, hold=timedelta(seconds=hold), tier=AGENT_CONFIG_TIER, event_id=event_id,
                             resume=resume_meta_of(event, environ=environ), model=event.get("model"))
    except Exception as exc:
        note(refused="failed")
        return _deny(f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                     "Do not retry it and do not achieve it another way.")
    try:
        notify(settings, f"levain: {AGENT} waits on you up to {hold:g} s: {what} (request {request.id}).",
               cwd=queue.directory, ref=f"request:{request.id}")
        if posture in ATTENDED_POSTURES and still_away is not None:
            # held only because he was away (above): when away ends, the call goes back to Codex's approval
            got, back = hold_for_answer(queue, request, hold + 5.0, tool_input=tool_input, posture=posture,
                                        still_away=still_away)
            if got is None and back:
                if queue.withdraw(request.id, note=f"away mode ended ({back}); Codex's approval asks"):
                    return halt_wins(request.id)  # Codex's approval asks only a session that is not halted
                # not pending any more: an answer may have landed between the wait and the withdraw
                got = _verified(queue.decision(request.id, tool_input=tool_input, posture=posture), request)
                return deliver(got) if got is not None else None
        else:
            got = _verified(queue.await_decision(request.id, hold + 5.0, poll=GATE_POLL, tool_input=tool_input,
                                                 posture=posture), request)
        if got is not None:
            return deliver(got)
        # the hold ends at expires_at (the library's deadline); mark_undecided applies at or after it
        left = (parse_stamp(request.expires_at) - datetime.now(timezone.utc)).total_seconds()
        still = queue.get(request.id)
        if left > 0 and still is not None and still[1] == "pending":
            sleep(min(left + 0.05, GATE_SLEEP_CAP))  # capped: a clock stepped back must not outlive the timeout
        marked = queue.mark_undecided(request.id, halt=tier == "irreversible", tool_input=tool_input,
                                      posture=posture)
        if isinstance(marked.decision, Decision) and _verified(marked.decision, request) is not None:
            return deliver(marked.decision)  # an answer verified in the last moments of the hold
        if marked.state == "undecided" and marked.gap is not None:
            note(refused="undecided", request_id=request.id)
            return undecided_output(tier, what, marked.gap)
        note(refused="deny", request_id=request.id)
        why = marked.reason or "no verified decision"
        return _deny(f"levain: the action was NOT taken: {what} ({why}). Do not retry it and do not achieve it "
                     "another way.")
    except Exception as exc:
        note(refused="failed", request_id=request.id)
        return _deny(f"levain: the gate failed ({type(exc).__name__}), so the action was NOT taken: {what}. "
                     "Do not retry it and do not achieve it another way.")


def gate_failed(event: Any, queue_dir: Path, settings: AdapterSettings, *, home: Path | None,
                environ: Mapping[str, str]) -> dict[str, Any] | None:
    """:func:`main`'s answer when the gate raised before it could answer (the queue would not open): a deny when
    the event lacks a valid ``session_id``, ``tool_name`` or absolute ``cwd`` (as :func:`gate` denies it), when the call is
    gated or cannot be classified, AND for every call while the queue store exists, since a halt on this session
    cannot then be ruled out; ``None`` only for a well-formed ungated call with no store (nothing was ever filed,
    so no session can be halted)."""
    try:
        tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
        session = event.get("session_id")
        if any(not isinstance(v, str) or not v for v in (tool_name, cwd, session)) or not os.path.isabs(cwd):
            tier, what = "irreversible", "this call"
        else:
            protected = protected_paths(queue_dir, settings, home=home, environ=environ, cwd=cwd)
            git_cache: dict[Any, Any] = {}   # one git budget for the call
            tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected,
                              git_cache=git_cache)
            what = _call_what(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected,
                              git_cache=git_cache)
    except Exception:
        tier, what = "irreversible", "this call"
    if tier is None and not os.path.lexists(queue_dir / QUEUE_DB_NAME):
        return None
    return could_not_run(what)


def queue_unresolved(event: Any, *, home: Path | None, environ: Mapping[str, str]) -> dict[str, Any] | None:
    """:func:`main`'s answer for a PreToolUse when no queue directory could be resolved (:func:`default_queue_dir`
    raised: a ``$LEVAIN_HOME`` that cannot be expanded, or no home): with no queue there is no halt to read and no
    settings, so only the levain home is classified (:func:`levain.consent.adapters.claude_code.levain_home_protected`, which
    never raises): a call that targets it or other agent config is denied, a malformed event too (as :func:`gate`
    denies one), and every other call has no decision, so a read is not an outage."""
    try:
        tool_name, tool_input, cwd = event.get("tool_name"), event.get("tool_input") or {}, event.get("cwd")
        session = event.get("session_id")
        if any(not isinstance(v, str) or not v for v in (tool_name, cwd, session)) or not os.path.isabs(cwd):
            return could_not_run("this call")
        protected = tuple(levain_home_protected(home, cwd, environ=environ))
        git_cache: dict[Any, Any] = {}
        if gate_class(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected,
                      git_cache=git_cache) is None:
            return None
        what = _call_what(tool_name, tool_input, cwd, home=home, environ=environ, protected=protected,
                          git_cache=git_cache)
    except Exception:
        what = "this call"
    return could_not_run(what)


def could_not_run(what: str) -> dict[str, Any]:
    """:func:`gate_failed`'s deny (no hard stop: on Codex the latch is the stop); the slot gate's too."""
    return _deny(f"levain: the gate could not run, so the action was NOT taken: {what}. Do not retry it and do "
                 "not achieve it another way.")


# --- the slot gate (F10 S2a): Claude Code's (:func:`levain.consent.adapters.claude_code.slot_gate`), in Codex's
# words. A slot has no local queue: an undecided irreversible call is denied with U-IRR's words, and the halt that
# follows it is the broker's (F10 S2b-2): every PreToolUse first asks ``GATE``
# (:func:`levain.consent.broker_client.gate_check`), before it is classified, and a session halted on such a call,
# or over a limit the operator set (limits apply when the broker has a limits source: the production broker has none
# yet), is denied; GO lets the rest of this gate run (never an allow); the broker down, refusing or out of shape
# denies.

def slot_gate(event: Any, *, home: Path, environ: Mapping[str, str], codex_dir: Path | None = None,
              client: Any = None,
              hold: float = DEFAULT_GATE_HOLD, expected_uid: int | None = None,
              sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """The slot's gate output for one Codex ``PreToolUse`` event, or ``None`` (the call goes on). An event missing
    a required field is denied (:func:`_missing_output`). Then, on EVERY call and before it is classified, the broker
    is asked ``GATE`` (:func:`levain.consent.broker_client.gate_check`; ``client``, default
    :meth:`~levain.consent.broker_client.BrokerClient.from_env`): halted, over a limit or the limits gate failed is
    denied, and the broker down, refusing or out of shape denies every call, ungated ones too. Past a GO, a call
    :func:`gate_class` does not gate (protected paths: none) is :func:`_slot_ungated`'s (``None`` unless a dispatch
    run's grant or queue decides it); a gated call is filed with the broker, held up to ``hold`` for a decision the broker
    verified (:func:`decision_output`), and at the end of the hold denied with :func:`undecided_output`, its gap
    named by the request id. Anything else once the call is gated (a settings file naming the socket variable,
    :func:`queue_socket_planted`; no socket or cap; no broker account, or a socket whose peer is not it; the broker
    unreachable, refusing or out of shape) is :func:`could_not_run`'s deny. ``expected_uid``: as Claude Code's."""
    from levain.consent import broker_client

    if not isinstance(event, Mapping) or event.get("hook_event_name") != GATE_EVENT:
        return None
    missing = _missing_of(event)
    if missing:
        return _missing_output(missing)
    tool_name, tool_input, cwd, session = event["tool_name"], event.get("tool_input", {}), event["cwd"], \
        event["session_id"]
    if tool_input is None:
        tool_input = {}
    # GATE first, before the call is classified (F10 S2b-2): the session's halt and its limits, every call
    try:
        if queue_socket_planted(cwd, home=home, environ=environ, codex_home=codex_dir):
            return could_not_run("this call")
        client = client if client is not None else broker_client.BrokerClient.from_env(environ,
                                                                                      expected_uid=expected_uid)
        tool_use_id = event.get("tool_use_id")
        stopped = broker_client.gate_check(
            client, agent=AGENT, tool_name=tool_name, tool_input=tool_input,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None, sleep=sleep)
    except Exception:  # noqa: BLE001 - fail closed: the broker down, refusing or malformed is a deny
        return could_not_run("this call")
    if stopped is not None:
        return _deny(stopped)
    try:
        git_cache: dict[Any, Any] = {}   # one git budget for the call, its description included
        tier = gate_class(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                          git_cache=git_cache)
        if tier is None:
            return _slot_ungated(event, session, tool_name, tool_input, cwd, home=home, environ=environ,
                                 codex_dir=codex_dir,
                                 git_cache=git_cache, client=client, hold=hold, expected_uid=expected_uid,
                                 sleep=sleep, monotonic=monotonic)
        what = _call_what(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                          git_cache=git_cache)
    except Exception:  # noqa: BLE001 - a call that cannot be classified is denied
        return could_not_run("this call")
    try:
        if queue_socket_planted(cwd, home=home, environ=environ, codex_home=codex_dir):
            return could_not_run(what)
        client = client if client is not None else broker_client.BrokerClient.from_env(environ,
                                                                                      expected_uid=expected_uid)
        tool_use_id = event.get("tool_use_id")
        request = broker_client.gate_request(
            agent=AGENT, session=session, tool_name=tool_name, tool_input=tool_input, cwd=cwd,
            event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
            tier=AGENT_CONFIG_TIER, hold=hold)
        decision = broker_client.file_and_hold(client, request, hold, poll=GATE_POLL, sleep=sleep,
                                               monotonic=monotonic)
    except Exception:  # noqa: BLE001 - fail closed: the broker down, refusing or malformed is a deny
        return could_not_run(what)
    if decision is None:
        return undecided_output(tier, what, request.id)
    return decision_output(decision, SLOT_HOW)


def _slot_ungated(event: Mapping[str, Any], session: str, tool_name: str, tool_input: Any, cwd: str, *,
                  home: Path, environ: Mapping[str, str], codex_dir: Path | None, git_cache: dict[Any, Any],
                  client: Any, hold: float,
                  expected_uid: int | None, sleep: Callable[[float], None],
                  monotonic: Callable[[], float]) -> dict[str, Any] | None:
    """Claude Code's :func:`levain.consent.adapters.claude_code._slot_ungated`, in Codex's words: a file write
    (:data:`PATCH_TOOL`) is sent with its member or with none; ``None`` (the mode decides) for any other call or on
    GO only; any failure, the member that cannot be read included, is :func:`could_not_run`'s deny."""
    from levain.consent import broker_client

    def what() -> str:
        try:
            return _call_what(tool_name, tool_input, cwd, home=home, environ=environ, codex_dir=codex_dir,
                              git_cache=git_cache)
        except Exception:  # noqa: BLE001 - a call that cannot be described is still denied
            return "this call"

    try:
        member = broker_client.grant_member(AGENT, tool_name, tool_input, cwd)
        if member is None and tool_name != PATCH_TOOL:
            return None
        if queue_socket_planted(cwd, home=home, environ=environ, codex_home=codex_dir):
            return could_not_run(what())
        tool_use_id = event.get("tool_use_id")
        got = broker_client.file_ungated(
            client, member=member, agent=AGENT, session=session, tool_name=tool_name, tool_input=tool_input,
            cwd=cwd, event_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None, hold=hold,
            poll=GATE_POLL, expected_uid=expected_uid, sleep=sleep, monotonic=monotonic)
        if got[0] == broker_client.GO:
            return None
        decision, request_id = got
    except Exception:  # noqa: BLE001 - fail closed, as a gated call: the broker down, refusing or malformed
        return could_not_run(what())
    if decision is None:
        return undecided_output("reversible", what(), request_id)
    return decision_output(decision, SLOT_HOW)


def _slot_main(stdin: IO[str], stdout: IO[str], *, home: Path | None, environ: Mapping[str, str],
               expected_uid: int | None = None) -> int:
    """:func:`main` in a slot session: no ConsentQueue, no settings file, no ledger. Always exits 0. A stdin that
    is not a JSON object is :func:`could_not_run`'s deny (Claude Code's :func:`_slot_main` says why)."""
    event: Any = None
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, Mapping):
            raise ValueError("the hook event is not a JSON object")
        out = slot_gate(event, home=home, environ=environ,
                        expected_uid=expected_uid) if event.get("hook_event_name") == GATE_EVENT \
            else None   # a PermissionRequest in a slot: no decision
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow; a gated one a deny
        if not isinstance(event, Mapping) or event.get("hook_event_name") == GATE_EVENT:
            try:
                stdout.write(json.dumps(could_not_run("this call")) + "\n")
                stdout.flush()
            except BaseException:
                pass
        try:
            print(f"levain consent hook: no decision ({type(exc).__name__}: {exc})"[:500], file=sys.stderr)
        except Exception:
            pass
    return 0


def _queue_for(queue_dir: Path, settings: AdapterSettings, *, home: Path) -> ConsentQueue:
    if settings.cockpit_data_root:
        root = Path(settings.cockpit_data_root)
        source = authority_source(root / "cockpit.db", anchor=anchor_path(root))
    else:
        source = authority_source(queue_dir, anchor=None)  # no authority: nothing can verify
    return ConsentQueue(queue_dir, authority=source, home=home)


def main(argv: list[str] | None = None, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """The hook entry point, for both events the one script is wired to: ``PermissionRequest`` (:func:`decide`)
    and ``PreToolUse`` (:func:`gate`). Always exits 0 (any other exit fails open on Codex). A PermissionRequest
    prints a decision only when the core verified one; a PreToolUse whose gate raised prints :func:`gate_failed`. In
    a slot session (:func:`levain.consent.broker_client.in_slot`) it is :func:`_slot_main` instead, and opens no
    queue."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    event: Any = None
    queue_dir: Path | None = None
    settings = AdapterSettings()   # set before the try: a raising load_settings leaves it, so gate_failed still
    # classifies the call (a store write denied, an ordinary call no decision), never with None
    # H4: the one read of the process environment and home; everything below is handed them
    environ: Mapping[str, str] = os.environ
    home = _bound_home()
    if in_slot(environ):
        return _slot_main(stdin, stdout, home=home, environ=environ)
    try:
        parser = argparse.ArgumentParser(prog="levain-consent-codex")
        parser.add_argument("--queue-dir", default=None)
        parser.add_argument("--max-hold", type=float, default=DEFAULT_MAX_HOLD)
        args = parser.parse_args(argv)
        queue_dir = Path(args.queue_dir) if args.queue_dir else queue_dir_for(environ)
        if home is None:
            raise RuntimeError("could not determine the home directory")
        settings = load_settings(queue_dir)
        queue = _queue_for(queue_dir, settings, home=home)   # one queue: the away mark, the hold and every recheck
        settings = with_away_mark(settings, queue_dir, AGENT, queue=queue)
        event = json.loads(stdin.read())
        if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT:
            out = gate(event, queue, settings, home=home, environ=environ)
        else:
            out = decide(event, queue, settings, environ=environ, home=home, max_hold=args.max_hold,
                         still_away=still_away_from(queue_dir / SETTINGS_NAME, settings, queue=queue))
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    except BaseException as exc:  # noqa: BLE001 - every failure is no decision, never allow
        try:
            if event is None:
                event = json.loads(stdin.read())   # not read yet: the failure came first
            out = None
            if isinstance(event, Mapping) and event.get("hook_event_name") == GATE_EVENT:
                out = gate_failed(event, queue_dir, settings, home=home, environ=environ) \
                    if queue_dir is not None else queue_unresolved(event, home=home, environ=environ)
            if out is not None:
                stdout.write(json.dumps(out) + "\n")
                stdout.flush()
        except BaseException:
            pass
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
    Codex's "Hooks need review" screen: this per-workspace install writes no trust (``levain init`` / ``update``
    write it only for the machine-wide hooks they render, :func:`write_hook_trust`). Refuses (``UntestedAgentVersion``) when the
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
    return _app_server_call(ws, "hooks/list", {"cwds": [str(ws)]}, home=home, codex_bin=codex_bin, timeout=timeout)


def _app_server_call(ws: Path, method: str, params: Mapping[str, Any], *, home: Path | None, codex_bin: str,
                     timeout: float) -> dict[str, Any]:
    """The result of one ``method`` asked of a ``codex app-server`` started in ``ws`` (stdio JSON-RPC)."""
    from levain.launch import child_env
    env = child_env()
    if home is not None:
        env["CODEX_HOME"] = str(home)
    msgs = [{"id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "levain-doctor", "version": "1"}, "capabilities": None}},
            {"method": "initialized"},
            {"id": 2, "method": method, "params": dict(params)}]
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
                    if not isinstance(msg.get("result"), dict):
                        raise RuntimeError(f"{method} failed: {msg.get('error')}")
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
    raise RuntimeError(f"codex app-server gave no {method} answer in time")


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
    import tomllib   # 3.11+, imported here so root's 3.9 can import this module for patch_targets; root never calls this function
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


# --- trust that levain writes (Q14 B5): Codex runs a hooks.json hook only when CODEX_HOME's config.toml holds
# its hash under ``hooks.state."<key>".trusted_hash``. An untrusted or modified hook is skipped with NO error or
# warning (runs3-codex RUN 2), so a gate whose trust is missing or stale is a gate that silently does not run.

def _event_label(event: str) -> str:
    """Codex's snake_case label of a hooks.json event name (``PreToolUse`` -> ``pre_tool_use``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def hook_trusted_hash(event: str, group: Mapping[str, Any], handler: Mapping[str, Any]) -> str:
    """The ``trusted_hash`` Codex 0.162.1 expects for one command handler (runs3-codex RUN 2, from
    ``hooks/src/engine/discovery.rs`` ``hook_hash`` and ``config/src/fingerprint.rs``): SHA-256 of the compact,
    key-sorted JSON of the normalized identity: the event's label, the group's matcher when set, and the one
    handler as ``type``, ``command``, ``timeout`` (600 when absent, at least 1), ``async`` (false when absent),
    ``statusMessage`` only when set. Raises ``ValueError`` for a handler it cannot hash (not a command handler,
    or a field Codex would hash that this formula does not cover)."""
    import hashlib

    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        raise ValueError("only a command handler with a string command is hashed")
    unknown = set(handler) - {"type", "command", "timeout", "async", "statusMessage"}
    if unknown:
        raise ValueError(f"handler fields this formula does not cover: {sorted(unknown)}")
    timeout = handler.get("timeout", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValueError("timeout must be an integer")
    norm: dict[str, Any] = {"type": "command", "command": handler["command"], "timeout": max(timeout, 1),
                            "async": bool(handler.get("async", False))}
    if handler.get("statusMessage") is not None:
        norm["statusMessage"] = handler["statusMessage"]
    ident: dict[str, Any] = {"event_name": _event_label(event), "hooks": [norm]}
    if group.get("matcher") is not None:
        ident["matcher"] = group["matcher"]
    text = json.dumps(ident, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def hook_identities(doc: Any) -> list[tuple[str, int, int, str]]:
    """``(event, group index, handler index, trusted_hash)`` of every command handler in a hooks.json document
    (Codex's key is ``<hooks.json path>:<event label>:<group>:<handler>``). Raises ``ValueError`` when the
    document is not a hooks.json shape or a handler cannot be hashed."""
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError("no 'hooks' object")
    out = []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} is not a list")
        for g, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                raise ValueError(f"hooks.{event}[{g}] has no handler list")
            for h, handler in enumerate(handlers):
                if not isinstance(handler, dict):
                    raise ValueError(f"hooks.{event}[{g}].hooks[{h}] is not an object")
                out.append((event, g, h, hook_trusted_hash(event, group, handler)))
    return out


@dataclass(frozen=True)
class HookTrust:
    """One hook's trust after :func:`write_hook_trust`: its key, event, the formula's hash, and what Codex's own
    ``hooks/list`` said afterwards (``trusted`` only when Codex reports it trusted AND enabled AND its current
    hash is the formula's)."""
    key: str
    event: str
    trusted_hash: str
    status: str


def _listed_hooks(chome: Path, hooks_path: Path, *, codex_bin: str, timeout: float) -> dict[str, dict[str, Any]]:
    """Codex's own ``hooks/list`` entries for the hooks in CODEX_HOME's ``hooks_path``, keyed by the key's
    ``:<event label>:<group>:<handler>`` suffix. Raises as :func:`_app_server_call` does."""
    real = os.path.realpath(hooks_path)
    got = _hooks_list(chome, home=chome, codex_bin=codex_bin, timeout=timeout)
    found: dict[str, dict[str, Any]] = {}
    for entry in got.get("data", []) if isinstance(got, dict) else []:
        for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if isinstance(hook, dict) and isinstance(hook.get("key"), str) and isinstance(
                    hook.get("sourcePath"), str) and os.path.realpath(hook["sourcePath"]) == real:
                found[hook["key"].rpartition(str(hook["sourcePath"]))[2]] = hook
    return found


# The gate's script as the package ships it: Codex's trust covers the entry's command, so levain checks the bytes.
GATE_SCRIPT_SOURCE = (Path(__file__).resolve().parents[2] / "templates" / "adapters" / "codex" / "activation"
                      / "hooks" / "consent_codex.py")


def gate_script_intact(script: Path | None) -> bool:
    """True when ``script`` is a regular file holding exactly the package's own gate script
    (:data:`GATE_SCRIPT_SOURCE`)."""
    if script is None:
        return False
    try:
        return script.is_file() and not script.is_symlink() and script.read_bytes() == GATE_SCRIPT_SOURCE.read_bytes()
    except OSError:
        return False


def _command_script(handler: Mapping[str, Any]) -> Path | None:
    """The script a hook command runs: its last token (the position every levain reader takes it from)."""
    try:
        tokens = shlex.split(handler.get("command") or "")
    except ValueError:
        return None
    return Path(tokens[-1]) if tokens else None


def _gate_entries(doc: Any) -> list[tuple[int, int, Mapping[str, Any], Mapping[str, Any]]]:
    """``(group index, handler index, group, handler)`` of every PreToolUse handler in a hooks.json document."""
    groups = (doc.get("hooks") or {}).get(GATE_EVENT) if isinstance(doc, dict) and isinstance(
        doc.get("hooks"), dict) else None
    out = []
    for g, group in enumerate(groups if isinstance(groups, list) else []):
        handlers = group.get("hooks") if isinstance(group, dict) else None
        for h, handler in enumerate(handlers if isinstance(handlers, list) else []):
            if isinstance(handler, dict):
                out.append((g, h, group, handler))
    return out


def gate_entry_differences(group: Mapping[str, Any], handler: Mapping[str, Any], script: Path,
                           python: str | None = None) -> list[str]:
    """How a gate entry differs from levain's render (empty when it does not): matcher ``*``, a handler of exactly
    ``type`` command, ``timeout`` :data:`GATE_TIMEOUT` (no ``async``, ``statusMessage`` or other field) and the
    command ``<python> -EP -m levain.hookrun <script>``, where ``<python>`` is ``python`` (default: this levain's
    own, ``sys.executable``), absolute and equal. A narrowed matcher, an async handler, a short
    timeout or another interpreter keeps Codex's trust once re-trusted and still lets the call run (L3 r1 codex 3,
    r2 codex 3)."""
    python = os.path.abspath(python or sys.executable)
    out = []
    if group.get("matcher") != "*":
        out.append(f"matcher {group.get('matcher')!r} (levain's is '*')")
    extra = sorted(set(handler) - {"type", "command", "timeout"})
    if extra:
        out.append(f"field(s) {', '.join(extra)} (levain's has none)")
    if handler.get("type") != "command":
        out.append(f"type {handler.get('type')!r}")
    if handler.get("timeout") != GATE_TIMEOUT:
        out.append(f"timeout {handler.get('timeout')!r} (levain's is {GATE_TIMEOUT})")
    try:
        tokens = shlex.split(handler.get("command") or "")
    except ValueError:
        tokens = []
    if len(tokens) != 5 or tokens[1:4] != [HOOK_FLAGS, *HOOKRUN] or tokens[4] != str(script):
        out.append("command (not levain's '<python> -EP -m levain.hookrun <script>')")
    elif tokens[0] != python:
        out.append(f"interpreter {tokens[0]!r} (levain's is {python!r})")
    return out


def write_hook_trust(hooks_path: Path, expected_text: str, *, home: Path | None = None, codex_bin: str | None = None,
                     timeout: float = TRUST_TIMEOUT) -> tuple[list[HookTrust], str | None]:
    """Trust every hook in CODEX_HOME's ``hooks_path``, and only while its bytes are ``expected_text`` (the
    render levain just wrote: every handler in it is levain's), then PROBE that Codex runs them. The hash is
    :func:`hook_trusted_hash` (never read back from Codex); Codex's own ``hooks/list`` supplies the key it files
    the hook under, and a hook whose ``currentHash`` there differs from the formula is not trusted (the formula
    no longer matches this Codex). The write goes through Codex's ``config/batchWrite`` (``hooks.state``, upsert),
    so Codex edits its own config.toml; then ``hooks/list`` is asked again. Returns ``(per-hook results, None)``,
    or ``([], why)`` when nothing was written."""
    try:
        if hooks_path.read_text(encoding="utf-8") != expected_text:
            return [], f"{hooks_path} is not the file levain rendered, so no hook in it is trusted"
        doc = json.loads(expected_text)
        mine = hook_identities(doc)
    except (OSError, ValueError) as exc:
        return [], f"{hooks_path} could not be read as levain's hooks ({exc})"
    # Codex's trust covers the gate's command, never its script's bytes: the gate is trusted only while the script
    # that command runs is the package's own (L3 r1 codex 1)
    refused = {(GATE_EVENT, g, h) for g, h, _group, handler in _gate_entries(doc)
               if not gate_script_intact(_command_script(handler))}
    codex_bin = codex_bin or shutil.which("codex")
    if not codex_bin:
        return [], "codex is not on PATH, so its hook trust could not be written"
    chome = Path(home) if home is not None else codex_home()
    real = os.path.realpath(hooks_path)

    def listed() -> dict[str, dict[str, Any]]:
        return _listed_hooks(chome, hooks_path, codex_bin=codex_bin, timeout=timeout)

    try:
        before = listed()
        updates: dict[str, dict[str, str]] = {}
        for event, g, h, digest in mine:
            hook = before.get(f":{_event_label(event)}:{g}:{h}")
            if hook is not None and hook.get("currentHash") == digest and (event, g, h) not in refused:
                updates[hook["key"]] = {"trusted_hash": digest}
        if updates:
            _app_server_call(chome, "config/batchWrite", {
                "edits": [{"keyPath": "hooks.state", "value": updates, "mergeStrategy": "upsert"}],
                "filePath": None, "expectedVersion": None, "reloadUserConfig": True},
                home=chome, codex_bin=codex_bin, timeout=timeout)
        after = listed()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return [], f"could not ask codex: {exc}"[:300]
    out = []
    for event, g, h, digest in mine:
        hook = after.get(f":{_event_label(event)}:{g}:{h}")
        if (event, g, h) in refused:
            status = "script_changed"
        elif hook is None:
            status = "unlisted"
        elif hook.get("currentHash") != digest:
            status = "formula_mismatch"
        elif hook.get("trustStatus") == "trusted" and hook.get("enabled") is True:
            status = "trusted"
        else:
            status = {"untrusted": "untrusted", "modified": "changed"}.get(hook.get("trustStatus"), "unknown") \
                if hook.get("enabled") is not False else "disabled"
        out.append(HookTrust(hook["key"] if hook is not None else f"{real}:{_event_label(event)}:{g}:{h}", event,
                             digest, status))
    return out, None


def _trust_states(home: Path | None) -> dict[str, Any]:
    """CODEX_HOME's ``hooks.state`` table (empty when config.toml is missing). Raises what ``tomllib.load`` raises
    (``OSError`` / ``tomllib.TOMLDecodeError``) when it cannot be read; ``tomllib`` is imported in this function."""
    import tomllib   # 3.11+, imported here so root's 3.9 can import this module for patch_targets; root never calls this function
    chome = Path(home) if home is not None else codex_home()
    try:
        with open(chome / "config.toml", "rb") as fh:
            states = tomllib.load(fh).get("hooks", {}).get("state", {})
    except FileNotFoundError:
        return {}
    return states if isinstance(states, dict) else {}


def _held_state(states: Mapping[str, Any], hooks_path: Path, suffix: str) -> tuple[str, dict[str, Any] | None]:
    """(key, state) for a hook whose key ends ``suffix``, under the hooks.json path as given or its real path."""
    keys = [f"{hooks_path}{suffix}", f"{os.path.realpath(hooks_path)}{suffix}"]
    for key in keys:
        if isinstance(states.get(key), dict):
            return key, states[key]
    return keys[0], None


def untrusted_hooks(hooks_path: Path, expected_text: str, *, home: Path | None = None) -> list[str] | None:
    """Without starting Codex: the events of the hooks in ``hooks_path`` that config.toml does not trust exactly
    as :func:`hook_trusted_hash` computes them (or that it disables); ``None`` when the file is not
    ``expected_text`` or a file cannot be read."""
    import tomllib   # 3.11+, imported here so root's 3.9 can import this module for patch_targets; root never calls this function
    try:
        if hooks_path.read_text(encoding="utf-8") != expected_text:
            return None
        mine = hook_identities(json.loads(expected_text))
        states = _trust_states(home)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    out = []
    for event, g, h, digest in mine:
        _key, state = _held_state(states, hooks_path, f":{_event_label(event)}:{g}:{h}")
        if state is None or state.get("trusted_hash") != digest or state.get("enabled") is False:
            out.append(event)
    return out


def gate_trust(hooks_path: Path, script: Path, *, home: Path | None = None, codex_bin: str | None = None,
               timeout: float = TRUST_TIMEOUT, python: str | None = None) -> TrustStatus:
    """Whether Codex will run levain's PreToolUse gate in CODEX_HOME's ``hooks_path`` (the entry running
    ``script``), by Codex's own verdict: ``trusted`` only when the entry is exactly levain's render
    (:func:`gate_entry_differences`, its interpreter ``python``), ``script`` is the package's own
    (:func:`gate_script_intact`) and Codex's ``hooks/list`` reports it trusted, enabled and with ``currentHash``
    equal to :func:`hook_trusted_hash`.
    Otherwise ``absent`` (no gate entry runs ``script``), ``changed`` (the entry differs from levain's render,
    or Codex lists it modified), ``script_changed``, ``untrusted``, ``disabled``, ``formula_mismatch`` (Codex hashes
    the entry differently: this Codex is not the one the formula was measured on), ``unlisted``, ``unknown``, or,
    when Codex cannot be asked, ``unconfirmed`` (config.toml holds the formula's hash, which is not Codex's word)
    or what config.toml says (``untrusted`` / ``changed`` / ``disabled``). ``trusted_hash`` is config.toml's,
    ``current_hash`` the formula's."""
    try:
        doc = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return TrustStatus("unknown", f"{hooks_path} could not be read ({exc})")
    found = None
    for g, h, group, handler in _gate_entries(doc):
        if _is_ours(handler, "", script):
            found = (g, h, group, handler)
    if found is None:
        return TrustStatus("absent", f"no levain PreToolUse gate in {hooks_path}")
    g, h, group, handler = found
    diffs = gate_entry_differences(group, handler, script, python)
    if diffs:
        return TrustStatus("changed", "the gate entry differs from levain's render: " + "; ".join(diffs))
    digest = hook_trusted_hash(GATE_EVENT, group, handler)
    intact = gate_script_intact(script)
    if not intact:
        return TrustStatus("script_changed", f"{script} is not the package's gate script (Codex's trust covers the "
                           "command, not the script)", None, None, digest, False)
    why = ""
    states: dict[str, Any] | None
    import tomllib   # 3.11+, imported here so root's 3.9 can import this module for patch_targets; root never calls this function
    try:
        states = _trust_states(home)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        states, why = None, f"CODEX_HOME's config.toml could not be read ({exc})"
    suffix = f":{_event_label(GATE_EVENT)}:{g}:{h}"
    key, state = _held_state(states or {}, hooks_path, suffix)
    held = state.get("trusted_hash") if state is not None else None
    if states is None:
        static = TrustStatus("unknown", why, key, None, digest, True)
    elif state is not None and state.get("enabled") is False:
        static = TrustStatus("disabled", "config.toml disables it", key, held, digest, True)
    elif held is None:
        static = TrustStatus("untrusted", "config.toml holds no trusted_hash for it", key, None, digest, True)
    elif held != digest:
        static = TrustStatus("changed", "config.toml's trusted_hash is not the entry's", key, held, digest, True)
    else:
        static = TrustStatus("unconfirmed", "config.toml holds the entry's hash", key, held, digest, True)
    codex_bin = codex_bin or shutil.which("codex")
    chome = Path(home) if home is not None else codex_home()
    try:
        if not codex_bin:
            raise RuntimeError("codex is not on PATH")
        hook = _listed_hooks(chome, hooks_path, codex_bin=codex_bin, timeout=timeout).get(suffix)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return TrustStatus(static.status, f"{static.detail}; Codex could not be asked ({exc})"[:400], key, held,
                           digest, True)
    if hook is None:
        return TrustStatus("unlisted", "Codex's hooks/list does not list the gate", key, held, digest, True)
    got, enabled, current = hook.get("trustStatus"), hook.get("enabled"), hook.get("currentHash")
    detail = f"Codex's hooks/list: trustStatus {got!r}, enabled {enabled!r}"
    if current != digest:
        return TrustStatus("formula_mismatch", f"{detail}, currentHash {current!r} is not levain's {digest!r}",
                           key, held, digest, True)
    if got == "trusted" and enabled is True:
        return TrustStatus("trusted", detail, key, held, digest, True)
    if enabled is False:
        return TrustStatus("disabled", detail, key, held, digest, True)
    return TrustStatus({"untrusted": "untrusted", "modified": "changed"}.get(got, "unknown"), detail, key, held,
                       digest, True)


if __name__ == "__main__":
    sys.exit(main())
