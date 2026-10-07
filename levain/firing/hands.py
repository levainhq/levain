"""levain.firing.hands — a dedicated, unprivileged OS user for an entity's bash hand (M2).

WHY A SECOND USER. On macOS the kernel hands the full argv and ENVIRONMENT of every process to any
other process with the same uid (``KERN_PROCARGS2``; the only gate in xnu's
``is_procargs_content_read_permitted`` is the caller's uid). No Seatbelt rule closes it: a
``sysctl-read`` deny by name, by prefix, or in total was measured inert on macOS 26.6.2 (2026-10-07,
in a VM). Running the entity's bash as a different, unprivileged user does: the kernel refuses the
read (``EINVAL``), measured the same day with levain's shipped profile unchanged. The same uid change
puts the operator's ``0600`` files and the operator's home directory (``0750``, group ``staff``) out
of reach by ownership alone, so the Seatbelt floor becomes the second layer under them, not the only
one.

WHAT THIS MODULE DOES. It builds and runs the one-time, root-only setup (``sudo levain
setup-isolation``) and its exact reverse (``--undo``). The plan is computed as data first
(:func:`plan_setup`, :func:`plan_undo`), so tests check it without root and ``--dry-run`` prints it.
The spawn that USES the user lives in :mod:`levain.firing.confinement`.

The hands user:
  - is named ``_levain_<slug>_<hash>`` from the entity's resolved path, so two entities never share
    one, and a name that does not match :data:`HANDS_USER_RE` is never acted on;
  - has no password and no login shell, is hidden from the login window (macOS), and its primary
    group is its own group, never ``staff`` (macOS) or a shared users group. Being outside ``staff``
    is what keeps it out of the operator's home directory;
  - is named, with the operator, in an inherited ACL on the workspace, so both users can edit each
    other's files there. The ACL names the two USERS rather than a shared group: a group added to
    the operator's account reaches its running processes only after a new login on Linux (measured
    in CI: the operator could not write a file the hands user created).

The privilege it gets the operator is one sudoers line, ``<operator> ALL=(<hands>) NOPASSWD: ALL``:
the operator may run commands AS the hands user. That is no escalation for the operator (the hands
user can do strictly less), and there is no rule FOR the hands user, which has no password, so sudo
from inside the hands always fails. sudo's default ``env_reset`` stays on.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import pwd
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

HostOS = Literal["darwin", "linux"]

#: Every name this module creates or deletes matches this; anything else is refused.
HANDS_USER_RE = re.compile(r"^_levain_[a-z0-9]{1,12}_[0-9a-f]{6}$")

#: Stamped on the user record (macOS RealName, Linux GECOS) so ``--undo`` deletes only a user this
#: command created, even if a name collides.
HANDS_MARKER = "Levain hands user"

#: macOS ids are taken from this range: below 500, so the account sorts with the system service
#: accounts. Hiding it from the login window is done explicitly (``IsHidden``), not by the range.
_DARWIN_ID_RANGE = range(300, 500)

_SUDOERS_DIR = Path("/etc/sudoers.d")
_LINUX_HANDS_HOME_ROOT = Path("/var/lib/levain-hands")

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


def sudoers_path(hands_user: str) -> Path:
    # sudo skips files in sudoers.d whose name contains a "." or ends in "~"; this name has neither.
    return _SUDOERS_DIR / f"levain-{hands_user}"


def sudoers_text(operator: str, hands_user: str) -> str:
    return (
        f"# Written by `levain setup-isolation`; removed by `levain setup-isolation --undo`.\n"
        f"# {operator} may run commands as the Levain hands user {hands_user}, and nothing else.\n"
        f"Defaults:{operator} !requiretty\n"
        f"{operator} ALL=({hands_user}) NOPASSWD: ALL\n"
    )


@dataclass(frozen=True)
class Step:
    """One action, as data. Exactly one of ``argv`` / ``write`` is set.

    ``skip_if`` is an argv run first; exit 0 means the step's effect is already present and the step
    is skipped (so a re-run, or an undo after a partial setup, does not fail on what exists)."""

    why: str
    argv: tuple[str, ...] = ()
    write: tuple[Path, str, int] | None = None   # (path, content, mode), owned root
    validate: tuple[str, ...] = ()               # run on the temp copy of ``write`` before install
    skip_if: tuple[str, ...] = ()
    allow_fail: bool = False                     # an undo step whose target may already be gone


@dataclass(frozen=True)
class Plan:
    host: HostOS
    operator: str
    hands_user: str
    group: str
    entity_dir: Path
    workspace: Path
    steps: tuple[Step, ...] = field(default_factory=tuple)


def _darwin_free_id(used: set[int]) -> int:
    for candidate in _DARWIN_ID_RANGE:
        if candidate not in used:
            return candidate
    raise HandsSetupError(
        f"no free user/group id in {_DARWIN_ID_RANGE.start}-{_DARWIN_ID_RANGE.stop - 1}"
    )


def _git_safe_dir_steps(run_as: str, home: str, value: str, *, add: bool) -> list[Step]:
    base = ("/usr/bin/sudo", "-u", run_as, "/usr/bin/env", f"HOME={home}", "git", "config", "--global")
    has = (*base, "--fixed-value", "--get-all", "safe.directory", value)
    if add:
        return [Step(f"trust the workspace in {run_as}'s git (safe.directory)",
                     (*base, "--add", "safe.directory", value), skip_if=has)]
    return [Step(f"remove the workspace trust from {run_as}'s git",
                 (*base, "--fixed-value", "--unset-all", "safe.directory", value), allow_fail=True)]


def deploy_key_path(hands_home: str | Path) -> Path:
    return Path(hands_home) / ".ssh" / "id_ed25519"


def _deploy_key_steps(hands: str, hands_home: str) -> list[Step]:
    """S3: the entity's own ssh key, generated by and owned by the hands user. The operator's
    ssh-agent refuses a client running as another user (OpenSSH checks the peer's uid), so the hands
    authenticate with this key instead; registering it as a deploy key limits it to the repositories
    the operator chooses."""
    key = deploy_key_path(hands_home)
    as_hands = ("/usr/bin/sudo", "-u", hands, "/usr/bin/env", f"HOME={hands_home}")
    return [
        Step("create the hands user's .ssh", (*as_hands, "mkdir", "-p", "-m", "700", str(key.parent))),
        Step("generate the entity's own ssh key (register it as a deploy key)",
             (*as_hands, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"levain {hands}", "-f", str(key)),
             skip_if=("test", "-e", str(key))),
    ]


def plan_setup(
    entity_dir: Path | str,
    *,
    operator: str,
    host: HostOS,
    used_ids: set[int] | None = None,
    hands_home_darwin: Path | None = None,
) -> Plan:
    """The setup, as data. ``used_ids`` (macOS only) is every UniqueID and PrimaryGroupID in use."""
    ed = Path(entity_dir).expanduser().resolve()
    ws = ed / "workspace"
    hands = hands_user_name(ed)
    group = hands
    safe_value = f"{ws}/*"
    steps: list[Step] = []
    if host == "darwin":
        uid = _darwin_free_id(used_ids or set())
        home = hands_home_darwin or Path("/Users") / hands
        dscl = ("/usr/bin/dscl", ".")
        steps += [
            Step("create the entity group", (*dscl, "-create", f"/Groups/{group}")),
            Step("set the group id", (*dscl, "-create", f"/Groups/{group}", "PrimaryGroupID", str(uid))),
            Step("label the group", (*dscl, "-create", f"/Groups/{group}", "RealName", HANDS_MARKER)),
            Step("create the hands user", (*dscl, "-create", f"/Users/{hands}")),
            Step("set its user id", (*dscl, "-create", f"/Users/{hands}", "UniqueID", str(uid))),
            Step("make the entity group its primary group (not staff)",
                 (*dscl, "-create", f"/Users/{hands}", "PrimaryGroupID", str(uid))),
            Step("give it no login shell", (*dscl, "-create", f"/Users/{hands}", "UserShell", "/usr/bin/false")),
            Step("set its home", (*dscl, "-create", f"/Users/{hands}", "NFSHomeDirectory", str(home))),
            Step("label the user", (*dscl, "-create", f"/Users/{hands}", "RealName", HANDS_MARKER)),
            Step("hide it from the login window", (*dscl, "-create", f"/Users/{hands}", "IsHidden", "1")),
            Step("give it no password", (*dscl, "-create", f"/Users/{hands}", "Password", "*")),
            Step("add the hands user to the entity group",
                 ("/usr/sbin/dseditgroup", "-o", "edit", "-a", hands, "-t", "user", group)),
            Step("create the hands home", ("/bin/mkdir", "-p", str(home))),
            Step("give the home to the hands user", ("/usr/sbin/chown", f"{hands}:{group}", str(home))),
            Step("make the home private", ("/bin/chmod", "700", str(home))),
            Step("hide the home in Finder", ("/usr/bin/chflags", "hidden", str(home))),
            Step("create the workspace", ("/bin/mkdir", "-p", str(ws))),
            Step("let the hands user edit the workspace (inherited ACL)",
                 ("/bin/chmod", "-R", "+a", f"user:{hands} allow {_DARWIN_ACL_PERMS}", str(ws))),
            Step(f"let {operator} edit what the hands user creates there (inherited ACL)",
                 ("/bin/chmod", "-R", "+a", f"user:{operator} allow {_DARWIN_ACL_PERMS}", str(ws))),
        ]
        hands_home = str(home)
    elif host == "linux":
        home = _LINUX_HANDS_HOME_ROOT / hands
        steps += [
            Step("create the hands homes directory", ("mkdir", "-p", str(_LINUX_HANDS_HOME_ROOT))),
            Step("create the entity group", ("groupadd", "--system", group)),
            Step("create the hands user (no password, no login shell)",
                 ("useradd", "--system", "--gid", group, "--home-dir", str(home), "--create-home",
                  "--shell", "/usr/sbin/nologin", "--comment", HANDS_MARKER, hands)),
            Step("make the home private", ("chmod", "700", str(home))),
            Step("create the workspace", ("mkdir", "-p", str(ws))),
            Step("let both users edit the workspace (ACL)",
                 ("setfacl", "-R", "-m", f"u:{hands}:rwX,u:{operator}:rwX", str(ws))),
            Step("make new files in the workspace inherit it (default ACL)",
                 ("find", str(ws), "-type", "d", "-exec", "setfacl", "-d", "-m",
                  f"u:{hands}:rwX,u:{operator}:rwX", "{}", "+")),
        ]
        hands_home = str(home)
    else:  # pragma: no cover - guarded by the caller
        raise HandsSetupError(f"unsupported host {host!r}")
    steps += _deploy_key_steps(hands, hands_home)
    steps.append(Step(
        "allow the operator to run commands as the hands user (sudoers drop-in)",
        write=(sudoers_path(hands), sudoers_text(operator, hands), 0o440),
        validate=("visudo", "-cf"),
    ))
    steps += _git_safe_dir_steps(hands, hands_home, safe_value, add=True)
    op_home = _home_of(operator)
    steps += _git_safe_dir_steps(operator, op_home, safe_value, add=True)
    return Plan(host, operator, hands, group, ed, ws, tuple(steps))


def plan_undo(entity_dir: Path | str, *, operator: str, host: HostOS, hands_user: str) -> Plan:
    """The exact reverse of :func:`plan_setup`. Every step tolerates its target being gone already,
    so an undo after a partial setup completes."""
    if not HANDS_USER_RE.match(hands_user):
        raise HandsSetupError(f"refusing to remove {hands_user!r}: not a Levain hands user name")
    ed = Path(entity_dir).expanduser().resolve()
    ws = ed / "workspace"
    group = hands_user
    safe_value = f"{ws}/*"
    steps: list[Step] = [Step("remove the sudoers drop-in", ("rm", "-f", str(sudoers_path(hands_user))))]
    # Before the user is deleted: what it created in the workspace (files, and .git objects in the
    # operator's repos) would otherwise be owned by a dead uid, and with the group gone too the
    # operator could no longer write to their own repository.
    steps.append(Step(f"give the files the hands user created back to {operator}",
                      ("find", str(ws), "-user", hands_user, "-exec", "chown", "-h", operator, "{}", "+"),
                      allow_fail=True))
    op_home = _home_of(operator)
    steps += _git_safe_dir_steps(operator, op_home, safe_value, add=False)
    if host == "darwin":
        dscl = ("/usr/bin/dscl", ".")
        home = _home_of(hands_user, default=str(Path("/Users") / hands_user))
        steps += [
            Step("remove the hands user's workspace ACL",
                 ("/bin/chmod", "-R", "-a", f"user:{hands_user} allow {_DARWIN_ACL_PERMS}", str(ws)), allow_fail=True),
            Step(f"remove {operator}'s workspace ACL",
                 ("/bin/chmod", "-R", "-a", f"user:{operator} allow {_DARWIN_ACL_PERMS}", str(ws)), allow_fail=True),
            Step("delete the hands user", (*dscl, "-delete", f"/Users/{hands_user}"), allow_fail=True),
            Step("delete the entity group", (*dscl, "-delete", f"/Groups/{group}"), allow_fail=True),
            Step("delete the hands home", ("/bin/rm", "-rf", home), allow_fail=True),
        ]
    elif host == "linux":
        steps += [
            Step("remove the workspace ACL",
                 ("setfacl", "-R", "-x", f"u:{hands_user},u:{operator}", str(ws)), allow_fail=True),
            Step("remove the default workspace ACL",
                 ("find", str(ws), "-type", "d", "-exec", "setfacl", "-d", "-x",
                  f"u:{hands_user},u:{operator}", "{}", "+"), allow_fail=True),
            Step("delete the hands user and its home", ("userdel", "--remove", hands_user), allow_fail=True),
            Step("delete the entity group", ("groupdel", group), allow_fail=True),
        ]
    return Plan(host, operator, hands_user, group, ed, ws, tuple(steps))


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


def darwin_used_ids() -> set[int]:
    used: set[int] = set()
    for kind, attr in (("/Users", "UniqueID"), ("/Groups", "PrimaryGroupID")):
        out = subprocess.run(["/usr/bin/dscl", ".", "-list", kind, attr],
                             capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[-1].lstrip("-").isdigit():
                used.add(int(parts[-1]))
    return used


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
    pwd.getpwnam(operator)  # KeyError → the caller reports it
    return operator


def user_record_is_ours(hands_user: str, host: HostOS) -> bool:
    """True when ``hands_user`` exists and carries :data:`HANDS_MARKER`. ``--undo`` deletes a user
    only then."""
    try:
        entry = pwd.getpwnam(hands_user)
    except KeyError:
        return False
    if host == "linux":
        return entry.pw_gecos.split(",")[0] == HANDS_MARKER
    out = subprocess.run(["/usr/bin/dscl", ".", "-read", f"/Users/{hands_user}", "RealName"],
                         capture_output=True, text=True).stdout
    return HANDS_MARKER in out


def run_plan(plan: Plan, *, dry_run: bool, emit: Callable[[str], None] = print) -> int:
    """Run the steps in order. Stops at the first failing step that is not ``allow_fail`` and returns
    1; returns 0 when every step ran or was skipped."""
    for i, step in enumerate(plan.steps, 1):
        label = f"[{i}/{len(plan.steps)}] {step.why}"
        if step.write is not None:
            path, content, mode = step.write
            emit(f"{label}\n      write {path} (mode {mode:o})")
        else:
            emit(f"{label}\n      {' '.join(step.argv)}")
        if dry_run:
            continue
        # cwd "/": a command run as the hands user dies on a working directory it cannot read
        # (measured: git fatals on the operator's home).
        if step.skip_if and subprocess.run(step.skip_if, capture_output=True, cwd="/").returncode == 0:
            emit("      already done")
            continue
        if step.write is not None:
            ok, why = _install_file(*step.write, validate=step.validate)
        else:
            r = subprocess.run(step.argv, capture_output=True, text=True, cwd="/")
            ok, why = r.returncode == 0, (r.stderr or r.stdout).strip()
        if not ok:
            if step.allow_fail:
                emit(f"      skipped ({why or 'nothing to remove'})")
                continue
            emit(f"      FAILED: {why}")
            return 1
    return 0


def _install_file(path: Path, content: str, mode: int, *, validate: tuple[str, ...]) -> tuple[bool, str]:
    """Write ``content`` to a temp file beside ``path``, validate it, then move it into
    place with ``mode``, owned by root. A drop-in that fails validation is never installed: a broken
    sudoers.d file breaks sudo for the whole machine."""
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
        return True, ""
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def record_hands_user(entity_dir: Path, hands_user: str | None, *, owner_uid: int, owner_gid: int) -> Path:
    """Set (or, with ``None``, remove) ``hands_user`` in ``<entity>/.levain/confinement.json``,
    keeping every other key, and leave the file owned by the operator."""
    cfg = Path(entity_dir) / ".levain" / "confinement.json"
    data: dict = {}
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise HandsSetupError(f"{cfg} is not a JSON object; fix it before running setup-isolation")
    if hands_user is None:
        data.pop("hands_user", None)
    else:
        data["hands_user"] = hands_user
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chown(cfg, owner_uid, owner_gid)
    return cfg



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
    except (HandsSetupError, IsolationError, ConfinementError) as exc:
        print(f"setup-isolation: {exc}")
        return 1
    except KeyError as exc:
        print(f"setup-isolation: no such account {exc}")
        return 1

    derived = hands_user_name(entity_dir)
    if undo:
        hands = cfg.hands_user or derived
        if not dry_run and _user_exists(hands) and not user_record_is_ours(hands, host):
            print(f"setup-isolation: refusing to remove {hands}: it exists but was not created by Levain.")
            return 1
        plan = plan_undo(entity_dir, operator=operator, host=host, hands_user=hands)
        print(f"Removing hands isolation for {entity_dir} (user {hands}).")
        rc = run_plan(plan, dry_run=dry_run)
        if rc == 0 and not dry_run:
            op = pwd.getpwnam(operator)
            record_hands_user(entity_dir, None, owner_uid=op.pw_uid, owner_gid=op.pw_gid)
            print("Done. Headless chat and unattended runs now execute bash as you again.")
        return rc

    if cfg.hands_user is not None:
        print(f"setup-isolation: {entity_dir} is already set up (user {cfg.hands_user}). "
              "Run with --undo first to set it up again.")
        return 1
    if _user_exists(derived):
        what = "a previous setup that did not finish" if user_record_is_ours(derived, host) else \
            "an account Levain did not create"
        print(f"setup-isolation: the user {derived} already exists ({what}). "
              + ("Run with --undo, then again." if what.startswith("a previous") else "Remove or rename it first."))
        return 1
    plan = plan_setup(entity_dir, operator=operator, host=host,
                      used_ids=darwin_used_ids() if host == "darwin" else None)
    print(f"Setting up hands isolation for {entity_dir}: bash will run as {plan.hands_user}.")
    rc = run_plan(plan, dry_run=dry_run)
    if rc != 0:
        print("Setup stopped part way. Run `sudo levain setup-isolation --undo"
              f" --path {entity_dir}` to remove what was created.")
        return rc
    if not dry_run:
        op = pwd.getpwnam(operator)
        record_hands_user(entity_dir, plan.hands_user, owner_uid=op.pw_uid, owner_gid=op.pw_gid)
        print(f"Done. Headless chat and unattended runs execute bash as {plan.hands_user}; "
              "the interactive `levain run` REPL still uses your account.")
        pub = deploy_key_path(_home_of(plan.hands_user)).with_suffix(".pub")
        try:
            print("\nThe entity has its own ssh key. To let it push, add this public key as a deploy "
                  "key (with write access) on each repository it should reach:\n")
            print("    " + pub.read_text(encoding="utf-8").strip())
        except OSError as exc:
            print(f"(could not read {pub}: {exc})")
    return 0


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False
