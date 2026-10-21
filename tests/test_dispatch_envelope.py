"""F11-C, authority (C): a contract filed inside an envelope (cause ``envelope``, the launch's pin bound in the filing
transaction), its slot taken under the envelope's precondition, and its land through the envelope object
(:class:`levain.dispatch.adopt.Envelope`, faked here: the coordinator implements it), plus each refusal. A REAL tmp
job store and a REAL git repository; the residue run these cases come from is
project_memory/1020-f11/f11c_run/RUN.txt."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.dispatch.adopt import ArtifactRecord, Envelope, artifact_digest, floor, land, stage_commit
from levain.dispatch.jobs import EnvelopeRefused, JobStore, TransitionRefused
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
        self.calls = []

    def refusal(self, job, onto, changes, landing_tier):
        self.calls.append(("refusal", onto, changes.paths, landing_tier))
        if self.refuse_line:
            return self.refuse_line
        if landing_tier == "T3" and self.ceiling == "T2":
            return f"the envelope was signed with a {self.ceiling} key; this land is {landing_tier}"
        return None

    def evidence(self):
        return {"envelope_id": self.env_id, "envelope_digest": "sha256:" + "e" * 64, "thread_id": self.thread,
                "grant": "named"}

    def precondition(self, conn, now):
        self.calls.append(("precondition", conn.in_transaction, now))
        return None if self.live else f"envelope {self.env_id} is not live at {now}"

    def hook(self, conn, now, job):
        self.calls.append(("hook", conn.in_transaction, now, job.envelope_id, job.thread_id))
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


def file_env(store, repo, env, *, pin="", thread=None, idem_key=None, **over):
    return store.file_in_envelope(submission(repo[0], repo[1], **over), resolve_template=registry(), policy=POLICY,
                                  filer_session="coord-1", pin=env.env_id if pin == "" else pin,
                                  thread_id=thread or env.thread, precondition=env.hook, idem_key=idem_key)


def verified(store, repo, env, tmp_path, *, recommended=False):
    job, cap = file_env(store, repo, env)
    store.take_slot(job.id, precondition=env.hook)
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


# --- the happy path ------------------------------------------------------------------------------------------------

def test_the_fake_is_an_envelope():
    assert isinstance(FakeEnvelope("e", "/r"), Envelope)


def test_filing_in_an_envelope_binds_the_pin_and_thread_and_signs_in_one_transaction(store, repo, env):
    job, cap = file_env(store, repo, env)
    assert cap and job.state == "signed" and job.signer_fp is None
    assert [(h.to_state, h.cause) for h in job.history] == [("filed", "filed"), ("signed", "envelope")]
    assert json.loads(job.history[-1].evidence) == {"envelope_id": "env-1", "thread_id": "thread-1"}
    assert (job.envelope_id, job.thread_id) == ("env-1", "thread-1")
    assert rows(store, "job_envelopes") == rows(store, "job_threads") == 1
    # the hook ran inside the filing transaction and saw the job bound there, before it was signed
    assert env.calls == [("hook", True, utc_stamp(NOW), "env-1", "thread-1")]


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
def test_a_filing_with_no_pin_is_refused_and_nothing_is_written(store, repo, env, pin):
    with pytest.raises(EnvelopeRefused, match="no pin"):
        store.file_in_envelope(submission(*repo), resolve_template=registry(), policy=POLICY,
                               filer_session="coord-1", pin=pin, thread_id="thread-1", precondition=env.hook)
    assert rows(store, "jobs") == 0 and env.calls == []


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


def test_a_hook_that_raises_writes_nothing(store, repo, env):
    def boom(conn, now, job):
        raise RuntimeError("the coordinator store cannot be read")
    with pytest.raises(RuntimeError):
        store.file_in_envelope(submission(*repo), resolve_template=registry(), policy=POLICY,
                               filer_session="coord-1", pin="env-1", thread_id="thread-1", precondition=boom)
    assert rows(store, "jobs") == 0


def test_a_replay_returns_the_job_only_under_the_same_binding(store, repo, env):
    job, cap = file_env(store, repo, env, idem_key="k1")
    again, none = file_env(store, repo, env, idem_key="k1")
    assert again.id == job.id and none is None
    from levain.dispatch import ContractRefused
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

def test_an_envelope_job_runs_only_through_take_slot_with_its_precondition(store, repo, env):
    job, _ = file_env(store, repo, env)
    with pytest.raises(EnvelopeRefused, match="runs only with the envelope's precondition"):
        store.take_slot(job.id)
    with pytest.raises(TransitionRefused, match="only through take_slot"):
        store._transition(job.id, "running", "slot_taken")
    store._transition(job.id, "queued", "slot_wait")
    env.live = False
    with pytest.raises(EnvelopeRefused, match="not live"):
        store.take_slot(job.id, precondition=env.hook)
    assert store.get(job.id).state == "queued"
    env.live = True
    assert store.take_slot(job.id, precondition=env.hook).state == "running"


def test_take_slot_serves_an_unbound_job_too(store, repo):
    job, _ = store.file(submission(*repo), resolve_template=registry(), policy=POLICY, filer_session="s")
    store._transition(job.id, "signed", "signature")
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


@pytest.mark.parametrize("bad", [None, {"envelope_id": "env-1"}, {
    "envelope_id": "env-1", "envelope_digest": "d", "thread_id": "thread-1", "grant": "everything"}])
def test_malformed_evidence_is_refused_not_needs_sign(store, repo, env, tmp_path, bad):
    job = verified(store, repo, env, tmp_path)
    env.evidence = lambda: bad
    out = by_envelope(store, job, env)
    assert out.outcome == "refused" and ("evidence" in out.message or "grant" in out.message)


def test_an_object_that_is_not_an_envelope_is_refused(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, object())
    assert out.outcome == "refused" and "not an envelope object" in out.message


def test_a_floor_failure_stays_refused_under_c(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    out = by_envelope(store, job, env, onto="levain/x")
    assert out.outcome == "refused"
    git(repo[0], "update-ref", "refs/heads/land", git(repo[0], "rev-parse", "main~1"))
    out = by_envelope(store, job, env)
    assert out.outcome == "refused" and "MOVED BASE" in out.message and store.get(job.id).state == "verified"


def test_authorise_envelope_rule_needs_its_precondition_and_the_binding(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    intent = json.dumps({"envelope": env.evidence()})
    with pytest.raises(EnvelopeRefused, match="only with it"):
        store.authorise(job.id, cause="envelope_rule", evidence=intent)
    wrong = json.dumps({"envelope": dict(env.evidence(), envelope_id="env-9")})
    with pytest.raises(EnvelopeRefused, match="bound to at filing"):
        store.authorise(job.id, cause="envelope_rule", evidence=wrong, precondition=env.precondition)
    assert store.get(job.id).state == "verified"


# --- an ended envelope ---------------------------------------------------------------------------------------------

def test_an_ended_envelope_fails_its_jobs_not_yet_authorised(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    other_env = FakeEnvelope("env-2", str(repo[0]), paths=("src/sub/deep.py",))
    other, _ = file_env(store, repo, other_env, write=["src/sub/deep.py"])
    assert store.end_envelope("env-1", why="killed") == (job.id,)
    ended = store.get(job.id)
    assert (ended.state, ended.history[-1].cause) == ("failed", "envelope_ended")
    assert json.loads(ended.history[-1].evidence) == {"envelope_id": "env-1", "why": "killed"}
    assert store.get(other.id).state == "signed"
    assert by_envelope(store, ended, env).outcome == "refused"
    # its write files are released: the same contract files again
    assert file_env(store, repo, env)[0].state == "signed"


def test_an_ended_envelope_keeps_an_authorised_job(store, repo, env, tmp_path):
    job = verified(store, repo, env, tmp_path)
    wt = tmp_path / "wt"
    git(repo[0], "worktree", "add", "-q", str(wt), "land")
    assert by_envelope(store, job, env).outcome == "merge"
    assert store.end_envelope("env-1") == ()
    assert store.get(job.id).state == "authorised"


# --- no door -------------------------------------------------------------------------------------------------------

def test_no_cli_mcp_or_phone_door_files_or_lands_by_envelope():
    root = Path(__file__).resolve().parent.parent / "levain" / "dispatch"
    for name in ("cli.py", "mcp.py", "phone.py"):
        text = (root / name).read_text()
        for word in ("file_in_envelope", "envelope=", "end_envelope", "take_slot"):
            assert word not in text, (name, word)
