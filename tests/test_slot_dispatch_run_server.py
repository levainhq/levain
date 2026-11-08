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
from levain.cockpit.consent import utc_stamp
from levain.slot.services import dispatch_run as D
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import EXP, _occurrence, _reservation, _run
from tests.test_agents_session_launcher import FakeControl, FakeRuntime, FakeSources
from tests.test_agents_session_reaper_stop import _session_row
import levain.authority as auth
from tests.test_agents_authority_replica import NO_BAR, Wire, _source
from tests.test_consent_queue import Key
from tests.test_authority_agent_link import revoke_agent

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
        self.billing = "subscription"        # the launcher's billing the point of use judges against
        self.pool, self.peer = pool, peer
        self.server = D.RunServer(self.launcher, verify=self._verify, pool_free=lambda: self.pool,
                                  peer_ok=lambda s: self.peer, holder_idle_s=20, **server_kw)
        self.threads: list[threading.Thread] = []

    def _verify(self, proof, exposure, claims=None):
        self.verified += 1
        return D.verify_point_of_use(proof, exposure, replay=self.rig.authority.replay, now=self.rig.now,
                                     bar_of=self.bar_of, billing=self.billing)

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
    _grant(rig, proof, exposure, **kw)


def _grant(rig, proof, exposure=EXP, **kw):
    return D.verify_point_of_use(proof, exposure, replay=rig.authority.replay, now=kw.pop("now", rig.now),
                                 bar_of=kw.pop("bar_of", D.agent_store_bar(rig.store.path)),
                                 billing=kw.pop("billing", "subscription"), **kw)


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
                                  bar_of=kw["bar_of"], billing="subscription")
        assert exc.value.name == name and exc.value.reason_class == "transient"
        return
    with pytest.raises(L.LauncherRefused) as exc:
        _check(rig, proof, exposure, **kw)
    assert exc.value.name == name


def test_the_check_is_one_named_function_called_before_the_spawn_and_again_before_the_release():
    text = (Path(D.__file__)).read_text()
    # the third call is the SYNC re-judge of a running session (levain 34, D2): the same verify, never a second authority
    assert text.count("def verify_point_of_use(") == 1 and text.count("self._verify(") == 3
    rejudge = text[text.index("def _rejudge("):]
    assert rejudge.index("self._verify(h.proof, h.exposure, claims)") < rejudge.index("self._end(h.key, 0.0)")
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
    # the refused connection keeps its connection permit until its handler sees the client's close (a refused LAUNCH
    # leaves the connection usable, so the server cannot free it at the answer): wait for that handler to end, or the
    # USAGE below can meet a full cap (flaked 1 in 4 before this join)
    env.threads[-1].join(10)
    assert not env.threads[-1].is_alive()
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
    for _ in range(250):                              # shutdown returns when the retry job is done; _run_end drops the holder
        # from _live only AFTER it sets done (and shutdown's own wait is on done), so the settled state is the drop itself
        if key not in env.server._live:
            break
        time.sleep(0.02)
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


@pytest.mark.parametrize("proven", [False, True])
def test_an_end_between_launch_and_release_makes_release_refuse_and_start_nothing(rig, tmp_path, proven):
    """codex HIGH bundle L3 (dispatch_run.py:471): a short-connection END after LAUNCH and before RELEASE (teardown
    running, or already proven) must make RELEASE refuse by name; the gated child is never released."""
    runtime = BlockingRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000d7"
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    if proven:
        runtime.gate.set()
        assert env.client().end_session(key, 5).terminated is True
    else:
        assert env.client().end_session(key, 0.05).terminated is False      # the END is in flight
        # the refused RELEASE runs the holder's cleanup, whose ABORT takes the key's stripe the in-flight END holds:
        # let that END finish shortly after the RELEASE arrives (it was marked ending before this point)
        threading.Timer(0.3, runtime.gate.set).start()
    with pytest.raises(RuntimeError, match="session-ended"):
        child.release()
    assert env.runtime.children[0].released is False
    runtime.gate.set()
    child._conn.close()
    env.drain()
    assert env.runtime.children[0].released is False and runtime.ended == ["sess-1"]


def test_an_end_while_the_launch_is_still_spawning_makes_release_refuse(rig, tmp_path):
    """The same ordering one step earlier: an END that lands while the LAUNCH's spawn is in flight (the holder is
    registered first) marks it ending, so the RELEASE that follows refuses and the child is never released."""
    runtime = GatedStartRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key, got = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000d8", {}

    def fire():
        got["child"] = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    t = threading.Thread(target=fire, daemon=True)
    t.start()
    assert runtime.entered.wait(5) and key in env.server._live
    assert env.client().end_session(key, 0.05).terminated is False      # nothing to prove yet: answered unproven
    runtime.gate.set()
    t.join(10)
    with pytest.raises(RuntimeError, match="session-ended"):
        got["child"].release()
    assert env.runtime.children[0].released is False
    got["child"]._conn.close()
    env.drain()
    assert env.runtime.children[0].released is False and runtime.ended == ["sess-1"]


def test_an_end_during_the_spawn_then_a_refused_release_aborts_and_ends_with_the_holder_still_open(rig, tmp_path):
    """L3 fixrel r1 (gemini MED): the END that lands during the spawn finishes unproven before the child exists; the
    RELEASE it makes refuse must run the holder's cleanup itself (ABORT, then END), not leave the gated child alive
    until the holder's EOF. The holder connection stays OPEN here."""
    runtime = GatedStartRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key, got = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000da", {}

    def fire():
        got["child"] = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    t = threading.Thread(target=fire, daemon=True)
    t.start()
    assert runtime.entered.wait(5) and key in env.server._live
    assert env.client().end_session(key, 0.5).terminated is False      # finished unproven: no session recorded yet
    runtime.gate.set()
    t.join(10)
    with pytest.raises(RuntimeError, match="session-ended"):
        got["child"].release()
    for _ in range(250):                              # no EOF: the holder's socket is still open
        # the END job writes the index record BEFORE it sets done and drops the holder from _live (_run_end), so the
        # settled state is both: waiting on the record alone reads _live in the gap between the two
        if env.launcher.index.get(key)["state"] == "ended" and key not in env.server._live:
            break
        time.sleep(0.02)
    inner = env.runtime.children[0]
    assert inner.released is False and inner.aborted == 1 and runtime.ended == ["sess-1"]
    assert env.launcher.index.get(key)["state"] == "ended" and key not in env.server._live
    got["child"]._conn.close()
    env.drain()
    assert inner.aborted == 1 and runtime.ended == ["sess-1"]


