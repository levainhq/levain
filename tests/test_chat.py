"""K1 part 2: the chat host (``levain.chat``) and its routes on ``levain serve --chat``.

Three layers. The ``ChatHost`` state machine against a stub session (no SDK, no model). The routes
through a real bound server. Then, openhands-gated, real ``EntitySession``s opened by the host's own
default factory: two entities with different floors at once, and a start that fails after its hands
were built. No model is contacted anywhere in this file; the real-model turn is the L4 run.
"""
from __future__ import annotations

import gc
import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from levain.chat import MAX_MESSAGE_CHARS, ChatError, ChatHost
from levain.firing.gate import PendingEfferent


# -- a stub session ------------------------------------------------------------------------------


@dataclass
class _Result:
    reply: str | None = "hello"
    tool_activity: list[str] = field(default_factory=list)
    error: str | None = None
    nudged: bool = False
    gated: bool = False
    timed_out: bool = False
    pending: tuple = ()

    @property
    def ok(self) -> bool:
        return self.error is None and not self.gated and not self.timed_out and bool(self.reply)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else (4 if self.gated else 3)


class _Stub:
    """Plays an EntitySession: each call pops the next scripted result, streaming one line first.
    ``gate`` (a threading.Event), when set on the stub, blocks run_turn until released."""

    label = "stub"
    model_label = "stub-model"
    gate_mode = "ungated"
    bash_ok = True
    deny_standard_creds = False
    workspace = Path("/nonexistent/workspace")

    def __init__(self, on_event, script: list[_Result], hold: threading.Event | None = None):
        self.on_event = on_event
        self.script = script
        self.hold = hold
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def _next(self, name: str, arg: Any = None) -> _Result:
        self.calls.append((name, arg))
        self.on_event(f"⚙ terminal: {name}")
        if self.hold is not None:
            assert self.hold.wait(5)
        return self.script.pop(0)

    def run_turn(self, message):
        return self._next("run_turn", message)

    def resume_turn(self):
        return self._next("resume_turn")

    def reject_turn(self, reason):
        return self._next("reject_turn", reason)

    def close(self):
        self.closed = True


class _Factory:
    def __init__(self, script=None, *, fail: Exception | None = None, hold=None):
        self.script = script if script is not None else []
        self.fail = fail
        self.hold = hold
        self.made: list[_Stub] = []
        self.seen: list[tuple[Path, set[str]]] = []

    def __call__(self, entity_dir, **kw):
        self.seen.append((entity_dir, set(kw)))
        if self.fail is not None:
            raise self.fail
        s = _Stub(kw["on_event"], self.script, self.hold)
        self.made.append(s)
        return s


def _wait(host: ChatHost, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = host.job_status(job_id)
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} still running after {timeout}s: {host.job_status(job_id)}")


def _host(tmp_path, factory, **kw) -> ChatHost:
    return ChatHost({"alpha": tmp_path / "alpha", "beta": tmp_path / "beta"},
                    session_factory=factory, **kw)


def _opened(host: ChatHost, entity="alpha") -> str:
    out = host.open(entity)
    assert _wait(host, out["job_id"])["status"] == "done"
    return out["session_id"]


# -- the state machine ---------------------------------------------------------------------------


def test_open_then_turn_streams_activity_and_returns_the_turn_result(tmp_path):
    f = _Factory([_Result(reply="hi there", tool_activity=["⚙ terminal: ls"])])
    host = _host(tmp_path, f)
    sid = _opened(host)
    assert host.session_status(sid)["state"] == "idle"
    assert host.session_status(sid)["model_label"] == "stub-model"

    job = host.turn(sid, "say hi")
    st = _wait(host, job["job_id"])
    assert st["status"] == "done" and st["kind"] == "turn"
    assert st["activity"] == ["⚙ terminal: run_turn"]
    assert st["result"]["reply"] == "hi there" and st["result"]["ok"] is True
    assert st["result"]["exit_code"] == 0
    assert f.made[0].calls == [("run_turn", "say hi")]
    assert host.session_status(sid)["state"] == "idle"


