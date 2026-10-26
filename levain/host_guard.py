"""Hosting preconditions for driving a Claude Code or Codex primary from the cockpit (chat S2).

The cockpit adds an HTTP route that answers approvals. An entity that can reach it can approve
its own held actions, which removes the human gate a terminal prompt keeps. So a hosted session
must hold one property: no code the entity can influence runs outside the OS sandbox while the
session is hosted. The shape was ruled on 10-06 (Claude Code: CC's own sandbox with loopback
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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

__all__ = [
    "HostRefusal", "CCHostProfile", "cc_host_profile", "cc_writable_roots", "is_within",
    "host_dir", "cc_sandbox_block", "check_sandbox_types", "cc_hook_settings", "cc_mcp_config",
    "hook_pin", "check_or_record_pin", "repin", "agent_hook_pins", "check_agent_hook_pins",
    "repin_agent_hooks", "cc_sandbox_state", "check_managed_settings", "host_env",
    "parse_cc_version", "CC_HOSTED_TOOLS", "CC_VERSION_FLOOR",
    "check_shared_code", "codex_initialize_params", "codex_config_overrides",
    "security_inputs_digest",
    "trust_managed", "load_managed_trust",
    "broker_method_allowed", "codex_sandbox_policy", "overwrite_codex_policy",
    "check_codex_effective_policy", "check_codex_hooks", "check_codex_code_paths",
    "codex_spawn_env",
]


class HostRefusal(Exception):
    """Hosting is refused. ``str()`` is the reason shown to the operator."""


# --restricted needs 2.1.248, and a sandbox made admin-required by `--settings` ignores the
# repository's loosening keys only from 2.1.285 (CC docs: cli-reference, sandboxing).
CC_VERSION_FLOOR = (2, 1, 285)

# D4 + D12: the exact hosted tool set, compared by string equality, and the ONE place it is set.
# Bash runs inside the kernel sandbox; Read is confined to the working directories by
# --restricted. Edit and Write are OUT (D12, proposed 10-07): the host approving a
# file-tool PATH that Claude Code resolves and writes LATER is a race that sandboxed Bash can
# win by swapping a workspace symlink to activation/hooks (L3 r1, codex 1), so every write the
# entity makes goes through Bash, where denyWrite holds at the syscall. Restoring them is a
# change to this tuple plus a write broker that is itself the writer (openat under a held
# directory fd, O_NOFOLLOW at every component), never a path policy in front of CC's own tool.
# Any other tool is re-admitted the same way: one at a time, behind a canary arm that proves it
# inherits the gate and the confinement.
CC_HOSTED_TOOLS = ("Bash", "Read")


# `.codex`: r3a measured a sandboxed model Bash creating <ws>/.codex/config.toml in hosted CC
# (1013-consent/R3A_RESULT.md), the workspace config Codex reads for hooks and its own sandbox.
PROTECTED_NAMES = frozenset({"activation", ".levain", ".claude", ".codex", ".git", ".mcp.json"})

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
    """True when ``path`` is ``root`` or below it, decided by FILE IDENTITY: each existing
    ancestor of the resolved path is compared with ``root`` by device and inode, so a case- or
    normalization-variant spelling on APFS cannot read as outside (L3 r1, codex 5). A path whose
    tail does not exist yet is judged by its deepest existing ancestor."""
    try:
        rid = _ident(os.stat(root))
    except OSError:
        rp, rr = _real(path), _real(root)
        return rp == rr or rr in rp.parents
    p = _real(path)
    for a in (p, *p.parents):
        try:
            if _ident(os.stat(a)) == rid:
                return True
        except OSError:
            continue
    return False


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
    ("allowAppleEvents",): bool,
    ("allowNestedUserNamespaces",): bool,
    ("network", "allowLocalBinding"): bool,
    ("network", "allowUnixSockets"): list,
    ("network", "allowAllUnixSockets"): bool,
    ("network", "allowMachLookup"): list,
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
        "allowAppleEvents": False,
        "allowNestedUserNamespaces": False,
        # strictAllowlist with an empty allowlist: the sandbox's own HTTP proxy denies every host
        # instead of prompting, so `curl http://127.0.0.1:<cockpit>` through $HTTP_PROXY is refused
        # (L2 r1 #3); the loopback names are denied as well in case an allow is merged in.
        "network": {"allowLocalBinding": False, "allowUnixSockets": [], "allowAllUnixSockets": False,
                    "allowMachLookup": [], "strictAllowlist": True,
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
    bytes). Refuses: any symlink or non-regular file (a link points at code the pin cannot see, a
    FIFO would hang the host) and any file with a second hard link, because an alias elsewhere in
    the workspace would let sandboxed Bash rewrite the hook's inode through a path denyWrite does
    not cover (L3 r1, codex 2). Each file is read through an O_NOFOLLOW descriptor whose fstat
    must match the lstat that vetted it. ``__pycache__`` is never read by a hosted hook: the host
    env sets PYTHONPYCACHEPREFIX, so a hook's own child interpreter uses the host-owned cache."""
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
            if st.st_nlink != 1:
                raise HostRefusal(f"hook file {p!r} has {st.st_nlink} hard links; another name for it "
                                  "could be rewritten from the workspace")
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if _ident(os.fstat(fd)) != _ident(st):
                    raise HostRefusal(f"hook file {p!r} changed while it was being pinned")
                with os.fdopen(fd, "rb", closefd=False) as fh:
                    data = fh.read()
            finally:
                os.close(fd)
            rel = os.path.relpath(p, tree)
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
        fd = os.open(pf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("not a regular file")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
                recorded = json.loads(fh.read())
        finally:
            os.close(fd)
        if not isinstance(recorded, dict):
            raise ValueError("not a JSON object")
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


# ---------------------------------------------------------------------------- agent hook pins
# `levain doctor` pins what the agents run on the operator's say-so: Claude Code's hook entries
# (a settings file can name any command), the scripts those entries run, and every file under the install each hook
# command names (an interpreter is never pinned), because Claude Code does not notice an edited hook SCRIPT (Q0 item
# 8, measured). Doctor only DETECTS: the pin file is Levain's own state under `.levain/`; the
# agents' settings are never written.
#
# Two rules the digests hold to (L3 r1 on 3b):
#   * only a pin file that does not exist is "first use"; one that exists and cannot be read or
#     is not a valid pin is a FAIL until an explicit `--repin-hooks` (``AgentPinError``);
#   * a component that cannot be read or hashed safely RAISES ``AgentPinError`` instead of
#     becoming a stable placeholder string, so neither doctor nor a re-pin can call it a match.

_AGENT_PIN_FILE = Path(".levain") / "agent_hook_pins.json"
_CODEX_KEY = "codex hooks.json (Codex)"


class AgentPinError(Exception):
    """A pin file or a pinned component cannot be read or hashed safely. ``items`` names each one."""

    def __init__(self, items: Sequence[str]):
        self.items = list(items)
        super().__init__("; ".join(self.items))


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_settings_doc(path: Path) -> Any:
    """The parsed settings file, or None when it is absent. Raises AgentPinError when it is there
    and cannot be read or parsed (Claude Code would drop it, and doctor cannot vouch for it)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentPinError([f"{path}: unreadable ({exc.strerror or exc})"]) from None
    try:
        return json.loads(text)
    except ValueError:
        raise AgentPinError([f"{path}: not valid JSON"]) from None


# Keys of a Claude Code settings file that run code or switch hooks off, pinned beside `hooks`.
# `env` is NOT read: it holds ordinary variables that change for no security reason, so a pin
# on it would be a standing false positive (named as unread in the CHANGELOG).
_SETTINGS_PINNED_KEYS = ("hooks", "disableAllHooks", "statusLine", "apiKeyHelper")


def _hooks_entry_digest(path: Path) -> str:
    doc = _read_settings_doc(path)
    if doc is None:
        return "absent"
    sub = {k: doc.get(k) for k in _SETTINGS_PINNED_KEYS} if isinstance(doc, dict) else {}
    return _sha_bytes(json.dumps(sub, sort_keys=True).encode())


# --- Claude Code cross-session messaging (Q9) ------------------------------------------------------------------
# The tools a permission rule governs messaging with: Claude Code's docs say both take "the bare tool name with no
# specifier", so a rule matches only as that exact string.
MESSAGING_TOOLS = ("SendMessage", "ListAgents")
# `crossSessionInbound` values, least strict first: Claude Code's "`refuse` is stricter than `hold`, and `hold` is
# stricter than `accept`".
INBOUND_VALUES = ("accept", "hold", "refuse")


def stricter_inbound(a: str | None, b: str | None) -> str | None:
    """The stricter of two `crossSessionInbound` values (``None`` = unset, looser than any value)."""
    rank = {v: i for i, v in enumerate(INBOUND_VALUES)}
    a = a if isinstance(a, str) and a in rank else None
    b = b if isinstance(b, str) and b in rank else None
    if a is None or b is None:
        return a or b
    return a if rank[a] >= rank[b] else b


def messaging_facts(doc: Any) -> dict[str, Any] | None:
    """The messaging-relevant facts one Claude Code settings document sets: `crossSessionInbound`,
    `isolatePeerMachines`, and which of :data:`MESSAGING_TOOLS` its `permissions.ask` / `permissions.deny` name.
    ``None`` when it sets none of them, so a file that never touched messaging pins as absent."""
    if not isinstance(doc, Mapping):
        return None
    facts: dict[str, Any] = {k: doc[k] for k in ("crossSessionInbound", "isolatePeerMachines") if k in doc}
    perms = doc.get("permissions")
    if isinstance(perms, Mapping):
        for kind in ("ask", "deny"):
            rules = perms.get(kind)
            hit = sorted(t for t in MESSAGING_TOOLS if isinstance(rules, list)
                         and any(_rule_names_tool(r, t) for r in rules))
            if hit:
                facts[kind] = hit
    return facts or None


def _rule_names_tool(rule: Any, tool: str) -> bool:
    """A deny or ask rule naming ``tool`` whole: the bare name, or a glob in the tool-name position (Claude Code:
    "The pattern must match the full tool name: `"*"` matches every tool"). A rule with a specifier (``X(...)``)
    does not name a tool whole."""
    if not isinstance(rule, str) or "(" in rule:
        return False
    return re.fullmatch(re.escape(rule).replace(r"\*", ".*"), tool) is not None


def _messaging_digest(path: Path) -> str:
    facts = messaging_facts(_read_settings_doc(path))
    return "absent" if facts is None else _sha_bytes(json.dumps(facts, sort_keys=True, default=repr).encode())


@dataclass(frozen=True)
class MessagingPosture:
    """What Claude Code applies to cross-session messaging in a workspace, as far as the files doctor reads say.
    ``inbound``: the effective `crossSessionInbound` (``None``: no value applies, Claude Code decides per message).
    ``outbound``: ``deny`` when a rule denies `SendMessage`, else ``ask`` when one asks for it, else ``open``.
    ``isolate``: some layer sets `isolatePeerMachines` true. ``sources``: every file read (``(unreadable)`` marked)."""
    inbound: str | None
    inbound_source: str | None
    outbound: Literal["deny", "ask", "open"]
    outbound_source: str | None
    list_denied: bool
    isolate: bool
    sources: tuple[str, ...]


def cc_messaging_posture(install: str | os.PathLike[str], home: str | os.PathLike[str] | None = None,
                         managed: Sequence[tuple[str, Any]] | None = None) -> MessagingPosture:
    """Claude Code's messaging controls for this workspace as its settings FILES say them, read with the vendor's own
    precedence (not the sandbox's first-layer-wins order). The trusted inbound value is managed settings' (the files
    merge in order, a later drop-in over an earlier one, so the last that sets it), else the user's; a workspace
    `hold`/`refuse` applies only when stricter; an unrecognised value holds wherever it is, and in managed settings
    counts as `refuse`. Permission rules merge across layers unless managed `allowManagedPermissionRulesOnly` is true;
    an `isolatePeerMachines` true from any layer applies. The workspace file is the shared `.claude/settings.json` in
    the workspace itself (Claude Code reads it from the session's working directory only); `settings.local.json` is not
    credited (it can only tighten). Not read: `--settings`, `--setting-sources`, a relocated CLAUDE_CONFIG_DIR,
    MDM-profile, server-managed and (on WSL) inherited Windows settings, plugin mods. `--settings` and the user file cannot loosen a workspace `hold`/`refuse` or remove a rule;
    a managed source with `allowManagedPermissionRulesOnly` discards the workspace rules."""
    ent = Path(os.path.abspath(install))
    hm = Path(home) if home is not None else Path(os.path.expanduser("~"))
    # Only the shared settings.json Claude Code reads from the session's working directory (the workspace; levain writes
    # its controls there) is credited. settings.local.json is not read: where Claude Code keeps it depends on git
    # worktrees, ownership and home, and a local value can only tighten (a local accept never applies, rules only add),
    # so leaving it out can only under-report, never report governed when it is not.
    project = [cc_project_settings_files(ent, hm)[1]]
    managed_docs = list(load_managed_settings() if managed is None else managed)
    # (path, document, kind): managed, then user, then the workspace files
    layers: list[tuple[str, Any, str]] = [(p, d, "managed") for p, d in managed_docs]
    for kind, paths in (("user", [hm / ".claude" / "settings.json"]), ("project", project)):
        for p in paths:
            if p.exists():
                try:
                    layers.append((str(p), json.loads(p.read_text(encoding="utf-8")), kind))
                except (OSError, ValueError):
                    layers.append((str(p), _MISSING, kind))

    def managed_last(key: str) -> tuple[Any, str | None]:
        for p, d in reversed(managed_docs):
            v = _get(d, (key,)) if isinstance(d, Mapping) else _MISSING
            if v is not _MISSING:
                return v, p
        return _MISSING, None

    inbound: str | None = None
    inbound_source: str | None = None
    v, src = managed_last("crossSessionInbound")
    if v is not _MISSING:
        inbound, inbound_source = (v if v in INBOUND_VALUES else "refuse"), src
    else:
        for path, doc, kind in layers:
            if kind == "user" and isinstance(doc, Mapping) and doc.get("crossSessionInbound") in INBOUND_VALUES:
                inbound, inbound_source = doc["crossSessionInbound"], path  # an unrecognised one is the hold below
                break
    for path, doc, kind in layers:
        if kind == "managed" or not isinstance(doc, Mapping) or "crossSessionInbound" not in doc:
            continue
        v = doc["crossSessionInbound"]
        if v not in INBOUND_VALUES:
            v = "hold"  # an unrecognised user, project or local value holds over any accept
        elif kind == "user" or v == "accept":
            continue  # the user value is the trusted one above; a project or local accept never applies
        new = stricter_inbound(inbound, v)
        if new != inbound:
            inbound, inbound_source = new, path
    outbound: Literal["deny", "ask", "open"] = "open"
    outbound_source: str | None = None
    list_denied = isolate = False
    # `allowManagedPermissionRulesOnly` in managed settings makes Claude Code ignore every other layer's rules
    managed_rules_only = managed_last("allowManagedPermissionRulesOnly")[0] is True
    for path, doc, kind in layers:
        facts = messaging_facts(doc) or {}
        if managed_rules_only and kind != "managed":
            facts = {k: v for k, v in facts.items() if k not in ("ask", "deny")}
        if "SendMessage" in facts.get("deny", ()):
            outbound, outbound_source = "deny", path
        elif "SendMessage" in facts.get("ask", ()) and outbound == "open":
            outbound, outbound_source = "ask", path
        list_denied = list_denied or "ListAgents" in facts.get("deny", ())
        isolate = isolate or facts.get("isolatePeerMachines") is True
    sources = tuple(p + (" (unreadable)" if d is _MISSING else "") for p, d, _k in layers)
    return MessagingPosture(inbound, inbound_source, outbound, outbound_source, list_denied, isolate, sources)


def _dir_digest(root: Path, _seen: frozenset[str] = frozenset()) -> str:
    """sha256 over every file under ``root`` (relative path and bytes). A symlink contributes its
    link string AND its target's content (a directory link: the target tree's digest), so both a
    retarget and an edit of what it points at change the digest. A file that cannot be read
    raises AgentPinError; a dangling link is hashed as dangling."""
    if not os.path.lexists(root):
        return "absent"
    real = os.path.realpath(root)
    if real in _seen:
        return "cycle"
    seen = _seen | {real}
    h = hashlib.sha256()
    for cur, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files + [d for d in dirs if os.path.islink(os.path.join(cur, d))]):
            p = os.path.join(cur, f)
            rel = os.fsencode(os.path.relpath(p, root))
            try:
                if os.path.islink(p):
                    h.update(rel + b"\0link\0" + os.fsencode(os.readlink(p)) + b"\0")
                    if os.path.isdir(p):
                        h.update(_dir_digest(Path(p), seen).encode())
                    elif os.path.isfile(p):
                        with open(p, "rb") as fh:
                            h.update(hashlib.sha256(fh.read()).digest())
                    else:
                        h.update(b"dangling")
                    continue
                with open(p, "rb") as fh:
                    h.update(rel + b"\0" + hashlib.sha256(fh.read()).digest())
            except OSError as exc:
                raise AgentPinError([f"{p}: unreadable ({exc.strerror or exc})"]) from None
    return h.hexdigest()


def _read_toml_doc(path: Path) -> Any:
    """The parsed TOML file, or None when absent; AgentPinError when it is there and cannot be read or parsed."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentPinError([f"{path}: unreadable ({exc.strerror or exc})"]) from None
    try:
        return tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AgentPinError([f"{path}: not valid TOML"]) from None


def _workspace_codex_digest(path: Path) -> str:
    """Digest of the `[hooks]` table of the workspace's `.codex/config.toml` (the rest of the file is not hook-bearing)."""
    doc = _read_toml_doc(path)
    if doc is None:
        return "absent"
    return _sha_bytes(json.dumps(doc.get("hooks"), sort_keys=True, default=str).encode())


def _git_root(ent: Path) -> Path | None:
    """The git work tree root above ``ent`` (a ``.git`` directory or file), found by walking up.
    Which root files Claude Code reads is :func:`cc_project_settings_files`'s answer; the hook pin
    (:func:`_settings_files`) covers those and also the root's shared settings.json, which a
    subdirectory session does not load (a pin that over-covers raises a false alarm at worst)."""
    for d in (ent, *ent.parents):
        if os.path.lexists(d / ".git"):
            return d
    return None


def _owned(p: Path) -> bool:
    try:
        return os.lstat(p).st_uid == os.getuid()
    except (OSError, AttributeError):
        return False


def _main_checkout(root: Path) -> Path:
    """The main checkout's root for a linked worktree (``.git`` is a file naming its gitdir), else ``root``."""
    dot = root / ".git"
    if not dot.is_file():
        return root
    try:
        first = dot.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if not first.startswith("gitdir:"):
            return root
        gitdir = Path(first[len("gitdir:"):].strip())
        gitdir = gitdir if gitdir.is_absolute() else (root / gitdir)
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        c = Path(os.path.normpath(gitdir / common))
    except (OSError, IndexError, ValueError):
        return root
    return c.parent if c.name == ".git" else root


def cc_project_settings_files(cwd: str | os.PathLike[str], home: str | os.PathLike[str] | None = None
                              ) -> tuple[list[Path], Path]:
    """(local files, shared file): the project settings files Claude Code reads for a session started in ``cwd``,
    by the vendor's rules (code.claude.com/docs/en/settings, "Where Claude Code keeps the local file in a git
    repository"). The shared ``.claude/settings.json`` is read from the session's primary working directory only,
    never from a repository root above it. ``settings.local.json`` is read at the repository root (a linked
    worktree's main checkout root), except outside git, when that root is the home directory, on Windows, or when
    the root or its ``.git`` / ``.claude`` entry is not this user's; then it stays beside the shared file. A local
    file an older Claude Code left in the starting directory is still read after the root's (the root's value wins).
    Local files come highest precedence first."""
    d = Path(os.path.abspath(cwd))
    hm = Path(os.path.realpath(home if home is not None else os.path.expanduser("~")))
    shared = d / ".claude" / "settings.json"
    beside = d / ".claude" / "settings.local.json"
    root = _git_root(d)
    main = _main_checkout(root) if root is not None else None
    if main is None or hm in (Path(os.path.realpath(root)), Path(os.path.realpath(main))) or sys.platform == "win32" or not all(
            _owned(p) for r in {root, main} for p in (r, r / ".git", r / ".claude") if p is r or os.path.lexists(p)):
        return [beside], shared
    at_root = main / ".claude" / "settings.local.json"
    return ([at_root] if at_root == beside else [at_root, beside]), shared


# The line init and doctor print for a workspace whose hooks stay in settings.json (cc_levain_hooks_file). It names
# no cause: the same branch covers a nested workspace, a linked worktree, no git, the home directory, a root not
# owned by this user and Windows.
CC_HOOKS_ROOT_ONLY = ("Claude Code reads levain's hooks only when it starts at the workspace folder itself (a session "
                      "started in a subfolder is not covered)")


# The git levain asks (L3 r3 G1): a fixed binary, never one PATH finds (a session's PATH can name a workspace bin/
# holding a planted git).
GIT_CANDIDATES = ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git")
_GIT_BINARY: list[str | None] = []   # resolved once per process: [path] or [None]


def git_binary() -> str | None:
    """The first existing executable of :data:`GIT_CANDIDATES`, resolved once per process; None when there is none
    (each caller then does what it does when git fails)."""
    if not _GIT_BINARY:
        _GIT_BINARY.append(next((g for g in GIT_CANDIDATES if os.path.isfile(g) and os.access(g, os.X_OK)), None))
    return _GIT_BINARY[0]


def git_env() -> dict[str, str]:
    """The environment for asking git about a workspace: levain's child environment (:func:`levain.launch.child_env`,
    never the whole of this process's) without any ``GIT_*`` variable, so a caller's GIT_DIR / GIT_WORK_TREE /
    GIT_INDEX_FILE (a git hook, an alias) cannot make git answer about another repository."""
    from levain.launch import child_env

    return {k: v for k, v in child_env().items() if not k.startswith("GIT_")}


def cc_levain_hooks_file(install: str | os.PathLike[str]) -> Path:
    """The ONE answer to where levain wires its Claude Code hooks for the workspace ``install``: its
    ``.claude/settings.local.json`` when Claude Code reads that file for a session started anywhere in the workspace,
    else its ``.claude/settings.json``. Claude Code reads settings.local.json at the git repository root, resolved
    through worktrees to the main checkout, and settings.json from the start folder only; so the local file covers a
    subfolder start only when the workspace IS the toplevel of a main checkout. Asked of git: ``rev-parse
    --show-toplevel`` is the workspace and ``--git-common-dir`` is the workspace's own ``.git`` (not a linked
    worktree, a submodule or a separate git dir), and :func:`cc_project_settings_files` agrees (not the home
    directory, owned by this user, not Windows). Anything else, git missing or failing included, gets settings.json,
    which loads for a session started at the workspace folder in every layout (:data:`CC_HOOKS_ROOT_ONLY`).
    install, update, doctor, the consent canary and verify-hooks all ask this; none derives it again."""
    ws = Path(os.path.abspath(install))
    shared = ws / ".claude" / "settings.json"
    local = ws / ".claude" / "settings.local.json"
    env = git_env()

    git = git_binary()

    def ask(*args: str) -> str | None:
        if git is None:
            return None
        try:
            r = subprocess.run([git, "-C", str(ws), "rev-parse", *args], capture_output=True, text=True,
                               timeout=10, env=env)
        except (OSError, subprocess.SubprocessError):
            return None
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None

    top = ask("--show-toplevel")
    common = ask("--git-common-dir") if top is not None else None
    real = os.path.realpath(ws)
    if (top is None or common is None or os.path.realpath(top) != real
            or os.path.realpath(os.path.join(ws, common)) != os.path.join(real, ".git")):
        return shared
    # asked for a session started BELOW the workspace: for one started at it the file beside settings.json is this
    # same file, so the home / ownership / Windows conditions would never show (L2 r2)
    return local if cc_project_settings_files(ws / "levain-subfolder-probe")[0][0] == local else shared


def cc_settings_aliased(install: str | os.PathLike[str]) -> bool:
    """Whether the workspace's ``.claude/settings.local.json`` and ``.claude/settings.json`` are one file (a symlink
    either way, or a hard link): then levain neither splits its hooks out of it nor migrates them between the two
    (removing them from one would remove them from both)."""
    d = Path(os.path.abspath(install)) / ".claude"
    a, b = d / "settings.json", d / "settings.local.json"
    if (os.path.islink(a) or os.path.islink(b)) and os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _settings_files(ent: Path) -> list[tuple[str, Path]]:
    """(component name, path) of every project settings file Claude Code loads for this install."""
    out = [(".claude/settings.json hooks", ent / ".claude" / "settings.json"),
           (".claude/settings.local.json hooks", ent / ".claude" / "settings.local.json")]
    root = _git_root(ent)
    if root is not None and root != ent:
        out += [(".claude/settings.json hooks (repo root)", root / ".claude" / "settings.json"),
                (".claude/settings.local.json hooks (repo root)", root / ".claude" / "settings.local.json")]
    listed = {p for _, p in out}
    out += [(".claude/settings.local.json hooks (main checkout)", p)
            for p in cc_project_settings_files(ent)[0] if p not in listed]
    return out


def _walk_commands(node: Any, out: list[str]) -> None:
    if isinstance(node, dict):
        c = node.get("command")
        if isinstance(c, str):
            out.append(c)
        for v in node.values():
            _walk_commands(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_commands(v, out)


def _hook_commands(docs: Sequence[Any]) -> list[str]:
    cmds: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        _walk_commands(doc.get("hooks"), cmds)
        _walk_commands(doc.get("statusLine"), cmds)
        if isinstance(doc.get("apiKeyHelper"), str):
            cmds.append(doc["apiKeyHelper"])
    return cmds


def _expand_token(tok: str, ent: Path) -> str:
    return os.path.expanduser(tok.replace("${CLAUDE_PROJECT_DIR}", str(ent)).replace("$CLAUDE_PROJECT_DIR", str(ent)))


def _operand_path(tok: str, ent: Path, first: bool) -> Path | None:
    """The existing file or directory ``tok`` names, as an absolute LEXICAL path (no symlink
    followed), or None. An absolute path (after ${CLAUDE_PROJECT_DIR} and ~ expansion) is taken as
    written; a relative path containing a slash resolves against the install only for the command's
    first token."""
    exp = _expand_token(tok, ent)
    if "/" not in exp:
        return None
    if os.path.isabs(exp):
        p = Path(os.path.abspath(exp))
    elif first:
        p = Path(os.path.abspath(ent / exp))
    else:
        return None
    return p if p.exists() else None


def _lexically_in_root(p: Path, ent: Path) -> bool:
    s = str(p)
    return any(s == r or s.startswith(r + os.sep) for r in {str(ent), os.path.realpath(ent)})


_INTERPRETER = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|ruby|perl|php|sh|bash|zsh|dash|uv|uvx|npx|env)$")
_BINARY_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"MZ")


def _is_interpreter(tok: str, ent: Path) -> bool:
    """argv[0] when later tokens exist: a known interpreter name, or a native binary (a copied or
    rebuilt venv python). A script with arguments is the executed code and is pinned."""
    if _INTERPRETER.match(os.path.basename(tok)):
        return True
    p = _operand_path(tok, ent, True)
    try:
        if p is not None and p.is_file():
            with open(p, "rb") as fh:
                return fh.read(4).startswith(_BINARY_MAGIC)
    except OSError:
        pass
    return False


def _pin_digest(p: Path) -> str:
    """Content of the file (through a link) plus the link string when ``p`` is itself a link."""
    data = p.read_bytes()
    if p.is_symlink():
        data = b"link:" + os.readlink(p).encode("utf-8", "surrogateescape") + b"\0" + data
    return _sha_bytes(data)


def _command_pins(ent: Path, docs: Sequence[Any]) -> tuple[dict[str, str], list[str]]:
    """(component -> digest of every file a hook command names under the install, what the commands
    name that no pin covers). argv[0] is the interpreter (a known name or a native binary) when later
    tokens exist and is never pinned; any other argv[0] is the executed script and is. Containment is lexical: an in-root symlink
    is pinned by link string and target content wherever the target lives. Every later existing
    file or directory operand outside the install is named, whether or not another operand pinned."""
    pins: dict[str, str] = {}
    unresolved: list[str] = []

    def warn(label: str) -> None:
        if label not in unresolved:
            unresolved.append(label)

    for cmd in _hook_commands(docs):
        try:
            argv = shlex.split(cmd)
        except ValueError:
            argv = []
        if not argv:
            warn(cmd.strip()[:80])
            continue
        found = False
        for i, tok in enumerate(argv):
            if i == 0 and len(argv) > 1 and _is_interpreter(tok, ent):
                continue
            p = _operand_path(tok, ent, i == 0)
            if p is None:
                continue
            if not _lexically_in_root(p, ent):
                if i > 0:
                    warn(f"{tok} (operand outside the install)")
                continue
            if not p.is_file():
                continue
            found = True
            try:
                pins[f"hook command {tok}"] = _pin_digest(p)
            except OSError as exc:
                raise AgentPinError([f"{p}: unreadable ({exc.strerror or exc})"]) from None
        if not found:
            warn(argv[0])
    return pins, unresolved


def _codex_home(codex_home: str | os.PathLike[str] | None) -> Path:
    return Path(codex_home) if codex_home is not None else Path(
        os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def _pins_global_codex(ent: Path, adapter: str | None = None) -> bool:
    """Whether this install's pin covers the machine-global Codex ``hooks.json``: only a Codex install's.
    levain wires Codex through the machine-global file only (a workspace ``.codex/hooks.json`` fires only in an
    interactive Codex started in that directory, and ``codex -C <dir>`` does not load it), so
    ``levain init --adapter codex`` in ANY directory rewrites that one file; pinning it from every install turned a Claude Code install's doctor red the moment a Codex
    install was made elsewhere (diogenes-20261015-021021). The adapter is the hook-carrying one the install
    attests (``effective_adapter``: claude-code or codex); ``adapter``, the one an init is about to write,
    counts only while the install attests none of those (an init reads the status BEFORE ``AGENTS.md``
    exists, and a hookless install being switched has none), so the status an init takes and the re-pin
    after it read the same adapter."""
    from levain.install import effective_adapter
    attested = effective_adapter(ent)
    return (attested if attested in ("claude-code", "codex") else adapter) == "codex"


def _read_small_regular(p: Path, limit: int = 1 << 20) -> bytes | None:
    """The bytes of ``p`` when it is a regular file of at most ``limit`` bytes, else None. Opened
    non-blocking and without following a final link, so a FIFO or a device named here cannot hang doctor."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def codex_hooks_owner(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                      ) -> Path | None:
    """The OTHER levain install that the Codex ``hooks.json`` is levain's own write for, else None.

    Codex reads one ``hooks.json`` per CODEX_HOME, so ``levain init --adapter codex`` in another install
    replaces this install's wiring with that install's. That is levain repointing the file, not an edit,
    and doctor's wiring rows already say this install is no longer wired. The file counts as another
    install's only when every hook command runs a script under ``<install>/activation/hooks/`` of ONE
    install other than this one, and that install's adapter receipt records exactly these bytes as what
    levain last wrote there (the adapter receipt). Anything else (an edit, an
    install from before the receipt existed, commands naming two installs) is None. The receipt is a
    file the operator's uid can write, like the pin itself: this tells levain's own write from an edit;
    it is not a boundary against a process running as the operator."""
    cj = _codex_home(codex_home) / "hooks.json"
    data = _read_small_regular(cj)
    return None if data is None else _codex_owner_of(Path(os.path.abspath(install)), cj, data)


def _codex_owner_of(ent: Path, cj: Path, data: bytes) -> Path | None:
    """:func:`codex_hooks_owner` on bytes already read, so a caller that also hashes the file decides
    both from one snapshot (L3 r1 codex: a file swapped between two reads was neither compared nor pinned)."""
    from levain.install import ADAPTER_RECEIPT_REL, _adapter_key
    try:
        cmds = _hook_commands([json.loads(data)])
    except (ValueError, RecursionError):
        return None
    owners: set[Path] = set()
    for cmd in cmds:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return None
        script = Path(argv[-1]) if argv else None
        if (script is None or not script.is_absolute() or script.parent.name != "hooks"
                or script.parent.parent.name != "activation"):
            return None
        owners.add(script.parent.parent.parent)
    if len(owners) != 1:
        return None
    owner = owners.pop()
    if os.path.realpath(owner) == os.path.realpath(ent):
        return None
    raw = _read_small_regular(owner.joinpath(*ADAPTER_RECEIPT_REL))
    try:
        files = json.loads(raw).get("files") if raw is not None else None
    except (ValueError, AttributeError, RecursionError):
        return None
    if not isinstance(files, dict) or files.get(_adapter_key(owner, cj)) != _sha_bytes(data):
        return None
    return owner


def _agent_pins_and_unresolved(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None
                               ) -> tuple[dict[str, str], list[str], Path | None]:
    """(pins, unresolved hook commands, the other install the Codex hooks.json now serves or None).
    While another install owns that file it is not this install's to pin, so neither it nor the
    commands in it are read here (:func:`_comparable_pin`)."""
    ent = Path(os.path.abspath(install))
    cc = ent / ".claude"
    pins: dict[str, str] = {}
    errors: list[str] = []
    docs: list[Any] = []
    owner: Path | None = None
    cdoc: Any = None
    if _pins_global_codex(ent):
        # read ONCE: the owner decision, the digest and the commands all come from these bytes
        cj = _codex_home(codex_home) / "hooks.json"
        try:
            cdata = cj.read_bytes() if os.path.lexists(cj) else None
        except OSError as exc:
            errors.append(f"{_CODEX_KEY}: {cj}: unreadable ({exc.strerror or exc})")
        else:
            if cdata is not None and not cj.is_symlink():
                owner = _codex_owner_of(ent, cj, cdata)
            if owner is None:
                pins[_CODEX_KEY] = "absent" if cdata is None else _sha_bytes(cdata)
                try:
                    cdoc = json.loads(cdata) if cdata is not None else None
                except (ValueError, UnicodeDecodeError, RecursionError):
                    cdoc = None  # not valid JSON: its digest still differs from any pin of a valid file

    def run(name: str, fn: Callable[[], str]) -> None:
        try:
            pins[name] = fn()
        except AgentPinError as exc:
            errors.extend(f"{name}: {i}" for i in exc.items)

    for name, path in _settings_files(ent):
        run(name, lambda path=path: _hooks_entry_digest(path))
        run(name.replace(" hooks", " messaging", 1), lambda path=path: _messaging_digest(path))
        try:
            d = _read_settings_doc(path)
            if d is not None:
                docs.append(d)
        except AgentPinError:
            pass  # already named by the run() above
    run(".claude/hooks/ (scripts)", lambda: _dir_digest(cc / "hooks"))

    def scripts() -> str:
        if not os.path.lexists(ent / "activation" / "hooks"):
            return "absent"
        try:
            return hook_pin(ent)
        except HostRefusal as exc:
            raise AgentPinError([f"refused: {exc}"]) from None
        except OSError as exc:
            raise AgentPinError([f"unreadable ({exc.strerror or exc})"]) from None
    run("activation/hooks/ (scripts)", scripts)
    wcc = ent / ".codex" / "config.toml"
    run(".codex/config.toml [hooks] (workspace)", lambda: _workspace_codex_digest(wcc))
    try:
        wdoc = _read_toml_doc(wcc)
        if wdoc is not None:
            docs.append(wdoc)
    except AgentPinError:
        pass
    unresolved: list[str] = []
    if cdoc is not None:
        docs.append(cdoc)
    try:
        cp, unresolved = _command_pins(ent, docs)
        pins.update(cp)
    except AgentPinError as exc:
        errors.extend(exc.items)
    if errors:
        raise AgentPinError(errors)
    return pins, unresolved, owner


def agent_hook_pins(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                    ) -> dict[str, str]:
    """Component name -> digest for every file the agents run hooks from for this install. Raises
    AgentPinError naming every component that cannot be read and hashed safely."""
    return _agent_pins_and_unresolved(install, codex_home)[0]


def unpinned_hook_commands(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                           ) -> list[str]:
    """First tokens of hook commands whose executable did not resolve to a file (a bare name, or a
    path that does not exist): their code is NOT covered by any pin."""
    try:
        return _agent_pins_and_unresolved(install, codex_home)[1]
    except AgentPinError:
        return []


def _load_agent_pins(ent: Path) -> dict[str, str] | None:
    """None only when the pin file does not exist (first use). Anything else wrong with it
    (symlink, not a file, unreadable, invalid JSON, no ``pins``, a non-string digest, another
    entity's pin) raises AgentPinError, so the evidence is never silently replaced."""
    pf = ent / _AGENT_PIN_FILE
    try:
        fd = os.open(pf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AgentPinError([f"pin file {pf}: cannot be opened ({exc.strerror or exc})"]) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AgentPinError([f"pin file {pf}: not a regular file"])
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
            rec = json.loads(fh.read())
    except (OSError, ValueError):
        raise AgentPinError([f"pin file {pf}: unreadable or not valid JSON"]) from None
    finally:
        os.close(fd)
    pins = rec.get("pins") if isinstance(rec, dict) else None
    if not isinstance(pins, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in pins.items()):
        raise AgentPinError([f"pin file {pf}: has no valid `pins` map"])
    if os.path.realpath(str(rec.get("entity"))) != os.path.realpath(ent):
        raise AgentPinError([f"pin file {pf}: recorded for another install ({rec.get('entity')!r})"])
    return pins


def repin_agent_hooks(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                      ) -> dict[str, str]:
    """The operator's explicit act: record the pins as they are now. Raises AgentPinError (and
    writes nothing) when any component cannot be read and hashed safely."""
    return repin_agent_hooks_with_owner(install, codex_home)[0]


def repin_agent_hooks_with_owner(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                                 ) -> tuple[dict[str, str], Path | None]:
    """:func:`repin_agent_hooks`, plus the other install whose Codex hooks.json was left out of the pin
    (None when it was pinned or this install does not pin it): read from the same snapshot as the pins."""
    ent = Path(os.path.abspath(install))
    pins, _, owner = _agent_pins_and_unresolved(ent, codex_home)
    tmp = ent / ".levain" / f".agent_hook_pins.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"entity": str(ent), "pins": pins}, fh, indent=1, sort_keys=True)
        os.replace(tmp, ent / _AGENT_PIN_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return pins, owner


def _hooks_entry_digest_or_none(path: Path) -> bool:
    """True when ``path`` sets any key the hook pin covers (or cannot be read: then it is not vouched for either)."""
    try:
        doc = _read_settings_doc(path)
    except AgentPinError:
        return True
    return isinstance(doc, dict) and any(doc.get(k) not in (None, {}, []) for k in _SETTINGS_PINNED_KEYS)


def agent_pin_status(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None,
                     adapter: str | None = None) -> Literal["none", "fresh", "match", "changed"]:
    """The pin's state right now, WITHOUT recording anything. ``fresh``: no pin and nothing install-
    local for it to cover yet (a first init), so what levain writes is the only content there.
    ``none``: no pin, but hook files already exist that levain did not write in this run.
    ``changed``: a pin that differs, or one that is invalid or cannot be compared. Never raises.
    ``adapter``: the adapter an init is about to write; read only while there is no pin yet and the install
    attests no adapter (otherwise the attested one decides, as it does for the re-pin)."""
    ent = Path(os.path.abspath(install))
    try:
        old = _load_agent_pins(ent)
    except AgentPinError:
        return "changed"
    if old is None:
        local = [ent / ".claude", ent / "activation" / "hooks"]
        root = _git_root(ent)
        if root is not None and root != ent:
            local += [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
        # the local file Claude Code reads at a worktree's main checkout counts only while it carries hooks: it is
        # commonly Claude Code's own "don't ask again" file, which must not make a first init read as unpinned
        local += [p for p in cc_project_settings_files(ent)[0] if p not in local and _hooks_entry_digest_or_none(p)]
        local.append(ent / ".codex" / "config.toml")
        # a Codex hooks.json already on disk is global and unvetted on a Codex install's first use, unless it
        # is levain's own recorded write for another install (a second Codex install's init then pins its own)
        if _pins_global_codex(ent, adapter) and codex_hooks_owner(ent, codex_home) is None:
            local.append(_codex_home(codex_home) / "hooks.json")
        return "none" if any(os.path.lexists(p) for p in local) else "fresh"
    try:
        now, _, owner = _agent_pins_and_unresolved(ent, codex_home)
    except AgentPinError:
        return "changed"
    return "changed" if pin_diffs(_comparable_pin(ent, old, now, owner), now) else "match"


def _comparable_pin(ent: Path, old: Mapping[str, str], now: Mapping[str, str], owner: Path | None
                    ) -> Mapping[str, str]:
    """The pin as it compares to ``now``. While the Codex hooks.json is levain's write for another install
    (``owner``), the pinned copy of it and the hook commands only it named are not compared: the file now
    runs that install's hooks, doctor's wiring rows report that, and a takeover by ``levain init --force``
    here re-pins (its status, taken before the write, is ``match``). Of the pinned commands that are gone,
    only scripts directly in this install's ``activation/hooks/`` are dropped: that is all levain's Codex
    hooks.json ever names here, and the activation tree pin still covers them. Any other gone command (one
    a settings file named, say) is still a difference (L1 on 6087443, reproduced)."""
    if owner is None:
        return old
    hooks_dir = os.path.realpath(ent / "activation" / "hooks")

    def from_codex_file(k: str) -> bool:
        if not k.startswith("hook command ") or k in now:
            return False
        p = Path(k[len("hook command "):])
        return p.is_absolute() and os.path.realpath(p.parent) == hooks_dir

    return {k: v for k, v in old.items() if k != _CODEX_KEY and not from_codex_file(k)}


def repin_after_levain_write(install: str | os.PathLike[str], before: Literal["none", "fresh", "match", "changed"],
                             codex_home: str | os.PathLike[str] | None = None) -> bool:
    """Levain's own hook writes (init, update) re-pin as their last act, through the one pin writer
    (``repin_agent_hooks``), only when ``before`` (:func:`agent_pin_status` taken BEFORE the write)
    was ``match`` or ``fresh``. ``changed`` was edited by something other than levain; ``none`` is
    an install with hook files levain never vouched for. Re-pinning either would bless them, so
    both stay unpinned and doctor's own first-run warning or FAIL speaks. Returns whether it
    re-pinned; a pin that cannot be written is reported by doctor, never a crash here."""
    if before not in ("match", "fresh"):
        return False
    try:
        repin_agent_hooks(install, codex_home)
    except (AgentPinError, OSError):
        return False
    return True


def check_agent_hook_pins(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                          ) -> tuple[Literal["recorded", "match", "changed"], list[str]]:
    """Trust on first use (only when the pin file does not exist), then compare. Returns the status
    and one named line per problem (for ``recorded``: the components pinned on first use) ("<what>: was <old>, now <new>"). An invalid pin file or a
    component that cannot be hashed is ``changed``, named, never recorded over. A ``match`` carries one
    line when the Codex hooks.json was left out because it is levain's write for another install. Never
    edits an agent settings file."""
    ent = Path(os.path.abspath(install))
    try:
        old = _load_agent_pins(ent)
    except AgentPinError as exc:
        return "changed", [*exc.items, "re-pin explicitly once you have read what is there"]
    try:
        now, _, owner = _agent_pins_and_unresolved(ent, codex_home)
    except AgentPinError as exc:
        return "changed", [f"cannot be pinned: {i}" for i in exc.items]
    if old is None:
        repin_agent_hooks(ent, codex_home)
        # what trust-on-first-use just vouched for: every component that exists
        return "recorded", sorted(k for k, v in now.items() if v != "absent")
    diffs = pin_diffs(_comparable_pin(ent, old, now, owner), now)
    if diffs:
        return "changed", diffs
    return "match", ([] if owner is None else [f"{_CODEX_KEY} NOT compared: it is levain's write for the install at "
                                               f"{owner}, whose scripts this pin does not cover"])


def workspace_settings_pin_matches(install: str | os.PathLike[str], codex_home: str | os.PathLike[str] | None = None
                                   ) -> bool:
    """Whether the install's hook pin covers the workspace ``.claude/settings.json`` as it is now: both of that
    file's components (its hooks entry and its messaging facts) compare equal under the same comparison doctor
    runs (:func:`_comparable_pin` then :func:`pin_diffs`). Only those two components are asked, so another
    file's drift does not answer for this one. No pin, an invalid pin or a component that cannot be hashed is
    ``False``. Never writes."""
    ent = Path(os.path.abspath(install))
    try:
        old = _load_agent_pins(ent)
        if old is None:
            return False
        now, _, owner = _agent_pins_and_unresolved(ent, codex_home)
    except AgentPinError:
        return False
    mine = (".claude/settings.json hooks", ".claude/settings.json messaging")
    diffs = pin_diffs({k: v for k, v in _comparable_pin(ent, old, now, owner).items() if k in mine},
                      {k: v for k, v in now.items() if k in mine})
    return not diffs


def pin_diffs(old: Mapping[str, str], now: Mapping[str, str]) -> list[str]:
    """One named line per component that differs from the pin. A component the pin does not carry and that is
    absent now is not a difference: a levain upgrade that adds a component (the Q9 messaging pin) must not turn every
    existing install red when there is nothing there to protect. Once pinned, its removal or edit is a difference."""
    diffs = [f"{k}: was {old.get(k, 'not pinned')[:12]}, now {now[k][:12]}"
             for k in now if old.get(k) != now[k] and not (k not in old and now[k] == "absent")]
    diffs += [f"{k}: was {old[k][:12]}, now gone" for k in old if k not in now]
    return diffs


def _sandbox_docs(ent: Path, hm: Path, managed: Sequence[tuple[str, Any]] | None
                  ) -> list[tuple[str, Any]]:
    """The settings layers doctor can read, in Claude Code's order (managed, project local,
    project, user), the project files from :func:`cc_project_settings_files`. It cannot see
    CLAUDE_CONFIG_DIR relocation, `--settings`, or server-managed settings, which is why the doctor
    line names the sources it did read."""
    docs: list[tuple[str, Any]] = list(load_managed_settings() if managed is None else managed)
    local, shared = cc_project_settings_files(ent, hm)
    paths = [*local, shared, hm / ".claude" / "settings.json"]
    for p in paths:
        if p.exists():
            try:
                docs.append((str(p), json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                docs.append((str(p), _MISSING))
    return docs


def _sandbox_setting(docs: Sequence[tuple[str, Any]], key: str) -> tuple[Any, str | None]:
    for path, doc in docs:
        v = _get(doc, ("sandbox", key)) if isinstance(doc, Mapping) else _MISSING
        if v is not _MISSING:
            return v, path
    return _MISSING, None


def cc_sandbox_state(install: str | os.PathLike[str], home: str | os.PathLike[str] | None = None,
                     managed: Sequence[tuple[str, Any]] | None = None) -> tuple[bool, str]:
    """Is Claude Code's own sandbox CONFIGURED on for this workspace? Claude Code's order: managed
    settings, then the workspace's local settings, the workspace's settings, the user's. The first
    layer that sets ``sandbox.enabled`` decides; none set means OFF (CC's default). Returns
    (configured enabled, source). Configured is not confined: see :func:`cc_sandbox_fails_closed`."""
    ent = Path(os.path.abspath(install))
    hm = Path(home) if home is not None else Path(os.path.expanduser("~"))
    v, path = _sandbox_setting(_sandbox_docs(ent, hm, managed), "enabled")
    if path is None:
        return False, "no settings layer sets sandbox.enabled (Claude Code's default is off)"
    return v is True, path


def cc_sandbox_fails_closed(install: str | os.PathLike[str], home: str | os.PathLike[str] | None = None,
                            managed: Sequence[tuple[str, Any]] | None = None) -> bool:
    """Is ``sandbox.failIfUnavailable`` effectively true? When it is not, a Claude Code that cannot
    start its sandbox runs Bash unconfined."""
    ent = Path(os.path.abspath(install))
    hm = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return _sandbox_setting(_sandbox_docs(ent, hm, managed), "failIfUnavailable")[0] is True


def cc_sandbox_sources(install: str | os.PathLike[str], home: str | os.PathLike[str] | None = None,
                       managed: Sequence[tuple[str, Any]] | None = None) -> list[str]:
    """The settings files and managed documents :func:`cc_sandbox_state` actually read."""
    ent = Path(os.path.abspath(install))
    hm = Path(home) if home is not None else Path(os.path.expanduser("~"))
    return [p for p, _ in _sandbox_docs(ent, hm, managed)]


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
    """The one MCP server runs outside the sandbox with the entity as its cwd, so it must start
    isolated from the cwd (`-P` or `-I` first, L3 r1 complement 11), from an interpreter outside
    the roots, and the only workspace path it may name is its own store."""
    server = cfg["mcpServers"]["anneal_memory"]
    problems = _check_exec(server.get("command", ""), roots, "the MCP server's interpreter")
    if set(server) - {"type", "command", "args"}:
        problems.append(f"the MCP server carries keys this check does not vet: {sorted(set(server) - {'type', 'command', 'args'})}")
    args = list(server.get("args", []))
    if not args or args[0] not in ("-P", "-I"):
        problems.append(f"the MCP server does not start isolated from its cwd (-P or -I first): {args!r}")
    db_problems, at = _db_arg(args, entity / ".levain" / "memory.db", "the MCP server")
    problems += db_problems
    for i, a in enumerate(args):
        if i != at and isinstance(a, str) and "/" in a:
            problems += _outside(a, roots, "an MCP server argument")
    return problems


def _is_db_flag(a: Any) -> bool:
    """``--db``, its ``--db=`` spelling, or an abbreviation of it (``--d``), which an argparse
    parser with the default allow_abbrev reads as ``--db``."""
    if not isinstance(a, str):
        return False
    name = a.split("=", 1)[0]
    return len(name) >= 3 and "--db".startswith(name)


def _db_arg(args: Sequence[Any], expected: str | os.PathLike[str], what: str) -> tuple[list[str], int | None]:
    """The MCP server's store, read from the ORIGINAL argv (chat L3 r1, codex H2): exactly one
    ``--db`` flag, spelled ``--db <path>``, whose path resolves to ``expected``. Returns the
    problems and the index of the vetted path, so the caller skips only that one word."""
    at = [i for i, a in enumerate(args) if _is_db_flag(a)]
    if len(at) != 1:
        return [f"{what} must name --db exactly once, found {len(at)}: {list(args)!r}"], None
    i = at[0]
    if args[i] != "--db" or i + 1 >= len(args) or not isinstance(args[i + 1], str):
        return [f"{what} must pass its store as `--db <path>`: {list(args)!r}"], None
    if _real(args[i + 1]) != _real(expected):
        return [f"{what}'s --db is {args[i + 1]!r}, expected {os.fspath(expected)!r}"], None
    return [], i + 1


# The keys of Claude Code's sandbox settings that relax the profile. network.allowMachLookup naming
# com.apple.trustd.agent opens what enableWeakerNetworkIsolation opens; a run (levain-memory 1026-8/p1)
# made trustd fetch a URL a sandboxed command chose, outside the proxy.
_LOOSENING_TRUE = (("allowUnsandboxedCommands",), ("enableWeakerNestedSandbox",),
                   ("enableWeakerNetworkIsolation",), ("allowAppleEvents",), ("allowNestedUserNamespaces",),
                   ("network", "allowAllUnixSockets"), ("network", "allowLocalBinding"),
                   ("filesystem", "disabled"))
_LOOSENING_NONEMPTY = (("excludedCommands",), ("filesystem", "allowWrite"), ("filesystem", "allowRead"),
                       ("network", "allowUnixSockets"), ("network", "allowedDomains"),
                       ("network", "allowMachLookup"))


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


def check_managed_settings(docs: Sequence[tuple[str, Any]], roots: Sequence[Path], *,
                           trusted_hooks: frozenset[str] = frozenset()) -> list[str]:
    """Managed settings still load under --restricted and their arrays merge with ours, so they
    can loosen the sandbox or add code that runs outside it. ACCEPTED, by allowlist: top-level
    keys hooks/sandbox/permissions; permission deny/ask lists only (an allow rule would answer a
    Bash prompt before the host does); sandbox keys this module pins, each with its exact type
    (L3 r1, codex 7: `"enabled": 0` is not False) and a value that does not loosen.

    Managed HOOK commands are not judged by reading their shell text: every reading of shell
    text found a new form that ran workspace code (`$(pwd)/x.py`, `python3 -c 'import x'` with
    the entity as cwd). D13: each command must be in the operator's trust pin
    (`python -m levain.host_guard trust-managed`), by exact sha256."""
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
        for k in sorted(set(doc) - _MANAGED_TOP_KEYS):
            problems.append(f"{path}: managed key {k!r} is not one hosting can vet")
        perms = doc.get("permissions", {})
        if not isinstance(perms, Mapping) or set(perms) - _MANAGED_PERMISSION_KEYS:
            problems.append(f"{path}: managed permissions may only hold deny/ask lists")
        sb = doc.get("sandbox", {})
        if not isinstance(sb, Mapping):
            problems.append(f"{path}: sandbox is not an object")
            sb = {}
        for k in sorted(set(sb) - _MANAGED_SANDBOX_KEYS):
            problems.append(f"{path}: sandbox.{k} is not a key hosting can vet")
        for sub, allowed in (("network", _MANAGED_NETWORK_KEYS), ("filesystem", _MANAGED_FILESYSTEM_KEYS)):
            node = sb.get(sub, {})
            for k in sorted((set(node) - allowed) if isinstance(node, Mapping) else {"<not an object>"}):
                problems.append(f"{path}: sandbox.{sub}.{k} is not a key hosting can vet")
        for keys, typ in {**_SANDBOX_TYPES, ("filesystem", "denyRead"): list,
                          ("filesystem", "allowRead"): list}.items():
            v = _get(sb, keys)
            if v is _MISSING:
                continue
            if type(v) is not typ:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} must be {typ.__name__}, got {type(v).__name__}")
            elif typ is list and (odd := next((i for i in v if type(i) is not str), _MISSING)) is not _MISSING:
                # RULINGS 1026-10 Q5 (2): items too, as render's repo check (`repo_loosening_refusals`) requires.
                problems.append(f"{path}: sandbox.{'.'.join(keys)} must be a list of strings, "
                                f"holds {type(odd).__name__}")
        for keys in _LOOSENING_TRUE:
            if _get(sb, keys) is True:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is true")
        if _get(sb, ("network", "strictAllowlist")) is False:
            problems.append(f"{path}: sandbox.network.strictAllowlist is false")
        for keys in (("enabled",), ("failIfUnavailable",)):
            if _get(sb, keys) is False:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is false")
        for keys in _LOOSENING_NONEMPTY:
            v = _get(sb, keys)
            if v is not _MISSING and v:
                problems.append(f"{path}: sandbox.{'.'.join(keys)} is not empty")
        for event, cmd in _managed_hook_commands(path, doc, problems):
            if _sha(cmd) not in trusted_hooks:
                problems.append(f"{path}: {event} hook is not in the operator's trust pin "
                                f"(run: python -m levain.host_guard trust-managed): {cmd!r}")
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
    inputs_digest: str = ""

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
        """What a canary verdict is bound to: the exact argv, every env value, and the claude
        binary's identity (device, inode, size, mtime), so a binary replaced in place or a changed
        PATH never reuses a verdict (L3 r1, complement 10)."""
        try:
            st = os.stat(_real(self.claude_bin))
            binary: list[int] = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]
        except OSError:
            binary = []
        blob = json.dumps([self.argv(), sorted(self.env.items()), binary, self.inputs_digest], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()


def cc_host_profile(entity: str | os.PathLike[str], *, claude_bin: str, python: str | None = None,
                    platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                    home: Path | None = None,
                    managed: Sequence[tuple[str, Any]] | None = None,
                    check_shared: bool = True) -> CCHostProfile:
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
    problems += check_managed_settings(
        load_managed_settings(platform) if managed is None else managed, roots,
        trusted_hooks=load_managed_trust(platform, environ, home))
    if check_shared:
        problems += check_shared_code(ent, roots, home=home)
    if problems:
        raise HostRefusal("; ".join(problems))
    pin_status = check_or_record_pin(ent, state)
    inputs = security_inputs_digest(ent, py, claude_bin, platform, environ, home)
    return CCHostProfile(entity=ent, claude_bin=claude_bin, settings=settings, mcp_config=mcp,
                         env={**host_env(environ, roots=roots, pycache=pycache),
                              "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1"}, run_dir=run,
                         state_dir=state, pin_status=pin_status, roots=roots, inputs_digest=inputs)


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


def check_codex_effective_policy(sandbox: Mapping[str, Any], *, forbidden: Sequence[Path],
                                 cwd: str | None = None,
                                 entity: str | os.PathLike[str] | None = None) -> list[str]:
    """Run on what the server REPORTS back (thread/start and thread/resume results). Network must be
    explicitly false for every type, readOnly included; no writable root may contain, or sit
    inside, the host run dir, CODEX_HOME or the hook and MCP code (L1 r1 #5a). The reported cwd
    is an implicit writable root, so it must be the entity itself (L3 r2, codex 2: a cwd of
    CODEX_HOME passed every explicit check)."""
    t = sandbox.get("type")
    if t not in ("readOnly", "workspaceWrite"):
        return [f"Codex sandbox type {t!r} is not hostable"]
    problems = []
    if entity is not None:
        if cwd is None or not (is_within(cwd, entity) and is_within(entity, cwd)):
            problems.append(f"Codex reports cwd {cwd!r}, which is not the entity directory")
    if sandbox.get("networkAccess", _MISSING) is not False:
        problems.append(f"Codex sandbox networkAccess is {sandbox.get('networkAccess', 'absent')!r}, must be false")
    if t == "workspaceWrite":
        roots = sandbox.get("writableRoots", _MISSING)
        if not isinstance(roots, list):
            problems.append("Codex sandbox writableRoots is missing")
        else:
            for r in list(roots) + ([cwd] if cwd else []):
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
    """``hooks/list``: the hooks Codex will run are EXACTLY Levain's, each ``trusted``. An extra
    hook from any other source refuses (L3 r2, codex 5: it runs outside Codex's sandbox and
    nothing else here vets it), and an untrusted Levain hook refuses because headless skips it
    silently, so "same entity" would fail invisibly. Writing the trust stays the operator's act."""
    problems = []
    seen = set()
    for entry in hooks_list.get("data", []) if isinstance(hooks_list, Mapping) else []:
        for h in entry.get("hooks", []) if isinstance(entry, Mapping) else []:
            src = h.get("sourcePath") if isinstance(h, Mapping) else None
            if src not in required_sources:
                problems.append(f"Codex lists a hook Levain did not install: {src!r} ({h.get('key') if isinstance(h, Mapping) else h!r})")
                continue
            seen.add(src)
            if h.get("trustStatus") != "trusted":
                problems.append(f"Codex hook {h.get('key')!r} is {h.get('trustStatus')!r}, not trusted")
    for src in required_sources:
        if src not in seen:
            problems.append(f"Codex does not list Levain's hooks from {src!r}")
    return problems


_SHELL_META = re.compile(r"[;&|<>`$(){}\n]")


def check_codex_code_paths(hooks_json: Mapping[str, Any], mcp_servers: Mapping[str, Any],
                           roots: Sequence[Path], *,
                           expected_db: str | os.PathLike[str] | None = None) -> list[str]:
    """Code a Codex entity runs outside its sandbox (hooks, MCP servers) must be an exact argv:
    an interpreter outside every writable root, its isolating flags BEFORE the script (a flag
    after the script is the script's argument, L1 r1 #5b), a script outside the writable roots,
    no shell composition, no relative path. MCP servers get the same rule, may not carry env or
    cwd keys (L1 r1 #5c), and must name `--db` exactly once with ``expected_db`` (this entity's
    own store, compared resolved) as its value; only that one word is then exempt, by POSITION
    (L3 r1, codex 4: removing it by value also removed an identical script argument; chat L3 r1,
    codex H2: removing every value after any `--db` let another entity's store through). Today's
    Codex install puts its hooks under the entity's own writable root, so this refuses it by
    design (plan D6: S4/S5 move them)."""
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
        if expected_db is None:
            problems.append(f"Codex MCP server {name!r} cannot be vetted without this entity's store path")
            at = None
        else:
            db_problems, at = _db_arg(args, expected_db, f"Codex MCP server {name!r}")
            problems += db_problems
        kept = [a for i, a in enumerate(args) if i != at]
        argv = [str(server.get("command", ""))] + kept
        problems += _vet_codex_argv(shlex.join(argv), roots, f"Codex MCP server {name!r}", ("-I",))
    return problems


def codex_spawn_env(codex_home: str | os.PathLike[str], environ: Mapping[str, str] | None = None, *,
                    roots: Sequence[Path] = ()) -> dict[str, str]:
    """The Codex app-server's env: the same allowlist, PATH filtered against the entity's roots
    (L3 r2, complement 4), plus the entity's own CODEX_HOME."""
    env = host_env(environ, roots=roots)
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
                                   "allowMachLookup", "strictAllowlist", "deniedDomains"})


_MANAGED_FILESYSTEM_KEYS = frozenset({"denyWrite", "denyRead", "allowRead", "disabled"})


_SCRIPT_EXT = re.compile(r"\.(py|sh|bash|zsh|js|mjs|ts|rb|pl)$")


def _outside_problems(p: str, roots: Sequence[Path]) -> bool:
    return any(is_within(p, r) for r in roots)


def _ident(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


_CODEX_FORWARD = {
    "thread/start": frozenset({"model"}),
    "thread/resume": frozenset({"threadId", "model"}),
    "turn/start": frozenset({"threadId", "input", "model", "effort", "summary", "clientUserMessageId"}),
}


def _vet_codex_argv(cmd: str, roots: Sequence[Path], what: str, flags: tuple[str, ...]) -> list[str]:
    """A strict grammar, not a scan: interpreter, then only argument-free isolating flags, then
    exactly one absolute script outside the roots, then plain arguments (an MCP server's vetted
    `--db` path has already been removed by position; the `--db` flag itself stays). A flag that takes a value (`-X`, `-W`, `-m`, `-c`,
    `--anything`) refuses, because it can swallow the next word and turn a vetted-looking argument
    into the real script (L3 r2, complement 1: `-I -X --db /ent/evil.py`)."""
    if _SHELL_META.search(cmd):
        return [f"{what} uses shell syntax: {cmd!r}"]
    argv = shlex.split(cmd)
    if not argv:
        return [f"{what} is empty"]
    problems = _check_exec(argv[0], roots, f"{what} interpreter")
    i = 1
    seen: set[str] = set()
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] not in _ISOLATING_FLAGS:
            return problems + [f"{what} passes interpreter option {argv[i]!r}, which hosting cannot vet: {cmd!r}"]
        seen.add(argv[i])
        i += 1
    missing = [f for f in flags if f not in seen]
    if missing:
        problems.append(f"{what} does not run with {' '.join(missing)} before its script: {cmd!r}")
    if i >= len(argv):
        return problems + [f"{what} names no script: {cmd!r}"]
    script = argv[i]
    if not os.path.isabs(script):
        problems.append(f"{what} script is not an absolute path: {script!r}")
    else:
        problems += _outside(script, roots, f"{what} script")
    for a in argv[i + 1:]:
        if "/" in a and not os.path.isabs(a):
            problems.append(f"{what} names a relative path {a!r}")
        elif os.path.isabs(a):
            problems += _outside(a, roots, f"{what} path")
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
    files, editable installs and site dirs add code). The probe LOCATES anneal_memory with
    importlib.util.find_spec and never imports it, so no package code runs before it is vetted
    (L3 r2, codex 1). It runs the host's own interpreter, the one levain itself is running in,
    so a .pth line it executes at startup is code the host process already executed. Fails
    closed."""
    code = ("import json, sys, importlib.util\n"
            "spec = importlib.util.find_spec('anneal_memory')\n"
            "origins = [spec.origin] if spec and spec.origin else []\n"
            "origins += list(spec.submodule_search_locations or []) if spec else []\n"
            "print(json.dumps([p for p in sys.path if p] + origins))")
    try:
        out = subprocess.run([python, "-P", "-c", code], env=dict(env), capture_output=True,
                             text=True, timeout=30, check=True, cwd="/").stdout
        paths = json.loads(out.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return [f"could not read the hook interpreter's import path: {exc}"]
    if not isinstance(paths, list) or len(paths) < 1:
        return ["the hook interpreter reported no import path"]
    problems: list[str] = []
    for p in paths:
        if isinstance(p, str) and os.path.isabs(p) and os.path.exists(p):
            problems += _outside(p, roots, "a directory the hook interpreter imports from")
    return problems


def check_shared_code(entity: Path, roots: Sequence[Path], *, home: Path | None = None) -> list[str]:
    """Code that OTHER processes run outside any sandbox must not live where the hosted entity
    can write it: the targets of the operator's ~/.claude entries (skills, agents, plugins,
    settings) and the scripts their user-level hooks name (L2 r1 #4). An entity whose directory
    holds the operator's own tooling is refused: hosting it would let the entity rewrite what the
    operator's next terminal session runs.

    BOUND, stated: this is a misconfiguration catch, not a boundary. It sees symlinks under
    ~/.claude (recursively under plugins/ and skills/) and absolute script paths written literally
    in user hook commands. A path a hook command COMPUTES in shell (`$(pwd)/x.py`), launchd and cron
    scripts, and shell rc files are not seen (L3 r2, codex 3). Reading shell text is the construct
    D13 deleted for managed hooks; it is not rebuilt here. The boundary for the hosted session
    itself is the sandbox; this only guards the operator's OTHER sessions from a badly placed
    entity."""
    home = Path(os.path.expanduser("~")) if home is None else home
    cc = home / ".claude"
    problems: list[str] = []
    if not cc.is_dir():
        return problems
    for entry in sorted(cc.iterdir()):
        if entry.name.startswith(".") or entry.name in ("projects", "jobs", "todos", "shell-snapshots",
                                                         "statsig", "logs", "cache"):
            continue  # Claude Code's own data, not code it loads
        problems += _outside(_real(entry), roots, f"~/.claude/{entry.name}")
        if entry.name in ("plugins", "skills", "agents", "commands") and entry.is_dir():
            for root, dirs, files in os.walk(entry, followlinks=False):
                for n in dirs + files:
                    q = os.path.join(root, n)
                    if os.path.islink(q):
                        problems += _outside(_real(q), roots, f"a link under ~/.claude/{entry.name}")
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
                cmd = cmd.replace("${HOME}", os.fspath(home)).replace("$HOME", os.fspath(home))
                try:
                    words = shlex.split(cmd)
                except ValueError:
                    continue
                for w in words:
                    for cand in (w, w.split("=", 1)[1] if "=" in w else ""):
                        cand = os.path.expanduser(cand)
                        if os.path.isabs(cand) and os.path.exists(cand):
                            problems += _outside(_real(cand), roots, f"a script the operator's {event} hook runs")
    return problems


def _managed_hook_commands(path: str, doc: Mapping[str, Any], problems: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    hooks = doc.get("hooks", {})
    if not isinstance(hooks, Mapping):
        problems.append(f"{path}: hooks is not an object")
        return out
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else [None]:
            hs = group.get("hooks") if isinstance(group, Mapping) else None
            for h in hs if isinstance(hs, list) else [None]:
                cmd = h.get("command") if isinstance(h, Mapping) else None
                if not isinstance(cmd, str) or not isinstance(h, Mapping) or h.get("type") != "command":
                    problems.append(f"{path}: {event} hook is not a plain command")
                    continue
                out.append((str(event), cmd))
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_trust_file(platform: str, environ: Mapping[str, str] | None, home: Path | None) -> Path:
    environ = os.environ if environ is None else environ
    home = Path(os.path.expanduser("~")) if home is None else home
    return _base_dir("state", platform, environ, home) / "managed_trust.json"


def load_managed_trust(platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                       home: Path | None = None) -> frozenset[str]:
    f = _managed_trust_file(platform, environ, home)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return frozenset(str(x) for x in data.get("hooks", []))
    except (OSError, ValueError, AttributeError):
        return frozenset()


def trust_managed(platform: str = sys.platform, environ: Mapping[str, str] | None = None,
                  home: Path | None = None) -> list[str]:
    """D13, the operator's explicit act: record the exact managed hook commands now in force.
    Returns the commands it recorded, so the operator sees what they vouched for."""
    problems: list[str] = []
    cmds = [c for p, d in load_managed_settings(platform) if isinstance(d, Mapping)
            for _, c in _managed_hook_commands(p, d, problems)]
    f = _managed_trust_file(platform, environ, home)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = f.with_name(f".managed_trust.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"hooks": sorted(_sha(c) for c in cmds)}, fh)
    os.replace(tmp, f)
    return cmds


def codex_config_overrides() -> dict[str, Any]:
    """What S4's broker writes into the entity's CODEX_HOME config.toml before it accepts a client.
    thread/start and thread/resume take only a sandbox MODE string (codex 0.146.1 schema), so the
    detailed workspace-write policy that check_codex_effective_policy requires (no /tmp, no
    $TMPDIR, no network) has to come from config, not from the request (L3 r1, codex 8)."""
    return {"sandbox_mode": "workspace-write",
            "sandbox_workspace_write": {"network_access": False, "exclude_slash_tmp": True,
                                        "exclude_tmpdir_env_var": True, "writable_roots": []}}


_ISOLATING_FLAGS = frozenset({"-I", "-B", "-E", "-P", "-s", "-S", "-u"})


def security_inputs_digest(entity: Path, python: str, claude_bin: str, platform: str,
                           environ: Mapping[str, str] | None, home: Path | None) -> str:
    """Everything outside the argv that changes what a hosted spawn runs, hashed: the managed
    settings documents, the operator's managed-hook trust pin, the hook-tree pin, and the identity
    of the interpreter and the claude binary. S3 rebuilds the profile before EVERY spawn and
    refuses when this differs from the canaried value (L3 r2, codex 4: a managed-settings change
    after a passing canary left the old digest standing)."""
    parts: list[Any] = [load_managed_settings(platform), sorted(load_managed_trust(platform, environ, home)),
                        hook_pin(entity)]
    for p in (python, claude_bin):
        try:
            st = os.stat(_real(p))
            parts.append([os.fspath(_real(p)), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns])
        except OSError:
            parts.append([p, None])
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """The operator's two explicit acts, run from their own terminal:
    ``python -m levain.host_guard repin <entity>`` after changing an entity's hook code (D9), and
    ``python -m levain.host_guard trust-managed`` to vouch for the machine's managed-settings hook
    commands exactly as they are now (D13). Each prints what it recorded."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m levain.host_guard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("repin", help="re-record the hook-tree pin for an entity")
    rp.add_argument("entity")
    sub.add_parser("trust-managed", help="vouch for the managed-settings hook commands now in force")
    a = ap.parse_args(argv)
    if a.cmd == "trust-managed":
        try:
            cmds = trust_managed()
        except (HostRefusal, OSError) as exc:
            print(f"trust-managed refused: {exc}", file=sys.stderr)
            return 1
        print(f"trusted {len(cmds)} managed hook command(s):")
        for c in cmds:
            print(f"  {c}")
        return 0
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
