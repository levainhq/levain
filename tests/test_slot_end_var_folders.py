"""1105-10 endvf (DESIGN_end_var_folders, rec (e)+(f)+(g), np-a6c66697): END and the slot uid's macOS var-folders dir.

On macOS 15.7.7 with SIP on, dirhelper's per-user dir ``/private/var/folders/<xx>/<hash>`` and its ``0`` ``C`` ``T`` ``X``
skeleton carry ``sunlnk`` + ``com.apple.rootless`` from creation; root could neither clear the flag nor rename, rmdir or
``rm -rf`` the skeleton, but a plain file a session wrote inside it WAS unlinked (RETURN_vmsunlnk, 060/061, measured
2026-11-05). END used to rename the dir into the retained tree, so every cloned session ended HELD at ``tree``. END now
copies what it finds there into ``tree/.levain-var-folders/`` (staged by the manifest step, the bytes it hashed), empties
the dir IN PLACE, and proves only the empty skeleton is left; its checks run before the first rename.

pytest cannot set ``SF_NOUNLINK``, so :func:`sunlnk` stands in for it: ``rename`` of the dir refuses EPERM, as measured.
Same world as test_slot_s2c_end (real files, the real Control.cas, fake freeze / kill / proof)."""
from __future__ import annotations

import base64
import errno
import hashlib
import os
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import end as end_mod
from levain.slot.end import HELD, RETAINED, REVOKED, end_session, tree_manifest
from tests.test_slot_s2c_end import Control, World, _unflag, snapshot

SKELETON = {"0", "C", "T", "X"}
PLANTED = b"a daemon wrote this for the uid\n"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sunlnk(monkeypatch, w: World) -> None:
    """The measured refusal: renaming the var-folders dir (or one of its skeleton dirs) is EPERM for root."""
    pinned = {os.fsencode(w.vf)} | {os.fsencode(w.vf / n) for n in SKELETON}
    real = os.rename

    def rename(src, dst, *a, **k):
        if "src_dir_fd" not in k and os.fsencode(src) in pinned:
            raise PermissionError(errno.EPERM, "Operation not permitted", os.fsdecode(src))
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", rename)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    for n in ("0", "X"):
        (w.vf / n).mkdir()
    (w.vf / "C" / "cachefile").write_bytes(PLANTED)
    (w.vf / "T" / "sub").mkdir()
    (w.vf / "T" / "sub" / "deep.txt").write_bytes(b"deep\n")
    (w.vf / "stray.txt").write_bytes(b"in the hash dir itself\n")
    sunlnk(monkeypatch, w)
    yield w
    _unflag(tmp_path)


def _manifest(w: World) -> dict:
    return {base64.b64decode(r["path_b64"]).decode(): r for r in w.manifest_file()}


def _skeleton_only(vf: Path) -> bool:
    return set(os.listdir(vf)) <= SKELETON and all(not os.listdir(vf / n) for n in os.listdir(vf))


def test_planted_file_is_copied_hashed_and_the_dir_emptied_in_place(world):
    pre, pre_manifest = world.pre(), world.pre_manifest()
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED, world.notes
    f = world.facts
    dest = f.tree / ".levain-var-folders"
    assert (dest / "C" / "cachefile").read_bytes() == PLANTED
    assert _manifest(world)[".levain-var-folders/C/cachefile"]["digest"] == _sha(PLANTED)
    assert (dest / "T" / "sub" / "deep.txt").read_bytes() == b"deep\n"
    assert (dest / "stray.txt").exists()
    # the dir itself stays, emptied: only the skeleton, each empty
    assert world.vf.is_dir() and sorted(os.listdir(world.vf)) == ["0", "C", "T", "X"]
    assert _skeleton_only(world.vf)
    # the retained tree is the pre-END tree, and hashes as the manifest says
    assert snapshot(f.tree) == pre
    assert world.manifest_file() == pre_manifest == tree_manifest(f.tree)
    assert sorted(os.listdir(f.retained)) == ["manifest.jsonl", "meta.jsonl", "object.json", "retained_at", "tree"]


