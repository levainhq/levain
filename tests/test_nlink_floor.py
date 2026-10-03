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


def test_a_file_blocked_by_another_users_dir_refuses(home, monkeypatch) -> None:
    """codex L3 r1: the another-user exception let an unverifiable jewel file through."""
    secret = _secret(home)
    policy = build_policy(_entity(home), deny_files=(secret,))
    _blocked(monkeypatch, secret)
    with pytest.raises(ConfinementError, match="could not check"):
        _refuse_multiply_linked_jewels(policy)


def _foreign_dir() -> Path:
    """A real directory owned by another user that this user cannot search (``/var/audit`` on macOS,
    ``/root`` on Linux), so a path under it cannot be stat-ed for real."""
    for d in ("/var/audit", "/root", "/var/lib/private", "/etc/ssl/private"):
        try:
            if os.lstat(d).st_uid == os.geteuid():
                continue
            os.stat(os.path.join(d, "levain-probe"))
        except PermissionError:
            return Path(d)
        except OSError:
            continue
    pytest.skip("no directory owned by another user that this user cannot search")


@pytest.mark.skipif(_ROOT, reason="root searches every directory")
@pytest.mark.parametrize("field", ["deny_files", "deny_sockets"])
def test_a_path_behind_another_users_dir_refuses_whatever_list_names_it(home, field) -> None:
    """codex, the r1 fix-diff round: the socket exception trusted the roster spelling, so a
    socket-listed path behind another user's directory was skipped without checking its type or
    owner. Behind a REAL root-owned directory, both a deny file and a socket path now refuse."""
    hidden = _foreign_dir() / "docker.sock"
    policy = dataclasses.replace(build_policy(_entity(home)), **{field: (hidden,)})
    with pytest.raises(ConfinementError, match="could not check"):
        _refuse_multiply_linked_jewels(policy)


@pytest.mark.skipif(_ROOT, reason="root searches every directory")
def test_a_socket_in_another_users_runtime_dir_refuses_and_names_the_fix(home, monkeypatch) -> None:
    """The su case. An earlier version skipped these sockets; codex + glm (nlink L3) showed the skip
    misses a link the socket's owner made at a public path, which a check that never sees the inode
    cannot count. So it refuses, and says how to fix the inherited variable."""
    foreign = _foreign_dir()
    monkeypatch.setattr(confinement, "_runtime_dirs", lambda: [str(foreign)])
    policy = build_policy(_entity(home))   # the $XDG_RUNTIME_DIR roster entries land under it
    assert [f for f in policy.deny_write_files if str(f).startswith(os.path.realpath(foreign))]
    with pytest.raises(ConfinementError, match="unset XDG_RUNTIME_DIR"):
        _refuse_multiply_linked_jewels(policy)


def test_a_symlink_spelled_as_a_runtime_dir_is_not_foreign(home, monkeypatch, tmp_path) -> None:
    """Ownership is read from the directory itself: a link this user owns, pointing at another
    user's directory, does not drop the sockets under it."""
    foreign = _foreign_dir()
    link = tmp_path / "rt"
    link.symlink_to(foreign)
    monkeypatch.setattr(confinement, "_runtime_dirs", lambda: [str(link)])
    assert confinement._foreign_runtime_dirs() == []


def _case_insensitive(d: Path) -> bool:
    (d / "CaseProbe").write_text("")
    try:
        return (d / "caseprobe").exists()
    finally:
        (d / "CaseProbe").unlink()


def test_two_spellings_of_one_entry_do_not_hide_an_outside_link(home) -> None:
    """codex + complement L3 r1: names were counted as strings, so `token` and `TOKEN` (one entry on
    a case-insensitive volume) counted as two and hid the outside link. The strict rule reads
    st_nlink alone, so listing a jewel at two spellings cannot hide anything."""
    secret = _secret(home)
    if not _case_insensitive(secret.parent):
        pytest.skip("needs a case-insensitive volume")
    os.link(secret, home / "outside")
    policy = build_policy(_entity(home), deny_files=(secret, secret.parent / "TOKEN"))
    with pytest.raises(ConfinementError, match="2 names on disk"):
        _refuse_multiply_linked_jewels(policy)


