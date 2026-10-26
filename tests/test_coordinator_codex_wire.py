"""The Codex wire client (CH-D3 rev 2 §3.2, S1) over the fake app-server in ``tests/_codex_fake_daemon.py``, on both
framings. Each behaviour is checked twice: green on :class:`CodexWire`, and red on a wire broken on purpose in exactly
that behaviour (one that drops server requests, one that reorders, one that coerces ``0`` to ``"0"``, one that reads
``id: 0`` as no id, one that answers a foreign thread, one with no bootstrap state, one with no reader-thread guard).
No daemon is started, no model is called and no sandbox runs; the one test that runs the installed ``codex`` only
regenerates its JSON schema, offline, under a scratch CODEX_HOME."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from levain.coordinator import codex_launch as cx
from levain.coordinator import codex_wire as cw
from levain.coordinator.codex_wire import CodexError, CodexWire, Inbound
from tests import _codex_fake_daemon as fake
from tests._codex_fake_daemon import FakeDaemon

TRANSPORTS = ("ws", "line")


# --- wires broken on purpose, one behaviour each ----------------------------------------------------------------
class DropsRequests(CodexWire):
    def _enqueue_locked(self, item: Inbound) -> None:
        if item.kind != "request":
            super()._enqueue_locked(item)


class Reorders(CodexWire):
    def _enqueue_locked(self, item: Inbound) -> None:
        if item.kind == "request":
            self._queue.appendleft(item)
        else:
            super()._enqueue_locked(item)


class CoercesIds(CodexWire):
    _id_key = staticmethod(lambda rid: ("str", str(rid)))
    _wire_id = staticmethod(lambda rid: str(rid))


class ZeroIsNoId(CodexWire):
    _has_id = staticmethod(lambda msg: bool(msg.get("id")))


class AnswersForeign(CodexWire):
    def _thread_of(self, method, params):
        tid, why = super()._thread_of(method, params)
        return (None if tid is not None else tid), why


class NoBootstrap(CodexWire):
    def start_thread(self, params, *, timeout=cw._CALL_TIMEOUT):
        got = self._call("thread/start", params, timeout, refuse=False, boot=False)
        self.bind_thread(got["thread"]["id"])
        return got


class NoReaderGuard(CodexWire):
    def _on_reader_thread(self) -> bool:
        return False


# --- fixtures and helpers ---------------------------------------------------------------------------------------
@pytest.fixture
def home():
    # a short path: a unix socket path is capped near 104 bytes on macOS, which pytest's tmp_path can pass
    d = Path(tempfile.mkdtemp(prefix="cw", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def daemon(home):
    d = FakeDaemon(home)
    yield d
    d.close()


def _open(daemon: FakeDaemon, transport: str, cls: type[CodexWire] = CodexWire, **kw) -> CodexWire:
    framing = (cw.WsFraming(cx.control_socket(daemon.home)) if transport == "ws"
               else cw.LineFraming(*daemon.serve_pipes()))
    return cls.open(framing, timeout=kw.pop("timeout", 3.0), **kw)


def _collect(wire: CodexWire, n: int, timeout: float = 2.0) -> list[Inbound]:
    out: list[Inbound] = []
    end = time.monotonic() + timeout
    while len(out) < n:
        got = wire.get(max(0.0, end - time.monotonic()))
        if got is None:
            break
        out.append(got)
    return out


def _drain_when(wire: CodexWire, n: int, timeout: float = 2.0) -> list[Inbound]:
    # every item queued before any is read, so a wire that reorders on enqueue shows it
    end = time.monotonic() + timeout
    while len(wire._queue) < n and time.monotonic() < end:
        time.sleep(0.01)
    return wire.drain()


def _red_and_green(check: Callable[[type[CodexWire]], None], broken: type[CodexWire]) -> None:
    check(CodexWire)
    with pytest.raises((AssertionError, CodexError)):
        check(broken)


# --- arrival order, server requests kept ------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("broken", [DropsRequests, Reorders])
def test_notifications_and_server_requests_are_queued_in_arrival_order(daemon, transport, broken):
    def check(cls):
        wire = _open(daemon, transport, cls)
        try:
            tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
            c = daemon.conns[-1]
            c.notify("turn/started", {"threadId": tid, "turn": {"id": "t1"}})
            c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "i1"), 7)
            c.notify("item/started", {"threadId": tid, "turnId": "t1", "item": {"id": "i2"}, "startedAtMs": 1})
            got = _drain_when(wire, 3)
            assert [(i.kind, i.method) for i in got] == [
                ("notification", "turn/started"), ("request", "item/commandExecution/requestApproval"),
                ("notification", "item/started")]
            assert [i.obs for i in got] == sorted(i.obs for i in got) and {i.gen for i in got} == {wire.gen}
        finally:
            wire.close()

    _red_and_green(check, broken)


# --- typed ids --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("broken", [CoercesIds, ZeroIsNoId])
def test_request_ids_keep_their_json_type_and_zero_is_an_id(daemon, transport, broken):
    def check(cls):
        wire = _open(daemon, transport, cls, timeout=1.0)
        try:
            tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
            c = daemon.conns[-1]
            c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "a"), 0)
            c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "b"), "0")
            got = _collect(wire, 2)
            assert [i.kind for i in got] == ["request", "request"], got
            assert [(type(i.id), i.id) for i in got] == [(int, 0), (str, "0")]
            row = cw.server_request_row(got[0].method)
            for item in got:
                wire.reply(item, result=row.reply)
            deadline = time.monotonic() + 2
            while len(daemon.client_replies) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert [(type(r["id"]), r["id"]) for r in daemon.client_replies] == [(int, 0), (str, "0")]
            assert [r["result"] for r in daemon.client_replies] == [{"decision": "decline"}] * 2
        finally:
            wire.close()

    _red_and_green(check, broken)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_response_whose_id_has_another_json_type_answers_no_call(daemon, transport):
    # the daemon answers hooks/list first under the string form of the call's integer id, then under the id itself
    def spoof(c, _params, _reply):
        c.send({"id": str(c.last_id), "result": {"data": ["spoofed"]}})

    daemon.before_reply["hooks/list"] = spoof
    daemon.hooks = [{"real": True}]

    def check(cls):
        daemon.client_replies.clear()
        wire = _open(daemon, transport, cls)
        try:
            assert wire.call("hooks/list", {}) == {"data": [{"real": True}]}
            (stray,) = _collect(wire, 1)
            assert stray.kind == "unparseable" and "no call is waiting" in stray.detail
        finally:
            wire.close()

    _red_and_green(check, CoercesIds)


# --- the thread filter and the bootstrap state ------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_request_on_another_thread_is_foreign_and_never_answered(daemon, transport):
    def check(cls):
        daemon.client_replies.clear()
        wire = _open(daemon, transport, cls)
        try:
            wire.start_thread({"cwd": "/tmp"})
            daemon.conns[-1].request("item/commandExecution/requestApproval",
                                     fake.command_approval("another-thread", "t9", "x"), 3)
            (item,) = _collect(wire, 1)
            assert item.kind == "foreign" and item.thread_id == "another-thread", item
            with pytest.raises(CodexError, match="never answered"):
                wire.reply(item, result={"decision": "decline"})
            time.sleep(0.1)
            assert daemon.client_replies == []
        finally:
            wire.close()

    _red_and_green(check, AnswersForeign)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_thread_frames_sent_before_thread_start_answers_are_held_then_released(daemon, transport):
    # codex MEDIUM (r2): a new thread has no id while thread/start is outstanding. The daemon here sends
    # thread/started and a settings update for the new thread, one for another thread and one global warning, all
    # BEFORE its response
    def preamble(c, _params, reply):
        tid = reply["result"]["thread"]["id"]
        c.notify("thread/started", {"thread": {"id": tid}})
        c.notify("thread/settings/updated", fake.settings_updated(tid))
        c.notify("thread/settings/updated", fake.settings_updated("someone-else"))
        c.notify("configWarning", {"summary": "w"})

    daemon.before_reply["thread/start"] = preamble

    def check(cls):
        wire = _open(daemon, transport, cls)
        try:
            tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
            assert wire.thread_id == tid
            got = _collect(wire, 4)
            by = {(i.method, i.thread_id): i.kind for i in got}
            assert by == {("thread/started", tid): "notification", ("thread/settings/updated", tid): "notification",
                          ("thread/settings/updated", "someone-else"): "foreign", ("configWarning", None):
                          "notification"}, by
            ours = [i.method for i in got if i.thread_id == tid]
            assert ours == ["thread/started", "thread/settings/updated"]          # released in arrival order
        finally:
            wire.close()

    _red_and_green(check, NoBootstrap)


def test_the_bootstrap_buffer_is_capped_and_says_what_it_dropped(daemon):
    def preamble(c, _params, reply):
        tid = reply["result"]["thread"]["id"]
        for n in range(5):
            c.notify("thread/status/changed", {"threadId": tid, "status": {"type": "idle", "n": n}})

    daemon.before_reply["thread/start"] = preamble
    wire = _open(daemon, "ws", bootstrap_cap=2)
    try:
        wire.start_thread({"cwd": "/tmp"})
        got = _collect(wire, 3)
        assert [i.params["status"]["n"] for i in got[:2]] == [0, 1]
        assert got[2].kind == "unparseable" and "3 message(s) past the bootstrap cap of 2" in \
            got[2].detail
        assert _collect(wire, 1, 0.2) == []
    finally:
        wire.close()


def test_a_thread_start_past_its_deadline_releases_what_it_held_as_foreign(daemon):
    def slow(c, _params, reply):
        c.notify("thread/started", {"thread": {"id": reply["result"]["thread"]["id"]}})
        time.sleep(0.6)

    daemon.before_reply["thread/start"] = slow
    wire = _open(daemon, "ws")
    try:
        with pytest.raises(CodexError, match="did not answer thread/start"):
            wire.start_thread({"cwd": "/tmp"}, timeout=0.2)
        (held,) = _collect(wire, 1)
        assert held.kind == "foreign" and held.method == "thread/started" and wire.thread_id is None
        time.sleep(0.6)
        assert _collect(wire, 1, 0.2) == []                     # the late response is dropped, not a stray
    finally:
        wire.close()


def test_a_required_thread_id_missing_and_a_request_without_a_usable_id_are_unparseable(daemon):
    wire = _open(daemon, "ws")
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        c = daemon.conns[-1]
        c.notify("turn/started", {"turn": {"id": "t1"}})
        c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "i"), None)
        c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "i"), True)
        got = _collect(wire, 3)
        assert [i.kind for i in got] == ["unparseable"] * 3
        assert "without the threadId its schema requires" in got[0].detail
        assert all("no usable id" in i.detail for i in got[1:])
    finally:
        wire.close()


# --- frames that are not messages -------------------------------------------------------------------------------
def test_ws_non_json_and_oversize_frames_are_unparseable_and_the_reader_goes_on(daemon):
    wire = _open(daemon, "ws")
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        c = daemon.conns[-1]
        c.send_raw(b"\x1b[2mnot json\x1b[0m")
        c.send_raw(b"[1, 2]")
        c.send_raw(b"x" * (cw._MAX_MESSAGE + 1))
        c.notify("turn/started", {"threadId": tid, "turn": {"id": "t1"}})
        got = _collect(wire, 4, 10.0)
        assert [i.kind for i in got] == ["unparseable", "unparseable", "unparseable", "notification"], got
        assert "not JSON" in got[0].detail and "not a JSON object" in got[1].detail
        assert f"over {cw._MAX_MESSAGE}" in got[2].detail and wire.closed is None
    finally:
        wire.close()


def test_line_non_json_lines_are_counted_logs_and_an_oversize_line_is_unparseable(daemon):
    wire = _open(daemon, "line")
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        c = daemon.conns[-1]
        c.send_raw(b"\x1b[2m2026-10-26 INFO codex_app_server: started\x1b[0m")
        c.send_raw(b"")
        c.send_raw(b"x" * (cw._MAX_MESSAGE + 1))
        c.notify("turn/started", {"threadId": tid, "turn": {"id": "t1"}})
        got = _collect(wire, 2, 10.0)
        assert [i.kind for i in got] == ["unparseable", "notification"], got
        assert wire.log_lines == 2 and len(wire.log_digest) == 64
    finally:
        wire.close()


# --- calls ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_initialize_then_initialized_then_requests_and_the_fake_refuses_a_request_before_initialized(daemon,
                                                                                                   transport):
    wire = _open(daemon, transport)
    try:
        wire.call("hooks/list", {})
        method, params = daemon.calls[0]
        assert method == "initialize" and params["clientInfo"]["name"] == "levain-chat"
        assert params["capabilities"] == {"experimentalApi": False,
                                          "optOutNotificationMethods": list(cw.OPT_OUT_NOTIFICATIONS)}
        assert daemon.client_notifications[0] == ("initialized", {}) and daemon.refused_before_init == []
    finally:
        wire.close()
    # the wire itself sends nothing but initialize before initialized
    raw = cw.WsFraming(cx.control_socket(daemon.home)) if transport == "ws" else \
        cw.LineFraming(*daemon.serve_pipes())
    unopened = CodexWire(raw)
    with pytest.raises(CodexError, match="before initialized"):
        unopened.call("hooks/list", {})
    # and the fake refuses one that a client sends anyway
    raw.send_text(json.dumps({"id": 5, "method": "thread/read", "params": {"threadId": "x"}}).encode())
    got = json.loads(raw.recv_message())
    assert got == {"id": 5, "error": fake.NOT_INITIALIZED} and daemon.refused_before_init == ["thread/read"]
    raw.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_call_refuse_raises_naming_the_server_request_and_the_request_stays_queued(daemon, transport):
    def ask(c, _params, _reply):
        tid = next(iter(daemon.threads))
        c.request("item/fileChange/requestApproval", fake.file_approval(tid, "t1", "f1"), 11)

    daemon.before_reply["thread/read"] = ask
    wire = _open(daemon, transport)
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        with pytest.raises(CodexError, match="item/fileChange/requestApproval"):
            wire.call("thread/read", {"threadId": tid}, on_server_request="refuse")
        (req,) = _collect(wire, 1)
        assert req.kind == "request" and req.id == 11
        assert wire.call("thread/read", {"threadId": tid})["thread"]["id"] == tid     # the default queues it
        (again,) = _collect(wire, 1)
        assert again.method == "item/fileChange/requestApproval" and _collect(wire, 1, 0.2) == []
        with pytest.raises(ValueError):
            wire.call("thread/read", {"threadId": tid}, on_server_request="drop")
    finally:
        wire.close()


def test_a_call_from_the_reader_thread_raises_at_once(daemon):
    # complement Low (r2): a request issued from the reader thread can never be answered, since only that thread
    # reads answers. Without the guard the call below waits out its whole deadline
    def check(cls):
        seen: list[tuple[str, float]] = []
        done = threading.Event()
        holder: dict[str, CodexWire] = {}

        def on_item(item: Inbound) -> None:
            if item.method == "turn/started" and "w" in holder:
                t0 = time.monotonic()
                try:
                    holder["w"].call("thread/items/list", {"threadId": item.thread_id}, timeout=0.5)
                    seen.append(("answered", time.monotonic() - t0))
                except CodexError as exc:
                    seen.append((str(exc), time.monotonic() - t0))
                done.set()

        wire = _open(daemon, "ws", cls, on_inbound=on_item)
        holder["w"] = wire
        try:
            tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
            daemon.conns[-1].notify("turn/started", {"threadId": tid, "turn": {"id": "t1"}})
            assert done.wait(3.0)
            (why, took), = seen
            assert "reader thread" in why and took < 0.1, (why, took)
        finally:
            wire.close()

    _red_and_green(check, NoReaderGuard)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_the_fake_serves_steer_lists_read_resume_and_hooks(daemon, transport):
    daemon.hooks = [{"eventName": "PreToolUse", "sourcePath": "/x/hooks.json"}]
    wire = _open(daemon, transport)
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        t1 = wire.call("turn/start", {"threadId": tid, "input": []})["turn"]["id"]
        daemon.threads[tid]["items"].append({"id": "i1", "turnId": t1, "type": "agentMessage", "text": "hi"})
        assert wire.call("turn/steer", {"threadId": tid, "expectedTurnId": t1, "input": []}) == {"turnId": t1}
        with pytest.raises(CodexError, match=re.escape(f"({t1})")) as stale:
            wire.call("turn/steer", {"threadId": tid, "expectedTurnId": "old", "input": []})
        assert stale.value.error["code"] == -32600
        assert wire.call("thread/items/list", {"threadId": tid, "turnId": t1})["data"][0]["id"] == "i1"
        assert [t["id"] for t in wire.call("thread/turns/list", {"threadId": tid,
                                                                  "sortDirection": "asc"})["data"]] == [t1]
        assert wire.call("thread/read", {"threadId": tid})["thread"]["status"]["type"] == "active"
        assert wire.call("thread/resume", {"threadId": tid, "excludeTurns": True})["approvalsReviewer"] == "user"
        assert wire.call("hooks/list", {})["data"] == daemon.hooks
        with pytest.raises(CodexError, match="thread not loaded") as gone:
            wire.call("thread/read", {"threadId": "nope"})
        assert gone.value.error == {"code": -32600, "message": "thread not loaded: nope"}
    finally:
        wire.close()


# --- replies ----------------------------------------------------------------------------------------------------
def test_each_registry_deny_goes_out_once_and_resolved_follows(daemon):
    def resolved(c, msg):
        tid = next(iter(daemon.threads))
        c.notify("serverRequest/resolved", {"threadId": tid, "requestId": msg["id"]})

    daemon.on_client_reply = resolved
    wire = _open(daemon, "ws")
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        c = daemon.conns[-1]
        sent = fake.non_approval_requests(tid, "t1")
        sent["item/commandExecution/requestApproval"] = fake.command_approval(tid, "t1", "c")
        sent["item/fileChange/requestApproval"] = fake.file_approval(tid, "t1", "f")
        sent["some/new/method"] = {"threadId": tid}
        for n, (method, params) in enumerate(sent.items()):
            c.request(method, params, n)
        items = [i for i in _collect(wire, 2 * len(sent), 3.0) if i.kind == "request"]
        assert [i.method for i in items] == list(sent)
        for item in items:
            row = cw.server_request_row(item.method)
            frame = row.deny()
            if "result" in frame:
                wire.reply(item, result=frame["result"])
            else:
                wire.reply(item, error=(frame["error"]["code"], frame["error"]["message"]))
            with pytest.raises(CodexError, match="already answered"):
                wire.reply(item, result={})
        resolved_ids = [i.params["requestId"] for i in _collect(wire, len(sent), 3.0)
                        if i.method == "serverRequest/resolved"]
        assert sorted(resolved_ids) == list(range(len(sent)))
        replies = {r["id"]: r for r in daemon.client_replies}
        assert replies[0]["result"] == {"permissions": {}} and replies[1]["result"] == {"action": "decline"}
        assert replies[2]["error"]["code"] == -32603
        assert replies[3]["result"] == {"contentItems": [], "success": False}
        assert replies[len(sent) - 1]["error"]["code"] == -32601                     # the catch-all
    finally:
        wire.close()


def test_a_request_from_a_dead_connection_is_never_answered_on_a_new_one(daemon):
    first = _open(daemon, "ws")
    tid = first.start_thread({"cwd": "/tmp"})["thread"]["id"]
    daemon.conns[-1].request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", "i"), 1)
    (old,) = _collect(first, 1)
    first.close()
    with pytest.raises(CodexError, match="closed"):
        first.reply(old, result={"decision": "decline"})
    second = _open(daemon, "ws")
    try:
        second.bind_thread(tid)
        assert second.gen != first.gen and len(second.gen) == 16
        with pytest.raises(CodexError, match="dead connection"):
            second.reply(old, result={"decision": "decline"})
    finally:
        second.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_peer_that_hangs_up_ends_the_queue_and_fails_the_calls(daemon, transport):
    daemon.before_reply["hooks/list"] = lambda c, _p, _r: c.hangup()
    wire = _open(daemon, transport)
    try:
        with pytest.raises(CodexError, match="hooks/list: the connection ended"):
            wire.call("hooks/list", {}, timeout=3.0)
        got = _collect(wire, 1, 3.0)
        assert [i.kind for i in got] == ["closed"] and wire.closed
        with pytest.raises(CodexError, match="the connection is closed"):
            wire.call("thread/read", {"threadId": "x"})
    finally:
        wire.close()


# --- the registry and the pinned surface ------------------------------------------------------------------------
def test_the_registry_covers_every_pinned_server_request_and_only_approvals_are_carded():
    pinned = cw.PINNED_SURFACE[cw.PINNED_VERSION]["ServerRequest"]
    assert set(cw.SERVER_REQUESTS) == set(pinned)
    assert {m for m, r in cw.SERVER_REQUESTS.items() if r.requires_card} == {
        "item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
    for row in cw.SERVER_REQUESTS.values():
        assert (row.reply is None) != (row.error is None), row.method
        assert set(pinned[row.method]) <= row.reads[cw.PINNED_VERSION], row.method
    assert cw.server_request_row("never/heard/of") is cw.CATCH_ALL and cw.CATCH_ALL.latches
    assert cw.CATCH_ALL.deny() == {"error": {"code": -32601, "message": "levain answers no such request in chat"}}
    assert set(cw.OPT_OUT_NOTIFICATIONS) <= set(cw.PINNED_SURFACE[cw.PINNED_VERSION]["ServerNotification"])
    assert "thread/settings/updated" not in cw.OPT_OUT_NOTIFICATIONS


def test_the_relied_on_notifications_are_marked_and_exclude_the_experimental_one():
    table = cw.PINNED_SURFACE[cw.PINNED_VERSION]["ServerNotification"]
    relied = cw.RELIED_NOTIFICATIONS[cw.PINNED_VERSION]
    assert relied <= set(table)
    assert "thread/settings/updated" in table and "thread/settings/updated" not in relied
    assert {"turn/started", "turn/completed", "item/started", "item/completed", "serverRequest/resolved",
            "item/fileChange/patchUpdated", "thread/status/changed", "thread/closed", "error"} <= relied


def test_the_opt_out_list_is_exactly_the_ten_and_disjoint_from_the_relied_on_list():
    assert sorted(cw.OPT_OUT_NOTIFICATIONS) == sorted((
        "item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded", "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
        "command/exec/outputDelta", "thread/tokenUsage/updated", "account/rateLimits/updated",
        "mcpServer/startupStatus/updated"))
    assert len(set(cw.OPT_OUT_NOTIFICATIONS)) == 10
    assert not set(cw.OPT_OUT_NOTIFICATIONS) & cw.RELIED_NOTIFICATIONS[cw.PINNED_VERSION]


def test_the_pinned_surface_matches_the_installed_codex_schema(tmp_path):
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("no codex on PATH: the pinned surface is not checked against a regenerated schema")
    version = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30).stdout.split()[-1]
    if version != cw.PINNED_VERSION:
        pytest.skip(f"codex {version} is installed, not the pinned {cw.PINNED_VERSION}")
    (tmp_path / "home").mkdir()
    out = tmp_path / "schema"
    subprocess.run([codex, "app-server", "generate-json-schema", "--out", str(out)], check=True, timeout=120,
                   capture_output=True, env={"PATH": f"{Path(codex).parent}:/usr/bin:/bin", "HOME": str(tmp_path),
                                             "CODEX_HOME": str(tmp_path / "home")})
    for table, methods in cw.PINNED_SURFACE[cw.PINNED_VERSION].items():
        doc = json.loads((out / f"{table}.json").read_text(encoding="utf-8"))
        defs = doc.get("definitions", {})
        found = {}
        for variant in doc["oneOf"]:
            ref = variant["properties"].get("params", {}).get("$ref", "")
            found[variant["properties"]["method"]["enum"][0]] = tuple(
                sorted(defs.get(ref.rsplit("/", 1)[-1], {}).get("required", [])))
        for method, required in methods.items():
            assert method in found, (table, method)
            assert found[method] == required, (table, method, found[method])
    for row in cw.SERVER_REQUESTS.values():
        if row.response is None:
            continue
        schema = json.loads((out / f"{row.response}.json").read_text(encoding="utf-8"))
        assert set(schema.get("required", [])) <= set(row.reply), row.method
        if "decision" in row.reply:
            assert f'"enum": ["{row.reply["decision"]}"]' in json.dumps(schema), row.method


# --- the L3 r1 bounds (W1-W9), each RED first on the r1 wire -------------------------------------------------------
def _within(fn: Callable[[], object], seconds: float) -> tuple[bool, object]:
    """Run ``fn`` on its own daemon thread: ``(finished, result or the exception it raised)``; a wire that hangs fails
    the test instead of hanging it."""
    out: list[object] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            out.append(exc)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return (not t.is_alive(), out[0] if out else None)


def test_w1_a_second_thread_start_raises_at_once_while_one_is_outstanding_or_bound(daemon):
    daemon.before_reply["thread/start"] = lambda c, _p, _r: time.sleep(0.5)
    wire = _open(daemon, "ws")
    try:
        first: dict[str, object] = {}
        t = threading.Thread(target=lambda: first.update(got=wire.start_thread({"cwd": "/tmp"})), daemon=True)
        t.start()
        time.sleep(0.15)                                    # the first thread/start is outstanding
        t0 = time.monotonic()
        with pytest.raises(CodexError, match="outstanding"):
            wire.start_thread({"cwd": "/tmp"})
        assert time.monotonic() - t0 < 0.2
        t.join(3.0)
        assert wire.thread_id == first["got"]["thread"]["id"]
        with pytest.raises(CodexError, match="already bound"):
            wire.start_thread({"cwd": "/tmp"})
        assert [m for m, _ in daemon.calls].count("thread/start") == 1
    finally:
        wire.close()


@pytest.mark.parametrize("method", ["applyPatchApproval", "execCommandApproval"])
def test_w2_a_legacy_approval_names_its_thread_by_conversation_id(daemon, method):
    wire = _open(daemon, "ws")
    try:
        tid = wire.start_thread({"cwd": "/tmp"})["thread"]["id"]
        c = daemon.conns[-1]
        c.request(method, fake.non_approval_requests("another-thread", "t1")[method], 1)
        c.request(method, fake.non_approval_requests(tid, "t1")[method], 2)
        c.request(method, {k: v for k, v in fake.non_approval_requests(tid, "t1")[method].items()
                           if k != "conversationId"}, 3)
        got = _collect(wire, 3)
        assert [(i.kind, i.thread_id) for i in got] == [("foreign", "another-thread"), ("request", tid),
                                                       ("unparseable", None)], got
        assert "conversationId" in got[2].detail
        with pytest.raises(CodexError, match="never answered"):
            wire.reply(got[0], error=(-32601, "no"))
    finally:
        wire.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_w3_a_send_the_daemon_never_reads_fails_in_the_calls_deadline_and_close_cannot_hang(daemon, transport):
    wire = _open(daemon, transport)
    try:
        daemon.reading.clear()                              # the daemon stops reading: the client's buffers fill
        t0 = time.monotonic()
        done, got = _within(lambda: wire.call("hooks/list", {"pad": "x" * (4 << 20)}, timeout=0.5), 5.0)
        assert done, "the call blocked in its send past its own deadline"
        assert isinstance(got, CodexError) and time.monotonic() - t0 < 2.0, got
        assert wire.closed, "a send that failed mid-message leaves the wire open"
        with pytest.raises(CodexError, match="closed"):
            wire.notify("turn/interrupt", {"threadId": "x", "turnId": "y"})
        done, _ = _within(wire.close, 3.0)
        assert done, "close() hung"
        # the stalled daemon never closes its output: the reader ends because levain closed, not because the peer did
        assert not wire._reader.is_alive()
    finally:
        daemon.reading.set()
        _within(wire.close, 3.0)


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_w4_a_thread_start_timeout_releases_what_it_held_on_the_reader_thread(daemon, transport):
    def slow(c, _params, reply):
        c.notify("thread/started", {"thread": {"id": reply["result"]["thread"]["id"]}})
        time.sleep(0.6)

    daemon.before_reply["thread/start"] = slow
    seen: list[tuple[str, str]] = []
    wire = _open(daemon, transport, on_inbound=lambda i: seen.append((i.kind, threading.current_thread().name)))
    try:
        with pytest.raises(CodexError, match="did not answer thread/start"):
            wire.start_thread({"cwd": "/tmp"}, timeout=0.2)
        (held,) = _collect(wire, 1)
        assert held.kind == "foreign"
        end = time.monotonic() + 2
        while not seen and time.monotonic() < end:
            time.sleep(0.01)
        assert seen == [("foreign", f"codex-wire-{wire.gen}")], seen
    finally:
        wire.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_w4_the_waiter_is_released_before_the_callbacks_for_the_frames_it_unblocked(daemon, transport):
    # codex HIGH 4: the caller holds a session mutex across start_thread and the callback takes the same mutex
    daemon.before_reply["thread/start"] = lambda c, _p, reply: c.notify(
        "thread/started", {"thread": {"id": reply["result"]["thread"]["id"]}})
    mutex = threading.Lock()
    seen: list[str] = []

    def on_item(item: Inbound) -> None:
        with mutex:
            seen.append(item.method)

    wire = _open(daemon, transport, on_inbound=on_item)
    try:
        def start():
            with mutex:
                return wire.start_thread({"cwd": "/tmp"}, timeout=1.0)

        done, got = _within(start, 4.0)
        assert done and isinstance(got, dict), got
        end = time.monotonic() + 2
        while not seen and time.monotonic() < end:
            time.sleep(0.01)
        assert seen == ["thread/started"]
    finally:
        _within(wire.close, 3.0)


def test_w5_a_pong_never_lands_inside_a_text_frame(daemon):
    # codex MED 7, complement MED 1: a ping arrives while a large request is half written
    wire = _open(daemon, "ws")
    try:
        daemon.reading.clear()
        done: list[object] = []
        t = threading.Thread(target=lambda: done.append(wire.call("hooks/list", {"pad": "y" * (4 << 20)},
                                                                  timeout=10.0)), daemon=True)
        t.start()
        time.sleep(0.3)                                     # the request is mid-send: the buffers are full
        daemon.conns[-1].ping(b"pp")
        time.sleep(0.3)                                     # the reader has the ping and would answer it now
        daemon.reading.set()
        t.join(10.0)
        assert daemon.bad_frames == [] and done == [{"data": []}], (daemon.bad_frames, done)
        end = time.monotonic() + 2
        while not daemon.pongs and time.monotonic() < end:
            time.sleep(0.01)
        assert daemon.pongs == [b"pp"] and wire.closed is None
    finally:
        daemon.reading.set()
        _within(wire.close, 3.0)


def test_w6_a_server_request_past_the_bootstrap_cap_closes_the_wire_rather_than_drop_its_id(daemon):
    def preamble(c, _params, reply):
        tid = reply["result"]["thread"]["id"]
        for n in range(3):
            c.request("item/commandExecution/requestApproval", fake.command_approval(tid, "t1", f"i{n}"), n)

    daemon.before_reply["thread/start"] = preamble
    wire = _open(daemon, "ws", bootstrap_cap=2)
    try:
        with pytest.raises(CodexError, match="bootstrap cap"):
            wire.start_thread({"cwd": "/tmp"})
        got = _collect(wire, 4)
        assert [i.kind for i in got][-1] == "closed" and "bootstrap cap" in (wire.closed or ""), got
        assert all(i.kind != "request" for i in got)       # nothing here can be answered: the wire is dead
    finally:
        wire.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("body", [{}, {"result": {"data": []}, "error": {"code": 1, "message": "m"}}])
def test_w7_a_response_without_exactly_one_of_result_and_error_is_a_protocol_fault(daemon, transport, body):
    daemon.before_reply["hooks/list"] = lambda c, _p, _r: c.send({"id": c.last_id, **body})
    wire = _open(daemon, transport)
    try:
        with pytest.raises(CodexError, match="result"):
            wire.call("hooks/list", {})
        (item,) = _collect(wire, 1)
        assert item.kind == "unparseable" and "exactly one of result and error" in item.detail, item
    finally:
        wire.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_w8_the_reader_dying_unexpectedly_closes_the_wire_and_fails_every_waiter(daemon, transport):
    class Faulty(CodexWire):
        def _thread_of(self, method, params):
            if method == "turn/started":
                raise RuntimeError("a bug in the sorting")
            return super()._thread_of(method, params)

    daemon.before_reply["hooks/list"] = lambda c, _p, _r: (c.notify("turn/started", {"threadId": "x", "turn": {}}),
                                                           time.sleep(0.3))
    wire = _open(daemon, transport, Faulty)
    try:
        t0 = time.monotonic()
        with pytest.raises(CodexError, match="RuntimeError"):
            wire.call("hooks/list", {}, timeout=3.0)
        assert time.monotonic() - t0 < 1.5
        assert "a bug in the sorting" in (wire.closed or "")
        assert [i.kind for i in _collect(wire, 1)] == ["closed"]
    finally:
        wire.close()


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_w9_arrival_order_and_its_ordinals_hold_across_bootstrap(daemon, transport):
    def preamble(c, _params, reply):
        tid = reply["result"]["thread"]["id"]
        c.notify("thread/started", {"thread": {"id": tid}})
        c.notify("configWarning", {"summary": "w"})
        c.notify("thread/status/changed", {"threadId": tid, "status": {"type": "idle"}})

    daemon.before_reply["thread/start"] = preamble
    wire = _open(daemon, transport)
    try:
        wire.start_thread({"cwd": "/tmp"})
        got = _drain_when(wire, 3)
        assert [i.method for i in got] == ["thread/started", "configWarning", "thread/status/changed"], got
        assert [i.obs for i in got] == sorted(i.obs for i in got)
    finally:
        wire.close()
