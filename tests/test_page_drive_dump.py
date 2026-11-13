"""tests/_page_drive_dump.py: the bounded retry of ``chrome --dump-dom`` (no real Chrome; a fake runner and a fake ``chrome`` script)."""
from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from tests import _page_drive_dump as dd
from tests._chrome_flags import CHROME_SAFE_FLAGS


def _hang(argv, *, timeout):
    raise subprocess.TimeoutExpired(argv, timeout, output=b"", stderr=b"[1:2:ERROR:dbus/bus.cc:405] Failed to connect to the bus\n")


def _ok(dom: str):
    return lambda argv, *, timeout: subprocess.CompletedProcess(argv, 0, dom, "")


def test_the_first_attempt_hangs_the_one_retry_returns_the_dom_and_each_attempt_has_its_own_profile() -> None:
    seen: list[list[str]] = []

    def runner(argv, *, timeout):
        seen.append(list(argv))
        if len(seen) == 1:
            return _hang(argv, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, "<html>ok</html>", "")

    assert dd.dump_dom("/x/chrome", "http://127.0.0.1:1/", timeout=7, runner=runner) == "<html>ok</html>"
    assert len(seen) == 2
    profiles = [next(a for a in argv if a.startswith("--user-data-dir=")) for argv in seen]
    assert profiles[0] != profiles[1], "the retry reused the hung attempt's profile"
    assert not any(Path(p.split("=", 1)[1]).exists() for p in profiles), "a profile directory outlived its attempt"


def test_two_hung_attempts_fail_the_test_and_the_message_carries_both_attempts_and_the_box() -> None:
    calls = []

    def runner(argv, *, timeout):
        calls.append(argv)
        return _hang(argv, timeout=timeout)

    with pytest.raises(AssertionError) as raised:
        dd.dump_dom("/x/chrome", "http://127.0.0.1:1/", timeout=3, runner=runner)
    assert len(calls) == dd.ATTEMPTS == 2, "a third attempt, or none"
    msg = str(raised.value)
    assert isinstance(raised.value, dd.DumpDomHung)
    assert "attempt 1:" in msg and "attempt 2:" in msg and "no exit within 3s" in msg
    assert "Failed to connect to the bus" in msg, "the stderr tail is not in the message"
    assert "load " in msg and "/dev/shm" in msg, "the box facts are not in the message"


def test_the_argv_is_the_shared_flags_first_then_the_profile_the_dump_flags_and_the_url_last() -> None:
    got, bounds = [], []

    def runner(argv, *, timeout):
        got.append(argv)
        bounds.append(timeout)
        return subprocess.CompletedProcess(argv, 0, "d", "")

    dd.dump_dom("/x/chrome", "http://h/p", timeout=5, extra=("--foo",), runner=runner)
    assert bounds == [5], "the per-attempt bound is not the one the caller named"
    argv = got[0]
    assert argv[0] == "/x/chrome" and argv[1:1 + len(CHROME_SAFE_FLAGS)] == CHROME_SAFE_FLAGS
    assert "--dump-dom" in argv and "--virtual-time-budget=8000" in argv and "--foo" in argv and argv[-1] == "http://h/p"


