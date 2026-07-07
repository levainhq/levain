"""levain.firing — the in-session firing + presence layer for a Levain entity.

Two harness-neutral seams, both **dependency-isolated leaves** (importing ``levain.firing`` pulls
NO OpenHands and NO anneal):

- the **FIRING contract** (``contract`` + the lazy ``anneal`` leaf) — per-turn memory recall +
  Stop→capture + the rotating drift-defense directive. Relocated from ``vagus.firing`` as the first
  installment of the vagus→Levain fold-back (the autonomic afferent/efferent engine stays in vagus;
  nothing here depends on it — a clean lift). Afferent-only.
- the **PRESENCE seam** (``presence``) — the compaction re-anchor content the ``LevainCondenser``
  folds in on the recovery turn.

The OpenHands binding lives in the ``levain.firing.openhands`` subpackage (behind the ``openhands``
extra). Importing THAT is what pulls OpenHands — never this module.
"""
from levain.firing.contract import (
    DIRECTIVES,
    CaptureRequest,
    FiringContract,
    InjectRequest,
    StubFiring,
    build_firing,
    register_firing,
    select_directive,
)
from levain.firing.presence import (
    PresenceSource,
    ReanchorRequest,
    StubPresence,
    build_presence,
    register_presence,
)

__all__ = [
    # firing contract
    "FiringContract",
    "InjectRequest",
    "CaptureRequest",
    "StubFiring",
    "DIRECTIVES",
    "select_directive",
    "register_firing",
    "build_firing",
    # presence seam
    "PresenceSource",
    "ReanchorRequest",
    "StubPresence",
    "register_presence",
    "build_presence",
]
