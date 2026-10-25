"""srt on this machine: `levain chat setup`'s install, its vendor check, and what `levain doctor` reads (P-5,
R15-10-R2 (4); CH-D2 S8).

srt (`@anthropic-ai/sandbox-runtime`) is pinned by the lockfile shipped beside the glue (`node/package-lock.json`: an
exact version and a sha512 integrity per package). :func:`setup` copies `package.json` + the lock into a Levain-owned
prefix under the levain home (`$LEVAIN_HOME/chat-node`, else `~/.levain/chat-node`; never global) and runs
`npm ci --ignore-scripts` there (npm checks every tarball against the lock's integrity), then the install-time VENDOR
CHECK: every module the glue deep-imports from srt's `dist/sandbox` and every name it takes from each must still be
there, read from the glue's own import lines (:func:`glue_imports`, the one home of that list) and matched against
each module's `export` statements without running them. Then it writes a stamp: the shipped lock's sha256 and
:func:`tree_hashes` of what is installed (one hash per package the lock names, one over everything else under
`node_modules`), so the whole tree the glue loads is covered, srt's dependencies (node-forge holds the CA key) included.

:func:`check` is what doctor and a launch read: the stamp present, the lock it was made from still the shipped one,
npm's own record (`node_modules/.package-lock.json`) naming the shipped integrity for every package, every tree hash
unchanged, the version `launch.SRT_VERSION`, and the vendor check passing. Any mismatch is a refusal naming it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
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
    """srt's `dist/sandbox` directory under `prefix` (the glue's `--srt-dist`)."""
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


#: the tree-hash key of every entry under `node_modules` that no locked package holds (npm's record, `.bin`, and
#: anything planted where Node resolves a name before the locked package, e.g. `node_modules/@scope/node_modules/x`)
OUTSIDE = "(outside every package the lock names)"


def locked_packages() -> list[str]:
    """The package paths the shipped lock names (`node_modules/...`), in the lock's order."""
    return [k for k in json.loads((NODE_DIR / "package-lock.json").read_text())["packages"] if k]


def tree_hashes(prefix: Path, packages: list[str]) -> dict[str, str]:
    """{package path: sha256 over its entries} for each of `packages`, plus `OUTSIDE` over every other entry under
    `<prefix>/node_modules`. An entry is its path relative to `prefix` and what it is: a file's sha256, a symlink's
    target text (never followed), a directory's mark. Each entry belongs to the longest package path holding it, so a
    nested package's files are its own."""
    prefix = Path(prefix)
    owners = sorted(packages, key=len, reverse=True)
    entries: dict[str, list[bytes]] = {p: [] for p in [*packages, OUTSIDE]}
    for top, dirs, files in os.walk(prefix / "node_modules"):
        for name in dirs + files:
            path = Path(top) / name
            rel = path.relative_to(prefix).as_posix()
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                what = b"L" + os.fsencode(os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                what = b"F" + _sha256(path).encode()
            elif stat.S_ISDIR(st.st_mode):
                what = b"D"
            else:
                what = b"?" + oct(stat.S_IFMT(st.st_mode)).encode()
            owner = next((p for p in owners if rel.startswith(p + "/")), OUTSIDE)
            entries[owner].append(rel.encode() + b"\0" + what + b"\n")
    return {p: hashlib.sha256(b"".join(sorted(lines))).hexdigest() for p, lines in entries.items()}


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
    packages = locked_packages()
    recorded = stamp.get("trees")
    if not isinstance(recorded, dict) or set(recorded) != {*packages, OUTSIDE}:
        raise SrtProblem("the setup stamp does not record every package the shipped lock names "
                         "(run `levain chat setup`)")
    now = tree_hashes(prefix, packages)
    for path in [*packages, OUTSIDE]:
        if recorded[path] != now[path]:
            where = ("a file under node_modules outside every package the lock names" if path == OUTSIDE
                     else f"{path}'s files")
            raise SrtProblem(f"hash mismatch: {where} changed since `levain chat setup`")
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
    write_stamp(prefix)
    return check(prefix)


def write_stamp(prefix: Path) -> None:
    """The stamp `check` verifies against, over what is installed at `prefix` now (setup's last step)."""
    (Path(prefix) / STAMP).write_text(json.dumps({"lock_sha256": _sha256(NODE_DIR / "package-lock.json"),
                                                  "trees": tree_hashes(prefix, locked_packages())},
                                                 sort_keys=True) + "\n")
