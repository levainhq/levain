"""The doctor's `consent ping reach` row: the consent hook is wired in the vendor settings, and the queue directory the
hook resolves must have a ping route (the gate's own test, ``claude_code.ping_configured``)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from levain import doctor
from levain.consent.adapters import claude_code
from levain.install import consent_hook_handler

ROW = "consent ping reach"


def _install(tmp_path: Path, *, wired: bool = True) -> Path:
    install = Path(os.path.realpath(tmp_path)) / "ws"
    (install / ".claude").mkdir(parents=True)
    hooks: dict = {}
    if wired:
        _cmd, handler, _script = consent_hook_handler(sys.executable, install, "claude-code")
        hooks = {"PermissionRequest": [{"hooks": [handler]}]}
    (install / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}))
    return install


def _env(tmp_path: Path) -> dict[str, str]:
    home = Path(os.path.realpath(tmp_path)) / "lhome"
    (home / "consent").mkdir(parents=True)
    return {"LEVAIN_HOME": str(home), "HOME": str(tmp_path)}


def _no_phone(monkeypatch):
    monkeypatch.setattr("levain.webpush.live", lambda _q: False)


def test_wired_with_notify_command_is_ok(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    q = Path(env["LEVAIN_HOME"]) / "consent"
    (q / claude_code.SETTINGS_NAME).write_text(json.dumps({"notify_argv": ["/usr/bin/true"]}))
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.name == ROW and row.ok and not row.warn
    assert str(q) in row.detail


def test_wired_with_paired_phone_is_ok(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr("levain.webpush.live", lambda q: seen.append(str(q)) or True)
    install, env = _install(tmp_path), _env(tmp_path)
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.ok and not row.warn
    assert seen == [str(Path(env["LEVAIN_HOME"]) / "consent")]   # asked of the queue the hook resolves


def test_wired_with_no_route_warns_and_names_the_fix(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    q = Path(env["LEVAIN_HOME"]) / "consent"
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.name == ROW and row.ok and row.warn      # a WARN: the hook fails toward his own dialog
    assert "a held request will reach nobody" in row.detail and str(q) in row.detail
    assert "notify_argv" in row.hint and str(q / claude_code.SETTINGS_NAME) in row.hint
    assert "paired phone" in row.hint or "pair a phone" in row.hint


def test_the_queue_is_the_one_the_hook_resolves(tmp_path, monkeypatch):
    """A route in ANOTHER home's queue does not count: the row follows LEVAIN_HOME."""
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    other = Path(os.path.realpath(tmp_path)) / "other" / "consent"
    other.mkdir(parents=True)
    (other / claude_code.SETTINGS_NAME).write_text(json.dumps({"notify_argv": ["/usr/bin/true"]}))
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.warn


