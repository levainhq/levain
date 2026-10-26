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
  §8.1 + §24), and beside it for an egress slot ``<S>/root/probe.sb``, V13's probe profile (``render_profile(...,
  probe=True)``). ⛔ A profile never holds a ``syscall`` operation (trg-069): :func:`render_profile` refuses its own
  output if the word appears anywhere in it.
- ``<S>/root/readset-template/info/attributes`` (kind ``dispatch|planner|verifier``, §5.4 step 3): the clone's
  template, exactly :data:`READSET_ATTRIBUTES`.
- the vendors' onboarding pre-seed under ``<S>/config/`` so the TUI starts with no dialog.

Plus the checks: the effective-config read-back (:func:`readback_refusals`, refused ``config-readback:<key>``) and
the §5.5 discovery check (:func:`discovery_refusals`, refused ``planted-discovery:<root>-<name>``), which ``start``
runs before exec, and the repo-layer loosening check (:func:`repo_loosening_refusals`, host_guard's lists, refused
``loosening:<key>``), which the supervisor runs on the clone before it bundles it.

Refusal names travel on the helper's stdout through ``runtime_grammar.refusal_line``, whose name alphabet
(``runtime_grammar._NAME_RE``) is lowercase words joined by ``-`` with one ``:`` qualifier (the qualifier may also
carry uppercase and ``.``); a key or file name is still folded to the lowercase alphabet by
:func:`wire_name` (``disableBypassPermissionsMode`` -> ``disable-bypass-permissions-mode``). The exact key or path
rides ``Refused.detail``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

import levain
from levain.contracts import ContractError
from levain.contracts import canon
from levain.broker import policy as broker_policy
from levain.firing.hands import HostOS
from levain.slot import Refused
from levain.slot.bwrap import LEVAIN_BWRAP
from levain.slot.ids import SlotId, loopback_alias
from levain.slot.layout import RUN, STORE
from levain.slot.runtime_grammar import AUTHS, KINDS, RenderBlob, parse_stdin
from levain.slot.tickets import RENDERS_AS
from levain.slot.wall import WallFacts

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
#: §5.4 step 3: the alloc kinds whose clone takes the read-set template (``--template=<S>/root/readset-template``);
#: ``interactive`` and ``login`` clone with ``--template=`` and run no post-clone check.
READSET_KINDS = frozenset({"dispatch", "planner", "verifier"})
#: The template's one file below ``<S>``, and its one line: every checkout conversion off, so worktree bytes equal
#: the bundle's blobs whatever ``.gitattributes`` the tree holds (``$GIT_DIR/info/attributes`` outranks it). The
#: line's one home: the clone's post-clone check compares the clone's copy against these bytes.
READSET_TEMPLATE = "root/readset-template"
READSET_ATTRIBUTES_PATH = f"{READSET_TEMPLATE}/info/attributes"
READSET_ATTRIBUTES = b"* -text -eol -crlf -ident -filter -working-tree-encoding\n"
#: The env var each brokered vendor reads its session token from (§3.2; Codex the ``env_key`` of its rendered
#: provider block). The token is never rendered: ``start`` adds it at exec (§3.2). Claude Code's is
#: ``ANTHROPIC_AUTH_TOKEN`` (head ruling 1020+28, N1: CC sends it as ``Authorization: Bearer``, the header the 3b
#: front reads, MB10), never ``ANTHROPIC_API_KEY``: on that one CC 2.1.303 stops at a "use this API key?" prompt
#: before its input box (runs/s2-linux-rerun/N1_EXPERIMENT.md, both VMs).
TOKEN_ENV = {"claude": "ANTHROPIC_AUTH_TOKEN", "codex": "OPENAI_API_KEY"}
#: The names that must be ABSENT from a vendor session's env, rendered or added at exec (:func:`check_env_absent`).
ABSENT_ENV: Mapping[str, frozenset] = {"claude": frozenset({"ANTHROPIC_API_KEY"}), "codex": frozenset()}
QUEUE_CAP_ENV = "LEVAIN_QUEUE_CAP"         # §3.2, added at exec like the token
QUEUE_SOCKET_ENV = "LEVAIN_QUEUE_SOCKET"   # §3.2, not a secret: rendered
#: DECISION: the provider id Codex's brokered block is filed under (runs/macos-RB 1e measured the shape as "lv").
CODEX_PROVIDER = "levain"

