"""Row 150: the dispatch body check as an allowlist.

A dispatch route admits only the top-level fields and the client-run tool types a codec has judged
(`dispatch_fields`, `dispatch_tool_types`); anything else is `body-unlisted:<name>` (403). An interactive route keeps
its denylists (unproven marks) and is untouched. Block types are an allowlist over the position table (see
`test_broker_codec_blockwalk.py`).

The admit fixtures are REAL client bodies, captured by pointing each client's base URL at a local listener that
recorded the request and answered 400; the prose is replaced, every key is kept (`tests/fixtures/client_bodies/`).
"""
from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import replace

import pytest

from levain.broker import codec, norm
from levain.broker.policy import parse_price

sys.path.insert(0, os.path.dirname(__file__))

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "client_bodies")
PRICE = parse_price("m1", {"in": 3_000_000, "out": 15_000_000, "cache_write": 3_750_000, "cache_read": 300_000,
                           "default_output": 4096, "overhead_tokens": 64, "image_tokens": 1600,
                           "per_use": {"web_search": 10_000}, "source": "https://example.test/a"})
PRICES = {"m1": PRICE}


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def _check(c, obj, *, dispatch):
    """The refusal's name, or the `Checked` the codec answered."""
    obj = dict(obj, model="m1")
    try:
        return c.check(obj, PRICES, dispatch=dispatch)
    except norm.Refused as r:
        return r.name


AM, OC, OR = (codec.CODECS[n] for n in ("anthropic-messages", "openai-chat", "openai-responses"))
BASE = {"anthropic-messages": {"max_tokens": 10, "messages": []}, "openai-chat": {"messages": []},
        "openai-responses": {"input": []}}


def _body(c, **extra):
    return dict(BASE[c.name], **extra)


# --- test 1: a real client body per codec, admitted on dispatch ----------------------------------------------------

@pytest.mark.parametrize("name", ["claude_code_messages_main.json", "claude_code_messages_full_tools.json"])
def test_a_real_claude_code_body_is_admitted_on_dispatch(name):
    obj = _fixture(name)
    got = _check(AM, obj, dispatch=True)
    assert not isinstance(got, str), got
    assert got.unproven == ()


def test_a_real_codex_body_without_web_search_is_admitted_on_dispatch():
    got = _check(OR, _fixture("codex_responses_exec_nosearch.json"), dispatch=True)
    assert not isinstance(got, str), got
    assert got.unproven == ()


def test_a_real_codex_body_with_its_default_web_search_tool_is_refused_as_unbounded_not_unlisted():
    # Codex's default tool list holds a `web_search` tool: a server tool the estimate cannot see. The refusal is the
    # denylist's name, so the operator knows to turn the tool off in the client, not to wait for a levain release.
    assert _check(OR, _fixture("codex_responses_exec.json"), dispatch=True) == "body-unbounded:web_search"
    got = _check(OR, _fixture("codex_responses_exec.json"), dispatch=False)
    assert got.unproven == ("web_search",)


def test_every_key_of_each_captured_body_is_on_its_codecs_list():
    for name, c in (("claude_code_messages_main.json", AM), ("claude_code_messages_full_tools.json", AM),
                    ("codex_responses_exec.json", OR), ("codex_responses_exec_nosearch.json", OR)):
        assert set(_fixture(name)) <= set(c.dispatch_fields), (name, set(_fixture(name)) - set(c.dispatch_fields))


# --- test 2: the two real misses, replayed -------------------------------------------------------------------------

def _not_found_yet(field_name):
    """`AnthropicMessages` in the state before the denylist learned `compaction`: it is on neither list."""
    c = copy.copy(AM)
    c.unbounded_fields = tuple(f for f in AM.unbounded_fields if f != field_name)
    c.dispatch_fields = tuple(f for f in AM.dispatch_fields if f != field_name)
    return c


def test_compaction_the_denylist_missed_is_refused_by_the_allowlist_alone():
    c = _not_found_yet("compaction")
    body = _body(c, compaction={})
    assert c._unbounded(dict(body, model="m1"), PRICE) == ()       # the denylist, as it was, sees nothing
    got = _check(c, body, dispatch=True)
    assert got == "body-unlisted:compaction"
    assert norm.status_of(got) == 403
    interactive = _check(c, body, dispatch=False)                 # the session cap bounds an interactive route
    assert not isinstance(interactive, str) and interactive.unproven == ()


