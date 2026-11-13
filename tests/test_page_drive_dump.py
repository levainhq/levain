"""tests/_page_drive_dump.py: the bounded retry of ``chrome --dump-dom`` (no real Chrome; a fake runner and a fake ``chrome`` script)."""
from __future__ import annotations

import os
import stat
import subprocess
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
    got = []

    def runner(argv, *, timeout):
        got.append(argv)
        return subprocess.CompletedProcess(argv, 0, "d", "")

    dd.dump_dom("/x/chrome", "http://h/p", timeout=5, extra=("--foo",), runner=runner)
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
