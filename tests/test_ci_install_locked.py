"""scripts/ci_install_locked.sh and requirements-ci-py39.txt: the locked-install path every CI job uses.

The script runs here under /bin/bash (the oldest bash a CI runner can put first on PATH) with stub ``python`` and ``uv``
that only record their arguments, so what is checked is the script's own argument handling and install order."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10: the backport the py39 pin file carries
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci_install_locked.sh"
BASH = "/bin/bash"


def _run(tmp_path, *args):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    (bindir / "python").write_text('#!/bin/sh\necho "python $*" >> "$STUB_LOG"\n')
    (bindir / "uv").write_text(
        '#!/bin/sh\necho "uv $*" >> "$STUB_LOG"\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "-o" ]; then echo "# stub" > "$2"; fi; shift; done\n'
    )
    for stub in ("python", "uv"):
        (bindir / stub).chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "STUB_LOG": str(log), "RUNNER_TEMP": str(tmp_path), "HOME": str(tmp_path)}
    proc = subprocess.run([BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_no_arguments_runs_under_the_old_system_bash(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert not any("--extra" in c for c in calls)


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_two_arguments_become_two_extras_on_the_rest_export_only(tmp_path):
    proc, calls = _run(tmp_path, "dev", "openhands")
    assert proc.returncode == 0, proc.stderr
    exports = [c for c in calls if c.startswith("uv ") and " export " in c]
    assert len(exports) == 2
    build, rest = exports
    assert "--only-group build" in build and "--extra" not in build
    assert "--extra dev --extra openhands" in rest and "--group build" not in rest


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_the_build_group_is_installed_hashed_before_the_rest_builds_without_isolation(tmp_path):
    proc, calls = _run(tmp_path, "dev")
    assert proc.returncode == 0, proc.stderr
    installs = [c for c in calls if c.startswith("python -m pip install") and "--require-hashes" in c]
    assert len(installs) == 2
    build, rest = installs
    assert "levain-build.txt" in build and "--no-build-isolation" not in build
    assert "levain-req.txt" in rest and "--no-build-isolation" in rest and "--no-deps" in rest
    # the editable install of the project itself also builds with the already-installed backend
    editable = [c for c in calls if c.endswith("-e .")]
    assert editable and "--no-build-isolation" in editable[0]


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_the_exports_go_to_a_private_directory_that_is_removed_on_exit(tmp_path):
    proc, calls = _run(tmp_path, "dev")
    assert proc.returncode == 0, proc.stderr
    exports = [c for c in calls if " export " in c]
    assert exports and all(f"{tmp_path}/levain-install." in c for c in exports)
    assert [p.name for p in tmp_path.glob("levain-install.*")] == []


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_pip_is_pinned_to_an_exact_version_beside_uv(tmp_path):
    proc, calls = _run(tmp_path)
    boot = next(c for c in calls if "--upgrade" in c or "uv==" in c)
    assert re.search(r"\bpip==\d+\.\d+(\.\d+)?\b", boot), boot
    assert "uv==" in boot
    assert "--upgrade pip " not in boot + " "


# --- requirements-ci-py39.txt -------------------------------------------------------------------------------------------

PY39 = ROOT / "requirements-ci-py39.txt"
# Packages whose py39 pin is deliberately not the lock's version, each with why. The lock is python >=3.12, so a
# release that dropped 3.9 cannot be the one pinned here. An entry that no longer differs is itself a failure.
PY39_DIVERGES = {
    "pytest": "pytest 9 requires Python >=3.10",
    "iniconfig": "the lock's release requires Python >=3.10",
    "pluggy": "pytest 8 caps it below the lock's release",
}


def _py39_pins():
    pins = {}
    for line in PY39.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)", line)
        if m:
            pins[m[1].lower()] = m[2]
    return pins


def _lock_versions():
    out = {}
    for pkg in tomllib.loads((ROOT / "uv.lock").read_text()).get("package", []):
        if pkg.get("version"):
            out.setdefault(pkg["name"], set()).add(pkg["version"])
    return out


def test_py39_file_names_the_command_that_regenerates_it():
    head = [ln for ln in PY39.read_text().splitlines() if ln.startswith("#")]
    text = "\n".join(head)
    assert "uv pip compile" in text and "--python-version 3.9" in text and "--generate-hashes" in text
    assert "-o requirements-ci-py39.txt" in text


def test_py39_pins_equal_the_lock_except_the_declared_divergences():
    lock = _lock_versions()
    differing = {n for n, v in _py39_pins().items() if n in lock and v not in lock[n]}
    assert differing == set(PY39_DIVERGES), (
        f"py39 pins that differ from uv.lock: {sorted(differing)}; declared: {sorted(PY39_DIVERGES)}. "
        "Regenerate the file with the command in its header, or declare the divergence with its reason.")


def test_py39_h11_satisfies_the_floor_the_system_python_install_uses():
    spec = subprocess.run([sys.executable, str(ROOT / "tests" / "ci" / "root_closure.py"), "h11-spec"],
                          capture_output=True, text=True, timeout=60, cwd=ROOT).stdout.strip()
    floor = tuple(int(x) for x in spec.removeprefix("h11>=").split("."))
    have = tuple(int(x) for x in _py39_pins()["h11"].split("."))
    assert have >= floor


# --- .github/workflows/dep-drift.yml ------------------------------------------------------------------------------------

DRIFT = (ROOT / ".github" / "workflows" / "dep-drift.yml").read_text()


def test_drift_workflow_is_guarded_against_forks():
    assert "if: github.repository == 'levainhq/levain'" in DRIFT


def test_drift_issue_title_has_no_date_and_one_open_label_is_the_dedupe_key():
    assert 'title="dependency drift red"' in DRIFT
    assert "gh issue list --state open --label dep-drift" in DRIFT
    assert "gh issue comment" in DRIFT and "gh issue close" in DRIFT


def test_drift_calls_a_flake_only_at_the_suite_stage_and_names_result_and_stage_otherwise():
    flaky = DRIFT.index("likely flaky test, not drift")
    assert DRIFT.rindex('elif d.get("stage") == "suite":', 0, flaky) > DRIFT.rindex("if d[", 0, flaky)
    assert 'str(d.get("result"))' in DRIFT and 'str(d.get("stage"))' in DRIFT


def test_the_build_group_carries_the_common_backends_beside_setuptools():
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["build"]
    assert {"hatchling", "wheel"} <= {re.split(r"[<>=!~ ]", b)[0] for b in build}
    assert {"hatchling", "wheel"} <= set(_lock_versions())


def test_drift_says_a_red_with_no_changed_package_is_likely_flaky():
    assert "red with no version change: likely flaky test, not drift" in DRIFT


# --- the lock agrees with pyproject.toml (offline twin of the stage-0 `uv lock --check`) --------------------------------

def _requirement_keys(requirements, extra=None):
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    keys = set()
    for text in requirements:
        req = Requirement(text)
        found = re.search(r"""extra == ['"]([^'"]+)['"]""", str(req.marker or ""))
        keys.add((req.name.lower().replace("_", "-"), (found[1] if found else None) or extra, SpecifierSet(str(req.specifier))))
    return keys


def _lock_levain_entry():
    return next(p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"] if p["name"] == "levain")


def test_the_lock_records_the_dependencies_pyproject_declares_now():
    """A dependency floor moved in pyproject.toml without regenerating uv.lock leaves every locked install on the old
    pin while the stage-0 job fails the push; this catches it on the laptop, with no network, before the push."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = _requirement_keys(pyproject["project"]["dependencies"])
    for extra, reqs in pyproject["project"]["optional-dependencies"].items():
        declared |= _requirement_keys(reqs, extra)
    locked = _requirement_keys(
        (f"{d['name']}{d.get('specifier', '')}" + (f"; {d['marker']}" if d.get("marker") else ""))
        for d in _lock_levain_entry()["metadata"]["requires-dist"]
    )
    assert declared == locked, f"only in pyproject: {sorted(declared - locked, key=str)}; only in uv.lock: {sorted(locked - declared, key=str)}"


