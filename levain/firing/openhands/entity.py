"""levain.firing.openhands.entity — the ISOLATED-entity agent chokepoint.

:func:`build_entity_agent` is the ONE blessed path to an OpenHands agent for a Levain entity. It
enforces the sovereignty invariant STRUCTURALLY and LOUD, at construction, BEFORE any REPL turn
runs (``structural_invariants_beat_discipline`` — the guard is unskippable because this is the only
constructor, not a discipline scattered across call sites):

  1. derive the entity's stores (:func:`~levain.firing.isolation.entity_store_paths`) and
     :func:`~levain.firing.isolation.assert_entity_isolated` — fail-closed if they'd reach the
     operator-laptop flow store or escape ``<entity>/.levain/``;
  2. verify the dir is an INITIALIZED entity (has ``.levain/``) — a friendly error, not a cryptic
     store-open failure three turns in;
  3. bind ``$LEVAIN_ENTITY_DIR`` — the serialization-safe channel the firing re-reads on fork, so
     isolation survives ``fork()`` / reload (a zero-arg registry rebuild finds the entity via env);
  4. build ``Agent(agent_context=vagus_agent_context(firing_kind="anneal_entity"),
     condenser=LevainCondenser.build(firing_kind="anneal_entity", ...))`` — EVERY firing/condenser
     uses the isolated kind, whose resolver has NO ``~/.anneal-memory/`` fallback, so the whole
     agent is isolated by construction.

Requires the ``openhands`` extra. The build-time guard is the loud front line; the firing's own
resolver (:class:`~levain.firing.anneal.AnnealEntityFiring`) is the fork/runtime backstop that
degrades to no-recall rather than leak. Two layers, one invariant.

The provisional ``levain run`` loop (the interactive REPL + Ollama LLM config + real tools) is
step 3 of the roadmap; this module is the chokepoint it will drive, so isolation is enforced BEFORE
the UX rides on top (``reference/levain_condenser_scope.md`` — "de-risk 'don't touch this laptop'
BEFORE any UX rides on it").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhands.sdk import LLM, Agent, LLMSummarizingCondenser
from openhands.sdk.context.condenser import CondenserBase

from levain.firing.isolation import (
    IsolationError,
    assert_entity_isolated,
    bind_entity,
    entity_store_paths,
)
from levain.firing.openhands.agent import vagus_agent_context
from levain.firing.openhands.levain_condenser import LevainCondenser

# The isolated firing kind — the ONE this module ever wires. Its resolver has no laptop-store
# fallback (see levain.firing.anneal.AnnealEntityFiring), so the whole agent stays sovereign.
ENTITY_FIRING_KIND = "anneal_entity"

__all__ = ["ENTITY_FIRING_KIND", "EntityBinding", "bind_entity", "build_entity_agent"]


@dataclass(frozen=True)
class EntityBinding:
    """The isolation the chokepoint enforced. Returned so ``levain run`` can DISPLAY it (``memory:
    <episodic>``, the honesty-floor provenance the operator sees) and a test can assert on it —
    provenance welded to the object, not spawned off it."""

    entity_dir: Path
    crystal_path: Path
    episodic_path: Path
    agent: Agent

    def capture_turn(self, conversation: object, *, session_id: str | None = None) -> None:
        """Run + capture a completed turn to the ENTITY store, pinning ``firing_kind="anneal_entity"``.

        Use this — NOT a bare ``vagus_run(conv)``, whose default ``firing_kind="anneal"`` resolves to
        the laptop flow store ``~/.anneal-memory/`` (the capture WRITE-leak, apparatus F1). The binding
        OWNS capture so the run loop can't wire an unisolated one; the pinned ``anneal_entity`` kind
        re-guards the store PER OP (``AnnealEntityFiring``), so it stays isolated even if ``.levain``
        is mutated after binding. (The entity-aware ``_env_*`` resolution also redirects a stray bare
        ``vagus_run`` to the entity, re-guarded — belt-and-suspenders; this is the belt.)"""
        from levain.firing.openhands.capture import vagus_run

        vagus_run(conversation, firing_kind=ENTITY_FIRING_KIND, session_id=session_id)

    def wrap_nudge(self, *, threshold: int | None = None) -> str | None:
        """The SessionEnd wrap-nudge against THIS entity's episodic store (never flow's, apparatus
        F2). RE-DERIVES + RE-GUARDS the episodic path from ``entity_dir`` at USE time (not the cached
        ``episodic_path``), so a post-bind ``.levain`` symlink-swap can't relocate the read to flow's
        store (codex round-2 TOCTOU) — a runtime guard, not a one-shot validator. Fail-soft: a guard
        trip degrades to ``None`` (no nudge), never a read of the wrong store."""
        from levain.firing.anneal import wrap_nudge as _wrap_nudge

        try:
            _, episodic = entity_store_paths(self.entity_dir)
            assert_entity_isolated(episodic, entity_dir=self.entity_dir)
        except IsolationError:
            return None  # guard tripped (e.g. post-bind .levain escape) → no nudge, never a leak
        return _wrap_nudge(episodic_path=episodic, threshold=threshold)


def build_entity_agent(
    entity_dir: Path | str,
    llm: LLM,
    *,
    tools: list[Any] | None = None,
    inner: CondenserBase | None = None,
    max_size: int = 120,
    keep_first: int = 4,
    presence_kind: str = "stub",
) -> EntityBinding:
    """Build an isolated OpenHands ``Agent`` for the entity at ``entity_dir``, running on ``llm``.

    Fail-closes on the sovereignty guard (via :func:`bind_entity`) BEFORE constructing anything.
    Every firing/condenser is wired with ``firing_kind="anneal_entity"`` — the isolated kind — so
    the agent recalls + captures ONLY under ``<entity>/.levain/`` and never touches flow's store,
    across fork/reload.

    ``inner`` defaults to an ``LLMSummarizingCondenser`` (compression-first, so the afferent inject
    is never summarized away or counted toward the size threshold — the shipped VagusCondenser
    contract). ``presence_kind`` stays ``"stub"`` until the real entity-seed presence source ships
    (roadmap step 4); the compaction re-anchor is a stub re-assertion for now."""
    ed, crystal, episodic = bind_entity(entity_dir)
    resolved_inner = (
        inner
        if inner is not None
        else LLMSummarizingCondenser(llm=llm, max_size=max_size, keep_first=keep_first)
    )
    agent = Agent(
        llm=llm,
        tools=tools if tools is not None else [],
        agent_context=vagus_agent_context(firing_kind=ENTITY_FIRING_KIND),
        condenser=LevainCondenser.build(
            inner=resolved_inner,
            firing_kind=ENTITY_FIRING_KIND,
            presence_kind=presence_kind,
        ),
    )
    return EntityBinding(entity_dir=ed, crystal_path=crystal, episodic_path=episodic, agent=agent)
