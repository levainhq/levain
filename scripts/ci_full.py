#!/usr/bin/env python3
"""Run the FULL levain suite in CI on a pushed branch, wait for the verdict, print pass or fail with the run URL.

The laptop's performance cores are the cockpit's; a full suite run there starves it (11-20: load 118 from three local suites
at once). CI runs the whole shard matrix for nothing (the repo is public), so a seat asks CI instead:

    python3 scripts/ci_full.py [BRANCH]            (BRANCH defaults to the current branch; push it first)

It dispatches `test.yml` on BRANCH (its workflow_dispatch trigger; the workflow's own `if:` and matrix arms decide what a seat-branch dispatch
runs and when it starts: read them in test.yml, not here), finds the run it
made by branch + head sha + creation time, polls it, and prints the conclusion, the wall time read from the run, the run URL
and every job that did not pass. Exit status: 0 the run succeeded, 1 it did not, 2 a usage or dispatch problem (the
branch is not pushed at the local tip, gh failed), 3 the wait timed out (the run keeps going; the URL is printed).

stdlib only; every external call is a `gh` or `git` argv list, so no branch name reaches a shell.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_REPO = "levainhq/levain"
WORKFLOW = "test.yml"
POLL_S = 20
DEFAULT_TIMEOUT_S = 90 * 60  # the hold job may wait on a landing, then the matrix runs
FIND_TIMEOUT_S = 120
CLOCK_SKEW_S = 90
CALL_TIMEOUT_S = 60
ROOT = str(Path(__file__).resolve().parent.parent)  # git is asked about the checkout this script lives in, whatever the caller's cwd


def _run(argv):
    """subprocess.run that never raises: a timeout or a missing binary reads as a failed call (returncode 127)."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(e).__name__}: {e}")


def parse_time(value):
    """An Actions timestamp ('2026-11-20T16:12:10Z') as an aware datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def pick_run(runs, branch, sha, not_before, exclude=()):
    """The newest workflow_dispatch run of `branch` at `sha` created at or after `not_before` (aware datetime) whose id is
    not in `exclude` (the runs that already existed before this dispatch), else None."""
    best = None
    for r in runs:
        if r.get("event") != "workflow_dispatch" or r.get("head_branch") != branch or r.get("head_sha") != sha:
            continue
        if r.get("id") in exclude:
            continue
        created = parse_time(r.get("created_at"))
        if created is None or created < not_before:
            continue
        if best is None or created > parse_time(best["created_at"]):
            best = r
    return best


def wall_seconds(run):
    """Seconds from the run's start to its last update, or None when either time is missing."""
    start, end = parse_time(run.get("run_started_at") or run.get("created_at")), parse_time(run.get("updated_at"))
    return None if start is None or end is None else max(0.0, (end - start).total_seconds())


def hold_and_run_seconds(jobs):
    """(seconds the `hold for land runs` job waited, seconds from the first other job's start to the last one's end), either
    None when the jobs do not show it. The hold is a wait for a landing, not the suite's cost."""
    hold, rest = None, []
    for j in jobs:
        a, b = parse_time(j.get("started_at")), parse_time(j.get("completed_at"))
        if a is None or b is None:
            continue
        if str(j.get("name", "")).startswith("hold for land runs"):
            hold = (b - a).total_seconds()
        elif j.get("conclusion") != "skipped":
            rest.append((a, b))
    run = (max(b for _, b in rest) - min(a for a, _ in rest)).total_seconds() if rest else None
    return hold, run


def not_passed(jobs):
    """Names of jobs whose conclusion is not success or skipped (a failure, a cancellation, a timeout)."""
    return [j.get("name", "?") for j in jobs if j.get("status") == "completed" and j.get("conclusion") not in ("success", "skipped")]


