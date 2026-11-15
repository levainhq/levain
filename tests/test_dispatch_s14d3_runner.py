"""S14 D3 (the runner and the store): an `artifact_failed` note fails the job by name, a nothing-new run fails at once,
and the grace covers a slow import. The follower's rows are in test_dispatch_s14d3_artifact.py (a second module: the
two rigs name different `store` fixtures).

The runner rides test_dispatch_run_jobs_exit's c6 rig (real CONTROL, real job store, real session index; the slot
runtime and usage sources are fakes). Everything root-side is VM residue."""
from __future__ import annotations

import sqlite3  # noqa: F401

import pytest

from levain.dispatch.run_jobs import ARTIFACT_GRACE_S, run_key
from tests.test_dispatch_run_jobs import MODELS, Runtime, World, cdir, keys, log, pytestmark, py, store, w  # noqa: F401
from tests.test_dispatch_run_jobs_exit import cause_of, end, evidence_of, fresh_runner, run, runner, take


# -- the store ---------------------------------------------------------------------------------------------------------

def test_the_store_records_a_failure_once_and_the_first_name_stands(w):
    store = w.jobs
    assert store.artifact_failure("s-" + "1" * 16, "t") is None
    assert store.note_artifact_failed("s-" + "1" * 16, "t", "adopt-cap:memory") is True
    assert store.note_artifact_failed("s-" + "1" * 16, "t", "adopt-cap:memory") is False       # a repeat
    assert store.note_artifact_failed("s-" + "1" * 16, "t", "another") is False                  # never replaced
    assert store.artifact_failure("s-" + "1" * 16, "t") == "adopt-cap:memory"
    assert store.artifact_failure("s-" + "1" * 16, "t2") is None


def test_a_recorded_failure_is_never_updated_or_deleted(w):
    store = w.jobs
    store.note_artifact_failed("s-" + "1" * 16, "t", "adopt-cap:memory")
    conn = store._connect()
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE artifact_failures SET name = 'x'")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM artifact_failures")
    finally:
        conn.close()


# -- the runner --------------------------------------------------------------------------------------------------------

def test_an_artifact_failed_run_fails_the_job_at_once_with_the_name_never_no_artifact(w):
    now = [1000.0]
    w.launcher.clock = lambda: now[0]
    r = runner(w, clock=lambda: now[0])
    job, _ = run(w, r)
    end(w, 0)
    end(w, 1)
    take(w, job, 0)
    assert r.settle_job(job.id).detail == "artifacts-not-taken"
    ph = w.launcher.run_phase(run_key(job.id, 1))
    w.jobs.note_artifact_failed(ph.session, ph.ticket, "adopt-cap:memory")          # no time has passed: not the grace
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    out = w.jobs.get(job.id)
    ev = evidence_of(out)
    assert (out.state, cause_of(out)) == ("failed", "run_failed")
    assert ev["run"] == 1 and ev["name"] == "artifact_failed:adopt-cap:memory"


def test_an_artifact_failed_run_fails_the_job_while_the_other_run_still_lives(w):
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    r.settle_job(job.id)
    ph = w.launcher.run_phase(run_key(job.id, 0))
    w.jobs.note_artifact_failed(ph.session, ph.ticket, "walker-cap:bytes")
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    assert w.runtime.children[1].aborted == 1                                       # the live run stopped first
    assert evidence_of(w.jobs.get(job.id))["name"] == "artifact_failed:walker-cap:bytes"


def test_a_run_with_a_taken_artifact_is_not_read_as_failed(w):
    """A run with BOTH a taken artifact and a failure note is not read as failed: the take is dispatch's own proof."""
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    end(w, 1)
    t0, t1 = take(w, job, 0), take(w, job, 1)
    w.jobs.note_artifact_failed(t0.session, t0.ticket, "late-note")
    assert r.settle_job(job.id).action == "verifying"


def test_a_nothing_new_run_fails_the_job_at_once_run_failed_no_change(w):
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    end(w, 1)
    take(w, job, 0)
    take(w, job, 1, bundle=False)
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    out = w.jobs.get(job.id)
    ev = evidence_of(out)
    assert (out.state, cause_of(out)) == ("failed", "run_failed")
    assert ev["run"] == 1 and ev["name"] == "no-change"


def test_a_nothing_new_run_fails_the_job_while_the_other_run_still_lives(w):
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    r.settle_job(job.id)
    take(w, job, 0, bundle=False)
    done = r.settle_job(job.id)
    assert (done.action, done.cause) == ("stopped", "run_failed")
    assert w.runtime.children[1].aborted == 1
    assert evidence_of(w.jobs.get(job.id))["name"] == "no-change"


def test_runs_that_all_made_a_change_still_go_to_verifying(w):
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    end(w, 1)
    take(w, job, 0)
    take(w, job, 1)
    assert r.settle_job(job.id).action == "verifying"


def test_a_restart_reads_the_failure_from_the_store_and_converges(w):
    r = runner(w)
    job, _ = run(w, r)
    end(w, 0)
    end(w, 1)
    take(w, job, 0)
    assert r.settle_job(job.id).action == "waiting"                                   # the poll records both runs `ended`
    ph = w.launcher.run_phase(run_key(job.id, 1))
    w.jobs.note_artifact_failed(ph.session, ph.ticket, "adopt-work-cap")
    w.jobs.note_artifact_failed(ph.session, ph.ticket, "adopt-work-cap")            # the replay: idempotent
    fresh, _ln, _rt = fresh_runner(w)
    done = fresh.tick()[0]
    assert (done.action, done.cause) == ("stopped", "run_failed")
    assert evidence_of(w.jobs.get(job.id))["name"] == "artifact_failed:adopt-work-cap"


# -- the grace ---------------------------------------------------------------------------------------------------------

def test_the_grace_covers_a_slow_import_and_is_pinned_to_the_root_side_constants():
    from levain.slot import reaper
    from levain.slot import runtime_helper as H
    assert ARTIFACT_GRACE_S == reaper.V11_S + H.V7_SECONDS + H.RESULT_GRACE_S + 120.0
    assert ARTIFACT_GRACE_S > H.V7_SECONDS                                           # an import of V7's length is not "no artifact"
