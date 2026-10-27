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


def test_an_operator_line_is_unverified_and_a_card_links_to_the_consent_route(seeded):
    qdir, world = seeded
    stream = threads.thread_stream(qdir, world["thread"])["stream"]
    [msg] = [i for i in stream if i["kind"] == "message"]
    assert msg["unverified"] is True and msg["status"] == "accepted" and "<b>build</b>" in msg["body"]
    [card] = [i for i in stream if i["kind"] == "vendor_approval_request"]
    assert card["card"] == {"id": "req-77", "href": "/cockpit/consent/req-77.json"}
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