def test_the_factory_gets_the_registered_dir_and_nothing_from_the_client(tmp_path):
    """The client names an entity. The factory receives the OPERATOR's directory for it and the
    host's own on_event sink, and no other argument exists to carry a client spec."""
    f = _Factory([])
    host = _host(tmp_path, f)
    _opened(host, "beta")
    assert f.seen == [(tmp_path / "beta", {"on_event"})]
    for bad in ({"agent": {}}, ["alpha"], "../alpha", "gamma", None):
        with pytest.raises(ChatError) as e:
            host.open(bad)
        assert e.value.http_status == 404


def test_a_busy_session_refuses_a_second_turn_until_the_first_finishes(tmp_path):
    hold = threading.Event()
    f = _Factory([_Result(), _Result(reply="second")], hold=hold)
    host = _host(tmp_path, f)
    hold.set()
    sid = _opened(host)
    hold.clear()
    first = host.turn(sid, "one")
    with pytest.raises(ChatError) as e:
        host.turn(sid, "two")
    assert e.value.http_status == 409 and e.value.code == "wrong_state"
    with pytest.raises(ChatError):
        host.close(sid)   # never torn down under a running turn
    hold.set()
    _wait(host, first["job_id"])
    assert _wait(host, host.turn(sid, "two")["job_id"])["result"]["reply"] == "second"


def test_a_gated_turn_holds_new_messages_and_accepts_approve_or_reject(tmp_path):
    held = PendingEfferent(tool_name="terminal", detail="git push", reason="network egress")
    f = _Factory([
        _Result(reply=None, gated=True, pending=(held,)),
        _Result(reply="pushed"),
        _Result(reply=None, gated=True, pending=(held,)),
        _Result(reply="ok, a PR instead"),
    ])
    host = _host(tmp_path, f)
    sid = _opened(host)
    st = _wait(host, host.turn(sid, "push it")["job_id"])
    assert st["result"]["gated"] is True and st["result"]["exit_code"] == 4
    assert st["result"]["pending"] == [{"tool": "terminal", "detail": "git push",
                                        "reason": "network egress", "recognized": True}]
    assert host.session_status(sid)["state"] == "gated"
    with pytest.raises(ChatError) as e:
        host.turn(sid, "wait, don't")   # a new message here would be read as approval
    assert e.value.http_status == 409

    assert _wait(host, host.approve(sid)["job_id"])["result"]["reply"] == "pushed"
    assert host.session_status(sid)["state"] == "idle"
    with pytest.raises(ChatError):
        host.approve(sid)               # nothing is held now

    _wait(host, host.turn(sid, "push again")["job_id"])
    st = _wait(host, host.reject(sid, "open a PR instead")["job_id"])
    assert st["result"]["reply"] == "ok, a PR instead"
    assert f.made[0].calls[-1] == ("reject_turn", "open a PR instead")


def test_a_held_action_streamed_mid_turn_does_not_stay_in_the_jobs_activity(tmp_path):
    """The stream fires when an action is ISSUED, before the gate holds it. At a gated finish the
    job's activity becomes the result's tool_activity (held actions removed), so a held push never
    reads as work that ran."""
    held = PendingEfferent(tool_name="terminal", detail="git push", reason="network egress")
    f = _Factory([_Result(reply=None, gated=True, pending=(held,),
                          tool_activity=["⚙ file_editor: view README.md"])])
    host = _host(tmp_path, f)
    sid = _opened(host)
    st = _wait(host, host.turn(sid, "push it")["job_id"])
    assert f.made[0].calls == [("run_turn", "push it")]     # the stub streamed "⚙ terminal: run_turn"
    assert st["activity"] == ["⚙ file_editor: view README.md"]
    assert st["result"]["pending"][0]["detail"] == "git push"


@pytest.mark.parametrize("bad", [
    _Result(reply=None, error="boom"),
    _Result(reply=None, error="took too long", timed_out=True),
    _Result(reply=None, error="the refusal did NOT take", gated=True),
])
def test_a_turn_that_errors_breaks_the_session_and_releases_it(tmp_path, bad):
    f = _Factory([bad])
    host = _host(tmp_path, f)
    sid = _opened(host)
    st = _wait(host, host.turn(sid, "x")["job_id"])
    assert st["status"] == "done" and st["result"]["error"] == bad.error
    view = host.session_status(sid)
    assert view["state"] == "broken" and view["error"] == bad.error
    assert f.made[0].closed is True
    with pytest.raises(ChatError) as e:
        host.turn(sid, "again")
    assert e.value.http_status == 409


