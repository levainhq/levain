"""The cockpit stall watchdog: a fake clock and a fake sleep drive the loop with no real time, so each case is exact.
Two cases use real time and a real faulthandler timer: the stop/start race and the interpreter-holding thread."""
from __future__ import annotations

import errno
import faulthandler
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from levain.cockpit import stall_watch
from levain.cockpit.stall_watch import (INTERVAL_S, KEEP_DUMPS, LATE_S, MAX_ARM_FAILURES, MIN_DUMP_GAP_S,
                                        StallWatch)


class FakeTime:
    """A clock and a sleep that share one reading. Each sleep advances the clock by the sleep plus the next scripted
    extra (how late that wake is); once the script is spent, the sleep ends the watch so ``run`` returns."""

    def __init__(self, extras: list[float]) -> None:
        self.now = 1000.0
        self.extras = list(extras)
        self.watch: StallWatch | None = None

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if not self.extras:
            assert self.watch is not None
            self.watch.stop()
            return
        self.now += seconds + self.extras.pop(0)


def drive(dump_dir, extras: list[float]) -> StallWatch:
    """Run the loop on the calling thread over the scripted lateness, one wake per extra, then return the watch."""
    fake = FakeTime(extras)
    watch = StallWatch(dump_dir, clock=fake.clock, sleep=fake.sleep)
    fake.watch = watch
    watch.run()
    return watch


def blocked_in_stall_probe_helper(entered: threading.Event, release: threading.Event) -> None:
    entered.set()
    release.wait(30)


def late_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("cockpit stall: watch late ")]


def watch_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "cockpit-stall-watch" and t.is_alive()]


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---- the loop and the dump --------------------------------------------------------------------------------------

def test_no_dump_and_no_stack_walk_below_the_threshold(tmp_path, capsys, monkeypatch):
    walked = []
    monkeypatch.setattr(stall_watch, "_write_stacks", lambda handle, budget: walked.append(1))
    watch = drive(tmp_path, [0.0, 0.01, LATE_S - 0.01, 0.0])
    assert walked == []
    assert watch.dumps() == []
    assert list(tmp_path.iterdir()) == []            # the scratch file is gone once the loop has ended
    assert capsys.readouterr().err == ""


def test_a_late_wake_writes_one_dump_and_one_stderr_line_carrying_its_path(tmp_path, capsys):
    watch = drive(tmp_path, [0.0, LATE_S + 0.5, 0.0])
    files = watch.dumps()
    assert len(files) == 1
    assert files[0].parent == tmp_path
    lines = late_lines(capsys.readouterr().err)
    assert lines == [f"cockpit stall: watch late {LATE_S + 0.5:.2f} s, dump {files[0]}"]
    assert f"watch late {LATE_S + 0.5:.2f} s" in files[0].read_text()


def test_the_dump_file_is_written_before_the_line_is_printed(tmp_path, monkeypatch):
    seen = []

    def say(line: str) -> None:
        seen.append((line, sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(stall_watch.DUMP_PREFIX))))

    monkeypatch.setattr(stall_watch, "say", say)
    drive(tmp_path, [LATE_S + 0.5])
    assert len(seen) == 1 and seen[0][0].startswith("cockpit stall: watch late ")
    assert len(seen[0][1]) == 1                       # the file was on disk when the line went out
    assert seen[0][1][0] in seen[0][0]


def test_the_new_dump_is_never_trimmed_by_its_own_call_even_when_a_backwards_clock_sorts_it_oldest(tmp_path,
                                                                                                   monkeypatch):
    for i in range(KEEP_DUMPS):
        (tmp_path / f"stall-2099010{i % 10}T000000000000Z-{i:06d}.txt").write_text(f"old {i}")
    monkeypatch.setattr(stall_watch, "datetime", _FixedStamp(2001))       # the clock stepped back: sorts before all 20
    watch = StallWatch(tmp_path)
    path = watch._write_dump(1.5)
    assert path.exists()
    assert path in watch.dumps() and len(watch.dumps()) == KEEP_DUMPS


def test_the_dump_names_a_thread_blocked_in_a_known_function(tmp_path):
    entered, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=blocked_in_stall_probe_helper, args=(entered, release), name="probe-worker",
                              daemon=True)
    worker.start()
    try:
        assert entered.wait(10)
        watch = drive(tmp_path, [LATE_S + 1.0])
    finally:
        release.set()
        worker.join(10)
    text = watch.dumps()[0].read_text()
    assert "blocked_in_stall_probe_helper" in text
    assert "probe-worker" in text


