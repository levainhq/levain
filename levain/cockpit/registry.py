"""levain.cockpit.registry: the closed facet registry and the ordering registry (design §3.2.1, §3.3).

Both are kernel-owned so that ``overdue_days`` means the same thing on every surface and a row
set has ONE order. Providers never sort and renderers never sort or regroup: an ordering is a
named pure function over rows' facets, and it may assign each row to a kernel-declared group."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from levain.cockpit.results import RowIn

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class CockpitRegistrationError(ValueError):
    """A registration-time packaging bug (unregistered facet or ordering, a gate panel with an
    optional source, a staleness window longer than the refresher's liveness). Surfaced loud."""


# --- facets ------------------------------------------------------------------------------


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _opt(check: Callable[[Any], bool]) -> Callable[[Any], bool]:
    return lambda v: v is None or check(v)


def _enum(*allowed: str) -> Callable[[Any], bool]:
    return lambda v: v in allowed


def _is_date(v: Any) -> bool:
    return isinstance(v, str) and bool(_ISO_DATE.match(v))


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


# name -> (type label, validator). The type label is documentation the diff script can print.
FACETS: dict[str, tuple[str, Callable[[Any], bool]]] = {
    "disposition": ("enum", _enum("loop", "seed", "handoff", "agenda", "note")),
    "domain": ("str", _is_str),
    "tier": ("enum", _enum("hot", "warm", "cold", "parked")),
    "salience": ("int", _is_int),
    "spore_type": ("str", _is_str),
    "age_days": ("int", _is_int),
    "due": ("date|null", _opt(_is_date)),
    "overdue_days": ("int", _is_int),
    "held_until": ("date|null", _opt(_is_date)),
    "handoff_expired": ("bool", _is_bool),
    "last_seen": ("timestamp|null", _opt(_is_str)),
    "source": ("str", _is_str),
    "episode_type": ("str", _is_str),
    "agent": ("str", _is_str),          # who wrote an episode: the entity itself or a federated feed
    "edit_kind": ("str", _is_str),
    "at": ("timestamp", _is_str),
    "repo": ("str", _is_str),
    "owner_install": ("str", _is_str),
    "seat": ("str", _is_str),
    "stopped": ("str", _is_str),
    "exposure_count": ("int", _is_int),
    "undoable": ("bool", _is_bool),
    "crystal_level": ("int", _is_int),
    "permanence": ("str", _is_str),
    "last_activated_on": ("date|null", _opt(_is_str)),
    "tags": ("list[str]", _is_str_list),
    "legacy_meta": ("str", _is_str),
    # K1 job rows (design §6.4 item 3: the consult panel's recent-jobs list). Registered here, in
    # the one closed registry, rather than invented by the provider.
    "job_status": ("enum", _enum("pending", "running", "done", "failed")),
    "verb": ("str", _is_str),
}


class FacetValueError(ValueError):
    """A registered facet carrying a value of the wrong type: bad DATA in one row (a store holding a
    tier outside the enum), not a provider bug. The engine skips and counts the row."""


def validate_facets(facets: dict[str, Any]) -> None:
    """Raise ``CockpitRegistrationError`` for an unregistered facet (a provider bug) and
    ``FacetValueError`` for an ill-typed value (bad data in that row)."""
    for name, value in facets.items():
        entry = FACETS.get(name)
        if entry is None:
            raise CockpitRegistrationError(f"unregistered facet {name!r}")
        if not entry[1](value):
            raise FacetValueError(f"facet {name!r} must be {entry[0]}, got {value!r}")


# --- orderings ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ordering:
    """``key`` is the sort key within a group; ``group`` assigns a group id (or None when the
    ordering declares no groups); ``groups`` lists the group ids in display order with titles;
    ``urgent`` names the groups whose rows are ``band == "now"`` (one definition of urgent per
    row set, owned by its ordering, design §3.2.4)."""

    name: str
    key: Callable[[RowIn, date], tuple]
    group: Callable[[RowIn, date], str] | None = None
    groups: tuple[tuple[str, str], ...] = ()
    urgent: frozenset[str] = frozenset()


KERNEL_PREFIXES = frozenset({"spore", "time", "decision", "exposure", "crystal", "legacy"})

_TIER_ORDER = {"hot": 0, "warm": 1, "cold": 2, "parked": 3}
_DISP_ORDER = {"handoff": 0, "seed": 1, "agenda": 2}


def parse_date(v: Any) -> date | None:
    if not isinstance(v, str):
        return None
    m = _ISO_DATE.match(v)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(0))
    except ValueError:
        return None


def _salience(row: RowIn) -> int:
    v = row.facets.get("salience")
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _seen_ordinal(row: RowIn) -> int:
    d = parse_date(row.facets.get("last_seen"))
    return (d or date.min).toordinal()


def _tray_key(row: RowIn, today: date) -> tuple:
    # Ported from flow's Tray order (RULED Q2, 2026-10-09). Six keys, in order: expired-handoff
    # demotion, dated before undated, disposition (handoff first), tier, salience, seen-recency.
    f = row.facets
    return (
        1 if f.get("handoff_expired") else 0,
        0 if parse_date(f.get("due")) else 1,
        _DISP_ORDER.get(f.get("disposition"), 9),
        _TIER_ORDER.get(f.get("tier"), 9),
        -_salience(row),
        -_seen_ordinal(row),
    )


