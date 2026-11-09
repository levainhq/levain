"""The public repository tracks nothing under ``project_memory/``.

``project_memory/`` is the seats' working notes (review logs, handoffs, local paths). ``.gitignore`` lists it, but a
``git add -f`` gets past an ignore rule, so the rule is held here: CI fails the moment a file under it is tracked.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git_env() -> dict:
    # a GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE in the environment would point ls-files at another repository
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _tracked(pathspec: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", pathspec], env=_git_env(),
                         check=True, capture_output=True, text=True, timeout=60).stdout
    return [p for p in out.split("\0") if p]


def test_no_file_under_project_memory_is_tracked() -> None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        if os.environ.get("CI"):
            pytest.fail("this guard needs git and a checkout with .git in CI; it must not skip there")
        pytest.skip("needs a git checkout")
    # no trailing slash: matches an entry named exactly project_memory (a symlink or gitlink) and everything under it
    tracked = _tracked("project_memory")
    assert tracked == [], f"project_memory is never tracked in this public repo; untrack: {tracked[:10]}"
