"""Request normalisation and the refusal table, ONE module imported by the front and by keys (CONTRACT §4.7), each
applying it independently: the front to what the slot sent, keys again to the `Q` the front sent it. Nothing here
decodes a percent escape; bytes are compared as sent.

Runs on the root interpreter (3.9): no `match`, no 3.10+ API.
"""
from __future__ import annotations

import hashlib
import re
from typing import Mapping, Optional, Pattern, Sequence

#: refusal name -> HTTP status: CONTRACT §4.7's table with 3b's three names. The front and keys both answer through it.
STATUS = {
    "token-invalid": 401, "token-revoked": 401, "front-mismatch": 401,
    "route-not-allowed": 403, "model-not-allowed": 403, "host-not-allowed": 403, "address-refused": 403,
    "policy-header": 403, "passthrough-for-dispatch": 403, "credential-host-mismatch": 403,
    "template-miss": 404,
    "body-cap": 413,
    "malformed": 400,
    "budget-exhausted": 402, "budget-overrun": 402,
    "upstream-error": 502, "upstream-timeout": 504, "keys-unavailable": 503,
    "upstream-redirect": 502,
    "credential-unavailable": 503,
    "body-json": 400, "body-unbounded": 403, "body-timeout": 408,
}
#: the names that carry a `:<what>` suffix on the wire (R4 §1.6: `body-unbounded:document`); the status is the prefix's.
PARAMETERISED = ("body-unbounded",)
_WHAT = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def status_of(name: str) -> int:
    """A refusal name's status, `KeyError` for a name the table lacks. `body-unbounded:<what>` is `body-unbounded`'s."""
    head, sep, what = name.partition(":")
    if sep:
        if head not in PARAMETERISED or not _WHAT.match(what):
            raise KeyError(name)
        return STATUS[head]
    return STATUS[name]


def is_name(name: str) -> bool:
    try:
        status_of(name)
    except KeyError:
        return False
    return True
REASON = {400: b"Bad Request", 401: b"Unauthorized", 402: b"Payment Required", 403: b"Forbidden",
          404: b"Not Found", 408: b"Request Timeout", 413: b"Payload Too Large", 502: b"Bad Gateway",
          503: b"Service Unavailable", 504: b"Gateway Timeout"}


class Refused(Exception):
    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(name)
        self.name = name
        self.detail = detail

    @property
    def status(self) -> int:
        return status_of(self.name)


def digest_of_token(token: str) -> str:
    """The one token digest: `sha256:` + hex SHA-256 of the token's ASCII bytes (keys' cache key, CONTROL's
    `cap_digest`). A non-ASCII token raises, never digests."""
    return "sha256:" + hashlib.sha256(token.encode("ascii")).hexdigest()


# --- the target ---------------------------------------------------------------------------------------------------

#: one `{seg}` value: never `.` or `..` (checked separately), no `/`.
SEG = re.compile(rb"[A-Za-z0-9._~:-]+\Z")
#: escapes that decode to `.`, `/`, `\`, `%` or a control byte, any case.
_BAD_ESCAPE = re.compile(rb"%(2[eEfF5]|5[cC]|[01][0-9a-fA-F]|7[fF])")


def _ascii_visible(data: bytes) -> bool:
    return all(0x21 <= b <= 0x7E for b in data)


def check_path(path: bytes) -> None:
    """The path after `/r/<route>`: starts with `/`; ASCII visible bytes only; no backslash, `;` or `#`; no escape
    decoding to a refused byte; no `.`, `..` or empty segment (a trailing `/` is an empty last segment, and
    significant, so it never matches a template without one)."""
    if not path.startswith(b"/"):
        raise Refused("template-miss", "path-start")
    if not _ascii_visible(path) or b"\\" in path or b";" in path or b"#" in path:
        raise Refused("template-miss", "path-byte")
    if _BAD_ESCAPE.search(path):
        raise Refused("template-miss", "path-escape")
    segs = path[1:].split(b"/")
    for i, s in enumerate(segs):
        if s in (b".", b".."):
            raise Refused("template-miss", "dot-segment")
        if not s and i != len(segs) - 1:
            raise Refused("template-miss", "empty-segment")


def check_query(query: bytes) -> None:
    """The raw query: refused only for non-ASCII, a raw control byte, a space or `#`. Escapes pass (`%25` included):
    nothing decodes the query as a path, and keys forwards it byte for byte."""
    for b in query:
        if b >= 0x7F or b <= 0x20 or b == 0x23:
            raise Refused("template-miss", "query-byte")


class Template:
    """One route template: a method, literal segments and `{seg}` slots, an optional `?*` (a query allowed), and
    `upgrade` = "websocket" or None."""
    __slots__ = ("method", "path", "segs", "query_ok", "upgrade")

    def __init__(self, method: str, path: str, upgrade: Optional[str] = None) -> None:
        self.method = method
        self.path = path
        self.query_ok = path.endswith("?*")
        body = path[:-2] if self.query_ok else path
        self.segs = tuple(body[1:].split("/"))
        self.upgrade = upgrade

    def matches(self, method: bytes, path: bytes, query: Optional[bytes]) -> bool:
        if method.decode("ascii") != self.method:
            return False
        if query is not None and not self.query_ok:
            return False
        segs = path[1:].split(b"/")
        if len(segs) != len(self.segs):
            return False
        for got, want in zip(segs, self.segs):
            if want == "{seg}":
                if not SEG.match(got) or got in (b".", b".."):
                    return False
            elif got != want.encode("ascii"):
                return False
        return True


