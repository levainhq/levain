"""levain.dispatch.follow_run: the follower's runner loop and its entry.

CONTROL is real where it can be: test_dispatch_follow's served rig (the verifier rig's event journal on a
`ControlServer` socket) and its job store; the loop's waits are a recording seam, so a backoff is read, never slept."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.dispatch import follow as F
from levain.dispatch import follow_run as FR
from levain.slot import control as C
from levain.slot import control_client as CC
from tests.test_dispatch_follow import (Dispatch, acked, artifact_follower, exported, published,  # noqa: F401
                                        served, setup_verifier)
from tests.test_dispatch_jobs import at_state, base_tree, clock, model, repo, store  # noqa: F401 - fixtures
from tests.test_routemaps import st  # noqa: F401 - the signed store fixture
from tests.test_tickets import DISPATCH_UID


def until(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Waits:
    """The runner's wait seam: records each delay; ``then(i)`` runs before wait ``i`` returns; stops after ``stop_at``."""

    def __init__(self, stop_at: int | None = None, then=None) -> None:
        self.delays: list[float] = []
        self.stop_at, self.then = stop_at, then or (lambda i: None)

    def __call__(self, delay: float) -> bool:
        self.delays.append(delay)
        self.then(len(self.delays))
        return self.stop_at is not None and len(self.delays) >= self.stop_at


def in_thread(runner: FR.FollowRunner) -> threading.Thread:
    t = threading.Thread(target=runner.run, daemon=True)
    t.start()
    return t


def test_a_live_stream_runs_through_to_the_ack(store, repo, served):  # noqa: F811
    """The real path in one process: the runner steps a follower of the served CONTROL, the verifier's terminal event
    is recorded and acked, the cursor moves, and stop() ends the run."""
    d = setup_verifier(store, repo, served)
    runner = FR.FollowRunner(d.follower())
    t = in_thread(runner)
    assert until(lambda: acked(served))
    assert until(lambda: store.event_cursor() == (served.events.journal_id, served.kept()[-1]["seq"]))
    runner.stop()
    t.join(5.0)
    assert not t.is_alive()


def test_control_down_backs_off_doubling_then_resumes_and_converges(store, repo, served):  # noqa: F811
    """No CONTROL at the socket: each step is refused control-down and waited out, 1 then 2 (first_s doubling); once
    CONTROL serves the socket the next step takes the event and acks it."""
    d = setup_verifier(store, repo, served)
    path = Path(tempfile.mkdtemp(dir="/tmp")) / "c.sock"
    servers: list = []

    def then(i: int) -> None:
        if i == 2:
            server = C.ControlServer(served.control, path, peer_uid=lambda _s: DISPATCH_UID)
            server.start()
            servers.append(server)

    waits = Waits(then=then)
    f = F.EventFollower.at(store, path, d.resolve, read_deadline=0.5)
    runner = FR.FollowRunner(f, first_s=1.0, max_s=8.0, lasting_s=100.0, wait=waits)
    t = in_thread(runner)
    try:
        assert until(lambda: acked(served))
        runner.stop()
        t.join(5.0)
        assert not t.is_alive()
        assert waits.delays[:2] == [1.0, 2.0] and all(d <= 8.0 for d in waits.delays)
        assert [r.run for r in store.run_records(d.job.id)] == [0]
    finally:
        for s in servers:
            s.stop()


def test_a_lasting_cause_waits_the_long_wait_every_time_and_never_acks(store, repo, served):  # noqa: F811
    """An unresolved verifier ticket names a cause no retry mends: every stop waits lasting_s (no doubling from
    first_s, no hot loop), the event stays unacked and the cursor before it."""
    d = setup_verifier(store, repo, served)
    waits = Waits(stop_at=3)
    f = F.EventFollower.at(store, served.path, lambda _ev: None, read_deadline=0.5)
    runner = FR.FollowRunner(f, first_s=1.0, max_s=8.0, lasting_s=100.0, wait=waits)
    runner.run()
    assert waits.delays == [100.0, 100.0, 100.0]
    assert not acked(served)
    cur = store.event_cursor()
    assert cur is None or cur[1] < served.kept()[-1]["seq"]