ROUTE_TYPES = ("egress", "brokered", "subscription_passthrough")   # §4.1
#: wfetch DESIGN §2.4: the proxy env names a repo settings layer may not set, upper and lower case. Clearing one
#: cannot get past the OS wall (DESIGN §1.3), but every fetch would then fail with no cause shown.
PROXY_ENV = frozenset(n for b in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY") for n in (b, b.lower()))
#: wfetch RULINGS Q3: Codex's server-side search mode until Phill rules (fail closed: the front cannot see it).
CODEX_WEB_SEARCH = "disabled"
#: The Codex launch's ``-c`` overrides (``start.vendor_argv``), the layer above every config file, so ``web_search``
#: holds even if the repo's own ``.codex/config.toml`` were loaded (``codex_config`` files the repo untrusted).
CODEX_LAUNCH_ARGS = ("-c", f'web_search="{CODEX_WEB_SEARCH}"')
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
    driver). ``term``: ``TERM`` from the supervisor, or none. ``route_hosts``: route id -> its CONNECT hosts, read
    beside ``route_types`` from the same base map (``runtime_helper.base_routes``); render uses only the hosts of the
    blob's ``egress`` routes (the ``WebFetch(domain:)`` allows), and an egress route of the blob with no hosts here is
    refused (:func:`egress_hosts`)."""

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
    route_hosts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
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
    egress_hosts(blob, facts)
    own_loopback(blob, facts)


def egress_hosts(blob: RenderBlob, facts: RenderFacts) -> tuple[str, ...]:
    """The CONNECT hosts of an egress-class session's ``egress`` routes, sorted, each through the policy parser's one
    host grammar (``policy.hosts``), so no host that is not a lowercase A-label name reaches a permission rule. A
    none-class session has none (it renders no egress rule). An egress route of the blob with no ``route_hosts``
    entry, or an empty one, is refused ``egress-route-no-hosts`` (WF-1b: the base map gives every egress route its
    hosts, so a missing list is a defect, never a session whose on-list fetches all prompt); a ``route_hosts`` that is
    not a mapping of host lists, or an entry that does not parse, is refused ``bad-config``."""
    if facts.slot.slot_class != "egress":
        return ()
    if not isinstance(facts.route_hosts, Mapping):
        raise Refused("bad-config", "route_hosts")
    out: set[str] = set()
    for r in blob.routes:
        if facts.route_types.get(r) != "egress":
            continue
        raw = facts.route_hosts.get(r)
        if raw is None or (isinstance(raw, (tuple, list)) and not raw):
            raise Refused("egress-route-no-hosts", r)
        if not isinstance(raw, (tuple, list)):
            raise Refused("bad-config", f"route_hosts {r}")
        try:
            out.update(broker_policy.hosts(r, list(raw)))
        except Exception:
            raise Refused("bad-config", f"route_hosts {r}") from None
    return tuple(sorted(out))


def check_ticket(blob: RenderBlob, kind: str, slot_class: str, ticket: str | None,
                 ticket_facts: Mapping[str, object] | None) -> None:
    """CONTRACT §3.5 "Ticket facts reach render", render's half (the second gate; CONTROL's ``MINT`` claims check is
    the first): the blob against the ticket facts alloc carries (root's snapshot of CONTROL's record), refused by name
    before render acts. A session with no ticket has nothing to check here.

    - ``ticket-facts-missing``: a ticket without its facts (an interrupted ``prepare``); facts lacking a field the
      ticket's kind requires (an executor's or planner's ``route`` and ``model_requested``, a verifier's
      ``verifier_steps``: 3c-8, so no gate below compares a field against an absent one); or a verifier ticket whose
      facts hold no resolved ``verifier`` entry (no driver is authorised without one);
    - ``ticket-kind-mismatch``: the alloc's kind and class are not what the ticket's kind renders as;
    - ``ticket-routes-mismatch``: the blob's routes are not exactly the ticket's ``route`` + ``egress_routes``;
    - ``ticket-model-mismatch``: the blob's model is not the ticket's ``model_requested`` (none for a verifier);
    - ``ticket-steps-mismatch`` / ``ticket-driver-mismatch``: a verifier blob's ``verifier_steps`` are not the
      ticket's, or its ``driver`` is not the resolved entry's ``driver_template``."""
    if ticket is None:
        return
    if ticket_facts is None:
        raise Refused("ticket-facts-missing", "alloc names a ticket without its facts")
    if RENDERS_AS.get(str(ticket_facts.get("kind"))) != (kind, slot_class):
        raise Refused("ticket-kind-mismatch", f"{ticket_facts.get('kind')!s:.16} is not {kind} {slot_class}")
    required = ("verifier_steps",) if ticket_facts["kind"] == "verifier" else ("route", "model_requested")
    for k in required:
        if k not in ticket_facts:
            raise Refused("ticket-facts-missing", f"a {ticket_facts['kind']} ticket's facts without {k}")
    want = sorted([*([ticket_facts["route"]] if "route" in ticket_facts else []),
                   *ticket_facts["egress_routes"]])                                   # type: ignore[misc]
    if sorted(blob.routes) != want:
        raise Refused("ticket-routes-mismatch", "the blob's routes are not the ticket's")
    if blob.model != ticket_facts.get("model_requested"):
        raise Refused("ticket-model-mismatch", "the blob's model is not the ticket's")
    if ticket_facts["kind"] != "verifier":
        return
    resolved = ticket_facts.get("verifier")
    if not isinstance(resolved, Mapping):
        raise Refused("ticket-facts-missing", "a verifier ticket without its resolved entry")
    steps = [{"argv": list(s.argv), "wall_s": s.wall_s} for s in blob.verifier_steps or ()]
    if canon.canonical_bytes(steps) != canon.canonical_bytes(ticket_facts.get("verifier_steps")):
        raise Refused("ticket-steps-mismatch", "the blob's verifier_steps are not the ticket's")
    if blob.driver != resolved.get("driver_template"):
        raise Refused("ticket-driver-mismatch", "the blob's driver is not the ticket's driver_template")


#: CONTRACT §3.5: a driver template's name (a verifier entry's ``driver_template``) and the most of one root reads
_DRIVER_TEMPLATE_RE = re.compile(r"[a-z0-9_]{1,48}")
DRIVER_TEMPLATE_MAX = 1024 * 1024


def read_driver_template(name: str) -> bytes | None:
    """The bytes of driver template ``name`` in the import tree root runs (CONTRACT §3.5: ``<import
    tree>/levain/verifier_drivers/<name>.py``, the tree this interpreter imports ``levain`` from), the directory and
    the file each opened ``O_NOFOLLOW``, the file regular and at most :data:`DRIVER_TEMPLATE_MAX`; None for a name
    outside the grammar (or ``_``-prefixed) and for no such regular file."""
    if type(name) is not str or not _DRIVER_TEMPLATE_RE.fullmatch(name) or name.startswith("_"):
        return None
    tree = os.path.join(os.path.dirname(os.path.abspath(levain.__file__)), "verifier_drivers")
    try:
        dfd = os.open(tree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        fd = os.open(name + ".py", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
    except OSError:
        return None
    finally:
        os.close(dfd)
    try:
        if (os.fstat(fd).st_mode & 0o170000) != 0o100000:
            return None
        out = b""
        while len(out) <= DRIVER_TEMPLATE_MAX:
            part = os.read(fd, DRIVER_TEMPLATE_MAX + 1 - len(out))
            if not part:
                return out
            out += part
        return None
    finally:
        os.close(fd)


def check_driver_template(resolved: Mapping[str, object], read=read_driver_template) -> None:
    """CONTRACT §3.5: render refuses ``driver-template-digest-mismatch`` unless the installed template the resolved
    verifier entry names hashes (SHA-256 of its bytes) to the entry's ``driver_template_digest``: a levain upgrade that
    changed a template stops every run of an entry signed for the old one until the operator re-signs it."""
    data = read(str(resolved.get("driver_template")))
    if data is None or "sha256:" + hashlib.sha256(data).hexdigest() != resolved.get("driver_template_digest"):
        raise Refused("driver-template-digest-mismatch", f"{str(resolved.get('driver_template'))[:48]}")


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
        "GIT_CONFIG_GLOBAL": f"{S}/root/gitconfig", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    if facts.term is not None:
        env["TERM"] = facts.term
    return env


def check_env_absent(vendor: str, names: Iterable[str]) -> None:
    """Refused ``env-forbidden`` when ``names`` holds one of :data:`ABSENT_ENV`'s for ``vendor``, so no later edit
    can put ``ANTHROPIC_API_KEY`` (and its blocking prompt) back in a Claude Code session."""
    bad = sorted(set(names) & ABSENT_ENV.get(vendor, frozenset()))
    if bad:
        raise Refused("env-forbidden", ",".join(bad))


def render_env(blob: RenderBlob, facts: RenderFacts) -> dict[str, dict[str, str]]:
    """§3.4 per role (:func:`_render_env`), each env checked against :data:`ABSENT_ENV`."""
    envs = _render_env(blob, facts)
    for env in envs.values():
        check_env_absent(facts.vendor, env)
    return envs


def _render_env(blob: RenderBlob, facts: RenderFacts) -> dict[str, dict[str, str]]:
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
    check_env_absent(facts.vendor, out)
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
    a repo or local layer setting it true would switch Levain's gate hooks off).

    WebFetch (wfetch DESIGN §3 WF-1, RULINGS Q1): an egress session gets ``skipWebFetchPreflight: true`` (the
    preflight goes to ``api.anthropic.com``, which the front never admits as a CONNECT host on a brokered session;
    one behaviour for both auths, and the front's host list is the gate) and one ``WebFetch(domain:<h>)`` allow
    per host of its ``egress`` routes, so an on-list fetch does not prompt; the front, not this rule, refuses an
    off-list host. Every session gets
    ``disableClaudeAiConnectors: true`` (wfetch runs/r1: connectors reach ``mcp-proxy.anthropic.com``, a host on
    no route)."""
    check_facts(blob, facts)
    sandbox: dict[str, Any] = {"enabled": facts.host == "linux", "excludedCommands": []}
    permissions: dict[str, Any] = {"disableBypassPermissionsMode": "disable"}
    if facts.host == "linux":
        sandbox.update({"failIfUnavailable": True, "bwrapPath": str(LEVAIN_BWRAP)})
        permissions["ask"] = [LINUX_ASK_RULE]
    out: dict[str, Any] = {"sandbox": sandbox, "permissions": permissions, "disableAllHooks": False,
                           "disableClaudeAiConnectors": True}
    if facts.slot.slot_class == "egress":
        out["skipWebFetchPreflight"] = True
        allow = [f"WebFetch(domain:{h})" for h in egress_hosts(blob, facts)]
        if allow:
            permissions["allow"] = allow
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
    ``[projects."<S>/repo"] trust_level = "untrusted"``: Codex then leaves out the repo's own ``.codex/`` layer
    (its words: "To load project-local config, hooks, and exec policies, update its trust setting"). Filed
    ``trusted``, the repo's ``.codex/config.toml`` ranked above this file and its ``sandbox_mode``,
    ``approval_policy`` and ``web_search`` were the effective ones (1026-2/wf1ag, Codex 0.162.1 ``config/read``).
    With no entry at all, a thread's sandbox under this file's Linux shape was ``readOnly``; ``untrusted`` gave
    ``workspaceWrite``, as ``trusted`` did. Whether the TUI asks a trust question for an ``untrusted`` entry: no run
    measured it.
    Brokered: the provider block runs/macos-RB 1e measured (env-only base URL did not work)."""
    check_facts(blob, facts)
    top: dict[str, Any] = {"approval_policy": "never", "cli_auth_credentials_store": "file",
                           "web_search": CODEX_WEB_SEARCH}
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
    tables[f"projects.{_toml_str(str(facts.S / 'repo'))}"] = {"trust_level": "untrusted"}
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


def profile_lines(facts: WallFacts) -> list[str]:
    """The SBPL lines of one wall, a pure function of its :class:`~levain.slot.wall.WallFacts` (s5 §5b, A7): the
    slot's profile and the export targets' profiles are this one builder called with different facts, never a
    fork of it. In order: ``(allow default)``; the read-denied subpaths; ``file-write*`` denied, then allowed for
    each writable group (one line per group) and the device literals; with ``devices_only``, reads and ioctls
    under ``/dev`` denied but for those literals; the network lines as given; the IPC denials; Mach denied but for
    the allow-list. A facts value with no network line, an empty writable group or a path off the SBPL-safe
    alphabet is refused: an omission here would leave ``(allow default)`` holding the operation."""
    if not facts.network or not facts.writable or not all(facts.writable):
        raise Refused("internal", "wall facts: no network rule or an empty writable group")
    if any(ch in facts.label for ch in "\n\r\x00"):     # the label is a comment line: a break would start a rule
        raise Refused("internal", "wall facts: a line break or NUL in the label")
    lines = ["(version 1)", f";; {facts.label}", "(allow default)"]
    if facts.read_denied:
        lines.append("(deny file-read* " + " ".join(f"(subpath {_q(p)})" for p in facts.read_denied) + ")")
    lines += ["(deny file-write*)", "(allow file-write*"]
    lines += ["  " + " ".join(f"(subpath {_q(p)})" for p in group) for group in facts.writable]
    lines.append("  " + " ".join(f"(literal {_q(d)})" for d in facts.devices) + ")")
    if facts.devices_only:
        lines += ['(deny file-read* file-ioctl (subpath "/dev"))',
                  "(allow file-read* file-ioctl " + " ".join(f"(literal {_q(d)})" for d in facts.devices) + ")"]
    lines += list(facts.network)
    if facts.ipc_denied:
        lines += ["(deny ipc-posix*)", "(deny ipc-sysv*)"]
    lines += ["(deny mach-lookup)", "(deny mach-register)"]
    lines += [f'(allow mach-lookup (global-name "{m}"))' for m in facts.mach_allow]
    return lines


def _profile_bytes(facts: WallFacts) -> bytes:
    text = "\n".join(profile_lines(facts)) + "\n"
    assert_no_syscall(text)
    return text.encode("utf-8")


def render_profile(blob: RenderBlob, facts: RenderFacts, *, probe: bool = False) -> bytes:
    """§8.1's classes as SBPL, every path the real ``/private/...`` path, in the forms runs/macos-RM2 and
    runs/macos-RM1b measured (``vm/rm2_profile.sb.in``, ``vm/rm1b_profile.sb.in``), through :func:`profile_lines`.

    ``probe=True`` is V13's probe profile ``<S>/root/probe.sb`` (DESIGN_V13_REV2 §5, head R1): the same lines with
    exactly ``(deny network-outbound)`` left out. Under ``(allow default)`` outbound is then allowed, so the probe
    child measures pf alone; the bind and inbound denials, the unix-socket allows and every file, IPC and Mach rule
    stay. The wall binds this file to the probe's connector argv only (``wall._check_spec``).

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
    network = ([] if probe else ["(deny network-outbound)"]) + ["(deny network-bind)", "(deny network-inbound)"]
    if facts.slot.slot_class == "egress":
        if own_loopback(blob, facts):
            network += ['(allow network-outbound (remote ip "localhost:*"))',
                        '(allow network-bind (local ip "localhost:*"))',
                        '(allow network-inbound (local ip "localhost:*"))']
        else:
            network.append(f'(allow network-outbound (remote ip "localhost:{facts.gwport}"))')
    sock = _q(f"{S}/sock")
    network += [f"(allow network-outbound (remote unix-socket (subpath {sock})))",
                f"(allow network-bind (local unix-socket (subpath {sock})))",
                f"(allow network-inbound (local unix-socket (subpath {sock})))"]
    R, n = str(facts.R), facts.slot.index
    if "levain_queue" in blob.hooks:
        network.append(f"(allow network-outbound (remote unix-socket (subpath {_q(f'{R}/qb/slot{n}')})))")
    if facts.kind == "planner":
        network.append(f"(allow network-outbound (remote unix-socket (subpath {_q(f'{R}/planner/slot{n}')})))")
    return _profile_bytes(WallFacts(
        label=f"Levain session profile (CONTRACT §8.1) for {facts.session} on {facts.slot.tag}, kind {facts.kind}.",
        writable=(tuple(f"{S}/{d}" for d in writable), tuple(facts.var_folders)),
        devices=DEVICE_LITERALS, network=tuple(network), mach_allow=MACH_ALLOW))


