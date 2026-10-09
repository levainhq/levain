"""levain.firing.ws_git — the operator's ways into a hands workspace (M2, H then A).

THE RULE (Phill, 2026-10-07: "go with H", then (A): "technically the best way for an augmentation
operator to be editing these files is through their entity anyways"). The workspace and everything
in it belong to the hands user; the operator can read it and nothing more. Git refuses to read the
config of, or run the hooks of, a repository its user does not own ("By default, Git will refuse to
even parse a Git config of a repository owned by someone else, let alone run its hooks",
git-config(1), safe.directory), so the operator's own git refuses every repository there. And
because the operator owns no directory there, the entity has nowhere to build a repository the
operator's git WOULD trust: git checks a bare repository's ownership on that directory alone, so an
operator-owned folder the entity could write was enough for one. That holds while nothing in the
workspace lets the operator write: the hands user owns every directory and could open one up, and
then the operator's own tools would create folders there. ``levain doctor`` fails on any entry the
operator can write, as well as on any the hands user does not own.

The operator changes the workspace through the entity, or through four doors, none of which runs
anything the entity wrote as the operator:
  - the remote: the entity pushes with its deploy key;
  - ``levain ws-git``: git AS THE HANDS USER, the repository's executable config switched off, and
    refused while a session of the entity is open (the hands lock) or its user has a process running;
  - ``levain ws-put``: one file copied in as data, written by the hands user;
  - ``levain ws-adopt``: a repository of the operator's imported from where it sits, outside the
    workspace, as a new repository the hands user owns.
"""
from __future__ import annotations

import os
import platform
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from levain.firing.hands import SECURE_PATH, WORKSPACE_ROOT, _abs
from levain.launch import child_env

#: The only config keys ``ws-git`` accepts in a repository's own config. Every other key (fsmonitor,
#: hooksPath, pager, editor, sshCommand, alias.*, filter.*, diff.*, include*, credential.*, ...) can
#: name a program, so a repository carrying one is refused rather than filtered.
CONFIG_ALLOWLIST = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)"
    r"|remote\.[^.]+\.(url|fetch)|branch\.[^.]+\.(remote|merge)|extensions\.objectformat)$"
)
_URL_OK = re.compile(r"^(https://|ssh://|git@[A-Za-z0-9.-]+:)")

#: Passed on every ``ws-git`` command line, after the repository config is checked: belt and braces
#: for the program-naming keys git also reads from places the allowlist does not see.
_NEUTRALISE = (
    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=", "-c", "core.pager=cat",
    "-c", "core.editor=false", "-c", "core.sshCommand=/usr/bin/ssh", "-c", "credential.helper=",
    "-c", "core.askPass=", "-c", "core.gitProxy=", "-c", "gpg.program=false",
    "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false", "-c", "safe.bareRepository=explicit",
    "-c", "protocol.ext.allow=never", "-c", "core.alternateRefsCommand=",
)


def _real_git() -> str:
    """The git binary itself. On macOS ``/usr/bin/git`` is an xcrun shim whose lookup could be
    steered by the user running it (the hands user); resolve the real binary once, here, and
    accept it only if root owns it and nobody else can write it."""
    import platform
    import stat

    if platform.system() == "Darwin":
        r = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True, cwd="/", env=child_env())
        cand = r.stdout.strip()
        if r.returncode == 0 and cand:
            try:
                st = os.stat(cand)
                if st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    return cand
            except OSError:
                pass
    return _abs("git")


class WsGitError(RuntimeError):
    """ws-git / ws-adopt refused."""


@dataclass(frozen=True)
class Hands:
    user: str
    uid: int
    home: str
    workspace: Path


def load_hands(entity_dir: Path | str) -> Hands:
    from levain.firing.confinement import load_confinement_config

    cfg = load_confinement_config(Path(entity_dir).expanduser())
    if cfg.hands_user is None or cfg.hands_uid is None or cfg.hands_workspace is None:
        raise WsGitError("this entity has no hands user; run `sudo levain setup-isolation` first")
    try:
        home = pwd.getpwnam(cfg.hands_user).pw_dir
    except KeyError:
        raise WsGitError(f"the hands user {cfg.hands_user} does not exist") from None
    return Hands(cfg.hands_user, cfg.hands_uid, home, cfg.hands_workspace)


def find_gitdir(start: Path, workspace: Path) -> Path:
    """The ``.git`` DIRECTORY of the repository containing ``start``, inside ``workspace``. Found by
    walking the tree, never by asking git (git would parse the repository's config to answer)."""
    ws = workspace.resolve()
    cur = start.resolve()
    if cur != ws and ws not in cur.parents:
        raise WsGitError(f"{start} is not inside the hands workspace {workspace}")
    while True:
        cand = cur / ".git"
        if os.path.lexists(cand):
            if cand.is_symlink() or not cand.is_dir():
                raise WsGitError(f"{cand} is not a plain directory (a gitfile or a link); refusing")
            return cand
        if cur == ws:
            raise WsGitError(f"no repository at {start}")
        cur = cur.parent