def test_stop_ends_a_step_blocked_on_a_quiet_stream(store, served):  # noqa: F811
    """No read deadline and nothing to read: the step blocks on the stream; stop() ends it CANCELLED and run returns."""
    f = F.EventFollower.at(store, served.path, lambda _ev: None, read_deadline=None)
    runner = FR.FollowRunner(f, wait=Waits(stop_at=1))
    t = in_thread(runner)
    assert until(lambda: f._active is not None)
    runner.stop()
    t.join(5.0)
    assert not t.is_alive()


class FakeJobs:
    def __init__(self) -> None:
        self.cursor = None

    def event_cursor(self):
        return self.cursor


class FakeFollower:
    """A follower whose steps are a script: a Stepped is returned, an exception raised, a callable called first."""

    def __init__(self, script) -> None:
        self.jobs = FakeJobs()
        self.script = list(script)
        self.closed = 0
        self.steps = 0

    def step(self):
        self.steps += 1
        item = self.script.pop(0) if self.script else F.Stepped(CC.CLOSED)
        if callable(item):
            item = item(self)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed += 1


def moved(f) -> F.Stepped:
    f.jobs.cursor = ("j", (f.jobs.cursor or ("j", 0))[1] + 1)
    return F.Stepped(CC.CLOSED)


def test_a_stream_ending_with_nothing_read_backs_off_and_progress_resets_it():
    """CLOSED with the cursor where it was waits like a transient stop, doubling up to max_s; a step that moves the
    cursor is followed at once and the next wait starts from first_s again; a DEADLINE is followed at once."""
    closed = F.Stepped(CC.CLOSED)
    f = FakeFollower([closed, closed, closed, closed, moved, F.Stepped(CC.DEADLINE), closed])
    waits = Waits(stop_at=5)
    FR.FollowRunner(f, first_s=1.0, max_s=4.0, lasting_s=100.0, wait=waits).run()
    assert waits.delays == [1.0, 2.0, 4.0, 4.0, 1.0]
    assert f.steps == 7 and f.closed == 1


def test_transient_stops_double_and_lasting_ones_do_not():
    f = FakeFollower([F.FollowStopped("control-down"), F.FollowStopped("artifact-io"),
                      F.FollowStopped("take-bundle-absent"), F.FollowStopped("record-conflict"),
                      F.FollowStopped("control-down")])
    waits = Waits(stop_at=5)
    FR.FollowRunner(f, first_s=1.0, max_s=60.0, lasting_s=100.0, wait=waits).run()
    assert waits.delays == [1.0, 2.0, 4.0, 100.0, 16.0]


def test_a_stop_after_progress_restarts_the_backoff():
    def stopped_after_moving(f):
        moved(f)
        return F.FollowStopped("control-down")

    f = FakeFollower([F.FollowStopped("control-down"), F.FollowStopped("control-down"), stopped_after_moving])
    waits = Waits(stop_at=3)
    FR.FollowRunner(f, first_s=1.0, max_s=60.0, lasting_s=100.0, wait=waits).run()
    assert waits.delays == [1.0, 2.0, 1.0]


def test_stop_ends_a_long_wait_at_once():
    """The default wait is the stop event's: a stop during the lasting wait ends run now, not after lasting_s."""
    f = FakeFollower([F.FollowStopped("record-conflict")] * 3)
    runner = FR.FollowRunner(f, lasting_s=100.0)
    t = in_thread(runner)
    assert until(lambda: f.steps >= 1)
    start = time.monotonic()
    runner.stop()
    t.join(5.0)
    assert not t.is_alive() and time.monotonic() - start < 5.0 and f.steps == 1


def test_another_exception_ends_run_and_closes_the_follower():
    f = FakeFollower([RuntimeError("the store is broken")])
    with pytest.raises(RuntimeError):
        FR.FollowRunner(f, wait=Waits()).run()
    assert f.closed == 1


def test_a_cancelled_step_ends_run_without_a_wait():
    f = FakeFollower([F.Stepped(CC.CANCELLED)])
    waits = Waits()
    FR.FollowRunner(f, wait=waits).run()
    assert waits.delays == [] and f.steps == 1


def test_a_stop_between_the_cursor_read_and_the_step_enters_no_step():
    """A stop that lands after the loop's check, in the cursor read just before the step, enters no step at all."""
    f = FakeFollower([])
    runner = FR.FollowRunner(f, wait=Waits())
    f.jobs.event_cursor = lambda: runner.stop()
    runner.run()
    assert f.steps == 0 and f.closed >= 1


