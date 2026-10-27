"""END: the path that ends a session and RETAINS its work (CONTRACT §7.3-7.5, V1 V2 V6 V12 V14 V17).

``REVOKED -> FROZEN -> DEAD -> CREDENTIAL_PARKED -> RETAINED``. Ending a session never deletes its
work (plan §2f): the session tree is renamed, whole, into ``L/retained/<session>/tree/`` after its
manifest and metadata are durable, then neutralized in place (flags, ACLs, xattrs and setuid/setgid
stripped, owner root). Any step that is not PROVEN moves the record to HELD with ``failed_step``; the
tree is never touched by a failure path, and a retry resumes idempotently.

Root acts only on alloc facts, its own intent journal, its own slot state file and a live check
(CONTRACT §7.1): every path here comes from :class:`EndFacts`, built from the alloc record, never from
the state record. Before anything is signalled, root's slot state file must show the slot leased to
THIS session at alloc's ``lease_seq``, else HELD ``failed_step:"slot-not-ours"`` with nothing signalled
(L2 M4). The state record's ``state`` / ``failed_step`` (the reaper's read, §7.7 step 3) only choose
which CAS edges :func:`end_session` sends; every proof (freeze, kill, no process left, for EVERY uid the
session holds) and every idempotent act runs on every call, so a forged state can make root send a
wrong edge (CONTROL refuses it), never skip a proof or signal another session's processes. The one
exception destroys nothing: a record already RETAINED makes END return at once, unproven, ONLY when root's
own retained object for the session is whole (:func:`retained_whole`); a RETAINED record without one is
not believed and END runs from its proofs as for REVOKED (codex r1 1), so a forged RETAINED cannot let
teardown SCRUB and release a slot whose work was never retained.

Locks, always in this order (§3.5 LOCK ORDER: pool -> slot -> session END lock): :func:`end_session` and
:func:`reconcile_session` hold the slot's ``L/slots/{e,v}<n>/lock`` (:func:`slot_lock`), then root's
per-session lock ``L/alloc/<session>.lock`` (:func:`end_lock`: a bounded wait of :data:`END_LOCK_WAIT_S`, then
``end-busy`` with the record unchanged; the reaper or helper retries). The slot lock is held across the slot
check, every proof and every act (codex r1 2): SCRUBBED takes the same lock FIRST and only TRIES the pool lock
after its kills, and prepare holds the pool lock and only TRIES slot locks, so a slot cannot be released and
re-leased between END's check and its signals and no holder ever waits slot -> pool. A CAS CONTROL does not take
is never a step failure (L2 M5): see :func:`send_cas`.

Root reads no bytes the session could not (L2 H2, codex r1 4): a file hard-linked to a path outside the
tree, and any inode a session uid does not own whatever its link count, is hashed (and, outside-linked,
copied) by a reader dropped to a session uid, and one that uid cannot read is recorded and never copied;
root never reads such an inode's metadata. ONE classification (:func:`classify`), computed by the manifest
step and stored beside it, is what the meta and tree steps read. Metadata, strip and chown address every
entry from its walk dirfd, never by a full path (L2 M2).

Every root-only act is an injected seam with a real default: the freeze, kill and process proof
(the defaults here refuse, which lands HELD; the real ones are :func:`levain.slot.proc.end_seams`'s),
``chown``, the metadata reader and strip, the tree flush (``sync``), the session-uid reader, root's slot
state read, the clock, CONTROL's ``cas``. The manifest walk is real
code over dirfds with ``O_NOFOLLOW``.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import fcntl
import hashlib
import os
import secrets
import stat
import struct
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Iterator, Mapping, Sequence

from levain.contracts import ContractError, canon
from levain.slot import Refused, stage, wall, write_all
from levain.slot.ids import SlotId
from levain.slot.layout import RESERVE_BYTES
# The §7.3 states END sends; records owns the names (the full set and its legal edges).
from levain.slot.records import (CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED, parse_slot_state,
                                 read_record)
from levain.slot.root_intent import (INTENT_PHASES, TIMESTAMP_RE, Cas, Fault, Phase, PhaseFailed, Plan,
                                     RootIntent, atomic_write, begin, fsync_dir, intent_path, read_root_intent,
                                     reconcile, sweep_temps, temp_path, unlink_durable)

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
# The manifest's depth cap (the head's 1021+19 ruling on the s5 design's Q5): a tree entry deeper than this many
# levels below the session root refuses ``manifest-depth-cap``, a budget refusal. The slot's code can build a chain
# of any depth; the cap bounds the walk's work and its in-memory stack, never the fds it holds (that is
# WALK_OPEN_DIRS, below the smallest default RLIMIT_NOFILE).
MANIFEST_DEPTH = 1024
# DECISION: the most directory fds one walk holds open at a time (its root's included). An ancestor past it is
# closed before the walk opens a child and reopened through the child's ``..`` when the walk climbs back, checked
# to be the same directory, so a deep chain costs reopens, not fds.
WALK_OPEN_DIRS = 32
# The most fds one walk has open at any instant: WALK_OPEN_DIRS plus one reserved for enumeration (listing a
# directory by its fd duplicates the fd for as long as the listing runs).
WALK_PEAK_FDS = WALK_OPEN_DIRS + 1
# DECISION: how long END keeps retrying a CAS CONTROL did not take (``locked``, CONTROL down) before it
# leaves the record for the next pass: V12's order, so one END never outlives a reaper pass by much.
CAS_RETRY_SECONDS = V12_SECONDS
CAS_BACKOFF_FIRST = 0.01
CAS_BACKOFF_MAX = 1.0
# DECISION: END waits for the slot lock on V23's order with a doubling backoff, as the helper's own flock
# waits do; past it, ``slot-locked`` is raised for the caller to retry.
SLOT_LOCK_WAIT_S = 10.0
SLOT_LOCK_FIRST_S = 0.02
SLOT_LOCK_MAX_S = 0.5
# DECISION: END's wait for the session END lock (a ``start`` holds it from before its clone through its ACTIVE
# CAS), on the same order and backoff; past it, ``end-busy``.
END_LOCK_WAIT_S = SLOT_LOCK_WAIT_S

#: The slot-lock and END-lock descriptors this process holds (comp r2 L4). Every forked child (the session-uid
#: reader, macOS's signalling child) closes them before it runs anything, so a child that outlives END can never
#: keep either flock held; a closed duplicate leaves the parent's lock as it was.
_LOCK_FDS: set[int] = set()


def _close_lock_fds_in_child() -> None:
    for fd in tuple(_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            pass
    _LOCK_FDS.clear()


os.register_at_fork(after_in_child=_close_lock_fds_in_child)

MANIFEST_FORMAT = "levain-end-manifest/1"
META_FORMAT = "levain-retention-meta/1"
OBJECT_FORMAT = "levain-retained-object/1"


#: The vendor credential files (plan §2e 11): vendor -> (dir under the session tree, file name), parked
#: in ``L/slots/e<n>/keep/`` under the same name, for a passthrough session of that vendor only.
CREDENTIALS: Mapping[str, tuple[tuple[bytes, ...], bytes]] = {
    "claude": ((b"config", b"claude"), b".credentials.json"),
    "codex": ((b"config", b"codex"), b"auth.json"),
}
CREDENTIAL_FILES = tuple(CREDENTIALS.values())
#: IG-1 (1027-18 RULINGS): ``L/retained/<s>/replaced/``, where END puts the credential a confirmed re-login replaces in
#: ``keep/``. It is inside the session's own retained directory, so discard, restore and purge carry it with the session.
REPLACED = "replaced"
#: DECISION: a ``levain-baseline/1`` file is five short fields; one past this is not root's baseline.
BASELINE_MAX_BYTES = 4096
VAR_FOLDERS_NAME = b".levain-var-folders"     # CONTRACT §7.4: macOS /private/var/folders/<slot>

#: The refusals that mean "past the manifest budget": HELD ``failed_step:"manifest_budget"``, whose
#: exit is END again with larger caps (§7.3 force-retire a).
BUDGET_REFUSALS = frozenset({"manifest-inode-cap", "manifest-time-cap", "manifest-byte-cap", "meta-byte-cap",
                             "link-copy-cap", "manifest-depth-cap"})

#: ``L/retained/<s>/linkcopy/<dev>-<ino>``: a foreign inode's bytes as the session uid read them, staged by
#: the manifest step and linked into the tree by the tree step (L2 H2).
LINKCOPY = "linkcopy"
#: (size, digest) of an outside-linked file the session uid cannot read. DECISION: the manifest records it
#: as ``size:0, digest:""`` (a file's digest is never empty otherwise) and the tree holds an empty 0600
#: placeholder at its path: the entry is recorded, its bytes never copied, never root's.
UNREAD = (0, "")
#: ``L/retained/<s>/classes.jsonl``: the manifest step's ONE classification of the tree's inodes
#: (:func:`classify`), read back by the meta and tree steps (codex r1 4). DECISION: a root working file
#: beside the manifest, removed by the ``commit`` step, so the retained object stays §7.4's list.
CLASSES = "classes.jsonl"
#: A regular file some of whose links lie outside the tree: read by a session uid, copied, the copy placed.
OUTSIDE = "outside"
#: Any other inode a session uid does not own: its bytes read by a session uid only (hashed, never copied),
#: its metadata never read by root, whatever its link count.
UNOWNED = "unowned"
#: The session-uid reader's answer for a file its uid may not open (EACCES / EPERM), END's own name beside
#: stage's ``staging-missing`` (ENOENT) and ``staging-not-regular`` (ELOOP, not a regular file).
LINK_UNREADABLE = "link-unreadable"
#: The reader answers that mean "this uid cannot read it" and land :data:`UNREAD` (comp r1 2); every other
#: answer (stage's ``staging-open-failed`` for EMFILE, EIO, ENOMEM ... included) is ``link-reader-failed``.
UNREAD_ANSWERS = frozenset({"staging-missing", "staging-not-regular", LINK_UNREADABLE})
#: DECISION: an UNOWNED file is hashed, never copied, so the copy cap does not bound it; the V2 time budget does.
HASH_ONLY_CAP = (1 << 63) - 1

_CHUNK = 1 << 20
_LINK_TMP = b".levain-link."
_O_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_O_ENTRY = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
_O_SYMLINK = getattr(os, "O_SYMLINK", 0)       # macOS: open the link itself
_SETID = stat.S_ISUID | stat.S_ISGID


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
    """The manifest budget (V1 inodes, V2 seconds, V17 manifest + meta bytes, the depth cap) and the
    outside-link copy cap. ``force-retire`` (a) is END again with a larger one. DECISION: ``scaled`` leaves
    ``depth`` as it is: depth is a shape of the tree, not an amount of work, so a scaled budget never makes
    an ordinary nested tree refuse."""

    inodes: int = V1_INODES
    seconds: float = V2_SECONDS
    manifest_bytes: int = V17_MANIFEST_BYTES
    meta_bytes: int = V17_META_BYTES
    link_copy_bytes: int = LINK_COPY_BYTES
    depth: int = MANIFEST_DEPTH

    def scaled(self, factor: float) -> "Caps":
        return Caps(int(self.inodes * factor), self.seconds * factor, int(self.manifest_bytes * factor),
                    int(self.meta_bytes * factor), int(self.link_copy_bytes * factor), self.depth)


# --- facts (from the alloc record only) ------------------------------------------------------------


@dataclass(frozen=True)
class EndFacts:
    """Everything END needs about a session, from its alloc record (CONTRACT §7.1) and the store root.
    ``var_folders``: macOS ``/private/var/folders/<slot>``, the session owner's path from alloc, ``None`` on
    Linux. ``lease_seq``: the lease root's slot state
    file must still show (§3.5 Pool). ``child_uid``: a verifier pair's child (§1, §5.6), frozen, killed and
    proven gone with the slot uid. ``vendor`` + ``auth``: a credential is parked on a passthrough session
    only. ``kind``: alloc's; only a ``login`` session can replace a parked credential (IG-1). The defaults fail
    closed: no ``lease_seq`` = ``slot-not-ours``, no ``auth`` = nothing parked, no ``kind`` = nothing replaced."""

    session: str
    slot: SlotId
    slot_uid: int
    slot_gid: int
    store: Path
    var_folders: Path | None = None
    lease_seq: int | None = None
    child_uid: int | None = None
    vendor: str | None = None
    auth: str | None = None
    kind: str | None = None

    @property
    def uids(self) -> tuple[int, ...]:
        """Every uid the session's processes run as (L2 H3)."""
        return (self.slot_uid,) if self.child_uid is None else (self.slot_uid, self.child_uid)

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
    def cloned_marker(self) -> Path:
        return cloned_path(self.store, self.session)

    @property
    def lock(self) -> Path:
        """DECISION: root's per-session END lock beside the journal (L2 M7); never unlinked."""
        return self.intent.with_name(f"{self.session}.lock")

    @property
    def keep(self) -> Path:
        return self.slot.slot_dir(self.store) / "keep"

    @property
    def replaced(self) -> Path:
        return self.retained / REPLACED

    @property
    def slot_state(self) -> Path:
        return self.slot.slot_dir(self.store) / "state"

    @property
    def slot_lock(self) -> Path:
        """The slot's flock, the one prepare, SCRUBBED and END take (§3.5)."""
        return self.slot.lock_path(self.store)

    @property
    def parks(self) -> bool:
        return self.slot.slot_class == "egress" and self.auth == "passthrough" and self.vendor in CREDENTIALS

    @property
    def credential(self) -> tuple[tuple[bytes, ...], bytes] | None:
        """The one credential file END parks (the session vendor's, on a passthrough session)."""
        return CREDENTIALS[self.vendor] if self.parks else None   # type: ignore[index]

    @property
    def parked_paths(self) -> frozenset[bytes]:
        c = self.credential
        return frozenset() if c is None else frozenset({b"/".join(c[0] + (c[1],))})

    @property
    def extra(self) -> dict[bytes, Path]:
        return {VAR_FOLDERS_NAME: self.var_folders} if self.var_folders is not None else {}


