"""levain.slot.limits_doc — the ``levain-limits/1`` document as a slot reads it: ``L/policy/limits``.

The document layer alone, for the processes that run under ``ROOT_PYTHON`` (the install helper that writes the file,
the queue broker that reads it): stdlib and :mod:`levain.contracts.canon` only, 3.9-valid. :mod:`levain.consent.limits`
(Q13's counter and its local ``limits.json``) re-exports every name here, so there is one home for the format, the kinds,
the defaults and the slot ranges.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["AGENTS", "DEFAULTS", "KINDS", "LIMITS_FORMAT", "REPEAT_WINDOW", "SLOT_KINDS", "SLOT_LIMITS_MAX",
           "SLOT_RANGE", "LimitsDocError", "parse_limits_doc", "slot_doc", "slot_values"]

LIMITS_FORMAT = "levain-limits/1"
KINDS = ("calls", "active_hours", "fresh_tokens", "cache_read_tokens", "repeat")
# F5b, ruled 10-19: on by default, set above the operator's measured sessions (design §3 holds the measurement and its command)
DEFAULTS: Dict[str, float] = {"calls": 3000, "active_hours": 12, "fresh_tokens": 15_000_000,
                              "cache_read_tokens": 600_000_000, "repeat": 8}
REPEAT_WINDOW = 10
AGENTS = ("claude_code", "codex")
# what a queue-broker slot counts: its GATE frame carries no transcript path, so no token kind
SLOT_KINDS = ("calls", "active_hours", "repeat")
# a slot's limits document (L/policy/limits) is read whole up to this many bytes; a larger one is refused
SLOT_LIMITS_MAX = 4 << 10
# a slot value's inclusive range: an integer (the contracts canonicaliser refuses floats, so active_hours is whole hours),
# repeat at least 2 (1 would hold every call) and at most REPEAT_WINDOW (a slot has no Extend to widen the window, so a
# larger repeat limit could never be reached)
SLOT_RANGE = {"calls": (1, 10**9), "active_hours": (1, 10**9), "repeat": (2, REPEAT_WINDOW)}


class LimitsDocError(ValueError):
    """A ``levain-limits/1`` document refused. ``code`` is the refusal's name: ``limits-oversize``,
    ``limits-malformed`` (not canonical JSON, not an object, not this format), ``limits-key:<key>`` (a top-level key
    or a kind no slot reads, or an agent with no block), ``limits-not-counted:<kind>`` (a token kind: a slot counts no
    tokens) or ``limits-value:<block>[.<kind>]`` (a block not an object, a value not an integer in :data:`SLOT_RANGE`)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} ({detail})" if detail else code)
        self.code = code
        self.detail = detail


def slot_values(doc: Any, agent: Optional[str]) -> Dict[str, float]:
    """A parsed document read as a slot reads it: the WHOLE document is checked, every block whatever ``agent`` is,
    and its first fault raises :class:`LimitsDocError` (not a ``levain-limits/1`` object; top-level keys ``format``
    and any of ``default`` / ``claude_code`` / ``codex``; each block an object over :data:`SLOT_KINDS`; each value an
    integer in :data:`SLOT_RANGE`), as does an ``agent`` outside :data:`AGENTS` (never read as a block). The result is
    exactly :data:`SLOT_KINDS`: :data:`DEFAULTS`, overlaid by the ``default`` block, then by ``agent``'s.
    ``agent=None`` reads the ``default`` block only (a writer validating the document)."""
    if not isinstance(doc, dict) or doc.get("format") != LIMITS_FORMAT:
        raise LimitsDocError("limits-malformed", f"not a {LIMITS_FORMAT} object")
    for key in doc:
        if key != "format" and key not in ("default", *AGENTS):
            raise LimitsDocError(f"limits-key:{key}"[:128])
    for block in ("default", *AGENTS):
        got = doc.get(block)
        if got is None:
            continue
        if not isinstance(got, dict):
            raise LimitsDocError(f"limits-value:{block}", "a block is an object")
        for kind, v in got.items():
            if kind in KINDS and kind not in SLOT_KINDS:
                raise LimitsDocError(f"limits-not-counted:{kind}", "a slot counts no tokens")
            if kind not in SLOT_KINDS:
                raise LimitsDocError(f"limits-key:{kind}"[:128])
            lo, hi = SLOT_RANGE[kind]
            if type(v) is not int or not lo <= v <= hi:
                raise LimitsDocError(f"limits-value:{block}.{kind}", f"an integer from {lo} to {hi}")
    if agent is not None and agent not in AGENTS:
        raise LimitsDocError(f"limits-key:{agent}"[:128], "not an agent with a block")
    out = {k: DEFAULTS[k] for k in SLOT_KINDS}
    for block in ("default",) if agent is None else ("default", agent):
        out.update(doc.get(block) or {})       # each block checked an object above
    return out