def _api(repo, path):
    p = _run(["gh", "api", f"repos/{repo}/{path}"])
    if p.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    repo = DEFAULT_REPO
    timeout = DEFAULT_TIMEOUT_S
    for a in argv[1:]:
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif a.startswith("--timeout-min="):
            timeout = int(a.split("=", 1)[1]) * 60
        elif a.startswith("--"):
            print(f"ci_full: unknown option {a}", file=sys.stderr)
            return 2
    if len(args) > 1:
        print("usage: ci_full.py [BRANCH] [--repo=OWNER/NAME] [--timeout-min=N]", file=sys.stderr)
        return 2
    if args:
        branch = args[0]
    else:
        p = _run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"])
        branch = p.stdout.strip()
        if p.returncode != 0 or not branch or branch == "HEAD":
            print("ci_full: not on a branch; name one", file=sys.stderr)
            return 2
    if branch == "main" or branch == "land" or branch.startswith("land/"):
        print("ci_full: main and land/** run through the landing chain, not a dispatch", file=sys.stderr)
        return 2

    local = _run(["git", "-C", ROOT, "rev-parse", branch])
    remote = _run(["git", "-C", ROOT, "ls-remote", "origin", f"refs/heads/{branch}"])
    if local.returncode != 0 or remote.returncode != 0 or not remote.stdout.strip():
        print(f"ci_full: branch {branch} is not pushed to origin (or not local): push it first", file=sys.stderr)
        return 2
    sha, remote_sha = local.stdout.strip(), remote.stdout.split()[0]
    if sha != remote_sha:
        print(f"ci_full: origin/{branch} is at {remote_sha[:10]}, local {branch} at {sha[:10]}: push first, CI tests what origin holds", file=sys.stderr)
        return 2

    try:
        before = {r.get("id") for r in _api(repo, f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&branch={branch}&per_page=20")["workflow_runs"]}
    except (RuntimeError, KeyError, ValueError):
        before = set()
    t0 = datetime.now(timezone.utc)
    p = _run(["gh", "workflow", "run", WORKFLOW, "--repo", repo, "--ref", branch])
    if p.returncode != 0:
        print(f"ci_full: dispatch failed: {p.stderr.strip()[:300]}", file=sys.stderr)
        return 2
    print(f"dispatched {WORKFLOW} on {branch} at {sha[:10]}; looking for the run")

    not_before = datetime.fromtimestamp(t0.timestamp() - CLOCK_SKEW_S, timezone.utc)
    run = None
    deadline = time.time() + FIND_TIMEOUT_S
    while run is None and time.time() < deadline:
        time.sleep(5)
        try:
            runs = _api(repo, f"actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&branch={branch}&per_page=20")["workflow_runs"]
        except (RuntimeError, KeyError, ValueError) as e:
            print(f"ci_full: {e}", file=sys.stderr)
            continue
        run = pick_run(runs, branch, sha, not_before, before)
    if run is None:
        print("ci_full: dispatched, but no run for this branch and sha appeared; read the run list by hand", file=sys.stderr)
        return 2
    url = run["html_url"]
    print(f"run {run['id']}: {url}")

    deadline = time.time() + timeout
    errors = 0
    while time.time() < deadline:
        try:
            cur = _api(repo, f"actions/runs/{run['id']}")
            errors = 0
        except (RuntimeError, ValueError) as e:
            errors += 1
            print(f"ci_full: {e}", file=sys.stderr)
            if errors >= 5:
                print(f"ci_full: the API failed 5 times in a row; the run continues at {url}", file=sys.stderr)
                return 2
            time.sleep(POLL_S)
            continue
        if cur.get("status") == "completed":
            ok = cur.get("conclusion") == "success"
            verdict = "PASS" if ok else "FAIL"
            wall = wall_seconds(cur)
            line = f"{verdict}: {cur.get('conclusion')} in {wall / 60:.1f} min wall ({url})" if wall is not None else f"{verdict}: {cur.get('conclusion')} ({url})"
            try:
                jobs = _api(repo, f"actions/runs/{run['id']}/jobs?per_page=100").get("jobs", [])
            except (RuntimeError, ValueError) as e:
                print(line)
                print(f"  (the job list could not be read: {e})")
                return 0 if ok else 1
            hold, ran = hold_and_run_seconds(jobs)
            if hold is not None and ran is not None:
                line += f" = held {hold / 60:.1f} min for land runs + {ran / 60:.1f} min of jobs"
            print(line)
            for name in not_passed(jobs):
                print(f"  did not pass: {name}")
            return 0 if ok else 1
        time.sleep(POLL_S)
    print(f"ci_full: still running after the wait; it keeps going at {url}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
