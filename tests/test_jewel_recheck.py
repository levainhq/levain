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
    """L1 MED-1 (run): an OSError while re-inspecting escaped run() raw, with the shell left alive.
    Raised deterministically here: a chmod-based setup depends on the Python version and on not
    running as root (L3 r1: codex, glm, complement)."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    sh = _BwrapShell(policy=build_policy(_entity(tmp_path)), argv=["/bin/false"], cwd=tmp_path, env={})

    def boom(policy):
        raise PermissionError(13, "Permission denied", "x")

    monkeypatch.setattr(conf, "_refuse_plantable_sqlite_jewels", boom)
    with pytest.raises(ConfinementError, match="could not be re-checked"):
        sh.run("echo x")
    assert sh.closed


def _empty_jewel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "data" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    policy = build_policy(_entity(tmp_path), deny_files=(store,))
    sh = _BwrapShell(policy=policy, argv=["/bin/false"], cwd=tmp_path, env={})
    sh._jewel_types = {p: "file" if p == store else "absent" for p in policy.deny_files}  # as start() would
    return store, sh


def test_a_sidecar_that_appears_after_spawn_refuses(tmp_path, monkeypatch):
    """L3 r1 codex (run): the host unlinked the database while its connection kept the -wal, the main
    file read "absent", the -wal had no magic, and the next command read the -wal."""
    store, sh = _empty_jewel(tmp_path, monkeypatch)
    Path(f"{store}-wal").write_bytes(b"\x37\x7f\x06\x82")
    with pytest.raises(ConfinementError, match="appeared after the shell started"):
        sh.run("echo x")
    assert sh.closed


def test_a_jewel_replaced_by_a_symlink_refuses(tmp_path, monkeypatch):
    store, sh = _empty_jewel(tmp_path, monkeypatch)
    other = tmp_path / "other.db"
    other.write_bytes(b"")
    store.unlink()
    store.symlink_to(other)
    with pytest.raises(ConfinementError, match="changed type"):
        sh.run("echo x")


def test_a_closed_shell_reports_closed_not_a_jewel_change(tmp_path, monkeypatch):
    store, sh = _empty_jewel(tmp_path, monkeypatch)
    sh.close()
    _make_wal_db(store).close()
    with pytest.raises(ConfinementError, match="not running"):
        sh.run("echo x")


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


@linux_live
def test_live_an_unlinked_database_with_a_live_wal_closes_the_shell(tmp_path, monkeypatch):
    """The reproduced L3 r1 failure (argushub, e6d3293): the next command read the -wal."""
    import os
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "data" / "store.db"
    store.parent.mkdir()
    store.write_bytes(b"")
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(store,)))
    try:
        assert sh.run("true", timeout=20).exit_code == 0
        con = _make_wal_db(store)
        try:
            os.unlink(store)
            assert Path(f"{store}-wal").exists()
            with pytest.raises(ConfinementError):
                sh.run(f"grep -c SECRET-ROW-1312 '{store}-wal'", timeout=20)
            assert sh.closed
        finally:
            con.close()
    finally:
        sh.close()
