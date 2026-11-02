"""P6-3a: the slot table and the no_slot miss."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents import slots
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec
from levain.agents.store import open_agent_store
from levain.coordinator.lease import LEASE_SCHEMA

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)


def _store(tmp_path, cron="*/20 * * * *"):
    d = copy.deepcopy(SPECS["job_push_check"])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    d["trigger"]["cron"] = cron
    store = open_agent_store(tmp_path / "s")
    return store, register_spec(store, parse_spec(d), "America/New_York")[0]


def _launch(conn, launch_id, session_key, *events):
    """A launches row on the coordinator's own DDL, with the given event kinds in order."""
    for ddl in LEASE_SCHEMA:
        conn.execute(ddl)
    conn.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, token, placement, at)"
                 " VALUES (?, ?, ?, 'claude', 'coordinator', '[]', 1, 'p', '2026-11-02T11:00:00Z')",
                 (launch_id, abs(hash(launch_id)) % 10**6, session_key))
    for seq, kind in enumerate(events, 1):
        conn.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, ?, ?, ?, 'x', 't')",
                     (launch_id, seq, abs(hash((launch_id, seq))) % 10**6 + 10**7, kind))


def test_the_cap_is_shared_and_a_release_frees_it(tmp_path):
    store, _ = _store(tmp_path)
    with store.write() as c:
        assert slots.max_slots(c) == 2
        assert slots.take(c, slots.OCCURRENCE, "oc-a", T0) == 1
        assert slots.take(c, slots.LAUNCH, "ln-1", T0) == 2
        assert slots.take(c, slots.OCCURRENCE, "oc-b", T0) is None           # full: the caller keeps its refusal path
        assert slots.take(c, slots.OCCURRENCE, "oc-a", T0) == 1              # the same owner keeps its slot
        assert slots.release(c, "oc-a") and not slots.release(c, "oc-a")
        assert slots.take(c, slots.OCCURRENCE, "oc-b", T0) == 1              # lowest free
        c.execute("INSERT INTO agent_settings (key, value) VALUES ('max_slots', '3')")
        assert slots.take(c, slots.OCCURRENCE, "oc-c", T0) == 3              # the setting is the cap
        c.execute("UPDATE agent_settings SET value = '0' WHERE key = 'max_slots'")
        with pytest.raises(ValueError):                                      # an unreadable cap is not a free pass
            slots.max_slots(c)


def test_a_coordinator_lease_expires_at_900s_and_is_reclaimed_only_when_its_launch_is_over(tmp_path):
    store, _ = _store(tmp_path)
    alive = {"k-live": True, "k-dead": False}
    with store.write() as c:
        _launch(c, "ln-live", "k-live", "pending", "active")
        _launch(c, "ln-dead", "k-dead", "pending", "active")
        assert slots.take(c, slots.LAUNCH, "ln-live", T0) == 1
        assert slots.take(c, slots.LAUNCH, "ln-dead", T0) == 2
        assert c.execute("SELECT expires_at FROM agent_slots WHERE slot = 1").fetchone()[0] == "2026-11-02T12:15:00Z"
        # not yet expired: nothing is reclaimed, though one session is dead
        assert slots.reclaim(c, T0 + timedelta(seconds=899), alive.__getitem__) == []
        # expired: the dead session's lease goes, the live session's stays
        assert slots.reclaim(c, T0 + timedelta(seconds=900), alive.__getitem__) == [2]
        # a renewal pushes the live lease out
        assert slots.renew(c, "ln-live", T0 + timedelta(seconds=900))
        assert slots.reclaim(c, T0 + timedelta(seconds=1700), lambda k: False) == []
        assert slots.reclaim(c, T0 + timedelta(seconds=1800), lambda k: False) == [1]
        assert not slots.renew(c, "ln-live", T0 + timedelta(seconds=1801))   # gone: the watcher is told


