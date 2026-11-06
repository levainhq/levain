"""The observe-mode logger: counts families only, answers nothing, never touches the live ~/.claude."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

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


# --- 1106-1 miscdeny: the live ~/.claude spelled in another letter case (casesweep row 13) -------------------------

def _case_alias(p):
    """``p`` spelled with its letters' case swapped, when this filesystem takes that spelling as the same directory (a
    case-insensitive one: macOS APFS by default); else the test is skipped."""
    import os
    alias = Path(str(p.resolve()).swapcase())
    if str(alias) == str(p.resolve()) or not alias.exists() or not os.path.samefile(alias, p):
        pytest.skip("this filesystem is case-sensitive: no case alias of a directory exists")
    return alias


def test_a_case_alias_of_the_live_claude_dir_still_needs_live(tmp_path, monkeypatch, capsys):
    """casesweep row 13: `Path.resolve()` keeps the spelling it is given, so `--settings-root /USERS/<me>/.CLAUDE` compared
    unequal to the live `~/.claude` and installed the hook there without `--live`. DENY direction (`maybe_inside` both
    ways: equal by identity, or unsure). Needs a case-insensitive volume (macOS APFS): on a case-sensitive one no alias
    of a directory exists and this skips, so a Linux CI run never reds it; `..._with_an_injected_fold` below runs there."""
    home = tmp_path / "home"
    live = home / ".claude"
    live.mkdir(parents=True)
    alias = _case_alias(live)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    assert observe.is_live_root(alias) and observe.is_live_root(live)
    assert not observe.is_live_root(alias / "sub") and not observe.is_live_root(home)     # equal, not "below" or "above"
    assert cli.main(["observe", "install", "--settings-root", str(alias)]) == 2
    assert cli.main(["observe", "uninstall", "--settings-root", str(alias)]) == 2
    assert not (live / "settings.json").exists()
    assert "live ~/.claude" in capsys.readouterr().err


def test_a_settings_file_in_a_case_alias_of_the_live_dir_is_live(tmp_path, monkeypatch):
    """casesweep row 13, `_is_live_settings`: the settings file inside an alias of the live dir (it need not exist yet)
    resolves into the live `~/.claude`. Same volume condition as above (skips on a case-sensitive one)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    alias = _case_alias(home / ".claude")
    monkeypatch.setenv("HOME", str(home))
    assert observe._is_live_settings(alias / "settings.json")
    assert observe._is_live_settings(alias / "other-name.json")                  # the live dir, any name (as before)
    assert not observe._is_live_settings(_case_alias(other) / "settings.json")


def test_the_live_claude_dir_is_found_through_an_injected_case_fold(tmp_path, monkeypatch):
    """The same logic where no alias exists (a Linux CI run): `pathid._fs_spelling` is replaced by a case-insensitive
    walk over the real tree, standing in for a case-insensitive volume. The canonical spellings still answer as before."""
    from levain import pathid
    from tests.test_pathid import _case_insensitive_spelling
    home = tmp_path.resolve() / "home"
    (home / ".claude").mkdir(parents=True)
    (home / "other").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(pathid, "_fs_spelling", _case_insensitive_spelling)
    assert observe.is_live_root(home / ".CLAUDE") and observe.is_live_root(home / ".claude")
    assert observe._is_live_settings(home / ".CLAUDE" / "settings.json")
    assert not observe.is_live_root(home / "OTHER") and not observe.is_live_root(home / ".CLAUDE" / "sub")
    assert not observe.is_live_root(home) and not observe._is_live_settings(home / "OTHER" / "settings.json")


def test_a_case_alias_of_an_absent_live_dir_still_needs_live(tmp_path, monkeypatch, capsys):
    """L3 casefix r1 F2 (codex): with NO `~/.claude` yet, `~/.CLAUDE` is a distinct absent path to `maybe_inside`, so
    `observe install --settings-root ~/.CLAUDE` passed both guards and created the live config directory (on a
    case-insensitive volume `.CLAUDE` is `.claude`). A root whose canonical spelling folds to the live dir's counts
    as live; on a case-sensitive volume that over-refuses `~/.CLAUDE`, which only demands `--live`."""
    home = tmp_path.resolve() / "home"
    home.mkdir()
    (home / "other").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(observe, "_can_import", lambda py: True)
    assert observe.is_live_root(home / ".CLAUDE") and observe.is_live_root(home / ".claude")
    assert observe._is_live_settings(home / ".CLAUDE" / "settings.json")
    assert not observe.is_live_root(home / ".CLAUDE" / "sub") and not observe.is_live_root(home / "other")
    assert not observe._is_live_settings(home / "other" / "settings.json")
    assert cli.main(["observe", "install", "--settings-root", str(home / ".CLAUDE")]) == 2
    assert "live ~/.claude" in capsys.readouterr().err
    assert sorted(p.name for p in home.iterdir()) == ["other"]                  # nothing created


def test_an_unreadable_settings_file_counts_as_live(tmp_path, monkeypatch):
    """L3 casefix r1 F2 (gemini): `canonical` swallows its failures, so `_is_live_settings`'s "cannot tell: treat as
    live" except was dead and a settings file whose spelling could not be read compared unequal and passed."""
    from tests.test_pathid import _unknowable
    home = tmp_path.resolve() / "home"
    (home / ".claude").mkdir(parents=True)
    elsewhere = tmp_path.resolve() / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "settings.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    _unknowable(monkeypatch, elsewhere / "settings.json")
    assert observe._is_live_settings(elsewhere / "settings.json")
    assert not observe._is_live_settings(elsewhere / "other.json")             # a readable file elsewhere: not live
