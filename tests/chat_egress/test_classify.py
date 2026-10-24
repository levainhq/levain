"""CH-D2 S3a: the classifier's verdict (design §3.8.5(o) R-3/R-4 via `parse.walls`, R-5's query mark, R-7, R-9).

Rows: R9-A3, R15-10-A2, R15-10-A11, K3, R15-12-B2, R15-12-B10 (REVERSED; its reds are R15-17-10's), R15-13-Q2,
R15-13-Q3, R15-13-Q4, R15-15-Q3, R15-15-Q4, R15-17-6, R15-17-7, R15-17-10, R15-18-Q2, R15-18-L2.

A table-driven suite: every case is one request, and its expectation names the verdict in all four cells,
(gated | `open network`) x (unkeyed | keyed). "Keyed" is the route snapshot holding a record for the host (R15-23-4,
R15-18-Q2): the classifier is handed that one lookup result (R15-18-L1) and reads nothing else.

The forward-after-allow halves (what the upstream receives once he allows, zero upstream bytes before an allow)
need the composed run and stay red until S9: they are `xfail(strict=True)` at the bottom of this file.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from levain.chat_egress import classify, parse
from levain.chat_egress.classify import GATED, OPEN_NETWORK, decide

AUDIT = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"


@dataclass(frozen=True)
class FakeRoute:
    """The plan's fixed `RouteSnapshot` shape (S6 owns the definition; S6 had not landed `keys.py` when S3a ran)."""
    record_id: str
    canonical_host: str
    credential_source: str
    declared_repos: frozenset
    attach: bool


def frame(method="GET", host="public.example", port=443, path="/", query=None, headers=None, sni="default"):
    hdrs = {k: list(v) if isinstance(v, (list, tuple)) else [v] for k, v in (headers or {}).items()}
    hdrs.setdefault("x-levain-audit", [AUDIT])
    f = {"t": "req", "id": 1, "method": method, "host": host, "port": port, "path": path, "headers": hdrs}
    if sni == "default":
        sni = host if port == 443 else None
    if sni:
        f["sni"] = sni
    if query is not None:
        f["query"] = query
    return f


def route_for(p, attach=True):
    return FakeRoute(record_id="rec-1", canonical_host=p.canonical_host, credential_source="keychain:x",
                     declared_repos=frozenset(), attach=attach)


def run(fr, mode, keyed, **kw):
    p = parse.parse(fr)
    return decide(p, route_for(p) if keyed else None, mode, **kw)


S = ("silent", None)


def C(why):
    return ("card", why)


def W(why):
    return ("wall", why)


CQ, WQ = C("credential_in_query"), W("credential_in_query")


def cells(gu, gk, ou, ok):
    """(gated unkeyed, gated keyed, open-network unkeyed, open-network keyed)."""
    return {(GATED, False): gu, (GATED, True): gk, (OPEN_NETWORK, False): ou, (OPEN_NETWORK, True): ok}


#: a credential-named query value: a card gated, a wall under open network, on any host (A2, Q2, R15-17-10)
CRED = cells(CQ, CQ, WQ, WQ)
#: a signed-URL / Azure read: silent on an unkeyed host, no exemption on a keyed route (B2, Q3, R15-15-Q3)
EXEMPT = cells(S, CQ, S, WQ)


def NONREAD(why):
    """A non-read with no query credential: a card gated; under open network forwarded to an unkeyed host and a card
    to a keyed one (R-9, A11)."""
    return cells(C(why), C(why), S, C(why))


READ = cells(S, S, S, S)

AMZ = ("X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA%2F20261024%2Fus-east-1%2Fs3%2Faws4_request"
       "&X-Amz-Date=20261024T000000Z&X-Amz-Expires=300&X-Amz-SignedHeaders=host&X-Amz-Signature=abc123")
AMZ_ONLY = ("X-Amz-Credential=AKIA%2F20261024&X-Amz-Date=20261024T000000Z&X-Amz-Expires=300"
            "&X-Amz-SignedHeaders=host&X-Amz-Signature=abc123")
