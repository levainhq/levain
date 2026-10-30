"""The coordinator watcher inside ``levain serve`` (C10 S1): a thread running ``watch_once`` on a cadence, under the
store's single-flight lock that a hand-run ``levain coord watch`` takes too. A fake ``claude`` replays the measured
outputs and records every call; no test here runs the real one, a real serve loop, or a browser."""
from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from levain.coordinator import launch as cl
from levain.coordinator import supervise
from levain.coordinator.store import CoordStore
from tests.test_coordinator_launch import _FAKE, HID, OTHER, SID, Fake, _coordinator_dir

REPO = Path(__file__).resolve().parents[1]
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"      # what launchd hands a user agent that names no PATH of its own


def _install_fake(d: Path) -> Fake:
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "claude"
    exe.write_text(_FAKE.format(python=sys.executable, HID=HID, SID=SID))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    f = Fake(d)
    f.mode("ok")
    f.others([OTHER])
    return f


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(cl, "READBACK_DELAY", 0.0)
    monkeypatch.setattr(cl, "UNLISTED_RECHECK", 0.0)


@pytest.fixture
def fake(tmp_path, monkeypatch, quick):
    f = _install_fake(tmp_path / "fakebin")
    monkeypatch.setenv("PATH", f"{f.dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return f


@pytest.fixture(autouse=True)
def _levain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "levain-home"))


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "coord-install"
    d.mkdir()
    return _coordinator_dir(d)


@pytest.fixture
def live_hook(monkeypatch):
    monkeypatch.setattr(cl, "hook_live", lambda d: (True, "wired to this install"))


@pytest.fixture
def store(qdir, workdir):
    s = CoordStore(qdir)
    s.coord_set_agent("claude", model="haiku", cwd=str(workdir.resolve()))
    return s


def started(store):
    d, _ = store.accept_drop("fix the build", surface="cli")
    launch_id = store.woken_launch(d.drop_id)
    got = cl.launch_session(store, launch_id)
    assert got.started, got
    return store.launch(launch_id)


def ended_events(qdir, launch_id):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        return c.execute("SELECT count(*) FROM launch_events WHERE launch_id = ? AND kind = 'ended'",
                         (launch_id,)).fetchone()[0]
    finally:
        c.close()


def _source(tmp_path):
    from levain.dashboard import AnnealPaths, SubstrateSource

    (tmp_path / "memory.db").write_bytes(b"")
    return SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "memory.db"))


def watcher_threads():
    return [t for t in threading.enumerate() if t.name == "levain-coord-watcher" and t.is_alive()]


# --- with the serve up and no terminal watcher -------------------------------------------------------------------
def test_a_released_coordinators_session_is_stopped_within_one_pass(store, fake, live_hook):
    ln = started(store)
    store.coord_release()                               # the operator's Stop: the lease and the launch end
    assert store.launch(ln.launch_id).state == "ended" and fake.stops() == []
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append).start()
    try:
        assert w.wait_passes(1, 30)
    finally:
        assert w.stop(10)
    assert fake.stops() == [["stop", HID]]
    assert any(line.startswith(f"launch {ln.launch_id} ended") and f"claude stop {HID}" in line for line in out), out


@pytest.mark.parametrize("wrote, cause", [(False, "0 turns"), (True, "gone")])
def test_a_session_claude_stop_killed_is_written_ended(store, fake, live_hook, wrote, cause, qdir):
    ln = started(store)
    if wrote:
        store.coord_claim(ln.launch_id, ln.session_key)
    fake.gone()                                         # `claude stop` by hand: the session is no longer listed
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append).start()
    try:
        assert w.wait_passes(1, 30)
    finally:
        assert w.stop(10)
    got = store.launch(ln.launch_id)
    assert got.state == "ended" and got.cause == cause
    assert f"launch {ln.launch_id} ended: {cause} (its session is no longer listed)" in out
    assert ended_events(qdir, ln.launch_id) == 1


def test_the_serve_runs_the_watcher_while_it_is_up_and_ends_it_with_the_serve(store, fake, live_hook, tmp_path,
                                                                              monkeypatch):
    import levain.web_server as ws

    ln = started(store)
    store.coord_release()
    monkeypatch.setattr(supervise, "default_store", lambda: store)
    passed = threading.Event()
    real = cl.watch_once

    def watch_once(s):
        try:
            return real(s)
        finally:
            passed.set()

    monkeypatch.setattr(cl, "watch_once", watch_once)
    monkeypatch.setattr(ws, "_resolve_source", lambda p: _source(tmp_path))

    class _Httpd:
        server_address = ("127.0.0.1", 7464)
        write_token = None
        closed = False

        def serve_forever(self):
            assert passed.wait(30)                      # the serve is up; no terminal watcher runs
            raise KeyboardInterrupt

        def server_close(self):
            _Httpd.closed = True

    monkeypatch.setattr(ws, "make_server", lambda *a, **k: _Httpd())
    assert ws.run_web_server(tmp_path, open_browser=False, write=True) == 0
    assert fake.stops() == [["stop", HID]] and store.launch(ln.launch_id).state == "ended"
    assert _Httpd.closed and watcher_threads() == []


