"""levain.dispatch.readset — the read set made physical: a scratch repo holding the contract's read files and nothing else.

A contract names the exact files an executor may read, each bound to a blob sha at ``base``. The slot's bundle step
(``levain.slot.stage.make_bundle``) clones a BRANCH of a repo, so handed the operator's repo it would carry every
tracked file and that branch's whole history. :func:`build_readset` builds the repo it is handed instead: a scratch
repo whose object database receives only the read blobs (copied across by bytes and checked against the contract's
sha), one tree made from them with the modes recorded at base, and ONE parentless commit. What the executor can
reach is then exactly what the contract bound: no other file, no history, no old blob in a pack.

Which contract is built is the caller's word checked against the record: the caller passes the digest the job store
recorded at filing (the one the human signs), and a contract whose own digest is not that one is refused before any
git work. A contract value edited after filing therefore no longer builds, whatever its read list says.

The commit is a pure function of the contract: a fixed identity, a fixed date, no parent, and a message naming the
contract's digest. Two builds of one contract name one sha, so a later stage compares a sha and builds nothing to
trust. The scratch is a directory this call creates (it must not exist yet) and is never inside the operator's repo,
judged by directory identity, not spelling; the operator's repo is only read, under
:func:`levain.dispatch.contract.git_env` with no user or system git config added on top (the scratch's own
environment, ``_scratch_env``), lazy fetch off and no transport allowed.

A write path that exists at base is in the read set only when the contract also names it as a read: the contract
binds each list on its own and this module follows it. A write path that is new is created by the executor.

The read set then crosses from filing to the run as one git bundle, and the run trusts its CONTENT, never its carrier:
:func:`readset_commit` derives the commit ``E`` and the object count ``N`` from the canonical contract bytes alone
(a pure function: no git, no repo), and every later step compares against that. :func:`export_readset` writes the
filing's bundle into a private, locked temp and returns it uninstalled; :func:`open_handoff` and :func:`verify_readset`
ingest it at the run: one exact copy into the attempt's own workdir, a fixed-prefix header compare and a pack-count
compare before git parses a byte, a bare fsck'd clone under resource limits, its refs and objects checked against
``E`` and ``N``, and the repo-layer settings check on ``E``'s tree. Stdlib only.
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass

from levain.dispatch import ContractRefused, DispatchError
from levain.dispatch.attempt_id import is_attempt_id
from levain.dispatch.contract import GIT_TIMEOUT, ResolvedContract, git_env, parse_contract

__all__ = ["BRANCH", "ExportedReadSet", "ReadSet", "ReadSetRefused", "VerifiedReadSet", "WORKDIR_PREFIX",
           "build_readset", "clone_limits", "discard", "export_readset", "full_fsync", "open_handoff",
           "readset_commit", "verify_readset", "workdir_name"]

_log = logging.getLogger(__name__)

#: The branch the scratch repo's one commit sits on; the supervisor bundles this name.
BRANCH = "readset"
_IDENTITY = "levain-dispatch"
_EMAIL = "levain-dispatch@localhost"
_DATE = "@0 +0000"   # the epoch; git refuses a bare "0"
_REGULAR = frozenset({"100644", "100755"})
_SHA1 = re.compile(r"[0-9a-f]{40}")
_INDEX = "readset.index"


class ReadSetRefused(DispatchError):
    """A read set that cannot be built, by name (``name``) with a human ``detail``. Nothing was handed on."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


@dataclass(frozen=True)
class ReadSet:
    """The built scratch repo: its directory, the branch and commit sha, and the commit's tree sha."""
    repo_dir: str
    branch: str
    commit: str
    tree: str


def _git(cwd: str, *args: str, env: dict[str, str], data: bytes | None = None) -> bytes:
    argv = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args]
    try:
        cp = subprocess.run(argv, capture_output=True, timeout=GIT_TIMEOUT, env=env,
                            input=data, stdin=None if data is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadSetRefused("readset-git-unavailable", f"git could not run ({type(exc).__name__})") from None
    if cp.returncode != 0:
        tail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ReadSetRefused("readset-git-failed", f"git {args[0]}: {tail[-1][:200] if tail else cp.returncode}")
    return cp.stdout


def _scratch_env() -> dict[str, str]:
    # DECISION: no user git config reaches the scratch (a signing or template setting there would change the commit
    # and break "one contract, one sha"); the identity and date are fixed here, after git_env scrubbed every GIT_*.
    env = git_env()
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": _IDENTITY, "GIT_AUTHOR_EMAIL": _EMAIL,
                "GIT_AUTHOR_DATE": _DATE, "GIT_COMMITTER_NAME": _IDENTITY, "GIT_COMMITTER_EMAIL": _EMAIL,
                "GIT_COMMITTER_DATE": _DATE, "GIT_ATTR_NOSYSTEM": "1"})
    return env


