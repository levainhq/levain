"""Hosting preconditions for driving a Claude Code or Codex primary from the cockpit (chat S2).

The cockpit adds an HTTP route that answers approvals. An entity that can reach it can approve
its own held actions, which removes the human gate a terminal prompt keeps. So a hosted session
must hold one property: no code the entity can influence runs outside the OS sandbox while the
session is hosted. Phill ruled the shape on 10-06 (Claude Code: CC's own sandbox with loopback
and Unix sockets denied, or refuse; Codex: network-denied sandbox, broker socket in a private
dir). The design, its decisions D1-D10 and the review that shaped it live flow-side in
projects/levain/reference/chat_s2_plan_1007.md (branch seat/1007-22-s2-plan).

THE FRAME (D1): a hosted Claude Code session is built UP from `--restricted`, which loads only
managed settings and `--settings` and confines the file tools to the working directories, and
then gets exactly what this module adds: four tools, Levain's own MCP server, Levain's own hooks,
the sandbox block. Subtracting escapes from the operator's own settings was tried in the plan's
first round, and every subtraction found the next surface.

This module builds and checks; it never runs a model. `levain.host_canary` proves by behaviour
what this module can only configure. Nothing here writes a Seatbelt profile or calls
sandbox-exec: the sandbox is Claude Code's or Codex's own, as shipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "HostRefusal", "CCHostProfile", "cc_host_profile", "cc_writable_roots", "is_within",
    "host_dir", "cc_sandbox_block", "check_sandbox_types", "cc_hook_settings", "cc_mcp_config",
    "hook_pin", "check_or_record_pin", "repin", "check_managed_settings", "host_env",
    "file_write_decision", "parse_cc_version", "CC_HOSTED_TOOLS", "CC_VERSION_FLOOR",
    "check_shared_code", "codex_initialize_params",
    "broker_method_allowed", "codex_sandbox_policy", "overwrite_codex_policy",
    "check_codex_effective_policy", "check_codex_hooks", "check_codex_code_paths",
    "codex_spawn_env",
]


class HostRefusal(Exception):
    """Hosting is refused. ``str()`` is the reason shown to the operator."""


# --restricted needs 2.1.248, and a sandbox made admin-required by `--settings` ignores the
# repository's loosening keys only from 2.1.285 (CC docs: cli-reference, sandboxing).
CC_VERSION_FLOOR = (2, 1, 285)

# D4: the exact hosted tool set, compared by string equality. Bash runs inside the sandbox; Read,
# Edit and Write are confined to the working directories by --restricted, and Edit/Write prompts
# reach the host (file_write_decision). Anything else is re-admitted one tool at a time, behind a
# canary arm that proves it inherits the gate and the confinement.
CC_HOSTED_TOOLS = ("Bash", "Read", "Edit", "Write")

# Workspace names whose contents something outside the sandbox reads or runs: the hooks and their
# content (activation), Levain's own state and memory (.levain), Claude Code and git config.
PROTECTED_NAMES = frozenset({"activation", ".levain", ".claude", ".git", ".mcp.json"})

_ENV_ALLOW = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR"})
_ENV_ALLOW_PREFIXES = ("LC_",)

_TEMPLATES = Path(__file__).resolve().parent / "templates" / "adapters"
_CC_SETTINGS_TEMPLATE = _TEMPLATES / "claude-code" / "settings.template.json"
_CC_MCP_TEMPLATE = _TEMPLATES / "claude-code" / "mcp.template.json"

_MANAGED_SETTINGS = {
    "darwin": Path("/Library/Application Support/ClaudeCode"),
    "linux": Path("/etc/claude-code"),
}


# ---------------------------------------------------------------------------- paths


def _real(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(p))


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True when ``path`` resolves to ``root`` or below it (symlinks resolved on both sides)."""
    rp, rr = _real(path), _real(root)
    return rp == rr or rr in rp.parents


def cc_writable_roots(entity: str | os.PathLike[str]) -> tuple[Path, ...]:
    """Every place a hosted CC entity can write: its working directory (sandboxed Bash and the
    confined file tools) and the temp dirs sandboxed Bash may write. Over-inclusive on purpose:
    a root listed here that the sandbox does not grant only makes a check stricter."""
    candidates = [Path(entity), Path(tempfile.gettempdir()), Path("/tmp"), Path("/private/tmp"),
                  Path("/var/tmp"), Path("/dev/shm")]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidates.append(Path(tmpdir))
    out: list[Path] = []
    for c in candidates:
        if c.exists():
            r = _real(c)
            if r not in out:
                out.append(r)
    return tuple(out)


def _outside(path: str | os.PathLike[str], roots: Sequence[Path], what: str) -> list[str]:
    if not os.path.isabs(path):
        return [f"{what} is not an absolute path: {path!r}"]
    for root in roots:
        if is_within(path, root):
            return [f"{what} {os.fspath(path)!r} is inside {os.fspath(root)!r}, which the entity can write"]
    return []


def _base_dir(kind: Literal["run", "state"], platform: str, environ: Mapping[str, str],
              home: Path) -> Path:
    if platform == "darwin":
        return home / "Library" / "Application Support" / "levain" / kind
    if platform.startswith("linux"):
        if kind == "run":
            xdg = environ.get("XDG_RUNTIME_DIR", "")
            if not xdg or not os.path.isabs(xdg):
                raise HostRefusal("XDG_RUNTIME_DIR is unset or not absolute; Levain will not put its "
                                  "host sockets in /tmp, so hosting is refused")
            try:
                st = os.stat(xdg)
            except OSError as exc:
                raise HostRefusal(f"XDG_RUNTIME_DIR {xdg!r} is unusable: {exc}") from None
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise HostRefusal(f"XDG_RUNTIME_DIR {xdg!r} is not owned by this user with mode 0700")
            return Path(xdg) / "levain"
        state = environ.get("XDG_STATE_HOME", "")
        base = Path(state) if state and os.path.isabs(state) else home / ".local" / "state"
        return base / "levain" / "state"
    raise HostRefusal(f"hosting a primary is not supported on platform {platform!r}")


