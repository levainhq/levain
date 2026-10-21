"""F11 adoption: the floor, (A) the human authorise item, (B) a standing rule, and LAND, against fixture artifact
records on REAL git repositories in tmp (real keys in a real authority log, a real control store, a real job store).
Every git fact is read back from the repository, never assumed."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import adopt
from levain.dispatch import control as ctl
from levain.dispatch.adopt import (
    DISPATCH_AUTHORISE_NAMESPACE, ArtifactRecord, artifact_digest, authorise_message, authorise_payload, land,
    stage_commit, staged_ref,
)
from levain.dispatch.jobs import JobStore
from levain.dispatch.registry import active_adoption_rules, adoption_rules_at, active_policy, resolver
from tests import test_dispatch_control as tc
from tests.test_dispatch_contract import NOW
from tests.test_dispatch_control import BB, Clock, accept, base_config

keys, log, cdir = tc.keys, tc.log, tc.cdir

VERIFIED_AT = utc_stamp(NOW + timedelta(minutes=5))
WRITES = ["src/a.py", "src/b.py", "src/new.py"]
T2 = lambda repo, paths: "T2"          # noqa: E731  (the injected landing tier)
T3 = lambda repo, paths: "T3"          # noqa: E731


def env(**extra: str) -> dict[str, str]:
    e = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "GIT_CONFIG_NOSYSTEM": "1",
         "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@x.invalid", "GIT_COMMITTER_NAME": "op",
         "GIT_COMMITTER_EMAIL": "op@x.invalid", "LC_ALL": "C"}
    e.update(extra)
    return e


def git(repo: Path, *args: str, feed: bytes | None = None, **extra: str) -> str:
    cp = subprocess.run(["git", "-C", str(repo), *args], input=feed, capture_output=True, env=env(**extra),
                        timeout=60, check=True)
    return cp.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path) -> tuple[Path, str]:
    """main at base (src/a.py, src/b.py, src/run.py executable); the work tree has ``work`` checked out, so main is
    NOT checked out anywhere unless a test checks it out."""
    r = Path(os.path.realpath(tmp_path)) / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "commit.gpgsign", "false")
    (r / "src").mkdir()
    (r / "src/a.py").write_text("a = 1\n")
    (r / "src/b.py").write_text("b = 2\nc = 3\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    git(r, "checkout", "-q", "-b", "work")
    return r, git(r, "rev-parse", "main")


@pytest.fixture
def cstore(cdir, log, keys):
    s = ctl.ControlStore(cdir, authority=log.source, clock=Clock(NOW))
    base_config(s, log, keys)
    return s


@pytest.fixture
def jstore(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return JobStore(d, clock=Clock(NOW))


def rule_body(repo, criteria=None, **over):
    from tests.test_dispatch_control import tmpl
    r = {"repo": str(repo), "template_id": "fix-lint", "template_hash": tmpl()["content_hash"],
         "verifier_kind": "black-box",
         "criteria": criteria or [{"onto": ["main"]}, {"max_changed_lines": 40}]}
    r.update(over)
    return r


def verified_job(jstore, cstore, repo):
    path, base = repo
    sub = {"template": "fix-lint", "goal": "Fix a.", "read": ["src/a.py", "src/b.py"], "write": WRITES,
           "base": base, "budget": {"tool_calls": 40, "tokens": 200000, "dollars_cents": 150},
           "grant": ["Edit", "Write"], "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(path)}
    job, _cap = jstore.file(sub, resolve_template=resolver(cstore), policy=active_policy(cstore),
                            filer_session="sess-1")
    for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                      ("verified", "verifier_pass")):
        job = jstore._transition(job.id, to, cause)
    jstore._clock.now = NOW + timedelta(minutes=10)         # verified at +5, signed at +6, landed at +10
    return job


def make_tree(repo: Path, base: str, change: dict, tmp: Path) -> str:
    """base's tree with ``change`` applied through a scratch index: path -> bytes (a 100644 blob), (mode, bytes),
    ("gitlink", sha), or None (deleted)."""
    index = str(tmp / f"index-{os.urandom(4).hex()}")
    git(repo, "read-tree", base, GIT_INDEX_FILE=index)
    for p, v in change.items():
        if v is None:
            git(repo, "update-index", "--force-remove", "--", p, GIT_INDEX_FILE=index)
            continue
        if isinstance(v, tuple) and v[0] == "gitlink":
            mode, sha = "160000", v[1]
        else:
            mode, data = ("100644", v) if isinstance(v, bytes) else v
            sha = git(repo, "hash-object", "-w", "--stdin", feed=data)
        git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{sha},{p}", GIT_INDEX_FILE=index)
    return git(repo, "write-tree", GIT_INDEX_FILE=index)


def record(repo, job, run, tree, *, backend=None, kind="black-box", result="pass") -> ArtifactRecord:
    path, base = repo
    staged = stage_commit(str(path), job_id=job.id, run=run, contract_digest=job.digest, base=base, tree=tree,
                          verified_at=VERIFIED_AT)
    return ArtifactRecord(job_id=job.id, run=run, contract_digest=job.digest, base=base, staged_commit=staged,
                          tree=tree, verifier={"id": "pytest-bb", "hash": BB, "kind": kind, "result": result},
                          model={"requested": "glm-5", "answered": "glm-5",
                                 "backend_identity": backend or f"backend-{run}"},
                          verified_at=VERIFIED_AT)


def runs(repo, job, tmp, change, k=2):
    tree = make_tree(repo[0], repo[1], change, tmp)
    return [record(repo, job, i, tree) for i in range(k)]


def human(job, rec, keys, log, *, onto="main", key="laptop"):
    payload = authorise_payload(job, rec, onto=onto, signer_fp=keys[key].fp, signed_at=NOW + timedelta(minutes=6))
    return payload, keys[key].sign(authorise_message(payload), DISPATCH_AUTHORISE_NAMESPACE), log.source


def put(jstore, recs) -> None:
    """``recs`` into the job store's ``run_records``, the one source a land reads, as the verifier writer would write
    them (its canonical bytes and artifact digest; the envelope digest a placeholder). A run already stored with the
    same bytes is left; another record for a stored run fails the test, as the store's append-only trigger would."""
    import sqlite3

    from levain.contracts.canon import canonical

    conn = sqlite3.connect(jstore.directory / "dispatch.db")
    try:
        with conn:
            for r in recs:
                text = canonical(r.to_mapping())
                row = conn.execute("SELECT record FROM run_records WHERE job_id = ? AND run = ?",
                                   (r.job_id, r.run)).fetchone()
                if row is not None:
                    assert row[0] == text, f"run {r.run} is already stored with another record"
                    continue
                conn.execute("INSERT INTO run_records (job_id, run, record, digest, envelope_digest, at) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (r.job_id, r.run, text, artifact_digest(r), "sha256:" + "e" * 64, VERIFIED_AT))
    finally:
        conn.close()


def by_rule(jstore, cstore, job, recs, *, onto="main", tier=T2, **kw):
    put(jstore, recs)
    return land(jstore, job.id, landing_tier=tier, resolve_template=resolver(cstore),
                policy=active_policy(cstore), onto=onto, rules=lambda: adoption_rules_at(cstore), **kw)


def by_human(jstore, cstore, job, recs, h, *, tier=T2, **kw):
    put(jstore, recs)
    return land(jstore, job.id, landing_tier=tier, resolve_template=resolver(cstore),
                policy=active_policy(cstore), human=h, **kw)


def with_rule(cstore, log, keys, repo, **kw):
    accept(cstore, log, keys, "adoption_rules", 4, [rule_body(repo[0], **kw)])


A_FIX = {"src/a.py": b"a = 2\n"}


# --- the happy paths -----------------------------------------------------------------------------------------------

def test_a_rule_lands_by_compare_and_swap(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "adopted" and out.moved, out.message
    assert git(repo[0], "rev-parse", "main") == recs[0].staged_commit
    assert git(repo[0], "rev-parse", "work") == repo[1]                     # nothing else moved
    states = [(h.to_state, h.cause) for h in out.job.history[-2:]]
    assert states == [("authorised", "human_adoption_rule"), ("adopted", "adopted")]


def test_a_human_lands_one_run(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[1], keys, log))
    assert out.outcome == "adopted", out.message
    assert git(repo[0], "rev-parse", "main") == recs[1].staged_commit
    assert out.job.history[-2].cause == "human_authorises"


def test_the_staged_commit_is_deterministic(repo, jstore, cstore, tmp_path):
    job = verified_job(jstore, cstore, repo)
    tree = make_tree(repo[0], repo[1], A_FIX, tmp_path)
    a = stage_commit(str(repo[0]), job_id=job.id, run=0, contract_digest=job.digest, base=repo[1], tree=tree,
                     verified_at=VERIFIED_AT)
    git(repo[0], "update-ref", "-d", staged_ref(job.id, 0))
    b = stage_commit(str(repo[0]), job_id=job.id, run=0, contract_digest=job.digest, base=repo[1], tree=tree,
                     verified_at=VERIFIED_AT)
    shown = git(repo[0], "log", "-1", "--format=%an|%cn|%P", a)
    assert a == b and shown == f"levain dispatch|levain dispatch|{repo[1]}"


def test_staging_again_returns_the_held_commit_and_never_moves_a_ref(repo, jstore, cstore, tmp_path):
    from levain.dispatch import DispatchError

    job = verified_job(jstore, cstore, repo)
    tree = make_tree(repo[0], repo[1], A_FIX, tmp_path)
    kw = dict(job_id=job.id, run=0, contract_digest=job.digest, base=repo[1], tree=tree)
    a = stage_commit(str(repo[0]), verified_at=VERIFIED_AT, **kw)
    assert stage_commit(str(repo[0]), verified_at=VERIFIED_AT, **kw) == a
    later = utc_stamp(NOW + timedelta(minutes=7))
    with pytest.raises(DispatchError, match="never moved"):
        stage_commit(str(repo[0]), verified_at=later, **kw)
    with pytest.raises(DispatchError, match="dated by the event"):
        stage_commit(str(repo[0]), verified_at=later, **kw)
    assert git(repo[0], "rev-parse", staged_ref(job.id, 0)) == a


def test_a_symbolic_staged_ref_is_refused_never_followed(repo, jstore, cstore, tmp_path):
    from levain.dispatch import DispatchError

    job = verified_job(jstore, cstore, repo)
    tree = make_tree(repo[0], repo[1], A_FIX, tmp_path)
    kw = dict(job_id=job.id, run=0, contract_digest=job.digest, base=repo[1], tree=tree, verified_at=VERIFIED_AT)
    sha = stage_commit(str(repo[0]), **kw)
    git(repo[0], "update-ref", "-d", staged_ref(job.id, 0))
    # a branch at exactly the staged sha, and the staged name a symref to it: following it would accept a ref that
    # moves when the branch does
    git(repo[0], "update-ref", "refs/heads/elsewhere", sha)
    git(repo[0], "symbolic-ref", staged_ref(job.id, 0), "refs/heads/elsewhere")
    with pytest.raises(DispatchError, match="symbolic ref"):
        stage_commit(str(repo[0]), **kw)


def test_a_create_that_fails_with_no_ref_after_is_a_git_fault_carrying_gits_words(repo, jstore, cstore, tmp_path):
    from levain.dispatch.adopt import GitFault

    job = verified_job(jstore, cstore, repo)
    tree = make_tree(repo[0], repo[1], A_FIX, tmp_path)
    # a ref beneath the staged name: the staged ref cannot be created and does not exist
    git(repo[0], "update-ref", staged_ref(job.id, 0) + "/x", repo[1])
    with pytest.raises(GitFault) as exc:
        stage_commit(str(repo[0]), job_id=job.id, run=0, contract_digest=job.digest, base=repo[1], tree=tree,
                     verified_at=VERIFIED_AT)
    assert "None" not in str(exc.value) and staged_ref(job.id, 0) in str(exc.value)


# --- (a) crash after the swap, before adopted ----------------------------------------------------------------------

def test_a_crash_after_the_swap_recovers_to_adopted_moving_nothing(jstore, cstore, repo, log, keys, tmp_path,
                                                                   monkeypatch):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)

    def crash():
        raise RuntimeError("power cut")
    monkeypatch.setattr(adopt, "_after_cas", crash)
    with pytest.raises(RuntimeError, match="power cut"):
        by_rule(jstore, cstore, job, recs)
    assert jstore.get(job.id).state == "authorised"
    assert git(repo[0], "rev-parse", "main") == recs[0].staged_commit
    monkeypatch.undo()
    # the retry reads nothing mutable: rules that now raise and a tier that raises change nothing
    out = land(jstore, job.id, landing_tier=lambda r, p: 1 / 0, resolve_template=lambda t: 1 / 0,
               policy=active_policy(cstore), rules=lambda: 1 / 0)
    assert out.outcome == "adopted" and not out.moved
    assert git(repo[0], "rev-parse", "main") == recs[0].staged_commit