def test_a_short_end_answers_within_its_deadline_while_a_holder_abort_blocks(rig, tmp_path):
    """L3 fixrel r1 (complement LOW-MED :708): the holder's cleanup holds its lock across a slow ABORT; a short END
    with a deadline must not wait on that lock past its deadline. It marks the holder ending and answers unproven."""
    env = Env(rig, tmp_path)
    oid, key = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000db"
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    inner = env.runtime.children[0]
    entered, gate = threading.Event(), threading.Event()

    def slow_abort(timeout=5.0):
        entered.set()
        gate.wait(10)
        inner.aborted += 1
        return 0
    inner.abort = slow_abort
    child._conn.close()                               # EOF: the cleanup takes the holder lock and ABORTs (blocks)
    assert entered.wait(5)
    t0 = time.monotonic()
    got = env.client().end_session(key, 0.2)
    elapsed = time.monotonic() - t0
    holder = env.server._live[key]
    gate.set()
    env.drain()
    assert got.terminated is False and elapsed < 1.5, (got, elapsed)
    assert holder.ending is True
    for _ in range(250):
        if env.launcher.index.get(key)["state"] == "ended":
            break
        time.sleep(0.02)
    assert inner.aborted == 1 and inner.released is False and env.runtime.ended == ["sess-1"]


class SlowJobRuntime(FakeRuntime):
    """The first END (the cleanup's ABORT settle) answers unproven at once; every later one (the key's END job) blocks
    on ``gate``: a refused RELEASE whose cleanup is fast but whose END job is slow."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def end(self, session):
        if self.ended:
            self.gate.wait(30)
            return super().end(session)
        self.ended.append(session)
        return False


def test_a_release_refused_at_the_point_of_use_answers_by_name_inside_the_clients_release_timeout(rig, tmp_path):
    """C1/C2 (fixrel2 open item): the verify-refusal path waited ``HOLDER_END_WAIT_S`` (10 s) for the END job, the same
    as the client's RELEASE socket timeout, so a slow END turned the named refusal into 'unreachable'. The wait is
    now :data:`D.RELEASE_REFUSED_WAIT_S`, below the client's timeout; the END runs on and the EOF cleanup joins it."""
    runtime = SlowJobRuntime()
    env = Env(rig, tmp_path, runtime=runtime)
    oid, key = _occurrence(rig, billing="subscription", exposure=EXP), "s-00000000000000dc"
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    env.bar_of = lambda agent: (True, 0)            # a revoke lands while the slot is prepared
    t0 = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as exc:
            child.release()
        elapsed = time.monotonic() - t0
        assert "refused RELEASE: agent-barred" in str(exc.value), exc.value
        assert elapsed < C._CONNECT_S - 1, elapsed
        assert env.server._jobs[key].done.is_set() is False        # the END job runs on past the answer
    finally:
        runtime.gate.set()
    child._conn.close()
    env.drain()
    assert env.runtime.children[0].released is False and env.launcher.index.get(key)["state"] == "ended"
    assert D.RELEASE_REFUSED_WAIT_S < C._CONNECT_S


def test_an_end_before_the_holder_registers_answers_a_named_unproven_inside_its_deadline(rig, env):
    """C1/C2 (fixrel2 open item, decided as the documented edge): an END that arrives strictly before the key's LAUNCH
    answers unproven by name within its deadline, never 'ended', and leaves nothing behind that refuses the LAUNCH."""
    key = "s-00000000000000dd"
    t0 = time.monotonic()
    got = env.client().end_session(key, 2)
    assert time.monotonic() - t0 < 2
    assert got.terminated is False and "nothing observed" in got.detail, got
    assert key not in env.server._live
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    child = env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    child.release()                                   # the earlier END marked nothing: the reaper's next END is the retry
    assert env.client().end_session(key, 5).terminated is True and env.runtime.ended == ["sess-1"]
    child._conn.close()
    env.drain()


def test_usage_by_an_unknown_key_is_the_caps(env):
    got = env.client().usage("s-00000000000000ff")
    assert got.actions is None and got.tunnel_bytes is None and got.seconds is None


# --- levain 34, D2: a SYNC re-judges the live holders --------------------------------------------------------------------
#
# A session has no point of use after its RELEASE, so a revoke SYNCed into the replica while it ran used to be enforced
# only by the operator-uid tick. Each case drives the production shape (``Wire``: a replica-backed ``replica_verifier``,
# raw frames, SYNC over the wire) with a real signed ledger; the revoke is signed by the operator and SYNCed by "another
# fire". Cases 1-5 of the design (``1105-3/DESIGN_dispatch_lease_revoke.md`` section 4); case 6 is in
# ``test_agents_tick_driver.py`` and the lease-level twin of case 1 in ``test_upgrade_dispatch_end.py``.

