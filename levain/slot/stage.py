"""Staging -> ``<S>/in/``: ONE byte-copy primitive, run as the lease peer (CONTRACT §5.4, V19, V24, V25).

``render`` (3) never chowns, opens or follows anything in staging. :func:`stage_copy` forks a READER that drops to
alloc ``staging_uid`` (drop + assert as §8.2) before it opens anything, opens the one name under the staging dir with
``O_NOFOLLOW|O_NONBLOCK|O_NOCTTY``, refuses a non-regular file (a FIFO cannot hang it), takes ``accepted_size`` from
``fstat`` and streams exactly that many bytes to root over a pipe, then reports whether one more byte was there.
Root writes them into a FRESH file it creates (``O_CREAT|O_EXCL``), checks the fixed 16-byte git header and the
digest, and fsyncs. An early EOF, an extra byte or the deadline = ``staging-raced``, and root unlinks its fresh
destination; so does every other refusal after the destination exists. The reader can read nothing its uid could not
already read, so a planted link (hard or symbolic) yields at most the lease peer's own bytes into its own session.

Seams (every OS act a unit test cannot do as a non-root user): ``run_as`` (the fork + drop; default
:func:`fork_run_as`), ``chown`` (the destination's ``fchown``), ``clock`` (the V24 deadline).

Wire from the reader to root, over the pipe: ``R`` + 1-byte length + refusal name, or ``S`` + 8-byte big-endian
``accepted_size`` + exactly that many bytes + one trailer byte, ``E`` (EOF where it should be) or ``X`` (an extra
byte). A pipe EOF before the trailer is an early EOF.

:func:`make_bundle` is the supervisor's side (§5.4 1): the bundle the reader later copies.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import select
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from levain.slot import Refused, write_all
from levain.slot.render import parse_driver_input

BUNDLE_CAP = 8 * 1024 ** 3          # §11 V19
STAGE_DEADLINE_S = 600              # §11 V24
DRIVER_INPUT_CAP = 256 * 1024       # §11 V25
BUNDLE_HEADERS = (b"# v2 git bundle\n", b"# v3 git bundle\n")   # §5.4: the FIXED 16-byte header
HEADER_LEN = 16
CHUNK = 64 * 1024
POLL_SLICE_S = 1.0                  # DECISION: how often root re-reads the clock while the reader is silent

BUNDLE_NAME = "repo.bundle"
DRIVER_INPUT_NAME = "driver_input.json"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}", re.ASCII)
#: The names the reader may send back; anything else from the pipe is ``staging-reader-failed``.
_READER_REFUSALS = frozenset({"staging-missing", "staging-not-regular", "staging-open-failed",
                              "staging-raced", "bundle-cap", "driver-input-mismatch"})

_O_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_O_DIR = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
_O_DEST = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class Reader(Protocol):
    """What ``run_as`` hands back: the running reader, which root can kill (deadline) and must reap."""

    def kill(self) -> None: ...
    def wait(self) -> None: ...


#: ``run_as(uid, gid, fn, wfd)``: run ``fn(wfd)`` as ``uid``/``gid`` and own ``wfd`` (close it when ``fn`` ends).
RunAs = Callable[[int, int, Callable[[int], None], int], Reader]


@dataclass(frozen=True)
class StageResult:
    size: int
    digest: str      # "sha256:" + 64 hex, over every byte written


# --- the drop (§8.2) -------------------------------------------------------------------------------------------

def drop_and_assert(uid: int, gid: int) -> None:
    """§8.2's drop + assert, for a process that is root: groups, gid, uid (each call checked by its raising), then
    real + effective uid/gid == the target, groups == exactly ``{gid}``, uid != 0, and ``seteuid(0)`` /
    ``setegid(0)`` both FAIL. Any miss raises; the caller exits without reading."""
    if uid == 0 or gid == 0:
        raise PermissionError("refusing to drop to root")
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (uid, uid, gid, gid):
        raise PermissionError("ids after drop")
    if set(os.getgroups()) - {gid}:
        raise PermissionError("groups after drop")
    for regain in (os.seteuid, os.setegid):
        try:
            regain(0)
        except PermissionError:
            continue
        raise PermissionError(f"{regain.__name__}(0) succeeded after the drop")


def _close_fds_except(keep: Sequence[int]) -> None:
    hi = min(os.sysconf("SC_OPEN_MAX"), 65536)
    lo = 3
    for fd in sorted(k for k in keep if k >= 3):
        os.closerange(lo, fd)
        lo = fd + 1
    os.closerange(lo, hi)


class _Forked:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._reaped = False

    def kill(self) -> None:
        if not self._reaped:
            try:
                os.kill(self.pid, 9)
            except ProcessLookupError:
                pass

    def wait(self) -> None:
        if not self._reaped:
            os.waitpid(self.pid, 0)
            self._reaped = True


def fork_run_as(uid: int, gid: int, fn: Callable[[int], None], wfd: int) -> Reader:
    """The real ``run_as``: fork; the child closes every fd but 0-2 and ``wfd``, drops + asserts, runs ``fn`` and
    ``_exit``s (70 if the drop or ``fn`` raised, having written nothing more). Only root can run it."""
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the child; exercised only as root (VM row)
        code = 0
        try:
            _close_fds_except([wfd])
            drop_and_assert(uid, gid)
            fn(wfd)
        except BaseException:   # noqa: BLE001 - the child never returns into the parent's stack
            code = 70
        finally:
            os._exit(code)
    os.close(wfd)
    return _Forked(pid)


class _Threaded:
    def __init__(self, t: threading.Thread, stop: threading.Event) -> None:
        self._t, self._stop = t, stop

    def kill(self) -> None:
        self._stop.set()

    def wait(self) -> None:
        self._t.join(timeout=30)


def thread_run_as(uid: int, gid: int, fn: Callable[[int], None], wfd: int) -> Reader:
    """An in-process ``run_as`` for unit tests (no fork, no drop: the caller is not root). ``fn`` runs on a thread
    and ``wfd`` is closed when it ends."""
    stop = threading.Event()

    def body() -> None:
        try:
            fn(wfd)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                os.close(wfd)
            except OSError:
                pass

    t = threading.Thread(target=body, daemon=True)
    t.start()
    return _Threaded(t, stop)


# --- the reader (runs as staging_uid) ----------------------------------------------------------------------------

def _send_refusal(wfd: int, name: str) -> None:
    b = name.encode("ascii")
    write_all(wfd, b"R" + bytes([len(b)]) + b)


def reader(staging: int | str | os.PathLike[str], name: str, cap: int, cap_refusal: str, wfd: int) -> None:
    """The reader's whole job, after the drop. ``staging``: a dirfd, or the staging dir's path (opened here, as the
    lease peer, so root never opens it either)."""
    dfd = None
    try:
        if isinstance(staging, int):
            dfd = staging
        else:
            dfd = os.open(os.fspath(staging), _O_DIR)
        try:
            fd = os.open(name, _O_READ, dir_fd=dfd)
        except FileNotFoundError:
            return _send_refusal(wfd, "staging-missing")
        except OSError as e:
            # O_NOFOLLOW on a symlink: ELOOP (Linux, macOS) or EMLINK (FreeBSD's spelling).
            return _send_refusal(wfd, "staging-not-regular" if e.errno in (errno.ELOOP, errno.EMLINK)
                                 else "staging-open-failed")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return _send_refusal(wfd, "staging-not-regular")
            accepted = st.st_size
            if accepted > cap:
                return _send_refusal(wfd, cap_refusal)
            write_all(wfd, b"S" + struct.pack(">Q", accepted))
            left = accepted
            while left:
                try:
                    chunk = os.read(fd, min(CHUNK, left))
                except BlockingIOError:   # O_NONBLOCK: a regular file never says so; refuse rather than spin
                    return
                if not chunk:
                    return              # early EOF: the pipe closes short, root refuses staging-raced
                write_all(wfd, chunk)
                left -= len(chunk)
            write_all(wfd, b"X" if os.read(fd, 1) else b"E")
        finally:
            os.close(fd)
    finally:
        if dfd is not None and not isinstance(staging, int):
            os.close(dfd)


# --- root's side ----------------------------------------------------------------------------------------------------

class _Pipe:
    """Root's read end, every read under the deadline."""

    def __init__(self, rfd: int, deadline: float, clock: Callable[[], float], poll_slice: float) -> None:
        self.rfd, self.deadline, self.clock, self.poll_slice = rfd, deadline, clock, poll_slice

    def read(self, n: int) -> bytes:
        """Up to ``n`` bytes; ``b""`` at EOF. Past the deadline: ``staging-raced``."""
        while True:
            left = self.deadline - self.clock()
            if left <= 0:
                raise Refused("staging-raced", "deadline")
            r, _, _ = select.select([self.rfd], [], [], min(left, self.poll_slice))
            if r:
                return os.read(self.rfd, n)

    def exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            got = self.read(n - len(buf))
            if not got:
                break
            buf += got
        return bytes(buf)