def host_dir(entity: str | os.PathLike[str], kind: Literal["run", "state"], *,
             roots: Sequence[Path], platform: str = sys.platform,
             environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """A per-entity private directory outside every root the entity can write: ``run`` holds the
    canary listener, the hook bytecode cache and the Codex broker socket; ``state`` holds the hook
    pin. Never falls back to /tmp or $TMPDIR (a model shell could unlink or replace a socket in
    either, measured by the 10-06 probes). Created 0700; symlinks and foreign owners refused."""
    environ = os.environ if environ is None else environ
    home = Path(os.path.expanduser("~")) if home is None else home
    base = _base_dir(kind, platform, environ, home)
    name = hashlib.sha256(os.fsencode(_real(entity))).hexdigest()[:16]
    target = base / name
    # Create every missing component 0700, then check the Levain-owned chain: the target and each
    # parent up to and including the nearest directory named "levain".
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    chain = [target, *target.parents]
    stop = next((i for i, c in enumerate(chain) if c.name == "levain"), None)
    if stop is None:
        raise HostRefusal(f"host directory {os.fspath(target)!r} has no levain-owned parent")
    for c in chain[: stop + 1]:
        st = os.lstat(c)
        if stat.S_ISLNK(st.st_mode):
            raise HostRefusal(f"host directory component {os.fspath(c)!r} is a symlink")
        if st.st_uid != os.getuid():
            raise HostRefusal(f"host directory component {os.fspath(c)!r} is not owned by this user")
        if st.st_mode & 0o077:
            os.chmod(c, 0o700)
            if os.lstat(c).st_mode & 0o077:
                raise HostRefusal(f"host directory {os.fspath(c)!r} could not be made private")
    problems = _outside(target, roots, f"host {kind} directory")
    if problems:
        raise HostRefusal(problems[0])
    return target


# ---------------------------------------------------------------------------- sandbox block


# The pinned sandbox keys and their types. A string where a boolean belongs dropped the whole
# block silently on CC 2.1.292 (probes 1f, `allowUnsandboxedCommands: "forbid"`); the type check
# exists because of that measurement, and the canary exists because a type check is not enough.
_SANDBOX_TYPES: dict[tuple[str, ...], type] = {
    ("enabled",): bool,
    ("failIfUnavailable",): bool,
    ("allowUnsandboxedCommands",): bool,
    ("autoAllowBashIfSandboxed",): bool,
    ("excludedCommands",): list,
    ("enableWeakerNestedSandbox",): bool,
    ("enableWeakerNetworkIsolation",): bool,
    ("network", "allowLocalBinding"): bool,
    ("network", "allowUnixSockets"): list,
    ("network", "allowAllUnixSockets"): bool,
    ("network", "strictAllowlist"): bool,
    ("network", "deniedDomains"): list,
    ("filesystem", "disabled"): bool,
    ("filesystem", "denyWrite"): list,
}


def cc_sandbox_block(entity: str | os.PathLike[str]) -> dict[str, Any]:
    ent = _real(entity)
    return {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "excludedCommands": [],
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
        # strictAllowlist with an empty allowlist: the sandbox's own HTTP proxy denies every host
        # instead of prompting, so `curl http://127.0.0.1:<cockpit>` through $HTTP_PROXY is refused
        # (L2 r1 #3); the loopback names are denied as well in case an allow is merged in.
        "network": {"allowLocalBinding": False, "allowUnixSockets": [], "allowAllUnixSockets": False,
                    "strictAllowlist": True,
                    "deniedDomains": ["localhost", "*.localhost", "127.0.0.1", "::1"]},
        # One list for one fact: every protected name is write-denied to sandboxed Bash, the same
        # set the file-tool policy refuses (L1 r1 #4: two lists had drifted to two of five).
        "filesystem": {"disabled": False,
                       "denyWrite": [str(ent / name) for name in sorted(PROTECTED_NAMES)]},
    }


def check_sandbox_types(block: Mapping[str, Any]) -> list[str]:
    """Every pinned key present with exactly its type (``bool`` is not ``int`` here)."""
    problems = []
    for keys, typ in _SANDBOX_TYPES.items():
        node: Any = block
        for k in keys:
            node = node.get(k, _MISSING) if isinstance(node, Mapping) else _MISSING
        name = "sandbox." + ".".join(keys)
        if node is _MISSING:
            problems.append(f"{name} is missing")
        elif type(node) is not typ:
            problems.append(f"{name} must be {typ.__name__}, got {type(node).__name__}")
    return problems


_MISSING = object()


# ---------------------------------------------------------------------------- hooks


_HOOK_ARG = re.compile(r"[A-Za-z0-9_-]+")
_HOOK_SCRIPT = re.compile(r"\$\{CLAUDE_PROJECT_DIR\}/activation/hooks/([A-Za-z0-9_]+\.py)")


def cc_hook_settings(entity: str | os.PathLike[str], python: str,
                     pycache_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Levain's claude-code hook entries, rendered from the INSTALLED package's template (never the
    workspace copy of settings.json), each command rewritten to an exact argv:
    ``<python> -E -P -X pycache_prefix=<host run dir> <entity>/activation/hooks/<hook>.py [arg]``.
    -E ignores PYTHON* variables, -P keeps the cwd off sys.path, and the pycache prefix means
    bytecode is read and written only in the host-owned directory, so a .pyc planted in the
    workspace is never loaded. The scripts stay in place because the hooks derive the install
    root from their own location (_levain_hook.install_root). Refuses when the template's command
    shape is not the one this rewrite understands, rather than guessing."""
    ent = _real(entity)
    template = json.loads(_CC_SETTINGS_TEMPLATE.read_text(encoding="utf-8"))
    hooks = template.get("hooks")
    if not isinstance(hooks, dict) or not hooks:
        raise HostRefusal("the claude-code settings template carries no hooks")
    out: dict[str, Any] = {}
    for event, groups in hooks.items():
        new_groups = []
        for group in groups:
            new_hooks = []
            for h in group.get("hooks", []):
                if h.get("type") != "command" or not isinstance(h.get("command"), str):
                    raise HostRefusal(f"unexpected {event} hook shape in the settings template")
                argv = shlex.split(h["command"])
                m = _HOOK_SCRIPT.fullmatch(argv[1]) if len(argv) >= 2 else None
                if argv[:1] != ["{{PYTHON}}"] or m is None or not all(_HOOK_ARG.fullmatch(a) for a in argv[2:]):
                    raise HostRefusal(f"unexpected {event} hook command in the settings template: {h['command']!r}")
                rebuilt = [python, "-E", "-P", "-X", f"pycache_prefix={os.fspath(pycache_dir)}",
                           str(ent / "activation" / "hooks" / m.group(1)), *argv[2:]]
                new_hooks.append({**h, "command": shlex.join(rebuilt)})
            new_groups.append({**group, "hooks": new_hooks})
        out[event] = new_groups
    return out


def _hook_tree(entity: Path) -> Path:
    tree = entity / "activation" / "hooks"
    if tree.is_symlink() or not tree.is_dir() or (entity / "activation").is_symlink():
        raise HostRefusal(f"{os.fspath(tree)!r} is missing or a symlink")
    return tree


def hook_pin(entity: str | os.PathLike[str]) -> str:
    """sha256 over every file under ``activation/hooks`` except ``__pycache__`` (relative path and
    bytes). Any symlink or non-regular file in the tree refuses: a link points at code the pin
    cannot see, and a FIFO would hang the host. ``__pycache__`` is never read by a hosted hook:
    the host env sets PYTHONPYCACHEPREFIX, so even a hook's own child interpreter uses the
    host-owned cache."""
    tree = _hook_tree(_real(entity))
    h = hashlib.sha256()
    for root, dirs, files in os.walk(tree, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                raise HostRefusal(f"symlink in the hook tree: {os.path.join(root, d)!r}")
        for f in sorted(files):
            p = os.path.join(root, f)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise HostRefusal(f"not a regular file in the hook tree: {p!r}")
            rel = os.path.relpath(p, tree)
            with open(p, "rb") as fh:
                data = fh.read()
            h.update(os.fsencode(rel) + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def _pin_file(state_dir: Path) -> Path:
    return state_dir / "hook_pin.json"


def _write_pin(state_dir: Path, entity: Path, pin: str) -> None:
    tmp = state_dir / f".hook_pin.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"entity": str(entity), "pin": pin}, fh)
    os.replace(tmp, _pin_file(state_dir))


def check_or_record_pin(entity: str | os.PathLike[str], state_dir: Path) -> Literal["recorded", "match"]:
    """D9: the first host-open records the hook-tree pin (trust on first use); later opens refuse on
    any difference until the operator runs ``repin`` from the terminal."""
    ent = _real(entity)
    current = hook_pin(ent)
    pf = _pin_file(state_dir)
    if not pf.exists():
        _write_pin(state_dir, ent, current)
        return "recorded"
    try:
        recorded = json.loads(pf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HostRefusal(f"the hook pin at {os.fspath(pf)!r} is unreadable ({exc}); repin from your terminal: "
                          f"python -m levain.host_guard repin {shlex.quote(str(ent))}") from None
    if recorded.get("entity") != str(ent) or recorded.get("pin") != current:
        raise HostRefusal(
            f"the entity's hook code changed since it was pinned ({os.fspath(ent / 'activation' / 'hooks')}). "
            f"If you changed it, repin from your terminal: python -m levain.host_guard repin {shlex.quote(str(ent))} "
            "; until then this entity is not hosted.")
    return "match"


def repin(entity: str | os.PathLike[str], state_dir: Path) -> str:
    """The operator's explicit act after changing hook code. Returns the new pin."""
    ent = _real(entity)
    current = hook_pin(ent)
    _write_pin(state_dir, ent, current)
    return current


_ANNEAL_BIN = re.compile(r'^_INSTALL_ANNEAL_BIN\s*=\s*"([^"]*)"', re.M)


def _check_hook_executables(entity: Path, roots: Sequence[Path]) -> list[str]:
    """The installer bakes the anneal-memory binary's path into _levain_hook.py; hooks run it
    outside the sandbox, so it must not be something the entity can write. Fails closed: a
    missing file or an unrecognised shape refuses (L1 r1 #9)."""
    src = entity / "activation" / "hooks" / "_levain_hook.py"
    try:
        m = _ANNEAL_BIN.search(src.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"cannot read {os.fspath(src)!r}: {exc}"]
    if not m:
        return [f"{os.fspath(src)!r} no longer names the anneal binary the way this check reads it"]
    if not m.group(1) or "{{" in m.group(1):
        return []  # unsubstituted: the hook resolves anneal itself (from PATH, which host_env filters)
    return _check_exec(m.group(1), roots, "the anneal-memory binary the hooks run")


def cc_mcp_config(entity: str | os.PathLike[str], python: str) -> dict[str, Any]:
    """The hosted session's one MCP server, rendered from the INSTALLED package's template the way
    the installer renders it (`_claude_code_files`); the workspace's .mcp.json is never read."""
    ent = _real(entity)
    text = _CC_MCP_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{INSTALL_DIR}}", json.dumps(str(ent), ensure_ascii=False)[1:-1])
    text = text.replace("{{PYTHON}}", json.dumps(python, ensure_ascii=False)[1:-1])
    cfg = json.loads(text)
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict) or set(servers) != {"anneal_memory"}:
        raise HostRefusal("the claude-code MCP template no longer names exactly anneal_memory")
    return cfg


