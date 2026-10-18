"""levain.consent.delivery — a late ruling delivered back to the session it rules on (DESIGN §3.6, seat ruling R-i).

A ruling (:meth:`~levain.consent.queue.ConsentQueue.answer_late`) changes the queue; this module gets its text to the
agent. The text reaches a session ONCE, through :meth:`~levain.consent.queue.ConsentQueue.claim_rulings_for`, whichever
path claims it first: the session's own next hook or a resume started here. Where it
goes is read from the session's state at the moment of delivery, never assumed:

  - Claude Code, from ``claude agents --json`` run under the session's own config directory (a listed ``sessionId``
    is live, with ``status`` busy or idle and ``kind`` background or interactive; a running ``claude -p`` is listed
    too, as interactive):
      * listed and busy: its next hook carries the text (nothing is started here);
      * listed, idle, interactive (a terminal holds it): its next hook; the ping says it lands at its next step;
      * listed, idle, background: ``claude stop`` then ``claude --bg --resume`` under the same id, ONLY once the run
        owed for it shows the stop keeps the id and the conversation and forks nothing (:data:`STOP_THEN_RESUME_RUN`);
        until then, its next hook;
      * not listed, and the process recorded at filing gone: ended. It is resumed with the text as its prompt, in the
        directory it was started in and its permission mode (recorded at filing,
        :meth:`~levain.consent.queue.ConsentQueue.resume_meta`): ``claude -p --resume`` when it was a ``-p`` run, else
        ``claude --bg --resume``.
  - Codex has no listing: a session whose ``codex exec`` process (its pid recorded at filing) is gone is ended and
    resumed with ``codex exec resume``; one whose process still runs, or with no pid recorded, gets its next hook.

A session is never resumed while it may be running (a resume of a running session starts a copy: two agents on one
tree): a listing that fails, a recorded process still alive (any program: a reused pid only delays the delivery), or no
resume record all leave the text to the next hook. The text is claimed BEFORE the resume starts, so the resumed
session's own hooks find nothing to deliver a second time.

Known edges (stated, not built against):
  - a session resumed by someone else between the listing and the launch (about a second) gets a copy.
  - a ruling left to a busy session's next hook waits if that session then ends with no further tool call;
    :func:`deliver_undelivered` (``levain consent deliver``) later finds it ended and resumes it.
  - a resumed Claude Code session works in the directory it was started in; a ruled allow is taken only by a call made
    in the working directory the request was filed in.
  - a ruling on a subagent's call is claimed under the subagent's session: a resume delivers it to the parent session;
    the parent's own hooks do not.
  - Codex's permission mode is not restored (its hook's ``permission_mode`` is not its sandbox): a resume runs under
    the user's Codex config.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from levain.consent.queue import ConsentQueue, Ruling

__all__ = ["STOP_THEN_RESUME_RUN", "Delivery", "base_session", "claude_listing", "deliver", "deliver_undelivered",
           "plan", "process_running"]

#: The stop-then-resume path for an IDLE background Claude Code session (R-i). Off until the run owed in B7 shows
#: ``claude stop`` keeps the session id and its conversation and ``claude --bg --resume`` then continues it with no
#: fork; while off, such a session gets the ruling at its next step, and the delivery says so.
STOP_THEN_RESUME_RUN = False
# how long a listing may take
LISTING_TIMEOUT = 30.0
# how long a stopped session may stay listed before the stop is judged not to have cleared it
STOP_SETTLE = 20.0
_CLAUDE = "claude_code"
_CODEX = "codex"
# the permission modes a resumed Claude Code session is started in (anything else recorded is left to its default)
_MODES = frozenset({"default", "acceptEdits", "plan", "bypassPermissions", "auto", "dontAsk"})
# the agent's own credentials, which a resumed session needs as its original had them; the allowlisted child
# environment drops every credential-shaped name, so these are carried by name
_AGENT_AUTH = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
               "OPENAI_API_KEY", "CODEX_API_KEY")
_CONFIG_VAR = {_CLAUDE: "CLAUDE_CONFIG_DIR", _CODEX: "CODEX_HOME"}


@dataclass(frozen=True)
class Delivery:
    """What became of one ruling's delivery. ``route``: ``hook`` (left to the session's next hook, ``reason`` says
    why), ``resumed`` (a resume started, ``argv`` in ``cwd``), ``failed`` (the text was claimed but the resume did
    not start: ``reason``), ``delivered`` (already claimed, by a hook or an earlier delivery), ``none`` (nothing to
    deliver: not a ruling that verifies). ``rulings`` names every request whose text a resume carried (a session's
    undelivered rulings travel together)."""
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


def process_running(pid: Any) -> bool | None:
    """True when a process ``pid`` exists (whatever program it is now), False when none does, ``None`` when that
    cannot be told."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _resume_argv(agent: str, session: str, meta: Mapping[str, Any], env: Mapping[str, str]) -> list[str] | None:
    """The resume command for an ended session, without its prompt (appended last), or ``None`` when the agent's
    command is not found."""
    if agent == _CLAUDE:
        exe = _exe("claude", env)
        if exe is None:
            return None
        argv = [exe, "-p" if meta.get("invocation") == "-p" else "--bg", "--resume", session]
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
         running: Callable[[Any], bool | None] = process_running) -> Delivery:
    """Where the ruling on ``request_id`` would go now, with no side effect (see the module docstring). A ``resumed``
    plan carries the resume command without its prompt; :func:`deliver` claims the texts and starts it."""
    got = queue.get(request_id)
    if got is None:
        return Delivery(request_id, "", "", "none", "no sealed request of that id")
    request = got[0]
    agent, session = request.agent, request.agent_session_id
    ruling = _verified_ruling(queue, request_id)
    if ruling is None:
        return Delivery(request_id, agent, session, "none", f"the request is {got[1]}, with no ruling that verifies")
    out = Delivery(request_id, agent, session, "hook", "", gap=ruling.gap, ruling=ruling.ruling)
    when = queue.ruling_delivered(request_id)
    if when is not None:
        return replace(out, route="delivered", reason=f"its text reached the session at {when}")
    meta = queue.resume_meta(request_id) or {}
    target = base_session(session)
    if agent == _CLAUDE:
        live = listing(meta)
        if live is None:
            return replace(out, reason="the live sessions could not be listed, so the session is not resumed; the "
                                       "ruling lands at its next step")
        row = live.get(target)
        if row is not None:
            status, kind = row.get("status"), row.get("kind")
            if status == "busy":
                return replace(out, reason="the session is working; the ruling rides its next step")
            if status == "idle" and kind == "background":
                if STOP_THEN_RESUME_RUN:
                    return replace(out, route="stop_resume", reason="idle in the background: stopped, then resumed")
                return replace(out, reason="the session is idle in the background; stop-then-resume ships after its "
                                           "run (B7), so the ruling lands at its next step")
            if status == "idle":
                return replace(out, reason="the session is open in a terminal; the ruling lands at its next step")
            return replace(out, reason=f"the session is live ({status!r}); the ruling rides its next step")
        pid_key = "claude_pid"
    elif agent == _CODEX:
        pid_key = "codex_pid"
    else:
        return replace(out, route="none", reason=f"no delivery for agent {agent!r}")
    if not isinstance(meta.get("cwd"), str) or not meta.get("invocation"):
        return replace(out, reason="no resume record was kept for this session; the ruling lands at its next step")
    pid = meta.get(pid_key)
    if agent == _CODEX and pid is None:
        return replace(out, reason="no codex process was recorded, so it cannot be told ended; the ruling lands at "
                                   "its next step")
    if pid is not None and running(pid) is not False:
        return replace(out, reason=f"its process {pid} is still running; the ruling lands at its next step")
    argv = _resume_argv(agent, target, meta, _env(agent, meta))
    if argv is None:
        return replace(out, reason=f"no {'claude' if agent == _CLAUDE else 'codex'} command to resume it with")
    return replace(out, route="resumed", reason="the session has ended: resumed with the ruling as its prompt",
                   argv=tuple(argv), cwd=meta["cwd"])