def _check_digest(digest: str | None) -> None:
    if digest is not None and (type(digest) is not str or not _DIGEST_RE.fullmatch(digest)):
        raise Refused("bad-digest", "a digest is sha256: + 64 lowercase hex")


def stage_copy(staging: int | str | os.PathLike[str], name: str, cap: int, header_check: bool,
               digest: str | None, *, dest_dir: int | str | os.PathLike[str], dest_name: str,
               staging_uid: int, staging_gid: int, dest_gid: int, mode: int = 0o640,
               digest_refusal: str = "input-bundle-mismatch", cap_refusal: str = "bundle-cap",
               validate: Callable[[bytes], object] | None = None, deadline_s: float = STAGE_DEADLINE_S,
               run_as: RunAs = fork_run_as, chown: Callable[[int, int, int], None] = os.fchown,
               clock: Callable[[], float] = time.monotonic, poll_slice: float = POLL_SLICE_S,
               fsync: Callable[[int], None] = os.fsync) -> StageResult:
    """CONTRACT §5.4's ONE primitive. Copies ``staging/name`` into a fresh ``dest_dir/dest_name`` (root :
    ``dest_gid``, ``mode``: 0640 for the bundle, §2.1). ``header_check``: the first 16 bytes must be a git bundle header (``bundle-header``).
    ``digest``: ``sha256:<hex>`` the streamed bytes must hash to (``digest_refusal``). ``validate``: called with the
    whole body before the fsync (the driver_input parse); it raises :class:`Refused` to refuse. Every refusal after
    the destination is created unlinks it. Returns the size and digest of what was written."""
    for n in (name, dest_name):
        if not _NAME_RE.fullmatch(n) or n in (".", ".."):
            raise Refused("bad-token", f"name {n[:64]!r}")
    if type(cap) is not int or cap < 0:
        raise Refused("bad-config", "cap")
    for n, v in (("staging_uid", staging_uid), ("staging_gid", staging_gid)):
        if type(v) is not int or v <= 0:   # the lease peer: the operator or _levain_dispatch, never root
            raise Refused("staging-uid-root" if v == 0 else "bad-config", n)
    _check_digest(digest)
    deadline = clock() + deadline_s
    ddfd = dest_dir if isinstance(dest_dir, int) else os.open(os.fspath(dest_dir), _O_DIR)
    rfd, wfd = os.pipe()
    out = None
    created = False
    proc = None
    try:
        proc = run_as(staging_uid, staging_gid,
                      lambda w: reader(staging, name, cap, cap_refusal, w), wfd)
        wfd = -1   # run_as owns it now
        pipe = _Pipe(rfd, deadline, clock, poll_slice)
        tag = pipe.exactly(1)
        if tag == b"R":
            ln = pipe.exactly(1)
            got = pipe.exactly(ln[0]) if ln else b""
            refusal = got.decode("ascii", "replace")
            raise Refused(refusal if refusal in _READER_REFUSALS else "staging-reader-failed", name)
        if tag != b"S":
            raise Refused("staging-reader-failed", name)
        head = pipe.exactly(8)
        if len(head) != 8:
            raise Refused("staging-reader-failed", name)
        accepted = struct.unpack(">Q", head)[0]
        if accepted > cap:     # the reader checked; root does not trust that it did
            raise Refused(cap_refusal, f"{accepted} > {cap}")
        try:
            out = os.open(dest_name, _O_DEST, mode, dir_fd=ddfd)
        except FileExistsError:   # never written over, never unlinked: it is not this call's file
            raise Refused("staging-dest-exists", dest_name) from None
        created = True
        chown(out, 0, dest_gid)
        os.fchmod(out, mode)
        h = hashlib.sha256()
        keep = bytearray() if validate is not None else None
        left = accepted
        if header_check:
            hdr = pipe.exactly(HEADER_LEN)
            if len(hdr) == HEADER_LEN and hdr not in BUNDLE_HEADERS:
                raise Refused("bundle-header", name)
            if len(hdr) < HEADER_LEN:
                # A file shorter than the header at fstat is not a bundle; one that got shorter since raced.
                raise Refused("bundle-header" if accepted < HEADER_LEN else "staging-raced", name)
            write_all(out, hdr)
            h.update(hdr)
            left -= HEADER_LEN
        while left:
            chunk = pipe.read(min(CHUNK, left))
            if not chunk:
                raise Refused("staging-raced", "early EOF")
            write_all(out, chunk)
            h.update(chunk)
            if keep is not None:
                keep += chunk
            left -= len(chunk)
        trailer = pipe.exactly(1)
        if trailer != b"E":
            raise Refused("staging-raced", "extra byte" if trailer == b"X" else "early EOF")
        got_digest = "sha256:" + h.hexdigest()
        if digest is not None and got_digest != digest:
            raise Refused(digest_refusal, name)
        if validate is not None:
            validate(bytes(keep or b""))
        fsync(out)
        os.close(out)
        out = None
        fsync(ddfd)
        return StageResult(accepted, got_digest)
    except BaseException:
        if proc is not None:
            proc.kill()
        if out is not None:
            os.close(out)
            out = None
        if created:
            try:
                os.unlink(dest_name, dir_fd=ddfd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if proc is not None:
            os.close(rfd)          # a reader blocked writing gets EPIPE and ends
            rfd = -1
            proc.wait()
        if rfd >= 0:
            os.close(rfd)
        if wfd >= 0:
            os.close(wfd)
        if not isinstance(dest_dir, int):
            os.close(ddfd)


def stage_bundle(staging: int | str | os.PathLike[str], dest_dir: int | str | os.PathLike[str], *,
                 staging_uid: int, staging_gid: int, dest_gid: int, input_bundle_digest: str | None = None,
                 **seams) -> StageResult:
    """``L/staging/<session>/repo.bundle`` -> ``<S>/in/repo.bundle``: V19, the git header, and (verifier)
    ``input_bundle_digest`` (§5.6, ``input-bundle-mismatch``)."""
    return stage_copy(staging, BUNDLE_NAME, BUNDLE_CAP, True, input_bundle_digest, dest_dir=dest_dir,
                      dest_name=BUNDLE_NAME, staging_uid=staging_uid, staging_gid=staging_gid, dest_gid=dest_gid,
                      digest_refusal="input-bundle-mismatch", cap_refusal="bundle-cap", **seams)


def stage_driver_input(staging: int | str | os.PathLike[str], dest_dir: int | str | os.PathLike[str], *,
                       staging_uid: int, staging_gid: int, dest_gid: int, driver_input_digest: str,
                       **seams) -> StageResult:
    """``L/staging/<session>/driver_input.json`` -> ``<S>/driver/input.json``: V25, no header,
    ``driver_input_digest`` and canonical JSON nested <= 64 (§5.4, §5.6). Every refusal of the CONTENT is
    ``driver-input-mismatch``. DECISION: so is one over V25 (§5.6's terminal refusal names hold no cap name).
    Root : ``dest_gid`` (``_levain_v<n>``) 0440 (§5.6)."""
    if driver_input_digest is None:
        raise Refused("driver-input-mismatch", "no digest")
    return stage_copy(staging, DRIVER_INPUT_NAME, DRIVER_INPUT_CAP, False, driver_input_digest,
                      dest_dir=dest_dir, dest_name="input.json", staging_uid=staging_uid,
                      staging_gid=staging_gid, dest_gid=dest_gid, mode=0o440, digest_refusal="driver-input-mismatch",
                      cap_refusal="driver-input-mismatch", validate=parse_driver_input, **seams)


# --- the supervisor's side: the bundle (§5.4 1) ----------------------------------------------------------------------

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
                 "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")


