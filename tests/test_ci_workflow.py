"""The shape of .github/workflows/test.yml that the main-push skip gate depends on."""
from pathlib import Path

import yaml

WF = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml").read_text())
JOBS = WF["jobs"]
HEAVY_IF_TERMS = ("always()", "!cancelled()", "needs.gate.result == 'skipped'", "needs.gate.result == 'success'",
                  "needs.gate.outputs.skip != 'true'")


def test_the_gate_runs_only_for_a_main_push():
    cond = JOBS["gate"]["if"]
    assert "github.event_name == 'push'" in cond and "refs/heads/main" in cond


def test_every_heavy_job_runs_when_the_gate_was_skipped_or_failed():
    heavy = [n for n, j in JOBS.items() if j.get("needs") == "gate" and n != "verified"]
    assert len(heavy) >= 6
    for name in heavy:
        cond = JOBS[name]["if"]
        for term in HEAVY_IF_TERMS:
            assert term in cond, (name, term)
        # a failed gate is neither skipped nor a success-with-skip: the clause must not admit only those two by `failure`
        assert "needs.gate.result == 'failure'" not in cond, name


def test_the_verified_job_needs_a_successful_gate_that_said_skip():
    cond = JOBS["verified"]["if"]
    assert "needs.gate.result == 'success'" in cond and "needs.gate.outputs.skip == 'true'" in cond


def test_a_main_push_neither_cancels_nor_is_cancelled_by_another_main_push():
    c = WF["concurrency"]
    assert "github.sha" in c["group"] and "refs/heads/main" in c["group"]
    assert "refs/heads/main" in str(c["cancel-in-progress"])  # the flag is an expression that is false for a main push
    assert c["cancel-in-progress"] is not True


def test_no_run_line_expands_a_branch_name_or_an_api_string():
    for name, job in JOBS.items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            for bad in ("github.ref_name", "github.head_ref", "github.event.", "needs.gate.outputs", "steps.lookup.outputs"):
                assert bad not in run, (name, bad)
