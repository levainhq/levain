"""``levain dispatch``: the real CLI over a real store, a real git repo and real ssh keys in a real signed authority log.
The template registry does not exist yet (F3), so the tests inject one through the Python API (:func:`resolvers`);
every other path runs as the operator would run it. Exit codes are graded by the CLASS of what went wrong."""
from __future__ import annotations

import json
import os
import sqlite3
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

# the real check, kept before the fixture below stands it down: these tests run under an agent's process tree
REAL_AGENT_CALLER = getattr(dcli, "agent_caller", None)


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
    # the suite itself runs under claude or codex; the operator's terminal has neither for an ancestor
    from levain.consent import band
    monkeypatch.setattr(band, "_ancestors", lambda: iter([]))
    monkeypatch.setattr(dcli, "agent_caller", lambda: None, raising=False)
    return d


@pytest.fixture
def contract_file(tmp_path, repo):
    path, base = repo
    sub = submission(path, base, expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))
    f = tmp_path / "contract.json"
    f.write_text(json.dumps(sub))
    return f


def _soon():
    return utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2))


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
    assert code == 2 and "in your own terminal" in out
    assert "--yes" not in out.splitlines()[-1]               # the refusal never hands an agent the bypass
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
    assert ("no capability for job " + job_id + " in this session's record (claude_code, another-session)") in out
    assert out.index("no capability for job") < out.index("as the HUMAN (signed)")
    last = JobStore(ddir).get(job_id).history[-1]
    assert last.cause == "human_cancel" and "scope moved" in last.evidence


def test_a_human_cancel_without_a_tty_refuses(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "_is_tty", lambda: False)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--human", "--signing-key",
                       str(keys["laptop"].path), "--data-root", str(log.path.parent))
    assert code == 2 and "in your own terminal" in out and "--yes" not in out.splitlines()[-1]
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


# --- fix round 1 ---------------------------------------------------------------------------------------------------

def _sign_argv(ddir, job_id, keys, log, *extra):
    return ("sign", job_id, "--dispatch-dir", str(ddir), "--signing-key", str(keys["laptop"].path), "--data-root",
            str(log.path.parent), *extra)


def _cancel_argv(ddir, job_id, keys, log, *extra):
    return ("cancel", job_id, "--dispatch-dir", str(ddir), "--human", "--signing-key", str(keys["laptop"].path),
            "--data-root", str(log.path.parent), *extra)


