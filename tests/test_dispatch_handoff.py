"""The filing's handoff (W-C): a job and its read-set bundle are published together or not at all.

Real repo, real git, the real exporter (:func:`levain.dispatch.cli.readset_export`), a real store. The crash is a
real SIGKILL of a filing process; the race is two filings of one idempotency key."""
from __future__ import annotations

import errno
import os
import signal
import sqlite3
import subprocess
import sys

import pytest

from levain.dispatch import cli as dcli
from levain.dispatch import handoff as dh
from levain.dispatch import jobs as djobs
from levain.dispatch.jobs import JobStore
from tests.test_dispatch_contract import NOW, POLICY, make_repo, registry, submission
from tests.test_dispatch_envelope import Clock, FakeEnvelope

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CHILD = """
import os, signal, sys
sys.path.insert(0, {root!r})
from levain.dispatch import cli as dcli
from levain.dispatch import jobs as djobs
from tests.test_dispatch_contract import NOW, POLICY, registry, submission
from tests.test_dispatch_envelope import Clock, FakeEnvelope

def seam(point):
    if point == "installed":
        os.kill(os.getpid(), signal.SIGKILL)
djobs._FAULT_SEAM = seam
store = djobs.JobStore({store!r}, clock=Clock(NOW))
store.file_in_envelope(submission({repo!r}, {base!r}), resolve_template=registry(), policy=POLICY,
                       filer_session="coord-1", envelope=FakeEnvelope("env-1", {repo!r}), export=dcli.readset_export)
"""


def _store(tmp_path, name="dispatch"):
    d = tmp_path / name
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return JobStore(d, clock=Clock(NOW))


def _live(store):
    return sorted(e for e in os.listdir(store.handoffs.path) if e != ".tmp")


def _count(store, table):
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_an_envelope_filing_killed_between_install_and_commit_leaves_no_signed_job_and_its_bundle_is_collected(
        tmp_path):
    # reproduced on b30d25aa: file_in_envelope took the job filed -> signed with no handoff anywhere (a signed job
    # the run could only refuse); here the signing rides in the transaction that names the bundle, so a kill after
    # the install and before that commit leaves a bundle and no job
    repo, base = make_repo(tmp_path / "src")
    store = _store(tmp_path)
    store.sweep()           # the store exists before the filer process opens it
    child = _CHILD.format(root=ROOT, store=str(store.directory), repo=str(repo), base=base)
    proc = subprocess.run([sys.executable, "-P", "-c", child], capture_output=True, text=True, timeout=300,
                          cwd=ROOT)
    assert proc.returncode == -signal.SIGKILL, proc.stderr[-2000:]
    assert _count(store, "jobs") == 0 and _count(store, "handoffs") == 0
    orphan = _live(store)
    assert len(orphan) == 1 and orphan[0].startswith("hb-")
    # the next start collects it, and a filing then publishes job and bundle together, signed in that transaction
    report = JobStore(store.directory, clock=Clock(NOW)).collect_handoffs()
    assert report.bundles == orphan and _live(store) == []
    job, _cap = store.file_in_envelope(submission(repo, base), resolve_template=registry(), policy=POLICY,
                                       filer_session="coord-1", envelope=FakeEnvelope("env-1", str(repo)),
                                       export=dcli.readset_export)
    assert job.state == "signed"
    bundle_id, path, size = store.handoff_row(job.id)
    assert _live(store) == [bundle_id] and os.path.getsize(path) == size


