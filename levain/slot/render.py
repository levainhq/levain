"""``render``'s pure half: the bytes of ``<S>/root/*`` and the onboarding pre-seed (CONTRACT §2.1, §3.4, §5.3, §8).

Every function here is PURE: the render blob (:class:`levain.slot.runtime_grammar.RenderBlob`) and the alloc facts the
render needs (:class:`RenderFacts`) are parameters, and the result is file bytes with the owner, group and mode their
§2.1 row gives. Writing them into ``<S>`` is the helper's (session 2d); nothing here opens, stats or writes a file.

What it renders, per (host, vendor, class):

- ``<S>/root/settings.json``: the Claude Code ``--settings`` flag layer, with §5.3's pins whatever the blob says.
- ``<S>/root/codex.toml``: Codex's config, with ``approval_policy = "never"``; a Codex session gets the same bytes
  pre-seeded as ``<S>/config/codex/config.toml`` (§5.5: the one ``config.toml`` under ``CODEX_HOME`` ``start``
  accepts).
- ``<S>/root/gitconfig`` (``GIT_CONFIG_GLOBAL``), ``<S>/root/env`` (§3.4, exactly), ``<S>/root/profile.sb`` (macOS,
  §8.1 + §24). ⛔ A profile never holds a ``syscall`` operation (trg-069): :func:`render_profile` refuses its own
  output if the word appears anywhere in it.
- the vendors' onboarding pre-seed under ``<S>/config/`` so the TUI starts with no dialog.

Plus the checks: the effective-config read-back (:func:`readback_refusals`, refused ``config-readback:<key>``) and
the §5.5 discovery check (:func:`discovery_refusals`, refused ``planted-discovery:<root>-<name>``), which ``start``
runs before exec, and the repo-layer loosening check (:func:`repo_loosening_refusals`, host_guard's lists, refused
``loosening:<key>``), which the supervisor runs on the clone before it bundles it.

Refusal names travel on the helper's stdout through ``runtime_grammar.refusal_line``, whose name alphabet is
lowercase words joined by ``-`` with one ``:`` qualifier; a key or file name is folded into that alphabet by
:func:`wire_name` (``disableBypassPermissionsMode`` -> ``disable-bypass-permissions-mode``). The exact key or path
rides ``Refused.detail``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from levain.contracts import ContractError
from levain.contracts import canon
from levain.firing.hands import HostOS
from levain.slot import Refused
from levain.slot.bwrap import LEVAIN_BWRAP
from levain.slot.ids import SlotId, loopback_alias
from levain.slot.layout import RUN, STORE
from levain.slot.runtime_grammar import AUTHS, KINDS, RenderBlob, parse_stdin

VENDOR_PREFIX: Mapping[str, PurePosixPath] = {"darwin": PurePosixPath("/usr/local/lib/levain-vendor"),
                                              "linux": PurePosixPath("/usr/lib/levain-vendor")}   # §2.1
GWPORT_BASE = 47100            # §11 V9: macOS gw port = 47100 + n; Linux: the forwarder's 47100 in the netns (§3.4)
LINUX_ASK_RULE = "Bash(dangerouslyDisableSandbox:true)"   # §5.3
MACH_ALLOW = ("com.apple.trustd.agent", "com.apple.system.opendirectoryd.libinfo")   # §8.1 (R-M2, §21)
#: §8.1 "/dev/null-class devices": the measured list R-M2 ran (runs/macos-RM2/profile.sb), less its
#: ``^/dev/ttys[0-9]+$`` regex (L2 M3): with it the slot could open ANY pty for write, and a setgid-tty
#: ``write(1)`` put raw escapes on the operator's terminals past the supervisor relay. The vendor writes its
#: terminal through the pty fds it inherits (no path open) and ``/dev/tty``; no other tty path is writable.
DEVICE_LITERALS = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/dtracehelper", "/dev/ptmx")
SLOT_WRITABLE = ("repo", "config", "xdg", "tmp", "sock")   # §8.1 file-write* and §2.1's slot-owned dirs
#: DECISION: the verifier driver's one writable dir is its HOME, ``<S>/driver``, which is also its ``TMPDIR``
#: (render_env); DAC keeps the child out of it (the driver owns it), so the profile allows it whole.
DRIVER_WRITABLE = ("driver",)

#: §5.3 + §24: the kinds for which an egress session gets its own loopback alias on macOS.
LOOPBACK_KINDS = frozenset({"interactive", "login"})
#: The env var each brokered vendor reads its session token from (§3.2: CC ``ANTHROPIC_API_KEY``; Codex the
#: ``env_key`` of its rendered provider block). The token is never rendered: ``start`` adds it at exec (§3.2).
TOKEN_ENV = {"claude": "ANTHROPIC_API_KEY", "codex": "OPENAI_API_KEY"}
QUEUE_CAP_ENV = "LEVAIN_QUEUE_CAP"         # §3.2, added at exec like the token
QUEUE_SOCKET_ENV = "LEVAIN_QUEUE_SOCKET"   # §3.2, not a secret: rendered
#: DECISION: the provider id Codex's brokered block is filed under (runs/macos-RB 1e measured the shape as "lv").
CODEX_PROVIDER = "levain"

ROUTE_TYPES = ("egress", "brokered", "subscription_passthrough")   # §4.1
_TERM_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}", re.ASCII)
#: DECISION: the slot's ``/var/folders/<xx>/<id>`` subtree (the parent of its ``DARWIN_USER_DIR``, as R-M2 measured
#: it), its real ``/private`` path, in the alphabet ``confstr`` produces. Never a path a profile string must escape.
_VAR_FOLDERS_RE = re.compile(r"/private/var/folders/[A-Za-z0-9_+-]{1,8}/[A-Za-z0-9_+-]{1,64}", re.ASCII)
_HOOK_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}", re.ASCII)
_HOOK_EVENT_RE = re.compile(r"[A-Za-z]{1,64}", re.ASCII)
_HOOK_MATCHER_RE = re.compile(r"[\x20-\x7e]{0,256}", re.ASCII)
_SBPL_SAFE_RE = re.compile(r"[A-Za-z0-9/._+-]+", re.ASCII)

DRIVER_INPUT_DEPTH = 64   # §5.4: driver_input nests no deeper than 64

#: The read-back's "no such key". This module runs as root under the host's own ``/usr/bin/python3`` (macOS: CLT
#: 3.9), so it never imports ``levain.host_guard`` at module level (tomllib, 3.11+); :func:`_get` mirrors host_guard's.
_MISSING = object()


def _get(d: Any, keys: Iterable[str]) -> Any:
    for k in keys:
        if not isinstance(d, Mapping) or k not in d:
            return _MISSING
        d = d[k]
    return d


# --- wire names --------------------------------------------------------------------------------------------

def wire_name(text: str) -> str:
    """``text`` folded into the refusal-name alphabet: camelCase split, every other run of non-alphanumerics a
    single ``-``, lowercase. ``sandbox.excludedCommands`` -> ``sandbox-excluded-commands``; ``.mcp.json`` ->
    ``mcp-json``; ``AGENTS.override.md`` -> ``agents-override-md``."""
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", text)
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def _refused(prefix: str, key: str, detail: str) -> Refused:
    name = f"{prefix}:{wire_name(key)}"
    if len(name) > 64:   # refusal_line's cap; never let a long key turn a named refusal into "internal"
        name = name[:64].rstrip("-")
    return Refused(name, detail)


# --- facts --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class HookTemplate:
    """One of Levain's root-owned hook templates (session 4 owns the set): the executable's bytes and the Claude
    Code event + matcher it is wired under. ``render`` copies the bytes to ``<S>/root/hooks/<name>`` and wires the
    absolute path; it never reads a template off disk."""

    body: bytes
    event: str
    matcher: str | None = None


@dataclass(frozen=True)
class RenderFacts:
    """The alloc facts a render needs, as explicit parameters (§7.1 holds them; reading alloc is the verb's).

    ``slot_uid`` / ``slot_gid``: egress, the slot ``_levain_s<n>``; verifier, the CHILD ``_levain_c<n>`` (who owns
    ``<S>/config``). ``driver_uid`` / ``driver_gid``: verifier only, ``_levain_v<n>``. ``gwport``: egress only.
    ``route_types``: route id -> its §4.1 type, for at least every route the blob names. ``var_folders``: macOS
    only, the ``/var/folders`` subtree of each uid the profile confines (egress: the slot; verifier: child and
    driver). ``term``: ``TERM`` from the supervisor, or none."""

    host: HostOS
    session: str
    slot: SlotId
    kind: str
    vendor: str
    auth: str
    slot_uid: int
    slot_gid: int
    gwport: int | None = None
    route_types: Mapping[str, str] = field(default_factory=dict)
    term: str | None = None
    var_folders: tuple[str, ...] = ()
    driver_uid: int | None = None
    driver_gid: int | None = None
    hook_templates: Mapping[str, HookTemplate] = field(default_factory=dict)

    @property
    def S(self) -> PurePosixPath:
        return PurePosixPath(STORE[self.host]) / "live" / self.session

    @property
    def R(self) -> PurePosixPath:
        return PurePosixPath(RUN[self.host])


@dataclass(frozen=True)
class RenderedFile:
    """``path`` is relative to ``<S>``; owner, group and mode are its §2.1 row."""

    path: str
    data: bytes
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class Rendered:
    files: tuple[RenderedFile, ...]
    env: Mapping[str, Mapping[str, str]]      # role ("slot" | "child" | "driver") -> exactly the env it execs with
    secret_env: tuple[str, ...]               # names ``start`` adds at exec (§3.2), never rendered
    expected: Mapping[str, Mapping[str, Any]]  # vendor -> the effective config read-back must find
    own_loopback: bool

    def file(self, path: str) -> RenderedFile:
        for f in self.files:
            if f.path == path:
                return f
        raise KeyError(path)


# --- checks on the (blob, facts) pair ----------------------------------------------------------------------

def gwport_for(host: HostOS, slot: SlotId) -> int:
    """§3.4 + V9: macOS ``47100 + n``; Linux the netns forwarder's ``47100``."""
    return GWPORT_BASE + slot.index if host == "darwin" else GWPORT_BASE


def own_loopback(blob: RenderBlob, facts: RenderFacts) -> bool:
    """§24: TRUE for an ``interactive`` / ``login`` egress session on macOS, FALSE for dispatch, planner and the none
    class. DECISION: the blob may turn it off (``false``) but never on where §24 says false (refused
    ``own-loopback-not-allowed``). Linux: always false here; the netns already gives the slot its own ``lo``."""
    allowed = facts.host == "darwin" and facts.slot.slot_class == "egress" and facts.kind in LOOPBACK_KINDS
    if blob.allow_own_loopback is True and not allowed:
        raise Refused("own-loopback-not-allowed", f"{facts.host} {facts.slot.slot_class} {facts.kind}")
    return allowed and blob.allow_own_loopback is not False


def _brokered_route(blob: RenderBlob, facts: RenderFacts) -> str | None:
    by_type: dict[str, list[str]] = {t: [] for t in ROUTE_TYPES}
    for r in blob.routes:
        t = facts.route_types.get(r)
        if t not in ROUTE_TYPES:
            raise Refused("route-unknown", r)
        by_type[t].append(r)
    # §5.3: <= 1 brokered + <= 1 subscription_passthrough per vendor (a session has one vendor).
    if len(by_type["brokered"]) > 1 or len(by_type["subscription_passthrough"]) > 1:
        raise Refused("routes-per-vendor", f"{len(by_type['brokered'])} brokered, "
                                           f"{len(by_type['subscription_passthrough'])} passthrough")
    # DECISION: a brokered session names exactly its brokered route; a passthrough session its passthrough route;
    # neither names the other's (the base URL and the CONNECT set come from these).
    want = {"brokered": ("brokered", "subscription_passthrough"),
            "passthrough": ("subscription_passthrough", "brokered")}.get(facts.auth)
    if want is not None:
        need, forbid = want
        if len(by_type[need]) != 1 or by_type[forbid]:
            raise Refused("auth-routes-mismatch", f"auth {facts.auth}")
    elif by_type["brokered"] or by_type["subscription_passthrough"]:
        raise Refused("auth-routes-mismatch", f"auth {facts.auth}")
    return by_type["brokered"][0] if by_type["brokered"] else None


def check_facts(blob: RenderBlob, facts: RenderFacts) -> None:
    """The (blob, facts) consistency the render itself depends on. Refused by name before any byte is produced."""
    if facts.host not in STORE:
        raise Refused("unsupported-host", repr(facts.host))
    if facts.kind not in KINDS:
        raise Refused("bad-token", f"kind {facts.kind!r}")
    if (facts.kind == "verifier") != (facts.slot.slot_class == "none"):
        raise Refused("class-kind-mismatch", f"kind {facts.kind} is not {facts.slot.slot_class} class")
    if blob.vendor != facts.vendor:                                     # §5.3 vendor == prepare's
        raise Refused("vendor-mismatch", f"{blob.vendor} != {facts.vendor}")
    if facts.auth not in AUTHS:
        raise Refused("bad-token", f"auth {facts.auth!r}")
    if facts.slot.slot_class == "none" and (facts.auth != "none" or blob.hooks):
        # §5.1 prepare: a none-class session is auth none; §5.6: no Levain socket, so no hook that files to one.
        raise Refused("class-auth-mismatch" if facts.auth != "none" else "hooks-not-allowed", "none class")
    for n, v in (("slot_uid", facts.slot_uid), ("slot_gid", facts.slot_gid)):
        if type(v) is not int or v <= 0:
            raise Refused("bad-config", f"{n} {v!r}")
    if facts.slot.slot_class == "none":
        if blob.routes:                                                 # §5.3 none class: empty
            raise Refused("routes-not-empty", "none class")
        if facts.gwport is not None:
            raise Refused("gwport-mismatch", "none class has no gateway")
        for n, v in (("driver_uid", facts.driver_uid), ("driver_gid", facts.driver_gid)):
            if type(v) is not int or v <= 0:
                raise Refused("bad-config", f"{n} {v!r}")
        if blob.verifier_steps is None or blob.driver is None:          # §5.3 verifier only, both present
            raise Refused("verifier-keys-missing", "verifier")
    else:
        if facts.gwport != gwport_for(facts.host, facts.slot):
            raise Refused("gwport-mismatch", f"{facts.gwport!r}")
        if blob.verifier_steps is not None or blob.driver is not None:  # §5.3 absent otherwise
            raise Refused("verifier-keys-unexpected", facts.kind)
        if facts.driver_uid is not None or facts.driver_gid is not None:
            raise Refused("bad-config", "driver ids on an egress session")
    if facts.term is not None and (type(facts.term) is not str or not _TERM_RE.fullmatch(facts.term)):
        raise Refused("bad-config", "term")
    if facts.host == "darwin":
        want = 2 if facts.slot.slot_class == "none" else 1
        if len(facts.var_folders) != want or len(set(facts.var_folders)) != want:
            raise Refused("bad-config", f"var_folders: {want} wanted")
        for p in facts.var_folders:
            if type(p) is not str or not _VAR_FOLDERS_RE.fullmatch(p):
                raise Refused("bad-config", f"var_folders {str(p)[:96]!r}")
    elif facts.var_folders:
        raise Refused("bad-config", "var_folders is macOS only")
    names = set(blob.hooks)
    if set(facts.hook_templates) != names:
        raise Refused("hook-template-missing", ",".join(sorted(names ^ set(facts.hook_templates))))
    for name, t in facts.hook_templates.items():
        if not _HOOK_NAME_RE.fullmatch(name) or not isinstance(t, HookTemplate) or type(t.body) is not bytes \
                or not _HOOK_EVENT_RE.fullmatch(t.event) \
                or (t.matcher is not None and not _HOOK_MATCHER_RE.fullmatch(t.matcher)):
            raise Refused("bad-hook-template", name[:64])
    # DECISION: Codex's hook wiring is not in this contract revision, so a Codex session asking for a hook is
    # refused by name rather than rendered with the hook silently unwired.
    if names and facts.vendor == "codex":
        raise Refused("hooks-not-wired:codex", ",".join(sorted(names)))
    _brokered_route(blob, facts)
    own_loopback(blob, facts)


# --- env (§3.4) ---------------------------------------------------------------------------------------------

def _base_env(facts: RenderFacts, home: PurePosixPath, user: str) -> dict[str, str]:
    S = facts.S
    env = {
        "HOME": str(home), "USER": user, "LOGNAME": user,
        "PATH": f"{VENDOR_PREFIX[facts.host]}/bin:/usr/bin:/bin",
        "TMPDIR": f"{S}/tmp", "CLAUDE_CODE_TMPDIR": f"{S}/tmp", "TMPPREFIX": f"{S}/tmp/zsh",
        "XDG_DATA_HOME": f"{S}/xdg/data", "XDG_CACHE_HOME": f"{S}/xdg/cache",
        "XDG_CONFIG_HOME": f"{S}/xdg/config", "XDG_STATE_HOME": f"{S}/xdg/state",
        "npm_config_cache": f"{S}/xdg/cache/npm",
        "CLAUDE_CONFIG_DIR": f"{S}/config/claude", "CODEX_HOME": f"{S}/config/codex",
        "GIT_CONFIG_GLOBAL": f"{S}/root/gitconfig", "GIT_CONFIG_NOSYSTEM": "1", "DISABLE_AUTOUPDATER": "1",
    }
    if facts.term is not None:
        env["TERM"] = facts.term
    return env


def render_env(blob: RenderBlob, facts: RenderFacts) -> dict[str, dict[str, str]]:
    """§3.4 exactly, per role. Egress: one env, ``slot``. Verifier: ``child`` (``HOME=<S>``, user ``_levain_c<n>``)
    and ``driver`` (``HOME=<S>/driver``, user ``_levain_v<n>``).

    DECISION (driver): §3.4 fixes only its ``HOME``; it gets HOME/USER/LOGNAME/PATH, ``TMPDIR=<S>/driver`` (the one
    dir it can write) and TERM, nothing pointing into the child's dirs. DECISION (own loopback): the alias joins
    ``NO_PROXY`` so a client honouring the proxy vars reaches ``$HOST`` directly, not through the front."""
    check_facts(blob, facts)
    S = facts.S
    if facts.slot.slot_class == "none":
        driver_name, child_name = facts.slot.accounts
        child = _base_env(facts, S, child_name)
        driver = {"HOME": f"{S}/driver", "USER": driver_name, "LOGNAME": driver_name,
                  "PATH": child["PATH"], "TMPDIR": f"{S}/driver"}
        if facts.term is not None:
            driver["TERM"] = facts.term
        return {"child": child, "driver": driver}
    env = _base_env(facts, S, facts.slot.primary)
    proxy = f"http://127.0.0.1:{facts.gwport}"
    no_proxy = ["127.0.0.1", "localhost", "::1"]
    if own_loopback(blob, facts):
        alias = loopback_alias(facts.slot)
        env["LEVAIN_LOOPBACK_HOST"] = alias           # §24
        env["HOST"] = alias
        no_proxy.append(alias)
    env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "NO_PROXY": ",".join(no_proxy)})
    brokered = _brokered_route(blob, facts)
    if brokered is not None and facts.vendor == "claude":
        env["ANTHROPIC_BASE_URL"] = f"{proxy}/r/{brokered}"
    if "levain_queue" in blob.hooks:                  # §5.3: levain_queue = the queue grant
        env[QUEUE_SOCKET_ENV] = f"{facts.R}/qb/slot{facts.slot.index}/q.sock"
    return {"slot": env}