def _until(pred, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _rejudge_idle(server) -> bool:
    """True once the SYNC re-judge worker has finished its pass (it is flagged running before a SYNC answers)."""
    return _until(lambda: not server._rejudge_running)


@pytest.fixture
def wire(rig, tmp_path):
    w = Wire(rig, tmp_path)
    yield w
    w.close()


def _released(rig, wire, key="s-00000000000000b1", **occ):
    """A synced, launched and RELEASED session of a fresh subscription link; returns (oid, holder socket). ``occ``
    goes to ``_occurrence`` (a second session needs another spec ``name``, or its claim is a duplicate)."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP, **occ)
    assert wire.sync()["ok"] is True
    sock = wire.conn()
    assert wire.launch(sock, oid, key=key)["ok"] is True
    assert wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)["ok"] is True
    assert wire.runtime.children[0].released is True and wire.runtime.ended == []
    return oid, sock


def _ended(wire, key="s-00000000000000b1") -> bool:
    # the holder stays registered while its connection is open (a short-connection END proves without its cleanup)
    return _until(lambda: wire.launcher_index(key).get("state") == "ended")


def _count_verifies(wire):
    calls = []
    real = wire.server._verify

    def counting(proof, exposure, claims=None):
        calls.append(1)
        return real(proof, exposure, claims)
    wire.server._verify = counting
    return calls


def _sign(rig, oid, how):
    """The operator signs the revoke ``how`` of the session's link, signed so that the session's link is gone."""
    link_id = f"al-{rig.n:012x}"                       # _occurrence's link is the rig's latest
    if how == "agent":
        rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid), "laptop")
    elif how == "grant":
        rig.world.link({"op": "revoke_grant", "grant_id": link_id}, "laptop")
    else:       # the born signer's key (the last laptop-presence key may not go alone): a second one is enrolled, then it signs
        second = Key(rig.world.tmp / "keys", "laptop2")
        rig.world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
        rig.world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key,
                        "class": "laptop-presence", "custody": "passphrase"}, "laptop")
        rig.world.link({"op": "revoke_key", "fingerprint": rig.world.fp("laptop")}, "laptop2")


@pytest.mark.parametrize("how", ["agent", "grant", "key"])
def test_a_revoke_synced_while_a_session_runs_ends_it_by_the_replicas_own_judgement(rig, wire, how):
    """Design cases 1 and 2: revoke_agent, revoke_grant of the link and revoke_key of its born signer. Nothing but the
    SYNC touches the run server after the RELEASE, so only the re-judge can end the session."""
    oid, sock = _released(rig, wire)
    _sign(rig, oid, how)
    assert wire.sync()["ok"] is True                                  # the answer is not the re-judge's
    assert _ended(wire), "the SYNCed revoke ended nothing"
    assert wire.runtime.ended == ["sess-1"] and len(wire.server._jobs) == 1
    sock.close()


def test_a_revoke_synced_before_the_release_makes_the_pending_release_refuse(rig, wire):
    """The re-judge also covers a holder not yet released: its END marks ``ending``, so the RELEASE refuses by name."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    assert wire.sync()["ok"] is True
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    mine = wire.head()
    _sign(rig, oid, "agent")
    assert wire.sync()["ok"] is True
    assert _ended(wire)
    gone = wire.call(sock, "RELEASE", ledger_head=mine, bar=NO_BAR)
    # the RELEASE's own verify refuses first (the revoked link): this case proves the RELEASE starts nothing; the case
    # below proves the ``ending`` mark alone blocks it (complement L3 r1, tests)
    assert gone["ok"] is False and gone["refused"] == "link-not-standing", gone
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    sock.close()


def test_an_end_begun_by_the_rejudge_blocks_a_release_whose_own_verify_passes(rig, wire):
    """The ``ending`` mark, alone: the re-judge refuses (standing) while the RELEASE's own verify passes, so only the
    END the re-judge began can make the RELEASE refuse, and it does, by ``session-ended``."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    assert wire.sync()["ok"] is True
    sock = wire.conn()
    assert wire.launch(sock, oid)["ok"] is True
    real = wire.server._verify

    def verify(proof, exposure, claims=None):
        if claims is not None and claims.rejudge:
            raise L.LauncherRefused("link-not-standing", "the re-judge's verdict only")
        return real(proof, exposure, claims)
    wire.server._verify = verify
    assert wire.sync()["ok"] is True
    assert _ended(wire)
    gone = wire.call(sock, "RELEASE", ledger_head=wire.head(), bar=NO_BAR)
    assert (gone["ok"], gone.get("refused")) == (False, "session-ended"), gone
    assert wire.runtime.children[0].released is False and wire.runtime.ended == ["sess-1"]
    sock.close()


def test_a_restored_or_forked_ledger_after_a_synced_revoke_cannot_un_end_it_and_ends_it_once(rig, wire):
    """Design case 3 (the C5 case): the revoke was judged when its SYNC landed. A later SYNC from a ledger restored to
    before it (rows the replica holds: stale) or one on another chain (refused ``ledger-fork``) changes nothing, and
    an empty SYNC (a head read) judges again: the session ended once, one END job, one teardown."""
    oid, sock = _released(rig, wire)
    before = _source(rig)
    _sign(rig, oid, "agent")
    assert wire.sync()["ok"] is True
    assert _ended(wire)
    restored = wire.sync(before[-2:])                                 # state A: a restore to before the revoke
    assert (restored["ok"], restored["refused"]) == (False, "ledger-stale")
    last = _source(rig)[-1]
    forked = wire.sync([dataclasses.replace(last, record=last.record + " ", hash="0" * 64)])      # state B
    assert (forked["ok"], forked["refused"]) == (False, "ledger-fork")
    assert wire.sync([])["ok"] is True                                # a head read re-judges: the holder is skipped
    assert _rejudge_idle(wire.server)
    assert wire.runtime.ended == ["sess-1"] and len(wire.server._jobs) == 1
    assert wire.replica.head() == (last.seq, last.hash)
    sock.close()
    wire.close()
    assert wire.runtime.ended == ["sess-1"]                           # the holder's EOF joins the proven END


def test_a_transient_failure_during_the_rejudge_ends_nothing_and_the_next_sync_decides(rig, wire, monkeypatch):
    """Design case 4: a read glitch is not a revoke. The refusal is transient, nothing ends; the next SYNC, whose
    replay reads, finds the revoke and ends the session."""
    oid, sock = _released(rig, wire)
    calls = _count_verifies(wire)
    _sign(rig, oid, "agent")
    real = wire.replica.replay
    monkeypatch.setattr(wire.replica, "replay", lambda: (_ for _ in ()).throw(OSError("the replica is unreadable")))
    assert wire.sync()["ok"] is True                                  # the rows commit; only the re-judge's read fails
    assert _rejudge_idle(wire.server)
    assert calls and wire.runtime.ended == [] and wire.launcher_index("s-00000000000000b1").get("state") != "ended"
    assert wire.runtime.children[0].released is True
    monkeypatch.setattr(wire.replica, "replay", real)
    assert wire.sync([])["ok"] is True
    assert _ended(wire) and wire.runtime.ended == ["sess-1"]
    sock.close()


def test_an_unrelated_sync_ends_nothing(rig, wire):
    """Design case 5: another agent's grant is judged (the re-judge ran) and the session stands."""
    oid, sock = _released(rig, wire)
    calls = _count_verifies(wire)
    rig.link()                                                        # an unrelated link: bars and revokes nothing
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server)
    assert calls, "the re-judge never ran, so this case proves nothing"
    assert wire.runtime.ended == [] and "s-00000000000000b1" in wire.server._live
    assert wire.launcher_index("s-00000000000000b1").get("state") != "ended"
    sock.close()


def test_the_sync_answer_does_not_wait_for_the_rejudge(rig, wire):
    """The re-judge runs on a worker: a verifier that blocks cannot delay the SYNC's answer."""
    oid, sock = _released(rig, wire)
    gate, entered = threading.Event(), threading.Event()
    real = wire.server._verify

    def slow(proof, exposure, claims=None):
        entered.set()
        gate.wait(10)
        return real(proof, exposure, claims)
    wire.server._verify = slow
    rig.link()
    t0 = time.monotonic()
    assert wire.sync()["ok"] is True
    assert time.monotonic() - t0 < 2.0 and entered.wait(5)
    gate.set()
    assert _rejudge_idle(wire.server)
    sock.close()


# --- levain 34, D2 fix round 1 (L3 r1) ------------------------------------------------------------------------------------

B2 = "s-00000000000000b2"


def test_an_end_that_raises_for_one_holder_skips_no_other_and_the_next_sync_retries_it(rig, wire):
    """L3 r1 MED (four seats): the END of the first revoked holder raises; the second is still ENDed in the same pass,
    and the first, whose END never started, is ENDed by the next (empty) SYNC."""
    _, s1 = _released(rig, wire)
    oid, s2 = _released(rig, wire, key=B2, name="research-bot")
    real_end = wire.server._end

    def end(key, deadline_s):
        if key == "s-00000000000000b1":
            raise RuntimeError("can't start new thread")
        return real_end(key, deadline_s)
    wire.server._end = end
    _sign(rig, oid, "key")                                            # the born signer of both links
    assert wire.sync()["ok"] is True
    assert _ended(wire, B2), "the raise for holder 1 skipped holder 2"
    assert _rejudge_idle(wire.server)
    assert wire.launcher_index("s-00000000000000b1").get("state") != "ended"
    wire.server._end = real_end
    assert wire.sync([])["ok"] is True
    assert _ended(wire) and len(wire.server._jobs) == 2
    s1.close()
    s2.close()


