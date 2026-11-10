"""RM-C: the Claude stream-json DriverSession over a fake transport, and the containment helpers.

Every frame here is SYNTHETIC, written in the shape the RM0 / RM0B recorded runs showed (init per turn, assistant
tool_use, user tool_result with ``tool_result_meta``, ``result`` with ``permission_denials``, ``control_response``).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from levain.desk import claude_session as cs
from levain.desk import driver
from levain.desk.chat import EVENT_BODY_MAX, EVENT_KINDS
from levain.desk.claude_session import (ClaudeSession, NotLive, PipeTransport, Transport, TransportClosed,
                                               canary_denied, default_deny_paths, open_probe, room_argv,
                                               room_settings_json)
from levain.desk.driver import (ApprovalAnswer, DriverSession, InterruptCommand, NotSent, SendCommand)

SID = "11111111-2222-3333-4444-555555555555"
SECRET = "SYNTHETIC-HOOK-OUTPUT-DO-NOT-KEEP"


# ---- synthetic frames ------------------------------------------------------------------------------------------------
_n = [0]


def uid() -> str:
    _n[0] += 1
    return f"00000000-0000-4000-8000-{_n[0]:012d}"


def init(mode: str = "dontAsk", tools=None, servers=None, version: str = "2.1.317") -> dict:
    return {"type": "system", "subtype": "init", "cwd": "/private/tmp/rmc", "session_id": SID,
            "tools": list(tools if tools is not None else ["Glob", "Grep", "Read", "WebSearch", cs.ROOM_MCP_TOOL]),
            "mcp_servers": servers if servers is not None else [{"name": "levain_room", "status": "connected",
                                                                 "source": "dynamic"}],
            "model": "claude-sonnet-5-5", "permissionMode": mode, "claude_code_version": version, "uuid": uid(),
            "slash_commands": ["a", "b"], "plugins": [{"name": "his-plugin"}], "memory_paths": {"auto": "/x"}}


def hook(sub: str, name: str = "SessionStart:startup", hid: str = "h1") -> dict:
    f = {"type": "system", "subtype": sub, "hook_id": hid, "hook_name": name, "hook_event": name.split(":")[0],
         "session_id": SID, "uuid": uid()}
    if sub == "hook_response":
        f.update(outcome="success", exit_code=0, output=SECRET, stdout=SECRET, stderr="")
    return f


def text(t: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]},
            "parent_tool_use_id": None, "session_id": SID, "uuid": uid()}


def thinking() -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking",
                                                                                 "thinking": SECRET}]},
            "parent_tool_use_id": None, "session_id": SID, "uuid": uid()}


def tool_use(tid: str, path: str, name: str = "Read") -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": {"file_path": path}}]},
        "parent_tool_use_id": None, "session_id": SID, "uuid": uid()}


def tool_result(tid: str, body: str, *, error: bool = False, decision: dict | None = None) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": body}
    if error:
        block["is_error"] = True
    meta = decision or ({"decision": "reject", "source": "config", "reason_type": "rule"} if error else
                        {"decision": "accept", "source": "user_permanent", "reason_type": "rule"})
    return {"type": "user", "message": {"role": "user", "content": [block]}, "parent_tool_use_id": None,
            "session_id": SID, "uuid": uid(), "tool_result_meta": [{"id": tid, "permission_decision": meta}]}


def result(subtype: str = "success", *, denials=(), error: bool = False, reason: str = "completed") -> dict:
    return {"type": "result", "subtype": subtype, "is_error": error, "terminal_reason": reason, "session_id": SID,
            "permission_denials": list(denials), "uuid": uid(), "num_turns": 1}


def aborted() -> dict:
    return result("error_during_execution", error=True, reason="aborted_streaming")


def ctl_response(rid: str, subtype: str = "success") -> dict:
    return {"type": "control_response", "response": {"subtype": subtype, "request_id": rid,
                                                      "response": {"still_queued": []}}}


def ctl_request(rid: str, subtype: str, **req) -> dict:
    return {"type": "control_request", "request_id": rid, "request": {"subtype": subtype, **req}}


# ---- the fake transport ------------------------------------------------------------------------------------------------
class FakeTransport:
    """Replays lines the test feeds; keeps every line the session wrote. ``on_write`` may feed a reply."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.written: list[dict] = []
        self.on_write = None
        self.closed = False
        self.refuse: Exception | None = None

    def feed(self, *frames) -> None:
        self.lines += [f if isinstance(f, str) else json.dumps(f) + "\n" for f in frames]

    def write(self, data: str) -> None:
        if self.refuse is not None:
            raise self.refuse
        assert data.endswith("\n") and data.count("\n") == 1
        self.written.append(json.loads(data))
        if self.on_write is not None:
            self.on_write(self.written[-1])

    def poll(self, wait: float) -> list[str]:
        out, self.lines = self.lines, []
        return out

    def close(self) -> None:
        self.closed = True


def make(**kw) -> tuple[ClaudeSession, FakeTransport]:
    t = FakeTransport()
    return ClaudeSession(t, session_id=SID, **kw), t


def cmd(text_: str = "hi", op: str = "op-0001", mode: str = "queue", attachments=()) -> SendCommand:
    return SendCommand(op, text_, attachments, mode, None)


def kinds(batch) -> list[str]:
    return [e.kind for e in batch.events]


# ---- the contract ------------------------------------------------------------------------------------------------------
def test_is_a_driver_session_with_the_room_capabilities():
    s, _ = make()
    assert isinstance(s, DriverSession) and isinstance(s.harness, str) and s.harness == "claude"
    assert isinstance(s._t, Transport)
    c = s.capabilities()
    assert (c.steer, c.queue) == (False, True)
    assert c.correlation_field is None and c.idempotency_key is False and c.interrupt_by_turn is False
    assert c.vendor_version == ""
    t = s._t
    t.feed(init(version="2.9.9"))
    assert s.capabilities().vendor_version == "2.9.9"


def test_error_literal_is_the_drivers():
    assert cs._control_error("r1", "no") == driver._control_error("r1", "no")


def test_body_cap_is_chats():
    assert cs._BODY_MAX == EVENT_BODY_MAX


# ---- send ----------------------------------------------------------------------------------------------------------------
def test_send_writes_one_stream_json_user_line():
    s, t = make()
    r = s.send(cmd("hello there"))
    assert t.written == [{"type": "user", "message": {"role": "user", "content": "hello there"}}]
    assert r.outcome == "accepted" and r.vendor_turn_id == "t1-1" and r.vendor_item_id == "u1-1"


def test_send_refuses_what_the_room_cannot_carry_before_any_byte():
    s, t = make()
    r = s.send(cmd(mode="steer"))
    assert r.outcome == "not_delivered" and t.written == []
    with pytest.raises(NotSent):
        s.send(cmd(attachments=("sha256:" + "0" * 64,)))
    t.refuse = TransportClosed("gone")
    with pytest.raises(NotSent):
        s.send(cmd())
    t.refuse = None
    s.close()
    with pytest.raises(NotSent):
        s.send(cmd())
    assert t.written == [] and t.closed


def test_a_write_that_may_have_left_is_not_notsent():
    s, t = make()
    t.refuse = BrokenPipeError("pipe")
    with pytest.raises(BrokenPipeError):
        s.send(cmd())
    t.refuse = None
    assert s.pending_lines() == 0          # an uncounted line: a turn it opens is an unclaimed one, never a lie


# ---- init is evidence every turn --------------------------------------------------------------------------------------
def test_init_every_turn_is_evidence_not_a_new_session():
    s, t = make()
    s.send(cmd("one"))
    t.feed(init(), text("a"), result())
    first = s.read(None)
    assert kinds(first) == ["error", "turn_started", "agent_text", "turn_ended"]
    s.send(cmd("two", "op-0002"))
    t.feed(init(), text("b"), result())
    both = s.read(None)
    assert kinds(both).count("turn_started") == 2 and kinds(both).count("turn_ended") == 2
    ev = s.liveness_evidence()
    assert ev.source == "claude init" and ev.raw["session_id"] == SID and ev.raw["permissionMode"] == "dontAsk"
    assert s.meta().vendor_session_id == SID
    assert [e.turn_id for e in both.events if e.kind == "turn_started"] == ["t1-1", "t1-2"]


