"""END: the path that ends a session and RETAINS its work (CONTRACT §7.3-7.5, V1 V2 V6 V12 V14 V17).

``REVOKED -> FROZEN -> DEAD -> CREDENTIAL_PARKED -> RETAINED``. Ending a session never deletes its
work (plan §2f): the session tree is renamed, whole, into ``L/retained/<session>/tree/`` after its
manifest and metadata are durable, then neutralized in place (flags, ACLs and xattrs stripped, owner
root). Any step that is not PROVEN moves the record to HELD with ``failed_step``; the tree is never
touched by a failure path, and a retry resumes idempotently.

Root acts only on alloc facts, its own intent journal and a live check (CONTRACT §7.1): every path
here comes from :class:`EndFacts`, built from the alloc record, never from the state record. The
state record's ``state`` / ``failed_step`` (the reaper's read, §7.7 step 3) only choose which CAS
edges :func:`end_session` sends; every proof (freeze, kill, no process left) and every idempotent act
runs on every call, so a forged state can make root send a wrong edge (CONTROL refuses it), never
skip a proof or touch a tree it should not.

Every root-only act is an injected seam with a real default: the freeze and kill (2d / 2g build
them; the defaults here refuse, which lands HELD), ``chown``, the metadata reader and strip, the
clock, CONTROL's ``cas``. The manifest walk is real code over dirfds with ``O_NOFOLLOW``.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import hashlib
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

from levain.contracts import canon
from levain.slot import Refused, write_all
from levain.slot.ids import SlotId
from levain.slot.layout import RESERVE_BYTES
# The §7.3 states END sends; records owns the names (the full set and its legal edges).
from levain.slot.records import CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED
from levain.slot.root_intent import (INTENT_PHASES, Cas, Fault, Phase, PhaseFailed, Plan, RootIntent,
                                     atomic_write, begin, fsync_dir, intent_path, read_root_intent,
                                     reconcile)

# --- values (CONTRACT §11) -------------------------------------------------------------------------

V1_INODES = 2_000_000
V2_SECONDS = 600.0
V5_FLOOR_BYTES = 8 << 30
V12_SECONDS = 10.0
V14_BYTES = 64 << 10
V17_MANIFEST_BYTES = 512 << 20
V17_META_BYTES = 64 << 20
# DECISION: the contract fixes no cap for copying a file hard-linked to a path outside the tree
# (plan §2h 8 asks only that it be known before any mutation). V4's SIGKILL-row tree bytes is the
# order of magnitude a session's own work is sized at; refused as the manifest budget past it.
LINK_COPY_BYTES = 2 << 30

MANIFEST_FORMAT = "levain-end-manifest/1"
META_FORMAT = "levain-retention-meta/1"
OBJECT_FORMAT = "levain-retained-object/1"


#: The vendor credential files (plan §2e 11): (dir under the session tree, file name), parked in
#: ``L/slots/e<n>/keep/`` under the same name.
CREDENTIAL_FILES: tuple[tuple[tuple[bytes, ...], bytes], ...] = (
    ((b"config", b"claude"), b".credentials.json"),
    ((b"config", b"codex"), b"auth.json"),
)
VAR_FOLDERS_NAME = b".levain-var-folders"     # CONTRACT §7.4: macOS /private/var/folders/<slot>

#: The refusals that mean "past the manifest budget": HELD ``failed_step:"manifest_budget"``, whose
#: exit is END again with larger caps (§7.3 force-retire a).
BUDGET_REFUSALS = frozenset({"manifest-inode-cap", "manifest-time-cap", "manifest-byte-cap", "meta-byte-cap",
                             "link-copy-cap"})

_CHUNK = 1 << 20


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _root_owned(st: os.stat_result) -> bool:
    return st.st_uid == 0


def _no_fault(point: str) -> None:
    return None


# --- caps ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Caps:
    """The manifest budget (V1 inodes, V2 seconds, V17 manifest + meta bytes) and the outside-link copy
    cap. ``force-retire`` (a) is END again with a larger one."""

    inodes: int = V1_INODES
    seconds: float = V2_SECONDS
    manifest_bytes: int = V17_MANIFEST_BYTES
    meta_bytes: int = V17_META_BYTES
    link_copy_bytes: int = LINK_COPY_BYTES

    def scaled(self, factor: float) -> "Caps":
        return Caps(int(self.inodes * factor), self.seconds * factor, int(self.manifest_bytes * factor),
                    int(self.meta_bytes * factor), int(self.link_copy_bytes * factor))


# --- facts (from the alloc record only) ------------------------------------------------------------


@dataclass(frozen=True)
class EndFacts:
    """Everything END needs about a session, from its alloc record (CONTRACT §7.1) and the store root.
    ``var_folders``: macOS ``/private/var/folders/<slot>`` as the caller derives it from the slot uid
    (the alloc record does not carry it), ``None`` on Linux."""

    session: str
    slot: SlotId
    slot_uid: int
    slot_gid: int
    store: Path
    var_folders: Path | None = None

    @property
    def live(self) -> Path:
        return self.store / "live" / self.session

    @property
    def retained(self) -> Path:
        return self.store / "retained" / self.session

    @property
    def tree(self) -> Path:
        return self.retained / "tree"

    @property
    def alloc_dir(self) -> Path:
        return self.store / "alloc"

    @property
    def intent(self) -> Path:
        return intent_path(self.alloc_dir, self.session)

    @property
    def keep(self) -> Path:
        return self.slot.slot_dir(self.store) / "keep"

    @property
    def extra(self) -> dict[bytes, Path]:
        return {VAR_FOLDERS_NAME: self.var_folders} if self.var_folders is not None else {}


def _is_id(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def facts_from_alloc(alloc: Mapping[str, object], *, store: Path, var_folders: Path | None = None) -> EndFacts:
    """The END facts of a parsed ``levain-alloc/1`` record: ``session``, ``slot{class,index}``,
    ``slot_uid``, ``slot_gid``. Anything else in it is not END's."""
    try:
        slot = alloc["slot"]
        facts = EndFacts(alloc["session"], SlotId(slot["class"], slot["index"]),  # type: ignore[index]
                         alloc["slot_uid"], alloc["slot_gid"], Path(store), var_folders)  # type: ignore[arg-type]
    except (KeyError, TypeError) as e:
        raise Refused("alloc-malformed", f"END facts: {e}") from None
    if not _is_id(facts.slot_uid) or not _is_id(facts.slot_gid):
        raise Refused("alloc-malformed", "slot_uid / slot_gid")
    facts.intent   # validates the session id
    return facts


