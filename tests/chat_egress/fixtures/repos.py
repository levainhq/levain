"""Local git repositories for round 4's smart-HTTP probes, built with `git fast-import` from mkrepos.py's streams."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import env as envmod

PY = sys.executable


def git_env(home: Path) -> dict[str, str]:
    gdir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
    return {"HOME": str(home), "PATH": f"{gdir}:/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0"}


def write_streams(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    if not (out / "big.fi").exists():
        subprocess.run([PY, str(envmod.TOOLS_DIR / "mkrepos.py"), str(out)], check=True, capture_output=True, timeout=300)


def build_bare(repo_root: Path, name: str, stream: Path, home: Path) -> Path:
    """A bare repo <repo_root>/<name>.git whose history is the fast-import stream (HEAD -> refs/heads/main)."""
    dest = repo_root / f"{name}.git"
    if dest.exists():
        shutil.rmtree(dest)
    env = git_env(home)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(dest)], check=True, env=env, capture_output=True)
    with stream.open("rb") as f:
        subprocess.run(["git", "-C", str(dest), "fast-import", "--quiet"], stdin=f, check=True, env=env, capture_output=True,
                       timeout=600)
    return dest


def git_through_proxy(proxy_port: int, ca: Path, home: Path, *args: str, timeout: float = 300, cwd: Path | None = None,
                      version0: bool = False) -> subprocess.CompletedProcess:
    """`git` with no user/system config and no credential helper, through the proxy, trusting the proxy's CA."""
    cfg = ["-c", f"http.proxy=http://127.0.0.1:{proxy_port}", "-c", f"http.sslCAInfo={ca}"]
    if version0:
        cfg += ["-c", "protocol.version=0"]
    return subprocess.run(["git", *cfg, *args], env=git_env(home), capture_output=True, text=True, timeout=timeout, cwd=cwd)