def test_a_second_init_inside_an_open_turn_opens_nothing():
    s, t = make()
    s.send(cmd())
    t.feed(init(), text("a"), init(), text("b"), result())
    b = s.read(None)
    assert kinds(b).count("turn_started") == 1
    assert {e.turn_id for e in b.events} == {"t1-1"}
    assert sum(1 for e in b.events if e.vendor_id.startswith("mode:")) == 2     # each init reports its mode


def test_evidence_keeps_only_what_the_probe_reads():
    s, t = make()
    t.feed(init())
    raw = s.liveness_evidence().raw
    assert "slash_commands" not in raw and "plugins" not in raw and "memory_paths" not in raw
    assert {"tools", "mcp_servers", "permissionMode", "session_id"} <= set(raw)


def test_meta_before_and_after_the_first_init():
    s, t = make()
    assert s.liveness_evidence().raw is None
    m = s.meta()
    assert m.vendor_session_id == SID and m.permission_mode is None and m.tools == ()
    t.feed(init(mode="default"))
    m = s.meta()
    assert m.permission_mode == "default" and m.model == "claude-sonnet-5-5" and m.vendor_version == "2.1.317"
    assert m.mcp_servers == ("levain_room",) and "Read" in m.tools and m.cwd == "/private/tmp/rmc"
    t.feed(init(mode="bypassPermissions"))             # a later init that reports a change is the change
    assert s.meta().permission_mode == "bypassPermissions"


# ---- normalize ------------------------------------------------------------------------------------------------------------
def test_normalize_maps_frames_to_the_contract_kinds():
    s, t = make()
    s.send(cmd())
    t.feed(hook("hook_started"), hook("hook_response"), init(), thinking(), text("looking"),
           tool_use("toolu_1", "/tmp/x/ok.txt"), tool_result("toolu_1", "1\tGREEN"),
           tool_use("toolu_2", "/tmp/x/.env"), tool_result("toolu_2", "denied", error=True),
           {"type": "rate_limit_event", "rate_limit_info": {}}, {"type": "system", "subtype": "hook_progress",
                                                                 "stdout": SECRET}, result())
    evs = list(s.read(None).events)
    assert all(e.kind in EVENT_KINDS for e in evs)
    assert all(e.key == f"{e.kind}:{e.vendor_id}" and e.key.startswith(e.kind + ":") for e in evs)
    assert len({e.key for e in evs}) == len(evs)
    by = {e.key: e for e in evs}
    call = by["tool_call:toolu_1"]
    assert (call.tool_name, call.call_id, call.tool_input) == ("Read", "toolu_1", {"file_path": "/tmp/x/ok.txt"})
    ok = by["tool_result:toolu_1"]
    assert ok.tool_name == "Read" and ok.call_id == "toolu_1" and ok.body == "1\tGREEN"
    assert ok.decision_source == {"decision": "accept", "source": "user_permanent", "reason_type": "rule"}
    assert by["tool_result:toolu_2"].decision_source["decision"] == "reject"
    assert [e.body for e in evs if e.kind == "agent_text"] == ["looking"]       # the thinking block is not kept
    assert [e.kind for e in evs] == ["error", "error", "error", "turn_started", "agent_text", "tool_call",
                                     "tool_result", "tool_call", "tool_result", "turn_ended"]
    end = [e for e in evs if e.kind == "turn_ended"][0]
    assert end.body == "success completed" and end.turn_id == "t1-1"


def test_hook_events_carry_name_and_outcome_never_output():
    s, t = make()
    t.feed(hook("hook_started", "PreToolUse:Read", "h9"), hook("hook_response", "PreToolUse:Read", "h9"))
    evs = list(s.read(None).events)
    assert [e.hook for e in evs] == [{"event": "hook_started", "name": "PreToolUse:Read"},
                                     {"event": "hook_response", "name": "PreToolUse:Read"}]
    assert evs[1].body == "PreToolUse:Read success"
    blob = repr(evs) + repr(s.recent_frames()) + repr(s.liveness_evidence())
    assert SECRET not in blob


def test_thinking_and_hook_text_never_reach_events_or_recent_frames():
    s, t = make()
    t.feed(thinking(), hook("hook_response"), {"type": "system", "subtype": "hook_progress", "stdout": SECRET})
    assert SECRET not in repr(list(s.read(None).events))
    assert all(f.get("subtype") not in ("hook_response", "hook_started", "hook_progress") for f in s.recent_frames())


def test_a_line_it_cannot_parse_is_an_unparseable_event_without_its_content():
    s, t = make()
    t.feed("this is not json " + SECRET + "\n", "[1, 2]\n", "\n")
    evs = list(s.read(None).events)
    assert len(evs) == 2
    assert all(e.kind == "error" and e.vendor_id.startswith("unparseable:") and SECRET not in e.body for e in evs)
    assert all(e.raw_sha256 and e.raw_sha256.startswith("sha256:") for e in evs)


def test_a_body_is_capped():
    s, t = make()
    t.feed(text("x" * (EVENT_BODY_MAX * 2)), tool_use("toolu_9", "/a"), tool_result("toolu_9", "é" * EVENT_BODY_MAX))
    for e in s.read(None).events:
        assert len(e.body.encode("utf-8")) <= EVENT_BODY_MAX


def test_a_user_text_frame_is_a_user_item_and_a_tool_result_frame_is_not():
    s, t = make()
    f = {"type": "user", "message": {"role": "user", "content": "typed outside"}, "uuid": uid(), "session_id": SID}
    t.feed(f)
    (e,) = s.read(None).events
    assert e.kind == "user_item" and e.body == "typed outside"


def test_the_events_in_a_tool_results_frame_join_the_calls_that_named_the_tool():
    s, t = make()
    t.feed(tool_use("toolu_a", "/a", "Grep"), tool_result("toolu_a", "r"))
    evs = list(s.read(None).events)
    assert evs[1].tool_name == "Grep"


def test_normalize_text():
    s, _ = make()
    assert s.normalize("  a \n\t b  ") == "a b"
    assert s.normalize("é") == s.normalize("é")


# ---- read and positions --------------------------------------------------------------------------------------------------
def test_positions_are_ordered_and_a_read_ends_on_one_boundary():
    s, t = make()
    s.send(cmd())
    t.feed(init(), text("a"), result(), init(), text("b"), result())
    first = s.read(None)                                  # the launch's history: everything, boundaries included
    assert kinds(first).count("turn_ended") == 2
    assert all(s.pos_lt(a.pos, b.pos) for a, b in zip(first.events, first.events[1:]))
    assert first.complete_through == first.next_cursor == first.events[-1].pos
    assert s.position() == first.events[-1].pos
    s.send(cmd("again", "op-0002"))
    t.feed(init(), text("c"), result(), init(), text("d"), result())
    got, cursor = [], first.next_cursor
    while True:
        b = s.read(cursor)
        if not b.events:
            break
        got.append(kinds(b))
        cursor = b.next_cursor
    assert got == [["error", "turn_started"], ["agent_text", "turn_ended"], ["error", "turn_started"],
                   ["agent_text", "turn_ended"]]
    assert all(sum(k in ("turn_started", "turn_ended") for k in g) == 1 and g[-1] in ("turn_started", "turn_ended")
               for g in got)


def test_an_empty_read_keeps_the_cursor():
    s, t = make()
    t.feed(text("a"))
    b = s.read(None)
    again = s.read(b.next_cursor)
    assert again.events == () and again.next_cursor == b.next_cursor and again.gap is False


def test_origin_and_pos_lt_and_foreign_connections():
    a, _ = make()
    b = ClaudeSession(FakeTransport(), conn=2, session_id=SID)
    assert a.origin() == "00000001:000000000000" and a.position() == a.origin()
    assert a.pos_lt("00000001:000000000009", "00000001:000000000010")
    assert a.pos_lt("00000001:999999999999", "00000002:000000000000")       # a later spawn sorts after
    with pytest.raises(ValueError):
        a.pos_lt("1", "2")
    with pytest.raises(ValueError):
        a.read("00000002:000000000001")
    assert b.origin().startswith("00000002:")


