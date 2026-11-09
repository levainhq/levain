"""The ``--setting-sources ""`` tripwire: a doctor row that notices a Claude Code version the empty source list was not
measured on, and re-runs a no-model probe (a SessionStart hook planted in a scratch project, a local API listener).
Every probe test drives a FAKE claude written here; the real binary is never run."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from levain import doctor, host_guard
from levain import setting_sources_canary as ssc

_FAKE = textwrap.dedent('''\
    #!{py}
    import json, os, socket, subprocess, sys, time
    from urllib.parse import urlsplit
    argv = sys.argv[1:]
    empty = "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == ""
    role = "empty" if empty else "ctl"
    mode = {mode!r}
    side = {side!r}
    open(os.path.join(side, "home_" + role), "w").write(os.environ.get("HOME", ""))

    def hook():
        cmd = json.load(open(".claude/settings.json"))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        subprocess.run(cmd, shell=True)

    held_open = []

    def send(line):
        u = urlsplit(os.environ.get("ANTHROPIC_BASE_URL", ""))
        try:
            s = socket.create_connection((u.hostname, u.port), timeout=2)
            s.sendall(line + b"\\r\\nHost: x\\r\\n\\r\\n")
            held_open.append(s)
        except OSError:
            pass

    def api():
        send(b"POST /v1/messages?beta=true HTTP/1.1")

    # the real binary's measured order: a HEAD preflight before any hook, the model request well after the hooks
    parse_fails = (role, mode) in (("empty", "reject"), ("empty", "noisy_reject"), ("ctl", "ctl_exit"),
                                   ("ctl", "ctl_exit_empty_leak"))
    if not parse_fails:
        send(b"HEAD /api/hello HTTP/1.1")
        time.sleep(0.2)
    if mode == "preflight_only":
        if role == "ctl":
            hook()
        time.sleep(60)

    if role == "ctl":
        if mode in ("leak_only", "hang_empty_and_ctl"):
            time.sleep(60)
        if mode in ("ctl_exit", "ctl_exit_empty_leak"):
            sys.stderr.write("ctl boom\\n")
            sys.exit(3)
        if mode == "ctl_exit_orphan":
            child = subprocess.Popen(["sleep", "60"])
            open(os.path.join(side, "child.pid"), "w").write(str(child.pid))
            sys.exit(3)
        if mode in ("ctl_api_first", "ctl_api_first_leak"):
            api()
            time.sleep(0.5)
            hook()
            time.sleep(60)
        hook()
        if mode != "ctl_tight":
            time.sleep(0.2)
        api()
        time.sleep(60)
    if mode == "reject":
        sys.stderr.write("error: option '--setting-sources <sources>' argument missing\\n")
        sys.exit(2)
    if mode == "noisy_reject":
        for _ in range(200):
            sys.stderr.write("banner " * 150 + "\\n")
        sys.stderr.write("error: option '--setting-sources <sources>' argument missing\\n")
        sys.exit(2)
    if mode == "ctl_exit_empty_leak":
        time.sleep(0.05)
        hook()
    if mode == "ctl_api_first_leak":
        time.sleep(0.1)
        hook()
    if mode == "post_then_leak":
        time.sleep(0.3)
        api()
        time.sleep(0.15)
        hook()
        time.sleep(60)
    if mode == "crash":
        time.sleep(0.3)
        sys.exit(1)
    if mode == "hang_empty":
        time.sleep(60)
    if mode == "orphan":
        child = subprocess.Popen(["sleep", "60"])
        open(os.path.join(side, "child.pid"), "w").write(str(child.pid))
    if mode == "slow_leak":
        time.sleep(1.5)
    if mode in ("leak", "leak_only", "slow_leak"):
        hook()
    api()
    time.sleep(60)
    ''')


def _fake(tmp_path: Path, mode: str) -> str:
    side = tmp_path / "side"
    side.mkdir(exist_ok=True)
    p = tmp_path / f"claude_{mode}"
    p.write_text(_FAKE.format(py=sys.executable, mode=mode, side=str(side)))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _probe(binary: str, timeout: float) -> ssc.ProbeResult:
    return ssc.run_probe(binary, timeout=timeout)


def _run(tmp_path, mode, timeout=8.0):
    t0 = time.monotonic()
    r = _probe(_fake(tmp_path, mode), timeout)
    return r, time.monotonic() - t0


def test_probe_held_when_only_the_control_fires(tmp_path):
    r, _ = _run(tmp_path, "held")
    assert r.verdict == "held", r


def test_probe_leaked_when_the_empty_list_still_loads_the_project(tmp_path):
    r, _ = _run(tmp_path, "leak")
    assert r.verdict == "leaked", r


def test_probe_inconclusive_when_the_control_never_fires(tmp_path):
    r, _ = _run(tmp_path, "hang_empty_and_ctl", timeout=1.5)
    assert r.verdict == "inconclusive", r


def test_a_rejected_flag_is_inconclusive_never_held_and_carries_stderr(tmp_path):
    r, _ = _run(tmp_path, "reject")
    assert r.verdict == "inconclusive", r
    assert "argument missing" in r.detail and "2" in r.detail, r


def test_an_empty_run_crash_before_its_api_call_is_inconclusive(tmp_path):
    r, _ = _run(tmp_path, "crash")
    assert r.verdict == "inconclusive", r


def test_an_empty_run_that_never_reaches_its_api_call_is_inconclusive_at_the_timeout(tmp_path):
    r, _ = _run(tmp_path, "hang_empty", timeout=3.0)
    assert r.verdict == "inconclusive", r


def test_a_slow_empty_run_that_leaks_is_leaked_not_timed_out_as_held(tmp_path):
    r, _ = _run(tmp_path, "slow_leak")
    assert r.verdict == "leaked", r


def test_a_leak_while_the_control_never_fires_is_still_leaked(tmp_path):
    r, _ = _run(tmp_path, "leak_only", timeout=4.0)
    assert r.verdict == "leaked", r


def test_a_control_that_exits_early_is_inconclusive_at_once(tmp_path):
    r, took = _run(tmp_path, "ctl_exit", timeout=8.0)
    assert r.verdict == "inconclusive" and "ctl boom" in r.detail, r
    assert took < 4.0, took


def test_a_control_that_reaches_its_api_before_its_hook_is_inconclusive(tmp_path):
    r, _ = _run(tmp_path, "ctl_api_first")
    assert r.verdict == "inconclusive", r


def test_a_preflight_alone_is_not_the_api_call_so_it_is_inconclusive_never_held(tmp_path):
    r, _ = _run(tmp_path, "preflight_only", timeout=2.0)
    assert r.verdict == "inconclusive", r


def test_held_names_the_model_request_as_its_evidence(tmp_path):
    r, _ = _run(tmp_path, "held")
    assert r.verdict == "held" and "/v1/messages" in r.detail, r


def test_each_run_gets_its_own_home(tmp_path):
    r, _ = _run(tmp_path, "held")
    side = tmp_path / "side"
    homes = {(side / "home_ctl").read_text(), (side / "home_empty").read_text()}
    assert r.verdict == "held" and len(homes) == 2, (r, homes)


def test_the_whole_process_group_is_killed(tmp_path):
    r, _ = _run(tmp_path, "orphan")
    pid = int((tmp_path / "side" / "child.pid").read_text())
    deadline = time.monotonic() + 3
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
    assert not alive, (r, pid)


def test_a_temp_dir_failure_is_inconclusive(monkeypatch):
    def boom(**_kw):
        raise OSError("no space")
    monkeypatch.setattr(ssc.tempfile, "mkdtemp", boom)
    r = ssc.run_probe("/nonexistent/claude", timeout=1.0)
    assert r.verdict == "inconclusive" and "no space" in r.detail, r


def test_a_leak_landing_just_after_the_control_exits_is_leaked_not_inconclusive(tmp_path):
    r, _ = _run(tmp_path, "ctl_exit_empty_leak")
    assert r.verdict == "leaked", r


def test_a_leak_landing_just_after_a_misordered_control_is_leaked_not_inconclusive(tmp_path):
    r, _ = _run(tmp_path, "ctl_api_first_leak")
    assert r.verdict == "leaked", r


def test_a_control_whose_hook_barely_precedes_its_request_does_not_validate_the_order(tmp_path):
    r, _ = _run(tmp_path, "ctl_tight")
    assert r.verdict == "inconclusive", r


def test_a_hook_landing_just_after_the_empty_runs_request_is_leaked_not_held(tmp_path):
    r, _ = _run(tmp_path, "post_then_leak")
    assert r.verdict == "leaked", r


def test_stderr_keeps_its_last_bytes_and_never_lands_on_disk(tmp_path, monkeypatch):
    root = tmp_path / "probe-root"
    root.mkdir()
    monkeypatch.setattr(ssc.tempfile, "mkdtemp", lambda **_kw: str(root))
    monkeypatch.setattr(ssc.shutil, "rmtree", lambda *_a, **_kw: None)
    r, _ = _run(tmp_path, "noisy_reject")
    assert r.verdict == "inconclusive" and "argument missing" in r.detail, r
    big = [p for p in root.rglob("*") if p.is_file() and p.stat().st_size > 4096]
    assert big == [], big


def test_each_process_group_is_signalled_at_most_once(tmp_path, monkeypatch):
    sent = []
    real = os.killpg

    def killpg(pid, sig):
        sent.append(pid)
        real(pid, sig)
    monkeypatch.setattr(ssc.os, "killpg", killpg)
    r, _ = _run(tmp_path, "held")
    assert r.verdict == "held" and len(sent) == len(set(sent)) == 2, (r, sent)


def test_a_binary_popen_rejects_is_inconclusive_never_a_crash(tmp_path):
    r = ssc.run_probe(str(tmp_path / "cla\0ude"), timeout=1.0)
    assert r.verdict == "inconclusive", r


def test_teardown_waits_a_short_fixed_time_for_each_leader(tmp_path, monkeypatch):
    waits = []
    real = subprocess.Popen.wait

    def wait(self, timeout=None):
        waits.append(timeout)
        return real(self, timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    r, _ = _run(tmp_path, "held")
    assert r.verdict == "held" and waits and all(w is not None and w <= 1.0 for w in waits), (r, waits)


def test_a_hung_probe_stops_within_its_timeout_plus_its_stated_overrun(tmp_path):
    r, took = _run(tmp_path, "hang_empty", timeout=1.5)
    assert r.verdict == "inconclusive" and took <= 1.5 + ssc.OVERRUN + 0.5, (r, took)


def test_a_stderr_pipe_that_never_runs_dry_cannot_hold_the_probe_past_its_timeout(tmp_path, monkeypatch):
    procs: list = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            procs.append(self)
    monkeypatch.setattr(ssc.subprocess, "Popen", Recording)
    real_read = os.read
    give_up = time.monotonic() + 1.5 + ssc.OVERRUN + 3.0  # past this the pipe runs dry, so a hang still ends

    def read(fd, n):
        if time.monotonic() < give_up and any(p.stderr is not None and not p.stderr.closed and
                                              p.stderr.fileno() == fd for p in procs):
            return b"x" * n
        return real_read(fd, n)
    monkeypatch.setattr(ssc.os, "read", read)
    r, took = _run(tmp_path, "hang_empty", timeout=1.5)
    assert r.verdict == "inconclusive" and took <= 1.5 + ssc.OVERRUN + 0.5, (r, took)


def test_a_leader_reaped_early_is_never_signalled_by_number_and_its_grandchild_still_dies(tmp_path, monkeypatch):
    procs: list = []
    real_popen = subprocess.Popen

    class Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            procs.append(self)
    monkeypatch.setattr(ssc.subprocess, "Popen", Recording)
    signalled = []
    real_killpg = os.killpg

    def killpg(pgid, sig):
        leader = [p for p in procs if p.pid == pgid]
        signalled.append((pgid, leader[0].returncode if leader else "no leader"))
        real_killpg(pgid, sig)
    monkeypatch.setattr(ssc.os, "killpg", killpg)
    r, _ = _run(tmp_path, "ctl_exit_orphan")
    assert r.verdict == "inconclusive", r
    assert signalled and all(code is None for _pgid, code in signalled), signalled
    pid = int((tmp_path / "side" / "child.pid").read_text())
    deadline = time.monotonic() + 3
    alive = True
    while alive and time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            alive = False
    assert not alive, (r, pid)


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


def test_an_unreadable_version_still_runs_the_probe_and_a_leak_fails(tmp_path):
    row, calls = _row(tmp_path, None, "leaked")
    assert calls == ["/x/claude"] and not row.ok and "not readable" in row.detail


def test_an_unreadable_version_that_holds_still_warns(tmp_path):
    row, calls = _row(tmp_path, None, "held")
    assert calls == ["/x/claude"] and row.ok and row.warn and "not readable" in row.detail


def test_no_claude_is_neutral(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    (row,) = doctor._check_setting_sources()
    assert row.neutral


def test_doctor_bounds_the_probe_by_passing_a_timeout(monkeypatch):
    seen = {}

    def fake_probe(binary, *, timeout):
        seen["timeout"] = timeout
        return ssc.ProbeResult("held", "x")
    monkeypatch.setattr(ssc, "run_probe", fake_probe)
    doctor._check_setting_sources(claude_bin="/x/claude", version="9.9.9")
    assert 0 < seen["timeout"] <= 10, seen
    assert seen["timeout"] + ssc.OVERRUN <= doctor._SETTING_SOURCES_PROBE_BUDGET, seen


def test_only_a_complete_post_to_the_model_endpoint_is_the_anchor():
    yes = [b"POST /v1/messages HTTP/1.1\r\n", b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: x\r\n"]
    no = [b"HEAD /api/hello HTTP/1.1\r\n", b"POST /v1/messages?beta=true HTTP/1.1", b"GET /v1/messages HTTP/1.1\r\n",
          b"POST /v1/messages/count_tokens HTTP/1.1\r\n", b"", b"\x16\x03\x01garbage\r\n"]
    assert all(ssc._is_model_request(h) for h in yes)
    assert not any(ssc._is_model_request(h) for h in no)
