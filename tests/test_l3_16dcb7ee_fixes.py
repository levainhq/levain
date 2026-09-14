"""Regression tests for L3 `16dcb7eebc6d3775` and Diogenes 2026-09-14, each built from a
failure reproduced against `849c259` before it was fixed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import levain.install as inst
from levain.doctor import _check_mcp_command
from levain.install import (
    InitError,
    _copy_activation_tree,
    _is_bytecode_residue,
    _merge_codex_config,
    activation_receipt_path,
)


def _layer(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "shape",
    ["refused", "sibling_then_swap_fails", "symlink_swap_fails", "symlink_rename_fails"],
)
def test_a_swap_that_does_not_go_through_leaves_the_live_receipt(tmp_path: Path, monkeypatch, shape):
    """complement HIGH + glm MED ×2, and round 2's complement + glm HIGH (`symlink_rename_fails`):
    each shape left the previous tree intact and its receipt deleted. MUTATION: drop the receipt
    restore in `_copy_activation_tree`'s rollback, or assign `moved_to` before the rename."""
    old = _layer(tmp_path / "old", {"posture.md": "OLD\n"})
    new = _layer(tmp_path / "new", {"posture.md": "NEW\n"})
    install = tmp_path / "install"
    dst = install / "activation"
    if shape.startswith("symlink"):
        install.mkdir()
        _copy_activation_tree([old], install / "real", base_activation=old, emit=lambda s: None)
        os.symlink("real", dst)
    else:
        _copy_activation_tree([old], dst, base_activation=old, emit=lambda s: None)
    receipt = activation_receipt_path(install)
    before = receipt.read_text(encoding="utf-8")

    real_replace = os.replace
    real_mkdir = Path.mkdir

    def _replace(src, d):
        s, t = Path(src), Path(d)
        if shape == "refused" and s == dst:
            raise OSError(28, "No space left on device")
        if shape == "symlink_rename_fails" and s == dst:
            raise PermissionError(13, "Permission denied")
        if shape == "sibling_then_swap_fails" and s == dst and "backups" in str(t):
            raise OSError(18, "cross-device")
        if shape != "refused" and s.name.startswith(".levain-activation-new-"):
            raise OSError(5, "EIO")
        return real_replace(src, d)

    def _mkdir(self, *a, **k):
        if shape == "refused" and "backups" in str(self):
            raise PermissionError(13, "denied")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(inst.os, "replace", _replace)
    monkeypatch.setattr(Path, "mkdir", _mkdir)
    expected = InitError if shape == "refused" else OSError
    with pytest.raises(expected):
        _copy_activation_tree([new], dst, base_activation=new, emit=lambda s: None)
    monkeypatch.undo()

    assert (dst / "posture.md").read_text(encoding="utf-8") == "OLD\n"
    assert receipt.read_text(encoding="utf-8") == before


def test_doctor_does_not_treat_another_venvs_python_as_this_one(tmp_path: Path):
    """codex HIGH: two venvs' `bin/python` resolve to one base binary, so a realpath match
    ran the other venv (and its `.pth` files). MUTATION: compare realpath alone."""
    other = tmp_path / "other-venv" / "bin" / "python"
    other.parent.mkdir(parents=True)
    other.symlink_to(os.path.realpath(sys.executable))
    r = _check_mcp_command("m", str(other), ["-P", "-m", "anneal_memory", "--db", "x", "serve"],
                           tmp_path)
    assert not r.ok and not r.upgrade_pending and "does not run it" in r.detail