def test_the_compaction_edit_in_context_management_is_the_value_denylists_to_catch():
    # `context_management` is a field real clients send (Claude Code does), so it is listed: the allowlist alone
    # cannot refuse a compaction edit inside it. That is the value check's (`_unbounded_shapes`) job, still in force.
    body = _body(AM, context_management={"edits": [{"type": "compact_20260112"}]})
    assert "context_management" in AM.dispatch_fields
    assert _check(AM, body, dispatch=True) == "body-unbounded:context_management"
    blind = copy.copy(AM)
    blind._unbounded_shapes = lambda obj: []
    got = _check(blind, body, dispatch=True)
    assert not isinstance(got, str)                                # the residue a field list cannot hold


# --- test 3: an unknown top-level field ----------------------------------------------------------------------------

@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
def test_an_unlisted_field_is_refused_on_dispatch_and_admitted_interactive(c):
    body = _body(c, zz_new_vendor_field=True)
    got = _check(c, body, dispatch=True)
    assert got == "body-unlisted:zz_new_vendor_field"
    assert norm.status_of(got) == 403 and norm.is_name(got)
    interactive = _check(c, body, dispatch=False)
    assert not isinstance(interactive, str) and interactive.unproven == ()


def test_the_first_unlisted_field_in_body_order_is_named():
    body = _body(AM, aaa_one=1, bbb_two=2)
    assert _check(AM, body, dispatch=True) == "body-unlisted:aaa_one"


# --- test 4: a field name outside the wire grammar -----------------------------------------------------------------

@pytest.mark.parametrize("field", ["X-Foo", "a" * 60, "café", "Upper", "9lead", "with space", "", "a:b"])
def test_a_field_name_outside_the_grammar_is_the_fixed_suffix(field):
    got = _check(AM, _body(AM, **{field: 1}), dispatch=True)
    assert got == "body-unlisted:field"
    assert norm.status_of(got) == 403                              # status_of does not raise


def test_a_field_name_inside_the_grammar_is_named():
    assert _check(AM, _body(AM, **{"a" * 48: 1}), dispatch=True) == "body-unlisted:" + "a" * 48
    assert _check(AM, _body(AM, **{"a" * 49: 1}), dispatch=True) == "body-unlisted:field"


# --- test 5: tool types --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("c,tool", [
    (AM, {"type": "quantum_tool_20260101", "name": "q"}),
    (OC, {"type": "quantum_tool", "name": "q"}),
    (OR, {"type": "quantum_tool", "name": "q"}),
    (OR, {"type": "tool_search"}),
], ids=["am", "oc", "or", "or-tool_search"])
def test_an_unlisted_tool_type_is_refused_on_dispatch(c, tool):
    body = _body(c, tools=[tool])
    got = _check(c, body, dispatch=True)
    assert got.startswith("body-unlisted:") and norm.is_name(got), got
    interactive = _check(c, body, dispatch=False)
    assert not isinstance(interactive, str)


def test_the_refusal_names_the_tool_type_with_its_version_removed():
    body = _body(AM, tools=[{"type": "quantum_tool_20260101", "name": "q"}])
    assert _check(AM, body, dispatch=True) == "body-unlisted:quantum_tool"


@pytest.mark.parametrize("c,tools", [
    (AM, [{"name": "Bash", "description": "d", "input_schema": {"type": "object"}}, {"type": "custom", "name": "c"}]),
    (OC, [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}},
          {"type": "custom", "custom": {"name": "c"}}]),
    (OR, [{"type": "function", "name": "f", "parameters": {"type": "object"}}, {"type": "custom", "name": "c"},
          {"type": "namespace", "name": "n", "tools": []}]),
], ids=["am", "oc", "or"])
def test_client_run_tools_are_admitted_on_dispatch(c, tools):
    got = _check(c, _body(c, tools=tools), dispatch=True)
    assert not isinstance(got, str), got


