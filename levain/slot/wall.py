"""The per-OS second wall a session's processes start behind (CONTRACT §8, §9; plan §2d 1-3, §2e 1-2, §2e 7,
§2f 8).

Two halves, one module:

* the PLAN half (pure, runs in the root helper): :func:`launch_spec` decides who a target runs as, where, with
  which env file and profile; :func:`macos_launch_argv` and :func:`linux_launch_argv` wrap it in the per-OS
  launch path; :func:`macos_egress_ready` is ``start``'s pf + alias check; :func:`check_profile_text` is the
  apply-time ``syscall`` guard.
* the STAGE half (:func:`stage_main`, this module run as ``__main__`` by the root interpreter): the last hop
  before the target. It reads the spec from a pipe (never argv: the spec carries the session's handles,
  §3.2), drops to the target's principal and PROVES the drop (:func:`drop_and_assert`), reads the rendered
  env file, guards the profile, changes to the target's cwd, closes every fd off the allowlist and execs.
  Any failure = the stage exits :data:`STAGE_EXIT` and nothing is exec'd.

macOS (§8.2): ``launchctl asuser <uid>`` -> the stage (drop + assert -> ``RLIMIT_NPROC`` -> profile guard)
-> ``sandbox-exec -f <S>/root/profile.sb <target>``, the R-M2 method (``vm/rm2_launch.sh``): sandbox-exec
compiles and applies the profile, then execs the target, so a profile that fails to apply never reaches the
target [not run: R-M2 never fed it a failing profile; a 2g row]. The vendor's target is the TRAMPOLINE
(:func:`trampoline_main`), which runs under the applied profile, writes the exec marker and execs the vendor.
The front (§8.2 last sentence): the stage alone, no ``asuser``, no profile.

Linux (§9): the forked root child joins the session's cgroup, then ``nsenter --net=/run/netns/levain-{e,v}<n>``
-> Levain's bwrap copy, run as root with ``--die-with-parent``, builds the §9.2 allow-listed root and the
per-session mount, IPC, PID and UTS namespaces -> the stage (drop + assert + ``no_new_privs``) -> the target.
That is R-L1's measured view (``vm/n3.sh``:
ro ``/usr`` ``/etc``, ``bin``/``sbin``/``lib`` links into ``/usr``, a fresh ``/proc``, rbind ``/dev`` with a
fresh ``/dev/shm``, tmpfs, the session dir and the socket dirs) built by bwrap instead of hand mounts.

Targets: ``vendor``, ``clone`` (§5.4 step 3: by the SAME path as the vendor), ``clone-check`` (the post-clone
check's ``git`` children, §5.4 step 3 (b), by that same path), ``step`` (a verifier step, §5.6 step 2), ``driver``
(§5.6 step 3), ``forwarder`` (Linux egress, §9.1), ``front`` (§5.1 render (4)), ``probe`` (V13: the firewall
self-probe's connector, an egress slot's, under ``<S>/root/probe.sb`` on macOS) and ``identity`` (§7.6, IG-3: the
Claude identity call's child, an egress slot's, under the session's own ``profile.sb``). The export targets
``importer`` and ``verifier`` (s5 §5b) run as their own singletons (:func:`export_principal`), ROOT-launched
through the same stage by their own spec builder (:func:`export_launch_spec`) and walls (:class:`WallFacts`).

⛔ Nothing here is ever run on the host by a test: every OS act is a seam (:class:`OsCalls`) and every
argv is asserted as text. No ``syscall`` operation may reach a profile (trg-069, §8).
"""
from __future__ import annotations

import errno
import os
import re
import resource
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple

from levain.slot import Refused

#: The format of the stage's spec (canonical JSON on the spec pipe). Internal to the helper and its own
#: stage, never a file or a wire another principal reads.
SPEC_FORMAT = "levain-wall-spec/1"

#: CONTRACT §11 V3: ``RLIMIT_NPROC`` per slot, "macOS min with kern.maxprocperuid".
NPROC = 2048

#: DECISION: the stage's exit status for every refusal; outside the shell's 126/127 and a signal's 128+n.
STAGE_EXIT = 121

#: DECISION: the fd the spec pipe's read end sits on in the stage. Closed before the target's exec.
SPEC_FD = 3
#: DECISION: the fd ``start``'s exec-marker pipe (§5.1 start) sits on through the launch hops. The LAST hop before
#: the vendor (Linux: the stage; macOS: the trampoline, under the applied profile) marks it close-on-exec, writes
#: :data:`EXEC_OK` and execs the vendor; a refusal anywhere, or an execve that fails after the marker, writes
#: :data:`EXEC_REFUSED`. ``start`` takes the exec as done only on :data:`EXEC_OK` followed by EOF (the last
#: close-on-exec copy closes AT the vendor's exec); EOF with no byte = the start failed. That EOF arrives through
#: bwrap's monitor and pid-1 [measured: runs/s2-linux-r3 row L27, bubblewrap 0.9.0, kernel 7.2.6, ACTIVE in 0.4 s,
#: 2026-10-20] and through ``launchctl asuser`` + the trampoline [measured: runs/s2-macos RESULT2, 2026-10-20].
#: Never one of the vendor's fds.
EXEC_FD = 4
EXEC_OK = b"+"
EXEC_REFUSED = b"!"
#: The forwarder's readiness pipe: spawn_target puts it on the same fd as the exec pipe; the forwarder keeps it
#: and writes :data:`EXEC_OK` once its socket is bound and listening (EOF or :data:`EXEC_REFUSED` = not ready).
READY_FD = EXEC_FD
#: V13 (DESIGN_V13_REV2 §5): the probe child's result pipe, on the same fd as the exec / ready pipe (spawn_target
#: puts its write end there); the connector module it runs (stdlib only, head ruling N3) and the profile it runs under
#: on macOS, ``<S>/root/probe.sb`` (render's ``render_profile(..., probe=True)``). :func:`_check_spec` binds that
#: profile to exactly this module's argv, in both directions.
PROBE_RESULT_FD = EXEC_FD
PROBE_MODULE = "levain.slot.probe_child"
PROBE_PROFILE = "probe.sb"
#: §7.6 (1027-18 RULINGS IG-3, G1b): the Claude identity call's slot side, run from the root tree as the slot with
#: the session's env file (its ``HTTPS_PROXY`` and ``CLAUDE_CONFIG_DIR``); its stdout is root's to drain (the runtime
#: helper's capture). :func:`_check_identity_spec` binds the target to exactly this module's argv.
IDENTITY_MODULE = "levain.slot.identity_child"
#: DECISION: the front's fixed argv from root (lane F3 b): at most this many strings of at most 4 KiB, no NUL.
_FRONT_ARGS = 16

SPEC_CAP = 64 * 1024
ENV_CAP = 64 * 1024
PROFILE_CAP = 1024 * 1024

TARGETS = ("vendor", "clone", "clone-check", "step", "driver", "forwarder", "front", "probe", "identity")
#: s5 §5b: the export wall's targets, each ROOT-launched by :func:`export_launch_spec`, never by
#: :func:`launch_spec` (which is slot-shaped). The stage runs either spec with the same machinery.
EXPORT_TARGETS = ("importer", "verifier")
#: §3.1 step 4: a slot child holds 0, 1, 2 + its pty; the front 0, 1, 2 + its GO pipe (§5.1 render (4)); the
#: forwarder 0, 1, 2 + its readiness pipe; an export target 0, 1, 2 + its result pipe (s5 §5b); the V13 probe
#: 0, 1, 2 + its result pipe.
_ONE_EXTRA_FD = frozenset({"vendor", "front", "forwarder", "probe"}) | frozenset(EXPORT_TARGETS)
#: Only these targets carry handles or a TERM (§3.2: start adds token + cap at the VENDOR's exec).
_EXTRA_ENV = frozenset({"vendor", "front"})

LAUNCHCTL = "/bin/launchctl"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
GIT = "/usr/bin/git"
#: DECISION: fixed paths, not a PATH search, so the argv is the same on every host that builds it.
NSENTER = "/usr/bin/nsenter"
CGROUP_ROOT = "/sys/fs/cgroup/levain.slice"     # CONTRACT §7.3
NETNS_DIR = "/run/netns"
FORWARDER_PORT = 47100                          # CONTRACT §3.4, §9.1: the forwarder's port inside the netns

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MODULE = re.compile(r"levain(\.[a-z_][a-z0-9_]*)+")

_STEP_ARGS, _STEP_ARG_BYTES = 64, 4096          # CONTRACT §5.6: argv <= 64 strings of <= 4 KiB, no NUL


# --- principals ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    """Who a target becomes. ``gid == uid`` (§1.1); ``groups`` is EXACTLY the account's own group plus the
    one shared group its row grants (§1.2): ``levain-slots`` for a slot account, ``levain-gwfronts`` for a
    front. Never another slot's group: ``<S>`` is ``root : _levain_s<n> 0750``, so a second slot group is
    the listing of a second session (T:2/test_other_slot_cannot_list_session)."""

    name: str
    uid: int
    gid: int
    groups: Tuple[int, ...]


def principal(host: str, name: str, ids: Mapping[str, int], shared: Mapping[str, int]) -> Principal:
    from levain.slot.ids import RANGES, SINGLETONS, in_own_range

    uid = ids.get(name)
    if name in SINGLETONS or not in_own_range(host, name, uid):
        raise Refused("bad-token", f"{name}: uid {uid!r} is not a session account in its §1.1 range")
    assert isinstance(uid, int)
    grp = "levain-gwfronts" if name.startswith("_levain_gw") else "levain-slots"
    sg = shared.get(grp)
    if not isinstance(sg, int) or isinstance(sg, bool) or sg not in RANGES[host].shared:
        raise Refused("bad-token", f"{grp}: gid {sg!r} is not in the §1.1 shared range")
    return Principal(name, uid, uid, (uid, sg))


#: s5 §4.9 (A16, S2): the export roles' singleton accounts, the group each runs with, and the entry each execs
#: from the helper dir (names from s5 §5b). The importer's group follows the export class it imports for.
EXPORT_ACCOUNT = {"importer": "_levain_export", "verifier": "_levain_verify"}
EXPORT_GROUPS = {("importer", "operator"): "levain-export", ("importer", "dispatch"): "levain-dispatch-export",
                 ("verifier", None): "levain-verify"}
EXPORT_ENTRY = {"importer": "export-importer", "verifier": "export-verifier"}


def export_principal(host: str, role: str, ids: Mapping[str, int], shared: Mapping[str, int], *,
                     export_class: Optional[str] = None) -> Principal:
    """Who an export target becomes (s5 §4.9, A16, S2), on its OWN path: :func:`principal` keeps refusing every
    singleton for a slot target. ``importer``: ``_levain_export`` with ``levain-export`` (``export_class``
    ``operator``) or ``levain-dispatch-export`` (``dispatch``); ``verifier``: ``_levain_verify`` with
    ``levain-verify`` (no class). Never ``levain-slots``, never the other role's group.

    Refused ``export-account-missing`` when the account has no uid in ``ids``; ``export-account-bad`` when its
    uid is not an int in §1.1's singleton range (a slot-range uid among them) or is any other account's in
    ``ids``, or its group's gid is missing, outside the shared range, the account's own uid or any other group's
    in ``shared``."""
    from levain.slot.ids import RANGES

    if host not in RANGES:
        raise Refused("unsupported-host", repr(host))
    if (role, export_class) not in EXPORT_GROUPS:
        raise Refused("bad-token", f"export role {role!r} class {export_class!r}")
    name = EXPORT_ACCOUNT[role]
    if name not in ids:
        raise Refused("export-account-missing", name)
    uid = ids[name]
    r = RANGES[host]
    if not _is_int(uid) or uid in r.slots or uid not in r.singletons:
        raise Refused("export-account-bad", f"{name}: uid {uid!r} is not in the §1.1 singleton range")
    # Every other account in the map, the other export role and every other singleton among them: sharing a uid
    # would run this target as that service.
    owner = next((n for n, v in ids.items() if n != name and v == uid), None)
    if owner is not None:
        raise Refused("export-account-bad", f"{name}: uid {uid} is {owner}'s")
    grp = EXPORT_GROUPS[(role, export_class)]
    sg = shared.get(grp)
    if not _is_int(sg) or sg not in r.shared:
        raise Refused("export-account-bad", f"{grp}: gid {sg!r} is not in the §1.1 shared range")
    # Every other group in the map (a privileged one included), and the account's own group (gid == uid).
    if sg == uid or any(g != grp and v == sg for g, v in shared.items()):
        raise Refused("export-account-bad", f"{grp}: gid {sg} is another group's")
    return Principal(name, uid, uid, (uid, sg))


def target_account(slot, target: str) -> str:
    """The account a target runs as (§1, §5.4, §5.6): an egress slot's vendor, clone, forwarder, V13 probe and
    identity call run as ``_levain_s<n>``, its front as ``_levain_gw<n>``; a verifier pair's clone and steps run as the CHILD
    ``_levain_c<n>``, its driver as ``_levain_v<n>``. Any other pairing is refused."""
    first, second = slot.accounts
    if slot.slot_class == "egress":
        table = {"vendor": first, "clone": first, "clone-check": first, "forwarder": first, "front": second,
                 "probe": first, "identity": first}
    else:
        table = {"clone": second, "clone-check": second, "step": second, "driver": first}
    if target not in table:
        raise Refused("bad-token", f"target {target!r} does not run in a {slot.slot_class} slot")
    return table[target]


# --- the spec ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """What the stage does, decided by the plan half. ``env_file``: the rendered env (§3.4), parsed by the
    stage; ``extra_env``: the handles + TERM ``start`` adds at exec (§3.2), never in argv. ``profile``: macOS
    slot targets only. ``resolve``: look ``argv[0]`` up on the env's PATH as the target (verifier steps)."""

    target: str
    uid: int
    gid: int
    groups: Tuple[int, ...]
    nproc: Optional[int]
    env_file: Optional[str]
    extra_env: Tuple[Tuple[str, str], ...]
    profile: Optional[str]
    cwd: str
    argv: Tuple[str, ...]
    resolve: bool
    keep_fds: Tuple[int, ...]
    exec_fd: Optional[int] = None

    def to_json(self) -> bytes:
        from levain.contracts import canon

        return canon.canonical_bytes({
            "format": SPEC_FORMAT, "target": self.target, "uid": self.uid, "gid": self.gid,
            "groups": list(self.groups), "nproc": self.nproc, "env_file": self.env_file,
            "extra_env": dict(self.extra_env), "profile": self.profile, "cwd": self.cwd,
            "argv": list(self.argv), "resolve": self.resolve, "keep_fds": list(self.keep_fds),
            "exec_fd": self.exec_fd})


