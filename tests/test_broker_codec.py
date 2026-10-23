"""3c-2a session i: the body codecs, the price entry and the priced map formats (3c R4 §1.2, §1.3, §1.6).

Every estimate below was hand-computed BEFORE the codec ran (`project_memory/1023-8/3c2a/a1_expected.md`).
Runs on the root interpreter too (3.9): codec.py and policy.py are imported by the root-run broker.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

from levain.broker import codec, norm, policy, vendor_prices
from levain.broker.policy import PolicyError, parse_base_map, parse_price, parse_route, parse_routemap

sys.path.insert(0, os.path.dirname(__file__))
from _policy_pin_corpus import corpus, render  # noqa: E402

EA_RAW = {"in": 3_000_000, "out": 15_000_000, "cache_write": 3_750_000, "cache_read": 300_000,
          "default_output": 4096, "overhead_tokens": 64, "image_tokens": 1600,
          "per_use": {"web_search": 10_000}, "source": "https://example.test/a"}
EO_RAW = {"in": 2_000_000, "out": 8_000_000, "cache_write": 2_000_000, "cache_read": 500_000,
          "default_output": 4096, "overhead_tokens": 64, "image_tokens": 1600, "source": "https://example.test/o"}
EA = parse_price("EA", EA_RAW)
EO = parse_price("EO", EO_RAW)


def _go(cname, body, entry, *, dispatch=False, model_requested=None):
    """§1.7 step 4-5 in order: parse, check, write_ceiling, estimate; the forwarded bytes. A refusal = its name."""
    c = codec.CODECS[cname]
    try:
        obj = c.parse(body.encode() if isinstance(body, str) else body)
        chk = codec.guarded(c.check, obj, {"m1": entry}, dispatch=dispatch, model_requested=model_requested)
        codec.guarded(c.write_ceiling, obj, chk.price)
        return codec.guarded(c.estimate, obj, chk.price), chk.unproven, codec.serialize(obj).decode()
    except norm.Refused as r:
        return r.name


def test_codec_table_is_the_policy_codec_set():
    assert set(codec.CODECS) == set(policy.BODY_CODECS) - {"none"}
    assert all(c.name == n for n, c in codec.CODECS.items())


E2_FWD = '{"model":"m1","n":3,"messages":[{"role":"user","content":"hi"}],"max_completion_tokens":4096}'
ESTIMATES = [
    ("E1", "anthropic-messages", '{"model":"m1","max_tokens":1000,"messages":[{"role":"user","content":"hi"}]}',
     EA, 15525, ()),
    ("E2", "openai-chat", '{"model":"m1","n":3,"messages":[{"role":"user","content":"hi"}]}', EO, 98618, ()),
    ("E3", "openai-responses", '{"model":"m1","max_output_tokens":500,"input":[{"role":"user","content":'
     '[{"type":"input_image","image_url":"data:image/png;base64,AAAA"}]}]}', EO, 7556, ()),
    ("E4", "anthropic-messages", '{"model":"m1","max_tokens":100,"tools":[{"type":"web_search_20250305",'
     '"name":"web_search","max_uses":5}],"messages":[]}', EA, 52187, ("web_search",)),
    ("E5", "anthropic-messages", '{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":[{"type":'
     '"image","source":{"type":"base64","media_type":"image/png","data":"QUJDRA=="}}]}]}', EA, 6953, ()),
    ("R14", "openai-chat", '{"model":"m1","messages":[{"role":"user","content":"hi"}]}', EO, 33070, ()),
]


@pytest.mark.parametrize("row", ESTIMATES, ids=[r[0] for r in ESTIMATES])
def test_estimate_hand_computed(row):
    _, cname, body, entry, want, unproven = row
    got = _go(cname, body, entry)
    assert got[:2] == (want, unproven)


def test_absent_ceiling_written_in_and_multiplied_by_n():
    est, _, fwd = _go("openai-chat", '{"model":"m1","n":3,"messages":[{"role":"user","content":"hi"}]}', EO)
    assert fwd == E2_FWD
    responses = _go("openai-responses", '{"model":"m1","input":"x"}', EO)[2]
    assert json.loads(responses)["max_output_tokens"] == 4096


def test_stated_ceiling_never_written_over():
    body = '{"model":"m1","max_tokens":50,"messages":[]}'
    est, _, fwd = _go("openai-chat", body, EO)
    assert fwd == body and est == -(-((len(body) + 64) * 2_000_000) // 1_000_000) + 50 * 8
    # Anthropic requires max_tokens: the codec never writes one in (the provider refuses the call)
    assert "max_tokens" not in _go("anthropic-messages", '{"model":"m1","messages":[]}', EA)[2]


def test_both_openai_chat_ceilings_take_the_larger():
    body = '{"model":"m1","max_tokens":10,"max_completion_tokens":20,"messages":[]}'
    est = _go("openai-chat", body, EO)[0]
    assert est == -(-((len(body) + 64) * 2_000_000) // 1_000_000) + 20 * 8


def test_image_data_excluded_from_bytes():
    small = _go("openai-chat", '{"model":"m1","max_tokens":1,"messages":[{"role":"user","content":[{"type":'
                '"image_url","image_url":{"url":"data:image/png;base64,QQ=="}}]}]}', EO)[0]
    big = _go("openai-chat", '{"model":"m1","max_tokens":1,"messages":[{"role":"user","content":[{"type":'
              '"image_url","image_url":{"url":"data:image/png;base64,' + "Q" * 100000 + '"}}]}]}', EO)[0]
    assert small == big


REFUSALS = [
    ("R1", "anthropic-messages", "{", {}, "body-json"),
    ("R2", "anthropic-messages", "[" * 100000, {}, "body-json"),
    ("R3", "anthropic-messages", "[]", {}, "body-json"),
    ("R4", "openai-chat", '{"model":"m1","temperature":NaN}', {}, "body-json"),
    ("R4b", "openai-chat", b'{"model":"m1","x":"\xff"}', {}, "body-json"),
    ("R5", "anthropic-messages", '{"model":"nope","max_tokens":1}', {}, "model-not-allowed"),
    ("R5b", "anthropic-messages", '{"max_tokens":1}', {}, "model-not-allowed"),
    ("R6", "anthropic-messages", '{"model":"m1","max_tokens":1}', {"model_requested": "m2"}, "model-not-allowed"),
    ("R8", "anthropic-messages", '{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":[{"type":'
     '"document","source":{"type":"base64","media_type":"application/pdf","data":"QQ=="}}]}]}',
     {"dispatch": True}, "body-unbounded:document"),
    ("R8b", "anthropic-messages", '{"model":"m1","max_tokens":10,"tools":[{"type":"web_fetch_20250910",'
     '"name":"web_fetch"}],"messages":[]}', {"dispatch": True}, "body-unbounded:web_fetch"),
    ("R9", "openai-responses", '{"model":"m1","previous_response_id":"resp_1","input":"x"}', {"dispatch": True},
     "body-unbounded:previous_response_id"),
    ("R9b", "openai-responses", '{"model":"m1","tools":[{"type":"web_search"}],"input":"x"}', {"dispatch": True},
     "body-unbounded:web_search"),
    ("R10", "openai-chat", '{"model":"m1","plugins":[{"id":"web"}],"messages":[]}', {"dispatch": True},
     "body-unbounded:plugins"),
    ("R12", "anthropic-messages", '{"model":"m1","max_tokens":"100"}', {}, "body-json"),
    ("R12b", "openai-chat", '{"model":"m1","max_tokens":true}', {}, "body-json"),
    ("R12c", "openai-chat", '{"model":"m1","n":"3"}', {}, "body-json"),
]


@pytest.mark.parametrize("row", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_refusal_shapes(row):
    _, cname, body, kw, want = row
    entry = EA if cname == "anthropic-messages" else EO
    assert _go(cname, body, entry, **kw) == want
    assert norm.status_of(want) == {"body-json": 400}.get(want, 403)


def test_unproven_marked_interactive_refused_dispatch():
    body = ('{"model":"m1","max_tokens":10,"tools":[{"type":"web_search_20250305","name":"web_search"}],'
            '"messages":[{"role":"user","content":[{"type":"document","source":{"type":"url","url":"https://x.test/a.pdf"}}]}]}')
    assert _go("anthropic-messages", body, EA)[1] == ("web_search", "document")
    assert _go("anthropic-messages", body, EA, dispatch=True) == "body-unbounded:web_search"


def test_websocket_refused_on_metered_route():
    with pytest.raises(norm.Refused) as e:
        codec.refuse_upgrade()
    assert e.value.name == "body-unbounded:websocket" and e.value.status == 403


def test_recursion_in_a_later_step_is_body_json(monkeypatch):
    # the image strip walks content positions only, so no later step recurses on the body's depth itself; the one
    # recursive step left is json.dumps, whose C limit is not sys.getrecursionlimit on 3.12. Either way, never bare.
    deep: object = "x"
    for _ in range(sys.getrecursionlimit() + 50):
        deep = {"a": deep}
    obj = {"model": "m1", "max_tokens": 1, "messages": [deep]}
    try:
        assert isinstance(codec.guarded(codec.CODECS["anthropic-messages"].estimate, obj, EA), int)
    except norm.Refused as e:
        assert e.name == "body-json"

    def deep_dumps(*a, **k):
        raise RecursionError
    monkeypatch.setattr(codec.json, "dumps", deep_dumps)
    with pytest.raises(norm.Refused) as e:
        codec.guarded(codec.CODECS["anthropic-messages"].estimate, {"model": "m1", "max_tokens": 1}, EA)
    assert e.value.name == "body-json"


def test_forwarded_body_is_the_compact_serialization():
    est, _, fwd = _go("anthropic-messages", '{ "model" : "m1",\n "max_tokens": 1, "messages": [] }', EA)
    assert fwd == '{"model":"m1","max_tokens":1,"messages":[]}'


def test_refused_names_on_the_wire():
    from levain.broker import gwwire
    for name in ("body-json", "body-unbounded:document", "body-unbounded:websocket"):
        assert gwwire.decode(b"F", gwwire.encode(b"F", {"refused": name})[5:]) == {"refused": name}
    for bad in ("body-unbounded:", "body-unbounded:Bad", "body-json:x", "model-not-allowed:x"):
        assert not norm.is_name(bad)


# --- the price entry ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key,val", [("in", 0), ("out", -1), ("in", True), ("cache_read", 1.5), ("default_output", 0),
                                     ("image_tokens", 0), ("in", 10 ** 12 + 1), ("source", "http://x.test"),
                                     ("source", "https://x.test/a b"), ("per_use", {"Web": 1}),
                                     ("per_use", {"web_search": 0}), ("per_use", []), ("extra", 1)])
def test_price_entry_refused(key, val):
    raw = dict(EA_RAW, **{key: val})
    with pytest.raises(PolicyError):
        parse_price("t", raw)


def test_price_entry_missing_key_refused_and_zero_cache_accepted():
    for k in ("in", "out", "cache_write", "cache_read", "default_output", "overhead_tokens", "image_tokens", "source"):
        with pytest.raises(PolicyError):
            parse_price("t", {x: v for x, v in EA_RAW.items() if x != k})
    p = parse_price("t", dict(EA_RAW, cache_read=0, cache_write=0, overhead_tokens=0))
    assert (p.cache_read, p.cache_write, p.overhead_tokens) == (0, 0, 0)


# --- base /2 and routemap /3 --------------------------------------------------------------------------------------


def _brokered(**over):
    base = {"route": "anth", "type": "brokered",
            "upstream": {"scheme": "https", "host": "api.anthropic.com", "base_path": ""},
            "requests": {"templates": [{"method": "POST", "path": "/v1/messages"}], "headers": {},
                         "body_cap": 1048576, "body_codec": "anthropic-messages"},
            "credential": "anth-key", "max_session_microdollars": 5_000_000}
    base.update(over)
    return base


def test_base_v2_reads_prices_and_refuses_per_call():
    m = parse_base_map({"format": "levain-routes-base/2", "routes": [
        _brokered(prices={"m1": EA_RAW}), {"route": "pypi", "type": "egress", "hosts": ["pypi.org"]}]})
    assert m["anth"].prices == {"m1": EA} and m["anth"].max_call_microdollars is None
    assert m["pypi"].prices is None
    for bad in ([_brokered()],                                                    # no prices
                [_brokered(prices={"m1": EA_RAW}, max_call_microdollars=1)],      # a per-call cap
                [_brokered(prices={"m1": dict(EA_RAW, in_=1)})],                  # a bad entry
                [_brokered(prices={"m1": EA_RAW}, requests=dict(_brokered()["requests"], body_cap=policy.BODY_CAP_MAX + 1))],
                [{"route": "pypi", "type": "egress", "hosts": ["pypi.org"], "prices": {}}],
                [_brokered(prices={"m1": EA_RAW}), _brokered(prices={"m1": EA_RAW})]):
        with pytest.raises(PolicyError):
            parse_base_map({"format": "levain-routes-base/2", "routes": bad})


def test_base_v1_reads_as_main():
    one = _brokered(max_call_microdollars=250_000)
    m = parse_base_map({"format": "levain-routes-base/1", "routes": [one]})
    assert render(m["anth"]) == render(parse_route(copy.deepcopy(one), source="base")) and m["anth"].prices is None
    with pytest.raises(PolicyError):
        parse_base_map({"format": "levain-routes-base/1", "routes": [_brokered(prices={"m1": EA_RAW})]})
    with pytest.raises(PolicyError):
        parse_base_map({"format": "levain-routes-base/3", "routes": []})


def _dispatch_route(**over):
    pol = {k: v for k, v in _brokered().items() if k != "max_session_microdollars"}
    r = {"policy": pol, "provider": "openrouter",
         "models": [{"requested": "z-ai/glm-5.2", "backend": "zhipu/glm-5.2", "price": EO_RAW}],
         "body_policy": {"provider_only": ["z-ai"]}, "retention_claim": "recorded, never a guarantee"}
    r.update(over)
    return r


def test_routemap_v3():
    [r] = parse_routemap({"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route()]})
    assert r.policy.prices == {"z-ai/glm-5.2": EO} and r.backends == {"z-ai/glm-5.2": "zhipu/glm-5.2"}
    assert r.policy.max_call_microdollars is None
    pol_cap = dict(_dispatch_route()["policy"], max_call_microdollars=1)
    for bad in ({"format": "levain-dispatch-routemap/2", "routes": [_dispatch_route()]},
                {"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(policy=pol_cap)]},
                {"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(
                    models=[{"requested": "z-ai/glm-5.2", "backend": "zhipu/glm-5.2"}])]},
                {"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(models=[])]},
                {"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(), _dispatch_route()]},
                {"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(extra=1)]}):
        with pytest.raises(PolicyError):
            parse_routemap(bad)


# --- the frozen readers and the vendor table ----------------------------------------------------------------------


def test_route_policy_v1_reader_pinned_to_main():
    """main's `parse_route` (66115cde) on the corpus, every source, compared result by result."""
    with open(os.path.join(os.path.dirname(__file__), "data", "policy_pin_main_66115cde.json")) as f:
        pinned = json.load(f)
    rows = corpus()
    got = []
    for i, src, _ in pinned:
        try:
            pol = parse_route(copy.deepcopy(rows[i]), source=src)
            assert pol.prices is None
            got.append([i, src, "OK " + render(pol)])
        except PolicyError as e:
            got.append([i, src, "PolicyError " + str(e)])
        except Exception as e:
            got.append([i, src, type(e).__name__ + " " + str(e)])
    assert got == pinned


