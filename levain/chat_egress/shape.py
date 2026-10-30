"""R-10's path shape: the decider's ONE path-shape function (design §3.8.5(o), R15-19-5, R15-21-2, R15-18-Q3).

Nothing here ever returns a query value, and the glue logs no path at all (R15-21-2).

KEYED host (the route snapshot holds a record): a segment is literal only if it is (a) a segment of a declared repo
path of that route (item 4; compared in R-2's canonical form) or (b) in GIT_VOCABULARY (exact case); every other
segment is `<len-bucket:charset-class>`.

UNKEYED host: B15's high-entropy redaction. A segment is a token when it looks like an identifier or a secret
(`is_high_entropy`) or carries a character outside RFC 3986's pchar set (so a literal can never spoof a token or
break a log line); every other segment is literal. A short low-entropy secret segment stays literal: the known edge
(§15), the operator's own local log.

A WALLED path (no R-2 canonical form) is all tokens, keyed or not. The canonical form is what is shaped otherwise.

The token alphabet [the buckets and classes are the build's, design fold reading]:
  len-bucket     0, 1-4, 5-8, 9-16, 17-32, 33-64, 65+ (never the exact length)
  charset-class  the classes present, in the order a (a-z), A (A-Z), 9 (0-9), - (`-._~`), % (`%`), * (any other)
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from levain.chat_egress import advert
from levain.chat_egress.parse import ParsedRequest, QueryParam

if TYPE_CHECKING:  # the type only: S6 owns the definition
    from levain.chat_egress.keys import RouteSnapshot

GIT_VOCABULARY = frozenset({"info", "refs", "git-upload-pack", "git-receive-pack", "HEAD", "objects"})

_BUCKETS = ((4, "1-4"), (8, "5-8"), (16, "9-16"), (32, "17-32"), (64, "33-64"))
_PCHAR = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@%]+")
_HEX = re.compile(r"[0-9a-fA-F]+")
_DIGITS = re.compile(r"[0-9]+")

#: B15's thresholds (the build's): a segment this long is an identifier whatever it holds; a mixed-class segment at
#: least `_MIXED_LEN` long with at least `_MIN_BITS` bits of entropy per character; a hex id with both digits and
#: letters from `_HEX_LEN`; an all-digit run from `_DIGIT_LEN`.
_ALWAYS_LEN = 32
_MIXED_LEN = 16
_MIN_BITS = 3.0
_HEX_LEN = 12
_DIGIT_LEN = 16


def len_bucket(n: int) -> str:
    if n <= 0:
        return "0"
    for top, name in _BUCKETS:
        if n <= top:
            return name
    return "65+"


def charset_class(seg: str) -> str:
    present = set()
    for c in seg:
        if "a" <= c <= "z":
            present.add("a")
        elif "A" <= c <= "Z":
            present.add("A")
        elif "0" <= c <= "9":
            present.add("9")
        elif c in "-._~":
            present.add("-")
        elif c == "%":
            present.add("%")
        else:
            present.add("*")
    return "".join(k for k in "aA9-%*" if k in present)


def segment_token(seg: str, *, flag: str | None = None) -> str:
    tail = f":{flag}" if flag else ""
    return f"<{len_bucket(len(seg))}:{charset_class(seg)}{tail}>"


def _bits_per_char(seg: str) -> float:
    n = len(seg)
    return -sum((k / n) * math.log2(k / n) for k in Counter(seg).values())


def is_high_entropy(seg: str) -> bool:
    """B15: whether an UNKEYED host's segment is redacted as identifier- or secret-like."""
    n = len(seg)
    if n >= _ALWAYS_LEN:
        return True
    if n >= _DIGIT_LEN and _DIGITS.fullmatch(seg):
        return True
    if n >= _HEX_LEN and _HEX.fullmatch(seg) and re.search(r"[0-9]", seg) and re.search(r"[a-fA-F]", seg):
        return True
    classes = sum(1 for k in "aA9" if k in charset_class(seg))
    return n >= _MIXED_LEN and classes >= 2 and _bits_per_char(seg) >= _MIN_BITS


def _declared_segments(declared_repos: Iterable[str]) -> frozenset[str]:
    segs: set[str] = set()
    for entry in declared_repos:
        canon = advert._canonical_repo(entry)
        if canon:
            segs.update(s for s in canon.split("/") if s)
    return frozenset(segs)


def path_shape(path: str | None, *, keyed: bool, declared_repos: Iterable[str] = (), raw: str = "") -> str:
    """The shape of an R-2 canonical path (`path`), or of `raw` all-tokens when `path` is None (a walled path)."""
    if path is None:
        return "/".join(segment_token(s) if s else "" for s in raw.split("/"))
    literal = _declared_segments(declared_repos) | GIT_VOCABULARY if keyed else frozenset()
    out = []
    for seg in path.split("/"):
        if not seg:
            out.append("")
        elif keyed:
            out.append(seg if seg in literal else segment_token(seg))
        elif not _PCHAR.fullmatch(seg) or is_high_entropy(seg):
            out.append(segment_token(seg))
        else:
            out.append(seg)
    return "/".join(out)


def request_path_shape(parsed: ParsedRequest, route: "RouteSnapshot | None") -> str:
    """The loggable path of one request: keyed iff the route snapshot holds a record for its host."""
    keyed = route is not None
    return path_shape(parsed.canonical_path, keyed=keyed,
                      declared_repos=route.declared_repos if keyed else (), raw=parsed.path)


def query_name_shapes(params: Iterable[QueryParam]) -> tuple[str, ...]:
    """R-10 / R15-21-2 / R15-22-2: a query NAME is literal (folded) only when it is in item 3's named lists (the
    credential list or the signed-URL set) or the pagination names; a name the backstop alone matches is a token
    flagged `backstop`; any other name is a token. Never a value."""
    from levain.chat_egress import classify as c   # the lists' one home today (S3a observation 1)
    named = c._CRED_STRIP_QUERY | c._SIGNED_URL_READ_EXEMPT | c._PAGINATION_NOT_CREDENTIALS
    out = []
    for q in params:
        if q.folded in named:
            out.append(q.folded)
        elif c._credential_like(q.folded):
            out.append(segment_token(q.folded, flag="backstop"))
        else:
            out.append(segment_token(q.folded))
    return tuple(out)
