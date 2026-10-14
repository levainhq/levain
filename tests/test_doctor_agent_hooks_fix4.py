"""Ruling 3b FIX4 (L3 round 3 on 3b): argv[0] is the interpreter whenever later tokens exist and is
never pinned, even a copy under the install; containment is lexical, so an in-root symlink to an
outside target is pinned by content; every later existing file or directory operand that cannot be
pinned is named, whether or not another operand pinned."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return tmp_path


def _ent(root: Path) -> Path:
    ent = root / "ent"
    (ent / "activation" / "hooks").mkdir(parents=True)
    (ent / ".levain").mkdir()
    (ent / "activation" / "hooks" / "s.py").write_text("# s\n")
    (ent / ".claude").mkdir()
    return ent


def _set(ent: Path, cmd: str) -> None:
    (ent / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}]}]}}))


def _check(ent: Path):
    return doctor._check_agent_hook_pins(ent)


def test_an_interpreter_copied_under_the_install_is_not_pinned(env):
    ent = _ent(env)
    py = ent / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("v1")
    tool = ent / "tools" / "t.py"
    tool.parent.mkdir()
    tool.write_text("1")
    _set(ent, f'{py} "{tool}"')
    _check(ent)
    py.write_text("v2 (venv rebuilt)")
    (r, *rest) = _check(ent)
    assert r.ok and "match" in r.detail, r.detail
    assert not [x for x in rest if "not pinned" in x.detail]
    tool.write_text("2")
    assert not _check(ent)[0].ok


def test_a_lone_in_root_executable_is_still_pinned(env):
    ent = _ent(env)
    sh = ent / "tools" / "h.sh"
    sh.parent.mkdir()
    sh.write_text("a")
    _set(ent, str(sh))
    _check(ent)
    sh.write_text("b")
    assert not _check(ent)[0].ok


def test_an_in_root_symlink_to_an_outside_script_is_pinned_by_content(env):
    ent = _ent(env)
    target = env / "elsewhere" / "h.sh"
    target.parent.mkdir()
    target.write_text("one")
    (ent / "tools").mkdir()
    os.symlink(target, ent / "tools" / "h.sh")
    _set(ent, "sh ${CLAUDE_PROJECT_DIR}/tools/h.sh")
    _check(ent)
    assert _check(ent)[0].ok
    target.write_text("two")
    (r, *_) = _check(ent)
    assert not r.ok and "h.sh" in r.detail


def test_an_outside_operand_is_named_even_when_another_operand_pinned(env):
    ent = _ent(env)
    runner = ent / "tools" / "runner.py"
    runner.parent.mkdir()
    runner.write_text("r")
    payload = env / "tmp" / "payload.py"
    payload.parent.mkdir()
    payload.write_text("p")
    _set(ent, f"python3 {runner} {payload}")
    rs = _check(ent)
    named = [r for r in rs if "not pinned" in r.detail and r.warn]
    assert named and str(payload) in named[0].detail, [r.detail for r in rs]


def test_an_outside_directory_operand_is_named(env):
    ent = _ent(env)
    runner = ent / "tools" / "runner.py"
    runner.parent.mkdir()
    runner.write_text("r")
    d = env / "plugins"
    d.mkdir()
    _set(ent, f"python3 {runner} --dir {d}")
    named = [r for r in _check(ent) if "not pinned" in r.detail]
    assert named and str(d) in named[0].detail


def test_a_pinned_in_root_operand_alone_is_silent(env):
    ent = _ent(env)
    runner = ent / "tools" / "runner.py"
    runner.parent.mkdir()
    runner.write_text("r")
    _set(ent, f"python3 {runner}")
    assert not [r for r in _check(ent) if "not pinned" in r.detail]


def test_a_native_binary_argv0_with_any_name_is_an_interpreter(env):
    ent = _ent(env)
    exe = ent / "bin" / "myrt"
    exe.parent.mkdir()
    exe.write_bytes(b"\xcf\xfa\xed\xfe" + b"v1")
    tool = ent / "tools" / "t.py"
    tool.parent.mkdir()
    tool.write_text("1")
    _set(ent, f"{exe} {tool}")
    _check(ent)
    exe.write_bytes(b"\xcf\xfa\xed\xfe" + b"v2")
    assert _check(ent)[0].ok