def secret_env_names(blob: RenderBlob, facts: RenderFacts) -> tuple[str, ...]:
    """The handle names ``start`` adds at exec from ``SECRETS`` (§3.2): the token's carrier on a brokered session,
    the queue cap on a queue grant. Never rendered, never in argv."""
    out = []
    if facts.slot.slot_class == "egress" and _brokered_route(blob, facts) is not None:
        out.append(TOKEN_ENV[facts.vendor])
    if "levain_queue" in blob.hooks:
        out.append(QUEUE_CAP_ENV)
    return tuple(out)


def env_text(env: Mapping[str, str]) -> bytes:
    """``KEY=VALUE`` lines, sorted by key; a value holding a newline or NUL is refused (one line per variable)."""
    lines = []
    for k in sorted(env):
        v = env[k]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or "\n" in v or "\x00" in v or "\r" in v:
            raise Refused("bad-env", k[:64])
        lines.append(f"{k}={v}\n")
    return "".join(lines).encode("utf-8")


# --- Claude Code flag layer + onboarding (§5.3) -------------------------------------------------------------

def render_cc_settings(blob: RenderBlob, facts: RenderFacts) -> dict:
    """The ``--settings`` layer. Pinned whatever the blob says (§5.3): macOS ``sandbox.enabled:false``; Linux
    ``true`` + ``failIfUnavailable:true`` + ``bwrapPath`` (Levain's copy) + the ask rule; both
    ``disableBypassPermissionsMode:"disable"``, ``excludedCommands: []`` and ``disableAllHooks: false`` (L2 M6:
    a repo or local layer setting it true would switch Levain's gate hooks off)."""
    check_facts(blob, facts)
    sandbox: dict[str, Any] = {"enabled": facts.host == "linux", "excludedCommands": []}
    permissions: dict[str, Any] = {"disableBypassPermissionsMode": "disable"}
    if facts.host == "linux":
        sandbox.update({"failIfUnavailable": True, "bwrapPath": str(LEVAIN_BWRAP)})
        permissions["ask"] = [LINUX_ASK_RULE]
    out: dict[str, Any] = {"sandbox": sandbox, "permissions": permissions, "disableAllHooks": False}
    if blob.model is not None:
        out["model"] = blob.model
    if blob.hooks:
        events: dict[str, list] = {}
        for name in sorted(blob.hooks):
            t = facts.hook_templates[name]
            entry: dict[str, Any] = {"hooks": [{"type": "command", "command": f"{facts.S}/root/hooks/{name}"}]}
            if t.matcher is not None:
                entry["matcher"] = t.matcher
            events.setdefault(t.event, []).append(entry)
        out["hooks"] = events
    return out


