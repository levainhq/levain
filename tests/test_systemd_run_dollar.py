"""systemd 259+ (Ubuntu 26.04) expands ``$`` in ``systemd-run`` command arguments: the escape is conditional
on a probe of the host (cached only when conclusive), and touches only the command words after systemd-run's ``--``."""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from levain.firing import confinement as c

_REAL_RUN = subprocess.run


@pytest.fixture(autouse=True)
def _reset():
    c._sd_expands_dollar = None
    c._sd_probe_runner = None
    yield
    c._sd_expands_dollar = None
    c._sd_probe_runner = None


def test_probe_intact_means_no_escape():
    c._sd_probe_runner = lambda: "$$"
    assert c.systemd_run_expands_dollar() is False
    assert c._escape_for_systemd_run(["bash", "-c", "echo ${A}$B"]) == ["bash", "-c", "echo ${A}$B"]


def test_probe_expanded_means_escape_dollar_only():
    c._sd_probe_runner = lambda: "$"
    assert c.systemd_run_expands_dollar() is True
    assert c._escape_for_systemd_run(["bash", "-c", "V=${BASH_VERSINFO[0]} $1 $$ a%b", "bash"]) == [
        "bash", "-c", "V=$${BASH_VERSINFO[0]} $$1 $$$$ a%b", "bash"]


@pytest.mark.parametrize("out", [None, "", "x", "garbage", "$$$"])
def test_inconclusive_probe_means_no_escape_and_is_not_cached(out):
    calls = []

    def runner():
        calls.append(1)
        return out

    c._sd_probe_runner = runner
    assert c.systemd_run_expands_dollar() is False
    assert c._escape_for_systemd_run(["$X"]) == ["$X"]
    assert c._sd_expands_dollar is None
    c.systemd_run_expands_dollar()
    assert len(calls) == 3   # every ask re-probes: the two calls and the escape's own


def test_transient_failure_then_conclusive_expansion_is_seen():
    """L3 r1 (codex MED, complement LOW-MED): one early flake must not pin the old behaviour forever."""
    answers = iter([None, "$"])
    c._sd_probe_runner = lambda: next(answers)
    assert c.systemd_run_expands_dollar() is False
    assert c.systemd_run_expands_dollar() is True
    assert c._escape_for_systemd_run(["$a"]) == ["$$a"]


def test_conclusive_probe_runs_once():
    calls = []

    def runner():
        calls.append(1)
        return "$"

    c._sd_probe_runner = runner
    for _ in range(3):
        assert c.systemd_run_expands_dollar() is True
    assert len(calls) == 1


def test_concurrent_first_callers_probe_once():
    """L3 r1 (codex, gemini, complement): the first probe is serialised, so no second probe runs beside it."""
    active, peak, calls = [0], [0], []
    gate = threading.Lock()

    def runner():
        with gate:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            calls.append(1)
        time.sleep(0.05)
        with gate:
            active[0] -= 1
        return "$"

    c._sd_probe_runner = runner
    barrier = threading.Barrier(8)
    results = []

    def worker():
        barrier.wait()
        results.append(c.systemd_run_expands_dollar())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 8
    assert peak[0] == 1 and len(calls) == 1


def test_escape_does_not_mutate_input():
    c._sd_probe_runner = lambda: "$"
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


def test_real_probe_argv_needs_no_environment(monkeypatch):
    seen = _fake_run(monkeypatch, 0, b"$", b"")
    assert c.systemd_run_expands_dollar() is True
    argv = seen[0]
    assert argv[0] == "/usr/bin/env" and argv[2:] == c._SD_PROBE_ARGV
    assert c._SD_PROBE_ARGV[-3:] == ["-c", "printf %s '$$'", "sh"]
    assert "HOME" not in " ".join(c._SD_PROBE_ARGV)


def test_real_probe_intact_output(monkeypatch):
    _fake_run(monkeypatch, 0, b"$$", b"")
    assert c.systemd_run_expands_dollar() is False


@pytest.mark.parametrize("stderr", [
    b"Failed to connect to bus\n",
    b"Invalid environment variable name evaluates to an empty string: X\n",
    "Nom de variable d'environnement invalide\n".encode(),
])
def test_real_probe_nonzero_exit_is_inconclusive_whatever_the_language(monkeypatch, stderr):
    _fake_run(monkeypatch, 1, b"", stderr)
    assert c.systemd_run_expands_dollar() is False
    assert c._sd_expands_dollar is None


def test_real_probe_missing_systemd_run(monkeypatch):
    _fake_run(monkeypatch, 0, b"", b"", exists=False)
    assert c.systemd_run_expands_dollar() is False
    assert c._sd_expands_dollar is None


def _systemd_that_runs_the_command(monkeypatch, expands: bool):
    """A stand-in systemd-run: really runs the words after the scope options with the env it was given
    (an old systemd passes them through; a 259+ one first turns ``$$`` into ``$``, the escape's rule)."""
    monkeypatch.setattr(c.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(c.os, "access", lambda p, m: True)

    def run(argv, **kw):
        cmd = argv[argv.index("/bin/sh"):]
        if expands:
            cmd = [w.replace("$$", "$") for w in cmd]
        return _REAL_RUN(cmd, stdin=kw.get("stdin"), stdout=kw.get("stdout"), stderr=kw.get("stderr"),
                         env=kw.get("env"), timeout=kw.get("timeout"))

    monkeypatch.setattr(c.subprocess, "run", run)


@pytest.mark.skipif(not os.access("/bin/sh", os.X_OK), reason="needs /bin/sh")
@pytest.mark.parametrize("expands", [False, True])
def test_probe_without_home_reads_the_host_right(monkeypatch, expands):
    """L3 r1 (codex, complement, deepseek, gemini): levain started without HOME (env -u HOME, a service
    manager) on an old systemd must not read as expanding, which would hand bash $$ for every $."""
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    monkeypatch.setattr(c, "_probe_env", lambda: env)
    _systemd_that_runs_the_command(monkeypatch, expands)
    assert c.systemd_run_expands_dollar() is expands
