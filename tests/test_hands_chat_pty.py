"""The hands chat runs on a pty the launcher owns, in a session of its own (leg 6 fix, L2 HIGH 2).

Measured on macOS 27.0.1 with a pty pair the probe owned: a process whose controlling terminal is a tty can
``ioctl(TIOCSTI)`` bytes into that tty's input queue; a process in another session (``setsid``) that still holds an fd
to the tty gets ``EACCES``. So a vendor that inherited the operator's terminal and session (``!use_pty``) could queue
keystrokes the operator's shell reads after the chat. Here a pty of the test stands in for the operator's terminal: a
harness process has it as its controlling terminal and stdio and calls the real ``_spawn``; the "vendor" tries the
injection on its own stdin and reports what it held.
"""
from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the hands chat launch is macOS only")

VENDOR = r'''
import fcntl, json, os, sys, termios
out = {"sid": os.getsid(0), "tty": os.ttyname(0) if os.isatty(0) else None}
try:
    for ch in "INJECTED\n":
        fcntl.ioctl(0, termios.TIOCSTI, ch.encode())
    out["tiocsti"] = "ok"
except OSError as e:
    out["tiocsti"] = f"errno {e.errno}"
line = (sys.stdin.readline() + sys.stdin.readline()) if len(sys.argv) > 2 else ""
out["line"] = line
sys.stdout.write("VENDOR-SAYS-HI\r\n")
sys.stdout.flush()
open(sys.argv[1], "w").write(json.dumps(out))
sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
'''

HARNESS = r'''
import json, os, sys
from levain.firing import hands_chat as hc
report = sys.argv[1]
json.dump({"sid": os.getsid(0), "tty": os.ttyname(0)}, open(report + ".launcher", "w"))
proc = hc._spawn([sys.executable, *sys.argv[2:]], os.getcwd())
rc = proc.wait()
close = getattr(proc, "close", None)
if close is not None:
    close()
# stay the session leader while the test inspects the terminal: macOS revokes a session's terminal when its leader
# exits, so the input queue and the modes are read before this process ends
import time
open(report + ".done", "w").close()
end = time.monotonic() + 15
while not os.path.exists(report + ".go") and time.monotonic() < end:
    time.sleep(0.05)
sys.exit(rc)
'''


def _take_ctty() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _drain(fd: int, until: float) -> bytes:
    got = b""
    while time.monotonic() < until:
        r, _, _ = select.select([fd], [], [], 0.1)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    return got


def _run(tmp_path, *vendor_args: str, typed: bytes = b"") -> tuple[int, dict, dict, bytes, bytes, list, list]:
    """The harness on a pty of the test (the operator's terminal). Returns the harness's code, the vendor's and the
    launcher's reports, what reached the operator's screen, what was left in the operator terminal's input queue,
    and the operator terminal's modes before and after."""
    vendor, harness, report = tmp_path / "vendor.py", tmp_path / "harness.py", tmp_path / "report.json"
    vendor.write_text(VENDOR)
    harness.write_text(HARNESS)
    master, slave = os.openpty()
    before = termios.tcgetattr(slave)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT), "TERM": "xterm"}
    p = subprocess.Popen([sys.executable, str(harness), str(report), str(vendor), str(report), *vendor_args],
                         stdin=slave, stdout=slave, stderr=slave, start_new_session=True, preexec_fn=_take_ctty,
                         cwd=str(tmp_path), env=env)
    try:
        time.sleep(0.8)
        if typed:
            os.write(master, typed)
        screen = b""
        done = Path(str(report) + ".done")
        deadline = time.monotonic() + 20
        while not done.exists() and p.poll() is None and time.monotonic() < deadline:
            screen += _drain(master, time.monotonic() + 0.2)
        screen += _drain(master, time.monotonic() + 0.3)
        after = termios.tcgetattr(slave)
        os.set_blocking(slave, False)
        left = b""
        try:
            left = os.read(slave, 4096)
        except (BlockingIOError, OSError):
            pass
        Path(str(report) + ".go").touch()
        rc = p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()
        os.close(master)
        os.close(slave)
    return (rc, json.loads(report.read_text()), json.loads(Path(str(report) + ".launcher").read_text()),
            screen, left, before, after)


def test_the_vendor_never_holds_the_operators_terminal_nor_shares_its_session(tmp_path):
    rc, vendor, launcher, _, left, _, _ = _run(tmp_path)
    assert b"INJECTED" not in left          # nothing queued into the operator's input for the shell to read after
    assert vendor["tty"] is not None and vendor["tty"] != launcher["tty"]     # a pty of its own, not the operator's
    assert vendor["sid"] != launcher["sid"]                                    # a session of its own


