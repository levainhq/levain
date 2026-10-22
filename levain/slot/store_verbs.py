"""The store verbs (CONTRACT §7.4; S1 session 5 DESIGN §4.6, §4.7): ``discard``, ``restore``, ``purge``.

Each is a root intent (:mod:`levain.slot.root_intent`): the journal names its phase BEFORE the step runs, every
step is idempotent, and a crash anywhere is finished FORWARD by :func:`reconcile_store` from the journal and the
filesystem only, never from the CONTROL-written state record.

- ``discard``: RETAINED (root's own retained object whole, no journal) -> ``rename`` (retained -> trash) ->
  ``stamp`` (a carried ``restored_at`` removed; ``trashed_at``, written once) -> ``commit`` (CAS TRASH).
- ``restore``: TRASH (root's trash entry with its ``trashed_at``) -> ``mark`` (``restored_at`` INSIDE the trash
  entry, so the rename carries it) -> ``rename`` (trash -> retained) -> ``unstamp`` (``trashed_at`` removed) ->
  ``commit`` (CAS RETAINED). Rename only: nothing is re-applied.
- ``purge``: TRASH -> ``check`` (``forced`` by the password-gated install verb, else ``trashed_at`` at least
  ``retention_days`` old; neither = the journal is deleted and the trash entry left) -> ``delete_export`` (the
  session's ``<s>`` and the publish transaction's ``<s>.new`` in its export class dir; a ``verify.new`` inside
  ``<s>`` goes with it) -> ``delete`` (the trash entry) -> ``commit`` (CAS PURGED). The reaper's aged purge is
  :func:`purge` with ``retention_days`` and no ``forced``.

Each verb first finishes a store journal the session already holds (a run whose step or CAS failed), so re-running
the verb completes it rather than refusing it.

Locks (§4.7): root's per-session lock ``L/alloc/<s>.lock`` (shared with every per-session root verb), TRY, held =
``session-busy``; purge also TRIES the machine-wide adopt lock ``L/export/adopt.lock`` after it (order session ->
adopt), held = ``adopt-busy``. Every delete walks by directory fd (``unlinkat``), never follows a link, never crosses
a mount, and clears BSD flags (macOS) / the immutable and append-only bits (Linux) before it unlinks.
"""
from __future__ import annotations

import array
import errno
import fcntl
import os
import stat
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from levain.slot import Refused
from levain.slot import records as R
from levain.slot.grammar import SESSION_RE
from levain.slot.root_intent import (STORE_INTENTS, TIMESTAMP_RE, Cas, Fault, Phase, PhaseFailed, Plan, Retry,
                                     RootIntent, atomic_write, begin, delete_root_intent, fsync_dir, intent_path,
                                     read_root_intent, reconcile, sweep_temps, unlink_durable)

#: The export class dir of each alloc kind (CONTRACT §2.1 rows ``L/export/{operator,dispatch}/<session>``).
EXPORT_CLASS: Mapping[str, str] = {"interactive": "operator", "login": "operator", "dispatch": "dispatch",
                                   "planner": "dispatch", "verifier": "dispatch"}
#: The publish transaction's staging name beside ``<s>``: the session grammar (``s-`` + 16 hex) has no ``.``, so
#: no id ends in it.
STAGING_SUFFIX = ".new"
DAY_SECONDS = 86400
# DECISION: a stamp is one timestamp and a newline; a file past this is not one.
MAX_STAMP_BYTES = 64
#: Root's own stamp files at the top of a trash entry (the only temp names swept there).
STAMPS = ("trashed_at", "restored_at")
# DECISION: a delete holds one directory fd per level of the tree, so it refuses a tree deeper than this
# (``delete-too-deep``) rather than run out of fds part-way. It sits at half of macOS's default soft RLIMIT_NOFILE
# (256, the smallest default of the hosts), leaving the rest for the process's own fds.
DELETE_MAX_DEPTH = 128

_DIR_OPEN = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def _root_owned(st: os.stat_result) -> bool:
    return st.st_uid == 0


def _no_fault(point: str) -> None:
    return None


def _direct(fn: Callable[[], None]) -> None:
    fn()


def utc_now() -> str:
    return R.timestamp(time.time())


# --- flags -------------------------------------------------------------------------------------------------------

