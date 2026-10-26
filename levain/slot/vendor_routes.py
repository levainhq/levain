"""The vendor routes the install helper merges into ``L/routes/base.json`` (3c design §4, 3c-9: ``routes-base``;
R4 §1.3: the install helper "ships the vendor table and refreshes it on repair/upgrade").

Beside them the shipped egress allow-list, ``egress-default`` (wfetch DESIGN §3 WF-1 item 5, RULINGS Q2 (a): F3's
default list), the CONNECT hosts every interactive session carries (``supervisor.pick_routes`` takes every ``egress``
route of the map). The operator's own additions are a second egress route, ``egress-operator``, written only by the
install helper's ``routes-egress`` (:func:`set_operator_hosts`); it is never shipped, so the merge keeps it as written.

Claude's ``subscription_passthrough`` route (CONTRACT §4.1; 1026-10 rulings R1-R4) is the CONNECT host list a
``--auth passthrough`` session of the slot's own Claude login may tunnel to. Its hosts are derived from Anthropic's
published Claude Code network requirements, not invented: :data:`CLAUDE_NETWORK_DOC`.

One brokered route per vendor whose models carry a price row in :mod:`levain.broker.vendor_prices`: Anthropic's
Messages API, priced for every Claude row there. The OpenRouter row (``z-ai/glm-5.2``) gets no base route: no
interactive session picks one (``supervisor.VENDOR_HOSTS`` names no OpenRouter host), and dispatch routes come from the
signed route map.
A body's ``models`` fallback list or ``route`` is refused at the check, and on the openai-chat codec (the one whose
route can fall back, ``Codec.checks_answered_model``) a response naming another model is not priced at the checked
model's row (3c-9, ``levain.broker.codec.answered_other``).

The merge is by route id: every shipped id is replaced whole by its shipped entry, every other entry of the map is
kept as written. The operator's route wins in two places (:func:`_shipped_for`, each choice named in the merge's
notes): a host one of the map's own ``egress`` routes already admits is left off ``egress-default``; and (1026-10 R6)
a shipped ``subscription_passthrough`` route is not added when the map already holds the operator's own passthrough
route for the same vendor under another id (a vendor's hosts are ``supervisor.VENDOR_HOSTS``, the supervisor's own
pick; :func:`yielded_routes`), so a merge never adds a second passthrough route for a vendor (``route-ambiguous`` at
every launch). Any other route of the map on a host of an ``egress`` route is a clash, refused. The result is parsed
by :func:`levain.broker.policy.parse_base_map` (keys' own parser) and checked by :func:`connect_host_clashes` before
root writes it, so root never writes a map keys would refuse or a map whose egress hosts the front would refuse in
every session.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Sequence, Tuple

from levain.broker import policy as P
from levain.broker.vendor_prices import VENDOR
from levain.slot.supervisor import VENDOR_HOSTS, _hosts

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


#: the source of :func:`claude_passthrough_route`'s hosts, its "Network access requirements" table
CLAUDE_NETWORK_DOC = "https://code.claude.com/docs/en/network-config"

#: the rows of that table a CLI subscription login and its turns need inside the slot: ``api.anthropic.com`` (Claude
#: API requests), ``claude.ai`` (claude.ai account authentication), ``platform.claude.com`` (OAuth token exchange,
#: refresh and revocation, for claude.ai accounts too). The browser-side sign-in hosts (``claude.com``,
#: ``sso.claude.com``) are opened by the operator's own browser, outside the slot; the telemetry, error-reporting,
#: update, plugin and connector rows are no login's and are not shipped.
CLAUDE_PASSTHROUGH_HOSTS = ("api.anthropic.com", "claude.ai", "platform.claude.com")


def claude_passthrough_route() -> dict:
    """The slot's own Claude login, tunnelled end to end; never a dispatch route (CONTRACT §4.1). It shares
    ``api.anthropic.com`` with :func:`anthropic_route`; a session carries one or the other by its auth (render refuses
    ``auth-routes-mismatch`` otherwise)."""
    return {"route": "claude-subscription", "type": "subscription_passthrough", "hosts": list(CLAUDE_PASSTHROUGH_HOSTS)}


def vendor_routes() -> List[dict]:
    return [anthropic_route(), claude_passthrough_route()]


#: wfetch RULINGS Q2 (a): F3's default list ("github.com, api.github.com, pypi.org, files.pythonhosted.org"), sorted
EGRESS_DEFAULT = "egress-default"
EGRESS_DEFAULT_HOSTS = ("api.github.com", "files.pythonhosted.org", "github.com", "pypi.org")
#: the operator's additions, written by ``routes-egress`` only (:func:`set_operator_hosts`); never shipped
EGRESS_OPERATOR = "egress-operator"


def egress_default_route() -> dict:
    """The shipped egress allow-list: CONNECT ``host:443`` to these names only, through the front."""
    return {"route": EGRESS_DEFAULT, "type": "egress", "hosts": list(EGRESS_DEFAULT_HOSTS)}


def shipped_routes() -> List[dict]:
    """Every route the merge ships, each replaced whole by id: the vendor routes, then ``egress-default``."""
    return [*vendor_routes(), egress_default_route()]


def connect_host_clashes(parsed: Mapping[str, P.RoutePolicy]) -> List[Tuple[str, str, str]]:
    """Each host of an ``egress`` route that another route of the map also names (its ``hosts`` or, brokered, its
    upstream host), as ``(host, the egress route, the other route)``, sorted. Every egress route goes to every
    interactive session, and the front refuses a policy that admits a host by two CONNECT routes or as a CONNECT host
    and a brokered host (``levain.broker.front.load_policy``), so a map with one would have every session refused at
    its front."""
    names: Dict[str, List[str]] = {}
    for rid, pol in parsed.items():
        hs = set(pol.hosts)
        if pol.upstream is not None:
            hs.add(pol.upstream.host)
        for h in hs:
            names.setdefault(h, []).append(rid)
    out = set()
    for rid, pol in parsed.items():
        if pol.type != "egress":
            continue
        for h in pol.hosts:
            out.update((h, rid, other) for other in names[h] if other != rid)
    return sorted(out)


def clash_message(clashes: Sequence[Tuple[str, str, str]], base: str = "base.json") -> str:
    """One refusal line for ``clashes`` (:func:`connect_host_clashes`): each host with the two routes that admit it
    (each pair once), and the remedy: edit the route of the pair the merge does not ship (a shipped one is replaced
    whole by every merge) in ``base``. ``routes-egress`` sets only the ``egress-operator`` route, so it is named only
    where that route is one of the pair."""
    shipped = {r["route"] for r in shipped_routes()}
    pairs = sorted({(h, *sorted((a, b))) for h, a, b in clashes})
    parts = []
    for h, a, b in pairs:
        yours = [r for r in (a, b) if r not in shipped] or [a, b]
        fix = f"edit route {' or '.join(yours)} in {base}"
        if EGRESS_OPERATOR in (a, b):
            fix += " (`install-helper routes-egress` sets egress-operator)"
        parts.append(f"host {h} is on routes {a} and {b}: {fix}")
    return "; ".join(parts)[:400]


class MergeRefused(ValueError):
    """The map on disk cannot be merged (``code`` names why); root leaves it as it is."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _yielded(kept: Sequence[object]) -> Dict[str, List[str]]:
    """For each shipped ``subscription_passthrough`` route: the ids of the operator's own passthrough routes among
    ``kept`` (entries under a non-shipped id) for the same vendor, when there is one. A route is a vendor's when one of
    its hosts is in ``VENDOR_HOSTS`` for that vendor (``supervisor.pick_routes``' test)."""
    theirs = [e for e in kept if type(e) is dict and e.get("type") == "subscription_passthrough"
              and type(e.get("route")) is str]
    out: Dict[str, List[str]] = {}
    for r in vendor_routes():
        if r["type"] != "subscription_passthrough":
            continue
        vendors = [v for v, hs in VENDOR_HOSTS.items() if _hosts(r) & hs]
        mine = sorted({e["route"] for e in theirs for v in vendors if _hosts(e) & VENDOR_HOSTS[v]})
        if mine:
            out[r["route"]] = mine
    return out