@pytest.mark.parametrize("c,tool,name", [
    (AM, {"type": "web_search_20250305", "name": "web_search"}, "body-unbounded:web_search"),
    (AM, {"type": "code_execution_20250522", "name": "code_execution"}, "body-unbounded:code_execution"),
    (OR, {"type": "web_search_preview"}, "body-unbounded:web_search_preview"),
    (OR, {"type": "mcp", "server_label": "s"}, "body-unbounded:mcp"),
    (OC, {"type": "openrouter:web_search"}, "body-unbounded:openrouter_web_search"),
], ids=["am-ws", "am-ce", "or-wsp", "or-mcp", "oc-or"])
def test_a_known_server_tool_keeps_the_unbounded_name(c, tool, name):
    got = _check(c, _body(c, tools=[tool]), dispatch=True)
    assert got == name, got


# --- test 6: the pins ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
def test_the_lists_hold_every_field_and_tool_the_codec_itself_reads(c):
    assert set(c.unbounded_fields) <= set(c.dispatch_fields)       # the value check, not the allowlist, decides it
    assert set(c.ceiling_fields) <= set(c.dispatch_fields)
    assert {"model", "stream", "tools"} <= set(c.dispatch_fields)
    assert {p[0][0] for p in c.image_paths} <= set(c.dispatch_fields)
    assert not set(c.server_tools) & set(c.dispatch_tool_types)
    assert not any(t.startswith(c.server_tool_prefixes) for t in c.dispatch_tool_types)


@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
def test_the_lists_have_no_duplicates_and_hold_non_empty_strings(c):
    for lst in (c.dispatch_fields, c.dispatch_tool_types):
        assert len(lst) == len(set(lst)) and all(type(x) is str and x for x in lst)


def test_the_codec_specific_reads_are_listed():
    assert "n" in OC.dispatch_fields and "stream_options" in OC.dispatch_fields
    assert {"stream_options", "prompt", "previous_response_id", "conversation"} <= set(OR.dispatch_fields)
    assert {"mcp_servers", "plugins", "compaction", "context_management"} <= set(AM.dispatch_fields)


def test_the_unjudged_spend_fields_stay_off_the_lists():
    # board row 151 lists these as unmeasured; until one is measured it is a refusal on dispatch, never a guess
    for field in ("background", "modalities", "moderation", "context_management"):
        assert field not in OR.dispatch_fields
    for field in ("modalities", "audio", "prediction"):
        assert field not in OC.dispatch_fields
    assert "container" not in AM.dispatch_fields


# --- test 7: the keys path -----------------------------------------------------------------------------------------

