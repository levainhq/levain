"""F11-C, authority (C): a contract filed inside an envelope (cause ``envelope``, the binding read from the envelope
object inside the filing transaction), its slot taken under that object, and its land through it
(:class:`levain.dispatch.adopt.Envelope`, faked here: the coordinator implements it), plus each refusal. Every
envelope write takes ONE object: its evidence is the binding and its precondition the check (L3 r1 codex 3). A REAL
tmp job store and a REAL git repository; the residue runs these cases come from are
project_memory/1020-f11/f11c_run/RUN.txt and RUN_r2.txt."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.contracts.canon import canonical
from levain.dispatch import ContractRefused
from levain.dispatch.adopt import ArtifactRecord, Envelope, artifact_digest, floor, land, stage_commit
from levain.dispatch.jobs import EnvelopeEnd, EnvelopeRefused, JobStore, TransitionRefused
from tests.test_dispatch_contract import NOW, POLICY, make_repo, registry, submission

VERIFIED_AT = utc_stamp(NOW + timedelta(minutes=5))
PATHS = ("src/a.py", "src/new.py", "src/b.py")
T2 = lambda repo, paths: "T2"          # noqa: E731
T3 = lambda repo, paths: "T3"          # noqa: E731


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeEnvelope:
    """The coordinator's envelope object, faked: live until killed, ``where`` one repo and paths, one thread, a
    refusal line when set, a signer-class ceiling."""

    def __init__(self, env_id, repo, *, thread="thread-1", paths=PATHS):
        self.env_id, self.repo, self.thread, self.paths = env_id, repo, thread, paths
        self.live = True
        self.refuse_line = None
        self.ceiling = "T2"
        self.digest = "sha256:" + "e" * 64
        self.calls = []

    def refusal(self, job, onto, changes, landing_tier):
        self.calls.append(("refusal", onto, changes.paths, landing_tier))
        if self.refuse_line:
            return self.refuse_line
        if landing_tier == "T3" and self.ceiling == "T2":
            return f"the envelope was signed with a {self.ceiling} key; this land is {landing_tier}"
        return None

    def evidence(self):
        return {"envelope_id": self.env_id, "envelope_digest": self.digest, "thread_id": self.thread,
                "grant": "named"}

    def precondition(self, conn, now, job):
        self.calls.append(("precondition", conn.in_transaction, now, job.envelope_id, job.thread_id))
        if not self.live:
            return f"envelope {self.env_id} is not live at {now}"
        if job.repo != self.repo or not set(job.contract.write_paths) <= set(self.paths):
            return "the contract does not lie inside the envelope's where"
        if job.thread_id != self.thread:
            return "the filer's thread is not the envelope's"
        return None


def git(repo, *args, feed=None, **env):
    e = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "GIT_CONFIG_NOSYSTEM": "1",
         "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@x.invalid", "GIT_COMMITTER_NAME": "op",
         "GIT_COMMITTER_EMAIL": "op@x.invalid", "LC_ALL": "C", **env}
    return subprocess.run(["git", "-C", str(repo), *args], input=feed, capture_output=True, env=e, check=True,
                          timeout=60).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    """make_repo's repository (main checked out in its work tree) with ``land`` at base, checked out nowhere."""
    path, base = make_repo(tmp_path / "r")
    git(path, "branch", "land", base)
    return path, base


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return JobStore(d, clock=Clock(NOW))


@pytest.fixture
def env(repo):
    return FakeEnvelope("env-1", str(repo[0]))


