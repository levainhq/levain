"""CH-D1c-B, R8-6 + R9-9: the chat core files its requests with its own ``chat:`` event-id prefix
(``ConsentQueue.file(event_source="chat")``), so the take path and the hook-event index never see a chat-core row,
while the dialog-outcome match and the receipt exporter still pair a chat row by its tool-use id."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from levain.cockpit.consent import ConsentError
from levain.consent.agentboundary import _tool_use_id_of
from levain.consent.queue import EVENT_SOURCES
from tests.test_consent_queue import authority, clock, keys, queue, signed  # noqa: F401 - the shared fixtures

INPUT = {"command": "touch x"}
CWD = "/work/proj"


def _file(q, event_id, *, source=None, session="sess-1", tool_input=INPUT):
    kw = {} if source is None else {"event_source": source}
    return q.file(agent="claude_code", agent_session_id=session, tool_name="Bash", tool_input=tool_input, cwd=CWD,
                  hold=timedelta(minutes=5), event_id=event_id, **kw)


def _event_id(q, request_id):
    conn = sqlite3.connect(q.directory / "queue.db")
    try:
        return conn.execute("SELECT event_id FROM requests WHERE id = ?", (request_id,)).fetchone()[0]
    finally:
        conn.close()


def test_queue_file_event_source_hook_byte_identical(queue):
    assert EVENT_SOURCES == ("hook", "chat")
    assert _event_id(queue, _file(queue, "toolu_1").id) == "hook:toolu_1"
    assert _event_id(queue, _file(queue, "toolu_2", source="hook").id) == "hook:toolu_2"
    assert _event_id(queue, _file(queue, None).id).startswith("rand:")


def test_queue_file_chat_source_stores_chat_prefix(queue):
    assert _event_id(queue, _file(queue, "toolu_1", source="chat").id) == "chat:toolu_1"


@pytest.mark.parametrize("source,event_id", [("chat", None), ("chat", ""), ("Chat", "toolu_1"), ("rand", "x")])
def test_queue_file_refuses_a_chat_filing_without_an_id_or_another_source(queue, source, event_id):
    with pytest.raises(ConsentError):
        _file(queue, event_id, source=source)


def test_chat_core_filing_after_gate_filing_same_tool_use_id_succeeds(queue):
    _file(queue, "toolu_1")
    with pytest.raises(Exception):
        _file(queue, "toolu_1")                      # the hook-event index: a hook retry is refused
    assert _file(queue, "toolu_1", source="chat").id  # the chat core's own filing of that call is not


@pytest.mark.parametrize("source,taken", [("chat", False), (None, True)])
def test_take_gate_allow_never_takes_chat_core_row(queue, keys, source, taken):
    request = _file(queue, "toolu_1", source=source)
    queue.answer(request.id, *signed(queue, request, keys["phone"]))
    took = queue.take_gate_allow("claude_code", "sess-1", "Bash", INPUT, cwd=CWD, within=timedelta(seconds=5))
    assert (took is not None) is taken
    if taken:
        assert took.request_id == request.id


def test_dialog_outcome_matches_chat_row_by_id(queue):
    first = _file(queue, "toolu_1", source="chat")
    _file(queue, "toolu_2", source="chat")
    got = queue.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                           event="PostToolUse", tool_use_id="toolu_1")
    assert got is not None and got[0] == first.id


def test_dialog_outcome_fallback_skips_chat_rows(queue):
    _file(queue, "toolu_1", source="chat")
    assert queue.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                            event="PostToolUse", tool_use_id="toolu_9") is None


def test_dialog_outcome_hook_rows_unchanged(queue):
    first = _file(queue, "toolu_1")
    got = queue.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash", tool_input=INPUT,
                           event="PostToolUse", tool_use_id="toolu_1")
    assert got is not None and got[0] == first.id


@pytest.mark.parametrize("event_id,want", [("hook:toolu_1", "toolu_1"), ("chat:toolu_1", "toolu_1"),
                                           ("chat:a:b", "a:b"), ("rand:0123", None), ("toolu_1", None)])
def test_exporter_reads_the_tool_use_id_of_either_prefix(event_id, want):
    assert _tool_use_id_of(event_id) == want