def render_importer_profile(host: HostOS) -> bytes:
    """``L/export/importer.sb`` (s5 §5b): the importer's facts (:func:`levain.slot.wall.export_wall_facts`)
    through :func:`profile_lines`, guarded like a slot's (:func:`assert_no_syscall` here, the stage's
    ``check_profile_text`` before it is applied). macOS only."""
    return _export_profile(host, "importer")


def render_verifier_profile(host: HostOS) -> bytes:
    """``L/export/verifier.sb`` (s5 §5b, §6.0): the importer's shape with ``.verify`` the one writable path."""
    return _export_profile(host, "verifier")


def _export_profile(host: HostOS, role: str) -> bytes:
    from levain.slot.wall import check_profile_text, export_wall_facts

    if host != "darwin":
        raise Refused("unsupported-host", f"{role}.sb is macOS only")
    data = _profile_bytes(export_wall_facts(host, role))
    check_profile_text(data.decode("utf-8"))
    return data


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
        if facts.slot.slot_class == "egress":      # V13: only an egress slot is probed
            files.append(RenderedFile("root/probe.sb", render_profile(blob, facts, probe=True), 0, rgid, fmode))
    if facts.kind in READSET_KINDS:
        # §2.1: root : root 0644 whatever the class, so the slot can read it and no slot group can change it.
        files.append(RenderedFile(READSET_ATTRIBUTES_PATH, READSET_ATTRIBUTES, 0, 0, 0o644))
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
                           "permissions.disableBypassPermissionsMode": "disable", "disableAllHooks": False,
                           "disableClaudeAiConnectors": True}
    if facts.host == "linux":
        exp.update({"sandbox.failIfUnavailable": True, "sandbox.bwrapPath": str(LEVAIN_BWRAP),
                    "permissions.ask": Contains((LINUX_ASK_RULE,))})
    if facts.slot.slot_class == "egress":
        exp["skipWebFetchPreflight"] = True
        allow = tuple(f"WebFetch(domain:{h})" for h in egress_hosts(blob, facts))
        if allow:
            exp["permissions.allow"] = Contains(allow)
    return exp