GOOG = ("X-Goog-Credential=svc%2F20261024&X-Goog-Date=20261024T000000Z&X-Goog-Expires=600"
        "&X-Goog-SignedHeaders=host&X-Goog-Signature=abc123")
AZ = "acct.blob.core.windows.net"
SAS = "sv=2022-11-02&se=2026-12-01T00%3A00%3A00Z&sp=r&sr=b&sig=abc%3D"

CASES = [
    # --- R9-A3: override names make a bodyless GET a non-read; why = override:<name> / _method -------------------
    ("R9-A3", "x-http-method-override", frame(headers={"x-http-method-override": "DELETE"}),
     NONREAD("override:x-http-method-override")),
    ("R9-A3", "x-method-override", frame(headers={"x-method-override": "DELETE"}),
     NONREAD("override:x-method-override")),
    ("R9-A3", "x-http-method", frame(headers={"x-http-method": "DELETE"}), NONREAD("override:x-http-method")),
    ("R9-A3", "X_HTTP_Method_Override folded", frame(headers={"x_http_method_override": "DELETE"}),
     NONREAD("override:x-http-method-override")),
    ("R9-A3", "X.Method.Override folded", frame(headers={"x.method.override": "PUT"}),
     NONREAD("override:x-method-override")),
    ("R9-A3", "?_method", frame(query="_method=DELETE"), NONREAD("_method")),
    ("R9-A3", "?%5fmethod", frame(query="%5fmethod=DELETE"), NONREAD("_method")),
    ("R9-A3", "?_METHOD any case", frame(query="_METHOD=DELETE"), NONREAD("_method")),
    ("R9-A3", "?-method is the same folded key", frame(query="-method=DELETE"), NONREAD("_method")),
    ("R9-A3", "plain GET is a read", frame(), READ),
    ("R9-A3", "HEAD is a read", frame(method="HEAD"), READ),
    ("R9-A3", "OPTIONS is a read", frame(method="OPTIONS"), READ),
    # the rest of R-7: a body, a method outside SAFE_METHODS
    ("R9-A3", "GET with a body", frame(headers={"content-length": "5"}), NONREAD("body")),
    ("R9-A3", "GET chunked", frame(headers={"transfer-encoding": "chunked"}), NONREAD("body")),
    ("R9-A3", "GET content-length 0 is bodyless", frame(headers={"content-length": "0"}), READ),
    ("R9-A3", "GET content-length unreadable is a body", frame(headers={"content-length": "x"}), NONREAD("body")),
    ("R9-A3", "GET content-length 0 twice", frame(headers={"content-length": ["0", "0"]}), READ),
    ("R9-A3", "GET content-length 0 and 5", frame(headers={"content-length": ["0", "5"]}), NONREAD("body")),
    ("R9-A3", "DELETE", frame(method="DELETE"), NONREAD("method")),
    # --- R15-10-A2: a credential in the query is carded (gated), walled (open network), never stripped ----------
    ("R15-10-A2", "codex: GET /trigger?access_token=", frame(path="/trigger", query="access_token=WORKSPACE_SECRET"),
     CRED),
    ("R15-10-A2", "access%5Ftoken", frame(query="access%5Ftoken=x"), CRED),
    ("R15-10-A2", ";-separated", frame(query="a=1;access_token=x"), CRED),
    ("R15-10-A2", "repeated key", frame(query="token=a&token=b"), CRED),
    ("R15-10-A2", "folded ACCESS.TOKEN", frame(query="ACCESS.TOKEN=x"), CRED),
    ("R15-10-A2", "valueless ?api_key", frame(query="api_key"), CRED),
    ("R15-10-A2", "backstop: my_secret", frame(query="my_secret=x"), CRED),
    ("R15-10-A2", "backstop: session_id", frame(query="session_id=x"), CRED),
    ("R15-10-A2", "backstop: x-auth-key", frame(query="x-auth-key=x"), CRED),
    ("R15-10-A2", "backstop: client_credentials", frame(query="client_credentials=x"), CRED),
    ("R15-10-A2", "backstop: user-password", frame(query="user-password=x"), CRED),
    ("R15-10-A2", "backstop: x-api-key", frame(query="x-api-key=x"), CRED),
    ("R15-10-A2", "non-credential names stay a read", frame(query="q=hello&page=2&sort=asc"), READ),
    ("R15-10-A2", "a non-read carrying one", frame(method="POST", query="token=x"), CRED),
    # --- R15-10-A11 / K3 / R15-18-Q2: the method rule for a keyed host under open network --------------------
    ("R15-10-A11", "bodyless POST", frame(method="POST"), NONREAD("method")),
    ("R15-10-A11", "POST with a body", frame(method="POST", headers={"content-length": "12"}), NONREAD("body")),
    ("K3", "PUT", frame(method="PUT", headers={"content-length": "3"}), NONREAD("body")),
    ("K3", "GET to a keyed host is silent", frame(path="/repos/o/r"), READ),
    ("R15-18-Q2", "POST to http://<host>/ (port 80)", frame(method="POST", port=80), NONREAD("method")),
    ("R15-18-Q2", "POST to <host>:8443", frame(method="POST", port=8443), NONREAD("method")),
    # --- R15-12-B2: the signed-URL read exemption, B2's shape; empty on a keyed route (R15-15-Q3) --------------
    ("R15-12-B2", "GET carrying only X-Amz-* names", frame(query=AMZ_ONLY), EXEMPT),
    ("R15-12-B2", "GET with X-Amz-Algorithm (not credential-like) too", frame(query=AMZ), EXEMPT),
    ("R15-12-B2", "HEAD carrying only X-Amz-* names", frame(method="HEAD", query=AMZ_ONLY), EXEMPT),
    ("R15-12-B2", "GET carrying only X-Goog-* names", frame(query=GOOG), EXEMPT),
    ("R15-12-B2", "x-amz-security-token in the query", frame(query="X-Amz-Security-Token=t&X-Amz-Signature=s"),
     EXEMPT),
    ("R15-12-B2", "GET with a body", frame(query=AMZ_ONLY, headers={"content-length": "4"}), CRED),
    ("R15-12-B2", "GET chunked", frame(query=AMZ_ONLY, headers={"transfer-encoding": "chunked"}), CRED),
    ("R15-12-B2", "encoded _method", frame(query=AMZ_ONLY + "&%5Fmethod=DELETE"), CRED),
    ("R15-12-B2", "override header", frame(query=AMZ_ONLY, headers={"x-http-method-override": "DELETE"}), CRED),
    ("R15-12-B2", "Upgrade", frame(query=AMZ_ONLY, headers={"upgrade": "websocket"}),
     cells(*[W("protocol")] * 4)),
    ("R15-12-B2", "one exempt name plus ?token=", frame(query="X-Amz-Signature=s&token=t"), CRED),
    ("R15-12-B2", "OPTIONS carrying one", frame(method="OPTIONS", query="X-Amz-Signature=s"), CRED),
    ("R15-12-B2", "a non-read (POST) carrying one", frame(method="POST", query="X-Amz-Signature=s"), CRED),
    ("R15-12-B2", "a non-read carrying only X-Amz-Date", frame(method="PUT", query="X-Amz-Date=d"), CRED),
    ("R15-12-B2", "generic ?signature= never exempt", frame(query="signature=s"), CRED),
    ("R15-12-B2", "generic ?sig= never exempt", frame(query="sig=s"), CRED),
    ("R15-12-B2", "generic ?token= never exempt", frame(query="token=t"), CRED),
    # --- R15-13-Q2: a keyed route carrying a query credential --------------------------------------------------
    ("R15-13-Q2", "bodyless GET ?token=", frame(query="token=t"), CRED),
    ("R15-13-Q2", "keyed POST ?token=", frame(method="POST", query="token=t"), CRED),
    ("R15-13-Q2", "exempt-only read on a keyed route", frame(query=AMZ_ONLY), EXEMPT),
    # --- R15-13-Q3 / R15-15-Q4 / R15-17-7: the Azure SAS read shape ----------------------------------------------
    ("R15-13-Q3", "the shape, sp=r", frame(host=AZ, query=SAS), EXEMPT),
    ("R15-13-Q3", "the shape, HEAD", frame(method="HEAD", host=AZ, query=SAS), EXEMPT),
    ("R15-13-Q3", "sp=rw", frame(host=AZ, query=SAS.replace("sp=r", "sp=rw")), CRED),
    ("R15-13-Q3", "with a body", frame(host=AZ, query=SAS, headers={"content-length": "2"}), CRED),
    ("R15-13-Q3", "with an override", frame(host=AZ, query=SAS, headers={"x-http-method": "PUT"}), CRED),
    ("R15-13-Q3", "with ?_method", frame(host=AZ, query=SAS + "&_method=PUT"), CRED),
    ("R15-13-Q3", "missing sv", frame(host=AZ, query=SAS.replace("sv=2022-11-02&", "")), CRED),
    ("R15-13-Q3", "missing se", frame(host=AZ, query=SAS.replace("se=2026-12-01T00%3A00%3A00Z&", "")), CRED),
    ("R15-13-Q3", "missing sp", frame(host=AZ, query=SAS.replace("sp=r&", "")), CRED),
    ("R15-13-Q3", "the shape plus ?token=", frame(host=AZ, query=SAS + "&token=t"), CRED),
    ("R15-13-Q3", "webhook GET ?signature=", frame(host="hooks.example", query="signature=s&event=push"), CRED),
    ("R15-13-Q3", "?sig= outside the shape", frame(host=AZ, query="sig=s&sr=b"), CRED),
    ("R15-13-Q3", "a header named sig is no query mark", frame(headers={"sig": "s"}), READ),
    ("R15-15-Q4", "sp= empty", frame(host=AZ, query=SAS.replace("sp=r", "sp=")), CRED),
    ("R15-15-Q4", "sp valueless", frame(host=AZ, query=SAS.replace("sp=r", "sp")), CRED),
    ("R15-15-Q4", "sp=R upper case is not r", frame(host=AZ, query=SAS.replace("sp=r", "sp=R")), CRED),
    ("R15-17-7", "sp=rl sr=c on blob.core", frame(host=AZ, query=SAS.replace("sp=r&sr=b", "sp=rl&sr=c")), EXEMPT),
    ("R15-17-7", "sp=l", frame(host=AZ, query=SAS.replace("sp=r", "sp=l")), EXEMPT),
    ("R15-17-7", "z7 DNS-zone endpoint", frame(host="acct.z7.blob.storage.azure.net",
                                               query=SAS.replace("sp=r&sr=b", "sp=rl&sr=c")), EXEMPT),
    ("R15-17-7", "z50 DNS-zone endpoint", frame(host="acct.z50.dfs.storage.azure.net", query=SAS), EXEMPT),
    ("R15-17-7", "each standard service", frame(host="acct.table.core.windows.net", query=SAS), EXEMPT),
    ("R15-17-7", "on evil.example", frame(host="evil.example", query=SAS), CRED),
    ("R15-17-7", "on a custom domain", frame(host="files.contoso.com", query=SAS), CRED),
    ("R15-17-7", "suffix spoof blob.core.windows.net.evil", frame(host="acct.blob.core.windows.net.evil.example",
                                                                    query=SAS), CRED),
    ("R15-17-7", "two account labels", frame(host="a.b.blob.core.windows.net", query=SAS), CRED),
    ("R15-17-7", "bare suffix, no account", frame(host="blob.core.windows.net", query=SAS), CRED),
    ("R15-17-7", "z51 is not a zone", frame(host="acct.z51.blob.storage.azure.net", query=SAS), CRED),
    ("R15-17-7", "z0 is not a zone", frame(host="acct.z0.blob.storage.azure.net", query=SAS), CRED),
    ("R15-17-7", "z07 is not a zone", frame(host="acct.z07.blob.storage.azure.net", query=SAS), CRED),
    ("R15-17-7", "storage.azure.net without a zone", frame(host="acct.blob.storage.azure.net", query=SAS), CRED),
    ("R15-17-7", "core.windows.net with a zone label", frame(host="acct.z1.blob.core.windows.net", query=SAS),
     CRED),
    ("R15-17-7", "sp=r&sp=rw", frame(host=AZ, query=SAS + "&sp=rw"), CRED),
    ("R15-17-7", "sig twice", frame(host=AZ, query=SAS + "&sig=again"), CRED),
    ("R15-17-7", "a duplicated non-credential name voids it", frame(host=AZ, query=SAS + "&sr=c"), CRED),
    ("R15-17-7", "x-amz-signature twice", frame(query=AMZ_ONLY + "&X-Amz-Signature=again"), CRED),
    ("R15-17-7", "a duplicate after the fold", frame(query=AMZ_ONLY + "&x_amz_date=again"), CRED),
    # --- R15-17-6: pagination names leave the backstop, keyed too --------------------------------------------
    ("R15-17-6", "page_token", frame(query="page_token=abc"), READ),
    ("R15-17-6", "next_token", frame(query="next_token=abc"), READ),
    ("R15-17-6", "continuation_token", frame(query="continuation_token=abc"), READ),
    ("R15-17-6", "cursor", frame(query="cursor=abc"), READ),
    ("R15-17-6", "pageToken", frame(query="pageToken=abc"), READ),
    ("R15-17-6", "nextPageToken", frame(query="nextPageToken=abc"), READ),
    ("R15-17-6", "PAGE.TOKEN folded", frame(query="PAGE.TOKEN=abc"), READ),
    ("R15-17-6", "page_token with token: token decides", frame(query="page_token=a&token=t"), CRED),
    ("R15-17-6", "only exempt names to http://<host>/", frame(port=80, query=AMZ_ONLY), EXEMPT),
    ("R15-17-6", "pagination on a POST is still the method rule", frame(method="POST", query="cursor=a"),
     NONREAD("method")),
    # --- R15-17-10 (B10 REVERSED): under open network a read carrying a query credential walls -----------------
    ("R15-17-10", "?token=", frame(query="token=t"), CRED),
    ("R15-17-10", "?key=", frame(query="key=k"), CRED),
    ("R15-17-10", "?auth=", frame(query="auth=a"), CRED),
    ("R15-17-10", "?session=", frame(query="session=s"), CRED),
    ("R15-17-10", "?page_token= forwarded", frame(query="page_token=p"), READ),
    ("R15-17-10", "exempt signed-URL names", frame(query=AMZ_ONLY), EXEMPT),
    # --- R15-18-L2: old-style presigned URLs stay carded on unkeyed hosts (accepted friction) ------------------
    ("R15-18-L2", "S3 SigV2", frame(host="bucket.s3.amazonaws.com",
                                   query="AWSAccessKeyId=AKIA&Expires=1767225600&Signature=abc%3D"), CRED),
    ("R15-18-L2", "CloudFront", frame(host="d1.cloudfront.net",
                                     query="Expires=1767225600&Signature=abc&Key-Pair-Id=K2"), CRED),
    # --- R-3 / R-4 through parse.walls: both modes, keyed or not ---------------------------------------------
    ("walls", "git-receive-pack", frame(method="POST", path="/o/r.git/git-receive-pack"),
     cells(*[W("use_w2_push")] * 4)),
    ("walls", "info/refs receive-pack", frame(path="/o/r.git/info/refs", query="service=git-receive-pack"),
     cells(*[W("use_w2_push")] * 4)),
    ("walls", "Upgrade on a POST", frame(method="POST", headers={"upgrade": "h2c"}), cells(*[W("protocol")] * 4)),
    ("walls", "a dot-segment path", frame(path="/a/%2e%2e/b", query="token=t"), cells(*[W("path")] * 4)),
]