def test_a_broken_sessions_shell_is_released_before_it_reads_broken(tmp_path):
    """codex L3 r1: publishing `broken` (which frees the cap slot) before the shell closed let a new
    open start while the old teardown was still running."""
    f = _Factory([_Result(reply=None, error="boom")])
    host = _host(tmp_path, f)
    sid = _opened(host)
    seen = []
    f.made[0].close = lambda: seen.append(host.session_status(sid)["state"])
    _wait(host, host.turn(sid, "x")["job_id"])
    assert seen == ["busy"] and host.session_status(sid)["state"] == "broken"


def test_a_failed_open_keeps_the_message_text_and_never_the_exception(tmp_path):
    f = _Factory(fail=RuntimeError("no such model"))
    host = _host(tmp_path, f)
    out = host.open("alpha")
    st = _wait(host, out["job_id"])
    assert st["status"] == "failed" and st["error"] == "no such model"
    view = host.session_status(out["session_id"])
    assert view["state"] == "failed" and view["error"] == "no such model"
    # Every value the host holds for it is plain data: no exception, so no traceback, so no frame.
    rec = host._sessions[out["session_id"]]
    job = host._jobs[out["job_id"]]
    def walk(value):
        assert not isinstance(value, BaseException), value
        assert getattr(value, "__traceback__", None) is None
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple, set)):
            for v in value:
                walk(v)

    for value in (*vars(rec).values(), *vars(job).values()):
        walk(value)


def test_the_session_cap_counts_live_sessions_only(tmp_path):
    f = _Factory([])
    host = _host(tmp_path, f, max_sessions=2)
    a = _opened(host)
    _opened(host, "beta")
    with pytest.raises(ChatError) as e:
        host.open("alpha")
    assert e.value.http_status == 429
    host.close(a)
    assert f.made[0].closed is True
    _opened(host)


@pytest.mark.parametrize("msg,status", [("", 400), ("   ", 400), (None, 400), (7, 400),
                                        ("x" * (MAX_MESSAGE_CHARS + 1), 413)])
def test_a_bad_message_is_refused_before_any_job(tmp_path, msg, status):
    host = _host(tmp_path, _Factory([]))
    sid = _opened(host)
    with pytest.raises(ChatError) as e:
        host.turn(sid, msg)
    assert e.value.http_status == status
    assert host.session_status(sid)["state"] == "idle"


def test_a_broken_session_does_not_hold_a_slot_and_ended_ones_are_pruned(tmp_path, monkeypatch):
    import levain.chat as chat_mod

    monkeypatch.setattr(chat_mod, "_ENDED_SESSIONS_KEPT", 2)
    f = _Factory([_Result(reply=None, error="boom")])
    host = _host(tmp_path, f, max_sessions=1)
    broken = _opened(host)
    _wait(host, host.turn(broken, "x")["job_id"])
    assert host.session_status(broken)["state"] == "broken"
    later = []
    for _ in range(3):
        sid = _opened(host)          # the broken one does not count against max_sessions=1
        host.close(sid)
        later.append(sid)
    remaining = set(host._sessions)
    assert broken not in remaining and len(remaining) <= 3
    assert later[-1] in remaining


def test_the_premises_this_module_rests_on_still_hold():
    """Two claims in levain.chat's docstring are about levain.session. If either stops being true,
    the chat host's security story changes, so they are pinned here rather than left as prose."""
    import inspect

    import levain.session as session_mod
    from levain.session import EntitySession

    # 1. A session's agent is built inside `open` from the entity; `open` takes no agent, tool spec
    #    or tool params, so no caller (and so no client) can hand one in.
    params = set(inspect.signature(EntitySession.open).parameters)
    assert not params & {"agent", "tools", "tool_specs", "spec", "params", "binding"}, params
    # 2. Nothing is persisted, so a server restart ends every conversation and no resume path
    #    (with the binding design's section-7 obligations) exists yet.
    assert "persistence_dir" not in inspect.getsource(session_mod)


class _Escape(BaseException):
    """Not an Exception, like TurnTimeout and CancelledError."""