def yielded_routes(doc: object) -> Dict[str, List[str]]:
    """For each shipped ``subscription_passthrough`` route :func:`merge` does not add: the ids of the operator's own
    passthrough routes in ``doc`` (the map on disk, parsed; ``None`` = absent) for the same vendor, kept in its place
    (:func:`_yielded`). An entry under a shipped id is not the operator's here, the merge replaces it."""
    if type(doc) is not dict or type(doc.get("routes")) is not list:
        return {}
    ids = {r["route"] for r in shipped_routes()}
    return _yielded([e for e in doc["routes"] if type(e) is dict and type(e.get("route")) is str
                     and e["route"] not in ids])


def _shipped_for(kept: Sequence[object]) -> Tuple[List[dict], List[str]]:
    """``(the shipped routes for a map keeping ``kept``, one note per choice made for the operator)``. The user's route
    wins twice. A host that a kept ``egress`` route already admits is left off ``egress-default`` (every interactive
    session carries every egress route, ``supervisor.pick_routes``, so the host stays reachable everywhere), and an
    ``egress-default`` with no host left is not shipped. A shipped ``subscription_passthrough`` route is not shipped
    when a kept passthrough route is the operator's own for the same vendor (:func:`_yielded`; 1026-10 R6): two would
    make every passthrough launch of that vendor ``route-ambiguous``. A kept passthrough (or brokered) route on an
    ``egress-default`` host does not win: only the sessions that pick it carry it, so trimming would take the host
    from the rest; that clash is left for :func:`connect_host_clashes` to refuse. ``kept`` entries are parsed by the
    caller; a malformed one is skipped here."""
    owner: Dict[str, str] = {}
    for entry in kept:
        if type(entry) is dict and entry.get("type") == "egress" and type(entry.get("hosts")) is list:
            for h in entry["hosts"]:
                if type(h) is str:
                    owner.setdefault(h, entry["route"])
    yielded = _yielded(kept)
    out: List[dict] = []
    notes: List[str] = []
    for r in shipped_routes():
        if r["route"] in yielded:
            theirs = yielded[r["route"]]
            if len(theirs) == 1:
                notes.append(f"route {r['route']} not added: your own {theirs[0]} is kept as the passthrough route "
                             "for its vendor (two would make every passthrough launch route-ambiguous)")
            else:
                notes.append(f"route {r['route']} not added: your routes {', '.join(theirs)} are all passthrough "
                             "routes for its vendor; launches will refuse route-ambiguous until one is removed")
            continue
        if r["route"] == EGRESS_DEFAULT:
            keep = [h for h in r["hosts"] if h not in owner]
            notes.extend(f"host {h} stays on your route {owner[h]}, so {EGRESS_DEFAULT} leaves it off"
                         for h in r["hosts"] if h in owner)
            if not keep:
                continue
            r = {**r, "hosts": keep}
        out.append(r)
    return out, notes


