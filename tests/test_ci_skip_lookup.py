"""tests/ci/skip_lookup.py: a main push skips only on a clear verdict from another ref, and fails closed otherwise."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent / "ci" / "skip_lookup.py"
_spec = importlib.util.spec_from_file_location("ci_skip_lookup", _PATH)
skip_lookup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skip_lookup)


def run(rid, branch="land/x", event="push", conclusion="success", status="completed"):
    return {"id": rid, "head_branch": branch, "event": event, "conclusion": conclusion, "status": status}


def reply(*runs, total=None):
    return {"total_count": len(runs) if total is None else total, "workflow_runs": list(runs)}


def decide(rep, event="push", ref_type="branch", ref="main", run_id="999"):
    return skip_lookup.decide(event, ref_type, ref, run_id, rep)


def test_a_main_push_skips_on_a_green_run_of_the_same_sha_elsewhere():
    assert decide(reply(run(11, "land/k"))) == (True, "land/k", "11")


def test_a_seat_branch_or_a_manual_run_counts_as_the_verdict():
    assert decide(reply(run(12, "seat/a"))) == (True, "seat/a", "12")
    assert decide(reply(run(13, "seat/a", event="workflow_dispatch"))) == (True, "seat/a", "13")


@pytest.mark.parametrize("rep", [
    reply(),                                              # nothing ran for this sha
    reply(run(1, "main")),                                # main's own earlier run is not another ref
    reply(run(1, "land/k", conclusion="failure")),        # not green
    reply(run(1, "land/k", status="in_progress", conclusion=None)),
    reply(run(1, "land/k", event="pull_request")),        # a PR run is not the landed ref's verdict
    reply(run(1, "land/k", event="schedule")),
    reply(run(999, "land/k")),                            # this very run
    reply(run(1, "refs/tags/v1")),
], ids=lambda r: "")
def test_no_clear_verdict_runs_the_suite(rep):
    assert decide(rep)[0] is False


def test_a_page_that_may_hide_runs_fails_closed():
    assert decide(reply(run(1, "main"), total=2))[0] is False


@pytest.mark.parametrize("rep", [None, {}, [], {"total_count": 1}, {"total_count": "1", "workflow_runs": []},
                                 {"total_count": 1, "workflow_runs": [{"id": 1}]},
                                 {"total_count": 1, "workflow_runs": [None]},
                                 {"total_count": True, "workflow_runs": [run(1)]}])
def test_a_malformed_reply_fails_closed(rep):
    assert decide(rep) == (False, "", "")


@pytest.mark.parametrize("kw", [dict(event="workflow_dispatch"), dict(event="schedule"), dict(event="pull_request"),
                                dict(ref_type="tag", ref="v1.0"), dict(ref="seat/x"), dict(ref="land/x")])
def test_only_a_plain_push_to_main_can_skip(kw):
    assert decide(reply(run(11, "land/k")), **kw)[0] is False


ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "abc", "GITHUB_TOKEN": "t", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main", "GITHUB_RUN_ID": "999"}


def outputs(tmp_path, env, fetcher):
    out = tmp_path / "out"
    assert skip_lookup.main({**env, "GITHUB_OUTPUT": str(out)}, fetcher) == 0
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_main_writes_the_verifying_run(tmp_path, capsys):
    assert outputs(tmp_path, ENV, lambda *a: reply(run(11, "land/k"))) == {"skip": "true", "ref": "land/k", "run_id": "11"}
    assert "verified on land/k run 11" in capsys.readouterr().out


def test_an_api_error_or_a_missing_variable_runs_the_suite(tmp_path):
    def boom(*a):
        raise OSError("api down")
    assert outputs(tmp_path, ENV, boom)["skip"] == "false"
    assert outputs(tmp_path, {k: v for k, v in ENV.items() if k != "GITHUB_TOKEN"}, lambda *a: reply(run(11)))["skip"] == "false"