def test_a_dispatch_route_request_with_an_unlisted_field_is_refused_before_any_hold(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    from tests.test_tickets_keys import MODEL, RUN, _claims, _exchange, dispatch_pol
    body = json.dumps({"model": MODEL, "max_tokens": 10, "messages": [], "zz_new_vendor_field": 1}).encode()

    async def go(d):
        return await _exchange(d, monkeypatch, body, _claims(RUN), dispatch_pol())

    out = _run(go)
    name = json.loads(out["frames"][-1][1])["refused"]
    assert out["frames"][-1][0] == b"F" and name == "body-unlisted:zz_new_vendor_field", out["frames"]
    assert out["handshakes"] == 0 and not [x for x in out["lines"] if x["kind"] == "reserved"]


def test_a_real_claude_code_body_passes_a_dispatch_route(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    from tests.test_tickets_keys import MODEL, RUN, _claims, _exchange, dispatch_pol
    obj = _fixture("claude_code_messages_main.json")
    obj["model"] = MODEL
    body = json.dumps(obj, separators=(",", ":")).encode()

    async def go(d):
        return await _exchange(d, monkeypatch, body, _claims(RUN), replace(dispatch_pol(), body_cap=200_000))

    out = _run(go)
    assert not (out["frames"][-1][0] == b"F" and "body-un" in json.loads(out["frames"][-1][1]).get("refused", "")), \
        out["frames"]
    assert [x for x in out["lines"] if x["kind"] == "reserved"]


# --- F6: every refusal name a codec emits parses on the wire --------------------------------------------------------

def _emitters(c):
    """One body per thing the `body-unbounded` check can name, so each emitted name is tried."""
    bodies = []
    for f in c.unbounded_fields:
        bodies.append(_body(c, **{f: {"x": 1} if f != "plugins" else [{"id": "web"}]}))
    for t in tuple(c.server_tools) + ("openrouter:web_search", "openrouter:web-fetch", "openrouter:" + "x" * 80):
        bodies.append(_body(c, tools=[{"type": t + "_20250101"}]))
    for b in c.unbounded_blocks:
        bodies.append(_body(c, **{"messages" if c is not OR else "input": [{"role": "user", "content": [{"type": b}]}]}))
    if c is AM:
        bodies.append(_body(c, context_management={"edits": [{"type": "compact_20260101"}]}))
    return bodies


@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
def test_every_body_unbounded_name_a_codec_emits_has_a_status(c):
    seen = 0
    for body in _emitters(c):
        got = _check(c, body, dispatch=True)
        assert isinstance(got, str) and got.startswith("body-unbounded:"), (body, got)
        assert norm.is_name(got), got
        assert norm.status_of(got) == 403
        seen += 1
    assert seen >= 4


def test_the_openrouter_namespace_tool_is_named_with_an_underscore():
    assert _check(OC, _body(OC, tools=[{"type": "openrouter:web_search"}]), dispatch=True) == \
        "body-unbounded:openrouter_web_search"


def test_the_unbounded_refusal_detail_tells_the_operator_to_turn_the_tool_off():
    with pytest.raises(norm.Refused) as e:
        OR.check(dict(_body(OR, tools=[{"type": "web_search"}]), model="m1"), PRICES, dispatch=True)
    assert "turn it off in the client" in e.value.detail


# --- F1/F5: the openai SDK's own wire shape, and the provenance sidecar -------------------------------------------

@pytest.mark.parametrize("name,c", [("openai_sdk_chat_stream_tools.json", OC), ("openai_sdk_chat_openrouter_path.json", OC),
                                    ("openai_sdk_responses_stream_tools.json", OR)])
def test_an_openai_sdk_body_is_admitted_on_dispatch(name, c):
    # the SDK's wire shape, not an agent client: no agent client for the Chat API is installed on this machine
    obj = _fixture(name)
    assert set(obj) <= set(c.dispatch_fields), set(obj) - set(c.dispatch_fields)
    got = _check(c, obj, dispatch=True)
    assert not isinstance(got, str), got
    assert got.unproven == ()


def test_every_client_body_fixture_has_a_provenance_entry():
    prov = _fixture("PROVENANCE.json")
    names = {n for n in os.listdir(FIX) if n.endswith(".json") and n != "PROVENANCE.json"}
    assert names == {k for k in prov if not k.startswith("_")}
    for k in names:
        assert {"client", "version", "captured", "how"} <= set(prov[k]), k


# --- r1 F2: a `tools` that is not a list ---------------------------------------------------------------------------

@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
@pytest.mark.parametrize("tools", [{"type": "web_search"}, "web_search", 1], ids=["obj", "str", "int"])
def test_a_tools_that_is_not_a_list_is_refused_on_dispatch(c, tools):
    # the tool-type allowlist reads a list; a `tools` of any other shape judges nothing, so dispatch refuses it
    assert _check(c, _body(c, tools=tools), dispatch=True) == "body-unlisted:tools"
    assert not isinstance(_check(c, _body(c, tools=tools), dispatch=False), str)


@pytest.mark.parametrize("c", [AM, OC, OR], ids=lambda c: c.name)
def test_a_null_tools_is_no_tools_on_dispatch(c):
    # generic clients send `"tools": null` for "no tools"; the provider reads it as absent, and so does dispatch
    assert not isinstance(_check(c, _body(c, tools=None), dispatch=True), str)


def test_a_non_object_entry_inside_a_tools_list_stays_the_providers_to_refuse():
    assert not isinstance(_check(OR, _body(OR, tools=["web_search", 1]), dispatch=True), str)


# --- r1 F3: a tool definition declared inside the content, not in `tools[]` ----------------------------------------
# D150b A2's reproduced rows: every one was admitted on dispatch before this round.

def _responses_item(item):
    return _body(OR, input=[{"role": "user", "content": "hi"}, item])


def _anthropic_block(block):
    return _body(AM, messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}, block]}])


