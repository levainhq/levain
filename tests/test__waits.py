"""The shared wait scale: the env var, its floor, and a poll whose deadline is scaled."""
from __future__ import annotations

import pytest

from tests._waits import scale, scaled, wait_until


def test_scale_reads_the_env_var_and_never_goes_below_one(monkeypatch):
    monkeypatch.delenv("LEVAIN_TEST_WAIT_SCALE", raising=False)
    assert scale() == 1.0
    for raw, want in (("2", 2.0), ("1.5", 1.5), ("0.25", 1.0), ("0", 1.0), ("-3", 1.0), ("junk", 1.0), ("", 1.0)):
        monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", raw)
        assert scale() == want, raw
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "2")
    assert scaled(0.3) == pytest.approx(0.6)


def test_wait_until_returns_when_true_and_names_what_when_never(monkeypatch):
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "1")
    n = []
    wait_until(lambda: n.append(1) or len(n) >= 3, 2, what="three polls")
    with pytest.raises(AssertionError, match="the broker settles"):
        wait_until(lambda: False, 0.1, what="the broker settles")


def test_wait_until_deadline_is_scaled(monkeypatch):
    import time
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "3")
    t0 = time.monotonic()
    with pytest.raises(AssertionError):
        wait_until(lambda: False, 0.2, interval=0.02)
    assert time.monotonic() - t0 >= 0.55
