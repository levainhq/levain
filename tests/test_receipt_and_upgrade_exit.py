"""spore-900 (the activation install receipt) and spore-840 (doctor's distinct exit code
for a post-upgrade step still pending), both ruled by Phill 2026-09-13.

Each test names the mutation it exists to kill.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import levain.doctor as doctor
import levain.install as inst
from levain.doctor import (
    EXIT_BROKEN,
    EXIT_HEALTHY,
    EXIT_UPGRADE_PENDING,
    CheckResult,
    _check_mcp_command,
)
from levain.install import (
    _copy_activation_tree,
    activation_receipt_path,
    read_activation_receipt,
    read_activation_receipt_file,
)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _layer(root: Path) -> Path:
    for rel, body in {
        "posture.md": "P\n",
        "hooks/_levain_hook.py": 'X = 1\n_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"\n',
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


# ---------- spore-900: the receipt ----------


def test_the_receipt_records_INSTALLED_and_SOURCE_hashes_per_file(tmp_path: Path):
    """Both maps (Diogenes 2026-08-12 HIGH against spore-492): the installed hash answers
    "changed since install"; the source hash is what a package-moved question needs.

    MUTATION: hash the source into `installed` (or vice versa) -> the hook assertions fail,
    because substitution makes the two differ."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")

    files, status = read_activation_receipt(install)
    assert status == "ok" and files is not None
    assert sorted(files) == ["hooks/_levain_hook.py", "posture.md"]
    hook = files["hooks/_levain_hook.py"]
    assert hook["installed"] == _sha(dst / "hooks" / "_levain_hook.py")
    assert hook["source"] == _sha(base / "hooks" / "_levain_hook.py")
    assert hook["installed"] != hook["source"]
    assert files["posture.md"]["installed"] == files["posture.md"]["source"]


def test_the_receipt_is_NOT_written_for_a_swap_that_failed(tmp_path: Path, monkeypatch):
    """A receipt describes the tree in place. MUTATION: write it before the swap -> a
    receipt exists for a tree that was never installed."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    real_replace = inst.os.replace

    def flaky(src, dst_):
        if ".levain-activation-new-" in str(src):
            raise OSError(28, "No space left on device")
        return real_replace(src, dst_)

    monkeypatch.setattr(inst.os, "replace", flaky)
    with pytest.raises(OSError):
        _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/x")
    assert not activation_receipt_path(install).exists()


@pytest.mark.parametrize("damage", [
    '{"schema": 1, "files": {"posture.md": {"installed": "nothex", "source": "nothex"}}}',
    '{"schema": 99, "files": {}}',
    "not json at all",
    '{"schema": 1, "files": {}}',
])
def test_a_damaged_or_empty_receipt_is_UNKNOWN_never_pristine(tmp_path: Path, damage: str):
    """⛔ Absent / corrupt / empty is never OK (spore-492 HIGH; ruling B's "unproven").

    MUTATION: have the reader return a partial map, or `{}` as ok -> a pristine-looking
    hook is copied silently instead of named as unknown."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")
    activation_receipt_path(install).write_text(damage, encoding="utf-8")

    files, status = read_activation_receipt_file(activation_receipt_path(install))
    assert files is None and status in {"corrupt", "empty"}

    said: list[str] = []
    _copy_activation_tree([base], dst, base_activation=base,
                          anneal_path="/usr/local/bin/anneal-memory", emit=said.append)
    assert [m for m in said if "Previous activation/ kept whole" in m and "cannot tell" in m], said
    assert not [m for m in said if "Operator-edited" in m], said


def test_ONE_malformed_entry_voids_the_WHOLE_receipt(tmp_path: Path):
    """A half-written receipt must not yield a partial trust map. Found by the mutation
    pass: the single-entry damage cases above could not tell "skip the bad entry" from
    "void the receipt", because skipping the only entry also reads as empty.

    MUTATION: `continue` past a malformed entry -> status "ok" with the valid entry kept."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    _copy_activation_tree([base], install / "activation", base_activation=base,
                          anneal_path="/opt/a/anneal-memory")
    path = activation_receipt_path(install)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["files"]["hooks/_levain_hook.py"]["installed"] = "truncated"
    path.write_text(json.dumps(data), encoding="utf-8")

    files, status = read_activation_receipt_file(path)
    assert (files, status) == (None, "corrupt")


def test_a_FAILED_receipt_write_removes_the_previous_receipt(tmp_path: Path, monkeypatch):
    """⛔ A receipt that outlives its tree turns every freshly written file into an
    "Operator-edited" claim on the next run. MUTATION: drop the unlink -> the old receipt
    survives describing the previous tree."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")
    assert activation_receipt_path(install).exists()

    def boom(*_a, **_k):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(inst, "_write_activation_receipt", boom)
    said: list[str] = []
    _copy_activation_tree([base], dst, base_activation=base,
                          anneal_path="/usr/local/bin/anneal-memory", emit=said.append)
    assert not activation_receipt_path(install).exists()
    assert [m for m in said if "could not record the activation install receipt" in m], said


