"""Linux: before every command the confined shell checks that the disk still matches the mounts it was
started with, and closes if not (spore-1312 rec C, ruled by Phill 2026-10-03, widened by its L3)."""

from __future__ import annotations

import os
import platform
import sqlite3
from pathlib import Path

import pytest

from levain.firing.confinement import (
    ConfinementError,
    _BwrapShell,
    _bwrap_argv,
    _mount_manifest,
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


def _shell(tmp_path, monkeypatch, *, deny_files=(), extra=()):
    """An unstarted _BwrapShell with the manifest the real plan would record (pure: no bwrap)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    policy = build_policy(_entity(tmp_path), deny_files=deny_files, extra_deny_read_write=extra)
    manifest = _mount_manifest(_bwrap_argv(policy), policy)
    return _BwrapShell(policy=policy, manifest=manifest, argv=["/bin/false"], cwd=tmp_path, env={})


def _empty(tmp_path, name="store.db") -> Path:
    p = tmp_path / "data" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"")
    return p


def test_a_jewel_initialised_as_a_database_in_place_refuses(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    _make_wal_db(store).close()
    with pytest.raises(ConfinementError, match="changed since this shell started|SQLite database"):
        sh.run("echo never")
    assert sh.closed


def test_run_passes_through_when_nothing_changed(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    with pytest.raises(ConfinementError, match="not running"):   # the base run, not the check
        sh.run("echo x")


def test_a_jewel_that_cannot_be_re_checked_refuses_and_closes(tmp_path, monkeypatch):
    """L1 MED-1 (run): an OSError while re-inspecting escaped run() raw, with the shell left alive."""
    import levain.firing.confinement as conf
    sh = _shell(tmp_path, monkeypatch)

    def boom(policy):
        raise PermissionError(13, "Permission denied", "x")

    monkeypatch.setattr(conf, "_refuse_plantable_sqlite_jewels", boom)
    with pytest.raises(ConfinementError, match="could not be re-checked"):
        sh.run("echo x")
    assert sh.closed


def test_a_denied_file_replaced_atomically_refuses(tmp_path, monkeypatch):
    """RUN on argushub at 509a40e (released floor): the live shell read the replacement."""
    cred = tmp_path / "creds" / "token"
    cred.parent.mkdir()
    cred.write_text("OLD")
    sh = _shell(tmp_path, monkeypatch, deny_files=(cred,))
    tmp = cred.with_name("token.new")
    tmp.write_text("NEW")
    os.replace(tmp, cred)
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo x")
    assert sh.closed


def test_a_sidecar_that_appears_after_spawn_refuses(tmp_path, monkeypatch):
    """L3 r1 codex (run): an unlinked database's live -wal was read by the next command."""
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    Path(f"{store}-wal").write_bytes(b"\x37\x7f\x06\x82")
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo x")


def test_a_jewel_replaced_by_a_symlink_refuses(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    other = _empty(tmp_path, "other.db")
    store.unlink()
    store.symlink_to(other)
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo x")
    assert sh.closed


def test_a_hidden_directory_swapped_for_a_link_refuses(tmp_path, monkeypatch):
    """L3 r2 codex: a tmpfs-covered directory renamed away and replaced by a symlink."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "token").write_text("x")
    sh = _shell(tmp_path, monkeypatch, extra=(secrets,))
    secrets.rename(tmp_path / "moved")
    secrets.symlink_to(tmp_path / "moved")
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo x")


def test_a_closed_shell_reports_closed_not_a_jewel_change(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    sh.close()
    _make_wal_db(store).close()
    with pytest.raises(ConfinementError, match="not running"):
        sh.run("echo x")


def test_the_manifest_is_settled_once(tmp_path, monkeypatch):
    """L3 r2 codex/complement: a second start() re-snapshotted, laundering a change made since."""
    absent = tmp_path / "creds" / "absent-token"   # a mountpoint bwrap would create at start
    absent.parent.mkdir()
    sh = _shell(tmp_path, monkeypatch, deny_files=(absent,))
    sh.settle_created_mountpoints()
    before = dict(sh._manifest)
    absent.write_text("planted after the start")
    sh.settle_created_mountpoints()                  # must not adopt it
    assert sh._manifest == before
    with pytest.raises(ConfinementError, match="changed since this shell started"):
        sh.run("echo x")


def test_paths_inside_a_tmpfs_root_are_not_tracked(tmp_path, monkeypatch):
    """The ssh vectors bound inside the agent-mode ~/.ssh tmpfs: host changes there are hidden by the
    tmpfs, so tracking them would close shells for nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    policy = build_policy(_entity(tmp_path))
    manifest = _mount_manifest(_bwrap_argv(policy), policy)
    ssh = str((tmp_path / ".ssh").resolve())
    assert ssh in manifest
    assert not [q for q in manifest if q.startswith(ssh + "/")]


@linux_live
def test_live_an_empty_jewel_initialised_as_a_wal_database_closes_the_shell(tmp_path, monkeypatch):
    """Reproduced at 3838801: the next command read the row out of the -wal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _empty(tmp_path)
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(store,)))
    try:
        assert sh.run("echo spawned", timeout=20).exit_code == 0
        con = _make_wal_db(store)
        try:
            with pytest.raises(ConfinementError):
                sh.run(f"grep -c SECRET-ROW-1312 '{store}-wal'", timeout=20)
            assert sh.closed
        finally:
            con.close()
    finally:
        sh.close()


@linux_live
def test_live_an_unlinked_database_with_a_live_wal_closes_the_shell(tmp_path, monkeypatch):
    """Reproduced at e6d3293: the next command read the -wal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _empty(tmp_path)
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(store,)))
    try:
        assert sh.run("true", timeout=20).exit_code == 0
        con = _make_wal_db(store)
        try:
            os.unlink(store)
            with pytest.raises(ConfinementError):
                sh.run(f"grep -c SECRET-ROW-1312 '{store}-wal'", timeout=20)
        finally:
            con.close()
    finally:
        sh.close()


@linux_live
def test_live_an_atomically_replaced_credential_closes_the_shell(tmp_path, monkeypatch):
    """Reproduced at 509a40e (released floor): the same live shell read the rotated secret."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cred = tmp_path / "creds" / "token"
    cred.parent.mkdir()
    cred.write_text("OLD-SECRET")
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(cred,)))
    try:
        assert sh.run(f"cat '{cred}'", timeout=20).exit_code != 0
        new = cred.with_name("token.new")
        new.write_text("NEW-ROTATED-SECRET")
        os.replace(new, cred)
        with pytest.raises(ConfinementError):
            sh.run(f"cat '{cred}'", timeout=20)
        assert sh.closed
    finally:
        sh.close()


@linux_live
def test_live_an_unchanged_session_keeps_running(tmp_path, monkeypatch):
    """CONTROL: the manifest must not close a shell when nothing on disk changed (created stubs and
    pinned directories included)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    absent = tmp_path / "creds" / "absent-token"     # bwrap creates its mountpoint at start
    absent.parent.mkdir()
    sh = select_provider().spawn_shell(build_policy(_entity(tmp_path), deny_files=(absent,)))
    try:
        for i in range(5):
            assert sh.run(f"echo {i}", timeout=20).output.strip() == str(i)
        assert not sh.closed
    finally:
        sh.close()
