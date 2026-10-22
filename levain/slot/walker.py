"""Adopt's walker: root reads the retained tree and streams it as ``levain-walk/1`` frames (CONTRACT §10.1).

Root walks but never parses: the tree is walked by END's own :func:`levain.slot.end.walk` (``O_NOFOLLOW`` opens
from the parent fd checked against the ``lstat`` that named the entry, byte-order names, the depth refusal, the
bounded fds), with a ``prune`` decision that keeps only ``repo/`` and ``in/repo.bundle``. Every entry the walk
yields is stream-merged against the retained ``manifest.jsonl`` END wrote from the same walk, so the order is the
same by identity; a pruned subtree's records are skipped in the merge. A regular file is hashed, rewound and
streamed while hashed again, and either pass disagreeing with the manifest abandons the stream with no terminal
frame. A link's target bytes are its payload, never followed. Every other type is a header only, never opened.

Every write to the importer waits in ``poll`` for the one deadline; a reader that stops reading or goes away is
``importer-gone``, never a hang. Every root-only fact (the owner the tree must have, the walk itself, a pause
between a file's two passes) is an injected parameter with a real default.
"""
from __future__ import annotations

import errno
import hashlib
import os
import select
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from levain.contracts import ContractError, canon
from levain.slot import Refused, end, walkframe
from levain.slot.end import Entry, entry_type

#: The retained tree's root children adopt reads; every other root child is pruned.
SCOPE_ROOTS = (walkframe.PRUNE_SCOPE_PREFIX[0], b"in")
#: The one ``in/`` child adopt reads: the base bundle render staged.
BASE_BUNDLE = b"in/repo.bundle"

#: ``st_flags`` bit of a macOS dataless file (``<sys/stat.h>`` ``SF_DATALESS``): its bytes are not local and a read
#: would fetch them, so it is refused, never read.
SF_DATALESS = 0x40000000
#: The mode END gives the empty placeholder it leaves for an ``UNREAD`` file.
UNREAD_PLACEHOLDER_MODE = 0o600

_CHUNK = 1 << 20
#: The preflight reads the walk's clock once before its first record and again every this many records.
_PREFLIGHT_CLOCK_EVERY = 4096
_O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_SETID = stat.S_ISUID | stat.S_ISGID
_FD_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE})
_MANIFEST_KEYS = frozenset({"path_b64", "type", "mode", "size", "nlink", "digest"})
_POLL_BAD = select.POLLERR | select.POLLHUP | getattr(select, "POLLNVAL", 0)

FRAME = "frame"            # walked and framed
OUT = "out"                # outside adopt's scope: no frame, not descended
SKIPPED_GIT = "skipped-git"


def is_git_name(name: bytes) -> bool:
    """A path component that IS ``.git`` to a case-insensitive or normalising filesystem: equal to ``.git`` after
    NFC and casefold (``.GIT``, ``.Git``). A name that is not UTF-8 never folds to it."""
    if name == b".git":
        return True
    try:
        text = name.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return unicodedata.normalize("NFC", text).casefold() == ".git"


def scope(path: bytes) -> str:
    """Where one tree path stands for adopt: :data:`FRAME`, :data:`OUT` or :data:`SKIPPED_GIT`. ``in`` itself is
    framed (as a ``dir``) so the importer sees the base bundle's parent as it sees ``repo/``'s directories. Every
    ``.git`` test is :func:`is_git_name`."""
    parts = path.split(b"/")
    top = parts[0]
    if top not in SCOPE_ROOTS:
        return OUT
    if top == b"in":
        return FRAME if len(parts) == 1 or path == BASE_BUNDLE else OUT
    if len(parts) > 2 and is_git_name(parts[-1]):
        return SKIPPED_GIT
    if len(parts) > 2 and any(is_git_name(p) for p in parts[1:-1]) and not is_git_name(parts[1]):
        # Below a nested .git: unreachable when the walk prunes (a skipped .git is never descended).
        return OUT
    return FRAME


def adopt_prune(e: Entry) -> bool:
    """``end.walk``'s ``prune``: do not descend a directory outside adopt's scope or a nested ``.git``."""
    return scope(e.path) != FRAME


