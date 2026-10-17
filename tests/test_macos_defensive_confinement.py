"""Text-only tests for the defensive macOS socket and persistence floor.

Nothing in this module starts the Seatbelt driver or applies a profile.
"""
from __future__ import annotations

import ctypes
from pathlib import Path

from levain.firing import confinement as conf
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
           '(path-literal "/tmp/gpg/S.gpg-agent.extra")))')
    scoped = ('(allow network-outbound (remote unix-socket '
              '(path-literal "/tmp/scoped/agent.sock")))')
    assert text.count(deny) == 1
    assert all(text.index(rule) > text.index(deny) for rule in (mdns, gpg, scoped))
    assert "S.gpg-agent\"" not in text
    assert "S.gpg-agent.ssh" not in text and "S.gpg-agent.browser" not in text
    assert '(subpath "/tmp/gpg")' in text and '(subpath "/tmp/scoped")' in text
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
        home / ".claude" / "hooks",
        workspace / ".claude" / "hooks",
    ):
        assert f'(subpath "{path}")' in text
    for path in (
        home / ".codex" / "hooks.json",
        home / ".codex" / "config.toml",
        workspace / ".codex" / "hooks.json",
        workspace / ".codex" / "config.toml",
    ):
        assert f'(literal "{path}")' in text
    assert f'^{conf._sbpl_regex(str(home / ".claude"))}\\/settings[^\\/]*\\.json$' in text
    assert f'^{conf._sbpl_regex(str(workspace / ".claude"))}\\/settings[^\\/]*\\.json$' in text
    assert f'(subpath "{workspace}")' not in text
    assert str(workspace / "ordinary.txt") not in text
    assert f'(subpath "{home / ".claude"}")' not in text
    assert f'(subpath "{home / ".codex"}")' not in text


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
        env={"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/operator/agent"},
        grants=(grant,),
        ssh_agent=agent,
    )

    assert shell is made[0]
    assert shell.env["SSH_AUTH_SOCK"] == agent.socket
    assert "IdentityAgent=SSH_AUTH_SOCK" in shell.env["GIT_SSH_COMMAND"]
    assert str(agent.dir / ssh_agent.KNOWN_HOSTS_NAME) in shell.env["GIT_SSH_COMMAND"]
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
            return 0

    check = Check()
    monkeypatch.setattr(
        ssh_agent.ctypes,
        "CDLL",
        lambda *args, **kwargs: type("Lib", (), {"sandbox_check": check})(),
    )
    assert ssh_agent._macos_confinement_state() is False


def test_macos_status_names_the_stricter_same_domain_cost(monkeypatch) -> None:
    monkeypatch.setattr(conf._landlock, "host_read", lambda system=None: (0, None))
    monkeypatch.setattr(conf, "load_operator_grants", lambda: ())
    monkeypatch.setattr(ssh_agent, "openssh_problem", lambda: None)
    lines = conf.unix_socket_lines(system="Darwin")
    assert lines[0] == conf.MACOS_SOCKET_TIER_LINE
    assert "sockets created inside the sandbox are denied too" in lines[0]
    assert "default grant: /private/var/run/mDNSResponder (name resolution)" in lines
