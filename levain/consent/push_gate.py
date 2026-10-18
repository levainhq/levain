"""levain.consent.push_gate — ``levain push``: the push executor's gated surface (Q14 B6).

A Bash ``git push`` is not gated (ruling 1: a shell command is never parsed; R-g). The gated path for the
irreversible tier is this command: the agent runs ``levain push --remote URL --branch NAME`` (through its shell,
like any command), and levain, not the agent, runs git (:func:`levain.consent.push.execute`). No hook sees a Bash
call, so the command gates ITSELF:

1. it reads the branch tip out of the repository from outside it (``git ls-remote`` on the path, which runs no
   repository hook), and the request it files names repo, canonical remote, branch AND that commit, so an answer
   allows exactly one commit; the executor then refuses to push any other;
2. a late ALLOW ruled on an earlier undecided identical push (same agent, session, input and repository) is taken
   first (:meth:`~levain.consent.queue.ConsentQueue.take_ruled_allow`, once); otherwise it files one ``levain_push``
   request on the consent queue, pings the operator (the adapter's ``notify``) and holds for a verified decision
   (the adapter's ``hold_for_answer``), up to the gate's D;
3. a verified allow: :func:`~levain.consent.push.execute`, whose fresh judgment under the per-repo lock re-reads the
   decision against a fresh replay (a key revoked meanwhile pushes nothing). A deny, a refusal, or the hold
   ending: the remote is untouched. At the end of the hold the request is marked undecided (gap #N, the session
   halted on it: a push is the irreversible tier) and the agent is told U-IRR.

Who can say yes: only a signature the queue verifies (an enrolled, unrevoked key whose class covers
:data:`PUSH_TIER`) or a late ruling the same way. This process never signs and holds no signing path; what it
reads from its own environment (which agent, which session) only names the request, never authorises it.

Known edges (one line each, not built against):

- a Bash ``git push`` stays ungated until a sandbox makes it an escape (R-g); this command is a path, not a wall.
- the hold is bounded by the agent's own shell timeout, which can be shorter than D [not measured here]: a SIGTERM
  or SIGHUP withdraws the request and nothing is pushed; a SIGKILL leaves it pending until it expires.
- with no session in the environment the session is :data:`NO_SESSION`, so gap numbers and ruled allows are shared
  by every such caller (a ruled allow still matches only the identical push).
- the push receipt is a line in ``<levain home>/push/receipts.jsonl`` beside the executor's journal, not a row in
  the queue's receipts.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from levain.consent import push
from levain.consent.adapters import claude_code, codex
from levain.consent.queue import ConsentQueue, Decision, Ruling, authority_source, default_queue_dir

__all__ = ["AGENT_LINE", "NO_SESSION", "PUSH_TIER", "PUSH_TOOL", "add_parser", "run"]

# the tool name a push request is filed under (never a shell tool's name: the queue shows it as structure)
PUSH_TOOL = "levain_push"
# The tier a push is filed at: an allow needs a key whose class covers it. T2, so a display-phone passkey (a key
# the operator's uid cannot hold) can allow a push while he is away; at T3 an away push could only be denied.
PUSH_TIER = "T2"
# the session a request names when the environment names none
NO_SESSION = "levain-push"
# the executor's mirror name for pushes allowed by an answer or a ruling (one mirror per repository)
ANSWERED = "answered"
# what an agent is told so it can choose this over ``git push`` (the line a template or brief carries)
AGENT_LINE = ("To push a branch, run `levain push --remote <url> --branch <name>` from the repository (Bash timeout "
              "600000): it asks the operator and pushes only on a signed yes. Do not run `git push` yourself.")

_CONTROL = re.compile("[\x00-\x1f\x7f]")
_ADAPTERS = {"claude_code": claude_code, "codex": codex}

EXIT_PUSHED, EXIT_NOT_TAKEN, EXIT_UNDECIDED, EXIT_UNKNOWN = 0, 1, 3, 4


class _Interrupted(Exception):
    pass


def _agent_and_session(agent: str | None, session: str | None) -> tuple[str, str]:
    """The agent and session the request names: the flags, else the agent's own environment. Naming only."""
    env = os.environ
    if agent is None:
        if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
            agent = "claude_code"
        elif env.get("CODEX_THREAD_ID") or env.get("CODEX_SANDBOX"):
            agent = "codex"
        else:
            raise ValueError("cannot tell which agent is calling; pass --agent claude_code or --agent codex")
    if agent not in _ADAPTERS:
        raise ValueError(f"--agent must be one of {sorted(_ADAPTERS)}")
    if session is None:
        session = env.get("CLAUDE_CODE_SESSION_ID") if agent == "claude_code" else env.get("CODEX_THREAD_ID")
    return agent, session or NO_SESSION