def merge(doc: object) -> Tuple[dict, List[str], List[str]]:
    """``(the merged map, the shipped route ids it holds, notes)``. ``doc`` is the map on disk, parsed (``None`` =
    absent, merged as the empty map). A ``/1`` map is carried to ``/2``: its egress and passthrough entries are the
    same objects in both formats; a ``/1`` brokered entry that is not a vendor id has no price table and is refused
    ``base-map-unmergeable`` (the operator's own brokered route is never rewritten by root). A host the map's own
    egress route admits is left off ``egress-default``, and a shipped passthrough route :func:`yielded_routes` names
    is neither added nor among the ids; one note each (:func:`_shipped_for`). The result is parsed by keys' parser
    and checked by :func:`connect_host_clashes`; a refusal there is ``base-map-unmergeable`` too."""
    if doc is None:
        doc = {"format": BASE_FORMAT, "routes": []}
    try:
        P.parse_base_map(doc)
    except P.PolicyError as e:
        raise MergeRefused("base-map-malformed", str(e)[:200]) from None
    ids = {r["route"] for r in shipped_routes()}
    kept: List[dict] = []
    for entry in doc["routes"]:                       # type: ignore[index]
        if entry["route"] in ids:
            continue
        if doc["format"] != BASE_FORMAT and entry.get("type") == "brokered":   # type: ignore[index]
            raise MergeRefused("base-map-unmergeable", f"route {entry['route']}: a /1 brokered route has no prices")
        kept.append(entry)
    shipped, notes = _shipped_for(kept)
    out = {"format": BASE_FORMAT, "routes": kept + shipped}
    try:
        parsed = P.parse_base_map(out)
    except P.PolicyError as e:
        raise MergeRefused("base-map-unmergeable", str(e)[:200]) from None
    clashes = connect_host_clashes(parsed)
    if clashes:
        raise MergeRefused("base-map-unmergeable", clash_message(clashes))
    return out, sorted(r["route"] for r in shipped), notes


