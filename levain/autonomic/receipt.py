"""levain.autonomic.receipt — the gate's trace: the EFFERENT (action) face of the receipt.

The vagus efferent gate is the PRODUCER of the action-rooted projection of the FROZEN
DecisionInfluenceReceipt (``projects/anneal_memory/decision_influence_receipt_contract.md``). The
afferent stage already emitted the shared input slice with the efferent fields EXPLICITLY null
(``build_action_face`` below, lifted from vagus.afferent.proposal
at the 2026-10-06 fold so the efferent engine imports no afferent code); this module FILLS those nulls at the moment of gating —
``gate`` (the membrane's decision), ``authority_scope`` (the grant), ``downstream_claim_or_action_id``
(the fired action), ``actor_first_estimate`` (the human's pre-truth read, when a confirm routed
through a human).

It populates a dict slice — it does NOT build a receipt system (the schema is anneal-owned). It
carries the afferent face's builder (``build_action_face``, below) so the persisted shape is EXACTLY
the frozen contract (no new keys, just the nulls filled). anneal absorbs these fields (``gate``/``authority_scope`` from ``RECEIPT_VERSION=3``, ``terminal_state``/``refuse_class`` from 4) when it unifies the
recall + action corpora (the ``spore-166`` coordination checkpoint) — DECOUPLED: the vagus writes
its own slice now.
"""
from __future__ import annotations

import json
from typing import Any

from levain.autonomic.authority import AuthorityScope

__all__ = ["GateVerdict", "build_action_face", "build_gate_verdict", "build_gate_face"]


# The contract's ``query_text`` bound: ≤16KB. KB = BYTES, not characters — a char bound would let
# non-ASCII content (CJK/emoji) persist a receipt well over 16KB with ``query_truncated`` still
# false (apparatus L3 codex MED). Bound the UTF-8 ENCODING, on a codepoint boundary.
QUERY_TEXT_MAX_BYTES = 16 * 1024

# The frozen ``exposed[].source`` enum (checkpoint #1): afferent items are produced-not-retrieved.
SOURCE_AFFERENT = "afferent"


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_bytes`` of UTF-8 on a codepoint boundary (never splits a
    multibyte char). Returns ``(text_or_truncation, was_truncated)``. ``errors="ignore"`` drops only
    the trailing partial sequence the byte-cut would leave — the body is valid UTF-8, so nothing
    else is dropped — yielding a valid string whose encoding is ``<= max_bytes``."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


def build_action_face(
    *,
    context_id: str,
    query_text: str,
    query_date: str,
    producers: list[str],
) -> dict[str, Any]:
    """Populate the afferent-input slice of the FROZEN DecisionInfluenceReceipt.

    ``producers`` is the lineage(s) that produced proposals this cycle — ONE for the ambient single
    perceiver. (This ALREADY ranks N producers into N ``exposed[]`` entries, so the receipt layer
    is panel-ready; the Slice-2 panel that produces N RAW proposals + ONE receipt is a *runner*
    addition, not a receipt-schema one.) Every afferent exposed item carries ``source="afferent"``
    + ``producer=<lineage>`` and a null ``score`` (afferent perception carries no recall score —
    the human weights it at the fan-in).

    Face note (contract §2): the action face keys the influence event on ``context_id``; ``query_id``
    is the RECALL face's key for the same row (same field, different face) — so a ``query_id`` is
    intentionally absent here, not omitted by accident.

    The EFFERENT + human-supplied fields are left EXPLICITLY ``None`` — what the afferent stage
    knows vs what only the efferent gate / human supplies is structurally visible as the null set.
    ``gate``/``authority_scope`` land when the Phase-2 efferent gate writes (RECEIPT_VERSION=3);
    ``cited_used``/``actor_first_estimate``/``outcome_signal`` at the human collapse. ``provenance
    _spans`` is the grounding for the USED subset, so it is null until ``cited_used`` exists (it
    pairs with the collapse, not the perceive). No non-contract keys are emitted — the persisted
    slice is exactly the frozen contract's shape (apparatus L2 LOW: a ``_pending`` helper key would
    leak into the durable receipt that a strict RECEIPT_VERSION=3 / FleetView consumer reads).
    """
    bounded_query, truncated = _truncate_utf8(query_text, QUERY_TEXT_MAX_BYTES)
    exposed = [
        {
            "rank": rank,
            "score": None,                 # afferent: no recall score; human-weighted at the fan-in
            "source": SOURCE_AFFERENT,     # produced-not-retrieved (checkpoint #1 enum value)
            "producer": producer,          # the per-lineage provenance the kept map survives on
        }
        for rank, producer in enumerate(producers, start=1)
    ]
    return {
        "context_id": context_id,
        "query_text": bounded_query,
        "query_truncated": truncated,
        "query_date": query_date,
        "exposed": exposed,
        # --- pending the fan-in collapse (provenance_spans pairs with cited_used) + the efferent
        #     gate (gate / authority_scope): all EXPLICITLY null, no non-contract keys ------------
        "provenance_spans": None,
        "cited_used": None,
        "actor_first_estimate": None,
        "gate": None,                      # {verdict ∈ approved|denied|auto, by ∈ human|binding|on-loop}
        "authority_scope": None,
        "downstream_claim_or_action_id": None,
        "outcome_signal": None,
        # RECEIPT_VERSION=4 (§2.1) — the termination MODE + refuse taxonomy; the efferent gate fills
        # them at the terminal decision (a proposal that is never gated reads them null, like ``gate``).
        "terminal_state": None,            # fired | refused | killed | timed_out  (orthogonal to verdict)
        "refuse_class": None,              # constitutional | prudential | kill_triggered
    }


