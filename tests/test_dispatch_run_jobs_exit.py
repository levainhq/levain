"""S14 R2 (`levain.dispatch.run_jobs`): exit and stop, on the same c6 rig as R1's `test_dispatch_run_jobs.py` (real CONTROL
with a signed two-route map, the real job store, the real session index; only the slot runtime and the usage sources are
fakes). The rig's slot runtime does not bind a ticket to a session through CONTROL's lease, so a run's ticket stays
`unbound` there and the run's session is the index record's: what the tests read of a run is what the runner reads (the
index record, `RUN-TICKET-STATUS`, the store's taken artifacts), never what the runner says it did.

THE PATHS, each ending in its named cause only after END is proven:
* exit: every run `ended` and every artifact taken -> `verifying` (`runs_exited`), refused by the STORE otherwise;
* a run that ended with no artifact past the grace -> `run_failed`;
* a partial start, a standing mint refusal, a start a crash cut short -> `run_failed`;
* an overrun -> `budget_cap`; cancels; an ended envelope's `to_stop`; a lapsed lease;
* a stop whose END is unproven records nothing.
And the restart: a fresh runner over the same index and CONTROL neither re-mints nor loses a run."""
from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from levain.agents import session_launcher as L
from levain.cockpit.consent import parse_stamp
from levain.dispatch import cli as dcli
from levain.dispatch.jobs import EnvelopeEnd, JobStore, TakenArtifact, TransitionRefused
from levain.dispatch.run_jobs import ARTIFACT_GRACE_S, JobRunner, lineage_overrun_reader, run_key
from levain.slot import Refused
from tests.test_agents_session_launcher import FakeSources
from tests.test_dispatch_contract import NOW, POLICY, registry, submission, template
from tests.test_dispatch_envelope import FakeEnvelope
from tests.test_dispatch_run_jobs import (MODELS, Runtime, World, cdir, keys, log, pytestmark, py, store,  # noqa: F401
                                          w)

NOW_PLUS = NOW + timedelta(minutes=2)


class Killed(BaseException):
    """The runner process dying: nothing after it runs, so no handler of the dead process gets to undo anything."""


def runner(w: World, **kw) -> JobRunner:
    return JobRunner(w.jobs, w.launcher, w.port, w.cfg.active, **{"stager": w.stager, **kw})


def fresh_runner(w: World, **kw):
    """A runner in a NEW process: a new launcher and runtime over the SAME session index, CONTROL and job store."""
    rt = Runtime(w.stager.stage, w.tmp / "staging")
    ln = L.RealLauncher(w.port, rt, FakeSources(), w.index, auth="brokered")
    return JobRunner(w.jobs, ln, w.port, w.cfg.active, **{"stager": w.stager, **kw}), ln, rt


def run(w: World, r: JobRunner | None = None, **jobkw):
    job = w.job(**jobkw)
    started = (r or w.runner).run_job(job.id)
    assert started.refused is None and started.released
    return job, started


def end(w: World, i: int) -> None:
    """Run ``i``'s gated process exits clean (the launcher's child answers it only after S1 END)."""
    w.runtime.children[i].process.returncode = 0


def take(w: World, job, i: int, launcher=None, *, bundle: bool = True) -> TakenArtifact:
    """Run ``i``'s artifact taken; ``bundle=False`` is a nothing-new run's (no bundle digest, no copy)."""
    ph = (launcher or w.launcher).run_phase(run_key(job.id, i))
    assert ph.session is not None and ph.ticket is not None, ph
    t = TakenArtifact(ph.session, ph.ticket, "sha256:" + f"{i + 1:064x}",
                      "sha256:" + f"{i + 101:064x}" if bundle else None, f"takes/{i}/bundle" if bundle else None, 0, 0)
    assert w.jobs.record_take(t) is True
    return t


def cause_of(job) -> str:
    return job.history[-1].cause


def evidence_of(job) -> dict:
    return json.loads(job.history[-1].evidence)


def index_states(w: World, job, k: int = 2) -> list[str | None]:
    return [(w.index.get(run_key(job.id, i)) or {}).get("state") for i in range(k)]


# ---- exit: running -> verifying only with every artifact taken ----------------------------------------------------------

def test_runs_exit_to_verifying_only_when_every_run_ended_and_every_artifact_is_taken(w):
    job, _ = run(w)
    assert w.runner.settle_job(job.id).action == "waiting"                    # both runs live
    end(w, 0)
    end(w, 1)
    s = w.runner.settle_job(job.id)
    assert (s.action, s.detail) == ("waiting", "artifacts-not-taken") and w.jobs.get(job.id).state == "running"
    assert index_states(w, job) == ["ended", "ended"]                         # END proven and recorded, by the poll
    t0 = take(w, job, 0)
    assert w.runner.settle_job(job.id).action == "waiting" and w.jobs.get(job.id).state == "running"
    t1 = take(w, job, 1)
    done = w.runner.settle_job(job.id)
    assert (done.action, done.cause) == ("verifying", "runs_exited")
    out = w.jobs.get(job.id)
    assert out.state == "verifying" and cause_of(out) == "runs_exited"
    ev = evidence_of(out)
    assert ev["0"]["manifest_digest"] == t0.manifest_digest and ev["1"]["ticket"] == t1.ticket
    # it is over: a second pass touches nothing
    assert w.runner.settle_job(job.id).action == "skipped"


