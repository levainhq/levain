"""tests/ci/junit_no_skips.py: a missing junit file says so plainly, and a present one is judged as before."""
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent / "ci" / "junit_no_skips.py"


def run(path):
    return subprocess.run([sys.executable, str(_SCRIPT), str(path)], capture_output=True, text=True)


def test_a_missing_junit_file_says_the_marker_step_never_produced_its_report(tmp_path):
    proc = run(tmp_path / "chat-egress-srt.xml")
    assert proc.returncode == 1
    out = proc.stdout + proc.stderr
    assert "never produced its report" in out
    assert "did not run or failed before writing it" in out
    assert "chat-egress-srt.xml" in out
    assert "Traceback" not in out and "FileNotFoundError" not in out


def test_a_report_with_a_pass_and_no_skips_passes(tmp_path):
    xml = tmp_path / "ok.xml"
    xml.write_text('<testsuites><testsuite><testcase classname="a" name="b"/></testsuite></testsuites>')
    proc = run(xml)
    assert proc.returncode == 0
    assert "1 passed, 0 skipped" in proc.stdout


def test_a_report_with_a_skip_fails(tmp_path):
    xml = tmp_path / "skip.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="a" name="b"/>'
        '<testcase classname="a" name="c"><skipped message="no node"/></testcase></testsuite></testsuites>'
    )
    proc = run(xml)
    assert proc.returncode == 1
    assert "SKIPPED a::c: no node" in proc.stdout
