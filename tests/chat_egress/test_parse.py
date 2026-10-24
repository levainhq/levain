"""CH-D2 S2: the one normaliser (R-1), canonical host, R-2's path rule and the structural walls R-2 .. R-4.

Rows: R9-A1, R15-10-A5 (the Python layer; srt's own refusal of the same spellings is in test_guard_srt.py),
R15-12-B3 (+ B14, whose red is B3's), R15-16-Q1, R15-21-6 (the canonical-host function).
Pure functions over a decider request frame as srt 0.0.81's `reqFrameFields` builds it; no srt, no network.
"""

from __future__ import annotations

import inspect

import pytest

from levain.chat_egress import parse
from levain.chat_egress.parse import ParseError, ParsedRequest, canonical_host, fold_name, walls

AUDIT = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"


def frame(method="GET", host="example.com", port=443, path="/", query=None, headers=None, sni="default",
          host_header=None, rid=1, audit=True):
    """A `req` frame as srt sends it: lower-case header names (srt lower-cases, it does not fold), list values."""
    hdrs = {k: list(v) for k, v in (headers or {}).items()}
    if audit:
        hdrs.setdefault("x-levain-audit", [AUDIT])
    f = {"t": "req", "id": rid, "method": method, "host": host, "port": port, "path": path}
    if sni == "default":
        sni = host if port == 443 else None
    if sni:
        f["sni"] = sni
    if host_header is not None:
        f["hostHeader"] = host_header
    if query is not None:
        f["query"] = query
    if hdrs:
        f["headers"] = hdrs
    return f


# --- R9-A1: Upgrade / HTTP2-Settings / h2 prior knowledge walled before any silent verdict -----------------------

@pytest.mark.parametrize("headers", [
    {"upgrade": ["websocket"], "connection": ["Upgrade"]},
    {"connection": ["Upgrade, HTTP2-Settings"], "http2-settings": ["AAMAAABkAAQAAP__"]},
    {"connection": ["Upgrade, HTTP2-Settings"]},          # the listing alone names an upgrade
    {"http2_settings": ["AAMAAABkAAQAAP__"]},              # any spelling of the name
    {"Upgrade".lower(): ["h2c"]},
])
@pytest.mark.parametrize("scheme_port", [443, 80])
def test_r9_a1_upgrade_is_wall_protocol(headers, scheme_port):
    p = parse.parse(frame(headers=headers, port=scheme_port))
    assert walls(p) == "protocol"


def test_r9_a1_h2_prior_knowledge_is_wall_protocol():
    assert walls(parse.parse(frame(method="PRI", path="*"))) == "protocol"


def test_r9_a1_walls_take_no_mode():
    """The same verdict under `open network`: the structural walls have no mode input to vary on."""
    assert list(inspect.signature(walls).parameters) == ["parsed"]


def test_r9_a1_plain_get_is_not_walled():
    assert walls(parse.parse(frame(headers={"connection": ["keep-alive"]}))) is None


# --- R15-16-Q1 / R15-10-A5: git-receive-pack is WALLED in both modes, on the canonical target -----------------------

@pytest.mark.parametrize("method,path,query", [
    ("POST", "/org/repo.git/git-receive-pack", None),
    ("GET", "/org/repo.git/info/refs", "service=git-receive-pack"),
    ("POST", "/org/repo.git/git-%72eceive-pack", None),             # R15-10-A5, codex's spelling
    ("POST", "/org/repo.git/git-%72%65ceive-pack", None),
    ("GET", "/org/repo.git/info/refs", "service=git-%72eceive-pack"),
    ("GET", "/org/repo.git/info/refs", "%73ervice=git-receive-pack"),
    ("GET", "/org/repo.git/info/refs", "x=1;service=git-receive-pack"),
    ("POST", "/org/repo.git/git-receive-pack/", None),
])
def test_r15_16_q1_receive_pack_walls(method, path, query):
    p = parse.parse(frame(method=method, path=path, query=query))
    assert walls(p) == "use_w2_push"


def test_r15_16_q1_upload_pack_is_not_walled():
    p = parse.parse(frame(method="GET", path="/org/repo.git/info/refs", query="service=git-upload-pack"))
    assert walls(p) is None


# --- R15-10-A5: ambiguous targets wall `path` (the Python layer) --------------------------------------------------

