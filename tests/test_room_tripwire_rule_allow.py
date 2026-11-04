"""RM-E seam 1: the room's TripwireContext.

Each recorded room run (RM0, RM0B) shows every accepted in-set tool call carrying a permission_decision of
``accept`` / ``user_permanent`` (his user allow list) or ``config`` (the argv's allow list) / reason ``rule``, and no
PreToolUse event in the stream. A worker's window latches both (``unexpected_rule``; a ``deficit`` against its hook
count). ``rule_allow`` is the room's way to say its own allow list is the one answering. These tests pin what it
accepts and what it does not."""
from __future__ import annotations

import pytest

from levain.coordinator.chat_tripwires import TripwireContext, TripwireWindow
from levain.coordinator.driver import event

ROOM_PAIRS = frozenset((t, s) for t in ("Read", "Grep", "Glob", "WebSearch", "mcp__levain_room__propose_drop")
                       for s in ("user_permanent", "config"))
RULE = {"decision": "accept", "source": "user_permanent", "reason_type": "rule"}


def _ctx(rule_allow=frozenset()):
    def expected(tool):
        raise ValueError(tool)
    return TripwireContext("/tmp", expected, lambda _i, _t: False, rule_allow)


def _call_and_result(w, tool, decision, n=1):
    w.observe(event("tool_call", f"c{n}", f"p{n}a", call_id=f"c{n}", tool_name=tool, tool_input={"file_path": "/x"}))
    return w.observe(event("tool_result", f"c{n}", f"p{n}b", call_id=f"c{n}", tool_name=tool, decision_source=decision))[0]


def test_a_worker_context_latches_an_accepted_rule_read():
    """The failure the room would have without rule_allow (pins the base behaviour)."""
    trip = _call_and_result(TripwireWindow(_ctx()), "Read", RULE)
    assert trip is not None and trip.name == "deficit"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "WebSearch", "mcp__levain_room__propose_drop"])
@pytest.mark.parametrize("source", ["user_permanent", "config"])
def test_the_rooms_own_allow_rule_does_not_latch(tool, source):
    w = TripwireWindow(_ctx(ROOM_PAIRS))
    assert _call_and_result(w, tool, {"decision": "accept", "source": source, "reason_type": "rule"}) is None


def test_a_deny_of_an_in_set_tool_does_not_latch():
    w = TripwireWindow(_ctx(ROOM_PAIRS))
    assert _call_and_result(w, "Read", {"decision": "reject", "source": "config", "reason_type": "rule"}) is None


@pytest.mark.parametrize("decision,name", [
    ({"decision": "accept", "source": "projectSettings", "reason_type": "rule"}, "unexpected_rule"),
    ({"decision": "accept", "source": "session", "reason_type": "rule"}, "unexpected_rule"),
    ({"decision": "accept", "source": "hook", "reason_type": "hook"}, "accepted_without_host"),
    ({"decision": "accept", "source": "config", "reason_type": "mode"}, "accepted_without_host"),
    ({"decision": "accept", "source": "user_permanent", "reason_type": "other"}, "accepted_without_host"),
    ({"decision": "ask", "source": "config", "reason_type": "rule"}, "accepted_without_host"),
    (None, "accepted_without_host"),
])
def test_anything_but_an_allow_rule_from_a_listed_source_still_latches(decision, name):
    trip = _call_and_result(TripwireWindow(_ctx(ROOM_PAIRS)), "Read", decision)
    assert trip is not None and trip.name == name


@pytest.mark.parametrize("tool", ["Bash", "Edit", "Write", "WebFetch", "Task"])
def test_a_tool_outside_the_rooms_set_latches_even_under_the_rooms_allow_rule(tool):
    trip = _call_and_result(TripwireWindow(_ctx(ROOM_PAIRS)), tool, RULE)
    assert trip is not None and trip.name == "deficit"


def test_an_unhashable_source_is_a_trip_not_a_crash():
    trip = _call_and_result(TripwireWindow(_ctx(ROOM_PAIRS)), "Read",
                            {"decision": "accept", "source": ["config"], "reason_type": "rule"})
    assert trip is not None and trip.name == "unexpected_rule"
