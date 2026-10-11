"""levain.cockpit.install: the broker's install class and the start checks it owns (K2b design §6,
invariant 7).

- ``levain serve --install-class reported`` is the only way to ``reported``. With no flag and no
  governed marker the broker is disabled and every write route answers "run the installer"; the read
  server still starts (§6).
- A governed marker names its data root by ``sha256(realpath(data_root))[:32]``. The governed checks
  (root-owned marker, uid, mode 0700, no ACL) are owed by K2b-3 (§13); a governed marker here yields
  the typed refusal "governed install not built", and the same marker beside ``--install-class
  reported`` refuses to start (the operator's uid may not declare away a root-held class).
- Local filesystems only, both classes (§6, r1 codex A3): every store CAS rests on ``flock``, which
  can silently no-op on a network filesystem, so a data root or registered store path whose
  ``statfs`` type is one is refused with the type named.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["GOVERNED", "INSTALL_CLASSES", "InstallDecision", "REPORTED", "decide_install",
           "marker_dir", "marker_path", "network_fs_problem", "statfs_type"]

REPORTED = "reported"
GOVERNED = "governed"
INSTALL_CLASSES = (REPORTED,)          # what ``--install-class`` accepts (§6)

# §6: the marker directories, root-owned
_MARKER_DIRS = {"darwin": Path("/Library/Application Support/Levain/install"),
                "linux": Path("/etc/levain/install")}

# macOS ``f_fstypename`` values of network filesystems (§6)
_MAC_NETWORK = frozenset({"nfs", "smbfs", "afpfs", "webdav"})
# Linux ``f_type`` magic numbers (linux/magic.h): NFS, SMB, CIFS, SMB2, FUSE (sshfs rides FUSE)
_LINUX_NETWORK = {0x6969: "nfs", 0x517B: "smb", 0xFF534D42: "cifs", 0xFE534D42: "smb2",
                  0x65735546: "fuse"}


@dataclass(frozen=True)
class InstallDecision:
    """``install_class`` is ``reported`` when the broker may start; ``None`` with ``reason`` when it
    stays disabled (``INSTALL_DISABLED``, never healed)."""
    install_class: str | None
    reason: str | None


def marker_dir() -> Path | None:
    return _MARKER_DIRS.get(sys.platform)


def marker_path(data_root: Path | str, directory: Path | None = None) -> Path | None:
    d = directory if directory is not None else marker_dir()
    if d is None:
        return None
    digest = hashlib.sha256(os.path.realpath(data_root).encode("utf-8")).hexdigest()[:32]
    return d / f"{digest}.json"


def marker_present(marker: Path | None) -> bool:
    """Whether a governed marker exists. Only ENOENT is absence: any other ``lstat`` failure (EACCES,
    EIO, ELOOP) raises, so an unreadable marker is never taken for a reported root."""
    if marker is None:
        return False
    try:
        os.lstat(marker)
    except FileNotFoundError:
        return False
    return True


def marker_unreadable(marker: Path | None, exc: OSError) -> str:
    return f"cannot read the governed marker path ({marker}): {exc.strerror or exc}"


def decide_install(declared: str | None, data_root: Path | str, *,
                   marker_directory: Path | None = None) -> InstallDecision:
    """The install class the broker starts under, from the ``--install-class`` declaration and the
    marker for THIS data root (another root's marker is ignored)."""
    if declared is not None and declared not in INSTALL_CLASSES:
        return InstallDecision(None, f"unknown install class {declared!r}")
    marker = marker_path(data_root, marker_directory)
    try:
        has_marker = marker_present(marker)
    except OSError as exc:
        return InstallDecision(None, marker_unreadable(marker, exc))
    if has_marker and declared == REPORTED:
        return InstallDecision(None, f"a governed marker names this data root ({marker}); "
                                     "--install-class reported cannot declare it away")
    if has_marker:
        return InstallDecision(None, "governed install not built")
    if declared == REPORTED:
        return InstallDecision(REPORTED, None)
    return InstallDecision(None, "run the installer (or start with --install-class reported)")


# --- statfs ---------------------------------------------------------------------------------------

class _MacStatfs(ctypes.Structure):
    # <sys/mount.h> struct statfs with 64-bit inodes, the only layout on arm64 and the $INODE64 one on x86_64
    _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7)]


class _LinuxStatfs(ctypes.Structure):
    # only f_type is read; the buffer is oversized so the kernel's whole struct fits
    _fields_ = [("f_type", ctypes.c_long), ("_rest", ctypes.c_byte * 256)]


def statfs_type(path: Path | str) -> str:
    """The filesystem type of ``path``: macOS ``f_fstypename``, Linux ``f_type`` as ``0x..`` hex.
    ``OSError`` when the call fails."""
    libc = ctypes.CDLL(None, use_errno=True)
    raw = os.fsencode(os.fspath(path))
    if sys.platform == "darwin":
        fn = getattr(libc, "statfs$INODE64", None) or libc.statfs
        buf = _MacStatfs()
        fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(_MacStatfs)]
        if fn(raw, ctypes.byref(buf)) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), os.fspath(path))
        return buf.f_fstypename.decode("utf-8", "replace")
    lbuf = _LinuxStatfs()
    lfn = libc.statfs
    lfn.argtypes = [ctypes.c_char_p, ctypes.POINTER(_LinuxStatfs)]
    if lfn(raw, ctypes.byref(lbuf)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), os.fspath(path))
    return hex(lbuf.f_type & 0xFFFFFFFF)


def _nearest_existing(path: Path) -> Path:
    p = Path(os.path.abspath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def network_fs_problem(paths: Iterable[Path | str]) -> str | None:
    """Why one of ``paths`` (the data root and every registered store path) may not hold a cockpit
    store, or ``None``. A path that does not exist yet is judged by its nearest existing ancestor,
    where it would be created. A failed ``statfs`` refuses too: an unknown filesystem is not a
    local one."""
    for raw in paths:
        p = _nearest_existing(Path(raw))
        try:
            kind = statfs_type(p)
        except OSError as exc:
            return f"cannot read the filesystem type of {p} ({exc.strerror or exc})"
        if sys.platform == "darwin":
            if kind.lower() in _MAC_NETWORK:
                return f"{raw} is on a network filesystem ({kind}); flock may not lock there"
        else:
            try:
                name = _LINUX_NETWORK.get(int(kind, 16))
            except ValueError:
                return f"unrecognised filesystem type {kind!r} at {p}"
            if name is not None:
                return f"{raw} is on a network filesystem ({name}); flock may not lock there"
    return None