def test_a_step_that_raises_once_stop_is_set_is_the_clean_end():
    """A stop that lands during a step on a closed follower: whatever that raises (a closed socket, a closed store
    handle) is the stop, not a failure."""
    runner: list = []

    class Closed(FakeFollower):
        def step(self):
            self.steps += 1
            runner[0].stop()
            raise OSError("bad file descriptor")

    f = Closed([])
    runner.append(FR.FollowRunner(f, wait=Waits()))
    runner[0].run()                                             # does not raise
    assert f.steps == 1 and f.closed >= 1


def test_a_step_that_raises_with_no_stop_still_reaches_the_caller():
    f = FakeFollower([OSError("disk gone")])
    with pytest.raises(OSError):
        FR.FollowRunner(f, wait=Waits()).run()


def test_an_unknown_outcome_waits_the_long_wait_even_when_the_cursor_moved():
    def unknown_after_moving(f):
        moved(f)
        return F.Stepped("something-new")

    f = FakeFollower([unknown_after_moving, F.Stepped("something-new")])
    waits = Waits(stop_at=2)
    FR.FollowRunner(f, first_s=1.0, max_s=4.0, lasting_s=100.0, wait=waits).run()
    assert waits.delays == [100.0, 100.0]


def test_a_stop_name_is_retryable_only_where_a_later_attempt_can_succeed():
    assert {"control-down", "artifact-io", "take-failed", "take-bundle-absent"} <= FR.RETRYABLE
    assert not ({"ticket-unresolved", "record-conflict", "event-shape"} & FR.RETRYABLE)


def test_the_signal_handler_takes_no_lock_a_second_signal_could_find_held(store):  # noqa: F811
    """A second SIGTERM/SIGINT arrives on the main thread while the first handler is running there. The handler only
    records the request, so it returns while the follower's lock is held (as it is inside close())."""
    f = F.EventFollower.at(store, "/nonexistent/c.sock", lambda _ev: None)
    requested: list = []
    handler = FR._stop_request_handler(requested)
    returned = threading.Event()
    with f._lock:
        t = threading.Thread(target=lambda: (handler(signal.SIGTERM, None), handler(signal.SIGINT, None),
                                             returned.set()), daemon=True)
        t.start()
        assert returned.wait(5.0)
    assert len(requested) == 2


# -- the entry ----------------------------------------------------------------------------------------------------


def test_main_takes_an_artifact_acks_it_and_ends_on_sigterm(store, served, tmp_path, monkeypatch):  # noqa: F811
    """main() in this process: its own JobStore at --store-dir, the follower of --socket, takes from --export-dir;
    the artifact event is taken and acked, and the installed SIGTERM handler, called directly, ends main with 0."""
    session, ticket, seq = published(served)
    (tmp_path / "export").mkdir()
    export = exported(tmp_path / "export", session, ticket)
    done: list = []

    handlers: dict = {}
    runners: list = []
    real_signal, real_runner = signal.signal, FR.FollowRunner

    def recording_signal(num, h):
        handlers[num] = h
        return real_signal(num, h)

    class Recording(real_runner):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            runners.append(self)

    monkeypatch.setattr(signal, "signal", recording_signal)
    monkeypatch.setattr(FR, "FollowRunner", Recording)

    def term() -> None:
        ok = until(lambda: served.control.store.get(session).fields.get("artifact_acked") is True)
        done.append(ok)
        # the handler main() installed is called directly: nothing signals this process, and a failure ends main
        # on a bounded wait instead of hanging it
        if ok and until(lambda: signal.SIGTERM in handlers):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        elif runners:
            runners[0].stop()

    t = threading.Thread(target=term, daemon=True)
    t.start()
    rc = FR.main(["--store-dir", str(store.directory), "--socket", str(served.path), "--export-dir", str(export)])
    t.join(5.0)
    assert rc == 0 and done == [True]
    assert signal.SIGTERM in handlers  # main() installed its handler: the fallback stop alone must not pass this test
    assert store.event_cursor()[1] >= seq
    assert store.taken_artifact(session, ticket) is not None


