"""The dispatch job store: a real SQLite file, real git repos, real keys in a real authority log. The whole edge table
is walked (every allowed edge taken, every other (state, cause) pair refused), leases lapse through the injected
clock, and the append-only triggers are hit with raw SQL."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch import sign as dsign
from levain.dispatch.jobs import (
    CAUSES, EDGES, FORMAT, STATES, TERMINAL, UNSIGNED_LEASE, CapabilityRefused, JobStore, JobStoreError,
    SignatureRefused, TransitionRefused,
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


@pytest.mark.parametrize("state", ["signed", "queued", "verifying", "verified", "recommended"])
def test_a_signed_job_lapses_at_its_expiry_until_authorised(store, repo, clock, state):
    job, _ = at_state(store, repo, state)
    clock.now = NOW + UNSIGNED_LEASE + timedelta(minutes=1)                  # past the unsigned lease: no matter
    assert store.get(job.id).state == state
    clock.now = NOW + timedelta(hours=2)
    assert store.get(job.id).state == "failed"


def test_a_running_job_is_never_swept_and_is_named_overdue_for_the_supervisor(store, repo, clock):
    job, _ = at_state(store, repo, "running")
    assert store.overdue_running() == []
    clock.now = NOW + timedelta(hours=2)
    assert store.sweep() == [] and store.get(job.id).state == "running"
    assert store.overdue_running() == [job.id]
    assert store.stop_recorded(job.id, "lease_lapsed").state == "failed"
    assert store.overdue_running() == []


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

# the envelope causes are taken only by their own methods, with their binding and precondition (F11-C:
# tests/test_dispatch_envelope.py walks each); the private walk refuses them
ENVELOPE_CAUSES = {"envelope", "envelope_rule", "envelope_ended"}
EDGE_CASES = sorted((f, t, c) for (f, t), causes in EDGES.items() for c in causes if c not in ENVELOPE_CAUSES)
# out of running, every edge but runs_exited is stop_recorded's alone (the supervisor stopped the process first)
STOP_CASES = [e for e in EDGE_CASES if e[0] == "running" and e[1] != "verifying"]
WALK_CASES = [e for e in EDGE_CASES if e not in STOP_CASES]


@pytest.mark.parametrize("from_state, to_state, cause",
                         sorted((f, t, c) for (f, t), cs in EDGES.items() for c in cs if c in ENVELOPE_CAUSES))
def test_the_private_walk_refuses_every_envelope_cause(tmp_path, clock, repo, from_state, to_state, cause):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, from_state)
    with pytest.raises(TransitionRefused, match="written only with its envelope"):
        store._transition(job.id, to_state, cause)
    assert store.get(job.id).history == job.history


@pytest.mark.parametrize("from_state, to_state, cause", WALK_CASES)
def test_every_allowed_edge_is_taken(tmp_path, clock, repo, from_state, to_state, cause):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, from_state)
    moved = store._transition(job.id, to_state, cause, evidence="e")
    assert moved.state == to_state and moved.history[-1].cause == cause and moved.history[-1].evidence == "e"
    assert [t.seq for t in moved.history] == list(range(1, len(moved.history) + 1))


@pytest.mark.parametrize("from_state, to_state, cause", STOP_CASES)
def test_every_edge_out_of_running_but_runs_exited_is_stop_recorded_alone(tmp_path, clock, repo, from_state, to_state,
                                                                          cause):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, from_state)
    clock.now = NOW + timedelta(hours=2)                                     # past the lease: lease_lapsed holds
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        store._transition(job.id, to_state, cause, evidence="e")
    assert store.get(job.id).state == "running"
    moved = store.stop_recorded(job.id, cause, why="reaped")
    assert (moved.state, moved.history[-1].cause) == (to_state, cause)
    assert json.loads(moved.history[-1].evidence) == {"stopped": True, "why": "reaped"}


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
    assert EDGES[("authorised", "failed")] == {"base_moved"}
    assert "base_moved" in EDGES[("verified", "failed")] and "base_moved" in EDGES[("recommended", "failed")]
    assert EDGES[("verified", "authorised")] == EDGES[("recommended", "authorised")] == {
        "human_authorises", "human_adoption_rule", "envelope_rule"}
    assert EDGES[("filed", "signed")] == {"signature", "envelope"}
    for f in ("filed", "signed", "queued", "running", "verifying", "verified", "recommended"):
        assert "envelope_ended" in EDGES[(f, "failed")]
    assert "envelope_ended" not in EDGES[("authorised", "failed")]


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
    with pytest.raises(TransitionRefused, match=r"recommend is only for a verified job \(job is filed\)"):
        store.recommend(job.id, cap)


def test_recommend_names_the_state_on_every_non_verified_job(store, repo):
    """L4 F5: 'a job cannot go from signed to recommended' did not tell the planner what recommend is for."""
    for state in ("signed", "verifying"):
        job, cap = at_state(store, repo, state)
        with pytest.raises(TransitionRefused, match=rf"recommend is only for a verified job \(job is {state}\)"):
            store.recommend(job.id, cap)
        store.cancel_by_planner(job.id, cap)
    job, cap = at_state(store, repo, "verified")
    assert store.recommend(job.id, cap).state == "recommended"


def test_recommend_with_a_wrong_capability_is_a_capability_refusal_first(store, repo):
    job, _cap = file_job(store, repo)
    with pytest.raises(CapabilityRefused):
        store.recommend(job.id, "not-the-capability")


def test_recommend_keeps_the_proposed_branch_and_run_as_its_evidence(store, repo):
    import json

    job, cap = at_state(store, repo, "verified")
    out = store.recommend(job.id, cap, onto="main", run=1)
    assert (out.history[-1].to_state, json.loads(out.history[-1].evidence)) == ("recommended", {"onto": "main",
                                                                                               "run": 1})


def test_a_recommend_with_no_proposal_writes_no_evidence(store, repo):
    job, cap = at_state(store, repo, "verified")
    assert store.recommend(job.id, cap).history[-1].evidence == ""


@pytest.mark.parametrize("over, match", [
    ({"onto": "refs/heads/main"}, "bare name"), ({"onto": "levain/x"}, "under levain/"),
    ({"onto": "no-such-branch"}, "not a branch here"), ({"run": 2}, r"runs 0\.\.1"), ({"run": True}, r"runs 0\.\.1"),
])
def test_a_bad_proposal_is_refused_at_recommend_and_nothing_is_written(store, repo, over, match):
    job, cap = at_state(store, repo, "verified")
    with pytest.raises(TransitionRefused, match=match):
        store.recommend(job.id, cap, **over)
    assert store.get(job.id).state == "verified"


def test_check_capability_reads_and_writes_nothing(store, repo):
    job, cap = at_state(store, repo, "verified")
    assert store.check_capability(job.id, cap).id == job.id
    with pytest.raises(CapabilityRefused):
        store.check_capability(job.id, cap + "x")
    assert store.get(job.id).history == job.history


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
    with pytest.raises(SignatureRefused, match="is signed; only a filed job can be signed"):
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
    with pytest.raises(SignatureRefused, match="is failed; only a filed job can be signed"):
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
    assert JobStore().directory == (tmp_path / "home" / "consent").resolve()


def test_a_naive_clock_is_refused(tmp_path, repo):
    store = new_store(tmp_path, lambda: NOW.replace(tzinfo=None))
    with pytest.raises(Exception, match="naive"):
        store.list()


# --- fix round 1 (L1/L2 slice a) -----------------------------------------------------------------------------------

_SEED = ("INSERT INTO jobs (id, digest, contract, filed_at, filer_session, capability_sha256, idem_key, repo, "
         f"expires_at, unsigned_until, writer_format) VALUES (?, ?, 'c', 't', 's', 'cap', ?, '/r', 'e', 'u', "
         f"'{FORMAT}')")


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


def test_overlap_counts_an_ancestor_or_descendant_write_path(store, repo):
    """L4 F7: job A creating src/sub and job B creating src/sub/deep.py were both live; only one can land."""
    wide = registry(template(write_paths=WRITE_SET | {"src/sub", "src/Sub/deep.py"}))
    one, cap = file_job(store, repo, write=["src/sub"], resolver=wide)
    with pytest.raises(ContractRefused, match=f"live job {one.id} already writes src/sub, and this contract's "
                                              "src/sub/deep.py is under it; one live contract per write file"):
        file_job(store, repo, write=["src/sub/deep.py"], resolver=wide)
    with pytest.raises(ContractRefused, match=f"live job {one.id} already writes src/sub"):
        file_job(store, repo, write=["src/Sub/deep.py"], resolver=wide)          # casefolded, as equal paths are
    store.cancel_by_planner(one.id, cap)
    two, _ = file_job(store, repo, write=["src/sub/deep.py"], resolver=wide)
    with pytest.raises(ContractRefused, match=f"live job {two.id} already writes src/sub/deep.py, and this "
                                              "contract's src/sub is a directory above it"):
        file_job(store, repo, write=["src/sub"], resolver=wide)
    # a shared name prefix is not an ancestor: src/subway.py is free while src/sub/deep.py is live
    sib = registry(template(write_paths=WRITE_SET | {"src/subway.py"}))
    assert file_job(store, repo, write=["src/subway.py"], resolver=sib)[0].state == "filed"


def test_a_replay_with_another_digest_says_how_to_retry(store, repo, clock):
    """L4 F2: a planner that rebuilt expires_at on retry was refused with no hint of what to resend."""
    file_job(store, repo, idem_key="k1")
    with pytest.raises(ContractRefused, match=r"resend the IDENTICAL submission \(the same expires_at included\), "
                                              "and a lost capability cannot be re-issued"):
        file_job(store, repo, idem_key="k1", expires_at=utc_stamp(NOW + timedelta(hours=2, seconds=1)))


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
    with pytest.raises(SignatureRefused, match="is failed; only a filed job can be signed"):
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


def test_an_authority_log_that_cannot_be_read_is_a_machine_fault(store, repo, keys, clock, tmp_path):
    # L1 M1 (fix r1): a log that cannot be read is a fault on this machine, raised as itself, never "refused"
    from levain.consent.queue import authority_source
    bad = tmp_path / "cockpit.db"
    bad.write_bytes(b"this is not a database" * 100)
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    cpayload, csig = cancel_item(job, keys["phone"])
    for source in (authority_source(bad, anchor=None),
                   authority_source(tmp_path / "missing" / "cockpit.db", anchor=None)):
        with pytest.raises(sqlite3.Error):
            store.sign(job.id, payload, sig, source)
        with pytest.raises(sqlite3.Error):
            store.cancel_by_human(job.id, cpayload, csig, source)
    assert store.get(job.id).state == "filed"


def test_an_authority_log_the_replay_refuses_stays_a_signature_refusal(store, repo, keys, log, clock, tmp_path):
    from levain.consent.queue import authority_source
    anchor = tmp_path / "authority.anchor"
    anchor.write_text("not an anchor")
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="the authority log is refused"):
        store.sign(job.id, payload, sig, authority_source(log.path, anchor=anchor))
    cpayload, csig = cancel_item(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="the authority log is refused"):
        store.cancel_by_human(job.id, cpayload, csig, authority_source(log.path, anchor=anchor))
    assert store.get(job.id).state == "filed"


def test_a_sign_verifier_that_cannot_run_is_a_machine_fault(tmp_path, repo, keys, log, clock):
    # L1 M1 (fix r1): the contract signature's twin of the cancel's verifier fault
    from levain.authority import VerifierUnavailable
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    store = JobStore(d, clock=clock, ssh_keygen=tmp_path / "no-ssh-keygen")
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = signed(job, keys["phone"])
    with pytest.raises(VerifierUnavailable):
        store.sign(job.id, payload, sig, log.source)
    assert store.get(job.id).state == "filed"


def test_a_live_job_whose_stored_contract_no_longer_parses_is_a_damaged_store(store, repo):
    # glm: a damaged live row surfaced as ContractRefused, blaming the new submission
    path, _base = repo
    store.list()                                            # the store exists
    damaged = "dj-" + "f" * 32
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute("INSERT INTO jobs (id, digest, contract, filed_at, filer_session, capability_sha256, idem_key, "
                     "repo, expires_at, unsigned_until, writer_format) "
                     "VALUES (?, ?, 'not a contract', ?, 's', ?, NULL, ?, ?, ?, ?)",
                     (damaged, "sha256:" + "0" * 64, utc_stamp(NOW), "0" * 64, str(path),
                      utc_stamp(NOW + timedelta(hours=1)), utc_stamp(NOW + timedelta(minutes=20)), FORMAT))
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


# --- the human cancel (slice b1) ----------------------------------------------------------------------------------

CNS = dsign.DISPATCH_CANCEL_NAMESPACE


def cancel_item(job, key, at=None, reason="not this one", ns=CNS, **over):
    payload = dsign.cancel_payload(job, signer_fp=key.fp, signed_at=at or NOW + timedelta(minutes=1), reason=reason)
    payload.update(over)
    return payload, key.sign(dsign.sign_message(payload), ns)


@pytest.mark.parametrize("state", ["filed", "signed", "queued", "verified", "recommended", "authorised"])
def test_the_human_cancels_any_live_job(tmp_path, clock, repo, keys, log, state):
    store = new_store(tmp_path, clock)
    job, _cap = at_state(store, repo, state)
    clock.now = NOW + timedelta(minutes=2)
    payload, sig = cancel_item(job, keys["laptop"])
    out = store.cancel_by_human(job.id, payload, sig, log.source)
    assert out.state == "cancelled" and out.history[-1].cause == "human_cancel"
    assert '"reason": "not this one"' in out.history[-1].evidence and keys["laptop"].fp in out.history[-1].evidence
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT payload, signature, signer_fp FROM cancel_signatures").fetchall() == [
            (dsign.sign_message(payload).decode(), sig, keys["laptop"].fp)]
        assert conn.execute("SELECT COUNT(*) FROM signatures WHERE payload LIKE '%dispatch-cancel%'").fetchone() == (0,)


def test_a_running_job_is_not_cancelled_by_the_human_door_but_by_the_supervisor(tmp_path, clock, repo, keys, log):
    store = new_store(tmp_path, clock)
    job, cap = at_state(store, repo, "running")
    clock.now = NOW + timedelta(minutes=2)
    payload, sig = cancel_item(job, keys["laptop"])
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        store.cancel_by_human(job.id, payload, sig, log.source)
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        store.cancel_by_planner(job.id, cap)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cancel_signatures").fetchone() == (0,)
    assert store.get(job.id).state == "running"


@pytest.mark.parametrize("state", ["adopted", "failed", "cancelled"])
def test_a_terminal_job_cannot_be_cancelled(tmp_path, clock, repo, keys, log, state):
    store = new_store(tmp_path, clock)
    job, _cap = at_state(store, repo, state)
    payload, sig = cancel_item(job, keys["laptop"])
    with pytest.raises(TransitionRefused, match=f"is {state}, which is final"):
        store.cancel_by_human(job.id, payload, sig, log.source)


def test_a_contract_signature_presented_as_a_cancel_is_refused(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    spayload, ssig = signed(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="not a dispatch cancel record"):
        store.cancel_by_human(job.id, spayload, ssig, log.source)
    # a cancel payload signed in the contract namespace
    payload, sig = cancel_item(job, keys["phone"], ns=NS)
    with pytest.raises(SignatureRefused, match="does not verify"):
        store.cancel_by_human(job.id, payload, sig, log.source)
    # and a cancel presented as a contract signature
    payload, sig = cancel_item(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="not a dispatch signature record"):
        store.sign(job.id, payload, sig, log.source)
    assert store.get(job.id).state == "filed"


@pytest.mark.parametrize("over, match", [
    ({"store_id": "0" * 32}, "another dispatch store"),
    ({"job_id": "dj-" + "0" * 32}, "another job"),
    ({"digest": "sha256:" + "0" * 64}, "another contract digest"),
])
def test_a_cancel_for_another_store_job_or_digest_is_refused(store, repo, keys, log, clock, over, match):
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["phone"], **over)
    with pytest.raises(SignatureRefused, match=match):
        store.cancel_by_human(job.id, payload, sig, log.source)
    assert store.get(job.id).state == "filed"


def test_a_cancel_by_a_revoked_key_is_refused(store, repo, keys, log):
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["revoked"])
    with pytest.raises(SignatureRefused, match="revoked"):
        store.cancel_by_human(job.id, payload, sig, log.source)


def test_a_future_dated_cancel_is_refused(store, repo, keys, log):
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["phone"], at=NOW + timedelta(minutes=10))
    with pytest.raises(SignatureRefused, match="the cancel is dated in the future"):
        store.cancel_by_human(job.id, payload, sig, log.source)
    # inside the skew it is the operator's
    payload, sig = cancel_item(job, keys["phone"], at=NOW + timedelta(minutes=1))
    assert store.cancel_by_human(job.id, payload, sig, log.source).state == "cancelled"


def test_a_cancel_takes_the_authority_source_never_a_snapshot(store, repo, keys, snapshot):
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["phone"])
    with pytest.raises(SignatureRefused, match="AuthoritySource"):
        store.cancel_by_human(job.id, payload, sig, snapshot)


def test_a_cancel_verifier_that_cannot_run_is_a_machine_fault(tmp_path, repo, keys, log, clock):
    from levain.authority import VerifierUnavailable
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    store = JobStore(d, clock=clock, ssh_keygen=tmp_path / "no-ssh-keygen")
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["phone"])
    with pytest.raises(VerifierUnavailable):
        store.cancel_by_human(job.id, payload, sig, log.source)
    assert store.get(job.id).state == "filed"


def test_an_authorised_job_cancelled_by_the_human_releases_its_write_files(store, repo, keys, log, clock):
    job, _cap = at_state(store, repo, "authorised")
    with pytest.raises(ContractRefused, match="already writes"):
        file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    payload, sig = cancel_item(job, keys["laptop"])
    store.cancel_by_human(job.id, payload, sig, log.source)
    again, _ = file_job(store, repo)                 # the same write files, now free
    assert again.state == "filed" and again.id != job.id


def test_the_cancel_table_is_append_only(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    payload, sig = cancel_item(job, keys["phone"])
    store.cancel_by_human(job.id, payload, sig, log.source)
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql in ("UPDATE cancel_signatures SET signature = 'y'", "DELETE FROM cancel_signatures",
                    f"INSERT OR REPLACE INTO cancel_signatures VALUES ('{job.id}', 'p2', 's2', 'f', 'a')",
                    f"INSERT INTO cancel_signatures VALUES ('{job.id}', 'p2', 's2', 'f', 'a')"):
            with pytest.raises(sqlite3.IntegrityError, match="written once|never replaced"):
                conn.execute(sql)
    finally:
        conn.close()


# --- fix round 1 ---------------------------------------------------------------------------------------------------

def test_a_stale_cancel_never_undoes_a_later_contract_signature(store, repo, keys, log, clock):
    # L2 Q2: a cancel signed before the operator signed the contract could still kill it
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=5)
    job = store._transition(job.id, "signed", "signature")
    stale, stale_sig = cancel_item(job, keys["phone"], at=NOW + timedelta(minutes=2))
    with pytest.raises(SignatureRefused, match="before the job's signature at"):
        store.cancel_by_human(job.id, stale, stale_sig, log.source)
    assert store.get(job.id).state == "signed"
    clock.now = NOW + timedelta(minutes=6)
    fresh, fresh_sig = cancel_item(job, keys["phone"], at=NOW + timedelta(minutes=6))
    assert store.cancel_by_human(job.id, fresh, fresh_sig, log.source).state == "cancelled"


def test_a_stale_cancel_never_undoes_a_later_authorisation(store, repo, keys, log, clock):
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    for to, cause in PATH["verified"]:
        job = store._transition(job.id, to, cause)
    clock.now = NOW + timedelta(minutes=10)
    job = store._transition(job.id, "authorised", "human_authorises")
    stale, stale_sig = cancel_item(job, keys["laptop"], at=NOW + timedelta(minutes=5))
    with pytest.raises(SignatureRefused, match="before the job's human_authorises at"):
        store.cancel_by_human(job.id, stale, stale_sig, log.source)
    assert store.get(job.id).state == "authorised"


def test_the_filing_agent_is_kept_beside_the_job(store, repo):
    # L2 Q1 LOW: the signer could not see which agent filed the job
    path, base = repo
    job, _ = store.file(submission(path, base), resolve_template=registry(), policy=POLICY, filer_session="sess-1",
                        filer_agent="codex")
    assert job.filer_agent == "codex" and store.get(job.id).filer_agent == "codex"
    other, _ = file_job(store, repo, write=["src/b.py"], read=["src/b.py"])
    assert other.filer_agent is None
    with pytest.raises(DispatchError, match="filer_agent must be one line"):
        store.file(submission(path, base), resolve_template=registry(), policy=POLICY, filer_session="sess-1",
                   filer_agent="co\ndex")
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql in ("UPDATE job_filers SET agent = 'claude_code'", "DELETE FROM job_filers",
                    f"INSERT OR REPLACE INTO job_filers VALUES ('{job.id}', 'claude_code')"):
            with pytest.raises(sqlite3.IntegrityError, match="written once|never replaced"):
                conn.execute(sql)
    finally:
        conn.close()


# --- F11-s1 --------------------------------------------------------------------------------------------------------

def test_a_rule_firing_never_makes_a_fresher_human_cancel_stale(store, repo, keys, log, clock):
    # a rule fires when it fires, not when the human signed it: a cancel signed before the rule fired is still the
    # human's latest word over that job (staleness is dated by real signatures only)
    job, _ = file_job(store, repo)
    clock.now = NOW + timedelta(minutes=1)
    for to, cause in PATH["verified"]:
        job = store._transition(job.id, to, cause)
    clock.now = NOW + timedelta(minutes=10)
    job = store.authorise(job.id, cause="human_adoption_rule", evidence="{}")
    payload, sig = cancel_item(job, keys["laptop"], at=NOW + timedelta(minutes=5))
    assert store.cancel_by_human(job.id, payload, sig, log.source).state == "cancelled"


def test_authorise_writes_only_from_verified_or_recommended_and_pairs_the_human_cause(store, repo):
    job, _ = at_state(store, repo, "running")
    with pytest.raises(TransitionRefused, match="only a verified or recommended"):
        store.authorise(job.id, cause="human_adoption_rule", evidence="{}")
    with pytest.raises(TransitionRefused, match="human's signed item"):
        store.authorise(job.id, cause="human_authorises", evidence="{}")


def test_the_authorise_table_is_append_only(store, repo):
    job, _ = file_job(store, repo)
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute(f"INSERT INTO authorise_signatures VALUES ('{job.id}', 'p', 's', 'f', 'a')")
        for sql in ("UPDATE authorise_signatures SET signature = 'y'", "DELETE FROM authorise_signatures",
                    f"INSERT OR REPLACE INTO authorise_signatures VALUES ('{job.id}', 'p2', 's2', 'f', 'a')",
                    f"INSERT INTO authorise_signatures VALUES ('{job.id}', 'p2', 's2', 'f', 'a')"):
            with pytest.raises(sqlite3.IntegrityError, match="written once|never replaced"):
                conn.execute(sql)
    finally:
        conn.close()


# --- W2-a: the verifier writer and the run records -----------------------------------------------------------------

VERIFIED_AT = utc_stamp(NOW + timedelta(minutes=5))


def base_tree(repo) -> str:
    from tests.test_dispatch_contract import git
    return git(repo[0], "rev-parse", f"{repo[1]}^{{tree}}")


def model(run=0) -> dict:
    return {"requested": "glm-5", "answered": "glm-5", "backend_identity": f"backend-{run}"}


def verify(store, repo, job, run=0, *, verdicts=("pass", "pass"), entry=None, raw=None, ev=None, **kw):
    """One verifier session's end for ``run`` of ``job``, its envelope built from the fixture unless given."""
    from tests import test_dispatch_verifier_report as tvr

    r = tvr.vrun(job.id, run, entry)
    if raw is None:
        raw, digest = tvr.envelope(r, tvr.body(verdicts))
        ev = ev or tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest)
    return store.record_verification(r, envelope=raw, event=ev or tvr.event(r, at=VERIFIED_AT), repo=str(repo[0]),
                                     tree=kw.pop("tree", base_tree(repo)), model=kw.pop("model", model(run)), **kw)


