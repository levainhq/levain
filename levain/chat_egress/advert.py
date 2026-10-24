"""The one read-shaped exception of 1.0: git's `git-upload-pack` fetch negotiation (design §3.8.5(o) item 6, rev 15.11).

A `POST <prefix>/git-upload-pack` is READ-SHAPED (R-8 may treat it as a read) only when ALL of:
  * its shape: method POST; R-2's canonical path ends `/git-upload-pack` (exact case, no trailing slash or `;param`);
    exactly one `Content-Type`, media type `application/x-git-upload-pack-request`; an `Accept` naming
    `application/x-git-upload-pack-result` (R15-10-A12); no method-override header and no `_method` query key (an
    override names another method, and R-8 lifts R-7's clauses, so the shape refuses it);
  * an ADVERTISEMENT for its (scheme, canonical host, port, prefix) is in this launch's advertisement set
    (R15-17-1, the predicate R15-21-5 / R15-23-1 below), as the decider has DRAINED it at this decision (R15-19-1:
    the drain is the decider's; an observation not yet drained is absent, a card);
  * its body was spooled and did not run past UPLOAD_PACK_BODY_CAP decoded bytes (srt's `need_body` `cut` flag,
    R15-17-2 / R15-19-2 / R15-19-3). The body is never parsed: no pkt-line is read (R15-15-Q1).

The advertisement predicate is written ONCE, here (`is_advertisement`): status 200; the reply's `Content-Type` as
srt's normalised response headers give it (one string or none: no multiplicity clause, R15-23-1); its media type,
parameters ignored, equal ASCII case-insensitively to `application/x-git-upload-pack-advertisement`; to a `GET
<prefix>/info/refs` whose `service` value, through R-1's folded query parse, is exactly `git-upload-pack`; the tuple
compared by string equality on R-2's canonical prefix and R-1's canonical host.

The set (`AdvertisementSet`): per launch, at most 256 entries, oldest-first eviction by LAST insertion; re-inserting
a tuple that passed the predicate refreshes it to newest; a reply that fails the predicate neither refreshes nor
removes (R15-24-3, R15-26-1). Only the enum `content_type_class` is ever stored about a response (R15-21-3).

Keys (R15-17-1): `key_allowed` narrows a keyed route's attach on a GIT EXCHANGE (a path ending `/info/refs` or
`/git-upload-pack`, any method or query) to the route's declared repo set; it ANDs into classify's `reattached`
and S6's liveness. A non-git request keeps the snapshot's rule.

Order (B11) is classify's: walls, R-7, R-5's query mark, then this predicate (never asked about a marked request).
The decider binds the launch state: `functools.partial(read_shaped, adverts=<the set>, body_cut=<spool result>)`.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from levain.chat_egress import parse
from levain.chat_egress.parse import ParsedRequest, fold_name

if TYPE_CHECKING:  # the type only: S6 owns the definition (the plan fixes its shape)
    from levain.chat_egress.keys import RouteSnapshot

READ_SHAPED_VERSION = 1                 # in containment_digest
UPLOAD_PACK_BODY_CAP = 2 * 1024 * 1024  # R15-19-3: 2 MiB DECODED entity bytes (srt's need_body max), a bound
ADVERTISEMENT_SET_CAP = 256             # R15-21-5 / R15-24-3

REQUEST_TYPE = "application/x-git-upload-pack-request"
RESULT_TYPE = "application/x-git-upload-pack-result"
ADVERTISEMENT_TYPE = "application/x-git-upload-pack-advertisement"
UPLOAD_PACK_SUFFIX = "/git-upload-pack"
DISCOVERY_SUFFIX = "/info/refs"
SERVICE = "git-upload-pack"

#: R15-21-3: the only thing ever stored about a response's Content-Type.
CT_ADVERTISEMENT = "git-upload-pack-advertisement"
CT_OTHER = "other"
CT_ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class ReadShape:
    method: str
    path_suffix: str
    content_type: str
    accept: str
    advertised: str
    body_cap: int


READ_SHAPED = (
    ReadShape(method="POST", path_suffix=UPLOAD_PACK_SUFFIX, content_type=REQUEST_TYPE, accept=RESULT_TYPE,
              advertised=ADVERTISEMENT_TYPE, body_cap=UPLOAD_PACK_BODY_CAP),
)

AdvertKey = tuple[str, str, int, str]   # (scheme, canonical host, port, canonical prefix)
Disposition = Literal["not_upload_pack", "shape", "no_advertisement", "unspooled", "over_cap", "read_shaped"]

_OWS = " \t"
_SERVICE_KEY = fold_name("service")
_METHOD_KEY = fold_name("_method")


def _ascii_lower(s: str) -> str:
    """ASCII-only case folding: str.lower() also folds U+212A KELVIN SIGN to `k`, which is not ASCII-insensitive."""
    return s.translate(_ASCII_LOWER)


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def media_type(value: str | None) -> str | None:
    """A Content-Type's media type, parameters dropped, ASCII lower-cased; None when absent or empty."""
    if value is None:
        return None
    mt = value.partition(";")[0].strip(_OWS)   # a media type's parameters, never a query
    return _ascii_lower(mt) or None


def content_type_class(content_type: str | None) -> str:
    """R15-21-3's enum: `git-upload-pack-advertisement`, `other` or `absent`. Never the raw value."""
    if content_type is None:
        return CT_ABSENT
    return CT_ADVERTISEMENT if media_type(content_type) == ADVERTISEMENT_TYPE else CT_OTHER


