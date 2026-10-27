"""CH-U1a: the thread view's read half over a real ``dispatch.db`` built with ChatService + ScriptedSession."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from levain.cockpit import threads
from levain.coordinator.chat import ChatService, ChatStore
from levain.coordinator.driver import ScriptedSession
from levain.coordinator.envelopes import EnvelopeStore
from tests.test_coordinator_chat_send import LIVE, ME, Clock, _world, check, ready, send


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def seeded(qdir, clock):
    """A thread with a send, an agent line, a tool call (open), an approval request and a second finished tool."""
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    s = ScriptedSession()
    svc = ready(cs, world, s)
    send(svc, cs, world, "fix the <b>build</b>")
    s.show("agent_text", body="on it")
    s.show("tool_call", "c1", body="Bash: ls", call_id="call-1")
    s.show("tool_call", "c2", body="Bash: pwd", call_id="call-2")
    s.show("tool_result", "r2", body="/repo", call_id="call-2")
    s.inbound("req-77", "can_use_tool", tool_name="Bash", tool_input={"command": "rm x"})
    svc.ingest(world["launch"], s.read("00000000"))
    return qdir, world


def test_the_list_names_the_thread_with_its_latest_gseq_and_composer(seeded):
    qdir, world = seeded
    out = threads.thread_list(qdir)
    [row] = out["threads"]
    assert row["thread_id"] == world["thread"] and row["launch_id"] == world["launch"]
    assert row["latest_gseq"] is not None and row["latest_at"]
    assert row["composer"]["state"] == "no_session"       # nothing is attached in a read-only view


def test_the_stream_is_in_gseq_order_and_a_tool_is_open_until_its_result(seeded):
    qdir, world = seeded
    got = threads.thread_stream(qdir, world["thread"])
    gseqs = [i["gseq"] for i in got["stream"]]
    assert gseqs == sorted(gseqs) and len(set(gseqs)) == len(gseqs)
    tools = {i["tool"]["call_id"]: i["tool"]["open"] for i in got["stream"] if i["kind"] == "vendor_tool_call"}
    assert tools == {"call-1": True, "call-2": False}


def test_an_operator_line_is_unverified_and_an_approval_names_no_card(seeded):
    qdir, world = seeded
    stream = threads.thread_stream(qdir, world["thread"])["stream"]
    [msg] = [i for i in stream if i["kind"] == "message"]
    assert msg["unverified"] is True and msg["status"] == "accepted" and "<b>build</b>" in msg["body"]
    [appr] = [i for i in stream if i["kind"] == "vendor_approval_request"]
    assert "card" not in appr and "req-77" not in json.dumps(appr)      # no link, no id: no correlation exists yet
    assert not any("unverified" in i for i in stream if i["kind"] != "message")


def test_a_driver_without_catch_up_shows_the_gap(qdir, clock):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    assert threads.thread_stream(qdir, world["thread"])["gap"] is False
    with cs._write() as (conn, now):
        from levain.coordinator import chat
        chat.record_cursor(conn, now, launch_id=world["launch"], vendor_cursor=None, complete_through=None, gap=True)
    assert threads.thread_stream(qdir, world["thread"])["gap"] is True


def test_reads_write_nothing_and_a_missing_store_is_empty(seeded, tmp_path):
    qdir, world = seeded
    db = qdir / "dispatch.db"
    before = db.read_bytes()
    threads.thread_list(qdir); threads.thread_stream(qdir, world["thread"])
    assert db.read_bytes() == before
    empty = tmp_path / "none"
    empty.mkdir()
    assert threads.thread_list(empty) == {"threads": [], "truncated": False}
    assert not (empty / "dispatch.db").exists()      # never created
    assert threads.thread_stream(empty, world["thread"]) is None


def test_serve_routes_and_refusals(seeded):
    qdir, world = seeded
    st, body = threads.serve_threads(threads.THREADS_LIST_PATH, "", directory=qdir)
    assert st == 200 and body["threads"][0]["thread_id"] == world["thread"]
    st, body = threads.serve_threads(f"{threads.THREAD_PREFIX}{world['thread']}.json", "", directory=qdir)
    assert st == 200 and json.loads(threads.encode(body))["thread_id"] == world["thread"]
    for bad in ("th-" + "0" * 32, "../x", "th-zz", ""):
        assert threads.serve_threads(f"{threads.THREAD_PREFIX}{bad}.json", "", directory=qdir)[0] == 404
    assert threads.serve_threads("/cockpit/other", "", directory=qdir)[0] == 404
    assert threads.is_threads_path(threads.THREADS_LIST_PATH) and not threads.is_threads_path("/cockpit/thread/x")


def test_a_store_that_is_not_a_database_is_a_fixed_500(tmp_path):
    (tmp_path / "dispatch.db").write_bytes(b"not sqlite" * 50)
    st, body = threads.serve_threads(threads.THREADS_LIST_PATH, "", directory=tmp_path)
    assert st == 500 and body["message"] == "the thread record could not be read"


def test_an_old_store_without_the_chat_view_lists_nothing(tmp_path):
    c = sqlite3.connect(tmp_path / "dispatch.db")
    c.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY)")
    c.commit(); c.close()
    assert threads.thread_list(tmp_path)["threads"] == []


def _row(kind, ref, launch):
    from levain.coordinator.chat import TimelineRow
    return TimelineRow(1, kind, "vendor", ref, launch, None, "b", "t", None)


def test_a_tool_completion_belongs_to_its_own_launch():
    facts = {("L-A", "tool_call:1"): ("call-1", 0, 0), ("L-B", "tool_call:1"): ("call-1", 0, 0)}
    done = {("L-A", "call-1")}          # the result arrived in launch A only
    a = threads._item(_row("vendor_tool_call", "tool_call:1", "L-A"), facts, done)
    b = threads._item(_row("vendor_tool_call", "tool_call:1", "L-B"), facts, done)
    assert (a["tool"]["open"], b["tool"]["open"]) == (False, True)


@pytest.mark.parametrize("missing", ["launches", "chat_events", "chat_messages", "chat_outbox_events", "chat_cursors",
                                     "replies"])
def test_a_store_missing_a_table_degrades_that_part_not_the_response(seeded, tmp_path, missing):
    qdir, world = seeded
    part = tmp_path / "part"
    part.mkdir()
    src = sqlite3.connect(qdir / "dispatch.db")
    dst = sqlite3.connect(part / "dispatch.db")
    src.backup(dst)
    src.close()
    dst.execute(f"DROP TABLE {missing}")
    dst.commit(); dst.close()
    st, listing = threads.serve_threads(threads.THREADS_LIST_PATH, "", directory=part)
    assert st == 200 and isinstance(listing["threads"], list)
    st, one = threads.serve_threads(f"{threads.THREAD_PREFIX}{world['thread']}.json", "", directory=part)
    assert st == 200 and one["thread_id"] == world["thread"]
    if missing == "launches":
        assert one["launch_id"] is None and one["composer"] is None
    if missing in ("chat_messages", "chat_outbox_events"):
        assert one["composer"] is None
    if missing == "chat_events":
        assert one["gap"] is False and not any("tool" in i for i in one["stream"])


def test_any_failure_at_the_handler_boundary_is_the_fixed_sentence(seeded, monkeypatch):
    qdir, world = seeded

    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(threads.chat, "timeline", boom)
    st, body = threads.serve_threads(f"{threads.THREAD_PREFIX}{world['thread']}.json", "", directory=qdir)
    assert st == 500 and body == {"error": "internal", "message": "the thread record could not be read"}


def test_the_list_holds_only_threads_with_a_launch_or_a_chat_record_newest_activity_first(qdir, clock):
    from levain.coordinator.store import Part
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)                    # thread 1: a worker launch, no chat lines yet
    cs = ChatStore(store)
    cs._connect().close()
    token = store.coord_take()

    def thread(text):
        d = store.accept_drop(text, surface="cli")[0]
        return store.split(d.drop_id, [Part("work", text)])[0].thread_id
    bare = thread("nothing happens here")      # thread 2: neither a launch nor a chat record
    late = thread("started last")
    ln = store.launch_request(token, harness="claude", role="worker", threads=[late])
    store.launch_activate(ln.launch_id, harness_session_id="sess-worker-2")
    ids = [t["thread_id"] for t in threads.thread_list(qdir)["threads"]]
    assert bare not in ids
    assert ids == [late, world["thread"]]      # the newer launch leads though neither has a chat line
