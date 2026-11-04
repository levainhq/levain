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
    assert (root / "settings.json").read_text() == before
    (root / "settings.json").write_text("{}")
    monkeypatch.setattr(observe, "_can_import", lambda py: False)
    assert cli.main(["observe", "install", "--settings-root", str(root)]) == 1
    assert (root / "settings.json").read_text() == "{}"


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


# --- 1104-8: the attribution log -----------------------------------------------------------------------------

def _events(d):
    return [json.loads(x) for x in (d / observe.EVENTS_NAME).read_text().splitlines()]


def test_event_log_attributes_and_never_holds_the_command(tmp_path):
    ev = {**SECRET_EVENT, "tool_input": {"command": "export API_KEY=sk-LIVE-9f8e7d6c && curl -H 'Authorization: Bearer sk-LIVE-9f8e7d6c' https://x.example/p",
                                         "file_path": "/Users/someone/private-cwd/.env"},
          "session_id": "ac23b408-5e48-4b5c-a603-058ab01b91d7", "agent_id": "a9614e9e95ccaf22e",
          "agent_type": "general-purpose"}
    observe.record(tmp_path / "counts.json", ev)
    text = (tmp_path / observe.EVENTS_NAME).read_text()
    for leak in ("sk-LIVE", "API_KEY", "Bearer", "x.example", "private-cwd", ".env"):
        assert leak not in text
    (row,) = _events(tmp_path)
    assert row["session_id"] == "ac23b408-5e48-4b5c-a603-058ab01b91d7" and row["agent_id"] == "a9614e9e95ccaf22e"
    assert row["agent_type"] == "general-purpose" and row["permission_mode"] == "bypassPermissions"
    assert row["tool_name"] == "Bash" and row["command"] == "export" and row["session"] == "subagent"
    assert row["ts"].endswith("Z") and (tmp_path / observe.EVENTS_NAME).stat().st_mode & 0o777 == 0o600
    # the counts file is unchanged in shape
    assert set(json.loads((tmp_path / "counts.json").read_text())) == {"Bash|export|0|1|0|subagent|bypassPermissions"}


def test_event_log_keeps_only_identifier_shaped_values_and_a_closed_tool_name(tmp_path):
    observe.record(tmp_path / "c.json", {"tool_name": "mcp__secret-server__do", "session_id": "has space TOKEN=abc",
                                         "agent_id": "x" * 200, "agent_type": "a/b", "permission_mode": "weird"})
    (row,) = _events(tmp_path)
    assert row["tool_name"] == "mcp" and row["session_id"] is None and row["agent_id"] is None
    assert row["agent_type"] is None and row["permission_mode"] == "other"


def test_event_log_is_bounded_by_rotation_and_refuses_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(observe, "EVENTS_CAP", 600)
    for _ in range(40):
        observe.record(tmp_path / "c.json", SECRET_EVENT)
    live, old = tmp_path / observe.EVENTS_NAME, tmp_path / (observe.EVENTS_NAME + ".1")
    assert live.stat().st_size <= 600 and old.exists() and old.stat().st_size <= 600
    live.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    live.symlink_to(target)
    observe.record(tmp_path / "c.json", SECRET_EVENT)   # counts still written, the symlink never followed
    assert target.read_text() == "keep"


def test_event_log_survives_a_fifo_swap_and_tightens_a_loose_file(tmp_path):
    """L3 r1 (codex): a FIFO at the log path must not block the hook; a 0644 file made by hand ends 0600."""
    import os
    fifo = tmp_path / observe.EVENTS_NAME
    os.mkfifo(fifo)
    observe.append_event(tmp_path, SECRET_EVENT)   # lstat refuses a non-regular file: returns, never blocks
    fifo.unlink()
    log = tmp_path / observe.EVENTS_NAME
    log.write_text("")
    os.chmod(log, 0o644)
    observe.append_event(tmp_path, SECRET_EVENT)
    assert log.stat().st_mode & 0o777 == 0o600 and len(_events(tmp_path)) == 1
