"""The host profile's tripwire context and mode check (levain 22, key 1112-36, lane ROOM-P1B).

Measured (the ROOM-D0 design page, section 0): under ``bypassPermissions`` every accepted call carries
``{accept, source config, reason_type mode}``, and no PreToolUse event reaches the stream. The confined room's context
latches that shape as a ``deficit``. A host that runs the room open sets ``TripwireContext.open_tools``: a rule or mode
accept for ANY tool, from the argv's own config or the operator's user allow list, is the normal path. Everything that
is not that stays a latch: an accept from the session's own source, a hook accept, an ask, a control request that sets
the mode, and a turn boundary with a call open. Without the flag nothing changes (the existing tripwire suites are the
proof; the pins here repeat the one shape the flag exists for).

The room's expected permission mode is the service's (``ChatService(room_permission_mode=...)``): the confined room
keeps ``dontAsk``, the host room expects ``bypassPermissions``, and each latches the other's mode."""
from __future__ import annotations

import pytest

from levain.desk import chat
from levain.desk.chat import ROOM_PERMISSION_MODE, ChatService, _room_mode_trip
from levain.desk.chat_tripwires import TripwireContext, TripwireWindow
from levain.desk.driver import ScriptedSession, SessionMeta, event

from tests.test_chat_room_send import _live, _open_room_thread, _room_launch, _thread, cs, store  # noqa: F401
from tests.test_desk_chat_liveness import kinds

BYPASS = "bypassPermissions"
CONFIG_MODE = {"decision": "accept", "source": "config", "reason_type": "mode"}      # D0 probe B, every call
USER_MODE = {"decision": "accept", "source": "user_permanent", "reason_type": "mode"}
CONFIG_RULE = {"decision": "accept", "source": "config", "reason_type": "rule"}
USER_RULE = {"decision": "accept", "source": "user_permanent", "reason_type": "rule"}
HOOK_DENY = {"decision": "reject", "source": "hook", "reason_type": "hook"}          # D0 probe: the turn guard's refusal


def _ctx(**kw):
    def expected(tool):
        raise ValueError(tool)
    return TripwireContext("/tmp", expected, lambda _i, _t: False, frozenset(), **kw)


def _call_and_result(w, tool, decision, n=1, *, name=True):
    w.observe(event("tool_call", f"c{n}", f"p{n}a", call_id=f"c{n}", tool_name=tool, tool_input={"command": "x"}))
    return w.observe(event("tool_result", f"c{n}", f"p{n}b", call_id=f"c{n}", tool_name=tool if name else None,
                           decision_source=decision))[0]


TOOLS = ["Bash", "Write", "Read", "Edit", "mcp__levain_room__relay_to_desk", "Foo"]


# --- the flag passes the measured shapes, for any tool ----------------------------------------------------------------
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("decision", [CONFIG_MODE, USER_MODE, CONFIG_RULE, USER_RULE])
def test_open_tools_passes_a_rule_or_mode_accept_for_any_tool(tool, decision):
    assert _call_and_result(TripwireWindow(_ctx(open_tools=True)), tool, decision) is None


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("decision", [
    HOOK_DENY,
    {"decision": "reject", "source": "config", "reason_type": "mode"},
    {"decision": "reject", "source": "config", "reason_type": "rule"},
])
def test_open_tools_passes_a_deny_for_any_tool(tool, decision):
    assert _call_and_result(TripwireWindow(_ctx(open_tools=True)), tool, decision) is None


def test_open_tools_passes_a_run_of_calls_with_no_pretooluse_event_in_the_stream():
    w = TripwireWindow(_ctx(open_tools=True))
    assert [_call_and_result(w, t, CONFIG_MODE, n=i) for i, t in enumerate(TOOLS)] == [None] * len(TOOLS)


# --- and latches everything else --------------------------------------------------------------------------------------
LATCHING = [
    {"decision": "accept", "source": "session", "reason_type": "rule"},          # the session's own grant is not config
    {"decision": "accept", "source": "session", "reason_type": "mode"},
    {"decision": "accept", "source": "projectSettings", "reason_type": "rule"},
    {"decision": "accept", "source": "localSettings", "reason_type": "mode"},
    {"decision": "accept", "source": "hook", "reason_type": "hook"},
    {"decision": "accept", "source": "config", "reason_type": "hook"},
    {"decision": "accept", "source": "config", "reason_type": "other"},
    {"decision": "accept", "source": "user_permanent", "reason_type": "other"},
    {"decision": "accept", "source": "user_temporary"},
    {"decision": "accept", "source": ["config"], "reason_type": "mode"},
    {"decision": "accept", "source": "config"},
    {"decision": "ask", "source": "config", "reason_type": "mode"},
    {"decision": "allow", "source": "config", "reason_type": "mode"},
    {"source": "config", "reason_type": "mode"},
    None,
    "accept",
]


