"""levain.firing.openhands.tools — the CONFINED executor-tool bundle for a sovereign entity.

STEP 6 (spore-277): the isolated ``levain run`` entity moves from conversation to AGENCY — it gets
HANDS. This module builds the FIRST slice of those hands: a file-editor tool structurally FENCED
to the entity's own ``<entity>/workspace/``, so the entity can read/write/search its workspace but
can NEVER reach the operator-laptop flow store or escape its sandbox.

**The confinement is the whole game, and it is OURS to build** (Phill's gate, 2026-07-09). The
shipped OpenHands ``FileEditorTool`` does NOT confine: its ``workspace_root`` is cosmetic (a
path-SUGGESTION hint; ``FileEditor.validate_path`` has no containment check), so a stock editor
would happily ``view ~/.anneal-memory/`` (reading flow's identity into the entity) or
``create /etc/...``. So the fence is ours, and it is STRUCTURAL
(``structural_invariants_beat_discipline``), not a command/path blocklist: the confined executor
resolves every path and refuses anything outside ``<entity>/workspace/`` via the shared
:func:`~levain.firing.isolation.assert_path_within_workspace` guard — the same resolve-then-check
fence that guards the store, so a symlink escape is caught by its resolved target.

**Why file-first, and why bash is NOT here.** A persistent host shell (OpenHands' ``TerminalTool``)
cannot be confined by any in-process check — its ``working_dir`` is only the STARTING dir, and
``cd ~`` / absolute paths escape freely; the only structural fence for a real shell is an OS sandbox
(macOS ``sandbox-exec`` / Linux ``bwrap`` / a container), a platform-specific lift that is NOT
portable across "run it anywhere, any model". File hands are structurally confinable TODAY, purely
in Python, on every platform — the honest thinnest first slice. bash/terminal is its own later
OS-sandbox slice.

**Threat model + the known limits of resolve-then-check (apparatus L2, 2026-07-09).** The fence
confines an ADVERSARIAL LLM emitting arbitrary file-editor tool calls, with no other hands, in a
workspace only it + the operator populate. Verified HELD against every LLM-reachable escape (``..``,
absolute, ``~``, symlink-to-flow-store, ``view``-is-fenced, ``undo_edit``-is-fenced). It CANNOT
close three vectors that resolve-then-check is structurally blind to — but which the file-editor
entity cannot reach (no symlink/hardlink primitive, no concurrency):
  1. a **hardlink** in the workspace pointing at a flow-store file (resolves in-tree → passes → a
     ``view`` leaks the text neocortex);
  2. a **symlinked sub-directory** the SDK's ``view`` walk enumerates 2 levels deep (leaks flow-store
     FILENAMES, not contents);
  3. a **mid-session swap** of a path/workspace component by an external process (TOCTOU).
All three require bash hands OR untrusted workspace PRE-POPULATION (a seed/git/second-entity drops a
link) OR an external actor. They are the reason bash needs the OS-sandbox slice — an in-process fence
is not sufficient once the workspace is writable by anything but this entity. Do NOT paper over them
with LLM-unreachable partial mitigations here; close them at the OS-sandbox layer.

Requires the ``openhands`` extra.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openhands.sdk.tool import Action, DeclaredResources, Tool, register_tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
from openhands.tools.file_editor.impl import FileEditorExecutor

from levain.firing.isolation import IsolationError, assert_path_within_workspace

if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState

__all__ = [
    "LEVAIN_FILE_EDITOR_TOOL",
    "WorkspaceConfinedFileEditorExecutor",
    "LevainFileEditorTool",
    "build_entity_tools",
]

# The REGISTRY key for the confined file editor (the ``Tool(name=...)`` spec name). Deliberately
# DISTINCT from the stock ``"file_editor"`` so an entity's tool set can never resolve to the
# unconfined tool. The RESOLVED tool keeps its own ``.name == "file_editor"`` (the SDK's tools_map
# keys on ``tool.name``, not the spec name — verified sdk/agent/base.py:587), so the LLM still sees
# the FAMILIAR ``file_editor`` function name (better tool-use reliability for weak open models)
# while the registry stays collision-free.
LEVAIN_FILE_EDITOR_TOOL = "levain_file_editor"


class WorkspaceConfinedFileEditorExecutor(FileEditorExecutor):
    """A :class:`~openhands.tools.file_editor.impl.FileEditorExecutor` that FAIL-CLOSES on any path
    outside the entity workspace.

    Before delegating to the shipped executor, it asserts ``action.path`` (resolved) is within
    ``workspace_root`` via :func:`~levain.firing.isolation.assert_path_within_workspace` — on EVERY
    command, ``view`` INCLUDED. (This is exactly why we do NOT reuse the SDK's own
    ``allowed_edits_files`` allowlist, which exempts ``view``: for a sovereign entity a view of the
    flow store is an isolation leak, not just an unsafe write.) A refusal returns an in-band error
    :class:`FileEditorObservation` — the model is told "no" and the turn continues; the filesystem
    is never touched. The guard runs PER OP (the point-of-use invariant), so a symlink swapped into
    the workspace after construction is still caught by its resolved target."""

    def __init__(self, *, workspace_root: str, **kwargs: Any) -> None:
        super().__init__(workspace_root=workspace_root, **kwargs)
        # The fence root (resolved lazily by the guard). ``workspace_root`` is
        # ``conv_state.workspace.working_dir`` == ``<entity>/workspace/``, already asserted in-tree
        # by ``levain.run`` before this conversation was built. Held so every ``__call__`` re-guards.
        self._confine_root = workspace_root

    def __call__(
        self,
        action: "FileEditorAction",
        conversation: Any = None,
    ) -> FileEditorObservation:
        try:
            assert_path_within_workspace(action.path, workspace_root=self._confine_root)
        except IsolationError as exc:
            # Name the concrete workspace root + the absolute-path requirement in the refusal: a weak
            # open model that emitted a RELATIVE path (resolved against cwd → refused before the stock
            # editor's own "use an absolute path" hint fires) can then recover in one turn instead of
            # thrashing (apparatus L1 note #4).
            return FileEditorObservation.from_text(
                text=(
                    f"REFUSED (workspace isolation): {exc} Use an ABSOLUTE path inside your own "
                    f"workspace: {self._confine_root}"
                ),
                command=action.command,
                is_error=True,
            )
        return super().__call__(action, conversation)


class LevainFileEditorTool(FileEditorTool):
    """The confined file editor, registered under :data:`LEVAIN_FILE_EDITOR_TOOL`.

    Reuses the stock tool's rich (vision-aware) description + schema + annotations and swaps in the
    workspace-confined executor — so the model sees the identical, familiar ``file_editor`` contract,
    now structurally fenced. The LLM-visible ``.name`` stays ``"file_editor"`` (familiar → better
    tool-use on weak open models; the SDK keys ``tools_map`` on it) while the REGISTRY key stays
    ``"levain_file_editor"`` (``register_tool`` below), so the unconfined stock tool is never
    reachable from an entity. The explicit ``name`` short-circuits the SDK's ``__init_subclass__``
    auto-derivation (which would otherwise make it ``"levain_file_editor"``).

    It must be a REAL ``LevainFileEditorTool`` instance (not a stock ``FileEditorTool`` with a swapped
    executor) so that OUR :meth:`declared_resources` override is the one the runtime calls — the
    pre-executor surface the confinement must also own (codex L3)."""

    name: ClassVar[str] = "file_editor"

    def declared_resources(self, action: Action) -> DeclaredResources:
        """Own the PRE-EXECUTOR path surface too (codex L3, non-replaceable catch).

        OpenHands' ``ParallelToolExecutor`` calls ``declared_resources()`` BEFORE the executor to
        compute file locks, and the STOCK version does ``Path(action.path).resolve()`` — which RAISES
        on a malformed path (an LLM-emittable embedded NUL) inside the executor's ``try``, surfacing a
        raw ``AgentErrorEvent`` and SKIPPING the executor entirely, so the confined executor's clean
        in-band refusal never fires. That is a ``claim > enforcement`` gap: the fence must own EVERY
        surface that parses/resolves the path, not just ``__call__``. So run the fence here; on ANY
        rejection or malformed path, declare NO lock (``declared=True``, empty keys) — which the
        runtime treats as "safe, no resources" and STILL RUNS the executor
        (``parallel_executor.py:255``), which then returns the real refusal Observation. Never raises."""
        assert isinstance(action, FileEditorAction)
        try:
            assert_path_within_workspace(action.path, workspace_root=self._confine_root())
            resolved = Path(action.path).expanduser().resolve()
        except (IsolationError, ValueError, OSError):
            return DeclaredResources(keys=(), declared=True)
        return DeclaredResources(keys=(f"file:{resolved}",), declared=True)

    def _confine_root(self) -> str:
        """The confined executor's fence root. ``create`` always wires our executor, so this holds."""
        assert isinstance(self.executor, WorkspaceConfinedFileEditorExecutor)
        return self.executor._confine_root

    @classmethod
    def create(cls, conv_state: "ConversationState") -> list["LevainFileEditorTool"]:  # type: ignore[override]
        confined = WorkspaceConfinedFileEditorExecutor(
            workspace_root=conv_state.workspace.working_dir
        )
        # Build REAL LevainFileEditorTool instances (not stock via set_executor, which keeps the stock
        # class + its raising declared_resources), reusing the stock tool's rich description/schema/
        # annotations by copying its fields — so our declared_resources override is what runs.
        return [
            cls(
                description=stock.description,
                action_type=stock.action_type,
                observation_type=stock.observation_type,
                annotations=stock.annotations,
                executor=confined,
            )
            for stock in FileEditorTool.create(conv_state)
        ]


# Register at import so ``Tool(name="levain_file_editor")`` resolves. Importing this module also
# imports the stock file_editor definition (which self-registers ``"file_editor"``) — harmless: an
# entity only ever references the levain name. A duplicate re-import just warns (registry is
# last-write-wins with the same resolver), never raises.
register_tool(LEVAIN_FILE_EDITOR_TOOL, LevainFileEditorTool)


def build_entity_tools() -> list[Tool]:
    """The confined executor-tool bundle for a ``levain run`` entity — SLICE 1: the fenced file
    editor. Returns ``Tool`` SPECS (resolved to the confined executor at conversation-build time via
    the registry). This is the ONLY blessed file-tool builder Levain ships, and it is confined by
    construction. bash/terminal is intentionally absent (its own OS-sandbox slice)."""
    return [Tool(name=LEVAIN_FILE_EDITOR_TOOL)]