def _addition(definition):
    return {"type": "tool_addition", "tool": {"type": "tool_definition", "definition": definition}}


@pytest.mark.parametrize("item,name", [
    ({"type": "additional_tools", "role": "developer", "tools": [{"type": "web_search"}]}, "body-unbounded:web_search"),
    ({"type": "additional_tools", "tools": [{"type": "function", "name": "f"}, {"type": "mcp", "server_label": "s"}]},
     "body-unbounded:mcp"),
    ({"type": "additional_tools", "tools": [{"type": "quantum_tool"}]}, "body-unlisted:quantum_tool"),
    ({"type": "tool_search_output", "call_id": "c", "tools": [{"type": "file_search"}]}, "body-unbounded:file_search"),
    ({"type": "tool_search_output", "call_id": "c", "tools": [{"type": "quantum_tool"}]}, "body-unlisted:quantum_tool"),
    ({"type": "additional_tools", "tools": {"type": "web_search"}}, "body-unlisted:tools"),
], ids=["additional-ws", "additional-mcp", "additional-unlisted", "search-output-fs", "search-output-unlisted",
        "additional-not-a-list"])
def test_a_responses_inline_tool_goes_through_the_tool_allowlist(item, name):
    body = _responses_item(item)
    assert _check(OR, body, dispatch=True) == name
    interactive = _check(OR, body, dispatch=False)
    assert not isinstance(interactive, str)
    if name.startswith("body-unbounded:"):
        assert name.partition(":")[2] in interactive.unproven     # the same naming the interactive route marks


@pytest.mark.parametrize("definition,name", [
    ({"type": "web_search_20250305", "name": "web_search"}, "body-unbounded:web_search"),
    ({"type": "code_execution_20250522", "name": "code_execution"}, "body-unbounded:code_execution"),
    ({"type": "mcp_toolset", "mcp_server_name": "x"}, "body-unlisted:mcp_toolset"),
    ({"type": "quantum_tool_20260101", "name": "q"}, "body-unlisted:quantum_tool"),
], ids=["ws", "ce", "mcp-toolset", "unlisted"])
def test_an_anthropic_inline_tool_definition_goes_through_the_tool_allowlist(definition, name):
    body = _anthropic_block(_addition(definition))
    assert _check(AM, body, dispatch=True) == name
    assert not isinstance(_check(AM, body, dispatch=False), str)


def test_an_anthropic_compaction_blocks_tool_changes_go_through_the_tool_allowlist():
    block = {"type": "compaction", "content": "x",
             "tool_changes": [{"type": "tool_removal", "tool": {"type": "tool_reference", "name": "a"}},
                              _addition({"type": "web_fetch_20250910", "name": "web_fetch"})]}
    assert _check(AM, _anthropic_block(block), dispatch=True) == "body-unbounded:web_fetch"


@pytest.mark.parametrize("c,body", [
    (OR, _responses_item({"type": "additional_tools", "tools": [{"type": "function", "name": "f"},
                                                                 {"type": "custom", "name": "c"}]})),
    (OR, _responses_item({"type": "tool_search_output", "call_id": "c", "tools": [{"type": "function", "name": "f"}]})),
    (AM, _anthropic_block(_addition({"name": "Read", "input_schema": {"type": "object"}}))),
    (AM, _anthropic_block(_addition({"type": "custom", "name": "c", "input_schema": {"type": "object"}}))),
    (AM, _anthropic_block({"type": "tool_addition", "tool": {"type": "tool_reference", "name": "Read"}})),
], ids=["or-additional-client", "or-search-output-client", "am-typeless", "am-custom", "am-reference"])
def test_a_client_run_inline_tool_is_admitted_on_dispatch(c, body):
    got = _check(c, body, dispatch=True)
    assert not isinstance(got, str), got


def test_a_tool_definition_in_a_tool_input_is_not_read_as_a_declaration():
    # a `tool_use.input` is the client's JSON, not a declaration position
    block = {"type": "tool_use", "id": "t", "name": "f", "input": _addition({"type": "web_search_20250305"})}
    assert not isinstance(_check(AM, _anthropic_block(block), dispatch=True), str)
