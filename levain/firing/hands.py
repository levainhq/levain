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
``/var/lib/levain/<hands>/workspace`` (Linux), owned by the operator with an inherited ACL naming the
two users (named users, not a shared group: a group added to the operator's account reaches its
running processes only after a new login on Linux, measured in CI), and records the path in
``confinement.json``. A recorded path, never a symlink.

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

HostOS = Literal["darwin", "linux"]

#: Every name this module creates or deletes matches this; anything else is refused.
HANDS_USER_RE = re.compile(r"^_levain_[a-z0-9]{1,12}_[0-9a-f]{6}$")

#: An operator name that cannot change meaning inside a sudoers line or an ACL spec.
OPERATOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

#: Stamped on the user record (macOS RealName, Linux GECOS) so ``--undo`` deletes only a user this
#: command created, even if a name collides.
HANDS_MARKER = "Levain hands user"

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
_CRON_DENY = {"darwin": (Path("/usr/lib/cron/cron.deny"), Path("/usr/lib/cron/at.deny")),
              "linux": (Path("/etc/cron.deny"), Path("/etc/at.deny"))}

#: The inherited ACL that lets the operator and the hands user both edit the workspace (macOS).
_DARWIN_ACL_PERMS = (
    "list,add_file,search,add_subdirectory,delete_child,readattr,writeattr,readextattr,"
    "writeextattr,readsecurity,read,write,append,execute,delete,file_inherit,directory_inherit"
)


class HandsSetupError(RuntimeError):
    """setup-isolation cannot run here, or refused to act on something it did not create."""


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


