"""levain.firing.ws_git — the operator's ways into a hands workspace (M2, H then A).

THE RULE (Phill, 2026-10-07: "go with H", then (A): "technically the best way for an augmentation
operator to be editing these files is through their entity anyways"). The workspace and everything
in it belong to the hands user; the operator can read it and nothing more. Git refuses to read the
config of, or run the hooks of, a repository its user does not own ("By default, Git will refuse to
even parse a Git config of a repository owned by someone else, let alone run its hooks",
git-config(1), safe.directory), so the operator's own git refuses every repository there. And
because the operator owns no directory there, the entity has nowhere to build a repository the
operator's git WOULD trust: git checks a bare repository's ownership on that directory alone, so an
operator-owned folder the entity could write was enough for one.

The operator changes the workspace through the entity, or through four doors, none of which runs
anything the entity wrote as the operator:
  - the remote: the entity pushes with its deploy key;
  - ``levain ws-git``: git AS THE HANDS USER, the repository's executable config switched off, and
    refused while the entity's user has a process running;
  - ``levain ws-put``: one file copied in as data, written by the hands user;
  - ``levain ws-adopt``: a repository of the operator's imported from where it sits, outside the
    workspace, as a new repository the hands user owns.
"""
from __future__ import annotations

import os
import platform
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from levain.firing.hands import SECURE_PATH, WORKSPACE_ROOT, _abs

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
    "-c", "core.editor=false", "-c", "core.sshCommand=false", "-c", "credential.helper=",
    "-c", "core.askPass=", "-c", "core.gitProxy=", "-c", "gpg.program=false",
    "-c", "gpg.ssh.program=false", "-c", "gpg.x509.program=false", "-c", "safe.bareRepository=explicit",
    "-c", "protocol.ext.allow=never",
)


def _real_git() -> str:
    """The git binary itself. On macOS ``/usr/bin/git`` is an xcrun shim whose lookup could be
    steered by the user running it (the hands user); resolve the real binary once, here, and
    accept it only if root owns it and nobody else can write it."""
    import platform
    import stat

    if platform.system() == "Darwin":
        r = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True, cwd="/")
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


def ws_git_argv(hands: Hands, gitdir: Path, args: list[str]) -> list[str]:
    """``git`` as the hands user, on exactly the git directory that was checked (``--git-dir`` and
    ``--work-tree``: no discovery, so git cannot fall through to another one), with no system or
    global config (the hands user writes its own ~/.gitconfig), the program-naming keys switched
    off, and a clean environment."""
    return _as_hands(
        hands, _real_git(), *_NEUTRALISE, f"--git-dir={gitdir}", f"--work-tree={gitdir.parent}",
        "-C", str(gitdir.parent), *args,
        env=("GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_PAGER=cat", "GIT_TERMINAL_PROMPT=0",
             "LANG=" + os.environ.get("LANG", "en_US.UTF-8")),
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
                            start_new_session=True, cwd="/")
    assert proc.stdout is not None
    for chunk in iter(lambda: proc.stdout.read(65536), b""):
        sys.stdout.buffer.write(_sanitise(chunk))
    sys.stdout.flush()
    return proc.wait()


def proc_hides_processes(mountinfo: str) -> bool:
    """Whether ``/proc`` is mounted with ``hidepid`` (other users' processes invisible), from
    ``/proc/self/mountinfo`` text."""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == "/proc" and re.search(r"hidepid=(?!0\b|off\b)", line):
            return True
    return False


def entity_session_live(hands_uid: int) -> bool:
    """Whether anything runs as the hands user right now. The workspace is the hands user's and the
    operator can only read it, so whatever can change it while ws-git works (an entity session's
    bash, anything it left running) runs as that user: this is the session's liveness measured where
    it cannot be missed, an orphaned background job included. Raises when it cannot tell (a process
    table the operator cannot see all of), so callers fail closed."""
    if platform.system() == "Linux":
        try:
            mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise WsGitError(f"cannot tell whether the entity is running ({exc}); refusing") from None
        if proc_hides_processes(mounts):
            raise WsGitError("cannot tell whether the entity is running (/proc hides other users' "
                             "processes); refusing")
    r = subprocess.run([_abs("pgrep"), "-U", str(hands_uid)], capture_output=True, text=True, cwd="/")
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    raise WsGitError(f"cannot tell whether the entity is running (pgrep: {r.stderr.strip() or r.returncode}); refusing")


def _refuse_while_live(hands: Hands, what: str) -> None:
    if entity_session_live(hands.uid):
        raise WsGitError(
            f"the entity's user {hands.user} has processes running (a session is live, or something it "
            f"left running); {what} waits until it stops, so the entity cannot rewrite what was checked")