def test_a_read_only_serve_runs_no_watcher(tmp_path, monkeypatch):
    import levain.web_server as ws

    calls = []
    monkeypatch.setattr(supervise, "default_store", lambda: calls.append(1))
    monkeypatch.setattr(ws, "_resolve_source", lambda p: _source(tmp_path))

    class _Httpd:
        server_address = ("127.0.0.1", 7465)
        write_token = None

        def serve_forever(self):
            assert watcher_threads() == []
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(ws, "make_server", lambda *a, **k: _Httpd())
    assert ws.run_web_server(tmp_path, open_browser=False, write=False) == 0
    assert calls == []


def test_no_coordinator_store_is_watched_and_none_is_made(monkeypatch):
    from levain.consent.queue import default_queue_dir

    out: list[str] = []
    w = supervise.Watcher(every=3600, emit=out.append)
    assert w.run_pass() == [] and out == []
    assert not (default_queue_dir() / "dispatch.db").exists()


# --- one pass at a time: the serve's watcher and a hand-run watch ------------------------------------------------
def _block_first_list(monkeypatch):
    """The first `claude agents` read of a pass waits for ``go``, once ``entered`` says the pass is inside the lock."""
    entered, go = threading.Event(), threading.Event()
    real = cl._observed
    first = [True]

    def observed(exe, env, cwd):
        if first[0]:
            first[0] = False
            entered.set()
            assert go.wait(30)
        return real(exe, env, cwd)

    monkeypatch.setattr(cl, "_observed", observed)
    return entered, go


def test_a_hand_run_pass_beside_the_serves_does_nothing_and_nothing_is_stopped_twice(store, fake, live_hook, qdir,
                                                                                    monkeypatch):
    ln = started(store)
    store.coord_release()
    entered, go = _block_first_list(monkeypatch)
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append).start()
    try:
        assert entered.wait(30)                         # the serve's pass holds the lock, mid-pass
        lines = cl.watch_once(CoordStore(qdir))         # the hand-run `levain coord watch --once`, another thread
        assert lines == [f"another watcher pass is running over {qdir}: this pass did nothing"]
        assert fake.stops() == []
        go.set()
        assert w.wait_passes(1, 30)
    finally:
        go.set()
        assert w.stop(10)
    assert fake.stops() == [["stop", HID]]
    cl.watch_once(CoordStore(qdir))                     # the lock is free again; the session is gone from the list
    assert fake.stops() == [["stop", HID]]
    assert ended_events(qdir, ln.launch_id) == 1


def test_many_passes_at_once_stop_once_and_end_once(store, fake, live_hook, qdir, monkeypatch):
    # a slow stop widens the window in which a second, unlocked pass would list the session and stop it again
    ln = started(store)
    store.coord_release()
    real_stop = cl._stop

    def slow_stop(*a, **kw):
        time.sleep(0.3)
        return real_stop(*a, **kw)

    monkeypatch.setattr(cl, "_stop", slow_stop)
    barrier = threading.Barrier(6)
    results: list[list[str]] = []

    def one():
        barrier.wait(30)
        results.append(cl.watch_once(CoordStore(qdir)))

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 6
    assert fake.stops() == [["stop", HID]]
    assert ended_events(qdir, ln.launch_id) == 1


def test_many_passes_at_once_end_a_gone_session_once(store, fake, live_hook, qdir):
    ln = started(store)
    fake.gone()
    barrier = threading.Barrier(6)
    results: list[list[str]] = []

    def one():
        barrier.wait(30)
        results.append(cl.watch_once(CoordStore(qdir)))

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(results) == 6
    assert sum(1 for lines in results for x in lines if x.startswith(f"launch {ln.launch_id} ended: 0 turns")) == 1
    assert ended_events(qdir, ln.launch_id) == 1 and store.launch(ln.launch_id).cause == "0 turns"


def _child_env(fake_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO)
    return env