@pytest.mark.parametrize("path", [
    "/a/%2e%2e/b", "/a/%2E%2e/b", "/a/../b", "/a/./b", "/a/.", "/a/%2e",
    "//a/b", "/a//b", "/a/b//",
    "/a\\b", "/a/%5cb", "/a/%5Cb",
    "/a/%zz", "/a/%4",                                       # a malformed escape
    "/a b", "/a/\x7f",                                       # not one printable-ASCII token
    "a/b",                                                   # not origin form
])
def test_r15_10_a5_ambiguous_path_walls(path):
    p = parse.parse(frame(path=path))
    assert p.canonical_path is None and p.path_problem
    assert walls(p) == "path"


@pytest.mark.parametrize("path,canon", [
    ("/a/%41%7e%2d", "/a/A~-"),                              # unreserved escapes decoded
    ("/a/%2f/b", "/a/%2F/b"),                                # a reserved escape stays escaped (case normalised)
    ("/api/v4/projects/group%2fproject", "/api/v4/projects/group%2Fproject"),
    ("/", "/"),
])
def test_r15_10_a5_canonical_path(path, canon):
    p = parse.parse(frame(path=path))
    assert p.canonical_path == canon and p.path_problem is None and walls(p) is None


def test_options_star_is_a_path():
    p = parse.parse(frame(method="OPTIONS", path="*"))
    assert p.canonical_path == "*" and walls(p) is None
    assert walls(parse.parse(frame(method="GET", path="*"))) == "path"


# --- R-2: Host must name the SNI / CONNECT host ------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"sni": "other.example"},
    {"host_header": "other.example"},
    {"host_header": "example.com:8443"},
    {"port": 80, "sni": None, "host_header": "other.example:80"},
])
def test_r2_host_mismatch(kw):
    assert walls(parse.parse(frame(**kw))) == "host_mismatch"


@pytest.mark.parametrize("kw", [
    {"host_header": "Example.COM"}, {"host_header": "example.com:443"}, {"sni": "EXAMPLE.com."},
    {"port": 80, "sni": None, "host_header": "example.com"},
])
def test_r2_host_match(kw):
    assert walls(parse.parse(frame(**kw))) is None


# --- R15-12-B3 (+ B14): one header-name fold and one query-key parse, shared by every consumer --------------------

def test_r15_12_b3_override_header_any_spelling_reads_folded():
    p = parse.parse(frame(headers={"x_http_method_override": ["DELETE"]}))
    assert "X-HTTP-Method-Override" in p.header_names
    assert "x.http.method.override" in p.header_names
    assert p.header("X-Http-Method-Override") == ("DELETE",)
    assert [h.name for h in p.headers if h.folded == "x-http-method-override"] == ["x_http_method_override"]


def test_r15_12_b3_percent_encoded_query_key_reads_decoded():
    p = parse.parse(frame(query="%5fmethod=DELETE"))
    assert "_method" in p.query_keys
    (q,) = p.params
    assert (q.raw_key, q.key, q.folded, q.value) == ("%5fmethod", "_method", fold_name("_method"), "DELETE")
    assert p.query_values("_METHOD") == ("DELETE",)


def test_r15_12_b3_query_separators_and_decoding_once():
    p = parse.parse(frame(query="a=1;b=2&&c&d=%2541&e+f=g+h"))
    assert [(q.key, q.value, q.has_value) for q in p.params] == [
        ("a", "1", True), ("b", "2", True), ("c", "", False), ("d", "%41", True), ("e f", "g h", True)]


@pytest.mark.parametrize("query", ["a=%zz", "a=%4", "%ff=1", "a=%c3"])
def test_r15_12_b3_undecodable_query_is_a_parse_error(query):
    with pytest.raises(ParseError) as e:
        parse.parse(frame(query=query))
    assert e.value.reason == "query"


@pytest.mark.parametrize("name", ["x-original-url", "X_Original_URL", "x.rewrite.url", "x-forwarded-host",
                                  "X-FORWARDED_HOST"])
def test_r15_12_b14_routing_headers_are_named_for_strip(name):
    """B14's expected verdict is STRIPPED: the routing headers are named, in their folded form (which srt's
    `removeHeaders` matches in any spelling), for every request, whatever its verdict."""
    p = parse.parse(frame(headers={name.lower(): ["/admin/delete"]}))
    assert p.routing_headers == (fold_name(name),)
    assert set(p.routing_headers) <= set(parse.ROUTING_HEADERS)


def test_r15_12_b3_override_names_are_not_routing_headers():
    """R15-13 Q-R15.2-4: the override names are never stripped (the card shows them, an allow forwards them)."""
    assert not set(parse.OVERRIDE_HEADERS) & set(parse.ROUTING_HEADERS)
    p = parse.parse(frame(headers={"x-http-method": ["PUT"], "x-method-override": ["PUT"]}))
    assert p.routing_headers == ()


