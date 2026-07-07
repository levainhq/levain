"""The session_start half of the OpenHands firing-adapter: the constitution suffix.

The firing fires at TWO lifecycle points (``vagus.firing.FiringContract``):

  - ``per_turn``      → the ``VagusCondenser`` (recall + rotating directive, every turn).
  - ``session_start`` → THIS module: the static constitution, placed ONCE into the only
    set-once trusted surface OpenHands exposes — ``AgentContext.system_message_suffix``.

The 2026-06-07 spike mapped the boundary: ``system_message_suffix`` is the trusted, static,
set-once surface (it lives in the system message, so it survives compaction and is never a
trimmable recency event), while the condenser is the trusted, DYNAMIC, every-turn surface.
The constitution is static framing → it belongs in the suffix, not repeated every turn.

Afferent-only: this is framing injected into the agent's OWN context. It never acts outward
and never consolidates — same membrane as the rest of ``vagus.firing``.

Wiring both halves onto one agent::

    from openhands.sdk import Agent, LLMSummarizingCondenser
    from levain.firing.openhands import vagus_agent_context, VagusCondenser

    agent = Agent(
        llm=llm,
        tools=[...],
        agent_context=vagus_agent_context(firing_kind="anneal"),          # session_start
        condenser=VagusCondenser.build(                                    # per_turn
            inner=LLMSummarizingCondenser(llm=llm, max_size=120, keep_first=4),
            firing_kind="anneal",
        ),
    )

Use the SAME ``firing_kind`` for both halves so one firing's constitution and per-turn
recall/directive stay coherent.
"""
from __future__ import annotations

from openhands.sdk.context.agent_context import AgentContext

from levain.firing.contract import FiringContract, InjectRequest, build_firing


def vagus_agent_context(
    firing_kind: str = "stub",
    *,
    firing: FiringContract | None = None,
    base: AgentContext | None = None,
) -> AgentContext:
    """Build (or extend) an ``AgentContext`` carrying the firing's constitution as the
    set-once ``system_message_suffix``.

    Args:
        firing_kind: the serializable firing kind whose ``session_start`` inject supplies the
            constitution. Pass the SAME kind used for the ``VagusCondenser`` per-turn half.
        firing: an explicit live firing (test double / in-process handle). The suffix is a
            plain string computed ONCE here, so — unlike the condenser's per-turn handle —
            it has no fork/reload survival concern (the string is serialized into the
            AgentContext as-is). ``firing`` overrides ``firing_kind`` when given.
        base: an existing ``AgentContext`` (the adopter's own skills / suffixes). The
            constitution is APPENDED to any existing ``system_message_suffix`` (never
            clobbered) and all other base fields are preserved.

    Returns:
        an ``AgentContext`` whose ``system_message_suffix`` ends with the constitution.
    """
    f = firing if firing is not None else build_firing(firing_kind)
    # session_start is static: no recall query, no rotation index.
    constitution = f.inject(InjectRequest(lifecycle_point="session_start")).strip()

    existing = (base.system_message_suffix if base is not None else None) or ""
    # Append (don't clobber) the adopter's framing — two trusted blocks, theirs then ours.
    merged = f"{existing.rstrip()}\n\n{constitution}".strip() if existing.strip() else constitution

    if base is not None:
        return base.model_copy(update={"system_message_suffix": merged})
    return AgentContext(system_message_suffix=merged)
