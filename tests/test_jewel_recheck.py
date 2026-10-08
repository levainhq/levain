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
    _identity,
    _mount_plan_paths,
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
    mounted, unmounted = _mount_plan_paths(_bwrap_argv(policy), policy)
    manifest = {q: _identity(Path(q)) for q in [*mounted, *unmounted]}
    return _BwrapShell(policy=policy, manifest=manifest, argv=["/bin/false"], cwd=tmp_path, env={})


_CHANGED = "the floor no longer covers what is on disk"


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
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")
    assert sh.closed


def test_a_sidecar_that_appears_after_spawn_refuses(tmp_path, monkeypatch):
    """L3 r1 codex (run): an unlinked database's live -wal was read by the next command."""
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    Path(f"{store}-wal").write_bytes(b"\x37\x7f\x06\x82")
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")
    assert sh.closed


def test_a_jewel_replaced_by_a_symlink_refuses(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    other = _empty(tmp_path, "other.db")
    store.unlink()
    store.symlink_to(other)
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")
    assert sh.closed


def test_a_hidden_directory_swapped_for_a_link_refuses(tmp_path, monkeypatch):
    """L3 r2 codex: a tmpfs-covered directory renamed away and replaced by a symlink. Below a
    subdirectory of $HOME: directly in $HOME the host rename is not even visible to bash (the
    step (0) view), so there is nothing to recheck."""
    (tmp_path / "sub").mkdir()
    secrets = tmp_path / "sub" / "secrets"
    secrets.mkdir()
    (secrets / "token").write_text("x")
    sh = _shell(tmp_path, monkeypatch, extra=(secrets,))
    secrets.rename(tmp_path / "sub" / "moved")
    secrets.symlink_to(tmp_path / "sub" / "moved")
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")


def test_a_closed_shell_reports_closed_not_a_jewel_change(tmp_path, monkeypatch):
    store = _empty(tmp_path)
    sh = _shell(tmp_path, monkeypatch, deny_files=(store,))
    sh.close()
    _make_wal_db(store).close()
    with pytest.raises(ConfinementError, match="not running"):
        sh.run("echo x")


def test_nothing_is_adopted_after_the_start(tmp_path, monkeypatch):
    """L3 r3 codex: a post-start settle could adopt a replacement as the baseline. The manifest is
    recorded once, before the start; a path recorded absent that appears later is a change."""
    from levain.firing.confinement import _prepare_mountpoints
    absent = tmp_path / "creds" / "absent-token"
    _prepare_mountpoints({str(absent): "file"})      # what the provider does before bwrap
    sh = _shell(tmp_path, monkeypatch, deny_files=(absent,))
    new = absent.with_name("absent-token.new")
    new.write_text("rotated in after the start")
    os.replace(new, absent)
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")


def test_a_jewel_the_plan_leaves_unmounted_is_watched(tmp_path, monkeypatch):
    """L3 r3 codex/complement: an absent jewel under the read-only .levain store gets no mount (bwrap
    cannot create one there); if the host creates it later the read-only bind shows it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    entity = _entity(tmp_path)
    vault = entity / ".levain" / "vault" / "token"
    policy = build_policy(entity, deny_files=(vault,))
    mounted, unmounted = _mount_plan_paths(_bwrap_argv(policy), policy)
    assert str(vault.resolve()) not in mounted and str(vault.resolve()) in unmounted
    sh = _BwrapShell(policy=policy, argv=["/bin/false"], cwd=tmp_path, env={},
                     manifest={q: _identity(Path(q)) for q in [*mounted, *unmounted]})
    vault.parent.mkdir()
    vault.write_text("SECRET")
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")


def test_tmpfs_containment_is_exact_not_case_folded():
    """L3 r3: a case-folded match dropped a distinct path on a case-sensitive filesystem."""
    from levain.firing.confinement import build_policy as _bp  # noqa: F401 (import check only)

    class _P:
        deny_files = deny_write_files = own_memory_files = sqlite_sidecars = deny_read_write = ()
        config_file = None

    argv = ["--tmpfs", "/x/Secrets", "--ro-bind", "/dev/null", "/x/secrets/token",
            "--ro-bind", "/dev/null", "/x/Secrets/inner"]
    mounted, _ = _mount_plan_paths(argv, _P())
    assert "/x/secrets/token" in mounted and "/x/Secrets/inner" not in mounted


def test_a_sidecar_inside_a_tmpfs_root_is_not_watched():
    class _P:
        deny_files = deny_write_files = own_memory_files = deny_read_write = ()
        sqlite_sidecars = (Path("/x/Secrets/store.db-wal"), Path("/y/store.db-wal"))
        config_file = None

    _, unmounted = _mount_plan_paths(["--tmpfs", "/x/Secrets"], _P())
    assert unmounted == ["/y/store.db-wal"]


def test_an_error_recording_the_manifest_refuses_before_any_start(tmp_path, monkeypatch):
    """L3 r3 codex/glm: a raw OSError from the manifest escaped spawn."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)

    def boom(p):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(conf, "_identity", boom)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)   # fail in _identity only
    started = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: started.append(1) or self)
    with pytest.raises(ConfinementError, match="could not (prepare or record|inspect)"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)))
    assert not started


