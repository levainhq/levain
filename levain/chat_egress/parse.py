"""R-1's one normaliser for a decider request frame, the canonical host, R-2's path rule and the structural walls.

Design §3.8.5(o), "Per request", R-1 .. R-4 (rev 15.11). Every policy of the chat-egress decider reads ONE
representation of a request, the `ParsedRequest` that `parse` returns (row R15-12-B3): header names folded once
(case, and `-` / `_` / `.` equated), query keys parsed once (`&` and `;` separate, percent-decoding applied once),
the target canonicalised once. Strip (R-5), override detection (R-7), the credential backstop (item 3), the card
(item 5) and the record (R-10) take a `ParsedRequest`; none of them reads the frame, the target or the header block
again: this module is the only reader of those (a structural test holds the package to it).

The input is the `req` frame srt 0.0.81's decider protocol sends, as JSON-decoded:
    {"t": "req", "id": int, "method": str, "host": str, "port": int, "hostHeader"?: str, "sni"?: str,
     "path": str, "query"?: str, "headers"?: {lower-case name: [value, ...]}}
srt lower-cases header names but does not fold them; it leaves `Host` out of `headers` (it is `hostHeader`).

Everything here is a pure function of the frame: no I/O, no clock, no mode. The verdict, the keys and the mode live in
the modules that consume a `ParsedRequest`.

Import surface (frozen for the CH-D2 sessions that build on it; a change is a head call):
    parse(frame) -> ParsedRequest          raises ParseError
    walls(parsed) -> str | None            the structural walls R-2 .. R-4, in order
    canonical_host(host) -> str            raises ValueError
    canonical_path(path, method) -> (str | None, str | None)   R-2's canonical form, or None and why it walls `path`
    fold_name(name) -> str
    parse_query(raw) -> tuple[QueryParam, ...]   raises ParseError (also for a form body's `_method`, R-7)
    ParsedRequest, Header, QueryParam, Names, ParseError
    ROUTING_HEADERS, OVERRIDE_HEADERS, WALL_REASONS
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator, Literal, Mapping

from levain.chat_egress import wire

#: The routing headers R-1 strips from EVERY request and never shows on a card (R15-12-B3 as amended by R15-13
#: Q-R15.2-4; B14's expected verdict). Folded spellings.
ROUTING_HEADERS: tuple[str, ...] = ("x-original-url", "x-rewrite-url", "x-forwarded-host")

#: The method-override header names (R-7): NEVER stripped (R15-13 Q-R15.2-4). Folded spellings. The query / form-body
#: key `_method` is the fourth override name; it is a query key, not a header.
OVERRIDE_HEADERS: tuple[str, ...] = ("x-http-method-override", "x-http-method", "x-method-override")

#: The reasons `walls` can return (its docstring gives the order it checks them in).
WALL_REASONS: tuple[str, ...] = ("non_dns_host", "host_mismatch", "path", "use_w2_push", "protocol")

_MAX_HOST = 253
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")
#: Characters IDNA 2003 (the stdlib codec) maps where IDNA 2008 / UTS 46 non-transitional (what a client's URL
#: parser uses) keeps them: one spelling would name two A-labels, so `canonical_host` refuses them.
_IDNA_DEVIATIONS = frozenset("ßς‌‍")
#: WHATWG's "ends in a number" last label (decimal, or `0x` hex, possibly empty): a URL parser reads the host as IPv4.
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset(b"0123456789abcdefABCDEF")
_PRINTABLE_PATH = re.compile(r"/[!-~]*")
_FRAME_KEYS = frozenset({"t", "id", "method", "host", "port", "hostHeader", "sni", "path", "query", "headers"})
_FRAME_REQUIRED = frozenset({"t", "id", "method", "host", "port", "path"})


class ParseError(ValueError):
    """The frame cannot be read as a request. `reason` is one of:
      "frame"  the frame is not srt's `req` shape (a missing or mistyped field, an unknown key);
      "query"  the query has a malformed percent-escape, or decodes to bytes that are not UTF-8.
    The decider is total (R15-12-B8): it catches this and denies that one request."""

    def __init__(self, reason: Literal["frame", "query"], detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


#: The one name fold (lower-case; `_` and `.` read as `-`, srt's `foldHeaderName`), defined once in `wire`.
fold_name = wire.fold_name


class Names:
    """An immutable set of FOLDED names whose lookups fold their argument, so a consumer cannot miss a name by
    spelling it `x_api_key` against a stored `x-api-key`. Iterates the folded names in first-seen order."""

    __slots__ = ("_order", "_set")

    def __init__(self, names: Iterable[str] = ()) -> None:
        order: list[str] = []
        seen: set[str] = set()
        for n in names:
            f = fold_name(n)
            if f not in seen:   # a set beside the order list: O(1) per insert
                seen.add(f)
                order.append(f)
        self._order = tuple(order)
        self._set = frozenset(seen)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and fold_name(name) in self._set

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Names) and self._set == other._set

    def __hash__(self) -> int:
        return hash(self._set)

    def __repr__(self) -> str:
        return f"Names({list(self._order)!r})"

    def matching(self, names: Iterable[str]) -> tuple[str, ...]:
        """The folded forms of `names` that are in this set, in `names`' order, each once."""
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            f = fold_name(n)
            if f in self._set and f not in seen:
                seen.add(f)
                out.append(f)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class Header:
    """One header value as the frame carried it. `name` is srt's lower-cased spelling (never shown on a card as a
    value carrier: names only); `folded` is `fold_name(name)`; `value` is never logged or carded (R-10, item 5)."""
    name: str
    folded: str
    value: str


