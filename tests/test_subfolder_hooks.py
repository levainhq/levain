"""Levain's Claude Code hooks live in the workspace's .claude/settings.local.json with absolute script paths when
the workspace is the toplevel of a main checkout, else in its settings.json (1019 subfolder-hook fix, Option B;
host_guard.cc_levain_hooks_file): Claude Code reads the project settings.json only for a session started in the
workspace folder, and the local file at the git root, so only the local file reaches a session started in a
subfolder (measured: project_memory/1019-q10/run9, 1019-subhook/run11). init renders and merges it, update migrates
between the layouts, doctor / verify / the resume gate check read it."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard
from levain import install as inst
from levain import verify
from levain.consent import delivery
from levain.install import refresh_adapter
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)

LOCAL = (".claude", "settings.local.json")
REAL_LAYOUT = host_guard.cc_levain_hooks_file


@pytest.fixture
def cc_local_layout(monkeypatch):
    """A test workspace counts as the toplevel of a main checkout for host_guard.cc_levain_hooks_file (tmp_path is
    not in git, which gets the settings.json layout): the local layout's tests. The layout decision itself is tested
    with real git below (test_layout_*), with REAL_LAYOUT."""
    monkeypatch.setattr(host_guard, "cc_levain_hooks_file",
                        lambda ws: Path(os.path.abspath(ws)) / ".claude" / "settings.local.json")


pytestmark = pytest.mark.usefixtures("cc_local_layout")


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


# --- fix r1 (b3/subhook_FIX_r1.md) -------------------------------------------------------------------------------

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main")


def _g(*args: str) -> None:
    subprocess.run([*GIT, *args], check=True, capture_output=True, text=True)


def _init_at(install: Path, tmp_path: Path, capsys, *, force: bool = True) -> tuple[int, str]:
    from tests.test_init_answers import _filled

    af = tmp_path / f"{install.name}-{abs(hash(str(install)))}-answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    rc = inst.run_init(install, "claude-code", force=force, answers_file=af)
    return rc, capsys.readouterr().out


def _levain_cmds(doc: dict, install: Path) -> list[str]:
    return [h["command"] for gs in (doc.get("hooks") or {}).values() for g in gs for h in g["hooks"]
            if inst._levain_handler(h, install)]


@pytest.fixture
def real_layout(monkeypatch, make_install):
    """host_guard.cc_levain_hooks_file as shipped, asking git (make_install: scratch HOME / CODEX_HOME)."""
    monkeypatch.setattr(host_guard, "cc_levain_hooks_file", REAL_LAYOUT)


def _assert_layout(install: Path, *, local: bool, out: str) -> None:
    shared, lfile = install / ".claude" / "settings.json", install.joinpath(*LOCAL)
    assert (REAL_LAYOUT(install) == lfile) is local
    if local:
        assert "hooks" not in _json(shared)
        assert _levain_cmds(_json(lfile), install)
        assert host_guard.CC_HOOKS_ROOT_ONLY not in out
    else:
        assert _levain_cmds(_json(shared), install)
        assert not lfile.exists() or not _levain_cmds(_json(lfile), install)
        assert host_guard.CC_HOOKS_ROOT_ONLY in out
        res = doctor._check_claude_code(install)
        assert _row(res, "levain hooks coverage").detail == host_guard.CC_HOOKS_ROOT_ONLY
        assert _row(res, ".claude/settings.json hooks").ok
        assert not [r for r in res if r.name == "levain hooks location"]


def test_layout_s0_git_root_workspace_uses_the_local_file(tmp_path, capsys, real_layout):
    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    _assert_layout(ws, local=True, out=out)


def test_layout_s0_nested_workspace_keeps_settings_json(tmp_path, capsys, real_layout):
    _g("init", "-q", str(tmp_path / "outer"))
    ws = tmp_path / "outer" / "ws"
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    _assert_layout(ws, local=False, out=out)
    # never into the parent repo's settings
    assert not (tmp_path / "outer" / ".claude").exists()


def test_layout_s0_linked_worktree_keeps_settings_json(tmp_path, capsys, real_layout):
    main = tmp_path / "main"
    _g("init", "-q", str(main))
    _g("-C", str(main), "commit", "-q", "--allow-empty", "-m", "x")
    ws = tmp_path / "wt"
    _g("-C", str(main), "worktree", "add", "-q", str(ws))
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    _assert_layout(ws, local=False, out=out)
    assert not (main / ".claude").exists()  # never into the main checkout's settings


def test_layout_s0_non_git_workspace_keeps_settings_json(tmp_path, capsys, real_layout):
    ws = tmp_path / "plain"
    rc, out = _init_at(ws, tmp_path, capsys, force=False)
    assert rc == 0, out
    _assert_layout(ws, local=False, out=out)


def test_layout_s0_update_migrates_both_ways_when_the_decision_flips(tmp_path, capsys, real_layout):
    import shutil

    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    local_cmds = sorted(_levain_cmds(_json(ws.joinpath(*LOCAL)), ws))
    shutil.rmtree(ws / ".git")  # no longer its repo's root: the hooks go back to settings.json
    r, text = _refresh(ws)
    assert ".claude/settings.local.json (levain's hooks moved to settings.json)" in r.refreshed, text
    assert _levain_cmds(_json(ws / ".claude" / "settings.json"), ws)
    assert not _levain_cmds(_json(ws.joinpath(*LOCAL)), ws)
    assert inst.CC_LOCAL_HOOKS_KEY not in _json(ws / ".levain" / "adapter-receipt.json")["files"]
    assert _refresh(ws)[0].refreshed == []  # settled
    _g("init", "-q", str(ws))  # a git root again: back to the local file, written fresh (not read as a deletion)
    r, text = _refresh(ws)
    assert ".claude/settings.local.json (levain's hooks)" in r.refreshed, text
    assert sorted(_levain_cmds(_json(ws.joinpath(*LOCAL)), ws)) == local_cmds
    assert "hooks" not in _json(ws / ".claude" / "settings.json")


def test_s1_aliased_settings_files_are_never_split_or_migrated(tmp_path, capsys, make_install):
    cc = make_install("cc")
    shared, lfile = cc / ".claude" / "settings.json", cc.joinpath(*LOCAL)
    merged = {**_json(shared), "hooks": _json(lfile)["hooks"]}
    for link, target in ((lfile, shared), (shared, lfile)):
        for p in (shared, lfile):
            p.unlink(missing_ok=True)
        target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        link.symlink_to(target.name)
        before = target.read_bytes()
        r, text = _refresh(cc)
        assert ".claude/settings.json" in r.review and "one file (a link)" in text, text
        assert link.is_symlink() and target.read_bytes() == before and _levain_cmds(_json(target), cc)
        res = doctor._check_claude_code(cc)
        assert _row(res, "levain hooks location").warn
        rc, out = _init_at(cc, tmp_path, capsys)
        assert rc == 1 and "one file (a link)" in out, out
        assert link.is_symlink() and target.read_bytes() == before


def test_s2_a_moved_workspace_s_stale_hooks_are_replaced(tmp_path, capsys, make_install):
    import os as _os

    cc = make_install("cc")
    moved = tmp_path / "moved"
    _os.rename(cc, moved)
    stale = _levain_cmds(_json(moved.joinpath(*LOCAL)), moved)
    assert stale and all(str(cc) in c for c in stale)  # claimed as levain's: stale_levain_handler
    row = _row(doctor._check_claude_code(moved), "levain hook scripts")
    assert row.warn and "a levain hook names a missing script (moved workspace?)" in row.detail
    assert "levain update" in row.hint
    r, text = _refresh(moved)
    cmds = _commands(_json(moved.joinpath(*LOCAL))["hooks"])
    assert not [c for c in cmds if f"{cc}/" in c], cmds
    assert any(str(moved / "activation" / "hooks" / "session_start.py") in c for c in cmds)
    assert not [r for r in doctor._check_claude_code(moved) if r.name == "levain hook scripts"]
    # init --force replaces them too
    other = tmp_path / "other"
    _os.rename(moved, other)
    rc, out = _init_at(other, tmp_path, capsys)
    assert rc == 0, out
    hooks = _json(other.joinpath(*LOCAL))["hooks"]
    assert not [c for c in _commands(hooks) if f"{moved}/" in c]
    for event, groups in hooks.items():  # one set, no stale copy beside it
        per = [h["command"] for g in groups for h in g["hooks"]]
        assert len(per) == len(set(per)), (event, per)


def test_s2_a_foreign_script_that_exists_is_not_claimed(tmp_path):
    from levain.doctor import stale_levain_handler

    there = tmp_path / "x" / "activation" / "hooks" / "stop.py"
    there.parent.mkdir(parents=True)
    there.write_text("", encoding="utf-8")
    assert stale_levain_handler({"command": f"/usr/bin/python3 {there}"}) is None
    assert stale_levain_handler({"command": f"/usr/bin/python3 {tmp_path}/gone/activation/hooks/stop.py"})
    assert stale_levain_handler({"command": f"/usr/bin/python3 {tmp_path}/gone/activation/hooks/mine.py"}) is None
    assert stale_levain_handler({"command": '"/p" "${CLAUDE_PROJECT_DIR}/activation/hooks/stop.py"'}) is None


def test_s2_levain_hook_scripts_match_the_package():
    with inst._templates_root() as root:
        shipped = {p.name for p in (root / "activation" / "hooks").glob("*.py") if not p.name.startswith("_")}
    assert doctor.LEVAIN_HOOK_SCRIPTS == shipped | set(inst.CONSENT_HOOK_SCRIPTS.values())


def test_s3_doctor_finds_levain_s_handler_after_the_user_s(make_install):
    cc = make_install("cc")
    lfile = cc.joinpath(*LOCAL)
    doc = _json(lfile)
    for event in ("SessionStart", "UserPromptSubmit"):
        doc["hooks"][event] = [{"hooks": [{"type": "command", "command": "/bin/echo mine"}]}, *doc["hooks"][event]]
    lfile.write_text(json.dumps(doc), encoding="utf-8")
    res = doctor._check_claude_code(cc)
    for event, script in (("SessionStart", "session_start.py"), ("UserPromptSubmit", "user_prompt_submit.py")):
        r = _row(res, f"settings {event} → {script}")
        assert r.ok and "wired to this install" in r.detail, r


@pytest.mark.parametrize("bad", [{"SessionStart": 7}, {"Stop": "x", "SessionStart": []}])
def test_s4_init_refuses_a_local_file_with_a_non_list_event_before_writing(tmp_path, capsys, make_install, bad):
    ws = tmp_path / "cc"
    (ws / ".claude").mkdir(parents=True)
    text = json.dumps({"hooks": bad})
    ws.joinpath(*LOCAL).write_text(text, encoding="utf-8")
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 1 and "could not take levain's hooks" in out and "not lists" in out, out
    assert ws.joinpath(*LOCAL).read_text(encoding="utf-8") == text
    assert not (ws / ".claude" / "settings.json").exists() and not (ws / "activation").exists()


def test_s4_update_lists_a_local_file_with_a_non_list_event_and_writes_nothing(make_install):
    cc = make_install("cc")
    lfile, shared = cc.joinpath(*LOCAL), cc / ".claude" / "settings.json"
    doc = _json(lfile)
    doc["hooks"]["SessionStart"] = 7
    lfile.write_text(json.dumps(doc), encoding="utf-8")
    before, shared_before = lfile.read_bytes(), shared.read_bytes()
    r, text = _refresh(cc)
    assert ".claude/settings.local.json" in r.review and "not lists" in text, text
    assert lfile.read_bytes() == before and shared.read_bytes() == shared_before


# --- fix r2 (b4/subhook_FIX_r2.md) -------------------------------------------------------------------------------

def _nested_ws(tmp_path: Path) -> Path:
    """A workspace nested in a bigger repo: the settings.json layout, by git's answer (not a monkeypatch)."""
    _g("init", "-q", str(tmp_path / "outer"))
    ws = tmp_path / "outer" / "ws"
    (ws / ".claude").mkdir(parents=True)
    return ws


