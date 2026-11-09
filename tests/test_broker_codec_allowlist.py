"""Row 150: the dispatch body check as an allowlist.

A dispatch route admits only the top-level fields and the client-run tool types a codec has judged
(`dispatch_fields`, `dispatch_tool_types`); anything else is `body-unlisted:<name>` (403). An interactive route keeps
its denylists (unproven marks) and is untouched. Block types stay a denylist.

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
    (OC, {"type": "openrouter:web_search"}, "body-unbounded:openrouter:web_search"),
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