def test_a_base_exception_from_a_turn_settles_the_job_and_frees_the_slot(tmp_path):
    f = _Factory([])
    host = _host(tmp_path, f, max_sessions=1)
    sid = _opened(host)
    f.made[0].run_turn = lambda m: (_ for _ in ()).throw(_Escape("stop"))
    st = _wait(host, host.turn(sid, "x")["job_id"])
    assert st["status"] == "failed" and "_Escape" in st["error"]
    assert host.session_status(sid)["state"] == "broken" and f.made[0].closed
    _opened(host)   # the slot came back


def test_a_fault_while_settling_a_job_still_settles_it(tmp_path):
    class _BadLabel(_Stub):
        @property
        def label(self):          # read by _describe while the open is being recorded
            raise RuntimeError("label broke")

    made = []

    def factory(entity_dir, *, on_event):
        made.append(_BadLabel(on_event, []))
        return made[-1]

    host = ChatHost({"alpha": tmp_path / "alpha"}, session_factory=factory)
    out = host.open("alpha")
    st = _wait(host, out["job_id"])
    assert st["status"] == "failed" and "label broke" in st["error"]
    assert host.session_status(out["session_id"])["state"] == "failed"
    assert made[0].closed


def test_shutdown_during_a_turn_closes_the_session_when_the_turn_ends(tmp_path):
    hold = threading.Event()
    f = _Factory([_Result()], hold=hold)
    host = _host(tmp_path, f)
    hold.set()
    sid = _opened(host)
    hold.clear()
    job = host.turn(sid, "x")["job_id"]
    host.shutdown()
    assert host.session_status(sid)["state"] == "busy" and not f.made[0].closed
    hold.set()
    _wait(host, job)
    assert host.session_status(sid)["state"] == "closed" and f.made[0].closed


def test_shutdown_during_an_open_closes_the_session_it_opened(tmp_path):
    gate = threading.Event()
    made = []

    def factory(entity_dir, *, on_event):
        assert gate.wait(5)
        made.append(_Stub(on_event, []))
        return made[-1]

    host = ChatHost({"alpha": tmp_path / "alpha"}, session_factory=factory)
    out = host.open("alpha")
    host.shutdown()
    gate.set()
    st = _wait(host, out["job_id"])
    assert st["status"] == "failed" and "shut down" in st["error"]
    assert host.session_status(out["session_id"])["state"] == "closed" and made[0].closed


def test_a_worker_that_cannot_start_leaves_nothing_behind(tmp_path, monkeypatch):
    f = _Factory([])
    host = _host(tmp_path, f)
    sid = _opened(host)

    def no_thread(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_thread)
    with pytest.raises(ChatError) as e:
        host.open("beta")
    assert e.value.http_status == 503
    with pytest.raises(ChatError) as e:
        host.turn(sid, "x")
    assert e.value.http_status == 503
    assert host.session_status(sid)["state"] == "idle"
    assert list(host._sessions) == [sid]


def test_a_hung_turn_does_not_keep_the_process_alive(tmp_path):
    """L1 + L2, both run: worker threads joined at interpreter exit, so a turn that never returned
    kept the server process alive after it stopped serving."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(f"""
        import threading, time
        from pathlib import Path
        from levain.chat import ChatHost

        class S:
            def run_turn(self, m):
                threading.Event().wait()   # never returns
            def close(self):
                pass

        host = ChatHost({{"a": Path({str(tmp_path)!r})}},
                        session_factory=lambda d, on_event: S())
        out = host.open("a")
        while host.session_status(out["session_id"])["state"] != "idle":
            time.sleep(0.01)
        host.turn(out["session_id"], "hang")
        host.shutdown()
        print("main done", flush=True)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=20)
    assert proc.returncode == 0 and "main done" in proc.stdout, proc.stderr


def test_activity_lines_and_result_activity_are_size_bounded(tmp_path):
    from levain.chat import MAX_ACTIVITY_LINES, MAX_LINE_CHARS

    big = _Result(reply="r", tool_activity=["y" * (MAX_LINE_CHARS * 3)] * (MAX_ACTIVITY_LINES + 7))
    f = _Factory([big])
    host = _host(tmp_path, f)
    sid = _opened(host)
    f.made[0].on_event("x" * (MAX_LINE_CHARS * 3))
    st = _wait(host, host.turn(sid, "x")["job_id"])
    assert all(len(line) <= MAX_LINE_CHARS + 2 for line in st["activity"])
    acts = st["result"]["tool_activity"]
    assert len(acts) == MAX_ACTIVITY_LINES and all(len(a) <= MAX_LINE_CHARS + 2 for a in acts)


