"""levain.firing.hands — a dedicated, unprivileged OS user for an entity's bash hand (M2).

WHY A SECOND USER. On macOS the kernel hands the full argv and ENVIRONMENT of every process to any
other process with the same uid (``KERN_PROCARGS2``; the only gate in xnu's
``is_procargs_content_read_permitted`` is the caller's uid). No Seatbelt rule closes it: a
``sysctl-read`` deny by name, by prefix, or in total was measured inert on macOS 26.6.2 (2026-10-07,
in a VM). A process running as a different, unprivileged user is refused that read by the kernel
(``EINVAL``), measured the same day with levain's shipped profile unchanged. The same uid change puts
the operator's ``0600`` files and the operator's home directory (``0750``, group ``staff``) out of
reach by ownership alone.

WHAT THIS MODULE DOES. It builds and runs the one-time, root-only setup (``sudo levain
setup-isolation``) and its reverse (``--undo``). The plan is computed as data first
(:func:`plan_setup`, :func:`plan_undo`), so tests check it without root and ``--dry-run`` prints it.
Nothing here starts the entity's bash; this module only creates and removes the user.

The hands user:
  - is named ``_levain_<slug>_<hash>`` from the entity's resolved path, so two entities never share
    one, and a name that does not match :data:`HANDS_USER_RE` is never acted on;
  - has no password, no login shell, a disabled account (macOS), is hidden from the login window
    (macOS), and its primary group is its own group, never ``staff`` or a shared users group. Being
    outside ``staff`` is what keeps it out of the operator's home directory;
  - gets an id that no Levain hands user has had before (a retired id is never reused), taken from
    the top of the range, away from the ids macOS updates have claimed for system daemons;
  - is refused remote login (an sshd ``DenyUsers`` drop-in) and, where the system keeps a deny list,
    cron and at.

Its workspace is NOT ``<entity>/workspace``: that sits under the operator's home, which the hands
user cannot enter. Setup creates ``/Users/Shared/levain/<hands>/workspace`` (macOS) or
``/var/lib/levain/<hands>/workspace`` (Linux) and records the path in ``confinement.json`` (a
recorded path, never a symlink). The workspace and everything in it belong to the hands user. The
operator gets an inherited ACL that allows reading and listing only, never writing, creating or
deleting (Phill, 2026-10-07, ruling A: the operator edits the entity's workspace through the
entity). The reason: git's ownership check for a BARE repository looks only at that directory, so
any operator-owned directory the hands user could write would be one the operator's git trusts and
the entity could fill with a repository whose config runs code as the operator. With nothing in the
workspace owned by the operator, and nothing there the operator can write, there is no such
directory. The hands user owns every directory and could still open one up to the operator, so
``levain doctor`` fails on any entry the operator can write. The operator's other ways in are the
remote, ``levain ws-git``, ``levain ws-put`` (a file, as data) and ``levain ws-adopt`` (an import).

The privilege it gives the operator is one sudoers line, ``<operator> ALL=(<hands>) NOPASSWD: ALL``:
the operator may run commands AS the hands user. That is no escalation for the operator (the hands
user can do strictly less), and there is no rule FOR the hands user, which has no password, so sudo
from inside the hands always fails.

Root runs this command, from the operator's own Python environment; that environment is already
trusted with root by the act of typing ``sudo levain``. Every program this module runs as root is
given by absolute path or resolved on a root-owned PATH (:data:`SECURE_PATH`), never the operator's.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from levain.launch import child_env

HostOS = Literal["darwin", "linux"]

#: Every name this module creates or deletes matches this; anything else is refused.
HANDS_USER_RE = re.compile(r"^_levain_[a-z0-9]{1,12}_[0-9a-f]{6}$")

#: An operator name that cannot change meaning inside a sudoers line or an ACL spec.
OPERATOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

#: Stamped on the user record (macOS RealName, Linux GECOS) so ``--undo`` deletes only a user this
#: command created, even if a name collides.
HANDS_MARKER = "Levain hands user"

#: What undo leaves: the account, disabled in place, so its id stays reserved in the directory
#: service the OS allocates from (Phill's head, 2026-10-07, ruling (a): a tombstone, never a
#: deletion). Starts with :data:`HANDS_MARKER`, so it is still recognisably Levain's.
RETIRED_MARKER = {"darwin": "Levain hands user (retired)", "linux": "Levain hands user,retired"}

#: Where root resolves a bare program name: root-owned directories only.
SECURE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

#: Ids are taken from the top of these ranges downward, skipping any id in use or retired.
#: macOS: below 500 sorts with the system accounts; macOS 15 claimed 301-304 for new daemons and its
#: updater deletes accounts that collide (https://github.com/NixOS/nix/issues/10892), so the low end
#: is the dangerous end and is never reached.
_DARWIN_IDS = range(499, 399, -1)
#: Linux: the top of the usual system-account range (SYS_UID_MAX is 999 on Debian and Fedora).
_LINUX_IDS = range(999, 899, -1)

_SUDOERS_DIR = Path("/etc/sudoers.d")
_SSHD_DROPIN_DIR = Path("/etc/ssh/sshd_config.d")
_SSHD_CONFIG = Path("/etc/ssh/sshd_config")
_LINUX_HANDS_HOME_ROOT = Path("/var/lib/levain-hands")
WORKSPACE_ROOT = {"darwin": Path("/Users/Shared/levain"), "linux": Path("/var/lib/levain")}
_RETIRED_IDS = {"darwin": Path("/var/db/levain/retired-hands-ids"),
                "linux": Path("/var/lib/levain/retired-hands-ids")}
_AT_SPOOL = {"darwin": Path("/usr/lib/cron/jobs"), "linux": Path("/var/spool/cron/atjobs")}
_CRON_DENY = {"darwin": (Path("/usr/lib/cron/cron.deny"), Path("/usr/lib/cron/at.deny")),
              "linux": (Path("/etc/cron.deny"), Path("/etc/at.deny"))}

#: The inherited ACL that lets the operator read the hands user's workspace (macOS): list, search
#: and read, and nothing that writes, creates or deletes.
_DARWIN_OPERATOR_READ = "list,search,readattr,readextattr,readsecurity,read,file_inherit,directory_inherit"

#: Linux egress boundary (P-1 (a), Phill 2026-10-08): where the per-user nftables ruleset and the
#: systemd unit that loads it at boot live. Root-owned; only setup writes them, only undo removes them.
_EGRESS_DIR = Path("/etc/levain/egress")
_SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")


class HandsSetupError(RuntimeError):
    """setup-isolation cannot run here, or refused to act on something it did not create."""


# --- the Linux egress boundary --------------------------------------------------------------------
#
# WHY. P2b (2026-10-08, levain project_memory P2_PROBE_2026-10-08/RESULT.md, property 3) ran claude
# under its own sandbox and codex read-only as a hands user, both pointed at a rogue listener, and
# both reached it: a harness vendor's sandbox confines the harness's TOOL children, never the harness
# process. The boundary has to be the OS's, around the uid. On Linux that is an nftables output rule
# matching the socket owner (`meta skuid`): every IP socket the hands user opens is refused except
# loopback TCP to the recorded proxy ports. A Unix-socket proxy is outside netfilter and needs no
# rule. nftables sees IP sockets only: a local daemon the hands user can ask to connect for it (a
# resolver over D-Bus or varlink, say) is not covered by this rule.


def egress_table(hands_user: str) -> str:
    """The nftables table of ``hands_user``'s boundary, keyed by NAME (undo removes it without an id).
    nft identifiers start with a letter, so the leading ``_levain_`` becomes ``levain_``."""
    if not HANDS_USER_RE.match(hands_user):
        raise HandsSetupError(f"refusing {hands_user!r}: not a Levain hands user name")
    return "levain_" + hands_user[len("_levain_"):]


def hands_net_group(hands_user: str) -> str:
    """The group whose gid the boundary lets out (Linux, D: Phill 2026-10-09). Only network git that
    ``levain ws-git`` starts runs with it: the hands user is never a member and the group has no
    password, so no process of the hands user can take this gid by itself."""
    egress_table(hands_user)   # validates the name
    return hands_user + "_net"


def egress_rules_path(hands_user: str) -> Path:
    return _EGRESS_DIR / f"{egress_table(hands_user)}.nft"


def egress_unit_name(hands_user: str) -> str:
    return f"levain-egress-{egress_table(hands_user)[len('levain_'):].replace('_', '-')}.service"


def egress_unit_path(hands_user: str) -> Path:
    return _SYSTEMD_UNIT_DIR / egress_unit_name(hands_user)


def check_egress_ports(ports: object) -> tuple[int, ...]:
    """``ports`` as a sorted tuple of distinct TCP ports, or HandsSetupError."""
    if not isinstance(ports, (list, tuple)) or any(
            isinstance(p, bool) or not isinstance(p, int) or not 1 <= p <= 65535 for p in ports):
        raise HandsSetupError(f"egress ports must be TCP port numbers (1-65535), got {ports!r}")
    return tuple(sorted(set(ports)))


def egress_ruleset(hands_user: str, hands_id: int, ports: tuple[int, ...], net_gid: int | None = None) -> str:
    """The ruleset, loaded with ``nft -f`` as ONE transaction: the empty ``table`` line makes the
    ``delete`` succeed when the table is absent, so a load replaces an older ruleset atomically and
    there is no moment with no rule. Reject, not drop: a refused connect fails at once, by name."""
    table = egress_table(hands_user)
    ports = check_egress_ports(ports)
    allow: list[str] = []
    if ports:
        dports = "{ " + ", ".join(str(p) for p in ports) + " }"
        allow = [f"    meta skuid {hands_id} ip daddr 127.0.0.1 tcp dport {dports} accept",
                 f"    meta skuid {hands_id} ip6 daddr ::1 tcp dport {dports} accept"]
    if net_gid is not None:
        # The one way out: a socket opened with the net group as its gid (network git ws-git starts).
        allow.append(f"    meta skuid {hands_id} meta skgid {net_gid} accept")
    return "\n".join([
        "# Written by `levain setup-isolation`; removed by `levain setup-isolation --undo`.",
        f"# The network boundary of the hands user {hands_user} (uid {hands_id}).",
        f"table inet {table} {{}}",
        f"delete table inet {table}",
        f"table inet {table} {{",
        "  chain output {",
        "    type filter hook output priority filter; policy accept;",
        *allow,
        f"    meta skuid {hands_id} meta l4proto tcp reject with tcp reset",
        f"    meta skuid {hands_id} reject with icmpx admin-prohibited",
        "  }",
        "}",
        "",
    ])


def egress_unit_text(hands_user: str) -> str:
    """Loads the ruleset at boot, before the network is configured. No ExecStop: stopping the unit
    must not lift the boundary; only ``--undo`` removes the table."""
    return "\n".join([
        "# Written by `levain setup-isolation`; removed by `levain setup-isolation --undo`.",
        "[Unit]",
        f"Description=Levain network boundary for the hands user {hands_user}",
        "DefaultDependencies=no",
        "Before=network-pre.target",
        "Wants=network-pre.target",
        # nftables.service's own config starts with `flush ruleset` on Debian-family hosts: load after
        # it, and run again whenever it is restarted, or a boot or a restart of it deletes this table.
        "After=nftables.service local-fs.target",
        "PartOf=nftables.service",
        # PartOf carries stop and restart, never reload: a reload of nftables.service re-runs its
        # config (a flush on Debian-family hosts), so ours reloads with it (L3 r1, codex).
        "ReloadPropagatedFrom=nftables.service",
        "Before=sysinit.target",
        "",
        "[Service]",
        "Type=oneshot",
        "RemainAfterExit=yes",
        f"ExecStart={_abs('nft')} -f {egress_rules_path(hands_user)}",
        f"ExecReload={_abs('nft')} -f {egress_rules_path(hands_user)}",
        "",
        "[Install]",
        "WantedBy=sysinit.target nftables.service",
        "",
    ])


def egress_unavailable() -> str | None:
    """Why this Linux host cannot carry the boundary, or None. Setup refuses by this name rather
    than set up a hands user whose network nothing confines."""
    for tool, package in (("nft", "nftables"), ("systemctl", "systemd")):
        if shutil.which(tool, path=SECURE_PATH) is None:
            return (f"{package} is not installed (no `{tool}` on {SECURE_PATH}); the hands user's network "
                    f"boundary needs it. Install {package}, then run setup again")
    if not os.path.isdir("/run/systemd/system"):
        return ("systemd is not running as init here (no /run/systemd/system), so nothing would load the hands "
                "user's network boundary at boot")
    try:
        r = subprocess.run([_abs("nft"), "list", "tables"], capture_output=True, text=True, timeout=30,
                           env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"nftables could not be run ({exc}); the hands user's network boundary needs it"
    if r.returncode != 0:
        said = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
        return f"the kernel refused nftables ({said}); the hands user's network boundary needs it"
    return None


def _egress_table_loaded(hands_user: str) -> tuple[bool, str]:
    r = subprocess.run([_abs("nft"), "list", "table", "inet", egress_table(hands_user)],
                       capture_output=True, text=True, env=child_env())
    return (True, "") if r.returncode == 0 else (False, "the boundary's table is not loaded: "
                                                 + ((r.stderr or r.stdout).strip() or f"exit {r.returncode}"))


def _egress_table_absent(hands_user: str) -> tuple[bool, str]:
    r = subprocess.run([_abs("nft"), "delete", "table", "inet", egress_table(hands_user)],
                       capture_output=True, text=True, env={**child_env(), "LC_ALL": "C"})
    said = (r.stderr or r.stdout).strip()
    # Only the kernel saying there is no such table is "already gone"; any other failure is a failure.
    return (True, "") if r.returncode == 0 or "No such file or directory" in said else (False, said)


_EGRESS_PROBE = (
    "import socket, sys\n"
    "s = socket.socket()\n"
    "s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('127.0.0.1', int(sys.argv[1])))\n"
    "    print('connected')\n"
    "except OSError as e:\n"
    "    print('refused', e.errno)\n"
)


_ECONNREFUSED = 111   # Linux; what a connect gets from `reject with tcp reset`


def _probe_connect(hands_user: str, port: int, group: str | None, timeout: float) -> tuple[str, str]:
    """(what the hands user's connect to 127.0.0.1:``port`` printed, stderr), run with ``group`` as its
    gid when given."""
    r = subprocess.run(["/usr/bin/sudo", "-n", "-u", hands_user, *(("-g", group) if group else ()), "/usr/bin/env", "-i",
                        f"PATH={SECURE_PATH}", _abs("python3"), "-I", "-c", _EGRESS_PROBE, str(port)],
                       capture_output=True, text=True, timeout=timeout, cwd="/", env=child_env())
    return (r.stdout or "").strip(), (r.stderr or "").strip() or f"exit {r.returncode}"


def egress_boundary_problem(hands_user: str, ports: tuple[int, ...] = (), *, net_group: str | None = None,
                            timeout: float = 15.0) -> str | None:
    """Does the hands user's network boundary hold, asked of the kernel rather than the ruleset (which
    only root can list)? None when it holds, else what is wrong. Run by the operator, who may run
    commands as the hands user (the sudoers rule setup wrote).

    The operator listens on a loopback port the boundary does not allow, and the hands user tries to
    connect to it. It holds when that connect is refused with ECONNREFUSED (the rule's reset; a timeout
    or another error is some other mechanism and proves nothing), nothing from the hands user reached
    the listener, AND the operator's own connect then lands (a dead listener must not read as a
    boundary). With ``net_group``, the same connect made with that gid must land too: the boundary's
    one way out works. A probe that cannot run is a problem, never a pass."""
    import socket

    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        for _ in range(64):                          # never probe a port the boundary allows
            if listener.getsockname()[1] not in ports:
                break
            listener.close()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
        if listener.getsockname()[1] in ports:
            return "the probe found no loopback port the boundary refuses (are all ports allowed?)"
        listener.listen(4)
        listener.settimeout(3)
        port = listener.getsockname()[1]

        def landed() -> bool:
            try:
                listener.accept()[0].close()
                return True
            except OSError:
                return False

        try:
            said, err = _probe_connect(hands_user, port, None, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"the probe could not run as {hands_user} ({exc})"
        listener.settimeout(0.2)
        if said == "connected" or landed():
            return (f"{hands_user} connected to a loopback port its boundary does not allow: nothing confines "
                    "its network (the nftables rule is not loaded)")
        if said != f"refused {_ECONNREFUSED}":
            return f"the probe as {hands_user} did not get the boundary's refusal ({said or err})"
        listener.settimeout(3)
        socket.create_connection(("127.0.0.1", port), timeout=3).close()
        if not landed():
            return "the probe's own listener did not answer, so the refusal proves nothing"
        if net_group is not None:
            try:
                said, err = _probe_connect(hands_user, port, net_group, timeout)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"the probe could not run as {hands_user} with {net_group} ({exc})"
            if said != "connected" or not landed():
                return (f"{hands_user} with the gid of {net_group} could not connect ({said or err}): network git "
                        "(levain ws-git push/fetch) would be refused too")
        return None
    except OSError as exc:
        return f"the probe failed ({exc})"
    finally:
        listener.close()


def egress_drift_problem(hands_user: str, hands_id: int, ports: tuple[int, ...]) -> str | None:
    """Did levain's own steps converge: the ruleset on disk (what boot and every reload load) says exactly
    what the record says, and the unit that loads it at boot is enabled? A repair that stopped part way,
    or two that raced, can leave MORE allowed than recorded or the unit not enabled while the table is
    still loaded, and the probe tests only the live kernel and one refused port (L3 r2, r5). It does not
    try to prove the boot path against root: units, drop-ins and the firewall are root's, and a root
    rewrite lifts the boundary until it is re-asserted (Phill 2026-10-09, ruling (A)).

    The live check before a launch is the CALLER's: a caller starting a hands process on Linux calls
    :func:`egress_boundary_problem` and :func:`net_group_problem` first and refuses on a problem."""
    import grp

    try:
        net_gid = grp.getgrnam(hands_net_group(hands_user)).gr_gid
        rules = egress_rules_path(hands_user).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeError) as exc:
        return f"the boundary's ruleset or group cannot be read ({exc})"
    if rules != egress_ruleset(hands_user, hands_id, ports, net_gid):
        return (f"the ruleset in {egress_rules_path(hands_user)} is not the one the record describes (allowed ports "
                f"{list(ports) or 'none'}): a repair stopped part way, or the file was edited")
    try:
        r = subprocess.run([_abs("systemctl"), "is-enabled", egress_unit_name(hands_user)], capture_output=True,
                           text=True, timeout=20, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"systemd could not say whether the boundary loads at boot ({exc})"
    if r.stdout.strip() != "enabled":
        return (f"{egress_unit_name(hands_user)} is {r.stdout.strip() or 'unknown'}, not enabled: the next boot "
                "would not load the boundary (run setup again)")
    return None


def _egress_dirs() -> tuple[bool, str]:
    """/etc/levain and /etc/levain/egress, root's, 0755 (doctor reads the ruleset as the operator), made
    in-process: no program found on a PATH runs as root for it (L3 r3, r4)."""
    for d in (_EGRESS_DIR.parent, _EGRESS_DIR):
        os.makedirs(d, mode=0o755, exist_ok=True)
        st = os.lstat(d)
        if not os.path.isdir(d) or os.path.islink(d):
            return False, f"{d} is not a directory"
        if st.st_uid != 0:
            return False, f"{d} exists and is not root's; refusing to put the boundary in it"
        os.chmod(d, 0o755)
    return True, ""


def egress_steps(hands_user: str, hands_id: int, ports: tuple[int, ...], net_gid: int | None = None) -> list[Step]:
    """Install (or replace) the boundary: idempotent, so setup and a repair run use the same steps.
    The ruleset is checked by the kernel (``nft -c``) before it is installed; ``restart`` re-runs the
    unit even when it was already active, which is what loads a changed ruleset."""
    systemctl, unit = _abs("systemctl"), egress_unit_name(hands_user)
    return [
        Step("make its directories root's and readable by all (doctor reads the ruleset as you)", call=_egress_dirs),
        Step("write the hands user's network boundary (nftables, checked with nft -c)",
             write=(egress_rules_path(hands_user), egress_ruleset(hands_user, hands_id, ports, net_gid), 0o644),
             validate=(_abs("nft"), "-c", "-f")),
        Step("load it at every boot, before the network (systemd unit)",
             write=(egress_unit_path(hands_user), egress_unit_text(hands_user), 0o644)),
        Step("tell systemd about the unit", (systemctl, "daemon-reload")),
        Step("enable it", (systemctl, "enable", unit)),
        Step("load the boundary now", (systemctl, "restart", unit)),
        Step("check the boundary is loaded", call=lambda: _egress_table_loaded(hands_user)),
    ]


def _net_group_retired_and_gone(hands_user: str) -> tuple[bool, str]:
    """Retire the net group's gid (never given out again: a rule left behind by a failed undo must
    never let a later group out), then delete the group. Absent is done."""
    import grp

    name = hands_net_group(hands_user)
    try:
        gid = grp.getgrnam(name).gr_gid
    except KeyError:
        return True, ""
    ok, why = _ensure_line(_RETIRED_IDS["linux"], str(gid), present=True, create=True)
    if not ok:
        return False, why
    r = subprocess.run([_abs("groupdel"), name], capture_output=True, text=True, env=child_env())
    return (r.returncode == 0, (r.stderr or r.stdout).strip())


def net_group_problem(hands_user: str) -> str | None:
    """Why a process of the hands user could get past the boundary by taking another id, or None: the
    hands user must not be a member of its net group (a member takes the gid with ``sg`` or ``newgrp``,
    no password), nor have a subordinate id range."""
    import grp

    name = hands_net_group(hands_user)
    try:
        g = grp.getgrnam(name)
    except KeyError:
        return f"the group {name} that network git runs with does not exist"
    if hands_user in g.gr_mem:
        return f"{hands_user} is a member of {name}, so any of its processes can take the gid the boundary lets out"
    try:
        if pwd.getpwnam(hands_user).pw_gid == g.gr_gid:
            return f"{name} is {hands_user}'s primary group, so every one of its processes is let out"
    except KeyError:
        pass
    # A subordinate id range lets a process map itself to other ids (newuidmap), and the rule matches
    # the hands user's own uid: setup never gives one (useradd --system), so one here was added since.
    try:
        owners = {hands_user, str(pwd.getpwnam(hands_user).pw_uid)}   # subuid(5): a name or a numeric uid
    except KeyError:
        owners = {hands_user}
    for db in (Path("/etc/subuid"), Path("/etc/subgid")):
        try:
            text = db.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"{db} cannot be read ({exc.strerror}), so a subordinate id range for {hands_user} cannot be ruled out"
        if any(ln.split(":", 1)[0] in owners for ln in text.splitlines()):
            return f"{hands_user} has a range in {db}, so its processes can take ids the boundary does not match"
    return _subid_nss_problem(hands_user)


def _subid_nss_problem(hands_user: str) -> str | None:
    """subuid(5): ranges can come from an NSS provider instead of the files (``subid:`` in
    nsswitch.conf). Then ask the provider through ``getsubids`` (uid and gid ranges); a provider that
    cannot be asked fails closed (L3 r2, codex)."""
    try:
        conf = Path("/etc/nsswitch.conf").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"/etc/nsswitch.conf cannot be read ({exc.strerror}), so a subordinate id provider cannot be ruled out"
    lines = (ln.split("#", 1)[0].strip() for ln in conf.splitlines())
    providers = [ln.split(":", 1)[1].split() for ln in lines if ln.startswith("subid:")]
    if not providers or all(p in ("files",) for p in providers[-1]):
        return None
    getsubids = shutil.which("getsubids", path=SECURE_PATH)
    if getsubids is None:
        return (f"nsswitch.conf names a subordinate id provider ({' '.join(providers[-1])}) and getsubids is not "
                f"installed, so a range for {hands_user} cannot be ruled out")
    for flag in ((), ("-g",)):
        try:
            r = subprocess.run([getsubids, *flag, hands_user], capture_output=True, text=True, timeout=20,
                               env=child_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"getsubids could not answer for {hands_user} ({exc}), so a range cannot be ruled out"
        # getsubids exits non-zero when the provider could not be asked; only a clean, empty answer is "none".
        if r.returncode != 0:
            return (f"getsubids {' '.join(flag)} {hands_user} failed ({(r.stderr or '').strip() or f'exit {r.returncode}'}), "
                    "so a range cannot be ruled out")
        if r.stdout.strip():
            return f"{hands_user} has a subordinate id range from {' '.join(providers[-1])}: {r.stdout.strip()}"
    return None


def egress_undo_steps(hands_user: str) -> list[Step]:
    """Remove the boundary. Run only after every hands process is stopped, so nothing of the hands
    user runs with its network open."""
    systemctl, unit, unit_path = _abs("systemctl"), egress_unit_name(hands_user), egress_unit_path(hands_user)
    return [
        Step("stop loading the network boundary at boot", (systemctl, "disable", unit),
             skip_if=(_abs("test"), "!", "-e", str(unit_path))),
        Step("remove its systemd unit", ("/bin/rm", "-f", str(unit_path))),
        Step("tell systemd", (systemctl, "daemon-reload")),
        Step("remove its ruleset file", ("/bin/rm", "-f", str(egress_rules_path(hands_user)))),
        Step("remove the boundary's table", call=lambda: _egress_table_absent(hands_user)),
        Step("retire and remove the group network git ran with", call=lambda: _net_group_retired_and_gone(hands_user)),
    ]


def hands_user_name(entity_dir: Path | str) -> str:
    """The hands user's name for this entity: a slug of the directory name plus a hash of the
    resolved path, so two entities with the same directory name still get different users."""
    resolved = Path(entity_dir).expanduser().resolve()
    slug = re.sub(r"[^a-z0-9]", "", resolved.name.lower())[:12] or "entity"
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
    name = f"_levain_{slug}_{digest}"
    assert HANDS_USER_RE.match(name), name
    return name


def hands_workspace(host: HostOS, hands_user: str) -> Path:
    return WORKSPACE_ROOT[host] / hands_user / "workspace"


def sudoers_path(hands_user: str) -> Path:
    # sudo skips files in sudoers.d whose name contains a "." or ends in "~"; this name has neither.
    return _SUDOERS_DIR / f"levain-{hands_user}"


def sshd_dropin_path(hands_user: str) -> Path:
    # ".conf": Debian/Ubuntu include only sshd_config.d/*.conf.
    return _SSHD_DROPIN_DIR / f"levain-{hands_user}.conf"


def sudoers_text(operator: str, hands_user: str, net_group: str | None = None) -> str:
    return (
        f"# Written by `levain setup-isolation`; removed by `levain setup-isolation --undo`.\n"
        f"# {operator} may run commands as the Levain hands user {hands_user}, and nothing else.\n"
        f"Defaults>{hands_user} !requiretty\n"
        f"Defaults>{hands_user} env_reset\n"
        # levain hands bash a socket on stdin and reads its state back over it; I/O logging and a
        # pseudo-terminal would put sudo's own pipe or pty there instead (S2 L2b L3).
        f"Defaults>{hands_user} !log_input, !log_output, !use_pty\n"
        # Linux: the runas group lets the operator's `levain ws-git` start network git with the net
        # group's gid, the one gid the egress boundary lets out.
        + (f"{operator} ALL=({hands_user} : {net_group}) NOPASSWD: ALL\n" if net_group
           else f"{operator} ALL=({hands_user}) NOPASSWD: ALL\n")
    )


def _abs(name: str) -> str:
    """A program for root to run, resolved on :data:`SECURE_PATH` (the bare name if not found, so a
    plan built on a host without the tool still renders; running it then fails)."""
    return shutil.which(name, path=SECURE_PATH) or name


@dataclass(frozen=True)
class Step:
    """One action, as data: ``argv``, ``write`` or ``call``.

    ``skip_if`` is an argv run first; exit 0 means the step's effect is already present and the step
    is skipped. ``allow_fail`` marks a best-effort step whose failure does not stop the plan; it is
    used only where nothing later depends on the step having worked. With ``write``, ``call`` is a
    check run after the file is in place (removed again if the check fails)."""

    why: str
    argv: tuple[str, ...] = ()
    write: tuple[Path, str, int] | None = None   # (path, content, mode), owned root
    validate: tuple[str, ...] = ()               # run on the temp copy of ``write`` before install
    call: Callable[[], tuple[bool, str]] | None = None
    skip_if: tuple[str, ...] = ()
    allow_fail: bool = False


@dataclass(frozen=True)
class Plan:
    host: HostOS
    operator: str
    hands_user: str
    hands_id: int
    entity_dir: Path
    workspace: Path
    steps: tuple[Step, ...] = field(default_factory=tuple)


def choose_id(host: HostOS, used: set[int], retired: set[int]) -> int:
    for candidate in (_DARWIN_IDS if host == "darwin" else _LINUX_IDS):
        if candidate not in used and candidate not in retired:
            return candidate
    raise HandsSetupError("no free user/group id left in Levain's range")


def _nologin() -> str:
    for candidate in ("/usr/sbin/nologin", "/sbin/nologin"):
        if os.path.exists(candidate):
            return candidate
    return "/bin/false"


def plan_setup(
    entity_dir: Path | str,
    *,
    operator: str,
    host: HostOS,
    hands_id: int,
    reenable: bool = False,
    git_identity: dict[str, str] | None = None,
    sshd_dropins: bool = False,
    deny_lists: tuple[Path, ...] = (),
    egress_ports: tuple[int, ...] = (),
    net_gid: int | None = None,
) -> Plan:
    """The setup, as data. ``sshd_dropins`` says whether sshd reads ``sshd_config.d``;
    ``deny_lists`` are the cron/at deny files that exist (only an existing list is extended: where
    neither list exists, cron's default is site-specific and a new deny file could widen it)."""
    if not OPERATOR_RE.match(operator):
        raise HandsSetupError(f"refusing the account name {operator!r}: it could change meaning in a sudoers rule")
    ed = Path(entity_dir).expanduser().resolve()
    hands = hands_user_name(ed)
    ws = hands_workspace(host, hands)
    ws_parent = ws.parent
    root = WORKSPACE_ROOT[host]
    steps: list[Step] = []
    if host == "darwin":
        home = Path("/Users") / hands
        dscl = ("/usr/bin/dscl", ".")
        u, g = f"/Users/{hands}", f"/Groups/{hands}"
        steps += [
            Step("create the hands group", (*dscl, "-create", g)),
            Step("set the group id", (*dscl, "-create", g, "PrimaryGroupID", str(hands_id))),
            Step("label the group", (*dscl, "-create", g, "RealName", HANDS_MARKER)),
            Step("create the hands user", (*dscl, "-create", u)),
            Step("set its user id", (*dscl, "-create", u, "UniqueID", str(hands_id))),
            Step("make its own group its primary group (not staff)", (*dscl, "-create", u, "PrimaryGroupID", str(hands_id))),
            Step("give it no login shell", (*dscl, "-create", u, "UserShell", "/usr/bin/false")),
            Step("set its home", (*dscl, "-create", u, "NFSHomeDirectory", str(home))),
            Step("label the user", (*dscl, "-create", u, "RealName", HANDS_MARKER)),
            Step("hide it from the login window", (*dscl, "-create", u, "IsHidden", "1")),
            Step("give it no password", (*dscl, "-create", u, "Password", "*")),
            Step("disable the account for login", (*dscl, "-create", u, "AuthenticationAuthority", ";DisabledUser;")),
            Step("create the hands home", ("/bin/mkdir", "-p", str(home))),
            Step("give the home to the hands user", ("/usr/sbin/chown", f"{hands}:{hands_id}", str(home))),
            Step("make the home private", ("/bin/chmod", "700", str(home))),
            Step("hide the home in Finder", ("/usr/bin/chflags", "hidden", str(home))),
            Step("create the shared workspace root", ("/bin/mkdir", "-p", "-m", "755", str(root))),
            Step("check the shared root is still root's alone", call=lambda: _still_roots(host)),
            Step("create the entity's directory (root's)", ("/bin/mkdir", "-m", "755", str(ws_parent))),
            Step("create the workspace", ("/bin/mkdir", "-m", "700", str(ws))),
            Step("give the workspace to the hands user", ("/usr/sbin/chown", f"{hands}:{hands_id}", str(ws))),
            Step(f"let {operator} read the workspace, and nothing more (inherited ACL)",
                 ("/bin/chmod", "+a", f"user:{operator} allow {_DARWIN_OPERATOR_READ}", str(ws))),
        ]
    elif host == "linux":
        home = _LINUX_HANDS_HOME_ROOT / hands
        steps.append(Step("create the hands homes directory",
                          (_abs("mkdir"), "-p", "-m", "755", str(_LINUX_HANDS_HOME_ROOT))))
        if reenable:
            # The tombstone undo left: the same account and id, brought back.
            steps += [
                Step("bring the retired hands user back (its own home, still no login shell, no other groups)",
                     (_abs("usermod"), "--home", str(home), "--shell", _nologin(), "--comment", HANDS_MARKER,
                      "--groups", "", hands)),
                Step("lift its expiry", (_abs("chage"), "--expiredate", "-1", hands)),
                Step("create its home", (_abs("mkdir"), "-m", "700", str(home))),
                Step("give it the home", (_abs("chown"), f"{hands}:{hands_id}", str(home))),
            ]
        else:
            steps += [
                Step("create the hands group", (_abs("groupadd"), "--system", "--gid", str(hands_id), hands)),
                Step("create the hands user (no password, no login shell)",
                     (_abs("useradd"), "--system", "--uid", str(hands_id), "--gid", str(hands_id),
                      "--home-dir", str(home), "--create-home", "--shell", _nologin(),
                      "--comment", HANDS_MARKER, hands)),
            ]
        steps += [
            Step("make the home private", (_abs("chmod"), "700", str(home))),
            Step("create the shared workspace root", (_abs("mkdir"), "-p", "-m", "755", str(root))),
            Step("check the shared root is still root's alone", call=lambda: _still_roots(host)),
            Step("create the entity's directory (root's)", (_abs("mkdir"), "-m", "755", str(ws_parent))),
            Step("create the workspace", (_abs("mkdir"), "-m", "700", str(ws))),
            Step("give the workspace to the hands user", (_abs("chown"), f"{hands}:{hands_id}", str(ws))),
            Step(f"let {operator} read the workspace, and nothing more (ACL)",
                 (_abs("setfacl"), "-m", f"u:{operator}:rX", str(ws))),
            Step("make new files in the workspace inherit it (default ACL)",
                 (_abs("setfacl"), "-d", "-m", f"u:{operator}:rX", str(ws))),
            # Each Linux command runs in a cgroup leaf of its own, a transient scope of the operator's
            # systemd user manager (confinement `_cgroup_problem`); linger keeps that manager running
            # without a login session (cron, ssh without pam_systemd). --undo leaves it: it is the
            # operator's own manager, and other services of theirs may rely on it.
            Step(f"keep {operator}'s systemd user manager running without a login session (linger)",
                 (_abs("loginctl"), "enable-linger", operator),
                 skip_if=(_abs("test"), "-e", f"/var/lib/systemd/linger/{operator}")),
        ]
        # Before anything runs as the hands user (the key generation below), so nothing of it ever
        # runs with its network open.
        if net_gid is None:
            raise HandsSetupError("a Linux setup needs a gid for the hands user's network group")
        steps += [Step("create the group network git runs with (the hands user is not a member)",
                       (_abs("groupadd"), "--system", "--gid", str(net_gid), hands_net_group(hands)))]
        steps += egress_steps(hands, hands_id, egress_ports, net_gid)
    else:  # pragma: no cover - guarded by the caller
        raise HandsSetupError(f"unsupported host {host!r}")

    as_hands = ("/usr/bin/sudo", "-u", hands, "/usr/bin/env", "-i", f"HOME={home}", f"PATH={SECURE_PATH}")
    git, keygen = _abs("git"), _abs("ssh-keygen")
    key = deploy_key_path(home)
    # No safe.directory on either side (Phill, 2026-10-07: "go with H"): every repository in the
    # workspace belongs to the hands user, so the hands user's git needs no exception and the
    # operator's git refuses them by its own ownership check.
    steps += [
        Step("create the hands user's .ssh", (*as_hands, "/bin/mkdir", "-p", "-m", "700", str(key.parent))),
        Step("generate the entity's own ssh key (register it as a deploy key)",
             (*as_hands, keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"levain {hands}", "-f", str(key)),
             skip_if=("/bin/test", "-e", str(key))),
    ]
    for gkey, value in sorted((git_identity or {}).items()):
        steps.append(Step(f"give the hands user your git {gkey}", (*as_hands, git, "config", "--global", gkey, value)))
    steps.append(Step(
        "allow you to run commands as the hands user, and nothing else (sudoers drop-in)",
        write=(sudoers_path(hands), sudoers_text(operator, hands, hands_net_group(hands) if host == "linux" else None),
               0o440),
        validate=(_abs("visudo"), "-cf"),
    ))
    if sshd_dropins:
        steps.append(Step(
            "refuse remote login for the hands user (sshd drop-in, checked with sshd -t)",
            write=(sshd_dropin_path(hands), f"# Written by `levain setup-isolation`.\nDenyUsers {hands}\n", 0o644),
            call=lambda: _sshd_config_check(sshd_dropin_path(hands)),
        ))
    for deny in deny_lists:
        steps.append(Step(f"refuse scheduled jobs for the hands user ({deny})",
                          call=lambda p=deny: _ensure_line(p, hands, present=True)))
    return Plan(host, operator, hands, hands_id, ed, ws, tuple(steps))


def _present(kind: Literal["user", "group"], host: HostOS, name: str) -> tuple[str, ...]:
    """A ``skip_if`` that exits 0 when the account exists."""
    return ("/bin/sh", "-c", _absent(kind, host, name)[2][2:])


def _darwin_group_with_id(name: str, gid: int) -> tuple[bool, str]:
    """The group ``name`` with PrimaryGroupID ``gid``: created if absent; if present, it must
    already hold that id (another id is refused, never overwritten)."""
    r = subprocess.run(["/usr/bin/dscl", ".", "-read", f"/Groups/{name}", "PrimaryGroupID"],
                       capture_output=True, text=True, cwd="/", env=child_env())
    if r.returncode == 0:
        have = r.stdout.split()[-1] if r.stdout.split() else ""
        return (True, "") if have == str(gid) else (False, f"group {name} exists with id {have!r}, not {gid}")
    for argv in (("/usr/bin/dscl", ".", "-create", f"/Groups/{name}"),
                 ("/usr/bin/dscl", ".", "-create", f"/Groups/{name}", "PrimaryGroupID", str(gid))):
        ok, why = _run_ok(argv)
        if not ok:
            return False, why
    return True, ""


def _darwin_leave_groups(user: str) -> tuple[bool, str]:
    """Remove ``user`` from every group that lists it as a member."""
    out = subprocess.run(["/usr/bin/dscl", ".", "-list", "/Groups", "GroupMembership"], capture_output=True,
                         text=True, cwd="/", env=child_env())
    if out.returncode != 0:
        return False, out.stderr.strip()
    for line in out.stdout.splitlines():
        parts = line.split()
        if parts and user in parts[1:]:
            ok, why = _run_ok(("/usr/sbin/dseditgroup", "-o", "edit", "-d", user, "-t", "user", parts[0]))
            if not ok:
                return False, why
    return True, ""


def _absent(kind: Literal["user", "group"], host: HostOS, name: str) -> tuple[str, ...]:
    """A ``skip_if`` that exits 0 when the account is already gone."""
    if host == "darwin":
        node = "/Users" if kind == "user" else "/Groups"
        probe = f"/usr/bin/dscl . -read {node}/{name}"
    else:
        probe = f"{_abs('getent')} {'passwd' if kind == 'user' else 'group'} {name}"
    return ("/bin/sh", "-c", f"! {probe} >/dev/null 2>&1")


def plan_undo(
    entity_dir: Path | str,
    *,
    operator: str,
    host: HostOS,
    hands_user: str,
    hands_id: int | None,
    operator_gid: int,
    account_gone: bool = False,
) -> Plan:
    """The reverse of :func:`plan_setup`, ordered so nothing acts on the tree while the hands user
    can still change it: the sudoers rule goes first (no new processes), the id is retired, cron and
    at jobs are removed, and every process of the hands user is killed and the kill verified. Only
    then are files given the operator's group and the account retired in place (a tombstone that keeps
    its id reserved where the OS allocates ids; it is never deleted). A step that fails stops the undo.

    ``hands_id`` is the id the DIRECTORY SERVICE gives the user (never the config's alone, which the
    operator account can write: with ``account_gone`` the caller takes the config's id only because
    the workspace setup made still carries it, and then nothing is killed and the tombstone is made
    again); ``None`` when the user is already gone, and then nothing is killed
    and only files with no owner left are touched. The workspace is always the derived one."""
    if not HANDS_USER_RE.match(hands_user):
        raise HandsSetupError(f"refusing to remove {hands_user!r}: not a Levain hands user name")
    ed = Path(entity_dir).expanduser().resolve()
    ws = hands_workspace(host, hands_user)
    tree = ws.parent
    steps: list[Step] = [
        Step("remove the sudoers drop-in (no new hands processes)", ("/bin/rm", "-f", str(sudoers_path(hands_user)))),
    ]
    if hands_id is not None:
        steps += [
            Step("retire the user id so no later hands user reuses it",
                 call=lambda: _ensure_line(_RETIRED_IDS[host], str(hands_id), present=True, create=True)),
            Step("remove the hands user's cron jobs", (_abs("crontab"), "-r", "-u", hands_user), allow_fail=True),
            Step("remove the hands user's at jobs", call=lambda: _remove_owned(_AT_SPOOL[host], hands_id)),
        ]
        if not account_gone:
            # With the account gone there is nothing of it to stop, and root does not kill by an id
            # that only the config names.
            steps.append(Step("stop every process of the hands user, and check they are gone",
                              call=lambda: _kill_all(hands_id)))
    if host == "linux":
        # After the processes are stopped: the boundary is lifted only once nothing of the hands user
        # runs. Keyed by name, so it is removed even when the account and its id are gone.
        steps += egress_undo_steps(hands_user)
    steps += [
        Step("give what the hands user owned your group (its owner stays its old id: never you, never root)",
             call=lambda: _to_operator_group(tree, hands_id, operator_gid)),
        Step("remove the workspace ACLs",
             call=lambda: _run_ok(("/bin/chmod", "-R", "-N", str(tree)) if host == "darwin"
                                  else (_abs("setfacl"), "-R", "-P", "-b", str(tree)), missing_ok=True)),
        Step("let your group read it, and empty the hooks and settings of the entity's repositories",
             call=lambda: _readable_and_sanitised(tree, operator_gid, owner_uid=hands_id)),
    ]
    if hands_id is None:
        pass   # no account and no id anything vouches for: no tombstone to keep or make
    elif host == "darwin":
        dscl, u, g = ("/usr/bin/dscl", "."), f"/Users/{hands_user}", f"/Groups/{hands_user}"
        if account_gone:
            # Someone deleted the account: make the tombstone, so the id is reserved again.
            steps += [
                Step("create the hands group again, holding its id (or check the one there holds it)",
                     call=lambda: _darwin_group_with_id(hands_user, hands_id)),
                Step("create the hands user again, to hold its id", (*dscl, "-create", u)),
                Step("give it its id", (*dscl, "-create", u, "UniqueID", str(hands_id))),
                Step("and its own group", (*dscl, "-create", u, "PrimaryGroupID", str(hands_id))),
            ]
        steps += [
            Step("keep the account as a tombstone: no login shell", (*dscl, "-create", u, "UserShell", "/usr/bin/false")),
            Step("no password", (*dscl, "-create", u, "Password", "*")),
            Step("disabled for login", (*dscl, "-create", u, "AuthenticationAuthority", ";DisabledUser;")),
            Step("hidden", (*dscl, "-create", u, "IsHidden", "1")),
            Step("no home", (*dscl, "-create", u, "NFSHomeDirectory", "/var/empty")),
            Step("labelled as a tombstone", (*dscl, "-create", u, "RealName", RETIRED_MARKER[host])),
            Step("take it out of every group", call=lambda: _darwin_leave_groups(hands_user)),
            Step("delete the hands home (its ssh key goes with it)", ("/bin/rm", "-rf", str(Path("/Users") / hands_user))),
        ]
    else:
        home = _LINUX_HANDS_HOME_ROOT / hands_user
        if account_gone:
            steps += [
                Step("create the hands group again, to hold its id",
                     (_abs("groupadd"), "--system", "--gid", str(hands_id), hands_user),
                     skip_if=_present("group", host, hands_user)),
                Step("create the hands user again, to hold its id",
                     (_abs("useradd"), "--system", "--uid", str(hands_id), "--gid", str(hands_id),
                      "--no-create-home", "--home-dir", "/nonexistent", "--shell", _nologin(),
                      "--comment", RETIRED_MARKER[host], hands_user)),
            ]
        steps += [
            Step("keep the account as a tombstone: locked, no login shell, no home, no other groups",
                 (_abs("usermod"), "--lock", "--shell", _nologin(), "--home", "/nonexistent",
                  "--comment", RETIRED_MARKER[host], "--groups", "", hands_user)),
            Step("expired", (_abs("chage"), "--expiredate", "0", hands_user)),
            Step("delete the hands home (its ssh key goes with it)", (_abs("rm"), "-rf", str(home))),
            Step("remove the hands homes directory if empty", (_abs("rmdir"), str(_LINUX_HANDS_HOME_ROOT)),
                 allow_fail=True),
        ]
    steps += [
        Step("remove the sshd drop-in", ("/bin/rm", "-f", str(sshd_dropin_path(hands_user)))),
        *[Step(f"remove the hands user from {deny}", call=lambda p=deny: _ensure_line(p, hands_user, present=False))
          for deny in _CRON_DENY[host]],
        Step("remove the workspace if it is empty", call=lambda: _remove_if_empty(ws)),
    ]
    return Plan(host, operator, hands_user, hands_id if hands_id is not None else -1, ed, ws, tuple(steps))


# --- step bodies that are not a single command ----------------------------------------------------


def _run_ok(argv: tuple[str, ...], *, missing_ok: bool = False) -> tuple[bool, str]:
    if missing_ok and not os.path.lexists(argv[-1]):
        return True, "already gone"
    r = subprocess.run(argv, capture_output=True, text=True, cwd="/", env=child_env())
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _kill_all(uid: int, *, attempts: int = 20) -> tuple[bool, str]:
    for _ in range(attempts):
        subprocess.run([_abs("pkill"), "-KILL", "-U", str(uid)], capture_output=True, cwd="/", env=child_env())
        if subprocess.run([_abs("pgrep"), "-U", str(uid)], capture_output=True, cwd="/", env=child_env()).returncode == 1:
            return True, ""
        time.sleep(0.25)
    return False, f"processes of uid {uid} are still running"


def _remove_owned(spool: Path, uid: int) -> tuple[bool, str]:
    if not spool.is_dir():
        return True, f"no {spool}"
    return _run_ok((_abs("find"), str(spool), "-maxdepth", "1", "-type", "f", "-uid", str(uid), "-delete"))


def _owned_selector(uid: int | None) -> tuple[str, ...]:
    """``find`` predicates for "what the hands user owned": by numeric uid, or (user already gone)
    by having no owner at all. Directories, and files with a single link only: a hard link the
    entity made to someone else's file is never re-grouped or re-moded (it is not the entity's)."""
    who = ("-nouser",) if uid is None else ("-uid", str(uid))
    return ("(", "-type", "d", "-o", "-links", "1", ")", *who)


def _to_operator_group(tree: Path, uid: int | None, operator_gid: int) -> tuple[bool, str]:
    """Give everything the hands user owned under ``tree`` the operator's group, and leave its owner
    the hands user's numeric id, which is retired (never given to a later user). Nothing goes to the
    operator: a directory the entity filled (a repository under any name, a bare one included) would
    be trusted by the operator's git if the operator owned it. Nothing goes to root either: root's
    git trusts a root-owned repository, so a later ``sudo git`` there would run what the entity
    planted. An id with no account behind it is trusted by nobody's git. Runs only after
    :func:`_kill_all`, so nothing can swap a directory for a symlink under it."""
    if not tree.exists():
        return True, "no workspace"
    return _run_ok((_abs("find"), str(tree), *_owned_selector(uid),
                    "-exec", _abs("chgrp"), "-h", str(operator_gid), "{}", "+"))


def _owned_by(path: Path, uid: int | None) -> bool:
    """``path`` belongs to ``uid``, or (``None``) to an id with no account."""
    owner = path.lstat().st_uid
    if uid is not None:
        return owner == uid
    try:
        pwd.getpwuid(owner)
        return False
    except KeyError:
        return True


def _git_dir_shaped(path: Path) -> bool:
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def _readable_and_sanitised(tree: Path, operator_gid: int, *, owner_uid: int | None) -> tuple[bool, str]:
    """After the ACLs are cleared (a group chmod on a file with a Linux ACL edits the mask, which
    clearing discards, measured in CI): let the operator's group read what the retired id still owns
    (``owner_uid``; ``None`` = the account is already gone, so: files with no owner), and, in every
    directory shaped like a git directory, empty ``hooks/`` and cut ``config`` to the keys ``ws-git``
    accepts. No git trusts those repositories any more; this is belt and braces."""
    if not tree.exists():
        return True, "no workspace"
    # ! -type l: chmod follows a link, and this runs as root; a link the entity left would aim it at
    # any file on the machine.
    ok, why = _run_ok((_abs("find"), str(tree), *_owned_selector(owner_uid), "!", "-type", "l",
                       "-gid", str(operator_gid), "-exec", _abs("chmod"), "g+rX,g-w", "{}", "+"))
    if not ok:
        return ok, why
    from levain.firing.ws_git import CONFIG_ALLOWLIST

    repos: list[str] = []
    for root, dirs, _files in os.walk(tree):
        here = Path(root)
        if not _git_dir_shaped(here) or not _owned_by(here, owner_uid):
            continue
        hooks = here / "hooks"
        if hooks.is_dir() and not hooks.is_symlink():
            for h in hooks.iterdir():
                if h.is_file() or h.is_symlink():
                    h.unlink()
        cfg = here / "config"
        if cfg.is_file() and not cfg.is_symlink():
            cfg_uid = cfg.lstat().st_uid
            r = subprocess.run([_abs("git"), "config", "--file", str(cfg), "--list"], capture_output=True, text=True,
                               cwd="/", env={"PATH": SECURE_PATH, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_CONFIG_GLOBAL": "/dev/null"})
            kept = [ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln]
            kept = [(k, v) for k, v in kept if CONFIG_ALLOWLIST.match(k)]
            cfg.unlink()
            for k, v in kept:
                subprocess.run([_abs("git"), "config", "--file", str(cfg), "--add", k, v], capture_output=True, cwd="/", env=child_env())
            if cfg.exists():
                os.chown(cfg, cfg_uid, operator_gid)
                os.chmod(cfg, 0o640)
        repos.append(str(here.parent if here.name == ".git" else here))
        dirs[:] = []
    if repos:
        first = repos[0]
        return True, ("the entity's repositories keep its retired id as owner, with no hooks and only basic "
                      "settings; no git (yours or root's) will use them directly. To keep the work: git -c "
                      "safe.directory="
                      f"{first} -c core.hooksPath=/dev/null clone --no-local {first} <destination>   "
                      f"(repositories: {', '.join(repos)})")
    return True, ""


def _remove_if_empty(ws: Path) -> tuple[bool, str]:
    if not ws.exists():
        return True, "already gone"
    try:
        ws.rmdir()
        ws.parent.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return True, (f"kept: {ws} has files in it, still owned by the retired id and readable by you; "
                          "remove it with "
                          f"`sudo rm -rf {ws.parent}` when you no longer need them")
        raise
    return True, ""


def _ensure_line(path: Path, line: str, *, present: bool, create: bool = False) -> tuple[bool, str]:
    """Add or remove one exact line in a root-owned list file, keeping every other line."""
    if not path.exists():
        if not (present and create):
            return True, f"{path} does not exist"
        path.parent.mkdir(parents=True, exist_ok=True)
        current: list[str] = []
        mode = 0o644
    else:
        current = path.read_text(encoding="utf-8").splitlines()
        mode = path.stat().st_mode & 0o7777
    wanted = [ln for ln in current if ln.strip() != line] + ([line] if present else [])
    if wanted == current:
        return True, "already so"
    tmp = path.with_name(path.name + ".levain-tmp")
    tmp.write_text("".join(f"{ln}\n" for ln in wanted), encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True, ""


def _sshd_config_check(dropin: Path) -> tuple[bool, str]:
    """``sshd -t`` with the drop-in in place. If it fails, check whether it fails without the drop-in
    too (no host keys yet, an existing error): then the drop-in did not cause it and is kept."""
    ok, why = _run_ok((_abs("sshd"), "-t"))
    if ok:
        return True, ""
    content, mode = dropin.read_text(encoding="utf-8"), dropin.stat().st_mode & 0o7777
    dropin.unlink()
    baseline_ok, _ = _run_ok((_abs("sshd"), "-t"))
    if baseline_ok:
        return False, why
    dropin.write_text(content, encoding="utf-8")
    os.chmod(dropin, mode)
    return True, f"kept; `sshd -t` fails here without it too ({why})"


# --- the host -------------------------------------------------------------------------------------


def deploy_key_path(hands_home: str | Path) -> Path:
    return Path(hands_home) / ".ssh" / "id_ed25519"


def _home_of(user: str, default: str | None = None) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        if default is not None:
            return default
        raise HandsSetupError(f"no such user {user!r}") from None


def host_os() -> HostOS:
    system = platform.system()
    if system == "Darwin":
        return "darwin"
    if system == "Linux":
        return "linux"
    raise HandsSetupError(f"setup-isolation supports macOS and Linux, not {system}")


def used_ids(host: HostOS) -> set[int]:
    used: set[int] = set()
    if host == "darwin":
        for kind, attr in (("/Users", "UniqueID"), ("/Groups", "PrimaryGroupID")):
            out = subprocess.run(["/usr/bin/dscl", ".", "-list", kind, attr],
                                 capture_output=True, text=True, check=True, env=child_env()).stdout
            used.update(int(p[-1]) for p in (ln.split() for ln in out.splitlines())
                        if p and p[-1].lstrip("-").isdigit())
        return used
    for db in ("passwd", "group"):
        out = subprocess.run([_abs("getent"), db], capture_output=True, text=True, check=True, env=child_env()).stdout
        used.update(int(f[2]) for f in (ln.split(":") for ln in out.splitlines()) if len(f) > 2 and f[2].isdigit())
    return used


def retired_ids(host: HostOS) -> set[int]:
    path = _RETIRED_IDS[host]
    if not path.exists():
        return set()
    return {int(ln) for ln in path.read_text(encoding="utf-8").split() if ln.isdigit()}


def sshd_reads_dropins() -> bool:
    try:
        text = _SSHD_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(ln.strip().lower().startswith("include") and str(_SSHD_DROPIN_DIR) in ln for ln in text.splitlines())


def existing_deny_lists(host: HostOS) -> tuple[Path, ...]:
    return tuple(p for p in _CRON_DENY[host] if p.exists())


#: The first release of each git line that also checks who owns the git DIRECTORY, not just the
#: worktree (CVE-2022-29187; RelNotes 2.30.5: "The safety check that verifies a safe ownership of
#: the Git worktree is now extended to also cover the ownership of the Git directory"). Option H
#: depends on it.
_GITDIR_OWNERSHIP_FIX = {30: 5, 31: 4, 32: 3, 33: 4, 34: 4, 35: 4, 36: 2, 37: 1}


def git_checks_gitdir_ownership(version_text: str) -> bool:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if not m:
        return False
    major, minor, patch = (int(x) for x in m.groups())
    if major != 2:
        return major > 2
    if minor >= 38:
        return True
    return minor in _GITDIR_OWNERSHIP_FIX and patch >= _GITDIR_OWNERSHIP_FIX[minor]


def git_version() -> str:
    return subprocess.run([_abs("git"), "--version"], capture_output=True, text=True, cwd="/", env=child_env()).stdout.strip()


def git_refuses_foreign_gitdir(operator: str) -> bool:
    """MEASURED, as root at setup: in a scratch repository whose worktree is the operator's and
    whose git directory belongs to ``nobody``, the operator's git must refuse to work. A version
    number cannot say this (distributions backport the fix without changing it)."""
    nobody = pwd.getpwnam("nobody")
    op = pwd.getpwnam(operator)
    scratch = Path(tempfile.mkdtemp(prefix="levain-gitcheck-"))
    try:
        repo = scratch / "r"
        env = {"PATH": SECURE_PATH, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        if subprocess.run([_abs("git"), "init", "-q", str(repo)], env=env, cwd="/", capture_output=True).returncode:
            return False
        os.chmod(scratch, 0o755)
        for root, dirs, files in os.walk(repo / ".git"):
            for name in [*dirs, *files]:
                os.lchown(os.path.join(root, name), nobody.pw_uid, nobody.pw_gid)
                os.chmod(os.path.join(root, name), 0o755 if name in dirs else 0o644)
        os.chown(repo / ".git", nobody.pw_uid, nobody.pw_gid)
        os.chown(repo, op.pw_uid, op.pw_gid)
        r = subprocess.run(["/usr/bin/sudo", "-u", operator, "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in env.items()],
                            _abs("git"), "-C", str(repo), "status"], capture_output=True, text=True, cwd="/", env=child_env())
        return r.returncode != 0 and "dubious ownership" in (r.stderr + r.stdout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def shared_root_problem(host: HostOS) -> str | None:
    """The shared workspace root (``/Users/Shared`` is world-writable on macOS, so another local
    account could create ``/Users/Shared/levain`` first) must be absent, or a real directory owned by
    root and not writable by group or others."""
    root = WORKSPACE_ROOT[host]
    if not os.path.lexists(root):
        return None
    st = root.lstat()
    if not stat_is_dir(st.st_mode) or root.is_symlink():
        return f"{root} is not a plain directory"
    if st.st_uid != 0 or st.st_mode & 0o022:
        return f"{root} must be owned by root and not writable by group or others"
    return None


def _still_roots(host: HostOS) -> tuple[bool, str]:
    """After ``mkdir -p``: it accepts a directory another local account made first (``/Users/Shared``
    is world-writable), so the root is checked again once it exists."""
    problem = shared_root_problem(host)
    return problem is None, problem or ""


def stat_is_dir(mode: int) -> bool:
    import stat as _stat

    return _stat.S_ISDIR(mode)


def invoking_operator() -> str:
    """The operator who ran ``sudo levain setup-isolation``. Refuses unless running as root through
    sudo from a non-root account: the sudoers rule names that account."""
    if os.geteuid() != 0:
        raise HandsSetupError("setup-isolation needs root once: run it as `sudo levain setup-isolation`.")
    operator = os.environ.get("SUDO_USER")
    if not operator or operator == "root":
        raise HandsSetupError(
            "run setup-isolation through sudo from your own account (SUDO_USER names the account "
            "that will be allowed to start the hands user)."
        )
    if not OPERATOR_RE.match(operator):
        raise HandsSetupError(f"refusing the account name {operator!r}: it could change meaning in a sudoers rule")
    pwd.getpwnam(operator)  # KeyError → the caller reports it
    return operator


def operator_git_identity(operator: str) -> dict[str, str]:
    """The operator's global ``user.name`` / ``user.email``, read as the operator."""
    found: dict[str, str] = {}
    run_as = ["/usr/bin/sudo", "-u", operator] if os.geteuid() == 0 else []
    for key in ("user.name", "user.email"):
        r = subprocess.run([*run_as, "/usr/bin/env", f"HOME={_home_of(operator)}", _abs("git"),
                            "config", "--global", "--get", key], capture_output=True, text=True, cwd="/", env=child_env())
        if r.returncode == 0 and r.stdout.strip():
            found[key] = r.stdout.strip()
    return found


def user_record_is_ours(hands_user: str, host: HostOS) -> bool:
    """True when ``hands_user`` exists and carries :data:`HANDS_MARKER`."""
    try:
        entry = pwd.getpwnam(hands_user)
    except KeyError:
        return False
    if host == "linux":
        return entry.pw_gecos.split(",")[0] == HANDS_MARKER
    out = subprocess.run(["/usr/bin/dscl", ".", "-read", f"/Users/{hands_user}", "RealName"],
                         capture_output=True, text=True, env=child_env()).stdout
    return HANDS_MARKER in out


def user_is_retired(hands_user: str, host: HostOS) -> bool:
    """True when ``hands_user`` exists as an undo tombstone (:data:`RETIRED_MARKER`), asked of the
    directory service in a fresh process (this process's cache can lag a change just made)."""
    if host == "linux":
        r = subprocess.run([_abs("getent"), "passwd", hands_user], capture_output=True, text=True, cwd="/", env=child_env())
        fields = r.stdout.strip().split(":")
        return r.returncode == 0 and len(fields) > 4 and fields[4] == RETIRED_MARKER[host]
    r = subprocess.run(["/usr/bin/dscl", ".", "-read", f"/Users/{hands_user}", "RealName"],
                       capture_output=True, text=True, cwd="/", env=child_env())
    return r.returncode == 0 and RETIRED_MARKER[host] in r.stdout


def _group_exists(name: str) -> bool:
    import grp

    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


# --- running a plan -------------------------------------------------------------------------------


def run_plan(plan: Plan, *, dry_run: bool, emit: Callable[[str], None] = print) -> int:
    """Run the steps in order. Stops at the first failing step that is not ``allow_fail`` and returns
    1; returns 0 when every step ran or was skipped."""
    for i, step in enumerate(plan.steps, 1):
        label = f"[{i}/{len(plan.steps)}] {step.why}"
        if step.write is not None:
            emit(f"{label}\n      write {step.write[0]} (mode {step.write[2]:o})")
        elif step.argv:
            emit(f"{label}\n      {' '.join(step.argv)}")
        else:
            emit(label)
        if dry_run:
            continue
        # cwd "/": a command run as the hands user dies on a working directory it cannot read
        # (measured: git fatals on the operator's home).
        if step.skip_if and subprocess.run(step.skip_if, capture_output=True, cwd="/", env=child_env()).returncode == 0:
            emit("      already done")
            continue
        if step.write is not None:
            ok, why = _install_file(*step.write, validate=step.validate, check=step.call)
        elif step.call is not None:
            try:
                ok, why = step.call()
            except Exception as exc:  # noqa: BLE001 — a step that raises is a failed step, not a crash mid-undo
                ok, why = False, f"{type(exc).__name__}: {exc}"
        else:
            r = subprocess.run(step.argv, capture_output=True, text=True, cwd="/", env=child_env())
            ok, why = r.returncode == 0, (r.stderr or r.stdout).strip()
        if ok and why:
            emit(f"      {why}")
        if not ok:
            if step.allow_fail:
                emit(f"      skipped ({why or 'not possible here'})")
                continue
            emit(f"      FAILED: {why}")
            return 1
    return 0


def _install_file(path: Path, content: str, mode: int, *, validate: tuple[str, ...],
                  check: Callable[[], tuple[bool, str]] | None = None) -> tuple[bool, str]:
    """Write ``content`` to a temp file beside ``path``, validate it, then move it into place with
    ``mode``, owned by root. ``check`` runs after the move (for a config whose validator reads the
    installed tree, like ``sshd -t``); if it fails the file is removed again. A drop-in that fails
    validation is never left installed: a broken sudoers.d file breaks sudo for the whole machine."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory, so the final rename is atomic and never crosses filesystems; the ".tmp" suffix
    # makes sudo ignore the file while it is being validated (sudoers.d skips names with a ".").
    fd, tmp = tempfile.mkstemp(prefix="levain-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        if validate:
            r = subprocess.run([*validate, tmp], capture_output=True, text=True, env=child_env())
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()
        os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if check is not None:
        ok, why = check()
        if not ok:
            path.unlink(missing_ok=True)
            return False, why
    return True, ""


# --- the record -----------------------------------------------------------------------------------

RECORD_KEYS = ("hands_user", "hands_uid", "hands_workspace", "hands_egress_ports")


def record_hands(entity_dir: Path, values: dict | None, *, owner_uid: int, owner_gid: int) -> Path:
    """Set (or, with ``None``, remove) the hands keys in ``<entity>/.levain/confinement.json``,
    keeping every other key, owned by the operator. Root does this inside a tree the operator
    controls, so everything goes through a directory fd opened without following a symlink, and the
    file is replaced atomically (a crash leaves the old file, never a half-written one)."""
    levain_dir = Path(entity_dir) / ".levain"
    try:
        dfd = os.open(levain_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise HandsSetupError(f"refusing to write in {levain_dir}: {exc.strerror} (a symlink?)") from None
    try:
        data: dict = {}
        try:
            fd = os.open("confinement.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError:
            fd = None
        except OSError as exc:
            raise HandsSetupError(f"refusing to write {levain_dir}/confinement.json: {exc.strerror} (a symlink?)") from None
        if fd is not None:
            with os.fdopen(fd, encoding="utf-8") as fh:
                data = json.loads(fh.read() or "{}")
            if not isinstance(data, dict):
                raise HandsSetupError(f"{levain_dir}/confinement.json is not a JSON object; fix it first")
        for k in RECORD_KEYS:
            data.pop(k, None)
        data.update(values or {})
        tmp = f".confinement.json.levain-{os.getpid()}"
        wfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            with os.fdopen(wfd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2) + "\n")
                os.fchown(fh.fileno(), owner_uid, owner_gid)
            os.replace(tmp, "confinement.json", src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)
    return levain_dir / "confinement.json"


def _undo_lock(entity_dir: Path, owner_uid: int, owner_gid: int) -> int:
    """The hands lock, exclusive, for the whole undo: no session, ws-git, ws-put or ws-adopt runs
    while the account and its sudoers rule go. If the lock file does not exist yet, root creates it
    (O_EXCL, no link followed) and gives it to the operator, so the operator's own sessions can open
    it afterwards; absence is never taken to mean nobody can start. Returns the fd, or -1 when the
    lock is held."""
    import fcntl

    from levain.firing.ws_git import HANDS_LOCK

    cloexec = getattr(os, "O_CLOEXEC", 0)
    # Root, in a tree the operator controls: .levain is opened without following a link and must be
    # the operator's directory, and the lock is made relative to that fd, never by a path again.
    dfd = os.open(entity_dir / ".levain", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec)
    try:
        if os.fstat(dfd).st_uid != owner_uid:
            raise HandsSetupError(f"{entity_dir}/.levain is not yours; refusing to create the lock in it")
        flags = os.O_RDWR | os.O_NOFOLLOW | cloexec
        try:
            fd = os.open(HANDS_LOCK, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
            os.fchown(fd, owner_uid, owner_gid)
        except FileExistsError:
            fd = os.open(HANDS_LOCK, flags, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return -1
    return fd


def _operator_path_under_home(operator: str) -> list[str]:
    home = _home_of(operator)
    path = os.environ.get("PATH", "")
    return [d for d in path.split(os.pathsep) if d and (d == home or d.startswith(home + os.sep))]


# --- the command ----------------------------------------------------------------------------------


def _repair_linux(entity_dir: Path, cfg, operator: str, op: pwd.struct_passwd, ports: tuple[int, ...], *,
                  dry_run: bool) -> int:
    """``setup-isolation`` on a Linux entity already set up: the steps an older install lacks run
    here, idempotently, without an undo ("run setup again" is their remedy). Linger, from before the
    cgroup frame (L3 r9); the network boundary, from before P-1 (a), or to change its ports."""
    hands = cfg.hands_user
    if hands != hands_user_name(entity_dir) or not user_record_is_ours(hands, "linux"):
        # The record is the operator's to write: root rewrites a sudoers rule and a ruleset only for the
        # hands user THIS entity's path derives, and only one Levain created (L3 r1, codex).
        print(f"setup-isolation: refusing to repair {hands}: it is not the hands user setup made for {entity_dir}. "
              "Run with --undo, then set it up again.")
        return 1
    try:
        hands_id = pwd.getpwnam(hands).pw_uid
    except KeyError:
        print(f"setup-isolation: the recorded hands user {hands} does not exist. Run with --undo, then again.")
        return 1
    if hands_id != cfg.hands_uid:
        # From the directory service: root must not write a rule for an id the operator-writable config
        # alone names.
        print(f"setup-isolation: {hands} has id {hands_id}, but confinement.json records {cfg.hands_uid}; refusing.")
        return 1
    net = hands_net_group(hands)
    steps = [Step(f"keep {operator}'s systemd user manager running without a login session (linger)",
                  (_abs("loginctl"), "enable-linger", operator))]
    if _group_exists(net):
        import grp

        net_gid = grp.getgrnam(net).gr_gid
        if (problem := net_group_problem(hands)) is not None:
            print(f"setup-isolation: refusing: {problem}.")
            return 1
    else:
        # An install from before D has no net group: make one, with an id no Levain account has had.
        try:
            net_gid = choose_id("linux", used_ids("linux"), retired_ids("linux"))
        except (HandsSetupError, subprocess.CalledProcessError) as exc:
            print(f"setup-isolation: {exc}")
            return 1
        steps.append(Step("create the group network git runs with (the hands user is not a member)",
                          (_abs("groupadd"), "--system", "--gid", str(net_gid), net)))
    steps.append(Step("let you start network git as the hands user with that group (sudoers drop-in)",
                      write=(sudoers_path(hands), sudoers_text(operator, hands, net), 0o440),
                      validate=(_abs("visudo"), "-cf")))
    steps += egress_steps(hands, hands_id, ports, net_gid)
    plan = Plan("linux", operator, hands, hands_id, entity_dir, cfg.hands_workspace, tuple(steps))
    print(f"Repairing the setup recorded in {entity_dir} (user {hands}): linger, and the network boundary "
          + (f"(loopback ports {', '.join(map(str, ports))} allowed)." if ports else "(no port allowed)."))
    # One repair at a time, and none under a session or ws-git: the hands lock, exclusive, from the
    # re-read of the record to the end of the plan (L3 r2, codex: two repairs could record B and load A).
    lock_fd = None
    if not dry_run:
        try:
            lock_fd = _undo_lock(entity_dir, op.pw_uid, op.pw_gid)
        except (HandsSetupError, OSError) as exc:
            print(f"setup-isolation: {exc}")
            return 1
        if lock_fd == -1:
            print("setup-isolation: a session of this entity, or ws-git / ws-put / ws-adopt, or another repair is "
                  "running; refusing to repair under it.")
            return 1
    try:
        if lock_fd is not None:
            from levain.firing.confinement import ConfinementError, load_confinement_config

            try:
                same = load_confinement_config(entity_dir, bound_hands=False) == cfg
            except (ConfinementError, OSError, ValueError):   # ValueError covers a decode error (L3 r4)
                same = False
            if not same:
                print("setup-isolation: the record changed while this repair started; run it again.")
                return 1
            if ports != cfg.hands_egress_ports:
                # Recorded FIRST, as the state to reach: if a step below fails part way, the ruleset on disk
                # may already hold these ports, and a rerun (which reads the record) converges on them rather
                # than silently reverting (L3 r1).
                try:
                    record_hands(entity_dir, {"hands_user": hands, "hands_uid": hands_id,
                                              "hands_workspace": str(cfg.hands_workspace),
                                              "hands_egress_ports": list(ports)},
                                 owner_uid=op.pw_uid, owner_gid=op.pw_gid)
                except (HandsSetupError, OSError, ValueError) as exc:
                    print(f"setup-isolation: could not record the ports ({exc}); nothing was changed.")
                    return 1
        rc = run_plan(plan, dry_run=dry_run)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
    if rc != 0:
        print("setup-isolation: the repair stopped part way; run it again (it converges on the recorded ports).")
    if rc != 0 or dry_run:
        return rc
    # The repair was what this run was for: it succeeded, so it is not a refusal (L3 r10). This run
    # checked nothing else of the recorded setup, so it claims nothing else (L3 r11).
    print(f"setup-isolation: linger is on and the network boundary is loaded for {hands}; "
          "`levain doctor` checks the rest of the setup.")
    return 0


def cmd_setup_isolation(path: Path | str, *, undo: bool, dry_run: bool, reenable: bool = False,
                        egress_ports: tuple[int, ...] | None = None) -> int:
    """``levain setup-isolation [--undo] [--dry-run] [--egress-port N ...]``. Returns a process exit
    code. ``egress_ports`` (Linux): the loopback TCP ports the hands user may connect to (the proxy
    path); None keeps what is recorded."""
    from levain.firing.confinement import ConfinementError, load_confinement_config
    from levain.firing.isolation import IsolationError, guard_entity

    try:
        host = host_os()
        entity_dir, _crystal, _episodic = guard_entity(path)
        cfg = load_confinement_config(entity_dir, bound_hands=False)
        if dry_run:
            operator = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        else:
            operator = invoking_operator()
        op = pwd.getpwnam(operator)
    except (HandsSetupError, IsolationError, ConfinementError) as exc:
        print(f"setup-isolation: {exc}")
        return 1
    except KeyError as exc:
        print(f"setup-isolation: no such account {exc}")
        return 1

    derived = hands_user_name(entity_dir)
    if undo:
        hands = cfg.hands_user or derived
        if not HANDS_USER_RE.match(hands):
            print(f"setup-isolation: refusing to remove {hands!r}: not a Levain hands user name")
            return 1
        if cfg.hands_workspace is not None and cfg.hands_workspace != hands_workspace(host, hands):
            print(f"setup-isolation: the recorded workspace {cfg.hands_workspace} is not the one setup "
                  f"creates for {hands}; refusing (fix or remove the hands keys in confinement.json).")
            return 1
        hands_id: int | None = None
        account_gone = False
        if not _user_exists(hands) and cfg.hands_uid is not None:
            # The account was removed by someone else. Its files still carry the recorded id: retire
            # it and work on it, unless another account has it now.
            try:
                taken = pwd.getpwuid(cfg.hands_uid).pw_name
            except KeyError:
                # The config is the operator's to write; the id is believed only when the workspace
                # setup made still carries it.
                ws_now = hands_workspace(host, hands)
                try:
                    owner = ws_now.lstat().st_uid
                except OSError:
                    owner = None
                if owner != cfg.hands_uid:
                    print(f"setup-isolation: {hands} is gone and its workspace {ws_now} is not owned by the "
                          f"recorded id {cfg.hands_uid}; refusing to act on that id.")
                    return 1
                hands_id = cfg.hands_uid
                account_gone = True
            else:
                print(f"setup-isolation: {hands} is gone and its id {cfg.hands_uid} now belongs to {taken}; "
                      "refusing to touch files by that id.")
                return 1
        if _user_exists(hands):
            if not dry_run and not user_record_is_ours(hands, host):
                print(f"setup-isolation: refusing to remove {hands}: it exists but was not created by Levain.")
                return 1
            # From the directory service, after the marker check: the config is writable by the
            # operator account, and root must not kill or re-own another account's processes and files.
            hands_id = pwd.getpwnam(hands).pw_uid
            if cfg.hands_uid is not None and cfg.hands_uid != hands_id:
                print(f"setup-isolation: {hands} has id {hands_id}, but confinement.json records "
                      f"{cfg.hands_uid}; refusing.")
                return 1
        if hands != derived and hands_id is not None:
            # A record from another path (the entity was moved, or the config copied). That entity's
            # sessions hold a hands lock in ITS directory, which this undo cannot take; so refuse
            # while anything at all runs as that hands user.
            from levain.firing.ws_git import LIVENESS_TIMEOUT, WsGitError, entity_session_live

            try:
                live = entity_session_live(hands_id, timeout=LIVENESS_TIMEOUT)
            except WsGitError as exc:
                print(f"setup-isolation: {exc}")
                return 1
            if live:
                print(f"setup-isolation: {hands} was set up for another entity directory and has processes "
                      "running; refusing to undo it from here while it is in use.")
                return 1
            print(f"Note: {hands} was set up for this entity at another path. After this undo, a setup here "
                  "makes a new hands user; the old one stays retired.")
        try:
            plan = plan_undo(entity_dir, operator=operator, host=host, hands_user=hands, hands_id=hands_id,
                             operator_gid=op.pw_gid, account_gone=account_gone)
        except HandsSetupError as exc:
            print(f"setup-isolation: {exc}")
            return 1
        print(f"Removing hands isolation for {entity_dir} (user {hands}).")
        try:
            lock_fd = None if dry_run else _undo_lock(entity_dir, op.pw_uid, op.pw_gid)
        except (HandsSetupError, OSError) as exc:
            print(f"setup-isolation: {exc}")
            return 1
        if lock_fd == -1:
            print("setup-isolation: a session of this entity, or levain ws-git / ws-put / ws-adopt, is "
                  "running; refusing to undo under it.")
            return 1
        try:
            rc = run_plan(plan, dry_run=dry_run)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
        if dry_run or rc != 0:
            return rc
        # Asked of the directory service in a fresh process: this process's getpwnam can keep
        # returning a just-deleted macOS account (measured on a CI runner).
        retired = user_is_retired(hands, host)
        leftovers = [what for what, there in (
            (f"the user {hands} as a live account (not retired)", hands_id is not None and not retired),
            (f"the sudoers rule {sudoers_path(hands)}", sudoers_path(hands).exists()),
            (f"the network boundary unit {egress_unit_path(hands)}", host == "linux" and egress_unit_path(hands).exists()),
            (f"the network boundary ruleset {egress_rules_path(hands)}",
             host == "linux" and egress_rules_path(hands).exists()),
            (f"the group {hands_net_group(hands)}", host == "linux" and _group_exists(hands_net_group(hands))),
        ) if there]
        if leftovers:
            print("setup-isolation: undo did not finish: " + "; ".join(leftovers) + " still present.")
            return 1
        record_hands(entity_dir, None, owner_uid=op.pw_uid, owner_gid=op.pw_gid)
        print(f"Done. The hands user {hands} is retired: its account stays, disabled (no login, no password, no "
              "sudo rule, no home), so its id can never be given to another account. To give this entity hands "
              "again later: sudo levain setup-isolation --reenable")
        return 0

    try:
        ports = check_egress_ports(egress_ports if egress_ports is not None else cfg.hands_egress_ports)
    except HandsSetupError as exc:
        print(f"setup-isolation: {exc}")
        return 1
    if host == "linux" and not dry_run:
        unavailable = egress_unavailable()
        if unavailable:
            print(f"setup-isolation: refusing: {unavailable}.")
            return 1
    if cfg.hands_user is not None:
        if host == "linux":
            return _repair_linux(entity_dir, cfg, operator, op, ports, dry_run=dry_run)
        print(f"setup-isolation: {entity_dir} is already set up (user {cfg.hands_user}). "
              "Run with --undo first to set it up again.")
        return 1
    if egress_ports is not None and host != "linux":
        print("setup-isolation: --egress-port is for Linux; on macOS the hands user has no network boundary yet.")
        return 1
    reenable_id: int | None = None
    if _user_exists(derived):
        ours = user_record_is_ours(derived, host)
        if ours and user_is_retired(derived, host):
            if not reenable:
                print(f"setup-isolation: this entity's hands user {derived} is retired (an earlier --undo; "
                      "its id stays reserved). To give the entity its hands back with that same account: "
                      "sudo levain setup-isolation --reenable")
                return 1
            reenable_id = pwd.getpwnam(derived).pw_uid
        else:
            print(f"setup-isolation: the user {derived} already exists "
                  + ("(a previous setup that did not finish). Run with --undo, then again." if ours
                     else "(an account Levain did not create). Remove or rename it first."))
            return 1
    elif reenable:
        print(f"setup-isolation: there is no retired hands user {derived} to re-enable; run it without --reenable.")
        return 1
    problem = shared_root_problem(host)
    if problem:
        print(f"setup-isolation: refusing: {problem}.")
        return 1
    if not dry_run and not git_refuses_foreign_gitdir(operator):
        print(f"setup-isolation: refusing: your git ({git_version() or 'git'}) works in a repository whose "
              "git directory belongs to another user, so it would run code the entity writes into one. "
              "Upgrade git (2.37.1 or later, or a release with the CVE-2022-29187 fix).")
        return 1
    ws_parent = hands_workspace(host, derived).parent
    if os.path.lexists(ws_parent):
        print(f"setup-isolation: {ws_parent} already exists (files from an earlier setup). "
              "Move or delete it, then run setup again.")
        return 1
    try:
        hands_id = reenable_id if reenable_id is not None else choose_id(host, used_ids(host), retired_ids(host))
        net_gid = (choose_id(host, used_ids(host) | {hands_id}, retired_ids(host)) if host == "linux" else None)
        plan = plan_setup(entity_dir, operator=operator, host=host, hands_id=hands_id, reenable=reenable_id is not None,
                          git_identity=operator_git_identity(operator),
                          sshd_dropins=sshd_reads_dropins(), deny_lists=existing_deny_lists(host),
                          egress_ports=ports, net_gid=net_gid)
    except (HandsSetupError, subprocess.CalledProcessError) as exc:
        print(f"setup-isolation: {exc}")
        return 1
    print(f"Setting up hands isolation for {entity_dir}: user {plan.hands_user} (id {hands_id}), "
          f"workspace {plan.workspace}.")
    rc = run_plan(plan, dry_run=dry_run)
    if rc != 0:
        print("Setup stopped part way. Run `sudo levain setup-isolation --undo"
              f" --path {entity_dir}` to remove what was created.")
        return rc
    if dry_run:
        return 0
    record_hands(entity_dir, {"hands_user": plan.hands_user, "hands_uid": hands_id,
                              "hands_workspace": str(plan.workspace),
                              **({"hands_egress_ports": list(ports)} if host == "linux" else {})},
                 owner_uid=op.pw_uid, owner_gid=op.pw_gid)
    print(f"Done. The hands user {plan.hands_user} exists; its workspace is {plan.workspace}.")
    if not sshd_reads_dropins():
        print("Note: sshd here does not read sshd_config.d, so remote login was not refused for the "
              f"hands user. If Remote Login is on, add `DenyUsers {plan.hands_user}` to sshd_config.")
    if not existing_deny_lists(host):
        print("Note: this system has no cron.deny or at.deny; whether the hands user may schedule "
              "jobs depends on your cron's default.")
    under_home = _operator_path_under_home(operator)
    if under_home:
        print("Note: these directories on your PATH are inside your home, so the hands user cannot "
              "run programs from them: " + ", ".join(under_home))
    pub = deploy_key_path(_home_of(plan.hands_user)).with_suffix(".pub")
    try:
        print("\nThe entity has its own ssh key. To let it push, add this public key as a deploy "
              "key (with write access) on each repository it should reach:\n")
        print("    " + pub.read_text(encoding="utf-8").strip())
    except OSError as exc:
        print(f"(could not read {pub}: {exc})")
    return 0
