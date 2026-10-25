"""R-5's strip lists, the backstop and `CRED_STRIP_VERSION` (design §3.8.5(o) item 3; CH-D2 S6).

R-5 runs on EVERY request, read or write, silent or carded, under `open network` too, plain HTTP and TLS alike:
  headers  every LISTED header name (`CRED_STRIP_HEADERS`) and every header name the BACKSTOP matches is removed
           (the allow's `removeHeaders`, matched by srt's fold, so one folded name removes every spelling and every
           duplicate). The listed names always go in the removal, present or not; what is RECORDED (`stripped`) is
           only the names the request carried, never a value. A removal past srt's limit (`wire.MAX_REMOVE_NAMES`)
           is a DENY: `strip` raises :class:`TooManyCredentialHeaders`, so no StripResult (no allow) exists for it.
  query    never rewritten (srt's decider protocol cannot rewrite a target, R15-10-A2): a listed or backstop-matched
           query NAME marks the request `credential_in_query`. This module says which names are credential names;
           the exemptions (the signed-URL read exemption, the Azure SAS read shape, the duplicate-name void, keyed
           strictness) and the verdict are classify's (S3a), which reads the constants here.

All names are compared after `wire.fold_name` (lower-case, `_` and `.` read as `-`); query keys arrive already
percent-decoded from `parse.parse_query`. The lists' own names are compared in the same folded form.

The backstop (A1): a folded name in which one of `BACKSTOP_WORDS` STARTS at a word boundary (the name's start or a
`-`), with anything after it, so `x-auth-key`, `authx`, `x-passwd`, `sessionid` and `x-vault-token` match and
`x-deploy-key` (codex's named edge) and `oauthish` do not. `sig` joins the QUERY backstop only, as an exact name
(R15-13). `PAGINATION_NOT_CREDENTIALS` leave the backstop (R15-17-6). The exact spelling is pinned by the K1 corpus
(S9); every list, the pattern and both exemption shapes are versioned together under `CRED_STRIP_VERSION`, and
`PINNED_POLICY_SHA256` holds the digest each version was signed with: a change to any of them without a version bump
is red (a change re-signs, R15-3).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Iterable

from levain.chat_egress import wire

__all__ = ["AZURE_SAS_READ_SHAPE", "AZURE_STORAGE_HOST_SUFFIXES", "BACKSTOP", "BACKSTOP_WORDS", "CRED_STRIP_HEADERS",
           "CRED_STRIP_QUERY", "CRED_STRIP_VERSION", "DENY_TOO_MANY_CREDENTIAL_HEADERS", "PAGINATION_NOT_CREDENTIALS", "PINNED_POLICY_SHA256",
           "QUERY_BACKSTOP_EXACT", "SIGNED_URL_READ_EXEMPT", "StripResult", "TooManyCredentialHeaders",
           "credential_query_names", "fold",
           "header_is_credential", "query_name_is_credential", "query_name_is_listed", "strip", "strip_policy",
           "strip_policy_digest"]

CRED_STRIP_VERSION = 1

CRED_STRIP_HEADERS = frozenset({        # names, compared folded
    "authorization", "proxy-authorization", "cookie",                       # R15-3's three, always
    "x-api-key", "api-key", "x-goog-api-key", "ocp-apim-subscription-key",  # api-key headers
    "private-token", "job-token", "x-auth-token", "x-access-token", "x-amz-security-token"})
CRED_STRIP_QUERY = frozenset({          # query keys: a match CARDS the request, or WALLS it under open network;
                                        # never removed (R15-10-A2)
    "access_token", "api_key", "apikey", "key", "token", "auth", "private_token", "client_secret", "password"})
SIGNED_URL_READ_EXEMPT = frozenset({    # R15-12-B2 / R15-13: the QUERY mark lifted on a READ to an unkeyed host only
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-amz-date", "x-amz-expires",
    "x-amz-signedheaders",
    "x-goog-signature", "x-goog-credential", "x-goog-date", "x-goog-expires", "x-goog-signedheaders"})
AZURE_STORAGE_HOST_SUFFIXES = (         # R15-17-7: the endpoint formats (design item 3 cites the page)
    ".blob.core.windows.net", ".web.core.windows.net", ".dfs.core.windows.net",
    ".file.core.windows.net", ".queue.core.windows.net", ".table.core.windows.net",
    ".blob.storage.azure.net", ".web.storage.azure.net", ".dfs.storage.azure.net",
    ".file.storage.azure.net", ".queue.storage.azure.net", ".table.storage.azure.net")
AZURE_SAS_READ_SHAPE = {                # R15-13 / R15-17-7: a SHAPE, not a name set
    "requires_exactly_one": ("sig", "sv", "se", "sp"),
    "sp_letters": "rl",
    "host_suffixes": AZURE_STORAGE_HOST_SUFFIXES}
PAGINATION_NOT_CREDENTIALS = frozenset({  # R15-17-6: folded; never a backstop match
    "page-token", "next-token", "continuation-token", "cursor", "pagetoken", "nextpagetoken"})

BACKSTOP_WORDS = ("auth", "token", "secret", "passw", "credential", "api-key", "signature", "session")
BACKSTOP = re.compile(r"(?:^|-)(?:" + "|".join(re.escape(w) for w in BACKSTOP_WORDS) + r")")
QUERY_BACKSTOP_EXACT = frozenset({"sig"})

# the digest of strip_policy() each CRED_STRIP_VERSION was signed with
PINNED_POLICY_SHA256 = {
    1: "dc5343ad89de782cf90e455e8565d6c22c3b6d612e3f1180eec7cfcc8de28fb2",
}

fold = wire.fold_name
_LEVAIN_PREFIX = wire.LEVAIN_HEADER_PREFIX


def _folded(names: Iterable[str]) -> frozenset[str]:
    return frozenset(fold(n) for n in names)


def _backstop(folded: str) -> bool:
    return BACKSTOP.search(folded) is not None


def header_is_credential(name: str) -> bool:
    """Whether the header `name` (any spelling) is stripped: listed, or matched by the backstop."""
    f = fold(name)
    return f in _folded(CRED_STRIP_HEADERS) or _backstop(f)


def query_name_is_listed(key: str) -> bool:
    """Whether the (decoded) query key is on `CRED_STRIP_QUERY` (no exemption ever lifts one of these)."""
    return fold(key) in _folded(CRED_STRIP_QUERY)


def query_name_is_credential(key: str) -> bool:
    """Whether the (decoded) query key marks a request `credential_in_query` before any exemption: listed, the exact
    `sig`, or matched by the backstop; a pagination name never is."""
    f = fold(key)
    if f in _folded(PAGINATION_NOT_CREDENTIALS):
        return False
    return f in _folded(CRED_STRIP_QUERY) or f in QUERY_BACKSTOP_EXACT or _backstop(f)


@dataclass(frozen=True, slots=True)
class StripResult:
    """R-5's header half for one request.
    remove_headers  the decider's `removeHeaders` names: every listed header plus every backstop-matched name the
                    request carried, folded, each once (the wrapper adds its own plumbing after, item 2 step 5)
    stripped        the folded names the request CARRIED that are removed, first-seen order: the record's and the
                    card's names (never a value; never an `x-levain-` name, R15-24-2)
    A StripResult exists only for a request whose removal fits one allow; there is no deny form of it."""
    remove_headers: tuple[str, ...]
    stripped: tuple[str, ...]


#: the deny reason when a request carries more credential header names than one allow can remove
DENY_TOO_MANY_CREDENTIAL_HEADERS = "too-many-credential-headers"


class TooManyCredentialHeaders(Exception):
    """The request carries more credential header names than srt's `wire.MAX_REMOVE_NAMES` lets one allow remove:
    it is DENIED (`wire.deny_frame` with `reason`), never truncated, since a name left off the list would forward its
    value. Raised, so a composer that does not handle it sends no allow at all (fail closed by construction).
    `stripped` is the folded names the request carried, for the record (never a value)."""

    reason = DENY_TOO_MANY_CREDENTIAL_HEADERS

    def __init__(self, stripped: tuple[str, ...]) -> None:
        super().__init__(f"{len(stripped)} credential header names: more than one allow can remove")
        self.stripped = stripped


def strip(parsed) -> StripResult:
    """R-5's header strip for a `parse.ParsedRequest`; the same on plain HTTP and TLS, keyed or not. Over srt's
    removal limit (the listed names plus the carried ones past `wire.MAX_REMOVE_NAMES`) it raises
    :class:`TooManyCredentialHeaders`: the request is denied."""
    present: list[str] = []
    for h in parsed.headers:
        f = h.folded
        if f.startswith(_LEVAIN_PREFIX) or f in present:
            continue
        if header_is_credential(f):
            present.append(f)
    remove = sorted(_folded(CRED_STRIP_HEADERS))
    remove += [f for f in present if f not in remove]
    if len(remove) > wire.MAX_REMOVE_NAMES:
        raise TooManyCredentialHeaders(tuple(present))
    return StripResult(remove_headers=tuple(remove), stripped=tuple(present))


def credential_query_names(parsed) -> tuple[str, ...]:
    """The folded query (or form) keys of a `parse.ParsedRequest` that are credential names
    (:func:`query_name_is_credential`), each once, first-seen order. Classify applies the exemptions."""
    out: list[str] = []
    for q in parsed.params:
        if q.folded not in out and query_name_is_credential(q.key):
            out.append(q.folded)
    return tuple(out)


def strip_policy() -> dict:
    """Everything `CRED_STRIP_VERSION` versions, in one canonical value (what `containment_digest` and the session
    header's `known_credentials_scrubbed` stand for, item 7)."""
    return {
        "version": CRED_STRIP_VERSION,
        "headers": sorted(_folded(CRED_STRIP_HEADERS)),
        "query": sorted(_folded(CRED_STRIP_QUERY)),
        "backstop": BACKSTOP.pattern,
        "query_backstop_exact": sorted(QUERY_BACKSTOP_EXACT),
        "pagination": sorted(_folded(PAGINATION_NOT_CREDENTIALS)),
        "signed_url_read_exempt": sorted(_folded(SIGNED_URL_READ_EXEMPT)),
        "azure_sas_read_shape": {"requires_exactly_one": list(AZURE_SAS_READ_SHAPE["requires_exactly_one"]),
                                 "sp_letters": AZURE_SAS_READ_SHAPE["sp_letters"],
                                 "host_suffixes": list(AZURE_SAS_READ_SHAPE["host_suffixes"])},
    }


def strip_policy_digest() -> str:
    """sha256 of :func:`strip_policy` in canonical JSON."""
    raw = json.dumps(strip_policy(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()