def test_a_sign_verifier_that_cannot_run_is_exit_3_and_says_nothing_was_signed(capsys, ddir, contract_file, keys,
                                                                                log, monkeypatch):
    # L1 M1: the contract signature's verifier fault read as "refused"
    job_id = file_one(capsys, ddir, contract_file)["id"]

    def unavailable(*a, **k):
        raise VerifierUnavailable("ssh-keygen could not run")

    monkeypatch.setattr("levain.dispatch.sign.verify_link_sig", unavailable)
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and "could not do it (VerifierUnavailable)" in out
    assert f"nothing was signed; job {job_id} is unchanged (filed)" in out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_a_cancel_fault_says_the_job_is_not_cancelled(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]

    def unavailable(*a, **k):
        raise VerifierUnavailable("ssh-keygen could not run")

    monkeypatch.setattr("levain.dispatch.sign.verify_link_sig", unavailable)
    code, out, _ = run(capsys, *_cancel_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and f"job {job_id} is NOT cancelled; it is still filed and holds its files" in out


@pytest.mark.parametrize("verb", ["sign", "cancel"])
def test_an_authority_log_that_cannot_be_read_is_exit_3(capsys, ddir, contract_file, keys, tmp_path, verb):
    # L1 M1: a garbage cockpit.db read as "refused, nothing changed"
    job_id = file_one(capsys, ddir, contract_file)["id"]
    root = tmp_path / "bad-cockpit"
    root.mkdir()
    (root / "cockpit.db").write_bytes(b"this is not a database" * 100)
    argv = ["--dispatch-dir", str(ddir), "--signing-key", str(keys["laptop"].path), "--data-root", str(root), "--yes"]
    code, out, _ = run(capsys, *(["sign", job_id] if verb == "sign" else ["cancel", job_id, "--human"]), *argv)
    assert code == 3 and "DatabaseError" in out, out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_an_authority_log_its_anchor_refuses_stays_a_refusal(capsys, ddir, contract_file, keys, log, tmp_path):
    from levain.authority import anchor_path
    job_id = file_one(capsys, ddir, contract_file)["id"]
    root = tmp_path / "anchored"
    root.mkdir()
    src, dst = sqlite3.connect(log.path), sqlite3.connect(root / "cockpit.db")
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    anchor_path(root).parent.mkdir(parents=True, exist_ok=True)
    anchor_path(root).write_text("not an anchor")
    code, out, _ = run(capsys, "sign", job_id, "--dispatch-dir", str(ddir), "--signing-key",
                       str(keys["laptop"].path), "--data-root", str(root), "--yes")
    assert code == 2 and "the authority log is refused" in out, out


def _keygen(monkeypatch, behaviour):
    import subprocess

    import levain.cockpit.keys as k
    real = subprocess.run

    def run2(cmd, *a, **kw):
        if "-Y" in cmd and "sign" in cmd:
            return behaviour(cmd, *a, **kw)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(k.subprocess, "run", run2)


def test_an_ssh_keygen_that_cannot_run_to_sign_is_exit_3(capsys, ddir, contract_file, keys, log, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]

    def missing(*a, **k):
        raise FileNotFoundError("ssh-keygen")

    _keygen(monkeypatch, missing)
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and "could not do it (SignerUnavailable)" in out and "nothing was signed" in out


def test_an_ssh_keygen_that_refuses_to_sign_is_exit_2(capsys, ddir, contract_file, keys, log, monkeypatch):
    import subprocess
    job_id = file_one(capsys, ddir, contract_file)["id"]
    _keygen(monkeypatch, lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 1, b"", b""))
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 2 and "could not sign" in out and "(exit 1)" in out


def test_a_value_error_from_below_is_a_fault_not_a_refusal(capsys, ddir, monkeypatch):
    def broken(self, **kw):
        raise ValueError("a malformed row")

    monkeypatch.setattr(JobStore, "list", broken)
    code, out, _ = run(capsys, "list", "--dispatch-dir", str(ddir))
    assert code == 3 and "could not do it (ValueError)" in out


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("verb", ["sign", "cancel"])
def test_an_agent_cannot_sign_or_human_cancel(capsys, ddir, contract_file, keys, log, monkeypatch, verb, yes):
    # L1 M2 / L2 Q1: --yes let an agent sign as the operator
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "agent_caller", lambda: "claude_code")
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    argv = (_sign_argv if verb == "sign" else _cancel_argv)(ddir, job_id, keys, log, *(["--yes"] if yes else []))
    code, out, _ = run(capsys, *argv)
    assert code == 2 and "runs under claude_code" in out and "--yes" not in out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_the_agent_check_is_the_question_bands(monkeypatch):
    from levain.consent import band
    monkeypatch.setattr(band, "_ancestors", lambda: iter([]))
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX"):
        monkeypatch.delenv(var, raising=False)
    assert REAL_AGENT_CALLER() is None
    monkeypatch.setenv("CLAUDECODE", "1")
    assert REAL_AGENT_CALLER() == "claude_code"
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(42, ["/usr/local/bin/codex", "exec"])]))
    assert REAL_AGENT_CALLER() == "codex"


def test_the_prompt_sits_under_a_fixed_height_summary(capsys, ddir, contract_file, keys, log, monkeypatch):
    # L2 Q1: a long view scrolled the digest and the counts away from the question
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "_is_tty", lambda: True)
    seen = {}

    def answer(prompt):
        seen["before"] = capsys.readouterr().out
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    run(capsys, *_sign_argv(ddir, job_id, keys, log))
    job = JobStore(ddir).get(job_id)
    summary = dcli.confirm_summary(job, "sign")
    assert seen["before"].rstrip("\n").splitlines()[-len(summary):] == summary
    text = "\n".join(summary)
    for want in (job.digest, "fix-lint  tier R0", "glm-5, kimi-k2 (k=2)", "40 tool calls", job.contract.expires_at,
                 "2 read, 2 write", "bytes, 2 lines", "filed by  claude_code, session sess-1"):
        assert want in text, want


