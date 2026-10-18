"""Text-only tests for the defensive macOS socket and persistence floor.

Nothing in this module starts the Seatbelt driver or applies a profile.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path

import pytest

from levain.firing import confinement as conf
from levain.firing import seatbelt_guard
from levain.firing import ssh_agent


def _policy(tmp_path: Path, monkeypatch) -> conf.CrownJewelsPolicy:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    return conf.build_policy(entity, workspace=tmp_path / "workspace", deny_standard_creds=False)


def test_profile_default_denies_unix_and_allows_only_declared_paths(tmp_path, monkeypatch) -> None:
    policy = _policy(tmp_path, monkeypatch)
    grant = conf.SocketGrant("/tmp/gpg/S.gpg-agent.extra", "gpg", "signing", gpg_home="/g")
    agent = ssh_agent.ScopedAgent("0123456789ab", "/tmp/scoped/agent.sock", 123)

    text = conf.SeatbeltProvider().render_profile(policy, grants=(grant,), ssh_agent=agent)

    deny = "(deny network-outbound (remote unix-socket))"
    mdns = ('(allow network-outbound (remote unix-socket '
            '(path-literal "/private/var/run/mDNSResponder")))')
    gpg = ('(allow network-outbound (remote unix-socket '
           f'(path-literal "{os.path.realpath(grant.path)}")))')
    scoped = ('(allow network-outbound (remote unix-socket '
              f'(path-literal "{os.path.realpath(agent.socket)}")))')
    assert text.count(deny) == 1
    assert all(text.index(rule) > text.index(deny) for rule in (mdns, gpg, scoped))
    assert "S.gpg-agent\"" not in text
    assert "S.gpg-agent.ssh" not in text and "S.gpg-agent.browser" not in text
    assert f'(subpath "{Path(os.path.realpath(grant.path)).parent}")' in text
    assert f'(subpath "{Path(os.path.realpath(agent.socket)).parent}")' in text
    assert ('(allow file-read* file-write* '
            '(literal "/tmp/scoped/known_hosts"))') in text


def test_profile_denies_home_and_agent_persistence_but_not_the_workspace(tmp_path, monkeypatch) -> None:
    policy = _policy(tmp_path, monkeypatch)
    home, workspace = Path.home(), policy.workspace
    text = conf.SeatbeltProvider().render_profile(policy)

    assert f'^{conf._sbpl_regex(str(home))}\\/[^\\/]+$' in text
    for path in (
        home / "bin",
        home / ".local" / "bin",
        home / "Library" / "LaunchAgents",
        home / ".config" / "git",
    ):
        assert f'(subpath "{path}")' in text
    for path in policy.persistence_agent_roots:
        assert f'(regex #"{conf._agent_root_regex(path)}")' in text
    assert r"\.[cC][lL][aA][uU][dD][eE]($|\/.*)" in text
    assert r"\.[cC][oO][dD][eE][xX]($|\/.*)" in text
    assert f'(subpath "{workspace}")' not in text
    assert str(workspace / "ordinary.txt") not in text


def test_persistence_policy_is_the_source_for_editor_and_profile(tmp_path, monkeypatch) -> None:
    policy = _policy(tmp_path, monkeypatch)
    workspace = policy.workspace
    text = conf.SeatbeltProvider().render_profile(policy)

    for path in (
        workspace / ".codex" / "config.toml",
        workspace / ".codex" / "hooks.json",
        workspace / ".codex" / "notes.txt",
        workspace / ".claude" / "settings.local.json",
        workspace / ".claude" / "hooks" / "plant.sh",
        workspace / ".claude" / "ordinary.md",
    ):
        assert conf.crown_jewel_reason(policy, path) is not None
    assert conf.crown_jewel_reason(policy, workspace / "ordinary.txt") is None
    for path in policy.persistence_write_files:
        assert f'(literal "{path}")' in text
    for path in policy.persistence_write_subtrees:
        assert f'(subpath "{path}")' in text
    for path in policy.persistence_agent_dirs:
        assert f'(subpath "{path}")' in text
    for path in policy.persistence_agent_hops:
        assert f'(literal "{path}")' in text


def test_symlinked_agent_roots_deny_lexical_and_resolved_targets(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    real_codex = workspace / "real-codex"
    real_claude = workspace / "real-claude"
    real_codex.mkdir(parents=True)
    real_claude.mkdir()
    workspace.mkdir(exist_ok=True)
    (workspace / ".codex").symlink_to(real_codex, target_is_directory=True)
    (workspace / ".claude").symlink_to(real_claude, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    text = conf.SeatbeltProvider().render_profile(policy)

    assert conf.crown_jewel_reason(policy, workspace / ".codex" / "config.toml") is not None
    assert conf.crown_jewel_reason(policy, real_codex / "config.toml") is not None
    assert conf.crown_jewel_reason(policy, workspace / ".claude" / "settings.json") is not None
    assert conf.crown_jewel_reason(policy, real_claude / "settings.json") is not None
    assert f'(literal "{workspace / ".codex"}")' in text
    assert f'(subpath "{real_codex}")' in text
    assert f'(subpath "{real_claude}")' in text


@pytest.mark.parametrize("name", [".claude", ".Claude", ".CLAUDE", ".codex", ".Codex", ".CODEX"])
def test_agent_directories_are_wholly_denied_case_insensitively(tmp_path, monkeypatch, name) -> None:
    policy = _policy(tmp_path, monkeypatch)
    for anchor in (Path.home(), policy.workspace):
        assert conf.crown_jewel_reason(policy, anchor / name) is not None
        assert conf.crown_jewel_reason(policy, anchor / name / "ordinary.txt") is not None


def test_symlinked_file_below_agent_root_protects_its_resolved_target(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    protected = workspace / ".claude" / "settings.json"
    target = workspace / "real-config" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("SAFE")
    protected.parent.mkdir()
    protected.symlink_to(target)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    text = conf.SeatbeltProvider().render_profile(policy)

    assert target in policy.persistence_agent_files
    assert f'(literal "{target}")' in text
    assert conf.crown_jewel_reason(policy, protected) is not None
    assert conf.crown_jewel_reason(policy, target) is not None


def test_swappable_intermediate_agent_symlink_is_pinned(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    dir_a = workspace / "dirA"
    real = dir_a / "real"
    real.mkdir(parents=True)
    workspace.joinpath("sub").symlink_to(dir_a, target_is_directory=True)
    workspace.joinpath(".codex").symlink_to(Path("sub") / "real", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    text = conf.SeatbeltProvider().render_profile(policy)

    for hop in (workspace / ".codex", workspace / "sub", dir_a, real):
        assert hop in policy.persistence_agent_hops
        assert f'(literal "{hop}")' in text
        assert conf.crown_jewel_reason(policy, hop) is not None
    assert f'(subpath "{real}")' in text
    assert conf.crown_jewel_reason(policy, real / "config.toml") is not None


def test_broken_agent_root_refuses_policy_build(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".claude").symlink_to(workspace / "missing")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    with pytest.raises(conf.ConfinementError, match=r"protected persistence path .*\.claude.*cannot be resolved"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_looping_agent_root_refuses_policy_build(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".codex").symlink_to(workspace / ".codex")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    with pytest.raises(conf.ConfinementError, match="protected persistence path.*cannot be resolved"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_home_top_level_entry_is_denied_by_editor_and_inode_guard(tmp_path, monkeypatch) -> None:
    policy = _policy(tmp_path, monkeypatch)
    home, workspace = Path.home(), policy.workspace
    home.mkdir(parents=True)
    top = home / ".inputrc"
    top.write_text("protected")
    workspace.mkdir(parents=True)
    os.link(top, workspace / "alias")
    policy = conf.build_policy(policy.entity_dir, workspace=workspace, deny_standard_creds=False)

    assert conf.crown_jewel_reason(policy, top) is not None
    with pytest.raises(conf.ConfinementError, match="crown jewel with 2 names"):
        conf._refuse_multiply_linked_jewels(policy)


@pytest.mark.parametrize("kind", ["config", "hook", "case-settings", "ordinary"])
def test_hardlink_alias_of_persistence_file_refuses_spawn(tmp_path, monkeypatch, kind) -> None:
    policy = _policy(tmp_path, monkeypatch)
    home, workspace = Path.home(), policy.workspace
    if kind == "config":
        protected = home / ".codex" / "config.toml"
    elif kind == "hook":
        protected = home / ".claude" / "hooks" / "commit.sh"
    elif kind == "case-settings":
        protected = home / ".Claude" / "Settings.JSON"
    else:
        protected = home / ".codex" / "ordinary.txt"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("protected")
    workspace.mkdir(parents=True, exist_ok=True)
    os.link(protected, workspace / f"{kind}-alias")
    policy = conf.build_policy(policy.entity_dir, workspace=workspace, deny_standard_creds=False)
    with pytest.raises(conf.ConfinementError, match="crown jewel with 2 names"):
        conf._refuse_multiply_linked_jewels(policy)


def test_profile_matrix_never_contains_a_kernel_mask_token(tmp_path, monkeypatch) -> None:
    policy = _policy(tmp_path, monkeypatch)
    grant = conf.SocketGrant("/tmp/gpg/S.gpg-agent.extra", "gpg", "signing", gpg_home="/g")
    agent = ssh_agent.ScopedAgent("0123456789ab", "/tmp/scoped/agent.sock", 123)
    for grants in ((), (grant,)):
        for scoped in (None, agent):
            for keychain in (False, True):
                for localhost in (False, True):
                    cell = conf.replace(policy, deny_keychain=keychain,
                                        deny_localhost_outbound=localhost)
                    text = conf.SeatbeltProvider().render_profile(
                        cell, grants=grants, ssh_agent=scoped
                    )
                    assert "syscall" not in text.lower()
                    assert seatbelt_guard.scan(text) == []


def test_macos_spawn_strips_the_ambient_agent_and_sets_only_the_scoped_one(
    tmp_path, monkeypatch
) -> None:
    policy = _policy(tmp_path, monkeypatch)
    grant = conf.SocketGrant("/tmp/gpg/S.gpg-agent.extra", "gpg", "signing", gpg_home="/g")
    agent = ssh_agent.ScopedAgent("0123456789ab", "/tmp/scoped/agent.sock", 123)
    made = []

    class FakeShell:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            made.append(self)

        def start(self):
            return self

    monkeypatch.setattr(conf, "sandbox_exec_available", lambda: True)
    monkeypatch.setattr(conf, "_validate_macos_grant", lambda value: None)
    monkeypatch.setattr(conf, "_validate_macos_ssh_agent", lambda value: None)
    monkeypatch.setattr(conf, "_SeatbeltShell", FakeShell)

    shell = conf.SeatbeltProvider()._spawn_shell_impl(
        policy,
        env={"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/operator/agent",
             "GIT_SSH": "/attacker/git-ssh", "GIT_SSH_COMMAND": "/attacker/wrapper"},
        grants=(grant,),
        ssh_agent=agent,
    )

    assert shell is made[0]
    assert shell.env["SSH_AUTH_SOCK"] == agent.socket
    assert "IdentityAgent=SSH_AUTH_SOCK" in shell.env["GIT_SSH_COMMAND"]
    assert str(agent.dir / ssh_agent.KNOWN_HOSTS_NAME) in shell.env["GIT_SSH_COMMAND"]
    assert "/attacker/wrapper" not in shell.env["GIT_SSH_COMMAND"]
    assert "GIT_SSH" not in shell.env
    profile = shell.argv[shell.argv.index("-p") + 1]
    assert grant.path in profile and agent.socket in profile

    plain = conf.SeatbeltProvider()._spawn_shell_impl(
        policy,
        env={"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/operator/agent"},
    )
    assert "SSH_AUTH_SOCK" not in plain.env


def test_macos_ssh_add_context_check_refuses_inside_and_allows_operator(monkeypatch) -> None:
    monkeypatch.setattr(ssh_agent, "_macos_confinement_state", lambda: True)
    assert "every levain macOS confinement" in (ssh_agent.confinement_refusal("Darwin") or "")
    monkeypatch.setattr(ssh_agent, "_macos_confinement_state", lambda: False)
    assert ssh_agent.confinement_refusal("Darwin") is None
    monkeypatch.setattr(ssh_agent, "_macos_confinement_state", lambda: None)
    assert "cannot ask Seatbelt" in (ssh_agent.confinement_refusal("Darwin") or "")


def test_macos_context_query_declares_the_fixed_variadic_abi(monkeypatch) -> None:
    class Check:
        restype = None
        argtypes = None

        def __call__(self, *args):
            assert self.argtypes == [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            assert args[1].value == b"file-write-data"
            assert args[2].value == 1
            assert args[3].value == os.fsencode(ssh_agent.MACOS_CONFINEMENT_SENTINEL)
            return 0

    check = Check()
    monkeypatch.setattr(
        ssh_agent.ctypes,
        "CDLL",
        lambda *args, **kwargs: type("Lib", (), {"sandbox_check": check})(),
    )
    assert ssh_agent._macos_confinement_state() is False


def test_macos_context_query_treats_negative_as_uncertainty(monkeypatch) -> None:
    class Check:
        restype = None
        argtypes = None

        def __call__(self, *_args):
            return -1

    check = Check()
    monkeypatch.setattr(
        ssh_agent.ctypes,
        "CDLL",
        lambda *args, **kwargs: type("Lib", (), {"sandbox_check": check})(),
    )
    assert ssh_agent._macos_confinement_state() is None


def test_macos_profile_always_denies_the_fixed_context_sentinel(tmp_path, monkeypatch) -> None:
    text = conf.SeatbeltProvider().render_profile(_policy(tmp_path, monkeypatch))
    assert (f'(deny file-write* (literal "{ssh_agent.MACOS_CONFINEMENT_SENTINEL}"))'
            in text)


@pytest.mark.parametrize("gpg", [False, True])
def test_macos_operator_grants_fail_closed_without_listener_identity_or_gpg_mapping(gpg) -> None:
    grant = conf.SocketGrant("/private/tmp/service.sock", "service", "work",
                             gpg_home="/g" if gpg else None)
    with pytest.raises(conf.ConfinementError, match=(
        "no protected mapping" if gpg else "cannot be shown not to be an ssh agent"
    )):
        conf._validate_macos_grant(grant)


def test_macos_scoped_agent_refuses_a_noncanonical_socket_spelling(tmp_path) -> None:
    real = tmp_path / "real" / "0123456789ab"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "real", target_is_directory=True)
    agent = ssh_agent.ScopedAgent("0123456789ab", str(alias / "0123456789ab" / "agent.sock"), 123)
    with pytest.raises(conf.ConfinementError, match="is not canonical"):
        conf._validate_macos_ssh_agent(agent)


def test_macos_status_names_the_stricter_same_domain_cost(monkeypatch) -> None:
    monkeypatch.setattr(conf._landlock, "host_read", lambda system=None: (0, None))
    monkeypatch.setattr(conf, "load_operator_grants", lambda: ())
    monkeypatch.setattr(ssh_agent, "openssh_problem", lambda: None)
    lines = conf.unix_socket_lines(system="Darwin")
    assert lines[0] == conf.MACOS_SOCKET_TIER_LINE
    assert "sockets created inside the sandbox are denied too" in lines[0]
    assert "default grant: /private/var/run/mDNSResponder (name resolution)" in lines
