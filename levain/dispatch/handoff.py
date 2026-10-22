"""levain.dispatch.handoff — the filing's handoff store: where an operator-filed job's read-set bundle waits for its run.

A filing exports the job's read set as one git bundle into a private temp under the handoff dir's ``.tmp/``, which
its writer locks at create and holds until the install. This module owns everything after that on disk: the bundle's
id, its install into the live dir, the one lock that serialises installs with the cleaners, and the cleaners
themselves. The rows that name bundles are the job store's; this module is handed ids and never reads a row.

THE ID. A bundle is named ``hb-`` + 128 random bits as lowercase hex (:func:`new_bundle_id`), never the contract
digest and never the job id, so two filings never name one file and a filing's cleanup can only reach its own bundle.
Every id is checked against that form (:func:`is_bundle_id`) before it becomes a path, whatever handed it over (a row
read back from the store included).

THE LOCK (:meth:`HandoffDir.locked`). ONE handoff-store lock: a fixed file in the store's private directory, opened
``O_RDWR|O_CREAT|O_NOFOLLOW|O_CLOEXEC``, checked by ``fstat`` (a regular file, owned by this uid, no group or other
access) before the ``flock``, and never unlinked or replaced, so every holder locks one inode. It is held across an
install and the job store's publishing transaction, by a replay's cleanup of its own bundle, and by garbage
collection, so collection never sees a bundle that is installed but not yet named by a committed row. The lock is
taken before the database (store lock -> DB). An ``flock`` is per open file: one process takes it once per hold and
never a second time on a new fd while it holds it.

THE INSTALL (:meth:`HandoffDir.install`). A no-replace claim: the temp is ``link``ed to a fresh id's name (a fresh id
on ``EEXIST``; a file already there is never replaced), the temp unlinked, and both directories flushed. A ``link``
failure other than ``EEXIST`` is ``handoff-install-failed`` and the temp is left to collection.

THE CLEANERS. :meth:`HandoffDir.collect` (under the lock) removes every live-dir entry no row names and every
``.tmp/`` temp that is UNLOCKED and older than :data:`TEMP_GRACE_S` (a live filer's temp is locked, so it is
skipped, and so is one in the instant between its create and its lock). :meth:`HandoffDir.remove`
(no lock) deletes the bundles of the ids it is handed, the job store's rows for its terminal jobs: by row, never
because a listing lacks one.

Every flush tries ``F_FULLFSYNC`` (:func:`full_fsync`). Stdlib only.

Known edges: a handoff dir or ``.tmp/`` with a looser mode or another owner is refused here (``handoff-dir``) before
any install or collection; the store dir itself is the job store's to check. An entry of the live dir
that is a directory is never removed by collection; it is logged. Pre-S1 the handoff sits beside the job store under
the operator's uid, so any process of that uid can replace a bundle: the run re-verifies the content against the
signed contract (and the size against the row), so a replaced bundle denies the run and cannot change what it accepts.
"""
from __future__ import annotations

import errno
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from levain.dispatch import DispatchError

__all__ = ["BUNDLE_ID_FORM", "HANDOFF_DIR", "HandoffDir", "HandoffRefused", "LOCK_NAME", "TEMP_DIR", "TEMP_GRACE_S",
           "CollectReport", "full_fsync", "is_bundle_id", "new_bundle_id"]

_log = logging.getLogger(__name__)

#: The form of a bundle id. Applied with ``fullmatch`` only, on a ``str``.
BUNDLE_ID_FORM = re.compile(r"hb-[0-9a-f]{32}")
#: The live dir, inside the job store's directory.
HANDOFF_DIR = "handoffs"
#: The lock file, inside the job store's directory (a sibling of the live dir, so collection never lists it).
LOCK_NAME = "handoff.lock"
#: The exporter's private temp dir, inside the live dir (same filesystem: an install is a link, never a copy).
TEMP_DIR = ".tmp"
#: An unlocked temp younger than this is left by collection ([judged]: a temp between its create and its lock).
TEMP_GRACE_S = 60
_ACCESS_BY_OTHERS = 0o077
_INSTALL_TRIES = 8
_FALLBACK_ERRNOS = frozenset({errno.ENOTSUP, errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOSYS,
                              errno.ENODEV})
_fallback_logged = False


class HandoffRefused(DispatchError):
    """A handoff step refused, by name (``name``); nothing was published."""

    def __init__(self, name: str, detail: str = "") -> None:
        self.name = name
        self.detail = detail
        super().__init__(f"{name}: {detail}" if detail else name)


