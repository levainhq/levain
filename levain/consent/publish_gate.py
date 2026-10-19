"""levain.consent.publish_gate — ``levain publish``: the PyPI / npm publish executor's gated path (Q20).

A Bash ``twine upload`` or ``npm publish`` is not gated (ruling 1: a shell command is never parsed; R-g).
``levain publish`` is the PATH a cooperative agent is told to take: it shows the operator exactly which package,
version and index will be published and uploads only that, only on his signed yes. It is not a boundary (R-k): it
runs as a child of the agent's own shell, and an agent that wants to publish without asking can. The flow is
``levain push``'s (:mod:`levain.consent.push_gate`):

1. the artifacts are copied into a levain-owned snapshot and read there (:func:`levain.consent.publish.snapshot`):
   the request names the index, the canonical target, the package and version read out of the artifact, and each
   file's name, sha256 and size, so an answer allows exactly those bytes to exactly that index;
2. a late ALLOW ruled on an earlier undecided identical publish is taken first; a late DENY ruled on the newest
   such publish refuses without filing or pinging again; otherwise it files one ``levain_publish`` request at
   :data:`PUBLISH_TIER`, pings the operator and holds for a verified decision, up to the push hold
   (``--hold``, or ``push_hold_seconds`` in the agent's consent settings, never past the gate's ceiling);
3. any decision is acted on only when the request it decides is a ``levain_publish`` request at
   :data:`PUBLISH_TIER` whose display reads back as exactly this publish;
4. a verified allow: :func:`~levain.consent.publish.execute`, whose fresh judgment under the per-target lock re-reads
   the decision against a fresh replay and re-hashes the snapshot. A deny, a refusal, or the hold ending: nothing
   is uploaded. At the end of the hold the request is marked undecided (gap #N, the session halted on it: a
   publish is the irreversible tier) and the agent is told U-IRR.

Who can say yes, and what an interruption does, are ``levain push``'s: a signature the queue verifies or a late
ruling the same way; withdrawn while pending, outcome unknown (exit 4) during the executor.

Known edges (one line each, not built against):

- a Bash ``twine upload`` / ``npm publish`` stays ungated until Levain's hands run as another user (R-g, R-k).
- the queue directory, agent and session are the caller's word, as for ``levain push`` (R-k).
- the request's working directory is the first artifact's directory: the same files run from another directory
  are another request, and do not take its ruled allow.
- the hold is ``levain push``'s setting (``push_hold_seconds``); publish has none of its own.
- a ruled allow is consumed when taken, so an executor that then refuses (the lock, a missing twine) spends it.
- the publish receipt is a line in ``<levain home>/publish/receipts.jsonl`` beside the executor's journal, not a
  row in the queue's receipts.

Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from levain.consent import publish, push
from levain.consent.adapters import claude_code
from levain.consent.listing import filed_input
from levain.consent.push_gate import NO_SESSION as PUSH_NO_SESSION
from levain.consent.push_gate import (
    _ADAPTERS, PUSH_HOLD, PUSH_TIER, _agent_and_session, _catch_signals, _Interrupted, _push_hold, _queue, _withdraw,
)
from levain.consent.queue import ConsentQueue, Decision, Ruling, authority_source, default_queue_dir
from levain.consent.record import redact_display, tool_input_digest

__all__ = ["AGENT_LINE", "NO_SESSION", "PUBLISH_TIER", "PUBLISH_TOOL", "add_parser", "publish_what", "run"]

# the tool name a publish request is filed under (never a shell tool's name: the queue shows it as structure)
PUBLISH_TOOL = "levain_publish"
# push's tier, for push's reason: a display-phone passkey can allow a publish while the operator is away
PUBLISH_TIER = PUSH_TIER
# the session a request names when the environment names none
NO_SESSION = "levain-publish"
# what an agent is told so it can choose this over ``twine upload`` / ``npm publish``
AGENT_LINE = ("To publish a package, run `levain publish pypi dist/<files>` or `levain publish npm <pack.tgz>` "
              "(Bash timeout 600000): it asks the operator and uploads only on a signed yes. Do not run `twine "
              "upload` or `npm publish` yourself.")

EXIT_PUBLISHED, EXIT_NOT_TAKEN, EXIT_UNDECIDED, EXIT_UNKNOWN = 0, 1, 3, 4


def publish_what(call: dict[str, Any]) -> str:
    """The publish in the words the operator is pinged with."""
    names = ", ".join(f["name"] for f in call["files"])
    npm = f", dist-tag {call['tag']}, access {call['access']}" if call["index"] == "npm" else ""
    return f"publish of {call['package']} {call['version']} to {call['target']} ({names}{npm})"


def _signable(settings: Any) -> bool:
    """The cockpit the settings name replays to at least one active key: something can answer."""
    root = settings.cockpit_data_root
    if not root or not (Path(root) / "cockpit.db").is_file():
        return False
    from levain.authority import anchor_path

    try:
        return bool(authority_source(Path(root) / "cockpit.db", anchor=anchor_path(root))().active_keys())
    except Exception as exc:  # noqa: BLE001 - no key can answer; the cause is said, never swallowed
        print(f"levain publish: the cockpit at {root} could not be read: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return False


def _receipt(home: Path, extra: dict[str, Any]):
    def write(record: dict[str, Any]) -> None:
        push._private_dir(home / "publish")
        push.append_line(home / "publish" / "receipts.jsonl", {**record, **extra, "at": time.time()})
    return write


def _filed_for(queue: ConsentQueue, got: Decision | Ruling, call: dict[str, Any]) -> bool:
    """The decision decides a ``levain_publish`` request at :data:`PUBLISH_TIER` whose display reads back as exactly
    ``call`` and whose digest is ``call``'s: what the operator was shown is what would be published."""
    found = queue.get(got.request_id)
    if found is None:
        return False
    request = found[0]
    return (request.tool_name == PUBLISH_TOOL and request.tier == PUBLISH_TIER
            and request.digest == got.request_digest and request.tool_input_digest == tool_input_digest(call)
            and filed_input(request) == call)


