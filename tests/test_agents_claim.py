"""P6-1b: the occurrence claim and the controller tick."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.agents import controller as ctl
from levain.agents.store import AgentStoreRefused, open_agent_store
from levain.autonomic.db import DB_NAME

NY = "America/New_York"
AG = "ag-" + "a" * 32
EVERY_20 = {"kind": "schedule", "cron": "*/20 * * * *", "zone": NY, "window": None}
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path, trigger=EVERY_20):
    store = open_agent_store(tmp_path / "s")
    with store.write() as c:
        c.execute("INSERT INTO agents (agent_id, name, current_version, registration, operation, created_at)"
                  " VALUES (?, 'scratch', 1, 'registered', 'active', '2026-11-01T00:00:00Z')", (AG,))
        c.execute("INSERT INTO agent_specs (agent_id, version, spec_hash, role_hash, spec, created_at)"
                  " VALUES (?, 1, 'h1', 'r1', ?, '2026-11-01T00:00:00Z')", (AG, json.dumps({"trigger": trigger})))
    return store


def _rows(store):
    with store.read() as c:
        return c.execute("SELECT key, state, reason FROM agent_occurrences ORDER BY key").fetchall()


def _claim(store, key):
    with store.write() as c:
        return ctl.claim(c, agent_id=AG, kind="scheduled", key=key, spec_hash="h1", binding_id="b1",
                         window_key="d:2026-11-02", now=T0)


def _clock(box):
    return lambda: box[0]


def _armed(agent_id, spec_hash):
    return "b1"


def test_duplicate_delivery_writes_nothing_and_raises_nothing(tmp_path):
    store = _store(tmp_path)
    first = _claim(store, "2026-11-02T12:20:00Z")
    # the second delivery arrives on its own connection
    other = open_agent_store(tmp_path / "s")
    with other.write() as c:
        second = ctl.claim(c, agent_id=AG, kind="scheduled", key="2026-11-02T12:20:00Z", spec_hash="h1",
                           binding_id="b1", window_key="d:2026-11-02", now=T0 + timedelta(minutes=3))
    assert first[1] == ctl.CLAIMED and second == (first[0], ctl.DUPLICATE)
    rows = _rows(store)
    assert rows == [("2026-11-02T12:20:00Z", "admitted", None)]
    with store.read() as c:  # the loser did not even touch admitted_at
        assert c.execute("SELECT admitted_at FROM agent_occurrences").fetchone()[0] == "2026-11-02T12:00:00Z"


def test_one_occurrence_per_instant(tmp_path):
    store = _store(tmp_path)
    box = [T0]
    ctl.tick(store, _clock(box), _armed)  # first look: cursor set, history not fired
    for _ in range(9):  # a fake clock in 5-minute ticks across 45 minutes; a redelivering daemon repeats each tick
        box[0] += timedelta(minutes=5)
        ctl.tick(store, _clock(box), _armed)
        ctl.tick(store, _clock(box), _armed)
    keys = [r[0] for r in _rows(store)]
    assert keys == ["2026-11-02T12:20:00Z", "2026-11-02T12:40:00Z"] and len(set(keys)) == len(keys)
    # the second slot found the first still admitted (unlaunched): its own overlap row, never a second fire
    assert [r[1] for r in _rows(store)] == ["admitted", "skipped-overlap"]


def test_a_refused_schema_refuses_the_tick_with_the_named_reason(tmp_path):
    store = _store(tmp_path)
    conn = sqlite3.connect(store.path, isolation_level=None)
    conn.execute("DROP TRIGGER agent_occurrences_one_fire")
    conn.close()
    with pytest.raises(AgentStoreRefused, match="agent schema differs: missing/changed \\['agent_occurrences_one_fire'\\]"):
        ctl.tick(tmp_path / "s", _clock([T0]), _armed)
    assert (tmp_path / "s" / DB_NAME).is_file() and _rows_raw(store.path) == 0


def _rows_raw(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM agent_occurrences").fetchone()[0]
    finally:
        conn.close()


def test_a_long_idle_cursor_ticks_in_bounded_chunks_without_raising(tmp_path):
    daily = {"kind": "schedule", "cron": "0 9 * * *", "zone": NY, "window": None}
    store = _store(tmp_path, daily)
    with store.write() as c:  # last looked 200 days ago: more than two MAX_SPAN walks
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, '2026-04-16T12:00:00Z')",
                  (ctl.CURSOR_PREFIX + AG,))
    out = ctl.tick(store, _clock([T0]), _armed)
    assert len(out.missed) >= 190 and out.claimed == []  # old slots are counted, never fired
    with store.read() as c:
        assert c.execute("SELECT value FROM agent_settings WHERE key = ?",
                         (ctl.CURSOR_PREFIX + AG,)).fetchone()[0] == "2026-11-02T12:00:00Z"
