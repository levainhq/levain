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
    remaining = {s["session_id"] for s in host.listing()["sessions"]}
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
    try:
        out = host.open("alpha")
        st = _wait(host, out["job_id"], 60)
        assert st["status"] == "failed" and "injected" in st["error"]
        assert built, "the injection never ran: the test would grade nothing"
        gc.collect()
        assert [d for d in built if os.path.exists(d)] == []
    finally:
        host.shutdown()