def staged_refs(repo, job) -> list[str]:
    from tests.test_dispatch_contract import git
    out = git(repo[0], "for-each-ref", "--format=%(refname)", f"refs/heads/levain/dispatch/{job.id}/")
    return out.splitlines()


def test_every_run_passing_verifies_the_job_and_the_records_read_back(store, repo):
    from levain.dispatch.adopt import artifact_digest, artifact_problem, staged_ref
    from tests import test_dispatch_verifier_report as tvr
    from tests.test_dispatch_contract import git

    job, _ = at_state(store, repo, "verifying")
    after_one = verify(store, repo, job, 0)
    assert after_one.state == "verifying" and after_one.history == job.history       # a record, no transition
    done = verify(store, repo, job, 1)
    assert done.state == "verified" and done.history[-1].cause == "verifier_pass"
    recs = store.run_records(job.id)
    assert [r.run for r in recs] == [0, 1]
    for r in recs:
        assert r.verifier == {"id": "pytest-bb", "hash": tvr.ENTRY["verifier_artifact_digest"], "kind": "black-box",
                              "result": "pass"}
        assert r.verified_at == VERIFIED_AT and r.model == model(r.run)
        assert artifact_problem(str(repo[0]), done, r) is None
        assert git(repo[0], "rev-parse", staged_ref(job.id, r.run)) == r.staged_commit
    conn = sqlite3.connect(store.path)
    try:
        rows = conn.execute("SELECT run, digest, envelope_digest FROM run_records WHERE job_id = ? ORDER BY run",
                            (job.id,)).fetchall()
    finally:
        conn.close()
    assert [d for _r, d, _e in rows] == [artifact_digest(r) for r in recs]
    assert all(e.startswith("sha256:") for _r, _d, e in rows)