def _remove(dest_dir: str, made: tuple[int, int]) -> None:
    # only the directory this call made is removed: a path that is now a symlink, or another directory, is left alone
    try:
        st = os.lstat(dest_dir)
        if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == made:
            shutil.rmtree(dest_dir)
    except OSError:
        pass    # the refusal stands; a dest_dir left behind fails the must-not-exist check on any retry


def build_readset(repo: str, contract: ResolvedContract, dest_dir: str, *, contract_digest: str) -> ReadSet:
    """Build the read set of ``contract`` (read from ``repo``, which must be the contract's own) as a bare scratch repo
    in ``dest_dir`` (a real absolute path that must NOT exist yet; this call creates it, mode 0700 less the process umask), or raise
    :class:`ReadSetRefused`.

    ``contract_digest`` is the digest the job store recorded when the contract was filed (``Job.digest``, the one the
    human signs); ``contract`` is refused (``readset-digest-mismatch``) unless its own digest is that one. A refusal
    after ``dest_dir`` was created removes it again, only while it is still the directory this call made (same
    device and inode, not a symlink); otherwise it is left and the refusal stands. A ``dest_dir`` with the operator's
    repo among its ancestors (compared by device and inode, so another letter case or a symlink does not hide it) is
    refused (``readset-dest``). A source repo owned by another uid is refused (``readset-git-failed``): no user git
    config is read, so a ``safe.directory`` entry there does not apply. A blob missing from a partial clone is
    refused (``readset-git-failed``), never fetched."""
    if not isinstance(contract, ResolvedContract):
        raise ReadSetRefused("readset-contract", "not a ResolvedContract")
    if not isinstance(contract_digest, str) or contract.digest != contract_digest:
        raise ReadSetRefused("readset-digest-mismatch", "the contract is not the one filed under that digest")
    # the entries built from are decoded from the digest-bound canonical bytes under today's parser, never taken off
    # the caller's object
    try:
        parsed = parse_contract(contract.canonical())
    except ContractRefused as exc:
        raise ReadSetRefused("readset-contract", str(exc)[:200]) from None
    if not isinstance(repo, str) or repo != parsed.repo:
        raise ReadSetRefused("readset-repo", "the repo is not the contract's own")
    try:
        usable = (isinstance(dest_dir, str) and os.path.isabs(dest_dir) and os.path.realpath(dest_dir) == dest_dir)
    except (OSError, ValueError) as exc:
        raise ReadSetRefused("readset-dest", f"dest_dir cannot be read ({type(exc).__name__})") from None
    if not usable:
        raise ReadSetRefused("readset-dest", "dest_dir must be a real absolute path")
    for e in parsed.read:
        if e.mode not in _REGULAR or _SHA1.fullmatch(e.blob) is None:
            raise ReadSetRefused("readset-entry", f"read path {e.path[:120]} is not a regular blob entry")
    _path_checks([e.path for e in parsed.read])     # the run refuses these, so the filing does first
    try:
        rst = os.stat(repo)
    except (OSError, ValueError) as exc:
        raise ReadSetRefused("readset-repo", f"the repo cannot be read ({type(exc).__name__})") from None
    repo_id = (rst.st_dev, rst.st_ino)
    try:
        os.mkdir(dest_dir, 0o700)    # exclusive: a path that exists already is never taken over
        dst = os.lstat(dest_dir)
    except FileExistsError:
        raise ReadSetRefused("readset-dest", "dest_dir must not exist yet: build_readset creates it") from None
    except OSError as exc:
        raise ReadSetRefused("readset-dest", f"dest_dir cannot be created ({type(exc).__name__})") from None
    made = (dst.st_dev, dst.st_ino)

    env = _scratch_env()
    # a partial clone's missing blob is a refusal, never a fetch run under the source repo's own config:
    # GIT_NO_LAZY_FETCH is read from git 2.44 only, so no transport is allowed at all (an empty allow list)
    source_env = {**env, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0"}
    index = os.path.join(dest_dir, _INDEX)
    ienv = {**env, "GIT_INDEX_FILE": index}
    try:
        p = os.path.realpath(dest_dir)
        while True:
            try:
                ast = os.stat(p)
            except OSError as exc:
                raise ReadSetRefused("readset-dest", f"an ancestor of dest_dir cannot be read "
                                     f"({type(exc).__name__})") from None
            if (ast.st_dev, ast.st_ino) == repo_id:
                raise ReadSetRefused("readset-dest", "dest_dir is inside the operator's repo")
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        _git(dest_dir, "init", "--bare", "-q", "--template=", f"--initial-branch={BRANCH}", ".", env=env)
        try:
            for e in parsed.read:
                body = _git(repo, "cat-file", "blob", e.blob, env=source_env)
                got = _git(dest_dir, "hash-object", "-w", "--stdin", env=env, data=body).decode("ascii").strip()
                if got != e.blob:
                    raise ReadSetRefused("readset-blob-mismatch", f"{e.path[:120]}: the bytes hash to {got[:12]}, "
                                         f"the contract binds {e.blob[:12]}")
                _git(dest_dir, "update-index", "--add", "--cacheinfo", f"{e.mode},{e.blob},{e.path}", env=ienv)
            tree = _git(dest_dir, "write-tree", env=ienv).decode("ascii").strip()
        finally:
            try:
                os.unlink(index)
            except FileNotFoundError:
                pass
        commit = _git(dest_dir, "commit-tree", tree, "-m", f"levain read set {parsed.digest}",
                      env=env).decode("ascii").strip()
        _git(dest_dir, "update-ref", f"refs/heads/{BRANCH}", commit, env=env)
    except BaseException:
        _remove(dest_dir, made)
        raise
    return ReadSet(repo_dir=dest_dir, branch=BRANCH, commit=commit, tree=tree)


# --- the commit, derived from the contract bytes alone ----------------------------------------------------------------

#: APFS's name limit, in UTF-8 bytes of the NFD form (decomposition lengthens an NFC name).
COMPONENT_MAX_BYTES = 255
#: macOS's PATH_MAX, the terminating NUL counted; the slot checks a read path out under ``<S>/repo/``.
CHECKOUT_PATH_MAX = 1024


def _caseless(component: str) -> str:
    # Unicode canonical caseless matching (D145): NFD(casefold(NFD(casefold(NFD(c)))))
    nfd = unicodedata.normalize
    return nfd("NFD", nfd("NFD", nfd("NFD", component).casefold()).casefold())


def checkout_prefix() -> str:
    """The longest ``<S>/repo/`` the slot can have, ``realpath``'d (so ``/var`` -> ``/private/var`` counts at its real
    length): the session-dir constant at the longest session id the grammar admits."""
    from levain.slot.grammar import SESSION_RE
    from levain.slot.layout import session_repo

    host = "darwin" if sys.platform == "darwin" else "linux"
    longest = "s-" + "f" * 16
    if not SESSION_RE.fullmatch(longest) or SESSION_RE.fullmatch(longest + "f"):
        # the bound is only right while a session id has exactly this one length
        raise ReadSetRefused("readset-path-length", "the session-id grammar changed: the checkout bound is stale")
    return os.path.realpath(str(session_repo(host, longest))) + "/"


def _path_checks(paths: list[str]) -> None:
    """One trie of the read paths, keyed per component by its caseless form; each edge keeps its first spelling and
    whether it ends a file. A second spelling of a key at any level, or a key used both as a file and as a directory,
    is ``readset-path-collision``; a component longer than :data:`COMPONENT_MAX_BYTES` in NFD, or a whole path that
    cannot be checked out under :func:`checkout_prefix`, is ``readset-path-length``."""
    prefix_len = len(os.fsencode(checkout_prefix()))
    root: dict = {}
    for path in paths:
        nfd_len = len(unicodedata.normalize("NFD", path).encode("utf-8"))
        if prefix_len + nfd_len + 1 > CHECKOUT_PATH_MAX:
            raise ReadSetRefused("readset-path-length", f"{path[:120]}: {prefix_len} + {nfd_len} + 1 bytes is over "
                                 f"{CHECKOUT_PATH_MAX}")
        parts = path.split("/")
        node = root
        for i, comp in enumerate(parts):
            if len(unicodedata.normalize("NFD", comp).encode("utf-8")) > COMPONENT_MAX_BYTES:
                raise ReadSetRefused("readset-path-length", f"{path[:120]}: a component is over "
                                     f"{COMPONENT_MAX_BYTES} bytes in NFD")
            is_file = i == len(parts) - 1
            key = _caseless(comp)
            edge = node.get(key)
            if edge is None:
                edge = node[key] = (comp, is_file, {})
            elif edge[0] != comp:
                raise ReadSetRefused("readset-path-collision", f"{path[:120]}: {comp[:60]!r} folds onto "
                                     f"{edge[0][:60]!r}")
            elif edge[1] or is_file:
                # a file needed as a directory, a directory needed as a file, or (refused at parse already) a repeat
                raise ReadSetRefused("readset-path-collision", f"{path[:120]}: {comp[:60]!r} is both a file and a "
                                     f"directory")
            node = edge[2]


def _git_object(kind: bytes, body: bytes) -> bytes:
    return hashlib.sha1(kind + b" %d\0" % len(body) + body).digest()


def readset_commit(contract_bytes: bytes, D: str) -> tuple[str, str, int]:
    """``(E, tree, N)`` for the contract whose canonical bytes are ``contract_bytes``: the sha of the one commit
    :func:`build_readset` makes for it, its tree's sha, and the commit's object closure (1 commit + its distinct trees
    + its distinct blobs). Pure: no git, no repo. Refused ``readset-contract`` when the bytes are not a canonical
    contract, ``readset-digest-mismatch`` unless their digest is ``D``, ``readset-entry`` for a read entry that is not
    a regular blob, and by :func:`_path_checks`."""
    try:
        parsed = parse_contract(contract_bytes)
    except ContractRefused as exc:
        raise ReadSetRefused("readset-contract", str(exc)[:200]) from None
    if not isinstance(D, str) or parsed.digest != D:
        raise ReadSetRefused("readset-digest-mismatch", "the contract bytes are not the ones filed under that digest")
    for e in parsed.read:
        if e.mode not in _REGULAR or _SHA1.fullmatch(e.blob) is None:
            raise ReadSetRefused("readset-entry", f"read path {e.path[:120]} is not a regular blob entry")
    _path_checks([e.path for e in parsed.read])

    root: dict = {}
    for e in parsed.read:
        *dirs, name = e.path.split("/")
        node = root
        for d in dirs:
            node = node.setdefault(d, {})
        node[name] = (e.mode, e.blob)
    trees: set[bytes] = set()

    def tree_sha(node: dict) -> bytes:
        items = []
        for name, val in node.items():
            raw = name.encode("utf-8")
            if isinstance(val, dict):
                items.append((raw + b"/", b"40000 " + raw + b"\0" + tree_sha(val)))
            else:
                items.append((raw, val[0].encode("ascii") + b" " + raw + b"\0" + bytes.fromhex(val[1])))
        sha = _git_object(b"tree", b"".join(entry for _, entry in sorted(items)))
        trees.add(sha)
        return sha

    tree = tree_sha(root).hex()
    who = f"{_IDENTITY} <{_EMAIL}> 0 +0000"
    body = f"tree {tree}\nauthor {who}\ncommitter {who}\n\nlevain read set {parsed.digest}\n".encode("utf-8")
    commit = _git_object(b"commit", body).hex()
    return commit, tree, 1 + len(trees) + len({e.blob for e in parsed.read})


# --- durability -----------------------------------------------------------------------------------------------------

_FALLBACK_ERRNOS = frozenset({errno.ENOTSUP, errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOSYS,
                              errno.ENODEV})
_fallback_logged = False


def full_fsync(fd: int) -> str:
    """Flush ``fd`` to stable storage and name the call that did it: ``F_FULLFSYNC`` (which also flushes the drive's
    cache on macOS), or ``fsync`` where that call does not exist on this Python or this filesystem (logged once per
    process). Every other error propagates: a failing disk is never reported as durable."""
    global _fallback_logged
    try:
        import fcntl
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        return "F_FULLFSYNC"
    except AttributeError:
        why = "no F_FULLFSYNC on this Python"
    except OSError as exc:
        if exc.errno not in _FALLBACK_ERRNOS:
            raise
        why = f"F_FULLFSYNC unsupported here ({errno.errorcode.get(exc.errno, exc.errno)})"
    if not _fallback_logged:
        _fallback_logged = True
        _log.warning("readset: %s; falling back to fsync (ordering holds across process kill, not power loss)", why)
    os.fsync(fd)
    return "fsync"


# --- the bundle's fixed prefix ------------------------------------------------------------------------------------------

def _header(E: str) -> bytes:
    e = E.encode("ascii")
    return b"# v2 git bundle\n" + e + b" HEAD\n" + e + b" refs/heads/" + BRANCH.encode("ascii") + b"\n\n"


def _prefix_problem(fd: int, E: str, N: int) -> str | None:
    """``None`` when ``fd``'s first bytes are exactly the bundle header for ``E`` followed by a pack header of ``N``
    objects; else which half differs (``header`` or ``pack``). One fixed-size read, no line parser."""
    h = _header(E)
    got = os.pread(fd, len(h) + 12, 0)
    if len(got) < len(h) or got[:len(h)] != h:
        return "header"
    pack = got[len(h):]
    if len(pack) != 12 or pack[:4] != b"PACK":
        return "pack"
    version, count = struct.unpack(">II", pack[4:])
    if version not in (2, 3) or count != N:
        return "pack"
    return None


def _bundle_into(repo_dir: str, fd: int, env: dict[str, str]) -> None:
    # git's own `bundle create <path>` writes <path>.lock and renames it: stdout into an fd this module opened instead
    argv = ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", repo_dir, "bundle", "create", "-q",
            "-", "HEAD", f"refs/heads/{BRANCH}"]
    try:
        cp = subprocess.run(argv, stdout=fd, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT,
                            env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReadSetRefused("readset-git-unavailable", f"git could not run ({type(exc).__name__})") from None
    if cp.returncode != 0:
        tail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ReadSetRefused("readset-git-failed", f"git bundle: {tail[-1][:200] if tail else cp.returncode}")


def _open_dir(path: str) -> int:
    """An fd on the directory at ``path``, never through a symlink at its last component."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


# --- F3: the filing's export -----------------------------------------------------------------------------------------

#: The handoff dir's private subdirectory the filing's temps live in (the same filesystem, so an install is a link).
TMP_DIR = ".tmp"


@dataclass(frozen=True)
class ExportedReadSet:
    """The filing's bundle, written, flushed and read back, but NOT installed: ``path`` under the handoff dir's
    ``.tmp/``, ``size`` bytes, and ``fd`` open on it holding an exclusive ``flock`` (so a cleaner that takes the lock
    non-blocking skips it).
    The installer links ``path`` into the live dir, unlinks the temp, then closes ``fd``. ``fsync`` names the call
    that flushed it."""
    path: str
    size: int
    fd: int
    commit: str
    objects: int
    fsync: str


def _lock_at_create(dir_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    exlock = getattr(os, "O_EXLOCK", 0)
    fd = os.open(name, flags | exlock, 0o600, dir_fd=dir_fd)
    if not exlock:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            os.unlink(name, dir_fd=dir_fd)
            raise
    return fd


def export_readset(rs: ReadSet, contract_bytes: bytes, D: str, handoff_dir: str) -> ExportedReadSet:
    """F3: re-derive ``E`` and ``N`` from the contract bytes (:func:`readset_commit`), require ``rs.commit``, the
    scratch's ``HEAD`` and its ``refs/heads/readset`` each to be ``E`` (``readset-commit-mismatch``), run the
    repo-layer settings check on ``E``'s tree (so a contract the run would refuse is never put to the human to sign),
    then write ``git bundle create - HEAD refs/heads/readset`` into a fresh private temp under ``handoff_dir/.tmp/``
    (``O_EXCL|O_NOFOLLOW``, 0600, exclusively locked at create and held), flush it with :func:`full_fsync`, and read it
    back: its first bytes must be exactly the bundle header for ``E`` and a pack header of ``N`` objects, else
    ``readset-commit-mismatch`` and the temp is unlinked (and ``.tmp/`` flushed). Returns the temp UNINSTALLED; it
    never renames into ``handoff_dir``. ``handoff_dir`` must be a real absolute directory (``readset-dest``)."""
    E, _tree, N = readset_commit(contract_bytes, D)
    if not isinstance(rs, ReadSet):
        raise ReadSetRefused("readset-contract", "not a ReadSet")
    env = _scratch_env()
    if rs.commit != E:
        raise ReadSetRefused("readset-commit-mismatch", "the builder's commit is not the contract's")
    for ref in ("HEAD", f"refs/heads/{BRANCH}"):
        got = _git(rs.repo_dir, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", env=env).decode("ascii").strip()
        if got != E:
            raise ReadSetRefused("readset-commit-mismatch", f"the scratch's {ref} is not the contract's commit")
    _layer_check(rs.repo_dir, E)
    try:
        usable = isinstance(handoff_dir, str) and os.path.isabs(handoff_dir) and \
            os.path.realpath(handoff_dir) == handoff_dir
        hfd = _open_dir(handoff_dir) if usable else -1
    except (OSError, ValueError) as exc:
        raise ReadSetRefused("readset-dest", f"the handoff dir cannot be opened ({type(exc).__name__})") from None
    if hfd < 0:
        raise ReadSetRefused("readset-dest", "the handoff dir must be a real absolute path")
    try:
        try:
            os.mkdir(TMP_DIR, 0o700, dir_fd=hfd)
        except FileExistsError:
            pass
        tfd = os.open(TMP_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=hfd)
    except OSError as exc:
        os.close(hfd)
        raise ReadSetRefused("readset-dest", f"the handoff dir's {TMP_DIR}/ cannot be opened "
                             f"({type(exc).__name__})") from None
    os.close(hfd)
    try:
        name = "x-" + secrets.token_hex(16)
        fd = _lock_at_create(tfd, name)
        try:
            _bundle_into(rs.repo_dir, fd, env)
            how = full_fsync(fd)
            problem = _prefix_problem(fd, E, N)
            if problem is not None:
                raise ReadSetRefused("readset-commit-mismatch", f"the bundle written has another {problem}")
            size = os.fstat(fd).st_size
        except BaseException:
            os.close(fd)
            try:
                os.unlink(name, dir_fd=tfd)
                full_fsync(tfd)
            except OSError:
                pass        # the refusal stands; an unlinked-or-not temp is unlocked now, so startup GC takes it
            raise
    finally:
        os.close(tfd)
    return ExportedReadSet(path=os.path.join(handoff_dir, TMP_DIR, name), size=size, fd=fd, commit=E, objects=N,
                           fsync=how)


# --- R3: the run's ingest ----------------------------------------------------------------------------------------------

def _limits():
    from levain.slot.stage import BUNDLE_CAP, STAGE_DEADLINE_S

    return BUNDLE_CAP, STAGE_DEADLINE_S


def open_handoff(path: str, *, size: int) -> int:
    """An fd on the handoff bundle at ``path``: opened ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY|O_CLOEXEC``, then
    ``fstat``ed. A symlink at ``path``, anything that is not a regular file (a FIFO is never read), a file over the
    handoff cap, or a size other than ``size`` (the one the handoff row recorded) is ``readset-handoff``; the fd is
    closed on every refusal. Later steps read the fd, never ``path`` again."""
    cap, _ = _limits()
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError, TypeError) as exc:
        raise ReadSetRefused("readset-handoff", f"the handoff cannot be opened ({type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReadSetRefused("readset-handoff", "the handoff is not a regular file")
        if st.st_size > cap:
            raise ReadSetRefused("readset-handoff", f"the handoff is over {cap} bytes")
        if type(size) is not int or st.st_size != size:
            raise ReadSetRefused("readset-handoff", "the handoff is not the size its row recorded")
    except BaseException:
        os.close(fd)
        raise
    return fd


#: A run's workdir is named ``WORKDIR_PREFIX + <attempt id>`` under the dispatch-owned parent.
WORKDIR_PREFIX = "w-"
_COPY = "handoff.bundle"
_P = "P"
_CHUNK = 1 << 20
#: [judged] CPU seconds for the ingest clone: twice its wall-clock timeout.
CLONE_CPU_S = int(2 * GIT_TIMEOUT)
#: [judged] processes the clone may add to the uid's own count (git's index-pack and fsck children).
CLONE_NPROC_MARGIN = 64
#: [judged] address space on Linux, where it is enforced (macOS does not enforce RLIMIT_AS).
CLONE_AS_BYTES = 4 << 30


def workdir_name(attempt: str) -> str:
    """The workdir's name for ``attempt``, refused (``readset-attempt``) unless the id is in the attempt-id grammar,
    so nothing else ever reaches the path join."""
    if not is_attempt_id(attempt):
        raise ReadSetRefused("readset-attempt", "the attempt id is not in the attempt-id grammar")
    return WORKDIR_PREFIX + attempt


@dataclass(frozen=True)
class VerifiedReadSet:
    """What :func:`verify_readset` checked: the attempt, its workdir, ``P``'s identity (dev/ino), the commit ``E``,
    the digest ``D`` and the object count ``N``, plus the resource limits the clone ran under. A record, not a
    capability: any caller can construct one, so every sink re-checks it against the contract bytes."""
    attempt: str
    workdir: str
    p_dev: int
    p_ino: int
    commit: str
    digest: str
    objects: int
    limits: tuple = ()


def _uid_process_count() -> int:
    uid = os.getuid()
    if os.path.isdir("/proc/self"):
        n = 0
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    if os.stat(f"/proc/{pid}").st_uid == uid:
                        n += 1
                except OSError:
                    pass
        return n
    cp = subprocess.run(["ps", "-U", str(uid), "-o", "pid="], capture_output=True, timeout=10,
                        stdin=subprocess.DEVNULL)
    return len(cp.stdout.split())


def clone_limits() -> tuple:
    """``((name, value), ...)``: the rlimits the ingest clone's child runs under, measured now. ``RLIMIT_NPROC``
    counts every process of the real uid, so it is this uid's process count plus :data:`CLONE_NPROC_MARGIN`."""
    import resource

    cap, _ = _limits()
    out = [("RLIMIT_FSIZE", cap + (1 << 30)), ("RLIMIT_CPU", CLONE_CPU_S),
           ("RLIMIT_NPROC", _uid_process_count() + CLONE_NPROC_MARGIN)]
    if sys.platform.startswith("linux"):
        out.append(("RLIMIT_AS", CLONE_AS_BYTES))
    return tuple((name, value) for name, value in out if hasattr(resource, name))


def _rlimit_preexec(limits: tuple):
    def apply() -> None:
        import resource

        for name, value in limits:
            which = getattr(resource, name)
            _soft, hard = resource.getrlimit(which)
            v = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (v, v))
    return apply