def test_the_fallback_dump_takes_no_lock_that_threading_enumerate_takes(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("threading.enumerate takes the lock that guards thread start")

    monkeypatch.setattr(threading, "enumerate", boom)
    watch = drive(tmp_path, [LATE_S + 0.5])
    assert "MainThread" in watch.dumps()[0].read_text()


def test_two_late_wakes_inside_the_gap_write_one_dump_and_the_second_line_says_so(tmp_path, capsys):
    watch = drive(tmp_path, [LATE_S + 0.5, LATE_S + 0.5])
    files = watch.dumps()
    assert len(files) == 1
    lines = late_lines(capsys.readouterr().err)
    assert len(lines) == 2                            # the stderr line is not rate limited: both late wakes are named
    assert "rate limited" not in lines[0] and f"dump {files[0]}" in lines[0]
    assert "rate limited" in lines[1] and f"last dump {files[0]}" in lines[1]


def test_a_late_wake_past_the_gap_writes_a_second_dump(tmp_path):
    watch = drive(tmp_path, [LATE_S + 0.5, MIN_DUMP_GAP_S + 1.0])
    assert len(watch.dumps()) == 2


def test_only_the_newest_keep_dumps_stay(tmp_path):
    total = KEEP_DUMPS + 5
    extras = [MIN_DUMP_GAP_S + 1.0 + i * 0.01 for i in range(total)]
    watch = drive(tmp_path, extras)
    files = watch.dumps()
    assert len(files) == KEEP_DUMPS
    assert len(list(tmp_path.iterdir())) == KEEP_DUMPS
    # the survivors are the newest: the oldest kept is wake 5 (0-based), the newest is the last wake
    assert f"late {extras[total - KEEP_DUMPS]:.2f} s" in files[0].read_text()
    assert f"late {extras[-1]:.2f} s" in files[-1].read_text()


def test_an_unwritable_directory_is_swallowed_and_the_thread_survives(tmp_path, capsys):
    gone = tmp_path / "no" / "such" / "dir"
    gate, spent = threading.Event(), threading.Event()
    script = [LATE_S + 0.5, MIN_DUMP_GAP_S + 1.0, LATE_S + 0.5]
    fake = FakeTime([])

    def sleep(seconds: float) -> None:
        if script:
            fake.now += seconds + script.pop(0)
        else:
            spent.set()
            gate.wait(30)

    watch = StallWatch(gone, clock=fake.clock, sleep=sleep)
    watch.start()
    try:
        assert spent.wait(10)
        assert watch._thread is not None and watch._thread.is_alive()
        err = capsys.readouterr().err
        assert len(late_lines(err)) == 3               # every late wake still named, after the first failed write
        assert err.count("dump not written") == 2      # the failed attempts: the first and the one past the gap
        assert watch.dumps() == []
    finally:
        gate.set()
        watch.stop()
    assert not (tmp_path / "no").exists()              # the missing root was not made on the way


def test_the_loop_asks_each_sleep_for_the_interval(tmp_path):
    asked = []
    fake = FakeTime([])

    def sleep(seconds: float) -> None:
        asked.append(seconds)
        if len(asked) == 3:
            watch.stop()
        fake.now += seconds

    watch = StallWatch(tmp_path, clock=fake.clock, sleep=sleep)
    watch.run()
    assert asked == [INTERVAL_S] * 3


def test_a_clock_that_raises_does_not_end_the_loop(tmp_path, capsys):
    calls = {"n": 0}
    done = threading.Event()

    def clock() -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("clock broke")
        return float(calls["n"])

    def sleep(seconds: float) -> None:
        if calls["n"] >= 3:
            done.set()
            watch.stop()

    watch = StallWatch(tmp_path, clock=clock, sleep=sleep)
    watch.run()
    assert done.is_set()
    assert "watch error: RuntimeError" in capsys.readouterr().err


def test_a_closed_stderr_does_not_raise_from_the_report_or_the_say(tmp_path, monkeypatch):
    import io

    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)
    stall_watch.say("anything")                        # a closed stderr is swallowed
    watch = drive(tmp_path, [LATE_S + 0.5])
    assert len(watch.dumps()) == 1


# ---- permissions and the directory ------------------------------------------------------------------------------

@pytest.fixture
def umask022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def test_the_stalls_directory_is_0700_the_dump_and_the_scratch_are_0600(tmp_path, umask022):
    root = tmp_path / "data_root"
    root.mkdir()
    root_mode = mode_of(root)
    stalls = root / "stalls"
    watch = StallWatch(stalls)
    assert not stalls.exists()                         # constructing a watch touches no disk
    watch._prepare()
    try:
        assert mode_of(stalls) == 0o700
        scratch = stalls / stall_watch.scratch_name(os.getpid())
        assert scratch.exists() and mode_of(scratch) == 0o600
    finally:
        watch._close_live()
    drive(stalls, [LATE_S + 0.5])
    dumps = sorted(p for p in stalls.iterdir())
    assert len(dumps) == 1 and mode_of(dumps[0]) == 0o600
    assert mode_of(root) == root_mode                  # the existing root's mode is left alone


def test_a_missing_data_root_is_not_made(tmp_path, umask022):
    root = tmp_path / "no-root"
    watch = StallWatch(root / "stalls")
    watch._prepare()
    assert not root.exists()                           # never parents=True
    assert watch._scratch is None                      # no live capture without a directory


def test_the_directory_is_made_by_the_watch_thread_not_by_the_caller(tmp_path, umask022):
    root = tmp_path / "data_root"
    root.mkdir()
    stalls = root / "stalls"
    gate = threading.Event()
    watch = StallWatch(stalls, sleep=lambda s: gate.wait(30))
    watch.start()
    try:
        for _ in range(200):
            if stalls.is_dir():
                break
            time.sleep(0.05)
        assert stalls.is_dir() and mode_of(stalls) == 0o700
    finally:
        gate.set()
        watch.stop()
    assert not stalls.exists() or not any(stalls.iterdir())      # the scratch is removed on stop


# ---- the scratch file is per process ---------------------------------------------------------------------------

