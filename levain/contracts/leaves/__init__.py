"""The global leaf registry (F8): every frozen ``(leaf, v)``, the log kinds that accept it, and its field validator.

A type is registered in the same commit as its validator, never before (S1-a HEAD_RULINGS #7): an entry without one
would pass ``canon.check_leaf`` on framing alone. Provisional shapes (S1_DESIGN §2) are never here, so a released
build refuses to write or ingest them. ``canon.check_leaf`` reads this registry by default.
"""
from __future__ import annotations

from ..canon import LeafType
from . import team

REGISTRY: dict[tuple[str, int], LeafType] = {
    (team.LEAF, team.VERSION): LeafType(frozenset({team.LOG_KIND}), team.check_line_fields),
}