def _is_id(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def facts_from_alloc(alloc: Mapping[str, object], *, store: Path, var_folders: Path | None = None) -> EndFacts:
    """The END facts of a parsed ``levain-alloc/1`` record: ``session``, ``slot{class,index}``,
    ``slot_uid``, ``slot_gid``, ``lease_seq``, ``child_uid`` (none class), ``vendor``, ``auth``, ``kind``
    (absent = None). Anything else in it is not END's."""
    try:
        slot = alloc["slot"]
        facts = EndFacts(alloc["session"], SlotId(slot["class"], slot["index"]),  # type: ignore[index]
                         alloc["slot_uid"], alloc["slot_gid"], Path(store), var_folders,  # type: ignore[arg-type]
                         alloc["lease_seq"], alloc.get("child_uid"), alloc["vendor"],  # type: ignore[arg-type]
                         alloc["auth"], alloc.get("kind"))  # type: ignore[arg-type]
    except (KeyError, TypeError) as e:
        raise Refused("alloc-malformed", f"END facts: {e}") from None
    if not _is_id(facts.slot_uid) or not _is_id(facts.slot_gid):
        raise Refused("alloc-malformed", "slot_uid / slot_gid")
    if not isinstance(facts.lease_seq, int) or isinstance(facts.lease_seq, bool) or facts.lease_seq < 0:
        raise Refused("alloc-malformed", "lease_seq")
    if (facts.child_uid is not None) != (facts.slot.slot_class == "none") or \
            (facts.child_uid is not None and not _is_id(facts.child_uid)):
        raise Refused("alloc-malformed", "child_uid is a uid, present exactly for the none class")
    if not isinstance(facts.vendor, str) or not isinstance(facts.auth, str):
        raise Refused("alloc-malformed", "vendor / auth")
    if facts.kind is not None and not isinstance(facts.kind, str):
        raise Refused("alloc-malformed", "kind")
    facts.intent   # validates the session id
    return facts


# --- the walk --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One tree entry. ``dirfd`` + ``name`` address it without following a link (``dirfd`` is -1 for
    an ``extra`` root child, addressed by ``abspath``); both are valid only while the walk is at it.
    ``abspath`` is for messages; no metadata call uses it (L2 M2)."""

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


def _reopen_parent(child_fd: int, frame: list) -> None:
    """Reopen a closed ancestor ``frame`` through its child's ``..`` (never a link), checked to be the
    directory the walk opened before; anything else refuses ``tree-changed``."""
    fd = os.open(b"..", _DIR_FLAGS, dir_fd=child_fd)
    try:
        if not _same(os.fstat(fd), frame[7]):
            raise Refused("tree-changed", f"{frame[2]!r} changed under the walk")
    except BaseException:
        os.close(fd)
        raise
    frame[0] = fd


def walk(root: Path, extra: Mapping[bytes, Path] | None = None, *,
         depth_cap: int = MANIFEST_DEPTH, prune: Callable[[Entry], bool] | None = None) -> Iterator[Entry]:
    """Every entry under ``root`` (not ``root`` itself), depth first, a directory before its children,
    names in byte order; never follows a link (each directory opened ``O_NOFOLLOW`` from its parent's
    fd and checked against the ``lstat`` that named it). ``extra`` adds root children that live
    elsewhere (macOS ``/var/folders/<slot>``, walked as ``.levain-var-folders`` before it is moved
    there); an absent one is skipped, a present one must be a real directory, and a name the tree
    already has is ``retain-conflict``. An entry on another filesystem than its directory (a mount inside
    the tree, L2 L12) refuses ``tree-mount``: root never walks, strips or chowns into one.

    An entry more than ``depth_cap`` levels below ``root`` (a root child is level 1) refuses
    ``manifest-depth-cap``. At most :data:`WALK_OPEN_DIRS` directory fds are held: the root's always, and
    the deepest ones; an ancestor is closed before a child would pass that, and reopened from its child's ``..``
    on the way back up (:func:`_reopen_parent`). The walk never has more than :data:`WALK_PEAK_FDS` open at
    once. Only the directory of the entry yielded is guaranteed open.

    ``prune``: called on a yielded directory AFTER the consumer's step returns and before the walk opens it;
    true = do not descend (the directory itself was yielded, nothing under it is opened or listed). The default
    descends everywhere. A plain call, never a ``send()``: callers iterate with ``for``."""
    extra = dict(extra or {})
    root_b = os.fsencode(root)
    root_fd = os.open(root_b, _DIR_FLAGS)
    # frame: [fd (-1 = closed), prefix, absprefix, names, i, extra, dev, the directory's stat]
    stack: list[list] = []
    low = 1   # the shallowest non-root frame that may still be open; every frame below it is closed
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
        root_st = os.fstat(root_fd)
        stack.append([root_fd, b"", root_b, sorted(names + list(live_extra)), 0, live_extra, root_st.st_dev,
                      root_st])
        while stack:
            frame = stack[-1]
            fd, prefix, absprefix, names, i, fextra, dev, _dst = frame
            if i == len(names):
                stack.pop()
                if stack and stack[-1][0] < 0:
                    try:
                        _reopen_parent(fd, stack[-1])
                    finally:
                        os.close(fd)
                    low = len(stack) - 1
                else:
                    os.close(fd)
                continue
            frame[4] = i + 1
            name = names[i]
            path = prefix + name
            if len(stack) > depth_cap:
                raise Refused("manifest-depth-cap", f"{path[:200]!r}... is deeper than {depth_cap} levels")
            if name in fextra:
                absp, st = fextra[name]
                e = Entry(path, st, -1, name, absp)
                yield e
            else:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if st.st_dev != dev:
                    raise Refused("tree-mount", f"{path!r} is on another filesystem")
                absp = absprefix + b"/" + name
                e = Entry(path, st, fd, name, absp)
                yield e
                if not stat.S_ISDIR(st.st_mode):
                    continue
            if prune is not None and prune(e):
                continue
            # The open frames are the root's and stack[low:]; the child makes one more, so the shallowest goes
            # first (never the current frame: WALK_OPEN_DIRS is far above two).
            if 1 + len(stack) - low >= WALK_OPEN_DIRS:
                os.close(stack[low][0])
                stack[low][0] = -1
                low += 1
            child = _open_dir_at(absp, -1, st) if name in fextra else _open_dir_at(name, fd, st)
            try:
                cnames = sorted(os.fsencode(n) for n in os.listdir(child))   # the reserved fd: a dup of child
            except BaseException:
                os.close(child)
                raise
            stack.append([child, path + b"/", absp, cnames, 0, {}, st.st_dev, st])
    finally:
        for frame in stack:
            if frame[0] >= 0:
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


def _entry_fd(e: Entry) -> int | None:
    """An fd on the entry itself, opened from its walk dirfd and never through a link: a directory, file
    or fifo ``O_NOFOLLOW`` (``O_NONBLOCK``, so a fifo cannot hang it); a macOS symlink ``O_SYMLINK``.
    ``None`` for an entry with no safe fd form here: a Linux symlink, any socket (open refuses one) and any
    device (an open as root could act on the device)."""
    m = e.st.st_mode
    if stat.S_ISDIR(m):
        flags = _DIR_FLAGS
    elif stat.S_ISREG(m) or stat.S_ISFIFO(m):
        flags = _O_ENTRY
    elif stat.S_ISLNK(m) and _O_SYMLINK:
        flags = os.O_RDONLY | _O_SYMLINK | os.O_CLOEXEC
    else:
        return None
    fd = os.open(e.name, flags, dir_fd=e.dirfd) if e.dirfd >= 0 else os.open(e.abspath, flags)
    if not _same(os.fstat(fd), e.st):
        os.close(fd)
        raise Refused("tree-changed", f"{e.path!r} changed under the walk")
    return fd


# --- the manifest (levain-end-manifest/1) ----------------------------------------------------------


@dataclass
class LinkCount:
    nlink: int
    seen: int
    size: int
    regular: bool = True


class _Budget:
    def __init__(self, caps: Caps, clock: Callable[[], float]) -> None:
        self.caps, self.clock, self.t0, self.entries = caps, clock, clock(), 0

    @property
    def deadline(self) -> float:
        return self.t0 + self.caps.seconds

    def tick(self) -> None:
        if self.clock() - self.t0 > self.caps.seconds:
            raise Refused("manifest-time-cap", f"hashing passed {self.caps.seconds:g} s")

    def entry(self) -> None:
        self.entries += 1
        if self.entries > self.caps.inodes:
            raise Refused("manifest-inode-cap", f"more than {self.caps.inodes} entries")
        self.tick()


def _hash_file(e: Entry, budget: _Budget) -> tuple[int, str]:
    fd = os.open(e.name, _O_ENTRY, dir_fd=e.dirfd)
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


#: ``read_foreign(entry, budget) -> (size, digest)``: an outside-linked file's bytes, read as the session.
ForeignRead = Callable[[Entry, _Budget], "tuple[int, str]"]


def iter_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic, credentials: Collection[bytes] = (),
                  skip_credentials: bool = False, foreign: Collection[tuple[int, int]] = (),
                  read_foreign: ForeignRead | None = None) -> Iterator[dict]:
    """The manifest records of the tree, in walk order: ``{path_b64, type, mode, size, nlink, digest}``
    (``mode`` = the permission bits, ``stat.S_IMODE``; a file's digest is over its bytes, a link's over
    its ``readlink`` target bytes; a dir / fifo / socket / device: size 0, digest ""). The inode, time
    and depth caps refuse by name mid-walk.

    ``credentials``: the tree paths END parks (the session vendor's credential on a passthrough session,
    plan §2g 3); one still there refuses ``credential-not-parked`` (END parks first, so the manifest never
    holds it), and ``skip_credentials`` leaves it out instead, for a snapshot of a live tree. ``foreign``:
    the inodes some of whose links lie outside the tree; root never opens one, ``read_foreign`` (the
    session-uid reader) hashes it (L2 H2), and :data:`UNREAD` is what it records for one it cannot read."""
    budget = _Budget(caps, clock)
    creds = frozenset(credentials)
    foreign = frozenset(foreign)
    for e in walk(root, extra, depth_cap=caps.depth):
        t = entry_type(e.st.st_mode)
        if e.path in creds and t != "dir":
            if skip_credentials:
                continue
            raise Refused("credential-not-parked", e.path.decode("utf-8", "replace"))
        budget.entry()
        size, digest = 0, ""
        if t == "file":
            if (e.st.st_dev, e.st.st_ino) in foreign:
                if read_foreign is None:
                    raise Refused("link-reader-unset", e.path.decode("utf-8", "replace"))
                size, digest = read_foreign(e, budget)
            else:
                size, digest = _hash_file(e, budget)
        elif t == "link":
            target = os.readlink(e.name, dir_fd=e.dirfd)
            size, digest = len(target), digest_bytes(target)
        # DECISION: a directory's st_nlink is filesystem bookkeeping (APFS counts every entry, ext4 only
        # subdirectories) and drops when the credential is parked out of it; recorded as 1, so the
        # retained tree's manifest equals one taken before the freeze.
        yield {"path_b64": _b64(e.path), "type": t, "mode": stat.S_IMODE(e.st.st_mode), "size": size,
               "nlink": 1 if t == "dir" else e.st.st_nlink, "digest": digest}


def tree_manifest(root: Path, *, extra: Mapping[bytes, Path] | None = None, caps: Caps = Caps(),
                  clock: Callable[[], float] = time.monotonic, credentials: Collection[bytes] = (),
                  skip_credentials: bool = False) -> list[dict]:
    """The whole manifest in memory (a snapshot read as the caller; END streams its own to disk)."""
    return list(iter_manifest(root, extra=extra, caps=caps, clock=clock, credentials=credentials,
                              skip_credentials=skip_credentials))


def _count_link(links: dict[tuple[int, int], LinkCount], e: Entry) -> None:
    """Count ``e`` if it is a non-directory with more than one link, of ANY type (comp r1 4: a Linux symlink
    or a macOS fifo can be hard-linked outside the tree too)."""
    if not stat.S_ISDIR(e.st.st_mode) and e.st.st_nlink > 1:
        lc = links.setdefault((e.st.st_dev, e.st.st_ino),
                              LinkCount(e.st.st_nlink, 0, e.st.st_size, stat.S_ISREG(e.st.st_mode)))
        lc.seen += 1


def _count_links(root: Path, extra: Mapping[bytes, Path] | None = None, *,
                 depth_cap: int = MANIFEST_DEPTH) -> dict[tuple[int, int], LinkCount]:
    links: dict[tuple[int, int], LinkCount] = {}
    for e in walk(root, extra, depth_cap=depth_cap):
        _count_link(links, e)
    return links


def foreign_links(root: Path, extra: Mapping[bytes, Path] | None = None, *,
                  depth_cap: int = MANIFEST_DEPTH) -> dict[tuple[int, int], LinkCount]:
    """The non-directory inodes, of any type, some of whose links lie outside ``root`` (+ ``extra``):
    ``nlink`` greater than the links the walk sees. Root never strips or chowns one (comp r1 4)."""
    return {k: lc for k, lc in _count_links(root, extra, depth_cap=depth_cap).items() if lc.seen < lc.nlink}


def outside_link_bytes(links: Mapping[tuple[int, int], LinkCount]) -> int:
    """Bytes END will copy: one copy per regular file some of whose links lie outside the tree, linked into
    each of its in-tree paths."""
    return sum(lc.size for lc in links.values() if lc.regular and lc.seen < lc.nlink)


def classify(root: Path, extra: Mapping[bytes, Path] | None, uids: Collection[int], *,
             depth_cap: int = MANIFEST_DEPTH) -> tuple[dict[tuple[int, int], str], dict[tuple[int, int], LinkCount]]:
    """ONE classification of the frozen tree's inodes, by one walk, before a byte is read (codex r1 4):
    :data:`OUTSIDE` for a regular file some of whose links lie outside the tree, :data:`UNOWNED` for any
    other inode whose owner is not one of ``uids`` (the session's), whatever its link count. Returns it with
    the outside-linked counts (for the copy cap). A NON-regular inode linked outside the tree refuses
    ``tree-shared-special`` (comp r1 4): it has no copy, and root never strips or chowns a shared inode."""
    uids = frozenset(uids)
    links: dict[tuple[int, int], LinkCount] = {}
    classes: dict[tuple[int, int], str] = {}
    for e in walk(root, extra, depth_cap=depth_cap):
        _count_link(links, e)
        if e.st.st_uid not in uids:
            classes[(e.st.st_dev, e.st.st_ino)] = UNOWNED
    outside = {k: lc for k, lc in links.items() if lc.seen < lc.nlink}
    for key, lc in outside.items():
        if not lc.regular:
            raise Refused("tree-shared-special", f"inode {key[0]}-{key[1]}: a non-regular inode linked outside "
                                                 f"the tree ({lc.seen} of {lc.nlink} links in it)")
        classes[key] = OUTSIDE
    return classes, outside


def _write_classes(path: Path, classes: Mapping[tuple[int, int], str], cap: int, depth: int) -> None:
    """DECISION: under V17's meta cap (a budget refusal, ``meta-byte-cap``): it is metadata about the tree. The
    first line is the depth cap the manifest walk accepted the tree under (:func:`stored_classification`)."""
    def records() -> Iterator[dict]:
        yield {"depth": depth}
        yield from ({"dev": k[0], "ino": k[1], "class": c} for k, c in sorted(classes.items()))
    _stream_jsonl(path, records(), cap, "meta-byte-cap")


def stored_classification(facts: "EndFacts") -> tuple[dict[tuple[int, int], str], int]:
    """The manifest step's classification, as stored, and the depth cap its walk accepted the tree under:
    every later walk of the tree (meta, tree) takes that depth, never the pass's ``Caps.depth``, so a pass
    with a smaller cap cannot refuse a tree the manifest already accepted (and the tree step may already have
    renamed). Absent or not one = ``classes-missing`` (HELD at the phase that needed it), never a recompute.
    DECISION: a classification with no depth line (written before the cap existed) caps nothing: the walk
    that accepted it had none, and the walk's fds are bounded whatever the depth."""
    p = facts.retained / CLASSES
    try:
        data = _read_durable(p)
        out: dict[tuple[int, int], str] = {}
        depth = sys.maxsize
        for n, line in enumerate(data.splitlines()):
            rec = canon.parse(line)
            if n == 0 and isinstance(rec, dict) and set(rec) == {"depth"}:
                if not isinstance(rec["depth"], int) or isinstance(rec["depth"], bool) or rec["depth"] < 1:
                    raise ValueError(line[:80])
                depth = rec["depth"]
                continue
            if (not isinstance(rec, dict) or set(rec) != {"dev", "ino", "class"}
                    or rec["class"] not in (OUTSIDE, UNOWNED)):
                raise ValueError(line[:80])
            out[(int(rec["dev"]), int(rec["ino"]))] = rec["class"]   # type: ignore[arg-type]
    except (OSError, ValueError) as e:
        raise Refused("classes-missing", f"{p}: {e}") from None
    return out, depth


def read_classes(facts: "EndFacts") -> dict[tuple[int, int], str]:
    """The manifest step's classification, as stored (:func:`stored_classification`)."""
    return stored_classification(facts)[0]


def _stream_jsonl(path: Path, records: Iterator[dict], cap: int, cap_name: str) -> None:
    """``records`` as canonical JSONL at ``path``, durable (a temp unique per writer + fsync + rename +
    fsync(dir)); past ``cap`` bytes refuses ``cap_name`` with the temp removed and ``path`` untouched."""
    tmp = temp_path(path)
    fd = os.open(tmp, _O_NEW, 0o600)
    try:
        fh = os.fdopen(fd, "wb", buffering=_CHUNK)
    except BaseException:   # the file object never took the fd: close it here (glm r1 LOW)
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    ok = False
    try:
        with fh:
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


# --- the session-uid reader (L2 H2) ----------------------------------------------------------------

#: ``run_as(uid, gid, fn, wfd, keep)``: run ``fn(wfd)`` as ``uid``/``gid`` with the ``keep`` fds left
#: open, owning ``wfd`` (closing it when ``fn`` ends). Returns the running reader: stage's ``Reader`` whose
#: ``wait()`` returns the reader's exit status (0 = it ended cleanly; anything else, ``None`` included, is a
#: failed reader, comp r1 1).
SessionRunAs = Callable[[int, int, Callable[[int], None], int, Sequence[int]], "stage.Reader"]


class _Forked(stage._Forked):
    """stage's forked reader, whose ``wait()`` also returns the exit status (a signal = 128 + its number). A killed
    reader's is ``None`` (failed): stage's bounded reap takes it, and one that will not die is never waited on. So is
    one still alive past the deadline after its end mark (``link-reader-failed``, never a success)."""

    code: int | None = None

    def wait(self) -> int | None:   # type: ignore[override]
        if self._killed:
            super().wait()
        elif not self._reaped:
            status = self._clean_wait()
            if status is None:
                return None
            self.code = os.waitstatus_to_exitcode(status)
            if self.code < 0:
                self.code = 128 - self.code
        return self.code


def fork_run_as(uid: int, gid: int, fn: Callable[[int], None], wfd: int, keep: Sequence[int] = ()) -> stage.Reader:
    """The real ``run_as``, :func:`levain.slot.stage.fork_run_as`'s pattern with a session uid as the target:
    fork; the child closes every fd but 0-2, ``wfd`` and ``keep``, drops with the one drop
    (:func:`levain.slot.wall.drop_and_assert`, supplementary groups exactly ``{gid}``), runs ``fn`` and
    ``_exit``s (70 if the drop or ``fn`` raised, having written nothing more). Only root can run it.
    DECISION: no RLIMIT_NPROC (the reader never forks)."""
    pid = os.fork()
    if pid == 0:   # pragma: no cover - the child; exercised only as root (VM row)
        code = 0
        try:
            stage._close_fds_except([wfd, *keep])
            wall.drop_and_assert(uid, gid, (gid,), nproc=None)
            fn(wfd)
        except BaseException:   # noqa: BLE001 - the child never returns into the parent's stack
            code = 70
        finally:
            os._exit(code)
    os.close(wfd)
    return _Forked(pid)


def _reader_uid(facts: EndFacts, e: Entry) -> int:
    """DECISION: the session uid that owns the entry's directory (a verifier's child owns ``<S>/repo``,
    §3.4), else the slot uid. Either way a session uid, never root (§1.1: its gid is the uid)."""
    owner = os.fstat(e.dirfd).st_uid
    return owner if owner in facts.uids else facts.slot_uid


def session_reader(dirfd: int, name: bytes, cap: int, wfd: int) -> None:
    """The session-uid reader's whole job, after the drop (comp r1 2, END-side: stage.py folds every open
    errno but ENOENT / ELOOP into one ``staging-open-failed``). One probe open as the reader's uid, with
    stage's own flags, answers :data:`LINK_UNREADABLE` for EACCES / EPERM, the only "this uid may not read
    it" errnos; anything else falls to :func:`levain.slot.stage.reader`, whose open names absent and not
    regular and folds the rest into ``staging-open-failed``, which END takes as a failed reader (HELD, retried)."""
    try:
        fd = os.open(name, stage._O_READ, dir_fd=dirfd)
    except OSError as err:
        if err.errno in (errno.EACCES, errno.EPERM):
            return stage._send_refusal(wfd, LINK_UNREADABLE)
    else:
        os.close(fd)
    stage.reader(dirfd, name, cap, "link-copy-cap", wfd)   # type: ignore[arg-type]


def read_as_session(facts: EndFacts, seams: "EndSeams", e: Entry, out_fd: int | None, cap: int,
                    budget: _Budget) -> tuple[int, str]:
    """``e``'s bytes, read by :func:`session_reader` in a process dropped to a session uid and streamed into
    ``out_fd`` (``None``: hashed only): ``(bytes read, their digest)``, or :data:`UNREAD` when that uid
    cannot read it (absent, not regular, EACCES / EPERM: :data:`UNREAD_ANSWERS`). Anything else is
    ``link-reader-failed`` (HELD, retried), never UNREAD and never a kept file (comp r1 1): a reader that
    ends without an answer (its drop not proven, say), any other open errno, a short read (fewer bytes
    than its ``S`` header promised), a missing end mark, or a reader whose exit status is not 0, so neither a
    broken drop nor a transient error can turn readable work into placeholders or truncated copies.
    DECISION: what the reader read is what is recorded and copied, so a file an outside owner grows meanwhile
    keeps the bytes read (the reader's ``X`` end mark)."""
    uid = _reader_uid(facts, e)
    rfd, wfd = os.pipe()
    proc = None
    try:
        proc = seams.run_as(uid, uid, lambda w: session_reader(e.dirfd, e.name, cap, w), wfd, (e.dirfd,))
        wfd = -1   # run_as owns it now
        if isinstance(proc, stage._Forked):   # the real reader: its clean wait gets only what is left of the budget
            proc.until, proc.clock = budget.deadline, budget.clock
        pipe = stage._Pipe(rfd, budget.deadline, budget.clock, stage.POLL_SLICE_S)
        tag = pipe.exactly(1)
        if tag == b"R":
            ln = pipe.exactly(1)
            name = pipe.exactly(ln[0]).decode("ascii", "replace") if ln else ""
            if name == "link-copy-cap":
                raise Refused("link-copy-cap", f"{e.path!r} is past {cap} bytes")
            if name not in UNREAD_ANSWERS:
                raise Refused("link-reader-failed", f"{e.path!r}: {name}")
            got = UNREAD
        else:
            head = pipe.exactly(8) if tag == b"S" else b""
            if len(head) != 8:
                raise Refused("link-reader-failed", f"{e.path!r}: no answer from the reader")
            accepted = struct.unpack(">Q", head)[0]
            if accepted > cap:
                raise Refused("link-copy-cap", f"{e.path!r}: {accepted} > {cap}")
            h, n = hashlib.sha256(), 0
            while n < accepted:
                chunk = pipe.read(min(_CHUNK, accepted - n))
                if not chunk:
                    break
                if out_fd is not None:
                    write_all(out_fd, chunk)
                h.update(chunk)
                n += len(chunk)
                budget.tick()
            if n != accepted:
                raise Refused("link-reader-failed", f"{e.path!r}: a short read, {n} of {accepted} bytes")
            if pipe.exactly(1) not in (b"X", b"E"):
                raise Refused("link-reader-failed", f"{e.path!r}: the reader sent no end mark")
            got = (n, "sha256:" + h.hexdigest())
        code = proc.wait()
        if code != 0:
            raise Refused("link-reader-failed", f"{e.path!r}: the reader exited {code}")
        return got
    except Refused as err:
        if proc is not None:
            proc.kill()
        if err.name == "staging-raced":   # stage's pipe deadline, which is the manifest budget's
            raise Refused("manifest-time-cap", f"reading {e.path!r} passed the V2 budget") from None
        raise
    except BaseException:
        if proc is not None:
            proc.kill()
        raise
    finally:
        os.close(rfd)          # a reader blocked writing gets EPIPE and ends
        if proc is not None:
            proc.wait()
        if wfd >= 0:
            os.close(wfd)


def _staged_name(key: tuple[int, int]) -> bytes:
    return f"{key[0]}-{key[1]}".encode("ascii")


def _clear_linkcopy(facts: EndFacts, seams: "EndSeams") -> None:
    """Remove a staging dir an earlier attempt left (root's own copies, never work)."""
    d = facts.retained / LINKCOPY
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode) or not seams.owner_ok(st):
        raise Refused("bad-root", f"{d} is not a directory with the expected owner")
    fd = os.open(d, _DIR_FLAGS)
    try:
        for n in os.listdir(fd):
            os.unlink(n, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rmdir(d)
    fsync_dir(facts.retained)


# --- metadata (levain-retention-meta/1): per-OS readers and strips, every call on an fd --------------

MetaReader = Callable[[Entry, Callable[[str], None]], dict]
Strip = Callable[[Entry, Callable[[str], None]], None]

_ACL_TYPE_EXTENDED = 0x00000100
_libc = None


def _darwin_libc():
    global _libc
    if _libc is None:
        c = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        c.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        c.flistxattr.restype = ctypes.c_ssize_t
        c.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.c_uint32, ctypes.c_int]
        c.fgetxattr.restype = ctypes.c_ssize_t
        c.fremovexattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        c.fremovexattr.restype = ctypes.c_int
        c.fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
        c.fchflags.restype = ctypes.c_int
        c.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        c.acl_get_fd_np.restype = ctypes.c_void_p
        c.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
        c.acl_to_text.restype = ctypes.c_void_p
        c.acl_free.argtypes = [ctypes.c_void_p]
        c.acl_init.argtypes = [ctypes.c_int]
        c.acl_init.restype = ctypes.c_void_p
        c.acl_set_fd_np.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        c.acl_set_fd_np.restype = ctypes.c_int
        _libc = c
    return _libc


