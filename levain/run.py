"""levain.run — the DRIVERS for a runnable, ISOLATED Levain entity.

``levain run <entity_dir>`` is the "use it like Claude Code, but sovereign" surface: a
partner-entity that runs on an open model (Ollama by default), carries its OWN memory, and —
the #1 requirement (Phill, 2026-07-07) — MUST NOT touch the operator-laptop flow store.

**This module holds DRIVERS, not the entity.** The session itself — construction, the
sovereignty guards, and the one ``run_turn`` operation — lives in :mod:`levain.session`
(the K1 keystone). Two drivers live here:

  - :func:`run_entity` — the interactive REPL (a human at a tty).
  - :func:`run_task` — the NON-INTERACTIVE runner (``--task``): one spec in, run to
    completion, exit with a code the caller can branch on. This is what makes an entity
    drivable by a script, a scheduler, or a supervisor instead of only by a person.

The split is the whole point of K1: the web cockpit's ``/turn`` route and an unattended
constellation seat are two more drivers over the SAME session, not two more re-implementations
of it. BUILD ONCE, UNLOCK THREE.

The capability posture (the ⚛️ physicist-lens Sharpening, 2026-07-08 — "don't certify
sovereignty once at startup; mint a narrow authority bundle, enforce at the point of use") is
unchanged and enforced in :meth:`levain.session.EntitySession.open`:

  - **store** — per-op re-guarded by ``AnnealEntityFiring`` (the chokepoint owns it).
  - **files** — the OpenHands ``workspace`` is confined to ``<entity>/workspace/``, enforced by
    ``assert_workspace_isolated`` before the workspace is created AND again after (TOCTOU), plus
    per-op by the confined file tool.
  - **tools** — confined HANDS (file editor + OS-sandboxed bash), both fenced to the shared
    crown-jewels FLOOR; ``--no-tools`` for a pure conversational partner.
  - **relay** — none wired.

Requires the ``openhands`` extra (``pip install 'levain[openhands]'``). The heavy imports are
LAZY (inside the session's ``open``), so ``levain --help`` and ``import levain.run`` work
without the extra; a missing extra becomes a friendly one-line install hint.
"""
from __future__ import annotations

import sys
from pathlib import Path

from levain.firing.deadline import TurnDeadline, TurnTimeout, format_timeout_report
from levain.firing.gate import PendingEfferent
from levain.session import (
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    EntitySession,
    SessionStartError,
    TurnResult,
    WORKSPACE_SUBDIR,
    latest_agent_text,
    require_openhands_entity,
    resolve_llm_kwargs,
    resolve_model_label,
    turn_tool_activity,
)
from levain.turn_input import TurnReader

__all__ = ["run_entity", "run_task", "require_openhands_entity"]

# Back-compat aliases: these were private helpers of this module before the K1 extraction moved
# them into `levain.session`. They are re-exported under their original names so the existing
# test suite and any in-tree caller keep working against one implementation — the extraction
# must not fork the turn-boundary logic, which is exactly the class of drift `agent_reply`
# exists to forbid.
_WORKSPACE_SUBDIR = WORKSPACE_SUBDIR
_resolve_model = resolve_model_label
_resolve_llm_kwargs = resolve_llm_kwargs
_latest_agent_text = latest_agent_text
_turn_tool_activity = turn_tool_activity


# ---------------------------------------------------------------------------
# Driver 1 — the interactive REPL
# ---------------------------------------------------------------------------

