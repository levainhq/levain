"""The room's MCP door (RM-B): one tool, his quoted ids only, the launch bound per process (never named by a call),
refusal for a non-room or not-active launch, the band door's error shape, and the frame cap.

As in ``test_coordinator_room.py``, ``launches.role`` does not yet admit ``room`` on this base, so the fixture widens that
one CHECK in the test store's schema and turns a worker launch into a room launch on a raw connection (scaffolding only)."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import chat, lease, room, room_mcp
from levain.coordinator.chat import ChatStore
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import Part

NOLAUNCH = "ln-" + "0" * 32
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def qdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    d = tmp_path / "home" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def store(qdir, monkeypatch):
    s = EnvelopeStore(qdir, clock=Clock())
    s.coord_set_agent("none")
    ChatStore(s)._connect().close()
    return s


def raw(qdir):
    c = sqlite3.connect(qdir / "dispatch.db", isolation_level=None)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def make_thread(store, title="the room"):
    d = store.accept_drop(title, surface="cli")[0]
    return store.split(d.drop_id, [Part("note", title)])[0].thread_id


def make_launch(store, qdir, thread, *, role="room", n=1):
    token = store.coord_take()
    ln = store.launch_request(token, harness="claude", role="worker", threads=[thread])
    got, why = store.launch_activate(ln.launch_id, harness_session_id=f"sess-{n}")
    assert why is None
    if role != "worker":
        c = raw(qdir)
        c.execute("DROP TRIGGER launches_frozen")
        c.execute("UPDATE launches SET role = ? WHERE launch_id = ?", (role, ln.launch_id))
        c.close()
    return ln.launch_id, f"sess-{n}"


def say(store, thread, body, *, n, author="operator"):
    kind = {"operator": dict(author_kind="operator", author_surface="cockpit", client_id="tab-a"),
            "coordinator": dict(author_kind="coordinator", author_surface="coordinator", client_id="coord")}[author]
    with ChatStore(store)._write() as (conn, now):
        m, _ = chat.record_message(conn, now, thread_id=thread, recipient="thread", outbox_id=f"ob-{n:08d}",
                                   body=body, normalized=body, launch_id=None, **kind)
    return m.message_id


@pytest.fixture
def world(store, qdir):
    thread = make_thread(store)
    c = raw(qdir)
    room.ensure(c)
    room.open_room_thread(c, thread)
    c.close()
    launch, sess = make_launch(store, qdir, thread)
    return {"thread": thread, "launch": launch,
            "m1": say(store, thread, "drop this: fix the thing", n=1),
            "m2": say(store, thread, "and check the other", n=2)}


def call(lid, arguments, name="propose_drop", rid=1, **beside):
    params = {"name": name, "arguments": arguments, **beside}
    return room_mcp.handle(lid, {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params})


def proposals(qdir):
    c = raw(qdir)
    try:
        return c.execute("SELECT proposal_id, quoted, words, summary FROM drop_proposals").fetchall()
    finally:
        c.close()


def refused(reply, *, text):
    assert reply["result"]["isError"] is True, reply
    assert text in reply["result"]["content"][0]["text"], reply


# --- the tool surface -----------------------------------------------------------------------------------------------
def test_tools_list_is_exactly_the_one_tool_and_it_has_no_words_field():
    reply = room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = reply["result"]["tools"]
    assert [t["name"] for t in tools] == ["propose_drop"]
    schema = tools[0]["inputSchema"]
    assert set(schema["properties"]) == {"quote_ids", "summary", "about"}
    assert schema["additionalProperties"] is False


def test_initialize_ping_and_the_band_doors_error_shape(world):
    assert room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"][
        "capabilities"] == {"tools": {}}
    assert room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    assert room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    e = room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})["error"]
    assert e["code"] == -32601
    e = call(NOLAUNCH, {}, name="ask")["error"]
    assert e["code"] == -32602 and "unknown tool" in e["message"]
    assert room_mcp.handle(NOLAUNCH, [])["error"]["code"] == -32600
    assert room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": True, "method": "ping"})["error"]["code"] == -32600
    assert room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": 3})["error"][
        "code"] == -32602


# --- the proposal ---------------------------------------------------------------------------------------------------
def test_a_room_launch_proposes_and_the_words_are_his_bytes(store, qdir, world):
    reply = call(world["launch"], {"quote_ids": [world["m1"], world["m2"]], "summary": "he asked", "about": world["thread"]})
    res = reply["result"]
    assert res["isError"] is False
    text = res["content"][0]["text"]
    assert "A card is waiting for your tap; nothing is sent until you tap." in text
    (pid, quoted, words, summary), = proposals(qdir)
    assert pid in text and summary == "he asked"
    assert words == "drop this: fix the thing\n\nand check the other" and json.loads(quoted) == [world["m1"], world["m2"]]
    c = raw(qdir)
    assert c.execute("SELECT COUNT(*) FROM proposal_events").fetchone()[0] == 0      # nothing committed
    assert c.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 1                # only the fixture's own drop
    assert c.execute("SELECT COUNT(*) FROM drop_notes").fetchone()[0] == 0
    c.close()
    assert "fix the thing" not in text                                               # the reply echoes no words


@pytest.mark.parametrize("key", ["words", "body", "text", "content", "message", "drop"])
def test_a_call_carrying_free_text_words_is_refused_and_writes_nothing(qdir, world, key):
    e = call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s", key: "rm -rf /"})["error"]
    assert e["code"] == -32602 and "no words of its own" in e["message"]
    assert proposals(qdir) == []


def test_any_other_extra_field_is_refused(qdir, world):
    e = call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s", "launch_id": world["launch"]})["error"]
    assert e["code"] == -32602 and "launch_id" in e["message"]
    assert proposals(qdir) == []


@pytest.mark.parametrize("args, text", [
    ("notadict", "arguments must be an object"),
    ({"summary": "s"}, "missing arguments: quote_ids"),
    ({"quote_ids": ["cm-x"]}, "missing arguments: summary"),
    ({"quote_ids": "cm-x", "summary": "s"}, "list of 1 to"),
    ({"quote_ids": [], "summary": "s"}, "list of 1 to"),
    ({"quote_ids": [1], "summary": "s"}, "short string"),
    ({"quote_ids": [True], "summary": "s"}, "short string"),
    ({"quote_ids": ["x" * 500], "summary": "s"}, "short string"),
    ({"quote_ids": ["cm-\x00"], "summary": "s"}, "short string"),
    ({"quote_ids": ["cm-x"] * 21, "summary": "s"}, "list of 1 to"),
    ({"quote_ids": ["cm-x"], "summary": 5}, "summary must be a string"),
    ({"quote_ids": ["cm-x"], "summary": "s" * 601}, "over 600"),
    ({"quote_ids": ["cm-x"], "summary": "s", "about": 7}, "about must be"),
    ({"quote_ids": ["cm-x"], "summary": "s", "about": ""}, "about must be"),
])
def test_a_wrong_shape_is_the_callers_protocol_error(qdir, world, args, text):
    e = call(world["launch"], args)["error"]
    assert e["code"] == -32602 and text in e["message"]
    assert proposals(qdir) == []


def test_the_rooms_own_refusals_come_back_as_the_tools_error(qdir, store, world):
    refused(call(world["launch"], {"quote_ids": ["cm-" + "0" * 32], "summary": "s"}), text="no message")
    own = say(store, world["thread"], "the room's own words", n=3, author="coordinator")
    refused(call(world["launch"], {"quote_ids": [own], "summary": "s"}), text="only his words")
    refused(call(world["launch"], {"quote_ids": [world["m1"], world["m1"]], "summary": "s"}), text="distinct")
    refused(call(world["launch"], {"quote_ids": [world["m1"]], "summary": "   "}), text="summary")
    refused(call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s", "about": "th-" + "0" * 32}), text="names no thread")
    assert proposals(qdir) == []


# --- who may call ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("field", ["launch_id", "launch", "session_id", "sessionId", "threadId", "harness_session_id"])
def test_a_call_naming_its_own_launch_or_session_is_refused_as_a_spoof(qdir, world, field):
    e = call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s", field: world["launch"]})["error"]
    assert e["code"] == -32602 and "never names its own launch" in e["message"]
    e = call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s"}, **{field: "x"})["error"]
    assert e["code"] == -32602 and "never names its own launch" in e["message"]
    assert proposals(qdir) == []


def test_meta_is_not_read_and_the_environment_session_is_not_a_launch(qdir, world, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    reply = call(NOLAUNCH, {"quote_ids": [world["m1"]], "summary": "s"},
                 _meta={"threadId": "sess-1", "claudecode/toolUseId": "toolu_1"})
    refused(reply, text="does not exist")
    assert proposals(qdir) == []


def test_a_worker_launch_never_reaches_the_door(store, qdir, world):
    worker, _ = make_launch(store, qdir, world["thread"], role="worker", n=2)
    refused(call(worker, {"quote_ids": [world["m1"]], "summary": "s"}), text="not a room launch")
    assert proposals(qdir) == []


def test_an_ended_room_launch_refuses(store, qdir, world):
    with store._write() as (conn, now):
        lease.end_launch(conn, now, world["launch"], "ended", "done")
    refused(call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s"}), text="ended, not active")
    assert proposals(qdir) == []


def test_a_launch_of_a_rotated_room_refuses_through_the_rooms_own_check(store, qdir, world):
    new = make_thread(store, "the next room")
    with store._write() as (conn, now):
        room.rotate(conn, new)
    refused(call(world["launch"], {"quote_ids": [world["m1"]], "summary": "s"}), text="not a launch of the open room")
    assert proposals(qdir) == []


def test_no_store_means_nothing_is_made(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "empty"))
    reply = call(NOLAUNCH, {"quote_ids": ["cm-x"], "summary": "s"})
    assert reply["result"]["isError"] is True
    assert not (tmp_path / "empty" / "consent" / "dispatch.db").exists()


# --- the wire -------------------------------------------------------------------------------------------------------
def run(lines, *, binary=False):
    data = "".join(l + "\n" for l in lines)
    out = io.StringIO()
    room_mcp.serve(NOLAUNCH, io.BytesIO(data.encode()) if binary else io.StringIO(data), out)
    return [json.loads(l) for l in out.getvalue().splitlines()]


@pytest.mark.parametrize("binary", [False, True])
def test_malformed_and_oversized_frames_refuse_without_a_traceback_and_the_server_goes_on(binary):
    ping = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    big = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping", "pad": "x" * room_mcp.FRAME_MAX})
    deep = "[" * 100000
    replies = run(["{not json", big, deep, '{"jsonrpc": "2.0"}', ping], binary=binary)
    assert [r.get("error", {}).get("code") for r in replies[:4]] == [-32700, -32600, -32700, -32600]
    assert "frame over" in replies[1]["error"]["message"]
    assert replies[4] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_a_frame_exactly_at_the_cap_is_served():
    pad = room_mcp.FRAME_MAX - len(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": ""}))
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": "x" * pad})
    assert len(line) == room_mcp.FRAME_MAX
    assert run([line], binary=True) == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_replies_are_ascii_so_no_line_separator_splits_one():
    out = io.StringIO()
    room_mcp.serve(NOLAUNCH, io.StringIO(json.dumps({"jsonrpc": "2.0", "id": "a b", "method": "ping"}) + "\n"), out)
    assert out.getvalue().isascii() and len(out.getvalue().splitlines()) == 1


# --- the process -----------------------------------------------------------------------------------------------------
def spawn(qdir, *argv, launch=None):
    env = {k: v for k, v in os.environ.items() if k != room_mcp.LAUNCH_ENV}
    env["LEVAIN_HOME"] = str(qdir.parent)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(room_mcp.__file__))))
    if launch is not None:
        env[room_mcp.LAUNCH_ENV] = launch
    return subprocess.Popen([sys.executable, "-P", "-m", "levain.coordinator.room_mcp", *argv], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)


def test_the_door_refuses_to_start_without_a_launch_or_with_a_malformed_one(qdir):
    for argv, launch in (((), None), ((), "not-a-launch"), (("--launch", "ln-zz"), None)):
        p = spawn(qdir, *argv, launch=launch)
        out, err = p.communicate(timeout=30)
        assert p.returncode == 2 and out == "" and "not started" in err, (argv, launch, err)


@pytest.mark.parametrize("how", ["env", "argv"])
def test_the_real_process_proposes_for_its_bound_launch_only(store, qdir, world, how):
    p = spawn(qdir, *(("--launch", world["launch"]) if how == "argv" else ()),
              launch=world["launch"] if how == "env" else None)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "propose_drop", "arguments": {"quote_ids": [world["m1"]], "summary": "gist"},
                "_meta": {"claudecode/toolUseId": "toolu_1"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "propose_drop", "arguments": {"quote_ids": [world["m1"]], "summary": "gist", "launch_id": "x"}}}]
    out, err = p.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=30)
    replies = [json.loads(l) for l in out.splitlines()]
    assert err == "" and p.returncode == 0
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["propose_drop"]
    assert replies[2]["result"]["isError"] is False and "A card is waiting" in replies[2]["result"]["content"][0]["text"]
    assert replies[3]["error"]["code"] == -32602
    assert len(proposals(qdir)) == 1


# --- L3 r1 fixes ---------------------------------------------------------------------------------------------------
def test_initialize_answers_the_version_the_door_implements():
    r = room_mcp.handle(NOLAUNCH, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "9999-01-01"}})
    assert r["result"]["protocolVersion"] == room_mcp._PROTOCOL


def test_a_store_that_cannot_be_opened_is_a_tool_error_not_a_protocol_error(monkeypatch, world):
    from levain.dispatch.jobs import JobStoreError
    import levain.consent.queue as queue

    for exc in (JobStoreError("consent directory mode is not 0700"), OSError("disk gone")):
        def boom(exc=exc):
            raise exc
        monkeypatch.setattr(queue, "default_queue_dir", boom)
        r = call(world["launch"], {"quote_ids": [world["m1"]], "summary": "gist"})
        assert "error" not in r and r["result"]["isError"] is True
        assert "machine fault" in r["result"]["content"][0]["text"]


def test_a_stream_with_no_line_end_is_given_up_on_not_spun_on():
    class Endless:
        def __init__(self):
            self.reads = 0

        def readline(self, n=-1):
            self.reads += 1
            assert self.reads < 10_000, "unbounded drain"
            return b"x" * n

    out = io.StringIO()
    room_mcp.serve(NOLAUNCH, Endless(), out)
    replies = [json.loads(l) for l in out.getvalue().splitlines()]
    assert len(replies) == 1 and "frame over" in replies[0]["error"]["message"]