def _oserr(what: str) -> OSError:
    n = ctypes.get_errno()
    return OSError(n, f"{what}: {os.strerror(n)}")


def _darwin_xattr_names(fd: int) -> list[bytes]:
    c = _darwin_libc()
    for _ in range(4):   # the list can grow between the size call and the read
        n = c.flistxattr(fd, None, 0, 0)
        if n < 0:
            raise _oserr("flistxattr")
        if n == 0:
            return []
        buf = ctypes.create_string_buffer(n)
        m = c.flistxattr(fd, buf, n, 0)
        if m >= 0:
            return [x for x in buf.raw[:m].split(b"\0") if x]
        if ctypes.get_errno() != errno.ERANGE:
            raise _oserr("flistxattr")
    raise OSError(errno.ERANGE, "flistxattr kept growing")


def _darwin_getxattr(fd: int, name: bytes) -> bytes:
    c = _darwin_libc()
    n = c.fgetxattr(fd, name, None, 0, 0, 0)
    if n < 0:
        raise _oserr("fgetxattr")
    buf = ctypes.create_string_buffer(max(n, 1))
    m = c.fgetxattr(fd, name, buf, n, 0, 0)
    if m < 0:
        raise _oserr("fgetxattr")
    return buf.raw[:m]


def _darwin_acl_text(fd: int) -> str | None:
    c = _darwin_libc()
    acl = c.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() in (errno.ENOENT, errno.EOPNOTSUPP, errno.EINVAL, 0):
            return None
        raise _oserr("acl_get_fd_np")
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


