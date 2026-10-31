"""The shared wait scale: the env var, its floor, and a poll whose deadline is scaled."""
from __future__ import annotations

import pytest

from tests._waits import scale, scaled, wait_until


# the same table is run through the browser driver in test_page_drive_contract.py: the two parsers must agree
SCALE_TABLE = (("2", 2.0), ("1.5", 1.5), ("1e1", 10.0), ("0.25", 1.0), ("0", 1.0), ("-3", 1.0), ("junk", 1.0), ("", 1.0),
               ("2junk", 1.0), ("inf", 1.0), ("Infinity", 1.0), ("1e9999", 1.0), ("nan", 1.0), (" 2", 1.0), ("0x10", 1.0),
               ("1_0", 1.0), ("50", 20.0), ("20", 20.0), ("1e3", 20.0))


def test_scale_reads_the_env_var_and_never_goes_below_one(monkeypatch):
    monkeypatch.delenv("LEVAIN_TEST_WAIT_SCALE", raising=False)
    assert scale() == 1.0
    for raw, want in SCALE_TABLE:
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


def test_wait_until_retries_a_raising_predicate_and_chains_the_last_error(monkeypatch):
    monkeypatch.setenv("LEVAIN_TEST_WAIT_SCALE", "1")
    store: dict = {}
    calls = []

    def late():
        calls.append(1)
        if len(calls) >= 3:
            store["k"] = 1
        return store["k"] == 1                                          # KeyError until the third call

    wait_until(late, 2, what="the key lands")
    with pytest.raises(AssertionError, match="the key never lands") as ei:
        wait_until(lambda: {}["k"], 0.1, what="the key never lands")
    assert isinstance(ei.value.__cause__, KeyError) and "KeyError" in str(ei.value)
