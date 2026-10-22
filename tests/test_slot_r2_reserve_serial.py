"""END's reserve release and the reaper's refill serialize on one flock of ``L/reserve``.

The refill holds the lock across fstat, write and fsync; a release that does not take it truncates under the
refill. The refill side is a second process holding the reaper's own lock helper, so the two are real
competing open file descriptions.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
import sys
import time

from levain.slot import end as E
from levain.slot import reaper as RP

_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[3])
from pathlib import Path
from levain.slot import reaper as RP
with RP._reserve_lock(Path(sys.argv[1])) as held:
    print("held" if held else "busy", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def test_release_waits_for_a_refill_holding_the_reserve_lock(tmp_path):
    reserve = tmp_path / "reserve"
    reserve.write_bytes(bytes(8000))
    r = E.Reserve(reserve, 8000)
    hold = 1.0
    proc = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, str(reserve), str(hold), str(Path(E.__file__).parents[2])],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        t0 = time.monotonic()
        released = r.release_step()
        waited = time.monotonic() - t0
    finally:
        proc.wait(timeout=10)
    assert released == r.step
    assert waited >= hold * 0.5, f"release did not wait for the refill's lock ({waited:.3f}s)"
    assert reserve.stat().st_size == 8000 - r.step

