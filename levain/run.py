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

from levain.session import (
    EXIT_INTERRUPTED,
    EXIT_OK,
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
      - ``3`` the turn raised.

    A code derived from the agent's own account of its success would be the false-all-clear
    class Levain's trust pitch says cannot happen — a confined entity once reported "25 failed,
    1617 passed" for a suite that was 1702/0 outside its sandbox (``spore-373`` [5]). **If you
    need task-level success, verify the WORLD** (run the tests, read the diff), never ask the
    agent how it did. See :mod:`levain.session`.

    ``quiet`` suppresses the banner and the activity stream, printing only the reply — for use
    in a pipeline where the reply is the payload. ``max_iterations`` bounds the turn's agent
    steps, so an unattended run cannot spend forever on one message.

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

    def _emit_activity(line: str) -> None:
        if not quiet:
            print(f"  \033[2m{line}\033[0m", flush=True)

    try:
        session = EntitySession.open(
            path,
            model=model, base_url=base_url, api_key=api_key, with_tools=with_tools,
            on_event=None if quiet else _emit_activity,
            max_iterations=max_iterations,
        )
    except SessionStartError as exc:
        print(f"levain run: {exc.message}", file=sys.stderr, flush=True)
        return exc.code

    try:
        if not quiet:
            _banner_for(session, task=task)
        try:
            result = session.run_turn(task)
        except KeyboardInterrupt:
            # 130, not EXIT_USAGE (glm L3, HIGH). A supervisor that sees 2 goes hunting for a
            # bad --model or a missing extra; an interrupt is an operator action, and this is
            # precisely the unattended-supervision case the keystone exists to serve.
            print("\n  (task interrupted)", file=sys.stderr, flush=True)
            return EXIT_INTERRUPTED

        if result.error is not None:
            print(f"\n  ! the task failed: {result.error}", file=sys.stderr, flush=True)
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
        if deny_standard_creds:
            print("             standard cred stores ~/.config/gh · ~/.aws/credentials · ~/.netrc")
        print("             its OWN memory store (continuity/crystal/episodic) is WRITE-protected —")
        print("             only `levain wrap` composes it; the hands may READ but not rewrite it")
    print()
    if task is not None:
        print("  Running ONE task, then exiting. Exit code reports what the HARNESS saw:")
        print("    0 replied · 1 no reply · 2 startup error · 3 the turn raised")
        print("  It does NOT assert the task succeeded — verify that against the world.")
    else:
        print("  Talk to it. It recalls its OWN memory and captures each turn there.")
        print("  A pasted block is ONE message — or :paste … :end to be explicit.")
        print("  :quit (or Ctrl-D) to end the session.")


def _banner_for(session: EntitySession, *, task: str | None = None) -> None:
    """Render the banner from a live session — the drivers' convenience wrapper over
    :func:`_print_banner`, which stays fact-shaped so the floor tests stay cheap."""
    _print_banner(
        session.entity_dir, session.binding,
        model=session.model_label, with_tools=session.with_tools, bash_ok=session.bash_ok,
        ssh_mode=session.ssh_mode, deny_standard_creds=session.deny_standard_creds, task=task,
    )


def _farewell(session: EntitySession, *, interrupted: bool) -> None:
    """Close the session: surface a wrap-nudge if the entity's store is due for a consolidate."""
    nudge = session.wrap_nudge()
    print()
    if nudge:
        print(f"  {nudge}")
    print("  Session ended." if not interrupted else "  Session interrupted.")