@dataclass(frozen=True, slots=True)
class QueryParam:
    """One query (or form-body) parameter, decoded once.
    raw_key    the key as spelled on the wire (percent-escapes and `+` intact)
    key        the key decoded once (`+` read as space, then percent-decoded, then UTF-8)
    folded     fold_name(key): what every membership test compares
    value      the value decoded once; NEVER logged, carded or persisted (R15-17-8)
    has_value  whether the part carried an `=` (`?a` against `?a=`)"""
    raw_key: str
    key: str
    folded: str
    value: str
    has_value: bool


@dataclass(frozen=True, slots=True)
class ParsedRequest:
    """The one representation of a decider request (R-1). Immutable; a value, so every consumer reads the same one.

    srt_id          srt's protocol id for this request (the verdict frame's `id`); private to the srt link, never
                    the record's id
    method          as srt validated it
    host            the frame's `host` (srt's canonicalised target host: ASCII, lower-case, no brackets)
    canonical_host  canonical_host(host), or None when the frame host is not a DNS name or an IP literal (walls
                    `non_dns_host`)
    port            the target port
    scheme          "https" iff the frame carries `sni` (the decider's own scheme rule, C-2's second layer)
    sni             the TLS server name, or None on the plain path
    host_header     the `Host` header as sent, or None
    path            the frame's path as srt forwards it (never logged raw: R-10's shape is the decider's)
    canonical_path  R-2's canonical form (unreserved percent-escapes decoded, the rest upper-cased), or None when
                    the path walls `path`
    path_problem    why the path walls ("malformed", "escape", "backslash", "double-slash", "dot-segment"), or None
    query           the raw query string without its `?` ("" when absent); never logged
    params          parse_query(query)
    headers         every header value, in frame order, the wrapper's `x-levain-audit` excluded
    header_names    Names of `headers`
    query_keys      Names of `params`' keys
    request_id      the ONE `x-levain-audit` value when exactly one well-formed one is present, else None (the
                    decider then denies, reason `audit-missing`, item 2)
    routing_headers the folded ROUTING_HEADERS present, in ROUTING_HEADERS order (each to be removed, R-1)"""
    srt_id: int
    method: str
    host: str
    canonical_host: str | None
    port: int
    scheme: Literal["https", "http"]
    sni: str | None
    host_header: str | None
    path: str
    canonical_path: str | None
    path_problem: str | None
    query: str
    params: tuple[QueryParam, ...]
    headers: tuple[Header, ...]
    header_names: Names
    query_keys: Names
    request_id: str | None
    routing_headers: tuple[str, ...]

    def header(self, name: str) -> tuple[str, ...]:
        """Every value of the header `name` (any spelling), in frame order."""
        f = fold_name(name)
        return tuple(h.value for h in self.headers if h.folded == f)

    def query_values(self, key: str) -> tuple[str, ...]:
        """Every decoded value of the query key `key` (any spelling), in order."""
        f = fold_name(key)
        return tuple(q.value for q in self.params if q.folded == f)