def _where(e: Entry) -> str:
    return e.path.decode("utf-8", "replace")


def _open_for(e: Entry, what: str, note: Callable[[str], None]) -> int | None:
    """:func:`_entry_fd`, with an entry that has no fd form here noted (a bound, L2 M2: such an entry's
    ACL / xattrs are neither recorded nor stripped; on macOS that is a socket or a device)."""
    fd = _entry_fd(e)
    if fd is None:
        note(f"{what} {_where(e)}: a {entry_type(e.st.st_mode)} has no fd form here; ACL and xattrs left as is")
    return fd


def darwin_meta(e: Entry, note: Callable[[str], None]) -> dict:
    """macOS: the extended ACL (``acl_to_text``), the BSD flags (``uchg`` included), and every xattr
    (``com.apple.FinderInfo``, ``com.apple.ResourceFork`` included), all on an fd of the entry itself.
    What cannot be read is noted and left out, never a wedge (plan §2i 4)."""
    rec: dict = {}
    where = _where(e)
    flags = getattr(e.st, "st_flags", 0)
    if flags:
        rec["flags"] = flags
    try:
        fd = _open_for(e, "meta", note)
    except OSError as err:
        note(f"meta {where}: not opened ({err.strerror or err}); ACL and xattrs not recorded")
        return rec
    if fd is None:
        return rec
    try:
        try:
            acl = _darwin_acl_text(fd)
            if acl:
                rec["acl"] = acl
        except OSError as err:
            note(f"meta {where}: ACL unreadable ({err.strerror or err})")
        try:
            names = _darwin_xattr_names(fd)
        except OSError as err:
            note(f"meta {where}: xattrs unlistable ({err.strerror or err})")
            names = []
        xs = _xattrs_record(names, lambda nm: _darwin_getxattr(fd, nm), where, note)
        if xs:
            rec["xattrs"] = xs
    finally:
        os.close(fd)
    return rec


def _proc_path(e: Entry) -> bytes:
    """Linux: the entry by its walk dirfd (``/proc/self/fd/<dirfd>/<name>``, used not following the last
    component), for the types with no fd form; short whatever the tree's depth."""
    return b"/proc/self/fd/%d/" % e.dirfd + e.name


def _lx(call: Callable, target: int | bytes, *args):
    return call(target, *args) if isinstance(target, int) else call(target, *args, follow_symlinks=False)


def linux_meta(e: Entry, note: Callable[[str], None]) -> dict:
    """Linux: every xattr (POSIX ACLs are the ``system.posix_acl_*`` xattrs), on an fd of the entry, or
    by its walk dirfd's ``/proc`` path for a symlink / socket / device."""
    where = _where(e)
    try:
        fd = _entry_fd(e)
    except OSError as err:
        note(f"meta {where}: not opened ({err.strerror or err}); xattrs not recorded")
        return {}
    target: int | bytes = fd if fd is not None else _proc_path(e)
    try:
        try:
            names = [os.fsencode(n) for n in _lx(os.listxattr, target)]
        except OSError as err:
            if err.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                note(f"meta {where}: xattrs unlistable ({err.strerror or err})")
            return {}
        xs = _xattrs_record(names, lambda nm: _lx(os.getxattr, target, nm), where, note)
        return {"xattrs": xs} if xs else {}
    finally:
        if fd is not None:
            os.close(fd)


def _clear_setid(fd: int, e: Entry) -> None:
    """L2 M1: setuid / setgid off a file or directory BEFORE root's chown (macOS keeps them on a root
    chown). Raises when it cannot: a set-id entry is never handed to the chown."""
    m = e.st.st_mode
    if (stat.S_ISREG(m) or stat.S_ISDIR(m)) and m & _SETID:
        os.fchmod(fd, stat.S_IMODE(m) & ~_SETID)


#: SIP's marker on the system-made var-folders dirs: root cannot remove it (EPERM on every END [measured:
#: levain-memory runs/s2-macos/RESULT_D1D2.md, NEW findings, macOS 26.6.2]) and it grants the slot nothing, so
#: the strip skips this one name rather than note its failure every time.
_ROOTLESS_XATTR = b"com.apple.rootless"