def _branch_tip(real: str, branch: str) -> str:
    """The commit ``refs/heads/<branch>`` names in the repository at ``real``, read from outside it."""
    r = push._git(["git", "ls-remote", "--", real, f"refs/heads/{branch}"], timeout=60, protocols="file")
    if r.returncode:
        raise push.PushRefused(f"could not read branch {branch} of {real}: {r.stderr.strip()[-300:]}")
    for line in r.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}" and push._SHA.match(sha):
            return sha
    raise push.PushRefused(f"{real} has no branch {branch}")


def _what(call: dict[str, str]) -> str:
    return _CONTROL.sub("?", f"push of {call['branch']} at {call['commit'][:12]} to {call['remote']} "
                             f"(from {call['repo']})")


def _signable(settings: Any) -> bool:
    """The cockpit the settings name replays to at least one active key: something can answer."""
    root = settings.cockpit_data_root
    if not root or not (Path(root) / "cockpit.db").is_file():
        return False
    from levain.authority import anchor_path

    try:
        return bool(authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))().active_keys())
    except Exception:
        return False


def _queue(qdir: Path, settings: Any) -> ConsentQueue:
    from levain.authority import anchor_path

    root = Path(settings.cockpit_data_root)
    return ConsentQueue(qdir, authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))


def _receipt(home: Path, extra: dict[str, Any]):
    def write(record: dict[str, Any]) -> None:
        path = home / "push" / "receipts.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps({**record, **extra, "at": time.time()}, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    return write