# --- the walk --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One tree entry. ``dirfd`` + ``name`` address it without following a link (``dirfd`` is -1 for
    an ``extra`` root child, addressed by ``abspath``); both are valid only while the walk is at it."""

    path: bytes
    st: os.stat_result
    dirfd: int
    name: bytes
    abspath: bytes


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_dir_at(name: bytes, dirfd: int, st: os.stat_result) -> int:
    fd = os.open(name, _DIR_FLAGS, dir_fd=dirfd) if dirfd >= 0 else os.open(name, _DIR_FLAGS)
    if not _same(os.fstat(fd), st):
        os.close(fd)
        raise Refused("tree-changed", f"{name!r} changed under the walk")
    return fd


def walk(root: Path, extra: Mapping[bytes, Path] | None = None) -> Iterator[Entry]:
    """Every entry under ``root`` (not ``root`` itself), depth first, a directory before its children,
    names in byte order; never follows a link (each directory opened ``O_NOFOLLOW`` from its parent's
    fd and checked against the ``lstat`` that named it). ``extra`` adds root children that live
    elsewhere (macOS ``/var/folders/<slot>``, walked as ``.levain-var-folders`` before it is moved
    there); an absent one is skipped, a present one must be a real directory, and a name the tree
    already has is ``retain-conflict``."""
    extra = dict(extra or {})
    root_b = os.fsencode(root)
    root_fd = os.open(root_b, _DIR_FLAGS)
    stack: list[list] = []
    try:
        names = [os.fsencode(n) for n in os.listdir(root_fd)]
        clash = set(names) & set(extra)
        if clash:
            raise Refused("retain-conflict", f"{sorted(clash)!r} already in the tree")
        live_extra = {}
        for k, p in extra.items():
            try:
                pst = os.lstat(p)
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(pst.st_mode):
                raise Refused("var-folders-not-dir", str(p))
            live_extra[k] = (os.fsencode(p), pst)
        stack.append([root_fd, b"", root_b, sorted(names + list(live_extra)), 0, live_extra])
        while stack:
            frame = stack[-1]
            fd, prefix, absprefix, names, i, fextra = frame
            if i == len(names):
                os.close(fd)
                stack.pop()
                continue
            frame[4] = i + 1
            name = names[i]
            path = prefix + name
            if name in fextra:
                absp, st = fextra[name]
                yield Entry(path, st, -1, name, absp)
                child = _open_dir_at(absp, -1, st)
            else:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                absp = absprefix + b"/" + name
                yield Entry(path, st, fd, name, absp)
                if not stat.S_ISDIR(st.st_mode):
                    continue
                child = _open_dir_at(name, fd, st)
            try:
                cnames = sorted(os.fsencode(n) for n in os.listdir(child))
            except BaseException:
                os.close(child)
                raise
            stack.append([child, path + b"/", absp, cnames, 0, {}])
    finally:
        for frame in stack:
            try:
                os.close(frame[0])
            except OSError:
                pass


def entry_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    raise Refused("tree-unknown-type", f"mode {mode:o}")


# --- the manifest (levain-end-manifest/1) ----------------------------------------------------------


@dataclass
class LinkCount:
    nlink: int
    seen: int
    size: int


class _Budget:
    def __init__(self, caps: Caps, clock: Callable[[], float]) -> None:
        self.caps, self.clock, self.t0, self.entries = caps, clock, clock(), 0

    def tick(self) -> None:
        if self.clock() - self.t0 > self.caps.seconds:
            raise Refused("manifest-time-cap", f"hashing passed {self.caps.seconds:g} s")

    def entry(self) -> None:
        self.entries += 1
        if self.entries > self.caps.inodes:
            raise Refused("manifest-inode-cap", f"more than {self.caps.inodes} entries")
        self.tick()


def _hash_file(e: Entry, budget: _Budget) -> tuple[int, str]:
    fd = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=e.dirfd)
    try:
        if not _same(os.fstat(fd), e.st):
            raise Refused("tree-changed", f"{e.path!r} changed under the walk")
        h, n = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
            budget.tick()
    finally:
        os.close(fd)
    return n, "sha256:" + h.hexdigest()


def _credential_paths() -> frozenset[bytes]:
    return frozenset(b"/".join(d + (n,)) for d, n in CREDENTIAL_FILES)


def iter_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic,
                  links: dict[tuple[int, int], LinkCount] | None = None,
                  skip_credentials: bool = False) -> Iterator[dict]:
    """The manifest records of the tree, in walk order: ``{path_b64, type, mode, size, nlink, digest}``
    (``mode`` = the permission bits, ``stat.S_IMODE``; a file's digest is over its bytes, a link's over
    its ``readlink`` target bytes; a dir / fifo / socket / device: size 0, digest ""). The inode and
    time caps refuse by name mid-walk. ``links`` collects, for every file with ``nlink > 1``, how
    many of its links the walk saw (the hardlink accounting done before any mutation, plan §2i 4).
    A credential file still at its vendor path refuses ``credential-not-parked`` (END parks first, so
    the manifest never holds it, plan §2g 3); ``skip_credentials`` leaves it out instead, for a
    snapshot of a live tree that still holds one."""
    budget = _Budget(caps, clock)
    creds = _credential_paths()
    for e in walk(root, extra):
        t = entry_type(e.st.st_mode)
        if e.path in creds and t != "dir":
            if skip_credentials:
                continue
            raise Refused("credential-not-parked", e.path.decode("utf-8", "replace"))
        budget.entry()
        size, digest = 0, ""
        if t == "file":
            size, digest = _hash_file(e, budget)
            if e.st.st_nlink > 1 and links is not None:
                lc = links.setdefault((e.st.st_dev, e.st.st_ino), LinkCount(e.st.st_nlink, 0, size))
                lc.seen += 1
        elif t == "link":
            target = os.readlink(e.name, dir_fd=e.dirfd)
            size, digest = len(target), digest_bytes(target)
        # DECISION: a directory's st_nlink is filesystem bookkeeping (APFS counts every entry, ext4 only
        # subdirectories) and drops when the credential is parked out of it; recorded as 1, so the
        # retained tree's manifest equals one taken before the freeze.
        yield {"path_b64": _b64(e.path), "type": t, "mode": stat.S_IMODE(e.st.st_mode), "size": size,
               "nlink": 1 if t == "dir" else e.st.st_nlink, "digest": digest}


def tree_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic, skip_credentials: bool = False) -> list[dict]:
    """The whole manifest in memory (a pre-freeze snapshot; END streams it to disk)."""
    return list(iter_manifest(root, extra=extra, caps=caps, clock=clock, skip_credentials=skip_credentials))


def outside_link_bytes(links: Mapping[tuple[int, int], LinkCount]) -> int:
    """Bytes END will copy: every link to an inode some of whose links lie outside the tree."""
    return sum(lc.size * lc.seen for lc in links.values() if lc.seen < lc.nlink)


def _stream_jsonl(path: Path, records: Iterator[dict], cap: int, cap_name: str) -> None:
    """``records`` as canonical JSONL at ``path``, durable (temp + fsync + rename + fsync(dir)); past
    ``cap`` bytes refuses ``cap_name`` with the temp removed and ``path`` untouched."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    ok = False
    try:
        with os.fdopen(fd, "wb", buffering=_CHUNK) as fh:
            total = 0
            for rec in records:
                line = canon.canonical_bytes(rec) + b"\n"
                total += len(line)
                if total > cap:
                    raise Refused(cap_name, f"{path.name} passed {cap} bytes")
                fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        ok = True
    finally:
        if not ok:
            tmp.unlink(missing_ok=True)
    os.replace(tmp, path)
    fsync_dir(path.parent)