def _key(parsed: ParsedRequest, suffix: str) -> AdvertKey | None:
    path = parsed.canonical_path
    if parsed.canonical_host is None or path is None or not path.endswith(suffix):
        return None
    return (parsed.scheme, parsed.canonical_host, parsed.port, path[: -len(suffix)])


def discovery_key(parsed: ParsedRequest) -> AdvertKey | None:
    """The tuple of a discovery request: `GET <prefix>/info/refs` with exactly one `service` and it `git-upload-pack`."""
    if parsed.method != "GET" or parsed.query_values(_SERVICE_KEY) != (SERVICE,):
        return None
    return _key(parsed, DISCOVERY_SUFFIX)


def exchange_key(parsed: ParsedRequest) -> AdvertKey | None:
    """The tuple of an upload-pack POST, from its path alone (the shape is `upload_pack_disposition`'s)."""
    if parsed.method != "POST":
        return None
    return _key(parsed, UPLOAD_PACK_SUFFIX)


def is_advertisement(parsed: ParsedRequest, status: int, content_type: str | None) -> bool:
    """THE advertisement predicate (item 6; R-8 reads it): the discovery GET answered 200 with the advertisement's
    media type. `content_type` is srt's normalised header value (one string, or None)."""
    return (type(status) is int and status == 200 and discovery_key(parsed) is not None
            and media_type(content_type) == ADVERTISEMENT_TYPE)


class AdvertisementSet:
    """One launch's advertisements: bounded, oldest-first by last insertion, refreshed on a passing re-insert."""

    def __init__(self, capacity: int = ADVERTISEMENT_SET_CAP) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive int")
        self._cap = capacity
        self._entries: OrderedDict[AdvertKey, None] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, parsed: ParsedRequest, status: int, content_type: str | None) -> str:
        """One allowed response, drained by the decider: record it when it is an advertisement; return the enum the
        record stores (never the raw Content-Type)."""
        if is_advertisement(parsed, status, content_type):
            key = discovery_key(parsed)
            with self._lock:
                self._entries[key] = None
                self._entries.move_to_end(key)
                while len(self._entries) > self._cap:
                    self._entries.popitem(last=False)
        return content_type_class(content_type)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def _accepts_result(parsed: ParsedRequest) -> bool:
    for value in parsed.header("accept"):
        for rng in value.split(","):
            if media_type(rng) == RESULT_TYPE:
                return True
    return False


def _shape_ok(parsed: ParsedRequest) -> bool:
    cts = parsed.header("content-type")
    if len(cts) != 1 or media_type(cts[0]) != REQUEST_TYPE:
        return False
    if not _accepts_result(parsed):
        return False
    if any(parsed.header(n) for n in parse.OVERRIDE_HEADERS) or _METHOD_KEY in parsed.query_keys:
        return False
    return True


def upload_pack_disposition(parsed: ParsedRequest, *, adverts: AdvertisementSet,
                            body_cut: bool | None) -> Disposition:
    """Why this request is or is not read-shaped (K1 counts `over_cap` cards, R15-19-3).
    body_cut  srt's `cut` for the spooled body (True: it ran past the cap), or None when it was not spooled yet."""
    key = exchange_key(parsed)
    if key is None:
        return "not_upload_pack"
    if not _shape_ok(parsed):
        return "shape"
    if key not in adverts:
        return "no_advertisement"
    if body_cut is None:
        return "unspooled"
    if body_cut is not False:
        return "over_cap"
    return "read_shaped"


def needs_body(parsed: ParsedRequest, adverts: AdvertisementSet) -> bool:
    """Whether the decider must spool this request's body (need_body max = UPLOAD_PACK_BODY_CAP) before deciding:
    the shape and the advertisement hold and only the cap is left."""
    return upload_pack_disposition(parsed, adverts=adverts, body_cut=None) == "unspooled"


def read_shaped(parsed: ParsedRequest, route: "RouteSnapshot | None", *, adverts: AdvertisementSet,
                body_cut: bool | None) -> bool:
    """classify.decide's `read_shaped` (bind `adverts` and `body_cut`). The route does not change read-shapedness:
    a keyed route's key is `key_allowed`'s, and R-5's keyed query rule ran before this is asked."""
    return upload_pack_disposition(parsed, adverts=adverts, body_cut=body_cut) == "read_shaped"


def git_exchange_prefix(parsed: ParsedRequest) -> str | None:
    """The repo prefix of a git smart-HTTP exchange (canonical path ending `/info/refs` or `/git-upload-pack`, any
    method, any query), else None. Wider than the read shape on purpose: the key's narrowing fails closed."""
    path = parsed.canonical_path
    if path is None:
        return None
    for suffix in (DISCOVERY_SUFFIX, UPLOAD_PACK_SUFFIX):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return None


def _canonical_repo(entry: str) -> str | None:
    if not isinstance(entry, str) or not entry:
        return None
    if not entry.startswith("/"):
        entry = "/" + entry
    if len(entry) > 1 and entry.endswith("/"):
        entry = entry[:-1]
    canon, problem = parse._canonical_path(entry, "GET")
    return None if problem else canon


def key_allowed(parsed: ParsedRequest, route: "RouteSnapshot | None") -> bool:
    """R15-17-1: on a keyed route, a git exchange carries the held key only for a repo in the route's declared set
    (matched by prefix exactly as git spells it, in R-2's canonical form). Any other request: True (the snapshot's
    rule, S6's liveness and classify's `reattached` decide). The discovery GET of an undeclared repo is keyless too."""
    if route is None:
        return True
    prefix = git_exchange_prefix(parsed)
    if prefix is None:
        if parsed.canonical_path is None:
            return False
        return True
    declared = {c for c in (_canonical_repo(e) for e in route.declared_repos) if c is not None}
    return prefix in declared
