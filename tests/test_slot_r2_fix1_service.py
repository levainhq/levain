"""R2 fix round 1, the reaper SERVICE (levain.slot.services.reaper): the main process and its two children, on fake
pass and revoker objects (the steps themselves are tests/test_slot_r2_reaper.py's)."""
from __future__ import annotations

import os
import shutil
import signal
import tempfile
import time
from pathlib import Path

import pytest

from levain.slot import reaper as RP
from levain.slot import runtime_helper as H
from levain.slot.services import reaper as SR

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="the service forks its children")


class _FakeReaper:
    """With ``fail_first``, ``run_pass`` raises on its first call in each process (a restarted child starts fresh)."""

    fail_first = False

    def __init__(self, seams, **_kw):
        self.seams = seams
        self.calls = 0

    def boot_steps(self):
        return None

    def run_pass(self):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("one bad pass")
        return {k: 0 for k in ("pass_seq", "control", "swept", "revoked", "ended", "scrubbed", "reverted", "held")}


class _FakeRevoker:
    def __init__(self, seams, **_kw):
        self.seams = seams

    def wake(self):
        return {"control": "up", "outcomes": {}}

    def pending(self):
        return False


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(RP, "Reaper", _FakeReaper)
    monkeypatch.setattr(RP, "Revoker", _FakeRevoker)
    monkeypatch.setattr(RP, "boot_dirs", lambda seams, log=None: [])
    root = Path(tempfile.mkdtemp(prefix="lvrp", dir="/tmp"))     # an AF_UNIX path has a short limit on macOS
    (root / "R" / "reaper").mkdir(parents=True)
    seams = H.Seams(run=root / "R", store=root / "L", group_gid=lambda _g: os.getgid())
    logf = root / "log"

    def log(msg):
        with open(logf, "a") as f:
            f.write(msg + "\n")

    old = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError("serve did not return")))
    signal.alarm(20)
    try:
        yield seams, log, logf
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
        shutil.rmtree(root, ignore_errors=True)


def _serve(seams, log, **kw):
    return SR.serve(seams, chown_path=lambda *_a: None, log=log, tick_s=0.05, **kw)


def test_one_bad_pass_never_kills_the_worker_and_one_bound_is_enough(world, monkeypatch):
    # Reproduced on ff689f58: the worker died on the raised pass and restarted into the same raise (a crash loop),
    # and with only the worker bounded serve never returned (the alarm fired).
    seams, log, logf = world
    monkeypatch.setattr(_FakeReaper, "fail_first", True)
    assert _serve(seams, log, worker_passes=2) == 0
    text = logf.read_text()
    assert "RuntimeError: one bad pass" in text
    assert "restarting" not in text
    assert text.count("started worker") == 1


def test_a_second_instance_refuses_and_leaves_the_live_socket(world, monkeypatch):
    # Reproduced on ff689f58: the second serve unlinked the live instance's wake.sock and bound its own.
    seams, log, logf = world
    monkeypatch.setattr(SR, "INSTANCE_WAIT_S", 0.3)
    sock = seams.R / "reaper" / SR.WAKE_SOCK
    first = os.fork()
    if first == 0:
        code = 70
        try:
            signal.alarm(0)
            code = _serve(seams, log)
        finally:
            os._exit(code)
    try:
        deadline = time.monotonic() + 10
        while not sock.exists():
            assert time.monotonic() < deadline, "the first instance never bound wake.sock"
            time.sleep(0.02)
        ino = os.lstat(sock).st_ino
        assert _serve(seams, log, worker_passes=1, revoker_wakes=1) == SR.EX_INSTANCE_HELD
        assert os.lstat(sock).st_ino == ino
    finally:
        os.kill(first, signal.SIGTERM)
        os.waitpid(first, 0)
    assert not sock.exists()