# --- (b) the checked-out branch: his merge, his commits on top -----------------------------------------------------

def test_a_checked_out_branch_prints_his_merge_and_a_retry_adopts(jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    git(path, "checkout", "-q", "main")
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "merge" and out.job.state == "authorised"
    assert out.command == f"git -C {path} merge --ff-only {recs[0].staged_commit}"
    assert git(path, "rev-parse", "main") == base                           # Levain moved nothing
    git(path, "merge", "-q", "--ff-only", recs[0].staged_commit)            # his act
    (path / "src/b.py").write_text("b = 9\n")
    git(path, "commit", "-q", "-am", "his own commit on top")
    again = by_human(jstore, cstore, job, recs, None)
    assert again.outcome == "adopted" and not again.moved
    assert again.job.history[-1].to_state == "adopted"


# --- (c) a moved base ----------------------------------------------------------------------------------------------

def test_a_moved_base_fails_the_job_only_under_a_verified_authorisation(jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    git(path, "update-ref", "refs/heads/main", git(path, "commit-tree", f"{base}^{{tree}}", "-p", base,
                                                   feed=b"his\n"))
    staged = git(path, "for-each-ref", f"refs/heads/levain/dispatch/{job.id}/")
    assert staged
    # a rule land, and an item whose signature is garbage (L1 1020+17, run), refuse and keep the job and its refs
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and "MOVED BASE" in out.message and out.job.state == "verified"
    payload, _sig, source = human(job, recs[0], keys, log)
    out = by_human(jstore, cstore, job, recs, (payload, "garbage-signature", source))
    assert out.outcome == "refused" and jstore.get(job.id).state == "verified"
    assert git(path, "for-each-ref", f"refs/heads/levain/dispatch/{job.id}/") == staged
    # the human's verified authorisation of that run on that branch fails it and releases its files
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "failed" and out.job.state == "failed" and out.job.history[-1].cause == "base_moved"
    assert out.job.history[-2].to_state == "verified"                              # no intent was written
    assert git(path, "for-each-ref", f"refs/heads/levain/dispatch/{job.id}/") == ""    # staged refs deleted
    again = verified_job(jstore, cstore, repo)                                       # the same files re-file
    assert again.state == "verified"


def test_a_base_moved_after_authorisation_fails_from_authorised(jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    git(path, "checkout", "-q", "main")
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    assert by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log)).outcome == "merge"
    (path / "src/b.py").write_text("b = 7\n")
    git(path, "commit", "-q", "-am", "he committed instead of merging")
    out = by_human(jstore, cstore, job, recs, None)
    assert out.outcome == "failed" and [h.to_state for h in out.job.history[-2:]] == ["authorised", "failed"]


def test_a_verified_job_whose_branch_already_holds_its_staged_commit_is_refused(jstore, cstore, repo, log, keys,
                                                                               tmp_path):
    path, _base = repo
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    git(path, "update-ref", "refs/heads/main", recs[0].staged_commit)       # a merge Levain did not make
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and "a merge Levain did not make" in out.message
    assert jstore.get(job.id).state == "verified"


# --- (d) no hook, fsmonitor, textconv or external diff of the repository runs -------------------------------------

def test_no_repository_hook_filter_or_driver_runs(jstore, cstore, repo, log, keys, tmp_path):
    path, _base = repo
    marker = tmp_path / "marker"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {marker}\ncat \"$1\" 2>/dev/null\nexit 0\n")
    os.chmod(script, 0o755)
    hooks = path / ".git/hooks"
    for hook in ("post-merge", "reference-transaction", "pre-commit", "post-checkout"):
        (hooks / hook).write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
        os.chmod(hooks / hook, 0o755)
    # the repository names its own hooks directory (a user's global core.hooksPath would otherwise win)
    git(path, "config", "core.hooksPath", str(hooks))
    git(path, "config", "diff.evil.textconv", str(script))
    git(path, "config", "diff.external", str(script))
    git(path, "config", "core.fsmonitor", str(script))
    (path / ".gitattributes").write_text("*.py diff=evil\n")
    # positive control: this repository's hooks and drivers DO run under plain git
    git(path, "update-ref", "refs/heads/probe", "main")
    git(path, "diff", "main", "work", "--", "src/a.py")
    assert marker.exists()
    marker.unlink()
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    if marker.exists():
        marker.unlink()                         # staging the fixture used plain git; only land is under test
    assert adopt.change_set(str(path), recs[0].base, recs[0].staged_commit).entries[0].added == 1
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "adopted" and out.moved, out.message
    assert not marker.exists(), marker.read_text()