def test_a_rejudge_worker_that_cannot_start_still_runs_the_pass(rig, wire, monkeypatch):
    """L3 r1 MED (codex, deepseek): ``Thread.start`` fails for the re-judge worker (thread exhaustion). The SYNC that
    carried the revoke is the last one, so no later SYNC can pick the pass up; it runs on the SYNC's own thread."""
    oid, sock = _released(rig, wire)
    refused = []

    class NoWorker(threading.Thread):
        def start(self):
            if getattr(self, "_target", None) is not None and getattr(self._target, "__name__", "") == "_rejudge_loop":
                refused.append(1)
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(D.threading, "Thread", NoWorker)
    _sign(rig, oid, "agent")
    assert wire.sync()["ok"] is True                                  # the only SYNC after the revoke
    assert refused == [1] and not wire.server._rejudge_running
    assert _ended(wire), "a worker that could not start lost the committed revoke"
    assert wire.runtime.ended == ["sess-1"]
    sock.close()


def test_an_end_job_whose_thread_cannot_start_is_unproven_not_in_flight_for_good(rig, wire, monkeypatch):
    """A failed ``Thread.start`` for an END job used to leave a job that is never done; the re-judge would then skip
    the holder as 'in flight' forever. It is finished UNPROVEN instead, and the next SYNC starts a fresh one."""
    oid, sock = _released(rig, wire)
    refused = []

    class NoEndJob(threading.Thread):
        def start(self):
            if getattr(self, "_target", None) is not None and getattr(self._target, "__name__", "") == "_run_end" \
                    and not refused:
                refused.append(1)
                raise RuntimeError("can't start new thread")
            return super().start()
    monkeypatch.setattr(D.threading, "Thread", NoEndJob)
    _sign(rig, oid, "agent")
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server) and refused == [1]
    job = wire.server._jobs["s-00000000000000b1"]
    assert job.done.is_set() and job.result.terminated is False and wire.runtime.ended == []
    assert wire.sync([])["ok"] is True
    assert _ended(wire) and wire.runtime.ended == ["sess-1"]
    sock.close()


def test_an_expiry_alone_ends_no_running_session(rig, wire):
    """L3 r1 complement LOW 3: the design's stop reasons are revoke_agent / revoke_grant / revoke_key (and bar, raise).
    The session's link expires while it runs and an unrelated SYNC lands: the re-judge ran and nothing ended."""
    oid, sock = _released(rig, wire)
    calls = _count_verifies(wire)
    rig.world.clock.now += timedelta(days=30)                         # past the link's 29-day expiry
    rig.link()                                                        # an unrelated row, signed after the expiry
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server)
    assert calls, "the re-judge never ran, so this case proves nothing"
    assert wire.runtime.ended == [] and wire.launcher_index("s-00000000000000b1").get("state") != "ended"
    h = wire.server._live["s-00000000000000b1"]
    with pytest.raises(L.LauncherRefused, match="link-not-live"):     # the same link at a point of use still refuses
        wire.server._verify(h.proof, h.exposure, D.FrameClaims((wire.head()["seq"], wire.head()["hash"]), (False, 0)))
    sock.close()


# --- levain 35, lease fix (L3 lease r2) ---------------------------------------------------------------------------------

def _renew(rig, link_id, signer="laptop", days=29):
    at = rig.world.clock.now + timedelta(minutes=1)        # World.link moves the clock a minute before it signs
    rig.world.link({"op": "renew", "grant_id": link_id, "expires_at": utc_stamp(at + timedelta(days=days)),
                    "signed_at": utc_stamp(at)}, signer)


@pytest.mark.parametrize("twice", [False, True])
def test_a_legal_renewal_of_the_running_sessions_own_link_keeps_the_session(rig, wire, twice):
    """L3 lease r2 MED (codex): a renew moves the link's ``grant_seqs`` to the renewal row, so the LAUNCH proof's
    ``current`` row is no longer the ledger's. The re-judge used to read that as ``link-proof-mismatch`` (standing) and
    END a healthy session: the operator's own renewal killed his own run. A renewal (twice over too) keeps it."""
    oid, sock = _released(rig, wire)
    link_id = rig.row(oid)["link_id"]
    calls = _count_verifies(wire)
    _renew(rig, link_id)
    if twice:
        _renew(rig, link_id)
    state = rig.authority.replay()
    h = wire.server._live["s-00000000000000b1"]
    assert state.grant_seqs[link_id] > h.proof.current.seq, "the renew did not move the link's current row"
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server)
    assert calls, "the re-judge never ran, so this case proves nothing"
    assert wire.runtime.ended == [] and "s-00000000000000b1" in wire.server._live
    assert wire.launcher_index("s-00000000000000b1").get("state") != "ended"
    # the point of use stays exact: a LAUNCH or RELEASE with the old proof still refuses (a fire re-reads its proof)
    with pytest.raises(L.LauncherRefused, match="link-proof-mismatch"):
        wire.server._verify(h.proof, h.exposure, D.FrameClaims((wire.head()["seq"], wire.head()["hash"]), (False, 0)))
    sock.close()


@pytest.mark.parametrize("how", ["agent", "grant", "key"])
def test_a_revoke_after_a_renewal_of_the_running_link_still_ends_it(rig, wire, how):
    """The renewal tolerance does not reach the revoke path: renew, then revoke (agent, the link, or its born signer's
    key), each SYNCed on its own: the first SYNC keeps the session, the second ENDs it."""
    oid, sock = _released(rig, wire)
    _renew(rig, rig.row(oid)["link_id"])
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server) and wire.runtime.ended == []
    _sign(rig, oid, how)
    assert wire.sync()["ok"] is True
    assert _ended(wire), "a revoke after a renewal ended nothing"
    assert wire.runtime.ended == ["sess-1"]
    sock.close()


def test_a_renewal_signed_by_a_key_since_revoked_ends_the_session(rig, wire):
    """The renewal row the ledger now holds is part of what the link means: a second key renews the link and is then
    revoked. The LAUNCH proof's own rows still stand (the first key), but the link's current row's signer does not."""
    oid, sock = _released(rig, wire)
    second = Key(rig.world.tmp / "keys", "laptop2")
    rig.world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
    rig.world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key,
                    "class": "laptop-presence", "custody": "passphrase"}, "laptop")
    _renew(rig, rig.row(oid)["link_id"], signer="laptop2")
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server) and wire.runtime.ended == []
    rig.world.link({"op": "revoke_key", "fingerprint": second.fp}, "laptop")
    assert wire.sync()["ok"] is True
    assert _ended(wire), "the renewal's revoked signer ended nothing"
    sock.close()


