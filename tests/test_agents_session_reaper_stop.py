"""P7-1 S3: the reaper-side stop for session rows (known edge (a)).

A reaper built from a ``launched`` row holds no child. ``session.session_executor_of`` gives it an executor that stops a
session only when the row is its own, the fire is proven dead, and the launcher's ``end_session`` answers exactly
``StopResult(True, ...)``. Refusals first; then the lease of a row that cannot be stopped; then the one clean stop.
Stores are real ``/2`` agent stores with real launched occurrences; the launcher and the process table are stubs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import supervise
from levain.agents.accounting import StopResult, Usage
from levain.agents.role import add_role
from levain.autonomic.journal import RunJournal
from tests.test_agents_launch import Rig, ROLES
from tests.test_agents_session_launch import Stub, _occurrence
from tests.test_agents_supervise import Attention, Clock
from tests.test_authority_agent_link import revoke_agent

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

DEAD = lambda rec, deadline: "ended"       # noqa: E731
ALIVE = lambda rec, deadline: "matches"    # noqa: E731
FOGGY = lambda rec, deadline: "unreadable"  # noqa: E731


PROVEN = StopResult(True, "S1 END proven")


class Ender(Stub):
    """A launcher that can end a session by key: ``answer`` is what ``end_session`` returns (or raises)."""

    def __init__(self, answer=PROVEN, **kw):
        super().__init__(**kw)
        self.answer = answer
        self.ended = []

    def end_session(self, session_key, deadline_s):
        self.ended.append((session_key, deadline_s))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@pytest.fixture
def rig(tmp_path) -> Rig:
    return Rig(tmp_path)


def _session_row(rig: Rig, n=1, *, fire_record=True, pid=4100, role=None):
    """A launched session-role occurrence whose fire claimed it (``pid``) and opened its session (a key), with the
    fire's identity record on disk. Returns ``(oid, key)``."""
    add_role(rig.store, ROLES["model-role"])      # the store holds the role its spec pins (a rig does not)
    oid = _occurrence(rig, role=role)
    key = f"s-{n:016x}"
    with rig.store.write() as c:
        assert ctl.claim_fire(c, oid, pid, rig.now()) is not None
        assert ctl.record_session_key(c, oid, rig.row(oid)["launch_epoch"], key, rig.now())
    if fire_record:
        path = ctl.fire_record_path(rig.store.directory, oid)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps({"occurrence_id": oid, "pid": pid, "started": "Tue Nov  4 10:00:00 2026",
                                    "args": f"python -m levain.agents fire {oid}"}))
    return oid, key


def _row(rig, oid):
    got = rig.row(oid)
    return {k: got[k] for k in ("occurrence_id", "agent_id", "session_key", "pid", "epoch", "spec_hash")}


def _executor(rig, oid, launcher, fire_state=DEAD):
    return S.RowSessionExecutor(rig.store, _row(rig, oid), launcher, fire_state=fire_state)


def _sup(rig, clock, **kw):
    return supervise.Supervisor(rig.store, authority=rig.authority, clock=clock,
                                journal=RunJournal(rig.store.directory), **kw)


def _revoke(rig, oid):
    rig.world.link(revoke_agent(rig.row(oid)["agent_id"], rig.sid), "laptop")


# --- refusals first: every answer but a proven stop is "cannot stop" ------------------------------------------------

@pytest.mark.parametrize("answer", [
    StopResult(False, "the group is still there"),
    None, True, "terminated", 1, {"terminated": True},
    SimpleNamespace(terminated=True, detail="a lookalike"),
    RuntimeError("the S1 front is down"),
], ids=["false", "none", "bool", "str", "int", "dict", "lookalike", "raises"])
def test_an_end_that_is_not_a_proven_stop_is_cannot_stop(rig, answer):
    oid, key = _session_row(rig)
    launcher = Ender(answer)
    result = _executor(rig, oid, launcher).stop(10)
    assert type(result) is StopResult and result.terminated is False
    assert launcher.ended == [(key, 10.0)]          # it was asked, and the answer proved nothing
    assert rig.row(oid)["state"] == "launched"


def test_a_stopresult_subclass_is_not_the_exact_answer(rig):
    class Sub(StopResult):
        pass
    oid, _ = _session_row(rig)
    assert _executor(rig, oid, Ender(Sub(True, "subclass"))).stop(10).terminated is False


@pytest.mark.parametrize("state", [ALIVE, FOGGY], ids=["alive", "unreadable"])
def test_a_fire_not_proven_dead_is_never_ended_from_here(rig, state):
    oid, _ = _session_row(rig)
    launcher = Ender()
    result = _executor(rig, oid, launcher, state).stop(10)
    assert result.terminated is False and "not proven dead" in result.detail
    assert launcher.ended == []