_libc = None
# Linux <linux/fs.h>: FS_IOC_GETFLAGS / FS_IOC_SETFLAGS (an int argument), FS_IMMUTABLE_FL, FS_APPEND_FL.
_FS_IOC_GETFLAGS, _FS_IOC_SETFLAGS = 0x80086601, 0x40086602
_FS_CLEAR = 0x00000010 | 0x00000020
_NO_FLAG_SUPPORT = (errno.ENOTTY, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS)
# macOS: the BSD flags that refuse an unlink or a write inside a directory; any other (compressed, hidden, ...) is kept.
_DARWIN_CLEAR = (getattr(stat, "UF_IMMUTABLE", 0) | getattr(stat, "UF_APPEND", 0) | getattr(stat, "SF_IMMUTABLE", 0)
                 | getattr(stat, "SF_APPEND", 0))


def refusing_flags(st: os.stat_result) -> int:
    """The macOS flags on ``st`` that :func:`clear_flags` clears (0 elsewhere: Linux keeps them out of ``stat``)."""
    return getattr(st, "st_flags", 0) & _DARWIN_CLEAR


def _darwin_fchflags(fd: int, flags: int) -> None:
    global _libc
    import ctypes
    import ctypes.util
    if _libc is None:
        c = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        c.fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
        c.fchflags.restype = ctypes.c_int
        _libc = c
    if _libc.fchflags(fd, flags) != 0:
        n = ctypes.get_errno()
        raise OSError(n, f"fchflags: {os.strerror(n)}")


def clear_flags(fd: int, st: os.stat_result) -> None:
    """Clear what would refuse an unlink of the entry ``fd`` names, or of an entry inside it: the user and system
    immutable and append-only BSD flags on macOS (``st_flags`` says whether one is set; the others are kept), the
    immutable and append-only bits on Linux (``stat`` does not show them, so they are read). A filesystem without
    the call is not an error."""
    if sys.platform == "darwin":
        if refusing_flags(st):
            _darwin_fchflags(fd, st.st_flags & ~_DARWIN_CLEAR)
        return
    if sys.platform.startswith("linux"):
        buf = array.array("i", [0])
        try:
            fcntl.ioctl(fd, _FS_IOC_GETFLAGS, buf, True)
            if buf[0] & _FS_CLEAR:
                buf[0] &= ~_FS_CLEAR
                fcntl.ioctl(fd, _FS_IOC_SETFLAGS, buf, True)
        except OSError as e:
            if e.errno not in _NO_FLAG_SUPPORT:
                raise


# --- facts and seams ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreFacts:
    """A session's store paths, every one derived from the alloc record (CONTRACT §7.1) and the store root."""

    session: str
    store: Path
    kind: str
    operator_uid: int

    def __post_init__(self) -> None:
        if not isinstance(self.session, str) or not SESSION_RE.fullmatch(self.session):
            raise Refused("bad-token", f"session {self.session!r}")
        if self.kind not in EXPORT_CLASS:
            raise Refused("bad-value", f"alloc kind {self.kind!r}")

    @property
    def alloc_dir(self) -> Path:
        return self.store / "alloc"

    @property
    def intent(self) -> Path:
        return intent_path(self.alloc_dir, self.session)

    @property
    def lock(self) -> Path:
        """Root's per-session lock, shared with every per-session root verb, never unlinked."""
        return self.alloc_dir / f"{self.session}.lock"

    @property
    def retained(self) -> Path:
        return self.store / "retained" / self.session

    @property
    def trash(self) -> Path:
        return self.store / "trash" / self.session

    @property
    def export_class(self) -> str:
        return EXPORT_CLASS[self.kind]

    @property
    def adopt_lock(self) -> Path:
        return self.store / "export" / "adopt.lock"

    @property
    def export_names(self) -> tuple[str, str]:
        """The names purge removes from ``L/export/<class>/``, by name, never by pattern."""
        return (self.session, self.session + STAGING_SUFFIX)


def facts_from_alloc(a: R.Alloc, store: Path) -> StoreFacts:
    return StoreFacts(session=a.session, store=Path(store), kind=a.kind, operator_uid=a.operator_uid)


@dataclass
class StoreSeams:
    """``cas(target, fields)``: CONTROL's CAS; a record already at ``target`` counts as done (:func:`control_cas`)."""

    cas: Cas
    now: Callable[[], str] = utc_now
    owner_ok: Callable[[os.stat_result], bool] = _root_owned
    fault: Fault = _no_fault
    retry: Retry = _direct
    clear_flags: Callable[[int, os.stat_result], None] = clear_flags