def _tamper(signature: str) -> str:
    """Change ONE body character of an armored SSH signature, at a fixed place, to a different base64 character. A
    ``str.replace`` of a letter that may be absent from a random base64 body changes nothing about a quarter of the
    time (L3 lease r3, deepseek MED); this always changes the value, ten characters before the armor's footer."""
    at = signature.index("-----END") - 10
    return signature[:at] + ("B" if signature[at] == "A" else "A") + signature[at + 1:]


@pytest.mark.parametrize("fault", ["seq-past", "genuine-before-born", "genuine-after-current", "record", "signature"])
def test_a_rejudge_proof_whose_current_row_is_not_a_genuine_earlier_row_of_the_link_is_refused(rig, fault):
    """The re-judge accepts an EARLIER current row only as an authentic row of the ledger, at or after the link's born
    row and before its current one; a forged, moved or out-of-range row (an authentic row of another link included)
    still refuses ``link-proof-mismatch`` or ``link-signature-invalid`` (verify_point_of_use, ``rejudge=True``)."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    proof = _proof(rig, oid)
    _renew(rig, rig.row(oid)["link_id"])
    rig.link()                                                        # an unrelated row past the link's current one
    assert _check(rig, proof, rejudge=True) is None                   # the genuine earlier row passes
    cur, rows = proof.current, {r.seq: r for r in _source(rig)}

    def genuine(seq):
        r = rows[seq]
        return C.LinkRow(r.seq, r.prev_hash, r.record, r.signer_fp, r.signature)
    if fault == "seq-past":
        bad, name = dataclasses.replace(cur, seq=rig.authority.replay().seq + 1), "link-proof-mismatch"
    elif fault == "genuine-before-born":
        bad, name = genuine(proof.born.seq - 1), "link-proof-mismatch"
    elif fault == "genuine-after-current":
        bad, name = genuine(rig.authority.replay().seq), "link-proof-mismatch"
    elif fault == "record":
        bad, name = dataclasses.replace(cur, record=cur.record + " "), "link-proof-mismatch"
    else:
        tampered = _tamper(cur.signature)
        assert tampered != cur.signature, "the tamper changed nothing, so the case proves nothing"
        bad, name = dataclasses.replace(cur, signature=tampered), "link-signature-invalid"
    with pytest.raises(L.LauncherRefused) as exc:
        _check(rig, dataclasses.replace(proof, current=bad), rejudge=True)
    assert exc.value.name == name


def test_known_edge_pin_an_unrelated_authentic_row_between_born_and_current_is_accepted_as_the_proofs_current(rig):
    """KNOWN-EDGE PIN (L3 lease r3, codex LOW / deepseek MED; the head judged it accepted): the re-judge takes any
    authentic, active-signed ledger row at or after the link's born row and before its current one as the proof's
    current row, even one that is not this link's (here an enrol of another key, appended between the link's born
    row and its renewal). A production proof is the LAUNCH proof, verified exact then, so only a reconstructed proof
    reaches this. It is pinned as it behaves today: tightening the check (the row must be a renew naming the link)
    flips this test on purpose."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    proof = _proof(rig, oid)
    rig.link()                                                        # U: an unrelated authentic row after the born row
    _renew(rig, rig.row(oid)["link_id"])                              # R: the link's current row, past U
    state, rows = rig.authority.replay(), {r.seq: r for r in _source(rig)}
    u = rows[proof.born.seq + 1]
    assert proof.born.seq < u.seq < state.grant_seqs[rig.row(oid)["link_id"]], "U is not strictly between born and current"
    unrelated = C.LinkRow(u.seq, u.prev_hash, u.record, u.signer_fp, u.signature)
    assert _check(rig, dataclasses.replace(proof, current=unrelated), rejudge=True) is None
    with pytest.raises(L.LauncherRefused, match="link-proof-mismatch"):       # the exact check (LAUNCH, RELEASE) still refuses it
        _check(rig, dataclasses.replace(proof, current=unrelated))


def test_a_holder_with_no_claims_is_skipped_by_the_rejudge_not_ended(rig, wire):
    """L3 lease r2 MED (deepseek): a live holder whose ``claims`` is None. Unreachable by construction (every LAUNCH on
    a replica server decodes its claims or is refused), so the record is forced here. The replica verifier would refuse
    it ``ledger-head-missing`` (standing) and END a session over a hole in the server's own record; it is skipped, and
    a revoke SYNCed afterwards for that session is left to the operator tick."""
    oid, sock = _released(rig, wire)
    seen = []
    real = wire.server._verify

    def verify(proof, exposure, claims=None):
        seen.append(claims)
        return real(proof, exposure, claims)
    wire.server._verify = verify
    wire.server._live["s-00000000000000b1"].claims = None
    rig.link()
    assert wire.sync()["ok"] is True
    assert _rejudge_idle(wire.server)
    assert seen == [] and wire.runtime.ended == [] and "s-00000000000000b1" in wire.server._live
    sock.close()


def test_every_launch_on_a_replica_server_carries_claims_into_its_holder(rig, wire):
    """The construction the guard above rests on: on a server with a replica, a LAUNCH's holder holds the frame's
    claims (a LAUNCH with no ledger head or no bar is refused before any holder exists: pinned in
    ``tests/test_agents_authority_replica.py``, the ``for missing in`` case)."""
    _, sock = _released(rig, wire)
    claims = wire.server._live["s-00000000000000b1"].claims
    assert claims is not None and claims.rejudge is False and claims.ledger_head[0] >= 1
    sock.close()


# --- the bound readers ------------------------------------------------------------------------------------------------------

def test_the_bound_usage_readers_read_the_published_bytes_and_define_seconds(tmp_path, monkeypatch):
    """S10b C: ``tunnel_bytes`` is the figure root published at END (stamped on the ended record), never a walk of
    ``L/gw/`` (a front log holding a different figure is not read at all)."""
    from levain.qbroker import operator as op
    from levain.slot.runtime_helper import FRONT_LOG_NAME
    store = tmp_path / "L"
    for n, session in ((0, "s-aaaaaaaaaaaaaaaa"), (1, "s-bbbbbbbbbbbbbbbb")):
        d = store / "gw" / f"slot{n}"
        d.mkdir(parents=True)
        (d / FRONT_LOG_NAME).write_text(json.dumps({"event": "tunnel", "session": session, "passthrough_total": 7}) + "\n")
    idx = L.SessionIndex(tmp_path / "idx")
    idx.put("s-0000000000000001", "ended", session="s-bbbbbbbbbbbbbbbb", run_id="r-1", started_s=100.0, ended_s=161.2,
            tunnel_bytes=101)
    idx.put("s-0000000000000003", "ended", session="s-aaaaaaaaaaaaaaaa", run_id="r-3", started_s=1.0, ended_s=2.0)
    idx.put("s-0000000000000004", "ended", session="s-dddddddddddddddd", run_id="r-4", started_s=1.0, ended_s=2.0,
            tunnel_bytes=True)
    idx.put("s-0000000000000002", "started", session="s-cccccccccccccccc", run_id="r-2", started_s=100.0)
    monkeypatch.setattr(op, "run_state", lambda root, run_id: {"known": True, "calls": 9} if root == store / "queue" else None)
    src = D.bound_usage_sources(queue_root=store / "queue", store=store, index_dir=tmp_path / "idx")
    assert src.tunnel_bytes("s-bbbbbbbbbbbbbbbb") == 101
    assert src.tunnel_bytes("s-aaaaaaaaaaaaaaaa") is None          # ended, nothing published: the reservation settles
    assert src.tunnel_bytes("s-dddddddddddddddd") is None          # a bool is no count
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
    assert svc == units.by_name("dispatch_run")                      # registered (CONTRACT 1.3 / 2.2)


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


