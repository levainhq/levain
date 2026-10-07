"""A reply that is a model's raw tool-call syntax is not shown as the entity's reply (0.6.8).

When an open model's tool call fails to parse upstream of levain, its markup arrives as reply TEXT and no
tool ran. :func:`levain.firing.agent_reply.unreadable_tool_call` recognises that text by its shape so the
panel and the REPL can say what happened instead of rendering the markup as an answer. It only reads the
shape: nothing is repaired or run.
"""
from __future__ import annotations

import pytest

from levain.firing.agent_reply import (
    UNREADABLE_CALL_AFTER_ACTIONS_NOTICE,
    UNREADABLE_CALL_NOTICE,
    unreadable_call_notice,
    unreadable_tool_call,
)

TOOLS = frozenset({"terminal", "file_editor", "task_tracker", "finish", "think"})

# REAL replies: glm-5.2:cloud on levain 0.6.7's chat, Phill's prompt ("Please create a file called test.txt and
# put the following text inside it: "approval text""), measured 2026-10-05 by the 1005+11 seat. The markup bytes
# are as captured; the captured rows kept only the first 160 characters, and the operator's home path inside
# the path value is replaced with /work/entity.
GLM_REAL = [
    "_editor<arg_key>command</arg_key><arg_value>create</arg_value><arg_key>path</arg_key><arg_value>/work/ent",
    "<arg_key>command</arg_key><arg_value>create</arg_value><arg_key>path</arg_key><arg_value>/work/entity/en",
    "</arg_key><arg_value>create</arg_value><arg_key>path</arg_key><arg_value>/work/entity/entity-chat/worksp",
    "create</arg_value><arg_key>path</arg_key><arg_value>/work/entity/entity-chat/workspace/test3.txt</arg_va",
    "command</arg_key><arg_value>create</arg_value><arg_key>path</arg_key><arg_value>/work/entity/entity-chat",
]


@pytest.mark.parametrize("reply", GLM_REAL)
def test_glm_arg_key_markup_captured_from_glm_5_2_is_flagged(reply):
    assert unreadable_tool_call(reply, TOOLS)


# The two shapes below have no captured instance (glm-5.2 did not reproduce on 25 later runs, 2026-10-05): they
# follow the published Hermes/Qwen <tool_call> wrapper and the OpenAI function-call object.
def test_tool_call_wrapper_is_flagged():
    reply = '<tool_call>\n{"name": "file_editor", "arguments": {"command": "create", "path": "test.txt"}}\n</tool_call>'
    assert unreadable_tool_call(reply, TOOLS)
    assert unreadable_tool_call("<tool_call>file_editor<arg_key>command</arg_key><arg_value>create</arg_value>", TOOLS)
    # Qwen3-Coder's XML form (its model card's format)
    assert unreadable_tool_call("<tool_call>\n<function=terminal>\n<parameter=command>\nls\n</parameter>\n</function>\n</tool_call>", TOOLS)


def test_a_leak_after_earlier_text_in_the_same_turn_is_flagged():
    # L1 r1: the reply joins every agent message since the human's; the leak can follow real prose (an approved
    # action ran, then the next call leaked). It begins its own line.
    assert unreadable_tool_call("Done with the push. Now the file:\n" + GLM_REAL[1], TOOLS)
    assert unreadable_tool_call("I'll create the file.\n<tool_call>file_editor<arg_key>path</arg_key><arg_value>x", TOOLS)


@pytest.mark.parametrize("reply", [
    '{"name": "file_editor", "arguments": {"command": "create", "path": "test.txt", "file_text": "approval text"}}',
    '  {"name": "terminal", "parameters": {"command": "ls"}}\n',
    '```json\n{"name": "terminal", "arguments": {"command": "ls"}}\n```',
    '{"type": "function", "function": {"name": "terminal", "arguments": "{\\"command\\": \\"ls\\"}"}}',
    '[{"name": "terminal", "arguments": {"command": "ls"}}]',
])
def test_a_bare_json_call_naming_a_known_tool_is_flagged(reply):
    assert unreadable_tool_call(reply, TOOLS)


@pytest.mark.parametrize("reply", [
    # prose that MENTIONS the markup is an answer, not a leaked call
    "GLM models wrap a call in a <tool_call> tag, and when it fails to parse you see the raw text.",
    "The model emitted <tool_call> and </tool_call> around its call.",
    "Its format pairs each `<arg_key>command</arg_key><arg_value>create</arg_value>` inside backticks.",
    "Here is the format:\n```\n<tool_call>{\"name\": \"terminal\"}</tool_call>\n```\nThat is what a call looks like.",
    # JSON that is the answer: no known tool named, or not the call shape
    '{"name": "Ada", "arguments": ["one", "two"]}',
    '{"name": "file_editor"}',
    "I created test.txt with the text \"approval text\".",
    "",
])
def test_an_ordinary_reply_is_not_flagged(reply):
    assert not unreadable_tool_call(reply, TOOLS)


