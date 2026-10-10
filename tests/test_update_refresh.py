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
import os
import stat
import shlex
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


def test_an_edit_and_a_package_change_together_are_kept_flagged_once_and_staged(make_install):
    install = make_install()
    posture = install / "activation" / "posture.md"
    package = posture.read_bytes()
    posture.write_text("MY POSTURE\n")
    _set_receipt(install, "posture.md", b"what an older levain wrote\n")
    r, out = _refresh(install)
    assert r.review == ["activation/posture.md"] and "Merge it in" in out
    assert posture.read_text() == "MY POSTURE\n"
    staged = install / ".levain" / "pending" / "activation" / "posture.md"
    assert staged.read_bytes() == package
    # L2 HIGH: it used to stay in review on every run with no way to clear it.
    r2, out2 = _refresh(install)
    assert not r2.review and not r2.refreshed and out2 == ""


def test_an_install_without_a_receipt_keeps_edits_and_refreshes_only_hooks(make_install):
    # L2 HIGH, reproduced: with no receipt (every install before this branch) update used
    # to replace the whole tree, reverting posture.md and moving added files out of it.
    install = make_install()
    act = install / "activation"
    hook = act / HOOK
    current = hook.read_bytes()
    hook.write_bytes(b"# old hook\n")
    (act / "posture.md").write_text("MY POSTURE\n")
    (act / "hooks" / "my_custom_hook.py").write_text("# mine\n")
    activation_receipt_path(install).unlink()
    r, out = _refresh(install)
    assert hook.read_bytes() == current
    backups = list((install / ".levain" / "backups" / "activation").glob(f"files-*/{HOOK}"))
    assert [b.read_bytes() for b in backups] == [b"# old hook\n"]
    assert (act / "posture.md").read_text() == "MY POSTURE\n"
    assert r.review == ["activation/posture.md"]
    assert (act / "hooks" / "my_custom_hook.py").read_text() == "# mine\n"
    assert activation_receipt_path(install).is_file()  # receipt-backed from now on
    assert not _refresh(install)[0].review


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
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()  # no record: it would be staged
    r, out = _refresh(install, apply=False)
    assert f"activation/{HOOK}" in r.refreshed and ".mcp.json" in r.review
    assert "would refresh" in out
    assert hook.read_bytes() == b"# old hook\n" and mcp.read_text() == "{}\n"
    assert not (install / ".levain" / "pending").exists()


def test_an_adapter_file_without_a_record_is_kept_and_staged(make_install):
    # L1 + L2 HIGH, reproduced: an added MCP server was dropped on the first update.
    install = make_install()
    mcp = install / ".mcp.json"
    package = mcp.read_text()
    mine = json.loads(package)
    mine.setdefault("mcpServers", {})["github"] = {"command": "gh-mcp"}
    mcp.write_text(json.dumps(mine))
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert r.review == [".mcp.json"] and "github" in mcp.read_text()
    assert (install / ".levain" / "pending" / ".mcp.json").read_text() == package
    assert not _refresh(install)[0].review


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
    # L1 + L2 HIGH, reproduced: "/x/inst" is a prefix of "/x/inst2", and a substring
    # test let inst's update repoint the hooks inst2 owned.
    install = make_install("codex", name="inst")
    other = make_install("codex", name="inst2")  # now owns CODEX_HOME's files
    hooks = tmp_path / "codex-home" / "hooks.json"
    config = tmp_path / "codex-home" / "config.toml"
    before = hooks.read_text(), config.read_text()
    assert str(other) in before[0]
    r, out = _refresh(install)
    # hooks.json is not what levain wrote for THIS install, and this levain renders the same
    # as it did: kept as it is. Had the package moved, _refresh_decision would stage it.
    assert not r.review and (hooks.read_text(), config.read_text()) == before
    assert "another store" in out