def test_a_fail_writes_its_record_and_fails_the_job_naming_the_step(store, repo):
    job, _ = at_state(store, repo, "verifying")
    out = verify(store, repo, job, 0, verdicts=("pass", "fail"))
    assert out.state == "failed" and out.history[-1].cause == "verifier_fail"
    assert out.history[-1].evidence == '{"note": "step 1 fail", "run": 0, "step": 1}'
    assert [(r.run, r.verifier["result"]) for r in store.run_records(job.id)] == [(0, "fail")]


def test_a_failed_job_leaves_no_staged_ref_even_for_a_run_that_passed_first(store, repo):
    job, _ = at_state(store, repo, "verifying")
    verify(store, repo, job, 0)
    assert len(staged_refs(repo, job)) == 1
    out = verify(store, repo, job, 1, verdicts=("fail", "pass"))
    assert out.state == "failed" and staged_refs(repo, job) == []
    assert [r.run for r in store.run_records(job.id)] == [0, 1]


def test_a_refused_event_fails_the_job_with_its_name_and_no_record(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    out = store.record_verification(r, envelope=None, event=tvr.event(r, at=VERIFIED_AT,
                                                                      verifier_report_refused="driver-timeout"),
                                    repo=str(repo[0]), tree=base_tree(repo), model=model())
    assert (out.state, out.history[-1].cause, out.history[-1].evidence) == ("failed", "verifier_refused",
                                                                             "driver-timeout")
    assert store.run_records(job.id) == [] and staged_refs(repo, job) == []


def test_a_digest_mismatch_is_infrastructure_never_the_verifiers_fail(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r)
    out = verify(store, repo, job, 0, raw=raw + b" ", ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest))
    assert (out.state, out.history[-1].cause, out.history[-1].evidence) == ("failed", "verifier_refused",
                                                                             "report-digest-mismatch")
    assert store.run_records(job.id) == [] and staged_refs(repo, job) == []