def test_the_lock_records_the_build_group_pyproject_declares_now():
    declared = _requirement_keys(tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["build"])
    locked = _requirement_keys(
        f"{d['name']}{d.get('specifier', '')}" for d in _lock_levain_entry()["metadata"]["requires-dev"]["build"]
    )
    assert declared == locked


# --- the drift workflow's issue step, run for real against a stub `gh` ---------------------------------------------------

def _drift_issue_step():
    import yaml

    steps = yaml.safe_load(DRIFT)["jobs"]["drift"]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith("File, update or close"))


def _run_issue_step(tmp_path, drift_code, log_text, open_issue=""):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "gh.log"
    (bindir / "gh").write_text(
        '#!/bin/sh\n'
        'echo "gh $*" >> "$GH_LOG"\n'
        'if [ "$1 $2" = "issue list" ]; then echo "$GH_OPEN"; fi\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "--body-file" ]; then cp "$2" "$GH_BODY"; fi; shift; done\n'
    )
    (bindir / "gh").chmod(0o755)
    (tmp_path / "drift.log").write_text(log_text)
    env = {
        "PATH": f"{bindir}:{os.path.dirname(sys.executable)}:/usr/bin:/bin", "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path), "GH_LOG": str(calls), "GH_BODY": str(tmp_path / "body.copy"), "GH_OPEN": open_issue,
        "GH_TOKEN": "x", "DRIFT_CODE": drift_code, "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "levainhq/levain", "GITHUB_RUN_ID": "1",
    }
    proc = subprocess.run([BASH, "-c", _drift_issue_step()], env=env, capture_output=True, text=True, timeout=60)
    body = (tmp_path / "body.copy")
    return proc, (calls.read_text().splitlines() if calls.exists() else []), (body.read_text() if body.exists() else "")


RED_LINE = 'DEP_DRIFT {"result": "red", "stage": "suite", "changed": ["pyyaml 6.0.1 -> 6.0.3"], "failed_tests": ["tests/test_x.py::test_y"]}'


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_a_red_night_files_one_issue_naming_the_changed_package_and_the_failing_test(tmp_path):
    proc, calls, body = _run_issue_step(tmp_path, "1", "noise\n" + RED_LINE + "\n")
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("gh issue create --title dependency drift red --label dep-drift") for c in calls), calls
    assert "- pyyaml 6.0.1 -> 6.0.3" in body and "- tests/test_x.py::test_y" in body


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_a_second_red_night_comments_on_the_open_issue_instead_of_filing_another(tmp_path):
    proc, calls, _ = _run_issue_step(tmp_path, "1", RED_LINE + "\n", open_issue="7")
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("gh issue comment 7") for c in calls) and not any("issue create" in c for c in calls)


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_the_first_green_night_closes_the_open_issue_and_a_green_night_with_none_does_nothing(tmp_path):
    proc, calls, _ = _run_issue_step(tmp_path, "0", "", open_issue="7")
    assert proc.returncode == 0, proc.stderr
    assert any(c.startswith("gh issue close 7") for c in calls)
    other = tmp_path / "second"
    other.mkdir()
    proc, calls, _ = _run_issue_step(other, "0", "")
    assert proc.returncode == 0 and not any("issue close" in c or "issue create" in c for c in calls)