def test_an_idempotent_replay_whose_existing_job_wins_unlinks_only_its_own_bundle(tmp_path, monkeypatch):
    # reproduced as this test's own failing variant first: a loser that cleans up by the WINNER's name (a bundle
    # named by D or by the job, complement r4 3a) unlinked the bundle the winner's row names
    repo, base = make_repo(tmp_path / "src")
    store = _store(tmp_path)
    sub = submission(repo, base)
    won = {}

    def seam(point):
        # the loser has exported (it saw no prior row before resolving) and has not yet taken the lock: the winner
        # files the same key, whole, now
        if point == "exported" and not won:
            djobs._FAULT_SEAM = None
            won["job"], won["cap"] = JobStore(store.directory, clock=Clock(NOW)).file(
                sub, resolve_template=registry(), policy=POLICY, filer_session="sess-1", idem_key="k1",
                export=dcli.readset_export)

    monkeypatch.setattr(djobs, "_FAULT_SEAM", seam)
    job, cap = store.file(sub, resolve_template=registry(), policy=POLICY, filer_session="sess-1", idem_key="k1",
                          export=dcli.readset_export)
    assert won and job.id == won["job"].id and cap is None and won["cap"] is not None
    assert _count(store, "jobs") == 1 and _count(store, "handoffs") == 1
    bundle_id, path, size = store.handoff_row(job.id)
    # the winner's bundle is intact and is the only one: the loser's own was unlinked, no orphan, no temp
    assert _live(store) == [bundle_id] and os.path.getsize(path) == size
    assert os.listdir(os.path.join(store.handoffs.path, ".tmp")) == []
    assert store.collect_handoffs().bundles == []


def test_a_writer_that_opened_the_store_before_another_process_migrated_it_files_nothing(tmp_path):
    # reproduced (w2dc-fix1 repro.py R1): a 0.11.14-format JobStore opened before a current Levain migrated the store
    # kept _ready and filed a job with no handoff row, capability returned. The database now refuses the insert of a
    # writer whose format is not the marker's, read inside the inserting transaction
    repo, base = make_repo(tmp_path / "src")
    store = _store(tmp_path)
    store.sweep()                                   # this object's schema check is done; it never re-reads the marker
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        # the older writer's insert, as /4 writes it: no writer format named
        with pytest.raises(sqlite3.IntegrityError, match="^store-format: "):
            conn.execute("INSERT INTO jobs (id, digest, contract, filed_at, filer_session, capability_sha256, "
                         "idem_key, repo, expires_at, unsigned_until) VALUES ('dj-x', 'd', 'c', 't', 's', 'h', "
                         "NULL, '/r', 'e', 'u')")
        # another process moves the store to a newer format, as a migration does (the marker alone)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TRIGGER meta_frozen")
        conn.execute("UPDATE meta SET value = 'levain-dispatch-jobs/6' WHERE key = 'format'")
        conn.execute(djobs._append_only("meta", "store metadata is written once")[0])
        conn.execute("COMMIT")
    finally:
        conn.close()
    with pytest.raises(djobs.JobStoreError, match="^store-format: "):
        store.file(submission(repo, base), resolve_template=registry(), policy=POLICY, filer_session="sess-1",
                   export=dcli.readset_export)
    assert _count(store, "jobs") == 0 and _count(store, "handoffs") == 0
    # nothing of the refused filing is left: no bundle, no temp
    assert _live(store) == [] and os.listdir(os.path.join(store.handoffs.path, ".tmp")) == []


def test_a_failed_live_dir_flush_after_the_link_leaves_no_bundle_behind(tmp_path, monkeypatch):
    # reproduced (w2dc-fix1 repro.py R3a): the link succeeded, the live dir's flush raised EIO, and the installed
    # bundle stayed under an id no caller held, uncollected for the life of the store object
    repo, base = make_repo(tmp_path / "src")
    store = _store(tmp_path)
    store.collect_handoffs()
    real = dh.full_fsync
    live_dir = os.stat(store.handoffs.ensure())

    def eio_on_the_live_dir(fd):
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == (live_dir.st_dev, live_dir.st_ino):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return real(fd)

    monkeypatch.setattr(dh, "full_fsync", eio_on_the_live_dir)
    with pytest.raises(dh.HandoffRefused, match="handoff-install-failed: flush: EIO"):
        store.file(submission(repo, base), resolve_template=registry(), policy=POLICY, filer_session="sess-1",
                   export=dcli.readset_export)
    monkeypatch.setattr(dh, "full_fsync", real)
    assert _count(store, "jobs") == 0 and _count(store, "handoffs") == 0
    assert _live(store) == [] and os.listdir(os.path.join(store.handoffs.path, ".tmp")) == []