def test_r2_t1_shared_layout_leaves_a_broken_local_file_alone(tmp_path, capsys, real_layout):
    ws = _nested_ws(tmp_path)
    lfile = ws.joinpath(*LOCAL)
    lfile.write_bytes(b"{not json")
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    assert lfile.read_bytes() == b"{not json"
    assert "left as it is (levain could not read it" in out and "holds levain's hooks" not in out, out
    assert _levain_cmds(_json(ws / ".claude" / "settings.json"), ws)
    r, text = _refresh(ws)
    assert "/".join(LOCAL) not in r.review and "holds levain's hooks" not in text, text
    assert "left as it is (levain could not read it" in text
    assert lfile.read_bytes() == b"{not json"
    row = _row(doctor._check_claude_code(ws), "/".join(LOCAL))
    assert row.ok and row.warn  # a WARN in this layout, never a FAIL


def test_r2_t1_shared_layout_still_refuses_a_parsing_file_with_levain_hooks_it_cannot_rewrite(
        tmp_path, capsys, real_layout):
    ws = _nested_ws(tmp_path)
    mine = {"type": "command", "command": f'"/py" "{ws}/activation/hooks/session_start.py"'}
    text = json.dumps({"hooks": {"Stop": "x", "SessionStart": [{"hooks": [mine]}]}})
    ws.joinpath(*LOCAL).write_text(text, encoding="utf-8")
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 1 and "holds levain's hooks" in out, out
    assert ws.joinpath(*LOCAL).read_text(encoding="utf-8") == text