def _gone_pid() -> int:
    """The pid of a process that has run and been reaped."""
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])
    child.wait()
    return child.pid


def test_two_watches_with_different_pids_do_not_share_a_scratch_file(tmp_path):
    first, second = StallWatch(tmp_path, pid=os.getpid()), StallWatch(tmp_path, pid=os.getpid() + 100_000_000)
    first._prepare()
    second._prepare()
    try:
        assert first._scratch_path != second._scratch_path
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            [first._scratch_path.name, second._scratch_path.name])
        os.write(first._scratch.fileno(), b"first's stacks")
        os.ftruncate(second._scratch.fileno(), 0)        # what the second's arm does to its own file
        assert first._scratch_path.read_bytes() == b"first's stacks"
        second._close_live()                             # and its end removes only its own file
        assert first._scratch_path.exists()
    finally:
        first._close_live()
        second._close_live()


def test_a_scratch_file_of_a_dead_pid_is_removed_and_one_of_a_live_pid_is_not(tmp_path):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(b"left by a process that died")
    other = tmp_path / "live-stacks.notapid.scratch"
    other.write_bytes(b"not ours to judge")
    live_child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        alive = tmp_path / stall_watch.scratch_name(live_child.pid)
        alive.write_bytes(b"a running cockpit's stacks")
        watch = StallWatch(tmp_path)
        watch._prepare()
        try:
            assert not dead.exists()
            assert alive.read_bytes() == b"a running cockpit's stacks"
            assert other.exists()
            assert watch._scratch_path is not None and watch._scratch_path.exists()
        finally:
            watch._close_live()
    finally:
        live_child.kill()
        live_child.wait()


# ---- a killed process's live capture is kept, not deleted -------------------------------------------------------

STACK_TEXT = b"Thread 0x00000001 (most recent call first):\n  File \"bridge.py\", line 9 in held_the_interpreter\n"


def killed_dumps(directory) -> list:
    return sorted(p for p in directory.iterdir() if p.name.startswith("stall-") and "killed" in p.name)


def test_a_dead_pids_scratch_with_stacks_becomes_a_kept_dump_named_for_the_pid_and_killed(tmp_path):
    pid = _gone_pid()
    dead = tmp_path / stall_watch.scratch_name(pid)
    dead.write_bytes(STACK_TEXT)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        kept = killed_dumps(tmp_path)
        assert len(kept) == 1
        assert str(pid) in kept[0].name and "killed" in kept[0].name
        assert kept[0] in watch.dumps()
        assert STACK_TEXT.decode() in kept[0].read_text()
        assert mode_of(kept[0]) == 0o600
        assert not dead.exists()
    finally:
        watch._close_live()


def test_an_empty_scratch_of_a_dead_pid_is_removed_and_makes_no_dump(tmp_path):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(b"")
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert not dead.exists()
        assert watch.dumps() == []
    finally:
        watch._close_live()


def test_a_live_pids_scratch_is_never_touched_and_makes_no_dump(tmp_path):
    live_child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    try:
        alive = tmp_path / stall_watch.scratch_name(live_child.pid)
        alive.write_bytes(STACK_TEXT)
        watch = StallWatch(tmp_path)
        watch._prepare()
        try:
            assert alive.read_bytes() == STACK_TEXT
            assert watch.dumps() == []
        finally:
            watch._close_live()
    finally:
        live_child.kill()
        live_child.wait()


class _FixedStamp:
    """A stand-in for the module's datetime whose clock reads one fixed instant."""

    def __init__(self, year: int) -> None:
        from datetime import datetime, timezone
        self._at = datetime(year, 1, 1, tzinfo=timezone.utc)

    def now(self, tz=None):
        return self._at


def test_a_kept_dump_counts_inside_keep_dumps_and_the_oldest_goes_first_never_the_new_one(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "datetime", _FixedStamp(2001))       # earlier than every name below
    for i in range(KEEP_DUMPS):
        (tmp_path / f"stall-2099010{i % 10}T000000000000Z-{i:06d}.txt").write_text(f"old {i}")
    before = sorted(p.name for p in tmp_path.iterdir())
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        files = watch.dumps()
        assert len(files) == KEEP_DUMPS
        kept = killed_dumps(tmp_path)
        assert len(kept) == 1 and kept[0] in files               # the new one survives although it sorts oldest
        gone = set(before) - {p.name for p in files}
        assert gone == {before[0]}                               # the oldest of the others went
    finally:
        watch._close_live()


def test_a_kept_dump_body_is_inside_the_byte_budget(tmp_path):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(b"x" * 500_000)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        (kept,) = killed_dumps(tmp_path)
        text = kept.read_text()
        assert f"[dump cut at {stall_watch.MAX_DUMP_BYTES} bytes]" in text
        assert text.count("x") == stall_watch.MAX_DUMP_BYTES
    finally:
        watch._close_live()


def test_a_kept_dump_never_overwrites_one_of_the_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "datetime", _FixedStamp(2030))       # every name below shares one stamp
    pid = _gone_pid()
    for round_ in range(3):
        (tmp_path / stall_watch.scratch_name(pid)).write_text(f"stacks of round {round_}\n")
        watch = StallWatch(tmp_path)         # a new watch starts its sequence over, so its first name repeats
        watch._prepare()
        watch._close_live()
    kept = killed_dumps(tmp_path)
    assert len({p.name for p in kept}) == 3
    assert sorted(p.read_text().split("stacks of round")[1].strip() for p in kept) == ["0", "1", "2"]