def test_the_relay_carries_the_screen_and_the_keys_and_the_exit_code(tmp_path):
    rc, vendor, _, screen, _, _, _ = _run(tmp_path, "read", "3", typed=b"typed by the operator\r")
    assert rc == 3
    assert b"VENDOR-SAYS-HI" in screen
    # the vendor reads its own pty: what it injected there (its own queue, harmless), then the operator's keys
    assert vendor["line"] == "INJECTED\ntyped by the operator\n"


def test_the_operators_terminal_modes_are_put_back(tmp_path):
    _, _, _, _, _, before, after = _run(tmp_path)
    assert before == after


def _open_fds() -> set[int]:
    return {int(n) for n in os.listdir("/dev/fd")}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill 0; reaped means gone
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return done == 0


def _fail_winch(monkeypatch, exc):
    real = signal.signal

    def fake(signum, handler):
        if signum == signal.SIGWINCH and callable(handler) and handler not in (signal.SIG_DFL, signal.SIG_IGN) \
                and getattr(handler, "__name__", "") == "<lambda>":
            raise exc
        return real(signum, handler)
    monkeypatch.setattr(signal, "signal", fake)


def _fail_thread_start(monkeypatch, exc):
    real = threading.Thread.start

    def fake(self):
        if self.name == "hands-chat-pty":
            raise exc
        return real(self)
    monkeypatch.setattr(threading.Thread, "start", fake)


def _fail_pipe(monkeypatch, exc):
    real = os.pipe

    def fake():
        if sys._getframe(1).f_globals.get("__name__") == "levain.firing.hands_chat":   # not Popen's own pipe
            raise exc
        return real()
    monkeypatch.setattr(os, "pipe", fake)


def _fail_setraw(monkeypatch, exc):
    import tty as tty_mod
    real = tty_mod.setraw

    def fake(fd, when=termios.TCSAFLUSH):
        real(fd, when)          # the terminal is raw, then the interrupt lands
        raise exc
    monkeypatch.setattr(tty_mod, "setraw", fake)


def _modes_but_pendin(modes_or_fd) -> list:
    """The modes with PENDIN cleared. Measured on macOS 27.0.1 (pty pair): a raw-to-canonical tcsetattr with no flush
    makes the kernel set PENDIN (retype pending input), which it clears at the next read, the input kept. A setraw
    interrupted after it changed the modes is restored that way (no flush, typed-ahead input kept, leg 6 L3 r3)."""
    modes = termios.tcgetattr(modes_or_fd) if isinstance(modes_or_fd, int) else list(modes_or_fd)
    modes[3] &= ~termios.PENDIN
    return modes


@pytest.mark.parametrize("inject", [_fail_pipe, _fail_setraw, _fail_winch, _fail_thread_start])
@pytest.mark.parametrize("exc", [RuntimeError("can't start new thread"), KeyboardInterrupt()],
                         ids=["error", "interrupt"])