def test_vendor_table_parses_and_names_its_derivation():
    table = vendor_prices.prices()
    assert all(p.source.startswith("https://") for p in table.values())
    # the reviewed docstring (1e77173a) names the head's ruling as the model list's source; the earlier `git grep`
    # derivation printed nothing and was replaced (1023+13 head, ii-b RETURNED)
    assert "The model list is the head's ruling" in (vendor_prices.__doc__ or "")


# --- L3 r1 on ii-b2 (lane 3c2a-b2-fix1) ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_a_priced_route_with_a_bodyless_template_is_refused_at_load(method):
    # complement LOW 4: a priced route's template whose method carries no body (a GET /models) answered `body-json`
    # on every call, since keys parses every metered body. The map is refused at load instead, base /2 and
    # routemap /3 alike; a /1 base map (no prices) keeps main's reading.
    req = dict(_brokered()["requests"], templates=[{"method": "POST", "path": "/v1/messages"},
                                                   {"method": method, "path": "/v1/models"}])
    with pytest.raises(PolicyError, match="carries no body"):
        parse_base_map({"format": "levain-routes-base/2", "routes": [_brokered(prices={"m1": EA_RAW}, requests=req)]})
    pol = {k: v for k, v in _brokered(requests=req).items() if k != "max_session_microdollars"}
    with pytest.raises(PolicyError, match="carries no body"):
        parse_routemap({"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(policy=pol)]})
    m = parse_base_map({"format": "levain-routes-base/1",
                        "routes": [_brokered(max_call_microdollars=250_000, requests=req)]})
    assert [t.method for t in m["anth"].templates] == ["POST", method]


@pytest.mark.parametrize("model", ["mé", "ｍ1", "m 1", "m\x7f", "m\t1", ""])
def test_a_price_model_outside_printable_ascii_is_refused(model):
    # head ruling Q4 (ii-b RETURNED): `price_ref` is the model id, and the ledger refuses a non-ASCII ref, so the
    # price table refuses such a model at load. [measured by 3c2a-b2-fix1, 10-23: `_MODEL_ID` already refused every
    # one of these on 0497ccc6 (b2f1_failfirst.txt); this test pins it, no code changed]
    with pytest.raises(PolicyError):
        policy.parse_prices("anth", {model: EA_RAW})


# --- 3c2a-i L3 r1 (1023-8/3c2a/L3_a1_r1.txt): each test is its finding's own failure ------------------------------


@pytest.mark.parametrize("cname,body,what", [
    # codex + glm HIGH: `{}` ENABLES Chat Completions web search; presence is what turns it on
    ("openai-chat", '{"model":"m1","messages":[],"web_search_options":{}}', "web_search_options"),
    ("openai-chat", '{"model":"m1","messages":[],"web_search_options":null}', "web_search_options"),
    # complement LOW 6: 0 compared == to False, so it read as absent
    ("openai-chat", '{"model":"m1","messages":[],"plugins":0}', "plugins"),
    ("openai-responses", '{"model":"m1","input":"x","previous_response_id":0}', "previous_response_id"),
    ("openai-responses", '{"model":"m1","input":"x","conversation":{}}', "conversation"),
    ("anthropic-messages", '{"model":"m1","max_tokens":10,"messages":[],"mcp_servers":0}', "mcp_servers"),
    ("anthropic-messages", '{"model":"m1","max_tokens":10,"messages":[],"mcp_servers":{}}', "mcp_servers"),
])
def test_unbounded_field_by_what_disables_it(cname, body, what):
    assert _go(cname, body, EA if cname.startswith("anthropic") else EO, dispatch=True) == f"body-unbounded:{what}"


@pytest.mark.parametrize("cname,body", [
    ("openai-chat", '{"model":"m1","messages":[],"plugins":[]}'),
    ("openai-responses", '{"model":"m1","input":"x","previous_response_id":null,"conversation":null}'),
    ("anthropic-messages", '{"model":"m1","max_tokens":10,"messages":[],"mcp_servers":[]}'),
])
def test_disabled_spellings_stay_bounded(cname, body):
    got = _go(cname, body, EA if cname.startswith("anthropic") else EO, dispatch=True)
    assert not isinstance(got, str) and got[1] == ()


@pytest.mark.parametrize("tool", ["web_search_2025_08_26", "web_search_preview_2025_03_11"])
def test_dated_snapshot_tool_refused_on_dispatch(tool):
    # codex HIGH 2: OpenAI's snapshot names are `_YYYY_MM_DD`, which `_\d{8}` missed
    body = '{"model":"m1","input":"x","tools":[{"type":"%s"}]}' % tool
    assert _go("openai-responses", body, EO, dispatch=True) == "body-unbounded:" + tool.rsplit("_", 3)[0]


def test_dated_snapshot_tool_priced_per_use():
    entry = parse_price("EO", dict(EO_RAW, per_use={"web_search": 10_000}))
    dated = '{"model":"m1","input":"x","max_output_tokens":10,"tools":[{"type":"web_search_2025_08_26","max_uses":2}]}'
    assert _go("openai-responses", dated, entry)[0] - _go("openai-responses", dated, EO)[0] == 2 * 10_000


@pytest.mark.parametrize("lit", ["1e999", "-1e999", "1E400"])
def test_non_finite_number_is_body_json(lit):
    # complement + codex MED: an overflowing literal parses to inf without calling parse_constant
    body = '{"model":"m1","max_tokens":10,"temperature":%s,"messages":[]}' % lit
    assert _go("anthropic-messages", body, EA) == "body-json"


def test_serialize_failure_is_body_json():
    with pytest.raises(norm.Refused) as e:
        codec.serialize({"x": float("inf")})
    assert e.value.name == "body-json"
    with pytest.raises(norm.Refused) as e:
        codec.serialize({"x": "\ud800"})
    assert e.value.name == "body-json"


_FAKE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 4000}}


