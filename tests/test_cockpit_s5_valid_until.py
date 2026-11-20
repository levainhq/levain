"""S5 of the read layer: a provider's own ``valid_until``, a per-panel backstop, and heavy panels derived last. A real
``SnapshotHost`` is driven with ``run_once``; the loop's finish time is the wall clock, so each test starts at the
real ``time.time()`` and moves only the instant it hands ``run_once``."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.derive import PanelPlan
from levain.cockpit.engine import Cockpit, CockpitRegistrationError, Read
from levain.cockpit.snapshot_host import SnapshotHost

from tests.test_cockpit_k1 import _simple
from tests.test_cockpit_snapshot_host import NONE_CRED, _row


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


@pytest.fixture
def host_for(tmp_path: Path):
    made: list[SnapshotHost] = []

    def make(ck: Cockpit, **kw: Any) -> SnapshotHost:
        root = tmp_path / f"h{len(made)}"
        root.mkdir()
        h = SnapshotHost(ck, root, **kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.stop(timeout=10)


def _head(ck: Cockpit, pid: str) -> dict[str, Any]:
    return ck.manifest(NONE_CRED)["panels"][pid]


def _provider(pid: str, until: "list[float | None]", **kw: Any):
    """A panel whose every read returns the same rows and the next ``valid_until`` from ``until`` (the last one repeats)."""
    def read(ctx):
        t = until.pop(0) if len(until) > 1 else until[0]
        return Read(rows=(_row(1),), valid_until=None if t is None else _iso(t))

    return _simple(pid, read=read, **kw)


def test_a_declared_valid_until_is_served_in_the_attached_head(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 100]))
    host = host_for(ck)
    host.attach()
    host.run_once(t0)
    assert datetime.fromisoformat(_head(ck, "a")["valid_until"]).timestamp() == pytest.approx(t0 + 100)


def test_a_derive_that_moves_only_valid_until_keeps_the_generation(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 100, t0 + 200], backstop_s=5))
    host = host_for(ck)
    host.run_once(t0)
    first = host.reader.read("a")
    assert "a" in host.run_once(t0 + 6)
    again = host.reader.read("a")
    assert again.gen == first.gen == 1 and again.body_sha == first.body_sha
    assert again.valid_until == pytest.approx(t0 + 200) and first.valid_until == pytest.approx(t0 + 100)


def test_the_panel_is_due_at_its_valid_until_and_not_before(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 100], backstop_s=100000))
    host = host_for(ck)
    host.run_once(t0)
    assert "a" not in host.run_once(t0 + 99)
    assert "a" in host.run_once(t0 + 101)


def test_a_per_panel_backstop_reaches_the_plan_and_re_derives_with_no_input_move(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_simple("a", rows=[_row(1)], backstop_s=5))
    ck.register(_simple("b", rows=[_row(1)]))
    host = host_for(ck)
    plans = {p.id: p for p in host._plans()}
    assert plans["a"].backstop_s == 5 and plans["b"].backstop_s is None
    host.run_once(t0)
    assert "a" not in host.run_once(t0 + 4)
    assert "a" in host.run_once(t0 + 6)


@pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan, True])
def test_a_backstop_that_is_not_positive_and_finite_is_refused(bad: Any) -> None:
    ck = Cockpit()
    with pytest.raises(CockpitRegistrationError, match="backstop_s"):
        ck.register(_simple("a", backstop_s=bad))


def test_the_queue_orders_dirty_first_then_light_before_heavy_then_oldest(host_for) -> None:
    ck = Cockpit()
    host = host_for(ck)
    loop = host._loop
    loop.set_plans([PanelPlan(id="heavy_dirty", heavy=True), PanelPlan(id="light", heavy=False),
                    PanelPlan(id="heavy_clean", heavy=True)])
    for pid in ("heavy_dirty", "light", "heavy_clean"):
        loop._states[pid].derived_at = 50.0
    loop._states["heavy_dirty"].dirty_gen = 1
    order = sorted(["heavy_clean", "light", "heavy_dirty"], key=loop._queue_key)
    assert order == ["heavy_dirty", "light", "heavy_clean"]
