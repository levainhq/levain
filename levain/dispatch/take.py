"""Dispatch TAKES an executor's exported artifact before it acknowledges it (CONTRACT §3.5 Retention, §3.6).

``ARTIFACT-ACK`` lets the reaper trash the retained session and its export with it, so the ACK is sent only when
dispatch itself holds the artifact. :func:`take_artifact` copies the export's bundle into the dispatch store
(``<store directory>/taken/<session>-<ticket hash>/<bundle digest>``, 0600 in 0700 directories: the name is the digest,
so a take of other bytes lands at another name and never overwrites held ones), checks the copied bytes against
the ``bundle_digest`` the export names, and records the artifact (:meth:`levain.dispatch.jobs.JobStore.record_take`) in
one transaction. The take is idempotent by (session, ticket): a repeat finds the record and the held bytes and copies
nothing; a record whose file is gone (or no longer digests as recorded) is copied again from the export, and if that is
gone too the take fails. The export carries no per-file digests and no file list, only counts, so the bundle is the
one thing there is to hold. Takes of one (session, ticket) hold an exclusive ``flock`` on a lock file in ``taken`` across the temp sweep, the copy and
the record, so two followers never sweep each other's live temp. Before the record commits, the bundle file and every directory whose entry the take created
(the take's directory, ``taken``, the store directory) are fsynced.

Every failure raises :class:`TakeFailed` by name and leaves no partial bundle behind (the take's lock file stays). ``take-bundle-absent``,
``take-digest-mismatch`` and ``take-conflict`` are about the export and the record, and repeat until they change;
``take-failed`` is the machine's (descriptors, disk, store) and may mend.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import secrets
import stat
from typing import Iterator

from levain.dispatch import DispatchError
from levain.dispatch.export import ExportedArtifact
from levain.dispatch.jobs import JobStore, TakeConflict, TakenArtifact

_CHUNK = 1 << 20
_DIR = "taken"
_TEMP = ".bundle."
_ABSENT = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})         # the export holds no such bundle


class TakeFailed(DispatchError):
    """Dispatch could not take the artifact; ``name`` says why: ``take-bundle-absent``, ``take-digest-mismatch``,
    ``take-conflict`` or ``take-failed`` (the disk or the store)."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


def take_artifact(jobs: JobStore, export_dir: str | os.PathLike[str], art: ExportedArtifact) -> TakenArtifact:
    """Hold ``art`` in ``jobs``' store, copied from ``export_dir`` unless it is already held whole."""
    rel = None if art.bundle is None else _relative(art)
    want = TakenArtifact(art.session, art.ticket, art.manifest_digest, art.bundle_digest, rel, art.files, art.bytes)
    try:
        with _take_lock(jobs, rel):
            held = jobs.taken_artifact(art.session, art.ticket)
            if held is not None and held != want:
                raise TakeFailed("take-conflict", "this session's ticket is already taken with other digests")
            if rel is not None:
                _sweep(jobs, rel)
                if _holds(jobs, rel, art.bundle_digest):
                    _sync(jobs, rel)                   # a crash after the rename may have left the entries unsynced
                else:
                    _copy(jobs, os.fspath(export_dir), art, rel)
            if held is None:
                try:
                    jobs.record_take(want)
                except TakeConflict:
                    _drop_loser(jobs, art, rel)
                    raise
    except TakeFailed:
        raise
    except TakeConflict as e:
        raise TakeFailed("take-conflict", str(e)) from None
    except OSError as e:
        raise TakeFailed("take-failed", f"{type(e).__name__}: {e.strerror or ''}".rstrip(": ")) from None
    except Exception as e:                       # the store's own refusals (a vanished or foreign store)
        raise TakeFailed("take-failed", type(e).__name__) from None
    return want


def _take_dirs(jobs: JobStore, rel: str) -> tuple[str, str]:
    """``(taken, the take's directory)`` for ``rel``, both made 0700."""
    ddir = os.path.dirname(os.path.join(jobs.directory, rel))
    taken = os.path.dirname(ddir)
    for d in (taken, ddir):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)                           # the umask and a pre-existing directory leave the mode alone
    return taken, ddir


