"""The ``--setting-sources ""`` tripwire: a doctor row that notices a Claude Code version the empty source list was not
measured on, and re-runs a no-model probe (a SessionStart hook planted in a scratch project, an unreachable API)."""
from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

from levain import doctor, host_guard
from levain import setting_sources_canary as ssc

_FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, subprocess, sys, time
    argv = sys.argv[1:]
    empty = "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    mode = {mode!r}
    runs = mode == "leak" or not empty
    if mode == "dead":
        time.sleep(30)
    if runs:
        cmd = json.load(open(".claude/settings.json"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        subprocess.run(cmd, shell=True)
    time.sleep(30)
    ''')


def _fake(tmp_path: Path, mode: str) -> str:
    p = tmp_path / f"claude_{mode}"
    p.write_text(_FAKE.format(py=sys.executable, mode=mode))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run(tmp_path, mode):
    return ssc.run_probe(_fake(tmp_path, mode), ctl_timeout=8.0, min_grace=0.6)


def test_probe_held_when_only_the_control_fires(tmp_path):
    r = _run(tmp_path, "held")
    assert r.verdict == "held", r


def test_probe_leaked_when_the_empty_list_still_loads_the_project(tmp_path):
    r = _run(tmp_path, "leak")
    assert r.verdict == "leaked", r


def test_probe_inconclusive_when_the_control_never_fires(tmp_path):
    r = ssc.run_probe(_fake(tmp_path, "dead"), ctl_timeout=1.5, min_grace=0.2)
    assert r.verdict == "inconclusive", r


def _row(tmp_path, version, verdict="held"):
    calls = []

    def probe(binary):
        calls.append(binary)
        return ssc.ProbeResult(verdict, "x")
    (row,) = doctor._check_setting_sources(claude_bin="/x/claude", version=version, probe=probe)
    return row, calls


def test_measured_version_does_not_rerun_the_probe(tmp_path):
    v = ".".join(map(str, host_guard.SETTING_SOURCES_MEASURED_VERSION))
    row, calls = _row(tmp_path, v)
    assert row.ok and not row.warn and calls == []


def test_a_different_version_reruns_the_probe_and_says_so(tmp_path):
    row, calls = _row(tmp_path, "9.9.9")
    assert calls == ["/x/claude"] and row.ok and row.warn and "9.9.9" in row.detail and "measured" in row.detail


def test_a_leak_on_a_new_version_fails(tmp_path):
    row, _ = _row(tmp_path, "9.9.9", "leaked")
    assert not row.ok and "9.9.9" in row.detail


def test_inconclusive_probe_warns(tmp_path):
    row, _ = _row(tmp_path, "9.9.9", "inconclusive")
    assert row.ok and row.warn


def test_unreadable_version_warns_and_no_claude_is_neutral(monkeypatch):
    (row,) = doctor._check_setting_sources(claude_bin="/x/claude", version=None, probe=lambda b: None)
    assert row.ok and row.warn
    monkeypatch.setattr("shutil.which", lambda _n: None)
    (row,) = doctor._check_setting_sources()
    assert row.neutral
