"""The seat-dispatch hold (11-19): scripts/ci_hold_for_land.py and the `hold` job of .github/workflows/test.yml.

No network: the one subprocess call (`gh api`) is faked, and the clock and sleep are swapped for a counter.
Land runs, main, PR and schedule runs must run every heavy job exactly as before; the workflow tests prove that by
evaluating the dependency semantics of the parsed YAML for each event.
"""
import importlib.util
import itertools
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_hold_for_land", ROOT / "scripts" / "ci_hold_for_land.py")
hold = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hold)

NOW = datetime(2026, 11, 19, 23, 0, 0, tzinfo=timezone.utc)


def iso(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(branch, status, id=1, ended_ago=None, **extra):
    r = {"id": id, "head_branch": branch, "status": status, "created_at": iso(3600), "run_started_at": iso(3500)}
    if ended_ago is not None:
        r["updated_at"] = iso(ended_ago)
        r["conclusion"] = "success"
    r.update(extra)
    return r


def ids(runs):
    return [r["id"] for r in runs]


# ---- the pure function ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending"])
def test_an_active_land_run_blocks(status):
    assert ids(hold.blocking_land_runs([run("land/levain-abc", status, id=7)], NOW, 180, 99)) == [7]


def test_a_land_run_that_just_completed_blocks_inside_the_grace_and_not_after():
    assert ids(hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=60)], NOW, 180, 99)) == [2]
    assert hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=600)], NOW, 180, 99) == []
    # the edge: exactly the grace still blocks, one second more does not
    assert ids(hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=180)], NOW, 180, 99)) == [2]
    assert hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=181)], NOW, 180, 99) == []


def test_a_runner_clock_a_little_behind_the_api_still_blocks_a_just_finished_land_run():
    # the API's updated_at reads a few seconds AFTER the runner's now: still inside the grace
    assert ids(hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=-5)], NOW, 180, 99)) == [2]
    # the edge of the tolerance, and a timestamp far in the future (garbage) that does not block
    assert ids(hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=-hold.CLOCK_SKEW_S)], NOW, 180, 99)) == [2]
    assert hold.blocking_land_runs([run("land/x", "completed", id=2, ended_ago=-hold.CLOCK_SKEW_S - 1)], NOW, 180, 99) == []


@pytest.mark.parametrize("branch", ["seat/x", "landing/x", "main", "land", "seat/land/x", "x/land/y", "Land/x", "", None])
def test_only_a_branch_that_starts_with_land_slash_blocks(branch):
    assert hold.blocking_land_runs([run(branch, "in_progress")], NOW, 180, 99) == []


def test_land_levain_abc_blocks():
    assert ids(hold.blocking_land_runs([run("land/levain-abc", "queued", id=5)], NOW, 180, 99)) == [5]


def test_this_run_never_blocks_itself():
    runs = [run("land/x", "in_progress", id=42), run("land/x", "in_progress", id=43)]
    assert ids(hold.blocking_land_runs(runs, NOW, 180, 42)) == [43]
    assert ids(hold.blocking_land_runs(runs, NOW, 180, "42")) == [43]   # the env gives a string, the API an int
    assert ids(hold.blocking_land_runs(runs, NOW, 180, None)) == [42, 43]


def test_the_oldest_and_newest_of_several_are_all_reported():
    runs = [run("land/a", "completed", id=1, ended_ago=30), run("seat/b", "queued", id=2), run("land/c", "queued", id=3),
            run("land/d", "completed", id=4, ended_ago=9000)]
    assert ids(hold.blocking_land_runs(runs, NOW, 180, 99)) == [1, 3]


@pytest.mark.parametrize("odd", [
    None, 5, "land/x", [], {}, {"head_branch": "land/x"}, {"head_branch": "land/x", "status": None},
    {"head_branch": "land/x", "status": "completed"},                                   # no time at all
    {"head_branch": "land/x", "status": "completed", "updated_at": "not a time"},
    {"head_branch": "land/x", "status": "completed", "updated_at": 12345},
    {"head_branch": ["land/x"], "status": "queued"},
    {"head_branch": "land/x", "status": "weird"},
])
def test_a_missing_or_odd_field_never_raises_and_does_not_block(odd):
    assert hold.blocking_land_runs([odd], NOW, 180, 99) == []


def test_a_non_list_of_runs_never_raises():
    for bad in (None, {}, "x", 3):
        assert hold.blocking_land_runs(bad, NOW, 180, 1) == []


