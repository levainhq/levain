"""levain.slot.workspace: the run server's half of the workspace stager (levain 34, W1; design
``1105-32/DESIGN_render_wiring.md`` finding 3, cut W1; research ``1105-3/RESEARCH_stager.md``).

The run server (``_levain_dispatch``) cannot read the operator's repo, so the repo's snapshot reaches it as bytes on the
run socket. The operator-uid side (a later lane) builds the snapshot, cuts ``git bundle create - HEAD refs/heads/readset``
of it and sends two parts, the canonical :class:`levain.dispatch.snapshot.Manifest` and the bundle, as bounded WSDATA
chunks (``session_client.ws_chunk_frames``) after a WSBEGIN that declares their sizes and digests. This module is what
the server does with them. Two phases, one per side of the mint:

1. **Verify, before anything is allocated** (:meth:`WorkspaceUploads.verify`, called by the LAUNCH op after the
   authority check). Every refusal here is ``session_refused:workspace`` (:data:`WORKSPACE_REFUSAL`, the cause in the
   detail), leaves nothing staged, and drops the upload. In order: the parts are whole and hash to what WSBEGIN
   declared; the manifest parses to exactly its canonical form; the workspace member's ``head`` and ``withhold`` are
   the manifest's; (W1) withhold is empty; the snapshot carries none of :data:`DISPATCH_REFUSED_NAMES`;
   ``snapshot_commit`` derives ``E`` (the commit) and ``N`` (the object count) from the manifest alone; the bundle's
   first bytes are exactly the bundle header for ``E`` and a pack header of ``N`` objects; then
   the read set's own ingest (:func:`levain.dispatch.readset._verify_in`) takes the bundle into a private scratch clone
   (fsck on, rlimits, no hooks) and checks ``HEAD`` and ``refs/heads/readset`` are ``E``, no other ref, in-pack objects == ``N``, none
   loose, and the repo-layer settings check (:func:`levain.slot.render.repo_loosening_refusals` through
   ``supervisor.check_repo_layer``, the very function the interactive path binds): a snapshot whose
   ``.claude/settings*.json`` loosens the sandbox is REFUSED, never stripped.
2. **Stage, after the slot is prepared** (:meth:`WorkspaceUploads.stage`, the callable ``HelperSlotRuntime`` is given as
   ``workspace=``): the verified bytes go to ``staging/repo.bundle`` (``O_EXCL|O_NOFOLLOW``, 0600, ``full_fsync``, the
   prefix read back), and the file is removed again on any failure after it was created. The slot's own stage step
   (``stage.stage_bundle``) and its clone with the V3/V4 check still treat the file as hostile.

What is bound is HASH IDENTITY, not a signature (research frame 1, Phill's row pending): the manifest is unsigned, the
server re-derives ``E`` and ``N`` from it and refuses on any mismatch, and the run record carries the head sha, the
manifest digest and the withhold digest (``RealLauncher._workspace_record``). Each pending alternative is one edit:
a signed manifest verifies in :func:`manifest_from_bytes`'s caller (:meth:`WorkspaceUploads._judge`); withhold from a
role is :data:`W1_WITHHOLD_ALLOWED`; ``.mcp.json`` parity with the interactive path is :data:`DISPATCH_REFUSED_NAMES`.

Not done here (the operator-uid side, a later lane): building the snapshot, cutting the bundle, sending the frames. Not
done on either side: the built-in NEVER list and the content scan run where the repo is readable (``plan_snapshot``), and
the server does not repeat them on the received blobs.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import shutil
import stat
import struct
import threading
import unicodedata
from pathlib import Path
from typing import Mapping, Optional

from levain.agents.session import Workspace
from levain.agents.session_launcher import LauncherRefused, SlotStart
from levain.contracts import ContractError, canon
from levain.dispatch import DispatchError
from levain.dispatch import readset as RS
from levain.dispatch import snapshot as SN
from levain.dispatch.policy import path_text_problem
from levain.slot import stage, write_all

__all__ = ["BUNDLE_MAX_BYTES", "DISPATCH_REFUSED_NAMES", "MANIFEST_MAX_BYTES", "MAX_UPLOADS", "UploadOwner",
           "W1_WITHHOLD_ALLOWED", "WORKSPACE_REFUSAL", "WorkspaceRefused", "WorkspaceUploads", "manifest_from_bytes"]

_log = logging.getLogger("levain.slot.workspace")

#: The one refusal name every workspace failure answers LAUNCH with; the cause is the detail's first word.
WORKSPACE_REFUSAL = "session_refused:workspace"
#: [judged] a manifest of SNAPSHOT_MAX_FILES entries at a few hundred bytes each
MANIFEST_MAX_BYTES = 8 * 1024 ** 2
#: [judged, a guess like the snapshot bounds, DESIGN 9.4 v: wants a real-repo calibration run] the snapshot's byte bound
#: plus pack and tree overhead. Never above the slot's own V19 cap (``stage.BUNDLE_CAP``).
BUNDLE_MAX_BYTES = min(stage.BUNDLE_CAP, SN.SNAPSHOT_MAX_BYTES + 8 * 1024 ** 2)
#: [judged] uploads held at once: each holds its two parts in memory until it is staged or dropped
MAX_UPLOADS = 4
#: W1 sends no role withhold (``ceiling.withhold`` is W2 and a role-hash change, research frame 2): the built-in NEVER
#: list and the content scan still apply where the repo is read. True would admit a withhold list that the manifest and
#: the workspace member agree on.
W1_WITHHOLD_ALLOWED = False
#: Repo-root names a dispatch snapshot may not carry (research frame 4 reads ``.mcp.json`` with the settings: nobody is
#: there to approve a project's MCP servers). The interactive path launches it (``render.REPO_DISCOVERY``, "never
#: refused"); an empty tuple is that parity.
DISPATCH_REFUSED_NAMES = (".mcp.json",)

_MODES = ("100644", "100755")
_SCRATCH_IN = "in-"
_O_DIR = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC


class WorkspaceRefused(LauncherRefused):
    """``session_refused:workspace`` with the cause as the detail's first word (``cause``) and a human tail. Standing:
    the same upload meets the same answer."""

    def __init__(self, cause: str, detail: str = "") -> None:
        self.cause = cause
        super().__init__(WORKSPACE_REFUSAL, f"{cause}: {detail[:300]}" if detail else cause)


def _fold(name: str) -> str:
    # the same fold the settings check uses (supervisor._fold): a case- and normalization-insensitive store
    return unicodedata.normalize("NFD", name).casefold()


def _remove_workdir(pfd: int, parent: str, name: str, made: tuple) -> None:
    """Remove the workdir this verify made (by dev/ino, never a symlink or another directory). By fd when
    ``shutil.rmtree`` can (3.11+); else ``shutil.rmtree`` by path where it still avoids symlink attacks (the fd-walking
    form on Linux and macOS), inside a parent only this uid can write. A directory that cannot be removed is logged and
    left for the start-up sweep: it holds git's object store of a refused bundle, never a link."""
    try:
        st = os.lstat(name, dir_fd=pfd)
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != made:
            return
        if RS._rmtree_by_fd():
            shutil.rmtree(name, dir_fd=pfd)
        elif shutil.rmtree.avoids_symlink_attacks:
            shutil.rmtree(os.path.join(parent, name))
        else:
            _log.warning("the workspace workdir %s was left for the sweep: shutil.rmtree is symlink-unsafe here", name)
    except OSError as exc:
        _log.warning("the workspace workdir %s could not be removed (%s)", name, type(exc).__name__)