@pytest.mark.parametrize("how", ["absent", "garbage", "other-occurrence", "no-pid"])
def test_a_fire_record_that_cannot_prove_death_ends_nothing(rig, how):
    oid, _ = _session_row(rig, fire_record=how != "absent")
    path = ctl.fire_record_path(rig.store.directory, oid)
    if how == "garbage":
        path.write_text("{not json")
    elif how == "other-occurrence":
        path.write_text(json.dumps({"occurrence_id": "oc-someone-else", "pid": 4100, "started": "x", "args": "y"}))
    elif how == "no-pid":
        path.write_text(json.dumps({"occurrence_id": oid, "started": "x", "args": "y"}))
    launcher = Ender()
    assert _executor(rig, oid, launcher).stop(10).terminated is False
    assert launcher.ended == []


def test_a_launcher_with_no_end_session_ends_nothing(rig):
    oid, _ = _session_row(rig)
    for launcher in (Stub(), S.UnwiredLauncher()):
        result = _executor(rig, oid, launcher).stop(10)
        assert result.terminated is False and "no end_session" in result.detail


def test_a_row_that_is_not_launched_any_more_is_not_stopped(rig):
    oid, _ = _session_row(rig)
    ex, launcher = _executor(rig, oid, Ender()), None
    rig.finish(oid)
    result = ex.stop(10)
    assert result.terminated is False and "not launched" in result.detail


def test_an_executor_built_from_another_rows_key_never_ends_the_store_rows_session(rig):
    oid, key = _session_row(rig)
    row = _row(rig, oid)
    row["session_key"] = "s-" + "f" * 16               # a stale or forged row: the store's key is another
    launcher = Ender()
    result = S.RowSessionExecutor(rig.store, row, launcher, fire_state=DEAD).stop(10)
    assert result.terminated is False and "not the one" in result.detail
    assert launcher.ended == []


def test_an_executor_built_from_another_agents_row_never_ends_it(rig):
    oid, _ = _session_row(rig)
    row = _row(rig, oid)
    row["agent_id"] = "ag-someone-else"
    launcher = Ender()
    assert S.RowSessionExecutor(rig.store, row, launcher, fire_state=DEAD).stop(10).terminated is False
    row = _row(rig, oid)
    row["occurrence_id"] = "oc-not-in-this-store"
    assert S.RowSessionExecutor(rig.store, row, launcher, fire_state=DEAD).stop(10).terminated is False
    assert launcher.ended == []


def test_the_factory_gives_no_executor_for_another_agents_row_or_a_row_without_a_key(rig):
    oid, _ = _session_row(rig)
    row = _row(rig, oid)
    of = S.session_executor_of(rig.store, Ender(), agent_id=row["agent_id"], fire_state=DEAD)
    assert isinstance(of(row), S.RowSessionExecutor)
    assert of({**row, "agent_id": "ag-other"}) is None
    assert of({**row, "session_key": None}) is None
    with pytest.raises(ValueError):
        S.session_executor_of(rig.store, Ender(), agent_id="")


def test_a_row_whose_pinned_role_is_not_a_session_role_is_not_ended(rig):
    oid, _ = _session_row(rig)
    launcher = Ender()
    ex = _executor(rig, oid, launcher)
    real = S._role.role_by_hash
    S._role.role_by_hash = lambda store, digest: {**ROLES["local-role"], "executor": "dream"}
    try:
        result = ex.stop(10)
    finally:
        S._role.role_by_hash = real
    assert result.terminated is False and "not a session role" in result.detail
    assert launcher.ended == []


def test_a_stop_that_raises_inside_is_not_a_confirmation(rig):
    oid, _ = _session_row(rig)
    boom = lambda rec, deadline: (_ for _ in ()).throw(RuntimeError("ps exploded"))   # noqa: E731
    result = _executor(rig, oid, Ender(), boom).stop(10)
    assert result.terminated is False


def test_usage_never_raises_and_unreadable_is_none(rig):
    oid, key = _session_row(rig)
    ok = _executor(rig, oid, Ender(used=Usage(0, 7, 9, "stub"))).usage()
    assert ok == Usage(0, 7, 9, "stub")

    class Bad(Ender):
        def usage(self, session_key):
            raise RuntimeError("no record")
    assert _executor(rig, oid, Bad()).usage().usd_micros is None


# --- the reaper: a row it cannot stop is not renewed ----------------------------------------------------------------

def test_a_dead_sessions_lease_is_not_renewed_once_its_stop_failed(rig):
    # Reproduces known edge (a): before this slice the reaper renewed the lease of a launched session row every tick
    # whatever its stop answered, so a dead session's lease never lapsed and takeover never fired.
    clock = Clock(rig)
    oid, _ = _session_row(rig)
    sup = _sup(rig, clock)
    launcher = Ender(StopResult(False, "S1 END not proven"))
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    att = Attention()
    _revoke(rig, oid)
    leases = []
    for tick in range(3):
        clock.advance(400)
        out = sup.reap(of, attention=att.upsert, resolve=att.resolve)
        assert out.unconfirmed == [oid] and out.ended == []
        leases.append(rig.row(oid)["lease_until"])
        if tick:
            assert out.renewed == []                        # the first pass renews (it asks after); later ones do not
    assert leases[1] == leases[2]                           # frozen: the lease can now lapse
    assert rig.row(oid)["state"] == "launched" and len(launcher.ended) == 3
    assert list(att.rows) == [supervise.ATTENTION_PREFIX + oid] and att.calls == 1