# ---------- spore-840: exit code precedence ----------


def _stub_doctor(monkeypatch, tmp_path: Path, results: list[CheckResult]) -> Path:
    install = tmp_path / "ent"
    install.mkdir()
    (install / "CLAUDE.md").write_text("# tag\n", encoding="utf-8")
    monkeypatch.setattr(inst, "effective_adapter", lambda _p: "claude-code")
    for name in ("_check_install_layout", "_check_hook_freshness", "_check_activation_scope",
                 "_check_seed_content", "_check_recorded_answers", "_check_runtime",
                 "_check_store", "_check_compat_set"):
        monkeypatch.setattr(doctor, name, lambda *_a, **_k: [])
    monkeypatch.setattr(doctor, "_check_claude_code", lambda _p: list(results))
    return install


_OK = CheckResult("a", True, "fine")
_PENDING = CheckResult("b", False, "old hooks running", "init --force", upgrade_pending=True)
_BROKEN = CheckResult("c", False, "store corrupt", "restore")


@pytest.mark.parametrize("results, expected", [
    ([_OK], EXIT_HEALTHY),
    ([_OK, _PENDING], EXIT_UPGRADE_PENDING),
    ([_PENDING, _BROKEN], EXIT_BROKEN),
    ([_BROKEN], EXIT_BROKEN),
])
def test_doctor_exit_precedence_is_broken_over_pending_over_healthy(
    monkeypatch, tmp_path: Path, capsys, results, expected
):
    """MUTATION: return EXIT_UPGRADE_PENDING whenever any failure is pending -> the mixed
    case exits 6 and a broken install reads as routine. MUTATION: drop the pending branch
    -> the pending case exits 1 as before spore-840."""
    install = _stub_doctor(monkeypatch, tmp_path, results)
    assert doctor.run_doctor(install) == expected
    out = capsys.readouterr().out
    if expected == EXIT_UPGRADE_PENDING:
        assert "still running" in out and "activation layer" in out, out


def test_a_failed_live_fire_is_broken_even_when_every_static_failure_is_pending(
    monkeypatch, tmp_path: Path
):
    install = _stub_doctor(monkeypatch, tmp_path, [_PENDING])
    import levain.verify as verify

    monkeypatch.setattr(verify, "run_verify_hooks", lambda _p: 1)
    assert doctor.run_doctor(install, invoke=True) == EXIT_BROKEN


def test_the_pending_code_does_not_collide_with_levain_run_codes():
    session = pytest.importorskip("levain.session")
    run_codes = {getattr(session, n) for n in dir(session) if n.startswith("EXIT_")}
    assert EXIT_UPGRADE_PENDING not in run_codes
    assert EXIT_BROKEN == 1 and EXIT_HEALTHY == 0


# ---------- spore-840: which checks are pending ----------


def test_mcp_command_OK_only_for_this_interpreter_in_module_form(tmp_path: Path):
    good = _check_mcp_command("m", sys.executable,
                              ["-P", "-m", "anneal_memory", "--db", "x", "serve"], tmp_path)
    assert good.ok


def test_a_pre_751_mcp_registration_is_PENDING_not_broken(tmp_path: Path):
    """The shape every install written before spore-751 carries: an anneal-memory script.

    MUTATION: drop `upgrade_pending=True` -> every existing install exits 1 on upgrade."""
    script = tmp_path / "anneal-memory"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    r = _check_mcp_command("m", str(script), ["--db", "x", "serve"], tmp_path)
    assert not r.ok and r.upgrade_pending
    # Same interpreter but without -P is also the pre-fix shape, not a pass.
    r2 = _check_mcp_command("m", sys.executable, ["-m", "anneal_memory", "serve"], tmp_path)
    assert not r2.ok and r2.upgrade_pending


