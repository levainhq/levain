"""The nightly dep-drift workflow's issue step, and the pins the lock-install path shares across files.

The issue step is run for real here: its ``run:`` block is pulled out of the workflow and executed under ``bash -e``
with a stub ``gh`` that records its arguments and the issue body it was handed, so what a timed-out, a failed, a red
and a green night each file (or close) is what is asserted, not what the YAML looks like."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DRIFT_YML = WORKFLOWS / "dep-drift.yml"
BASH = "/bin/bash"


def _drift_job():
    return yaml.safe_load(DRIFT_YML.read_text())["jobs"]["drift"]


def _step(prefix):
    return next(s for s in _drift_job()["steps"] if s.get("name", "").startswith(prefix))


ISSUE_STEP_NAME = "File, update or close the one dep-drift issue"

RESULT_LINE = 'DEP_DRIFT {"result": "red", "stage": "suite", "changed": ["foo 1.0 -> 1.1"], "failed_tests": ["tests/test_a.py::t"]}'


def _run_issue_step(tmp_path, *, code, log_lines, existing="", job_status="success"):
    """Run the issue step; return (returncode, gh calls, the body handed to `gh issue create|comment`, stderr)."""
    step = _step(ISSUE_STEP_NAME)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    bodies = tmp_path / "bodies.log"
    gh = bindir / "gh"
    gh.write_text(
        '#!/bin/sh\n'
        'echo "gh $*" >> "$STUB_CALLS"\n'
        'case "$*" in *"issue list"*) echo "$STUB_EXISTING";; esac\n'
        'prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--body-file" ]; then cat "$a" >> "$STUB_BODIES"; echo "-----" >> "$STUB_BODIES"; fi; prev="$a"; done\n'
        'exit 0\n')
    gh.chmod(0o755)
    (bindir / "python").symlink_to(sys.executable)
    runner_temp = tmp_path / "rt"
    runner_temp.mkdir()
    if log_lines is not None:
        (runner_temp / "drift.log").write_text("\n".join(log_lines) + "\n")
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RUNNER_TEMP": str(runner_temp), "STUB_CALLS": str(calls),
           "STUB_BODIES": str(bodies), "STUB_EXISTING": existing, "DRIFT_CODE": code, "JOB_STATUS": job_status,
           "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "levainhq/levain", "GITHUB_RUN_ID": "42",
           "GH_TOKEN": "x", "HOME": str(tmp_path)}
    proc = subprocess.run([BASH, "-e", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)
    return (proc.returncode, calls.read_text().splitlines() if calls.exists() else [],
            bodies.read_text() if bodies.exists() else "", proc.stderr)


needs_bash = pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")


# --- the job's shape ------------------------------------------------------------------------------------------------------

def test_the_drift_job_timeout_clears_the_suite_time_test_yml_measured():
    """test.yml's own comment puts the unsharded 3.12 suite at ~55-67 min; a cap below that cancels the job with no result."""
    assert _drift_job()["timeout-minutes"] >= 100


def test_the_issue_step_runs_whatever_happened_before_it():
    assert _step(ISSUE_STEP_NAME)["if"].strip() == "always()"
    assert _step("Fail the run when the drift was red")["if"].strip().startswith("always()")


def test_the_issue_step_is_told_the_job_status_and_the_drift_code():
    env = _step(ISSUE_STEP_NAME)["env"]
    assert env["DRIFT_CODE"] == "${{ steps.drift.outputs.code }}"
    assert env["JOB_STATUS"] == "${{ job.status }}"


def test_the_drift_runs_stay_serialised_and_the_label_is_the_dedupe_key():
    wf = yaml.safe_load(DRIFT_YML.read_text())
    assert wf["concurrency"] == {"group": "dep-drift", "cancel-in-progress": False}
    run = _step(ISSUE_STEP_NAME)["run"]
    assert "gh issue list --state open --label dep-drift" in run


# --- the issue step, executed ---------------------------------------------------------------------------------------------

@needs_bash
def test_a_timed_out_job_files_a_did_not_finish_issue_not_a_red_one(tmp_path):
    """The job was cancelled mid-suite: the drift step never wrote its exit code, so DRIFT_CODE is empty."""
    rc, calls, body, err = _run_issue_step(tmp_path, code="", log_lines=["collecting ...", "tests/test_x.py ..."],
                                           job_status="cancelled")
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "did not finish" in body.lower()
    assert "cancelled" in body
    assert "does not pass on a freshly re-resolved lock" not in body, "a job that never finished must not claim the suite went red"
    assert "tests/test_x.py ..." in body, "the log tail is the only evidence of how far it got"


@needs_bash
def test_a_failed_setup_step_with_no_log_at_all_still_files_an_issue(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="", log_lines=None, job_status="failure")
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "did not finish" in body.lower() and "failure" in body


@needs_bash
def test_a_did_not_finish_night_comments_on_the_one_open_issue_instead_of_opening_a_second(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="", log_lines=["x"], existing="7", job_status="cancelled")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue comment 7")], calls
    assert not any(c.startswith("gh issue create") or c.startswith("gh issue close") for c in calls), calls


