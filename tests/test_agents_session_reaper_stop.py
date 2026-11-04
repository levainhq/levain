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
    ex = _executor(rig, oid, Ender())
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


def test_a_row_whose_pinned_role_is_not_a_session_role_is_not_ended(rig, monkeypatch):
    oid, _ = _session_row(rig)
    launcher = Ender()
    ex = _executor(rig, oid, launcher)
    monkeypatch.setattr(S._role, "role_by_hash", lambda store, digest: {**ROLES["local-role"], "executor": "dream"})
    result = ex.stop(10)
    assert result.terminated is False and "not a session role" in result.detail
    assert launcher.ended == []


def test_a_stop_that_raises_inside_is_not_a_confirmation(rig):
    oid, _ = _session_row(rig)
    boom = lambda rec, deadline: (_ for _ in ()).throw(RuntimeError("ps exploded"))   # noqa: E731
    result = _executor(rig, oid, Ender(), boom).stop(10)
    assert result.terminated is False


def test_end_unproven_marks_only_a_dead_fire_whose_end_failed_and_every_stop_resets_it(rig):
    # r1 F1: the one marker the reaper freezes on; set by no refusal before the proof, cleared by a proven end
    oid, _ = _session_row(rig)
    states = iter([DEAD, DEAD, ALIVE])
    launcher = Ender(StopResult(False, "not proven"))
    ex = S.RowSessionExecutor(rig.store, _row(rig, oid), launcher, fire_state=lambda r, d: next(states)(r, d))
    assert ex.end_unproven is False and ex.fire_proven_dead(10) is True       # the probe asks no end
    assert launcher.ended == []
    assert ex.stop(10).terminated is False and ex.end_unproven is True        # proven dead, the end failed
    assert ex.stop(10).terminated is False and ex.end_unproven is False       # a later stop refused (alive): reset
    raising = S.RowSessionExecutor(rig.store, _row(rig, oid), Ender(RuntimeError("front down")), fire_state=DEAD)
    assert raising.stop(10).terminated is False and raising.end_unproven is True   # an end that raised proved nothing
    proven = S.RowSessionExecutor(rig.store, _row(rig, oid), Ender(), fire_state=DEAD)
    assert proven.stop(10).terminated is True and proven.end_unproven is False
    for refused in (S.RowSessionExecutor(rig.store, _row(rig, oid), Stub(), fire_state=DEAD),
                    S.RowSessionExecutor(rig.store, {**_row(rig, oid), "agent_id": "ag-x"}, Ender(), fire_state=DEAD)):
        assert refused.fire_proven_dead(10) is False
        assert refused.stop(10).terminated is False and refused.end_unproven is False


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
    # Reproduces known edge (a): before S3 the reaper renewed the lease of a launched session row every tick whatever
    # its stop answered, so a dead session's lease never lapsed and takeover never fired. Its renewal now waits for its
    # own stop's answer in the same pass, so not even the first pass renews it.
    clock = Clock(rig)
    oid, _ = _session_row(rig)
    sup = _sup(rig, clock)
    launcher = Ender(StopResult(False, "S1 END not proven"))
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    att = Attention()
    _revoke(rig, oid)
    initial = rig.row(oid)["lease_until"]
    for _ in range(3):
        clock.advance(200)
        out = sup.reap(of, attention=att.upsert, resolve=att.resolve)
        assert out.unconfirmed == [oid] and out.ended == [] and out.renewed == [] and out.frozen == [oid]
        assert rig.row(oid)["lease_until"] == initial      # frozen: the lease can lapse
    assert rig.row(oid)["state"] == "launched" and len(launcher.ended) == 3
    assert list(att.rows) == [supervise.ATTENTION_PREFIX + oid] and att.calls == 1


