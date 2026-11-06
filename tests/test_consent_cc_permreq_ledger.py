"""The PermissionRequest decision path files its decision-time ledger row (1106-51): the live Claude Code hook is a
PermissionRequest hook only, and before this its signed allow left no row in the consent ledger."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from levain.consent.adapters import claude_code as cc
from levain.consent.ledger import Ledger
from tests.test_consent_cc_adapter import AWAY, FakeQueue, event


class DirQueue(FakeQueue):
    def __init__(self, directory, verdict=None, **kw):
        super().__init__(verdict, **kw)
        self.directory = Path(directory)


def run(monkeypatch, queue, e):
    monkeypatch.setattr(cc, "_queue_for", lambda d, s, **kw: queue)
    monkeypatch.setattr(cc, "load_settings", lambda d: AWAY)
    monkeypatch.setattr(cc, "nothing_can_decide", lambda s: False)
    out = io.StringIO()
    assert cc.main(["--queue-dir", str(queue.directory)], stdin=io.StringIO(json.dumps(e)), stdout=out) == 0
    return out.getvalue()


def counts(qdir):
    return Ledger(qdir).counts()


def rows(qdir):
    import sqlite3
    conn = sqlite3.connect(str(Path(qdir) / Ledger(qdir).path.name))
    try:
        return conn.execute("SELECT phase, stamp, tool_use_id, request_id, tool_input_digest FROM calls").fetchall()
    finally:
        conn.close()


def test_a_verified_allow_files_an_answer_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, "allow")
    out = run(monkeypatch, q, event())
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert counts(tmp_path) == [("claude_code", "answer", "unknown", 1)]
    [(phase, stamp, tuid, rid, _)] = rows(tmp_path)
    assert (phase, stamp, tuid, rid) == ("pre", "answer", None, q.requests[0].id)


def test_a_falsey_accepted_tool_input_is_recorded_with_its_own_digest(monkeypatch, tmp_path):
    from levain.consent.record import tool_input_digest
    q = DirQueue(tmp_path, "allow")
    run(monkeypatch, q, event(tool_input=[]))
    assert [r[4] for r in rows(tmp_path)] == [tool_input_digest([])]
    assert tool_input_digest([]) != tool_input_digest({})


def test_a_verified_deny_files_a_denied_by_gate_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, "deny")
    run(monkeypatch, q, event())
    assert counts(tmp_path) == [("claude_code", "unknown", "refused", 1)]


def test_no_decision_files_no_row(monkeypatch, tmp_path):
    q = DirQueue(tmp_path, None)
    assert run(monkeypatch, q, event()) == ""
    assert counts(tmp_path) == []


def test_a_ledger_that_cannot_be_written_never_changes_the_answer(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("ledger down")
    # _ledger_note swallows its own failures; this one escapes it, so only decide()'s own try/except holds the answer
    monkeypatch.setattr(cc, "_ledger_note", boom)
    out = run(monkeypatch, DirQueue(tmp_path, "allow"), event())
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert counts(tmp_path) == []


def test_a_gate_row_failure_inside_the_note_files_nothing_and_keeps_the_answer(monkeypatch, tmp_path):
    import levain.consent.ledger as led

    def boom(*a, **k):
        raise RuntimeError("ledger down")
    monkeypatch.setattr(led, "gate_row", boom)   # _ledger_note imports it at call time, so this patch is the one it reads
    out = run(monkeypatch, DirQueue(tmp_path, "allow"), event())
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert counts(tmp_path) == []


def test_a_gate_take_files_no_second_row(queue, keys, monkeypatch):
    """The PreToolUse gate filed the request (``hook:`` event id) and its ``deliver`` noted the row; the dialog's
    PermissionRequest then takes that allow. Still ONE row."""
    from datetime import timedelta
    from tests.test_consent_queue import signed
    from tests._h4 import h4_env

    monkeypatch.setattr(cc, "nothing_can_decide", lambda s: False)
    tool_input = {"file_path": "/work/proj/.claude/settings.json", "old_string": "a", "new_string": "b"}
    gated = queue.file(agent=cc.AGENT, agent_session_id="sess-1", tool_name="Edit", tool_input=tool_input,
                       cwd="/work/proj", hold=timedelta(minutes=5), tier=cc.AGENT_CONFIG_TIER, event_id="toolu_1")
    queue.answer(gated.id, *signed(queue, gated, keys["laptop"]))
    pre = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": tool_input, "cwd": "/work/proj",
           "session_id": "sess-1", "permission_mode": "acceptEdits", "tool_use_id": "toolu_1"}
    cc._ledger_note(Path(queue.directory), pre, "sess-1", "Edit", tool_input, "/work/proj",
                    decision=queue.decision(gated.id, tool_input=tool_input, posture="acceptEdits"))   # the gate's own row
    dialog = {"hook_event_name": "PermissionRequest", "tool_name": "Edit", "tool_input": tool_input,
              "cwd": "/work/proj", "session_id": "sess-1", "permission_mode": "acceptEdits"}
    out = cc.decide(dialog, queue, cc.AdapterSettings(), home=Path("/home/u"), environ=h4_env())
    assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert len(rows(queue.directory)) == 1 and Ledger(queue.directory).counts() == [("claude_code", "answer", "unknown", 1)]


def test_KNOWN_EDGE_a_permission_request_row_and_a_later_post_row_count_twice(monkeypatch, tmp_path):
    """Pins current behaviour, a known edge (not a wanted one): the PermissionRequest event has no tool_use_id and
    the later PostToolUse row has one, so nothing joins them (``_PER_CALL`` drops a post row only against a pre row
    sharing a non-null tool_use_id). When both hooks are wired the one call counts twice."""
    from levain.consent.ledger import hook_record
    e = event()
    run(monkeypatch, DirQueue(tmp_path, "allow"), e)
    post = {k: v for k, v in e.items() if k != "permission_suggestions"}
    post.update(hook_event_name="PostToolUse", tool_use_id="toolu_9", tool_response={})
    assert hook_record(cc.AGENT, post, queue_dir=tmp_path) is not None
    assert sum(n for *_, n in counts(tmp_path)) == 2


from tests.test_consent_queue import authority, clock, keys, queue  # noqa: E402,F401 - the shared queue fixtures