def darwin_strip(e: Entry, note: Callable[[str], None]) -> None:
    """Flags first (``uchg`` blocks the rest), then setuid / setgid (a gate: raises), then the ACL (an
    empty one set), then each xattr, all on an fd of the entry. A flag, ACL or xattr failure is noted and
    the strip carries on: it is hygiene, not a gate (plan §2i 4)."""
    where = _where(e)
    c = _darwin_libc()
    fd = _open_for(e, "strip", note)
    if fd is None:
        return
    try:
        if getattr(e.st, "st_flags", 0) and c.fchflags(fd, 0) != 0:
            note(f"strip {where}: flags not cleared ({_oserr('fchflags').strerror})")
        _clear_setid(fd, e)
        try:
            if _darwin_acl_text(fd):
                empty = c.acl_init(1)
                try:
                    if c.acl_set_fd_np(fd, empty, _ACL_TYPE_EXTENDED) != 0:
                        note(f"strip {where}: ACL not cleared ({_oserr('acl_set_fd_np').strerror})")
                finally:
                    c.acl_free(empty)
        except OSError as err:
            note(f"strip {where}: ACL unreadable ({err.strerror or err})")
        try:
            names = _darwin_xattr_names(fd)
        except OSError as err:
            note(f"strip {where}: xattrs unlistable ({err.strerror or err})")
            return
        for nm in names:
            if nm == _ROOTLESS_XATTR:
                continue
            if c.fremovexattr(fd, nm, 0) != 0:
                note(f"strip {where}: xattr {nm!r} not removed ({_oserr('fremovexattr').strerror})")
    finally:
        os.close(fd)


def linux_strip(e: Entry, note: Callable[[str], None]) -> None:
    where = _where(e)
    fd = _entry_fd(e)
    target: int | bytes = fd if fd is not None else _proc_path(e)
    try:
        if fd is not None:
            _clear_setid(fd, e)
        try:
            names = _lx(os.listxattr, target)
        except OSError as err:
            if err.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                note(f"strip {where}: xattrs unlistable ({err.strerror or err})")
            return
        for nm in names:
            try:
                _lx(os.removexattr, target, nm)
            except OSError as err:
                note(f"strip {where}: xattr {nm!r} not removed ({err.strerror or err})")
    finally:
        if fd is not None:
            os.close(fd)


def default_meta_reader() -> MetaReader:
    return darwin_meta if sys.platform == "darwin" else linux_meta


def default_strip() -> Strip:
    return darwin_strip if sys.platform == "darwin" else linux_strip


def iter_meta(root: Path, *, extra: Mapping[bytes, Path] | None = None, reader: MetaReader,
              note: Callable[[str], None], skip: Collection[tuple[int, int]] = (),
              depth_cap: int = MANIFEST_DEPTH) -> Iterator[dict]:
    """``{path_b64, acl?, flags?, xattrs?}`` for every entry that carries any of the three. ``skip``: the
    classified inodes (:func:`classify`, any type: outside-linked, or not a session uid's), whose metadata
    root never reads. DECISION: an entry with none gets no line, so meta stays within V17's 64 MiB beside a
    full manifest."""
    skip = frozenset(skip)
    for e in walk(root, extra, depth_cap=depth_cap):
        if (e.st.st_dev, e.st.st_ino) in skip:
            continue
        rec = reader(e, note)
        if rec:
            yield {"path_b64": _b64(e.path), **rec}


# --- chown + the outside-link copies ---------------------------------------------------------------

Chown = Callable[[int, bytes, int, int], None]

#: The errnos of a placement the filesystem refuses for good (an immutable entry or directory: EPERM / EACCES;
#: a link count at its limit: EMLINK): the one class of placement failure that is noted, not raised.
_PLACE_REFUSED = frozenset({errno.EPERM, errno.EACCES, errno.EMLINK})


def os_chown(dirfd: int, name: bytes, uid: int, gid: int) -> None:
    """``fchownat(dirfd, name, uid, gid, AT_SYMLINK_NOFOLLOW)``: a link's own owner, never its target's."""
    os.chown(name, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def _place_link_copies(facts: EndFacts, tree: Path, note: Callable[[str], None], depth_cap: int) -> None:
    """Each in-tree link of an outside-linked inode becomes a hard link to that inode's staged copy (the
    manifest step's: the bytes the session uid read and hashed), so an in-tree link set stays one inode
    and the outside file is no longer shared (root never chowns a shared inode, plan §2g 2). Redone after a
    crash: an entry already placed (its inode is a staged copy's) is left and its directory fsynced before
    any staging copy is unlinked, a temp link this step made and did not rename is removed, and the staging
    dir goes last. Only a placement the filesystem REFUSES
    (:data:`_PLACE_REFUSED`: an immutable directory or file, a link count at its limit) is noted, and that
    entry stays shared: unstripped, unchowned. Every other failure (ENOSPC, EIO, a failed fsync) propagates,
    so the tree phase stays journaled and the reserve's retry runs (codex r1 7); a temp link whose rename
    failed is removed first, so none is left in the retained tree."""
    sdir = facts.retained / LINKCOPY
    try:
        sfd = os.open(sdir, _DIR_FLAGS)
    except FileNotFoundError:
        return
    try:
        staged: dict[bytes, os.stat_result] = {}
        for n in os.listdir(sfd):
            nb = os.fsencode(n)
            st = os.stat(nb, dir_fd=sfd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                staged[nb] = st
        copies = {(st.st_dev, st.st_ino) for st in staged.values()}
        synced: set[bytes] = set()   # the tree dirs (by path) fsynced since their last placement
        for e in walk(tree, depth_cap=depth_cap):
            if not stat.S_ISREG(e.st.st_mode):
                continue
            key = (e.st.st_dev, e.st.st_ino)
            parent = os.path.dirname(e.path)
            if key in copies:
                if e.name.startswith(_LINK_TMP):
                    os.unlink(e.name, dir_fd=e.dirfd)
                    os.fsync(e.dirfd)
                    synced.add(parent)
                elif parent not in synced:
                    # Placed by an earlier pass whose dir fsync may have failed (comp r2 N1): made durable
                    # here, before the staging copies it links to are unlinked below.
                    os.fsync(e.dirfd)
                    synced.add(parent)
                continue
            src = _staged_name(key)
            if src not in staged:
                continue
            tmp = _LINK_TMP + secrets.token_hex(8).encode("ascii")
            try:
                os.link(src, tmp, src_dir_fd=sfd, dst_dir_fd=e.dirfd)
            except OSError as err:
                if err.errno not in _PLACE_REFUSED:
                    raise
                note(f"link {e.abspath!r}: not replaced by its copy ({err.strerror or err}); left unstripped, "
                     f"unchowned")
                continue
            try:
                os.rename(tmp, e.name, src_dir_fd=e.dirfd, dst_dir_fd=e.dirfd)
            except OSError as err:
                try:
                    os.unlink(tmp, dir_fd=e.dirfd)
                except FileNotFoundError:   # comp r2 L3: never masks the rename's errno
                    pass
                if err.errno not in _PLACE_REFUSED:
                    raise
                note(f"link {e.abspath!r}: not replaced by its copy ({err.strerror or err}); left unstripped, "
                     f"unchowned")
                continue
            os.fsync(e.dirfd)
            synced.add(parent)
        for nb in staged:
            os.unlink(nb, dir_fd=sfd)
        os.fsync(sfd)
    finally:
        os.close(sfd)
    os.rmdir(sdir)
    fsync_dir(facts.retained)


# --- seams -----------------------------------------------------------------------------------------


def unbuilt_freeze(uid: int, bound_s: float) -> bool:
    """Freeze every process of ``uid`` within ``bound_s`` (V12); ``True`` only when that is PROVEN, and
    past the bound is not proven. END calls it once per uid the session holds. This default refuses, which
    lands HELD; the real seam is :func:`levain.slot.proc.end_seams` ``(host, cgroup)``'s, and how it
    proves the freeze is proc's to say."""
    raise Refused("seam-unbuilt", "freeze: pass levain.slot.proc.end_seams(host, cgroup)")


def unbuilt_kill(uid: int, bound_s: float) -> bool:
    """SIGKILL every process of ``uid`` within ``bound_s`` (V12); ``True`` only when that is PROVEN. This
    default refuses (HELD); the real seam is :func:`levain.slot.proc.end_seams`'s."""
    raise Refused("seam-unbuilt", "kill: pass levain.slot.proc.end_seams(host, cgroup)")


def unbuilt_no_process(uid: int) -> bool:
    """The proof END rests on: ``True`` only when no process of ``uid`` exists. This default refuses
    (HELD); the real seam is :func:`levain.slot.proc.end_seams`'s."""
    raise Refused("seam-unbuilt", "no_process: pass levain.slot.proc.end_seams(host, cgroup)")


def sync_tree(tree: Path) -> None:
    """The tree step's metadata (setid bits cleared, xattrs / ACLs / flags stripped, owners root) durable
    before the journal leaves ``tree`` (codex r1 5). DECISION: one filesystem-wide flush rather than an fsync
    per mutated inode (V1 allows 2 M). Linux: ``syncfs(2)`` on the tree's filesystem (libc's; ``sync(2)`` where
    libc has none). macOS: ``sync(2)``, then ``F_FULLFSYNC`` on the tree directory, which also asks the
    device to flush its write cache. A failure raises (HELD at ``tree``, redone)."""
    fd = os.open(tree, _DIR_FLAGS)
    try:
        if sys.platform == "darwin":
            os.sync()
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)   # type: ignore[attr-defined]
            return
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        syncfs = getattr(libc, "syncfs", None)
        if syncfs is None:
            os.sync()
            return
        syncfs.argtypes = [ctypes.c_int]
        if syncfs(fd) != 0:
            raise _oserr("syncfs")
    finally:
        os.close(fd)


def read_slot_state(path: Path) -> bytes | None:
    """Root's own slot state file (§3.5 Pool, root 0644 in a root dir), ``None`` when absent; a link or a
    non-regular file refuses."""
    return read_record(path, max_bytes=4096)


@dataclass
class EndSeams:
    cas: Cas
    freeze: Callable[[int, float], bool] = unbuilt_freeze
    kill: Callable[[int, float], bool] = unbuilt_kill
    no_process: Callable[[int], bool] = unbuilt_no_process
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], str] = utc_now
    meta_reader: MetaReader = field(default_factory=default_meta_reader)
    strip: Strip = field(default_factory=default_strip)
    chown: Chown = os_chown
    sync: Callable[[Path], None] = sync_tree
    run_as: SessionRunAs = fork_run_as
    slot_state: Callable[[Path], "bytes | None"] = read_slot_state
    root_uid: int = 0
    root_gid: int = 0
    fault: Fault = _no_fault
    note: Callable[[str], None] = lambda msg: None
    reserve: "Reserve | None" = None
    owner_ok: Callable[[os.stat_result], bool] = _root_owned

    @property
    def retry(self) -> Callable[[Callable[[], None]], None]:
        return self.reserve.retry if self.reserve is not None else (lambda fn: fn())


# --- CONTROL's CAS (L2 M5) --------------------------------------------------------------------------


class CasNotSent(Exception):
    """A CAS CONTROL did not take: never a step failure and never HELD. The record stays where it is; every
    proof and act END runs is idempotent, so the next pass redoes them and sends it again."""

    def __init__(self, target: str, cause: BaseException) -> None:
        self.target, self.cause = target, cause
        super().__init__(f"CAS {target} not sent: {cause}")


