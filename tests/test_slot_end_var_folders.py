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
import shutil
import time
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


# --- L3 r1 fixes (1105-17 endvf_fix1) --------------------------------------------------------------------------------


def _move_away(vf: Path) -> Path:
    """``vf`` renamed aside by the dir-fd rename form (which gets past the :func:`sunlnk` stand-in: test setup)."""
    gone = vf.with_name("gone")
    pfd = os.open(vf.parent, os.O_RDONLY)
    try:
        os.rename(vf.name, gone.name, src_dir_fd=pfd, dst_dir_fd=pfd)
    finally:
        os.close(pfd)
    return gone


def _vf_walks(monkeypatch, vf: Path) -> list[int]:
    """Counts the entries every walk rooted AT ``vf`` yields (the var-folders scan and the tree step's emptying)."""
    real, seen = end_mod.walk, [0]

    def counting(root, *a, **k):
        for e in real(root, *a, **k):
            if Path(root) == vf:
                seen[0] += 1
            yield e
    monkeypatch.setattr(end_mod, "walk", counting)
    return seen


@pytest.mark.parametrize("cap", ["inodes", "seconds"])
def test_a_scratch_tree_past_the_budget_refuses_without_scanning_it_all(world, monkeypatch, cap):
    """E1 (codex 3): the scan counts against the manifest's inode / time budget as it goes, so a huge scratch tree
    refuses by the budget's name with a bounded number of entries scanned, never the whole tree held first."""
    for i in range(300):
        (world.vf / "T" / f"f{i:03}").write_bytes(b"x")
    seen = _vf_walks(monkeypatch, world.vf)
    ctl = Control()
    if cap == "inodes":
        caps, kw, name, bound = end_mod.Caps(inodes=50), {}, "manifest-inode-cap", 51
    else:
        ticks = iter(range(1_000_000))
        caps, kw, name, bound = end_mod.Caps(seconds=5), {"clock": lambda: float(next(ticks))}, "manifest-time-cap", 10
    assert end_session(world.facts, world.seams(ctl, **kw), record_state=REVOKED, caps=caps) == HELD
    assert ctl.failed_step == "manifest_budget"
    assert any(name in n for n in world.notes), world.notes
    assert 0 < seen[0] <= bound, seen[0]
    assert world.facts.live.is_dir() and not world.facts.tree.exists()


def test_a_failing_copy_mkdir_leaks_no_fd_across_retries(world, tmp_path, monkeypatch):
    """E2 (codex 5): with an outside-linked file (``linkcopy`` opened) and the copy's mkdir failing, every retry
    closes what it opened."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x" * 10)
    os.link(outside, world.S / "repo" / "hl-out")
    real = os.mkdir

    def mkdir(p, *a, **k):
        if os.fsencode(p).endswith(b"/" + os.fsencode(end_mod.VFCOPY)):
            raise OSError(errno.ENAMETOOLONG, "File name too long", os.fsdecode(p))
        return real(p, *a, **k)
    monkeypatch.setattr(os, "mkdir", mkdir)

    def nfds() -> int:
        return len(os.listdir("/dev/fd"))
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest"
    before = nfds()
    for i in range(6):
        assert end_session(world.facts, world.seams(ctl), record_state=HELD, failed_step="manifest",
                           held_attempts=i + 1) == HELD
        assert ctl.failed_step == "manifest"
    assert nfds() == before


def _once_before_classify(monkeypatch, act) -> None:
    real, done = end_mod.classify, [False]

    def classify(*a, **k):
        if not done[0]:
            done[0] = True
            act()
        return real(*a, **k)
    monkeypatch.setattr(end_mod, "classify", classify)


def _assert_changed_held(w: World, ctl: Control, step: str) -> None:
    assert ctl.failed_step == step
    assert any("var-folders-changed" in n for n in w.notes), w.notes
    assert w.facts.live.is_dir() and not w.facts.tree.exists()


def test_a_dir_made_after_an_absent_preflight_holds_at_manifest_and_the_redo_rederives(world, monkeypatch):
    """E3 (codex 2): absent at the preflight, its skeleton made before the manifest walk: the manifest names the
    dir and no copy exists. HELD ``manifest``, nothing moved; the redo scans, copies and retains it."""
    _move_away(world.vf)

    def remake():
        world.vf.mkdir()
        for n in SKELETON:
            (world.vf / n).mkdir()
    _once_before_classify(monkeypatch, remake)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    _assert_changed_held(world, ctl, "manifest")
    assert end_session(world.facts, world.seams(ctl), record_state=HELD, failed_step="manifest",
                       held_attempts=1) == RETAINED, world.notes
    assert sorted(os.listdir(world.facts.tree / ".levain-var-folders")) == sorted(SKELETON)
    assert world.manifest_file() == tree_manifest(world.facts.tree)


def test_a_skeleton_dir_made_between_the_scan_and_the_manifest_holds_at_manifest(world, monkeypatch):
    """E3: a skeleton dir the scan did not see but the manifest walk did is in the manifest and not the copy."""
    os.rmdir(world.vf / "0")
    _once_before_classify(monkeypatch, lambda: (world.vf / "0").mkdir())
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    _assert_changed_held(world, ctl, "manifest")


def test_a_scanned_entry_gone_before_the_manifest_holds_at_manifest(world, monkeypatch):
    """E3, the other direction: a scanned dir gone before the manifest walk is in the copy and not the manifest."""
    def drop():
        os.unlink(world.vf / "T" / "sub" / "deep.txt")
        os.rmdir(world.vf / "T" / "sub")
    _once_before_classify(monkeypatch, drop)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == HELD
    _assert_changed_held(world, ctl, "manifest")


def test_the_tree_step_holds_when_the_manifest_names_a_copy_that_is_gone(world):
    """E3 at the tree step: the manifest names the dir, the copy is missing. HELD ``tree`` before the first rename,
    and no original is unlinked (without the check, they were emptied with no copy kept)."""
    def fault(p):
        if p == "retain:before:tree":
            shutil.rmtree(world.facts.retained / end_mod.VFCOPY)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED) == HELD
    _assert_changed_held(world, ctl, "tree")
    assert (world.vf / "C" / "cachefile").read_bytes() == PLANTED


def test_the_tree_step_holds_when_a_copy_exists_the_manifest_does_not_name(world):
    """E3 at the tree step, the other direction: no var-folders dir at the manifest, a copy present at the tree."""
    _move_away(world.vf)

    def fault(p):
        if p == "retain:before:tree":
            (world.facts.retained / end_mod.VFCOPY).mkdir()
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl, fault=fault), record_state=REVOKED) == HELD
    _assert_changed_held(world, ctl, "tree")


@pytest.mark.parametrize("call", ["_scan_var_folders", "_empty_var_folders", "prove_var_folders_skeleton"])
def test_a_dir_gone_after_the_preflight_is_none_not_held(world, monkeypatch, call):
    """E4 (complement 2): the dir removed between a preflight and the walk / open is "none", never a spurious HELD:
    at the tree step (empty, prove: the copy is retained) and at the manifest step's scan (no dir, so no copy)."""
    real = getattr(end_mod, call)

    def gone(*a, **k):
        shutil.rmtree(world.vf)
        return real(*a, **k)
    monkeypatch.setattr(end_mod, call, gone)
    ctl = Control()
    assert end_session(world.facts, world.seams(ctl), record_state=REVOKED) == RETAINED, world.notes
    dest = world.facts.tree / ".levain-var-folders"
    if call == "_scan_var_folders":
        assert not dest.exists() and world.manifest_file() == tree_manifest(world.facts.tree)
    else:
        assert (dest / "C" / "cachefile").read_bytes() == PLANTED