def canonical_host(host: str) -> str:
    """THE canonical host (design R-1, R15-21-6), defined once: lower-cased, one trailing dot stripped, in its IDNA
    A-label form. The keyed lookup, the one-live-record rule and R-6's attachment compare hosts by this function only.

    An IP literal (IPv6 with or without brackets) comes back in Python's compressed form. Refused (ValueError): an
    empty name or label, a label over 63 or a name over 253 characters, anything but letters, digits, `-` and `_` in
    a label (a label may not start or end with `-`), a port, a scope id, a name whose last label is a WHATWG number
    but is no IP literal (decimal digits or `0x` hex: a URL parser reads `1.2.3` and `127.0x1` as IPv4 addresses), and
    a non-ASCII name holding a character IDNA 2003 and IDNA 2008 map differently (`ß`, final sigma, ZWJ, ZWNJ),
    looked for AFTER NFKC and lower-casing, so `ẞ` (lower-cases to `ß`) and a word-final `Σ` (to `ς`) are refused
    too: one spelling must never name two hosts."""
    if not isinstance(host, str) or not host:
        raise ValueError("empty host")
    h = host
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
        if "%" in h:
            raise ValueError("scoped address")
        try:
            return str(ipaddress.IPv6Address(h))
        except ValueError as e:
            raise ValueError(f"bad IPv6 literal: {e}") from None
    if "%" in h:
        raise ValueError("scoped address")
    if ":" in h:
        try:
            return str(ipaddress.IPv6Address(h))
        except ValueError:
            raise ValueError("not a host (a port or a bad IPv6 literal)") from None
    try:
        return str(ipaddress.IPv4Address(h))
    except ValueError:
        pass
    if h.endswith("."):
        h = h[:-1]
    if not h:
        raise ValueError("empty host")
    if not h.isascii():
        if any(_IDNA_DEVIATIONS & set(form) for form in _case_forms(h)):
            raise ValueError("an IDNA deviation character: the host has two A-label forms")
        try:
            h = h.encode("idna").decode("ascii")
        except UnicodeError as e:
            raise ValueError(f"not an IDNA name: {e}") from None
    h = h.lower()
    if len(h) > _MAX_HOST:
        raise ValueError("host longer than 253")
    labels = h.split(".")
    for lab in labels:
        if not _LABEL.fullmatch(lab):
            raise ValueError(f"bad label {lab!r}")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        raise ValueError("last label is a number but the host is no IP literal")
    return h


def _case_forms(h: str) -> tuple[str, ...]:
    """`h` as spelled and under each normalisation the codec or a client's UTS 46 mapping would apply before the
    deviation characters matter: NFKC and lower-casing, in both orders. (`casefold` is not one: it maps `ß` to `ss`,
    hiding the deviation.)"""
    nfkc = unicodedata.normalize("NFKC", h)
    return (h, nfkc, nfkc.lower(), unicodedata.normalize("NFKC", h.lower()), h.lower())


