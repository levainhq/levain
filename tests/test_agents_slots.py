"""P6-3a: the slot table and the no_slot miss."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import controller as ctl
from levain.agents import slots
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec
from levain.agents.store import open_agent_store
from levain.desk.lease import LEASE_SCHEMA
from levain.desk.legacy import SQL_ROLE_DESK

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


def _dispatch(tmp_path, *launches):
    """The desk's own file, apart from the agent tables' (`dispatch.db` beside `autonomic.db`): returns
    `(launch_over, session_alive)` reading it. Each launch is `(launch_id, session_key, *event kinds)`."""
    db = sqlite3.connect(tmp_path / "dispatch.db")
    for ddl in LEASE_SCHEMA:
        db.execute(ddl)
    for launch_id, session_key, *events in launches:
        db.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, token, placement, at)"
                   f" VALUES (?, ?, ?, 'claude', {SQL_ROLE_DESK}, '[]', 1, 'p', '2026-11-02T11:00:00Z')",
                   (launch_id, abs(hash(launch_id)) % 10**6, session_key))
        for seq, kind in enumerate(events, 1):
            db.execute("INSERT INTO launch_events (launch_id, seq, gseq, kind, cause, at) VALUES (?, ?, ?, ?, 'x', 't')",
                       (launch_id, seq, abs(hash((launch_id, seq))) % 10**6 + 10**7, kind))
    db.commit()
    alive = {}

    def launch_over(launch_id):
        if db.execute("SELECT 1 FROM launches WHERE launch_id = ?", (launch_id,)).fetchone() is None:
            return True
        last = db.execute("SELECT kind FROM launch_events WHERE launch_id = ? ORDER BY seq DESC LIMIT 1",
                          (launch_id,)).fetchone()
        return last is not None and last[0] in ("failed", "expired", "ended")

    def session_alive(launch_id):
        key = db.execute("SELECT session_key FROM launches WHERE launch_id = ?", (launch_id,)).fetchone()[0]
        return alive.get(key)

    return launch_over, session_alive, alive


def _reclaim(c, now, launch_over=None, session_alive=None):
    ids = slots.expired_launches(c, now)
    return slots.reclaim(c, now, slots.probe_over(ids, launch_over, session_alive))


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


def test_a_desk_lease_expires_at_900s_and_is_reclaimed_only_when_its_launch_is_over(tmp_path):
    store, _ = _store(tmp_path)
    over, alive_fn, alive = _dispatch(tmp_path, ("ln-live", "k-live", "pending", "active"),
                                      ("ln-dead", "k-dead", "pending", "active"))
    alive.update({"k-live": True, "k-dead": False})
    with store.write() as c:
        assert slots.take(c, slots.LAUNCH, "ln-live", T0) == 1
        assert slots.take(c, slots.LAUNCH, "ln-dead", T0) == 2
        assert c.execute("SELECT expires_at FROM agent_slots WHERE slot = 1").fetchone()[0] == "2026-11-02T12:15:00Z"
        # not yet expired: nothing is reclaimed, though one session is dead
        assert _reclaim(c, T0 + timedelta(seconds=899), over, alive_fn) == []
        # expired: the dead session's lease goes, the live session's stays
        assert _reclaim(c, T0 + timedelta(seconds=900), over, alive_fn) == [2]
        # a renewal pushes the live lease out
        assert slots.renew(c, "ln-live", T0 + timedelta(seconds=900))
        alive["k-live"] = False
        assert _reclaim(c, T0 + timedelta(seconds=1700), over, alive_fn) == []
        assert _reclaim(c, T0 + timedelta(seconds=1800), over, alive_fn) == [1]
        assert not slots.renew(c, "ln-live", T0 + timedelta(seconds=1801))   # gone: the watcher is told


def test_a_terminal_launch_frees_an_expired_lease_even_if_the_session_looks_alive(tmp_path):
    store, _ = _store(tmp_path)
    over, alive_fn, alive = _dispatch(tmp_path, ("ln-done", "k-done", "pending", "active", "ended"),
                                      ("ln-pend", "k-pend", "pending"))
    alive.update({"k-done": True, "k-pend": True})
    with store.write() as c:
        assert slots.take(c, slots.LAUNCH, "ln-done", T0) == 1
        assert slots.take(c, slots.LAUNCH, "ln-pend", T0) == 2
        late = T0 + timedelta(seconds=901)
        assert _reclaim(c, late, None, alive_fn) == []                       # no launch state: the slot is kept
        assert _reclaim(c, late, over, alive_fn) == [1]


def test_a_launch_the_agent_store_cannot_see_is_never_freed_without_the_probe(tmp_path):
    """Planted: the slots in autonomic.db, the launches in dispatch.db. The old join met `no such table` and freed
    a live desk's slot without asking whether its session ran."""
    store, _ = _store(tmp_path)
    over, alive_fn, alive = _dispatch(tmp_path, ("ln-live", "k-live", "pending", "active"))
    alive["k-live"] = True
    with store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-live", T0)
        late = T0 + timedelta(seconds=901)
        assert _reclaim(c, late) == [] and _reclaim(c, late, None, alive_fn) == []
        assert _reclaim(c, late, over, alive_fn) == []                       # live: kept
        for broken in (lambda i: None, lambda i: 1 / 0):                      # unknown or raising: kept
            assert _reclaim(c, late, broken, alive_fn) == []
            assert _reclaim(c, late, over, broken) == []
        alive["k-live"] = False
        assert _reclaim(c, late, over, alive_fn) == [1]


