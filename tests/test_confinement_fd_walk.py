"""The persistence-agent walk is descriptor-relative: depth beyond the path limit is not a refusal.

Every tree here is synthetic and lives under ``tmp_path``; HOME points there too, so nothing in
this module walks the operator's real agent configuration.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from levain.firing import confinement as conf

# Deeper than PATH_MAX on both macOS (1024) and Linux (4096).
_DEEP_LEVELS = 25
_DEEP_NAME = "x" * 200


@pytest.fixture
def synthetic_home(tmp_path, monkeypatch) -> tuple[Path, Path, Path]:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    for d in (home / ".claude", workspace, outside):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return home, workspace, outside


def _build_deep_chain(parent: Path) -> None:
    """Nest directories past the path limit, each made relative to its parent's descriptor."""
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(_DEEP_LEVELS):
            os.mkdir(_DEEP_NAME, dir_fd=fd)
            child = os.open(_DEEP_NAME, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.close(os.open("leaf", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    finally:
        os.close(fd)


def test_walk_builds_the_floor_through_a_tree_deeper_than_the_path_limit(synthetic_home) -> None:
    # Reproduced on main before the descriptor walk: ``[Errno 63] File name too long`` refused the
    # whole floor because each entry was inspected by its absolute path.
    assert shutil.rmtree.avoids_symlink_attacks  # the descriptor-based rmtree, which handles depth
    home, workspace, outside = synthetic_home
    jobs = home / ".claude" / "jobs"
    jobs.mkdir()
    (home / ".claude" / "skills").mkdir()
    (home / ".claude" / "skills" / "ext").symlink_to(outside)
    try:
        _build_deep_chain(jobs)
        roots, dirs, _, pins = conf._persistence_agent_paths(home, workspace)
    finally:
        shutil.rmtree(jobs)
    assert home / ".claude" in roots
    assert Path(os.path.realpath(outside)) in dirs
    assert home / ".claude" / "skills" / "ext" in pins


def test_walk_still_denies_the_target_of_a_deep_outward_symlink(synthetic_home) -> None:
    home, workspace, outside = synthetic_home
    deep = home / ".claude" / "projects" / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    (outside / "inner").mkdir()
    farther = outside.parent / "farther.json"
    farther.write_text("{}")
    (outside / "inner" / "next.json").symlink_to(farther)
    link = deep / "out"
    link.symlink_to(outside)

    _, dirs, files, pins = conf._persistence_agent_paths(home, workspace)

    assert Path(os.path.realpath(outside)) in dirs
    assert link in pins
    # The outward target is itself walked: a link inside it is followed in turn.
    assert Path(os.path.realpath(farther)) in files
    assert Path(os.path.realpath(outside)) / "inner" / "next.json" in pins