def test_none_and_no_tool_names():
    assert not unreadable_tool_call(None, TOOLS)
    # the markup shapes do not need the tool names; only the bare-JSON shape does
    assert unreadable_tool_call(GLM_REAL[0], frozenset())
    assert not unreadable_tool_call('{"name": "terminal", "arguments": {}}', frozenset())


def test_the_notice_never_says_nothing_ran_when_actions_did():
    assert unreadable_call_notice([]) == UNREADABLE_CALL_NOTICE
    assert unreadable_call_notice(["⚙ terminal: git push"]) == UNREADABLE_CALL_AFTER_ACTIONS_NOTICE
    assert "nothing ran" not in UNREADABLE_CALL_AFTER_ACTIONS_NOTICE


def test_the_notice_says_nothing_ran():
    assert UNREADABLE_CALL_NOTICE == (
        "The model tried to call a tool, but its call couldn't be read, so nothing ran. Ask again, or switch models."
    )


# ---------- the flag reaches the surfaces ----------

def _leaking_session(tmp_path, reply_text):
    """A session whose conversation ends its turn with ``reply_text`` as the agent's message."""
    from types import SimpleNamespace

    from levain.session import EntitySession
    from tests.test_session import _Binding, _Event

    class _Conv:
        def __init__(self):
            self.state = SimpleNamespace(events=[], execution_status="idle")
            self.agent = SimpleNamespace(tools_map={n: object() for n in TOOLS})

        def send_message(self, message):
            self.state.events.append(_Event("user", [message]))

        def run(self):
            self.state.events.append(_Event("agent", [reply_text]))
            self.state.execution_status = "finished"

    return EntitySession(
        entity_dir=tmp_path, binding=_Binding(), conversation=_Conv(), workspace=tmp_path / "workspace",
        model_label="m", with_tools=False, bash_ok=False, gate_mode="ungated",
    )


def test_a_turn_ending_in_leaked_markup_is_marked_and_its_text_kept(tmp_path):
    result = _leaking_session(tmp_path, GLM_REAL[0]).run_turn("create test.txt")
    assert result.unreadable_call is True
    assert result.reply == GLM_REAL[0]            # kept as it arrived, for the collapsed display
    assert result.exit_code == 7 and not result.ok   # 0.6.10: not an answer, so not exit 0


def test_a_bare_json_call_needs_the_conversations_own_tool_names(tmp_path):
    call = '{"name": "file_editor", "arguments": {"command": "create"}}'
    assert _leaking_session(tmp_path, call).run_turn("x").unreadable_call is True
    other = '{"name": "spreadsheet", "arguments": {"cell": "A1"}}'
    assert _leaking_session(tmp_path, other).run_turn("x").unreadable_call is False


def test_an_ordinary_reply_is_not_marked(tmp_path):
    result = _leaking_session(tmp_path, "The model emitted a <tool_call> tag.").run_turn("x")
    assert result.unreadable_call is False


def test_the_chat_payload_carries_the_flag():
    from levain.chat import _turn_payload
    from levain.session import TurnResult

    assert _turn_payload(TurnResult(reply=GLM_REAL[1], unreadable_call=True))["unreadable_call"] is True
    assert _turn_payload(TurnResult(reply="hi"))["unreadable_call"] is False


def test_the_repl_shows_the_notice_not_the_markup_as_a_reply(capsys):
    from types import SimpleNamespace

    from levain.run import _render_turn
    from levain.session import TurnResult

    raw = GLM_REAL[0] + "café"
    _render_turn(SimpleNamespace(label="ent"), TurnResult(reply=raw, unreadable_call=True))
    out = capsys.readouterr().out
    assert UNREADABLE_CALL_NOTICE in out
    assert "ent ›" not in out                      # never the entity's reply line
    assert "caf\\u{00E9}" in out and "é" not in out   # escaped by the allowlist display


def test_run_task_puts_the_notice_on_stderr_and_exits_7_with_no_payload(tmp_path, monkeypatch, capsys):
    # 0.6.10 (complement L3): it exited 0 with an empty stdout, and with --quiet put the markup on stdout as the answer
    from levain.run import run_task
    from levain.session import EXIT_UNREADABLE_CALL, TurnResult
    from tests.test_session import _FakeSession, _patch_open

    assert EXIT_UNREADABLE_CALL == 7
    for quiet in (False, True):
        sess = _FakeSession(tmp_path, TurnResult(reply=GLM_REAL[2], unreadable_call=True))
        _patch_open(monkeypatch, sess)
        assert run_task(tmp_path, "create it", quiet=quiet) == EXIT_UNREADABLE_CALL
        out = capsys.readouterr()
        assert UNREADABLE_CALL_NOTICE in out.err and GLM_REAL[2] not in out.out


