"""The policy objects the `levain-route-policy/1` pin replays (tests/test_broker_policy_pin.py). Valid routes of each
type, each codec, and the mutations every rule of `parse_route` refuses. Append only: a new row needs a new pin
generated from main's reader, never from the reader under test."""
from __future__ import annotations

import copy


def _brokered(**over):
    base = {"route": "v", "type": "brokered", "upstream": {"scheme": "https", "host": "api.v.example", "base_path": ""},
            "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {"anthropic-version": "2023-06-01"},
                         "body_cap": 1048576, "body_codec": "anthropic-messages"},
            "credential": "vendor-key", "max_call_microdollars": 250000, "max_session_microdollars": 5000000}
    base.update(over)
    return base


def _req(**over):
    r = {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {}, "body_cap": 1, "body_codec": "none"}
    r.update(over)
    return r


def corpus() -> list:
    rows = [
        _brokered(),
        _brokered(credential=None),
        {k: v for k, v in _brokered().items() if k != "credential"},
        {k: v for k, v in _brokered().items() if k != "max_session_microdollars"},
        {k: v for k, v in _brokered().items() if k != "max_call_microdollars"},
        _brokered(max_call_microdollars=0),
        _brokered(max_call_microdollars=True),
        _brokered(max_session_microdollars=0),
        _brokered(max_session_microdollars="5"),
        _brokered(hosts=["api.v.example"]),
        _brokered(hosts=[]),
        _brokered(responses={"headers": ["content-type", "x-ratelimit-remaining"]}),
        _brokered(responses={"headers": ["set-cookie"]}),
        _brokered(prices={}),
        _brokered(price={"in": 1}),
        _brokered(requests=_req(body_codec="openai-chat")),
        _brokered(requests=_req(body_codec="openai-responses")),
        _brokered(requests=_req(body_codec="none")),
        _brokered(requests=_req(body_codec="bogus")),
        _brokered(requests=_req(body_cap=0)),
        _brokered(requests=_req(body_cap=33554433)),
        _brokered(requests=_req(templates=[{"method": "GET", "path": "/v1/x", "upgrade": "websocket"}])),
        _brokered(requests=_req(templates=[{"method": "POST", "path": "/v1/x", "upgrade": "websocket"}])),
        _brokered(requests=_req(templates=[{"method": "POST", "path": "/v1/../x"}])),
        _brokered(requests=_req(templates=[])),
        _brokered(requests=_req(headers={"x-api-key": ".*"})),
        _brokered(requests=_req(headers={"anthropic-beta": "(a"})),
        _brokered(requests={"templates": [{"method": "POST", "path": "/v1/x"}], "headers": {}, "body_cap": 1}),
        _brokered(upstream={"scheme": "https", "host": "api.v.example", "base_path": "/"}),
        _brokered(upstream={"scheme": "https", "host": "api.v.example", "base_path": "/api"}),
        _brokered(upstream={"scheme": "http", "host": "api.v.example", "base_path": ""}),
        _brokered(upstream={"scheme": "https", "host": "127.0.0.1", "base_path": ""}),
        _brokered(upstream={"scheme": "https", "host": "openrouter.ai", "base_path": "/api"}),
        _brokered(route="Bad Route"),
        _brokered(unknown=1),
        {"route": "e", "type": "egress", "hosts": ["pypi.org", "files.pythonhosted.org"]},
        {"route": "e", "type": "egress", "hosts": ["0x7f.0x1"]},
        {"route": "e", "type": "egress", "hosts": ["pypi.org"], "credential": "x"},
        {"route": "p", "type": "subscription_passthrough", "hosts": ["api.anthropic.com"]},
        {"route": "x", "type": "other", "hosts": ["a.b"]},
        "not an object",
        [],
    ]
    return copy.deepcopy(rows)


#: main's `RoutePolicy` fields at the pin (66115cde). A field added since must default to None and is checked apart.
PINNED_FIELDS = ("route", "type", "hosts", "upstream", "credential", "templates", "headers", "body_cap", "body_codec",
                 "response_headers", "max_call_microdollars", "max_session_microdollars")


def render(pol) -> str:
    """A RoutePolicy as text with no object addresses: templates as (method, path, upgrade), patterns as source."""
    out = []
    for name in PINNED_FIELDS:
        v = getattr(pol, name)
        if name == "templates":
            v = [(t.method, t.path, t.upgrade) for t in v]
        elif name == "headers":
            v = None if v is None else sorted((k, p.pattern) for k, p in v.items())
        elif name == "upstream":
            v = None if v is None else (v.scheme, v.host, v.base_path)
        out.append(f"{name}={v!r}")
    return ", ".join(out)