def test_paths_inside_a_tmpfs_root_are_not_tracked(tmp_path, monkeypatch):
    """The ssh vectors bound inside the agent-mode ~/.ssh tmpfs: host changes there are hidden by the
    tmpfs, so tracking them would close shells for nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").mkdir()
    policy = build_policy(_entity(tmp_path))
    mounted, unmounted = _mount_plan_paths(_bwrap_argv(policy), policy)
    ssh = str((tmp_path / ".ssh").resolve())
    # ~/.ssh itself is inside step (0)'s $HOME view now: hidden from the host like what is in it.
    assert ssh not in mounted
    assert not [q for q in [*mounted, *unmounted] if q.startswith(ssh + "/")]


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


def test_a_directory_created_at_an_unmounted_root_is_watched(tmp_path, monkeypatch):
    """codex L3 r4: an absent root under the read-only store, created as a directory between the plan
    and the manifest, was neither mounted nor watched (is_dir() dropped it)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    entity = _entity(tmp_path)
    vault = entity / ".levain" / "vaultdir"
    policy = build_policy(entity, extra_deny_read_write=(vault,))
    mounted, unmounted = _mount_plan_paths(_bwrap_argv(policy), policy)
    assert str(vault.resolve()) in unmounted
    sh = _BwrapShell(policy=policy, argv=["/bin/false"], cwd=tmp_path, env={},
                     manifest={q: _identity(Path(q)) for q in [*mounted, *unmounted]})
    vault.mkdir()
    with pytest.raises(ConfinementError, match=_CHANGED):
        sh.run("echo x")


def test_an_unreachable_path_costs_nothing_until_it_changes(tmp_path, monkeypatch):
    """codex L3 r4: a socket in another user's runtime dir made lstat raise EACCES and refused every
    spawn. It is recorded as unreachable and only a change closes the shell."""
    if os.geteuid() == 0:
        pytest.skip("root can stat anything")
    from levain.firing.confinement import _UNREACHABLE
    locked = tmp_path / "otheruser"
    locked.mkdir()
    sock = locked / "docker.sock"
    locked.chmod(0o000)
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)   # the locked dir is now "another user's"
    try:
        assert _identity(sock) == _UNREACHABLE
        monkeypatch.setenv("HOME", str(tmp_path))
        policy = build_policy(_entity(tmp_path))
        sh = _BwrapShell(policy=policy, manifest={str(sock): _UNREACHABLE},
                         argv=["/bin/false"], cwd=tmp_path, env={})
        with pytest.raises(ConfinementError, match="not running"):   # unchanged: base run
            sh.run("echo x")
        sh2 = _BwrapShell(policy=policy, manifest={str(sock): _UNREACHABLE},
                          argv=["/bin/false"], cwd=tmp_path, env={})
        locked.chmod(0o755)
        with pytest.raises(ConfinementError, match=_CHANGED):
            sh2.run("echo x")
    finally:
        locked.chmod(0o755)


def test_only_tmpfs_and_dev_null_targets_are_created():
    """codex/complement L3 r4: a self-bind or a --*-try bind target must never be created."""
    class _P:
        deny_files = deny_write_files = own_memory_files = sqlite_sidecars = deny_read_write = ()
        config_file = None

    argv = ["--tmpfs", "/a/t", "--ro-bind", "/dev/null", "/a/f", "--ro-bind", "/a/s", "/a/s",
            "--bind-try", "/a/c", "/a/c", "--ro-bind-try", "/dev/null", "/a/g", "--bind", "/a/p", "/a/p"]
    mounted, _ = _mount_plan_paths(argv, _P())
    assert mounted == {"/a/t": "dir", "/a/f": "file", "/a/s": None, "/a/c": None,
                       "/a/g": "file", "/a/p": None}


