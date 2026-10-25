"""The ``/1 -> /2`` store migration (14b DESIGN_REV3 §8, §10 S2 and S3b): the function, the ``graduation_steps``
audit table, and the flip: ``STORE_FORMAT`` is ``/2`` and the first open by this binary migrates a ``/1`` store.

A ``/1`` store is made by the ``/1`` binary itself: ``tests/fixtures/autonomic_db_v1_4c280672.py`` is
``levain/autonomic/db.py`` as it stood at 4c280672, the last commit before the flip (format ``/1``, the marker
checked inside every transaction, the migration built but not wired), loaded as its own module. Its objects are
"an older levain" with the store open."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

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


_OLDER = Path(__file__).resolve().parent / "fixtures" / "autonomic_db_v1_4c280672.py"
_spec = importlib.util.spec_from_file_location("autonomic_db_v1_4c280672", _OLDER)
older = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(older)


def v1_store(tmp_path):
    """A ``/1`` store holding one binding row, made by the ``/1`` binary, and that binary's open object on it."""
    db = older.AutonomicDB(tmp_path / "store")
    with db.write() as conn:
        conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b1', '{}', 1)")
    return db


def test_the_format_is_flipped():
    assert STORE_FORMAT == STORE_FORMAT_V2 == "levain-autonomic/2"
    assert STORE_FORMAT_V1 == older.STORE_FORMAT == "levain-autonomic/1"


def test_an_older_binarys_new_store_is_v1_with_no_graduation_steps_table(tmp_path):
    db = v1_store(tmp_path)
    assert "graduation_steps" not in tables(db.path)
    assert fmt(db.path) == STORE_FORMAT_V1


def test_this_binarys_new_store_is_v2_with_its_audit_table(tmp_path):
    db = AutonomicDB(tmp_path / "store")
    assert db.meta("format") == STORE_FORMAT_V2
    assert "graduation_steps" in tables(db.path)


def test_the_first_open_migrates_a_v1_store_once_and_keeps_what_it_held(tmp_path, monkeypatch):
    v1 = v1_store(tmp_path)
    store_id = v1.meta("store_id")
    calls = []
    real = dbmod.migrate_to_v2
    monkeypatch.setattr(dbmod, "migrate_to_v2", lambda conn: calls.append(real(conn)) or calls[-1])
    db = AutonomicDB(tmp_path / "store")
    assert db.meta("format") == STORE_FORMAT_V2 and calls == [True]
    assert db.meta("store_id") == store_id                     # the same store: its identity is kept
    with db.read() as conn:
        assert conn.execute("SELECT binding_id FROM bindings").fetchall() == [("b1",)]
        assert [r[1] for r in conn.execute("PRAGMA table_info(graduation_steps)")] == STEP_COLUMNS
    AutonomicDB(tmp_path / "store").meta("format")              # another object: nothing left to migrate
    assert calls == [True, False]


def test_a_live_older_object_refuses_the_store_once_this_binary_has_opened_it(tmp_path):
    # S2 ruling, carried to S3b: an older levain still serving must not keep firing on the /2 store
    v1 = v1_store(tmp_path)
    assert v1.meta("format") == STORE_FORMAT_V1                 # the older object is ready at /1
    AutonomicDB(tmp_path / "store").meta("format")              # this binary opens it: migrated
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        with v1.write() as conn:
            conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b2', '{}', 2)")
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        v1.meta("store_id")
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        older.AutonomicDB(tmp_path / "store").meta("format")   # and an older binary opening it afresh


def test_a_store_of_another_format_is_refused_at_open_and_left_as_it_was(tmp_path):
    v1 = v1_store(tmp_path)
    with v1.write() as conn:
        conn.execute("UPDATE meta SET value = 'other/9' WHERE key = 'format'")
    with pytest.raises(StoreFormatError, match="other/9"):
        AutonomicDB(tmp_path / "store").meta("format")
    assert fmt(v1.path) == "other/9" and "graduation_steps" not in tables(v1.path)


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


class OtherWording:
    """A connection whose missing-table errors read as another SQLite build may word them."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        try:
            return self._conn.execute(sql, *args)
        except sqlite3.OperationalError as exc:
            if str(exc).startswith("no such table: "):
                raise sqlite3.OperationalError(str(exc).replace("no such table: ", "no such table: main.")) from None
            raise


def test_a_missing_meta_table_is_found_by_the_schema_not_the_error_text(tmp_path):
    # deepseek LOW 5: the marker read asks sqlite_master whether meta exists, so the wording of the error is moot
    path = tmp_path / DB_NAME
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("CREATE TABLE other (x)")
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreFormatError, match="no store format marker"):
            migrate_to_v2(OtherWording(conn))
        with pytest.raises(StoreFormatError, match="no format marker"):
            dbmod._check_format(OtherWording(conn))
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_a_ready_object_refuses_the_store_once_another_connection_migrates_it(tmp_path):
    # codex HIGH r1: the marker is checked inside every read and write, not only when the object first opens
    db = v1_store(tmp_path)
    assert db.meta("format") == STORE_FORMAT_V1          # ready at /1
    in_immediate(db.path, migrate_to_v2)                 # another connection (another process) migrates
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        with db.read() as conn:
            conn.execute("SELECT 1").fetchone()
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        with db.write() as conn:
            conn.execute("INSERT INTO bindings (binding_id, record, seq) VALUES ('b2', '{}', 2)")
    with pytest.raises(older.StoreFormatError):
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
    with pytest.raises(older.StoreFormatError):
        db.meta("format")


def test_a_ready_object_refuses_after_its_own_migration_commits(tmp_path):
    db = v1_store(tmp_path)
    with db.write() as conn:
        migrate_to_v2(conn)
    with pytest.raises(older.StoreFormatError, match="levain-autonomic/2"):
        db.meta("format")


def test_a_ready_object_refuses_a_store_whose_marker_was_removed(tmp_path):
    db = v1_store(tmp_path)
    in_immediate(db.path, lambda c: c.execute("DELETE FROM meta WHERE key = 'format'"))
    with pytest.raises(older.StoreFormatError):
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


def test_a_migration_that_fails_at_open_leaves_the_store_v1(tmp_path, monkeypatch):
    v1 = v1_store(tmp_path)

    def broken(conn):
        conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (STORE_FORMAT_V2,))
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(dbmod, "migrate_to_v2", broken)
    with pytest.raises(sqlite3.OperationalError):
        AutonomicDB(tmp_path / "store").meta("format")
    assert fmt(v1.path) == STORE_FORMAT_V1 and v1.meta("format") == STORE_FORMAT_V1