def test_an_envelope_named_but_not_read_is_report_missing(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    out = store.record_verification(r, envelope=None, event=tvr.event(r, at=VERIFIED_AT,
                                                                      verifier_report_digest="sha256:" + "1" * 64),
                                    repo=str(repo[0]), tree=base_tree(repo), model=model())
    assert out.history[-1].evidence == "report-missing"


def test_a_minted_verifier_that_is_not_the_templates_is_refused_before_staging(store, repo):
    from levain.dispatch import verifier_report as vr
    from levain.dispatch.jobs import NOT_THE_TEMPLATES
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    # a consistent entry (its artifact digest recomputed) for another artifact than the template pins
    other = {**tvr.ENTRY, "driver_template_digest": "sha256:" + "f" * 64}
    other["verifier_artifact_digest"] = tvr.vr.artifact_digest(other)
    out = verify(store, repo, job, 0, entry=other)
    assert (out.state, out.history[-1].cause, out.history[-1].evidence) == ("failed", "verifier_refused",
                                                                             NOT_THE_TEMPLATES)
    assert staged_refs(repo, job) == [] and store.run_records(job.id) == []


@pytest.mark.parametrize("field, value", [("declared_kind", "in-process"), ("verifier_id", "other-bb")])
def test_each_identity_field_of_the_entry_is_checked(tmp_path, clock, repo, field, value):
    from levain.dispatch.jobs import NOT_THE_TEMPLATES
    from tests import test_dispatch_verifier_report as tvr

    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, "verifying")
    entry = {**tvr.ENTRY, field: value}
    entry["verifier_artifact_digest"] = tvr.vr.artifact_digest(entry)     # consistent: only the identity differs
    out = verify(store, repo, job, 0, entry=entry)
    assert out.history[-1].evidence == NOT_THE_TEMPLATES


