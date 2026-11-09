"""Row 176: dispatch block types as an allowlist over a scoped content walk.

On a dispatch route every content block at a position a codec's table names must be a type the table admits there;
the unscoped denylist (`unbounded_blocks`) still runs beneath it. The position table is the one the estimate's image
count reads, so the walk and the estimate cannot name different positions.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

from levain.broker import codec, norm
from levain.broker.policy import parse_price

sys.path.insert(0, os.path.dirname(__file__))
from tests.test_broker_codec_allowlist import AM, OC, OR, PRICE, PRICES, _body, _check, _fixture  # noqa: E402


def _am(*blocks, role="user"):
    return _body(AM, messages=[{"role": role, "content": list(blocks)}])


def _or(*items):
    return _body(OR, input=[{"role": "user", "content": "hi"}, *items])


def _oc_parts(*parts):
    return _body(OC, messages=[{"role": "user", "content": list(parts)}])


# --- the shapes dispatch admitted before, now refused -------------------------------------------------------------

@pytest.mark.parametrize("c,body,name", [
    (OR, _or({"type": "item_reference", "id": "msg_1"}), "body-unbounded:item_reference"),
    (OR, _or({"id": "msg_1"}), "body-unbounded:item_reference"),
    (OR, _or({"type": "compaction_trigger"}), "body-unbounded:compaction_trigger"),
    (OR, _or({"type": "compaction", "encrypted_content": "x"}), "body-unbounded:compaction"),
    (OR, _or({"type": "web_search_call", "id": "w"}), "body-unbounded:web_search_call"),
    (OR, _body(OR, input=[{"role": "user", "content": [{"type": "input_video", "video_url": "u"}]}]),
     "body-unlisted:input_video"),
    (OR, _or({"type": "tool_search_call", "execution": "server"}), "body-unbounded:tool_search_call"),
    (AM, _am({"type": "advisor_tool_result", "tool_use_id": "a"}), "body-unbounded:advisor_tool_result"),
    (AM, _am({"type": "server_tool_use", "id": "s", "name": "n"}, role="assistant"), "body-unbounded:server_tool_use"),
    (AM, _am({"type": "web_search_tool_result", "tool_use_id": "s"}), "body-unbounded:web_search_tool_result"),
    (AM, _am({"type": "compaction", "content": "x"}), "body-unbounded:compaction"),
    (AM, _am({"type": "quantum_block"}), "body-unlisted:quantum_block"),
    (AM, _am({"type": "tool_result", "tool_use_id": "t", "content": [{"type": "browser_state"}]}),
     "body-unlisted:browser_state"),
    (AM, _am({"type": "tool_result", "tool_use_id": "t", "content": [{"type": "document", "source": {}}]}),
     "body-unbounded:document"),
    (OC, _oc_parts({"type": "input_video", "video_url": "u"}), "body-unlisted:input_video"),
    (OC, _body(OC, messages=[{"role": "assistant", "content": "x", "audio": {"id": "audio_1"}}]),
     "body-unbounded:audio"),
], ids=["resp-item-ref", "resp-typeless-ref", "resp-compaction-trigger", "resp-compaction", "resp-search-call",
        "resp-video", "resp-search-call-server", "am-advisor", "am-server-tool-use", "am-ws-result", "am-compaction",
        "am-unlisted", "am-browser-state", "am-doc-in-result", "chat-video", "chat-audio-ref"])
def test_a_block_outside_the_allowlist_is_refused_on_dispatch(c, body, name):
    assert _check(c, body, dispatch=True) == name
    assert norm.status_of(name) == 403
    assert not isinstance(_check(c, body, dispatch=False), str)       # an interactive route still admits it


@pytest.mark.parametrize("c,body,mark", [
    (OR, _or({"type": "item_reference", "id": "msg_1"}), "item_reference"),
    (OR, _or({"id": "msg_1"}), "item_reference"),
    (OR, _or({"type": "compaction_trigger"}), "compaction_trigger"),
    (AM, _am({"type": "advisor_tool_result", "tool_use_id": "a"}), "advisor_tool_result"),
    (OR, _or({"type": "tool_search_call", "execution": "server", "arguments": {}}), "tool_search_call"),
    (OC, _body(OC, messages=[{"role": "assistant", "content": "x", "audio": {"id": "audio_1"}}]), "audio"),
], ids=["resp-item-ref", "resp-typeless-ref", "resp-compaction-trigger", "am-advisor", "resp-search-call-server",
        "chat-audio-ref"])
def test_an_interactive_route_marks_the_known_cost_block_unproven(c, body, mark):
    got = _check(c, body, dispatch=False)
    assert mark in got.unproven


# --- the later turns of a real conversation stay admitted ---------------------------------------------------------

def test_an_anthropic_second_turn_with_thinking_and_a_tool_round_trip_is_admitted():
    body = _body(AM, system=[{"type": "text", "text": "s"}], messages=[
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "t", "signature": "sig"},
            {"type": "redacted_thinking", "data": "d"},
            {"type": "text", "text": "a"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"type": "item_reference"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": "r"},
                {"type": "image", "source": {"type": "file", "file_id": "f"}},
                {"type": "tool_reference", "tool_name": "Read"},
                {"type": "search_result", "source": "s", "title": "t", "content": [{"type": "text", "text": "x"}]}]},
            {"type": "tool_addition", "tool": {"type": "tool_reference", "name": "Read"}},
            {"type": "tool_removal", "tool": {"type": "tool_reference", "name": "Read"}}]}])
    got = _check(AM, body, dispatch=True)
    assert not isinstance(got, str), got
    assert got.unproven == ()


def test_a_responses_second_turn_with_reasoning_and_a_function_round_trip_is_admitted():
    body = _body(OR, input=[
        {"role": "developer", "content": [{"type": "input_text", "text": "d"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]},
        {"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "enc"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{\"type\": \"item_reference\"}"},
        {"type": "function_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "o"},
                                                                    {"type": "input_image", "file_id": "f"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "a"},
                                                              {"type": "refusal", "refusal": "r"}]},
        {"type": "local_shell_call_output", "call_id": "l", "output": "o"},
        {"type": "tool_search_call", "execution": "client"},
        {"role": "user", "content": "typeless message"}])
    got = _check(OR, body, dispatch=True)
    assert not isinstance(got, str), got
    assert got.unproven == ()


def test_a_chat_second_turn_is_admitted():
    body = _body(OC, messages=[
        {"role": "system", "content": [{"type": "text", "text": "s"}]},
        {"role": "user", "content": [{"type": "text", "text": "q"}, {"type": "image_url", "image_url": {"url": "u"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}, {"type": "refusal", "refusal": "r"}],
         "tool_calls": []},
        {"role": "tool", "tool_call_id": "c", "content": "o"}])
    got = _check(OC, body, dispatch=True)
    assert not isinstance(got, str), got


# --- an image by file reference stays counted at one image's price ------------------------------------------------

@pytest.mark.parametrize("c,body", [
    (AM, _am({"type": "image", "source": {"type": "file", "file_id": "f"}})),
    (AM, _am({"type": "tool_result", "tool_use_id": "t", "content": [
        {"type": "image", "source": {"type": "file", "file_id": "f"}}]})),
    (OR, _body(OR, input=[{"role": "user", "content": [{"type": "input_image", "file_id": "f"}]}])),
], ids=["am-content", "am-tool-result", "resp-file-id"])
def test_a_file_referenced_image_is_admitted_and_counted_as_one_image(c, body):
    got = _check(c, body, dispatch=True)
    assert not isinstance(got, str), got
    assert c._strip_images(dict(body, model="m1"))[1] == 1


# --- a block's own fields are the client's JSON, never walked -----------------------------------------------------

@pytest.mark.parametrize("input_", [{"type": "item_reference"}, {"type": "compaction"}, {"type": "quantum_block"}])
def test_a_tool_use_input_is_not_a_content_position(input_):
    body = _am({"type": "tool_use", "id": "t", "name": "f", "input": {"x": [input_]}}, role="assistant")
    assert not isinstance(_check(AM, body, dispatch=True), str)


# --- unjudged type names ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("block", [{"type": "Type-X"}, {"type": "t" * 60}, {"type": "café"}, {"type": 7}, {"type": None},
                                   {"text": "no type"}, "a string", 3, None, ["x"]],
                         ids=["upper-hyphen", "long", "non-ascii", "int", "null", "typeless", "str", "int-block",
                              "null-block", "list-block"])
def test_a_block_with_no_usable_type_is_the_fixed_suffix_and_never_raises(block):
    got = _check(AM, _am(block), dispatch=True)
    assert got == "body-unlisted:block", got
    assert norm.status_of(got) == 403


def test_a_content_that_is_a_string_judges_no_block():
    assert not isinstance(_check(AM, _body(AM, messages=[{"role": "user", "content": "hi"}]), dispatch=True), str)
    assert not isinstance(_check(AM, _body(AM, system="s"), dispatch=True), str)


@pytest.mark.parametrize("c,body", [
    (OR, _body(OR, input=["Hello world"])),
    (OR, _or({"type": "function_call_output", "call_id": "c", "output": ["error output"]})),
    (AM, _am("hello")),
    (AM, _body(AM, system=["prompt"])),
], ids=["resp-string-item", "resp-string-output-part", "am-string-content-part", "am-string-system-part"])
def test_a_bare_string_inside_a_block_list_is_refused_as_the_provider_would(c, body):
    # the vendors' request types admit a string for the whole field, never a string inside the list
    assert _check(c, body, dispatch=True) == "body-unlisted:block"


def test_a_string_function_output_judges_no_block():
    assert not isinstance(_check(OR, _or({"type": "function_call_output", "call_id": "c", "output": "42"}),
                                 dispatch=True), str)


def test_a_typeless_responses_item_with_a_role_is_a_message_and_one_without_is_a_reference():
    assert not isinstance(_check(OR, _body(OR, input=[{"role": "user", "content": "x"}]), dispatch=True), str)
    assert _check(OR, _body(OR, input=[{"content": "x"}]), dispatch=True) == "body-unbounded:item_reference"


def test_the_first_block_in_body_order_is_named():
    got = _check(AM, _am({"type": "quantum_block"}, {"type": "other_block"}), dispatch=True)
    assert got == "body-unlisted:quantum_block"


@pytest.mark.parametrize("body,name", [
    # a block nested in an earlier block comes before that block's later sibling
    (_am({"type": "tool_result", "tool_use_id": "t", "content": [{"type": "browser_state"}]}, {"type": "quantum_block"}),
     "body-unlisted:browser_state"),
    # two positions under two top-level keys: the key the body sends first is read first
    ({"max_tokens": 10, "messages": [{"role": "user", "content": [{"type": "quantum_block"}]}],
      "system": [{"type": "weird"}]}, "body-unlisted:quantum_block"),
    ({"max_tokens": 10, "system": [{"type": "weird"}],
      "messages": [{"role": "user", "content": [{"type": "quantum_block"}]}]}, "body-unlisted:weird"),
], ids=["nested-before-sibling", "messages-then-system", "system-then-messages"])
def test_the_first_block_in_body_order_is_named_across_positions(body, name):
    assert _check(AM, body, dispatch=True) == name


def test_an_unjudged_block_in_an_earlier_position_does_not_hide_a_later_known_cost():
    body = {"max_tokens": 10, "system": [{"type": "bad_sys"}],
            "messages": [{"role": "user", "content": [{"type": "advisor_tool_result", "tool_use_id": "a"}]}]}
    assert _check(AM, body, dispatch=True) == "body-unbounded:advisor_tool_result"


def test_a_known_cost_is_named_before_an_unjudged_block():
    # the denylist's names come first, as for a field or a tool: the operator is told what to turn off
    got = _check(AM, _am({"type": "quantum_block"}, {"type": "advisor_tool_result"}), dispatch=True)
    assert got == "body-unbounded:advisor_tool_result"


# --- the unscoped denylist still runs beneath the walk ------------------------------------------------------------

def test_the_unscoped_denylist_still_refuses_a_document_off_every_position():
    body = _body(AM, tools=[{"name": "t", "input_schema": {"type": "object",
                                                             "properties": {"p": {"type": "document"}}}}])
    assert _check(AM, body, dispatch=True) == "body-unbounded:document"


# --- the pins: one table, no overlaps -----------------------------------------------------------------------------

CODECS = [AM, OC, OR]


@pytest.mark.parametrize("c", CODECS, ids=lambda c: c.name)
def test_the_estimates_image_paths_are_the_allowlist_tables_own_rows(c):
    assert isinstance(codec.Codec.image_paths, property)
    assert all("image_paths" not in vars(k) for k in (codec.Codec, type(c)) if k is not codec.Codec)
    assert c.content_positions
    assert c.image_paths == tuple((p.path, p.images) for p in c.content_positions if p.images)


@pytest.mark.parametrize("c", CODECS, ids=lambda c: c.name)
def test_an_image_type_is_counted_only_where_the_table_admits_it(c):
    assert any(p.images for p in c.content_positions)
    for p in c.content_positions:
        assert set(p.images) <= set(p.admit), p
    # take one image type off the table and the estimate stops counting it there: the same row serves both
    fake = type(c)()
    rows = [p for p in c.content_positions if p.images]
    fake.content_positions = tuple(p if p is not rows[0] else codec.Position(p.path, p.admit, ()) for p in
                                   c.content_positions)
    assert fake.image_paths == c.image_paths[1:]


@pytest.mark.parametrize("c", CODECS, ids=lambda c: c.name)
def test_no_admitted_type_is_on_a_denylist(c):
    admitted = {t for p in c.content_positions for t in p.admit}
    assert not admitted & set(c.unbounded_blocks)
    assert not admitted & set(c.known_unbounded_blocks)
    assert not set(c.unbounded_blocks) & set(c.known_unbounded_blocks)


@pytest.mark.parametrize("c", CODECS, ids=lambda c: c.name)
def test_every_refusal_name_the_walk_can_emit_has_a_status(c):
    for t in tuple(c.known_unbounded_blocks) + tuple(c.unbounded_blocks):
        name = "body-unbounded:" + codec._unbounded_what(t)
        assert norm.is_name(name) and norm.status_of(name) == 403, name
    for t in ("quantum_block", "block"):
        name = "body-unlisted:" + t
        assert norm.is_name(name) and norm.status_of(name) == 403, name


@pytest.mark.parametrize("c,body", [
    (AM, _am({"type": "thinking", "thinking": "t", "signature": "sig"}, {"type": "redacted_thinking", "data": "d"},
             role="assistant")),
    (OR, _or({"type": "reasoning", "id": "rs", "summary": [], "encrypted_content": "enc"})),
], ids=["am-thinking", "resp-reasoning"])
def test_the_opaque_blocks_are_admitted_by_bytes_and_marked_by_nothing(c, body):
    # UNMEASURED: no measured call yet shows their billed input fits inside their bytes; the table carries no mark
    assert "opaque" not in {f.name for f in dataclasses.fields(codec.Position)}
    for dispatch in (True, False):
        got = _check(c, body, dispatch=dispatch)
        assert not isinstance(got, str), got
        assert got.unproven == ()


# --- the captured client bodies stay admitted ---------------------------------------------------------------------

@pytest.mark.parametrize("name,c", [
    ("claude_code_messages_main.json", AM), ("claude_code_messages_full_tools.json", AM),
    ("codex_responses_exec_nosearch.json", OR), ("openai_sdk_chat_stream_tools.json", OC),
    ("openai_sdk_chat_openrouter_path.json", OC), ("openai_sdk_responses_stream_tools.json", OR)])
def test_every_captured_body_is_still_admitted(name, c):
    got = _check(c, _fixture(name), dispatch=True)
    assert not isinstance(got, str), (name, got)
    assert got.unproven == ()


# --- the keys path ------------------------------------------------------------------------------------------------

def test_a_dispatch_route_item_reference_is_refused_before_any_hold(monkeypatch):
    from tests.test_broker_keys_upstream import _run
    from tests.test_tickets_keys import MODEL, RUN, _claims, _exchange, dispatch_pol
    body = json.dumps({"model": MODEL, "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "advisor_tool_result", "tool_use_id": "a"}]}]}).encode()

    async def go(d):
        return await _exchange(d, monkeypatch, body, _claims(RUN), dispatch_pol())

    out = _run(go)
    name = json.loads(out["frames"][-1][1])["refused"]
    assert out["frames"][-1][0] == b"F" and name == "body-unbounded:advisor_tool_result", out["frames"]
    assert out["handshakes"] == 0 and not [x for x in out["lines"] if x["kind"] == "reserved"]
