#!/usr/bin/env python3
"""Hold a seat's workflow_dispatch run until no land/** run of this workflow exists (levain CI, 11-19).

Why: the account's concurrent-job cap serves jobs in the order they were created, with no priority. A seat dispatch that
creates its jobs while a land run runs, or just before the next land push, puts them ahead of the next land run's,
and the landing waits behind them. The `hold` job of .github/workflows/test.yml runs this script first and the
heavy jobs `needs: hold`, so a seat's jobs are created only after the land run's.

Fails OPEN on purpose: an API error, a bad JSON body or the timeout releases the run. The seat's proof is unchanged,
only delayed or not delayed; a red `hold` would read as a red seat. This script never exits non-zero.

stdlib only: the runner has python3 before any install. Reads GH_TOKEN (for `gh`), GITHUB_REPOSITORY and GITHUB_RUN_ID
from the environment; HOLD_POLL_S, HOLD_TIMEOUT_S and HOLD_GRACE_S override the defaults below. Prints no secret.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

DEFAULT_POLL_S = 30
# The workflow job's timeout-minutes (the `hold` job in test.yml) must stay above this.
DEFAULT_TIMEOUT_S = 2400
# Chained land runs are only a short while apart: a hold that released in the gap would put the seat's jobs ahead of the
# next land run, so a land run that finished within this many seconds still blocks.
DEFAULT_GRACE_S = 180
GH_CALL_TIMEOUT_S = 60
# Land runs are push runs of this workflow; the list is newest first.
RUNS_PATH = "repos/{repo}/actions/workflows/test.yml/runs?per_page=50"
# One page (per_page in the path) is enough: a run that can block is queued, running, or finished within the grace, i.e.
# among the newest runs. [judged by levain-speed 8, 11-19, against project_memory/1119-51_gate503/return_R.md: the newest
# page spans hours of this workflow's runs and a land run lasts minutes]
LAND_PREFIX = "land/"
ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})


def _parse_time(value):
    """An Actions API timestamp ('2026-11-19T22:26:00Z') as an aware datetime, or None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _finished_at(run):
    """When a completed run ended, as best the list shows: updated_at, else the start, else the creation."""
    for key in ("updated_at", "run_started_at", "created_at"):
        t = _parse_time(run.get(key))
        if t is not None:
            return t
    return None


def blocking_land_runs(runs, now, grace_s, own_run_id=None):
    """The land runs that block a seat dispatch.

    A run blocks when its head_branch STARTS with `land/` (a name that merely contains `land` does not) and it is queued,
    in progress, waiting or pending, or it completed within `grace_s` seconds of `now` (an aware datetime). The run whose
    id is `own_run_id` never blocks. Pure, and it never raises: a run that is not a dict, or has a missing or odd field,
    does not block (fail open); a completed run with no readable time does not block.
    """
    blockers = []
    if not isinstance(runs, (list, tuple)):
        return blockers
    own = None if own_run_id is None else str(own_run_id)
    for run in runs:
        try:
            if not isinstance(run, dict):
                continue
            branch = run.get("head_branch")
            if not isinstance(branch, str) or not branch.startswith(LAND_PREFIX):
                continue
            if own is not None and str(run.get("id")) == own:
                continue
            status = run.get("status")
            if status in ACTIVE_STATUSES:
                blockers.append(run)
            elif status == "completed":
                ended = _finished_at(run)
                if ended is not None and 0 <= (now - ended).total_seconds() <= grace_s:
                    blockers.append(run)
        except Exception:  # an odd run must never turn the hold red
            continue
    return blockers


# The clock and the sleep are module names so a test can swap them.
def _utcnow():
    return datetime.now(timezone.utc)


_monotonic = time.monotonic
_sleep = time.sleep


def _gh_runs(repo):
    """One `gh api` call; returns the list of runs. Raises on a non-zero exit, a timeout or a body that is not the runs JSON."""
    proc = subprocess.run(
        ["gh", "api", RUNS_PATH.format(repo=repo)],
        capture_output=True, text=True, timeout=GH_CALL_TIMEOUT_S, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("gh api exit %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:200]))
    body = json.loads(proc.stdout)
    runs = body.get("workflow_runs") if isinstance(body, dict) else body
    if not isinstance(runs, list):
        raise ValueError("no workflow_runs list in the response")
    return runs


def _clean(text):
    """A branch name is an API string: keep it to one printable line."""
    return "".join(c if c.isprintable() else "?" for c in str(text))[:120]


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def main():
    poll_s = max(_env_int("HOLD_POLL_S", DEFAULT_POLL_S), 1)
    timeout_s = _env_int("HOLD_TIMEOUT_S", DEFAULT_TIMEOUT_S)
    grace_s = _env_int("HOLD_GRACE_S", DEFAULT_GRACE_S)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    own = os.environ.get("GITHUB_RUN_ID") or None
    start = _monotonic()
    while True:
        try:
            runs = _gh_runs(repo)
        except Exception as exc:  # fail open: an API or JSON error releases the run
            print("released: api error (%s: %s)" % (type(exc).__name__, _clean(exc)), flush=True)
            return 0
        blockers = blocking_land_runs(runs, _utcnow(), grace_s, own)
        if not blockers:
            print("released: no land run", flush=True)
            return 0
        for run in blockers:
            print("holding: land run %s on %s (%s)" % (_clean(run.get("id")), _clean(run.get("head_branch")),
                                                       _clean(run.get("status"))), flush=True)
        if _monotonic() - start + poll_s > timeout_s:
            print("released: timeout after %d s" % timeout_s, flush=True)
            return 0
        _sleep(poll_s)


if __name__ == "__main__":
    sys.exit(main())
