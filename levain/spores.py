"""levain.spores — the spore *disposition* vocabulary (control-plane Slice 3).

A spore's ``disposition`` routes it across THREE operator-facing classes:
  - ``loop`` (the default, absent/``loop``) — the entity's OWN prospective loop; belongs in
    its cognition (recall / salience / digest / Top of Mind).
  - the **Tray** inbox (``seed`` / ``handoff`` / ``agenda``) — operator session-I/O that
    should NOT pollute cognition; a ``seed`` metabolizes into a ``loop`` (or resolves). It
    FORMS, then resolves.
  - legacy **note** reference (``note``) — durable operator reference; persists until removed,
    resolve-EXEMPT (never germinates, never ascends; removed via descend). There is no Keep
    panel any more: a note still in a store renders in the Tray, and a pinned-dormant loop
    (the ``parked`` TIER) renders in Open loops, marked parked.
Tray + note are both ``NON_COGNITION``; lineage rides the existing ``pointer``.

The field is ADDITIVE: every pre-Slice-3 spore has no ``disposition`` key and reads as a
``loop`` (no migration; anneal round-trips an unknown ``disposition`` untouched — the
Slice-3 de-risk, proven empirically, is exactly why anneal needs NO change).

This module is the dashboard's render half: it CLASSIFIES a spore dict into the three
operator-facing projections — Open Loops / Tray — via :func:`bucket_of`.

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
    "WRITE_DISPOSITIONS",
    "stored_disposition",
    "PARKED_TIER",
    "BUCKET_LOOP",
    "BUCKET_TRAY",
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
# WRITE set (WRITE_DISPOSITIONS): a new handoff or Keep note is refused at the write seam.
RETIRED_DISPOSITIONS = ("handoff", "note")
KEEP_DISPOSITIONS = ("note",)                        # legacy operator REFERENCE — read-only vocabulary
# Everything the operator authored as I/O — all excluded from the entity's OWN cognition.
# The retired names STAY: ``is_loop`` fails OPEN on an unknown value, so dropping them would
# flood every legacy note and handoff into the agent's collision surface. The order is the
# hook copies' tuple order (drift contract: byte-equal to ``_NON_COGNITION_DISPOSITIONS``).
NON_COGNITION_DISPOSITIONS = ("seed", "handoff", "agenda", "note")
# Two sets, two names. VALID: every value a row may HOLD (the read set; byte-equal to the embedder's
# ``VALID_DISPOSITIONS``, a drift-contract tuple). WRITE: every value a write may SET.
VALID_DISPOSITIONS = (LOOP_DISPOSITION,) + NON_COGNITION_DISPOSITIONS
WRITE_DISPOSITIONS = (LOOP_DISPOSITION,) + TRAY_WRITE_DISPOSITIONS

RETIRED_HANDOFF_MSG = (
    "handoff is retired: ask the operator with `levain ask` (or `--on <date>` for later), "
    "or put the work on the board"
)
RETIRED_KEEP_NOTE_MSG = (
    "the Keep is retired: a fact or command goes to memory (Durable Facts), "
    "an intention is a seed"
)


class RetiredDisposition(ValueError):
    """A write tried to SET a disposition outside the write set: a retired handoff / Keep note (any
    spelling anneal would persist as one), or a value nothing reads."""


def stored_disposition(value: Any) -> Any:
    """``value`` as anneal will persist it (``normalize_spore_field``: controls and trailing
    whitespace removed, NFC). A non-string is returned unchanged; anneal refuses it itself."""
    if not isinstance(value, str):
        return value
    from anneal_memory.spores import normalize_spore_field

    return normalize_spore_field(value)


def retired_message(value: Any) -> str | None:
    """The refusal text for a retired disposition value, in any spelling or case, else None."""
    stored = stored_disposition(value)
    key = stored.casefold() if isinstance(stored, str) else None
    if key == "handoff":
        return RETIRED_HANDOFF_MSG
    if key == "note":
        return RETIRED_KEEP_NOTE_MSG
    return None


def refuse_retired_disposition(value: Any, current: Any = None) -> None:
    """TS-1, the write seams' early refusal (a clean 422 before the store is touched). A retired value
    is refused unless ``current`` already holds its persisted form. ``current`` here is an unlocked
    read, so this is never the authority: the store guard is (:func:`guarded_spore_store`), and it
    passes an echo only on a CAS. An undo that RESTORES a retired value is a change, so it is refused."""
    msg = retired_message(value)
    if msg is not None and stored_disposition(value) != current:
        raise RetiredDisposition(msg)


def _refuse_unwritable(value: Any, cas: Any) -> None:
    """THE store rule, one bound for every writer. The disposition is judged as anneal will PERSIST
    it, against the write set (an allow-list, so no spelling of a retired or unknown value slips
    through). A value outside the set passes only as a provable non-change: ``cas`` (the call's own
    ``expect_disposition``, which anneal compares under its lock before writing) equals it. There is
    no fallback read: without that CAS it is refused."""
    if not isinstance(value, str):
        return   # absent / an explicit _UNSET / None (a clear); a non-str anneal refuses itself
    stored = stored_disposition(value)
    if not stored or stored in WRITE_DISPOSITIONS:
        return   # "" / whitespace is a clear, as anneal stores it
    if isinstance(cas, str) and cas == stored:
        return
    raise RetiredDisposition(
        retired_message(stored)
        or f"disposition must be one of {list(WRITE_DISPOSITIONS)} (got {value!r})")


_GUARDED: Any = None
_GUARDED_LOCK = threading.Lock()   # anneal is imported lazily (this module stays stdlib-only), so build once under a lock


def guarded_spore_store(path: Any) -> Any:
    """An anneal ``SporeStore`` that will not SET a disposition outside ``WRITE_DISPOSITIONS``
    through ``add``, ``update`` or ``apply`` (a planned cockpit effect that deserialized after an
    upgrade included), so no writer (``apply_edit``, the cockpit effects, a CLI, a migration) can
    route around the retirement. Legacy rows still read. An update that leaves a legacy value
    unchanged must say so with ``expect_disposition`` equal to it; a retried ``add`` of a key that
    already exists returns that row, as anneal's own ``add`` does."""
    global _GUARDED
    with _GUARDED_LOCK:
        if _GUARDED is None:
            _GUARDED = _build_guarded_class()
    return _GUARDED(path)