@needs_bash
def test_a_red_night_names_the_changed_packages_and_the_failing_tests(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="1", log_lines=["noise", RESULT_LINE])
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "Red on" in body and "foo 1.0 -> 1.1" in body and "tests/test_a.py::t" in body
    assert "did not finish" not in body.lower()


@needs_bash
def test_a_red_night_with_no_result_line_says_the_script_died(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="2", log_lines=["Traceback ..."], existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue comment 7")], calls
    assert "none (the script died before printing one)" in body


@needs_bash
def test_a_green_night_closes_the_open_issue_and_files_nothing(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="0", log_lines=[RESULT_LINE], existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue close 7")], calls
    assert not any(c.startswith("gh issue create") or c.startswith("gh issue comment") for c in calls), calls


@needs_bash
def test_a_green_night_with_no_open_issue_does_nothing(tmp_path):
    rc, calls, body, err = _run_issue_step(tmp_path, code="0", log_lines=["ok"], existing="")
    assert rc == 0, err
    assert not any(c.startswith(("gh issue create", "gh issue comment", "gh issue close")) for c in calls), calls


# --- one uv pin, one pip pin ----------------------------------------------------------------------------------------------

def _script_pin(name):
    m = re.search(rf'^{name}="\$\{{{name}:-([0-9.]+)\}}"$', (ROOT / "scripts" / "ci_install_locked.sh").read_text(), re.M)
    assert m, f"scripts/ci_install_locked.sh no longer defaults {name} in the shape this test reads"
    return m[1]


def _pins_in_workflows(package):
    """Every `<package>==X` under .github/workflows, as {file: [X, ...]}."""
    found = {}
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        hits = re.findall(rf'(?<![\w-]){package}==([0-9][0-9.]*)', wf.read_text())
        if hits:
            found[wf.name] = hits
    return found


def test_every_workflow_uv_pin_is_the_scripts_default():
    want = _script_pin("UV_VERSION")
    found = _pins_in_workflows("uv")
    # The sites that exist today; a site that disappears or a new file that pins uv must be looked at, not skipped.
    assert set(found) == {"test.yml", "dep-drift.yml"}, found
    assert sum(len(v) for v in found.values()) >= 3, found
    wrong = {f: v for f, v in found.items() if set(v) != {want}}
    assert not wrong, f"uv pinned differently from scripts/ci_install_locked.sh ({want}): {wrong}"


def test_the_workflow_pip_pin_is_the_scripts_default():
    """py39's `pip==25.3` is a different pin on purpose (26.1+ needs Python 3.10); only the 3.12 sites must agree."""
    want = _script_pin("PIP_VERSION")
    text = DRIFT_YML.read_text()
    assert re.findall(r'(?<![\w-])pip==([0-9][0-9.]*)', text) == [want]


def test_the_stale_lock_message_quotes_the_scripts_uv_pin():
    text = (ROOT / "tests" / "test_ci_install_locked.py").read_text()
    assert "0.13.5" not in text, "test_ci_install_locked.py quotes a literal uv version; it must read the script's default"


# --- h11 for the system python, installed from a hashed file ---------------------------------------------------------------

H11_FILE = ROOT / "requirements-ci-root-h11.txt"


def _file_h11():
    text = H11_FILE.read_text()
    m = re.search(r"^h11==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n?)+)", text, re.M)
    assert m, "requirements-ci-root-h11.txt must pin h11 exactly, with hashes"
    return m[1], set(re.findall(r"sha256:([0-9a-f]{64})", m[2]))


def _lock_h11():
    pkgs = [p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"] if p["name"] == "h11"]
    assert len(pkgs) == 1
    p = pkgs[0]
    hashes = {p["sdist"]["hash"].split(":")[1]} | {w["hash"].split(":")[1] for w in p["wheels"]}
    return p["version"], hashes


def test_the_root_h11_file_is_the_locks_h11_with_the_locks_hashes():
    assert _file_h11() == _lock_h11()


def test_the_root_h11_file_satisfies_the_floor_root_vendors():
    spec = subprocess.run([sys.executable, str(ROOT / "tests" / "ci" / "root_closure.py"), "h11-spec"],
                          capture_output=True, text=True, timeout=60, cwd=ROOT).stdout.strip()
    floor = tuple(int(x) for x in spec.removeprefix("h11>=").split("."))
    assert tuple(int(x) for x in _file_h11()[0].split(".")) >= floor


def test_every_root_h11_install_is_hashed_and_from_the_file():
    seen = 0
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for m in re.finditer(r"^\s*run: (sudo /usr/bin/python3 -m pip install .*)$", wf.read_text(), re.M):
            seen += 1
            cmd = m[1]
            assert "--require-hashes" in cmd and "-r requirements-ci-root-h11.txt" in cmd, (wf.name, cmd)
            assert "h11-spec" not in cmd, (wf.name, cmd)
    assert seen == 3, f"expected the three root h11 installs (two in test.yml, one in dep-drift.yml), found {seen}"