#: The one edge into each store-verb target (CONTRACT §7.3).
CAS_FROM: Mapping[str, str] = {R.TRASH: R.RETAINED, R.RETAINED: R.TRASH, R.PURGED: R.TRASH}


def control_cas(ctx, session: str) -> Cas:
    """A CAS seam on the runtime helper's context (``ctx.store`` reads, ``ctx.call`` sends): the record is read as a
    HINT for its generation; a record already at the target is done (a crash between the CAS and the journal
    delete, CONTRACT §7.4); otherwise the CAS names the target's one edge, so CONTROL refuses any other ``from``."""
    def cas(target: str, fields: Mapping[str, object]) -> None:
        data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
        if data is None:
            raise Refused("no-session", session)
        rec = R.parse_state(data)
        if rec.state == target:
            return
        ctx.call("CAS", session=session, generation=rec.generation, **{"from": CAS_FROM[target], "to": target},
                 fields=dict(fields))
    return cas


# --- locks -------------------------------------------------------------------------------------------------------


@contextmanager
def _try_lock(path: Path, busy: str, detail: str, owner_ok: Callable[[os.stat_result], bool]) -> Iterator[None]:
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise Refused("bad-root", f"{path}: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not owner_ok(st):
            raise Refused("bad-root", f"{path} is not a regular file with the expected owner")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(busy, detail) from None
        yield
    finally:
        os.close(fd)   # closing the only descriptor releases the flock


@contextmanager
def session_lock(facts: StoreFacts, seams: StoreSeams) -> Iterator[None]:
    """The per-session TRY-lock (held = ``session-busy``). Under it no other writer of this session's journal or
    trash entry runs, so a dead writer's temp files there are swept."""
    with _try_lock(facts.lock, "session-busy", facts.session, seams.owner_ok):
        sweep_temps(facts.alloc_dir, facts.intent.name, owner_ok=seams.owner_ok)
        if _real_dir(facts.trash, seams):
            for base in STAMPS:
                sweep_temps(facts.trash, base, owner_ok=seams.owner_ok)
        yield


def adopt_lock(facts: StoreFacts, seams: StoreSeams):
    """The machine-wide adopt lock, TRIED after the session lock (held = ``adopt-busy``)."""
    return _try_lock(facts.adopt_lock, "adopt-busy", "an import or verify holds the adopt lock", seams.owner_ok)


# --- root facts --------------------------------------------------------------------------------------------------


def _real_dir(p: Path, seams: StoreSeams) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(st.st_mode) and seams.owner_ok(st)


def read_stamp(p: Path, seams: StoreSeams) -> str | None:
    """A root stamp file's timestamp (``trashed_at``, ``restored_at``), or None when it is absent or not one."""
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not seams.owner_ok(st):
            return None
        data = os.read(fd, MAX_STAMP_BYTES + 1)
    finally:
        os.close(fd)
    text = data.decode("ascii", "replace")
    if not text.endswith("\n") or not TIMESTAMP_RE.fullmatch(text[:-1]):
        return None
    return text[:-1]


def retained_whole(facts: StoreFacts, seams: StoreSeams) -> bool:
    """END's own predicate (one function: a RETAINED by root facts that END takes at its word is the one discard
    takes)."""
    from levain.slot import end as E
    return E.retained_whole(facts, seams)   # type: ignore[arg-type] - it reads .retained, .session, .owner_ok


def in_trash(facts: StoreFacts, seams: StoreSeams) -> bool:
    """TRASH by root facts: root's trash entry is a real directory holding a ``trashed_at`` stamp."""
    return _real_dir(facts.trash, seams) and read_stamp(facts.trash / "trashed_at", seams) is not None


def purge_due(facts: StoreFacts, seams: StoreSeams, retention_days: int | None) -> bool:
    """``now - trashed_at >= retention_days`` from the ROOT stamp (never the state record)."""
    if retention_days is None:
        return False
    stamp = read_stamp(facts.trash / "trashed_at", seams)
    if stamp is None:
        return False
    return R.epoch_of(seams.now()) - R.epoch_of(stamp) >= retention_days * DAY_SECONDS


# --- the delete --------------------------------------------------------------------------------------------------


def _open_store_dir(store: Path, *parts: str) -> int | None:
    """``L/<parts>`` opened one component at a time, none followed; None when a component is absent. A component
    on another device than the store root is refused (``delete-crosses-mount``)."""
    try:
        fd = os.open(store, _DIR_OPEN)
    except FileNotFoundError:
        return None
    try:
        dev = os.fstat(fd).st_dev
        for part in parts:
            try:
                nfd = os.open(part, _DIR_OPEN, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = nfd
            if os.fstat(fd).st_dev != dev:
                raise Refused("delete-crosses-mount", part)
        out, fd = fd, -1
        return out
    finally:
        if fd >= 0:
            os.close(fd)


def _lstat_at(dfd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_dir_at(dfd: int, name: str, st: os.stat_result, seams: StoreSeams) -> int:
    """The directory ``name`` that ``st`` described, its flags cleared; a different object there = ``delete-moved``."""
    fd = os.open(name, _DIR_OPEN, dir_fd=dfd)
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise Refused("delete-moved", name)
        seams.clear_flags(fd, fst)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _darwin_clear_at(dfd: int, name: str, st: os.stat_result) -> None:
    """macOS, a symlink / FIFO / socket leaf: its refusing flags cleared by path (a socket has no fd to take), the
    path being the directory fd's own plus ``name``, checked to name the inode ``st`` described first."""
    buf = fcntl.fcntl(dfd, fcntl.F_GETPATH, bytes(1024))
    path = os.path.join(os.fsdecode(buf.split(b"\0", 1)[0]), name)
    pst = os.lstat(path)
    if (pst.st_dev, pst.st_ino) != (st.st_dev, st.st_ino):
        raise Refused("delete-moved", name)
    os.chflags(path, pst.st_flags & ~_DARWIN_CLEAR, follow_symlinks=False)


def _clear_leaf(dfd: int, name: str, st: os.stat_result, seams: StoreSeams) -> bool:
    """Clear a leaf's flags: a regular file's through an fd of it, a macOS non-regular leaf's by path; False when
    it is neither (a Linux non-regular leaf: no fd ioctl reaches it)."""
    if not stat.S_ISREG(st.st_mode):
        if sys.platform == "darwin" and refusing_flags(st):
            _darwin_clear_at(dfd, name, st)
            return True
        return False
    fd = os.open(name, _LEAF_OPEN, dir_fd=dfd)
    try:
        fst = os.fstat(fd)
        if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise Refused("delete-moved", name)
        seams.clear_flags(fd, fst)
    finally:
        os.close(fd)
    return True


def _unlink_leaf(dfd: int, name: str, st: os.stat_result, seams: StoreSeams) -> None:
    if refusing_flags(st):
        _clear_leaf(dfd, name, st, seams)
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        return
    except PermissionError:
        # Linux's immutable bit is not in stat: clear it now that the unlink named it, then once more.
        if not _clear_leaf(dfd, name, st, seams):
            raise
        os.unlink(name, dir_fd=dfd)


def _list_all(cur: int) -> list[tuple[str, os.stat_result]]:
    """Every entry of ``cur`` with its lstat, the directory read to its end once (the stream is closed only after
    it is exhausted, so no name read ahead is dropped)."""
    out: list[tuple[str, os.stat_result]] = []
    with os.scandir(cur) as it:
        for e in it:
            est = _lstat_at(cur, e.name)
            if est is not None:
                out.append((e.name, est))
    return out


def delete_at(dfd: int, name: str, seams: StoreSeams) -> bool:
    """Delete ``name`` in the directory ``dfd`` and everything below it; False when it was absent. Iterative and by
    directory fd, the way ``shutil.rmtree``'s fd walk is: each directory is listed once, to its end, and its
    entries are removed from that list; a subdirectory is opened relative to its parent's fd (checked to be the
    object it was listed as, else ``delete-moved``), and the parent's fd stays open on a stack so an emptied
    subdirectory is removed relative to it, never by a path or ``..``. No link is followed; a directory on another
    device than ``dfd`` (``name`` itself included) is refused (``delete-crosses-mount``), never entered; a tree
    deeper than :data:`DELETE_MAX_DEPTH` is refused (``delete-too-deep``) before the fd past it is opened. Flags are
    cleared before each unlink. Idempotent: a crash or refusal part-way leaves a smaller tree that the next call
    deletes."""
    st = _lstat_at(dfd, name)
    if st is None:
        return False
    if not stat.S_ISDIR(st.st_mode):
        _unlink_leaf(dfd, name, st, seams)
        os.fsync(dfd)
        return True
    top_dev = os.fstat(dfd).st_dev
    if st.st_dev != top_dev:
        raise Refused("delete-crosses-mount", name)
    # One level per open directory: its fd, its name and lstat in the parent, the entries still to remove.
    stack: list[tuple[int, str, os.stat_result, list[tuple[str, os.stat_result]]]] = []
    fd = _open_dir_at(dfd, name, st, seams)
    try:
        stack.append((fd, name, st, _list_all(fd)))
    except BaseException:
        os.close(fd)
        raise
    try:
        while stack:
            cur, _, _, entries = stack[-1]
            if entries:
                n, est = entries.pop()
                if not stat.S_ISDIR(est.st_mode):
                    _unlink_leaf(cur, n, est, seams)
                    continue
                if est.st_dev != top_dev:
                    raise Refused("delete-crosses-mount", n)
                if len(stack) >= DELETE_MAX_DEPTH:
                    raise Refused("delete-too-deep", n)
                child = _open_dir_at(cur, n, est, seams)
                try:
                    stack.append((child, n, est, _list_all(child)))
                except BaseException:
                    os.close(child)
                    raise
                continue
            cur, n, est, _ = stack.pop()
            os.close(cur)
            parent = stack[-1][0] if stack else dfd
            now = _lstat_at(parent, n)
            if now is None or (now.st_dev, now.st_ino) != (est.st_dev, est.st_ino):
                raise Refused("delete-moved", n)
            os.rmdir(n, dir_fd=parent)
        os.fsync(dfd)
        return True
    finally:
        for cur, _, _, _ in stack:
            os.close(cur)


def delete_named(store: Path, parts: tuple[str, ...], names: tuple[str, ...], seams: StoreSeams) -> list[str]:
    """Delete each of ``names`` (exact names, never a pattern) in ``L/<parts>``; the names that were there."""
    dfd = _open_store_dir(store, *parts)
    if dfd is None:
        return []
    try:
        return [n for n in names if delete_at(dfd, n, seams)]
    finally:
        os.close(dfd)


def delete_export(facts: StoreFacts, seams: StoreSeams) -> list[str]:
    """§7.4 purge ``delete_export``: ``L/export/<class>/<s>/`` (a ``verify.new/`` inside it goes with it) and the
    publish transaction's ``L/export/<class>/<s>.new/``, the class from alloc ``kind``."""
    return delete_named(facts.store, ("export", facts.export_class), facts.export_names, seams)


# --- the steps ---------------------------------------------------------------------------------------------------


def _move(src: Path, dst: Path) -> None:
    """``src`` renamed to ``dst``, both parents fsynced. Idempotent: ``dst`` alone = already moved (the parents are
    fsynced again). Both present = ``store-conflict``, neither = ``store-lost``: each is refused, nothing touched."""
    s, d = os.path.lexists(src), os.path.lexists(dst)
    if s and d:
        raise Refused("store-conflict", f"{src} and {dst} both exist")
    if not s and not d:
        raise Refused("store-lost", f"neither {src} nor {dst}")
    if s:
        os.rename(src, dst)
    fsync_dir(src.parent)
    fsync_dir(dst.parent)


def _stamp(p: Path, seams: StoreSeams) -> None:
    """A root stamp (0644), written once: a redo keeps the first, so crash time never moves it."""
    if read_stamp(p, seams) is None:
        atomic_write(p, (seams.now() + "\n").encode("ascii"), 0o644)
    else:
        fsync_dir(p.parent)


def _discard_stamp(facts: StoreFacts, seams: StoreSeams) -> None:
    """A previous restore's ``restored_at`` (the rename carried it in) removed, so the next restore writes its own;
    then ``trashed_at``."""
    unlink_durable(facts.trash / "restored_at")
    _stamp(facts.trash / "trashed_at", seams)


def _mark(facts: StoreFacts, seams: StoreSeams) -> None:
    if os.path.lexists(facts.trash):
        _stamp(facts.trash / "restored_at", seams)
    elif not os.path.lexists(facts.retained):
        raise Refused("store-lost", f"neither {facts.trash} nor {facts.retained}")


def _check(facts: StoreFacts, seams: StoreSeams, ri: RootIntent, retention_days: int | None) -> None:
    """§7.4 ``check``: forced, or due by the root stamp. Neither = the journal is deleted (the trash entry is left
    as it is) and ``purge-not-due`` raised."""
    if ri.forced is True or purge_due(facts, seams, retention_days):
        return
    delete_root_intent(facts.intent)
    raise Refused("purge-not-due", facts.session)


def _delete_export_step(facts: StoreFacts, seams: StoreSeams) -> None:
    delete_export(facts, seams)


def _delete_trash_step(facts: StoreFacts, seams: StoreSeams) -> None:
    delete_named(facts.store, ("trash",), (facts.session,), seams)


def discard_plan(facts: StoreFacts, seams: StoreSeams) -> Plan:
    return Plan("discard", (
        Phase("rename", lambda: _move(facts.retained, facts.trash)),
        Phase("stamp", lambda: _discard_stamp(facts, seams)),
        Phase("commit", lambda: fsync_dir(facts.trash)),
    ))


def restore_plan(facts: StoreFacts, seams: StoreSeams) -> Plan:
    return Plan("restore", (
        Phase("mark", lambda: _mark(facts, seams)),
        Phase("rename", lambda: _move(facts.trash, facts.retained)),
        Phase("unstamp", lambda: unlink_durable(facts.retained / "trashed_at")),
        Phase("commit", lambda: fsync_dir(facts.retained)),
    ))


def purge_plan(facts: StoreFacts, seams: StoreSeams, ri: RootIntent, retention_days: int | None = None) -> Plan:
    return Plan("purge", (
        Phase("check", lambda: _check(facts, seams, ri, retention_days)),
        Phase("delete_export", lambda: _delete_export_step(facts, seams)),
        Phase("delete", lambda: _delete_trash_step(facts, seams)),
        Phase("commit", lambda: fsync_dir(facts.store / "trash")),
    ))


def _plan_for(facts: StoreFacts, seams: StoreSeams, retention_days: int | None) -> Callable[[RootIntent], Plan]:
    def plan_for(ri: RootIntent) -> Plan:
        if ri.intent == "discard":
            return discard_plan(facts, seams)
        if ri.intent == "restore":
            return restore_plan(facts, seams)
        return purge_plan(facts, seams, ri, retention_days)
    return plan_for


def _not_due(e: PhaseFailed) -> bool:
    return e.phase == "check" and isinstance(e.cause, Refused) and e.cause.name == "purge-not-due"


# --- the verbs ---------------------------------------------------------------------------------------------------


def _reconcile_held(facts: StoreFacts, seams: StoreSeams, retention_days: int | None) -> RootIntent | None:
    """:func:`reconcile_store`'s body; the caller holds the session lock (and, for a purge journal, the adopt lock)."""
    try:
        return reconcile(facts.intent, _plan_for(facts, seams, retention_days), cas=seams.cas, fault=seams.fault,
                         retry=seams.retry, owner_ok=seams.owner_ok, intents=STORE_INTENTS)
    except PhaseFailed as e:
        if _not_due(e):
            raise e.cause from None
        raise


def _finish_journal(facts: StoreFacts, seams: StoreSeams, verb: str, retention_days: int | None = None) -> bool:
    """A store journal this session already holds (a run whose step or CAS failed) is finished FORWARD before the
    verb starts its own: True when it was this verb's and is now done. A journal this verb cannot finish (END's, an
    unreadable one, a purge under discard / restore, which do not hold the adopt lock) refuses ``intent-in-flight``;
    a purge journal at ``check`` that is neither forced nor due is deleted and the verb goes on."""
    if not os.path.lexists(facts.intent):
        return False
    ri = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
    if ri is None or ri.intent not in STORE_INTENTS or (ri.intent == "purge" and verb != "purge"):
        raise Refused("intent-in-flight", f"{ri.intent}/{ri.phase}" if ri is not None else facts.session)
    try:
        _reconcile_held(facts, seams, retention_days)
    except Refused as e:
        if e.name == "purge-not-due":
            return False
        raise
    return ri.intent == verb


def _start(facts: StoreFacts, seams: StoreSeams, plan: Plan, forced: bool | None = None) -> None:
    begin(facts.intent, plan, started_at=seams.now(), cas=seams.cas, forced=forced, fault=seams.fault,
          retry=seams.retry)


def discard(facts: StoreFacts, seams: StoreSeams) -> str:
    """RETAINED -> TRASH. Refuses ``session-busy``, ``intent-in-flight``, ``not-retained:trash`` (a trash entry
    exists) or ``not-retained:absent`` (no whole retained object). Raises :class:`PhaseFailed` with the journal
    left at that phase (the verb run again, or :func:`reconcile_store`, finishes it; ``intent-in-flight`` is a
    journal it cannot finish). Returns the target state."""
    with session_lock(facts, seams):
        if _finish_journal(facts, seams, "discard"):
            return R.TRASH
        if os.path.lexists(facts.trash):
            raise Refused("not-retained:trash", facts.session)
        if not retained_whole(facts, seams):
            raise Refused("not-retained:absent", facts.session)
        _start(facts, seams, discard_plan(facts, seams))
    return R.TRASH


def _require_trash(facts: StoreFacts, seams: StoreSeams) -> None:
    if not in_trash(facts, seams):
        where = "retained" if os.path.lexists(facts.retained) else "absent"
        raise Refused(f"not-trash:{where}", facts.session)
    if os.path.lexists(facts.retained):
        raise Refused("store-conflict", f"{facts.trash} and {facts.retained} both exist")


def restore(facts: StoreFacts, seams: StoreSeams) -> str:
    """TRASH -> RETAINED, rename only. Refuses ``session-busy``, ``intent-in-flight``, ``not-trash:retained`` /
    ``not-trash:absent``. Returns the target state."""
    with session_lock(facts, seams):
        if _finish_journal(facts, seams, "restore"):
            return R.RETAINED
        _require_trash(facts, seams)
        _start(facts, seams, restore_plan(facts, seams))
    return R.RETAINED


def purge(facts: StoreFacts, seams: StoreSeams, *, forced: bool = False, retention_days: int | None = None) -> str:
    """TRASH -> PURGED: the install helper's ``purge <session>`` (``forced=True``, password-gated) and the reaper's
    aged purge (``retention_days`` from policy, no ``forced``) are this one function. Takes the session lock, then
    TRIES the adopt lock; refuses ``session-busy``, ``adopt-busy``, ``intent-in-flight``, ``not-trash:retained`` /
    ``not-trash:absent`` and, unforced, ``purge-not-due`` (before any journal is written). Removes
    ``L/export/<class>/<s>``, ``L/export/<class>/<s>.new`` (with any ``verify.new`` inside ``<s>``) and the
    trash entry. Raises :class:`PhaseFailed` with the journal left at that phase; the next purge finishes that
    journal first and answers PURGED. Returns the target state."""
    with session_lock(facts, seams), adopt_lock(facts, seams):
        if _finish_journal(facts, seams, "purge", retention_days):
            return R.PURGED
        _require_trash(facts, seams)
        if not forced and not purge_due(facts, seams, retention_days):
            raise Refused("purge-not-due", facts.session)
        ri = RootIntent("purge", "check", seams.now(), True if forced else None)
        _start(facts, seams, purge_plan(facts, seams, ri, retention_days), forced=ri.forced)
    return R.PURGED


def reconcile_store(facts: StoreFacts, seams: StoreSeams, *, retention_days: int | None = None) -> RootIntent | None:
    """The reaper's step 2 / helper start for a store-verb journal: finish it FORWARD under the session lock (and,
    for a purge, the adopt lock), from the journal and the filesystem only. None when there is no journal; a
    journal of END's refuses ``intent-unbuilt`` untouched; a purge at ``check`` that is neither forced nor due
    deletes its journal and raises ``purge-not-due``. Raises :class:`PhaseFailed` (journal left at the phase)."""
    with session_lock(facts, seams):
        ri = read_root_intent(facts.intent, owner_ok=seams.owner_ok)
        if ri is None:
            return None
        with adopt_lock(facts, seams) if ri.intent == "purge" else nullcontext():
            return _reconcile_held(facts, seams, retention_days)


__all__ = ["CAS_FROM", "EXPORT_CLASS", "STAGING_SUFFIX", "StoreFacts", "StoreSeams", "adopt_lock", "clear_flags",
           "control_cas", "delete_at", "delete_export", "delete_named", "discard", "discard_plan", "facts_from_alloc",
           "in_trash", "purge", "purge_due", "purge_plan", "read_stamp", "reconcile_store", "restore",
           "restore_plan", "retained_whole", "session_lock"]