def _tray_group(row: RowIn, today: date) -> str:
    # Ported from flow's session-open banding (RULED Q2): an explicit past date is OVERDUE whatever
    # the disposition; today's date is TODAY; an undated handoff still inside its claim is TODAY;
    # the rest are ALSO.
    f = row.facets
    d = parse_date(f.get("due"))
    if d and d < today:
        return "overdue"
    if d == today:
        return "today"
    if not d and f.get("disposition") == "handoff" and not f.get("handoff_expired"):
        return "today"
    return "also"


def _tray_order_key(row: RowIn, today: date) -> tuple:
    # OVERDUE is ordered by due date, most overdue first; TODAY and ALSO keep sort_tray's
    # relative order. The group index leads the sort (applied in apply_ordering).
    if _tray_group(row, today) == "overdue":
        d = parse_date(row.facets.get("due")) or today
        return (d.toordinal(),) + _tray_key(row, today)
    return _tray_key(row, today)


def _loops_key(row: RowIn, today: date) -> tuple:
    f = row.facets
    return (_TIER_ORDER.get(f.get("tier"), 9), -_salience(row), -_seen_ordinal(row), row.id)


def _keep_key(row: RowIn, today: date) -> tuple:
    f = row.facets
    return (_TIER_ORDER.get(f.get("tier"), 9), -_salience(row), -_seen_ordinal(row), row.id)


def _time_desc_key(row: RowIn, today: date) -> tuple:
    # newest first: invert the timestamp string character-wise so the key is plain-ascending.
    at = str(row.facets.get("at") or "")
    return (tuple(-ord(c) for c in at), row.id)


def _age_key(row: RowIn, today: date) -> tuple:
    # oldest first; a row with no known age goes last
    v = row.facets.get("age_days")
    return (0, -v, row.id) if _is_int(v) else (1, 0, row.id)


def _decision_key(row: RowIn, today: date) -> tuple:
    d = parse_date(row.facets.get("due"))
    return (0 if d else 1, d.toordinal() if d else 0, row.id)


def _exposure_key(row: RowIn, today: date) -> tuple:
    v = row.facets.get("exposure_count")
    return (-(v if isinstance(v, int) else 0), row.id)


def _crystal_key(row: RowIn, today: date) -> tuple:
    v = row.facets.get("crystal_level")
    return (-(v if isinstance(v, int) else 0), row.id)


ORDERINGS: dict[str, Ordering] = {}


def register_ordering(o: Ordering, *, builtin: bool = False) -> None:
    """Kernel orderings are registered at import. A downstream may register only under its OWN
    prefix (``flow.*``), for its own row sets, never under a kernel prefix and never twice."""
    prefix = o.name.split(".", 1)[0]
    if "." not in o.name:
        raise CockpitRegistrationError(f"ordering {o.name!r} must be <prefix>.<name>")
    if not builtin and prefix in KERNEL_PREFIXES:
        raise CockpitRegistrationError(
            f"ordering {o.name!r}: the {prefix!r} prefix is kernel-owned; register under your own prefix"
        )
    if o.name in ORDERINGS:
        raise CockpitRegistrationError(f"ordering {o.name!r} is already registered")
    ids = [g[0] for g in o.groups]
    if len(set(ids)) != len(ids) or not o.urgent <= set(ids):
        raise CockpitRegistrationError(f"ordering {o.name!r}: bad groups")
    if bool(o.groups) != (o.group is not None):
        raise CockpitRegistrationError(f"ordering {o.name!r}: groups and group() go together")
    ORDERINGS[o.name] = o


register_ordering(
    Ordering(
        "spore.tray", _tray_order_key, _tray_group,
        (("today", "Today"), ("overdue", "Overdue"), ("also", "Also pending")),
        frozenset({"today", "overdue"}),
    ),
    builtin=True,
)
register_ordering(Ordering("spore.loops", _loops_key), builtin=True)
register_ordering(Ordering("spore.keep", _keep_key), builtin=True)
register_ordering(Ordering("spore.age", _age_key), builtin=True)   # the Tray's "by age" view
register_ordering(Ordering("time.desc", _time_desc_key), builtin=True)
register_ordering(Ordering("decision.dated", _decision_key), builtin=True)
register_ordering(Ordering("exposure.desc", _exposure_key), builtin=True)
register_ordering(Ordering("crystal.level", _crystal_key), builtin=True)
# rows a migrated source already ordered (an external panel, design §6.2): one constant key, and the stable
# sort keeps the source's own order
register_ordering(Ordering("legacy.source", lambda row, today: ()), builtin=True)


def apply_ordering(name: str, rows: list[RowIn], today: date) -> list[tuple[RowIn, str | None]]:
    """Order ``rows`` by the named ordering; returns ``(row, group id)`` pairs. A stable sort, so
    rows whose keys tie keep the provider's read order."""
    o = ORDERINGS[name]
    order = {gid: i for i, (gid, _t) in enumerate(o.groups)}
    tagged = [(row, o.group(row, today) if o.group else None) for row in rows]
    tagged.sort(key=lambda p: (order.get(p[1], 99) if p[1] is not None else 0, o.key(p[0], today)))
    return tagged