def send_cas(seams: EndSeams, target: str, fields: Mapping[str, object]) -> None:
    """``seams.cas(target, fields)`` under §3.3's client rule: ``locked`` (a try-lock collision) and a
    CONTROL that is down (``OSError``) are retried with backoff for up to :data:`CAS_RETRY_SECONDS`;
    ``stale-generation`` is retried once (the seam reads the record's generation on every call). Anything
    else, or the retries spent, raises :class:`CasNotSent`. A CAS out of HELD (any target but HELD) also
    clears ``failed_step`` and ``held_attempts`` (L1-F4: CONTROL merges, so an unnamed field survives)."""
    if target != HELD:
        fields = {"failed_step": None, "held_attempts": None, **fields}
    t0 = seams.clock()
    delay = CAS_BACKOFF_FIRST
    stale_retried = False
    while True:
        try:
            seams.cas(target, fields)
            return
        except Refused as e:
            if e.name == "stale-generation" and not stale_retried:
                stale_retried = True
                continue
            if e.name != "locked":
                raise CasNotSent(target, e) from None
            err: BaseException = e
        except OSError as e:
            err = e
        if seams.clock() - t0 + delay > CAS_RETRY_SECONDS:
            raise CasNotSent(target, err) from None
        seams.sleep(delay)
        delay = min(delay * 2, CAS_BACKOFF_MAX)


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
    """The manifest of the frozen tree, durable, before any mutation. First the ONE classification
    (:func:`classify`), stored as ``classes.jsonl``. An :data:`OUTSIDE` inode (L2 H2) is read once, by the
    session-uid reader, into ``L/retained/<s>/linkcopy/<dev>-<ino>`` (the copy keeps its mode less
    setuid/setgid; an UNREAD one is an empty 0600 placeholder); an :data:`UNOWNED` file is read once by it and
    hashed, never copied (codex r1 4); that read is what the manifest records, and an UNOWNED file it cannot
    read refuses ``unowned-unreadable`` (codex r2 N2: its real bytes stay in the tree, so UNREAD's size 0
    would be a false record). The copy cap is checked
    before anything is read, and is ONE budget across every reader: each gets only what the reads before it
    left (codex r1 8), so files an outside holder grows after the sum cannot stage past it."""
    classes, outside = classify(facts.live, facts.extra, facts.uids, depth_cap=caps.depth)
    copy = outside_link_bytes(outside)
    if copy > caps.link_copy_bytes:
        raise Refused("link-copy-cap", f"{copy} bytes hard-linked outside the tree > {caps.link_copy_bytes}")
    _clear_linkcopy(facts, seams)
    _write_classes(facts.retained / CLASSES, classes, caps.meta_bytes, caps.depth)
    sfd = -1
    if outside:
        os.mkdir(facts.retained / LINKCOPY, 0o700)
        fsync_dir(facts.retained)
        sfd = os.open(facts.retained / LINKCOPY, _DIR_FLAGS)
    staged: dict[tuple[int, int], tuple[int, str]] = {}
    copied = 0

    def read_foreign(e: Entry, budget: _Budget) -> tuple[int, str]:
        nonlocal copied
        key = (e.st.st_dev, e.st.st_ino)
        if key not in staged:
            if classes[key] == OUTSIDE:
                out = os.open(_staged_name(key), _O_NEW, 0o600, dir_fd=sfd)
                try:
                    got = read_as_session(facts, seams, e, out, caps.link_copy_bytes - copied, budget)
                    os.fchmod(out, 0o600 if got == UNREAD else stat.S_IMODE(e.st.st_mode) & ~_SETID)
                    os.fsync(out)
                finally:
                    os.close(out)
                copied += got[0]
            else:
                got = read_as_session(facts, seams, e, None, HASH_ONLY_CAP, budget)
                if got == UNREAD:
                    # codex r2 N2: an UNOWNED inode has no placeholder; it stays in the tree, so a size-0,
                    # digest-"" line would misdescribe the bytes retained. Fail closed: HELD, named.
                    raise Refused("unowned-unreadable",
                                  f"{e.abspath!r}: no session uid can read it, and it is not copied")
            if got == UNREAD:
                seams.note(f"{classes[key]} {e.abspath!r}: the session uid cannot read it; recorded, not copied")
            staged[key] = got
        return staged[key]

    try:
        _stream_jsonl(facts.retained / "manifest.jsonl",
                      iter_manifest(facts.live, extra=facts.extra, caps=caps, clock=seams.clock,
                                    credentials=facts.parked_paths, foreign=classes, read_foreign=read_foreign),
                      caps.manifest_bytes, "manifest-byte-cap")
        if sfd >= 0:
            os.fsync(sfd)
    finally:
        if sfd >= 0:
            os.close(sfd)
    seams.fault("retain:manifest:written")


def _meta_step(facts: EndFacts, seams: EndSeams, caps: Caps) -> None:
    """The metadata of every entry the manifest step's classification left to root: the STORED one, never a
    recompute (codex r1 4: an outside link removed between the two steps cannot hand root the inode), walked
    under the depth the manifest step accepted."""
    classes, depth = stored_classification(facts)
    _stream_jsonl(facts.retained / "meta.jsonl",
                  iter_meta(facts.live, extra=facts.extra, reader=seams.meta_reader, note=seams.note,
                            skip=classes, depth_cap=depth),
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
    ``tree/.levain-var-folders/``), link the staged copies over the outside-linked files, strip (setuid /
    setgid included), then chown root, then flush it all (:attr:`EndSeams.sync`) before the journal may
    leave ``tree`` (codex r1 5). Each sub-step is idempotent; a redo whose rename already happened still
    fsyncs both parents before going on (codex r1 6). Every session uid is proven dead, so nothing changes
    under the walks, and every call is on a walk dirfd or an fd of the entry (L2 M2). An inode a path outside
    the tree still shares (a copy that could not be placed) is never stripped or chowned, and a shared
    non-regular one refuses ``tree-shared-special`` (comp r1 4). An :data:`UNOWNED` inode has only its
    setuid / setgid cleared, by fd: root reads none of its metadata (codex r1 4). Every walk takes the depth the
    manifest step accepted, so no depth refusal can land on a renamed tree; the classification is read before
    the rename, so a missing one refuses with nothing moved."""
    classes, depth = stored_classification(facts)
    tree = facts.tree
    if os.path.lexists(facts.live):
        if os.path.lexists(tree):
            raise Refused("retain-conflict", f"{facts.live} and {tree} both exist")
        _rename_durable(facts.live, tree)
    elif not (os.path.isdir(tree) and not os.path.islink(tree)):
        raise Refused("tree-missing", str(tree))
    else:
        fsync_dir(facts.live.parent)   # the rename happened; its durability may not have
        fsync_dir(tree.parent)
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
    elif vf is not None and os.path.isdir(dest) and not os.path.islink(dest):
        if os.path.isdir(vf.parent):
            fsync_dir(vf.parent)
        fsync_dir(dest.parent)
    seams.fault("retain:tree:var-folders")

    # DECISION: the outside-link copies are placed before the strip (§7.4 names rename, strip, fchown),
    # so the strip never clears flags or xattrs on an inode a path outside the tree still shares.
    _place_link_copies(facts, tree, seams.note, depth)
    seams.fault("retain:tree:links")

    shared = foreign_links(tree, depth_cap=depth)
    special = [k for k, lc in shared.items() if not lc.regular]
    if special:
        raise Refused("tree-shared-special", f"inode {special[0][0]}-{special[0][1]} is linked outside the tree")
    for e in walk(tree, depth_cap=depth):
        key = (e.st.st_dev, e.st.st_ino)
        if key in shared:
            continue
        if classes.get(key) == UNOWNED:
            _clear_setid_only(e)
        else:
            seams.strip(e, seams.note)
    seams.fault("retain:tree:stripped")

    rfd = os.open(facts.retained, _DIR_FLAGS)
    try:
        seams.chown(rfd, b"tree", seams.root_uid, seams.root_gid)
    finally:
        os.close(rfd)
    for e in walk(tree, depth_cap=depth):
        if (e.st.st_dev, e.st.st_ino) not in shared:
            seams.chown(e.dirfd, e.name, seams.root_uid, seams.root_gid)
    fsync_dir(tree)
    seams.sync(tree)


def _clear_setid_only(e: Entry) -> None:
    """An :data:`UNOWNED` entry's strip: setuid / setgid off by fd before root's chown (a gate, as in the
    full strip), no xattr, ACL or flag read. DECISION: its other metadata stays as found."""
    m = e.st.st_mode
    if not ((stat.S_ISREG(m) or stat.S_ISDIR(m)) and m & _SETID):
        return
    fd = _entry_fd(e)
    if fd is None:
        return
    try:
        _clear_setid(fd, e)
    finally:
        os.close(fd)


def _commit_step(facts: EndFacts, seams: EndSeams) -> None:
    """Root's ``retained_at`` (0644; the dispatch retention age clock, §3.5), written once: a redo
    keeps the first one, so HELD or crash time never moves it. First the classification goes: the journal is
    past ``tree`` (forward only), so no step that reads it can run again, and the retained object is §7.4's
    list exactly."""
    unlink_durable(facts.retained / CLASSES)
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


def confirmed_relogin(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> bool:
    """IG-1: whether this session is a ``login`` whose ``confirm-identity yes`` was given. Root's facts only: alloc's
    kind ``login`` on a session that parks, and root's baseline file (a regular file, not followed, the expected owner,
    at most :data:`BASELINE_MAX_BYTES`) passing :func:`levain.slot.identity.check_baseline` whole and naming THIS
    session and vendor. ``confirm-identity yes`` writes it under ``L/alloc/<s>.lock``, the lock END holds, and only
    while the record is running, so no yes can land while END reads it. Anything else is False."""
    if facts.kind != "login" or not facts.parks:
        return False
    # identity imports runtime_helper, which imports this module: a top-level import here would be a cycle.
    from levain.slot import identity as ID
    try:
        fd = os.open(facts.slot.slot_dir(facts.store) / ID.BASELINE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not owner_ok(st) or st.st_size > BASELINE_MAX_BYTES:
            return False
        data = os.read(fd, BASELINE_MAX_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        obj = ID.check_baseline(canon.parse(data))
    except (ContractError, Refused):
        return False
    return obj["session"] == facts.session and obj["vendor"] == facts.vendor


def _own_dir(path: Path, owner_ok: Callable[[os.stat_result], bool]) -> None:
    """``path`` made root 0700 (its parent fsynced) or, when it exists, checked a real directory with the expected
    owner (a planted link is never followed): else ``bad-root``."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or not owner_ok(st):
            raise Refused("bad-root", f"{path} is not a directory with the expected owner") from None
        return
    fsync_dir(path.parent)