#: Measured, then adopted: a blob over this size is streamed while indexing instead of inflated whole in memory (the
#: ingest clone's peak memory on a small pack holding a large blob of zeros fell from about the blob's size to a few MiB).
CLONE_BIG_FILE_THRESHOLD = "1m"


def clone_argv(bundle: str, dest: str, *, extra: tuple = ()) -> list[str]:
    """The ingest clone: bare, fsck'd, no hooks, no template, no local shortcut, large blobs streamed. ``extra`` is
    ``-c`` settings for a measurement only (a later ``-c`` overrides an earlier one)."""
    return ["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-c", "transfer.fsckObjects=true",
            "-c", f"core.bigFileThreshold={CLONE_BIG_FILE_THRESHOLD}", *extra, "clone", "--bare", "--no-local",
            "--template=", "-q", "--", bundle, dest]


def _clone_env() -> dict[str, str]:
    # an empty allow list refuses a bundle clone; `file` admits it and nothing else
    return {**_scratch_env(), "GIT_ALLOW_PROTOCOL": "file", "GIT_PROTOCOL_FROM_USER": "0"}


def _p_env() -> dict[str, str]:
    return {**_scratch_env(), "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0", "GIT_NO_LAZY_FETCH": "1"}


def _layer_check(repo_dir: str, E: str) -> None:
    from pathlib import Path

    from levain.slot import Refused
    from levain.slot.supervisor import check_repo_layer, repo_layer_docs

    try:
        check_repo_layer(repo_layer_docs(Path(repo_dir), treeish=E))
    except Refused as exc:
        raise ReadSetRefused(exc.name, exc.detail) from None


