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

Keys (R15-17-1): `key_allowed` is the ONE home of "is this a git exchange that needs a declared repo?" (S6's
`keys.decide_key` calls it, and holds no rule of its own). On a keyed route it narrows the attach on a GIT EXCHANGE
(`git_exchange`: an endpoint or discovery path, a dumb-protocol path under a `*.git` segment, or any `service` query;
matched case-folded, `;...` stripped per segment, one trailing `/` stripped) to the route's declared repo set, and an
escape in KEYLESS_ESCAPES (`%2F`, `%5C`, `%25`, `%3B`) in a keyed route's path is keyless outright; it ANDs into
classify's `reattached`. Any other non-git request keeps the snapshot's rule.

Order (B11) is classify's: walls, R-7, R-5's query mark, then this predicate (never asked about a marked request).
The decider binds the launch state: `functools.partial(read_shaped, adverts=<the set>, body_cut=<spool result>)`.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from levain.chat_egress import parse
from levain.chat_egress.classify import Verdict
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

#: R15-21-3: the only thing ever stored about a response's Content-Type. The one definition: records stores these
#: (its CONTENT_TYPE_CLASSES is built from them).
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


def _one_upload_pack_service(parsed: ParsedRequest) -> bool:
    """THE clause "exactly one `service`, and it `git-upload-pack`", over R-1's folded query parse (discovery_key and
    git_exchange both read it here)."""
    return parsed.query_values(_SERVICE_KEY) == (SERVICE,)


def _key(parsed: ParsedRequest, suffix: str) -> AdvertKey | None:
    """The tuple of a request whose canonical path ends `suffix`. A repo at the host's root has the prefix "/" (as
    `git_exchange` and a declared "/" spell it), never ""."""
    path = parsed.canonical_path
    if parsed.canonical_host is None or path is None or not path.endswith(suffix):
        return None
    return (parsed.scheme, parsed.canonical_host, parsed.port, path[: -len(suffix)] or "/")


def discovery_key(parsed: ParsedRequest) -> AdvertKey | None:
    """The tuple of a discovery request: `GET <prefix>/info/refs` with exactly one `service` and it `git-upload-pack`."""
    if parsed.method != "GET" or not _one_upload_pack_service(parsed):
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
        if type(capacity) is not int or not 1 <= capacity <= ADVERTISEMENT_SET_CAP:
            raise ValueError(f"capacity must be an int in 1..{ADVERTISEMENT_SET_CAP} (the hard bound)")
        self._cap = capacity
        self._entries: OrderedDict[AdvertKey, None] = OrderedDict()
        self._lock = threading.Lock()

    def observe(self, parsed: ParsedRequest, status: int, content_type: str | None, *, verdict: Verdict) -> str:
        """One allowed response, drained by the decider: record it when it is an advertisement AND its request was
        classified silent and unmarked (`verdict` is classify's for this request; B11: a request the query marked,
        or any request that went through a card, never feeds the set). Return the enum the record stores (never the
        raw Content-Type), recorded or not."""
        if not isinstance(verdict, Verdict):
            raise TypeError("observe needs the request's classify.Verdict")
        clean = (verdict.kind == "silent" and not verdict.query_names
                 and "credential_in_query" not in verdict.why_all)
        if clean and is_advertisement(parsed, status, content_type):
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


#: RFC 9110 §12.4.2's qvalue.
_QVALUE = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")


def _range_weight(params: str) -> str | None:
    """The `q` of one Accept range's parameters (the text after its media type): "" when it carries none, None when
    it is malformed (a second `q`, an empty or non-qvalue value, whitespace inside `q=`). `q` is case-insensitive."""
    q = ""
    seen = False
    while params:
        param, _, params = params.partition(";")
        name, eq, val = param.strip(_OWS).partition("=")
        if _ascii_lower(name.strip(_OWS)) != "q":
            continue
        if seen or not eq or name != name.strip(_OWS) or not _QVALUE.fullmatch(val):
            return None
        seen, q = True, val
    return q if seen else ""


def _accepts_result(parsed: ParsedRequest) -> bool:
    """An `Accept` range names the result type with a weight above 0. Fail closed: any result-type range with q=0 or
    a malformed q refuses the shape, and a value holding a quoted string (a `,` or `;` inside one would cut the
    ranges wrong) never counts."""
    found = False
    for value in parsed.header("accept"):
        if '"' in value:
            continue
        for rng in value.split(","):
            mt, _, params = rng.partition(";")
            if _ascii_lower(mt.strip(_OWS)) != RESULT_TYPE:
                continue
            q = _range_weight(params)
            if q is None or (q and float(q) == 0):
                return False
            found = True
    return found


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


