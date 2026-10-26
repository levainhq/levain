"""CH-D1b-2: the driver contract's rev 7 / rev 8 completions (CH-D1 rev 15 §10, §8.1): the four new ``Event`` fields,
``DriverSession.reject_control(request_id, text)`` and the session's ``_inbound`` record of every inbound
``control_request``, against :class:`ScriptedSession`. No vendor runs here."""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from levain.coordinator import driver
from levain.coordinator.driver import ApprovalAnswer, CommandResult, DriverSession, Event, ScriptedSession, event


def answer(raw_key: str, reply=None) -> ApprovalAnswer:
    return ApprovalAnswer("op-1", "vk-1", raw_key, reply or {"behavior": "deny", "message": "no"})


def test_event_carries_the_rev7_fields_defaulted():
    assert Event._fields[-4:] == ("reason_type", "blocked_path", "control_subtype", "raw_sha256")
    e = event("agent_text", "a1", "00000001", body="hi")
    assert (e.reason_type, e.blocked_path, e.control_subtype, e.raw_sha256) == (None, None, None, None)
    e = event("approval_request", "r1", "00000002", reason_type="safetyCheck", blocked_path="/x/.git",
              control_subtype="can_use_tool", raw_sha256="sha256:" + "0" * 64)
    assert (e.reason_type, e.blocked_path, e.control_subtype) == ("safetyCheck", "/x/.git", "can_use_tool")


def test_protocol_names_reject_control():
    assert "reject_control" in {n for n in dir(DriverSession) if not n.startswith("__")}
    assert list(inspect.signature(DriverSession.reject_control).parameters) == ["self", "request_id", "text"]
    assert isinstance(ScriptedSession(), DriverSession)


def test_inbound_is_recorded_before_the_event_is_shown():
    s = ScriptedSession()
    e = s.inbound("req-1", "can_use_tool", tool_name="Bash", tool_input={"command": "ls"}, reason_type="other")
    assert s._inbound == {"req-1": "can_use_tool"}
    assert e.kind == "approval_request" and e.request_key == "req-1" and e.host and e.control_subtype == "can_use_tool"
    assert e.reason_type == "other" and e.blocked_path is None and e.tool_name == "Bash"
    line = s.received[-1]
    assert e.raw_sha256 == "sha256:" + hashlib.sha256(line.encode("utf-8")).hexdigest()
    assert json.loads(line)["request"]["subtype"] == "can_use_tool"
    h = s.inbound("req-2", "hook_callback")
    assert h.kind == "error" and h.key == "error:control:req-2" and h.control_subtype == "hook_callback"
    assert s._inbound == {"req-1": "can_use_tool", "req-2": "hook_callback"}
    assert [x.key for x in s.read(None).events] == [e.key, h.key]


def test_reject_control_unknown_request_id_raises():
    s = ScriptedSession()
    with pytest.raises(ValueError, match="no such inbound request"):
        s.reject_control("req-404", "levain does not answer this")
    assert s.written == []


def test_reject_control_on_recorded_can_use_tool_raises():
    s = ScriptedSession()
    s.inbound("req-1", "can_use_tool", tool_name="Bash")
    with pytest.raises(ValueError, match="one door"):
        s.reject_control("req-1", "levain answers approvals only through attempt_answer")
    assert s.written == [] and s._inbound == {"req-1": "can_use_tool"}


def test_reject_control_refuses_can_use_tool():
    # §8.1: no approval, a deny included, leaves the one door through reject_control
    s = ScriptedSession()
    s.inbound("req-1", "can_use_tool", tool_name="Bash")
    for text in ("deny", '{"behavior": "allow"}'):
        with pytest.raises(ValueError):
            s.reject_control("req-1", text)
    assert s.written == []


@pytest.mark.parametrize("subtype,text", [
    ("hook_callback", "levain registers no SDK hooks"), ("mcp_message", "levain hosts no SDK MCP server"),
    ("elicitation", "levain answers no elicitation in chat"), ("set_permission_mode", "levain fixes this launch's mode"),
    ("brand_new_subtype", "levain does not answer brand_new_subtype")])
def test_each_non_approval_subtype_gets_the_error_literal(subtype, text):
    s = ScriptedSession()
    s.inbound("req-9", subtype)
    got = s.reject_control("req-9", text)
    assert got.outcome == "confirmed"
    assert s.written == [{"type": "control_response",
                          "response": {"subtype": "error", "request_id": "req-9", "error": text}}]


def test_answer_on_recorded_non_approval_id_raises():
    s = ScriptedSession()
    s.inbound("req-3", "mcp_message")
    with pytest.raises(ValueError, match="not an approval"):
        s.answer(answer("req-3"))
    assert s.written == [] and s._inbound == {"req-3": "mcp_message"}


def test_answer_on_an_unknown_key_is_no_such_request():
    s = ScriptedSession()
    assert s.answer(answer("req-404")).outcome == "no_such_request"
    assert s.written == []


def test_answer_frames_the_service_reply_verbatim():
    s = ScriptedSession()
    s.inbound("req-1", "can_use_tool", tool_name="Bash")
    reply = {"behavior": "allow", "updatedInput": {"command": "ls"}}
    assert s.answer(answer("req-1", reply)) == CommandResult("confirmed", "answered")
    assert s.written == [{"type": "control_response",
                          "response": {"subtype": "success", "request_id": "req-1", "response": reply}}]


def test_inbound_entry_removed_after_reply():
    s = ScriptedSession()
    s.inbound("req-1", "can_use_tool", tool_name="Bash")
    s.inbound("req-2", "elicitation")
    s.answer(answer("req-1"))
    s.reject_control("req-2", "levain answers no elicitation in chat")
    assert s._inbound == {}
    assert s.answer(answer("req-1")).outcome == "no_such_request"         # answered once; a second reply is refused
    with pytest.raises(ValueError, match="no such inbound request"):
        s.reject_control("req-2", "again")
    assert len(s.written) == 2


def test_reject_control_literal_is_error_only():
    # static (§8.1): the literal reject_control writes has no key an approval could ride on
    lit = driver._control_error("req-1", "x")
    assert set(lit) == {"type", "response"} and set(lit["response"]) == {"subtype", "request_id", "error"}
    assert lit["response"]["subtype"] == "error"
    src = inspect.getsource(driver._control_error)
    for key in ("behavior", "allow", "updatedInput", "success"):
        assert key not in src
    tree = ast.parse(inspect.getsource(ScriptedSession.reject_control).strip().replace("\n    ", "\n"))
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_control_error" in called


def test_driver_still_imports_nothing_from_consent():
    tree = ast.parse(Path(driver.__file__).read_text())
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith("levain") for m in mods)
