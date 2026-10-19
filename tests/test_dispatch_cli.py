"""``levain dispatch``: the real CLI over a real store, a real git repo and real ssh keys in a real signed authority log.
The template registry does not exist yet (F3), so the tests inject one through the Python API (:func:`resolvers`);
every other path runs as the operator would run it. Exit codes are graded by the CLASS of what went wrong."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from levain.authority import VerifierUnavailable
from levain.cli import main
from levain.cockpit.consent import utc_stamp
from levain.dispatch import capability as caps
from levain.dispatch import cli as dcli
from levain.dispatch import contract as dcontract
from levain.dispatch.jobs import JobStore
from tests.test_dispatch_contract import POLICY, make_repo, registry, submission
from tests.test_dispatch_sign import keys, log  # noqa: F401  (module fixtures)


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    return make_repo(tmp_path_factory.mktemp("dispatch-cli-repo"))


@pytest.fixture
def ddir(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    # the operator's terminal: no agent session in the environment, and the registry injected through the API
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(dcli, "resolvers", lambda: (registry(), POLICY))
    return d


@pytest.fixture
def contract_file(tmp_path, repo):
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    f = tmp_path / "contract.json"
    f.write_text(json.dumps(sub))
    return f


def run(capsys, *argv):
    code = main(["dispatch", *argv])
    got = capsys.readouterr()
    return code, got.out, got.err


def file_one(capsys, ddir, contract_file, session="sess-1"):
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "claude_code",
                       "--session", session, "--json")
    assert code == 0, out
    return json.loads(out)


def test_file_with_no_registry_refuses_with_exit_2(capsys, ddir, contract_file, monkeypatch):
    monkeypatch.setattr(dcli, "resolvers", lambda: None)
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 2
    assert "no template registry is set up; every contract is R2, which is never dispatched" in out
    assert JobStore(ddir).list() == []


def test_file_prints_the_job_and_keeps_the_capability_once(capsys, ddir, contract_file):
    got = file_one(capsys, ddir, contract_file)
    assert got["state"] == "filed" and got["id"].startswith("dj-") and got["digest"].startswith("sha256:")
    assert got["capability"] and "kept in" in got["capability_kept"]
    assert caps.recall(ddir, "claude_code", "sess-1", got["id"]) == got["capability"]
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "claude_code",
                       "--session", "sess-1")
    assert code == 2 and "already writes" in out           # one live contract per write file


def test_file_with_no_session_says_the_capability_is_not_kept(capsys, ddir, contract_file):
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 0
    assert "not kept (no session was named): it is shown this once" in out
    assert not (ddir / "capabilities").exists()


def test_show_and_list_read_the_resolved_job(capsys, ddir, contract_file, repo):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "show", job_id, "--dispatch-dir", str(ddir))
    assert code == 0
    for want in ("Fix the lint in a.py.", "Keep b as is.", "src/a.py", "src/new.py  (new file)", "fix-lint",
                 "glm-5, kimi-k2 (k=2)", "pytest-bb (black-box", "40 tool calls", "1 filed", "Bash is never granted"):
        assert want in out, want
    code, out, _ = run(capsys, "show", job_id, "--dispatch-dir", str(ddir), "--json")
    view = json.loads(out)
    assert view["id"] == job_id and [e["path"] for e in view["write"]] == ["src/a.py", "src/new.py"]
    assert view["history"][0]["cause"] == "filed"
    code, out, _ = run(capsys, "list", "--dispatch-dir", str(ddir), "--repo", str(repo[0]))
    assert code == 0 and job_id in out
    code, out, _ = run(capsys, "show", "dj-" + "0" * 32, "--dispatch-dir", str(ddir))
    assert code == 2 and "no dispatch job" in out


def test_sign_without_a_tty_refuses(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "_is_tty", lambda: False)
    code, out, _ = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key", str(keys["laptop"].path),
                       "--data-root", str(log.path.parent))
    assert code == 2 and "pass --yes" in out
    assert "Fix the lint in a.py." in out                    # the view came first
    assert JobStore(ddir).get(job_id).state == "filed"


def test_sign_asks_on_a_tty_and_no_is_no(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code, out, _ = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key", str(keys["laptop"].path),
                       "--data-root", str(log.path.parent))
    assert code == 2 and "not confirmed" in out
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    code, out, _ = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key", str(keys["laptop"].path),
                       "--data-root", str(log.path.parent))
    assert code == 0, out
    job = JobStore(ddir).get(job_id)
    assert job.state == "signed" and job.signer_fp == keys["laptop"].fp


def test_sign_with_yes_still_prints_the_view_then_signs(capsys, ddir, contract_file, keys, log):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, err = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key",
                         str(keys["phone"].path), "--data-root", str(log.path.parent), "--yes", "--json")
    assert code == 0, out
    assert "Fix the lint in a.py." in err and json.loads(out)["state"] == "signed"


def test_a_key_that_cannot_sign_t2_is_refused_after_signing(capsys, ddir, contract_file, keys, log):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key",
                       str(keys["presence"].path), "--data-root", str(log.path.parent), "--yes")
    assert code == 2 and "does not cover T2" in out


def test_cancel_as_the_planner_from_the_session_record(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 0 and "as the PLANNER" in out
    assert JobStore(ddir).get(job_id).history[-1].cause == "planner_cancel"


def test_cancel_with_no_capability_is_the_human_signed(capsys, ddir, contract_file, keys, log):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "another-session", "--signing-key", str(keys["laptop"].path), "--data-root",
                       str(log.path.parent), "--yes", "--reason", "scope moved")
    assert code == 0, out
    assert "as the HUMAN (signed)" in out
    last = JobStore(ddir).get(job_id).history[-1]
    assert last.cause == "human_cancel" and "scope moved" in last.evidence


def test_a_human_cancel_without_a_tty_refuses(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "_is_tty", lambda: False)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--human", "--signing-key",
                       str(keys["laptop"].path), "--data-root", str(log.path.parent))
    assert code == 2 and "pass --yes" in out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_recommend_is_the_planners_alone(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "recommend", job_id, "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 2 and "filing planner's alone" in out
    code, out, _ = run(capsys, "recommend", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code",
                       "--session", "sess-1")
    assert code == 2 and "only for a verified job" in out


# --- exit codes by class -------------------------------------------------------------------------------------------

def test_git_that_cannot_run_is_exit_3(capsys, ddir, contract_file, monkeypatch):
    def broken(repo, *args):
        raise dcontract._GitUnavailable("git could not run (TimeoutExpired)")

    monkeypatch.setattr(dcontract, "_git", broken)
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 3 and "could not do it (GitUnavailable)" in out


def test_a_contract_refusal_is_exit_2(capsys, ddir, tmp_path, repo):
    bad = tmp_path / "bad.json"
    path, base = repo
    bad.write_text(json.dumps(submission(path, base, grant=["Bash"])))
    code, out, _ = run(capsys, "file", str(bad), "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 2 and "refused, nothing changed" in out
    bad.write_text("{not json")
    code, out, _ = run(capsys, "file", str(bad), "--dispatch-dir", str(ddir), "--agent", "codex")
    assert code == 2 and "not JSON" in out


def test_a_store_that_cannot_be_trusted_is_exit_3(capsys, ddir):
    os.chmod(ddir, 0o755)
    code, out, _ = run(capsys, "list", "--dispatch-dir", str(ddir))
    assert code == 3 and "JobStoreError" in out


def test_a_capability_record_that_cannot_be_trusted_is_exit_3(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    os.chmod(ddir / "capabilities", 0o755)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 3 and "CapabilityRecordError" in out


def test_a_verifier_that_cannot_run_is_exit_3(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]

    def unavailable(*a, **k):
        raise VerifierUnavailable("ssh-keygen could not run")

    monkeypatch.setattr("levain.dispatch.sign.verify_link_sig", unavailable)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--human", "--signing-key",
                       str(keys["laptop"].path), "--data-root", str(log.path.parent), "--yes")
    assert code == 3 and "VerifierUnavailable" in out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_a_planner_cancel_after_authorisation_points_at_the_human_cancel(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    store = JobStore(ddir)
    for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited"),
                      ("verified", "verifier_pass"), ("authorised", "human_authorises")):
        store._transition(job_id, to, cause)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 2 and "--human" in out