def rows(store, table):
    store.list()
    conn = sqlite3.connect(store.path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def file_env(store, repo, env, *, idem_key=None, **over):
    return store.file_in_envelope(submission(repo[0], repo[1], **over), resolve_template=registry(), policy=POLICY,
                                  filer_session="coord-1", envelope=env, idem_key=idem_key)


def verified(store, repo, env, tmp_path, *, recommended=False):
    job, cap = file_env(store, repo, env)
    store.take_slot(job.id, envelope=env)
    store._transition(job.id, "verifying", "runs_exited")
    job = store._transition(job.id, "verified", "verifier_pass")
    put_records(store, repo, job, tmp_path)
    if recommended:
        store.recommend(job.id, cap)
    store._clock.now = NOW + timedelta(minutes=10)
    return store.get(job.id)


def put_records(store, repo, job, tmp_path):
    """Two passing runs over one staged tree (src/a.py changed), staged and written as the verifier writer would."""
    path, base = repo
    index = str(tmp_path / f"index-{os.urandom(4).hex()}")
    git(path, "read-tree", base, GIT_INDEX_FILE=index)
    sha = git(path, "hash-object", "-w", "--stdin", feed=b"a = 2\n")
    git(path, "update-index", "--add", "--cacheinfo", f"100644,{sha},src/a.py", GIT_INDEX_FILE=index)
    tree = git(path, "write-tree", GIT_INDEX_FILE=index)
    conn = sqlite3.connect(store.path)
    try:
        with conn:
            for run in (0, 1):
                staged = stage_commit(str(path), job_id=job.id, run=run, contract_digest=job.digest, base=base,
                                      tree=tree, verified_at=VERIFIED_AT)
                rec = ArtifactRecord(job_id=job.id, run=run, contract_digest=job.digest, base=base,
                                     staged_commit=staged, tree=tree,
                                     verifier={"id": "pytest-bb", "hash": "sha256:" + "b" * 64,
                                               "kind": "black-box", "result": "pass"},
                                     model={"requested": "glm-5", "answered": "glm-5",
                                            "backend_identity": f"backend-{run}"}, verified_at=VERIFIED_AT)
                conn.execute("INSERT INTO run_records (job_id, run, record, digest, envelope_digest, at) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (job.id, run, canonical(rec.to_mapping()),
                                                           artifact_digest(rec), "sha256:" + "f" * 64, VERIFIED_AT))
    finally:
        conn.close()


def by_envelope(store, job, envelope, *, onto="land", tier=T2, **kw):
    return land(store, job.id, landing_tier=tier, resolve_template=registry(), policy=POLICY, onto=onto,
                envelope=envelope, **kw)


def move(repo, branch="land"):
    git(repo[0], "update-ref", f"refs/heads/{branch}", git(repo[0], "rev-parse", "main~1"))


# --- the happy path ------------------------------------------------------------------------------------------------

def test_the_fake_is_an_envelope():
    assert isinstance(FakeEnvelope("e", "/r"), Envelope)


def test_filing_in_an_envelope_binds_its_evidence_and_signs_in_one_transaction(store, repo, env):
    job, cap = file_env(store, repo, env)
    assert cap and job.state == "signed" and job.signer_fp is None
    assert [(h.to_state, h.cause) for h in job.history] == [("filed", "filed"), ("signed", "envelope")]
    assert json.loads(job.history[-1].evidence) == {"envelope_id": "env-1", "thread_id": "thread-1"}
    assert (job.envelope_id, job.thread_id) == ("env-1", "thread-1")
    assert rows(store, "job_envelopes") == rows(store, "job_threads") == 1
    # the object's own precondition ran inside the filing transaction and saw the job bound there
    assert env.calls == [("precondition", True, utc_stamp(NOW), "env-1", "thread-1")]


@pytest.mark.parametrize("recommended", [False, True])
def test_a_verified_envelope_job_lands_through_c(store, repo, env, tmp_path, recommended):
    job = verified(store, repo, env, tmp_path, recommended=recommended)
    env.calls.clear()
    out = by_envelope(store, job, env)
    assert out.outcome == "adopted" and out.moved, out.message
    auth = [h for h in out.job.history if h.to_state == "authorised"]
    assert len(auth) == 1 and auth[0].cause == "envelope_rule"
    intent = json.loads(auth[0].evidence)
    assert intent["authority"] == "C" and intent["envelope"] == env.evidence() and intent["run"] == 0
    assert git(repo[0], "rev-parse", "land") == intent["staged"]
    # refusal at the floor with the change set and tier, then the precondition inside the authorise transaction
    assert [c[0] for c in env.calls] == ["refusal", "precondition"]
    assert env.calls[0][1:] == ("land", ("src/a.py",), "T2") and env.calls[1][1] is True