def test_the_panel_shows_the_same_sentence():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "levain" / "templates" / "web" / "dashboard_chat.js").read_text()
    assert f'const UNREADABLE_CALL_NOTICE = "{UNREADABLE_CALL_NOTICE}";' in js
    assert f'const UNREADABLE_CALL_AFTER_ACTIONS_NOTICE = "{UNREADABLE_CALL_AFTER_ACTIONS_NOTICE}";' in js


def test_the_repl_notice_after_actions_ran_does_not_say_nothing_ran(capsys):
    from types import SimpleNamespace

    from levain.run import _render_turn
    from levain.session import TurnResult

    _render_turn(SimpleNamespace(label="ent"), TurnResult(
        reply="Done with the push.\n" + GLM_REAL[1], tool_activity=["⚙ terminal: git push"], unreadable_call=True))
    out = capsys.readouterr().out
    assert "terminal: git push" in out and UNREADABLE_CALL_AFTER_ACTIONS_NOTICE in out and "nothing ran" not in out


# ---------- L3 r1 ----------

def test_deeply_nested_json_is_not_a_call_and_never_raises():
    assert unreadable_tool_call("[" * 3000 + "]" * 3000, TOOLS) is False
    # 0.6.10 (head's ruling on L2 Q1 b): a call naming an own tool inside other text counts, nesting included
    assert unreadable_tool_call("[" * 1200 + '{"name": "terminal", "arguments": {}}' + "]" * 1200, TOOLS) is True
    assert unreadable_tool_call('[[{"name": "terminal", "arguments": {}}]]', TOOLS) is True


@pytest.mark.parametrize("reply", [
    "Here is the format:\n~~~xml\n<tool_call>{\"name\": \"terminal\"}</tool_call>\n~~~\nThat is a call.",
    "Here is the format:\n\n    <tool_call>{\"name\": \"terminal\"}</tool_call>\n    </arg_key><arg_value>x\n\nAs above.",
    "Here:\n````\n```\n<tool_call>{\"name\": \"terminal\"}</tool_call>\n```\n````",
    "Use ``<tool_call>{...}`` around it.",
])
def test_markup_in_other_code_forms_is_an_answer(reply):
    assert not unreadable_tool_call(reply, TOOLS)


def test_a_whole_tilde_fence_of_call_json_is_flagged():
    assert unreadable_tool_call('~~~json\n{"name": "terminal", "arguments": {"command": "ls"}}\n~~~', TOOLS)


def test_a_classifier_failure_never_fails_the_turn(tmp_path, monkeypatch):
    import levain.session as session_mod

    def boom(*a, **k):
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(session_mod, "unreadable_tool_call", boom)
    result = _leaking_session(tmp_path, GLM_REAL[0]).run_turn("x")
    # 0.6.10 (L1): it failed open, and with exit 7 now riding on the flag that meant exit 0 for unread markup
    assert result.error is None and result.reply == GLM_REAL[0] and result.unreadable_call is True



def test_unreadable_tool_names_fail_closed(tmp_path):
    # L3 r2 (complement LOW): an unreadable tools_map read as no tools, so a bare call naming one passed with exit 0
    session = _leaking_session(tmp_path, '{"name": "terminal", "arguments": {"command": "touch x"}}')
    session.conversation.agent = object()
    result = session.run_turn("x")
    assert result.error is None and result.unreadable_call is True and result.exit_code == 7

# ---------- L3 r2: fences by CommonMark 0.31.2 s4.5 ----------

def test_a_longer_closing_fence_closes_and_a_leak_after_it_is_caught():
    quoted = "Example:\n```\nquoted\n````\n"
    assert unreadable_tool_call(quoted + '<tool_call>{"name": "terminal", "arguments": {}}', TOOLS)
    assert unreadable_tool_call(quoted + GLM_REAL[1], TOOLS)
    assert unreadable_tool_call('~~~json\n{"name": "terminal", "arguments": {}}\n~~~~', TOOLS)


def test_crlf_fences_close():
    assert unreadable_tool_call("Example:\r\n```\r\nquoted\r\n```\r\n" + GLM_REAL[1], TOOLS)
    assert not unreadable_tool_call("Example:\r\n```\r\n<tool_call>{\"name\": 1}\r\n```\r\nok", TOOLS)


def test_a_closer_of_the_other_character_or_shorter_does_not_close():
    # inside a ``` fence, ~~~ and `` are content, so the markup after them is still quoted (prose before it, since a
    # reply that is only one fence is read as the call it holds)
    assert not unreadable_tool_call("Example:\n```\n~~~\n``\n<tool_call>{\"name\": \"terminal\"}\n```", TOOLS)


# ---------- L3 r3 (Phill's B): code spans by CommonMark 0.31.2 s6.1, lines by s2.1 ----------

CALL = '<tool_call>{"name": "terminal", "arguments": {}}'


def test_a_code_span_that_crosses_a_line_ending_is_code():
    assert not unreadable_tool_call("Example: `quoted\n" + CALL + "` done", TOOLS)
    assert not unreadable_tool_call("Example: ``a\n" + GLM_REAL[1] + "\n`` done", TOOLS)