def _json_safe(value: Any) -> Any:
    """Coerce a value to a JSON-serializable form so it can NEVER corrupt the durable receipt
    (L1-HIGH-1). ``actor_first_estimate`` is typed ``Any`` (a human's free-form pre-truth read, fed
    by the Slice-2 confirm transport) — a ``datetime`` / dataclass / arbitrary object would raise
    ``TypeError`` at ``json.dumps`` time, crashing the append AFTER the effect fired. We make the
    receipt builder REFUSE non-serializable data (``structural_invariants_beat_discipline`` — don't
    trust the "JSON-serializable" docstring claim): a serializable value passes through unchanged; a
    non-serializable one is preserved as its ``str()`` (the human's read survives as text rather than
    crashing the trace). ``None`` stays ``None``."""
    if value is None:
        return None
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)

# The contract's ``gate.verdict`` enum: a constitutional/structural decision (``auto`` = an on-loop
# autonomous fire; ``denied`` = refused by the gate or the human), or a human ``approved``.
GateVerdict = str  # "approved" | "denied" | "auto"


def build_gate_verdict(*, verdict: GateVerdict, by: str, binding_id: str | None = None) -> dict[str, Any]:
    """Build the receipt's ``gate`` slice — ``{verdict ∈ approved|denied|auto, by ∈ human|binding|
    on-loop, binding_id?}``. ``binding_id`` is included only when a binding made the decision (kept
    out of the dict entirely otherwise, so a manual/on-loop gate carries no spurious null binding)."""
    g: dict[str, Any] = {"verdict": verdict, "by": by}
    if binding_id is not None:
        g["binding_id"] = binding_id
    return g


def build_gate_face(
    *,
    context_id: str,
    query_text: str,
    query_date: str,
    producers: list[str] | tuple[str, ...],
    gate: dict[str, Any],
    authority: AuthorityScope,
    terminal_state: str,
    downstream_id: str | None = None,
    actor_first_estimate: Any | None = None,
    refuse_class: str | None = None,
) -> dict[str, Any]:
    """Build the FILLED action-face: start from the afferent slice (which emits the exact frozen
    contract shape with the efferent fields null) and overlay only the fields the gate now knows.
    No new keys are introduced — the durable receipt stays exactly the frozen contract (a strict
    RECEIPT_VERSION=4 / FleetView consumer reads it unchanged).

    ``terminal_state`` (``RECEIPT_VERSION=4`` §2.1) is the termination-MODE — ``fired`` | ``refused`` |
    ``killed`` | ``timed_out`` — orthogonal to ``gate.verdict``'s decision-CLASS (``approved`` |
    ``denied`` | ``auto``). The gate fills it on EVERY terminal decision (a DEFERRED writes no receipt,
    so ``deferred`` never persists here). ``refuse_class`` (``constitutional`` | ``prudential`` |
    ``kill_triggered``) is null except where the gate can classify a refusal — today only the
    prediction-error KILL stamps ``kill_triggered`` (the broader refuse-vs-deny taxonomy is a separate
    receipt-contract checkpoint); the field is additive-nullable so a consumer reads it uniformly.

    ``cited_used`` / ``provenance_spans`` stay null here: the USED-subset + its grounding pair with
    the human's collapse (the fan-in), not the gate's fire — they land when a human cites at the
    confirm (Slice 2) or the outcome face fills (Phase 4)."""
    face = build_action_face(
        context_id=context_id,
        query_text=query_text,
        query_date=query_date,
        producers=list(producers),
    )
    face["gate"] = gate
    face["authority_scope"] = authority.to_dict()
    face["downstream_claim_or_action_id"] = downstream_id
    face["actor_first_estimate"] = _json_safe(actor_first_estimate)  # never let it corrupt the receipt
    face["terminal_state"] = terminal_state
    face["refuse_class"] = refuse_class
    return face
