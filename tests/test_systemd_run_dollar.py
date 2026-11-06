"""systemd 259+ (Ubuntu 26.04) expands ``$`` in ``systemd-run`` command arguments: the escape is conditional
on a once-per-process probe of the host, and touches only the command words after systemd-run's ``--``."""
from __future__ import annotations

import subprocess

import pytest

from levain.firing import confinement as c


@pytest.fixture(autouse=True)
def _reset():
    c._sd_expands_dollar = None
    c._sd_probe_runner = None
    yield
    c._sd_expands_dollar = None
    c._sd_probe_runner = None


def test_probe_intact_means_no_escape():
    c._sd_probe_runner = lambda: "x"
    assert c.systemd_run_expands_dollar() is False
    assert c._escape_for_systemd_run(["bash", "-c", "echo ${A}$B"]) == ["bash", "-c", "echo ${A}$B"]


@pytest.mark.parametrize("out", ["", "garbage"])
def test_probe_expanded_means_escape_dollar_only(out):
    c._sd_probe_runner = lambda: out
    assert c.systemd_run_expands_dollar() is True
    assert c._escape_for_systemd_run(["bash", "-c", "V=${BASH_VERSINFO[0]} $1 $$ a%b", "bash"]) == [
        "bash", "-c", "V=$${BASH_VERSINFO[0]} $$1 $$$$ a%b", "bash"]


def test_probe_cannot_run_means_old_behaviour():
    c._sd_probe_runner = lambda: None
    assert c.systemd_run_expands_dollar() is False
    assert c._escape_for_systemd_run(["$X"]) == ["$X"]


def test_probe_runs_once():
    calls = []

    def runner():
        calls.append(1)
        return ""

    c._sd_probe_runner = runner
    for _ in range(3):
        assert c.systemd_run_expands_dollar() is True
    assert len(calls) == 1


def test_escape_does_not_mutate_input():
    c._sd_probe_runner = lambda: ""
    words = ["$a"]
    assert c._escape_for_systemd_run(words) == ["$$a"]
    assert words == ["$a"]


def _fake_run(monkeypatch, returncode, stdout, stderr, exists=True):
    monkeypatch.setattr(c.os.path, "isfile", lambda p: exists)
    monkeypatch.setattr(c.os, "access", lambda p, m: exists)
    monkeypatch.setattr(c, "_probe_env", lambda: {})
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(c.subprocess, "run", run)
    return seen


def test_real_probe_argv_and_26_04_failure_reads_as_expansion(monkeypatch):
    seen = _fake_run(monkeypatch, 1, b"",
                     b"Invalid environment variable name evaluates to an empty string: HOME+x\n")
    assert c.systemd_run_expands_dollar() is True
    argv = seen[0]
    assert argv[0] == "/usr/bin/env" and argv[2:] == c._SD_PROBE_ARGV
    assert c._SD_PROBE_ARGV[-3:] == ["-c", 'printf %s "${HOME+x}"', "sh"]


def test_real_probe_intact_output(monkeypatch):
    _fake_run(monkeypatch, 0, b"x", b"")
    assert c.systemd_run_expands_dollar() is False


def test_real_probe_manager_refusal_is_not_evidence(monkeypatch):
    _fake_run(monkeypatch, 1, b"", b"Failed to connect to bus\n")
    assert c.systemd_run_expands_dollar() is False


def test_real_probe_missing_systemd_run(monkeypatch):
    _fake_run(monkeypatch, 0, b"", b"", exists=False)
    assert c.systemd_run_expands_dollar() is False