def test_a_dump_that_cannot_be_written_leaves_the_dead_scratch_in_place_and_does_not_raise(tmp_path, monkeypatch,
                                                                                         capsys):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)

    def no_space(self, path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(StallWatch, "_open_new_dump", no_space)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert dead.read_bytes() == STACK_TEXT
        assert "not written" in capsys.readouterr().err
    finally:
        watch._close_live()


@pytest.mark.skipif(os.getuid() == 0, reason="root reads any file")
def test_a_scratch_that_cannot_be_read_does_not_crash_prepare_and_is_left_in_place_and_named(tmp_path, capsys):
    pid = _gone_pid()
    dead = tmp_path / stall_watch.scratch_name(pid)
    dead.write_bytes(STACK_TEXT)
    dead.chmod(0)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert watch._scratch is not None             # the watch still has its own live capture
        assert dead.exists()                          # fail closed: not deleted unread
        assert watch.dumps() == []
        said = capsys.readouterr().err
        assert f"{pid}" in said and "not written" in said
    finally:
        dead.chmod(0o600)
        watch._close_live()


def _open_failing_for(monkeypatch, target, err: int) -> None:
    """Make os.open raise OSError(err) for ``target`` only; every other open is the real one."""
    real_open = os.open

    def fake_open(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            raise OSError(err, os.strerror(err))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)


@pytest.mark.parametrize("err", [errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EINTR, errno.EIO,
                                 errno.EACCES, errno.EPERM, errno.ENXIO])
def test_an_open_that_fails_for_a_reason_about_the_process_leaves_the_dead_scratch_and_says_so(tmp_path, monkeypatch,
                                                                                               capsys, err):
    pid = _gone_pid()
    dead = tmp_path / stall_watch.scratch_name(pid)
    dead.write_bytes(STACK_TEXT)
    _open_failing_for(monkeypatch, dead, err)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert dead.read_bytes() == STACK_TEXT         # kept for the next start, not deleted unread
        assert killed_dumps(tmp_path) == []
        said = capsys.readouterr().err
        assert f"{pid}" in said and "not written" in said and os.strerror(err) in said
    finally:
        watch._close_live()


@pytest.mark.parametrize("err", [errno.ENOENT, errno.ELOOP, errno.EMLINK])
def test_an_open_that_says_gone_or_a_refused_symlink_still_deletes_the_dead_scratch(tmp_path, monkeypatch, err):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)
    _open_failing_for(monkeypatch, dead, err)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert not dead.exists()
        assert killed_dumps(tmp_path) == []
    finally:
        watch._close_live()


def test_a_symlink_scratch_is_refused_and_the_link_is_deleted_not_its_target(tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(STACK_TEXT)
    stalls = tmp_path / "stalls"
    stalls.mkdir()
    link = stalls / stall_watch.scratch_name(_gone_pid())
    link.symlink_to(target)
    watch = StallWatch(stalls)
    watch._prepare()
    try:
        assert not link.is_symlink() and not link.exists()
        assert target.read_bytes() == STACK_TEXT      # never followed, never touched
        assert killed_dumps(stalls) == []
    finally:
        watch._close_live()


def test_an_unforeseen_error_while_keeping_leaves_the_dead_scratch_and_says_so(tmp_path, monkeypatch, capsys):
    pid = _gone_pid()
    dead = tmp_path / stall_watch.scratch_name(pid)
    dead.write_bytes(STACK_TEXT)

    def odd_open(self, path):
        raise ValueError("odd dump name")

    monkeypatch.setattr(StallWatch, "_open_new_dump", odd_open)     # not an OSError: only the outer net catches it
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert dead.read_bytes() == STACK_TEXT
        assert killed_dumps(tmp_path) == []
        said = capsys.readouterr().err
        assert f"{pid}" in said and "not written" in said and "ValueError" in said
    finally:
        watch._close_live()


def test_a_write_that_fails_midway_removes_the_partial_dump_and_keeps_the_scratch(tmp_path, monkeypatch, capsys):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)
    seen = []

    def fail_after_create(fd, handle, budget, banner):
        seen.append(len(killed_dumps(tmp_path)))      # the dump file already exists when the copy starts
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(stall_watch, "_copy_fd", fail_after_create)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        assert seen == [1]
        assert killed_dumps(tmp_path) == []           # the partial one is gone
        assert dead.read_bytes() == STACK_TEXT
        assert "not written" in capsys.readouterr().err
    finally:
        watch._close_live()


def test_a_failing_trim_after_a_written_dump_still_removes_the_scratch_and_says_nothing(tmp_path, monkeypatch, capsys):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)

    def broken_trim(self, keep=None):
        raise RuntimeError("degraded directory")

    monkeypatch.setattr(StallWatch, "_trim_dumps", broken_trim)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        (kept,) = killed_dumps(tmp_path)
        assert STACK_TEXT.decode() in kept.read_text()
        assert not dead.exists()
        assert "not written" not in capsys.readouterr().err
    finally:
        watch._close_live()


