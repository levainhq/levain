"""Operator-declared unix-socket grants, the bind-backs they replace, and the gnupg program write-denies.

Mocked: no Landlock syscall, sandbox or seatbelt profile runs here. The Linux spawn path is driven with
bwrap and the shell start replaced, as tests/test_landlock.py does; live grant runs are the VM's.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import levain.firing.confinement as conf
from levain.firing import landlock as L
from levain.firing.confinement import ConfinementError, SocketGrant, build_policy

# Captured at import, before tests/conftest.py replaces it with the host-neutral stub for every test.
_REAL_GPG_COMPONENTS = conf._gpg_components
_REAL_GPG_EXTRA_SOCKETS = conf._gpg_extra_sockets


def _entity(root: Path) -> Path:
    d = root / "ent"
    (d / ".levain").mkdir(parents=True)
    return d


def _listen(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.listen(4)
    return s


@pytest.fixture
def rd(monkeypatch):
    """A user runtime dir under /tmp (unix socket paths are short), resolved, as the only one."""
    d = tempfile.TemporaryDirectory(dir="/tmp", prefix="lvg")
    root = Path(d.name).resolve()
    os.chmod(root, 0o700)
    monkeypatch.setattr(conf, "_runtime_dirs", lambda: [str(root)])
    yield root
    d.cleanup()


@pytest.fixture
def grants(tmp_path, monkeypatch):
    """Write the operator's grant file (0600) and point levain at it."""
    gf = tmp_path / "grants.json"
    monkeypatch.setattr(conf, "grants_file", lambda: gf)

    def write(entries) -> Path:
        gf.write_text(json.dumps({"grants": entries}) if not isinstance(entries, str) else entries)
        gf.chmod(0o600)
        return gf
    return write


# --- the grant file is an authority file ----------------------------------------------------------

