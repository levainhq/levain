"""levain.broker.norm (CONTRACT §4.7) and the route-policy parser, row by row: T:3a/test_norm_corpus, and the table half of
T:3b/test_refusal_status_table. Reproduced (3b-1a): origin/main had no `levain.broker.norm`, and its front answered every
`/r/` target 503 `keys-unavailable`, a `;` or `%2e` path included, where §4.7 says `template-miss`."""
from __future__ import annotations

import re

import pytest

from levain.broker import gwwire, norm
from levain.broker.policy import PolicyError, parse_route

T = [norm.Template("POST", "/v1/messages?*"), norm.Template("GET", "/v1/items/{seg}"),
     norm.Template("POST", "/v1/exact")]


def _match(method: bytes, path: bytes, query):
    try:
        return norm.match_template(T, method, path, query).path
    except norm.Refused as r:
        return r.name


CORPUS = [
    (b"POST", b"/v1/messages", None, "/v1/messages?*"),
    (b"POST", b"/v1/messages", b"beta=true", "/v1/messages?*"),
    (b"POST", b"/v1/messages", b"a=%25&b=%2e", "/v1/messages?*"),
    (b"POST", b"/v1/exact", b"x=1", "template-miss"),
    (b"POST", b"/v1/exact/", None, "template-miss"),
    (b"GET", b"/v1/items/abc.~:-_9", None, "/v1/items/{seg}"),
    (b"GET", b"/v1/items/a@b", None, "template-miss"),
    (b"GET", b"/v1/items/..", None, "template-miss"),
    (b"GET", b"/v1/items/.", None, "template-miss"),
    (b"POST", b"/v1/%2e%2e/messages", None, "template-miss"),
    (b"POST", b"/v1%2Fmessages", None, "template-miss"),
    (b"POST", b"/v1/messages%25", None, "template-miss"),
    (b"POST", b"/v1/messages%7f", None, "template-miss"),
    (b"POST", b"/v1/messages%5C", None, "template-miss"),
    (b"POST", b"/v1//messages", None, "template-miss"),
    (b"POST", b"/v1/./messages", None, "template-miss"),
    (b"POST", b"/v1\\messages", None, "template-miss"),
    (b"POST", b"/v1/messages;x", None, "template-miss"),
    (b"POST", b"/v1/messages#x", None, "template-miss"),
    (b"POST", "/v1/mé".encode(), None, "template-miss"),
    (b"POST", b"/v1/messages", b"a#b", "template-miss"),
    (b"POST", b"/v1/messages", b"a b", "template-miss"),
    (b"POST", b"/v1/messages", b"a\x01", "template-miss"),
    (b"GET", b"/v1/messages", None, "template-miss"),
    (b"POST", b"v1/messages", None, "template-miss"),
]
_ALLOW = {"content-type": re.compile("application/json"), "anthropic-version": re.compile("[0-9-]{10}")}
HEADERS = [
    ([("Content-Type", "application/json"), ("X-Telemetry", "1")], False, [("content-type", "application/json")]),
    ([("authorization", "Bearer x"), ("x-api-key", "k"), ("accept-encoding", "gzip"), ("host", "h")], False, []),
    ([("connection", "keep-alive")], False, []),
    ([("Connection", "Keep-Alive, close")], False, []),
    ([("connection", "x-other")], False, "malformed"),
    ([("connection", "keep-alive, upgrade")], False, "malformed"),
    ([("te", "trailers")], False, "malformed"),
    ([("proxy-authorization", "x")], False, "malformed"),
    ([("upgrade", "websocket"), ("connection", "upgrade")], False, "malformed"),
    ([("upgrade", "websocket"), ("connection", "upgrade")], True, []),
    ([("content-type", "application/json"), ("content-type", "application/json")], False, "malformed"),
    ([("accept", "a"), ("accept", "b")], False, []),
    ([("anthropic-version", "nope")], False, "policy-header"),
]
POLICIES = [
    ({}, None),
    ({"upstream": {"scheme": "https", "host": "api.v.example", "base_path": "/"}}, "base_path"),
    ({"upstream": {"scheme": "https", "host": "api.v.example", "base_path": "v1"}}, "base_path"),
    ({"upstream": {"scheme": "http", "host": "api.v.example", "base_path": ""}}, "scheme"),
    ({"requests": {"templates": [{"method": "POST", "path": "/v1/../x"}], "headers": {}, "body_cap": 1,
                   "body_codec": "none"}}, "template path"),
    ({"requests": {"templates": [{"method": "POST", "path": "/v1/x", "upgrade": "websocket"}], "headers": {},
                   "body_cap": 1, "body_codec": "none"}}, "only a GET"),
    ({"requests": {"templates": [{"method": "GET", "path": "/v1/x"}], "headers": {"x-api-key": ".*"},
                   "body_cap": 1, "body_codec": "none"}}, "forbidden"),
    ({"responses": {"headers": ["set-cookie"]}}, "forbidden"),
    ({"credential": "vendor-key"}, "credential"),
    ({"max_call_microdollars": 0}, "max_call"),
]


def _route(**over):
    base = {"route": "v", "type": "brokered", "upstream": {"scheme": "https", "host": "api.v.example", "base_path": ""},
            "requests": {"templates": [{"method": "GET", "path": "/v1/x"}], "headers": {}, "body_cap": 1,
                         "body_codec": "none"}, "max_call_microdollars": 1}
    base.update(over)
    return base


def _headers(pairs, upgrade_ok):
    try:
        return norm.check_headers(pairs, _ALLOW, upgrade_ok)
    except norm.Refused as r:
        return r.name


@pytest.mark.parametrize("i", range(len(CORPUS) + len(HEADERS) + len(POLICIES) + 1))
def test_norm_corpus(i):
    if i < len(CORPUS):
        method, path, query, want = CORPUS[i]
        assert _match(method, path, query) == want
        return
    i -= len(CORPUS)
    if i < len(HEADERS):
        pairs, upgrade_ok, want = HEADERS[i]
        assert _headers(pairs, upgrade_ok) == want
        return
    i -= len(HEADERS)
    if i < len(POLICIES):
        over, want = POLICIES[i]
        if want is None:
            assert parse_route(_route(**over), source="session").route == "v"
        else:
            with pytest.raises(PolicyError, match=want):
                parse_route(_route(**over), source="session")
        return
    # the refusal table: every name has a status, every status a reason, and an F naming each one decodes
    assert len(norm.STATUS) == 23
    for name, status in norm.STATUS.items():
        assert status in norm.REASON
        assert gwwire.decode(b"F", gwwire.encode(b"F", {"refused": name})[5:]) == {"refused": name}
    with pytest.raises(gwwire.WireError):
        gwwire.decode(b"F", b'{"refused":"not-a-name"}')
    with pytest.raises(gwwire.WireError):
        gwwire.decode(b"F", b'{"refused":"malformed","refused":"malformed"}')
    with pytest.raises(gwwire.WireError):
        gwwire.decode(b"A", b"")
