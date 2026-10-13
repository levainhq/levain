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

def test_an_absent_grant_file_grants_nothing_and_a_valid_one_loads(grants, tmp_path):
    assert conf.load_operator_grants(tmp_path / "absent.json") == ()
    grants([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg", "why": "signing"},
            {"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    got = conf.load_operator_grants()
    assert got == (SocketGrant("/run/user/1/gnupg/S.gpg-agent.extra", "gpg", "signing"),
                   SocketGrant("/run/user/1/podman/podman.sock", "podman", "builds"))
    assert [g.gpg_extra for g in got] == [True, False]


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
])
def test_a_grant_file_levain_cannot_take_as_written_is_refused(grants, entries, why):
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
        with pytest.raises(ConfinementError, match="'podman'.*which confined bash can write"):
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
              SocketGrant(str(rd / "gnupg" / "S.gpg-agent.extra"), "gpg", "w"))
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

def _spawn(tmp_path, monkeypatch, env):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setattr(conf, "bwrap_available", lambda: True)
    monkeypatch.setattr(conf, "_prepare_mountpoints", lambda mounted, made=None: None)
    monkeypatch.setattr(conf, "_default_grant_ops", lambda: ["--bind", "/run/j/dev-log", "/dev/log"])
    seen = {}
    monkeypatch.setattr(conf._BwrapShell, "start", lambda self: seen.setdefault("shell", self))
    monkeypatch.setattr(conf._BwrapShell, "_recheck", lambda self: None)
    monkeypatch.setattr(conf._landlock, "host_read", lambda: (10, None))   # read as T9; no syscall
    conf.BwrapProvider()._spawn_shell_impl(build_policy(_entity(tmp_path)), env=env)
    return seen["shell"]


def test_the_spawn_binds_each_grant_and_hands_the_stub_its_view_path(rd, grants, tmp_path, monkeypatch):
    socks = [_listen(rd / "podman" / "p.sock"), _listen(rd / "gnupg" / "S.gpg-agent.extra")]
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


def test_no_grant_reaches_a_hands_users_shell(rd, grants):
    """The hands spawn reads no grant file: the operator's sockets are not the hands user's to use."""
    import inspect

    src = inspect.getsource(conf.BwrapProvider._spawn_bwrap_canaried)
    assert "load_operator_grants() if hands is None else ()" in src


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
    got = conf._gpg_program_spellings()
    real_home = home.resolve()
    assert (home / "bin" / "pinentry") in got   # the name the conf gives (a link: re-pointing it is denied)
    assert (real_home / "bin" / "pinentry-real") in got   # and what it runs
    assert (home / "tools" / "scdaemon") in got   # absent but creatable: a plant is denied
    assert not any("nonexistent" in str(p) for p in got)   # absent and not creatable: nothing to deny
    policy = build_policy(_entity(tmp_path))
    assert (real_home / "bin" / "pinentry-real") in policy.deny_write_files
    assert (home / "bin") in policy.deny_write_dirs   # renaming its directory is refused too


def test_a_program_this_user_cannot_change_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(conf, "_gpg_components", lambda: [Path("/bin/sh")])   # root's, in root's dir
    assert conf._gpg_program_spellings() == []


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
    grants([{"path": "/run/user/1/gnupg/S.gpg-agent.extra", "name": "gpg", "why": "signing"},
            {"path": "/run/user/1/podman/podman.sock", "name": "podman", "why": "builds"}])
    monkeypatch.setattr(L, "host_read", lambda system=None: (8, None))
    lines = conf.unix_socket_lines(SimpleNamespace(runtime_sockets=("podman/podman.sock",)), system="Linux")
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
    grants("{broken")
    (r,) = doctor._check_unix_sockets(ent)
    assert not r.ok and "will refuse bash" in r.detail


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
