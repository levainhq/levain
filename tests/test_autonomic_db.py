"""The autonomic store's database: it refuses what is not its own, never starts empty under a running
object, and never masks the error that rolled a transaction back."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from levain.autonomic.db import DB_NAME, AutonomicDB, StoreFormatError


def test_a_foreign_sqlite_database_is_refused_not_taken_over(tmp_path):
    # L1 on 4ae4a00: a database with tables and no marker was given the schema and a marker
    d = tmp_path / "store"
    d.mkdir()
    conn = sqlite3.connect(d / DB_NAME)
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreFormatError, match="not an autonomic store"):
        AutonomicDB(d).meta("format")
    conn = sqlite3.connect(d / DB_NAME)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert names == {"notes"}


def test_a_store_of_another_format_is_refused(tmp_path):
    db = AutonomicDB(tmp_path / "store")
    with db.write() as conn:
        conn.execute("UPDATE meta SET value = 'other/9' WHERE key = 'format'")
    with pytest.raises(StoreFormatError, match="other/9"):
        AutonomicDB(tmp_path / "store").meta("format")


def test_a_store_deleted_under_a_running_object_is_not_recreated_empty(tmp_path):
    db = AutonomicDB(tmp_path / "store")
    with db.write() as conn:
        conn.execute("INSERT INTO fences (binding_id, generation) VALUES ('b', 3)")
    for name in os.listdir(tmp_path / "store"):
        os.unlink(tmp_path / "store" / name)
    with pytest.raises(StoreFormatError, match="disappeared"):
        db.meta("format")
    assert not (tmp_path / "store" / DB_NAME).exists()


def test_a_new_directory_is_private_and_an_existing_one_keeps_its_mode(tmp_path):
    AutonomicDB(tmp_path / "new").meta("format")
    assert stat.S_IMODE(os.stat(tmp_path / "new").st_mode) == 0o700
    (tmp_path / "mine").mkdir(mode=0o750)
    os.chmod(tmp_path / "mine", 0o750)
    AutonomicDB(tmp_path / "mine").meta("format")
    assert stat.S_IMODE(os.stat(tmp_path / "mine").st_mode) == 0o750


def test_a_failed_rollback_does_not_mask_the_error_that_caused_it(tmp_path):
    db = AutonomicDB(tmp_path / "store")
    with pytest.raises(KeyError, match="the real cause"):
        with db.write() as conn:
            conn.execute("COMMIT")          # the transaction is gone: ROLLBACK will itself fail
            raise KeyError("the real cause")
