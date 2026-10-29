"""The ONE parser of a `levain-route-policy/1` object (CONTRACT §4.3, design 1.4). The front parses the routes of its
session file with it (`front.load_policy` keeps the file's envelope checks and calls `parse_route`); keys parses its
base map and the dispatch map with it.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping, Optional, Pattern, Tuple

from levain.broker import norm

ROUTE_TYPES = ("egress", "subscription_passthrough", "brokered")
CONNECT_TYPES = ("egress", "subscription_passthrough")
BODY_CODECS = ("anthropic-messages", "openai-responses", "openai-chat", "none")
#: where a policy object comes from: the base map, a dispatch map, or a front's session file (render's copy of
#: either, with no credential field). `dispatch-session` is a dispatch or planner session's file: its brokered
#: routes keep the dispatch map's rules, and its egress routes load as in any session file.
SOURCES = ("base", "dispatch", "session", "dispatch-session")
#: the sources whose brokered routes follow the dispatch rules (a codec, no websocket template, no session budget).
DISPATCH_RULES = ("dispatch", "dispatch-session")
#: the sources that are a front's session file (no credential field).
SESSION_FILES = ("session", "dispatch-session")
_ROUTE_ID = re.compile(r"^[a-z0-9-]{1,48}\Z")
_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
#: lowercase IDNA A-label host names with at least two labels; the last label is not all digits (no address literals).
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}\Z){_LABEL}(?:\.{_LABEL})+\Z")
_POLICY_KEYS = frozenset({"route", "type", "hosts", "upstream", "credential", "requests", "responses",
                          "max_call_microdollars", "max_session_microdollars"})
_REQUEST_KEYS = frozenset({"templates", "headers", "body_cap", "body_codec"})
_TEMPLATE_PATH = re.compile(r"(/([A-Za-z0-9._~:-]+|\{seg\}))+/?(\?\*)?\Z")
_METHOD = re.compile(r"[A-Z]{1,16}\Z")
#: the response allow-list when a route has no `responses`.
DEFAULT_RESPONSE_HEADERS = ("content-type", "request-id", "x-request-id", "retry-after")
#: the most a priced route may ask keys to buffer: the Messages API's own request ceiling (Claude API errors page,
#: "Messages API 32 MB", https://platform.claude.com/docs/en/api/errors; r3 §2.2 c step 2, kept by R4 §1.7). Enforced
#: on the priced formats (base `/2`, routemap `/3`); the `/1` readers keep main's behaviour (the pin test).
BODY_CAP_MAX = 32 * 1024 * 1024
BASE_MAP_FORMATS = ("levain-routes-base/1", "levain-routes-base/2")
ROUTEMAP_FORMAT = "levain-dispatch-routemap/3"
#: a price entry's keys (R4 §1.3). Rates are integer microdollars per million tokens; the token counts are tokens.
PRICE_RATES = ("in", "out", "cache_write", "cache_read")
PRICE_TOKENS = ("default_output", "overhead_tokens", "image_tokens")
_PRICE_KEYS = frozenset(PRICE_RATES + PRICE_TOKENS + ("source",))
#: a priced tool's name as `per_use` keys it: the tool type with any `_YYYYMMDD` version suffix removed.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}\Z")
#: the template methods whose requests carry no body. A priced route parses every request's body (its codec reads
#: the model and estimates the call), so a priced route naming one would answer `body-json` on every call: refused.
BODYLESS_METHODS = frozenset({"GET", "HEAD"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z")
#: an upper bound on every integer in a price entry, so no product can leave exact float-free integer arithmetic
#: unreadable: 10**12 microdollars per million tokens is $1M per million tokens.
_PRICE_INT_MAX = 10 ** 12


class PolicyError(ValueError):
    """A policy object is unusable: refused whole."""


class BodyPolicyError(PolicyError):
    """A routemap route's `body_policy` fails `/2`'s validator (a non-text value, the entry caps): refused whole, and
    named `route-invalid:body_policy` at `ROUTE-SUBMIT` like every other body_policy problem (head ruling H2)."""


@dataclass(frozen=True)
class Upstream:
    scheme: str
    host: str
    base_path: str


@dataclass(frozen=True)
class RoutePolicy:
    route: str
    type: str
    hosts: tuple[str, ...]
    upstream: Optional[Upstream] = None
    credential: Optional[str] = None
    templates: tuple[norm.Template, ...] = ()
    headers: Optional[Mapping[str, Pattern[str]]] = None
    body_cap: int = 0
    body_codec: str = "none"
    response_headers: tuple[str, ...] = DEFAULT_RESPONSE_HEADERS
    max_call_microdollars: Optional[int] = None
    max_session_microdollars: Optional[int] = None
    #: model -> its price entry: base `/2` and routemap `/3` only (R4 §1.3). None on every `/1` and session-file read.
    prices: Optional[Mapping[str, "Price"]] = None
    #: a routemap `/3` route's `body_policy` (head ruling H2: keys writes it in as OpenRouter's `provider` object);
    #: None on every base and session-file read.
    body_policy: Optional[Mapping[str, object]] = None


def _is_inet_aton_spelling(h: str) -> bool:
    try:
        socket.inet_aton(h)
    except OSError:
        return False
    return True


def is_host_name(h: object) -> bool:
    """The one host grammar, for CONNECT hosts and a brokered route's `upstream.host` alike: a lowercase IDNA A-label
    name of two or more labels, with no trailing dot and no port. A host that is an address in disguise is refused
    too: a last label of digits only, and any spelling `socket.inet_aton` accepts as IPv4 (hex, octal, short forms
    such as 0x7f.0x1), because the system resolver would turn it into that address without a lookup."""
    return (isinstance(h, str) and _HOSTNAME.match(h) is not None and not h.rsplit(".", 1)[1].isdigit()
            and not _is_inet_aton_spelling(h))


def hosts(route: str, raw: object) -> list[str]:
    """A route's ``hosts`` list: non-empty, every entry passing :func:`is_host_name`, none named twice (the front
    refuses a CONNECT host admitted twice, so a map holding one would fail every session that carries the route)."""
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"route {route}: hosts must be a non-empty list")
    out = []
    for h in raw:
        if not is_host_name(h):
            raise PolicyError(f"route {route}: host {h!r} is not a lowercase A-label host name")
        if h in out:
            raise PolicyError(f"route {route}: host {h} named twice")
        out.append(h)
    return out


def _posint(route: str, key: str, v: object) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise PolicyError(f"route {route}: {key} must be a positive integer")
    return v


def _header_name(route: str, where: str, name: object) -> str:
    if not isinstance(name, str) or name != name.lower() or not re.match(r"[a-z0-9!#$%&'*+.^_`|~-]+\Z", name):
        raise PolicyError(f"route {route}: {where} header name {name!r}")
    if norm.is_forbidden(name):
        raise PolicyError(f"route {route}: {where} names the forbidden header {name}")
    return name


def _templates(route: str, raw: object, source: str) -> tuple[norm.Template, ...]:
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"route {route}: requests.templates must be a non-empty list")
    out = []
    for t in raw:
        if not isinstance(t, dict) or not {"method", "path"} <= set(t) <= {"method", "path", "upgrade"}:
            raise PolicyError(f"route {route}: a template is exactly {{method, path, upgrade?}}")
        method, path, upgrade = t["method"], t["path"], t.get("upgrade")
        if not isinstance(method, str) or not _METHOD.match(method):
            raise PolicyError(f"route {route}: template method {method!r}")
        if not isinstance(path, str) or not _TEMPLATE_PATH.match(path) or "/./" in path + "/" or "/../" in path + "/":
            raise PolicyError(f"route {route}: template path {path!r}")
        if upgrade is not None:
            if upgrade != "websocket" or method != "GET":
                raise PolicyError(f"route {route}: only a GET template may carry upgrade: websocket")
            if source in DISPATCH_RULES:
                raise PolicyError(f"route {route}: a dispatch route carries no websocket template")
        out.append(norm.Template(method, path, upgrade))
    return tuple(out)


def _requests(route: str, raw: object, source: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != _REQUEST_KEYS:
        raise PolicyError(f"route {route}: requests must be exactly {sorted(_REQUEST_KEYS)}")
    headers = raw["headers"]
    if not isinstance(headers, dict):
        raise PolicyError(f"route {route}: requests.headers must be an object")
    pats: dict[str, Pattern[str]] = {}
    for name, pat in headers.items():
        _header_name(route, "requests", name)
        if not isinstance(pat, str):
            raise PolicyError(f"route {route}: requests.headers.{name} must be a pattern")
        try:
            pats[name] = re.compile(pat)
        except Exception as e:   # re.error, and the compiler's own limits: OverflowError (a huge repetition count),
            # RecursionError (deep nesting). Every compile failure is the policy's, never a crash.
            raise PolicyError(f"route {route}: requests.headers.{name}: {e}") from None
    if raw["body_codec"] not in BODY_CODECS:
        raise PolicyError(f"route {route}: body_codec {raw['body_codec']!r}")
    if source in DISPATCH_RULES and raw["body_codec"] == "none":
        raise PolicyError(f"route {route}: a dispatch route needs a body_codec")
    return {"templates": _templates(route, raw["templates"], source), "headers": pats,
            "body_cap": _posint(route, "requests.body_cap", raw["body_cap"]), "body_codec": raw["body_codec"]}


def _upstream(route: str, raw: object) -> Upstream:
    if not isinstance(raw, dict) or set(raw) != {"scheme", "host", "base_path"}:
        raise PolicyError(f"route {route}: upstream must be exactly {{scheme, host, base_path}}")
    host = hosts(route, [raw["host"]])[0]
    if raw["scheme"] != "https":
        raise PolicyError(f"route {route}: upstream scheme must be https")
    bp = raw["base_path"]
    if not isinstance(bp, str) or (bp and (not bp.startswith("/") or bp.endswith("/"))):
        raise PolicyError(f"route {route}: base_path must be empty or start with / and not end with /")
    if bp:
        try:
            norm.check_path(bp.encode("ascii"))
        except (norm.Refused, UnicodeEncodeError):
            raise PolicyError(f"route {route}: base_path {bp!r}") from None
    return Upstream("https", host, bp)


def parse_route(pol: object, *, source: str, per_call: bool = True) -> RoutePolicy:
    """One `levain-route-policy/1` object. `source` decides the per-map rules: `max_session_microdollars` REQUIRED
    on a base-map brokered route and FORBIDDEN on a dispatch-map one; a dispatch route is brokered with a codec and
    no websocket template; a session file's route (render's copy) carries no `credential`; a dispatch or planner
    session file's brokered route (`dispatch-session`) keeps the dispatch rules.

    `per_call=False` is the priced formats' reading (base `/2`, routemap `/3`, R4 §1.2): `max_call_microdollars` is
    FORBIDDEN and `body_cap` is at most `BODY_CAP_MAX`. The default is main's `/1` reading, unchanged."""
    if source not in SOURCES:
        raise ValueError(source)
    if not isinstance(pol, dict):
        raise PolicyError("a route must be an object")
    route = pol.get("route")
    if not isinstance(route, str) or not _ROUTE_ID.match(route):
        raise PolicyError(f"bad route id {route!r}")
    if source in SESSION_FILES and "credential" in pol:
        raise PolicyError(f"route {route}: the front's policy carries no credential field")
    unknown = set(pol) - _POLICY_KEYS
    if unknown:
        raise PolicyError(f"route {route}: unknown keys {sorted(unknown)}")
    typ = pol.get("type")
    if typ not in ROUTE_TYPES:
        raise PolicyError(f"route {route}: type {typ!r} not in {ROUTE_TYPES}")
    if typ != "brokered":
        if source == "dispatch":
            raise PolicyError(f"route {route}: a dispatch route must be brokered")
        extra = set(pol) - {"route", "type", "hosts"}
        if extra:
            raise PolicyError(f"route {route}: a {typ} route carries only route, type, hosts")
        return RoutePolicy(route, typ, tuple(hosts(route, pol.get("hosts"))))
    up = _upstream(route, pol.get("upstream"))
    hs = tuple(hosts(route, pol["hosts"])) if "hosts" in pol else ()
    req = _requests(route, pol.get("requests"), source)
    cred = pol.get("credential")
    if cred is not None and (not isinstance(cred, str) or not _ROUTE_ID.match(cred)):
        raise PolicyError(f"route {route}: credential name {cred!r}")
    resp = DEFAULT_RESPONSE_HEADERS
    if "responses" in pol:
        r = pol["responses"]
        if not isinstance(r, dict) or set(r) != {"headers"} or not isinstance(r["headers"], list):
            raise PolicyError(f"route {route}: responses must be exactly {{headers: [names]}}")
        resp = tuple(_header_name(route, "responses", n) for n in r["headers"])
    if per_call:
        max_call: Optional[int] = _posint(route, "max_call_microdollars", pol.get("max_call_microdollars"))
    else:
        if "max_call_microdollars" in pol:
            raise PolicyError(f"route {route}: a priced route carries no max_call_microdollars")
        if req["body_cap"] > BODY_CAP_MAX:
            raise PolicyError(f"route {route}: requests.body_cap is above BODY_CAP_MAX")
        max_call = None
    max_sess = pol.get("max_session_microdollars")
    if source in DISPATCH_RULES and max_sess is not None:
        raise PolicyError(f"route {route}: a dispatch route carries no max_session_microdollars")
    if source == "base" and max_sess is None:
        raise PolicyError(f"route {route}: a base-map brokered route needs max_session_microdollars")
    if max_sess is not None:
        max_sess = _posint(route, "max_session_microdollars", max_sess)
    return RoutePolicy(route, typ, hs, up, cred, req["templates"], req["headers"], req["body_cap"],
                       req["body_codec"], resp, max_call, max_sess)


