"""levain.run — the interactive REPL for a runnable, ISOLATED Levain entity.

`levain run <entity_dir>` is the "use it like Claude Code, but sovereign" surface: a
partner-entity that runs on an open model (Ollama by default), carries its OWN memory,
and — the #1 requirement (Phill, 2026-07-07) — MUST NOT touch the operator-laptop flow
store. Everything sovereignty-critical is already welded into the STEP-2 chokepoint
(:func:`levain.firing.openhands.entity.build_entity_agent`): it fail-closes on the
isolation guard BEFORE building anything, and every firing/condenser is pinned to the
``anneal_entity`` kind whose resolver re-guards the store PER OP. This module just wraps
a REPL around the binding the chokepoint returns.

The capability posture (the ⚛️ physicist-lens Sharpening, 2026-07-08 — "don't certify
sovereignty once at startup; mint a narrow authority bundle, enforce at the point of
use"):

  - **store** — already per-op re-guarded by ``AnnealEntityFiring`` (the chokepoint owns
    it). Nothing added here.
  - **files** — the OpenHands ``workspace`` is confined to ``<entity>/workspace/``, NEVER
    the operator's cwd or ``$HOME``. ENFORCED (not merely documented) by
    ``assert_workspace_isolated`` before the workspace is created, so a symlink escape is
    refused and file authority is bounded the moment executor tools arrive.
  - **tools** — this first slice runs with NO executor tools (``build_entity_agent``
    defaults ``tools=[]``): a sovereign conversational partner with memory. Executor
    tools are the explicit NEXT slice, added under the same per-run minting discipline
    (a confined ``{bash, file}`` bundle), NOT an ambient grant.
  - **relay** — none wired.

Requires the ``openhands`` extra (``pip install 'levain[openhands]'``). The heavy imports
(OpenHands SDK + the entity chokepoint, which imports it at module level) are LAZY — done
inside :func:`run_entity` — so ``levain --help`` and ``import levain.run`` work without the
extra; a missing extra becomes a friendly one-line install hint, not an ImportError
traceback.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from levain.firing.agent_reply import (
    finish_message,
    is_corrective_nudge,
    message_event_text,
)
from levain.install import effective_adapter
from levain.firing.isolation import (
    ENTITY_STORE_SUBDIR,
    IsolationError,
    assert_workspace_isolated,
)

__all__ = ["run_entity"]

# The confined file-authority root for a run: a sibling of the sovereign store, NEVER the
# operator's cwd/$HOME. With zero tools it stays inert, but the fence is structural so
# file authority is already bounded when executor tools land (the capability-minting
# discipline — bound the authority BEFORE the tool can spend it).
_WORKSPACE_SUBDIR = "workspace"

# The REPL exit tokens (typed at the prompt). EOF (Ctrl-D) also exits.
_QUIT_TOKENS = frozenset({":quit", ":q", ":exit"})


def _latest_agent_text(events) -> str | None:
    """The assistant's text from the just-completed turn — the ``source == "agent"`` events
    AFTER the last genuine human message.

    Shares its event-shape parsing with ``capture.render_turn`` via
    ``levain.firing.agent_reply`` (pure/duck-typed, so this stays SDK-free for the tests),
    so the shown reply and the captured episode never diverge: an agent reply is either a
    ``MessageEvent`` (``message_event_text``) or an ``ActionEvent(FinishAction)``
    (``finish_message`` — the SDK routes a no-tool answer through the built-in ``finish``
    tool, not a MessageEvent). The boundary skips the SDK's synthetic corrective nudge
    (``is_corrective_nudge``) so a weak-model turn keys on the real question. Returns
    ``None`` when there is no agent text yet."""
    evs = list(events)
    last_user = None
    for i in range(len(evs) - 1, -1, -1):
        if getattr(evs[i], "source", None) == "user" and not is_corrective_nudge(evs[i]):
            last_user = i
            break
    start = 0 if last_user is None else last_user
    parts: list[str] = []
    for e in evs[start:]:
        if getattr(e, "source", None) != "agent":
            continue
        text = message_event_text(e) or finish_message(e)
        if text and text not in parts:  # dedup a finish echoing a prior MessageEvent
            parts.append(text)
    return "\n".join(parts) if parts else None


def _resolve_model(model: str) -> str:
    """Prefix a bare model name with the Ollama provider for litellm.

    ``minimax-m3:cloud`` → ``ollama/minimax-m3:cloud`` (the default sovereign path — an
    open model through the local Ollama endpoint). A name that already carries a
    ``provider/`` prefix (``ollama/…``, ``openai/…``) is passed through untouched, so an
    advanced operator can point at any litellm-routable model."""
    return model if "/" in model else f"ollama/{model}"


def run_entity(
    path: Path,
    *,
    model: str = "minimax-m3:cloud",
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
) -> int:
    """Run the interactive REPL for the isolated entity at ``path``.

    Returns a process exit code: 0 on a clean session, 2 for a usage/environment error
    (missing extra, not an initialized entity, isolation refusal) surfaced BEFORE the loop.
    """
    entity_dir = Path(str(path)).expanduser().resolve()

    if not (entity_dir / ENTITY_STORE_SUBDIR).is_dir():
        print(
            f"levain run: {entity_dir} is not an initialized Levain entity "
            f"(no {ENTITY_STORE_SUBDIR}/).\n"
            f"  Create one first:  levain init --adapter openhands --path {entity_dir}"
        )
        return 2

    # This command drives an OpenHands entity — require it to be a CLEAN openhands entity via
    # the shared `effective_adapter` classifier (the same one doctor + verify use, so all
    # three agree). Hosted files dominate a stale marker, so we never start an OpenHands agent
    # against a claude-code/codex store or a residue-bearing mixed install — a bare .levain/
    # store, or an openhands marker sitting on top of hosted-harness files, is not enough.
    if effective_adapter(entity_dir) != "openhands":
        print(
            f"levain run: {entity_dir} is a Levain store, but not a clean OpenHands entity.\n"
            f"  Re-scaffold it as one:  levain init --adapter openhands --path {entity_dir}"
        )
        return 2

    # Lazy — the entity chokepoint imports the OpenHands SDK at module level; keep it out
    # of `levain --help` / `import levain.run`, and turn a missing extra into a hint.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    try:
        from openhands.sdk import LLM, Conversation

        from levain.firing.openhands.entity import build_entity_agent
    except ImportError as exc:
        print(
            "levain run: the OpenHands runtime is not installed.\n"
            "  Install the extra:  pip install 'levain[openhands]'\n"
            f"  ({exc})"
        )
        return 2

    # Quiet the SDK's own INFO/WARNING chatter (e.g. "no persistence_dir") + litellm's cost
    # warnings, so the banner's clean-output promise holds; real ERRORs still surface.
    for noisy in ("openhands", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    # The sovereignty-critical construction, under one exit-2 handler:
    #   - build_entity_agent fail-CLOSES on the STORE guard before building anything (a store
    #     that would reach ~/.anneal-memory/ or escape .levain/ raises IsolationError);
    #   - assert_workspace_isolated ENFORCES the FILE fence (the workspace can't symlink out of
    #     the entity tree) BEFORE the workspace is created — so the "confined" claim is real,
    #     in place before any future executor tool can write (the physicist minting discipline);
    #   - a bad --model/--base-url is a usage error → clean exit 2, not a raw traceback.
    try:
        llm = LLM(model=_resolve_model(model), base_url=base_url, api_key=api_key,
                  usage_id="levain-run")
        binding = build_entity_agent(entity_dir, llm)
        workspace = entity_dir / _WORKSPACE_SUBDIR
        assert_workspace_isolated(workspace, entity_dir=entity_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        # Re-guard AFTER mkdir, right before use (codex L3 TOCTOU): mkdir(exist_ok=True)
        # follows a symlink swapped in after the first assert, so re-assert at the point of
        # use — the invariant fires at USE, not once at validation.
        assert_workspace_isolated(workspace, entity_dir=entity_dir)
        # visualizer=None: the REPL owns all output (with zero tools the only event worth
        # showing is the assistant reply, which we render). `Any`: the SDK types
        # `Conversation(...)` as the abstract `BaseConversation`, which under-declares the
        # concrete `send_message` / `state` surface (shipped `capture.vagus_run` sidesteps
        # this by leaving its conversation param untyped).
        conversation: Any = Conversation(
            binding.agent, workspace=str(workspace), visualizer=None
        )
    except IsolationError as exc:
        print(f"levain run: sovereignty guard REFUSED to start the entity:\n  {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — a bad model/endpoint config is a usage error
        print(
            f"levain run: could not start the entity ({exc}).\n"
            f"  Check --model / --base-url (default: an open model via local Ollama)."
        )
        return 2

    _print_banner(entity_dir, binding, model=_resolve_model(model))

    interrupted = False
    try:
        while True:
            try:
                line = input("\n\033[1myou ›\033[0m ").strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\n  (type :quit to exit)")
                continue

            if not line:
                continue
            if line.lower() in _QUIT_TOKENS:
                break

            try:
                conversation.send_message(line)
                binding.capture_turn(conversation)
            except KeyboardInterrupt:
                print("\n  (turn interrupted — exiting)")
                interrupted = True
                break
            except Exception as exc:  # noqa: BLE001 — a failed turn must not crash the REPL
                # A raised run() leaves the SDK conversation in an ERROR state the NEXT turn
                # would resume FROM (stale-prompt reuse). End cleanly on a corrupted
                # conversation rather than limp on — the entity's memory (anneal) is intact.
                print(
                    f"\n  ! the turn failed: {exc}\n"
                    f"  Ending the session — your entity's memory is intact; "
                    f"restart with `levain run`."
                )
                interrupted = True
                break

            reply = _latest_agent_text(conversation.state.events)
            print(f"\n\033[1m{_entity_label(binding)} ›\033[0m {reply or '(no reply)'}")
    finally:
        _close_quietly(conversation)

    _farewell(binding, interrupted=interrupted)
    return 0


def _close_quietly(conversation: Any) -> None:
    """Release the SDK conversation's resources (a no-op with zero tools — atexit also calls
    close() — but the REPL owns one session per process, and executor tools next slice hold
    subprocesses that close() frees, so close deterministically here)."""
    try:
        conversation.close()
    except Exception:  # noqa: BLE001 — teardown must never raise
        pass


def _entity_label(binding) -> str:
    """The entity's display name for the prompt — its dir name (the sovereign handle)."""
    return binding.entity_dir.name