def test_a_span_ends_with_its_paragraph():
    # a blank line ends the paragraph, so the opener there has no closer and is literal; the call after it is real
    assert unreadable_tool_call("Stray ` backtick.\n\n" + CALL + "\n\nand a later ` one", TOOLS)
    # a fence ends the paragraph too
    assert unreadable_tool_call("Stray ` here\n```\nx\n```\n" + CALL + "\n`", TOOLS)


def test_an_opener_without_an_equal_closer_is_literal():
    assert unreadable_tool_call("Look: ``\n" + CALL + "\n` (one backtick does not close two)", TOOLS)


def test_a_backslash_escaped_backtick_does_not_open_a_span():
    assert unreadable_tool_call("A literal \\`\n" + CALL + "\nand `", TOOLS)


def test_only_cr_lf_and_crlf_end_lines():
    # U+2028 is not a line ending: the ``` after it does not close the fence, so the markup stays quoted
    assert not unreadable_tool_call("Example:\n```\nquoted\u2028```\n" + CALL + "\n```\nok", TOOLS)
    assert unreadable_tool_call("Example:\r" + GLM_REAL[1], TOOLS)


def test_an_indented_block_of_call_json_is_not_a_whole_fence():
    # 0.6.10 (head's ruling on L2 Q3): an indented block alone is read like a lone fence, so its call is found
    assert unreadable_tool_call('    ```json\n    {"name": "terminal", "arguments": {}}\n    ```', TOOLS)
    # r3 LOW: a leading strip made this 4-space opener a column-0 fence; it is an indented code line, not a fence,
    # and the call after it is prose (0.6.10: a call in prose naming an own tool counts)
    assert unreadable_tool_call('    ```json\n{"name": "terminal", "arguments": {}}\n```', TOOLS)
    assert unreadable_tool_call('\n```json\n{"name": "terminal", "arguments": {}}\n```\n', TOOLS)


# ---------- L3 r4 (Phill's A): code regions come from a CommonMark parser ----------

@pytest.mark.parametrize("reply", [
    # a heading, a list item or a block quote ends the paragraph, so a stray backtick cannot pair across it
    "Stray ` here\n# heading\n" + CALL + "\n`",
    "Stray ` here\n- item\n\n" + CALL + "\n`",
    # an autolink or an inline HTML tag owns its backtick
    "<https://example.com/`>\n" + CALL + "\n`",
    '<a title="`">x</a>\n' + CALL + "\n`",
])
def test_a_real_leak_is_not_hidden_by_a_backtick_another_construct_owns(reply):
    assert unreadable_tool_call(reply, TOOLS)


def test_markup_after_a_code_span_is_outside_it():
    # A' (Phill 2026-10-05): only what the span holds is code; markup right after it is a leak
    assert unreadable_tool_call("`x`" + CALL, TOOLS)
    assert not unreadable_tool_call("`x" + CALL + "`", TOOLS)


def test_an_entity_or_escape_is_quoted_markup():
    assert not unreadable_tool_call("&lt;tool_call>{\"name\": \"terminal\"}", TOOLS)
    assert not unreadable_tool_call("\\<tool_call>{\"name\": \"terminal\"}", TOOLS)


def test_many_blank_lines_are_cheap():
    import time

    t = time.perf_counter()
    assert unreadable_tool_call("\n" * 150_000 + GLM_REAL[1], TOOLS)
    assert unreadable_tool_call("`" * 2000 + " " + "``` " * 2000 + "\n" + CALL, TOOLS)
    assert time.perf_counter() - t < 5


# ---------- L3 r5 (Phill's A): block context and line starts come from the parser's tokens ----------

@pytest.mark.parametrize("reply", [
    "# " + CALL,
    "**" + CALL + "**",
    "*" + CALL + "*",
    "[" + CALL + "](https://example.com)",
    "> " + CALL,
    "- " + CALL,
    "1. " + GLM_REAL[1],
    "Intro.\n\n> quoted:\n> " + CALL,
])
def test_markup_in_a_heading_emphasis_link_quote_or_list_is_a_leak(reply):
    # A' (Phill 2026-10-05): tool-call markup outside a code region is a leak wherever it is, by design. (r5 had
    # these as answers; the block-context exclusions that did that hid real leaks in r6.)
    assert unreadable_tool_call(reply, TOOLS)


def test_the_hermes_form_with_the_json_on_the_next_line_is_caught():
    assert unreadable_tool_call('<tool_call>\n{"name": "file_editor", "arguments": {"command": "create"}}\n</tool_call>', TOOLS)
    assert unreadable_tool_call("Done.\n# Next\n" + CALL, TOOLS)


def test_the_parser_is_compiled_before_any_turn_uses_it():
    # codex L3 r5: the rule caches are built on first use, publishing an empty cache first; two sessions' first
    # replies could race. Import parses once, so every chain is already compiled.
    from levain.firing import agent_reply

    for ruler in (agent_reply._MD.core.ruler, agent_reply._MD.block.ruler, agent_reply._MD.inline.ruler,
                  agent_reply._MD.inline.ruler2):
        assert getattr(ruler, "__cache__") is not None


