"""scripts/ci_full.py: finding the run a dispatch made, and reading its verdict (the pure parts; the dispatch itself is run in CI)."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("ci_full", Path(__file__).resolve().parent.parent / "scripts" / "ci_full.py")
ci_full = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_full)

T0 = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)


def _run(rid, created, branch="seat/x", sha="abc", event="workflow_dispatch"):
    return {"id": rid, "created_at": created, "head_branch": branch, "head_sha": sha, "event": event}


def test_pick_run_takes_the_newest_dispatch_of_this_branch_and_sha_made_after_the_start():
    runs = [
        _run(1, "2026-11-20T15:00:00Z"),                       # before the dispatch
        _run(2, "2026-11-20T16:00:30Z", sha="other"),          # another sha
        _run(3, "2026-11-20T16:00:31Z", branch="seat/y"),      # another branch
        _run(4, "2026-11-20T16:00:32Z", event="push"),         # not a dispatch
        _run(5, "2026-11-20T16:00:40Z"),
        _run(6, "2026-11-20T16:01:10Z"),
    ]
    assert ci_full.pick_run(runs, "seat/x", "abc", T0)["id"] == 6
    assert ci_full.pick_run(runs[:4], "seat/x", "abc", T0) is None


def test_not_passed_names_failures_cancellations_and_timeouts_only():
    jobs = [
        {"name": "a", "status": "completed", "conclusion": "success"},
        {"name": "b", "status": "completed", "conclusion": "skipped"},
        {"name": "c", "status": "completed", "conclusion": "failure"},
        {"name": "d", "status": "completed", "conclusion": "cancelled"},
        {"name": "e", "status": "in_progress", "conclusion": None},
    ]
    assert ci_full.not_passed(jobs) == ["c", "d"]
    run = {"run_started_at": "2026-11-20T16:00:00Z", "updated_at": "2026-11-20T16:07:30Z"}
    assert ci_full.wall_seconds(run) == 450.0