def test_an_unexpected_launch_failure_answers_internal_error_and_leaves_one_cause_line_on_stderr(rig, env, monkeypatch,
                                                                                                 capsys):
    """VM finding 5: a non-LauncherRefused failure reached the operator as a bare `internal-error` with its cause
    nowhere on the box. The cause line names the type and the last frame, never a field of the request."""
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    key = "s-00000000000000c7"

    def boom(launch):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.launcher, "spawn", boom)
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    assert (exc.value.name, exc.value.reason_class) == ("internal-error", "transient")
    env.drain()
    err = [ln for ln in capsys.readouterr().err.splitlines() if "RuntimeError" in ln]
    assert len(err) == 1, err
    assert "dispatch_run" in err[0] or "test_slot_dispatch_run_server" in err[0] or ".py:" in err[0]
    for secret in (key, str(oid), "passthrough"):
        assert secret not in err[0]


def test_a_supervisor_refusal_keeps_its_name_in_the_cause_line_and_the_reply(rig, env, monkeypatch, capsys):
    """VM v2 finding 2: a `staging-missing` refusal off the helper's answer line (the SAME path the supervisor takes,
    `parse_helper_line` at supervisor.py) reached the log as `Refused at supervisor.py:407` and the client as a bare
    `internal-error`: the refusal's name was dropped. Both now carry the NAME (and only the name)."""
    from levain.slot import supervisor as SUP

    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    key = "s-00000000000000c8"

    def refused(launch):
        SUP.parse_helper_line(b'{"ok":false,"refused":"staging-missing"}\n', "render")

    monkeypatch.setattr(env.launcher, "spawn", refused)
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, key, "claude", "sonnet", "b", "passthrough", EXP))
    assert (exc.value.name, exc.value.reason_class) == ("internal-error", "transient")
    assert "staging-missing" in exc.value.detail
    env.drain()
    err = [ln for ln in capsys.readouterr().err.splitlines() if "Refused" in ln]
    assert len(err) == 1, err
    assert err[0].endswith(" (refused 'staging-missing')"), err
    for secret in (key, str(oid), "passthrough", "runtime helper"):
        assert secret not in err[0] and secret not in exc.value.detail


def test_a_refusal_detail_never_reaches_the_log_or_the_reply(rig, env, monkeypatch, capsys):
    """L3 v2fix r1: a supervisor `Refused.detail` can embed git stderr, a repo, settings or base-map path
    (supervisor.py `err.strip()[:200]`). Only the refusal's NAME is emitted: a detail holding a path and a `key`
    marker appears in neither the log line nor the client reply."""
    from levain.slot import Refused

    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    leak = "/Users/someone/.levain/repo key=sk-ant-SECRET fatal: could not read from remote"

    def refused(launch):
        raise Refused("worktree-failed", leak)

    monkeypatch.setattr(env.launcher, "spawn", refused)
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000c9", "claude", "sonnet", "b", "passthrough", EXP))
    env.drain()
    err = capsys.readouterr().err
    assert "'worktree-failed'" in err and "'worktree-failed'" in exc.value.detail
    for marker in ("/Users/someone", "key=", "SECRET", "fatal"):
        assert marker not in err and marker not in exc.value.detail, marker


def test_a_refusal_name_cannot_forge_a_second_log_line(capsys):
    from levain.slot import Refused

    D._report_failure("LAUNCH", Refused("x\nlevain dispatch-run: op END failed: forged", "d\r\n" + "y" * 500))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1, lines
    assert len(lines[0]) < 400 and "yyyy" not in lines[0]


def test_the_cause_line_stays_one_line_whatever_op_the_client_sent(capsys):
    """L3 setupfix r2: `op` comes from the client frame; a newline in it must not forge a second journald line."""
    D._report_failure("LAUNCH\nlevain dispatch-run: op END failed: forged\r" + "x" * 500, RuntimeError("boom"))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1, lines
    assert "forged" not in lines[0].split("failed:")[0][len("levain dispatch-run: op "):] or "\\n" in lines[0]
    assert len(lines[0]) < 200


# --- the launchd log is bounded by the program (L3 r1 of the stderr unit) -------------------------------------------------

def _log_fd(tmp_path, size):
    """A log as launchd opens it (appending), already ``size`` bytes long (sparse: the bytes are never read)."""
    import os
    path = tmp_path / "dispatch_run.log"
    with open(path, "wb") as f:
        f.truncate(size)
    return path, os.open(path, os.O_WRONLY | os.O_APPEND)


def test_an_over_cap_log_is_emptied_and_an_under_cap_one_is_left_alone(tmp_path):
    import os
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES + 1)
    try:
        D._bound_log(fd, path)
        assert path.stat().st_size == 0
    finally:
        os.close(fd)
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES)
    try:
        D._bound_log(fd, path)
        assert path.stat().st_size == D.LOG_MAX_BYTES        # at the cap, not past it
    finally:
        os.close(fd)


def test_a_pipe_and_a_closed_descriptor_are_left_alone():
    import os
    r, w = os.pipe()
    try:
        D._bound_log(w)                                      # not a regular file: no truncate, no raise
    finally:
        os.close(r)
        os.close(w)
    D._bound_log(w)                                          # closed: the OSError is swallowed


def test_the_size_check_and_the_truncate_run_under_the_lock(tmp_path, monkeypatch):
    import os
    held = []
    real = os.ftruncate

    def spy(fd, n):
        held.append(D._LOG_LOCK.locked())
        return real(fd, n)

    monkeypatch.setattr(D.os, "ftruncate", spy)
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES + 1)
    try:
        D._bound_log(fd, path)
    finally:
        os.close(fd)
    assert held == [True]


