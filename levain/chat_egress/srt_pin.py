"""srt on this machine: `levain chat setup`'s install, its vendor check, and what `levain doctor` reads (P-5,
R15-10-R2 (4); CH-D2 S8).

srt (`@anthropic-ai/sandbox-runtime`) is pinned by the lockfile shipped beside the glue (`node/package-lock.json`: an
exact version and a sha512 integrity per package). :func:`setup` copies `package.json` + the lock into a Levain-owned
prefix under the levain home (`$LEVAIN_HOME/chat-node`, else `~/.levain/chat-node`; never global) and runs
`npm ci --ignore-scripts` there (npm checks every tarball against the lock's integrity), then the install-time VENDOR
CHECK: every module the glue deep-imports from srt's `dist/sandbox` and every name it takes from each must still be
there, read from the glue's own import lines (:func:`glue_imports`, the one home of that list) and matched against
each module's `export` statements without running them. Then it writes a stamp: the shipped lock's sha256 and a
sha256 over srt's installed files.

:func:`check` is what doctor and a launch read: the stamp present, the lock it was made from still the shipped one,
npm's own record (`node_modules/.package-lock.json`) naming the shipped integrity for every package, the tree hash
unchanged, the version `launch.SRT_VERSION`, and the vendor check passing. Any mismatch is a refusal naming it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["NODE_DIR", "SRT_PKG", "SrtProblem", "check", "default_prefix", "glue_imports", "setup", "srt_dist",
           "vendor_check"]

NODE_DIR = Path(__file__).resolve().parent / "node"
SRT_PKG = "@anthropic-ai/sandbox-runtime"
STAMP = "levain-srt.json"
NPM_TIMEOUT_S = 600.0

_GLUE_IMPORT = re.compile(r"const \{([^}]*)\} = await import\(SRT \+ '([A-Za-z0-9_.-]+\.js)'\);")


class SrtProblem(Exception):
    """srt is not installed as the shipped lock pins it, or its import names moved: chat network refuses."""


def default_prefix() -> Path:
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "chat-node").resolve()


def srt_package(prefix: Path) -> Path:
    return Path(prefix) / "node_modules" / SRT_PKG


def srt_dist(prefix: Path) -> Path:
    """The `dist/sandbox` directory `launch.start_egress(srt_dist=...)` takes."""
    return srt_package(prefix) / "dist" / "sandbox"


def glue_imports(glue: Path | None = None) -> dict[str, tuple[str, ...]]:
    """{srt module file: the names the glue takes from it}, read from the glue's `await import(SRT + '...')` lines."""
    from levain.chat_egress.launch import GLUE

    out: dict[str, tuple[str, ...]] = {}
    for names, mod in _GLUE_IMPORT.findall(Path(glue or GLUE).read_text()):
        out[mod] = tuple(n.strip() for n in names.split(",") if n.strip())
    return out


def _exports(source: str, name: str) -> bool:
    n = re.escape(name)
    return bool(re.search(rf"^export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+{n}\b", source, re.M)
                or any(re.search(rf"(?:^|[\s,]){n}(?:\s+as\s+\w+)?\s*(?:,|$)", body)
                       for body in re.findall(r"^export\s*\{([^}]*)\}", source, re.M)))


def vendor_check(dist: Path, imports: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    """Every glue import srt no longer provides, as `module: name` (empty: the check passes)."""
    missing = []
    for mod, names in (imports if imports is not None else glue_imports()).items():
        try:
            src = (Path(dist) / mod).read_text()
        except OSError:
            missing.append(f"{mod}: (module missing)")
            continue
        missing += [f"{mod}: {n}" for n in names if not _exports(src, n)]
    return missing


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_hash(pkg: Path) -> str:
    """sha256 over srt's installed files: each regular file's relative path and its bytes, in path order."""
    h = hashlib.sha256()
    for p in sorted(q for q in Path(pkg).rglob("*") if q.is_file() and not q.is_symlink()):
        h.update(p.relative_to(pkg).as_posix().encode() + b"\0")
        h.update(_sha256(p).encode() + b"\n")
    return h.hexdigest()


@dataclass(frozen=True)
class SrtState:
    prefix: Path
    version: str
    dist: Path


def check(prefix: Path | None = None) -> SrtState:
    """The installed srt, verified (see the module doc); else SrtProblem naming the first mismatch."""
    from levain.chat_egress.launch import SRT_VERSION

    prefix = Path(prefix or default_prefix())
    pkg = srt_package(prefix)
    try:
        stamp = json.loads((prefix / STAMP).read_text())
    except (OSError, ValueError):
        raise SrtProblem(f"chat network is not set up at {prefix} (run `levain chat setup`)") from None
    shipped = json.loads((NODE_DIR / "package-lock.json").read_text())
    if stamp.get("lock_sha256") != _sha256(NODE_DIR / "package-lock.json"):
        raise SrtProblem("srt was installed from another lock than this Levain ships (run `levain chat setup`)")
    try:
        npm_record = json.loads((prefix / "node_modules" / ".package-lock.json").read_text())["packages"]
    except (OSError, ValueError, KeyError, TypeError):
        raise SrtProblem(f"npm's install record is missing under {prefix}") from None
    for path, entry in shipped["packages"].items():
        if path and (npm_record.get(path) or {}).get("integrity") != entry["integrity"]:
            raise SrtProblem(f"hash mismatch: {path} is not the integrity the shipped lock pins")
    if stamp.get("tree_sha256") != tree_hash(pkg):
        raise SrtProblem(f"hash mismatch: srt's files under {pkg} changed since `levain chat setup`")
    try:
        version = json.loads((pkg / "package.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        version = None
    if version != SRT_VERSION:
        raise SrtProblem(f"srt {version!r} is installed; chat network needs {SRT_VERSION}")
    missing = vendor_check(srt_dist(prefix))
    if missing:
        raise SrtProblem(f"srt's import names moved: {', '.join(missing)}")
    return SrtState(prefix=prefix, version=version, dist=srt_dist(prefix))


def setup(prefix: Path | None = None, *, npm: str | None = None, offline: bool = False,
          cache: Path | None = None) -> SrtState:
    """`levain chat setup`: `npm ci --ignore-scripts` of the shipped lock into `prefix`, the vendor check, the stamp."""
    prefix = Path(prefix or default_prefix())
    npm = npm or shutil.which("npm")
    if npm is None:
        raise SrtProblem("chat network needs npm (with Node 26) on PATH")
    prefix.mkdir(mode=0o700, parents=True, exist_ok=True)
    (prefix / STAMP).unlink(missing_ok=True)                 # a failed setup leaves no stamp a check would accept
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(NODE_DIR / name, prefix / name)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "USER", "LANG", "TMPDIR")}
    argv = [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund", "--prefix", str(prefix)]
    try:
        out = subprocess.run(argv + (["--offline"] if offline else []) + (["--cache", str(cache)] if cache else []),
                             cwd=prefix, env=env, capture_output=True,
                             text=True, timeout=NPM_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SrtProblem(f"npm ci did not run: {type(exc).__name__}") from None
    if out.returncode != 0:
        raise SrtProblem(f"npm ci failed (exit {out.returncode}): {out.stderr.strip()[-400:]}")
    missing = vendor_check(srt_dist(prefix))
    if missing:
        raise SrtProblem(f"srt's import names moved: {', '.join(missing)}")
    (prefix / STAMP).write_text(json.dumps({"lock_sha256": _sha256(NODE_DIR / "package-lock.json"),
                                            "tree_sha256": tree_hash(srt_package(prefix))}, sort_keys=True) + "\n")
    return check(prefix)
