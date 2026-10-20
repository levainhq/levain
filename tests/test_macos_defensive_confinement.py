"""Text-only tests for the defensive macOS socket and persistence floor.

Nothing in this module starts the Seatbelt driver or applies a profile.
"""
from __future__ import annotations

import ctypes
import errno
import logging
import os
from pathlib import Path

import pytest

from levain.firing import confinement as conf
from levain.firing import seatbelt_guard
from levain.firing import ssh_agent

# Taken at import, before the autouse fixture replaces it with "no git" for every test.
_REAL_RUN_GIT_CONFIG = conf._run_git_config


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
    for path in policy.persistence_agent_pins:
        assert f'(literal "{path}")' in text


def _clear_git_env(monkeypatch) -> None:
    for var in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL"):
        monkeypatch.delenv(var, raising=False)


def test_xdg_config_home_git_subtree_is_write_denied(tmp_path, monkeypatch) -> None:
    # git reads $XDG_CONFIG_HOME/git/{config,attributes}, and a filter driver either names runs
    # during the cockpit's status reads.
    _clear_git_env(monkeypatch)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    policy = _policy(tmp_path, monkeypatch)
    text = conf.SeatbeltProvider().render_profile(policy)

    for git_dir in (xdg / "git", Path(os.path.realpath(xdg)) / "git"):
        assert git_dir in policy.persistence_write_subtrees
        assert f'(subpath "{git_dir}")' in text
        for name in ("config", "attributes"):
            assert conf.crown_jewel_reason(policy, git_dir / name) is not None


def test_git_config_global_file_is_write_denied(tmp_path, monkeypatch) -> None:
    _clear_git_env(monkeypatch)
    cfg = tmp_path / "elsewhere" / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    policy = _policy(tmp_path, monkeypatch)
    text = conf.SeatbeltProvider().render_profile(policy)

    for spelling in (cfg, Path(os.path.realpath(cfg))):
        assert spelling in policy.persistence_write_files
        assert f'(literal "{spelling}")' in text
        assert conf.crown_jewel_reason(policy, spelling) is not None


def _rebuild(tmp_path: Path) -> conf.CrownJewelsPolicy:
    return conf.build_policy(tmp_path / "entity", workspace=tmp_path / "workspace",
                             deny_standard_creds=False)


def test_git_config_global_dev_null_exactly_adds_nothing(tmp_path, monkeypatch) -> None:
    _clear_git_env(monkeypatch)
    baseline = _policy(tmp_path, monkeypatch)
    for value in ("/dev/null", "/dev/./null"):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", value)
        assert _rebuild(tmp_path).persistence_write_files == baseline.persistence_write_files