def run_entity(
    path: Path,
    *,
    model: str = "glm-5.2:cloud",
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
    with_tools: bool = True,
) -> int:
    """Run the interactive REPL for the isolated entity at ``path``.

    ``with_tools`` (default True) grants the entity its confined HANDS; ``False``
    (``--no-tools``) runs a pure conversational partner.

    Returns a process exit code: 0 on a clean session, 2 for a usage/environment error
    (missing extra, not an initialized entity, isolation refusal) surfaced BEFORE the loop.
    """
    try:
        session = EntitySession.open(
            path, model=model, base_url=base_url, api_key=api_key, with_tools=with_tools,
            # A human is DRIVING this one. With the default `efferent_gate: "auto"` that resolves
            # UNGATED — the operator watching tool activity stream past is the fan-in the gate
            # would otherwise supply, so gating here would add a prompt without adding oversight,
            # and prompts get bypassed. An operator who pins `"gated"` opts INTO the inline
            # approve/refuse drain below; that is an affordance, not the default.
            mode="interactive",
        )
    except SessionStartError as exc:
        print(f"levain run: {exc.message}")
        return exc.code

    reader = TurnReader()
    interrupted = False
    try:
        # INSIDE the try/finally (glm L3): the banner was previously printed in the gap between
        # open() and the loop, so a BrokenPipeError from a closed stdout jumped past
        # `finally: session.close()` and leaked the session — and with it the OS-sandboxed bash
        # subprocess it holds. Pre-existing gap, but the bash slice made it cost a process.
        _banner_for(session)
        while True:
            try:
                # ONE turn — a typed line, a whole pasted block, an explicit `:paste`…`:end`
                # block, or (on a pipe/heredoc) the whole stream. `None` = session over.
                message = reader.read_turn("\n\033[1myou ›\033[0m ")
            except KeyboardInterrupt:
                # Ctrl-C cancels the message being composed — including a `:paste` block in
                # progress and the rest of a paste already read into the reader (leaving it
                # queued would deliver the tail of a cancelled block as the next turn). SAY how
                # much was dropped: a silently discarded three-paragraph block looks identical
                # to a no-op, and the operator has no other way to learn their text is gone.
                dropped = reader.cancelled_lines
                if dropped:
                    print(f"\n  (cancelled — {dropped} line(s) discarded; :quit to exit)")
                else:
                    print("\n  (type :quit to exit)")
                continue

            if message is None:
                break
            if not message:
                continue

            try:
                result = session.run_turn(message)
                if result.gated:
                    result, interrupted = _drain_gate(session, result)
                    if interrupted:
                        break
            except KeyboardInterrupt:
                print("\n  (turn interrupted — exiting)")
                interrupted = True
                break

            if result.error is not None:
                # A raised run() leaves the SDK conversation in an ERROR state the NEXT turn
                # would resume FROM (stale-prompt reuse). End cleanly on a corrupted
                # conversation rather than limp on — the entity's memory (anneal) is intact.
                print(
                    f"\n  ! the turn failed: {result.error}\n"
                    f"  Ending the session — your entity's memory is intact; "
                    f"restart with `levain run`."
                )
                interrupted = True
                break

            _render_turn(session, result)
    finally:
        session.close()

    _farewell(session, interrupted=interrupted)
    return EXIT_OK


def _drain_gate(
    session: EntitySession, result: TurnResult
) -> tuple[TurnResult, bool]:
    """Fan a gated turn in to the operator, repeatedly, until the turn moves on.

    Reached ONLY when the entity pinned ``efferent_gate: "gated"`` — the REPL's default posture
    is ungated, because a watching human already is the fan-in. So this is the affordance an
    operator explicitly asked for, not a prompt imposed on them, which is the distinction that
    keeps it from becoming the permission-fatigue pattern operators bypass in real life.

    Loops because approving one batch can lead straight into the next. Returns the final result
    plus whether the session should END (the operator interrupted at the decision point).

    **Ambiguity refuses.** A bare Enter, EOF and Ctrl-C all REJECT rather than approve: the only
    input that runs an efferent action is an explicit yes. A gate whose default answer is "do it"
    is a formality, and this one is meant to be load-bearing.
    """
    while result.gated:
        print("\n  ⛔ \033[1mheld at the efferent gate\033[0m — this changes the world:")
        _print_pending(result.pending)
        try:
            answer = input("\n  approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  (declined at the gate — ending the session)")
            session.reject_turn("the operator interrupted at the gate")
            return result, True

        if answer in ("y", "yes"):
            print("  → approved; running it.")
            result = session.resume_turn()
        else:
            print("  → declined; telling the entity so it can adapt.")
            result = session.reject_turn("the operator declined this action")

        if result.error is not None:
            return result, False
    return result, False


def _print_pending(pending: tuple[PendingEfferent, ...]) -> None:
    """Render what the gate is holding. Each entry shows the ACT, then why it fanned in."""
    for item in pending:
        print(f"    • {item.line()}")


# ---------------------------------------------------------------------------
# Driver 2 — the non-interactive runner (K1: `levain run --task`)
# ---------------------------------------------------------------------------