def test_codex_global_files_of_this_install_are_refreshed(make_install, tmp_path):
    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    current = hooks.read_text()
    older = current.replace('"timeout": 30', '"timeout": 31')   # still this install's shape
    assert older != current
    hooks.write_text(older)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    assert "codex-home/hooks.json" in data["files"]  # init records it
    data["files"]["codex-home/hooks.json"] = _sha(older.encode())
    receipt.write_text(json.dumps(data))
    r, _out = _refresh(install)
    assert str(hooks) in r.refreshed and hooks.read_text() == current
    assert list(hooks.parent.glob("hooks.json.bak.*"))


def _codex_r7_shape(shape: str, current: str, tmp_path: Path) -> str:
    """hooks.json texts from spore-866 L3 r7's findings against the deleted text predicate,
    and a legacy install's own render from another interpreter. On 3d6152a the echo,
    symlink and legacy texts were overwritten; nan_constant was already refused there and
    stays as a regression pin."""
    data = json.loads(current)
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]
    script = shlex.split(cmd["command"])[1]
    if shape == "nan_constant":        # a JSON constant json.loads accepts
        cmd["timeout"] = float("nan")
    elif shape == "echo_exact_script":  # the exact generated command, run by something else
        cmd["command"] = "/bin/echo " + cmd["command"]
    elif shape == "symlink_dotdot":    # normpath collapses `link/..`; the kernel follows link
        (tmp_path / "far" / "away").mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "far" / "away")
        rel = Path(script).relative_to(tmp_path)
        cmd["command"] = cmd["command"].replace(script, f"{tmp_path}/link/../{rel}")
    else:                              # "legacy": levain's render, another interpreter
        cmd["command"] = cmd["command"].replace(shlex.split(cmd["command"])[0],
                                                "/usr/old/bin/python3", 1)
    return json.dumps(data, indent=2)


@pytest.mark.parametrize("shape", ["nan_constant", "echo_exact_script", "symlink_dotdot",
                                   "legacy"])
def test_codex_hooks_not_recorded_for_this_install_are_staged(
        make_install, tmp_path, monkeypatch, shape):
    # Phill 2026-10-10, "go with B": the adapter receipt alone decides. With no record of
    # levain writing hooks.json (an install from before 0.5.0, or a lost receipt), any bytes
    # go to pending, untouched. MUTATION (2026-10-10): on 3d6152a's install.py the echo,
    # symlink and legacy cases were overwritten.
    import levain.install as inst_mod

    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    current = hooks.read_text()
    text = _codex_r7_shape(shape, current, tmp_path)
    assert text != current
    hooks.write_text(text)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    del data["files"]["codex-home/hooks.json"]
    receipt.write_text(json.dumps(data))
    r, out = _refresh(install)
    assert hooks.read_text() == text and "codex-home/hooks.json" in r.review
    staged = install.joinpath(".levain", "pending", "codex-home", "hooks.json")
    assert staged.read_text() == current
    assert not list(hooks.parent.glob("hooks.json.bak.*"))
    r, out = _refresh(install)                        # once, then quiet
    assert hooks.read_text() == text and not r.review and "hooks.json" not in out
    hooks.write_text(staged.read_text())              # the operator accepts levain's version
    r, out = _refresh(install)
    assert not r.review and not r.refreshed
    moved = current + "\n"                           # the next release refreshes it
    monkeypatch.setattr(inst_mod, "_codex_hooks_json", lambda *a: moved)
    r, out = _refresh(install)
    assert str(hooks) in r.refreshed and hooks.read_text() == moved


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


def test_a_symlinked_activation_file_stays_a_symlink(make_install, tmp_path):
    install = make_install()
    posture = install / "activation" / "posture.md"
    package = posture.read_bytes()
    real = tmp_path / "dotfiles-posture.md"
    real.write_bytes(b"older\n")
    posture.unlink()
    posture.symlink_to(real)
    _set_receipt(install, "posture.md", b"older\n")
    r, _out = _refresh(install)
    assert r.refreshed == ["activation/posture.md"]
    assert posture.is_symlink() and real.read_bytes() == package


