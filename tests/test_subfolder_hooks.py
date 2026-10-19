"""Levain's Claude Code hooks live in the workspace's .claude/settings.local.json with absolute script paths, never
in its settings.json (1019 subfolder-hook fix, Option B): Claude Code reads the project settings.json only for a
session started in the workspace folder, and the local file at the git root, so only the local file reaches a
session started in a subfolder (measured: project_memory/1019-q10/run9, 1019-subhook/run11). init renders and merges
it, update migrates an install from before the move, doctor / verify / the resume gate check read it."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from levain import doctor
from levain import install as inst
from levain import verify
from levain.consent import delivery
from levain.install import refresh_adapter
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)

LOCAL = (".claude", "settings.local.json")


@pytest.fixture
def init_into(tmp_path, capsys, make_install):
    """`levain init --force` into a directory the test prepared (make_install's environment, which refuses a
    non-empty directory)."""
    def _init(name: str = "cc") -> Path:
        from tests.test_init_answers import _filled

        af = tmp_path / f"{name}-answers.json"
        af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
        install = tmp_path / name
        assert inst.run_init(install, "claude-code", force=True, answers_file=af) == 0
        capsys.readouterr()
        return install

    return _init


def _json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _commands(hooks: dict) -> list[str]:
    return [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]


def _refresh(install: Path, apply: bool = True):
    lines: list[str] = []
    out = refresh_adapter(install, apply=apply, emit=lines.append)
    return out, "\n".join(lines)


def _row(results, name: str):
    (r,) = [r for r in results if r.name == name]
    return r


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _to_old_layout(install: Path, *, extra_shared: dict | None = None) -> None:
    """Put the install back in the 0.11.x layout: levain's hooks in settings.json in the ${CLAUDE_PROJECT_DIR} form
    (the consent entries absolute, as 0.11.x wrote them), no local file, no receipt record of it."""
    shared = install / ".claude" / "settings.json"
    local = install.joinpath(*LOCAL)
    hooks = _json(local)["hooks"]
    text = json.dumps(hooks)
    for script in ("session_start", "user_prompt_submit", "automemory_mirror", "post_tool_use", "stop"):
        text = text.replace(json.dumps(str(install / "activation" / "hooks" / f"{script}.py"))[1:-1],
                            "${CLAUDE_PROJECT_DIR}/activation/hooks/" + script + ".py")
    doc = {**_json(shared), "hooks": json.loads(text), **(extra_shared or {})}
    shared.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    local.unlink()
    receipt = install / ".levain" / "adapter-receipt.json"
    rec = _json(receipt)
    rec["files"].pop(inst.CC_LOCAL_HOOKS_KEY, None)
    rec["files"][".claude/settings.json"] = hashlib.sha256(shared.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(rec), encoding="utf-8")


# --- the render --------------------------------------------------------------------------------------------------

def test_init_writes_hooks_to_the_local_file_with_absolute_paths(make_install):
    cc = make_install("cc")
    assert "hooks" not in _json(cc / ".claude" / "settings.json")
    hooks = _json(cc.joinpath(*LOCAL))["hooks"]
    assert {"SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "PermissionRequest",
            "PreToolUse"} <= set(hooks)
    cmds = _commands(hooks)
    assert cmds and not [c for c in cmds if "CLAUDE_PROJECT_DIR" in c]
    assert all(doctor.levain_hook_handler({"command": c}, cc) for c in cmds)
    assert any(str(cc / "activation" / "hooks" / "session_start.py") in c for c in cmds)
    # recorded, so a later update can tell levain's entries from the operator's edit of them
    assert inst.CC_LOCAL_HOOKS_KEY in _json(cc / ".levain" / "adapter-receipt.json")["files"]


def test_the_template_keeps_the_placeholder_host_guard_reads(make_install):
    # host_guard.cc_hook_settings parses the TEMPLATE's ${CLAUDE_PROJECT_DIR} command shape; the render fills it
    from levain import host_guard as hg

    cc = make_install("cc")
    out = hg.cc_hook_settings(cc, "/usr/bin/python3", cc / ".pyc")
    assert str(cc / "activation" / "hooks" / "session_start.py") in json.dumps(out)


def test_init_merges_into_the_users_local_file(tmp_path, init_into):
    (tmp_path / "cc" / ".claude").mkdir(parents=True)
    user_hook = {"type": "command", "command": "/bin/echo mine"}
    team = {"type": "command", "command": "/usr/bin/python3 -P -m levain.team.hook sessionstart"}
    (tmp_path / "cc").joinpath(*LOCAL).write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [user_hook, team]}],
                  "Notification": [{"hooks": [user_hook]}]}}), encoding="utf-8")
    cc = init_into("cc")
    doc = _json(cc.joinpath(*LOCAL))
    assert doc["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert doc["hooks"]["Notification"] == [{"hooks": [user_hook]}]
    ss = doc["hooks"]["SessionStart"]
    assert ss[0] == {"matcher": "startup", "hooks": [user_hook, team]}  # the user's first, untouched
    assert len(ss) == 2 and doctor.levain_hook_handler(ss[1]["hooks"][0], cc)


def test_ownership_is_this_installs_activation_scripts_only(tmp_path):
    install = tmp_path / "ws"
    (install / "activation" / "hooks").mkdir(parents=True)
    own = doctor.levain_hook_handler
    assert own({"command": f'"/py" "{install}/activation/hooks/session_start.py"'}, install)
    assert own({"command": '"/py" "${CLAUDE_PROJECT_DIR}/activation/hooks/stop.py"'}, install)
    assert own({"command": f"/py -EP -m levain.hookrun {install}/activation/hooks/consent_claude_code.py"}, install)
    assert not own({"command": f'"/py" "{tmp_path}/other/activation/hooks/session_start.py"'}, install)
    assert not own({"command": f'"/py" "{install}-x/activation/hooks/session_start.py"'}, install)
    assert not own({"command": "/py -P -m levain.team.hook sessionstart"}, install)
    assert not own({"command": "session_start.py"}, install)
    assert not own({"command": 'unterminated "quote'}, install)


# --- git ignore --------------------------------------------------------------------------------------------------

def test_a_git_workspace_ignores_the_local_file(tmp_path, init_into):
    (tmp_path / "cc").mkdir()
    assert _git("-C", str(tmp_path / "cc"), "init", "-q").returncode == 0
    cc = init_into("cc")
    assert ".claude/.gitignore" in _git("-C", str(cc), "check-ignore", "-v", ".claude/settings.local.json").stdout
    assert (cc / ".claude" / ".gitignore").read_text().splitlines() == ["settings.local.json"]


def test_an_already_ignored_local_file_adds_nothing(tmp_path, init_into):
    (tmp_path / "cc").mkdir()
    assert _git("-C", str(tmp_path / "cc"), "init", "-q").returncode == 0
    (tmp_path / "cc" / ".gitignore").write_text("settings.local.json\n")
    cc = init_into("cc")
    assert not (cc / ".claude" / ".gitignore").exists()


def test_outside_git_no_gitignore_is_written(make_install):
    cc = make_install("cc")
    if _git("-C", str(cc), "rev-parse", "--is-inside-work-tree").returncode == 0:
        return  # tmp is inside some repo on this machine: the case is not available here
    assert not (cc / ".claude" / ".gitignore").exists()


# --- update: the migration ---------------------------------------------------------------------------------------

def test_update_migrates_an_unedited_old_layout(make_install):
    cc = make_install("cc")
    _to_old_layout(cc)
    out, text = _refresh(cc)
    assert "hooks" not in _json(cc / ".claude" / "settings.json")
    hooks = _json(cc.joinpath(*LOCAL))["hooks"]
    assert hooks["SessionStart"] and not [c for c in _commands(hooks) if "CLAUDE_PROJECT_DIR" in c]
    assert ".claude/settings.local.json" not in out.review
    # settled: the next update leaves both files alone
    before = (cc / ".claude" / "settings.json").read_bytes(), cc.joinpath(*LOCAL).read_bytes()
    out2, text2 = _refresh(cc)
    assert not [r for r in out2.refreshed if "settings" in r], text2
    assert ((cc / ".claude" / "settings.json").read_bytes(), cc.joinpath(*LOCAL).read_bytes()) == before


def test_update_strips_levain_hooks_from_an_edited_settings_json(make_install):
    cc = make_install("cc")
    mine = {"type": "command", "command": "/bin/echo mine"}
    _to_old_layout(cc, extra_shared={"env": {"FOO": "1"}})
    shared = cc / ".claude" / "settings.json"
    doc = _json(shared)
    doc["hooks"]["SessionStart"][0]["hooks"].append(mine)  # the operator's own hook in levain's group
    shared.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")  # now an edit: no record matches
    out, text = _refresh(cc)
    after = _json(shared)
    assert after["env"] == {"FOO": "1"}
    assert after["hooks"] == {"SessionStart": [{"matcher": "startup|resume|clear|compact", "hooks": [mine]}]}
    assert "levain's hooks removed" in text
    assert list((cc / ".claude").glob("settings.json.bak.*"))
    assert _json(cc.joinpath(*LOCAL))["hooks"]["UserPromptSubmit"]


def test_dry_run_changes_nothing(make_install):
    cc = make_install("cc")
    _to_old_layout(cc)
    before = (cc / ".claude" / "settings.json").read_bytes()
    out, _ = _refresh(cc, apply=False)
    assert (cc / ".claude" / "settings.json").read_bytes() == before
    assert not cc.joinpath(*LOCAL).exists()
    assert any("settings.local.json" in r for r in out.refreshed)


def test_an_edited_levain_entry_the_package_also_moved_is_staged(make_install):
    cc = make_install("cc")
    local = cc.joinpath(*LOCAL)
    doc = _json(local)
    doc["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 5  # the operator's edit of levain's own entry
    local.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    receipt = cc / ".levain" / "adapter-receipt.json"
    rec = _json(receipt)
    rec["files"][inst.CC_LOCAL_HOOKS_KEY] = "0" * 64  # the package rendered something else last time
    receipt.write_text(json.dumps(rec), encoding="utf-8")
    edited = local.read_bytes()
    out, text = _refresh(cc)
    assert ".claude/settings.local.json" in out.review
    assert local.read_bytes() == edited
    staged = cc / ".levain" / "pending" / ".claude" / "settings.local.json"
    assert "timeout" not in json.dumps(_json(staged)["hooks"]["Stop"])
    # listed once
    out2, _ = _refresh(cc)
    assert ".claude/settings.local.json" not in out2.review


def test_an_unreadable_local_file_keeps_settings_json_hooks(make_install):
    cc = make_install("cc")
    _to_old_layout(cc)
    cc.joinpath(*LOCAL).write_text("{not json", encoding="utf-8")
    out, text = _refresh(cc)
    assert ".claude/settings.local.json" in out.review
    assert cc.joinpath(*LOCAL).read_text() == "{not json"
    # nothing would run levain's hooks if settings.json lost them now
    assert _json(cc / ".claude" / "settings.json").get("hooks")


# --- doctor ------------------------------------------------------------------------------------------------------

def test_doctor_reads_the_local_file(make_install):
    cc = make_install("cc")
    res = doctor._check_claude_code(cc)
    assert _row(res, ".claude/settings.local.json hooks").ok
    assert not [r for r in res if r.name == "levain hooks location"]
    assert not [r for r in res if r.name == "levain hook paths"]


def test_doctor_warns_on_the_old_layout(make_install):
    cc = make_install("cc")
    _to_old_layout(cc)
    res = doctor._check_claude_code(cc)
    moved = _row(res, "levain hooks location")
    assert moved.warn and "subfolder" in moved.detail and "levain update" in moved.hint
    paths = _row(res, "levain hook paths")
    assert paths.warn and "SessionStart" in paths.detail and "levain update" in paths.hint


def test_doctor_warns_on_hooks_left_in_settings_json(make_install):
    cc = make_install("cc")
    shared = cc / ".claude" / "settings.json"
    doc = _json(shared)
    doc["hooks"] = {"Stop": _json(cc.joinpath(*LOCAL))["hooks"]["Stop"]}
    shared.write_text(json.dumps(doc), encoding="utf-8")
    res = doctor._check_claude_code(cc)
    assert _row(res, ".claude/settings.local.json hooks").ok
    left = [r for r in res if r.name == "levain hooks location"]
    assert left and left[0].warn and "twice" in left[0].detail and "levain update" in left[0].hint


# --- the other readers -------------------------------------------------------------------------------------------

def test_verify_takes_the_launcher_from_the_local_file(make_install):
    cc = make_install("cc")
    launcher = verify._resolve_install_launcher(cc)
    local_ss = _json(cc.joinpath(*LOCAL))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert local_ss.startswith(json.dumps(launcher[0]))


def test_resume_gate_check_finds_the_gate_in_the_local_file(tmp_path, make_install):
    cc = make_install("cc")
    assert delivery.gate_refusal(str(cc), str(tmp_path / "cfg")) is None
    cc.joinpath(*LOCAL).unlink()
    assert delivery.gate_refusal(str(cc), str(tmp_path / "cfg")) == "no levain gate is installed in the settings it would load"