# --- (e) binary, mode change, deletion, gitlink: a rule refuses, a human may sign ----------------------------------

ODD = {
    "binary": ({"src/a.py": b"a\x00b\n"}, "is binary"),
    "mode": ({"src/a.py": ("100755", b"a = 1\n")}, "changes type or mode"),
    "deletion": ({"src/b.py": None}, "is deleted"),
    "gitlink": ({"src/new.py": ("gitlink", None)}, "gitlink"),
}


@pytest.mark.parametrize("name", sorted(ODD))
def test_odd_entries_refuse_a_rule_and_a_human_may_land_them(name, jstore, cstore, repo, log, keys, tmp_path):
    change, why = ODD[name]
    if name == "gitlink":
        change = {"src/new.py": ("gitlink", repo[1])}
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, change)
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and why in out.message, out.message
    assert jstore.get(job.id).state == "verified"
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "adopted", out.message
    assert git(repo[0], "rev-parse", "main") == recs[0].staged_commit


# --- (f) the caps, each refusing before content is read ------------------------------------------------------------

@pytest.mark.parametrize("cap, value, change, why", [
    ("MAX_ENTRIES", 1, {"src/a.py": b"a = 2\n", "src/b.py": b"b = 3\n"}, "more than 1 entries"),
    ("MAX_BLOB_BYTES", 8, {"src/a.py": b"a = 2222222\n"}, "over the 8-byte cap"),
    ("MAX_TOTAL_BYTES", 10, {"src/a.py": b"a = 2\n"}, "over the 10-byte cap"),
])
def test_each_cap_refuses_and_reads_no_content(cap, value, change, why, jstore, cstore, repo, log, keys, tmp_path,
                                               monkeypatch):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, change)
    seen: list[tuple[str, ...]] = []
    real = adopt._git

    def spy(repo_, *args, **kw):
        seen.append(args)
        return real(repo_, *args, **kw)
    monkeypatch.setattr(adopt, "_git", spy)
    monkeypatch.setattr(adopt, cap, value)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "refused" and why in out.message, out.message
    assert not any("--batch" in a for a in seen)                               # no blob content was read
    assert jstore.get(job.id).state == "verified"


# --- (g) the target ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("onto, why", [
    ("refs/heads/main", "never a full ref"), ("levain/x", "under levain/"), ("Levain/x", "under levain/"),
    ("@{-1}", "not a branch name as written"), ("alias", "symbolic ref"), ("-main", "never a full ref"),
])
def test_bad_targets_are_refused(onto, why, jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    git(path, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    for name in ("levain/x", "Levain/y"):
        git(path, "update-ref", f"refs/heads/{name}", base)
    assert adopt.branch_problem(str(path), onto) is not None
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log, onto=onto))
    assert out.outcome == "refused" and why in out.message, out.message
    assert git(path, "rev-parse", "main") == base


def test_onto_must_be_the_branch_the_authorisation_names(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log), onto="work")
    assert out.outcome == "refused" and "not the branch the authorisation names" in out.message


# --- (h) rules: text criteria never accepted; another repo's rule never lands -------------------------------------

