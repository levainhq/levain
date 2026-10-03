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
:class:`~levain.firing.confinement.CrownJewelsPolicy` — resolved ONCE per conversation into its
:class:`~levain.firing.binding.ConversationBinding` and handed to both hands as one
:class:`_SharedFloor` by :class:`LevainHands` (spore-438) — fences BOTH hands. bash rides the rendered platform sandbox — macOS
``sandbox-exec`` or, since K4c, a Linux ``bwrap`` mount namespace (the OS fences it — a persistent
shell whose cwd wanders can't be confined in-process, which is the whole reason bash needed an OS
sandbox). The POLICY is identical on both; only the enforcement model differs. The file editor is ordinary in-process Python, NOT
under the sandbox, so it calls the IN-PROCESS twin :func:`~levain.firing.confinement.crown_jewel_reason`
on every path — the same denylist, so there is no ``claim > enforcement`` gap between the two hands.

**The crown-jewels floor (structural, never).** ``~/.anneal-memory/`` (flow's memory — the identity
moat in file terms), sibling entities' ``.levain/`` stores, ``~/.ssh`` key material (``ssh_mode=
"agent"`` — the entity authenticates via the agent socket but can't read/exfil raw keys),
``~/.ssh/authorized_keys`` WRITE (both ssh_modes — no key can be planted as a persistence backdoor),
plus the operator-declared credential files/subtrees from ``confinement.json`` and — when the operator
opts in (``deny_standard_creds``) — the standard cred stores (``~/.config/gh`` / ``~/.aws/credentials``
/ ``~/.netrc``). The entity's OWN ``<entity>/.levain/`` is its working space, NOT a jewel — readable,
and its non-store files are writable — EXCEPT its own memory STORE files (``memory.{continuity.md,
crystal.json,db}`` + the SQLite sidecars), which are WRITE-denied to the hands (``own_memory_files``,
spore-359): spore-359 folds the neocortex into the always-loaded frame, so only the host-process wrap
may compose it, never the hands. The firing's ``assert_entity_isolated`` moat, not these tools, keeps
recall/capture off flow's store.

**Gating (v1 REALITY, load-bearing honesty — REWRITTEN 2026-07-29 for the post-K3 world).** The
floor protects the crown jewels and NOTHING else. With default-allow and no permission prompts
(Phill: people bypass those IRL), a confabulating open model can still ``rm -rf`` a real repo,
``git push --force``, or ``curl | bash`` — **none of which THE FLOOR stops**, which is a true
statement about the floor and never was one about the whole system.

The rest is covered by the **efferent gate** (:mod:`levain.firing.gate`, shipped K3), which halts
every efferent action — bash is ALWAYS efferent — when no human is present: the REPL
(``human_present=True``) resolves UNGATED and the operator watching activity IS the fan-in, while
``--task`` and scheduled seats resolve GATED and exit ``EXIT_GATED`` (4) with nothing executed.
``efferent_gate: "ungated"`` disarms it in both cases, so a surface claiming an entity is governed
must RESOLVE that setting rather than assume it. **UNATTENDED OPERATION IS NOW A v1 CLAIM** (K4a,
``levain daemon install-seat``); what is still absent is the per-domain threshold POLICY
(``spore-417``) — nothing graduates, everything efferent gates. The full honest limits live on
:mod:`levain.firing.confinement`, and they are NOT identical across platforms: the shared ones are
pre-populated hardlinks, resource exhaustion, non-crown-jewel network exfil and IPC side channels,
while "Apple-deprecated ``sandbox-exec``" is macOS-only and Linux carries its own — chiefly that a
denied read reports ENOENT rather than EPERM, and that a missing write-denied file's mountpoint is
created on the host.

Requires the ``openhands`` extra.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    Tool,
    ToolDefinition,
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
    crown_jewel_reason,
    refresh_socket_denies,
    select_provider,
)
from levain.firing.binding import ConversationBinding