def _late_deny(queue: ConsentQueue, agent: str, session: str, call: dict[str, Any], cwd: str) -> int | None:
    """The gap number when the newest gap this session gave this exact publish is a verified DENY ruling filed for
    exactly this call."""
    digest, newest, n = tool_input_digest(call), None, 1
    while (g := queue.request_by_gap(agent, session, n)) is not None:
        if (g.request.tool_name, g.request.tool_input_digest, g.request.cwd) == (PUBLISH_TOOL, digest, cwd):
            newest = g
        n += 1
    if newest is None or newest.state != "denied":
        return None
    ruled = queue.decision(newest.request.id)
    if isinstance(ruled, Ruling) and ruled.ruling == "deny" and _filed_for(queue, ruled, call):
        return newest.gap
    return None


def run(args: argparse.Namespace, out: Any = None) -> int:
    out = out if out is not None else sys.stdout

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    snap: Path | None = None
    what = "publish"
    # installed before the snapshot is taken, so a SIGTERM while copying still removes it (a SIGKILL leaves it for
    # the next snapshot's sweep)
    old = _catch_signals()
    try:
        try:
            agent, session = _agent_and_session(args.agent, args.session)
            session = NO_SESSION if session == PUSH_NO_SESSION else session
            if args.hold is not None and not 0 < args.hold < float("inf"):
                raise ValueError("--hold must be a positive number of seconds")
            qdir = args.queue_dir.expanduser().resolve() if args.queue_dir is not None else default_queue_dir()
            target = args.target or publish.DEFAULT_TARGETS[args.index]
            if args.index == "pypi":     # no credential: refused now, not after the operator's yes
                publish.pypi_credentials(publish.canonical_target(target))
            call, snap = publish.snapshot(args.index, list(args.files), target, qdir.parent)
            cwd = os.path.dirname(os.path.realpath(args.files[0]))
            if filed_input(SimpleNamespace(tool_name=PUBLISH_TOOL, display=redact_display(PUBLISH_TOOL, call))) != call:
                raise ValueError("the publish is too long to show the operator whole")
        except (ValueError, OSError) as exc:            # PublishRefused is a ValueError
            say(f"levain publish: refused, nothing was published: {exc}")
            return EXIT_NOT_TAKEN
        adapter = _ADAPTERS[agent]
        settings = adapter.load_settings(qdir)
        what = publish_what(call)
        if not _signable(settings):
            say(f"levain publish: NOT taken: {what}. No enrolled key can answer (the queue's {adapter.SETTINGS_NAME} "
                "names no cockpit with an active key), so nothing can allow it.")
            return EXIT_NOT_TAKEN
        queue = _queue(qdir, settings)
        return _gated(args, queue, adapter, settings, agent, session, call, cwd, snap, qdir, what, say)
    except (_Interrupted, KeyboardInterrupt):    # the executor reports its own; this is before it ran
        say(f"levain publish: interrupted; nothing was published: {what}.")
        return EXIT_NOT_TAKEN
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        if snap is not None:
            shutil.rmtree(snap, ignore_errors=True)


