"""The take: a verified session export into the operator's OWN repository (S1 session 5 DESIGN §6a, CONTRACT §10.4).

The operator CLI runs it after ``quarantine_verify`` passed in the confined verifier and the CLI made its private
copies of the bundle and of ``verify/q.idx`` (§6 setup, S4). It MIGRATES and does not reparse pack bytes (A2): no
``index-pack`` runs in his repository. It trusts nothing the verifier says about bytes (S3):

0. :func:`derive` re-derives, in Python, from the CLI's private bundle copy: the refs, prerequisites and pack offset by
   :mod:`bundlefmt`, the pack's sha256 and, in the SAME pass, the format's hash over the pack minus its trailer, and
   the pack header's object count. The verifier's claims must equal them (``verifier-claim-mismatch``).
   :func:`check_idx` then validates the private idx copy STRUCTURALLY, inflating nothing
   (``take-idx-invalid:<check>``, ``<check>`` from :data:`IDX_CHECKS`, E14's closed enum).
1. :func:`migrate` writes the ``.pack`` from the private copy, hashed while it writes (the running sha256 must equal
   step 0's, else ``take-bytes-changed``), then the ``.idx`` LAST (a pack is invisible to git until its idx exists),
   each to an ``O_CREAT|O_EXCL`` 0444 temp in his ``objects/pack/``, fsynced, then linked to ``pack-<trailer>.<ext>``
   (never replacing a file there) and the dir fsynced. Both final names are looked at before anything is written: one
   already holding equal bytes is skipped (the dir is fsynced again before :func:`migrate` returns, both skipped
   included, so a final another take linked is durable before this take's ref); different bytes = ``take-pack-collision``; an OSError =
   ``take-failed:<errno name>``; a link that fails otherwise (hard links refused included) is ``take-failed:<errno
   name>`` too, never a rename. On ANY failure, an interrupt included, the temps are removed. A final name already
   linked stays: a pack whose idx did not land is invisible to git, a retry finds it present with equal bytes, and
   another take of the same export may already have adopted it.
2. :func:`create_ref`: ``update-ref --no-deref --create-reflog refs/heads/levain/<s>[-wip] <oid> <zero oid>``,
   create-only (``take-ref-exists``, ``take-ref-conflict``, ``take-failed:<rc>``).

Before step 1 writes anything, :func:`check_prerequisites` holds every bundle prerequisite to his repository
(``base-missing``).

His git runs here on trusted input only: ``cat-file -e <oid>`` once per prerequisite, ``rev-parse --git-path
objects`` and ``update-ref``, each through :func:`_git` with :func:`git_env`, ``-c core.hooksPath=/dev/null`` and
``--git-dir=<repo>/.git --work-tree=<repo>`` (:func:`repo_args`: never a discovered repository); update-ref also gets
``-c hook.reference-transaction.enabled=false`` (it runs the ``reference-transaction`` hook otherwise). A spawn that
cannot start or outlives its timeout is ``take-failed:<errno name>`` / ``take-failed:timeout``, as is an OSError reading
the private copies.

Known edge (§6a, HEAD_r5 Q-C, S3's words): "Shadowing requires the confined verifier to produce a semantically false
idx; the take does not independently validate oid-to-offset mappings."
Known edges (HEAD_rulings 1023-6 "adopt S3 L3 r1"): a retry after a ref that landed reports ``take-ref-exists``; a
symlinked ``objects/`` or ``objects/pack/`` fails closed (``take-failed:ELOOP``, or ``ENOTDIR``, the errno macOS
gives for ``objects/``: ``1023-6/adopt/s3f1_run.txt`` row M7).
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import struct
import subprocess
import sys
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from levain import unshowable
from levain.slot import Refused, adopt_verify, bundlefmt
from levain.slot.grammar import SESSION_RE

#: E14's closed ``<check>`` enum.
IDX_CHECKS = ("magic", "version", "fanout", "count", "crc-len", "order", "large-table", "offset-range",
              "first-offset", "offset-dup", "pack-checksum", "pack-hash", "trailer")
IDX_MAGIC = b"\377tOc"
HASH = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}
#: The five byte facts the verifier claims and the take re-derives (§6a step 0).
CLAIMS = ("refs", "prerequisites", "pack_offset", "pack_sha256", "object_count")
_FANOUT_END = 8 + 256 * 4
_CHUNK = 1 << 20
_GIT_TIMEOUT = 120
#: The ref each take kind creates (``-wip``, not ``/wip``, so his two adopted refs never D/F each other).
REF_SUFFIX = {"head": "", "wip": "-wip"}

Log = Optional[Callable[[str], None]]


def _note(log: Log, line: str) -> None:
    if log is not None:
        log(line)


def _idx_bad(check: str, detail: str) -> Refused:
    if check not in IDX_CHECKS:
        raise ValueError(f"idx check {check!r} is not in E14's enum")
    return Refused(f"take-idx-invalid:{check}", detail)


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno or 0, str(e.errno))


def _failed(e: OSError, what: str) -> Refused:
    return Refused(f"take-failed:{_errno_name(e)}", f"{what}: {e.strerror}")


def _check_session(session: str) -> None:
    # DESIGN §4.1 (S16-2): "a session id is `^s-[0-9a-f]{16}$` (the S1 contract's argv grammar, CONTRACT.md §5 Helper
    # grammars: "Session `^s-[0-9a-f]{16}$`"; `levain/slot/grammar.py` `SESSION_RE = s-[0-9a-f]{16}`".
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise ValueError(f"session id {session!r}")


@dataclass(frozen=True)
class Derived:
    """Step 0's facts, every one from the CLI's private bundle copy."""

    object_format: str
    refs: Dict[str, str]                # head / wip -> oid
    prerequisites: Tuple[str, ...]
    pack_offset: int
    pack_size: int
    pack_sha256: str
    object_count: int
    trailer: bytes                      # the pack's last hash-length bytes
    pack_hash_ok: bool                  # the format's hash over the pack minus its trailer == ``trailer``


def derive(bundle: Path, *, session: str, object_format: str) -> Derived:
    """Step 0's derivations from the private bundle copy, in ONE read of its pack region."""
    _check_session(session)
    if object_format not in HASH:
        raise ValueError(f"object format {object_format!r}")
    try:
        return _derive(bundle, session, object_format)
    except OSError as e:
        raise _failed(e, f"the private bundle copy {bundle.name}") from None