def test_a_scratch_cut_mid_character_keeps_a_replacement_character_at_the_end_of_the_dump(tmp_path):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT + b"\xc3")             # the last write of a killed process ended inside a character
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        (kept,) = killed_dumps(tmp_path)
        assert kept.read_text().endswith(STACK_TEXT.decode() + "\ufffd")
    finally:
        watch._close_live()


def test_a_later_dumps_trim_can_delete_a_kept_dump_once_keep_dumps_newer_ones_exist(tmp_path):
    dead = tmp_path / stall_watch.scratch_name(_gone_pid())
    dead.write_bytes(STACK_TEXT)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        (kept,) = killed_dumps(tmp_path)
        for i in range(KEEP_DUMPS - 1):
            (tmp_path / f"stall-2099010{i % 10}T000000000000Z-{i:06d}.txt").write_text(f"newer {i}")
        assert kept.exists() and len(watch.dumps()) == KEEP_DUMPS     # full, the kept one still inside
        watch._write_dump(1.5)                                         # the 21st: the trim has no protected dump
        assert not kept.exists()
        assert len(watch.dumps()) == KEEP_DUMPS
    finally:
        watch._close_live()


def test_the_kept_dump_header_names_the_pid_and_the_scratch_size_and_claims_no_cause(tmp_path):
    pid = _gone_pid()
    dead = tmp_path / stall_watch.scratch_name(pid)
    dead.write_bytes(STACK_TEXT)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        (kept,) = killed_dumps(tmp_path)
        header = kept.read_text().splitlines()[0]
        assert header.startswith(f"cockpit stall: process {pid} ended without stopping its watch; ")
        assert f"size {len(STACK_TEXT)} bytes" in header
        assert header.endswith("a late-wake report may or may not have been written from them")
        assert "no late-wake report was written" not in header
        assert f"killed-pid{pid}-" in kept.name
    finally:
        watch._close_live()


# ---- a directory that is not ours to trust ------------------------------------------------------------------------

def test_a_stalls_directory_that_is_a_symlink_is_refused_for_the_scratch_and_the_dumps(tmp_path, capsys):
    outside = tmp_path / "outside"
    outside.mkdir()
    stalls = tmp_path / "stalls"
    stalls.symlink_to(outside)
    watch = drive(stalls, [LATE_S + 0.5, MIN_DUMP_GAP_S + 1.0, LATE_S + 0.5])
    assert list(outside.iterdir()) == []                 # nothing was written through the link
    assert watch._scratch is None
    err = capsys.readouterr().err
    assert err.count("live capture and dumps off") == 1  # said once, not once per late wake
    assert len(late_lines(err)) == 3                     # every late wake is still named on stderr


def test_a_stalls_path_that_is_a_file_is_refused(tmp_path, capsys):
    stalls = tmp_path / "stalls"
    stalls.write_text("not a directory")
    watch = drive(stalls, [LATE_S + 0.5])
    assert stalls.read_text() == "not a directory"
    assert watch._scratch is None
    assert "live capture and dumps off" in capsys.readouterr().err


def test_an_existing_stalls_directory_this_user_owns_is_set_to_0700(tmp_path, umask022):
    stalls = tmp_path / "stalls"
    stalls.mkdir()
    os.chmod(stalls, 0o755)
    watch = StallWatch(stalls)
    watch._prepare()
    try:
        assert mode_of(stalls) == 0o700
        assert watch._scratch is not None
    finally:
        watch._close_live()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="this platform has no O_NOFOLLOW")
def test_a_symlink_planted_at_the_scratch_name_is_not_followed(tmp_path, capsys):
    stalls = tmp_path / "stalls"
    stalls.mkdir()
    target = tmp_path / "victim"
    target.write_text("keep me")
    watch = StallWatch(stalls)
    (stalls / stall_watch.scratch_name(os.getpid())).symlink_to(target)
    watch._prepare()
    assert target.read_text() == "keep me"
    assert watch._scratch is None
    assert "live capture off" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="this platform has no O_NOFOLLOW")
def test_the_dump_is_opened_without_following_a_link(tmp_path, monkeypatch):
    flags = []
    real = os.open
    monkeypatch.setattr(stall_watch.os, "open", lambda path, fl, *a: (flags.append(fl), real(path, fl, *a))[1])
    watch = StallWatch(tmp_path)
    watch._write_dump(1.5)
    assert flags and all(f & os.O_NOFOLLOW for f in flags)


class _OsWithBrokenFdopen:
    """``os`` as stall_watch sees it, with fdopen failing and every fd os.open hands out remembered."""

    def __init__(self) -> None:
        self.fds: list[int] = []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, *args, **kwargs):
        fd = os.open(*args, **kwargs)
        self.fds.append(fd)
        return fd

    def fdopen(self, *args, **kwargs):
        raise OSError("no file object")


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_a_failed_fdopen_in_the_dump_closes_the_fd_and_removes_the_file(tmp_path, monkeypatch):
    fake_os = _OsWithBrokenFdopen()
    monkeypatch.setattr(stall_watch, "os", fake_os)
    watch = StallWatch(tmp_path)
    with pytest.raises(OSError, match="no file object"):
        watch._write_dump(1.5)
    assert len(fake_os.fds) == 1 and not _is_open(fake_os.fds[0])
    assert list(tmp_path.iterdir()) == []


