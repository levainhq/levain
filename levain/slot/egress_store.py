"""Root's host-grant FILE store (board levain#66 slice S2, plan hgs2_plan_v2 section 3): the ``R/grants/slot<n>/``
directory walk, the read of the file root last placed there, and the unique-temp publish.

``R/grants/slot<n>/`` is ROOT's (root : the front's group, 0750), never the front's: no uid but root can create,
rename, link into or unlink in it, so the file at ``grants.json`` is always the one root last placed (no rollback
past a revoke, no temp swap, no planting). Every refusal of the walk is ``egress-grant-not-ready``: the layout row is
made by ``setup`` and the reaper's boot pass, so an installed helper that predates it answers that until they run.

Root's ``/usr/bin/python3`` (3.9) imports this module: no match statement, no ``X | Y`` outside annotations.
"""
from __future__ import annotations

import errno
import os
import re
import stat
import sys
from typing import Callable, List, Sequence

from levain.slot import Refused, write_all
from levain.slot import egress_grants as eg
from levain.slot.ids import SlotId

GRANTS_NAME = "grants.json"
FILE_MODE = 0o440
DIR_MODE = 0o750
TEMP_SWEEP_MAX = 16
_TEMP_RE = re.compile(r"grants\.json\.[0-9a-f]{16}\.new")
_O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_O_TEMP = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

NOT_READY = "egress-grant-not-ready"


class RenamedNotDurable(OSError):
    """:func:`commit` failed AFTER its rename: the new file IS in force but is not proved durable."""


def full_fsync(fd: int) -> None:
    """An fsync that reaches the medium. Darwin: ``F_FULLFSYNC`` (a plain fsync there leaves the drive's cache
    unflushed), and ``os.fsync`` only when that fcntl answers ENOTSUP or EINVAL; any other errno is raised.
    Elsewhere ``os.fsync``. A file and a directory alike. (The same rule as ``adopt_take._full_fsync``, which is not
    imported: it takes the whole adopt machinery in.)"""
    if sys.platform == "darwin":
        import fcntl

        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            if e.errno not in (errno.ENOTSUP, errno.EINVAL):
                raise
    os.fsync(fd)


def fsync_of(seams: object) -> Callable[[int], None]:
    """The sync the store uses: the helper's own seam when a test replaced it, else :func:`full_fsync` (the
    helper's default is plain ``os.fsync``, which orders nothing across files on Darwin)."""
    chosen = getattr(seams, "fsync", None)
    return full_fsync if chosen is None or chosen is os.fsync else chosen


def front_account(slot: SlotId) -> str:
    return slot.accounts[1]


class SlotDir:
    """An open ``R/grants/slot<n>/`` that passed the walk: ``fd``, the front's ``gid`` and the dir's ``path``."""

    def __init__(self, fd: int, gid: int, path: str) -> None:
        self.fd, self.gid, self.path = fd, gid, path

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "SlotDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_slot_dir(ctx: object, slot: SlotId) -> SlotDir:
    """``R/grants/slot<n>/``, walked one ``O_NOFOLLOW`` component at a time: ``R`` and ``R/grants`` pass
    ``seams.trusted``, and the slot dir, by its OPEN descriptor, is root's (uid 0), the front account's group and
    exactly 0750. Anything else is ``egress-grant-not-ready``, with nothing written."""
    seams = ctx.seams  # type: ignore[attr-defined]
    try:
        gid = seams.group_gid(front_account(slot))
    except Refused:
        raise Refused(NOT_READY, "the front's group is unknown") from None
    where = "%s/grants/slot%d" % (seams.R, slot.index)
    cur = -1
    try:
        cur = os.open(str(seams.R), _O_DIR)
        if not seams.trusted(os.fstat(cur)):
            raise Refused(NOT_READY, "R is not root's")
        for name in ("grants", "slot%d" % slot.index):
            nxt = os.open(name, _O_DIR, dir_fd=cur)
            os.close(cur)
            cur = nxt
            if name == "grants" and not seams.trusted(os.fstat(cur)):
                raise Refused(NOT_READY, "R/grants is not root's")
        st = os.fstat(cur)
        if (st.st_uid != seams.chowned_uid(0) or st.st_gid != gid or stat.S_IMODE(st.st_mode) != DIR_MODE):
            raise Refused(NOT_READY, "%s is not root:front 0750" % where)
        out = SlotDir(cur, gid, where)
        cur = -1
        return out
    except OSError:
        raise Refused(NOT_READY, "%s cannot be opened as a directory" % where) from None
    finally:
        if cur >= 0:
            os.close(cur)