def test_the_store_refuses_runs_exited_without_every_artifact_whatever_the_runner_read(w):
    job, _ = run(w)
    end(w, 0)
    end(w, 1)
    t0 = take(w, job, 0)
    assert w.runner.settle_job(job.id).action == "waiting"                    # the poll records both runs `ended`
    ph1 =w.launcher.run_phase(run_key(job.id, 1))
    runs = {0: (t0.session, t0.ticket), 1: (ph1.session, ph1.ticket)}
    with pytest.raises(TransitionRefused, match="no taken artifact"):
        w.jobs.runs_exited(job.id, runs)
    with pytest.raises(TransitionRefused, match="has 2 runs"):
        w.jobs.runs_exited(job.id, {0: runs[0]})
    with pytest.raises(TransitionRefused, match="has 2 runs"):
        w.jobs.runs_exited(job.id, {0: runs[0], 1: runs[1], 2: runs[1]})
    assert w.jobs.get(job.id).state == "running"
    # the mutation: a runner whose own reading says every artifact is taken still cannot move the job
    r = runner(w)
    real = r._view
    r._view = lambda j, i: dataclasses.replace(real(j, i), taken=True)
    s = r.settle_job(job.id)
    assert (s.action, s.detail) == ("waiting", "runs-exited-refused") and w.jobs.get(job.id).state == "running"
    take(w, job, 1)
    assert runner(w).settle_job(job.id).action == "verifying"


def test_a_run_ended_with_no_artifact_past_the_grace_fails_run_failed_after_the_other_run_is_stopped(w):
    now = [1000.0]
    w.launcher.clock = lambda: now[0]
    r = runner(w, clock=lambda: now[0])
    job, _ = run(w, r)
    end(w, 0)
    take(w, job, 0)
    s = r.settle_job(job.id)                                                  # run 0 ended, run 1 still live
    assert s.action == "waiting"
    end(w, 1)
    assert r.settle_job(job.id).detail == "artifacts-not-taken"
    now[0] += ARTIFACT_GRACE_S - 1
    assert r.settle_job(job.id).action == "waiting"
    now[0] += 2
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "run_failed")
    ev = evidence_of(out)
    assert ev["run"] == 1 and ev["name"] == "no-artifact" and ev["stopped"] is True
    assert index_states(w, job) == ["ended", "ended"]


def test_a_live_run_is_stopped_before_a_dead_ones_missing_artifact_is_recorded(w):
    now = [1000.0]
    w.launcher.clock = lambda: now[0]
    r = runner(w, clock=lambda: now[0])
    job, _ = run(w, r)
    end(w, 0)
    r.settle_job(job.id)
    now[0] += ARTIFACT_GRACE_S + 1                                            # run 0 left no artifact; run 1 still runs
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    assert w.runtime.children[1].aborted == 1                                 # kill, then record
    assert index_states(w, job) == ["ended", "ended"]
    assert evidence_of(w.jobs.get(job.id))["run"] == 0


# ---- after the take: a refusal fails the job, once END is proven -----------------------------------------------------------

def test_a_run_refused_after_the_first_started_fails_the_job_run_failed_with_the_first_run_aborted(w):
    job = w.job()
    real = w.launcher.spawn

    def spawn(launch):
        if launch.binding.run_index == 1:
            raise L.LauncherRefused("mint-refused:no-free-slot", "none", reason_class="transient")
        return real(launch)

    w.launcher.spawn = spawn
    started = w.runner.run_job(job.id)
    assert started.refused == "mint-refused:no-free-slot" and started.stopped == "run_failed"
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "run_failed") and started.job.state == "failed"
    assert evidence_of(out) == {"stopped": True, "why": "run 1 did not start: mint-refused:no-free-slot", "run": 1,
                                "name": "mint-refused:no-free-slot"}
    assert w.runtime.children[0].aborted == 1 and w.runtime.children[0].released is False
    assert index_states(w, job)[0] == "ended" and w.minted() == 1


def test_a_standing_mint_refusal_after_the_take_fails_the_job_and_mints_nothing(w):
    job = w.job(cents=2000)                                                   # 10_000_000 a run: over the ceiling
    started = w.runner.run_job(job.id)
    assert started.refused.startswith("mint-refused:ticket-ceiling") and started.stopped == "run_failed"
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "run_failed")
    assert evidence_of(out)["name"].startswith("mint-refused:ticket-ceiling") and evidence_of(out)["run"] == 0
    with pytest.raises(Refused) as e:
        w.port.call("RUN-TICKET-STATUS", attempt_id=run_key(job.id, 0))
    assert e.value.name == "attempt-unknown" and w.runtime.starts == []


def test_a_stop_whose_end_is_unproven_records_nothing_and_the_next_pass_records_it(w, monkeypatch):
    job, _ = run(w)
    monkeypatch.setattr(w.runtime, "end", lambda session: False)
    s = w.runner.stop_job(job.id, "run_failed", why="test")
    assert s.action == "unproven" and w.jobs.get(job.id).state == "running"
    assert index_states(w, job) == ["started", "started"]
    monkeypatch.undo()
    s = w.runner.stop_job(job.id, "run_failed", why="test")
    assert (s.action, s.cause) == ("stopped", "run_failed") and w.jobs.get(job.id).state == "failed"
    assert index_states(w, job) == ["ended", "ended"]


# ---- the transient retry, the preflight moves --------------------------------------------------------------------------

