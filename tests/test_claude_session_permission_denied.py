"""A real Claude stream's ``system/permission_denied`` frame carries a sentence in its top-level ``message``; the room
showed "a frame this session cannot read: message is not an object" after every refused tool use (roomfix, 1105-24).
The frames here are a captured run, not written by hand: a Read inside the working directory that succeeded, then a Read
outside it that the settings refused."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_coordinator_claude_session import make

REAL = Path(__file__).with_name("fixtures") / "claude_stream" / "permission_denied_read.jsonl"


def _frames() -> list[dict]:
    return [json.loads(line) for line in REAL.read_text().splitlines() if line.strip()]


def test_the_captured_run_has_the_frame_that_broke_ingest():
    sysf = [f for f in _frames() if f.get("type") == "system"]
    assert len(sysf) == 1 and sysf[0]["subtype"] == "permission_denied" and isinstance(sysf[0]["message"], str)


def test_a_real_permission_denied_frame_makes_no_error_event_and_the_tool_rows_land_as_before():
    s, t = make()
    t.feed(*_frames())
    evs = list(s.read(None).events)
    assert [e for e in evs if e.kind == "error"] == [], [(e.vendor_id, e.body) for e in evs if e.kind == "error"]
    assert not any(e.vendor_id.startswith("unparseable:") for e in evs)
    calls = [e for e in evs if e.kind == "tool_call"]
    results = [e for e in evs if e.kind == "tool_result"]
    assert [e.tool_name for e in calls] == ["Read", "Read"]
    assert len(results) == 2 and [e.call_id for e in results] == [e.call_id for e in calls]
    assert "outside" in results[1].body                                  # the denial text rides the tool_result row
    assert any(f.get("subtype") == "permission_denied" for f in s.recent_frames())     # kept for the diagnosis tail


@pytest.mark.parametrize("kind", ["assistant", "user"])
def test_a_string_message_on_a_frame_whose_handler_reads_an_object_is_still_refused(kind):
    s, t = make()
    t.feed({"type": kind, "message": "not a mapping", "uuid": "u-msg"})
    evs = list(s.read(None).events)
    bad = [e for e in evs if e.vendor_id.startswith("unparseable:")]
    assert len(bad) == 1 and "message is not an object" in bad[0].body
