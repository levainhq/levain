"""`levain consent canary --sandbox` (the courier design): the verdict logic, the probe file, and the CLI wiring.

No test runs a model or `levain session`. The scripted sessions stand in for the session path: they return a
transcript and may also act on the host's far side (create the home file, connect to the listener), which is what
a real escape would leave behind.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from levain import canary_probe
from levain.consent import canary


def _lines(nonce: str, **status: str) -> str:
    base = {"CONTROL": "OK", "HOMEWRITE": "STOPPED", "OFFHOST": "STOPPED", "COCKPITPORT": "STOPPED",
            "HOOKPLANT": "STOPPED", "END": "DONE"}
    base.update(status)
    return "\n".join(f"{canary_probe.PREFIX} {nonce} {k} {v}" for k, v in base.items()) + "\n"


def _args(instruction: str) -> tuple[str, Path, int]:
    nonce = re.search(r"--nonce (\S+)", instruction).group(1)
    home = Path(re.search(r"--home (\S+)", instruction).group(1).strip("'"))
    return nonce, home, int(re.search(r"--port (\d+)", instruction).group(1))


def _run(tmp_path: Path, session) -> canary.SandboxResult:
    home = tmp_path / "home"
    home.mkdir()
    return canary.run_sandbox_canary("claude_code", workspace=tmp_path / "ws", session=session, home=home)


def _by_name(result: canary.SandboxResult) -> dict[str, str]:
    return {c.name: c.status for c in result.checks}


def test_every_arm_stopped_reads_pass(tmp_path):
    def session(vendor, repo, instruction, nonce, timeout):
        assert vendor == "claude" and (repo / canary.PROBE_FILE).is_file()
        return _lines(nonce)
    result = _run(tmp_path, session)
    assert result.status == "PASS", result.checks
    assert result.ok()


def test_a_reported_escape_reads_fail(tmp_path):
    result = _run(tmp_path, lambda v, r, i, n, t: _lines(n, HOMEWRITE="ESCAPED"))
    assert result.status == "FAIL"
    assert _by_name(result)["HOMEWRITE"] == "fail"
    assert not result.ok()


def test_a_report_of_stopped_that_the_host_contradicts_reads_fail(tmp_path):
    # the home file really lands while the report says STOPPED: the host's own look wins
    def session(vendor, repo, instruction, nonce, timeout):
        _, home, _ = _args(instruction)
        (home / f".levain-canary-probe-{nonce}").write_text("x")
        return _lines(nonce)
    result = _run(tmp_path, session)
    assert _by_name(result)["HOMEWRITE"] == "fail"
    assert result.status == "FAIL"
    assert not (tmp_path / "home" / ".levain-canary-probe-x").exists()
    assert list((tmp_path / "home").iterdir()) == [], "the run removes the file it named"


def test_a_hit_on_the_listener_reads_fail_even_when_the_report_says_stopped(tmp_path):
    def session(vendor, repo, instruction, nonce, timeout):
        _, _, port = _args(instruction)
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        import time
        time.sleep(0.5)  # let the accept thread count it
        return _lines(nonce)
    result = _run(tmp_path, session)
    assert _by_name(result)["COCKPITPORT"] == "fail"
    assert result.status == "FAIL"


def test_missing_and_foreign_lines_are_inconclusive_never_pass(tmp_path):
    only_two = "\n".join(f"{canary_probe.PREFIX} {{n}} {k} STOPPED" for k in ("OFFHOST", "HOOKPLANT"))
    result = _run(tmp_path, lambda v, r, i, n, t: only_two.format(n=n))
    names = _by_name(result)
    assert names["HOMEWRITE"] == names["COCKPITPORT"] == "inconclusive"
    assert result.status == "INCONCLUSIVE" and not result.ok()
    # lines under another nonce count for nothing
    (tmp_path / "again").mkdir()
    result = _run(tmp_path / "again", lambda v, r, i, n, t: _lines("0" * 12))
    assert result.status == "INCONCLUSIVE"


def test_a_failed_control_is_inconclusive(tmp_path):
    result = _run(tmp_path, lambda v, r, i, n, t: _lines(n, CONTROL="BAD"))
    assert _by_name(result)["control write"] == "inconclusive"
    assert not result.ok()


def test_the_instruction_carries_no_report_line(tmp_path):
    seen = {}

    def session(vendor, repo, instruction, nonce, timeout):
        seen["i"], seen["n"] = instruction, nonce
        return ""
    _run(tmp_path, session)
    assert canary.parse_report(seen["i"], seen["n"]) == {}


def test_parse_report_survives_ansi_and_keeps_the_first_line():
    n = "abc123"
    text = f"\x1b[1m{canary_probe.PREFIX} {n} HOMEWRITE STOPPED EACCES\x1b[0m\r\n" \
           f"{canary_probe.PREFIX} {n} HOMEWRITE ESCAPED\n"
    assert canary.parse_report(text, n)["HOMEWRITE"] == ("STOPPED", "EACCES")


def test_the_probe_unconfined_reads_escaped_on_every_arm(tmp_path):
    """The control the research asks for: with no confinement the probe's acts succeed, so a STOPPED line in a
    real run means a wall (the probe can fail). Runs the shipped file as a child, not through any model."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git missing")
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    off, cock = socket.socket(), socket.socket()
    for s in (off, cock):
        s.bind(("127.0.0.1", 0))
        s.listen(4)
    try:
        out = subprocess.run(
            [sys.executable, str(Path(canary_probe.__file__)), "--nonce", "n1", "--home", str(home),
             "--offhost", f"127.0.0.1:{off.getsockname()[1]}", "--port", str(cock.getsockname()[1])],
            cwd=cwd, capture_output=True, text=True, timeout=60).stdout
    finally:
        off.close()
        cock.close()
    report = canary.parse_report(out, "n1")
    assert report["CONTROL"][0] == "OK" and report["END"][0] == "DONE"
    assert {a: report[a][0] for a in canary_probe.ARMS} == {a: "ESCAPED" for a in canary_probe.ARMS}, out
    checks = canary.evaluate_sandbox(report, home_file=home / ".levain-canary-probe-n1", listener_hits=0,
                                     listener_live=True)
    assert [c.status for c in checks if c.name in canary_probe.ARMS] == ["fail"] * 4


