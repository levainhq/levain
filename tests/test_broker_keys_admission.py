"""keys' process entry and its one shutdown trigger (S1 session 3b-3a). The admission pools, the fencing and the `F`
path are proven by the session's fixture run against the real front process; these two pin the trigger's edges."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import levain
from levain.broker import keys as K

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")


def _connect(path: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return "connected"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root")
def test_stdin_not_a_pipe_never_ends_keys():
    # launchd and `nohup ... </dev/null` hand keys a stdin that is not a pipe: EOF there must not shut it down
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")   # short: a unix socket path is capped near 104 bytes
    try:
        os.mkdir(os.path.join(d, "keys"))
        front = os.path.join(d, "keys", "front.sock")
        env = {**os.environ, "PYTHONPATH": str(Path(levain.__file__).resolve().parents[1])}
        p = subprocess.Popen([sys.executable, "-m", "levain.broker.keys", "--runtime-root", d, "--lib-root", d,
                              "--control", os.path.join(d, "control.sock"), "--test-single-uid"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        try:
            end = time.monotonic() + 20
            while _connect(front) != "connected":
                assert p.poll() is None, p.stderr.read().decode()
                assert time.monotonic() < end, "keys never served front.sock"
                time.sleep(0.05)
            time.sleep(1.5)
            assert p.poll() is None, "keys exited on its /dev/null stdin"
            p.send_signal(signal.SIGTERM)
            assert p.wait(10) == 0
            err = p.stderr.read().decode()
            assert "trigger is not armed" in err and "stopped" in err, err
            assert _connect(front) == "ConnectionRefusedError"
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shutdown_triggered_during_start_ends_keys():
    # a SIGTERM between arm_triggers() and start(): the latch's run finishes, nothing is served, the wait ends
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")

    async def run() -> tuple:
        k = K.Keys(single_uid=os.getuid())
        socks = K.bind_listeners([(os.path.join(d, "f.sock"), None)])
        await asyncio.wait_for(k.shutdown(), 5)
        await k.start(socks)
        await asyncio.wait_for(k.wait_done(), 5)
        return [s.is_serving() for s in k.servers], [s.fileno() for s in socks]

    try:
        serving, fds = asyncio.run(run())
        assert serving == [] and fds == [-1]
    finally:
        shutil.rmtree(d, ignore_errors=True)