def test_a_text_criteria_rule_is_refused_at_acceptance(cstore, repo, log, keys):
    with pytest.raises(ctl.ConfigRefused, match="free text is checked by nothing"):
        with_rule(cstore, log, keys, repo, criteria=["contrast-clean", "diff under 40 lines"])


def test_a_rule_for_another_repo_does_not_land(jstore, cstore, repo, log, keys, tmp_path):
    accept(cstore, log, keys, "adoption_rules", 4, [rule_body("/elsewhere/repo")])
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and "no adoption rule" in out.message
    assert git(repo[0], "rev-parse", "main") == repo[1]


def test_a_failing_criterion_is_the_line_shown(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo, criteria=[{"onto": ["main"]}, {"max_changed_lines": 1}])
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, {"src/a.py": b"a = 2\nz = 1\n"})
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and out.message == "criterion max_changed_lines: 3 changed lines is over 1"


# --- (i) the landing tier ------------------------------------------------------------------------------------------

def test_t3_refuses_a_rule_and_needs_a_key_covering_t3(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_rule(jstore, cstore, job, recs, tier=T3)
    assert out.outcome == "refused" and "lands at T3" in out.message
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log, key="phone"), tier=T3)
    assert out.outcome == "refused" and "does not cover T3" in out.message
    assert jstore.get(job.id).state == "verified"
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log), tier=T3)
    assert out.outcome == "adopted", out.message


@pytest.mark.parametrize("tier", [lambda r, p: "T1", lambda r, p: None, lambda r, p: 1 / 0])
def test_a_landing_tier_that_is_not_t2_or_t3_fails_closed(tier, jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log), tier=tier)
    assert out.outcome == "refused" and "landing tier" in out.message


def test_the_landing_tier_has_no_default():
    with pytest.raises(TypeError):
        land(None, "dj-x", resolve_template=None, policy=None)            # type: ignore[call-arg, arg-type]


# --- (j) the contrast ----------------------------------------------------------------------------------------------

