"""levain.spores — the spore *disposition* vocabulary (control-plane Slice 3).

A spore's ``disposition`` routes it across THREE operator-facing classes:
  - ``loop`` (the default, absent/``loop``) — the entity's OWN prospective loop; belongs in
    its cognition (recall / salience / digest / Top of Mind).
  - the **Tray** inbox (``seed`` / ``handoff`` / ``agenda``) — operator session-I/O that
    should NOT pollute cognition; a ``seed`` metabolizes into a ``loop`` (or resolves). It
    FORMS, then resolves.
  - **Keep** reference (``note``) — durable operator reference; persists until removed,
    resolve-EXEMPT (never germinates, never ascends; removed via descend). Keep ALSO holds
    pinned-dormant loops via the existing ``parked`` TIER.
Tray + Keep are both ``NON_COGNITION``; lineage rides the existing ``pointer``.

The field is ADDITIVE: every pre-Slice-3 spore has no ``disposition`` key and reads as a
``loop`` (no migration; anneal round-trips an unknown ``disposition`` untouched — the
Slice-3 de-risk, proven empirically, is exactly why anneal needs NO change).

This module is the dashboard's render half: it CLASSIFIES a spore dict into the three
operator-facing projections — Open Loops / Tray / Keep — via :func:`bucket_of`.

⚠ **Scope of the "keep operator-I/O OUT of cognition" invariant.** ``is_loop`` is the
PREDICATE; who ENFORCES it depends on the surface. flow's own store enforces it at its
cognition reads (``scripts/spores.py`` + flow_state/constellation — flow's layer 2). A bare
LEVAIN install's cognition surfaces are its activation hooks
(``templates/activation/hooks/*`` — session-start dormant-surface + per-prompt collision):
3b ports ``is_loop`` there (the standalone hook copies filter on
``_NON_COGNITION_DISPOSITIONS``), so the boundary holds for a stranger too.

⚠ **Drift contract (load-bearing).** The taxonomy is duplicated in FOUR places BY DESIGN —
this module, flow's ``scripts/spores.py``, and the TWO standalone hook copies (Claude +
Codex adapters, which can't import a package). anneal — the shared dependency — deliberately
does NOT carry the taxonomy (the de-risk keeps the canonical object's storage
disposition-blind), so every consumer that must INTERPRET disposition carries the vocab. A
NEW operator-I/O disposition added upstream must be added to ALL FOUR (and to the write
seam's ``choices``). Guards: the flow↔levain cross-repo equality test
(``flow/scripts/test_tray_disposition.py``); the hook↔hook parity + hook↔levain.spores tests
(``levain/tests/test_hooks.py``). The ``is_loop`` definition below is the byte-for-byte twin
of flow's; keep them identical.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "LOOP_DISPOSITION",
    "TRAY_DISPOSITIONS",
    "KEEP_DISPOSITIONS",
    "NON_COGNITION_DISPOSITIONS",
    "VALID_DISPOSITIONS",
    "PARKED_TIER",
    "BUCKET_LOOP",
    "BUCKET_TRAY",
    "BUCKET_KEEP",
    "disposition_of",
    "is_loop",
    "is_tray",
    "is_note",
    "bucket_of",
]

LOOP_DISPOSITION = "loop"
TRAY_DISPOSITIONS = ("seed", "handoff", "agenda")   # operator INBOX — forms, then resolves
KEEP_DISPOSITIONS = ("note",)                        # operator durable REFERENCE — never resolves
# Everything the operator authored as I/O (inbox + reference) — all excluded from the
# entity's OWN cognition, the complement of a cognition loop. (Tray resolves; a note is
# resolve-exempt reference. Both are operator I/O, not the entity's prospective loops.)
NON_COGNITION_DISPOSITIONS = TRAY_DISPOSITIONS + KEEP_DISPOSITIONS
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + NON_COGNITION_DISPOSITIONS

# The tier value that means *deliberate dormancy* (anneal's own ``Tier`` literal — see
# ``anneal_memory.spores.VALID_TIERS``). A parked LOOP is Keep — pinned-dormant, exempt
# from the dormancy→compost prompt — NOT a disposition. Kept as a literal (not an anneal
# import) so this render-vocab module stays import-light; anneal owns the tier taxonomy.
PARKED_TIER = "parked"

# The three operator-facing render buckets (the capture-UX (A) projection: ONE Tray, not
# three disposition panels — seed/handoff/agenda all land in Tray).
BUCKET_LOOP = "loop"   # the entity's active prospective loops (Open Loops panel)
BUCKET_TRAY = "tray"   # the operator inbox awaiting AI triage (Tray panel)
BUCKET_KEEP = "keep"   # durable reference (notes) + pinned-dormant loops (Keep panel)


def disposition_of(item: Mapping[str, Any]) -> str:
    """A spore with no ``disposition`` key is a normal prospective loop (the default
    for every pre-Slice-3 spore)."""
    return item.get("disposition") or LOOP_DISPOSITION


def is_loop(item: Mapping[str, Any]) -> bool:
    """True iff the spore flows into the entity's OWN cognition (salience / Top of Mind /
    digest / constellation agent seeds). The operator-I/O dispositions — the Tray inbox
    (seed/handoff/agenda) AND Keep reference (note) — are the complement, kept OUT of
    cognition.

    Fail-OPEN on the unknown: only an EXPLICITLY known operator-I/O disposition is
    excluded; a falsy/absent value (every pre-Slice-3 spore) OR an unrecognized string
    reads as in-cognition. The exclusion axis is the silent-harm one — an operator-I/O
    item leaking into a render is visible text, but a real loop wrongly bucketed out
    vanishes from the Open-Loops panel with no signal. An unknown value is almost always
    a typo / corruption / version-skew on a real loop — fail it toward the visible loop.
    (Byte-for-byte twin of flow's ``scripts/spores.py:is_loop``; keeping them identical
    is the drift contract above.)"""
    return disposition_of(item) not in NON_COGNITION_DISPOSITIONS


def is_tray(item: Mapping[str, Any]) -> bool:
    """The operator session-I/O INBOX set (seed/handoff/agenda) — EXPLICIT membership,
    NOT ``not is_loop``: a ``note`` is also non-cognition but it is Keep reference, not
    the Tray, so the three classes (loop / tray / keep) are distinct."""
    return disposition_of(item) in TRAY_DISPOSITIONS


def is_note(item: Mapping[str, Any]) -> bool:
    """The operator durable-REFERENCE set (Keep notes) — resolve-exempt, agent-queryable,
    cognition-excluded. Explicit membership, the complement-within-Keep of a parked loop."""
    return disposition_of(item) in KEEP_DISPOSITIONS


def bucket_of(item: Mapping[str, Any]) -> str:
    """Classify a spore into its operator-facing render bucket — a TOTAL partition (every
    spore lands in exactly one):

    - ``BUCKET_TRAY`` — a Tray inbox disposition (seed/handoff/agenda), ANY tier.
      Disposition wins over tier: a parked-but-un-triaged seed is still a Tray item.
    - ``BUCKET_KEEP`` — durable reference: a ``note`` disposition (resolve-exempt) OR a
      loop on the ``parked`` tier (pinned-dormant, exempt from the dormancy→compost
      prompt). The two Keep halves from the original FATE design.
    - ``BUCKET_LOOP`` — a loop on any active tier: the entity's live cognition.

    Disposition is checked before tier so a (hypothetical) parked note still reads Keep.
    """
    d = disposition_of(item)
    if d in TRAY_DISPOSITIONS:
        return BUCKET_TRAY
    if d in KEEP_DISPOSITIONS or item.get("tier") == PARKED_TIER:
        return BUCKET_KEEP
    return BUCKET_LOOP
