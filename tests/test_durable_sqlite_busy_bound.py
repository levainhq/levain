"""The retry on busy is bounded by wall clock, and never retries a connection set fail-fast.

L3 r1 on e7ce6a95 (lane-limitsrace): the bound counted only the retry's own pauses, while each try could also block in
SQLite's busy handler, so a held lock stalled a writer far past ``BUSY_WAIT_S``; and ``CockpitDB._connect_nowait``
(``busy_timeout = 0``) was retried for the whole bound. A real second connection holds the lock in both tests."""
from __future__ import annotations

import sqlite3
import time

import pytest

from levain.autonomic import durable_sqlite as D

WAIT_S = 0.5
MARGIN_S = 1.5


def _store(path):
    conn = D.open_durable(path)
    conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
    conn.close()


def _holder(path):
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_a_held_write_lock_stalls_a_writer_no_longer_than_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "BUSY_WAIT_S", WAIT_S)
    path = tmp_path / "s.db"
    _store(path)
    holder = _holder(path)
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with D.write_transaction(lambda: D.open_durable(path)):
                raise AssertionError("the lock is held: the block must not run")
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert WAIT_S * 0.5 <= took < WAIT_S + MARGIN_S, took


def test_a_fail_fast_connection_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "BUSY_WAIT_S", 2.0)
    path = tmp_path / "s.db"
    _store(path)

    def nowait():
        conn = D.open_durable(path)
        conn.execute("PRAGMA busy_timeout = 0")
        return conn

    holder = _holder(path)
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with D.write_transaction(nowait):
                raise AssertionError("the lock is held: the block must not run")
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 0.5, took


def test_the_busy_wait_is_restored_after_a_retried_begin(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "BUSY_WAIT_S", WAIT_S)
    path = tmp_path / "s.db"
    _store(path)
    with D.write_transaction(lambda: D.open_durable(path)) as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(WAIT_S * 1000)


def test_a_shorter_connection_busy_timeout_bounds_the_retry(tmp_path, monkeypatch):
    """CI on 435bbbf6: the deadline was BUSY_WAIT_S for every connection, so a caller whose connection set a shorter
    ``busy_timeout`` (the consent store's) waited the longer bound. The connection's own value is the most it waits."""
    monkeypatch.setattr(D, "BUSY_WAIT_S", 6.0)
    path = tmp_path / "s.db"
    _store(path)

    def short():
        conn = D.open_durable(path)
        conn.execute("PRAGMA busy_timeout = 1000")
        return conn

    holder = _holder(path)
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with D.write_transaction(short):
                raise AssertionError("the lock is held: the block must not run")
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert 0.5 <= took < 1.0 + MARGIN_S, took
