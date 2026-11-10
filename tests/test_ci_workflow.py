"""The shape of .github/workflows/test.yml that the main-push skip gate depends on."""
import itertools
import re
from pathlib import Path

import pytest
import yaml

WF = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github" / "workflows" / "test.yml").read_text())
JOBS = WF["jobs"]
GATE_CLAUSE = "!(needs.gate.result == 'success' && needs.gate.outputs.skip == 'true')"
# stage0 (the invariant tests) skips only where the gate skips everything.
STAGE0_IF = "${{ always() && !cancelled() && " + GATE_CLAUSE + " }}"
# The jobs that need both the gate and stage0 run only on a stage0 that SUCCEEDED, besides the gate clause.
HEAVY_IF = "${{ always() && !cancelled() && needs.stage0.result == 'success' && " + GATE_CLAUSE + " }}"


def evaluate(cond, gate_result, skip, cancelled=False, stage0="success"):
    """Evaluate a `${{ ... }}` condition of exactly the shape the heavy jobs use: the functions always() and
    !cancelled(), string comparisons on needs.gate.result / needs.gate.outputs.skip / needs.stage0.result, && || !
    and parentheses. Anything else raises, so a new shape cannot slip past this table unevaluated."""
    m = re.fullmatch(r"\$\{\{ (.*) \}\}", cond)
    assert m, cond
    expr = m.group(1)
    expr = expr.replace("!cancelled()", "(not CANCELLED)").replace("always()", "True")
    expr = expr.replace("needs.gate.result", "RESULT").replace("needs.gate.outputs.skip", "SKIP")
    expr = expr.replace("needs.stage0.result", "STAGE0")
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    leftover = re.sub(r"\b(True|and|or|not|RESULT|SKIP|STAGE0|CANCELLED)\b|'[a-z]*'|==|!=|[()\s]", "", expr)
    assert leftover == "", (cond, leftover)
    return bool(eval(expr, {"__builtins__": {}}, {"RESULT": gate_result, "SKIP": skip, "CANCELLED": cancelled,
                                                  "STAGE0": stage0}))


def test_the_gate_runs_only_for_a_main_push():
    cond = JOBS["gate"]["if"]
    assert "github.event_name == 'push'" in cond and "refs/heads/main" in cond


def _needs(name, jobs=None):
    needs = (jobs or JOBS)[name].get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _heavy():
    """The jobs fed directly by the gate and stage0: every one must carry the stage0 dependency and guard."""
    return [n for n, j in JOBS.items() if set(_needs(n)) == {"gate", "stage0"}]


# (gate result, skip output, heavy job runs?). The skip output is "" when the gate did not run or failed before writing.
HEAVY_TABLE = [
    ("success", "true", False),    # the gate looked up a verifying run and said skip: the only case that skips
    ("success", "false", True),    # the gate found no verdict: run
    ("success", "", True),         # a success that wrote no skip output is not a skip: run
    ("failure", "", True),         # a failed gate (fail closed): run
    ("failure", "true", True),     # a failed gate never skips, whatever its output holds
    ("skipped", "", True),         # a non-main run has no gate: run
]


STAGE0_RESULTS = ("success", "failure", "cancelled", "skipped")


def test_there_are_heavy_jobs():
    assert len(_heavy()) >= 6
    # the jobs the move names; a heavy job that dropped its stage0 dependency falls out of _heavy(), so name them
    for n in ("plan", "root-py39", "linux-live", "browser", "isolation", "chat-egress-srt"):
        assert n in _heavy(), n


def test_stage0_runs_the_invariant_tests_alone_behind_the_gate():
    job = JOBS["stage0"]
    assert _needs("stage0") == ["gate"]
    assert job["if"] == STAGE0_IF
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "-m invariant" in runs


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


@pytest.mark.parametrize("name", _heavy() + ["test"])
def test_a_heavy_job_runs_only_after_a_successful_stage0(name):
    assert "stage0" in _needs(name), name
    cond = JOBS[name]["if"]
    assert "needs.stage0.result == 'success'" in cond, name
    for stage0 in STAGE0_RESULTS:
        for result, skip, _ in HEAVY_TABLE:
            if name == "test":
                continue  # `test` also needs plan; covered by the simulation below
            assert evaluate(cond, result, skip, stage0=stage0) is (stage0 == "success" and evaluate(cond, result, skip)), \
                (name, stage0, result, skip)