def test_a_transient_mint_refusal_of_the_first_run_leaves_the_job_running_and_the_next_tick_mints_again(w):
    job = w.job()
    real = w.launcher.spawn
    calls = []

    def spawn(launch):
        calls.append(launch.binding.run_index)
        if len(calls) == 1:
            raise L.LauncherRefused("mint-refused:no-free-slot", "none", reason_class="transient")
        return real(launch)

    w.launcher.spawn = spawn
    # the launcher's refusal leaves no record when raised from here; give it the one a real transient mint refusal leaves
    w.launcher.refuse_key(run_key(job.id, 0), "mint:no-free-slot", occurrence_id=job.id, reason_class="transient")
    started = w.runner.run_job(job.id)
    assert started.refused == "mint-refused:no-free-slot" and started.stopped is None
    assert w.jobs.get(job.id).state == "running" and w.minted() == 0
    out = w.runner.tick()
    assert [(s.action) for s in out] == ["waiting"] and calls == [0, 0, 1]
    assert w.jobs.get(job.id).state == "running" and w.minted() == 2
    assert index_states(w, job) == ["started", "started"] and [c.released for c in w.runtime.children] == [True, True]


def test_the_ticks_preflight_fails_a_standing_refusal_untaken_and_waits_out_a_transient_one(w, monkeypatch):
    bad = w.job(models=(MODELS[0], "nobody/none"))
    out = w.runner.tick()
    assert [(s.action, s.cause) for s in out] == [("stopped", "run_failed")]
    failed = w.jobs.get(bad.id)
    assert (failed.state, cause_of(failed)) == ("failed", "run_failed")
    assert evidence_of(failed)["name"] == "route-resolve:no-brokered-route" and evidence_of(failed)["taken"] is False
    assert w.minted() == 0
    # a transient preflight: signed -> queued (slot_wait), retried the next tick, which takes it
    job = w.job(cents=151)                                                    # a different contract, same files: a new job
    # (the first job's failure released its write files)
    real = w.launcher.resolve_route
    calls = []

    def flaky(model):
        calls.append(model)
        if len(calls) == 1:
            raise L.LauncherRefused("route-resolve-failed", "ConnectionError", reason_class="transient")
        return real(model)

    monkeypatch.setattr(w.launcher, "resolve_route", flaky)
    assert [s.action for s in w.runner.tick()] == ["waiting"]
    q = w.jobs.get(job.id)
    assert q.state == "queued" and cause_of(q) == "slot_wait" and w.minted() == 0
    assert [s.action for s in w.runner.tick()] == ["waiting"]
    assert w.jobs.get(job.id).state == "running" and w.minted() == 2


# ---- overrun, cancels, envelope, lease -------------------------------------------------------------------------------------

def test_an_overrun_of_any_run_stops_the_others_and_records_budget_cap_even_with_every_artifact_taken(w):
    job, _ = run(w)
    run_id = w.index.get(run_key(job.id, 1))["run_id"]
    r = w.runner                                                              # the runner that holds the gated children
    r.overran = lambda rid: rid == run_id
    end(w, 0)
    take(w, job, 0)
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "budget_cap")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "budget_cap") and "overran" in evidence_of(out)["why"]
    assert w.runtime.children[1].aborted == 1 and index_states(w, job) == ["ended", "ended"]
    # and with every run ended and every artifact taken, an overrun still never verifies
    job2, _ = run(w, runner(w), cents=151, write=["src/b.py"])
    rid2 = w.index.get(run_key(job2.id, 0))["run_id"]
    end(w, 2)
    end(w, 3)
    take(w, job2, 0)
    take(w, job2, 1)
    r2 = runner(w, overran=lambda rid: rid == rid2)
    assert r2.settle_job(job2.id).cause == "budget_cap" and w.jobs.get(job2.id).state == "failed"


def test_the_lineage_reader_sees_an_overrun_row_of_the_run_and_nothing_else(tmp_path):
    sink = tmp_path / "lineage.jsonl"
    r = "r-" + "a" * 16
    rows = [{"format": "levain-dispatch-lineage/3", "run_id": r, "overrun": None},
            {"format": "levain-dispatch-lineage/3", "run_id": "r-" + "b" * 16, "overrun": 5},
            {"format": "levain-dispatch-lineage/3", "run_id": r, "overrun": 0}]
    sink.write_text("".join(json.dumps(x) + "\n" for x in rows) + '{"torn": ')
    read = lineage_overrun_reader(sink)
    assert read(r) is False and read("r-" + "b" * 16) is True and read("") is None
    with sink.open("a") as fh:
        fh.write('\n' + json.dumps({"format": "levain-dispatch-lineage/3", "run_id": r, "overrun": 1}) + "\n")
    assert read(r) is True


def test_an_absent_lineage_sink_is_unknown_never_no_overrun(tmp_path):
    """S14 L3 F5 (DECISION): a run CONTROL minted is expected to have a sink; an absent one is no evidence. Asserts the reader answers None (UNKNOWN: the job waits, bounded by its lease) and, once the sink exists
    with an overrun row, True. RED on the old code: ENOENT answered False, which the runner reads as "no overrun", so
    a job whose sink was missing moved to verifying."""
    sink = tmp_path / "absent.jsonl"
    r = "r-" + "a" * 16
    assert lineage_overrun_reader(sink)(r) is None
    sink.write_text(json.dumps({"format": "levain-dispatch-lineage/3", "run_id": r, "overrun": 3}) + "\n")
    assert lineage_overrun_reader(sink)(r) is True


@pytest.mark.parametrize("cause", ["planner_cancel", "human_cancel"])
def test_a_cancel_the_hook_names_with_no_kept_request_is_never_recorded(w, cause):
    # s14fix2 (codex L221, complement L22): the store records a cancel only from the party's kept request, so a hook
    # that names one the store never kept records nothing (an unsigned human_cancel above all)
    job, _ = run(w)
    done = runner(w, cancel_for=lambda j: cause).settle_job(job.id)
    assert done.action == "skipped" and "stop-refused" in done.detail
    assert w.jobs.get(job.id).state == "running"
    # the known edge: the runs were stopped before the store refused the record; past the grace the job fails
    assert index_states(w, job) == ["ended", "ended"]
    late = runner(w, grace_s=0).settle_job(job.id)
    assert (late.action, late.cause, late.detail) == ("stopped", "run_failed", "no-artifact")


