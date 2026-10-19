"""The dispatch job store: a real SQLite file, real git repos, real keys in a real authority log. The whole edge table
is walked (every allowed edge taken, every other (state, cause) pair refused), leases lapse through the injected
clock, and the append-only triggers are hit with raw SQL."""
from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import sign as dsign
from levain.dispatch.jobs import (
    CAUSES, EDGES, STATES, TERMINAL, UNSIGNED_LEASE, CapabilityRefused, JobStore, JobStoreError, SignatureRefused,
    TransitionRefused,
)
from tests.test_dispatch_contract import NOW, POLICY, WRITE_SET, make_repo, registry, resolve, submission, template
from tests.test_dispatch_sign import Clock, keys, log, snapshot  # noqa: F401  (module fixtures)

NS = dsign.DISPATCH_SIGN_NAMESPACE

# the shortest walk from filed to each state, through the private transition
PATH = {
    "filed": [],
    "signed": [("signed", "signature")],
    "queued": [("signed", "signature"), ("queued", "slot_wait")],
    "running": [("signed", "signature"), ("running", "slot_taken")],
    "verifying": [("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited")],
    "verified": [("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                 ("verified", "verifier_pass")],
    "recommended": [("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                    ("verified", "verifier_pass"), ("recommended", "planner_recommends")],
    "authorised": [("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                   ("verified", "verifier_pass"), ("authorised", "human_authorises")],
    "adopted": [("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                ("verified", "verifier_pass"), ("authorised", "human_authorises"), ("adopted", "adopted")],
    "failed": [("failed", "budget_cap")],
    "cancelled": [("cancelled", "human_cancel")],
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-jobs-repo"))


@pytest.fixture
def clock():
    return Clock(NOW)


def new_store(tmp_path, clock, name="dispatch") -> JobStore:
    d = tmp_path / name
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return JobStore(d, clock=clock)


@pytest.fixture
def store(tmp_path, clock):
    return new_store(tmp_path, clock)


def file_job(store, repo, *, session="sess-1", idem_key=None, resolver=None, policy=POLICY, **over):
    path, base = repo
    return store.file(submission(path, base, **over), resolve_template=resolver or registry(), policy=policy,
                      filer_session=session, idem_key=idem_key)


def at_state(store, repo, state):
    job, cap = file_job(store, repo)
    for to, cause in PATH[state]:
        job = store._transition(job.id, to, cause)
    assert job.state == state
    return job, cap


# --- filing --------------------------------------------------------------------------------------------------------

def test_file_stores_the_canonical_contract_and_returns_a_capability_once(store, repo):
    job, cap = file_job(store, repo)
    assert job.id.startswith("dj-") and len(job.id) == 35 and job.state == "filed"
    assert job.contract == resolve(submission(*repo)) and job.digest == job.contract.digest
    assert job.filed_at == utc_stamp(NOW) and job.unsigned_until == utc_stamp(NOW + UNSIGNED_LEASE)
    assert isinstance(cap, str) and len(cap) >= 40
    with sqlite3.connect(store.path) as conn:
        stored = conn.execute("SELECT capability_sha256 FROM jobs").fetchone()[0]
    assert cap not in stored and len(stored) == 64            # only the hash is kept
    assert job.history[0].cause == "filed"
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_idempotent_filing_returns_the_same_job_and_no_capability(store, repo):
    job, cap = file_job(store, repo, idem_key="k1")
    again, cap2 = file_job(store, repo, idem_key="k1")
    assert again.id == job.id and cap2 is None and cap is not None
    with pytest.raises(ContractRefused, match="already filed job .* with another contract digest"):
        file_job(store, repo, idem_key="k1", goal="another goal")
    assert len(store.list()) == 1


def test_file_refuses_bad_labels_and_a_non_submission(store, repo):
    with pytest.raises(ContractRefused, match="a contract is exactly"):
        store.file(resolve(submission(*repo)), resolve_template=registry(), policy=POLICY, filer_session="s")
    with pytest.raises(TypeError):
        store.file(submission(*repo), filer_session="s")              # type: ignore[call-arg]
    with pytest.raises(DispatchError, match="filer_session"):
        file_job(store, repo, session="")
    with pytest.raises(DispatchError, match="idem_key"):
        file_job(store, repo, idem_key="a\nb")


def test_an_expired_contract_is_not_filed(store, repo, clock):
    clock.now = NOW + timedelta(minutes=5)
    with pytest.raises(ContractRefused, match="not in the future"):
        file_job(store, repo, expires_at=utc_stamp(NOW + timedelta(minutes=5)))


# --- one live contract per write file ------------------------------------------------------------------------------

def test_overlap_is_refused_and_released_when_the_other_is_terminal(store, repo):
    one, cap = file_job(store, repo)                                          # writes src/a.py, src/new.py
    with pytest.raises(ContractRefused, match=f"live job {one.id} already writes src/a.py"):
        file_job(store, repo, write=["src/a.py", "src/b.py"])
    two, _ = file_job(store, repo, write=["src/b.py"])                       # disjoint: fine
    store.cancel_by_planner(one.id, cap)
    three, _ = file_job(store, repo, write=["src/a.py"])
    assert {two.state, three.state} == {"filed"}


def test_overlap_is_released_when_the_other_lapses(store, repo, clock):
    file_job(store, repo)
    clock.now = NOW + UNSIGNED_LEASE
    again, _ = file_job(store, repo, expires_at=utc_stamp(clock.now + timedelta(hours=1)))
    assert again.state == "filed"


def test_an_authorised_job_still_holds_its_files(store, repo, clock):
    at_state(store, repo, "authorised")
    clock.now = NOW + timedelta(hours=3)                                      # past its expiry: authorised never lapses
    with pytest.raises(ContractRefused, match="already writes"):
        file_job(store, repo, expires_at=utc_stamp(clock.now + timedelta(hours=1)))


def test_overlap_is_per_repo(store, repo, tmp_path):
    file_job(store, repo)
    other = make_repo(tmp_path / "other")
    job, _ = file_job(store, other)
    assert job.state == "filed"


# --- leases --------------------------------------------------------------------------------------------------------

def test_an_unsigned_job_lapses_after_the_unsigned_lease(store, repo, clock):
    job, _ = file_job(store, repo)
    clock.now = NOW + UNSIGNED_LEASE - timedelta(microseconds=1)
    assert store.get(job.id).state == "filed"
    clock.now = NOW + UNSIGNED_LEASE
    lapsed = store.get(job.id)
    assert lapsed.state == "failed" and lapsed.history[-1].cause == "lease_lapsed"
    assert store.sweep() == []


def test_the_unsigned_lease_never_outlives_the_expiry(store, repo, clock):
    job, _ = file_job(store, repo, expires_at=utc_stamp(NOW + timedelta(minutes=10)))
    assert job.unsigned_until == utc_stamp(NOW + timedelta(minutes=10))
    clock.now = NOW + timedelta(minutes=10)
    assert store.sweep() == [job.id]


@pytest.mark.parametrize("state", ["signed", "queued", "running", "verifying", "verified", "recommended"])
def test_a_signed_job_lapses_at_its_expiry_until_authorised(store, repo, clock, state):
    job, _ = at_state(store, repo, state)
    clock.now = NOW + UNSIGNED_LEASE + timedelta(minutes=1)                  # past the unsigned lease: no matter
    assert store.get(job.id).state == state
    clock.now = NOW + timedelta(hours=2)
    assert store.get(job.id).state == "failed"


@pytest.mark.parametrize("state", ["authorised", "adopted", "cancelled"])
def test_authorised_and_terminal_jobs_never_lapse(store, repo, clock, state):
    job, _ = at_state(store, repo, state)
    clock.now = NOW + timedelta(days=3)
    assert store.get(job.id).state == state


def test_every_operation_sweeps_first(store, repo, clock):
    job, cap = file_job(store, repo)
    clock.now = NOW + UNSIGNED_LEASE
    with pytest.raises(TransitionRefused, match="from failed to cancelled"):
        store.cancel_by_planner(job.id, cap)                                   # swept before the cancel
    assert [j.state for j in store.list()] == ["failed"]


# --- the edge table ------------------------------------------------------------------------------------------------

EDGE_CASES = sorted((f, t, c) for (f, t), causes in EDGES.items() for c in causes)


@pytest.mark.parametrize("from_state, to_state, cause", EDGE_CASES)
def test_every_allowed_edge_is_taken(tmp_path, clock, repo, from_state, to_state, cause):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, from_state)
    moved = store._transition(job.id, to_state, cause, evidence="e")
    assert moved.state == to_state and moved.history[-1].cause == cause and moved.history[-1].evidence == "e"
    assert [t.seq for t in moved.history] == list(range(1, len(moved.history) + 1))


@pytest.mark.parametrize("from_state", STATES)
def test_every_other_state_and_cause_is_refused(tmp_path, clock, repo, from_state):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, from_state)
    for to_state in STATES:
        for cause in sorted(CAUSES | {"filed", "made_up"}):
            if cause in EDGES.get((from_state, to_state), ()):
                continue
            with pytest.raises(TransitionRefused):
                store._transition(job.id, to_state, cause)
    assert store.get(job.id).history == job.history                           # nothing was written


def test_the_table_has_the_specified_shape():
    assert not any(f in TERMINAL for f, _ in EDGES)
    assert not any(t == "filed" for _, t in EDGES)
    for f in ("filed", "signed", "queued", "running", "verifying", "verified", "recommended"):
        assert EDGES[(f, "failed")] >= {"lease_lapsed", "budget_cap", "floor_refused"}
        assert EDGES[(f, "cancelled")] == {"planner_cancel", "human_cancel"}
    assert EDGES[("authorised", "cancelled")] == {"human_cancel"}
    assert ("authorised", "failed") not in EDGES
    assert EDGES[("verified", "authorised")] == EDGES[("recommended", "authorised")] == {
        "human_authorises", "human_adoption_rule"}


def test_an_unknown_job_is_refused(store):
    with pytest.raises(TransitionRefused, match="no dispatch job"):
        store._transition("dj-" + "0" * 32, "signed", "signature")
    with pytest.raises(TransitionRefused, match="no dispatch job"):
        store.recommend("not-an-id", "x")
    assert store.get("dj-" + "0" * 32) is None and store.get(None) is None


# --- the planner channel -------------------------------------------------------------------------------------------

def test_recommend_takes_the_capability_and_only_from_verified(store, repo):
    job, cap = at_state(store, repo, "verified")
    with pytest.raises(CapabilityRefused):
        store.recommend(job.id, cap + "x")
    with pytest.raises(CapabilityRefused):
        store.recommend(job.id, None)
    assert store.recommend(job.id, cap).state == "recommended"
    with pytest.raises(TransitionRefused):
        store.recommend(job.id, cap)


def test_recommend_from_filed_is_refused(store, repo):
    job, cap = file_job(store, repo)
    with pytest.raises(TransitionRefused, match="from filed to recommended"):
        store.recommend(job.id, cap)


def test_another_jobs_capability_is_refused(store, repo):
    one, cap1 = file_job(store, repo)
    two, _cap2 = file_job(store, repo, write=["src/b.py"])
    with pytest.raises(CapabilityRefused):
        store.cancel_by_planner(two.id, cap1)


def test_the_planner_cancels_before_authorised_never_after(store, repo, tmp_path, clock):
    job, cap = at_state(store, repo, "recommended")
    assert store.cancel_by_planner(job.id, cap).history[-1].cause == "planner_cancel"
    other = new_store(tmp_path, clock, "d2")
    job, cap = at_state(other, repo, "authorised")
    with pytest.raises(TransitionRefused, match="'planner_cancel' does not take a job from authorised"):
        other.cancel_by_planner(job.id, cap)


# --- signing through the store -------------------------------------------------------------------------------------

def signed(job, key, at=None):
    payload = dsign.sign_payload(job, signer_fp=key.fp, signed_at=at or NOW + timedelta(minutes=1))
    return payload, key.sign(dsign.sign_message(payload), NS)


def test_sign_moves_filed_to_signed_and_stores_the_signature(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    out = store.sign(job.id, payload, sig, log.source)
    assert out.state == "signed" and out.signer_fp == keys["phone"].fp and out.history[-1].cause == "signature"
    assert out.lease_end == job.expires_at
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT payload, signature FROM signatures").fetchone()
    assert row == (dsign.sign_message(payload).decode(), sig)
    with pytest.raises(SignatureRefused, match="not filed; a contract is signed once"):
        store.sign(job.id, payload, sig, log.source)


def test_a_refused_signature_leaves_the_job_filed(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    for name, match in (("presence", "does not cover T2"), ("revoked", "revoked"), ("stranger", "not an enrolled")):
        payload, sig = signed(job, keys[name])
        with pytest.raises(SignatureRefused, match=match):
            store.sign(job.id, payload, sig, log.source)
    payload, _ = signed(job, keys["phone"])
    other = keys["phone"].sign(dsign.sign_message(dict(payload, digest="sha256:" + "0" * 64)), NS)
    with pytest.raises(SignatureRefused, match="does not verify"):
        store.sign(job.id, payload, other, log.source)
    assert store.get(job.id).state == "filed" and store.get(job.id).signer_fp is None


def test_a_future_dated_signature_is_refused(store, repo, keys, log):
    job, _ = file_job(store, repo)
    payload, sig = signed(job, keys["phone"], at=NOW + timedelta(minutes=10))
    with pytest.raises(SignatureRefused, match="dated in the future"):
        store.sign(job.id, payload, sig, log.source)


def test_a_lapsed_job_cannot_be_signed(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    payload, sig = signed(job, keys["phone"])
    clock.now = NOW + UNSIGNED_LEASE
    with pytest.raises(SignatureRefused, match="is failed, not filed"):
        store.sign(job.id, payload, sig, log.source)


def test_sign_takes_the_authority_source_never_a_snapshot(store, repo, keys, snapshot):
    job, _ = file_job(store, repo)
    payload, sig = signed(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="AuthoritySource"):
        store.sign(job.id, payload, sig, snapshot)


# --- the store itself ----------------------------------------------------------------------------------------------

def test_the_tables_are_append_only(store, repo):
    job, _ = at_state(store, repo, "signed")
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql in ("UPDATE jobs SET digest = 'x'", "DELETE FROM jobs",
                    "UPDATE transitions SET to_state = 'authorised'", "DELETE FROM transitions",
                    "INSERT OR REPLACE INTO jobs SELECT * FROM jobs",
                    "INSERT OR REPLACE INTO transitions (job_id, seq, to_state, cause, at, evidence) "
                    f"VALUES ('{job.id}', 2, 'authorised', 'human_authorises', 'x', '')",
                    f"INSERT INTO transitions (job_id, seq, to_state, cause, at, evidence) "
                    f"VALUES ('{job.id}', 9, 'adopted', 'adopted', 'x', '')"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only|written once|never replaced|next seq"):
                conn.execute(sql)
        conn.execute("INSERT INTO signatures VALUES (?, 'p', 's', 'f', 'a')", (job.id,))
        for sql in ("UPDATE signatures SET signature = 'y'", "DELETE FROM signatures",
                    f"INSERT OR REPLACE INTO signatures VALUES ('{job.id}', 'p2', 's2', 'f', 'a')"):
            with pytest.raises(sqlite3.IntegrityError, match="written once|never replaced"):
                conn.execute(sql)
    finally:
        conn.close()
    assert store.get(job.id).state == "signed"


def test_a_directory_others_can_reach_is_refused(tmp_path, clock):
    d = tmp_path / "open"
    d.mkdir()
    os.chmod(d, 0o755)
    with pytest.raises(JobStoreError, match="0700"):
        JobStore(d, clock=clock).list()
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    os.symlink(real, tmp_path / "link")
    with pytest.raises(JobStoreError, match="not a directory"):
        JobStore(tmp_path / "link", clock=clock).list()


def test_another_database_is_refused(tmp_path, clock):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    with sqlite3.connect(d / "dispatch.db") as conn:
        conn.execute("CREATE TABLE other (x)")
    with pytest.raises(JobStoreError, match="no dispatch-store format marker"):
        JobStore(d, clock=clock).list()


def test_the_default_directory_is_under_levain_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home"))
    assert JobStore().directory == (tmp_path / "home" / "dispatch").resolve()


def test_a_naive_clock_is_refused(tmp_path, repo):
    store = new_store(tmp_path, lambda: NOW.replace(tzinfo=None))
    with pytest.raises(Exception, match="naive"):
        store.list()


# --- fix round 1 (L1/L2 slice a) -----------------------------------------------------------------------------------

_SEED = ("INSERT INTO jobs (id, digest, contract, filed_at, filer_session, capability_sha256, idem_key, repo, "
         "expires_at, unsigned_until) VALUES (?, ?, 'c', 't', 's', 'cap', ?, '/r', 'e', 'u')")


def test_insert_or_replace_through_the_session_idem_key_is_refused(store):
    """L1 MED1, the reviewer's probe1: a new id with an existing (filer_session, idem_key) silently deleted the old
    row through the UNIQUE key, without firing the delete trigger."""
    store.list()                                                              # the schema exists
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute(_SEED, ("dj-a", "d", "k"))
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            conn.execute(_SEED.replace("INSERT", "INSERT OR REPLACE"), ("dj-b", "EVIL", "k"))
        assert conn.execute("SELECT id, digest FROM jobs").fetchall() == [("dj-a", "d")]
        conn.execute(_SEED, ("dj-c", "d", None))                              # a null idem_key never collides
        conn.execute(_SEED, ("dj-d", "d", None))
    finally:
        conn.close()


def test_the_meta_table_is_append_only(store):
    store.list()
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql in ("UPDATE meta SET value = 'x'", "DELETE FROM meta",
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('store_id', 'other')",
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('format', 'other')"):
            with pytest.raises(sqlite3.IntegrityError, match="store metadata is written once"):
                conn.execute(sql)
    finally:
        conn.close()


def test_the_stores_own_connections_enforce_foreign_keys(store):
    store.list()
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute("INSERT INTO transitions VALUES ('dj-zzz', 1, 'filed', 'filed', 't', '')")
    finally:
        conn.close()


def test_file_takes_a_submission_and_resolves_it_itself(store, repo):
    """L2 M4 / L1 MED3, the reviewers' probe2: a hand-built ResolvedContract with a 10-year expiry was filed. There
    is no public way to file a ResolvedContract object now."""
    import dataclasses

    from levain.cockpit.consent import utc_stamp as stamp
    good = resolve(submission(*repo))
    far = dataclasses.replace(good, expires_at=stamp(NOW + timedelta(days=3650)))
    with pytest.raises(ContractRefused, match="a contract is exactly"):
        store.file(far, resolve_template=registry(), policy=POLICY, filer_session="s")
    with pytest.raises(ContractRefused, match="never open-ended"):
        file_job(store, repo, expires_at=stamp(NOW + timedelta(days=3650)))
    with pytest.raises(ContractRefused, match="data boundary"):
        file_job(store, repo, read=["work/client.txt"])
    assert store.list() == []


def test_overlap_compares_paths_without_case(store, repo):
    """L1 L6: two NEW files differing only in case are one file on a case-insensitive disk."""
    wide = registry(template(write_paths=WRITE_SET | {"src/New.py"}))
    one, _ = file_job(store, repo, write=["src/new.py"])
    with pytest.raises(ContractRefused, match=f"live job {one.id} already writes src/new.py"):
        file_job(store, repo, write=["src/New.py"], resolver=wide)


def test_an_idempotent_replay_after_expiry_returns_the_same_job(store, repo, clock):
    """L1 L2: the replay was refused 'expired' instead of returning the job it filed."""
    job, cap = file_job(store, repo, idem_key="k1", expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    clock.now = NOW + timedelta(minutes=6)
    again, cap2 = file_job(store, repo, idem_key="k1", expires_at=utc_stamp(NOW + timedelta(minutes=5)))
    assert again.id == job.id and cap2 is None and cap is not None and again.state == "failed"
    with pytest.raises(ContractRefused, match="another contract digest"):
        file_job(store, repo, idem_key="k1", expires_at=utc_stamp(NOW + timedelta(minutes=5)), goal="other")


def test_a_sweep_is_committed_even_when_the_operation_raises(store, repo, clock):
    """L1 L3: the sweep ran inside the operation's transaction, so a raised operation rolled the lapse back."""
    job, cap = file_job(store, repo)
    clock.now = NOW + UNSIGNED_LEASE
    with pytest.raises(CapabilityRefused):
        store.cancel_by_planner(job.id, cap + "x")
    with sqlite3.connect(store.path) as conn:                                  # read raw: no store call sweeps it
        rows = conn.execute("SELECT to_state, cause, at FROM transitions WHERE job_id = ? ORDER BY seq",
                            (job.id,)).fetchall()
    assert rows[-1] == ("failed", "lease_lapsed", utc_stamp(NOW + UNSIGNED_LEASE))


def test_sweep_takes_no_caller_time(store):
    with pytest.raises(TypeError):
        store.sweep(NOW + timedelta(days=365))                               # type: ignore[call-arg]


def test_each_store_has_its_own_store_id_and_a_signature_binds_it(tmp_path, clock, repo, keys, snapshot):
    """L2 L2: a signature names the store it was made for; one from another store is refused."""
    import dataclasses
    a, b = new_store(tmp_path, clock, "a"), new_store(tmp_path, clock, "b")
    job_a, _ = file_job(a, repo)
    job_b, _ = file_job(b, repo)
    assert job_a.store_id != job_b.store_id and len(job_a.store_id) == 32
    assert a.get(job_a.id).store_id == job_a.store_id                       # stable across connections
    assert JobStore(a.directory, clock=clock).list()[0].store_id == job_a.store_id
    payload, sig = signed(job_a, keys["phone"])
    assert payload["store_id"] == job_a.store_id
    assert dsign.refusal(payload, sig, snapshot, job_a) is None
    # the same job id, digest and expiry in another store (a restored or copied store's twin): refused by store
    twin = dataclasses.replace(job_a, store_id=job_b.store_id)
    assert dsign.refusal(payload, sig, snapshot, twin) == "the signature is for another dispatch store"


# --- fix round 2 (L3 r1) -----------------------------------------------------------------------------------------

class Sequence:
    """A clock that answers each call from a list, then repeats the last: the time before a lock wait, then after."""
    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0) if len(self.times) > 1 else self.times[0]


def test_a_lock_wait_never_dates_a_signature_inside_a_lapsed_lease(store, repo, keys, log):
    # codex: the stamp was sampled before the lock and reused; a signing call that waited past the unsigned lease
    # was still judged at the earlier time and accepted
    job, _ = file_job(store, repo)
    payload, sig = signed(job, keys["phone"], at=NOW + UNSIGNED_LEASE - timedelta(seconds=2))
    store._clock = Sequence(NOW + UNSIGNED_LEASE - timedelta(seconds=1), NOW + UNSIGNED_LEASE + timedelta(seconds=10))
    with pytest.raises(SignatureRefused, match="is failed, not filed"):
        store.sign(job.id, payload, sig, log.source)
    got = store.get(job.id)
    assert got.state == "failed" and got.history[-1].at == utc_stamp(NOW + UNSIGNED_LEASE + timedelta(seconds=10))


def test_filing_dates_the_job_and_its_lease_from_the_clock_read_under_the_lock(store, repo):
    store._clock = Sequence(NOW, NOW + timedelta(minutes=5))
    job, _ = file_job(store, repo)
    assert job.filed_at == utc_stamp(NOW + timedelta(minutes=5))
    assert job.unsigned_until == utc_stamp(NOW + timedelta(minutes=5) + UNSIGNED_LEASE)


def test_a_key_revoked_while_signing_waits_for_the_lock_is_refused(store, repo, keys, clock, tmp_path):
    # complement: the authority was replayed before the write lock; a revocation in that window was missed
    from contextlib import contextmanager

    from tests.test_consent_queue import Log
    (tmp_path / "auth").mkdir()
    lg = Log(tmp_path / "auth", keys)
    lg.enrol("laptop", "laptop-presence")
    lg.enrol("phone", "display-phone")
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    original, entered = store._write, []

    @contextmanager
    def waiting():
        entered.append(1)
        if len(entered) == 2:                 # the operation's own lock: the operator revokes while we wait
            lg.revoke("phone")
        with original() as conn:
            yield conn

    store._write = waiting
    with pytest.raises(SignatureRefused, match="revoked"):
        store.sign(job.id, payload, sig, lg.source)
    store._write = original
    assert store.get(job.id).state == "filed"


def test_an_authority_log_that_cannot_be_read_is_a_signature_refusal(store, repo, keys, clock, tmp_path):
    # codex: a sqlite3.Error from the authority source escaped sign() as a crash
    from levain.consent.queue import authority_source
    bad = tmp_path / "cockpit.db"
    bad.write_bytes(b"this is not a database" * 100)
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="the authority log could not be read"):
        store.sign(job.id, payload, sig, authority_source(bad, anchor=None))
    with pytest.raises(SignatureRefused, match="the authority log could not be read"):
        store.sign(job.id, payload, sig, authority_source(tmp_path / "missing" / "cockpit.db", anchor=None))
    assert store.get(job.id).state == "filed"


def test_a_live_job_whose_stored_contract_no_longer_parses_is_a_damaged_store(store, repo):
    # glm: a damaged live row surfaced as ContractRefused, blaming the new submission
    path, _base = repo
    store.list()                                            # the store exists
    damaged = "dj-" + "f" * 32
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute("INSERT INTO jobs (id, digest, contract, filed_at, filer_session, capability_sha256, idem_key, "
                     "repo, expires_at, unsigned_until) VALUES (?, ?, 'not a contract', ?, 's', ?, NULL, ?, ?, ?)",
                     (damaged, "sha256:" + "0" * 64, utc_stamp(NOW), "0" * 64, str(path),
                      utc_stamp(NOW + timedelta(hours=1)), utc_stamp(NOW + timedelta(minutes=20))))
        conn.execute("INSERT INTO transitions (job_id, seq, to_state, cause, at, evidence) "
                     "VALUES (?, 1, 'filed', 'filed', ?, '')", (damaged, utc_stamp(NOW)))
    finally:
        conn.close()
    with pytest.raises(JobStoreError, match=f"live job {damaged} holds a stored contract that no longer parses"):
        file_job(store, repo)


@pytest.mark.parametrize("capability", ["\ud800", "ok\udfffok", 7, None])
def test_a_capability_that_is_not_encodable_text_is_a_mismatch_never_a_crash(store, repo, capability):
    # complement: a lone surrogate raised UnicodeEncodeError out of the planner channel
    job, _ = file_job(store, repo)
    with pytest.raises(CapabilityRefused):
        store.cancel_by_planner(job.id, capability)
    assert store.get(job.id).state == "filed"
