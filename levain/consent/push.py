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
use. Every ``GIT_*`` variable of the caller is removed from git's environment (then ``GIT_TERMINAL_PROMPT=0``,
``GIT_NO_LAZY_FETCH=1``, ``GIT_NO_REPLACE_OBJECTS=1`` and ``GIT_ALLOW_PROTOCOL`` are set by levain), and so are the loader's
``LD_*`` / ``DYLD_*`` search variables. ``git`` is never looked up on ``PATH``: it is :data:`GIT_CANDIDATES`
(``/usr/bin/git``), refused unless it and its directory are root-owned and not group- or world-writable, and
git's own children (``ssh``, a credential helper) see the fixed :data:`TRUSTED_PATH`.

The agent's object store is read only when it is its own: ``.git/objects`` a real directory with no symlink
anywhere under it, no ``objects/info/alternates`` or ``http-alternates``, no ``.git/commondir``, not a partial
clone (``extensions.partialclone``, a ``remote.<n>.promisor``, or an ``objects/pack/*.promisor`` file refuses),
and not SHA-256 (refused by name: the mirror is SHA-1). That check runs twice: before the lock, and again under
it immediately before the fetch, which must find the same directories (same realpath, device and inode).

What git may DO while it reads the agent's repo is bounded by transport, not by another look at the directory:
the fetch runs with ``GIT_ALLOW_PROTOCOL=file`` and the push with the destination's own scheme only (``ssh``,
``https``, or ``file`` for a ``file://`` destination), inherited by every git child. A promisor remote, a lazy
fetch, an ``ext::`` / ``fd::`` URL or an ``insteadOf`` the agent writes into its repo after the check is refused
by git itself, on any git version (an older git that ignores ``GIT_NO_LAZY_FETCH`` included); every other git
call runs with an empty allow-list. Measured on Apple git 2.50.1 by planting each after the under-lock check
(``test_transport_planted_after_the_check_never_runs``).

What it does NOT confine, stated: the operator's system and global git config apply (credential helpers live
there, and an edit of ``~/.gitconfig`` is outside every workspace), except that a ``url.*.insteadOf`` /
``pushInsteadOf`` that would rewrite the pinned URL refuses the push.

Known edges (one line each, not built against):

- object-store swaps: an agent still running can change its repository, ``.git``, object store, alternates or a
  loose object after the under-lock check and before or while git reads it (git takes a path, not a
  descriptor), and the static refusals above only see a store that holds still. Bounded, not closed: what is
  pushed is fixed by the approved commit's hash, and the fetch verifies every object it receives against its
  hash, so a swapped store can supply only objects of exactly those hashes. The residue is an approved commit
  that references objects the agent could not read itself, which needs the agent to know their hashes. The
  alternates / ``commondir`` / symlink refusals stay as defence in depth.
- a promisor the agent plants after the check with a ``file://`` URL (and its own ``uploadpack`` command) is
  allowed by the workspace fetch's ``file`` protocol. It fires only on a git that lazy-fetches inside
  upload-pack despite ``GIT_NO_LAZY_FETCH``, i.e. one without git's May 2024 security releases (CVE-2024-32004,
  fixed in 2.39.4, 2.40.2, 2.41.1, 2.42.2, 2.43.4, 2.44.1, 2.45.1 and distribution backports); no version floor is
  enforced, because a backported distribution git reports the older number.
- a HARD link the agent makes inside its object store to an object file outside it is not detected (a hard
  link is indistinguishable from the file); the agent must be able to name that file to make one.
- a credential helper or ``core.sshCommand`` in the operator's own config named by a bare command that lives
  outside :data:`TRUSTED_PATH` (a Homebrew ``gh``, say) is not found; give it an absolute path. The system
  config's ``osxkeychain`` helper resolves (git's exec path), measured on Apple git 2.50.1.
- on macOS ``/usr/bin/git`` is the ``xcrun`` shim: it picks the developer directory from ``xcode-select``
  (root's) and ``DEVELOPER_DIR`` (dropped from the environment by the allowlist), and keeps a per-user lookup
  cache (``xcrun_db``) in the user's temp directory, which levain does not inspect.

The object-store walk is capped at :data:`MAX_OBJECT_ENTRIES` entries (refused above it), so the per-repo lock
is held for a bounded scan.

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

__all__ = ["GIT_CANDIDATES", "MAX_OBJECT_ENTRIES", "MIRROR_CONFIG", "PUSH_CONFIG", "TRUSTED_PATH", "PushOutcomeUnknown",
           "PushRecordIncomplete", "PushRefused", "PushResult", "branch_matches", "branch_problem",
           "canonical_remote", "execute", "git_bin", "git_env", "push_argv", "push_protocol", "repo_key"]

# The git levain runs, by absolute path; the first that passes :func:`git_bin`'s ownership test is used.
GIT_CANDIDATES: tuple[str, ...] = ("/usr/bin/git",)
# git's children (ssh, credential helpers, the xcrun shim's lookups) search only these.
TRUSTED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The most entries (files and directories) the object-store walk visits before it refuses: the walk runs twice
# per push, once under the per-repo lock, and a store the agent filled on purpose must not hold that lock
# unbounded. A store this large is packed first (``git gc``).
MAX_OBJECT_ENTRIES = 200_000

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
# Only https drops its default port. An explicit ssh :22 is kept: with ``Host h / Port 2222`` in the ssh config,
# ``ssh://h:22/r`` and ``ssh://h/r`` reach different ports, so the two spellings are not one destination.
_DEFAULT_PORT = {"https": "443"}
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class PushRefused(ValueError):
    """A push levain will not run. Raised before any effect on the remote; the message says why."""


class PushOutcomeUnknown(RuntimeError):
    """``git push`` started and did not report (it timed out, or could not be run or read): the remote may or
    may not have the ref. The journal's ``done`` line and the receipt both carry status ``"unknown"`` (as far
    as each could be written; ``record_errors`` names any that failed). ``result`` is that record."""

    def __init__(self, msg: str, result: "PushResult", record_errors: list[str]):
        super().__init__(msg)
        self.result = result
        self.record_errors = record_errors


class PushRecordIncomplete(RuntimeError):
    """The push ran and git reported on it (``result``), but the ``done`` journal line or the receipt could not
    be written. The effect on the remote is what ``result`` says; ``record_errors`` names what failed."""

    def __init__(self, msg: str, result: "PushResult", record_errors: list[str]):
        super().__init__(msg)
        self.result = result
        self.record_errors = record_errors


@dataclass(frozen=True)
class PushResult:
    """What one granted push did. ``ok`` is git's verdict on the one ref: ``status`` is ``"new"`` (a new
    branch), ``"fast-forward"``, ``"up-to-date"``, ``"rejected"`` (git refused it: non-fast-forward, or the
    remote's own rules), ``"error"`` (git failed without a ref line) or ``"unknown"`` (git did not report; only
    in a :class:`PushOutcomeUnknown`)."""

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
    lower-cased; port 443 (https) dropped, an explicit ssh port (22 included) kept; one trailing ``/`` dropped;
    ``.git`` kept as written. An scp-form URL with an absolute path becomes ``ssh://[user@]host/path`` (git sends
    the same path either way), except one whose first segment starts with ``~``, which is refused (git expands
    ``ssh://host/~u/x`` server-side but sends scp-form ``host:/~u/x`` literally); one with a relative path STAYS
    scp-form, because ``ssh://host/path`` would name ``/path`` on the server while ``host:path`` names it
    relative to the login directory. Raises :class:`ValueError` naming why for:
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
        if path.startswith("/~"):
            raise ValueError("an scp-form absolute path whose first segment starts with '~' names a different "
                             "server path as ssh://; give the ssh:// URL")
        return f"ssh://{user + '@' if user else ''}{host}{path}"
    if path.startswith("-"):
        raise ValueError("the remote path begins with '-'")
    if p := _path_problem("/" + path):
        raise ValueError(p)
    return f"{user + '@' if user else ''}{host}:{path}"


# ---------------------------------------------------------------------------------------------------------------
# branches


def _ref_format_ok(name: str) -> bool:
    r = _git(["git", "check-ref-format", "--branch", name], timeout=30)
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


def push_protocol(canon: str) -> str:
    """The one transport a push to the canonical remote ``canon`` may use: ``ssh`` (``ssh://`` and scp-form),
    ``https`` or ``file``. Derived from :func:`canonical_remote`'s output, never from the request."""
    m = _SCHEME.match(canon)
    if not m:
        return "ssh"   # scp-form is the only canonical spelling without a scheme
    scheme = m.group(1)
    if scheme not in ("ssh", "https", "file"):
        raise PushRefused(f"no push transport for scheme {scheme}://")
    return scheme


def git_env(protocols: str = "") -> dict[str, str]:
    """git's environment for a granted push: :func:`levain.launch.child_env` (the allowlist; ``HOME`` and
    ``SSH_AUTH_SOCK`` are on it, so the user's own credentials work) with EVERY ``GIT_*`` name removed, so a
    caller's ``GIT_DIR``, ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n``, ``GIT_SSH_COMMAND`` and the rest mean
    nothing; the loader's ``LD_*`` / ``DYLD_*`` variables removed too, and ``PATH`` replaced by
    :data:`TRUSTED_PATH` (an agent-writable directory on the server's own ``PATH``, a workspace virtualenv say,
    must not supply git's ``ssh`` or a library). Then levain's own values: ``GIT_TERMINAL_PROMPT=0`` (a push from
    a server with no terminal must fail, never hang on a prompt), ``GIT_NO_LAZY_FETCH=1`` (a partial clone's
    upload-pack must never fetch a missing object through the agent's remote config; Apple git 2.50.1 was
    measured to default to this inside upload-pack, an older git may not), ``GIT_NO_REPLACE_OBJECTS=1``, and
    ``GIT_ALLOW_PROTOCOL=protocols`` (colon-separated; empty allows no transport at all). git's children inherit
    it, and it overrides every ``protocol.*.allow`` in any config, so it bounds a lazy fetch upload-pack starts in
    the agent's repo too."""
    env = {k: v for k, v in child_env().items()
           if not k.upper().startswith(("GIT_", "LD_", "DYLD_"))}
    env["PATH"] = TRUSTED_PATH
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_ALLOW_PROTOCOL"] = protocols
    return env


def _root_owned(path: str, *, want_dir: bool) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    kind = stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)
    return kind and st.st_uid == 0 and not st.st_mode & 0o022


def git_bin() -> str:
    """The absolute path of the git levain runs: the first of :data:`GIT_CANDIDATES` that, after resolving
    links, is a regular file owned by root and not group- or world-writable, in a directory of the same kind
    (and, for a link, the link's own directory too). Never a ``PATH`` lookup. Raises :class:`PushRefused`
    naming the candidates when none qualifies."""
    for c in GIT_CANDIDATES:
        if not os.path.isabs(c):
            continue
        real = os.path.realpath(c)
        dirs = {os.path.dirname(real), os.path.dirname(os.path.normpath(c))}
        if _root_owned(real, want_dir=False) and all(_root_owned(d, want_dir=True) for d in dirs):
            return real
    raise PushRefused(f"no root-owned git at {', '.join(GIT_CANDIDATES)} (owned by root, not group- or "
                      "world-writable, in a directory of the same kind); levain never runs a git found on PATH")


def repo_key(realpath: str) -> str:
    return hashlib.sha256(os.fsencode(realpath)).hexdigest()


def push_argv(mirror: Path, remote: str, branch: str) -> list[str]:
    """The only push argv levain builds: no force, no delete, no tags, same-name ``refs/heads/<b>``."""
    argv = ["git", f"--git-dir={mirror}"]   # argv[0] is replaced by :func:`git_bin` when it runs
    for c in PUSH_CONFIG:
        argv += ["-c", c]
    return argv + ["push", "--no-verify", "--porcelain", remote, f"{SRC_REF}:refs/heads/{branch}"]


def _walk_objects(objs: str) -> None:
    """Refuse a symlink anywhere under the object store (a fan-out directory, a loose object, a pack), a
    ``.promisor`` pack file, and a store of more than :data:`MAX_OBJECT_ENTRIES` entries."""
    stack = [objs]
    seen = 0
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as exc:
            raise PushRefused(f"could not read the object store at {d}: {exc.strerror}") from None
        seen += len(entries)
        if seen > MAX_OBJECT_ENTRIES:
            raise PushRefused(f"the object store at {objs} holds more than {MAX_OBJECT_ENTRIES} entries; levain "
                              "caps its scan so the per-repo lock stays bounded (pack it first: git gc)")
        for e in entries:
            if e.is_symlink():
                raise PushRefused(f"the object store holds a symlink ({e.path}); levain reads only objects the "
                                  "repository holds itself")
            if e.is_dir(follow_symlinks=False):
                stack.append(e.path)
            elif e.name.endswith(".promisor"):
                raise PushRefused(f"the repository is a partial clone ({e.path}); a partial clone is not pushed")


def _config_problem(gitdir: str) -> str | None:
    """Why the repository's own config refuses (a partial clone, SHA-256), read as a file: nothing in it runs."""
    for name in ("config", "config.worktree"):
        f = os.path.join(gitdir, name)
        if not os.path.lexists(f):
            continue
        r = _git(["git", "config", "--file", f, "--includes", "-z", "--get-regexp",
                  r"^(extensions\.(partialclone|objectformat)|remote\..*\.promisor)$"], timeout=30)
        if r.returncode not in (0, 1):
            return f"could not read {f}: {r.stderr.strip()[-200:]}"
        for item in r.stdout.split("\0"):
            key, _, value = item.partition("\n")
            key, value = key.lower(), value.strip().lower()
            if key == "extensions.objectformat" and value not in ("", "sha1"):
                return ("SHA-256 repositories are not supported yet" if value == "sha256"
                        else f"object format {value!r} is not supported")
            if key == "extensions.partialclone" or (key.endswith(".promisor") and value not in ("false", "no",
                                                                                              "off", "0")):
                return f"the repository is a partial clone ({key} in {f}); a partial clone is not pushed"
    return None


def _repo_topology(repo_path: str | os.PathLike) -> tuple[str, tuple]:
    """(realpath, identity) of a repository levain will read, or :class:`PushRefused`. The identity is the
    (device, inode) of the repository, its ``.git`` and its ``.git/objects``, so a second call can tell a
    swap."""
    real = _repo_realpath(repo_path)
    gitdir = os.path.join(real, ".git")
    if os.path.lexists(os.path.join(gitdir, "commondir")):
        raise PushRefused(f"{real}/.git/commondir exists: a repository sharing another's refs and object store "
                          "is refused")
    objs = os.path.join(gitdir, "objects")
    try:
        ost = os.lstat(objs)
    except OSError:
        raise PushRefused(f"{real}/.git has no object store") from None
    if not stat.S_ISDIR(ost.st_mode):
        raise PushRefused(f"{real}/.git/objects is not a real directory: levain reads only the repository's own "
                          "object store")
    for alt in ("alternates", "http-alternates"):
        if os.path.lexists(os.path.join(objs, "info", alt)):
            raise PushRefused(f"the object store borrows objects (objects/info/{alt}); levain reads only the "
                              "repository's own objects")
    _walk_objects(objs)
    if problem := _config_problem(gitdir):
        raise PushRefused(problem)
    ident = tuple((s.st_dev, s.st_ino) for s in (os.lstat(real), os.lstat(gitdir), ost))
    return real, ident


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


def _fsync_dir(path: Path | str) -> None:
    dfd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _private_dir(path: Path) -> None:
    """``path`` as a 0700 directory of this user, not a link. When it creates the directory it fsyncs the parent,
    so the new entry (and the journal under it) survives a crash."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    else:
        _fsync_dir(path.parent)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PushRefused(f"{path} is not levain's own directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _git(argv: list[str], *, timeout: int = 600, protocols: str = "",
         git: str | None = None) -> subprocess.CompletedProcess:
    """Run ``argv`` (``argv[0]`` is ``"git"``) as ``git`` (default :func:`git_bin`), in
    :func:`git_env` with ``protocols`` the only transports allowed, from ``/``. Output is decoded as UTF-8 with
    ``errors="replace"``: a remote hook's bytes never raise."""
    if argv[0] != "git":
        raise ValueError("levain runs only git here")
    return subprocess.run([git or git_bin(), *argv[1:]], env=git_env(protocols), cwd="/", stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


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
    """Append ``record`` as one line, whole (a short write is continued; a write that makes no progress
    raises), fsync it, and on the journal's first creation fsync its directory too."""
    path = levain_home / "push" / f"{key}.journal.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        fd = os.open(path, flags)
        created = False
    try:
        data = memoryview((json.dumps(record, sort_keys=True) + "\n").encode())
        while data:
            n = os.write(fd, data)
            if n <= 0:
                raise OSError(f"journal write to {path} made no progress")
            data = data[n:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


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


def _record(home: Path, key: str, result: PushResult, real: str,
            receipt: Callable[[dict[str, Any]], Any]) -> list[str]:
    """The ``done`` journal line, then the receipt, each attempted whatever the other did. Returns what failed
    (empty when both were written)."""
    errs = []
    try:
        _journal(home, key, {"state": "done", "at": time.time(), **result.to_dict()})
    except Exception as exc:  # noqa: BLE001 - any failure here must still reach the receipt
        errs.append(f"done journal: {type(exc).__name__}: {exc}")
    try:
        receipt({**result.to_dict(), "repo": real})
    except Exception as exc:  # noqa: BLE001 - reported to the caller as a record error, never swallowed
        errs.append(f"receipt: {type(exc).__name__}: {exc}")
    return errs


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

    Raises :class:`PushRefused` for anything refused before the push (an ``OSError`` writing the intent line
    also aborts before it); git is resolved and the push argv built before the intent line, so no
    :class:`PushRefused` is raised after it. A rejection by git (non-fast-forward, a protected branch) is a :class:`PushResult`
    with ``ok`` False: reported, never forced. Once the push has started every path writes the ``done`` line and
    calls ``receipt``, each attempted whatever the other did: a push that did not report (timeout, git could not
    run, any other exception once the intent line is written) raises :class:`PushOutcomeUnknown` with status ``"unknown"``; a push that reported but whose done line
    or receipt failed raises :class:`PushRecordIncomplete` carrying the result."""
    try:
        canon = canonical_remote(remote)
    except ValueError as exc:
        raise PushRefused(f"remote: {exc}") from None
    if not isinstance(branch, str) or "*" in branch or (bp := branch_problem(branch)):
        raise PushRefused(f"branch: {bp if isinstance(branch, str) and '*' not in branch else 'not a branch name'}")
    gid = grant.get("id") if isinstance(grant, dict) else None
    if not isinstance(gid, str):
        raise PushRefused("the grant carries no id")
    real, ident = _repo_topology(repo_path)
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
        # the topology again, immediately before git reads the repository: the same directories, or refuse
        try:
            again = _repo_topology(real)
        except PushRefused as exc:
            raise PushRefused(f"{real} changed while waiting for the lock: {exc}") from None
        if again != (real, ident):
            raise PushRefused(f"{real} changed while waiting for the lock (a different repository, .git or object "
                              "store is at that path now)")
        f = _git(["git", f"--git-dir={mirror}", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
                  "-c", "protocol.fd.allow=never", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                  "--no-recurse-submodules", real, f"+refs/heads/{branch}:{SRC_REF}"], protocols="file")
        if f.returncode:
            raise PushRefused(f"could not read branch {branch} from {real}: {f.stderr.strip()[-400:]}")
        rp = _git(["git", f"--git-dir={mirror}", "rev-parse", "--verify", "--quiet", f"{SRC_REF}^{{commit}}"])
        new = rp.stdout.strip()
        if rp.returncode or not _SHA.match(new):
            raise PushRefused(f"branch {branch} of {real} is not a commit")
        # the private ref names the verified commit itself (a branch naming a tag object would push the tag)
        u = _git(["git", f"--git-dir={mirror}", "-c", "core.hooksPath=/dev/null", "update-ref", "--no-deref",
                  SRC_REF, new])
        if u.returncode:
            raise PushRefused(f"could not pin the mirror's ref to {new}: {u.stderr.strip()[-200:]}")
        # step 3: the pin must mean the destination
        if hits := _rewrites(mirror, canon):
            raise PushRefused(f"the git config rewrites the pinned remote ({hits[0]}); a pin must mean the "
                              "destination")
        # everything the push needs is resolved BEFORE the intent line: after it, nothing refuses
        gb = git_bin()
        argv = push_argv(mirror, canon, branch)
        proto = push_protocol(canon)
        base = {"grant_id": gid, "grant_seq": seq, "remote": canon, "branch": branch, "repo": real, "new_sha": new}
        _journal(home, key, {"state": "intent", "at": time.time(), **base})
        # from here on the remote may change: every path reaches the receipt
        try:
            p = _git(argv, protocols=proto, git=gb)
        except Exception as exc:  # noqa: BLE001 - after the intent line any failure is an unknown outcome
            result = PushResult(ok=False, status="unknown", grant_id=gid, grant_seq=seq, remote=canon,
                                branch=branch, old_sha=None, new_sha=new, exit=-1,
                                detail=f"git push did not report: {type(exc).__name__}: {exc}"[:400])
            errs = _record(home, key, result, real, receipt)
            raise PushOutcomeUnknown(f"the push of {branch} to {canon} may or may not have landed ({result.detail})"
                                     + (f"; record errors: {'; '.join(errs)}" if errs else ""),
                                     result, errs) from exc
        status, old, detail = _parse_porcelain(p.stdout, f"refs/heads/{branch}")
        if status == "error":
            detail = (p.stderr.strip() or p.stdout.strip())[-400:]
        ok = p.returncode == 0 and status in ("new", "fast-forward", "up-to-date")
        if status == "up-to-date":
            old = new
        result = PushResult(ok=ok, status=status, grant_id=gid, grant_seq=seq, remote=canon, branch=branch,
                            old_sha=old, new_sha=new, exit=p.returncode, detail=detail)
        # step 4: the done line and the receipt (policy head and the store are the caller's)
        if errs := _record(home, key, result, real, receipt):
            raise PushRecordIncomplete(f"the push completed ({status}) but its record is incomplete: "
                                       f"{'; '.join(errs)}", result, errs)
        return result
    finally:
        os.close(lock_fd)