def test_a_cancel_hook_that_names_no_cancel_cause_or_raises_stops_nothing(w):
    job, _ = run(w)
    for hook in (lambda j: "budget_cap", lambda j: (_ for _ in ()).throw(RuntimeError("x"))):
        assert runner(w, cancel_for=hook).settle_job(job.id).action == "waiting"
    assert w.jobs.get(job.id).state == "running" and index_states(w, job) == ["started", "started"]


def test_an_ended_envelope_stops_its_running_job_through_to_stop(w):
    env = FakeEnvelope("env-1", str(w.repo))
    reg = registry(template(executor="claude", models=MODELS, k=2))
    job, _cap = w.jobs.file_in_envelope(
        submission(w.repo, w.base, budget={"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}),
        resolve_template=reg, policy=POLICY, filer_session="coord-1", envelope=env, export=dcli.readset_export)
    r = runner(w, envelope_for=lambda j: env)
    started = r.run_job(job.id)
    assert started.released and w.jobs.get(job.id).state == "running"
    ended = w.jobs.end_envelope("env-1")
    assert ended == EnvelopeEnd(failed=(), to_stop=(job.id,)) and w.jobs.get(job.id).state == "running"
    [done] = r.stop_envelope(ended)
    assert (done.action, done.cause) == ("stopped", "envelope_ended")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "envelope_ended") and evidence_of(out)["envelope_id"] == "env-1"
    assert index_states(w, job) == ["ended", "ended"]


def test_a_running_job_past_its_lease_is_stopped_then_recorded_lease_lapsed(w):
    job, _ = run(w)
    assert w.runner.settle_job(job.id).action == "waiting"
    w.jobs._clock.now = parse_stamp(w.jobs.get(job.id).expires_at)
    done = w.runner.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "lease_lapsed")
    assert w.jobs.get(job.id).state == "failed" and index_states(w, job) == ["ended", "ended"]


# ---- the restart reconcile -----------------------------------------------------------------------------------------------

def test_a_runner_killed_between_the_mint_and_the_start_is_reconciled_by_a_fresh_one_that_mints_nothing(w, monkeypatch):
    job = w.job()
    monkeypatch.setattr(w.launcher, "_undo", lambda *a, **k: None)           # the dead process undoes nothing
    monkeypatch.setattr(w.runtime, "start", lambda req: (_ for _ in ()).throw(Killed()))
    with pytest.raises(Killed):
        w.runner.run_job(job.id)
    # what the dead runner left: the job taken, run 0 minted (a ticket CONTROL holds unbound) and never started, run 1 unminted
    assert w.jobs.get(job.id).state == "running" and w.minted() == 1
    assert index_states(w, job) == ["minted", None] and w.runtime.starts == []
    ticket = w.port.call("RUN-TICKET-STATUS", attempt_id=run_key(job.id, 0))
    assert ticket["state"] == "unbound"

    fresh, ln, rt = fresh_runner(w)
    out = fresh.tick()
    assert [(s.action, s.cause) for s in out] == [("stopped", "run_failed")]
    done = w.jobs.get(job.id)
    assert (done.state, cause_of(done)) == ("failed", "run_failed")
    ev = evidence_of(done)
    assert ev["run"] == 0 and ev["name"] == "crashed-between-mint-and-start"
    # nothing was re-minted, nothing started; the run was not lost: its ticket is CANCELLED in CONTROL and its record `undone`
    assert w.minted() == 1 and rt.starts == []
    assert w.port.call("RUN-TICKET-STATUS", attempt_id=run_key(job.id, 0))["state"] == "cancelled"
    assert index_states(w, job) == ["undone", None]
    # and the key can never be minted again
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(_launch_of(job, 0))
    assert e.value.name == "session-key-already-used" and w.minted() == 1


def _launch_of(job, i):
    from levain.agents.session import DispatchBinding, SessionLaunch
    return SessionLaunch(job.id, run_key(job.id, i), "claude", MODELS[i], "brief", "brokered", None, usd_micros=750_000,
                         binding=DispatchBinding(job.id, i, job.digest))


def test_a_runner_killed_after_the_first_run_started_is_reconciled_by_ending_it_before_the_job_is_recorded(w, monkeypatch):
    job = w.job()
    real = w.launcher.spawn

    def spawn(launch):
        if launch.binding.run_index == 1:
            raise Killed()
        return real(launch)

    monkeypatch.setattr(w.launcher, "spawn", spawn)
    with pytest.raises(Killed):
        w.runner.run_job(job.id)
    assert index_states(w, job) == ["started", None] and w.minted() == 1
    fresh, ln, rt = fresh_runner(w)
    rt_ends = []
    monkeypatch.setattr(rt, "end", lambda session: rt_ends.append(session) or True)
    [s] = fresh.tick()
    assert (s.action, s.cause) == ("stopped", "run_failed")
    done = w.jobs.get(job.id)
    assert (done.state, evidence_of(done)["name"], evidence_of(done)["run"]) == ("failed", "partial-start", 1)
    assert len(rt_ends) == 1 and index_states(w, job) == ["ended", None]      # END proved before the record
    assert w.minted() == 1 and rt.starts == []


