"""The nightly dep-drift workflow's reporter job, and the pins the lock-install path shares across files.

The reporter is a job of its own (``report``, ``needs: drift``, ``if: always()``) so that a hard timeout of the job that
holds the suite cannot take the reporter with it. Its issue step is run for real here: the ``run:`` block is pulled out
of the workflow and executed under ``bash -e`` with a stub ``gh`` that records its arguments, its ``GH_REPO`` and the
issue body it was handed. The step's ``env:`` is taken from the workflow too, with each ``${{ }}`` expression resolved
from the values the ``drift`` job would have handed over (``needs.drift.result``, ``needs.drift.outputs.code``), so what a
timed-out, a failed-setup, a red and a green night each file (or close) is what is asserted, not what the YAML looks like."""
import os
import re
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


def _workflow():
    return yaml.safe_load(DRIFT_YML.read_text())


def _job(name):
    return _workflow()["jobs"][name]


def _step(job, prefix):
    return next(s for s in _job(job)["steps"] if s.get("name", "").startswith(prefix))


def _needs(job):
    needs = _job(job).get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


ISSUE_STEP_NAME = "File, update or close the one dep-drift issue"

RESULT_LINE = 'DEP_DRIFT {"result": "red", "stage": "suite", "changed": ["foo 1.0 -> 1.1"], "failed_tests": ["tests/test_a.py::t"]}'


def _expand(value, ctx):
    """Resolve each ``${{ expr }}`` from ``ctx``; an expression the test did not model fails loudly instead of passing as text."""
    def sub(m):
        expr = m[1].strip()
        assert expr in ctx, f"the reporter step reads {expr!r}, which this test does not model"
        return ctx[expr]
    return re.sub(r"\$\{\{(.*?)\}\}", sub, str(value))


