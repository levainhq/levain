"""CH-D2 S8 (second slice): row C8's hostile-tree red for `chat_tmp`'s launch-end removal (`tmp_clean.remove_tree`).

A worker-built tree under `chat_tmp` with a mode-000 dir, a non-writable dir and a symlink out of the tree ->
the removal removes the tree, never follows the symlink, and a remainder is a logged leftover, never a crash.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat

import pytest

from levain.chat_egress.tmp_clean import remove_tree


def _hostile(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    root = tmp_path / "chat_tmp"
    (root / "zero" / "deep").mkdir(parents=True)
    (root / "zero" / "deep" / "f").write_text("x")
    (root / "ro").mkdir()
    (root / "ro" / "f").write_text("x")
    (root / "out").symlink_to(outside)
    (root / "ro" / "out2").symlink_to(outside / "keep")
    for name, mode in (("r-only", 0o400), ("x-only", 0o100)):
        (root / name).mkdir()
        (root / name / "f").write_text("x")
        os.chmod(root / name, mode)
    os.chmod(root / "zero" / "deep", 0o000)
    os.chmod(root / "zero", 0o000)
    os.chmod(root / "ro", 0o500)
    return root, outside


def test_control_a_plain_rmtree_is_defeated_by_the_tree(tmp_path) -> None:
    root, _ = _hostile(tmp_path)
    with pytest.raises(PermissionError):
        shutil.rmtree(root)
    remove_tree(root)


def test_the_hostile_tree_is_removed_and_the_symlink_never_followed(tmp_path) -> None:
    root, outside = _hostile(tmp_path)
    assert remove_tree(root) == []
    assert not root.exists() and not root.is_symlink()
    assert (outside / "keep").read_text() == "keep" and stat.S_IMODE(os.stat(outside).st_mode) & 0o200


@pytest.mark.skipif(not hasattr(os, "chflags"), reason="UF_IMMUTABLE is macOS/BSD's (os.chflags)")
def test_a_remainder_is_a_logged_leftover_never_a_crash(tmp_path, caplog) -> None:
    root, outside = _hostile(tmp_path)
    stuck = root / "stuck"
    stuck.write_text("x")
    os.chflags(stuck, stat.UF_IMMUTABLE)
    try:
        left = remove_tree(root)
        assert left == [f"{root}/stuck: PermissionError", f"{root}: OSError"]
        assert all(f"chat_tmp leftover: {item}" in caplog.text for item in left)
        assert sorted(p.name for p in root.iterdir()) == ["stuck"]
        assert (outside / "keep").read_text() == "keep"
    finally:
        os.chflags(stuck, 0)


def test_a_root_that_is_a_symlink_is_left_and_named(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (tmp_path / "chat_tmp").symlink_to(outside)
    # O_NOFOLLOW on the symlink: macOS answers ENOTDIR (NotADirectoryError), Linux ELOOP (a plain OSError)
    assert remove_tree(tmp_path / "chat_tmp") in ([f"{tmp_path / 'chat_tmp'}: NotADirectoryError"],
                                                  [f"{tmp_path / 'chat_tmp'}: OSError"])
    assert (outside / "keep").exists()
    assert remove_tree(tmp_path / "absent") == []


# --- S8b r1 item 6: the same removal where os.chmod cannot refuse to follow a symlink (Linux) -----------------------

def _linux_chmod(how: str):
    """os.chmod as a Linux CPython build answers `follow_symlinks=False`: it has no lchmod, so it calls
    fchmodat(AT_SYMLINK_NOFOLLOW), which fails with ENOTSUP on glibc before 2.32 or without /proc; CPython then
    raises ValueError (with dir_fd) or NotImplementedError (without). `errno` is the raw ENOTSUP, for completeness."""
    real = os.chmod

    def chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
        if not follow_symlinks:
            if how == "errno":
                raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))
            if dir_fd is not None:
                raise ValueError("chmod: cannot use dir_fd and follow_symlinks together")
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        return real(path, mode, dir_fd=dir_fd)
    return chmod


def _restore(root) -> None:
    for p in (root / "zero", root / "zero" / "deep", root / "ro", root / "r-only", root / "x-only"):  # parent first
        if p.is_dir() and not p.is_symlink():
            os.chmod(p, 0o700)


@pytest.mark.parametrize("how", ["valueerror", "errno"])
def test_the_hostile_tree_is_removed_where_chmod_cannot_refuse_a_symlink(how, tmp_path, monkeypatch) -> None:
    """With an fd chmod (Linux O_PATH + /proc/self/fd, emulated by `_FdChmodRig`) the whole tree goes."""
    from levain.chat_egress import tmp_clean
    root, outside = _hostile(tmp_path)
    rig = _FdChmodRig(monkeypatch, _linux_chmod(how))
    try:
        left = remove_tree(root)
    finally:
        monkeypatch.undo()
        _restore(root)
    print(f"RUN S8D item 6 simulated Linux chmod ({how}): leftovers {left}; root exists {root.exists()}")
    assert left == [] and not root.exists() and rig.following == []
    assert (outside / "keep").read_text() == "keep" and stat.S_IMODE(os.stat(outside).st_mode) & 0o200
    assert tmp_clean._FD_CHMOD is (getattr(os, "O_PATH", None) is not None and os.path.isdir("/proc/self/fd"))


# --- S8D r1 fix item 2: the worker swaps a symlink in between the lstat and the chmod --------------------------------

_FAKE_O_PATH = 1 << 40          # never reaches a real os.open: the rig strips it


class _FdChmodRig:
    """Linux's no-follow chmod on macOS: `os.chmod` refuses follow_symlinks=False as a Linux CPython does; an
    O_PATH|O_NOFOLLOW open of a symlink gives the symlink's own fd (macOS O_SYMLINK), of a directory a dup of its
    parent's fd standing for it (fstat: a directory), and `/proc/self/fd/<n>` chmods what that fd was opened on.
    `following` records every chmod that would follow a symlink; `swap` runs once, after the first refused open of
    its name."""

    def __init__(self, monkeypatch, chmod, *, fd_chmod: bool = True, swap=None) -> None:
        from levain.chat_egress import tmp_clean
        self.following: list[str] = []
        self.handles: dict[int, tuple[int, str] | None] = {}
        real_open, real_chmod, linux = os.open, os.chmod, chmod
        pending = [swap] if swap is not None else []

        def fake_open(path, flags, mode=0o777, *, dir_fd=None):
            if flags & _FAKE_O_PATH:
                st = os.lstat(path, dir_fd=dir_fd)
                if stat.S_ISLNK(st.st_mode):
                    fd = real_open(path, os.O_RDONLY | os.O_SYMLINK, dir_fd=dir_fd)
                    self.handles[fd] = None
                else:
                    fd = os.dup(dir_fd)
                    self.handles[fd] = (dir_fd, path)
                return fd
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            except PermissionError:
                if pending and path == pending[0][0]:
                    pending.pop()[1]()
                raise

        def fake_chmod(path, mode, *, dir_fd=None, follow_symlinks=True):
            if isinstance(path, str) and path.startswith("/proc/self/fd/"):
                target = self.handles[int(path.rsplit("/", 1)[1])]
                assert target is not None, "the fd chmod reached a symlink's fd"
                return real_chmod(target[1], mode, dir_fd=target[0], follow_symlinks=False)
            if follow_symlinks:
                self.following.append(str(path))
            return linux(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "open", fake_open)
        monkeypatch.setattr(os, "chmod", fake_chmod)
        monkeypatch.setattr(tmp_clean, "_CHMOD_NOFOLLOW", False, raising=False)
        monkeypatch.setattr(tmp_clean, "_O_PATH", _FAKE_O_PATH if fd_chmod else None, raising=False)
        monkeypatch.setattr(tmp_clean, "_FD_CHMOD", fd_chmod, raising=False)


def _swap_rig(tmp_path, root, monkeypatch, *, fd_chmod: bool):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data").write_text("service data")
    os.chmod(victim, 0o500)
    real_chmod = os.chmod

    def swap() -> None:                       # the worker's move: the mode-000 dir out, a symlink to the victim in
        real_chmod(root / "zero", 0o700)      # a directory's rename rewrites its "..": the worker unlocks it first
        os.rename(root / "zero", tmp_path / "zero-moved")
        os.symlink(victim, root / "zero")
    return victim, _FdChmodRig(monkeypatch, _linux_chmod("valueerror"), fd_chmod=fd_chmod, swap=("zero", swap))


@pytest.mark.skipif(not hasattr(os, "O_SYMLINK"), reason="the rig stands O_PATH in with macOS O_SYMLINK")
@pytest.mark.parametrize("fd_chmod", [True, False], ids=["fd-chmod", "no-fd-chmod"])
def test_a_symlink_swapped_in_before_the_chmod_never_has_its_target_chmodded(fd_chmod, tmp_path, monkeypatch) -> None:
    root, outside = _hostile(tmp_path)
    victim, rig = _swap_rig(tmp_path, root, monkeypatch, fd_chmod=fd_chmod)
    try:
        left = remove_tree(root)
    finally:
        monkeypatch.undo()
        _restore(root)
        for p in (tmp_path / "zero-moved", tmp_path / "zero-moved" / "deep"):
            if p.is_dir():
                os.chmod(p, 0o700)
    mode = stat.S_IMODE(os.lstat(victim).st_mode)
    print(f"RUN S8D fix 2 swap ({'fd chmod' if fd_chmod else 'no fd chmod'}): leftovers {left}; victim {mode:o}; "
          f"following chmods {rig.following}")
    assert mode == 0o500 and rig.following == [] and (victim / "data").read_text() == "service data"
    why = "NotADirectoryError" if fd_chmod else "NoSafeChmod"
    assert f"{root}/zero: {why}" in left and left[-1] == f"{root}: OSError"
    assert (root / "zero").is_symlink()                               # left as it is, and named


@pytest.mark.skipif(not hasattr(os, "O_SYMLINK"), reason="the rig stands O_PATH in with macOS O_SYMLINK")
def test_with_no_fd_chmod_an_unreadable_dir_is_left_and_named_never_chmodded(tmp_path, monkeypatch) -> None:
    root, outside = _hostile(tmp_path)
    rig = _FdChmodRig(monkeypatch, _linux_chmod("valueerror"), fd_chmod=False)
    try:
        left = remove_tree(root)
    finally:
        monkeypatch.undo()
        _restore(root)
    assert rig.following == []
    assert left == [f"{root}/x-only: NoSafeChmod", f"{root}/zero: NoSafeChmod", f"{root}: OSError"]


@pytest.mark.skipif(not getattr(__import__("levain.chat_egress.tmp_clean", fromlist=["x"]), "_FD_CHMOD", False),
                    reason="real O_PATH + /proc/self/fd (Linux) only")
def test_real_linux_fd_chmod_removes_the_tree_and_refuses_a_swapped_symlink(tmp_path, monkeypatch) -> None:
    root, outside = _hostile(tmp_path)
    assert remove_tree(root) == [] and not root.exists()
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o500)
    root.mkdir()
    (root / "zero").mkdir(mode=0o000)
    real_open = os.open

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        try:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        except PermissionError:
            if path == "zero" and not (root / "zero").is_symlink():
                os.chmod(root / "zero", 0o700)
                os.rename(root / "zero", tmp_path / "zero-moved")
                os.symlink(victim, root / "zero")
            raise
    monkeypatch.setattr(os, "open", racing_open)
    left = remove_tree(root)
    monkeypatch.undo()
    os.chmod(tmp_path / "zero-moved", 0o700)
    assert stat.S_IMODE(os.stat(victim).st_mode) == 0o500
    assert f"{root}/zero: NotADirectoryError" in left