@pytest.mark.parametrize("fresh", [False, True], ids=["one-supervisor", "supervisor-per-tick"])
def test_a_lapsed_unstoppable_session_is_taken_over_not_kept_alive(rig, fresh):
    # F2 (r1): the freeze holds with no process-local memory, so a Supervisor built per tick, or a reaper restarted
    # between ticks, freezes it too. Before the r1 fix a fresh Supervisor renewed it on every pass, for ever.
    clock = Clock(rig)
    oid, _ = _session_row(rig)
    sup = _sup(rig, clock)
    of = S.session_executor_of(rig.store, Ender(StopResult(False, "no")), agent_id=rig.row(oid)["agent_id"],
                               fire_state=DEAD)
    _revoke(rig, oid)
    passes = []
    for _ in range(2):
        clock.advance(400)
        out = (_sup(rig, clock) if fresh else sup).reap(of)
        passes.append((out.renewed, out.frozen))
    assert passes == [([], [oid]), ([], [oid])]
    # a renewal at t = 400 would reach 400 + LEASE_S. Just past the launch's lease, only a never-renewed one has lapsed
    clock.advance(supervise.LEASE_S + 50 - 800)
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, clock()) is True        # lapse recovery fires: the lease was not renewed past it
    assert rig.row(oid)["state"] == "stopped" and rig.row(oid)["reason"] == "crash_recovered"


def _held_through_three_passes(rig, oid, of, att):
    """Three reaps 400 s apart on one Supervisor: every pass renews ``oid``, and just past the lease the FIRST pass
    wrote, takeover still refuses (the later renewals moved it on). Returns the passes."""
    clock = Clock(rig)
    sup = _sup(rig, clock)
    outs = []
    for _ in range(3):
        clock.advance(400)
        outs.append(sup.reap(of, attention=att.upsert, resolve=att.resolve))
        assert outs[-1].renewed == [oid] and outs[-1].frozen == []
    clock.advance(supervise.LEASE_S + 50 - 800)         # t = 400 + LEASE_S + 50: the first pass's lease has lapsed
    with rig.store.write() as c:
        assert ctl.takeover(c, oid, clock()) is False       # held: never taken over, slot and reservation kept
    assert rig.row(oid)["state"] == "launched" and rig.taken(oid) == ([(1,)], [("open",)])
    return outs


@pytest.mark.parametrize("state", [ALIVE, FOGGY], ids=["alive", "unreadable"])
def test_a_session_whose_fire_is_not_proven_dead_keeps_its_lease_and_is_never_taken_over(rig, state):
    # F1 (r1): head ruling (1). A live or unreadable fire is held exactly as before S3: renewed, one attention row,
    # never frozen into a takeover that would free the slot under a session that may still run.
    oid, _ = _session_row(rig)
    launcher = Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=state)
    att = Attention()
    _revoke(rig, oid)
    outs = _held_through_three_passes(rig, oid, of, att)
    assert all(o.asked == [oid] and o.unconfirmed == [oid] for o in outs)
    assert launcher.ended == [] and att.calls == 1


def test_another_agents_session_row_is_never_asked_and_keeps_its_lease(rig):
    # F1 (r1): an executor_of scoped to another agent gives no executor; nothing was asked, so nothing is frozen
    oid, _ = _session_row(rig)
    launcher = Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id="ag-someone-else", fire_state=DEAD)
    att = Attention()
    _revoke(rig, oid)
    outs = _held_through_three_passes(rig, oid, of, att)
    assert all(o.asked == [] and o.unconfirmed == [oid] for o in outs)
    assert launcher.ended == [] and att.calls == 1


@pytest.mark.parametrize("why", ["no-end-session", "no-record"])
def test_a_refusal_before_the_dead_fire_proof_keeps_the_lease(rig, why):
    # F1 (r1): the launcher has no end_session, or the record cannot prove the death: refused before the proof, held
    oid, _ = _session_row(rig, fire_record=why != "no-record")
    launcher = Stub() if why == "no-end-session" else Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    _revoke(rig, oid)
    _held_through_three_passes(rig, oid, of, Attention())


