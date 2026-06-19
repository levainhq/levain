"""levain.spores — the spore *disposition* vocabulary (control-plane Slice 3).

A spore's ``disposition`` routes it between the entity's OWN prospective loops (the
default, absent/``loop`` → these belong in the entity's cognition: recall / salience /
digest / Top of Mind) and the operator's session-I/O **Tray** (``seed`` / ``handoff`` /
``agenda`` → the operator inbox, which should NOT pollute that cognition). A ``seed``
metabolizes into a ``loop`` (or descends/ascends to a real object); lineage rides the
existing ``pointer``. **Keep**'s pinned-dormant reuses the existing ``parked`` TIER, not
a disposition.

The field is ADDITIVE: every pre-Slice-3 spore has no ``disposition`` key and reads as a
``loop`` (no migration; anneal round-trips an unknown ``disposition`` untouched — the
Slice-3 de-risk, proven empirically, is exactly why anneal needs NO change).

This module is the dashboard's render half (layer 3a): it CLASSIFIES a spore dict into
the three operator-facing projections — Open Loops / Tray / Keep — via :func:`bucket_of`.

⚠ **Scope of the "keep Tray OUT of cognition" invariant.** ``is_loop`` is the PREDICATE;
who ENFORCES it depends on the surface. flow's own store enforces it at its cognition
reads (``scripts/spores.py`` + flow_state/constellation — flow's already-shipped layer 2).
A bare LEVAIN install's cognition surfaces are its activation hooks
(``templates/activation/hooks/*`` — session-start dormant-surface + per-prompt collision),
which do NOT yet filter disposition: porting ``is_loop`` there is the 3b work that ships
WITH the Tray write path (a stranger can't CREATE a Tray item until 3b, so the invariant
is vacuously held until then). This module (3a) only RENDERS the split — it does not, on
its own, keep anything out of a Levain entity's cognition.

⚠ **Drift contract (load-bearing).** ``TRAY_DISPOSITIONS`` is duplicated here AND in
flow's own ``scripts/spores.py`` BY DESIGN. anneal — the shared dependency — deliberately
does NOT carry the disposition taxonomy (the de-risk keeps the canonical object's storage
disposition-blind), so each consumer that must INTERPRET disposition carries the vocab:
flow's ``scripts/spores.py`` for the cognition-exclude (layer 2), this module for the
render partition (layer 3). They MUST agree on the literal tuple. A NEW Tray-class
disposition added upstream must be added to BOTH (and to the write seam's ``choices``) or
the cognition boundary and the render boundary drift apart. The ``is_loop`` definition
below is the byte-for-byte twin of flow's; keep them identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "LOOP_DISPOSITION",
    "TRAY_DISPOSITIONS",
    "VALID_DISPOSITIONS",
    "PARKED_TIER",
    "BUCKET_LOOP",
    "BUCKET_TRAY",
    "BUCKET_KEEP",
    "disposition_of",
    "is_loop",
    "is_tray",
    "bucket_of",
]

LOOP_DISPOSITION = "loop"
TRAY_DISPOSITIONS = ("seed", "handoff", "agenda")
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + TRAY_DISPOSITIONS

# The tier value that means *deliberate dormancy* (anneal's own ``Tier`` literal — see
# ``anneal_memory.spores.VALID_TIERS``). A parked LOOP is Keep — pinned-dormant, exempt
# from the dormancy→compost prompt — NOT a disposition. Kept as a literal (not an anneal
# import) so this render-vocab module stays import-light; anneal owns the tier taxonomy.
PARKED_TIER = "parked"

# The three operator-facing render buckets (the capture-UX (A) projection: ONE Tray, not
# three disposition panels — seed/handoff/agenda all land in Tray).
BUCKET_LOOP = "loop"   # the entity's active prospective loops (Open Loops panel)
BUCKET_TRAY = "tray"   # the operator inbox awaiting AI triage (Tray panel)
BUCKET_KEEP = "keep"   # durable pinned-dormant loops (Keep panel)


def disposition_of(item: Mapping[str, Any]) -> str:
    """A spore with no ``disposition`` key is a normal prospective loop (the default
    for every pre-Slice-3 spore)."""
    return item.get("disposition") or LOOP_DISPOSITION


def is_loop(item: Mapping[str, Any]) -> bool:
    """True iff the spore flows into the entity's OWN cognition (salience / Top of Mind /
    digest / constellation agent seeds). The operator Tray dispositions (seed / handoff /
    agenda) are the complement — surfaced at session-open, kept OUT of cognition.

    Fail-OPEN on the unknown: only an EXPLICITLY known Tray disposition is excluded; a
    falsy/absent value (every pre-Slice-3 spore) OR an unrecognized string reads as
    in-cognition. The exclusion axis is the silent-harm one — a Tray item leaking into a
    render is visible text, but a real loop wrongly bucketed as Tray vanishes from the
    Open-Loops panel with no signal. An unknown value is almost always a typo /
    corruption / version-skew on a real loop, not a legitimate Tray item — fail it toward
    the visible loop. (Byte-for-byte twin of flow's ``scripts/spores.py:is_loop`` — the
    cognition-exclude predicate; keeping them identical is the drift contract above.)"""
    return disposition_of(item) not in TRAY_DISPOSITIONS


def is_tray(item: Mapping[str, Any]) -> bool:
    """The explicit complement of :func:`is_loop`: the operator session-I/O Tray set."""
    return not is_loop(item)


def bucket_of(item: Mapping[str, Any]) -> str:
    """Classify a spore into its operator-facing render bucket — a TOTAL partition (every
    spore lands in exactly one):

    - ``BUCKET_TRAY`` — a Tray disposition (seed/handoff/agenda), ANY tier. Disposition
      wins over tier: a parked-but-un-triaged seed is still a Tray item (it needs the AI's
      sort), and this keeps the render boundary identical to layer 2's cognition-exclude
      (``is_tray`` ≡ ``not is_loop``).
    - ``BUCKET_KEEP`` — a loop on the ``parked`` tier: durable, pinned-dormant, exempt
      from the dormancy→compost prompt.
    - ``BUCKET_LOOP`` — a loop on any active tier: the entity's live cognition.
    """
    if is_tray(item):
        return BUCKET_TRAY
    if item.get("tier") == PARKED_TIER:
        return BUCKET_KEEP
    return BUCKET_LOOP