def test_the_summary_is_the_same_height_for_any_goal(ddir, repo):
    path, base = repo
    store, other = JobStore(ddir / "short"), JobStore(ddir / "long")
    short, _ = store.file(submission(path, base, goal="x", expires_at=_soon()), resolve_template=registry(),
                          policy=POLICY, filer_session="s1")
    long, _ = other.file(submission(path, base, goal="line\n" * 300, expires_at=_soon()),
                         resolve_template=registry(), policy=POLICY, filer_session="s1")
    assert len(dcli.confirm_summary(short, "sign")) == len(dcli.confirm_summary(long, "cancel"))
    assert all("\n" not in line for line in dcli.confirm_summary(long, "cancel"))


@pytest.mark.parametrize("verb", ["file", "cancel", "recommend"])
def test_a_session_other_than_the_detected_one_is_refused(capsys, ddir, contract_file, monkeypatch, verb):
    # L2 Q3: --session let one planner session read another's capability record
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mine")
    target = str(contract_file) if verb == "file" else job_id
    code, out, _ = run(capsys, verb, target, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 2 and "is not this session" in out and "--capability" in out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_the_detected_session_named_explicitly_is_accepted(capsys, ddir, contract_file, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 0 and "as the PLANNER" in out


def test_a_human_cancel_without_a_key_names_the_operators_signed_cancel(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "codex", "--yes")
    assert code == 2 and "the operator's signed cancel needs --signing-key" in out
    assert out.index("no capability for job") < out.index("signed cancel needs")


def test_the_view_names_the_filing_agent_and_session(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    code, out, _ = run(capsys, "show", job_id, "--dispatch-dir", str(ddir))
    assert "filed by    claude_code, session sess-1" in out
    code, out, _ = run(capsys, "show", job_id, "--dispatch-dir", str(ddir), "--json")
    view = json.loads(out)
    assert view["filer_agent"] == "claude_code" and view["filer_session"] == "sess-1"


def test_json_output_is_ascii_escaped(capsys, ddir, tmp_path, repo):
    path, base = repo
    f = tmp_path / "c.json"
    f.write_text(json.dumps(submission(path, base, goal="caf\u00e9 \u00bfs\u00ed?",
                                       expires_at=utc_stamp(datetime.now(timezone.utc) + timedelta(hours=2)))))
    job_id = file_one(capsys, ddir, f)["id"]
    code, out, _ = run(capsys, "show", job_id, "--dispatch-dir", str(ddir), "--json")
    assert code == 0 and out.isascii() and "caf\\u00e9" in out
    assert json.loads(out)["goal"].startswith("caf\u00e9")


def test_render_escapes_every_field_it_shows(ddir, contract_file, capsys, monkeypatch):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    job = JobStore(ddir).get(job_id)
    real_view = dcli.job_view

    def view(j):
        v = real_view(j)
        v["template"] = dict(v["template"], id="fix\x9blint", models=["glm\x1b[2J"])
        v["history"][0]["cause"] = "filed\u202e"
        return v

    monkeypatch.setattr(dcli, "job_view", view)
    text = dcli.render(job)
    assert "\x9b" not in text and "\x1b" not in text and "\u202e" not in text
    assert "fix\\u009blint" in text and "glm\\u001b[2J" in text and "filed\\u202e" in text


# --- fix round 2 ---------------------------------------------------------------------------------------------------

def _faulty_reads(monkeypatch, failures):
    """JobStore.get raises a disk fault on its first ``failures`` calls, then reads for real."""
    real_get, calls = JobStore.get, {"n": 0}

    def get(self, job_id):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise sqlite3.OperationalError("disk I/O error")
        return real_get(self, job_id)

    monkeypatch.setattr(JobStore, "get", get)


@pytest.mark.parametrize("failures", [1, 99])
def test_a_store_fault_on_the_first_read_of_sign_says_nothing_was_signed(capsys, ddir, contract_file, keys, log,
                                                                         monkeypatch, failures):
    # codex OPEN 1b: the job load before the confirm sat outside the guard
    job_id = file_one(capsys, ddir, contract_file)["id"]
    _faulty_reads(monkeypatch, failures)
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and "could not do it (OperationalError)" in out
    state = "filed" if failures == 1 else "state unknown: the store could not be read"
    assert f"nothing was signed; job {job_id} is unchanged ({state})" in out, out
    monkeypatch.undo()
    assert JobStore(ddir).get(job_id).state == "filed"


@pytest.mark.parametrize("failures", [1, 99])
def test_a_store_fault_on_the_first_read_of_a_human_cancel_says_not_cancelled(capsys, ddir, contract_file, keys, log,
                                                                              monkeypatch, failures):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    _faulty_reads(monkeypatch, failures)
    code, out, _ = run(capsys, *_cancel_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and "could not do it (OperationalError)" in out
    want = ("it is still filed and holds its files" if failures == 1
            else "its state is unknown: the store could not be read")
    assert f"job {job_id} is NOT cancelled; {want}" in out, out


def test_a_record_fault_on_a_planner_cancel_says_not_cancelled(capsys, ddir, contract_file):
    job_id = file_one(capsys, ddir, contract_file)["id"]
    os.chmod(ddir / "capabilities", 0o755)
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-1")
    assert code == 3 and "CapabilityRecordError" in out
    assert f"job {job_id} is NOT cancelled; it is still filed and holds its files" in out, out


def test_a_store_fault_names_an_unknown_job_id_escaped(capsys, ddir, keys, log, monkeypatch):
    _faulty_reads(monkeypatch, 99)
    code, out, _ = run(capsys, *_sign_argv(ddir, "dj-\x1b[2J", keys, log, "--yes"))
    assert code == 3 and "\x1b" not in out and "dj-\\u001b[2J" in out


@pytest.mark.parametrize("verb", ["cancel", "recommend", "file"])
def test_a_codex_process_cannot_claim_another_agents_session(capsys, ddir, contract_file, monkeypatch, verb):
    # codex OPEN 4: a supplied --agent suppressed detection, so Codex read a Claude Code session's record
    job_id = file_one(capsys, ddir, contract_file, session="sess-other")["id"]
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-1")
    target = str(contract_file) if verb == "file" else job_id
    code, out, _ = run(capsys, verb, target, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-other")
    assert code == 2 and "--agent 'claude_code' is not the agent this process runs under (codex" in out, out
    assert [j.state for j in JobStore(ddir).list()] == ["filed"]


def test_a_codex_ancestor_wins_over_a_claude_environment(capsys, ddir, contract_file, monkeypatch):
    # a Codex run from a Claude Code shell inherits Claude Code's variables; the ancestor decides, not the flag
    from levain.consent import band
    job_id = file_one(capsys, ddir, contract_file, session="sess-other")["id"]
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(42, ["/usr/local/bin/codex", "exec"])]))
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--agent", "claude_code", "--session",
                       "sess-other")
    assert code == 2 and "is not the agent this process runs under (codex" in out, out
    monkeypatch.setattr(dcli, "agent_caller", REAL_AGENT_CALLER)
    assert dcli.agent_caller() == "codex"


def test_a_session_flag_under_a_detected_agent_with_no_session_is_refused(capsys, ddir, contract_file, monkeypatch):
    # an agent was detected, its session was not: the flag cannot name one for it
    from levain.consent import band
    job_id = file_one(capsys, ddir, contract_file, session="sess-other")["id"]
    monkeypatch.setattr(band, "_ancestors", lambda: iter([(42, ["/usr/local/bin/claude"])]))
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir), "--session", "sess-other")
    assert code == 2 and "is not this session (none, from claude_code's environment)" in out, out
    assert JobStore(ddir).get(job_id).state == "filed"


