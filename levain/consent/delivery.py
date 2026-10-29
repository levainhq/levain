"""levain.consent.delivery — a late ruling delivered back to the session it rules on (DESIGN §3.6, seat ruling R-i).

A ruling (:meth:`~levain.consent.queue.ConsentQueue.answer_late`) changes the queue; this module gets its text to the
agent. The text reaches a session ONCE: through the session's own next hook
(:meth:`~levain.consent.queue.ConsentQueue.claim_rulings_for`), or through a resume started here, which leases the text
(:meth:`~levain.consent.queue.ConsentQueue.lease_rulings_for`) and counts it delivered only once the resume is
confirmed: still running, or exited 0, :data:`RESUME_SETTLE` seconds after it started. A resume that could not start
or exited non-zero within that time is recorded ``failed`` and the text stays deliverable (by the session's next hook,
or ``levain consent deliver``); one this process never settled (it died in between) stays ``unconfirmed``, never
counted delivered and never retried, since it may have run. Where the text goes is read from the session's state at
the moment of delivery, never assumed:

  - Claude Code, from ``claude agents --json`` run under the session's own config directory (a listed ``sessionId``
    is live, with ``status`` busy or idle and ``kind`` background or interactive; a running ``claude -p`` is listed
    too, as interactive):
      * listed and busy: its next hook carries the text (nothing is started here);
      * listed, idle, interactive (a terminal holds it): its next hook; the ping says it lands at its next step;
      * listed, idle, background: ``claude stop`` then ``claude --bg --resume`` under the same id, ONLY once the run
        owed for it shows the stop keeps the id and the conversation and forks nothing (:data:`STOP_THEN_RESUME_RUN`);
        until then, its next hook;
      * not listed, and the process recorded at filing gone (no process of that pid, or one that started at another
        time): ended. It is resumed with the text as its prompt, in the directory it was started in and its
        permission mode (recorded at filing, :meth:`~levain.consent.queue.ConsentQueue.resume_meta`):
        ``claude -p --resume=<id>`` when it was a ``-p`` run, else ``claude --bg --resume=<id>``.
  - Codex has no listing: a session whose ``codex exec`` process (its pid recorded at filing) is gone is ended and
    resumed with ``codex exec resume``; one whose process still runs, or with no pid recorded, gets its next hook.

A session is never resumed while it may be running (a resume of a running session starts a copy: two agents on one
tree): a listing that fails, no process recorded, a recorded process still alive, or no resume record all leave the
text to the next hook. Nor is it resumed on inputs that cannot be trusted to bring back the session that was filed
(:func:`resume_refusal`): a session id that is not a UUID, a start directory that is not an existing absolute one,
no transcript for that config directory, directory and id, no levain gate in the settings the resume would load, a
permission mode that is not a known one, or an original command line that carried a flag the resume cannot restore
(its settings, MCP config, tool lists, added directories, system prompt): resumed without it, the session would run
with fewer constraints than it was started with.

Known edges (stated, not built against):
  - a session resumed by someone else between the listing and the launch (about a second) gets a copy.
  - a ruling left to a busy session's next hook waits if that session then ends with no further tool call;
    :func:`deliver_undelivered` (``levain consent deliver``) later finds it ended and resumes it.
  - a resume that starts, runs past :data:`RESUME_SETTLE` and then fails (a credential refused late) counts as
    delivered; one that did its work and still exited non-zero inside it is retried, and its text arrives twice.
  - the transcript is looked up at the directory name Claude Code derives from the start directory (every character
    outside ``A-Za-z0-9`` as ``-``); a start directory long enough that Claude Code shortens that name is not resumed.
  - a ruling on a subagent's call is claimed under the subagent's session: a resume delivers it to the parent session;
    the parent's own hooks do not.
  - Codex's permission mode is not restored (its hook's ``permission_mode`` is not its sandbox): a resume runs under
    the user's Codex config.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from levain.consent.queue import ConsentQueue, Ruling, gap_label

__all__ = ["QUOTED_NOTE", "RESUME_SETTLE", "STOP_THEN_RESUME_RUN", "Delivery", "agent_notify", "base_session",
           "claude_listing", "deliver", "deliver_undelivered", "gate_refusal", "option_names", "plan",
           "process_identity", "process_running", "resume_refusal", "route", "session_named_running", "session_ok",
           "unrestored_flags"]

#: The stop-then-resume path for an IDLE background Claude Code session (R-i). Off until the run owed in B7 shows
#: ``claude stop`` keeps the session id and its conversation and ``claude --bg --resume`` then continues it with no
#: fork; while off, such a session gets the ruling at its next step, and the delivery says so.
STOP_THEN_RESUME_RUN = False
# how long a listing may take
LISTING_TIMEOUT = 30.0
# how long a stopped session may stay listed before the stop is judged not to have cleared it
STOP_SETTLE = 20.0
#: how long a started resume is watched before it counts as delivered: one that exits non-zero inside it (an unknown
#: session, a refused credential, a bad argument) failed and its text stays deliverable
RESUME_SETTLE = 10.0
_CLAUDE = "claude_code"
_CODEX = "codex"
# the permission modes a resumed Claude Code session is started in
_MODES = frozenset({"default", "acceptEdits", "plan", "bypassPermissions", "auto", "dontAsk"})
# The options of an original Claude Code command line a resume may run without: each changes no tool set,
# permission, setting, budget or confinement (or a resume without it is the stricter session), and a resume carries
# the permission mode itself. Any other option refuses the resume: an allowlist, so an option a later Claude Code
# adds is refused until it is judged here. It was a denylist until 1019+1 measured Claude Code 2.1.303's options
# against it and found resumes that dropped --restricted (the code-running tools came back), --agent,
# --disable-slash-commands, --max-budget-usd, --safe-mode and --bare (each also as ``--flag=value``). A run that is
# not persisted (--no-session-persistence, Codex's --ephemeral) leaves no record of what it ran under, so a resume
# would read an older one: those refuse too.
_CLAUDE_NEUTRAL = frozenset({
    "-p", "--print", "--bg", "-r", "--resume", "-c", "--continue", "--session-id", "--fork-session",
    "--permission-mode", "--allow-dangerously-skip-permissions", "--model", "--fallback-model", "--effort",
    "--output-format", "--input-format", "--json-schema", "--include-partial-messages", "--include-hook-events",
    "--replay-user-messages", "--forward-subagent-text", "--verbose", "-d", "--debug", "--debug-file", "-n",
    "--name", "--prompt-suggestions", "--ax-screen-reader", "--ide", "--chrome", "--brief",
    "--exclude-dynamic-system-prompt-sections", "--autocompact", "--remote-control",
    "--remote-control-session-name-prefix", "-h", "--help", "-v", "--version",
})
# the one --dangerously-* flag a resume restores: bypassPermissions IS that flag's mode
_BYPASS_FLAG = "--dangerously-skip-permissions"
# Codex: `codex exec resume` takes no -s and runs under config.toml's sandbox_mode, not the original's [measured:
# 1019+1, codex-cli 0.162.1: an exec started -s read-only was resumed workspace-write]. So the resume restores the
# sandbox the session last ran under, read from Codex's own rollout (its last turn_context's sandbox_policy, never
# the command line as ps shows it, where a prompt's words cannot be told from options), with -s and, for
# workspace-write, its recorded fields (codex_sandbox_args); --skip-git-repo-check and --strict-config are carried; and any other option on the original command line refuses the resume unless it
# is on this list of ones that change no confinement, approval or rules (fail closed: an option a later Codex adds
# is refused until it is judged here). A prompt word that looks like an option refuses the resume, except
# --skip-git-repo-check, which then skips a check the original already passed by running in that directory.
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
_CODEX_CARRIED = ("--skip-git-repo-check", "--strict-config")
_CODEX_NEUTRAL = ("--json", "-o", "--output-last-message", "-m", "--model", "-i", "--image", "--color",
                  "--output-schema", "--no-alt-screen", "-s", "--sandbox")
# a session id: Claude Code's and Codex's are UUIDs; a subagent's key adds ``#agent:<id>``
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# the agent's own credentials, which a resumed session needs as its original had them; the allowlisted child
# environment drops every credential-shaped name, so these are carried by name
_AGENT_AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "OPENAI_API_KEY", "CODEX_API_KEY")
_CONFIG_VAR = {_CLAUDE: "CLAUDE_CONFIG_DIR", _CODEX: "CODEX_HOME"}
# what follows the §3.6 lines in a resume's prompt: the action in them is quoted from the agent's own filing
QUOTED_NOTE = ("(Each «…» above is the action quoted as the agent filed it in its request, not the operator's "
               "words.)")


@dataclass(frozen=True)
class Delivery:
    """What became of one ruling's delivery. ``route``: ``hook`` (left to the session's next hook, ``reason`` says
    why), ``resumed`` (a resume started and was confirmed, ``argv`` in ``cwd``), ``failed`` (the resume did not
    start or exited at once: ``reason``; the text stays deliverable), ``unconfirmed`` (a resume was started for it
    and never settled: it may have run, so it is not retried), ``delivered`` (already delivered, by a hook or an
    earlier resume), ``none`` (nothing to deliver: not a ruling that verifies). ``rulings`` names every request whose
    text a resume carried (a session's undelivered rulings travel together)."""
    request_id: str
    agent: str
    session: str
    route: str
    reason: str
    argv: tuple[str, ...] = ()
    cwd: str | None = None
    rulings: tuple[str, ...] = ()
    gap: int | None = None
    ruling: str | None = None


def base_session(session: str) -> str:
    """The resumable session of a hook's session key (a subagent's key is ``<session>#agent:<id>``)."""
    return session.partition("#agent:")[0]


def session_ok(session: Any) -> bool:
    """True when ``session`` is a session key a resume may name: a UUID, optionally ``#agent:<id>``."""
    if not isinstance(session, str):
        return False
    base, sep, agent_id = session.partition("#agent:")
    return _UUID.fullmatch(base) is not None and (not sep or _AGENT_ID.fullmatch(agent_id) is not None)


def _env(agent: str, meta: Mapping[str, Any]) -> dict[str, str]:
    """The environment a listing or a resume runs in: the allowlisted child environment, the agent's credentials and
    network settings, and the session's own config directory as recorded at filing (unset when it ran under the
    default)."""
    from levain.launch import NETWORK, child_env

    env = child_env(*_AGENT_AUTH, *NETWORK, seat="attended")   # a session of the operator's: the band's D4 marker
    var = _CONFIG_VAR.get(agent)
    if var is not None and "config_dir" in meta:
        if isinstance(meta["config_dir"], str) and meta["config_dir"]:
            env[var] = meta["config_dir"]
        else:
            env.pop(var, None)
    return env


def _exe(name: str, env: Mapping[str, str]) -> str | None:
    found = shutil.which(name, path=env.get("PATH", os.defpath))
    return found if found and os.path.isabs(found) else None


def claude_listing(meta: Mapping[str, Any]) -> dict[str, dict[str, Any]] | None:
    """``claude agents --json`` under the session's config directory, keyed by ``sessionId``; ``None`` when it cannot
    be run or read whole (no listing is ever read as "nothing is live")."""
    env = _env(_CLAUDE, meta)
    exe = _exe("claude", env)
    if exe is None:
        return None
    try:
        got = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=LISTING_TIMEOUT,
                             env=env, stdin=subprocess.DEVNULL, cwd=str(Path.home()))
        rows = json.loads(got.stdout) if got.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("sessionId"), str):
            return None
        out[row["sessionId"]] = row
    return out


