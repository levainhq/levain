"""The adopt importer, the program that runs as ``_levain_export`` inside the importer wall (S1 session 5 DESIGN §5).

It holds no path of the retained tree: everything it knows arrives as ``levain-walk/1`` frames on fd 0 from root's
walker, and every byte of them is hostile data. It writes only inside ``.work/`` (its cwd), and answers ONE result
line on fd 4.

Steps built here (5b1): 0, spool every frame into one append-only file and validate the terminal frame before any git
command runs; 1, the base objects from ``in/repo.bundle``; 2, the session's own objects, loose ones name-verified and
packs re-indexed by ``index-pack``, every other ``.git/`` path ignored and counted. Step 3 (5b2): the session's refs,
parsed as data, under ``refs/levain/<s>/``. Step 4: the worktree's blobs written by the importer itself, one commit of
them as ``refs/levain/<s>/wip``. Step 5: ``fsck --strict``, then the explicit ``no-ref`` check. Step 6: the result
(``delta``, ``unrelated`` or ``adopt-nothing-new``), the pack with the special blobs, ``out/bundle`` and
``out/report.json``.

Also here: the root-written ``repo.git`` config for both object formats (:func:`render_config`), the skeleton root
makes at seal-in (:func:`make_skeleton`), and the environment and argv every importer git runs under.

The result line (:func:`result_line`) carries ``enospc`` SOURCE-SIDE: computed only on the fatal path, at the point
the importer decides to exit non-zero, from the importer's OWN readings (an errno of its own failing write, one probe
write, one ``statvfs``); exit 0 therefore always carries ``enospc: false``. A git child's stderr never sets it: that
text can carry bytes of the hostile objects, so a child that failed on a full disk is the generic failure unless the
importer's own probe sees the disk full too. Every exit path, a usage error included, answers exactly one result line.
"""
from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import io
import itertools
import os
import re
import select
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence

from levain.contracts import canon
from levain.slot import Refused, bundlefmt, fsck_ids, special_paths, walker, walkframe, write_all
from levain.slot.grammar import SESSION_RE

#: Hex length and hash constructor of each object format.
FORMATS = {"sha1": (40, hashlib.sha1), "sha256": (64, hashlib.sha256)}
#: ``repo.git/HEAD`` as root writes it: an unborn branch. ``git fsck`` refuses a symbolic HEAD outside ``refs/heads/``,
#: so the name is a branch name; the refs step refuses any imported ref of exactly that name
#: (``ref-reserved:refs/heads/levain-none``), so that HEAD never resolves to slot-supplied content.
HEAD_TEXT = b"ref: refs/heads/levain-none\n"
#: The most bytes the result line may take, newline included.
RESULT_CAP = 64 << 10
#: How long the importer waits, after the terminal frame, for the frame pipe's end of stream. The contract is that
#: root closes its write end right after the terminal frame; this bounds a root that does not: past it the run
#: refuses ``frame-bad:trailing`` rather than hang while root waits on fd 4.
TRAILING_WAIT = 5.0
#: The base bundle's frame path.
BASE_BUNDLE = b"in/repo.bundle"
#: Where the session's git directory sits in frame paths.
GIT_PREFIX = walkframe.PRUNE_SCOPE_PREFIX[0] + b"/.git/"

_CHUNK = 1 << 20
_CAPTURE = 64 << 10
_DETAIL_MAX = 1000
_ENOSPC_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
_LOOSE_TYPES = frozenset({b"blob", b"tree", b"commit", b"tag"})
_LOOSE_HEADER_MAX = 32
_PACK = re.compile(rb"objects/pack/[^/]+\.pack")


def render_config(object_format: str) -> bytes:
    """``repo.git/config``, root-written (§4.2): the format's repository version, hooks and fsmonitor off, HFS/NTFS
    protection, no symlinks, no automatic gc or maintenance, two pack threads, no protocol, and every CONFIG fsck id
    promoted to ``error``. No ``include``, ``filter``, ``diff``, ``transfer`` or ``fetch`` key: nothing here fetches."""
    if object_format not in FORMATS:
        raise ValueError(f"object format {object_format!r}")
    sha256 = object_format == "sha256"
    lines = ["[core]", f"\trepositoryformatversion = {1 if sha256 else 0}", "\tbare = true", "\thooksPath = /dev/null",
             "\tfsmonitor = false", "\tprotectHFS = true", "\tprotectNTFS = true", "\tsymlinks = false"]
    if sha256:
        lines += ["[extensions]", "\tobjectFormat = sha256"]
    lines += ["[gc]", "\tauto = 0", "[maintenance]", "\tauto = false", "[pack]", "\tthreads = 2",
              "[protocol]", "\tallow = never", "[uploadpack]", "\tallowAnySHA1InWant = false", "[fsck]"]
    lines += [f"\t{i} = error" for i in fsck_ids.CONFIG_IDS]
    return ("\n".join(lines) + "\n").encode("ascii")


def _mkdir_exact(path: Path, mode: int) -> None:
    """``mkdir`` then ``fchmod`` to ``mode``: the final mode never depends on the caller's umask."""
    path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _create_exact(path: Path, mode: int, data: bytes = b"") -> None:
    """A new file (``O_EXCL``, no symlink followed), ``fchmod`` to ``mode`` whatever the umask, then ``data``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, mode)
        write_all(fd, data)
    finally:
        os.close(fd)


def make_skeleton(work: Path, object_format: str) -> None:
    """The ``.work/`` skeleton root makes at seal-in, owners aside (root chowns): ``repo.git/`` with ``config`` and
    ``HEAD`` (the importer cannot replace them, nor add ``hooks/`` or ``info/``), its ``objects/`` and ``refs/``, and
    ``spool/``, ``out/``, ``log/`` with the two log files. Every mode is set explicitly, never left to the umask: a
    root umask of ``0077`` would otherwise leave root's ``repo.git`` unreadable to the importer."""
    repo = work / "repo.git"
    _mkdir_exact(repo, 0o755)
    _create_exact(repo / "config", 0o644, render_config(object_format))
    _create_exact(repo / "HEAD", 0o644, HEAD_TEXT)
    for d in (repo / "objects", repo / "refs", work / "spool", work / "out", work / "log"):
        _mkdir_exact(d, 0o700)
    for name in ("stdout", "stderr"):
        _create_exact(work / "log" / name, 0o600)


