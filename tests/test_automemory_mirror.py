"""The auto-memory mirror (activation/hooks/automemory_mirror.py), run against a real anneal
store the way an install runs it: the hook scripts copied into an install tree, the sweep and
the hook invoked as subprocesses on this interpreter. Each test reproduces a run made while
building 0.5.10."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the mirror uses fcntl")

_HOOKS = Path(__file__).resolve().parent.parent / "levain" / "templates" / "activation" / "hooks"


def _folder(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


@pytest.fixture
def inst(tmp_path):
    root = (tmp_path / "install").resolve()
    shutil.copytree(_HOOKS, root / "activation" / "hooks")
    (root / ".levain").mkdir(parents=True)
    cfg = tmp_path / "claude-config"
    mem = cfg / "projects" / _folder(root) / "memory"
    mem.mkdir(parents=True)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(cfg)}
    for k in ("LEVAIN_AUTOMEMORY_MIRROR", "LEVAIN_HOOK_SUPPRESS", "LEVAIN_SCOPE"):
        env.pop(k, None)
    anneal(root, env, "init")
    return root, mem, env


def anneal(root: Path, env: dict, *args: str, stdin: str | None = None) -> str:
    r = subprocess.run([sys.executable, "-P", "-m", "anneal_memory", "--db",
                        str(root / ".levain" / "memory.db"), *args],
                       input=stdin, capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def sweep(root: Path, env: dict, *extra: str) -> dict:
    r = subprocess.run([sys.executable, str(root / "activation" / "hooks" / "automemory_mirror.py"),
                        "sweep", *extra], capture_output=True, text=True, env=env, cwd=root,
                       timeout=120)
    return json.loads(r.stdout)


def mirrored(root: Path, env: dict, superseded: bool = False) -> list[dict]:
    args = ["--json", "episodes", "--source", "automemory-mirror", "--limit", "100"]
    if superseded:
        args.append("--include-superseded")
    return json.loads(anneal(root, env, *args))["episodes"]


def test_first_sweep_baselines_then_hook_mirrors_a_new_note(inst):
    root, mem, env = inst
    (mem / "MEMORY.md").write_text("# index\n")
    (mem / "old.md").write_text("Existing note before the mirror.\n")
    counts = sweep(root, env)
    assert counts.get("baselined") == 1 and mirrored(root, env) == []

    (mem / "new.md").write_text("Operator shorthand: '->' means next step.\n")
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(mem / "new.md")}})
    r = subprocess.run([sys.executable, str(root / "activation" / "hooks" / "automemory_mirror.py"),
                        "hook"], input=payload, capture_output=True, text=True, env=env,
                       cwd=root, timeout=30)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    deadline = time.monotonic() + 30
    while not mirrored(root, env) and time.monotonic() < deadline:
        time.sleep(0.5)
    eps = mirrored(root, env)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["type"] == "decision"
    assert ep["content"].startswith("OPERATOR RULE (auto-memory mirror): memory/new.md")
    assert {"operator-rule", "automemory", "mirror"} <= set(ep["metadata"]["tags"])


def test_edit_delete_recreate_leave_only_the_current_note_visible(inst):
    root, mem, env = inst
    sweep(root, env)                                        # baseline (empty)
    note = mem / "b.md"
    note.write_text("Operator shorthand: '->' means next step.\n")
    assert sweep(root, env)["new"] == 1
    note.write_text("Never deploy on Fridays; ask first.\n")
    assert sweep(root, env)["changed"] == 1
    visible = mirrored(root, env)
    assert len(visible) == 1 and visible[0]["content"].startswith("OPERATOR RULE, REVISED")
    note.unlink()
    assert sweep(root, env)["deleted"] == 1
    visible = mirrored(root, env)
    assert len(visible) == 1 and visible[0]["content"].startswith("RETRACTED OPERATOR RULE")
    note.write_text("Fridays are fine now.\n")
    assert sweep(root, env)["new"] == 1
    visible = mirrored(root, env)
    assert len(visible) == 1 and visible[0]["content"].rstrip().endswith("Fridays are fine now.")
    assert len(mirrored(root, env, superseded=True)) == 4
    (mem / "MEMORY.md").write_text("# index changed\n")
    assert sweep(root, env)["new"] == 0


def test_a_full_rewrite_anneal_will_not_ground_is_retried_with_the_earlier_text(inst):
    root, mem, env = inst
    sweep(root, env)
    note = mem / "c.md"
    note.write_text(" ".join(f"alpha{i}word" for i in range(150)))
    assert sweep(root, env)["new"] == 1
    note.write_text(" ".join(f"omega{i}term" for i in range(150)))
    assert sweep(root, env)["changed"] == 1
    visible = mirrored(root, env)
    assert len(visible) == 1
    assert "It replaces this earlier text:" in visible[0]["content"]


def test_lost_state_stops_the_mirror_and_doctor_reports_it(inst):
    root, mem, env = inst
    sweep(root, env)
    (mem / "d.md").write_text("A rule.\n")
    assert sweep(root, env)["new"] == 1
    (root / ".levain" / "automemory_mirror.json").unlink()
    (mem / "d.md").write_text("A rule, edited.\n")
    assert sweep(root, env).get("state_lost") == 1
    assert (root / ".levain" / "automemory_mirror.lost").exists()
    assert sweep(root, env).get("state_lost") == 1          # stays stopped
    assert len(mirrored(root, env, superseded=True)) == 1

    from levain.doctor import _check_automemory_mirror
    hooks = {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command":
             f'"{sys.executable}" "${{CLAUDE_PROJECT_DIR}}/activation/hooks/automemory_mirror.py" hook'}]}]}
    (res,) = _check_automemory_mirror(root, hooks)
    assert res.ok and "STOPPED" in res.detail


def test_opt_out_by_env_or_config(inst):
    root, mem, env = inst
    assert "disabled" in sweep(root, {**env, "LEVAIN_AUTOMEMORY_MIRROR": "off"})
    (root / ".levain" / "config.json").write_text(json.dumps({"automemory_mirror": False}))
    assert "disabled" in sweep(root, env)
    assert not (root / ".levain" / "automemory_mirror.json").exists()
    assert "disabled" not in sweep(root, {**env, "LEVAIN_AUTOMEMORY_MIRROR": "on"})


def test_auto_memory_directory_from_settings_is_mirrored_and_baselined_when_it_appears(inst):
    root, mem, env = inst
    sweep(root, env)                                        # baseline of the default folder
    custom = root / ".automem"
    custom.mkdir()
    (custom / "existing.md").write_text("Written before the setting was seen.\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(
        json.dumps({"autoMemoryDirectory": str(custom)}))
    counts = sweep(root, env)
    assert counts.get("baselined") == 1 and counts["new"] == 0
    (custom / "rule.md").write_text("Operator: never squash merge.\n")
    assert sweep(root, env)["new"] == 1
    assert [e["content"].split(":")[1].split("(")[0].strip()
            for e in mirrored(root, env)] == ["memory/rule.md"]