def test_unequal_staged_trees_refuse_a_rule_and_a_human_picks_one(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    r0 = record(repo, job, 0, make_tree(repo[0], repo[1], A_FIX, tmp_path))
    r1 = record(repo, job, 1, make_tree(repo[0], repo[1], {"src/a.py": b"a = 3\n"}, tmp_path))
    out = by_rule(jstore, cstore, job, [r0, r1])
    assert out.outcome == "refused" and "staged trees differ" in out.message
    out = by_human(jstore, cstore, job, [r0, r1], human(job, r1, keys, log))
    assert out.outcome == "adopted" and git(repo[0], "rev-parse", "main") == r1.staged_commit


@pytest.mark.parametrize("backends, kinds, why", [
    (("b", "b"), ("black-box", "black-box"), "same backend identity"),
    ((None, None), ("black-box", "in-process"), "another verifier than the signed template"),
])
def test_a_rule_needs_distinct_backends_and_black_box_passes(backends, kinds, why, jstore, cstore, repo, log, keys,
                                                             tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    tree = make_tree(repo[0], repo[1], A_FIX, tmp_path)
    recs = [record(repo, job, i, tree, backend=backends[i], kind=kinds[i]) for i in (0, 1)]
    assert why in by_rule(jstore, cstore, job, recs).message


# --- revalidation and the artifact ---------------------------------------------------------------------------------

def test_a_changed_template_or_policy_refuses(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    h = human(job, recs[0], keys, log)
    put(jstore, recs)
    out = land(jstore, job.id, landing_tier=T2, resolve_template=lambda t: None, policy=active_policy(cstore),
               human=h)
    assert out.outcome == "refused" and "no longer registered" in out.message
    from levain.dispatch.policy import ProviderPolicy
    other = ProviderPolicy.from_mapping({"never": [], "providers": {"openrouter": {"read": ["**"]}}})
    out = land(jstore, job.id, landing_tier=T2, resolve_template=resolver(cstore), policy=other, human=h)
    assert out.outcome == "refused" and "provider policy" in out.message


def test_a_tampered_record_is_refused(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    h = human(job, recs[0], keys, log)
    forged = ArtifactRecord(**{**recs[0].__dict__, "verified_at": utc_stamp(NOW)})
    out = by_human(jstore, cstore, job, [forged, recs[1]], h)
    assert out.outcome == "refused" and "artifact digest" in out.message
    assert artifact_digest(forged) != artifact_digest(recs[0])


def test_a_write_outside_the_contract_is_refused(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, {"src/sub/deep.py": b"x = 1\n"})
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "refused" and "not a path the contract's write list names exactly" in out.message


def test_a_graft_file_in_the_repository_is_not_read(jstore, cstore, repo, log, keys, tmp_path):
    # .git/info/grafts rewrites a commit's parents for any git that reads it: the floor's parent check must see the
    # commit as it is (contract.py's GIT_GRAFT_FILE guard, carried to the adopt runner)
    path, base = repo
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    other = git(path, "commit-tree", f"{base}^{{tree}}", feed=b"orphan\n")
    (path / ".git/info").mkdir(exist_ok=True)
    (path / ".git/info/grafts").write_text(f"{recs[0].staged_commit} {other}\n")
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "adopted", out.message


# --- L2 1020+17 (each from a run) ----------------------------------------------------------------------------------

def test_a_case_variant_of_a_branch_is_refused_and_moves_nothing(jstore, cstore, repo, log, keys, tmp_path):
    # on a case-insensitive disk refs/heads/Main is main's loose file: a land onto "Main" moved a checked-out main
    path, base = repo
    git(path, "checkout", "-q", "main")
    assert "differs only in case" in adopt.branch_problem(str(path), "Main")
    assert "not a branch here, as written" in adopt.branch_problem(str(path), "nope")
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log, onto="Main"))
    assert out.outcome == "refused" and out.job.state == "verified"
    assert git(path, "rev-parse", "main") == base


def test_a_branch_differing_only_in_case_from_another_is_refused(repo, monkeypatch):
    path, _base = repo
    real = adopt._git_ok

    def two_names(r, *args, **kw):
        if args[:1] == ("for-each-ref",):
            return b"refs/heads/main\nrefs/heads/MAIN\nrefs/heads/work\n"     # packed refs can hold both
        return real(r, *args, **kw)

    monkeypatch.setattr(adopt, "_git_ok", two_names)
    assert "differs only in case" in adopt.branch_problem(str(path), "main")


def test_parents_are_read_with_the_commit_graph_off(repo, monkeypatch):
    # a forged commit-graph reported base as a staged commit's parent while the object held another (L2, run)
    path, base = repo
    seen = []
    real = subprocess.run

    def spy(argv, **kw):
        seen.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(adopt.subprocess, "run", spy)
    adopt._commit_shape(str(path), base)
    assert seen and all("core.commitGraph=false" in a for a in seen)


def test_the_printed_merge_quotes_the_worktree_path(jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    wt = tmp_path / "his tree; touch PWNED"
    git(path, "worktree", "add", "-q", str(wt), "main")
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "merge"
    assert out.command == f"git -C '{os.path.realpath(wt)}' merge --ff-only {recs[0].staged_commit}"


def test_a_worktree_path_with_a_control_character_prints_nothing(jstore, cstore, repo, log, keys, tmp_path):
    path, base = repo
    wt = tmp_path / "wt"
    git(path, "worktree", "add", "-q", str(wt), "main")
    (gitdir,) = (path / ".git" / "worktrees").iterdir()
    (gitdir / "gitdir").write_text(f"{tmp_path}/x\n; touch PWNED #/.git\n")
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    out = by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))
    assert out.outcome == "refused" and "control characters" in out.message and not out.command
    assert git(path, "rev-parse", "main") == base


# --- L1 1020+17 ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("winner", ["adopted", "failed"])
def test_a_land_that_loses_the_authorise_race_reports_the_winners_outcome(winner, jstore, cstore, repo, log, keys,
                                                                          tmp_path, monkeypatch):
    path, base = repo
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    real = JobStore.authorise
    raced = []

    def racing(self, job_id, **kw):
        if not raced:                       # the other land wins inside this land's window
            raced.append(1)
            real(self, job_id, cause="human_adoption_rule", evidence=__import__("json").dumps(
                {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": base,
                 "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2",
                 "rule": {"generation": 4, "index": 0}}))
            if winner == "adopted":
                self._transition(job_id, "adopted", "adopted", evidence="{}")
            else:
                self._transition(job_id, "failed", "base_moved", evidence="{}")
        return real(self, job_id, **kw)

    monkeypatch.setattr(JobStore, "authorise", racing)
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == winner and out.job.state == winner


def test_line_counts_stay_linear_past_the_budget_and_bound_from_above():
    import time

    old, new = b"\n" * 8000 + b"x\n", b"\n" * 8000 + b"y\n" + b"\n" * 8000
    t = time.monotonic()
    added, deleted = adopt._line_counts(old, new, [adopt._LINE_DIFF_BUDGET])
    assert time.monotonic() - t < 2                  # 8,000 repeated lines took 9 s before the budget (L1, run)
    exact = adopt._line_counts(old, new, [10 ** 12])
    assert added >= exact[0] and deleted >= exact[1]
    # inside the budget the count is exact, and the common head and tail never count
    assert adopt._line_counts(b"a\nb\nc\n", b"a\nB\nc\n", [100]) == (1, 1)


@pytest.mark.parametrize("criterion, change, holds", [
    ({"no_new_files": True}, {"src/new.py": b"n = 1\n"}, False),
    ({"no_new_files": True}, {"src/a.py": b"a = 2\n"}, True),
    ({"paths": ["src/a.py"]}, {"src/b.py": b"b = 9\n"}, False),
    ({"paths": ["src/"]}, {"src/b.py": b"b = 9\n"}, True),
    ({"max_bytes": 3}, {"src/a.py": b"a = 2\n"}, False),
    ({"max_bytes": 64}, {"src/a.py": b"a = 2\n"}, True),
    ({"max_files": 1}, {"src/a.py": b"a = 2\n", "src/b.py": b"b = 9\n"}, False),
    ({"max_files": 2}, {"src/a.py": b"a = 2\n", "src/b.py": b"b = 9\n"}, True),
])
def test_each_typed_criterion_is_evaluated(criterion, change, holds, jstore, cstore, repo, tmp_path):
    job = verified_job(jstore, cstore, repo)
    rec = runs(repo, job, tmp_path, change, k=1)[0]
    changes = adopt.change_set(str(repo[0]), rec.base, rec.staged_commit)
    failure = adopt.criteria_failure([next(iter(criterion.items()))], changes, onto="main",
                                     verified_at=rec.verified_at, now=rec.verified_at)
    assert (failure is None) == holds, failure


def test_max_age_hours_is_evaluated(jstore, cstore, repo, tmp_path):
    job = verified_job(jstore, cstore, repo)
    rec = runs(repo, job, tmp_path, A_FIX, k=1)[0]
    changes = adopt.change_set(str(repo[0]), rec.base, rec.staged_commit)
    later = utc_stamp(NOW + timedelta(hours=3))
    assert adopt.criteria_failure([("max_age_hours", 2)], changes, onto="main", verified_at=rec.verified_at,
                                  now=later) is not None
    assert adopt.criteria_failure([("max_age_hours", 4)], changes, onto="main", verified_at=rec.verified_at,
                                  now=later) is None


def test_an_item_signed_in_another_namespace_is_refused(jstore, cstore, repo, log, keys, tmp_path):
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    payload, _sig, source = human(job, recs[0], keys, log)
    other = keys["laptop"].sign(authorise_message(payload), "levain-dispatch-cancel")
    out = by_human(jstore, cstore, job, recs, (payload, other, source))
    assert out.outcome == "refused" and "does not verify" in out.message


def test_a_rule_land_records_the_generation_of_the_rules_it_was_judged_by(jstore, cstore, repo, log, keys,
                                                                          tmp_path):
    import json

    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    out = by_rule(jstore, cstore, job, runs(repo, job, tmp_path, A_FIX))
    assert out.outcome == "adopted"
    intent = json.loads(next(h for h in out.job.history if h.to_state == "authorised").evidence)
    assert intent["rule"] == {"generation": 4, "index": 0}


# --- L3 r1 1020+17 -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["SRC/a.py", "src/A.py", "src/á.py"])
def test_a_case_or_form_variant_of_a_signed_path_is_refused(variant, jstore, cstore, repo, log, keys, tmp_path):
    # folding approved SRC/A.PY as src/a.py: another file on a case-sensitive disk (codex + complement)
    with_rule(cstore, log, keys, repo, criteria=[{"onto": ["main"]}, {"max_files": 5}])
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, {variant: b"x = 1\n"})
    for out in (by_rule(jstore, cstore, job, recs), by_human(jstore, cstore, job, recs, human(job, recs[0], keys, log))):
        assert out.outcome == "refused" and "names exactly" in out.message, out.message
    assert git(repo[0], "rev-parse", "main") == repo[1]


def test_a_record_naming_another_verifier_is_refused(jstore, cstore, repo, log, keys, tmp_path):
    from dataclasses import replace

    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = [replace(r, verifier={**r.verifier, "id": "my-own-verifier"}) for r in runs(repo, job, tmp_path, A_FIX)]
    out = by_rule(jstore, cstore, job, recs)
    assert out.outcome == "refused" and "another verifier than the signed template" in out.message