class _Floored(_Stub):
    """A stub whose hands report a floor, as EntitySession's do."""

    def __init__(self, on_event, *, deny_localhost: bool | None):
        super().__init__(on_event, [])
        if deny_localhost is not None:
            policy = type("P", (), {"deny_localhost_outbound": deny_localhost})()
            executor = type("E", (), {"_policy": policy})()
            tool = type("T", (), {"executor": executor})()
            agent = type("A", (), {"tools_map": {"file_editor": tool}})()
            self.conversation = type("C", (), {"agent": agent})()


@pytest.mark.parametrize("deny_localhost", [False, None])
def test_a_session_whose_hands_may_reach_localhost_is_refused_after_open(
    tmp_path, monkeypatch, deny_localhost
):
    """codex L3 r1 HIGH: the config is read before open and again by open, so it can change in
    between; the floor the opened hands enforce is the one checked last. An unreadable floor on a
    session with a shell is refused too."""
    from levain.session import EntitySession

    made = []
    monkeypatch.setattr(EntitySession, "open", classmethod(
        lambda cls, path, **kw: made.append(_Floored(kw["on_event"], deny_localhost=deny_localhost))
        or made[-1]))
    host = ChatHost({"ok": _entity(tmp_path, "ok")})
    st = _wait(host, host.open("ok")["job_id"])
    assert st["status"] == "failed" and "localhost" in st["error"]
    assert made[0].closed


def test_the_listing_never_carries_a_session_or_job_id(tmp_path):
    host = _host(tmp_path, _Factory([]))
    sid = _opened(host)
    listed = host.listing()["sessions"]
    assert listed and all("session_id" not in v and "job_id" not in v for v in listed)
    assert sid not in json.dumps(host.listing())


def test_max_iterations_below_one_is_refused_before_anything_starts(tmp_path):
    from levain.cli import _positive_int

    with pytest.raises(ValueError):
        ChatHost({"a": tmp_path}, max_iterations=0)
    import argparse

    for bad in ("0", "-3", "x"):
        with pytest.raises(argparse.ArgumentTypeError):
            _positive_int(bad)
    assert _positive_int("12") == 12


def test_chat_opens_headless_and_refuses_an_entity_that_may_reach_localhost(tmp_path, monkeypatch):
    """HIGH-1 (L1): the routes authenticate nobody, so a session is not `interactive` (the design
    gives that to an authenticated human). L2: an entity allowed to reach localhost could call the
    chat routes from its own shell."""
    import levain.chat as chat_mod
    from levain.session import EntitySession

    seen = {}
    monkeypatch.setattr(EntitySession, "open", classmethod(
        lambda cls, path, **kw: seen.update(kw) or _Floored(kw["on_event"], deny_localhost=True)))
    ok = _entity(tmp_path, "ok")
    open_ = _entity(tmp_path, "open_", deny=None)
    (open_ / ".levain" / "confinement.json").write_text('{"allow_localhost_outbound": true}')
    host = ChatHost({"ok": ok, "open_": open_})
    assert _wait(host, host.open("ok")["job_id"])["status"] == "done"
    assert seen["mode"] == chat_mod.CHAT_DRIVE_MODE == "headless"
    st = _wait(host, host.open("open_")["job_id"])
    assert st["status"] == "failed" and "localhost" in st["error"]

    from levain.web_server import _build_chat_host

    built, err = _build_chat_host([ok, open_], model="m", base_url="u", api_key=None,
                                  max_iterations=None)
    assert built is None and "allow_localhost_outbound" in err


def test_unknown_ids_read_as_unknown_or_404(tmp_path):
    host = _host(tmp_path, _Factory([]))
    assert host.job_status("nope") == {"job_id": "nope", "status": "unknown"}
    with pytest.raises(ChatError) as e:
        host.session_status("nope")
    assert e.value.http_status == 404


def test_shutdown_closes_every_idle_session_and_refuses_new_work(tmp_path):
    f = _Factory([])
    host = _host(tmp_path, f)
    sid = _opened(host)
    host.shutdown()
    assert f.made[0].closed is True
    assert host.session_status(sid)["state"] == "closed"
    with pytest.raises(ChatError) as e:
        host.open("alpha")
    assert e.value.http_status == 503


