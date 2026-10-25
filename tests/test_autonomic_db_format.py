"""The ``/1 -> /2`` store migration (14b DESIGN_REV3 §8, §10 S2): the function and the ``graduation_steps`` audit
table, built behind the format constant. ``STORE_FORMAT`` stays ``/1`` until S3b flips it and wires the migration
into ``_ensure_schema``, so no release carries ``/2`` before the whole clamp exists (the one-release-unit rule)."""
from __future__ import annotations

import sqlite3

import pytest

from levain.autonomic import db as dbmod
from levain.autonomic.db import (
    DB_NAME, STORE_FORMAT, STORE_FORMAT_V1, STORE_FORMAT_V2, AutonomicDB, StoreFormatError, migrate_to_v2,
)

STEP_COLUMNS = ["step", "at", "old_id", "new_id", "family", "from_posture", "to_posture", "clean_count",
                "fire_count", "policy_id", "incarnation", "authority_head"]


def tables(path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def fmt(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0]
    finally:
        conn.close()


def v1_store(tmp_path) -> AutonomicDB:
    db = AutonomicDB(tmp_path / "store")
    with db.write() as conn:
        conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b1', '{}', 1)")
    return db


def test_the_format_is_not_flipped_here():
    assert STORE_FORMAT == STORE_FORMAT_V1 == "levain-autonomic/1"
    assert STORE_FORMAT_V2 == "levain-autonomic/2"


def test_a_new_store_has_no_graduation_steps_table_until_migrated(tmp_path):
    db = v1_store(tmp_path)
    assert "graduation_steps" not in tables(db.path)
    assert fmt(db.path) == STORE_FORMAT_V1


def test_the_migration_marks_the_store_v2_and_adds_the_audit_table(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        assert migrate_to_v2(conn) is True
    assert fmt(db.path) == STORE_FORMAT_V2
    conn = sqlite3.connect(db.path)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(graduation_steps)")]
        assert cols == STEP_COLUMNS
        # what the store held before is kept, and its identity with it
        assert conn.execute("SELECT binding_id FROM bindings").fetchall() == [("b1",)]
        assert conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0].startswith("store-")
    finally:
        conn.close()


def test_the_audit_table_takes_a_step_row_and_refuses_a_partial_one(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    conn = sqlite3.connect(db.path)
    try:
        conn.execute(
            "INSERT INTO graduation_steps (at, old_id, new_id, family, from_posture, to_posture, clean_count, "
            "fire_count, policy_id, incarnation, authority_head) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-10-25T12:00:00.000000Z", "bind-a", "bind-b", None, "CONFIRM", "COOLING_OFF", 20, 20,
             "gp-0123456789ab", "3:abc", "7:" + "h" * 64))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO graduation_steps (at, old_id) VALUES ('x', 'y')")
        assert conn.execute("SELECT step, new_id FROM graduation_steps").fetchall() == [(1, "bind-b")]
    finally:
        conn.close()


def test_the_migration_is_idempotent_on_a_v2_store(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    conn = sqlite3.connect(db.path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert migrate_to_v2(conn) is False
        conn.execute("COMMIT")
    finally:
        conn.close()
    assert fmt(db.path) == STORE_FORMAT_V2


def in_immediate(path, fn):
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = fn(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return out
    finally:
        conn.close()


def test_a_v2_store_missing_its_audit_table_is_repaired_and_says_so(tmp_path):
    # the boolean is "this call changed the store": the marker, or the audit table on a /2 store
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    in_immediate(db.path, lambda c: c.execute("DROP TABLE graduation_steps"))
    assert in_immediate(db.path, migrate_to_v2) is True
    assert "graduation_steps" in tables(db.path) and fmt(db.path) == STORE_FORMAT_V2
    assert in_immediate(db.path, migrate_to_v2) is False


def test_an_operational_error_other_than_a_missing_meta_table_raises_as_itself(tmp_path):
    # a meta table with no value column is not "no marker": the read's own error propagates
    path = tmp_path / DB_NAME
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            migrate_to_v2(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_a_database_with_no_meta_table_is_refused_as_unmarked(tmp_path):
    path = tmp_path / DB_NAME
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE other (x)")
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreFormatError, match="no store format marker"):
            migrate_to_v2(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_a_ready_object_refuses_the_store_once_another_connection_migrates_it(tmp_path):
    # codex HIGH r1: the marker is checked inside every read and write, not only when the object first opens
    db = v1_store(tmp_path)
    assert db.meta("format") == STORE_FORMAT_V1          # ready at /1
    in_immediate(db.path, migrate_to_v2)                 # another connection (another process) migrates
    with pytest.raises(StoreFormatError, match="levain-autonomic/2"):
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
    with pytest.raises(StoreFormatError, match="levain-autonomic/2"):
        with db.write() as conn:
            conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b2', '{}', 2)")
    with pytest.raises(StoreFormatError):
        db.meta("store_id")
    # the refused write wrote nothing
    conn = sqlite3.connect(db.path)
    try:
        assert conn.execute("SELECT binding_id FROM bindings").fetchall() == [("b1",)]
    finally:
        conn.close()


def test_a_read_reads_the_state_its_format_check_read(tmp_path):
    # a migration committed after a read's check is not seen by that read: its snapshot began at the check
    db = v1_store(tmp_path)
    with db.read() as conn:
        assert in_immediate(db.path, migrate_to_v2) is True
        assert conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == STORE_FORMAT_V1
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'graduation_steps'").fetchone() is None
    assert fmt(db.path) == STORE_FORMAT_V2
    with pytest.raises(StoreFormatError):
        db.meta("format")


def test_a_ready_object_refuses_after_its_own_migration_commits(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    with pytest.raises(StoreFormatError, match="levain-autonomic/2"):
        db.meta("format")


def test_a_ready_object_refuses_a_store_whose_marker_was_removed(tmp_path):
    db = v1_store(tmp_path)
    in_immediate(db.path, lambda c: c.execute("DELETE FROM meta WHERE key = 'format'"))
    with pytest.raises(StoreFormatError):
        db.meta("store_id")


def test_another_format_is_refused_and_left_as_it_was(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        conn.execute("UPDATE meta SET value = 'other/9' WHERE key = 'format'")
    conn = sqlite3.connect(db.path, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreFormatError, match="other/9"):
            migrate_to_v2(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()
    assert fmt(db.path) == "other/9" and "graduation_steps" not in tables(db.path)


def test_a_database_with_no_marker_is_refused(tmp_path):
    path = tmp_path / DB_NAME
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreFormatError, match="no store format marker"):
            migrate_to_v2(conn)
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_the_migration_runs_only_inside_its_callers_transaction(tmp_path):
    db = v1_store(tmp_path)
    conn = sqlite3.connect(db.path, isolation_level=None)
    try:
        with pytest.raises(RuntimeError, match="transaction"):
            migrate_to_v2(conn)
    finally:
        conn.close()
    assert fmt(db.path) == STORE_FORMAT_V1


def test_a_migration_rolled_back_by_its_caller_leaves_v1(tmp_path):
    db = v1_store(tmp_path)
    with pytest.raises(ZeroDivisionError):
        with db.write() as conn:
            migrate_to_v2(conn)
            1 / 0
    assert fmt(db.path) == STORE_FORMAT_V1 and "graduation_steps" not in tables(db.path)


def test_this_binary_refuses_a_v2_store_until_the_flip(tmp_path):
    # the one-release-unit rule, seen from this side: a /2 store under a /1 constant is refused, never read
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    with pytest.raises(StoreFormatError, match="levain-autonomic/2"):
        AutonomicDB(tmp_path / "store").meta("format")


def test_ensure_schema_does_not_call_the_migration(tmp_path, monkeypatch):
    # S3b wires it; S2 only builds it
    called = []
    monkeypatch.setattr(dbmod, "migrate_to_v2", lambda conn: called.append(conn))
    AutonomicDB(tmp_path / "store").meta("format")
    assert called == []