CELLS = [(GATED, False), (GATED, True), (OPEN_NETWORK, False), (OPEN_NETWORK, True)]


def _cell_id(mode, keyed):
    return f"{'gated' if mode == GATED else 'open'}-{'keyed' if keyed else 'unkeyed'}"


@pytest.mark.parametrize("mode,keyed", CELLS, ids=[_cell_id(*c) for c in CELLS])
@pytest.mark.parametrize("row,name,fr,expect", CASES, ids=[f"{c[0]}:{c[1]}" for c in CASES])
def test_verdict_table(row, name, fr, expect, mode, keyed):
    v = run(fr, mode, keyed)
    assert (v.kind, v.why) == expect[(mode, keyed)], f"{row} {name}"
    assert v.keyed is keyed


def test_the_table_covers_every_row():
    rows = {c[0] for c in CASES}
    assert {"R9-A3", "R15-10-A2", "R15-10-A11", "K3", "R15-12-B2", "R15-13-Q2", "R15-13-Q3", "R15-15-Q4",
            "R15-17-6", "R15-17-7", "R15-17-10", "R15-18-Q2", "R15-18-L2"} <= rows


# --- the card's fields ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("hdr", ["x-http-method-override", "x-http-method", "x-method-override"])
def test_r15_13_q4_override_header_card_shows_the_effective_method_and_the_header(hdr):
    v = run(frame(headers={hdr: "delete"}), GATED, False)
    assert (v.kind, v.method) == ("card", "DELETE")
    assert hdr in v.card_header_names and hdr in v.override_names


