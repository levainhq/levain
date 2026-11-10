"""ClaudeSession.close / PipeTransport.close against real children over real pipes: a hung send never blocks close,
an idle child gets a graceful EOF, a send whose bytes left is ambiguous (never NotSent), a descendant holding stdin
cannot hang close, and a second close waits for the first (sessclose + L3 r1 fixes)."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from levain.desk import claude_session as cs
from levain.desk.chat import ChatService
from levain.desk.claude_session import ClaudeSession, PipeTransport, TransportClosed
from levain.desk.driver import NotSent, SendCommand

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")

_BIG = "x" * 4_000_000                 # far past any pipe buffer: the write blocks once the child stops reading

# reads a known prefix (so bytes demonstrably left), says so, then never reads again; dies on SIGTERM
_PREFIX_CHILD = "import sys, time\nsys.stdin.buffer.read(1000)\nprint('got', flush=True)\nwhile True:\n    time.sleep(1)\n"
# exits 0 on stdin EOF (the graceful path); SIGTERM would make its returncode -15
_EOF_CHILD = "import sys\nsys.stdin.read()\n"
# never reads, never exits on EOF
_DEAF_CHILD = "import time\nwhile True:\n    time.sleep(1)\n"
# never reads, ignores SIGTERM: only SIGKILL ends it
_STUBBORN_CHILD = ("import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('ready', flush=True)\n"
                   "while True:\n    time.sleep(1)\n")
# a grandchild that inherits stdin and never reads; {escape} makes it leave the process group
_GRANDCHILD = ("import os, signal, sys, time\n{escape}signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
               "print('gpid', os.getpid(), flush=True)\nwhile True:\n    time.sleep(1)\n")
_PARENT = ("import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-I', '-c', {code!r}])\n"
           "while True:\n    time.sleep(1)\n")


def _spawn(code: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)


def _await_line(t: PipeTransport, prefix: str, limit: float = 10.0) -> str:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        for line in t.poll(0.1):
            if line.startswith(prefix):
                return line.strip()
    raise AssertionError(f"the child never said {prefix!r}")


def _sender(session: ClaudeSession, text: str, outcome: list) -> threading.Thread:
    def run() -> None:
        try:
            session.send(SendCommand("op-0001", text, (), "queue", None))
            outcome.append("sent")
        except Exception as exc:  # noqa: BLE001 - the test reads which one
            outcome.append(exc)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def _timed_close(close, bound: float) -> float:
    closer = threading.Thread(target=close, daemon=True)
    started = time.monotonic()
    closer.start()
    closer.join(bound)
    assert not closer.is_alive(), f"close() is still blocked after {bound} s"
    return time.monotonic() - started


def _kill(proc: subprocess.Popen, *pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        proc.kill()
        proc.wait(5)


def test_a_send_whose_bytes_left_is_ambiguous_and_close_does_not_wait_behind_it():
    # Q2 (codex HIGH 1) + the original hang: the child read 1000 bytes, so the line is NOT NotSent
    proc = _spawn(_PREFIX_CHILD)
    t = PipeTransport(proc)
    session = ClaudeSession(t, session_id="s")
    outcome: list = []
    try:
        sending = _sender(session, _BIG, outcome)
        _await_line(t, "got")
        assert sending.is_alive(), "the send must be blocked on the full pipe for this test to mean anything"
        took = _timed_close(session.close, 12)
        assert took < cs._BUSY_GRACE + cs._SIGNAL_WAIT, "a send in flight takes the signal-first path, no EOF grace"
        sending.join(5)
        assert not sending.is_alive(), "the hung send was not freed"
        assert len(outcome) == 1 and isinstance(outcome[0], BrokenPipeError) and not isinstance(outcome[0], NotSent)
        assert "bytes were written" in str(outcome[0])
        assert proc.poll() == -signal.SIGTERM
        assert proc.stdin.closed                # the write returned, so stdin was closed
        assert session.closed
        session.close()                         # idempotent
    finally:
        _kill(proc)


def test_the_service_records_a_partly_written_send_as_ambiguous():
    # Q2: the mapping the ChatService already has (any non-NotSent exception from send is ambiguous)
    class _Partial:
        def send(self, command):
            raise BrokenPipeError(32, "the child's stdin failed after 65536 of 4000000 bytes were written")
    state = ChatService._call(_Partial(), SendCommand("op-0001", "x", (), "queue", None))[0]
    assert state == "ambiguous"


def test_an_idle_child_gets_a_graceful_eof_and_no_signal():
    # Q1 (complement MED): EOF first; the child's own exit, not SIGTERM
    proc = _spawn(_EOF_CHILD)
    session = ClaudeSession(PipeTransport(proc), session_id="s")
    try:
        took = _timed_close(session.close, 8)
        assert proc.poll() == 0, "the idle child was signalled instead of being given its EOF"
        assert took < cs._EOF_GRACE
    finally:
        _kill(proc)


def test_an_idle_child_that_ignores_eof_is_terminated_after_the_grace():
    proc = _spawn(_DEAF_CHILD)
    session = ClaudeSession(PipeTransport(proc), session_id="s")
    try:
        took = _timed_close(session.close, 8)
        assert proc.poll() == -signal.SIGTERM
        assert cs._EOF_GRACE <= took < cs._EOF_GRACE + cs._SIGNAL_WAIT
    finally:
        _kill(proc)


def test_a_send_that_has_not_written_when_close_runs_is_not_sent():
    # Q3 (deepseek HIGH): close() lands between send's closed check and its write. Verified: NotSent, no ValueError.
    proc = _spawn(_EOF_CHILD)
    t = PipeTransport(proc)
    session = ClaudeSession(t, session_id="s")
    real_pump = session._pump

    def pump_then_close(wait: float) -> None:
        real_pump(wait)
        session.close()                         # runs while send holds _io: close never takes it

    session._pump = pump_then_close
    try:
        with pytest.raises(NotSent):
            session.send(SendCommand("op-0001", "hi", (), "queue", None))
        assert session.pending_lines() == 0
    finally:
        _kill(proc)


def test_a_write_to_a_stdin_closed_beneath_the_transport_is_transport_closed():
    # Q3: the stdin closed under a transport that is not (yet) closed: nothing was written
    proc = _spawn(_EOF_CHILD)
    t = PipeTransport(proc)
    try:
        proc.stdin.close()
        with pytest.raises(TransportClosed):
            t.write("x\n")
    finally:
        t.close()
        _kill(proc)


def test_a_descendant_holding_stdin_cannot_hang_close_and_dies_with_the_group():
    # Q4 (codex HIGH 2): the child dies on SIGTERM, its grandchild ignores it and keeps the pipe's read end
    proc = _spawn(_PARENT.format(code=_GRANDCHILD.format(escape="")))
    t = PipeTransport(proc)
    session = ClaudeSession(t, session_id="s")
    gpid = int(_await_line(t, "gpid").split()[1])
    outcome: list = []
    try:
        assert os.getpgid(gpid) == proc.pid
        sending = _sender(session, _BIG, outcome)
        time.sleep(0.3)
        assert sending.is_alive()
        took = _timed_close(session.close, cs.CLOSE_BOUND + 2)
        assert took < cs.CLOSE_BOUND
        sending.join(5)
        assert not sending.is_alive() and isinstance(outcome[0], BrokenPipeError)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(gpid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the grandchild holding stdin outlived the close")
        assert proc.stdin.closed
    finally:
        _kill(proc, gpid)


def test_a_descendant_that_left_the_group_costs_a_bounded_close_and_stdin_is_abandoned():
    # Q4 bound + known edge: a setsid() escapee is out of reach; close still returns, stdin is left, never closed
    proc = _spawn(_PARENT.format(code=_GRANDCHILD.format(escape="os.setsid()\n")))
    t = PipeTransport(proc)
    session = ClaudeSession(t, session_id="s")
    gpid = int(_await_line(t, "gpid").split()[1])
    outcome: list = []
    try:
        assert os.getpgid(gpid) != proc.pid
        sending = _sender(session, _BIG, outcome)
        time.sleep(0.3)
        assert sending.is_alive()
        took = _timed_close(session.close, cs.CLOSE_BOUND + 2)
        assert took < cs.CLOSE_BOUND
        assert proc.poll() is not None
        assert sending.is_alive() and not proc.stdin.closed     # abandoned: never closed under the blocked write
        os.kill(gpid, signal.SIGKILL)
        sending.join(5)
        assert not sending.is_alive() and isinstance(outcome[0], BrokenPipeError)
    finally:
        _kill(proc, gpid)


def test_a_second_concurrent_transport_close_waits_for_the_first():
    # Q4 (codex MED 3, complement LOW 3/4): the child ignores SIGTERM, so the first close runs to SIGKILL
    proc = _spawn(_STUBBORN_CHILD)
    t = PipeTransport(proc)
    try:
        _await_line(t, "ready")                 # SIGTERM is ignored from here on
        first = threading.Thread(target=t.close, daemon=True)
        first.start()
        time.sleep(0.2)
        _timed_close(t.close, cs.CLOSE_BOUND + 2)
        assert proc.returncode is not None, "the second close() returned while the first was still ending the child"
        first.join(5)
        assert proc.returncode == -signal.SIGKILL
    finally:
        _kill(proc)


def test_a_second_concurrent_session_close_waits_for_the_first():
    release = threading.Event()

    class _SlowTransport:
        eof = False

        def write(self, data):
            pass

        def poll(self, wait):
            return []

        def close(self):
            release.wait(10)

    session = ClaudeSession(_SlowTransport(), session_id="s")
    first = threading.Thread(target=session.close, daemon=True)
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=session.close, daemon=True)
    second.start()
    second.join(0.5)
    assert second.is_alive(), "the second close() returned while the first was still closing the transport"
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