# --- metadata (levain-retention-meta/1): per-OS readers and strips ---------------------------------

MetaReader = Callable[[bytes, os.stat_result, Callable[[str], None]], dict]
Strip = Callable[[bytes, os.stat_result, Callable[[str], None]], None]

_XATTR_NOFOLLOW = 0x0001
_ACL_TYPE_EXTENDED = 0x00000100
_libc = None


def _darwin_libc():
    global _libc
    if _libc is None:
        c = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        c.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        c.listxattr.restype = ctypes.c_ssize_t
        c.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                               ctypes.c_uint32, ctypes.c_int]
        c.getxattr.restype = ctypes.c_ssize_t
        c.removexattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        c.removexattr.restype = ctypes.c_int
        c.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
        c.acl_get_link_np.restype = ctypes.c_void_p
        c.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        c.acl_to_text.restype = ctypes.c_void_p
        c.acl_free.argtypes = [ctypes.c_void_p]
        c.acl_init.argtypes = [ctypes.c_int]
        c.acl_init.restype = ctypes.c_void_p
        c.acl_set_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
        c.acl_set_link_np.restype = ctypes.c_int
        _libc = c
    return _libc


def _oserr(what: str) -> OSError:
    n = ctypes.get_errno()
    return OSError(n, f"{what}: {os.strerror(n)}")


def _darwin_xattr_names(path: bytes) -> list[bytes]:
    c = _darwin_libc()
    for _ in range(4):   # the list can grow between the size call and the read
        n = c.listxattr(path, None, 0, _XATTR_NOFOLLOW)
        if n < 0:
            raise _oserr("listxattr")
        if n == 0:
            return []
        buf = ctypes.create_string_buffer(n)
        m = c.listxattr(path, buf, n, _XATTR_NOFOLLOW)
        if m >= 0:
            return [x for x in buf.raw[:m].split(b"\0") if x]
        if ctypes.get_errno() != errno.ERANGE:
            raise _oserr("listxattr")
    raise OSError(errno.ERANGE, "listxattr kept growing")


def _darwin_getxattr(path: bytes, name: bytes) -> bytes:
    c = _darwin_libc()
    n = c.getxattr(path, name, None, 0, 0, _XATTR_NOFOLLOW)
    if n < 0:
        raise _oserr("getxattr")
    buf = ctypes.create_string_buffer(max(n, 1))
    m = c.getxattr(path, name, buf, n, 0, _XATTR_NOFOLLOW)
    if m < 0:
        raise _oserr("getxattr")
    return buf.raw[:m]