def test_a_completed_run_falls_back_to_the_other_timestamps():
    r = {"id": 8, "head_branch": "land/x", "status": "completed", "created_at": iso(30)}
    assert ids(hold.blocking_land_runs([r], NOW, 180, 99)) == [8]
    r = {"id": 8, "head_branch": "land/x", "status": "completed", "updated_at": "garbage", "run_started_at": iso(30)}
    assert ids(hold.blocking_land_runs([r], NOW, 180, 99)) == [8]


# ---- main(), with a fake gh and a fake clock ------------------------------------------------------------------------

class World:
    """One fake gh and one fake clock. `script` is a list of poll results: a list of runs, or an Exception to raise as
    a failed gh call, or the string 'badjson'. The last entry repeats."""

    def __init__(self, monkeypatch, script):
        self.script, self.calls, self.slept, self.t = list(script), [], [], 0.0
        monkeypatch.setattr(hold.subprocess, "run", self._run)
        monkeypatch.setattr(hold, "_monotonic", lambda: self.t)
        monkeypatch.setattr(hold, "_sleep", self._sleep)
        monkeypatch.setattr(hold, "_utcnow", lambda: NOW + timedelta(seconds=self.t))
        for k in ("HOLD_POLL_S", "HOLD_TIMEOUT_S", "HOLD_GRACE_S"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "levainhq/levain")
        monkeypatch.setenv("GITHUB_RUN_ID", "999")
        monkeypatch.setenv("GH_TOKEN", "secret-token-value")

    def _sleep(self, s):
        self.slept.append(s)
        self.t += s

    def _run(self, argv, **kw):
        self.calls.append(argv)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            return subprocess.CompletedProcess(argv, 1, "", str(item))
        if item == "badjson":
            return subprocess.CompletedProcess(argv, 0, "<html>not json", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"workflow_runs": item}), "")


def test_it_releases_at_once_when_nothing_blocks(monkeypatch, capsys):
    w = World(monkeypatch, [[run("seat/x", "in_progress"), run("main", "completed", ended_ago=5)]])
    assert hold.main() == 0
    assert w.slept == [] and len(w.calls) == 1
    assert "released: no land run" in capsys.readouterr().out
    assert w.calls[0][:2] == ["gh", "api"]
    assert w.calls[0][2] == "repos/levainhq/levain/actions/workflows/test.yml/runs?event=push&per_page=100"


def test_it_waits_then_releases_when_the_blocker_clears(monkeypatch, capsys):
    blocking = [run("land/levain-abc", "in_progress", id=77)]
    w = World(monkeypatch, [blocking, blocking, [run("land/levain-abc", "completed", id=77, ended_ago=1000)]])
    monkeypatch.setenv("HOLD_POLL_S", "10")
    assert hold.main() == 0
    out = capsys.readouterr().out
    assert w.slept == [10, 10] and len(w.calls) == 3
    assert out.count("holding: land run 77 on land/levain-abc (in_progress)") == 2
    assert out.strip().splitlines()[-1] == "released: no land run"


def test_it_keeps_holding_through_the_grace_after_a_land_run_ends(monkeypatch, capsys):
    # the run completed 100 s before the first poll: the grace (180 s) holds the dispatch 80 s more, then it releases
    w = World(monkeypatch, [[run("land/x", "completed", id=3, ended_ago=100)]])
    monkeypatch.setenv("HOLD_POLL_S", "30")
    assert hold.main() == 0
    assert sum(w.slept) >= 80 and "released: no land run" in capsys.readouterr().out


def test_it_never_counts_its_own_run(monkeypatch, capsys):
    w = World(monkeypatch, [[run("land/x", "in_progress", id=999)]])
    assert hold.main() == 0 and w.slept == []
    assert "released: no land run" in capsys.readouterr().out


def test_it_releases_on_an_api_error(monkeypatch, capsys):
    w = World(monkeypatch, [RuntimeError("HTTP 502")])
    assert hold.main() == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].startswith("released: api error")
    assert len(w.calls) == hold.API_ERRORS_TO_RELEASE and len(w.slept) == hold.API_ERRORS_TO_RELEASE - 1


def test_one_transient_api_error_does_not_release_a_blocked_hold(monkeypatch, capsys):
    blocking = [run("land/x", "in_progress", id=5)]
    w = World(monkeypatch, [blocking, RuntimeError("HTTP 502"), blocking, [run("land/x", "completed", id=5, ended_ago=1000)]])
    assert hold.main() == 0
    out = capsys.readouterr().out
    assert "api error 1, retrying" in out and out.strip().splitlines()[-1] == "released: no land run"
    assert len(w.calls) == 4


