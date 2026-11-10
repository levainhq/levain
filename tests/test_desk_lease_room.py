"""The ``room`` launch role (RM-D1; DESIGN_ROOM L1-L5): the ``launches`` rebuild, the room's own open and activate, and
the rule that every reader meaning "a session that works" leaves a room out."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.desk import lease
from levain.desk.store import CoordError, CoordStore, Part
from levain.desk.legacy import ROLE_DESK, SQL_ROLE_DESK

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock):
    s = CoordStore(qdir, clock=clock)
    s.coord_set_agent("claude", cwd=str(qdir.parent))
    return s


def _thread(store):
    d = store.accept_drop("talk about it", surface="cli")[0]
    return store.split(d.drop_id, [Part("work", "x")])[0].thread_id


def _open_room(store, thread_id, pid=4242, harness="claude", cwd=None):
    with store._write() as (conn, now):
        return lease.open_room_launch(conn, now, harness=harness, model=None, cwd=cwd or str(store.path.parent),
                                      room_thread=thread_id, pid=pid)


def _activate_room(store, launch_id, sid="room-sid"):
    with store._write() as (conn, now):
        return lease.activate_room(conn, now, launch_id, harness_session_id=sid, argv_sha256="a" * 64)


def _db(store):
    return sqlite3.connect(store.path)


_OLD_ROLE = f"role TEXT NOT NULL CHECK (role IN ({SQL_ROLE_DESK}, 'worker'))"
_NEW_ROLE = f"role TEXT NOT NULL CHECK (role IN ({SQL_ROLE_DESK}, 'worker', 'room'))"


def _age(store) -> None:
    """Make the store's ``launches`` table the shape an earlier release made (the role CHECK without ``room``), by the
    same hold-and-refill the migration uses, with foreign keys off: every row, rowid and trigger survives."""
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("BEGIN IMMEDIATE")
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'launches'").fetchone()[0]
        assert _NEW_ROLE in sql
        triggers = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = "
                                            "'launches'")]
        cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(launches)"))
        c.execute(f"CREATE TEMP TABLE hold AS SELECT rowid AS rid, {cols} FROM launches")
        c.execute("DROP TABLE launches")
        c.execute(sql.replace(_NEW_ROLE, _OLD_ROLE))
        c.execute(f"INSERT INTO launches (rowid, {cols}) SELECT rid, {cols} FROM hold")
        c.execute("DROP TABLE hold")
        for t in triggers:
            c.execute(t)
        c.execute("COMMIT")
    finally:
        c.close()


def _snapshot(store):
    c = _db(store)
    try:
        return {t: c.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("launches", "launch_events", "launch_spawns", "lease_events")}
    finally:
        c.close()


def _a_store_with_history(store, clock):
    """Desk launches (one active, one pending), a worker, a spawn claim: every child table holds a row."""
    d = store.accept_drop("fix the build", surface="cli")[0]
    ln = store.coord_status()["launches"][0]
    assert store.launch_activate(ln["launch_id"], harness_session_id="probe-A")[1] is None
    token = store.coord_status()["lease"]["token"]
    t = store.split(d.drop_id, [Part("work", "x")], by=ROLE_DESK, token=token)[0]
    worker = store.launch_request(token, harness="claude", threads=[t.thread_id])
    assert store.launch_claim_spawn(worker.launch_id, 77)[1] is None
    return t.thread_id, worker.launch_id


# --- L1: the rebuild --------------------------------------------------------------------------------------------------
def test_an_old_store_upgrades_with_every_row_and_key_intact(store, clock):
    thread, worker = _a_store_with_history(store, clock)
    before = _snapshot(store)
    assert before["launches"] and before["launch_events"] and before["launch_spawns"]
    _age(store)
    assert _OLD_ROLE in _db(store).execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()[0]
    fresh = CoordStore(store.path.parent, clock=clock)          # a new opener ensures the schema: the upgrade
    fresh.coord_status()
    assert _snapshot(store) == before
    c = _db(store)
    try:
        assert _NEW_ROLE in c.execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()[0]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name = 'launches' AND type = "
                                        "'trigger'")} == {"launches_frozen", "launches_kept", "launches_no_replace"}
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("UPDATE launches SET token = 99")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute("DELETE FROM launches")
    finally:
        c.close()
    assert fresh.launch(worker).state == "pending"              # and the store still works
    assert _open_room(fresh, thread).role == "room"


def test_the_upgrade_keeps_a_non_default_connections_foreign_key_setting(store, clock):
    _a_store_with_history(store, clock)
    _age(store)
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA defer_foreign_keys = OFF")
        c.execute("BEGIN IMMEDIATE")
        assert lease.migrate_launch_roles(c) is True
        c.execute("COMMIT")
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA defer_foreign_keys").fetchone()[0] == 0
    finally:
        c.close()


def test_the_migration_runs_only_inside_a_transaction_and_a_current_store_is_a_no_op(store, clock):
    _a_store_with_history(store, clock)
    c = _db(store)
    try:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        assert lease.migrate_launch_roles(c) is False
        c.execute("COMMIT")
        _age(store)
        with pytest.raises(CoordError, match="inside the caller's BEGIN IMMEDIATE"):
            lease.migrate_launch_roles(c)
    finally:
        c.close()


def _refused_rebuild(store, mutate, match):
    """An aged store changed by ``mutate`` (a newer or hand-changed shape): the rebuild refuses by name and the file is
    byte-for-byte what it was."""
    _age(store)
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        mutate(c)
    finally:
        c.close()
    before = _snapshot(store)
    schema = _db(store).execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    with pytest.raises(CoordError, match=match):
        CoordStore(store.path.parent).coord_status()
    assert _snapshot(store) == before
    assert _db(store).execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall() == schema


def test_a_newer_store_with_an_unknown_column_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)
    _refused_rebuild(store, lambda c: c.execute("ALTER TABLE launches ADD COLUMN newer TEXT"),
                     r"launches holds column\(s\) newer")


def test_a_generated_column_is_named_not_dropped(store, clock):
    _a_store_with_history(store, clock)
    _refused_rebuild(store, lambda c: c.execute("ALTER TABLE launches ADD COLUMN twice INTEGER GENERATED ALWAYS "
                                                "AS (token * 2) VIRTUAL"), r"launches holds column\(s\) twice")


def test_an_unknown_index_or_a_changed_trigger_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)
    _refused_rebuild(store, lambda c: c.execute("CREATE INDEX launches_by_harness ON launches (harness)"),
                     "launches holds launches_by_harness")


def test_a_known_trigger_whose_text_changed_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)

    def change(c):
        c.execute("DROP TRIGGER launches_kept")
        c.execute("CREATE TRIGGER launches_kept BEFORE DELETE ON launches BEGIN SELECT RAISE(ABORT, 'other'); END")

    _refused_rebuild(store, change, "launches holds launches_kept")


def test_an_unknown_unique_constraint_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)

    def add(c):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'launches'").fetchone()[0]
        triggers = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = "
                                            "'launches'")]
        cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(launches)"))
        c.execute("BEGIN IMMEDIATE")
        c.execute(f"CREATE TEMP TABLE hold AS SELECT rowid AS rid, {cols} FROM launches")
        c.execute("DROP TABLE launches")
        c.execute(re.sub(r"\)$", ", UNIQUE (harness, cwd, at))", sql))
        c.execute(f"INSERT INTO launches (rowid, {cols}) SELECT rid, {cols} FROM hold")
        c.execute("DROP TABLE hold")
        for t in triggers:
            c.execute(t)
        c.execute("COMMIT")

    # a UNIQUE the CREATE of this release lacks: an automatic index, no text, found by its origin and columns
    _refused_rebuild(store, add, r"launches holds a constraint on harness, cwd, at")


def _add_child(action, parent="launches"):
    def mutate(c):
        c.execute(f"CREATE TABLE launch_notes (id INTEGER PRIMARY KEY, launch_id TEXT REFERENCES {parent}(launch_id) "
                  f"ON DELETE {action})")
        c.execute("INSERT INTO launch_notes (launch_id) SELECT launch_id FROM launches")
    return mutate


@pytest.mark.parametrize("action", ["CASCADE", "SET NULL", "SET DEFAULT"])
def test_a_child_table_that_acts_on_delete_refuses_the_rebuild(store, clock, action):
    _a_store_with_history(store, clock)
    _refused_rebuild(store, _add_child(action), rf"launch_notes\(launch_id\) ON DELETE {action}")


@pytest.mark.parametrize("parent", ["LAUNCHES", "Launches"])
@pytest.mark.parametrize("action", ["CASCADE", "SET NULL"])
def test_a_child_naming_launches_in_another_case_still_refuses_the_rebuild(store, clock, action, parent):
    # SQLite names are case-insensitive, and foreign_key_list reports the spelling the child wrote
    _a_store_with_history(store, clock)
    _refused_rebuild(store, _add_child(action, parent), rf"launch_notes\(launch_id\) ON DELETE {action}")


@pytest.mark.parametrize("action", ["NO ACTION", "RESTRICT"])
def test_a_child_with_no_delete_action_does_not_block_the_rebuild(store, clock, action):
    _a_store_with_history(store, clock)
    _age(store)
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        _add_child(action)(c)
    finally:
        c.close()
    CoordStore(store.path.parent).coord_status()
    assert _NEW_ROLE in _db(store).execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()[0]


def test_a_newer_role_check_with_no_rows_of_it_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)

    def widen(c):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'launches'").fetchone()[0]
        triggers = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = "
                                            "'launches'")]
        cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(launches)"))
        c.execute("BEGIN IMMEDIATE")
        c.execute(f"CREATE TEMP TABLE hold AS SELECT rowid AS rid, {cols} FROM launches")
        c.execute("DROP TABLE launches")
        c.execute(sql.replace(_OLD_ROLE, f"role TEXT NOT NULL CHECK (role IN ({SQL_ROLE_DESK}, 'worker', 'scribe'))"))
        c.execute(f"INSERT INTO launches (rowid, {cols}) SELECT rid, {cols} FROM hold")
        c.execute("DROP TABLE hold")
        for t in triggers:
            c.execute(t)
        c.execute("COMMIT")

    _refused_rebuild(store, widen, "role CHECK this release does not know")


def test_a_legacy_role_check_with_any_other_change_to_the_definition_refuses_by_name(store, clock):
    _a_store_with_history(store, clock)

    def add_check(c):
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'launches'").fetchone()[0]
        triggers = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = "
                                            "'launches'")]
        cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(launches)"))
        c.execute("BEGIN IMMEDIATE")
        c.execute(f"CREATE TEMP TABLE hold AS SELECT rowid AS rid, {cols} FROM launches")
        c.execute("DROP TABLE launches")
        c.execute(sql[:-1] + ", CHECK (token >= 0))")
        c.execute(f"INSERT INTO launches (rowid, {cols}) SELECT rid, {cols} FROM hold")
        c.execute("DROP TABLE hold")
        for t in triggers:
            c.execute(t)
        c.execute("COMMIT")

    _refused_rebuild(store, add_check, "table definition this release does not know")


def test_the_legacy_text_with_its_alter_added_columns_appended_still_upgrades(store, clock):
    # a table from before user_scope and role_ref carries them as ALTER appends, not inline
    _a_store_with_history(store, clock)
    _age(store)
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        c.execute("PRAGMA foreign_keys = OFF")
        c.execute("ALTER TABLE launches DROP COLUMN role_ref")
        c.execute("ALTER TABLE launches DROP COLUMN user_scope")
        c.execute("ALTER TABLE launches ADD COLUMN user_scope INTEGER NOT NULL DEFAULT 0 CHECK (user_scope IN (0, 1))")
        c.execute("ALTER TABLE launches ADD COLUMN role_ref TEXT")
    finally:
        c.close()
    CoordStore(store.path.parent).coord_status()
    assert _NEW_ROLE in _db(store).execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()[0]


def test_an_older_opener_leaves_the_new_table_alone(store, clock):
    # what an earlier release does to a store this release made: its CREATE ... IF NOT EXISTS statements change nothing
    thread, _w = _a_store_with_history(store, clock)
    room = _open_room(store, thread)
    old = tuple(s.replace(_NEW_ROLE, _OLD_ROLE) for s in lease.LEASE_SCHEMA)
    before = _snapshot(store)
    c = sqlite3.connect(store.path, isolation_level=None)
    try:
        for stmt in old:
            c.execute(stmt)
    finally:
        c.close()
    assert _snapshot(store) == before
    assert _NEW_ROLE in _db(store).execute("SELECT sql FROM sqlite_master WHERE name = 'launches'").fetchone()[0]
    assert store.launch(room.launch_id).role == "room"


# --- L2, L3: the room's own open and activate -------------------------------------------------------------------------
def test_a_room_launch_is_pending_spawn_claimed_and_never_unspawned(store, clock):
    thread = _thread(store)
    room = _open_room(store, thread, pid=99)
    assert (room.role, room.state, room.threads, room.token, room.harness) == ("room", "pending", (thread,), 0,
                                                                              "claude")
    assert room.until is not None
    c = _db(store)
    try:
        assert c.execute("SELECT pid FROM launch_spawns WHERE launch_id = ?", (room.launch_id,)).fetchone() == (99,)
    finally:
        c.close()
    with store._write() as (conn, _now):
        assert room.launch_id not in {ln.launch_id for ln in lease.unspawned(conn)}   # the desk's is there
        assert lease.claim_spawn(conn, _now, room.launch_id, 5)[1] is not None     # already claimed: never spawned twice


def test_a_room_launch_validates_what_it_is_given(store, clock):
    thread = _thread(store)
    with store._write() as (conn, now):
        for kw, match in (({"harness": "gemini"}, "harness is one of"), ({"room_thread": "th-" + "0" * 32}, "no room thread"),
                          ({"room_thread": "nope"}, "no room thread"), ({"pid": 0}, "positive integer pid"),
                          ({"pid": True}, "positive integer pid"), ({"model": "--flag"}, "model")):
            args = dict(harness="claude", model=None, cwd="/tmp", room_thread=thread, pid=1) | kw
            with pytest.raises(CoordError, match=match):
                lease.open_room_launch(conn, now, **args)
        assert conn.execute("SELECT COUNT(*) FROM launches WHERE role = 'room'").fetchone() == (0,)


def test_activate_room_takes_no_lease_and_pins_no_envelope(store, clock):
    thread = _thread(store)                         # no lease held, no desk: a room needs neither
    assert store.coord_status()["lease"]["cause"] == "bootstrap"      # only the desk's own; the room reads none
    room = _open_room(store, thread)
    got, why = _activate_room(store, room.launch_id)
    assert why is None and got.state == "active" and got.harness_session_id == "room-sid"
    assert got.pinned_envelope_id is None
    c = _db(store)
    try:
        assert c.execute("SELECT argv_sha256 FROM launch_events WHERE launch_id = ? AND kind = 'active'",
                         (room.launch_id,)).fetchone() == ("a" * 64,)
    finally:
        c.close()
    again, why = _activate_room(store, room.launch_id)
    assert again.state == "active" and "not pending" in why


def test_a_room_launch_past_its_until_is_expired_not_activated(store, clock):
    room = _open_room(store, _thread(store))
    clock.advance(minutes=6)
    got, why = _activate_room(store, room.launch_id)
    assert got.state == "expired" and "pending past" in why


def test_a_worker_activation_path_refuses_a_room_and_the_room_path_refuses_a_worker(store, clock):
    thread, worker = _a_store_with_history(store, clock)
    room = _open_room(store, thread)
    with pytest.raises(lease.LeaseRefused, match="activate_room"):
        store.launch_activate(room.launch_id, harness_session_id="sid")
    with pytest.raises(lease.LeaseRefused, match="not a room"):
        _activate_room(store, worker)
    assert store.launch(room.launch_id).state == "pending"
    assert store.launch(worker).state == "pending"


def test_a_room_is_never_asked_for_under_a_lease(store, clock):
    thread, _w = _a_store_with_history(store, clock)
    token = store.coord_status()["lease"]["token"]
    with pytest.raises(CoordError, match="open_room_launch"):
        store.launch_request(token, harness="claude", role="room", threads=[thread])


# --- L4: the readers that mean a session that works -------------------------------------------------------------------
def test_a_takeover_does_not_fail_a_pending_or_end_an_active_room(store, clock):
    thread, worker = _a_store_with_history(store, clock)
    pending = _open_room(store, thread)
    live = _open_room(store, thread, pid=2)
    assert _activate_room(store, live.launch_id)[1] is None
    store.coord_take()
    assert store.launch(pending.launch_id).state == "pending"
    assert store.launch(live.launch_id).state == "active"
    assert store.launch(worker).state == "failed"               # the worker's pending launch is still voided
    assert store.coord_status()["lease"]["holder"] == "operator"


def test_a_pending_room_is_not_expired_by_the_lazy_expiry_and_the_status_ignores_it(store, clock):
    thread = _thread(store)
    room = _open_room(store, thread)
    clock.advance(hours=1)
    st = store.coord_status()
    assert store.launch(room.launch_id).state == "pending"
    with store._write() as (conn, _now):
        assert lease._in_state(conn, "pending") == []
        assert [ln.launch_id for ln in lease._in_state(conn, "pending", "room")] == [room.launch_id]
        assert lease.wake_state(conn, _now)[0] != "launching"
    assert st["wake"] != "launching"


def test_in_state_defaults_to_desk_or_worker_and_names_a_room_only_when_asked(store, clock):
    thread, worker = _a_store_with_history(store, clock)
    room = _open_room(store, thread)
    with store._write() as (conn, now):
        roles = {ln.role for st in ("pending", "active") for ln in lease._in_state(conn, st)}
        assert roles == {ROLE_DESK, "worker"}
        assert [ln.launch_id for ln in lease._in_state(conn, "pending", "room")] == [room.launch_id]
    assert room.launch_id not in {ln.launch_id for ln in store.launches_active()}


def test_by_harness_session_maps_a_room_to_no_worker_unless_asked(store, clock):
    thread = _thread(store)
    room = _open_room(store, thread)
    assert _activate_room(store, room.launch_id, sid="room-sid")[1] is None
    with store._write() as (conn, _now):
        assert lease.by_harness_session(conn, "room-sid") is None
        assert lease.by_harness_session(conn, "room-sid", room=True).launch_id == room.launch_id
    assert store.launch_by_harness_session("room-sid") is None


def test_a_room_sharing_a_workers_session_id_does_not_hide_the_worker(store, clock):
    thread, _w = _a_store_with_history(store, clock)         # the desk is active as "probe-A"
    coord = store.launch_by_harness_session("probe-A")
    assert coord is not None and coord.role == ROLE_DESK
    room = _open_room(store, thread)
    with store._write() as (conn, now):                      # a duplicate the activation path refuses, written raw
        lease._launch_event(conn, now, room.launch_id, "active", "spawned", harness_session_id="probe-A",
                            argv_sha256=None)
        assert lease.by_harness_session(conn, "probe-A").launch_id == coord.launch_id
        assert lease.by_harness_session(conn, "probe-A", room=True).launch_id == room.launch_id
    assert store.launch_by_harness_session("probe-A").launch_id == coord.launch_id


def test_activate_room_refuses_a_session_id_another_launch_recorded(store, clock):
    thread, _w = _a_store_with_history(store, clock)
    room = _open_room(store, thread)
    with pytest.raises(lease.LeaseRefused, match="already recorded"):
        _activate_room(store, room.launch_id, sid="probe-A")
    assert store.launch(room.launch_id).state == "pending"
    assert _activate_room(store, room.launch_id, sid="room-own")[1] is None


def test_a_room_on_a_closed_or_done_thread_is_refused(store, clock):
    thread = _thread(store)
    for state in ("done", "closed"):
        with store._write() as (conn, now):
            conn.execute("INSERT INTO thread_events (thread_id, seq, field, value, cause, by, at) "
                         "SELECT ?, MAX(seq) + 1, 'state', ?, 'test', 'operator', ? FROM thread_events WHERE thread_id = ?",
                         (thread, state, now, thread))
        with pytest.raises(lease.LeaseRefused, match=f"is {state}"):
            _open_room(store, thread)


def test_the_watchers_lists_leave_a_room_out(store, clock):
    thread = _thread(store)
    room = _open_room(store, thread, harness="codex", cwd="/tmp/room-dir")
    assert _activate_room(store, room.launch_id)[1] is None
    with store._write() as (conn, now):
        assert "/tmp/room-dir" not in lease.launch_dirs(conn)
        assert lease.end_launch(conn, now, room.launch_id, "ended", "cockpit closed").state == "ended"
        assert lease.over_unswept(conn, "codex") == []


def test_a_room_that_failed_is_no_death(store, clock):
    thread = _thread(store)
    room = _open_room(store, thread)
    with store._write() as (conn, now):
        ended = lease.end_launch(conn, now, room.launch_id, "failed", "could not start")
        assert ended.state == "failed" and lease.launch_death(conn, ended) is False
        assert lease.death_run(conn) == []


def test_a_room_ends_through_end_launch_like_any_launch(store, clock):
    room = _open_room(store, _thread(store))
    assert _activate_room(store, room.launch_id)[1] is None
    with store._write() as (conn, now):
        assert lease.end_launch(conn, now, room.launch_id, "ended", "rotated").state == "ended"
        with pytest.raises(lease.LeaseRefused, match="cannot be"):
            lease.end_launch(conn, now, room.launch_id, "ended", "again")
        assert lease.end_unlisted(conn, now, room.launch_id) is None
