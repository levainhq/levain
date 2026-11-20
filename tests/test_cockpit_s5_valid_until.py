"""S5 of the read layer: a provider's own ``valid_until``, a per-panel backstop, and heavy panels derived last. A real
``SnapshotHost`` is driven with ``run_once``; the loop's finish time is the wall clock, so each test starts at the
real ``time.time()`` and moves only the instant it hands ``run_once``."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.derive import HEAVY_YIELD_S, PanelPlan, due_panels, queue_order
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


def _provider(pid: str, until: "list[float | None]", iso: Any = _iso, **kw: Any):
    """A panel whose every read returns the same rows and the next ``valid_until`` from ``until`` (the last one repeats)."""
    def read(ctx):
        t = until.pop(0) if len(until) > 1 else until[0]
        return Read(rows=(_row(1),), valid_until=None if t is None else iso(t))

    return _simple(pid, read=read, **kw)


@pytest.fixture
def fixed_tz():
    """A fixed zone west of UTC with no DST (so a naive time has no ambiguous hour), restored on teardown."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT+8"
    time.tzset()
    yield
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


_FAR = "9999-12-31T23:59:59-23:59"  # past UTC year 9999: its epoch form is out of range

_VALUES: "dict[str, Any]" = {
    "aware": _iso,
    # a naive one is local time (here a zone west of UTC, where reading it as UTC would put it in the past)
    "naive": lambda t: datetime.fromtimestamp(t).isoformat(),
    "aware_datetime": lambda t: datetime.fromtimestamp(t, timezone.utc),
    "naive_datetime": lambda t: datetime.fromtimestamp(t),
    "year_9999_string": lambda t: _FAR,
    "year_9999_datetime": lambda t: datetime.fromisoformat(_FAR),
}


@pytest.mark.parametrize("case", list(_VALUES))
def test_a_declared_valid_until_is_served_in_the_attached_head(host_for, fixed_tz, case: str) -> None:
    ck = Cockpit()
    t0 = time.time()
    ck.register(_provider("a", [t0 + 100], iso=_VALUES[case]))
    host = host_for(ck)
    host.attach()
    host.run_once(t0)
    served = _head(ck, "a")["valid_until"]
    if case.startswith("year_9999"):
        assert served is None  # no bound, and the head still builds
    else:
        assert datetime.fromisoformat(served).timestamp() == pytest.approx(t0 + 100)


def test_a_derive_that_moves_only_valid_until_keeps_the_generation(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 100, t0 + 200], backstop_s=5))
    host = host_for(ck)
    t0 = time.time()  # after the setup: a slow one cannot push the derive's finish past the instants below
    host.run_once(t0)
    first = host.reader.read("a")
    assert "a" in host.run_once(t0 + 6)
    again = host.reader.read("a")
    assert again.gen == first.gen == 1 and again.body_sha == first.body_sha
    assert again.valid_until == pytest.approx(t0 + 200) and first.valid_until == pytest.approx(t0 + 100)


def test_the_panel_is_due_at_its_valid_until_and_not_before(host_for) -> None:
    t0 = time.time()
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 100], backstop_s=100000, stale_after_s=100000))
    host = host_for(ck)
    host.run_once(t0)
    assert "a" not in host.run_once(t0 + 99)
    assert "a" in host.run_once(t0 + 101)


def test_a_per_panel_backstop_reaches_the_plan_and_re_derives_with_no_input_move(host_for) -> None:
    ck = Cockpit()
    ck.register(_simple("a", rows=[_row(1)], backstop_s=5))
    ck.register(_simple("b", rows=[_row(1)]))
    host = host_for(ck)
    plans = {p.id: p for p in host._plans()}
    assert plans["a"].backstop_s == 5 and plans["b"].backstop_s is None
    t0 = time.time()
    host.run_once(t0)
    assert "a" not in host.run_once(t0 + 4)
    assert "a" in host.run_once(t0 + 6)


@pytest.mark.parametrize("bad", [0, -1, math.inf, math.nan, True, 901])
def test_a_backstop_that_is_not_positive_and_finite_is_refused(bad: Any) -> None:
    # 901 is past the default stale_after_s (900): the panel would show stale while correct
    ck = Cockpit()
    with pytest.raises(CockpitRegistrationError, match="backstop_s"):
        ck.register(_simple("a", backstop_s=bad, stale_after_s=900))
    if bad == 901:
        with pytest.raises(CockpitRegistrationError, match=r"stale_after_s \(900"):
            ck.register(_simple("b", backstop_s=bad, stale_after_s=900))


def test_the_queue_orders_dirty_first_then_light_before_heavy_then_oldest(host_for) -> None:
    ck = Cockpit()
    host = host_for(ck)
    loop = host._loop
    ids = ["heavy_dirty", "light_new", "light_old", "light_never", "heavy_clean"]
    loop.set_plans([PanelPlan(id=i, heavy=i.startswith("heavy")) for i in ids])
    ages = {"heavy_dirty": 50.0, "light_new": 60.0, "light_old": 10.0, "light_never": None, "heavy_clean": 5.0}
    for pid, age in ages.items():
        loop._states[pid].derived_at = age
    loop._states["heavy_dirty"].dirty_gen = 1
    want = ["heavy_dirty", "light_never", "light_old", "light_new", "heavy_clean"]
    def key(pid: str, waited: "float | None" = 0.0):
        return queue_order(pid, loop._plans.get(pid), loop._states[pid], waited)

    assert sorted(reversed(ids), key=key) == want
    # due_panels reads the same order: every panel here is due at a backstop, so the one function decides
    due = due_panels(loop._plans, loop._states, {}, None, now=1e9, backstop_s=1.0)
    assert [pid for pid, _ in due] == want


def test_a_heavy_job_that_has_waited_the_yield_sorts_before_a_light_one_queued_now(host_for) -> None:
    ck = Cockpit()
    loop = host_for(ck)._loop
    loop.set_plans([PanelPlan(id="h", heavy=True), PanelPlan(id="l")])
    loop._states["h"].derived_at = loop._states["l"].derived_at = 10.0
    light_now = queue_order("l", loop._plans["l"], loop._states["l"], 0.0)
    assert queue_order("h", loop._plans["h"], loop._states["h"], HEAVY_YIELD_S) < light_now
    assert queue_order("h", loop._plans["h"], loop._states["h"], 0.0) > light_now
    assert queue_order("h", loop._plans["h"], loop._states["h"], None) > light_now  # a batch never waits


def test_an_expiry_that_passes_during_its_own_derive_is_stored_and_not_re_derived_every_poll(host_for) -> None:
    t0 = time.time() - 10  # the real now is past t0 + 1, so the reader sees the stored bound as passed
    ck = Cockpit()
    ck.register(_provider("a", [t0 + 1], backstop_s=100000, stale_after_s=100000))
    host = host_for(ck, clock=lambda: t0 + 2)  # the derive finishes at t0 + 2
    host.attach()
    host.run_once(t0)
    assert host.reader.read("a").valid_until == pytest.approx(t0 + 1)
    assert _head(ck, "a")["stale"] is True
    assert "a" not in host.run_once(t0 + 3)