def test_r15_12_b3_audit_header_is_not_an_agent_header():
    p = parse.parse(frame())
    assert p.request_id == AUDIT
    assert "x-levain-audit" not in p.header_names           # the wrapper's own name is not the agent's request
    assert parse.parse(frame(audit=False)).request_id is None
    two = frame(headers={"x-levain-audit": [AUDIT, AUDIT]})
    assert parse.parse(two).request_id is None              # exactly one value, else the decider denies
    assert parse.parse(frame(headers={"x-levain-audit": ["not-a-uuid"]})).request_id is None


def test_r15_12_b3_parsed_request_is_immutable():
    p = parse.parse(frame(headers={"accept": ["*/*"]}, query="a=1"))
    with pytest.raises(AttributeError):
        p.path = "/x"                                        # type: ignore[misc]
    assert isinstance(p.headers, tuple) and isinstance(p.params, tuple)


@pytest.mark.parametrize("bad", [
    {"t": "req", "id": 1, "method": "GET", "host": "a.com", "port": 443},                # no path
    {"t": "req", "id": 1, "method": "GET", "host": "a.com", "port": "443", "path": "/"},  # port not an int
    {"t": "req", "id": 1, "method": "GET", "host": "a.com", "port": 443, "path": "/", "headers": {"a": "b"}},
    {"t": "req", "id": 1, "method": "GET", "host": "a.com", "port": 443, "path": "/", "extra": 1},
    {"t": "verdict", "id": 1},
])
def test_frame_shape_errors_are_parse_errors(bad):
    with pytest.raises(ParseError) as e:
        parse.parse(bad)
    assert e.value.reason == "frame"


def test_scheme_is_https_iff_sni():
    assert parse.parse(frame()).scheme == "https"
    assert parse.parse(frame(port=80, sni=None)).scheme == "http"


# --- R15-21-6: canonical host, one function -----------------------------------------------------------------------

@pytest.mark.parametrize("raw,canon", [
    ("Example.COM.", "example.com"), ("example.com", "example.com"), ("EXAMPLE.com", "example.com"),
    ("bücher.example", "xn--bcher-kva.example"), ("XN--BCHER-KVA.example", "xn--bcher-kva.example"),
    ("[2001:DB8::1]", "2001:db8::1"), ("2001:0db8:0:0::1", "2001:db8::1"), ("192.0.2.1", "192.0.2.1"),
    ("a_b.example", "a_b.example"),
])
def test_r15_21_6_canonical_host(raw, canon):
    assert canonical_host(raw) == canon


def test_r15_21_6_one_keyed_host_for_two_spellings():
    """The red's first clause at the function level: a setup host `Example.COM.` and a request to `example.com` are
    one keyed host. The setup-time refusal, the mid-launch and the redirect clauses are the keys layer's."""
    req = parse.parse(frame(host="example.com"))
    assert canonical_host("Example.COM.") == req.canonical_host == canonical_host("example.com.")


@pytest.mark.parametrize("raw", [
    "", ".", "example.com..", "a..b", "-a.example", "a-.example", "a b.example", "a/b", "a:443",
    "x" * 64 + ".example", ("a." * 127) + "example",
    "1.2.3", "example.123",                    # ends in a number but is no IP literal (WHATWG reads it as IPv4)
    "fe80::1%en0", "[fe80::1%25en0]",          # a scope id
    "faß.example", "ς.example", "a‍b.example",   # IDNA 2003 / 2008 deviation characters: refused, not mapped
])
def test_r15_21_6_canonical_host_refuses(raw):
    with pytest.raises(ValueError):
        canonical_host(raw)


def test_unparseable_frame_host_walls_non_dns_host():
    p = parse.parse(frame(host="a..b", sni=None, port=80))
    assert p.canonical_host is None and walls(p) == "non_dns_host"


def test_parsed_request_fields_are_the_frozen_surface():
    """The import surface later sessions build on; a change here is a head call (CHD2 build plan §2)."""
    assert {f for f in ParsedRequest.__dataclass_fields__} == {
        "srt_id", "method", "host", "canonical_host", "port", "scheme", "sni", "host_header", "path",
        "canonical_path", "path_problem", "query", "params", "headers", "header_names", "query_keys",
        "request_id", "routing_headers"}


def test_one_fold_function():
    """B3: one fold, not two equal copies (`wire` folds `removeHeaders`, `parse` folds the frame)."""
    from levain.chat_egress import wire
    assert parse.fold_name is wire.fold_name
    assert fold_name("X_Api.Key") == "x-api-key"
