"""The checks a `levain-dispatch-routemap/2` body makes of its text fields, its body_policy and its entry counts, in
one place: the registry's `/2` reader (`registry._routemap`) and keys' `/3` reader (`broker.policy.parse_routemap`,
`/3` = `/2` + a price per model) both call these, so `/3` cannot drift from `/2`. Each returns the problem as a line,
or None.

Runs on the root interpreter (3.9): `broker.policy` imports it.
"""
from __future__ import annotations

from typing import Any, Optional

from levain.dispatch.policy import path_text_problem

#: the most routes a map, models a route, keys a body_policy, or values a body_policy key, may hold.
ENTRIES_MAX = 256
#: the longest text field.
TEXT_MAX = 1024


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
