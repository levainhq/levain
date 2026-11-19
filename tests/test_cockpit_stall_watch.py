"""The cockpit stall watchdog: a fake clock and a fake sleep drive the loop with no real time, so each case is exact.
Two cases use real time and a real faulthandler timer: the stop/start race and the interpreter-holding thread."""
from __future__ import annotations

import faulthandler
import os
import stat
import sys
import threading
import time

import pytest

from levain.cockpit import stall_watch
from levain.cockpit.stall_watch import INTERVAL_S, KEEP_DUMPS, LATE_S, MIN_DUMP_GAP_S, StallWatch


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
        scratch = stalls / stall_watch.SCRATCH_NAME
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


def test_an_arm_that_fails_turns_the_live_capture_off_and_the_fallback_still_dumps(tmp_path, monkeypatch, capsys):
    class Broken(FakeFaulthandler):
        def dump_traceback_later(self, *a, **k):
            raise RuntimeError("no timer")

    monkeypatch.setattr(stall_watch, "faulthandler", Broken())
    watch = drive(tmp_path, [LATE_S + 0.5])
    assert "live capture off: RuntimeError: no timer" in capsys.readouterr().err
    assert "fallback stacks" in watch.dumps()[0].read_text()
    assert list(p.name for p in tmp_path.iterdir() if "scratch" in p.name) == []


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


def test_a_thread_holding_the_interpreter_in_a_c_call_is_named_by_the_live_dump(tmp_path):
    """Real time, real faulthandler (no slow marker exists in this repo's pytest config, so this is not marked).
    The holder never releases the GIL for longer than LATE_S, so the Python-level watcher cannot run; the dump is
    taken by faulthandler's C thread during the stall, which is the only way the holder's frame can be in it."""
    t0 = time.perf_counter()
    sum(range(2_000_000))
    per_item = max((time.perf_counter() - t0) / 2_000_000, 1e-9)
    n = int(3.0 / per_item)                            # about three seconds of interpreter-holding work
    watch = StallWatch(tmp_path / "stalls")
    started = threading.Event()
    watch.start()
    try:
        time.sleep(0.5)                                # let the watcher arm its first ticks
        holder = threading.Thread(target=gil_holder_probe_helper, args=(started, n), name="gil-holder", daemon=True)
        holder.start()
        holder.join(60)
        for _ in range(100):
            if watch.dumps():
                break
            time.sleep(0.1)
    finally:
        watch.stop()
    files = watch.dumps()
    assert files, "the watcher reported no stall"
    text = files[0].read_text()
    assert "live stacks" in text
    assert "gil_holder_probe_helper" in text
