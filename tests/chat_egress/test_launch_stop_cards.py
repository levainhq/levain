"""CH-D2 S8 (second slice): stop with N held cards inside its bound (the S8 DONE line).

N POSTs on a gated launch each hold a card (nobody answers); `stop_egress` then ends the launch inside its bound:
the glue exits 0 with the final flush (its own drain bound, `DRAIN_MS`), the decider closes clean, no service thread
is left, no POST reached the origin, and every held card is withdrawn.
"""

from __future__ import annotations

import http.client
import threading
import time

import pytest

from levain.chat_egress import decider as dm
from levain.chat_egress import launch as lm

from . import test_launch as tl
from .decider_rig import Desk
from .test_launch_k4 import RecOrigin

N = 6


@pytest.fixture
def origin():
    o = RecOrigin()
    yield o
    o.close()


@pytest.mark.chat_egress_srt
def test_stop_with_n_held_cards_ends_inside_its_bound(tmp_path, origin, monkeypatch) -> None:
    desk = Desk()
    monkeypatch.setattr(tl, "Desk", lambda: desk)
    e = tl._start(tmp_path, origin, monkeypatch)
    results: list[object] = []

    def post(i: int) -> None:
        c = http.client.HTTPConnection("127.0.0.1", e.http_port, timeout=30)
        try:
            c.request("POST", f"http://pub.s8.test:{origin.port}/w{i}", body=b"x",
                      headers={"Host": f"pub.s8.test:{origin.port}"})
            results.append(c.getresponse().status)
        except (OSError, http.client.HTTPException) as exc:
            results.append(type(exc).__name__)
        finally:
            c.close()
    threads = [threading.Thread(target=post, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 10
    while len(desk.filed) < N and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(desk.filed) == N, desk.filed
    t0 = time.monotonic()
    out = lm.stop_egress(e)
    took = time.monotonic() - t0
    for t in threads:
        t.join(10)
    bound = lm.GLUE_EXIT_TIMEOUT_S + dm.Limits().stop_drain_s + 5.0
    print(f"RUN S8B stop with {N} held cards: {took:.2f}s (bound {bound}s); {out}; clients {results}; "
          f"cards ended {sorted(desk.ended)}")
    assert took < bound and out.glue_exit == 0 and out.decider_clean and out.final_flush is not None
    assert dm.service_threads() == [] and origin.seen == [] and len(results) == N
    assert 200 not in results
    assert sorted(desk.ended) == sorted(f"card-{i}" for i in range(1, N + 1))
