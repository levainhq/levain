"""RED (DIOGAP stage 1, finding D3). Fails on the code as it stands: ``levain doctor``'s ``slot alarms`` row drops a slot's
last ``suppressed`` summary once it is older than ``SLOT_ALARM_WINDOW``, so alarms suppressed inside the window after that
summary read as "none in the last 24 hours"."""
from __future__ import annotations

from datetime import timedelta

import tests.test_qbroker as base
from levain import doctor
from levain.qbroker import broker as qbroker
from tests.test_qbroker import make_broker
from tests.test_qbroker_alarm import alarm, records

L, broker, clock, keys, resolver, run_dir = base.L, base.broker, base.clock, base.keys, base.resolver, base.run_dir


def test_d3_alarms_suppressed_after_the_last_summary_left_the_window_are_not_none(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    t0 = clock["now"]
    for i in range(qbroker.ALARM_SLOT_BUDGET + 64):                  # the last summary the log carries is at count 64
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        assert alarm(b, reason=f"flood{i}")["ok"] is True
    sums = [r["suppressed"] for r in records(L) if "suppressed" in r]
    assert sums[-1] == 64
    mono["t"] += 23 * 3600
    clock["now"] = t0 + timedelta(hours=23)
    for i in range(10):                                              # counts 65..74: no doubling, so no new summary
        mono["t"] += 1.0 / qbroker.ALARM_RATE
        clock["now"] += timedelta(seconds=5)
        assert alarm(b, reason=f"late{i}")["ok"] is True
    [row] = doctor._check_slot_alarms(store=L, now=t0 + timedelta(hours=24, minutes=30))
    assert "none in the last 24 hours" not in row.detail, row.detail