def test_it_releases_on_a_json_error(monkeypatch, capsys):
    World(monkeypatch, ["badjson"])
    assert hold.main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith("released: api error")


def test_it_releases_when_gh_is_missing(monkeypatch, capsys):
    World(monkeypatch, [[]])

    def boom(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(hold.subprocess, "run", boom)
    assert hold.main() == 0
    assert "released: api error" in capsys.readouterr().out


def test_it_releases_when_an_error_comes_after_some_waiting(monkeypatch, capsys):
    blocking = [run("land/x", "queued", id=5)]
    w = World(monkeypatch, [blocking, RuntimeError("rate limited")])
    assert hold.main() == 0
    out = capsys.readouterr().out
    assert "holding: land run 5" in out and out.strip().splitlines()[-1].startswith("released: api error")
    assert len(w.slept) == hold.API_ERRORS_TO_RELEASE


def test_it_releases_on_the_timeout_and_exits_zero(monkeypatch, capsys):
    w = World(monkeypatch, [[run("land/x", "in_progress", id=5)]])
    monkeypatch.setenv("HOLD_POLL_S", "30")
    monkeypatch.setenv("HOLD_TIMEOUT_S", "100")
    assert hold.main() == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1].startswith("released: timeout")
    assert 90 <= sum(w.slept) <= 100        # it waited, and did not run past the timeout


def test_the_defaults_are_named_constants_and_the_script_prints_no_secret(monkeypatch, capsys):
    assert (hold.DEFAULT_POLL_S, hold.DEFAULT_TIMEOUT_S) == (30, 2400)
    w = World(monkeypatch, [[run("land/x", "in_progress", id=5)]])
    monkeypatch.setenv("HOLD_TIMEOUT_S", "60")
    hold.main()
    out = capsys.readouterr().out
    assert "secret-token-value" not in out and "secret-token-value" not in " ".join(map(str, w.calls))
    assert w.slept[0] == hold.DEFAULT_POLL_S


def test_a_branch_name_with_a_newline_stays_one_log_line(monkeypatch, capsys):
    World(monkeypatch, [[run("land/x\n::error::boom", "in_progress", id=5)]])
    monkeypatch.setenv("HOLD_TIMEOUT_S", "1")
    hold.main()
    assert all(not ln.startswith("::") for ln in capsys.readouterr().out.splitlines())


# ---- the workflow ----------------------------------------------------------------------------------------------------

WF_TEXT = (ROOT / ".github" / "workflows" / "test.yml").read_text()
WF = yaml.safe_load(WF_TEXT)
JOBS = WF["jobs"]
HEAVY = ["stage0", "plan", "test", "root-py39", "linux-live", "browser", "isolation", "chat-egress-srt"]
SEAT_PREDICATE = ("github.event_name == 'workflow_dispatch' && startsWith(github.ref, 'refs/heads/') && "
                  "github.ref != 'refs/heads/main' && github.ref != 'refs/heads/land' && "
                  "!startsWith(github.ref, 'refs/heads/land/')")
HEAVY_IF = "${{ always() && !cancelled() && !(needs.gate.result == 'success' && needs.gate.outputs.skip == 'true') }}"
# the concurrency block of origin/main 7f3504b02, byte for byte: a land push, a main push, a PR and a schedule run
# must come out with the same group and the same cancel-in-progress
OLD_GROUP = ("${{ github.workflow }}-${{ github.ref }}-${{ (github.event_name == 'workflow_dispatch' || github.event_name == "
             "'schedule') && github.run_id || (github.event_name == 'push' && github.ref == 'refs/heads/main') && github.sha || 'ci' }}")
OLD_CANCEL = "${{ !(github.event_name == 'push' && github.ref == 'refs/heads/main') }}"

SEAT = ("workflow_dispatch", "refs/heads/seat/1119-51-cihold")
NOT_SEAT = [
    ("push", "refs/heads/land/levain-abc"),
    ("push", "refs/heads/main"),
    ("pull_request", "refs/pull/1/merge"),
    ("schedule", "refs/heads/main"),
    ("push", "refs/tags/v1.0"),
    ("push", "refs/heads/seat/x"),                  # a seat's own push runs no dispatch hold
    ("workflow_dispatch", "refs/heads/main"),
    ("workflow_dispatch", "refs/heads/land"),
    ("workflow_dispatch", "refs/heads/land/levain-abc"),
    ("workflow_dispatch", "refs/tags/v1.0"),
]