@pytest.mark.parametrize("value", ["/dev/shm/agent_gitcfg", "/dev/../tmp/levain-b2/operator.gitconfig"])
def test_git_config_global_elsewhere_under_dev_is_protected(tmp_path, monkeypatch, value) -> None:
    # git reads the real file: /dev/shm is a writable tmpfs on Linux, and /dev/.. is not /dev.
    _clear_git_env(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", value)
    policy = _policy(tmp_path, monkeypatch)
    assert Path(value) in policy.persistence_write_files
    assert conf.crown_jewel_reason(policy, Path(os.path.realpath(value))) is not None


@pytest.mark.parametrize("var,value", [("GIT_CONFIG_GLOBAL", "relative/gitconfig"),
                                       ("XDG_CONFIG_HOME", "relative/xdg")])
def test_a_relative_git_config_location_refuses_the_floor(tmp_path, monkeypatch, var, value) -> None:
    # git resolves it against its own working directory, which for the cockpit is the workspace.
    _clear_git_env(monkeypatch)
    monkeypatch.setenv(var, value)
    with pytest.raises(conf.ConfinementError, match=rf"\${var} is the relative path.*fail-closed"):
        _policy(tmp_path, monkeypatch)


def test_files_git_reports_reading_are_write_denied(tmp_path, monkeypatch) -> None:
    _clear_git_env(monkeypatch)
    home = tmp_path / "home"
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    seen: list[dict] = []

    def fake_git(args, environ):
        seen.append(dict(environ))
        if "--list" in args:
            return f"file:{home / '.gitconfig'}\0include.path\0file:{cfg / 'inc.cfg'}\0x.y\0command line:\0a.b\0"
        if "--get-regexp" in args:
            return (f"file:{home / '.gitconfig'}\0include.path\ninc.cfg\0"
                    f"file:{home / '.gitconfig'}\0includeif.gitdir:~/w/.path\n~/cond.cfg\0"
                    f"file:{cfg / 'inc.cfg'}\0include.path\n{cfg / 'absent.cfg'}\0")
        if "core.attributesFile" in args:
            return "~/attrs\n"
        raise AssertionError(args)

    monkeypatch.setattr(conf, "_run_git_config", fake_git)
    policy = _policy(tmp_path, monkeypatch)
    text = conf.SeatbeltProvider().render_profile(policy)
    for derived in (home / ".gitconfig", cfg / "inc.cfg", home / "inc.cfg", home / "cond.cfg",
                    cfg / "absent.cfg", home / "attrs"):
        for spelling in (derived, Path(os.path.realpath(derived))):
            assert spelling in policy.persistence_write_files, spelling
            assert f'(literal "{spelling}")' in text
    # git is asked with the operator's environment.
    assert seen and all(e.get("HOME") == str(home) for e in seen)


def test_a_missing_or_failing_git_adds_nothing(tmp_path, monkeypatch) -> None:
    _clear_git_env(monkeypatch)
    baseline = _policy(tmp_path, monkeypatch)
    monkeypatch.setattr(conf, "_run_git_config", lambda args, environ: None)
    assert _rebuild(tmp_path).persistence_write_files == baseline.persistence_write_files
    monkeypatch.setattr(conf, "_run_git_config", lambda args, environ: "")
    assert _rebuild(tmp_path).persistence_write_files == baseline.persistence_write_files


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def _planted_git_on_path(tmp_path: Path, monkeypatch) -> Path:
    """An executable ``git`` first on PATH, as an activated workspace venv would put it; returns the
    marker it leaves when anything runs it."""
    marker = tmp_path / "planted-git-ran"
    _script(tmp_path / "venv-bin" / "git",
            f": > '{marker}'\nprintf 'file:{tmp_path}/planted.cfg\\000k\\000'")
    monkeypatch.setenv("PATH", f"{tmp_path / 'venv-bin'}{os.pathsep}{os.environ.get('PATH', '')}")
    return marker


def test_git_config_never_runs_a_git_found_on_path(tmp_path, monkeypatch) -> None:
    # L3 r2 (codex HIGH): the derivation runs git as the operator at every policy build, so a PATH
    # lookup ran whatever the agent could plant in a workspace-local PATH entry.
    from levain.consent import push
    marker = _planted_git_on_path(tmp_path, monkeypatch)
    trusted_ran = tmp_path / "trusted-git-ran"
    trusted = _script(tmp_path / "root-owned" / "git", f": > '{trusted_ran}'")
    monkeypatch.setattr(push, "git_bin", lambda: str(trusted))
    assert _REAL_RUN_GIT_CONFIG(["--list"], {"HOME": str(tmp_path / "home")}) == ""
    assert trusted_ran.exists()
    assert not marker.exists()


def test_no_root_owned_git_derives_nothing_and_runs_nothing(tmp_path, monkeypatch) -> None:
    from levain.consent import push
    marker = _planted_git_on_path(tmp_path, monkeypatch)

    def refuse():
        raise push.PushRefused("no root-owned git")

    monkeypatch.setattr(push, "git_bin", refuse)
    monkeypatch.setattr(conf, "_run_git_config", _REAL_RUN_GIT_CONFIG)
    assert conf._git_config_read_files({"HOME": str(tmp_path / "home")}) == []
    assert not marker.exists()


def _git_that(tmp_path: Path, monkeypatch, body: str) -> dict:
    """A trusted git that runs ``body``, and an operator environment whose global config exists."""
    from levain.consent import push
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".gitconfig").write_text("[user]\n\tname = x\n")
    git = _script(tmp_path / "root-owned" / "git", body)
    monkeypatch.setattr(push, "git_bin", lambda: str(git))
    return {"HOME": str(home)}


