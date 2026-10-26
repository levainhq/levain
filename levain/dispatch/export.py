"""Dispatch's reader of the executor's export artifact (CONTRACT §3.5 Results): ``<export dir>/<session>/dispatch-artifact.json``.

Root builds the file in the same publish transaction as the bundle (CONTRACT §10.2); dispatch reads it as a member of the
export group, never writes it. :func:`read_artifact` returns an :class:`ExportedArtifact` only for a file that is exactly
the ``levain-dispatch-artifact/1`` shape (a ``format`` key is optional; present, it is that value): anything else
(absent, not a regular file, too large, unparseable, a key missing or extra, a field of the wrong type) raises :class:`ArtifactUnreadable` by name, so a caller never acts on a
partial record.

The file is NOT an :class:`levain.dispatch.adopt.ArtifactRecord`: that record is a verified RUN (staged commit, tree,
verifier, model), which the verifier's report writes; this file names the session's exported bundle and its manifest.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass

from levain.contracts import ContractError
from levain.contracts.canon import parse
from levain.dispatch import DispatchError
from levain.slot.grammar import SESSION_RE

FORMAT = "levain-dispatch-artifact/1"
FILE = "dispatch-artifact.json"
#: the file holds seven short fields; a larger one is not root's
MAX_BYTES = 16 * 1024
#: the fields CONTRACT §3.5 names; a `format` key, which the contract does not define, is allowed beside them
_KEYS = frozenset({"session", "ticket", "manifest_digest", "bundle", "bundle_digest", "files", "bytes"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)


class ArtifactUnreadable(DispatchError):
    """The export holds no artifact dispatch may act on; ``name`` says why."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}" if detail else name)
        self.name = name


@dataclass(frozen=True)
class ExportedArtifact:
    """``levain-dispatch-artifact/1``. ``bundle`` and ``bundle_digest`` are both None for an ``adopt-nothing-new``
    import (no bundle), and both set otherwise."""
    session: str
    ticket: str
    manifest_digest: str
    bundle: str | None
    bundle_digest: str | None
    files: int
    bytes: int


def read_artifact(export_dir: str | os.PathLike[str], session: str) -> ExportedArtifact:
    """The artifact of ``session`` under ``export_dir``; raises :class:`ArtifactUnreadable` for anything short of a
    whole, well-formed one."""
    if not isinstance(session, str) or SESSION_RE.fullmatch(session) is None:
        raise ArtifactUnreadable("bad-session", "not a session id")
    text = _read(os.fspath(export_dir), session)
    try:
        obj = parse(text)
    except ContractError as e:
        raise ArtifactUnreadable("unparseable", str(e)) from None
    return _shape(obj, session)


def _read(export_dir: str, session: str) -> bytes:
    """The file's bytes, opened without following a link at the session directory or at the file, as a regular file
    only (O_NONBLOCK so a FIFO planted there cannot block the open)."""
    dfd = ffd = -1
    try:
        try:
            dfd = os.open(os.path.join(export_dir, session), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            ffd = os.open(FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            raise ArtifactUnreadable("absent", f"no {FILE} for the session") from None
        except OSError as e:
            raise ArtifactUnreadable("unopenable", type(e).__name__) from None
        st = os.fstat(ffd)
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactUnreadable("not-regular", f"{FILE} is not a regular file")
        if st.st_size > MAX_BYTES:
            raise ArtifactUnreadable("too-large", f"{FILE} is over {MAX_BYTES} bytes")
        try:
            data = os.read(ffd, MAX_BYTES + 1)
        except OSError as e:
            raise ArtifactUnreadable("unreadable", type(e).__name__) from None
        if len(data) > MAX_BYTES or len(data) != st.st_size:
            raise ArtifactUnreadable("changed", f"{FILE} changed while it was read")
        return data
    finally:
        for fd in (ffd, dfd):
            if fd >= 0:
                os.close(fd)


def _shape(obj: object, session: str) -> ExportedArtifact:
    if not isinstance(obj, dict) or set(obj) - {"format"} != _KEYS:
        raise ArtifactUnreadable("shape", "not exactly the levain-dispatch-artifact/1 keys")
    if "format" in obj and obj["format"] != FORMAT:
        raise ArtifactUnreadable("format", "not levain-dispatch-artifact/1")
    if obj["session"] != session:
        raise ArtifactUnreadable("session-mismatch", "the file names another session than its directory")
    ticket = obj["ticket"]
    if not isinstance(ticket, str) or not ticket:
        raise ArtifactUnreadable("shape", "ticket is a non-empty string")
    if not _is_digest(obj["manifest_digest"]):
        raise ArtifactUnreadable("shape", "manifest_digest is sha256:<64 hex>")
    bundle, bundle_digest = obj["bundle"], obj["bundle_digest"]
    if bundle is None:
        if bundle_digest is not None:
            raise ArtifactUnreadable("shape", "a null bundle has a null bundle_digest")
    elif bundle != "bundle" or not _is_digest(bundle_digest):
        raise ArtifactUnreadable("shape", "bundle is \"bundle\" with a sha256 bundle_digest, or both null")
    for name in ("files", "bytes"):
        v = obj[name]
        if type(v) is not int or v < 0:
            raise ArtifactUnreadable("shape", f"{name} is a non-negative integer")
    return ExportedArtifact(session, ticket, obj["manifest_digest"], bundle, bundle_digest, obj["files"], obj["bytes"])


def _is_digest(v: object) -> bool:
    return isinstance(v, str) and _DIGEST.fullmatch(v) is not None


__all__ = ["ArtifactUnreadable", "ExportedArtifact", "FILE", "FORMAT", "MAX_BYTES", "read_artifact"]
