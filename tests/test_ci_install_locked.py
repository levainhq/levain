"""scripts/ci_install_locked.sh and requirements-ci-py39.txt: the locked-install path every CI job uses.

The script runs here under /bin/bash (the oldest bash a CI runner can put first on PATH) with stub ``python`` and ``uv``
that only record their arguments, so what is checked is the script's own argument handling and install order."""
import os
import re
import shutil
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


def _req_names(reqs):
    return {re.split(r"[<>=!~ ;\[]", r.strip(), maxsplit=1)[0].lower().replace("_", "-") for r in reqs}


@pytest.mark.skipif(not os.path.exists(BASH), reason="needs /bin/bash")
def test_the_project_installs_editable_last_with_a_backend_the_locked_build_group_supplies(tmp_path):
    """The shard jobs install ONLY through this script, so `import levain` works because of its last line (the editable
    install, after both hashed installs) and that install builds without isolation, so the project's own backend
    ([build-system].requires) must be in the `build` group the script installs from the lock first."""
    proc, calls = _run(tmp_path, "dev", "openhands")
    assert proc.returncode == 0, proc.stderr
    pips = [c for c in calls if c.startswith("python -m pip install")]
    assert pips[-1].endswith("--no-deps --no-build-isolation -e ."), pips
    assert [("--require-hashes" in c) for c in pips[-3:-1]] == [True, True], pips
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    backend = _req_names(pyproject["build-system"]["requires"])
    assert backend and backend <= _req_names(pyproject["dependency-groups"]["build"]), (
        "the editable install runs with --no-build-isolation: its backend must come from the build group")
    assert backend <= {n.lower() for n in _lock_versions()}


def test_the_test_jobs_install_only_through_the_script_and_with_the_dev_extra():
    import yaml
    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "test.yml").read_text())["jobs"]
    for name in ("stage0", "test"):
        lines = [ln.strip() for s in jobs[name]["steps"] if isinstance(s.get("run"), str) for ln in s["run"].splitlines()]
        via_script = [ln for ln in lines if "ci_install_locked.sh" in ln]
        assert via_script and all(re.search(r"ci_install_locked\.sh\s.*\bdev\b", ln) for ln in via_script), (name, via_script)
        # the one other pip install is the hashed root-python h11 file; nothing else resolves or installs the project
        stray = [ln for ln in lines if re.search(r"\bpip\b.*\binstall\b", ln) and "requirements-ci-root-h11.txt" not in ln]
        assert not stray, (name, stray)


def test_the_dev_extra_and_the_py39_file_hold_what_the_workflow_tests_import():
    """tests/test_ci_dep_drift_workflow.py imports yaml (PyYAML) and, below Python 3.11, tomli. The 3.12 and 3.13 jobs get
    PyYAML from the `dev` extra and tomllib from the standard library. The py39 job installs requirements-ci-py39.txt, has
    tomli but no PyYAML, and never selects these two files: the closure selects only test files that import a levain
    module (tests/ci/root_closure.py `_test_files`), and neither imports one."""
    import ast
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert {"pytest", "pytest-xdist", "pyyaml"} <= _req_names(dev)
    assert {"pytest", "pytest-xdist", "pyyaml"} <= {n.lower() for n in _lock_versions()}
    assert {"tomli", "pytest"} <= set(_py39_pins())
    for name in ("test_ci_dep_drift_workflow.py", "test_ci_install_locked.py"):
        tree = ast.parse((ROOT / "tests" / name).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(m == "levain" or m.startswith("levain.") for m in imported), (name, imported)


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
    assert DRIFT.rindex('elif d.get("stage") == "suite":', 0, flaky) > DRIFT.rindex("if changed:", 0, flaky)
    assert 'str(d.get("result"))' in DRIFT and 'str(d.get("stage"))' in DRIFT


def test_the_build_group_carries_the_common_backends_beside_setuptools():
    build = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["build"]
    assert {"hatchling", "wheel"} <= {re.split(r"[<>=!~ ]", b)[0] for b in build}
    assert {"hatchling", "wheel"} <= set(_lock_versions())


def test_drift_says_a_red_with_no_changed_package_is_likely_flaky():
    assert "red with no version change: likely flaky test, not drift" in DRIFT


# --- uv.lock against pyproject.toml -------------------------------------------------------------------------------------

def _pinned_uv():
    """The one uv pin: the install script's default (tests/test_ci_dep_drift_workflow.py holds every workflow to it)."""
    return re.search(r'^UV_VERSION="\$\{UV_VERSION:-([0-9.]+)\}"$', SCRIPT.read_text(), re.M)[1]


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH (the lock is checked with `uv lock --check`)")
def test_uv_lock_agrees_with_pyproject_toml(tmp_path):
    """Every CI install reads uv.lock and never resolves, so a pyproject.toml change (a moved pin, a new extra) that
    leaves the lock behind installs the OLD versions with every other test in this file green. `--offline` keeps this
    a pure comparison of the two files: a fresh lock needs no fetch, a stale one cannot be re-resolved without one."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "UV_CACHE_DIR": str(tmp_path / "uv-cache"),
           "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1"}
    proc = subprocess.run(["uv", "lock", "--check", "--offline", "--directory", str(ROOT)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        f"uv.lock is stale against pyproject.toml; regenerate it with the uv the workflows pin (`uv=={_pinned_uv()}`: "
        f"`uvx uv@{_pinned_uv()} lock`) and commit it.\n{proc.stdout}{proc.stderr}")