def _check_mcp(cfg: Mapping[str, Any], entity: Path, roots: Sequence[Path]) -> list[str]:
    server = cfg["mcpServers"]["anneal_memory"]
    problems = _check_exec(server.get("command", ""), roots, "the MCP server's interpreter")
    if set(server) - {"type", "command", "args"}:
        problems.append(f"the MCP server carries keys this check does not vet: {sorted(set(server) - {'type', 'command', 'args'})}")
    args = list(server.get("args", []))
    db = str(entity / ".levain" / "memory.db")
    for i, a in enumerate(args):
        if i > 0 and args[i - 1] == "--db":
            if a != db:
                problems.append(f"the MCP server's --db is {a!r}, expected {db!r}")
        elif isinstance(a, str) and "/" in a:
            problems += _outside(a, roots, "an MCP server argument")
    return problems


_LOOSENING_TRUE = (("allowUnsandboxedCommands",), ("enableWeakerNestedSandbox",),
                   ("enableWeakerNetworkIsolation",), ("network", "allowAllUnixSockets"),
                   ("network", "allowLocalBinding"))
_LOOSENING_NONEMPTY = (("excludedCommands",), ("filesystem", "allowWrite"),
                       ("network", "allowUnixSockets"), ("network", "allowedDomains"))


def _get(d: Any, keys: Sequence[str]) -> Any:
    for k in keys:
        if not isinstance(d, Mapping) or k not in d:
            return _MISSING
        d = d[k]
    return d