def _hold(queue: ConsentQueue, request: Any, wait: float, call: dict[str, str]) -> Decision | None:
    """``hold_for_answer`` with SIGTERM / SIGHUP turned into a withdraw: a caller killed by its shell's timeout
    leaves no pending request behind."""
    def stop(signum: int, _frame: Any) -> None:
        raise _Interrupted(signal.Signals(signum).name)

    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        got, _why = claude_code.hold_for_answer(queue, request, wait, tool_input=call, posture=None)
        return got
    except (_Interrupted, KeyboardInterrupt):
        queue.withdraw(request.id, note="levain push was interrupted while it held")
        raise
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def run(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        agent, session = _agent_and_session(args.agent, args.session)
        if args.hold is not None and not 0 < args.hold < float("inf"):
            raise ValueError("--hold must be a positive number of seconds")
        canon = push.canonical_remote(args.remote)
        if problem := push.branch_problem(args.branch):
            raise push.PushRefused(f"branch: {problem}")
        real, _ident = push._repo_topology(os.path.abspath(args.repo))
        call = {"repo": real, "remote": canon, "branch": args.branch, "commit": _branch_tip(real, args.branch)}
    except (ValueError, push.PushRefused) as exc:
        say(f"levain push: refused, nothing was pushed: {exc}")
        return EXIT_NOT_TAKEN
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    home = qdir.parent
    adapter = _ADAPTERS[agent]
    settings = adapter.load_settings(qdir)
    what = _what(call)
    if not _signable(settings):
        say(f"levain push: NOT taken: {what}. No enrolled key can answer (the queue's {adapter.SETTINGS_NAME} "
            "names no cockpit with an active key), so nothing can allow it.")
        return EXIT_NOT_TAKEN
    queue = _queue(qdir, settings)
    got: Decision | None = queue.take_ruled_allow(agent, session, PUSH_TOOL, call, cwd=real)
    if got is None:
        hold = min(args.hold if args.hold is not None else settings.gate_hold_seconds, claude_code.GATE_HOLD_CEILING)
        request = queue.file(agent=agent, agent_session_id=session, tool_name=PUSH_TOOL, tool_input=call, cwd=real,
                             hold=timedelta(seconds=hold), tier=PUSH_TIER)
        say(f"levain push: waiting up to {hold:g} s for the operator to allow the {what} (request {request.id}).")
        claude_code.notify(settings, f"levain: {agent} asks to push, up to {hold:g} s: {what} "
                                     f"(request {request.id}).", cwd=qdir)
        try:
            got = _hold(queue, request, hold + 5.0, call)
        except (_Interrupted, KeyboardInterrupt):
            say(f"levain push: interrupted; the request was withdrawn and nothing was pushed: {what}.")
            return EXIT_NOT_TAKEN
        if got is None:
            marked = queue.mark_undecided(request.id, halt=True, tool_input=call)
            if isinstance(marked.decision, Decision):
                got = marked.decision            # an answer that verified in the hold's last moments
            elif marked.state == "undecided" and marked.gap is not None:
                say(claude_code.U_IRR.format(x=what, n=marked.gap))
                return EXIT_UNDECIDED
            else:
                say(f"levain push: NOT taken: {what} ({marked.reason}). Do not retry it and do not achieve it "
                    "another way.")
                return EXIT_NOT_TAKEN
    if got.decision != "allow":
        say(f"levain push: DENIED by a signed levain answer (key {got.signer_fp}); nothing was pushed: {what}. "
            "Do not retry it and do not achieve it another way.")
        return EXIT_NOT_TAKEN
    decided = got

    def refusal(*, path: str, remote: str, branch: str) -> tuple[str, int] | str:
        # the fresh judgment under the per-repo lock: the same call, and its allow still verifies now
        if (path, remote, branch) != (call["repo"], call["remote"], call["branch"]):
            return "the push is not the one that was allowed"
        now = queue.decision(decided.request_id, tool_input=call)
        if isinstance(now, Decision) and now.decision == "allow" and now.request_digest == decided.request_digest:
            return ANSWERED, 0
        if isinstance(now, Ruling) and now.ruling == "allow" and now.request_digest == decided.request_digest:
            return ANSWERED, 0
        return "the allow no longer verifies (a key revoked, or the record changed)"

    try:
        result = push.execute({"id": ANSWERED}, repo_path=real, remote=canon, branch=call["branch"],
                              levain_home=home, refusal=refusal, expect_sha=call["commit"],
                              receipt=_receipt(home, {"request_id": decided.request_id, "via": decided.via,
                                                      "signer_fp": decided.signer_fp}))
    except push.PushRefused as exc:
        say(f"levain push: allowed, but refused before pushing; nothing was pushed: {exc}")
        return EXIT_NOT_TAKEN
    except (push.PushOutcomeUnknown, push.PushRecordIncomplete) as exc:
        say(f"levain push: {exc}")
        return EXIT_UNKNOWN if isinstance(exc, push.PushOutcomeUnknown) else EXIT_PUSHED
    how = (f"your ruling on gap #{decided.gap}" if decided.via == "ruling"
           else f"a signed answer (key {decided.signer_fp})")
    if result.ok:
        say(f"levain push: pushed {result.branch} {result.old_sha or '?'}..{result.new_sha} to {result.remote} "
            f"({result.status}), allowed by {how}.")
        return EXIT_PUSHED
    say(f"levain push: git refused the push ({result.status}): {result.detail}. Nothing was forced.")
    return EXIT_NOT_TAKEN


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """``levain push``, under the top-level subparsers."""
    p = subparsers.add_parser(
        "push", help="Push a branch only on the operator's signed yes (the gated push).",
        description="Ask the operator to allow pushing BRANCH of this repository (its current commit) to REMOTE, "
                    "wait for a signed answer, and push only on an allow: levain runs git itself, never a force, "
                    "a delete or a tag. A deny, or no answer before the hold ends, pushes nothing; the hold's end "
                    "leaves the push open as a numbered gap the operator can rule on later (run the same command "
                    "again after an ALLOW ruling). Exit 0 pushed, 1 not pushed, 3 undecided, 4 outcome unknown.")
    p.add_argument("--remote", required=True, help="The remote URL (ssh://, scp-form, https:// or file:///).")
    p.add_argument("--branch", required=True, help="The branch to push, same name on the remote.")
    p.add_argument("--repo", default=".", help="The repository (default: the current directory).")
    p.add_argument("--hold", type=float, default=None,
                   help="Seconds to wait for an answer (default: the consent settings' gate hold).")
    p.add_argument("--agent", default=None, choices=sorted(_ADAPTERS),
                   help="Which agent asks (default: read from the agent's environment).")
    p.add_argument("--session", default=None, help="The agent session the request names (default: from the "
                   "agent's environment).")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    p.set_defaults(func=run)