def test_a_run_is_recorded_once(store, repo):
    job, _ = at_state(store, repo, "verifying")
    verify(store, repo, job, 0)
    before = store.get(job.id).history
    with pytest.raises(TransitionRefused, match="already recorded"):
        verify(store, repo, job, 0)
    assert store.get(job.id).history == before and len(store.run_records(job.id)) == 1


def test_the_run_records_table_is_append_only(store, repo):
    job, _ = at_state(store, repo, "verifying")
    verify(store, repo, job, 0)
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        for sql in ("UPDATE run_records SET digest = 'x'", "DELETE FROM run_records",
                    f"INSERT OR REPLACE INTO run_records VALUES ('{job.id}', 0, 'r', 'd', 'e', 'a')",
                    f"INSERT INTO run_records VALUES ('{job.id}', 0, 'r', 'd', 'e', 'a')"):
            with pytest.raises(sqlite3.IntegrityError, match="written once|never replaced"):
                conn.execute(sql)
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["running", "verified", "failed"])
def test_only_a_verifying_job_records_its_verifier(tmp_path, clock, repo, state):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, state)
    with pytest.raises(TransitionRefused, match="only a verifying job"):
        verify(store, repo, job, 0)
    assert staged_refs(repo, job) == []


def test_an_event_that_is_not_this_sessions_is_refused_and_writes_nothing(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r)
    good = tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest)
    for ev, match in (({**good, "session": "v-other"}, "another session"),
                      ({**good, "ticket": "t-other"}, "another ticket"),
                      ({**good, "kind": "artifact"}, "state event"),
                      ({**good, "verifier_report_refused": "driver-timeout"}, "exactly one"),
                      ({k: v for k, v in good.items() if k != "verifier_report_digest"}, "exactly one"),
                      ({**good, "verifier_report_digest": "sha1:x"}, "sha256"),
                      ({**{k: v for k, v in good.items() if k != "verifier_report_digest"},
                        "verifier_report_refused": "a\nb"}, "one line")):
        with pytest.raises(DispatchError, match=match):
            store.record_verification(r, envelope=raw, event=ev, repo=str(repo[0]), tree=base_tree(repo),
                                      model=model())
    assert store.get(job.id).history == job.history


