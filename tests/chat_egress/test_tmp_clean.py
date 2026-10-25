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


@pytest.mark.parametrize("how", ["valueerror", "errno"])
def test_the_hostile_tree_is_removed_where_chmod_cannot_refuse_a_symlink(how, tmp_path, monkeypatch) -> None:
    from levain.chat_egress import tmp_clean
    root, outside = _hostile(tmp_path)
    monkeypatch.setattr(os, "chmod", _linux_chmod(how))
    monkeypatch.setattr(tmp_clean, "_CHMOD_NOFOLLOW", False, raising=False)     # os.supports_follow_symlinks there
    try:
        left = remove_tree(root)
    finally:
        monkeypatch.undo()
        for p in (root / "zero", root / "zero" / "deep", root / "ro", root / "r-only", root / "x-only"):  # parent first
            if p.is_dir() and not p.is_symlink():
                os.chmod(p, 0o700)
    print(f"RUN S8D item 6 simulated Linux chmod ({how}): leftovers {left}; root exists {root.exists()}")
    assert left == [] and not root.exists()
    assert (outside / "keep").read_text() == "keep" and stat.S_IMODE(os.stat(outside).st_mode) & 0o200
