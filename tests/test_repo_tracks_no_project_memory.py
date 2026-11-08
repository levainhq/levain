"""The public repository tracks nothing under ``project_memory/``.

``project_memory/`` is the seats' working notes (review logs, handoffs, local paths). ``.gitignore`` lists it, but a
``git add -f`` gets past an ignore rule, so the rule is held here: CI fails the moment a file under it is tracked.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tracked(prefix: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", prefix],
                         check=True, capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="needs a git checkout")
def test_no_file_under_project_memory_is_tracked() -> None:
    tracked = _tracked("project_memory/")
    assert tracked == [], f"project_memory/ is never tracked in this public repo; untrack: {tracked[:10]}"