def test_r15_13_q4_query_method_card_shows_the_effective_method():
    v = run(frame(query="_method=DELETE"), GATED, False)
    assert (v.kind, v.why, v.method) == ("card", "_method", "DELETE")


def test_r15_13_q4_form_body_method():
    fr = frame(method="POST", headers={"content-type": "application/x-www-form-urlencoded",
                                       "content-length": "14"})
    v = run(fr, GATED, False, form_params=parse.parse_query("_method=DELETE"))
    assert (v.kind, v.why, v.method) == ("card", "_method", "DELETE")
    assert "body" in v.why_all and "method" in v.why_all


def test_r15_13_q4_no_override_card_shows_the_request_method():
    assert run(frame(method="PATCH"), GATED, False).method == "PATCH"
    assert run(frame(), GATED, False).method == "GET"


def test_conflicting_overrides_name_no_single_method():
    v = run(frame(headers={"x-http-method-override": "DELETE"}, query="_method=PUT"), GATED, False)
    assert v.kind == "card" and v.method == classify.AMBIGUOUS_METHOD
    v = run(frame(headers={"x-http-method-override": "DE LETE"}), GATED, False)
    assert v.kind == "card" and v.method == classify.AMBIGUOUS_METHOD


@pytest.mark.parametrize("name", ["x-original-url", "x-rewrite-url", "x-forwarded-host"])
def test_r15_13_q4_routing_headers_never_on_a_card(name):
    read = run(frame(headers={name: "/admin/delete"}), GATED, False)
    assert read.kind == "silent"
    card = run(frame(method="POST", headers={name: "/admin/delete", "x-custom": "1"}), GATED, False)
    assert card.kind == "card" and name not in card.card_header_names and "x-custom" in card.card_header_names