def test_a_failed_fdopen_in_prepare_closes_the_fd_and_removes_the_scratch(tmp_path, monkeypatch):
    fake_os = _OsWithBrokenFdopen()
    monkeypatch.setattr(stall_watch, "os", fake_os)
    watch = StallWatch(tmp_path)
    watch._prepare()
    assert watch._scratch is None and len(fake_os.fds) == 1 and not _is_open(fake_os.fds[0])
    assert list(tmp_path.iterdir()) == []


def test_a_failed_fchmod_in_prepare_closes_the_fd_and_removes_the_scratch(tmp_path, monkeypatch, capsys):
    class BrokenFchmod(_OsWithBrokenFdopen):
        def fchmod(self, fd, mode):
            raise PermissionError("no chmod")

    fake_os = BrokenFchmod()
    monkeypatch.setattr(stall_watch, "os", fake_os)
    watch = StallWatch(tmp_path)
    watch._prepare()
    assert watch._scratch is None and len(fake_os.fds) == 1 and not _is_open(fake_os.fds[0])
    assert list(tmp_path.iterdir()) == []
    assert "live capture off: PermissionError" in capsys.readouterr().err


def test_a_thread_that_will_not_start_leaves_stop_and_the_next_start_working(tmp_path, monkeypatch):
    real = threading.Thread

    class Unstartable(real):
        def start(self):
            raise RuntimeError("can't start new thread")

    watch = StallWatch(tmp_path / "stalls")
    with monkeypatch.context() as patch:
        patch.setattr(stall_watch.threading, "Thread", Unstartable)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            watch.start()
    assert watch._thread is None
    watch.stop()                                         # joins nothing: there is no thread that ran
    watch.start()                                        # and a later start works
    try:
        assert len(watch_threads()) == 1
    finally:
        watch.stop()
    assert watch_threads() == []


# ---- the caps ---------------------------------------------------------------------------------------------------

def _recurse(depth: int, entered: threading.Event, release: threading.Event) -> None:
    if depth == 0:
        entered.set()
        release.wait(30)
        return
    _recurse(depth - 1, entered, release)


def test_the_fallback_dump_caps_threads_and_frame_depth(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "MAX_THREADS", 3)
    monkeypatch.setattr(stall_watch, "MAX_FRAMES", 5)
    release = threading.Event()
    workers = []
    for i in range(6):
        entered = threading.Event()
        t = threading.Thread(target=_recurse, args=(30, entered, release), name=f"deep-{i}", daemon=True)
        t.start()
        assert entered.wait(10)
        workers.append(t)
    try:
        watch = drive(tmp_path, [LATE_S + 0.5])
    finally:
        release.set()
        for t in workers:
            t.join(10)
    text = watch.dumps()[0].read_text()
    sections = text.split("\n--- thread ")[1:]
    assert len(sections) == 3
    assert " 3 of " in text                            # the header says how many were shown of how many there were
    for section in sections:
        assert section.count('  File "') <= 5          # the innermost MAX_FRAMES frames, no more


def test_the_dump_body_is_cut_at_the_byte_cap_with_one_note(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "MAX_DUMP_BYTES", 300)
    watch = drive(tmp_path, [LATE_S + 0.5])
    text = watch.dumps()[0].read_text()
    assert text.count("[dump cut at 300 bytes]") == 1
    assert len(text) < 300 + 500                       # the header and the note, plus the capped body


def test_the_byte_budget_counts_and_cuts_by_encoded_bytes_not_characters(tmp_path):
    handle_path = tmp_path / "out.txt"
    with open(handle_path, "w", encoding="utf-8") as handle:
        budget = stall_watch._Budget(10)
        budget.write(handle, "\U0001F600" * 100)         # a 4-byte character each: 400 bytes, 100 characters
        budget.write(handle, "more")                      # after the cut nothing more is written
    data = handle_path.read_bytes()
    body, note = data.split(b"\n[dump cut", 1)
    assert len(body) <= 10 and len(body) == 8             # two whole characters; the third would pass the budget
    assert body.decode("utf-8") == "\U0001F600" * 2
    assert note.count(b"bytes]") == 1 and b"more" not in data


def test_the_live_copy_reads_at_most_the_cap_plus_one_byte_of_the_scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "MAX_DUMP_BYTES", 1000)
    watch = StallWatch(tmp_path)
    watch._prepare()
    try:
        os.write(watch._scratch.fileno(), b"x" * 500_000)
        read = []
        real = os.pread
        monkeypatch.setattr(stall_watch.os, "pread", lambda fd, n, off: (read.append(n), real(fd, n, off))[1])
        out = tmp_path / "out.txt"
        with open(out, "w", encoding="utf-8") as handle:
            assert watch._copy_live(handle, stall_watch._Budget(1000)) is True
    finally:
        watch._close_live()
    assert sum(read) == 1001
    assert out.read_text().count("[dump cut at 1000 bytes]") == 1


def test_the_fallback_dump_is_written_in_pieces_not_joined(tmp_path, monkeypatch):
    writes = []
    real = stall_watch._Budget.write

    def counting(self, handle, text):
        writes.append(text)
        real(self, handle, text)

    monkeypatch.setattr(stall_watch._Budget, "write", counting)
    drive(tmp_path, [LATE_S + 0.5])
    assert len(writes) > 3                             # a header, then a header and its frames per thread


# ---- the live capture: faulthandler armed and re-armed every tick -----------------------------------------------