@pytest.mark.parametrize("stage0", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("event,ref", [("push", "refs/heads/seat/x"), ("push", "refs/heads/main")])
def test_a_stage0_that_did_not_succeed_stops_every_heavy_job(stage0, event, ref):
    results = simulate(event, ref, "success", "false", stage0_outcome=stage0)
    suite = [n for n in JOBS if n not in NO_TESTS and n != "stage0"]
    assert [n for n in suite if results[n] != "skipped"] == [], (stage0, event, ref)


def test_the_stage0_checks_can_fail():
    # Controls: drop the stage0 guard from one job, or its dependency from another, and the checks above must go red.
    saved = {n: (JOBS[n]["if"], JOBS[n]["needs"]) for n in ("root-py39", "isolation")}
    try:
        JOBS["root-py39"]["if"] = HEAVY_IF.replace("needs.stage0.result == 'success' && ", "")
        assert simulate("push", "refs/heads/seat/x", stage0_outcome="failure")["root-py39"] == "success"
        assert "needs.stage0.result == 'success'" not in JOBS["root-py39"]["if"]
        JOBS["isolation"]["needs"] = "gate"
        assert "isolation" not in _heavy() and "stage0" not in _needs("isolation")
    finally:
        for n, (i, nd) in saved.items():
            JOBS[n]["if"], JOBS[n]["needs"] = i, nd


def test_the_verified_job_needs_a_successful_gate_that_said_skip():
    cond = JOBS["verified"]["if"]
    for result, skip, runs in HEAVY_TABLE:
        assert evaluate(cond, result, skip) is (not runs), (result, skip)


# Jobs that run no suite. `gate` is the skip-lookup job (it only decides); `verified` exists only when the gate said skip.
NO_TESTS = {"gate": "the skip lookup: decides, runs no tests", "verified": "runs only when the gate skipped the suite"}

STATUS_FN = re.compile(r"\b(always|cancelled|success|failure)\(\)")


def evaluate_job_if(cond, results, outputs, event, ref, cancelled=False):
    """Evaluate one job's `if` the way GitHub does: the expression may use always(), !cancelled(), github.event_name,
    github.ref and needs.<job>.result / needs.<job>.outputs.skip. Anything else raises. With no status function in the
    expression GitHub prepends an implicit success(), which is false when any needed job did not succeed."""
    expr = cond
    m = re.fullmatch(r"\$\{\{ (.*) \}\}", cond) if cond else None
    if cond:
        assert m, cond
        expr = m.group(1)
    else:
        expr = ""
    py = expr.replace("!cancelled()", "(not CANCELLED)").replace("always()", "True")
    py = re.sub(r"needs\.([\w-]+)\.result", lambda g: "R[%r]" % g.group(1), py)
    py = re.sub(r"needs\.([\w-]+)\.outputs\.skip", lambda g: "O[%r]" % g.group(1), py)
    py = py.replace("github.event_name", "EV").replace("github.ref", "REF")
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    leftover = re.sub(r"\b(True|and|or|not|EV|REF|CANCELLED)\b|[RO]\['[\w-]+'\]|'[^']*'|==|!=|[()\s]", "", py)
    assert leftover == "", (cond, leftover)
    if py.strip():
        value = bool(eval(py, {"__builtins__": {}}, {"R": results, "O": outputs, "EV": event, "REF": ref,
                                                     "CANCELLED": cancelled}))
    else:
        value = True
    return value


def _ancestors(name, jobs=None):
    """Every job `name` depends on, directly or through other jobs' `needs`."""
    found, stack = set(), list(_needs(name, jobs))
    while stack:
        d = stack.pop()
        if d not in found:
            found.add(d)
            stack.extend(_needs(d, jobs))
    return found


def _topological(jobs=None):
    order, seen = [], set()

    def visit(n):
        if n in seen:
            return
        seen.add(n)
        for d in _needs(n, jobs):
            visit(d)
        order.append(n)

    for n in jobs or JOBS:
        visit(n)
    return order


def simulate(event, ref, gate_outcome=None, skip="", cancelled=False, jobs=None, stage0_outcome="success"):
    """Propagate job results through `needs` in topological order. A job whose condition holds RUNS and succeeds (the
    suite is assumed green: this tests whether the job runs, not whether the code passes). The gate's outcome is only
    consulted when its own `if` lets it run. Returns {job: 'success' | 'skipped' | gate_outcome}.
    The implicit success() of a job whose `if` has no status function looks at EVERY ancestor in its needs chain, not
    only its direct needs: run 42524643315 skipped the pytest matrix while its direct need (`plan`) succeeded, because
    `plan`'s own need (`gate`) was skipped."""
    jobs = jobs or JOBS
    results, outputs = {}, {}
    for n in _topological(jobs):
        cond = jobs[n].get("if")
        expr = cond or ""
        has_status_fn = bool(STATUS_FN.search(expr))
        implicit = has_status_fn or (not cancelled and all(results[d] == "success" for d in _ancestors(n, jobs)))
        runs = implicit and evaluate_job_if(cond, results, outputs, event, ref, cancelled)
        if not runs:
            results[n] = "skipped"
        elif n == "gate":
            results[n] = gate_outcome
            outputs[n] = skip
        elif n == "stage0":
            results[n] = stage0_outcome
        else:
            results[n] = "success"
    return results


# Every kind of run that is not a push to main. The gate's own `if` is false for all of them, so it is skipped.
NON_MAIN = [
    ("push", "refs/heads/seat/x"),
    ("push", "refs/heads/land/x"),
    ("push", "refs/tags/v1.0"),
    ("pull_request", "refs/pull/1/merge"),
    ("schedule", "refs/heads/main"),
    ("workflow_dispatch", "refs/heads/seat/x"),
]
SUITE_JOBS = [n for n in JOBS if n not in NO_TESTS]


def test_there_are_suite_jobs():
    assert "test" in SUITE_JOBS and len(SUITE_JOBS) >= 7


@pytest.mark.parametrize("event,ref", NON_MAIN)
def test_every_suite_job_runs_on_a_run_that_is_not_a_main_push(event, ref):
    # The gate is skipped here, and a job without a status function in its `if` inherits the skip through `needs`
    # (run 42524643315: a seat/** run went green without the suite). Checked as behaviour, not as text.
    results = simulate(event, ref)
    assert results["gate"] == "skipped", (event, ref)
    assert results["verified"] == "skipped", (event, ref)
    skipped = [n for n in SUITE_JOBS if results[n] != "success"]
    assert skipped == [], (event, ref, skipped)


@pytest.mark.parametrize("outcome,skip", [("success", "false"), ("success", ""), ("failure", ""), ("failure", "true")])
def test_every_suite_job_runs_on_a_main_push_the_gate_did_not_skip(outcome, skip):
    results = simulate("push", "refs/heads/main", outcome, skip)
    assert results["gate"] == outcome
    assert results["verified"] == "skipped"
    assert [n for n in SUITE_JOBS if results[n] != "success"] == []


def test_a_main_push_the_gate_skipped_runs_verified_and_no_suite_job():
    results = simulate("push", "refs/heads/main", "success", "true")
    assert results["verified"] == "success"
    assert [n for n in SUITE_JOBS if results[n] != "skipped"] == []


def test_a_cancelled_workflow_runs_no_suite_job():
    for event, ref in NON_MAIN + [("push", "refs/heads/main")]:
        results = simulate(event, ref, "success", "false", cancelled=True)
        assert [n for n in SUITE_JOBS if results[n] != "skipped"] == [], (event, ref)


def test_the_simulation_detects_a_job_that_would_inherit_the_skip():
    # A control: the same propagation must flag a job whose `if` has no status function, or the tests above prove nothing.
    saved = JOBS["plan"]["if"]
    try:
        JOBS["plan"]["if"] = "${{ always() && needs.gate.result == 'success' }}"
        assert simulate("push", "refs/heads/seat/x")["test"] == "skipped"
        JOBS["plan"]["if"] = "${{ needs.gate.result != 'cancelled' }}"
        assert simulate("push", "refs/heads/seat/x")["plan"] == "skipped"
    finally:
        JOBS["plan"]["if"] = saved


def _fixture_jobs(name):
    return yaml.safe_load((Path(__file__).resolve().parent / "ci" / name).read_text())["jobs"]


def _suite_skipped_on_seat_push(jobs):
    results = simulate("push", "refs/heads/seat/x", jobs=jobs)
    return [n for n in jobs if n not in NO_TESTS and results[n] != "success"]


def test_the_workflow_as_run_in_42524643315_is_caught():
    # The `test` job needed `plan` (success) but `plan` needed the skipped `gate`, so the matrix was skipped.
    assert _suite_skipped_on_seat_push(_fixture_jobs("workflow_before_always_guard.yml")) == ["test"]


def test_the_workflow_with_the_always_guard_on_test_is_clean():
    assert _suite_skipped_on_seat_push(_fixture_jobs("workflow_with_always_guard.yml")) == []


def test_a_plan_refactored_to_drop_always_is_caught():
    jobs = yaml.safe_load(yaml.safe_dump(_fixture_jobs("workflow_with_always_guard.yml")))
    jobs["plan"]["if"] = "${{ needs.gate.result != 'success' || needs.gate.outputs.skip != 'true' }}"
    assert "plan" in _suite_skipped_on_seat_push(jobs)


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