def process_identity(pid: Any) -> tuple[str, str] | None:
    """The OS's start time and command line of process ``pid`` (``ps -o lstart= -o args=``), or ``None`` when none
    can be read. The start time tells a pid's process from a later one that reused the pid."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        got = subprocess.run(["ps", "-ww", "-o", "lstart=", "-o", "args=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL, env={"PATH": "/bin:/usr/bin",
                                                                                  "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    line = got.stdout.strip() if got.returncode == 0 else ""
    m = re.fullmatch(r"(\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(.*)", line, re.S)
    return None if m is None else (" ".join(m.group(1).split()), m.group(2))


def process_running(pid: Any, started: Any = None) -> bool | None:
    """True when process ``pid`` exists (whatever program it is now), False when none does, or when ``started`` (its
    start time recorded at filing, :func:`process_identity`) is given and the process there now started at another
    time; ``None`` when that cannot be told."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return None
    if isinstance(started, str) and started:
        now = process_identity(pid)
        if now is not None and now[0] != " ".join(started.split()):
            return False                                 # the pid was reused: the recorded process is gone
    return True


def _transcript(meta: Mapping[str, Any], session: str) -> Path:
    """Where Claude Code keeps the session's transcript: ``<config>/projects/<start dir, every character outside
    A-Za-z0-9 as ->/<id>.jsonl``."""
    config = meta.get("config_dir") if isinstance(meta.get("config_dir"), str) else None
    base = Path(config) if config else Path.home() / ".claude"
    return base / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(meta["cwd"])) / f"{session}.jsonl"