def _launch(argv: list[str], cwd: str, env: Mapping[str, str], log: Path) -> str | None:
    """Start ``argv`` detached in ``cwd``, its output to ``log``; ``None`` when it started, else why not."""
    try:
        log.parent.mkdir(mode=0o700, exist_ok=True)
        with open(log, "ab") as fh:
            subprocess.Popen(argv, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=fh,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


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
            running: Callable[[Any], bool | None] = process_running,
            launch: Callable[[list[str], str, Mapping[str, str], Path], str | None] = _launch,
            notify: Callable[[str], Any] | None = None) -> Delivery:
    """Deliver the ruling on ``request_id`` (:func:`plan`): for an ended session, claim the session's undelivered
    ruling texts, start the resume with them as its prompt and record it (:meth:`ConsentQueue.record_resume`); for
    any other, leave it to the session's next hook and, with ``notify``, ping where it waits."""
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
    if got.route != "resumed":
        if got.route == "hook" and notify is not None:
            notify(f"levain: ruling on gap #{got.gap} ({(got.ruling or '').upper()}) waits: {got.reason}.")
        return got
    if not os.path.isdir(got.cwd or ""):
        return replace(got, route="hook", argv=(), reason=f"the session's start directory {got.cwd} is gone; the "
                                                         "ruling lands at its next step")
    claimed = queue.claim_rulings_for(got.agent, got.session)
    if not claimed:
        return replace(got, route="delivered", argv=(), reason="its text was claimed by another path first")
    lines = [line for line in (ruling_line(queue, r) for r in claimed) if line]
    argv = [*got.argv, "\n".join(lines)]
    ids = tuple(r.request_id for r in claimed)
    env = _env(got.agent, meta)
    why = launch(argv, got.cwd or "", env, Path(queue.directory) / "resumes" / f"{ids[0]}.log")
    queue.record_resume(list(ids), agent_session_id=got.session, argv=argv, cwd=got.cwd or "",
                        outcome="started" if why is None else "failed", detail=why)
    if why is not None:
        return replace(got, route="failed", argv=tuple(argv), rulings=ids,
                       reason=f"the ruling was claimed but the resume did not start ({why})")
    return replace(got, argv=tuple(argv), rulings=ids)


def deliver_undelivered(queue: ConsentQueue, **kw: Any) -> list[Delivery]:
    """:func:`deliver` every ruling whose text has not reached its session (``levain consent deliver``, for a ruling
    left to a next hook that never came)."""
    out = []
    for gapped in queue.undelivered_rulings():
        if queue.ruling_delivered(gapped.request.id) is None:   # a resume above may have carried it
            out.append(deliver(queue, gapped.request.id, **kw))
    return out