def cmd_ws_git(entity_dir: Path | str, repo: Path | str, args: list[str]) -> int:
    try:
        hands = load_hands(entity_dir)
        _refuse_while_live(hands, "ws-git")
        gitdir = find_gitdir(Path(repo), hands.workspace)
        check_repo(gitdir, hands.uid)
    except (WsGitError, OSError) as exc:
        print(f"ws-git: {exc}")
        return 1
    return _run_relayed(ws_git_argv(hands, gitdir, args))


def _as_hands(hands: Hands, *argv: str, env: tuple[str, ...] = ()) -> list[str]:
    """``argv`` run as the hands user with a clean environment."""
    return ["/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i", f"HOME={hands.home}",
            f"PATH={SECURE_PATH}", *env, *argv]


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
                           stdin=subprocess.DEVNULL, cwd="/")
        if r.returncode == 0:
            return cand
    raise WsGitError(f"no Python that the entity's user {hands.user} can run was found")


def cmd_ws_put(entity_dir: Path | str, src: Path | str, dest: Path | str) -> int:
    """Copy one regular file of the operator's into the workspace, written by the hands user as plain
    data (mode 0644, never executable). Nothing is run from the workspace and no terminal is given."""
    try:
        hands = load_hands(entity_dir)
        parts = put_parts(hands.workspace, dest)
        src = Path(src).expanduser()
        if _under_a_workspace_root(src):
            # The entity could have left a link there to a file of yours.
            raise WsGitError(f"{src} is in an entity's workspace; ws-put copies files of yours into it")
        fd = os.open(src, os.O_RDONLY)
    except (WsGitError, OSError) as exc:
        print(f"ws-put: {exc}")
        return 1
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            print(f"ws-put: {src} is not a regular file")
            return 1
        try:
            py = _hands_python(hands)
        except WsGitError as exc:
            print(f"ws-put: {exc}")
            return 1
        r = subprocess.run(_as_hands(hands, py, "-I", "-S", "-c", _PUT_SCRIPT, str(hands.workspace), *parts),
                           stdin=fd, capture_output=True, start_new_session=True, cwd="/")
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
#: branch and tag, and checked out. argv: git, the destination, the branch HEAD names, then the
#: settings that switch hooks and the like off. The repository starts with no template (no hooks).
_IMPORT_SCRIPT = r"""
set -eu
g="$1"; dest="$2"; head="$3"; shift 3
t="$(mktemp -d)"
trap 'rm -rf "$t"' EXIT
cat > "$t/bundle"
"$g" "$@" init -q --template= "$dest"
"$g" "$@" -C "$dest" fetch -q --update-head-ok --no-tags "$t/bundle" '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'
"$g" "$@" -C "$dest" symbolic-ref HEAD "refs/heads/$head"
"$g" "$@" -C "$dest" reset -q --hard
"""