_SPEC_KEYS = frozenset({"format", "target", "uid", "gid", "groups", "nproc", "env_file", "extra_env", "profile",
                        "cwd", "argv", "resolve", "keep_fds", "exec_fd"})


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _abs_path(v: object) -> bool:
    return (isinstance(v, str) and v.startswith("/") and "\x00" not in v
            and str(PurePosixPath(v)) == v and ".." not in PurePosixPath(v).parts)


def _check_spec(s: Spec) -> None:
    """The shape every spec must have, checked by the plan half when it builds one AND by the stage when it
    reads one (the stage trusts its root parent, but never runs a spec it would not have built)."""
    if s.target not in TARGETS and s.target not in EXPORT_TARGETS:
        raise Refused("spec-malformed", f"target {s.target!r}")
    if not (_is_int(s.uid) and _is_int(s.gid) and s.uid > 0 and s.gid > 0):
        raise Refused("drop-to-root", f"uid {s.uid!r} gid {s.gid!r}")
    if not s.groups or not all(_is_int(g) and g > 0 for g in s.groups) or len(set(s.groups)) != len(s.groups) \
            or s.gid not in s.groups:
        raise Refused("spec-malformed", f"groups {list(s.groups)!r}")
    if s.nproc is not None and not (_is_int(s.nproc) and s.nproc > 0):
        raise Refused("spec-malformed", f"nproc {s.nproc!r}")
    for p in (s.env_file, s.profile):
        if p is not None and not _abs_path(p):
            raise Refused("spec-malformed", f"path {p!r}")
    if not _abs_path(s.cwd):
        raise Refused("spec-malformed", f"cwd {s.cwd!r}")
    if not s.argv or not all(isinstance(a, str) and "\x00" not in a for a in s.argv) or not s.argv[0]:
        raise Refused("spec-malformed", "argv")
    if not s.resolve and not _abs_path(s.argv[0]):
        raise Refused("target-off-list", f"argv[0] {s.argv[0]!r} is not an absolute path")
    for k, v in s.extra_env:
        if not (isinstance(k, str) and _ENV_KEY.fullmatch(k) and isinstance(v, str) and "\x00" not in v):
            raise Refused("spec-malformed", f"extra env key {k!r}")
    if len({k for k, _ in s.extra_env}) != len(s.extra_env):
        raise Refused("spec-malformed", "extra env key repeated")
    if s.extra_env and s.target not in _EXTRA_ENV:
        raise Refused("spec-malformed", f"{s.target} takes no extra env")
    if not all(_is_int(f) and f > 2 and f != SPEC_FD for f in s.keep_fds) or len(set(s.keep_fds)) != len(s.keep_fds):
        raise Refused("spec-malformed", f"keep_fds {list(s.keep_fds)!r}")
    if len(s.keep_fds) > (1 if s.target in _ONE_EXTRA_FD else 0):
        raise Refused("fd-off-list", f"{s.target} keeps {list(s.keep_fds)!r}")
    if s.exec_fd is not None and (s.exec_fd != EXEC_FD or s.target != "vendor" or s.exec_fd in s.keep_fds):
        raise Refused("spec-malformed", f"exec_fd {s.exec_fd!r}: only the vendor's, on {EXEC_FD}, never kept")
    if s.target == "probe":
        _check_probe_spec(s)
    elif s.profile is not None and PurePosixPath(s.profile).name == PROBE_PROFILE:
        raise Refused("spec-malformed", f"{s.target} under the probe profile")
    if s.target == "identity":
        _check_identity_spec(s)


def _check_probe_spec(s: Spec) -> None:
    """V13 (head R3, codex MED "a probe-labelled spec can run something else"): a ``probe`` spec is EXACTLY the one
    :func:`launch_spec` builds: the root-tree module launcher running :data:`PROBE_MODULE` with
    ``--result-fd`` :data:`PROBE_RESULT_FD` and 1 to ``probe_child.MAX_ADDRS`` distinct addresses, each already in
    :func:`probe_child.probe_address`'s spelling; cwd ``<S>/tmp``; no env file, extra env, resolve or exec fd; the
    one kept fd; the slot's NPROC; on macOS (the launcher's host) ``<S>/root/probe.sb``, on Linux no profile. Run by
    the plan half and by the stage (:func:`parse_spec`), so the stage never runs a probe-labelled spec root would
    not have built."""
    from levain.slot.grammar import SESSION_RE
    from levain.slot.layout import STORE
    from levain.slot.probe_child import MAX_ADDRS, probe_address

    if s.env_file is not None or s.extra_env or s.resolve or s.exec_fd is not None \
            or s.keep_fds != (PROBE_RESULT_FD,) or s.nproc != NPROC:
        raise Refused("spec-malformed", "probe: env, resolve, fds or nproc")
    cwd = PurePosixPath(s.cwd)
    S = cwd.parent
    host = None
    for h in ("darwin", "linux"):
        prefix = module_argv(h, PROBE_MODULE, "--result-fd", str(PROBE_RESULT_FD))
        if s.argv[:len(prefix)] == prefix and S.parent == PurePosixPath(str(STORE[h])) / "live":
            host, addrs = h, s.argv[len(prefix):]
            break
    if host is None or cwd.name != "tmp" or not SESSION_RE.fullmatch(S.name):
        raise Refused("spec-malformed", "probe: argv or cwd is not the connector's")
    if not 0 < len(addrs) <= MAX_ADDRS or len(set(addrs)) != len(addrs) \
            or any(probe_address(a) != a for a in addrs):
        raise Refused("spec-malformed", "probe: addresses")
    want = str(S / "root" / PROBE_PROFILE) if host == "darwin" else None
    if s.profile != want:
        raise Refused("spec-malformed", f"probe: profile {s.profile!r} is not {want!r}")


def _check_identity_spec(s: Spec) -> None:
    """G1b: an ``identity`` spec is EXACTLY the one :func:`launch_spec` builds: the root-tree module launcher running
    :data:`IDENTITY_MODULE` with no argument; cwd ``<S>/tmp``; the session's env file ``<S>/root/env``; no extra env,
    resolve, exec fd or kept fd; the slot's NPROC; on macOS (the launcher's host) ``<S>/root/profile.sb``, on Linux no
    profile. Run by the plan half and by the stage (:func:`parse_spec`), so the stage never runs an
    identity-labelled spec root would not have built."""
    from levain.slot.grammar import SESSION_RE
    from levain.slot.layout import STORE

    if s.extra_env or s.resolve or s.exec_fd is not None or s.keep_fds or s.nproc != NPROC:
        raise Refused("spec-malformed", "identity: env, resolve, fds or nproc")
    cwd = PurePosixPath(s.cwd)
    S = cwd.parent
    host = next((h for h in ("darwin", "linux") if s.argv == module_argv(h, IDENTITY_MODULE)
                 and S.parent == PurePosixPath(str(STORE[h])) / "live"), None)
    if host is None or cwd.name != "tmp" or not SESSION_RE.fullmatch(S.name):
        raise Refused("spec-malformed", "identity: argv or cwd is not the identity child's")
    if s.env_file != str(S / "root" / "env"):
        raise Refused("spec-malformed", f"identity: env file {s.env_file!r}")
    want = str(S / "root" / "profile.sb") if host == "darwin" else None
    if s.profile != want:
        raise Refused("spec-malformed", f"identity: profile {s.profile!r} is not {want!r}")


def parse_spec(data: bytes) -> Spec:
    from levain.contracts import ContractError, canon

    if len(data) > SPEC_CAP:
        raise Refused("spec-malformed", "over the cap")
    try:
        obj = canon.parse(data)
    except (ContractError, ValueError) as e:
        raise Refused("spec-malformed", str(e)) from None
    if not isinstance(obj, dict) or set(obj) != _SPEC_KEYS or obj["format"] != SPEC_FORMAT \
            or canon.canonical_bytes(obj) != data:
        raise Refused("spec-malformed", "keys, format or bytes")
    if not isinstance(obj["groups"], list) or not isinstance(obj["argv"], list) \
            or not isinstance(obj["keep_fds"], list) or not isinstance(obj["extra_env"], dict) \
            or not isinstance(obj["resolve"], bool) or not isinstance(obj["target"], str):
        raise Refused("spec-malformed", "types")
    s = Spec(obj["target"], obj["uid"], obj["gid"], tuple(obj["groups"]), obj["nproc"], obj["env_file"],
             tuple(sorted(obj["extra_env"].items())), obj["profile"], obj["cwd"], tuple(obj["argv"]),
             obj["resolve"], tuple(obj["keep_fds"]), obj["exec_fd"])
    _check_spec(s)
    return s


def _session_dir(host: str, session: str) -> PurePosixPath:
    from levain.slot.grammar import SESSION_RE
    from levain.slot.layout import STORE

    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise Refused("bad-token", f"session {session!r}")
    return PurePosixPath(str(STORE[host])) / "live" / session


def _under(path: str, root: PurePosixPath) -> bool:
    return _abs_path(path) and PurePosixPath(path).parent == root


