"""The manifest reads its panels side by side (levain#138): one request costs its slowest panel, not the sum of every panel's read.
Measured on the operator's cockpit the manifest took seconds while each panel alone took tens to hundreds of milliseconds."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from levain.cockpit import engine as cockpit_engine
from levain.cockpit.providers import build_default_cockpit
from tests.test_cockpit_k2a import _install

PER_PANEL_S = 0.1


@pytest.fixture(autouse=True)
def _close_patched_manifest_pools():
    """A test that patches the pool size makes a pool of that size; shut down any pool this test added."""
    before = set(cockpit_engine._manifest_pools)
    yield
    with cockpit_engine._manifest_pools_lock:
        made = [cockpit_engine._manifest_pools.pop(k) for k in list(cockpit_engine._manifest_pools) if k not in before]
    for pool in made:
        pool.shutdown(wait=True)


CRED = {"class": "token", "name": "browser", "device_class": None}


def _timed_manifest(tmp_path: Path, monkeypatch, readers: int) -> tuple[float, int]:
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    real = ck._snap_for

    def slow(spec, ctx, read_after=None):
        time.sleep(PER_PANEL_S)      # a panel whose source takes its time
        return real(spec, ctx, read_after)

    monkeypatch.setattr(ck, "_snap_for", slow)
    monkeypatch.setattr(cockpit_engine, "_MANIFEST_POOL_THREADS", readers)
    t0 = time.perf_counter()
    m = ck.manifest(CRED)
    return time.perf_counter() - t0, len(m["panels"])


def test_the_manifest_costs_its_slowest_panel_not_the_sum(tmp_path: Path, monkeypatch) -> None:
    took, n = _timed_manifest(tmp_path, monkeypatch, readers=16)
    assert n >= 20, n
    serial = n * PER_PANEL_S
    # generous: far over one panel's time, far under the sum a one-by-one pass pays (the sum is 2+ s here)
    assert took < serial / 2, (took, serial)


def test_the_same_manifest_read_one_panel_at_a_time_is_over_that_bound(tmp_path: Path, monkeypatch) -> None:
    # the control: with one reader the pass is the old serial one and misses the bound above, so the bound can tell the two apart
    took, n = _timed_manifest(tmp_path, monkeypatch, readers=1)
    assert took >= n * PER_PANEL_S * 0.9, (took, n)
