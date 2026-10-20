"""S1 session 2 fix round 5, lane FX6: the supervisor's relay bounds (``S2_L3_r2_TRIAGE.md`` "L3 r4 triage",
r4-1 .. r4-5). A stop signal is not kept waiting on a terminal that trickles; a master still written after the helper's
exit is read for a bounded time; the after-exit cap holds to the byte; the `reset` advice is not given to a terminal
that is gone; held status lines never overtake what the stderr wrapper still buffers.
"""
from __future__ import annotations

import os
import re
import signal
import threading
import time
from pathlib import Path

import pytest

from levain.slot import supervisor as SV
from tests.test_slot_s2fx5_relay_exit import FX5_HELPER, closing, drain, stalled
from tests.test_slot_s2s_supervisor import Rig, bounded, isolated_git  # noqa: F401
from tests.test_slot_s2fx3_supervisor import rig as fx3_rig

FX6_HOOKS = r'''
    if mode == "orphan":
        import signal
        if os.fork() == 0:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            with open(os.path.join(store, "pid-orphan"), "w") as f:
                f.write(str(os.getpid()))
            end = time.monotonic() + 20
            try:
                while time.monotonic() < end:
                    os.write(1, b"o" * 1024)    # never quiet: every read pass finds the master ready
            except OSError:
                pass
            os._exit(0)
        while not os.path.exists(os.path.join(store, "pid-orphan")):
            time.sleep(0.01)
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode == "modes":'''

FX6_HELPER = FX5_HELPER.replace('    if mode == "modes":', FX6_HOOKS, 1)
assert FX6_HELPER.count('"orphan"') == 1


def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Rig:
    r = fx3_rig(tmp_path, monkeypatch, **env)
    r.helper.write_text(FX6_HELPER)
    return r


def no_ctty(r: Rig) -> None:
    # As FX5's after-exit test: without a controlling terminal the helper's exit leaves the pty as it is (Linux's
    # shape), so a process it left behind keeps writing to it.
    real = r.seams.spawn
    r.seams.spawn = lambda argv, **kw: real(argv, **{**kw, "ctty": False})


# --- r4-1: a stop signal that ended the relay is honoured by the final drain ------------------------------------------

@bounded(60)
def test_stop_signal_abandons_the_rest_at_once_on_a_terminal_that_trickles(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SV, "FINAL_DRAIN_MAX_S", 8.0)
    r = rig(tmp_path, monkeypatch, FAKE_START="forever")
    r.seams.start_grace_s = 1.0
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    done = threading.Event()

    def trickle() -> None:                   # small reads, well inside FINAL_DRAIN_S of each other
        while not done.is_set():
            time.sleep(0.3)
            try:
                os.read(out_r, 4096)
            except OSError:
                return
    sent: list = []

    def stop() -> None:
        deadline = time.monotonic() + 20
        while not (r.store / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)
        sent.append(time.monotonic())
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=trickle, daemon=True).start()
    threading.Thread(target=stop, daemon=True).start()
    try:
        assert r.run() == 1
        took = time.monotonic() - sent[0]
    finally:
        done.set()
        closing(out_r, out_w)
    err = r.stderr()
    assert took < 5                          # not the 8 s the trickling terminal would have kept the drain
    assert int(re.search(r"your terminal did not take the session's last (\d+) bytes", err).group(1)) > 0
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]


# --- r4-2: a master still written after the helper exited is read for a bounded time ---------------------------------

@bounded(30)
def test_a_process_left_writing_after_the_helper_exited_does_not_hold_the_relay(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch
                                                                                ) -> None:
    monkeypatch.setattr(SV, "AFTER_EXIT_S", 0.5)
    r = rig(tmp_path, monkeypatch, FAKE_START="orphan")
    no_ctty(r)
    # His terminal stalled too: the select then wakes only for the master, which the writer never lets go quiet.
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    t0 = time.monotonic()
    try:
        # `start`'s line is not the stream's last any more (start-result-unreadable, the ruled DECISION); the point is
        # that the run ends at all.
        assert r.run() == 1
        took = time.monotonic() - t0
    finally:
        closing(out_r, out_w)
        pid_file = r.store / "pid-orphan"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
    err = r.stderr()
    assert took < 15
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]
    assert re.search(r"the vendor's terminal was still being written 0\.5 s after its helper exited: the relay "
                     r"stopped reading it \(\d+ bytes it held, and whatever came after, not shown\)", err)


