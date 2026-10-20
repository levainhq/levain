"""The dispatch job store's format: a store written by Levain 0.11.10 (format /1) or 0.11.12 (format /2) is migrated
in place to /3 when this code opens it, keeping its jobs, and a Levain that knows only an older format refuses a newer
store.

The /1 store is made by this module's own schema with F11's and W2-a's statements left out, which is 0.11.10's schema
exactly (`diff <(git show v0.11.10:levain/dispatch/jobs.py | sed -n '/^_SCHEMA = (/,/^)/p') <(sed -n
'/^_SCHEMA = (/,/^)/p' levain/dispatch/jobs.py)` prints only the authorise_signatures and run_records lines); the /2
store leaves out W2-a's alone, 0.11.12's schema (the same diff against v0.11.12 prints only the run_records lines).
A real 0.11.12 store opened by this code, and refused by 0.11.12 once migrated, is the W2-a run
(project_memory/1020-f11/w2a_run/RUN.txt)."""
from __future__ import annotations

import sqlite3

import pytest

from levain.dispatch import jobs as djobs
from levain.dispatch.jobs import JobStore, JobStoreError
from tests.test_dispatch_jobs import at_state, clock, new_store, repo  # noqa: F401  (module fixtures)

V1 = "levain-dispatch-jobs/1"
V2 = "levain-dispatch-jobs/2"
V2_SCHEMA = tuple(s for s in djobs._SCHEMA if "run_records" not in s)
V1_SCHEMA = tuple(s for s in V2_SCHEMA if "authorise_signatures" not in s)


def old_store(tmp_path, clock, monkeypatch, repo, fmt, schema, state="verified"):
    """A store of format ``fmt`` holding one job at ``state``, written as the Levain of that format wrote it."""
    with monkeypatch.context() as m:
        m.setattr(djobs, "FORMAT", fmt)
        m.setattr(djobs, "_SCHEMA", schema)
        m.setattr(djobs, "_MIGRATES_FROM", frozenset())
        store = new_store(tmp_path, clock)
        job, _cap = at_state(store, repo, state)
    return store.directory, job


def v1_store(tmp_path, clock, monkeypatch, repo, state="verified"):
    """A /1 store holding one job at ``state``, written as 0.11.10 wrote it."""
    return old_store(tmp_path, clock, monkeypatch, repo, V1, V1_SCHEMA, state)


def tables(path):
    conn = sqlite3.connect(path)
    try:
        return ({r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")},
                conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0])
    finally:
        conn.close()


def test_a_0_11_10_store_is_migrated_in_place_and_keeps_its_jobs(tmp_path, clock, monkeypatch, repo):
    d, job = v1_store(tmp_path, clock, monkeypatch, repo)
    path = d / djobs.DB_NAME
    names, fmt = tables(path)
    assert fmt == V1 and "authorise_signatures" not in names and "run_records" not in names
    store = JobStore(d, clock=clock)
    again = store.get(job.id)
    assert again is not None and again.state == "verified" and again.digest == job.digest
    names, fmt = tables(path)
    assert fmt == djobs.FORMAT == "levain-dispatch-jobs/3" and {"authorise_signatures", "run_records"} <= names
    # the migrated job takes /2's edges
    assert store._transition(job.id, "failed", "base_moved").state == "failed"
    # and opening it again changes nothing
    JobStore(d, clock=clock).get(job.id)
    assert tables(path)[1] == djobs.FORMAT


def test_a_0_11_12_store_is_migrated_in_place_and_its_verifying_job_records_its_verifier(tmp_path, clock,
                                                                                         monkeypatch, repo):
    from tests.test_dispatch_jobs import verify

    d, job = old_store(tmp_path, clock, monkeypatch, repo, V2, V2_SCHEMA, state="verifying")
    path = d / djobs.DB_NAME
    names, fmt = tables(path)
    assert fmt == V2 and "authorise_signatures" in names and "run_records" not in names
    store = JobStore(d, clock=clock)
    again = store.get(job.id)
    assert again is not None and again.history == job.history
    names, fmt = tables(path)
    assert fmt == djobs.FORMAT and "run_records" in names
    # the migrated job takes /3's table and cause
    verify(store, repo, job, 0)
    assert store._transition(job.id, "failed", "verifier_refused", evidence="driver-timeout").state == "failed"
    assert len(store.run_records(job.id)) == 1


def test_the_migration_leaves_meta_written_once(tmp_path, clock, monkeypatch, repo):
    d, _job = v1_store(tmp_path, clock, monkeypatch, repo)
    JobStore(d, clock=clock).list()
    conn = sqlite3.connect(d / djobs.DB_NAME)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="written once"):
            conn.execute("UPDATE meta SET value = 'x' WHERE key = 'format'")
        with pytest.raises(sqlite3.DatabaseError, match="written once"):
            conn.execute("DELETE FROM meta WHERE key = 'format'")
    finally:
        conn.close()


@pytest.mark.parametrize("known, migrates", [(V1, frozenset()), (V2, frozenset({V1}))])
def test_a_levain_that_knows_only_an_older_format_refuses_a_v3_store(tmp_path, clock, monkeypatch, repo, known,
                                                                     migrates):
    store = new_store(tmp_path, clock)
    at_state(store, repo, "verified")
    # 0.11.10's and 0.11.12's check, verbatim in effect: a marker that is not its FORMAT and not one it migrates from
    # is refused, naming both
    monkeypatch.setattr(djobs, "FORMAT", known)
    monkeypatch.setattr(djobs, "_MIGRATES_FROM", migrates)
    with pytest.raises(JobStoreError, match=rf"store format 'levain-dispatch-jobs/3' is not '{known}'"):
        JobStore(store.directory, clock=clock).list()


def test_an_unknown_format_is_refused_not_migrated(tmp_path, clock, monkeypatch, repo):
    with monkeypatch.context() as m:
        m.setattr(djobs, "FORMAT", "levain-dispatch-jobs/9")
        m.setattr(djobs, "_MIGRATES_FROM", frozenset())
        store = new_store(tmp_path, clock)
        store.list()
    with pytest.raises(JobStoreError, match=r"store format 'levain-dispatch-jobs/9' is not"):
        JobStore(store.directory, clock=clock).list()
    assert tables(store.directory / djobs.DB_NAME)[1] == "levain-dispatch-jobs/9"