def match_template(templates: Sequence[Template], method: bytes, path: bytes,
                   query: Optional[bytes]) -> Template:
    """The first template the request matches, else `template-miss`. `query` is None when the target has no `?`."""
    check_path(path)
    if query is not None:
        check_query(query)
    for t in templates:
        if t.matches(method, path, query):
            return t
    raise Refused("template-miss", "no-template")


# --- headers ------------------------------------------------------------------------------------------------------

HOP_BY_HOP = frozenset({"connection", "keep-alive", "te", "trailer", "upgrade", "transfer-encoding",
                        "proxy-connection", "proxy-authenticate", "proxy-authorization"})
#: never forwarded as a header whatever a policy says (CONTRACT §4.7, design 1.4). A policy naming one is refused at load.
FORBIDDEN = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie",
                       "location", "content-encoding", "transfer-encoding", "content-length", "host",
                       "sec-websocket-extensions", "accept-encoding", "expect", "sec-websocket-accept"}) | HOP_BY_HOP
#: consumed by the front (the token, the body framing, `Expect`) or written by keys alone (`accept-encoding`): never
#: forwarded, and not logged as a drop.
CONSUMED = frozenset({"authorization", "host", "content-length", "transfer-encoding", "expect", "accept-encoding"})
#: the request headers that may repeat; any other name twice is refused.
LIST_HEADERS = frozenset({"accept", "accept-language", "cache-control", "anthropic-beta"})
_NAME = re.compile(r"[a-z0-9!#$%&'*+.^_`|~-]+\Z")
#: the `Connection` options a slot may send on any brokered request; they are dropped, never refused.
CONNECTION_OPTIONS = frozenset({"keep-alive", "close"})


def connection_tokens(value: str) -> list[str]:
    """A `Connection` value as its lowercased options (RFC 9110 §7.6.1: a comma list; empty elements ignored)."""
    return [t.strip().lower() for t in value.split(",") if t.strip()]


def is_forbidden(name: str) -> bool:
    return name in FORBIDDEN or name.startswith("proxy-")


def check_headers(pairs: Sequence[tuple[str, str]], allow: Mapping[str, Pattern[str]], upgrade_ok: bool,
                  strict: bool = False) -> list[tuple[str, str]]:
    """The headers to forward, in order. Names are lowercased. Refused `malformed`: a hop-by-hop name, a duplicate
    of a non-list name. `Connection` is read as a token list and consumed: `keep-alive` and `close` are dropped
    silently (every common client sends one; the front closes after each brokered exchange anyway), `upgrade` passes
    only when `upgrade_ok`, any other token is `malformed`. `Upgrade` passes, consumed, only when `upgrade_ok`
    (the front checks the whole websocket handshake). Then
    allow-list forwarding: a forbidden or consumed name is removed, a name not in `allow` is dropped, a named one
    whose value does not fully match its pattern is `policy-header`. `strict` (keys' re-check of a `Q`): a
    forbidden, consumed or off-list name is `policy-header` instead of being removed."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw_name, value in pairs:
        name = raw_name.lower()
        if not _NAME.match(name):
            raise Refused("malformed", "header-name")
        if name in seen and name not in LIST_HEADERS:
            raise Refused("malformed", "duplicate-header")
        seen.add(name)
        if name in HOP_BY_HOP or name.startswith("proxy-"):
            if strict:
                raise Refused("policy-header", name)
            if name == "connection":
                toks = connection_tokens(value)
                if not toks or not set(toks) <= (CONNECTION_OPTIONS | ({"upgrade"} if upgrade_ok else set())):
                    raise Refused("malformed", "connection")
                continue
            if name == "upgrade" and upgrade_ok:
                continue   # the front judges it with the rest of the websocket handshake
            if name == "transfer-encoding":
                continue   # the body framing, already read by the parser
            raise Refused("malformed", "hop-by-hop")
        if name in CONSUMED or is_forbidden(name):
            if strict:
                raise Refused("policy-header", name)
            continue
        pat = allow.get(name)
        if pat is None:
            if strict:
                raise Refused("policy-header", name)
            continue
        if not pat.fullmatch(value):
            raise Refused("policy-header", name)
        out.append((name, value))
    return out


#: `Bearer <43 base64url chars>` (V15: 32 bytes base64url, unpadded).
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_-]{43})\Z")


def bearer_token(pairs: Sequence[tuple[str, str]]) -> str:
    """Exactly one `authorization` header of the form `Bearer <token>`; anything else is `token-invalid`."""
    vals = [v for n, v in pairs if n.lower() == "authorization"]
    if len(vals) != 1:
        raise Refused("token-invalid", "authorization-count")
    m = _BEARER.match(vals[0])
    if not m:
        raise Refused("token-invalid", "authorization-form")
    return m.group(1)