def test_a_reclaim_rechecks_the_row_the_probe_saw(tmp_path):
    store, _ = _store(tmp_path)
    with store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-1", T0)
        verdict = slots.probe_over(slots.expired_launches(c, T0 + timedelta(seconds=901)), lambda i: True)
        assert verdict == {"ln-1"}
        slots.renew(c, "ln-1", T0 + timedelta(seconds=901))                  # renewed after the probe
        assert slots.reclaim(c, T0 + timedelta(seconds=901), verdict) == []
        assert slots.reclaim(c, T0 + timedelta(seconds=901 + 900), verdict) == [1]


def test_a_renewal_never_moves_an_expiry_backward(tmp_path):
    store, _ = _store(tmp_path)
    with store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-1", T0 + timedelta(seconds=600))     # expires 12:25
        assert slots.renew(c, "ln-1", T0)                                    # a late pass: 12:15 would be earlier
        assert c.execute("SELECT expires_at FROM agent_slots").fetchone()[0] == "2026-11-02T12:25:00Z"


def test_a_probe_that_raises_on_a_null_session_key_keeps_the_slot_and_the_tick_finishes(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
        slots.take(c, slots.OCCURRENCE, "held-1", T0 - 60 * MIN)
        slots.take(c, slots.LAUNCH, "ln-nokey", T0)

    def launch_over(launch_id):
        return False

    def session_alive(launch_id):
        raise TypeError("session_key is NULL")                                 # the caller's NULL key, as a raise

    ctl.tick(store, lambda: T0 + MIN, armed)
    r = ctl.tick(store, lambda: T0 + 16 * MIN, armed, session_alive, launch_over)
    assert r.no_slot == [(ag, "2026-11-02T12:00:00Z")]                       # the kept lease still fills the cap
    with store.read() as c:
        assert [h[2] for h in slots.held(c)] == ["held-1", "ln-nokey"]       # kept, tick finished


def _lock_probe_outcome(tmp_path, tick):
    store, ag = _store(tmp_path)
    with store.write() as c:
        slots.take(c, slots.LAUNCH, "ln-1", T0)
    other = sqlite3.connect(store.path, timeout=0.1, isolation_level=None)
    outcome = []

    def launch_over(launch_id):
        try:
            other.execute("BEGIN IMMEDIATE")                                 # 'database is locked' if tick holds it
            other.execute("ROLLBACK")
            outcome.append("acquired")
        except sqlite3.OperationalError as exc:
            outcome.append(str(exc))
        return False

    tick(store, lambda: T0 + 16 * MIN, lambda a, h: None, None, launch_over)
    other.close()
    return outcome


def test_a_probe_does_not_hold_the_write_lock(tmp_path):
    # the outcome is recorded in the test, not inferred from the tick: `_ask` swallows a probe's exception
    assert _lock_probe_outcome(tmp_path, ctl.tick) == ["acquired"]


def test_the_lock_probe_test_can_fail(tmp_path, monkeypatch):
    """A tick that probes inside its write transaction makes the same test fail, so it is not vacuous."""
    def probing_inside_write(store, clock, binding_of, session_alive=None, launch_over=None):
        now = clock()
        with store.read() as conn:
            expired = slots.expired_launches(conn, now)
        with store.write():
            slots.probe_over(expired, launch_over, session_alive)

    out = _lock_probe_outcome(tmp_path, probing_inside_write)
    assert out and out != ["acquired"] and "locked" in out[0]


def _full_cap_with(store, ag, kind, key, admitted_at):
    with store.write() as c:
        for i in (1, 2):
            slots.take(c, slots.OCCURRENCE, f"held-{i}", T0 - 60 * MIN)
        ctl.claim(c, agent_id=ag, kind=kind, key=key, spec_hash="h", binding_id="b1", window_key="w", now=T0 - 60 * MIN)
        c.execute("DROP TRIGGER agent_occurrences_identity")                 # a corrupted row is the fixture
        c.execute("UPDATE agent_occurrences SET admitted_at = ?", (admitted_at,))


@pytest.mark.parametrize("kind,key,admitted_at", [
    ("scheduled", "2026-11-02T11:00:00Z", "not-a-stamp"),
    ("scheduled", "not-a-stamp", "2026-11-02T11:00:00Z"),
    ("manual", "x", "not-a-stamp"),
])
def test_an_unparseable_stamp_keeps_the_row_admitted_and_never_aborts_the_tick(tmp_path, kind, key, admitted_at):
    store, ag = _store(tmp_path)
    _full_cap_with(store, ag, kind, key, admitted_at)
    with store.write() as c:
        assert ctl.close_no_slot(c, T0) == []
    r = ctl.tick(store, lambda: T0, lambda a, h: None)                       # the tick itself goes on
    assert r.no_slot == []
    with store.read() as c:
        assert c.execute("SELECT state FROM agent_occurrences").fetchone()[0] == "admitted"


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
    # D1-Q: still never ended as no_slot (that reason is for a held cap); a row past its grace that nothing launched
    # now ends for what it is
    assert r.no_slot == [] and _rows(store)[0] == ("2026-11-02T12:00:00Z", "missed", ctl.NEVER_LAUNCHED)


def test_an_expired_desk_lease_frees_the_slot_the_tick_would_have_refused(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    over, alive_fn, alive = _dispatch(tmp_path)                              # no launch rows: ln-gone is absent
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
        slots.take(c, slots.OCCURRENCE, "held-1", T0 - 60 * MIN)
        slots.take(c, slots.LAUNCH, "ln-gone", T0)                           # expires 12:15
    ctl.tick(store, lambda: T0 + MIN, armed)
    # at 12:16 the lease is expired and its launch absent: the tick reclaims it, a slot is free, nothing is missed
    r = ctl.tick(store, lambda: T0 + 16 * MIN, armed, alive_fn, over)
    assert r.no_slot == []
    with store.read() as c:
        assert [h[2] for h in slots.held(c)] == ["held-1"]


def test_without_probes_a_full_cap_holds_and_the_occurrence_ends_no_slot(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
        slots.take(c, slots.OCCURRENCE, "held-1", T0 - 60 * MIN)
        slots.take(c, slots.LAUNCH, "ln-gone", T0)
    ctl.tick(store, lambda: T0 + MIN, armed)
    r = ctl.tick(store, lambda: T0 + 40 * MIN, armed)
    assert r.no_slot == [(ag, "2026-11-02T12:00:00Z")]


def test_closing_an_occurrence_releases_a_slot_it_holds_and_an_unreadable_cap_halts_nothing(tmp_path):
    store, ag = _store(tmp_path)
    armed = lambda a, h: "b1"  # noqa: E731
    with store.write() as c:
        c.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?)",
                  (ctl.CURSOR_PREFIX + ag, "2026-11-02T11:58:00Z"))
    ctl.tick(store, lambda: T0 + MIN, armed)
    with store.write() as c:
        oid = c.execute("SELECT occurrence_id FROM agent_occurrences").fetchone()[0]
        slots.take(c, slots.OCCURRENCE, oid, T0)                             # not reachable by A0 §2.4; defensive
        slots.take(c, slots.OCCURRENCE, "held-2", T0)
    r = ctl.tick(store, lambda: T0 + 40 * MIN, armed)
    assert r.no_slot == [(ag, "2026-11-02T12:00:00Z")]
    with store.read() as c:
        assert [h[2] for h in slots.held(c)] == ["held-2"]
    with store.write() as c:                                                 # a cap nobody can read
        c.execute("INSERT INTO agent_settings (key, value) VALUES ('max_slots', 'lots')")
    r = ctl.tick(store, lambda: T0 + 61 * MIN, armed)                        # the tick completes and still claims
    # it walked the slot, nothing halted. D1-Q: the 12:40 row, admitted and 21 minutes old, is ended without reading
    # the cap (an admitted row holds nothing), so 13:00 is claimed, not filed skipped-overlap behind it
    assert r.claimed == [(ag, "2026-11-02T13:00:00Z")] and r.overlapped == [] and r.no_slot == []
    assert r.unlaunched == [(ag, "2026-11-02T12:40:00Z")]
