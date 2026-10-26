"""levain.consent.push_gate — ``levain push``: the push executor's gated path (Q14 B6).

A Bash ``git push`` is not gated (ruling 1: a shell command is never parsed; R-g). ``levain push`` is the PATH a
cooperative agent is told to take for the irreversible tier: it shows the operator exactly what will be pushed and
pushes only that, only on his signed yes. It is not a boundary (R-k): it runs as a child of the agent's own shell,
so its queue, settings, cockpit and code are all within the agent's reach, and an agent that wants to push without
asking can. The wall is Levain's hands as another OS user, not this command. No hook sees a Bash call, so the
command gates ITSELF:

1. it reads the branch tip out of the repository from outside it (``git ls-remote`` on the path, which runs no
   repository hook), and the request it files names repo, canonical remote, branch AND that commit, so an answer
   allows exactly one commit; the executor then refuses to push any other;
2. a late ALLOW ruled on an earlier undecided identical push (same agent, session, input and repository) is taken
   first (:meth:`~levain.consent.queue.ConsentQueue.take_ruled_allow`, once); a late DENY ruled on the newest
   such push refuses without filing or pinging again; otherwise it files one ``levain_push`` request on the
   consent queue, pings the operator (``notify``) and holds for a verified decision (the agent's adapter's
   ``hold_for_answer``), up to :data:`PUSH_HOLD` (``--hold``, or ``push_hold_seconds`` in the agent's consent
   settings, never past the gate's ceiling);
3. any decision is acted on only when the request it decides is a ``levain_push`` request at :data:`PUSH_TIER`
   whose display reads back as exactly this push (the answer binds the digest; this ties the display the
   operator read to it);
4. a verified allow: :func:`~levain.consent.push.execute`, whose fresh judgment under the per-repo lock re-reads
   the decision against a fresh replay (a key revoked meanwhile pushes nothing). A deny, a refusal, or the hold
   ending: the remote is untouched. At the end of the hold the request is marked undecided (gap #N, the session
   halted on it: a push is the irreversible tier) and the agent is told U-IRR.

Who can say yes on this path: a signature the queue verifies (an enrolled, unrevoked key whose class covers
:data:`PUSH_TIER`) or a late ruling the same way. This process never signs; which agent and which session it
names come from its caller and only route the request, never authorise it.

Interruptions (SIGTERM, SIGHUP, Ctrl-C), caught from before the request is filed until the executor returns: while
the request is pending it is withdrawn (any other failure there withdraws it too, the failure re-raised) and
nothing is pushed; during the executor the outcome is reported unknown (exit 4), since the push may have started.

Known edges (one line each, not built against):

- a Bash ``git push`` stays ungated until Levain's hands run as another user (R-g, R-k); this command is a path.
- the queue directory, agent and session are the caller's word (``--queue-dir``, ``LEVAIN_HOME``, ``--agent``,
  ``--session``): a caller can name another queue, or another session's id, and take that session's ruled allow
  for the byte-identical push, or halt that session on a request it leaves undecided (R-k).
- a shell that stops this command before the hold ends withdraws the request on SIGTERM and leaves it pending to
  expiry on SIGKILL. Claude Code 2.1.303's Bash timeout sends neither: it moves the command to the background, the
  hold runs to its end and the agent reads the result when it finishes [measured: B7 S6, 2026-10-18, a 6 s timeout
  on a 120 s hold; re-run that case on another Claude Code version before relying on it there].
- an interruption inside the executor before its intent line is still reported unknown (nothing was pushed then).
- off the main thread no signal can be caught, and a signal inside the filing itself cannot name the request: in
  both cases the request is left to its expiry.
- a ruled allow is consumed when taken, so an executor that then refuses (the branch moved, the lock) spends it.
- linked worktrees and submodules (a ``.git`` file) are refused: run it from the main working tree.
- the operator's ``~/.ssh/config`` (``Host`` aliases, ``ProxyCommand``) can send an ``ssh://`` remote's push to
  another host than the one he was shown; levain refuses git's URL rewrites but does not read ssh's config.
- with no session in the environment the session is :data:`NO_SESSION`, so gap numbers and ruled allows are shared
  by every such caller (a ruled allow still matches only the identical push).
- the newest gap is chosen by gap number: a deny applied later to an older gap is missed and the operator is
  simply asked again.
- a signal landing between ``queue.file`` returning and the cleanup ``try`` leaves the request to its expiry.
- a signal or crash between git returning and the record being written reports exit 4, outcome unknown.
- a ruled allow taken before ``_filed_for`` refuses it is spent.
- withdraw reports "answered or ended" also when the store errored.
- the push receipt is a line in ``<levain home>/push/receipts.jsonl`` beside the executor's journal, not a row in
  the queue's receipts.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from levain import unshowable
from levain.consent import push
from levain.consent.adapters import claude_code, codex
from levain.consent.listing import filed_input
from levain.consent.queue import ConsentQueue, Decision, Ruling, authority_source, default_queue_dir
from levain.consent.record import redact_display, tool_input_digest

__all__ = ["AGENT_LINE", "NO_SESSION", "PUSH_HOLD", "PUSH_TIER", "PUSH_TOOL", "add_parser", "push_call", "push_what", "run"]

# the tool name a push request is filed under (never a shell tool's name: the queue shows it as structure)
PUSH_TOOL = "levain_push"
# The tier a push is filed at: an allow needs a key whose class covers it. T2, so a display-phone passkey (a key
# the operator's uid cannot hold) can allow a push while he is away; at T3 an away push could only be denied (R-j).
PUSH_TIER = "T2"
# The default hold: short enough that an agent which runs this with its shell's default timeout still reaches
# the undecided gap (and a later ruling) instead of being killed mid-hold, which withdraws the request.
PUSH_HOLD = 90.0
# the session a request names when the environment names none
NO_SESSION = "levain-push"
# the executor's mirror name for pushes allowed by an answer or a ruling (one mirror per repository)
ANSWERED = "answered"
# what an agent is told so it can choose this over ``git push`` (the line a template or brief carries)
AGENT_LINE = ("To push a branch, run `levain push --remote <url> --branch <name>` from the repository (Bash timeout "
              "600000): it asks the operator and pushes only on a signed yes. Do not run `git push` yourself.")

_CONTROL = re.compile("[\x00-\x1f\x7f]")
# the agent's adapter: its own settings file and its hold. The ping, the U-IRR text and the hold ceiling used
# here are the Claude Code adapter's, for either agent
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


def _showable(name: str, text: str) -> None:
    """Refuse a field the operator would read with a character he cannot see (``ValueError``)."""
    found = unshowable.first_hidden(text, allow="", selectors=False)
    if found is not None:
        raise ValueError(f"the {name} carries an invisible or direction-changing character (U+{ord(found[1]):04X}); "
                         "the operator could not read what he is asked to allow")


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


def push_call(repo: str, remote: str, branch: str) -> dict[str, str]:
    """The exact call ``levain push --repo REPO --remote REMOTE --branch BRANCH`` files and takes a ruled allow for:
    the repository's real path, the canonical remote, the branch and its tip read from outside the repository. The
    one derivation of it, so a call filed ahead (``levain ask --would-push``) has the digest the push will have
    while the tip is unchanged. Raises ``ValueError`` (a :class:`~levain.consent.push.PushRefused` among them),
    ``OSError`` or ``subprocess.SubprocessError`` for a push that would be refused."""
    canon = push.canonical_remote(remote)
    _showable("branch", branch)
    if problem := push.branch_problem(branch):
        raise push.PushRefused(f"branch: {problem}")
    path = os.path.abspath(repo)
    _showable("repository path", path)
    real, _ident = push._repo_topology(path)
    _showable("repository path", real)
    _showable("remote", canon)
    call = {"repo": real, "remote": canon, "branch": branch, "commit": _branch_tip(real, branch)}
    if filed_input(SimpleNamespace(tool_name=PUSH_TOOL, display=redact_display(PUSH_TOOL, call))) != call:
        raise ValueError("the push is too long to show the operator whole")
    return call


def push_what(call: dict[str, str]) -> str:
    """The push in the words the operator is pinged with."""
    return _what(call)


def _signable(settings: Any) -> bool:
    """The cockpit the settings name replays to at least one active key: something can answer."""
    root = settings.cockpit_data_root
    if not root or not (Path(root) / "cockpit.db").is_file():
        return False
    from levain.authority import anchor_path

    try:
        return bool(authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))().active_keys())
    except Exception as exc:  # noqa: BLE001 - no key can answer; the cause is said, never swallowed
        print(f"levain push: the cockpit at {root} could not be read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def _queue(qdir: Path, settings: Any) -> ConsentQueue:
    from levain.authority import anchor_path

    root = Path(settings.cockpit_data_root)
    return ConsentQueue(qdir, home=Path.home(),
                        authority=authority_source(root / "cockpit.db", anchor=anchor_path(root)))


def _push_hold(qdir: Path, adapter: Any) -> float:
    """``push_hold_seconds`` from the agent's consent settings when it is a finite number > 0, else
    :data:`PUSH_HOLD`; never past the gate's ceiling."""
    try:
        got = json.loads((qdir / adapter.SETTINGS_NAME).read_text(encoding="utf-8")).get("push_hold_seconds")
    except (OSError, ValueError, RecursionError, AttributeError):
        got = None
    try:                                     # an integer past float range raises on both calls
        ok = isinstance(got, (int, float)) and not isinstance(got, bool) and math.isfinite(got) and got > 0
        hold = float(got) if ok else PUSH_HOLD
    except OverflowError:
        hold = PUSH_HOLD
    return min(hold, claude_code.GATE_HOLD_CEILING)