def _operator_git(src: Path, *args: str) -> subprocess.CompletedProcess:
    """The operator's git on the operator's own repository (outside every workspace)."""
    return subprocess.run([_abs("git"), *_NEUTRALISE, "-C", str(src), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, cwd="/")


def _heads(lines: str) -> dict[str, str]:
    """``<sha> <ref>`` lines -> {ref: sha}."""
    return {ref: sha for sha, _, ref in (ln.partition(" ") for ln in lines.splitlines()) if ref}


def cmd_ws_adopt(entity_dir: Path | str, repo: Path | str, name: str | None = None) -> int:
    """Import a repository of the operator's into the workspace: the hands user builds a new
    repository from a bundle of every branch and tag, and the branch list is checked against the
    original's. The original is not moved or changed. Uncommitted changes and stashes are not copied."""
    try:
        hands = load_hands(entity_dir)
        _refuse_while_live(hands, "ws-adopt")
        src = Path(repo).expanduser()
        if _under_a_workspace_root(src):
            raise WsGitError(f"{src} is inside an entity's workspace; ws-adopt imports a repository of yours "
                             "from outside it")
        top = _operator_git(src, "rev-parse", "--show-toplevel")
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != src.resolve():
            raise WsGitError(f"{src} is not the top of a repository")
        name = name or src.resolve().name
        if name in ("", ".", "..") or "/" in name or "\0" in name:
            raise WsGitError(f"refusing the name {name!r}")
        dest = hands.workspace / name
        if os.path.lexists(dest):
            raise WsGitError(f"{dest} already exists")
        head = _operator_git(src, "symbolic-ref", "-q", "HEAD")
        if head.returncode != 0 or not head.stdout.startswith("refs/heads/"):
            raise WsGitError(f"{src} has no branch checked out (a detached HEAD); check one out first")
        head_branch = head.stdout.strip()[len("refs/heads/"):]
        want = _heads(_operator_git(src, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads").stdout)
        if not want:
            raise WsGitError(f"{src} has no branches to import")
        remotes = {}
        for line in _operator_git(src, "config", "--get-regexp", r"^remote\..*\.url$").stdout.splitlines():
            key, _, url = line.partition(" ")
            if _URL_OK.match(url):
                remotes[key.split(".", 1)[1].rsplit(".", 1)[0]] = url
    except (WsGitError, OSError) as exc:
        print(f"ws-adopt: {exc}")
        return 1

    # The bundle's stderr goes to a file, not a pipe: nothing reads a pipe until the import ends,
    # and a full one would stall the bundle, and the import waiting on it, for good.
    with tempfile.TemporaryFile() as bundle_err:
        bundle = subprocess.Popen([_abs("git"), *_NEUTRALISE, "-C", str(src), "bundle", "create", "-", "--branches",
                                   "--tags"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=bundle_err, cwd="/")
        assert bundle.stdout is not None
        imp = subprocess.run(
            _as_hands(hands, "/bin/sh", "-c", _IMPORT_SCRIPT, "sh", _real_git(), str(dest), head_branch,
                      *_NEUTRALISE, env=("GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null")),
            stdin=bundle.stdout, capture_output=True, start_new_session=True, cwd="/")
        bundle.stdout.close()
        bundle_rc = bundle.wait()
        bundle_err.seek(0)
        bundle_msg = bundle_err.read().decode("utf-8", "replace").strip()
    problem = None
    if bundle_rc != 0:
        problem = f"git bundle failed: {bundle_msg}"
    elif imp.returncode != 0:
        problem = "the import failed: " + _sanitise(imp.stderr).decode("utf-8", "replace").strip()
    else:
        try:
            check_repo(dest / ".git", hands.uid)
            got = subprocess.run(ws_git_argv(hands, dest / ".git", ["for-each-ref", "--format=%(objectname) %(refname)",
                                                                   "refs/heads"]),
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd="/")
            have = _heads(got.stdout)
            if got.returncode != 0 or have != want:
                missing = sorted(set(want) - set(have)) or sorted(r for r in want if want[r] != have.get(r))
                problem = f"the branches did not all come across ({', '.join(missing) or got.stderr.strip()})"
        except (WsGitError, OSError) as exc:
            problem = str(exc)
    if problem:
        subprocess.run(_as_hands(hands, "/bin/rm", "-rf", "--", str(dest)), capture_output=True, cwd="/")
        print(f"ws-adopt: {problem}. Nothing was kept in the workspace; your repository is unchanged.")
        return 1
    for rname, url in remotes.items():
        subprocess.run(ws_git_argv(hands, dest / ".git", ["remote", "add", "--", rname, url]), capture_output=True,
                       stdin=subprocess.DEVNULL, cwd="/")
    print(f"Imported {src} as {dest} ({len(want)} branch(es)), owned by the entity's user. Your repository is "
          "unchanged where it is; uncommitted changes and stashes were not copied.")
    return 0


def foreign_entries(workspace: Path, hands_uid: int) -> list[Path]:
    """Everything in the workspace (the workspace itself included, any type, no depth limit) that the
    hands user does not own. Under ruling A there should be none: an operator-owned directory the
    hands user can write is one the entity could fill with a repository the operator's git trusts.
    An entry that cannot be read or stat'ed is reported too, never assumed clean."""
    found: list[Path] = []

    def unreadable(err: OSError) -> None:
        found.append(Path(err.filename or workspace))

    try:
        if workspace.lstat().st_uid != hands_uid:
            found.append(workspace)
    except OSError:
        return [workspace]
    for root, dirs, files in os.walk(workspace, onerror=unreadable):
        for name in (*dirs, *files):
            p = Path(root) / name
            try:
                if p.lstat().st_uid != hands_uid:
                    found.append(p)
            except OSError:
                found.append(p)
    return sorted(set(found))


def bare_repository_explicit() -> bool:
    """Whether the operator's git uses a bare repository only when told to (``safe.bareRepository =
    explicit``), so a bare repository planted anywhere is never picked up by discovery. Read only."""
    r = subprocess.run([_abs("git"), "config", "--get", "safe.bareRepository"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, cwd="/")
    return r.stdout.strip().lower() == "explicit"


def wildcard_safe_directory(roots: tuple[Path, ...] = ()) -> list[str]:
    """Where the operator's git config trusts every repository (``safe.directory = *``) or every one
    under a workspace root (a ``<dir>/*`` entry covering it, or an entry inside it). Either switches
    off git's ownership check for the entity's repositories. Read only; nothing is executed."""
    r = subprocess.run([_abs("git"), "config", "--show-origin", "--get-all", "safe.directory"],
                       capture_output=True, text=True, cwd="/")
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


def mask_repair_argv(hands: Hands) -> list[str]:
    """Linux: restore the ACL mask on files the hands user owns in its workspace. A file created with
    mode 0600 (an atomic write) gets a mask of ---, which cancels the operator's named read entry
    (measured in CI). Run as the hands user, the owner, at the end of each turn."""
    return [
        "/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i", f"PATH={SECURE_PATH}",
        _abs("find"), str(hands.workspace), "-user", hands.user, "-exec", _abs("setfacl"), "-m", "m::rX", "{}", "+",
    ]