def load_managed_settings(platform: str = sys.platform) -> list[tuple[str, Any]]:
    """Every managed settings document on this machine as (path, parsed JSON). Unparseable files
    are returned as (path, _MISSING) so the check refuses rather than skipping them."""
    base = _MANAGED_SETTINGS.get("linux" if platform.startswith("linux") else platform)
    if base is None:
        return []
    docs: list[tuple[str, Any]] = []
    files = [base / "managed-settings.json"]
    dropins = base / "managed-settings.d"
    if dropins.is_dir():
        files += sorted(dropins.glob("*.json"))
    for f in files:
        if f.exists():
            try:
                docs.append((str(f), json.loads(f.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                docs.append((str(f), _MISSING))
    mcp = base / "managed-mcp.json"
    if mcp.exists():
        docs.append((str(mcp), {"__managed_mcp__": True}))
    return docs


def check_managed_settings(docs: Sequence[tuple[str, Any]], roots: Sequence[Path]) -> list[str]:
    """Managed settings still load under --restricted and their arrays merge with ours, so they
    can loosen the sandbox or add code that runs outside it. ACCEPTED, by allowlist: top-level
    keys hooks/sandbox/permissions; permission deny/ask lists only (an allow rule would answer a
    Write before the host does); sandbox keys this module pins, with values that do not loosen;
    and hook commands whose code is fixed outside the writable roots. Everything else refuses,
    an unknown key included (L1 r1 #7: the list form failed open on keys it did not know).

    A hook command may be shell (the 10-07 flow guard under /Library is), so it is read as shell
    words: every word containing a slash must be an absolute path outside the roots, a script-
    looking word must be absolute, and nothing may name a cwd- or home-relative variable."""
    problems: list[str] = []
    for path, doc in docs:
        if doc is _MISSING:
            problems.append(f"managed settings {path!r} could not be parsed")
            continue
        if not isinstance(doc, Mapping):
            problems.append(f"managed settings {path!r} is not an object")
            continue
        if doc.get("__managed_mcp__"):
            problems.append(f"managed MCP servers ({path!r}) would load into the hosted session")
            continue
        for k in set(doc) - _MANAGED_TOP_KEYS:
            problems.append(f"{path}: managed key {k!r} is not one hosting can vet")
        perms = doc.get("permissions", {})
        if not isinstance(perms, Mapping) or set(perms) - _MANAGED_PERMISSION_KEYS:
            problems.append(f"{path}: managed permissions may only hold deny/ask lists")
        sb = doc.get("sandbox", {})
        if not isinstance(sb, Mapping):
            problems.append(f"{path}: sandbox is not an object")
            sb = {}
        for k in set(sb) - _MANAGED_SANDBOX_KEYS:
            problems.append(f"{path}: sandbox.{k} is not a key hosting can vet")
        for sub, allowed in (("network", _MANAGED_NETWORK_KEYS), ("filesystem", _MANAGED_FILESYSTEM_KEYS)):
            node = sb.get(sub, {})
            for k in (set(node) - allowed) if isinstance(node, Mapping) else {"<not an object>"}:
                problems.append(f"{path}: sandbox.{sub}.{k} is not a key hosting can vet")
        for keys in _LOOSENING_TRUE:
            if _get(sb, keys) is True:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is true")
        if _get(sb, ("filesystem", "disabled")) is True:
            problems.append(f"{path}: sandbox.filesystem.disabled is true")
        if _get(sb, ("network", "strictAllowlist")) is False:
            problems.append(f"{path}: sandbox.network.strictAllowlist is false")
        for keys in (("enabled",), ("failIfUnavailable",)):
            if _get(sb, keys) is False:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is false")
        for keys in _LOOSENING_NONEMPTY:
            v = _get(sb, keys)
            if v is not _MISSING and v:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is not empty")
        hooks = doc.get("hooks", {})
        if not isinstance(hooks, Mapping):
            problems.append(f"{path}: hooks is not an object")
            continue
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else [None]:
                hs = group.get("hooks") if isinstance(group, Mapping) else None
                for h in hs if isinstance(hs, list) else [None]:
                    cmd = h.get("command") if isinstance(h, Mapping) else None
                    if not isinstance(cmd, str) or not isinstance(h, Mapping) or h.get("type") != "command":
                        problems.append(f"{path}: {event} hook is not a plain command")
                        continue
                    problems += [f"{path}: {event} hook: {p}" for p in _vet_shell_words(cmd, roots)]
    return problems


def host_env(environ: Mapping[str, str] | None = None, *,
             roots: Sequence[Path] = (), pycache: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """An allowlist, never a scrub: nothing reaches the entity's process (and every shell and hook
    it starts) unless named here. That keeps out Claude Code's own messaging socket and token
    (CLAUDE_CODE_*), API keys and the cockpit's token without having to name them. PATH keeps only
    absolute entries outside the writable roots, because hooks run `git` and anneal from it
    unsandboxed (L1 r1 #9). PYTHONPYCACHEPREFIX points every Python a hook starts at the
    host-owned cache, so a .pyc planted in the workspace is never read (L1 r1 #8)."""
    environ = os.environ if environ is None else environ
    env = {k: v for k, v in environ.items() if k in _ENV_ALLOW or k.startswith(_ENV_ALLOW_PREFIXES)}
    if "PATH" in env:
        keep = [p for p in env["PATH"].split(os.pathsep)
                if p and os.path.isabs(p) and not _outside_problems(p, roots)]
        env["PATH"] = os.pathsep.join(keep)
    if pycache is not None:
        env["PYTHONPYCACHEPREFIX"] = os.fspath(pycache)
    return env


_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_cc_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ---------------------------------------------------------------------------- the profile


@dataclass(frozen=True)
class CCHostProfile:
    """Everything a hosted Claude Code spawn passes, built once per host session. ``argv()`` is
    the session's spawn; ``argv(canary=True)`` differs from it in exactly the three flags that do
    not touch security (no persistence, the canary's model), which a test asserts."""

    entity: Path
    claude_bin: str
    settings: Mapping[str, Any]
    mcp_config: Mapping[str, Any]
    env: Mapping[str, str]
    run_dir: Path
    state_dir: Path
    pin_status: str
    tools: tuple[str, ...] = CC_HOSTED_TOOLS
    roots: tuple[Path, ...] = field(default=())

    def argv(self, *, canary: bool = False, canary_model: str = "haiku") -> list[str]:
        argv = [
            self.claude_bin, "-p",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-hook-events",
            "--restricted",
            "--permission-mode", "default",
            "--permission-prompt-tool", "stdio",
            "--disable-slash-commands",
            "--tools", ",".join(self.tools),
            "--strict-mcp-config", "--mcp-config", json.dumps(self.mcp_config, sort_keys=True),
            "--settings", json.dumps(self.settings, sort_keys=True),
        ]
        if canary:
            argv += ["--no-session-persistence", "--model", canary_model]
        return argv

    def digest(self) -> str:
        blob = json.dumps([self.argv(), sorted(self.env)], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def cc_host_profile(entity: str | os.PathLike[str], *, claude_bin: str, python: str | None = None,
                    platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                    home: Path | None = None,
                    managed: Sequence[tuple[str, Any]] | None = None) -> CCHostProfile:
    """Build the hosted profile, or raise HostRefusal naming every problem found."""
    ent = _real(entity)
    if not (ent / ".levain").is_dir():
        raise HostRefusal(f"{os.fspath(ent)!r} is not a Levain install")
    roots = cc_writable_roots(ent)
    py = python or sys.executable
    problems: list[str] = []
    problems += _check_exec(py, roots, "the Python that runs the hooks and the MCP server")
    problems += _check_exec(claude_bin, roots, "the claude binary")
    if problems:
        raise HostRefusal("; ".join(problems))
    run = host_dir(ent, "run", roots=roots, platform=platform, environ=environ, home=home)
    state = host_dir(ent, "state", roots=roots, platform=platform, environ=environ, home=home)
    pycache = run / "pycache"
    pycache.mkdir(mode=0o700, exist_ok=True)
    sandbox = cc_sandbox_block(ent)
    # includeGitInstructions false: CC's own startup `git status` snapshot runs outside the
    # sandbox in the entity dir, where a planted fsmonitor would run with it (L2 r1 #7).
    settings = {"sandbox": sandbox, "includeGitInstructions": False,
                "hooks": cc_hook_settings(ent, py, pycache)}
    mcp = cc_mcp_config(ent, py)
    problems += check_sandbox_types(sandbox)
    problems += _check_hook_executables(ent, roots)
    problems += _check_mcp(mcp, ent, roots)
    problems += _check_python_paths(py, roots, host_env(environ, roots=roots))
    problems += check_managed_settings(load_managed_settings(platform) if managed is None else managed, roots)
    if managed is None:
        problems += check_shared_code(ent, roots, home=home)
    if problems:
        raise HostRefusal("; ".join(problems))
    pin_status = check_or_record_pin(ent, state)
    return CCHostProfile(entity=ent, claude_bin=claude_bin, settings=settings, mcp_config=mcp,
                         env={**host_env(environ, roots=roots, pycache=pycache),
                              "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}, run_dir=run,
                         state_dir=state, pin_status=pin_status, roots=roots)


def file_write_decision(entity: str | os.PathLike[str], tool_name: str,
                        tool_input: Mapping[str, Any]) -> Literal["allow", "refuse", "ask"]:
    """D2, the host's answer to an Edit/Write prompt: refuse anything that is, or is inside, a
    protected name; allow other paths inside the workspace; send everything else to the human.

    Decided by FILE IDENTITY, not spelling (L1 r1 #1, #3): APFS is case- and normalization-
    insensitive, so `ACTIVATION/hooks/x.py` is the hook, and `link/../x` resolves through the
    link. So a path with a `..` component refuses outright; the deepest existing ancestor is
    resolved by the OS; "inside the workspace" means some ancestor IS the entity directory
    (same device and inode); "protected" means some ancestor IS a protected directory, or a
    component below the entity folds to a protected name. A hard-linked target refuses too: its
    other name may be a hook. CC performs the write after this answer, so a symlink swapped in
    between is CC's confinement's to stop (D10, residue R4)."""
    if tool_name not in ("Edit", "Write"):
        return "ask"
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw or "\0" in raw:
        return "refuse"
    ent = _real(entity)
    p = Path(raw) if os.path.isabs(raw) else ent / raw
    if ".." in p.parts:
        return "refuse"
    try:
        ent_id = _ident(os.stat(ent))
        protected_ids = {_ident(os.stat(ent / n)) for n in PROTECTED_NAMES if os.path.lexists(ent / n)}
        tail: list[str] = []
        q = p
        while not os.path.lexists(q):
            tail.insert(0, q.name)
            if q.parent == q:
                return "ask"
            q = q.parent
        if tail == [] and os.lstat(q).st_nlink > 1 and stat.S_ISREG(os.lstat(q).st_mode):
            return "refuse"
        anc = _real(q)
        below: list[str] = list(tail)
        inside = False
        for a in (anc, *anc.parents):
            ident = _ident(os.stat(a))
            if ident in protected_ids:
                return "refuse"
            if ident == ent_id:
                inside = True
                break
            below.insert(0, a.name)
    except OSError:
        return "refuse"
    if not inside:
        return "ask"
    if any(_fold(part) in _PROTECTED_FOLDED for part in below):
        return "refuse"
    return "allow"


_BROKER_METHODS = frozenset({
    "initialize", "thread/start", "thread/resume", "thread/read", "thread/list",
    "thread/loaded/list", "thread/unsubscribe", "turn/start", "turn/interrupt", "model/list",
    "hooks/list",
})


def broker_method_allowed(method: str) -> bool:
    return method in _BROKER_METHODS


def codex_sandbox_policy() -> dict[str, Any]:
    """The policy the broker writes onto every thread start/resume and turn start: the entity's
    cwd writable, nothing else (no /tmp, no $TMPDIR), no network."""
    return {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
            "excludeTmpdirEnvVar": True, "excludeSlashTmp": True}


def overwrite_codex_policy(method: str, params: Mapping[str, Any], *,
                           cwd: str | os.PathLike[str]) -> dict[str, Any]:
    """The broker never validates-and-forwards client policy: it rebuilds the params from an
    allowlist and writes its own. thread/start and thread/resume take a ``sandbox`` mode string,
    turn/start a ``sandboxPolicy`` object. Approval policy ``untrusted`` sends every command to
    the broker as a request (D11, the S4/S10 slices may refine it). Methods not listed here
    carry no execution policy and pass through unchanged."""
    if method == "initialize":
        return codex_initialize_params()
    if method not in _CODEX_FORWARD:
        return dict(params)
    out = {k: v for k, v in params.items() if k in _CODEX_FORWARD[method]}
    out["cwd"] = os.fspath(cwd)
    out["approvalPolicy"] = "untrusted"
    out["approvalsReviewer"] = "user"  # never auto_review: a subagent would approve escapes (L2 r1 #1)
    if method == "turn/start":
        out["sandboxPolicy"] = codex_sandbox_policy()
    else:
        out["sandbox"] = "workspace-write"
    return out


def check_codex_effective_policy(sandbox: Mapping[str, Any], *,
                                 forbidden: Sequence[Path]) -> list[str]:
    """Run on what the server REPORTS back (thread/start and thread/resume results). Network must be
    explicitly false for every type, readOnly included; no writable root may contain, or sit
    inside, the host run dir, CODEX_HOME or the hook and MCP code (L1 r1 #5a: one direction
    let a root inside CODEX_HOME through)."""
    t = sandbox.get("type")
    if t not in ("readOnly", "workspaceWrite"):
        return [f"Codex sandbox type {t!r} is not hostable"]
    problems = []
    if sandbox.get("networkAccess", _MISSING) is not False:
        problems.append(f"Codex sandbox networkAccess is {sandbox.get('networkAccess', 'absent')!r}, must be false")
    if t == "workspaceWrite":
        roots = sandbox.get("writableRoots", _MISSING)
        if not isinstance(roots, list):
            problems.append("Codex sandbox writableRoots is missing")
        else:
            for r in roots:
                if not isinstance(r, str) or not os.path.isabs(r):
                    problems.append(f"Codex writable root {r!r} is not an absolute path")
                    continue
                for f in forbidden:
                    if is_within(f, r) or is_within(r, f):
                        problems.append(f"Codex writable root {r!r} overlaps {os.fspath(f)!r}")
        for k in ("excludeSlashTmp", "excludeTmpdirEnvVar"):
            if sandbox.get(k) is not True:
                problems.append(f"Codex sandbox {k} is {sandbox.get(k, 'absent')!r}, must be true")
    return problems


def check_codex_hooks(hooks_list: Mapping[str, Any], required_sources: Sequence[str]) -> list[str]:
    """``hooks/list``: every hook whose source file is one of Levain's must be ``trusted`` (an
    untrusted hook is skipped silently in headless, so "same entity" would fail invisibly). Writing
    the trust stays the operator's act in the TUI."""
    problems = []
    seen = set()
    for entry in hooks_list.get("data", []):
        for h in entry.get("hooks", []):
            src = h.get("sourcePath")
            if src in required_sources:
                seen.add(src)
                if h.get("trustStatus") not in ("trusted", "managed"):
                    problems.append(f"Codex hook {h.get('key')!r} is {h.get('trustStatus')!r}, not trusted")
    for src in required_sources:
        if src not in seen:
            problems.append(f"Codex does not list Levain's hooks from {src!r}")
    return problems


_SHELL_META = re.compile(r"[;&|<>`$(){}\n]")


def check_codex_code_paths(hooks_json: Mapping[str, Any], mcp_servers: Mapping[str, Any],
                           roots: Sequence[Path]) -> list[str]:
    """Code a Codex entity runs outside its sandbox (hooks, MCP servers) must be an exact argv:
    an interpreter outside every writable root, its isolating flags BEFORE the script (a flag
    after the script is the script's argument, L1 r1 #5b), a script outside the writable roots,
    no shell composition, no relative path. MCP servers get the same rule and may not carry
    env or cwd keys (L1 r1 #5c). Today's Codex install puts its hooks under the entity's own
    writable root, so this refuses it by design (plan D6: S4/S5 move them)."""
    problems: list[str] = []
    for event, groups in (hooks_json.get("hooks") or {}).items():
        for group in groups or []:
            for h in group.get("hooks", []):
                problems += _vet_codex_argv(h.get("command", ""), roots, f"Codex {event} hook", ("-I", "-B"))
    for name, server in (mcp_servers or {}).items():
        extra = set(server) - {"command", "args", "type", "enabled", "startup_timeout_sec", "tool_timeout_sec"}
        if extra:
            problems.append(f"Codex MCP server {name!r} carries keys hosting cannot vet: {sorted(extra)}")
        args = [str(a) for a in server.get("args", [])]
        db_values = {args[i + 1] for i, a in enumerate(args[:-1]) if a == "--db"}
        argv = [str(server.get("command", ""))] + [a for a in args if a not in db_values]
        problems += _vet_codex_argv(shlex.join(argv), roots, f"Codex MCP server {name!r}", ("-I",))
    return problems


def codex_spawn_env(codex_home: str | os.PathLike[str],
                    environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = host_env(environ)
    env["CODEX_HOME"] = os.fspath(codex_home)
    return env


def _check_exec(path: str, roots: Sequence[Path], what: str) -> list[str]:
    """An executable Levain spawns outside the sandbox: absolute, and neither the path as given
    (every lexical ancestor, since a symlink there could be re-pointed) nor its resolution may
    sit inside a writable root. A venv interpreter's prefix (pyvenv.cfg beside bin/) is checked
    too, because its site-packages is code (L1 r1 #6)."""
    if not os.path.isabs(path):
        return [f"{what} is not an absolute path: {path!r}"]
    problems = []
    lexical = Path(os.path.abspath(path))
    for anc in (lexical, *lexical.parents):
        for root in roots:
            if anc == root or root in anc.parents or _real(anc) == root or root in _real(anc).parents:
                problems.append(f"{what} {path!r} passes through {os.fspath(anc)!r}, which the entity can write")
                break
        if problems:
            break
    problems += _outside(path, roots, what)
    venv = lexical.parent.parent
    if (venv / "pyvenv.cfg").exists():
        problems += _outside(venv, roots, f"the venv behind {what}")
    return problems


_MANAGED_TOP_KEYS = frozenset({"hooks", "sandbox", "permissions"})


_MANAGED_PERMISSION_KEYS = frozenset({"deny", "ask"})


_MANAGED_SANDBOX_KEYS = frozenset({k[0] for k in _SANDBOX_TYPES} | {"network", "filesystem"})


_MANAGED_NETWORK_KEYS = frozenset({"allowLocalBinding", "allowUnixSockets", "allowAllUnixSockets",
                                   "strictAllowlist", "deniedDomains"})


_MANAGED_FILESYSTEM_KEYS = frozenset({"denyWrite", "denyRead", "disabled"})


_CWD_VARS = re.compile(r"\$\{?(CLAUDE_PROJECT_DIR|PWD|OLDPWD|HOME)\b|~")


_SCRIPT_EXT = re.compile(r"\.(py|sh|bash|zsh|js|mjs|ts|rb|pl)$")


def _vet_shell_words(cmd: str, roots: Sequence[Path]) -> list[str]:
    if _CWD_VARS.search(cmd):
        return [f"names a cwd- or home-relative location: {cmd!r}"]
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return [f"cannot be parsed as shell words: {cmd!r}"]
    problems = []
    for w in words:
        if set(w) <= set("();<>|&"):
            continue  # an operator, split off by punctuation_chars
        cand = w.split("=", 1)[1] if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w) else w
        if not cand:
            continue
        if "/" in cand and not cand.startswith("/"):
            problems.append(f"names a relative path {cand!r}")
        elif cand.startswith("/"):
            problems += _outside(cand, roots, "a path")
        elif _SCRIPT_EXT.search(cand):
            problems.append(f"runs a script by relative name {cand!r}")
    return problems


def _outside_problems(p: str, roots: Sequence[Path]) -> bool:
    return any(is_within(p, r) for r in roots)


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


_PROTECTED_FOLDED = frozenset(_fold(n) for n in PROTECTED_NAMES)


def _ident(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


# Client params the broker forwards, per method (codex 0.146.1 schema: ThreadStartParams,
# ThreadResumeParams, TurnStartParams). Everything else a client sends is dropped, and the
# broker writes cwd, sandbox and approval policy itself (L1 r1 #5d: a key denylist forwarded
# cwd, approvalPolicy and config untouched).


_CODEX_FORWARD = {
    "thread/start": frozenset({"model"}),
    "thread/resume": frozenset({"threadId", "model"}),
    "turn/start": frozenset({"threadId", "input", "model", "effort", "summary", "clientUserMessageId"}),
}


def _vet_codex_argv(cmd: str, roots: Sequence[Path], what: str, flags: tuple[str, ...]) -> list[str]:
    if _SHELL_META.search(cmd):
        return [f"{what} uses shell syntax: {cmd!r}"]
    argv = shlex.split(cmd)
    if not argv:
        return [f"{what} is empty"]
    problems = _check_exec(argv[0], roots, f"{what} interpreter")
    i = 1
    seen: set[str] = set()
    while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-m", "-c"):
        seen.add(argv[i])
        i += 1
    missing = [f for f in flags if f not in seen]
    if missing:
        problems.append(f"{what} does not run with {' '.join(missing)} before its script: {cmd!r}")
    for a in argv[i:]:
        if "/" in a and not os.path.isabs(a):
            problems.append(f"{what} names a relative path {a!r}")
        elif os.path.isabs(a):
            problems += _outside(a, roots, f"{what} path")
        elif _SCRIPT_EXT.search(a):
            problems.append(f"{what} runs a script by relative name {a!r}")
    return problems


def codex_initialize_params() -> dict[str, Any]:
    """The broker sends its own initialize. A client's would be able to opt into
    capabilities.experimentalApi, which unlocks `permissions`, `dynamicTools` and `process/*`
    (the last runs outside Codex's sandbox per its docs; L2 r1 #1)."""
    return {"clientInfo": {"name": "levain-broker", "version": "1"},
            "capabilities": {"experimentalApi": False}}


def _check_python_paths(python: str, roots: Sequence[Path], env: Mapping[str, str]) -> list[str]:
    """Hooks and the MCP server run Python outside the sandbox, so everything that Python imports
    from must be outside the writable roots: the interpreter path is not enough (L2 r1 #9: .pth
    files, editable installs and site dirs add code). Asks the interpreter itself, the way the
    MCP server starts (`-P`), with the hosted env. Fails closed."""
    code = ("import json, sys\n"
            "import anneal_memory\n"
            "print(json.dumps([anneal_memory.__file__] + [p for p in sys.path if p]))")
    try:
        out = subprocess.run([python, "-P", "-c", code], env=dict(env), capture_output=True,
                             text=True, timeout=30, check=True).stdout
        paths = json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return [f"could not read the hook interpreter's import path: {exc}"]
    problems = []
    for p in paths:
        if os.path.isabs(p) and os.path.exists(p):
            problems += _outside(p, roots, "a directory the hook interpreter imports from")
    return problems


def check_shared_code(entity: Path, roots: Sequence[Path], *, home: Path | None = None) -> list[str]:
    """Code that OTHER processes run outside any sandbox must not live where the hosted entity
    can write it: the targets of the operator's ~/.claude entries (skills, agents, plugins,
    settings), the scripts their user-level hooks run, and personal plugins (L2 r1 #4). An
    entity whose directory holds the operator's own tooling is refused: hosting it would let the
    entity rewrite what the operator's next terminal session runs. Launchd/cron scripts and
    shell rc files are outside this check (stated residue)."""
    home = Path(os.path.expanduser("~")) if home is None else home
    cc = home / ".claude"
    problems: list[str] = []
    if not cc.is_dir():
        return problems
    for entry in sorted(cc.iterdir()):
        if entry.name in ("projects", "jobs", "todos", "shell-snapshots", "statsig", "logs", "cache"):
            continue  # Claude Code's own data, not code it loads
        problems += [p for p in _outside(_real(entry), roots, f"~/.claude/{entry.name}")
                     if not entry.name.startswith(".")]
    settings = cc / "settings.json"
    try:
        doc = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (OSError, ValueError):
        return problems + [f"{os.fspath(settings)!r} could not be parsed"]
    for event, groups in (doc.get("hooks") or {}).items():
        for group in groups if isinstance(groups, list) else []:
            for h in (group.get("hooks") or []) if isinstance(group, Mapping) else []:
                cmd = h.get("command") if isinstance(h, Mapping) else None
                if not isinstance(cmd, str):
                    continue
                try:
                    words = shlex.split(cmd)
                except ValueError:
                    continue
                for w in words:
                    w = os.path.expanduser(w.replace("$HOME", os.fspath(home)))
                    if os.path.isabs(w) and os.path.exists(w):
                        problems += _outside(_real(w), roots, f"a script the operator's {event} hook runs")
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m levain.host_guard repin <entity>``: the operator's explicit repin after changing
    an entity's hook code (D9). Prints the old and new pin so the change is on the record."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.host_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("repin", help="re-record the hook-tree pin for an entity")
    rp.add_argument("entity")
    a = ap.parse_args(argv)
    ent = _real(a.entity)
    try:
        state = host_dir(ent, "state", roots=cc_writable_roots(ent))
        pf = _pin_file(state)
        old = json.loads(pf.read_text(encoding="utf-8")).get("pin") if pf.exists() else None
        new = repin(ent, state)
    except (HostRefusal, OSError, ValueError) as exc:
        print(f"repin refused: {exc}", file=sys.stderr)
        return 1
    print(f"{ent}: hook pin {old or '(none)'} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