def _derive(bundle: Path, session: str, object_format: str) -> Derived:
    hl = HASH[object_format]().digest_size
    fd = os.open(bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        h = adopt_verify.read_header_at(fd)
        if h.object_format != object_format:
            raise Refused(bundlefmt.MISMATCH, f"a {h.object_format} bundle for a {object_format} repository")
        size = os.fstat(fd).st_size
        pack_size = size - h.size
        count = adopt_verify.pack_count_at(fd, h.size, pack_size)
        if pack_size < 12 + hl:
            raise Refused(bundlefmt.MISMATCH, f"a pack region of {pack_size} bytes")
        sha = hashlib.sha256()
        fmt = HASH[object_format]()
        tail = b""
        pos, left = h.size, pack_size
        while left > 0:
            b = os.pread(fd, min(_CHUNK, left), pos)
            if not b:
                raise Refused("take-bytes-changed", "the private bundle copy shrank while step 0 read it")
            sha.update(b)
            buf = tail + b
            fmt.update(buf[:-hl])
            tail = buf[-hl:]
            pos += len(b)
            left -= len(b)
    finally:
        os.close(fd)
    prefix = f"refs/levain/{session}/".encode()
    refs: Dict[str, str] = {}
    for oid, name in h.refs:
        key = name[len(prefix):].decode("ascii", "replace") if name.startswith(prefix) else name.decode(
            "ascii", "replace")
        refs[key] = oid
    return Derived(object_format, refs, h.prerequisites, h.size, pack_size, sha.hexdigest(), count, tail,
                   fmt.digest() == tail)


def _shown(v: object) -> str:
    return unshowable.mark(str(v), allow="")[:200]


def check_claims(claims: Mapping[str, object], d: Derived) -> None:
    """The verifier's five byte claims equal step 0's derivations, or ``verifier-claim-mismatch``. Both values reach
    his terminal in the detail (the verifier's, and the derived refs, which are the hostile bundle's names), so each
    passes :func:`levain.unshowable.mark` (the one escaper) before the 200-character cut."""
    derived = {"refs": dict(d.refs), "prerequisites": list(d.prerequisites), "pack_offset": d.pack_offset,
               "pack_sha256": d.pack_sha256, "object_count": d.object_count}
    for k in CLAIMS:
        got = claims.get(k)
        if k == "prerequisites" and isinstance(got, tuple):
            got = list(got)
        if type(got) is not type(derived[k]) or got != derived[k]:
            raise Refused("verifier-claim-mismatch", f"{k}: the verifier claims {_shown(got)}, the private copy "
                                                     f"gives {_shown(derived[k])}")


def _read_exact(fd: int, n: int, pos: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.pread(fd, min(_CHUNK, n - len(out)), pos + len(out))
        if not b:
            raise Refused("take-bytes-changed", "the private idx copy shrank while step 0 read it")
        out += b
    return bytes(out)


def check_idx(idx: Path, d: Derived) -> str:
    """E14's structural check of the private idx copy against step 0's derivations, nothing inflated. Returns the
    idx's sha256 (step 1 writes the idx hashed against it). An OSError reading the copy = ``take-failed:<errno name>``.

    ``large-table`` holds the table's length to git's own ceiling, at most ``max(n - 1, 0)`` entries (``packfile.c``
    ``load_idx``'s max_size; measured: git's ``index-pack --index-version=2,0`` idx, m == n, is "wrong index v2 file
    size" to git 2.54.0 and 2.50.1 and its ``2,0x10`` idx, m == n - 1, opens: ``1023-6/adopt/s3f1_large_*.txt``),
    and its indexes, the low 31 bits of the msbit-set 4-byte offsets, to exactly ``0 .. m - 1`` each once:
    the table's length equals the msbit-set count and every index is in range of it (§6a "a large-offset index in
    range of its table") [seat reading, adopt5dii-s3, 10-23, against E14; head KEEP, S3 Q3].

    ``fanout`` holds more than E14's "monotonic": ``fanout[i]`` must equal the number of oids whose first byte is at
    most ``i``, for every ``i``, so each oid lies in the slice git's lookup binary-searches for it (``fanout[b - 1]``
    up to ``fanout[b]``, ``b`` its first byte) [judged by adopt5dii-s3, 10-23, against E14's "fanout (monotonic)": a
    stricter reading of the same token; head KEEP, S3 Q3]."""
    try:
        return _check_idx(idx, d)
    except OSError as e:
        raise _failed(e, f"the private idx copy {idx.name}") from None


def _check_idx(idx: Path, d: Derived) -> str:
    hl = len(d.trailer)
    fmt = HASH[d.object_format]
    fd = os.open(idx, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        length = os.fstat(fd).st_size
        whole = hashlib.sha256()
        body = fmt()                                    # the idx's own hash, over every byte before its trailer

        def read(n: int, pos: int) -> bytes:
            b = _read_exact(fd, n, pos)
            whole.update(b)
            if pos + n <= length - hl:
                body.update(b)
            else:
                body.update(b[:max(0, length - hl - pos)])
            return b

        head = read(min(_FANOUT_END, length), 0) if length >= 8 else _read_exact(fd, length, 0)
        if head[:4] != IDX_MAGIC:
            raise _idx_bad("magic", f"first bytes {head[:4]!r}")
        if len(head) < 8 or struct.unpack(">I", head[4:8])[0] != 2:
            raise _idx_bad("version", f"version {struct.unpack('>I', head[4:8])[0] if len(head) >= 8 else None}")
        if len(head) < _FANOUT_END:
            raise _idx_bad("fanout", f"an idx of {length} bytes holds no whole fanout table")
        fanout = struct.unpack(">256I", head[8:_FANOUT_END])
        for i in range(255):
            if fanout[i] > fanout[i + 1]:
                raise _idx_bad("fanout", f"fanout[{i}] = {fanout[i]} > fanout[{i + 1}] = {fanout[i + 1]}")
        n = fanout[255]
        if n != d.object_count:
            raise _idx_bad("count", f"fanout[255] = {n}, the pack header counts {d.object_count}")
        fixed = _FANOUT_END + n * hl + 4 * n + 4 * n + 2 * hl
        rest = length - fixed
        if rest < 0 or rest % 8:
            raise _idx_bad("crc-len", f"{length} bytes do not hold {n} oids, {n} CRC32s, {n} offsets and the "
                                      "trailers on an 8-byte large-offset table")
        m = rest // 8
        if m > max(n - 1, 0):                           # git's load_idx ceiling, before the table is read
            raise _idx_bad("large-table", f"{m} large-offset entries for {n} objects; git opens at most "
                                          f"{max(n - 1, 0)}")
        # oids: strictly increasing; each in its fanout bucket
        pos = _FANOUT_END
        prev = b""
        per_byte = [0] * 256
        done = 0
        while done < n:
            k = min(n - done, max(1, _CHUNK // hl))
            block = read(k * hl, pos)
            for j in range(k):
                oid = block[j * hl:(j + 1) * hl]
                if oid <= prev:
                    raise _idx_bad("order", f"oid {done + j} {oid.hex()} after {prev.hex()}")
                per_byte[oid[0]] += 1
                prev = oid
            pos += k * hl
            done += k
        run = 0
        for i in range(256):
            run += per_byte[i]
            if run != fanout[i]:
                raise _idx_bad("fanout", f"fanout[{i}] = {fanout[i]}, the oids give {run}")
        read(4 * n, pos)                                # the CRC32 table: its length is crc-len's; nothing inflated
        pos += 4 * n
        small = array("I", read(4 * n, pos))           # network order on disk
        pos += 4 * n
        large = array("Q", read(8 * m, pos))
        pos += 8 * m
        if sys.byteorder == "little":
            small.byteswap()
            large.byteswap()
        msb = [v & 0x7FFFFFFF for v in small if v & 0x80000000]
        if sorted(msb) != list(range(m)):
            raise _idx_bad("large-table", f"{len(msb)} msbit-set offsets do not index the {m} large-offset entries "
                                          "once each")
        limit = d.pack_size - hl
        offsets = array("Q", (large[v & 0x7FFFFFFF] if v & 0x80000000 else v for v in small))
        for i, off in enumerate(offsets):
            if not 12 <= off < limit:
                raise _idx_bad("offset-range", f"entry {i} at {off}, the pack's objects lie in [12, {limit})")
        if n and min(offsets) != 12:
            raise _idx_bad("first-offset", f"the lowest offset is {min(offsets)}, not 12")
        ordered = sorted(offsets)
        for a, b in zip(ordered, ordered[1:]):
            if a == b:
                raise _idx_bad("offset-dup", f"two entries at offset {a}")
        embedded = read(hl, pos)
        pos += hl
        if embedded != d.trailer:
            raise _idx_bad("pack-checksum", f"the idx names pack {embedded.hex()}, the copy's trailer is "
                                            f"{d.trailer.hex()}")
        if not d.pack_hash_ok:
            raise _idx_bad("pack-hash", f"the pack's {d.object_format} is not its trailer {d.trailer.hex()}")
        trailer = read(hl, pos)
        if trailer != body.digest():
            raise _idx_bad("trailer", f"the idx trailer {trailer.hex()} is not the hash of its bytes")
        return whole.hexdigest()
    finally:
        os.close(fd)


# --- his git ----------------------------------------------------------------------------------------------------


def git_env() -> Dict[str, str]:
    """§6a's env for his git: levain's child environment with every ``GIT_*`` removed, then
    ``GIT_CONFIG_GLOBAL=/dev/null``, ``GIT_CONFIG_NOSYSTEM=1``, ``GIT_NO_LAZY_FETCH=1``, ``GIT_NO_REPLACE_OBJECTS=1``
    (his git reads literal objects, never one a ``refs/replace/`` ref stands in for), ``LC_ALL=C``."""
    from levain.launch import child_env

    env = {k: v for k, v in child_env().items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C"})
    return env


def repo_args(repo: Path) -> Tuple[str, str]:
    """``--git-dir=<repo>/.git --work-tree=<repo>``: his git acts on the repository he named and never discovers
    one (an empty or invalid ``<repo>/.git`` makes a discovering git walk up to a PARENT repository and act there;
    with these it refuses instead: L3 s4a r1 complement MED 2)."""
    return (f"--git-dir={os.path.join(str(repo), '.git')}", f"--work-tree={repo}")


def _git(git: str, repo: Path, *args: str, config: Tuple[str, ...] = (),
         log: Log = None) -> subprocess.CompletedProcess:
    """His git, run once, ``-c core.hooksPath=/dev/null`` then a ``-c`` per ``config`` entry, then
    :func:`repo_args`. A spawn that outlives ``_GIT_TIMEOUT`` = ``take-failed:timeout``; one that cannot start (no
    such git, his repo dir gone) = ``take-failed:<errno name>``."""
    argv = [git, "-c", "core.hooksPath=/dev/null"]
    for c in config:
        argv += ["-c", c]
    argv += [*repo_args(repo), *args]
    _note(log, "spawn " + " ".join(argv[1:]))
    what = "git " + args[0]
    try:
        return subprocess.run(argv, cwd=str(repo), env=git_env(), capture_output=True, timeout=_GIT_TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Refused("take-failed:timeout", f"{what} ran past {_GIT_TIMEOUT} s") from None
    except OSError as e:
        raise _failed(e, what) from None


def objects_dir(git: str, repo: Path, log: Log = None) -> Path:
    """His ``objects`` dir by ``rev-parse --git-path objects`` (relative answers are relative to ``repo``)."""
    r = _git(git, repo, "rev-parse", "--git-path", "objects", log=log)
    out = r.stdout.decode("utf-8", "surrogateescape").rstrip("\n")
    if r.returncode != 0 or not out or "\n" in out:
        raise Refused(f"take-failed:{r.returncode}", "git rev-parse --git-path objects: "
                      + r.stderr.decode("utf-8", "replace").strip()[-300:])
    p = Path(out)
    return p if p.is_absolute() else repo / p


# --- step 1 -----------------------------------------------------------------------------------------------------


@dataclass
class Migrated:
    pack: str                                       # the final names, in ``objects/pack/``
    idx: str
    written: List[str] = field(default_factory=list)    # the final names THIS take created, in order
    skipped: List[str] = field(default_factory=list)    # already present with equal bytes


def _full_fsync(fd: int) -> None:
    """An fsync that reaches the medium. Darwin: ``F_FULLFSYNC`` (a plain fsync there leaves the drive's cache
    unflushed), and ``os.fsync`` only when that fcntl answers ENOTSUP or EINVAL (a filesystem without it; the fallback
    set is the S3 fix brief's, after git's ``git_fsync``); any other errno is raised. Elsewhere ``os.fsync``. A file
    and a directory alike."""
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, errno.EINVAL):
                raise
    os.fsync(fd)


def _sha256_at(dirfd: int, name: str) -> Optional[str]:
    """The sha256 of ``name`` in ``dirfd`` when it is a regular file opened without following a link; None when the
    name does not exist; ``""`` (equal to no digest: other bytes) when it is a symlink or not a regular file. Any
    other OSError = ``take-failed:<errno name>``."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dirfd)
    except FileNotFoundError:
        return None
    except OSError as e:                            # a symlink (ELOOP under O_NOFOLLOW), a socket: other bytes
        try:
            mode = os.stat(name, dir_fd=dirfd, follow_symlinks=False).st_mode
        except OSError:
            raise _failed(e, f"objects/pack/{name}") from None
        if not stat.S_ISREG(mode):
            return ""
        raise _failed(e, f"objects/pack/{name}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        h = hashlib.sha256()
        while True:
            b = os.read(fd, _CHUNK)
            if not b:
                return h.hexdigest()
            h.update(b)
    finally:
        os.close(fd)


def _present(dirfd: int, name: str, want: str, log: Log) -> bool:
    """True when ``name`` already holds bytes whose sha256 is ``want`` (skipped); False when absent;
    ``take-pack-collision`` when it holds anything else."""
    got = _sha256_at(dirfd, name)
    if got is None:
        return False
    if got != want:
        raise Refused("take-pack-collision", f"objects/pack/{name} exists with other bytes")
    _note(log, f"present {name}")
    return True


def _copy_into(dirfd: int, tmp: str, src: Path, offset: int, size: int, want: str) -> None:
    """``src[offset:offset+size]`` into a new 0444 ``tmp`` in ``dirfd``, hashed while written; fsynced."""
    from levain import slot

    write = slot.write_all                          # looked up per call: a test seam replaces it (ENOSPC)
    out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o444, dir_fd=dirfd)
    try:
        os.fchmod(out, 0o444)
        h = hashlib.sha256()
        inp = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            pos, left = offset, size
            while left > 0:
                b = os.pread(inp, min(_CHUNK, left), pos)
                if not b:
                    break
                h.update(b)
                write(out, b)
                pos += len(b)
                left -= len(b)
            extra = os.pread(inp, 1, offset + size)     # both regions end at the copy's end: a grown copy changed
        finally:
            os.close(inp)
        if left or extra or h.hexdigest() != want:
            raise Refused("take-bytes-changed", f"{src.name}: the bytes written are not the bytes step 0 checked")
        _full_fsync(out)
    finally:
        os.close(out)


def _finalize(dirfd: int, tmp: str, final: str, want: str, m: Migrated, log: Log) -> None:
    """``tmp`` to ``final`` without replacing a file there: link (never following a link), then unlink the temp
    (git's own ``finalize_object_file`` order). ``final`` goes into ``m.written`` when the link lands; into
    ``m.skipped`` when ``final`` appeared meanwhile with equal bytes (other bytes = ``take-pack-collision``). Any other
    link failure, a filesystem that refuses hard links included, is raised (``take-failed:<errno name>``): there is no
    rename, which would replace a file that appeared at ``final``."""
    try:
        os.link(tmp, final, src_dir_fd=dirfd, dst_dir_fd=dirfd, follow_symlinks=False)
    except FileExistsError:
        if not _present(dirfd, final, want, log):   # other bytes: take-pack-collision
            raise                                   # gone again between the link and the look: take-failed:EEXIST
        m.skipped.append(final)
        os.unlink(tmp, dir_fd=dirfd)
        return
    m.written.append(final)
    _note(log, f"finalise {tmp} -> {final}")
    os.unlink(tmp, dir_fd=dirfd)


def migrate(git: str, repo: Path, bundle: Path, idx: Path, d: Derived, idx_sha256: str, *,
            log: Log = None) -> Migrated:
    """Step 1: the private copies' pack then idx into his ``objects/pack/``; ``objects/pack`` is fsynced before every
    return, both finals skipped included. Both final names are looked at before
    anything is written (a stale idx at the idx's name would otherwise go live the moment the pack is linked). Any
    failure, an interrupt included, removes this take's temps and is re-raised; an OSError is raised as
    ``take-failed:<errno name>``. A final name already linked stays: a pack whose idx did not land is invisible to git
    and a retry finds it present with equal bytes, and another take may already have adopted it."""
    stem = "pack-" + d.trailer.hex()
    m = Migrated(stem + ".pack", stem + ".idx")
    objects = objects_dir(git, repo, log)
    tmps: List[str] = []
    pack_fd = -1
    try:
        try:
            obj_fd = os.open(objects, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                pack_fd = os.open("pack", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=obj_fd)
            finally:
                os.close(obj_fd)
            idx_size = os.stat(idx, follow_symlinks=False).st_size
            todo = ((m.pack, bundle, d.pack_offset, d.pack_size, d.pack_sha256),
                    (m.idx, idx, 0, idx_size, idx_sha256))
            present = [_present(pack_fd, final, want, log) for final, _src, _off, _size, want in todo]
            for (final, src, off, size, want), there in zip(todo, present):
                if there:
                    m.skipped.append(final)
                    continue
                tmp = f"tmp_{final[-4:]}_levain_{secrets.token_hex(6)}"
                tmps.append(tmp)
                _copy_into(pack_fd, tmp, src, off, size, want)
                _finalize(pack_fd, tmp, final, want, m, log)
                tmps.remove(tmp)
                _full_fsync(pack_fd)
            _full_fsync(pack_fd)    # every return, both skipped included: a final another take linked may be unsynced
            return m
        except OSError as e:
            raise _failed(e, "objects/pack") from None
    except BaseException:
        for t in tmps:
            try:
                os.unlink(t, dir_fd=pack_fd)
            except OSError:
                pass
        raise
    finally:
        if pack_fd >= 0:
            os.close(pack_fd)


# --- step 2 -----------------------------------------------------------------------------------------------------


def check_prerequisites(git: str, repo: Path, d: Derived, *, log: Log = None) -> None:
    """Every prerequisite of the bundle is an object in his repository, or ``base-missing`` (a ref onto a delta whose
    base he lacks is a disconnected branch). One ``cat-file -e <oid>`` each: rc 1 = absent; any other non-zero rc =
    ``take-failed:<rc>``."""
    for oid in d.prerequisites:
        r = _git(git, repo, "cat-file", "-e", oid, log=log)
        if r.returncode == 1:
            raise Refused("base-missing", f"the prerequisite {oid} is not in his repository")
        if r.returncode != 0:
            raise Refused(f"take-failed:{r.returncode}", "git cat-file -e: "
                          + r.stderr.decode("utf-8", "replace").strip()[-300:])


def ref_name(session: str, which: str) -> str:
    _check_session(session)
    return f"refs/heads/levain/{session}{REF_SUFFIX[which]}"


def create_ref(git: str, repo: Path, session: str, which: str, oid: str, object_format: str, *,
               log: Log = None) -> str:
    """Step 2, create-only. Returns the ref created."""
    ref = ref_name(session, which)
    zero = "0" * bundlefmt.OID_HEX[object_format]
    r = _git(git, repo, "update-ref", "--no-deref", "--create-reflog", ref, oid, zero,
             config=("hook.reference-transaction.enabled=false",), log=log)
    if r.returncode == 0:
        return ref
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode == 128 and "reference already exists" in err:
        raise Refused("take-ref-exists", ref)
    if r.returncode == 128 and "exists; cannot create" in err:
        raise Refused("take-ref-conflict", err.strip()[-300:])
    raise Refused(f"take-failed:{r.returncode}", err.strip()[-300:])


# --- the whole take ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Taken:
    ref: str
    oid: str
    migrated: Migrated


def take(*, git: str, repo: Path, bundle: Path, idx: Path, claims: Mapping[str, object], session: str,
         object_format: str, which: str, log: Log = None) -> Taken:
    """Steps 0-2 over the CLI's private copies (``bundle``, ``idx``) and the verifier's ``claims``
    (``verified.json``, read by the CLI through ``canon``). ``which``: ``head`` or ``wip``, a ref the bundle
    carries (the CLI offers only those)."""
    if which not in REF_SUFFIX:
        raise ValueError(f"take {which!r}")
    _check_session(session)
    d = derive(bundle, session=session, object_format=object_format)
    check_claims(claims, d)
    if which not in d.refs:
        raise ValueError(f"the bundle carries no {which} ref")
    idx_sha = check_idx(idx, d)
    _note(log, "step0 ok")
    check_prerequisites(git, repo, d, log=log)      # before anything is written in his repo
    m = migrate(git, repo, bundle, idx, d, idx_sha, log=log)
    ref = create_ref(git, repo, session, which, d.refs[which], object_format, log=log)
    return Taken(ref, d.refs[which], m)
