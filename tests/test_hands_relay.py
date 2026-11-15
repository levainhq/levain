"""The hands proxy relay's stop (levain#30, levain#33): the host side ``out`` relay removes its sockets
and its directory on SIGTERM, whenever the SIGTERM comes. Its own module because
tests/test_hands_spawn.py skips whole without the openhands extra, and this needs nothing but python3.

Each test runs the relay program behind a prelude that injects a SIGTERM (or a stall) at one point of
the relay's life; the relay sees no other signal."""
from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from levain.firing import confinement

_ONCE = """import os as _o, signal as _s
_done = []
def _term():
    if not _done:
        _done.append(1)
        _o.kill(_o.getpid(), _s.SIGTERM)
"""

# Just before the relay blocks SIGTERM for its wait, with its handler not yet run (a no-op stands in
# for it, so only the C half of the handler has seen the signal): the signal is then not pending,
# and a sigwait alone would wait forever.
_BEFORE_BLOCK = _ONCE + """_signal, _mask = _s.signal, _s.pthread_sigmask
def _no_handler(sig, h):
    return _signal(sig, (lambda *a: None) if sig == _s.SIGTERM and callable(h) else h)
def _then(how, sigs):
    if how == _s.SIG_BLOCK and _s.SIGTERM in sigs:
        _term()
    return _mask(how, sigs)
_s.signal, _s.pthread_sigmask = _no_handler, _then
"""

# Just before the relay waits: for a sigwait the SIGTERM is pending; for the old handler + pause()
# the main thread can no longer be woken by it (the CI hang, job 132956252372).
_BEFORE_WAIT = _ONCE + """_sigwait, _pause = _s.sigwait, _s.pause
def _w(sigs):
    _term()
    return _sigwait(sigs)
def _p():
    _s.pthread_sigmask(_s.SIG_BLOCK, {_s.SIGTERM})
    _term()
    return _pause()
_s.sigwait, _s.pause = _w, _p
"""

# The setup stalls after the relay made its directory and bound a socket (a slow home): it says so on
# stderr and sleeps; levain's ready timeout then sends SIGTERM.
_STALL = """import os as _o, time as _t
_chmod = _o.chmod
def _slow(*a, **k):
    _o.write(2, b"stall\\n")
    _t.sleep(30)
    return _chmod(*a, **k)
_o.chmod = _slow
"""


def _relay(d: str, *args: str, prelude: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-I", "-S", "-c", prelude + confinement._HANDS_RELAY, "out", d, *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)


def _reap(relay: subprocess.Popen[bytes]) -> None:
    if relay.poll() is None:   # alive, so not reaped: its pid (and group) cannot have been reused
        try:
            os.killpg(relay.pid, signal.SIGKILL)
        except OSError:
            pass
    relay.wait()
    relay.stdout.close()   # type: ignore[union-attr]
    relay.stderr.close()   # type: ignore[union-attr]


def _line(f, timeout: float = 10.0) -> bytes:
    """One line from a relay's pipe, or what came before the deadline."""
    got, deadline = b"", time.monotonic() + timeout
    while not got.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([f], [], [], left)[0]:
            break
        chunk = os.read(f.fileno(), 1)
        if not chunk:
            break
        got += chunk
    return got


def _stops(relay: subprocess.Popen[bytes], d: str, what: str) -> None:
    try:
        relay.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail(f"the relay ignored a SIGTERM {what}")
    assert relay.returncode == 0 and not os.path.exists(d)


@pytest.fixture
def place():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    base = tempfile.mkdtemp(prefix="lvr", dir="/tmp")   # short: a unix socket path has a small limit
    yield os.path.join(base, ".levain-relay-x"), str(held.getsockname()[1])
    held.close()
    shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("ports", [0, 1])
@pytest.mark.parametrize("when", ["before the block", "before the wait"])
def test_a_relay_stops_on_a_sigterm_that_arrives_as_it_starts_to_wait(place, when, ports):
    """levain#30 (CI 132956252372): the host relay waited with signal.pause(), so a SIGTERM landing just
    before the pause was lost and the relay, with its directory, lived on. With a port a relay thread
    runs too, which is where such a signal may land."""
    d, port = place
    relay = _relay(d, *([port] if ports else []), prelude=_BEFORE_BLOCK if when == "before the block" else _BEFORE_WAIT)
    try:
        assert _line(relay.stdout) == b"ready\n", relay.stderr.read()   # type: ignore[union-attr]
        _stops(relay, d, f"that arrived {when}")
    finally:
        _reap(relay)


def test_a_relay_whose_setup_stalls_is_tidied_by_its_sigterm(place):
    """levain#33 (codex L3 r1): a setup stalled after its directory was made gets levain's SIGTERM at the
    ready timeout, then a SIGKILL 2 s later. The SIGTERM must remove the directory at once: a live
    maker's directory is never swept."""
    d, port = place
    relay = _relay(d, port, prelude=_STALL)
    try:
        assert _line(relay.stderr) == b"stall\n"
        assert os.path.isdir(d)
        relay.send_signal(signal.SIGTERM)
        _stops(relay, d, "during its setup")
    finally:
        _reap(relay)
