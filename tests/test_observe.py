"""The observe-mode logger: counts families only, answers nothing, never touches the live ~/.claude."""

from __future__ import annotations

import io
import json
from pathlib import Path

from levain import cli, observe

SECRET_EVENT = {
    "tool_name": "Bash",
    "tool_input": {"command": "curl https://user:TOKEN-abc123@example.com/secret/path $HOME"},
    "session_id": "SESSION-ID-777",
    "cwd": "/Users/someone/private-cwd",
    "permission_mode": "bypassPermissions",
}


def test_stored_file_has_no_command_path_cwd_or_session_id(tmp_path):
    p = tmp_path / "counts.json"
    observe.record(p, SECRET_EVENT)
    observe.record(p, {**SECRET_EVENT, "agent_id": "AGENT-XYZ"})
    stored = p.read_text()
    for leak in ("TOKEN-abc123", "example.com", "secret/path", "SESSION-ID-777", "private-cwd", "AGENT-XYZ", "HOME"):
        assert leak not in stored
    assert set(json.loads(stored)) == {"Bash|curl|1|0|0|main|bypassPermissions",
                                       "Bash|curl|1|0|0|subagent|bypassPermissions"}


def test_hook_prints_nothing_and_exits_zero_on_garbage_and_unwritable_home(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LEVAIN_HOME", str(blocker))  # observe/ cannot be created under a file
    for stdin in ("not json {{", "", json.dumps(SECRET_EVENT)):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        assert observe.main(["hook"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_install_preserves_other_hooks_and_uninstall_removes_only_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    root = tmp_path / "claude"
    root.mkdir()
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-hook"}]}
    (root / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"PermissionRequest": [other]}}))
    assert observe.install(root) == 0
    doc = json.loads((root / "settings.json").read_text())
    assert doc["theme"] == "dark" and doc["hooks"]["PermissionRequest"][0] == other
    assert len(doc["hooks"]["PermissionRequest"]) == 2
    assert observe.install(root) == 0 and len(json.loads((root / "settings.json").read_text())["hooks"]["PermissionRequest"]) == 2
    assert observe.uninstall(root) == 0
    assert json.loads((root / "settings.json").read_text()) == {"theme": "dark", "hooks": {"PermissionRequest": [other]}}


def test_install_refuses_the_live_claude_dir_without_live(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    live = tmp_path / ".claude"
    live.mkdir()
    assert cli.main(["observe", "install", "--settings-root", str(live)]) != 0
    assert cli.main(["observe", "uninstall", "--settings-root", str(live)]) != 0
    assert not (live / "settings.json").exists()
    assert cli.main(["observe", "install", "--settings-root", str(live), "--live"]) == 0
    assert (live / "settings.json").exists()


def test_install_refuses_and_writes_nothing_on_a_non_list_hook_entry_or_a_failed_import(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    (root / "settings.json").write_text('{"hooks": {"PermissionRequest": {"matcher": "Bash"}}}')
    before = (root / "settings.json").read_text()
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    assert cli.main(["observe", "install", "--settings-root", str(root)]) == 1
    assert cli.main(["observe", "uninstall", "--settings-root", str(root)]) == 1
    (root / "settings.json").write_text("{}")
    monkeypatch.setattr(observe, "_can_import", lambda py: False)
    assert cli.main(["observe", "install", "--settings-root", str(root)]) == 1
    assert (root / "settings.json").read_text() == "{}" and before


def test_a_settings_symlink_into_the_live_dir_needs_live(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    other = tmp_path / "other"
    other.mkdir()
    (other / "settings.json").symlink_to(home / ".claude" / "settings.json")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    assert cli.main(["observe", "install", "--settings-root", str(other)]) == 2
    assert (home / ".claude" / "settings.json").read_text() == "{}"