def test_a_deleted_adapter_file_stays_deleted(make_install):
    install = make_install()
    (install / ".mcp.json").unlink()
    r, _out = _refresh(install)
    assert not r.refreshed and not (install / ".mcp.json").exists()


def _older_carrier(install):
    carrier = install / "CLAUDE.md"
    carrier.write_text("older\n")
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"]["CLAUDE.md"] = _sha(b"older\n")
    receipt.write_text(json.dumps(data))
    return carrier


def test_the_carrier_is_refreshed_when_unedited(make_install):
    install = make_install()
    carrier = _older_carrier(install)
    r, _out = _refresh(install)
    assert "CLAUDE.md" in r.refreshed and carrier.read_text() != "older\n"


def test_the_carrier_is_held_while_the_pack_layer_is_unsettled(make_install):
    install = make_install()
    carrier = _older_carrier(install)
    lines: list[str] = []
    r = refresh_adapter(install, apply=True, emit=lines.append, carrier=False)
    assert carrier.read_text() == "older\n" and "CLAUDE.md" not in r.refreshed
    assert any("holding seed changes" in ln for ln in lines)


def test_the_carrier_is_held_while_a_seed_it_imports_is_missing(make_install):
    install = make_install()
    carrier = _older_carrier(install)
    (install / "seed" / "partnership.md").unlink()
    r, out = _refresh(install)
    assert carrier.read_text() == "older\n" and "partnership.md" in out


def test_an_unreadable_adapter_file_is_flagged_not_passed(make_install):
    import os

    install = make_install()
    carrier = install / "CLAUDE.md"
    os.chmod(carrier, 0)
    try:
        r, out = _refresh(install)
    finally:
        os.chmod(carrier, 0o644)
    assert r.review == ["CLAUDE.md"] and "could not be read" in out


def test_update_on_a_path_that_is_not_an_install_creates_nothing(tmp_path):
    from levain.update import run_update

    target = tmp_path / "typo"
    target.mkdir()
    lines: list[str] = []
    assert run_update(target, no_pip=True, emit=lines.append) == 1
    assert not (target / ".levain").exists()


def _pre_launcher_mcp(install: Path, store: str) -> Path:
    """.mcp.json as levain 0.4.6 wrote it (before spore-751's launcher), and no record."""
    mcp = install / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"anneal_memory": {
        "type": "stdio", "command": "/usr/local/bin/anneal-memory",
        "args": ["--db", store, "serve"]}}}))
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    return mcp


def test_an_older_releases_mcp_launcher_is_refreshed(make_install):
    install = make_install()
    package = (install / ".mcp.json").read_text()
    mcp = _pre_launcher_mcp(install, str(install / ".levain" / "memory.db"))
    r, _out = _refresh(install)
    assert ".mcp.json" in r.refreshed and mcp.read_text() == package
    assert list(install.glob(".mcp.json.bak.*"))


def test_an_mcp_entry_serving_another_store_is_staged_not_replaced(make_install, tmp_path):
    install = make_install()
    mcp = _pre_launcher_mcp(install, str(tmp_path / "elsewhere.db"))
    before = mcp.read_text()
    r, _out = _refresh(install)
    assert r.review == [".mcp.json"] and mcp.read_text() == before


def test_a_missing_imported_seed_is_review_not_clean(make_install):
    # codex L3 HIGH: the carrier was held but update exited 0 over a dangling import.
    install = make_install()
    (install / "seed" / "partnership.md").unlink()
    r, _out = _refresh(install)
    assert "CLAUDE.md" in r.review