def test_a_fresh_runner_over_live_runs_waits_mints_nothing_and_exits_them_once_the_slots_are_released(w, monkeypatch):
    job, _ = run(w)
    assert w.minted() == 2
    fresh, ln, rt = fresh_runner(w)
    assert [s.action for s in fresh.tick()] == ["waiting"]                    # the runs are live: nothing to do, nothing minted
    assert w.minted() == 2 and rt.starts == [] and index_states(w, job) == ["started", "started"]
    assert w.jobs.get(job.id).state == "running"
    # CONTROL releases each run's ticket (the slot was scrubbed): the fresh runner proves END and records `ended`
    real = ln._status_answer

    def released(key):
        kind, ans = real(key)
        return kind, {**ans, "state": "released", "session": (w.index.get(key) or {}).get("session")}

    monkeypatch.setattr(ln, "_status_answer", released)
    assert fresh.tick()[0].detail == "artifacts-not-taken" and index_states(w, job) == ["ended", "ended"]
    take(w, job, 0, ln)
    take(w, job, 1, ln)
    [s] = fresh.tick()
    assert (s.action, s.cause) == ("verifying", "runs_exited") and w.jobs.get(job.id).state == "verifying"
    assert w.minted() == 2


def test_a_tick_reconciles_running_jobs_before_it_picks_up_a_new_one(w):
    first, _ = run(w)
    second = w.job(cents=151, write=["src/b.py"])
    order = []
    r = runner(w)
    real_settle, real_pick = r.settle_job, r._pick
    r.settle_job = lambda jid: order.append(("settle", jid)) or real_settle(jid)
    r._pick = lambda j: order.append(("pick", j.id)) or real_pick(j)
    r.tick()
    assert order[0] == ("settle", first.id) and ("pick", second.id) in order


def test_the_launcher_settle_run_answers_by_what_it_read_and_never_by_a_guess(w):
    job = w.job()
    ln = w.launcher
    key = run_key(job.id, 0)
    assert ln.settle_run(key) == "none"
    # a pending record CONTROL never minted: zero, recorded undone
    ln.index.create(key, "pending", occurrence_id=job.id, created_s=1.0, auth="brokered")
    assert ln.settle_run(key) == "zero" and w.index.get(key)["state"] == "undone"
    # CONTROL that does not answer proves nothing
    key1 = run_key(job.id, 1)
    ln.index.create(key1, "minted", occurrence_id=job.id, created_s=1.0, auth="brokered", run_id="r-" + "0" * 16)
    w.port.control = SimpleNamespace(handle=lambda *a, **k: {"ok": False, "refused": "control-down"})
    assert ln.settle_run(key1) == "unproven" and w.index.get(key1)["state"] == "minted"


# ---- the cancel source: the store's durable stop request ----------------------------------------------------------------

def test_a_planners_cancel_of_a_running_job_is_refused_and_kept_as_a_request_the_runner_acts_on(w):
    from levain.dispatch.jobs import CapabilityRefused
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    assert w.jobs.stop_requested(job.id) is None
    # a wrong capability keeps nothing
    with pytest.raises(CapabilityRefused):
        w.jobs.cancel_by_planner(job.id, "not-the-capability")
    assert w.jobs.stop_requested(job.id) is None
    # the filer's own capability: refused (running), and the request stays
    cap = w.filed_cap
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        w.jobs.cancel_by_planner(job.id, cap)
    assert w.jobs.get(job.id).state == "running" and w.jobs.stop_requested(job.id) == "planner_cancel"
    with pytest.raises(TransitionRefused):
        w.jobs.cancel_by_planner(job.id, cap)                                   # a second request changes nothing
    done = runner(w, cancel_for=store_cancel_for(w.jobs)).settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "planner_cancel")
    assert (w.jobs.get(job.id).state, cause_of(w.jobs.get(job.id))) == ("cancelled", "planner_cancel")
    assert index_states(w, job) == ["ended", "ended"]


def test_a_humans_cancel_of_a_running_job_keeps_its_proof_and_the_runner_records_it(w, keys, log):
    import sqlite3
    from tests.test_dispatch_jobs import cancel_item
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    payload, sig = cancel_item(job, keys["laptop"], at=NOW_PLUS)
    w.jobs._clock.now = NOW_PLUS + timedelta(minutes=1)
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        w.jobs.cancel_by_human(job.id, payload, sig, log.source)
    assert w.jobs.stop_requested(job.id) == "human_cancel"
    with sqlite3.connect(w.jobs.path) as conn:
        assert conn.execute("SELECT cause, signer, signature FROM stop_requests").fetchall() == [
            ("human_cancel", keys["laptop"].fp, sig)]
        assert conn.execute("SELECT COUNT(*) FROM cancel_signatures").fetchone() == (0,)
    done = runner(w, cancel_for=store_cancel_for(w.jobs)).settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "human_cancel")
    # L1 F5 (decided): once recorded, the human's cancel of a running job holds its cancel_signatures row and its
    # history line names the signer and the reason, as a cancel of a job that was not running does
    with sqlite3.connect(w.jobs.path) as conn:
        assert conn.execute("SELECT signature, signer_fp FROM cancel_signatures").fetchall() == [
            (sig, keys["laptop"].fp)]
    ev = evidence_of(w.jobs.get(job.id))
    assert ev["stopped"] is True and ev["signer_fp"] == keys["laptop"].fp and ev["reason"] == payload["reason"]