def test_an_unresolvable_mcp_command_is_BROKEN(tmp_path: Path):
    """MUTATION: mark every non-matching command pending -> a server that cannot start
    exits 6, as routine."""
    r = _check_mcp_command("m", str(tmp_path / "gone" / "python"), ["-P", "-m", "anneal_memory"],
                           tmp_path)
    assert not r.ok and not r.upgrade_pending
    r2 = _check_mcp_command("m", None, [], tmp_path)
    assert not r2.ok and not r2.upgrade_pending
    # The pre-751 SCRIPT shape with a path that no longer exists: the probe never runs for
    # it, so only the resolvability gate stops it reading as a routine pending step.
    # Found by the mutation pass (M12 survived without this case).
    r3 = _check_mcp_command("m", str(tmp_path / "gone" / "anneal-memory"),
                            ["--db", "x", "serve"], tmp_path)
    assert not r3.ok and not r3.upgrade_pending


@pytest.mark.parametrize("lock_version, pending", [("0.0.1", True), ("999.0.0", False)])
def test_compat_levain_UPGRADE_is_pending_and_DOWNGRADE_is_broken(
    monkeypatch, tmp_path: Path, lock_version: str, pending: bool
):
    """MUTATION: drop the `_cmp(...) > 0` guard -> a downgrade exits 6."""
    from levain import __version__, manifest

    install = tmp_path / "ent"
    (install / ".levain").mkdir(parents=True)
    (install / ".levain" / "memory.db").write_bytes(b"")
    verdict = manifest.AxisVerdict("levain", "drift", "levain moved", "Run `levain update`.")
    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: "anneal-memory")
    monkeypatch.setattr(manifest, "declared_set", lambda: None)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *_a: SimpleNamespace(levain=__version__))
    monkeypatch.setattr(manifest, "read_lock_status",
                        lambda _p: (SimpleNamespace(levain=lock_version), "ok"))
    monkeypatch.setattr(manifest, "compute_drift",
                        lambda *_a: SimpleNamespace(verdicts=[verdict]))
    monkeypatch.setattr(manifest, "pip_floor_verdict",
                        lambda: manifest.AxisVerdict("pip-pin", "in_sync", "ok"))
    rows = [r for r in doctor._check_compat_set(install) if r.name == "compat: levain"]
    assert len(rows) == 1 and not rows[0].ok
    assert rows[0].upgrade_pending is pending


def test_carrier_freshness_stale_is_pending(tmp_path: Path):
    """MUTATION: drop `upgrade_pending=True` on the carrier FAIL -> exits 1 on upgrade."""
    from levain.packs import ON_DEMAND_SEED

    name = sorted(ON_DEMAND_SEED)[0]
    carrier = tmp_path / "CLAUDE.md"
    carrier.write_text(f"@seed/{name}\n", encoding="utf-8")
    r = doctor._check_carrier_freshness(tmp_path, carrier)[0]
    assert not r.ok and r.upgrade_pending


