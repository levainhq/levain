"""levain.autonomic.agent_bind — compile a named agent's plain values into a sealed Binding.

A named agent is not a standing grant in the ``bindings`` table. Each occurrence compiles a Binding from the values
that pin it (the role hash, the AgentSpec hash, the signed capabilities) and holds it only as that occurrence's
snapshot. Every fire recomputes the seal from the same values and compares it with the snapshot's id, so a role row,
a spec or a capability set that changed since signing no longer matches.

This module is in the autonomic leaf: it takes plain values only and imports nothing outside the package. The caller
reads the rows (the role row's stored text, the spec hash, the link's capabilities) and hands them in.

Two things are checked here and nowhere else in the leaf:

- **The role row is content-addressed.** The role table has no trigger, so a swapped row would otherwise compile
  under a pinned hash. The compiler re-hashes the row's text and refuses a mismatch.
- **A compiled Binding is recognisable.** :func:`is_agent_binding` names the shape so the fire path can refuse one
  that reaches it directly; the compiled form is built PAUSED and is never written to a registry.

Nothing here takes a weight or a score from a person: the tightness is fixed, and the posture is the most involved
rung that still executes, so the compiled core never reads as a grant to act unattended.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from levain.autonomic.binding import (Binding, BindingStatus, SubGoal, TightnessVector, TriggerSpec,
                                      seal_binding_id)
from levain.autonomic.posture import Posture

__all__ = [
    "AgentBindError",
    "AGENT_TRIGGER_TYPE",
    "AGENT_TOOL",
    "AGENT_CREATOR_PREFIX",
    "ROLE_HASH_PREFIX",
    "role_text_hash",
    "compile_binding",
    "seal",
    "is_agent_binding",
]

ROLE_HASH_PREFIX = b"levain-rolespec/1\n"
AGENT_TRIGGER_TYPE = "agent.run"
AGENT_TOOL = "run_agent"
AGENT_CREATOR_PREFIX = "agent:"

_HEX64 = frozenset("0123456789abcdef")


class AgentBindError(ValueError):
    """The values handed to the compiler do not describe a compilable agent; the message names the field."""


def role_text_hash(role_text: str) -> str:
    """The role hash of a stored role row's text: sha256 over the versioned prefix and the text's UTF-8 bytes."""
    return hashlib.sha256(ROLE_HASH_PREFIX + role_text.encode("utf-8")).hexdigest()


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX64:
        raise AgentBindError(f"{name} must be 64 lowercase hex characters")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentBindError(f"{name} must be a non-empty string")
    return value


def _core(*, agent_id: str, role_hash: str, role_text: str, spec_hash: str, capabilities: dict[str, Any],
          created_at: str) -> dict[str, Any]:
    _text(agent_id, "agent_id")
    _text(created_at, "created_at")
    _hash(role_hash, "role_hash")
    _hash(spec_hash, "spec_hash")
    if not isinstance(role_text, str):
        raise AgentBindError("role_text must be the role row's stored text")
    if not isinstance(capabilities, dict):
        raise AgentBindError("capabilities must be a mapping")
    try:
        role_digest = role_text_hash(role_text)
    except UnicodeEncodeError:
        raise AgentBindError("role_text holds a lone surrogate, which is not valid text") from None
    if role_digest != role_hash:
        raise AgentBindError("role row refused: its text does not hash to the pinned role_hash")
    try:
        # Canonical-JSON-unambiguous capabilities (str keys, finite numbers) or the seal would not be sound;
        # the trigger's own construction enforces the same floor, this fails earlier with the field named.
        json.dumps(capabilities, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise AgentBindError(f"capabilities is not canonical JSON: {e}") from None
    return {
        "created_by": AGENT_CREATOR_PREFIX + agent_id,
        "created_at": created_at,
        "trigger": TriggerSpec(type=AGENT_TRIGGER_TYPE, pattern={
            "agent_id": agent_id, "role_hash": role_hash, "spec_hash": spec_hash, "capabilities": capabilities}),
        "goal": (SubGoal(goal=f"{AGENT_TOOL}({spec_hash})", tools=(AGENT_TOOL,), output=f"agent:{agent_id}"),),
        "tightness": TightnessVector(goal_spec=1.0, tool_min=1.0, pattern_precision=1.0, output_bound=1.0),
        "posture": Posture.CONFIRM,
        "one_shot": True,
    }


def compile_binding(*, agent_id: str, role_hash: str, role_text: str, spec_hash: str,
                    capabilities: dict[str, Any], created_at: str) -> Binding:
    """Compile the plain values into a sealed, PAUSED Binding. Deterministic: equal inputs give an equal Binding,
    and a changed byte in any input gives a different ``binding_id``. Raises :class:`AgentBindError` when a value is
    malformed or when ``role_text`` does not hash to ``role_hash``."""
    core = _core(agent_id=agent_id, role_hash=role_hash, role_text=role_text, spec_hash=spec_hash,
                 capabilities=capabilities, created_at=created_at)
    return Binding.create(status=BindingStatus.PAUSED, **core)


def seal(*, agent_id: str, role_hash: str, role_text: str, spec_hash: str, capabilities: dict[str, Any],
         created_at: str) -> str:
    """The ``binding_id`` the compiler would produce for these values, without building the Binding. A fire compares
    this with the snapshot's id."""
    core = _core(agent_id=agent_id, role_hash=role_hash, role_text=role_text, spec_hash=spec_hash,
                 capabilities=capabilities, created_at=created_at)
    return seal_binding_id(**core)


def is_agent_binding(binding: Binding) -> bool:
    """True for the compiled form: the agent creator prefix and the agent trigger type together."""
    return (isinstance(binding.created_by, str) and binding.created_by.startswith(AGENT_CREATOR_PREFIX)
            and binding.trigger.type == AGENT_TRIGGER_TYPE)