def expected_effective_codex(blob: RenderBlob, facts: RenderFacts) -> dict[str, Any]:
    cfg = codex_config(blob, facts)["top"]
    return {k: cfg[k] for k in ("approval_policy", "cli_auth_credentials_store", "sandbox_mode", "web_search")
            if k in cfg}


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
    JSON. Refused by key: every ``sandbox`` key in host_guard's ``_LOOSENING_TRUE`` that is ``true`` or not a bool,
    and every one in ``_LOOSENING_NONEMPTY`` that is non-empty or not a list of strings (the keys whose merge across
    layers the flag layer cannot pin; a ``sandbox.network`` or ``sandbox.filesystem`` that is not an object is
    ``repo-settings-malformed``),
    and a ``disableAllHooks`` that is anything but ``false`` (L2 M6; DECISION: a non-boolean is refused too, as
    its meaning to CC is unmeasured), and an ``env`` naming the queue's vars or a :data:`PROXY_ENV` name. A
    document that is not an object is refused ``repo-settings-malformed``.
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
        env = doc.get("env", {})
        if not isinstance(env, Mapping):
            out.append(Refused("repo-settings-malformed", f"{path}: env"))
        else:
            # The queue's env reaches the agent through the process env only (§3.2); the consent hook denies every
            # gated call when a settings layer names one, so the session is refused here, by name, instead.
            for name in sorted(n for n in env if n in (QUEUE_CAP_ENV, QUEUE_SOCKET_ENV)):
                out.append(_refused("loosening", f"env.{name}", f"{path}: the queue's env is set by levain only"))
            for name in sorted(n for n in env if n in PROXY_ENV):
                out.append(_refused("loosening", f"env.{name}", f"{path}: {name} is set by levain only"))
        sb = doc.get("sandbox", {})
        if not isinstance(sb, Mapping):
            out.append(Refused("repo-settings-malformed", f"{path}: sandbox"))
            continue
        bad_sub = sorted(s for s in ("network", "filesystem") if s in sb and not isinstance(sb[s], Mapping))
        for sub in bad_sub:
            out.append(Refused("repo-settings-malformed", f"{path}: sandbox.{sub}"))
        if bad_sub:
            continue
        # Exact type first (RULINGS 1026-10 Q5, host_guard's managed check's twin): a falsy non-list passed the
        # not-empty check, and a schema type error can drop CC's sandbox block silently. The type is membership:
        # a _LOOSENING_TRUE key is a bool, a _LOOSENING_NONEMPTY key a list of strings.
        for keys in _LOOSENING_TRUE:
            v = _get(sb, keys)
            if v is _MISSING:
                continue
            if type(v) is not bool:
                out.append(_refused("loosening", "sandbox." + ".".join(keys),
                                    f"{path}: must be bool, got {type(v).__name__}"))
            elif v:
                out.append(_refused("loosening", "sandbox." + ".".join(keys), f"{path}: true"))
        for keys in _LOOSENING_NONEMPTY:
            v = _get(sb, keys)
            if v is _MISSING:
                continue
            if type(v) is not list or any(type(i) is not str for i in v):
                out.append(_refused("loosening", "sandbox." + ".".join(keys),
                                    f"{path}: must be a list of strings, got {type(v).__name__}"))
            elif v:
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
    "VENDOR_PREFIX", "READSET_ATTRIBUTES", "READSET_ATTRIBUTES_PATH", "READSET_KINDS", "READSET_TEMPLATE",
    "HookTemplate", "RenderFacts", "RenderedFile", "Rendered", "Contains", "DISCOVERY",
    "REPO_DISCOVERY", "wire_name", "gwport_for", "own_loopback", "check_facts", "render_env", "secret_env_names",
    "TOKEN_ENV", "ABSENT_ENV", "check_env_absent", "PROXY_ENV", "CODEX_WEB_SEARCH", "CODEX_LAUNCH_ARGS", "egress_hosts",
    "env_text", "render_cc_settings", "render_cc_onboarding", "codex_config", "codex_toml", "gitconfig_text",
    "assert_no_syscall", "check_driver_template", "profile_lines", "read_driver_template", "render_profile",
    "render_importer_profile", "render_verifier_profile",
    "render", "render_from_stdin", "expected_effective_cc",
    "expected_effective_codex", "readback_refusals", "refuse_readback", "repo_loosening_refusals",
    "refuse_repo_loosening", "discovery_refusals", "refuse_discovery", "repo_discovery_names", "parse_driver_input",
]
