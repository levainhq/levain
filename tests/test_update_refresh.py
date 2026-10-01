"""`levain update` refreshes the activation tree and adapter files (gap #19).

Reproduced through the CLI before any of this existed: an install made by levain
0.4.6 from PyPI, then this branch's `levain update`, left `doctor` at exit 6 (a stale
`_levain_hook.py` and a pre-spore-751 `.mcp.json` command); only `init --force`, which
re-runs the whole interview, cleared it. These build a REAL install with `run_init`,
because the refresh decides from real receipts and real template bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from levain.install import (
    ADAPTER_RECEIPT_REL,
    activation_receipt_path,
    refresh_adapter,
    run_init,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def make_install(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    def _make(adapter: str = "claude-code", name: str = "entity") -> Path:
        from tests.test_init_answers import _filled

        af = tmp_path / f"{name}-answers.json"
        af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
        install = tmp_path / name
        assert run_init(install, adapter, force=False, answers_file=af) == 0
        capsys.readouterr()
        return install

    return _make


def _set_receipt(install: Path, rel: str, written: bytes) -> None:
    """Make the receipt say an older levain wrote ``written`` at ``rel``."""
    path = activation_receipt_path(install)
    data = json.loads(path.read_text())
    data["files"][rel]["installed"] = _sha(written)
    path.write_text(json.dumps(data))


def _refresh(install: Path, *, apply: bool = True):
    lines: list[str] = []
    return refresh_adapter(install, apply=apply, emit=lines.append), "\n".join(lines)


HOOK = "hooks/_levain_hook.py"


def test_a_fresh_install_is_already_current(make_install):
    install = make_install()
    r, out = _refresh(install)
    assert not r.refreshed and not r.review and out == ""
    assert install.joinpath(*ADAPTER_RECEIPT_REL).is_file()  # init records it


def test_an_unedited_hook_from_an_older_release_is_refreshed(make_install):
    install = make_install()
    hook = install / "activation" / HOOK
    current = hook.read_bytes()
    hook.write_bytes(b"# old hook\n")
    _set_receipt(install, HOOK, b"# old hook\n")
    r, _out = _refresh(install)
    assert r.refreshed == [f"activation/{HOOK}"] and not r.review
    assert hook.read_bytes() == current
    receipt = json.loads(activation_receipt_path(install).read_text())["files"]
    assert receipt[HOOK]["installed"] == _sha(current)


def test_an_operator_edit_the_package_has_not_moved_is_kept_quietly(make_install):
    install = make_install()
    posture = install / "activation" / "posture.md"
    posture.write_text("MY POSTURE\n")
    r, out = _refresh(install)
    assert not r.refreshed and not r.review and out == ""
    assert posture.read_text() == "MY POSTURE\n"


def test_an_edit_and_a_package_change_together_are_kept_and_flagged(make_install):
    install = make_install()
    posture = install / "activation" / "posture.md"
    posture.write_text("MY POSTURE\n")
    _set_receipt(install, "posture.md", b"what an older levain wrote\n")
    r, out = _refresh(install)
    assert r.review == ["activation/posture.md"] and "Merge by hand" in out
    assert posture.read_text() == "MY POSTURE\n"


def test_an_install_without_a_receipt_is_replaced_whole_with_a_backup(make_install):
    install = make_install()
    hook = install / "activation" / HOOK
    current = hook.read_bytes()
    hook.write_bytes(b"# old hook\n")
    activation_receipt_path(install).unlink()
    r, out = _refresh(install)
    assert r.refreshed and r.refreshed[0].startswith("activation/ (whole tree")
    assert hook.read_bytes() == current
    backups = list((install / ".levain" / "backups" / "activation").glob("tree-*"))
    kept = [b for b in backups if b.is_dir()]
    assert [(k / HOOK).read_bytes() for k in kept] == [b"# old hook\n"]
    assert activation_receipt_path(install).is_file()  # receipt-backed from now on


def test_an_unreadable_receipt_refreshes_nothing_in_the_tree(make_install):
    install = make_install()
    (install / "activation" / HOOK).write_bytes(b"# old hook\n")
    activation_receipt_path(install).write_text("{not json")
    r, out = _refresh(install)
    assert r.review == ["activation/"] and "unreadable" in out
    assert (install / "activation" / HOOK).read_bytes() == b"# old hook\n"


def test_a_dry_run_writes_nothing(make_install):
    install = make_install()
    hook = install / "activation" / HOOK
    hook.write_bytes(b"# old hook\n")
    _set_receipt(install, HOOK, b"# old hook\n")
    mcp = install / ".mcp.json"
    mcp.write_text("{}\n")
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()  # no record: it would be refreshed
    r, out = _refresh(install, apply=False)
    assert f"activation/{HOOK}" in r.refreshed and ".mcp.json" in r.refreshed
    assert "would refresh" in out
    assert hook.read_bytes() == b"# old hook\n" and mcp.read_text() == "{}\n"


def test_adapter_files_without_a_record_are_backed_up_then_refreshed(make_install):
    install = make_install()
    mcp = install / ".mcp.json"
    current = mcp.read_text()
    mcp.write_text('{"old": true}\n')
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert ".mcp.json" in r.refreshed and mcp.read_text() == current
    assert [b.read_text() for b in install.glob(".mcp.json.bak.*")] == ['{"old": true}\n']


def test_a_customised_settings_file_is_kept_when_levain_has_not_changed_it(make_install):
    install = make_install()
    settings = install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["myKey"] = 1
    settings.write_text(json.dumps(data))
    r, _out = _refresh(install)
    assert not r.refreshed and not r.review
    assert json.loads(settings.read_text())["myKey"] == 1


def test_a_customised_file_levain_also_changed_is_flagged(make_install):
    install = make_install()
    settings = install / ".claude" / "settings.json"
    settings.write_text('{"mine": true}\n')
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"][".claude/settings.json"] = _sha(b"an older levain's settings\n")
    receipt.write_text(json.dumps(data))
    r, _out = _refresh(install)
    assert r.review == [".claude/settings.json"]
    assert settings.read_text() == '{"mine": true}\n'


def test_codex_global_files_of_another_install_are_left_alone(make_install, tmp_path):
    install = make_install("codex")
    other = make_install("codex", name="other")  # now owns CODEX_HOME's files
    hooks = tmp_path / "codex-home" / "hooks.json"
    config = tmp_path / "codex-home" / "config.toml"
    before = hooks.read_text(), config.read_text()
    assert str(other) in before[0]
    r, out = _refresh(install)
    assert not r.review and (hooks.read_text(), config.read_text()) == before
    assert "belongs to another install" in out and "another store" in out


def test_codex_global_files_of_this_install_are_refreshed(make_install, tmp_path):
    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    current = hooks.read_text()
    hooks.write_text(current.replace("activation", "activation-old"))
    r, _out = _refresh(install)
    assert str(hooks) in r.refreshed and hooks.read_text() == current
    assert list(hooks.parent.glob("hooks.json.bak.*"))


def test_an_openhands_entity_has_nothing_to_refresh(make_install):
    install = make_install("openhands")
    r, out = _refresh(install)
    assert not r.refreshed and not r.review and out == ""


def test_update_refreshes_even_when_the_lock_is_already_current(make_install):
    # The early "nothing to reconcile" return used to end the run before any refresh,
    # so an operator who upgraded and ran update once would never get the new hooks.
    from levain.update import run_update

    install = make_install()
    hook = install / "activation" / HOOK
    current = hook.read_bytes()
    hook.write_bytes(b"# old hook\n")
    _set_receipt(install, HOOK, b"# old hook\n")
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    assert hook.read_bytes() == current
    assert any("Already at the known-good set" in ln or "advisories" in ln for ln in lines)


def test_update_is_refused_while_another_process_writes_the_install(make_install):
    import fcntl
    import os

    from levain.install import INSTALL_LOCK_REL
    from levain.update import run_update

    install = make_install()
    fd = os.open(install.joinpath(*INSTALL_LOCK_REL), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lines: list[str] = []
    try:
        assert run_update(install, no_pip=True, emit=lines.append) == 1
    finally:
        os.close(fd)
    assert any("another levain process" in ln for ln in lines)
