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
# the flags of an original Claude Code command line that a resume does not carry over: resumed without them, the
# session would run with fewer constraints than it was started with (each also as ``--flag=value``)
_UNRESTORED = ("--settings", "--mcp-config", "--strict-mcp-config", "--add-dir", "--allowedTools", "--allowed-tools",
               "--disallowedTools", "--disallowed-tools", "--tools", "--append-system-prompt",
               "--append-system-prompt-file", "--system-prompt", "--system-prompt-file", "--agents", "--plugin-dir",
               "--permission-prompt-tool", "--setting-sources")
# the one --dangerously-* flag a resume restores: bypassPermissions IS that flag's mode
_BYPASS_FLAG = "--dangerously-skip-permissions"
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

    env = child_env(*_AGENT_AUTH, *NETWORK)
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
    import shlex

    from levain.consent.adapters.claude_code import GATE_EVENT, GATE_TIMEOUT, HOOK_FLAGS, HOOKRUN
    from levain.install import CONSENT_HOOK_SCRIPTS

    user = Path(config_dir) if config_dir else Path.home() / ".claude"
    project = Path(cwd) / ".claude"
    files = {f: _settings(f) for f in (project / "settings.json", project / "settings.local.json",
                                        user / "settings.json")}
    off = [str(f) for f, doc in files.items() if doc.get("disableAllHooks") is True]
    if off:
        return f"{off[0]} turns every hook off"
    want = {"type": "command", "onFailure": "block", "timeout": GATE_TIMEOUT}
    lookalike = None
    for f in (project / "settings.json", user / "settings.json"):
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
    """The flags of an original command line (:func:`option_names`) that a resume would not carry over."""
    out = []
    for token in flags.split():
        name = token.partition("=")[0]
        if name in _UNRESTORED or (name.startswith("--dangerously-")
                                   and not (name == _BYPASS_FLAG and mode == "bypassPermissions")):
            out.append(name)
    return list(dict.fromkeys(out))


def resume_refusal(agent: str, session: str, meta: Mapping[str, Any]) -> str | None:
    """Why the ended session ``session`` (its resumable id) is not resumed on what was recorded at filing, or
    ``None`` (see the module docstring)."""
    if not session_ok(session):
        return "its session id is not a UUID"
    cwd = meta.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return f"its start directory {cwd!r} is not an existing absolute directory"
    if agent != _CLAUDE:
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
    return None if exe is None else [exe, "exec", "resume", session]


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
    question band's delivery. ``named`` (default :func:`session_named_running`) is the last guard before a resume: a
    recorded process that has ended does not mean the SESSION has (an earlier resume, or the operator's own
    ``codex resume``, runs under a new process), so any running process whose command line names the session id
    leaves the text to its next hook."""
    named = named if named is not None else session_named_running
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
    if named(target) is not False:
        return "hook", f"a process naming this session may be running (an earlier resume, or one by hand); {lands}", \
            (), None
    why = resume_refusal(agent, target, meta)
    if why is not None:
        return "hook", f"the session has ended but is not resumed: {why}; resume it by hand and {lands}", (), None
    argv = _resume_argv(agent, target, meta, _env(agent, meta))
    if argv is None:
        return "hook", f"no {'claude' if agent == _CLAUDE else 'codex'} command to resume it with", (), None
    return "resumed", f"the session has ended: resumed with the {noun} as its prompt", tuple(argv), meta["cwd"]


def session_named_running(session: str) -> bool | None:
    """Whether a running process's command line names ``session`` (a resume of it: ``--resume=<id>``, ``codex exec
    resume <id>``), ``None`` when the process list cannot be read (read as maybe: no resume)."""
    if not session_ok(session):
        return None
    try:
        got = subprocess.run(["ps", "-axww", "-o", "args="], capture_output=True, text=True, timeout=5,
                             stdin=subprocess.DEVNULL, env={"PATH": "/bin:/usr/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if got.returncode != 0:
        return None
    return any(session in line for line in got.stdout.splitlines())


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


def agent_notify(queue_dir: Path, agent: str) -> Callable[[str], Any] | None:
    """The ping for a ruling's delivery under the request's own agent's settings (its notify command and phone URL),
    or ``None`` when that agent has none configured."""
    from levain.consent.adapters import claude_code, codex

    adapter = {_CLAUDE: claude_code, _CODEX: codex}.get(agent)
    try:
        settings = adapter.load_settings(queue_dir) if adapter is not None else None
    except Exception:  # noqa: BLE001 - a ping is optional
        return None
    if settings is None or not getattr(settings, "notify_argv", None):
        return None
    send = getattr(adapter, "notify", claude_code.notify)   # Codex shares Claude Code's notify seam
    return lambda text: send(settings, text, cwd=queue_dir)