def test_an_authorised_envelope_job_finishes_from_its_intent_reading_nothing_mutable(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    wt = tmp_path / "wt"
    git(repo[0], "worktree", "add", "-q", str(wt), "land")
    out = by_envelope(store, job, env)
    assert out.outcome == "merge" and out.job.state == "authorised"
    git(repo[0], "worktree", "remove", "--force", str(wt))
    env.live = False                    # the envelope ends after the intent: it revokes nothing
    env.calls.clear()
    out = by_envelope(store, job, env)
    assert out.outcome == "adopted" and env.calls == []


# --- filing refusals -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pin", [None, ""])
def test_a_filing_whose_object_names_no_pin_is_refused_and_nothing_is_written(store, repo, env, pin):
    env.env_id = pin
    with pytest.raises(EnvelopeRefused, match="pinned envelope"):
        file_env(store, repo, env)
    assert rows(store, "jobs") == 0 and env.calls == []


@pytest.mark.parametrize("thing", [None, object(), "env-1"])
def test_a_filing_with_no_envelope_object_is_refused(store, repo, thing):
    with pytest.raises(EnvelopeRefused, match="envelope object"):
        file_env(store, repo, thing)
    assert rows(store, "jobs") == 0


def test_a_precondition_refusing_at_filing_writes_nothing(store, repo, env):
    env.live = False
    with pytest.raises(EnvelopeRefused, match="not live"):
        file_env(store, repo, env)
    for table in ("jobs", "transitions", "job_envelopes", "job_threads"):
        assert rows(store, table) == 0, table


def test_a_contract_outside_the_envelopes_where_is_refused(store, repo, repo_narrow=("src/a.py",)):
    env = FakeEnvelope("env-n", str(repo[0]), paths=repo_narrow)
    with pytest.raises(EnvelopeRefused, match="inside the envelope's where"):
        file_env(store, repo, env)
    assert rows(store, "jobs") == 0


def test_a_precondition_that_raises_writes_nothing(store, repo, env):
    def boom(conn, now, job):
        raise RuntimeError("the coordinator store cannot be read")
    env.precondition = boom
    with pytest.raises(RuntimeError):
        file_env(store, repo, env)
    assert rows(store, "jobs") == 0


def test_a_replay_returns_the_job_only_under_the_same_binding_and_asserts_no_liveness(store, repo, env):
    job, cap = file_env(store, repo, env, idem_key="k1")
    env.live = False
    env.calls.clear()
    again, none = file_env(store, repo, env, idem_key="k1")
    # a replay is not a liveness assertion: the dead envelope's job comes back, and no precondition ran
    assert again.id == job.id and none is None and env.calls == []
    other = FakeEnvelope("env-2", str(repo[0]))
    with pytest.raises(ContractRefused, match="another envelope binding"):
        file_env(store, repo, other, idem_key="k1")
    with pytest.raises(ContractRefused, match="another envelope binding"):
        store.file(submission(*repo), resolve_template=registry(), policy=POLICY, filer_session="coord-1",
                   idem_key="k1")


def test_the_binding_rows_are_append_only(store, repo, env):
    job, _ = file_env(store, repo, env)
    conn = sqlite3.connect(store.path)
    try:
        for table in ("job_envelopes", "job_threads"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"UPDATE {table} SET thread_id = 'x' WHERE job_id = ?", (job.id,))
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f"DELETE FROM {table} WHERE job_id = ?", (job.id,))
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            conn.execute("INSERT OR REPLACE INTO job_envelopes (job_id, envelope_id, thread_id, bound_at) "
                         "VALUES (?, 'env-9', 'thread-9', 'x')", (job.id,))
    finally:
        conn.close()


# --- the slot ------------------------------------------------------------------------------------------------------