def test_hook_freshness_stale_is_pending_and_says_the_old_hooks_run(tmp_path: Path):
    """The ruling's other half: the FAIL text stays honest that the installed copies are
    what runs. MUTATION: drop `upgrade_pending=True`, or the "hooks that run" clause."""
    from levain.install import _base_activation_root, _templates_root

    install = tmp_path / "ent"
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)
    with _templates_root() as tr:
        for f in (_base_activation_root("claude-code", tr) / "hooks").glob("*.py"):
            (hooks / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (install / "CLAUDE.md").write_text("# tag\n", encoding="utf-8")
    f = hooks / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# stale\n", encoding="utf-8")

    r = doctor._check_hook_freshness(install)[0]
    assert not r.ok and r.upgrade_pending
    assert "the hooks that run are these installed copies" in r.detail


# ---------- L1/L2 round: fixes, each with its mutant ----------


def test_an_interrupt_AFTER_the_swap_never_leaves_the_OLD_receipt(tmp_path: Path, monkeypatch):
    """L1 + L2 HIGH, reproduced. MUTATION: skip the pre-swap invalidation -> the old
    receipt survives, and the next re-install calls install's own hook an operator edit."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")

    real_write = inst._write_activation_receipt

    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(inst, "_write_activation_receipt", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _copy_activation_tree([base], dst, base_activation=base,
                              anneal_path="/usr/local/bin/anneal-memory")
    monkeypatch.setattr(inst, "_write_activation_receipt", real_write)
    assert not activation_receipt_path(install).exists()

    said: list[str] = []
    _copy_activation_tree([base], dst, base_activation=base,
                          anneal_path="/opt/b/anneal-memory", emit=said.append)
    assert not [m for m in said if "Operator-edited" in m], said


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the unwritable-dir permission")
def test_an_unremovable_old_receipt_REFUSES_before_the_swap(tmp_path: Path):
    """L1 HIGH, reproduced with a real unwritable `.levain/`. MUTATION: swallow the unlink
    error -> the swap proceeds and the stale receipt survives."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")
    levain_dir = install / ".levain"
    os.chmod(levain_dir, 0o500)
    try:
        with pytest.raises(inst.InitError, match="previous activation install receipt"):
            _copy_activation_tree([base], dst, base_activation=base,
                                  anneal_path="/usr/local/bin/anneal-memory")
        hook = (dst / "hooks" / "_levain_hook.py").read_text(encoding="utf-8")
        assert '"/opt/a/anneal-memory"' in hook, "refused BEFORE the swap"
        # A no-change reinstall is not blocked by the same unwritable directory.
        _copy_activation_tree([base], dst, base_activation=base,
                              anneal_path="/opt/a/anneal-memory")
    finally:
        os.chmod(levain_dir, 0o700)


