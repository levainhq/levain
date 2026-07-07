"""levain.firing.isolation — the sovereignty guard for an ISOLATED Levain entity.

An OpenHands Levain entity runs on an open model with its OWN memory — its anneal store lives
in the entity's own ``.levain/`` dir (``<entity>/.levain/memory.db`` + ``memory.crystal.json``,
the convention ``doctor._check_store`` / ``dashboard.SubstrateSource.local`` enforce). The #1
sovereignty requirement (Phill, 2026-07-07): an entity's firing must NEVER touch the operator-
laptop's flow store (``~/.anneal-memory/``).

The leak this closes (grounded): the default firing (``AnnealFiring``, kind ``"anneal"``) hard-
DEFAULTS its crystal + episodic paths to ``~/.anneal-memory/`` and the serialization-safe registry
rebuilds it ZERO-ARG on ``fork()`` / reload — so "point it at the entity via ``VAGUS_*`` env" is
DISCIPLINE, not structure. Forget to set the env (or reach a firing built off the chokepoint) and
an Ollama entity silently recalls flow's identity. That is the exact ``claim > enforcement`` gap.

The fix is STRUCTURAL (``structural_invariants_beat_discipline``):

  - :func:`entity_store_paths` derives the entity's crystal + episodic paths under
    ``<entity>/.levain/`` (mirrors ``dashboard.AnnealPaths.from_db``'s ``memory.db`` → sibling
    stem convention, re-derived here so this leaf stays free of the dashboard's dependency
    closure — the dependency-isolated-leaf invariant).
  - :func:`assert_entity_isolated` FAIL-CLOSES: any resolved store path that lives under
    ``~/.anneal-memory/`` OR escapes ``<entity>/.levain/`` raises :class:`IsolationError`. Both
    checks run (defense-in-depth) — either alone suffices given the derivation, but the redundancy
    IS the structural guarantee, and the forbidden-zone check runs FIRST so a ``.levain`` symlink
    into the flow store is caught before the containment check would (wrongly) pass it.
  - :class:`~levain.firing.anneal.AnnealEntityFiring` (kind ``"anneal_entity"``) resolves ONLY
    through this module and has NO ``~/.anneal-memory/`` fallback. The isolation contract rides
    the SERIALIZED ``firing_kind`` string, so a forked child rebuilds as the isolated kind — the
    contract survives fork by construction, not by remembering to re-set env.

Pure stdlib — importing this pulls NO anneal and NO openhands, so the guard is unit-testable in
complete isolation (the same discipline the firing contract + presence seams hold).
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "LEVAIN_ENTITY_DIR_ENV",
    "ENTITY_STORE_SUBDIR",
    "IsolationError",
    "flow_store_dir",
    "entity_store_paths",
    "resolve_entity_dir",
    "assert_entity_isolated",
    "bind_entity",
]

# The serialization-safe binding channel: the entity's ROOT dir. Read per-inject/-capture by
# ``AnnealEntityFiring`` (re-read, never frozen), so it survives a fork/reload — the ONE channel
# that round-trips a zero-arg registry rebuild, exactly as the legacy ``VAGUS_CRYSTAL_PATH``
# override does. ``levain run`` / :func:`~levain.firing.openhands.entity.build_entity_agent`
# set it (the process owns exactly one entity).
LEVAIN_ENTITY_DIR_ENV = "LEVAIN_ENTITY_DIR"

# The per-entity substrate dir (matches ``doctor._check_store`` / ``dashboard`` / ``install``).
ENTITY_STORE_SUBDIR = ".levain"


class IsolationError(RuntimeError):
    """Raised when an isolated entity's resolved store path would reach the operator-laptop flow
    store OR escape its own sovereign ``.levain/`` dir. FAIL-CLOSED: refuse rather than silently
    read/write the wrong store. Inside the firing's inject/capture this is caught by the fail-soft
    boundary → degrades to no-recall / a loud lost-capture, NEVER a silent read of flow's memory."""