def test_a_failing_codex_config_merge_is_review_not_a_traceback(make_install, tmp_path,
                                                                monkeypatch):
    # codex L3 HIGH: _merge_codex_config raises InitError, which escaped as a traceback.
    from levain import install as inst_mod

    install = make_install("codex")
    config = tmp_path / "codex-home" / "config.toml"
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    text = config.read_text()
    older = text.replace('"serve"]', '"serve-old"]')
    assert older != text
    config.write_text(older)
    data["files"][inst_mod.CODEX_CONFIG_KEY] = inst_mod._codex_block_hash(older)
    receipt.write_text(json.dumps(data))

    def boom(*_a, **_k):
        raise inst_mod.InitError("backup failed")

    monkeypatch.setattr(inst_mod, "_merge_codex_config", boom)
    r, out = _refresh(install)
    assert inst_mod.CODEX_CONFIG_KEY in r.review and "backup failed" in out


def test_a_customised_codex_block_on_the_same_store_is_staged(make_install, tmp_path):
    # codex L3 MED: store equality alone let update replace an operator's `env`.
    install = make_install("codex")
    config = tmp_path / "codex-home" / "config.toml"
    text = config.read_text()
    mine = text.replace("[mcp_servers.anneal_memory]\n",
                        "[mcp_servers.anneal_memory]\nstartup_timeout_ms = 90000\n", 1)
    assert mine != text
    config.write_text(mine)
    # Unchanged package: the edit is simply kept, with nothing to say.
    assert not _refresh(install)[0].review and config.read_text() == mine
    # The package moves (the record holds an older block): now it is staged, once.
    from levain import install as inst_mod

    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"][inst_mod.CODEX_CONFIG_KEY] = _sha(b"an older block\n")
    receipt.write_text(json.dumps(data))
    r, out = _refresh(install)
    assert config.read_text() == mine
    assert any("config.toml" in x for x in r.review) and "Listed once" in out
    assert not _refresh(install)[0].review