def test_a_terminal_launch_frees_an_expired_lease_even_if_the_session_looks_alive(tmp_path):
    store, _ = _store(tmp_path)
    with store.write() as c:
        _launch(c, "ln-done", "k-done", "pending", "active", "ended")
        _launch(c, "ln-pend", "k-pend", "pending")
        assert slots.take(c, slots.LAUNCH, "ln-done", T0) == 1
        assert slots.take(c, slots.LAUNCH, "ln-pend", T0) == 2
        late = T0 + timedelta(seconds=901)
        assert slots.reclaim(c, late, lambda k: True) == [1]
        assert slots.reclaim(c, late, None) == []                            # liveness unknown: the slot is kept


def test_a_store_without_the_coordinator_tables_reclaims_an_expired_lease(tmp_path):
    store, _ = _store(tmp_path)
    with store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-orphan", T0)
        assert slots.reclaim(c, T0 + timedelta(seconds=900), None) == [1]


def _fill(store, ag, cursor="2026-11-02T11:58:00Z"):
    with store.write() as c:
        for i in (1, 2):
            slots.take(c, slots.OCCURRENCE, f"held-{i}", T0 - 60 * MIN)
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)", (ctl.CURSOR_PREFIX + ag, cursor))


def _rows(store):
    with store.read() as c:
        return c.execute("SELECT key, state, reason FROM agent_occurrences ORDER BY key").fetchall()


def test_a_named_occurrence_waits_for_a_slot_and_ends_missed_no_slot_after_its_grace(tmp_path):
    store, ag = _store(tmp_path)
    _fill(store, ag)
    armed = lambda a, h: "b1"  # noqa: E731
    r = ctl.tick(store, lambda: T0 + MIN, armed)
    assert r.claimed == [(ag, "2026-11-02T12:00:00Z")]
    # retried each tick through the grace; the row stays admitted at 15:00 exactly
    for minutes in (5, 14, 15):
        r = ctl.tick(store, lambda m=minutes: T0 + m * MIN, armed)
        assert r.no_slot == [] and _rows(store) == [("2026-11-02T12:00:00Z", "admitted", None)]
    r = ctl.tick(store, lambda: T0 + 15 * MIN + timedelta(seconds=60), armed)
    assert r.no_slot == [(ag, "2026-11-02T12:00:00Z")]
    assert r.missed == {ag: 1}                                               # the miss streak sees it
    assert _rows(store) == [("2026-11-02T12:00:00Z", "missed", "no_slot")]
    with store.read() as c:
        assert c.execute("SELECT ended_at FROM agent_occurrences").fetchone()[0] == "2026-11-02T12:16:00Z"
    # the next slot is no longer blocked as an overlap by the row the cap held
    r = ctl.tick(store, lambda: T0 + 21 * MIN, armed)
    assert r.claimed == [(ag, "2026-11-02T12:20:00Z")] and r.overlapped == []


def test_a_free_slot_never_ends_an_occurrence_as_no_slot(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
    ctl.tick(store, lambda: T0 + MIN, armed)
    r = ctl.tick(store, lambda: T0 + 40 * MIN, armed)                        # past grace, both slots free
    assert r.no_slot == [] and _rows(store)[0] == ("2026-11-02T12:00:00Z", "admitted", None)


def test_an_expired_coordinator_lease_frees_the_slot_the_tick_would_have_refused(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
        slots.take(c, slots.OCCURRENCE, "held-1", T0 - 60 * MIN)
        slots.take(c, slots.LAUNCH, "ln-gone", T0)                           # expires 12:15
    ctl.tick(store, lambda: T0 + MIN, armed)
    # at 12:16 the lease is expired and its launch absent: the tick reclaims it, a slot is free, nothing is missed
    r = ctl.tick(store, lambda: T0 + 16 * MIN, armed)
    assert r.no_slot == []
    with store.read() as c:
        assert [h[2] for h in slots.held(c)] == ["held-1"]