def test_an_error_that_is_not_a_timeout_is_not_retried() -> None:
    calls = []

    def runner(argv, *, timeout):
        calls.append(1)
        raise FileNotFoundError("no chrome")

    with pytest.raises(FileNotFoundError):
        dd.dump_dom("/x/chrome", "http://h/", runner=runner)
    assert calls == [1]


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_a_hung_chrome_and_its_helper_do_not_outlive_the_test(tmp_path: Path) -> None:
    """The real runner on a fake ``chrome`` that spawns a helper and never exits: both attempts time out, and every pid it wrote is gone."""
    pids = tmp_path / "pids"
    fake = tmp_path / "chrome"
    fake.write_text('#!/bin/sh\nfor a; do last=$a; done\nsleep 300 &\necho $! >> "$last"\necho $$ >> "$last"\nsleep 300\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(dd.DumpDomHung):
        dd.dump_dom(str(fake), str(pids), timeout=1.5)
    found = [int(x) for x in pids.read_text().split()]
    assert len(found) == 4, found      # a helper and the chrome itself, for each of the two attempts
    # no wait: the runner itself returns only once the killed group is gone
    alive = [p for p in found if _alive(p)]
    assert alive == [], f"still running after the test: {alive}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie of this process still answers kill(0); it is not running
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return True
    return bool(out) and not out.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_a_hung_attempt_records_its_process_tree_before_the_kill_removes_it(tmp_path: Path) -> None:
    """The fake ``chrome`` and its ``sleep`` helper are alive when the snapshot is taken (so it is taken BEFORE the group kill),
    and the message names the group's columns, the profile-carrying main process and the helper that carries no profile path."""
    pids = tmp_path / "pids"
    fake = tmp_path / "chrome"
    fake.write_text('#!/bin/sh\nfor a; do last=$a; done\nsleep 301 &\necho $! >> "$last"\necho $$ >> "$last"\nsleep 301\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(dd.DumpDomHung) as raised:
        dd.dump_dom(str(fake), str(pids), timeout=1.5)
    msg = str(raised.value)
    assert msg.count("process tree about") == 2, "one snapshot per hung attempt"
    assert "PGID" in msg and "WCHAN" in msg and "COMMAND" in msg, "the ps header is not in the message"
    assert "/bin/sh " in msg, "the main process (the fake chrome is a sh script; its path may be cut by the column cap) is not in the tree"
    assert "sleep 301" in msg, "the helper, which carries no profile path, is not in the tree"
    assert "process tree: none recorded" not in msg


def test_no_live_process_with_the_profile_is_said_plainly() -> None:
    assert dd._hung_tree("/no/such/levain-dump-profile-zzz").startswith("(no live process")


def test_the_watcher_records_only_when_the_attempt_outlives_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(dd, "_hung_tree", lambda profile: calls.append(profile) or f"tree of {profile}")
    done, found = threading.Event(), []
    done.set()                                   # the attempt ended before the delay: no ps at all
    dd._watch("/p1", 30, done, found).join(5)
    assert calls == [] and found == []
    dd._watch("/p2", 0, threading.Event(), found).join(5)      # the delay passed with the attempt still running
    assert calls == ["/p2"] and found == ["tree of /p2"]


def test_a_normal_run_takes_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher thread is joined here (the delay is 0.1 s), so a watcher that was never told to stop would show as a ps call."""
    calls: list[str] = []
    monkeypatch.setattr(dd, "_hung_tree", lambda profile: calls.append(profile) or "tree")
    watchers: list[threading.Thread] = []
    real = dd._watch
    monkeypatch.setattr(dd, "_watch", lambda *a: watchers.append(real(*a)) or watchers[-1])
    assert dd.dump_dom("/x/chrome", "http://h/", timeout=0.2, runner=_ok("<html/>")) == "<html/>"
    assert len(watchers) == 1
    watchers[0].join(5)
    assert not watchers[0].is_alive() and calls == [], "ps ran for a run that finished"


def test_a_snapshot_under_way_when_the_attempt_times_out_is_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner raises its timeout while ``ps`` is still being read: the message must still carry that snapshot."""
    entered, release = threading.Event(), threading.Event()

    def slow_tree(profile: str) -> str:
        entered.set()
        release.wait(0.3)
        return "the slow tree"

    def runner(argv, *, timeout):
        entered.wait(5)
        return _hang(argv, timeout=timeout)

    monkeypatch.setattr(dd, "_hung_tree", slow_tree)
    with pytest.raises(dd.DumpDomHung) as raised:
        dd.dump_dom("/x/chrome", "http://h/", timeout=0.2, runner=runner)
    assert "the slow tree" in str(raised.value), "the snapshot under way was not waited for"