def test_a_humans_request_honoured_at_the_verifying_edge_also_keeps_its_signature(w, keys, log):
    import sqlite3
    from tests.test_dispatch_jobs import cancel_item
    job, _ = run(w)
    _ended_and_taken(w, job)
    payload, sig = cancel_item(job, keys["laptop"], at=NOW_PLUS)
    w.jobs._clock.now = NOW_PLUS + timedelta(minutes=1)
    with pytest.raises(TransitionRefused):
        w.jobs.cancel_by_human(job.id, payload, sig, log.source)
    done = runner(w).settle_job(job.id)                                       # a runner with no cancel hook at all
    assert (done.action, done.cause) == ("stopped", "human_cancel")
    with sqlite3.connect(w.jobs.path) as conn:
        assert conn.execute("SELECT signature FROM cancel_signatures").fetchall() == [(sig,)]
    assert evidence_of(w.jobs.get(job.id))["signer_fp"] == keys["laptop"].fp


def test_a_cancel_that_does_not_verify_keeps_no_request(w, keys, log):
    from levain.dispatch.jobs import SignatureRefused
    from tests.test_dispatch_jobs import cancel_item
    job, _ = run(w)
    payload, sig = cancel_item(job, keys["stranger"], at=NOW_PLUS)
    w.jobs._clock.now = NOW_PLUS + timedelta(minutes=1)
    with pytest.raises(SignatureRefused):
        w.jobs.cancel_by_human(job.id, payload, sig, log.source)
    assert w.jobs.stop_requested(job.id) is None


def test_a_job_that_is_not_running_has_no_request_and_a_cancel_of_it_is_unchanged(w):
    job = w.job()
    assert w.jobs.stop_requested(job.id) is None
    assert w.jobs.cancel_by_planner(job.id, w.filed_cap).state == "cancelled"
    assert w.jobs.stop_requested(job.id) is None


def _ended_and_taken(w: World, job) -> None:
    """Both runs exit and both artifacts are taken: the next pass would move the job to `verifying`."""
    end(w, 0)
    end(w, 1)
    assert w.runner.settle_job(job.id).action == "waiting"                    # the poll records both runs `ended`
    take(w, job, 0)
    take(w, job, 1)


def test_a_request_kept_after_the_runner_read_none_is_honoured_by_the_verifying_edge(w):
    # the race (L1 F3): the runner reads no request, the planner's request lands, then both runs have exited: the
    # verifying edge itself reads the kept request and records the cancel, so the job never verifies
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    _ended_and_taken(w, job)
    read = store_cancel_for(w.jobs)
    landed = []

    def hook(j):
        answer = read(j)
        if not landed:
            with pytest.raises(TransitionRefused):
                w.jobs.cancel_by_planner(j.id, w.filed_cap)
            landed.append(True)
        return answer

    done = runner(w, cancel_for=hook).settle_job(job.id)
    assert landed and (done.action, done.cause) == ("stopped", "planner_cancel")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("cancelled", "planner_cancel")
    ev = evidence_of(out)
    assert ev["stopped"] is True and set(ev["runs"]) == {"0", "1"}           # the runs it took are still named


def test_the_store_honours_a_kept_request_at_runs_exited_whoever_calls_it(w):
    job, _ = run(w)
    _ended_and_taken(w, job)
    with pytest.raises(TransitionRefused):
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    runs = {i: (lambda ph: (ph.session, ph.ticket))(w.launcher.run_phase(run_key(job.id, i))) for i in range(2)}
    out = w.jobs.runs_exited(job.id, runs)
    assert (out.state, cause_of(out)) == ("cancelled", "planner_cancel")


def test_a_humans_signed_cancel_is_kept_behind_a_planners_request_and_is_the_one_recorded(w, keys, log):
    # L1 F4 (decided): a human-signed cancel is always recordable, even after a planner's request
    import sqlite3
    from tests.test_dispatch_jobs import cancel_item
    from levain.dispatch.jobs import StopRequested
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    with pytest.raises(StopRequested) as planner:
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    assert planner.value.cause == "planner_cancel" and w.jobs.stop_requested(job.id) == "planner_cancel"
    payload, sig = cancel_item(job, keys["laptop"], at=NOW_PLUS)
    w.jobs._clock.now = NOW_PLUS + timedelta(minutes=1)
    with pytest.raises(StopRequested) as human:
        w.jobs.cancel_by_human(job.id, payload, sig, log.source)
    assert human.value.cause == "human_cancel" and w.jobs.stop_requested(job.id) == "human_cancel"
    with pytest.raises(StopRequested):
        w.jobs.cancel_by_planner(job.id, w.filed_cap)                           # a party's second request: no-op
    assert w.jobs.stop_requested(job.id) == "human_cancel"
    with sqlite3.connect(w.jobs.path) as conn:
        assert sorted(conn.execute("SELECT cause, signature FROM stop_requests")) == [
            ("human_cancel", sig), ("planner_cancel", "")]
    done = runner(w, cancel_for=store_cancel_for(w.jobs)).settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "human_cancel")
    assert cause_of(w.jobs.get(job.id)) == "human_cancel"


def test_stop_requested_reads_none_only_for_a_missing_table_and_raises_any_other_store_error(w, monkeypatch):
    # L1 F6: a locked or corrupt store is never read as "no request" (the runner's hook logs it and retries)
    import sqlite3
    job, _ = run(w)
    monkeypatch.setattr(JobStore, "_kept_request",
                        staticmethod(lambda conn, jid: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))))
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        w.jobs.stop_requested(job.id)
    monkeypatch.undo()
    with sqlite3.connect(w.jobs.path) as conn:
        conn.execute("DROP TABLE stop_requests")
    assert w.jobs.stop_requested(job.id) is None


# ---- s14fix2: the cancel's authority is decided in stop_recorded's own transaction ---------------------------------------