def test_a_run_outside_the_template_another_repo_or_a_bad_record_writes_nothing(store, repo, tmp_path):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    with pytest.raises(DispatchError, match="not one of the template's runs"):
        verify(store, repo, job, 2)
    other = make_repo(tmp_path / "other")
    with pytest.raises(DispatchError, match="another repository"):
        r = tvr.vrun(job.id)
        store.record_verification(r, envelope=None, event=tvr.event(r, at=VERIFIED_AT,
                                                                    verifier_report_refused="driver-timeout"),
                                  repo=str(other[0]), tree=base_tree(repo), model=model())
    with pytest.raises(DispatchError, match="model is exactly"):
        verify(store, repo, job, 0, model={"requested": "glm-5"})
    with pytest.raises(DispatchError, match="tree is a full object id"):
        verify(store, repo, job, 0, tree="HEAD")
    assert store.get(job.id).history == job.history and staged_refs(repo, job) == []


def test_a_crash_between_staging_and_the_record_does_not_wedge_the_retry(store, repo):
    from levain.dispatch.adopt import stage_commit

    job, _ = at_state(store, repo, "verifying")
    staged = stage_commit(str(repo[0]), job_id=job.id, run=0, contract_digest=job.digest, base=repo[1],
                          tree=base_tree(repo), verified_at=VERIFIED_AT)
    verify(store, repo, job, 0)
    assert store.run_records(job.id)[0].staged_commit == staged


