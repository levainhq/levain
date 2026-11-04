"""P7-1 S10b: the dispatch run server (levain.slot.services.dispatch_run).

The server runs in-process over socketpairs, driven by the REAL operator-side client (``session_client``), the REAL
``RealLauncher`` over the S4 fakes, and the REAL point-of-use check against a real signed authority log (the S10a rig).

Held: Q1 (final, Phill 11-04 "8a"): a valid enrolled signature over the link naming the spec hash, re-judged against the
server's own replay, plus exposure inside the link's budgets, at each fire; peer-uid refusal; the pre-mint pool check;
holder EOF ENDs exactly once; END async and idempotent; USAGE by the record; the bound usage readers; the plist golden.

What no test here holds (UNVERIFIED until the VM rows): the unit under launchd as ``_levain_dispatch``, real AF_UNIX
peer credentials across two uids, the dispatch uid's read access to the cockpit ledger and the agent store, a real run.
"""
from __future__ import annotations

import dataclasses
import json
import plistlib
import shutil
import socket
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import session_client as C
from levain.agents import session_launcher as L
from levain.agents.accounting import ActionExposure
from levain.slot.services import dispatch_run as D
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import EXP, _occurrence, _reservation, _run
from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
from tests.test_agents_session_reaper_stop import _session_row

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


class BlockingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.entered = 0

    def end(self, session):
        self.entered += 1
        self.gate.wait(10)
        return super().end(session)


class Env:
    def __init__(self, rig, tmp_path, *, runtime=None, pool=True, peer=True, bar=None, **server_kw):
        self.rig = rig
        self.control, self.runtime = FakeControl(), runtime or FakeRuntime()
        self.launcher = L.RealLauncher(self.control, self.runtime, FakeSources(), L.SessionIndex(tmp_path / "idx"))
        self.bar_of = bar or D.agent_store_bar(rig.store.path)
        self.verified = 0
        self.pool, self.peer = pool, peer
        self.server = D.RunServer(self.launcher, verify=self._verify, pool_free=lambda: self.pool,
                                  peer_ok=lambda s: self.peer, holder_idle_s=20, **server_kw)
        self.threads: list[threading.Thread] = []

    def _verify(self, proof, exposure, claims=None):
        self.verified += 1
        D.verify_point_of_use(proof, exposure, replay=self.rig.authority.replay, now=self.rig.now, bar_of=self.bar_of)

    def connect(self):
        a, b = socket.socketpair()
        t = threading.Thread(target=self.server.handle, args=(b,), daemon=True)
        t.start()
        self.threads.append(t)
        return a

    def client(self):
        r = self.rig
        return C.DispatchSessionLauncher(self.connect, lambda occ: C.link_proof(r.store, r.world.root, occ, now=r.now()))

    def drain(self):
        for t in self.threads:
            t.join(10)


@pytest.fixture
def env(rig, tmp_path):
    return Env(rig, tmp_path)


def _proof(rig, oid):
    return C.link_proof(rig.store, rig.world.root, oid, now=rig.now())


def _check(rig, proof, exposure=EXP, **kw):
    D.verify_point_of_use(proof, exposure, replay=rig.authority.replay, now=kw.pop("now", rig.now),
                          bar_of=kw.pop("bar_of", D.agent_store_bar(rig.store.path)), **kw)


# --- Q1: the one function ---------------------------------------------------------------------------------------------