def test_a_failure_at_any_step_after_the_spawn_restores_the_terminal_closes_fds_and_stops_the_child(
        tmp_path, monkeypatch, inject, exc):
    """Leg 6 L3 r1 (four lineages): a failure after setraw left the operator's terminal raw, the pty master and the
    wake pipe open, the relay thread running and the child unstopped, since the constructor never returned a handle.
    Each step after the Popen fails in turn (an error, then an interrupt): the construction is undone on every path."""
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    before_modes = termios.tcgetattr(slave)
    started: list[int] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):        # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self.pid)
    try:
        fds_before = _open_fds()
        monkeypatch.setattr(hc.subprocess, "Popen", Recording)
        inject(monkeypatch, exc)
        with pytest.raises(type(exc)):
            hc._PtyChat(["/bin/sleep", "30"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        monkeypatch.undo()
        assert _modes_but_pendin(slave) == _modes_but_pendin(before_modes)   # not left raw
        assert _open_fds() <= fds_before                                 # master and wake pipe closed
        assert not [t for t in threading.enumerate() if t.name == "hands-chat-pty"]
        [pid] = started
        assert not _alive(pid)                                           # the child was stopped
    finally:
        os.close(master)
        os.close(slave)


def test_a_raising_stop_in_a_failed_construction_still_restores_the_terminal_and_closes_fds(tmp_path, monkeypatch):
    """Leg 6 L3 r2 (complement LOW, deepseek MED): _undo called _stop unguarded, so a _stop that raised skipped
    close(): the operator's terminal stayed raw and the master and wake pipe stayed open."""
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    before_modes = termios.tcgetattr(slave)
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class Recording(real_popen):        # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)

    def bad_stop(proc):
        raise OSError("stop failed")
    try:
        fds_before = _open_fds()
        monkeypatch.setattr(hc.subprocess, "Popen", Recording)
        monkeypatch.setattr(hc, "_stop", bad_stop)
        _fail_thread_start(monkeypatch, RuntimeError("can't start new thread"))
        with pytest.raises((OSError, RuntimeError)):
            hc._PtyChat(["/bin/sleep", "30"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        monkeypatch.undo()
        assert termios.tcgetattr(slave) == before_modes                  # not left raw
        assert _open_fds() <= fds_before                                 # master and wake pipe closed
    finally:
        for p in started:
            p.kill()
            p.wait(timeout=10)
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("where", ["settle", "relay-join"])
def test_an_interrupt_inside_close_still_restores_the_terminal_and_closes_fds(tmp_path, monkeypatch, where):
    """Leg 6 L3 r2 (complement LOW): close() ran the relay join and the settle before tcsetattr, outside any
    try/finally, so a KeyboardInterrupt or SystemExit there left the operator's terminal raw and the fds open."""
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    before_modes = termios.tcgetattr(slave)
    try:
        fds_before = _open_fds()
        chat = hc._PtyChat(["/usr/bin/true"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        assert chat.wait(timeout=10) == 0
        assert termios.tcgetattr(slave) != before_modes                  # raw while the chat ran

        def interrupt(*a, **k):
            raise KeyboardInterrupt
        if where == "settle":
            monkeypatch.setattr(hc._PtyChat, "_settle", staticmethod(interrupt))
        else:
            monkeypatch.setattr(chat._thread, "join", interrupt)
        with pytest.raises(KeyboardInterrupt):
            chat.close()
        monkeypatch.undo()
        assert termios.tcgetattr(slave) == before_modes
        assert _open_fds() <= fds_before
    finally:
        os.close(master)
        os.close(slave)


def test_close_of_a_terminal_never_made_raw_neither_waits_nor_discards_typed_ahead_input(tmp_path, monkeypatch):
    """Leg 6 L3 r2 (complement LOW, gemini LOW, codex): close() ran the settle (up to SETTLE_MAX_S, discarding input)
    and the flush even when setraw had failed and the terminal was never raw. Input typed ahead stays for the shell."""
    import tty as tty_mod
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    settled: list[int] = []
    try:
        def failing_setraw(fd, when=termios.TCSAFLUSH):
            raise termios.error(25, "Inappropriate ioctl for device")
        monkeypatch.setattr(tty_mod, "setraw", failing_setraw)
        monkeypatch.setattr(hc._PtyChat, "_settle", staticmethod(lambda fd: settled.append(fd)))
        chat = hc._PtyChat(["/usr/bin/true"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        assert chat.wait(timeout=10) == 0
        chat._thread.join(timeout=5)
        assert not chat._thread.is_alive()          # the relay ended with the vendor: nothing reads the input now
        os.write(master, b"typed ahead\n")
        time.sleep(0.1)
        _drain(master, time.monotonic() + 0.2)      # the echo: a TCSAFLUSH would wait on undrained output
        chat.close()
        assert settled == []
        os.set_blocking(slave, False)
        assert os.read(slave, 4096) == b"typed ahead\n"
    finally:
        os.close(master)
        os.close(slave)


def test_an_interrupt_inside_setraw_before_its_tcsetattr_restores_the_modes_and_keeps_typed_ahead_input(
        tmp_path, monkeypatch):
    """Leg 6 L3 r3 (deepseek LOW): ``_raw`` was set before ``tty.setraw``, which is Python-level (tcgetattr, mutate,
    tcsetattr). An interrupt after its tcgetattr and before its tcsetattr left the modes unchanged with ``_raw`` True,
    so close() settled and flushed the operator's typed-ahead input. The saved modes are still put back."""
    import tty as tty_mod
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    before_modes = termios.tcgetattr(slave)
    settled: list[int] = []
    restored: list[list] = []
    real_tcsetattr = termios.tcsetattr
    try:
        os.write(master, b"typed ahead\n")
        time.sleep(0.1)
        _drain(master, time.monotonic() + 0.2)      # the echo: a TCSAFLUSH would wait on undrained output

        def interrupted_setraw(fd, when=termios.TCSAFLUSH):
            termios.tcgetattr(fd)
            raise KeyboardInterrupt                 # lands before setraw's own tcsetattr

        def recording_tcsetattr(fd, when, attrs):
            restored.append(attrs)
            return real_tcsetattr(fd, when, attrs)
        monkeypatch.setattr(tty_mod, "setraw", interrupted_setraw)
        monkeypatch.setattr(hc.termios, "tcsetattr", recording_tcsetattr)
        monkeypatch.setattr(hc._PtyChat, "_settle", staticmethod(lambda fd: settled.append(fd)))
        with pytest.raises(KeyboardInterrupt):
            hc._PtyChat(["/bin/sleep", "30"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        monkeypatch.undo()
        assert settled == []                                             # no settle window
        assert restored == [before_modes]                                # the saved modes were put back
        assert termios.tcgetattr(slave) == before_modes
        os.set_blocking(slave, False)
        assert os.read(slave, 4096) == b"typed ahead\n"                  # not flushed
    finally:
        os.close(master)
        os.close(slave)


def test_close_flushes_input_that_arrived_after_the_relay_stopped(tmp_path):
    """A terminal's late answer to a query the chat wrote (or anything queued once the relay stopped reading) is
    discarded at close, not left for the operator's shell."""
    from levain.firing import hands_chat as hc
    master, slave = os.openpty()
    try:
        chat = hc._PtyChat(["/usr/bin/true"], str(tmp_path), {"PATH": "/usr/bin:/bin"}, tty_in=slave, tty_out=slave)
        assert chat.wait(timeout=10) == 0
        chat._thread.join(timeout=5)
        assert not chat._thread.is_alive()          # the relay ended with the vendor: nothing reads the input now
        os.write(master, b"LATE-REPLY\n")
        time.sleep(0.2)
        chat.close()
        os.set_blocking(slave, False)
        left = b""
        try:
            left = os.read(slave, 4096)
        except (BlockingIOError, OSError):
            pass
        assert b"LATE-REPLY" not in left
    finally:
        os.close(master)
        os.close(slave)


QUERY_VENDOR = r'''
import sys, time
sys.stdout.write("\x1b[6n")         # a cursor-position query: the terminal answers on its input queue
sys.stdout.flush()
time.sleep(0.05)
'''


def _terminal(master: int, stop: threading.Event, delay: float) -> threading.Thread:
    """The operator's terminal emulator: it answers a CSI 6n it is shown ``delay`` seconds later, on its input."""
    def run():
        seen = b""
        while not stop.is_set():
            r, _, _ = select.select([master], [], [], 0.05)
            if not r:
                continue
            try:
                seen += os.read(master, 4096)
            except OSError:
                return
            if b"\x1b[6n" in seen:
                time.sleep(delay)
                os.write(master, b"\x1b[12;40R")
                return
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _next_read(fd: int) -> bytes:
    """What the operator's shell reads next: its line editor puts the terminal in raw mode first (a canonical-mode
    read would wait for a newline the reply never has, and FIONREAD counts only whole lines there)."""
    import tty as tty_mod
    tty_mod.setraw(fd, termios.TCSANOW)
    r, _, _ = select.select([fd], [], [], 0.2)
    return os.read(fd, 4096) if r else b""


@pytest.mark.parametrize("redirected", [False, True], ids=["tty-stdin", "stdin-redirected"])
def test_a_terminal_reply_that_arrives_just_after_the_chat_does_not_reach_the_next_read(tmp_path, redirected):
    """Leg 6 L3 r1 (complement MED, deepseek MED, gemini LOW): the vendor's last output is a terminal query; the
    terminal's reply lands after the relay stopped. close() waits out a settle window before its flush, and flushes
    the terminal's input through stdout's fd when stdin is not the terminal, so the next read (the operator's shell)
    never gets the reply as typed input. Measured with a pty pair this test owns, the reply 80 ms after the query."""
    from levain.firing import hands_chat as hc
    vendor = tmp_path / "q.py"
    vendor.write_text(QUERY_VENDOR)
    master, slave = os.openpty()
    null = os.open(os.devnull, os.O_RDONLY)
    stop = threading.Event()
    try:
        term = _terminal(master, stop, 0.08)
        chat = hc._PtyChat([sys.executable, str(vendor)], str(tmp_path), {"PATH": "/usr/bin:/bin"},
                           tty_in=null if redirected else slave, tty_out=slave)
        assert chat.wait(timeout=10) == 0
        chat.close()
        term.join(timeout=5)
        time.sleep(0.1)
        assert b"12;40R" not in _next_read(slave)
    finally:
        stop.set()
        os.close(null)
        os.close(master)
        os.close(slave)