def _settings(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def gate_refusal(cwd: str, config_dir: str | None) -> str | None:
    """Why a Claude Code session resumed in ``cwd`` under ``config_dir`` would not run levain's gate, or ``None``:
    the project's or the user's settings must hold the gate entry in the exact shape ``install`` writes (a
    ``PreToolUse`` group with matcher ``*`` whose handler is ``type`` command, the command
    ``<python> -EP -m levain.hookrun <...>/consent_claude_code.py`` on an existing script, ``onFailure`` block and
    ``timeout`` :data:`GATE_TIMEOUT`, no other field), and no settings file the resume loads may set
    ``disableAllHooks``. A lookalike entry (another matcher, a non-blocking one) gates less than every call, so the
    reason names the field that differs."""
    from levain.consent.adapters.claude_code import GATE_EVENT, GATE_TIMEOUT, HOOK_FLAGS, HOOKRUN
    from levain.install import CONSENT_HOOK_SCRIPTS

    from levain.host_guard import cc_project_settings_files

    user = Path(config_dir) if config_dir else Path.home() / ".claude"
    # the project files Claude Code loads for a session in cwd: settings.local.json at the git root and beside
    # settings.json, and settings.json from cwd itself (levain wires the gate in one of them, per workspace:
    # host_guard.cc_levain_hooks_file)
    local, shared = cc_project_settings_files(cwd)
    project = Path(cwd) / ".claude"
    hook_files = [*local, shared, user / "settings.json"]
    files = {f: _settings(f) for f in dict.fromkeys([*hook_files, project / "settings.local.json"])}
    off = [str(f) for f, doc in files.items() if doc.get("disableAllHooks") is True]
    if off:
        return f"{off[0]} turns every hook off"
    want = {"type": "command", "onFailure": "block", "timeout": GATE_TIMEOUT}
    lookalike = None
    for f in dict.fromkeys(hook_files):
        hooks = files[f].get("hooks")
        groups = hooks.get(GATE_EVENT) if isinstance(hooks, dict) else None
        for group in groups if isinstance(groups, list) else ():
            for h in (group.get("hooks") if isinstance(group, dict) else None) or ():
                try:
                    tokens = shlex.split(h["command"]) if isinstance(h, dict) and isinstance(h.get("command"),
                                                                                            str) else []
                except ValueError:
                    continue
                if not (len(tokens) == 5 and tokens[1:4] == [HOOK_FLAGS, *HOOKRUN]
                        and os.path.basename(tokens[4]) == CONSENT_HOOK_SCRIPTS["claude-code"]
                        and os.path.isfile(tokens[4])):
                    continue
                bad = ([] if group.get("matcher") == "*" else [f"matcher {group.get('matcher')!r}"]) + [
                    f"{k} {h.get(k)!r}" for k, v in want.items() if h.get(k) != v or type(h.get(k)) is not type(v)]
                bad += [f"extra field {k!r}" for k in h if k not in ("command", *want)]
                if not bad:
                    return None
                lookalike = lookalike or f"the gate entry in {f} is not the one levain installs ({', '.join(bad)})"
    return lookalike or "no levain gate is installed in the settings it would load"


def option_names(argv_text: str) -> str:
    """The option names of a command line as ``ps`` shows it (each token starting with ``-``, cut at ``=``), never
    their values or the prompt: what :func:`unrestored_flags` reads, and all that is recorded of the command line
    (a value on it can be a secret). The executable stands first, so a record is never empty."""
    tokens = argv_text.split()
    return " ".join([*tokens[:1], *(t.partition("=")[0] for t in tokens[1:] if t.startswith("-"))])


def unrestored_flags(flags: str, mode: str | None) -> list[str]:
    """The flags of an original command line (:func:`option_names`, the executable first) that a resume would not
    carry over: any not in :data:`_CLAUDE_NEUTRAL`, except :data:`_BYPASS_FLAG` when the mode a resume sets IS that
    flag's (``bypassPermissions``)."""
    out = []
    for token in flags.split()[1:]:
        name = token.partition("=")[0]
        if name not in _CLAUDE_NEUTRAL and not (name == _BYPASS_FLAG and mode == "bypassPermissions"):
            out.append(name)
    return list(dict.fromkeys(out))


def _last_turn_context(path: Path) -> dict[str, Any] | None:
    """The newest ``turn_context`` record of a rollout, read from the file's end, or ``None``: none, or the file's
    last line or the newest line that names one does not parse (a truncated or damaged newest record never lets an
    older one decide), or a line longer than :data:`_ROLLOUT_LINE_MAX`."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos, tail, seen_last = fh.tell(), b"", False
        while pos > 0:
            step = min(_ROLLOUT_BLOCK, pos)
            pos -= step
            fh.seek(pos)
            tail = fh.read(step) + tail
            lines = tail.split(b"\n")
            tail = lines[0] if pos > 0 else b""        # the first piece may be the end of an earlier line
            for raw in reversed(lines[1:] if pos > 0 else lines):
                if not seen_last and raw.strip():
                    seen_last = True
                    try:                  # the newest line, damaged where its type would be, decides nothing
                        json.loads(raw)
                    except ValueError:
                        return None
                if b'"turn_context"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    return None
                if isinstance(rec, dict) and rec.get("type") == "turn_context":
                    payload = rec.get("payload")
                    return payload if isinstance(payload, dict) else None
            if len(tail) > _ROLLOUT_LINE_MAX:
                return None
    return None


_ROLLOUT_BLOCK = 1 << 16
_ROLLOUT_LINE_MAX = 1 << 24
# a workspace-write policy's fields as Codex records them [measured: 1019+1, codex-cli 0.162.1], each restored with
# `-c sandbox_workspace_write.<key>=`; a missing bool or any other field, in any mode, refuses the resume (it would
# come from config.toml, or cannot be reproduced)
_WW_LISTS, _WW_BOOLS = ("writable_roots",), ("network_access", "exclude_tmpdir_env_var", "exclude_slash_tmp")


def codex_sandbox_args(meta: Mapping[str, Any], thread: str) -> list[str] | None:
    """The options that make ``codex exec resume`` run under the sandbox the Codex thread last ran under (its one
    rollout under the recorded config directory, ``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl``, its
    newest ``turn_context`` ``sandbox_policy``): ``-s <mode>`` first (a command-line flag outranks config.toml), then a
    workspace-write policy's fields; or ``None`` (no single rollout, an unreadable newest record, a mode not in
    :data:`CODEX_SANDBOXES`, a field that cannot be reproduced)."""
    if not session_ok(thread):
        return None
    config = meta.get("config_dir")
    chome = Path(config) if isinstance(config, str) and os.path.isabs(config) else Path.home() / ".codex"
    try:
        found = list((chome / "sessions").glob(f"*/*/*/rollout-*-{thread}.jsonl"))
        if len(found) != 1:
            return None
        context = _last_turn_context(found[0])
    except OSError:
        return None
    policy = context.get("sandbox_policy") if context else None
    if not isinstance(policy, dict) or policy.get("type") not in CODEX_SANDBOXES:
        return None
    mode = policy["type"]
    fields = {k: v for k, v in policy.items() if k != "type"}
    if mode != "workspace-write":
        return None if fields else ["-s", mode]
    # Codex leaves writable_roots out when it is empty [measured: 1019+1, 0.162.1], so absent means [] and is written
    # as []; a bool left out would come from config.toml as it is now, so it refuses
    fields.setdefault("writable_roots", [])
    if set(fields) != {*_WW_LISTS, *_WW_BOOLS}:
        return None
    out = ["-s", mode]
    for key, value in fields.items():
        if key in _WW_BOOLS and isinstance(value, bool):
            out += ["-c", f"sandbox_workspace_write.{key}={'true' if value else 'false'}"]
        elif (key in _WW_LISTS and isinstance(value, list)
              and all(isinstance(r, str) and os.path.isabs(r) and r.isprintable() for r in value)):
            out += ["-c", f"sandbox_workspace_write.{key}={json.dumps(value, ensure_ascii=False)}"]
        else:
            return None
    return out


def codex_unrestored(flags: str) -> list[str]:
    """The options of an original Codex command line (:func:`option_names`) that a resume would not carry over."""
    ok = (*_CODEX_CARRIED, *_CODEX_NEUTRAL)
    return list(dict.fromkeys(t.partition("=")[0] for t in flags.split()[1:] if t.partition("=")[0] not in ok))


def resume_refusal(agent: str, session: str, meta: Mapping[str, Any]) -> str | None:
    """Why the ended session ``session`` (its resumable id) is not resumed on what was recorded at filing, or
    ``None`` (see the module docstring)."""
    if not session_ok(session):
        return "its session id is not a UUID"
    cwd = meta.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return f"its start directory {cwd!r} is not an existing absolute directory"
    if agent != _CLAUDE:
        flags = meta.get("codex_flags")
        if not isinstance(flags, str) or not flags:
            return "its original command line was not recorded, so what a resume would drop cannot be told"
        dropped = codex_unrestored(flags)
        if dropped:
            return f"it was started with {', '.join(dropped)}, which a resume does not carry over"
        if codex_sandbox_args(meta, session) is None:
            return ("the sandbox it ran under could not be read from its rollout, or cannot be reproduced, so a resume "
                    "could run under a looser one")
        return None
    if not _transcript(meta, session).is_file():
        return "no transcript of it is where Claude Code keeps it for that config directory and start directory"
    mode = meta.get("permission_mode")
    if mode not in _MODES:
        return f"its permission mode {mode!r} is not one a resume can set"
    flags = meta.get("claude_flags")
    if not isinstance(flags, str) or not flags:
        return "its original command line was not recorded, so what a resume would drop cannot be told"
    dropped = unrestored_flags(flags, mode)
    if dropped:
        return f"it was started with {', '.join(dropped)}, which a resume does not carry over"
    config = meta.get("config_dir") if isinstance(meta.get("config_dir"), str) else None
    return gate_refusal(cwd, config)


def _resume_argv(agent: str, session: str, meta: Mapping[str, Any], env: Mapping[str, str]) -> list[str] | None:
    """The resume command for an ended session, without its prompt (appended last), or ``None`` when the agent's
    command is not found or the session id is not one (checked again here: it is an argument)."""
    if not session_ok(session) or "#" in session:
        return None
    if agent == _CLAUDE:
        exe = _exe("claude", env)
        if exe is None:
            return None
        argv = [exe, "-p" if meta.get("invocation") == "-p" else "--bg", f"--resume={session}"]
        mode = meta.get("permission_mode")
        if mode in _MODES:
            argv += ["--permission-mode", mode]
        return argv
    exe = _exe("codex", env)
    if exe is None:
        return None
    sandbox = codex_sandbox_args(meta, session)
    if sandbox is None:
        return None                       # never resumed under config.toml's sandbox (resume_refusal says why)
    names = {t.partition("=")[0] for t in str(meta.get("codex_flags") or "").split()}
    return [exe, *sandbox[:2], "exec", "resume", *(f for f in _CODEX_CARRIED if f in names), *sandbox[2:], session]


def _verified_ruling(queue: ConsentQueue, request_id: str) -> Ruling | None:
    got = queue.decision(request_id)
    return got if isinstance(got, Ruling) else None


def plan(queue: ConsentQueue, request_id: str, *,
         listing: Callable[[Mapping[str, Any]], dict[str, dict[str, Any]] | None] = claude_listing,
         running: Callable[..., bool | None] = process_running) -> Delivery:
    """Where the ruling on ``request_id`` would go now, with no side effect (see the module docstring). A ``resumed``
    plan carries the resume command without its prompt; :func:`deliver` leases the texts and starts it."""
    got = queue.get(request_id)
    if got is None:
        return Delivery(request_id, "", "", "none", "no sealed request of that id")
    request = got[0]
    agent, session = request.agent, request.agent_session_id
    ruling = _verified_ruling(queue, request_id)
    if ruling is None:
        return Delivery(request_id, agent, session, "none", f"the request is {got[1]}, with no ruling that verifies")
    out = Delivery(request_id, agent, session, "hook", "", gap=ruling.gap, ruling=ruling.ruling)
    if request.tool_name == "levain_limit" and ruling.ruling == "deny":
        # Q13: a late End leaves the session stopped; resuming it would spend a turn only to be stopped again
        return replace(out, reason="a limit's End: the session stays stopped and no resume is started for it")
    if request.tool_name == "levain_limit" and ruling.ruling == "allow":
        from levain.consent.limits import extended_by, root_halted

        try:
            mine = extended_by(queue.directory, request_id)
        except Exception:  # noqa: BLE001 - unreadable: today's behaviour (the route decides)
            mine = True
        if root_halted(queue.directory, agent, session) is False and not mine:
            # Q13: ANOTHER Extend already lifted the halt (its holder resumed it, if it could): never a second resume.
            # This ruling's own Extend lifting it (applied at the gate or by `levain limits extend`) still resumes
            return replace(out, reason="the limit was already extended by another allow; no resume")
    state, when = queue.delivery_state(request_id)
    if state == "delivered":
        return replace(out, route="delivered", reason=f"its text reached the session at {when}")
    if state == "unconfirmed":
        return replace(out, route="unconfirmed", reason=f"a resume was started for it at {when} and never confirmed: "
                                                        "it may have run, so it is not started again")
    meta = queue.resume_meta(request_id) or {}
    where, why, argv, cwd = route(agent, session, meta, listing=listing, running=running)
    return replace(out, route=where, reason=why, argv=argv, cwd=cwd)


def route(agent: str, session: str, meta: Mapping[str, Any], *, noun: str = "ruling",
          listing: Callable[[Mapping[str, Any]], dict[str, dict[str, Any]] | None] = claude_listing,
          running: Callable[..., bool | None] = process_running,
          named: Callable[[str], bool | None] | None = None) -> tuple[str, str, tuple[str, ...], str | None]:
    """Where a text owed to ``session`` (a hook's session key) would go now, from what was recorded for a resume
    (``meta``) and the session's state read now, with no side effect: ``(route, reason, argv, cwd)``, the route
    ``hook``, ``stop_resume``, ``resumed`` (``argv`` without its prompt, in ``cwd``) or ``none`` (see the module
    docstring). ``noun`` names the text in the reason (a ruling, a question's answer). Shared by :func:`plan` and the
    question band's delivery. ``named`` (default :func:`session_named_running`, given the directory recorded in
    ``meta``, so a ``claude -c`` elsewhere does not hold it) is the last guard before a resume: a
    recorded process that has ended does not mean the SESSION has (an earlier resume, or the operator's own
    ``codex resume``, runs under a new process), so any running process whose command line resumes the session id
    leaves the text to its next hook."""
    if named is None:
        def named(sid: str) -> bool | None:
            return session_named_running(sid, meta.get("cwd"))
    target = base_session(session)
    lands = f"the {noun} lands at its next step"
    if agent == _CLAUDE:
        live = listing(meta)
        if live is None:
            return "hook", f"the live sessions could not be listed, so the session is not resumed; {lands}", (), None
        row = live.get(target)
        if row is not None:
            status, kind = row.get("status"), row.get("kind")
            if status == "busy":
                return "hook", f"the session is working; the {noun} rides its next step", (), None
            if status == "idle" and kind == "background":
                if STOP_THEN_RESUME_RUN:
                    return "stop_resume", "idle in the background: stopped, then resumed", (), None
                return "hook", ("the session is idle in the background; stop-then-resume ships after its run (B7), "
                                f"so {lands}"), (), None
            if status == "idle":
                return "hook", f"the session is open in a terminal; {lands}", (), None
            return "hook", f"the session is live ({status!r}); the {noun} rides its next step", (), None
        pid_key, started = "claude_pid", meta.get("claude_started")
    elif agent == _CODEX:
        pid_key, started = "codex_pid", None
    else:
        return "none", f"no delivery for agent {agent!r}", (), None
    if not isinstance(meta.get("cwd"), str) or not meta.get("invocation"):
        return "hook", f"no resume record was kept for this session; {lands}", (), None
    pid = meta.get(pid_key)
    if pid is None:
        return "hook", f"no process was recorded for it, so it cannot be told ended; {lands}", (), None
    if running(pid, started) is not False:
        return "hook", f"its process {pid} (started {started or 'at a time not recorded'}) may still run; {lands}", \
            (), None
    resuming = named(target)
    if resuming is True:
        return "hook", f"a process resuming this session may be running (an earlier resume, or one by hand); {lands}", \
            (), None
    if resuming is not False:
        return "hook", ("a resume without a session id is running (codex resume --last or by name, claude -c, "
                        "--continue or a --resume picker), or the process list could not be read, so this session "
                        f"may be the one it resumed; {lands}"), (), None
    why = resume_refusal(agent, target, meta)
    if why is not None:
        return "hook", f"the session has ended but is not resumed: {why}; resume it by hand and {lands}", (), None
    argv = _resume_argv(agent, target, meta, _env(agent, meta))
    if argv is None:
        return "hook", f"no {'claude' if agent == _CLAUDE else 'codex'} command to resume it with", (), None
    return "resumed", f"the session has ended: resumed with the {noun} as its prompt", tuple(argv), meta["cwd"]


# the options of `codex resume` / `codex exec resume` that take a separate value [measured: codex-cli 0.162.1
# --help]; any other option is read as a switch, so the first word after the options is the session selector
_CODEX_VALUED = frozenset({
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-i", "--image", "-m",
    "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a",
    "--ask-for-approval", "--thread-source", "--cyber-access-program", "--output-schema", "-o",
    "--output-last-message",
})

# :func:`_resumes` for a `claude -c` / `--continue`: it resumes the latest session of its OWN directory, so which
# session it holds is told by its cwd (:func:`_continues_in`)
_CONTINUE = "continue"


def _agent_cli(exe: str) -> str | None:
    """``claude`` or ``codex`` when the executable path ``exe`` is that CLI, else ``None``. Claude Code's native
    install runs as ``.../claude/versions/<version>`` (its basename is the version, not ``claude``), and its npm
    install as ``node .../claude-code/cli.js``."""
    path = Path(exe)
    if path.name in ("claude", "codex"):
        return path.name
    if path.parent.name == "versions" and path.parent.parent.name == "claude":
        return "claude"
    if path.name == "cli.js" and "claude-code" in path.parts:
        return "claude"
    return None


def _cli_word(token: str) -> str | None:
    """``claude`` or ``codex`` when the argv word ``token`` may be that CLI's executable: :func:`_agent_cli`, or a
    path containing ``claude-code/`` or ``.local/share/claude/versions/``. It is a test on the word alone, with no
    list of wrappers or of their options, so ``env -u K claude``, ``nohup claude``, ``caffeinate -i claude`` and
    ``node --require x <dir>/claude-code/cli.js`` all name the CLI, and so does the word ``claude`` in another tool's
    arguments (``grep claude -c file``): the reading fails closed. No marker or name holds a space, so a path ``ps``
    split at a space still ends in a word this reads."""
    name = _agent_cli(token)
    if name:
        return name
    if "claude-code/" in token or ".local/share/claude/versions/" in token:
        return "claude"
    return None


def _claude_selector(value: str) -> str | None:
    """The session id a Claude Code ``--resume`` value names: the UUID itself, or the stem of a transcript path
    (``--resume <dir>/<uuid>.jsonl``); ``None`` for a name, a search term or nothing."""
    if _UUID.fullmatch(value):
        return value
    stem = os.path.basename(value)
    if stem.endswith(".jsonl") and _UUID.fullmatch(stem[:-6]):
        return stem[:-6]
    return None


def _codex_selector(after: list[str]) -> tuple[str | None, bool]:
    """``(selector, last)`` for the words after Codex's ``resume``: the first positional word once its options and
    their values (:data:`_CODEX_VALUED`) are passed, and whether ``--last`` was among those options. A UUID later in
    the prompt is never the selector."""
    last, i = False, 0
    while i < len(after):
        tok = after[i]
        if tok == "--":
            return (after[i + 1] if i + 1 < len(after) else None), last
        if not tok.startswith("-") or tok == "-":
            return tok, last
        last = last or tok == "--last"
        i += 2 if (tok in _CODEX_VALUED and "=" not in tok) else 1
    return None, last


def _names_resume(tokens: list[str], want: str) -> bool:
    """Whether the lower-cased argv ``tokens`` carry ``want`` as the value of a resume option, ANYWHERE and in ANY
    program: ``--resume <id>``, ``--resume=<id>``, ``-r <id>``, ``-r<id>`` (the value may be a transcript path,
    :func:`_claude_selector`), or a ``resume`` word whose session selector (:func:`_codex_selector`) is the id."""
    for i, tok in enumerate(tokens):
        if tok in (f"--resume={want}", f"-r{want}"):
            return True
        if tok in ("--resume", "-r") and i + 1 < len(tokens) and _claude_selector(tokens[i + 1]) == want:
            return True
        if tok.startswith("--resume=") and _claude_selector(tok.split("=", 1)[1]) == want:
            return True
        if tok == "resume" and _codex_selector(tokens[i + 1:])[0] == want:
            return True
        # ps does not quote: a transcript path with a space splits, and its last piece still ends with the file name
        if tok.endswith(f"/{want}.jsonl") and any(t in ("--resume", "-r") for t in tokens[:i]):
            return True
    return False


def _cli_resumes(prog: str, rest: list[str], want: str) -> bool | str | None:
    """:func:`_resumes` for the lower-cased words ``rest`` after a word naming the CLI ``prog`` (:func:`_cli_word`):
    the forms that need the program, the strong ones (:func:`_names_resume`) already read."""
    if prog == "codex":
        lead = _options(rest, words=("exec",))
        if "resume" not in lead:
            return False
        selector, last = _codex_selector(rest[lead.index("resume") + 1:])
        return None if last or selector is None or not _UUID.fullmatch(selector) else False
    for i, tok in enumerate(rest):
        if tok in (f"--session-id={want}", f"-r={want}"):
            return True
        if tok == "--session-id" and i + 1 < len(rest) and _claude_selector(rest[i + 1]) == want:
            return True
    opts = _options(rest)
    continues = False
    for i, tok in enumerate(opts):
        if tok in ("-c", "--continue"):
            continues = True
        elif tok in ("--resume", "-r"):
            if not (i + 1 < len(opts) and _claude_selector(opts[i + 1])):
                return None
        elif tok.startswith(("--resume=", "-r")) and not _claude_selector(
                tok.split("=", 1)[1] if "=" in tok else tok[2:]):
            return None
    return _CONTINUE if continues else False


def _resumes(tokens: list[str], session: str) -> bool | str | None:
    """Whether the argv ``tokens`` run ``session``, read so that a process it cannot place HOLDS delivery rather than
    lets a second copy start (this is the backstop behind ``claude agents --json``; a held delivery rides the
    session's next step). Ids compare case-insensitively and only as a whole token.

    ``True`` for the strong forms in any process, with no program check (:func:`_names_resume`): the id as the value
    of ``--resume`` / ``-r`` (also a transcript path), or as a ``resume`` word's session selector (``codex resume``,
    ``codex exec resume``). Every other form counts only after a word that may be the CLI (:func:`_cli_word`), read
    from each such word on: ``True`` for a Claude Code ``--session-id <id>``, ``--session-id=<id>`` or ``-r=<id>``;
    ``None`` for a resume that names no session id and so may be resuming this one (a Claude ``--resume`` / ``-r``
    with no id or a name, a Codex ``resume`` with ``--last``, no selector or a name); :data:`_CONTINUE` for a Claude
    ``-c`` / ``--continue``. ``False`` otherwise. The id-less forms are read only in the leading option run after that
    word (:func:`_options`), so a ``-c`` or ``resume`` inside a prompt does not count."""
    want = session.lower()
    low = [t.lower() for t in tokens]
    if _names_resume(low, want):
        return True
    found: list[bool | str | None] = []
    for at, tok in enumerate(tokens):
        prog = _cli_word(tok)
        if prog:
            found.append(_cli_resumes(prog, low[at + 1:], want))
    if any(got is True for got in found):
        return True
    if any(got is None for got in found):
        return None
    return _CONTINUE if _CONTINUE in found else False


def _options(tokens: list[str], words: tuple[str, ...] = ()) -> list[str]:
    """The leading run of ``tokens`` that can still be options (or the subcommand ``words``): it ends before the first
    word that neither starts with ``-`` nor follows a token that does (an option's value), so a prompt's words
    (``please resume``, a ``-c`` inside an unquoted prompt) are not read as flags. The last word kept may be a
    subcommand (``resume``)."""
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if not (tok.startswith("-") or tok in words or tok == "resume"
                or (i and tokens[i - 1].startswith("-") and "=" not in tokens[i - 1])):
            break
        out.append(tok)
        if tok == "resume":
            break
    return out


#: how long :func:`session_named_running` may spend reading the process list and the cwds it needs, in seconds
NAMED_BUDGET = 5.0


def _continues_in(pid: str, cwd: Any, deadline: float) -> bool | None:
    """Whether the ``claude -c`` process ``pid`` runs in ``cwd`` (the same directory, :func:`os.path.samefile`, so a
    case or symlink difference on macOS still matches; resolved-path equality when either cannot be stat'ed), read
    with ``lsof``; ``None`` when ``cwd`` is not a path or the process's cwd cannot be read before ``deadline``."""
    left = deadline - time.monotonic()
    if not isinstance(cwd, str) or not cwd or left <= 0:
        return None
    try:
        got = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", pid], capture_output=True, text=True,
                             timeout=left, stdin=subprocess.DEVNULL,
                             env={"PATH": "/usr/sbin:/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    path = next((ln[1:] for ln in got.stdout.splitlines() if ln.startswith("n")), None)
    if not path:
        return None
    try:
        return os.path.samefile(path, cwd)
    except (OSError, ValueError):
        return os.path.realpath(path) == os.path.realpath(cwd)


def session_named_running(session: str, cwd: Any = None) -> bool | None:
    """Whether a running process resumes ``session`` (:func:`_resumes`, read from ``ps``'s command lines), ``None``
    when the process list cannot be read or a resume that names no session id may be resuming it (``codex resume
    --last``, a ``--resume`` picker: which session it holds is not in its command line), so the caller starts no
    resume. This argv reading is the BACKSTOP behind Claude Code's ``claude agents --json`` listing, and it fails
    CLOSED: a process it cannot place holds delivery (which rides the session's next step) rather than reads ``False``
    (which starts a second copy of a live session). So the id as a resume option's value (``--resume <id>``, ``-r<id>``,
    a ``resume <id>``) counts in ANY process, with no check of which program it is; the weaker forms count after any
    word that may be the CLI (:func:`_cli_word`), with no list of wrappers. A ``claude -c`` / ``--continue`` can only
    be resuming a session started in its own directory: it counts (``None``) only when its cwd is ``cwd``, the
    directory ``session`` was started in, or when either cannot be read. A process that only mentions the id outside
    a resume option (``levain consent rule --session <id>``, its shell, ``tail -f`` of the transcript) does not count,
    and this process never does. An ancestor that IS a resume of the session counts: the session is then running (a
    hook of it delivering)."""
    if not session_ok(session):
        return None
    deadline = time.monotonic() + NAMED_BUDGET
    try:
        got = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True,
                             timeout=NAMED_BUDGET, stdin=subprocess.DEVNULL,
                             env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if got.returncode != 0:
        return None
    me = str(os.getpid())
    idless = False
    continuing: list[str] = []
    for line in got.stdout.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid == me:
            continue
        # ps does not quote: a prompt's apostrophe can unbalance shlex, and a quote pair can swallow tokens, so both
        # readings are tried
        try:
            quoted = shlex.split(args)
        except ValueError:
            quoted = []
        readings = (_resumes(quoted, session), _resumes(args.split(), session))
        if True in readings:
            return True
        if None in readings:
            idless = True
        elif _CONTINUE in readings:
            continuing.append(pid)
    if idless:
        return None
    # the cwds are read after the whole list, so a match on the id is never delayed by lsof
    if any(_continues_in(pid, cwd, deadline) is not False for pid in continuing):
        return None
    return False


def _launch(argv: list[str], cwd: str, env: Mapping[str, str], log: Path) -> str | None:
    """Start ``argv`` detached in ``cwd``, its output to ``log``, and watch it :data:`RESUME_SETTLE` seconds: ``None``
    when it is still running then or exited 0 (confirmed), else why it failed. Any exception before ``Popen``
    returns (an argv with a NUL byte raises ``ValueError``) is a failure: no child is running. One raised after
    that propagates, since a child may then be running. A child still running is reaped by a daemon thread, so a
    long-lived caller keeps no zombie."""
    settle = RESUME_SETTLE
    try:
        log.parent.mkdir(mode=0o700, exist_ok=True)
        fh = open(log, "ab")
    except Exception as exc:  # noqa: BLE001 - nothing was started: a retryable failure
        return f"{type(exc).__name__}: {exc}"
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except Exception as exc:  # noqa: BLE001 - Popen raised, so nothing was started: a retryable failure
        fh.close()
        return f"{type(exc).__name__}: {exc}"
    # the child is spawned: from here an exception (this close's included) propagates, never a retryable failure
    fh.close()
    try:
        code = proc.wait(timeout=settle)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, name="levain-resume-reaper", daemon=True).start()
        return None
    return None if code == 0 else f"it exited {code} within {settle:g} s (its output: {log})"


def _stop(session: str, meta: Mapping[str, Any],
          listing: Callable[[Mapping[str, Any]], dict[str, dict[str, Any]] | None]) -> str | None:
    """``claude stop <session>``, then wait for the listing to drop it; ``None`` when it did, else why not."""
    env = _env(_CLAUDE, meta)
    exe = _exe("claude", env)
    if exe is None:
        return "no claude command"
    try:
        got = subprocess.run([exe, "stop", session], capture_output=True, text=True, timeout=LISTING_TIMEOUT,
                             env=env, stdin=subprocess.DEVNULL, cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError) as exc:
        return f"claude stop failed: {type(exc).__name__}"
    if got.returncode != 0:
        return f"claude stop exited {got.returncode}"
    end = time.monotonic() + STOP_SETTLE
    while time.monotonic() < end:
        live = listing(meta)
        if live is not None and session not in live:
            return None
        time.sleep(1.0)
    return "the stopped session is still listed"


def deliver(queue: ConsentQueue, request_id: str, *,
            listing: Callable[[Mapping[str, Any]], dict[str, dict[str, Any]] | None] = claude_listing,
            running: Callable[..., bool | None] = process_running,
            launch: Callable[[list[str], str, Mapping[str, str], Path], str | None] = _launch,
            notify: Callable[[str], Any] | None = None) -> Delivery:
    """Deliver the ruling on ``request_id`` (:func:`plan`): for an ended session, lease the session's undelivered
    ruling texts (:meth:`ConsentQueue.lease_rulings_for`), start the resume with them as its prompt and settle the
    lease by its outcome (:meth:`ConsentQueue.settle_resume`); for any other, leave it to the session's next hook.
    With ``notify``, the operator is pinged when the ruling waits, failed or is unconfirmed (a ping never fails the
    delivery)."""
    from levain.consent.adapters.claude_code import ruling_line

    got = plan(queue, request_id, listing=listing, running=running)
    meta = queue.resume_meta(request_id) or {}
    target = base_session(got.session)
    if got.route == "stop_resume":
        why = _stop(target, meta, listing)
        if why is not None:
            got = replace(got, route="hook", reason=f"the background session was not stopped ({why}); the ruling "
                                                    "lands at its next step")
        else:
            got = plan(queue, request_id, listing=listing, running=running)
    if got.route == "resumed" and not os.path.isdir(got.cwd or ""):
        got = replace(got, route="hook", argv=(), reason=f"the session's start directory {got.cwd} is gone; the "
                                                         "ruling lands at its next step")
    if got.route == "resumed":
        leased = queue.lease_rulings_for(got.agent, got.session)
        if not leased:
            state, when = queue.delivery_state(request_id)
            if state == "delivered":
                got = replace(got, route="delivered", argv=(), reason=f"its text reached the session at {when}")
            elif state == "unconfirmed":
                got = replace(got, route="unconfirmed", argv=(), reason="another resume holds it, not yet confirmed")
            else:
                got = replace(got, route="hook", argv=(), reason="its ruling could not be verified now, so nothing "
                                                                 "was started; the ruling lands at its next step")
        else:
            ids = tuple(r.request_id for r in leased)
            argv = list(got.argv)
            try:
                lines = [line for line in (ruling_line(queue, r, quote=True) for r in leased) if line]
                argv = [*got.argv, "\n".join([*lines, QUOTED_NOTE])]
                env = _env(got.agent, meta)
                log = Path(queue.directory) / "resumes" / f"{ids[0]}.log"
            except Exception as exc:  # noqa: BLE001 - nothing was started: the lease settles failed, so it is retried
                why = f"the resume could not be prepared ({type(exc).__name__}: {exc})"
            else:
                # ``launch`` turns every exception before the child is spawned into a reason; one it raises
                # leaves the lease unsettled, read as unconfirmed (a child may be running)
                why = launch(argv, got.cwd or "", env, log)
            queue.settle_resume(list(ids), agent_session_id=got.session, argv=argv, cwd=got.cwd or "",
                                outcome="started" if why is None else "failed", detail=why)
            if why is None:
                return replace(got, argv=tuple(argv), rulings=ids)
            got = replace(got, route="failed", argv=tuple(argv), rulings=ids,
                          reason=f"the resume did not start ({why}); the ruling stays deliverable: the session's next "
                                 "step, or `levain consent deliver`")
    if notify is not None and got.route in ("hook", "failed", "unconfirmed"):
        word = {"hook": "waits", "failed": "was not delivered", "unconfirmed": "is unconfirmed"}[got.route]
        try:
            notify(f"levain: ruling on gap {gap_label(got.gap or 0, got.session)} ({(got.ruling or '').upper()}) "
                   f"{word}: {got.reason}.")
        except Exception:  # noqa: BLE001 - a ping is a ping: it never fails the delivery
            pass
    return got


def deliver_undelivered(queue: ConsentQueue, **kw: Any) -> list[Delivery]:
    """:func:`deliver` every ruling whose text is not known to have reached its session (``levain consent
    deliver``, for a ruling left to a next hook that never came, or a resume that failed); one ruling's unexpected
    error is reported as its ``failed`` delivery and never stops the rest."""
    out = []
    for gapped in queue.undelivered_rulings():
        rid = gapped.request.id
        if queue.ruling_delivered(rid) is not None:   # a resume above may have carried it
            continue
        try:
            out.append(deliver(queue, rid, **kw))
        except Exception as exc:  # noqa: BLE001 - the next ruling is still delivered
            out.append(Delivery(rid, gapped.request.agent, gapped.request.agent_session_id, "failed",
                                f"delivery raised {type(exc).__name__}: {exc}", gap=gapped.gap))
    return out


def agent_notify(queue_dir: Path, agent: str, request_id: str | None = None) -> Callable[[str], Any] | None:
    """The ping for a ruling's delivery under the request's own agent's settings (its notify command and phone URL,
    and the phone's web push when pings are on), or ``None`` when that agent has no ping configured."""
    from levain.consent.adapters import claude_code, codex

    adapter = {_CLAUDE: claude_code, _CODEX: codex}.get(agent)
    try:
        settings = adapter.load_settings(queue_dir) if adapter is not None else None
    except Exception:  # noqa: BLE001 - a ping is optional
        return None
    if settings is None or not claude_code.ping_configured(settings, queue_dir):
        return None
    send = getattr(adapter, "notify", claude_code.notify)   # Codex shares Claude Code's notify seam
    ref = None if request_id is None else f"request:{request_id}"
    return lambda text: send(settings, text, cwd=queue_dir, kind="ruling", ref=ref)