def _git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k in _GIT_ENV_DROP or k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_"):
            del env[k]
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def make_bundle(repo: str | os.PathLike[str], branch: str, out: str | os.PathLike[str], *, git: str = "git",
                run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                tmp_parent: str | os.PathLike[str] | None = None) -> Path:
    """The supervisor's bundle: a clone of ``repo``'s ``branch`` made with plan §2c 5's flags
    (``-c core.hooksPath=/dev/null clone --no-local --no-hardlinks --template= --single-branch``), so tracked
    files only and no shared inode; its ``origin`` remote removed (remotes + credentials stripped); then
    ``git -c core.hooksPath=/dev/null bundle create <out> HEAD <branch>`` (HEAD included, so the clone checks
    out). Runs as the caller (the operator or ``_levain_dispatch``); ``out`` is ``L/staging/<session>/repo.bundle``.
    Refused ``bad-branch`` / ``bundle-failed`` by name; the scratch clone is always removed."""
    if type(branch) is not str or not branch or branch.startswith("-") or "\x00" in branch:
        raise Refused("bad-branch", branch[:64] if isinstance(branch, str) else "")
    env = _git_env()
    gitc = [git, "-c", "core.hooksPath=/dev/null"]
    chk = run([git, "check-ref-format", "--branch", branch], env=env, capture_output=True, text=True)
    if chk.returncode != 0:
        raise Refused("bad-branch", branch[:64])
    tmp = tempfile.mkdtemp(prefix="levain-bundle-", dir=None if tmp_parent is None else os.fspath(tmp_parent))
    try:
        clone = os.path.join(tmp, "clone")
        # DECISION: --no-checkout added to §2c 5's flags: bundling needs no work tree, so no checkout runs a
        # filter, an fsmonitor or a hook of the operator's own config on the scratch clone.
        steps = [
            ("clone", gitc + ["clone", "--no-local", "--no-hardlinks", "--template=", "--single-branch",
                              "--no-checkout", "--branch", branch, "--", os.fspath(repo), clone]),
            ("remote-remove", gitc + ["-C", clone, "remote", "remove", "origin"]),
            ("bundle-create", gitc + ["-C", clone, "bundle", "create", os.fspath(out), "HEAD", branch]),
        ]
        for label, argv in steps:
            p = run(argv, env=env, capture_output=True, text=True)
            if p.returncode != 0:
                raise Refused("bundle-failed", f"{label}: {(p.stderr or '').strip()[:200]}")
        return Path(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = [
    "BUNDLE_CAP", "STAGE_DEADLINE_S", "DRIVER_INPUT_CAP", "BUNDLE_HEADERS", "StageResult", "Reader", "RunAs",
    "drop_and_assert", "fork_run_as", "thread_run_as", "reader", "stage_copy", "stage_bundle",
    "stage_driver_input", "make_bundle",
]