def module_argv(host: str, module: str, *args: str) -> Tuple[str, ...]:
    """Run a Levain module from the root-owned import tree, the way the entry scripts do (§23): the
    interpreter isolated (``-IS``), the tree the ONE path added, the module run as ``__main__``."""
    from levain.slot.root_tree import PY_FLAGS, ROOT_PYTHON, boot_path_code

    if not _MODULE.fullmatch(module):
        raise Refused("bad-token", f"module {module!r}")
    # The entry scripts' own boot line (row 33: one boot text), then the module as __main__.
    boot = (f"{boot_path_code(host)}; import runpy; m = sys.argv.pop(1); "
            "runpy.run_module(m, run_name='__main__', alter_sys=True)")
    return (ROOT_PYTHON[host], PY_FLAGS, "-c", boot, module) + tuple(args)


#: §5.4 step 3 (V4): every clone and check argv runs no hook and no automatic gc or maintenance, so no git
#: process outlives the one root waits for.
_GIT_PREFIX = (GIT, "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-c", "maintenance.auto=false")
#: §5.4 step 3 (b): every ``hash-object`` batch's argv up to its first path. A batch is bounded by its spec's
#: serialised size (:data:`SPEC_CAP`, which the stage enforces), never by a separate argv cap.
HASH_OBJECT_PREFIX = _GIT_PREFIX + ("hash-object", "--no-filters", "--")


def clone_argv(session_dir: PurePosixPath, kind: str) -> Tuple[str, ...]:
    """§5.4 step 3: the slot (verifier: the child) clones its repo from the bundle into the empty dir root
    made. Alloc kind ``dispatch|planner|verifier`` clones with ``--template=<S>/root/readset-template`` (render's,
    whose one ``info/attributes`` line turns every checkout conversion off); ``interactive|login`` with
    ``--template=`` (no template). A kind outside the five is refused, never defaulted to either form.
    ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_ATTR_NOSYSTEM=1`` and the rendered global config come from the env file
    (§3.4)."""
    from levain.slot.render import READSET_KINDS, READSET_TEMPLATE
    from levain.slot.runtime_grammar import KINDS

    if not isinstance(kind, str) or kind not in KINDS:
        raise Refused("bad-token", f"kind {kind!r}")
    template = str(session_dir / READSET_TEMPLATE) if kind in READSET_KINDS else ""
    return _GIT_PREFIX + ("clone", f"--template={template}", str(session_dir / "in" / "repo.bundle"),
                          str(session_dir / "repo"))


def ls_files_argv() -> Tuple[str, ...]:
    """§5.4 step 3 (b): the index as NUL-separated ``<mode> SP <sha> SP <stage> TAB <path>`` records, which root
    parses itself (no git path quoting is read)."""
    return _GIT_PREFIX + ("ls-files", "-s", "-z")


def hash_object_argv(paths: Sequence[str]) -> Tuple[str, ...]:
    """§5.4 step 3 (b): the worktree bytes of ``paths`` hashed with no attribute applied, the paths as argv after
    ``--`` (no ``--stdin-paths``, so no quoting protocol). At least one path, no NUL; the batch's size is its
    spec's, bounded by :data:`SPEC_CAP` (:func:`launch_spec` refuses a spec past it)."""
    if not paths or not all(isinstance(p, str) and p and "\x00" not in p for p in paths):
        raise Refused("spec-malformed", f"hash-object batch of {len(paths)} paths")
    return HASH_OBJECT_PREFIX + tuple(paths)


def _check_clone_check_argv(argv: Sequence[str]) -> None:
    """The ``clone-check`` target runs exactly :func:`ls_files_argv` or a :func:`hash_object_argv` batch."""
    n = len(HASH_OBJECT_PREFIX)
    if tuple(argv) == ls_files_argv():
        return
    if tuple(argv[:n]) == HASH_OBJECT_PREFIX:
        hash_object_argv(tuple(argv[n:]))
        return
    raise Refused("target-off-list", "clone-check runs git ls-files -s -z or git hash-object --no-filters -- only")


def _check_step_argv(argv: Sequence[str]) -> None:
    if not 0 < len(argv) <= _STEP_ARGS or not all(
            isinstance(a, str) and "\x00" not in a and len(a.encode("utf-8", "surrogatepass")) <= _STEP_ARG_BYTES
            for a in argv) or not argv[0]:
        raise Refused("spec-malformed", "verifier step argv (§5.6: <= 64 strings of <= 4 KiB, no NUL)")


def launch_spec(host: str, target: str, *, slot, session: str, ids: Mapping[str, int], shared: Mapping[str, int],
                argv: Sequence[str] = (), env_file: Optional[str] = None, profile: Optional[str] = None,
                extra_env: Optional[Mapping[str, str]] = None, keep_fds: Iterable[int] = (),
                front_module: Optional[str] = None, exec_fd: Optional[int] = None,
                kind: Optional[str] = None, probe_addrs: Sequence[str] = ()) -> Spec:
    """The stage's spec for ``target`` of ``slot``'s ``session``. Every path is derived from the session id
    (§5: no argv token or stdin field is a path); ``env_file`` / ``profile`` may only name a file in the
    rendered layer ``<S>/root/`` (lane B renders them; this module takes their paths, never their text).

    * ``vendor``: ``argv[0]`` under the vendor prefix (§2.1), cwd ``<S>/repo`` (claude fails in an
      unreadable cwd, 1c C15), one extra fd (its pty), the handles in ``extra_env``.
    * ``clone``: :func:`clone_argv` for the alloc ``kind`` (required: never defaulted), cwd ``<S>/repo`` (root
      made it empty, slot-owned, §5.4 step 3).
    * ``clone-check``: the post-clone check's child (§5.4 step 3 (b)), the clone's principal, cwd and env file;
      ``argv`` is exactly :func:`ls_files_argv` or one :func:`hash_object_argv` batch. Its stdout is root's to
      drain (the runtime helper's capture), never the helper's own.
    * ``step``: the ticket's argv, resolved on the env's PATH as the child, cwd ``<S>/repo`` (§5.6 step 2).
    * ``driver``: a template argv (the root interpreter or the import tree), cwd ``<S>/driver``; ``<S>/repo``
      is child 0700, unreadable to it by design (§5.6 step 3).
    * ``forwarder`` (Linux): Levain's TCP->unix relay, cwd ``<S>/tmp``, keeping :data:`READY_FD` (its
      readiness pipe, ``--ready-fd``); ``keep_fds`` is built here, never the caller's.
    * ``probe`` (V13, egress): :data:`PROBE_MODULE` from the root tree with ``--result-fd`` and ``probe_addrs``, cwd
      ``<S>/tmp``, keeping :data:`PROBE_RESULT_FD`; on macOS the profile is ALWAYS ``<S>/root/probe.sb`` (any other
      is refused); no env file. ``argv`` and ``keep_fds`` are built here, never the caller's.
    * ``identity`` (§7.6, IG-3, egress): :data:`IDENTITY_MODULE` from the root tree, no argument, cwd ``<S>/tmp``,
      the session's env file ``<S>/root/env`` (any other is refused), no kept fd; on macOS the session's
      ``<S>/root/profile.sb`` (any other is refused). ``argv`` is built here, never the caller's. Its stdout is
      root's to drain (the runtime helper's capture).
    * ``front``: drop + assert only (§8.2 last sentence): no profile, no ``RLIMIT_NPROC`` (V3 is per SLOT),
      no env file; its env is ``extra_env`` alone, cwd ``/``, one extra fd (the GO pipe). ``argv`` is the front
      module's FIXED arguments, root's (never slot-supplied), appended after the module.

    ``exec_fd`` (vendor only): :data:`EXEC_FD`, ``start``'s exec-happened pipe (see the constant).
    """
    from levain.slot.layout import IMPORT_TREE, RUN
    from levain.slot.root_tree import ROOT_PYTHON, VENDOR_PREFIX

    if host not in ("darwin", "linux"):
        raise Refused("unsupported-host", repr(host))
    if target not in TARGETS:
        raise Refused("bad-token", f"target {target!r}")
    who = principal(host, target_account(slot, target), ids, shared)
    S = _session_dir(host, session)
    root_layer = S / "root"
    argv = tuple(argv)
    resolve = False
    if tuple(probe_addrs) and target != "probe":
        raise Refused("spec-malformed", f"{target} takes no probe addresses")
    if target == "vendor":
        prefix = PurePosixPath(str(VENDOR_PREFIX[host]))
        if not argv or not _abs_path(argv[0]) or prefix not in PurePosixPath(argv[0]).parents:
            raise Refused("target-off-list", f"vendor {argv[:1]!r} is not under {prefix}")
        cwd = S / "repo"
    elif target == "clone":
        if argv:
            raise Refused("spec-malformed", "clone builds its own argv")
        if kind is None:
            raise Refused("bad-token", "clone takes the alloc kind (never defaulted)")
        argv, cwd = clone_argv(S, kind), S / "repo"
    elif target == "clone-check":
        _check_clone_check_argv(argv)
        cwd = S / "repo"
    elif target == "step":
        _check_step_argv(argv)
        cwd, resolve = S / "repo", True
    elif target == "driver":
        tree = PurePosixPath(str(IMPORT_TREE[host]))
        if not argv or not _abs_path(argv[0]) or (argv[0] != ROOT_PYTHON[host]
                                                   and tree not in PurePosixPath(argv[0]).parents):
            raise Refused("target-off-list", f"driver {argv[:1]!r} is neither {ROOT_PYTHON[host]} nor under {tree}")
        cwd = S / "driver"
    elif target == "forwarder":
        if host != "linux":
            raise Refused("unsupported-host", "the forwarder is Linux's (§9.1); macOS slots dial the front's port")
        if argv or tuple(keep_fds):
            raise Refused("spec-malformed", "the forwarder builds its own argv and fds")
        sock = PurePosixPath(str(RUN[host])) / "gw" / f"slot{slot.index}" / "gw.sock"
        argv = module_argv(host, "levain.slot.forwarder", "--port", str(FORWARDER_PORT), "--unix", str(sock),
                           "--ready-fd", str(READY_FD))
        cwd, keep_fds = S / "tmp", (READY_FD,)
    elif target == "probe":
        if argv or tuple(keep_fds) or env_file is not None:
            raise Refused("spec-malformed", "the probe builds its own argv and fds and reads no env file")
        if host == "darwin":
            want = str(root_layer / PROBE_PROFILE)
            if profile is not None and profile != want:
                raise Refused("spec-malformed", f"the probe runs only under {want}")
            profile = want
        argv = module_argv(host, PROBE_MODULE, "--result-fd", str(PROBE_RESULT_FD), *probe_addrs)
        cwd, keep_fds = S / "tmp", (PROBE_RESULT_FD,)
    elif target == "identity":
        if argv or tuple(keep_fds):
            raise Refused("spec-malformed", "the identity call builds its own argv and keeps no fd")
        want = str(root_layer / "env")
        if env_file is not None and env_file != want:
            raise Refused("spec-malformed", f"the identity call reads only {want}")
        if host == "darwin":
            want = str(root_layer / "profile.sb")
            if profile is not None and profile != want:
                raise Refused("spec-malformed", f"the identity call runs only under {want}")
        argv = module_argv(host, IDENTITY_MODULE)
        cwd = S / "tmp"
    else:   # front
        if len(argv) > _FRONT_ARGS or not all(
                isinstance(a, str) and "\x00" not in a and len(a.encode("utf-8", "surrogatepass")) <= _STEP_ARG_BYTES
                for a in argv):
            raise Refused("spec-malformed", f"the front's fixed argv: <= {_FRONT_ARGS} strings of <= 4 KiB, no NUL")
        argv = module_argv(host, front_module or "", *argv)
        cwd = PurePosixPath("/")

    slot_target = target != "front"
    if env_file is None and target in ("vendor", "clone", "clone-check", "step", "driver", "identity"):
        # A verifier renders two env files (§3.4: the driver's HOME is <S>/driver, the child's <S>).
        env_file = str(root_layer / ("driver.env" if target == "driver" else "env"))
    if env_file is not None and (target in ("front", "forwarder") or not _under(env_file, root_layer)):
        raise Refused("view-path-off-list", f"env file {env_file!r} is not in {root_layer}")
    if host == "darwin" and slot_target:
        profile = profile if profile is not None else str(root_layer / "profile.sb")
        if not _under(profile, root_layer):
            raise Refused("view-path-off-list", f"profile {profile!r} is not in {root_layer}")
    elif profile is not None:
        raise Refused("spec-malformed", f"{host} {target}: no profile on this path")
    spec = Spec(target, who.uid, who.gid, who.groups, NPROC if slot_target else None, env_file,
                tuple(sorted((extra_env or {}).items())), profile, str(cwd), argv, resolve, tuple(keep_fds), exec_fd)
    _check_spec(spec)
    size = len(spec.to_json())
    if size > SPEC_CAP:   # the stage would refuse it as malformed; refused here by name, before any fork
        raise Refused("spec-over-cap", f"{target} spec of {size} bytes, past {SPEC_CAP}")
    return spec


