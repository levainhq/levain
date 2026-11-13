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


def _wait_for_lines(path: Path, n: int, limit: float = 30.0) -> None:
    """Block until ``path`` holds ``n`` lines: the fake chrome's own signal that it is up. Not a sleep for a guessed time: it ends the
    moment the file is ready, and the bound only keeps a broken fake from hanging the suite."""
    ready, waited = threading.Event(), 0.0
    while waited < limit:
        if path.exists() and len(path.read_text().split()) >= n:
            return
        ready.wait(0.02)
        waited += 0.02
    raise AssertionError(f"{path} never reached {n} lines")


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_a_hung_attempt_records_its_process_tree_before_the_kill_removes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake ``chrome`` and its ``sleep`` helper are alive when the snapshot is taken (so it is taken BEFORE the group kill),
    and the message names the group's columns, the profile-carrying main process and the helper that carries no profile path.

    Deterministic: the snapshot delay is injected as zero and the snapshot itself waits for the fake's own "I am up" lines, so what is
    left to a stalled box is only the attempt's whole bound (4 s), not a guess about how long ``sh`` takes to start."""
    pids = tmp_path / "pids"
    fake = tmp_path / "chrome"
    fake.write_text('#!/bin/sh\nfor a; do last=$a; done\nsleep 301 &\necho $! >> "$last"\necho $$ >> "$last"\nsleep 301\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    real_tree = dd._hung_tree

    def gated_tree(profile: str) -> str:
        _wait_for_lines(pids, 2)
        return real_tree(profile)

    monkeypatch.setattr(dd, "_snapshot_delay", lambda timeout: 0)
    monkeypatch.setattr(dd, "_hung_tree", gated_tree)
    monkeypatch.setattr(dd, "ATTEMPTS", 1)
    with pytest.raises(dd.DumpDomHung) as raised:
        dd.dump_dom(str(fake), str(pids), timeout=4)
    msg = str(raised.value)
    assert msg.count("process tree about") == 1, "one snapshot per hung attempt"
    assert "PGID" in msg and "WCHAN" in msg and "COMMAND" in msg, "the ps header is not in the message"
    assert "/bin/sh " in msg, "the main process (the fake chrome is a sh script; its path may be cut by the column cap) is not in the tree"
    assert "sleep 301" in msg, "the helper, which carries no profile path, is not in the tree"
    assert "process tree: none recorded" not in msg


def test_the_snapshot_delay_is_the_lead_before_the_timeout_and_never_under_half_of_it() -> None:
    assert dd._snapshot_delay(90) == 90 - dd.TREE_LEAD
    assert dd._snapshot_delay(1.5) == 0.75, "a short bound is snapshotted at half of it, not at a negative lead"
    assert dd._snapshot_delay(0.2) == pytest.approx(0.1)


def test_the_message_names_the_lead_the_delay_implies(monkeypatch: pytest.MonkeyPatch) -> None:
    taken = threading.Event()
    monkeypatch.setattr(dd, "_hung_tree", lambda profile: taken.set() or "a tree")
    monkeypatch.setattr(dd, "_snapshot_delay", lambda timeout: 0)       # the snapshot is taken at once and the attempt then times out

    def runner(argv, *, timeout):
        assert taken.wait(10), "the zero-delay snapshot never ran"
        return _hang(argv, timeout=timeout)

    with pytest.raises(dd.DumpDomHung) as raised:
        dd.dump_dom("/x/chrome", "http://h/", timeout=10, runner=runner)
    assert "process tree about 10s before the kill" in str(raised.value), "the lead in the message is not timeout minus the delay"


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


class _Recorded:
    """A watcher thread that records the joins made on it (``dump_dom`` must join it on every way out, not only on a timeout)."""
    def __init__(self, inner: threading.Thread) -> None:
        self.inner, self.joins = inner, []

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)
        self.inner.join(timeout)

    def is_alive(self) -> bool:
        return self.inner.is_alive()


