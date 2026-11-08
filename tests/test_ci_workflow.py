"""The shape of .github/workflows/test.yml that the main-push skip gate depends on."""
import itertools
import re
from pathlib import Path

import pytest
import yaml

WF = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml").read_text())
JOBS = WF["jobs"]
HEAVY_IF = ("${{ always() && !cancelled() && "
            "!(needs.gate.result == 'success' && needs.gate.outputs.skip == 'true') }}")


def evaluate(cond, gate_result, skip, cancelled=False):
    """Evaluate a `${{ ... }}` condition of exactly the shape the heavy jobs use: the functions always() and
    !cancelled(), string comparisons on needs.gate.result / needs.gate.outputs.skip, && || ! and parentheses.
    Anything else raises, so a new shape cannot slip past this table unevaluated."""
    m = re.fullmatch(r"\$\{\{ (.*) \}\}", cond)
    assert m, cond
    expr = m.group(1)
    expr = expr.replace("!cancelled()", "(not CANCELLED)").replace("always()", "True")
    expr = expr.replace("needs.gate.result", "RESULT").replace("needs.gate.outputs.skip", "SKIP")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    leftover = re.sub(r"\b(True|and|or|not|RESULT|SKIP|CANCELLED)\b|'[a-z]*'|==|!=|[()\s]", "", expr)
    assert leftover == "", (cond, leftover)
    return bool(eval(expr, {"__builtins__": {}}, {"RESULT": gate_result, "SKIP": skip, "CANCELLED": cancelled}))


def test_the_gate_runs_only_for_a_main_push():
    cond = JOBS["gate"]["if"]
    assert "github.event_name == 'push'" in cond and "refs/heads/main" in cond


def _heavy():
    return [n for n, j in JOBS.items() if j.get("needs") == "gate" and n != "verified"]


# (gate result, skip output, heavy job runs?). The skip output is "" when the gate did not run or failed before writing.
HEAVY_TABLE = [
    ("success", "true", False),    # the gate looked up a verifying run and said skip: the only case that skips
    ("success", "false", True),    # the gate found no verdict: run
    ("success", "", True),         # a success that wrote no skip output is not a skip: run
    ("failure", "", True),         # a failed gate (fail closed): run
    ("failure", "true", True),     # a failed gate never skips, whatever its output holds
    ("skipped", "", True),         # a non-main run has no gate: run
]


def test_there_are_heavy_jobs():
    assert len(_heavy()) >= 6


@pytest.mark.parametrize("name", _heavy())
def test_every_heavy_job_skips_only_when_the_gate_succeeded_and_said_skip(name):
    cond = JOBS[name]["if"]
    assert cond == HEAVY_IF, name
    for result, skip, runs in HEAVY_TABLE:
        assert evaluate(cond, result, skip) is runs, (name, result, skip)
    # a cancelled workflow runs nothing, whatever the gate said
    for result, skip in itertools.product(("success", "failure", "skipped", "cancelled"), ("true", "false", "")):
        assert evaluate(cond, result, skip, cancelled=True) is False, (name, result, skip)
    # a cancelled gate while the workflow is still live is not a verdict: run
    assert evaluate(cond, "cancelled", "", cancelled=False) is True, name


def test_the_verified_job_needs_a_successful_gate_that_said_skip():
    cond = JOBS["verified"]["if"]
    for result, skip, runs in HEAVY_TABLE:
        assert evaluate(cond, result, skip) is (not runs), (result, skip)


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