def test_the_detected_agent_named_explicitly_is_accepted(capsys, ddir, contract_file, monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-1")
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "codex",
                       "--json")
    assert code == 0, out
    job_id = json.loads(out)["id"]
    assert JobStore(ddir).get(job_id).filer_session == "thr-1"
    code, out, _ = run(capsys, "cancel", job_id, "--dispatch-dir", str(ddir))
    assert code == 0 and "as the PLANNER" in out


def test_sign_refuses_under_a_detected_agent_whatever_the_fixture_says(capsys, ddir, contract_file, keys, log,
                                                                       monkeypatch):
    # the operator-only verbs read the same detection as the planner's identity
    job_id = file_one(capsys, ddir, contract_file)["id"]
    monkeypatch.setattr(dcli, "agent_caller", REAL_AGENT_CALLER)
    monkeypatch.setenv("CODEX_THREAD_ID", "thr-1")
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 2 and "runs under codex" in out
    assert JobStore(ddir).get(job_id).state == "filed"


def _record_dir(ddir):
    d = ddir / "capabilities"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


@pytest.mark.parametrize("damage", ["reachable", "unwritable", "corrupt", "symlink"])
def test_a_record_that_cannot_be_kept_refuses_the_filing_before_it_is_made(capsys, ddir, contract_file, damage,
                                                                         tmp_path):
    # complement MED: a record that could not be written after the filing still exited 0
    d = _record_dir(ddir)
    record = caps.record_path(ddir, "claude_code", "sess-1")
    if damage == "reachable":
        os.chmod(d, 0o755)
    elif damage == "unwritable":
        os.chmod(d, 0o500)
    elif damage == "corrupt":
        record.write_bytes(b"not a record")
        os.chmod(record, 0o600)
    else:
        os.rmdir(d)
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        os.symlink(tmp_path / "elsewhere", d)
    try:
        code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "claude_code",
                           "--session", "sess-1")
    finally:
        if damage == "unwritable":
            os.chmod(d, 0o700)
    assert code == 3 and "CapabilityRecordError" in out and "nothing was filed" in out, out
    assert JobStore(ddir).list() == []