def manifest_from_bytes(data: bytes) -> SN.Manifest:
    """The manifest ``data`` says, or :class:`WorkspaceRefused`. ``data`` must be exactly that manifest's canonical
    bytes (so the digest the run record carries is one every honest party computes), its format and the fields the
    snapshot module writes, ``history`` 0, entries sorted and unique, every mode regular, every blob a 40-hex sha, every
    path inside the snapshot's own hygiene (UTF-8 text that shows as itself, NFC, ``readset``'s path checks), at most
    ``SNAPSHOT_MAX_FILES`` of them."""
    try:
        obj = canon.parse(data)
    except ContractError:
        raise WorkspaceRefused("manifest-malformed", "not canonical JSON") from None
    keys = {"format", "head", "history", "withhold", "withhold_digest", "withheld_paths", "entries"}
    if type(obj) is not dict or set(obj) != keys or canon.canonical_bytes(obj) != data:
        raise WorkspaceRefused("manifest-malformed", "not the snapshot manifest's canonical shape")
    entries_raw, withhold, withheld = obj["entries"], obj["withhold"], obj["withheld_paths"]
    if obj["format"] != SN.FORMAT or type(obj["head"]) is not str or type(obj["history"]) is not int \
            or type(withhold) is not list or type(withheld) is not list or type(entries_raw) is not list \
            or any(type(x) is not str for x in (*withhold, *withheld)):
        raise WorkspaceRefused("manifest-malformed", "a field has the wrong type")
    if len(entries_raw) > SN.SNAPSHOT_MAX_FILES:
        raise WorkspaceRefused("manifest-too-many-files", f"{len(entries_raw)} entries")
    entries = []
    for e in entries_raw:
        if type(e) is not list or len(e) != 3 or any(type(x) is not str for x in e) or e[1] not in _MODES \
                or SN._SHA1.fullmatch(e[2]) is None:      # the snapshot's own sha1 pattern
            raise WorkspaceRefused("manifest-malformed", "an entry is not [path, regular mode, blob sha]")
        entries.append((e[0], e[1], e[2]))
    if SN._SHA1.fullmatch(obj["head"]) is None:
        raise WorkspaceRefused("manifest-malformed", "head is not a commit sha")
    paths = [e[0] for e in entries]
    if entries != sorted(entries) or len(set(paths)) != len(paths):
        raise WorkspaceRefused("manifest-malformed", "entries are not sorted and unique")
    for p in paths:
        if path_text_problem(p) is not None or unicodedata.normalize("NFC", p) != p:
            raise WorkspaceRefused("manifest-path", f"{p[:120]!r} cannot show as itself or is not NFC")
    try:
        RS._path_checks(paths)
        pats = SN._withhold_patterns(withhold)
    except (RS.ReadSetRefused, SN.SnapshotRefused) as exc:
        raise WorkspaceRefused("manifest-path" if isinstance(exc, RS.ReadSetRefused) else "manifest-withhold",
                               exc.detail) from None
    m = SN.Manifest(head=obj["head"], withhold=pats, withheld_paths=tuple(withheld), entries=tuple(entries),
                    history=obj["history"])
    if m.canonical() != data:       # the withhold digest, the order of withhold and withheld paths: all or nothing
        raise WorkspaceRefused("manifest-malformed", "does not round-trip to its own canonical bytes")
    return m