def test_concurrent_reporters_share_a_bounded_log_and_none_raises(tmp_path, monkeypatch):
    import os
    import sys
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES + 1)
    monkeypatch.setattr(D, "_expected_log", lambda: path)
    saved = os.dup(2)
    out = open(path, "a")                                    # sys.stderr for the program, appending as launchd's fd does
    errors = []
    try:
        os.dup2(fd, 2)
        monkeypatch.setattr(sys, "stderr", out)

        def work():
            try:
                for _ in range(100):
                    D._report_failure("LAUNCH", RuntimeError("boom"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        os.dup2(saved, 2)
        os.close(saved)
        os.close(fd)
        out.close()
    assert errors == []
    assert 0 < path.stat().st_size < D.LOG_MAX_BYTES        # the over-cap file was emptied once, then only lines
    assert all(ln.startswith("levain dispatch-run: op 'LAUNCH' failed: RuntimeError") for ln in
               path.read_bytes().replace(b"\0", b"").decode().splitlines() if ln)


def test_the_program_bounds_its_log_at_startup_and_before_each_cause_line(monkeypatch):
    import os
    import pwd
    calls = []
    monkeypatch.setattr(D, "_trim_if_over", lambda fd, expected=None: calls.append(fd))
    monkeypatch.setattr(D, "_bound_log", lambda fd=2, expected=None: calls.append(fd))
    D._report_failure("LAUNCH", RuntimeError("boom"))
    assert calls == [2]
    if os.geteuid() == 0:
        pytest.skip("the program refuses root before the startup trim")
    me = pwd.getpwuid(os.geteuid())
    monkeypatch.setattr(D.pwd, "getpwnam", lambda name: me)
    assert D.main(["--operator-uid", "x"]) == D.EX_CONFIG    # fails at argument parsing, after the startup trim
    assert calls == [2, 2]


# --- L3 r2 of the stderr unit: one lock span, an offset reset, and only the unit's own log is trimmed ---------------------

def test_the_failure_line_is_written_under_the_same_lock_that_guards_the_truncate(monkeypatch):
    """gemini MED: a truncate between a line's write and its flush erased it. The write itself must be inside the span."""
    import sys
    held = []

    class Spy:
        def write(self, text):
            held.append(D._LOG_LOCK.locked())
            return len(text)

        def flush(self):
            held.append(D._LOG_LOCK.locked())

    monkeypatch.setattr(sys, "stderr", Spy())
    D._report_failure("LAUNCH", RuntimeError("boom"))
    assert held and all(held), held


def test_a_truncate_cannot_interleave_with_a_failure_line_write(monkeypatch):
    """With the lock held by another truncater, the reporter writes nothing until it is released."""
    import sys
    import io
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stderr", sink)
    D._LOG_LOCK.acquire()
    try:
        t = threading.Thread(target=D._report_failure, args=("LAUNCH", RuntimeError("boom")))
        t.start()
        t.join(0.3)
        assert t.is_alive() and sink.getvalue() == ""        # blocked on the lock, nothing written
    finally:
        D._LOG_LOCK.release()
    t.join(5)
    assert sink.getvalue().startswith("levain dispatch-run: op 'LAUNCH' failed")


def test_the_offset_is_reset_after_a_truncate(tmp_path):
    import os
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES + 1)
    try:
        os.lseek(fd, 0, os.SEEK_END)
        D._bound_log(fd, path)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
    finally:
        os.close(fd)


def test_only_the_units_own_log_is_trimmed(tmp_path, monkeypatch):
    """deepseek MED: whatever regular file fd 2 happened to be (a shell redirect) was emptied once over the cap."""
    import os
    path, fd = _log_fd(tmp_path, D.LOG_MAX_BYTES + 1)
    other = tmp_path / "somewhere_else.log"
    other.write_bytes(b"x")
    try:
        D._bound_log(fd, other)                              # fd is not that file
        assert path.stat().st_size == D.LOG_MAX_BYTES + 1
        D._bound_log(fd, tmp_path / "absent.log")            # the expected file is missing (Linux): nothing to trim
        assert path.stat().st_size == D.LOG_MAX_BYTES + 1
        monkeypatch.setattr(D, "_expected_log", lambda: other)
        D._bound_log(fd)                                     # the default path is the module's, not the fd's
        assert path.stat().st_size == D.LOG_MAX_BYTES + 1
        D._bound_log(fd, path)
        assert path.stat().st_size == 0
    finally:
        os.close(fd)


def test_the_expected_log_is_the_path_the_unit_gives_launchd(monkeypatch):
    from levain.slot import units
    monkeypatch.setattr(D, "_host", lambda: "darwin")
    assert D._expected_log() == units.launchd_log_path(units.by_name("dispatch_run"))


# --- S-B1 (152a): step 4 judges the link's billing against the LAUNCHER's ---------------------------------------------------

def test_an_api_key_link_passes_for_an_api_key_launcher_with_no_action_exposure(rig):
    oid = _occurrence(rig, billing="api_key")
    proof = _proof(rig, oid)
    figure = rig.authority.replay().policy.grants[proof.link_id]["budget_run_usd_micros"]
    # dispatch wave 3: the check hands back the grant, and the share is read from it (his verified link)
    grant = _grant(rig, proof, None, billing="api_key")
    assert figure > 0 and D.signed_share(grant) == figure


def test_a_subscription_grant_signs_no_share(rig):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    grant = _grant(rig, _proof(rig, oid))
    # REVERT: the `billing != "api_key"` leg of signed_share
    assert grant is not None and D.signed_share(grant) is None
    assert D.signed_share(None) is None and D.signed_share({"billing": "api_key", "budget_run_usd_micros": 0}) is None


@pytest.mark.parametrize("link_billing,launcher,exposure", [("api_key", "subscription", EXP), ("subscription", "api_key", None)])
def test_a_link_whose_billing_is_not_the_launchers_is_refused(rig, link_billing, launcher, exposure):
    oid = _occurrence(rig, billing=link_billing, exposure=EXP) if link_billing == "subscription" else _occurrence(rig, billing="api_key")
    with pytest.raises(D.LauncherRefused) as caught:
        _check(rig, _proof(rig, oid), exposure, billing=launcher)
    assert caught.value.name == "link-billing-not-the-launchers"


def test_an_api_key_launcher_refuses_an_action_exposure_and_a_zero_dollar_link(rig):
    oid = _occurrence(rig, billing="api_key")
    proof = _proof(rig, oid)
    with pytest.raises(D.LauncherRefused) as caught:
        _check(rig, proof, EXP, billing="api_key")
    assert caught.value.name == "exposure-over-link"
    real = rig.authority.replay()

    class Zeroed:                       # the replayed state, but the figure he signed on this link reads zero
        policy = type("P", (), {"grants": {**real.policy.grants,
                                            proof.link_id: {**real.policy.grants[proof.link_id], "budget_run_usd_micros": 0}}})()

        def __getattr__(self, name):
            return getattr(real, name)

    with pytest.raises(D.LauncherRefused) as caught:
        D.verify_point_of_use(proof, None, replay=lambda: Zeroed(), now=rig.now, bar_of=D.agent_store_bar(rig.store.path),
                              billing="api_key")
    assert caught.value.name == "link-run-budget-zero"


# --- dispatch wave 3 (S-B2): the run server stamps the signed link's figure as a brokered launch's share ---------------------

def test_a_brokered_launch_is_priced_at_the_links_signed_figure_and_nothing_the_fire_sent(rig, env):
    oid = _occurrence(rig, billing="api_key")
    proof = _proof(rig, oid)
    figure = rig.authority.replay().policy.grants[proof.link_id]["budget_run_usd_micros"]
    env.billing = "api_key"
    env.launcher.auth = "brokered"
    env.control.script["ROUTE-RESOLVE"] = lambda f: {"route": "glm", "routemap_generation": 1,
                                                     "route_policy_digest": "sha256:" + "a" * 64}
    # the fire's reserved dollars travel and are only COMPARED: a figure that is not the link's refuses (finding 1)
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b0", "claude", "sonnet", "b", "brokered", None, None, 1))
    assert exc.value.name == "dollars-not-the-links-share" and env.control.minted == {}
    env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b1", "claude", "sonnet", "b", "brokered", None, None,
                                       figure))
    mint = [f for op, f in env.control.calls if op == "RUN-TICKET-MINT"]
    assert len(mint) == 1 and mint[0]["reserved_microdollars"] == figure
    assert env.runtime.starts[0].auth == "brokered"


