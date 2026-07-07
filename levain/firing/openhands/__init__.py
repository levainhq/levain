"""levain.firing.openhands — the OpenHands firing + presence adapter.

Importing this requires the ``openhands`` extra (``pip install 'levain[openhands]'``). The
harness-neutral seams (``levain.firing`` — the firing contract + the presence seam) carry NO
OpenHands import; this subpackage is the OpenHands binding.

Relocated from ``vagus.adapters.openhands`` (the vagus→Levain fold-back): ``VagusCondenser`` (the
firing condenser: per-turn recall + rotating directive), ``vagus_agent_context`` (the session_start
constitution suffix), ``vagus_run`` / ``render_turn`` (Stop→capture), and ``wrap_nudge`` (SessionEnd).
``LevainCondenser`` (this repo, Slice 1) subclasses ``VagusCondenser`` and adds the compaction-reinject
fold. (The ``Vagus*`` names are kept for now to minimize churn; a rename is optional follow-up polish.)
"""
from levain.firing.anneal import wrap_nudge  # SessionEnd→wrap-nudge (harness-neutral, re-exported)
from levain.firing.openhands.agent import vagus_agent_context
from levain.firing.openhands.capture import render_turn, vagus_run
from levain.firing.openhands.condenser import VagusCondenser
from levain.firing.openhands.entity import (
    ENTITY_FIRING_KIND,
    EntityBinding,
    bind_entity,
    build_entity_agent,
)
from levain.firing.openhands.levain_condenser import LevainCondenser

__all__ = [
    "LevainCondenser",
    "VagusCondenser",
    "vagus_agent_context",
    "vagus_run",
    "render_turn",
    "wrap_nudge",
    # the isolated-entity agent chokepoint (spore-277)
    "build_entity_agent",
    "bind_entity",
    "EntityBinding",
    "ENTITY_FIRING_KIND",
]