def _copy_exact(src: int, dst: int, size: int, deadline_s: float) -> None:
    end = time.monotonic() + deadline_s
    off = 0
    while off < size:
        chunk = os.pread(src, min(_CHUNK, size - off), off)
        if not chunk:
            raise ReadSetRefused("readset-handoff-raced", "the handoff ended early")
        view = memoryview(chunk)
        while view:
            n = os.write(dst, view)
            if n <= 0:
                raise OSError(errno.EIO, "short write")
            view = view[n:]
        off += len(chunk)
        if time.monotonic() > end:
            raise ReadSetRefused("readset-handoff-raced", "the handoff copy passed its deadline")
    if os.pread(src, 1, size):
        raise ReadSetRefused("readset-handoff-raced", "the handoff grew past its size")


def _rmtree_at(parent_fd: int, name: str, made: tuple[int, int]) -> None:
    # only the directory this call made, by dev/ino, never through a symlink
    try:
        st = os.lstat(name, dir_fd=parent_fd)
        if stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == made and shutil.rmtree.avoids_symlink_attacks:
            shutil.rmtree(name, dir_fd=parent_fd)
    except OSError:
        pass


def verify_readset(contract_bytes: bytes, D: str, fd: int, parent: str, attempt: str) -> VerifiedReadSet:
    """R3: ingest the handoff on ``fd`` (from :func:`open_handoff`) into attempt ``attempt``'s own workdir ``W`` under
    ``parent`` and check it against ``E`` and ``N`` derived here from (``contract_bytes``, ``D``), before any lease.

    ``W`` is ``parent/`` + :func:`workdir_name` (``attempt`` validated first), made by an exclusive ``mkdir`` 0700; a
    ``W`` that exists already is ``readset-workdir``. Exactly ``fstat(fd).st_size`` bytes are copied into
    ``W/handoff.bundle`` and EOF is required, under the stage deadline (``readset-handoff-raced``); every later step
    reads that copy only. Then: the fixed-prefix header compare (``readset-handoff``), the pack header's count == ``N``
    (``readset-surplus``), a bare fsck'd clone into ``W/P`` under :func:`clone_limits` (``readset-git-failed``),
    render's attributes line in ``P/info/attributes``, the origin remote removed, ``HEAD`` and ``refs/heads/readset``
    == ``E`` and no other ref (``readset-head-mismatch``), ``count-objects`` in-pack == ``N`` and no loose object
    (``readset-surplus``), and the repo-layer settings check on ``E``'s tree (refused by the loosening's own name).
    Any refusal removes ``W`` while it is still the directory this call made."""
    E, _tree, N = readset_commit(contract_bytes, D)
    name = workdir_name(attempt)
    cap, deadline_s = _limits()
    try:
        usable = isinstance(parent, str) and os.path.isabs(parent) and os.path.realpath(parent) == parent
        pfd = _open_dir(parent) if usable else -1
    except (OSError, ValueError) as exc:
        raise ReadSetRefused("readset-workdir", f"the workdir parent cannot be opened ({type(exc).__name__})") from None
    if pfd < 0:
        raise ReadSetRefused("readset-workdir", "the workdir parent must be a real absolute path")
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
            wst = os.lstat(name, dir_fd=pfd)
        except FileExistsError:
            raise ReadSetRefused("readset-workdir", "the attempt's workdir exists already") from None
        except OSError as exc:
            raise ReadSetRefused("readset-workdir", f"the workdir cannot be made ({type(exc).__name__})") from None
        made = (wst.st_dev, wst.st_ino)
        try:
            return _verify_in(pfd, D, fd, parent, name, attempt, E, N, cap, deadline_s)
        except BaseException:
            _rmtree_at(pfd, name, made)
            raise
    finally:
        os.close(pfd)