def test_an_envelope_job_runs_only_through_take_slot_with_its_envelope(store, repo, env):
    job, _ = file_env(store, repo, env)
    with pytest.raises(EnvelopeRefused, match="runs only with that envelope"):
        store.take_slot(job.id)
    with pytest.raises(TransitionRefused, match="only through take_slot"):
        store._transition(job.id, "running", "slot_taken")
    store._transition(job.id, "queued", "slot_wait")
    env.live = False
    with pytest.raises(EnvelopeRefused, match="not live"):
        store.take_slot(job.id, envelope=env)
    assert store.get(job.id).state == "queued"
    env.live = True
    assert store.take_slot(job.id, envelope=env).state == "running"


def test_the_substitution_another_live_envelope_cannot_take_the_slot(store, repo, env):
    """L3 r1 codex 3: a job pinned to A, A dead, B live and covering the same repo and thread: B's object (or any
    hook) never starts A's job, because the binding is read from the object judged."""
    job, _ = file_env(store, repo, env)
    env.live = False
    b = FakeEnvelope("env-B", str(repo[0]))
    with pytest.raises(EnvelopeRefused, match=r"bound to at filing \(env-1, thread thread-1\)"):
        store.take_slot(job.id, envelope=b)
    assert b.calls == [] and store.get(job.id).state == "signed"


def test_take_slot_serves_an_unbound_job_and_refuses_an_envelope_for_it(store, repo, env):
    job, _ = store.file(submission(*repo), resolve_template=registry(), policy=POLICY, filer_session="s")
    store._transition(job.id, "signed", "signature")
    with pytest.raises(EnvelopeRefused, match="not filed inside an envelope"):
        store.take_slot(job.id, envelope=env)
    assert store.take_slot(job.id).state == "running"
    with pytest.raises(TransitionRefused, match="only a signed or queued job"):
        store.take_slot(job.id)


# --- land refusals -------------------------------------------------------------------------------------------------

