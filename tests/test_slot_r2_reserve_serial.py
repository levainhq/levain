"""END's reserve release and the reaper's refill serialize on one flock of ``L/reserve``.

The refill holds the lock across fstat, write and fsync; a release that does not take it truncates under the
refill. The refill side is a second process holding the reaper's own lock helper, so the two are real
competing open file descriptions. The holder keeps the lock until the parent signals on stdin, so no case
depends on how long a sleep happens to last.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from levain.slot import Refused
from levain.slot import end as E

_HOLDER = """
import sys
sys.path.insert(0, sys.argv[2])
from pathlib import Path
from levain.slot import reaper as RP
with RP._reserve_lock(Path(sys.argv[1])) as held:
    print("held" if held else "busy", flush=True)
    sys.stdin.readline()
"""


@pytest.fixture
def holder(tmp_path):
    reserve = tmp_path / "reserve"
    reserve.write_bytes(bytes(8000))
    proc = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, str(reserve), str(Path(E.__file__).parents[2])],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        yield reserve, proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _in_thread(fn):
    out: dict = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:   # noqa: BLE001 - handed to the asserting thread
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def test_release_waits_for_a_refill_holding_the_reserve_lock(holder):
    reserve, proc = holder
    r = E.Reserve(reserve, 8000)
    t, out = _in_thread(r.release_step)
    t.join(timeout=0.5)
    assert t.is_alive(), "release did not wait for the refill's lock"
    assert reserve.stat().st_size == 8000
    proc.stdin.write("go\n")
    proc.stdin.flush()
    t.join(timeout=10)
    assert not t.is_alive(), "release never finished after the refill let go"
    assert out.get("value") == r.step
    assert reserve.stat().st_size == 8000 - r.step


def test_release_refuses_within_the_bound_when_the_lock_is_never_let_go(holder):
    reserve, proc = holder
    r = E.Reserve(reserve, 8000)
    r.lock_wait_s = 0.3
    t, out = _in_thread(r.release_step)
    t.join(timeout=5)
    assert not t.is_alive(), "release waited on the lock past its bound"
    assert isinstance(out.get("error"), Refused) and out["error"].name == "reserve-locked", out
    assert reserve.stat().st_size == 8000