# the hand-run watch's own pass in another process; the account home the coordinator record is read under is the
# test's (the conftest's patch does not reach a child process)
_HAND_RUN = """
import sys
from pathlib import Path
from levain.coordinator import door, launch
from levain.coordinator.envelopes import EnvelopeStore
door._own_home = lambda: Path(sys.argv[2])
for line in launch.watch_once(EnvelopeStore(Path(sys.argv[1]))):
    print(line)
"""


def test_a_hand_run_watch_process_beside_the_serves_pass_does_nothing(store, fake, live_hook, qdir, tmp_path,
                                                                     monkeypatch):
    ln = started(store)
    store.coord_release()
    entered, go = _block_first_list(monkeypatch)
    w = supervise.Watcher(lambda: store, every=3600, emit=lambda line: None).start()
    try:
        assert entered.wait(30)
        own = tmp_path / "child-account-home"
        own.mkdir()
        cp = subprocess.run([sys.executable, "-c", _HAND_RUN, str(qdir), str(own)],
                            cwd=REPO, env=_child_env(fake.dir), capture_output=True, text=True, timeout=120)
        assert cp.returncode == 0, cp.stderr
        assert cp.stdout.splitlines() == [f"another watcher pass is running over {qdir}: this pass did nothing"]
        assert fake.stops() == []                       # the other process stopped nothing
        go.set()
        assert w.wait_passes(1, 30)
    finally:
        go.set()
        assert w.stop(10)
    assert fake.stops() == [["stop", HID]]
    assert ended_events(qdir, ln.launch_id) == 1


_HOLDER = """
import sys
from pathlib import Path
from levain.coordinator import launch
with launch._single_flight(Path(sys.argv[1])) as held:
    print("held" if held else "busy", flush=True)
    sys.stdin.readline()
"""


def test_the_serves_pass_does_nothing_while_another_process_holds_the_lock(store, fake, live_hook, qdir):
    ln = started(store)
    store.coord_release()
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(qdir)], cwd=REPO, env=_child_env(fake.dir),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        out: list[str] = []
        w = supervise.Watcher(lambda: store, every=3600, emit=out.append)
        assert w.run_pass() == [f"another watcher pass is running over {qdir}: this pass did nothing"]
        assert fake.stops() == []
    finally:
        holder.communicate("\n", timeout=30)
    assert holder.returncode == 0
    supervise.Watcher(lambda: store, every=3600, emit=lambda line: None).run_pass()     # released with its process
    assert fake.stops() == [["stop", HID]]
    assert ended_events(qdir, ln.launch_id) == 1


def test_the_lock_is_a_private_file_in_the_store_directory(store, fake, qdir):
    cl.watch_once(store)
    lock = qdir / cl.WATCH_LOCK
    assert lock.is_file() and not lock.is_symlink() and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_lock_that_cannot_be_opened_faults_the_pass_and_nothing_runs(store, fake, live_hook, qdir):
    ln = started(store)
    store.coord_release()
    (qdir / cl.WATCH_LOCK).symlink_to(qdir / "elsewhere")      # never followed
    from levain.coordinator.store import CoordError

    with pytest.raises(CoordError, match="lock"):
        cl.watch_once(store)
    assert fake.stops() == [] and store.launch(ln.launch_id).state == "ended"


# --- faults and shutdown ---------------------------------------------------------------------------------------
def test_a_faulting_pass_is_logged_and_the_next_one_runs(store, monkeypatch):
    from levain.dispatch.jobs import JobStoreError

    calls = []

    def once(s):
        calls.append(1)
        if len(calls) == 1:
            raise JobStoreError("dispatch.db said no")
        return ["fine"]

    monkeypatch.setattr(cl, "watch_once", once)
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=0.05, emit=out.append).start()
    try:
        assert w.wait_passes(2, 30)
    finally:
        assert w.stop(10)
    assert out[0] == "watch pass faulted (JobStoreError: dispatch.db said no); next pass in 0.05s"
    assert "fine" in out[1:]


def test_an_opener_that_faults_is_logged_and_a_log_that_faults_ends_nothing(store, monkeypatch):
    opened = []

    def opener():
        opened.append(1)
        if len(opened) == 1:
            raise OSError("the queue directory moved")
        return None

    def broken_log(line):
        raise OSError("log full")

    w = supervise.Watcher(opener, every=0.05, emit=broken_log).start()
    try:
        assert w.wait_passes(3, 30)
    finally:
        assert w.stop(10)