def _gated(args: argparse.Namespace, queue: ConsentQueue, adapter: Any, settings: Any, agent: str, session: str,
           call: dict[str, Any], cwd: str, snap: Path, qdir: Path, what: str, say: Any) -> int:
    """From the ruled-allow take to the executor's result, with the interruption handlers installed."""
    got: Decision | None = queue.take_ruled_allow(agent, session, PUBLISH_TOOL, call, cwd=cwd)
    if got is None:
        if (gap := _late_deny(queue, agent, session, call, cwd)) is not None:
            say(f"levain publish: NOT taken: the operator ruled DENY on this exact publish (gap #{gap}); nothing was "
                f"published and nothing was asked again: {what}. Do not retry it and do not achieve it another way.")
            return EXIT_NOT_TAKEN
        hold = min(args.hold if args.hold is not None else _push_hold(qdir, adapter), claude_code.GATE_HOLD_CEILING)
        request = queue.file(agent=agent, agent_session_id=session, tool_name=PUBLISH_TOOL, tool_input=call, cwd=cwd,
                             hold=timedelta(seconds=hold), tier=PUBLISH_TIER)
        # from here until a verified decision or the undecided mark, any exit withdraws the request
        try:
            say(f"levain publish: waiting up to {hold:g} s for the operator to allow the {what} (request "
                f"{request.id}). Let it finish: stopping it before then withdraws the request, or on a kill leaves "
                "it pending to its expiry.")
            claude_code.notify(settings, f"levain: {agent} asks to publish, up to {hold:g} s: {what} "
                                         f"(request {request.id}).", cwd=qdir)
            got, _why = adapter.hold_for_answer(queue, request, hold + 5.0, tool_input=call, posture=None)
            marked = None if got is not None else queue.mark_undecided(request.id, halt=True, tool_input=call)
        except BaseException as exc:
            withdrawn = _withdraw(queue, request.id, "levain publish stopped while it held")
            if not isinstance(exc, (_Interrupted, KeyboardInterrupt)):
                raise
            if withdrawn:
                say(f"levain publish: interrupted; the request was withdrawn and nothing was published: {what}.")
            else:
                say(f"levain publish: interrupted; nothing was published, but the request could not be withdrawn (it "
                    f"was answered or ended meanwhile; this run acts on nothing): {what}.")
            return EXIT_NOT_TAKEN
        if marked is not None:
            if isinstance(marked.decision, Decision):
                got = marked.decision            # an answer that verified in the hold's last moments
            elif marked.state == "undecided" and marked.gap is not None:
                say(claude_code.U_IRR.format(x=what, n=marked.gap))
                return EXIT_UNDECIDED
            else:
                say(f"levain publish: NOT taken: {what} ({marked.reason}). Do not retry it and do not achieve it "
                    "another way.")
                return EXIT_NOT_TAKEN
    if not _filed_for(queue, got, call):
        say(f"levain publish: NOT taken: the decision found is not the publish it was filed for (its request shows "
            f"another publish, or another tier); nothing was published: {what}.")
        return EXIT_NOT_TAKEN
    if got.decision != "allow":
        say(f"levain publish: DENIED by a signed levain answer (key {got.signer_fp}); nothing was published: "
            f"{what}. Do not retry it and do not achieve it another way.")
        return EXIT_NOT_TAKEN
    return _execute(queue, got, call, snap, qdir.parent, say)