def test_an_entry_that_cannot_be_unlinked_ends_held_naming_it_with_its_copy_kept(world, monkeypatch):
    real = os.unlink

    def unlink(p, *a, **k):
        if os.fsencode(p) == b"cachefile" and "dir_fd" in k:
            raise PermissionError(errno.EPERM, "Operation not permitted", os.fsdecode(p))
        return real(p, *a, **k)
    monkeypatch.setattr(os, "unlink", unlink)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"
    assert any("var-folders-residue" in n and "C/cachefile" in n for n in world.notes), world.notes
    assert (world.vf / "C" / "cachefile").read_bytes() == PLANTED          # nothing lost ...
    assert (world.facts.tree / ".levain-var-folders" / "C" / "cachefile").read_bytes() == PLANTED   # ... and kept


def test_a_file_written_after_the_manifest_is_residue_never_unlinked(world):
    def fault(p):
        if p == "retain:before:tree":
            (world.vf / "T" / "late.txt").write_bytes(b"written after the hash\n")
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"
    assert any("var-folders-residue" in n and "T/late.txt" in n for n in world.notes), world.notes
    assert (world.vf / "T" / "late.txt").read_bytes() == b"written after the hash\n"


def test_a_non_empty_skeleton_name_that_is_not_a_dir_is_residue(world):
    """Something outside the skeleton (here a dir the next session would see) that cannot be removed: HELD, named."""
    def fault(p):
        if p == "retain:before:tree":
            (world.vf / "Q").mkdir()
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"
    assert any("var-folders-residue" in n and "Q" in n for n in world.notes), world.notes


def _plant_symlink(w: World, tmp: Path) -> str:
    os.symlink("/etc/passwd", w.vf / "T" / "ln")
    return "T/ln"


def _plant_hardlink(w: World, tmp: Path) -> str:
    (tmp / "outside.txt").write_bytes(b"outside\n")
    os.link(tmp / "outside.txt", w.vf / "C" / "hl")
    return "C/hl"


def _plant_fifo(w: World, tmp: Path) -> str:
    os.mkfifo(w.vf / "0" / "fifo")
    return "0/fifo"


@pytest.mark.parametrize("plant", [_plant_symlink, _plant_hardlink, _plant_fifo])
def test_a_special_entry_refuses_before_anything_moves_and_is_never_followed(world, tmp_path, plant):
    rel = plant(world, tmp_path)
    pre_live = snapshot(world.S)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest"
    assert any("var-folders-special" in n and rel in n for n in world.notes), world.notes
    assert world.facts.live.is_dir() and not world.facts.tree.exists()
    pre_live.pop("config/claude/.credentials.json", None)
    assert snapshot(world.S) == pre_live
    assert os.path.lexists(world.vf / rel)
    assert not (world.facts.retained / end_mod.VFCOPY).exists()


def test_the_var_folders_checks_run_before_the_first_rename(world):
    """A var-folders dir that is no longer a real dir at the tree step: refused with ``live/<s>`` unmoved (the old
    step renamed ``live`` first and failed after, leaving a half-moved tree: RETURN_vmsunlnk §2)."""
    ctl = Control()
    vf = world.vf

    def swap(p):
        if p == "retain:before:tree":
            # the dir swapped for a symlink to it; the dir-fd rename form gets past the sunlnk stand-in (test setup)
            pfd = os.open(vf.parent, os.O_RDONLY)
            try:
                os.rename(vf.name, "gone", src_dir_fd=pfd, dst_dir_fd=pfd)
            finally:
                os.close(pfd)
            os.symlink(vf.with_name("gone"), vf)
    assert end_session(world.facts, world.seams(ctl, fault=swap), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"
    assert any("var-folders-not-dir" in n for n in world.notes), world.notes
    assert world.facts.live.is_dir() and not world.facts.tree.exists()


def test_a_var_folders_dir_no_session_uid_owns_refuses_before_anything_is_read(world):
    """The session is a uid that owns nothing here (as in test_slot_end_run_capture): the dir is not its own, so END
    neither copies nor empties it; HELD at ``manifest``, named, nothing moved."""
    from dataclasses import replace
    w = world
    w.facts = replace(w.facts, slot_uid=os.getuid() + 4242, slot_gid=os.getgid())
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest"
    assert any("var-folders-foreign" in n for n in w.notes), w.notes
    assert w.facts.live.is_dir() and not w.facts.tree.exists()
    assert (w.vf / "C" / "cachefile").read_bytes() == PLANTED


def test_the_retained_object_carries_no_staging(world):
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED
    obj = canon.parse((world.facts.retained / "object.json").read_bytes())
    assert obj["entries"] == len(world.manifest_file())
    assert not (world.facts.retained / end_mod.VFCOPY).exists()
    assert not (world.facts.retained / end_mod.VFKEYS).exists()
