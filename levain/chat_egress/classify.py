"""The chat-egress classifier: one request's verdict, silent / card / wall (design §3.8.5(o), rev 15.11).

`decide(parsed, route, mode)` is a pure function of three inputs and reads nothing else:
    parsed  the ONE `parse.ParsedRequest` of the request (R-1, R15-12-B3)
    route   the ONE keyed-route lookup for the request (R15-18-L1): the launch snapshot's `RouteSnapshot` for
            `parsed.canonical_host`, or None. "Keyed" = the snapshot holds a record for the host, independent of
            scheme and port (R15-23-4, R15-18-Q2); it governs classification everywhere, A11 included.
    mode    GATED or OPEN_NETWORK (R15-5)

The order (the first step that decides, decides):
  1. `parse.walls(parsed)`: R-2 .. R-4's structural walls, both modes, keyed or not. Called FIRST and never
     re-implemented here (head ruling on S2 observation 3).
  2. R-7's non-read clauses (an override header or `_method`, any body, a method outside SAFE_METHODS) (B11 step 2).
  3. R-5's query mark: a listed, backstop-matched or signed-URL-set name in the query, pagination names apart
     (R15-17-6), minus the exemptions, which apply to a READ BY SHAPE to an UNKEYED host only (R15-15-Q3) and are
     voided by any duplicated name (R15-17-7) -> `credential_in_query` (B11 step 3).
  4. R-8: only then may the read-shaped predicate (item 6, S3b's `advert.read_shaped`, passed in) treat a non-read
     as a read, and never a request step 3 marked (B11 step 4).
  5. R-9: gated: marked -> card `credential_in_query`; non-read -> card; read -> silent.
     open network: marked -> wall `credential_in_query` on any host, read or not (R15-17-10, B10 REVERSED); keyed ->
     the method rule kept (A11): a non-read is a card; unkeyed -> forward, silent.

What this module does NOT decide (each is another session's, named so a reader does not look for it here):
header stripping and the strip lists' header half (R-5 strip, S6 `strip.py`); whether a key actually attaches (R-6:
snapshot liveness, S6 `keys.py`; the declared-repo set on an upload-pack exchange, S3b) -- `reattached` here is the
snapshot's (https, host, 443, attach) eligibility, which those AND in; the advertisement predicate and the
upload-pack cap (S3b); the body caps and timeouts (S5b); the card's rendering and sanitisation (S7).

The query-credential lists below are design item 3's, verbatim. ⚠ S6's `strip.py` owns the strip lists and the
backstop (the plan's ownership table); S6 had not landed when S3a was built, so the query half lives here until the
head merges the two into one home (an observation in S3a's handoff). Nothing here is a value: names only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Literal

from levain.chat_egress import parse
from levain.chat_egress.parse import ParsedRequest, QueryParam, fold_name

if TYPE_CHECKING:  # the type only: S6 owns the definition (the plan fixes its shape)
    from levain.chat_egress.keys import RouteSnapshot

GATED = "gated"
OPEN_NETWORK = "open network"
MODES = (GATED, OPEN_NETWORK)

#: R-7 (as (a)): the methods a read may have.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: B2's READ BY SHAPE, for the exemptions: a bodyless, override-free, non-Upgrade GET / HEAD (an OPTIONS cards).
_EXEMPTION_METHODS = frozenset({"GET", "HEAD"})

#: The card's `method` when the overrides do not name ONE method token (two different values, or a value that is
#: not an HTTP method token). The card never guesses which one the origin honours.
AMBIGUOUS_METHOD = "<ambiguous override>"
#: RFC 9110 §5.6.2's token (tchar, ASCII only), checked on the value AS SENT (before any case folding), with a
#: display-length cut of 20: a longer value names no method the card will show.
_METHOD_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,20}")
_ASCII_UPPER = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}

_METHOD_KEY = fold_name("_method")

# --- design §3.8.5(o) item 3 (CRED_STRIP_VERSION = 1), the query half ---------------------------------------------
_CRED_STRIP_QUERY = frozenset(fold_name(n) for n in (
    "access_token", "api_key", "apikey", "key", "token", "auth", "private_token", "client_secret", "password"))
_SIGNED_URL_READ_EXEMPT = frozenset(fold_name(n) for n in (
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-amz-date", "x-amz-expires",
    "x-amz-signedheaders",
    "x-goog-signature", "x-goog-credential", "x-goog-date", "x-goog-expires", "x-goog-signedheaders"))
_PAGINATION_NOT_CREDENTIALS = frozenset(fold_name(n) for n in (
    "page-token", "next-token", "continuation-token", "cursor", "pagetoken", "nextpagetoken"))
#: The backstop (R15-10 A1), over a FOLDED name: a word-boundary match at the word's START, so `passw` reaches
#: `password` and `auth` reaches `authorization` (and over-matches `author`: friction, never a reach). Its exact
#: spelling is pinned by S9's K1 corpus.
_BACKSTOP = re.compile(r"(?<![a-z0-9])(?:auth|token|secret|passw|credential|api-key|signature|session)")
#: R15-13 Q-R15.2-3: `sig` joins the QUERY backstop only, as a whole word.
_QUERY_ONLY_BACKSTOP = re.compile(r"(?<![a-z0-9])sig(?![a-z0-9])")
_SIG_WORDS = re.compile(r"(?<![a-z0-9])(?:sig|signature)(?![a-z0-9])")

AZURE_STORAGE_HOST_SUFFIXES = (
    ".blob.core.windows.net", ".web.core.windows.net", ".dfs.core.windows.net",
    ".file.core.windows.net", ".queue.core.windows.net", ".table.core.windows.net",
    ".blob.storage.azure.net", ".web.storage.azure.net", ".dfs.storage.azure.net",
    ".file.storage.azure.net", ".queue.storage.azure.net", ".table.storage.azure.net")
_AZURE_SAS_REQUIRED = ("sig", "sv", "se", "sp")
_AZURE_SP_LETTERS = frozenset("rl")
_AZURE_ZONE = re.compile(r"z(?:[1-9]|[1-4][0-9]|50)")


@dataclass(frozen=True, slots=True)
class Verdict:
    """One request's classification. Names only, never a value (R15-17-8).

    kind               "silent" (forward), "card" (file a consent card, R-9 / item 5), "wall" (deny)
    why                a wall's reason, or the card's `why` (item 5: `credential_in_query`, `override:<name>`,
                       `_method`, `body`, `method`), None when silent. `credential_in_query` takes precedence on a
                       card (it is what walls the request under open network and what R-8 may never lift); after
                       it, the R-7 clauses in why_all's order
    method             the card's method: the EFFECTIVE method when an override names one (R15-13-Q4), else the
                       request's; AMBIGUOUS_METHOD when overrides do not name one
    keyed              the route snapshot holds a record for the host
    reattached         the canonical host when the snapshot would attach on this route ((https, 443) and attach),
                       else None. Snapshot eligibility only: R-6's liveness check and S3b's declared-repo rule AND in
    why_all            every non-read clause found, in order: override:<name>..., _method, body, method,
                       credential_in_query
    query_names        the folded query names that marked the request credential_in_query (never values)
    override_names     the folded override names present (headers, then `_method`)
    card_header_names  every header name the card shows (item 5: every outbound name), the routing headers R-1
                       strips excluded (R15-13-Q4)
    exemption          "signed_url" / "azure_sas" when an exemption lifted the mark on this request, else None
    tags               K1's counters: signed_url_exempt, azure_sas_exempt, sig_backstop, presigned_v2,
                       credential_in_query_wall"""
    kind: Literal["silent", "card", "wall"]
    why: str | None
    method: str
    keyed: bool
    reattached: str | None
    why_all: tuple[str, ...]
    query_names: tuple[str, ...]
    override_names: tuple[str, ...]
    card_header_names: tuple[str, ...]
    exemption: Literal["signed_url", "azure_sas"] | None
    tags: frozenset[str]


ReadShaped = Callable[[ParsedRequest, "RouteSnapshot | None"], bool]


def _credential_like(folded: str) -> bool:
    """A listed, backstop-matched or signed-URL-set query name; pagination names never (R15-17-6)."""
    if folded in _PAGINATION_NOT_CREDENTIALS:
        return False
    return (folded in _CRED_STRIP_QUERY or folded in _SIGNED_URL_READ_EXEMPT
            or bool(_BACKSTOP.search(folded)) or bool(_QUERY_ONLY_BACKSTOP.search(folded)))


def _has_body(p: ParsedRequest) -> bool:
    """R-7: a `Transfer-Encoding`, or a `Content-Length` other than absent / all `0`. Unreadable counts as a body."""
    if "transfer-encoding" in p.header_names:
        return True
    return any(v.strip() != "0" for v in p.header("content-length"))


def _azure_storage_host(host: str) -> bool:
    """R15-17-7: one account label + a standard suffix, or an account label + `z<N>` (1..50) + a DNS-zone suffix."""
    for suffix in AZURE_STORAGE_HOST_SUFFIXES:
        if not host.endswith(suffix):
            continue
        labels = host[: -len(suffix)].split(".")
        if suffix.endswith(".core.windows.net"):
            return len(labels) == 1 and bool(labels[0])
        return len(labels) == 2 and bool(labels[0]) and bool(_AZURE_ZONE.fullmatch(labels[1]))
    return False


def _azure_sas_read_shape(p: ParsedRequest) -> bool:
    """The Azure SAS read shape's QUERY half (R15-13-Q3, R15-15-Q4, R15-17-7): exactly one each of sig, sv, se, sp
    (duplicates are already excluded by the caller), sp non-empty and made only of `r` and `l`."""
    if not all(k in p.query_keys for k in _AZURE_SAS_REQUIRED):
        return False
    sp = p.query_values("sp")
    return len(sp) == 1 and bool(sp[0]) and set(sp[0]) <= _AZURE_SP_LETTERS


def _effective_method(p: ParsedRequest, overrides: list[str]) -> str:
    if not overrides:
        return p.method
    stripped = [v.strip(" \t") for v in overrides]
    if not all(_METHOD_TOKEN.fullmatch(v) for v in stripped):
        return AMBIGUOUS_METHOD
    values = {v.translate(_ASCII_UPPER) for v in stripped}   # ASCII only: str.upper() maps U+017F to `S`
    if len(values) != 1:
        return AMBIGUOUS_METHOD
    (v,) = values
    return v


def decide(parsed: ParsedRequest, route: "RouteSnapshot | None", mode: str, *,
           form_params: tuple[QueryParam, ...] | None = None,
           read_shaped: ReadShaped | None = None) -> Verdict:
    """The verdict for one request (module docstring for the order).

    form_params  the request's `application/x-www-form-urlencoded` body as `parse.parse_query` read it, when the
                 caller (S5b, after the spool) has it: its `_method` is the fourth override name (R15-13-Q4). The
                 body already makes the request a non-read; the form only sets the card's method.
    read_shaped  item 6's predicate (S3b's `advert.read_shaped`). None: no non-read is read-shaped (fail closed:
                 a card). Never asked about a request the query marked, nor under open network for an unkeyed host
                 (R-8 is skipped there: the request is forwarded anyway)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if route is not None and route.canonical_host != parsed.canonical_host:
        raise ValueError("the route snapshot is for another host: the composer looked up the wrong one")
    keyed = route is not None
    reattached = (parsed.canonical_host if keyed and route.attach and parsed.scheme == "https"
                  and parsed.port == 443 else None)
    card_headers = tuple(n for n in parsed.header_names if n not in parsed.routing_headers)

    def verdict(kind, why, *, method=parsed.method, why_all=(), query_names=(), override_names=(),
                exemption=None, tags=frozenset()):
        return Verdict(kind=kind, why=why, method=method, keyed=keyed, reattached=reattached, why_all=why_all,
                       query_names=query_names, override_names=override_names, card_header_names=card_headers,
                       exemption=exemption, tags=frozenset(tags))

    # 1. the structural walls, parse's, first
    wall = parse.walls(parsed)
    if wall is not None:
        return verdict("wall", wall)

    # 2. R-7's non-read clauses
    why_all: list[str] = []
    override_names: list[str] = []
    override_values: list[str] = []
    for name in parse.OVERRIDE_HEADERS:
        vals = parsed.header(name)
        if vals:
            override_names.append(name)
            override_values += vals
            why_all.append(f"override:{name}")
    method_params = [q for q in parsed.params if q.folded == _METHOD_KEY]
    method_params += [q for q in (form_params or ()) if q.folded == _METHOD_KEY]
    if method_params:
        override_names.append("_method")
        override_values += [q.value for q in method_params]
        why_all.append("_method")
    body = _has_body(parsed)
    if body:
        why_all.append("body")
    if parsed.method not in SAFE_METHODS:
        why_all.append("method")
    nonread = bool(why_all)
    effective = _effective_method(parsed, override_values)

    # 3. R-5's query mark, with the exemptions (unkeyed, read by shape, no duplicated name)
    tags: set[str] = set()
    folded = [q.folded for q in parsed.params]
    credential_names = [n for n in dict.fromkeys(folded) if _credential_like(n)]
    if any(_SIG_WORDS.search(n) for n in credential_names):
        tags.add("sig_backstop")
    if "signature" in folded and ("awsaccesskeyid" in folded or "key-pair-id" in folded):
        tags.add("presigned_v2")
    read_by_shape = parsed.method in _EXEMPTION_METHODS and not nonread
    exemptions_apply = not keyed and read_by_shape and len(set(folded)) == len(folded)
    lifted: set[str] = set()
    exemption = None
    if exemptions_apply:
        signed = {n for n in credential_names if n in _SIGNED_URL_READ_EXEMPT}
        azure = ({"sig"} if parsed.canonical_host and _azure_storage_host(parsed.canonical_host)
                 and _azure_sas_read_shape(parsed) else set())
        lifted = signed | azure
    marking = tuple(n for n in credential_names if n not in lifted)
    if not marking and lifted:
        if lifted & _SIGNED_URL_READ_EXEMPT:
            exemption = "signed_url"
            tags.add("signed_url_exempt")
        if "sig" in lifted:
            exemption = "azure_sas"
            tags.add("azure_sas_exempt")
    if marking:
        why_all.append("credential_in_query")
        exemption = None
        tags -= {"signed_url_exempt", "azure_sas_exempt"}

    common = dict(method=effective, why_all=tuple(why_all), query_names=marking,
                  override_names=tuple(override_names), exemption=exemption)
    if marking:
        if mode == OPEN_NETWORK:
            return verdict("wall", "credential_in_query", tags=tags | {"credential_in_query_wall"}, **common)
        return verdict("card", "credential_in_query", tags=tags, **common)
    if mode == OPEN_NETWORK and not keyed:
        return verdict("silent", None, tags=tags, **common)

    # 4. R-8: the read-shaped list may treat this non-read as a read
    if nonread and read_shaped is not None and read_shaped(parsed, route):
        nonread = False

    # 5. R-9
    if nonread:
        return verdict("card", why_all[0], tags=tags, **common)
    return verdict("silent", None, tags=tags, **common)