def test_position_space_is_a_width_not_a_wrap():
    s, t = make()
    s._seq = cs.SEQ_MAX
    t.feed(text("a"))
    with pytest.raises(NotLive):
        s.read(None)
    with pytest.raises(NotLive):
        ClaudeSession(FakeTransport(), conn=cs.CONN_MAX + 1)


def test_events_is_the_whole_history():
    s, t = make()
    t.feed(text("a"), text("b"))
    assert [e.body for e in s.events()] == ["a", "b"]


# ---- the mid-turn fold ------------------------------------------------------------------------------------------------------
def test_a_recorded_shape_fold_is_one_turn_and_leaves_no_stranded_line():
    s, t = make()
    r1 = s.send(cmd("long task", "op-0001"))
    t.feed(init(), tool_use("toolu_1", "/a"))
    s.read(None)
    r2 = s.send(cmd("also PINEAPPLE", "op-0002"))        # mid-turn: folds into the running turn
    assert r1.vendor_turn_id == r2.vendor_turn_id == "t1-1"
    assert s.turn_lines("t1-1") == 2 and s.pending_lines() == 2
    assert r2.detail == "folded into the running turn"
    t.feed(tool_result("toolu_1", "x"), text("done PINEAPPLE"), result())         # ONE result for two lines
    b = s.read(None)
    assert kinds(b).count("turn_ended") == 1
    assert s.pending_lines() == 0 and s.turn_lines("t1-1") == 2 and s.unclaimed_turns() == 0
    r3 = s.send(cmd("next", "op-0003"))                   # the next line is the next turn, not a stranded one
    assert r3.vendor_turn_id == "t1-2" and s.pending_lines() == 1


def test_lines_written_before_the_first_init_are_claimed_by_that_turn():
    s, t = make()
    a = s.send(cmd("one", "op-0001"))
    b = s.send(cmd("two", "op-0002"))
    assert a.vendor_turn_id == b.vendor_turn_id == "t1-1" and s.pending_lines() == 2
    t.feed(init(), text("both"), result())
    s.read(None)
    assert s.turn_lines("t1-1") == 2 and s.pending_lines() == 0 and s.unclaimed_turns() == 0


def test_a_line_the_vendor_runs_as_its_own_turn_is_counted_and_shows_as_unclaimed():
    """The result was already on the wire when the line was counted into the running turn: the vendor opens a turn
    for it, and the accounting names it instead of losing it."""
    s, t = make()
    s.send(cmd("one", "op-0001"))
    t.feed(init(), text("a"))
    s.read(None)
    s.send(cmd("late", "op-0002"))                        # looks mid-turn: the result is not yet read
    t.feed(result(), init(), text("b"), result())
    s.read(None)
    assert s.unclaimed_turns() == 1 and s.pending_lines() == 0


def test_a_turn_aborted_before_its_init_ends_the_awaiting_line():
    s, t = make()
    s.send(cmd("x", "op-0001"))
    t.feed(aborted())                                     # the interrupt landed before init (RM0 C)
    b = s.read(None)
    assert kinds(b) == ["turn_ended"] and b.events[0].turn_id == "t1-1" and "aborted_streaming" in b.events[0].body
    assert s.pending_lines() == 0 and s.turn_lines("t1-1") == 1 and s.unclaimed_turns() == 0


# ---- interrupt ----------------------------------------------------------------------------------------------------------------
def test_interrupt_is_the_stream_json_control_request_and_the_process_survives():
    s, t = make(interrupt_wait=1.0)
    s.send(cmd())
    t.feed(init(), tool_use("toolu_1", "/a"))
    s.read(None)
    t.on_write = lambda line: t.feed(ctl_response(line["request_id"])) if line["type"] == "control_request" else None
    r = s.interrupt(InterruptCommand("op-int-1", "t1-1"))
    assert r.outcome == "confirmed"
    wrote = t.written[-1]
    assert wrote == {"type": "control_request", "request_id": wrote["request_id"], "request": {"subtype": "interrupt"}}
    assert re.fullmatch(r"int-1-1-[0-9a-f]{16}", wrote["request_id"])
    t.feed(aborted())
    end = s.read(None).events[-1]
    assert end.kind == "turn_ended" and end.body == "error_during_execution aborted_streaming is_error"
    assert s.pending_lines() == 0 and not t.closed
    nxt = s.send(cmd("after", "op-0002"))                 # the process takes the next line
    assert nxt.vendor_turn_id == "t1-2"
    t.feed(init(), text("OK"), result())
    assert kinds(s.read(None))[-1] == "turn_ended"


def test_interrupt_with_no_response_in_time_is_ambiguous_and_a_vendor_error_is_rejected():
    now = [0.0]

    def clock() -> float:
        now[0] += 0.5
        return now[0]
    s, t = make(interrupt_wait=2.0, clock=clock)
    assert s.interrupt(InterruptCommand("op-a", None)).outcome == "ambiguous"
    t.on_write = lambda line: t.feed(ctl_response(line["request_id"], "error"))
    assert s.interrupt(InterruptCommand("op-b", None)).outcome == "rejected"


def test_a_stale_turn_id_is_never_interrupted():
    s, t = make()
    s.send(cmd())
    t.feed(init(), text("a"), result())
    s.read(None)
    n = len(t.written)
    r = s.interrupt(InterruptCommand("op-x", "t1-1"))     # that turn has ended
    assert r.outcome == "rejected" and len(t.written) == n


def test_interrupt_on_a_closed_transport_writes_nothing():
    s, t = make()
    t.refuse = TransportClosed("gone")
    assert s.interrupt(InterruptCommand("op-x", None)).outcome == "rejected"
    t.refuse = BrokenPipeError()
    assert s.interrupt(InterruptCommand("op-y", None)).outcome == "ambiguous"
    s.close()
    assert s.interrupt(InterruptCommand("op-z", None)).outcome == "rejected"


# ---- inbound control requests --------------------------------------------------------------------------------------------------
def test_every_inbound_control_request_is_recorded_and_surfaced():
    s, t = make()
    t.feed(init(), ctl_request("req-A", "can_use_tool", tool_name="Bash", input={"command": "ls"},
                               decision_reason_type="mode", blocked_path="/x"),
           ctl_request("req-B", "hook_callback"), ctl_request("req-C", "set_permission_mode"))
    evs = {e.key: e for e in s.read(None).events}
    a = evs["approval_request:req-A"]
    assert (a.host, a.request_key, a.tool_name, a.control_subtype) == (True, "req-A", "Bash", "can_use_tool")
    assert a.reason_type == "mode" and a.blocked_path == "/x" and a.tool_input == {"command": "ls"}
    assert a.raw_sha256.startswith("sha256:")
    b = evs["error:control:req-B"]
    assert b.kind == "error" and b.control_subtype == "hook_callback" and b.raw_sha256.startswith("sha256:")
    assert "error:control:req-C" in evs


def test_reject_control_writes_the_error_literal_for_any_other_subtype():
    s, t = make()
    t.feed(ctl_request("req-B", "mcp_message"), ctl_request("req-C", "something_new"))
    s.read(None)
    assert s.reject_control("req-B", "no").outcome == "confirmed"
    assert t.written[-1] == driver._control_error("req-B", "no")
    assert s.reject_control("req-C", "no").outcome == "confirmed"
    assert s.denials() == ()
    with pytest.raises(ValueError, match="no such inbound request"):
        s.reject_control("never-seen", "no")
    with pytest.raises(ValueError, match="no such inbound request"):
        s.reject_control("req-B", "again")               # one reply per request


