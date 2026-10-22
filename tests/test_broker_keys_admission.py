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
    except (ConnectionRefusedError, FileNotFoundError):
        # nothing listens: a closed listener whose path is left behind refuses; Python 3.13's asyncio unlinks a unix
        # server's path when the server closes, so the same state reads as a missing file there
        return "refused"
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
            assert _connect(front) == "refused"
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


def test_trigger_while_start_creates_its_server_leaves_no_listener(monkeypatch):
    # a SIGTERM while start() is suspended inside its server creation: the server it was creating is closed too
    d = tempfile.mkdtemp(prefix="lk", dir="/tmp")
    path = os.path.join(d, "f.sock")
    real = asyncio.start_unix_server

    async def run() -> tuple:
        k = K.Keys(single_uid=os.getuid())
        socks = K.bind_listeners([(path, None)])
        entered, gate = asyncio.Event(), asyncio.Event()

        async def gated(*a, **kw):
            entered.set()
            await gate.wait()
            return await real(*a, **kw)

        monkeypatch.setattr(asyncio, "start_unix_server", gated)
        st = asyncio.ensure_future(k.start(socks))
        await asyncio.wait_for(entered.wait(), 5)
        k.trigger()
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.wait_for(st, 5)
        await asyncio.wait_for(k.wait_done(), 5)
        conn = await asyncio.get_running_loop().run_in_executor(None, _connect, path)
        return [s.is_serving() for s in k.servers], conn

    try:
        serving, conn = asyncio.run(run())
        assert serving == [False] and conn == "refused", (serving, conn)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_admin_calls_after_the_latch_change_nothing():
    # `closing` is the one shutdown state: after it, FRONT-REGISTER / FRONT-ARM / DROP-SESSION answer nothing
    async def run() -> tuple:
        k = K.Keys(single_uid=os.getuid())
        k._in_loop()
        await k.front_register(0, "s1", 70000, 123, 456, 1, 1)
        await asyncio.wait_for(k.shutdown(), 5)
        reg = await k.front_register(0, "s2", 70001, 124, 457, 1, 1)
        arm = await k.front_arm(0, "s1", 1)
        drop = await k.drop_session(0, "s1")
        return reg, arm, drop, sorted(k.regs), k.regs[70000].state

    reg, arm, drop, uids, state = asyncio.run(run())
    assert (reg, arm, drop) == (None, None, None)
    assert uids == [70000] and state == "registered"
