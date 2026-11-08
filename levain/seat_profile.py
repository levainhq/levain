"""The terminal seat profile builder (PATHZERO Z1b): everything one terminal Claude Code seat is launched with.

A terminal seat works in ONE lane clone plus its RETURN directory. The sandbox bounds Bash at the syscall; the file
tools are confined to those two roots by ``--restricted`` and kept off every protected name by ``permissions.deny``; the seat loads no settings, hooks or MCP
from its roots (the source list, :data:`levain.host_guard.SEAT_SOURCE_ARGS`), so a write that slips the deny lands
in a file nothing reads. The D9 ruling about the file tools lives with ``CC_SEAT_TOOLS`` in ``levain.host_guard``
and is not restated here. ``levain.coordinator.launch`` keeps its own ``project,local`` layers for the coordinator;
this profile drops them on purpose, a different fact with its own home.

Nothing here launches a seat or writes outside the roots it is handed. ``build_seat_profile`` is the ONE builder:
the canary (``lease=None`` under a fixed test root) and the lease launch both come through it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain import host_guard as hg
from levain import seat_gh
from levain.authority import SEAT_EGRESS_KIND, SEAT_LEASE_KIND
from levain.host_guard import (CC_SEAT_TOOLS, PROTECTED_NAMES, SEAT_SOURCE_ARGS, HostRefusal)

__all__ = ["SeatRefusal", "SeatProfile", "seat_deny_write", "seat_permission_deny", "build_seat_profile",
           "exempt_permission_deny", "tracked_at_base", "TRACKED_EXEMPT_NAMES",
           "CANARY_DIRNAME", "FILE_TOOLS", "SEAT_LEASE_KIND", "GH_SERVER_NAME", "GH_TOOL_ALLOW", "gh_mcp_config",
           "real_levain_home"]

CANARY_DIRNAME = "levain-seat-canary"
#: The one MCP server a seat loads (PATHZERO Z2b): it holds the GitHub read tokens host-side and has two fixed verbs.
GH_SERVER_NAME = "levain_gh"
GH_TOOL_ALLOW = (f"mcp__{GH_SERVER_NAME}__gh_read", f"mcp__{GH_SERVER_NAME}__git_fetch")


def gh_mcp_config(python: str, levain_home: Path | str) -> dict[str, Any]:
    """The seat's ``--mcp-config``: exactly the ``levain_gh`` stdio server and no other. Its arguments carry the levain
    home (so it finds the token directory) and no secret; ``-EP`` keeps the PYTHON* variables and the cwd off its path."""
    return {"mcpServers": {GH_SERVER_NAME: {"type": "stdio", "command": python,
                                            "args": ["-EP", "-B", "-m", "levain.seat_gh_mcp", "--home", str(levain_home)]}}}
#: All four file tools are named in every deny, so a later re-admission inherits it. Measured 11-06 on 2.1.319: an
#: ``Edit(...)`` rule refuses the Write tool and a ``Write(...)`` rule alone did NOT, so the Edit rules carry the deny.
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_GIT_DENY = ("hooks", "config", "info", "modules", "worktrees")
_GIT_DENY_FILES = frozenset({"config"})   # a file under .git: its rule names it, with no /** (a pattern ending /** does not match a file)


def _git_rule_path(prefix: str, sub: str) -> str:
    return f"{prefix}/{sub}" if sub in _GIT_DENY_FILES else f"{prefix}/{sub}/**"
_WALK_CAP = 200_000
_HOOK_TIMEOUT = 30


def real_levain_home(environ: Mapping[str, str]) -> Path:
    """The levain home ``environ`` names (``$LEVAIN_HOME``, else ``~/.levain``), absolute: the one
    ``levain seat gh-token set`` writes under. Never a temp directory."""
    from levain.consent.queue import queue_dir_for
    return queue_dir_for(environ).parent


class SeatRefusal(HostRefusal):
    """A seat profile is refused. ``str()`` is the reason shown to the operator."""


def _real(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(p))


def seat_deny_write(root: Path, *, git: bool) -> list[str]:
    """``sandbox.filesystem.denyWrite`` for one root. A lane clone (``git=True``) denies only the git paths that run
    code or carry configuration, never ``.git`` whole, so ``git commit`` works; a RETURN directory has no ``.git``.
    The rest is derived from :data:`PROTECTED_NAMES`, never retyped."""
    root = Path(root)
    out = [str(root / name) for name in sorted(PROTECTED_NAMES - {".git"})]
    if git:
        out += [str(root / ".git" / sub) for sub in _GIT_DENY]
    return out


def _abs_rule(tool: str, path: str) -> str:
    return f"{tool}(//{path.lstrip('/')})"


def exempt_permission_deny(clone: Path, exempt: Sequence[str]) -> list[str]:
    """The file-tool deny rules for each tracked-exempt directory (:data:`TRACKED_EXEMPT_NAMES`): all four tools, the
    absolute spelling and the cwd-relative one, the same two spellings :func:`seat_permission_deny` gives the clone."""
    rules: list[str] = []
    for path in exempt:
        rel = Path(path).relative_to(clone).as_posix()
        for tool in FILE_TOOLS:
            rules += [_abs_rule(tool, f"{path}/**"), f"{tool}({rel}/**)"]
    return rules


def seat_permission_deny(roots: Sequence[Path], key_dir: Path,
                         gh_dir: Path | Sequence[Path] | None = None) -> list[str]:
    """``permissions.deny``: all four file tools on every protected pattern of every root, and the Read tool on the
    launcher key directory. The first root (the clone, the working directory) also gets the cwd-relative spellings,
    which are the ones measured to fire after symlink resolution. ``.git`` is denied only at the paths that run code or
    carry configuration (:data:`_GIT_DENY`), never whole and never as ``**/.git``: Claude Code turns a file-tool deny
    rule into a sandbox write denial, so a whole-``.git`` rule made ``git commit`` fail with ``index.lock: Operation
    not permitted`` (1106-16/z1e/RETURN_gitdeny.md, measured). A NESTED ``.git`` (a gitfile or directory below a
    root's top level) is denied by ``<root>/*/**/.git``, a shape that never matches the root's own ``.git`` (measured on
    the host, one turn each: with it ``git commit`` lands and a Write to ``sub/.git`` is "denied by your permission
    settings"; the z1e rerun without it prompted on that Write). A Write to a ``.git`` file of the root outside the
    :data:`_GIT_DENY` paths still reaches the vendor's own "sensitive file" prompt instead of a hard deny."""
    rules: list[str] = []
    names = sorted(PROTECTED_NAMES - {".mcp.json", ".git"})
    for i, root in enumerate(roots):
        pats = [f"{root}/{name}/**" for name in names]
        pats += [_git_rule_path(f"{root}/.git", sub) for sub in _GIT_DENY]
        pats += [f"{root}/.mcp.json", f"{root}/*/**/.git"]
        for tool in FILE_TOOLS:
            rules += [_abs_rule(tool, p) for p in pats]
            if i == 0:
                rules += [f"{tool}({name}/**)" for name in names]
                rules += [f"{tool}({_git_rule_path('.git', sub)})" for sub in _GIT_DENY]
                rules += [f"{tool}(.mcp.json)", f"{tool}(*/**/.git)"]
    rules.append(_abs_rule("Read", f"{key_dir}/**"))
    gh_dirs = [] if gh_dir is None else [gh_dir] if isinstance(gh_dir, (str, os.PathLike)) else list(gh_dir)
    for d in gh_dirs:                           # the read-token directory (Z2b): one more member of the same deny
        rules.append(_abs_rule("Read", f"{d}/**"))
    return rules


@dataclass(frozen=True)
class SeatProfile:
    """Everything a terminal Claude Code spawn passes, built once per launch."""

    clone: Path
    return_dir: Path
    roots: tuple[Path, ...]
    claude_bin: str
    settings: Mapping[str, Any]
    mcp_config: Mapping[str, Any]
    env: Mapping[str, str]
    tools: tuple[str, ...] = CC_SEAT_TOOLS

    def argv(self, *, headless: bool = False, model: str | None = None) -> list[str]:
        argv = [self.claude_bin]
        if headless:
            argv += ["-p", "--output-format", "stream-json", "--verbose", "--include-hook-events"]
        # --restricted (L3 z1 r1, codex 2), measured headless on 2.1.319 (1106-16/z1/L3_z1_r1_triage.md): without it a
        # Write outside the working directories came back "requested permissions ... you haven't granted it yet" (a
        # prompt a person could approve); with it, "is outside <cwd>, <RETURN>; --restricted confines the file tools
        # to the working directory" (a refusal). additionalDirectories from --settings counted as a working directory,
        # Bash named in --tools stayed, and the --settings hooks fired. The interactive start is not yet measured.
        argv += [*SEAT_SOURCE_ARGS, "--restricted",
                 "--permission-mode", "acceptEdits",
                 "--tools", ",".join(self.tools),
                 "--strict-mcp-config", "--mcp-config", json.dumps(self.mcp_config, sort_keys=True),
                 "--settings", json.dumps(self.settings, sort_keys=True)]
        if model:
            argv += ["--model", model]
        return argv

    def digest(self) -> str:
        """What a canary verdict and a mint row are bound to: the argv, every env value, the roots and the claude
        binary's identity (device, inode, size, mtime), the same binding as ``CCHostProfile.digest``."""
        try:
            st = os.stat(_real(self.claude_bin))
            binary: list[int] = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
        except OSError:
            binary = []
        blob = json.dumps([self.argv(), self.argv(headless=True), sorted(self.env.items()), binary,
                           [str(r) for r in self.roots]], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def _canary_prefix(environ: Mapping[str, str]) -> Path:
    return _real(environ.get("TMPDIR") or tempfile.gettempdir()) / CANARY_DIRNAME


def ensure_canary_prefix(environ: Mapping[str, str]) -> Path:
    """The canary prefix, made 0700 if absent, and refused unless it is a real directory of this user that nobody
    else can write: on a shared ``/tmp`` another user could make it first and swap a canary root inside it."""
    prefix = _canary_prefix(environ)
    try:
        prefix.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise SeatRefusal(f"the canary prefix {str(prefix)!r} cannot be made ({exc.strerror})") from None
    st = os.lstat(prefix)
    if not os.path.isdir(prefix) or os.path.islink(prefix) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise SeatRefusal(f"the canary prefix {str(prefix)!r} is not a private directory of this user")
    return prefix


#: The protected names a lane may hold below its top level when git tracks them at the launch base (Phill 11-07,
#: np-4f771671 (a)): levain's own repo ships ``levain/templates/activation/`` as package data. ``.claude``, ``.codex``,
#: ``.levain``, ``.mcp.json`` and ``.git`` never qualify: levain's repo tracks none of them (``git ls-files``, 11-07),
#: and those are the names a vendor tool reads from any directory it is started in, which ``activation`` is not.
TRACKED_EXEMPT_NAMES = frozenset({"activation"})
_BLOB_MODES = {"100644": False, "100755": True}   # regular files only: never 120000 (a symlink) or 160000 (a gitlink)
_BASE_SHA_LEN = (40, 64)
_GIT_TIMEOUT = 60
_LS_TREE_MAX_BYTES = 64 << 20
_READ_CHUNK = 1 << 20
#: The most candidate ``activation`` directories one lane may hold for vetting; more refuses (L3 r2 #4): the candidates
#: become ``ls-tree`` pathspecs, so an unbounded list is an unbounded argv. levain's own repo holds one.
_CANDIDATE_CAP = 256
#: Characters that change what a permission rule or a sandbox path matches (glob syntax, its escape, gitignore-style
#: negation and comment at a component's start). An exempt path holding one refuses instead of being escaped (L3 r1 #5).
_GLOB_CHARS = frozenset("*?[]{}\\")
_GLOB_LEAD = ("!", "#")
_PROTECTED_FOLDED = frozenset(n.casefold() for n in PROTECTED_NAMES)


def _read_capped(proc, deadline: float) -> bytes:
    """``proc``'s stdout, streamed under :data:`_WALK_CAP` records and :data:`_LS_TREE_MAX_BYTES` bytes and a wall
    deadline: the base tree is never held whole past the cap. Raises :class:`SeatRefusal`."""
    import selectors
    import time
    buf, records = bytearray(), 0
    fd = proc.stdout.fileno()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise SeatRefusal(f"reading the base tree did not finish within {_GIT_TIMEOUT} s")
            if not sel.select(left):
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                return bytes(buf)
            records += chunk.count(b"\0")
            buf += chunk
            if records > _WALK_CAP or len(buf) > _LS_TREE_MAX_BYTES:
                raise SeatRefusal(f"the base tree under the activation directories is too large to vet "
                                  f"(over {_WALK_CAP} entries)")


def tracked_at_base(clone: Path, base_sha: str, paths: Sequence[str] | None = None) -> dict[str, tuple[str, str]]:
    """``{path: (mode, object id)}`` of what git tracks in ``base_sha``'s tree under ``paths`` (literal, relative to
    the clone's top; None: the whole tree), read by the LAUNCHER from the clone's object store before any seat runs: no
    hooks, no global or system config, no replace refs, no lazy fetch. Never read from the working tree. The output is
    streamed under the walk cap and a deadline. Raises :class:`SeatRefusal` (with the reason, never a traceback) when
    the commit cannot be read, git cannot run or time out, or the tree is too large."""
    if len(base_sha) not in _BASE_SHA_LEN or any(c not in "0123456789abcdef" for c in base_sha):
        raise SeatRefusal("the base sha must be a full lowercase hex commit id")
    if paths is not None and not paths:
        return {}
    import subprocess
    import time
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""),
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
           "GIT_NO_LAZY_FETCH": "1", "GIT_LITERAL_PATHSPECS": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
    argv = ["git", "--no-replace-objects", f"--git-dir={clone / '.git'}", "-c", "core.hooksPath=/dev/null",
            "ls-tree", "-r", "-z", "--full-tree", f"{base_sha}^{{commit}}"]
    if paths:
        argv += ["--", *paths]
    try:
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(argv, env=env, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=errf)
            try:
                data = _read_capped(proc, time.monotonic() + _GIT_TIMEOUT)
                code = proc.wait(timeout=max(1, _GIT_TIMEOUT))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stdout.close()
            errf.seek(0)
            err = errf.read(4096).decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise SeatRefusal(f"the base {base_sha[:12]} cannot be read from the clone: {type(exc).__name__}: "
                          f"{str(exc)[:200]}") from None
    if code:
        raise SeatRefusal(f"the base {base_sha[:12]} cannot be read from the clone: {err[:300]}")
    out: dict[str, tuple[str, str]] = {}
    try:
        for rec in data.split(b"\0"):
            if rec:
                meta, _, path = rec.partition(b"\t")
                mode, _kind, oid = meta.decode().split(" ")
                out[os.fsdecode(path)] = (mode, oid)
    except ValueError:
        raise SeatRefusal(f"the base {base_sha[:12]} tree listing could not be parsed") from None
    return out


def _glob_problem(path: str) -> bool:
    return any(c in _GLOB_CHARS for c in path) or any(part.startswith(_GLOB_LEAD) for part in path.split("/"))


def _file_problem(dir_fd: int, name: str, st: os.stat_result, oid: str, shown: str) -> str | None:
    """Why the regular file ``name`` (in ``dir_fd``, lstat'd as ``st``) is not the tracked blob ``oid``, or None. One
    link only (a second name is another way to write the inode, as ``host_guard.hook_pin`` rules); opened with
    ``O_NOFOLLOW`` and its identity compared with ``st`` (a swap after the walk saw it refuses); hashed in chunks."""
    if st.st_nlink != 1:
        return f"{shown} has {st.st_nlink} hard links"
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        return f"{shown} cannot be read ({exc.strerror or type(exc).__name__})"
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino) or not stat.S_ISREG(fst.st_mode) or fst.st_nlink != 1:
            return f"{shown} changed while it was checked"
        h = hashlib.sha1() if len(oid) == 40 else hashlib.sha256()   # noqa: S324 (git's object id, not a security hash)
        h.update(b"blob %d\0" % fst.st_size)
        n = 0
        while chunk := os.read(fd, _READ_CHUNK):
            n += len(chunk)
            if n > fst.st_size:
                break
            h.update(chunk)
        if n != fst.st_size:
            return f"{shown} changed while it was checked"
    except OSError as exc:
        return f"{shown} cannot be read ({exc.strerror or type(exc).__name__})"
    finally:
        os.close(fd)
    return None if h.hexdigest() == oid else f"{shown} differs from the base"


def _exempt_problem(root: Path, top: Path, tracked: Mapping[str, tuple[str, str]]) -> str | None:
    """Why the protected-name directory ``top`` (below ``root``'s top level) is NOT exempt, or None when it is: its
    working tree is exactly what git tracks under it at the base (same regular files, same bytes, same executable bit,
    one link each, no symlink, no extra file or directory, no protected name inside, ``activation`` included), and no
    path in it holds a glob character. Walked by file descriptor without following links; an environment error is a
    reason, never a raised exception."""
    rel = top.relative_to(root).as_posix()
    if os.path.islink(top):
        return "it is a symlink"
    if _glob_problem(str(top)):
        return "its path holds a glob character the deny rules would read as a pattern"
    want = {p[len(rel) + 1:]: v for p, v in tracked.items() if p.startswith(rel + "/")}
    if not want:
        return "git does not track it at the base"
    for p, (mode, _) in sorted(want.items()):
        if mode not in _BLOB_MODES:
            return f"{rel}/{p} is tracked as mode {mode}, not a regular file"
        if _glob_problem(p):
            return f"{rel}/{p} holds a glob character the deny rules would read as a pattern"
    dirs = {str(Path(p).parent) for p in want} - {"."}
    dirs |= {str(a) for d in list(dirs) for a in Path(d).parents} - {"."}
    seen_files: set[str] = set()
    seen = 0
    try:
        for dirpath, dirnames, filenames, dir_fd in os.fwalk(top, follow_symlinks=False):
            here = Path(dirpath)
            for name in dirnames + filenames:
                sub = (here / name).relative_to(top).as_posix()
                shown = f"{rel}/{sub}"
                seen += 1
                if seen > _WALK_CAP:
                    return "it is too large to vet"
                if name.casefold() in _PROTECTED_FOLDED:
                    return f"{shown} is a protected name inside it"
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if stat.S_ISLNK(st.st_mode):
                    return f"{shown} is a symlink"
                if stat.S_ISDIR(st.st_mode):
                    if sub not in dirs:
                        return f"{shown} is a directory git does not track at the base"
                    continue
                if not stat.S_ISREG(st.st_mode):
                    return f"{shown} is not a regular file"
                if sub not in want:
                    return f"{shown} is not tracked at the base"
                mode, oid = want[sub]
                if bool(st.st_mode & 0o100) != _BLOB_MODES[mode]:
                    return f"{shown} differs from the base (executable bit)"
                if why := _file_problem(dir_fd, name, st, oid, shown):
                    return why
                seen_files.add(sub)
    except OSError as exc:
        return f"it cannot be read ({exc.strerror or type(exc).__name__})"
    missing = sorted(set(want) - seen_files)
    if missing:
        return f"{rel}/{missing[0]} is tracked at the base but missing"
    return None


def _contains_agent_config(root: Path, *, git_dir_ok: bool, base_sha: str | None = None,
                           exempt: list[str] | None = None) -> list[str]:
    """Every agent-config target under ``root`` (a :data:`PROTECTED_NAMES` entry at any depth). A top-level real
    ``.git`` directory is the clone's own and is not descended into when ``git_dir_ok``; a gitfile or a nested
    ``.git`` is a target. With ``base_sha``, a :data:`TRACKED_EXEMPT_NAMES` directory BELOW the top level is a
    candidate: only when the walk found one is the base read (:func:`tracked_at_base`, restricted to the candidates),
    and a candidate whose tree is exactly the tracked one is not a target: its absolute path is appended to ``exempt``
    (the caller denies writes to it) and a mismatch is reported with its reason. A symlink anywhere in ``root`` that
    resolves onto an exempt directory, an ancestor of one or a path inside one is a target (another spelling a write
    could take past the path-based deny rules)."""
    found: list[str] = []
    candidates: list[Path] = []
    links: list[Path] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        # every entry counts, BEFORE any is filtered out: a candidate or a protected name is never free (L3 r2 #4)
        seen += len(dirnames) + len(filenames)
        if seen > _WALK_CAP:
            found.append(f"{root} is too large to vet")
            return found
        keep = []
        for d in dirnames:
            if d == ".git" and git_dir_ok and here == root:
                continue                               # judged once, after the walk (L3 r2 #5)
            if d in TRACKED_EXEMPT_NAMES and base_sha is not None and here != root:
                candidates.append(here / d)            # judged below, after the walk; never descended here
                if len(candidates) > _CANDIDATE_CAP:
                    found.append(f"{root} holds more than {_CANDIDATE_CAP} activation directories to vet")
                    return found
            elif d in PROTECTED_NAMES:
                found.append(str(here / d))
            elif os.path.islink(here / d):
                links.append(here / d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f == ".git" and git_dir_ok and here == root:
                continue                               # a file, broken link or link to a file: judged once, below
            if f in PROTECTED_NAMES:
                found.append(str(here / f))
            elif os.path.islink(here / f):
                links.append(here / f)
    # the SOLE report for the clone's own top-level .git: anything but a real directory (a symlink to one included)
    if git_dir_ok and (os.path.islink(root / ".git") or not (root / ".git").is_dir()):
        found.append(f"{root}/.git is not a real directory")
    if not candidates:
        return found
    try:
        tracked = tracked_at_base(root, base_sha, [c.relative_to(root).as_posix() for c in candidates])
    except SeatRefusal as exc:
        return found + [f"{c} (not exempt: {exc})" for c in candidates]
    ok: list[str] = []
    for c in candidates:
        why = _exempt_problem(root, c, tracked)
        if why is None:
            ok.append(str(c))
        else:
            found.append(f"{c} (not exempt: {why})")
    for link in links:
        target = os.path.realpath(link)
        for ex in ok:
            if hg.is_within(ex, target) or hg.is_within(target, ex):
                found.append(f"{link} (it is a symlink onto the read-only {ex})")
                break
    if exempt is not None:
        exempt += ok
    return found


def _roots_problems(clone: Path, return_dir: Path, key_dir: Path, gh_dirs: Sequence[Path] = ()) -> list[str]:
    problems: list[str] = []
    for what, p in (("the clone", clone), ("the RETURN directory", return_dir)):
        if os.path.islink(p) or not p.is_dir():
            problems.append(f"{what} {str(p)!r} is not a real directory")
    if hg.is_within(clone, return_dir) or hg.is_within(return_dir, clone):
        problems.append("the clone and the RETURN directory overlap")
    for what, p in (("the clone", clone), ("the RETURN directory", return_dir)):
        if hg.is_within(key_dir, p) or hg.is_within(p, key_dir):
            problems.append(f"{what} overlaps the launcher key directory")
        if any(hg.is_within(g, p) or hg.is_within(p, g) for g in gh_dirs):
            problems.append(f"{what} overlaps the read-token directory")
        if hg.is_within(p, Path.home() / ".claude"):
            problems.append(f"{what} is inside ~/.claude")
    return problems


def _lease_problems(clone: Path, return_dir: Path, lease: Mapping[str, Any] | None, canary_root: Path | None,
                    environ: Mapping[str, str]) -> list[str]:
    if lease is None:
        if canary_root is None:
            return ["no lease: a profile without one is only for the canary's fixed test root"]
        prefix = _canary_prefix(environ)
        cr = _real(canary_root)
        if prefix not in cr.parents:
            return [f"the canary root {str(cr)!r} is not under {str(prefix)!r}"]
        if not (hg.is_within(clone, cr) and hg.is_within(return_dir, cr)):
            return ["the clone and the RETURN directory must both be under the canary root"]
        return []
    if canary_root is not None:
        return ["a lease and a canary root were both given"]
    problems = []
    if lease.get("kind") != SEAT_LEASE_KIND or lease.get("agent") != "claude_code":
        problems.append("the lease is not a claude_code seat-root-lease")
    for key, path in (("lanes_parent", clone), ("returns_parent", return_dir)):
        parent = lease.get(key)
        if not isinstance(parent, str) or not os.path.isabs(parent):
            problems.append(f"the lease carries no absolute {key}")
        elif _real(parent) == path or not hg.is_within(path, parent):
            problems.append(f"{str(path)!r} is not below the lease's {key}")
    return problems


def _egress_problems(clone: Path, lease: Mapping[str, Any] | None, egress: Mapping[str, Any] | None,
                     cockpit_root: Path | str | None, levain_home: Path | str | None) -> list[str]:
    if egress is None:
        return []
    if lease is None:
        return ["a seat-egress grant needs a lease launch, never the canary's fixed root"]
    problems = []
    if egress.get("kind") != SEAT_EGRESS_KIND or egress.get("agent") != "claude_code":
        problems.append("the egress grant is not a claude_code seat-egress grant")
    parent = egress.get("lanes_parent")
    if not isinstance(parent, str) or not os.path.isabs(parent) or _real(parent) == clone or not hg.is_within(clone, parent):
        problems.append("the clone is not below the egress grant's lanes_parent")
    if lease.get("lanes_parent") != parent:
        problems.append("the egress grant's lanes_parent is not the lease's")
    if not isinstance(egress.get("allowed_domains"), (list, tuple)):
        problems.append("the egress grant carries no allowed_domains list")
    if cockpit_root is None or levain_home is None:
        problems.append("an egress profile needs the cockpit root and the levain home its hook reads")
    return problems


def build_seat_profile(clone: Path, return_dir: Path, *, claude_bin: str, python: str,
                       lease: Mapping[str, Any] | None, key_dir: Path,
                       canary_root: Path | None = None, environ: Mapping[str, str] | None = None,
                       managed: Sequence[tuple[str, Any]] | None = None, platform: str = sys.platform,
                       home: Path | None = None, egress: Mapping[str, Any] | None = None,
                       cockpit_root: Path | str | None = None, levain_home: Path | str | None = None,
                       tools: Sequence[str] = CC_SEAT_TOOLS, base_sha: str | None = None) -> SeatProfile:
    """Build the terminal seat profile, or raise :class:`SeatRefusal` naming every problem found. ``lease=None`` is
    accepted only with ``canary_root`` under the fixed test prefix. Refuses a root that already holds an agent-config
    target, and an executable (python, claude) a seat could write.

    ``egress`` is the live ``seat-egress`` grant (PATHZERO Z2a, :mod:`levain.seat_push`) or None. With it the sandbox's
    ``allowedDomains`` is the grant's ``allowed_domains`` and the profile carries the lane-branch ``Stop`` /
    ``SessionEnd`` push hook (``cockpit_root`` and ``levain_home`` are what the hook reads, both required); with None
    both are exactly what they were before Z2a (empty domains, no such hook). A grant never reaches a canary profile.
    A lease launch needs ``levain_home`` with or without a grant (Z2b: the real read-token directory is denied and the
    ``levain_gh`` server reads it); the canary's server gets a home of its own, and the real directory is denied there too.
    The domains are frozen at launch: a revoke stops the push at once, the network only at the seat's relaunch (the
    KNOWN EDGE at the ``allowedDomains`` line). ``tools`` is the seat's ``--tools`` set (default ``CC_SEAT_TOOLS``; a
    subset when a failed canary named a fallback, PATHZERO Z5).

    ``base_sha`` is the commit the launcher checked the clone out at (the lease launch passes it; the canary and the
    dry run do not). With it, a :data:`TRACKED_EXEMPT_NAMES` directory below the clone's top level whose working tree
    is exactly what git tracks under it at that commit (:func:`tracked_at_base`, read by the launcher, never from the
    working tree) is launched read-only instead of refused. The bound that holds it read-only is the push
    (:func:`levain.seat_push.tree_refusal` refuses a lane tip that changes it); the sandbox's ``denyWrite`` and the
    file tools' deny rules name it as defence in depth. Anything extra, changed, symlinked or untracked in it still
    refuses, as does every other protected name."""
    environ = os.environ if environ is None else environ
    tools = tuple(tools)
    clone, return_dir, key_dir = _real(clone), _real(return_dir), _real(key_dir)
    roots = (clone, return_dir)
    problems = _lease_problems(clone, return_dir, lease, canary_root, environ)
    if not tools or len(set(tools)) != len(tools) or any(t not in CC_SEAT_TOOLS for t in tools):
        problems.append(f"the tool set {tools!r} must be a non-empty subset of the admitted seat tools, with no repeats")
    problems += _egress_problems(clone, lease, egress, cockpit_root, levain_home)
    # The read-token directory (Z2b, L3 z2b r1 HIGH): the REAL one, ``<levain home>/seat_gh`` as ``environ`` names the
    # levain home, is denied in EVERY profile, with or without an egress grant and in the canary's too: the seat's Bash
    # runs as the same user and would otherwise read the 0600 token. A lease launch must name the levain home (the MCP
    # server reads its tokens there); only the canary's lease-free profile points its server at a home of its own.
    real_home = _real(real_levain_home(environ))
    if lease is not None and levain_home is None:
        problems.append("a lease launch needs the levain home (its read-token directory is denied to the seat and the "
                        "levain_gh server reads it)")
    gh_home = _real(levain_home) if levain_home is not None else _canary_prefix(environ) / "levain-home"
    gh_dirs = list(dict.fromkeys([gh_home / seat_gh.TOKEN_DIR, real_home / seat_gh.TOKEN_DIR]))
    problems += _roots_problems(clone, return_dir, key_dir, gh_dirs)
    exempt: list[str] = []
    if not problems:
        if base_sha is not None and (len(base_sha) not in _BASE_SHA_LEN
                                     or any(c not in "0123456789abcdef" for c in base_sha)):
            problems.append("the base sha must be a full lowercase hex commit id")
        else:
            problems += [f"{t} is an agent-config target inside a seat root" for t in
                         _contains_agent_config(clone, git_dir_ok=True, base_sha=base_sha, exempt=exempt)
                         + _contains_agent_config(return_dir, git_dir_ok=False)]
    problems += hg._check_exec(python, roots, "the Python that runs the seat's hooks")
    problems += hg._check_exec(claude_bin, roots, "the claude binary")
    pkg = Path(__file__).resolve().parent
    problems += [f"the levain package ({pkg}) is inside a seat root" for r in roots if hg.is_within(pkg, r)]
    if problems:
        raise SeatRefusal("; ".join(problems))

    sandbox = hg.cc_sandbox_block(clone)
    # strictAllowlist; empty without a live seat-egress grant (D7: no local binding), else the grant's own list.
    # KNOWN EDGE (L3 zc r1 codex HIGH 1): this list is FROZEN into the launch's --settings JSON. A revoke or an expiry
    # cuts the PUSH at once (the hook and the executor re-read the authority log each time) but NOT the network a
    # running seat already has: the domains last until that seat is relaunched. Mitigated: the seat holds no remote
    # credential (the push is Levain's executor, outside the bound), and a launch under a revoked or expired grant gets
    # no domains (and under an expired lease refuses). ``levain seat egress revoke`` stops the seats launched under the
    # revoked grant (L-F, ``seat_lease.stop_seats_for_grant``).
    sandbox["network"]["allowedDomains"] = [] if egress is None else list(egress["allowed_domains"])
    sandbox["filesystem"] = {
        "disabled": False,
        "allowWrite": [str(r) for r in roots],
        "denyWrite": seat_deny_write(clone, git=True) + seat_deny_write(return_dir, git=False) + exempt,
        "denyRead": [str(key_dir), *map(str, gh_dirs)],
    }
    py = shlex.quote(python)
    from levain import observe   # constants only
    # -EP, not -I: -I also drops the user site, where levain may be installed (the consent hooks' ruling, HOOK_FLAGS in
    # levain.consent.adapters.claude_code); -E and -P keep the PYTHON* variables and the cwd off the import path.
    # Known edge (L3 r1 codex, closed under the reachability line): Python derives the user site from the child's HOME, so a
    # user-site install is found only while the seat's HOME is the one levain was installed under; -I never found it at all.
    hook = lambda sub: {"type": "command", "command": f"{py} -EP -B -m levain.seat_record {sub}", "timeout": _HOOK_TIMEOUT}
    records = shlex.quote(str(key_dir / "records"))
    hooks = {
        "PermissionRequest": [{"matcher": "", "hooks": [{
            "type": "command", "command": f"{py} -EP -m {observe.MARKER} {observe.SUFFIX}"}]}],
        "PreToolUse": [{"matcher": "", "hooks": [hook(f"pre {records}")]}],
        "PostToolUse": [{"matcher": "", "hooks": [hook("post")]}],
        "PostToolUseFailure": [{"matcher": "", "hooks": [hook("post")]}],
    }
    if egress is not None:                       # the lane-branch push (Z2a): Levain's executor, outside the bound
        from levain import seat_push
        hooks = {**hooks, **seat_push.hook_entries(python, clone=clone, key_dir=key_dir, cockpit_root=cockpit_root,
                                                   home=Path(levain_home))}
    settings = {
        "sandbox": sandbox,
        "permissions": {"defaultMode": "acceptEdits", "additionalDirectories": [str(return_dir)],
                        "allow": list(GH_TOOL_ALLOW),
                        "deny": seat_permission_deny(roots, key_dir, gh_dirs) + exempt_permission_deny(clone, exempt)},
        "includeGitInstructions": False,
        "autoMemoryEnabled": False,
        "hooks": hooks,
    }
    problems += hg.check_sandbox_types(sandbox)
    problems += hg.check_managed_settings(
        hg.load_managed_settings(platform) if managed is None else managed, roots,
        trusted_hooks=hg.load_managed_trust(platform, environ, home))
    if problems:
        raise SeatRefusal("; ".join(problems))
    env = {**hg.host_env(environ, roots=roots), "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}
    return SeatProfile(clone=clone, return_dir=return_dir, roots=roots, claude_bin=claude_bin, settings=settings,
                       mcp_config=gh_mcp_config(python, gh_home), env=env, tools=tools)