def test_the_grant_directory_is_hidden_from_every_confined_process(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    policy = build_policy(_entity(tmp_path))
    gdir = (tmp_path / "lh" / conf.GRANTS_DIR).resolve()
    assert gdir in policy.deny_read_write
    assert gdir.parent in policy.deny_write_dirs   # its ancestors cannot be renamed
    assert conf.crown_jewel_reason(policy, gdir / conf.GRANTS_FILE) is not None   # the file editor refuses it


def test_a_hardlink_alias_of_the_grant_file_in_the_workspace_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    gdir = tmp_path / "lh" / conf.GRANTS_DIR
    gdir.mkdir(parents=True)
    (gdir / conf.GRANTS_FILE).write_text('{"grants": []}')
    ent = _entity(tmp_path)
    policy = build_policy(ent)
    policy.workspace.mkdir(parents=True, exist_ok=True)
    os.link(gdir / conf.GRANTS_FILE, policy.workspace / "alias.json")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        conf._refuse_multiply_linked_jewels(policy)


# --- loading -------------------------------------------------------------------------------------

def test_an_absent_grant_file_grants_nothing_and_a_valid_one_loads(grants, tmp_path, monkeypatch):
    assert conf.load_operator_grants(tmp_path / "absent.json") == ()
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {"/run/user/1/gnupg/S.gpg-agent.extra": "/h/.gnupg"})
    grants([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg", "why": "signing"},
            {"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    got = conf.load_operator_grants()
    assert got == (SocketGrant("/run/user/1/gnupg/S.gpg-agent.extra", "gpg", "signing", gpg_home="/h/.gnupg"),
                   SocketGrant("/run/user/1/podman/podman.sock", "podman", "builds"))
    assert [g.gpg_extra for g in got] == [True, False]


def test_a_grant_is_gpgs_only_at_the_path_gpgconf_names_never_by_its_name(grants, monkeypatch):
    """A socket is gpg's extra socket only where gpgconf puts the default home's or levain's GNUPGHOME's;
    another socket under that name (a daemon, another home's agent) is refused, never taken as gpg's."""
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {"/run/user/1/gnupg/S.gpg-agent.extra": "/h/.gnupg"})
    grants([{"path": "/run/user/1/x/S.gpg-agent.extra", "name": "podman", "why": "w"}])
    with pytest.raises(ConfinementError, match="gpg grant for another home is refused"):
        conf.load_operator_grants()
    grants([{"path": "/run/user/1/x/S.gpg-agent.extra2", "name": "named-alike", "why": "w"}])
    assert [g.gpg_extra for g in conf.load_operator_grants()] == [False]   # an ordinary grant


def test_gpgs_extra_sockets_are_asked_for_the_default_home_and_levains_gnupghome(monkeypatch):
    from types import SimpleNamespace

    seen = []
    monkeypatch.setattr(conf, "_gpg_extra_sockets", _REAL_GPG_EXTRA_SOCKETS)
    monkeypatch.setenv("GNUPGHOME", "/levains/g")
    monkeypatch.setattr(conf.shutil, "which", lambda name: "/usr/bin/gpgconf")
    out = {None: "/run/user/1/gnupg/S.gpg-agent.extra\n", "/levains/g": "/run/user/1/gnupg/d.x%3ay/S.gpg-agent.extra\n"}

    def run(argv, **kw):
        seen.append((argv, kw["env"]))
        return SimpleNamespace(returncode=0, stdout=out[argv[2] if argv[1] == "--homedir" else None])
    monkeypatch.setattr(conf.subprocess, "run", run)
    got = conf._gpg_extra_sockets()
    assert got == {"/run/user/1/gnupg/S.gpg-agent.extra": str(Path.home() / ".gnupg"),
                   "/run/user/1/gnupg/d.x:y/S.gpg-agent.extra": "/levains/g"}
    assert [a for a, _ in seen] == [["/usr/bin/gpgconf", "--list-dirs", "agent-extra-socket"],
                                    ["/usr/bin/gpgconf", "--homedir", "/levains/g", "--list-dirs",
                                     "agent-extra-socket"]]
    assert all("GNUPGHOME" not in env for _, env in seen)


@pytest.mark.parametrize("entries,why", [
    ("{not json", "not valid JSON"),
    ('{"grants": "x"}', r'must be \{"grants"'),
    ([{"path": "/run/a.sock", "name": "a"}], "non-empty path, name and why"),
    ([{"path": "run/a.sock", "name": "a", "why": "w"}], "not an exact absolute path"),
    ([{"path": "/run/../run/a.sock", "name": "a", "why": "w"}], "not an exact absolute path"),
    ([{"path": "/run/user/1/*.sock", "name": "a", "why": "w"}], "is a pattern"),
    ([{"path": "/run/user/1/gnupg/S.gpg-agent", "name": "a", "why": "w"}], "standard socket"),
    ([{"path": "/run/user/1/gnupg/S.gpg-agent.ssh", "name": "a", "why": "w"}], "ssh socket"),
    ([{"path": "/run/user/1/gnupg/S.gpg-agent.browser", "name": "a", "why": "w"}], "browser socket"),
    ([{"path": "/run/user/1/gnupg/S.dirmngr", "name": "a", "why": "w"}], "dirmngr"),
    ([{"path": "/run/a.sock", "name": "a", "why": "w"}, {"path": "/run/a.sock", "name": "b", "why": "w"}],
     "granted twice"),
    ([{"path": "/run/1/S.gpg-agent.extra", "name": "a", "why": "w"},
      {"path": "/run/2/S.gpg-agent.extra", "name": "b", "why": "w"}], "more than one gpg extra"),
    ([{"path": "/run/3/S.gpg-agent.extra", "name": "a", "why": "w"}], "gpg grant for another home"),
    (b'{"grants": []}\xff', "cannot be read"),
])
def test_a_grant_file_levain_cannot_take_as_written_is_refused(grants, entries, why, monkeypatch):
    monkeypatch.setattr(conf, "_gpg_extra_sockets",
                        lambda: {"/run/1/S.gpg-agent.extra": "/h1", "/run/2/S.gpg-agent.extra": "/h2"})
    if isinstance(entries, bytes):
        grants([]).write_bytes(entries)   # not UTF-8
    else:
        grants(entries)
    with pytest.raises(ConfinementError, match=why):
        conf.load_operator_grants()


def test_a_grant_file_that_is_a_link_or_others_can_write_is_refused(grants, tmp_path):
    gf = grants([])
    gf.chmod(0o620)
    with pytest.raises(ConfinementError, match="no one else can write"):
        conf.load_operator_grants()
    gf.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(gf)
    with pytest.raises(ConfinementError, match="a link is refused"):
        conf.load_operator_grants(link)


# --- resolution at entry -------------------------------------------------------------------------

def test_a_grant_resolves_only_as_an_operator_owned_socket_in_reach_of_nothing_confined(rd, tmp_path, monkeypatch):
    srv = _listen(rd / "podman" / "p.sock")
    try:
        ok = SocketGrant(str(rd / "podman" / "p.sock"), "podman", "builds")
        conf.resolve_grant(ok, [rd])   # inside the runtime dir's view: fine
        with pytest.raises(ConfinementError, match="'podman'.*where confined bash can rename it"):
            conf.resolve_grant(ok, [])   # the same dir, not a view: bash could replace the socket
        (rd / "file").write_text("x")
        (rd / "dir").mkdir()
        (rd / "link.sock").symlink_to(rd / "podman" / "p.sock")
        for path, why in ((rd / "absent.sock", "cannot be opened"), (rd / "file", "is not a socket"),
                          (rd / "dir", "is not a socket"), (rd / "link.sock", "is reached through a link")):
            with pytest.raises(ConfinementError, match=f"'x' \\({path}\\) {why}"):
                conf.resolve_grant(SocketGrant(str(path), "x", "w"), [rd])
        monkeypatch.setattr(conf.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(ConfinementError, match="not you"):
            conf.resolve_grant(ok, [rd])
    finally:
        srv.close()


def test_a_grant_under_a_directory_of_yours_in_sticky_tmp_is_refused():
    """Its own directory unwritable is not enough: in sticky /tmp you may rename what you own, so confined
    bash could move that directory away and put another in its place before the bind."""
    top = Path(tempfile.mkdtemp(dir="/tmp", prefix="lvst")).resolve()
    srv = _listen(top / "s.sock")
    try:
        os.chmod(top, 0o555)
        g = SocketGrant(str(top / "s.sock"), "s", "w")
        with pytest.raises(ConfinementError, match=f"has {top} in {top.parent}, where confined bash can rename it"):
            conf.resolve_grant(g, [])
        conf.resolve_grant(g, [top])   # a runtime dir's view: bash sees levain's mountpoint, not this
    finally:
        srv.close()
        os.chmod(top, 0o700)
        (top / "s.sock").unlink()
        top.rmdir()


def test_a_grant_whose_parent_another_user_can_write_is_refused(rd):
    srv = _listen(rd / "shared" / "s.sock")
    try:
        os.chmod(rd / "shared", 0o777)
        with pytest.raises(ConfinementError, match="another user can write"):
            conf.resolve_grant(SocketGrant(str(rd / "shared" / "s.sock"), "s", "w"), [rd])
    finally:
        srv.close()


def test_grant_ops_bind_each_socket_and_gpgs_extra_socket_where_bashs_gpg_looks(rd):
    socks = [_listen(rd / "podman" / "p.sock"), _listen(rd / "gnupg" / "S.gpg-agent.extra")]
    try:
        gs = (SocketGrant(str(rd / "podman" / "p.sock"), "podman", "w"),
              SocketGrant(str(rd / "gnupg" / "S.gpg-agent.extra"), "gpg", "w", gpg_home="/h/.gnupg"))
        view = rd / "gnupg" / "d.abc" / "S.gpg-agent"   # a non-default home's hashed socket dir
        ops, views = conf._grant_ops(gs, [rd], view)
        assert views == [str(rd / "podman" / "p.sock"), str(view)]
        assert ops == ["--perms", "0700", "--dir", str(rd / "podman"),
                       "--bind", str(rd / "podman" / "p.sock"), str(rd / "podman" / "p.sock"),
                       "--perms", "0700", "--dir", str(rd / "gnupg"),
                       "--perms", "0700", "--dir", str(rd / "gnupg" / "d.abc"),
                       "--bind", str(rd / "gnupg" / "S.gpg-agent.extra"), str(view)]
        with pytest.raises(ConfinementError, match="outside a user runtime dir"):
            conf._grant_ops(gs[1:], [rd], Path("/home/u/.gnupg/S.gpg-agent"))
        with pytest.raises(ConfinementError, match="no path gpgconf could name"):
            conf._grant_ops(gs[1:], [rd], None)
    finally:
        for s in socks:
            s.close()


def test_the_default_grant_is_bound_at_dev_log_from_the_socket_it_resolves_to(rd, monkeypatch):
    srv = _listen(rd / "journal" / "dev-log")
    try:
        (rd / "log").symlink_to(rd / "journal" / "dev-log")
        monkeypatch.setattr(L, "DEFAULT_GRANTS", (str(rd / "log"),))
        assert conf._default_grant_ops() == ["--bind", str(rd / "journal" / "dev-log"), str(rd / "log")]
        monkeypatch.setattr(L, "DEFAULT_GRANTS", (str(rd / "absent"),))
        assert conf._default_grant_ops() == []
    finally:
        srv.close()


# --- the spawn -----------------------------------------------------------------------------------

def _spawn(tmp_path, monkeypatch, env, *, hands=None, grants=None):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    monkeypatch.setattr(conf, "_default_grant_ops", lambda: ["--bind", "/run/j/dev-log", "/dev/log"])
    seen = {}
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: seen.setdefault("shell", self))
    monkeypatch.setattr(conf._BwrapShell, "_recheck", lambda self: None)
    monkeypatch.setattr(conf._landlock, "host_read", lambda: (10, None))   # read as T9; no syscall
    if grants is None:   # the conversation's snapshot, as its floor reads it
        grants = conf.snapshot_operator_grants(hands, system="Linux").get()
    conf.BwrapProvider()._spawn_bwrap(build_policy(_entity(tmp_path)), env, 120.0, hands, None, grants)
    return seen["shell"]


def test_the_spawn_binds_each_grant_and_hands_the_stub_its_view_path(rd, grants, tmp_path, monkeypatch):
    socks = [_listen(rd / "podman" / "p.sock"), _listen(rd / "gnupg" / "S.gpg-agent.extra")]
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {str(rd / "gnupg" / "S.gpg-agent.extra"): "/h/.gnupg"})
    grants([{"path": str(rd / "podman" / "p.sock"), "name": "podman", "why": "builds"},
            {"path": str(rd / "gnupg" / "S.gpg-agent.extra"), "name": "gpg", "why": "signing"}])
    asked = []
    monkeypatch.setattr(conf, "gpg_agent_view_path",
                        lambda env: asked.append(env) or rd / "gnupg" / "S.gpg-agent")
    try:
        sh = _spawn(tmp_path, monkeypatch, {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/agent.sock",
                                            "GNUPGHOME": "/home/u/g"})
        argv = sh._argv
        new_session = argv.index("--new-session")
        windows = [argv[k:k + 3] for k in range(new_session)]
        assert ["--bind", str(rd / "podman" / "p.sock"), str(rd / "podman" / "p.sock")] in windows
        assert ["--bind", str(rd / "gnupg" / "S.gpg-agent.extra"), str(rd / "gnupg" / "S.gpg-agent")] in windows
        assert ["--bind", "/run/j/dev-log", "/dev/log"] in windows
        at = argv.index(L.STUB)
        stub_grants = [argv[k + 1] for k in range(at, len(argv)) if argv[k] == "--grant"]
        assert stub_grants == [str(rd / "podman" / "p.sock"), str(rd / "gnupg" / "S.gpg-agent")]
        assert asked[0]["GNUPGHOME"] == "/home/u/g"   # the shell's own home decides where gpg looks
        assert "SSH_AUTH_SOCK" not in sh._env   # the operator's ambient agent is never passed
        assert str(rd / "podman" / "p.sock") in sh._manifest   # a replaced grant closes the shell
        sh.close()
    finally:
        for s in socks:
            s.close()


def test_a_grant_that_does_not_resolve_refuses_the_spawn_naming_it(rd, grants, tmp_path, monkeypatch):
    grants([{"path": str(rd / "podman" / "gone.sock"), "name": "podman", "why": "builds"}])
    with pytest.raises(ConfinementError, match="operator grant 'podman'.*cannot be opened"):
        _spawn(tmp_path, monkeypatch, {"PATH": "/usr/bin"})


def test_no_grant_reaches_a_hands_users_shell(rd, grants, tmp_path, monkeypatch):
    """A hands user's shell gets no operator grant: its snapshot is empty without reading the file (a
    broken one included), and a grant handed to its spawn anyway is neither bound nor given to the stub."""
    hands = conf.HandsIdentity(user="ent-hands", uid=os.getuid() + 1, home=str(tmp_path / "hh"),
                               workspace=tmp_path / "hw")
    grants("{broken")
    assert conf.snapshot_operator_grants(hands, system="Linux").get() == ()
    srv = _listen(rd / "podman" / "p.sock")
    try:
        g = SocketGrant(str(rd / "podman" / "p.sock"), "podman", "builds")
        monkeypatch.setattr(conf, "_hands_policy", lambda policy, hands: policy)
        monkeypatch.setattr(conf, "hands_prefix", lambda hands: ["sudo", "-u", hands.user, "--"])
        monkeypatch.setattr(conf, "_hands_env", lambda hands: {"PATH": "/usr/bin"})
        sh = _spawn(tmp_path, monkeypatch, {"PATH": "/usr/bin"}, hands=hands, grants=(g,))
        argv = sh._argv
        assert str(rd / "podman" / "p.sock") not in argv[:argv.index(L.STUB)]   # not bound
        assert "--grant" not in argv   # not granted by the stub
        sh._canary = None
        sh.close()
    finally:
        srv.close()


# --- gnupg's programs ----------------------------------------------------------------------------

def test_the_programs_gpg_agent_starts_are_write_denied_with_their_ancestors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".gnupg").mkdir(parents=True)
    (home / "bin").mkdir()
    (home / "bin" / "pinentry-real").write_text("#!/bin/sh\n")
    (home / "bin" / "pinentry").symlink_to(home / "bin" / "pinentry-real")
    (home / ".gnupg" / "gpg-agent.conf").write_text(
        "# comment\npinentry-program ~/bin/pinentry\nscdaemon-program /nonexistent/levain/scdaemon\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GNUPGHOME", raising=False)
    monkeypatch.setattr(conf, "_gpg_components", lambda: [home / "tools" / "scdaemon"])   # absent, creatable
    (home / "tools").mkdir()
    got = conf._gpg_program_spellings(components=True)
    real_home = home.resolve()
    assert (home / "bin" / "pinentry") in got   # the name the conf gives (a link: re-pointing it is denied)
    assert (real_home / "bin" / "pinentry-real") in got   # and what it runs
    assert (home / "tools" / "scdaemon") in got   # absent but creatable: a plant is denied
    assert not any("nonexistent" in str(p) for p in got)   # absent and not creatable: nothing to deny
    policy = build_policy(_entity(tmp_path))
    assert (real_home / "bin" / "pinentry-real") in policy.deny_write_files
    assert (home / "bin") in policy.deny_write_dirs   # renaming its directory is refused too
    # gnupg's defaults are asked for only with a gpg grant: the floor has them then, with their ancestors.
    assert (home / "tools" / "scdaemon") not in policy.deny_write_files
    with_gpg = conf.with_gpg_program_denies(policy)
    assert (home / "tools" / "scdaemon") in with_gpg.deny_write_files
    assert (home / "tools") in with_gpg.deny_write_dirs


