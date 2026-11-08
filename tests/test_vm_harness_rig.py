"""The 1105-17 VM rig fixes (11-07 rigfix): `seed` writes the slot's identity seed, and the rig ships the CANONICAL fake
vendor source (tests/vm_fakes/fire_fakeclaude.c), never a copy of its own. No VM, no root."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import end as E
from scripts.vm_harness import rig_seed, rig_stage

REPO = Path(__file__).resolve().parent.parent
CANON_FILE = REPO / "tests" / "vm_fakes" / "fire_fakeclaude.c"
PATCH = REPO / "scripts" / "vm_harness" / "rig_1105_17.patch"
RIG = REPO.parent / "levain" / "project_memory" / "1105-17" / "vmend"     # the untracked rig (absent on a bare clone)


def _store(tmp_path, tag="e3"):
    k = tmp_path / "slots" / tag / "keep"
    k.mkdir(parents=True)
    return tmp_path


def test_seed_writes_the_product_shape(tmp_path):
    p = rig_seed.write_identity_seed(_store(tmp_path), "e3", chown_root=False)
    assert p == tmp_path / "slots/e3/keep" / E.IDENTITY_SEED_NAME
    assert (p.stat().st_mode & 0o777) == 0o600
    rec = E.check_identity_seed(canon.parse(p.read_bytes()))       # the product's own whole-record check
    assert rec["vendor"] == "claude" and rec["account"]["emailAddress"] == "fake@example.invalid"


def test_seed_account_matches_the_rigs_baseline_digest():
    # g_vm's baseline digest is made from these two values; a drift would trip the product's identity compare
    src = (Path(rig_seed.__file__)).read_text()
    assert "123e4567-e89b-12d3-a456-426614174000" in src and "fake@example.invalid" in src


def test_seed_refuses_to_overwrite(tmp_path):
    rig_seed.write_identity_seed(_store(tmp_path), "e3", chown_root=False)
    with pytest.raises(FileExistsError):
        rig_seed.write_identity_seed(tmp_path, "e3", chown_root=False)


def _fake_rig(tmp_path, stale=b"/* an OLD fake */\n"):
    rig = tmp_path / "rig"
    rig.mkdir()
    (rig / "g_vm.py").write_text("x = 1\n")
    (rig / "fire_fakeclaude.c").write_bytes(stale)
    (rig / "__pycache__").mkdir()
    return rig


def test_stage_never_ships_a_stale_fakeclaude(tmp_path):
    canon_bytes = CANON_FILE.read_bytes()
    out = rig_stage.stage_rig_files(_fake_rig(tmp_path), tmp_path / "up", canon_bytes)
    assert (tmp_path / "up" / "fire_fakeclaude.c").read_bytes() == canon_bytes
    assert {p.name for p in out} == {"g_vm.py", "fire_fakeclaude.c", "rig_seed.py"}


def test_check_staged_fails_on_a_differing_or_missing_source(tmp_path):
    canon_bytes = CANON_FILE.read_bytes()
    up = tmp_path / "up"
    rig_stage.stage_rig_files(_fake_rig(tmp_path), up, canon_bytes)
    (up / "fire_fakeclaude.c").write_bytes(b"old\n")
    with pytest.raises(RuntimeError, match="differs"):
        rig_stage.check_staged(up, canon_bytes)
    (up / "fire_fakeclaude.c").unlink()
    with pytest.raises(RuntimeError, match="no fire_fakeclaude.c"):
        rig_stage.check_staged(up, canon_bytes)
    (up / "fire_fakeclaude.c").write_bytes(canon_bytes)
    (up / "rig_seed.py").unlink()
    with pytest.raises(RuntimeError, match="rig_seed"):
        rig_stage.check_staged(up, canon_bytes)


def test_canonical_is_read_from_git_and_equals_the_tracked_file():
    assert rig_stage.canonical_fakeclaude(REPO, "HEAD") == subprocess.run(
        ["git", "-C", str(REPO), "show", "HEAD:tests/vm_fakes/fire_fakeclaude.c"], capture_output=True, check=True).stdout
    with pytest.raises(RuntimeError):
        rig_stage.canonical_fakeclaude(REPO, "no-such-rev-xyz")


@pytest.mark.skipif(not (RIG / "vmend.py").is_file(), reason="the untracked 1105-17 rig is not on this machine")
def test_the_rig_patch_wires_both_fixes(tmp_path):
    work = tmp_path / "vmend"
    work.mkdir()
    shutil.copy2(RIG / "vmend.py", work / "vmend.py")
    shutil.copytree(RIG / "rig", work / "rig", ignore=shutil.ignore_patterns("__pycache__"))
    if "rig_stage" in (work / "vmend.py").read_text():
        pass                                                         # the rig already carries the patch
    else:
        subprocess.run(["patch", "-p1", "-s", "-i", str(PATCH)], cwd=work, check=True, timeout=30)
    assert "rig_seed.write_identity_seed" in (work / "rig" / "g_vm.py").read_text()   # `seed` writes the seed
    vm = (work / "vmend.py").read_text()
    assert "rig_stage.stage_rig_files" in vm and "canonical_fakeclaude(LEVAIN, a.rev)" in vm
    # staging from the (possibly stale) rig dir yields the canonical source whatever the rig holds
    out = tmp_path / "up"
    rig_stage.stage_rig_files(work / "rig", out, CANON_FILE.read_bytes())
    assert (out / "fire_fakeclaude.c").read_bytes() == CANON_FILE.read_bytes()


def test_stage_into_a_reused_dir_drops_files_an_earlier_revision_left(tmp_path):
    # revert: the clearing loop at the top of rig_stage.stage_rig_files (a helper removed from the rig would still ship)
    canon_bytes = CANON_FILE.read_bytes()
    up = tmp_path / "up"
    up.mkdir()
    (up / "removed_helper.sh").write_text("echo old\n")
    (up / "g_vm.py").write_text("x = 0   # an older revision\n")
    (up / "link_helper").symlink_to(up / "removed_helper.sh")
    out = rig_stage.stage_rig_files(_fake_rig(tmp_path), up, canon_bytes)
    assert {p.name for p in out} == {"g_vm.py", "fire_fakeclaude.c", "rig_seed.py"}
    assert not (up / "removed_helper.sh").exists() and not (up / "link_helper").is_symlink()
    assert (up / "g_vm.py").read_text() == "x = 1\n"