def check_repo(gitdir: Path, hands_uid: int) -> None:
    """Refuse a repository that is not the hands user's, or whose own config could name a program.
    Read with ``git config --file``, which parses the file and executes nothing."""
    st = gitdir.lstat()
    if st.st_uid != hands_uid:
        raise WsGitError(f"{gitdir} does not belong to the hands user; nothing in the workspace should be yours "
                         "(`levain doctor` lists it)")
    for name in ("commondir", "worktrees", "modules"):
        if os.path.lexists(gitdir / name):
            raise WsGitError(f"{gitdir}/{name} present (linked worktrees and submodules are not supported)")
    if (gitdir / "objects" / "info" / "alternates").exists() and (gitdir / "objects" / "info" / "alternates").stat().st_size:
        raise WsGitError(f"{gitdir} borrows objects from elsewhere (alternates); refusing")
    keys = _config_lines(gitdir / "config", "--list", "--name-only")
    bad = [k for k in keys if not CONFIG_ALLOWLIST.match(k)]
    if bad:
        raise WsGitError(f"{gitdir}/config sets {bad[0]}, which can name a program; refusing")
    for line in _config_lines(gitdir / "config", "--get-regexp", r"^remote\..*\.url$"):
        url = line.split(" ", 1)[-1]
        if not _URL_OK.match(url):
            raise WsGitError(f"{gitdir}/config has a remote URL Levain does not accept ({url.split(':', 1)[0]}:...)")


