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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "HostRefusal", "CCHostProfile", "cc_host_profile", "cc_writable_roots", "is_within",
    "host_dir", "cc_sandbox_block", "check_sandbox_types", "cc_hook_settings", "cc_mcp_config",
    "hook_pin", "check_or_record_pin", "repin", "check_managed_settings", "host_env",
    "file_write_decision", "parse_cc_version", "CC_HOSTED_TOOLS", "CC_VERSION_FLOOR",
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
        "network": {"allowLocalBinding": False, "allowUnixSockets": [], "allowAllUnixSockets": False},
        "filesystem": {"denyWrite": [str(ent / "activation"), str(ent / ".levain")]},
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
    bytes). Any symlink in the tree refuses: a link is a pointer to code the pin cannot see."""
    tree = _hook_tree(_real(entity))
    h = hashlib.sha256()
    for root, dirs, files in os.walk(tree, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                raise HostRefusal(f"symlink in the hook tree: {os.path.join(root, d)!r}")
        for f in sorted(files):
            p = os.path.join(root, f)
            if os.path.islink(p):
                raise HostRefusal(f"symlink in the hook tree: {p!r}")
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
    outside the sandbox, so it must not be something the entity can write."""
    src = entity / "activation" / "hooks" / "_levain_hook.py"
    try:
        m = _ANNEAL_BIN.search(src.read_text(encoding="utf-8"))
    except OSError:
        return []
    if not m or not m.group(1) or "{{" in m.group(1):
        return []
    return _outside(m.group(1), roots, "the anneal-memory binary the hooks run")


# ---------------------------------------------------------------------------- MCP


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
    problems = _outside(server.get("command", ""), roots, "the MCP server's interpreter")
    args = list(server.get("args", []))
    db = str(entity / ".levain" / "memory.db")
    for i, a in enumerate(args):
        if i > 0 and args[i - 1] == "--db":
            if a != db:
                problems.append(f"the MCP server's --db is {a!r}, expected {db!r}")
        elif isinstance(a, str) and a.startswith("/"):
            problems += _outside(a, roots, "an MCP server argument")
    return problems


# ---------------------------------------------------------------------------- managed settings


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
    """Managed settings still load under --restricted and their arrays merge with ours, so they can
    loosen the sandbox or add code that runs outside it. Refuse any loosening key, any MCP server,
    and any hook command that names a path inside a root the entity can write. A managed hook whose
    code sits outside those roots (the 10-07 flow guard under /Library) is the admin's and stays."""
    problems: list[str] = []
    for path, doc in docs:
        if doc is _MISSING:
            problems.append(f"managed settings {path!r} could not be parsed")
            continue
        if isinstance(doc, Mapping) and doc.get("__managed_mcp__"):
            problems.append(f"managed MCP servers ({path!r}) would load into the hosted session")
            continue
        sb = doc.get("sandbox", {}) if isinstance(doc, Mapping) else {}
        for keys in _LOOSENING_TRUE:
            if _get(sb, keys) is True:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is true")
        if _get(sb, ("enabled",)) is False:
            problems.append(f"{path}: sandbox.enabled is false")
        for keys in _LOOSENING_NONEMPTY:
            v = _get(sb, keys)
            if v is not _MISSING and v:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is not empty")
        if isinstance(doc, Mapping) and doc.get("mcpServers"):
            problems.append(f"{path}: declares MCP servers")
        hooks = doc.get("hooks", {}) if isinstance(doc, Mapping) else {}
        for event, groups in (hooks.items() if isinstance(hooks, Mapping) else []):
            for group in groups or []:
                for h in (group.get("hooks", []) if isinstance(group, Mapping) else []):
                    cmd = h.get("command") if isinstance(h, Mapping) else None
                    if not isinstance(cmd, str):
                        continue
                    try:
                        tokens = shlex.split(cmd)
                    except ValueError:
                        problems.append(f"{path}: {event} hook command cannot be parsed")
                        continue
                    for tok in tokens:
                        for cand in (tok, tok.split("=", 1)[1] if "=" in tok else ""):
                            if cand.startswith("/"):
                                problems += [f"{path}: {event} hook: {p}" for p in _outside(cand, roots, "a path")]
    return problems


# ---------------------------------------------------------------------------- env, version


