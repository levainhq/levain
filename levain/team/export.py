"""``levain team export``: the ledger as JSON lines, for anneal-memory's team import and for people.

Default: EVERY entry, verbatim, grouped per file in file order, so a consumer can rebuild each file's
``prev -> hash`` chain and resolve every ``supersedes`` target. ``in_force=True`` is a convenience view
(only what is in force, time order) and cannot be chain-verified.
"""
from __future__ import annotations

import json

from . import index as I


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"


def export_lines(ledger: I.Ledger, *, in_force: bool = False) -> list[str]:
    if in_force:
        return [_line(e) for e in ledger.in_force]
    out: list[str] = []
    for f in sorted(ledger.files, key=lambda f: f.rel):
        out.extend(_line(obj) for obj in f.raw)
    return out
