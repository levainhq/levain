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
from levain.firing.deadline import TurnTimeout
from levain.firing.drive import (
    DriveMode,
    bind_drive_mode,
    human_present,
    resolve_cred_floor,
)
from levain.firing.gate import (
    GateMode,
    PendingEfferent,
    resolve_gate_mode,
)
from levain.firing.isolation import (
    ENTITY_STORE_SUBDIR,
    IsolationError,
    assert_workspace_isolated,
)
from levain.install import effective_adapter

__all__ = [
    "EXIT_GATED",
    "EXIT_OK",
    "EXIT_INTERRUPTED",
    "EXIT_NO_REPLY",
    "EXIT_TIMEOUT",
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

EXIT_GATED = 4
"""The turn HALTED AT THE EFFERENT GATE with actions awaiting the human (**K3**, ``spore-295``).

Its own code, and that is the point. Without it a gated halt would surface as
:data:`EXIT_NO_REPLY` — because a halted turn genuinely has no reply yet — and a supervisor would
read *"the entity is waiting for you"* as *"the entity stalled"*. Those demand opposite responses:
one wants a human decision, the other wants a restart or an alarm. Conflating them re-creates, at
the process boundary, exactly the ambiguity the other four codes were written to remove.

**Not a failure.** The gate firing is the governor working — the entity proposed action on the
world and the harness stopped it pending fan-in. A supervisor should treat ``4`` as *needs a
decision*, never as an error to retry blindly (retrying re-proposes the same action into the same
gate). The actions themselves are REPORTED, not persisted to a queue: nothing survives process
exit, so a headless driver's report is the whole handoff. Naming that limit is deliberate — a
durable pending-action queue is the unattended seat's problem (K4a), and claiming one here that
does not exist is the ``claim > enforcement`` gap the floor's own discipline forbids."""

EXIT_TIMEOUT = 5
"""The turn EXCEEDED ITS WALL-CLOCK BOUND and was terminated (**K4a ⑥**, ``spore-434``).

Its own code, for the same reason :data:`EXIT_GATED` has one: collapsing it into an existing code
inverts the operator's response. The tempting home is :data:`EXIT_TURN_FAILED` — a timeout does
arrive as an exception — but ``3`` tells a supervisor *the work raised, the session is
un-resumable, go read the traceback*, and it is a code that recurs identically on a retry because
the cause is in the code. A timeout says the opposite: **the ENVIRONMENT stalled** (a hung model
call, a socket with no read timeout), nothing is wrong with the entity or the task, and the next
scheduled run is free to start and will very likely succeed.

**Not a failure of the task, and not a failure of governance.** It is the time bound working — the
half of K4a's *"supervised / restartable / time-bounded"* definition that ``--max-iterations``
structurally cannot supply, because iterations count STEPS and the failure mode is one step that
never returns. A supervisor should treat ``5`` as *retry on the cadence, and if it repeats, the
model endpoint is sick* — never as *the entity is broken*.

**Nothing is captured to memory on a timeout**, by the same discipline that refuses to capture a
gated halt: the turn was killed mid-flight, so what completed is unknowable, and an episode
asserting a completed turn would be memory recording a fiction."""

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

    gated: bool = False
    """The turn STOPPED at the efferent gate, awaiting a human decision (**K3**).

    Deliberately a FIELD, not a property derived from :attr:`pending` being non-empty. The
    runtime's execution status is what authoritatively says a turn halted; the pending list is a
    DESCRIPTION of what it halted on, built by reading and filtering events. Deriving the fact
    from the description means any failure to describe — an unreadable action, a batch this
    build filtered to nothing — silently reports the turn as complete, and the driver then
    captures and exits on a turn whose actions never ran. Authority and description are separate
    because they fail separately."""

    timed_out: bool = False
    """The turn was terminated for EXCEEDING ITS WALL-CLOCK BOUND (**K4a ⑥**, ``spore-434``).

    A FIELD set from the exception TYPE we ourselves raised, for the same reason :attr:`gated` is a
    field: the alternative is matching text in :attr:`error`, and an outcome recognised from its own
    description silently reclassifies the day the wording changes. Here that reclassification has a
    specific cost — the timeout would collapse into :data:`EXIT_TURN_FAILED`, telling a supervisor
    to go read a traceback for a stalled network call.

    A timed-out turn always also carries an :attr:`error` (the bound it exceeded), because it did
    not complete. :attr:`exit_code` therefore checks this FIRST — see there."""

    pending: tuple[PendingEfferent, ...] = ()
    """What the gate is holding, for the human to judge.

    May be empty even when :attr:`gated` is ``True`` (see above) — an anomaly the report itself
    surfaces rather than hides. Each entry carries what the entity proposed and why that
    classification fanned in, because an operator deciding on ``git push --force`` needs the
    command, not the tool's name."""

    @property
    def ok(self) -> bool:
        """The turn completed AND produced a reply. Not 'the task succeeded'.

        A gated turn is not ``ok`` — it has not finished. It is also not an ERROR, which is why
        the two are separate properties rather than one tri-state: a driver that only asks
        ``ok`` still behaves correctly (it does not treat a halt as success), and a driver that
        wants to offer the human a decision asks :attr:`gated`."""
        return self.error is None and not self.gated and not self.timed_out and bool(self.reply)

    @property
    def exit_code(self) -> int:
        """The process exit code this turn implies, for a non-interactive driver.

        Order is load-bearing, and every step of it was paid for:

        - **A timeout beats the generic raise.** A timed-out turn always carries an ``error`` too
          (it did not complete), so checking ``error`` first would collapse every timeout into
          :data:`EXIT_TURN_FAILED` and tell a supervisor to hunt a traceback for a hung socket.
          More specific outcome, earlier check.
        - **A raised turn beats a gated one** — an exception means we cannot trust what the pending
          list even is.
        - **A gated turn beats the reply check** — otherwise a halt, which by construction has
          produced no reply yet, would report as a stall.
        """
        if self.timed_out:
            return EXIT_TIMEOUT
        if self.error is not None:
            return EXIT_TURN_FAILED
        if self.gated:
            return EXIT_GATED
        return EXIT_OK if self.reply else EXIT_NO_REPLY


def _apply_drive_policy(cfg: Any, mode: DriveMode) -> bool:
    """Publish the drive mode on the fork-safe channel AND resolve the cred floor from it.

    **One function on purpose, because the two must not drift.** The crown-jewels floor has one
    policy and two enforcers: the bash seatbelt is rendered from the value this RETURNS, while the
    file-editor policy is rebuilt PER CALL outside the session (fork-safety) and reads the mode
    back off ``$LEVAIN_DRIVE_MODE``. Bind without resolving and bash gets the wrong floor; resolve
    without binding and the FILE EDITOR does — and the file editor is the ``view`` path, i.e. the
    afferent read the unattended cred floor exists to close. Two adjacent statements held together
    by a comment is exactly how that drifts, so they are one call with one test.

    Resolving through :func:`~levain.firing.drive.resolve_cred_floor` rather than reading
    ``cfg.deny_standard_creds`` directly is load-bearing: the field is a TRI-STATE whose ``None``
    means "derive from the drive", so a raw read silently treats an UNDECLARED entity as opted-OUT
    and reinstates the exact hole this closes.

    ``cfg`` is ``None`` for a ``--no-tools`` session; the mode is still published (nothing else
    would) and the floor still resolves, harmlessly, for hands that do not exist.
    """
    bind_drive_mode(mode)
    return resolve_cred_floor(
        cfg.deny_standard_creds if cfg is not None else None, mode=mode
    )


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
    gate_mode: GateMode = "gated"
    """The resolved EFFERENT GATE posture for this session (**K3**).

    Defaults to ``"gated"`` — fail-closed. A caller that forgets to declare whether a human is
    driving gets the governed posture, which costs an unnecessary halt; the opposite default
    would silently hand an unattended seat ungoverned hands, and only one of those two mistakes
    is recoverable after the fact."""
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
        mode: DriveMode = "headless",
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

        ``mode`` declares HOW this session is being driven, and it is the SINGLE AUTHORITY both
        drive-dependent policies resolve against (:mod:`levain.firing.drive`):

        - ``"interactive"`` — the REPL. A human is watching activity stream past.
        - ``"headless"`` — ``--task``, typed by a human who will read the output afterwards.
        - ``"unattended"`` — a scheduled seat. A scheduler invoked it; nobody necessarily reads
          anything.

        The **efferent gate** (K3, ``spore-295``) resolves on ``human_present(mode)``, which is
        ``True`` only for ``interactive`` — an operator watching the stream already IS the fan-in
        the gate exists to provide, so the gate supplies that fan-in exactly when presence does
        not. The gate therefore treats ``headless`` and ``unattended`` identically, which is
        correct: neither has anyone to fan an action in to at the moment it would fire.

        The **crown-jewels cred floor** resolves on the mode DIRECTLY, because it must NOT collapse
        those two: a human typing ``--task "open a PR"`` legitimately needs ``gh``, while a
        scheduled seat's silent credential read can compound into always-loaded memory with nobody
        in the loop. That asymmetry — and why the rest of the floor is deliberately presence-
        INDEPENDENT — is argued in full in :mod:`levain.firing.drive`.

        **It defaults to ``"headless"``** — a caller that forgets gets the GATE armed (governed,
        not ungoverned) while keeping the cred floor at its documented default, so forgetting is
        safe on the axis that can execute and non-surprising on the axis that cannot.

        Raises :class:`SessionStartError` (message already operator-ready) if the entity cannot
        be started sovereignly — INCLUDING a gate that would not arm. A session that believes it
        is gated while executing every efferent action is worse than an honestly ungated one, so
        the wiring is proven at startup rather than assumed.
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
            from levain.firing.openhands.gate import (
                GateArmingError,
                arm_efferent_gate,
                disarm_efferent_gate,
            )
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
            # Publish the drive mode AND resolve the cred floor — ONE call, because the two must
            # not drift (see `_apply_drive_policy`). Must run BEFORE any tool policy is built.
            deny_standard_creds = _apply_drive_policy(cfg, mode)
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

            # The EFFERENT GATE (K3), armed on the live conversation — and this is the LAST step
            # of construction on purpose: it needs the conversation, and a session that reached
            # here ungated must not become reachable by any path that skips it.
            # `human_present(mode)` is a DERIVATION, not a second signal — one drive-mode
            # authority, so the gate and the cred floor can never disagree about how this
            # session is being driven. resolve_gate_mode's own contract is UNCHANGED.
            gate_mode = resolve_gate_mode(
                cfg.efferent_gate if cfg is not None else "auto",
                human_present=human_present(mode),
            )
            if gate_mode == "gated":
                arm_efferent_gate(conversation)
            else:
                disarm_efferent_gate(conversation)
        except GateArmingError as exc:
            # Its OWN handler, ABOVE the generic one: a gate that will not arm must never be
            # reported as "check --model / --base-url", which would send the operator hunting a
            # configuration problem while the actual news is that this entity would run
            # ungoverned. Same lesson as the bash_ok handler below it (glm L3).
            raise SessionStartError(
                f"the efferent gate could not be armed — refusing to start:\n  {exc}\n"
                f"  This entity is configured to gate efferent actions "
                f"(.levain/confinement.json → efferent_gate), and the runtime did not accept it."
            ) from exc
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
            gate_mode=gate_mode,
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

        # REFUSE A NEW MESSAGE WHILE THE GATE IS HOLDING (codex L3, HIGH). ``send_message`` does
        # NOT clear ``WAITING_FOR_CONFIRMATION`` — but the ``run()`` that follows it DOES, and
        # the SDK clears it as *the user approved*: ``Agent.step()`` executes the unmatched
        # pending actions BEFORE it samples any new action. So a driver that answers a halt by
        # simply talking again — "wait, don't push that" — EXECUTES the very push it is objecting
        # to, and the objection arrives afterwards.
        #
        # The REPL drains the gate and ``--task`` exits, so neither reaches this today. But
        # ``EntitySession`` is the SHARED long-lived API: K1's entire premise is that a server's
        # ``/turn`` route and an unattended seat are further DRIVERS over this same object, and
        # the ``/turn`` route is the next thing being built. A footgun reachable only by a caller
        # not yet written is still a footgun, and this one silently breaks the one invariant the
        # gate exists to hold. So it is refused HERE, at the object, rather than by asking every
        # future driver to remember — ``invariant_must_fire_at_the_point_of_use``.
        status = self._gate_status()
        if status is None:
            return self._undeterminable_gate_result(nudged=False)
        if status:
            return TurnResult(
                reply=None,
                tool_activity=[],
                error=(
                    "this session is HELD at the efferent gate — a new message here would be "
                    "read by the runtime as approval and would run the held actions. Call "
                    "resume_turn() to approve them, or reject_turn(reason) to refuse."
                ),
                gated=True,
                pending=self._gate_report(),
            )

        try:
            self.conversation.send_message(message)
        except TurnTimeout as exc:
            # `send_message` is not obviously blocking, but the deadline is armed around the WHOLE
            # turn and a bound that reclassifies itself depending on which statement it lands in
            # would be a bound the operator cannot reason about. Same TYPE-first ordering as
            # `_drive`.
            return TurnResult(
                reply=None, tool_activity=[], error=str(exc), timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn is a RESULT, not a crash
            return TurnResult(reply=None, tool_activity=[], error=str(exc))
        return self._drive()

    def resume_turn(self) -> TurnResult:
        """APPROVE the actions the gate is holding and carry the turn on.

        The human's half of the fan-in. The pending actions execute — the SDK's next ``run()``
        drains them — and the turn then continues normally, so approving is a continuation of
        one turn rather than the start of a new one. That matters for memory: the episode this
        turn eventually captures contains the whole arc including the halt, not a fragment
        either side of it.

        Safe to call when nothing is pending (it is then an ordinary continuation).
        """
        if self._closed:
            return TurnResult(reply=None, tool_activity=[], error=_CLOSED_SESSION_ERROR)
        return self._drive()

    def reject_turn(self, reason: str = "the operator declined this action") -> TurnResult:
        """REFUSE the actions the gate is holding, and let the entity answer for it.

        ``reason`` reaches the model as a rejection observation, so the entity LEARNS the action
        did not happen — the difference between a partner that adapts ("understood, I'll open a
        PR instead of pushing") and one that narrates success over a world it never touched. The
        turn then continues, which is why this returns a :class:`TurnResult` rather than
        ``None``: a refusal is a move in the conversation, not a dead end.
        """
        if self._closed:
            return TurnResult(reply=None, tool_activity=[], error=_CLOSED_SESSION_ERROR)
        try:
            from levain.firing.openhands.gate import reject_pending

            reject_pending(self.conversation, reason)
        except Exception:  # noqa: BLE001 — a rejection must not take down the driver
            pass

        # VERIFY THE REFUSAL LANDED — do not trust that the call succeeded (glm L3, HIGH).
        # `_drive()` calls `run()`, and `run()` on a STILL-HALTED conversation is the SDK's
        # APPROVAL path. So a rejection that silently failed would go on to execute the exact
        # action the operator just refused: the operator says no, and the world changes.
        #
        # The bug was a fail-soft written for AVAILABILITY ("a rejection must not take down the
        # driver") applied to a SECURITY boundary, where the two goals point opposite ways. On a
        # gate, uptime loses. Same discipline as `arm_efferent_gate`: check the STATE, never the
        # call's return — `verify_the_output_not_the_run`.
        if self._gate_halted():
            return TurnResult(
                reply=None,
                tool_activity=[],
                error=(
                    "the refusal did NOT take — the actions are still held and were NOT run. "
                    "Do not resume this session; restart it."
                ),
                gated=True,
                pending=self._gate_report(),
            )
        return self._drive()

    def pending_efferent(self) -> tuple[PendingEfferent, ...]:
        """The actions the gate is currently holding, or ``()`` if it is not holding any.

        Asks AUTHORITY before DESCRIPTION — an un-halted session has nothing pending, and must
        not be handed the "contents unknown" placeholder that a real-but-undescribable halt
        produces. Never raises."""
        if not self._gate_halted():
            return ()
        return self._gate_report()

    def _drive(self) -> TurnResult:
        """Run the conversation forward and report what the harness observed.

        Shared by the first send, an approval and a refusal, so all three agree on the gate
        check, the act-first backstop and — the one that bites — WHEN the turn is captured.
        """
        nudged = False
        try:
            self.conversation.run()

            # The gate check comes FIRST, before the act-first backstop, because a halted turn
            # looks exactly like a stalled one from the outside: no tool ran, no reply arrived.
            # Nudging here would tell an entity that DID act ("I'll run the tests" → proposed
            # bash → held) to stop planning and act, which is both false and useless — the thing
            # standing between it and acting is a human, not its own hesitation.
            status = self._gate_status()
            if status is None:
                return self._undeterminable_gate_result(nudged=nudged)
            if status:
                return self._gated_result(nudged=nudged)

            if self.with_tools and planned_without_acting(self.conversation.state.events):
                nudged = True
                self.conversation.send_message(LEVAIN_ACT_NUDGE)
                self.conversation.run()
                # Same three-valued treatment as the pre-nudge check — the post-nudge run() is
                # a second chance to halt, and an unreadable status here cascades identically.
                status = self._gate_status()
                if status is None:
                    return self._undeterminable_gate_result(nudged=nudged)
                if status:
                    return self._gated_result(nudged=nudged)

            # CAPTURE ONLY A COMPLETED TURN — never a gated halt. This is the SAME defect the
            # nudge ordering above already paid for: `vagus_run` keys idempotency on the user
            # turn id, so capturing at the halt would make the post-approval capture a NO-OP on
            # the unchanged id, and memory would keep the moment the entity was STOPPED while
            # the world got the work it was later allowed to do. Worse than losing the episode:
            # a store that is perfectly grounded in a perfectly recorded misunderstanding.
            #
            # The accepted consequence, stated rather than discovered later: a headless run that
            # halts and is never resumed captures NOTHING for that turn. Correct — the turn did
            # not happen. An episode asserting "I ran X" for an action a human refused would be
            # memory recording a fiction.
            self.binding.capture_turn(self.conversation)
        except TurnTimeout as exc:
            # BEFORE the generic handler, because `TurnTimeout` IS an `Exception` and the broad
            # clause below would otherwise swallow it into an ordinary failed turn — losing the
            # one distinction a supervisor acts on differently (stalled environment, retry on the
            # cadence) and reporting `3` for a hung socket. Classified by TYPE, never by message.
            #
            # Note what is NOT here: no capture. The bound can fire anywhere inside this block,
            # including mid-`capture_turn`, and a turn killed mid-flight has no completed work to
            # record. That the bound covers capture too is deliberate — capture opens the store and
            # can itself block, so exempting it would leave a hole in the very bound being added.
            return TurnResult(
                reply=None, tool_activity=[], error=str(exc), nudged=nudged, timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn is a RESULT, not a crash
            return TurnResult(reply=None, tool_activity=[], error=str(exc), nudged=nudged)

        events = self.conversation.state.events
        return TurnResult(
            reply=latest_agent_text(events),
            tool_activity=turn_tool_activity(events, self.workspace),
            error=None,
            nudged=nudged,
        )

    def _undeterminable_gate_result(self, *, nudged: bool) -> TurnResult:
        """The turn ends as a FAILURE when the gate's own state cannot be read.

        Not captured, not reported as complete, and non-zero on exit. "I could not tell whether
        efferent actions are being held" is a real failure of a governed session — rendering it
        as an ordinary finished turn is the same silent-green move the whole keystone exists to
        refuse, and it is what would let the held actions run on the following turn."""
        return TurnResult(
            reply=None,
            tool_activity=turn_tool_activity(self.conversation.state.events, self.workspace),
            error=(
                "could not determine whether the efferent gate is holding actions — ending the "
                "turn rather than continuing blind. Restart the session; do not resume it."
            ),
            nudged=nudged,
        )

    def _gated_result(self, *, nudged: bool) -> TurnResult:
        """The result for a turn stopped at the gate: activity so far, and what is held.

        ``reply`` is ``None`` by construction — the turn has not reached one. Tool activity IS
        reported, because afferent work earlier in the same turn really did happen and hiding it
        would leave the operator judging a proposed action with no view of what led to it — but
        the HELD actions are SUBTRACTED from it (codex L3).

        That subtraction matters more than it sounds: the runtime emits an ``ActionEvent`` before
        the confirmation decision and then skips execution, so an unfiltered activity list prints
        ``⚙ terminal: git push`` — the standard "here is what it DID" line — on the same screen
        where the gate says nothing was executed. Two true-looking statements contradicting each
        other, and the operator has to guess which one to believe."""
        return TurnResult(
            reply=None,
            tool_activity=self._executed_activity(),
            error=None,
            nudged=nudged,
            gated=True,
            pending=self._gate_report(),
        )

    def _executed_activity(self) -> list[str]:
        """Activity for the actions that actually RAN this turn, with the held ones removed."""
        events = self.conversation.state.events
        try:
            from levain.firing.openhands.gate import held_action_ids

            held = held_action_ids(self.conversation)
            if held:
                events = [e for e in events if str(getattr(e, "id", "")) not in held]
        except Exception:  # noqa: BLE001 — a display filter must never break a turn
            pass
        return turn_tool_activity(events, self.workspace)

    def _gate_status(self) -> bool | None:
        """AUTHORITY: did the runtime stop this turn at the gate? ``None`` = undeterminable.

        Three-valued deliberately (glm L3, LOW). An unreadable status is NOT the same as "not
        halted", and collapsing them defers the gate's own failure into the NEXT turn: the
        driver would capture, report a normal result, and the following ``run()`` — issued on a
        conversation the SDK still has parked in ``WAITING_FOR_CONFIRMATION`` — would execute
        the previous turn's held actions with nobody having approved them. The invariant would
        break one turn later than the HIGH above, by the same mechanism.

        So an undeterminable status ends the turn honestly instead (see :meth:`_drive`), which
        also removes the "next turn" that the cascade needs. Short-circuits to ``False`` when the
        gate was never armed, so an ungated session pays nothing for any of this."""
        if self.gate_mode != "gated":
            return False
        try:
            from levain.firing.openhands.gate import awaiting_confirmation

            # `awaiting_confirmation` is itself three-valued and returns None for an unreadable
            # status — it must NOT collapse that to False, or this whole branch is dead code
            # that merely looks like a safeguard (codex L3, HIGH: it did exactly that).
            return awaiting_confirmation(self.conversation)
        except Exception:  # noqa: BLE001 — undeterminable, NOT "no"
            return None

    def _gate_halted(self) -> bool:
        """``True`` only when the runtime positively reports a halt.

        Kept for the callers that are asking "is something held right now?" rather than driving
        a turn — an undeterminable status is not a halt to report to them."""
        return self._gate_status() is True

    def _gate_report(self) -> tuple[PendingEfferent, ...]:
        """DESCRIPTION: what the gate is holding, for the operator to judge.

        Never gates anything itself — :meth:`_gate_halted` already decided that. A halt this
        cannot describe returns a single loud placeholder rather than an empty list, because
        "held, contents unknown" and "nothing held" look identical to a reader and mean opposite
        things; the operator must never be shown an empty decision."""
        if self.gate_mode != "gated":
            return ()
        try:
            from levain.firing.openhands.gate import pending_gate_report

            report = tuple(pending_gate_report(self.conversation))
        except Exception:  # noqa: BLE001 — a report must never break a turn
            report = ()
        if report:
            return report
        return (
            PendingEfferent(
                tool_name="<unreported>",
                detail="the runtime halted at the gate, but the held action could not be read",
                reason="report unavailable — approve only if you know what this entity was doing",
                recognized=False,
            ),
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