def test_a_failed_reply_is_ambiguous_and_never_written_twice():
    s, t = make()
    t.feed(ctl_request("req-A", "mcp_message"))
    s.read(None)
    t.refuse = BrokenPipeError()
    assert s.reject_control("req-A", "no").outcome == "ambiguous"
    t.refuse = None
    assert s.reject_control("req-A", "no").outcome == "ambiguous" and t.written == []


def test_a_closed_transport_leaves_the_request_answerable():
    s, t = make()
    t.feed(ctl_request("req-A", "mcp_message"))
    s.read(None)
    t.refuse = TransportClosed("gone")
    assert s.reject_control("req-A", "no").outcome == "rejected"
    t.refuse = None
    assert s.reject_control("req-A", "no").outcome == "confirmed"


def test_a_cancelled_request_is_forgotten():
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Bash", input={}))
    s.read(None)
    t.feed({"type": "control_cancel_request", "request_id": "req-A"})
    (e,) = s.read(s.position()).events
    assert e.kind == "error" and e.cancel_of == "req-A"
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "no_such_request"
    with pytest.raises(ValueError):
        s.reject_control("req-A", "no")
    assert t.written == []


def test_a_replayed_request_id_keeps_its_first_subtype():
    s, t = make()
    t.feed(ctl_request("req-A", "mcp_message"), ctl_request("req-A", "can_use_tool", tool_name="Bash", input={}))
    s.read(None)
    s.reject_control("req-A", "no")
    assert t.written[-1] == driver._control_error("req-A", "no")      # never moved across the one door


def test_a_control_request_with_no_id_is_unparseable():
    s, t = make()
    t.feed({"type": "control_request", "request": {"subtype": "can_use_tool"}})
    (e,) = s.read(None).events
    assert e.vendor_id.startswith("unparseable:")


# ---- close ----------------------------------------------------------------------------------------------------------------------
def test_close_is_idempotent_and_closes_the_transport_once():
    s, t = make()
    s.close()
    s.close()
    assert s.closed and t.closed


# ---- the real pipe transport ------------------------------------------------------------------------------------------------------
_ECHO_CHILD = """
import sys, json
for line in sys.stdin:
    o = json.loads(line)
    if o.get("type") == "user":
        for f in ({"type": "system", "subtype": "init", "session_id": "s", "tools": [], "mcp_servers": [],
                   "permissionMode": "dontAsk", "uuid": "u-1"},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "echo " + o["message"]["content"]}]},
                   "uuid": "u-2"},
                  {"type": "result", "subtype": "success", "uuid": "u-3", "is_error": False}):
            print(json.dumps(f), flush=True)
"""


