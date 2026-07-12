"""levain.firing.openhands.tools — the CONFINED executor-tool bundle for a sovereign entity.

The isolated ``levain run`` entity moves from conversation to AGENCY — it gets HANDS. This module
builds them, and it builds them so the (less-trusted, open-model) entity can work like Claude Code /
Codex on the operator's REAL repos while the sovereignty CROWN JEWELS stay structurally off-limits no
matter what the model is told to do:

  - :class:`LevainBashTool` — a persistent, stateful bash confined by an OS sandbox (spore-311's
    :class:`~levain.firing.confinement.SandboxedShell`), NOT the SDK's un-confinable host
    ``TerminalExecutor``. This is the CC/Codex-replacement hand: `cd` anywhere, run builds, SSH out,
    hit the network — everything you use bash for — with the crown jewels fenced at the syscall level.
  - :class:`LevainFileEditorTool` — the file-editor hand, RELAXED (slice 2) from the step-6
    ``<entity>/workspace/`` jail to the SAME crown-jewels floor as bash. A networked bash that goes
    anywhere paired with a file editor still jailed to a workspace is incoherent; both hands relax
    together, protected by the floor rather than a jail.

**One floor, two enforcers (``structural_invariants_beat_discipline``).** A
:class:`~levain.firing.confinement.CrownJewelsPolicy` — built by EACH hand's ``create`` from the SAME
inputs (the entity dir + ``<entity>/.levain/confinement.json``) via :func:`policy_for_conv_state`, so
the two are equivalent — fences BOTH hands. bash rides the rendered ``sandbox-exec``
profile (the OS fences it — a persistent shell whose cwd wanders can't be confined in-process, which
is the whole reason bash needed an OS sandbox). The file editor is ordinary in-process Python, NOT
under the sandbox, so it calls the IN-PROCESS twin :func:`~levain.firing.confinement.crown_jewel_reason`
on every path — the same denylist, so there is no ``claim > enforcement`` gap between the two hands.

**The crown-jewels floor (structural, never).** ``~/.anneal-memory/`` (flow's memory — the identity
moat in file terms), sibling entities' ``.levain/`` stores, ``~/.ssh`` key material (``ssh_mode=
"agent"`` — the entity authenticates via the agent socket but can't read/exfil raw keys),
``~/.ssh/authorized_keys`` WRITE (both ssh_modes — no key can be planted as a persistence backdoor),
plus the operator-declared credential files/subtrees from ``confinement.json`` and — when the operator
opts in (``deny_standard_creds``) — the standard cred stores (``~/.config/gh`` / ``~/.aws/credentials``
/ ``~/.netrc``). The entity's OWN ``<entity>/.levain/`` store is deliberately NOT a jewel (its memory
is its own); the firing's ``assert_entity_isolated`` moat, not these tools, keeps recall/capture off
flow's store.

**Gating (v1 REALITY, load-bearing honesty).** The floor protects the crown jewels and NOTHING else.
With default-allow, no threshold membrane (a SPEC — spore-295), and no permission prompts (Phill:
people bypass those IRL), a confabulating open model can still ``rm -rf`` a real repo, ``git push
--force``, or ``curl | bash`` — none of which the floor stops. So v1 = structural floor +
you-in-the-loop (YOLO-mode CC, crown jewels structurally protected). The membrane is the precondition
for UNATTENDED operation, sequenced after — NOT a v1 claim. The full honest limits (Apple-deprecated
``sandbox-exec``, pre-populated hardlinks, resource exhaustion, non-crown-jewel network exfil, IPC
side channels) live on :mod:`levain.firing.confinement`.

Requires the ``openhands`` extra.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Tool,
    ToolExecutor,
    register_tool,
)
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
from openhands.tools.file_editor.impl import FileEditorExecutor
from openhands.tools.terminal.definition import (
    TerminalAction,
    TerminalObservation,
    TerminalTool,
)
from openhands.tools.terminal.metadata import CmdOutputMetadata

from levain.firing.confinement import (
    ConfinementError,
    CrownJewelsPolicy,
    SandboxedShell,
    build_policy,
    crown_jewel_reason,
    load_confinement_config,
    select_provider,
)
from levain.firing.isolation import LEVAIN_ENTITY_DIR_ENV

if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState

__all__ = [
    "LEVAIN_FILE_EDITOR_TOOL",
    "LEVAIN_BASH_TOOL",
    "CrownJewelsFileEditorExecutor",
    "LevainFileEditorTool",
    "SandboxedBashExecutor",
    "LevainBashTool",
    "policy_for_conv_state",
    "build_entity_tools",
]

# The REGISTRY keys (the ``Tool(name=...)`` spec names). Deliberately DISTINCT from the stock
# ``"file_editor"`` / ``"terminal"`` so an entity's tool set can never resolve to an UNCONFINED stock
# tool. Each resolved tool keeps its own ``.name`` == the stock name (the SDK's tools_map keys on
# ``tool.name``, not the spec name), so the LLM still sees the FAMILIAR function name (better tool-use
# reliability for weak open models) while the registry stays collision-free.
LEVAIN_FILE_EDITOR_TOOL = "levain_file_editor"
LEVAIN_BASH_TOOL = "levain_bash"


# --- the shared crown-jewels floor for a run -------------------------------------------------


def policy_for_conv_state(conv_state: "ConversationState") -> CrownJewelsPolicy:
    """Build the ONE :class:`~levain.firing.confinement.CrownJewelsPolicy` that fences BOTH hands for
    this run — the shared floor.

    The entity dir is the authoritative ``$LEVAIN_ENTITY_DIR`` (bound by
    :func:`~levain.firing.isolation.bind_entity` BEFORE the conversation — and its tools — are built,
    fork-safe), falling back to ``<workspace>/..`` (``levain run`` always creates the workspace as
    ``<entity>/workspace/``) when the env is unset (e.g. a direct unit test). The universal floor
    (flow store + sibling stores + ssh key material) is always applied by ``build_policy``; the
    operator's app-specific credential files/subtrees come from ``<entity>/.levain/confinement.json``
    (fail-closed if that file is present but malformed — a broken crown-jewels declaration must not
    silently yield a floor with holes)."""
    workspace = Path(conv_state.workspace.working_dir).expanduser().resolve()
    env = os.environ.get(LEVAIN_ENTITY_DIR_ENV, "").strip()
    entity_dir = Path(env).expanduser().resolve() if env else workspace.parent
    cfg = load_confinement_config(entity_dir)
    return build_policy(
        entity_dir,
        workspace=workspace,
        ssh_mode=cfg.ssh_mode,
        deny_files=cfg.deny_files,
        extra_deny_read_write=cfg.deny_subtrees,
        deny_standard_creds=cfg.deny_standard_creds,
    )


# --- the file-editor hand (relaxed to the crown-jewels floor) --------------------------------


class CrownJewelsFileEditorExecutor(FileEditorExecutor):
    """A :class:`~openhands.tools.file_editor.impl.FileEditorExecutor` fenced to the crown-jewels
    FLOOR (slice 2), relaxing the step-6 ``<entity>/workspace/`` jail.

    Before delegating to the shipped executor it asks
    :func:`~levain.firing.confinement.crown_jewel_reason` whether ``action.path`` is a crown jewel — on
    EVERY command, ``view`` INCLUDED (a view of the flow store is an isolation LEAK, not just an unsafe
    write; this is exactly why we do NOT reuse the SDK's ``allowed_edits_files`` allowlist, which
    exempts ``view``). A refusal returns an in-band error :class:`FileEditorObservation` — the model is
    told "no" and the turn continues; the filesystem is never touched. Anything NOT a crown jewel is
    allowed (broad reach, like bash) — the incoherent asymmetry of a networked bash + a workspace-jailed
    editor is gone.

    The guard runs PER OP (the point-of-use invariant), so a symlink swapped into the tree after
    construction is caught by its resolved target. The stock editor REQUIRES an absolute path
    (``FileEditor.validate_path`` rejects a relative path before operating), so the path
    ``crown_jewel_reason`` resolves is the exact path that would be touched — no cwd-base mismatch.
    ``workspace_root`` is passed to the stock editor only as its relative-path SUGGESTION base (it was
    never a real jail — its containment is cosmetic), NOT as a confinement; the floor is the
    confinement."""

    def __init__(self, *, policy: CrownJewelsPolicy, **kwargs: Any) -> None:
        super().__init__(workspace_root=str(policy.workspace), **kwargs)
        self._policy = policy

    def __call__(
        self,
        action: "FileEditorAction",
        conversation: Any = None,
    ) -> FileEditorObservation:
        reason = crown_jewel_reason(self._policy, action.path)
        if reason is not None:
            return FileEditorObservation.from_text(
                text=(
                    f"REFUSED (crown-jewels floor): {reason}. Your hands reach the rest of the "
                    "filesystem, but the sovereignty crown jewels are structurally off-limits."
                ),
                command=action.command,
                is_error=True,
            )
        return super().__call__(action, conversation)


class LevainFileEditorTool(FileEditorTool):
    """The floored file editor, registered under :data:`LEVAIN_FILE_EDITOR_TOOL`.

    Reuses the stock tool's rich (vision-aware) description + schema + annotations and swaps in the
    crown-jewels-floored executor — so the model sees the identical, familiar ``file_editor`` contract,
    now fenced to the floor. The LLM-visible ``.name`` stays ``"file_editor"`` (familiar → better
    tool-use on weak open models; the SDK keys ``tools_map`` on it) while the REGISTRY key stays
    ``"levain_file_editor"`` (``register_tool`` below), so the unconfined stock tool is never reachable
    from an entity. The explicit ``name`` short-circuits the SDK's ``__init_subclass__`` auto-derivation.

    It must be a REAL ``LevainFileEditorTool`` instance (not a stock ``FileEditorTool`` with a swapped
    executor) so that OUR :meth:`declared_resources` override is the one the runtime calls — the
    pre-executor surface the confinement must also own (codex L3)."""

    name: ClassVar[str] = "file_editor"

    def declared_resources(self, action: Action) -> DeclaredResources:
        """Own the PRE-EXECUTOR path surface too (codex L3, non-replaceable catch).

        OpenHands' ``ParallelToolExecutor`` calls ``declared_resources()`` BEFORE the executor to
        compute file locks, and the STOCK version does ``Path(action.path).resolve()`` — which RAISES
        on a malformed path (an LLM-emittable embedded NUL) inside the executor's ``try``, surfacing a
        raw ``AgentErrorEvent`` and SKIPPING the executor entirely, so the floored executor's clean
        in-band refusal never fires. So run the fence here too; on a crown-jewel path OR a malformed
        path, declare NO lock (``declared=True``, empty keys) — the runtime treats that as "safe, no
        resources" and STILL RUNS the executor, which then returns the real refusal Observation. Never
        raises."""
        assert isinstance(action, FileEditorAction)
        try:
            resolved = Path(action.path).expanduser().resolve()
        except (ValueError, OSError):
            return DeclaredResources(keys=(), declared=True)
        if crown_jewel_reason(self._policy(), resolved) is not None:
            return DeclaredResources(keys=(), declared=True)
        return DeclaredResources(keys=(f"file:{resolved}",), declared=True)

    def _policy(self) -> CrownJewelsPolicy:
        """The floored executor's crown-jewels policy. ``create`` always wires our executor, so this
        holds."""
        assert isinstance(self.executor, CrownJewelsFileEditorExecutor)
        return self.executor._policy

    @classmethod
    def create(cls, conv_state: "ConversationState") -> list["LevainFileEditorTool"]:  # type: ignore[override]
        floored = CrownJewelsFileEditorExecutor(policy=policy_for_conv_state(conv_state))
        # Build REAL LevainFileEditorTool instances (not stock via set_executor, which keeps the stock
        # class + its raising declared_resources), reusing the stock tool's rich description/schema/
        # annotations by copying its fields — so our declared_resources override is what runs.
        return [
            cls(
                description=stock.description,
                action_type=stock.action_type,
                observation_type=stock.observation_type,
                annotations=stock.annotations,
                executor=floored,
            )
            for stock in FileEditorTool.create(conv_state)
        ]


# --- the bash hand (a persistent OS-sandboxed shell) -----------------------------------------


class SandboxedBashExecutor(ToolExecutor[TerminalAction, TerminalObservation]):
    """Drives spore-311's :class:`~levain.firing.confinement.SandboxedShell` (a persistent
    ``sandbox-exec``-confined bash) instead of the SDK's un-confinable host ``TerminalExecutor``.

    The shell is spawned LAZILY on the first command — a ``--no-tools`` or never-touch-bash session
    pays nothing, and a spawn failure (no OS sandbox on this platform) becomes a clean in-band refusal,
    never a conversation-build crash (fail-closed: no floor → no unconfined shell). The
    ``SandboxedShell`` is SINGLE-CALLER; :meth:`LevainBashTool.declared_resources` serializes bash calls
    against each other so two never race the one shell. ``reset`` closes + respawns a fresh shell;
    ``exit`` inside a command closes it and the next command respawns; ``is_input`` (interactive stdin)
    is refused — the confined shell is a non-interactive dev shell + agent-auth SSH, no PTY."""

    def __init__(
        self,
        policy: CrownJewelsPolicy,
        *,
        default_timeout: float = 120.0,
    ) -> None:
        self._policy = policy
        self._default_timeout = default_timeout
        self._shell: SandboxedShell | None = None
        # Guards the lazy spawn / teardown against interrupt()/close() from another thread. Bash calls
        # themselves are serialized by declared_resources, so this is only the cross-thread guard.
        self._lock = threading.Lock()

    def _ensure_shell(self) -> SandboxedShell:
        """The live shell — spawning a fresh one on first use OR after the previous one exited
        (``exit``/reset). Raises :class:`ConfinementError` (caught by :meth:`__call__` → in-band
        refusal) if no OS confinement floor can be established here."""
        with self._lock:
            if self._shell is None or self._shell.closed:
                provider = select_provider()  # raises ConfinementError off a supported platform
                self._shell = provider.spawn_shell(
                    self._policy, default_timeout=self._default_timeout
                )
            return self._shell

    def _teardown(self) -> None:
        with self._lock:
            shell, self._shell = self._shell, None
        if shell is not None:
            shell.close()

    def __call__(
        self,
        action: TerminalAction,
        conversation: Any = None,
    ) -> TerminalObservation:
        if action.reset and action.is_input:
            # Mirror the stock TerminalExecutor's contract for this invalid combination.
            raise ValueError("Cannot use reset=True with is_input=True")
        if action.is_input:
            return self._error(
                action,
                "This confined shell does not support interactive input (is_input=True): it runs "
                "non-interactive dev commands + agent-auth SSH, with no PTY. Run the program "
                "non-interactively instead (flags/env, a heredoc, or `yes |`).",
            )
        if action.reset:
            self._teardown()
            if not action.command.strip():
                return TerminalObservation.from_text(
                    text=(
                        "Sandboxed shell reset — a fresh confined bash will start on the next "
                        "command; env, cwd, and shell state were cleared."
                    ),
                    command="[RESET]",
                    exit_code=0,
                    metadata=CmdOutputMetadata(exit_code=0),
                )

        try:
            shell = self._ensure_shell()
        except ConfinementError as exc:
            return self._error(
                action,
                f"could not establish the OS confinement floor ({exc}). Refusing to run bash "
                "without the sandbox (fail-closed).",
            )
        try:
            result = shell.run(action.command, timeout=action.timeout)
        except ConfinementError as exc:
            return self._error(action, f"confined shell error: {exc}")

        if result.timed_out:
            limit = action.timeout if action.timeout is not None else self._default_timeout
            text = (
                f"{result.output}\n[command timed out after {limit:.0f}s and is STILL RUNNING; "
                "send reset=True to recover the shell if it stays wedged]"
            )
            return TerminalObservation.from_text(
                text=text,
                command=action.command,
                exit_code=-1,
                timeout=True,
                metadata=CmdOutputMetadata(exit_code=-1),
                is_error=True,
            )

        code = result.exit_code
        if code is None:
            # The command ended the shell (an ``exit``): bash reached EOF BEFORE the sentinel printf, so
            # the exit STATUS is unrecoverable (the sentinel that carries ``$?`` never ran). Surface
            # that the shell exited + is_error so a silent empty result isn't misread as success (a
            # non-zero ``exit N`` would otherwise vanish — apparatus L1); the next command transparently
            # respawns a fresh confined shell.
            return TerminalObservation.from_text(
                text=(
                    f"{result.output}\n[the shell exited (a command ran `exit`); its exit status is "
                    "unrecoverable — a fresh confined shell starts on the next command]"
                ),
                command=action.command,
                exit_code=None,
                metadata=CmdOutputMetadata(exit_code=-1),
                is_error=True,
            )
        return TerminalObservation.from_text(
            text=result.output,
            command=action.command,
            exit_code=code,
            metadata=CmdOutputMetadata(exit_code=code),
            is_error=code != 0,
        )

    # A refusal is NOT a timeout: use 126 ("command cannot execute" convention), not -1 — the SDK's
    # Rich visualizer renders -1 as "Process still running (soft timeout)", the wrong label for a
    # fail-closed refusal (apparatus L1). is_error=True + the REFUSED text carry the real meaning.
    _REFUSAL_EXIT_CODE = 126

    @classmethod
    def _error(cls, action: TerminalAction, text: str) -> TerminalObservation:
        return TerminalObservation.from_text(
            text=f"REFUSED: {text}",
            command=action.command,
            exit_code=cls._REFUSAL_EXIT_CODE,
            metadata=CmdOutputMetadata(exit_code=cls._REFUSAL_EXIT_CODE),
            is_error=True,
        )

    def interrupt(self) -> None:
        """Best-effort Ctrl-C the running command (called from another thread on a conversation
        interrupt). Never raises."""
        shell = self._shell
        if shell is not None:
            shell.interrupt()

    def close(self) -> None:
        """Reap the sandboxed bash + its whole process group (the SDK calls this on conversation
        teardown — 'always close tool executors, they hold runtime resources'). Idempotent."""
        self._teardown()


class LevainBashTool(TerminalTool):
    """The confined bash tool, registered under :data:`LEVAIN_BASH_TOOL`.

    Reuses the stock terminal tool's schema (``TerminalAction``/``TerminalObservation``) + platform
    description + annotations and swaps in the :class:`SandboxedBashExecutor` — so the model sees the
    familiar ``terminal`` contract, now riding an OS sandbox instead of the un-confinable host shell.
    The LLM-visible ``.name`` stays ``"terminal"`` (the SDK keys ``tools_map`` on it) while the REGISTRY
    key stays ``"levain_bash"``, so the unconfined stock terminal is never reachable from an entity."""

    name: ClassVar[str] = "terminal"

    def declared_resources(self, action: Action) -> DeclaredResources:  # noqa: ARG002
        """Serialize bash calls against each other — the :class:`~levain.firing.confinement.SandboxedShell`
        is SINGLE-CALLER (its ``run()`` fails fast on a concurrent call), so two bash tool-calls must
        never run at once. Declare the shared session key UNCONDITIONALLY — unlike the stock
        ``TerminalTool`` (which opts OUT of serialization under a tmux pane pool), our confined shell has
        no pool. Own this pre-executor surface explicitly (the codex L3 lesson: the confinement owns
        EVERY surface the runtime consults, not just ``__call__``)."""
        return DeclaredResources(keys=("terminal:session",), declared=True)

    @classmethod
    def create(cls, conv_state: "ConversationState") -> list["LevainBashTool"]:  # type: ignore[override]
        executor = SandboxedBashExecutor(policy_for_conv_state(conv_state))
        # Pass our executor to the stock create so it does NOT build a host TerminalExecutor; then copy
        # its (platform-correct) description/schema/annotations into a REAL LevainBashTool so OUR
        # declared_resources override runs.
        return [
            cls(
                action_type=stock.action_type,
                observation_type=stock.observation_type,
                description=stock.description,
                annotations=stock.annotations,
                executor=executor,
            )
            for stock in TerminalTool.create(conv_state, executor=executor)
        ]


# Register at import so ``Tool(name="levain_file_editor")`` / ``Tool(name="levain_bash")`` resolve.
# Importing this module also imports the stock definitions (which self-register ``"file_editor"`` /
# ``"terminal"``) — harmless: an entity only ever references the levain names. A duplicate re-import
# just warns (registry is last-write-wins with the same resolver), never raises.
register_tool(LEVAIN_FILE_EDITOR_TOOL, LevainFileEditorTool)
register_tool(LEVAIN_BASH_TOOL, LevainBashTool)


def build_entity_tools(*, with_bash: bool = True) -> list[Tool]:
    """The confined executor-tool bundle for a ``levain run`` entity: the crown-jewels-floored file
    editor + (``with_bash``) the OS-sandboxed bash. Returns ``Tool`` SPECS (resolved to the confined
    executors at conversation-build time via the registry, where the shared floor is built).

    These are the ONLY blessed executor-tool builders Levain ships, and both are confined by
    construction. ``with_bash=False`` drops bash — the caller (``levain run``) passes it when the
    platform has no OS confinement floor (:func:`~levain.firing.confinement.confinement_supported`),
    so the entity keeps its file-editor hand rather than getting a bash whose first command would
    fail-closed. NEVER grants an unconfined shell as a fallback."""
    tools: list[Tool] = [Tool(name=LEVAIN_FILE_EDITOR_TOOL)]
    if with_bash:
        tools.append(Tool(name=LEVAIN_BASH_TOOL))
    return tools
