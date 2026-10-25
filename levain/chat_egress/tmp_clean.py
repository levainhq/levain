"""`chat_tmp`'s launch-end removal (row C8; design §3.3's `chat_tmp_dir`, §3.8.5(o); CH-D2 S8).

A worker-built tree can defeat a plain rmtree (a mode-000 directory, a directory without write permission) or point
out of itself (a symlink). :func:`remove_tree` walks it fd-relative: each directory is opened `O_NOFOLLOW` from its
parent's fd and made 0700 before it is listed, every entry that is not a directory is unlinked (a symlink is removed
as itself, never followed), then the directory is removed. A failure is a logged leftover, never a crash; the
leftovers are returned so the launch end can name them.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

__all__ = ["remove_tree"]

log = logging.getLogger(__name__)

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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
            os.chmod(name, 0o700, dir_fd=dfd, follow_symlinks=False)
            cfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
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