def test_a_file_the_operator_ADDED_is_named_under_an_intact_receipt(tmp_path: Path):
    """L1 MED. MUTATION: drop the `prior_status == "ok"` branch -> "cannot tell"."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/a/anneal-memory")
    (dst / "my_notes.md").write_text("mine\n", encoding="utf-8")
    said: list[str] = []
    _copy_activation_tree([base], dst, base_activation=base,
                          anneal_path="/opt/a/anneal-memory", emit=said.append)
    assert [m for m in said if "Operator-edited my_notes.md" in m], said


def _receipted_hooked_install(tmp_path: Path) -> Path:
    from levain.install import _base_activation_root, _templates_root, _write_activation_receipt

    install = tmp_path / "ent"
    hooks = install / "activation" / "hooks"
    hooks.mkdir(parents=True)
    with _templates_root() as tr:
        for f in (_base_activation_root("claude-code", tr) / "hooks").glob("*.py"):
            (hooks / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (install / "CLAUDE.md").write_text("# tag\n", encoding="utf-8")
    # The package "moves": the installed hook is an older text, and the receipt says init
    # wrote exactly that text.
    f = hooks / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "\n# older release\n", encoding="utf-8")
    _write_activation_receipt(install, {
        f"hooks/{p.name}": {"installed": _sha(p), "source": _sha(p)} for p in hooks.glob("*.py")
    })
    return install


def test_a_receipt_proven_OUTDATED_hook_is_pending(tmp_path: Path):
    r = doctor._check_hook_freshness(_receipted_hooked_install(tmp_path))[0]
    assert not r.ok and r.upgrade_pending
    assert "cannot tell" not in r.detail


def test_a_hook_EDITED_since_install_is_BROKEN_not_pending(tmp_path: Path):
    """L2 HIGH, reproduced: an appended payload exited 6. MUTATION: skip the receipt
    comparison -> the edited hook is classed as a pending upgrade step."""
    install = _receipted_hooked_install(tmp_path)
    f = install / "activation" / "hooks" / "_levain_hook.py"
    f.write_text(f.read_text(encoding="utf-8") + "import os\n", encoding="utf-8")
    r = doctor._check_hook_freshness(install)[0]
    assert not r.ok and not r.upgrade_pending
    assert "changed since `levain init` wrote them" in r.detail


@pytest.mark.parametrize("lock_version, pending", [("0.0.1", True), ("999.0.0", False)])
def test_anneal_lock_drift_is_pending_only_alongside_a_levain_UPGRADE(
    monkeypatch, tmp_path: Path, lock_version: str, pending: bool
):
    """L2 HIGH, reproduced: the routine upgrade also moves the anneal lock and still exited
    1. MUTATION: drop "anneal-lock" from the pending axes."""
    from levain import __version__, manifest

    install = tmp_path / "ent"
    (install / ".levain").mkdir(parents=True)
    (install / ".levain" / "memory.db").write_bytes(b"")
    verdicts = [manifest.AxisVerdict("levain", "drift", "moved", "update"),
                manifest.AxisVerdict("anneal-lock", "drift", "moved", "update")]
    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: "anneal-memory")
    monkeypatch.setattr(manifest, "declared_set", lambda: None)
    monkeypatch.setattr(manifest, "discover_installed_set",
                        lambda *_a: SimpleNamespace(levain=__version__))
    monkeypatch.setattr(manifest, "read_lock_status",
                        lambda _p: (SimpleNamespace(levain=lock_version), "ok"))
    monkeypatch.setattr(manifest, "compute_drift", lambda *_a: SimpleNamespace(verdicts=verdicts))
    monkeypatch.setattr(manifest, "pip_floor_verdict",
                        lambda: manifest.AxisVerdict("pip-pin", "in_sync", "ok"))
    rows = {r.name: r for r in doctor._check_compat_set(install)}
    assert rows["compat: anneal-lock"].upgrade_pending is pending


def test_a_module_form_command_that_CANNOT_IMPORT_anneal_is_BROKEN(tmp_path: Path):
    """L2 MED, reproduced with a system python lacking anneal. MUTATION: skip the probe
    -> a server that cannot start is reported as a pending step or OK."""
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho 'No module named anneal_memory' >&2\nexit 1\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    r = _check_mcp_command("m", str(fake), ["-P", "-m", "anneal_memory", "serve"], tmp_path)
    assert not r.ok and not r.upgrade_pending and "cannot start" in r.detail


def test_a_module_form_command_in_ANOTHER_environment_is_not_OK(tmp_path: Path):
    """L1 + L2 MED: compare the environment, not the path spelling. MUTATION: accept any
    probe success -> another venv's registration reads as this levain's."""
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\necho /some/other/venv\n", encoding="utf-8")
    fake.chmod(0o755)
    r = _check_mcp_command("m", str(fake), ["-P", "-m", "anneal_memory", "serve"], tmp_path)
    assert not r.ok and "/some/other/venv" in r.detail


# ---------- spore-861 ruling B: receipt-gated pruning ----------


def _trees(install: Path) -> list[Path]:
    root = install / ".levain" / "backups" / "activation"
    return sorted(p for p in root.iterdir() if p.name.startswith("tree-") and p.is_dir())


def _hook_text(tree: Path) -> str:
    return (tree / "hooks" / "_levain_hook.py").read_text(encoding="utf-8")


def test_pristine_trees_rotate_to_the_newest_three_WITH_their_receipts(tmp_path: Path):
    """MUTATION: drop the rotation -> five trees; drop the receipt unlink -> orphan receipts;
    drop the `tree-` filter -> the legacy dir is removed."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    legacy = install / ".levain" / "backups" / "activation" / "1700000000000000000"
    legacy.mkdir(parents=True)
    (legacy / "posture.md").write_text("AN EDIT ONLY THIS DIR HOLDS\n", encoding="utf-8")
    said: list[str] = []
    for n in range(6):
        _copy_activation_tree([base], dst, base_activation=base,
                              anneal_path=f"/opt/{n}/anneal", emit=said.append)

    # A bare-timestamp dir is not a tree in this scheme, so it is neither pruned nor
    # reported as a tree that "may hold edits" (MUTATION: drop the `tree-` filter).
    assert not [m for m in said if "may hold edits" in m], said
    trees = _trees(install)
    assert [f'"/opt/{n}/anneal"' in _hook_text(t) for n, t in zip((2, 3, 4), trees)] == [True] * 3
    assert len(trees) == 3
    receipts = sorted((install / ".levain" / "backups" / "activation").glob("tree-*.receipt.json"))
    assert [r.name for r in receipts] == [f"{t.name}.receipt.json" for t in trees]
    assert (legacy / "posture.md").read_text(encoding="utf-8") == "AN EDIT ONLY THIS DIR HOLDS\n"