def test_gpgconfs_components_are_asked_only_when_a_gpg_extra_socket_is_granted(grants, tmp_path, monkeypatch):
    """The conversation's floor asks gpgconf for gnupg's default programs only when its grant snapshot
    holds gpg's extra socket (build_policy never asks)."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    from levain.firing.openhands.tools import _SharedFloor

    asked = []
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "tools").mkdir(parents=True)
    monkeypatch.setattr(conf, "_gpg_components", lambda: asked.append(1) or [tmp_path / "home" / "tools" / "scd"])
    monkeypatch.setattr(conf.platform, "system", lambda: "Linux")
    policy = build_policy(_entity(tmp_path))
    grants([{"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    floor = _SharedFloor(policy)
    assert asked == [] and floor.policy == policy
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {"/run/user/1/gnupg/S.gpg-agent.extra": "/h"})
    grants([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg", "why": "signing"}])
    floor = _SharedFloor(policy)
    assert asked == [1] and (tmp_path / "home" / "tools" / "scd") in floor.policy.deny_write_files


def test_gnupgs_secret_keys_are_jewels_in_every_home_on_both_floors(tmp_path, monkeypatch):
    """The secret-key directory (and legacy secret keyring) of the default home and of levain's own
    $GNUPGHOME are denied both ways: in the policy's jewel lists, and so in the macOS profile (rendered
    as text only, never run)."""
    home = tmp_path / "home"
    (home / ".gnupg" / "private-keys-v1.d").mkdir(parents=True)
    (home / ".gnupg" / "secring.gpg").write_text("")
    other = tmp_path / "g2"   # absent: gpg creates it on first run, so its keys are denied already
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GNUPGHOME", str(other))
    policy = build_policy(_entity(tmp_path))
    profile = conf.SeatbeltProvider().render_profile(policy)
    for h in (home.resolve() / ".gnupg", other.resolve()):
        assert h / "private-keys-v1.d" in policy.deny_read_write
        assert f'(subpath "{h / "private-keys-v1.d"}")' in profile
    # The legacy keyring only where it exists (gnupg 2.1+ never creates one; absent cred files are not guessed).
    present = home.resolve() / ".gnupg" / "secring.gpg"
    assert present in policy.deny_files and f'(literal "{present}")' in profile
    assert other.resolve() / "secring.gpg" not in policy.deny_files


def test_a_direct_spawn_with_a_gpg_grant_carries_gnupgs_program_denies(tmp_path, monkeypatch):
    """The provider's own spawn applies them, so a caller without the conversation floor is covered."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "tools").mkdir(parents=True)
    scd = tmp_path / "home" / "tools" / "scd"
    monkeypatch.setattr(conf, "_gpg_components", lambda: [scd])
    seen = []

    class _Direct(conf.ConfinementProvider):
        def available(self):
            return True

        def render_profile(self, policy):
            return ""

        def _spawn_shell_impl(self, policy, *, env=None, default_timeout=120.0, grants=None):
            seen.append(policy)
            return conf.SandboxedShell(argv=["/bin/true"], cwd=tmp_path, env={})

    policy = build_policy(_entity(tmp_path))
    assert scd not in policy.deny_write_files
    podman = SocketGrant("/run/user/1/podman/podman.sock", "podman", "builds")
    gpg = SocketGrant("/run/user/1/gnupg/S.gpg-agent.extra", "gpg", "signing", gpg_home="/h/.gnupg")
    _Direct().spawn_shell(policy, grants=(podman,))
    assert scd not in seen[-1].deny_write_files   # no gpg grant: not asked for
    shell = _Direct().spawn_shell(policy, grants=(podman, gpg))
    assert scd in seen[-1].deny_write_files and scd in shell.effective_policy.deny_write_files
    assert scd.parent in seen[-1].deny_write_dirs


