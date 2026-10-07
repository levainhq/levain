"""levain.firing.ws_git — the operator's access to the repositories in a hands workspace (M2, H).

THE RULE (Phill, 2026-10-07: "go with H"). Every repository in the hands workspace belongs to the
hands user. Git refuses to read the config of, or run the hooks of, a repository its user does not
own ("By default, Git will refuse to even parse a Git config of a repository owned by someone else,
let alone run its hooks", git-config(1), safe.directory). So the operator's own git refuses all of
them, by git's own check, and nothing the entity writes into a repository can run as the operator.
Setup writes no safe.directory entry on either side.

The operator reaches those repositories through the remote (the entity pushes with its deploy key)
or through ``levain ws-git``, which runs git AS THE HANDS USER with the repository's executable
config switched off, and refuses a repository whose config holds anything outside a short allowlist.
A repository the operator created in the workspace (which the operator's git WOULD trust, and the
hands user could write) is a doctor failure; ``levain ws-adopt`` replaces it with a clone owned by
the hands user.
"""
from __future__ import annotations

import os
import pwd
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from levain.firing.hands import SECURE_PATH, _abs

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
        raise WsGitError(f"{gitdir} does not belong to the hands user (run `levain ws-adopt` on it)")
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
    return [
        "/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i",
        f"HOME={hands.home}", f"PATH={SECURE_PATH}", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_PAGER=cat", "GIT_TERMINAL_PROMPT=0", "LANG=" + os.environ.get("LANG", "en_US.UTF-8"),
        _real_git(), *_NEUTRALISE, f"--git-dir={gitdir}", f"--work-tree={gitdir.parent}",
        "-C", str(gitdir.parent), *args,
    ]


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


def cmd_ws_git(entity_dir: Path | str, repo: Path | str, args: list[str]) -> int:
    try:
        hands = load_hands(entity_dir)
        gitdir = find_gitdir(Path(repo), hands.workspace)
        check_repo(gitdir, hands.uid)
    except (WsGitError, OSError) as exc:
        print(f"ws-git: {exc}")
        return 1
    return _run_relayed(ws_git_argv(hands, gitdir, args))


def cmd_ws_adopt(entity_dir: Path | str, repo: Path | str) -> int:
    """Replace an operator-owned repository in the workspace with a clone owned by the hands user.
    The original is moved aside (``<repo>.operator-<time>``), not deleted. Committed history and
    branches come across; uncommitted changes and stashes stay in the moved-aside copy."""
    try:
        hands = load_hands(entity_dir)
        src = Path(repo).resolve()
        gitdir = find_gitdir(src, hands.workspace)
        if gitdir.parent != src:
            raise WsGitError(f"{src} is not the top of a repository")
        if gitdir.lstat().st_uid == hands.uid:
            print(f"{src} already belongs to the hands user.")
            return 0
        if gitdir.lstat().st_uid != os.getuid():
            raise WsGitError(f"{gitdir} belongs to neither you nor the hands user")
        remotes = {}
        for line in _config_lines(gitdir / "config", "--get-regexp", r"^remote\..*\.url$"):
            key, url = line.split(" ", 1)
            if _URL_OK.match(url):
                remotes[key.split(".", 1)[1].rsplit(".", 1)[0]] = url
    except (WsGitError, OSError) as exc:
        print(f"ws-adopt: {exc}")
        return 1
    aside = src.with_name(f"{src.name}.operator-{time.strftime('%Y%m%d-%H%M%S')}")
    src.rename(aside)
    # Cloned BY the hands user, reading the operator's repository: the hands user trusting the
    # operator's config is no escalation, and hooks/fsmonitor are off anyway.
    clone = [
        "/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i", f"HOME={hands.home}", f"PATH={SECURE_PATH}",
        "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
        _abs("git"), "-c", f"safe.directory={aside}", *_NEUTRALISE,
        "clone", "--no-local", "--quiet", "--origin", "operator-copy", str(aside), str(src),
    ]
    r = subprocess.run(clone, capture_output=True, text=True, cwd="/")
    if r.returncode != 0:
        aside.rename(src)
        print(f"ws-adopt: the clone failed, nothing changed: {r.stderr.strip()}")
        return 1
    # The clone's own remote points at the moved-aside path, a URL ws-git itself refuses: drop it.
    subprocess.run(ws_git_argv(hands, src / ".git", ["remote", "remove", "operator-copy"]), capture_output=True, cwd="/")
    for name, url in remotes.items():
        subprocess.run(ws_git_argv(hands, src / ".git", ["remote", "add", "--", name, url]), capture_output=True, cwd="/")
    print(f"Adopted {src}: it now belongs to the hands user. Your original is at {aside}. Check the "
          "clone, then delete the original yourself; uncommitted changes and stashes exist only there.")
    return 0


def operator_owned_gitdirs(workspace: Path, hands_uid: int) -> list[Path]:
    """Every git directory under the workspace (``.git`` directories and files, and directories
    shaped like one under any name, bare repositories included) that the hands user does not own.
    The operator's git trusts such a repository while the hands user can write into it. No depth
    limit; a directory that cannot be read is reported too, never assumed clean."""
    found: list[Path] = []

    def unreadable(err: OSError) -> None:
        found.append(Path(err.filename or workspace))

    for root, dirs, files in os.walk(workspace, onerror=unreadable):
        here = Path(root)
        candidates = [here / ".git"] if (".git" in dirs or ".git" in files) else []
        if (here / "HEAD").is_file() and (here / "objects").is_dir() and (here / "refs").is_dir():
            candidates.append(here)
        for g in candidates:
            try:
                if g.lstat().st_uid != hands_uid:
                    found.append(g)
            except OSError:
                found.append(g)
    return sorted(set(found))


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
    mode 0600 (an atomic write) gets a mask of ---, which cancels the operator's named entry (measured
    in CI). Run as the hands user, the owner, at the end of each turn."""
    return [
        "/usr/bin/sudo", "-n", "-u", hands.user, "/usr/bin/env", "-i", f"PATH={SECURE_PATH}",
        _abs("find"), str(hands.workspace), "-user", hands.user, "-exec", _abs("setfacl"), "-m", "m::rwX", "{}", "+",
    ]