def test_r2_t2_the_gitignore_step_asks_the_workspace_s_git_not_the_caller_s(tmp_path, monkeypatch):
    other = tmp_path / "other"
    _g("init", "-q", str(other))
    (other / ".git" / "info" / "exclude").write_text(".claude/settings.local.json\n", encoding="utf-8")
    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    (ws / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no global excludes (Claude Code adds the file to them)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # a caller's repository (a git hook, an alias)
    assert "GIT_DIR" not in host_guard.git_env()
    line = inst._ensure_local_settings_ignored(ws)
    assert line and "added to .claude/.gitignore" in line, line
    assert (ws / ".claude" / ".gitignore").read_text().splitlines() == ["settings.local.json"]


def test_r2_t3_the_root_only_line_names_no_cause():
    assert "git" not in host_guard.CC_HOOKS_ROOT_ONLY
    assert host_guard.CC_HOOKS_ROOT_ONLY.startswith(
        "Claude Code reads levain's hooks only when it starts at the workspace folder itself")


@pytest.mark.parametrize("version,warns", [("2.1.210", True), ("2.1.211", False), ("2.1.304", False), (None, False)])
def test_r2_t4_doctor_warns_on_a_claude_code_too_old_for_the_root_local_file(make_install, monkeypatch, version,
                                                                             warns):
    from levain.consent import canary

    cc = make_install("cc")
    monkeypatch.setattr(canary, "agent_version", lambda agent, **_: version if agent == "claude_code" else None)
    rows = [r for r in doctor._check_claude_code(cc) if r.name == "levain hooks: Claude Code version"]
    if warns:
        (row,) = rows
        assert row.ok and row.warn and "2.1.210" in row.detail and "2.1.211" in row.hint
    else:
        assert not rows


def test_r2_t4_no_version_row_in_the_settings_json_layout(tmp_path, capsys, real_layout, monkeypatch):
    from levain.consent import canary

    ws = _nested_ws(tmp_path)
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    monkeypatch.setattr(canary, "agent_version", lambda agent, **_: "2.1.100")
    assert not [r for r in doctor._check_claude_code(ws) if r.name == "levain hooks: Claude Code version"]


def test_r2_t5_doctor_warns_when_git_tracks_the_local_file(tmp_path, capsys, real_layout):
    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    rc, out = _init_at(ws, tmp_path, capsys)
    assert rc == 0, out
    assert not [r for r in doctor._check_claude_code(ws) if r.name == "levain hooks file tracked"]
    _g("-C", str(ws), "add", "-f", "/".join(LOCAL))
    row = _row(doctor._check_claude_code(ws), "levain hooks file tracked")
    assert row.ok and row.warn and "git rm --cached" in row.hint


def test_r2_t6_a_claude_entry_not_owned_by_the_user_keeps_the_local_file_beside(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    (ws / ".claude").mkdir()
    (ws / "sub").mkdir()
    assert host_guard.cc_project_settings_files(ws / "sub")[0][0] == ws.joinpath(*LOCAL)
    assert REAL_LAYOUT(ws) == ws.joinpath(*LOCAL)
    real_owned = host_guard._owned
    monkeypatch.setattr(host_guard, "_owned", lambda p: Path(p).name != ".claude" and real_owned(p))
    assert host_guard.cc_project_settings_files(ws / "sub")[0] == [ws / "sub" / ".claude" / "settings.local.json"]
    assert REAL_LAYOUT(ws) == ws / ".claude" / "settings.json"


def test_r2_t7_the_local_file_is_replaced_whole_and_keeps_its_mode(tmp_path, init_into):
    (tmp_path / "cc" / ".claude").mkdir(parents=True)
    lfile = (tmp_path / "cc").joinpath(*LOCAL)
    lfile.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}), encoding="utf-8")
    os.chmod(lfile, 0o640)
    ino = os.stat(lfile).st_ino
    cc = init_into("cc")
    assert _levain_cmds(_json(lfile), cc)
    st = os.stat(lfile)
    assert st.st_ino != ino and (st.st_mode & 0o777) == 0o640  # temp file + os.replace, mode carried
    assert not [p.name for p in (cc / ".claude").iterdir() if p.name.endswith(".tmp")]


