"""The per-session registry the hooks write for the cockpit's Sessions panel (Q7a, lane A):
`<install>/.levain/sessions/<sha1(agent:session_id)[:24]>.json`. A DISPLAY, never an authority;
fail-soft. The real hook scripts run as subprocesses against a tmp install, for both adapters."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[1] / "levain" / "templates"
_TREES = {
    "claude_code": _TEMPLATES / "activation" / "hooks",
    "codex": _TEMPLATES / "adapters" / "codex" / "activation" / "hooks",
}
_HOOK_FILES = ("_levain_hook.py", "session_start.py", "user_prompt_submit.py", "automemory_mirror.py")


def _install(root: Path, tree: Path) -> Path:
    hooks = root / "activation" / "hooks"
    hooks.mkdir(parents=True)
    for name in _HOOK_FILES:
        if (tree / name).exists():
            shutil.copy(tree / name, hooks / name)
    (root / "activation" / "recency_directives.md").write_text(
        "# directives\n\n## one\nRECENCY-DIRECTIVE\n", encoding="utf-8")
    (root / ".levain").mkdir()
    return hooks


def _run(root: Path, script: str, payload: dict):
    return subprocess.run(
        [sys.executable, str(root / "activation" / "hooks" / script)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=root, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root)})


def _record(root: Path, agent: str, sid: str) -> dict:
    key = hashlib.sha1(f"{agent}:{sid}".encode()).hexdigest()[:24]
    return json.loads((root / ".levain" / "sessions" / f"{key}.json").read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(_TREES))
def agent_root(request, tmp_path):
    _install(tmp_path, _TREES[request.param])
    return request.param, tmp_path


def test_session_start_writes_the_contract_shape(agent_root):
    agent, root = agent_root
    proc = _run(root, "session_start.py", {"session_id": "s1", "source": "startup", "cwd": "/work/proj"})
    assert proc.returncode == 0 and proc.stderr == ""
    rec = _record(root, agent, "s1")
    assert set(rec) == {"v", "agent", "session_id", "cwd", "first_seen", "last_seen", "last_event",
                        "context_delivered"}
    assert rec["v"] == 1 and rec["agent"] == agent and rec["session_id"] == "s1"
    assert rec["cwd"] == "/work/proj" and rec["last_event"] == "SessionStart"
    assert rec["context_delivered"] is True
    assert rec["first_seen"] == rec["last_seen"] and rec["first_seen"].endswith("Z")


def test_user_prompt_submit_updates_last_seen_and_keeps_the_rest(agent_root):
    agent, root = agent_root
    _run(root, "session_start.py", {"session_id": "s1", "source": "startup", "cwd": "/w"})
    path = next((root / ".levain" / "sessions").glob("*.json"))
    first = json.loads(path.read_text())
    first["first_seen"] = "2020-01-01T00:00:00Z"
    first["last_seen"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(first))
    proc = _run(root, "user_prompt_submit.py", {"session_id": "s1", "prompt": "hello", "cwd": "/w"})
    assert proc.returncode == 0 and proc.stderr == ""
    rec = _record(root, agent, "s1")
    assert rec["first_seen"] == "2020-01-01T00:00:00Z"
    assert rec["last_seen"] != "2020-01-01T00:00:00Z" and rec["last_event"] == "UserPromptSubmit"
    assert rec["context_delivered"] is True


def test_unwritable_sessions_dir_changes_nothing_the_hook_says(agent_root, tmp_path_factory):
    agent, root = agent_root
    other = tmp_path_factory.mktemp("baseline")
    _install(other, _TREES[agent])
    payload = {"session_id": "s1", "source": "startup", "cwd": "/w"}
    base = _run(other, "session_start.py", payload)
    # a FILE where the sessions directory should be: mkdir fails, so the write cannot happen
    (root / ".levain" / "sessions").write_text("not a directory")
    proc = _run(root, "session_start.py", payload)
    assert (proc.returncode, proc.stderr) == (base.returncode, base.stderr) == (0, "")

    def ctx(p):  # the orientation line carries a clock; everything else must match
        c = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        return [s for s in c.split("\n\n") if not s.startswith("[session orientation]")]

    assert ctx(proc) == ctx(base)
    up = _run(root, "user_prompt_submit.py", {"session_id": "s1", "prompt": "x", "cwd": "/w"})
    assert up.returncode == 0 and up.stderr == ""
