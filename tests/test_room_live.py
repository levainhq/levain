"""492 R3: the room's streamed text is an EPHEMERAL live buffer (levain.coordinator.room_live), never the record.

Frame shape: Claude Code stream-json with ``--include-partial-messages`` wraps each Anthropic Messages-API streaming
event as ``{"type": "stream_event", "event": {...}, "session_id", "parent_tool_use_id", "uuid"}`` (message_start,
content_block_start, content_block_delta with ``delta: {"type": "text_delta", "text": ...}``, content_block_stop,
message_delta, message_stop). Built here from the documented Messages streaming events; NOT captured from a live child
under the room's argv (residue)."""
from __future__ import annotations

import json

import pytest

from levain.coordinator import claude_session as cs
from levain.coordinator import room_live
from levain.coordinator.claude_session import ClaudeSession, room_argv

SID = "11111111-1111-4111-8111-111111111111"
LID = "launch-live-1"
_n = [0]


def _uid() -> str:
    _n[0] += 1
    return f"00000000-0000-4000-8000-{_n[0]:012d}"


def se(event: dict, parent=None) -> dict:
    return {"type": "stream_event", "event": event, "session_id": SID, "parent_tool_use_id": parent, "uuid": _uid()}


def delta(t: str, parent=None) -> dict:
    return se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}}, parent)


def start() -> dict:
    return se({"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "content": []}})


def block_start() -> dict:
    return se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})


def init() -> dict:
    return {"type": "system", "subtype": "init", "cwd": "/x", "session_id": SID, "tools": [], "mcp_servers": [],
            "permissionMode": "dontAsk", "uuid": _uid()}


def whole(t: str) -> dict:
    return {"type": "assistant", "message": {"id": "msg_1", "role": "assistant",
                                             "content": [{"type": "text", "text": t}]},
            "parent_tool_use_id": None, "session_id": SID, "uuid": _uid()}


def result() -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "completed",
            "session_id": SID, "uuid": _uid(), "num_turns": 1}


class T:
    def __init__(self):
        self.lines: list[str] = []
        self.eof = False
        self.closed = False

    def feed(self, *frames):
        self.lines += [json.dumps(f) + "\n" for f in frames]

    def poll(self, wait):
        out, self.lines = self.lines, []
        return out

    def write(self, data):
        pass

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean():
    room_live._purge(LID)
    yield
    room_live._purge(LID)


def make():
    t = T()
    s = ClaudeSession(t, session_id=SID, live=room_live.Sink(LID))
    return s, t


def texts(batch) -> list[str]:
    return [e.body for e in batch.events if e.kind == "agent_text"]


def test_argv_carries_the_partial_messages_flag_and_nothing_else_changed():
    a = room_argv(mcp_config="/r/mcp.json", settings="/r/settings.json", model="sonnet", session_id=SID)
    assert a.count("--include-partial-messages") == 1
    base = [x for x in a if x != "--include-partial-messages"]
    assert base[:7] == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    for must in ("--tools", "--allowedTools", "--disallowedTools", "--strict-mcp-config", "--setting-sources",
                 "--settings", "--permission-mode"):
        assert must in a
    assert "--dangerously-skip-permissions" not in a and "--bare" not in a


def test_live_text_grows_as_deltas_arrive_and_seq_rises():
    s, t = make()
    t.feed(init(), start(), block_start(), delta("Hel"))
    s.read(None)
    first = room_live.snapshot(LID)
    assert first["text"] == "Hel" and first["turn"]
    t.feed(delta("lo, "), delta("world"))
    s.read(None)
    second = room_live.snapshot(LID)
    assert second["text"] == "Hello, world" and second["seq"] > first["seq"] and second["turn"] == first["turn"]


def test_the_whole_frame_clears_live_and_stores_exactly_one_agent_text():
    s, t = make()
    t.feed(init(), start(), block_start(), delta("Hello"), delta(" there"))
    b1 = s.read(None)
    assert texts(b1) == [] and room_live.snapshot(LID)["text"] == "Hello there"
    t.feed(whole("Hello there"))
    b2 = s.read(None)
    assert room_live.snapshot(LID) is None
    assert texts(b2) == ["Hello there"]          # one record; the streamed copy was never stored