def test_reference_definitions_are_cheap():
    # codex L3 r5: markdown-it-py 3.0.0 took 23 s here (quadratic); the floor is 4.0
    import time

    t = time.perf_counter()
    unreadable_tool_call("".join(f"[r{i}]: /{i}\n" for i in range(5000)), TOOLS)
    assert time.perf_counter() - t < 5


def test_the_dependency_floor_is_four():
    import tomllib
    from pathlib import Path

    deps = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
    assert "markdown-it-py>=4,<5" in deps



# ---------- L3 r6 (Phill's A'): outside code is a leak; a size bound ----------

def test_markup_mentioned_in_prose_is_a_leak_by_design():
    assert unreadable_tool_call("Its markup pairs tags like </arg_key><arg_value> around each argument.", TOOLS)
    assert unreadable_tool_call('The format is <tool_call>{"name": ...}</tool_call>, see docs.', TOOLS)
    # the bare tag with no call after it is not the wrapper shape
    assert not unreadable_tool_call("GLM wraps a call in a <tool_call> tag.", TOOLS)


@pytest.mark.parametrize("reply", [
    # r6: each was missed by the block-context rule
    "file_editor<arg_key>command</arg_key><arg_value>create</arg_value><arg_key>file_text</arg_key><arg_value>Title\n---\nbody</arg_value>",
    "1. do\n" + CALL,
    "Steps:\n- one\nfile_editor<arg_key>command</arg_key><arg_value>x",
    '<tool_call>\n\n{"name": "file_editor", "arguments": {}}\n</tool_call>',
    '<tool_call>\r\n \r\n{"name": "file_editor", "arguments": {}}',
    "x</arg_key>\n\n<arg_value>y",
])
def test_leaks_next_to_headings_lists_or_blank_lines_are_caught(reply):
    assert unreadable_tool_call(reply, TOOLS)