def test_a_genuine_link_passes_the_point_of_use_check(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    assert _check(rig, _proof(rig, oid)) is None


@pytest.mark.parametrize("fault,name", [
    ("spec", "link-not-standing"), ("store", "link-not-standing"), ("record", "link-proof-mismatch"),
    ("signature", "link-signature-invalid"), ("seq", "link-proof-mismatch"), ("expired", "link-not-live"),
    ("calls", "exposure-over-link"), ("bytes", "exposure-over-link"), ("nobytes", "exposure-over-link"),
    ("barred", "agent-barred"), ("barunreadable", "bar-unreadable"), ("ledger", "authority-unreadable")])
def test_a_proof_or_standing_that_does_not_hold_at_the_point_of_use_is_refused(rig, fault, name):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    proof, exposure, kw = _proof(rig, oid), EXP, {}
    if fault == "spec":
        proof = dataclasses.replace(proof, spec_hash="0" * 64)
    elif fault == "store":
        proof = dataclasses.replace(proof, store_id="other")
    elif fault == "record":
        proof = dataclasses.replace(proof, born=dataclasses.replace(proof.born, record=proof.born.record + " "))
    elif fault == "signature":
        sig = proof.born.signature
        proof = dataclasses.replace(proof, born=dataclasses.replace(proof.born, signature=sig.replace("A", "B", 3)))
    elif fault == "seq":
        proof = dataclasses.replace(proof, current=dataclasses.replace(proof.current, seq=proof.current.seq + 1))
    elif fault == "expired":
        kw["now"] = lambda: rig.now(timedelta(days=400))
    elif fault == "calls":
        exposure = ActionExposure(21, "tool_calls", 3600, 5_000_000)
    elif fault == "bytes":
        exposure = ActionExposure(20, "tool_calls", 3600, 5_000_001)
    elif fault == "nobytes":
        exposure = ActionExposure(20, "tool_calls", 3600, None)
    elif fault == "barred":
        kw["bar_of"] = lambda agent: (True, 0)
    elif fault == "barunreadable":
        kw["bar_of"] = D.agent_store_bar(rig.store.path.with_name("absent.db"))
    elif fault == "ledger":
        kw["bar_of"] = lambda agent: (False, 0)
        with pytest.raises(L.LauncherRefused) as exc:
            D.verify_point_of_use(proof, exposure, replay=lambda: (_ for _ in ()).throw(OSError("gone")), now=rig.now,
                                  bar_of=kw["bar_of"])
        assert exc.value.name == name and exc.value.reason_class == "transient"
        return
    with pytest.raises(L.LauncherRefused) as exc:
        _check(rig, proof, exposure, **kw)
    assert exc.value.name == name


def test_the_check_is_one_named_function_called_before_the_spawn_and_again_before_the_release():
    text = (Path(D.__file__)).read_text()
    assert text.count("def verify_point_of_use(") == 1 and text.count("self._verify(") == 2
    launch = text[text.index('if op == "LAUNCH":'):]
    assert launch.index("self._verify(") < launch.index("self._pool()") < launch.index("self.launcher.spawn(")
    release = text[text.index('if op == "RELEASE":'):]
    assert release.index("self._verify(") < release.index("mine.child.release()")


# --- over the wire ------------------------------------------------------------------------------------------------------------

def test_run_session_through_client_and_server_ends_one_occurrence_done(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    _sup, report = _run(rig, oid, env.client(), exposure=EXP)
    assert (report.outcome, report.state, report.code) == ("exited", "done", 0), report
    assert env.verified == 2 and len(env.control.minted) == 1      # judged at LAUNCH and again at RELEASE
    res = _reservation(rig, oid)
    assert res[0] == "settled" and res[2] == 7 and res[3] == 1000 and res[4] == 30     # USAGE keyed by the record


def test_a_launch_whose_proof_fails_is_refused_before_anything_is_minted(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    env.bar_of = lambda agent: (True, 0)            # a revoke opens between the fire's own check and the server's
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000a1", "claude", "sonnet", "b", "passthrough", EXP))
    assert exc.value.name == "agent-barred"
    assert env.control.minted == {} and env.runtime.starts == []


def test_no_free_slot_is_a_transient_refusal_before_the_mint(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    env.pool = False
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000a2", "claude", "sonnet", "b", "passthrough", EXP))
    assert (exc.value.name, exc.value.reason_class) == ("no-free-slot", "transient")
    assert env.control.minted == {}


def test_a_peer_that_is_not_the_operator_is_closed_unanswered(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    env.peer = False
    child = env.client().spawn(S.SessionLaunch(oid, "s-00000000000000a3", "claude", "sonnet", "b", "passthrough", EXP))
    assert child.process.poll() == ctl.GATE_UNPROVEN and env.verified == 0 and env.control.minted == {}


def test_an_op_before_launch_and_a_foreign_protocol_are_refused_and_closed(env):
    for frame, name in (({"p": C.PROTOCOL, "op": "RELEASE", "id": 1}, "bad-request"),
                        ({"p": "levain-run/9", "op": "USAGE", "id": 1}, "protocol-mismatch")):
        sock = env.connect()
        sock.sendall(C.encode_run_frame(frame))
        ans = C.read_run_frame(sock)
        assert ans["ok"] is False and ans["refused"] == name
        assert C.read_run_frame(sock) is None
        sock.close()


@pytest.mark.parametrize("released", [False, True])
def test_a_holder_that_vanishes_ends_the_session_exactly_once(rig, env, released):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b1", "claude", "sonnet", "b", "passthrough", EXP))
    if released:
        child.release()
    child._conn.close()                              # the fire dies: EOF on the holder
    env.drain()
    assert env.runtime.ended == ["sess-1"]


def test_end_is_async_and_idempotent_two_ends_one_teardown(rig, tmp_path):
    runtime = BlockingRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key = _session_row(rig)
    env.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    client = env.client()
    first = client.end_session(key, 0.05)
    second = client.end_session(key, 0.05)
    assert first.terminated is False and "in progress" in first.detail and second.terminated is False
    runtime.gate.set()
    third = client.end_session(key, 5)
    assert third.terminated is True and client.end_session(key, 1).terminated is True
    assert runtime.ended == ["sess-1"]


def test_a_link_that_dies_between_launch_and_release_is_refused_at_release_and_ended(rig, env):
    """codex HIGH r1 :339: the authority is re-judged at the point the run actually starts, not only at the mint."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, "s-00000000000000c1", "claude", "sonnet", "b", "passthrough", EXP))
    env.bar_of = lambda agent: (True, 0)            # a revoke lands while the slot is prepared
    with pytest.raises(RuntimeError, match="agent-barred"):
        child.release()
    assert env.verified == 2 and env.runtime.children[0].released is False and env.runtime.ended == ["sess-1"]
    child.abort()                                    # start_run's abort after a refused release
    child._conn.close()
    env.drain()
    assert env.runtime.children[0].released is False and env.runtime.ended == ["sess-1"]


def test_holders_cannot_take_the_permits_usage_and_end_need(rig, tmp_path):
    """codex MED r1 :266: holder connections are capped below the connection cap."""
    env = Env(rig, tmp_path, max_connections=3, max_holders=2)
    held = []
    oid = _occurrence(rig, billing="subscription", exposure=EXP)      # one occurrence, distinct session keys
    for n in range(2):
        held.append(env.client().spawn(S.SessionLaunch(oid, f"s-00000000000000e{n}", "claude", "sonnet", "b",
                                                       "passthrough", EXP)))
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000e9", "claude", "sonnet", "b", "passthrough", EXP))
    assert (exc.value.name, exc.value.reason_class) == ("no-holder-capacity", "transient")
    sock = env.connect()
    sock.sendall(C.encode_run_frame({"p": C.PROTOCOL, "op": "USAGE", "id": 1, "session_key": "s-00000000000000ff"}))
    ans = C.read_run_frame(sock)
    assert ans is not None and ans["ok"] is True
    sock.close()
    for child in held:
        child._conn.close()
    env.drain()


def test_a_proven_end_is_never_evicted_and_never_rerun(rig, tmp_path, monkeypatch):
    """codex MED r1 :397: a proven END answers every repeat, however many other keys were ENDed since."""
    monkeypatch.setattr(D, "_JOBS_KEEP", 4)
    env = Env(rig, tmp_path)
    oid, key = _session_row(rig)
    env.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    monkeypatch.setattr(env.launcher, "_mark_ended", lambda k, **kw: False)   # the `ended` write fails: only the job knows
    client = env.client()
    assert client.end_session(key, 5).terminated is True
    for n in range(6):
        assert client.end_session(f"s-00000000000001{n:02x}", 1).terminated is False      # unrecorded keys, unproven
    env.runtime.end_ok = False                       # a second teardown after SCRUB would not prove again
    assert client.end_session(key, 5).terminated is True
    assert env.runtime.ended == ["sess-1"]


def test_shutdown_ends_every_live_session_and_refuses_new_launches(rig, env):
    """complement MED r1: SIGTERM must not orphan a held session (daemon handler threads never run their finally)."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, "s-00000000000000f1", "claude", "sonnet", "b", "passthrough", EXP))
    child.release()
    env.server.shutdown(5)
    assert env.runtime.ended == ["sess-1"]
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid,"s-00000000000000f2", "claude", "sonnet", "b", "passthrough", EXP))
    assert (exc.value.name, exc.value.reason_class) == ("server-stopping", "transient")
    child._conn.close()
    env.drain()
    assert env.runtime.ended == ["sess-1"]
    text = Path(D.__file__).read_text()
    body = text[text.index("def main("):]
    assert body.index("server.shutdown(") < body.index("close()")


class GatedStartRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.gate, self.entered = threading.Event(), threading.Event()

    def start(self, req):
        self.entered.set()
        self.gate.wait(10)
        return super().start(req)


def test_a_spawn_in_flight_at_shutdown_is_registered_first_and_the_deadline_is_absolute(rig, tmp_path):
    """codex MED r2 :502 / finding 5: the spawn is tracked BEFORE it blocks, shutdown returns by its deadline with it
    still unproven on the record (the reaper's), and the spawn ENDs itself when it lands."""
    runtime = GatedStartRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key, got = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000a5", {}

    def fire():
        try:
            env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
        except L.LauncherRefused as exc:
            got["exc"] = exc
    t = threading.Thread(target=fire, daemon=True)
    t.start()
    assert runtime.entered.wait(5)
    assert key in env.server._live                   # registered before the blocking spawn
    t0 = time.monotonic()
    env.server.shutdown(0.3)
    assert time.monotonic() - t0 < 2.0
    assert env.launcher.index.get(key)["state"] != "ended"
    runtime.gate.set()
    t.join(10)
    assert got["exc"].name == "server-stopping" and runtime.ended == ["sess-1"]
    env.drain()


def test_shutdown_returns_by_its_deadline_when_holder_teardowns_block(rig, tmp_path):
    """complement/deepseek MED r2: a blocking ABORT/END per holder must not push shutdown past launchd's 20 s."""
    runtime = BlockingRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    keys = ["s-00000000000000b7", "s-00000000000000b8"]
    held = [env.client().spawn(S.SessionLaunch(oid, k, "claude", "sonnet", "b", "passthrough", EXP)) for k in keys]
    t0 = time.monotonic()
    env.server.shutdown(0.5)                         # never released: each is ABORTed, and END blocks
    assert time.monotonic() - t0 < 2.0
    assert all(env.launcher.index.get(k)["state"] != "ended" for k in keys)
    runtime.gate.set()
    for _ in range(200):
        if all(env.launcher.index.get(k)["state"] == "ended" for k in keys):
            break
        time.sleep(0.02)
    for child in held:
        child._conn.close()
    env.drain()
    assert all(env.launcher.index.get(k)["state"] == "ended" for k in keys)


def test_an_unproven_end_after_a_refused_release_stays_registered_and_shutdown_retries_it(rig, env):
    """codex MED r2 :465: an END that does not prove keeps the session registered (no retry loop here: shutdown and
    the reaper's END are the retries)."""
    oid, key = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000c2"
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    env.runtime.end_ok = False
    env.bar_of = lambda agent: (True, 0)
    with pytest.raises(RuntimeError, match="agent-barred"):
        child.release()
    job = env.server._jobs[key]
    assert job.done.wait(5) and job.result.terminated is False
    assert key in env.server._live
    env.runtime.end_ok = True
    env.server.shutdown(5)
    assert env.launcher.index.get(key)["state"] == "ended" and key not in env.server._live
    child._conn.close()
    env.drain()


def test_a_holder_eof_and_a_reaper_end_on_one_session_make_one_teardown(rig, tmp_path):
    """complement MED r1: the holder's END and a short-connection END are one job, never two concurrent teardowns."""
    runtime = BlockingRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    key = "s-00000000000000d1"
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    child.release()
    assert env.client().end_session(key, 0.05).terminated is False      # the reaper's END is in flight
    child._conn.close()                                                  # and the fire dies at the same moment
    for _ in range(50):
        if runtime.entered >= 2:
            break
        threading.Event().wait(0.01)
    runtime.gate.set()
    env.drain()
    assert runtime.ended == ["sess-1"] and runtime.entered == 1


def test_usage_by_an_unknown_key_is_the_caps(env):
    got = env.client().usage("s-00000000000000ff")
    assert got.actions is None and got.tunnel_bytes is None and got.seconds is None


# --- the bound readers ------------------------------------------------------------------------------------------------------

def test_the_bound_usage_readers_define_seconds_and_scan_every_slots_log(tmp_path, monkeypatch):
    from levain.qbroker import operator as op
    from levain.slot.runtime_helper import FRONT_LOG_NAME
    store = tmp_path / "L"
    for n, session in ((0, "s-aaaaaaaaaaaaaaaa"), (1, "s-bbbbbbbbbbbbbbbb")):
        d = store / "gw" / f"slot{n}"
        d.mkdir(parents=True)
        (d / FRONT_LOG_NAME).write_text(json.dumps({"event": "tunnel", "session": session, "passthrough_total": 100 + n}) + "\n")
    idx = L.SessionIndex(tmp_path / "idx")
    idx.put("s-0000000000000001", "ended", session="s-bbbbbbbbbbbbbbbb", run_id="r-1", started_s=100.0, ended_s=161.2)
    idx.put("s-0000000000000002", "started", session="s-cccccccccccccccc", run_id="r-2", started_s=100.0)
    monkeypatch.setattr(op, "run_state", lambda root, run_id: {"known": True, "calls": 9} if root == store / "queue" else None)
    src = D.bound_usage_sources(queue_root=store / "queue", store=store, index_dir=tmp_path / "idx")
    assert src.tunnel_bytes("s-bbbbbbbbbbbbbbbb") == 101 and src.tunnel_bytes("s-aaaaaaaaaaaaaaaa") == 100
    assert src.tunnel_bytes("s-cccccccccccccccc") is None
    assert src.seconds("s-bbbbbbbbbbbbbbbb") == 62                 # 61.2 rounded UP
    assert src.seconds("s-cccccccccccccccc") is None and src.seconds("s-unknown0000000000") is None
    assert src.calls("r-1") == 9


# --- the unit and the process -------------------------------------------------------------------------------------------------

def test_the_unit_row_renders_a_dispatch_uid_keepalive_plist_golden():
    from levain.slot import units
    svc = D.service_row()
    plist = plistlib.loads(units.launchd_plist_text(svc).encode())
    assert plist["UserName"] == "_levain_dispatch" and plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["Label"] == "com.levain.dispatch_run" and plist["ProgramArguments"][-1] == "dispatch_run"
    assert units.program_relpath(svc) == "levain/slot/services/dispatch_run.py"
    assert svc.name not in {s.name for s in units.SERVICES}         # registration is the head's (CONTRACT 1.3 / 2.2)


def test_the_program_refuses_to_run_as_anyone_but_the_dispatch_account(capsys):
    assert D.main([]) in (D.EX_CONFIG, D.EX_WRONG_USER)


def test_the_registered_launcher_answers_end_session_through_to_the_real_server(rig, env):
    """S11b needs ``end_session`` on the launcher the install registers: the S10a client has it, ``stop`` is the server's."""
    from levain.agents import session_wire as W
    from tests.test_agents_session_reaper_stop import DEAD, _executor
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=C.LAUNCHER_NAME)
    got = W.resolve_launcher(rig.store, W.read_install(rig.store.directory))
    assert callable(getattr(got, "end_session", None)) and isinstance(got, S.SessionEnder) and not hasattr(got, "stop")
    got._connect = env.connect                       # the production socket path, swapped for the in-process server
    oid, key = _session_row(rig)
    env.launcher.spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    assert _executor(rig, oid, got, DEAD).stop(10).terminated is True
    assert env.runtime.ended == ["sess-1"]