def _verify_in(pfd: int, D: str, fd: int, parent: str, name: str, attempt: str, E: str, N: int,
               cap: int, deadline_s: float) -> VerifiedReadSet:
    workdir = os.path.join(parent, name)
    wfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    try:
        try:
            size = os.fstat(fd).st_size
            # the same compare on the source first, so a handoff that will be refused is never copied; the copy's
            # compare below is the one that counts
            early = _prefix_problem(fd, E, N)
        except (OSError, TypeError) as exc:
            raise ReadSetRefused("readset-handoff", f"the handoff fd cannot be read ({type(exc).__name__})") from None
        if size > cap:
            raise ReadSetRefused("readset-handoff", f"the handoff is over {cap} bytes")
        problem = early
        if problem is None:
            cfd = os.open(_COPY, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=wfd)
            try:
                _copy_exact(fd, cfd, size, deadline_s)
                problem = _prefix_problem(cfd, E, N)
            finally:
                os.close(cfd)
        if problem == "header":
            raise ReadSetRefused("readset-handoff", "the bundle header is not the read set's")
        if problem == "pack":
            raise ReadSetRefused("readset-surplus", f"the pack header does not hold exactly {N} objects")
        copy, p = os.path.join(workdir, _COPY), os.path.join(workdir, _P)
        limits = clone_limits()
        try:
            cp = subprocess.run(clone_argv(copy, p), capture_output=True, timeout=GIT_TIMEOUT, env=_clone_env(),
                                stdin=subprocess.DEVNULL, cwd=workdir, preexec_fn=_rlimit_preexec(limits))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadSetRefused("readset-git-unavailable", f"git could not run ({type(exc).__name__})") from None
        if cp.returncode != 0:
            tail = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            # the first line names the cause (an fsck message); the last is the transport's generic one
            raise ReadSetRefused("readset-git-failed", f"git clone: {' | '.join(tail)[:300] if tail else cp.returncode}")
        pst = os.lstat(p)
        if not stat.S_ISDIR(pst.st_mode):
            raise ReadSetRefused("readset-git-failed", "the clone is not a directory")
        _write_attributes(p)
        env = _p_env()
        _git(p, "remote", "remove", "origin", env=env)
        for ref in ("HEAD", f"refs/heads/{BRANCH}"):
            got = _git(p, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", env=env).decode("ascii").strip()
            if got != E:
                raise ReadSetRefused("readset-head-mismatch", f"{ref} is not the contract's commit")
        refs = _git(p, "for-each-ref", "--format=%(refname)", env=env).decode("utf-8", "replace").split()
        if refs != [f"refs/heads/{BRANCH}"]:
            raise ReadSetRefused("readset-head-mismatch", f"the clone holds refs {refs[:4]!r}")
        counts = dict(line.split(": ", 1) for line in
                      _git(p, "count-objects", "-v", env=env).decode("ascii").splitlines() if ": " in line)
        if counts.get("in-pack") != str(N) or counts.get("count") != "0":
            raise ReadSetRefused("readset-surplus", f"in-pack {counts.get('in-pack')}, loose {counts.get('count')}; "
                                 f"the read set is {N}")
        _layer_check(p, E)
        pst2 = os.lstat(p)
        if (pst2.st_dev, pst2.st_ino) != (pst.st_dev, pst.st_ino):
            raise ReadSetRefused("readset-git-failed", "the clone was replaced during the checks")
        return VerifiedReadSet(attempt=attempt, workdir=workdir, p_dev=pst.st_dev, p_ino=pst.st_ino, commit=E,
                               digest=D, objects=N, limits=limits)
    finally:
        os.close(wfd)


def _write_attributes(p: str) -> None:
    # P is bare and checks nothing out; the line keeps every later command on P free of conversions
    from levain.slot.render import READSET_ATTRIBUTES

    info = os.path.join(p, "info")
    try:
        os.mkdir(info, 0o700)
    except FileExistsError:
        pass
    ifd = os.open(info, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        afd = os.open("attributes", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                      dir_fd=ifd)
        try:
            os.write(afd, READSET_ATTRIBUTES)
        finally:
            os.close(afd)
    finally:
        os.close(ifd)


def discard(v: VerifiedReadSet) -> None:
    """R10: remove ``v``'s workdir (the copy and ``P``), only while it is a real directory named for ``v.attempt``
    (never through a symlink, fd-relative from its parent). Anything else is left; the next supervisor start's sweep
    takes a stale one."""
    if not isinstance(v, VerifiedReadSet) or not is_attempt_id(v.attempt):
        return
    parent, name = os.path.split(v.workdir)
    if name != WORKDIR_PREFIX + v.attempt or not shutil.rmtree.avoids_symlink_attacks:
        return
    try:
        pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        st = os.lstat(name, dir_fd=pfd)
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(name, dir_fd=pfd)
    except OSError:
        pass
    finally:
        os.close(pfd)