_log = logging.getLogger("levain.firing.tools")  # module convention: see levain/wrap.py, jobs.py

if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState

__all__ = [
    "LEVAIN_HANDS_TOOL",
    "CrownJewelsFileEditorExecutor",
    "LevainFileEditorTool",
    "SandboxedBashExecutor",
    "LevainBashTool",
    "LevainHands",
    "build_entity_tools",
]

# The REGISTRY key (the ``Tool(name=...)`` spec name) for BOTH hands. Deliberately DISTINCT from the
# stock ``"file_editor"`` / ``"terminal"`` so an entity's tool set can never resolve to an UNCONFINED
# stock tool. Each resolved tool keeps its own ``.name`` == the stock name (the SDK's tools_map keys
# on ``tool.name``, not the spec name), so the LLM still sees the FAMILIAR function names (better
# tool-use reliability for weak open models) while the registry stays collision-free.
LEVAIN_HANDS_TOOL = "levain_hands"


def _log_never_raises(msg: str) -> None:
    """``_log.exception(msg)`` that cannot propagate — for use inside teardown paths.

    ⛔ A LOGGING CALL IS NOT A SAFE STATEMENT. `logging.Handler.emit` implementations are expected
    to route their own failures through `handleError` (which prints and swallows), but that is a
    convention of the stdlib handlers, not a guarantee of the interface: a custom handler whose
    `emit` raises directly propagates out of `_log.exception`. Anywhere a log call sits between a
    failure and the cleanup for that failure, it is a second failure point that can strand the
    cleanup — which is precisely how `_close_candidate_shell` could leak a rejected shell while
    reporting that it was falling back.
    """
    try:
        _log.exception(msg)
    except BaseException:  # noqa: BLE001 — a teardown path may never be replaced by its own logging
        pass