def test_pipe_transport_round_trip_over_a_real_child():
    import subprocess
    proc = subprocess.Popen([sys.executable, "-I", "-c", _ECHO_CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    t = PipeTransport(proc)
    s = ClaudeSession(t, session_id="s", read_wait=0.2)
    try:
        s.send(cmd("ping"))
        got: list = []
        cursor = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(e.kind == "turn_ended" for e in got):
            b = s.read(cursor)
            cursor = b.next_cursor
            got += b.events
        assert [e.kind for e in got] == ["error", "turn_started", "agent_text", "turn_ended"]
        assert got[2].body == "echo ping"
    finally:
        s.close()
    assert proc.poll() is not None
    with pytest.raises(NotSent):
        s.send(cmd())
    with pytest.raises(TransportClosed):
        t.write("x\n")


# ---- containment helpers --------------------------------------------------------------------------------------------------------------
_RULE = re.compile(r"^(Read|Grep|Glob)\((//[^)]*|\*\*/[^)]*)\)$")


def test_every_deny_rule_is_the_double_slash_or_a_documented_glob():
    paths = [*default_deny_paths("/Users/someone"), "/Users/someone/exports/keys.pem"]
    doc = json.loads(room_settings_json("/Users/someone/room", paths))
    deny = doc["permissions"]["deny"]
    assert deny and all(_RULE.match(r) for r in deny), [r for r in deny if not _RULE.match(r)]
    for r in deny:
        assert r.startswith(("Read(//", "Grep(//", "Glob(//", "Read(**/", "Grep(**/", "Glob(**/"))
        assert not re.match(r"^(Read|Grep|Glob)\(/[^/]", r)       # a single leading slash is relative: never
    assert doc["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    for tool in ("Read", "Grep", "Glob"):
        assert f"{tool}(//Users/someone/.ssh/**)" in deny and f"{tool}(//Users/someone/.ssh)" in deny
        assert f"{tool}(**/.env*)" in deny
        assert f"{tool}(//Users/someone/exports/keys.pem/**)" in deny
    for needle in (".ssh", ".aws", ".gnupg", ".config/gcloud", ".env", "keychain"):
        assert any(needle in r for r in deny)
    assert len(deny) == len(set(deny))


def test_a_relative_deny_path_is_refused_not_silently_ignored():
    for bad in ("~/.ssh", ".ssh", "secrets/**", "/"):
        with pytest.raises(ValueError):
            room_settings_json("/Users/someone/room", [bad])


def test_a_workdir_inside_a_deny_path_is_refused():
    with pytest.raises(ValueError):
        room_settings_json("/Users/someone/.ssh/room", ["/Users/someone/.ssh"])
    with pytest.raises(ValueError):
        room_settings_json("room", ["/Users/someone/.ssh"])
    room_settings_json("/Users/someone/.sshx/room", ["/Users/someone/.ssh"])       # a sibling name is not inside


def test_default_deny_paths_need_an_absolute_home():
    with pytest.raises(ValueError):
        default_deny_paths("home")
    assert default_deny_paths(Path("/h"))[0] == "/h/.ssh"


def test_room_argv_is_the_section_2_4_argv():
    a = room_argv(mcp_config="/r/mcp.json", settings="/r/settings.json", model="sonnet", session_id=SID)
    assert a[:7] == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]

    def val(flag: str) -> str:
        return a[a.index(flag) + 1]
    assert val("--model") == "sonnet"
    assert val("--tools") == "Read,Grep,Glob,WebSearch"
    assert val("--allowedTools") == "Read,Grep,Glob,WebSearch,mcp__levain_room__propose_drop"
    assert val("--disallowedTools") == "Bash,Edit,Write,NotebookEdit,WebFetch"
    assert "--strict-mcp-config" in a and val("--mcp-config") == "/r/mcp.json" and val("--settings") == "/r/settings.json"
    assert val("--permission-mode") == "dontAsk" and val("--session-id") == SID
    assert "--dangerously-skip-permissions" not in a and "--bare" not in a and "--resume" not in a
    r = room_argv(mcp_config="/r/mcp.json", settings="/r/settings.json", resume=SID)
    assert val_of(r, "--resume") == SID and "--session-id" not in r and "--model" not in r


def val_of(argv, flag):
    return argv[argv.index(flag) + 1]


def test_room_argv_refuses_what_it_cannot_make_safe():
    ok = dict(mcp_config="/r/m.json", settings="/r/s.json")
    with pytest.raises(ValueError):
        room_argv(**ok)
    with pytest.raises(ValueError):
        room_argv(**ok, session_id=SID, resume=SID)
    with pytest.raises(ValueError):
        room_argv(mcp_config="m.json", settings="/r/s.json", session_id=SID)
    with pytest.raises(ValueError):
        room_argv(mcp_config="/r/m.json", settings="s.json", session_id=SID)


def test_open_probe_is_live_only_for_exactly_the_rooms_surface():
    s, t = make()
    t.feed(init())
    p = open_probe(s.liveness_evidence().raw)
    assert p.live and "dontAsk" in p.reason
    assert open_probe(init()).live


@pytest.mark.parametrize("make_init, why", [
    (lambda: init(tools=["Glob", "Grep", "Read", "WebSearch", cs.ROOM_MCP_TOOL, "Bash"]), "extra"),
    (lambda: init(tools=["Glob", "Grep", "Read", "WebSearch"]), "missing"),
    (lambda: init(tools=["Glob", "Grep", "Read", "WebSearch", cs.ROOM_MCP_TOOL, "Read"]), "extra"),
    (lambda: init(tools=["Glob", "Grep", "Read", "WebSearch", "mcp__other__thing"]), "extra"),
    (lambda: init(servers=[]), "exactly one"),
    (lambda: init(servers=[{"name": "levain_room", "status": "connected"}, {"name": "x", "status": "connected"}]),
     "exactly one"),
    (lambda: init(servers=[{"name": "other", "status": "connected"}]), "levain_room"),
    (lambda: init(servers=[{"name": "levain_room", "status": "pending"}]), "connected"),
    (lambda: init(mode="default"), "dontAsk"),
    (lambda: init(mode="bypassPermissions"), "dontAsk"),
    (lambda: {k: v for k, v in init().items() if k != "permissionMode"}, "dontAsk"),
    (lambda: {k: v for k, v in init().items() if k != "tools"}, "tool list"),
])
def test_open_probe_refuses_anything_else_with_the_reason(make_init, why):
    p = open_probe(make_init())
    assert not p.live and why in p.reason


def test_open_probe_with_no_init():
    assert not open_probe(None).live and "no init" in open_probe(None).reason
    assert not open_probe("tools").live


DECOY = "/tmp/rmc/.ssh/id_decoy"


def _canary(*, attempt=True, errored=True, listed=True, accept=False, path=DECOY):
    f = []
    if attempt:
        f.append(tool_use("toolu_c", path))
    if errored or accept:
        f.append(tool_result("toolu_c", "denied" if errored else "DECOY", error=errored,
                             decision={"decision": "accept", "source": "x", "reason_type": "rule"} if accept else None))
    elif attempt:
        bare = tool_result("toolu_c", "DECOY")
        del bare["tool_result_meta"]                      # content came back, no decision recorded
        f.append(bare)
    f.append(result(denials=[{"tool_name": "Read", "tool_use_id": "toolu_c", "tool_input": {"file_path": path}}]
                    if listed else []))
    return f


def test_canary_denied_needs_the_error_result_and_the_denials_entry():
    v = canary_denied(_canary(), DECOY)
    assert v.denied and "permission_denials" in v.reason


@pytest.mark.parametrize("kw, why", [
    ({"attempt": False, "errored": False, "listed": False}, "never attempted"),
    ({"errored": False}, "no is_error"),
    ({"listed": False}, "permission_denials"),
    ({"path": "/tmp/other"}, "never attempted"),
    ({"accept": True}, "accepted"),
])
def test_canary_not_denied_when_any_half_is_missing(kw, why):
    v = canary_denied(_canary(**kw), DECOY)
    assert not v.denied and why in v.reason


def test_canary_reads_the_sessions_own_recent_frames():
    s, t = make()
    s.send(cmd("read the decoy"))
    t.feed(init(), *_canary(), text("it was denied"))
    s.read(None)
    assert canary_denied(s.recent_frames(), DECOY).denied


# ---- L3 r1 (RMCF): each test below was written from a reproduced failure on 14016513 -------------------------------------
def _bare_read(tid: str, body: str = "DECOY") -> dict:
    f = tool_result(tid, body)
    del f["tool_result_meta"]                             # content came back, no decision recorded
    return f


def test_canary_judges_only_the_last_turn_not_an_earlier_denied_one():
    # codex 4: turn 1 denied the canary; turn 2's canary returned the decoy's content with no decision record
    frames = [*_canary(), tool_use("toolu_d", DECOY), _bare_read("toolu_d"), result()]
    v = canary_denied(frames, DECOY)
    assert not v.denied, v.reason


def test_canary_needs_one_attempt_carrying_both_proofs():
    # codex 4: attempt a errored but is not listed; attempt b is listed but returned content
    frames = [tool_use("toolu_a", DECOY), tool_result("toolu_a", "denied", error=True),
              tool_use("toolu_b", DECOY), _bare_read("toolu_b"),
              result(denials=[{"tool_name": "Read", "tool_use_id": "toolu_b", "tool_input": {"file_path": DECOY}}])]
    v = canary_denied(frames, DECOY)
    assert not v.denied, v.reason


def test_canary_any_attempt_that_returned_content_is_not_denied():
    frames = [tool_use("toolu_a", DECOY), tool_result("toolu_a", "denied", error=True),
              tool_use("toolu_b", DECOY), _bare_read("toolu_b"),
              result(denials=[{"tool_name": "Read", "tool_use_id": "toolu_a", "tool_input": {"file_path": DECOY}}])]
    assert not canary_denied(frames, DECOY).denied


def test_canary_attempted_after_the_last_result_is_not_yet_judged():
    frames = [*_canary(), tool_use("toolu_e", DECOY)]
    v = canary_denied(frames, DECOY)
    assert not v.denied and "not ended" in v.reason


def test_canary_tolerates_wrong_container_types():
    frames = [tool_use("toolu_c", DECOY), {**tool_result("toolu_c", "x", error=True), "tool_result_meta": 1},
              {**result(), "permission_denials": 5}]
    assert not canary_denied(frames, DECOY).denied


def test_answer_writes_the_services_reply_verbatim_once():
    # codex 5: the one door writes the desk's durable reply
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Bash", input={"command": "ls"}))
    s.read(None)
    reply = {"behavior": "deny", "message": "the room runs nothing", "interrupt": False}
    r = s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", reply))
    assert r.outcome == "confirmed"
    assert t.written == [{"type": "control_response", "response": {"subtype": "success", "request_id": "req-A",
                                                                    "response": reply}}]
    assert s.answer(ApprovalAnswer("op-2", "ak-1", "req-A", reply)).outcome == "no_such_request"
    assert len(t.written) == 1
    assert s.denials() == (("req-A", "can_use_tool", "Bash"),)


def test_answer_refuses_a_non_approval_and_an_unknown_key():
    s, t = make()
    t.feed(ctl_request("req-B", "mcp_message"))
    s.read(None)
    with pytest.raises(ValueError):
        s.answer(ApprovalAnswer("op-1", "ak-1", "req-B", {"behavior": "allow"}))
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "nope", {})).outcome == "no_such_request"
    assert t.written == []
    assert s.reject_control("req-B", "no").outcome == "confirmed"      # still answerable through its own door


def test_answer_write_failure_is_ambiguous_and_never_written_twice():
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Read", input={}))
    s.read(None)
    t.refuse = BrokenPipeError()
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "ambiguous"
    t.refuse = None
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "ambiguous"
    assert t.written == []
    s2, tt2 = make()
    tt2.feed(ctl_request("req-A", "can_use_tool", tool_name="Read", input={}))
    s2.read(None)
    tt2.refuse = TransportClosed("gone")
    assert s2.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "rejected"
    tt2.refuse = None
    assert s2.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "confirmed"


def test_answer_with_a_reply_json_cannot_hold_leaves_it_answerable():
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Read", input={}))
    s.read(None)
    with pytest.raises((TypeError, ValueError)):
        s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": object()}))
    assert t.written == []
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "confirmed"


def test_reject_control_refuses_an_approval():
    # codex 5: no approval, a deny included, leaves through the non-approval door
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Bash", input={"command": "ls"}))
    s.read(None)
    with pytest.raises(ValueError, match="one door"):
        s.reject_control("req-A", "no")
    assert t.written == []
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "confirmed"


def test_an_identical_replayed_control_request_is_suppressed():
    # codex 6: the replay emitted a second event under the same key; record_event rolls the batch back on that
    s, t = make()
    t.feed(ctl_request("r", "mcp_message"), ctl_request("r", "mcp_message"))
    evs = list(s.read(None).events)
    assert [e.key for e in evs] == ["error:control:r"]
    assert s.reject_control("r", "no").outcome == "confirmed"
    t.feed(ctl_request("r", "mcp_message"))                     # a replay after the answer
    assert s.read(s.position()).events == ()
    with pytest.raises(ValueError):
        s.reject_control("r", "no")                             # one reply per request
    assert len(t.written) == 1


