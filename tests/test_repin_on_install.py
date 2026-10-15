"""Ruling 3b FIX1: a legitimate `levain init` / `levain update` re-pins the agent hook files it
wrote, so `doctor` is not a standing false positive; an edit by anything else still fails."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain.install import refresh_adapter, run_init

HOOK = "hooks/_levain_hook.py"


@pytest.fixture
def make_install(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])

    def _make(name: str = "entity", adapter: str = "claude-code") -> Path:
        from tests.test_init_answers import _filled

        af = tmp_path / f"{name}-answers.json"
        af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
        install = tmp_path / name
        assert run_init(install, adapter, force=False, answers_file=af) == 0
        capsys.readouterr()
        return install

    return _make


def _pin_check(install: Path):
    (res,) = doctor._check_agent_hook_pins(install)
    return res


def test_init_then_doctor_has_no_hook_pin_fail(make_install):
    install = make_install()
    res = _pin_check(install)
    assert res.ok and not res.warn, res
    assert "match the pin" in res.detail


def test_update_that_rewrites_a_hook_script_does_not_fail(make_install):
    install = make_install()
    hook = install / "activation" / HOOK
    current = hook.read_bytes()
    # an older levain wrote this script: the receipt says so, the package has moved on
    hook.write_bytes(b"# old hook\n")
    from levain.install import activation_receipt_path
    import hashlib
    path = activation_receipt_path(install)
    data = json.loads(path.read_text())
    data["files"][HOOK]["installed"] = hashlib.sha256(b"# old hook\n").hexdigest()
    path.write_text(json.dumps(data))
    hg.repin_agent_hooks(install)  # the pin as it stood with the old script
    r = refresh_adapter(install, apply=True, emit=lambda _l: None)
    assert r.refreshed == [f"activation/{HOOK}"] and hook.read_bytes() == current
    res = _pin_check(install)
    assert res.ok and not res.warn, res


def test_a_hand_edit_after_init_still_fails(make_install):
    install = make_install()
    hook = install / "activation" / HOOK
    hook.write_bytes(hook.read_bytes() + b"# tampered\n")
    res = _pin_check(install)
    assert not res.ok and "changed since pinned" in res.detail


def test_an_update_never_blesses_an_earlier_edit(make_install):
    install = make_install()
    hook = install / "activation" / "posture.md"
    s = install / ".claude" / "settings.json"
    s.write_text(s.read_text() + "\n")  # not a hooks change; harmless
    other = install / ".claude" / "hooks"
    other.mkdir(exist_ok=True)
    (other / "evil.py").write_text("print('x')\n")  # an edit that is not levain's
    assert hg.agent_pin_status(install) == "changed"
    refresh_adapter(install, apply=True, emit=lambda _l: None)
    res = _pin_check(install)
    assert not res.ok and "changed since pinned" in res.detail


def test_reinit_force_after_a_doctor_pin_does_not_fail(make_install, tmp_path, capsys):
    install = make_install()
    assert _pin_check(install).ok
    # the operator's existing pin, then a legitimate re-init that rewrites the hook files
    (install / "activation" / HOOK).write_bytes(b"# stale\n")
    hg.repin_agent_hooks(install)
    from tests.test_init_answers import _filled

    af = tmp_path / "again.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    assert run_init(install, "claude-code", force=True, answers_file=af) == 0
    capsys.readouterr()
    res = _pin_check(install)
    assert res.ok and not res.warn and "match the pin" in res.detail, res


# diogenes-20261015-021021, reproduced through the real CLI: a Codex init in ANOTHER directory rewrites the one
# global Codex hooks.json, and that turned a Claude Code install's doctor red.
def test_a_codex_install_elsewhere_leaves_a_claude_code_install_green(make_install, tmp_path):
    cc = make_install("cc")
    make_install("cx", adapter="codex")
    assert (tmp_path / "codex-home" / "hooks.json").is_file()
    res = _pin_check(cc)
    assert res.ok and not res.warn, res
    assert hg.agent_pin_status(cc) == "match"


def test_a_codex_install_still_pins_the_global_codex_hooks(make_install, tmp_path):
    cx = make_install("cx", adapter="codex")
    assert _pin_check(cx).ok
    cj = tmp_path / "codex-home" / "hooks.json"
    doc = json.loads(cj.read_text(encoding="utf-8"))
    doc.setdefault("hooks", {}).setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command", "command": "/tmp/planted.sh"}]})
    cj.write_text(json.dumps(doc), encoding="utf-8")
    res = _pin_check(cx)
    assert not res.ok and "codex hooks.json" in res.detail, res
    # an init reads the status before AGENTS.md exists: a Codex init must still treat a hooks.json it did not
    # write as unvetted, while a Claude Code init ignores it
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert hg.agent_pin_status(fresh, adapter="codex") == "none"
    assert hg.agent_pin_status(fresh, adapter="claude-code") == "fresh"
    # a hookless (openhands) install switched to codex: its marker must not outrank the init's adapter (L3 r2, run)
    oh = make_install("oh", adapter="openhands")
    assert hg.agent_pin_status(oh, adapter="codex") == "none"