def _close_candidate_shell(candidate: SandboxedShell) -> None:
    """Tear down a rejected shell without ever letting the teardown replace the refusal.

    ⛔ codex L3 MED, 2026-09-04. Two defects in the previous one-liner: a raising `close()` MASKED
    the original `ConfinementError` (a caller matching on type saw "close failed" rather than "no
    effective policy"), and — worse — an OVERRIDDEN `close()` that raises before doing any cleanup
    left the subprocess, FIFO and descriptors alive, held by the shell's own reader thread, after
    the only application reference was dropped. Repeated refusals could then exhaust resources.
    ▶ So: try the object's own `close()`, and if that fails fall back to the base-class teardown.
    Nothing here is allowed to propagate.

    ⚠ **AND THE FALLBACK'S GUARANTEE IS NARROWER THAN AN EARLIER VERSION OF THIS DOCSTRING CLAIMED**
    (glm-5.2 L3 LOW, 2026-09-04). It said the base path is one "a subclass cannot have replaced".
    Not exactly: `SandboxedShell.close` dispatches to `self._signal_group()`, an ordinary overridable
    method, so a subclass that overrode BOTH could still defeat the fallback. What is actually
    guaranteed is that the base `close` BODY runs rather than the override's — which is what matters
    for the case this exists for, an override that raises before doing any teardown.
    ⚠ It also cannot run a subclass's ADDITIVE cleanup, because it does not know about it. That is
    the subclass's problem to solve locally, and `_SeatbeltShell.close` now unlinks its profile in a
    `finally` for exactly this reason."""
    # ⛔ THE BASE TEARDOWN RUNS UNCONDITIONALLY — codex L3 round 9, 2026-09-05. This used to
    # `return` when the override's `close()` did not RAISE, so an override that silently NO-OPS
    # (returns cleanly having torn down nothing) skipped the base path entirely and leaked the
    # subprocess, its process group, the FIFO dir and the reader thread on every rejected spawn.
    # The fallback covered overrides that raise and not overrides that lie, and the docstring
    # above already conceded that narrowness rather than fixing it.
    # ⚠ SAFE BECAUSE IT IS VERIFIED, NOT BECAUSE IT IS ASSERTED: read `SandboxedShell.close` in
    # confinement.py — it is "Idempotent, never raises", sets `_closed` first and nulls every
    # resource behind a None-guard, so the second call is a no-op when the override did its job.
    # ⛔ CITE THE SYMBOL, NOT THE LINE (Diogenes LOW, 2026-09-06). This used to name a line in
    # confinement.py, and it was EXACT when written — the next commit, six minutes later, inserted
    # into `crown_jewel_reason` above it, and the cited line became a blank one while the real
    # definition moved down. A symbol survives an insertion above it; a line number is invalidated
    # by any edit anywhere earlier in the file.
    # ⛔ AND THE FIRST VERSION OF THIS PARAGRAPH RESTATED THE TWO COORDINATES IT WAS RETIRING
    # (Diogenes LOW, 2026-09-07) — "…:1694 became a blank line while the real definition moved to
    # :1703". ⚡ THE SECOND NUMBER WAS NEVER EXACT AT ALL, AND GIT SAYS SO (L1, 2026-09-07).
    # Tracing `SandboxedShell.close` in confinement.py across the commits that moved it:
    # `bdb4b2f` had it at 1694 and wrote ":1694" — correct · `23fedaf`, six minutes later,
    # moved it to 1703 and killed that citation · `fd7c4d4` moved it to 1717 AND IS THE COMMIT
    # THAT WROTE ":1703" — `git log -S"1703" -- <this file>` finds it.
    # ⛔ So the replacement coordinate was FOURTEEN LINES WRONG AT THE INSTANT IT WAS COMMITTED:
    # the same edit invalidated the number and wrote the sentence about the number. That is
    # strictly sharper than the "went stale later" story it replaced, and the earlier claim that
    # "both were exact when written" was false for one of the two.
    # ▶ The numbers survive here only as the QUOTED dead claim, deliberately not refreshed, and
    # the distance is stated in words for the same reason: a fresh figure inside the paragraph
    # retiring a figure is how this defect keeps regenerating.
    # ▶ The coordinates are GONE rather than corrected: updating them to today's numbers only
    # reproduces the defect on a two-week timer, and the narrative carries its whole argument
    # without them.
    #
    # ⛔⛔ THE BASE TEARDOWN IS IN A `finally` — codex L3 round 10, 2026-09-06, and it is THIS
    # RELEASE'S OWN CLASS ARRIVING ONE LAYER UP. Round 9 removed a `return` so the base path could
    # not be skipped by an override that lies. But the base path was still reached only by falling
    # off the end of the `except` below, and `_log.exception` IS NOT GUARANTEED NOT TO RAISE: a
    # custom handler whose `emit` raises without routing through `handleError` propagates straight
    # out of the logging call. A logging failure would then skip the teardown and leak the shell —
    # exactly the defect round 9 closed, reintroduced through the line that REPORTS it.
    # ▶ So the guarantee is now STRUCTURAL rather than resting on the logging call's good
    # behaviour: `finally` runs whatever happens above it, and `_log_never_raises` cannot
    # propagate. Either alone would do; together the invariant does not depend on the helper
    # being correct, which is the point.
    try:
        try:
            candidate.close()
        except BaseException:
            _log_never_raises("a rejected shell's close() raised; falling back to the base teardown")
    finally:
        try:
            SandboxedShell.close(candidate)     # non-overridable path; idempotent
        except BaseException:
            _log_never_raises("base teardown of a rejected shell also failed; it may leak")