def run_task(
    path: Path,
    task: str,
    *,
    model: str = "glm-5.2:cloud",
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
    with_tools: bool = True,
    quiet: bool = False,
    max_iterations: int | None = None,
    max_seconds: float | None = None,
    unattended: bool = False,
) -> int:
    """Drive the entity at ``path`` through ONE task, non-interactively, and exit.

    This is the headless runner: no tty, no REPL, no human. It sends ``task`` as a single
    turn, streams the entity's tool activity as it happens, prints the reply, and returns a
    process exit code the caller can branch on.

    **What the exit code means — and what it deliberately does NOT mean.** It reports what the
    HARNESS observed, never what the agent claimed:

      - ``0`` the turn completed and the agent replied;
      - ``1`` the turn completed but the agent produced NO reply (a real stall);
      - ``2`` a usage / environment / startup error, before any turn ran;
      - ``3`` the turn raised;
      - ``4`` the turn HALTED AT THE EFFERENT GATE — the entity proposed action on the world and
        the harness stopped it pending a human decision (K3, ``spore-295``). **Not a failure**:
        it is the governor working. A supervisor should surface ``4`` for a human, never retry it
        blindly — a retry re-proposes the same action into the same gate.
      - ``5`` the turn EXCEEDED ITS WALL-CLOCK BOUND and was terminated (K4a ⑥, ``spore-434``).
        An ENVIRONMENT stall, not a broken task — retry on the cadence; if it repeats, the model
        endpoint is sick.

    A headless run is ``human_present=False``, so with the default ``efferent_gate: "auto"`` it
    is GATED. That is the whole reason K3 exists: this is the driver a scheduler and an
    unattended seat use, and it is definitionally the case where nobody is watching the stream.

    A code derived from the agent's own account of its success would be the false-all-clear
    class Levain's trust pitch says cannot happen — a confined entity once reported "25 failed,
    1617 passed" for a suite that was 1702/0 outside its sandbox (``spore-373`` [5]). **If you
    need task-level success, verify the WORLD** (run the tests, read the diff), never ask the
    agent how it did. See :mod:`levain.session`.

    ``quiet`` suppresses the banner and the activity stream, printing only the reply — for use
    in a pipeline where the reply is the payload. ``max_iterations`` bounds the turn's agent
    steps, so an unattended run cannot spend forever on one message.

    ``max_seconds`` bounds the turn in WALL-CLOCK time, which is a different guarantee and the one
    a supervisor actually needs (K4a ⑥, ``spore-434``): ``max_iterations`` counts STEPS, so a turn
    that hangs inside ONE step — a stalled model call — is unbounded by it. Left ``None`` nothing is
    armed, preserving the existing behaviour for an operator running a task by hand; a scheduled
    seat always sets it (see :func:`levain.daemon.build_seat_spec`).

    Streaming note: output is flushed as it is produced, so a caller reading the pipe sees
    progress DURING the turn rather than nothing until the process exits.
    """
    # An empty --task is a USAGE error, and it must be caught BEFORE opening a session (codex
    # L3): EntitySession.open spawns a sandboxed bash subprocess and mints the confined tool
    # bundle, so discovering "there was no task" after all that is both wasteful and a
    # confusing failure — the operator sees a full sovereign banner for a no-op.
    if not task.strip():
        print(
            "levain run: --task must not be empty.\n"
            "  Give it a spec:  levain run --task 'fix the failing test in verify.py'",
            file=sys.stderr, flush=True,
        )
        return EXIT_USAGE

    # Line-buffer stdout so a piped/redirected caller sees output as it happens. Python
    # block-buffers a non-tty stdout, which is exactly why headless output was invisible until
    # process exit — the reconfigure is the fix, and it must happen before the first write.
    _line_buffer_stdout()

    # THE WALL-CLOCK BOUND WRAPS EVERYTHING, INCLUDING SESSION STARTUP AND TEARDOWN (K4a ⑥).
    #
    # Not just the model turn, and the reason is what the defect actually is: launchd COALESCES per
    # label, so it refuses to start the next scheduled run while THIS PROCESS is alive — it does not
    # care which phase is stuck. `EntitySession.open` spawns the confined shell and completes a
    # handshake over a private FIFO; `close` kills a process group. Both can block, and either one
    # blocking forever starves the cadence exactly as a hung model call does. A bound scoped to
    # `run_turn` alone would be a bound on the phase we happened to think of first.
    #
    # Consequence, stated rather than discovered: a turn that completes just under the bound and
    # then hangs in teardown past the grace period exits `5` rather than `0`. Correct — at that
    # point the PROCESS has overrun, which is the thing being bounded.
    deadline = TurnDeadline(max_seconds, hard_exit_code=EXIT_TIMEOUT)
    with deadline:
        try:
            return _drive_task(
                path, task,
                model=model, base_url=base_url, api_key=api_key, with_tools=with_tools,
                quiet=quiet, max_iterations=max_iterations, max_seconds=max_seconds,
                unattended=unattended,
            )
        except TurnTimeout:
            # Reached when the bound fires OUTSIDE the turn (startup, teardown, rendering).
            # `run_turn` catches its own and reports via `result.timed_out`, which routes to the
            # same report below — one wording for one outcome, wherever it lands.
            return _report_timeout(deadline.seconds, captured=None)