def test_a_rule_refuses_more_runs_than_the_templates_k(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    out = by_rule(jstore, cstore, job, runs(repo, job, tmp_path, A_FIX, k=3))
    assert out.outcome == "refused" and "each once" in out.message


def test_a_stale_moved_base_never_fails_another_lands_authorisation(jstore, cstore, repo, log, keys, tmp_path):
    # codex: a request that read the job as verified must not fail it once another land authorised it
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    jstore.authorise(job.id, cause="human_adoption_rule", evidence="{}")
    out = adopt._fail_moved(jstore, job, "MOVED BASE: stale")          # ``job`` is the stale verified read
    assert out.outcome == "refused" and jstore.get(job.id).state == "authorised"
    assert git(repo[0], "for-each-ref", f"refs/heads/levain/dispatch/{job.id}/")    # nothing deleted


def test_a_finisher_that_loses_to_another_reports_the_adoption(jstore, cstore, repo, log, keys, tmp_path):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    stale = jstore.authorise(job.id, cause="human_adoption_rule", evidence=__import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": repo[1],
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2", "rule": None}))
    assert adopt._finish(jstore, stale).outcome == "adopted"           # the first finisher lands
    again = adopt._finish(jstore, stale)                               # the second read it as authorised
    assert again.outcome == "adopted" and again.job.state == "adopted"


def test_a_git_that_fails_is_never_read_as_absent(repo, monkeypatch):
    path, base = repo

    class Failed:
        returncode, stdout, stderr = 128, b"", b"fatal: broken"

    monkeypatch.setattr(adopt, "_git", lambda *a, **k: Failed())
    with pytest.raises(adopt.GitFault):
        adopt._read_branch(str(path), "main")
    with pytest.raises(adopt.GitFault):
        adopt._object_type(str(path), base)


def test_a_finisher_whose_cas_moved_the_branch_says_so_when_another_recorded_it(jstore, cstore, repo, log, keys,
                                                                                tmp_path, monkeypatch):
    # L3 r2 codex: A's CAS moves the branch, B records adopted first; A must report moved=True
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    stale = jstore.authorise(job.id, cause="human_adoption_rule", evidence=__import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": repo[1],
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2", "rule": None}))
    monkeypatch.setattr(adopt, "_after_cas", lambda: adopt._finish(jstore, stale))   # B, inside A's window
    out = adopt._finish(jstore, stale)
    assert out.outcome == "adopted" and out.moved and recs[0].staged_commit in out.message


def test_a_cancel_after_this_lands_cas_reports_the_move(jstore, cstore, repo, log, keys, tmp_path, monkeypatch):
    # the codex-1 known edge: a human cancel wins between the CAS and the adopted write; the move is said, flagged
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    stale = jstore.authorise(job.id, cause="human_adoption_rule", evidence=__import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": repo[1],
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2", "rule": None}))
    monkeypatch.setattr(adopt, "_after_cas", lambda: jstore._transition(job.id, "cancelled", "human_cancel"))
    out = adopt._finish(jstore, stale)
    assert out.outcome == "refused" and out.moved and "git revert" in out.message
    assert git(repo[0], "rev-parse", "main") == recs[0].staged_commit


def test_a_land_reads_the_records_the_verifier_wrote_and_takes_none_from_its_caller(jstore, cstore, repo, log, keys,
                                                                                     tmp_path):
    # the F11-s2a run (levain-memory 1020-f11/f11s2a_run): on main 087c2bf5 a caller's record over a forged staged
    # commit (another tree, parent base, never verified) landed under a standing rule. Now land() has no way to take it
    import inspect

    from tests import test_dispatch_verifier_report as tvr

    with_rule(cstore, log, keys, repo)
    assert {"records", "now"}.isdisjoint(inspect.signature(land).parameters)   # no caller records, no caller clock
    path, base = repo
    # a job walked to verifying, then both runs through the verifier writer itself
    sub = {"template": "fix-lint", "goal": "Fix a.", "read": ["src/a.py"], "write": WRITES, "base": base,
           "budget": {"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}, "grant": ["Edit"],
           "expires_at": utc_stamp(NOW + timedelta(hours=2)), "repo": str(path)}
    j, _cap = jstore.file(sub, resolve_template=resolver(cstore), policy=active_policy(cstore), filer_session="s")
    for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited")):
        j = jstore._transition(j.id, to, cause)
    tree = make_tree(path, base, A_FIX, tmp_path)
    for i in (0, 1):
        r = tvr.vrun(j.id, i)
        raw, digest = tvr.envelope(r, tvr.body(("pass", "pass")))
        j = jstore.record_verification(r, envelope=raw, event=tvr.event(r, at=VERIFIED_AT,
                                                                         verifier_report_digest=digest),
                                        repo=str(path), tree=tree,
                                        model={"requested": "glm-5", "answered": "glm-5", "backend_identity": f"b{i}"})
    assert j.state == "verified"
    stored = jstore.run_records(j.id)
    jstore._clock.now = NOW + timedelta(minutes=10)
    kw = dict(landing_tier=T2, resolve_template=resolver(cstore), policy=active_policy(cstore), onto="main",
              rules=lambda: adoption_rules_at(cstore))
    with pytest.raises(TypeError):
        land(jstore, j.id, stored, **kw)                                  # type: ignore[call-arg, misc]
    out = land(jstore, j.id, **kw)
    assert out.outcome == "adopted", out.message
    assert git(path, "rev-parse", "main") == stored[0].staged_commit
    assert git(path, "rev-parse", "main^{tree}") == tree


@pytest.mark.parametrize("stored", ["absent", "another"])
def test_an_authorised_job_whose_run_record_is_not_the_authorised_one_refuses(stored, jstore, cstore, repo, log, keys,
                                                                             tmp_path, monkeypatch):
    # only store damage reaches this: the finish reads the intent's run row alone, and moves nothing without it
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    stale = jstore.authorise(job.id, cause="human_adoption_rule", evidence=__import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": repo[1],
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2", "rule": None}))
    other = ArtifactRecord(**{**recs[0].__dict__, "verified_at": utc_stamp(NOW)})
    monkeypatch.setattr(jstore, "run_record", lambda job_id, run: None if stored == "absent" else other)
    out = adopt._finish(jstore, stale)
    assert out.outcome == "refused" and "no longer the one authorised" in out.message
    assert git(repo[0], "rev-parse", "main") == repo[1]                       # nothing moved


def test_the_finish_reads_only_the_authorised_runs_row(jstore, cstore, repo, log, keys, tmp_path, monkeypatch):
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    stale = jstore.authorise(job.id, cause="human_adoption_rule", evidence=__import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": repo[1],
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2", "rule": None}))
    monkeypatch.setattr(jstore, "run_records", lambda job_id: 1 / 0)        # every-row read: never on this path
    assert adopt._finish(jstore, stale).outcome == "adopted"


@pytest.mark.parametrize("when", ["records", "authorise"])
def test_a_job_that_ends_during_a_land_is_refused_never_raised(when, jstore, cstore, repo, log, keys, tmp_path,
                                                               monkeypatch):
    # L3 r1 codex 3 + complement 1: the lease lapsing inside the land's own reads raised TransitionRefused
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    real_records, real_authorise = JobStore.run_records, JobStore.authorise

    def records(self, job_id):
        out = real_records(self, job_id)
        if when == "records" and self.get(job_id).state == "verified":
            self._transition(job_id, "failed", "lease_lapsed")
        return out

    def authorise(self, job_id, **kw):
        if when == "authorise":
            self._transition(job_id, "cancelled", "human_cancel")
        return real_authorise(self, job_id, **kw)

    monkeypatch.setattr(JobStore, "run_records", records)
    monkeypatch.setattr(JobStore, "authorise", authorise)
    out = land(jstore, job.id, landing_tier=T2, resolve_template=resolver(cstore), policy=active_policy(cstore),
               onto="main", rules=lambda: adoption_rules_at(cstore))
    assert out.outcome == "refused", out.message
    assert git(repo[0], "rev-parse", "main") == repo[1]


# --- F11-s2b: the world the verbs land in -----------------------------------------------------------------------

class LandWorld:
    """What the land verbs bind from (F11-s2b §0), all real: a cockpit authority log with an ssh laptop key
    (laptop-presence), a consent directory 0700 whose claude_code.json names that cockpit and which holds the
    dispatch store, a signed control store there (verifiers, template, policy, one adoption rule onto main), and a git
    repo whose ``work`` is checked out (main is not). Jobs are filed on the store's real clock, walked to
    ``verifying`` by the private edge writer (no public writer yet) and verified through the REAL
    :meth:`JobStore.record_verification` (fixture envelope bytes, as the verifier-report tests build them)."""

    def __init__(self, tmp: Path, *, rule: bool = True) -> None:
        import json

        from tests.test_consent_queue import Key, Log
        from tests.test_dispatch_contract import POLICY
        from tests.test_dispatch_control import VERIFIERS, tmpl

        tmp = Path(os.path.realpath(tmp))
        self.tmp = tmp
        self.keys = {"laptop": Key(tmp, "laptop")}
        self.root = tmp / "cockpit"
        self.root.mkdir()
        self.log = Log(self.root, self.keys)
        self.log.enrol("laptop", "laptop-presence")
        self.ddir = tmp / "consent"
        self.ddir.mkdir(mode=0o700)
        os.chmod(self.ddir, 0o700)
        (self.ddir / "claude_code.json").write_text(json.dumps({"cockpit_data_root": str(self.root),
                                                                "away_mode": False, "away_hold_seconds": 0}))
        self.repo = tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "src").mkdir()
        (self.repo / "src/a.py").write_text("a = 1\n")
        (self.repo / "src/b.py").write_text("b = 2\nc = 3\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "work")
        ctl.ControlStore.init(self.ddir, self.keys["laptop"].fp, authority=self.log.source)
        self.cstore = ctl.ControlStore(self.ddir, authority=self.log.source)
        accept(self.cstore, self.log, self.keys, "verifiers", 1, VERIFIERS)
        accept(self.cstore, self.log, self.keys, "templates", 2, [tmpl()])
        accept(self.cstore, self.log, self.keys, "provider_policy", 3, POLICY.to_mapping())
        if rule:
            accept(self.cstore, self.log, self.keys, "adoption_rules", 4, [rule_body(self.repo)])
        self.jstore = JobStore(self.ddir)

    def main(self) -> str:
        return git(self.repo, "rev-parse", "main")

    def refs(self, job_id: str = "") -> list[str]:
        return git(self.repo, "for-each-ref", "--format=%(refname)",
                   f"refs/heads/levain/dispatch/{job_id + '/' if job_id else ''}").split()

    def file(self, *, write=("src/a.py", "src/b.py", "src/new.py"), session="sess-1", agent="claude_code"):
        from datetime import datetime, timezone

        sub = {"template": "fix-lint", "goal": "Fix a.", "read": ["src/a.py", "src/b.py"], "write": list(write),
               "base": self.main(), "budget": {"tool_calls": 40, "tokens": 200000, "dollars_cents": 150},
               "grant": ["Edit", "Write"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)),
               "repo": str(self.repo)}
        return self.jstore.file(sub, resolve_template=resolver(self.cstore), policy=active_policy(self.cstore),
                                filer_session=session, filer_agent=agent)

    def verify(self, job_id: str, change: dict, *, results=("pass", "pass")):
        from datetime import datetime, timezone

        from tests import test_dispatch_verifier_report as tvr

        job = self.jstore.get(job_id)
        for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited")):
            job = self.jstore._transition(job.id, to, cause)
        tree = make_tree(self.repo, job.contract.base, change, self.tmp)
        for run, result in enumerate(results):
            r = tvr.vrun(job.id, run)
            raw, digest = tvr.envelope(r, tvr.body((result,) * 2))
            job = self.jstore.record_verification(
                r, envelope=raw, event=tvr.event(r, at=utc_stamp(datetime.now(timezone.utc)),
                                                 verifier_report_digest=digest),
                repo=str(self.repo), tree=tree,
                model={"requested": "glm-5", "answered": "glm-5", "backend_identity": f"backend-{run}"})
        return job

    def verified(self, change=None, **kw):
        job, cap = self.file(**kw)
        return self.verify(job.id, change or A_FIX), cap


@pytest.fixture
def world(tmp_path):
    return LandWorld(tmp_path)


def test_the_authorise_payload_is_built_from_the_stores_own_record(world):
    job, _cap = world.verified()
    fp = world.keys["laptop"].fp
    payload = adopt.authorise_payload_for(world.jstore, job.id, run=1, onto="main", signer_fp=fp)
    record = world.jstore.run_record(job.id, 1)
    assert payload["artifact_digest"] == artifact_digest(record) and payload["run"] == "1"
    assert payload["contract_digest"] == job.digest and payload["onto"] == "main" and payload["signer_fp"] == fp
    assert payload["store_id"] == job.store_id and payload["job_id"] == job.id
    assert set(payload) == adopt.AUTHORISE_KEYS


@pytest.mark.parametrize("run, onto, match", [
    (2, "main", "has no verified record"), (-1, "main", "integer from 0 to 63"), (True, "main", "integer"),
    (0, "refs/heads/main", "bare name"), (0, "levain/x", "under levain/"), (0, "nope", "not a branch here"),
])
def test_the_authorise_payload_is_refused_before_anything_is_signed(world, run, onto, match):
    from levain.dispatch import DispatchError

    job, _cap = world.verified()
    with pytest.raises(DispatchError, match=match):
        adopt.authorise_payload_for(world.jstore, job.id, run=run, onto=onto, signer_fp=world.keys["laptop"].fp)


def test_the_authorise_payload_needs_a_verified_job(world):
    from levain.dispatch.jobs import TransitionRefused

    job, _cap = world.file()
    with pytest.raises(TransitionRefused, match="only a verified or recommended job"):
        adopt.authorise_payload_for(world.jstore, job.id, run=0, onto="main", signer_fp=world.keys["laptop"].fp)


def test_the_sweep_deletes_the_staged_refs_of_jobs_that_ended_without_an_adoption(world):
    cancelled, cap = world.verified()
    world.jstore.cancel_by_planner(cancelled.id, cap)
    live, _ = world.verified({"src/b.py": b"b = 9\nc = 3\n"}, write=("src/b.py",))
    assert len(world.refs(cancelled.id)) == 2 and len(world.refs(live.id)) == 2
    assert adopt.sweep_staged(world.jstore, str(world.repo)) == []
    assert world.refs(cancelled.id) == [] and len(world.refs(live.id)) == 2        # a live job keeps its refs
    git(world.repo, "update-ref", "refs/heads/levain/dispatch/dj-" + "0" * 32 + "/r0", world.main())
    assert adopt.sweep_staged(world.jstore, str(world.repo)) == []
    assert world.refs("dj-" + "0" * 32) != []                                     # a job this store does not hold


def test_the_sweep_reports_a_ref_git_keeps_and_never_raises(world, monkeypatch):
    cancelled, cap = world.verified()
    world.jstore.cancel_by_planner(cancelled.id, cap)
    real = adopt._git

    def refusing(repo, *args, **kw):
        if args[:1] == ("update-ref",):
            return subprocess.CompletedProcess(args, 1, b"", b"no")
        return real(repo, *args, **kw)

    monkeypatch.setattr(adopt, "_git", refusing)
    kept = adopt.sweep_staged(world.jstore, str(world.repo))
    assert len(kept) == 2 and all("would not delete" in k for k in kept)
    monkeypatch.setattr(adopt, "_git", lambda *a, **k: (_ for _ in ()).throw(adopt.GitFault("git is gone")))
    assert adopt.sweep_staged(world.jstore, str(world.repo)) == [
        f"the staged refs in {world.repo} could not be swept: git is gone"]
    assert adopt.sweep_staged(world.jstore, str(world.tmp / "no-such-repo")) == []


@pytest.mark.parametrize("fault", [sqlite3.OperationalError("database is locked"), OSError("disk I/O error")])
def test_the_sweep_says_a_store_it_cannot_read_and_never_raises(world, monkeypatch, fault):
    # L3 r1 B (complement + codex + glm): the sweep runs after an adoption is durable, so its own read never raises
    from levain.dispatch.jobs import JobStoreError

    cancelled, cap = world.verified()
    world.jstore.cancel_by_planner(cancelled.id, cap)
    for exc in (fault, JobStoreError("a stored row does not match its digest")):
        monkeypatch.setattr(world.jstore, "list", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        (line,) = adopt.sweep_staged(world.jstore, str(world.repo))
        assert line.startswith(f"the ended jobs in {world.repo} could not be read") and type(exc).__name__ in line
    assert len(world.refs(cancelled.id)) == 2                                    # nothing was deleted blind


def _land_by_rule(world, job):
    return land(world.jstore, job.id, landing_tier=T2, resolve_template=resolver(world.cstore),
                policy=active_policy(world.cstore), onto="main", rules=lambda: adoption_rules_at(world.cstore))


def test_the_sweep_deletes_an_adopted_jobs_refs_once_its_branch_contains_the_landed_commit(world):
    # F11-s2b §9(3): the landed commit is kept by the branch; the staged refs are clutter once it holds it
    job, _cap = world.verified()
    base = world.main()
    out = _land_by_rule(world, job)
    assert out.outcome == "adopted" and len(world.refs(job.id)) == 2           # land itself sweeps nothing
    git(world.repo, "update-ref", "refs/heads/main", base)                       # the branch no longer holds it
    assert adopt.sweep_staged(world.jstore, str(world.repo)) == []
    assert len(world.refs(job.id)) == 2                                          # kept: the refs still hold it
    git(world.repo, "update-ref", "refs/heads/main", json.loads(out.job.history[-1].evidence)["landed"])
    assert adopt.sweep_staged(world.jstore, str(world.repo)) == []
    assert world.refs(job.id) == []


def test_a_control_store_that_cannot_be_read_during_a_rule_land_is_a_fault(world):
    # F11-s2b §9(2): ControlStateUnavailable is a DispatchError, and the floor's refusal must not swallow it
    from levain.dispatch import DispatchError

    job, _cap = world.verified()
    put_rules = dict(landing_tier=T2, resolve_template=resolver(world.cstore), policy=active_policy(world.cstore),
                     onto="main")

    def unreadable():
        raise ctl.ControlStateUnavailable("the control store cannot be read: disk I/O error")

    with pytest.raises(ctl.ControlStateUnavailable, match="disk I/O error"):
        land(world.jstore, job.id, rules=unreadable, **put_rules)
    assert world.jstore.get(job.id).state == "verified"

    def no_longer_holding():
        raise DispatchError("the active adoption rules no longer hold: a template is gone")

    out = land(world.jstore, job.id, rules=no_longer_holding, **put_rules)
    assert out.outcome == "refused" and "the adoption rules cannot be read: the active adoption rules" in out.message


@pytest.mark.parametrize("winner", ["authorised", "adopted", "base_moved"])
def test_a_job_another_land_took_during_the_records_read_is_reported_as_that_land_left_it(winner, jstore, cstore,
                                                                                         repo, log, keys, tmp_path,
                                                                                         monkeypatch):
    # L3 r2 codex MED + complement LOW: the job read again after the records could already be another land's
    path, base = repo
    with_rule(cstore, log, keys, repo)
    job = verified_job(jstore, cstore, repo)
    recs = runs(repo, job, tmp_path, A_FIX)
    put(jstore, recs)
    real_records = JobStore.run_records
    intent = __import__("json").dumps(
        {"authority": "B", "run": 0, "onto": "main", "staged": recs[0].staged_commit, "base": base,
         "tree": recs[0].tree, "artifact_digest": adopt.artifact_digest(recs[0]), "tier": "T2",
         "rule": {"generation": 4, "index": 0}})

    def records(self, job_id):
        out = real_records(self, job_id)
        if self.get(job_id).state == "verified":
            JobStore.authorise(self, job_id, cause="human_adoption_rule", evidence=intent)
            if winner == "adopted":
                git(path, "update-ref", "refs/heads/main", recs[0].staged_commit)
                self._transition(job_id, "adopted", "adopted", evidence="{}")
            elif winner == "base_moved":
                self._transition(job_id, "failed", "base_moved", evidence="{}")
        return out

    monkeypatch.setattr(JobStore, "run_records", records)
    out = land(jstore, job.id, landing_tier=T2, resolve_template=resolver(cstore), policy=active_policy(cstore),
               onto="main", rules=lambda: adoption_rules_at(cstore))
    expect = {"authorised": "adopted", "adopted": "adopted", "base_moved": "failed"}[winner]
    assert out.outcome == expect and out.job.state == expect, out.message