class _SharedFloor:
    """The ONE evolving :class:`CrownJewelsPolicy` for ONE CONVERSATION, read by BOTH hands.

    ⛔ WHY IT EXISTS — glm-5.2 L3, 2026-09-04. Each hand's ``create`` then built its own policy
    separately, producing two EQUAL BUT DISTINCT policy objects. That
    was harmless while the spawn-time socket refresh touched only ``deny_sockets`` (the connect arm
    has no in-process twin), and stopped being harmless once the refresh also updated
    ``deny_write_files`` / ``socket_spellings`` / ``deny_write_dirs``, which the file editor DOES
    enforce. The bash hand then evolved a floor the file-editor hand never saw.

    ⛔⛔ **MUTATE THROUGH :meth:`absorb`, NEVER BY ASSIGNING ``.policy`` — codex L3 MED, 2026-09-04,
    EXECUTION-REPRODUCED.** The previous version was assigned wholesale from the spawning executor's
    snapshot under that executor's OWN lock. A per-instance lock does not serialize anything when the
    object being mutated is SHARED: two executors could each read policy ``P``, spawn against ``P+A``
    and ``P+B``, and the second assignment would discard the first — *"a previously denied live
    container socket becomes reachable again"* after the losing executor respawned.
    ⚡ Two independent defects in one line, and both are now structural rather than disciplinary: the
    lock lives with the DATA it protects instead of with one of its writers, and the write is a UNION
    MERGE instead of a replace, so a lost update is impossible rather than merely unlikely. Even with
    per-conversation scoping (which leaves one bash mutator) this is kept — a floor that is only
    correct because of who happens to call it is a contract, and this file's own history says
    contracts drift."""

    __slots__ = ("_policy", "_lock")

    def __deepcopy__(self, memo: dict[int, Any]) -> "_SharedFloor":
        # The SDK's fork deep-copies a conversation's events, and the system-prompt event holds the
        # built hands. That copy is a record, never executed: the fork's agent builds its OWN hands
        # (and floor) from its tool spec. A lock cannot be copied, so the record shares this object.
        return self

    def __init__(self, policy: CrownJewelsPolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()

    @property
    def policy(self) -> CrownJewelsPolicy:
        return self._policy

    def absorb(self, spawned: CrownJewelsPolicy) -> None:
        """UNION the four evolving socket fields of ``spawned`` into the live policy, under the
        floor's own lock. Monotonic: a merge can only ever ADD a deny, never drop one — the same
        fail-closed property :func:`refresh_socket_denies` provides within a single spawn, extended
        across concurrent spawners."""
        def _union(a: tuple[Path, ...], b: tuple[Path, ...]) -> tuple[Path, ...]:
            seen: set[Path] = set()
            out: list[Path] = []
            for q in list(a) + list(b):
                if q not in seen:
                    seen.add(q)
                    out.append(q)
            return tuple(out)

        with self._lock:
            cur = self._policy
            # Named explicitly rather than **kwargs: `dataclasses.replace` is type-checked per field,
            # and a **dict defeats that on the one object where a wrong field is a security defect.
            self._policy = dataclasses.replace(
                cur,
                deny_sockets=_union(cur.deny_sockets, spawned.deny_sockets),
                deny_write_files=_union(cur.deny_write_files, spawned.deny_write_files),
                socket_spellings=_union(cur.socket_spellings, spawned.socket_spellings),
                deny_write_dirs=_union(cur.deny_write_dirs, spawned.deny_write_dirs),
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

    def __init__(
        self,
        *,
        policy: CrownJewelsPolicy | None = None,
        floor: "_SharedFloor | None" = None,
        **kwargs: Any,
    ) -> None:
        # `policy=` remains the simple form (tests, direct construction) and gets a PRIVATE floor,
        # so this hand behaves exactly as before. `floor=` is what `create` passes to share the
        # evolving floor with the bash hand (glm L3, 2026-09-04).
        # ⛔ EXACTLY ONE OF `policy` / `floor` (codex L3 LOW, 2026-09-04). Accepting both and
        # silently preferring `floor` is fail-OPEN: a caller passing a strict policy alongside an
        # accidentally permissive floor got the permissive one with no error. On a crown-jewels
        # constructor, a configuration mistake must be a TypeError, not a silent preference.
        if (policy is None) == (floor is None):
            raise TypeError(
                "CrownJewelsFileEditorExecutor takes EXACTLY ONE of policy= or floor= "
                "(got both or neither) — passing both would silently ignore the policy."
            )
        if floor is None:
            floor = _SharedFloor(policy)  # type: ignore[arg-type]
        self._floor = floor
        super().__init__(workspace_root=str(self._floor.policy.workspace), **kwargs)

    @property
    def _policy(self) -> CrownJewelsPolicy:
        """Read THROUGH the shared floor, never a cached copy — that copy going stale while the bash
        hand's evolved is exactly the divergence this indirection exists to prevent."""
        return self._floor.policy

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
    """The floored file editor, built by :class:`LevainHands` (registry key :data:`LEVAIN_HANDS_TOOL`).

    Reuses the stock tool's rich (vision-aware) description + schema + annotations and swaps in the
    crown-jewels-floored executor — so the model sees the identical, familiar ``file_editor`` contract,
    now fenced to the floor. The LLM-visible ``.name`` stays ``"file_editor"`` (familiar → better
    tool-use on weak open models; the SDK keys ``tools_map`` on it) while the REGISTRY key is
    ``"levain_hands"`` (``register_tool`` below), so the unconfined stock tool is never reachable
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
        # RuntimeError too — `expanduser()` raises it for a `~user` with no passwd entry, and
        # THIS function's docstring says "Never raises." A RuntimeError escaping here does the
        # exact harm the paragraph above describes: it surfaces a raw AgentErrorEvent and SKIPS
        # the executor, so the floored executor's clean in-band refusal never fires — the guard
        # written to stop that, defeated by the spelling it did not catch.
        except (ValueError, OSError, RuntimeError):
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
    def create(  # type: ignore[override]
        cls, conv_state: "ConversationState", *, floor: "_SharedFloor"
    ) -> list["LevainFileEditorTool"]:
        """Build around ``floor`` — the one :class:`LevainHands` builds for both hands. Not registered
        on its own: an entity reaches it only through ``levain_hands``."""
        floored = CrownJewelsFileEditorExecutor(floor=floor)
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
    OS-confined bash — ``sandbox-exec`` on macOS, ``bwrap`` on Linux since K4c) instead of the SDK's
    un-confinable host ``TerminalExecutor``.

    The shell is spawned LAZILY on the first command — a ``--no-tools`` or never-touch-bash session
    pays nothing, and a spawn failure (no usable OS sandbox on this host) becomes a clean in-band refusal,
    never a conversation-build crash (fail-closed: no floor → no unconfined shell). The
    ``SandboxedShell`` is SINGLE-CALLER; :meth:`LevainBashTool.declared_resources` serializes bash calls
    against each other so two never race the one shell. ``reset`` closes + respawns a fresh shell;
    ``exit`` inside a command closes it and the next command respawns; ``is_input`` (interactive stdin)
    is refused — the confined shell is a non-interactive dev shell + agent-auth SSH, no PTY."""

    def __deepcopy__(self, memo: dict[int, Any]) -> "SandboxedBashExecutor":
        # Same reason as `_SharedFloor.__deepcopy__`: a fork's event record shares this executor;
        # the fork runs bash through the executor its own agent builds.
        return self

    def __init__(
        self,
        policy: CrownJewelsPolicy | None = None,
        *,
        floor: "_SharedFloor | None" = None,
        default_timeout: float = 120.0,
    ) -> None:
        # ⛔ EXACTLY ONE OF `policy` / `floor` (codex L3 LOW, 2026-09-04). Accepting both and
        # silently preferring `floor` is fail-OPEN: a caller passing a strict policy alongside an
        # accidentally permissive floor got the permissive one with no error. On a crown-jewels
        # constructor, a configuration mistake must be a TypeError, not a silent preference.
        if (policy is None) == (floor is None):
            raise TypeError(
                "SandboxedBashExecutor takes EXACTLY ONE of policy= or floor= "
                "(got both or neither) — passing both would silently ignore the policy."
            )
        if floor is None:
            floor = _SharedFloor(policy)  # type: ignore[arg-type]
        self._floor = floor
        self._default_timeout = default_timeout

        self._shell: SandboxedShell | None = None
        # Guards the lazy spawn / teardown against interrupt()/close() from another thread. Bash calls
        # themselves are serialized by declared_resources, so this is only the cross-thread guard.
        self._lock = threading.Lock()

    @property
    def _policy(self) -> CrownJewelsPolicy:
        """Read THROUGH the shared floor — same indirection as the file-editor hand, so the two
        cannot drift apart as the floor evolves at each spawn (glm L3, 2026-09-04)."""
        return self._floor.policy

    def _ensure_shell(self) -> SandboxedShell:
        """The live shell — spawning a fresh one on first use OR after the previous one exited
        (``exit``/reset). Raises :class:`ConfinementError` (caught by :meth:`__call__` → in-band
        refusal) if no OS confinement floor can be established here."""
        with self._lock:
            if self._shell is None or self._shell.closed:
                provider = select_provider()  # raises ConfinementError off a supported platform
                # ⛔ VALIDATE THE CANDIDATE BEFORE COMMITTING IT TO `self._shell` (codex L3 HIGH,
                # 2026-09-04). The previous version assigned FIRST and raised AFTER — so the first
                # command was refused while the LIVE shell stayed cached, and the NEXT command saw a
                # non-closed `_shell`, skipped this branch entirely, and executed through the
                # unverified shell. **The advertised fail-closed check was fail-once/open-next**,
                # which is worse than no check: it emits exactly one refusal that reads as the guard
                # working, and then silently stops guarding.
                # ⛔⛔ THE REFRESH HAPPENS HERE, UPSTREAM OF THE PROVIDER — AND THAT IS WHAT
                # REPLACED A METACLASS GUARD THAT WAS DEFEATED SEVEN TIMES ACROSS FIVE VERSIONS
                # (Phill ruled 2026-09-04). The provider is handed an ALREADY-REFRESHED policy, so
                # a provider cannot skip the re-resolution: there is no step for it to omit.
                # ⚡ The guard tried to make it impossible to SKIP a call. This makes the call not
                # exist at that layer -- for a provider that implements the `_spawn_shell_impl`
                # seam. The base `spawn_shell` refreshes again on its own behalf, so a consumer
                # calling it directly is covered too.
                # (A comment here once claimed the K4c branch's BwrapProvider inherited this by
                # construction; it overrode `spawn_shell` directly and did not. The port fixed it,
                # and `test_no_shipped_provider_overrides_spawn_shell` holds it.)
                refreshed = refresh_socket_denies(self._floor.policy)
                candidate = provider.spawn_shell(
                    refreshed, default_timeout=self._default_timeout
                )
                try:
                    # `effective_policy` is Optional on the TYPE because a SandboxedShell built
                    # directly (not through the provider seam) legitimately has none.
                    effective = candidate.effective_policy
                    if effective is None:
                        raise ConfinementError(
                            "the confined shell carries no effective policy — the provider seam did "
                            "not stamp it, so the socket floor it was rendered with is unknown "
                            "(fail-closed)."
                        )
                    # MERGE, never assign: `absorb` unions under the FLOOR'S OWN lock, so a
                    # concurrent spawner's denies cannot be lost to a wholesale overwrite.
                    self._floor.absorb(effective)
                    # ⛔ THE COMMIT IS INSIDE THE PROTECTED BLOCK (codex L3 LOW, 2026-09-04). An
                    # asynchronous exception landing after validation but before the assignment
                    # would otherwise leave a LIVE shell that is neither cached nor closed — its
                    # reader thread keeps it, and the subprocess outlives the refusal.
                    self._shell = candidate
                except BaseException:
                    # ⛔ BaseException, NOT Exception (codex L3 MED). A KeyboardInterrupt or
                    # SystemExit raised by `close()` would otherwise REPLACE the original refusal,
                    # which is the one signal this whole path exists to preserve.
                    self._shell = None
                    _close_candidate_shell(candidate)
                    raise
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
    """The confined bash tool, built by :class:`LevainHands` (registry key :data:`LEVAIN_HANDS_TOOL`).

    Reuses the stock terminal tool's schema (``TerminalAction``/``TerminalObservation``) + platform
    description + annotations and swaps in the :class:`SandboxedBashExecutor` — so the model sees the
    familiar ``terminal`` contract, now riding an OS sandbox instead of the un-confinable host shell.
    The LLM-visible ``.name`` stays ``"terminal"`` (the SDK keys ``tools_map`` on it) while the REGISTRY
    key is ``"levain_hands"``, so the unconfined stock terminal is never reachable from an entity."""

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
    def create(  # type: ignore[override]
        cls, conv_state: "ConversationState", *, floor: "_SharedFloor"
    ) -> list["LevainBashTool"]:
        """Build around ``floor`` (see :meth:`LevainFileEditorTool.create`)."""
        executor = SandboxedBashExecutor(floor=floor)
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


class LevainHands(ToolDefinition[Action, Observation]):
    """BOTH hands for one conversation, registered under :data:`LEVAIN_HANDS_TOOL` (spore-438).

    The SDK resolves a spec as ``create(conv_state=..., **spec.params)``, and may resolve separate
    specs concurrently. One spec for both hands means ONE ``create`` call builds ONE
    :class:`_SharedFloor` and hands it to both executors as a local value — so the two hands share the
    floor by construction, with no registry keyed on the conversation.
    The floor itself comes from ``binding``, the
    :class:`~levain.firing.binding.ConversationBinding` the session resolved once; this reads no file
    and resolves no mode."""

    name: ClassVar[str] = LEVAIN_HANDS_TOOL

    @classmethod
    def create(  # type: ignore[override]
        cls,
        conv_state: "ConversationState",
        *,
        binding: dict[str, Any],
        with_bash: bool = True,
    ) -> list[ToolDefinition[Any, Any]]:
        floor = _SharedFloor(ConversationBinding.from_params(binding).floor)
        tools: list[ToolDefinition[Any, Any]] = [
            *LevainFileEditorTool.create(conv_state, floor=floor)
        ]
        if with_bash:
            tools.extend(LevainBashTool.create(conv_state, floor=floor))
        return tools


# Register at import so ``Tool(name="levain_hands")`` resolves. Importing this module also imports the
# stock definitions (which self-register ``"file_editor"`` / ``"terminal"``) — harmless: an entity only
# ever references the levain name. A duplicate re-import just warns (registry is last-write-wins with
# the same resolver), never raises.
register_tool(LEVAIN_HANDS_TOOL, LevainHands)


def build_entity_tools(binding: ConversationBinding, *, with_bash: bool = True) -> list[Tool]:
    """The confined executor-tool bundle for a ``levain run`` entity: the crown-jewels-floored file
    editor + (``with_bash``) the OS-sandboxed bash, both fenced by ``binding``'s floor. Returns ONE
    ``Tool`` SPEC carrying the binding as data; the SDK resolves it into both hands at
    conversation-build time (:class:`LevainHands`).

    These are the ONLY blessed executor-tool builders Levain ships, and both are confined by
    construction. ``with_bash=False`` drops bash — the caller (``levain run``) passes it when no OS
    confinement floor can be established here
    (:func:`~levain.firing.confinement.confinement_supported`), so the entity keeps its file-editor
    hand rather than getting a bash whose first command would fail-closed. NEVER grants an unconfined
    shell as a fallback.

    ⚠ "HERE", not "on this platform": since K4c a Linux host HAS a provider, so the common Linux
    case is a supported platform that still cannot establish a floor (AppArmor-restricted user
    namespaces). The bash-free entity is therefore a normal Linux configuration rather than an
    exotic one, and the file-editor floor is fully cross-platform by design."""
    return [
        Tool(
            name=LEVAIN_HANDS_TOOL,
            params={"binding": binding.to_params(), "with_bash": with_bash},
        )
    ]