def _config_lines(config: Path, *args: str) -> list[str]:
    r = subprocess.run(
        [_abs("git"), "config", "--file", str(config), *args],
        capture_output=True, text=True, cwd="/",
        env={"PATH": SECURE_PATH, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    if r.returncode not in (0, 1):  # 1 = no matching key
        raise WsGitError(f"cannot read {config}: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


#: The git verbs that reach a remote. On Linux ws-git runs them with the hands user's net group,
#: the one gid its egress boundary lets out (D, Phill 2026-10-09), only when the verb is the FIRST
#: argument: anything before it (a global option) and the command keeps the hands user's own group,
#: with no network at all. A network verb not listed here is refused by the boundary, not let out.
NET_VERBS = frozenset({"push", "fetch", "pull", "ls-remote"})


def ws_git_argv(hands: Hands, gitdir: Path, args: list[str], *, system: str | None = None) -> list[str]:
    """``git`` as the hands user, on exactly the git directory that was checked (``--git-dir`` and
    ``--work-tree``: no discovery, so git cannot fall through to another one), with no system or
    global config (the hands user writes its own ~/.gitconfig), the program-naming keys switched
    off, and a clean environment. ssh reads no config file and offers only the entity's deploy key:
    the hands user writes its own ~/.ssh/config, and a ProxyCommand there would run as it."""
    from levain.firing.hands import deploy_key_path, hands_net_group

    ssh = ("core.sshCommand=/usr/bin/ssh -F /dev/null -i " + shlex.quote(str(deploy_key_path(hands.home)))
           + " -o IdentitiesOnly=yes")
    net = (system or platform.system()) == "Linux" and bool(args) and args[0] in NET_VERBS
    # https and ssh only, for any URL on the command line as well as a configured remote (the config
    # check already admits only those): a local-path (file) transport runs --upload-pack /
    # --receive-pack on THIS machine, and for a network verb on Linux with the net gid (L3 r1).
    transports = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always")
    return _as_hands(
        hands, _real_git(), *_NEUTRALISE, *transports, "-c", ssh, f"--git-dir={gitdir}", f"--work-tree={gitdir.parent}",
        "-C", str(gitdir.parent), *args,
        env=("GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_PAGER=cat", "GIT_TERMINAL_PROMPT=0",
             "LANG=" + os.environ.get("LANG", "en_US.UTF-8")),
        group=hands_net_group(hands.user) if net else None,
    )


_KEEP = {9, 10}


def _sanitise(chunk: bytes) -> bytes:
    """Drop terminal control bytes (escape sequences included) from text the entity wrote; keep
    tab, newline and everything printable, UTF-8 included."""
    return bytes(b for b in chunk if b >= 0x20 and b != 0x7F or b in _KEEP)


def _run_relayed(argv: list[str]) -> int:
    """Run with no terminal at all (stdin /dev/null, a new session: nothing it runs can reach the
    operator's terminal) and relay its output with control characters removed."""
    import sys

    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True, cwd="/", env=child_env())
    assert proc.stdout is not None
    for chunk in iter(lambda: proc.stdout.read(65536), b""):
        sys.stdout.buffer.write(_sanitise(chunk))
    sys.stdout.flush()
    return proc.wait()


#: The per-user agents launchd starts for a uid on its own once it has run Apple code, measured on a
#: CI runner 2026-10-07 (exact paths), plus XPC services, which launchd alone starts. They live on
#: the sealed system volume, which no user can write.
_DARWIN_USER_AGENTS = frozenset({
    "/usr/sbin/cfprefsd", "/usr/sbin/distnoted", "/usr/libexec/lsd", "/usr/libexec/trustd",
    "/usr/libexec/secd", "/usr/libexec/containermanagerd", "/usr/libexec/secinitd",
    "/usr/libexec/UserEventAgent",
})
_DARWIN_XPC_SERVICE = re.compile(r"^/System/Library/[^\0]+\.xpc/Contents/MacOS/[^/]+$")


def _darwin_agent(ppid: str, path: str) -> bool:
    return ppid == "1" and (path in _DARWIN_USER_AGENTS or bool(_DARWIN_XPC_SERVICE.match(path)))


def proc_hides_processes(mountinfo: str) -> bool:
    """Whether ``/proc`` is mounted with ``hidepid`` (other users' processes invisible), from
    ``/proc/self/mountinfo`` text."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/proc" and re.search(r"hidepid=(?!0\b|off\b)", line):
            return True
    return False


#: How long a caller outside the session-end sweep gives :func:`entity_session_live` before it
#: cannot tell (a refusal).
LIVENESS_TIMEOUT = 10.0


def entity_session_live(hands_uid: int, *, timeout: float | None = None) -> bool:
    """Whether anything runs as the hands user right now. The workspace is the hands user's and the
    operator can only read it, so whatever can change it while ws-git works (an entity session's
    bash, anything it left running) runs as that user: this is the session's liveness measured where
    it cannot be missed, an orphaned background job included. Raises when it cannot tell (a process
    table the operator cannot see all of), so callers fail closed. It is checked once, when a command
    starts; a session started while the command runs is not seen. With ``timeout``, the whole check
    takes at most that long, and one that runs out cannot tell (raises)."""
    deadline = None if timeout is None else time.monotonic() + timeout

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        left = None if deadline is None else deadline - time.monotonic()
        try:
            if left is not None and left <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                  cwd="/", env=child_env(), timeout=left)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WsGitError(f"cannot tell whether the entity is running ({argv[0]}: {exc}); refusing") from None

    if platform.system() == "Linux":
        try:
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise WsGitError(f"cannot tell whether the entity is running ({exc}); refusing") from None
        if proc_hides_processes(mounts):
            raise WsGitError("cannot tell whether the entity is running (/proc hides other users' "
                             "processes); refusing")
    def pids() -> set[str]:
        r = run([_abs("pgrep"), "-U", str(hands_uid)])
        if r.returncode not in (0, 1):
            raise WsGitError(f"cannot tell whether the entity is running (pgrep: {r.stderr.strip() or r.returncode}); "
                             "refusing")
        return set(r.stdout.split())

    found = pids()
    if not found:
        return False
    if platform.system() != "Darwin":
        return True
    # macOS starts per-user system agents for any uid that has run Apple code (git and python3 in
    # /usr/bin are xcrun shims). Those, and only those, are not a session; an orphan of the
    # entity's is. The pid set is read again after ps: a process that exited and forked in between
    # would otherwise leave only agents in view.
    ps = run(["/bin/ps", "-o", "pid=,ppid=,comm=", "-p", ",".join(sorted(found))])
    rows = {}
    for ln in ps.stdout.splitlines():
        parts = ln.split(None, 2)
        if len(parts) == 3:
            rows[parts[0]] = (parts[1], parts[2].strip())
    if not all(_darwin_agent(ppid, path) for ppid, path in rows.values()):
        return True
    if set(rows) != found or pids() != found:
        raise WsGitError("cannot tell whether the entity is running (its processes changed while being read); "
                         "refusing")
    return False


#: Per entity, in the operator's tree (out of the hands user's reach). A session of a hands entity
#: holds it SHARED for its whole life (several chat sessions may run at once); ws-git and ws-adopt
#: hold it EXCLUSIVE for their whole run. So no session starts while they work, and they never start
#: while a session is open. Busy is a refusal on both sides, never a wait without end.
HANDS_LOCK = "hands.lock"


def _open_lock(entity_dir: Path | str) -> int:
    return os.open(Path(entity_dir) / ".levain" / HANDS_LOCK,
                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)


def hold_session_lock(entity_dir: Path | str, *, wait: float = 10.0) -> int:
    """Take the shared hands lock for a session; returns the fd, which the session closes when it
    ends. Waits up to ``wait`` seconds for a running ws-git or ws-adopt, then refuses."""
    import fcntl
    import time

    fd = _open_lock(entity_dir)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise WsGitError("levain ws-git or ws-adopt is working in this entity's workspace; "
                                 "start the session when it finishes") from None
            time.sleep(0.2)
        except BaseException:
            os.close(fd)
            raise


HANDS_SESSION_LOCK = "hands-session.lock"


def hold_hands_session(entity_dir: Path | str) -> int:
    """Take this entity's one-session lock, exclusive, for a session whose bash runs as the hands
    user; returns the fd, closed when the session ends. A second such session is refused at once: the
    session's end stops every process of the hands user (a command can leave its process group with
    ``setsid``), which is only safe while one session owns that user."""
    import fcntl

    fd = os.open(Path(entity_dir) / ".levain" / HANDS_SESSION_LOCK,
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise WsGitError("another session of this entity is running as its own user; one runs at a "
                         "time (start this one when it ends)") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


class _exclusive:
    """ws-git / ws-adopt: the hands lock, exclusive, for the whole run, or a refusal at once."""

    def __init__(self, entity_dir: Path | str) -> None:
        self.entity_dir = entity_dir
        self.fd: int | None = None

    def __enter__(self) -> "_exclusive":
        import fcntl

        self.fd = _open_lock(self.entity_dir)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise WsGitError("a session of this entity is open; this waits until it ends") from None
        except BaseException:
            os.close(self.fd)
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)


def _refuse_while_live(hands: Hands, what: str) -> None:
    if entity_session_live(hands.uid, timeout=LIVENESS_TIMEOUT):
        raise WsGitError(
            f"the entity's user {hands.user} has processes running (a session is live, or something it "
            f"left running); {what} waits until it stops, so the entity cannot rewrite what was checked")


def cmd_ws_git(entity_dir: Path | str, repo: Path | str, args: list[str]) -> int:
    try:
        hands = load_hands(entity_dir)
        with _exclusive(entity_dir):
            _refuse_while_live(hands, "ws-git")
            gitdir = find_gitdir(Path(repo), hands.workspace)
            check_repo(gitdir, hands.uid)
            return _run_relayed(ws_git_argv(hands, gitdir, args))
    except (WsGitError, OSError) as exc:
        print(f"ws-git: {exc}")
        return 1


def _as_hands(hands: Hands, *argv: str, env: tuple[str, ...] = (), group: str | None = None) -> list[str]:
    """``argv`` run as the hands user with a clean environment (and, with ``group``, that gid)."""
    return ["/usr/bin/sudo", "-n", "-u", hands.user, *(("-g", group) if group else ()), "/usr/bin/env", "-i",
            f"HOME={hands.home}", f"PATH={SECURE_PATH}", *env, *_start(), *argv]


def _start() -> list[str]:
    """Linux: the confinement start program in front of a hands process (a new session keyring; ended
    with levain), as for every other hands process."""
    import platform

    if platform.system() != "Linux":
        return []
    from levain.firing.confinement import HANDS_PYTHON, _start_argv

    return _start_argv(HANDS_PYTHON)


def _under_a_workspace_root(path: Path) -> bool:
    for root in WORKSPACE_ROOT.values():
        for p in (Path(os.path.abspath(path)), path.resolve()):
            if p == root or root in p.parents:
                return True
    return False


# --- ws-put ---------------------------------------------------------------------------------------

#: Run by the hands user's Python with -I -S (no site, no environment, nothing imported from the
#: workspace). argv: the workspace, then the destination's components. The data arrives on stdin.
#: Every directory is opened with O_NOFOLLOW from the one before it, so a symlink anywhere on the
#: path stops the write instead of redirecting it; the file is written beside its destination and
#: renamed over it, so a symlink at the destination is replaced, never written through.
_PUT_SCRIPT = r"""
import os, sys
F = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
ws, parts = sys.argv[1], sys.argv[2:]
d = os.open(ws, os.O_RDONLY | os.O_DIRECTORY | F)
for name in parts[:-1]:
    try:
        nd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=d)
    except FileNotFoundError:
        os.mkdir(name, 0o755, dir_fd=d)
        nd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | F, dir_fd=d)
    os.close(d)
    d = nd
