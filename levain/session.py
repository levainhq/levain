"""levain.session — the ENTITY SESSION: one sovereign entity, held open, driven by anyone.

This module owns the thing ``levain run`` used to own privately: a live, isolated entity —
its binding, its confined hands, its workspace fence, and its OpenHands ``Conversation`` —
plus the one operation that matters, :meth:`EntitySession.run_turn`.

**Why it exists (the K1 keystone).** ``run_entity`` welded entity CONSTRUCTION to the
interactive REPL LOOP inside a single function, so the only thing that could ever drive an
entity was a human typing at a tty. Every remaining Levain v2 deployment mode needs to drive
one WITHOUT that human:

  - the **headless runner** — ``levain run --task <spec>``, exit-when-done, exit code;
  - the **web cockpit chat** — a ``Conversation`` held in SERVER memory across requests;
  - the **local constellation seat** — a scheduled, supervised, unattended entity.

Those are not three builds. They are three DRIVERS over one seam, which is this module.
BUILD ONCE, UNLOCK THREE.

**Nothing about sovereignty is re-decided here.** The extraction moved the guard sequence
verbatim — the store guard fail-closes inside ``build_entity_agent`` before anything is
built; the workspace fence is asserted BEFORE ``mkdir`` and AGAIN after it (the codex-L3
TOCTOU re-guard: ``mkdir(exist_ok=True)`` follows a symlink swapped in after the first
assert, so the invariant has to fire at the point of USE, not once at validation); the
confinement config is validated EARLY so a typo is a clean startup error rather than a stack
trace on the first turn. A session that cannot be built sovereignly is not built at all.

**The honesty floor on RESULTS — read this before adding a success signal.** A
:class:`TurnResult` reports only what the HARNESS observed: did the turn complete, did the
agent emit a reply, did it raise. It deliberately carries **no notion of "the task
succeeded,"** because the entity cannot supply one truthfully. That is not a hypothetical:
a confined entity reported *"25 failed, 1617 passed… pre-existing on clean base"* for a
suite that was **1702 passed / 0 failed** outside the sandbox — its control was sound, its
conclusion was right, and its diagnosis was wrong (``spore-373`` blocker [5]). An exit code
derived from an agent's self-assessment would ship exactly the false-all-clear class
Levain's trust pitch says cannot happen — the same shape as a ``levain doctor`` that
validates wiring and calls a scrambled seed green. **Harness-observable facts only.** If a
caller wants task-level success, it must verify the WORLD (tests, diffs, exit codes of real
commands), never ask the agent how it did.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from levain.firing.agent_reply import (
    LEVAIN_ACT_NUDGE,
    finish_message,
    humanize_finish_json,
    is_corrective_nudge,
    message_event_text,
    planned_without_acting,
    tool_action_summary,
)
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
from levain.install import effective_adapter

__all__ = [
    "EXIT_OK",
    "EXIT_INTERRUPTED",
    "EXIT_NO_REPLY",
    "EXIT_USAGE",
    "EXIT_TURN_FAILED",
    "EntitySession",
    "SessionStartError",
    "TurnResult",
    "WORKSPACE_SUBDIR",
    "require_openhands_entity",
]

# The confined file-authority root for a session: a sibling of the sovereign store, NEVER the
# operator's cwd/$HOME. The fence is structural, so file authority is bounded BEFORE any tool
# can spend it (the capability-minting discipline).
WORKSPACE_SUBDIR = "workspace"

# ---------------------------------------------------------------------------
# Exit codes — the process-level contract for every non-interactive driver.
#
# These describe what the HARNESS saw. See the module docstring: none of them is derived from
# the agent's own account of its work, and none ever should be.
# ---------------------------------------------------------------------------
EXIT_OK = 0
"""The turn ran to completion and the agent produced a reply."""

EXIT_NO_REPLY = 1
"""The turn ran to completion but the agent produced NO reply.

A real, observable failure mode — not a crash. A weak open model can end a turn having said
nothing usable (the act-first nudge exists for the neighbouring case, a turn that narrates a
plan without acting). A driver that treated this as success would report a no-op as done."""

EXIT_USAGE = 2
"""A usage / environment / startup error, surfaced BEFORE any turn ran.

Missing ``openhands`` extra, not an initialized entity, a sovereignty refusal, an invalid
confinement config, a bad ``--model``. Unchanged from ``levain run``'s existing convention."""

EXIT_TURN_FAILED = 3
"""The turn itself raised. The session is left un-resumable by design: a raised ``run()``
leaves the SDK conversation in an ERROR state a later turn would resume FROM."""