def _report_timeout(seconds: float | None, *, captured: bool | None) -> int:
    """Print the graceful-timeout report and return its exit code. One wording, one place.

    ``captured`` is threaded rather than assumed because the two call sites have DIFFERENT authority
    (codex L3, MED). When the SESSION classified the timeout, it knows the turn never reached capture.
    When the bound fired somewhere else under the ``with`` — rendering, teardown, startup — nothing
    here can know, and the earlier version asserted "nothing was captured to memory" in that window
    too, which is false for a turn that completed and was captured a moment before the alarm."""
    print(
        format_timeout_report(seconds or 0.0, hard=False, captured=captured),
        file=sys.stderr, flush=True,
    )
    return EXIT_TIMEOUT


def _drive_task(
    path: Path,
    task: str,
    *,
    model: str,
    base_url: str,
    api_key: str | None,
    with_tools: bool,
    quiet: bool,
    max_iterations: int | None,
    max_seconds: float | None,
    unattended: bool,
) -> int:
    """Open a session, run ONE turn, report it, close. The body :func:`run_task` bounds.

    ``max_seconds`` is NOT enforced here — the caller owns the bound. It is passed only so the
    report can name the bound that fired, rather than recovering the number by parsing it back out
    of an error message (an outcome read from its own description is the drift this build
    deliberately avoids elsewhere).

    Split out so the wall-clock bound can wrap session startup and teardown as well as the turn
    (see the caller) without the drive logic being nested inside it — the bound is a property of the
    PROCESS, and reading it should not require reading past it to find the work.
    """

    def _emit_activity(line: str) -> None:
        if not quiet:
            print(f"  \033[2m{line}\033[0m", flush=True)

    try:
        session = EntitySession.open(
            path,
            model=model, base_url=base_url, api_key=api_key, with_tools=with_tools,
            on_event=None if quiet else _emit_activity,
            max_iterations=max_iterations,
            # No human is driving. With `efferent_gate: "auto"` BOTH modes below resolve GATED —
            # the fan-in a watching operator supplies at the REPL has to come from the gate here.
            #
            # The two are NOT interchangeable for the crown-jewels CRED floor, which is the whole
            # reason this is a three-valued mode rather than the old `human_present` bool:
            # `headless` is a human who typed this and will read the output (they may legitimately
            # need `gh`), while `unattended` is a scheduler with nobody in the loop at all, where a
            # silent credential read can compound into always-loaded memory. See
            # `levain.firing.drive`.
            mode="unattended" if unattended else "headless",
        )
    except SessionStartError as exc:
        print(f"levain run: {exc.message}", file=sys.stderr, flush=True)
        return exc.code

    try:
        if not quiet:
            _banner_for(session, task=task, max_seconds=max_seconds)
        try:
            result = session.run_turn(task)
        except KeyboardInterrupt:
            # 130, not EXIT_USAGE (glm L3, HIGH). A supervisor that sees 2 goes hunting for a
            # bad --model or a missing extra; an interrupt is an operator action, and this is
            # precisely the unattended-supervision case the keystone exists to serve.
            print("\n  (task interrupted)", file=sys.stderr, flush=True)
            return EXIT_INTERRUPTED

        # BEFORE the generic error branch, because a timed-out turn carries an error too. Reporting
        # it as "the task failed" would tell a supervisor the entity or the spec is broken, when
        # what actually happened is that the environment stalled and the next run is fine.
        if result.timed_out:
            # The session vouches for this one: `_drive` returns `timed_out` only from a path that
            # has NOT called `capture_turn`.
            return _report_timeout(max_seconds, captured=False)

        if result.error is not None:
            print(f"\n  ! the task failed: {result.error}", file=sys.stderr, flush=True)
            return result.exit_code

        if result.gated:
            # STDERR, and worded as a decision rather than an error: stdout is the reply payload
            # and there is no reply — the turn has not finished. Say plainly that nothing ran, so
            # a supervisor's log cannot be misread as "it did the thing and said nothing".
            # The "⚙" lines already streamed above are emitted by the runtime BEFORE the
            # confirmation decision, so a held action can appear there looking like completed
            # work. The result's own activity list has the held actions subtracted, but the
            # STREAM cannot un-print itself — so say plainly that anything shown above was
            # proposed. Leaving the operator to reconcile "⚙ terminal: git push" against
            # "nothing was executed" is asking them to guess which of two true-looking lines
            # to believe (codex L3).
            print(
                "\n  ⛔ HELD AT THE EFFERENT GATE — nothing was executed.\n"
                "     The entity proposed action on the world; this run has no human to fan it "
                "in to.\n"
                "     Any ⚙ activity shown above was PROPOSED — the held actions below did NOT "
                "run.",
                file=sys.stderr, flush=True,
            )
            for item in result.pending:
                print(f"       • {item.line()}", file=sys.stderr, flush=True)
            print(
                "     Re-run it with a human at the terminal (`levain run` without --task), or "
                "pin\n"
                "     efferent_gate: \"ungated\" in .levain/confinement.json if this entity is "
                "trusted\n"
                "     to act unattended. Nothing is queued — re-running re-proposes.",
                file=sys.stderr, flush=True,
            )
            return result.exit_code

        # The reply goes to STDOUT as the payload — activity already streamed above, so it is
        # NOT reprinted here (printing `result.tool_activity` too would double every line).
        if result.reply:
            if not quiet:
                print(f"\n\033[1m{session.label} ›\033[0m {result.reply}", flush=True)
            else:
                print(result.reply, flush=True)
        else:
            # A completed turn with no reply is a REAL failure, not a quiet success. Say so on
            # stderr so a pipeline reading stdout gets an empty payload AND a visible reason.
            print(
                f"\n  ! the entity completed the turn without a reply "
                f"({'after the act-first nudge' if result.nudged else 'no output'}).",
                file=sys.stderr, flush=True,
            )
        return result.exit_code
    finally:
        session.close()


