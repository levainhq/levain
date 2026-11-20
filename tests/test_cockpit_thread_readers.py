"""The thread record's shared readers (:mod:`levain.cockpit.threads`) that the room still reads through: one timeline row as a
stream item, the per-event facts, the read-only open, and the model a session reported. The thread view's own routes are gone."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import urllib.error
import urllib.request

import pytest

from levain.cockpit import threads
from levain.desk.chat import TimelineRow
from levain.desk.chat_approvals import APPROVAL_EVENT_KINDS


def _row(kind, ref, launch):
    return TimelineRow(1, kind, "vendor", ref, launch, None, "b", "t", None)


def test_a_tool_completion_belongs_to_its_own_launch():
    facts = {("L-A", "tool_call:1"): ("call-1", 0, 0, None), ("L-B", "tool_call:1"): ("call-1", 0, 0, None)}
    done = {("L-A", "call-1")}          # the result arrived in launch A only
    a = threads._item(_row("vendor_tool_call", "tool_call:1", "L-A"), facts, done)
    b = threads._item(_row("vendor_tool_call", "tool_call:1", "L-B"), facts, done)
    assert (a["tool"]["open"], b["tool"]["open"]) == (False, True)


def test_an_approval_body_never_reaches_the_json_in_either_kind():
    # the last two are chat_timeline's own approval arms (APPROVAL_VIEW_ARMS), whose ref is the whole durable key
    for kind in ("vendor_approval_request", "vendor_approval_resolved", "approval",
                 *("approval_" + k for k in APPROVAL_EVENT_KINDS)):
        row = dataclasses.replace(_row(kind, "can_use_tool:req-77:toolu_SECRETID", "L-A"), body="Bash rm x req-77")
        item = threads._item(row, {}, set())
        assert item["body"] is None and "req-77" not in json.dumps(item) and "toolu_SECRETID" not in json.dumps(item), item
    # a kind that only shares the prefix is an ordinary row
    plain = threads._item(dataclasses.replace(_row("approval_queue_note", "a:b", "L-A"), body="kept"), {}, set())
    assert plain["body"] == "kept" and plain["ref"] == "a:b"


def test_an_operator_line_is_unverified_and_a_tool_row_carries_the_vendors_tool_name():
    said = dataclasses.replace(_row("message", "cm-1", None), source="operator", body="hi")
    assert threads._item(said, {}, set())["unverified"] is True
    item = threads._item(_row("vendor_tool_result", "tool_result:1", "L-A"), {("L-A", "tool_result:1"): ("c", 0, 0, "Bash")}, set())
    assert item["tool_name"] == "Bash"


def test_open_is_read_only_and_a_missing_store_is_none(tmp_path):
    assert threads._open(tmp_path) is None
    db = sqlite3.connect(tmp_path / "dispatch.db")
    db.execute("CREATE TABLE t (x)"); db.commit(); db.close()
    conn = threads._open(tmp_path)
    try:
        assert threads._has(conn, "t") and not threads._has(conn, "nope")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()


def test_event_facts_of_a_store_with_no_chat_tables_is_empty(tmp_path):
    db = sqlite3.connect(tmp_path / "dispatch.db"); db.close()
    conn = threads._open(tmp_path)
    try:
        assert threads._event_facts(conn, {"L-A"}) == ({}, False)
    finally:
        conn.close()


class _Svc:
    def __init__(self, model):
        self._model = model

    def session_meta(self, launch):
        if isinstance(self._model, Exception):
            raise self._model
        return type("M", (), {"model": self._model})()


def test_the_model_in_use_is_the_one_the_session_reported():
    assert threads.model_in_use(_Svc("claude-test-1"), "ln-1") == "claude-test-1"


@pytest.mark.parametrize("model", [None, "", 7, RuntimeError("meta broke")])
def test_a_model_that_is_absent_unreadable_or_not_a_string_is_none(model):
    assert threads.model_in_use(_Svc(model), "ln-1") is None


def test_without_a_service_or_a_launch_there_is_no_model():
    assert threads.model_in_use(None, "ln-1") is None and threads.model_in_use(_Svc("m"), None) is None


def test_event_facts_gap_is_true_for_a_stream_gap_event_or_a_cursor_that_could_not_catch_up(tmp_path):
    # the gap flag the room's read still carries (levain#177 deleted its only other pin, the thread catch-up test)
    db = sqlite3.connect(tmp_path / "dispatch.db")
    db.execute("CREATE TABLE chat_cursors (launch_id TEXT, seq INTEGER, gap INTEGER)")
    db.executemany("INSERT INTO chat_cursors VALUES (?, ?, ?)", [("L-A", 1, 1), ("L-A", 2, 0), ("L-B", 1, 0), ("L-B", 2, 1)])
    db.commit(); db.close()
    conn = threads._open(tmp_path)
    try:
        assert threads._event_facts(conn, {"L-A"})[1] is False       # the LATEST cursor row decides: L-A caught up
        assert threads._event_facts(conn, {"L-B"})[1] is True        # L-B's latest says it could not
        assert threads._event_facts(conn, {"L-A", "L-B"})[1] is True
    finally:
        conn.close()


def test_the_deleted_thread_routes_answer_the_generic_404_past_the_write_token(tmp_path):
    # levain#177: the Threads zone's routes are gone; a client that still asks gets an unknown route, never a 500 or a read
    from tests.test_cockpit_k2a import TOKEN, _install, _serve
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        for path in ("/cockpit/threads.json", "/cockpit/thread/th-" + "0" * 32 + ".json"):
            req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + TOKEN})
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=10)
            assert err.value.code == 404, (path, err.value.code)