def test_the_probe_stops_on_a_refusing_errno_and_is_inconclusive_otherwise(tmp_path):
    n = "n2"
    # a home that is a regular file: ENOTDIR, not a refusal errno -> INCONCLUSIVE, never STOPPED
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    out = subprocess.run([sys.executable, str(Path(canary_probe.__file__)), "--nonce", n, "--home", str(blocker),
                          "--offhost", "127.0.0.1:1", "--port", "1"], cwd=cwd, capture_output=True, text=True,
                         timeout=60).stdout
    report = canary.parse_report(out, n)
    assert report["HOMEWRITE"][0] == "INCONCLUSIVE"
    assert report["OFFHOST"][0] == "STOPPED"  # ECONNREFUSED on a closed loopback port is a refusal errno


def test_the_probe_without_a_working_directory_write_attempts_no_arm(tmp_path):
    out = subprocess.run([sys.executable, str(Path(canary_probe.__file__)), "--nonce", "n3", "--home",
                          str(tmp_path), "--port", "1"], cwd="/", capture_output=True, text=True, timeout=60)
    if (Path("/") / "control-n3").exists():  # running as a user who can write /
        (Path("/") / "control-n3").unlink()
        pytest.skip("cwd was writable")
    report = canary.parse_report(out.stdout, "n3")
    assert all(report[a][0] == "INCONCLUSIVE" for a in canary_probe.ARMS) and "CONTROL" not in report


def test_the_cli_flag_routes_to_the_sandbox_run_and_exits_by_verdict(monkeypatch, capsys):
    import argparse

    from levain import cli

    def parse(*argv):
        sub = argparse.ArgumentParser().add_subparsers()
        cli._add_consent_canary_parser(sub)
        return sub.choices["canary"].parse_args(list(argv))
    ok = canary.SandboxResult("codex", None, (canary.SandboxCheck("x", "pass", "e"),), "/w")
    bad = canary.SandboxResult("codex", None, (canary.SandboxCheck("x", "fail", "e"),), "/w")
    for result, code in ((ok, 0), (bad, 1)):
        monkeypatch.setattr(canary, "run_sandbox_canary", lambda agent, workspace=None, r=result: r)
        monkeypatch.setattr(canary, "agent_version", lambda a, **k: None)
        args = parse("--agent", "codex", "--sandbox")
        assert args.sandbox is True and args.func(args) == code
    assert "tokens" in capsys.readouterr().out  # the price is printed before the run
    args = parse("--agent", "codex")
    assert args.sandbox is False