# --- the price entry and the priced formats (R4 §1.3, §1.6) --------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """One model's price entry. Each rate is the HIGHEST the model can be billed at over every tier and service mode
    its provider lists, and `source` the pricing URL that choice was read from (R4 §1.3)."""
    in_: int
    out: int
    cache_write: int
    cache_read: int
    default_output: int
    overhead_tokens: int
    image_tokens: int
    #: tool name -> microdollars per use, read-only; left out of the hash (a Price hashes on its rates and source).
    per_use: Mapping[str, int] = field(hash=False)
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_use", MappingProxyType(dict(self.per_use)))


def _price_int(where: str, key: str, v: object, *, positive: bool) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < (1 if positive else 0) or v > _PRICE_INT_MAX:
        raise PolicyError(f"{where}: price {key} must be an integer in [{1 if positive else 0}, {_PRICE_INT_MAX}]")
    return v


def parse_price(where: str, raw: object) -> Price:
    """One price entry `{in, out, cache_write, cache_read, default_output, overhead_tokens, image_tokens, per_use?,
    source}`. `in`, `out`, `default_output` and `image_tokens` are positive; the cache rates and `overhead_tokens` may
    be 0. `per_use` maps a tool name to integer microdollars per use. `source` is an https URL."""
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: a price entry must be an object")
    keys = set(raw)
    if not _PRICE_KEYS <= keys <= _PRICE_KEYS | {"per_use"}:
        raise PolicyError(f"{where}: a price entry is exactly {sorted(_PRICE_KEYS)} + per_use?")
    vals = {k: _price_int(where, k, raw[k], positive=k in ("in", "out", "default_output", "image_tokens"))
            for k in PRICE_RATES + PRICE_TOKENS}
    per_use: dict[str, int] = {}
    if "per_use" in raw:
        pu = raw["per_use"]
        if not isinstance(pu, dict):
            raise PolicyError(f"{where}: price per_use must be an object")
        for tool, v in pu.items():
            if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
                raise PolicyError(f"{where}: price per_use tool name {tool!r}")
            per_use[tool] = _price_int(where, f"per_use.{tool}", v, positive=True)
    src = raw["source"]
    if not isinstance(src, str) or not src.startswith("https://") or len(src) > 512 or any(c.isspace() for c in src):
        raise PolicyError(f"{where}: price source must be an https URL")
    return Price(vals["in"], vals["out"], vals["cache_write"], vals["cache_read"], vals["default_output"],
                 vals["overhead_tokens"], vals["image_tokens"], per_use, src)


def _priced_templates(pol: RoutePolicy) -> None:
    """A priced route (base `/2`, routemap `/3`) names no template whose method carries no body."""
    for t in pol.templates:
        if t.method in BODYLESS_METHODS:
            raise PolicyError(f"route {pol.route}: a priced route's template {t.method} {t.path} carries no body")


def parse_prices(route: str, raw: object) -> dict:
    """A `prices: {<model>: <entry>}` table (base `/2`)."""
    if not isinstance(raw, dict):
        raise PolicyError(f"route {route}: prices must be an object")
    out: dict[str, Price] = {}
    for model, entry in raw.items():
        if not isinstance(model, str) or not _MODEL_ID.match(model):
            raise PolicyError(f"route {route}: price model id {model!r}")
        out[model] = parse_price(f"route {route} model {model}", entry)
    return out


def parse_base_map(doc: object) -> dict:
    """A base route map, `/1` or `/2`, as `{route id: RoutePolicy}`; any defect refuses the map whole.

    `/1` (main's): each entry is a policy object read by `parse_route(source="base")` exactly as on main.
    `/2` (R4 §1.3): each entry is a policy object; a `brokered` one also carries `prices` (required) and no
    `max_call_microdollars`, and its `body_cap` is at most `BODY_CAP_MAX`. `prices` is refused on any other type."""
    if not isinstance(doc, dict) or doc.get("format") not in BASE_MAP_FORMATS or set(doc) != {"format", "routes"} \
            or not isinstance(doc["routes"], list):
        raise PolicyError("a base map is exactly {format: levain-routes-base/1|2, routes: [..]}")
    v2 = doc["format"] == BASE_MAP_FORMATS[1]
    parsed: dict[str, RoutePolicy] = {}
    for entry in doc["routes"]:
        if v2:
            if not isinstance(entry, dict):
                raise PolicyError("a route must be an object")
            rest = {k: v for k, v in entry.items() if k != "prices"}
            pol = parse_route(rest, source="base", per_call=False)
            if pol.type == "brokered":
                if pol.body_codec == "none":
                    # a priced route is metered, and `none` has no codec to parse or estimate the body with
                    raise PolicyError(f"route {pol.route}: a base /2 brokered route needs a body_codec")
                if "prices" not in entry:
                    raise PolicyError(f"route {pol.route}: a base /2 brokered route needs prices")
                _priced_templates(pol)
                pol = replace(pol, prices=parse_prices(pol.route, entry["prices"]))
            elif "prices" in entry:
                raise PolicyError(f"route {pol.route}: only a brokered route carries prices")
        else:
            pol = parse_route(entry, source="base")
        if pol.route in parsed:
            raise PolicyError(f"route {pol.route}: named twice")
        parsed[pol.route] = pol
    return parsed


@dataclass(frozen=True)
class DispatchRoute:
    """One `levain-dispatch-routemap/3` route: routemap `/2`'s entry with a `price` on every model (R4 §1.6).
    `policy.prices` maps each `requested` model to its price; `backends` maps it to its backend id."""
    policy: RoutePolicy
    provider: str
    backends: Mapping[str, str]
    body_policy: Mapping[str, object]
    retention_claim: str


_ROUTEMAP_ROUTE_KEYS = frozenset({"policy", "provider", "models", "body_policy", "retention_claim"})


def parse_routemap(body: object) -> list:
    """The BODY of a `routemap` config item at `levain-dispatch-routemap/3` = `/2` (1019-dispatch/SCHEMAS.md §1b)
    + `price` per model − `max_call_microdollars`. Signature, store and generation are CONTROL's (3c-4); this reads
    the body. The frozen `/2` is not read here: a `/2` body is refused (`format`). `/2`'s own checks (the text rule on
    each text field, a body_policy value is text or a list of text, the entry caps) are `/2`'s validators in
    `levain.dispatch.routemap_rules`, called here, then the price on each model."""
    # imported here, not at the top: only the routemap reader needs the text rule, and the front never reads a routemap.
    from levain.dispatch import routemap_rules as rules
    if not isinstance(body, dict) or set(body) != {"format", "routes"} or body["format"] != ROUTEMAP_FORMAT \
            or not isinstance(body["routes"], list):
        raise PolicyError(f"a routemap body is exactly {{format: {ROUTEMAP_FORMAT}, routes: [..]}}")
    problem = rules.routes_count_problem(body["routes"])
    if problem is not None:
        raise PolicyError(problem)
    out: list[DispatchRoute] = []
    seen: set[str] = set()
    for r in body["routes"]:
        if not isinstance(r, dict) or set(r) != _ROUTEMAP_ROUTE_KEYS:
            raise PolicyError(f"a routemap route is exactly {sorted(_ROUTEMAP_ROUTE_KEYS)}")
        pol = parse_route(r["policy"], source="dispatch", per_call=False)
        _priced_templates(pol)
        if pol.route in seen:
            raise PolicyError(f"route {pol.route}: named twice")
        seen.add(pol.route)
        for name in ("provider", "retention_claim"):
            problem = rules.text_problem(r[name], f"a route's {name}")
            if problem is not None:
                raise PolicyError(f"route {pol.route}: {problem}")
        problem = rules.body_policy_problem(pol.route, r["body_policy"])
        if problem is not None:
            raise BodyPolicyError(problem)
        problem = rules.models_count_problem(pol.route, r["models"])
        if problem is not None:
            raise PolicyError(problem)
        models = r["models"]
        prices: dict[str, Price] = {}
        backends: dict[str, str] = {}
        for m in models:
            if not isinstance(m, dict) or set(m) != {"requested", "backend", "price"}:
                raise PolicyError(f"route {pol.route}: a model is exactly requested, backend, price")
            req, back = m["requested"], m["backend"]
            for name, v in (("requested", req), ("backend", back)):
                problem = rules.text_problem(v, f"a model's {name}")
                if problem is not None:
                    raise PolicyError(f"route {pol.route}: {problem}")
                if not _MODEL_ID.match(v):
                    raise PolicyError(f"route {pol.route}: model id {v!r}")
            if req in prices:
                raise PolicyError(f"route {pol.route}: model {req} named twice")
            prices[req] = parse_price(f"route {pol.route} model {req}", m["price"])
            backends[req] = back
        out.append(DispatchRoute(replace(pol, prices=prices, body_policy=dict(r["body_policy"])), r["provider"],
                                 backends, r["body_policy"], r["retention_claim"]))
    return out


#: OpenRouter's API host: a route is an OpenRouter route by its pinned upstream host, never a label (R4 §1.5).
OPENROUTER_HOST = "openrouter.ai"
#: head ruling H2: a `body_policy` key -> (OpenRouter's `provider` field, the policy's allowed values -> the field's
#: value). `provider_only` is a list of provider slugs, any non-empty text; the other three are fixed words.
BODY_POLICY_FIELDS: Mapping[str, Tuple[str, Optional[Mapping[str, object]]]] = {
    "provider_only": ("only", None),
    "allow_fallbacks": ("allow_fallbacks", {"yes": True, "no": False}),
    "zdr": ("zdr", {"yes": True, "no": False}),
    "data_collection": ("data_collection", {"allow": "allow", "deny": "deny"}),
}


def body_policy_problem(pol: RoutePolicy, body_policy: object) -> Optional[str]:
    """Head ruling H2's accept-time rule (`ROUTE-SUBMIT` refuses `route-invalid:body_policy`): a non-empty
    `body_policy` only on an OpenRouter route; on one, only the four keys of :data:`BODY_POLICY_FIELDS`, each with a
    value in its set (`provider_only` a list of text). Never judged at request time."""
    if not isinstance(body_policy, Mapping):
        return f"route {pol.route}: body_policy is an object"
    if not body_policy:
        return None
    if pol.upstream is None or pol.upstream.host != OPENROUTER_HOST:
        return f"route {pol.route}: a body_policy is written only for an OpenRouter route"
    for k, v in body_policy.items():
        if k not in BODY_POLICY_FIELDS:
            return f"route {pol.route}: body_policy key {k!r}"
        values = BODY_POLICY_FIELDS[k][1]
        if values is None:
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                return f"route {pol.route}: body_policy {k} is a list of provider slugs"
        elif not isinstance(v, str) or v not in values:
            return f"route {pol.route}: body_policy {k} is one of {sorted(values)}"
    return None


def provider_object(body_policy: Mapping[str, object]) -> dict:
    """The OpenRouter `provider` object a checked `body_policy` maps to (head ruling H2)."""
    out: dict = {}
    for k, v in body_policy.items():
        field, values = BODY_POLICY_FIELDS[k]
        out[field] = list(v) if values is None else values[v]   # type: ignore[index, arg-type]
    return out