def test_only_importlibs_own_cache_name_counts_as_bytecode_residue(tmp_path: Path):
    """codex HIGH: `h.notes.pyc` beside `h.py` was skipped by the pristine proof, so rotation
    deleted it. MUTATION: go back to `<stem>.*.pyc`."""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "h.py").write_text("x", encoding="utf-8")
    assert not _is_bytecode_residue(Path("hooks/__pycache__/h.notes.pyc"), tmp_path)
    # Round 2 (codex HIGH, reproduced): a tag that is not a cache tag, and a dotted source stem.
    assert not _is_bytecode_residue(Path("hooks/__pycache__/h.notes-1.pyc"), tmp_path)
    (tmp_path / "hooks" / "foo.bar.py").write_text("x", encoding="utf-8")
    assert _is_bytecode_residue(Path("hooks/__pycache__/foo.bar.cpython-313.pyc"), tmp_path)
    assert _is_bytecode_residue(Path("hooks/__pycache__/h.cpython-312.pyc"), tmp_path)
    assert _is_bytecode_residue(Path("hooks/__pycache__/h.cpython-313.opt-1.pyc"), tmp_path)


def test_a_version_flag_ahead_of_db_is_not_the_registration_init_writes(tmp_path: Path):
    """codex HIGH: this argv printed a version and exited, and doctor called it healthy.
    MUTATION: accept any argv starting `-P -m anneal_memory` and ending `serve`."""
    r = _check_mcp_command(
        "m", sys.executable, ["-P", "-m", "anneal_memory", "--version", "--db", "x", "serve"],
        tmp_path)
    assert not r.ok and not r.upgrade_pending


def test_a_command_merely_prefixed_anneal_memory_is_broken_not_pending(tmp_path: Path):
    """codex HIGH: `anneal-memory-malware` exited 6, as a routine upgrade step. MUTATION: go
    back to a basename prefix match."""
    fake = tmp_path / "anneal-memory-malware"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _check_mcp_command("m", str(fake), ["--db", "x", "serve"], tmp_path)
    assert not r.ok and not r.upgrade_pending


def test_a_customised_launcher_on_the_same_store_is_still_backed_up(tmp_path: Path):
    """codex HIGH, round 2, reproduced: once levain's own keys were excluded from the compare,
    an operator wrapper plus `--trace` on the same store was replaced with no backup and no
    warning. MUTATION: drop `_is_levain_launcher` from the `relaunched` test."""
    install = tmp_path / "inst"
    store = install / ".levain" / "memory.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.anneal_memory]\n"
        'command = "/opt/wrap/anneal-with-trace"\n'
        f'args = ["--trace", "--db", "{store}", "serve"]\n',
        encoding="utf-8",
    )
    template = Path(inst.__file__).parent / "templates" / "adapters" / "codex" / "mcp.template.toml"
    fragment = (template.read_text(encoding="utf-8")
                .replace("{{PYTHON}}", sys.executable).replace("{{INSTALL_DIR}}", str(install)))
    said: list[str] = []
    _merge_codex_config(cfg, fragment, emit=said.append)
    assert [m for m in said if "customisation is gone" in m], said
    assert len(list(tmp_path.glob("config.toml.bak*"))) == 1


def test_a_pristine_pre_751_codex_block_upgrades_without_a_lost_customisation_alarm(tmp_path: Path):
    """Diogenes MEDIUM, his signature reproduced: 7 lines ending "that customisation is gone"
    and a backup, for a block holding only what levain wrote. MUTATION: compare whole dicts."""
    install = tmp_path / "inst"
    store = install / ".levain" / "memory.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.anneal_memory]\n"
        'command = "/usr/local/bin/anneal-memory"\n'
        f'args = ["--db", "{store}", "serve"]\n',
        encoding="utf-8",
    )
    template = Path(inst.__file__).parent / "templates" / "adapters" / "codex" / "mcp.template.toml"
    fragment = (template.read_text(encoding="utf-8")
                .replace("{{PYTHON}}", sys.executable).replace("{{INSTALL_DIR}}", str(install)))
    said: list[str] = []
    _merge_codex_config(cfg, fragment, emit=said.append)
    assert not [m for m in said if "customisation is gone" in m], said
    # Preserve always (spore-865): the benign relaunch is still backed up; only its notice is benign.
    assert len(list(tmp_path.glob("config.toml.bak*"))) == 1
    assert [m for m in said if "keeps its store" in m], said
    assert '"-P", "-m", "anneal_memory"' in cfg.read_text(encoding="utf-8")