def test_repeated_lines_are_logged_once_and_again_when_they_change(store, monkeypatch):
    said = iter([["claude is not on PATH: no session observed"]] * 3 + [["launch x started"]]
                + [["claude is not on PATH: no session observed"]])
    monkeypatch.setattr(cl, "watch_once", lambda s: next(said))
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append)
    for _ in range(5):
        w.run_pass()
    assert out == ["claude is not on PATH: no session observed", "launch x started",
                   "claude is not on PATH: no session observed"]


def test_a_serve_that_stops_mid_wait_ends_the_thread_and_no_pass_runs_after(store, monkeypatch):
    calls = []
    monkeypatch.setattr(cl, "watch_once", lambda s: calls.append(1) or [])
    w = supervise.Watcher(lambda: store, every=3600, emit=lambda line: None).start()
    assert w.wait_passes(1, 30)                         # the first pass ran; the thread now waits out its cadence
    began = time.monotonic()
    assert w.stop(10)
    assert time.monotonic() - began < 5                 # the wait ends at once, not at the cadence
    assert watcher_threads() == []
    assert calls == [1] and w.passes == 1


def test_a_watcher_stopped_before_its_first_pass_runs_none(store, monkeypatch):
    calls = []
    monkeypatch.setattr(cl, "watch_once", lambda s: calls.append(1) or [])
    w = supervise.Watcher(lambda: store, every=0.05, emit=lambda line: None)
    w.stop()
    w.start()
    w._thread.join(10)
    assert not w._thread.is_alive() and calls == [] and w.passes == 0


def test_stop_waits_for_a_pass_in_flight(store, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    calls = []

    def slow(s):
        calls.append(1)
        inside.set()
        assert release.wait(30)
        return []

    monkeypatch.setattr(cl, "watch_once", slow)
    w = supervise.Watcher(lambda: store, every=0.05, emit=lambda line: None).start()
    assert inside.wait(30)
    assert w.stop(0.1) is False                         # still in its pass: not left half-done by the stop
    release.set()
    assert w._thread is not None
    w._thread.join(10)
    assert not w._thread.is_alive() and calls == [1]    # and no pass after it


# --- the serve under launchd's minimal PATH (C10 D2 ruling, 10-29) ---------------------------------------------
def test_under_launchds_minimal_path_the_watcher_finds_claude_and_stops_the_session(store, live_hook, quick,
                                                                                    monkeypatch):
    # the native installer puts claude in ~/.local/bin; launchd hands the serve none of the shell's PATH
    f = _install_fake(Path.home() / ".local" / "bin")
    monkeypatch.setenv("PATH", f"{f.dir}:{LAUNCHD_PATH}")
    started(store)                                      # started from a shell that had it
    store.coord_release()
    monkeypatch.setenv("PATH", LAUNCHD_PATH)            # and stopped by the serve launchd runs
    assert cl.shutil.which("claude") is None            # the minimal PATH alone does not name it
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append).start()
    try:
        assert w.wait_passes(1, 30)
    finally:
        assert w.stop(10)
    assert f.stops() == [["stop", HID]], out
    assert not any("not on PATH" in line for line in out)
    exe, env = cl._harness()
    assert exe == str(Path.home() / ".local" / "bin" / "claude")
    assert env["PATH"].split(os.pathsep)[:4] == LAUNCHD_PATH.split(":")    # the minimal PATH first, then the rest
    assert str(Path.home() / ".local" / "bin") in env["PATH"].split(os.pathsep)


def test_under_launchds_minimal_path_a_pending_launch_starts(store, live_hook, quick, monkeypatch):
    f = _install_fake(Path.home() / ".local" / "bin")
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    store.accept_drop("from the phone", surface="cli")              # a drop that started nothing itself
    out: list[str] = []
    supervise.Watcher(lambda: store, every=3600, emit=out.append).run_pass()
    assert any(f"started: claude attach {HID}" in line for line in out), out
    assert len(f.spawns()) == 1


def test_under_launchds_minimal_path_a_claude_nowhere_refuses_in_the_serve_log(store, live_hook, quick, monkeypatch):
    monkeypatch.setenv("PATH", LAUNCHD_PATH)
    monkeypatch.setattr(cl, "_harness_dirs", lambda: (str(Path.home() / ".local" / "bin"),))     # nothing there
    out: list[str] = []
    w = supervise.Watcher(lambda: store, every=3600, emit=out.append)
    w.run_pass()
    assert "claude is not on PATH: no session observed" in out


def test_a_claude_on_path_is_taken_as_it_is(fake):
    exe, env = cl._harness()
    assert exe == str(fake.dir / "claude") and env["PATH"] == os.environ["PATH"]
