"""A crown-jewel file with more than one name refuses bash at spawn.

REPRODUCED 2026-10-03 at e8f403d on macOS seatbelt and Linux bwrap: a pre-planted hardlink to a
``deny_files`` token printed it through the link, and writing through a hardlink to
``authorized_keys`` added a line to the host's real file, while both direct paths were refused.
"""
from __future__ import annotations

import dataclasses
import os
import platform
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


def _blocked(monkeypatch, target: Path) -> None:
    real_stat = os.stat

    def stat(p, *a, **k):
        if str(p) == str(target):
            raise PermissionError(13, "Permission denied", str(p))
        return real_stat(p, *a, **k)

    monkeypatch.setattr(confinement.os, "stat", stat)
    monkeypatch.setattr(confinement, "_identity", lambda p: confinement._UNREACHABLE)


def test_a_file_blocked_by_another_users_dir_refuses(home, monkeypatch) -> None:
    """codex L3 r1: the another-user exception let an unverifiable jewel file through."""
    secret = _secret(home)
    policy = build_policy(_entity(home), deny_files=(secret,))
    _blocked(monkeypatch, secret)
    with pytest.raises(ConfinementError, match="could not check"):
        _refuse_multiply_linked_jewels(policy)


def test_a_socket_blocked_by_another_users_dir_is_left_alone(home, monkeypatch) -> None:
    sock = home / "other-runtime" / "docker.sock"
    policy = dataclasses.replace(build_policy(_entity(home)), deny_sockets=(sock,))
    _blocked(monkeypatch, sock)
    _refuse_multiply_linked_jewels(policy)


def _case_insensitive(d: Path) -> bool:
    (d / "CaseProbe").write_text("")
    try:
        return (d / "caseprobe").exists()
    finally:
        (d / "CaseProbe").unlink()


def test_two_spellings_of_one_entry_do_not_hide_an_outside_link(home) -> None:
    """codex + complement L3 r1: names were counted as strings, so `token` and `TOKEN` (one entry on
    a case-insensitive volume) counted as two and hid the outside link."""
    secret = _secret(home)
    if not _case_insensitive(secret.parent):
        pytest.skip("needs a case-insensitive volume")
    os.link(secret, home / "outside")
    policy = build_policy(_entity(home), deny_files=(secret, secret.parent / "TOKEN"))
    with pytest.raises(ConfinementError, match="2 names on disk, and only 1"):
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


def test_a_linked_own_memory_file_is_refused(home) -> None:
    ent = _entity(home)
    mem = ent / ".levain" / "memory.continuity.md"
    mem.write_text("own memory")
    os.link(mem, ent / "workspace" / "mem.md")
    with pytest.raises(ConfinementError, match="memory.continuity.md is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(ent))


def test_a_linked_confinement_config_is_refused(home) -> None:
    ent = _entity(home)
    cfg = ent / ".levain" / "confinement.json"
    cfg.write_text("{}")
    os.link(cfg, ent / "workspace" / "cfg.json")
    with pytest.raises(ConfinementError, match="confinement.json is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(ent))


def test_a_linked_private_key_under_the_hidden_ssh_dir_is_refused(home) -> None:
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("PRIVATE KEY")
    os.link(ssh / "id_ed25519", home / "key")
    with pytest.raises(ConfinementError, match="id_ed25519 is a crown jewel"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode="agent"))


@pytest.mark.skipif(platform.system() != "Linux", reason="bwrap step (7) sockets are Linux-only")
def test_a_linked_session_bus_is_refused_on_linux(home, monkeypatch) -> None:
    import socket
    import tempfile
    rd = Path(tempfile.mkdtemp(dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rd))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(rd / "bus"))
        os.link(rd / "bus", home / "bus2")
        with pytest.raises(ConfinementError, match="bus"):
            _refuse_multiply_linked_jewels(build_policy(_entity(home)))
    finally:
        s.close()
        (home / "bus2").unlink(missing_ok=True)
        (rd / "bus").unlink(missing_ok=True)
        rd.rmdir()


def test_linked_jewel_reason_names_the_jewel_for_a_link_and_spares_plain_files(home) -> None:
    from levain.firing.confinement import linked_jewel_reason
    secret = _secret(home)
    os.link(secret, home / "t")
    plain = home / "notes.txt"
    plain.write_text("x")
    os.link(plain, home / "notes2.txt")   # two names, neither a jewel
    policy = build_policy(_entity(home), deny_files=(secret,))
    reason = linked_jewel_reason(policy, home / "t")
    assert reason and "token" in reason and "hardlink" in reason
    assert linked_jewel_reason(policy, plain) is None
    assert linked_jewel_reason(policy, home / "absent") is None


def test_the_file_editor_refuses_a_hardlink_to_a_jewel(home) -> None:
    """L1 (RUN, 8783e51): bash was refused, a direct view of the token was refused, and a view of
    the planted link returned the token."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    from openhands.tools.file_editor.definition import FileEditorAction
    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor
    ent = _entity(home)
    secret = _secret(home)
    os.link(secret, ent / "workspace" / "t")
    ex = CrownJewelsFileEditorExecutor(policy=build_policy(ent, deny_files=(secret,)))
    obs = ex(FileEditorAction(command="view", path=str(ent / "workspace" / "t")))
    text = "".join(c.text for c in obs.to_llm_content if getattr(c, "text", None))
    assert obs.is_error and "REFUSED" in text and "SECRET" not in text, text
    (ent / "workspace" / "ok.txt").write_text("fine")
    ok = ex(FileEditorAction(command="view", path=str(ent / "workspace" / "ok.txt")))
    assert not ok.is_error
