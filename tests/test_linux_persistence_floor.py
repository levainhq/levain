"""Ledger #2 on Linux: the bwrap plan's step (2p) binds the persistence trees read-only.

RUN 2026-10-19 in the b1c VM (kernel 7.2.6) on main f0bc6fde, before this step existed: confined bash
planted ~/bin, ~/.local/bin, ~/.config/systemd/user, ~/.config/autostart, existing ~/.claude and
~/.codex contents and the workspace's .claude/.codex (project_memory/1019-conf/linux/RESULT.md).
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
        assert argv.index(bound[0]) < _first_mask(argv), "a host bind after a mask"
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
    with pytest.raises(ConfinementError, match="Replace the symlink"):
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