def test_a_pre_receipt_settings_leaf_edit_is_staged(make_install):
    # codex L3 MED: same-shaped JSON was taken as levain's even with an operator's leaf.
    install = make_install()
    settings = install / ".claude" / "settings.json"
    data = json.loads(settings.read_text())

    def first_string_leaf(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and "python" not in v.lower() and "/" not in v:
                    node[k] = v + "-mine"
                    return True
                if first_string_leaf(v):
                    return True
        if isinstance(node, list):
            return any(first_string_leaf(v) for v in node)
        return False

    assert first_string_leaf(data)
    settings.write_text(json.dumps(data))
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert r.review == [".claude/settings.json"]


def test_a_pre_receipt_settings_from_another_interpreter_is_refreshed(make_install):
    install = make_install()
    settings = install / ".claude" / "settings.json"
    import sys

    settings.write_text(settings.read_text().replace(sys.executable, "/old/venv/bin/python"))
    install.joinpath(*ADAPTER_RECEIPT_REL).unlink()
    r, _out = _refresh(install)
    assert ".claude/settings.json" in r.refreshed


def test_an_obsolete_activation_file_is_named_and_never_deleted(make_install):
    # A file levain stopped shipping is dropped from the record and named, never deleted
    # (codex L3 r2: deleting by receipt key reached outside the install).
    install = make_install()
    old = install / "activation" / "hooks" / "obsolete.py"
    old.write_text("# shipped once\n")
    path = activation_receipt_path(install)
    data = json.loads(path.read_text())
    data["files"]["hooks/obsolete.py"] = {"installed": _sha(b"# shipped once\n"),
                                          "source": _sha(b"# shipped once\n")}
    path.write_text(json.dumps(data))
    r, out = _refresh(install)
    assert old.exists() and "no longer shipped" in out
    assert "hooks/obsolete.py" not in json.loads(path.read_text())["files"]


def test_an_obsolete_edited_activation_file_is_left(make_install):
    install = make_install()
    old = install / "activation" / "notes.md"
    old.write_text("mine now\n")
    path = activation_receipt_path(install)
    data = json.loads(path.read_text())
    data["files"]["notes.md"] = {"installed": _sha(b"shipped\n"), "source": _sha(b"shipped\n")}
    path.write_text(json.dumps(data))
    r, out = _refresh(install)
    assert old.read_text() == "mine now\n" and "left in place" in out


def test_a_receipt_key_outside_activation_never_deletes_anything(make_install, tmp_path):
    # codex L3 r2 HIGH, reproduced: "../../important.txt" with a matching hash was deleted.
    install = make_install()
    victim = tmp_path / "important.txt"
    victim.write_text("precious\n")
    path = activation_receipt_path(install)
    data = json.loads(path.read_text())
    data["files"]["../../important.txt"] = {"installed": _sha(b"precious\n"),
                                            "source": _sha(b"precious\n")}
    path.write_text(json.dumps(data))
    _refresh(install)
    assert victim.read_text() == "precious\n"


def test_a_current_tree_without_a_receipt_gets_one(make_install):
    install = make_install()
    activation_receipt_path(install).unlink()
    _refresh(install)
    assert activation_receipt_path(install).is_file()


def test_a_pre_receipt_deleted_file_is_not_recreated(make_install):
    # complement L3: with no record, an absent posture.md may be one the operator deleted.
    install = make_install()
    (install / "activation" / "posture.md").unlink()
    activation_receipt_path(install).unlink()
    r, _out = _refresh(install)
    assert not (install / "activation" / "posture.md").exists()
    assert "activation/posture.md" in r.review


def test_an_unreadable_codex_hooks_file_is_review(make_install, tmp_path):
    # codex L3 MED: unreadable read as "another install's" and passed.
    import os

    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    os.chmod(hooks, 0)
    try:
        r, out = _refresh(install)
    finally:
        os.chmod(hooks, 0o644)
    assert str(hooks) in r.review and "could not be read" in out


def test_update_does_not_install_a_pack_hook_the_reconcile_is_holding(tmp_path, capsys, monkeypatch):
    """codex L3 on 19811f7, reproduced: a pulled pack changed its activation hook; update's
    pack reconcile listed it "activation changed — review" and exited 1, while the adapter
    refresh in the same run installed the new hook bytes (executable code) anyway."""
    from levain.update import run_update
    from tests.test_init_answers import _filled
    from tests.test_reconcile import _write_pack

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    pack = _write_pack(tmp_path / "pack", name="zzpack", seed={"zz_note.md": "note\n"},
                       activation={"hooks/zz_pack_hook.py": "print('v1')\n"})
    af = tmp_path / "a.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    install = tmp_path / "ent"
    assert run_init(install, "claude-code", force=False, packs=[pack], answers_file=af) == 0
    hook = install / "activation" / "hooks" / "zz_pack_hook.py"
    assert hook.read_text() == "print('v1')\n"

    # ...and, in the same run, a base hook an older levain wrote (L1 on the first cut of this
    # fix: holding the whole tree kept base hook fixes out for as long as the pack review stood).
    base = install / "activation" / HOOK
    current = base.read_bytes()
    base.write_bytes(b"# old hook\n")
    _set_receipt(install, HOOK, b"# old hook\n")

    (pack / "activation" / "hooks" / "zz_pack_hook.py").write_text("print('v2')\n")
    lines: list[str] = []
    rc = run_update(install, no_pip=True, yes=True, emit=lines.append, confirm=lambda s: False)
    out = "\n".join(lines)
    assert rc == 1 and "activation changed" in out
    assert hook.read_text() == "print('v1')\n", "an unreviewed pack hook was installed"
    assert base.read_bytes() == current, "a base hook fix was held along with the pack hook"


def test_doctor_names_update_for_a_stale_hook_and_carrier_and_update_clears_both(make_install):
    """Reproduced at 19811f7 (the merge of install truth): doctor's stale-hook remedy said
    hook fixes "do NOT arrive via ... `levain update`" and sent the operator to
    `init --force`, which re-runs the whole interview, while `levain update` (this
    refresh) already cleared the same hook. The carrier remedy said the same. Follow the
    remedy doctor prints, then doctor must go green."""
    from levain.doctor import _check_carrier_freshness, _check_hook_freshness

    install = make_install()
    hook = install / "activation" / HOOK
    hook.write_bytes(b"# old hook\n")
    _set_receipt(install, HOOK, b"# old hook\n")
    carrier = install / "CLAUDE.md"
    stale_text = carrier.read_text() + "\n@seed/spore_instructions.md\n"
    carrier.write_text(stale_text)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"]["CLAUDE.md"] = _sha(stale_text.encode())
    receipt.write_text(json.dumps(data))

    for check, remedy in ((_check_hook_freshness(install), "hook"),
                          (_check_carrier_freshness(install, carrier), "carrier")):
        assert not check[0].ok, remedy
        assert "levain update" in check[0].hint and "init --force" not in check[0].hint, remedy

    _refresh(install)
    assert _check_hook_freshness(install)[0].ok
    assert _check_carrier_freshness(install, carrier)[0].ok


def _install_with_render_pack(tmp_path, capsys, monkeypatch) -> tuple[Path, Path]:
    """A real install (run_init + a real anneal store) composed with one pack whose
    ``zz_role.md`` is a RENDER seed, with the store's migrate marker set back by a real
    ``anneal migrate ack`` so a real proposal is pending and `--ack` has something to
    record (no anneal stub; independent of where levain caps the init-time marker)."""
    import subprocess

    from levain import manifest
    from tests.test_init_answers import _filled
    from tests.test_reconcile import _write_pack

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    pack = _write_pack(tmp_path / "pack", name="zzpack", render=["zz_role.md"],
                       seed={"zz_role.md": "Operator: {{OPERATOR_NAME}}\n"})
    af = tmp_path / "a.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    install = tmp_path / "ent"
    assert run_init(install, "claude-code", force=False, packs=[pack], answers_file=af) == 0
    capsys.readouterr()
    subprocess.run([manifest.resolve_anneal_bin(), "--db", str(install / ".levain" / "memory.db"),
                    "migrate", "ack", "0.4.6"], check=True, capture_output=True)
    return install, pack


def _migrate_state(install: Path):
    from levain import manifest

    return manifest.discover_installed_set(install / ".levain" / "memory.db",
                                           manifest.resolve_anneal_bin())


def test_ack_is_not_recorded_while_the_reconcile_holds_a_seed_for_review(tmp_path, capsys,
                                                                         monkeypatch):
    """complement L3 (2026-10-03): `--ack` was recorded at step 3, BEFORE the pack
    reconcile. An operator who had edited a seed file ran `levain update --ack`; the
    reconcile then held that file for review (the new text at `.new`, the old text still
    installed) while the migration proposal was already marked done. The ack must wait
    for the reconcile and be skipped when it holds a seed change."""
    from levain.update import run_update

    install, pack = _install_with_render_pack(tmp_path, capsys, monkeypatch)
    before = _migrate_state(install)
    assert before.pending_count, "a fresh store should carry a real pending proposal"
    role = install / "seed" / "zz_role.md"
    role.write_text("Operator: MY HAND EDIT\n", encoding="utf-8")            # operator edit
    (pack / "seed" / "zz_role.md").write_text("Operator v2: {{OPERATOR_NAME}}\n")  # pulled

    lines: list[str] = []
    rc = run_update(install, no_pip=True, ack=True, emit=lines.append,
                    confirm=lambda _p: False)
    out = "\n".join(lines)
    assert rc == 1
    assert (install / "seed" / "zz_role.md.new").exists()          # the seed WAS held
    assert role.read_text() == "Operator: MY HAND EDIT\n"
    after = _migrate_state(install)
    assert after.migrate_acked == before.migrate_acked, "ack recorded over a held seed"
    assert after.pending_count == before.pending_count
    assert "--ack SKIPPED" in out and "held" in out
    assert "acknowledged up to" not in out


def test_ack_waits_for_a_run_that_changes_nothing(tmp_path, capsys, monkeypatch):
    """codex, the ack L3 round: an unedited seed that the reconcile fast-forwards is held
    for nothing, yet the proposals were shown against the OLD file, so this run must not
    record the ack. The next `update --ack`, which changes nothing, records it."""
    from levain.update import run_update

    install, pack = _install_with_render_pack(tmp_path, capsys, monkeypatch)
    before = _migrate_state(install)
    assert before.pending_count
    (pack / "seed" / "zz_role.md").write_text("Operator v2: {{OPERATOR_NAME}}\n")

    lines: list[str] = []
    rc1 = run_update(install, no_pip=True, ack=True, emit=lines.append,
                     confirm=lambda _p: False)
    out = "\n".join(lines)
    assert rc1 == 1, out   # the proposals are still pending
    assert (install / "seed" / "zz_role.md").read_text() == "Operator v2: Chris\n"
    mid = _migrate_state(install)
    assert mid.migrate_acked == before.migrate_acked, "ack recorded on the run that changed a seed"
    assert "--ack SKIPPED" in out and "pack reconcile changed" in out

    lines = []
    rc = run_update(install, no_pip=True, ack=True, emit=lines.append,
                    confirm=lambda _p: False)
    out = "\n".join(lines)
    after = _migrate_state(install)
    assert after.migrate_acked == before.anneal and after.pending_count == 0
    assert f"acknowledged up to {before.anneal}." in out and "SKIPPED" not in out
    assert rc == 0, out


def test_ack_is_skipped_on_a_run_that_refreshes_the_carrier(tmp_path, capsys, monkeypatch):
    """The adapter refresh rewrites CLAUDE.md, an instruction file, so it gates the ack
    the same way the pack reconcile does."""
    from levain.update import run_update

    install, _pack = _install_with_render_pack(tmp_path, capsys, monkeypatch)
    before = _migrate_state(install)
    carrier = install / "CLAUDE.md"
    current = carrier.read_text()
    older = current + "\nAn older release's line.\n"
    carrier.write_text(older)
    receipt_path = install.joinpath(*ADAPTER_RECEIPT_REL)
    receipt = json.loads(receipt_path.read_text())
    receipt["files"]["CLAUDE.md"] = _sha(older.encode())
    receipt_path.write_text(json.dumps(receipt))

    lines: list[str] = []
    run_update(install, no_pip=True, ack=True, emit=lines.append, confirm=lambda _p: False)
    out = "\n".join(lines)
    assert carrier.read_text() == current
    assert _migrate_state(install).migrate_acked == before.migrate_acked
    assert "--ack SKIPPED" in out and "refreshed" in out


def test_an_interrupted_codex_hooks_write_leaves_the_old_file(tmp_path, monkeypatch):
    # Reproduced 2026-10-10 on a4b2bef: ENOSPC during the write left NO hooks.json (it was
    # unlinked first), only its backup. Now it is written beside it and renamed over it.
    import errno
    import os

    from levain.install import _write_codex_hooks

    hooks = tmp_path / "hooks.json"
    hooks.write_text('{"old": true}\n')

    def full(*_a):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError):
        _write_codex_hooks(hooks, '{"new": true}\n', lambda _s: None)
    assert hooks.read_text() == '{"old": true}\n'
    assert sorted(p.name.split(".bak.")[0] for p in tmp_path.iterdir()) == ["hooks.json",
                                                                           "hooks.json"]
    monkeypatch.undo()
    _write_codex_hooks(hooks, '{"new": true}\n', lambda _s: None)
    assert hooks.read_text() == '{"new": true}\n'
    # codex L3 r2, reproduced: a NEW file was forced to 0644 under umask 077.
    fresh = tmp_path / "new" / "hooks.json"
    fresh.parent.mkdir()
    old_umask = os.umask(0o077)
    try:
        _write_codex_hooks(fresh, "{}", lambda _s: None)
    finally:
        os.umask(old_umask)
    assert fresh.stat().st_mode & 0o077 == 0