def is_bundle_id(value: object) -> bool:
    """True only for a ``str`` in :data:`BUNDLE_ID_FORM`, in full."""
    return type(value) is str and BUNDLE_ID_FORM.fullmatch(value) is not None


def new_bundle_id() -> str:
    """A fresh id in :data:`BUNDLE_ID_FORM`."""
    return "hb-" + secrets.token_hex(16)


def _checked(bundle_id: object) -> str:
    if not is_bundle_id(bundle_id):
        # the value is never echoed: it may be anything a damaged row holds
        raise HandoffRefused("handoff-id", "a bundle id is not hb- + 32 lowercase hex; no path is built from it")
    return bundle_id  # type: ignore[return-value]


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
        _log.warning("handoff: %s; falling back to fsync (ordering holds across process kill, not power loss)", why)
    os.fsync(fd)
    return "fsync"


def _private_dir(path: str) -> int:
    """An fd on the directory at ``path`` (never through a symlink), refused unless it is this uid's and 0700-tight."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise HandoffRefused("handoff-dir", f"{path} cannot be opened as a directory "
                             f"({errno.errorcode.get(exc.errno, exc.errno)})") from None
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or st.st_mode & _ACCESS_BY_OTHERS:
        os.close(fd)
        raise HandoffRefused("handoff-dir", f"{path} is not this uid's alone (uid {st.st_uid}, mode "
                             f"{st.st_mode & 0o777:o})")
    return fd


@dataclass
class CollectReport:
    """What one collection did: the live-dir bundles and the temps it removed, and the entries it left."""
    bundles: list[str] = field(default_factory=list)
    temps: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)


class HandoffDir:
    """The handoff store of the job store whose directory is ``store_dir``: bundles in ``store_dir/handoffs/``, the
    lock at ``store_dir/handoff.lock``. ``store_dir`` is the job store's own (its owner and mode are the store's
    check); nothing here creates it."""

    def __init__(self, store_dir: Path | str) -> None:
        # resolved once: the exporter takes only a real path, and the installer finds its temp by that same spelling
        self.store_dir = os.path.realpath(str(store_dir))
        self.path = os.path.join(self.store_dir, HANDOFF_DIR)
        self.lock_path = os.path.join(self.store_dir, LOCK_NAME)

    def ensure(self) -> str:
        """Create the live dir (0700) if absent and return its path; refused if it is not this uid's alone."""
        try:
            os.mkdir(self.path, 0o700)
        except FileExistsError:
            pass
        os.close(_private_dir(self.path))
        return self.path

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the ONE handoff-store lock for the block (blocking ``flock``)."""
        import fcntl

        sfd = _private_dir(self.store_dir)
        try:
            fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=sfd)
        except OSError as exc:
            raise HandoffRefused("handoff-lock", f"{self.lock_path} cannot be opened "
                                 f"({errno.errorcode.get(exc.errno, exc.errno)}; a symlink there is refused)") from None
        finally:
            os.close(sfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & _ACCESS_BY_OTHERS:
                raise HandoffRefused("handoff-lock", f"{self.lock_path} is not a private regular file of this uid")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)     # releases the flock; the file is never unlinked

    # --- under the lock ------------------------------------------------------------------------------------------
    def install(self, temp_path: str, temp_fd: int) -> str:
        """F3': claim a fresh id for the exporter's temp ``temp_path`` (under this live dir's ``.tmp/``) by a no-replace
        ``link``, unlink the temp, flush both dirs, close ``temp_fd`` (its lock goes with it); returns the id. Call
        under :meth:`locked`. A ``link`` failure other than ``EEXIST`` is ``handoff-install-failed`` (the temp is left,
        unlocked once ``temp_fd`` closes, for collection)."""
        try:
            tmp_dir = os.path.join(self.path, TEMP_DIR)
            if os.path.dirname(temp_path) != tmp_dir:
                raise HandoffRefused("handoff-install-failed", "the temp is not in this handoff dir's .tmp/")
            name = os.path.basename(temp_path)
            hfd = _private_dir(self.path)
            try:
                tfd = _private_dir(tmp_dir)
                try:
                    for _ in range(_INSTALL_TRIES):
                        bundle_id = _checked(new_bundle_id())
                        try:
                            os.link(name, bundle_id, src_dir_fd=tfd, dst_dir_fd=hfd, follow_symlinks=False)
                        except FileExistsError:
                            continue
                        except OSError as exc:
                            raise HandoffRefused("handoff-install-failed", f"link: "
                                                 f"{errno.errorcode.get(exc.errno, exc.errno)}") from None
                        break
                    else:
                        raise HandoffRefused("handoff-install-failed", f"no free id in {_INSTALL_TRIES} tries")
                    try:
                        os.unlink(name, dir_fd=tfd)
                        full_fsync(tfd)
                    except OSError as exc:
                        # the bundle is installed under its id; a temp left behind is a second name collection removes
                        _log.warning("handoff: the installed temp %s was not unlinked: %s", name,
                                     errno.errorcode.get(exc.errno, exc.errno))
                    full_fsync(hfd)
                    return bundle_id
                finally:
                    os.close(tfd)
            finally:
                os.close(hfd)
        finally:
            # the temp's lock goes with its fd, on every path: installed, or left unlocked for collection
            os.close(temp_fd)

    def unlink_own(self, bundle_id: str) -> None:
        """A filing's cleanup of ITS OWN bundle (its row was not adopted): unlink, flush the dir. Call under
        :meth:`locked`. A failure is logged and left for collection."""
        name = _checked(bundle_id)
        try:
            hfd = _private_dir(self.path)
            try:
                os.unlink(name, dir_fd=hfd)
                full_fsync(hfd)
            finally:
                os.close(hfd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.warning("handoff: unlinking %s failed: %s; collection takes it", name,
                         errno.errorcode.get(exc.errno, exc.errno))

    def collect(self, named: Iterable[str], *, now: float | None = None) -> CollectReport:
        """Startup collection, under :meth:`locked`: remove every live-dir entry no row names (``named``: the ids the
        store's rows hold, each checked) and every ``.tmp/`` temp that is unlocked and older than
        :data:`TEMP_GRACE_S`. A directory, or a temp still locked, is left."""
        import fcntl

        keep = {_checked(i) for i in named}
        report = CollectReport()
        if not os.path.lexists(self.path):
            return report
        now = time.time() if now is None else now
        hfd = _private_dir(self.path)
        try:
            for entry in os.listdir(hfd):
                if entry == TEMP_DIR or entry in keep:
                    continue
                st = os.lstat(entry, dir_fd=hfd)
                if stat.S_ISDIR(st.st_mode):
                    report.left.append(entry)
                    _log.warning("handoff: %s in the live dir is a directory; left", entry[:80])
                    continue
                os.unlink(entry, dir_fd=hfd)
                report.bundles.append(entry)
            if os.path.lexists(os.path.join(self.path, TEMP_DIR)):
                tfd = _private_dir(os.path.join(self.path, TEMP_DIR))
                try:
                    for entry in os.listdir(tfd):
                        st = os.lstat(entry, dir_fd=tfd)
                        if not stat.S_ISREG(st.st_mode):
                            report.left.append(f"{TEMP_DIR}/{entry}")
                            continue
                        if now - st.st_mtime < TEMP_GRACE_S:
                            report.left.append(f"{TEMP_DIR}/{entry}")
                            continue
                        try:
                            efd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                          dir_fd=tfd)
                        except FileNotFoundError:
                            continue
                        try:
                            try:
                                fcntl.flock(efd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                report.left.append(f"{TEMP_DIR}/{entry}")     # a live filer holds it
                                continue
                            os.unlink(entry, dir_fd=tfd)
                            report.temps.append(entry)
                        finally:
                            os.close(efd)
                    if report.temps:
                        full_fsync(tfd)
                finally:
                    os.close(tfd)
            if report.bundles:
                full_fsync(hfd)
        finally:
            os.close(hfd)
        return report

    # --- no lock: the job store's sync -----------------------------------------------------------------------------
    def remove(self, bundle_ids: Iterable[str]) -> list[str]:
        """Delete the bundles of ``bundle_ids`` (the rows of terminal jobs), each id checked; returns those removed.
        Takes no lock (the sync runs after a commit and never waits on a filing). Only entries present in the live
        dir are touched, and only by the row's id."""
        ids = {_checked(i) for i in bundle_ids}
        if not ids or not os.path.lexists(self.path):
            return []
        hfd = _private_dir(self.path)
        removed: list[str] = []
        try:
            for entry in sorted(ids.intersection(os.listdir(hfd))):
                try:
                    os.unlink(entry, dir_fd=hfd)
                    removed.append(entry)
                except FileNotFoundError:
                    pass
            if removed:
                full_fsync(hfd)
        finally:
            os.close(hfd)
        return removed