@pytest.mark.parametrize("decision", LATCHING)
def test_open_tools_still_latches_every_other_shape(decision):
    """Latched under the decision's own branch (ROOMFIX4): an open room owes no PreToolUse count, so not ``deficit``."""
    trip = _call_and_result(TripwireWindow(_ctx(open_tools=True)), "Bash", decision)
    assert trip is not None and trip.name in ("accepted_without_host", "unexpected_rule")


@pytest.mark.parametrize("flag", [False, 1, "yes", None])
@pytest.mark.parametrize("decision", [CONFIG_MODE, CONFIG_RULE, HOOK_DENY])
def test_only_a_true_flag_opens_the_context(flag, decision):
    trip = _call_and_result(TripwireWindow(_ctx(open_tools=flag)), "Bash", decision)
    assert trip is not None and trip.name == "deficit"


@pytest.mark.parametrize("flag", [False, 1, "yes", None])
@pytest.mark.parametrize("decision, trip_name", [(CONFIG_MODE, "accepted_without_host"), (CONFIG_RULE, "unexpected_rule"),
                                                 (USER_RULE, "unexpected_rule")])
def test_a_context_that_is_not_open_keeps_the_later_branches_for_the_open_shapes(flag, decision, trip_name):
    """With the PreToolUse count met, the count is not what latches: the decision's own branch must, and the flag's
    absence must leave that branch in force."""
    def expected(tool):
        return 1
    w = TripwireWindow(TripwireContext("/tmp", expected, lambda _i, _t: False, frozenset(), open_tools=flag))
    w.observe(event("agent_text", "h1", "p0", hook={"event": "hook_response", "name": "PreToolUse:Bash"}))
    trip = _call_and_result(w, "Bash", decision)
    assert trip is not None and trip.name == trip_name


def test_without_the_flag_the_mode_accept_still_latches_as_a_deficit():
    for w in (TripwireWindow(_ctx()), TripwireWindow(_ctx(open_tools=False))):
        trip = _call_and_result(w, "Bash", CONFIG_MODE)
        assert trip is not None and trip.name == "deficit"


def test_the_default_context_is_closed():
    assert _ctx().open_tools is False


def test_no_context_at_all_still_latches():
    trip = _call_and_result(TripwireWindow(None), "Bash", CONFIG_MODE)
    assert trip is not None and trip.name == "deficit"


def test_open_tools_leaves_a_result_whose_tool_cannot_be_named_latched():
    w = TripwireWindow(_ctx(open_tools=True))
    trip = w.observe(event("tool_result", "orphan", "p1", call_id="orphan", decision_source=CONFIG_MODE))[0]
    assert trip is not None and trip.name == "deficit"


def test_open_tools_still_latches_a_hook_decision_after_a_cancelled_host_request():
    w = TripwireWindow(_ctx(open_tools=True))
    w.observe(event("tool_call", "c1", "p1", call_id="c1", tool_name="Bash", tool_input={}))
    w.observe(event("approval_request", "r1", "p2", call_id="c1", request_key="r1", host=True, tool_name="Bash",
                    control_subtype="can_use_tool"))
    w.observe(event("approval_resolved", "x1", "p3", cancel_of="r1"))
    trip = w.observe(event("tool_result", "c1", "p4", call_id="c1", tool_name="Bash", decision_source=HOOK_DENY))[0]
    assert trip is not None and trip.name == "unexpected_hook"


def test_open_tools_still_latches_a_set_permission_mode_control_request():
    w = TripwireWindow(_ctx(open_tools=True))
    trip, reply = w.observe(event("error", "control:req-9", "p1", body="inbound set_permission_mode",
                                  control_subtype="set_permission_mode"))
    assert trip is not None and trip.name == "unexpected_control" and reply is not None


def test_open_tools_still_latches_a_turn_boundary_with_a_call_open():
    w = TripwireWindow(_ctx(open_tools=True))
    w.observe(event("tool_call", "c1", "p1", call_id="c1", tool_name="Bash", tool_input={}))
    trip = w.boundary(turn=True)
    assert trip is not None and trip.name == "deficit"


def test_open_tools_still_latches_a_read_gap_with_a_call_open():
    w = TripwireWindow(_ctx(open_tools=True))
    w.observe(event("tool_call", "c1", "p1", call_id="c1", tool_name="Bash", tool_input={}))
    trip = w.gap(False)
    assert trip is not None and trip.name == "deficit"


