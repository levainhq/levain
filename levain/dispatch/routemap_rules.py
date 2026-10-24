"""The checks a `levain-dispatch-routemap/2` body makes of its text fields, its body_policy and its entry counts, in
one place: the registry's `/2` reader (`registry._routemap`) and keys' `/3` reader (`broker.policy.parse_routemap`,
`/3` = `/2` + a price per model) both call these, so `/3` cannot drift from `/2`. Each returns the problem as a line,
or None.

Also the envelope a `levain-dispatch-config/1` item carries beyond its generation (:func:`envelope_problem`): the
producer's `dispatch.control.parse_item` and CONTROL's and keys' `slot.routemaps.check_item` both call it, so the checker
accepts exactly what the producer emits.

Runs on the root interpreter (3.9): `broker.policy` and `slot.routemaps` import it.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from levain.dispatch.policy import path_text_problem

#: the most routes a map, models a route, keys a body_policy, or values a body_policy key, may hold.
ENTRIES_MAX = 256
#: the longest text field.
TEXT_MAX = 1024
#: an item's `prev` from generation 2: the previous item's digest
PREV_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: an item's `issued_at`: `levain.cockpit.consent.utc_stamp`'s one format (a test holds the two equal)
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def envelope_problem(generation: int, prev: Any, issued_at: Any) -> Optional[str]:
    """A config item's `prev` and `issued_at`, given its (already valid) generation: `prev` is "" at generation 1 and
    `sha256:<64 hex>` after it; `issued_at` is a fixed-width UTC stamp naming a real instant."""
    if generation == 1 and prev != "":
        return "prev is empty at generation 1"
    if generation > 1 and (not isinstance(prev, str) or PREV_DIGEST.fullmatch(prev) is None):
        return "prev is sha256:<64 hex>, the previous item's digest, from generation 2"
    if not isinstance(issued_at, str) or STAMP.fullmatch(issued_at) is None:
        return "issued_at is a UTC stamp"
    try:
        datetime.strptime(issued_at, STAMP_FORMAT)
    except ValueError:              # the right shape, an impossible calendar date
        return "issued_at is a UTC stamp"
    return None


def text_problem(value: Any, what: str) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > TEXT_MAX:
        return f"{what} must be 1 to {TEXT_MAX} characters of text"
    return path_text_problem(value, what)


def routes_count_problem(routes: Any) -> Optional[str]:
    if not isinstance(routes, list) or len(routes) > ENTRIES_MAX:
        return f"routes is a list of at most {ENTRIES_MAX} routes"
    return None


def models_count_problem(route: str, models: Any) -> Optional[str]:
    if not isinstance(models, list) or not models or len(models) > ENTRIES_MAX:
        return f"route {route}: models is a non-empty list of at most {ENTRIES_MAX}"
    return None


def body_policy_problem(route: str, bp: Any) -> Optional[str]:
    if not isinstance(bp, dict) or len(bp) > ENTRIES_MAX:
        return f"route {route}: body_policy is a mapping of at most {ENTRIES_MAX} keys"
    for key, value in bp.items():
        problem = text_problem(key, "a body_policy key")
        if problem is not None:
            return f"route {route}: {problem}"
        values = value if isinstance(value, list) else [value]
        if isinstance(value, list) and len(value) > ENTRIES_MAX:
            return f"route {route}: body_policy {key} lists too many values"
        for v in values:
            problem = text_problem(v, f"body_policy {key}")
            if problem is not None:
                return f"route {route}: {problem} (a value is text or a list of text)"
    return None