# --- fix r3 (b4/subhook_FIX_r3.md) -------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [{"hooks": 7}, {"hooks": [{"type": "command", "command": 5}]},
                                  {"hooks": [7]}, "x"])
def test_r3_u1_a_malformed_group_after_a_valid_one_is_not_a_crash(make_install, junk):
    cc = make_install("cc")
    lfile = cc.joinpath(*LOCAL)
    doc = _json(lfile)
    for event in ("SessionStart", "UserPromptSubmit"):
        mine = {"hooks": [{"type": "command", "command": "/bin/echo mine"}]}
        doc["hooks"][event] = [mine, junk, *doc["hooks"][event]]
    lfile.write_text(json.dumps(doc), encoding="utf-8")
    res = doctor._check_claude_code(cc)
    for event, script in (("SessionStart", "session_start.py"), ("UserPromptSubmit", "user_prompt_submit.py")):
        r = _row(res, f"settings {event} → {script}")
        assert r.ok and "wired to this install" in r.detail, r


def test_r3_u1_the_narrowed_group_keeps_its_matcher(tmp_path):
    ws = tmp_path / "ws"
    (ws / "activation" / "hooks").mkdir(parents=True)
    script = ws / "activation" / "hooks" / "session_start.py"
    script.write_text("", encoding="utf-8")
    h = {"type": "command", "command": f'"/py" "{script}"'}
    entries = [{"hooks": [{"type": "command", "command": "/bin/echo mine"}]},
               {"matcher": "startup", "timeout": 9, "hooks": [{"type": "command", "command": "/bin/true"}, h]}]
    assert doctor._entries_for(entries, ws, "session_start.py") == [{"matcher": "startup", "timeout": 9,
                                                                      "hooks": [h]}]


