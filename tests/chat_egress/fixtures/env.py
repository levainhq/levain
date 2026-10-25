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


#: set once per session by conftest's `_srt_prefix_factory` (pytest's tmp_path_factory); `srt_prefix` builds under it
_FACTORY = None
_PREFIX: list[Path] = []


def srt_prefix() -> Path:
    """A Levain-shaped srt install `srt_pin.check` accepts, built once per session: the real installed tree that
    `srt_package_dir` names (its whole `node_modules`), copied beside the shipped package.json + lock, and stamped by
    `srt_pin.write_stamp` (setup's own last step). A test that changes it works on its own copy."""
    if not _PREFIX:
        from levain.chat_egress import srt_pin

        pkg = srt_package_dir()
        prefix = _FACTORY.mktemp("srt-prefix") / "chat-node"
        shutil.copytree(pkg.parents[1], prefix / "node_modules", symlinks=True)     # <prefix>/node_modules/@scope/pkg
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(srt_pin.NODE_DIR / name, prefix / name)
        srt_pin.write_stamp(prefix)
        srt_pin.check(prefix)
        _PREFIX.append(prefix)
    return _PREFIX[0]


def srt_prefix_copy(dest: Path) -> Path:
    """Its own copy of `srt_prefix()` at `dest`, for a test that changes files in it."""
    shutil.copytree(srt_prefix(), dest, symlinks=True)
    return dest


def git_backend() -> str | None:
    import subprocess
    git = shutil.which("git")
    if not git:
        return None
    p = Path(subprocess.check_output([git, "--exec-path"], text=True).strip()) / "git-http-backend"
    return str(p) if p.is_file() else None
