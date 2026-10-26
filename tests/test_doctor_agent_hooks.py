"""Ruling 3b item 2: doctor pins the agents' hook entries and scripts and warns when Claude
Code's sandbox is off. Doctor detects; it never edits the operator's settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg


@pytest.fixture
def install(tmp_path: Path, monkeypatch):
    ent = tmp_path / "ent"
    hooks = ent / "activation" / "hooks"
    hooks.mkdir(parents=True)
    (ent / ".levain").mkdir()
    (hooks / "session_start.py").write_text("# s\n")
    (ent / ".claude").mkdir()
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [1]}}))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return ent


def test_first_run_records_with_a_warning(install):
    (r,) = doctor._check_agent_hook_pins(install)
    assert r.ok and r.warn and "trust on first use" in r.detail


def test_unchanged_passes(install):
    doctor._check_agent_hook_pins(install)
    (r,) = doctor._check_agent_hook_pins(install)
    assert r.ok and not r.warn and "match" in r.detail


def test_edited_hook_script_fails_named_with_repin_hint(install):
    doctor._check_agent_hook_pins(install)
    with open(install / "activation" / "hooks" / "session_start.py", "a") as fh:
        fh.write("# changed\n")
    (r,) = doctor._check_agent_hook_pins(install)
    assert not r.ok
    assert "activation/hooks/ (scripts)" in r.detail
    assert "--repin-hooks" in r.hint


def test_repin_flag_accepts_the_change(install):
    doctor._check_agent_hook_pins(install)
    (install / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"SessionStart": [2]}}))
    assert not doctor._check_agent_hook_pins(install)[0].ok
    assert doctor._check_agent_hook_pins(install, repin=True)[0].ok
    assert doctor._check_agent_hook_pins(install)[0].ok


def test_doctor_does_not_write_the_operators_settings(install):
    before = (install / ".claude" / "settings.json").read_bytes()
    doctor._check_agent_hook_pins(install)
    doctor._check_cc_sandbox(install)
    assert (install / ".claude" / "settings.json").read_bytes() == before


def test_sandbox_off_warns_and_on_passes(install):
    (r,) = doctor._check_cc_sandbox(install)
    assert r.ok and r.warn and "OFF" in r.detail
    (install / ".claude" / "settings.local.json").write_text(json.dumps({"sandbox": {"enabled": True, "failIfUnavailable": True}}))
    (r,) = doctor._check_cc_sandbox(install)
    assert r.ok and not r.warn


def test_doctor_fails_when_a_workspace_or_user_config_turns_the_hooks_off(install, tmp_path, monkeypatch):
    """L3 r1 G5 (codex H1): a cloned repository's ``.claude/settings.json`` ``disableAllHooks: true`` or
    ``.codex/config.toml`` ``[features] hooks = false`` turns off every hook, the consent gate's floor included.
    Doctor cannot enforce it; it FAILs naming the file. Hooks on, or a file that does not parse, is no row."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    home, codex_home = tmp_path / "home", tmp_path / "codex"
    assert doctor._check_hooks_off(install, "claude-code", home=home) == []
    assert doctor._check_hooks_off(install, "codex", home=home, codex_home=codex_home) == []
    for path, body, adapter in (
            (install / ".claude" / "settings.json", json.dumps({"hooks": {}, "disableAllHooks": True}), "claude-code"),
            (install / ".claude" / "settings.local.json", json.dumps({"disableAllHooks": True}), "claude-code"),
            (home / ".claude" / "settings.json", json.dumps({"disableAllHooks": True}), "claude-code"),
            (install / ".codex" / "config.toml", "[features]\nhooks = false\n", "codex"),
            (codex_home / "config.toml", "features.codex_hooks = false\n", "codex")):
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text() if path.exists() else None
        path.write_text(body)
        (r,) = doctor._check_hooks_off(install, adapter, home=home, codex_home=codex_home)
        assert not r.ok and str(path) in r.detail and "cannot refuse" in r.detail, path
        path.write_text(before) if before is not None else path.unlink()
    (install / ".claude" / "settings.json").write_text(json.dumps({"disableAllHooks": False}))
    (install / ".codex").mkdir(exist_ok=True)
    (install / ".codex" / "config.toml").write_text("[features]\nhooks = true\n")
    (install / ".claude" / "settings.local.json").write_text("{not json")
    assert doctor._check_hooks_off(install, "claude-code", home=home) == []
    assert doctor._check_hooks_off(install, "codex", home=home, codex_home=codex_home) == []


def test_doctor_s_own_call_with_no_home_reads_the_user_settings(install, tmp_path, monkeypatch):
    """L3 r1 (deepseek MED 1, CH-D1c-A) said ``_hooks_off_files`` hands ``_settings_files`` a ``home`` of ``None``
    when doctor omits it. Run 2026-10-26 in a scratch ``$HOME``, doctor's own form (no ``home``) raised nothing:
    ``_hooks_off_files`` binds ``Path.home()`` before that call. This pins the form doctor uses."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    user = tmp_path / "home" / ".claude" / "settings.json"
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"disableAllHooks": True}))
    (r,) = doctor._check_hooks_off(install, "claude-code")
    assert not r.ok and str(user) in r.detail