def _darwin_acl_text(path: bytes) -> str | None:
    c = _darwin_libc()
    acl = c.acl_get_link_np(path, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in (errno.ENOENT, errno.EOPNOTSUPP, errno.EINVAL, 0):
            return None
        raise _oserr("acl_get_link_np")
    try:
        ln = ctypes.c_ssize_t()
        txt = c.acl_to_text(acl, ctypes.byref(ln))
        if not txt:
            raise _oserr("acl_to_text")
        try:
            return ctypes.string_at(txt, ln.value).decode("utf-8")
        finally:
            c.acl_free(txt)
    finally:
        c.acl_free(acl)


def _xattrs_record(names: list[bytes], get: Callable[[bytes], bytes], where: str,
                   note: Callable[[str], None]) -> list[dict]:
    out = []
    for nm in sorted(names):
        try:
            sname = nm.decode("utf-8")
        except UnicodeDecodeError:
            note(f"meta {where}: xattr name {nm!r} is not UTF-8, not recorded")
            continue
        try:
            out.append({"name": sname, "value_b64": _b64(get(nm))})
        except OSError as e:
            note(f"meta {where}: xattr {sname} unreadable ({e.strerror or e}), not recorded")
    return out


def darwin_meta(path: bytes, st: os.stat_result, note: Callable[[str], None]) -> dict:
    """macOS: the extended ACL (``acl_to_text``), the BSD flags (``uchg`` included), and every xattr
    (``com.apple.FinderInfo``, ``com.apple.ResourceFork`` included), none following a link. What
    cannot be read is noted and left out, never a wedge (plan §2i 4)."""
    rec: dict = {}
    where = path.decode("utf-8", "replace")
    try:
        acl = _darwin_acl_text(path)
        if acl:
            rec["acl"] = acl
    except OSError as e:
        note(f"meta {where}: ACL unreadable ({e.strerror or e})")
    flags = getattr(st, "st_flags", 0)
    if flags:
        rec["flags"] = flags
    try:
        names = _darwin_xattr_names(path)
    except OSError as e:
        note(f"meta {where}: xattrs unlistable ({e.strerror or e})")
        names = []
    xs = _xattrs_record(names, lambda nm: _darwin_getxattr(path, nm), where, note)
    if xs:
        rec["xattrs"] = xs
    return rec


def linux_meta(path: bytes, st: os.stat_result, note: Callable[[str], None]) -> dict:
    """Linux: every xattr (POSIX ACLs are the ``system.posix_acl_*`` xattrs), not following a link."""
    where = path.decode("utf-8", "replace")
    try:
        names = [os.fsencode(n) for n in os.listxattr(path, follow_symlinks=False)]
    except OSError as e:
        if e.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return {}
        note(f"meta {where}: xattrs unlistable ({e.strerror or e})")
        return {}
    xs = _xattrs_record(names, lambda nm: os.getxattr(path, nm, follow_symlinks=False), where, note)
    return {"xattrs": xs} if xs else {}


def darwin_strip(path: bytes, st: os.stat_result, note: Callable[[str], None]) -> None:
    """Flags first (``uchg`` blocks the rest), then the ACL (an empty one set), then each xattr. A
    failure is noted and the strip carries on: it is hygiene, not a gate (plan §2i 4)."""
    where = path.decode("utf-8", "replace")
    c = _darwin_libc()
    if getattr(st, "st_flags", 0):
        try:
            os.chflags(path, 0, follow_symlinks=False)
        except OSError as e:
            note(f"strip {where}: flags not cleared ({e.strerror or e})")
    try:
        if _darwin_acl_text(path):
            empty = c.acl_init(1)
            try:
                if c.acl_set_link_np(path, _ACL_TYPE_EXTENDED, empty) != 0:
                    note(f"strip {where}: ACL not cleared ({_oserr('acl_set_link_np').strerror})")
            finally:
                c.acl_free(empty)
    except OSError as e:
        note(f"strip {where}: ACL unreadable ({e.strerror or e})")
    try:
        names = _darwin_xattr_names(path)
    except OSError as e:
        note(f"strip {where}: xattrs unlistable ({e.strerror or e})")
        return
    for nm in names:
        if c.removexattr(path, nm, _XATTR_NOFOLLOW) != 0:
            note(f"strip {where}: xattr {nm!r} not removed ({_oserr('removexattr').strerror})")


def linux_strip(path: bytes, st: os.stat_result, note: Callable[[str], None]) -> None:
    where = path.decode("utf-8", "replace")
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as e:
        if e.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
            note(f"strip {where}: xattrs unlistable ({e.strerror or e})")
        return
    for nm in names:
        try:
            os.removexattr(path, nm, follow_symlinks=False)
        except OSError as e:
            note(f"strip {where}: xattr {nm!r} not removed ({e.strerror or e})")


def default_meta_reader() -> MetaReader:
    return darwin_meta if sys.platform == "darwin" else linux_meta


def default_strip() -> Strip:
    return darwin_strip if sys.platform == "darwin" else linux_strip


def iter_meta(root: Path, *, extra: Mapping[bytes, Path] | None = None, reader: MetaReader,
              note: Callable[[str], None]) -> Iterator[dict]:
    """``{path_b64, acl?, flags?, xattrs?}`` for every entry that carries any of the three.
    DECISION: an entry with none gets no line, so meta stays within V17's 64 MiB beside a full
    manifest."""
    for e in walk(root, extra):
        rec = reader(e.abspath, e.st, note)
        if rec:
            yield {"path_b64": _b64(e.path), **rec}


# --- chown + the outside-link copy -----------------------------------------------------------------

Chown = Callable[[int, bytes, int, int], None]


def os_chown(dirfd: int, name: bytes, uid: int, gid: int) -> None:
    """``fchownat(dirfd, name, uid, gid, AT_SYMLINK_NOFOLLOW)``: a link's own owner, never its target's."""
    os.chown(name, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def _count_links(tree: Path) -> dict[tuple[int, int], LinkCount]:
    links: dict[tuple[int, int], LinkCount] = {}
    for e in walk(tree):
        if stat.S_ISREG(e.st.st_mode) and e.st.st_nlink > 1:
            lc = links.setdefault((e.st.st_dev, e.st.st_ino), LinkCount(e.st.st_nlink, 0, e.st.st_size))
            lc.seen += 1
    return links


def _copy_over(e: Entry, tmp_dir_fd: int, tmp_name: bytes) -> None:
    """Replace the link ``e`` by a fresh file holding its bytes (root never chowns a shared inode, plan
    §2g 2). The copy is made in the retained dir and renamed over the link; setuid/setgid are not
    carried onto a root-owned copy (DECISION)."""
    src = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=e.dirfd)
    try:
        if not _same(os.fstat(src), e.st):
            raise Refused("tree-changed", f"{e.path!r}")
        try:
            os.unlink(tmp_name, dir_fd=tmp_dir_fd)
        except FileNotFoundError:
            pass
        dst = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                      dir_fd=tmp_dir_fd)
        try:
            while True:
                chunk = os.read(src, _CHUNK)
                if not chunk:
                    break
                write_all(dst, chunk)
            os.fchmod(dst, stat.S_IMODE(e.st.st_mode) & ~(stat.S_ISUID | stat.S_ISGID))
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)
    os.rename(tmp_name, e.name, src_dir_fd=tmp_dir_fd, dst_dir_fd=e.dirfd)
    os.fsync(e.dirfd)