def render_cc_onboarding(facts: RenderFacts) -> dict:
    """``<S>/config/claude/.claude.json``: onboarding done and ``<S>/repo`` trusted, no credential in it (the shape
    runs/macos-RM2 raw/100 and runs/macos-RB raw/044 measured; R-M2 found the TUI keeps it in ``CLAUDE_CONFIG_DIR``)."""
    return {"hasCompletedOnboarding": True, "theme": "dark", "bypassPermissionsModeAccepted": False,
            "projects": {f"{facts.S}/repo": {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


# --- Codex (§5.3) ---------------------------------------------------------------------------------------------

def _toml_str(s: str) -> str:
    out = ['"']
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def codex_config(blob: RenderBlob, facts: RenderFacts) -> dict[str, Any]:
    """Codex's settings as data (top-level keys, then tables). Pinned: ``approval_policy = "never"`` (§5.3).

    DECISION (macOS): ``sandbox_mode = "danger-full-access"``: Codex's own Seatbelt cannot nest inside the profile
    (R-M2 ran it with its sandbox bypassed), so as with CC the OS wall is the sandbox. Linux leaves Codex's
    sandbox_mode at its default (no run measured it inside the view). DECISION: ``cli_auth_credentials_store =
    "file"``, so a passthrough login lands in ``auth.json`` (the file ``keep/`` parks, §2.1), never a keychain.
    DECISION: ``[projects."<S>/repo"] trust_level = "trusted"``, the trust pre-seed (no run measured Codex's TUI
    trust dialog). Brokered: the provider block runs/macos-RB 1e measured (env-only base URL did not work)."""
    check_facts(blob, facts)
    top: dict[str, Any] = {"approval_policy": "never", "cli_auth_credentials_store": "file"}
    if facts.host == "darwin":
        top["sandbox_mode"] = "danger-full-access"
    if blob.model is not None:
        top["model"] = blob.model
    tables: dict[str, dict[str, Any]] = {}
    brokered = _brokered_route(blob, facts)
    if brokered is not None and facts.vendor == "codex":
        top["model_provider"] = CODEX_PROVIDER
        tables[f"model_providers.{CODEX_PROVIDER}"] = {
            "name": CODEX_PROVIDER, "base_url": f"http://127.0.0.1:{facts.gwport}/r/{brokered}/v1",
            "env_key": TOKEN_ENV["codex"], "wire_api": "responses"}
    tables[f"projects.{_toml_str(str(facts.S / 'repo'))}"] = {"trust_level": "trusted"}
    return {"top": top, "tables": tables}


def _toml_value(v: Any) -> str:
    if type(v) is str:
        return _toml_str(v)
    if type(v) is bool:
        return "true" if v else "false"
    raise Refused("internal", f"toml value {type(v).__name__}")


def codex_toml(blob: RenderBlob, facts: RenderFacts) -> bytes:
    cfg = codex_config(blob, facts)
    lines = [f"{k} = {_toml_value(v)}" for k, v in sorted(cfg["top"].items())]
    for name in sorted(cfg["tables"]):
        lines.append("")
        lines.append(f"[{name}]")
        lines.extend(f"{k} = {_toml_value(v)}" for k, v in sorted(cfg["tables"][name].items()))
    return ("\n".join(lines) + "\n").encode("utf-8")


# --- gitconfig ------------------------------------------------------------------------------------------------

def _git_value(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gitconfig_text(blob: RenderBlob) -> bytes:
    """``GIT_CONFIG_GLOBAL``: the blob's identity, every value quoted (``#`` and ``;`` start a comment unquoted).
    DECISION: no identity = an empty file (git reads it as no config)."""
    if blob.git_identity is None:
        return b""
    return (f"[user]\n\tname = {_git_value(blob.git_identity.name)}\n"
            f"\temail = {_git_value(blob.git_identity.email)}\n").encode("utf-8")


# --- macOS profile (§8.1, §24) ---------------------------------------------------------------------------------

def _q(path: str) -> str:
    if not _SBPL_SAFE_RE.fullmatch(path) or ".." in path.split("/"):
        raise Refused("internal", f"profile path {path[:96]!r}")
    return f'"{path}"'


def assert_no_syscall(text: str) -> None:
    """⛔ trg-069: a rendered profile may never hold a ``syscall`` operation. Checked on the WHOLE text (comments
    included), so no spelling of one can slip past a comment-aware check."""
    if "syscall" in text.lower():
        raise Refused("profile-syscall-rule", "a rendered profile names syscall")


def render_profile(blob: RenderBlob, facts: RenderFacts) -> bytes:
    """§8.1's classes as SBPL, every path the real ``/private/...`` path, in the forms runs/macos-RM2 and
    runs/macos-RM1b measured (``vm/rm2_profile.sb.in``, ``vm/rm1b_profile.sb.in``).

    Network block (§24): egress without own loopback = outbound to ``localhost:<gwport>`` only; with it,
    outbound/bind/inbound ``localhost:*`` (pf isolates the slots); none class = nothing. AF_UNIX: ``<S>/sock``
    both ways (R-M2: a slot-bound socket needs bind + inbound there), the queue socket's dir on a queue grant, the
    planner socket's dir for a planner session. DECISION: the two Levain sockets are allowed by ``subpath`` of their
    per-slot root-made dir (the one unix-socket filter form a run measured; each dir holds one socket)."""
    check_facts(blob, facts)
    if facts.host != "darwin":
        raise Refused("unsupported-host", "profile.sb is macOS only")
    S = str(facts.S)
    writable = SLOT_WRITABLE + (DRIVER_WRITABLE if facts.slot.slot_class == "none" else ())
    lines = [
        "(version 1)",
        f";; Levain session profile (CONTRACT §8.1) for {facts.session} on {facts.slot.tag}, kind {facts.kind}.",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
        "  " + " ".join(f"(subpath {_q(f'{S}/{d}')})" for d in writable),
        "  " + " ".join(f"(subpath {_q(vf)})" for vf in facts.var_folders),
        "  " + " ".join(f"(literal {_q(d)})" for d in DEVICE_LITERALS) + ")",
        "(deny network-outbound)",
        "(deny network-bind)",
        "(deny network-inbound)",
    ]
    if facts.slot.slot_class == "egress":
        if own_loopback(blob, facts):
            lines += ['(allow network-outbound (remote ip "localhost:*"))',
                      '(allow network-bind (local ip "localhost:*"))',
                      '(allow network-inbound (local ip "localhost:*"))']
        else:
            lines.append(f'(allow network-outbound (remote ip "localhost:{facts.gwport}"))')
    sock = _q(f"{S}/sock")
    lines += [f"(allow network-outbound (remote unix-socket (subpath {sock})))",
              f"(allow network-bind (local unix-socket (subpath {sock})))",
              f"(allow network-inbound (local unix-socket (subpath {sock})))"]
    R, n = str(facts.R), facts.slot.index
    if "levain_queue" in blob.hooks:
        lines.append(f"(allow network-outbound (remote unix-socket (subpath {_q(f'{R}/qb/slot{n}')})))")
    if facts.kind == "planner":
        lines.append(f"(allow network-outbound (remote unix-socket (subpath {_q(f'{R}/planner/slot{n}')})))")
    lines += ["(deny mach-lookup)", "(deny mach-register)"]
    lines += [f'(allow mach-lookup (global-name "{m}"))' for m in MACH_ALLOW]
    text = "\n".join(lines) + "\n"
    assert_no_syscall(text)
    return text.encode("utf-8")


# --- the whole render ------------------------------------------------------------------------------------------

def render(blob: RenderBlob, facts: RenderFacts) -> Rendered:
    """Every file ``render`` (3) writes under ``<S>`` (except ``<S>/in/``, §5.4's), with its §2.1 owner and mode.

    Egress ``<S>/root/*``: root : ``_levain_s<n>`` 0640 (hooks 0750). Verifier: root : root 0644 (hooks 0755), as its
    ``root/`` layer is 0755 / 0644 (§2.1). Onboarding files: the slot (verifier: the child), 0600, inside its own
    0700 config dir. DECISION: the verifier driver's env is ``<S>/root/driver.env`` beside ``env`` (the child's):
    §2.1 names one ``env`` file and §3.4 two envs."""
    check_facts(blob, facts)
    verifier = facts.slot.slot_class == "none"
    rgid, fmode, xmode = (0, 0o644, 0o755) if verifier else (facts.slot_gid, 0o640, 0o750)
    envs = render_env(blob, facts)
    settings = render_cc_settings(blob, facts)
    toml = codex_toml(blob, facts)
    files = [
        RenderedFile("root/settings.json", _json_bytes(settings), 0, rgid, fmode),
        RenderedFile("root/codex.toml", toml, 0, rgid, fmode),
        RenderedFile("root/gitconfig", gitconfig_text(blob), 0, rgid, fmode),
        RenderedFile("root/env", env_text(envs["child" if verifier else "slot"]), 0, rgid, fmode),
    ]
    if verifier:
        files.append(RenderedFile("root/driver.env", env_text(envs["driver"]), 0, rgid, fmode))
    if facts.host == "darwin":
        files.append(RenderedFile("root/profile.sb", render_profile(blob, facts), 0, rgid, fmode))
    for name in sorted(blob.hooks):
        files.append(RenderedFile(f"root/hooks/{name}", facts.hook_templates[name].body, 0, rgid, xmode))
    # The onboarding pre-seed is the session vendor's only: a Codex session's CLAUDE_CONFIG_DIR stays empty, and a
    # Claude session's CODEX_HOME holds no config.toml (so §5.5 refuses any that appears there).
    if facts.vendor == "claude":
        files.append(RenderedFile("config/claude/.claude.json", _json_bytes(render_cc_onboarding(facts)),
                                  facts.slot_uid, facts.slot_gid, 0o600))
        expected = expected_effective_cc(blob, facts)
    else:
        files.append(RenderedFile("config/codex/config.toml", toml, facts.slot_uid, facts.slot_gid, 0o600))
        expected = expected_effective_codex(blob, facts)
    return Rendered(files=tuple(files), env=envs, secret_env=secret_env_names(blob, facts),
                    expected={facts.vendor: expected}, own_loopback=own_loopback(blob, facts))


def render_from_stdin(raw: bytes, facts: RenderFacts) -> Rendered:
    """The helper's entry: the §5.3 blob bytes through ``runtime_grammar.parse_stdin`` (which owns the unknown-key
    refusal, ``unknown-key``) and then :func:`render`."""
    blob = parse_stdin("render", raw)
    assert isinstance(blob, RenderBlob)
    return render(blob, facts)


# --- effective-config read-back (§5.3) ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Contains:
    """A list the effective config must CONTAIN (a merge may add tightening entries, never drop ours)."""

    items: tuple[str, ...]


def expected_effective_cc(blob: RenderBlob, facts: RenderFacts) -> dict[str, Any]:
    """Every pin of :func:`render_cc_settings`, as dotted key -> the value the effective config must hold."""
    exp: dict[str, Any] = {"sandbox.enabled": facts.host == "linux", "sandbox.excludedCommands": [],
                           "permissions.disableBypassPermissionsMode": "disable", "disableAllHooks": False}
    if facts.host == "linux":
        exp.update({"sandbox.failIfUnavailable": True, "sandbox.bwrapPath": str(LEVAIN_BWRAP),
                    "permissions.ask": Contains((LINUX_ASK_RULE,))})
    return exp


def expected_effective_codex(blob: RenderBlob, facts: RenderFacts) -> dict[str, Any]:
    cfg = codex_config(blob, facts)["top"]
    return {k: cfg[k] for k in ("approval_policy", "cli_auth_credentials_store", "sandbox_mode") if k in cfg}


def _lookup(observed: Any, key: str) -> Any:
    return _get(observed, key.split("."))


def readback_refusals(expected: Mapping[str, Any], observed: Any) -> list[str]:
    """The dotted keys whose observed value differs from the expected one (a missing key differs). Equality is by
    JSON value AND type (``0`` is not ``false``)."""
    out = []
    for key in sorted(expected):
        want, got = expected[key], _lookup(observed, key)
        if isinstance(want, Contains):
            ok = type(got) is list and all(i in got for i in want.items)
        else:
            ok = got is not _MISSING and _same(got, want)
        if not ok:
            out.append(key)
    return out


def _same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return a == b


def refuse_readback(expected: Mapping[str, Any], observed: Any) -> None:
    """§5.3: effective config read-back differing = REFUSE by key (the first differing key, sorted)."""
    bad = readback_refusals(expected, observed)
    if bad:
        raise _refused("config-readback", bad[0], "differing: " + ", ".join(bad))


# --- the repo layer (BUILD_PLAN §4 row 2: a loosening layer REFUSES by key name) ---------------------------------

def repo_loosening_refusals(docs: Mapping[str, Any]) -> list[Refused]:
    """``docs``: repo-relative file name (``.claude/settings.json``, ``.claude/settings.local.json``) -> its parsed
    JSON. Refused by key: every ``sandbox`` key in host_guard's ``_LOOSENING_TRUE`` that is ``true`` and every one
    in ``_LOOSENING_NONEMPTY`` that is non-empty (the keys whose merge across layers the flag layer cannot pin),
    and a ``disableAllHooks`` that is anything but ``false`` (L2 M6; DECISION: a non-boolean is refused too, as
    its meaning to CC is unmeasured). A document that is not an object is refused ``repo-settings-malformed``.
    Hooks, permissions and every other key launch (plan §2c 3: the repo's settings are its own).

    Operator-side: the supervisor runs this on the clone it bundles, before the bundle is made (the slot clones
    exactly that bundle), never the root helper. host_guard is imported here, not at module level, so the root path
    that imports this module stays on the host's 3.9."""
    from levain.host_guard import _LOOSENING_NONEMPTY, _LOOSENING_TRUE

    out: list[Refused] = []
    for path in sorted(docs):
        doc = docs[path]
        if not isinstance(doc, Mapping):
            out.append(Refused("repo-settings-malformed", path))
            continue
        if "disableAllHooks" in doc and doc["disableAllHooks"] is not False:
            out.append(_refused("loosening", "disableAllHooks", f"{path}: not false"))
        sb = doc.get("sandbox", {})
        if not isinstance(sb, Mapping):
            out.append(Refused("repo-settings-malformed", f"{path}: sandbox"))
            continue
        for keys in _LOOSENING_TRUE:
            if _get(sb, keys) is True:
                out.append(_refused("loosening", "sandbox." + ".".join(keys), f"{path}: true"))
        for keys in _LOOSENING_NONEMPTY:
            v = _get(sb, keys)
            if v is not _MISSING and v:
                out.append(_refused("loosening", "sandbox." + ".".join(keys), f"{path}: not empty"))
    return out


def refuse_repo_loosening(docs: Mapping[str, Any]) -> None:
    bad = repo_loosening_refusals(docs)
    if bad:
        raise bad[0]


# --- §5.5 discovery names ------------------------------------------------------------------------------------

DISCOVERY: Mapping[str, tuple[str, ...]] = {
    "claude": ("settings.json", "settings.local.json", "CLAUDE.md", "AGENTS.md", "rules", "skills", "agents",
               "commands", "output-styles", "plugins", "cowork_settings.json", "daemon.json", "daemon"),
    "codex": ("config.toml", ".env", "AGENTS.md", "AGENTS.override.md", "hooks.json", "rules", "agents",
              "environments.toml"),
    "home": ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".mcp.json", ".gitconfig", ".config/git",
             ".config/anthropic", ".agents", ".netrc", ".bash_profile", ".bash_login",
             # the same two names where the env's XDG_CONFIG_HOME (<S>/xdg/config) sends git's and Claude's lookups
             "xdg/config/git", "xdg/config/anthropic"),
}
#: §5.5: the repo's own names, never refused, listed in the adopt preview.
REPO_DISCOVERY = (".claude", ".codex", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".agents")


def _norm(entry: str) -> str:
    return entry.strip("/")


def _present(listing: Iterable[str], name: str) -> bool:
    return any(e == name or e.startswith(name + "/") for e in (_norm(x) for x in listing))


def discovery_refusals(listings: Mapping[str, Iterable[str]], *, codex_config: bytes | None = None,
                       rendered_codex_config: bytes | None = None) -> list[Refused]:
    """``listings``: root (``claude`` = ``CLAUDE_CONFIG_DIR``, ``codex`` = ``CODEX_HOME``, ``home`` = ``HOME``) ->
    the entries present below it, as root-relative paths (a directory with or without its trailing ``/``; nested
    entries such as ``.config/git`` as such). Every §5.5 name present is refused by name. ``config.toml`` under
    ``CODEX_HOME`` is accepted only when its bytes (``codex_config``) equal the ones render made."""
    out: list[Refused] = []
    for root in ("claude", "codex", "home"):
        listing = [str(e) for e in listings.get(root, ())]
        for name in DISCOVERY[root]:
            if not _present(listing, name):
                continue
            if root == "codex" and name == "config.toml" and codex_config is not None \
                    and rendered_codex_config is not None and codex_config == rendered_codex_config \
                    and "config.toml" in (_norm(e) for e in listing):
                continue
            out.append(_refused("planted-discovery", f"{root}-{name}", f"{root}: {name}"))
    return out


def refuse_discovery(listings: Mapping[str, Iterable[str]], **kw: bytes | None) -> None:
    bad = discovery_refusals(listings, **kw)
    if bad:
        raise bad[0]


def repo_discovery_names(repo_listing: Iterable[str]) -> list[str]:
    """§5.5: the repo-level discovery names present, for the adopt preview (never a refusal)."""
    listing = [str(e) for e in repo_listing]
    return [n for n in REPO_DISCOVERY if _present(listing, n)]


# --- driver_input (§5.4) -------------------------------------------------------------------------------------

def parse_driver_input(raw: bytes) -> object:
    """§5.4: canonical JSON nested no deeper than 64, canon's refusals (floats, NaN, duplicate keys, a BOM, lone
    surrogates, out-of-range ints). Every failure is ``driver-input-mismatch``. ``canon.parse`` caps nesting at its
    own ``MAX_DEPTH``, which is shallower, so the text's depth is measured here and the parse uses canon's own hooks."""
    from levain.contracts.canon import _int, _no_constant, _no_float, _no_lone_surrogate, _pairs, text_depth
    try:
        text = raw.decode("utf-8")
        if text.startswith("﻿") or text_depth(text) > DRIVER_INPUT_DEPTH:
            raise ContractError("depth or BOM")
        obj = json.loads(text, object_pairs_hook=_pairs, parse_float=_no_float, parse_constant=_no_constant,
                         parse_int=_int)
        _no_lone_surrogate(obj)
        if canon.canonical_bytes(obj) != raw:
            raise ContractError("not canonical")
    except (ContractError, ValueError, UnicodeError, RecursionError):
        raise Refused("driver-input-mismatch", "driver_input.json is not canonical JSON") from None
    return obj


__all__ = [
    "VENDOR_PREFIX", "HookTemplate", "RenderFacts", "RenderedFile", "Rendered", "Contains", "DISCOVERY",
    "REPO_DISCOVERY", "wire_name", "gwport_for", "own_loopback", "check_facts", "render_env", "secret_env_names",
    "env_text", "render_cc_settings", "render_cc_onboarding", "codex_config", "codex_toml", "gitconfig_text",
    "assert_no_syscall", "render_profile", "render", "render_from_stdin", "expected_effective_cc",
    "expected_effective_codex", "readback_refusals", "refuse_readback", "repo_loosening_refusals",
    "refuse_repo_loosening", "discovery_refusals", "refuse_discovery", "repo_discovery_names", "parse_driver_input",
]