def sudoers_text(operator: str, hands_user: str) -> str:
    return (
        f"# Written by `levain setup-isolation`; removed by `levain setup-isolation --undo`.\n"
        f"# {operator} may run commands as the Levain hands user {hands_user}, and nothing else.\n"
        f"Defaults>{hands_user} !requiretty\n"
        f"Defaults>{hands_user} env_reset\n"
        f"{operator} ALL=({hands_user}) NOPASSWD: ALL\n"
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
    operator_gid: int,
    git_identity: dict[str, str] | None = None,
    sshd_dropins: bool = False,
    deny_lists: tuple[Path, ...] = (),
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
    opg = f"{operator}:{operator_gid}"
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
            Step("create the entity's workspace directory, yours", ("/bin/mkdir", "-m", "700", str(ws_parent))),
            Step("create the workspace, yours", ("/bin/mkdir", "-m", "700", str(ws))),
            Step("own them as you", ("/usr/sbin/chown", opg, str(ws_parent), str(ws))),
            Step("let the hands user pass through to the workspace",
                 ("/bin/chmod", "+a", f"user:{hands} allow search", str(ws_parent))),
            Step("let the hands user edit the workspace (inherited ACL)",
                 ("/bin/chmod", "+a", f"user:{hands} allow {_DARWIN_ACL_PERMS}", str(ws))),
            Step(f"let {operator} edit what the hands user creates there (inherited ACL)",
                 ("/bin/chmod", "+a", f"user:{operator} allow {_DARWIN_ACL_PERMS}", str(ws))),
        ]
    elif host == "linux":
        home = _LINUX_HANDS_HOME_ROOT / hands
        steps += [
            Step("create the hands homes directory", (_abs("mkdir"), "-p", "-m", "755", str(_LINUX_HANDS_HOME_ROOT))),
            Step("create the hands group", (_abs("groupadd"), "--system", "--gid", str(hands_id), hands)),
            Step("create the hands user (no password, no login shell)",
                 (_abs("useradd"), "--system", "--uid", str(hands_id), "--gid", str(hands_id),
                  "--home-dir", str(home), "--create-home", "--shell", _nologin(),
                  "--comment", HANDS_MARKER, hands)),
            Step("make the home private", (_abs("chmod"), "700", str(home))),
            Step("create the shared workspace root", (_abs("mkdir"), "-p", "-m", "755", str(root))),
            Step("create the entity's workspace directory, yours", (_abs("mkdir"), "-m", "700", str(ws_parent))),
            Step("create the workspace, yours", (_abs("mkdir"), "-m", "700", str(ws))),
            Step("own them as you", (_abs("chown"), opg, str(ws_parent), str(ws))),
            Step("let the hands user pass through to the workspace",
                 (_abs("setfacl"), "-m", f"u:{hands_id}:x", str(ws_parent))),
            Step("let both users edit the workspace (ACL)",
                 (_abs("setfacl"), "-m", f"u:{hands_id}:rwX,u:{operator}:rwX", str(ws))),
            Step("make new files in the workspace inherit it (default ACL)",
                 (_abs("setfacl"), "-d", "-m", f"u:{hands_id}:rwX,u:{operator}:rwX", str(ws))),
        ]
    else:  # pragma: no cover - guarded by the caller
        raise HandsSetupError(f"unsupported host {host!r}")

    as_hands = ("/usr/bin/sudo", "-u", hands, "/usr/bin/env", "-i", f"HOME={home}", f"PATH={SECURE_PATH}")
    git, keygen = _abs("git"), _abs("ssh-keygen")
    key = deploy_key_path(home)
    steps += [
        # The hands user's git runs only inside its confined shell, so trusting every repository
        # costs nothing a hook could not already do there; it works on any git version (the
        # `<dir>/*` form needs git 2.46). The OPERATOR gets no safe.directory entry.
        Step("let the hands user's git use repositories it does not own",
             (*as_hands, git, "config", "--global", "safe.directory", "*")),
        Step("create the hands user's .ssh", (*as_hands, "/bin/mkdir", "-p", "-m", "700", str(key.parent))),
        Step("generate the entity's own ssh key (register it as a deploy key)",
             (*as_hands, keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"levain {hands}", "-f", str(key)),
             skip_if=("/bin/test", "-e", str(key))),
    ]
    for gkey, value in sorted((git_identity or {}).items()):
        steps.append(Step(f"give the hands user your git {gkey}", (*as_hands, git, "config", "--global", gkey, value)))
    steps.append(Step(
        "allow you to run commands as the hands user, and nothing else (sudoers drop-in)",
        write=(sudoers_path(hands), sudoers_text(operator, hands), 0o440),
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
    workspace: Path | None,
) -> Plan:
    """The reverse of :func:`plan_setup`, ordered so nothing acts on the tree while the hands user
    can still change it: the sudoers rule goes first (no new processes), then every process of the
    hands user is killed and the kill is verified, and only then are files handed back and the
    account deleted. A deletion that fails stops the undo."""
    if not HANDS_USER_RE.match(hands_user):
        raise HandsSetupError(f"refusing to remove {hands_user!r}: not a Levain hands user name")
    ed = Path(entity_dir).expanduser().resolve()
    ws = workspace or hands_workspace(host, hands_user)
    steps: list[Step] = [
        Step("remove the sudoers drop-in (no new hands processes)", ("/bin/rm", "-f", str(sudoers_path(hands_user)))),
    ]
    if hands_id is not None:
        steps += [
            Step("stop every process of the hands user, and check they are gone", call=lambda: _kill_all(hands_id)),
            Step("remove the hands user's cron jobs", (_abs("crontab"), "-r", "-u", hands_user), allow_fail=True),
            Step(f"give the files the hands user created back to {operator}",
                 call=lambda: _chown_back(ws.parent, hands_id, f"{operator}:{operator_gid}")),
        ]
    if host == "darwin":
        steps += [
            Step("remove the workspace ACLs",
                 call=lambda: _run_ok(("/bin/chmod", "-R", "-N", str(ws.parent)), missing_ok=True)),
            Step("delete the hands user", ("/usr/bin/dscl", ".", "-delete", f"/Users/{hands_user}"),
                 skip_if=_absent("user", host, hands_user)),
            Step("delete the hands group", ("/usr/bin/dscl", ".", "-delete", f"/Groups/{hands_user}"),
                 skip_if=_absent("group", host, hands_user)),
            Step("delete the hands home", ("/bin/rm", "-rf", str(Path("/Users") / hands_user))),
        ]
    else:
        steps += [
            Step("remove the workspace ACLs",
                 call=lambda: _run_ok((_abs("setfacl"), "-R", "-b", str(ws.parent)), missing_ok=True)),
            Step("delete the hands user and its home", (_abs("userdel"), "--remove", hands_user),
                 skip_if=_absent("user", host, hands_user)),
            Step("delete the hands group", (_abs("groupdel"), hands_user), skip_if=_absent("group", host, hands_user)),
            Step("remove the hands homes directory if empty", (_abs("rmdir"), str(_LINUX_HANDS_HOME_ROOT)),
                 allow_fail=True),
        ]
    steps += [
        Step("remove the sshd drop-in", ("/bin/rm", "-f", str(sshd_dropin_path(hands_user)))),
        *[Step(f"remove the hands user from {deny}", call=lambda p=deny: _ensure_line(p, hands_user, present=False))
          for deny in _CRON_DENY[host]],
        Step("remove the workspace if it is empty", call=lambda: _remove_if_empty(ws)),
    ]
    if hands_id is not None:
        steps.append(Step("retire the user id so no later hands user reuses it",
                          call=lambda: _ensure_line(_RETIRED_IDS[host], str(hands_id), present=True, create=True)))
    return Plan(host, operator, hands_user, hands_id if hands_id is not None else -1, ed, ws, tuple(steps))


# --- step bodies that are not a single command ----------------------------------------------------


def _run_ok(argv: tuple[str, ...], *, missing_ok: bool = False) -> tuple[bool, str]:
    if missing_ok and not os.path.lexists(argv[-1]):
        return True, "already gone"
    r = subprocess.run(argv, capture_output=True, text=True, cwd="/")
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _kill_all(uid: int, *, attempts: int = 20) -> tuple[bool, str]:
    for _ in range(attempts):
        subprocess.run([_abs("pkill"), "-KILL", "-U", str(uid)], capture_output=True, cwd="/")
        if subprocess.run([_abs("pgrep"), "-U", str(uid)], capture_output=True, cwd="/").returncode == 1:
            return True, ""
        time.sleep(0.25)
    return False, f"processes of uid {uid} are still running"


def _chown_back(tree: Path, uid: int, owner: str) -> tuple[bool, str]:
    """Hand every file ``uid`` owns under ``tree`` to ``owner`` (user:group). Runs only after
    :func:`_kill_all` succeeded, so nothing can swap a directory for a symlink under it."""
    if not tree.exists():
        return True, "no workspace"
    return _run_ok((_abs("find"), str(tree), "-uid", str(uid), "-exec", _abs("chown"), "-h", owner, "{}", "+"))


def _remove_if_empty(ws: Path) -> tuple[bool, str]:
    if not ws.exists():
        return True, "already gone"
    try:
        ws.rmdir()
        ws.parent.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return True, f"kept: {ws} has files in it; it is yours, delete it when you no longer need them"
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
                                 capture_output=True, text=True, check=True).stdout
            used.update(int(p[-1]) for p in (ln.split() for ln in out.splitlines())
                        if p and p[-1].lstrip("-").isdigit())
        return used
    for db in ("passwd", "group"):
        out = subprocess.run([_abs("getent"), db], capture_output=True, text=True, check=True).stdout
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
                            "config", "--global", "--get", key], capture_output=True, text=True, cwd="/")
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
                         capture_output=True, text=True).stdout
    return HANDS_MARKER in out


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
        if step.skip_if and subprocess.run(step.skip_if, capture_output=True, cwd="/").returncode == 0:
            emit("      already done")
            continue
        if step.write is not None:
            ok, why = _install_file(*step.write, validate=step.validate, check=step.call)
        elif step.call is not None:
            ok, why = step.call()
        else:
            r = subprocess.run(step.argv, capture_output=True, text=True, cwd="/")
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
            r = subprocess.run([*validate, tmp], capture_output=True, text=True)
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

