"""Where the externals are: node, the pinned srt, git. Resolved once; a missing one is a SKIP reason, never a guess."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

SRT_VERSION = "0.0.81"
# Dev-machine fallback ONLY (the spike's install). CI and any other machine set CHD2_SRT_DIST to the
# `.../node_modules/@anthropic-ai/sandbox-runtime` directory (or its dist/sandbox) of an `npm ci --ignore-scripts` install.
_SPIKE_FALLBACK = Path.home() / "Briefcase/levain/project_memory/1024-6/spike/run/node_modules/@anthropic-ai/sandbox-runtime"

HERE = Path(__file__).resolve().parent
GLUE_DIR = HERE / "spike_glue"
TOOLS_DIR = HERE / "tools"


def node_bin() -> str | None:
    return os.environ.get("CHD2_NODE") or shutil.which("node")


def srt_package_dir() -> Path | None:
    raw = os.environ.get("CHD2_SRT_DIST")
    cands = [Path(raw)] if raw else [_SPIKE_FALLBACK]
    for c in cands:
        for pkg in (c, c.parent.parent if c.name == "sandbox" else c):
            if (pkg / "package.json").is_file() and (pkg / "dist/sandbox/http-proxy.js").is_file():
                return pkg
    return None


def srt_version(pkg: Path) -> str | None:
    try:
        return json.loads((pkg / "package.json").read_text()).get("version")
    except (OSError, ValueError):
        return None


def srt_skip_reason() -> str | None:
    """None when node and srt 0.0.81 are present; else why the srt-backed probes cannot run here."""
    if node_bin() is None:
        return "node is not on PATH (set CHD2_NODE)"
    pkg = srt_package_dir()
    if pkg is None:
        return "@anthropic-ai/sandbox-runtime is not installed (set CHD2_SRT_DIST to its package directory)"
    v = srt_version(pkg)
    if v != SRT_VERSION:
        return f"srt {v} is installed, the probes were measured on {SRT_VERSION}"
    return None


def git_backend() -> str | None:
    import subprocess
    git = shutil.which("git")
    if not git:
        return None
    p = Path(subprocess.check_output([git, "--exec-path"], text=True).strip()) / "git-http-backend"
    return str(p) if p.is_file() else None