@pytest.mark.parametrize("damage", ["record", "digest"])
def test_a_record_that_no_longer_matches_its_digest_is_never_skipped(store, repo, damage):
    job, _ = at_state(store, repo, "verifying")
    verify(store, repo, job, 0)
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute("DROP TRIGGER run_records_frozen")
        if damage == "record":
            conn.execute("UPDATE run_records SET record = replace(record, 'backend-0', 'backend-9')")
        else:
            conn.execute(f"UPDATE run_records SET digest = 'sha256:{'0' * 64}'")
    finally:
        conn.close()
    with pytest.raises(JobStoreError, match="does not match its digest"):
        store.run_records(job.id)


def test_run_records_of_an_unknown_job_is_refused(store):
    with pytest.raises(TransitionRefused, match="no dispatch job"):
        store.run_records("dj-" + "0" * 32)


def test_a_second_writer_recording_the_same_run_first_is_refused_inside_the_transaction(store, repo, monkeypatch):
    from levain.dispatch import adopt

    job, _ = at_state(store, repo, "verifying")
    real = adopt.artifact_problem
    raced = []

    def racing(repo_path, j, record):
        # another writer records this run between this call's pre-read and its write transaction
        if not raced:
            raced.append(True)
            monkeypatch.setattr(adopt, "artifact_problem", real)
            verify(JobStore(store.directory, clock=store._clock), repo, job, 0)
        return real(repo_path, j, record)

    monkeypatch.setattr(adopt, "artifact_problem", racing)
    with pytest.raises(TransitionRefused, match="already recorded"):
        verify(store, repo, job, 0)
    assert raced and len(store.run_records(job.id)) == 1 and store.get(job.id).state == "verifying"


# --- L3 r1 -----------------------------------------------------------------------------------------------------------

def test_a_minted_entry_of_only_the_template_fields_fails_as_verifier_refused(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    entry = {k: tvr.ENTRY[k] for k in ("verifier_id", "verifier_artifact_digest", "declared_kind")}
    out = verify(store, repo, job, 0, entry=entry)
    assert (out.state, out.history[-1].cause, out.history[-1].evidence) == ("failed", "verifier_refused",
                                                                             "verifier-entry")
    assert staged_refs(repo, job) == [] and store.run_records(job.id) == []


@pytest.mark.parametrize("state", ["ACTIVE", "REVOKED", None])
def test_an_event_in_a_state_that_is_not_terminal_is_refused_and_writes_nothing(store, repo, state):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r)
    ev = tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest)
    if state is None:
        del ev["state"]
    else:
        ev["state"] = state
    with pytest.raises(DispatchError, match="RETAINED or HELD"):
        verify(store, repo, job, 0, raw=raw, ev=ev)
    assert store.get(job.id).history == job.history and staged_refs(repo, job) == []


def test_a_held_session_is_a_terminal_event(store, repo):
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r)
    out = verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest,
                                                            state="HELD"))
    assert out.state == "verifying" and len(store.run_records(job.id)) == 1


def test_a_refused_result_for_a_run_recorded_meanwhile_is_refused_never_fails_the_job(store, repo, monkeypatch):
    from levain.dispatch import jobs as jobs_mod
    from levain.dispatch.verifier_report import ReportRefused

    job, _ = at_state(store, repo, "verifying")
    real = jobs_mod.check_envelope
    raced = []

    def racing(raw, *, expected, event_digest):
        # another writer records this run with a pass between this call's pre-read and its write transaction
        if not raced:
            raced.append(True)
            monkeypatch.setattr(jobs_mod, "check_envelope", real)
            verify(JobStore(store.directory, clock=store._clock), repo, job, 0)
        raise ReportRefused("report-missing")

    monkeypatch.setattr(jobs_mod, "check_envelope", racing)
    with pytest.raises(TransitionRefused, match="already recorded"):
        verify(store, repo, job, 0)
    assert raced and store.get(job.id).state == "verifying" and len(store.run_records(job.id)) == 1


def test_a_staging_writer_that_finds_the_job_failed_deletes_its_staged_refs(store, repo, monkeypatch):
    from levain.dispatch import adopt

    job, _ = at_state(store, repo, "verifying")
    real = adopt.artifact_problem

    def racing(repo_path, j, record):
        # the job fails by another cause after this writer staged its run's commit
        monkeypatch.setattr(adopt, "artifact_problem", real)
        JobStore(store.directory, clock=store._clock)._transition(job.id, "failed", "budget_cap")
        return real(repo_path, j, record)

    monkeypatch.setattr(adopt, "artifact_problem", racing)
    with pytest.raises(TransitionRefused, match="only a verifying job"):
        verify(store, repo, job, 0)
    assert store.get(job.id).state == "failed" and staged_refs(repo, job) == []


@pytest.mark.parametrize("text", ["not json", '{"run": 0}', '{"verifier": []}', "[]",
                                  # a row that only says pass is not a record (L3 r2 G2): the pass count reads rows
                                  # with run_records' own check
                                  '{"verifier":{"result":"pass"}}'])