def test_init_over_a_dangling_hooks_json_symlink_writes_the_file(make_install, tmp_path):
    # glm L3 r3 HIGH 2026-10-10, reproduced through `levain init`: the backup followed the
    # missing link and raised FileNotFoundError after config.toml was already written.
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    hooks = codex_home / "hooks.json"
    hooks.symlink_to(tmp_path / "nowhere" / "hooks.json")
    install = make_install("codex")  # asserts init exits 0
    assert hooks.is_file() and not hooks.is_symlink()
    assert str(install) in hooks.read_text()
    (bak,) = codex_home.glob("hooks.json.bak.*")
    assert bak.is_symlink() and os.readlink(bak) == str(tmp_path / "nowhere" / "hooks.json")


def test_an_adopted_staged_copy_is_removed(make_install, tmp_path):
    # Reproduced 2026-10-10 through `levain update`: the pending copy outlived its adoption,
    # so copying it over later downgraded the file and the next update called that "keep".
    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    current = hooks.read_text()
    hooks.write_text(current.replace('"timeout": 30', '"timeout": 45'))  # an operator edit
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"]["codex-home/hooks.json"] = _sha(b"an older release's render")
    receipt.write_text(json.dumps(data))
    staged = install / ".levain" / "pending" / "codex-home" / "hooks.json"
    r, _out = _refresh(install)
    assert staged.read_text() == current and r.review
    hooks.write_text(staged.read_text())  # the operator adopts it
    r, out = _refresh(install)
    assert not r.review and not staged.exists() and "settled and removed" in out