class FakeFaulthandler:
    """Stands in for ``dump_traceback_later``: records every arm and cancel, and when told to, writes a live dump into
    the file it was armed with (what the C thread does when a tick does not come back in time)."""

    def __init__(self, write_on_arm: str = "") -> None:
        self.calls: list[tuple] = []
        self.write_on_arm = write_on_arm

    def dump_traceback_later(self, timeout, repeat=False, file=None, exit=False):
        self.calls.append(("arm", timeout, repeat, exit))
        if self.write_on_arm:
            os.write(file.fileno(), self.write_on_arm.encode())

    def cancel_dump_traceback_later(self):
        self.calls.append(("cancel",))


def test_every_tick_cancels_and_re_arms_the_live_timer_and_the_end_leaves_none_armed(tmp_path, monkeypatch):
    fake = FakeFaulthandler()
    monkeypatch.setattr(stall_watch, "faulthandler", fake)
    drive(tmp_path, [0.0, 0.0, 0.0])
    arms = [c for c in fake.calls if c[0] == "arm"]
    assert len(arms) == 4                              # one per tick: three scripted wakes and the one that ends it
    assert all(c == ("arm", LATE_S, False, False) for c in arms)
    assert fake.calls[-1] == ("cancel",)               # nothing is left armed once the loop has ended
    assert list(tmp_path.iterdir()) == []


def test_a_late_wake_with_live_stacks_in_the_scratch_dumps_those_not_the_fallback(tmp_path, monkeypatch):
    live = "Thread 0x0001 (most recent call first):\n  File \"x.py\", line 1 in the_holder_frame\n"
    monkeypatch.setattr(stall_watch, "faulthandler", FakeFaulthandler(write_on_arm=live))
    watch = drive(tmp_path, [LATE_S + 0.5])
    text = watch.dumps()[0].read_text()
    assert "the_holder_frame" in text and "live stacks" in text
    assert "fallback stacks" not in text


def test_a_short_stall_leaves_no_live_stacks_behind_for_the_next_report(tmp_path, monkeypatch):
    live = "Thread 0x0001 (most recent call first):\n  File \"x.py\", line 1 in a_stale_frame\n"
    fake = FakeFaulthandler(write_on_arm=live)
    monkeypatch.setattr(stall_watch, "faulthandler", fake)
    sizes = []
    real = StallWatch._arm

    def watching(self):
        real(self)
        sizes.append(os.fstat(self._scratch.fileno()).st_size if self._scratch is not None else None)

    monkeypatch.setattr(StallWatch, "_arm", watching)
    drive(tmp_path, [0.0, 0.0])
    assert sizes == [len(live)] * 3                    # emptied before each arm, so never more than one dump's worth


def test_a_rate_limited_wake_drops_the_live_stacks_and_writes_no_file(tmp_path, monkeypatch):
    live = "Thread 0x0001 (most recent call first):\n  File \"x.py\", line 1 in the_holder_frame\n"
    monkeypatch.setattr(stall_watch, "faulthandler", FakeFaulthandler(write_on_arm=live))
    watch = drive(tmp_path, [LATE_S + 0.5, LATE_S + 0.5])
    assert len(watch.dumps()) == 1


def test_arms_that_keep_failing_turn_the_live_capture_off_and_the_fallback_still_dumps(tmp_path, monkeypatch, capsys):
    class Broken(FakeFaulthandler):
        def dump_traceback_later(self, *a, **k):
            raise RuntimeError("no timer")

    monkeypatch.setattr(stall_watch, "faulthandler", Broken())
    watch = drive(tmp_path, [LATE_S + 0.5] + [0.0] * MAX_ARM_FAILURES)
    assert "live capture off: RuntimeError: no timer" in capsys.readouterr().err
    assert "fallback stacks" in watch.dumps()[0].read_text()
    assert list(p.name for p in tmp_path.iterdir() if "scratch" in p.name) == []


def test_one_failed_arm_does_not_turn_the_live_capture_off_and_a_success_resets_the_count(tmp_path, monkeypatch, capsys):
    class Flaky(FakeFaulthandler):
        """Raises on the arms listed by (1-based) number, succeeds on the rest."""

        def __init__(self, failing) -> None:
            super().__init__()
            self.failing, self.arms = set(failing), 0

        def dump_traceback_later(self, *a, **k):
            self.arms += 1
            if self.arms in self.failing:
                raise RuntimeError("one bad arm")
            super().dump_traceback_later(*a, **k)

    # failures, never MAX_ARM_FAILURES in a row, with a success between each run of them
    failing = [1] + list(range(3, 3 + MAX_ARM_FAILURES - 1)) + [3 + MAX_ARM_FAILURES]
    fake = Flaky(failing)
    monkeypatch.setattr(stall_watch, "faulthandler", fake)
    ticks = 3 + MAX_ARM_FAILURES + 2
    watch = drive(tmp_path, [0.0] * (ticks - 1))
    err = capsys.readouterr().err
    assert "live capture off" not in err
    assert err.count("live capture arm failed") == len(failing)
    assert watch._arm_failures == 0
    assert fake.arms == ticks                           # an armed live capture was still being asked each tick


