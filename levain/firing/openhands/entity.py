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

import logging
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
from levain.firing.seed import SEED_SUBDIR, EntitySeed

_log = logging.getLogger("levain.firing.openhands.entity")

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
    presence_kind: str = "entity_seed",
) -> EntityBinding:
    """Build an isolated OpenHands ``Agent`` for the entity at ``entity_dir``, running on ``llm``.

    Fail-closes on the sovereignty guard (via :func:`bind_entity`) BEFORE constructing anything.
    Every firing/condenser is wired with ``firing_kind="anneal_entity"`` — the isolated kind — so
    the agent recalls + captures ONLY under ``<entity>/.levain/`` and never touches flow's store,
    across fork/reload.

    ``inner`` defaults to an ``LLMSummarizingCondenser`` (compression-first, so the afferent inject
    is never summarized away or counted toward the size threshold — the shipped VagusCondenser
    contract).

    IDENTITY (step 4 — spore-294): the entity boots as ITSELF, sourced from its OWN ``seed/``:
      - the **constitution** (session_start) is rendered from ``<entity>/seed/*.md``
        (:class:`~levain.firing.seed.EntitySeed`) — ``origin`` (who it is) + ``world`` (its operator)
        + ``partnership`` (how it works) — and baked into the set-once ``system_message_suffix``, so a
        fresh "who are you?" answers with the seed identity, not the model's stock "I am OpenHands". A
        bare ``.levain``-only entity (no seed) falls back to the firing's generic default constitution.
      - the **re-anchor** (``presence_kind="entity_seed"``, the default) re-asserts that identity at
        recency on the post-compaction recovery turn (``SeedPresence``, resolved per-op from the bound
        ``$LEVAIN_ENTITY_DIR`` — fork-safe like the store). Pass ``presence_kind="stub"`` to opt out.

    The constitution rides a STRING baked into the AgentContext (fork-safe as data, so the per-turn
    firing kind need not carry it); the re-anchor rides the serializable ``presence_kind`` (rebuilt on
    fork). Both read only the ENTITY's own seed — never flow's fossil (isolation applies to the seed)."""
    ed, crystal, episodic = bind_entity(entity_dir)
    resolved_inner = (
        inner
        if inner is not None
        else LLMSummarizingCondenser(llm=llm, max_size=max_size, keep_first=keep_first)
    )
    # Seed-sourced constitution → the set-once suffix. ``None`` (no readable seed) falls through to
    # the firing's generic default (``vagus_agent_context`` consults ``ENTITY_FIRING_KIND``).
    seed = EntitySeed(ed)
    seed_constitution = seed.constitution()
    if seed_constitution is None and seed.seed_dir_present():
        # DISTINGUISH the two None cases (apparatus HIGH-2): a bare entity with NO seed/ booting
        # generic is expected + silent; a seed/ dir that is PRESENT but yielded no constitution
        # (unreadable / non-UTF-8 / a symlink refused by the isolation guard) is a step-4 FAILURE —
        # the entity boots as a generic substrate instead of itself, so surface it LOUD rather than
        # let the identity silently degrade with no signal.
        _log.warning(
            "entity %s has a %s/ directory but no readable constitution — booting with the GENERIC "
            "default identity, not the seed. Its %s/origin.md is missing, unreadable, escaping the "
            "entity tree, or has no identity body.",
            ed,
            SEED_SUBDIR,
            SEED_SUBDIR,
        )
    agent = Agent(
        llm=llm,
        tools=tools if tools is not None else [],
        # Serialize tool calls STRUCTURALLY (apparatus L2 MED — do not rely on the SDK default). The
        # file editor's crown-jewels check resolves a path, then the stock editor re-resolves it at
        # open(); with bash now granting a symlink primitive (`ln -s`), a CONCURRENT file-editor +
        # bash batch could swap a symlink component between the two resolves (TOCTOU) to read a jewel
        # through the un-sandboxed editor. Pinning to 1 makes the entity single-threaded across tool
        # calls (the file editor's `file:` lock and bash's `terminal:session` lock are disjoint, so
        # nothing else serializes them), closing that window by construction rather than by an SDK
        # default that a future bump could raise.
        tool_concurrency_limit=1,
        agent_context=vagus_agent_context(
            firing_kind=ENTITY_FIRING_KIND, constitution=seed_constitution
        ),
        condenser=LevainCondenser.build(
            inner=resolved_inner,
            firing_kind=ENTITY_FIRING_KIND,
            presence_kind=presence_kind,
        ),
    )
    return EntityBinding(entity_dir=ed, crystal_path=crystal, episodic_path=episodic, agent=agent)
