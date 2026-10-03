"""A crown-jewel file with more than one name refuses bash at spawn.

REPRODUCED 2026-10-03 at e8f403d on macOS seatbelt and Linux bwrap: a pre-planted hardlink to a
``deny_files`` token printed it through the link, and writing through a hardlink to
``authorized_keys`` added a line to the host's real file, while both direct paths were refused.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from levain.firing import confinement
from levain.firing.confinement import (
    ConfinementError,
    ConfinementProvider,
    SandboxedShell,
    _refuse_multiply_linked_jewels,
    build_policy,
)
from tests.test_floor_project_memory import _entity, home, live  # noqa: F401


def _secret(home: Path) -> Path:
    d = home / "secrets"
    d.mkdir()
    f = d / "token"
    f.write_text("SECRET")
    return f


def test_no_extra_links_spawns(home) -> None:
    _refuse_multiply_linked_jewels(build_policy(_entity(home), deny_files=(_secret(home),)))


def test_a_linked_deny_file_is_refused_and_named(home) -> None:
    secret = _secret(home)
    os.link(secret, home / "elsewhere")
    with pytest.raises(ConfinementError, match=r"token is a crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), deny_files=(secret,)))


def test_a_linked_deny_file_reached_through_a_symlink_is_refused(home) -> None:
    secret = _secret(home)
    os.link(secret, home / "elsewhere")
    alias = home / "alias"
    alias.symlink_to(secret)
    with pytest.raises(ConfinementError, match="2 names"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), deny_files=(alias,)))


@pytest.mark.parametrize("ssh_mode", ["agent", "raw"])
def test_a_linked_write_only_vector_is_refused(home, ssh_mode) -> None:
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA real\n")
    os.link(ssh / "authorized_keys", home / "ak")
    with pytest.raises(ConfinementError, match="authorized_keys is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode=ssh_mode))


def test_a_linked_file_under_a_hidden_subtree_is_refused(home) -> None:
    store = home / ".anneal-memory"
    (store / "deep").mkdir(parents=True)
    (store / "deep" / "memory.db").write_text("store")
    os.link(store / "deep" / "memory.db", home / "copy.db")
    with pytest.raises(ConfinementError, match="memory.db is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home)))


def test_a_subtree_root_that_is_a_linked_file_is_refused(home) -> None:
    store = home / ".anneal-memory"   # argushub's shape: the root is a SQLite file
    store.write_text("store")
    os.link(store, home / "copy.db")
    with pytest.raises(ConfinementError, match=r"\.anneal-memory is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home)))


def test_a_symlink_inside_a_subtree_is_not_followed(home) -> None:
    outside = home / "shared.txt"
    outside.write_text("not a jewel")
    os.link(outside, home / "shared2.txt")
    store = home / ".anneal-memory"
    store.mkdir()
    (store / "pointer").symlink_to(outside)
    _refuse_multiply_linked_jewels(build_policy(_entity(home)))


class _Recorder(ConfinementProvider):
    name = "recorder"

    def __init__(self) -> None:
        self.spawned = False

    def available(self) -> bool:
        return True

    def render_profile(self, policy):
        return ""

    def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0):
        self.spawned = True
        raise ConfinementError("recorder reached the platform spawn")


def test_spawn_shell_refuses_before_the_platform_spawn(home) -> None:
    secret = _secret(home)
    os.link(secret, home / "elsewhere")
    p = _Recorder()
    with pytest.raises(ConfinementError, match="2 names"):
        p.spawn_shell(build_policy(_entity(home), deny_files=(secret,)))
    assert not p.spawned


@live
def test_live_a_planted_link_refuses_the_shell_and_the_host_file_is_untouched(home) -> None:
    from levain.firing.confinement import select_provider
    ent = _entity(home)
    ssh = home / ".ssh"
    ssh.mkdir()
    ak = ssh / "authorized_keys"
    ak.write_text("ssh-ed25519 AAAA real\n")
    os.link(ak, ent / "workspace" / "ak")
    with pytest.raises(ConfinementError, match="authorized_keys is a crown jewel with 2 names"):
        select_provider().spawn_shell(build_policy(ent, ssh_mode="raw"))
    os.unlink(ent / "workspace" / "ak")
    with select_provider().spawn_shell(build_policy(ent, ssh_mode="raw")) as sh:   # control
        r = sh.run(f"echo planted >> {ak} 2>&1", timeout=20)
        assert r.exit_code != 0, r.output
    assert ak.read_text() == "ssh-ed25519 AAAA real\n"


_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(_ROOT, reason="root ignores directory modes")
@pytest.mark.parametrize("mode", [0o300, 0o600])
def test_a_jewel_dir_made_unreadable_refuses_instead_of_skipping(home, mode) -> None:
    """L2 (RUN): chmod on the jewel's own directory made the check skip it while the planted link
    stayed readable. Unverifiable is now a refusal."""
    store = home / ".anneal-memory"
    locked = store / "locked"
    locked.mkdir(parents=True)
    (locked / "memory.db").write_text("store")
    os.link(locked / "memory.db", home / "copy.db")
    locked.chmod(mode)
    try:
        with pytest.raises(ConfinementError, match="could not check"):
            _refuse_multiply_linked_jewels(build_policy(_entity(home)))
    finally:
        locked.chmod(0o700)


@pytest.mark.skipif(_ROOT, reason="root ignores directory modes")
def test_a_linked_authorized_keys_behind_a_locked_ssh_dir_is_refused(home) -> None:
    """L2's worst case: link authorized_keys out, then chmod 0600 ~/.ssh so stat fails. sshd (root)
    still honours the key."""
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA real\n")
    os.link(ssh / "authorized_keys", home / "ak")
    ssh.chmod(0o600)
    try:
        with pytest.raises(ConfinementError, match="could not check"):
            _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode="raw"))
    finally:
        ssh.chmod(0o700)


def test_a_path_blocked_by_another_users_dir_is_left_alone(home, monkeypatch) -> None:
    secret = _secret(home)
    policy = build_policy(_entity(home), deny_files=(secret,))
    real_stat = os.stat

    def stat(p, *a, **k):
        if str(p) == str(secret):
            raise PermissionError(13, "Permission denied", str(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(confinement.os, "stat", stat)
    monkeypatch.setattr(confinement, "_identity", lambda p: confinement._UNREACHABLE)
    _refuse_multiply_linked_jewels(policy)


def test_two_names_both_inside_the_floor_do_not_refuse(home) -> None:
    store = home / ".anneal-memory"
    store.mkdir()
    (store / "a.db").write_text("store")
    os.link(store / "a.db", store / "b.db")
    _refuse_multiply_linked_jewels(build_policy(_entity(home)))


def test_a_linked_daemon_socket_is_refused(home, tmp_path) -> None:
    """L2 (RUN on Linux bwrap): a link to a user-owned daemon socket reached the daemon while the
    socket's own path was denied."""
    import socket
    import tempfile
    d = Path(tempfile.mkdtemp(dir="/tmp"))   # AF_UNIX paths are length-limited
    sock_path = d / "daemon.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
        try:
            os.link(sock_path, d / "link.sock")
        except OSError as exc:
            pytest.skip(f"this filesystem does not hardlink sockets: {exc}")
        with pytest.raises(ConfinementError, match="daemon.sock|link.sock"):
            policy = dataclasses.replace(build_policy(_entity(home)), deny_sockets=(sock_path,))
            _refuse_multiply_linked_jewels(policy)
    finally:
        s.close()
        for q in (d / "link.sock", sock_path):
            if q.exists() or q.is_socket():
                q.unlink()
        d.rmdir()