def test_a_precondition_refusing_at_authorise_is_needs_sign_and_writes_nothing(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    before = (rows(store, "transitions"), git(repo[0], "rev-parse", "land"))
    env.live = False
    out = by_envelope(store, job, env)
    assert out.outcome == "needs_sign" and "not live" in out.message
    assert (rows(store, "transitions"), git(repo[0], "rev-parse", "land")) == before
    assert store.get(job.id).state == "verified"


def test_a_refusal_line_is_needs_sign_carrying_it(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    env.refuse_line = "criterion max_changed_lines: 1 changed lines is over 0"
    out = by_envelope(store, job, env)
    assert (out.outcome, out.message) == ("needs_sign", env.refuse_line)
    assert store.get(job.id).state == "verified"


def test_a_needs_sign_is_said_only_when_the_target_passes(store, repo, env, tmp_path):
    """L3 r1 complement 1: step 7's target checks run when step 6 finds (C) missing; a target they refuse is
    refused (a SIGN cannot fix it). A checked-out onto is not a step-7 refusal (a SIGN lands it as the operator's
    merge), so it stays needs_sign."""
    job = verified(store, repo, env, tmp_path)
    env.refuse_line = "criterion max_changed_lines: 1 changed lines is over 0"
    for onto in ("no-such-branch", "levain/x", "refs/heads/land"):
        out = by_envelope(store, job, env, onto=onto)
        assert out.outcome == "refused", (onto, out.message)
    git(repo[0], "worktree", "add", "-q", str(tmp_path / "wt"), "land")
    assert by_envelope(store, job, env).outcome == "needs_sign"
    unbound = by_envelope(store, job, FakeEnvelope("env-OTHER", str(repo[0])), onto="no-such-branch")
    assert unbound.outcome == "refused" and "no-such-branch" in unbound.message
    assert store.get(job.id).state == "verified"


def test_the_tier_check_is_the_objects(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, env, tier=T3)
    assert out.outcome == "needs_sign" and "this land is T3" in out.message
    env.ceiling = "T3"                  # a T3 envelope lands a T3 result: no T2-only rule for (C)
    assert by_envelope(store, job, env, tier=T3).outcome == "adopted"


def test_step_5_never_runs_for_c(store, repo, env, tmp_path, monkeypatch):
    from levain.dispatch import adopt
    job = verified(store, repo, env, tmp_path)
    monkeypatch.setattr(adopt, "_contrast_problem", lambda *a: pytest.fail("step 5 ran for (C)"))
    monkeypatch.setattr(adopt, "_entries_problem", lambda *a: pytest.fail("step 5 ran for (C)"))
    assert by_envelope(store, job, env).outcome == "adopted"


def test_an_envelope_that_is_not_the_jobs_binding_is_needs_sign(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    for other in (FakeEnvelope("env-OTHER", str(repo[0])), FakeEnvelope("env-1", str(repo[0]), thread="thread-2")):
        out = by_envelope(store, job, other)
        assert out.outcome == "needs_sign" and "bound to at filing (env-1, thread thread-1)" in out.message
        assert other.calls == []        # never asked: the binding decides first


def test_an_unbound_job_cannot_land_by_envelope(store, repo, env, tmp_path):
    job, _ = store.file(submission(*repo), resolve_template=registry(), policy=POLICY, filer_session="s")
    for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                      ("verified", "verifier_pass")):
        job = store._transition(job.id, to, cause)
    put_records(store, repo, job, tmp_path)
    out = by_envelope(store, job, env)
    assert out.outcome == "needs_sign" and "not filed inside an envelope" in out.message


def test_two_authorities_are_refused(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, env, rules=lambda: (0, ()))
    assert out.outcome == "refused" and out.message.startswith("one authority")
    verdict = floor(job, store.run_records(job.id), onto="land", landing_tier=T2, resolve_template=registry(),
                    policy=POLICY, now=store.stamp(), human={"onto": "land"}, envelope=env)
    assert verdict.failure.startswith("one authority") and not verdict.needs_sign


GOOD = {"envelope_id": "env-1", "envelope_digest": "sha256:" + "e" * 64, "thread_id": "thread-1", "grant": "named"}


@pytest.mark.parametrize("bad", [
    None, {"envelope_id": "env-1"}, dict(GOOD, grant="everything"), dict(GOOD, extra="x"),
    dict(GOOD, envelope_digest="x"), dict(GOOD, envelope_digest="sha256:" + "E" * 64),
    dict(GOOD, envelope_digest="sha256:" + "e" * 63), dict(GOOD, envelope_digest="sha1:" + "e" * 64),
    dict(GOOD, envelope_digest="sha256:" + "e" * 64 + "\n"), dict(GOOD, thread_id=7)])
def test_malformed_evidence_is_refused_not_needs_sign(store, repo, env, tmp_path, bad):
    """L3 r1 codex MED / complement 3: the exact key set and the digest's form (sha256: + 64 lowercase hex)."""
    job = verified(store, repo, env, tmp_path)
    env.evidence = lambda: bad
    out = by_envelope(store, job, env)
    assert out.outcome == "refused" and any(w in out.message for w in ("evidence", "grant", "digest")), out.message
    assert store.get(job.id).state == "verified" and git(repo[0], "rev-parse", "land") == repo[1]


def test_malformed_evidence_refuses_a_filing(store, repo, env):
    env.digest = "x"
    with pytest.raises(EnvelopeRefused, match="digest"):
        file_env(store, repo, env)
    assert rows(store, "jobs") == 0


def test_an_object_that_is_not_an_envelope_is_refused(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, object())
    assert out.outcome == "refused" and "not an envelope object" in out.message


def test_a_floor_failure_stays_refused_under_c(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, env, onto="levain/x")
    assert out.outcome == "refused" and store.get(job.id).state == "verified"


def test_a_moved_base_under_the_jobs_own_envelope_fails_the_job(store, repo, env, tmp_path):
    """Head, F11-C r1 H (C0 §5): MOVED BASE under (C) fails the job, cause base_moved, as the existing edge."""
    job = verified(store, repo, env, tmp_path)
    move(repo)
    out = by_envelope(store, job, env)
    assert out.outcome == "failed" and "MOVED BASE" in out.message
    failed = store.get(job.id)
    assert (failed.state, failed.history[-1].cause) == ("failed", "base_moved")


def test_a_moved_base_under_an_envelope_not_the_jobs_own_is_refused_and_the_job_kept(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    move(repo)
    out = by_envelope(store, job, FakeEnvelope("env-OTHER", str(repo[0])))
    assert out.outcome == "refused" and "MOVED BASE" in out.message and store.get(job.id).state == "verified"


@pytest.mark.parametrize("line", ["envelope env-1 is not live",
                                  "criterion max_changed_lines: 1 changed lines is over 0"])
def test_a_moved_base_under_the_jobs_own_envelope_that_refuses_still_fails_the_job(store, repo, env, tmp_path, line):
    """Head, F11-C r2 H: a dead (or refusing) envelope the job is bound to meets MOVED BASE: the job fails, cause
    base_moved, and its write files are released; it never stays held."""
    job = verified(store, repo, env, tmp_path)
    move(repo)
    env.live, env.refuse_line = False, line
    out = by_envelope(store, job, env)
    assert out.outcome == "failed" and "MOVED BASE" in out.message
    failed = store.get(job.id)
    assert (failed.state, failed.history[-1].cause) == ("failed", "base_moved")
    later = utc_stamp(store._clock.now + timedelta(hours=2))
    assert store.file(submission(*repo, expires_at=later), resolve_template=registry(), policy=POLICY,
                      filer_session="coord-2")[0].state == "filed"


def test_authorise_envelope_rule_takes_one_object_and_the_binding(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    intent = json.dumps({"authority": "C", "envelope": env.evidence()})
    with pytest.raises(EnvelopeRefused, match="only with it"):
        store.authorise(job.id, cause="envelope_rule", evidence=intent)
    with pytest.raises(EnvelopeRefused, match="only with it"):
        store.authorise(job.id, cause="human_adoption_rule", evidence=intent, envelope=env)
    # the substitution: A's intent, B's object (B live, A dead)
    env.live = False
    b = FakeEnvelope("env-9", str(repo[0]))
    with pytest.raises(EnvelopeRefused, match="bound to at filing"):
        store.authorise(job.id, cause="envelope_rule", evidence=intent, envelope=b)
    assert b.calls == []
    env.live = True
    # the right object, an intent that names another digest or no authority C
    for wrong in ({"authority": "C", "envelope": dict(env.evidence(), envelope_digest="sha256:" + "0" * 64)},
                  {"authority": "B", "envelope": env.evidence()}, {"envelope": env.evidence()}):
        with pytest.raises(EnvelopeRefused, match="intent"):
            store.authorise(job.id, cause="envelope_rule", evidence=json.dumps(wrong), envelope=env)
    assert store.get(job.id).state == "verified"
    assert store.authorise(job.id, cause="envelope_rule", evidence=intent, envelope=env).state == "authorised"


# --- an ended envelope ---------------------------------------------------------------------------------------------

def test_an_ended_envelope_fails_its_jobs_not_yet_running_or_authorised(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    other_env = FakeEnvelope("env-2", str(repo[0]), paths=("src/sub/deep.py",))
    other, _ = file_env(store, repo, other_env, write=["src/sub/deep.py"])
    assert store.end_envelope("env-1", why="killed") == EnvelopeEnd(failed=(job.id,), to_stop=())
    ended = store.get(job.id)
    assert (ended.state, ended.history[-1].cause) == ("failed", "envelope_ended")
    assert json.loads(ended.history[-1].evidence) == {"envelope_id": "env-1", "thread_id": None, "why": "killed"}
    assert store.get(other.id).state == "signed"
    assert by_envelope(store, ended, env).outcome == "refused"
    # its write files are released: the same contract files again
    assert file_env(store, repo, env)[0].state == "signed"


def test_an_ended_envelope_leaves_a_running_job_running_until_the_supervisor_stops_it(store, repo, env):
    """L3 r1 codex 1: a running job's process may still write, so end_envelope never fails it (its write files stay
    held); the supervisor stops it, then stop_recorded fails it."""
    job, _ = file_env(store, repo, env)
    store.take_slot(job.id, envelope=env)
    assert store.end_envelope("env-1") == EnvelopeEnd(failed=(), to_stop=(job.id,))
    assert store.get(job.id).state == "running"
    with pytest.raises(ContractRefused, match="already writes"):
        store.file(submission(*repo), resolve_template=registry(), policy=POLICY, filer_session="rival")
    stopped = store.stop_recorded(job.id, "envelope_ended", why="killed")
    assert (stopped.state, stopped.history[-1].cause) == ("failed", "envelope_ended")
    assert json.loads(stopped.history[-1].evidence) == {"envelope_id": "env-1", "stopped": True, "why": "killed"}
    with pytest.raises(TransitionRefused, match="only a running job"):
        store.stop_recorded(job.id, "envelope_ended")
    assert store.file(submission(*repo), resolve_template=registry(), policy=POLICY,
                      filer_session="rival")[0].state == "filed"


def test_stop_recorded_envelope_ended_refuses_an_unbound_or_not_running_job(store, repo, env):
    job, _ = file_env(store, repo, env)
    with pytest.raises(TransitionRefused, match="only a running job"):
        store.stop_recorded(job.id, "envelope_ended")
    plain, _ = store.file(submission(repo[0], repo[1], write=["src/sub/deep.py"]), resolve_template=registry(),
                          policy=POLICY, filer_session="s")
    store._transition(plain.id, "signed", "signature")
    store.take_slot(plain.id)
    with pytest.raises(TransitionRefused, match="not filed inside an envelope"):
        store.stop_recorded(plain.id, "envelope_ended")
    with pytest.raises(TransitionRefused, match="by its own envelope method"):
        store._transition(plain.id, "failed", "envelope_ended")


# --- one door out of running (F11-C r2 B) ---------------------------------------------------------------------------

def running_job(store, repo, env):
    job, cap = file_env(store, repo, env)
    store.take_slot(job.id, envelope=env)
    return job, cap


def test_end_envelope_on_an_expired_running_job_returns_it_to_stop_with_its_files_held(store, repo, env):
    """L3 r2 codex HIGH: the sweep inside end_envelope used to lapse the running job first, releasing its files
    while its process could still write; now no sweep touches a running job."""
    job, _ = running_job(store, repo, env)
    store._clock.now = parse_stamp(job.expires_at) + timedelta(minutes=1)
    assert store.end_envelope("env-1") == EnvelopeEnd(failed=(), to_stop=(job.id,))
    assert store.sweep() == []
    held = store.get(job.id)
    assert (held.state, held.history[-1].cause) == ("running", "slot_taken")
    assert store.overdue_running() == [job.id]
    later = utc_stamp(store._clock.now + timedelta(hours=2))
    with pytest.raises(ContractRefused, match="already writes"):
        store.file(submission(*repo, expires_at=later), resolve_template=registry(), policy=POLICY,
                   filer_session="rival")


def test_every_door_but_stop_recorded_refuses_to_end_a_running_job(store, repo, env):
    job, cap = running_job(store, repo, env)
    with pytest.raises(TransitionRefused, match="stop_recorded"):
        store.cancel_by_planner(job.id, cap)
    for to, cause in (("failed", "lease_lapsed"), ("failed", "budget_cap"), ("failed", "floor_refused"),
                      ("cancelled", "planner_cancel"), ("cancelled", "human_cancel")):
        with pytest.raises(TransitionRefused, match="stop_recorded"):
            store._transition(job.id, to, cause)
    with pytest.raises(TransitionRefused, match="by its own envelope method"):
        store._transition(job.id, "failed", "envelope_ended")
    assert not hasattr(store, "stopped_by_envelope")
    assert store.get(job.id).state == "running"
    # the lease has not lapsed, so the store refuses that cause; a cause outside the stop set refuses too
    with pytest.raises(TransitionRefused, match="has not lapsed"):
        store.stop_recorded(job.id, "lease_lapsed")
    with pytest.raises(TransitionRefused, match="not a cause that stops"):
        store.stop_recorded(job.id, "verifier_fail")
    out = store.stop_recorded(job.id, "budget_cap", why="killed at the cap")
    assert (out.state, out.history[-1].cause) == ("failed", "budget_cap")
    assert json.loads(out.history[-1].evidence) == {"stopped": True, "why": "killed at the cap"}


@pytest.mark.parametrize("cause", ["planner_cancel", "human_cancel"])
def test_stop_recorded_records_a_cancel_as_cancelled(store, repo, env, cause):
    job, _ = running_job(store, repo, env)
    out = store.stop_recorded(job.id, cause)
    assert (out.state, out.history[-1].cause) == ("cancelled", cause)


def test_stop_recorded_lease_lapsed_past_the_lease(store, repo, env):
    job, _ = running_job(store, repo, env)
    store._clock.now = parse_stamp(job.expires_at)
    out = store.stop_recorded(job.id, "lease_lapsed")
    assert (out.state, out.history[-1].cause) == ("failed", "lease_lapsed")
    assert store.overdue_running() == []


def test_runs_exited_still_leaves_running(store, repo, env):
    job, _ = running_job(store, repo, env)
    assert store._transition(job.id, "verifying", "runs_exited").state == "verifying"


# --- ids an end can name -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env_id,thread", [("a\nb", "thread-1"), ("env-1", "thr\u200bead")])
def test_evidence_with_unshowable_ids_refuses_a_filing_and_writes_nothing(store, repo, env_id, thread):
    """L3 r2 complement MED: filing accepts only the ids end_envelope can name."""
    env = FakeEnvelope(env_id, str(repo[0]), thread=thread)
    with pytest.raises(EnvelopeRefused, match="one line of plain text"):
        file_env(store, repo, env)
    assert rows(store, "jobs") == rows(store, "job_envelopes") == rows(store, "transitions") == 0
    assert env.calls == []


def test_end_envelope_with_a_thread_fails_only_that_threads_jobs(store, repo):
    """L3 r1 codex 2: E binds T1 and T2; ending E for T1 leaves T2's work."""
    t1 = FakeEnvelope("env-1", str(repo[0]), thread="thread-1")
    t2 = FakeEnvelope("env-1", str(repo[0]), thread="thread-2", paths=PATHS + ("src/sub/deep.py",))
    j1, _ = file_env(store, repo, t1)
    j2, _ = file_env(store, repo, t2, write=["src/sub/deep.py"])
    assert store.end_envelope("env-1", thread_id="thread-1") == EnvelopeEnd(failed=(j1.id,), to_stop=())
    assert (store.get(j1.id).state, store.get(j2.id).state) == ("failed", "signed")
    assert json.loads(store.get(j1.id).history[-1].evidence)["thread_id"] == "thread-1"
    assert store.end_envelope("env-1", thread_id="thread-9") == EnvelopeEnd(failed=(), to_stop=())
    assert store.end_envelope("env-1") == EnvelopeEnd(failed=(j2.id,), to_stop=())


def test_an_ended_envelope_keeps_an_authorised_job(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    wt = tmp_path / "wt"
    git(repo[0], "worktree", "add", "-q", str(wt), "land")
    assert by_envelope(store, job, env).outcome == "merge"
    assert store.end_envelope("env-1") == EnvelopeEnd(failed=(), to_stop=())
    assert store.get(job.id).state == "authorised"


# --- no door -------------------------------------------------------------------------------------------------------

def test_no_cli_mcp_or_phone_door_files_or_lands_by_envelope():
    root = Path(__file__).resolve().parent.parent / "levain" / "dispatch"
    for name in ("cli.py", "mcp.py", "phone.py"):
        text = (root / name).read_text()
        for word in ("file_in_envelope", "envelope=", "end_envelope", "take_slot", "stop_recorded",
                     "overdue_running"):
            assert word not in text, (name, word)