def _limit_value(kind: str, v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:                            # an integer past float range raises here, and is a bad value like any other
        if not math.isfinite(v) or v <= 0 or (kind != "active_hours" and v != int(v)):
            return None
        return float(v)
    except (OverflowError, ValueError):
        return None


def parse_limits_doc(doc: Any, agent: Optional[str], *, slot: bool = False) -> Tuple[Dict[str, float], List[str]]:
    """THE reading of a parsed ``levain-limits/1`` document, Q13's ``limits.json`` and a slot's ``L/policy/limits``
    alike: (:data:`DEFAULTS` overlaid by the ``default`` block, then by ``agent``'s block; what was skipped).

    ``slot=False`` (Q13's local file): a block that is not an object, or a kind or value :func:`_limit_value` refuses,
    is skipped and named in the list; raises :class:`LimitsDocError` only when ``doc`` is not a ``levain-limits/1``
    object. ``slot=True``: :func:`slot_values` (the whole document checked, its first fault raised; exactly
    :data:`SLOT_KINDS`), and the list is empty. ``agent=None`` reads the ``default`` block only."""
    if slot:
        return slot_values(doc, agent), []
    if not isinstance(doc, dict) or doc.get("format") != LIMITS_FORMAT:
        raise LimitsDocError("limits-malformed", f"not a {LIMITS_FORMAT} object")
    out = dict(DEFAULTS)
    bad: List[str] = []
    for block in ("default",) if agent is None else ("default", agent):
        got = doc.get(block)
        if got is None:
            continue
        if not isinstance(got, dict):
            bad.append(block)
            continue
        for kind, v in got.items():
            val = _limit_value(kind, v) if kind in DEFAULTS else None
            if val is None:
                bad.append(f"{block}.{kind}")
            else:
                out[kind] = val
    return out, bad


def slot_doc(data: bytes, *, canonical: bool = True) -> Dict[str, Any]:
    """A slot's limits document from its bytes, checked whole (:func:`slot_values`), or :class:`LimitsDocError`: at
    most :data:`SLOT_LIMITS_MAX` bytes, parsed under the contracts refusals (duplicate keys, floats, NaN, out-of-range
    integers, a BOM, non-UTF-8: :func:`levain.contracts.canon.parse`) and, with ``canonical``, exactly its canonical
    bytes (the form the root writer writes, so one document has one byte form)."""
    from levain.contracts import ContractError
    from levain.contracts.canon import canonical_bytes, parse

    if not isinstance(data, bytes) or len(data) > SLOT_LIMITS_MAX:
        raise LimitsDocError("limits-oversize", f"over {SLOT_LIMITS_MAX} bytes")
    try:
        doc = parse(data)
    except (ContractError, RecursionError) as exc:
        raise LimitsDocError("limits-malformed", str(exc)[:200] or type(exc).__name__) from None
    slot_values(doc, None)
    if canonical and canonical_bytes(doc) != data:
        raise LimitsDocError("limits-malformed", "not its canonical bytes")
    return doc
