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
PREDICATE; who ENFORCES it depends on the surface. The embedder's own store enforces it at its
cognition reads (``scripts/spores.py`` + flow_state/constellation — the embedder's layer 2). A bare
LEVAIN install's cognition surfaces are its activation hooks
(``templates/activation/hooks/*`` — session-start dormant-surface + per-prompt collision):
3b ports ``is_loop`` there (the standalone hook copies filter on
``_NON_COGNITION_DISPOSITIONS``), so the boundary holds for a stranger too.

⚠ **Drift contract (load-bearing).** The taxonomy is duplicated in FOUR places BY DESIGN —
this module, the embedder's ``scripts/spores.py``, and the TWO standalone hook copies (Claude +
Codex adapters, which can't import a package). anneal — the shared dependency — deliberately
does NOT carry the taxonomy (the de-risk keeps the canonical object's storage
disposition-blind), so every consumer that must INTERPRET disposition carries the vocab. A
NEW operator-I/O disposition added upstream must be added to ALL FOUR (and to the write
seam's ``choices``). Guards: the flow↔levain cross-repo equality test
(``flow/scripts/test_tray_disposition.py``); the hook↔hook parity + hook↔levain.spores tests
(``levain/tests/test_hooks.py``). The ``is_loop`` definition below is the byte-for-byte twin
of the reference implementation's; keep them identical.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

__all__ = [
    "LOOP_DISPOSITION",
    "TRAY_DISPOSITIONS",
    "TRAY_WRITE_DISPOSITIONS",
    "RETIRED_HANDOFF_MSG",
    "RETIRED_KEEP_NOTE_MSG",
    "RetiredDisposition",
    "retired_message",
    "refuse_retired_disposition",
    "guarded_spore_store",
    "KEEP_DISPOSITIONS",
    "RETIRED_DISPOSITIONS",
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
TRAY_DISPOSITIONS = ("seed", "handoff", "agenda")   # operator INBOX — the READ set (a legacy handoff still reads)
TRAY_WRITE_DISPOSITIONS = ("seed", "agenda")        # the Tray values a NEW write may take
# RETIRED (TS-1): legacy rows still carry these. They stay in the READ sets (a legacy handoff
# or Keep note still renders, descends, and re-dispositions to seed/agenda) but are OUT of the
# WRITE set (VALID_DISPOSITIONS): a new handoff or Keep note is refused at the write seam.
RETIRED_DISPOSITIONS = ("handoff", "note")
KEEP_DISPOSITIONS = ("note",)                        # legacy operator REFERENCE — read-only vocabulary
# Everything the operator authored as I/O — all excluded from the entity's OWN cognition.
# The retired names STAY: ``is_loop`` fails OPEN on an unknown value, so dropping them would
# flood every legacy note and handoff into the agent's collision surface. The order is the
# hook copies' tuple order (drift contract: byte-equal to ``_NON_COGNITION_DISPOSITIONS``).
NON_COGNITION_DISPOSITIONS = ("seed", "handoff", "agenda", "note")
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + TRAY_WRITE_DISPOSITIONS   # the WRITE set

RETIRED_HANDOFF_MSG = (
    "handoff is retired: ask the operator with `levain ask` (or `--on <date>` for later), "
    "or put the work on the board"
)
RETIRED_KEEP_NOTE_MSG = (
    "the Keep is retired: a fact or command goes to memory (Durable Facts), "
    "an intention is a seed"
)


class RetiredDisposition(ValueError):
    """A write tried to CREATE a retired disposition (handoff / Keep note)."""


def retired_message(value: Any) -> str | None:
    """The refusal text for a retired disposition value, else None."""
    if value == "handoff":
        return RETIRED_HANDOFF_MSG
    if value == "note":
        return RETIRED_KEEP_NOTE_MSG
    return None


def refuse_retired_disposition(value: Any, current: Any = None) -> None:
    """TS-1, the ONE refusal every writer shares. A retired value is refused unless it is NOT a
    change (``current`` already holds it: an echo, or a crash-after-apply retry whose write already
    landed). An undo that RESTORES a retired value is a change, so it is refused (known edge)."""
    msg = retired_message(value)
    if msg is not None and value != current:
        raise RetiredDisposition(msg)


_GUARDED: Any = None
_GUARDED_LOCK = threading.Lock()   # anneal is imported lazily (this module stays stdlib-only), so build once under a lock


def guarded_spore_store(path: Any) -> Any:
    """An anneal ``SporeStore`` that refuses to CREATE a retired disposition through ``add``,
    ``update`` or ``apply`` (a planned cockpit effect that deserialized after an upgrade included),
    so no writer (``apply_edit``, the cockpit effects, a CLI, a migration) can route around the
    retirement. Legacy rows still read; a write that leaves a retired value unchanged passes."""
    global _GUARDED
    with _GUARDED_LOCK:
        if _GUARDED is None:
            _GUARDED = _build_guarded_class()
    return _GUARDED(path)


def _build_guarded_class() -> Any:
    from anneal_memory.spores import ApplyRefused, SporeStore

    class GuardedSporeStore(SporeStore):  # type: ignore[misc]
        def add(self, *a: Any, **kw: Any) -> Any:
            refuse_retired_disposition(kw.get("disposition"))
            return super().add(*a, **kw)

        def update(self, spore_id: str, *a: Any, **kw: Any) -> Any:
            d = kw.get("disposition")
            if retired_message(d) is not None:
                if "expect_disposition" in kw:
                    # anneal CAS-checks this INSIDE its transaction, so the stored value IS this
                    # one when the write lands: no unlocked read, no TOCTOU
                    cur_d = kw["expect_disposition"]
                else:
                    # known edge: no CAS sent, so this read is outside the store lock
                    cur_d = (self.get(spore_id) or {}).get("disposition")
                refuse_retired_disposition(d, cur_d)
            return super().update(spore_id, *a, **kw)

        def apply(self, effect: Any) -> Any:
            d = dict(getattr(effect, "args", None) or {}).get("disposition")
            if getattr(effect, "op", None) in ("add", "update") and retired_message(d) is not None:
                cur = self.get_by_origin_key(effect.origin_key)
                try:
                    if effect.op == "add" and cur is not None:
                        return super().apply(effect)   # the create already landed: anneal reports ALREADY
                    refuse_retired_disposition(d, (cur or {}).get("disposition"))
                except RetiredDisposition as exc:
                    raise ApplyRefused(str(exc)) from exc
            return super().apply(effect)

    return GuardedSporeStore


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
    (Byte-for-byte twin of the embedder's ``scripts/spores.py:is_loop``; keeping them identical
    is the drift contract above.)"""
    return disposition_of(item) not in NON_COGNITION_DISPOSITIONS


def is_tray(item: Mapping[str, Any]) -> bool:
    """The operator session-I/O INBOX set (seed/agenda, plus a legacy handoff row) — EXPLICIT membership,
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
    if d in TRAY_DISPOSITIONS or d == "handoff":   # a legacy handoff still renders in the Tray
        return BUCKET_TRAY
    if d in KEEP_DISPOSITIONS or item.get("tier") == PARKED_TIER:
        return BUCKET_KEEP
    return BUCKET_LOOP
