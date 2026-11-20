"""S5 of the read layer: a provider's own ``valid_until``, a per-panel backstop, and one order for the queue. A real
``SnapshotHost`` is driven with ``run_once``. A test that times a re-derive hands the host its own clock, so the loop's
finish time is the test's instant and a slow runner cannot move it; the others start at the real ``time.time()``."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.derive import PanelPlan, due_panels, queue_order
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


class _T0:
    """The test's start instant, taken after the host is built; a provider reads it lazily, so no setup time moves it."""

    value = 0.0

    def start(self) -> float:
        self.value = time.time()
        return self.value


def _provider(pid: str, until: "list[float | None]", t0: _T0, iso: Any = _iso, **kw: Any):
    """A panel whose every read returns the same rows and the next ``valid_until`` from ``until`` (offsets from ``t0``;
    the last one repeats)."""
    def read(ctx):
        t = until.pop(0) if len(until) > 1 else until[0]
        return Read(rows=(_row(1),), valid_until=None if t is None else iso(t0.value + t))

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

_PRE_EPOCH = "1969-12-31T23:59:59+00:00"  # before the epoch: its epoch form must never be taken

_VALUES: "dict[str, Any]" = {
    "aware": _iso,
    # a naive one is local time (here a zone west of UTC, where reading it as UTC would put it in the past)
    "naive": lambda t: datetime.fromtimestamp(t).isoformat(),
    "aware_datetime": lambda t: datetime.fromtimestamp(t, timezone.utc),
    "naive_datetime": lambda t: datetime.fromtimestamp(t),
    "year_9999_string": lambda t: _FAR,
    "year_9999_datetime": lambda t: datetime.fromisoformat(_FAR),
    "before_1970_string": lambda t: _PRE_EPOCH,
    "before_1970_datetime": lambda t: datetime.fromisoformat(_PRE_EPOCH),
}


@pytest.mark.parametrize("case", list(_VALUES))
def test_a_declared_valid_until_is_served_in_the_attached_head(host_for, fixed_tz, case: str) -> None:
    ck = Cockpit()
    clock = _T0()
    ck.register(_provider("a", [100], clock, iso=_VALUES[case]))
    host = host_for(ck)
    host.attach()
    t0 = clock.start()
    host.run_once(t0)
    served = _head(ck, "a")["valid_until"]
    if case.startswith(("year_9999", "before_1970")):
        assert served is None  # no bound, and the head still builds
    else:
        assert datetime.fromisoformat(served).timestamp() == pytest.approx(t0 + 100, abs=0.01)


def test_a_derive_that_moves_only_valid_until_keeps_the_generation(host_for) -> None:
    clock = _T0()
    ck = Cockpit()
    ck.register(_provider("a", [100, 200], clock, backstop_s=5))
    host = host_for(ck, clock=lambda: clock.value)  # the derive finishes at the test's t0, never later
    t0 = clock.start()  # after the setup: a slow one cannot push the derive's finish past the instants below
    host.run_once(t0)
    first = host.reader.read("a")
    assert "a" in host.run_once(t0 + 6)
    again = host.reader.read("a")
    assert again.gen == first.gen == 1 and again.body_sha == first.body_sha
    assert again.valid_until == pytest.approx(t0 + 200, abs=0.01) and first.valid_until == pytest.approx(t0 + 100, abs=0.01)


def test_the_panel_is_due_at_its_valid_until_and_not_before(host_for) -> None:
    clock = _T0()
    ck = Cockpit()
    ck.register(_provider("a", [100], clock, backstop_s=100000, stale_after_s=100000))
    host = host_for(ck, clock=lambda: clock.value)
    t0 = clock.start()
    host.run_once(t0)
    assert "a" not in host.run_once(t0 + 99)
    assert "a" in host.run_once(t0 + 101)


def test_a_per_panel_backstop_reaches_the_plan_and_re_derives_with_no_input_move(host_for) -> None:
    ck = Cockpit()
    ck.register(_simple("a", rows=[_row(1)], backstop_s=5))
    ck.register(_simple("b", rows=[_row(1)]))
    clock = _T0()
    host = host_for(ck, clock=lambda: clock.value)
    plans = {p.id: p for p in host._plans()}
    assert plans["a"].backstop_s == 5 and plans["b"].backstop_s is None
    t0 = clock.start()
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


def test_the_queue_orders_dirty_first_then_never_derived_then_oldest(host_for) -> None:
    ck = Cockpit()
    host = host_for(ck)
    loop = host._loop
    ids = ["dirty", "new", "old", "never", "oldest"]
    loop.set_plans([PanelPlan(id=i) for i in ids])
    ages = {"dirty": 50.0, "new": 60.0, "old": 10.0, "never": None, "oldest": 5.0}
    for pid, age in ages.items():
        loop._states[pid].derived_at = age
    loop._states["dirty"].dirty_gen = 1
    want = ["dirty", "never", "oldest", "old", "new"]
    assert sorted(reversed(ids), key=lambda pid: queue_order(pid, loop._states[pid])) == want
    # the live queue's key and due_panels read the same order: every panel here is due at a backstop
    assert sorted(reversed(ids), key=loop._queue_key) == want
    due = due_panels(loop._plans, loop._states, {}, None, now=1e9, backstop_s=1.0)
    assert [pid for pid, _ in due] == want


def test_an_expiry_that_passes_during_its_own_derive_is_stored_and_not_re_derived_every_poll(host_for) -> None:
    clock = _T0()
    ck = Cockpit()
    ck.register(_provider("a", [1], clock, backstop_s=100000, stale_after_s=100000))
    host = host_for(ck, clock=lambda: clock.value + 2)  # the derive finishes at t0 + 2
    t0 = clock.start() - 10  # the real now is past t0 + 1, so the reader sees the stored bound as passed
    clock.value = t0
    host.attach()
    host.run_once(t0)
    assert host.reader.read("a").valid_until == pytest.approx(t0 + 1, abs=0.01)
    assert _head(ck, "a")["stale"] is True
    assert "a" not in host.run_once(t0 + 3)