def read_current(ctx: object, slot: SlotId, session: str, sd: SlotDir) -> List[eg.Entry]:
    """The entries of the file root last placed, or ``[]`` when there is none, it no longer parses, or it names another
    session or slot. Never a merge of anything root did not write: a bad file is an EMPTY rebuild, which only narrows."""
    seams = ctx.seams  # type: ignore[attr-defined]
    got = eg.read_grants_file(os.path.join(sd.path, GRANTS_NAME), owner_uid=seams.chowned_uid(0), group_gid=sd.gid)
    if got is None or got.session != session or got.slot != slot.index:
        return []
    return list(got.entries)


def stage(ctx: object, sd: SlotDir, raw: bytes, at: Callable[[str], None]) -> str:
    """The new file's bytes in a unique temp (``grants.json.<16 hex>.new``, ``O_EXCL|O_NOFOLLOW``), owned by root with
    the front's group, mode 0440 BEFORE it can be named ``grants.json``, written and synced. Returns the temp's name.
    Any failure unlinks only the temp this call made and re-raises."""
    seams = ctx.seams  # type: ignore[attr-defined]
    sync = fsync_of(seams)
    at("egress-grant:stage")
    tmp = "%s.%s.new" % (GRANTS_NAME, os.urandom(8).hex())
    fd = os.open(tmp, _O_TEMP, 0o600, dir_fd=sd.fd)
    try:
        try:
            seams.chown(fd, 0, sd.gid, "grants/%s/%s" % (os.path.basename(sd.path), GRANTS_NAME))
            os.fchmod(fd, FILE_MODE)
            at("egress-grant:write")
            write_all(fd, raw)
            sync(fd)
        finally:
            os.close(fd)
        at("egress-grant:staged")
    except BaseException:
        discard(sd, tmp)
        raise
    return tmp


def commit(ctx: object, sd: SlotDir, tmp: str, at: Callable[[str], None]) -> None:
    """``tmp`` becomes ``grants.json`` (one rename, atomic), then the directory is synced. A failure of the rename
    leaves the old file in force (the caller unlinks the temp); a failure of the sync after it is
    :class:`RenamedNotDurable`."""
    sync = fsync_of(ctx.seams)  # type: ignore[attr-defined]
    at("egress-grant:rename")
    os.replace(tmp, GRANTS_NAME, src_dir_fd=sd.fd, dst_dir_fd=sd.fd)
    try:
        at("egress-grant:dirsync")
        sync(sd.fd)
    except OSError as e:
        raise RenamedNotDurable(e.errno or errno.EIO, "%s renamed, not proved durable: %s" % (GRANTS_NAME, e)) from e


def discard(sd: SlotDir, tmp: str) -> None:
    """Unlink the temp this writer made; an absent one is fine."""
    try:
        os.unlink(tmp, dir_fd=sd.fd)
    except OSError:
        pass


def sweep_temps(sd: SlotDir) -> int:
    """Unlink the stale ``grants.json.<16 hex>.new`` a killed writer left (root-only names in a root-only dir), at most
    :data:`TEMP_SWEEP_MAX` per call. Returns how many went."""
    try:
        names: Sequence[str] = sorted(n for n in os.listdir(sd.fd) if _TEMP_RE.fullmatch(n))
    except OSError:
        return 0
    gone = 0
    for name in names[:TEMP_SWEEP_MAX]:
        try:
            os.unlink(name, dir_fd=sd.fd)
            gone += 1
        except OSError:
            pass
    return gone


def publish(ctx: object, sd: SlotDir, raw: bytes, at: Callable[[str], None]) -> None:
    """:func:`stage` then :func:`commit`, for a write with nothing to burn (a revoke only narrows)."""
    tmp = stage(ctx, sd, raw, at)
    try:
        commit(ctx, sd, tmp, at)
    except RenamedNotDurable:
        raise
    except BaseException:
        discard(sd, tmp)
        raise


__all__ = ["DIR_MODE", "FILE_MODE", "GRANTS_NAME", "NOT_READY", "RenamedNotDurable", "SlotDir", "TEMP_SWEEP_MAX",
           "commit", "discard", "front_account", "fsync_of", "full_fsync", "open_slot_dir", "publish", "read_current",
           "stage", "sweep_temps"]