def _git_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "levain.firing.confinement" and r.levelno == logging.WARNING]


@pytest.mark.parametrize("body", ["exit 3", "echo 'fatal: bad config line 1' >&2; exit 128"])
def test_a_failing_git_config_call_warns_that_the_derived_files_were_not_added(
        tmp_path, monkeypatch, caplog, body) -> None:
    # L3 r2 (complement LOW): fail-open stays, but it is no longer silent.
    environ = _git_that(tmp_path, monkeypatch, body)
    with caplog.at_level(logging.WARNING, logger="levain.firing.confinement"):
        assert _REAL_RUN_GIT_CONFIG(["--list"], environ) is None
    [msg] = _git_warnings(caplog)
    assert "git config" in msg and "not added" in msg


def test_a_git_that_cannot_run_or_hangs_warns(tmp_path, monkeypatch, caplog) -> None:
    import subprocess
    from levain.consent import push
    environ = _git_that(tmp_path, monkeypatch, "exit 0")
    monkeypatch.setattr(push, "git_bin", lambda: str(tmp_path / "gone" / "git"))
    with caplog.at_level(logging.WARNING, logger="levain.firing.confinement"):
        assert _REAL_RUN_GIT_CONFIG(["--list"], environ) is None
    assert len(_git_warnings(caplog)) == 1

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], 10)

    caplog.clear()
    monkeypatch.setattr(conf.subprocess, "run", hang)
    with caplog.at_level(logging.WARNING, logger="levain.firing.confinement"):
        assert _REAL_RUN_GIT_CONFIG(["--list"], environ) is None
    assert len(_git_warnings(caplog)) == 1


def test_nothing_found_absent_config_or_missing_git_stay_silent(tmp_path, monkeypatch, caplog) -> None:
    # MEASURED on git 2.50.1: a --get / --get-regexp that matches nothing exits 1, and a --list with
    # no global config file at all exits 128; both are every-build normal and lose nothing.
    from levain.consent import push
    environ = _git_that(tmp_path, monkeypatch, "exit 1")
    with caplog.at_level(logging.WARNING, logger="levain.firing.confinement"):
        assert _REAL_RUN_GIT_CONFIG(["--get", "core.attributesFile"], environ) is None
        (tmp_path / "home" / ".gitconfig").unlink()
        _script(tmp_path / "root-owned" / "git", "exit 128")
        assert _REAL_RUN_GIT_CONFIG(["--list"], environ) is None

        def refuse():
            raise push.PushRefused("no root-owned git")

        monkeypatch.setattr(push, "git_bin", refuse)
        assert _REAL_RUN_GIT_CONFIG(["--list"], environ) is None
    assert _git_warnings(caplog) == []


def test_unresolvable_env_git_config_refuses_the_floor(tmp_path, monkeypatch) -> None:
    _clear_git_env(monkeypatch)
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    (xdg / "git").symlink_to(xdg / "git")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    with pytest.raises(conf.ConfinementError, match=r"XDG_CONFIG_HOME.*fail-closed"):
        _policy(tmp_path, monkeypatch)

    _clear_git_env(monkeypatch)
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(loop))
    with pytest.raises(conf.ConfinementError, match=r"GIT_CONFIG_GLOBAL.*fail-closed"):
        conf.build_policy(tmp_path / "entity", workspace=tmp_path / "workspace",
                          deny_standard_creds=False)