@pytest.mark.parametrize("where", ["schema", "tool_use"])
def test_fake_image_off_position_counts_in_full(where):
    # complement MED 2: only a content position is an image; elsewhere the provider reads the bytes as text
    if where == "schema":
        body = {"model": "m1", "max_tokens": 10, "messages": [],
                "tools": [{"name": "t", "input_schema": {"type": "object", "default": _FAKE}}]}
    else:
        body = {"model": "m1", "max_tokens": 10, "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "t", "input": {"x": _FAKE}}]}]}
    est, _, fwd = _go("anthropic-messages", json.dumps(body), EA)
    in_est = len(fwd.encode()) + EA.overhead_tokens            # every forwarded byte, no image term
    assert est == -(-(in_est * EA.cache_write) // 1_000_000) + -(-(10 * EA.out) // 1_000_000)


def test_images_at_content_positions_are_stripped():
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 4000}}
    body = {"model": "m1", "max_tokens": 10, "messages": [{"role": "user", "content": [
        img, {"type": "tool_result", "tool_use_id": "a", "content": [img]}]}]}
    est, _, fwd = _go("anthropic-messages", json.dumps(body), EA)
    stripped = fwd.replace("A" * 4000, "")
    in_est = len(stripped.encode()) + 2 * EA.image_tokens + EA.overhead_tokens
    assert est == -(-(in_est * EA.cache_write) // 1_000_000) + -(-(10 * EA.out) // 1_000_000)
    chat = {"model": "m1", "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}}]}],
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {
            "default": {"type": "image_url", "image_url": "data:" + "B" * 4000}}}}]}
    est, _, fwd = _go("openai-chat", json.dumps(chat), EO)
    in_est = len(fwd.replace("data:image/png;base64," + "A" * 4000, "").encode()) + EO.image_tokens + EO.overhead_tokens
    assert est == -(-(in_est * EO.in_) // 1_000_000) + -(-(10 * EO.out) // 1_000_000)


def test_non_ascii_serialized_as_utf8():
    # complement MED 3: ensure_ascii escaped each CJK char to 6 bytes
    body = '{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":"' + "中" * 100 + '"}]}'
    est, _, fwd = _go("anthropic-messages", body, EA)
    assert "中" * 100 in fwd and len(codec.serialize(json.loads(fwd))) == len(fwd.encode("utf-8"))
    lone = '{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":"\\ud800"}]}'
    assert _go("anthropic-messages", lone, EA) == "body-json"


def test_serialized_body_above_cap_max_refused(monkeypatch):
    monkeypatch.setattr(policy, "BODY_CAP_MAX", 200)
    body = '{"model":"m1","max_tokens":10,"messages":[{"role":"user","content":"' + "x" * 300 + '"}]}'
    assert _go("anthropic-messages", body, EA) == "body-cap"


def test_base_v2_brokered_codec_none_refused():
    # codex MED 3
    req = dict(_brokered()["requests"], body_codec="none")
    with pytest.raises(PolicyError):
        parse_base_map({"format": "levain-routes-base/2", "routes": [_brokered(prices={"m1": EA_RAW}, requests=req)]})


@pytest.mark.parametrize("over", [
    {"body_policy": {"zdr": 1}},                                       # codex MED 5: a value is text or a list of text
    {"body_policy": {"k" + str(i): "v" for i in range(257)}},          # the 256-entry cap
    {"provider": "x‮y"},                                          # /2's text rule on a text field
    {"models": [{"requested": "m%d" % i, "backend": "b", "price": EO_RAW} for i in range(257)]},
])
def test_routemap_v3_keeps_v2_validators(over):
    with pytest.raises(PolicyError):
        parse_routemap({"format": "levain-dispatch-routemap/3", "routes": [_dispatch_route(**over)]})


def test_routemap_v3_route_count_cap():
    routes = [_dispatch_route(policy=dict(_dispatch_route()["policy"], route="r%d" % i)) for i in range(257)]
    with pytest.raises(PolicyError):
        parse_routemap({"format": "levain-dispatch-routemap/3", "routes": routes})


def test_null_ceiling_is_absent_and_written_over():
    # complement LOW 4
    _, _, fwd = _go("openai-chat", '{"model":"m1","messages":[],"max_tokens":null}', EO)
    assert json.loads(fwd)["max_tokens"] == EO.default_output and "max_completion_tokens" not in json.loads(fwd)
    got = _go("openai-chat", '{"model":"m1","messages":[],"max_tokens":null,"max_completion_tokens":null}', EO)
    fwd = json.loads(got[2])
    assert fwd["max_completion_tokens"] == EO.default_output and fwd.get("max_tokens") is None and "max_tokens" not in fwd


def test_price_is_hashable_and_per_use_immutable():
    # complement LOW 5
    assert hash(EA) == hash(parse_price("EA", EA_RAW)) and {EA, EO}
    with pytest.raises(TypeError):
        EA.per_use["web_search"] = 1


def test_every_unbounded_field_has_a_disabled_by_row():
    assert {f for c in codec.CODECS.values() for f in c.unbounded_fields} == set(codec._DISABLED_BY)


# codex MED (L3_a1_r2.txt): image positions in Responses tool OUTPUTS and prompt variables, from the Responses
# create reference (1023-8/3c2a/A1_FIX2_RETURN.md names the pages). Each position: a small URL image counts one
# image at image_tokens; a large data-URL image there has its data out of the byte term.
_BIG = "data:image/png;base64," + "A" * 4000


def _resp_at(pos, image_url):
    img = {"type": "input_image", "image_url": image_url}
    body = {"model": "m1", "max_output_tokens": 10}
    if pos == "function_call_output":
        body["input"] = [{"type": "function_call_output", "call_id": "c", "output": [img]}]
    elif pos == "custom_tool_call_output":
        body["input"] = [{"type": "custom_tool_call_output", "call_id": "c", "output": [img]}]
    elif pos == "computer_call_output":
        body["input"] = [{"type": "computer_call_output", "call_id": "c",
                          "output": {"type": "computer_screenshot", "image_url": image_url}}]
    else:
        body["input"] = "x"
        body["prompt"] = {"id": "pmpt_1", "variables": {"pic": img, "who": "a"}}
    return body


_RESP_POSITIONS = ["function_call_output", "custom_tool_call_output", "computer_call_output", "prompt_variable"]


def _resp_est(fwd_bytes, n):
    in_est = fwd_bytes + n * EO.image_tokens + EO.overhead_tokens
    return -(-(in_est * EO.in_) // 1_000_000) + -(-(10 * EO.out) // 1_000_000)


@pytest.mark.parametrize("pos", _RESP_POSITIONS)
def test_responses_small_url_image_counts_at_image_tokens(pos):
    body = _resp_at(pos, "https://example.test/a.png")
    assert codec.CODECS["openai-responses"]._strip_images(body)[1] == 1
    est, _, fwd = _go("openai-responses", json.dumps(body), EO)
    assert est == _resp_est(len(fwd.encode()), 1)


@pytest.mark.parametrize("pos", _RESP_POSITIONS)
def test_responses_data_url_image_data_out_of_bytes(pos):
    body = _resp_at(pos, _BIG)
    est, _, fwd = _go("openai-responses", json.dumps(body), EO)
    assert _BIG in fwd
    assert est == _resp_est(len(fwd.replace(_BIG, "").encode()), 1)


def test_responses_image_shape_off_a_tool_output_item_counts_in_full():
    # the position is the item's documented type, not the shape: an input_image under `output` of an item that is
    # not a tool-call output is text the provider reads.
    body = {"model": "m1", "max_output_tokens": 10,
            "input": [{"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}",
                       "output": [{"type": "input_image", "image_url": _BIG}]}]}
    assert codec.CODECS["openai-responses"]._strip_images(body)[1] == 0
    est, _, fwd = _go("openai-responses", json.dumps(body), EO)
    assert est == _resp_est(len(fwd.encode()), 0)


@pytest.mark.parametrize("cname,body", [
    ("openai-chat", {"model": "m1", "max_tokens": 10, "messages": [{"role": "user", "content": "x"}]}),
    ("openai-responses", {"model": "m1", "max_output_tokens": 10, "input": [
        {"role": "user", "content": [{"type": "input_image", "image_url": "https://example.test/a.png"}]}]}),
])
def test_estimate_serializes_once_when_nothing_is_stripped(monkeypatch, cname, body):
    # complement LOW (L3_a1_r2.txt): reuse the cap check's serialization when no image data was emptied.
    c = codec.CODECS[cname]
    calls = []
    real = codec.serialize
    monkeypatch.setattr(codec, "serialize", lambda o: calls.append(1) or real(o))
    c.estimate(body, EO)
    assert len(calls) == 1


# --- 3c-2b: the usage reader and the price by class (R4 §1.2, §1.3, §1.7 step 8) -------------------------------------
# The responses are `tests/usage_responses.py`, the bytes lane 3c2b's step-1 run relayed through keys as a child
# process (`project_memory/1023-15/3c2b/run1_main_ledger.json`). Expected usages written from the fixtures' numbers
# before the reader ran.

from tests import usage_responses as UR  # noqa: E402

CT_JSON, CT_SSE = b"application/json", b"text/event-stream; charset=utf-8"


def _read(cname, data, stream, step=None):
    """The usage `cname`'s side reader reports for `data`, fed in reads of `step` bytes (all at once when None)."""
    r = codec.CODECS[cname].usage(CT_SSE if stream else CT_JSON)
    step = step or max(1, len(data))
    for i in range(0, len(data), step):
        r.feed(data[i:i + step])
    return r.result()


def _u(i, o, cw, cr, uses=()):
    return codec.Usage(in_=i, out=o, cache_write=cw, cache_read=cr, uses=tuple(uses))


STEPS = [None, 1, 7, 4096]


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("stream,want", [
    (False, _u(2095, 503, 2051, 4000, [("web_search", 2)])),
    # message_start's counts with the last message_delta's (cumulative) over them
    (True, _u(472, 15, 100, 200)),
])
def test_codec_usage_anthropic_messages(stream, want, step):
    assert _read("anthropic-messages", UR.body("anthropic-messages", stream), stream, step) == want


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("stream,want", [
    (False, _u(1117 - 1024, 46, 0, 1024)),   # prompt_tokens holds the cached ones
    (True, _u(300 - 128, 40, 0, 128)),       # the final chunk's usage; `[DONE]` is no JSON event
])
def test_codec_usage_openai_chat(stream, want, step):
    assert _read("openai-chat", UR.body("openai-chat", stream), stream, step) == want


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("stream", [False, True])
def test_codec_usage_openai_responses(stream, step):
    assert _read("openai-responses", UR.body("openai-responses", stream), stream, step) == _u(328 - 64, 52, 0, 64)


def test_codec_usage_openrouter_cache_writes_priced_apart():
    # OpenRouter's `prompt_tokens_details.cache_write_tokens` (its usage-accounting page's example): out of `in`
    body = json.dumps({"usage": {"prompt_tokens": 194, "completion_tokens": 2, "cost": 0.95,
                                 "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 100}}}).encode()
    assert _read("openai-chat", body, False) == _u(94, 2, 100, 0)


def test_codec_usage_responses_incomplete_reports_usage():
    ev = UR._sse([("response.incomplete", {"type": "response.incomplete", "response": {
        "status": "incomplete", "usage": {"input_tokens": 5, "output_tokens": 10}}})])
    assert _read("openai-responses", ev, True) == _u(5, 10, 0, 0)


def test_codec_usage_crlf_lines():
    data = UR.body("anthropic-messages", True).replace(b"\n", b"\r\n")
    assert _read("anthropic-messages", data, True, 5) == _u(472, 15, 100, 200)


@pytest.mark.parametrize("cname", sorted(codec.CODECS))
def test_codec_usage_over_cap_event_is_no_usage(cname):
    # one SSE event past USAGE_EVENT_MAX stops the side reader; the usage after it is never read
    big = b"data: " + b"x" * (codec.USAGE_EVENT_MAX + 1) + b"\n\n"
    r = codec.CODECS[cname].usage(CT_SSE)
    r.feed(big[:100])
    r.feed(big[100:])
    r.feed(UR.body(cname, True))
    assert r.result() is None and r.stopped == "event-over-cap"


@pytest.mark.parametrize("cname", sorted(codec.CODECS))
def test_codec_usage_over_cap_event_in_many_lines_is_no_usage(cname):
    # the cap is on the EVENT held, not one line: many short `data:` lines with no blank line between them
    line = b"data: " + b"y" * 1000 + b"\n"
    r = codec.CODECS[cname].usage(CT_SSE)
    for _ in range(codec.USAGE_EVENT_MAX // 1000 + 2):
        r.feed(line)
    r.feed(b"\n" + UR.body(cname, True))
    assert r.result() is None and r.stopped == "event-over-cap"


@pytest.mark.parametrize("cname", sorted(codec.CODECS))
def test_codec_usage_over_cap_body_is_no_usage(cname):
    # a non-stream body past USAGE_COPY_MAX is not copied whole, so its usage is never read
    data = UR.body(cname, False)
    pad = b" " * (codec.USAGE_COPY_MAX - len(data) + 1)   # JSON whitespace: the body still parses, it is just long
    assert _read(cname, data[:-1] + pad + data[-1:], False, 65536) is None
    assert _read(cname, data[:-1] + pad[:-1] + data[-1:], False, 65536) is not None   # at the cap exactly: read


@pytest.mark.parametrize("cname,data,stream", [
    ("anthropic-messages", UR.body("anthropic-messages", True).split(b"event: message_delta")[0], True),  # cut
    ("openai-chat", UR._sse(UR.OPENAI_CHAT_SSE[:2] + [(None, "[DONE]")]), True),   # no include_usage
    ("openai-responses", UR._sse(UR.OPENAI_RESPONSES_SSE[:2]), True),               # no response.completed
    ("openai-chat", b'{"usage":{"prompt_tokens":-1,"completion_tokens":2}}', False),
    ("openai-chat", b'{"usage":{"prompt_tokens":true,"completion_tokens":2}}', False),
    ("anthropic-messages", b'{"usage":{"input_tokens":1}}', False),                 # no output count
    ("anthropic-messages", b'{"type":"error","error":{"type":"overloaded_error"}}', False),
    ("openai-responses", b'{"usage":{"input_tokens":1,"output_tokens":2', False),   # cut mid-body
    ("anthropic-messages", b'[' * 100000 + b']' * 100000, False),                   # RecursionError
    ("openai-chat", b"data: " + b"[" * 100000 + b"\n\n", True),
])
def test_codec_usage_none_reported(cname, data, stream):
    assert _read(cname, data, stream) is None


def test_codec_usage_last_event_without_blank_line_is_discarded():
    data = UR.body("openai-chat", True)
    cut = data[:data.index(b"data: [DONE]")].rstrip(b"\n")   # the usage chunk not ended by its blank line
    assert _read("openai-chat", cut, True) is None


def test_price_usage_by_class_rounded_up():
    # 2095 x 3 + 503 x 15 + ceil(2051 x 3.75) + 4000 x 0.3 + 2 x 10000 = 6285 + 7545 + 7692 + 1200 + 20000
    u = _read("anthropic-messages", UR.body("anthropic-messages", False), False)
    assert codec.price_usage(u, EA) == 42722
    assert u.meta() == {"usage": {"in": 2095, "out": 503, "cache_write": 2051, "cache_read": 4000,
                                  "uses": {"web_search": 2}}}
    # a tool use with no per_use rate costs nothing (the estimate held nothing for it)
    assert codec.price_usage(_u(0, 0, 0, 0, [("web_fetch", 3)]), EA) == 0
    # one rounding: a class's product is rounded up on its own (1 token x 0.3 microdollars = 1)
    assert codec.price_usage(_u(0, 0, 0, 1), EA) == 1


def test_codec_usage_anthropic_delta_nulls_never_override_the_start():
    # L3 r1 complement 2: a `message_delta.usage` key whose value is null leaves `message_start`'s count standing
    events = [e if e[0] != "message_delta" else
              ("message_delta", dict(e[1], usage={"output_tokens": 15, "input_tokens": None,
                                                  "cache_read_input_tokens": None,
                                                  "cache_creation_input_tokens": None}))
              for e in UR.ANTHROPIC_SSE]
    assert _read("anthropic-messages", UR._sse(events), True) == _u(472, 15, 100, 200)
    # a non-null delta count still lays over the start's (the counts are cumulative)
    events = [e if e[0] != "message_delta" else
              ("message_delta", dict(e[1], usage={"output_tokens": 15, "input_tokens": 480}))
              for e in UR.ANTHROPIC_SSE]
    assert _read("anthropic-messages", UR._sse(events), True) == _u(480, 15, 100, 200)


@pytest.mark.parametrize("cname", sorted(codec.CODECS))
def test_codec_usage_cap_hit_has_its_own_reason(cname):
    # L3 r1 complement 3: a side reader stopped by either cap answers `usage-over-cap`, distinct from `no-usage`
    ev = codec.CODECS[cname].usage(CT_SSE)
    ev.feed(b"data: " + b"x" * (codec.USAGE_EVENT_MAX + 1) + b"\n\n")
    data = UR.body(cname, False)
    body = codec.CODECS[cname].usage(CT_JSON)
    body.feed(data[:-1] + b" " * (codec.USAGE_COPY_MAX - len(data) + 1) + data[-1:])
    assert (ev.result(), ev.reason) == (None, "usage-over-cap")
    assert (body.result(), body.reason) == (None, "usage-over-cap")
    cut = codec.CODECS[cname].usage(CT_JSON)
    cut.feed(data[:-1])
    assert (cut.result(), cut.reason) == (None, "no-usage")
    whole = codec.CODECS[cname].usage(CT_JSON)
    whole.feed(data)
    assert whole.result() is not None


def test_usage_over_cap_reason_is_a_ledger_reason():
    from levain.broker import ledger
    assert ledger._REASON_RE.fullmatch("usage-over-cap") and "usage-over-cap" not in ledger.RESERVED_REASONS


@pytest.mark.parametrize("cname,u", [
    # L3 r1 complement 4 + codex 5: cached + written above the documented total is no usage, never clamped to 0
    ("openai-chat", {"prompt_tokens": 1, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 1000}}),
    ("openai-chat", {"prompt_tokens": 100, "completion_tokens": 0,
                     "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 41}}),
    ("openai-responses", {"input_tokens": 1, "output_tokens": 0, "input_tokens_details": {"cached_tokens": 1000}}),
])
def test_codec_usage_impossible_breakdown_is_no_usage(cname, u):
    assert _read(cname, json.dumps({"usage": u}).encode(), False) is None


def test_codec_usage_breakdown_equal_to_the_total_is_usage():
    body = {"usage": {"prompt_tokens": 100, "completion_tokens": 3,
                      "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 40}}}
    assert _read("openai-chat", json.dumps(body).encode(), False) == _u(0, 3, 40, 60)


@pytest.mark.parametrize("det,want", [
    # codex 2: `input_tokens_details.cache_write_tokens`, when present, is priced at cache_write and is out of `in`
    ({"cached_tokens": 0, "cache_write_tokens": 100}, _u(0, 7, 100, 0)),
    ({"cached_tokens": 20, "cache_write_tokens": 30}, _u(50, 7, 30, 20)),
    ({"cached_tokens": 20}, _u(80, 7, 0, 20)),
])
def test_codec_usage_responses_cache_writes_priced_apart(det, want):
    body = {"usage": {"input_tokens": 100, "output_tokens": 7, "input_tokens_details": det}}
    assert _read("openai-responses", json.dumps(body).encode(), False) == want
    ev = UR._sse([("response.completed", {"type": "response.completed", "response": {"status": "completed", **body}})])
    assert _read("openai-responses", ev, True) == want


def test_codec_usage_responses_cache_write_over_the_total_is_no_usage():
    body = {"usage": {"input_tokens": 100, "output_tokens": 7,
                      "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 41}}}
    assert _read("openai-responses", json.dumps(body).encode(), False) is None


_WS1 = {"type": "web_search_call", "id": "ws_1", "status": "completed",
        "action": {"type": "search", "query": "q"}}
_WS2 = {"type": "web_search_call", "id": "ws_2", "status": "completed", "action": {"type": "open_page"}}
_FS1 = {"type": "file_search_call", "id": "fs_1", "status": "completed", "queries": ["q"], "results": None}
_MSG = UR.OPENAI_RESPONSES_JSON["output"][0]


def test_codec_usage_responses_counts_billable_tool_calls():
    # codex 1: the Responses output items billed per call count into `Usage.uses` under the per_use names
    body = dict(UR.OPENAI_RESPONSES_JSON, output=[_WS1, _MSG, _WS2, _FS1, dict(_WS1)])   # ws_1 twice: one call
    want = _u(328 - 64, 52, 0, 64, [("file_search", 1), ("web_search", 2)])
    for step in STEPS:
        assert _read("openai-responses", json.dumps(body).encode(), False, step) == want
    priced = parse_price("EP", dict(EA_RAW, per_use={"web_search": 10_000, "file_search": 2_500}))
    base = codec.price_usage(_u(328 - 64, 52, 0, 64), priced)
    assert codec.price_usage(want, priced) == base + 2 * 10_000 + 2_500
    assert want.meta()["usage"]["uses"] == {"file_search": 1, "web_search": 2}


def test_codec_usage_responses_stream_counts_tool_calls_once_by_id():
    # stream: each `response.output_item.done` item and the final event's `response.output`, deduplicated by id
    events = [
        ("response.created", UR.OPENAI_RESPONSES_SSE[0][1]),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": _WS1}),
        ("response.output_item.done", {"type": "response.output_item.done", "output_index": 1, "item": _FS1}),
        ("response.completed", {"type": "response.completed", "response": {
            "status": "completed", "output": [_WS1, _FS1, _WS2, _MSG], "usage": UR._RESP_USAGE}}),
    ]
    want = _u(328 - 64, 52, 0, 64, [("file_search", 1), ("web_search", 2)])
    for step in STEPS:
        assert _read("openai-responses", UR._sse(events), True, step) == want
    # no final event: no usage, whatever items were seen
    assert _read("openai-responses", UR._sse(events[:3]), True) is None


def test_codec_usage_responses_without_tool_calls_has_no_uses():
    assert _read("openai-responses", UR.body("openai-responses", False), False).uses == ()


def test_codec_usage_responses_tool_call_edges(monkeypatch):
    # an item with no string id is a call of its own (counted high, never dropped)
    anon = {k: v for k, v in _WS1.items() if k != "id"}
    body = dict(UR.OPENAI_RESPONSES_JSON, output=[anon, dict(anon), _WS1])
    assert _read("openai-responses", json.dumps(body).encode(), False).uses == (("web_search", 3),)
    # an `output` that is not a list is not the documented shape: no usage
    body = dict(UR.OPENAI_RESPONSES_JSON, output={"0": _WS1})
    assert _read("openai-responses", json.dumps(body).encode(), False) is None
    # past BILLED_ITEMS_MAX distinct items, no usage (stream and non-stream)
    monkeypatch.setattr(codec.OpenAIResponses, "BILLED_ITEMS_MAX", 2)
    three = [dict(_WS1, id="ws_%d" % i) for i in range(3)]
    body = dict(UR.OPENAI_RESPONSES_JSON, output=three)
    assert _read("openai-responses", json.dumps(body).encode(), False) is None
    assert _read("openai-responses", json.dumps(dict(body, output=three[:2])).encode(), False) is not None
    events = [("response.output_item.done", {"type": "response.output_item.done", "item": it}) for it in three]
    events.append(("response.completed", {"type": "response.completed", "response": {"usage": UR._RESP_USAGE}}))
    assert _read("openai-responses", UR._sse(events), True) is None
