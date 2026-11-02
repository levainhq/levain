"""P6-1b: the occurrence claim and the controller tick."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec, set_operation
from levain.agents.store import AgentStoreRefused, open_agent_store
from levain.autonomic.db import DB_NAME

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
NY = "America/New_York"
EVERY_20 = "*/20 * * * *"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
AG = ""  # set per test by _store: the agent_id register_spec minted


def _spec(cron, name):
    d = copy.deepcopy(SPECS["job_push_check"])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    d["name"] = name
    d["trigger"]["cron"] = cron
    return parse_spec(d)


def _register(store, cron=EVERY_20, name="job_push_check"):
    return register_spec(store, _spec(cron, name), NY)[0]


def _store(tmp_path, cron=EVERY_20):
    global AG
    store = open_agent_store(tmp_path / "s")
    AG = _register(store, cron)
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


def _set_cursor(store, agent_id, stamp):
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                  " value = excluded.value", (ctl.CURSOR_PREFIX + agent_id, stamp))


def _get_cursor(store, agent_id):
    with store.read() as c:
        return c.execute("SELECT value FROM agent_settings WHERE key = ?",
                         (ctl.CURSOR_PREFIX + agent_id,)).fetchone()[0]


def _rewrite_spec(store, agent_id, text):
    """Tamper with a stored spec the way only a direct writer could: around the freeze trigger, then restore it."""
    conn = sqlite3.connect(store.path, isolation_level=None)
    try:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'agent_specs_frozen'").fetchone()[0]
        conn.execute("DROP TRIGGER agent_specs_frozen")
        conn.execute("UPDATE agent_specs SET spec = ? WHERE agent_id = ?", (text, agent_id))
        conn.execute(sql)
    finally:
        conn.close()


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
    store = _store(tmp_path, "0 9 * * *")
    _set_cursor(store, AG, "2026-04-16T12:00:00Z")  # last looked 200 days ago: more than two MAX_SPAN walks
    out = ctl.tick(store, _clock([T0]), _armed)
    assert out.missed[AG] >= 190 and out.claimed == []  # old slots are counted, never fired
    assert _get_cursor(store, AG) == "2026-11-02T12:00:00Z"


# ---- L3 r1 -------------------------------------------------------------------------------------------------------

def test_a_tampered_stored_spec_never_fires(tmp_path):
    # codex 1: a daily spec rewritten to every minute under its authorized hash
    store = _store(tmp_path, "0 9 * * *")
    _set_cursor(store, AG, "2026-11-02T11:55:00Z")
    with store.read() as c:
        text = c.execute("SELECT spec FROM agent_specs WHERE agent_id = ?", (AG,)).fetchone()[0]
    _rewrite_spec(store, AG, text.replace("0 9 * * *", "* * * * *"))
    seen = []
    out = ctl.tick(open_agent_store(tmp_path / "s"), _clock([T0]), lambda a, h: seen.append(a) or "b1")
    assert out.claimed == [] and _rows(store) == [] and seen == []  # not even asked for a binding
    assert [a for a, _ in out.refused] == [AG] and "does not hash to its spec_hash" in out.refused[0][1]
    assert _get_cursor(store, AG) == "2026-11-02T12:00:00Z"  # a repaired spec later releases no backlog


def test_one_unreadable_spec_does_not_starve_the_other_agents(tmp_path):
    # deepseek 2: agents tick in agent_id order; the bad one must not stop the rest
    store = open_agent_store(tmp_path / "s")
    ids = sorted([_register(store, name="job_a"), _register(store, name="job_b")])
    for a in ids:
        _set_cursor(store, a, "2026-11-02T12:15:00Z")
    _rewrite_spec(store, ids[0], "{}")
    out = ctl.tick(open_agent_store(tmp_path / "s"), _clock([T0 + timedelta(minutes=21)]), _armed)
    assert [a for a, _ in out.refused] == [ids[0]] and out.claimed == [(ids[1], "2026-11-02T12:20:00Z")]


def test_a_pause_between_the_snapshot_and_the_write_claims_nothing(tmp_path):
    # codex 2: the lifecycle is re-read inside the claim's write
    store = _store(tmp_path)
    _set_cursor(store, AG, "2026-11-02T12:15:00Z")
    other = open_agent_store(tmp_path / "s")

    def pause_then_bind(agent_id, spec_hash):
        set_operation(other, agent_id, "paused")
        return "b1"
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=21)]), pause_then_bind)
    assert out.claimed == [] and out.changed == [AG] and _rows(store) == []
    assert _get_cursor(store, AG) == "2026-11-02T12:15:00Z"  # left exactly as it was for the next tick


def test_a_paused_agents_cursor_advances_so_resume_releases_no_backlog(tmp_path):
    # codex 3: the 12:20 slot passes while paused; resuming at 12:30 is within its grace
    store = _store(tmp_path)
    _set_cursor(store, AG, "2026-11-02T12:00:00Z")
    set_operation(store, AG, "paused")
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=21)]), _armed)
    assert out.paused == [AG] and _get_cursor(store, AG) == "2026-11-02T12:21:00Z"
    set_operation(store, AG, "active")
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=30)]), _armed)
    assert out.claimed == [] and _rows(store) == []


def test_each_chunk_commits_and_revalidates_before_the_next(tmp_path, monkeypatch):
    # codex 4: the lifecycle is read again between chunks, and the chunk already walked stays committed
    store = _store(tmp_path, "0 9 * * *")
    _set_cursor(store, AG, "2026-04-16T12:00:00Z")
    other = open_agent_store(tmp_path / "s")
    real = ctl.schedule.slots_between
    calls = []

    def pause_after_first(trigger, zone, after, upto):
        calls.append(upto)
        if len(calls) == 2:
            set_operation(other, AG, "paused")
        return real(trigger, zone, after, upto)
    monkeypatch.setattr(ctl.schedule, "slots_between", pause_after_first)
    out = ctl.tick(store, _clock([T0]), _armed)
    first_end = datetime(2026, 4, 16, 12, tzinfo=timezone.utc) + ctl.schedule.MAX_SPAN
    assert len(calls) == 2 and out.changed == [AG]
    assert _get_cursor(store, AG) == ctl._stamp(first_end)  # chunk one committed, chunk two not
    assert isinstance(out.missed[AG], int) and 85 <= out.missed[AG] <= 91


def test_a_cursor_past_the_clock_is_clamped_to_now_and_reported(tmp_path):
    # codex 5 / complement 3 / deepseek 3: never a silent disable, and the only backward move is to now
    store = _store(tmp_path)
    _set_cursor(store, AG, "2099-01-01T00:00:00Z")
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=19)]), _armed)
    assert out.clamped == [(AG, "2099-01-01T00:00:00Z")] and out.claimed == []
    assert _get_cursor(store, AG) == "2026-11-02T12:19:00Z"
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=21)]), _armed)  # the next slot fires as usual
    assert out.claimed == [(AG, "2026-11-02T12:20:00Z")] and out.clamped == []
    _set_cursor(store, AG, "2026-11-02T13:00:00Z")  # unarmed: the same explicit clamp, never a silent rewrite
    out = ctl.tick(store, _clock([T0 + timedelta(minutes=30)]), lambda a, h: None)
    assert out.unarmed == [AG] and out.clamped == [(AG, "2026-11-02T13:00:00Z")]


class _RacingConn:
    """A connection in a deferred BEGIN whose rival delivery commits just before its INSERT."""

    def __init__(self, conn, rival):
        self.conn, self.rival = conn, rival

    @property
    def in_transaction(self):
        return self.conn.in_transaction

    def execute(self, sql, *args):
        if sql.lstrip().startswith("INSERT") and self.rival is not None:
            self.rival, rival = None, self.rival
            rival()
        return self.conn.execute(sql, *args)


def test_a_duplicate_under_a_deferred_begin_is_still_duplicate(tmp_path):
    # codex 6 / complement 4-5: the rival commits between this delivery's BEGIN and its INSERT
    store = _store(tmp_path)
    key = "2026-11-02T12:20:00Z"
    first = []

    def rival():
        first.append(_claim(store, key))
    conn = sqlite3.connect(store.path, isolation_level=None, timeout=2)
    conn.execute("BEGIN")
    try:
        got = ctl.claim(_RacingConn(conn, rival), agent_id=AG, kind="scheduled", key=key, spec_hash="h1",
                        binding_id="b1", window_key="d:2026-11-02", now=T0)
    finally:
        conn.execute("ROLLBACK")
        conn.close()
    assert first[0][1] == ctl.CLAIMED and got == (first[0][0], ctl.DUPLICATE)
    assert _rows(store) == [(key, "admitted", None)]


def test_a_naive_now_is_refused_and_writes_nothing(tmp_path):
    # codex 7 / deepseek 4: a naive datetime would be stamped in the host's zone
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        with store.write() as c:
            ctl.claim(c, agent_id=AG, kind="scheduled", key="k", spec_hash="h1", binding_id="b1",
                      window_key="d:2026-11-02", now=datetime(2026, 11, 2, 12, 0))
    assert _rows(store) == []
