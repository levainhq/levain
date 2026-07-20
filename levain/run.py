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
    ``assert_workspace_isolated`` before the workspace is created (a symlink escape of the
    workspace DIR is refused), AND per-op by the confined file tool below (every tool path is
    resolved + fenced to the workspace before it touches the filesystem).
  - **tools** — the entity gets confined HANDS (``levain.firing.openhands.tools.build_entity_tools``):
    a file-editor tool AND (spore-311 slice 2) a persistent OS-sandboxed bash, BOTH fenced to the
    shared crown-jewels FLOOR — they work like CC/Codex on the operator's real repos while
    ``~/.anneal-memory/`` (flow's store), sibling entity stores, and ``~/.ssh`` key material stay
    structurally off-limits (plus the operator's declared creds from ``.levain/confinement.json``).
    bash rides ``sandbox-exec`` (a persistent host shell can't be confined in-process); on a platform
    with no OS sandbox the entity keeps its file-editor hand and bash is dropped (honesty floor, NEVER
    an unconfined fallback). Pass ``with_tools=False`` (``levain run --no-tools``) for a pure
    conversational partner.
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
    LEVAIN_ACT_NUDGE,
    finish_message,
    humanize_finish_json,
    is_corrective_nudge,
    message_event_text,
    planned_without_acting,
    tool_action_summary,
)
from levain.install import effective_adapter
from levain.firing.confinement import (
    ConfinementError,
    confinement_supported,
    load_confinement_config,
)
from levain.firing.isolation import (
    ENTITY_STORE_SUBDIR,
    IsolationError,
    assert_workspace_isolated,
)
from levain.turn_input import TurnReader

__all__ = ["run_entity", "require_openhands_entity"]

# The confined file-authority root for a run: a sibling of the sovereign store, NEVER the
# operator's cwd/$HOME. With zero tools it stays inert, but the fence is structural so
# file authority is already bounded when executor tools land (the capability-minting
# discipline — bound the authority BEFORE the tool can spend it).
_WORKSPACE_SUBDIR = "workspace"

# The REPL exit tokens + the multi-line affordances live in `levain.turn_input` — the module
# that owns WHAT CONSTITUTES ONE TURN. That boundary is not a display detail: reading one LINE
# as one TURN is what let an entity read its own pasted specification as a dialogue and
# consolidate the misreading into permanent memory (see turn_input's docstring). Keeping the
# rule in one place means the REPL cannot drift from it.


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
        if text:
            text = humanize_finish_json(text)  # spore-297: unwrap finish/think-as-JSON-text
            if text not in parts:  # dedup a finish echoing a prior MessageEvent
                parts.append(text)
    return "\n".join(parts) if parts else None


def _resolve_model(model: str) -> str:
    """Prefix a bare model name with the Ollama provider — for the operator-facing BANNER label only.
    The LLM route itself is `_resolve_llm_kwargs` (open models go via the OpenAI-compatible /v1
    endpoint); this keeps the banner showing the plain ``ollama/<name>`` identity the operator thinks
    in, not the litellm ``openai/…`` protocol detail.

    ``glm-5.2:cloud`` → ``ollama/glm-5.2:cloud`` (an open model served by the local Ollama endpoint).
    A name that already carries a ``provider/`` prefix (``ollama/…``, ``openai/…``) is passed through
    untouched, so an advanced operator can point at any litellm-routable model."""
    return model if "/" in model else f"ollama/{model}"


def _resolve_llm_kwargs(model: str, base_url: str, api_key: str | None) -> dict:
    """LLM kwargs for ``levain run`` — route OPEN (Ollama) models through Ollama's OpenAI-compatible
    ``/v1`` endpoint with NATIVE tool-calling.

    This REPLACES the spore-358 default (the ``ollama/`` litellm provider + PROMPT-mode tool-calling).
    That default existed because the ``ollama/`` provider drops glm/kimi tool-calls to JSON-TEXT — the
    model emits a well-formed call, but not as a structured ``tool_calls`` entry, so OpenHands sees a
    plain message and ends the turn, task untouched. The fix was mis-attributed to model tier; it is
    the ROUTE. The ``/v1`` endpoint returns structured ``tool_calls``, so native FC works and prompt
    mode's schema+example+regex tax disappears. Bake-off 2026-07-17 (multi-file coding task, n=10):
      glm  — ollama/native 0/5 · ollama/prompt 10/10 (63s) · /v1-native 10/10 (35s)
      kimi —                     ollama/prompt  9/10 (106s) · /v1-native 10/10 (12s)
      minimax (the default) unaffected: /v1-native 10/10 — no regression. All /v1-native: 0 backstop
      nudges. Structured calls are also 2–9x faster (no per-turn schema/example tokens, no regex parse).
    A provider-prefixed model an advanced operator passes (``openai/…``, ``anthropic/…``) is honored
    as-is with native FC — a capable native-FC caller; the Ollama ``/v1`` reroute would be wrong for it.
    """
    if "/" in model and not model.startswith("ollama/"):
        return {"model": model, "base_url": base_url, "api_key": api_key,
                "native_tool_calling": True}
    bare = model.split("/", 1)[1] if model.startswith("ollama/") else model
    if not bare.strip():
        # A prefix-only (`ollama/`) or empty --model would resolve to `openai/` — an invalid model
        # that fails on the FIRST turn, not at startup. Fail CLOSED here (run_entity's except → a
        # clean exit 2 with the --model hint), matching the entity's startup-validation discipline
        # (codex + gpt-oss L3 2026-07-17).
        raise ValueError(f"--model must name a model, not just a provider prefix (got {model!r})")
    host = base_url.rstrip("/")
    v1 = host if host.endswith("/v1") else f"{host}/v1"
    # `api_key is not None` (not truthiness): an operator who deliberately passes an EMPTY key keeps
    # it — only an UNSET key falls back to the Ollama sentinel (codex + gpt-oss L3 2026-07-17).
    return {"model": f"openai/{bare}", "base_url": v1,
            "api_key": api_key if api_key is not None else "ollama",
            "native_tool_calling": True}


def require_openhands_entity(entity_dir: Path) -> str | None:
    """Return an error message if ``entity_dir`` is not a clean, initialized OpenHands entity,
    else ``None``.

    Shared by ``levain run`` and ``levain wrap`` so both agree on EXACTLY what a runnable /
    wrappable sovereign entity is: an initialized ``.levain/`` store AND a CLEAN openhands adapter
    (hosted files dominate a possibly-stale marker, via :func:`~levain.install.effective_adapter` —
    so a claude-code/codex store or a residue-bearing mixed install is refused, not silently driven).
    The caller prefixes its own ``levain run:`` / ``levain wrap:`` label and prints; this returns the
    bare reason + fix so the two commands can't drift on the definition."""
    if not (entity_dir / ENTITY_STORE_SUBDIR).is_dir():
        return (
            f"{entity_dir} is not an initialized Levain entity (no {ENTITY_STORE_SUBDIR}/).\n"
            f"  Create one first:  levain init --adapter openhands --path {entity_dir}"
        )
    if effective_adapter(entity_dir) != "openhands":
        return (
            f"{entity_dir} is a Levain store, but not a clean OpenHands entity.\n"
            f"  Re-scaffold it as one:  levain init --adapter openhands --path {entity_dir}"
        )
    return None


def run_entity(
    path: Path,
    *,
    model: str = "glm-5.2:cloud",
    base_url: str = "http://localhost:11434",
    api_key: str | None = None,
    with_tools: bool = True,
) -> int:
    """Run the interactive REPL for the isolated entity at ``path``.

    ``with_tools`` (default True) grants the entity its confined HANDS — a file editor plus, where an
    OS confinement floor exists (macOS ``sandbox-exec``), a persistent sandboxed bash — both fenced to
    the shared crown-jewels floor; ``False`` (``--no-tools``) runs a pure conversational partner. On a
    platform with no OS sandbox, bash is dropped and only the file editor is granted (honesty floor).

    Returns a process exit code: 0 on a clean session, 2 for a usage/environment error
    (missing extra, not an initialized entity, isolation refusal) surfaced BEFORE the loop.
    """
    entity_dir = Path(str(path)).expanduser().resolve()

    # This command drives an OpenHands entity — require an initialized, CLEAN openhands entity
    # (shared with `levain wrap` so both agree on the definition; `effective_adapter` lets hosted
    # files dominate a stale marker, so a claude-code/codex or residue-bearing mixed install is
    # refused, not silently driven).
    err = require_openhands_entity(entity_dir)
    if err:
        print(f"levain run: {err}")
        return 2

    # Lazy — the entity chokepoint imports the OpenHands SDK at module level; keep it out
    # of `levain --help` / `import levain.run`, and turn a missing extra into a hint.
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    try:
        from openhands.sdk import LLM, Conversation

        from levain.firing.openhands.entity import build_entity_agent
        from levain.firing.openhands.tools import build_entity_tools
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
        # Tool-calling mode (spore-358 + L2 review 2026-07-17). `levain run` targets OPEN models via
        # Ollama, via its OpenAI-compatible /v1 endpoint with NATIVE tool-calling (`_resolve_llm_kwargs`).
        # The prior default (spore-358) forced PROMPT-mode on the `ollama/` litellm provider because that
        # provider drops glm/kimi tool-calls to JSON-TEXT (no structured `tool_calls` → OpenHands ends the
        # turn). The bake-off (2026-07-17) proved that a ROUTE artifact, not a model limit: the /v1 endpoint
        # returns structured tool_calls, so native FC completes multi-step tasks reliably (glm 10/10, kimi
        # 9/10→10/10, minimax unaffected 10/10) and 2–9x faster than prompt-mode's schema+example+regex. The
        # act-first directive + narrate-without-act backstop remain as belt-and-suspenders (0 nudges needed
        # under /v1-native). Tool EXECUTION is unchanged (confined editor + sandboxed bash); only the CALL
        # channel differs. Full data + the repeatable harness: `bench/entity_bakeoff.py`.
        llm = LLM(usage_id="levain-run", **_resolve_llm_kwargs(model, base_url, api_key))
        # The confined executor-tool bundle (file editor + sandboxed bash, both fenced to the shared
        # crown-jewels floor) — the entity's hands. `build_entity_tools` is the ONLY blessed builder,
        # confined by construction; `None` (--no-tools) = a pure conversational partner. bash is
        # granted only where an OS confinement floor exists (fail-closed: no sandbox → no bash, never
        # an unconfined shell) — the honesty floor the banner then displays.
        bash_ok = confinement_supported()
        # Validate .levain/confinement.json EARLY (fail-closed BEFORE the banner, so a config typo is a
        # clean startup error — not a stack trace on the first turn when a tool resolves the config),
        # and read the floor-shaping fields so the honesty-floor banner reflects the ACTUAL floor (a
        # static "~/.ssh protected" line would LIE under ssh_mode="raw"; a static floor list would omit
        # the opted-in standard cred stores — apparatus L1).
        _cfg = load_confinement_config(entity_dir) if with_tools else None
        ssh_mode = _cfg.ssh_mode if _cfg is not None else "agent"
        deny_standard_creds = _cfg.deny_standard_creds if _cfg is not None else False
        entity_tools = build_entity_tools(with_bash=bash_ok) if with_tools else None
        binding = build_entity_agent(entity_dir, llm, tools=entity_tools)
        workspace = entity_dir / _WORKSPACE_SUBDIR
        assert_workspace_isolated(workspace, entity_dir=entity_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        # Re-guard AFTER mkdir, right before use (codex L3 TOCTOU): mkdir(exist_ok=True)
        # follows a symlink swapped in after the first assert, so re-assert at the point of
        # use — the invariant fires at USE, not once at validation. (The confined file tool ALSO
        # re-guards every path per op, so this is the workspace-DIR fence; the tool is the per-file
        # fence — defense-in-depth, both structural.)
        assert_workspace_isolated(workspace, entity_dir=entity_dir)
        # visualizer=None: the REPL owns all output (it renders the turn's tool activity + the
        # assistant reply itself, so the SDK's own visualizer would double-print). `Any`: the SDK types
        # `Conversation(...)` as the abstract `BaseConversation`, which under-declares the
        # concrete `send_message` / `state` surface (shipped `capture.vagus_run` sidesteps
        # this by leaving its conversation param untyped).
        conversation: Any = Conversation(
            binding.agent, workspace=str(workspace), visualizer=None
        )
    except IsolationError as exc:
        print(f"levain run: sovereignty guard REFUSED to start the entity:\n  {exc}")
        return 2
    except ConfinementError as exc:
        print(
            f"levain run: the confinement config is invalid — fix it and retry:\n  {exc}\n"
            f"  ({entity_dir / ENTITY_STORE_SUBDIR / 'confinement.json'})"
        )
        return 2
    except Exception as exc:  # noqa: BLE001 — a bad model/endpoint config is a usage error
        print(
            f"levain run: could not start the entity ({exc}).\n"
            f"  Check --model / --base-url (default: an open model via local Ollama)."
        )
        return 2

    _print_banner(
        entity_dir, binding, model=_resolve_model(model),
        with_tools=with_tools, bash_ok=with_tools and bash_ok, ssh_mode=ssh_mode,
        deny_standard_creds=deny_standard_creds,
    )

    reader = TurnReader()
    interrupted = False
    try:
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
                conversation.send_message(message)
                conversation.run()
                # Narrate-without-act backstop (spore-358 follow-through, harness-agnosticism): a weak
                # open model often ENDS a turn by describing its plan ("I'll run the tests…") with no
                # tool call — OpenHands reads that plan as a valid answer and stops, task untouched (the
                # DOMINANT failure across glm-5.2 / kimi in the 2026-07-17 bake-off; an act-first prompt
                # lifted glm 2/3 -> 3/3). Nudge ONCE to execute, then re-run — the structural equivalent
                # of that prompt, applied to WHATEVER model is driving. Capped at 1 (never loops);
                # tools-only (a --no-tools partner has nothing to execute); the nudge is a synthetic
                # user turn filtered by `is_corrective_nudge`, so it never shows on screen nor in memory.
                # The SDK's own nudge covers the no-message case; this covers plan-as-message.
                if with_tools and planned_without_acting(conversation.state.events):
                    conversation.send_message(LEVAIN_ACT_NUDGE)
                    conversation.run()
                # Capture the turn ONCE, AFTER the (possible) nudge cycle — so the episode reflects the
                # completed WORK, not the abandoned plan. Capturing before the nudge (the first cut)
                # tripped vagus_run's turn-id idempotency: the post-nudge capture no-oped on the
                # unchanged user-turn id and memory kept only the stall while the screen showed the work
                # — the exact capture-vs-display divergence agent_reply exists to forbid (L1+L2 review
                # 2026-07-17). One turn -> one episode -> final state; capture_turn's own run() is a
                # no-op now (the turn already ran), it just performs the single capture.
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

            _render_turn(conversation, binding, workspace)
    finally:
        _close_quietly(conversation)

    _farewell(binding, interrupted=interrupted)
    return 0


def _turn_tool_activity(events, workspace: Path) -> list[str]:
    """The tool actions the entity ran THIS turn (since the last genuine user message), as compact
    display lines — so a workspace file op is VISIBLE, never silent (with hands, a turn is no longer
    just a reply). Paths are shown workspace-relative for readability; the boundary skips the SDK's
    synthetic corrective nudge exactly as ``_latest_agent_text`` does, so activity keys on the real
    turn."""
    evs = list(events)
    last_user = None
    for i in range(len(evs) - 1, -1, -1):
        if getattr(evs[i], "source", None) == "user" and not is_corrective_nudge(evs[i]):
            last_user = i
            break
    start = 0 if last_user is None else last_user
    prefix = str(workspace).rstrip(os.sep) + os.sep
    lines: list[str] = []
    for e in evs[start:]:
        if getattr(e, "source", None) != "agent":
            continue
        summary = tool_action_summary(e)
        if summary is None:
            continue
        tool_name, detail = summary
        lines.append(f"⚙ {tool_name}: {detail.replace(prefix, '')}")
    return lines


def _render_turn(conversation: Any, binding, workspace: Path) -> None:
    """Print the turn: the entity's tool ACTIVITY (what it did to its workspace) then its reply."""
    events = conversation.state.events
    for line in _turn_tool_activity(events, workspace):
        print(f"  \033[2m{line}\033[0m")  # dim — activity is context, the reply is the message
    reply = _latest_agent_text(events)
    print(f"\n\033[1m{_entity_label(binding)} ›\033[0m {reply or '(no reply)'}")


def _close_quietly(conversation: Any) -> None:
    """Release the SDK conversation's resources (atexit also calls close(), but the REPL owns one
    session per process, so close deterministically here). The file executor is in-process and holds
    no subprocess; the future bash slice's OS-sandbox process is what will make this teardown
    load-bearing."""
    try:
        conversation.close()
    except Exception:  # noqa: BLE001 — teardown must never raise
        pass


def _entity_label(binding) -> str:
    """The entity's display name for the prompt — its dir name (the sovereign handle)."""
    return binding.entity_dir.name


def _print_banner(
    entity_dir: Path, binding, *, model: str, with_tools: bool, bash_ok: bool,
    ssh_mode: str = "agent", deny_standard_creds: bool = False,
) -> None:
    """The session header — and the HONESTY FLOOR: show the operator exactly which stores
    this entity reads/writes, what hands it has, AND what the crown-jewels floor keeps off-limits, so
    sovereignty is VISIBLE, not merely asserted. The floor lines are rendered from the ACTUAL config
    (``ssh_mode``), never a static string that could invert under a real setting (apparatus L1)."""
    print("=" * 66)
    print(f"  levain run — {entity_dir.name}  (sovereign entity)")
    print("=" * 66)
    print(f"  model:     {model}")
    print(f"  memory:    {binding.episodic_path}")
    print(f"             {binding.crystal_path}")
    print(f"  workspace: {entity_dir / _WORKSPACE_SUBDIR}")
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
    print("  Talk to it. It recalls its OWN memory and captures each turn there.")
    print("  A pasted block is ONE message — or :paste … :end to be explicit.")
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