def test_a_lapsed_unstoppable_session_is_taken_over_not_kept_alive(rig):
    clock = Clock(rig)
    oid, _ = _session_row(rig)
    sup = _sup(rig, clock)
    of = S.session_executor_of(rig.store, Ender(StopResult(False, "no")), agent_id=rig.row(oid)["agent_id"],
                               fire_state=DEAD)
    _revoke(rig, oid)
    for _ in range(2):
        clock.advance(400)
        sup.reap(of)
    # the first pass renewed to t1 + LEASE_S (t1 = 400); a second renewal at t = 800 would reach 800 + LEASE_S. Just past
    # the first one, only a lease that was NOT renewed again has lapsed
    clock.advance(supervise.LEASE_S + 50 - 400)
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, clock()) is True        # lapse recovery fires: the lease was not renewed past it
    assert rig.row(oid)["state"] == "stopped" and rig.row(oid)["reason"] == "crash_recovered"


def test_a_sessionless_row_is_still_renewed_after_an_unconfirmed_stop(rig):
    # the dream's rows carry no session key: their renewal is unchanged (test_agents_supervise case 384 pins it too)
    from tests.test_agents_supervise_fire import _fire
    clock = Clock(rig)
    rig.link()
    add_role(rig.store, ROLES["notify-role"])      # the rig agent's role, executor dream: a sessionless run
    sup = _sup(rig, clock)
    oid = _fire(rig, "a", sup._journal)
    with rig.store.write() as c:
        assert ctl.claim_fire(c, oid, 4001, rig.now()) is not None
    rig.world.link(revoke_agent(rig.agent, rig.sid), "laptop")
    seen = []
    for _ in range(2):
        clock.advance(400)
        out = sup.reap(lambda row: SimpleNamespace(stop=lambda d: StopResult(False, "no"),
                                                   usage=lambda: Usage(None, None, None, "t")))
        seen.append(out.renewed)
    assert seen == [[oid], [oid]]


# --- the one clean stop ----------------------------------------------------------------------------------------------

def test_a_stub_session_whose_fire_died_is_stopped_and_its_slot_and_reservation_released(rig):
    clock = Clock(rig)
    oid, key = _session_row(rig)
    assert rig.taken(oid) == ([(1,)], [("open",)])
    sup = _sup(rig, clock)
    launcher = Ender(used=Usage(0, 10, 20, "stub"))
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    att = Attention()
    _revoke(rig, oid)
    clock.advance(60)
    out = sup.reap(of, attention=att.upsert, resolve=att.resolve)
    row = rig.row(oid)
    assert out.asked == [oid] and out.ended == [oid] and out.unconfirmed == []
    assert (row["state"], row["reason"]) == ("stopped", "barred") or row["state"] == "stopped"
    assert launcher.ended == [(key, float(supervise.STOP_DEADLINE_S))]
    assert rig.taken(oid) == ([], [("settled",)]) and att.rows == {}


def test_a_stop_at_due_session_whose_fire_died_ends_stopped_stop_at(rig):
    oid, key = _session_row(rig)
    clock = Clock(rig)
    sup = _sup(rig, clock)
    launcher = Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    with rig.store.read() as c:
        stop_at = json.loads(c.execute("SELECT spec FROM agent_specs WHERE spec_hash = ?",
                                       (rig.row(oid)["spec_hash"],)).fetchone()[0])["stop_at"]
    clock.advance(stop_at + 1)
    out = sup.reap(of)
    assert out.due == [oid] and out.ended == [oid]
    assert (rig.row(oid)["state"], rig.row(oid)["reason"]) == ("stopped", "stop_at")


def test_a_stop_is_asked_the_stores_key_not_the_callers(rig):
    oid, key = _session_row(rig)
    launcher = Ender()
    ex = _executor(rig, oid, launcher)
    assert ex.stop(3).terminated is True
    assert launcher.ended == [(key, 3.0)]


# --- the real fire-state reader (no signals, real processes) ---------------------------------------------------------

def _rec(pid, started="x", args="y", oid="oc-1"):
    return {"occurrence_id": oid, "pid": pid, "started": started, "args": args}


def test_fire_state_dead_pid_is_ended():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert S._fire_state(_rec(proc.pid), 5) == "ended"


def test_fire_state_a_live_process_that_is_not_the_recorded_one_is_ended():
    # this process holds the pid now, but not under the recorded start time or arguments: the number was reused
    assert S._fire_state(_rec(os.getpid(), started="Mon Jan  1 00:00:00 2001", args="not this"), 5) == "ended"


def test_fire_state_the_recorded_live_fire_is_matches_and_never_signalled():
    from levain.consent.delivery import process_identity
    oid = "oc-0123456789abcdef"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", oid])
    try:
        ident = None
        for _ in range(50):
            ident = process_identity(proc.pid)
            if ident is not None:
                break
            time.sleep(0.05)
        assert ident is not None
        assert S._fire_state(_rec(proc.pid, ident[0], ident[1], oid), 5) == "matches"
        assert proc.poll() is None                      # still alive: the reader sent nothing
    finally:
        proc.kill()
        proc.wait()