# --- the wall's facts (s5 §5b, A7) ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class WallFacts:
    """One wall's capability list as a value: the parameter the profile-line builder
    (:func:`levain.slot.render.profile_lines`) and the view builder (:func:`wall_view_args`) take, so a slot and an
    export target differ only in their facts, never in a forked builder (s5 §5b, A7).

    Both OSes: ``writable`` (groups of paths: on macOS one ``file-write*`` line per group; on Linux every path is
    one rw bind at its own path, in order), ``chdir`` and ``keep_fds`` (the fds kept past 0, 1, 2). macOS:
    ``label`` (the profile's comment line), ``read_denied`` (``file-read*`` denied subpaths), ``devices`` (the
    writable device literals), ``devices_only`` (reads and ioctls under ``/dev`` limited to ``devices``),
    ``network`` (the network rule lines, required: an empty list would leave ``(allow default)`` holding
    them), ``ipc_denied``, ``mach_allow``. Linux: ``unshare`` (the namespaces bwrap makes beside the mount one,
    in order), ``new_session``, ``caps`` (None = bwrap's own default; a tuple = ``--cap-drop ALL`` then those
    added back), ``read_only`` (ro binds beside ``/usr`` and ``/etc``), ``host_dev`` (the host's ``/dev`` bound
    whole, the slot's; else bwrap's minimal ``--dev``)."""

    writable: Tuple[Tuple[str, ...], ...]
    network: Tuple[str, ...] = ()
    label: str = ""
    read_denied: Tuple[str, ...] = ()
    devices: Tuple[str, ...] = ()
    devices_only: bool = False
    ipc_denied: bool = False
    mach_allow: Tuple[str, ...] = ()
    unshare: Tuple[str, ...] = ()
    new_session: bool = False
    caps: Optional[Tuple[str, ...]] = ()
    read_only: Tuple[str, ...] = ()
    host_dev: bool = False
    chdir: str = "/"
    keep_fds: Tuple[int, ...] = ()


#: s5 §5b: each export target's ONE durable writable path, relative to ``L/export/`` (its per-run image's mount
#: point). The wall facts and the sealed run read the same constant, so they cannot disagree (§4.9).
EXPORT_WRITABLE = (".work",)
VERIFY_WRITABLE = (".verify",)
#: s5 §5b (HEAD_r10 Q-r7-5): the one protected object, the users' data, under every spelling macOS gives it.
EXPORT_READ_DENIED = ("/Users", "/System/Volumes/Data/Users", "/Volumes")
#: s5 §5b: an export target's devices on macOS, NOT the slot's set (no tty, no ptmx, no dtracehelper).
#: ``/dev/urandom``: the target's interpreter (``/usr/bin/python3``) reads it at startup for hash randomization and
#: exits ``Fatal Python error`` without it (measured in a slot-runtime VM, 1026+8 wallexec). The profile allows each
#: device for write as well as read; the node is ``crw-rw-rw-`` on macOS, so any account can write it outside a wall.
EXPORT_DEVICES = ("/dev/null", "/dev/zero", "/dev/urandom")
#: s5 §5b: the start of the Mach allow-list; the list git and the importer need is measured on the VM (5g).
EXPORT_MACH_ALLOW = ("com.apple.system.opendirectoryd.libinfo",)
#: s5 §5b: ``--cap-drop ALL`` with only the two capabilities the stage spends on its drop added back. The stage
#: runs as root inside bwrap and calls ``setgroups`` / ``setgid`` / ``setuid``; with no capability at all each
#: fails EPERM and nothing starts [reasoned from bwrap's privileged path; not run on Linux, a 5g row].
#: ``drop_and_assert`` clears the inheritable set bwrap fills with them, then proves the permitted, effective,
#: ambient and inheritable sets empty, and ``no_new_privs`` keeps a setuid program from using the bounding set
#: left behind.
EXPORT_CAPS = ("CAP_SETGID", "CAP_SETUID")
#: s5 §5b: ``RLIMIT_NPROC`` for an export target (its principal owns no other process).
EXPORT_NPROC = 64
#: s5 §5b: the fd an export target's bounded result pipe sits on (its fd map is 0 / 1, 2 / this).
RESULT_FD = 4
_EXPORT_DIRS = {"importer": EXPORT_WRITABLE, "verifier": VERIFY_WRITABLE}
_EXPORT_SCOPE = {"importer": "export.scope", "verifier": "verify.scope"}


def export_wall_facts(host: str, role: str) -> WallFacts:
    """The importer's or the verifier's wall (s5 §5b capability list): the one writable path ``L/export/.work``
    (``.verify``), the cwd there, fd 4 kept (the bounded result pipe). macOS: ``(allow default)`` reads less the
    three user-data subpaths, :data:`EXPORT_DEVICES` the only devices, all network and IPC denied, Mach
    denied but for the allow-list. Linux: fresh net, IPC, PID and UTS namespaces, a new session, capabilities
    dropped (:data:`EXPORT_CAPS`), ro ``/usr`` ``/etc`` (+ the import tree and helper dir where ``/usr`` does not
    hold them), bwrap's minimal ``/dev``, nothing of the operator's and no socket dir."""
    from levain.slot.layout import HELPER_DIR, IMPORT_TREE, STORE

    if host not in ("darwin", "linux"):
        raise Refused("unsupported-host", repr(host))
    if role not in EXPORT_TARGETS:
        raise Refused("bad-token", f"export role {role!r}")
    base = PurePosixPath(str(STORE[host])) / "export"
    writable = tuple(str(base / d) for d in _EXPORT_DIRS[role])
    common = dict(writable=(writable,), chdir=writable[0], keep_fds=(RESULT_FD,))
    if host == "darwin":
        return WallFacts(label=f"Levain export wall profile, target {role}, principal {EXPORT_ACCOUNT[role]}.",
                         read_denied=EXPORT_READ_DENIED, devices=EXPORT_DEVICES, devices_only=True,
                         network=("(deny network*)",), ipc_denied=True, mach_allow=EXPORT_MACH_ALLOW, **common)
    ro = tuple(str(p) for p in (IMPORT_TREE[host], HELPER_DIR[host])
               if PurePosixPath("/usr") not in PurePosixPath(str(p)).parents
               and PurePosixPath("/etc") not in PurePosixPath(str(p)).parents)
    return WallFacts(unshare=("net", "ipc", "pid", "uts"), new_session=True, caps=EXPORT_CAPS, read_only=ro,
                     host_dev=False, **common)


def _export_dir(host: str) -> PurePosixPath:
    from levain.slot.layout import STORE

    return PurePosixPath(str(STORE[host])) / "export"


def _export_profile(role: str) -> str:
    """macOS: the export role's profile, ``L/export/<role>.sb``."""
    return str(_export_dir("darwin") / f"{role}.sb")


def export_launch_spec(host: str, role: str, *, ids: Mapping[str, int], shared: Mapping[str, int],
                       args: Sequence[str] = (), export_class: Optional[str] = None) -> Spec:
    """The stage's spec for an export target (s5 §5b; build note [r6: I codex MED launch]): the same
    :class:`Spec` as :func:`launch_spec`, built on its own path because ``launch_spec`` is slot-shaped. The
    principal is :func:`export_principal`'s; argv is the role's root-tree entry (``HELPER_DIR/export-<role>``)
    then ``args``, root's fixed arguments (at most :data:`_FRONT_ARGS` strings of at most 4 KiB, no NUL); cwd and
    the kept fd are the role's :func:`export_wall_facts`; ``RLIMIT_NPROC`` :data:`EXPORT_NPROC`; no env file and
    no extra env (the stage execs it with an empty env); macOS the profile ``L/export/<role>.sb``."""
    from levain.slot.layout import HELPER_DIR

    if host not in ("darwin", "linux"):
        raise Refused("unsupported-host", repr(host))
    if role not in EXPORT_TARGETS:
        raise Refused("bad-token", f"export role {role!r}")
    who = export_principal(host, role, ids, shared, export_class=export_class)
    facts = export_wall_facts(host, role)
    args = tuple(args)
    if len(args) > _FRONT_ARGS or not all(
            isinstance(a, str) and "\x00" not in a and len(a.encode("utf-8", "surrogatepass")) <= _STEP_ARG_BYTES
            for a in args):
        raise Refused("spec-malformed", f"an export target's fixed argv: <= {_FRONT_ARGS} strings of <= 4 KiB")
    argv = (str(PurePosixPath(str(HELPER_DIR[host])) / EXPORT_ENTRY[role]),) + args
    profile = _export_profile(role) if host == "darwin" else None
    spec = Spec(role, who.uid, who.gid, who.groups, EXPORT_NPROC, None, (), profile, facts.chdir, argv, False,
                facts.keep_fds)
    _check_spec(spec)
    size = len(spec.to_json())
    if size > SPEC_CAP:
        raise Refused("spec-over-cap", f"{role} spec of {size} bytes, past {SPEC_CAP}")
    return spec


def stage_argv(host: str, spec_fd: int = SPEC_FD) -> Tuple[str, ...]:
    """The stage: this module run as ``__main__`` by the root interpreter, reading its spec from ``spec_fd``."""
    return module_argv(host, "levain.slot.wall", "--spec-fd", str(spec_fd))


# --- macOS (§8.2) ----------------------------------------------------------------------------------------------


def macos_launch_argv(spec: Spec, *, spec_fd: int = SPEC_FD) -> Tuple[str, ...]:
    """§8.2: ``launchctl asuser <uid>`` (the slot's bootstrap namespace, measured to cut pasteboard and
    screenshots, runs/macos-RD2) -> the stage, which drops, asserts, sets ``RLIMIT_NPROC`` and execs the
    target under ``sandbox-exec -f <profile>``. The front skips ``asuser`` and the profile."""
    _check_spec(spec)
    if spec.target == "front":
        if spec.profile is not None:
            raise Refused("spec-malformed", "the front runs with no profile")
        return stage_argv("darwin", spec_fd)
    if spec.profile is None:
        raise Refused("spec-malformed", f"a macOS {spec.target} runs under its profile")
    export_dir = _export_dir("darwin")
    if spec.target in EXPORT_TARGETS:
        if spec.profile != _export_profile(spec.target):
            raise Refused("spec-malformed", f"the {spec.target} runs only under its own export profile")
    elif export_dir in PurePosixPath(spec.profile).parents:
        raise Refused("spec-malformed", f"a slot {spec.target} under an export profile")
    return (LAUNCHCTL, "asuser", str(spec.uid)) + stage_argv("darwin", spec_fd)


# --- Linux (§9) ----------------------------------------------------------------------------------------------

#: §9.2: the host top-level names the view carries as links into /usr, never as binds.
_USR_LINKS = ("/bin", "/sbin", "/lib", "/lib64")


def read_host_links(readlink: Callable[[str], str] = os.readlink) -> dict:
    """Each of ``/bin /sbin /lib /lib64`` -> its link target on this host, or None where the host has no
    such link (``/lib64`` is absent on arm64, R-L1 N3c ``ls /``)."""
    out: dict = {}
    for p in _USR_LINKS:
        try:
            out[p] = readlink(p)
        except FileNotFoundError:
            out[p] = None
        except OSError as e:
            if e.errno != errno.EINVAL:   # EINVAL = a real directory: refused below, by name
                raise
            out[p] = ""
    return out


