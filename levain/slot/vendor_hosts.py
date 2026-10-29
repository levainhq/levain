"""Which hosts belong to which vendor, and the hosts a base-map route names.

Kept apart from ``supervisor`` so the root interpreter's import closure does not take the supervisor in.
"""
from __future__ import annotations

from typing import Any, Mapping

#: DECISION: §4.3's route policy carries no vendor, and the base map holds every vendor's brokered and passthrough
#: routes, so a route belongs to a vendor when one of its hosts (``hosts`` + ``upstream.host``) is that vendor's.
VENDOR_HOSTS: Mapping[str, frozenset] = {
    "claude": frozenset({"api.anthropic.com", "console.anthropic.com", "claude.ai", "platform.claude.com"}),
    "codex": frozenset({"api.openai.com", "auth.openai.com", "chatgpt.com"}),
}


def _hosts(route: Mapping[str, Any]) -> set:
    hs = {h for h in route.get("hosts", ()) if type(h) is str} if type(route.get("hosts")) is list else set()
    up = route.get("upstream")
    if type(up) is dict and type(up.get("host")) is str:
        hs.add(up["host"])
    return hs
