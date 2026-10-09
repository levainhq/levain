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
from types import MappingProxyType

__all__ = ["RiskClass", "ActionRisk", "ActionManifest", "ManifestFrozen", "UnknownAction"]


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


class ManifestFrozen(RuntimeError):
    """Raised by :meth:`ActionManifest.register` on a manifest a gate holds (desk ruling (a), 2026-10-09):
    a declaration changes by building a new manifest and a new gate, never by editing one in place."""


class ActionManifest:
    """A registry of ``action_name → ActionRisk``. Fail-closed by construction (§1.3): an
    unregistered action raises :class:`UnknownAction` — there is deliberately NO "default risk,"
    because a default is a risk the model reaches by naming an action you never risk-classed. Build
    the manifest with the actions you've consciously declared; everything else refuses.

    The manifest is the governance artifact (Memory-Is-Governance at the action layer, §2.6): the
    operator governs the gradient by editing declarations here, the agent earns its way down it with
    evidence. Stdlib-only; the concrete per-deployment action set is the adapter's (fossil), this
    schema is the yeast.

    FROZEN PER GATE (desk ruling (a), 2026-10-09): an :class:`~levain.autonomic.gate.EfferentGate`
    freezes the manifest it is built with, and :meth:`register` raises :class:`ManifestFrozen` from then
    on, so every admission a gate makes reads one manifest. A change to the declarations, a TIGHTENING
    included, takes effect by building a new manifest and a new gate on it, never by editing one a gate
    holds. The only swap that exists is the process: a new process builds a new gate (flow's adapter
    built one per CLI command when this was written, 2026-10-09: ``grep -n 'build_gate()'
    scripts/vagus_efferent.py``). An IN-PROCESS swap needs one handle that every consumer of a
    gate (``FireDispatcher``, ``ChainExecutor``, the sweeps) reads it through, so no caller keeps an old
    gate after the swap and an admission in flight sees the old gate or the new, never a mix; no such
    handle is built. The trust-tiering fold's change to a standing policy MUST build that handle and take
    effect through it, with a test of the swap's atomicity (owed there: on e3e5010 a tightening
    registered mid-admission did not reproduce a mixed decision, because the fence read again at
    admission differed and the hold reopened at the new rung). A binding risk resolver that reads a
    catalog of its own is not frozen by the gate: within one admission the fence holds it to one read
    (the same e3e5010 run), and freezing it is the adapter's.
    """

    def __init__(self, actions: dict[str, ActionRisk] | None = None) -> None:
        self._actions: dict[str, ActionRisk] = dict(actions or {})
        self._frozen = False

    def register(self, name: str, risk: ActionRisk) -> None:
        """Declare (or re-declare) an action's risk while the manifest is being composed. Last-writer-wins.
        Raises :class:`ManifestFrozen` once a gate holds the manifest (:meth:`freeze`)."""
        if self._frozen:
            raise ManifestFrozen(
                f"cannot register {name!r}: a gate holds this manifest; build a new manifest and swap in a "
                "new gate built on it")
        self._actions[name] = risk

    def freeze(self) -> None:
        """End composition: every later :meth:`register` raises. Idempotent. The gate calls it. The
        declarations become a read-only view, so a write that does not go through :meth:`register` fails
        too."""
        self._frozen = True
        self._actions = MappingProxyType(dict(self._actions))  # type: ignore[assignment]

    @property
    def frozen(self) -> bool:
        return self._frozen

    def risk_of(self, name: str) -> ActionRisk:
        """Return the declared risk, or raise :class:`UnknownAction` (fail-closed — §1.3)."""
        try:
            return self._actions[name]
        except KeyError as e:
            raise UnknownAction(name) from e

    def __contains__(self, name: object) -> bool:
        return name in self._actions
