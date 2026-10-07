"""levain.autonomic.risk — the DECLARED action-risk manifest (agent_authority_model.md §2.1-2.2).

Risk is INTRINSIC and DECLARED — set deliberately in a manifest, NEVER inferred by a model. This is
the load-bearing safety boundary (``structural_invariants_beat_discipline``): the model selects
WHICH action; the manifest declares HOW DANGEROUS it is; untrusted input cannot talk its way into a
lower risk tier (same shape as the §1.5 gate — model proposes, structure disposes). Risk is a class
+ tags, not a float (§2.2 — reversibility is the dominant tag); a readable rule table over
(class × tags) is auditable where a magic number is not.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = ["RiskClass", "ActionRisk", "ActionManifest", "UnknownAction"]


class RiskClass(IntEnum):
    """The coarse risk band (§2.2). Ordered, but the TAGS below drive posture more than the class
    does (reversibility is dominant) — the class only forces a floor at ``CRITICAL``."""

    TRIVIAL = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass(frozen=True)
class ActionRisk:
    """The declared risk of one action. The tags carry the safety weight (§2.2):
    ``reversible`` is dominant (§1.4 reversibility gates the ceiling); ``external`` = reaches a
    human / the world (collapses to irreversible — the recipient saw it, §1.4 corollary);
    ``financial`` = moves money (the hardest floor). Frozen — a manifest entry is a declaration, not
    a mutable buffer."""

    cls: RiskClass
    reversible: bool
    external: bool
    financial: bool

    def __post_init__(self) -> None:
        """Validate types at construction (codex L3 LOW-8): a JSON-loaded manifest with
        ``reversible="false"`` (a TRUTHY string) would otherwise silently read as reversible and LOWER
        the floor. Fail LOUDLY here so a misconfigured manifest can never quietly weaken governance —
        the tags are booleans, ``cls`` is a ``RiskClass`` (``structural_invariants_beat_discipline``)."""
        if not isinstance(self.cls, RiskClass):
            raise TypeError(f"ActionRisk.cls must be a RiskClass, got {type(self.cls).__name__}")
        for field_name in ("reversible", "external", "financial"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(
                    f"ActionRisk.{field_name} must be a bool, got {type(getattr(self, field_name)).__name__}"
                )


class UnknownAction(KeyError):
    """Raised by a manifest lookup for an unregistered action. The gate CATCHES this and fails
    toward involvement (§1.3) — an unknown action is the maximally-untrusted case (a risk tier the
    model could reach by NAMING an action you never declared), never a silent low-risk default."""


class ActionManifest:
    """A registry of ``action_name → ActionRisk``. Fail-closed by construction (§1.3): an
    unregistered action raises :class:`UnknownAction` — there is deliberately NO "default risk,"
    because a default is a risk the model reaches by naming an action you never risk-classed. Build
    the manifest with the actions you've consciously declared; everything else refuses.

    The manifest is the governance artifact (Memory-Is-Governance at the action layer, §2.6): the
    operator governs the gradient by editing declarations here, the agent earns its way down it with
    evidence. Stdlib-only; the concrete per-deployment action set is the adapter's (fossil), this
    schema is the yeast."""

    def __init__(self, actions: dict[str, ActionRisk] | None = None) -> None:
        self._actions: dict[str, ActionRisk] = dict(actions or {})

    def register(self, name: str, risk: ActionRisk) -> None:
        """Declare (or re-declare) an action's risk. Last-writer-wins — a deployment composes its
        manifest by registering the actions it has consciously classed."""
        self._actions[name] = risk

    def risk_of(self, name: str) -> ActionRisk:
        """Return the declared risk, or raise :class:`UnknownAction` (fail-closed — §1.3)."""
        try:
            return self._actions[name]
        except KeyError as e:
            raise UnknownAction(name) from e

    def __contains__(self, name: object) -> bool:
        return name in self._actions