leaf = parts[-1]
tmp = "." + leaf[:100] + ".levain-put-" + str(os.getpid())
w = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | F, 0o644, dir_fd=d)
try:
    while True:
        chunk = os.read(0, 1 << 16)
        if not chunk:
            break
        while chunk:
            chunk = chunk[os.write(w, chunk):]
    os.fchmod(w, 0o644)
    os.close(w)
    os.rename(tmp, leaf, src_dir_fd=d, dst_dir_fd=d)
except BaseException:
    os.unlink(tmp, dir_fd=d)
    raise
"""


def _pin_operator_source(src: Path, hands: Hands, *, want_dir: bool) -> int:
    """Open a file (or, with ``want_dir``, a directory) the operator hands in, and return its fd.

    The entity can leave a link (or, on macOS, a hard link) in /tmp or /Users/Shared that points at
    a secret of the operator's, or swap a folder on the path after it was checked. So the path is
    resolved one component at a time through directory fds opened with O_NOFOLLOW, and the result
    is used through the fd, never by name again. A link is followed only if root or the operator
    made it; nothing on the way may belong to the hands user; and no directory on the way may be
    writable by others without the sticky bit (in one, the entity could swap one of the operator's
    entries for another). The source may not lie in any entity's workspace."""
    if _under_a_workspace_root(src):
        raise WsGitError(f"{src} is in an entity's workspace; this copies something of yours into it")
    me = os.getuid()
    cloexec = getattr(os, "O_CLOEXEC", 0)
    parts = [x for x in os.path.abspath(src).split("/") if x]
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | cloexec)
    hops = 0
    try:
        while parts:
            name = parts.pop(0)
            if name == ".":
                continue
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if st.st_uid == hands.uid:
                raise WsGitError(f"{name} on the path to {src} belongs to the entity's user; refusing a source "
                                 "it could have chosen for you")
            if stat.S_ISLNK(st.st_mode):
                if st.st_uid not in (me, 0):
                    raise WsGitError(f"{name} on the path to {src} is a link another user made; give the real path")
                hops += 1
                if hops > 40:
                    raise WsGitError(f"too many links on the path to {src}")
                target = os.readlink(name, dir_fd=fd)
                if target.startswith("/"):
                    os.close(fd)
                    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | cloexec)
                parts = [x for x in target.split("/") if x] + parts
                continue
            last = not parts
            flags = os.O_RDONLY | os.O_NOFOLLOW | cloexec
            flags |= os.O_DIRECTORY if (not last or want_dir) else os.O_NONBLOCK
            nfd = os.open(name, flags, dir_fd=fd)
            nst = os.fstat(nfd)
            if nst.st_uid == hands.uid or (not last and nst.st_mode & stat.S_IWOTH and not nst.st_mode & stat.S_ISVTX):
                os.close(nfd)
                raise WsGitError(f"{name} on the path to {src} belongs to the entity's user, or anyone can swap "
                                 "what is in it; refusing")
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check_operator_source(src: Path, hands: Hands) -> None:
    """:func:`_pin_operator_source`, for a caller that only needs the verdict."""
    os.close(_pin_operator_source(src, hands, want_dir=Path(src).is_dir()))


