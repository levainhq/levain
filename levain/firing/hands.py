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
_AT_SPOOL = {"darwin": Path("/usr/lib/cron/jobs"), "linux": Path("/var/spool/cron/atjobs")}
_CRON_DENY = {"darwin": (Path("/usr/lib/cron/cron.deny"), Path("/usr/lib/cron/at.deny")),
              "linux": (Path("/etc/cron.deny"), Path("/etc/at.deny"))}

#: The inherited ACL that lets the operator read the hands user's workspace (macOS): list, search
#: and read, and nothing that writes, creates or deletes.
_DARWIN_OPERATOR_READ = "list,search,readattr,readextattr,readsecurity,read,file_inherit,directory_inherit"


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
        steps += [
            Step("create the hands homes directory", (_abs("mkdir"), "-p", "-m", "755", str(_LINUX_HANDS_HOME_ROOT))),
            Step("create the hands group", (_abs("groupadd"), "--system", "--gid", str(hands_id), hands)),
            Step("create the hands user (no password, no login shell)",
                 (_abs("useradd"), "--system", "--uid", str(hands_id), "--gid", str(hands_id),
                  "--home-dir", str(home), "--create-home", "--shell", _nologin(),
                  "--comment", HANDS_MARKER, hands)),
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
        ]
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
) -> Plan:
    """The reverse of :func:`plan_setup`, ordered so nothing acts on the tree while the hands user
    can still change it: the sudoers rule goes first (no new processes), the id is retired, cron and
    at jobs are removed, and every process of the hands user is killed and the kill verified. Only
    then are files re-owned and the account deleted. A step that fails stops the undo.

    ``hands_id`` is the id the DIRECTORY SERVICE gives the user (never the config's, which the
    operator account can write); ``None`` when the user is already gone, and then nothing is killed
    and only files with no owner left are re-owned. The workspace is always the derived one."""
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
            Step("stop every process of the hands user, and check they are gone", call=lambda: _kill_all(hands_id)),
        ]
    steps += [
        Step(f"hand what the hands user owned to root, readable by your group (never to you)",
             call=lambda: _to_root(tree, hands_id, operator_gid)),
        Step("remove the workspace ACLs",
             call=lambda: _run_ok(("/bin/chmod", "-R", "-N", str(tree)) if host == "darwin"
                                  else (_abs("setfacl"), "-R", "-P", "-b", str(tree)), missing_ok=True)),
        Step("let your group read it, and empty the hooks and settings of the entity's repositories",
             call=lambda: _readable_and_sanitised(tree, operator_gid)),
    ]
    if host == "darwin":
        steps += [
            Step("delete the hands user", ("/usr/bin/dscl", ".", "-delete", f"/Users/{hands_user}"),
                 skip_if=_absent("user", host, hands_user)),
            Step("delete the hands group", ("/usr/bin/dscl", ".", "-delete", f"/Groups/{hands_user}"),
                 skip_if=_absent("group", host, hands_user)),
            Step("delete the hands home", ("/bin/rm", "-rf", str(Path("/Users") / hands_user))),
        ]
    else:
        steps += [
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


def _remove_owned(spool: Path, uid: int) -> tuple[bool, str]:
    if not spool.is_dir():
        return True, f"no {spool}"
    return _run_ok((_abs("find"), str(spool), "-maxdepth", "1", "-type", "f", "-uid", str(uid), "-delete"))


def _owned_selector(uid: int | None) -> tuple[str, ...]:
    """``find`` predicates for "what the hands user owned": by numeric uid, or (user already gone)
    by having no owner at all. Directories, and files with a single link only: a hard link the
    entity made to someone else's file is never re-owned or re-moded (it is not the entity's)."""
    who = ("-nouser",) if uid is None else ("-uid", str(uid))
    return ("(", "-type", "d", "-o", "-links", "1", ")", *who)


def _to_root(tree: Path, uid: int | None, operator_gid: int) -> tuple[bool, str]:
    """Re-own everything the hands user owned under ``tree`` to root, group = the operator's group.
    Nothing goes to the operator: a directory the entity filled (a repository under any name, a
    bare one included) would be trusted by the operator's git if the operator owned it. Runs only
    after :func:`_kill_all`, so nothing can swap a directory for a symlink under it."""
    if not tree.exists():
        return True, "no workspace"
    return _run_ok((_abs("find"), str(tree), *_owned_selector(uid),
                    "-exec", _abs("chown"), "-h", f"0:{operator_gid}", "{}", "+"))


def _git_dir_shaped(path: Path) -> bool:
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


def _readable_and_sanitised(tree: Path, operator_gid: int, *, root_uid: int = 0) -> tuple[bool, str]:
    """After the ACLs are cleared (a group chmod on a file with a Linux ACL edits the mask, which
    clearing discards, measured in CI): let the operator's group read what root now owns, and, in
    every directory shaped like a git directory, empty ``hooks/`` and cut ``config`` to the keys
    ``ws-git`` accepts, because root's own git trusts a root-owned repository."""
    if not tree.exists():
        return True, "no workspace"
    ok, why = _run_ok((_abs("find"), str(tree), "(", "-type", "d", "-o", "-links", "1", ")", "-uid", "0",
                       "-gid", str(operator_gid), "-exec", _abs("chmod"), "g+rX,g-w", "{}", "+"))
    if not ok:
        return ok, why
    from levain.firing.ws_git import CONFIG_ALLOWLIST

    repos: list[str] = []
    for root, dirs, _files in os.walk(tree):
        here = Path(root)
        if not _git_dir_shaped(here) or here.lstat().st_uid != root_uid:
            continue
        hooks = here / "hooks"
        if hooks.is_dir() and not hooks.is_symlink():
            for h in hooks.iterdir():
                if h.is_file() or h.is_symlink():
                    h.unlink()
        cfg = here / "config"
        if cfg.is_file() and not cfg.is_symlink():
            r = subprocess.run([_abs("git"), "config", "--file", str(cfg), "--list"], capture_output=True, text=True,
                               cwd="/", env={"PATH": SECURE_PATH, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_CONFIG_GLOBAL": "/dev/null"})
            kept = [ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln]
            kept = [(k, v) for k, v in kept if CONFIG_ALLOWLIST.match(k)]
            cfg.unlink()
            for k, v in kept:
                subprocess.run([_abs("git"), "config", "--file", str(cfg), "--add", k, v], capture_output=True, cwd="/")
            if cfg.exists():
                os.chown(cfg, root_uid, operator_gid)
                os.chmod(cfg, 0o640)
        repos.append(str(here.parent if here.name == ".git" else here))
        dirs[:] = []
    if repos:
        first = repos[0]
        return True, ("the entity's repositories are now root's, with no hooks and only basic settings; "
                      "your git will not use them directly. To keep the work: git -c safe.directory="
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
            return True, (f"kept: {ws} has files in it, now root's and readable by you; remove it with "
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
    return subprocess.run([_abs("git"), "--version"], capture_output=True, text=True, cwd="/").stdout.strip()


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
                            _abs("git"), "-C", str(repo), "status"], capture_output=True, text=True, cwd="/")
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
            try:
                ok, why = step.call()
            except Exception as exc:  # noqa: BLE001 — a step that raises is a failed step, not a crash mid-undo
                ok, why = False, f"{type(exc).__name__}: {exc}"
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
        if not HANDS_USER_RE.match(hands):
            print(f"setup-isolation: refusing to remove {hands!r}: not a Levain hands user name")
            return 1
        if cfg.hands_workspace is not None and cfg.hands_workspace != hands_workspace(host, hands):
            print(f"setup-isolation: the recorded workspace {cfg.hands_workspace} is not the one setup "
                  f"creates for {hands}; refusing (fix or remove the hands keys in confinement.json).")
            return 1
        hands_id: int | None = None
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
        try:
            plan = plan_undo(entity_dir, operator=operator, host=host, hands_user=hands, hands_id=hands_id,
                             operator_gid=op.pw_gid)
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
        hands_id = choose_id(host, used_ids(host), retired_ids(host))
        plan = plan_setup(entity_dir, operator=operator, host=host, hands_id=hands_id,
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
