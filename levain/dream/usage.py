"""levain.dream.usage (D1-1c): ``<occ>/usage.json`` read back into A0's ``Usage`` (D1_EXECUTOR §7).

The executor writes ``usage.json`` on every terminal path (``executor._write_usage``). This module is the reading half
the supervisor settles from: it never invents a figure, and a file that is missing, torn or not a usage record reads
as an UNREADABLE ``Usage`` (``usd_micros=None``), so the whole reservation is debited (A0 invariant 3, fail-closed).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from levain.agents.accounting import Usage

USAGE_FILE = "usage.json"
UNREADABLE_SOURCE = "dream:unreadable"


def read_doc(occ_dir: Path | str) -> dict[str, Any] | None:
    """The parsed ``usage.json`` of an occurrence, or None when it is missing, torn or not an object."""
    try:
        doc = json.loads((Path(occ_dir) / USAGE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _count(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def to_usage(doc: dict[str, Any] | None) -> Usage:
    """A0's ``Usage`` for a usage document. On the Claude route (``unit == "usd_list"``) the dollars are the summed
    ``total_cost_usd`` of the composes, and only when every compose's was readable. On the token route there is no
    mapped price (R-D9), so ``usd_micros`` is None and the reservation is debited whole until A0 can state a token
    exposure. A count that could not be read is None, never 0.

    A subscription run settles on ``actions`` (the dream's turns), ``tunnel_bytes`` and ``seconds`` (A0 §2.2), not on
    dollars. ``actions`` and ``seconds`` come from the record when it carries them. ``tunnel_bytes`` stays None: the
    dream runs behind no byte-counting front, and a reservation that sets a byte cap reads as unreadable (its cap is
    debited) rather than as zero."""
    if doc is None:
        return Usage(None, None, None, UNREADABLE_SOURCE)
    source = doc.get("source") if isinstance(doc.get("source"), str) and doc.get("source") else "dream:unknown"
    usd = _count(doc.get("actual_usd_micros")) if doc.get("unit") == "usd_list" and doc.get("readable") is True else None
    return Usage(usd, _count(doc.get("input_tokens")), _count(doc.get("output_tokens")), source,
                 actions=_count(doc.get("turns")), tunnel_bytes=None, seconds=_count(doc.get("seconds")))


def read_usage(occ_dir: Path | str) -> Usage:
    return to_usage(read_doc(occ_dir))


def summary(doc: dict[str, Any] | None) -> dict[str, Any]:
    """The card's ``usage`` block: the figures only, so a card never carries a compose row."""
    if doc is None:
        return {"readable": False, "unit": None, "actual_usd_micros": None, "reserved_usd_micros": None,
                "input_tokens": None, "output_tokens": None, "source": UNREADABLE_SOURCE}
    keys = ("unit", "readable", "actual_usd_micros", "reserved_usd_micros", "input_tokens", "output_tokens", "source")
    return {k: doc.get(k) for k in keys}