def _execute(queue: ConsentQueue, decided: Decision, call: dict[str, Any], snap: Path, home: Path, say: Any,
             **inject: Any) -> int:
    def refusal(now_call: dict[str, Any]) -> str | None:
        # the fresh judgment under the per-target lock: the same call, and its allow still verifies now
        if now_call != call:
            return "the publish is not the one that was allowed"
        now = queue.decision(decided.request_id, tool_input=call)
        if isinstance(now, Decision) and now.decision == "allow" and now.request_digest == decided.request_digest:
            return None
        if isinstance(now, Ruling) and now.ruling == "allow" and now.request_digest == decided.request_digest:
            return None
        return "the allow no longer verifies (a key revoked, or the record changed)"

    what = publish_what(call)
    try:
        result = publish.execute(call, snap=snap, levain_home=home, refusal=refusal,
                                 receipt=_receipt(home, {"request_id": decided.request_id, "via": decided.via,
                                                         "signer_fp": decided.signer_fp}), **inject)
    except publish.PublishRefused as exc:
        say(f"levain publish: allowed, but refused before uploading; nothing was published: {exc}")
        return EXIT_NOT_TAKEN
    except publish.PublishOutcomeUnknown as exc:
        say(f"levain publish: {exc}")
        return EXIT_UNKNOWN
    except publish.PublishRecordIncomplete as exc:
        r, errs = exc.result, "; ".join(exc.record_errors)
        if r.ok:
            say(f"levain publish: published {r.package} {r.version} to {r.target}, but its record is incomplete: "
                f"{errs}")
            return EXIT_PUBLISHED
        say(f"levain publish: the upload failed (exit {r.exit}): {r.detail}. If it failed after the upload began, "
            f"the index may already hold it. Its record is incomplete: {errs}")
        return EXIT_NOT_TAKEN
    except (OSError, subprocess.SubprocessError) as exc:   # the executor failed before it uploaded
        say(f"levain publish: allowed, but refused before uploading; nothing was published: "
            f"{type(exc).__name__}: {exc}")
        return EXIT_NOT_TAKEN
    except (_Interrupted, KeyboardInterrupt):
        # the executor turns an interruption after its intent line into PublishOutcomeUnknown: this one came before
        say(f"levain publish: interrupted before the upload started; nothing was published: {what}.")
        return EXIT_NOT_TAKEN
    how = (f"your ruling on gap #{decided.gap}" if decided.via == "ruling"
           else f"a signed answer (key {decided.signer_fp})")
    if result.ok:
        say(f"levain publish: published {result.package} {result.version} to {result.target}, allowed by {how}.")
        return EXIT_PUBLISHED
    say(f"levain publish: the upload failed (exit {result.exit}): {result.detail}. If it failed after the upload "
        f"began, the index may already hold {result.package} {result.version}: check it before publishing again.")
    return EXIT_NOT_TAKEN


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """``levain publish``, under the top-level subparsers."""
    p = subparsers.add_parser(
        "publish", help="Publish a package to PyPI or npm only on the operator's signed yes (the gated path).",
        description="Ask the operator to allow publishing these artifacts (the package and version are read out of "
                    "them) to the index, wait for a signed answer, and upload exactly those bytes only on an allow. "
                    "A deny, or no answer before the hold ends, publishes nothing; the hold's end leaves the publish "
                    "open as a numbered gap the operator can rule on later (run the same command again after an "
                    "ALLOW ruling). A path a cooperative agent takes, not a boundary: it runs as the agent. "
                    "Exit 0 published, 1 not published, 3 undecided, 4 outcome unknown.")
    sub = p.add_subparsers(dest="index", required=True, metavar="{pypi,npm}")
    for index, files_help, flag, flag_help in (
            ("pypi", "The wheel(s) and/or sdist of ONE project version.", "--repository-url",
             f"The upload URL (default {publish.DEFAULT_TARGETS['pypi']})."),
            ("npm", "The tarball npm pack made.", "--registry",
             f"The registry (default {publish.DEFAULT_TARGETS['npm']}).")):
        s = sub.add_parser(index, help=f"Publish to {'PyPI' if index == 'pypi' else 'npm'}.")
        s.add_argument("files", nargs="+" if index == "pypi" else 1, metavar="FILE", help=files_help)
        s.add_argument(flag, dest="target", default=None, help=flag_help)
        s.add_argument("--hold", type=float, default=None,
                       help=f"Seconds to wait for an answer (default: push_hold_seconds in the agent's consent "
                            f"settings, else {PUSH_HOLD:g}; at most {claude_code.GATE_HOLD_CEILING:g}).")
        s.add_argument("--agent", default=None, choices=sorted(_ADAPTERS),
                       help="Which agent asks (default: read from the agent's environment).")
        s.add_argument("--session", default=None, help="The agent session the request names (default: from the "
                       "agent's environment).")
        s.add_argument("--queue-dir", dest="queue_dir", type=Path, default=None,
                       help="The consent queue's directory (default: $LEVAIN_HOME/consent, else ~/.levain/consent).")
        s.set_defaults(func=run)
