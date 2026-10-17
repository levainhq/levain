"""levain.consent.push — the effect half of the push grant: the only code in levain that runs ``git push``
under a grant.

A push grant (the ``push`` kind; S5 design §2) names a workspace, ONE
pinned remote URL and a list of branch names or ``prefix/*`` patterns. This module holds the pieces of that
design that do not touch the authority log:

- :func:`canonical_remote`: the one spelling of a remote URL a grant pins and a request is compared to (byte
  equality after canonicalising both sides).
- :func:`branch_problem` / :func:`branch_matches`: the branch rule (an exact name or ``prefix/*``; bare ``*``
  refused; every name passes ``git check-ref-format --branch``).
- :func:`execute`: §3 steps 1-4. The grant JUDGMENT is not here: the caller passes ``refusal``, a callable that
  re-finds the covering grant in a fresh replay and judges it (the one checker, ``push_grant_refusal``, lives
  beside the other grant checkers). This module calls it under the per-repo lock and pushes only when it names
  the grant.

What :func:`execute` guarantees about the agent's repository: nothing the agent wrote into its ``.git/config``
or ``.git/hooks`` is executed by levain. The branch tip is FETCHED out of the agent's repo into a levain-owned
bare mirror (a fetch runs the source's ``upload-pack``, which runs no hook and honours
``uploadpack.packObjectsHook`` only from protected config; measured on Apple git 2.50.1, case c of
``tests/test_consent_push_exec.py``), and the push runs from the mirror, whose config levain rewrites on every
use. Every ``GIT_*`` variable of the caller is removed from git's environment. What it does NOT confine,
stated: the operator's system and global git config apply (credential helpers live there, and an edit of
``~/.gitconfig`` is outside every workspace), except that a ``url.*.insteadOf`` / ``pushInsteadOf`` that would
rewrite the pinned URL refuses the push; ``git`` is the one on the server's ``PATH``.

Stdlib only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from levain.launch import child_env

__all__ = ["MIRROR_CONFIG", "PUSH_CONFIG", "PushRefused", "PushResult", "branch_matches", "branch_problem",
           "canonical_remote", "execute", "git_env", "push_argv", "repo_key"]

# The config flags of every push (§3 step 3), plus core.abbrev=no so --porcelain reports full object names
# (git abbreviates them to 7 by default, which a receipt cannot carry).
PUSH_CONFIG: tuple[str, ...] = (
    "core.hooksPath=/dev/null", "push.recurseSubmodules=no", "push.followTags=false", "push.gpgSign=false",
    "protocol.ext.allow=never", "protocol.fd.allow=never", "core.abbrev=no")
# The mirror's whole config, written by levain on every use: an edit of it between pushes does not survive.
MIRROR_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n"
# the local ref the agent's branch tip is fetched into, inside the mirror
SRC_REF = "refs/levain/src"
ZERO_SHA = "0" * 40

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
_TRANSPORT = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_USER = re.compile(r"^[A-Za-z0-9._~][A-Za-z0-9._~-]*$")
_DEFAULT_PORT = {"ssh": "22", "https": "443"}
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class PushRefused(ValueError):
    """A push levain will not run. Raised before any effect on the remote; the message says why."""


@dataclass(frozen=True)
class PushResult:
    """What one granted push did. ``ok`` is git's verdict on the one ref: ``status`` is ``"new"`` (a new
    branch), ``"fast-forward"``, ``"up-to-date"``, ``"rejected"`` (git refused it: non-fast-forward, or the
    remote's own rules) or ``"error"`` (git failed without a ref line)."""

    ok: bool
    status: str
    grant_id: str
    grant_seq: int
    remote: str
    branch: str
    old_sha: str | None
    new_sha: str
    exit: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------------------
# remote URL


def _path_problem(path: str) -> str | None:
    if not path.startswith("/"):
        return "the path must be absolute"
    segs = path.split("/")[1:]
    if any(s in ("", ".", "..") for s in segs):
        return "the path has an empty, '.' or '..' segment"
    return None


def _host_port(hostport: str, scheme: str) -> str:
    host, sep, port = hostport.partition(":")
    host = host.lower()
    if not _HOST.match(host):
        raise ValueError(f"host {host!r} is not a plain ASCII host name (IP literals in brackets, non-ASCII and "
                         "a leading '-' are refused)")
    if sep:
        if not port.isdigit() or not port.isascii() or not 0 < int(port) < 65536 or port.startswith("0"):
            raise ValueError(f"port {port!r} is not a port number")
        if port == _DEFAULT_PORT.get(scheme):
            return host
        return f"{host}:{port}"
    return host


def canonical_remote(url: str) -> str:
    """The one spelling of a remote URL a push grant pins. Accepted: ``ssh://[user@]host[:port]/path``, scp-form
    ``[user@]host:path``, ``https://[user@]host[:port]/path``, ``file:///abs/path``. Scheme and host are
    lower-cased; ports 22 (ssh) and 443 (https) dropped; one trailing ``/`` dropped; ``.git`` kept as written.
    An scp-form URL with an absolute path becomes ``ssh://[user@]host/path`` (git sends the same path either
    way); one with a relative path STAYS scp-form, because ``ssh://host/path`` would name ``/path`` on the server
    while ``host:path`` names it relative to the login directory. Raises :class:`ValueError` naming why for:
    ``<transport>::`` (ext, fd, remote helpers), any other scheme, userinfo carrying a password, a query or
    fragment, ``%``-escapes, whitespace or control characters, a relative path, and an empty, ``.`` or ``..``
    path segment."""
    if not isinstance(url, str) or not url:
        raise ValueError("the remote is not a non-empty string")
    if not url.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("the remote has non-ASCII, whitespace or control characters")
    if _TRANSPORT.match(url):
        raise ValueError("a '<transport>::' remote (ext, fd, a remote helper) is never pushed to")
    for ch, why in (("?", "a query"), ("#", "a fragment"), ("%", "a %-escape"), ("\\", "a backslash")):
        if ch in url:
            raise ValueError(f"the remote carries {why}")
    m = _SCHEME.match(url)
    if m:
        scheme = m.group(1).lower()
        rest = url[m.end():]
        if scheme not in ("ssh", "https", "file"):
            raise ValueError(f"scheme {scheme}:// is not accepted (ssh, https, file and scp-form only)")
        if scheme == "file":
            if not rest.startswith("/"):
                raise ValueError("a file:// remote names no host: it must be file:///abs/path")
            path = rest
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            if p := _path_problem(path):
                raise ValueError(p)
            return "file://" + path
        authority, slash, path = rest.partition("/")
        if not slash:
            raise ValueError("the remote has no path")
        path = "/" + path
        user = None
        if "@" in authority:
            user, _, authority = authority.rpartition("@")
            if ":" in user:
                raise ValueError("the remote's userinfo carries a password")
            if not _USER.match(user):
                raise ValueError(f"user {user!r} is not a plain user name")
        hostport = _host_port(authority, scheme)
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        if p := _path_problem(path):
            raise ValueError(p)
        return f"{scheme}://{user + '@' if user else ''}{hostport}{path}"
    # scp-form: a ':' before any '/', per git's own rule
    colon, slash = url.find(":"), url.find("/")
    if colon <= 0 or (0 <= slash < colon):
        raise ValueError("the remote is a relative or bare local path, or no URL git and levain both accept")
    head, path = url[:colon], url[colon + 1:]
    user = None
    if "@" in head:
        user, _, head = head.rpartition("@")
        if not _USER.match(user):
            raise ValueError(f"user {user!r} is not a plain user name")
    if "[" in head or "]" in head:
        raise ValueError("a bracketed scp-form host is refused")
    host = _host_port(head, "ssh")
    if ":" in host:
        raise ValueError("scp-form carries no port")
    if not path:
        raise ValueError("the remote has no path")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path.startswith("/"):
        if p := _path_problem(path):
            raise ValueError(p)
        return f"ssh://{user + '@' if user else ''}{host}{path}"
    if path.startswith("-"):
        raise ValueError("the remote path begins with '-'")
    if p := _path_problem("/" + path):
        raise ValueError(p)
    return f"{user + '@' if user else ''}{host}:{path}"


# ---------------------------------------------------------------------------------------------------------------
# branches


def _ref_format_ok(name: str) -> bool:
    r = subprocess.run(["git", "check-ref-format", "--branch", name], cwd="/", env=git_env(),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and r.stdout.strip() == name


def branch_problem(pattern: str) -> str | None:
    """Why ``pattern`` is not a branch rule a push grant may carry, or ``None``. A rule is an exact branch
    name or ``prefix/*`` (one ``*``, last, right after a ``/``); the name (or the prefix) passes
    ``git check-ref-format --branch``. Bare ``*`` is refused: a rule that names ``main`` too is a whole-repo
    grant, and a user who wants that signs the names."""
    if not isinstance(pattern, str) or not pattern:
        return "a branch rule must be a non-empty string"
    if pattern == "*":
        return "a bare '*' covers every branch, main included; name the branches or a prefix/*"
    if "@{" in pattern or pattern.startswith("-"):
        return f"{pattern!r} is not a branch name"
    name = pattern
    if "*" in pattern:
        if pattern.count("*") != 1 or not pattern.endswith("/*") or len(pattern) < 3:
            return f"{pattern!r}: a pattern is prefix/* (one '*', last, after a '/')"
        name = pattern[:-2]
    if not _ref_format_ok(name):
        return f"{name!r} fails git check-ref-format --branch"
    return None


def branch_matches(patterns: Iterable[str], branch: str) -> bool:
    """Whether ``branch`` (a branch name, never a pattern) is named by one of ``patterns``: equal to an exact
    rule, or under a ``prefix/*`` rule with a non-empty remainder. A ``branch`` that is not a valid branch name
    matches nothing."""
    if not isinstance(branch, str) or "*" in branch or branch_problem(branch) is not None:
        return False
    for p in patterns:
        if p.endswith("/*"):
            if branch.startswith(p[:-1]) and len(branch) > len(p) - 1:
                return True
        elif p == branch:
            return True
    return False


# ---------------------------------------------------------------------------------------------------------------
# execute


def git_env() -> dict[str, str]:
    """git's environment for a granted push: :func:`levain.launch.child_env` (the allowlist; ``HOME`` and
    ``SSH_AUTH_SOCK`` are on it, so the user's own credentials work) with EVERY ``GIT_*`` name removed, so a
    caller's ``GIT_DIR``, ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n``, ``GIT_SSH_COMMAND`` and the rest mean
    nothing; then ``GIT_TERMINAL_PROMPT=0`` (levain's own value: a push from a server with no terminal must
    fail, never hang on a prompt)."""
    env = {k: v for k, v in child_env().items() if not k.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def repo_key(realpath: str) -> str:
    return hashlib.sha256(os.fsencode(realpath)).hexdigest()


def push_argv(mirror: Path, remote: str, branch: str) -> list[str]:
    """The only push argv levain builds: no force, no delete, no tags, same-name ``refs/heads/<b>``."""
    argv = ["git", f"--git-dir={mirror}"]
    for c in PUSH_CONFIG:
        argv += ["-c", c]
    return argv + ["push", "--no-verify", "--porcelain", remote, f"{SRC_REF}:refs/heads/{branch}"]


def _repo_realpath(repo_path: str | os.PathLike) -> str:
    raw = os.fspath(repo_path)
    if not os.path.isabs(raw):
        raise PushRefused(f"{raw}: the repository path must be absolute")
    ab = os.path.normpath(raw)
    real = os.path.realpath(ab)
    if real != ab:
        raise PushRefused(f"{raw} resolves through a symlink (to {real}); pass the real path")
    try:
        st = os.lstat(real)
    except OSError as exc:
        raise PushRefused(f"{raw}: {exc.strerror}") from None
    if not stat.S_ISDIR(st.st_mode):
        raise PushRefused(f"{raw} is not a directory")
    try:
        gst = os.lstat(os.path.join(real, ".git"))
    except OSError:
        raise PushRefused(f"{raw} has no .git directory") from None
    if not stat.S_ISDIR(gst.st_mode):
        raise PushRefused(f"{raw}/.git is not a directory (a gitfile: linked worktrees and submodules are refused, "
                          "or a link)")
    return real


def _private_dir(path: Path) -> None:
    """``path`` as a 0700 directory of this user, not a link."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PushRefused(f"{path} is not levain's own directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _git(argv: list[str], *, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=git_env(), cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout)


def _mirror(levain_home: Path, grant_id: str, key: str) -> Path:
    """The levain-owned bare mirror for this grant and repository, (re)written: 0700, levain's own config, no
    hooks directory. Keyed by repository as well as grant: the lock is per repository, so two repositories under
    one grant must not share the mirror's ``refs/levain/src``."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", grant_id) or ".." in grant_id:
        raise PushRefused(f"grant id {grant_id!r} is not a plain id")
    root = levain_home / "push" / "mirrors"
    _private_dir(root)
    _private_dir(root / grant_id)
    m = root / grant_id / f"{key}.git"
    if not os.path.lexists(m):
        r = _git(["git", "init", "--quiet", "--bare", "--template=", str(m)])
        if r.returncode:
            raise PushRefused(f"could not create the mirror: {r.stderr.strip()}")
    _private_dir(m)
    hooks = m / "hooks"
    if os.path.lexists(hooks):
        if os.path.islink(hooks) or not os.path.isdir(hooks):
            os.unlink(hooks)
        else:
            shutil.rmtree(hooks)
    cfg = m / "config"
    tmp = m / "config.levain-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(MIRROR_CONFIG)
    os.replace(tmp, cfg)
    return m


def _rewrites(mirror: Path, remote: str) -> list[str]:
    """Every ``url.<base>.insteadOf`` / ``pushInsteadOf`` in the effective config whose prefix matches
    ``remote`` (compared case-insensitively, which over-matches and so refuses more, never less)."""
    r = _git(["git", f"--git-dir={mirror}", "config", "--includes", "--get-regexp",
              r"^url\..*\.(insteadof|pushinsteadof)$"], timeout=60)
    if r.returncode not in (0, 1):
        raise PushRefused(f"could not read the effective git config: {r.stderr.strip()}")
    hits = []
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value and remote.lower().startswith(value.lower()):
            hits.append(f"{key} = {value}")
    return hits


def _journal(levain_home: Path, key: str, record: dict[str, Any]) -> None:
    path = levain_home / "push" / f"{key}.journal.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_porcelain(stdout: str, dst: str) -> tuple[str, str | None, str]:
    """(status, old sha or None, detail) for the ``dst`` ref line of ``git push --porcelain``."""
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[1].endswith(":" + dst):
            continue
        flag, summary = parts[0], parts[2]
        if flag == "*":
            return "new", ZERO_SHA, summary
        if flag == " ":
            old = summary.split("..", 1)[0]
            return "fast-forward", old if _SHA.match(old) else None, summary
        if flag == "=":
            return "up-to-date", None, summary
        if flag == "!":
            return "rejected", None, summary
        return "error", None, line
    return "error", None, ""


Refusal = Callable[..., "tuple[str, int] | str"]


def execute(grant: dict[str, Any], *, repo_path: str | os.PathLike, remote: str, branch: str,
            levain_home: str | os.PathLike, refusal: Refusal,
            receipt: Callable[[dict[str, Any]], Any]) -> PushResult:
    """Push ``branch`` of the repository at ``repo_path`` to ``remote`` under a grant (§3 steps 1-4).

    ``grant`` is the grant the caller found before the lock; its ``id`` names the mirror. Under the per-repo lock
    ``refusal(path=<repo realpath>, remote=<canonical remote>, branch=<branch>)`` re-finds and judges the
    covering grant in a fresh replay and returns ``(grant id, link seq)``, or a refusal string; it must name
    ``grant["id"]`` (a different covering grant refuses: the caller asks again). ``receipt`` is called with the
    receipt record after git ran (also when git rejected the ref); an intent line is journalled under
    ``<levain_home>/push/`` before the push and an outcome line after it, so a receipt write that fails after
    the effect leaves a record.

    Raises :class:`PushRefused` for anything refused before the push. A rejection by git (non-fast-forward, a
    protected branch) is a :class:`PushResult` with ``ok`` False: reported, never forced."""
    try:
        canon = canonical_remote(remote)
    except ValueError as exc:
        raise PushRefused(f"remote: {exc}") from None
    if not isinstance(branch, str) or "*" in branch or (bp := branch_problem(branch)):
        raise PushRefused(f"branch: {bp if isinstance(branch, str) and '*' not in branch else 'not a branch name'}")
    gid = grant.get("id") if isinstance(grant, dict) else None
    if not isinstance(gid, str):
        raise PushRefused("the grant carries no id")
    real = _repo_realpath(repo_path)
    home = Path(os.path.abspath(os.fspath(levain_home)))
    if not os.path.isdir(home):
        raise PushRefused(f"levain home {home} is not a directory")
    key = repo_key(real)
    _private_dir(home / "push")
    lock_fd = os.open(home / "push" / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # step 1: the fresh judgment, under the lock
        verdict = refusal(path=real, remote=canon, branch=branch)
        if isinstance(verdict, str):
            raise PushRefused(verdict)
        if (not isinstance(verdict, tuple) or len(verdict) != 2 or not isinstance(verdict[0], str)
                or not isinstance(verdict[1], int) or isinstance(verdict[1], bool)):
            raise PushRefused("the grant check returned no grant")
        found_id, seq = verdict
        if found_id != gid:
            raise PushRefused(f"the covering grant is now {found_id}, not {gid}; ask again")
        # step 2: the tip, fetched out of the agent's repo into levain's mirror
        mirror = _mirror(home, gid, key)
        f = _git(["git", f"--git-dir={mirror}", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
                  "-c", "protocol.fd.allow=never", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                  "--no-recurse-submodules", real, f"+refs/heads/{branch}:{SRC_REF}"])
        if f.returncode:
            raise PushRefused(f"could not read branch {branch} from {real}: {f.stderr.strip()[-400:]}")
        rp = _git(["git", f"--git-dir={mirror}", "rev-parse", "--verify", "--quiet", f"{SRC_REF}^{{commit}}"])
        new = rp.stdout.strip()
        if rp.returncode or not _SHA.match(new):
            raise PushRefused(f"branch {branch} of {real} is not a commit")
        # step 3: the pin must mean the destination
        if hits := _rewrites(mirror, canon):
            raise PushRefused(f"the git config rewrites the pinned remote ({hits[0]}); a pin must mean the "
                              "destination")
        base = {"grant_id": gid, "grant_seq": seq, "remote": canon, "branch": branch, "repo": real, "new_sha": new}
        _journal(home, key, {"state": "intent", "at": time.time(), **base})
        p = _git(push_argv(mirror, canon, branch))
        status, old, detail = _parse_porcelain(p.stdout, f"refs/heads/{branch}")
        if status == "error":
            detail = (p.stderr.strip() or p.stdout.strip())[-400:]
        ok = p.returncode == 0 and status in ("new", "fast-forward", "up-to-date")
        if status == "up-to-date":
            old = new
        result = PushResult(ok=ok, status=status, grant_id=gid, grant_seq=seq, remote=canon, branch=branch,
                            old_sha=old, new_sha=new, exit=p.returncode, detail=detail)
        _journal(home, key, {"state": "done", "at": time.time(), **result.to_dict()})
        # step 4: the receipt (policy head and the store are the caller's)
        receipt({**result.to_dict(), "repo": real})
        return result
    finally:
        os.close(lock_fd)