def test_a_program_this_user_cannot_change_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "_gpg_components", lambda: [Path("/bin/sh")])   # root's, in root's dir
    assert conf._gpg_program_spellings(components=True) == []
    # A link in a directory this user cannot write, to such a binary (Linux's /bin/sh -> dash): the
    # link's own mode bits do not make it changeable (CI on Linux 2026-10-13 read them as writable).
    (tmp_path / "ro").mkdir()
    (tmp_path / "ro" / "sh").symlink_to("/bin/sh")
    (tmp_path / "ro").chmod(0o555)
    try:
        monkeypatch.setattr(conf, "_gpg_components", lambda: [tmp_path / "ro" / "sh"])
        assert conf._gpg_program_spellings(components=True) == []
    finally:
        (tmp_path / "ro").chmod(0o755)


def test_gpgconfs_components_give_the_default_pinentry_and_scdaemon(monkeypatch):
    seen = []
    monkeypatch.setattr(conf.shutil, "which", lambda name: "/usr/bin/gpgconf")
    out = ("gpg:OpenPGP:/usr/bin/gpg\ngpg-agent:Private Keys:/usr/bin/gpg-agent\n"
           "scdaemon:Smartcards:/usr/lib/gnupg/scdaemon\npinentry:Passphrase Entry:/opt/x%3ay/pinentry\n")
    monkeypatch.setattr(conf.subprocess, "run", lambda argv, **kw: seen.append(argv) or SimpleNamespace(
        returncode=0, stdout=out))
    assert _REAL_GPG_COMPONENTS() == [Path("/usr/lib/gnupg/scdaemon"), Path("/opt/x:y/pinentry")]
    assert seen == [["/usr/bin/gpgconf", "--list-components"]]