def test_the_module_run_as_a_program_acks_and_exits_0_on_sigterm(store, served, tmp_path):  # noqa: F811
    """``python -m levain.dispatch.follow_run`` as its own process (this worktree's levain on PYTHONPATH, so not
    ``-I``): it follows this process's served CONTROL, takes and acks the artifact, and exits 0 on SIGTERM."""
    session, ticket, seq = published(served)
    (tmp_path / "export").mkdir()
    export = exported(tmp_path / "export", session, ticket)
    root = Path(FR.__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    proc = subprocess.Popen([sys.executable, "-m", "levain.dispatch.follow_run", "--store-dir", str(store.directory),
                             "--socket", str(served.path), "--export-dir", str(export)], env=env, cwd=str(root),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert until(lambda: served.control.store.get(session).fields.get("artifact_acked") is True, 20.0)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=10.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err.decode()
    assert store.event_cursor()[1] >= seq and store.taken_artifact(session, ticket) is not None


def test_two_sigterms_in_a_row_still_end_the_program_with_0(store, served, tmp_path):  # noqa: F811
    session, ticket, seq = published(served)
    (tmp_path / "export").mkdir()
    export = exported(tmp_path / "export", session, ticket)
    root = Path(FR.__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    proc = subprocess.Popen([sys.executable, "-m", "levain.dispatch.follow_run", "--store-dir", str(store.directory),
                             "--socket", str(served.path), "--export-dir", str(export)], env=env, cwd=str(root),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert until(lambda: served.control.store.get(session).fields.get("artifact_acked") is True, 20.0)
        proc.send_signal(signal.SIGTERM)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=10.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err.decode()


def test_main_restores_the_signal_handlers_only_after_the_runner_has_ended(store, served, tmp_path, monkeypatch):  # noqa: F811
    """A SIGTERM during a slow final step must still reach our handler, so the previous handlers come back last."""
    events: list = []
    handlers: dict = {}
    real_signal, real_runner = signal.signal, FR.FollowRunner

    def recording_signal(num, h):
        handlers[num] = h
        events.append(("signal", num))
        return real_signal(num, h)

    class Recording(real_runner):
        def run(self) -> None:
            try:
                super().run()
            finally:
                events.append("ended")

    monkeypatch.setattr(signal, "signal", recording_signal)
    monkeypatch.setattr(FR, "FollowRunner", Recording)
    (tmp_path / "export").mkdir()

    def term() -> None:
        if until(lambda: signal.SIGTERM in handlers):
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    threading.Thread(target=term, daemon=True).start()
    assert FR.main(["--store-dir", str(store.directory), "--socket", str(served.path),
                    "--export-dir", str(tmp_path / "export")]) == 0
    assert events.index("ended") < max(i for i, e in enumerate(events) if e != "ended" and e[1] == signal.SIGTERM)
    assert events.count(("signal", signal.SIGTERM)) == 2


def test_a_previous_handler_of_none_is_restored_as_the_default():
    restored: list = []
    FR._restore_handlers({signal.SIGTERM: None, signal.SIGINT: signal.SIG_IGN}, lambda s, h: restored.append((s, h)))
    assert restored == [(signal.SIGTERM, signal.SIG_DFL), (signal.SIGINT, signal.SIG_IGN)]


def test_a_held_lock_is_refused_before_the_store_is_opened(tmp_path):
    d = tmp_path / "store"
    d.mkdir(mode=0o700)
    fd = FR.take_instance_lock(d)
    assert fd is not None
    try:
        assert FR.main(["--store-dir", str(d), "--socket", "/nonexistent/c.sock"]) == FR.EX_INSTANCE_HELD
        assert not (d / "dispatch.db").exists()
    finally:
        os.close(fd)


def test_main_exits_tempfail_while_another_runner_holds_the_store(store):  # noqa: F811
    store.event_cursor()
    fd = FR.take_instance_lock(Path(store.directory))
    assert fd is not None
    try:
        rc = FR.main(["--store-dir", str(store.directory), "--socket", "/nonexistent/c.sock"])
        assert rc == FR.EX_INSTANCE_HELD
    finally:
        os.close(fd)
    fd = FR.take_instance_lock(Path(store.directory))      # released with its holder
    assert fd is not None
    os.close(fd)


def test_main_refuses_a_relative_path(tmp_path):
    with pytest.raises(SystemExit) as e:
        FR.main(["--store-dir", "relative/store"])
    assert e.value.code == 2