# --- seams -----------------------------------------------------------------------------------------


def unbuilt_freeze(uid: int, bound_s: float) -> bool:
    """Freeze every process of ``uid`` within ``bound_s`` (V12) and return whether that is PROVEN.
    Algorithm (plan §2e 4, built by 2d / 2g): Linux writes ``1`` to the session cgroup's
    ``cgroup.freeze`` and polls ``cgroup.events`` for ``frozen 1``; macOS, in a forked child that drops
    to ``uid`` and asserts real + effective ids == ``uid`` != 0, sends ``kill(-1, SIGSTOP)`` in a loop
    until a process sweep of ``uid`` shows every process stopped. Past the bound = not proven."""
    raise Refused("seam-unbuilt", "freeze is session 2d / 2g's")


def unbuilt_kill(uid: int, bound_s: float) -> bool:
    """SIGKILL every process of ``uid`` within ``bound_s`` (V12): Linux ``cgroup.kill``; macOS the same
    forked child as the freeze, ``kill(-1, SIGKILL)`` until the sweep is empty (plan §2e 4)."""
    raise Refused("seam-unbuilt", "kill is session 2d / 2g's")


def unbuilt_no_process(uid: int) -> bool:
    """The proof END rests on: no process of ``uid`` exists (Linux: the session cgroup's
    ``cgroup.procs`` empty AND no ``/proc/<pid>`` with that uid; macOS: a ``proc_listallpids`` sweep)."""
    raise Refused("seam-unbuilt", "the process sweep is session 2d / 2g's")


@dataclass
class EndSeams:
    cas: Cas
    freeze: Callable[[int, float], bool] = unbuilt_freeze
    kill: Callable[[int, float], bool] = unbuilt_kill
    no_process: Callable[[int], bool] = unbuilt_no_process
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], str] = utc_now
    meta_reader: MetaReader = field(default_factory=default_meta_reader)
    strip: Strip = field(default_factory=default_strip)
    chown: Chown = os_chown
    root_uid: int = 0
    root_gid: int = 0
    fault: Fault = _no_fault
    note: Callable[[str], None] = lambda msg: None
    reserve: "Reserve | None" = None
    owner_ok: Callable[[os.stat_result], bool] = _root_owned

    @property
    def retry(self) -> Callable[[Callable[[], None]], None]:
        return self.reserve.retry if self.reserve is not None else (lambda fn: fn())


# --- the retain intent's steps ---------------------------------------------------------------------