def test_a_wrapper_command_with_levains_exact_args_is_still_a_customisation(tmp_path: Path):
    """Round 3, all three seats, reproduced: the current-shape branch never checked `command`,
    so this wrapper was overwritten with no backup and no warning. MUTATION: drop the
    `_PYTHON_NAME` check from `_is_levain_launcher`."""
    install = tmp_path / "inst"
    store = install / ".levain" / "memory.db"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[mcp_servers.anneal_memory]\n"
        'command = "/opt/company/audit-wrapper"\n'
        f'args = ["-P", "-m", "anneal_memory", "--db", "{store}", "serve"]\n',
        encoding="utf-8",
    )
    template = Path(inst.__file__).parent / "templates" / "adapters" / "codex" / "mcp.template.toml"
    fragment = (template.read_text(encoding="utf-8")
                .replace("{{PYTHON}}", sys.executable).replace("{{INSTALL_DIR}}", str(install)))
    said: list[str] = []
    _merge_codex_config(cfg, fragment, emit=said.append)
    assert [m for m in said if "customisation is gone" in m], said
    baks = list(tmp_path.glob("config.toml.bak*"))
    assert len(baks) == 1 and "audit-wrapper" in baks[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("symlinked", [False, True])
def test_an_interrupt_after_the_rename_puts_the_previous_tree_back(
    tmp_path: Path, monkeypatch, symlinked
):
    """Round 3, codex, reproduced: the rename landed, the interrupt arrived before `moved_to`
    was set, and `activation/` was left missing. MUTATION: drop the from-disk reconcile of
    `candidate` in the rollback."""
    old = _layer(tmp_path / "old", {"posture.md": "OLD\n"})
    new = _layer(tmp_path / "new", {"posture.md": "NEW\n"})
    install = tmp_path / "install"
    dst = install / "activation"
    if symlinked:
        install.mkdir()
        _copy_activation_tree([old], install / "real", base_activation=old, emit=lambda s: None)
        os.symlink("real", dst)
    else:
        _copy_activation_tree([old], dst, base_activation=old, emit=lambda s: None)
    receipt = activation_receipt_path(install)
    before = receipt.read_text(encoding="utf-8")

    real_replace = os.replace

    def _replace(src, d):
        result = real_replace(src, d)
        if Path(src) == dst:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(inst.os, "replace", _replace)
    with pytest.raises(KeyboardInterrupt):
        _copy_activation_tree([new], dst, base_activation=new, emit=lambda s: None)
    monkeypatch.undo()

    assert (dst / "posture.md").read_text(encoding="utf-8") == "OLD\n"
    assert receipt.read_text(encoding="utf-8") == before


def test_a_lock_written_before_751_names_the_path_anneal_not_an_upgrade():
    """⚖ Phill 2026-09-14, option 3. The line under test is the one the two-anneal
    scratch-HOME upgrade printed. MUTATION: drop the pre-751 branch in `compute_drift`."""
    from levain import manifest

    declared = manifest.CompatSet(levain="0.4.7", anneal="0.9.8", schema="partnership")
    installed = manifest.InstalledSet(levain="0.4.7.dev0", anneal="0.9.9", schema="partnership",
                                      migrate_acked=None, pending_count=0)
    measured = ("anneal-memory changed 0.9.10.dev0 -> 0.9.9 since this install was last "
                "composed (an out-of-band upgrade)")

    pre = manifest.compute_drift(
        declared, installed,
        manifest.CompatSet(levain="0.4.6", anneal="0.9.10.dev0", schema="partnership"),
    ).of("anneal-lock")
    assert pre is not None and pre.status == "drift"
    assert measured not in pre.detail
    assert "first on PATH" in pre.detail and "levain init --force" in (pre.hint or "")

    post = manifest.compute_drift(
        declared, installed,
        manifest.CompatSet(levain="0.4.7", anneal="0.9.10.dev0", schema="partnership"),
    ).of("anneal-lock")
    assert post is not None and post.detail == measured