def test_two_names_both_inside_a_hidden_subtree_refuse(home) -> None:
    """The strict rule: four review rounds each found a way that counting names "inside the floor"
    misjudged, so a jewel with a second name refuses wherever that name is, and says how to find it."""
    store = home / ".anneal-memory"
    store.mkdir()
    (store / "a.db").write_text("store")
    os.link(store / "a.db", store / "b.db")
    with pytest.raises(ConfinementError, match=r"\.db is a crown jewel with 2 names") as exc:
        _refuse_multiply_linked_jewels(build_policy(_entity(home)))
    assert "find" in str(exc.value) and "-samefile" in str(exc.value)


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
    own = linked_jewel_reason(policy, secret)   # the jewel's own name, with a second name elsewhere
    assert own and "token" in own and "hardlink" in own
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


def test_linked_jewel_reason_refuses_every_name_of_a_linked_jewel(home) -> None:
    """codex + complement, the r1 fix-diff round: the editor allowed a path whose directory-entry key
    matched a jewel's, so a bind alias of a hidden directory read the store. The strict rule has no
    allow branch: every name of a jewel with more than one name is refused, its own included."""
    from levain.firing.confinement import linked_jewel_reason
    store = home / ".anneal-memory"
    store.mkdir()
    (store / "x.db").write_text("STORE")
    alias = (home / "alias").resolve()
    alias.mkdir()
    os.link(store / "x.db", alias / "x.db")
    policy = build_policy(_entity(home))
    reason = linked_jewel_reason(policy, alias / "x.db")
    assert reason and "x.db" in reason and "hardlink" in reason
    own = linked_jewel_reason(policy, store.resolve() / "x.db")
    assert own and "x.db" in own and "hardlink" in own


def test_linked_jewel_reason_refuses_a_same_dir_case_variant_link(home) -> None:
    """The second shape of the same finding: on a case-sensitive volume ``TOKEN`` beside ``token``
    is a separate name for the jewel."""
    from levain.firing.confinement import linked_jewel_reason
    secret = _secret(home)
    if _case_insensitive(secret.parent):
        pytest.skip("needs a case-sensitive volume")
    os.link(secret, secret.parent / "TOKEN")
    policy = build_policy(_entity(home), deny_files=(secret,))
    reason = linked_jewel_reason(policy, secret.parent / "TOKEN")
    assert reason and "hardlink" in reason


@pytest.mark.skipif(platform.system() != "Linux" or not confinement.bwrap_available(),
                    reason="a real bind alias needs a working bwrap")