def _run_issue_step(tmp_path, *, code, drift_result, log_lines, existing="", repo="levainhq/levain",
                    python3_broken=False):
    """Run the reporter's issue step as the `report` job would; return (returncode, gh calls, the body handed to
    `gh issue create|comment`, stderr, the GH_REPO each gh call saw). ``code`` is the drift job's ``outputs.code`` and
    ``drift_result`` its ``result``: a job cut off by its timeout has code "" and result "cancelled"."""
    step = _step("report", ISSUE_STEP_NAME)
    ctx = {"needs.drift.outputs.code": code, "needs.drift.result": drift_result, "github.repository": repo,
           "github.token": "x"}
    step_env = {k: _expand(v, ctx) for k, v in (step.get("env") or {}).items()}
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    bodies = tmp_path / "bodies.log"
    repos = tmp_path / "repos.log"
    gh = bindir / "gh"
    # With neither a git remote (there is no checkout in the reporter) nor GH_REPO, the real gh has no repository and exits 1.
    gh.write_text(
        '#!/bin/sh\n'
        'echo "gh $*" >> "$STUB_CALLS"\n'
        'echo "${GH_REPO:-}" >> "$STUB_REPOS"\n'
        'if [ -z "${GH_REPO:-}" ]; then echo "gh: no git remote and no GH_REPO" >&2; exit 1; fi\n'
        'case "$*" in *"issue list"*) echo "$STUB_EXISTING";; esac\n'
        'prev=""\n'
        'for a in "$@"; do if [ "$prev" = "--body-file" ]; then cat "$a" >> "$STUB_BODIES"; echo "-----" >> "$STUB_BODIES"; fi; prev="$a"; done\n'
        'exit 0\n')
    gh.chmod(0o755)
    # The reporter has no setup-python: ubuntu-24.04 has `python3` and no bare `python`, so the stub dir offers only python3.
    if python3_broken:
        (bindir / "python3").write_text('#!/bin/sh\necho "python3: simulated crash" >&2\nexit 3\n')
        (bindir / "python3").chmod(0o755)
    else:
        (bindir / "python3").symlink_to(sys.executable)
    runner_temp = tmp_path / "rt"
    runner_temp.mkdir()
    if log_lines is not None:
        # the `drift-log` artifact, downloaded into $RUNNER_TEMP/drift-log
        (runner_temp / "drift-log").mkdir()
        (runner_temp / "drift-log" / "drift.log").write_text("\n".join(log_lines) + "\n")
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "RUNNER_TEMP": str(runner_temp), "STUB_CALLS": str(calls),
           "STUB_BODIES": str(bodies), "STUB_REPOS": str(repos), "STUB_EXISTING": existing,
           "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": repo, "GITHUB_RUN_ID": "42",
           "HOME": str(tmp_path), **step_env}
    proc = subprocess.run([BASH, "-e", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60,
                          cwd=tmp_path)
    return (proc.returncode, calls.read_text().splitlines() if calls.exists() else [],
            bodies.read_text() if bodies.exists() else "", proc.stderr,
            repos.read_text().splitlines() if repos.exists() else [])


needs_bash = pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")


# --- the job's shape ------------------------------------------------------------------------------------------------------

def test_the_drift_job_timeout_clears_the_suite_time_test_yml_measured():
    """test.yml's own comment puts the unsharded 3.12 suite at ~55-67 min; a cap below that cancels the job with no result."""
    assert _job("drift")["timeout-minutes"] >= 100


def test_the_reporter_is_a_separate_job_that_runs_after_whatever_happened_to_the_drift_job():
    """An always() step INSIDE the job a timeout cancels has no guaranteed budget; a job of its own does."""
    jobs = _workflow()["jobs"]
    report = jobs["report"]
    assert report is not jobs["drift"]
    assert "drift" in _needs("report")
    assert "always()" in str(report["if"])
    # it is not the job that holds the suite, and the job that holds the suite does not hold the reporter
    assert not any("dep_drift.py" in str(s.get("run", "")) for s in report["steps"])
    assert not any(s.get("name") == ISSUE_STEP_NAME for s in jobs["drift"]["steps"])
    assert 0 < report["timeout-minutes"] <= 15 < jobs["drift"]["timeout-minutes"]


def test_the_reporter_still_has_the_forks_guard_the_drift_job_has():
    """always() also runs when `drift` was skipped, and a fork's skipped drift must not file an issue on the fork."""
    assert "github.repository == 'levainhq/levain'" in str(_job("report")["if"])


def test_the_reporter_has_no_checkout_and_reads_the_repo_from_gh_repo():
    report = _job("report")
    assert not any(str(s.get("uses", "")).startswith("actions/checkout") for s in report["steps"])
    env = _step("report", ISSUE_STEP_NAME)["env"]
    assert env["GH_REPO"] == "${{ github.repository }}"


def test_the_reporter_is_told_the_drift_jobs_result_and_exit_code():
    env = _step("report", ISSUE_STEP_NAME)["env"]
    assert env["DRIFT_CODE"] == "${{ needs.drift.outputs.code }}"
    assert env["JOB_STATUS"] == "${{ needs.drift.result }}"
    assert env["GH_TOKEN"] == "${{ github.token }}"
    assert _job("drift")["outputs"] == {"code": "${{ steps.drift.outputs.code }}"}


def test_only_the_reporter_may_write_issues():
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert "issues" not in (wf["jobs"]["drift"].get("permissions") or {})
    # The reporter has no checkout, and a same-run artifact download takes no token scope, so it holds issues:write and nothing else.
    assert wf["jobs"]["report"]["permissions"] == {"issues": "write"}


def test_the_reporter_holds_no_scope_it_does_not_use():
    report = _job("report")
    assert set(report["permissions"]) == {"issues"}
    assert not any(str(s.get("uses", "")).startswith("actions/checkout") for s in report["steps"])


def test_the_cancelled_run_trade_off_is_written_above_the_reporters_if():
    """A user cancelling the whole run files a "Did not finish" issue, on purpose: `!cancelled()` would also stop the reporter
    after a timeout-cancel. The `if:` stays `always()`, and the reason sits on the line above it."""
    lines = DRIFT_YML.read_text().splitlines()
    report_at = lines.index("  report:")
    if_at = next(i for i in range(report_at, len(lines)) if lines[i].startswith("    if:"))
    above = lines[if_at - 1].strip()
    assert above.startswith("#") and "cancelled" in above and "did-not-finish by design" in above, above
    assert "!cancelled()" in above and "timeout-cancel" in above, above
    assert _job("report")["if"].strip() == "${{ always() && github.repository == 'levainhq/levain' }}"


def test_the_drift_job_still_fails_the_run_on_red_and_saves_its_log_for_the_reporter():
    assert _step("drift", "Fail the run when the drift was red")["if"].strip().startswith("always()")
    upload = _step("drift", "Keep the drift log")
    assert upload["if"].strip() == "always()"
    assert upload.get("continue-on-error") is True
    assert upload["with"]["name"] == "drift-log" and upload["with"]["path"].endswith("/drift.log")
    download = next(s for s in _job("report")["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert download["with"]["name"] == "drift-log"
    assert download.get("continue-on-error") is True, "a night with no saved log (the job was cut first) must still be reported"
    assert download["with"]["path"].endswith("/drift-log")


def test_the_drift_runs_stay_serialised_and_the_label_is_the_dedupe_key():
    assert _workflow()["concurrency"] == {"group": "dep-drift", "cancel-in-progress": False}
    run = _step("report", ISSUE_STEP_NAME)["run"]
    assert "gh issue list --state open --label dep-drift" in run


# --- the issue step, executed ---------------------------------------------------------------------------------------------

@needs_bash
def test_a_timed_out_job_files_a_did_not_finish_issue_not_a_red_one(tmp_path):
    """The drift job was cancelled mid-suite: needs.drift.result is "cancelled" and its code output was never written."""
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="", drift_result="cancelled",
                                                  log_lines=["collecting ...", "tests/test_x.py ..."])
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "did not finish" in body.lower()
    assert "cancelled" in body
    assert "does not pass on a freshly re-resolved lock" not in body, "a job that never finished must not claim the suite went red"
    assert "tests/test_x.py ..." in body, "the log tail is the only evidence of how far it got"


@needs_bash
def test_a_failed_setup_step_with_no_log_at_all_still_files_an_issue(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="", drift_result="failure", log_lines=None)
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "did not finish" in body.lower() and "failure" in body


@needs_bash
def test_a_did_not_finish_night_comments_on_the_one_open_issue_instead_of_opening_a_second(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="", drift_result="cancelled", log_lines=["x"],
                                                  existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue comment 7")], calls
    assert not any(c.startswith("gh issue create") or c.startswith("gh issue close") for c in calls), calls


@needs_bash
def test_a_red_night_names_the_changed_packages_and_the_failing_tests(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure",
                                                  log_lines=["noise", RESULT_LINE])
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert "Red on" in body and "foo 1.0 -> 1.1" in body and "tests/test_a.py::t" in body
    assert "did not finish" not in body.lower()


@needs_bash
def test_a_red_night_with_no_result_line_says_the_script_died(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="2", drift_result="failure",
                                                  log_lines=["Traceback ..."], existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue comment 7")], calls
    assert "none (the script died before printing one)" in body


UNREADABLE = "The result line could not be read"


@needs_bash
@pytest.mark.parametrize("result_line", [
    'DEP_DRIFT {"result": "red", "stage": "suite", "changed": [',          # truncated JSON
    "DEP_DRIFT this is not json at all",                                    # not JSON
    "DEP_DRIFT ",                                                           # nothing after the marker
    'DEP_DRIFT {"result": "red", "stage": "suite", "failed_tests": []}',    # no `changed` key
    'DEP_DRIFT {"result": "red", "changed": "foo 1.0 -> 1.1"}',             # `changed` is a string
    'DEP_DRIFT {"result": "red", "changed": 5}',                            # `changed` is a number
    'DEP_DRIFT {"result": "red", "changed": null}',                         # `changed` is null
    "DEP_DRIFT [1, 2]",                                                     # valid JSON, not an object
    'DEP_DRIFT "just a string"',
], ids=["truncated", "not-json", "empty", "no-changed", "changed-string", "changed-number", "changed-null",
        "array", "string"])
def test_a_result_line_that_cannot_be_read_still_files_the_issue(tmp_path, result_line):
    """The reporter runs under `set -euo pipefail`: a parser that raised on a bad result line would end the step before
    `gh issue create`, and the night would be a red `report` job with no issue. The issue is filed, says the line could not
    be read, and still carries the log tail."""
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure",
                                                  log_lines=["collecting ...", "the last log line", result_line])
    assert rc == 0, err
    assert "Traceback" not in err, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert UNREADABLE in body
    assert "the last log line" in body, "the log tail is still the evidence"
    assert "Red on" in body, "the headline for a red night is unchanged"


@needs_bash
def test_a_result_line_that_cannot_be_read_comments_on_the_open_issue(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure",
                                                  log_lines=["DEP_DRIFT {oops"], existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue comment 7")], calls
    assert UNREADABLE in body


@needs_bash
def test_a_readable_result_line_does_not_claim_it_could_not_be_read(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure",
                                                  log_lines=[RESULT_LINE])
    assert rc == 0, err
    assert UNREADABLE not in body and "foo 1.0 -> 1.1" in body


@needs_bash
def test_a_parser_that_crashes_outright_still_files_the_issue(tmp_path):
    """The belt under the braces: whatever kills the python call (a missing or broken interpreter), the step still files."""
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure",
                                                  log_lines=["the last log line", RESULT_LINE], python3_broken=True)
    assert rc == 0, err
    assert any(c.startswith("gh issue create") for c in calls), calls
    assert UNREADABLE in body and "the last log line" in body


@needs_bash
@pytest.mark.parametrize("failed", ["5", '"tests/a.py::x"', "null", "{}"])
def test_a_failed_tests_field_that_is_not_a_list_does_not_unread_the_whole_line(tmp_path, failed):
    line = 'DEP_DRIFT {"result": "red", "stage": "suite", "changed": ["foo 1.0 -> 1.1"], "failed_tests": %s}' % failed
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure", log_lines=[line])
    assert rc == 0, err
    assert UNREADABLE not in body and "foo 1.0 -> 1.1" in body
    assert "Failing tests" not in body


@needs_bash
def test_an_empty_changed_list_is_a_readable_line_not_an_unreadable_one(tmp_path):
    line = 'DEP_DRIFT {"result": "red", "stage": "suite", "changed": [], "failed_tests": ["tests/test_b.py::u"]}'
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="1", drift_result="failure", log_lines=[line])
    assert rc == 0, err
    assert UNREADABLE not in body and "likely flaky test" in body and "tests/test_b.py::u" in body


@needs_bash
def test_a_green_night_closes_the_open_issue_and_files_nothing(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="0", drift_result="success",
                                                  log_lines=[RESULT_LINE], existing="7")
    assert rc == 0, err
    assert [c for c in calls if c.startswith("gh issue close 7")], calls
    assert not any(c.startswith("gh issue create") or c.startswith("gh issue comment") for c in calls), calls


@needs_bash
def test_a_green_night_with_no_open_issue_does_nothing(tmp_path):
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="0", drift_result="success", log_lines=["ok"],
                                                  existing="")
    assert rc == 0, err
    assert not any(c.startswith(("gh issue create", "gh issue comment", "gh issue close")) for c in calls), calls


@needs_bash
def test_every_gh_call_names_its_repository_because_the_reporter_has_no_git_remote(tmp_path):
    """gh reads GH_REPO, not GITHUB_REPOSITORY; with checkout failed or absent there is no remote to fall back on."""
    rc, calls, body, err, repos = _run_issue_step(tmp_path, code="", drift_result="cancelled", log_lines=["x"])
    assert rc == 0, err
    assert calls and set(repos) == {"levainhq/levain"}, (calls, repos)


# --- every workflow file, .yml and .yaml ----------------------------------------------------------------------------------

def _workflow_files(root=WORKFLOWS):
    """Every workflow GitHub would run: both spellings of the extension."""
    return sorted(p for p in Path(root).iterdir() if p.suffix in (".yml", ".yaml"))


def test_the_workflow_enumeration_reads_both_extensions(tmp_path):
    for name in ("a.yml", "b.yaml", "c.txt", "d.yml.bak"):
        (tmp_path / name).write_text("x: 1\n")
    assert [p.name for p in _workflow_files(tmp_path)] == ["a.yml", "b.yaml"]
    assert {"test.yml", "dep-drift.yml"} <= {p.name for p in _workflow_files()}


# --- one uv pin, one pip pin ----------------------------------------------------------------------------------------------

def _script_pin(name):
    m = re.search(rf'^{name}="\$\{{{name}:-([0-9.]+)\}}"$', (ROOT / "scripts" / "ci_install_locked.sh").read_text(), re.M)
    assert m, f"scripts/ci_install_locked.sh no longer defaults {name} in the shape this test reads"
    return m[1]


def _pins_in_workflows(package, root=WORKFLOWS):
    """Every `<package>==X` under the workflows directory (.yml and .yaml), as {file: [X, ...]}."""
    found = {}
    for wf in _workflow_files(root):
        hits = re.findall(rf'(?<![\w-]){package}==([0-9][0-9.]*)', wf.read_text())
        if hits:
            found[wf.name] = hits
    return found


def _pin_errors(package, want, root=WORKFLOWS):
    return {f: v for f, v in _pins_in_workflows(package, root).items() if set(v) != {want}}


def test_every_workflow_uv_pin_is_the_scripts_default():
    want = _script_pin("UV_VERSION")
    found = _pins_in_workflows("uv")
    # The sites that exist today; a site that disappears or a new file that pins uv must be looked at, not skipped.
    assert set(found) == {"test.yml", "dep-drift.yml"}, found
    assert sum(len(v) for v in found.values()) >= 3, found
    assert not _pin_errors("uv", want), f"uv pinned differently from scripts/ci_install_locked.sh ({want})"


def test_a_divergent_uv_pin_in_a_yaml_file_is_caught(tmp_path):
    (tmp_path / "ok.yml").write_text("run: python -m pip install uv==9.9.9\n")
    (tmp_path / "release.yaml").write_text("run: python -m pip install uv==0.12.0\n")
    assert _pin_errors("uv", "9.9.9", tmp_path) == {"release.yaml": ["0.12.0"]}
    assert set(_pins_in_workflows("uv", tmp_path)) == {"ok.yml", "release.yaml"}


def test_the_workflow_pip_pin_is_the_scripts_default():
    """py39's `pip==25.3` is a different pin on purpose (26.1+ needs Python 3.10); only the 3.12 sites must agree."""
    want = _script_pin("PIP_VERSION")
    text = DRIFT_YML.read_text()
    assert re.findall(r'(?<![\w-])pip==([0-9][0-9.]*)', text) == [want]


def _uv_literals(text):
    """Every `uv==X.Y` version literal in ``text``; an f-string hole or a bare `uv==` has none."""
    return re.findall(r'(?<![\w-])uv==[0-9]+(?:\.[0-9]+)*(?:[.-]?(?:post|dev|rc|a|b)[0-9]*)?', text)


def test_the_stale_lock_message_quotes_the_scripts_uv_pin():
    text = (ROOT / "tests" / "test_ci_install_locked.py").read_text()
    assert not _uv_literals(text), (
        "test_ci_install_locked.py quotes a literal uv version; it must read the script's UV_VERSION: "
        f"{_uv_literals(text)}")


def test_the_uv_literal_check_sees_any_version_and_not_a_computed_one():
    assert _uv_literals("regenerate with uv==0.13.5") == ["uv==0.13.5"]
    assert _uv_literals("regenerate with uv==0.14.2.post1") == ["uv==0.14.2.post1"]
    assert _uv_literals("uv==1") == ["uv==1"]
    assert _uv_literals('f"`uv=={_pinned_uv()}`" and "uv==" in boot') == []


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


PIP_INSTALL_UNDER_PYTHON3 = re.compile(r"python3(?:\.\d+)?\s+-m\s+pip\s+install\b")


def _root_h11_installs(root=WORKFLOWS):
    """Every logical command line, in every step's ``run`` string of every workflow (.yml and .yaml, the one-line form
    and the ``run: |`` block form alike), that runs ``python3 -m pip install`` and names h11: (file, job, command)."""
    found = []
    for wf in _workflow_files(root):
        for job_name, job in (yaml.safe_load(wf.read_text()) or {}).get("jobs", {}).items():
            for step in job.get("steps") or []:
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for line in run.replace("\\\n", " ").splitlines():
                    if PIP_INSTALL_UNDER_PYTHON3.search(line) and "h11" in line:
                        found.append((wf.name, job_name, line.strip()))
    return found


def _root_h11_problems(installs):
    return [i for i in installs
            if "--require-hashes" not in i[2] or not re.search(r"-r\s+requirements-ci-root-h11\.txt\b", i[2])
            or "h11-spec" in i[2]]


def test_every_root_h11_install_is_hashed_and_from_the_file():
    installs = _root_h11_installs()
    assert not _root_h11_problems(installs), _root_h11_problems(installs)
    assert len(installs) == 3, f"expected the three root h11 installs (two in test.yml, one in dep-drift.yml), found {installs}"


def _write_workflow(tmp_path, name, run_block):
    (tmp_path / name).write_text("jobs:\n  j:\n    runs-on: x\n    steps:\n      - name: s\n        run: " + run_block)
    return tmp_path


def test_a_root_h11_install_in_block_form_is_counted_and_an_unhashed_one_is_caught(tmp_path):
    good = "sudo /usr/bin/python3 -m pip install --break-system-packages --require-hashes --no-deps -r requirements-ci-root-h11.txt"
    _write_workflow(tmp_path, "good.yaml", "|\n          set -e\n          " + good + "\n")
    (_write_workflow(tmp_path, "bad.yml", "|\n          set -e\n"
                     "          sudo /usr/bin/python3 -m pip install --break-system-packages h11==0.16.0\n"))
    installs = _root_h11_installs(tmp_path)
    assert {i[0] for i in installs} == {"good.yaml", "bad.yml"}, installs
    assert {i[0] for i in _root_h11_problems(installs)} == {"bad.yml"}


def test_a_root_h11_install_continued_over_lines_is_read_as_one_command(tmp_path):
    cont = "|\n          sudo /usr/bin/python3 -m pip install --break-system-packages \\\n            --require-hashes -r requirements-ci-root-h11.txt\n"
    _write_workflow(tmp_path, "cont.yml", cont)
    installs = _root_h11_installs(tmp_path)
    assert len(installs) == 1 and not _root_h11_problems(installs), installs
    loose = "|\n          sudo /usr/bin/python3 -m pip install --break-system-packages \\\n            -r requirements-ci-root-h11.txt\n"
    _write_workflow(tmp_path, "loose.yml", loose)
    assert {i[0] for i in _root_h11_problems(_root_h11_installs(tmp_path))} == {"loose.yml"}


def test_an_unhashed_root_h11_install_in_a_yaml_file_is_caught(tmp_path):
    _write_workflow(tmp_path, "release.yaml", "sudo /usr/bin/python3 -m pip install --break-system-packages h11\n")
    assert [i[0] for i in _root_h11_problems(_root_h11_installs(tmp_path))] == ["release.yaml"]