def test_r15_10_a2_names_never_values():
    v = run(frame(query="access_token=WORKSPACE_SECRET&page=2"), GATED, False)
    assert v.query_names == ("access-token",)
    assert "WORKSPACE_SECRET" not in repr(v)


def test_why_all_lists_every_clause_in_order():
    v = run(frame(method="POST", headers={"x-http-method-override": "DELETE", "content-length": "3"},
                  query="token=t"), GATED, False)
    assert v.why == "credential_in_query"
    assert v.why_all == ("override:x-http-method-override", "body", "method", "credential_in_query")


# --- K3: reattached ---------------------------------------------------------------------------------------------

def test_k3_keyed_write_card_names_the_reattached_host():
    v = run(frame(method="POST", host="api.github.com"), GATED, True)
    assert (v.kind, v.reattached) == ("card", "api.github.com")


def test_k3_keyed_get_is_silent_and_reattached():
    v = run(frame(host="api.github.com"), GATED, True)
    assert (v.kind, v.reattached) == ("silent", "api.github.com")


def test_k3_unkeyed_write_has_no_reattached():
    assert run(frame(method="POST"), GATED, False).reattached is None


@pytest.mark.parametrize("port", [80, 8443])
def test_r15_18_q2_keyed_off_443_or_plain_carries_no_key(port):
    """Keyed for classification, independent of scheme and port; attachment only on (https, host, 443)."""
    v = run(frame(method="POST", port=port), OPEN_NETWORK, True)
    assert (v.kind, v.why, v.keyed, v.reattached) == ("card", "method", True, None)


