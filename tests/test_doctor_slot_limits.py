"""The doctor's ``slot limits`` row (slot limits DESIGN §3): what each agent's slot sessions run under, read from
``L/policy/limits`` by the queue broker's own reader, so the row says what the broker enforces. Runs as the lane uid:
the scratch store's owner stands in for root."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from levain import doctor
from levain.consent import limits as CL
from levain.contracts import canon
from levain.slot import layout

ME = os.getuid()


def _store(tmp: Path, *, pool: bool = False) -> Path:
    store = tmp / "store"
    (store / "policy").mkdir(parents=True)
    os.chmod(store / "policy", 0o755)
    if pool:
        (store / "policy" / "pool").write_text(layout.pool_config_text(1, 1))
    return store


def _row(store: Path, running=lambda host: True):
    rows = doctor._check_slot_limits(host="linux", store=store, owner=ME, running=running)
    assert len(rows) == 1 and rows[0].name == "slot limits"
    return rows[0]


def _put(store: Path, data: bytes, mode: int = 0o644) -> Path:
    p = store / "policy" / "limits"
    p.write_bytes(data)
    os.chmod(p, mode)
    return p


def test_no_row_before_the_isolation_install(tmp_path):
    assert doctor._check_slot_limits(host="linux", store=tmp_path / "none", owner=ME) == []


def test_no_file_is_the_defaults_and_says_so(tmp_path):
    r = _row(_store(tmp_path))
    assert r.ok and not r.warn
    assert "claude_code: calls 3,000, active hours 12.0, repeat 8" in r.detail
    assert "codex: calls 3,000" in r.detail and r.detail.endswith("[defaults (no file)]")


def test_the_file_per_agent(tmp_path):
    store = _store(tmp_path)
    p = _put(store, canon.canonical_bytes({"format": CL.LIMITS_FORMAT, "default": {"calls": 500},
                                           "codex": {"calls": 100, "repeat": 4}}))
    r = _row(store)
    assert r.ok and not r.warn, r.detail
    assert "claude_code: calls 500, active hours 12.0, repeat 8" in r.detail
    assert "codex: calls 100, active hours 12.0, repeat 4" in r.detail
    assert r.detail.endswith(f"[{p}]")


@pytest.mark.parametrize("data,mode,why", [
    (b"garbage", 0o644, "limits-malformed"),
    (b'{ "format": "levain-limits/1" }', 0o644, "not its canonical bytes"),
    (canon.canonical_bytes({"format": CL.LIMITS_FORMAT, "default": {"fresh_tokens": 5}}), 0o644,
     "limits-not-counted:fresh_tokens"),
    (canon.canonical_bytes({"format": CL.LIMITS_FORMAT}), 0o666, "writable by group or others"),
])
def test_an_unusable_file_fails_with_the_brokers_reason_and_the_fix(tmp_path, data, mode, why, caplog):
    store = _store(tmp_path)
    _put(store, data, mode)
    with caplog.at_level(logging.WARNING, logger="levain.qbroker"):
        r = _row(store)
    assert not r.ok
    assert why in r.detail and "is not usable" in r.detail and "limits-failed" in r.detail
    assert "levain limits set --slots" in r.hint
    assert not [rec for rec in caplog.records if rec.name == "levain.qbroker"]     # said in the row, not logged
    assert logging.getLogger("levain.qbroker").disabled is False


def test_a_link_fails(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "real").write_bytes(canon.canonical_bytes({"format": CL.LIMITS_FORMAT}))
    os.symlink(tmp_path / "real", store / "policy" / "limits")
    r = _row(store)
    assert not r.ok and "a symlink" in r.detail


def test_a_policy_directory_not_roots_fails(tmp_path):
    # a non-root test cannot chown the file alone; the directory is checked first and is this uid's, not "root's"
    store = _store(tmp_path)
    _put(store, canon.canonical_bytes({"format": CL.LIMITS_FORMAT}))
    r = doctor._check_slot_limits(host="linux", store=store, owner=ME + 1, running=lambda h: True)[0]
    assert not r.ok and f"is not a directory of uid {ME + 1}" in r.detail


@pytest.mark.parametrize("up,words", [(False, "is not running"), (None, "could not be checked")])
def test_a_pool_without_a_running_broker_warns_not_enforced(tmp_path, up, words):
    r = _row(_store(tmp_path, pool=True), running=lambda host: up)
    assert r.ok and r.warn
    assert words in r.detail and "not enforced" in r.detail


def test_a_pool_with_a_running_broker_is_ok(tmp_path):
    r = _row(_store(tmp_path, pool=True))
    assert r.ok and not r.warn


def test_no_pool_asks_nothing_about_the_broker(tmp_path):
    def running(host):
        raise AssertionError("asked")
    assert _row(_store(tmp_path), running=running).ok


def test_the_row_runs_in_the_doctor():
    import inspect

    assert "core.extend(_check_slot_limits())" in inspect.getsource(doctor)