# --- r4-3: the after-exit cap admits only the room left, and counts the rest ------------------------------------------

@bounded(30)
def test_after_exit_cap_holds_to_the_byte_and_counts_the_excess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                 ) -> None:
    monkeypatch.setattr(SV, "_OUTPUT_MAX", 64)
    monkeypatch.setattr(SV, "_AFTER_EXIT_MAX", 100)
    monkeypatch.setattr(SV, "FINAL_DRAIN_S", 0.3)
    r = rig(tmp_path, monkeypatch, FAKE_START="trickle", FAKE_CHUNKS="1", FAKE_CHUNK="600")
    no_ctty(r)
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    try:
        assert r.run() == 0
    finally:
        closing(out_r, out_w)
    err = r.stderr()
    left = int(re.search(r"your terminal did not take the session's last (\d+) bytes", err).group(1))
    over = int(re.search(r"(\d+) bytes written to the terminal after the vendor's helper exited were not shown",
                         err).group(1))
    assert left <= 64 + 100                  # the buffer never past its bound (the filter's flush adds nothing here)
    assert left + over >= 600                # every T is either held (and counted untaken) or counted as not shown


# --- r4-4: "run `reset`" is not said to a terminal that is gone -------------------------------------------------------

@bounded(60)
def test_reset_advice_is_folded_into_the_failure_line_when_stdout_failed(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modesbig", FAKE_CHUNKS="2", FAKE_CHUNK="65536")
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    os.close(out_r)                          # gone: every write is EPIPE, the reset among them
    try:
        assert r.run() == 1
    finally:
        closing(out_w)
    err = r.stderr()
    assert "run `reset`" not in err
    fail = [x for x in err.splitlines() if "your terminal refused the session's output" in x]
    assert len(fail) == 1 and "the reset of the reporting modes the session turned on did not reach it" in fail[0]


# --- r4-5: the held lines' fd path never overtakes what the stderr wrapper still buffers ------------------------------

def _sup(r: Rig, err: object) -> SV.Supervisor:
    r.seams.err = err
    return SV.Supervisor(SV.Request(vendor="claude", repo=r.repo), r.seams)


def test_held_lines_come_out_after_what_the_wrapper_buffered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)
    err_r, err_w = os.pipe()
    err = os.fdopen(err_w, "w")
    sup = _sup(r, err)
    try:
        err.write("levain: first\n")         # in the wrapper's buffer, not yet on the fd
        sup._held = ["levain: second\n"]
        sup._release_held()
        err.flush()
        got = drain(err_r)
    finally:
        err.close()
        closing(err_r)
    assert got == b"levain: first\nlevain: second\n"
    assert sup.unshown_lines == 0


def test_held_lines_wait_behind_a_buffer_the_stalled_stderr_never_takes(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SV, "HELD_DRAIN_S", 0.3)
    r = rig(tmp_path, monkeypatch)
    err_r, err_w = stalled()
    err = os.fdopen(err_w, "w")
    sup = _sup(r, err)
    t0 = time.monotonic()
    try:
        err.write("levain: first\n")
        sup._held = ["levain: second\n", "levain: third\n"]
        sup._release_held()
        took = time.monotonic() - t0
        assert os.get_blocking(err_w)        # put back as it was
        got = drain(err_r)
    finally:
        closing(err_r)
        try:
            err.close()
        except OSError:
            pass
    assert took < 2
    assert b"second" not in got and b"third" not in got
    assert sup.unshown_lines == 2