def test_stop_cancels_the_live_timer(tmp_path, monkeypatch):
    fake = FakeFaulthandler()
    monkeypatch.setattr(stall_watch, "faulthandler", fake)
    gate = threading.Event()
    watch = StallWatch(tmp_path, sleep=lambda s: gate.wait(30))
    watch.start()
    try:
        for _ in range(200):
            if any(c[0] == "arm" for c in fake.calls):
                break
            time.sleep(0.01)
        fake.calls.clear()
    finally:
        gate.set()
        watch.stop()
    assert ("cancel",) in fake.calls


# ---- the lifecycle ----------------------------------------------------------------------------------------------

def test_start_and_stop_are_idempotent_and_stop_is_prompt(tmp_path):
    watch = StallWatch(tmp_path / "stalls")
    watch.stop()                                       # before start: nothing to do, no error
    watch.start()
    watch.start()
    try:
        ours = watch_threads()
        assert len(ours) == 1
        assert ours[0].daemon
    finally:
        began = time.monotonic()
        watch.stop()
        watch.stop()
    assert time.monotonic() - began < 1.0
    assert not watch_threads()
    watch.start()                                      # a stopped watch starts again
    watch.stop()


def test_stop_then_start_while_the_old_thread_lives_never_runs_two_watchers(tmp_path, monkeypatch):
    monkeypatch.setattr(stall_watch, "JOIN_S", 0.05)
    gate, asleep = threading.Event(), threading.Event()

    def sleep(seconds: float) -> None:                 # a sleep that ignores the stop event
        asleep.set()
        gate.wait(30)

    watch = StallWatch(tmp_path / "stalls", sleep=sleep)
    watch.start()
    old = watch._thread
    assert asleep.wait(10)
    try:
        watch.stop()                                   # times out: the old thread is still in its sleep
        assert old.is_alive()
        watch.start()                                  # must not start a second watcher beside the stopping one
        assert len(watch_threads()) == 1
        assert watch._thread is old
        gate.set()                                     # the old thread now sees its own stop event and ends
        old.join(10)
        assert not old.is_alive()
        gate.clear()
        watch.start()                                  # and with it gone, a start runs a fresh one
        assert len(watch_threads()) == 1 and watch._thread is not old
    finally:
        gate.set()
        watch.stop()
    assert watch_threads() == []


def test_stop_and_start_in_a_tight_loop_leave_at_most_one_live_watcher(tmp_path):
    watch = StallWatch(tmp_path / "stalls")
    try:
        for _ in range(30):
            watch.stop()
            watch.start()
            assert len(watch_threads()) <= 1
    finally:
        watch.stop()
    assert watch_threads() == []


# ---- a real stall -----------------------------------------------------------------------------------------------

def gil_holder_probe_helper(started: threading.Event, n: int) -> None:
    started.set()
    sum(range(n))                                      # one C call that never lets go of the interpreter lock


def seconds_per_item(attempts: int = 10) -> float:
    """The fastest observed cost of one item of ``sum(range)``. A slow sample (the machine busy) only overstates the
    cost, so the fastest of several is the one to size by; the sizing is accepted once the last three samples agree,
    and the test is skipped, with the reason, if they never do."""
    samples = []
    for _ in range(attempts):
        t0 = time.perf_counter()
        sum(range(1_000_000))
        samples.append((time.perf_counter() - t0) / 1_000_000)
        last = samples[-3:]
        if len(last) == 3 and max(last) <= 1.25 * min(last):
            return max(min(samples), 1e-9)
    pytest.skip(f"the machine is too busy to size a lock-holding call: {min(samples):.3g}..{max(samples):.3g} s/item")


class ArmSignal:
    """The real faulthandler, plus an Event the watcher sets each time it has armed the live timer."""

    def __init__(self) -> None:
        self.armed = threading.Event()

    def dump_traceback_later(self, *args, **kwargs):
        faulthandler.dump_traceback_later(*args, **kwargs)
        self.armed.set()

    def cancel_dump_traceback_later(self):
        faulthandler.cancel_dump_traceback_later()


def test_a_thread_holding_the_interpreter_in_a_c_call_is_named_by_the_live_dump(tmp_path, monkeypatch):
    """Real time, real faulthandler (no slow marker exists in this repo's pytest config, so this is not marked).
    The holder never releases the GIL for longer than LATE_S, so the Python-level watcher cannot run; the dump is
    taken by faulthandler's C thread during the stall, which is the only way the holder's frame can be in it."""
    gil_enabled = getattr(sys, "_is_gil_enabled", None)
    if gil_enabled is not None and not gil_enabled():
        pytest.skip("a free-threaded interpreter: sum(range) holds no interpreter lock, so nothing stalls")
    n = int((LATE_S + 1.0) / seconds_per_item())        # about LATE_S + 1 s of interpreter-holding work
    signal = ArmSignal()
    monkeypatch.setattr(stall_watch, "faulthandler", signal)
    watch = StallWatch(tmp_path / "stalls")
    started = threading.Event()
    watch.start()
    try:
        assert signal.armed.wait(30), "the watcher never armed the live timer"   # armed before the holder starts
        holder = threading.Thread(target=gil_holder_probe_helper, args=(started, n), name="gil-holder", daemon=True)
        holder.start()
        assert started.wait(30)
        holder.join(120)
        deadline = time.monotonic() + 30
        while not watch.dumps() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        watch.stop()
    files = watch.dumps()
    assert files, "the watcher reported no stall"
    text = files[0].read_text()
    assert "live stacks" in text
    assert "gil_holder_probe_helper" in text