# -- the routes, through a real bound server -----------------------------------------------------


def _source(tmp_path):
    from levain.dashboard import AnnealPaths, SubstrateSource

    return SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"))


@contextmanager
def _serving(source, chat_host):
    from levain.web_server import make_server

    httpd = make_server(source, host="127.0.0.1", port=0, chat_host=chat_host)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _call(url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 — loopback only
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def _poll(base, job_id):
    for _ in range(500):
        code, st = _call(f"{base}/chat/job.json?id={job_id}")
        assert code == 200
        if st["status"] != "running":
            return st
        time.sleep(0.01)
    raise AssertionError("job never finished")


def test_the_routes_drive_a_turn_end_to_end_on_a_read_only_source(tmp_path):
    """The substrate is read-only (no write scope) and chat still works: chat acts through the
    entity's floor, not the substrate's write path. Extra request fields are not read."""
    f = _Factory([_Result(reply="from the route")])
    host = _host(tmp_path, f)
    with _serving(_source(tmp_path), host) as base:
        code, listing = _call(f"{base}/chat.json")
        assert code == 200 and listing["entities"] == ["alpha", "beta"]
        code, out = _call(f"{base}/chat/open",
                          {"entity": "alpha", "agent": {"tools": []}, "model": "x", "mode": "unattended"})
        assert code == 202
        assert _poll(base, out["job_id"])["status"] == "done"
        sid = out["session_id"]
        code, job = _call(f"{base}/chat/turn", {"session_id": sid, "message": "hi"})
        assert code == 202
        st = _poll(base, job["job_id"])
        assert st["result"]["reply"] == "from the route"
        code, view = _call(f"{base}/chat/session.json?id={sid}")
        assert code == 200 and view["state"] == "idle"
        assert _call(f"{base}/chat/close", {"session_id": sid})[0] == 200
    assert f.seen == [(tmp_path / "alpha", {"on_event"})]


def test_the_chat_routes_ride_the_same_host_and_csrf_guards(tmp_path):
    host = _host(tmp_path, _Factory([]))
    with _serving(_source(tmp_path), host) as base:
        assert _call(f"{base}/chat/open", {"entity": "alpha"},
                     {"Sec-Fetch-Site": "cross-site"})[0] == 403
        assert _call(f"{base}/chat/open", {"entity": "alpha"},
                     {"Content-Type": "text/plain"})[0] == 415
        assert _call(f"{base}/chat/open", {"entity": "alpha"},
                     {"Host": "evil.example"})[0] == 403
        assert _call(f"{base}/chat.json", headers={"Host": "evil.example"})[0] == 403
        assert _call(f"{base}/chat/open", {"entity": "gamma"})[0] == 404
        assert _call(f"{base}/chat/turn", {"session_id": "nope", "message": "x"})[0] == 404
        assert _call(f"{base}/chat/job.json")[0] == 400
    assert host.listing()["sessions"] == []


def test_without_a_chat_host_the_chat_routes_do_not_exist(tmp_path):
    with _serving(_source(tmp_path), None) as base:
        assert _call(f"{base}/chat.json")[0] == 404
        assert _call(f"{base}/chat/open", {"entity": "alpha"})[0] == 404


@pytest.mark.parametrize("host_arg", ["192.168.1.10", "100.64.0.1"])
def test_a_chat_server_refuses_any_non_loopback_bind(tmp_path, host_arg):
    from levain.web_server import make_server

    with pytest.raises(ValueError, match="--chat is loopback-only"):
        make_server(_source(tmp_path), host=host_arg, port=0, chat_host=_host(tmp_path, _Factory([])))


def test_downstream_routes_cannot_claim_a_chat_path(tmp_path):
    from levain.web_server import make_server

    with pytest.raises(ValueError, match="collides"):
        make_server(_source(tmp_path), port=0, extra_json={"/chat.json": lambda: b"{}"})


def test_serve_refuses_a_chat_dir_that_is_not_an_openhands_entity(tmp_path, capsys):
    from levain.web_server import _build_chat_host

    (tmp_path / "a" / "x").mkdir(parents=True)
    (tmp_path / "b" / "x").mkdir(parents=True)
    host, err = _build_chat_host([tmp_path / "a" / "x"], model="m", base_url="u",
                                 api_key=None, max_iterations=None)
    assert host is None and "x" in err
    for d in ("a", "b"):
        (tmp_path / d / "x" / ".levain").mkdir()
        (tmp_path / d / "x" / ".levain" / "config.json").write_text('{"adapter": "openhands"}')
    host, err = _build_chat_host([tmp_path / "a" / "x", tmp_path / "b" / "x"], model="m",
                                 base_url="u", api_key=None, max_iterations=None)
    assert host is None and "two entities are named 'x'" in err
    same = tmp_path / "a" / "x"
    host, err = _build_chat_host([same, tmp_path / "a" / "." / "x"], model="m", base_url="u",
                                 api_key=None, max_iterations=None)
    assert host is not None and host.listing()["entities"] == ["x"]


# -- real EntitySessions (openhands-gated) -------------------------------------------------------


def _entity(root: Path, name: str, deny: list[str] | None = None) -> Path:
    ent = root / name
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    if deny:
        (ent / ".levain" / "confinement.json").write_text(json.dumps({"deny_files": deny}))
    return ent


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("LEVAIN_ENTITY_DIR", raising=False)
    return h


def test_two_entities_open_concurrently_and_each_session_holds_its_own_floor(tmp_path, home):
    """The spore-438 property, through the chat host: two sessions on two entities, opened at the
    same time on the host's workers, each with hands enforcing ITS OWN entity's floor."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    from openhands.tools.file_editor.definition import FileEditorAction

    secret = tmp_path / "shared" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("SECRET-K1P2")
    a = _entity(tmp_path, "alpha", deny=[str(secret)])
    b = _entity(tmp_path, "beta")
    host = ChatHost({"alpha": a, "beta": b})
    try:
        oa, ob = host.open("alpha"), host.open("beta")
        assert _wait(host, oa["job_id"], 60)["status"] == "done", host.job_status(oa["job_id"])
        assert _wait(host, ob["job_id"], 60)["status"] == "done", host.job_status(ob["job_id"])
        sa = host._sessions[oa["session_id"]].session
        sb = host._sessions[ob["session_id"]].session
        assert sa.entity_dir == a.resolve() and sb.entity_dir == b.resolve()
        assert host.session_status(oa["session_id"])["gate_mode"] == sa.gate_mode

        def reads(s) -> bool:
            ed = s.conversation.agent.tools_map["file_editor"].executor
            return "SECRET-K1P2" in str(ed(FileEditorAction(command="view", path=str(secret))))

        assert reads(sa) is False and reads(sb) is True
        fa = sa.conversation.agent.tools_map["file_editor"].executor._policy
        fb = sb.conversation.agent.tools_map["file_editor"].executor._policy
        assert fa is not fb and fa.workspace != fb.workspace
        # The operator's per-entity pin lives in alpha's floor only.
        assert secret.resolve() in fa.deny_files and secret.resolve() not in fb.deny_files
    finally:
        host.shutdown()


def test_a_start_that_fails_after_its_hands_were_built_releases_them(
    tmp_path, home, monkeypatch, request
):
    """codex L3 r1 MED on the binding branch: a server that KEPT a startup exception would keep the
    failed hands alive through its traceback, and their editor history dir with them. The failure is
    injected after the hands exist, inside the SDK's tool build, where conversation.close() cannot
    reach them; they go only when nothing references them any more."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    import tempfile

    import levain.firing.openhands.tools as tools_mod

    scratch = tmp_path / "tmpdirs"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    from openhands.sdk.tool import register_tool

    built: list[str] = []

    class FailingHands(tools_mod.LevainHands):
        @classmethod
        def create(cls, conv_state, **params):  # type: ignore[override]
            hands = tools_mod.LevainHands.create(conv_state, **params)
            for t in hands:
                cache = getattr(getattr(getattr(t.executor, "editor", None),
                                        "_history_manager", None), "cache", None)
                if cache is not None:
                    built.append(str(cache.directory))
            raise RuntimeError("injected after the hands were built")

    # The SDK binds `create` when a tool is REGISTERED, so the class is re-registered, not patched.
    register_tool(tools_mod.LEVAIN_HANDS_TOOL, FailingHands)
    request.addfinalizer(lambda: register_tool(tools_mod.LEVAIN_HANDS_TOOL, tools_mod.LevainHands))
    host = ChatHost({"alpha": _entity(tmp_path, "alpha")})
    # Automatic collection off and no collect() here: the SDK holds the failure in a reference
    # cycle, so only the HOST's own collection can release the hands (L1 review, run).
    gc.disable()
    try:
        out = host.open("alpha")
        st = _wait(host, out["job_id"], 60)
        assert st["status"] == "failed" and "injected" in st["error"]
        assert built, "the injection never ran: the test would grade nothing"
        assert [d for d in built if os.path.exists(d)] == []
    finally:
        gc.enable()
        host.shutdown()


# -- the turn's wall-clock bound and the counted teardown (K1p2 follow-ons, 2026-10-03) ------------


class _Stoppable(_Stub):
    """run_turn blocks until request_stop, then returns the timed-out result a session would."""

    def __init__(self, on_event):
        super().__init__(on_event, [])
        self.stop = threading.Event()
        self.stops = 0

    def run_turn(self, message):
        self.on_event("⚙ terminal: sleep")
        assert self.stop.wait(5), "the deadline never asked the turn to stop"
        return _Result(reply=None, error="stopped at its wall-clock bound", timed_out=True)

    def request_stop(self):
        self.stops += 1
        self.stop.set()


def test_a_turn_past_its_deadline_is_stopped_and_its_session_released(tmp_path):
    made: list[_Stoppable] = []

    def factory(entity_dir, *, on_event):
        made.append(_Stoppable(on_event))
        return made[-1]

    host = ChatHost({"alpha": tmp_path / "alpha"}, session_factory=factory, turn_seconds=0.2)
    sid = _opened(host)
    st = _wait(host, host.turn(sid, "long")["job_id"])
    assert st["deadline_hit"] is True and st["result"]["timed_out"] is True
    assert host.session_status(sid)["state"] == "broken" and made[0].closed
    assert made[0].stops >= 1


def test_a_turn_inside_its_deadline_is_not_marked(tmp_path):
    host = _host(tmp_path, _Factory([_Result()]), turn_seconds=30)
    sid = _opened(host)
    st = _wait(host, host.turn(sid, "quick")["job_id"])
    assert st["deadline_hit"] is False and host.session_status(sid)["state"] == "idle"


class _SlowClose(_Stub):
    def __init__(self, on_event, release: threading.Event):
        super().__init__(on_event, [])
        self.release = release

    def close(self):
        assert self.release.wait(5)
        self.closed = True


def test_a_session_being_closed_still_holds_its_slot(tmp_path):
    """codex L3 r2 (MED), reproduced here: close published `closed` before its teardown, so a slow
    teardown let an open exceed max_sessions."""
    release = threading.Event()
    made: list[_Stub] = []

    def factory(entity_dir, *, on_event):
        made.append(_SlowClose(on_event, release) if not made else _Stub(on_event, []))
        return made[-1]

    host = ChatHost({"alpha": tmp_path / "alpha", "beta": tmp_path / "beta"},
                    session_factory=factory, max_sessions=1)
    sid = _opened(host)
    closer = threading.Thread(target=host.close, args=(sid,))
    closer.start()
    deadline = time.monotonic() + 5
    while host.session_status(sid)["state"] != "closing":
        assert time.monotonic() < deadline, host.session_status(sid)
        time.sleep(0.01)
    with pytest.raises(ChatError) as e:
        host.open("beta")
    assert e.value.http_status == 429
    with pytest.raises(ChatError) as e:
        host.close(sid)
    assert e.value.http_status == 409
    release.set()
    closer.join(5)
    assert host.session_status(sid)["state"] == "closed" and made[0].closed
    _opened(host, "beta")


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf")])
def test_a_turn_bound_that_would_never_fire_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        _host(tmp_path, _Factory([]), turn_seconds=bad)


@pytest.mark.parametrize("text", ["0", "-5", "nan", "inf", "soon"])
def test_serve_refuses_a_turn_seconds_that_would_never_fire(text, capsys):
    from levain.cli import main

    with pytest.raises(SystemExit) as e:
        main(["serve", "--turn-seconds", text])
    assert e.value.code == 2
