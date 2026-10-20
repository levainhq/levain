"""Text-only tests for the defensive macOS socket and persistence floor.

Nothing in this module starts the Seatbelt driver or applies a profile.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
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
    for path in policy.persistence_agent_pins:
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



def _walk_tree(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """An agent root with one subdirectory holding one file, for racing the protected-path walk."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    sub = workspace / ".claude" / "sub"
    sub.mkdir(parents=True)
    (sub / "child.json").write_text("{}")
    return home, workspace, sub


def _vanish_once(monkeypatch, name: str, trigger: Path, act) -> None:
    """Patch ``os.<name>`` so its first call on ``trigger`` runs ``act`` before the real call."""
    stock = getattr(conf.os, name)
    fired: list[bool] = []

    def racing(path, *args, **kwargs):
        if not fired and Path(path) == trigger:
            fired.append(True)
            act()
        return stock(path, *args, **kwargs)

    monkeypatch.setattr(conf.os, name, racing)


@pytest.mark.parametrize("name", ["stat", "scandir"])
def test_walk_skips_a_directory_that_vanishes_after_being_queued(tmp_path, monkeypatch, name) -> None:
    home, workspace, sub = _walk_tree(tmp_path, monkeypatch)
    _vanish_once(monkeypatch, name, sub, lambda: shutil.rmtree(sub))
    _, dirs, files, _ = conf._persistence_agent_paths(home, workspace)
    assert workspace / ".claude" in dirs
    assert sub not in dirs and sub not in files


def test_walk_skips_a_child_that_vanishes_between_scandir_and_lstat(tmp_path, monkeypatch) -> None:
    home, workspace, sub = _walk_tree(tmp_path, monkeypatch)
    child = sub / "child.json"
    _vanish_once(monkeypatch, "lstat", child, child.unlink)
    _, dirs, _, _ = conf._persistence_agent_paths(home, workspace)
    assert workspace / ".claude" in dirs


def test_walk_treats_a_directory_replaced_by_a_file_as_a_file_target(tmp_path, monkeypatch) -> None:
    home, workspace, sub = _walk_tree(tmp_path, monkeypatch)

    def replace() -> None:
        shutil.rmtree(sub)
        sub.write_text("now a file")

    _vanish_once(monkeypatch, "scandir", sub, replace)
    _, _, files, _ = conf._persistence_agent_paths(home, workspace)
    assert sub in files


@pytest.mark.parametrize(("name", "reason"), [
    ("stat", "unresolvable directory target"),
    ("scandir", "cannot be walked"),
    ("lstat", "cannot be inspected"),
])
def test_walk_still_refuses_non_enoent_errors(tmp_path, monkeypatch, name, reason) -> None:
    home, workspace, sub = _walk_tree(tmp_path, monkeypatch)
    trigger = sub / "child.json" if name == "lstat" else sub

    def deny() -> None:
        raise PermissionError(errno.EACCES, "denied", str(trigger))

    _vanish_once(monkeypatch, name, trigger, deny)
    with pytest.raises(conf.ConfinementError, match=f"{reason}.*denied"):
        conf._persistence_agent_paths(home, workspace)
