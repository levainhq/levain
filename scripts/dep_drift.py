#!/usr/bin/env python3
"""Dependency drift: would the suite pass if the lock were regenerated today?

    python3 scripts/dep_drift.py [--repo DIR] [--python PYTHON] [--no-suite] [--keep] [--extras dev,openhands]

Works in a temporary copy of the repo's pyproject.toml, README and uv.lock (the checkout is never locked or
modified): `uv lock --upgrade` re-resolves every package to its newest allowed release, the packages whose
version changed against the committed lock are listed (old -> new), the upgraded lock is installed hash-checked into a
fresh venv, an import smoke runs, then the suite runs. It prints one machine-readable line last:

    DEP_DRIFT {"result": "green|red|resolve_failed|install_failed", "stage": ..., "changed": [...], ...}  (a suite run also carries failed_tests)

and exits 0 on green, 1 on red/failed. The same logic backs .github/workflows/dep-drift.yml, which runs it on a schedule
and files the issue; a laptop scheduled task can call it and read the last line.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

SMOKE = "import levain, openhands.sdk"
COPIED = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "NOTICE")


def lock_versions(text: str) -> dict[str, tuple[str, ...]]:
    """name -> sorted versions, from the text of a uv.lock. A source-only package (the project itself) has no version."""
    out: dict[str, set[str]] = {}
    for pkg in tomllib.loads(text).get("package", []):
        version = pkg.get("version")
        if version:
            out.setdefault(pkg["name"], set()).add(version)
    return {name: tuple(sorted(v)) for name, v in out.items()}


def diff_versions(old: dict[str, tuple[str, ...]], new: dict[str, tuple[str, ...]]) -> list[dict]:
    """Every package whose version set differs: [{name, old, new}], old/new None when added/removed; sorted by name."""
    rows = []
    for name in sorted(set(old) | set(new)):
        if old.get(name) != new.get(name):
            rows.append({
                "name": name,
                "old": ",".join(old[name]) if name in old else None,
                "new": ",".join(new[name]) if name in new else None,
            })
    return rows


def format_change(row: dict) -> str:
    return f"{row['name']} {row['old'] or '(new)'} -> {row['new'] or '(removed)'}"


def result_line(result: str, stage: str, changed: list[dict], **extra) -> str:
    return "DEP_DRIFT " + json.dumps({"result": result, "stage": stage, "changed": [format_change(r) for r in changed], **extra})


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=cwd, env=env)


def run_capture(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> tuple[int, list[str]]:
    """Run a command, echo its output as it arrives, and return (exit code, the lines)."""
    print("+ " + " ".join(cmd), flush=True)
    lines: list[str] = []
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line.rstrip("\n"))
    return proc.wait(), lines


def failed_tests(lines: list[str], limit: int = 20) -> list[str]:
    """The node ids from pytest's short summary (`FAILED path::name - reason`, `ERROR path::name`), first `limit`."""
    ids: list[str] = []
    for line in lines:
        head, _, rest = line.partition(" ")
        if head in ("FAILED", "ERROR") and "::" in rest:
            ids.append(rest.split(" - ")[0].strip())
    return ids[:limit]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--python", default=sys.executable, help="interpreter the scratch venv is made from")
    ap.add_argument("--extras", default="dev,openhands")
    ap.add_argument("--no-suite", action="store_true", help="resolve, diff, install and smoke only")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory and print its path")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    work = Path(tempfile.mkdtemp(prefix="dep-drift-"))
    try:
        return _drift(repo, work, args)
    finally:
        if args.keep:
            print(f"scratch kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


def _drift(repo: Path, work: Path, args) -> int:
    lockdir = work / "lock"
    lockdir.mkdir()
    for name in COPIED:
        if (repo / name).exists():
            shutil.copy2(repo / name, lockdir / name)
    old = lock_versions((repo / "uv.lock").read_text())
    if run(["uv", "lock", "--upgrade"], cwd=lockdir) != 0:
        print(result_line("resolve_failed", "resolve", []))
        return 1
    new = lock_versions((lockdir / "uv.lock").read_text())
    changed = diff_versions(old, new)
    print(f"{len(changed)} package(s) changed against the committed lock")
    for row in changed:
        print("  " + format_change(row))

    venv = work / "venv"
    py = venv / "bin" / "python"
    if run([args.python, "-m", "venv", str(venv)]) != 0:
        print(result_line("install_failed", "install", changed))
        return 1
    # The same install path every test run uses, pointed at the re-resolved lock: the venv's python and pip come
    # first on PATH, LOCK_DIR names the scratch copy that holds the upgraded uv.lock, and the project installs from repo.
    install_env = {**os.environ, "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
                   "LOCK_DIR": str(lockdir), "RUNNER_TEMP": str(work), "VIRTUAL_ENV": str(venv)}
    extras = [e for e in args.extras.split(",") if e]
    if run([str(repo / "scripts" / "ci_install_locked.sh"), *extras], cwd=repo, env=install_env) != 0:
        print(result_line("install_failed", "install", changed))
        return 1
    if run([str(py), "-c", SMOKE]) != 0:
        print(result_line("red", "smoke", changed, smoke=SMOKE))
        return 1
    if args.no_suite:
        print(result_line("green", "smoke", changed))
        return 0
    env = {**os.environ, "LEVAIN_TEST_WAIT_SCALE": os.environ.get("LEVAIN_TEST_WAIT_SCALE", "2")}
    code, out = run_capture([str(py), "-m", "pytest", "-n", "auto", "-q", "-p", "no:cacheprovider", "--rootdir", str(repo),
                             "--basetemp", str(work / "pt"), str(repo / "tests")], cwd=repo, env=env)
    print(result_line("green" if code == 0 else "red", "suite", changed, pytest_exit=code, failed_tests=failed_tests(out)))
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