@dataclass(frozen=True)
class WalkCaps:
    """V7's walk caps: payload bytes (logical: a sparse file counts its full size), frames, and the wall-clock
    deadline (``clock()`` value) that bounds the walk and every pipe write."""
    bytes: int
    entries: int
    deadline: float


@dataclass
class WalkSeams:
    #: The uid every walked entry must have (root; END chowned the retained tree to it).
    owner_uid: int = 0
    #: The walk; END's.
    walk: Callable[..., Iterator[Entry]] = end.walk
    #: Called between a regular file's hash pass and its streaming pass, with the entry and the open fd.
    between_passes: Callable[[Entry, int], None] | None = None
    clock: Callable[[], float] = time.monotonic
    note: Callable[[str], None] = field(default=lambda s: None)


@dataclass(frozen=True)
class WalkResult:
    count: int
    bytes: int
    base_digest: str


def escape(path: bytes, cap: int = 200) -> str:
    """A path for a refusal's text: printable ASCII as is, every other byte ``\\xNN`` (no terminal control)."""
    return "".join(chr(b) if 0x20 <= b < 0x7F and b != 0x5C else f"\\x{b:02x}" for b in path[:cap])


def _mismatch(path: bytes, why: str) -> Refused:
    return Refused("walk-manifest-mismatch", f"{escape(path)}: {why}")


def _oserror(err: OSError, where: bytes) -> Refused:
    """The walk's one translation of an ``OSError``: an fd shortage is ``walker-cap:fds``; any other open, reopen
    or listing failure is the tree changing under the walk (the tree is root's, SCRUBBED, so nothing should move)."""
    if err.errno in _FD_ERRNOS:
        return Refused("walker-cap:fds", f"{errno.errorcode.get(err.errno, err.errno)} at {escape(where)}")
    return Refused("tree-changed", f"{errno.errorcode.get(err.errno, err.errno)} at {escape(where)}")


