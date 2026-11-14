"""A read of a retained repo that leaves ``.git/index`` as END left it.

``git status`` refreshes stat data and rewrites ``.git/index`` when a file's mtime/ctime moved (a copy, a restore, a
guest reboot). ``session adopt`` walks the retained tree against END's manifest, so an observer that rewrites the index
after END makes the product (correctly) answer ``refused walk-digest-mismatch: repo/.git/index``. This module reads the
status from a COPY of the index (``GIT_INDEX_FILE``), with ``--no-optional-locks``, so the retained repo is never
written. The vmend rig's ``observe`` (outside this repo) should call :func:`readonly_status` instead of running
``git status --short`` on the repo directly."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence


def readonly_status_argv(git: str, repo: str, *, safe_directory: bool = True) -> list[str]:
    pre = ["-c", "safe.directory=*"] if safe_directory else []
    return [git, "--no-optional-locks", *pre, "-C", repo, "status", "--short"]


def readonly_status(git: str, repo: str | Path, *, timeout: float = 60, safe_directory: bool = True) -> str:
    """``git status --short`` of ``repo`` computed on a throwaway copy of its index; the repo's own ``.git/index`` is
    opened for reading once (copied) and never written."""
    repo = Path(repo)
    idx = repo / ".git" / "index"
    with tempfile.TemporaryDirectory(prefix="ro-observe-") as td:
        env = dict(os.environ)
        if idx.is_file():
            shutil.copyfile(idx, Path(td) / "index")
            env["GIT_INDEX_FILE"] = str(Path(td) / "index")
        argv: Sequence[str] = readonly_status_argv(git, str(repo), safe_directory=safe_directory)
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return cp.stdout if cp.returncode == 0 else cp.stdout + cp.stderr