def test_a_passthrough_server_never_stamps_a_share(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b2", "claude", "sonnet", "b", "passthrough", EXP))
    (mint,) = [f for op, f in env.control.calls if op == "RUN-TICKET-MINT"]
    assert mint["reserved_microdollars"] == 0 and "route" not in mint


def test_a_brokered_launcher_behind_a_verifier_that_answers_no_share_refuses(rig, env):
    oid = _occurrence(rig, billing="api_key")
    env.launcher.auth = "brokered"
    env._verify = lambda proof, exposure, claims=None: None            # a verifier with no figure to hand back
    env.server._verify = env._verify
    # the fire sends a positive figure: the share is the verified link's (here none), never the fire's
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b3", "claude", "sonnet", "b", "brokered", None, None,
                                           1_000_000))
    # REVERT: the `dataclasses.replace(launch, usd_micros=share)` stamp in RunServer._priced (it mints the fire's figure)
    assert exc.value.name == "no-dollar-share" and env.control.minted == {}


# --- finding 1 of the wave-3 RETURN: a brokered run's share is exactly what the fire's window was debited --------------------

def _brokered(env):
    env.billing = "api_key"
    env.launcher.auth = "brokered"
    env.control.script["ROUTE-RESOLVE"] = lambda f: {"route": "glm", "routemap_generation": 1,
                                                     "route_policy_digest": "sha256:" + "a" * 64}
    client = env.client()
    client.auth = "brokered"            # the fire's side of a brokered route (PRE-W wires the real one)
    return client


def _mints(env):
    return [f["reserved_microdollars"] for op, f in env.control.calls if op == "RUN-TICKET-MINT"]


def test_a_fire_that_reserved_less_than_the_signed_figure_is_refused_before_any_mint(rig, env, caplog):
    # S-B1 admits exposure <= the signed figure: this fire's window was debited 100_000 under a link signing 1_000_000.
    # Before the fix the server minted the link's FULL figure, a keys cap ten times what the window holds.
    oid = _occurrence(rig, billing="api_key", micros=100_000)
    figure = rig.authority.replay().policy.grants[_proof(rig, oid).link_id]["budget_run_usd_micros"]
    assert figure == 1_000_000
    client = _brokered(env)
    with caplog.at_level("WARNING", logger="levain.agents.session"):
        _sup, report = _run(rig, oid, client, micros=100_000)
    # REVERT: the `sent != share` refusal in RunServer._priced (the server mints `figure`)
    assert _mints(env) == [] and env.control.minted == {} and env.runtime.starts == []
    assert (report.outcome, report.state) == ("session_spawn_failed", "failed")
    assert "dollars-not-the-links-share" in caplog.text


def test_a_fire_that_reserved_exactly_the_signed_figure_mints_that_figure(rig, env):
    oid = _occurrence(rig, billing="api_key", micros=1_000_000)
    client = _brokered(env)
    _sup, report = _run(rig, oid, client, micros=1_000_000)
    # REVERT: the fire's `reserved.usd_micros` on its SessionLaunch (it sends none and is refused)
    assert _mints(env) == [1_000_000], report
    assert env.runtime.starts[0].auth == "brokered"
    with rig.store.read() as c:
        (held,) = c.execute("SELECT reserved_usd_micros FROM agent_reservations WHERE occurrence_id = ?",
                            (oid,)).fetchone()
    assert held == 1_000_000            # the keys cap is the window's debit, to the micro-dollar


def test_a_brokered_launch_that_sends_no_dollars_is_refused(rig, env):
    oid = _occurrence(rig, billing="api_key")
    client = _brokered(env)
    with pytest.raises(L.LauncherRefused) as exc:
        client.spawn(S.SessionLaunch(oid, "s-00000000000000b4", "claude", "sonnet", "b", "brokered", None))
    assert exc.value.name == "dollars-not-the-links-share" and env.control.minted == {}


def test_a_passthrough_server_refuses_a_launch_that_carries_dollars(rig, env):
    oid = _occurrence(rig, billing="subscription", exposure=EXP)
    with pytest.raises(L.LauncherRefused) as exc:
        env.client().spawn(S.SessionLaunch(oid, "s-00000000000000b5", "claude", "sonnet", "b", "passthrough", EXP,
                                           None, 0))
    # REVERT: the passthrough leg of RunServer._priced
    assert exc.value.name == "dollars-on-a-passthrough-launch" and env.control.minted == {}


def test_the_dollars_travel_on_a_brokered_launch_frame_only():
    proof = C.LinkProof("oc-1", "h", "l", "s", C.LinkRow(1, "", "{}", "fp", "sig"), C.LinkRow(1, "", "{}", "fp", "sig"))
    priced = S.SessionLaunch("oc-1", "s-00000000000000b6", "claude", "sonnet", "b", "brokered", None, None, 250_000)
    plain = S.SessionLaunch("oc-1", "s-00000000000000b7", "claude", "sonnet", "b", "passthrough", EXP)
    wire = C.launch_to_wire(priced, proof)
    assert wire["launch"]["usd_micros"] == 250_000
    assert C.launch_from_wire({"p": C.PROTOCOL, "op": "LAUNCH", "id": 1, **wire})[0].usd_micros == 250_000
    # a launch with no dollars sends the frame it always sent: no key, so an older server reads it unchanged
    assert "usd_micros" not in C.launch_to_wire(plain, proof)["launch"]
    assert C.launch_from_wire({"p": C.PROTOCOL, "op": "LAUNCH", "id": 1,
                               **C.launch_to_wire(plain, proof)})[0].usd_micros is None
    for bad in (-1, True, 1.5, "1"):
        w = C.launch_to_wire(priced, proof)
        w["launch"]["usd_micros"] = bad
        with pytest.raises(C.WireError):
            C.launch_from_wire({"p": C.PROTOCOL, "op": "LAUNCH", "id": 1, **w})