def test_live_the_file_editor_check_refuses_a_real_bind_alias(home) -> None:
    """RUN on argushub at c02ee1e before the fix: inside ``bwrap --bind <hidden> <alias>``, both
    crown_jewel_reason and linked_jewel_reason returned None for ``<alias>/x.db`` and the editor
    would have read the store."""
    import subprocess
    import sys
    store = home / ".anneal-memory"
    store.mkdir()
    (store / "x.db").write_text("STORE")
    os.link(store / "x.db", home / "elsewhere.db")
    alias = home / "alias"
    alias.mkdir()
    ent = _entity(home)
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from levain.firing.confinement import build_policy, crown_jewel_reason, linked_jewel_reason\n"
        f"p = build_policy(Path({str(ent)!r}))\n"
        f"t = Path({str(alias / 'x.db')!r})\n"
        "r = crown_jewel_reason(p, t) or linked_jewel_reason(p, t)\n"
        "print('REFUSED' if r else 'READ ' + t.read_text())\n"
    )
    env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": str(Path(confinement.__file__).parents[2])}
    out = subprocess.run(["bwrap", "--dev-bind", "/", "/", "--bind", str(store), str(alias),
                          sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "REFUSED", out.stdout


@pytest.mark.skipif(_ROOT, reason="root searches every directory")
@pytest.mark.parametrize("rd", [os.path.realpath("/tmp"), "/"])
def test_a_reachable_socket_under_a_foreign_owned_runtime_dir_is_still_checked(home, monkeypatch, rd) -> None:
    """L2 (RUN at 29ed753): owning the runtime dir proves nothing about reach. With
    $XDG_RUNTIME_DIR at a root-owned shared dir, or at /, a socket this user owns, with a second
    link beside it, was dropped from the check."""
    import socket
    monkeypatch.setattr(confinement, "_runtime_dirs", lambda: [rd])
    d = Path(os.path.realpath("/tmp"))
    sock, link = d / f"lv-nlink-{os.getpid()}.sock", d / f"lv-nlink-{os.getpid()}-2.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(sock))
        try:
            os.link(sock, link)
        except OSError as exc:
            pytest.skip(f"this filesystem does not hardlink sockets: {exc}")
        assert confinement._foreign_runtime_dirs(), "precondition: the runtime dir reads as foreign"
        policy = dataclasses.replace(build_policy(_entity(home)), deny_sockets=(sock,),
                                     socket_spellings=(sock,))
        with pytest.raises(ConfinementError, match="2 names on disk"):
            _refuse_multiply_linked_jewels(policy)
    finally:
        s.close()
        link.unlink(missing_ok=True)
        sock.unlink(missing_ok=True)


@pytest.mark.skipif(_ROOT, reason="root searches every directory")
def test_a_socket_blocked_by_this_users_own_dir_inside_a_foreign_runtime_dir_refuses(home, monkeypatch) -> None:
    """A directory this user owns can be chmod-ed open again, so a socket behind it is not
    unreachable: it refuses instead of being dropped."""
    d = Path(os.path.realpath("/tmp"))
    monkeypatch.setattr(confinement, "_runtime_dirs", lambda: [str(d)])
    mine = d / f"lv-nlink-{os.getpid()}-locked"
    mine.mkdir()
    try:
        mine.chmod(0o000)
        sock = mine / "docker.sock"
        policy = dataclasses.replace(build_policy(_entity(home)), deny_sockets=(sock,),
                                     socket_spellings=(sock,))
        with pytest.raises(ConfinementError, match="could not check"):
            _refuse_multiply_linked_jewels(policy)
    finally:
        mine.chmod(0o700)
        mine.rmdir()


def test_a_read_denied_jewel_linked_as_the_entitys_own_memory_refuses(home) -> None:
    """L1 (RUN at 29ed753): the floor lets bash READ the entity's own memory files, so a deny_files
    token whose second name is memory.continuity.md was counted as fully covered and bash was
    granted, with the token readable through the memory path."""
    ent = _entity(home)
    secret = _secret(home)
    os.link(secret, ent / ".levain" / "memory.continuity.md")
    with pytest.raises(ConfinementError, match="token is a crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(ent, deny_files=(secret,)))


def test_a_read_denied_jewel_linked_as_rebound_known_hosts_refuses(home) -> None:
    """In agent mode ~/.ssh is hidden but known_hosts is bound back read-write for ssh itself."""
    ssh = home / ".ssh"
    ssh.mkdir()
    secret = _secret(home)
    os.link(secret, ssh / "known_hosts")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode="agent",
                                                    deny_files=(secret,)))


def test_a_hidden_write_only_file_linked_as_own_memory_refuses(home) -> None:
    """codex (nlink L3): agent mode hides ~/.ssh, so ~/.ssh/rc is not readable; linked to the
    entity's memory (readable) it passed an access-graded count, and bash read rc through the
    memory name."""
    ent = _entity(home)
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "rc").write_text("hidden")
    os.link(ssh / "rc", ent / ".levain" / "memory.continuity.md")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(ent, ssh_mode="agent"))


