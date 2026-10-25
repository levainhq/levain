"""CH-D2 S8 (second slice): row C8's hostile-tree red for `chat_tmp`'s launch-end removal (`tmp_clean.remove_tree`).

A worker-built tree under `chat_tmp` with a mode-000 dir, a non-writable dir and a symlink out of the tree ->
the removal removes the tree, never follows the symlink, and a remainder is a logged leftover, never a crash.
"""

from __future__ import annotations

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
    assert remove_tree(tmp_path / "chat_tmp") == [f"{tmp_path / 'chat_tmp'}: NotADirectoryError"]
    assert (outside / "keep").exists()
    assert remove_tree(tmp_path / "absent") == []
