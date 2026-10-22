"""The ONE parser of a `levain-route-policy/1` object (CONTRACT §4.3, design 1.4). The front parses the routes of its
session file with it (`front.load_policy` keeps the file's envelope checks and calls `parse_route`); keys parses its
base map and the dispatch map with it.

Runs on the root interpreter (3.9).
"""
from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from typing import Mapping, Optional, Pattern

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


class PolicyError(ValueError):
    """A policy object is unusable: refused whole."""


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


def _is_inet_aton_spelling(h: str) -> bool:
    try:
        socket.inet_aton(h)
    except OSError:
        return False
    return True


def hosts(route: str, raw: object) -> list[str]:
    """The one host grammar, for CONNECT hosts and a brokered route's `upstream.host` alike: a lowercase IDNA A-label
    name of two or more labels, with no trailing dot and no port. A host that is an address in disguise is refused
    too: a last label of digits only, and any spelling `socket.inet_aton` accepts as IPv4 (hex, octal, short forms
    such as 0x7f.0x1), because the system resolver would turn it into that address without a lookup."""
    if not isinstance(raw, list) or not raw:
        raise PolicyError(f"route {route}: hosts must be a non-empty list")
    out = []
    for h in raw:
        if (not isinstance(h, str) or not _HOSTNAME.match(h) or h.rsplit(".", 1)[1].isdigit()
                or _is_inet_aton_spelling(h)):
            raise PolicyError(f"route {route}: host {h!r} is not a lowercase A-label host name")
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


def parse_route(pol: object, *, source: str) -> RoutePolicy:
    """One `levain-route-policy/1` object. `source` decides the per-map rules: `max_session_microdollars` REQUIRED
    on a base-map brokered route and FORBIDDEN on a dispatch-map one; a dispatch route is brokered with a codec and
    no websocket template; a session file's route (render's copy) carries no `credential`; a dispatch or planner
    session file's brokered route (`dispatch-session`) keeps the dispatch rules."""
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
    max_call = _posint(route, "max_call_microdollars", pol.get("max_call_microdollars"))
    max_sess = pol.get("max_session_microdollars")
    if source in DISPATCH_RULES and max_sess is not None:
        raise PolicyError(f"route {route}: a dispatch route carries no max_session_microdollars")
    if source == "base" and max_sess is None:
        raise PolicyError(f"route {route}: a base-map brokered route needs max_session_microdollars")
    if max_sess is not None:
        max_sess = _posint(route, "max_session_microdollars", max_sess)
    return RoutePolicy(route, typ, hs, up, cred, req["templates"], req["headers"], req["body_cap"],
                       req["body_codec"], resp, max_call, max_sess)