def _init_step(facts: EndFacts, seams: EndSeams) -> None:
    """A fresh ``L/retained/<s>/`` (root 0700). Redone after a crash: an existing one is accepted only
    when it is a real directory with the right owner holding no ``tree`` yet."""
    try:
        os.mkdir(facts.retained, 0o700)
        fsync_dir(facts.retained.parent)
    except FileExistsError:
        st = os.lstat(facts.retained)
        if not stat.S_ISDIR(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{facts.retained} is not a directory with the expected owner") from None
        if os.path.lexists(facts.tree):
            raise Refused("retained-exists", str(facts.tree)) from None
    os.chmod(facts.retained, 0o700)   # a real directory, checked above or just made
    if not os.path.isdir(facts.live):
        raise Refused("live-missing", str(facts.live))


def _manifest_step(facts: EndFacts, seams: EndSeams, caps: Caps) -> None:
    links: dict[tuple[int, int], LinkCount] = {}
    _stream_jsonl(facts.retained / "manifest.jsonl",
                  iter_manifest(facts.live, extra=facts.extra, caps=caps, clock=seams.clock, links=links),
                  caps.manifest_bytes, "manifest-byte-cap")
    copy = outside_link_bytes(links)
    if copy > caps.link_copy_bytes:
        (facts.retained / "manifest.jsonl").unlink()
        raise Refused("link-copy-cap", f"{copy} bytes hard-linked outside the tree > {caps.link_copy_bytes}")
    seams.fault("retain:manifest:written")


def _meta_step(facts: EndFacts, seams: EndSeams, caps: Caps) -> None:
    _stream_jsonl(facts.retained / "meta.jsonl",
                  iter_meta(facts.live, extra=facts.extra, reader=seams.meta_reader, note=seams.note),
                  caps.meta_bytes, "meta-byte-cap")


def _read_durable(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def object_record(facts: EndFacts, created_at: str) -> dict:
    """``levain-retained-object/1`` from the two durable inputs alone, so a redo writes the same bytes.
    ``entries`` = manifest lines; ``bytes`` = the sum of their ``size``; ``created_at`` = the retain
    journal's ``started_at`` (DECISION: deterministic across a redo)."""
    manifest = _read_durable(facts.retained / "manifest.jsonl")
    meta = _read_durable(facts.retained / "meta.jsonl")
    entries = total = 0
    for line in manifest.splitlines():
        rec = canon.parse(line)
        entries += 1
        total += rec["size"]   # type: ignore[index]
    return {"format": OBJECT_FORMAT, "session": facts.session, "manifest_digest": digest_bytes(manifest),
            "meta_digest": digest_bytes(meta), "entries": entries, "bytes": total, "created_at": created_at}


def _object_step(facts: EndFacts, created_at: str) -> None:
    atomic_write(facts.retained / "object.json", canon.canonical_bytes(object_record(facts, created_at)), 0o600)


def _rename_durable(src: Path, dst: Path) -> None:
    os.rename(src, dst)
    fsync_dir(src.parent)
    fsync_dir(dst.parent)


def _tree_step(facts: EndFacts, seams: EndSeams) -> None:
    """Rename the session's work into ``tree/`` (and macOS ``/var/folders/<slot>`` into
    ``tree/.levain-var-folders/``), replace outside-linked files by copies, strip, then chown root.
    Each sub-step is idempotent; the slot uid is proven dead, so nothing changes under the walks."""
    tree = facts.tree
    if os.path.lexists(facts.live):
        if os.path.lexists(tree):
            raise Refused("retain-conflict", f"{facts.live} and {tree} both exist")
        _rename_durable(facts.live, tree)
    elif not (os.path.isdir(tree) and not os.path.islink(tree)):
        raise Refused("tree-missing", str(tree))
    seams.fault("retain:tree:renamed")

    vf = facts.var_folders
    dest = tree / os.fsdecode(VAR_FOLDERS_NAME)
    if vf is not None and os.path.lexists(vf):
        if os.path.lexists(dest):
            raise Refused("retain-conflict", f"{vf} and {dest} both exist")
        st = os.lstat(vf)
        if getattr(st, "st_flags", 0):   # an immutable dir cannot be renamed; its flags are in meta
            try:
                os.chflags(vf, 0, follow_symlinks=False)
            except OSError as e:
                seams.note(f"strip {vf}: flags not cleared ({e.strerror or e})")
        _rename_durable(vf, dest)
    seams.fault("retain:tree:var-folders")

    # DECISION: the outside-link copy runs before the strip (§7.4 names rename, strip, fchown), so
    # the strip never clears flags or xattrs on an inode a path outside the tree still shares.
    links = _count_links(tree)
    tmp_fd = os.open(facts.retained, _DIR_FLAGS)
    try:
        for e in walk(tree):
            lc = links.get((e.st.st_dev, e.st.st_ino)) if stat.S_ISREG(e.st.st_mode) else None
            if lc is not None and lc.seen < lc.nlink:
                try:
                    _copy_over(e, tmp_fd, b".linkcopy.tmp")
                except OSError as err:
                    seams.note(f"link {e.abspath!r}: not copied ({err.strerror or err}); left unstripped, unchowned")
    finally:
        os.close(tmp_fd)
    seams.fault("retain:tree:links")

    shared = {k for k, lc in _count_links(tree).items() if lc.seen < lc.nlink}
    for e in walk(tree):
        if (e.st.st_dev, e.st.st_ino) not in shared:
            seams.strip(e.abspath, e.st, seams.note)
    seams.fault("retain:tree:stripped")

    rfd = os.open(facts.retained, _DIR_FLAGS)
    try:
        seams.chown(rfd, b"tree", seams.root_uid, seams.root_gid)
    finally:
        os.close(rfd)
    for e in walk(tree):
        if (e.st.st_dev, e.st.st_ino) not in shared:
            seams.chown(e.dirfd, e.name, seams.root_uid, seams.root_gid)
    fsync_dir(tree)


def _commit_step(facts: EndFacts, seams: EndSeams) -> None:
    """Root's ``retained_at`` (0644; the dispatch retention age clock, §3.5), written once: a redo
    keeps the first one, so HELD or crash time never moves it."""
    p = facts.retained / "retained_at"
    if not os.path.lexists(p):
        atomic_write(p, (seams.now() + "\n").encode("ascii"), 0o644)


def retain_plan(facts: EndFacts, seams: EndSeams, ri: RootIntent | None = None, *, caps: Caps = Caps(),
                started_at: str | None = None) -> Plan:
    created = ri.started_at if ri is not None else started_at
    if created is None:
        raise ValueError("retain_plan needs the journal or its started_at")

    def fields() -> dict:
        obj = canon.parse(_read_durable(facts.retained / "object.json"))
        return {"manifest": {"digest": obj["manifest_digest"], "entries": obj["entries"],  # type: ignore[index]
                             "bytes": obj["bytes"]}}                                     # type: ignore[index]

    return Plan("retain", (
        Phase("init", lambda: _init_step(facts, seams)),
        Phase("manifest", lambda: _manifest_step(facts, seams, caps)),
        Phase("meta", lambda: _meta_step(facts, seams, caps)),
        Phase("object", lambda: _object_step(facts, created)),
        Phase("tree", lambda: _tree_step(facts, seams)),
        Phase("commit", lambda: _commit_step(facts, seams)),
    ), fields)


# --- park (the credential, plan §2e 11, §2f 7) -----------------------------------------------------


def _open_subdir(base: Path, parts: tuple[bytes, ...]) -> int | None:
    """``base`` then each part, ``O_NOFOLLOW`` from its parent's fd; ``None`` when a part is absent or
    not a real directory (a slot-made link is never followed)."""
    fd = os.open(base, _DIR_FLAGS)
    for part in parts:
        try:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
                return None
            raise
        os.close(fd)
        fd = nfd
    return fd


def park_move(facts: EndFacts) -> None:
    """Move each vendor credential file to ``keep/`` by rename, never opening it. Only a non-directory
    is moved (a directory at that path is ordinary work, retained); absent = already parked or never
    made. keep/ (root 0700, install's) is made if it is missing (DECISION)."""
    if facts.slot.slot_class != "egress" or not os.path.isdir(facts.live):
        return
    try:
        os.mkdir(facts.keep, 0o700)
    except FileExistsError:
        pass
    kfd = os.open(facts.keep, _DIR_FLAGS)
    try:
        for parts, name in CREDENTIAL_FILES:
            dfd = _open_subdir(facts.live, parts)
            if dfd is None:
                continue
            try:
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    continue
                os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=kfd)
                os.fsync(dfd)
                os.fsync(kfd)
            finally:
                os.close(dfd)
    finally:
        os.close(kfd)


def park_plan(facts: EndFacts) -> Plan:
    # DECISION: park's `commit` is its CAS; the step itself re-syncs keep/ so a redo is durable.
    return Plan("park", (Phase("move", lambda: park_move(facts)),
                         Phase("commit", lambda: fsync_dir(facts.keep) if os.path.isdir(facts.keep) else None)))


def keep_shape_ok(keep: Path, name: str, slot_uid: int, *, cap: int = V14_BYTES) -> tuple[bool, str]:
    """The return path's shape check (plan §2f 7, V14): ``lstat`` = a regular file, one link, owned by
    the slot uid, at most ``cap`` bytes. Any other shape is deleted by the caller, never moved."""
    try:
        st = os.lstat(Path(keep) / name)
    except FileNotFoundError:
        return False, "absent"
    if not stat.S_ISREG(st.st_mode):
        return False, "not a regular file"
    if st.st_nlink != 1:
        return False, f"{st.st_nlink} links"
    if st.st_uid != slot_uid:
        return False, f"owner {st.st_uid}, not the slot's {slot_uid}"
    if st.st_size > cap:
        return False, f"{st.st_size} bytes > {cap}"
    return True, ""


# --- the reserve (CONTRACT §7.5) -------------------------------------------------------------------


@dataclass
class Reserve:
    """``L/reserve``: ``size`` (V6) bytes of WRITTEN zeros, never fallocate (APFS). A transition write
    that hits ENOSPC truncates it one ``size // 8`` step and retries; the reaper refills it above V5."""

    path: Path
    size: int = RESERVE_BYTES

    @property
    def step(self) -> int:
        return max(1, self.size // 8)

    def current(self) -> int:
        try:
            return os.lstat(self.path).st_size
        except FileNotFoundError:
            return 0

    def release_step(self) -> int:
        """Shrink by one step; returns the bytes released (0 = nothing left to give)."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return 0
        try:
            cur = os.fstat(fd).st_size
            new = max(0, cur - self.step)
            os.ftruncate(fd, new)
            os.fsync(fd)
        finally:
            os.close(fd)
        return cur - new

    def retry(self, fn: Callable[[], None]) -> None:
        while True:
            try:
                fn()
                return
            except OSError as e:
                if e.errno != errno.ENOSPC or self.release_step() == 0:
                    raise

    def refill(self, free_bytes: Callable[[], int], *, floor: int = V5_FLOOR_BYTES) -> bool:
        """Back to ``size`` with written zeros when free space is above ``floor``; returns whether it
        is full."""
        if self.current() >= self.size:
            return True
        if free_bytes() <= floor:
            return False
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            cur = os.fstat(fd).st_size
            os.lseek(fd, cur, os.SEEK_SET)
            zeros = bytes(_CHUNK)
            try:
                while cur < self.size:
                    n = min(_CHUNK, self.size - cur)
                    write_all(fd, zeros[:n])
                    cur += n
            except OSError as e:
                if e.errno != errno.ENOSPC:
                    raise
            os.fsync(fd)
        finally:
            os.close(fd)
        return cur >= self.size


# --- the END driver --------------------------------------------------------------------------------

END_STEPS = ("freeze", "kill", "park", "retain")
_START = {REVOKED: 0, FROZEN: 1, DEAD: 2, CREDENTIAL_PARKED: 3}
_RESUME = {"freeze": 0, "kill": 1, "park": 2, "manifest_budget": 3, **{p: 3 for p in INTENT_PHASES["retain"]}}


def repo_cloned(facts: EndFacts) -> bool:
    """Whether the session ever cloned: ``<S>/repo/`` is a real directory with an entry in it. A
    session that never did retains nothing (CONTRACT §7.3)."""
    if not os.path.isdir(facts.live):
        return False
    fd = _open_subdir(facts.live, (b"repo",))
    if fd is None:
        return False
    try:
        return bool(os.listdir(fd))
    finally:
        os.close(fd)


def _held_name(intent: str, e: PhaseFailed) -> str:
    if intent == "park":
        return "park"
    if isinstance(e.cause, Refused) and e.cause.name in BUDGET_REFUSALS:
        return "manifest_budget"
    return e.phase


def _plan_for(facts: EndFacts, seams: EndSeams, caps: Caps) -> Callable[[RootIntent], Plan]:
    def plan_for(ri: RootIntent) -> Plan:
        return retain_plan(facts, seams, ri, caps=caps) if ri.intent == "retain" else park_plan(facts)
    return plan_for


def reconcile_session(facts: EndFacts, seams: EndSeams, *, caps: Caps = Caps()) -> RootIntent | None:
    """The reaper's step 2 / helper start for one session: finish its journal, from the journal and
    the filesystem only. Raises :class:`PhaseFailed` (journal left at the phase) or ``Refused``."""
    return reconcile(facts.intent, _plan_for(facts, seams, caps), cas=seams.cas, fault=seams.fault,
                     retry=seams.retry, owner_ok=seams.owner_ok)


def _prove(ok: Callable[[], bool], what: str, seams: EndSeams, bound: float | None) -> None:
    t0 = seams.clock()
    if not ok():
        raise Refused(f"{what}-unproven", f"the {what} seam did not prove it")
    if bound is not None and seams.clock() - t0 > bound:
        raise Refused(f"{what}-unproven", f"past the {bound:g} s bound")


def end_session(facts: EndFacts, seams: EndSeams, *, record_state: str, failed_step: str | None = None,
                caps: Caps = Caps()) -> str:
    """Drive one session through END from ``record_state`` (and ``failed_step`` for HELD); returns the
    state it leaves the record in (RETAINED or HELD). Every proof and idempotent act runs every call;
    the record's state only chooses which CAS edges are sent (see the module docstring)."""
    if record_state == RETAINED:
        return RETAINED
    start = _RESUME.get(failed_step or "") if record_state == HELD else _START.get(record_state)
    if start is None:
        raise Refused("not-ending", f"{record_state} / {failed_step}")
    uid = facts.slot_uid
    step = "freeze"
    try:
        _prove(lambda: seams.freeze(uid, V12_SECONDS), "freeze", seams, V12_SECONDS)
        if start <= 0:
            seams.cas(FROZEN, {})
        step = "kill"
        _prove(lambda: seams.kill(uid, V12_SECONDS), "kill", seams, V12_SECONDS)
        _prove(lambda: seams.no_process(uid), "kill", seams, None)
        if start <= 1:
            seams.cas(DEAD, {})

        step = "park"
        pending = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if pending is not None:
            try:
                reconcile_session(facts, seams, caps=caps)
            except PhaseFailed as e:
                step = _held_name(pending.intent, e)
                raise
            if pending.intent == "retain":
                return RETAINED
        if pending is None and start <= 2:
            begin(facts.intent, park_plan(facts), started_at=seams.now(), cas=seams.cas,
                  fault=seams.fault, retry=seams.retry)
        elif start > 2:
            park_move(facts)   # idempotent; no journal and no CAS: the record is already past it

        step = "init"
        if os.path.lexists(facts.tree):
            # A tree and no journal = a retain that finished (its CAS precedes the journal delete):
            # the filesystem says RETAINED, so that is the edge sent, never a second retain.
            seams.cas(RETAINED, dict(retain_plan(facts, seams, caps=caps, started_at=seams.now()).fields()))
            return RETAINED
        if not repo_cloned(facts):
            seams.cas(RETAINED, {})
            return RETAINED
        started = seams.now()
        try:
            begin(facts.intent, retain_plan(facts, seams, caps=caps, started_at=started),
                  started_at=started, cas=seams.cas, fault=seams.fault, retry=seams.retry)
        except PhaseFailed as e:
            step = _held_name("retain", e)
            raise
        return RETAINED
    except PhaseFailed as e:
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held = step
    except Exception as e:   # noqa: BLE001 - an unproven step is HELD, never a crash past the tree
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held = step
    seams.cas(HELD, {"failed_step": held})
    return HELD


__all__ = ["BUDGET_REFUSALS", "CREDENTIAL_FILES", "CREDENTIAL_PARKED", "Caps", "DEAD", "END_STEPS", "EndFacts",
           "EndSeams", "Entry", "FROZEN", "HELD", "LINK_COPY_BYTES", "MANIFEST_FORMAT", "META_FORMAT",
           "OBJECT_FORMAT", "REVOKED", "RETAINED", "Reserve", "V12_SECONDS", "V14_BYTES", "V17_MANIFEST_BYTES",
           "V17_META_BYTES", "V1_INODES", "V2_SECONDS", "V5_FLOOR_BYTES", "VAR_FOLDERS_NAME", "darwin_meta",
           "darwin_strip", "default_meta_reader", "default_strip", "digest_bytes", "end_session", "entry_type",
           "facts_from_alloc", "iter_manifest", "iter_meta", "keep_shape_ok", "linux_meta", "linux_strip",
           "object_record", "os_chown", "outside_link_bytes", "park_move", "park_plan", "reconcile_session",
           "repo_cloned", "retain_plan", "tree_manifest", "unbuilt_freeze", "unbuilt_kill",
           "unbuilt_no_process", "utc_now", "walk"]
