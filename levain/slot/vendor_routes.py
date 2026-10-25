"""The vendor brokered routes the install helper merges into ``L/routes/base.json`` (3c design §4, 3c-9:
``routes-base``; R4 §1.3: the install helper "ships the vendor table and refreshes it on repair/upgrade").

One route per vendor whose models carry a price row in :mod:`levain.broker.vendor_prices`: Anthropic's Messages API,
priced for every Claude row there. The OpenRouter row (``z-ai/glm-5.2``) gets no base route: no interactive session
picks one (``supervisor.VENDOR_HOSTS`` names no OpenRouter host), dispatch routes come from the signed route map, and
the Diogenes ``models`` / ``route`` fallback finding (1024 carried item) is open until its codec bound lands.

The merge is by route id: every id here is replaced whole by its shipped entry, every other entry of the map is kept
as written. The result is parsed by :func:`levain.broker.policy.parse_base_map` (keys' own parser) before root writes
it, so root never writes a map keys would refuse.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from levain.broker import policy as P
from levain.broker.vendor_prices import VENDOR

#: the format the merge writes (``layout.BASE_ROUTES_FORMAT``; a test holds the two equal)
BASE_FORMAT = "levain-routes-base/2"

#: [judged by lane-3c9b, 10-25, against R4 §1.4 (an overrun stops the session) and the Opus row's worst call
#: (128,000 output tokens at $44/MTok, about $5.63): a cap of $50 holds several concurrent worst-case calls; the head
#: rules the number]
ANTHROPIC_SESSION_CAP = 50_000_000

#: the Claude models the Anthropic route prices: every row of the vendor table whose id names Claude
CLAUDE_MODELS = tuple(sorted(m for m in VENDOR if m.startswith("claude-")))


def anthropic_route() -> dict:
    """The Messages API, brokered: the client's own credential header never passes (``norm.FORBIDDEN``), keys injects
    the operator's ``anthropic`` credential. ``?*`` admits the ``?beta=true`` query Claude Code sends and no query."""
    return {
        "route": "anthropic",
        "type": "brokered",
        "hosts": ["api.anthropic.com"],
        "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": ""},
        "credential": "anthropic",
        "requests": {
            "templates": [{"method": "POST", "path": "/v1/messages?*"}],
            "headers": {
                "content-type": r"application/json(;\s*charset=utf-8)?",
                "accept": r"application/json|text/event-stream",
                "anthropic-version": r"[0-9]{4}-[0-9]{2}-[0-9]{2}",
                "anthropic-beta": r"[a-z0-9-]{1,64}(,\s?[a-z0-9-]{1,64}){0,31}",
            },
            "body_cap": P.BODY_CAP_MAX,
            "body_codec": "anthropic-messages",
        },
        "max_session_microdollars": ANTHROPIC_SESSION_CAP,
        "prices": {m: dict(VENDOR[m]["price"]) for m in CLAUDE_MODELS},
    }


def vendor_routes() -> List[dict]:
    return [anthropic_route()]


class MergeRefused(ValueError):
    """The map on disk cannot be merged (``code`` names why); root leaves it as it is."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def merge(doc: object) -> Tuple[dict, List[str]]:
    """``(the merged map, the route ids replaced or added)``. ``doc`` is the map on disk, parsed (``None`` = absent,
    merged as the empty map). A ``/1`` map is carried to ``/2``: its egress and passthrough entries are the same
    objects in both formats; a ``/1`` brokered entry that is not a vendor id has no price table and is refused
    ``base-map-unmergeable`` (the operator's own brokered route is never rewritten by root). The result is parsed by
    keys' parser; a refusal there is ``base-map-unmergeable`` too."""
    if doc is None:
        doc = {"format": BASE_FORMAT, "routes": []}
    try:
        P.parse_base_map(doc)
    except P.PolicyError as e:
        raise MergeRefused("base-map-malformed", str(e)[:200]) from None
    shipped = vendor_routes()
    ids = {r["route"] for r in shipped}
    kept: List[dict] = []
    for entry in doc["routes"]:                       # type: ignore[index]
        if entry["route"] in ids:
            continue
        if doc["format"] != BASE_FORMAT and entry.get("type") == "brokered":   # type: ignore[index]
            raise MergeRefused("base-map-unmergeable", f"route {entry['route']}: a /1 brokered route has no prices")
        kept.append(entry)
    out = {"format": BASE_FORMAT, "routes": kept + shipped}
    try:
        P.parse_base_map(out)
    except P.PolicyError as e:
        raise MergeRefused("base-map-unmergeable", str(e)[:200]) from None
    return out, sorted(ids)


def reverted_fields(doc: object) -> Dict[str, List[str]]:
    """For each entry of ``doc`` (the map on disk, parsed; ``None`` = absent) whose id is a shipped route and which
    :func:`merge` replaces: the top-level fields where it differs from the shipped entry, by route id. An operator's
    change to one of them (a lowered ``max_session_microdollars``) is undone by the merge; this names it."""
    if type(doc) is not dict or type(doc.get("routes")) is not list:
        return {}
    shipped = {r["route"]: r for r in vendor_routes()}
    out: Dict[str, List[str]] = {}
    for entry in doc["routes"]:
        if type(entry) is not dict or entry.get("route") not in shipped:
            continue
        want = shipped[entry["route"]]
        diff = sorted(k for k in set(entry) | set(want) if entry.get(k) != want.get(k))
        if diff:
            out[entry["route"]] = diff
    return out


__all__ = ["ANTHROPIC_SESSION_CAP", "BASE_FORMAT", "CLAUDE_MODELS", "MergeRefused", "anthropic_route", "merge",
           "reverted_fields", "vendor_routes"]