def test_a_reply_over_the_bound_is_searched_without_the_parser():
    # 0.6.10 (gpt-oss L3): over the bound it was not classified at all, so a large leak read as an answer
    from levain.firing.agent_reply import MAX_CLASSIFIED_BYTES

    pad = "a" * MAX_CLASSIFIED_BYTES
    assert unreadable_tool_call(GLM_REAL[1] + pad, TOOLS)
    assert unreadable_tool_call(GLM_REAL[1] + pad[: MAX_CLASSIFIED_BYTES - len(GLM_REAL[1])], TOOLS)
    assert unreadable_tool_call(GLM_REAL[1] + "\u00e9" * (MAX_CLASSIFIED_BYTES // 2), TOOLS)
    # over the bound nothing counts as code, so even quoted markup is flagged; prose with no markup is not
    assert unreadable_tool_call("```\n" + CALL + "\n```\n" + pad, TOOLS)
    assert not unreadable_tool_call("An answer. " + pad, TOOLS)
    # a call written as one large JSON value (a big file_text) is the third shape
    big = '{"name": "file_editor", "arguments": {"command": "create", "file_text": "' + pad + '"}}'
    assert unreadable_tool_call(big, TOOLS)


def test_the_pair_is_searched_once_per_reply():
    # r6 (codex MED): one pair search per line start was quadratic (50,000 lines took 10.6 s)
    import time

    t = time.perf_counter()
    unreadable_tool_call("<arg_key>x\n" * 18_000, TOOLS)
    unreadable_tool_call("<tool_call> \n" * 15_000, TOOLS)
    assert time.perf_counter() - t < 5


# ---------- 0.6.9: the size bound never encodes a reply it will not classify ----------

def test_a_reply_longer_than_the_bound_in_characters_is_never_encoded():
    # L3 r7 (codex): the bound check encoded the whole reply first (300M characters: 0.03 s, ~286 MiB transient).
    # UTF-8 spends at least one byte per character, so a reply with more characters than the bound has more bytes.
    from levain.firing.agent_reply import MAX_CLASSIFIED_BYTES

    class NoEncode(str):
        def encode(self, *a, **k):
            raise AssertionError("encoded a reply already known to be over the bound")

    assert unreadable_tool_call(NoEncode(GLM_REAL[1] + "a" * MAX_CLASSIFIED_BYTES), TOOLS) is True
    # at or under the bound in characters the byte count still decides
    assert unreadable_tool_call(GLM_REAL[1] + "a" * (MAX_CLASSIFIED_BYTES - len(GLM_REAL[1])), TOOLS) is True
    assert unreadable_tool_call(GLM_REAL[1] + "é" * (MAX_CLASSIFIED_BYTES // 2), TOOLS) is True


# ---------- 0.6.10: release-range L3 over 0.6.4..0.6.9 ----------

# codex HIGH, reproduced: a call that did not run, then a finish call, all as text. The finish message was shown as
# the reply ("Created x") and the turn read as an answer.
CALL_THEN_FINISH = (
    '{"name": "terminal", "arguments": {"command": "touch x"}}\n'
    '{"name": "finish", "arguments": {"message": "Created x"}}'
)


def test_a_finish_message_never_hides_a_call_that_did_not_run():
    from levain.firing.agent_reply import humanize_finish_json

    assert humanize_finish_json(CALL_THEN_FINISH) == CALL_THEN_FINISH
    unknown = '{"name": "spreadsheet", "arguments": {}}\n{"name": "finish", "arguments": {"message": "Done"}}'
    assert humanize_finish_json(unknown) == unknown
    # spore-297's shape, think and finish only, is still unwrapped to the reply
    ok = '{"name": "think", "arguments": {"thought": "t"}}\n{"name": "finish", "arguments": {"message": "Hi"}}'
    assert humanize_finish_json(ok) == "Hi"


def test_a_turn_ending_in_a_call_and_a_finish_is_marked(tmp_path):
    result = _leaking_session(tmp_path, CALL_THEN_FINISH).run_turn("create x")
    assert result.unreadable_call is True and result.reply == CALL_THEN_FINISH and not result.ok
    think_finish = '{"name": "think", "arguments": {"thought": "t"}}\n{"name": "finish", "arguments": {"message": "Hi"}}'
    result = _leaking_session(tmp_path, think_finish).run_turn("hello")
    assert result.reply == "Hi" and result.unreadable_call is False and result.ok


# codex MED, reproduced: a reply that is only one fence holding call markup gave the markup search no text at all
@pytest.mark.parametrize("reply", [
    "```\n<tool_call>\n<function=terminal>\n<parameter=command>\ntouch x\n</parameter>\n</function>\n</tool_call>\n```",
    "```xml\n<tool_call>\n{\"name\": \"terminal\", \"arguments\": {\"command\": \"ls\"}} trailing\n</tool_call>\n```",
    "~~~\n" + GLM_REAL[1] + "\n~~~",
])
def test_a_reply_that_is_one_fence_of_call_markup_is_flagged(reply):
    assert unreadable_tool_call(reply, TOOLS)


# REAL reply: glm-5.2:cloud via Ollama on `levain run --task`, 2026-10-07 (lane R residue run), asked to echo a fenced
# Qwen call. Ollama's parser consumed "<tool_call>", so the function tag reached levain without its wrapper.
QWEN_UNWRAPPED_REAL = "```\n<function=terminal>\n<parameter=command>\ntouch x\n</parameter>\n</function>\n```"


def test_a_qwen_function_tag_without_its_wrapper_is_flagged():
    assert unreadable_tool_call(QWEN_UNWRAPPED_REAL, TOOLS)
    assert unreadable_tool_call("Creating it.\n<function=terminal>\n<parameter=command>\ntouch x", TOOLS)


# ---------- 0.6.10 L2 (parser lens), each reproduced ----------

def _multi_part_session(tmp_path, texts):
    """A session whose turn ends with each of ``texts`` as a separate agent message."""
    from types import SimpleNamespace

    from levain.session import EntitySession
    from tests.test_session import _Binding, _Event

    class _Conv:
        def __init__(self):
            self.state = SimpleNamespace(events=[], execution_status="idle")
            self.agent = SimpleNamespace(tools_map={n: object() for n in TOOLS})

        def send_message(self, message):
            self.state.events.append(_Event("user", [message]))

        def run(self):
            self.state.events.extend(_Event("agent", [t]) for t in texts)
            self.state.execution_status = "finished"

    return EntitySession(
        entity_dir=tmp_path, binding=_Binding(), conversation=_Conv(), workspace=tmp_path / "workspace",
        model_label="m", with_tools=False, bash_ok=False, gate_mode="ungated",
    )


def test_a_call_that_is_one_whole_message_after_a_plan_is_marked(tmp_path):
    # the join ("plan\ncall") is not all JSON, so the whole-reply shapes missed the call that ended the turn
    call = '{"name": "terminal", "arguments": {"command": "pytest"}}'
    result = _multi_part_session(tmp_path, ["I'll run the tests and read the source.", call]).run_turn("x")
    assert result.unreadable_call is True and result.exit_code == 7
    fence = "```\n<function=terminal>\n<parameter=command>\npytest\n</parameter>\n</function>\n```"
    assert _multi_part_session(tmp_path, ["I'll run the tests.", fence]).run_turn("x").unreadable_call is True


def test_a_finish_whose_arguments_are_a_json_string_is_unwrapped_and_never_raises(tmp_path):
    from levain.firing.agent_reply import humanize_finish_json

    wire = '{"name": "finish", "arguments": "{\\"message\\": \\"The answer is 4.\\"}"}'
    assert humanize_finish_json(wire) == "The answer is 4."
    assert humanize_finish_json('{"name": "finish", "arguments": "not json"}') == '{"name": "finish", "arguments": "not json"}'
    result = _leaking_session(tmp_path, wire).run_turn("x")
    assert result.reply == "The answer is 4." and result.ok


@pytest.mark.parametrize("reply", [
    '{"type": "function", "function": {"name": "finish", "arguments": {"message": "The answer is 4."}}}',
    '[{"name": "think", "arguments": {"thought": "t"}}, {"name": "finish", "arguments": {"message": "The answer is 4."}}]',
])
def test_the_finish_shapes_the_classifier_reads_are_unwrapped(reply):
    from levain.firing.agent_reply import humanize_finish_json

    assert humanize_finish_json(reply) == "The answer is 4."


def test_minimax_and_bare_glm_calls_are_flagged():
    minimax = ('<minimax:tool_call>\n<invoke name="terminal">\n<parameter name="command">ls</parameter>\n</invoke>\n'
               '</minimax:tool_call>')
    assert unreadable_tool_call(minimax, TOOLS)
    assert unreadable_tool_call("<tool_call>task_tracker</tool_call>", TOOLS)
    assert unreadable_tool_call("<tool_call>task_tracker\n</tool_call>", TOOLS)
    assert not unreadable_tool_call("Close it with <tool_call>whatever</tool_call>.", TOOLS)


# ---------- 0.6.10 L1 + the head's rulings on L1/L2 ----------

def test_a_call_beside_prose_naming_an_own_tool_is_flagged():
    # the head's ruling (Q1 b): the notice is true there, and the text is still shown under it
    assert unreadable_tool_call('{"name":"terminal","arguments":{"command":"ls"}}\nI will now list the files.', TOOLS)
    assert unreadable_tool_call('Listing: {"type": "function", "function": {"name": "terminal", "arguments": "{}"}}', TOOLS)
    # a name that is not one of the entity's tools, or JSON in code, is an answer
    assert not unreadable_tool_call('The record is {"name": "Ada", "arguments": ["a"]} as stored.', TOOLS)
    assert not unreadable_tool_call('Send `{"name": "terminal", "arguments": {}}` to call it.', TOOLS)


def test_an_unterminated_nest_of_call_starts_is_one_pass():
    import time

    t = time.perf_counter()
    assert not unreadable_tool_call("x " + '{"name": ' * 20_000, TOOLS)
    assert not unreadable_tool_call("x " + '{"name": 1} ' * 15_000, TOOLS)
    assert time.perf_counter() - t < 5


def test_an_indented_block_holding_a_call_alone_is_read_like_a_lone_fence():
    assert unreadable_tool_call("    <tool_call>\n    <function=terminal>\n    <parameter=command>\n    ls", TOOLS)
    assert unreadable_tool_call('    {"name": "terminal", "arguments": {"command": "ls"}}', TOOLS)


def test_a_glm_call_cut_after_its_first_key_and_kimis_stripped_call_are_flagged():
    assert unreadable_tool_call("terminal<arg_key>command</arg_key>", TOOLS)
    assert unreadable_tool_call('functions.terminal:0{"command": "ls"}', TOOLS)
    assert not unreadable_tool_call('functions.spreadsheet:0{"cell": "A1"}', TOOLS)


def test_text_sent_beside_a_parsed_finish_is_checked(tmp_path):
    # L1 F2: the SDK puts a response's text into the first action's `thought`; beside a parsed finish("Created x")
    # a terminal call in that text never ran and was never shown
    from types import SimpleNamespace


    sess = _leaking_session(tmp_path, "unused")

    def run():
        sess.conversation.state.events.append(SimpleNamespace(
            source="agent", tool_name="finish",
            action=SimpleNamespace(kind="FinishAction", message="Created x"),
            thought=[SimpleNamespace(text='{"name": "terminal", "arguments": {"command": "touch x"}}')],
        ))
        sess.conversation.state.execution_status = "finished"

    sess.conversation.run = run
    result = sess.run_turn("create x")
    assert result.unreadable_call is True and result.exit_code == 7
    # L3 r1 (codex HIGH): the reply, shown as "what the model sent", must carry the call, not "Created x" alone
    assert result.reply == '{"name": "terminal", "arguments": {"command": "touch x"}}\nCreated x'


def test_a_reply_over_the_bound_is_never_stripped():
    # L1 F5: .strip() copied a reply over the bound that ended in a newline (100M characters: ~100 MB)
    from levain.firing.agent_reply import MAX_CLASSIFIED_BYTES

    class NoStrip(str):
        def strip(self, *a):
            raise AssertionError("copied a reply over the bound")

    assert unreadable_tool_call(NoStrip(GLM_REAL[1] + "a" * MAX_CLASSIFIED_BYTES + "\n"), TOOLS) is True
    assert unreadable_tool_call(NoStrip("\n" + "a" * MAX_CLASSIFIED_BYTES + "\n"), TOOLS) is False


# ---------- 0.6.10 L3 r1 ----------

def test_a_large_array_of_calls_is_never_decoded_whole():
    # codex MED: an 11M-character array of small calls peaked at 146 MB in each of humanize and the classifier
    import tracemalloc

    from levain.firing.agent_reply import humanize_finish_json

    big = "[" + ",".join(['{"name":"think","arguments":{"t":1}}'] * 300_000) + "]"
    tracemalloc.start()
    try:
        assert humanize_finish_json(big) is big
        assert unreadable_tool_call(big, TOOLS)
        assert tracemalloc.get_traced_memory()[1] < 20_000_000
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("reply", [
    # gpt-oss: the scan resumed where the outer decode failed, past the call inside it
    '{"name": {"name": "terminal", "arguments": {}} oops',
    # a call nested in JSON that decodes whole
    'see {"type": "x", "calls": [{"name": "terminal", "arguments": {}}]} ok',
    # complement + gemini: a nest past the decoder's limit ended the scan before the call after it
    'x {"name": "a", "arguments": ' + "[" * 3000 + ' {"name": "terminal", "arguments": {}}',
])
def test_a_call_inside_or_after_other_json_is_found(reply):
    assert unreadable_tool_call(reply, TOOLS)


def test_a_deep_valid_nest_under_the_bound_is_one_pass():
    # retrying at each inner brace was quadratic: a valid 5,000-deep nest took 6 s
    import time

    from levain.firing.agent_reply import MAX_CLASSIFIED_BYTES

    d = 5000
    nest = '{"name": 1, "x": ' * d + '"' + "a" * (MAX_CLASSIFIED_BYTES - 30 * d) + '"' + "}" * d
    t = time.perf_counter()
    assert not unreadable_tool_call(nest, TOOLS)
    assert not unreadable_tool_call(nest[:-d], TOOLS)
    assert time.perf_counter() - t < 3


# ---------- 0.6.10 L3 r2 ----------

CALL_X = '{"name": "terminal", "arguments": {"command": "touch x"}}'


def _session_with_events(tmp_path, events):
    sess = _leaking_session(tmp_path, "unused")

    def run():
        sess.conversation.state.events.extend(events)
        sess.conversation.state.execution_status = "finished"

    sess.conversation.run = run
    return sess


def _action(kind, tool, thought, **fields):
    from types import SimpleNamespace

    return SimpleNamespace(source="agent", tool_name=tool, action=SimpleNamespace(kind=kind, **fields),
                           thought=[SimpleNamespace(text=thought)] if thought else [])


def test_a_call_beside_a_finish_with_no_message_is_the_reply(tmp_path):
    # codex + gemini: with no reply the thought was never checked, so the turn exited 1 with the call hidden
    result = _session_with_events(tmp_path, [_action("FinishAction", "finish", CALL_X, message="")]).run_turn("x")
    assert result.reply == CALL_X and result.unreadable_call is True and result.exit_code == 7


def test_a_thought_restating_a_call_that_ran_is_not_flagged(tmp_path):
    # complement + codex: the thought beside an executed terminal call restated it, and the turn exited 7
    events = [_action("TerminalAction", "terminal", CALL_X, command="touch x"),
              _action("FinishAction", "finish", None, message="done")]
    result = _session_with_events(tmp_path, events).run_turn("x")
    assert result.reply == "done" and result.unreadable_call is False and result.exit_code == 0


def test_a_flagged_message_is_not_repeated_when_its_text_was_repaired(tmp_path):
    # complement + glm: the repaired reply did not contain the raw part, so the part was prepended a second time
    from tests.test_session import _Event

    events = [_Event("agent", ["cafÃ© " + CALL_X]), _action("FinishAction", "finish", None, message="ok")]
    result = _session_with_events(tmp_path, events).run_turn("x")
    assert result.unreadable_call is True and result.reply.count(CALL_X) == 1


@pytest.mark.parametrize("reply", [
    # codex + glm: detection depended on "name" coming first
    'prefix {"arguments": {"command": "ls"}, "name": "terminal"} suffix',
    '{"id": "1", "name": "terminal", "arguments": {}} and more',
])
def test_a_call_is_found_whatever_its_key_order(reply):
    assert unreadable_tool_call(reply, TOOLS)


def test_over_the_bound_key_order_and_extra_keys_do_not_hide_a_call():
    from levain.firing.agent_reply import MAX_CLASSIFIED_BYTES

    pad = " x" * MAX_CLASSIFIED_BYTES
    assert unreadable_tool_call('{"arguments": {}, "name": "terminal"}' + pad, TOOLS)
    assert unreadable_tool_call('{"name": "terminal", "id": "1", "arguments": {}}' + pad, TOOLS)


def test_humanize_measures_its_bound_in_bytes():
    # codex LOW: 190,044 characters of emoji (760,044 bytes) were under the character bound and decoded whole
    from levain.firing.agent_reply import humanize_finish_json

    emoji = '{"name": "finish", "arguments": {"message": "' + "\U0001F600" * 190_000 + '"}}'
    assert humanize_finish_json(emoji) == emoji
