"""Ledger #2 on Linux: the bwrap plan's step (2p) binds the persistence trees read-only.

RUN 2026-10-19 in the b1c VM (kernel 7.2.6) on main f0bc6fde, before this step existed: confined bash
planted ~/bin, ~/.local/bin, ~/.config/systemd/user, ~/.config/autostart, existing ~/.claude and
~/.codex contents and the workspace's .claude/.codex (levain project_memory/1019-conf/linux/).
These tests read the bwrap argv as data; nothing here runs a sandbox. Linux behaviour is selected by
patching ``platform.system``, which is what the policy code consults.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.firing import confinement as C
from levain.firing.confinement import ConfinementError, build_policy


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in ("XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL",
                *{v for v, _, _ in C._CRED_OVERRIDES}):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(C.platform, "system", lambda: "Linux")
    return h


def _entity(home: Path) -> tuple[Path, Path]:
    d = home / "proj" / "ent"
    (d / ".levain").mkdir(parents=True)
    ws = home / "proj" / "ws"
    ws.mkdir()
    return d, ws


def _ro_binds(argv: list[str]) -> list[str]:
    return [argv[i + 2] for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == argv[i + 2]]


def _first_mask(argv: list[str]) -> int:
    idx = [i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == "/dev/null"]
    return idx[0] if idx else len(argv)


def test_existing_persistence_trees_are_bound_read_only_before_any_mask(home: Path) -> None:
    ent, ws = _entity(home)
    trees = [home / "bin", home / ".local" / "bin", home / ".config" / "systemd" / "user",
             home / ".config" / "autostart", home / ".config" / "fish", home / ".bashrc.d",
             home / ".claude", home / ".codex", ws / ".claude", ws / ".codex"]
    for t in trees:
        t.mkdir(parents=True)
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    for t in trees:
        bound = [r for r in ro if t == Path(r) or t.is_relative_to(Path(r))]
        assert bound, f"{t} is not inside any read-only bind"
        ro_at = next(i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == bound[0])
        assert ro_at < _first_mask(argv), "a host bind after a mask"
    # ~/.config/systemd covers its user/ subdirectory: one bind, not two.
    assert str(home / ".config" / "systemd" / "user") not in ro
    # The workspace itself stays writable.
    assert str(ws) not in ro


def test_an_absent_tree_bash_could_create_is_created_first_and_bound(home: Path) -> None:
    ent, ws = _entity(home)
    (home / ".local").mkdir()            # ~/.local exists, ~/.local/bin does not
    argv, create_first = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    lb, wc = str(home / ".local" / "bin"), str(ws / ".claude")
    assert lb in create_first and lb in _ro_binds(argv)
    assert wc in create_first and wc in _ro_binds(argv)
    # Directly in $HOME, an absent tree cannot be created inside bash (step (0)): no mount, no mkdir.
    hb = str(home / "bin")
    assert hb not in create_first and hb not in argv


def test_link_targets_outside_the_trees_are_bound_read_only(home: Path) -> None:
    ent, ws = _entity(home)
    shared = home / "dotfiles"
    (shared / "agents").mkdir(parents=True)
    (shared / "CLAUDE.md").write_text("x")
    (home / ".claude").mkdir()
    (home / ".claude" / "agents").symlink_to(shared / "agents")
    (home / ".claude" / "CLAUDE.md").symlink_to(shared / "CLAUDE.md")
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    assert str(shared / "agents") in ro
    assert str(shared / "CLAUDE.md") in ro
    # The shared directory itself was not made read-only: only what a link resolves to.
    assert str(shared) not in ro


def test_a_link_hop_in_a_writable_home_directory_has_its_parent_bound_read_only(home: Path) -> None:
    ent, ws = _entity(home)
    real = home / "store" / "claude"
    real.mkdir(parents=True)
    (real / "settings.json").write_text("{}")
    dots = home / "dots"
    dots.mkdir()
    (dots / "claude").symlink_to(real)                       # the middle hop, in a writable dir
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").symlink_to(dots / "claude" / "settings.json")
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    assert str(dots) in ro
    assert str(real / "settings.json") in ro


def test_a_workspace_agent_dir_that_is_a_symlink_refuses_bash(home: Path) -> None:
    ent, ws = _entity(home)
    elsewhere = home / "shared-claude"
    elsewhere.mkdir()
    (ws / ".claude").symlink_to(elsewhere)
    with pytest.raises(ConfinementError, match="the agent.s configuration resolves through"):
        C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))


def test_a_tool_view_inside_a_persistence_tree_binds_its_subdirectories_back_read_only(home: Path) -> None:
    ent, ws = _entity(home)
    (home / ".config" / "git" / "hooks").mkdir(parents=True)
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=True))
    hooks = str(home / ".config" / "git" / "hooks")
    assert ["--ro-bind-try", hooks, hooks] == argv[argv.index(hooks) - 1:argv.index(hooks) + 2]
    assert not [i for i, a in enumerate(argv) if a == "--bind-try" and argv[i + 1] == hooks]


def test_macos_policy_gains_no_linux_subtree(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(C.platform, "system", lambda: "Darwin")
    ent, ws = _entity(home)
    policy = build_policy(ent, workspace=ws, deny_standard_creds=False)
    names = {p.name for p in policy.persistence_write_subtrees}
    assert not names & {"systemd", "autostart", "fish", ".bashrc.d", "environment.d"}


def test_an_absent_ancestor_is_created_before_the_tree_under_it(home: Path) -> None:
    # ~/.local exists, ~/.local/share does not: the pin step creates ~/.local/share first, or the
    # tree's mkdir (no parents) fails and bash is refused. Holds because step (2p) is in argv before
    # step (1) reads it.
    ent, ws = _entity(home)
    (home / ".local").mkdir()
    _, create_first = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    share, units = str(home / ".local" / "share"), str(home / ".local" / "share" / "systemd")
    assert share in create_first and units in create_first
    assert create_first.index(share) < create_first.index(units)


def test_the_agents_config_dir_overrides_are_roots(home: Path, tmp_path: Path, monkeypatch) -> None:
    ent, ws = _entity(home)
    cc, cx = tmp_path / "cc-config", tmp_path / "codex-home"
    cc.mkdir()
    cx.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    monkeypatch.setenv("CODEX_HOME", str(cx))
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    assert str(cc.resolve()) in ro and str(cx.resolve()) in ro


def test_the_hardlink_check_covers_agent_config_surfaces_not_agent_data(home: Path) -> None:
    # RUN 2026-10-19: Claude Code hardlinks its file-history backups and a job's git clone hardlinks
    # its objects; with whole trees in the strict check, bash was refused for every real user.
    ent, ws = _entity(home)
    data = home / ".claude" / "file-history" / "s1" / "abc@v1"
    data.parent.mkdir(parents=True)
    data.write_text("backup")
    (ws / "edited.py").write_text("x")
    data.unlink()
    import os
    os.link(ws / "edited.py", data)
    policy = build_policy(ent, workspace=ws, deny_standard_creds=False)
    C._refuse_multiply_linked_jewels(policy)          # data: no refusal
    hook = home / ".claude" / "hooks" / "pre.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n")
    os.link(hook, ws / "pre-alias")
    policy = build_policy(ent, workspace=ws, deny_standard_creds=False)
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        C._refuse_multiply_linked_jewels(policy)


def test_a_hop_parent_that_would_cover_a_credential_view_refuses_bash(home: Path) -> None:
    # L1 r1 CRITICAL: ~/.config/ai -> ~/dotfiles/ai with ~/.claude/CLAUDE.md -> ~/.config/ai/CLAUDE.md
    # made ~/.config a hop parent; its read-only bind landed over the ~/.config/git view and put the
    # host's ~/.config/git/credentials back in bash's sight.
    ent, ws = _entity(home)
    (home / "dotfiles" / "ai").mkdir(parents=True)
    (home / "dotfiles" / "ai" / "CLAUDE.md").write_text("x")
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "credentials").write_text("https://u:token@host\n")
    (home / ".config" / "ai").symlink_to(home / "dotfiles" / "ai")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").symlink_to(home / ".config" / "ai" / "CLAUDE.md")
    with pytest.raises(ConfinementError, match="would cover"):
        C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=True))


def test_a_hop_parent_that_would_cover_the_entity_store_refuses_bash(home: Path) -> None:
    ent, ws = _entity(home)                                   # entity and workspace under ~/proj
    (home / "dotfiles" / "claude").mkdir(parents=True)
    (home / "dotfiles" / "claude" / "s.json").write_text("{}")
    (home / "proj" / "claude").symlink_to(home / "dotfiles" / "claude")
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").symlink_to(home / "proj" / "claude" / "s.json")
    with pytest.raises(ConfinementError):   # ~/proj holds the workspace: the hop rule refuses
        C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))


def test_a_persistence_subtree_that_is_a_symlink_has_its_parent_bound_read_only(home: Path) -> None:
    # L1 r1 HIGH: ~/.config/fish -> ~/dotfiles/fish; binding only the target left the link itself
    # replaceable in the writable ~/.config.
    ent, ws = _entity(home)
    (home / "dotfiles" / "fish").mkdir(parents=True)
    (home / ".config").mkdir()
    (home / ".config" / "fish").symlink_to(home / "dotfiles" / "fish")
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    assert str(home / ".config") in ro and str(home / "dotfiles" / "fish") in ro


def test_an_absent_agent_dir_in_a_workspace_this_process_cannot_write_refuses_bash(home: Path) -> None:
    # L1 r1 MEDIUM, the hands case: the workspace is the hands user's (operator: read only), so the
    # plan can neither create its .claude nor leave it creatable.
    ent, ws = _entity(home)
    ws.chmod(0o555)
    try:
        with pytest.raises(ConfinementError, match="setup-isolation"):
            C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    finally:
        ws.chmod(0o755)


def test_a_multi_hop_subtree_link_records_every_hop(home: Path) -> None:
    # L3 r1 (gemini MED): ~/.config/fish -> ~/d/a/fish with ~/d/a -> ~/d/b: the middle hop ~/d/a
    # sits in the writable ~/d (a hop directly in $HOME would be frozen by step (0)).
    ent, ws = _entity(home)
    (home / "d" / "b" / "fish").mkdir(parents=True)
    (home / "d" / "a").symlink_to(home / "d" / "b")
    (home / ".config").mkdir()
    (home / ".config" / "fish").symlink_to(home / "d" / "a" / "fish")
    argv, _ = C._bwrap_plan(build_policy(ent, workspace=ws, deny_standard_creds=False))
    ro = _ro_binds(argv)
    assert str(home / ".config") in ro and str(home / "d") in ro


def test_the_hardlink_check_follows_links_inside_config_subdirs_and_reads_memory(home: Path) -> None:
    import os
    ent, ws = _entity(home)
    cache = home / ".claude" / "cache" / "skill-v1"
    cache.mkdir(parents=True)
    (cache / "SKILL.md").write_text("x")
    (home / ".claude" / "skills").mkdir()
    (home / ".claude" / "skills" / "active").symlink_to(cache)
    os.link(cache / "SKILL.md", ws / "skill-alias")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        C._refuse_multiply_linked_jewels(build_policy(ent, workspace=ws, deny_standard_creds=False))
    (ws / "skill-alias").unlink()
    mem = home / ".claude" / "projects" / "-p" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("rule")
    os.link(mem / "MEMORY.md", ws / "mem-alias")
    with pytest.raises(ConfinementError, match="crown jewel with 2 names"):
        C._refuse_multiply_linked_jewels(build_policy(ent, workspace=ws, deny_standard_creds=False))
