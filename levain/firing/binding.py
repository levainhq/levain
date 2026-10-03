"""levain.firing.binding — the ONE per-conversation binding (spore-438).

A conversation serves one entity, is driven in one mode, and is fenced by one crown-jewels floor.
:class:`ConversationBinding` holds all three, is created ONCE (when the session opens, before its agent
exists) and is never changed. Every enforcer is HANDED it as data — the banner, both hands (through
the ``Tool`` spec params the OpenHands SDK passes to ``create()``), the condenser's recall and
re-anchor — so none of them looks a binding up, and nothing in this process can move one
conversation's floor by binding another.

Why data and not a lookup: the SDK resolves a tool as ``create(conv_state=..., **tool_spec.params)``
(``openhands/sdk/tool/registry.py``, ``_resolver_from_subclass``), so a spec's ``params`` is a
per-AGENT channel into the factory; ``test_confined_file_tool_lands_on_the_built_agent`` fails if an
SDK bump stops delivering it.
An earlier design (``seat/1002-spore438``) treated ``conv_state`` as the only channel and so kept
process-global registries keyed by it; three review rounds found new defects in that machinery each
time, so the machinery was deleted rather than guarded again.

**The entity-process latch.** One process-level bit survives, and it is not a lookup: once any
entity is opened in a process, :func:`mark_entity_process` sets ``$LEVAIN_ENTITY_PROCESS`` and the
DEFAULT (``"anneal"``-kind) store refuses to resolve the operator's ``~/.anneal-memory`` there. It
names no entity, is written with one constant value, and is never cleared — so it needs no lock and
a failed start that set it can only make a stray default-kind op refuse (the fail-closed side).
Ruled by Phill 2026-10-02 ("approve levain design").

Dependency-isolated: stdlib + :mod:`levain.firing.confinement` / ``drive`` / ``isolation`` only — no
anneal, no OpenHands.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, get_args, get_type_hints

from levain.firing.confinement import (
    ConfinementConfig,
    CrownJewelsPolicy,
    SshMode,
    build_policy,
    load_confinement_config,
)
from levain.firing.drive import DRIVE_MODES, DriveMode, resolve_cred_floor
from levain.firing.isolation import guard_entity

__all__ = [
    "BindingError",
    "ConversationBinding",
    "LEVAIN_ENTITY_PROCESS_ENV",
    "entity_process_latched",
    "mark_entity_process",
]

LEVAIN_ENTITY_PROCESS_ENV = "LEVAIN_ENTITY_PROCESS"
"""Set (to ``"1"``) once a process has opened an entity. An env var rather than a module global so a
child Python process inherits it too; the confined shell builds its env from scratch and does not.
In-process, :data:`_LATCHED` is the authority (an env var can be scrubbed)."""


# The authority in THIS process: a module flag nothing in levain sets back to False, so code that
# scrubs or restores os.environ cannot un-latch it (codex L3, 2026-10-02). The env var only carries
# the latch into child processes.
_LATCHED = False


def mark_entity_process() -> None:
    """Latch this process as one that hosts an entity. Idempotent; never cleared."""
    global _LATCHED
    # The env var is written first, so a child spawned by another thread between the two writes
    # still inherits the latch; the reverse order left this process latched and that child not
    # (codex L3 r2, reasoned: a two-statement window, so it is fixed by order, not by a test).
    os.environ[LEVAIN_ENTITY_PROCESS_ENV] = "1"
    _LATCHED = True


def entity_process_latched() -> bool:
    """True once this process opened an entity, or when it inherited the latch from its parent."""
    return _LATCHED or os.environ.get(LEVAIN_ENTITY_PROCESS_ENV, "").strip() == "1"


class BindingError(ValueError):
    """A binding could not be created, or its serialized form is not a complete binding."""


def _policy_to_params(policy: CrownJewelsPolicy) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclasses.fields(policy):
        v = getattr(policy, f.name)
        if isinstance(v, Path):
            out[f.name] = str(v)
        elif isinstance(v, tuple):
            out[f.name] = [str(p) for p in v]
        else:
            out[f.name] = v
    return out


# How each policy field is (de)serialized, by its RESOLVED annotation — an explicit table, so a field
# whose type is not listed here REFUSES rather than passing through unchecked (and a dropped
# `from __future__ import annotations` in confinement.py cannot quietly move fields to "unchecked").
_PATH, _OPT_PATH, _PATHS, _BOOL, _SSH_MODE = "path", "optional_path", "paths", "bool", "ssh_mode"


def _field_kinds() -> dict[str, str]:
    hints = get_type_hints(CrownJewelsPolicy)
    table = {
        Path: _PATH,
        Optional[Path]: _OPT_PATH,
        tuple[Path, ...]: _PATHS,
        bool: _BOOL,
        SshMode: _SSH_MODE,
    }
    kinds: dict[str, str] = {}
    for f in dataclasses.fields(CrownJewelsPolicy):
        kind = table.get(hints[f.name])
        if kind is None:
            raise BindingError(
                f"crown-jewels policy field {f.name!r} has a type this binding cannot carry "
                "(fail-closed — teach ConversationBinding about it)."
            )
        kinds[f.name] = kind
    return kinds


def _abs_path(name: str, v: Any) -> Path:
    if not isinstance(v, str) or not Path(v).is_absolute():
        # A relative path can never match the resolved paths enforcement compares, so it would be a
        # silent hole in the floor rather than a deny.
        raise BindingError(f"floor field {name!r} holds a non-absolute path (fail-closed).")
    return Path(v)


def _policy_from_params(data: Any) -> CrownJewelsPolicy:
    # EXACT key set. Most policy fields default to an empty tuple, so a missing key would construct
    # a floor with a silent hole; an extra key means this is not the shape this code wrote.
    kinds = _field_kinds()
    if not isinstance(data, dict) or set(data) != set(kinds):
        raise BindingError(
            "the serialized floor does not carry exactly the crown-jewels policy fields — refusing "
            "to build a floor from it (fail-closed)."
        )
    kwargs: dict[str, Any] = {}
    for name, kind in kinds.items():
        v = data[name]
        if kind == _PATHS:
            if not isinstance(v, list):
                raise BindingError(f"floor field {name!r} is not a list of paths (fail-closed).")
            kwargs[name] = tuple(_abs_path(name, x) for x in v)
        elif kind == _PATH:
            kwargs[name] = _abs_path(name, v)
        elif kind == _OPT_PATH:
            kwargs[name] = None if v is None else _abs_path(name, v)
        elif kind == _BOOL:
            if not isinstance(v, bool):
                raise BindingError(f"floor field {name!r} is not a bool (fail-closed).")
            kwargs[name] = v
        else:  # _SSH_MODE
            if v not in get_args(SshMode):
                raise BindingError(f"floor field {name!r} is not an ssh mode (fail-closed).")
            kwargs[name] = v
    # Enforcers key on ssh_dir, not ssh_mode: "agent" with no ssh_dir would drop the ~/.ssh deny.
    if (kwargs["ssh_mode"] == "agent") != (kwargs["ssh_dir"] is not None):
        raise BindingError("the serialized floor's ssh_mode and ssh_dir disagree (fail-closed).")
    return CrownJewelsPolicy(**kwargs)


@dataclass(frozen=True)
class ConversationBinding:
    """``(entity_dir, mode, floor)`` for one conversation. Build it with :meth:`create` only.

    ``deny_standard_creds`` is the RESOLVED cred floor (the config's tri-state through the drive
    mode) — the value the banner prints, kept beside the floor that already encodes it."""

    entity_dir: Path
    mode: DriveMode
    floor: CrownJewelsPolicy
    deny_standard_creds: bool

    @classmethod
    def create(
        cls,
        entity_dir: Path | str,
        *,
        mode: DriveMode,
        workspace: Path | str,
        config: ConfinementConfig | None = None,
    ) -> "ConversationBinding":
        """Resolve the floor ONCE, from a live entity.

        ``config`` is the entity's already-loaded ``confinement.json`` when the caller read it for its
        own decisions (the session does), so the floor and those decisions come from ONE read;
        ``None`` loads it here.

        Refuses (``IsolationError``) an entity dir that is not an initialized entity or whose store
        would escape isolation — so a resume of a deleted entity is refused here, never rebuilt
        without its declarations. Refuses (``ConfinementError``) a malformed ``confinement.json`` and
        (:class:`BindingError`) an unknown drive mode."""
        if mode not in DRIVE_MODES:
            raise BindingError(f"unknown drive mode {mode!r}; expected one of {DRIVE_MODES}")
        ed, _crystal, _episodic = guard_entity(entity_dir)
        cfg = config if config is not None else load_confinement_config(ed)
        # RESOLVED through the mode, never the raw tri-state: `None` means "derive from the drive",
        # and a raw read would treat an undeclared entity as opted OUT of the cred floor.
        deny_creds = resolve_cred_floor(cfg.deny_standard_creds, mode=mode)
        floor = build_policy(
            ed,
            workspace=Path(workspace).expanduser().resolve(),
            ssh_mode=cfg.ssh_mode,
            deny_files=cfg.deny_files,
            extra_deny_read_write=cfg.deny_subtrees,
            deny_standard_creds=deny_creds,
            # spore-725. NOT drive-resolved: a reachable container daemon is a total bypass in every
            # drive mode, so a watching operator is no mitigation.
            allow_container_sockets=cfg.allow_container_sockets,
            # spore-755. Both ssh modes; the only switch is the operator opt-out. The residuals this
            # does not close are documented on levain.firing.confinement (spore-1005).
            deny_localhost_outbound=(not cfg.allow_localhost_outbound),
        )
        return cls(entity_dir=ed, mode=mode, floor=floor, deny_standard_creds=deny_creds)

    def to_params(self) -> dict[str, Any]:
        """JSON-shaped, so it survives the SDK's fork (``model_dump`` → ``model_validate``) and its
        state autosave."""
        return {
            "entity_dir": str(self.entity_dir),
            "mode": self.mode,
            "floor": _policy_to_params(self.floor),
            "deny_standard_creds": self.deny_standard_creds,
        }

    @classmethod
    def from_params(cls, data: Any) -> "ConversationBinding":
        """Deserialize only — no file is read and nothing is re-resolved, so the floor a tool is built
        with is the one the session resolved. Fails closed on any shape it did not write."""
        if not isinstance(data, dict) or set(data) != {
            "entity_dir", "mode", "floor", "deny_standard_creds"
        }:
            raise BindingError("not a serialized conversation binding (fail-closed).")
        ed, mode, creds = data["entity_dir"], data["mode"], data["deny_standard_creds"]
        if not (isinstance(ed, str) and ed) or mode not in DRIVE_MODES or not isinstance(creds, bool):
            raise BindingError("a serialized conversation binding has a malformed field (fail-closed).")
        floor = _policy_from_params(data["floor"])
        if floor.entity_dir != Path(ed):
            raise BindingError(
                "a serialized conversation binding's floor fences a different entity (fail-closed)."
            )
        return cls(entity_dir=Path(ed), mode=mode, floor=floor, deny_standard_creds=creds)