_SERVICE_ENDPOINTS = frozenset({"git-upload-pack", "git-receive-pack"})
_DUMB_INFO = (["info", "packs"], ["info", "alternates"])


def _fold_segment(seg: str) -> str:
    return _ascii_lower(seg.partition(";")[0])


def git_exchange(parsed: ParsedRequest) -> tuple[bool, str | None]:
    """(is a git exchange, the repo prefix the declared set may name). Wider than the read shape on purpose: the
    key's narrowing fails closed (an over-match keeps the key off a non-git path: friction, never a reach).

    Over R-2's canonical path, each segment case-folded (ASCII) and stripped of `;...`, one trailing `/` stripped:
      (1) it ends `/info/refs`, `/git-upload-pack` or `/git-receive-pack`;
      (2) a dumb-protocol shape under a `*.git` segment: `/HEAD`, `/objects/...`, `/info/packs`, `/info/alternates`;
      (3) the query carries `service` at all (missing value, duplicated, any value).
    The prefix is the path up to the git suffix (up to and including the `*.git` segment for (2)), as spelled in the
    canonical path; "/" for a repo at the host's root, which is keyless in practice: setup refuses "/" as a declared
    repo, so no route's declared set holds it. None (no declared repo can match, keyless) when: an `info/refs` path
    does not carry exactly one `service` and it `git-upload-pack`; or only (3) holds (no repo path)."""
    path = parsed.canonical_path
    if path is None:
        return True, None
    service = parsed.query_values(_SERVICE_KEY)
    raw = path.split("/")
    if len(raw) > 2 and raw[-1] == "":
        raw = raw[:-1]
    folded = [_fold_segment(seg) for seg in raw]
    n = len(folded)
    prefix_raw: list[str] | None = None
    discovery = False
    if n >= 2 and folded[-1] in _SERVICE_ENDPOINTS:
        prefix_raw = raw[:-1]
    elif n >= 3 and folded[-2:] == ["info", "refs"]:
        prefix_raw, discovery = raw[:-2], True
    else:
        for i in range(n - 2, 0, -1):
            rest = folded[i + 1:]
            if folded[i].endswith(".git") and (rest == ["head"] or rest[0] == "objects" or rest in _DUMB_INFO):
                prefix_raw = raw[:i + 1]
                break
    if prefix_raw is None:
        return bool(service), None
    if discovery and not _one_upload_pack_service(parsed):
        return True, None
    return True, "/".join(prefix_raw) or "/"


def _canonical_repo(entry: str) -> str | None:
    if not isinstance(entry, str) or not entry:
        return None
    if not entry.startswith("/"):
        entry = "/" + entry
    if len(entry) > 1 and entry.endswith("/"):
        entry = entry[:-1]
    canon, problem = parse.canonical_path(entry, "GET")
    return None if problem else canon


#: The escapes that make a keyed route's path keyless: an encoded separator (`%2F`, `%5C`), an encoded `%` (`%25`: a
#: double-encoded separator such as `%252F` is `%2F` to a server that decodes twice) and an encoded `;` (`%3B`: the
#: git rule strips `;...` per segment only when it is literal). R-2's canonical form upper-cases every escape it
#: keeps, so these spellings cover every case (`%5C` already walls `path`; it is named here too so this rule does not
#: lean on the wall). authority.egress_key_repo_problem reads this set when it validates a setup record, at replay
#: too: widening it changes which already-signed records the log accepts.
KEYLESS_ESCAPES = ("%2F", "%5C", "%25", "%3B")


def key_allowed(parsed: ParsedRequest, route: "RouteSnapshot | None") -> bool:
    """R15-17-1, the ONE home (S6's `decide_key` calls this): on a keyed route, a git exchange (`git_exchange`)
    carries the held key only for a repo in the route's declared set (matched by prefix exactly as spelled, in R-2's
    canonical form). A walled path: False. A path holding a KEYLESS_ESCAPES escape (`%2F`, `%5C`, `%25`, `%3B`, any
    case): False, git exchange or not, declared repo or not (a server that decodes before routing would see a git path
    this rule never read). Any other request: True (the snapshot's rule, S6's liveness and classify's `reattached`
    decide). The discovery GET of an undeclared repo is keyless too."""
    if route is None:
        return True
    if parsed.canonical_path is None:
        return False
    if any(esc in parsed.canonical_path for esc in KEYLESS_ESCAPES):
        return False
    exchange, prefix = git_exchange(parsed)
    if not exchange:
        return True
    if prefix is None:
        return False
    declared = {c for c in (_canonical_repo(e) for e in route.declared_repos) if c is not None}
    return prefix in declared