def _seal_inputs(w: World):
    """The manifest step's inputs to :func:`_seal_vfcopy`, built for a direct call: the scan and a mirrored copy."""
    w.facts.retained.mkdir(exist_ok=True)
    scan = end_mod._scan_var_folders(w.vf, 10, end_mod._Budget(end_mod.Caps(), time.monotonic))
    vcopy = os.fsencode(w.facts.retained / end_mod.VFCOPY)
    os.mkdir(vcopy, 0o700)
    for rel, st in scan:
        if os.path.isdir(w.vf / os.fsdecode(rel)):
            os.mkdir(vcopy + b"/" + rel, 0o700)
    files = {(st.st_dev, st.st_ino) for rel, st in scan if not os.path.isdir(w.vf / os.fsdecode(rel))}
    return vcopy, os.lstat(w.vf), scan, files


def test_the_key_list_is_capped_by_the_manifest_byte_cap(world):
    """E5 (complement 3): ``vfkeys.jsonl`` refuses ``manifest-byte-cap``, and the meta cap does not bound it."""
    vcopy, vroot, scan, files = _seal_inputs(world)
    end_mod._seal_vfcopy(world.facts, vcopy, vroot, scan, files, end_mod.Caps(meta_bytes=10))
    with pytest.raises(end_mod.Refused) as e:
        end_mod._seal_vfcopy(world.facts, vcopy, vroot, scan, files, end_mod.Caps(manifest_bytes=10))
    assert e.value.name == "manifest-byte-cap"


def test_the_copy_dirs_get_their_modes_deepest_first_and_the_root_last(world, monkeypatch):
    """E6b (deepseek 3): the order the docstring states; the root used to go first."""
    vcopy, vroot, scan, files = _seal_inputs(world)
    order: list[int] = []
    real = os.fchmod

    def fchmod(fd, mode):
        order.append(os.fstat(fd).st_ino)
        return real(fd, mode)
    monkeypatch.setattr(os, "fchmod", fchmod)
    end_mod._seal_vfcopy(world.facts, vcopy, vroot, scan, files, end_mod.Caps())
    root = Path(os.fsdecode(vcopy))
    dirs = {Path(d): os.lstat(d).st_ino for d, _, _ in os.walk(root)}
    assert sorted(order) == sorted(dirs.values()) and order[-1] == dirs[root]
    for d, ino in dirs.items():
        if d != root:
            assert order.index(ino) < order.index(dirs[d.parent]), d