def _receipt(home: Path, extra: dict[str, Any]):
    def write(record: dict[str, Any]) -> None:
        push._private_dir(home / "push")
        push.append_line(home / "push" / "receipts.jsonl", {**record, **extra, "at": time.time()})
    return write


def _filed_for(queue: ConsentQueue, got: Decision | Ruling, call: dict[str, str]) -> bool:
    """The decision decides a ``levain_push`` request at :data:`PUSH_TIER` whose display reads back as exactly
    ``call`` and whose digest is ``call``'s: what the operator was shown is what would be pushed."""
    found = queue.get(got.request_id)
    if found is None:
        return False
    request = found[0]
    return (request.tool_name == PUSH_TOOL and request.tier == PUSH_TIER and request.digest == got.request_digest
            and request.tool_input_digest == tool_input_digest(call) and filed_input(request) == call)


def _late_deny(queue: ConsentQueue, agent: str, session: str, call: dict[str, str]) -> int | None:
    """The gap number when the newest gap this session gave this exact push is a verified DENY ruling that is
    filed for exactly this call (the same check an allow passes)."""
    digest, newest, n = tool_input_digest(call), None, 1
    while (g := queue.request_by_gap(agent, session, n)) is not None:
        if (g.request.tool_name, g.request.tool_input_digest, g.request.cwd) == (PUSH_TOOL, digest, call["repo"]):
            newest = g
        n += 1
    if newest is None or newest.state != "denied":
        return None
    ruled = queue.decision(newest.request.id)
    if isinstance(ruled, Ruling) and ruled.ruling == "deny" and _filed_for(queue, ruled, call):
        return newest.gap
    return None


