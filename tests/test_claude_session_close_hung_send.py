"""close() must not wait behind a send hung writing to a child that no longer reads (real pipes)."""
from __future__ import annotations

import subprocess
import sys
import threading
import time

from levain.coordinator.claude_session import ClaudeSession, PipeTransport
from levain.coordinator.driver import NotSent, SendCommand

_CHILD = "import time\nwhile True:\n    time.sleep(1)\n"      # never reads stdin


def _spawn() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", _CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def test_close_does_not_wait_behind_a_send_hung_on_a_full_pipe():
    proc = _spawn()
    session = ClaudeSession(PipeTransport(proc), session_id="s")
    outcome: list[object] = []

    def sender() -> None:
        try:
            session.send(SendCommand("op-0001", "x" * 4_000_000, (), "queue", None))
            outcome.append("sent")
        except NotSent as exc:
            outcome.append(exc)

    sending = threading.Thread(target=sender, daemon=True)
    sending.start()
    time.sleep(0.5)
    assert sending.is_alive(), "the send must be blocked on the full pipe for this test to mean anything"
    closer = threading.Thread(target=session.close, daemon=True)
    started = time.monotonic()
    closer.start()
    closer.join(12)
    try:
        assert not closer.is_alive(), "close() is still blocked behind the hung send"
        assert time.monotonic() - started < 12
        sending.join(5)
        assert not sending.is_alive(), "the hung send was not freed"
        assert outcome and isinstance(outcome[0], NotSent)
        assert proc.poll() is not None
        assert session.closed
        session.close()                         # idempotent
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)
