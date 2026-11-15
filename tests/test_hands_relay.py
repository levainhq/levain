"""The hands proxy relay's stop (levain#30): the host side ``out`` relay removes its sockets and its
directory on SIGTERM. Its own module because tests/test_hands_spawn.py skips whole without the
openhands extra, and this needs nothing but python3."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile

import pytest

from levain.firing import confinement

# Run ahead of the relay program: from here on the relay's main thread cannot be woken by a SIGTERM that
# arrives after its last look, as when one lands between Python's check for a caught signal and the
# wait's syscall (the CI hang, job 132956252372). A relay whose wait needs that wakeup (signal.pause)
# never sees the SIGTERM.
_LATE_WAKEUP = """import signal as _s
_pause = _s.pause
def _late():
    _s.pthread_sigmask(_s.SIG_BLOCK, {_s.SIGTERM})
    _pause()
_s.pause = _late
"""


def _relay(d: str, *args: str, prelude: str = "") -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-I", "-S", "-c", prelude + confinement._HANDS_RELAY, "out", d, *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)


def _reap(relay: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(relay.pid, signal.SIGKILL)
    except OSError:
        pass
    relay.wait()
    relay.stdout.close()   # type: ignore[union-attr]
    relay.stderr.close()   # type: ignore[union-attr]


@pytest.mark.parametrize("ports", [0, 1])
def test_a_relay_stops_on_a_sigterm_that_arrives_before_it_waits(ports):
    """levain#30 (CI 132956252372): the host relay waited with signal.pause(), so a SIGTERM landing just
    before the pause was lost and the relay, with its directory, lived on. Its stop must be waiting for
    it whenever it comes. With a port a relay thread runs too, which is where such a signal may land."""
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    base = tempfile.mkdtemp(prefix="lvr", dir="/tmp")   # short: a unix socket path has a small limit
    d = os.path.join(base, ".levain-relay-x")
    relay = _relay(d, *([str(held.getsockname()[1])] if ports else []), prelude=_LATE_WAKEUP)
    try:
        assert relay.stdout.readline() == b"ready\n", relay.stderr.read()   # type: ignore[union-attr]
        assert os.path.isdir(d)
        relay.send_signal(signal.SIGTERM)
        try:
            relay.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("the relay ignored a SIGTERM that arrived before it waited")
        assert relay.returncode == 0 and not os.path.exists(d)
    finally:
        _reap(relay)
        held.close()
        shutil.rmtree(base, ignore_errors=True)


def test_a_relay_stops_on_a_sigterm_during_its_setup():
    """A SIGTERM before "ready" (the relay still making its sockets) is held, not lost: the relay
    finishes its setup, then stops and removes what it made."""
    base = tempfile.mkdtemp(prefix="lvr", dir="/tmp")
    d = os.path.join(base, ".levain-relay-x")
    # The prelude stands in for a SIGTERM landing mid-setup: it is sent to the relay itself once the
    # relay holds SIGTERM (its first signal call), before its directory exists.
    early = """import os as _o, signal as _s
_mask = _s.pthread_sigmask
def _then(how, sigs):
    r = _mask(how, sigs)
    if how == _s.SIG_BLOCK and _s.SIGTERM in sigs:
        _o.kill(_o.getpid(), _s.SIGTERM)
    return r
_s.pthread_sigmask = _then
"""
    relay = _relay(d, prelude=early)
    try:
        relay.wait(timeout=10)
        assert relay.returncode == 0 and not os.path.exists(d), relay.stderr.read()   # type: ignore[union-attr]
    finally:
        _reap(relay)
        shutil.rmtree(base, ignore_errors=True)
