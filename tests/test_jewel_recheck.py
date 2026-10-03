"""Linux: a crown jewel that becomes a SQLite database after the shell starts closes the shell at the
next command (spore-1312, rec C, ruled by Phill 2026-10-03)."""

from __future__ import annotations

import platform
import sqlite3
from pathlib import Path

import pytest

from levain.firing.confinement import (
    ConfinementError,
    _BwrapShell,
    build_policy,
    bwrap_available,
    select_provider,
)

linux_live = pytest.mark.skipif(
    not (platform.system() == "Linux" and bwrap_available()),
    reason="needs a Linux host where bwrap can actually establish a namespace",
)


def _entity(root: Path) -> Path:
    d = root / "ent"
    (d / ".levain").mkdir(parents=True)
    (d / "workspace").mkdir()
    return d


def _make_wal_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES ('SECRET-ROW-1312')")
    con.commit()
    return con


def test_run_refuses_before_touching_the_shell_when_a_jewel_is_a_database(tmp_path, monkeypatch):
    """Pure: the check runs first, so no shell process is needed to see the refusal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "data" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    policy = build_policy(_entity(tmp_path), deny_files=(store,))
    sh = _BwrapShell(policy=policy, argv=["/bin/false"], cwd=tmp_path, env={})
    _make_wal_db(store).close()
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo never")
    assert sh.closed


def test_run_passes_through_when_no_jewel_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "data" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    sh = _BwrapShell(policy=build_policy(_entity(tmp_path), deny_files=(store,)),
                     argv=["/bin/false"], cwd=tmp_path, env={})
    with pytest.raises(ConfinementError, match="not running"):   # the base run, not the check
        sh.run("echo x")


def test_a_jewel_that_cannot_be_re_checked_refuses_and_closes(tmp_path, monkeypatch):
    """L1 MED-1 (run): an OSError while re-inspecting escaped run() raw, with the shell left alive."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "locked" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    sh = _BwrapShell(policy=build_policy(_entity(tmp_path), extra_deny_read_write=(store,)),
                     argv=["/bin/false"], cwd=tmp_path, env={})
    store.parent.chmod(0o000)
    try:
        with pytest.raises(ConfinementError, match="could not be re-checked"):
            sh.run("echo x")
        assert sh.closed
    finally:
        store.parent.chmod(0o755)


@linux_live
def test_live_an_empty_jewel_initialised_as_a_wal_database_closes_the_shell(tmp_path, monkeypatch):
    """The reproduced failure (argushub, 3838801): the next command read the row out of the -wal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "data" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(store,)))
    try:
        assert sh.run("echo spawned", timeout=20).exit_code == 0
        con = _make_wal_db(store)
        try:
            assert Path(f"{store}-wal").exists()
            with pytest.raises(ConfinementError, match="changed since this shell started"):
                sh.run(f"grep -c SECRET-ROW-1312 '{store}-wal'", timeout=20)
            assert sh.closed
        finally:
            con.close()
    finally:
        sh.close()