def test_required_spellings_refuses_a_loop_and_allows_an_absent_path(tmp_path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    with pytest.raises(conf.ConfinementError, match=r"loop target.*fail-closed"):
        conf._required_spellings(loop, "loop target")
    with pytest.raises(conf.ConfinementError, match=r"under a loop.*fail-closed"):
        conf._required_spellings(loop / "child", "under a loop")

    absent = tmp_path / "absent" / "deeper"
    assert absent in conf._required_spellings(absent, "absent")

    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    assert dangling in conf._required_spellings(dangling, "dangling")


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

    for hop in (workspace / ".codex", workspace / "sub", workspace, dir_a):
        assert hop in policy.persistence_agent_pins
        assert f'(literal "{hop}")' in text
        assert conf.persistence_agent_pin_reason(policy, hop) is not None
    assert f'(subpath "{real}")' in text
    assert conf.crown_jewel_reason(policy, real / "config.toml") is not None


def test_depth_n_symlink_target_and_full_multi_hop_chain_are_pinned(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    hooks = workspace / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    payload = workspace / "payload.sh"
    payload.write_text("SAFE")
    (hooks / "run.sh").symlink_to(Path("../..") / "payload.sh")

    target = workspace / "target"
    target.write_text("SAFE")
    (workspace / "a").symlink_to("b")
    (workspace / "b").symlink_to("target")
    (workspace / ".claude" / "x").symlink_to(Path("..") / "a")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    text = conf.SeatbeltProvider().render_profile(policy)

    assert payload in policy.persistence_agent_files
    assert target in policy.persistence_agent_files
    for link in (hooks / "run.sh", workspace / ".claude" / "x", workspace / "a", workspace / "b"):
        assert link in policy.persistence_agent_pins
        assert conf.persistence_agent_pin_reason(policy, link) is not None
        assert f'(literal "{link}")' in text
    assert conf.crown_jewel_reason(policy, payload) is not None
    assert conf.crown_jewel_reason(policy, target) is not None
    assert conf.crown_jewel_reason(policy, workspace / "payload-sibling.txt") is None


def test_symlink_before_dotdot_is_resolved_in_kernel_order(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    protected = workspace / ".claude" / "x"
    protected.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    true_target = outside / "payload"
    true_target.parent.mkdir()
    (outside / "dir").mkdir()
    true_target.write_text("SAFE")
    decoy = workspace / "payload"
    decoy.write_text("DECOY")
    (workspace / "jump").symlink_to(outside / "dir", target_is_directory=True)
    protected.symlink_to(Path("..") / "jump" / ".." / "payload")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)

    assert true_target in policy.persistence_agent_files
    assert decoy not in policy.persistence_agent_files
    assert workspace / "jump" in policy.persistence_agent_pins
    assert conf.crown_jewel_reason(policy, true_target) is not None


def test_valid_symlink_dag_may_revisit_a_directory_link(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    common = workspace / "common"
    common.mkdir(parents=True)
    final = common / "final"
    final.write_text("SAFE")
    (workspace / "shared").symlink_to(common, target_is_directory=True)
    (common / "base").symlink_to(Path("..") / "shared" / "final")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "cfg").symlink_to(Path("..") / "shared" / "base")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)

    assert final in policy.persistence_agent_files
    assert workspace / "shared" in policy.persistence_agent_pins


def test_absent_agent_roots_still_pin_both_anchors_and_every_root(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)

    for anchor in (home, workspace):
        assert anchor in policy.persistence_agent_pins
        for name in (".claude", ".codex"):
            root = anchor / name
            assert root in policy.persistence_agent_roots
            assert root in policy.persistence_agent_pins
            assert root.parent in policy.persistence_agent_pins


def test_agent_root_existence_check_refuses_non_enoent_errors(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    stock_lstat = conf.os.lstat

    def refusing_lstat(path, *args, **kwargs):
        if Path(path) == workspace / ".claude":
            raise PermissionError(errno.EACCES, "denied", str(path))
        return stock_lstat(path, *args, **kwargs)

    monkeypatch.setattr(conf.os, "lstat", refusing_lstat)
    with pytest.raises(conf.ConfinementError, match="existence cannot be inspected.*denied"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_agent_pins_do_not_turn_the_workspace_into_a_write_denied_subtree(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    ordinary = workspace / "ordinary.txt"

    assert workspace in policy.persistence_agent_pins
    assert workspace not in policy.deny_write_dirs
    assert conf.persistence_agent_pin_reason(policy, workspace) is not None
    assert conf.persistence_agent_pin_reason(policy, ordinary) is None
    assert conf.crown_jewel_reason(policy, workspace) is None
    assert conf.crown_jewel_reason(policy, ordinary) is None
    text = conf.SeatbeltProvider().render_profile(policy)
    full_write_section, pin_section = text.split(";; Resolved-set pins", maxsplit=1)
    assert f'(literal "{workspace}")' not in full_write_section
    assert "(deny file-write-create file-write-unlink" in pin_section
    assert f'(literal "{workspace}")' in pin_section
    ordinary.write_text("editor-control")
    assert ordinary.read_text() == "editor-control"


def test_nested_link_loop_refuses_with_named_reason(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "x").symlink_to(Path("..") / "a")
    (workspace / "a").symlink_to("b")
    (workspace / "b").symlink_to("a")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    with pytest.raises(conf.ConfinementError, match="symlink loop"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_chain_past_40_hops_refuses_with_named_reason(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "x").symlink_to(Path("..") / "hop0")
    for index in range(40):
        (workspace / f"hop{index}").symlink_to(f"hop{index + 1}")
    (workspace / "hop40").write_text("target")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    with pytest.raises(conf.ConfinementError, match="symlink chain past 40 hops"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_final_target_that_contains_workspace_refuses(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "container" / "workspace"
    workspace.mkdir(parents=True)
    home.mkdir()
    (home / ".claude").symlink_to(tmp_path / "container", target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    with pytest.raises(conf.ConfinementError, match="is or contains the workspace"):
        conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)


def test_operator_style_home_links_outside_home_spawn_and_deny_only_targets(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "elsewhere" / "global"
    target_dir.mkdir(parents=True)
    target = target_dir / "CLAUDE.md"
    target.write_text("operator")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").symlink_to(target)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    conf._refuse_multiply_linked_jewels(policy)

    assert target in policy.persistence_agent_files
    assert conf.crown_jewel_reason(policy, target) is not None
    assert conf.crown_jewel_reason(policy, target_dir / "sibling.md") is None


def test_editor_checks_lexical_and_canonical_agent_paths(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.md"
    target.write_text("protected")
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "linked.md").symlink_to(target)
    outside_alias = tmp_path / "outside-alias"
    outside_alias.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)

    assert conf.crown_jewel_reason(policy, alias / ".CLAUDE" / "settings.json") is not None
    assert conf.crown_jewel_reason(policy, outside_alias / "target.md") is not None


def test_depth_n_symlink_target_is_in_the_pre_spawn_inode_set(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    hooks = workspace / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    payload = workspace / "payload.sh"
    payload.write_text("protected")
    (hooks / "run.sh").symlink_to(Path("../..") / "payload.sh")
    os.link(payload, workspace / "payload-alias")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)

    with pytest.raises(conf.ConfinementError, match="crown jewel with 2 names"):
        conf._refuse_multiply_linked_jewels(policy)


@pytest.mark.parametrize("root, nested", [(".claude", False), (".codex", True)])
def test_dangling_agent_link_is_literal_denied_and_does_not_refuse(
    tmp_path, monkeypatch, root, nested
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = workspace / root
    protected.mkdir()
    if nested:
        protected = protected / "tmp"
        protected.mkdir()
    target_dir = tmp_path / f"outside-{root[1:]}"
    target_dir.mkdir()
    missing = target_dir / "missing-target"
    (protected / "dangling").symlink_to(missing)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    text = conf.SeatbeltProvider().render_profile(policy)

    assert missing in policy.persistence_agent_files
    assert target_dir in policy.persistence_agent_pins
    assert f'(literal "{missing}")' in text
    assert conf.crown_jewel_reason(policy, missing) is not None
    assert conf.crown_jewel_reason(policy, target_dir / "sibling") is None


def test_pin_reason_resolves_only_the_parent_dentry(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    real = workspace / "dirA" / "real"
    real.mkdir(parents=True)
    (workspace / "sub").symlink_to(workspace / "dirA", target_is_directory=True)
    (workspace / ".codex").symlink_to(Path("sub") / "real", target_is_directory=True)
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(workspace, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)

    policy = conf.build_policy(entity, workspace=workspace, deny_standard_creds=False)
    assert conf.persistence_agent_pin_reason(policy, alias / "sub") is not None

    self_link = workspace / "ordinary-link"
    self_link.symlink_to(workspace, target_is_directory=True)
    assert conf.persistence_agent_pin_reason(policy, self_link) is None


def test_looping_agent_root_refuses_policy_build(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".codex").symlink_to(workspace / ".codex")
    monkeypatch.setenv("HOME", str(home))
    entity = tmp_path / "entity"
    (entity / ".levain").mkdir(parents=True)
    with pytest.raises(conf.ConfinementError, match="protected persistence path.*symlink loop"):
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

    # The rule guards writing the entry itself, never reading it, and never working INSIDE a home child: a
    # workspace at ~/proj (the common layout) failed str_replace, insert and its own listing ("Invalid `path`").
    os.unlink(workspace / "alias")
    proj = home / "proj"
    proj.mkdir()
    (proj / "main.py").write_text("a\n")
    policy = conf.build_policy(policy.entity_dir, workspace=proj, deny_standard_creds=False)
    assert conf.crown_jewel_reason(policy, top, write=False) is None
    assert conf.crown_jewel_reason(policy, proj) is not None
    assert conf.crown_jewel_reason(policy, proj, container=True) is None
    pytest.importorskip("openhands.tools.file_editor", reason="openhands extra absent")
    from openhands.tools.file_editor import FileEditorAction

    from levain.firing.openhands.tools import CrownJewelsFileEditorExecutor

    editor = CrownJewelsFileEditorExecutor(policy=policy)
    assert not editor(FileEditorAction(command="str_replace", path=str(proj / "main.py"), old_str="a",
                                       new_str="b")).is_error
    assert not editor(FileEditorAction(command="insert", path=str(proj / "main.py"), insert_line=1,
                                       new_str="c")).is_error
    assert not editor(FileEditorAction(command="view", path=str(proj))).is_error
    assert not editor(FileEditorAction(command="view", path=str(top))).is_error
    assert editor(FileEditorAction(command="str_replace", path=str(top), old_str="protected",
                                   new_str="x")).is_error
    assert (proj / "main.py").read_text() == "b\nc\n" and top.read_text() == "protected"


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


def test_an_override_root_with_any_name_renders_and_is_denied(tmp_path, monkeypatch) -> None:
    # RUN 2026-10-19 in a macOS VM (1019-conf/mac-final): CLAUDE_CONFIG_DIR=/tmp/lvA3/lvA_claudecfg
    # refused the whole shell ("invalid protected agent-directory root"). An override is a root
    # whatever its last component is called (~/.config/claude); it must render, as a deny.
    cc = tmp_path / "xdg" / "claude"
    cx = tmp_path / "codex_home+1"
    cc.mkdir(parents=True)
    cx.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    monkeypatch.setenv("CODEX_HOME", str(cx))
    policy = _policy(tmp_path, monkeypatch)
    text = conf.SeatbeltProvider().render_profile(policy)
    for root in (cc, cx):
        real = Path(os.path.realpath(root))
        assert any(r == root or r == real for r in policy.persistence_agent_roots)
        assert f'(regex #"{conf._agent_root_regex(real)}")' in text
    # The '+' in a name is escaped, so it cannot widen the match.
    assert r"\+1($|\/.*)" in text


def test_agent_root_regex_under_slash_and_non_ascii(tmp_path) -> None:
    # L3 r4 (complement, gemini): a root directly under / doubled the separator, so its deny matched nothing.
    assert conf._agent_root_regex(Path("/claudecfg")).startswith(r"^\/[cC]")
    assert conf._agent_root_regex(Path("/Users/a/.claude")).startswith(r"^\/Users\/a\/\.[cC]")
    with pytest.raises(conf.ConfinementError, match="non-ASCII"):
        conf._agent_root_regex(Path("/x/Caf\u00e9"))
    with pytest.raises(conf.ConfinementError):
        conf._agent_root_regex(Path("claude"))