class UploadOwner:
    """One connection's claim on the uploads it began, so the connection ending drops what the LAUNCH did not take."""
    __slots__ = ("keys",)

    def __init__(self) -> None:
        self.keys: set = set()


@dataclasses.dataclass
class _Verified:
    manifest_digest: str
    commit: str
    objects: int


class _Upload:
    def __init__(self, key: str, owner: UploadOwner, sizes: Mapping[str, int], digests: Mapping[str, str]) -> None:
        self.key, self.owner = key, owner
        self.sizes, self.digests = dict(sizes), dict(digests)
        self.parts = {"manifest": bytearray(), "bundle": bytearray()}
        self.verified: Optional[_Verified] = None


class WorkspaceUploads:
    """The server's uploads (by session key) and the two phases that consume them (module docstring). ``scratch`` is
    the dispatch-owned directory the verify clones in (created 0700 on first use). Thread-safe: one lock guards the
    table; an upload is worked by its own connection's thread only."""

    def __init__(self, scratch: "str | os.PathLike[str]", *, max_uploads: int = MAX_UPLOADS,
                 manifest_max: int = MANIFEST_MAX_BYTES, bundle_max: int = BUNDLE_MAX_BYTES) -> None:
        self._scratch = os.fspath(scratch)
        self._max, self._manifest_max, self._bundle_max = max_uploads, manifest_max, bundle_max
        self._mu = threading.Lock()
        self._ups: dict = {}

    # -- the upload ------------------------------------------------------------------------------------------------

    def begin(self, key: str, owner: UploadOwner, sizes: Mapping[str, int], digests: Mapping[str, str]) -> None:
        """WSBEGIN: declare the two parts. Refused ``session_refused:workspace`` for a part over its bound or a key
        that already has an upload; ``no-workspace-capacity`` (transient) when :data:`MAX_UPLOADS` are held."""
        if sizes["manifest"] > self._manifest_max:
            raise WorkspaceRefused("manifest-over-cap", f"{sizes['manifest']} bytes is over {self._manifest_max}")
        if sizes["bundle"] > self._bundle_max:
            raise WorkspaceRefused("bundle-over-cap", f"{sizes['bundle']} bytes is over {self._bundle_max}")
        with self._mu:
            if key in self._ups:
                raise WorkspaceRefused("upload-exists", "this session key already has an upload")
            if len(self._ups) >= self._max:
                raise LauncherRefused("no-workspace-capacity", "every workspace upload slot is taken",
                                      reason_class="transient")
            self._ups[key] = _Upload(key, owner, sizes, digests)
            owner.keys.add(key)

    def chunk(self, key: str, owner: UploadOwner, part: str, offset: int, raw: bytes) -> int:
        """WSDATA: append one chunk, in order, inside the part's declared size; the bytes held so far. Any refusal
        drops the whole upload (a half upload is never kept)."""
        with self._mu:
            up = self._ups.get(key)
        if up is None or up.owner is not owner:
            raise WorkspaceRefused("no-upload", "no upload is open for this key on this connection")
        buf = up.parts[part]
        if offset != len(buf) or len(buf) + len(raw) > up.sizes[part]:
            self.drop(key)
            raise WorkspaceRefused("chunk-out-of-order" if offset != len(buf) else "chunk-over-size",
                                   f"{part} at {offset}, held {len(buf)} of {up.sizes[part]}")
        buf += raw
        return len(buf)

    def drop(self, key: str) -> None:
        with self._mu:
            up = self._ups.pop(key, None)
        if up is not None:
            up.owner.keys.discard(key)

    def drop_owner(self, owner: UploadOwner) -> None:
        for key in list(owner.keys):
            self.drop(key)

    # -- phase 1: verify (before the mint) -------------------------------------------------------------------------

    def verify(self, key: str, owner: UploadOwner, ws: Optional[Workspace]) -> Optional[Workspace]:
        """The LAUNCH's workspace judgment. No member and no upload: ``None`` (a launch that stages nothing). A member
        and a whole, matching upload: the member with ``manifest_digest`` set to the server's own derivation. Anything
        else: :class:`WorkspaceRefused`, the upload dropped."""
        with self._mu:
            up = self._ups.get(key)
        if ws is None:
            if up is None:
                return None
            self.drop(key)
            raise WorkspaceRefused("upload-without-workspace", "bytes were sent but the launch names no workspace")
        if up is None or up.owner is not owner:
            raise WorkspaceRefused("no-upload", "the launch names a workspace and no upload is open for its key")
        try:
            return dataclasses.replace(ws, manifest_digest=self._judge(up, ws))
        except WorkspaceRefused:
            self.drop(key)
            raise
        except (RS.ReadSetRefused, SN.SnapshotRefused, DispatchError) as exc:
            self.drop(key)
            raise WorkspaceRefused(getattr(exc, "name", "verify-failed"), getattr(exc, "detail", "")) from None
        except Exception as exc:  # noqa: BLE001 - a judgment that broke authorises nothing
            self.drop(key)
            _log.warning("the workspace verify for %s raised %s", key, type(exc).__name__)
            raise WorkspaceRefused("verify-failed", type(exc).__name__) from None

    def _judge(self, up: _Upload, ws: Workspace) -> str:
        parts = up.parts                  # the held bytearrays, not copies: an upload is up to ~80 MiB, four may be held
        for part, data in parts.items():
            if len(data) != up.sizes[part]:
                raise WorkspaceRefused("upload-incomplete", f"{part}: {len(data)} of {up.sizes[part]} bytes")
            if "sha256:" + hashlib.sha256(data).hexdigest() != up.digests[part]:
                raise WorkspaceRefused(f"{part}-digest", "the bytes held are not the bytes WSBEGIN declared")
        manifest = manifest_from_bytes(bytes(parts["manifest"]))
        if not W1_WITHHOLD_ALLOWED and (ws.withhold or manifest.withhold or manifest.withheld_paths):
            raise WorkspaceRefused("withhold-not-in-w1", "W1 stages a snapshot with an empty withhold")
        if manifest.head != ws.head:
            raise WorkspaceRefused("head-mismatch", "the manifest was cut from another commit than the launch names")
        if manifest.withhold != tuple(sorted(ws.withhold)):
            raise WorkspaceRefused("withhold-mismatch", "the manifest's withhold is not the launch's")
        refused = {_fold(n) for n in DISPATCH_REFUSED_NAMES}
        for path, _mode, _blob in manifest.entries:
            if _fold(path) in refused:
                raise WorkspaceRefused("repo-carries-" + path.lstrip(".").replace(".", "-").lower(),
                                       f"{path}: a dispatch snapshot does not carry it (refused, never stripped)")
        try:
            commit, _tree, objects = SN.snapshot_commit(manifest)
        except Exception as exc:  # noqa: BLE001 - e.g. a path that is both a file and a directory
            raise WorkspaceRefused("manifest-underivable", type(exc).__name__) from None
        bundle = parts["bundle"]
        header = RS._header(commit)
        if bundle[:len(header)] != header:
            raise WorkspaceRefused("bundle-header", "the bundle header is not the manifest's snapshot")
        pack = bundle[len(header):len(header) + 12]
        if len(pack) != 12 or pack[:4] != b"PACK":
            raise WorkspaceRefused("bundle-pack", "no pack header follows the bundle header")
        version, count = struct.unpack(">II", pack[4:])
        if version not in (2, 3) or count != objects:
            raise WorkspaceRefused("object-count", f"the pack holds {count} objects, the manifest derives {objects}")
        digest = manifest.digest()
        self._clone_check(up.key, bundle, commit, objects, digest)
        up.verified = _Verified(digest, commit, objects)
        return digest

    def _clone_check(self, key: str, bundle: bytes, commit: str, objects: int, digest: str) -> None:
        """The ingest, in a scratch clone: the bundle written to a private file, then the read set's own checks on it
        (:func:`readset._verify_in`: header compare, pack count, bounded fsck'd clone, refs, object count, the repo-layer
        settings check), then the clone and the file removed. Whatever it raises is a refusal.

        This does not go through ``readset.verify_derived``/``verify_readset``: those refuse where ``shutil.rmtree``
        takes no ``dir_fd`` (before 3.11), and this runs in the dispatch run server, a service under ROOT_PYTHON (3.9 on
        macOS). Only the REMOVAL differs: by fd where Python can (:func:`readset._rmtree_at`), else
        :func:`_remove_workdir`'s checked ``shutil.rmtree`` of a directory only this uid can enter."""
        parent = os.path.realpath(self._scratch)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        pfd = RS._open_parent(parent)          # a real directory, this uid's, not group- or world-writable
        name, inp = RS.workdir_name(key), _SCRATCH_IN + key + ".bundle"
        fd, made = -1, None
        try:
            fd = os.open(inp, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=pfd)
            write_all(fd, bundle)
            os.mkdir(name, 0o700, dir_fd=pfd)
            st = os.lstat(name, dir_fd=pfd)
            made = (st.st_dev, st.st_ino)
            cap, deadline_s = RS._limits()
            RS._verify_in(pfd, digest, fd, parent, name, key, commit, objects, cap, deadline_s)
        finally:
            if made is not None:
                _remove_workdir(pfd, parent, name, made)
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(inp, dir_fd=pfd)
            except OSError:
                pass
            os.close(pfd)

    def sweep(self) -> None:
        """At server start (one instance, nothing in flight): remove what a crashed verify left in the scratch, the
        workdirs and the input files, and nothing else. Never raises."""
        try:
            if not os.path.isdir(self._scratch):
                return
            parent = os.path.realpath(self._scratch)
            pfd = RS._open_parent(parent)
            try:
                for name in sorted(os.listdir(pfd)):
                    st = os.lstat(name, dir_fd=pfd)
                    if name.startswith(_SCRATCH_IN) and stat.S_ISREG(st.st_mode):
                        os.unlink(name, dir_fd=pfd)
                    elif name.startswith(RS.WORKDIR_PREFIX) and stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid():
                        _remove_workdir(pfd, parent, name, (st.st_dev, st.st_ino))
            finally:
                os.close(pfd)
        except Exception as exc:  # noqa: BLE001
            _log.warning("the workspace scratch sweep raised %s", type(exc).__name__)

    # -- phase 2: stage (after prepare) ----------------------------------------------------------------------------

    def stage(self, req: SlotStart, staging: Path) -> None:
        """The ``HelperSlotRuntime(workspace=...)`` callable: write the verified bundle to ``staging/repo.bundle``. A
        request with no workspace stages nothing. The request's member must carry the digest the verify derived for
        THIS key (so a SlotStart this server did not verify stages nothing). The upload is consumed either way."""
        ws = req.workspace
        if ws is None:
            return
        with self._mu:
            up = self._ups.pop(req.session_key, None)
        if up is not None:
            up.owner.keys.discard(req.session_key)
        if up is None or up.verified is None or ws.manifest_digest != up.verified.manifest_digest:
            raise WorkspaceRefused("not-verified", "the launch's workspace was not verified by this server")
        v = up.verified
        data = up.parts["bundle"]
        try:
            sfd = os.open(os.fspath(staging), _O_DIR)
        except OSError as exc:
            raise WorkspaceRefused("staging-unopenable", type(exc).__name__) from None
        fd = -1
        created = False
        try:
            try:
                fd = os.open(stage.BUNDLE_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=sfd)
            except OSError as exc:      # an existing name, a symlink included: never opened, never written through
                raise WorkspaceRefused("staging-dest", f"{stage.BUNDLE_NAME} cannot be created "
                                       f"({type(exc).__name__})") from None
            created = True
            os.fchmod(fd, 0o600)        # the umask must not narrow it: the same-uid reader opens this file
            write_all(fd, data)
            RS.full_fsync(fd)
            if os.fstat(fd).st_size != len(data) or RS._prefix_problem(fd, v.commit, v.objects) is not None:
                raise WorkspaceRefused("staging-readback", "the file staged is not the bundle verified")
            os.close(fd)
            fd = -1
            RS.full_fsync(sfd)
        except BaseException:
            if fd >= 0:
                os.close(fd)
                fd = -1
            if created:
                try:
                    os.unlink(stage.BUNDLE_NAME, dir_fd=sfd)
                    RS.full_fsync(sfd)
                except OSError as exc:
                    _log.warning("the refused workspace bundle could not be removed from staging (%s)",
                                 type(exc).__name__)
            raise
        finally:
            os.close(sfd)