def put_parts(workspace: Path, dest: Path | str) -> list[str]:
    """The destination's components below the workspace: ``dest`` is relative to it, or an absolute
    path inside it. Checked by name only (the hands user's write checks every component on disk)."""
    d = str(dest)
    ws = str(workspace)
    if os.path.isabs(d):
        if not d.startswith(ws.rstrip("/") + "/"):
            raise WsGitError(f"{dest} is not inside the workspace {workspace}")
        d = d[len(ws.rstrip("/")) + 1:]
    parts = d.split("/")
    if not d or any(p in ("", ".", "..") or "\0" in p for p in parts):
        raise WsGitError(f"refusing the destination {dest!r}: give a file path inside the workspace, "
                         "with no '.', '..' or empty parts")
    return parts


def _hands_python(hands: Hands) -> str:
    """A Python the hands user can run (the operator's own environment is usually under the
    operator's home, which the hands user cannot enter)."""
    for cand in dict.fromkeys((os.path.realpath(sys.executable), "/usr/bin/python3")):
        if _under_a_workspace_root(Path(cand)):
            continue
        r = subprocess.run(_as_hands(hands, cand, "-I", "-S", "-c", ""), capture_output=True,
                           stdin=subprocess.DEVNULL, cwd="/", env=child_env())
        if r.returncode == 0:
            return cand
    raise WsGitError(f"no Python that the entity's user {hands.user} can run was found")


def cmd_ws_put(entity_dir: Path | str, src: Path | str, dest: Path | str) -> int:
    """Copy one regular file of the operator's into the workspace, written by the hands user as plain
    data (mode 0644, never executable). Nothing is run from the workspace and no terminal is given.
    Runs under the exclusive hands lock, and not while anything runs as the hands user."""
    try:
        with _exclusive(entity_dir):
            return _put(entity_dir, src, dest)
    except (WsGitError, OSError) as exc:
        print(f"ws-put: {exc}")
        return 1


def _put(entity_dir: Path | str, src: Path | str, dest: Path | str) -> int:
    try:
        hands = load_hands(entity_dir)
        _refuse_while_live(hands, "ws-put")
        parts = put_parts(hands.workspace, dest)
        src = Path(src).expanduser()
        # O_NONBLOCK (in the pin): a FIFO is refused below instead of blocking the open.
        fd = _pin_operator_source(src, hands, want_dir=False)
    except (WsGitError, OSError) as exc:
        print(f"ws-put: {exc}")
        return 1
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1:
            print(f"ws-put: {src} must be a regular file of yours with no other names (no hard link)")
            return 1
        try:
            py = _hands_python(hands)
        except WsGitError as exc:
            print(f"ws-put: {exc}")
            return 1
        r = subprocess.run(_as_hands(hands, py, "-I", "-S", "-c", _PUT_SCRIPT, str(hands.workspace), *parts),
                           stdin=fd, capture_output=True, start_new_session=True, cwd="/", env=child_env())
    finally:
        os.close(fd)
    if r.returncode != 0:
        last = _sanitise(r.stderr).decode("utf-8", "replace").strip().splitlines()[-1:] or ["failed"]
        print(f"ws-put: the entity's user could not write {'/'.join(parts)}: {last[0]}")
        return 1
    print(f"Copied {src} to {hands.workspace / '/'.join(parts)}, as the entity's user.")
    return 0


# --- ws-adopt: an import ------------------------------------------------------------------------

#: Run by the hands user: the bundle arrives on stdin, is fetched into a new repository with every
#: branch and tag, and checked out. argv: git, the destination, the branch HEAD names, the object
#: format, then the settings that switch hooks and the like off. The repository starts with no
#: template (no hooks).
_IMPORT_SCRIPT = r"""
set -eu
g="$1"; dest="$2"; head="$3"; fmt="$4"; shift 4
t="$(mktemp -d)"
trap 'rm -rf "$t"' EXIT
cat > "$t/bundle"
"$g" "$@" init -q --template= --object-format="$fmt" "$dest"
"$g" "$@" -C "$dest" fetch -q --update-head-ok --no-tags "$t/bundle" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
"$g" "$@" -C "$dest" symbolic-ref HEAD "refs/heads/$head"
"$g" "$@" -C "$dest" reset -q --hard
"""