# --- the room's expected permission mode ------------------------------------------------------------------------------
def test_the_default_expected_mode_is_the_confined_rooms():
    assert _room_mode_trip(ROOM_PERMISSION_MODE) is None
    for other in (BYPASS, "default", "plan", "acceptEdits", "", None):
        trip = _room_mode_trip(other)
        assert trip is not None and trip.name == "mode_changed" and repr(ROOM_PERMISSION_MODE) in trip.detail


def test_an_expected_bypass_passes_bypass_and_latches_dontask():
    assert _room_mode_trip(BYPASS, expected=BYPASS) is None
    trip = _room_mode_trip("dontAsk", expected=BYPASS)
    assert trip is not None and trip.name == "mode_changed" and repr(BYPASS) in trip.detail and "'dontAsk'" in trip.detail


@pytest.mark.parametrize("reported", ["default", "plan", "acceptEdits", "bypass", "BYPASSPERMISSIONS", "", None, 0])
def test_an_expected_bypass_latches_anything_else_and_an_unreadable_report(reported):
    trip = _room_mode_trip(reported, expected=BYPASS)
    assert trip is not None and trip.name == "mode_changed"


# --- the service applies the room's mode at both of its sites ---------------------------------------------------------
class ModeSession(ScriptedSession):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def meta(self):
        return SessionMeta("room-session", self.mode, "claude-room-model", "2.1.317", None, ("Read",), ())


def _rig(store, cs, mode, **svc_kw):
    thread = _thread(store)
    _open_room_thread(store, thread)
    launch = _room_launch(store, thread, "room-sid-1")
    svc = ChatService(cs, probe=lambda _l, _e: chat.Verdict(True, "scripted probe: live"), sleep=lambda _s: None,
                      **svc_kw)
    _live(cs, launch)
    session = ModeSession(mode)
    svc.attach(launch, session)
    svc.ingest(launch, session.read(None))
    return svc, session, launch


def _trips(cs, launch):
    return [s for _k, s in kinds(cs, launch) if s.startswith("tripwire:")]


def _mode_line(session, body, n):
    session.show("error", f"mode:m{n}", body=body)


@pytest.mark.parametrize("mode, expected, latches", [
    ("dontAsk", None, False), (BYPASS, None, True),
    (BYPASS, BYPASS, False), ("dontAsk", BYPASS, True),
])
def test_the_services_meta_site_judges_the_mode_against_the_rooms_expected_mode(store, cs, mode, expected, latches):
    kw = {} if expected is None else {"room_permission_mode": expected}
    svc, session, launch = _rig(store, cs, mode, **kw)
    session.show("agent_text", None, body="hello")
    svc.read_next(launch)
    assert _trips(cs, launch) == (["tripwire:mode_changed"] if latches else [])


@pytest.mark.parametrize("line, expected, latches", [
    ("dontAsk", None, False), (BYPASS, None, True),
    (BYPASS, BYPASS, False), ("dontAsk", BYPASS, True),
])
def test_the_services_mode_line_site_judges_the_report_against_the_rooms_expected_mode(store, cs, line, expected,
                                                                                         latches):
    kw = {} if expected is None else {"room_permission_mode": expected}
    svc, session, launch = _rig(store, cs, None, **kw)         # the meta reports none yet: only the line speaks
    _mode_line(session, line, 1)
    svc.read_next(launch)
    assert _trips(cs, launch) == (["tripwire:mode_changed"] if latches else [])


@pytest.mark.parametrize("bad", ["", None, 3, ["bypassPermissions"]])
def test_the_service_refuses_an_expected_mode_it_cannot_compare(store, cs, bad):
    with pytest.raises(ValueError):
        ChatService(cs, room_permission_mode=bad)


def test_a_worker_launch_keeps_the_worker_rule_whatever_the_rooms_mode(store, cs):
    """The room's mode is for room launches only: a worker's mode is still judged against ``default``."""
    from tests.test_desk_chat_liveness import _world
    claude = _world(store, "claude")
    svc = ChatService(cs, probe=lambda _l, _e: chat.Verdict(True, "scripted probe: live"), sleep=lambda _s: None,
                      room_permission_mode=BYPASS)
    _live(cs, claude["launch"])
    session = ModeSession(BYPASS)
    svc.attach(claude["launch"], session)
    svc.ingest(claude["launch"], session.read(None))
    session.show("agent_text", None, body="hello")
    svc.read_next(claude["launch"])
    assert _trips(cs, claude["launch"]) == ["tripwire:mode_changed"]