def _withdraw(queue: ConsentQueue, request_id: str, note: str) -> bool:
    """Best effort: True only when the request was pending and is withdrawn now."""
    try:
        return queue.withdraw(request_id, note=note)
    except Exception:  # noqa: BLE001 - cleanup on a path already failing; the caller's exception stands
        return False


def _catch_signals() -> dict[int, Any]:
    """SIGTERM and SIGHUP raise :class:`_Interrupted` until restored; nothing is caught off the main thread."""
    def stop(signum: int, _frame: Any) -> None:
        raise _Interrupted(signal.Signals(signum).name)

    old: dict[int, Any] = {}
    try:
        for s in (signal.SIGTERM, signal.SIGHUP):
            old[s] = signal.signal(s, stop)
    except ValueError:                       # not the main thread
        pass
    return old


def run(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        agent, session = _agent_and_session(args.agent, args.session)
        if args.hold is not None and not 0 < args.hold < float("inf"):
            raise ValueError("--hold must be a positive number of seconds")
        call = push_call(args.repo, args.remote, args.branch)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:   # PushRefused is a ValueError
        say(f"levain push: refused, nothing was pushed: {exc}")
        return EXIT_NOT_TAKEN
    qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
    adapter = _ADAPTERS[agent]
    settings = adapter.load_settings(qdir)
    what = _what(call)
    if not _signable(settings):
        say(f"levain push: NOT taken: {what}. No enrolled key can answer (the queue's {adapter.SETTINGS_NAME} "
            "names no cockpit with an active key), so nothing can allow it.")
        return EXIT_NOT_TAKEN
    queue = _queue(qdir, settings)
    old = _catch_signals()
    try:
        return _gated(args, queue, adapter, settings, agent, session, call, qdir, what, say)
    except (_Interrupted, KeyboardInterrupt):    # the executor reports its own; this is before it ran
        say(f"levain push: interrupted; nothing was pushed: {what}.")
        return EXIT_NOT_TAKEN
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def _gated(args: argparse.Namespace, queue: ConsentQueue, adapter: Any, settings: Any, agent: str, session: str,
           call: dict[str, str], qdir: Path, what: str, say: Any) -> int:
    """From the ruled-allow take to the executor's result, with the interruption handlers installed."""
    real = call["repo"]
    got: Decision | None = queue.take_ruled_allow(agent, session, PUSH_TOOL, call, cwd=real)
    if got is None:
        if (gap := _late_deny(queue, agent, session, call)) is not None:
            say(f"levain push: NOT taken: the operator ruled DENY on this exact push (gap #{gap}); nothing was "
                f"pushed and nothing was asked again: {what}. Do not retry it and do not achieve it another way.")
            return EXIT_NOT_TAKEN
        hold = min(args.hold if args.hold is not None else _push_hold(qdir, adapter), claude_code.GATE_HOLD_CEILING)
        request = queue.file(agent=agent, agent_session_id=session, tool_name=PUSH_TOOL, tool_input=call, cwd=real,
                             hold=timedelta(seconds=hold), tier=PUSH_TIER)
        # from here until a verified decision or the undecided mark, any exit withdraws the request
        try:
            say(f"levain push: waiting up to {hold:g} s for the operator to allow the {what} (request {request.id}). "
                "Let it finish: stopping it before then withdraws the request, or on a kill leaves it pending to "
                "its expiry.")
            claude_code.notify(settings, f"levain: {agent} asks to push, up to {hold:g} s: {what} "
                                         f"(request {request.id}).", cwd=qdir, kind="push",
                               ref=f"request:{request.id}")
            got, _why = adapter.hold_for_answer(queue, request, hold + 5.0, tool_input=call, posture=None)
            marked = None if got is not None else queue.mark_undecided(request.id, halt=True, tool_input=call)
        except BaseException as exc:
            withdrawn = _withdraw(queue, request.id, "levain push stopped while it held")
            if not isinstance(exc, (_Interrupted, KeyboardInterrupt)):
                raise
            if withdrawn:
                say(f"levain push: interrupted; the request was withdrawn and nothing was pushed: {what}.")
            else:
                say(f"levain push: interrupted; nothing was pushed, but the request could not be withdrawn (it was "
                    f"answered or ended meanwhile; this run acts on nothing): {what}.")
            return EXIT_NOT_TAKEN
        if marked is not None:
            if isinstance(marked.decision, Decision):
                got = marked.decision            # an answer that verified in the hold's last moments
            elif marked.state == "undecided" and marked.gap is not None:
                say(claude_code.U_IRR.format(x=what, n=marked.gap))
                return EXIT_UNDECIDED
            else:
                say(f"levain push: NOT taken: {what} ({marked.reason}). Do not retry it and do not achieve it "
                    "another way.")
                return EXIT_NOT_TAKEN
    if not _filed_for(queue, got, call):
        say(f"levain push: NOT taken: the decision found is not the push it was filed for (its request shows "
            f"another push, or another tier); nothing was pushed: {what}.")
        return EXIT_NOT_TAKEN
    if got.decision != "allow":
        say(f"levain push: DENIED by a signed levain answer (key {got.signer_fp}); nothing was pushed: {what}. "
            "Do not retry it and do not achieve it another way.")
        return EXIT_NOT_TAKEN
    return _execute(queue, got, call, qdir.parent, say)


def _execute(queue: ConsentQueue, decided: Decision, call: dict[str, str], home: Path, say: Any) -> int:
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
        result = push.execute({"id": ANSWERED}, repo_path=call["repo"], remote=call["remote"], branch=call["branch"],
                              levain_home=home, refusal=refusal, expect_sha=call["commit"],
                              receipt=_receipt(home, {"request_id": decided.request_id, "via": decided.via,
                                                      "signer_fp": decided.signer_fp}))
    except push.PushRefused as exc:
        say(f"levain push: allowed, but refused before pushing; nothing was pushed: {exc}")
        return EXIT_NOT_TAKEN
    except push.PushOutcomeUnknown as exc:
        say(f"levain push: {exc}")
        return EXIT_UNKNOWN
    except push.PushRecordIncomplete as exc:
        r, errs = exc.result, "; ".join(exc.record_errors)
        if r.ok:
            say(f"levain push: pushed {r.branch} {r.old_sha or '?'}..{r.new_sha} to {r.remote} ({r.status}), but "
                f"its record is incomplete: {errs}")
            return EXIT_PUSHED
        say(f"levain push: git refused the push ({r.status}): {r.detail}. Nothing was forced. Its record is "
            f"incomplete: {errs}")
        return EXIT_NOT_TAKEN
    except (OSError, subprocess.SubprocessError) as exc:   # the executor failed before it pushed
        say(f"levain push: allowed, but refused before pushing; nothing was pushed: {type(exc).__name__}: {exc}")
        return EXIT_NOT_TAKEN
    except (_Interrupted, KeyboardInterrupt):
        say(f"levain push: interrupted while pushing; the outcome is UNKNOWN (the push may have started): "
            f"{_what(call)}. Check the remote; levain's journal is under {home / 'push'}.")
        return EXIT_UNKNOWN
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
        "push", help="Push a branch only on the operator's signed yes (the gated push path).",
        description="Ask the operator to allow pushing BRANCH of this repository (its current commit) to REMOTE, "
                    "wait for a signed answer, and push exactly that only on an allow: never a force, a delete or "
                    "a tag. A deny, or no answer before the hold ends, pushes nothing; the hold's end leaves the "
                    "push open as a numbered gap the operator can rule on later (run the same command again after "
                    "an ALLOW ruling). A path a cooperative agent takes, not a boundary: it runs as the agent. "
                    "Exit 0 pushed, 1 not pushed, 3 undecided, 4 outcome unknown.")
    p.add_argument("--remote", required=True, help="The remote URL (ssh://, scp-form, https:// or file:///).")
    p.add_argument("--branch", required=True, help="The branch to push, same name on the remote.")
    p.add_argument("--repo", default=".", help="The repository (default: the current directory).")
    p.add_argument("--hold", type=float, default=None,
                   help=f"Seconds to wait for an answer (default: push_hold_seconds in the agent's consent settings, "
                        f"else {PUSH_HOLD:g}; at most {claude_code.GATE_HOLD_CEILING:g}).")
    p.add_argument("--agent", default=None, choices=sorted(_ADAPTERS),
                   help="Which agent asks (default: read from the agent's environment).")
    p.add_argument("--session", default=None, help="The agent session the request names (default: from the "
                   "agent's environment).")
    p.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                   help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
    p.set_defaults(func=run)
