"""Dispatch TAKES an executor's exported artifact before it acknowledges it (CONTRACT §3.5 Retention, §3.6).

``ARTIFACT-ACK`` lets the reaper trash the retained session and its export with it, so the ACK is sent only when
dispatch itself holds the artifact. :func:`take_artifact` copies the export's bundle into the dispatch store
(``<store directory>/taken/<session>-<ticket hash>/bundle``, 0600 in 0700 directories), checks the copied bytes against
the ``bundle_digest`` the export names, and records the artifact (:meth:`levain.dispatch.jobs.JobStore.record_take`) in
one transaction. The take is idempotent by (session, ticket): a repeat finds the record and the held bytes and copies
nothing; a record whose file is gone (or no longer digests as recorded) is copied again from the export, and if that is
gone too the take fails. The export carries no per-file digests and no file list, only counts, so the bundle is the
one thing there is to hold.

Every failure raises :class:`TakeFailed` by name and leaves no partial file behind.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import stat

from levain.dispatch import DispatchError
from levain.dispatch.export import ExportedArtifact
from levain.dispatch.jobs import JobStore, TakeConflict, TakenArtifact

_CHUNK = 1 << 20
_DIR = "taken"
_FILE = "bundle"


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
        held = jobs.taken_artifact(art.session, art.ticket)
        if held is not None and held != want:
            raise TakeFailed("take-conflict", "this session's ticket is already taken with other digests")
        if rel is not None and not (held is not None and _holds(jobs, rel, art.bundle_digest)):
            _copy(jobs, os.fspath(export_dir), art, rel)
        if held is None:
            jobs.record_take(want)
    except TakeFailed:
        raise
    except TakeConflict as e:
        raise TakeFailed("take-conflict", str(e)) from None
    except OSError as e:
        raise TakeFailed("take-failed", f"{type(e).__name__}: {e.strerror or ''}".rstrip(": ")) from None
    except Exception as e:                       # the store's own refusals (a vanished or foreign store)
        raise TakeFailed("take-failed", type(e).__name__) from None
    return want


def _relative(art: ExportedArtifact) -> str:
    return f"{_DIR}/{art.session}-{hashlib.sha256(art.ticket.encode()).hexdigest()[:16]}/{_FILE}"


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
    taken = os.path.dirname(ddir)
    for d in (taken, ddir):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)                           # the umask and a pre-existing directory leave the mode alone
    sfd = dfd = tfd = -1
    tmp = os.path.join(ddir, f".{_FILE}.{secrets.token_hex(8)}")
    try:
        try:
            sfd = os.open(os.path.join(export_dir, art.session), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            dfd = os.open(art.bundle, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=sfd)
        except FileNotFoundError:
            raise TakeFailed("take-bundle-absent", "the export holds no bundle the artifact names") from None
        except OSError as e:
            raise TakeFailed("take-bundle-absent", f"the bundle cannot be opened ({type(e).__name__})") from None
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
        _fsync_dir(ddir)
        _fsync_dir(taken)
    finally:
        for fd in (tfd, dfd, sfd):
            if fd >= 0:
                os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = ["TakeFailed", "take_artifact"]