def flow_store_dir() -> Path:
    """The operator-laptop flow store dir (``~/.anneal-memory``) — the named forbidden zone.

    Computed FRESH from ``Path.home()`` on each call (never a frozen module constant) so a test
    that repoints ``$HOME`` — and the real thing, an operator whose home differs — is honored. An
    entity firing that resolved anywhere under here has leaked flow's identity into the sovereign
    entity (the exact failure this module guards)."""
    return Path.home() / ".anneal-memory"


def entity_store_paths(entity_dir: Path | str) -> tuple[Path, Path]:
    """Derive ``(crystal_path, episodic_path)`` for an entity rooted at ``entity_dir``.

    ``<entity>/.levain/memory.crystal.json`` (crystal) + ``<entity>/.levain/memory.db`` (episodic)
    — the ``memory.db`` → ``memory.crystal.json`` stem convention (mirrors
    ``dashboard.AnnealPaths.from_db``, re-derived here to keep the firing leaf dashboard-free).
    Returned RESOLVED (symlink-safe, non-strict — the tail may not exist until the first wrap /
    first capture) so :func:`assert_entity_isolated`'s containment check is exact."""
    base = Path(entity_dir).expanduser() / ENTITY_STORE_SUBDIR
    crystal = (base / "memory.crystal.json").resolve()
    episodic = (base / "memory.db").resolve()
    return crystal, episodic


def resolve_entity_dir(explicit: Path | str | None = None) -> Path:
    """The bound entity root: ``explicit`` if given, else ``$LEVAIN_ENTITY_DIR``.

    Raises :class:`IsolationError` if NEITHER is available — an isolated firing with no bound
    entity FAILS CLOSED (no store) rather than falling back to a default, because the only
    conceivable default is the operator-laptop store this module exists to refuse."""
    raw = (
        str(explicit)
        if explicit is not None
        else os.environ.get(LEVAIN_ENTITY_DIR_ENV, "").strip()
    )
    if not raw:
        raise IsolationError(
            f"no entity bound: set ${LEVAIN_ENTITY_DIR_ENV} (or pass entity_dir) — an isolated "
            "entity firing has NO default store (refusing to fall back to the operator-laptop "
            "flow store ~/.anneal-memory/)."
        )
    return Path(raw).expanduser()