def test_a_normal_run_takes_no_snapshot_and_leaves_no_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic: the snapshot delay is a day, so no clock decides this. A watcher that was never told to stop is still alive when
    ``dump_dom`` returns; one that was told but not joined has no recorded join."""
    calls: list[str] = []
    monkeypatch.setattr(dd, "_hung_tree", lambda profile: calls.append(profile) or "tree")
    monkeypatch.setattr(dd, "_snapshot_delay", lambda timeout: 86400)
    watchers: list[_Recorded] = []
    delays: list[float] = []
    real = dd._watch

    def recording_watch(profile, delay, *rest):
        delays.append(delay)
        watchers.append(_Recorded(real(profile, delay, *rest)))
        return watchers[-1]

    monkeypatch.setattr(dd, "_watch", recording_watch)
    assert dd.dump_dom("/x/chrome", "http://h/", timeout=0.2, runner=_ok("<html/>")) == "<html/>"
    assert len(watchers) == 1 and delays == [86400], "the watcher was not given the snapshot delay"
    assert watchers[0].joins, "the watcher was not joined on the success path"
    assert not watchers[0].is_alive(), "a watcher outlived the call"
    assert calls == [], "ps ran for a run that finished"


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


def _fake_chrome(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\nfor a; do last=$a; done\necho run >> \"$last\"\n" + body)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake


@pytest.mark.skipif(os.name != "posix", reason="a fake chrome script")
def test_chrome_exiting_nonzero_with_no_dom_is_a_launch_failure_that_says_why_and_is_not_retried(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    fake = _fake_chrome(tmp_path, 'echo "FATAL: cannot init the profile" >&2\nexit 7\n')
    with pytest.raises(AssertionError) as raised:
        dd.dump_dom(str(fake), str(runs), timeout=30)
    assert isinstance(raised.value, dd.DumpDomFailed) and not isinstance(raised.value, dd.DumpDomHung)
    msg = str(raised.value)
    assert "exited 7" in msg, "the exit code is not in the message"
    assert "cannot init the profile" in msg, "the stderr tail is not in the message"
    assert runs.read_text().split() == ["run"], "a launch failure was retried"


@pytest.mark.skipif(os.name != "posix", reason="a fake chrome script")
def test_chrome_exiting_nonzero_with_the_dom_already_printed_is_a_success(tmp_path: Path) -> None:
    """macOS full Chrome 157 without a fresh profile prints the DOM, then exits rc 2 on its teardown watchdog."""
    runs = tmp_path / "runs"
    fake = _fake_chrome(tmp_path, 'echo "<html>the dom</html>"\necho "Teardown watchdog expired; recording dump and terminating." >&2\nexit 2\n')
    assert dd.dump_dom(str(fake), str(runs), timeout=30).strip() == "<html>the dom</html>"
    assert runs.read_text().split() == ["run"]


def test_a_nonzero_exit_with_only_whitespace_on_stdout_is_still_no_dom() -> None:
    runner = lambda argv, *, timeout: subprocess.CompletedProcess(argv, 3, "  \n", "boom")
    with pytest.raises(dd.DumpDomFailed) as raised:
        dd.dump_dom("/x/chrome", "http://h/", runner=runner)
    assert "exited 3" in str(raised.value) and "boom" in str(raised.value)


def test_a_zero_exit_with_an_empty_stdout_is_returned_as_it_always_was() -> None:
    assert dd.dump_dom("/x/chrome", "http://h/", runner=_ok("")) == ""


def _fake_ps(tmp_path: Path, profile: str) -> Path:
    """A directory holding only a ``ps`` that prints the profile-carrying main process with bytes that are not UTF-8 in its argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ps = bindir / "ps"
    ps.write_text("#!/bin/sh\n"
                  "printf '  PID  PPID  PGID STAT WCHAN COMMAND\\n'\n"
                  f"printf '  100     1   100 S    -     /x/chrome --user-data-dir={profile} \\377\\376 bad\\n'\n"
                  "printf '  101   100   100 S    -     the-helper\\n'\n"
                  "printf '  200     1   200 S    -     unrelated\\n'\n")
    ps.chmod(ps.stat().st_mode | stat.S_IXUSR)
    return bindir


@pytest.mark.skipif(os.name != "posix", reason="a fake ps script")
def test_ps_output_with_bytes_that_are_not_utf8_still_gives_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = "/tmp/levain-dump-profile-fake"
    monkeypatch.setenv("PATH", str(_fake_ps(tmp_path, profile)))
    tree = dd._hung_tree(profile)
    assert "ps unavailable" not in tree, tree
    assert "PGID" in tree and "the-helper" in tree and "unrelated" not in tree
    assert "�" in tree, "the invalid bytes were not replaced"


def test_a_decode_error_out_of_ps_is_a_line_in_the_tree_never_a_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(dd.subprocess, "run", broken)
    assert dd._hung_tree("/p").startswith("(ps unavailable")