def _signatures(w):
    import sqlite3
    with sqlite3.connect(w.jobs.path) as conn:
        return conn.execute("SELECT signer_fp FROM cancel_signatures").fetchall()


def _kept_human(w, job, keys, log, *, reason="not this one", at=NOW_PLUS):
    from levain.dispatch.jobs import StopRequested
    from tests.test_dispatch_jobs import cancel_item
    payload, sig = cancel_item(job, keys["laptop"], at=at, reason=reason)
    w.jobs._clock.now = at + timedelta(minutes=1)
    with pytest.raises(StopRequested) as kept:
        w.jobs.cancel_by_human(job.id, payload, sig, log.source)
    return kept.value


def test_a_human_cancel_landing_during_the_end_proof_of_a_planner_stop_is_the_one_recorded(w, keys, log):
    # codex L221 / L2 F1 / complement L27: the runner read planner_cancel BEFORE its END proof; the human's signed
    # cancel lands during it. The store re-reads the kept requests as it records, so the human's is recorded with
    # its signature, never the planner's stale one
    from levain.dispatch.jobs import StopRequested
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    with pytest.raises(StopRequested):
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    real, landed = w.launcher.settle_run, []

    def settle(key):
        if not landed:
            landed.append(_kept_human(w, job, keys, log))
        return real(key)
    w.launcher.settle_run = settle
    done = runner(w, cancel_for=store_cancel_for(w.jobs)).settle_job(job.id)
    assert landed and (done.action, done.cause) == ("stopped", "human_cancel")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("cancelled", "human_cancel") and _signatures(w) == [(keys["laptop"].fp,)]
    assert evidence_of(out)["superseded"] == "planner_cancel"