@contextlib.contextmanager
def _take_lock(jobs: JobStore, rel: str | None) -> Iterator[None]:
    """An exclusive ``flock`` for one (session, ticket) take directory, held across the temp sweep, the copy and the
    record: two takes of the ticket (two followers) wait for each other, so neither sweeps the other's live temp. The
    lock file sits in ``taken`` beside the directory, never inside it, so the sweep and a listing of the take
    directory never see it."""
    if rel is None:
        yield
        return
    taken, ddir = _take_dirs(jobs, rel)
    fd = os.open(os.path.join(taken, "." + os.path.basename(ddir) + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                                 # closing the descriptor releases the flock


def _sweep(jobs: JobStore, rel: str) -> None:
    """Remove the ``.bundle.*`` temps a killed take left in ``rel``'s directory; the caller holds the take lock."""
    ddir = os.path.dirname(os.path.join(jobs.directory, rel))
    for name in os.listdir(ddir):
        if name.startswith(_TEMP):
            try:
                os.unlink(os.path.join(ddir, name))
            except OSError:
                pass


def _relative(art: ExportedArtifact) -> str:
    assert art.bundle_digest is not None
    digest = art.bundle_digest.split(":", 1)[1]
    return f"{_DIR}/{art.session}-{hashlib.sha256(art.ticket.encode()).hexdigest()[:16]}/{digest}"


def _drop_loser(jobs: JobStore, art: ExportedArtifact, rel: str | None) -> None:
    """A take that lost the record to another digest leaves its own copy behind: remove it, never the held one."""
    if rel is None:
        return
    row = jobs.taken_artifact(art.session, art.ticket)
    if row is not None and row.path != rel:
        try:
            os.unlink(os.path.join(jobs.directory, rel))
        except OSError:
            pass


def _holds(jobs: JobStore, rel: str, digest: str | None) -> bool:
    """The copy at ``rel`` is a regular file whose bytes digest as ``digest``."""
    try:
        fd = os.open(os.path.join(jobs.directory, rel), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        h = hashlib.sha256()
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
        return "sha256:" + h.hexdigest() == digest
    finally:
        os.close(fd)


def _copy(jobs: JobStore, export_dir: str, art: ExportedArtifact, rel: str) -> None:
    """Stream the export's bundle into a private temp file beside ``rel``, check its digest, then rename it in."""
    assert art.bundle is not None
    dest = os.path.join(jobs.directory, rel)
    ddir = os.path.dirname(dest)
    sfd = dfd = tfd = -1
    tmp = os.path.join(ddir, f"{_TEMP}{secrets.token_hex(8)}")
    try:
        try:
            sfd = os.open(os.path.join(export_dir, art.session), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            dfd = os.open(art.bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sfd)
        except OSError as e:
            if e.errno in _ABSENT:
                raise TakeFailed("take-bundle-absent", "the export holds no bundle the artifact names") from None
            raise                                    # descriptors, I/O, permission: the machine's, retried as take-failed
        if not stat.S_ISREG(os.fstat(dfd).st_mode):
            raise TakeFailed("take-bundle-absent", "the bundle is not a regular file")
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        h = hashlib.sha256()
        while chunk := os.read(dfd, _CHUNK):
            h.update(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(tfd, view):]
        if "sha256:" + h.hexdigest() != art.bundle_digest:
            raise TakeFailed("take-digest-mismatch", "the bundle's bytes are not the digest the artifact names")
        os.fsync(tfd)
        os.close(tfd)
        tfd = -1
        os.replace(tmp, dest)
        tmp = ""
        _sync(jobs, rel)
    finally:
        for fd in (tfd, dfd, sfd):
            if fd >= 0:
                os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _sync(jobs: JobStore, rel: str) -> None:
    """fsync the bundle's directory, ``taken`` and the store directory: each holds an entry a take creates."""
    ddir = os.path.dirname(os.path.join(jobs.directory, rel))
    for d in (ddir, os.path.dirname(ddir), os.fspath(jobs.directory)):
        _fsync_dir(d)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ["TakeFailed", "take_artifact"]