def test_a_record_that_fails_after_the_filing_still_shows_the_capability_once(capsys, ddir, contract_file,
                                                                            monkeypatch):
    # the job IS filed by then; a retry with the same idem key is the replay, so the capability is shown, exit 0
    def broken(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(caps, "keep", broken)
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--agent", "claude_code",
                       "--session", "sess-1", "--json")
    assert code == 0, out
    data = json.loads(out)
    assert data["capability"] and data["capability_kept"].startswith("NOT kept")
    assert [j.id for j in JobStore(ddir).list()] == [data["id"]]


def test_a_session_with_no_record_files_without_a_preflight(capsys, ddir, contract_file):
    os.makedirs(ddir / "capabilities", mode=0o755)
    os.chmod(ddir / "capabilities", 0o755)
    code, out, _ = run(capsys, "file", str(contract_file), "--dispatch-dir", str(ddir), "--json")
    assert code == 0 and json.loads(out)["capability_kept"].startswith("not kept"), out


def test_a_fault_after_the_signature_committed_does_not_claim_nothing_was_signed(capsys, ddir, contract_file, keys,
                                                                                 log, monkeypatch):
    # head r2 ruling: a fault raised after the store's own write committed must not say "nothing was signed"
    job_id = file_one(capsys, ddir, contract_file)["id"]
    real_sign = JobStore.sign

    def sign_then_fault(self, *a, **kw):
        real_sign(self, *a, **kw)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(JobStore, "sign", sign_then_fault)
    code, out, _ = run(capsys, *_sign_argv(ddir, job_id, keys, log, "--yes"))
    assert code == 3 and "nothing was signed" not in out, out
    assert f"job {job_id} is now signed (was filed)" in out and "is not known" in out, out