def _line_buffer_stdout() -> None:
    """Make stdout line-buffered so headless output streams instead of appearing at exit.

    Fail-soft: a stdout that cannot be reconfigured (already-wrapped, or a test's StringIO)
    must not break the run — the output still arrives, just buffered as before."""
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — buffering is an optimization, never a requirement
        pass


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------

def _render_turn(session: EntitySession, result: TurnResult) -> None:
    """Print a completed turn: tool ACTIVITY (what it did) then the reply.

    Used by the REPL, which does NOT stream (it has a human watching the whole turn). The
    headless driver streams instead and must not also call this, or activity prints twice."""
    for line in result.tool_activity:
        print(f"  \033[2m{line}\033[0m")  # dim — activity is context, the reply is the message
    print(f"\n\033[1m{session.label} ›\033[0m {result.reply or '(no reply)'}")


def _entity_label(binding) -> str:
    """The entity's display name for the prompt — its dir name (the sovereign handle)."""
    return binding.entity_dir.name


def _print_banner(
    entity_dir: Path, binding, *, model: str, with_tools: bool, bash_ok: bool,
    ssh_mode: str = "agent", deny_standard_creds: bool = False, task: str | None = None,
    gate_mode: str = "gated", max_seconds: float | None = None,
) -> None:
    """The session header — and the HONESTY FLOOR: show the operator exactly which stores this
    entity reads/writes, what hands it has, AND what the crown-jewels floor keeps off-limits, so
    sovereignty is VISIBLE, not merely asserted. The floor lines are rendered from the ACTUAL
    config (``ssh_mode``), never a static string that could invert under a real setting
    (apparatus L1).

    Takes PLAIN FACTS, not an :class:`~levain.session.EntitySession`, deliberately: this is a
    pure render function, and the honesty-floor tests must be able to exercise every floor
    permutation (raw ssh, no sandbox, opted-in cred stores) WITHOUT standing up a real
    sovereign entity. Coupling the banner to a live session would make the floor's own
    regression tests expensive, which is how a floor claim quietly stops being tested.
    """
    print("=" * 66)
    mode = "task" if task is not None else "sovereign entity"
    print(f"  levain run — {entity_dir.name}  ({mode})")
    print("=" * 66)
    print(f"  model:     {model}")
    print(f"  memory:    {binding.episodic_path}")
    print(f"             {binding.crystal_path}")
    print(f"  workspace: {entity_dir / WORKSPACE_SUBDIR}")
    if not with_tools:
        print("  tools:     none (conversational partner; --no-tools)")
    else:
        hands = "file_editor + terminal (bash)" if bash_ok else "file_editor"
        print(f"  tools:     {hands} — confined to the crown-jewels floor")
        if not bash_ok:
            print("             (bash dropped: no OS sandbox on this platform — file editor only)")
        print("  floor:     DENIES ~/.anneal-memory/ (flow store) · sibling entity stores ·")
        print("             operator creds + the confinement config (.levain/confinement.json)")
        if ssh_mode == "agent":
            print("             ~/.ssh key material (agent-auth only — keys usable, not readable)")
        else:
            print("             ⚠ ~/.ssh NOT confined (ssh_mode=raw — raw key reads ALLOWED;")
            print("               ssh authorized_keys/config/rc WRITE denied; other writes are NOT)")
        # Say it in BOTH directions. The line above names "operator creds" (the ones DECLARED in
        # confinement.json), which an operator can read as covering the standard stores too — so
        # when those are READABLE the banner has to say so out loud, or it claims a floor it does
        # not enforce. install-seat already warns on this; the run banner must not be quieter than
        # the installer about the same fact (codex L3 MEDIUM).
        if deny_standard_creds:
            print("             standard cred stores ~/.config/gh · ~/.aws/credentials · ~/.netrc")
        else:
            print("             ⚠ standard cred stores ~/.config/gh · ~/.aws/credentials · ~/.netrc")
            print("               are READABLE by this entity (deny_standard_creds is off)")
        print("             its OWN memory store (continuity/crystal/episodic) is WRITE-protected —")
        print("             only `levain wrap` composes it; the hands may READ but not rewrite it")
        # The GATE line sits with the floor because it answers the same question the floor does —
        # what may this entity do to the world — and it is rendered from the RESOLVED mode, never
        # a static string. A banner that claimed "gated" while the session ran ungated would be
        # the one lie this whole keystone exists to make impossible.
        if gate_mode == "gated":
            print("  gate:      EFFERENT actions HELD for you — bash, and any file write.")
            print("             Reads run free. Nothing graduates to unattended on its own.")
        else:
            print("  gate:      UNGATED — efferent actions run as the entity decides.")
            if task is None:
                print("             You are the fan-in: watch the activity as it streams.")
            else:
                print("             ⚠ no human is driving this run (efferent_gate: \"ungated\").")
    print()
    if task is not None:
        # THE TIME BOUND IS STATED, AND SO IS ITS ABSENCE. The banner already enumerates the exit
        # codes, so omitting `5` would have left it enumerating a contract it no longer matches —
        # the same claim-versus-behaviour asymmetry that WAS the bug in the cred-floor banner (one
        # resolved line above one static line, answering the same question).
        if max_seconds is not None:
            print(f"  time bound: {max_seconds:g}s wall-clock — the turn is terminated if it "
                  f"overruns.")
        else:
            print("  time bound: NONE — a hung model call will not be interrupted.")
        print("  Running ONE task, then exiting. Exit code reports what the HARNESS saw:")
        print("    0 replied · 1 no reply · 2 startup error · 3 the turn raised ·")
        print("    4 held at the efferent gate (a decision for you, NOT a failure) ·")
        print("    5 the wall-clock bound was exceeded (an ENVIRONMENT stall, not a bad task)")
        print("  It does NOT assert the task succeeded — verify that against the world.")
    else:
        print("  Talk to it. It recalls its OWN memory and captures each turn there.")
        print("  A pasted block is ONE message — or :paste … :end to be explicit.")
        print("  :quit (or Ctrl-D) to end the session.")


def _banner_for(
    session: EntitySession, *, task: str | None = None, max_seconds: float | None = None,
) -> None:
    """Render the banner from a live session — the drivers' convenience wrapper over
    :func:`_print_banner`, which stays fact-shaped so the floor tests stay cheap."""
    _print_banner(
        session.entity_dir, session.binding,
        model=session.model_label, with_tools=session.with_tools, bash_ok=session.bash_ok,
        ssh_mode=session.ssh_mode, deny_standard_creds=session.deny_standard_creds, task=task,
        gate_mode=session.gate_mode, max_seconds=max_seconds,
    )


def _farewell(session: EntitySession, *, interrupted: bool) -> None:
    """Close the session: surface a wrap-nudge if the entity's store is due for a consolidate."""
    nudge = session.wrap_nudge()
    print()
    if nudge:
        print(f"  {nudge}")
    print("  Session ended." if not interrupted else "  Session interrupted.")