def git_env(work: Path) -> dict[str, str]:
    """The whole environment of every importer git: ``GIT_DIR`` pinned (never discovered), no system or global
    config, no attributes file, no replace objects, no prompt, no home, the spool as ``TMPDIR``, the C locale."""
    return {"GIT_DIR": str(work / "repo.git"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/nonexistent", "TMPDIR": str(work / "spool"), "PATH": "/usr/bin:/bin", "LC_ALL": "C"}


def git_argv(git: str, work: Path, *args: str) -> list[str]:
    """``git -c safe.directory=<repo.git> <args>``: command-scope config is protected, so git honours it, and
    ``repo.git`` is root's while the importer is not."""
    return [git, "-c", f"safe.directory={work / 'repo.git'}", *args]


class Fatal(Exception):
    """The importer's decision to exit non-zero. ``name`` is an adopt refusal name or ``None`` (a failure with no
    adopt name; root names the run). ``own_enospc``: one of the importer's own writes saw ENOSPC or EDQUOT."""

    def __init__(self, name: str | None, detail: str, *, own_enospc: bool = False) -> None:
        super().__init__(f"{name}: {detail}" if name else detail)
        self.name = name
        self.detail = detail
        self.own_enospc = own_enospc


def _oserror(err: OSError, what: str) -> Fatal:
    return Fatal(None, f"{what}: {errno.errorcode.get(err.errno or 0, err.errno)}",
                 own_enospc=err.errno in _ENOSPC_ERRNOS)


@dataclass(frozen=True)
class Frame:
    path: bytes
    type: str
    mode: int
    size: int
    offset: int     # of the payload, in the spool


class Spool:
    """ONE append-only file holding every frame's payload back to back; the index is in memory. One inode, whatever
    the number of entries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        self.end = 0

    def append(self, data: bytes) -> None:
        try:
            write_all(self.fd, data)
        except OSError as e:
            raise _oserror(e, "spool write") from None
        self.end += len(data)

    def chunks(self, offset: int, size: int) -> Iterator[bytes]:
        left = size
        while left:
            data = os.pread(self.fd, min(left, _CHUNK), offset + size - left)
            if not data:
                raise Fatal(None, "the spool ended inside a payload")
            left -= len(data)
            yield data

    def read(self, offset: int, size: int) -> bytes:
        return b"".join(self.chunks(offset, size))

    def close(self) -> None:
        os.close(self.fd)


@dataclass
class Progress:
    """What the run has established, for the result line."""
    terminal: bool = False
    entries: int = 0
    bytes: int = 0
    ignored_git_paths: int | None = None


def _nothing_after(fh: BinaryIO, wait: float) -> None:
    """The stream must END after the terminal frame. On a raw fd (the importer's fd 0), wait at most ``wait`` seconds
    for it to turn readable, then read at most ONE byte: end of stream passes, a byte or a pipe still open at the
    deadline is ``frame-bad:trailing``. Any other stream (an in-memory one) is read for one byte."""
    fd = fh.fileno() if isinstance(fh, io.RawIOBase) else None
    if fd is not None:
        deadline = time.monotonic() + wait
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Fatal("frame-bad:trailing", f"the frame pipe was still open {wait:g} s after the terminal frame")
            try:
                ready, _, _ = select.select([fd], [], [], left)
            except InterruptedError:
                continue
            if ready:
                break
    if fh.read(1):
        raise Fatal("frame-bad:trailing", "bytes after the terminal frame")


def spool_frames(fh: BinaryIO, spool: Spool, progress: Progress, *, payload_cap: int,
                 trailing_wait: float = TRAILING_WAIT) -> list[Frame]:
    """Step 0: every frame into the spool, decoded by the shared codec, then the terminal frame, whose ``count`` and
    ``bytes`` must equal what arrived, with nothing after it (:func:`_nothing_after`, bounded by ``trailing_wait``).
    No git runs before this returns."""
    frames: list[Frame] = []
    seen: set[bytes] = set()
    total = 0
    while True:
        try:
            header = walkframe.read_header(fh)
        except Refused as r:
            raise Fatal(r.name, r.detail) from None
        if header is None:
            raise Fatal("frame-bad:terminal", f"the stream ended after {len(frames)} frames without a terminal one")
        if "end" in header:
            if header["count"] != len(frames) or header["bytes"] != total:
                raise Fatal("frame-bad:terminal",
                            f"says {header['count']}/{header['bytes']}, saw {len(frames)}/{total}")
            _nothing_after(fh, trailing_wait)
            progress.terminal = True
            progress.entries, progress.bytes = len(frames), total
            return frames
        path = base64.b64decode(header["path_b64"])
        if path in seen:
            raise Fatal("frame-bad:path_b64", "a path framed twice")
        seen.add(path)
        size = header["size"]
        if total + size > payload_cap:
            raise Fatal("frame-bad:size", f"payload past {payload_cap}")
        offset = spool.end
        left = size
        while left:
            data = fh.read(min(left, _CHUNK))
            if not data:
                raise Fatal("frame-bad:truncated", "inside a payload")
            spool.append(data)
            left -= len(data)
        total += size
        frames.append(Frame(path, header["type"], header["mode"], size, offset))


# --- git children -----------------------------------------------------------------------------------------------

@dataclass
class GitRunner:
    """Runs every importer git: the pinned env, the ``safe.directory`` argv, stdout and stderr through bounded
    readers (the first and last :data:`_CAPTURE` bytes), stdin optionally streamed from a spool slice. ``log``: the
    DONE's seam, one line per argv started."""
    git: str
    work: Path
    log: BinaryIO | None = None
    started: list[list[str]] = field(default_factory=list)

    def run(self, *args: str, stdin: tuple[Spool, int, int] | bytes | Path | None = None,
            env: dict[str, str] | None = None, stdout_fd: int | None = None) -> tuple[int, str, str]:
        """``stdin``: a spool slice streamed in chunks, bytes the importer itself built from data it parsed, or a file
        the importer wrote in ``spool/`` (streamed). ``env``: keys added to :func:`git_env` (the WIP's index file and
        ident), never replacing one of its own. ``stdout_fd``: the child's stdout goes to that fd whole (an output
        the bounded reader would cut); the returned stdout is then empty."""
        argv = git_argv(self.git, self.work, *args)
        self.started.append(argv)
        if self.log is not None:
            self.log.write((" ".join(argv) + "\n").encode("utf-8", "replace"))
            self.log.flush()
        child_env = git_env(self.work)
        if env:
            if set(env) & set(child_env):
                raise ValueError(f"env would replace {sorted(set(env) & set(child_env))}")
            child_env.update(env)
        p = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE if stdout_fd is None else stdout_fd,
                             stderr=subprocess.PIPE, env=child_env, cwd=str(self.work), close_fds=True)
        out: list[bytes] = []
        err: list[bytes] = []
        readers = ([(p.stdout, out)] if stdout_fd is None else []) + [(p.stderr, err)]
        started: list[threading.Thread] = []
        try:
            for pipe, keep in readers:
                t = threading.Thread(target=_drain, args=(pipe, keep), daemon=True)
                t.start()
                started.append(t)
            if stdin is not None:
                try:
                    if isinstance(stdin, bytes):
                        p.stdin.write(stdin)
                    elif isinstance(stdin, Path):
                        for chunk in _file_chunks(stdin):
                            p.stdin.write(chunk)
                    else:
                        spool, offset, size = stdin
                        for chunk in spool.chunks(offset, size):
                            p.stdin.write(chunk)
                except BrokenPipeError:
                    pass
                finally:
                    _close_quietly(p.stdin)
            rc = p.wait()
        except BaseException:
            # No child outlives a failure after its spawn: kill it, reap it, close what no reader owns.
            _close_quietly(p.stdin)
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
            for pipe, _keep in readers[len(started):]:
                _close_quietly(pipe)
            raise
        finally:
            for t in started:
                t.join()
        return rc, b"".join(out).decode("utf-8", "replace"), b"".join(err).decode("utf-8", "replace")

    def must(self, *args: str, stdin: tuple[Spool, int, int] | bytes | Path | None = None,
             env: dict[str, str] | None = None, stdout_fd: int | None = None) -> str:
        rc, out, err = self.run(*args, stdin=stdin, env=env, stdout_fd=stdout_fd)
        if rc != 0:
            last = (err.strip().splitlines() or [""])[-1]
            raise Fatal(None, f"git {args[0]} rc {rc}: {last[:300]}")
        return out


def _file_chunks(path: Path) -> Iterator[bytes]:
    """A file the importer wrote, in chunks; no symlink followed."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        while chunk := os.read(fd, _CHUNK):
            yield chunk
    finally:
        os.close(fd)


def _close_quietly(f: BinaryIO | None) -> None:
    if f is not None:
        try:
            f.close()
        except OSError:
            pass


def _drain(pipe: BinaryIO, keep: list[bytes]) -> None:
    """Read ``pipe`` to its end, keeping its first and last :data:`_CAPTURE` bytes; the elision marker only where
    bytes were dropped."""
    head = bytearray()
    tail = bytearray()
    dropped = False
    while True:
        data = pipe.read(_CHUNK)
        if not data:
            break
        if len(head) < _CAPTURE:
            take = _CAPTURE - len(head)
            head += data[:take]
            data = data[take:]
        tail += data
        if len(tail) > _CAPTURE:
            del tail[:-_CAPTURE]
            dropped = True
    pipe.close()
    keep.append(bytes(head) + (b"\n...\n" if dropped else b"") + bytes(tail))


# --- step 1: the base -------------------------------------------------------------------------------------------

def _index_pack(runner: GitRunner, spool: Spool, offset: int, size: int, cap: int) -> None:
    """One pack from the spool into ``repo.git`` with a fresh idx: object checks, two threads, the input cap."""
    runner.must("index-pack", "--stdin", "--fsck-objects", "--threads=2", f"--max-input-size={cap}",
                stdin=(spool, offset, size))


def ingest_base(runner: GitRunner, spool: Spool, frames: Sequence[Frame], *, object_format: str, base_oid: str,
                cap: int) -> None:
    """Step 1: ``in/repo.bundle``'s header by the shared grammar (the format the alloc recorded, no prerequisite, its
    HEAD the alloc's base), then its pack into ``repo.git``. The base commit must then be in the store."""
    base = [f for f in frames if f.path == BASE_BUNDLE]
    if len(base) != 1 or base[0].type != "file":
        raise Fatal("base-bundle-mismatch", "no base bundle frame")
    b = base[0]
    head = spool.read(b.offset, min(b.size, bundlefmt.HEADER_MAX))
    end = head.find(b"\n\n")
    if end < 0:
        raise Fatal("base-bundle-mismatch", "the base bundle has no blank line in its header")
    try:
        h = bundlefmt.parse(head[:end + 2])
        oid, _ref = bundlefmt.base_of(h)
    except Refused as r:
        raise Fatal("base-bundle-mismatch", f"{r.name}: {r.detail}") from None
    except Exception as e:  # noqa: BLE001 - a header the grammar did not foresee is still this name
        raise Fatal("base-bundle-mismatch", f"the base bundle header: {type(e).__name__}") from None
    if h.object_format != object_format:
        raise Fatal("base-bundle-mismatch", f"a {h.object_format} bundle for a {object_format} base")
    if oid != base_oid:
        raise Fatal("base-bundle-mismatch", "the bundle's HEAD is not the alloc's base")
    _index_pack(runner, spool, b.offset + h.size, b.size - h.size, cap)
    rc, out, _err = runner.run("cat-file", "-t", base_oid)
    if rc != 0 or out.strip() != "commit":
        raise Fatal("base-bundle-mismatch", "the base commit is not in the store after the base pack")


# --- step 2: the session's objects ------------------------------------------------------------------------------

def _loose_re(object_format: str) -> re.Pattern[bytes]:
    n = FORMATS[object_format][0]
    return re.compile(rb"objects/([0-9a-f]{2})/([0-9a-f]{%d})" % (n - 2))


def is_ref_path(rel: bytes) -> bool:
    """A ``.git/`` path step 3 reads (5b2): ``HEAD``, ``packed-refs``, ``refs/heads/**``."""
    return rel in (b"HEAD", b"packed-refs") or rel.startswith(b"refs/heads/")


def verify_loose(spool: Spool, f: Frame, oid: str, object_format: str, cap: int) -> None:
    """Inflate and hash one loose object from the spool: a ``<type> <size>\\0`` header with a known type and a size
    within ``cap``, exactly ``size`` content bytes, the zlib stream ending with the payload, and the hash of it all
    equal to its name. Any failure is ``loose-name-mismatch``: such a file is not the object its name says."""
    def bad(why: str) -> Fatal:
        return Fatal("loose-name-mismatch", f"{oid}: {why}")

    hasher = FORMATS[object_format][1]()
    d = zlib.decompressobj()
    header = bytearray()
    want: int | None = None
    got = 0

    def take(out: bytes) -> None:
        nonlocal want, got
        if want is None:
            header.extend(out)
            nul = header.find(b"\0")
            if nul < 0:
                if len(header) > _LOOSE_HEADER_MAX:
                    raise bad("no object header")
                return
            typ, sp, num = bytes(header[:nul]).partition(b" ")
            if typ not in _LOOSE_TYPES or not sp or not num.isdigit() or (num != b"0" and num[:1] == b"0"):
                raise bad("a malformed object header")
            want = int(num)
            if want > cap:
                raise bad(f"an object of {want} bytes past {cap}")
            hasher.update(bytes(header[:nul + 1]))
            out = bytes(header[nul + 1:])
        got += len(out)
        if got > want:
            raise bad("more content than its header says")
        hasher.update(out)

    try:
        for chunk in spool.chunks(f.offset, f.size):
            if d.eof:
                raise bad("bytes after the zlib stream")
            data = chunk
            while data:
                take(d.decompress(data, _CHUNK))
                data = d.unconsumed_tail
                if d.eof:
                    if data or d.unused_data:
                        raise bad("bytes after the zlib stream")
                    break
    except zlib.error as e:
        raise bad(f"zlib: {e}") from None
    if not d.eof or want is None or got != want:
        raise bad("the zlib stream does not end with the object")
    if hasher.hexdigest() != oid:
        raise bad("its content hashes to another name")


def install_loose(objects: Path, oid: str, spool: Spool, f: Frame, object_format: str) -> None:
    """Write one verified loose object's bytes at its final name: a temp name in its directory (``O_CREAT|O_EXCL``),
    fsync, ``link()`` to the final name, unlink the temp; no truncated object ever sits at a final name. An existing
    final name counts as present only if it inflates and hashes to the same oid (``export-object-collision``)."""
    d = objects / oid[:2]
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        final = d / oid[2:]
        tmp = d / f"tmp_{oid[2:]}_{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o444)
        try:
            try:
                for chunk in spool.chunks(f.offset, f.size):
                    write_all(fd, chunk)
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            _unlink_quietly(tmp)
            raise
        _link_final(tmp, final, oid, object_format)
    except OSError as e:
        raise _oserror(e, f"writing loose object {oid}") from None


def _link_final(tmp: Path, final: Path, oid: str, object_format: str) -> None:
    """``link()`` a written, fsynced temp to its final loose name, then unlink the temp. An existing final name counts
    as present only if it inflates and hashes to ``oid`` (``export-object-collision``, S7)."""
    try:
        os.link(tmp, final, follow_symlinks=False)
    except FileExistsError:
        if not _same_object(final, oid, object_format):
            raise Fatal("export-object-collision", f"{oid} is present with other bytes") from None
    finally:
        os.unlink(tmp)


def _unlink_quietly(path: Path) -> None:
    """A temp name the importer created, removed on a failed write (an error here would hide the first one)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _same_object(path: Path, oid: str, object_format: str) -> bool:
    """Whether the file at a final loose name inflates and hashes to ``oid``, read and inflated in chunks, so memory
    stays bounded whatever the object's size. Bytes after the zlib stream, or a stream that does not end, are not
    the object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    hasher = FORMATS[object_format][1]()
    d = zlib.decompressobj()
    try:
        while chunk := os.read(fd, _CHUNK):
            if d.eof:
                return False
            data = chunk
            while data:
                hasher.update(d.decompress(data, _CHUNK))
                data = d.unconsumed_tail
                if d.eof:
                    if data or d.unused_data:
                        return False
                    break
    except zlib.error:
        return False
    finally:
        os.close(fd)
    return d.eof and hasher.hexdigest() == oid


def ingest_objects(runner: GitRunner, spool: Spool, frames: Sequence[Frame], *, object_format: str,
                   cap: int) -> int:
    """Step 2: the session's loose objects (name-verified, copied) and packs (``index-pack``, a fresh idx). Every
    other non-directory path under ``repo/.git/`` that step 3 does not read is ignored and counted; the count is
    returned. No session byte is ever placed in ``repo.git`` but a verified object or a pack index-pack accepted."""
    loose = _loose_re(object_format)
    objects = runner.work / "repo.git" / "objects"
    packs: list[Frame] = []
    ignored = 0
    for f in frames:
        if not f.path.startswith(GIT_PREFIX):
            continue
        rel = f.path[len(GIT_PREFIX):]
        m = loose.fullmatch(rel) if f.type == "file" else None
        if m is not None:
            oid = (m.group(1) + m.group(2)).decode("ascii")
            verify_loose(spool, f, oid, object_format, cap)
            install_loose(objects, oid, spool, f, object_format)
        elif f.type == "file" and _PACK.fullmatch(rel):
            packs.append(f)
        elif f.type == "dir" or (f.type == "file" and is_ref_path(rel)):
            continue
        else:
            ignored += 1
    for f in packs:
        _index_pack(runner, spool, f.offset, f.size, cap)
    return ignored


# --- step 3: refs -------------------------------------------------------------------------------------------------

#: The most branch refs one session may carry (T14): past it the run refuses ``refs-cap`` before any ref is checked.
REFS_CAP = 4096
#: The branch root's HEAD names (:data:`HEAD_TEXT`). A session ref of that exact name is refused, so ``repo.git``'s
#: HEAD can never resolve to slot-supplied content.
RESERVED_REF = HEAD_TEXT[len(b"ref: "):].rstrip(b"\n")
#: The largest loose ref file or ``packed-refs`` line read as a ref; anything longer is not one.
_REF_TEXT_MAX = 4096
_HEADS = b"refs/heads/"


def _ref_bad(name: bytes, why: str) -> Fatal:
    return Fatal(f"ref-name-bad:{walker.escape(name)}", why)


@dataclass(frozen=True)
class RefValue:
    """One parsed ref: ``oid`` (hex) or ``target`` (a symbolic ``refs/heads/`` name), never both."""
    oid: str | None = None
    target: bytes | None = None


def parse_ref_text(data: bytes, object_format: str) -> RefValue | None:
    """A loose ref's or HEAD's bytes as data: ``<hex>`` of the format's length or ``ref: refs/heads/<name>``, one
    optional trailing newline. Anything else is ``None``."""
    n = FORMATS[object_format][0]
    m = re.fullmatch(rb"([0-9a-f]{%d})\n?" % n, data)
    if m is not None:
        return RefValue(oid=m.group(1).decode("ascii"))
    m = re.fullmatch(rb"ref: (refs/heads/[^\n\0]+)\n?", data)
    if m is not None:
        return RefValue(target=m.group(1))
    return None


def _packed_lines(spool: Spool, f: Frame) -> Iterator[bytes]:
    """``packed-refs`` line by line from the spool, each without its newline, none longer than
    :data:`_REF_TEXT_MAX` (a longer one refuses): memory stays bounded whatever the file's size."""
    buf = bytearray()
    for chunk in spool.chunks(f.offset, f.size):
        buf += chunk
        while (nl := buf.find(b"\n")) >= 0:
            if nl > _REF_TEXT_MAX:
                break
            yield bytes(buf[:nl])
            del buf[:nl + 1]
        if len(buf) > _REF_TEXT_MAX:
            raise _ref_bad(b"packed-refs", f"a line longer than {_REF_TEXT_MAX} bytes")
    if buf:
        yield bytes(buf)


def parse_packed_refs(spool: Spool, f: Frame, object_format: str) -> dict[bytes, str]:
    """``packed-refs`` as data: a ``#`` header on the first line only, peel lines (``^...``) skipped whatever they
    hold, ``<hex> <refname>`` kept when ``refname`` is under ``refs/heads/`` and skipped otherwise (tags, remotes).
    Any other line, or a branch listed twice, refuses. Past :data:`REFS_CAP` branches = ``refs-cap``, at once."""
    n = FORMATS[object_format][0]
    line_re = re.compile(rb"([0-9a-f]{%d}) ([^\n\0]+)" % n)
    out: dict[bytes, str] = {}
    for i, line in enumerate(_packed_lines(spool, f)):
        if line.startswith(b"^") or (i == 0 and line.startswith(b"#")):
            continue
        m = line_re.fullmatch(line)
        if m is None:
            raise _ref_bad(b"packed-refs", f"line {i + 1} is not '<oid> <refname>'")
        name = m.group(2)
        if not name.startswith(_HEADS):
            continue
        if name in out:
            raise _ref_bad(name, "listed twice in packed-refs")
        out[name] = m.group(1).decode("ascii")
        if len(out) > REFS_CAP:
            raise Fatal("refs-cap", f"packed-refs lists more than {REFS_CAP} branches")
    return out


def conflict_pair(names: Iterable[bytes]) -> tuple[bytes, bytes] | None:
    """The first name (in ``names`` order) that is also a DIRECTORY of another name, as ``(name, the other)``:
    ``a`` and ``a/b`` cannot both exist as refs, nor as index paths. One trie of ``/``-components, so the cost is the
    names' total length whatever their depth. Equal names are not a conflict (the caller's names are unique)."""
    root: dict = {}
    for name in names:
        node = root
        parts = name.split(b"/")
        for i, part in enumerate(parts):
            if None in node:  # a name already given ends here: it is a directory of this one
                return b"/".join(parts[:i]), name
            node = node.setdefault(part, {})
        if any(k is not None for k in node):  # this name is a directory of one already given
            longer = [name]
            while None not in node:
                k = next(k for k in node if k is not None)
                longer.append(k)
                node = node[k]
            return name, b"/".join(longer)
        node[None] = True
    return None


def _resolve_head(head: RefValue | None, refs: dict[bytes, RefValue]) -> str | None:
    """HEAD's oid: its own (detached), or the branch it names followed through symbolic branches. A branch that is
    not there (unborn) is no head. A chain that comes back to a name it passed is ``ref-head-loop``."""
    if head is None:
        return None
    seen: set[bytes] = set()
    cur = head
    while cur.oid is None:
        assert cur.target is not None
        if cur.target in seen:
            raise Fatal("ref-head-loop", f"HEAD's chain returns to {walker.escape(cur.target)}")
        seen.add(cur.target)
        nxt = refs.get(cur.target)
        if nxt is None:
            return None
        cur = nxt
    return cur.oid


def object_types(runner: GitRunner, oids: Sequence[str]) -> dict[str, str]:
    """``{oid: type}`` for full-length hex oids, ``missing`` for an absent one, by ``cat-file --batch-check``
    answering ``<oid> <type>``: each type is read beside its own oid, never by line position. The oids go in batches
    small enough that a batch's whole answer fits the bounded reader's kept head; an answer that is not exactly one
    line per oid asked is a failure."""
    per_line = max((len(o) for o in oids), default=0) + len(" missing\n")
    batch = max(1, _CAPTURE // max(per_line, 1))
    types: dict[str, str] = {}
    for i in range(0, len(oids), batch):
        part = oids[i:i + batch]
        out = runner.must("cat-file", "--batch-check=%(objectname) %(objecttype)",
                          stdin="".join(o + "\n" for o in part).encode())
        got: dict[str, str] = {}
        for line in out.split("\n")[:-1]:
            oid, _sp, kind = line.partition(" ")
            if oid in got:
                raise Fatal(None, f"cat-file answered {oid} twice")
            got[oid] = kind
        if set(got) != set(part):
            raise Fatal(None, f"cat-file answered {len(got)} of {len(part)} objects")
        types.update(got)
    return types


def import_refs(runner: GitRunner, spool: Spool, frames: Sequence[Frame], *, object_format: str,
                session: str) -> str | None:
    """Step 3: the session's ``HEAD``, loose ``refs/heads/**`` and ``packed-refs``, parsed as data (never by a git
    reading the session's ``.git``). A reftable store (any ``reftable`` path) is ``refs-format-unsupported``: its refs
    are not in these files. A loose ref overrides a packed one of the same name, as in git. Then, in this order: more
    than :data:`REFS_CAP` branches = ``refs-cap``; the reserved name = ``ref-reserved:<name>``; a HEAD loop =
    ``ref-head-loop``; each name, and each symbolic target (HEAD's or a branch's, whether or not a ref exists there),
    through ``check-ref-format`` (``ref-name-bad:<escaped>``); two imported branches where
    one is a directory of the other (``a`` and ``a/b``, packed or loose) = ``ref-conflict:<escaped shorter name>``, by
    name before any ref is written (``update-ref`` would refuse the pair without naming it); every oid in the store
    (``ref-object-missing``) and a commit (``ref-not-commit``: outside ``refs/heads/`` git writes a ref to any
    object); then ONE ``update-ref --stdin -z`` transaction creating ``refs/levain/<s>/heads/<name>``
    for each branch holding an oid and ``refs/levain/<s>/head`` for HEAD's oid. A symbolic branch is followed for HEAD
    only, never imported as a ref of its own. Returns HEAD's oid, or ``None`` when HEAD resolves to no oid."""
    loose: dict[bytes, RefValue] = {}
    packed: dict[bytes, str] = {}
    head: RefValue | None = None
    for f in frames:
        if not f.path.startswith(GIT_PREFIX):
            continue
        rel = f.path[len(GIT_PREFIX):]
        if rel == b"reftable" or rel.startswith(b"reftable/"):
            raise Fatal("refs-format-unsupported", "the session's refs are in a reftable store")
    for f in frames:
        if not f.path.startswith(GIT_PREFIX) or f.type != "file":
            continue
        rel = f.path[len(GIT_PREFIX):]
        if rel == b"packed-refs":
            packed = parse_packed_refs(spool, f, object_format)
            continue
        if rel != b"HEAD" and not rel.startswith(_HEADS):
            continue
        if f.size > _REF_TEXT_MAX:
            raise _ref_bad(rel, f"a ref file of {f.size} bytes")
        value = parse_ref_text(spool.read(f.offset, f.size), object_format)
        if value is None:
            raise _ref_bad(rel, "its content is neither an oid nor 'ref: refs/heads/<name>'")
        if rel == b"HEAD":
            head = value
        else:
            loose[rel] = value
            if len(loose) > REFS_CAP:
                raise Fatal("refs-cap", f"more than {REFS_CAP} loose branches")
    refs: dict[bytes, RefValue] = {name: RefValue(oid=oid) for name, oid in packed.items()}
    refs.update(loose)
    if len(refs) > REFS_CAP:
        raise Fatal("refs-cap", f"{len(refs)} branches, more than {REFS_CAP}")
    if RESERVED_REF in refs:
        raise Fatal(f"ref-reserved:{RESERVED_REF.decode('ascii')}", "the session carries the reserved branch name")
    head_oid = _resolve_head(head, refs)
    # Every symbolic target is a name too, whether or not a ref exists at it.
    targets = {v.target for v in (*refs.values(), *(() if head is None else (head,))) if v.target is not None}
    for name in sorted(set(refs) | targets):
        rc, _out, _err = runner.run("check-ref-format", os.fsdecode(name))
        if rc != 0:
            raise _ref_bad(name, "refused by check-ref-format" if name in refs
                           else "a symbolic target refused by check-ref-format")
    want = [(name, v.oid) for name, v in sorted(refs.items()) if v.oid is not None]
    pair = conflict_pair(name for name, _oid in want)
    if pair is not None:
        raise Fatal(f"ref-conflict:{walker.escape(pair[0])}",
                    f"{walker.escape(pair[0])} is also a directory of {walker.escape(pair[1])}")
    if head_oid is not None:
        want.append((b"HEAD", head_oid))
    oids = sorted({oid for _name, oid in want})
    if not oids:
        return None
    types = object_types(runner, oids)
    for name, oid in want:
        if types[oid] == "missing":
            raise Fatal("ref-object-missing", f"{walker.escape(name)} names {oid}, not in the store")
        if types[oid] != "commit":
            raise Fatal("ref-not-commit", f"{walker.escape(name)} names {oid}, a {types[oid]}")
    base = f"refs/levain/{session}/".encode("ascii")
    txn = b"".join(b"create " + base + b"heads/" + name[len(_HEADS):] + b"\0" + oid.encode("ascii") + b"\0"
                   for name, oid in want if name != b"HEAD")
    if head_oid is not None:
        txn += b"create " + base + b"head\0" + head_oid.encode("ascii") + b"\0"
    runner.must("update-ref", "--stdin", "-z", stdin=txn)
    return head_oid


# --- step 4: the worktree commit ----------------------------------------------------------------------------------

#: The WIP commit's author and committer: fixed, never the session's or the host's configuration.
WIP_IDENT = ("Levain", "levain@localhost")
#: The temp index's name under ``.work/spool/``.
WIP_INDEX = "wip-index"
_REPO = walkframe.PRUNE_SCOPE_PREFIX[0] + b"/"
_INDEX_ENTRY_FIXED = 40  # the ctime .. size words of an on-disk index entry, before its oid


def worktree_entries(frames: Sequence[Frame]) -> list[tuple[bytes, int, Frame]]:
    """The worktree as index entries ``(path, mode, frame)``: every ``file`` and ``link`` frame under ``repo/``, its
    path relative to ``repo/``, minus the session's own git directory (a first component that IS ``.git``) and minus
    every skipped nested repo whole (a directory holding a ``skipped-git`` frame: nothing below it is the session's
    worktree, as ``git add`` would not add its files). A ``link`` is 120000; a ``file`` is 100755 when the owner's
    execute bit is set (git's own index rule; a group or other bit alone is 100644), else 100644. Directories,
    special files and ``unread`` placeholders carry no index entry."""
    nested: dict = {}
    for f in frames:
        if f.type == "skipped-git" and f.path.startswith(_REPO):
            node = nested
            for part in f.path[len(_REPO):].split(b"/")[:-1]:
                node = node.setdefault(part, {})
            node[None] = True
    out: list[tuple[bytes, int, Frame]] = []
    for f in frames:
        if f.type not in walkframe.PAYLOAD_TYPES or not f.path.startswith(_REPO):
            continue
        rel = f.path[len(_REPO):]
        parts = rel.split(b"/")
        if walker.is_git_name(parts[0]):
            continue
        node = nested
        skipped = False
        for part in parts[:-1]:
            node = node.get(part)
            if node is None:
                break
            if None in node:
                skipped = True
                break
        if skipped:
            continue
        mode = 0o120000 if f.type == "link" else (0o100755 if f.mode & 0o100 else 0o100644)
        out.append((rel, mode, f))
    return out


def write_blob(objects: Path, spool: Spool, f: Frame, object_format: str, written: set[str]) -> str:
    """One worktree payload as a loose blob, by the importer itself (no git per blob, no filter): hash
    ``blob <len>\\0<payload>`` from the spool, then, unless this run already wrote that oid, deflate it into a temp
    name in its directory (``O_CREAT|O_EXCL``), fsync, and :func:`_link_final` it (the S7 rule for a name already
    there). Returns the oid."""
    hasher = FORMATS[object_format][1]()
    header = b"blob %d\0" % f.size
    hasher.update(header)
    for chunk in spool.chunks(f.offset, f.size):
        hasher.update(chunk)
    oid = hasher.hexdigest()
    if oid in written:
        return oid
    d = objects / oid[:2]
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        tmp = d / f"tmp_{oid[2:]}_{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o444)
        try:
            try:
                z = zlib.compressobj()
                write_all(fd, z.compress(header))
                for chunk in spool.chunks(f.offset, f.size):
                    write_all(fd, z.compress(chunk))
                write_all(fd, z.flush())
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            _unlink_quietly(tmp)
            raise
        _link_final(tmp, d / oid[2:], oid, object_format)
    except OSError as e:
        raise _oserror(e, f"writing blob {oid}") from None
    written.add(oid)
    return oid


def _index_paths(data: bytes, object_format: str) -> Iterator[bytes]:
    """The paths of an on-disk version-2 index (git wrote it from the importer's own lines): each entry is the fixed
    stat words, the oid, two flag bytes, the NUL-terminated path, padded to a multiple of eight."""
    raw = FORMATS[object_format][0] // 2
    count = int.from_bytes(data[8:12], "big")
    pos = 12
    for _ in range(count):
        start = pos + _INDEX_ENTRY_FIXED + raw + 2
        end = data.index(b"\0", start)
        yield data[start:end]
        pos += (_INDEX_ENTRY_FIXED + raw + 2 + (end - start) + 8) & ~7


def write_wip(runner: GitRunner, spool: Spool, frames: Sequence[Frame], *, object_format: str, session: str,
              head_oid: str | None, base_oid: str) -> str | None:
    """Step 4: every worktree blob written (:func:`write_blob`); the entries into a fresh temp index by ONE
    ``update-index -z --index-info``; ``write-tree``; ``commit-tree`` on the parent (``refs/levain/<s>/head`` when
    step 3 made it, else the base) with :data:`WIP_IDENT`; ``refs/levain/<s>/wip`` created only when the tree differs
    from the parent's. Returns the wip oid or ``None``.

    ``update-index --index-info`` refuses nothing: a path ``verify_path`` rejects (a ``.git`` in any HFS or NTFS
    spelling, a ``.gitmodules`` symlink) is dropped with a warning and exit 0, and of a file/directory pair the later
    line silently wins. So the importer checks the pair itself first, and after it reads the index's own entry count
    against the entries it fed; either failure = ``wip-path-refused:<escaped path>``, the path found in the importer's
    own data or in the index git wrote, never in git's stderr."""
    entries = worktree_entries(frames)
    pair = conflict_pair(rel for rel, _m, _f in entries)
    if pair is not None:
        raise Fatal(f"wip-path-refused:{walker.escape(pair[0])}",
                    f"{walker.escape(pair[0])} is a file and a directory of {walker.escape(pair[1])}")
    objects = runner.work / "repo.git" / "objects"
    written: set[str] = set()
    lines = bytearray()
    for rel, mode, f in entries:
        oid = write_blob(objects, spool, f, object_format, written)
        lines += b"%o %s\t%s\0" % (mode, oid.encode("ascii"), rel)
    index = runner.work / "spool" / WIP_INDEX
    if os.path.lexists(index):
        raise Fatal(None, "the WIP index exists before step 4")
    ienv = {"GIT_INDEX_FILE": str(index)}
    runner.must("update-index", "--index-version=2", "-z", "--index-info", stdin=bytes(lines), env=ienv)
    try:
        data = index.read_bytes() if entries else b""
    except OSError as e:
        raise _oserror(e, "reading the WIP index") from None
    if entries:
        if data[:4] != b"DIRC" or int.from_bytes(data[4:8], "big") != 2:
            raise Fatal(None, "the WIP index is not a version-2 index")
        if int.from_bytes(data[8:12], "big") != len(entries):
            present = set(_index_paths(data, object_format))
            first = next((rel for rel, _m, _f in entries if rel not in present), b"")
            raise Fatal(f"wip-path-refused:{walker.escape(first)}",
                        f"update-index kept {len(present)} of {len(entries)} paths")
    tree = runner.must("write-tree", env=ienv).strip()
    parent = head_oid if head_oid is not None else base_oid  # a commit: step 1 checks the base, step 3 HEAD's oid
    if runner.must("rev-parse", "--verify", f"{parent}^{{tree}}").strip() == tree:
        return None
    when = f"{int(time.time())} +0000"
    name, email = WIP_IDENT
    cenv = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": when}
    wip = runner.must("commit-tree", tree, "-p", parent, "-m", f"levain: the worktree of {session}", env=cenv).strip()
    runner.must("update-ref", "--stdin", "-z",
                stdin=f"create refs/levain/{session}/wip\0{wip}\0".encode("ascii"))
    return wip


# --- step 5: fsck and the no-ref check ----------------------------------------------------------------------------

_FSCK_LINE = re.compile(r"^(?:error|warning) in [a-z]+ [0-9a-f]+: (%s):" % fsck_ids.ID_PATTERN, re.MULTILINE)


def has_session_git(frames: Sequence[Frame]) -> bool:
    """Whether the walk framed a session git directory: any path under ``repo/`` whose first component is ``.git``
    to a case-insensitive or normalising filesystem."""
    return any(f.path.startswith(_REPO) and walker.is_git_name(f.path[len(_REPO):].split(b"/")[0]) for f in frames)


def check_store(runner: GitRunner, *, session: str, session_git: bool) -> None:
    """Step 5: ``fsck --strict --no-dangling`` over every object in the store; non-zero = ``fsck:<id>``, the id of its
    first ``error|warning in <type> <oid>: <id>:`` line matched against :func:`fsck_ids.closed_ids`, else
    ``fsck:other``. Then the explicit no-ref check: fsck exits 0 on a store with no ref at all, so when a session
    ``.git`` was framed (``session_git``) and steps 3 and 4 left no ref under ``refs/levain/<s>/``, the run is refused
    ``no-ref`` by its own name. With no session ``.git``, no ref means step 4 found the worktree equal to base's tree:
    step 6 answers ``adopt-nothing-new`` (DESIGN T35)."""
    rc, out, err = runner.run("fsck", "--strict", "--no-dangling")
    if rc != 0:
        m = _FSCK_LINE.search(err) or _FSCK_LINE.search(out)
        found = m.group(1) if m is not None else None
        ident = found if found in fsck_ids.closed_ids() else "other"
        raise Fatal(f"fsck:{ident}", f"fsck rc {rc}, first id line: {found or 'none'}"[:300])
    if session_git and not runner.must("for-each-ref", "--count=1", "--format=%(refname)",
                                       f"refs/levain/{session}/").strip():
        raise Fatal("no-ref", f"a session .git, and no ref under refs/levain/{session}/ after the refs and worktree "
                              "steps")


# --- step 6: the result, the pack, the bundle, the report ---------------------------------------------------------

#: ``out/report.json``'s format (DESIGN §4.4) and the most bytes it may take.
REPORT_FORMAT = "levain-adopt-report/1"
REPORT_CAP = 16 << 20
#: The three results (DESIGN §5 step 6, §4.5), as the result line and the report carry them.
DELTA, UNRELATED, NOTHING_NEW = "delta", "unrelated", "adopt-nothing-new"
#: The frame types the report names: entries the walk saw and carried no content for.
NAMED_TYPES = frozenset({"fifo", "socket", "device", "skipped-git", "unread"})
#: The longest line read from a git output file of oids and types; anything longer is not one.
_OUT_LINE_MAX = 256


def _new_file(path: Path) -> int:
    """A new file of the importer's own in ``.work/`` (``O_EXCL``, no symlink followed), open for writing."""
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)


def _read_own(path: Path) -> BinaryIO:
    """A file the importer (or a git it ran) wrote in ``.work/``, buffered for reading; no symlink followed."""
    return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb")


def _lines(fh: BinaryIO, what: str) -> Iterator[bytes]:
    while line := fh.readline(_OUT_LINE_MAX):
        if not line.endswith(b"\n"):
            raise Fatal(None, f"{what}: a line past {_OUT_LINE_MAX} bytes or without its newline")
        yield line[:-1]


def find_tips(runner: GitRunner, *, session: str, base_oid: str) -> list[tuple[str, bytes]]:
    """The tips, ``(oid, refname)``: of ``refs/levain/<s>/head`` and ``refs/levain/<s>/wip``, in that order, each that
    exists and is NOT reachable from base (``merge-base --is-ancestor <tip> <base>`` exits 1; 0 = reachable, so a head
    equal to base is no tip; any other exit fails)."""
    names = [f"refs/levain/{session}/head", f"refs/levain/{session}/wip"]
    have: dict[str, str] = {}
    for line in runner.must("for-each-ref", "--format=%(objectname) %(refname)", *names).splitlines():
        oid, _sp, name = line.partition(" ")
        have[name] = oid
    tips: list[tuple[str, bytes]] = []
    for name in names:
        oid = have.get(name)
        if oid is None:
            continue
        rc, _out, err = runner.run("merge-base", "--is-ancestor", oid, base_oid)
        if rc == 0:
            continue
        if rc != 1:
            last = (err.strip().splitlines() or [""])[-1]
            raise Fatal(None, f"git merge-base rc {rc}: {last[:300]}")
        tips.append((oid, name.encode("ascii")))
    return tips


def list_objects(runner: GitRunner, tips: Sequence[tuple[str, bytes]], *, base_oid: str, hexlen: int,
                 objects: Path) -> list[str]:
    """``rev-list --objects --no-object-names --boundary <tips> --not <base>`` into ``spool/``: its boundary lines
    (``-<oid>``, commits reachable from base) are returned, in order, once each, as the prerequisites; every other line
    is written to ``objects`` (one oid per line), the object list."""
    revlist = runner.work / "spool" / "rev-list"
    line_re = re.compile(rb"(-?)([0-9a-f]{%d})" % hexlen)
    prereqs: list[str] = []
    seen: set[str] = set()
    try:
        fd = _new_file(revlist)
        try:
            runner.must("rev-list", "--objects", "--no-object-names", "--boundary", *(o for o, _n in tips), "--not",
                        base_oid, stdout_fd=fd)
        finally:
            os.close(fd)
        fd = _new_file(objects)
        try:
            with _read_own(revlist) as fh:
                for line in _lines(fh, "rev-list"):
                    m = line_re.fullmatch(line)
                    if m is None:
                        raise Fatal(None, "rev-list printed a line that is not an oid")
                    oid = m.group(2).decode("ascii")
                    if m.group(1):
                        if oid not in seen:
                            seen.add(oid)
                            prereqs.append(oid)
                    else:
                        write_all(fd, m.group(2) + b"\n")
        finally:
            os.close(fd)
    except OSError as e:
        raise _oserror(e, "the object list") from None
    return prereqs


def _tree_payloads(path: Path, hexlen: int) -> Iterator[bytes]:
    """The payloads of a ``cat-file --batch`` output that holds only trees: ``<oid> tree <size>``, the payload, a
    newline, again and again."""
    head_re = re.compile(rb"[0-9a-f]{%d} tree (0|[1-9][0-9]{0,15})" % hexlen)
    with _read_own(path) as fh:
        for line in _lines(fh, "cat-file --batch"):
            m = head_re.fullmatch(line)
            if m is None:
                raise Fatal(None, "cat-file --batch printed a header that is not a tree's")
            size = int(m.group(1))
            payload = fh.read(size)
            if len(payload) != size or fh.read(1) != b"\n":
                raise Fatal(None, "cat-file --batch ended inside a tree")
            yield payload


def find_specials(runner: GitRunner, *, hexlen: int, objects: Path) -> set[str]:
    """The special oids to SEND: :func:`special_paths.adopt_specials` over every tree of the object list (types by
    ``cat-file --batch-check``, the tree oids among them, and payloads by ``cat-file --batch``, all through files in
    ``spool/``), minus the oids the list already holds. Each must be a blob in the store: step 5's fsck refuses a
    non-blob or an absent object at a special entry (measured on both gits), so anything else here is a failure with
    no adopt name. A tree Levain's reader refuses (git's fsck passed it) is ``fsck:other``."""
    spool_dir = runner.work / "spool"
    types, tree_list, trees = spool_dir / "types", spool_dir / "tree-oids", spool_dir / "trees"
    type_re = re.compile(rb"([0-9a-f]{%d}) (commit|tree|blob|tag)" % hexlen)
    try:
        fd = _new_file(types)
        try:
            runner.must("cat-file", "--batch-check=%(objectname) %(objecttype)", stdin=objects, stdout_fd=fd)
        finally:
            os.close(fd)
        with _read_own(types) as fh, os.fdopen(_new_file(tree_list), "wb") as out:
            for line in _lines(fh, "cat-file --batch-check"):
                m = type_re.fullmatch(line)
                if m is None:
                    raise Fatal(None, "cat-file --batch-check: an object of the list without a known type")
                if m.group(2) == b"tree":
                    out.write(m.group(1) + b"\n")
        fd = _new_file(trees)
        try:
            runner.must("cat-file", "--batch", stdin=tree_list, stdout_fd=fd)
        finally:
            os.close(fd)
        try:
            specials = special_paths.adopt_specials(_tree_payloads(trees, hexlen), hexlen)
        except ValueError as e:
            raise Fatal("fsck:other", f"a tree Levain's reader refuses: {e}"[:300]) from None
        with _read_own(objects) as fh:
            for line in _lines(fh, "the object list"):
                specials.discard(line.decode("ascii"))
    except OSError as e:
        raise _oserror(e, "the special blobs") from None
    if specials:
        kinds = object_types(runner, sorted(specials))
        odd = sorted(o for o, k in kinds.items() if k != "blob")
        if odd:
            raise Fatal(None, f"a special entry names {odd[0]}, a {kinds[odd[0]]}, past step 5")
    return specials


def unsent_branches(runner: GitRunner, *, session: str, base_oid: str,
                    tips: Sequence[tuple[str, bytes]]) -> list[bytes]:
    """The session's branches the bundle does not carry, as the session named them (``refs/heads/<name>``), in refname
    order: every ``refs/levain/<s>/heads/*`` reachable from none of base and the tips (one ``for-each-ref`` with a
    ``--no-merged`` per commit; git lists the refs reachable from none of them). Read from a file in ``spool/`` as
    bytes: a ref name need not be UTF-8."""
    prefix = f"refs/levain/{session}/heads/".encode("ascii")
    listing = runner.work / "spool" / "unsent"
    filters = [f"--no-merged={oid}" for oid in (base_oid, *(o for o, _n in tips))]
    names: list[bytes] = []
    try:
        fd = _new_file(listing)
        try:
            runner.must("for-each-ref", "--format=%(refname)", *filters, prefix.decode("ascii"), stdout_fd=fd)
        finally:
            os.close(fd)
        with _read_own(listing) as fh:
            while line := fh.readline(len(prefix) + _REF_TEXT_MAX + 1):
                if not line.endswith(b"\n") or not line.startswith(prefix):
                    raise Fatal(None, "for-each-ref printed a line that is not one of the session's branches")
                names.append(_HEADS + line[len(prefix):-1])
    except OSError as e:
        raise _oserror(e, "the unsent branches") from None
    return names


#: Step 6 writes each output under ``spool/`` and links it to its ``out/`` name only once both are whole.
_OUTPUTS = (("bundle.part", "bundle"), ("report.part", "report.json"))


def _publish(work: Path) -> None:
    """``link()`` each written output to its ``out/`` name (the bundle only when there is one), then unlink its
    temp: an ``out/`` name never holds a partial file. The pair is NOT atomic: a process killed between the two
    links, or failing after them (``run``'s cleanup is best effort), can leave one or both names in ``out/``. The
    commit point is root's, not this function's: root publishes ``out/`` only for a run it classifies SUCCESS (exit 0,
    a fully valid result line, no failure observation of its own; CONTRACT §10.2), so ``out/`` from any other run is
    never read."""
    for part, final in _OUTPUTS:
        src = work / "spool" / part
        if not os.path.lexists(src):
            continue
        os.link(src, work / "out" / final, follow_symlinks=False)
        os.unlink(src)


def remove_outputs(work: Path) -> bool:
    """On a failed run: unlink both ``out/`` names and both temps. True when none is left."""
    paths = [p for part, final in _OUTPUTS for p in (work / "spool" / part, work / "out" / final)]
    for p in paths:
        _unlink_quietly(p)
    return not any(os.path.lexists(p) for p in paths)


def write_bundle(runner: GitRunner, *, object_format: str, tips: Sequence[tuple[str, bytes]],
                 prereqs: Sequence[str], objects: Path, specials: set[str]) -> None:
    """The bundle, into ``spool/bundle.part``: the header by :func:`bundlefmt.write_header` (a header past its bounds
    is refused by the grammar's own name), then ``pack-objects --stdout`` fed the object list plus the specials, in
    object mode and with no ``--thin``, so every delta in the pack is against an object in it."""
    try:
        header = bundlefmt.write_header(object_format, list(tips), list(prereqs))
    except Refused as r:
        raise Fatal(r.name, r.detail) from None
    try:
        fd = os.open(objects, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            write_all(fd, b"".join(o.encode("ascii") + b"\n" for o in sorted(specials)))
        finally:
            os.close(fd)
        fd = _new_file(runner.work / "spool" / _OUTPUTS[0][0])
        try:
            write_all(fd, header)
            runner.must("pack-objects", "--stdout", "-q", "--threads=2", stdin=objects, stdout_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise _oserror(e, "writing the bundle") from None


def write_report(work: Path, frames: Sequence[Frame], progress: Progress, *, session: str, result: str,
                 unsent: Sequence[bytes]) -> None:
    """The report (DESIGN §4.4), into ``spool/report.part``, canonical JSON within :data:`REPORT_CAP`: the result, the
    walk's counts, the unsent branches (:func:`unsent_branches`) as ``{ref_b64}``, then every frame of a
    :data:`NAMED_TYPES` type in frame order as ``{path_b64, type}``; both lists share the cap, and ``truncated`` is
    set when the next entry would carry the report past it (no later entry of either list follows). No base and no
    oids; the branch names are the one git fact, so a side branch the bundle leaves behind is not silent."""
    body: dict = {"format": REPORT_FORMAT, "session": session, "result": result,
                  "walk": {"entries": progress.entries, "bytes": progress.bytes},
                  "ignored_git_paths": progress.ignored_git_paths, "unsent_branches": [], "named": [],
                  "truncated": False}
    size = len(canon.canonical_bytes(body))
    entries = itertools.chain(
        (("unsent_branches", {"ref_b64": base64.b64encode(n).decode("ascii")}) for n in unsent),
        (("named", {"path_b64": base64.b64encode(f.path).decode("ascii"), "type": f.type})
         for f in frames if f.type in NAMED_TYPES))
    for key, entry in entries:
        cost = len(canon.canonical_bytes(entry)) + (1 if body[key] else 0)  # a comma only after an entry
        if size + cost > REPORT_CAP:
            body["truncated"] = True
            break
        body[key].append(entry)
        size += cost
    data = canon.canonical_bytes(body)
    if len(data) > REPORT_CAP:
        raise Fatal(None, f"the report is {len(data)} bytes, past {REPORT_CAP}")
    try:
        fd = _new_file(work / "spool" / _OUTPUTS[1][0])
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise _oserror(e, "writing the report") from None


def write_result(runner: GitRunner, frames: Sequence[Frame], progress: Progress, *, object_format: str, session: str,
                 base_oid: str) -> str:
    """Step 6. No tip (:func:`find_tips`) = ``adopt-nothing-new``: the report alone, no bundle. Else the object list
    and its boundary (:func:`list_objects`): no prerequisite = ``unrelated`` (an orphan carries its whole history),
    some = ``delta``; the specials (:func:`find_specials`), the bundle (:func:`write_bundle`), the report; then both
    are published to ``out/`` together (:func:`_publish`). Returns the result."""
    tips = find_tips(runner, session=session, base_oid=base_oid)
    unsent = unsent_branches(runner, session=session, base_oid=base_oid, tips=tips)
    if not tips:
        result = NOTHING_NEW
    else:
        hexlen = FORMATS[object_format][0]
        objects = runner.work / "spool" / "objects"
        prereqs = list_objects(runner, tips, base_oid=base_oid, hexlen=hexlen, objects=objects)
        specials = find_specials(runner, hexlen=hexlen, objects=objects)
        write_bundle(runner, object_format=object_format, tips=tips, prereqs=prereqs, objects=objects,
                     specials=specials)
        result = DELTA if prereqs else UNRELATED
    write_report(runner.work, frames, progress, session=session, result=result, unsent=unsent)
    try:
        _publish(runner.work)
    except OSError as e:
        raise _oserror(e, "publishing the outputs") from None
    return result


# --- the result line and the run ----------------------------------------------------------------------------------

def probe_enospc(work: Path) -> bool:
    """The fatal path's own readings: ``statvfs`` of ``.work/spool`` showing no block available to the importer, or
    one block created and written there failing ENOSPC or EDQUOT."""
    try:
        if os.statvfs(work / "spool").f_bavail == 0:
            return True
    except OSError:
        pass
    p = work / "spool" / f".probe-{os.getpid()}"
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        return e.errno in _ENOSPC_ERRNOS
    try:
        write_all(fd, b"\0" * 4096)
        os.fsync(fd)
        return False
    except OSError as e:
        return e.errno in _ENOSPC_ERRNOS
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(p)
        except OSError:
            pass


def result_line(progress: Progress, *, refused: str | None, detail: str, enospc: bool,
                result: str | None) -> bytes:
    """The one result line: canonical JSON, newline-terminated, within :data:`RESULT_CAP`. ``result`` is step 6's
    answer on exit 0, ``None`` on every other exit."""
    body = {"v": 1, "terminal": progress.terminal, "enospc": enospc, "refused": refused, "result": result,
            "detail": detail[:_DETAIL_MAX].encode("utf-8", "replace").decode("utf-8"), "walk": {"entries": progress.entries, "bytes": progress.bytes},
            "ignored_git_paths": progress.ignored_git_paths}
    data = canon.canonical_bytes(body) + b"\n"
    if len(data) > RESULT_CAP:
        body["detail"] = ""
        data = canon.canonical_bytes(body) + b"\n"
    return data


def _build_unbuildable_line() -> bytes:
    """The line written when :func:`result_line` itself cannot be built: fixed content, built once at import."""
    data = canon.canonical_bytes({"v": 1, "terminal": False, "enospc": False, "refused": "result-unbuildable",
                                  "result": None, "detail": "", "walk": {"entries": 0, "bytes": 0}, "ignored_git_paths": None}) + b"\n"
    parsed = canon.parse(data[:-1])
    assert data.count(b"\n") == 1 and len(data) <= RESULT_CAP and parsed["refused"] == "result-unbuildable"
    return data


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of printing and exiting, so a usage error still answers one result line."""

    def error(self, message: str):  # type: ignore[override]
        raise _Usage(message)

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        raise _Usage(message or f"exit {status}")


def _parser() -> _Parser:
    ap = _Parser(prog="export-importer", add_help=False)
    ap.add_argument("--git", required=True)
    ap.add_argument("--session", required=True, help="the session id, `s-<16 hex>`; names refs/levain/<s>/")
    ap.add_argument("--object-format", required=True, choices=sorted(FORMATS))
    ap.add_argument("--base-oid", required=True)
    ap.add_argument("--max-bytes", required=True, type=int, help="the spool's payload total (V7 bytes)")
    ap.add_argument("--max-input-size", required=True, type=int,
                    help="index-pack's input cap and one loose object's size cap (V7 bytes)")
    ap.add_argument("--git-log")
    return ap


def _probe_quietly(work: Path) -> bool:
    try:
        return probe_enospc(work)
    except Exception:  # noqa: BLE001 - a probe never decides whether the line is written
        return False


_UNBUILDABLE_LINE = _build_unbuildable_line()


def run(argv: Sequence[str], *, stdin: BinaryIO | None, result_fd: int, work: Path | None,
        trailing_wait: float = TRAILING_WAIT, startup_error: OSError | None = None) -> int:
    """The importer. The result line is written at EXACTLY ONE place, the ``finally`` below, from a mutable outcome
    that starts as a generic failure; every step only updates it. Any exception (the work, a probe, a close) folds
    into the outcome. KeyboardInterrupt and SystemExit stay outside the contract (they propagate) but the line is
    still attempted on the way out. A line that cannot be written exits 1."""
    progress = Progress()
    out = {"rc": 1, "refused": None, "detail": "importer aborted", "enospc": False, "result": None}
    log: BinaryIO | None = None
    spool: Spool | None = None
    try:
        try:
            if startup_error is not None:  # fd 0 closed, or the cwd gone
                raise Fatal(None, f"startup: {errno.errorcode.get(startup_error.errno or 0, startup_error.errno)}",
                            own_enospc=startup_error.errno in _ENOSPC_ERRNOS)
            assert stdin is not None and work is not None
            try:
                a = _parser().parse_args(list(argv))
            except _Usage as u:
                raise Fatal(None, f"usage: {u}") from None
            if not re.fullmatch(r"[0-9a-f]{%d}" % FORMATS[a.object_format][0], a.base_oid):
                raise Fatal(None, "usage: --base-oid is not an oid of the object format")
            if not SESSION_RE.fullmatch(a.session):
                raise Fatal(None, "usage: --session is not a session id")
            if a.git_log:
                try:
                    log = open(a.git_log, "ab")
                except OSError as e:
                    raise _oserror(e, "--git-log open") from None
            runner = GitRunner(a.git, work, log)
            try:
                spool = Spool(work / "spool" / "frames")
            except OSError as e:
                raise _oserror(e, "spool create") from None
            frames = spool_frames(stdin, spool, progress, payload_cap=a.max_bytes, trailing_wait=trailing_wait)
            ingest_base(runner, spool, frames, object_format=a.object_format, base_oid=a.base_oid,
                        cap=a.max_input_size)
            progress.ignored_git_paths = ingest_objects(runner, spool, frames, object_format=a.object_format,
                                                        cap=a.max_input_size)
            head_oid = import_refs(runner, spool, frames, object_format=a.object_format, session=a.session)
            write_wip(runner, spool, frames, object_format=a.object_format, session=a.session, head_oid=head_oid,
                      base_oid=a.base_oid)
            check_store(runner, session=a.session, session_git=has_session_git(frames))
            result = write_result(runner, frames, progress, object_format=a.object_format, session=a.session,
                                  base_oid=a.base_oid)
            out.update(rc=0, refused=None, detail="", enospc=False, result=result)
        except Fatal as f:
            out.update(rc=1, refused=f.name, detail=f.detail, result=None,
                       enospc=f.own_enospc or (work is not None and _probe_quietly(work)))
        except Exception as e:  # noqa: BLE001 - every failure answers one line
            out.update(rc=1, refused=None, detail=f"{type(e).__name__}: {e}", result=None,
                       enospc=work is not None and _probe_quietly(work))
        # Cleanup can fail too: that folds into the outcome like everything else.
        for closer in (spool.close if spool is not None else None, log.close if log is not None else None):
            if closer is None:
                continue
            try:
                closer()
            except Exception as e:  # noqa: BLE001
                en = getattr(e, "errno", None)
                note = f"cleanup close: {errno.errorcode.get(en or 0, en) if isinstance(e, OSError) else type(e).__name__}"
                out["rc"] = 1
                out["result"] = None
                out["detail"] = f"{out['detail']}; {note}" if out["detail"] else note
                out["enospc"] = bool(out["enospc"]) or en in _ENOSPC_ERRNOS
    finally:
        exc_type = sys.exc_info()[0]
        if exc_type is not None:  # an interrupt or exit is propagating: never a success-shaped line
            out["rc"] = 1
            out["refused"] = None
            out["result"] = None
            out["detail"] = f"importer interrupted: {exc_type.__name__}"
        # A failed run leaves no output: whatever step 6 or a temp got to is unlinked before the line says so.
        if out["rc"] != 0 and work is not None and not _remove_outputs_quietly(work):
            out["detail"] = f"{out['detail']}; an output could not be removed"
        try:
            line = result_line(progress, refused=out["refused"], detail=str(out["detail"]), enospc=bool(out["enospc"]),
                               result=out["result"] if out["rc"] == 0 else None)
        except Exception:  # noqa: BLE001 - the line is owed on every exit; the fallback computes nothing
            line = _UNBUILDABLE_LINE
            out["rc"] = 1
        try:
            write_all(result_fd, line)  # THE one result-line write
        except OSError:
            out["rc"] = 1
            if work is not None:  # no line reached the caller: the run failed, so its outputs go too
                _remove_outputs_quietly(work)
    return int(out["rc"])


def _remove_outputs_quietly(work: Path) -> bool:
    try:
        return remove_outputs(work)
    except Exception:  # noqa: BLE001 - removal never decides whether the line is written
        return False


def main() -> int:
    # fd 0 unbuffered: no read-ahead hides bytes from the trailing check's readiness wait.
    stdin: BinaryIO | None = None
    work: Path | None = None
    err: OSError | None = None
    try:
        stdin = open(0, "rb", buffering=0, closefd=False)
        work = Path(os.getcwd())
    except OSError as e:
        err = e
    return run(sys.argv[1:], stdin=stdin, result_fd=4, work=work, startup_error=err)


if __name__ == "__main__":
    sys.exit(main())