def host_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """An allowlist, never a scrub: nothing reaches the entity's process (and every shell it
    starts) unless named here. That keeps out Claude Code's own messaging socket and token
    (CLAUDE_CODE_*), API keys and the cockpit's token without having to name them."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k in _ENV_ALLOW or k.startswith(_ENV_ALLOW_PREFIXES)}


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
    problems += _outside(py, roots, "the Python that runs the hooks and the MCP server")
    problems += _outside(_real(claude_bin), roots, "the claude binary")
    if problems:
        raise HostRefusal("; ".join(problems))
    run = host_dir(ent, "run", roots=roots, platform=platform, environ=environ, home=home)
    state = host_dir(ent, "state", roots=roots, platform=platform, environ=environ, home=home)
    pycache = run / "pycache"
    pycache.mkdir(mode=0o700, exist_ok=True)
    sandbox = cc_sandbox_block(ent)
    settings = {"sandbox": sandbox, "hooks": cc_hook_settings(ent, py, pycache)}
    mcp = cc_mcp_config(ent, py)
    problems += check_sandbox_types(sandbox)
    problems += _check_hook_executables(ent, roots)
    problems += _check_mcp(mcp, ent, roots)
    problems += check_managed_settings(load_managed_settings(platform) if managed is None else managed, roots)
    if problems:
        raise HostRefusal("; ".join(problems))
    pin_status = check_or_record_pin(ent, state)
    return CCHostProfile(entity=ent, claude_bin=claude_bin, settings=settings, mcp_config=mcp,
                         env=host_env(environ), run_dir=run, state_dir=state,
                         pin_status=pin_status, roots=roots)


# ---------------------------------------------------------------------------- file-tool policy


def file_write_decision(entity: str | os.PathLike[str], tool_name: str,
                        tool_input: Mapping[str, Any]) -> Literal["allow", "refuse", "ask"]:
    """D2, the host's answer to an Edit/Write prompt: refuse any path naming a protected component
    (lexically or after resolution); allow other paths inside the workspace; send everything else
    to the human. CC performs the write after this answer, so a symlink swapped between the two is
    CC's confinement's to stop (D10, the plan's residue R4)."""
    if tool_name not in ("Edit", "Write"):
        return "ask"
    raw = tool_input.get("file_path")
    if not isinstance(raw, str) or not raw:
        return "refuse"
    ent = _real(entity)
    lexical = Path(os.path.normpath(raw if os.path.isabs(raw) else ent / raw))
    resolved = _real(lexical)
    # Protected if EITHER spelling names a protected component below the entity: the resolved path
    # (a symlink into activation/) and the lexical one (activation/ itself replaced by a link).
    rels = []
    if is_within(resolved, ent):
        rels.append(resolved.relative_to(ent))
    for anc in (lexical, *lexical.parents):
        if _real(anc) == ent:
            rels.append(lexical.relative_to(anc))
            break
    if any(part in PROTECTED_NAMES for rel in rels for part in rel.parts):
        return "refuse"
    if not is_within(resolved, ent):
        return "ask"
    return "allow"


# ---------------------------------------------------------------------------- Codex (S4's broker calls these)


# The client methods a Levain broker forwards. Everything else, including every method a
# future Codex adds, is refused. Measured or schema-stated reasons for the notable refusals:
# thread/shellCommand runs unsandboxed (its own schema text; probes 2c), command/exec and fs/*
# act on the host directly, config/* and plugin/* change what the next turn runs, and
# thread/approveGuardianDeniedAction answers an approval outside the broker's own request map.
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


def overwrite_codex_policy(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The broker never validates-and-forwards client policy: it replaces it. thread/start and
    thread/resume take a ``sandbox`` mode string, turn/start a ``sandboxPolicy`` object (shapes
    from the 10-06 captures and codex 0.146.1's generated schema)."""
    out = {k: v for k, v in params.items() if k not in ("sandbox", "sandboxPolicy", "permissionProfile")}
    if method in ("thread/start", "thread/resume"):
        out["sandbox"] = "workspace-write"
    elif method == "turn/start":
        out["sandboxPolicy"] = codex_sandbox_policy()
    return out


def check_codex_effective_policy(sandbox: Mapping[str, Any], *,
                                 forbidden: Sequence[Path]) -> list[str]:
    """Run on what the server REPORTS back (thread/start and thread/resume results). Network must be
    explicitly false for every type, readOnly included; writable roots must stay clear of the host
    run dir, CODEX_HOME and the hook and MCP code."""
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
                for f in forbidden:
                    if not isinstance(r, str) or is_within(f, r):
                        problems.append(f"Codex writable root {r!r} covers {os.fspath(f)!r}")
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
    an interpreter and script outside every writable root, no shell composition, and for hooks
    ``-I -B``. Today's Codex install puts its hooks under the entity's own writable root, so this
    refuses it by design (plan D6: S4/S5 move them)."""
    problems: list[str] = []
    for event, groups in (hooks_json.get("hooks") or {}).items():
        for group in groups or []:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if _SHELL_META.search(cmd):
                    problems.append(f"Codex {event} hook uses shell syntax: {cmd!r}")
                    continue
                argv = shlex.split(cmd)
                if len(argv) < 2:
                    problems.append(f"Codex {event} hook is not an interpreter plus a script: {cmd!r}")
                    continue
                problems += _outside(argv[0], roots, f"Codex {event} hook interpreter")
                flags = [a for a in argv[1:] if a.startswith("-")]
                if not {"-I", "-B"} <= set(flags):
                    problems.append(f"Codex {event} hook does not run with -I -B: {cmd!r}")
                scripts = [a for a in argv[1:] if not a.startswith("-")]
                if not scripts:
                    problems.append(f"Codex {event} hook names no script: {cmd!r}")
                else:
                    problems += _outside(scripts[0], roots, f"Codex {event} hook script")
    for name, server in (mcp_servers or {}).items():
        cmd = server.get("command", "")
        problems += _outside(cmd, roots, f"Codex MCP server {name!r} command")
        args = list(server.get("args", []))
        for i, a in enumerate(args):
            if isinstance(a, str) and a.startswith("/") and not (i > 0 and args[i - 1] == "--db"):
                problems += _outside(a, roots, f"Codex MCP server {name!r} argument")
    return problems


def codex_spawn_env(codex_home: str | os.PathLike[str],
                    environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = host_env(environ)
    env["CODEX_HOME"] = os.fspath(codex_home)
    return env


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