def test_a_replay_with_a_changed_request_is_a_uniquely_keyed_protocol_error():
    s, t = make()
    # two DIFFERENT alterations (an identical altered retransmit is one error since L3 r3: see the RMCF3 test)
    t.feed(ctl_request("r", "mcp_message"), ctl_request("r", "can_use_tool", tool_name="Bash", input={}),
           ctl_request("r", "can_use_tool", tool_name="Bash", input={"command": "ls"}))
    evs = list(s.read(None).events)
    keys = [e.key for e in evs]
    assert keys[0] == "error:control:r" and len(keys) == 3 and len(set(keys)) == 3
    assert all(e.kind == "error" for e in evs) and "approval_request" not in {e.kind for e in evs}
    assert all(e.control_subtype == "can_use_tool" and "changed" in e.body for e in evs[1:])
    s.reject_control("r", "no")                                  # the original pending request is preserved
    assert t.written[-1] == driver._control_error("r", "no")


def test_a_changed_approval_input_under_the_same_id_is_not_a_new_approval():
    s, t = make()
    t.feed(ctl_request("r", "can_use_tool", tool_name="Read", input={"file_path": "/a"}),
           ctl_request("r", "can_use_tool", tool_name="Bash", input={"command": "rm -rf /"}))
    evs = list(s.read(None).events)
    assert [e.kind for e in evs] == ["approval_request", "error"]
    assert evs[0].tool_name == "Read" and len({e.key for e in evs}) == 2


def test_a_repeated_cancel_emits_one_event():
    s, t = make()
    t.feed(ctl_request("r", "can_use_tool", tool_name="Read", input={}),
           {"type": "control_cancel_request", "request_id": "r"},
           {"type": "control_cancel_request", "request_id": "r"})
    evs = list(s.read(None).events)
    assert [e.key for e in evs] == ["approval_request:r", "error:cancel:r"]


def test_identical_unparseable_lines_get_distinct_keys():
    s, t = make()
    t.feed("garbage\n", "garbage\n")
    evs = list(s.read(None).events)
    assert len(evs) == 2 and len({e.key for e in evs}) == 2


def test_a_late_ack_never_confirms_a_later_interrupt():
    # codex 8: reproduced on 14016513 (the second call confirmed without its own response)
    now = [0.0]

    def clock() -> float:
        now[0] += 0.5
        return now[0]
    s, t = make(interrupt_wait=1.0, clock=clock)
    assert s.interrupt(InterruptCommand("op-a", None)).outcome == "ambiguous"
    first = t.written[-1]["request_id"]
    t.feed(ctl_response(first))                                    # the late ack
    assert s.interrupt(InterruptCommand("op-a", None)).outcome == "ambiguous"
    second = t.written[-1]["request_id"]
    assert second != first


def test_interrupt_ids_are_unique_per_write_and_never_collide_by_truncation():
    s, t = make(interrupt_wait=0.0)
    a, b = "x" * 200 + "1", "x" * 200 + "2"
    s.interrupt(InterruptCommand(a, None))
    s.interrupt(InterruptCommand(b, None))
    s.interrupt(InterruptCommand(a, None))
    ids = [w["request_id"] for w in t.written]
    assert len(set(ids)) == 3 and all(len(i) <= 128 and a[:20] not in i for i in ids)


def test_an_unknown_control_response_is_discarded():
    s, t = make(interrupt_wait=1.0)
    t.feed(ctl_response("int-made-up"))
    s.read(None)
    t.on_write = lambda line: t.feed(ctl_response(line["request_id"]))
    assert s.interrupt(InterruptCommand("op-1", None)).outcome == "confirmed"


def _user_meta(meta) -> dict:
    f = tool_result("toolu_m", "body")
    f["tool_result_meta"] = meta
    return f


@pytest.mark.parametrize("frame", [
    _user_meta(1),
    _user_meta({"id": "toolu_m"}),
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "a\ud800b"}]},
     "uuid": "u-sur"},
    {"type": "assistant", "message": "not a mapping", "uuid": "u-msg"},
    {"type": "assistant", "message": {"content": 7}, "uuid": "u-content"},
    {**result(), "permission_denials": 5},
    {**init(), "tools": 5},
    {**init(), "mcp_servers": "levain_room"},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_s",
                                              "content": "x\udfffy"}]}, "uuid": "u-sur2"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_q", "name": "Read",
                                                   "input": {"file_path": "/a\ud801"}}]}, "uuid": "u-sur3"},
])
def test_a_malformed_valid_json_frame_is_a_digest_only_unparseable_event(frame):
    # codex 9: tool_result_meta 1 raised TypeError and a lone surrogate raised UnicodeEncodeError out of read()
    s, t = make()
    t.feed(frame, text("after"))
    evs = list(s.read(None).events)
    bad = [e for e in evs if e.vendor_id.startswith("unparseable:")]
    assert len(bad) == 1 and bad[0].kind == "error" and bad[0].raw_sha256.startswith("sha256:")
    assert "body" not in bad[0].body and "\ud800" not in bad[0].body
    assert [e.body for e in evs if e.kind == "agent_text"] == ["after"]          # the next frame still lands
    for e in evs:
        e.body.encode("utf-8")
    s.meta()                                                                       # never raises either
    assert all(f is not frame for f in s.recent_frames())


def test_a_body_cap_never_raises_on_a_lone_surrogate():
    assert cs._cap("a\ud800b").encode("utf-8")
    assert cs._cap("\ud800" * (EVENT_BODY_MAX * 2)).encode("utf-8")