def _activation_from_an_older_release(install: Path) -> tuple[Path, bytes]:
    """An unedited activation hook an older levain wrote, so `levain update` replaces it: (path, its bytes)."""
    rel = "hooks/_levain_hook.py"
    hook = install / "activation" / rel
    old = b"# old hook\n"
    hook.write_bytes(old)
    receipt = inst.activation_receipt_path(install)
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["files"][rel]["installed"] = hashlib.sha256(old).hexdigest()
    receipt.write_text(json.dumps(data), encoding="utf-8")
    return hook, old


def test_r3_u2_update_writes_nothing_when_the_local_file_stops_it(make_install):
    cc = make_install("cc")
    lfile, shared = cc.joinpath(*LOCAL), cc / ".claude" / "settings.json"
    hook, old = _activation_from_an_older_release(cc)
    doc = _json(lfile)
    doc["hooks"]["Stop"] = 7
    lfile.write_text(json.dumps(doc), encoding="utf-8")
    before = {p: p.read_bytes() for p in (lfile, shared, cc / "CLAUDE.md", inst.activation_receipt_path(cc))}
    r, text = _refresh(cc)
    assert "/".join(LOCAL) in r.review and not r.refreshed and "not lists" in text, text
    assert "NOT refreshed, none of them" in text, text
    assert hook.read_bytes() == old
    assert {p: p.read_bytes() for p in before} == before
    # the same install with the local file fixed: the activation hook is replaced (the setup was a real change)
    doc["hooks"].pop("Stop")
    lfile.write_text(json.dumps(doc), encoding="utf-8")
    r, text = _refresh(cc)
    assert "activation/hooks/_levain_hook.py" in r.refreshed and hook.read_bytes() != old, text


