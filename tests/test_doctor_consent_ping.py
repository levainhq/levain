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