def _build_guarded_class() -> Any:
    from anneal_memory.spores import ApplyRefused, SporeStore

    class GuardedSporeStore(SporeStore):  # type: ignore[misc]
        # keyword-only, as anneal's own add/update are: a positional disposition cannot exist
        def add(self, **kw: Any) -> Any:
            try:
                _refuse_unwritable(kw.get("disposition"), None)   # a create has no prior value to echo
            except RetiredDisposition:
                key = kw.get("origin_key")
                # anneal's add is idempotent by key: a retry of a create that already landed returns
                # that row. Nothing is written here, and a keyed row never leaves the store's record
                existing = self.get_by_origin_key(key) if key is not None else None
                if existing is not None:
                    return existing
                raise
            return super().add(**kw)

        def update(self, spore_id: str, **kw: Any) -> Any:
            _refuse_unwritable(kw.get("disposition"), kw.get("expect_disposition"))
            return super().update(spore_id, **kw)

        def apply(self, effect: Any) -> Any:
            op = getattr(effect, "op", None)
            if op not in ("add", "update"):
                return super().apply(effect)
            try:
                args = dict(getattr(effect, "args", None) or {})
                # only an update takes expect_disposition (anneal refuses it on an add)
                _refuse_unwritable(args.get("disposition"), args.get("expect_disposition") if op == "update" else None)
            except RetiredDisposition as exc:
                if op == "add":
                    try:
                        landed = self.get_by_origin_key(effect.origin_key) is not None
                    except (ValueError, TypeError) as key_exc:
                        raise ApplyRefused(str(key_exc)) from key_exc
                    if landed:
                        # the create already landed; a keyed row stays in the store (open, resolved
                        # or the deleted registry), so anneal reports ALREADY and never creates
                        return super().apply(effect)
                raise ApplyRefused(str(exc)) from exc
            except (ValueError, TypeError) as exc:   # malformed args: the base store's contract
                raise ApplyRefused(str(exc)) from exc
            return super().apply(effect)

    return GuardedSporeStore


# The tier value that means *deliberate dormancy* (anneal's own ``Tier`` literal — see
# ``anneal_memory.spores.VALID_TIERS``). A parked LOOP is pinned-dormant (shown in Open loops, marked parked), exempt
# from the dormancy→compost prompt — NOT a disposition. Kept as a literal (not an anneal
# import) so this render-vocab module stays import-light; anneal owns the tier taxonomy.
PARKED_TIER = "parked"

# The two operator-facing render buckets (the capture-UX (A) projection: ONE Tray, not
# three disposition panels — seed/handoff/agenda all land in Tray). There is no Keep panel
# (task surfaces TS-5a, Phill 11-07): a parked loop shows in Open loops with a parked marker,
# and a legacy ``note`` row still in a store shows in the Tray as legacy, never dropped.
BUCKET_LOOP = "loop"   # the entity's prospective loops, parked ones marked (Open Loops panel)
BUCKET_TRAY = "tray"   # the operator inbox awaiting AI triage + legacy notes (Tray panel)


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

    - ``BUCKET_TRAY`` — a Tray inbox disposition (seed/handoff/agenda), ANY tier, or a legacy
      ``note`` row (the Keep is gone; a note still in a store is shown, badged ``note``, never
      dropped). Disposition wins over tier.
    - ``BUCKET_LOOP`` — a loop on any tier, a ``parked`` one included (pinned-dormant, exempt
      from the dormancy→compost prompt; its row carries the parked marker).
    """
    d = disposition_of(item)
    if d in TRAY_DISPOSITIONS or d in KEEP_DISPOSITIONS:   # legacy handoff (in TRAY_DISPOSITIONS) and legacy note both render in the Tray
        return BUCKET_TRAY
    return BUCKET_LOOP
