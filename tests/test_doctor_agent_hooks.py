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