# --- run / doctor output -------------------------------------------------------------------------

def test_one_generator_gives_the_tier_each_waiver_the_defaults_ssh_and_the_ignored_key(grants, monkeypatch):
    monkeypatch.setattr(conf, "_gpg_extra_sockets", lambda: {"/run/user/1/gnupg/S.gpg-agent.extra": "/h"})
    grants([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg", "why": "signing"},
            {"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    monkeypatch.setattr(L, "host_read", lambda system=None: (8, None))
    lines = conf.unix_socket_lines(SimpleNamespace(runtime_sockets=("podman/podman.sock",), hands_user=None),
                                   system="Linux")
    assert lines[0] == L.tier_line(8)
    assert lines[1] == ("waiver (operator grant) gpg: /run/user/1/gnupg/S.gpg-agent.extra (signing); "
                        + conf.GPG_EXTRA_WAIVER)
    assert lines[2] == "waiver (operator grant) podman: /run/user/1/podman/podman.sock (builds)"
    assert lines[3] == "default grant: /dev/log (write-only syslog)"
    assert lines[4].startswith("ssh agent: none until levain's scoped agent (B2)")
    assert lines[5].startswith("ignored: runtime_sockets in .levain/confinement.json (podman/podman.sock)")
    monkeypatch.setattr(L, "host_read", lambda system=None: (0, None))
    mac = conf.unix_socket_lines(None, system="Darwin")
    assert mac[0].startswith("unix sockets (T0: Darwin has no Landlock)")
    assert "default grant: /private/var/run/mDNSResponder (name resolution)" in mac
    # Never bound on macOS, so one line says so, and the file is neither read nor validated here (a
    # Linux gpg path, or a broken file, does not fail this host's doctor).
    assert conf.GRANTS_NOT_APPLIED_LINE in mac and not any("waiver" in ln for ln in mac)
    grants("{broken")
    assert conf.GRANTS_NOT_APPLIED_LINE in conf.unix_socket_lines(None, system="Darwin")


def test_a_hands_users_entity_reads_and_reports_no_operator_grant(grants, tmp_path, monkeypatch):
    """Its shell gets none, so run and doctor neither read the file (a broken one included) nor list one."""
    from levain import doctor

    grants("{broken")
    ent = _entity(tmp_path)
    cfg = SimpleNamespace(hands_user="ent-hands", runtime_sockets=())
    monkeypatch.setattr(conf, "load_confinement_config", lambda d: cfg)
    lines = conf.unix_socket_lines(cfg, system="Linux")
    assert conf.HANDS_GRANTS_LINE in lines and not any("waiver" in ln for ln in lines)
    (r,) = doctor._check_unix_sockets(ent)
    assert r.ok and conf.HANDS_GRANTS_LINE in r.detail


def test_doctor_and_run_print_the_same_lines(grants, tmp_path, capsys, monkeypatch):
    from levain import doctor
    from levain.run import _print_banner

    grants([{"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    ent = _entity(tmp_path)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"runtime_sockets": ["bus"]}))
    lines = conf.unix_socket_lines(conf.load_confinement_config(ent))
    for r in doctor._check_unix_sockets(ent):
        doctor._emit(r)
    out = capsys.readouterr().out
    assert all(line in out for line in lines), out
    binding = SimpleNamespace(episodic_path=tmp_path / "e", crystal_path=tmp_path / "c")
    _print_banner(ent, binding, model="m", with_tools=True, bash_ok=True, socket_lines=lines)
    banner = " ".join(capsys.readouterr().out.split())
    assert all(" ".join(line.split()) in banner for line in lines), banner
    # Where grants apply (the file is not read elsewhere), a broken one fails doctor.
    monkeypatch.setattr(conf.platform, "system", lambda: "Linux")
    monkeypatch.setattr(L, "host_read", lambda system=None: (0, None))   # no syscall
    grants("{broken")
    (r,) = doctor._check_unix_sockets(ent)
    assert not r.ok and "will refuse bash" in r.detail
    grants([])
    monkeypatch.setattr(conf, "unix_socket_lines", lambda cfg: (_ for _ in ()).throw(UnicodeDecodeError(
        "utf-8", b"\xff", 0, 1, "invalid start byte")))
    (r,) = doctor._check_unix_sockets(ent)
    assert not r.ok and "not determinable" in r.detail   # undeterminable is a FAIL, never a pass


def test_the_canary_is_stopped_with_the_claims_release_not_before(tmp_path, monkeypatch):
    """A close that keeps the claim (a command's group not yet settled) keeps the canary answering; the
    close that releases the claim stops it."""
    sh = _spawn(tmp_path, monkeypatch, {"PATH": "/usr/bin"})
    canary = sh._canary
    monkeypatch.setattr(conf._BwrapShell, "_settled", lambda self: False)
    sh.close()
    assert sh._ledger_claim is not None and os.path.exists(canary.path)
    monkeypatch.setattr(conf._BwrapShell, "_settled", lambda self: True)
    sh.close()
    assert sh._ledger_claim is None and not os.path.exists(canary.path)


def test_every_spawn_of_a_conversations_shell_uses_the_grants_read_when_it_opened(grants, tmp_path, monkeypatch):
    """The floor reads the grant file once; the first spawn and every respawn of its shell get that
    snapshot, so a grant added after chat checked the session never reaches a lazy respawn."""
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    import levain.firing.openhands.tools as _t
    from levain.firing.confinement import SandboxedShell

    monkeypatch.setattr(conf.platform, "system", lambda: "Linux")
    ent = _entity(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    grants([{"path": "/run/user/1/bus", "name": "bus", "why": "notifications"}])
    ex = _t.SandboxedBashExecutor(build_policy(ent, workspace=ws))
    grants([{"path": "/run/user/1/bus", "name": "bus", "why": "notifications"},
            {"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "added later"}])
    seen = []

    class _Recording:
        def spawn_shell(self, policy, *, env=None, default_timeout=120.0, grants=None):
            seen.append(tuple(g.name for g in grants or ()))
            sh = SandboxedShell(argv=["/bin/true"], cwd=ws, env={})
            sh.effective_policy = policy
            return sh

    monkeypatch.setattr(_t, "select_provider", lambda: _Recording())
    ex._ensure_shell().close()   # the shell exits: the next command respawns it
    ex._ensure_shell().close()
    assert seen == [("bus",), ("bus",)]


def test_a_binding_written_with_the_legacy_runtime_sockets_field_resumes_without_it(tmp_path):
    """An autosaved conversation from before grants carries ``runtime_sockets`` in its floor: it grants
    nothing now, so it is dropped and the binding resumes; any other unexpected field still refuses."""
    from levain.firing.binding import BindingError, ConversationBinding

    ent = _entity(tmp_path)
    b = ConversationBinding.create(ent, mode="interactive", workspace=tmp_path / "ws")
    old = json.loads(json.dumps(b.to_params()))   # JSON, as the SDK's autosave keeps it
    old["floor"]["runtime_sockets"] = ["podman/podman.sock"]
    assert ConversationBinding.from_params(old).floor == b.floor
    old["floor"]["runtime_sockets"] = "podman/podman.sock"
    with pytest.raises(BindingError, match="runtime_sockets"):
        ConversationBinding.from_params(old)
    del old["floor"]["runtime_sockets"]
    old["floor"]["something_else"] = []
    with pytest.raises(BindingError, match="exactly the crown-jewels policy fields"):
        ConversationBinding.from_params(old)
