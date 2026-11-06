"""The room's open-check turn is the launch's own proof, not a conversation (roomcanary, 1105-31). Reproduced on a running
scratch room: the canary Reads, their refusals and the child's reply to the check were drawn at the top of the cockpit's room
and replayed with it. The rows below have the shape the live record gave (``chat_timeline`` rows: the check's rows were
recorded ``backfill``, between the launch's hook rows and its first ``vendor_turn_ended``)."""
from __future__ import annotations

from levain.cockpit import room as cockpit_room
from levain.coordinator.chat import TimelineRow
from levain.coordinator.room_launch import CANARY_PROMPT


def _row(gseq, kind, launch, body="", source="vendor"):
    return TimelineRow(gseq, kind, source, "%s:%d" % (kind, gseq), launch, None, body, "2026-11-05T21:48:38+00:00", None)


def _check(start, launch):
    """One launch's open check as the record held it: hooks, the turn, two Reads each refused, the reply, the turn's end."""
    return [
        _row(start, "vendor_error", launch, "SessionStart:startup"),
        _row(start + 1, "vendor_error", launch, "dontAsk"),
        _row(start + 2, "vendor_turn_started", launch),
        _row(start + 3, "vendor_tool_call", launch),
        _row(start + 4, "vendor_tool_result", launch, "<tool_use_error>File is in a directory that is denied by your permission settings.</tool_use_error>"),
        _row(start + 5, "vendor_tool_call", launch),
        _row(start + 6, "vendor_tool_result", launch, "/var/folders/x/levain-room-scope-q/levain-room-scope-canary.txt is outside /tmp/room"),
        _row(start + 7, "vendor_agent_text", launch, "done"),
        _row(start + 8, "vendor_turn_ended", launch, "success completed"),
    ]


def _real(start, launch):
    """One real turn: his line, then the room's turn."""
    return [
        _row(start, "message", None, "hi", source="operator"),
        _row(start + 1, "vendor_turn_started", launch),
        _row(start + 2, "vendor_agent_text", launch, "Hello."),
        _row(start + 3, "vendor_turn_ended", launch, "success completed"),
    ]


def _kinds(rows):
    return [(r.gseq, r.kind) for r in rows]


def test_a_launchs_open_check_turn_is_not_in_what_the_page_draws():
    rows = _check(10, "L1") + _real(30, "L1")
    got = cockpit_room._without_open_check(rows)
    assert [r.gseq for r in got] == [10, 11, 30, 31, 32, 33], _kinds(got)       # the hook rows and the real turn stay
    assert not [r for r in got if r.body in ("done",) or "canary" in (r.body or "")], got


def test_every_launch_of_the_room_loses_its_own_check_and_keeps_the_conversation_before_it():
    rows = _check(10, "L1") + _real(30, "L1") + _check(50, "L2") + _real(70, "L2")
    got = cockpit_room._without_open_check(rows)
    assert [r.gseq for r in got] == [10, 11, 30, 31, 32, 33, 50, 51, 70, 71, 72, 73], _kinds(got)


def test_a_check_still_running_shows_none_of_its_turn_and_a_failed_hook_still_shows():
    rows = _check(10, "L1")[:-3]            # no turn end yet: the Reads were made, the reply has not come
    got = cockpit_room._without_open_check(rows)
    assert [r.kind for r in got] == ["vendor_error", "vendor_error"], _kinds(got)


def test_his_own_line_is_never_dropped_even_when_it_is_recorded_before_the_check_rows():
    rows = _real(5, "L1")[:1] + _check(10, "L1") + _real(30, "L1")[1:]
    got = cockpit_room._without_open_check(rows)
    assert any(r.kind == "message" and r.body == "hi" for r in got), _kinds(got)
    assert [r.gseq for r in got if r.kind == "vendor_agent_text"] == [32], _kinds(got)


def test_a_row_with_no_launch_is_kept():
    rows = [_row(1, "vendor_agent_text", None, "x")] + _check(10, "L1")
    assert [r.gseq for r in cockpit_room._without_open_check(rows)][0] == 1


def test_the_check_prompt_tells_the_child_not_to_speak_of_it_afterwards():
    """Measured on the scratch room (haiku): with the old prompt the child answered his first "hi" with "the two canary reads
    are still blocked". The text names the check as the system's, not his, and asks for silence about it; whether a model
    obeys is a measurement, not this assertion."""
    text = CANARY_PROMPT.format(path="/p/decoy", scope="/q/scope")
    assert text.startswith("Open check") and "/p/decoy" in text and "/q/scope" in text
    assert "not the user's message" in text and "never mention it, these paths or their refusals afterwards" in text


def test_a_launch_that_is_not_a_room_launch_keeps_its_first_turn():
    """L3 room r2 codex MED: a worker launch can share the room's thread; its first turn is real work, not a check."""
    rows = _check(10, "L1") + _real(30, "L1") + _real(50, "W1")
    got = cockpit_room._without_open_check(rows, {"L1"})
    assert [r.gseq for r in got] == [10, 11, 30, 31, 32, 33, 50, 51, 52, 53], _kinds(got)   # W1's turn stays, L1's check goes