def test_authorized_keys_linked_as_the_rebound_known_hosts_refuses(home) -> None:
    """complement (nlink L3): known_hosts is bound back read-write in agent mode, so a link from
    authorized_keys to it would let bash append a key."""
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA real\n")
    os.link(ssh / "authorized_keys", ssh / "known_hosts")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode="agent"))


def test_two_names_with_the_same_access_refuse(home) -> None:
    """raw mode: authorized_keys and authorized_keys2 are both read-only binds. The access-graded
    rule let this pair through; the strict rule refuses any second name."""
    ssh = home / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA real\n")
    os.link(ssh / "authorized_keys", ssh / "authorized_keys2")
    with pytest.raises(ConfinementError, match="authorized_keys is a crown jewel with 2 names"):
        _refuse_multiply_linked_jewels(build_policy(_entity(home), ssh_mode="raw"))


def test_an_unresolvable_floor_path_refuses_instead_of_crashing(home) -> None:
    """codex (nlink L3), RUN at 6cf411c: ~/.ssh replaced by a symlink loop after the policy was
    built raised RuntimeError out of spawn instead of a refusal."""
    (home / ".ssh").mkdir()
    policy = build_policy(_entity(home), ssh_mode="agent")
    (home / ".ssh").rmdir()
    (home / ".ssh").symlink_to(home / ".ssh")
    with pytest.raises(ConfinementError):
        _refuse_multiply_linked_jewels(policy)


@pytest.mark.parametrize("suffix", ["/", "/."])
def test_the_file_editor_refuses_a_hardlink_spelled_with_a_trailing_suffix(home, suffix) -> None:
    """codex, the nlink L3 round (RUN on /bin/test and /bin/[): a raw ``<link>/`` stats as ENOTDIR,
    which read as absent, and the stock editor's ``Path`` then dropped the suffix and opened the link."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    from openhands.tools.file_editor.definition import FileEditorAction
    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor
    ent = _entity(home)
    secret = _secret(home)
    os.link(secret, ent / "workspace" / "t")
    ex = CrownJewelsFileEditorExecutor(policy=build_policy(ent, deny_files=(secret,)))
    obs = ex(FileEditorAction(command="view", path=str(ent / "workspace" / "t") + suffix))
    text = "".join(c.text for c in obs.to_llm_content if getattr(c, "text", None))
    assert obs.is_error and "REFUSED" in text and "SECRET" not in text, text


def test_linked_jewel_reason_returns_none_for_a_nul_byte(home) -> None:
    from levain.firing.confinement import linked_jewel_reason
    policy = build_policy(_entity(home))
    assert linked_jewel_reason(policy, str(home / "a\x00b")) is None


@pytest.mark.parametrize("roots", [[os.sep], ["//", os.sep]])
def test_an_unverifiable_jewel_under_a_root_runtime_dir_gets_no_xdg_hint(home, monkeypatch, roots) -> None:
    """complement, the nlink L3 round: a foreign runtime dir of ``/`` matched every path, so any
    unverifiable jewel was blamed on an inherited XDG_RUNTIME_DIR; codex, its fix-diff round:
    ``XDG_RUNTIME_DIR=//`` did too."""
    monkeypatch.setattr(confinement, "_foreign_runtime_dirs", lambda: roots)
    secret = _secret(home)
    policy = build_policy(_entity(home), deny_files=(secret,))
    monkeypatch.setattr(confinement.os, "stat", _raise_eacces_for(str(secret), os.stat))
    with pytest.raises(ConfinementError) as info:
        _refuse_multiply_linked_jewels(policy)
    assert "could not check" in str(info.value) and "XDG_RUNTIME_DIR" not in str(info.value)


def _raise_eacces_for(target: str, real):
    def fake(path, *a, **kw):
        if os.fspath(path) == target or os.path.realpath(os.fspath(path)) == os.path.realpath(target):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real(path, *a, **kw)
    return fake