def test_a_kept_human_cancel_outranks_a_lapsed_lease_read_after_the_cancel_hook_faulted(w, keys, log):
    # L2 F2 / complement L41 / deepseek L1: the hook raised (store locked), the lease lapsed: the store still records
    # the human's kept signed cancel, naming the cause it outranked
    import sqlite3
    job, _ = run(w)
    _kept_human(w, job, keys, log)
    r = runner(w, cancel_for=lambda j: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    w.jobs._clock.now = parse_stamp(w.jobs.get(job.id).expires_at) + timedelta(seconds=1)
    done = r.settle_job(job.id)
    out = w.jobs.get(job.id)
    assert (done.cause, out.state, cause_of(out)) == ("human_cancel", "cancelled", "human_cancel")
    assert evidence_of(out)["superseded"] == "lease_lapsed" and _signatures(w) == [(keys["laptop"].fp,)]


def test_a_failed_run_recorded_after_a_kept_human_cancel_records_the_human(w, keys, log):
    job, _ = run(w)
    _kept_human(w, job, keys, log)
    for i in range(2):
        assert w.launcher.settle_run(run_key(job.id, i)) == "ended"
    out = w.jobs.stop_recorded(job.id, "run_failed", run=1, name="no-artifact")
    assert (out.state, cause_of(out)) == ("cancelled", "human_cancel")
    ev = evidence_of(out)
    assert (ev["superseded"], ev["run"], ev["name"], ev["signer_fp"]) == ("run_failed", 1, "no-artifact",
                                                                          keys["laptop"].fp)


def test_an_unsigned_cancel_is_never_recorded_by_the_store(w):
    # complement L22 / codex L221: no kept request, no cancel; a planner's request is never a human's signature
    from levain.dispatch.jobs import StopRequested
    job, _ = run(w)
    for i in range(2):
        assert w.launcher.settle_run(run_key(job.id, i)) == "ended"
    for cause in ("human_cancel", "planner_cancel"):
        with pytest.raises(TransitionRefused, match="kept"):
            w.jobs.stop_recorded(job.id, cause)
    with pytest.raises(StopRequested):
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    with pytest.raises(TransitionRefused, match="human-signed"):
        w.jobs.stop_recorded(job.id, "human_cancel")
    assert w.jobs.get(job.id).state == "running" and _signatures(w) == []


def test_a_second_signed_cancel_keeps_nothing_and_its_receipt_names_the_kept_one(w, keys, log):
    # L2 F3 / gemini L398: the human request is written once (append-only); a second signed cancel of the running job
    # is told so, and the receipt names the KEPT signer and reason, the ones the supervisor records
    from levain.dispatch.run_jobs import store_cancel_for
    job, _ = run(w)
    first = _kept_human(w, job, keys, log, reason="first reason")
    second = _kept_human(w, job, keys, log, reason="second reason", at=NOW_PLUS + timedelta(seconds=5))
    assert (first.already_kept, first.signer_fp, first.reason) == (False, keys["laptop"].fp, "first reason")
    assert (second.already_kept, second.signer_fp, second.reason) == (True, keys["laptop"].fp, "first reason")
    runner(w, cancel_for=store_cancel_for(w.jobs)).settle_job(job.id)
    assert evidence_of(w.jobs.get(job.id))["reason"] == "first reason"


def test_a_planners_second_request_is_told_it_kept_nothing_new(w):
    from levain.dispatch.jobs import StopRequested
    job, _ = run(w)
    with pytest.raises(StopRequested) as one:
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    with pytest.raises(StopRequested) as two:
        w.jobs.cancel_by_planner(job.id, w.filed_cap)
    assert (one.value.already_kept, two.value.already_kept) == (False, True)


def test_budget_evidence_that_does_not_read_never_lets_an_ended_job_verify(w):
    # L3 codex L223: overran() None or raising was read as "no overrun", so a job whose sink was unreadable moved to
    # verifying with every artifact taken; now it is UNKNOWN and the job waits (its lease bounds the wait)
    job, _ = run(w)
    _ended_and_taken(w, job)
    for reader in (lambda rid: None, lambda rid: (_ for _ in ()).throw(OSError("sink")), lambda rid: "yes"):
        done = runner(w, overran=reader).settle_job(job.id)
        assert (done.action, done.detail) == ("waiting", "overrun-unknown") and w.jobs.get(job.id).state == "running"
    assert runner(w, overran=lambda rid: False).settle_job(job.id).action == "verifying"


def test_the_lineage_reader_reads_a_malformed_whole_line_of_the_run_as_unknown(tmp_path):
    rid = "r-" + "a" * 16
    sink = tmp_path / "s.jsonl"
    sink.write_text('{"run_id": "%s", "overrun": \n{"run_id": "%s", "overrun": 0}\n' % (rid, rid))
    assert lineage_overrun_reader(sink)(rid) is None                          # never "no overrun"
    sink.write_text('{"run_id": "%s", "overrun": 0}\n{"run_id": "%s", "ov' % (rid, rid))
    assert lineage_overrun_reader(sink)(rid) is False                         # the torn last line is a write in progress


def test_a_release_that_raises_stops_every_run_and_fails_the_job(w):
    # L3 codex L225 / complement L31: run 0 released, run 1's release raised: _launch escaped un-aborted, run 0 live
    # and run 1 gated until the lease. Now every run is stopped and proven over, then the job fails run_failed
    from tests.test_agents_session_launcher import FakeChild

    class BadRelease(FakeChild):
        def release(self):
            raise RuntimeError("release broke")
    real = w.runtime.start

    def start(req):
        handle = real(req)
        if len(w.runtime.children) == 2:
            w.runtime.children[-1] = child = BadRelease()
            return L.SlotHandle(handle.session, child)
        return handle
    w.runtime.start = start
    job = w.job()
    started = w.runner.run_job(job.id)                                       # never raises past the runner
    assert (started.refused, started.released, started.stopped) == ("release-failed", False, "run_failed")
    out = w.jobs.get(job.id)
    assert (out.state, cause_of(out)) == ("failed", "run_failed") and evidence_of(out)["name"] == "release-failed"
    assert index_states(w, job) == ["ended", "ended"] and w.runtime.children[0].released is True


def test_a_refusal_whose_stop_is_unproven_is_never_reported_stopped(w, monkeypatch):
    # L3 deepseek M1: _refused forwarded the stop's cause even when END was unproven and nothing was recorded, so the
    # tick logged a stop the store never wrote
    job = w.job()
    calls, real_spawn = [], w.launcher.spawn

    def spawn(launch):
        calls.append(1)
        if len(calls) == 2:
            raise L.LauncherRefused("ticket-ceiling:x", "no")
        return real_spawn(launch)
    monkeypatch.setattr(w.launcher, "spawn", spawn)
    monkeypatch.setattr(w.launcher, "settle_run", lambda key: "unproven")
    seen = []
    real_launch = w.runner._launch
    monkeypatch.setattr(w.runner, "_launch", lambda *a: seen.append(real_launch(*a)) or seen[-1])
    done = w.runner._pick(job)                                                # the tick's own path
    assert w.jobs.get(job.id).state == "running"
    assert (seen[0].stopped, seen[0].pending) == (None, "run_failed")
    assert (done.action, done.cause) == ("unproven", "run_failed")


def test_one_job_whose_pass_raises_never_stops_the_tick_for_the_others(w, monkeypatch):
    # L3 deepseek M4: a list comprehension over settle_job let one raising job abort the whole tick, so the jobs after
    # it (and every signed job to pick up) were never touched, tick after tick
    a, _ = run(w)
    queued = w.job(cents=151, write=["src/b.py"])
    real = w.runner.settle_job
    monkeypatch.setattr(w.runner, "settle_job",
                        lambda job_id: (_ for _ in ()).throw(RuntimeError("boom")) if job_id == a.id else real(job_id))
    out = w.runner.tick()
    by_job = {s.job.id: s for s in out if s.job is not None}
    assert (by_job[a.id].action, by_job[a.id].detail) == ("error", "RuntimeError")
    assert queued.id in by_job and by_job[queued.id].action != "error"         # the pass reached the signed job


def test_a_jobs_children_are_dropped_once_its_runs_exit(w):
    # L3 gemini L431: a child the poll never saw over stayed in _children for the process's life after the job exited
    job, _ = run(w)
    _ended_and_taken(w, job)
    w.runner._children[run_key(job.id, 0)] = object()
    assert w.runner.settle_job(job.id).action == "verifying"
    assert not ({run_key(job.id, 0), run_key(job.id, 1)} & set(w.runner._children))


def test_ended_runs_whose_tickets_control_cannot_answer_wait_no_longer_than_the_lease(w):
    # L3 deepseek H1 / gemini L458 (refuted as "forever"): with CONTROL down the runs' tickets read None, so their taken
    # artifacts cannot be looked up and the grace never starts; the lease, read from the store alone and checked first,
    # bounds the wait, and an ended record proves its END without CONTROL
    from types import SimpleNamespace
    job, _ = run(w)
    end(w, 0)
    end(w, 1)
    assert w.runner.settle_job(job.id).action == "waiting"                    # the poll records both runs `ended`
    w.port.control = SimpleNamespace(handle=lambda *a, **k: {"ok": False, "refused": "control-down"})
    assert w.runner.settle_job(job.id).detail == "artifacts-not-taken"
    w.jobs._clock.now = parse_stamp(w.jobs.get(job.id).expires_at)
    done = w.runner.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "lease_lapsed") and w.jobs.get(job.id).state == "failed"