def _set_aside(facts: EndFacts, kfd: int, name: bytes, owner_ok: Callable[[os.stat_result], bool],
               fault: Fault) -> None:
    """IG-1: the parked credential a confirmed re-login replaces, renamed (never opened) from ``keep/`` into
    ``L/retained/<s>/replaced/``, both directories then fsynced. A ``replaced/`` entry already there is never
    replaced (``replaced-occupied``)."""
    _own_dir(facts.retained, owner_ok)
    _own_dir(facts.replaced, owner_ok)
    fault("park:replace:dirs")
    rfd = os.open(facts.replaced, _DIR_FLAGS)
    try:
        try:
            os.stat(name, dir_fd=rfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused("replaced-occupied", f"{facts.replaced}/{name.decode('ascii')} already holds a file")
        os.rename(name, name, src_dir_fd=kfd, dst_dir_fd=rfd)
        os.fsync(kfd)
        os.fsync(rfd)
    finally:
        os.close(rfd)


def park_move(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned,
              fault: Fault = _no_fault) -> None:
    """Move the session vendor's credential file to ``keep/`` by rename, never opening it, on a
    passthrough session only (L1-F1): on any other, a file at a credential path is ordinary work, retained.
    Only a non-directory is moved; absent = already parked or never made. A ``keep/`` entry already there
    is never replaced (``keep-occupied``, HELD park), so a planted file cannot overwrite a parked login;
    ``keep/`` is root-only, so the check and the rename cannot race. The one exception is a
    :func:`confirmed_relogin` (IG-1): the parked entry is first set aside into ``replaced/`` (:func:`_set_aside`),
    then the new login is parked. In that order, a death at any point leaves the new login in the session tree or
    in ``keep/`` (the old one in ``keep/`` or ``replaced/``); a redo after the set-aside finds ``keep/`` empty and
    parks, and one after the park finds the source gone.
    keep/ (root 0700, install's) is made if it is missing (DECISION). A redo that finds the source gone and
    ``keep/`` holding it (a rename whose fsyncs did not finish) still fsyncs both directories (codex r1 6)."""
    cred = facts.credential
    if cred is None or not os.path.isdir(facts.live):
        return
    parts, name = cred
    dfd = _open_subdir(facts.live, parts)
    if dfd is None:
        return
    try:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            if os.path.lexists(facts.keep / os.fsdecode(name)):
                os.fsync(dfd)
                fsync_dir(facts.keep)
            return
        if stat.S_ISDIR(st.st_mode):
            return
        try:
            os.mkdir(facts.keep, 0o700)
        except FileExistsError:
            pass
        kfd = os.open(facts.keep, _DIR_FLAGS)
        try:
            try:
                os.stat(name, dir_fd=kfd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not confirmed_relogin(facts, owner_ok=owner_ok):
                    raise Refused("keep-occupied",
                                  f"{facts.keep}/{name.decode('ascii')} already holds a credential")
                _set_aside(facts, kfd, name, owner_ok, fault)
                fault("park:replace:aside")
            os.rename(name, name, src_dir_fd=dfd, dst_dir_fd=kfd)
            os.fsync(dfd)
            os.fsync(kfd)
        finally:
            os.close(kfd)
    finally:
        os.close(dfd)


def park_plan(facts: EndFacts, seams: "EndSeams | None" = None) -> Plan:
    # DECISION: park's `commit` is its CAS; the step itself re-syncs keep/ so a redo is durable.
    owner_ok = seams.owner_ok if seams is not None else _root_owned
    fault = seams.fault if seams is not None else _no_fault
    return Plan("park", (Phase("move", lambda: park_move(facts, owner_ok=owner_ok, fault=fault)),
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
    #: how long a release waits for the reaper's refill lock before it refuses (a test shortens it).
    lock_wait_s: float = SLOT_LOCK_WAIT_S

    @property
    def step(self) -> int:
        return max(1, self.size // 8)

    def current(self) -> int:
        try:
            return os.lstat(self.path).st_size
        except FileNotFoundError:
            return 0

    def release_step(self) -> int:
        """Shrink by one step; returns the bytes released (0 = nothing left to give). Never call it from a
        process holding the reaper's reserve lock: flock is per open file description, so it would wait on
        itself (now a bounded ``reserve-locked`` refusal)."""
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return 0
        try:
            # The reaper's refill holds a flock on this same file across its fstat, write and fsync; taking
            # it here means a release never truncates under a refill (the unlock is the close below). The
            # wait is bounded like END's other lock waits, else a stalled reaper would pin the transition.
            deadline = time.monotonic() + self.lock_wait_s
            delay = SLOT_LOCK_FIRST_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Refused("reserve-locked", f"{self.path} held past {self.lock_wait_s:g} s") from None
                    time.sleep(delay)
                    delay = min(delay * 2, SLOT_LOCK_MAX_S)
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
        created = not os.path.lexists(self.path)
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
        if created:   # the new name is durable too, or a crash leaves no reserve to give (glm r1 LOW)
            fsync_dir(self.path.parent)
        return cur >= self.size


# --- root's facts beside alloc: the clone marker, the slot check, the END lock ---------------------


def cloned_path(store: Path, session: str) -> Path:
    """DECISION: ``L/alloc/<session>.cloned`` (root 0600), beside the alloc record."""
    return intent_path(Path(store) / "alloc", session).with_name(f"{session}.cloned")


def mark_cloned(store: Path, session: str, *, now: Callable[[], str] = utc_now) -> None:
    """``start``'s root fact that the clone ran (L2 H1, L1-F3; §5.4 3: a failed clone refuses
    ``clone-failed``), written only after the clone succeeded. It holds the time it was written."""
    atomic_write(cloned_path(store, session), (now() + "\n").encode("ascii"), 0o600)


def repo_cloned(facts: EndFacts, *, owner_ok: Callable[[os.stat_result], bool] = _root_owned) -> bool:
    """Whether the session ever cloned: root's marker (:func:`mark_cloned`) exists. Never inferred from
    ``<S>/repo``, which the slot can empty: with the marker, whatever is there is retained, an empty repo
    included; without it the session never cloned and retains nothing (CONTRACT §7.3)."""
    try:
        st = os.lstat(facts.cloned_marker)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode) or not owner_ok(st):
        raise Refused("bad-root", f"{facts.cloned_marker} is not a regular file with the expected owner")
    return True


def check_slot_ours(facts: EndFacts, seams: EndSeams) -> None:
    """L2 M4, before anything is signalled: root's own slot state file (§3.5 Pool) shows this slot
    ``leased`` to THIS session at alloc's ``lease_seq``. Absent, unreadable, malformed, another session,
    another lease or not leased = ``slot-not-ours``."""
    try:
        data = seams.slot_state(facts.slot_state)
        st = parse_slot_state(data) if data is not None else None
    except (Refused, OSError) as e:
        raise Refused("slot-not-ours", f"{facts.slot_state} unreadable ({e})") from None
    if st is None:
        raise Refused("slot-not-ours", f"no {facts.slot_state}")
    if (st.slot != facts.slot or st.state != "leased" or st.session != facts.session
            or facts.lease_seq is None or st.lease_seq != facts.lease_seq):
        raise Refused("slot-not-ours", f"{facts.slot.tag} is {st.state} to {st.session} at lease {st.lease_seq}")


@contextmanager
def slot_lock(facts: EndFacts, seams: EndSeams) -> Iterator[bool]:
    """The slot's ``L/slots/{e,v}<n>/lock`` flock (§3.5), taken BEFORE :func:`end_lock` (the order pool ->
    slot -> session END lock; END never holds the pool lock), and held across the slot check, the freeze,
    kill and no-process proofs and every act after them (codex r1 2). SCRUBBED takes the same lock FIRST,
    re-checks the lease under it and only TRIES the pool lock after its kills (a lost TRY releases the slot lock and
    leaves the slot leased), never pool -> slot: a SCRUB waiting on the slot lock while holding the pool lock would
    stall every ``prepare`` behind END (reaper DESIGN Q2). So a slot END proved ours cannot be released and re-leased
    while END signals its uids. prepare only TRIES it under the pool lock, so a slot END holds is passed over
    and prepare never waits on END. Waited for up to :data:`SLOT_LOCK_WAIT_S`, else ``slot-locked`` (raised,
    never HELD: the caller retries). Yields whether the slot exists: DECISION no slot directory means no slot
    state either, so END lands ``slot-not-ours`` with nothing signalled. A lock file missing from a slot
    directory is made (root 0600, the file install makes)."""
    path = facts.slot_lock
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileNotFoundError:
        yield False
        return
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    _LOCK_FDS.add(fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        deadline = None
        delay = SLOT_LOCK_FIRST_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = seams.clock()
                deadline = now + SLOT_LOCK_WAIT_S if deadline is None else deadline
                if now >= deadline:
                    raise Refused("slot-locked", f"{facts.slot.tag} held past {SLOT_LOCK_WAIT_S:g} s") from None
                seams.sleep(delay)
                delay = min(delay * 2, SLOT_LOCK_MAX_S)
        yield True
    finally:
        _LOCK_FDS.discard(fd)
        os.close(fd)   # closing the only descriptor releases the flock


def retained_whole(facts: EndFacts, seams: EndSeams) -> bool:
    """Whether root's OWN retained object for the session exists and is whole: ``L/retained/<s>/`` a real
    directory, ``tree/`` a real directory, ``object.json`` a regular file that parses as THIS session's
    ``levain-retained-object/1``, ``retained_at`` a regular file holding one contract timestamp, each with
    the expected owner (root). Only this lets END take a RETAINED record at its word (codex r1 1)."""
    r = facts.retained
    try:
        for p, ok in ((r, stat.S_ISDIR), (r / "tree", stat.S_ISDIR), (r / "object.json", stat.S_ISREG),
                      (r / "retained_at", stat.S_ISREG)):
            st = os.lstat(p)
            if not ok(st.st_mode) or not seams.owner_ok(st):
                return False
        obj = canon.parse(_read_durable(r / "object.json"))
        stamp = _read_durable(r / "retained_at")
    except (OSError, ContractError):
        return False
    return (isinstance(obj, dict) and obj.get("format") == OBJECT_FORMAT and obj.get("session") == facts.session
            and stamp.endswith(b"\n") and TIMESTAMP_RE.fullmatch(stamp[:-1].decode("ascii", "replace")) is not None)


@contextmanager
def end_lock(facts: EndFacts, seams: EndSeams) -> Iterator[None]:
    """Root's per-session lock (L2 M7): one END or reconciliation per session at a time. Waited for up to
    :data:`END_LOCK_WAIT_S`, else ``end-busy`` with the record UNCHANGED, never HELD (the reaper's next pass or the
    helper retries): ``start`` holds this lock across every spawn and every kill it makes, so an END that waits here
    cannot prove while a ``start`` is still launching or abandoning in the slot (reaper DESIGN RH1, Q-F1). Its
    holder may also be a second END of the session, which a timed-out END must not write over. The open is
    ``O_CLOEXEC``, so no exec'd child keeps a copy of it. Under it, a dead writer's temp files of what END writes
    (the journal's ``L/alloc/.<s>.intent.json.*.tmp``, ``L/retained/<s>/.*.tmp``) are swept: no live writer
    can own one. ``start``'s clone-marker temp is not END's to sweep."""
    path = facts.lock
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    _LOCK_FDS.add(fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        deadline = None
        delay = SLOT_LOCK_FIRST_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = seams.clock()
                deadline = now + END_LOCK_WAIT_S if deadline is None else deadline
                if now >= deadline:
                    raise Refused("end-busy", f"{facts.session} held past {END_LOCK_WAIT_S:g} s") from None
                seams.sleep(delay)
                delay = min(delay * 2, SLOT_LOCK_MAX_S)
        sweep_temps(facts.alloc_dir, facts.intent.name, owner_ok=seams.owner_ok)
        if os.path.isdir(facts.retained) and not os.path.islink(facts.retained):
            sweep_temps(facts.retained, "", owner_ok=seams.owner_ok)
        yield
    finally:
        _LOCK_FDS.discard(fd)
        os.close(fd)   # closing the only descriptor releases the flock


# --- the END driver --------------------------------------------------------------------------------

END_STEPS = ("freeze", "kill", "park", "retain")
_START = {REVOKED: 0, FROZEN: 1, DEAD: 2, CREDENTIAL_PARKED: 3}
_RESUME = {"slot-not-ours": 0, "freeze": 0, "kill": 1, "park": 2, "manifest_budget": 3,
           **{p: 3 for p in INTENT_PHASES["retain"]}}


#: The retain phases that run before root's first act on the tree: only a budget refusal raised in one of them
#: is ``manifest_budget``, whose exit (END again with larger caps) assumes the tree as the session left it.
_BUDGET_PHASES = frozenset({"manifest", "meta"})


def _held_name(intent: str, e: PhaseFailed) -> str:
    if intent == "park":
        return "park"
    if isinstance(e.cause, Refused) and e.cause.name in BUDGET_REFUSALS and e.phase in _BUDGET_PHASES:
        return "manifest_budget"
    return e.phase


def _plan_for(facts: EndFacts, seams: EndSeams, caps: Caps) -> Callable[[RootIntent], Plan]:
    def plan_for(ri: RootIntent) -> Plan:
        return retain_plan(facts, seams, ri, caps=caps) if ri.intent == "retain" else park_plan(facts, seams)
    return plan_for


def _reconcile(facts: EndFacts, seams: EndSeams, caps: Caps) -> RootIntent | None:
    return reconcile(facts.intent, _plan_for(facts, seams, caps), cas=lambda t, f: send_cas(seams, t, f),
                     fault=seams.fault, retry=seams.retry, owner_ok=seams.owner_ok)


#: Per intent, the journal phases from which reconciling will run a step that reads or changes the session's
#: ``live/``: a journal at one of them is finished only after :func:`reconcile_session` proves the slot ours
#: and every session uid dead again (comp r1 3). The reconciler runs every LATER phase too, so the test is
#: the phases still to run, not the one named: a retain ``init`` journal goes on to the manifest and the
#: tree (codex r2 O3), and park's ``move`` renames the credential out of ``live/`` (comp r2 L2). Only
#: ``commit`` is left out (retain's writes root's ``retained_at``, park's syncs ``keep/``; both then CAS):
#: a crash between the CAS and the journal delete leaves a ``commit`` journal that SCRUBBED may already
#: have outlived, and finishing it must not need a slot that is no longer the session's.
PROVEN_PHASES: Mapping[str, tuple[str, ...]] = {
    "retain": tuple(p for p in INTENT_PHASES["retain"] if p != "commit"),
    "park": tuple(p for p in INTENT_PHASES["park"] if p != "commit"),
}


def reconcile_session(facts: EndFacts, seams: EndSeams, *, caps: Caps = Caps(),
                      held_attempts: int | None = None) -> RootIntent | None:
    """The reaper's step 2 / helper start for one session: finish its journal, from the journal and
    the filesystem only, under the slot lock then root's per-session lock (:func:`slot_lock`). A journal
    at one of its intent's :data:`PROVEN_PHASES` is finished only after the proofs END runs (the slot ours,
    then freeze, kill and no process for EVERY session uid): the journal outlives END's own proofs (HELD
    at ``tree``, a later pass), and a process of a session uid could have appeared since (comp r1 3). A proof
    that fails CASes HELD with that step as ``failed_step`` (``held_attempts`` one past the record's), as
    :func:`end_session` does, and raises its refusal: nothing is reconciled. Raises :class:`PhaseFailed`
    (journal left at the phase), ``Refused`` (``end-busy`` and ``slot-locked`` included) or
    :class:`CasNotSent`."""
    with slot_lock(facts, seams) as locked, end_lock(facts, seams):
        ri = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if ri is not None and ri.phase in PROVEN_PHASES.get(ri.intent, ()):
            step = "slot-not-ours"
            try:
                if not locked:
                    raise Refused("slot-not-ours", f"no {facts.slot.slot_dir(facts.store)}")
                check_slot_ours(facts, seams)
                step = "freeze"
                _prove_frozen(facts, seams)     # best-effort (H1): the kill and its proof are the test
                step = "kill"
                _prove_dead(facts, seams)
            except Exception as e:   # noqa: BLE001 - an unproven step is HELD
                seams.note(f"reconcile {facts.session}: {step} not proven: {e}")
                attempts = held_attempts if isinstance(held_attempts, int) and held_attempts >= 0 else 0
                send_cas(seams, HELD, {"failed_step": step, "held_attempts": attempts + 1})
                raise
        try:
            return _reconcile(facts, seams, caps)
        except PhaseFailed as e:
            if isinstance(e.cause, CasNotSent):
                raise e.cause from None
            raise


def _prove(ok: Callable[[], bool], what: str, seams: EndSeams, bound: float | None) -> None:
    t0 = seams.clock()
    if not ok():
        raise Refused(f"{what}-unproven", f"the {what} seam did not prove it")
    if bound is not None and seams.clock() - t0 > bound:
        raise Refused(f"{what}-unproven", f"past the {bound:g} s bound")


def _prove_frozen(facts: EndFacts, seams: EndSeams) -> bool:
    """The freeze of every session uid, BEST-EFFORT (reaper H1): each bounded to a quarter of V12, and whatever it
    answers the kill runs next. Processes of a uid can resume each other (``SIGCONT`` from the uid itself), so a
    freeze that never proves must not be what keeps them alive; the kill and the no-process proof are END's test.
    Returns whether every freeze proved; a freeze that did not is noted."""
    bound = V12_SECONDS / 4
    ok = True
    for uid in facts.uids:
        try:
            _prove(lambda u=uid: seams.freeze(u, bound), "freeze", seams, bound)
        except Exception as e:   # noqa: BLE001 - the kill runs whatever the freeze says
            seams.note(f"END {facts.session}: the freeze of uid {uid} did not prove ({e}); the kill runs")
            ok = False
    return ok


def _prove_dead(facts: EndFacts, seams: EndSeams) -> None:
    for uid in facts.uids:
        _prove(lambda u=uid: seams.kill(u, V12_SECONDS), "kill", seams, V12_SECONDS)
    for uid in facts.uids:
        _prove(lambda u=uid: seams.no_process(u), "kill", seams, None)


def end_session(facts: EndFacts, seams: EndSeams, *, record_state: str, failed_step: str | None = None,
                held_attempts: int | None = None, caps: Caps = Caps()) -> str:
    """Drive one session through END from ``record_state`` (and ``failed_step`` for HELD); returns the
    state it leaves the record in (RETAINED or HELD). Every proof and idempotent act runs every call;
    the record's state only chooses which CAS edges are sent (see the module docstring). A landing in
    HELD sends ``held_attempts`` one past the record's (§7.3: the reaper's backoff and doctor read it).
    Raises ``end-busy`` (another END or reconciliation holds the session), ``slot-locked`` and
    :class:`CasNotSent`. A RETAINED record is taken at its word only with root's own retained object whole
    (:func:`retained_whole`); otherwise END starts as from REVOKED (codex r1 1): the proofs run, and CONTROL
    refuses the FROZEN edge out of RETAINED (:class:`CasNotSent`), so teardown never reaches SCRUBBED."""
    if record_state == RETAINED:
        if retained_whole(facts, seams):
            return RETAINED
        seams.note(f"END {facts.session}: the record says RETAINED and root holds no whole retained object")
        start: int | None = _START[REVOKED]
    else:
        start = _RESUME.get(failed_step or "") if record_state == HELD else _START.get(record_state)
    if start is None:
        raise Refused("not-ending", f"{record_state} / {failed_step}")
    attempts = held_attempts if isinstance(held_attempts, int) and held_attempts >= 0 else 0
    with slot_lock(facts, seams) as locked, end_lock(facts, seams):
        return _end_locked(facts, seams, start, attempts + 1, caps, locked)


def _end_locked(facts: EndFacts, seams: EndSeams, start: int, attempts: int, caps: Caps, locked: bool) -> str:
    def cas(target: str, fields: Mapping[str, object]) -> None:
        send_cas(seams, target, fields)

    step = "slot-not-ours"
    try:
        if not locked:
            raise Refused("slot-not-ours", f"no {facts.slot.slot_dir(facts.store)}")
        check_slot_ours(facts, seams)
        step = "freeze"
        frozen = _prove_frozen(facts, seams)
        if frozen and start <= 0:
            cas(FROZEN, {})
        # H1: an unproven freeze does not stop the kill. Until FROZEN is sent a failure is still named "freeze" (the
        # retry resumes from the FROZEN edge); once the kill and its proof pass, every process of the session is gone,
        # which also answers what FROZEN records, so the edge goes then, before DEAD.
        step = "kill" if frozen else "freeze"
        _prove_dead(facts, seams)
        if not frozen and start <= 0:
            cas(FROZEN, {})
        step = "kill"
        if start <= 1:
            cas(DEAD, {})

        step = "park"
        pending = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if pending is not None:
            try:
                _reconcile(facts, seams, caps)
            except PhaseFailed as e:
                step = _held_name(pending.intent, e)
                raise
            if pending.intent == "retain":
                return RETAINED
        if pending is None and start <= 2:
            begin(facts.intent, park_plan(facts, seams), started_at=seams.now(), cas=cas,
                  fault=seams.fault, retry=seams.retry)
        elif start > 2:
            # idempotent; no journal and no CAS: the record is already past it
            park_move(facts, owner_ok=seams.owner_ok, fault=seams.fault)

        step = "init"
        if os.path.lexists(facts.tree):
            # A tree and no journal = a retain that finished (its CAS precedes the journal delete):
            # the filesystem says RETAINED, so that is the edge sent, never a second retain.
            cas(RETAINED, dict(retain_plan(facts, seams, caps=caps, started_at=seams.now()).fields()))
            return RETAINED
        if not repo_cloned(facts, owner_ok=seams.owner_ok):
            cas(RETAINED, {})
            return RETAINED
        started = seams.now()
        try:
            begin(facts.intent, retain_plan(facts, seams, caps=caps, started_at=started),
                  started_at=started, cas=cas, fault=seams.fault, retry=seams.retry)
        except PhaseFailed as e:
            step = _held_name("retain", e)
            raise
        return RETAINED
    except CasNotSent:
        raise
    except PhaseFailed as e:
        if isinstance(e.cause, CasNotSent):
            raise e.cause from None
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held = step
    except Exception as e:   # noqa: BLE001 - an unproven step is HELD, never a crash past the tree
        seams.note(f"END {facts.session}: {step} not proven: {e}")
        held = step
    cas(HELD, {"failed_step": held, "held_attempts": attempts})
    return HELD


__all__ = ["BUDGET_REFUSALS", "CAS_RETRY_SECONDS", "CLASSES", "CREDENTIALS", "CREDENTIAL_FILES", "CREDENTIAL_PARKED",
           "Caps", "HASH_ONLY_CAP", "LINK_UNREADABLE", "OUTSIDE", "PROVEN_PHASES", "SLOT_LOCK_WAIT_S", "UNOWNED",
           "UNREAD_ANSWERS", "WALK_OPEN_DIRS", "WALK_PEAK_FDS", "classify", "read_classes", "retained_whole",
           "stored_classification", "session_reader", "slot_lock", "sync_tree",
           "CasNotSent", "DEAD", "END_STEPS", "EndFacts", "EndSeams", "Entry", "FROZEN", "HELD", "LINKCOPY",
           "LINK_COPY_BYTES", "MANIFEST_FORMAT", "META_FORMAT", "OBJECT_FORMAT", "REVOKED", "RETAINED", "Reserve",
           "UNREAD", "V12_SECONDS", "V14_BYTES", "V17_MANIFEST_BYTES", "V17_META_BYTES", "V1_INODES", "V2_SECONDS",
           "V5_FLOOR_BYTES", "VAR_FOLDERS_NAME", "check_slot_ours", "cloned_path", "darwin_meta", "darwin_strip",
           "default_meta_reader", "default_strip", "digest_bytes", "end_lock", "end_session", "entry_type",
           "facts_from_alloc", "foreign_links", "fork_run_as", "iter_manifest", "iter_meta", "keep_shape_ok",
           "linux_meta", "linux_strip", "mark_cloned", "object_record", "os_chown", "outside_link_bytes",
           "REPLACED", "confirmed_relogin", "park_move", "park_plan", "read_as_session", "read_slot_state",
           "reconcile_session", "repo_cloned",
           "retain_plan", "send_cas", "tree_manifest", "unbuilt_freeze", "unbuilt_kill", "unbuilt_no_process",
           "utc_now", "walk"]
