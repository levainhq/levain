"""`chat_tmp`'s launch-end removal (row C8; design §3.3's `chat_tmp_dir`, §3.8.5(o); CH-D2 S8).

A worker-built tree can defeat a plain rmtree (a mode-000 directory, a directory without write permission) or point
out of itself (a symlink). :func:`remove_tree` walks it fd-relative: each directory is opened `O_NOFOLLOW` from its
parent's fd (one its owner cannot read is made 0700 first, never through a symlink, or left and named where this
platform has no such chmod: `_open_dir`) and made 0700 through that fd before it is
listed, every entry that is not a directory is unlinked (a symlink is removed as itself, never followed), then the
directory is removed. A failure is a logged leftover, never a crash; the leftovers are returned so the launch end can
name them.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

__all__ = ["remove_tree"]

log = logging.getLogger(__name__)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
#: os.chmod can refuse to follow a symlink (lchmod: macOS, the BSDs). A Linux CPython has no lchmod, and its
#: fchmodat(AT_SYMLINK_NOFOLLOW) stand-in raises ValueError / NotImplementedError where libc lacks it [judged by
#: lane-chd2-s8d, 10-25, against CPython's posixmodule/os.py; not run on Linux]. There the entry is opened
#: O_PATH|O_NOFOLLOW, checked a directory through that fd, and chmodded through `/proc/self/fd/<n>`, which names the
#: opened inode, never a path the worker can swap; with no O_PATH or no /proc/self/fd no chmod is issued at all.
_CHMOD_NOFOLLOW = os.chmod in os.supports_follow_symlinks
_O_PATH: int | None = getattr(os, "O_PATH", None)
_PROC_FD = "/proc/self/fd"
_FD_CHMOD = _O_PATH is not None and os.path.isdir(_PROC_FD)


class NoSafeChmod(OSError):
    """An unreadable directory left as it is: this platform has no chmod that cannot follow a swapped-in symlink."""


def _chmod_nofollow(dfd: int, name: str) -> None:
    """`name` under `dfd` made 0700, never through a symlink: an entry that is not a directory when opened raises."""
    if _CHMOD_NOFOLLOW:
        os.chmod(name, 0o700, dir_fd=dfd, follow_symlinks=False)
        return
    if not _FD_CHMOD:
        raise NoSafeChmod(f"{name}: no chmod here refuses a symlink")
    fd = os.open(name, _O_PATH | os.O_NOFOLLOW, dir_fd=dfd)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):        # a symlink swapped in after the lstat: never chmodded
            raise NotADirectoryError(f"{name} is no longer a directory")
        os.chmod(f"{_PROC_FD}/{fd}", 0o700)
    finally:
        os.close(fd)


def _open_dir(dfd: int, name: str) -> int:
    """`name` under `dfd` (lstat said a directory) opened O_NOFOLLOW. One its owner cannot read is made 0700 without
    following a symlink (`_chmod_nofollow`, which raises where it cannot) and opened again, O_NOFOLLOW again."""
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except PermissionError:
        pass
    _chmod_nofollow(dfd, name)
    return os.open(name, _DIR_FLAGS, dir_fd=dfd)


def _clear(dfd: int, label: str, leftovers: list[str]) -> None:
    try:
        os.fchmod(dfd, 0o700)
        names = os.listdir(dfd)
    except OSError as exc:
        leftovers.append(f"{label}: {type(exc).__name__}")
        return
    for name in names:
        where = f"{label}/{name}"
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=dfd)
                continue
            cfd = _open_dir(dfd, name)
            try:
                _clear(cfd, where, leftovers)
            finally:
                os.close(cfd)
            os.rmdir(name, dir_fd=dfd)
        except (OSError, RecursionError) as exc:
            leftovers.append(f"{where}: {type(exc).__name__}")


def remove_tree(root: Path) -> list[str]:
    """Remove `root` and everything under it without following a symlink; the leftovers, logged (empty: all gone)."""
    root = Path(root)
    leftovers: list[str] = []
    try:
        dfd = os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        return []
    except OSError as exc:                       # a symlink or a non-directory at the root itself: left, named
        leftovers.append(f"{root}: {type(exc).__name__}")
    else:
        try:
            _clear(dfd, str(root), leftovers)
        finally:
            os.close(dfd)
        try:
            os.rmdir(root)
        except OSError as exc:
            leftovers.append(f"{root}: {type(exc).__name__}")
    for item in leftovers:
        log.warning("chat_tmp leftover: %s", item)
    return leftovers