def read_netns_names(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> frozenset:
    """The namespaces ``ip`` itself knows (``ip netns list``: ``<name>`` or ``<name> (id: <k>)`` per line),
    as :mod:`levain.slot.netns` reads them. ``ip`` failing is not "none listed": it raises."""
    from levain.firing.hands import _abs
    from levain.launch import child_env

    r = run([_abs("ip"), "netns", "list"], capture_output=True, text=True, env=child_env())
    if r.returncode != 0:
        raise Refused("netns-missing", f"ip netns list failed: {(r.stderr or '').strip()}")
    return frozenset(line.split()[0] for line in r.stdout.splitlines() if line.strip())


def cgroup_dir(slot, session: str, leaf: Optional[str] = None) -> str:
    """§7.3: ``/sys/fs/cgroup/levain.slice/{e,v}<n>.scope/<session>/``; a verifier step its own child cgroup
    (§5.6 step 2). DECISION: leaf ``step<k>`` per step and ``driver`` for the driver; every other target
    sits in the session's own cgroup."""
    _session_dir("linux", session)
    if leaf is not None and not re.fullmatch(r"step(0|[1-9][0-9]?)|driver", leaf):
        raise Refused("bad-token", f"cgroup leaf {leaf!r}")
    base = f"{CGROUP_ROOT}/{slot.cgroup}/{session}"
    return base if leaf is None else f"{base}/{leaf}"


def linux_view_args(slot, session: str, *, kind: str, queue_grant: bool, host_links: Mapping[str, Optional[str]],
                    ro_prefixes: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """The bwrap arguments for the §9.2 root view, an ALLOW-LIST: anything not named here is absent from the
    slot's root (R-L1 N3: a socket under ``/var/lib`` is then ``FileNotFoundError``, not a guarded path).

    ro ``/usr`` ``/etc`` + the vendor and import prefixes (bound only where ``/usr`` does not already hold
    them) · ``/bin /sbin /lib /lib64`` as the host's links into ``/usr`` · a fresh ``/proc`` (a new PID
    namespace) · ``/dev`` bound with a fresh ``/dev/shm`` · tmpfs ``/tmp /var/tmp /run`` · rw ``<S>`` · the
    slot's socket dirs: ``R/gw/slot<n>/`` (egress), ``R/qb/slot<n>/`` (queue grant), ``R/planner/slot<n>/``
    (planner kind), each bind's ancestors made first with their §2.1 / §2.2 modes
    (:func:`view_ancestor_dirs`). No ``/sys``. Per-session IPC, PID and UTS namespaces (§9.1, plan §2e 7); bwrap always
    makes the mount namespace. The network namespace is joined by ``nsenter``, never unshared here.
    [not run: R-L1 built this view with hand mounts + ``pivot_root``; bwrap run AS ROOT building it (``/dev``
    recursion, no ``--cap-*`` so the slot's own nested bwrap keeps a full bounding set) is a 2g row.]

    A ro prefix off the allow-list, a socket grant the slot's class or kind does not carry, or a host link
    that does not point into ``/usr`` = ``view-path-off-list``, by name."""
    from levain.slot.layout import IMPORT_TREE, RUN
    from levain.slot.root_tree import VENDOR_PREFIX

    S = _session_dir("linux", session)
    allowed = (str(VENDOR_PREFIX["linux"]), str(IMPORT_TREE["linux"]))
    prefixes = tuple(allowed if ro_prefixes is None else ro_prefixes)
    for p in prefixes:
        if p not in allowed:
            raise Refused("view-path-off-list", f"ro {p!r}: only {', '.join(allowed)} join /usr and /etc")
    if slot.slot_class == "none":
        if kind != "verifier":
            raise Refused("bad-token", f"a verifier pair runs kind verifier, not {kind!r}")
        if queue_grant:
            raise Refused("view-path-off-list", "a none-class session reaches no Levain socket (§1)")
    elif kind == "verifier":
        raise Refused("bad-token", "kind verifier runs in a verifier pair")

    binds = [str(S)]
    if slot.slot_class == "egress":
        R = PurePosixPath(str(RUN["linux"]))
        grants = ["gw"] + (["qb"] if queue_grant else []) + (["planner"] if kind == "planner" else [])
        binds += [str(R / g / f"slot{slot.index}") for g in grants]
    ro = tuple(p for p in prefixes if PurePosixPath("/usr") not in PurePosixPath(p).parents
               and PurePosixPath("/etc") not in PurePosixPath(p).parents)
    # No --cap-* (caps None): the slot's own nested bwrap keeps a full bounding set.
    return wall_view_args(WallFacts(writable=(tuple(binds),), unshare=("ipc", "pid", "uts"), caps=None,
                                    read_only=ro, host_dev=True, chdir="/"), host_links)


def wall_view_args(facts: WallFacts, host_links: Mapping[str, Optional[str]]) -> Tuple[str, ...]:
    """The bwrap view of one wall, a pure function of its :class:`WallFacts` (s5 §5b, A7): the slot's view
    (:func:`linux_view_args`) and the export targets' (:func:`importer_view_args`, :func:`verifier_view_args`) are
    this one builder called with different facts. In order: ``--die-with-parent``, the ``--unshare-*`` flags,
    ``--new-session``, the capability flags, ro ``/usr`` ``/etc`` and the ``read_only`` binds, the host's
    ``/bin /sbin /lib /lib64`` links into ``/usr`` (any other link = ``view-path-off-list``), a fresh ``/proc``,
    ``/dev`` (the host's bound whole, or bwrap's minimal one) with a fresh ``/dev/shm``, tmpfs ``/tmp /var/tmp
    /run``, the writable binds' ancestors (:func:`view_ancestor_dirs`), the rw binds, ``--chdir``."""
    # --die-with-parent: bwrap's sandbox (every process of it, the target included) is SIGKILLed when bwrap or
    # its parent, the helper, dies, so a helper that dies or kills bwrap never leaves a vendor running past it.
    # It also bounds the forwarder: it lives until the helper exits, no longer until END [not run: a 2g row].
    args = ["--die-with-parent"] + [f"--unshare-{ns}" for ns in facts.unshare]
    if facts.new_session:
        args.append("--new-session")
    if facts.caps is not None:
        args += ["--cap-drop", "ALL"]
        for cap in facts.caps:
            args += ["--cap-add", cap]
    args += ["--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc"]
    for p in facts.read_only:
        args += ["--ro-bind", p, p]
    for name in _USR_LINKS:
        target = host_links.get(name)
        if target is None:
            continue
        resolved = PurePosixPath(target if target.startswith("/") else f"/{target}")
        if not target or ".." in resolved.parts or PurePosixPath("/usr") not in resolved.parents:
            raise Refused("view-path-off-list", f"{name} -> {target!r} is not a link into /usr (§9.2)")
        args += ["--symlink", target, name]
    args += ["--proc", "/proc"]
    args += ["--dev-bind", "/dev", "/dev"] if facts.host_dev else ["--dev", "/dev"]
    args += ["--tmpfs", "/dev/shm", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run"]
    binds = [p for group in facts.writable for p in group]
    if not binds or not all(_abs_path(p) for p in binds) or not _abs_path(facts.chdir):
        raise Refused("view-path-off-list", "a wall view needs absolute writable paths and cwd")
    args += view_ancestor_dirs(binds)
    for d in binds:
        args += ["--bind", d, d]
    args += ["--chdir", facts.chdir]
    return tuple(args)


def importer_view_args(host_links: Mapping[str, Optional[str]]) -> Tuple[str, ...]:
    """The importer's bwrap view (s5 §5b): :func:`wall_view_args` over ``export_wall_facts("linux", "importer")``."""
    return wall_view_args(export_wall_facts("linux", "importer"), host_links)


def verifier_view_args(host_links: Mapping[str, Optional[str]]) -> Tuple[str, ...]:
    """The verifier's bwrap view (s5 §5b, §6.0): the importer's shape with ``.verify`` the one rw bind."""
    return wall_view_args(export_wall_facts("linux", "verifier"), host_links)


#: DECISION: an ancestor §2.1 / §2.2 gives no row (``/var``, ``/run``) is the host's ordinary 0755.
_HOST_DIR_MODE = 0o755


def view_ancestor_modes() -> dict:
    """The modes the view's ancestor dirs take: layout's rows for the store and run dirs (§2.1, §2.2), the one
    source ``install`` makes the host's dirs from."""
    from levain.slot.layout import base_run_rows, base_store_rows

    rows = base_store_rows("linux", "-") + base_run_rows("linux")
    return {str(r.path): r.mode for r in rows if r.kind == "dir"}


def view_ancestor_dirs(binds: Iterable[str]) -> Tuple[str, ...]:
    """``--perms <mode> --dir <path>`` for every ancestor of each bind destination, parents first, each once.
    Without them bwrap makes a destination's missing parents ``root:root 0700`` and the slot cannot reach its
    own ``<S>`` (VM run s2-linux F4: ``/var/levain``, ``/var/levain/live``, ``/run/levain``, ``/run/levain/gw``).
    A ``--dir`` on a dir an earlier op already made (``/var`` by ``--tmpfs /var/tmp``, the ``/run`` tmpfs) leaves
    it as it is."""
    modes = view_ancestor_modes()
    out: list = []
    seen: set = set()
    for d in binds:
        for a in reversed(PurePosixPath(d).parents):
            if str(a) == "/" or str(a) in seen:
                continue
            seen.add(str(a))
            out += ["--perms", f"{modes.get(str(a), _HOST_DIR_MODE):04o}", "--dir", str(a)]
    return tuple(out)


@dataclass(frozen=True)
class LinuxLaunch:
    """``cgroup``: the dir whose ``cgroup.procs`` the forked root child joins before it execs ``argv``
    (None for the front, which runs in the host's namespaces: it needs upstream TCP)."""

    cgroup: Optional[str]
    argv: Tuple[str, ...]


def require_netns(slot, names: Iterable[str]) -> str:
    """§1.3: ``start`` refuses ``netns-missing`` rather than ever run a slot in the host's namespace."""
    if slot.netns not in set(names):
        raise Refused("netns-missing", slot.netns)
    return f"{NETNS_DIR}/{slot.netns}"


def linux_launch_argv(spec: Spec, *, slot, session: str, kind: str, queue_grant: bool = False,
                      netns_names: Callable[[], Iterable[str]] = read_netns_names,
                      host_links: Optional[Mapping[str, Optional[str]]] = None,
                      cgroup_leaf: Optional[str] = None, spec_fd: int = SPEC_FD) -> LinuxLaunch:
    """§9: ``nsenter --net=<the slot's netns>`` -> Levain's bwrap copy (root; the §9.2 view) -> the stage.
    The netns is checked against ``ip netns list`` first (``netns-missing``); ``nsenter`` failing to open it
    is a second, fail-closed refusal (no exec). The front: the stage alone."""
    from levain.slot.bwrap import LEVAIN_BWRAP

    _check_spec(spec)
    if spec.target in EXPORT_TARGETS:      # never nsenter, the host /dev or a slot cgroup for an export target
        raise Refused("spec-malformed", f"{spec.target} is an export target: export_linux_launch_argv launches it")
    if spec.profile is not None:
        raise Refused("spec-malformed", "Linux runs no Seatbelt profile")
    if spec.target == "front":
        return LinuxLaunch(None, stage_argv("linux", spec_fd))
    if spec.target in ("step", "driver") and cgroup_leaf is None:
        raise Refused("spec-malformed", "a verifier step and the driver each run in their own cgroup (§5.6)")
    ns = require_netns(slot, netns_names())
    view = linux_view_args(slot, session, kind=kind, queue_grant=queue_grant,
                           host_links=read_host_links() if host_links is None else host_links)
    argv = (NSENTER, f"--net={ns}", "--", str(LEVAIN_BWRAP)) + view + ("--",) + stage_argv("linux", spec_fd)
    return LinuxLaunch(cgroup_dir(slot, session, cgroup_leaf), argv)


def export_linux_launch_argv(spec: Spec, *, host_links: Optional[Mapping[str, Optional[str]]] = None,
                             spec_fd: int = SPEC_FD) -> LinuxLaunch:
    """s5 §5b: an export target's Linux launch. The forked root child joins this run's leaf
    (``levain.slice/export.scope/run``, the verifier's ``verify.scope/run``), then Levain's bwrap copy, run as
    root, builds the role's view (:func:`importer_view_args` / :func:`verifier_view_args`; its ``--unshare-net`` is
    the empty network namespace, so no ``nsenter``) -> the stage (drop + assert + ``no_new_privs``) -> the entry.
    This builds the argv only: it sets no cgroup limit and makes no image."""
    from levain.slot.bwrap import LEVAIN_BWRAP

    _check_spec(spec)
    if spec.target not in EXPORT_TARGETS:
        raise Refused("spec-malformed", f"{spec.target} is not an export target")
    if spec.profile is not None:
        raise Refused("spec-malformed", "Linux runs no Seatbelt profile")
    view = wall_view_args(export_wall_facts("linux", spec.target),
                          read_host_links() if host_links is None else host_links)
    argv = (str(LEVAIN_BWRAP),) + view + ("--",) + stage_argv("linux", spec_fd)
    return LinuxLaunch(f"{CGROUP_ROOT}/{_EXPORT_SCOPE[spec.target]}/run", argv)


# --- macOS start check: the live pf anchor + the slot's alias (carried item F5) ---------------------------------

_PF_NOISE = ("No ALTQ support in kernel", "ALTQ related functions disabled")


def _pf_expand(tokens: list) -> list:
    """``{ a b }`` lists -> every combination, as pf prints a rule it expanded."""
    for i, t in enumerate(tokens):
        if t.startswith("{"):
            j = next(k for k in range(i, len(tokens)) if tokens[k].endswith("}"))
            inner = " ".join(tokens[i:j + 1]).strip("{} ").split()
            return [r for v in inner for r in _pf_expand(tokens[:i] + [v] + tokens[j + 1:])]
    return [tokens]


def _ipv4(s: str) -> bool:
    return re.fullmatch(r"(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}", s) is not None


def pf_printed(rule: str) -> list:
    """The lines ``pfctl -sr`` prints for one rule in the shapes :func:`levain.slot.pf.anchor_rules` writes
    (measured: runs/s1-1c-macos/raw/C08_C09_pf_lo0.txt): ``block`` -> ``block drop``; lists expanded;
    ``inet`` added beside an IPv4 address; ``from any to any`` -> ``all``; ``user N`` / ``port N`` ->
    ``user = N`` / ``port = N``; a tcp ``pass … keep state`` gains ``flags S/SA``. A shape it does not know is
    refused (``pf-rule-unparsed``), so a change in pf.py's rules fails the check closed, never open."""
    out = []
    try:
        expanded = _pf_expand(rule.replace("{", " { ").replace("}", " } ").split())
    except StopIteration:   # an unclosed list
        raise Refused("pf-rule-unparsed", rule) from None
    for t in expanded:
        t = [x for x in t if x not in ("{", "}")]
        try:
            i = 0
            action = t[i]; i += 1
            if action not in ("block", "pass"):
                raise ValueError(action)
            direction = t[i]; i += 1
            if direction not in ("in", "out") or t[i] != "quick":
                raise ValueError(direction)
            i += 1
            iface = af = None
            if t[i] == "on":
                iface, i = t[i + 1], i + 2
            if t[i] in ("inet", "inet6"):
                af, i = t[i], i + 1
            if t[i] != "proto":
                raise ValueError(t[i])
            proto, i = t[i + 1], i + 2
            src = dst = "any"
            neg = False
            if i < len(t) and t[i] == "from":
                src = t[i + 1]
                if t[i + 2] != "to":
                    raise ValueError("to")
                i += 3
                if t[i] == "!":
                    neg, i = True, i + 1
                dst, i = t[i], i + 1
            port = user = flags = None
            keep = False
            while i < len(t):
                if t[i] == "port":
                    port, i = t[i + 1], i + 2
                elif t[i] == "user":
                    if t[i + 1] == "!=":
                        user, i = ("!=", t[i + 2]), i + 3
                    else:
                        user, i = ("=", t[i + 1]), i + 2
                elif t[i] == "flags":
                    flags, i = t[i + 1], i + 2
                elif t[i:i + 2] == ["keep", "state"]:
                    keep, i = True, i + 2
                else:
                    raise ValueError(t[i])
        except (IndexError, ValueError):
            raise Refused("pf-rule-unparsed", rule) from None
        if src != "any":
            raise Refused("pf-rule-unparsed", rule)
        if af is None and _ipv4(dst):
            af = "inet"
        if action == "pass" and proto == "tcp" and keep and flags is None:
            flags = "S/SA"
        parts = ["block drop" if action == "block" else "pass", direction, "quick"]
        parts += [f"on {iface}"] if iface else []
        parts += [af] if af else []
        parts += [f"proto {proto}"]
        parts += ["all"] if dst == "any" and not neg and port is None else [f"from any to {'! ' if neg else ''}{dst}"]
        parts += [f"port = {port}"] if port else []
        parts += [f"user {user[0]} {user[1]}"] if user else []
        parts += [f"flags {flags}"] if flags else []
        parts += ["keep state"] if keep else []
        out.append(" ".join(parts))
    return out


def expected_slot_rules(slot, ids: Mapping[str, int]) -> frozenset:
    """The printed lines the live anchor must hold for ``slot``: :func:`levain.slot.pf.anchor_rules` for that
    slot alone (its rules name only its own uid and alias once their lists are expanded), so the expected
    text has ONE source."""
    from levain.slot.pf import anchor_rules

    return frozenset(line for r in anchor_rules([slot], ids) for line in pf_printed(r))


def check_egress_ready(slot, ids: Mapping[str, int], *, pf_text: str, ifconfig_text: str) -> None:
    """Carried item F5: an egress slot on macOS starts only while (1) the LIVE sub-anchor holds every line of
    its rules and (2) lo0 is UP and carries its alias (§24, V27). Missing either = refused by name."""
    from levain.slot.ids import loopback_alias

    if slot.slot_class != "egress":
        raise Refused("bad-token", f"{slot.tag}: only an egress slot has pf rules to check")
    live = {" ".join(line.split()) for line in pf_text.splitlines()
            if line.strip() and not line.startswith(_PF_NOISE)}
    if any("<__automatic" in line for line in live):
        # pfctl's optimizer folds similar rules into a table (runs/s2-macos raw/M01c_pf_slots: 8 slots, 2
        # tables); the lines are then not the ones loaded, so refuse, never guess. pf.load_steps loads with
        # ``-o none`` so a Levain-loaded anchor never holds one.
        raise Refused("pf-anchor-not-loaded", "the live anchor holds an optimizer table; its rules cannot be read back")
    missing = sorted(expected_slot_rules(slot, ids) - live)
    if missing:
        raise Refused("pf-anchor-not-loaded", f"{slot.tag}: {len(missing)} rule(s) absent, first: {missing[0]}")
    alias = loopback_alias(slot)
    lines = ifconfig_text.splitlines()
    head = lines[0] if lines else ""
    m = re.match(r"lo0: flags=[0-9a-fA-F]+<([^>]*)>", head)
    if m is None or "UP" not in m.group(1).split(","):
        raise Refused("loopback-alias-down", f"lo0 is not up: {head!r}")
    if not any(f.split()[:2] == ["inet", alias] for f in lines[1:]):
        raise Refused("loopback-alias-down", f"lo0 has no inet {alias} ({slot.tag})")


def _read_cmd(argv: Sequence[str], run) -> str:
    from levain.launch import child_env

    r = run(list(argv), capture_output=True, text=True, env=child_env())
    if r.returncode != 0:
        raise Refused("pf-anchor-not-loaded", f"{' '.join(argv)}: {(r.stderr or r.stdout or '').strip()}")
    return r.stdout


def check_pf_enabled(info_text: str) -> None:
    """pf itself is on: a line of ``pfctl -s info`` that starts ``Status: Enabled`` (measured after a reboot in
    r3c10-vm 10-26: ``Status: Enabled for 0 days 00:00:10           Debug: Urgent``). A loaded anchor enforces nothing
    while pf is disabled, so anything else is refused (L3 r1 codex HIGH, lane pfboot)."""
    if not any(line.split()[:2] == ["Status:", "Enabled"] for line in info_text.splitlines()):
        first = next((line.strip() for line in info_text.splitlines() if line.strip()), "")
        raise Refused("pf-disabled", f"pfctl -s info does not report Status: Enabled ({first[:80]!r})")


def macos_egress_ready(slot, ids: Mapping[str, int], *,
                       run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> None:
    """:func:`check_pf_enabled` on ``pfctl -s info``, then :func:`check_egress_ready` on the live reads:
    ``pfctl -a com.apple/levain -sr`` (§23's sub-anchor, the one doctor reads) and ``ifconfig lo0``. Root only; the
    reads are the ``run`` seam."""
    from levain.slot.pf import ANCHOR, IFCONFIG, PFCTL

    check_pf_enabled(_read_cmd((PFCTL, "-s", "info"), run))
    check_egress_ready(slot, ids, pf_text=_read_cmd((PFCTL, "-a", ANCHOR, "-sr"), run),
                       ifconfig_text=_read_cmd((IFCONFIG, "lo0"), run))


# --- the profile guard (§8; trg-069) -------------------------------------------------------------------------------


def check_profile_text(text: str) -> None:
    """Refuse a profile that names a ``syscall`` operation, before anything applies it. Comments (``;`` to the
    end of a line, ``#| … |#``) are skipped, so a template may say why the rule is absent; everything else
    counts, string literals included (SBPL is a Scheme: a string can become an operation name). A profile
    this scanner cannot read to its end is refused too."""
    code = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("#|", i):
            j = text.find("|#", i + 2)
            if j < 0:
                raise Refused("profile-unreadable", "an unterminated #| comment")
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise Refused("profile-unreadable", "an unterminated string")
            code.append(text[i:j + 1])
            i = j + 1
        else:
            code.append(c)
            i += 1
    if "syscall" in "".join(code).lower():
        raise Refused("profile-syscall-rule", "a profile may never name a syscall operation (trg-069)")


# --- the stage -----------------------------------------------------------------------------------------------------


def _read_fd(fd: int, cap: int) -> bytes:
    chunks, size = [], 0
    try:
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            size += len(b)
            if size > cap:
                raise Refused("spec-malformed", "over the cap")
            chunks.append(b)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _read_file(path: str, cap: int) -> bytes:
    """A regular file, its last component never followed, at most ``cap`` bytes."""
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            raise Refused("view-path-off-list", f"{path}: not a regular file within {cap} bytes")
        data = b""
        while len(data) <= cap:
            b = os.read(fd, 65536)
            if not b:
                return data
            data += b
        raise Refused("view-path-off-list", f"{path}: grew past {cap} bytes")
    finally:
        os.close(fd)


def _darwin_kernel_groups() -> list:
    """The KERNEL's supplementary groups on macOS. ``os.getgroups()`` there returns the directory service's
    list for the euid's user, which ``setgroups`` does not change (Python docs, os.getgroups), so it cannot
    prove a drop; libSystem's plain ``getgroups`` symbol returns the list the process holds (getgroups(2)
    [not run after a real drop: a 2g row])."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.getgroups
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
    n = fn(0, None)
    if n < 0:
        raise OSError(ctypes.get_errno(), "getgroups")
    buf = (ctypes.c_uint32 * max(n, 1))()
    n = fn(len(buf), buf)
    if n < 0:
        raise OSError(ctypes.get_errno(), "getgroups")
    return [int(buf[i]) for i in range(n)]


def _darwin_maxprocperuid() -> int:
    """``kern.maxprocperuid`` by ``sysctlbyname`` (an int, readable by any uid): the per-uid process ceiling
    XNU clamps every ``RLIMIT_NPROC`` set to, silently (runs/s2-macos raw/M01d_M04: 2000 asked, 1333 read
    back). Raises ``OSError`` when the read fails."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.sysctlbyname
    fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                   ctypes.c_size_t]
    fn.restype = ctypes.c_int
    val = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(val))
    if fn(b"kern.maxprocperuid", ctypes.byref(val), ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctlbyname kern.maxprocperuid")
    if size.value != ctypes.sizeof(val):
        raise OSError(errno.EINVAL, f"sysctlbyname kern.maxprocperuid: {size.value} bytes")
    return int(val.value)


def _linux_caps() -> dict:
    out = {}
    with open("/proc/self/status", encoding="ascii") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            if k in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
                out[k] = int(v.strip(), 16)
    return out


def _list_fds() -> list:
    d = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
    out = []
    for name in os.listdir(d):
        fd = int(name)
        try:
            os.fstat(fd)
        except OSError as e:
            if e.errno == errno.EBADF:   # the listing's own fd, closed by the time it returns
                continue
            raise
        out.append(fd)
    return sorted(out)


def _access_x(path: str) -> bool:
    import stat

    try:
        return stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _write_text(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.write(fd, text.encode("ascii"))
    finally:
        os.close(fd)


def _read_text(path: str) -> str:
    with open(path, encoding="ascii") as fh:
        return fh.read()


_PR_SET_NO_NEW_PRIVS, _PR_GET_NO_NEW_PRIVS = 38, 39    # <linux/prctl.h>


def _linux_prctl(option: int, arg: int) -> int:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.prctl
    fn.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    fn.restype = ctypes.c_int
    r = fn(option, arg, 0, 0, 0)
    if r < 0:
        raise OSError(ctypes.get_errno(), "prctl")
    return r


def _linux_clear_inheritable() -> None:
    """``capget`` then ``capset`` with the inheritable set zeroed, permitted and effective as they were. Lowering
    the inheritable set needs no capability; the kernel then drops any ambient bit no longer inheritable
    [capabilities(7) and the kernel's capset rules; not run on Linux, a 5g row]."""
    import ctypes

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                    ("inheritable", ctypes.c_uint32)]

    libc = ctypes.CDLL(None, use_errno=True)
    hdr, data = Header(_LINUX_CAPABILITY_VERSION_3, 0), (Data * 2)()
    if libc.capget(ctypes.byref(hdr), data) < 0:
        raise OSError(ctypes.get_errno(), "capget")
    for d in data:
        d.inheritable = 0
    if libc.capset(ctypes.byref(hdr), data) < 0:
        raise OSError(ctypes.get_errno(), "capset")


_LINUX_CAPABILITY_VERSION_3 = 0x20080522    # <linux/capability.h>: two 32-bit words per set


def _linux_set_nnp() -> None:
    _linux_prctl(_PR_SET_NO_NEW_PRIVS, 1)


def _linux_get_nnp() -> int:
    return _linux_prctl(_PR_GET_NO_NEW_PRIVS, 0)


@dataclass
class OsCalls:
    """Every OS act the stage takes. ``getresuid`` / ``getresgid`` / ``capabilities`` / ``set_nnp`` /
    ``get_nnp``: Linux only (macOS has no public ``getresuid``, plan §2f 8: there the drop is PROVED by
    ``seteuid(0)`` / ``setegid(0)`` failing; XNU has no ``no_new_privs``: a setuid binary there is the
    profile's to refuse). ``maxprocperuid``: macOS only, ``kern.maxprocperuid`` (V3)."""

    setgroups: Callable = os.setgroups
    setgid: Callable = os.setgid
    setuid: Callable = os.setuid
    getuid: Callable = os.getuid
    geteuid: Callable = os.geteuid
    getgid: Callable = os.getgid
    getegid: Callable = os.getegid
    getgroups: Callable = os.getgroups
    seteuid: Callable = os.seteuid
    setegid: Callable = os.setegid
    getrlimit: Callable = resource.getrlimit
    setrlimit: Callable = resource.setrlimit
    getresuid: Optional[Callable] = None
    getresgid: Optional[Callable] = None
    capabilities: Optional[Callable] = None
    clear_inheritable: Optional[Callable] = None
    set_nnp: Optional[Callable] = None
    get_nnp: Optional[Callable] = None
    maxprocperuid: Optional[Callable] = None
    read_fd: Callable = _read_fd
    read_file: Callable = _read_file
    chdir: Callable = os.chdir
    list_fds: Callable = _list_fds
    close: Callable = os.close
    set_inheritable: Callable = os.set_inheritable
    write_fd: Callable = os.write
    access_x: Callable = _access_x
    dup2: Callable = os.dup2
    getpid: Callable = os.getpid
    write_file: Callable = _write_text   # the Linux cgroup join (exec_launch)
    read_text: Callable = _read_text


def real_os_calls() -> OsCalls:
    c = OsCalls()
    if sys.platform == "darwin":
        c.getgroups = _darwin_kernel_groups
        c.maxprocperuid = _darwin_maxprocperuid
    elif sys.platform.startswith("linux"):
        c.getresuid, c.getresgid, c.capabilities = os.getresuid, os.getresgid, _linux_caps
        c.clear_inheritable = _linux_clear_inheritable
        c.set_nnp, c.get_nnp = _linux_set_nnp, _linux_get_nnp
    return c


def _do(step: str, fn: Callable, *args) -> None:
    try:
        fn(*args)
    except OSError as e:
        raise Refused("drop-failed", f"{step}: {e.strerror or e}") from None


def _proved_fail(step: str, fn: Callable) -> None:
    try:
        fn(0)
    except OSError as e:
        if e.errno == errno.EPERM:
            return
        raise Refused("drop-assert", f"{step}(0) failed with errno {e.errno}, not EPERM") from None
    raise Refused("privilege-regained", f"{step}(0) succeeded after the drop")


def _nproc_target(nproc: int, c: OsCalls) -> int:
    """V3 "macOS min with kern.maxprocperuid": min(``nproc``, the hard limit, which an unprivileged process
    cannot raise, and on macOS ``kern.maxprocperuid``, which XNU clamps the set to without an error). A
    ``maxprocperuid`` that cannot be read, or reads as no positive int, is refused: never a guessed ceiling."""
    soft, hard = c.getrlimit(resource.RLIMIT_NPROC)
    lim = nproc if hard == resource.RLIM_INFINITY or hard >= nproc else hard
    if c.maxprocperuid is not None:
        try:
            per_uid = c.maxprocperuid()
        except OSError as e:
            raise Refused("drop-assert", f"kern.maxprocperuid unreadable: {e.strerror or e}") from None
        if not _is_int(per_uid) or per_uid <= 0:
            raise Refused("drop-assert", f"kern.maxprocperuid reads {per_uid!r}")
        lim = min(lim, per_uid)
    return lim


def drop_and_assert(uid: int, gid: int, groups: Iterable[int], *, nproc: Optional[int] = NPROC,
                    os_calls: Optional[OsCalls] = None, clear_inheritable: bool = False) -> None:
    """In the child, before its exec (§8.2, plan §2e 1 + §2f 8): ``setgroups`` -> ``setgid`` -> ``setuid``, each
    checked; then ASSERT real and effective uid and gid are the target's (and, on Linux, the saved ids, and
    no permitted, effective or ambient capability left), the supplementary groups are EXACTLY ``groups``, the uid is not 0; PROVE
    ``seteuid(0)`` and ``setegid(0)`` fail (EPERM); then ``RLIMIT_NPROC`` (``None``: not set, the front).
    Raises :class:`Refused` at the first step that is not proven; the caller then exits without exec.

    The groups are SET to an explicit list, never cleared (``setpriv --clear-groups``): with an empty list a
    root-launched process ran the 0750 ``levain-slots`` bwrap copy it should not reach, mechanism undiagnosed
    (s1-1d D12, R-L2 control B). An explicit set is what R-L1's view runs used (``setpriv --groups``), and on
    macOS ``setgroups`` also opts the credential out of the directory service's dynamic membership, so the
    kernel list is the whole truth the assertion reads [not run: XNU's documented setgroups semantics; a 2g
    row reads ``id`` and a group-only file as the dropped slot].

    ``clear_inheritable`` (an export target, s5 §5b): bwrap run by root with ``--cap-add`` sets the inheritable
    set to the added caps as well as permitted and effective, and ``setuid`` away from 0 clears only permitted,
    effective and ambient [read in bubblewrap's ``drop_all_caps``; not run, a 5g row]; so on Linux the
    inheritable set is cleared first and asserted empty after the drop. A slot target is not asked to: its bwrap
    runs with no ``--cap-*``, so the stage keeps whatever its root parent held, as before.
    The bounding set keeps the added caps: lowering it needs ``CAP_SETPCAP``, which the stage was never given,
    and ``no_new_privs`` is the guard that keeps an exec from using it."""
    c = os_calls or real_os_calls()
    groups = tuple(groups)
    if not (_is_int(uid) and _is_int(gid)) or uid == 0 or gid == 0 or 0 in groups:
        raise Refused("drop-to-root", f"uid {uid!r} gid {gid!r} groups {list(groups)!r}")
    if gid not in groups or len(set(groups)) != len(groups):
        raise Refused("drop-assert", f"groups {list(groups)!r} must hold gid {gid} once")
    if clear_inheritable and c.clear_inheritable is not None:
        _do("capset", c.clear_inheritable)
    _do("setgroups", c.setgroups, list(groups))
    _do("setgid", c.setgid, gid)
    _do("setuid", c.setuid, uid)
    got = (c.getuid(), c.geteuid(), c.getgid(), c.getegid())
    if got != (uid, uid, gid, gid):
        raise Refused("drop-assert", f"real/effective uid, gid = {got}, want {(uid, uid, gid, gid)}")
    if c.getresuid is not None and tuple(c.getresuid()) != (uid, uid, uid):
        raise Refused("drop-assert", f"saved uid: {tuple(c.getresuid())}")
    if c.getresgid is not None and tuple(c.getresgid()) != (gid, gid, gid):
        raise Refused("drop-assert", f"saved gid: {tuple(c.getresgid())}")
    have = list(c.getgroups())
    if sorted(have) != sorted(groups):
        raise Refused("drop-assert", f"groups {sorted(have)}, want exactly {sorted(groups)}")
    if c.capabilities is not None:
        caps = c.capabilities()
        if any(caps.get(k, 1) for k in ("CapPrm", "CapEff", "CapAmb") + (("CapInh",) if clear_inheritable else ())):
            raise Refused("drop-assert", f"capabilities left after the drop: {caps}")
    _proved_fail("seteuid", c.seteuid)
    _proved_fail("setegid", c.setegid)
    if nproc is not None:
        lim = _nproc_target(nproc, c)
        _do("setrlimit", c.setrlimit, resource.RLIMIT_NPROC, (lim, lim))
        if tuple(c.getrlimit(resource.RLIMIT_NPROC)) != (lim, lim):
            raise Refused("drop-assert", f"RLIMIT_NPROC is {c.getrlimit(resource.RLIMIT_NPROC)}, want {(lim, lim)}")


def parse_env(data: bytes) -> dict:
    """The rendered env file (§3.4), R-M2's shape: UTF-8, one ``KEY=VALUE`` per LF-terminated line, the value
    everything after the first ``=``, no quoting. No CR, no NUL, no blank line, no repeated key. Errors name
    the line, never a value (the file holds no secret, but the habit costs nothing)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("env-malformed", "not UTF-8") from None
    if "\x00" in text or "\r" in text or (text and not text.endswith("\n")):
        raise Refused("env-malformed", "NUL, CR or an unterminated last line")
    env: dict = {}
    for n, line in enumerate(text.split("\n")[:-1], 1):
        key, sep, value = line.partition("=")
        if not sep or not _ENV_KEY.fullmatch(key):
            raise Refused("env-malformed", f"line {n}")
        if key in env:
            raise Refused("env-malformed", f"line {n}: {key} repeated")
        env[key] = value
    return env


def _resolve(name: str, env: Mapping[str, str], c: OsCalls) -> str:
    """A verifier step's ``argv[0]`` on the session env's PATH, looked up AS THE CHILD (after the drop). A
    name with a ``/`` is taken as written (relative to the cwd, ``<S>/repo``)."""
    if "/" in name:
        return name
    for d in env.get("PATH", "").split(":"):
        if d.startswith("/") and c.access_x(f"{d.rstrip('/')}/{name}"):
            return f"{d.rstrip('/')}/{name}"
    raise Refused("target-not-found", f"{name!r} on the session PATH")


def _close_fds(keep: frozenset, c: OsCalls) -> None:
    """§3.1 step 4: every fd off the allowlist is closed (Python's own are CLOEXEC; an inherited one may not
    be), the kept ones made inheritable, and the result re-listed: an fd still open off the list = refused."""
    for fd in c.list_fds():
        if fd not in keep:
            c.close(fd)
    for fd in sorted(keep):
        c.set_inheritable(fd, True)
    left = sorted(set(c.list_fds()) - keep)
    if left:
        raise Refused("fd-off-list", f"still open: {left}")


def set_no_new_privs(c: OsCalls) -> None:
    """Linux: ``prctl(PR_SET_NO_NEW_PRIVS, 1)`` after the drop, then PROVE it reads back 1, so
    no setuid binary in the view (``/usr`` and ``/etc`` are plain ``--ro-bind``) runs with more than the target
    holds. A no-op where the host has none (macOS)."""
    if c.set_nnp is None:
        return
    _do("no_new_privs", c.set_nnp)
    if c.get_nnp() != 1:
        raise Refused("drop-assert", "no_new_privs does not read back 1")


def trampoline_argv(exec_fd: int, argv: Sequence[str]) -> Tuple[str, ...]:
    """The macOS vendor's target under ``sandbox-exec``: this module's :func:`trampoline_main` from the root
    import tree, then the vendor's own argv after ``--``."""
    return module_argv("darwin", "levain.slot.wall", "--exec-fd", str(exec_fd), "--", *argv)


def stage_main(spec_fd: int = SPEC_FD, os_calls: Optional[OsCalls] = None, *, err=None) -> int:
    """The stage, in order: spec -> session keyring (Linux export) -> drop + assert (+ ``RLIMIT_NPROC``, +
    ``no_new_privs`` on Linux) -> the env file -> the profile guard -> cwd -> the target's path -> the fd allowlist -> exec (macOS slot targets:
    ``sandbox-exec -f <profile>``). The vendor's exec marker (:data:`EXEC_FD`): with no profile the stage is the
    last hop, so it writes :data:`EXEC_OK` just before the execve; under a profile the trampoline writes it, so
    ``+`` means the profile applied. Returns :data:`STAGE_EXIT` on any refusal or error, having exec'd nothing
    (and written :data:`EXEC_REFUSED`, after a ``+`` too); a real ``execve`` never returns."""
    c = os_calls or real_os_calls()
    spec = None
    try:
        spec = parse_spec(c.read_fd(spec_fd, SPEC_CAP))
        if spec.target in EXPORT_TARGETS and sys.platform.startswith("linux"):
            # s5 §5b A6 (head ruling 1022+19 Q2): a fresh session keyring, joined while still root and proved,
            # between the spec and the drop. The real keyctl only on the real path (no os_calls); an injected
            # OsCalls carries its own ``keyctl`` or the join refuses: no path skips it.
            from levain.slot.keyring_join import join_session_keyring, real_keyctl
            join_session_keyring(real_keyctl if os_calls is None else getattr(c, "keyctl", None))
        drop_and_assert(spec.uid, spec.gid, spec.groups, nproc=spec.nproc, os_calls=c,
                        clear_inheritable=spec.target in EXPORT_TARGETS)
        set_no_new_privs(c)
        env = parse_env(c.read_file(spec.env_file, ENV_CAP)) if spec.env_file else {}
        for k, v in spec.extra_env:
            if k in env:
                raise Refused("env-conflict", f"{k} is the rendered env's")
            env[k] = v
        argv = list(spec.argv)
        if spec.profile is not None:
            if sys.platform != "darwin":
                raise Refused("spec-malformed", "a Seatbelt profile on a host that is not macOS")
            try:
                text = c.read_file(spec.profile, PROFILE_CAP).decode("utf-8")
            except UnicodeDecodeError:
                raise Refused("profile-unreadable", "not UTF-8") from None
            check_profile_text(text)
        c.chdir(spec.cwd)
        path = _resolve(argv[0], env, c) if spec.resolve else argv[0]
        if spec.profile is not None:
            target = [path] + argv[1:]
            if spec.exec_fd is not None:      # the marker is the trampoline's, written under the profile
                target = list(trampoline_argv(spec.exec_fd, target))
            path, argv = SANDBOX_EXEC, [SANDBOX_EXEC, "-f", spec.profile] + target
        exec_keep = frozenset() if spec.exec_fd is None else frozenset({spec.exec_fd})
        _close_fds(frozenset({0, 1, 2}) | frozenset(spec.keep_fds) | exec_keep, c)
        if spec.exec_fd is not None and spec.profile is None:
            c.set_inheritable(spec.exec_fd, False)     # the vendor never holds it
            c.write_fd(spec.exec_fd, EXEC_OK)          # the last act before the execve
        os.execve(path, argv, env)
    except Refused as e:
        _say(err, f"levain wall: refused {e.name}" + (f": {e.detail}" if e.detail else ""))
        _exec_failed(spec, c)
        return STAGE_EXIT
    except Exception as e:   # anything unforeseen: still no exec. The type and errno only: never a value.
        code = getattr(e, "errno", None)
        _say(err, f"levain wall: refused stage-error: {type(e).__name__}" + (f" errno {code}" if code else ""))
        _exec_failed(spec, c)
        return STAGE_EXIT
    _exec_failed(spec, c)
    return STAGE_EXIT


def _exec_failed(spec: Optional[Spec], c: OsCalls) -> None:
    """:data:`EXEC_REFUSED` on the exec pipe (after a ``+`` too: an execve that failed). A spec that never parsed
    names no pipe; the stage's exit then closes it with no byte, which the helper reads as a failed start."""
    if spec is not None and spec.exec_fd is not None:
        try:
            c.write_fd(spec.exec_fd, EXEC_REFUSED)
        except OSError:
            pass


def _say(err, line: str) -> None:
    try:
        (err or sys.stderr).write(line + "\n")
        (err or sys.stderr).flush()
    except Exception:
        pass


def exec_launch(argv: Sequence[str], spec_read_fd: int, *, cgroup: Optional[str] = None,
                os_calls: Optional[OsCalls] = None) -> None:
    """The forked root child's half (the runtime helper calls it after ``fork``): put the spec pipe's read end
    on :data:`SPEC_FD`; on Linux join ``cgroup`` and PROVE it (``/proc/self/cgroup``) — the cgroup is the
    kill domain END and §5.6 rely on, so a process outside it is never started; then exec the launch argv
    with an empty env (every hop reads its own: the stage parses the env file). Raises on any failure; the
    child then exits without exec. The parent writes :meth:`Spec.to_json` to the pipe's write end and
    closes it; it never holds the lease fd (§3.1)."""
    c = os_calls or real_os_calls()
    if spec_read_fd != SPEC_FD:
        c.dup2(spec_read_fd, SPEC_FD)
        c.close(spec_read_fd)
    c.set_inheritable(SPEC_FD, True)
    if cgroup is not None:
        c.write_file(f"{cgroup}/cgroup.procs", f"{c.getpid()}\n")
        want = "0::" + cgroup[len("/sys/fs/cgroup"):]
        if want not in c.read_text("/proc/self/cgroup").splitlines():
            raise Refused("cgroup-not-joined", cgroup)
    os.execve(argv[0], list(argv), {})


def trampoline_main(argv: Sequence[str], *, write: Callable = os.write, set_inheritable: Callable = os.set_inheritable,
                    environ: Optional[Mapping[bytes, bytes]] = None, err=None) -> int:
    """macOS, the vendor's last hop (``--exec-fd 4 -- <vendor argv>``), run by ``sandbox-exec`` AFTER it applied
    the profile, as the dropped slot: :data:`EXEC_FD` close-on-exec -> :data:`EXEC_OK` -> execve the vendor with
    the env it was given (the stage's). A failed execve after the marker writes :data:`EXEC_REFUSED`. A ``+``
    that cannot be written (the helper is gone) execs nothing. Returns :data:`STAGE_EXIT`; never returns on an
    exec."""
    if len(argv) < 4 or argv[0] != "--exec-fd" or argv[1] != str(EXEC_FD) or argv[2] != "--" \
            or not _abs_path(argv[3]):
        _say(err, "levain wall: refused bad-token: usage: --exec-fd 4 -- /abs/vendor [args]")
        if list(argv[:2]) == ["--exec-fd", str(EXEC_FD)]:   # only a pipe the argv names gets the byte
            try:
                write(EXEC_FD, EXEC_REFUSED)
            except OSError:
                pass
        return STAGE_EXIT
    target = [os.fsencode(a) for a in argv[3:]]
    try:
        set_inheritable(EXEC_FD, False)
        write(EXEC_FD, EXEC_OK)
    except OSError as e:
        _say(err, f"levain wall: refused stage-error: exec marker errno {e.errno}")
        return STAGE_EXIT
    try:
        os.execve(target[0], target, os.environb if environ is None else environ)
    except OSError as e:
        _say(err, f"levain wall: refused exec-failed: errno {e.errno}")
    try:
        write(EXEC_FD, EXEC_REFUSED)
    except OSError:
        pass
    return STAGE_EXIT


def main(argv: Sequence[str]) -> int:
    if argv[:1] == ["--exec-fd"]:
        return trampoline_main(argv)
    if len(argv) != 2 or argv[0] != "--spec-fd" or not re.fullmatch(r"[3-9]|[1-9][0-9]{1,3}", argv[1]):
        _say(None, "levain wall: refused bad-token: usage: --spec-fd N")
        return STAGE_EXIT
    return stage_main(int(argv[1]))


if __name__ == "__main__":
    os._exit(main(sys.argv[1:]))


__all__ = ["EXEC_FD", "EXEC_OK", "EXEC_REFUSED", "EXPORT_ACCOUNT", "EXPORT_CAPS", "EXPORT_DEVICES", "EXPORT_ENTRY",
           "EXPORT_GROUPS", "EXPORT_MACH_ALLOW", "EXPORT_NPROC", "EXPORT_READ_DENIED", "EXPORT_TARGETS",
           "EXPORT_WRITABLE", "FORWARDER_PORT", "HASH_OBJECT_PREFIX", "RESULT_FD", "VERIFY_WRITABLE", "WallFacts",
           "LinuxLaunch", "NPROC", "OsCalls", "Principal", "READY_FD", "SPEC_FD", "SPEC_FORMAT", "STAGE_EXIT",
           "Spec", "TARGETS", "cgroup_dir", "check_egress_ready", "check_pf_enabled", "check_profile_text",
           "clone_argv",
           "drop_and_assert", "exec_launch", "expected_slot_rules", "export_launch_spec", "export_linux_launch_argv",
           "export_principal", "export_wall_facts", "hash_object_argv", "importer_view_args", "launch_spec",
           "linux_launch_argv", "linux_view_args", "ls_files_argv", "macos_egress_ready", "macos_launch_argv",
           "verifier_view_args", "wall_view_args",
           "module_argv", "parse_env", "parse_spec", "pf_printed", "principal", "read_host_links", "read_netns_names",
           "real_os_calls", "require_netns",
           "set_no_new_privs", "stage_argv", "stage_main", "target_account", "trampoline_argv", "trampoline_main"]
