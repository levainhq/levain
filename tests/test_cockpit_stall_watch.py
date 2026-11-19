"""The cockpit stall watchdog: a fake clock and a fake sleep drive the loop with no real time, so each case is exact."""
from __future__ import annotations

import threading
import time

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
    return [line for line in err.splitlines() if line.startswith("cockpit stall: late ")]


def test_no_dump_and_no_stack_walk_below_the_threshold(tmp_path, capsys, monkeypatch):
    walked = []
    monkeypatch.setattr(stall_watch, "_stacks_text", lambda late: walked.append(late) or "")
    watch = drive(tmp_path, [0.0, 0.01, LATE_S - 0.01, 0.0])
    assert walked == []
    assert watch.dumps() == []
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == ""


def test_a_late_wake_writes_one_dump_and_one_stderr_line(tmp_path, capsys):
    watch = drive(tmp_path, [0.0, LATE_S + 0.5, 0.0])
    lines = late_lines(capsys.readouterr().err)
    assert lines == [f"cockpit stall: late {LATE_S + 0.5:.2f} s"]
    files = watch.dumps()
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert f"late {LATE_S + 0.5:.2f} s" in files[0].read_text()


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


def test_two_late_wakes_inside_the_gap_write_one_dump(tmp_path, capsys):
    watch = drive(tmp_path, [LATE_S + 0.5, LATE_S + 0.5])
    assert len(watch.dumps()) == 1
    # the stderr line is not rate limited: both late wakes are named
    assert len(late_lines(capsys.readouterr().err)) == 2


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


def test_start_and_stop_are_idempotent_and_stop_is_prompt():
    watch = StallWatch("/nonexistent-stall-watch-dir")
    watch.stop()                                       # before start: nothing to do, no error
    watch.start()
    watch.start()
    try:
        ours = [t for t in threading.enumerate() if t.name == "cockpit-stall-watch"]
        assert len(ours) == 1
        assert ours[0].daemon
    finally:
        began = time.monotonic()
        watch.stop()
        watch.stop()
    assert time.monotonic() - began < 1.0
    assert not [t for t in threading.enumerate() if t.name == "cockpit-stall-watch"]
    watch.start()                                      # a stopped watch starts again
    watch.stop()


def test_the_loop_asks_each_sleep_for_the_interval():
    asked = []
    fake = FakeTime([])

    def sleep(seconds: float) -> None:
        asked.append(seconds)
        if len(asked) == 3:
            watch.stop()
        fake.now += seconds

    watch = StallWatch("/nonexistent-stall-watch-dir", clock=fake.clock, sleep=sleep)
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