def test_a_stub_under_an_absent_directory_is_created_with_its_parents(tmp_path):
    """complement L3 r4: bwrap created missing parents; without them bash was refused."""
    from levain.firing.confinement import _prepare_mountpoints
    f = tmp_path / "config" / "gh" / "hosts.yml"
    d = tmp_path / "x" / "y" / "store"
    _prepare_mountpoints({str(f): "file", str(d): "dir", str(tmp_path / "skip"): None})
    assert f.is_file() and f.stat().st_size == 0 and d.is_dir()
    assert not (tmp_path / "skip").exists()
    for level in (tmp_path / "config", tmp_path / "config" / "gh", tmp_path / "x", tmp_path / "x" / "y"):
        assert level.stat().st_mode & 0o777 == 0o700, level   # every created level, not just the leaf


def test_a_jewel_that_changes_while_the_floor_is_planned_refuses_the_spawn(tmp_path, monkeypatch):
    """codex L3 r4: an absent root created as a directory between the plan and the manifest was
    recorded in its new state, unmounted, and never seen as a change; the shell could read it."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    entity = _entity(tmp_path)
    vault = entity / ".levain" / "vaultdir"
    real_plan = conf._bwrap_plan

    def plan_then_race(policy):
        out = real_plan(policy)
        vault.mkdir()                      # the host creates it right after the plan read the disk
        return out

    monkeypatch.setattr(conf, "_bwrap_plan", plan_then_race)
    started = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: started.append(1) or self)
    with pytest.raises(ConfinementError, match="changed while the floor was being planned"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(entity, extra_deny_read_write=(vault,)))
    assert not started


def test_an_own_uid_blocking_directory_is_not_unreachable(tmp_path):
    """complement L3 r5: a directory this user owns can be chmod'ed open by a same-uid shell, so a
    path behind it must not be waved through as unreachable."""
    if os.geteuid() == 0:
        pytest.skip("root can stat anything")
    locked = tmp_path / "mine"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            _identity(locked / "token")
    finally:
        locked.chmod(0o755)


def test_the_shell_env_never_carries_bash_env(tmp_path, monkeypatch):
    """codex L3 r5: BASH_ENV/ENV make bash source a file at startup, before the first check."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    seen = {}

    def fake_start(self):
        seen.update(self._env)
        return self

    monkeypatch.setattr(conf._BwrapShell, "start", fake_start)
    monkeypatch.setattr(conf._BwrapShell, "_recheck", lambda self: None)
    conf.BwrapProvider()._spawn_shell_impl(
        build_policy(_entity(tmp_path)), env={"PATH": "/usr/bin", "BASH_ENV": "/x", "ENV": "/y"})
    assert "BASH_ENV" not in seen and "ENV" not in seen and seen.get("PATH") == "/usr/bin"


def test_a_change_during_the_start_closes_the_shell_before_any_command(tmp_path, monkeypatch):
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    entity = _entity(tmp_path)
    late = tmp_path / "creds" / "late-token"
    late.parent.mkdir()
    closed = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: late.write_text("x") or self)
    monkeypatch.setattr(conf._BwrapShell, "close", lambda self: closed.append(1))
    with pytest.raises(ConfinementError, match=_CHANGED):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(entity, deny_files=(late,)))
    assert closed