def test_r15_18_q2_keyed_https_on_8443_carries_no_key():
    """R15-12-B1: re-attachment matches (https, host, 443); the setup host over TLS on 8443 gets no key."""
    v = run(frame(method="POST", port=8443, sni="public.example"), GATED, True)
    assert (v.kind, v.keyed, v.reattached) == ("card", True, None)
    assert run(frame(method="POST", port=443), GATED, True).reattached == "public.example"


def test_k3_snapshot_attach_false_reattaches_nothing():
    p = parse.parse(frame(method="POST"))
    v = decide(p, route_for(p, attach=False), GATED)
    assert (v.kind, v.keyed, v.reattached) == ("card", True, None)


# --- K1's counts (R15-13-Q3, R15-17-10, R15-18-L2) --------------------------------------------------------------

def test_k1_tags_signed_url_and_azure_exemptions():
    assert run(frame(query=AMZ_ONLY), GATED, False).exemption == "signed_url"
    assert "signed_url_exempt" in run(frame(query=AMZ_ONLY), GATED, False).tags
    az = run(frame(host=AZ, query=SAS), OPEN_NETWORK, False)
    assert az.exemption == "azure_sas" and "azure_sas_exempt" in az.tags
    keyed = run(frame(host=AZ, query=SAS), GATED, True)
    assert keyed.exemption is None and "azure_sas_exempt" not in keyed.tags