def _is_within(path: Path, root: Path) -> bool:
    """True iff ``path`` is ``root`` or lives under it. Both are assumed already-resolved."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_within_ci(path: Path, root: Path) -> bool:
    """Containment, ALSO catching a case-only difference on a case-insensitive volume (macOS /
    Windows), where ``~/.Anneal-Memory`` and ``~/.anneal-memory`` are the SAME dir yet
    ``relative_to`` compares case-sensitively (apparatus L1 finding). Separator-anchored, so it
    never false-matches a sibling like ``.anneal-memory-backup``. Used ONLY for the forbidden-zone
    check — over-matching there is fail-closed (refuse a case-variant of the flow store); the
    sovereign/containment checks stay exact so a legit case-distinct entity dir is never rejected."""
    if _is_within(path, root):
        return True
    rp, rt = str(path).casefold(), str(root).casefold()
    return rp == rt or rp.startswith(rt.rstrip(os.sep) + os.sep)


def assert_entity_isolated(*paths: Path, entity_dir: Path | str) -> None:
    """FAIL-CLOSED sovereignty guard. Raise :class:`IsolationError` if any of ``paths`` (resolved
    store paths) reaches the flow store, escapes ``<entity_dir>/.levain/``, OR the ``.levain`` dir
    itself is relocated outside the entity tree by a symlink.

    Three checks, all enforced (defense-in-depth, ``structural_invariants_beat_discipline``):

      1. **Forbidden zone (per-path, FIRST):** the path is under ``~/.anneal-memory/`` (case-
         insensitive, apparatus L1). Named explicitly so the error points at the exact failure —
         and checked before containment so a ``<entity>/.levain`` SYMLINK into the flow store
         (which ``.resolve()`` follows) is caught here with the right message.
      2. **Containment (per-path):** the path is NOT under ``<entity_dir>/.levain/``.
      3. **Sovereign-under-root (once):** the RESOLVED ``.levain`` dir must stay under the RESOLVED
         entity root (apparatus codex L3). A ``.levain`` symlinked to an arbitrary *non-flow*
         external dir would make check 2 pass VACUOUSLY (``sovereign`` follows the same symlink), so
         the store could still be relocated out of the entity tree — this closes that. A symlinked
         entity ROOT stays fine (``sovereign`` remains under the resolved root)."""
    forbidden = flow_store_dir().resolve()
    root = Path(entity_dir).expanduser().resolve()
    sovereign = (Path(entity_dir).expanduser() / ENTITY_STORE_SUBDIR).resolve()
    for p in paths:
        rp = Path(p).expanduser().resolve()
        if _is_within_ci(rp, forbidden):
            raise IsolationError(
                f"isolation violation: {rp} is under the operator-laptop flow store {forbidden} — "
                "an entity firing must never touch flow's memory."
            )
        if not _is_within(rp, sovereign):
            raise IsolationError(
                f"isolation violation: {rp} escapes the entity's sovereign store dir {sovereign}."
            )
    if not _is_within(sovereign, root):
        raise IsolationError(
            f"isolation violation: the entity store dir {sovereign} escapes the entity root {root} "
            "(a symlinked .levain must not relocate the store outside the entity tree)."
        )


def bind_entity(entity_dir: Path | str) -> tuple[Path, Path, Path]:
    """Resolve + GUARD an entity dir, then bind ``$LEVAIN_ENTITY_DIR`` (the process's single-entity
    binding). Returns ``(entity_dir, crystal_path, episodic_path)`` — all resolved.

    Raises :class:`IsolationError` if the dir is not an initialized entity or the derived stores would
    escape isolation. PURE — only ``os`` + this module's guard, NO anneal / NO openhands — so the
    ``levain run`` CLI (and any non-SDK caller) can bind + display paths WITHOUT the openhands extra.
    It lives HERE, not in the openhands adapter, precisely so that promise is true (apparatus codex
    round-2).

    The env write is the single-entity-per-process contract: this process owns exactly one entity, and
    a second bind to a DIFFERENT initialized entity is REFUSED (the entity↔entity cross-wire — the
    firing re-reads ``$LEVAIN_ENTITY_DIR`` every op, so a silent rebind would swap the first agent's
    store on its next turn). Idempotent re-bind to the same entity is fine; a leftover empty / non-dir
    value is not a live binding. NO ``$VAGUS_*`` backstop is written — the ``"anneal"``-kind default
    resolution is itself entity-aware + re-guarded PER OP when ``$LEVAIN_ENTITY_DIR`` is set
    (``anneal._env_*`` → ``_entity_env_path``), so a stray bare ``vagus_run`` / ``wrap_nudge`` in the
    entity process resolves to the entity at USE time — a runtime guard, not a cached bind-time path."""
    ed = Path(entity_dir).expanduser().resolve()
    if not (ed / ENTITY_STORE_SUBDIR).is_dir():
        raise IsolationError(
            f"{ed} is not an initialized Levain entity (no {ENTITY_STORE_SUBDIR}/). "
            "Run `levain init --adapter openhands` in it first."
        )
    crystal, episodic = entity_store_paths(ed)
    assert_entity_isolated(crystal, episodic, entity_dir=ed)  # loud, BEFORE binding
    existing = os.environ.get(LEVAIN_ENTITY_DIR_ENV, "").strip()
    if existing:
        existing_path = Path(existing).expanduser()
        if existing_path.is_dir() and existing_path.resolve() != ed:
            raise IsolationError(
                f"this process is already bound to a different entity ({existing_path.resolve()}); "
                f"refusing to rebind to {ed}. One process hosts one entity — start a new process."
            )
    os.environ[LEVAIN_ENTITY_DIR_ENV] = str(ed)  # the serialization-safe binding (re-read per op)
    return ed, crystal, episodic
