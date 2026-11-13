"""RED (DIOGAP stage 1, finding C1). Fails on the code as it stands: a cap that resolved once stays "known" in the slot's
ALARM cap set for the broker's life, so once CONTROL stops resolving it every frame carrying it costs a CONTROL round trip
that no bucket charges (one per ``RESOLVE_TTL``, since the refusal is cached), against the unseen-cap bucket's
``ALARM_RATE``."""
from __future__ import annotations

from datetime import timedelta

import tests.test_qbroker as base
from levain import doctor
from levain.qbroker import broker as qbroker
from tests.test_qbroker import make_broker
from tests.test_qbroker_alarm import alarm, records

L, broker, clock, keys, resolver, run_dir = base.L, base.broker, base.clock, base.keys, base.resolver, base.run_dir


def test_c1_a_known_cap_that_stops_resolving_is_charged_like_an_unseen_one(L, resolver, clock):
    mono = {"t": 1000.0}
    b = make_broker(L, resolver, clock, monotonic=lambda: mono["t"])
    assert alarm(b, reason="first")["ok"] is True                    # cap-a resolves once: it is known
    before = len(resolver.calls)
    resolver.caps.clear()                                            # its session ended
    n = 100
    step = qbroker.RESOLVE_TTL + 0.1                                 # one frame per cache lifetime: every one reaches CONTROL
    for i in range(n):
        mono["t"] += step
        assert alarm(b, reason=f"t{i}")["refused"] in ("cap-unknown", "rate-limited")
    spent = len(resolver.calls) - before
    allowed = qbroker.ALARM_BURST + qbroker.ALARM_RATE * n * step + 1     # what the unseen-cap bucket would have let through
    assert spent <= allowed, f"{spent} CONTROL round trips for {n} frames; the unseen-cap bucket allows {allowed:.0f}"