@pytest.mark.parametrize("q", ["sig=s", "signature=s", "Signature=s&Key-Pair-Id=k"])
def test_k1_tags_sig_signature_backstop_matches(q):
    assert "sig_backstop" in run(frame(query=q), GATED, False).tags


def test_k1_tags_presigned_v2():
    v2 = run(frame(query="AWSAccessKeyId=AKIA&Expires=1&Signature=abc"), GATED, False)
    cf = run(frame(query="Expires=1&Signature=abc&Key-Pair-Id=K2"), GATED, False)
    webhook = run(frame(query="signature=abc"), GATED, False)
    assert "presigned_v2" in v2.tags and "presigned_v2" in cf.tags and "presigned_v2" not in webhook.tags


def test_r15_17_10_open_network_wall_is_counted():
    v = run(frame(query="token=t"), OPEN_NETWORK, False)
    assert v.kind == "wall" and "credential_in_query_wall" in v.tags


# --- the order: parse.walls first (head ruling on S2 obs 3), then the query mark before R-8 ----------------------

def test_decide_calls_parse_walls_first_and_never_reimplements_one(monkeypatch):
    calls = []

    def fake_walls(p):
        calls.append(p)
        return "host_mismatch"

    monkeypatch.setattr(parse, "walls", fake_walls)
    p = parse.parse(frame(query="token=t"))
    v = decide(p, None, OPEN_NETWORK)
    assert calls == [p] and (v.kind, v.why) == ("wall", "host_mismatch")
    # and a wall-free answer from walls lets a receive-pack path through to the method rule: classify holds no copy
    monkeypatch.setattr(parse, "walls", lambda p: None)
    v = decide(parse.parse(frame(method="POST", path="/r.git/git-receive-pack")), None, GATED)
    assert (v.kind, v.why) == ("card", "method")