class _Manifest:
    """``manifest.jsonl`` as a peekable stream of validated records (root's own file, read once, in order)."""

    def __init__(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self._fh = os.fdopen(fd, "rb", buffering=_CHUNK)
        self._cap = walkframe.header_cap() + 1024
        self._next: dict | None = None
        self._line = 0

    def close(self) -> None:
        self._fh.close()

    def peek(self) -> dict | None:
        if self._next is None:
            line = self._fh.readline(self._cap + 1)
            if not line:
                return None
            self._line += 1
            if len(line) > self._cap or not line.endswith(b"\n"):
                raise _mismatch(b"", f"manifest line {self._line} past {self._cap} bytes or torn")
            try:
                rec = canon.parse(line[:-1])
            except ContractError as e:
                raise _mismatch(b"", f"manifest line {self._line}: {e}") from None
            if not isinstance(rec, dict) or set(rec) != _MANIFEST_KEYS:
                raise _mismatch(b"", f"manifest line {self._line}: not a manifest record")
            try:
                rec["_path"] = walkframe.check_entry({"v": 1, "path_b64": rec["path_b64"], "type": "dir",
                                                      "mode": 0, "size": 0, "digest": ""})
            except Refused:
                raise _mismatch(b"", f"manifest line {self._line}: bad path_b64") from None
            self._next = rec
        return self._next

    def take(self) -> dict | None:
        rec = self.peek()
        self._next = None
        return rec

    def skip_under(self, path: bytes) -> int:
        """Drop the records of ``path``'s subtree (the walk pruned it); the count dropped."""
        prefix, n = path + b"/", 0
        while True:
            rec = self.peek()
            if rec is None or not rec["_path"].startswith(prefix):
                return n
            self.take()
            n += 1


class _Pipe:
    """Writes to the importer's frame pipe: non-blocking, each wait in ``poll`` bounded by the one deadline."""

    def __init__(self, fd: int, deadline: float, clock: Callable[[], float]) -> None:
        os.set_blocking(fd, False)
        self.fd, self.deadline, self.clock = fd, deadline, clock
        self._poll = select.poll()
        self._poll.register(fd, select.POLLOUT)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            # The wall holds whether or not the importer drains: a reader that keeps up never reaches the poll.
            if self.clock() > self.deadline:
                raise Refused("walker-cap:time", "the walk ran past V7's wall")
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                n = 0
            except BrokenPipeError:
                raise Refused("importer-gone", "the importer closed its frame pipe") from None
            except OSError as err:
                raise Refused("importer-gone", f"{errno.errorcode.get(err.errno, err.errno)} on the frame pipe") \
                    from None
            view = view[n:]
            if not view:
                return
            left = self.deadline - self.clock()
            if left <= 0:
                raise Refused("importer-gone", "the importer stopped reading before the deadline")
            events = self._poll.poll(max(1, int(left * 1000)))
            if not events:
                raise Refused("importer-gone", "the importer stopped reading before the deadline")
            if events[0][1] & _POLL_BAD and not events[0][1] & select.POLLOUT:
                raise Refused("importer-gone", "the importer's frame pipe failed")


def _unread(rec: dict) -> bool:
    """END's ``UNREAD`` sentinel: a file record of size 0 with an empty digest (a real empty file carries the
    sha256 of empty bytes)."""
    return rec["type"] == "file" and rec["size"] == 0 and rec["digest"] == ""


def _check_record(e: Entry, rec: dict | None, t: str) -> dict:
    if rec is None:
        raise _mismatch(e.path, "the walk found an entry the manifest does not hold")
    if rec["_path"] != e.path:
        raise _mismatch(e.path, f"the manifest's next record is {escape(rec['_path'])}")
    if rec["type"] != t:
        raise _mismatch(e.path, f"type {t}, manifest {rec['type']!r}")
    mode, have = rec["mode"], stat.S_IMODE(e.st.st_mode)
    if not isinstance(mode, int) or isinstance(mode, bool):
        raise _mismatch(e.path, f"manifest mode {mode!r}")
    if _unread(rec):
        # END's placeholder for an UNREAD file is an empty 0600 file whatever the mode the manifest recorded.
        if have != UNREAD_PLACEHOLDER_MODE:
            raise _mismatch(e.path, f"an unread placeholder of mode {have:o}")
    elif (mode & ~_SETID) != (have & ~_SETID):
        raise _mismatch(e.path, f"mode {have:o}, manifest {mode:o}")
    size = rec["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _mismatch(e.path, "manifest size")
    if t in ("file", "link") and size != e.st.st_size:
        raise _mismatch(e.path, f"size {e.st.st_size}, manifest {size}")
    if not isinstance(rec["digest"], str):
        raise _mismatch(e.path, "manifest digest")
    return rec


def _check_open(fd: int, e: Entry, rec: dict, owner_uid: int) -> None:
    """The opened fd is still the manifest's file: the walk's inode, regular, root's, the manifest's mode, size
    and link count, and not dataless (a read of a dataless file would fetch it)."""
    st = os.fstat(fd)
    if not end._same(st, e.st) or not stat.S_ISREG(st.st_mode):
        raise Refused("tree-changed", f"{escape(e.path)} changed under the walk")
    if st.st_uid != owner_uid:
        raise Refused("walk-foreign-owner", f"{escape(e.path)} is owned by uid {st.st_uid}")
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        raise Refused("walk-dataless", f"{escape(e.path)} is a dataless file")
    if (stat.S_IMODE(st.st_mode) & ~_SETID) != (rec["mode"] & ~_SETID):
        raise _mismatch(e.path, f"mode {stat.S_IMODE(st.st_mode):o} once open, manifest {rec['mode']:o}")
    if st.st_size != rec["size"]:
        raise _mismatch(e.path, f"size {st.st_size} once open, manifest {rec['size']}")
    if st.st_nlink != rec["nlink"]:
        raise _mismatch(e.path, f"nlink {st.st_nlink}, manifest {rec['nlink']!r}")


def _check_clock(seams: WalkSeams, deadline: float, what: str, path: bytes) -> None:
    if seams.clock() > deadline:
        raise Refused("walker-cap:time", f"{what} {escape(path)}")


def _stream_file(e: Entry, rec: dict, pipe: _Pipe, seams: WalkSeams, deadline: float) -> None:
    """Hash pass (at most ``size + 1`` bytes), rewind, the header, then exactly ``size`` bytes streamed while
    hashed again. The fd's identity is checked against the manifest before the first read and again before the
    second pass is accepted; a read, seek or stat failure is ``tree-changed``."""
    size, want = rec["size"], rec["digest"]
    try:
        fd = os.open(e.name, _O_FILE, dir_fd=e.dirfd)
    except OSError as err:
        raise _oserror(err, e.path) from None
    try:
        _check_open(fd, e, rec, seams.owner_uid)
        h, n = hashlib.sha256(), 0
        while n <= size:
            chunk = os.read(fd, min(_CHUNK, size + 1 - n))
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
            _check_clock(seams, deadline, "hashing", e.path)
        if n != size or "sha256:" + h.hexdigest() != want:
            raise Refused("walk-digest-mismatch", f"{escape(e.path)}: the file is not the manifest's")
        if seams.between_passes is not None:
            seams.between_passes(e, fd)
        _check_clock(seams, deadline, "after the first pass of", e.path)
        os.lseek(fd, 0, os.SEEK_SET)
        pipe.write(walkframe.encode_header(e.path, "file", rec["mode"], size, want))
        h, left = hashlib.sha256(), size
        while left:
            _check_clock(seams, deadline, "streaming", e.path)
            chunk = os.read(fd, min(_CHUNK, left))
            if not chunk:
                raise Refused("walk-digest-mismatch", f"{escape(e.path)}: short read while streaming")
            h.update(chunk)
            left -= len(chunk)
            pipe.write(chunk)
        if "sha256:" + h.hexdigest() != want:
            raise Refused("walk-digest-mismatch", f"{escape(e.path)}: changed between the two passes")
        _check_open(fd, e, rec, seams.owner_uid)
    except OSError as err:
        raise Refused("tree-changed", f"{errno.errorcode.get(err.errno, err.errno)} at {escape(e.path)}") from None
    finally:
        os.close(fd)


def _link_target(e: Entry, rec: dict) -> bytes:
    try:
        target = os.readlink(e.name, dir_fd=e.dirfd)
        again = os.stat(e.name, dir_fd=e.dirfd, follow_symlinks=False)
    except OSError as err:
        raise _oserror(err, e.path) from None
    if not end._same(again, e.st):
        raise Refused("tree-changed", f"{escape(e.path)} changed under the walk")
    if len(target) != rec["size"] or end.digest_bytes(target) != rec["digest"]:
        raise Refused("walk-digest-mismatch", f"{escape(e.path)}: the link target is not the manifest's")
    return target


def _preflight(manifest: Path, deadline: float, clock: Callable[[], float]) -> None:
    """Before the first frame: every manifest path the walk will frame (the walk is merged against the manifest
    path for path, so these are the framed paths) is within the decoder's bound,
    :func:`walkframe.check_path_bound`, else its ``frame-bad:path_b64`` refuses with nothing written. The same
    pruning as the walk: an ``OUT`` path is not framed, and nothing below a directory adopt does not descend.
    V7's wall holds here too: the clock is read before the first record and every
    :data:`_PREFLIGHT_CLOCK_EVERY` records, and past ``deadline`` it refuses ``walker-cap:time``."""
    mf = _Manifest(manifest)
    n = 0
    try:
        while True:
            if n % _PREFLIGHT_CLOCK_EVERY == 0 and clock() > deadline:
                raise Refused("walker-cap:time", f"preflighting the manifest at record {n}")
            if (rec := mf.take()) is None:
                break
            n += 1
            path = rec["_path"]
            where = scope(path)
            if where != FRAME and rec["type"] == "dir":
                mf.skip_under(path)
            if where != OUT:
                walkframe.check_path_bound(path)
    finally:
        mf.close()


def walk_frames(tree: Path, manifest: Path, out_fd: int, *, depth: int, caps: WalkCaps,
                base_bundle_sha256: str, seams: WalkSeams | None = None) -> WalkResult:
    """Stream ``tree`` (the retained ``tree/``) to ``out_fd`` as ``levain-walk/1`` frames, merged against
    ``manifest`` (its ``manifest.jsonl``), walked to ``depth`` END levels; the terminal frame last, written only
    when every entry matched. ``in/repo.bundle``'s digest must equal ``base_bundle_sha256``
    (``base-bundle-mismatch``; absent, not a file, or ``unread`` the same). Raises :class:`Refused` by name with no
    terminal frame written."""
    seams = seams or WalkSeams()
    _preflight(manifest, caps.deadline, seams.clock)
    pipe = _Pipe(out_fd, caps.deadline, seams.clock)
    mf = _Manifest(manifest)
    it = seams.walk(tree, depth_cap=depth, prune=adopt_prune)
    count = total = 0
    base_digest = ""
    where = b""
    try:
        while True:
            try:
                e = next(it)
            except StopIteration:
                break
            except OSError as err:
                raise _oserror(err, where) from None
            where = e.path
            if seams.clock() > caps.deadline:
                raise Refused("walker-cap:time", f"at {escape(e.path)}")
            t = entry_type(e.st.st_mode)
            rec = _check_record(e, mf.take(), t)
            if e.st.st_uid != seams.owner_uid:
                raise Refused("walk-foreign-owner", f"{escape(e.path)} is owned by uid {e.st.st_uid}")
            where_scope = scope(e.path)
            if where_scope != FRAME and t == "dir":
                mf.skip_under(e.path)
            if where_scope == OUT:
                continue
            if getattr(e.st, "st_flags", 0) & SF_DATALESS:
                raise Refused("walk-dataless", f"{escape(e.path)} is a dataless file")
            count += 1
            if count > caps.entries:
                raise Refused("walker-cap:entries", f"more than {caps.entries} entries")
            if where_scope == SKIPPED_GIT:
                pipe.write(walkframe.encode_header(e.path, "skipped-git", rec["mode"], 0, ""))
                continue
            if e.path == BASE_BUNDLE and (t != "file" or rec["digest"] != base_bundle_sha256):
                raise Refused("base-bundle-mismatch", "in/repo.bundle is not the base bundle the alloc recorded")
            if _unread(rec):
                # END's UNREAD sentinel: an outside-linked file no session uid could read, kept as an empty
                # placeholder. Its frame says so and carries nothing.
                if e.st.st_size != 0:
                    raise _mismatch(e.path, "an unread placeholder that is not empty")
                pipe.write(walkframe.encode_header(e.path, "unread", rec["mode"], 0, ""))
                continue
            if t == "file":
                if total + rec["size"] > caps.bytes:
                    raise Refused("walker-cap:bytes", f"past {caps.bytes} bytes at {escape(e.path)}")
                _stream_file(e, rec, pipe, seams, caps.deadline)
                total += rec["size"]
                if e.path == BASE_BUNDLE:
                    base_digest = rec["digest"]
            elif t == "link":
                if total + rec["size"] > caps.bytes:
                    raise Refused("walker-cap:bytes", f"past {caps.bytes} bytes at {escape(e.path)}")
                target = _link_target(e, rec)
                pipe.write(walkframe.encode_header(e.path, "link", rec["mode"], len(target), rec["digest"]))
                pipe.write(target)
                total += len(target)
            else:
                if rec["size"] != 0 or rec["digest"] != "":
                    raise _mismatch(e.path, f"a {t} with a size or digest")
                pipe.write(walkframe.encode_header(e.path, t, rec["mode"], 0, ""))
        left = mf.take()
        if left is not None:
            raise _mismatch(left["_path"], "the manifest holds an entry the walk did not find")
        if not base_digest:
            raise Refused("base-bundle-mismatch", "no in/repo.bundle in the retained tree")
        _check_clock(seams, caps.deadline, "before the terminal frame of", b"")
        pipe.write(walkframe.encode_terminal(count, total))
    finally:
        it.close()
        mf.close()
    return WalkResult(count, total, base_digest)


__all__ = ["BASE_BUNDLE", "SF_DATALESS", "WalkCaps", "WalkResult", "WalkSeams", "adopt_prune", "escape", "scope",
           "walk_frames"]
