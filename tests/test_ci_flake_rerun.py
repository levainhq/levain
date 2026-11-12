"""scripts/ci_flake_rerun.py: one rerun, only for a failure set that is entirely on the flake ledger."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_flake_rerun.py"
spec = importlib.util.spec_from_file_location("ci_flake_rerun", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

A = "tests/test_a.py::test_one"
B = "tests/test_b.py::TestX::test_two[param-1]"
C = "tests/test_c.py::test_three"

LOG_FAILED = f"""\
..F.F.
=========================== short test summary info ============================
FAILED {A} - AssertionError: boom
FAILED {B} - TimeoutError
2 failed, 4 passed in 1.2s
"""


@pytest.fixture
def run(tmp_path, monkeypatch):
    reruns = []

    def fake_run(cmd, *a, **k):
        reruns.append(cmd)
        # stand in for pytest's --junitxml: one testcase per requested node, shaped by fake_run.outcome
        xml = next((c.split("=", 1)[1] for c in cmd if c.startswith("--junitxml=")), None)
        nodes = [c for c in cmd if "::" in c]
        if xml and fake_run.outcome != "nofile":
            if fake_run.outcome == "deselected":
                nodes = nodes[:-1]
            child = {"skipped": "<skipped type='pytest.skip'/>", "xfailed": "<skipped type='pytest.xfail'/>",
                     "errored": "<error message='teardown'/>", "failed": "<failure message='x'/>"}.get(fake_run.outcome, "")
            cases = "".join(f"<testcase classname='c' name='{i}'>{child}</testcase>" for i, _ in enumerate(nodes))
            Path(xml).write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
        return subprocess.CompletedProcess(cmd, fake_run.rc)

    fake_run.rc = 0
    fake_run.outcome = "passed"
    monkeypatch.setattr(mod, "_run", fake_run)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    ledger = tmp_path / "ledger.txt"
    real_ledger_ids = mod.ledger_ids
    monkeypatch.setattr(mod, "ledger_ids", lambda: real_ledger_ids(ledger))

    def go(code, log, ledger_text=None, rerun_rc=0, outcome="passed"):
        if ledger_text is not None:
            ledger.write_text(ledger_text)
        fake_run.rc = rerun_rc
        fake_run.outcome = outcome
        logf = tmp_path / "pytest.log"
        logf.write_text(log)
        rec = tmp_path / "rec"
        rc = mod.main(["x", str(code), str(logf), str(rec), "shard 0"])
        return rc, reruns, rec

    return go


def test_parse_log_reads_failed_ids_and_errors():
    failed, errored = mod.parse_log(LOG_FAILED)
    assert failed == [A, B] and errored is False
    failed, errored = mod.parse_log(LOG_FAILED + f"ERROR {C} - fixture blew up\n")
    assert errored is True


def test_reruns_once_when_exit_1_and_every_failure_is_on_the_ledger(run):
    rc, reruns, rec = run(1, LOG_FAILED, f"# flaky\n{A}\n{B}\n")
    assert rc == 0
    assert len(reruns) == 1 and reruns[0][-2:] == [A, B]
    row = json.loads((rec / "rows.jsonl").read_text())
    assert row["tests"] == [A, B] and row["rerun_passed"] is True


def test_a_failed_rerun_fails(run):
    rc, reruns, rec = run(1, LOG_FAILED, f"{A}\n{B}\n", rerun_rc=1)
    assert rc == 1 and len(reruns) == 1
    assert json.loads((rec / "rows.jsonl").read_text())["rerun_passed"] is False


def test_one_failure_off_the_ledger_means_no_rerun(run):
    rc, reruns, rec = run(1, LOG_FAILED, f"{A}\n")
    assert rc == 1 and reruns == [] and not rec.exists()


def test_no_ledger_file_means_no_rerun(run):
    rc, reruns, rec = run(1, LOG_FAILED, None)
    assert rc == 1 and reruns == []


def test_an_error_line_means_no_rerun(run):
    rc, reruns, _ = run(1, LOG_FAILED + f"ERROR {C} - collection\n", f"{A}\n{B}\n{C}\n")
    assert rc == 1 and reruns == []


@pytest.mark.parametrize("code", [0, 2, 3, 4, 5, 137])
def test_only_exit_1_reruns(run, code):
    rc, reruns, _ = run(code, LOG_FAILED, f"{A}\n{B}\n")
    assert rc == 1 and reruns == []


def test_exit_1_with_no_failed_line_means_no_rerun(run):
    rc, reruns, _ = run(1, "1 failed in 0.1s\n", f"{A}\n")
    assert rc == 1 and reruns == []


def test_a_captured_log_line_before_the_summary_is_ignored():
    log = "ERROR    pkg:file.py:12 something logged\nFAILED tests/test_z.py::test_z - x\n" + LOG_FAILED
    assert mod.parse_log(log) == ([A, B], False)


def test_an_errors_section_with_an_item_is_errored():
    log = "==== ERRORS ====\n_ ERROR at setup of test_x _\n" + LOG_FAILED
    assert mod.parse_log(log)[1] is True
    # the section alone, with the item removed, is not enough (a bare header may be captured output)
    assert mod.parse_log("==== ERRORS ====\n" + LOG_FAILED)[1] is False


def test_a_spoofed_errors_header_after_the_summary_or_without_an_item_is_ignored():
    assert mod.parse_log(LOG_FAILED + "==== ERRORS ====\n_ ERROR at setup of t _\n") == ([A, B], False)


def test_a_spoofed_summary_header_in_captured_output_does_not_reopen_the_summary():
    spoof = "==== short test summary info ====\nERROR tests/x.py::t - spoof\nFAILED tests/z.py::z - spoof\n"
    assert mod.parse_log(spoof + "captured\n" + LOG_FAILED) == ([A, B], False)


def test_no_summary_header_and_exit_1_means_no_rerun(run):
    rc, reruns, _ = run(1, f"FAILED {A} - boom\n", f"{A}\n")
    assert rc == 1 and reruns == []


def test_a_non_node_id_failed_line_in_the_summary_fails_closed():
    log = "==== short test summary info ====\nFAILED not a node id\nFAILED " + A + "\n"
    assert mod.parse_log(log) == ([A], True)


def test_a_non_node_id_error_line_in_the_summary_fails_closed():
    log = "==== short test summary info ====\nERROR not a node id\nFAILED " + A + "\n"
    assert mod.parse_log(log) == ([A], True)


def test_a_dash_inside_brackets_is_part_of_the_id():
    nid = "tests/test_a.py::test_p[a - b]"
    log = f"==== short test summary info ====\nFAILED {nid} - AssertionError: x - y\n"
    assert mod.parse_log(log) == ([nid], False)


@pytest.mark.parametrize("reason", [
    "worker 'gw3' crashed while running 'tests/test_a.py::test_one'",
    "node down: Not properly terminated",
])
def test_an_xdist_worker_crash_is_errored_and_never_rerun(run, reason):
    log = f"==== short test summary info ====\nFAILED {A} - {reason}\n1 failed in 1s\n"
    assert mod.parse_log(log)[1] is True
    rc, reruns, rec = run(1, log, f"{A}\n")
    assert rc == 1 and reruns == [] and not rec.exists()


def test_a_crash_among_ordinary_failures_blocks_the_whole_rerun(run):
    log = (f"==== short test summary info ====\nFAILED {A} - AssertionError\n"
           f"FAILED {B} - worker 'gw0' crashed while running '{B}'\n")
    rc, reruns, _ = run(1, log, f"{A}\n{B}\n")
    assert rc == 1 and reruns == []


LONG = "tests/test_a.py::test_x[" + "-".join(["a-very-long-parametrize-id"] * 4) + "]"


@pytest.mark.parametrize("crash_lines", [
    "[gw3] node down: Not properly terminated\nreplacing crashed worker gw3\n",
    f"worker 'gw3' crashed while running '{LONG}'\n",
    f"[gw3] node down: Not properly terminated\n\nreplacing crashed worker gw3\n",
    "maximum crashed workers reached: 2\n",
    "worker gw3 crashed and worker restarting disabled\n",
])
def test_a_crash_in_the_log_body_blocks_a_rerun_even_when_the_summary_line_is_truncated(run, crash_lines):
    # pytest cuts the summary reason to the terminal width, so a long id leaves a bare FAILED line with no crash text;
    # xdist's own report lines in the body are what still say the worker died
    log = ("..F\n" + crash_lines
           + f"=== short test summary info ===\nFAILED {LONG}\n1 failed, 2 passed in 1s\n")
    assert mod.parse_log(log) == ([LONG], True)
    rc, reruns, rec = run(1, log, f"{LONG}\n")
    assert rc == 1 and reruns == [] and not rec.exists()


def test_a_bare_failed_line_with_no_crash_report_still_reruns(run):
    log = f"..F\n=== short test summary info ===\nFAILED {LONG}\n1 failed, 2 passed in 1s\n"
    assert mod.parse_log(log) == ([LONG], False)
    rc, reruns, _ = run(1, log, f"{LONG}\n")
    assert rc == 0 and len(reruns) == 1


@pytest.mark.parametrize("msg", [
    "node down: Not properly terminated",
    "worker 'gw3' crashed while running 'tests/test_a.py::test_one'",
    "replacing crashed worker gw3",
])
def test_an_assertion_message_that_merely_contains_crash_text_is_an_ordinary_failure(run, msg):
    # the text is quoted inside a failure body and a summary reason, never the start of an xdist report line
    log = (f"=== FAILURES ===\n___ test_one ___\n    def test_one():\n>       assert False, {msg!r}\n"
           f"E       AssertionError: {msg}\n\n"
           f"=== short test summary info ===\nFAILED {A} - AssertionError: {msg}\n1 failed in 1s\n")
    assert mod.parse_log(log) == ([A], False)
    rc, reruns, _ = run(1, log, f"{A}\n")
    assert rc == 0 and len(reruns) == 1


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "errored", "failed", "deselected", "nofile"])
def test_a_rerun_that_exits_zero_without_every_node_passing_is_not_a_pass(run, outcome):
    # pytest exits 0 for a skipped or xfailed node, so the exit code alone must not decide
    rc, reruns, rec = run(1, LOG_FAILED, f"{A}\n{B}\n", rerun_rc=0, outcome=outcome)
    assert rc == 1 and len(reruns) == 1
    assert json.loads((rec / "rows.jsonl").read_text())["rerun_passed"] is False


def test_the_rerun_asks_pytest_for_a_junit_file_in_the_record_dir(run):
    rc, reruns, rec = run(1, LOG_FAILED, f"{A}\n{B}\n")
    assert rc == 0 and f"--junitxml={rec / 'rerun.xml'}" in reruns[0]


@pytest.mark.parametrize("body, nodes, want", [
    ("def test_ok():\n    pass\n", ["t.py::test_ok"], True),
    ("import pytest\ndef test_s():\n    pytest.skip('gone')\n", ["t.py::test_s"], False),
    ("import pytest\n@pytest.mark.xfail\ndef test_x():\n    assert False\n", ["t.py::test_x"], False),
    ("def test_ok():\n    pass\ndef test_b():\n    pass\n", ["t.py::test_ok", "t.py::test_b"], True),
])
def test_the_junit_check_reads_what_real_pytest_writes(tmp_path, body, nodes, want):
    # real pytest exits 0 for the skip and the xfail; only the junit file tells them from a pass
    (tmp_path / "t.py").write_text(body)
    xml = tmp_path / "r.xml"
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           f"--rootdir={tmp_path}", f"--junitxml={xml}", *nodes], cwd=tmp_path, capture_output=True)
    assert proc.returncode == 0
    assert mod._rerun_all_passed(xml, len(nodes)) is want