def test_an_unreadable_stored_record_in_the_pass_count_is_a_damaged_store(tmp_path, clock, repo, text):
    store = new_store(tmp_path, clock)
    job, _ = at_state(store, repo, "verifying")
    verify(store, repo, job, 0)
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        conn.execute("DROP TRIGGER run_records_frozen")
        conn.execute("UPDATE run_records SET record = ?", (text,))
    finally:
        conn.close()
    with pytest.raises(JobStoreError, match="damaged"):
        verify(store, repo, job, 1)
    assert store.get(job.id).state == "verifying"


OTHER_DIGEST = "sha256:" + "0" * 64


def test_a_repeat_names_itself_and_another_report_for_the_run_is_a_conflict(store, repo):
    """record_verification tells an idempotent repeat (this event's outcome held) from a conflict (another report's
    record for the run) and from a job ended by another cause, so its caller ACKs only what the store holds."""
    from levain.dispatch.jobs import AlreadyRecorded, RecordConflict
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r, tvr.body(("pass", "pass")))
    verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest))
    with pytest.raises(AlreadyRecorded):
        verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=digest))
    with pytest.raises(RecordConflict):
        verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=OTHER_DIGEST))
    with pytest.raises(RecordConflict):
        verify(store, repo, job, 0, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_refused="verifier-failed:exit"))


def test_a_refusal_repeat_is_a_repeat_only_on_its_ticket_and_outcome(store, repo):
    """RULINGS_OBS 10: the refusal that failed a job is stored with its run's ticket and the event's outcome, and only
    that ticket's same outcome is AlreadyRecorded. Another run's refusal of the same name, after the job failed, is a
    plain refusal (its follower passes it without an ACK); the same ticket with another outcome is a conflict."""
    from levain.dispatch.jobs import AlreadyRecorded, RecordConflict
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r0, r1 = tvr.vrun(job.id, 0), tvr.vrun(job.id, 1)
    verify(store, repo, job, 0, ev=tvr.event(r0, at=VERIFIED_AT, verifier_report_refused="verifier-failed:exit"))
    with pytest.raises(AlreadyRecorded):
        verify(store, repo, job, 0, ev=tvr.event(r0, at=VERIFIED_AT, verifier_report_refused="verifier-failed:exit"))
    with pytest.raises(TransitionRefused) as exc:
        verify(store, repo, job, 1, ev=tvr.event(r1, at=VERIFIED_AT, verifier_report_refused="verifier-failed:exit"))
    assert type(exc.value) is TransitionRefused
    with pytest.raises(RecordConflict):
        verify(store, repo, job, 0, ev=tvr.event(r0, at=VERIFIED_AT, verifier_report_refused="verifier-failed:other"))


def test_a_report_the_writer_refused_repeats_as_already_recorded(store, repo):
    """A digest event whose envelope the writer refused failed the job under the envelope's refusal name: its repeat
    is matched on the ticket and the event's digest, not on that name."""
    from levain.dispatch.jobs import AlreadyRecorded
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r, tvr.body(("pass", "pass")))
    bad = "sha256:" + "0" * 64                                         # the event names a digest the envelope is not
    out = verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=bad))
    assert out.state == "failed" and out.history[-1].cause == "verifier_refused"
    with pytest.raises(AlreadyRecorded):
        verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_digest=bad))


def test_a_refusal_failure_with_no_stored_ticket_is_a_plain_refusal(store, repo):
    """A job failed verifier_refused by a writer that stored no ticket (a store written before the ticket was kept):
    a matching name is not enough, so the repeat is a plain refusal and gets no ACK."""
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    store._transition(job.id, "failed", "verifier_refused", evidence="verifier-failed:exit")
    with pytest.raises(TransitionRefused) as exc:
        verify(store, repo, job, 0, ev=tvr.event(r, at=VERIFIED_AT, verifier_report_refused="verifier-failed:exit"))
    assert type(exc.value) is TransitionRefused


def test_a_control_stamped_event_records_with_the_stores_verified_at(store, repo):
    """CONTROL stamps a session event to the second (records.timestamp); record_verification takes it as it arrives
    and writes the instant in the job store's own stamp."""
    from datetime import datetime, timezone

    from levain.slot import records as SR
    from tests import test_dispatch_verifier_report as tvr

    job, _ = at_state(store, repo, "verifying")
    r = tvr.vrun(job.id, 0)
    raw, digest = tvr.envelope(r, tvr.body(("pass", "pass")))
    at = SR.timestamp(SR.epoch_of(VERIFIED_AT[:19] + "Z"))
    verify(store, repo, job, 0, raw=raw, ev=tvr.event(r, at=at, verifier_report_digest=digest))
    [rec] = store.run_records(job.id)
    assert rec.verified_at == utc_stamp(datetime.fromtimestamp(SR.epoch_of(at), timezone.utc))


def test_the_event_cursor_never_moves_backward_within_a_journal(store):
    """RULINGS_OBS 9: within one journal the cursor only moves forward (a repeat of its seq is a no-op); another
    journal's cursor is set only after drop_event_cursor, the reset's own path."""
    store.set_event_cursor("j-1", 5)
    store.set_event_cursor("j-1", 5)
    with pytest.raises(DispatchError):
        store.set_event_cursor("j-1", 3)
    with pytest.raises(DispatchError):
        store.set_event_cursor("j-2", 9)
    assert store.event_cursor() == ("j-1", 5)
    store.set_event_cursor("j-1", 8)
    assert store.event_cursor() == ("j-1", 8)
    store.drop_event_cursor()
    store.set_event_cursor("j-2", 1)
    assert store.event_cursor() == ("j-2", 1)