@pytest.mark.parametrize("through_link", [False, True])
def test_a_refreshed_hooks_json_keeps_its_mode(make_install, tmp_path, through_link):
    # complement L3 r3 LOW: deleting the fchmod branch left the suite green.
    install = make_install("codex")
    hooks = tmp_path / "codex-home" / "hooks.json"
    current = hooks.read_text()
    older = current.replace('"timeout": 30', '"timeout": 31')
    real = hooks
    if through_link:
        real = tmp_path / "dotfiles" / "hooks.json"
        real.parent.mkdir()
        hooks.unlink()
        hooks.symlink_to(real)
    real.write_text(older)
    real.chmod(0o640)
    receipt = install.joinpath(*ADAPTER_RECEIPT_REL)
    data = json.loads(receipt.read_text())
    data["files"]["codex-home/hooks.json"] = _sha(older.encode())
    receipt.write_text(json.dumps(data))
    r, _out = _refresh(install)
    assert str(hooks) in r.refreshed and hooks.read_text() == current
    assert stat.S_IMODE(hooks.stat().st_mode) == 0o640


def test_codex_home_is_one_writer_across_installs(make_install, tmp_path, capsys):
    # codex L3 r2 HIGH 2026-10-10, reproduced 2/20 pairs through the CLI: two installs'
    # inits interleaved, leaving config.toml on one store and hooks.json on the other.
    import fcntl

    from levain.install import CODEX_HOME_LOCK_NAME
    from levain.update import run_update
    from tests.test_init_answers import _filled

    first = make_install("codex", name="first")
    codex_home = tmp_path / "codex-home"
    before = {p.name: p.read_bytes() for p in codex_home.iterdir() if p.is_file()}
    af = tmp_path / "second-answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    fd = os.open(codex_home / CODEX_HOME_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another install's init, mid-write
    lines: list[str] = []
    try:
        assert run_init(tmp_path / "second", "codex", force=False, answers_file=af) == 1
        assert run_update(first, no_pip=True, emit=lines.append) == 1
    finally:
        os.close(fd)
    assert "writing " + str(codex_home) in capsys.readouterr().out
    assert any(str(codex_home) in ln for ln in lines)
    assert {p.name: p.read_bytes() for p in codex_home.iterdir() if p.is_file()} == before
    assert not (tmp_path / "second" / "seed").exists()