def set_operator_hosts(doc: object, hosts: Sequence[str]) -> dict:
    """The map ``doc`` (on disk, parsed; ``None`` = absent) with its ``egress-operator`` route set to ``hosts``
    (sorted), replaced in place when present, appended when not; an empty ``hosts`` removes it. Every other entry and
    the map's format are kept as written. Refused (:class:`MergeRefused`): ``base-map-missing`` (no map: setup writes
    one), ``base-map-malformed`` (the map does not parse; it is left as it is), ``routes-egress-request:hosts`` (a
    host the policy grammar refuses), ``routes-egress-host-on-route`` (a requested host another route admits,
    :func:`connect_host_clashes`), ``base-map-unmergeable`` (the map's other routes already clash, named as the merge
    names it: this verb never writes a map with a clash)."""
    if doc is None:
        raise MergeRefused("base-map-missing", "no base map: `install-helper setup` (or routes-base) writes one")
    try:
        P.parse_base_map(doc)
    except P.PolicyError as e:
        raise MergeRefused("base-map-malformed", str(e)[:200]) from None
    want = None
    if hosts:
        want = {"route": EGRESS_OPERATOR, "type": "egress", "hosts": sorted(hosts)}
        try:
            P.parse_route(want, source="base")
        except P.PolicyError as e:
            raise MergeRefused("routes-egress-request:hosts", str(e)[:200]) from None
    routes: List[dict] = []
    placed = False
    for entry in doc["routes"]:                       # type: ignore[index]
        if entry["route"] == EGRESS_OPERATOR:
            if want is not None:
                routes.append(want)
            placed = True
            continue
        routes.append(entry)
    if want is not None and not placed:
        routes.append(want)
    out = {"format": doc["format"], "routes": routes}  # type: ignore[index]
    try:
        parsed = P.parse_base_map(out)
    except P.PolicyError as e:
        raise MergeRefused("routes-egress-request:hosts", str(e)[:200]) from None
    clashes = connect_host_clashes(parsed)
    other = [c for c in clashes if EGRESS_OPERATOR not in c[1:]]
    if other:
        raise MergeRefused("base-map-unmergeable", clash_message(other))
    if clashes:
        raise MergeRefused("routes-egress-host-on-route",
                           "; ".join(sorted({f"{h} (on route {b if a == EGRESS_OPERATOR else a})"
                                             for h, a, b in clashes}))[:200])
    return out


def reverted_fields(doc: object, merged: object = None) -> Dict[str, List[str]]:
    """For each entry of ``doc`` (the map on disk, parsed; ``None`` = absent) whose id is a shipped route and which
    :func:`merge` replaces: the top-level fields where it differs from the entry the merge wrote (``merged``, the
    merged map; ``None``: the shipped entries as :func:`shipped_routes` gives them), by route id. An operator's change
    to one of them (a lowered ``max_session_microdollars``) is undone by the merge; this names it. An entry the merge
    dropped (an ``egress-default`` left with no host, a yielded passthrough route) is not named here; the merge's
    notes name it."""
    if type(doc) is not dict or type(doc.get("routes")) is not list:
        return {}
    ids = {r["route"] for r in shipped_routes()}
    if merged is None:
        shipped = {r["route"]: r for r in shipped_routes()}
    else:
        shipped = {r["route"]: r for r in merged["routes"] if r["route"] in ids}  # type: ignore[index]
    out: Dict[str, List[str]] = {}
    for entry in doc["routes"]:
        if type(entry) is not dict or entry.get("route") not in shipped:
            continue
        want = shipped[entry["route"]]
        diff = sorted(k for k in set(entry) | set(want) if entry.get(k) != want.get(k))
        if diff:
            out[entry["route"]] = diff
    return out


__all__ = ["ANTHROPIC_SESSION_CAP", "BASE_FORMAT", "CLAUDE_MODELS", "CLAUDE_NETWORK_DOC", "CLAUDE_PASSTHROUGH_HOSTS",
           "EGRESS_DEFAULT", "EGRESS_DEFAULT_HOSTS", "EGRESS_OPERATOR", "MergeRefused", "anthropic_route",
           "clash_message", "claude_passthrough_route", "connect_host_clashes", "egress_default_route", "merge",
           "reverted_fields", "set_operator_hosts", "shipped_routes", "vendor_routes", "yielded_routes"]