def test_a_dead_session_that_is_neither_due_nor_stale_is_renewed_and_not_ended(rig):
    # the withheld renewal is given back when the row is not one to stop this pass (known edge: a dead session early in
    # its window is renewed until it is due)
    oid, _ = _session_row(rig)
    launcher = Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    clock = Clock(rig)
    sup = _sup(rig, clock)
    leases = []
    for _ in range(2):
        clock.advance(60)
        out = sup.reap(of)
        assert out.renewed == [oid] and out.asked == [] and out.frozen == []
        leases.append(rig.row(oid)["lease_until"])
    assert leases[0] < leases[1] and launcher.ended == []


def test_a_withheld_row_whose_stop_did_not_re_prove_the_death_is_renewed_not_frozen(rig):
    # the probe before the renewals read the fire dead; the stop's own read did not (a record rewritten between them):
    # the freeze follows the stop's proof, never the probe's
    oid, _ = _session_row(rig)
    reads = iter([DEAD, FOGGY])
    launcher = Ender()
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"],
                               fire_state=lambda r, d: next(reads)(r, d))
    clock = Clock(rig)
    sup = _sup(rig, clock)
    _revoke(rig, oid)
    clock.advance(60)
    out = sup.reap(of)
    assert out.asked == [oid] and out.unconfirmed == [oid] and out.renewed == [oid] and out.frozen == []
    assert launcher.ended == []


def test_a_dead_session_deferred_past_the_stop_budget_is_renewed_not_frozen(rig, monkeypatch):
    # nothing was asked, so the withheld renewal is given back at the deferral
    oid, _ = _session_row(rig)
    launcher = Ender(StopResult(False, "no"))
    of = S.session_executor_of(rig.store, launcher, agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    clock = Clock(rig)
    sup = _sup(rig, clock)
    _revoke(rig, oid)
    real = sup._is_stale

    def slow_stale(row):
        clock.advance(supervise.STOP_BUDGET_S)          # the classification ate the whole stop budget
        return real(row)
    monkeypatch.setattr(sup, "_is_stale", slow_stale)
    before = rig.row(oid)["lease_until"]
    clock.advance(60)
    out = sup.reap(of)
    assert out.deferred == [oid] and out.renewed == [oid] and out.frozen == [] and launcher.ended == []
    assert rig.row(oid)["lease_until"] > before


def test_a_confirmed_stop_whose_settle_wrote_nothing_is_renewed_not_frozen(rig, monkeypatch):
    # the end WAS proven: the row is not frozen; the next pass asks again (settle_errors names it)
    oid, _ = _session_row(rig)
    of = S.session_executor_of(rig.store, Ender(), agent_id=rig.row(oid)["agent_id"], fire_state=DEAD)
    clock = Clock(rig)
    sup = _sup(rig, clock)
    _revoke(rig, oid)
    monkeypatch.setattr(sup, "_settle_stopped", lambda occ, usage, reason=None: ctl.Ended(False))
    clock.advance(60)
    out = sup.reap(of)
    assert out.settle_errors == {oid: "not_settled"} and out.renewed == [oid] and out.frozen == []
    assert rig.row(oid)["state"] == "launched"


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
    assert out.asked == [oid] and out.ended == [oid] and out.unconfirmed == [] and out.frozen == []
    assert (row["state"], row["reason"]) == ("stopped", "link_not_live")   # the revoke drops the link
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
        for _ in range(100):      # a venv launcher re-execs the real interpreter (macOS): wait for two equal reads
            seen = process_identity(proc.pid)
            if seen is not None and seen == ident and oid in seen[1].split():
                break
            ident = seen
            time.sleep(0.15)
        assert ident is not None and oid in ident[1].split()
        assert S._fire_state(_rec(proc.pid, ident[0], ident[1], oid), 5) == "matches"
        assert proc.poll() is None                      # still alive: the reader sent nothing
    finally:
        proc.kill()
        proc.wait()