EXIT_INTERRUPTED = 130
"""The operator interrupted the run (SIGINT / Ctrl-C). POSIX convention: 128 + SIGINT(2).

Distinct from :data:`EXIT_USAGE` deliberately (glm L3, HIGH). An interrupt is not a
configuration error, and conflating them is actively misleading in the unattended-supervision
case this whole keystone exists to enable: a supervisor seeing ``2`` goes looking for a bad
``--model`` or a missing extra, when in fact a human pressed Ctrl-C."""

_CLOSED_SESSION_ERROR = "session is closed"
"""The :attr:`TurnResult.error` marker for a turn attempted on an already-closed session.

Distinctive on purpose: a driver that wants to treat stale-handle use as a bug can still
detect it (``result.error == _CLOSED_SESSION_ERROR``) without ``run_turn`` having to raise."""


class SessionStartError(Exception):
    """A session could not be started, with the operator-facing reason already rendered.

    Carries the exact multi-line message a driver should print plus the process
    :attr:`code`, so the REPL, the headless runner, and the server all fail IDENTICALLY
    instead of each re-deriving the wording (and drifting). ``str(exc)`` is the message.
    """

    def __init__(self, message: str, *, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class TurnResult:
    """What the harness observed about one completed turn.

    Deliberately NOT a verdict on the task. See the module docstring — the entity cannot
    truthfully report on its own environment, so this records only observable facts.
    """

    reply: str | None
    """The agent's text for this turn, or ``None`` if it produced none."""

    tool_activity: list[str] = field(default_factory=list)
    """Compact display lines for the tool actions run this turn (workspace-relative)."""

    error: str | None = None
    """The exception text if the turn raised, else ``None``."""

    nudged: bool = False
    """Whether the act-first backstop fired (the agent narrated a plan without acting)."""

    @property
    def ok(self) -> bool:
        """The turn completed AND produced a reply. Not 'the task succeeded'."""
        return self.error is None and bool(self.reply)

    @property
    def exit_code(self) -> int:
        """The process exit code this turn implies, for a non-interactive driver."""
        if self.error is not None:
            return EXIT_TURN_FAILED
        return EXIT_OK if self.reply else EXIT_NO_REPLY


def require_openhands_entity(entity_dir: Path) -> str | None:
    """Return an error message if ``entity_dir`` is not a clean, initialized OpenHands entity,
    else ``None``.

    Shared by every driver (``levain run``, ``levain wrap``, the headless runner, the server)
    so they agree on EXACTLY what a runnable sovereign entity is: an initialized ``.levain/``
    store AND a CLEAN openhands adapter (hosted files dominate a possibly-stale marker via
    :func:`~levain.install.effective_adapter`, so a claude-code/codex store or a
    residue-bearing mixed install is refused, not silently driven). The caller prefixes its
    own ``levain run:`` / ``levain wrap:`` label; this returns the bare reason + fix so the
    commands cannot drift on the definition."""
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


def resolve_model_label(model: str) -> str:
    """Prefix a bare model name with the Ollama provider — for the operator-facing BANNER label
    only. The LLM route itself is :func:`resolve_llm_kwargs`; this keeps the banner showing the
    plain ``ollama/<name>`` identity the operator thinks in, not the litellm protocol detail.

    ``glm-5.2:cloud`` → ``ollama/glm-5.2:cloud``. A name already carrying a ``provider/`` prefix
    is passed through untouched, so an advanced operator can point at any litellm-routable model.
    """
    return model if "/" in model else f"ollama/{model}"


def resolve_llm_kwargs(model: str, base_url: str, api_key: str | None) -> dict:
    """LLM kwargs — route OPEN (Ollama) models through Ollama's OpenAI-compatible ``/v1``
    endpoint with NATIVE tool-calling.

    This REPLACES the spore-358 default (the ``ollama/`` litellm provider + PROMPT-mode
    tool-calling). That default existed because the ``ollama/`` provider drops glm/kimi
    tool-calls to JSON-TEXT — the model emits a well-formed call, but not as a structured
    ``tool_calls`` entry, so OpenHands sees a plain message and ends the turn, task untouched.
    The fix was mis-attributed to model tier; it is the ROUTE. Bake-off 2026-07-17 (multi-file
    coding task, n=10):
      glm  — ollama/native 0/5 · ollama/prompt 10/10 (63s) · /v1-native 10/10 (35s)
      kimi —                     ollama/prompt  9/10 (106s) · /v1-native 10/10 (12s)
      minimax unaffected: /v1-native 10/10. All /v1-native: 0 backstop nudges.
    A provider-prefixed model an advanced operator passes (``openai/…``, ``anthropic/…``) is
    honored as-is with native FC — the Ollama ``/v1`` reroute would be wrong for it.
    """
    if "/" in model and not model.startswith("ollama/"):
        return {"model": model, "base_url": base_url, "api_key": api_key,
                "native_tool_calling": True}
    bare = model.split("/", 1)[1] if model.startswith("ollama/") else model
    if not bare.strip():
        # A prefix-only (`ollama/`) or empty --model would resolve to `openai/` — an invalid
        # model that fails on the FIRST turn, not at startup. Fail CLOSED here, matching the
        # entity's startup-validation discipline (codex + gpt-oss L3 2026-07-17).
        raise ValueError(f"--model must name a model, not just a provider prefix (got {model!r})")
    host = base_url.rstrip("/")
    v1 = host if host.endswith("/v1") else f"{host}/v1"
    # `api_key is not None` (not truthiness): an operator who deliberately passes an EMPTY key
    # keeps it — only an UNSET key falls back to the Ollama sentinel (codex + gpt-oss L3).
    return {"model": f"openai/{bare}", "base_url": v1,
            "api_key": api_key if api_key is not None else "ollama",
            "native_tool_calling": True}


def turn_tool_activity(events, workspace: Path) -> list[str]:
    """The tool actions the entity ran THIS turn (since the last genuine user message), as
    compact display lines — so a workspace file op is VISIBLE, never silent. Paths are shown
    workspace-relative; the boundary skips the SDK's synthetic corrective nudge exactly as
    :func:`latest_agent_text` does, so activity keys on the real turn."""
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


def latest_agent_text(events) -> str | None:
    """The assistant's text from the just-completed turn — the ``source == "agent"`` events
    AFTER the last genuine human message.

    Shares its event-shape parsing with ``capture.render_turn`` via
    :mod:`levain.firing.agent_reply` (pure/duck-typed, so this stays SDK-free for the tests),
    so the shown reply and the captured episode never diverge: an agent reply is either a
    ``MessageEvent`` (``message_event_text``) or an ``ActionEvent(FinishAction)``
    (``finish_message`` — the SDK routes a no-tool answer through the built-in ``finish`` tool,
    not a MessageEvent). The boundary skips the SDK's synthetic corrective nudge so a
    weak-model turn keys on the real question. ``None`` when there is no agent text yet."""
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


@dataclass
class EntitySession:
    """A live sovereign entity: binding + conversation + workspace, driven by :meth:`run_turn`.

    Build with :meth:`open` — never construct directly; ``open`` is where the sovereignty
    guards live, and a directly-constructed session would bypass them.
    """

    entity_dir: Path
    binding: Any
    conversation: Any
    workspace: Path
    model_label: str
    with_tools: bool
    bash_ok: bool
    ssh_mode: str = "agent"
    deny_standard_creds: bool = False
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    # -- construction --------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        model: str = "glm-5.2:cloud",
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        with_tools: bool = True,
        on_event: Callable[[str], None] | None = None,
        max_iterations: int | None = None,
    ) -> EntitySession:
        """Open a sovereign session for the entity at ``path``.

        ``with_tools`` (default True) grants the confined HANDS — a file editor plus, where an
        OS confinement floor exists (macOS ``sandbox-exec``), a persistent sandboxed bash —
        both fenced to the shared crown-jewels floor. On a platform with no OS sandbox, bash is
        dropped and only the file editor is granted (honesty floor, NEVER an unconfined
        fallback).

        ``on_event`` opts into LIVE tool-activity streaming: it is called with a display line
        as each tool action happens, rather than the caller waiting for the whole turn. This is
        what makes a long headless turn observable instead of a silent process. A driver that
        streams should NOT also print :attr:`TurnResult.tool_activity`, or every action shows
        twice.

        ``max_iterations`` bounds a single turn's agent steps — the SDK's
        ``max_iteration_per_run``. ``None`` keeps the SDK default. A bounded turn is what stops
        an unattended entity from spending forever on one message.

        Raises :class:`SessionStartError` (message already operator-ready) if the entity cannot
        be started sovereignly.
        """
        entity_dir = Path(str(path)).expanduser().resolve()

        err = require_openhands_entity(entity_dir)
        if err:
            raise SessionStartError(err)

        # Lazy — the entity chokepoint imports the OpenHands SDK at module level; keep it out
        # of `levain --help` / `import levain.session`, and turn a missing extra into a hint.
        os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
        try:
            from openhands.sdk import LLM, Conversation

            from levain.firing.openhands.entity import build_entity_agent
            from levain.firing.openhands.tools import build_entity_tools
        except ImportError as exc:
            raise SessionStartError(
                "the OpenHands runtime is not installed.\n"
                "  Install the extra:  pip install 'levain[openhands]'\n"
                f"  ({exc})"
            ) from exc

        # Quiet the SDK's own INFO/WARNING chatter + litellm's cost warnings, so the banner's
        # clean-output promise holds; real ERRORs still surface.
        for noisy in ("openhands", "LiteLLM", "litellm"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

        # The sovereignty-critical construction, under one handler. Order is load-bearing and
        # is preserved verbatim from the REPL this was extracted out of:
        #   - build_entity_agent fail-CLOSES on the STORE guard before building anything;
        #   - assert_workspace_isolated ENFORCES the FILE fence BEFORE the workspace exists;
        #   - it fires AGAIN after mkdir (codex L3 TOCTOU — mkdir(exist_ok=True) follows a
        #     symlink swapped in after the first assert, so the invariant fires at USE);
        #   - a bad --model/--base-url is a usage error → clean exit 2, not a raw traceback.
        try:
            llm = LLM(usage_id="levain-run", **resolve_llm_kwargs(model, base_url, api_key))
            # Fail CLOSED if support cannot be determined: an undetermined sandbox means NO
            # bash, never an unconfined fallback (the honesty floor). Own handler so this can
            # never surface as the generic "check --model / --base-url" advice, which would
            # point the operator at the wrong thing entirely (glm L3).
            try:
                bash_ok = confinement_supported()
            except Exception:  # noqa: BLE001 — undetermined support == unsupported
                bash_ok = False
            # Validate .levain/confinement.json EARLY (fail-closed BEFORE any banner, so a
            # config typo is a clean startup error — not a stack trace on the first turn), and
            # read the floor-shaping fields so an honesty-floor banner reflects the ACTUAL
            # floor (a static "~/.ssh protected" line would LIE under ssh_mode="raw").
            cfg = load_confinement_config(entity_dir) if with_tools else None
            ssh_mode = cfg.ssh_mode if cfg is not None else "agent"
            deny_standard_creds = cfg.deny_standard_creds if cfg is not None else False
            entity_tools = build_entity_tools(with_bash=bash_ok) if with_tools else None
            binding = build_entity_agent(entity_dir, llm, tools=entity_tools)
            workspace = entity_dir / WORKSPACE_SUBDIR
            assert_workspace_isolated(workspace, entity_dir=entity_dir)
            workspace.mkdir(parents=True, exist_ok=True)
            assert_workspace_isolated(workspace, entity_dir=entity_dir)

            conv_kwargs: dict[str, Any] = {
                "workspace": str(workspace),
                # visualizer=None: the DRIVER owns all output, so the SDK's own visualizer
                # would double-print.
                "visualizer": None,
            }
            if on_event is not None:
                conv_kwargs["callbacks"] = [_activity_callback(on_event, workspace)]
            if max_iterations is not None:
                conv_kwargs["max_iteration_per_run"] = max_iterations
            # `Any`: the SDK types `Conversation(...)` as the abstract `BaseConversation`,
            # which under-declares the concrete `send_message` / `state` surface.
            conversation: Any = Conversation(binding.agent, **conv_kwargs)
        except IsolationError as exc:
            raise SessionStartError(
                f"sovereignty guard REFUSED to start the entity:\n  {exc}"
            ) from exc
        except ConfinementError as exc:
            raise SessionStartError(
                f"the confinement config is invalid — fix it and retry:\n  {exc}\n"
                f"  ({entity_dir / ENTITY_STORE_SUBDIR / 'confinement.json'})"
            ) from exc
        except Exception as exc:  # noqa: BLE001 — a bad model/endpoint config is a usage error
            raise SessionStartError(
                f"could not start the entity ({exc}).\n"
                f"  Check --model / --base-url (default: an open model via local Ollama)."
            ) from exc

        return cls(
            entity_dir=entity_dir,
            binding=binding,
            conversation=conversation,
            workspace=workspace,
            model_label=resolve_model_label(model),
            with_tools=with_tools,
            bash_ok=with_tools and bash_ok,
            ssh_mode=ssh_mode,
            deny_standard_creds=deny_standard_creds,
        )

    # -- the one operation ---------------------------------------------------

    def run_turn(self, message: str) -> TurnResult:
        """Send ``message``, run the turn to completion, capture it, and report what happened.

        The sequence is the REPL's, unchanged and in this order for reasons that were paid for:

          1. ``send_message`` + ``run()``.
          2. The **narrate-without-act backstop**: a weak open model often ENDS a turn by
             describing its plan ("I'll run the tests…") with no tool call — OpenHands reads
             that plan as a valid answer and stops, task untouched (the DOMINANT failure across
             glm-5.2 / kimi in the 2026-07-17 bake-off). Nudge ONCE to execute, then re-run.
             Capped at 1, never loops; tools-only; the nudge is a synthetic user turn filtered
             by ``is_corrective_nudge``, so it never shows on screen nor in memory.
          3. Capture ONCE, **AFTER** the possible nudge cycle — so the episode reflects the
             completed WORK, not the abandoned plan. Capturing before the nudge tripped
             ``vagus_run``'s turn-id idempotency: the post-nudge capture no-oped on the
             unchanged user-turn id, and memory kept only the stall while the screen showed the
             work — the exact capture-vs-display divergence ``agent_reply`` exists to forbid.

        Never raises for a failed turn: the error is returned in the result, because a driver
        holding a long-lived session (the server) must not lose the session to one bad turn.
        The conversation IS left un-resumable after an error — see :attr:`EXIT_TURN_FAILED`.
        """
        if self._closed:
            # A RESULT, not a raise — codex L3, and it caught a real self-contradiction: this
            # method's own contract is "never raises for a failed turn, because a driver
            # holding a long-lived session must not lose the session to one bad turn," and the
            # driver that motivates that contract is the very one most likely to hit this — a
            # server holding a session registry, handed a STALE session id by a reconnecting
            # client. Raising there kills the route over a race the route can handle. The
            # marker string stays distinctive so a driver can still detect genuine misuse.
            return TurnResult(
                reply=None, tool_activity=[], error=_CLOSED_SESSION_ERROR,
            )
        nudged = False
        try:
            self.conversation.send_message(message)
            self.conversation.run()
            if self.with_tools and planned_without_acting(self.conversation.state.events):
                nudged = True
                self.conversation.send_message(LEVAIN_ACT_NUDGE)
                self.conversation.run()
            self.binding.capture_turn(self.conversation)
        except Exception as exc:  # noqa: BLE001 — a failed turn is a RESULT, not a crash
            return TurnResult(reply=None, tool_activity=[], error=str(exc), nudged=nudged)

        events = self.conversation.state.events
        return TurnResult(
            reply=latest_agent_text(events),
            tool_activity=turn_tool_activity(events, self.workspace),
            error=None,
            nudged=nudged,
        )

    # -- lifecycle -----------------------------------------------------------

    def wrap_nudge(self) -> str | None:
        """A consolidate nudge if the entity's store is due. Re-guards the store at USE time
        and fail-softs to ``None``, so this never leaks and never crashes a caller."""
        try:
            return self.binding.wrap_nudge()
        except Exception:  # noqa: BLE001 — a nudge must never raise
            return None

    def close(self) -> None:
        """Release the SDK conversation's resources. Idempotent; never raises.

        The bash slice's OS-sandbox process is what makes this teardown load-bearing — a
        server holding N sessions leaks a sandboxed shell per session without it."""
        if self._closed:
            return
        self._closed = True
        try:
            self.conversation.close()
        except Exception:  # noqa: BLE001 — teardown must never raise
            pass

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def label(self) -> str:
        """The entity's display name — its dir name (the sovereign handle)."""
        return self.entity_dir.name

    def __enter__(self) -> EntitySession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _activity_callback(on_event: Callable[[str], None], workspace: Path):
    """Adapt the SDK's per-event callback to a display-line emitter.

    Filters to agent tool actions and renders them workspace-relative, matching
    :func:`turn_tool_activity`'s formatting exactly so a streamed line and a post-turn line are
    byte-identical. Fail-soft: a callback that raises would propagate into the SDK's run loop
    and kill a turn over a display concern."""
    prefix = str(workspace).rstrip(os.sep) + os.sep

    def _cb(event: object) -> None:
        try:
            if getattr(event, "source", None) != "agent":
                return
            summary = tool_action_summary(event)
            if summary is None:
                return
            tool_name, detail = summary
            on_event(f"⚙ {tool_name}: {detail.replace(prefix, '')}")
        except Exception:  # noqa: BLE001 — display must never break the run
            pass

    return _cb
