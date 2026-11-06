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
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from levain import host_guard as hg
from levain.authority import SEAT_EGRESS_KIND, SEAT_LEASE_KIND
from levain.host_guard import (CC_SEAT_TOOLS, PROTECTED_NAMES, SEAT_SOURCE_ARGS, HostRefusal)

__all__ = ["SeatRefusal", "SeatProfile", "seat_deny_write", "seat_permission_deny", "build_seat_profile",
           "CANARY_DIRNAME", "FILE_TOOLS", "SEAT_LEASE_KIND"]

CANARY_DIRNAME = "levain-seat-canary"
#: All four file tools are named in every deny, so a later re-admission inherits it. Measured 11-06 on 2.1.319: an
#: ``Edit(...)`` rule refuses the Write tool and a ``Write(...)`` rule alone did NOT, so the Edit rules carry the deny.
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
_GIT_DENY = ("hooks", "config", "info", "modules", "worktrees")
_WALK_CAP = 200_000
_HOOK_TIMEOUT = 30


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


def seat_permission_deny(roots: Sequence[Path], key_dir: Path) -> list[str]:
    """``permissions.deny``: all four file tools on every protected pattern of every root (plus a gitfile anywhere),
    and the Read tool on the launcher key directory. The first root (the clone, the working directory) also gets the
    cwd-relative spellings, which are the ones measured to fire after symlink resolution."""
    rules: list[str] = []
    for i, root in enumerate(roots):
        pats = [f"{root}/{name}/**" for name in sorted(PROTECTED_NAMES - {".mcp.json"})]
        pats += [f"{root}/.mcp.json", f"{root}/**/.git"]
        for tool in FILE_TOOLS:
            rules += [_abs_rule(tool, p) for p in pats]
            if i == 0:
                rules += [f"{tool}({name}/**)" for name in sorted(PROTECTED_NAMES - {".mcp.json"})]
                rules += [f"{tool}(.mcp.json)", f"{tool}(**/.git)"]
    rules.append(_abs_rule("Read", f"{key_dir}/**"))
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


def _contains_agent_config(root: Path, *, git_dir_ok: bool) -> list[str]:
    """Every agent-config target under ``root`` (a :data:`PROTECTED_NAMES` entry at any depth). A top-level real
    ``.git`` directory is the clone's own and is not descended into when ``git_dir_ok``; a gitfile or a nested
    ``.git`` is a target."""
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        keep = []
        for d in dirnames:
            if d == ".git" and git_dir_ok and here == root:
                if os.path.islink(here / d):
                    found.append(str(here / d))
                continue
            if d in PROTECTED_NAMES:
                found.append(str(here / d))
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            if f in PROTECTED_NAMES:
                found.append(str(here / f))
        seen += len(dirnames) + len(filenames)
        if seen > _WALK_CAP:
            found.append(f"{root} is too large to vet")
            break
    if git_dir_ok and not (root / ".git").is_dir():
        found.append(f"{root}/.git is not a real directory")
    return found


def _roots_problems(clone: Path, return_dir: Path, key_dir: Path) -> list[str]:
    problems: list[str] = []
    for what, p in (("the clone", clone), ("the RETURN directory", return_dir)):
        if os.path.islink(p) or not p.is_dir():
            problems.append(f"{what} {str(p)!r} is not a real directory")
    if hg.is_within(clone, return_dir) or hg.is_within(return_dir, clone):
        problems.append("the clone and the RETURN directory overlap")
    for what, p in (("the clone", clone), ("the RETURN directory", return_dir)):
        if hg.is_within(key_dir, p) or hg.is_within(p, key_dir):
            problems.append(f"{what} overlaps the launcher key directory")
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
                       cockpit_root: Path | str | None = None, levain_home: Path | str | None = None) -> SeatProfile:
    """Build the terminal seat profile, or raise :class:`SeatRefusal` naming every problem found. ``lease=None`` is
    accepted only with ``canary_root`` under the fixed test prefix. Refuses a root that already holds an agent-config
    target, and an executable (python, claude) a seat could write.

    ``egress`` is the live ``seat-egress`` grant (PATHZERO Z2a, :mod:`levain.seat_push`) or None. With it the sandbox's
    ``allowedDomains`` is the grant's ``allowed_domains`` and the profile carries the lane-branch ``Stop`` /
    ``SessionEnd`` push hook (``cockpit_root`` and ``levain_home`` are what the hook reads, both required); with None
    both are exactly what they were before Z2a (empty domains, no such hook). A grant never reaches a canary profile.
    The domains are frozen at launch: a revoke stops the push at once, the network only at the seat's relaunch (the
    KNOWN EDGE at the ``allowedDomains`` line)."""
    environ = os.environ if environ is None else environ
    clone, return_dir, key_dir = _real(clone), _real(return_dir), _real(key_dir)
    roots = (clone, return_dir)
    problems = _lease_problems(clone, return_dir, lease, canary_root, environ)
    problems += _egress_problems(clone, lease, egress, cockpit_root, levain_home)
    problems += _roots_problems(clone, return_dir, key_dir)
    if not problems:
        problems += [f"{t} is an agent-config target inside a seat root" for t in
                     _contains_agent_config(clone, git_dir_ok=True) + _contains_agent_config(return_dir, git_dir_ok=False)]
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
    # no domains (and under an expired lease refuses). Killing running seats on a revoke is the launcher lane's (L-F).
    sandbox["network"]["allowedDomains"] = [] if egress is None else list(egress["allowed_domains"])
    sandbox["filesystem"] = {
        "disabled": False,
        "allowWrite": [str(r) for r in roots],
        "denyWrite": seat_deny_write(clone, git=True) + seat_deny_write(return_dir, git=False),
        "denyRead": [str(key_dir)],
    }
    py = shlex.quote(python)
    from levain import observe   # constants only
    hook = lambda sub: {"type": "command", "command": f"{py} -I -B -m levain.seat_record {sub}", "timeout": _HOOK_TIMEOUT}
    records = shlex.quote(str(key_dir / "records"))
    hooks = {
        "PermissionRequest": [{"matcher": "", "hooks": [{
            "type": "command", "command": f"{py} -I -m {observe.MARKER} {observe.SUFFIX}"}]}],
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
                        "deny": seat_permission_deny(roots, key_dir)},
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
                       mcp_config={"mcpServers": {}}, env=env)