class _StubbornProc:
    """A Popen stand-in that ignores terminate; records what close() asked of it."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dead = False
        self.stdin = self
        self.stdout = iter(())

    def close(self) -> None:
        self.calls.append("stdin.close")

    def poll(self):
        return 0 if self.dead and self.calls[-1].startswith("wait") else None

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        if not self.dead:
            import subprocess
            raise subprocess.TimeoutExpired("x", timeout)
        return -9

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")
        self.dead = True


def test_close_waits_with_a_bound_after_kill():
    # codex 10: close() killed and never reaped
    p = _StubbornProc()
    PipeTransport(p).close()
    # no write in flight: EOF first and the child's own exit, then terminate and kill, each bounded
    assert p.calls == ["stdin.close", "wait(1.0)", "terminate", "wait(3.0)", "kill", "wait(3.0)"]


def test_room_argv_loads_only_the_user_setting_source():
    a = room_argv(mcp_config="/r/mcp.json", settings="/r/settings.json", session_id=SID)
    assert val_of(a, "--setting-sources") == "user" and a.count("--setting-sources") == 1
    assert "--restricted" not in a


# ---- L3 r2 fixes (RMCF2) -------------------------------------------------------------------------------------------------------
def _deny(workdir, paths) -> list[str]:
    return json.loads(room_settings_json(workdir, paths))["permissions"]["deny"]


def test_a_symlinked_deny_path_is_compared_after_realpath(tmp_path):
    # FIX 1: a deny path and a workdir that name one place under two spellings
    real = tmp_path / "real"
    (real / "room").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(real / "room"), [str(link)])
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(link / "room"), [str(real)])
    deny = _deny(str(tmp_path / "elsewhere"), [str(link)])
    target = os.path.realpath(str(real))
    assert f"Read(/{target})" in deny and f"Read(/{target}/**)" in deny          # the target is denied too
    assert f"Read(/{link})" in deny


@pytest.mark.skipif(os.path.realpath("/tmp") == "/tmp", reason="/tmp is not a symlink here (macOS spells it /private/tmp)")
def test_a_tmp_deny_path_catches_a_workdir_spelled_under_private_tmp():
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json("/private/tmp/rmcf2-x/room", ["/tmp/rmcf2-x"])
    deny = _deny("/Users/someone/room", ["/tmp/rmcf2-x"])
    assert "Read(//private/tmp/rmcf2-x/**)" in deny and "Read(//tmp/rmcf2-x/**)" in deny


def test_a_dotdot_deny_path_is_normalised_before_it_is_compared_or_spelled():
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json("/Users/someone/.ssh/room", ["/Users/someone/x/../.ssh"])
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json("/Users/someone/y/../.ssh/room", ["/Users/someone/.ssh"])
    deny = _deny("/Users/someone/room", ["/Users/someone/x/../.ssh", "//Users/someone/.aws/"])
    assert "Read(//Users/someone/.ssh/**)" in deny and not any(".." in r for r in deny)
    assert "Read(//Users/someone/.aws)" in deny and not any(r.startswith("Read(///") for r in deny)


@pytest.mark.parametrize("bad", ["/Users/someone/a)b", "/Users/someone/a) Read(//x", "**/.env)x",
                                 "/Users/someone/a\nb", "/Users/someone/a\x00b", "**/.env\x7f"])
def test_a_malformed_rule_text_is_refused(bad):
    with pytest.raises(ValueError, match=r"\)|control"):
        room_settings_json("/Users/someone/room", [bad])


def test_a_double_slash_root_is_still_the_root():
    with pytest.raises(ValueError):
        room_settings_json("/Users/someone/room", ["//"])


def _acks(t):
    t.on_write = lambda line: t.feed(ctl_response(line["request_id"])) if line["type"] == "control_request" else None


def test_interrupt_accepts_the_turn_send_predicted_before_its_init():
    # FIX 2: send() before init names t1-1; that id must be interruptible while no turn is open
    s, t = make(interrupt_wait=1.0)
    _acks(t)
    assert s.interrupt(InterruptCommand("op-0", "t1-1")).outcome == "rejected"     # no line awaits: never existed
    tid = s.send(cmd()).vendor_turn_id
    assert tid == "t1-1"
    n = len(t.written)
    assert s.interrupt(InterruptCommand("op-1", "t1-2")).outcome == "rejected" and len(t.written) == n
    assert s.interrupt(InterruptCommand("op-2", tid)).outcome == "confirmed"
    assert t.written[-1]["request"] == {"subtype": "interrupt"}


_BAD_UTF8_CHILD = r"""
import sys
out = sys.stdout.buffer
out.write(b'{"type":"assistant","message":{"content":[{"type":"text","text":"before"}]},"uuid":"u-1"}\n')
out.write(b'\xff\xfe not utf-8\n')
out.write(b'{"type":"assistant","message":{"content":[{"type":"text","text":"after"}]},"uuid":"u-2"}\n')
out.flush()
sys.stdin.readline()
"""


def _drain(s, until, limit=10.0):
    got, deadline = [], time.monotonic() + limit
    while time.monotonic() < deadline and not until(got):
        got += s.read(None).events[len(got):]
    return got


def test_a_bad_byte_costs_one_line_and_the_end_of_output_is_reported_once():
    # FIX 3: invalid UTF-8 on stdout must not silently end the session's output while the child lives
    import subprocess
    proc = subprocess.Popen([sys.executable, "-I", "-c", _BAD_UTF8_CHILD], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    t = PipeTransport(proc)
    s = ClaudeSession(t, session_id="s", read_wait=0.2)
    try:
        got = _drain(s, lambda g: any(e.body == "after" for e in g))
        assert [e.body for e in got if e.kind == "agent_text"] == ["before", "after"]
        assert any(e.vendor_id.startswith("unparseable:") for e in got)
        assert proc.poll() is None and not any(e.vendor_id.startswith("transport:") for e in got)
        proc.stdin.write("\n")
        proc.stdin.flush()
        got = _drain(s, lambda g: any(e.vendor_id.startswith("transport:") for e in g))
        ends = [e for e in got if e.vendor_id.startswith("transport:")]
        assert len(ends) == 1 and ends[0].kind == "error" and "output ended" in ends[0].body
        s.read(None)
        assert len([e for e in s.read(None).events if e.vendor_id.startswith("transport:")]) == 1
        t0 = time.monotonic()
        assert t.poll(5.0) == [] and time.monotonic() - t0 < 1.0    # a poll after the end never waits
    finally:
        s.close()


class _DeadPipeProc:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.stdin = self
        self.stdout = iter(())

    def write(self, data):
        raise self.exc

    def flush(self):
        raise self.exc

    def close(self):
        pass

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"), ValueError("I/O operation on closed file.")])
def test_a_dead_stdin_is_transport_closed_and_the_line_is_unsent(exc):
    # FIX 4
    t = PipeTransport(_DeadPipeProc(exc))
    with pytest.raises(TransportClosed):
        t.write("x\n")
    s = ClaudeSession(t, session_id="s")
    with pytest.raises(NotSent):
        s.send(cmd())
    assert s.pending_lines() == 0 and s.turn_lines("t1-1") == 0
    t._closed = True


def test_a_huge_tool_input_is_capped_on_tool_calls_and_approvals():
    # FIX 5
    big = {"file_path": "/a", "content": "x" * (1024 * 1024)}
    s, t = make()
    call = tool_use("toolu_big", "/a")
    call["message"]["content"][0]["input"] = big
    t.feed(init(), call, ctl_request("req-big", "can_use_tool", tool_name="Write", input=big))
    evs = {e.kind: e for e in s.read(None).events}
    full = json.dumps(big, sort_keys=True, ensure_ascii=False).encode("utf-8")
    for kind in ("tool_call", "approval_request"):
        ti = evs[kind].tool_input
        assert ti["levain_truncated"] is True and ti["bytes"] == len(full)
        assert ti["sha256"] == "sha256:" + __import__("hashlib").sha256(full).hexdigest()
        assert len(json.dumps(ti).encode("utf-8")) < 2 * EVENT_BODY_MAX
        assert full.decode("utf-8").startswith(ti["head"]) and ti["head"]


def test_a_deeply_nested_frame_is_a_digest_only_event_not_a_crash():
    # FIX 6
    depth = 100000
    s, t = make()
    t.feed('{"type":"assistant","x":' + "[" * depth + "]" * depth + "}\n", text("after"))
    evs = list(s.read(None).events)
    assert [e.body for e in evs if e.kind == "agent_text"] == ["after"]
    assert any(e.vendor_id.startswith("unparseable:") for e in evs)
    nested: list = []
    for _ in range(depth):
        nested = [nested]
    assert cs._malformed({"type": "assistant", "x": nested}) == "the frame is nested too deeply"


class _SlowTransport(FakeTransport):
    """A poll honours its wait (blocks), as PipeTransport's does."""

    def poll(self, wait: float) -> list[str]:
        if not self.lines and wait > 0:
            time.sleep(wait)
        return super().poll(wait)


def test_an_interrupt_never_waits_behind_a_long_read():
    # FIX 7
    import threading
    t = _SlowTransport()
    s = ClaudeSession(t, session_id=SID, read_wait=3.0, interrupt_wait=2.0)
    _acks(t)
    reader = threading.Thread(target=s.read, args=(None,), daemon=True)
    reader.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    assert s.interrupt(InterruptCommand("op-1", None)).outcome == "confirmed"
    assert time.monotonic() - t0 < 1.0
    reader.join(5)


def test_a_cancel_clears_an_ambiguous_reply_and_nothing_is_written_twice():
    # FIX 8 (L3 r2 finding 5): the ambiguous marker leaves with the cancelled request
    s, t = make()
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Bash", input={"command": "ls"}))
    s.read(None)
    t.refuse = BrokenPipeError()
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "ambiguous"
    t.refuse = None
    t.feed({"type": "control_cancel_request", "request_id": "req-A"})
    s.read(None)
    assert "req-A" not in s._replying and "req-A" not in s._inbound
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "no_such_request"
    t.feed(ctl_request("req-A", "can_use_tool", tool_name="Bash", input={"command": "ls"}))   # a replay
    s.read(None)
    assert s.answer(ApprovalAnswer("op-1", "ak-1", "req-A", {"behavior": "deny"})).outcome == "no_such_request"
    assert t.written == []


# ---- L3 r3 fixes (RMCF3) -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["a)b", "a) Read(x", "a\x01b"])
def test_a_symlinked_deny_path_whose_target_cannot_be_a_rule_is_refused(tmp_path, name):
    # FIX 1: every spelled form is a rule, so every spelled form is validated, not only the path as given
    real = tmp_path / name
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match=r"\)|control"):
        room_settings_json(str(tmp_path / "elsewhere"), [str(link)])