def test_r3_u2_update_writes_nothing_when_the_local_file_is_unreadable(make_install):
    cc = make_install("cc")
    hook, old = _activation_from_an_older_release(cc)
    cc.joinpath(*LOCAL).write_bytes(b"{not json")
    r, text = _refresh(cc)
    assert "/".join(LOCAL) in r.review and "could not be read" in text and hook.read_bytes() == old, text
    assert cc.joinpath(*LOCAL).read_bytes() == b"{not json"


def _stale_on(install: Path, event: str) -> None:
    """Point levain's ``event`` handlers at a workspace that no longer exists (a moved one's leftovers)."""
    lfile = install.joinpath(*LOCAL)
    doc = _json(lfile)
    doc["hooks"][event] = json.loads(json.dumps(doc["hooks"][event]).replace(str(install), "/nonexistent/old-ws"))
    lfile.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("event,blocks", [("SessionStart", False), ("UserPromptSubmit", True)])
def test_r3_u3_the_stale_row_names_the_event_and_blocks_only_for_userpromptsubmit(make_install, event, blocks):
    cc = make_install("cc")
    _stale_on(cc, event)
    row = _row(doctor._check_claude_code(cc), "levain hook scripts")
    assert row.warn and f"(on {event})" in row.detail, row.detail
    assert ("UserPromptSubmit" in row.detail) == blocks and ("blocks the prompt" in row.detail) == blocks, row.detail


def test_r3_u4_the_layout_git_calls_never_run_a_git_from_path(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _g("init", "-q", str(ws))
    (ws / ".claude").mkdir()
    planted = tmp_path / "bin"
    planted.mkdir()
    marker = tmp_path / "planted-git-ran"
    (planted / "git").write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 1\n", encoding="utf-8")
    (planted / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted}{os.pathsep}{os.environ.get('PATH', '')}")
    assert REAL_LAYOUT(ws) == ws.joinpath(*LOCAL)  # the fixed git answered
    inst._ensure_local_settings_ignored(ws)
    doctor._cc_local_layout_rows(ws)
    assert not marker.exists()
    # no git at a fixed path: what each call does when git fails
    monkeypatch.setattr(host_guard, "_GIT_BINARY", [None])
    assert REAL_LAYOUT(ws) == ws / ".claude" / "settings.json"
    assert inst._ensure_local_settings_ignored(ws) is None
    assert not [r for r in doctor._cc_local_layout_rows(ws) if r.name == "levain hooks file tracked"]
    assert not marker.exists()


def test_r3_u5_doctor_asks_claude_code_its_version_once_per_run(make_install, tmp_path, monkeypatch, capsys):
    cc = make_install("cc")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    count = tmp_path / "version-reads"
    (fake / "claude").write_text(f"#!/bin/sh\necho x >> '{count}'\necho '2.1.300 (Claude Code)'\n",
                                 encoding="utf-8")
    (fake / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    doctor.run_doctor(cc)
    capsys.readouterr()
    assert count.read_text(encoding="utf-8").count("x") == 1