def _print_banner(entity_dir: Path, binding, *, model: str) -> None:
    """The session header — and the HONESTY FLOOR: show the operator exactly which stores
    this entity reads/writes, so sovereignty is VISIBLE, not merely asserted."""
    print("=" * 66)
    print(f"  levain run — {entity_dir.name}  (sovereign entity)")
    print("=" * 66)
    print(f"  model:     {model}")
    print(f"  memory:    {binding.episodic_path}")
    print(f"             {binding.crystal_path}")
    print(f"  workspace: {entity_dir / _WORKSPACE_SUBDIR}")
    print("  tools:     none (conversational partner; executor tools are a later slice)")
    print()
    print("  Talk to it. It recalls its OWN memory and captures each turn there.")
    print("  :quit (or Ctrl-D) to end the session.")


def _farewell(binding, *, interrupted: bool) -> None:
    """Close the session: surface a wrap-nudge if the entity's store is due for a
    consolidate. ``wrap_nudge`` re-guards the entity store at USE time and fail-softs to
    ``None``, so this never leaks and never crashes the exit."""
    try:
        nudge = binding.wrap_nudge()
    except Exception:  # noqa: BLE001 — the farewell must never raise
        nudge = None
    print()
    if nudge:
        print(f"  {nudge}")
    print("  Session ended." if not interrupted else "  Session interrupted.")