def _check_source_repo(rfd: int, src: Path, hands: Hands) -> None:
    """Before the operator's git reads a repository to import, it must be one nobody else could have
    written: the operator's git trusts it and would run what its config names. Checked through the
    pinned directory fd ``rfd``: a plain ``.git`` directory; every entry in it the operator's and
    writable by neither group nor others; no borrowed objects, no linked worktrees, no config
    includes. Then the hands user itself is asked (``test -w`` on every entry, as that user) whether
    it could write any of it, which answers for ACLs as well."""
    st = os.stat(".git", dir_fd=rfd, follow_symlinks=False)
    if not stat.S_ISDIR(st.st_mode):
        raise WsGitError(f"{src}/.git is not a plain directory (a gitfile or a link); refusing")
    me = os.getuid()
    for root, dirs, files, dfd in os.fwalk(".git", dir_fd=rfd):
        for name in (".", *dirs, *files):
            pst = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISLNK(pst.st_mode):
                writable = False
            else:
                writable = bool(pst.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
            if pst.st_uid != me or writable:
                raise WsGitError(f"{src}/{root}/{name} is not yours alone (owner {pst.st_uid}, or writable by your "
                                 "group or others); refusing to let your git read this repository")
    for name in ("commondir", "worktrees"):
        try:
            os.stat(f".git/{name}", dir_fd=rfd, follow_symlinks=False)
            raise WsGitError(f"{src}/.git/{name} present (linked worktrees are not imported)")
        except FileNotFoundError:
            pass
    try:
        if os.stat(".git/objects/info/alternates", dir_fd=rfd, follow_symlinks=False).st_size:
            raise WsGitError(f"{src}/.git borrows objects from elsewhere (alternates); refusing")
    except FileNotFoundError:
        pass
    keys = _operator_git(rfd, "config", "--file", ".git/config", "--list", "--name-only").stdout.splitlines()
    if any(k.startswith(("include.", "includeif.")) for k in keys):
        raise WsGitError(f"{src}/.git/config includes another file; refusing")
    named = Path(os.path.abspath(src))
    writable = _hands_can_write(hands, named / ".git")
    if writable:
        raise WsGitError(f"the entity's user can write {writable}; refusing to let your git read this repository")
    # The probe went by name; it answered for the pinned repository only if the name still leads to it.
    pinned, now = os.stat(".git", dir_fd=rfd, follow_symlinks=False), os.stat(named / ".git", follow_symlinks=False)
    if (pinned.st_dev, pinned.st_ino) != (now.st_dev, now.st_ino):
        raise WsGitError(f"{src}/.git changed while it was being checked; refusing")


#: Run as the hands user. argv: the tree, its parent. Prints what it can write (the parent first: a
#: writable parent could rename the tree away), exits 0; exits 4 if the walk itself fails on what
#: it can see. What it cannot see at all it cannot write.
_CAN_WRITE_SCRIPT = r"""
[ -w "$2" ] && { printf '%s\n' "$2"; exit 0; }
[ -e "$1" ] || exit 0
find "$1" -exec test -w {} \; -print || exit 4
"""


def _hands_can_write(hands: Hands, tree: Path) -> str | None:
    """The first entry the hands user can write, among ``tree``, everything in it, and its parent,
    asked of the hands user itself (``test -w``, which answers for modes and ACLs alike), or None.
    Raises when the question could not be answered."""
    probe = subprocess.run(_as_hands(hands, "/bin/sh", "-c", _CAN_WRITE_SCRIPT, "sh", str(tree), str(tree.parent)),
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env())
    if probe.returncode != 0:
        raise WsGitError(f"could not ask the entity's user what it can write in {tree}; refusing")
    first = probe.stdout.split("\n", 1)[0].strip()
    return first or None


def _operator_git(rfd: int, *args: str) -> subprocess.CompletedProcess:
    """The operator's git on the operator's own repository, run inside the pinned directory ``rfd``
    (never by its name again)."""
    return subprocess.run([_abs("git"), *_NEUTRALISE, *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, pass_fds=(rfd,), preexec_fn=lambda: os.fchdir(rfd), env=child_env())


def _remote_ok(rname: str, url: str) -> bool:
    """A remote worth copying to the entity: a name ws-git's allowlist accepts (no dot), an https or
    ssh URL, and no login in it beyond the conventional ``git`` user (a user or token there would
    hand the operator's credential to the entity)."""
    if "." in rname or not _URL_OK.match(url):
        return False
    if url.startswith(("https://", "ssh://")):
        authority = url.split("://", 1)[1].split("/", 1)[0]
        user, at, _ = authority.rpartition("@")
        return not at or (url.startswith("ssh://") and user == "git")
    return True                                     # git@host:path (the scp form _URL_OK admits)


def _heads(lines: str) -> dict[str, str]:
    """``<sha> <ref>`` lines -> {ref: sha}."""
    return {ref: sha for sha, _, ref in (ln.partition(" ") for ln in lines.splitlines()) if ref}


def cmd_ws_adopt(entity_dir: Path | str, repo: Path | str, name: str | None = None) -> int:
    """Import a repository of the operator's into the workspace: the hands user builds a new
    repository from a bundle of every branch and tag, and the branch and tag lists are checked
    against the original's. The original is not moved or changed. Uncommitted changes and stashes
    are not copied. Runs under the exclusive hands lock."""
    try:
        with _exclusive(entity_dir):
            return _adopt(entity_dir, repo, name)
    except (WsGitError, OSError) as exc:
        print(f"ws-adopt: {exc}")
        return 1


def _adopt(entity_dir: Path | str, repo: Path | str, name: str | None) -> int:
    hands = load_hands(entity_dir)
    _refuse_while_live(hands, "ws-adopt")
    src = Path(repo).expanduser()
    if _under_a_workspace_root(src):
        raise WsGitError(f"{src} is inside an entity's workspace; ws-adopt imports a repository of yours "
                         "from outside it")
    rfd = _pin_operator_source(src, hands, want_dir=True)
    try:
        return _adopt_pinned(hands, src, rfd, name)
    finally:
        os.close(rfd)


def _adopt_pinned(hands: Hands, src: Path, rfd: int, name: str | None) -> int:
    try:
        _check_source_repo(rfd, src, hands)
        prefix = _operator_git(rfd, "rev-parse", "--show-prefix")
        if prefix.returncode != 0 or prefix.stdout.strip():
            raise WsGitError(f"{src} is not the top of a repository")
        name = name or src.resolve().name
        if name in ("", ".", "..") or "/" in name or "\0" in name:
            raise WsGitError(f"refusing the name {name!r}")
        dest = hands.workspace / name
        if os.path.lexists(dest):
            raise WsGitError(f"{dest} already exists")
        head = _operator_git(rfd, "symbolic-ref", "-q", "HEAD")
        if head.returncode != 0 or not head.stdout.startswith("refs/heads/"):
            raise WsGitError(f"{src} has no branch checked out (a detached HEAD); check one out first")
        head_branch = head.stdout.strip()[len("refs/heads/"):]
        want = _heads(_operator_git(rfd, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads",
                                    "refs/tags").stdout)
        if not any(r.startswith("refs/heads/") for r in want):
            raise WsGitError(f"{src} has no branches to import")
        fmt = _operator_git(rfd, "rev-parse", "--show-object-format").stdout.strip() or "sha1"
        if fmt not in ("sha1", "sha256"):
            raise WsGitError(f"{src} uses the object format {fmt!r}, which ws-adopt does not know")
        remotes, dropped = {}, []
        for line in _operator_git(rfd, "config", "--get-regexp", r"^remote\..*\.url$").stdout.splitlines():
            key, _, url = line.partition(" ")
            rname = key.split(".", 1)[1].rsplit(".", 1)[0]
            if _remote_ok(rname, url):
                remotes[rname] = url
            else:
                dropped.append(rname)
    except (WsGitError, OSError) as exc:
        print(f"ws-adopt: {exc}")
        return 1

    # The bundle's stderr goes to a file, not a pipe: nothing reads a pipe until the import ends,
    # and a full one would stall the bundle, and the import waiting on it, for good.
    with tempfile.TemporaryFile() as bundle_err:
        bundle = subprocess.Popen([_abs("git"), *_NEUTRALISE, "bundle", "create", "-", "--branches", "--tags"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=bundle_err,
                                  pass_fds=(rfd,), preexec_fn=lambda: os.fchdir(rfd), env=child_env())
        assert bundle.stdout is not None
        imp = subprocess.run(
            _as_hands(hands, "/bin/sh", "-c", _IMPORT_SCRIPT, "sh", _real_git(), str(dest), head_branch, fmt,
                      *_NEUTRALISE, env=("GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null")),
            stdin=bundle.stdout, capture_output=True, start_new_session=True, cwd="/", env=child_env())
        bundle.stdout.close()
        bundle_rc = bundle.wait()
        bundle_err.seek(0)
        bundle_msg = bundle_err.read().decode("utf-8", "replace").strip()
    problem = None
    if imp.returncode != 0:      # first: a failed import also makes the bundle fail, on a closed pipe
        problem = "the import failed: " + _sanitise(imp.stderr).decode("utf-8", "replace").strip()
    elif bundle_rc != 0:
        problem = f"git bundle failed: {bundle_msg}"
    else:
        try:
            check_repo(dest / ".git", hands.uid)
            got = subprocess.run(ws_git_argv(hands, dest / ".git", ["for-each-ref", "--format=%(objectname) %(refname)",
                                                                   "refs/heads", "refs/tags"]),
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env())
            have = _heads(got.stdout)
            if got.returncode != 0 or have != want:
                missing = sorted(set(want) - set(have)) or sorted(r for r in want if want[r] != have.get(r))
                problem = f"the branches and tags did not all come across ({', '.join(missing) or got.stderr.strip()})"
        except (WsGitError, OSError) as exc:
            problem = str(exc)
    if problem:
        rm = subprocess.run(_as_hands(hands, "/bin/rm", "-rf", "--", str(dest)), capture_output=True, cwd="/", env=child_env())
        kept = ("Nothing was kept in the workspace" if rm.returncode == 0 and not os.path.lexists(dest)
                else f"The partial import at {dest} could not be removed")
        print(f"ws-adopt: {problem}. {kept}; your repository is unchanged.")
        return 1
    for rname, url in remotes.items():
        if subprocess.run(ws_git_argv(hands, dest / ".git", ["remote", "add", "--", rname, url]), capture_output=True,
                          stdin=subprocess.DEVNULL, cwd="/", env=child_env()).returncode != 0:
            dropped.append(rname)
    try:
        check_repo(dest / ".git", hands.uid)          # what ws-git will check: it must still pass
    except WsGitError as exc:
        print(f"ws-adopt: imported to {dest}, but ws-git would refuse it: {exc}")
        return 1
    n_heads = sum(r.startswith("refs/heads/") for r in want)
    print(f"Imported {src} as {dest} ({n_heads} branch(es), {len(want) - n_heads} tag(s)), owned by the entity's user. Your repository is "
          "unchanged where it is; uncommitted changes and stashes were not copied.")
    if dropped:
        print("Remotes not copied (a dotted name, not an https or ssh URL, or one carrying a login): "
              f"{', '.join(sorted(dropped))}.")
    return 0


def foreign_entries(hands: Hands) -> list[Path]:
    """Everything in the workspace (the workspace itself included, any type, no depth limit) that the
    hands user does not own, or that the operator running this can write. Under ruling A there should
    be none: an operator-owned directory the hands user can write is one the entity could fill with a
    repository the operator's git trusts, and an entry the operator can write (the hands user, as
    owner, can open one up) is where the operator's own tools would create one.

    The walk runs AS THE HANDS USER (system ``find``, nothing from the workspace executed), which
    can read its own tree however the entity set its modes; the writability is judged as the
    operator, with ``access(W_OK)``, which answers for mode bits and ACLs alike. Raises when the walk
    itself fails, so the caller fails closed."""
    if os.geteuid() == 0:
        raise WsGitError("run this as yourself, not as root: the check asks what YOU can write there")
    find = _abs("find")

    def walk(*predicate: str) -> list[Path]:
        r = subprocess.run(_as_hands(hands, find, str(hands.workspace), *predicate, "-print0"),
                           capture_output=True, stdin=subprocess.DEVNULL, cwd="/", env=child_env())
        if r.returncode != 0:
            why = _sanitise(r.stderr).decode("utf-8", "replace").strip().splitlines()[-1:] or [str(r.returncode)]
            raise WsGitError(f"the scan of the workspace as {hands.user} failed: {why[0]}")
        return [Path(os.fsdecode(x)) for x in r.stdout.split(b"\0") if x]

    # -prune: an entry that is not the hands user's is a finding itself; the walk does not go into it
    # (the hands user may not be able to), so only a failure inside its own tree fails the scan.
    found = set(walk("!", "-user", str(hands.uid), "-prune"))
    found.update(p for p in walk("!", "-user", str(hands.uid), "-prune", "-o", "!", "-type", "l")
                 if p not in found and _writable_or_moved(p))
    return sorted(found)


def _writable_or_moved(p: Path) -> bool:
    """access(W_OK) on ``p``, believed only if ``p`` is the same object before and after and its path
    holds no link (the entity could swap a folder on the way for a link between the walk and the
    question, and have access() answer for somewhere else). A change is itself a finding."""
    try:
        before = p.lstat()
        writable = os.access(p, os.W_OK)
        after = p.lstat()
    except OSError:
        return True
    moved = (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or os.path.realpath(p) != str(p)
    return writable or moved


def bare_repository_explicit() -> bool:
    """Whether the operator's git uses a bare repository only when told to (``safe.bareRepository =
    explicit``), so a bare repository planted anywhere is never picked up by discovery. Read only."""
    r = subprocess.run([_abs("git"), "config", "--get", "safe.bareRepository"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, cwd="/", env=child_env(prefixes=("GIT_CONFIG",)))
    if r.stdout.strip().lower() != "explicit":
        return False
    # Before git 2.38 the key does not exist, so a value in it protects nothing.
    m = re.search(r"(\d+)\.(\d+)", subprocess.run([_abs("git"), "--version"], capture_output=True, text=True,
                                                   cwd="/", env=child_env(prefixes=("GIT_CONFIG",))).stdout)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 38)


def wildcard_safe_directory(roots: tuple[Path, ...] = ()) -> list[str]:
    """Where the operator's git config trusts every repository (``safe.directory = *``) or every one
    under a workspace root (a ``<dir>/*`` entry covering it, or an entry inside it). Either switches
    off git's ownership check for the entity's repositories. Read only; nothing is executed."""
    r = subprocess.run([_abs("git"), "config", "--show-origin", "--get-all", "safe.directory"],
                       capture_output=True, text=True, cwd="/", env=child_env(prefixes=("GIT_CONFIG",)))
    hits = []
    for ln in r.stdout.splitlines():
        origin, _, value = ln.partition("\t")
        value = value.strip()
        if value == "*":
            hits.append(origin)
            continue
        base = value[:-2] if value.endswith("/*") else value
        for root in roots:
            rs = str(root)
            if (value.endswith("/*") and (rs + "/").startswith(base.rstrip("/") + "/")) or base.startswith(rs + "/") or base == rs:
                hits.append(origin)
                break
    return hits


def mask_repair_argv(hands: Hands, since: float | None = None) -> list[str]:
    """Linux: restore the ACL mask on files the hands user owns in its workspace (with ``since``, only
    those whose status changed from that time on). A file created with mode 0600 (an atomic write), or
    chmod 600, gets a mask of ---, which cancels the operator's named read entry (measured in CI, and
    in a VM through a Linux hands bash). Run as the hands user, the owner, after each hands command."""
    newer = ("-newerct", f"@{int(since)}") if since is not None else ()
    return [
        "/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i", f"PATH={SECURE_PATH}", *_start(),
        _abs("find"), str(hands.workspace), "-user", hands.user, *newer,
        "-exec", _abs("setfacl"), "-m", "m::rX", "{}", "+",
    ]