def _needs(name):
    n = JOBS[name].get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def _hold_if(event, ref):
    from tests.test_ci_workflow import evaluate_job_if
    return evaluate_job_if(JOBS["hold"]["if"], {}, {}, event, ref)


def test_the_hold_job_exists_with_read_only_actions_and_contents():
    j = JOBS["hold"]
    assert j["permissions"] == {"actions": "read", "contents": "read"}
    assert j["runs-on"] == "ubuntu-24.04"
    assert "needs" not in j


def test_the_hold_if_is_the_exact_text_the_matrix_uses_for_3_12_only():
    assert JOBS["hold"]["if"] == "${{ " + SEAT_PREDICATE + " }}"
    assert SEAT_PREDICATE in JOBS["test"]["strategy"]["matrix"]["python-version"]


def test_the_hold_runs_for_a_seat_dispatch_and_for_nothing_else():
    # evaluated, not pattern-matched: the `if` text goes through the same tiny GitHub-expression evaluator the other
    # workflow tests use (tests/test_ci_workflow.py evaluate_job_if), which raises on any shape it does not know
    assert _hold_if(*SEAT) is True
    assert _hold_if("workflow_dispatch", "refs/heads/anything") is True
    for event, ref in NOT_SEAT:
        assert _hold_if(event, ref) is False, (event, ref)


def test_the_hold_predicate_check_can_fail():
    # a control: the evaluator must tell a seat from a land ref, or the table above proves nothing
    from tests.test_ci_workflow import evaluate_job_if
    assert evaluate_job_if("${{ github.event_name == 'workflow_dispatch' && !startsWith(github.ref, 'refs/heads/land/') }}",
                           {}, {}, "workflow_dispatch", "refs/heads/land/x") is False
    assert evaluate_job_if("${{ github.event_name == 'workflow_dispatch' && !startsWith(github.ref, 'refs/heads/land/') }}",
                           {}, {}, "workflow_dispatch", "refs/heads/seat/x") is True


def test_the_hold_job_runs_the_script_with_the_job_token_after_a_sparse_checkout():
    steps = JOBS["hold"]["steps"]
    assert steps[0]["uses"] == JOBS["stage0"]["steps"][0]["uses"]        # the same pinned actions/checkout the file uses
    assert steps[0]["with"]["sparse-checkout"] == "scripts/ci_hold_for_land.py"
    runs = [s for s in steps if "run" in s]
    assert len(runs) == 1 and runs[0]["run"] == "python3 scripts/ci_hold_for_land.py"
    assert runs[0]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "secrets." not in json.dumps(JOBS["hold"])


def test_the_hold_is_fail_open_in_the_workflow_too():
    j = JOBS["hold"]
    assert j["continue-on-error"] is True                  # a hold that errors never turns the run red
    assert [s.get("continue-on-error") for s in j["steps"] if "run" in s] == [True]
    # the job's timeout sits above the script's: the script releases itself, the job timeout never fires first
    assert j["timeout-minutes"] * 60 > hold.DEFAULT_TIMEOUT_S


@pytest.mark.parametrize("name", HEAVY)
def test_each_heavy_job_needs_the_hold_and_keeps_its_always_guard(name):
    assert _needs(name) == ["gate", "hold"], name
    assert "always()" in JOBS[name]["if"] and "!cancelled()" in JOBS[name]["if"], name
    assert JOBS[name]["if"] == HEAVY_IF, name              # untouched by this change
    assert "hold" not in JOBS[name]["if"], name


def test_no_other_job_waits_on_the_hold_and_the_hold_waits_on_nothing():
    assert sorted(n for n in JOBS if "hold" in _needs(n)) == sorted(HEAVY)
    assert _needs("verified") == ["gate"]


def _heavy_runs(event, ref, gate, hold_result, skip="", workflow_cancelled=False):
    """Which heavy jobs run: each job's own `if` over the needed jobs' results, as GitHub evaluates it."""
    from tests.test_ci_workflow import evaluate_job_if
    results = {"gate": gate, "hold": hold_result}
    outputs = {"gate": skip}
    return {n: evaluate_job_if(JOBS[n]["if"], results, outputs, event, ref, cancelled=workflow_cancelled) for n in HEAVY}


