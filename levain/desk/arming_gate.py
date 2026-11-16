"""levain.desk.arming_gate: the night desk's arming gate (ND-11a, board levain#42), a pure check, default closed.

No armed night may run an effect line until three things of his are on record: the ruling on ND-7's result, ND-8's
done, and his read of ND-9's table. :func:`arming_refusal` takes an order and the facts and answers ``None`` (nothing
refuses it) or the refusal in words. Each fact is the np id of the band row that records his act; a fact that is
absent, not a string, or not an np id is missing. With no facts, or facts that are not a mapping, every fact is missing:
the default is closed. Each fact is a different act on a different row, so one np id standing for two facts refuses.

The gate opens without the facts on ONE reading only: ``order`` is a mapping whose ``effect_lines`` key is present and
holds an empty ``list`` (an order of judgement lines only, or a ping that says it has no effect line). Everything else
may carry effects and needs the facts: a missing or misspelt key, ``None``, a tuple, a list subclass, any other value, an
order that is not a mapping (a bare list of lines, an unparsed string, ``None`` from a failed lookup), or a mapping
that raises when read.

Not here: wiring this into the effect verb's execution check (the NIGHT_ARMING_CLEARED read, levain#20, after
levain#12), which must also resolve each np id to a band row of the right kind. Nothing in this module reads a store, a
clock or the environment.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["CLOSED_FACTS", "FACTS", "arming_refusal"]

# the three facts, in the order he does them, each with what it records; the gate reads only this tuple
_FACT_WORDS: tuple[tuple[str, str], ...] = (
    ("nd7_ruled", "his ruling on ND-7's result (the phone notice table)"),
    ("nd8_done", "ND-8 done (the night key on the server)"),
    ("nd9_read", "his read of ND-9's disagreement table"),
)

#: the three facts, read-only; a view for callers, never what the gate decides from
FACTS: Mapping[str, str] = MappingProxyType(dict(_FACT_WORDS))

#: the closed state: no fact on record (read-only)
CLOSED_FACTS: Mapping[str, str] = MappingProxyType({})

_NP_ID = re.compile(r"np-[0-9a-f]{8}")
_ABSENT = object()


def _no_effect_lines(order: Any) -> bool:
    """The one open reading: a mapping with ``effect_lines`` present as an empty list. All else is False."""
    if not isinstance(order, Mapping):
        return False
    try:
        lines = order.get("effect_lines", _ABSENT)
    except Exception:  # a mapping that cannot be read cannot be read as having no effects
        return False
    return type(lines) is list and len(lines) == 0


def _np_id(facts: Any, key: str) -> str | None:
    if not isinstance(facts, Mapping):
        return None
    try:
        value = facts.get(key)
    except Exception:
        return None
    return value if type(value) is str and _NP_ID.fullmatch(value) else None


def arming_refusal(order: Any, facts: Any = None) -> str | None:
    """``None`` when ``order`` may run under ``facts``; else why not, naming each missing fact."""
    if _no_effect_lines(order):
        return None
    ids = {key: _np_id(facts, key) for key, _ in _FACT_WORDS}
    missing = [f"{key} ({words}) needs its np id" for key, words in _FACT_WORDS if ids[key] is None]
    if missing:
        return "night not armed for effect lines; missing: " + "; ".join(missing)
    if len(set(ids.values())) != len(ids):
        return "night not armed for effect lines: the same np id is given for more than one fact"
    return None