def test_a_tree_holding_an_EDIT_is_never_pruned_and_is_named(tmp_path: Path):
    """⚖ Ruling B. MUTATION: prune by count regardless of proof -> the edited tree is gone."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/0/anneal")
    (dst / "posture.md").write_text("MY TUNED POSTURE\n", encoding="utf-8")
    said: list[str] = []
    for n in range(1, 6):
        _copy_activation_tree([base], dst, base_activation=base,
                              anneal_path=f"/opt/{n}/anneal", emit=said.append)
    kept = [t for t in _trees(install)
            if (t / "posture.md").read_text(encoding="utf-8") == "MY TUNED POSTURE\n"]
    assert len(kept) == 1
    assert len(_trees(install)) == 4       # the edited tree + the newest three pristine
    assert [m for m in said if "may hold edits" in m and str(kept[0]) in m], said


def test_a_RECEIPTLESS_tree_is_never_pruned(tmp_path: Path):
    """Every tree from an install made before the receipt existed. MUTATION: treat an
    absent sibling receipt as pristine -> it is pruned."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    (dst / "hooks").mkdir(parents=True)
    (dst / "posture.md").write_text("P\n", encoding="utf-8")
    (dst / "hooks" / "_levain_hook.py").write_text('_INSTALL_ANNEAL_BIN = "/old"\n',
                                                    encoding="utf-8")
    for n in range(5):
        _copy_activation_tree([base], dst, base_activation=base, anneal_path=f"/opt/{n}/anneal")
    oldest = _trees(install)[0]
    assert '"/old"' in _hook_text(oldest)
    assert not (oldest.parent / f"{oldest.name}.receipt.json").exists()
    assert len(_trees(install)) == 4


def test_a_tree_whose_sibling_receipt_is_CORRUPT_is_never_pruned(tmp_path: Path):
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/0/anneal")
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/1/anneal")
    (first,) = _trees(install)
    (first.parent / f"{first.name}.receipt.json").write_text("truncated", encoding="utf-8")
    for n in range(2, 6):
        _copy_activation_tree([base], dst, base_activation=base, anneal_path=f"/opt/{n}/anneal")
    assert first in _trees(install)


def test_pycache_residue_does_not_block_the_proof(tmp_path: Path):
    """Hooks import `_levain_hook` from their own directory, so a used tree has
    `__pycache__`. MUTATION: stop skipping excluded residue -> no tree ever proves pristine."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/0/anneal")
    for n in range(1, 6):
        (dst / "hooks" / "__pycache__").mkdir(exist_ok=True)
        (dst / "hooks" / "__pycache__" / "_levain_hook.cpython-312.pyc").write_bytes(b"x")
        _copy_activation_tree([base], dst, base_activation=base, anneal_path=f"/opt/{n}/anneal")
    assert len(_trees(install)) == 3


def test_a_SYMLINK_in_a_tree_blocks_the_proof(tmp_path: Path):
    """Install never writes a symlink. MUTATION: follow symlinks in the proof -> a
    symlinked file matching its target's hash reads as pristine and is pruned."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/0/anneal")
    real = tmp_path / "posture_real.md"
    real.write_text((dst / "posture.md").read_text(encoding="utf-8"), encoding="utf-8")
    (dst / "posture.md").unlink()
    (dst / "posture.md").symlink_to(real)
    for n in range(1, 6):
        _copy_activation_tree([base], dst, base_activation=base, anneal_path=f"/opt/{n}/anneal")
    assert any((t / "posture.md").is_symlink() for t in _trees(install))


def test_the_receipt_beside_a_tree_is_the_one_that_installed_it(tmp_path: Path):
    """MUTATION: copy the receipt AFTER the pre-swap invalidation -> no sibling receipt."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    dst = install / "activation"
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/0/anneal")
    live_before = activation_receipt_path(install).read_text(encoding="utf-8")
    _copy_activation_tree([base], dst, base_activation=base, anneal_path="/opt/1/anneal")
    (tree,) = _trees(install)
    assert (tree.parent / f"{tree.name}.receipt.json").read_text(encoding="utf-8") == live_before


def test_receipt_json_is_a_stable_shape(tmp_path: Path):
    """The receipt is read at prune time by spore-861 (ruling B); pin its top level."""
    base = _layer(tmp_path / "base")
    install = tmp_path / "install"
    _copy_activation_tree([base], install / "activation", base_activation=base)
    data = json.loads(activation_receipt_path(install).read_text(encoding="utf-8"))
    assert data["schema"] == 1 and set(data) == {"schema", "written_by", "files"}
