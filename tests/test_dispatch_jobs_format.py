"""The dispatch job store's format: a store written by Levain 0.11.10 (format /1), 0.11.12 (format /2) or 0.11.13
(format /3) is migrated in place to /4 when this code opens it, keeping its jobs, and a Levain that knows only an older
format refuses a newer store.

The /1 store is made by this module's own schema with F11's and W2-a's statements left out, the /2 store leaves out
W2-a's alone, and the /3 store leaves out only F11-C's envelope tables (V1_SCHEMA, V2_SCHEMA and V3_SCHEMA below). Each
is that release's schema up to the statements the later schema added to tables that release already had (a column such
as run_records.ticket) or to tables it lacked (verifier_refusals, event_cursor): the exact difference is
`diff <(git show v0.11.NN:levain/dispatch/jobs.py | sed -n '/^_SCHEMA = (/,/^)/p') <(sed -n '/^_SCHEMA = (/,/^)/p'
levain/dispatch/jobs.py)` for NN in 10, 12, 13, and is not restated here.
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
V3 = "levain-dispatch-jobs/3"
V4 = "levain-dispatch-jobs/4"
# the envelope binding (F11-C) is /4's: 0.11.10, 0.11.12 and 0.11.13 had none of it
F11C = ("job_envelopes", "job_threads")
# the handoff rows (W-C) are /5's: every Levain through 0.11.14 had none
V4_SCHEMA = tuple(s for s in djobs._SCHEMA if "handoffs" not in s)
V3_SCHEMA = tuple(s for s in V4_SCHEMA if not any(t in s for t in F11C))
V2_SCHEMA = tuple(s for s in V3_SCHEMA if "run_records" not in s)
V1_SCHEMA = tuple(s for s in V2_SCHEMA if "authorise_signatures" not in s)


def old_store(tmp_path, clock, monkeypatch, repo, fmt, schema, state="verified"):
    """A store of format ``fmt`` holding one job at ``state``, written as the Levain of that format wrote it."""
    # this code reads the envelope binding on every job load, so the job is walked with those tables present and
    # they are dropped after, leaving the older Levain's tables exactly
    later = tuple(t for t in (*F11C, "handoffs") if not any(t in s for s in schema))
    f11c = tuple(s for s in djobs._SCHEMA if any(t in s for t in later))
    with monkeypatch.context() as m:
        m.setattr(djobs, "FORMAT", fmt)
        m.setattr(djobs, "_SCHEMA", schema + f11c)
        m.setattr(djobs, "_MIGRATES_FROM", frozenset())
        store = new_store(tmp_path, clock)
        job, _cap = at_state(store, repo, state)
    conn = sqlite3.connect(store.directory / djobs.DB_NAME)
    try:
        for t in later:
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
    finally:
        conn.close()
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
    assert fmt == djobs.FORMAT == "levain-dispatch-jobs/5" and {"authorise_signatures", "run_records", "handoffs"} <= names
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


@pytest.mark.parametrize("fmt0, schema, lacks", [(V3, V3_SCHEMA, set(F11C) | {"handoffs"}), (V4, V4_SCHEMA, {"handoffs"})])
def test_a_0_11_13_store_is_migrated_in_place_and_gains_the_envelope_binding(tmp_path, clock, monkeypatch, repo, fmt0,
                                                                             schema, lacks):
    # (/4, 0.11.14's, gains the handoff rows the same way; its job keeps no handoff row and is read as before)
    d, job = old_store(tmp_path, clock, monkeypatch, repo, fmt0, schema)
    path = d / djobs.DB_NAME
    names, fmt = tables(path)
    assert fmt == fmt0 and not lacks & names
    store = JobStore(d, clock=clock)
    again = store.get(job.id)
    assert again is not None and again.history == job.history and again.envelope_id is None
    names, fmt = tables(path)
    assert fmt == djobs.FORMAT and set(F11C) | {"handoffs"} <= names and store.handoff_row(job.id) is None


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


@pytest.mark.parametrize("known, migrates", [(V1, frozenset()), (V2, frozenset({V1})), (V3, frozenset({V1, V2})),
                                            (V4, frozenset({V1, V2, V3}))])
def test_a_levain_that_knows_only_an_older_format_refuses_a_current_store(tmp_path, clock, monkeypatch, repo, known,
                                                                     migrates):
    store = new_store(tmp_path, clock)
    at_state(store, repo, "verified")
    # 0.11.10's, 0.11.12's, 0.11.13's and 0.11.14's check, verbatim in effect: a marker that is not its FORMAT and not one it migrates from
    # is refused, naming both
    monkeypatch.setattr(djobs, "FORMAT", known)
    monkeypatch.setattr(djobs, "_MIGRATES_FROM", migrates)
    with pytest.raises(JobStoreError, match=rf"store format 'levain-dispatch-jobs/5' is not '{known}'"):
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