def test_the_shell_never_runs_startup_code_from_its_env(tmp_path, monkeypatch):
    """codex + complement L3 r6: imported functions, SHELLOPTS/PS4 and the like run code during the
    handshake, before the post-start recheck. They are stripped, and bash starts privileged (-p)."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    seen = {}

    def fake_start(self):
        seen["env"] = dict(self._env)
        seen["argv"] = list(self._argv)
        return self

    monkeypatch.setattr(conf._BwrapShell, "start", fake_start)
    monkeypatch.setattr(conf._BwrapShell, "_recheck", lambda self: None)
    hostile = {
        "BASH_FUNC_printf%%": "() { cat /etc/passwd; }",
        "SHELLOPTS": "xtrace", "BASHOPTS": "extdebug", "PS4": "$(cat /etc/passwd)",
        "PROMPT_COMMAND": "id",
    }
    conf.BwrapProvider()._spawn_shell_impl(
        build_policy(_entity(tmp_path)), env={"PATH": "/usr/bin", **hostile})
    assert not set(hostile) & set(seen["env"]) and seen["env"]["PATH"] == "/usr/bin"
    assert seen["argv"][-5:] == ["--as-pid-1", "/bin/bash", "--noprofile", "--norc", "-p"]


def test_a_filesystem_error_in_the_post_start_recheck_is_a_refusal(tmp_path, monkeypatch):
    """codex + complement L3 r6: a raw OSError escaped past every caller's ConfinementError handler."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    closed = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: self)
    monkeypatch.setattr(conf._BwrapShell, "close", lambda self: closed.append(1))

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(conf._BwrapShell, "_recheck", denied)
    with pytest.raises(ConfinementError, match="could not re-check"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)))
    assert closed


@linux_live
def test_live_a_hostile_env_runs_nothing_and_the_shell_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
        "BASH_FUNC_printf%%": "() { echo HOSTILE-FN; builtin printf \"$@\"; }",
        "SHELLOPTS": "xtrace", "PS4": "$(echo HOSTILE-PS4)",
    }
    with select_provider().spawn_shell(build_policy(_entity(tmp_path)), env=env) as sh:
        r = sh.run("printf 'ok\\n'", timeout=20)
        assert "ok" in r.output
        assert "HOSTILE" not in r.output


def test_a_ledger_that_cannot_be_written_refuses_before_any_start(tmp_path, monkeypatch):
    """codex (L3 r2 frozen tip): the claim commit failing (ENOSPC, say) was ignored and the shell
    started, while the ledger on disk did not carry its claim; another session's close could then
    remove a placeholder this shell's mask stands on."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    real_replace = os.replace

    def full_disk(src, dst):
        if str(dst).endswith(conf._LEDGER_NAME):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(conf.os, "replace", full_disk)
    started = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: started.append(1) or self)
    with pytest.raises(ConfinementError, match="cannot be written"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)))
    assert not started


def test_a_failed_ledger_commit_removes_what_the_spawn_made(tmp_path, monkeypatch):
    """complement + codex r3: the refusal left the 0444 placeholders it had made, in no ledger."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    real_replace = os.replace

    def full_disk(src, dst):
        if str(dst).endswith(conf._LEDGER_NAME):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(conf.os, "replace", full_disk)
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: self)
    with pytest.raises(ConfinementError, match="cannot be written"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path), deny_standard_creds=True))
    assert not (tmp_path / ".netrc").exists()


def test_an_unreadable_ledger_refuses_before_anything_is_made(tmp_path, monkeypatch):
    """codex r3: with the ledger unreadable the spawn went ahead with no claim on disk."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    d = conf._ledger_dir()
    d.mkdir(parents=True, mode=0o700)
    d.parent.chmod(0o700)
    (d / conf._LEDGER_NAME).write_text("{not json")
    started = []
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: started.append(1) or self)
    with pytest.raises(ConfinementError, match="cannot be read"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path), deny_standard_creds=True))
    assert not started and not (tmp_path / ".netrc").exists()


def test_a_retag_that_does_not_reach_the_disk_closes_the_shell(tmp_path, monkeypatch):
    """codex r3: the process-group retag was assumed durable; unwritten, a sweep after a levain crash
    would see only the dead parent's claim and remove a mountpoint the live sandbox stands on."""
    import levain.firing.confinement as conf
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    real_replace = os.replace
    commits = []

    def second_commit_fails(src, dst):
        if str(dst).endswith(conf._LEDGER_NAME):
            commits.append(1)
            if len(commits) == 2:
                raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    class _P:
        pid = 4242

    def start(self):
        self._after_spawn(_P.pid)   # each command tags the claim with its group, the probe first
        return self

    closed = []
    monkeypatch.setattr(conf.os, "replace", second_commit_fails)
    monkeypatch.setattr(conf._BwrapShell, "start", start)
    monkeypatch.setattr(conf._BwrapShell, "close", lambda self: closed.append(1))
    with pytest.raises(ConfinementError, match="cannot be written"):
        conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)))
    assert closed