def test_turn_end_clears_live():
    s, t = make()
    t.feed(init(), start(), block_start(), delta("half a sentence"))
    s.read(None)
    assert room_live.snapshot(LID) is not None
    t.feed(result())
    s.read(None)
    assert room_live.snapshot(LID) is None


def test_the_cap_holds_and_keeps_the_head():
    s, t = make()
    t.feed(init(), start(), block_start())
    for _ in range(40):
        t.feed(delta("é" * 1000))               # 2 bytes each: 80 KB offered
    s.read(None)
    snap = room_live.snapshot(LID)
    assert len(snap["text"].encode("utf-8")) <= room_live.LIVE_MAX
    assert snap["text"] == "é" * (len(snap["text"]))


def test_the_live_cap_equals_the_stored_cap():
    assert room_live.LIVE_MAX == cs._BODY_MAX


def test_unknown_and_foreign_stream_events_are_ignored_and_never_recorded():
    s, t = make()
    t.feed(init(), start(), block_start(),
           se({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{"}}),
           se({"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "SECRET"}}),
           se({"type": "ping"}), se({"type": "something_new", "x": 1}), se("not an object"),
           {"type": "stream_event"}, delta("from a sub-agent", parent="toolu_1"), delta("ok"))
    batch = s.read(None)
    assert room_live.snapshot(LID)["text"] == "ok"
    assert not [e for e in batch.events if e.kind == "error" and "unparseable" in e.vendor_id]
    assert texts(batch) == []


def test_a_session_with_no_sink_ignores_stream_events_as_before():
    t = T()
    s = ClaudeSession(t, session_id=SID)
    t.feed(init(), start(), delta("x"), whole("x"))
    assert texts(s.read(None)) == ["x"]
    assert room_live.snapshot(LID) is None


def test_close_and_eof_clear_live():
    s, t = make()
    t.feed(init(), start(), block_start(), delta("partial"))
    s.read(None)
    s.close()
    assert room_live.snapshot(LID) is None
    s2, t2 = make()
    t2.feed(init(), start(), block_start(), delta("partial"))
    s2.read(None)
    t2.eof = True
    s2.read(None)
    assert room_live.snapshot(LID) is None


def test_forget_and_launch_bound():
    sink = room_live.Sink(LID)
    sink.delta("t1", "abc")
    room_live.forget(LID)
    assert room_live.snapshot(LID) is None
    ids = [f"bound-{i}" for i in range(room_live.LAUNCH_MAX + 3)]
    for i in ids:
        room_live.Sink(i).delta("t", "x")
    held = [i for i in ids if room_live.snapshot(i) is not None]
    assert len(held) == room_live.LAUNCH_MAX and ids[-1] in held
    for i in ids:
        room_live.forget(i)


# ---- a REAL capture (haiku, --include-partial-messages, one turn; trimmed and anonymised) --------------------------------
def test_real_capture_replays_one_agent_text_and_live_grows_then_clears():
    from pathlib import Path
    lines = (Path(__file__).parent / "fixtures" / "room_partial_real.jsonl").read_text().splitlines()
    frames = [json.loads(x) for x in lines]
    assert not any(f["type"] == "system" for f in frames)
    s, t = make()
    t.feed(init())
    s.read(None)
    streamed, seen_live, agent_texts, thinking_whole = "", [], [], 0
    for f in frames:
        t.feed(f)
        batch = s.read(None)
        agent_texts = texts(batch)           # read(None) returns every event so far
        snap = room_live.snapshot(LID)
        if f["type"] == "stream_event":
            d = f["event"].get("delta") or {}
            if d.get("type") == "text_delta":
                streamed += d["text"]
                assert snap is not None and snap["text"] == streamed
                seen_live.append(len(snap["text"]))
            elif f["event"]["type"] != "content_block_start":
                pass
        if f["type"] == "assistant":
            blocks = f["message"]["content"]
            if blocks[0]["type"] == "thinking":
                thinking_whole += 1
                assert snap is None and agent_texts == []        # thinking made no live text and no agent_text
            else:
                assert snap is None                              # the whole text frame cleared live
    assert thinking_whole == 1 and len(seen_live) > 10 and seen_live == sorted(seen_live)
    assert len(set(seen_live)) > 10                              # it grew across the deltas
    assert len(agent_texts) == 1 and agent_texts[0] == streamed and streamed
    assert room_live.snapshot(LID) is None


# ---- L3 stream r1 fixes ------------------------------------------------------------------------------------------------
def test_a_delta_in_flight_at_teardown_never_resurrects_an_ended_launch():
    """codex + complement MED: the pump had polled a delta; ``_end`` forgets the buffer and closes the session; the pump
    then ingests the delta it already held. The ended launch must show nothing and hold no buffer, in either order."""
    s, t = make()
    t.feed(init(), start(), block_start(), delta("before"))
    s.read(None)
    room_live.forget(LID)                    # RoomLauncher._end: forget first ...
    s.close()                                # ... then the session's close
    s._ingest(json.dumps(delta("late")))     # the delta the pump polled before the end
    assert room_live.snapshot(LID) is None and LID not in room_live._BUF
    room_live.Sink(LID).delta("t", "direct")  # a write straight to the sink is refused alike
    assert room_live.snapshot(LID) is None and LID not in room_live._BUF


def test_a_character_split_at_the_cap_ends_the_buffer_as_a_pure_head():
    """gemini MED / complement LOW: a 2-byte char with 1 byte of room is dropped AND the buffer is full, so a later
    1-byte delta cannot land after the hole; the live text equals the stored cap of the same reply."""
    sink = room_live.Sink(LID)
    head = "a" * (room_live.LIVE_MAX - 1)
    sink.delta("t", head)
    sink.delta("t", "é")
    sink.delta("t", "b")
    snap = room_live.snapshot(LID)
    assert snap["text"] == head and snap["text"] == cs._cap(head + "é" + "b")


def test_an_open_buffer_outlives_newer_launches_and_seq_never_goes_back():
    """gemini MED: past LAUNCH_MAX an idle buffer goes first, never the one mid-reply; complement LOW: a buffer that
    WAS evicted keeps its seq rising when it is written again."""
    others = [f"lru-{i}" for i in range(room_live.LAUNCH_MAX + 2)]
    for i in others:
        room_live._purge(i)
    active = room_live.Sink(LID)
    active.delta("t1", "mid-reply")
    try:
        for i in others:                     # newer launches whose turn has ended (written, then cleared)
            room_live.Sink(i).delta("t", "x")
            room_live.clear(i)
        assert room_live.snapshot(LID)["text"] == "mid-reply"
        # now evict the active one deliberately: every other buffer is open too
        seq = room_live.snapshot(LID)["seq"]
        active.clear()
        seq = room_live._SEQ
        for i in others:
            room_live.Sink(i).delta("t", "y")
        assert LID not in room_live._BUF
        active.delta("t2", "again")
        assert room_live.snapshot(LID)["seq"] > seq
    finally:
        for i in others:
            room_live._purge(i)


def test_seq_never_goes_back_after_an_evicted_launch_is_written_late():
    """codex L3 r2 MED: evict a launch, churn many others, then a late delta for it: seq still rises (one global counter)."""
    others = [f"churn-{i}" for i in range(room_live.LAUNCH_MAX * 6)]
    room_live.Sink(LID).delta("t", "a")
    first = room_live.snapshot(LID)["seq"]
    room_live.clear(LID)                     # idle, so churn evicts it
    try:
        for i in others:
            room_live.Sink(i).delta("t", "x")
            room_live.clear(i)
        assert LID not in room_live._BUF
        room_live.Sink(LID).delta("t", "b")
        assert room_live.snapshot(LID)["seq"] > first
    finally:
        for i in others:
            room_live._purge(i)


def test_ended_tombstones_hold_thousands():
    assert room_live.ENDED_MAX == 4096