@pytest.mark.parametrize("event,ref", NOT_SEAT)
def test_every_heavy_job_still_runs_with_the_hold_skipped_on_every_other_event(event, ref):
    # THE exact-SHA rule: a land push, a main push, a PR and the nightly skip `hold`, and every heavy job still runs
    assert _hold_if(event, ref) is False
    gate = "skipped" if not (event == "push" and ref == "refs/heads/main") else "success"
    skip = "false" if gate == "success" else ""        # a main push whose gate found no verdict runs everything
    assert all(_heavy_runs(event, ref, gate, "skipped", skip).values()), (event, ref)


def test_a_main_push_the_gate_skipped_still_skips_the_heavy_jobs_with_the_hold_skipped():
    assert not any(_heavy_runs("push", "refs/heads/main", "success", "skipped", "true").values())


@pytest.mark.parametrize("hold_result", ["success", "skipped", "failure", "cancelled"])
def test_a_seat_dispatch_runs_every_heavy_job_whatever_the_hold_ended_as(hold_result):
    # fail open: a hold that errored, was skipped or was cancelled alone never skips a heavy job
    assert all(_heavy_runs(*SEAT, "skipped", hold_result).values()), hold_result


def test_a_cancelled_workflow_runs_no_heavy_job_with_or_without_the_hold():
    for event, ref in NOT_SEAT + [SEAT]:
        for hold_result in ("success", "skipped", "cancelled"):
            assert not any(_heavy_runs(event, ref, "skipped", hold_result, workflow_cancelled=True).values())


def test_the_hold_without_an_always_guard_would_skip_the_heavy_jobs_and_the_check_sees_it():
    # a control, the trap of the file's history: a skipped need skips its dependents unless the `if` carries always()
    from tests.test_ci_workflow import evaluate_job_if, simulate
    jobs = yaml.safe_load(yaml.safe_dump(JOBS))
    jobs["test"]["if"] = "${{ needs.gate.result != 'success' || needs.gate.outputs.skip != 'true' }}"
    results = simulate("push", "refs/heads/land/x", jobs=jobs)
    assert results["hold"] == "skipped" and results["test"] == "skipped"
    assert evaluate_job_if(JOBS["test"]["if"], {"gate": "skipped", "hold": "skipped"}, {"gate": ""}, "push",
                           "refs/heads/land/x") is True


def _group(event, ref, run_id="123", sha="abc"):
    """Evaluate the concurrency.group expression: three ${{ }} parts joined by '-', the last one && / || over the event."""
    text = WF["concurrency"]["group"]
    parts = re.findall(r"\$\{\{ (.*?) \}\}", text)
    assert text == "${{ %s }}-${{ %s }}-${{ %s }}" % tuple(parts), text
    env = {"EV": event, "REF": ref, "ID": run_id, "SHA": sha, "WF": "test"}

    def ev(expr):
        py = (expr.replace("github.workflow", "WF").replace("github.event_name", "EV").replace("github.ref", "REF")
              .replace("github.run_id", "ID").replace("github.sha", "SHA").replace("&&", " and ").replace("||", " or "))
        assert not re.sub(r"\b(EV|REF|ID|SHA|WF|and|or)\b|'[^']*'|==|[()\s]", "", py), py
        return eval(py, {"__builtins__": {}}, env)

    return "-".join(str(ev(p)) for p in parts)


def test_the_concurrency_block_is_byte_identical_to_main():
    # the serialize half (a `seat-dispatch` group with queue: max) was NOT built: see return_G.md for why
    assert WF["concurrency"]["group"] == OLD_GROUP
    assert WF["concurrency"]["cancel-in-progress"] == OLD_CANCEL
    assert set(WF["concurrency"]) == {"group", "cancel-in-progress"}


@pytest.mark.parametrize("event,ref,want", [
    ("push", "refs/heads/land/levain-abc", "test-refs/heads/land/levain-abc-ci"),
    ("push", "refs/heads/main", "test-refs/heads/main-abc"),
    ("pull_request", "refs/pull/1/merge", "test-refs/pull/1/merge-ci"),
    ("schedule", "refs/heads/main", "test-refs/heads/main-123"),
    ("workflow_dispatch", "refs/heads/seat/x", "test-refs/heads/seat/x-123"),
])
def test_the_group_each_event_gets_is_what_it_got_before(event, ref, want):
    assert _group(event, ref) == want