def test_hook_not_installed_has_no_row(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install, env = _install(tmp_path, wired=False), _env(tmp_path)
    assert doctor._check_consent_ping_reach(install, environ=env) == []


def test_no_settings_file_has_no_row(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install = Path(os.path.realpath(tmp_path)) / "empty"
    install.mkdir()
    assert doctor._check_consent_ping_reach(install, environ=_env(tmp_path)) == []


def test_row_is_read_only(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    before = sorted(p for p in Path(tmp_path).rglob("*"))
    doctor._check_consent_ping_reach(install, environ=env)
    assert sorted(p for p in Path(tmp_path).rglob("*")) == before


def test_row_is_registered_in_the_doctor_run(monkeypatch):
    import inspect
    src = inspect.getsource(doctor._run_doctor)
    assert "_check_consent_ping_reach" in src


def _settings_for(q: Path, argv):
    (q / claude_code.SETTINGS_NAME).write_text(json.dumps({"notify_argv": argv}))


def test_a_notify_command_the_runtime_cannot_start_is_a_warn(tmp_path, monkeypatch):
    """review r1 (codex 1, complement 1): ping_configured() is True for a relative or unfound command, which
    run_notify_command rejects. The row follows the runtime's resolution."""
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    q = Path(env["LEVAIN_HOME"]) / "consent"
    for argv in (["bin/ping"], ["no-such-notifier-xyz"]):
        _settings_for(q, argv)
        (row,) = doctor._check_consent_ping_reach(install, environ=env)
        assert row.warn and "cannot be started" in row.detail and "reach nobody" in row.detail, argv


def test_an_unrunnable_command_is_still_ok_with_a_live_phone(tmp_path, monkeypatch):
    monkeypatch.setattr("levain.webpush.live", lambda _q: True)
    install, env = _install(tmp_path), _env(tmp_path)
    _settings_for(Path(env["LEVAIN_HOME"]) / "consent", ["bin/ping"])
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.ok and not row.warn


def test_a_bare_command_found_on_the_child_path_is_ok(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install, env = _install(tmp_path), _env(tmp_path)
    _settings_for(Path(env["LEVAIN_HOME"]) / "consent", ["true"])
    monkeypatch.setattr("levain.launch.child_env", lambda: {"PATH": "/usr/bin:/bin"})
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert not row.warn


def _legacy(tmp_path, queue_dir):
    """A per-workspace hook as claude_code.install writes it, with a custom queue dir."""
    ws = Path(os.path.realpath(tmp_path)) / "legacy"
    ws.mkdir(exist_ok=True)
    script = ws / ".claude" / "hooks" / claude_code.HOOK_SCRIPT_NAME
    script.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--max-hold", "10.0"] + (["--queue-dir", str(queue_dir)] if queue_dir else [])
    script.write_text(claude_code.HOOK_TEMPLATE.format(python=sys.executable, argv=repr(argv)))
    cmd = claude_code.hook_command(sys.executable, script)
    (ws / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
    return ws


def test_a_legacy_hook_checks_the_queue_its_script_binds(tmp_path, monkeypatch):
    """review r1 (codex 2): the default queue has a route, the hook's own --queue-dir does not -> WARN naming that dir."""
    _no_phone(monkeypatch)
    env = _env(tmp_path)
    _settings_for(Path(env["LEVAIN_HOME"]) / "consent", ["/usr/bin/true"])
    custom = Path(os.path.realpath(tmp_path)) / "custom-q"
    custom.mkdir()
    (row,) = doctor._check_consent_ping_reach(_legacy(tmp_path, custom), environ=env)
    assert row.warn and str(custom) in row.detail
    _settings_for(custom, ["/usr/bin/true"])
    (row,) = doctor._check_consent_ping_reach(_legacy(tmp_path, custom), environ=env)
    assert not row.warn and str(custom) in row.detail


def test_a_legacy_hook_without_a_queue_dir_follows_levain_home(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    env = _env(tmp_path)
    (row,) = doctor._check_consent_ping_reach(_legacy(tmp_path, None), environ=env)
    assert row.warn and str(Path(env["LEVAIN_HOME"]) / "consent") in row.detail


def test_wiring_that_cannot_be_read_is_an_explicit_warn(tmp_path, monkeypatch):
    """review r1 (deepseek 2/4): wired but unreadable is never a silent no-row."""
    _no_phone(monkeypatch)
    env = _env(tmp_path)
    install = _install(tmp_path)
    (install / ".claude" / "settings.json").write_text("{ not json " + claude_code.HOOK_SCRIPT_NAME)
    (row,) = doctor._check_consent_ping_reach(install, environ=env)
    assert row.warn and "could not be read" in row.detail


def test_a_hook_script_named_in_an_unrecognised_shape_is_a_warn(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    install = _install(tmp_path, wired=False)
    cmd = f"/opt/wrapper {install}/.claude/hooks/{claude_code.HOOK_SCRIPT_NAME} --extra"
    (install / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"PermissionRequest": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
    (row,) = doctor._check_consent_ping_reach(install, environ=_env(tmp_path))
    assert row.warn and "could not be read" in row.detail


def test_a_legacy_script_that_cannot_be_parsed_is_a_warn(tmp_path, monkeypatch):
    _no_phone(monkeypatch)
    ws = _legacy(tmp_path, None)
    (ws / ".claude" / "hooks" / claude_code.HOOK_SCRIPT_NAME).write_text("#!/bin/sh\nexit 0\n")
    (row,) = doctor._check_consent_ping_reach(ws, environ=_env(tmp_path))
    assert row.warn and "could not be read" in row.detail


def test_a_corrupt_settings_file_with_no_levain_hook_has_no_row(tmp_path, monkeypatch):
    """Not wired and nothing of levain's there: a broken settings file of someone else's is not this row's finding."""
    _no_phone(monkeypatch)
    install = _install(tmp_path, wired=False)
    (install / ".claude" / "settings.json").write_text("{ not json")
    assert doctor._check_consent_ping_reach(install, environ=_env(tmp_path)) == []