RECORD_KEYS = ("hands_user", "hands_uid", "hands_workspace")


def record_hands(entity_dir: Path, values: dict | None, *, owner_uid: int, owner_gid: int) -> Path:
    """Set (or, with ``None``, remove) the hands keys in ``<entity>/.levain/confinement.json``,
    keeping every other key, owned by the operator. Refuses to write through a symlink."""
    levain_dir = Path(entity_dir) / ".levain"
    cfg = levain_dir / "confinement.json"
    if levain_dir.is_symlink() or cfg.is_symlink():
        raise HandsSetupError(f"refusing to write {cfg}: it is reached through a symlink")
    data: dict = {}
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HandsSetupError(f"{cfg} is not a JSON object; fix it before running setup-isolation")
    for k in RECORD_KEYS:
        data.pop(k, None)
    data.update(values or {})
    fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.chown(cfg, owner_uid, owner_gid)
    return cfg


def _operator_path_under_home(operator: str) -> list[str]:
    home = _home_of(operator)
    path = os.environ.get("PATH", "")
    return [d for d in path.split(os.pathsep) if d and (d == home or d.startswith(home + os.sep))]


# --- the command ----------------------------------------------------------------------------------


def cmd_setup_isolation(path: Path | str, *, undo: bool, dry_run: bool) -> int:
    """``levain setup-isolation [--undo] [--dry-run]``. Returns a process exit code."""
    from levain.firing.confinement import ConfinementError, load_confinement_config
    from levain.firing.isolation import IsolationError, guard_entity

    try:
        host = host_os()
        entity_dir, _crystal, _episodic = guard_entity(path)
        cfg = load_confinement_config(entity_dir)
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
        hands_id = cfg.hands_uid
        if hands_id is None and _user_exists(hands):
            hands_id = pwd.getpwnam(hands).pw_uid
        if not dry_run and _user_exists(hands) and not user_record_is_ours(hands, host):
            print(f"setup-isolation: refusing to remove {hands}: it exists but was not created by Levain.")
            return 1
        try:
            plan = plan_undo(entity_dir, operator=operator, host=host, hands_user=hands, hands_id=hands_id,
                             operator_gid=op.pw_gid, workspace=cfg.hands_workspace)
        except HandsSetupError as exc:
            print(f"setup-isolation: {exc}")
            return 1
        print(f"Removing hands isolation for {entity_dir} (user {hands}).")
        rc = run_plan(plan, dry_run=dry_run)
        if dry_run or rc != 0:
            return rc
        # Asked of the directory service in a fresh process: this process's getpwnam can keep
        # returning a just-deleted macOS account (measured on a CI runner).
        gone = subprocess.run(_absent("user", host, hands), capture_output=True, cwd="/").returncode == 0
        leftovers = [what for what, there in (
            (f"the user {hands}", not gone),
            (f"the sudoers rule {sudoers_path(hands)}", sudoers_path(hands).exists()),
        ) if there]
        if leftovers:
            print("setup-isolation: undo did not finish: " + "; ".join(leftovers) + " still present.")
            return 1
        record_hands(entity_dir, None, owner_uid=op.pw_uid, owner_gid=op.pw_gid)
        print("Done. The hands user is gone.")
        return 0

    if cfg.hands_user is not None:
        print(f"setup-isolation: {entity_dir} is already set up (user {cfg.hands_user}). "
              "Run with --undo first to set it up again.")
        return 1
    if _user_exists(derived):
        ours = user_record_is_ours(derived, host)
        print(f"setup-isolation: the user {derived} already exists "
              + ("(a previous setup that did not finish). Run with --undo, then again." if ours
                 else "(an account Levain did not create). Remove or rename it first."))
        return 1
    ws_parent = hands_workspace(host, derived).parent
    if os.path.lexists(ws_parent):
        print(f"setup-isolation: {ws_parent} already exists (files from an earlier setup). "
              "Move or delete it, then run setup again.")
        return 1
    try:
        hands_id = choose_id(host, used_ids(host), retired_ids(host))
        plan = plan_setup(entity_dir, operator=operator, host=host, hands_id=hands_id, operator_gid=op.pw_gid,
                          git_identity=operator_git_identity(operator),
                          sshd_dropins=sshd_reads_dropins(), deny_lists=existing_deny_lists(host))
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
                              "hands_workspace": str(plan.workspace)},
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