class _EndedTransport(FakeTransport):
    """A child whose stdout has ended: every poll is empty and returns at once."""

    eof = True
    returncode = None

    def __init__(self) -> None:
        super().__init__()
        self.polls = 0

    def poll(self, wait: float) -> list[str]:
        self.polls += 1
        return super().poll(wait)


def test_an_interrupt_after_the_output_ended_is_ambiguous_at_once_and_never_spins():
    # FIX 2: with stdout ended no control_response can arrive; waiting out interrupt_wait spun at full CPU
    t = _EndedTransport()
    s = ClaudeSession(t, session_id=SID, interrupt_wait=3.0)
    t0 = time.monotonic()
    r = s.interrupt(InterruptCommand("op-1", None))
    assert r.outcome == "ambiguous" and "ended" in r.detail
    assert time.monotonic() - t0 < 0.5 and t.polls < 10
    assert t.written[-1]["request"] == {"subtype": "interrupt"}


def test_a_read_issued_while_an_interrupt_waits_returns_within_a_slice():
    # FIX 3: the interrupt's wait no longer holds the io lock
    import threading
    s, t = make(interrupt_wait=3.0)
    out: list = []
    waiter = threading.Thread(target=lambda: out.append(s.interrupt(InterruptCommand("op-1", None))), daemon=True)
    waiter.start()
    deadline = time.monotonic() + 2.0
    while not t.written and time.monotonic() < deadline:
        time.sleep(0.01)
    assert t.written, "the interrupt was never written"
    t0 = time.monotonic()
    s.read(None)
    assert time.monotonic() - t0 < 0.25
    t.feed(ctl_response(t.written[-1]["request_id"]))
    waiter.join(3.0)
    assert out and out[0].outcome == "confirmed"


def test_a_concurrent_second_interrupt_never_steals_the_first_ones_ack():
    # FIX 3's guard: interrupts stay serialised once the io lock is released around the wait
    import threading
    s, t = make(interrupt_wait=3.0)
    out: list = []
    first = threading.Thread(target=lambda: out.append(("a", s.interrupt(InterruptCommand("op-a", None)))),
                             daemon=True)
    first.start()
    deadline = time.monotonic() + 2.0
    while not t.written and time.monotonic() < deadline:
        time.sleep(0.01)
    second = threading.Thread(target=lambda: out.append(("b", s.interrupt(InterruptCommand("op-b", None)))),
                              daemon=True)
    second.start()
    time.sleep(0.1)
    assert len(t.written) == 1                                   # the second waits its turn, writing nothing yet
    t.feed(ctl_response(t.written[0]["request_id"]))
    first.join(3.0)
    deadline = time.monotonic() + 2.0
    while len(t.written) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    t.feed(ctl_response(t.written[1]["request_id"]))
    second.join(3.0)
    assert dict(out) and dict(out)["a"].outcome == "confirmed" and dict(out)["b"].outcome == "confirmed"


def test_a_retransmitted_altered_request_is_one_protocol_error():
    # FIX 4: the same altered copy, sent again, is suppressed; a different alteration is its own error
    s, t = make()
    t.feed(ctl_request("r", "mcp_message"),
           ctl_request("r", "can_use_tool", tool_name="Bash", input={}),
           ctl_request("r", "can_use_tool", tool_name="Bash", input={}),
           ctl_request("r", "can_use_tool", tool_name="Bash", input={}),
           ctl_request("r", "can_use_tool", tool_name="Read", input={}))
    evs = list(s.read(None).events)
    changed = [e for e in evs if e.vendor_id.startswith("control_changed:")]
    assert len(evs) == 3 and len(changed) == 2 and len({e.key for e in evs}) == 3


# ---- 1105-49: the room's own read-guard hook entry ------------------------------------------------------------------
def test_the_rooms_settings_carry_the_vendors_block_and_the_hook_entry_together_and_the_argv_gains_no_prompt():
    from levain.desk import room_readguard
    from levain.desk.claude_session import ROOM_READ_REFUSAL, room_hook_command
    cmd = room_hook_command("/usr/bin/python3", "/run/room_readguard.py")
    doc = json.loads(room_settings_json("/Users/someone/room", default_deny_paths("/Users/someone"), cmd))
    assert doc["permissions"]["blockReadsOutsideWorkingDirectories"] is True and doc["permissions"]["deny"]
    assert doc["hooks"] == {"PreToolUse": [{"matcher": "Read|Grep|Glob", "hooks": [
        {"type": "command", "command": "/usr/bin/python3 -I /run/room_readguard.py", "timeout": 10}]}]}
    assert ROOM_READ_REFUSAL is room_readguard.ROOM_READ_REFUSAL
    for leak in ("/add-dir", "blockReadsOutsideWorkingDirectories", "permissions.", "/Users"):
        assert leak not in ROOM_READ_REFUSAL
    argv = room_argv(mcp_config="/m.json", settings="/s.json", session_id="sid")
    assert "--append-system-prompt" not in argv and "--setting-sources" in argv        # option B is not built


def test_a_hook_command_quotes_a_path_with_a_space_and_refuses_a_relative_one():
    from levain.desk.claude_session import room_hook_command
    assert room_hook_command("/opt/my py/python", "/run dir/g.py") == "'/opt/my py/python' -I '/run dir/g.py'"
    for py, script in (("python3", "/g.py"), ("/usr/bin/python3", "g.py")):
        with pytest.raises(ValueError):
            room_hook_command(py, script)


# ---- 1106-1 aliasc: a workdir inside a deny path, either one spelled in another letter case (casesweep rows 5, 5b) ----------------
def _case_alias_of(p):
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same file (a
    case-insensitive one: macOS APFS by default); else the test is skipped. On a case-sensitive volume (Linux CI) no
    alias exists, so the macOS test below can never red there; the injected-fold test after it runs everywhere."""
    full = str(p).swapcase()
    if full == str(p) or not os.path.exists(full) or not os.path.samefile(full, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return full


def test_a_workdir_inside_a_deny_path_is_refused_when_either_is_spelled_in_another_case(tmp_path):
    """casesweep rows 5/5b: ``_inside`` was a string prefix, so an alias-spelled workdir (or deny path) skipped the
    "workdir INSIDE a deny path" refusal and the room opened blind (its own cwd denied). A sibling that merely shares a
    prefix, and a canonical input, answer as before."""
    deny = tmp_path / "ssh"
    (deny / "room").mkdir(parents=True)
    (tmp_path / "sshx" / "room").mkdir(parents=True)
    alias_deny, alias_room = _case_alias_of(deny), _case_alias_of(deny / "room")
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(deny / "room"), [str(deny)])                       # canonical: as before
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(alias_room, [str(deny)])                               # row 5: the workdir in alias spelling
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(deny / "room"), [alias_deny])                      # row 5b: the deny path in alias spelling
    for ok_room in (str(tmp_path / "sshx" / "room"), _case_alias_of(tmp_path / "sshx" / "room")):
        room_settings_json(ok_room, [str(deny)])                                  # a sibling is not inside, in either spelling
        room_settings_json(ok_room, [alias_deny])


def test_a_workdir_inside_a_deny_path_is_refused_through_an_injected_case_fold(tmp_path, monkeypatch):
    """The same logic where no alias exists (a Linux CI run): ``pathid._fs_spelling`` is replaced by a case-insensitive
    walk over the real tree, and the deny path is spelled in a case that only the fold maps onto the real directory."""
    from levain import pathid
    from tests.test_pathid import _case_insensitive_spelling
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    base = tmp_path.resolve()
    (base / "ssh" / "room").mkdir(parents=True)
    (base / "sshx" / "room").mkdir(parents=True)
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(base / "ssh" / "room"), [str(base / "SSH")])
    with pytest.raises(ValueError, match="inside deny path"):
        room_settings_json(str(base / "SSH" / "ROOM"), [str(base / "ssh")])
    room_settings_json(str(base / "sshx" / "room"), [str(base / "SSH")])
    room_settings_json(str(base / "SSHX" / "room"), [str(base / "ssh")])