def test_read_shaped_turns_a_non_read_into_a_read_unless_the_query_marked_it():
    seen = []

    def rs(p, route):
        seen.append(p.method)
        return True

    up = frame(method="POST", path="/o/r.git/git-upload-pack", headers={"content-length": "100"})
    assert run(up, GATED, False, read_shaped=rs).kind == "silent"
    assert run(up, OPEN_NETWORK, True, read_shaped=rs).kind == "silent"
    marked = frame(method="POST", path="/o/r.git/git-upload-pack", query="access_token=x",
                   headers={"content-length": "100"})
    assert (run(marked, GATED, False, read_shaped=rs).kind, run(marked, GATED, False, read_shaped=rs).why) == \
        CQ
    assert run(up, GATED, False).kind == "card"        # no predicate given: never read-shaped (fail closed)
    assert seen == ["POST", "POST"]                     # never asked about a marked request


def test_read_shaped_not_asked_for_open_network_unkeyed():
    def rs(p, route):
        raise AssertionError("R-8 is skipped under open network for an unkeyed host")

    assert run(frame(method="POST"), OPEN_NETWORK, False, read_shaped=rs).kind == "silent"


# --- contract checks ---------------------------------------------------------------------------------------------

def test_mode_is_one_of_two():
    with pytest.raises(ValueError):
        run(frame(), "open_network", False)


def test_a_route_for_another_host_is_a_composer_bug():
    p = parse.parse(frame())
    other = FakeRoute("r", "other.example", "s", frozenset(), True)
    with pytest.raises(ValueError):
        decide(p, other, GATED)


# --- forward-after-allow halves: red until S9's composed run ------------------------------------------------------

S9 = pytest.mark.xfail(strict=True, reason="S9 composed run")


@S9
def test_r15_13_q4_allow_forwards_the_override_as_the_card_showed():
    """R15-13-Q4: his allow -> the upstream test server receives the method, path, query and header names the card
    showed, the override present."""
    raise NotImplementedError("needs the composed glue + decider + recording origin (S9)")


@S9
def test_r15_13_q4_origin_receives_no_routing_header():
    """R15-13-Q4 / R15-12-B3: X-Original-URL, X-Rewrite-URL, X-Forwarded-Host -> the origin receives none."""
    raise NotImplementedError("needs the composed run (S9)")


@S9
def test_r15_10_a2_zero_upstream_bytes_before_an_allow():
    """R15-10-A2: a query credential -> zero bytes upstream before an allow (gated) / ever (open network)."""
    raise NotImplementedError("needs the composed run (S9)")


@S9
def test_r15_17_10_open_network_wall_sends_zero_upstream_bytes_and_k1_counts_it():
    """R15-17-10: the wall leaves zero upstream bytes; K1's report counts it (K1's report is S9's)."""
    raise NotImplementedError("needs the composed run and K1's report (S9)")


@S9
def test_r15_18_q2_keyed_plain_post_zero_key_bytes_upstream():
    """R15-18-Q2: a POST to http://<setup host>/ under open network carries zero key bytes upstream."""
    raise NotImplementedError("needs the composed run (S9)")


@S9
def test_r15_18_l2_k1_report_counts_presigned_cards():
    """R15-18-L2 / R15-13-Q3: K1's report counts the presigned cards, the Azure exemptions and sig matches."""
    raise NotImplementedError("K1's report is S9's")