def _percent_decode(raw: str) -> str:
    """`+` as space, then each %XX once, then UTF-8 (strict). ParseError("query") on a bad escape or bad UTF-8."""
    b = raw.replace("+", " ").encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x25:   # %
            if i + 2 >= len(b) or b[i + 1] not in _HEX or b[i + 2] not in _HEX:
                raise ParseError("query", "malformed percent-escape")
            out.append(int(b[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(c)
            i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        raise ParseError("query", "not UTF-8 after decoding") from None


def parse_query(raw: str) -> tuple[QueryParam, ...]:
    """THE query-key parse (R-1, B3), also for an `application/x-www-form-urlencoded` body (R-7's `_method`): parts
    separated by `&` or `;`, empty parts skipped, key and value split at the first `=`, each decoded once."""
    params: list[QueryParam] = []
    for part in re.split(r"[&;]", raw):
        if not part:
            continue
        k, eq, v = part.partition("=")
        key = _percent_decode(k)
        params.append(QueryParam(raw_key=k, key=key, folded=fold_name(key), value=_percent_decode(v),
                                 has_value=bool(eq)))
    return tuple(params)


def canonical_path(path: str, method: str) -> tuple[str | None, str | None]:
    """(canonical form, None) or (None, why it walls `path`): R-2, "the path must not change under normalisation".
    Unreserved escapes are decoded (`%2e` is `.`, `%72` is `r`), other escapes upper-cased and kept (`%2F` stays an
    escape: an encoded slash is a resource name on real APIs). Walled: not one printable-ASCII origin-form token, a
    malformed escape, a backslash (raw or `%5C`), a `//` anywhere, a `.` or `..` segment."""
    if path == "*":
        return ("*", None) if method == "OPTIONS" else (None, "malformed")
    if not _PRINTABLE_PATH.fullmatch(path):
        return None, "malformed"
    b = path.encode("ascii")
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x25:
            if i + 2 >= len(b) or b[i + 1] not in _HEX or b[i + 2] not in _HEX:
                return None, "escape"
            v = int(b[i + 1:i + 3], 16)
            if v in _UNRESERVED:
                out.append(v)
            else:
                out += b"%" + b[i + 1:i + 3].upper()
            i += 3
        else:
            out.append(c)
            i += 1
    canon = out.decode("ascii")
    if "\\" in canon or "%5C" in canon:
        return None, "backslash"
    if "//" in canon:
        return None, "double-slash"
    if any(seg in (".", "..") for seg in canon.split("/")):
        return None, "dot-segment"
    return canon, None


def _frame_error(why: str) -> ParseError:
    return ParseError("frame", why)


def parse(frame: Mapping[str, object]) -> ParsedRequest:
    """The one parse of a decider `req` frame (R-1). Strict on the frame's shape (srt's protocol is strict too, so an
    unexpected shape means a different srt, which fails closed): ParseError("frame"). A bad query escape or bad UTF-8:
    ParseError("query"). A path or host problem is NOT an error: it is recorded (`path_problem`, `canonical_host`
    None) and `walls` reports it, so the request still has a parse to record."""
    if not isinstance(frame, Mapping):
        raise _frame_error("not an object")
    keys = set(frame)
    if not _FRAME_REQUIRED <= keys or not keys <= _FRAME_KEYS or frame.get("t") != "req":
        raise _frame_error(f"keys {sorted(keys)}")
    srt_id, method, host, port, path = (frame[k] for k in ("id", "method", "host", "port", "path"))
    if not (type(srt_id) is int and srt_id >= 1) or not (type(port) is int and 1 <= port <= 65535):
        raise _frame_error("id or port")
    if not all(isinstance(x, str) and x for x in (method, host, path)):
        raise _frame_error("method, host or path")
    sni = frame.get("sni")
    host_header = frame.get("hostHeader")
    query = frame.get("query", "")
    for name, val in (("sni", sni), ("hostHeader", host_header)):
        if val is not None and not (isinstance(val, str) and val):
            raise _frame_error(name)
    if not isinstance(query, str):
        raise _frame_error("query")
    raw_headers = frame.get("headers", {})
    if not isinstance(raw_headers, Mapping):
        raise _frame_error("headers")
    headers: list[Header] = []
    audit: list[str] = []
    for name, values in raw_headers.items():
        if not isinstance(name, str) or not name or name != name.lower() \
                or not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
            raise _frame_error("headers")
        folded = fold_name(name)
        if folded == wire.AUDIT_HEADER:
            audit += values
            continue
        headers += [Header(name=name, folded=folded, value=v) for v in values]
    params = parse_query(query)
    try:
        canon_host: str | None = canonical_host(host)
    except ValueError:
        canon_host = None
    canon_path, problem = canonical_path(path, method)
    header_names = Names(h.folded for h in headers)
    return ParsedRequest(
        srt_id=srt_id, method=method, host=host, canonical_host=canon_host, port=port,
        scheme="https" if sni else "http", sni=sni, host_header=host_header, path=path,
        canonical_path=canon_path, path_problem=problem, query=query, params=params, headers=tuple(headers),
        header_names=header_names, query_keys=Names(q.key for q in params),
        request_id=audit[0] if len(audit) == 1 and wire.is_request_id(audit[0]) else None,
        routing_headers=header_names.matching(ROUTING_HEADERS),
    )


def _host_header_mismatch(p: ParsedRequest) -> bool:
    if p.sni is not None:
        try:
            if canonical_host(p.sni) != p.canonical_host:
                return True
        except ValueError:
            return True
    if p.host_header is None:
        return False
    hh = p.host_header.strip()
    port: int | None = None
    if hh.startswith("["):
        end = hh.find("]")
        if end < 0:
            return True
        name, rest = hh[:end + 1], hh[end + 1:]
    else:
        name, sep, rest = hh.partition(":")
        rest = sep + rest
    if rest:
        if not rest.startswith(":") or not rest[1:].isdigit() or not rest[1:].isascii():
            return True
        port = int(rest[1:])
    try:
        if canonical_host(name) != p.canonical_host:
            return True
    except ValueError:
        return True
    if port is None:
        # a Host with no port names the scheme's default port (RFC 9110 7.2), never "whatever port was dialled"
        port = 443 if p.scheme == "https" else 80
    return port != p.port


def _is_receive_pack(p: ParsedRequest) -> bool:
    """R-3 on the canonical target: a last segment `git-receive-pack` (any case, a `;` parameter or one trailing
    slash ignored), or a `service` query value `git-receive-pack` on ANY path (wider than `/info/refs`: a wall that
    over-matches costs friction, never a reach)."""
    path = p.canonical_path or ""
    segs = [s for s in path.split("/") if s]
    if segs and segs[-1].split(";", 1)[0].lower() == "git-receive-pack":
        return True
    return any(v.lower() == "git-receive-pack" for v in p.query_values("service"))


def _is_protocol_switch(p: ParsedRequest) -> bool:
    """R-4: an `Upgrade` header (WebSocket, h2c), an `HTTP2-Settings` header, a `Connection` listing either, or HTTP/2
    prior knowledge (the `PRI` preface method)."""
    if p.method == "PRI" or "upgrade" in p.header_names or "http2-settings" in p.header_names:
        return True
    tokens = {fold_name(t.strip()) for v in p.header("connection") for t in v.split(",")}
    return bool(tokens & {"upgrade", "http2-settings"})


def walls(parsed: ParsedRequest) -> str | None:
    """The structural walls, the first that applies, or None. Mode-independent (gated and `open network` alike) and
    key-independent; they run before any silent verdict and before any card (R9-A1: R-4 precedes R-7 .. R-9):
      protocol       R-1, HTTP/1.1 only: the HTTP/2 prior-knowledge preface (`PRI`), checked first
      non_dns_host   the frame host has no canonical form
      host_mismatch  R-2: the SNI or the `Host` header names another host (or another port)
      path           R-2: the path changes under normalisation (see ParsedRequest.path_problem)
      use_w2_push    R-3: git-receive-pack, on the canonical target, in both modes (R15-16-Q1)
      protocol       R-4: a protocol switch
    The caller (the classifier) runs this FIRST and maps a reason to its wall verdict."""
    if parsed.method == "PRI":
        return "protocol"
    if parsed.canonical_host is None:
        return "non_dns_host"
    if _host_header_mismatch(parsed):
        return "host_mismatch"
    if parsed.canonical_path is None:
        return "path"
    if _is_receive_pack(parsed):
        return "use_w2_push"
    if _is_protocol_switch(parsed):
        return "protocol"
    return None
