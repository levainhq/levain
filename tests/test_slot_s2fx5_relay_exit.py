"""S1 session 2 fix round 4, lane FX5: the supervisor's relay exit path (``S2_L3_r2_TRIAGE.md`` "L3 r3 triage",
r3-C1 .. r3-C4). Every test runs against an operator terminal that is not reading (a pipe filled before the run,
nobody draining it): the vendor's exit, the reporting-mode reset, the held status lines and a stdout that fails all
end the run within a bound, and every byte his terminal did not get is counted and said.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from levain.slot import supervisor as SV
from tests.test_slot_s2fx3_supervisor import FX3_HELPER, gone, rig as fx3_rig, stop_when  # noqa: F401
from tests.test_slot_s2s_supervisor import SESSION, Rig, bounded, isolated_git  # noqa: F401

FX5_HOOKS = r'''
    if mode in ("trickle", "modesbig"):
        if mode == "modesbig":
            os.write(1, b"\x1b[?1000h")
        for _ in range(int(os.environ.get("FAKE_CHUNKS", "1"))):
            os.write(1, b"T" * int(os.environ.get("FAKE_CHUNK", "600")))
            time.sleep(float(os.environ.get("FAKE_GAP", "0")))
        line({"exit": 0, "ok": True})
        sys.exit(0)
    if mode == "modes":'''

FX5_HELPER = FX3_HELPER.replace('    if mode == "modes":', FX5_HOOKS, 1)
assert FX5_HELPER.count('"trickle"') == 1


def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Rig:
    r = fx3_rig(tmp_path, monkeypatch, **env)
    r.helper.write_text(FX5_HELPER)
    return r


def stalled() -> tuple:
    """His terminal, stalled: a pipe filled to the brim that nobody reads."""
    out_r, out_w = os.pipe()
    os.set_blocking(out_w, False)
    try:
        while True:
            os.write(out_w, b"F" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(out_w, True)
    return out_r, out_w


def drain(fd: int) -> bytes:
    os.set_blocking(fd, False)
    got = bytearray()
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            got += chunk
    except BlockingIOError:
        pass
    return bytes(got)


def closing(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


# --- r3-C1: the vendor exited, the master is drained, his terminal is stalled: straight to the bounded drain ----------

@bounded(60)
def test_vendor_exit_with_a_stalled_terminal_goes_to_the_bounded_drain_and_teardown(tmp_path: Path,
                                                                                    monkeypatch: pytest.MonkeyPatch
                                                                                    ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="trickle", FAKE_CHUNKS="4", FAKE_CHUNK="65536")
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    t0 = time.monotonic()
    try:
        assert r.run() == 0                  # the vendor's own exit; nothing about the session failed
        took = time.monotonic() - t0
        assert os.get_blocking(out_w)
    finally:
        closing(out_r, out_w)
    err = r.stderr()
    assert took < 20
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]
    assert f"session {SESSION} ended RETAINED" in err
    left = int(re.search(r"your terminal did not take the session's last (\d+) bytes", err).group(1))
    assert left >= 4 * 65536                 # all of it was still held, and all of it is counted


@bounded(30)
def test_master_past_the_cap_is_still_read_once_the_helper_exited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                   ) -> None:
    # The buffer is full and his terminal stalled when the helper exits with more (and `start`'s line) in the pty:
    # holding the master back now would hold the session's end forever. Past the after-exit allowance the bytes are
    # counted, not kept.
    monkeypatch.setattr(SV, "_OUTPUT_MAX", 64)
    monkeypatch.setattr(SV, "_AFTER_EXIT_MAX", 0)
    r = rig(tmp_path, monkeypatch, FAKE_START="trickle", FAKE_CHUNKS="2", FAKE_CHUNK="16", FAKE_GAP="0.3")
    # No controlling terminal: a macOS session leader's exit waits for its tty's output to drain (so there the
    # helper is not seen to exit until the master is read); without one the exit leaves its last bytes in the pty,
    # as a Linux pty's close does.
    real = r.seams.spawn
    r.seams.spawn = lambda argv, **kw: real(argv, **{**kw, "ctty": False})
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    try:
        assert r.run() == 0                  # `start`'s line was read: the tail is never what is counted away
    finally:
        closing(out_r, out_w)
    err = r.stderr()
    assert "claude exited 0" in err
    assert re.search(r"\d+ bytes written to the terminal after the vendor's helper exited were not shown", err)
    assert re.search(r"your terminal did not take the session's last \d+ bytes", err)


@bounded(60)
def test_a_slow_terminal_that_keeps_taking_bytes_keeps_the_final_drain(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    # Bounded on a terminal taking NOTHING, not on the clock: one that is slow but reading gets every byte.
    monkeypatch.setattr(SV, "FINAL_DRAIN_S", 0.5)
    n = 1 << 20
    r = rig(tmp_path, monkeypatch, FAKE_START="trickle", FAKE_CHUNKS=str(n // 65536), FAKE_CHUNK="65536")
    out_r, out_w = os.pipe()
    r.seams.stdout = out_w
    got = bytearray()
    import threading

    def reader() -> None:
        while True:
            time.sleep(0.1)                  # ~640 KB/s at most: the drain outlasts FINAL_DRAIN_S several times
            chunk = os.read(out_r, 65536)
            if not chunk:
                return
            got.extend(chunk)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        assert r.run() == 0
    finally:
        closing(out_w)
    t.join(20)
    closing(out_r)
    assert got.count(b"T") == n
    assert "did not take" not in r.stderr()


# --- r3-C2: the reset gets its own attempt after the drain gave up, and says so last when not taken ------------------

@bounded(60)
def test_reset_is_sent_after_the_abandoned_rest_led_by_can(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modesbig", FAKE_CHUNKS="2", FAKE_CHUNK="65536")
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    real = SV.Supervisor._drain
    seen = bytearray()

    def drain_then_resume(self, fd, buf, deadline, **kw):   # type: ignore[no-untyped-def]
        if fd == out_w and buf[:1] == SV._CAN:
            seen.extend(drain(out_r))        # his terminal resumes only after the rest was given up on
        return real(self, fd, buf, deadline, **kw)
    monkeypatch.setattr(SV.Supervisor, "_drain", drain_then_resume)
    try:
        assert r.run() == 0
        seen.extend(drain(out_r))
    finally:
        closing(out_r, out_w)
    assert seen.endswith(SV._CAN + b"\x1b[?1000l")
    err = r.stderr()
    assert re.search(r"your terminal did not take the session's last \d+ bytes", err)
    assert "run `reset`" not in err


@bounded(60)
def test_reset_not_taken_is_said_on_the_runs_last_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modesbig", FAKE_CHUNKS="1", FAKE_CHUNK="600")
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    t0 = time.monotonic()
    try:
        assert r.run() == 0
        took = time.monotonic() - t0
        assert b"\x1b[?1000l" not in drain(out_r)
    finally:
        closing(out_r, out_w)
    assert took < 20
    last = r.stderr().splitlines()[-1]
    assert "did not take the reset of the reporting modes" in last and "run `reset`" in last


# --- r3-C3: held status lines are released bounded and non-blocking, with a count -------------------------------------

@bounded(60)
def test_held_status_lines_never_block_the_runs_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)
    err_r, err_w = stalled()                 # his stderr is the stalled one this time
    r.seams.err = os.fdopen(err_w, "w")
    sup = SV.Supervisor(SV.Request(vendor="claude", repo=r.repo), r.seams)
    t0 = time.monotonic()
    try:
        assert sup.run() == 0
        took = time.monotonic() - t0
        assert os.get_blocking(err_w)        # put back as it was
        assert not drain(err_r).count(b"levain:")
    finally:
        closing(err_r, r.out_fd, r.in_w, r.in_r)
        r.seams.err.close()
    assert took < 25
    assert sup.unshown_lines >= 5            # the launch line, the session, the probe, the exit, the teardown
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]
    assert r.calls[-1] == ("close",)         # the lease closed before the held lines were tried


@bounded(60)
def test_held_lines_a_resumed_stderr_takes_are_written_with_none_counted(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    r = rig(tmp_path, monkeypatch)
    err_r, err_w = stalled()
    r.seams.err = os.fdopen(err_w, "w")
    sup = SV.Supervisor(SV.Request(vendor="claude", repo=r.repo), r.seams)
    real = SV.Supervisor._drain
    seen = bytearray()

    def resume(self, fd, buf, deadline, **kw):   # type: ignore[no-untyped-def]
        if fd == err_w:
            seen.extend(drain(err_r))
        return real(self, fd, buf, deadline, **kw)
    monkeypatch.setattr(SV.Supervisor, "_drain", resume)
    try:
        assert sup.run() == 0
        seen.extend(drain(err_r))
    finally:
        closing(err_r, r.out_fd, r.in_w, r.in_r)
        r.seams.err.close()
    assert sup.unshown_lines == 0
    text = seen.decode()
    assert f"session {SESSION} ended RETAINED" in text and "were not shown" not in text


# --- r3-C4: a stdout that refuses a write ends the relay into teardown, its loss counted -------------------------------

@bounded(60)
def test_stdout_error_mid_session_ends_the_relay_into_teardown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                               ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="forever")
    r.seams.start_grace_s = 1.0
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    os.close(out_r)                          # stalled, then gone: the next write is EPIPE
    try:
        assert r.run() == 1
    finally:
        closing(out_w)
    err = r.stderr()
    assert "your terminal refused the session's output" in err
    assert int(re.search(r"your terminal did not take the session's last (\d+) bytes", err).group(1)) > 0
    assert "start did not exit after the vendor's terminal closed" not in err   # no wait on a vendor still running
    assert [x["verb"] for x in r.verbs()] == ["prepare", "render", "start", "teardown"]
    assert f"session {SESSION} ended RETAINED" in err


@bounded(60)
def test_stdout_gone_during_the_final_drain_is_counted_not_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                                                ) -> None:
    r = rig(tmp_path, monkeypatch, FAKE_START="modesbig", FAKE_CHUNKS="2", FAKE_CHUNK="65536")
    out_r, out_w = stalled()
    r.seams.stdout = out_w
    real = SV.Supervisor._drain
    closed = []

    def reader_gone(self, fd, buf, deadline, **kw):   # type: ignore[no-untyped-def]
        if fd == out_w and not closed:
            os.close(out_r)
            closed.append(True)
        return real(self, fd, buf, deadline, **kw)
    monkeypatch.setattr(SV.Supervisor, "_drain", reader_gone)
    try:
        assert r.run() == 1
    finally:
        closing(out_w)
        if not closed:
            closing(out_r)
    err = r.stderr()
    assert "your terminal refused the session's output" in err
    assert int(re.search(r"your terminal did not take the session's last (\d+) bytes", err).group(1)) >= 2 * 65536
    # FX6 r4-4: the untaken reset is said on the failure line, not as "run `reset`" to a terminal that is gone.
    assert "the reset of the reporting modes the session turned on did not reach it" in err
    assert "run `reset`" not in err
