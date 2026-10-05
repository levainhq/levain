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
    # L1 r1: prose mentioning the markup WITHOUT backticks is an answer too
    "Its markup pairs tags like </arg_key><arg_value> around each argument.",
    'The format is <tool_call>{"name": ...}</tool_call>, see docs.',
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
    assert result.exit_code == 0 and result.ok    # display only: the exit contract is unchanged


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


def test_run_task_puts_the_notice_on_stderr_and_keeps_the_quiet_payload(tmp_path, monkeypatch, capsys):
    from levain.run import run_task
    from levain.session import TurnResult
    from tests.test_session import _FakeSession, _patch_open

    sess = _FakeSession(tmp_path, TurnResult(reply=GLM_REAL[2], unreadable_call=True))
    _patch_open(monkeypatch, sess)
    assert run_task(tmp_path, "create it") == 0
    out = capsys.readouterr()
    assert UNREADABLE_CALL_NOTICE in out.err and GLM_REAL[2] not in out.out

    sess = _FakeSession(tmp_path, TurnResult(reply=GLM_REAL[2], unreadable_call=True))
    _patch_open(monkeypatch, sess)
    run_task(tmp_path, "create it", quiet=True)
    out = capsys.readouterr()
    assert out.out == GLM_REAL[2] + "\n" and UNREADABLE_CALL_NOTICE in out.err


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
