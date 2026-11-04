"""The BOUND record through the installed PostToolUse hook scripts: a masked sandbox denial files its bound on the
ledger row AND tells the agent the one fixed sentence; every other call files no bound claim and prints nothing.
Run through the real template scripts (LEVAIN_HOME naming the consent directory), not the library."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from levain.consent import ledger as ledger_mod
from levain.consent.bound import AGENT_SENTENCE
from levain.consent.ledger import Ledger, hook_bound_sentence, hook_record

TEMPLATES = Path(__file__).resolve().parent.parent / "levain" / "templates"
CC_HOOKS = TEMPLATES / "activation" / "hooks"
CX_HOOKS = TEMPLATES / "adapters" / "codex" / "activation" / "hooks"
EPERM = "touch: /private/tmp/outside/root_control: Operation not permitted"
SENTENCE = AGENT_SENTENCE.format(n=1, classes="other")


def _home(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "consent").mkdir(mode=0o700, exist_ok=True)
    # PYTHONPATH pins the hook's `import levain` to this checkout, not whatever the interpreter has installed
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "LEVAIN_HOME": str(tmp_path),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            "LEVAIN_SCOPE": "global"}


def _run(hooks: Path, env: dict[str, str], payload: dict) -> subprocess.CompletedProcess:
    # the hook files its session log under its own install root, so run a copy inside the test's home
    root = Path(env["LEVAIN_HOME"]) / ("inst-" + hooks.parts[-4 if "adapters" in hooks.parts else -2])
    copy = root / "activation" / "hooks"
    if not copy.exists():
        shutil.copytree(hooks, copy)
    return subprocess.run([sys.executable, str(copy / "post_tool_use.py")], input=json.dumps(payload), text=True,
                          env=env, cwd=root, capture_output=True, timeout=60, check=True)


def _cc(tool_use_id: str, stderr: str, **kw) -> dict:
    return {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/w", "tool_name": "Bash",
            "tool_input": {"command": "touch /outside/x; echo done"}, "tool_use_id": tool_use_id,
            "tool_response": {"stdout": "done\n", "stderr": stderr, "interrupted": False}, **kw}


def _rows(env: dict[str, str]) -> list:
    # every row of the session (a Codex hook also stamps its row, so it is no longer pending)
    return Ledger(Path(env["LEVAIN_HOME"]) / "consent").window("claude_code", "s1", "2020-01-01T00:00:00Z") \
        + Ledger(Path(env["LEVAIN_HOME"]) / "consent").window("codex", "s1", "2020-01-01T00:00:00Z")


@pytest.mark.parametrize("hooks,agent", [(CC_HOOKS, "claude_code"), (CX_HOOKS, "codex")])
def test_masked_denial_files_the_bound_and_tells_the_agent(tmp_path, hooks, agent):
    env = _home(tmp_path)
    payload = _cc("t-masked", EPERM)
    if agent == "codex":
        payload["tool_response"] = EPERM + "\ndone\n"
    out = _run(hooks, env, payload)
    spec = json.loads(out.stdout)["hookSpecificOutput"]
    assert spec == {"hookEventName": "PostToolUse", "additionalContext": SENTENCE}
    rows = [r for r in _rows(env) if r.tool_use_id == "t-masked"]
    assert [(r.bound, r.bound_denials, r.bound_classes) for r in rows] == [("touched", 1, ("other",))]
    # the sentence carries no path and no echo of the command
    assert "/outside" not in out.stdout and "touch" not in out.stdout


@pytest.mark.parametrize("hooks", [CC_HOOKS, CX_HOOKS])
def test_a_quiet_call_prints_nothing_and_claims_no_bound(tmp_path, hooks):
    env = _home(tmp_path)
    out = _run(hooks, env, _cc("t-quiet", ""))
    assert out.stdout == ""
    # silence is unknown, never clean (a `curl -s` or a write to /dev/null leaves no text)
    assert [r.bound for r in _rows(env) if r.tool_use_id == "t-quiet"] == ["unknown"]


def test_failure_event_files_the_bound_and_prints_nothing(tmp_path):
    env = _home(tmp_path)
    fail = {"hook_event_name": "PostToolUseFailure", "session_id": "s1", "cwd": "/w", "tool_name": "Bash",
            "tool_input": {"command": "x"}, "tool_use_id": "t-fail", "error": "Exit code 1\n" + EPERM}
    # a failed call's exit status is visible, so the "status 0" sentence is not told; the bound is still filed
    assert _run(CC_HOOKS, env, fail).stdout == ""
    assert [r.bound for r in _rows(env) if r.tool_use_id == "t-fail"] == ["touched"]


def test_non_shell_tool_is_silent(tmp_path):
    env = _home(tmp_path)
    read = {"hook_event_name": "PostToolUse", "session_id": "s1", "cwd": "/w", "tool_name": "Read",
            "tool_input": {"file_path": "/w/a"}, "tool_use_id": "t-read", "tool_response": EPERM}
    assert _run(CC_HOOKS, env, read).stdout == ""


@pytest.mark.parametrize("hooks", [CC_HOOKS, CX_HOOKS])
def test_a_suppressed_session_is_told_nothing_but_the_bound_is_filed(tmp_path, hooks):
    env = {**_home(tmp_path), "LEVAIN_HOOK_SUPPRESS": "1"}
    payload = _cc("t-sup", EPERM)
    if hooks is CX_HOOKS:
        payload["tool_response"] = EPERM + "\ndone\n"
    out = _run(hooks, env, payload)
    assert out.stdout == ""
    assert [r.bound for r in _rows(env) if r.tool_use_id == "t-sup"] == ["touched"]


@pytest.mark.parametrize("hooks", [CC_HOOKS, CX_HOOKS])
def test_an_absent_event_name_is_a_success_event(tmp_path, hooks):
    env = _home(tmp_path)
    payload = _cc("t-noev", EPERM)
    del payload["hook_event_name"]
    if hooks is CX_HOOKS:
        payload["tool_response"] = EPERM + "\ndone\n"
    spec = json.loads(_run(hooks, env, payload).stdout)["hookSpecificOutput"]
    assert spec == {"hookEventName": "PostToolUse", "additionalContext": SENTENCE}


def test_an_escaped_call_is_none_and_silent(tmp_path):
    env = _home(tmp_path)
    escaped = _cc("t-esc", EPERM)
    escaped["tool_input"]["dangerouslyDisableSandbox"] = True
    assert _run(CC_HOOKS, env, escaped).stdout == ""
    assert [r.bound for r in _rows(env) if r.tool_use_id == "t-esc"] == ["none"]


def test_the_sentence_needs_no_ledger_and_never_raises(tmp_path):
    p = _cc("t-x", EPERM)
    assert hook_bound_sentence("claude_code", p) == SENTENCE
    # no consent directory: the row is not filed, the agent is still told
    assert hook_record("claude_code", p, queue_dir=tmp_path / "absent") is None
    for bad in (None, [], "x", {"hook_event_name": "PostToolUse"}, {"hook_event_name": "PostToolUse", "tool_name": 3}):
        assert hook_bound_sentence("claude_code", bad) is None
    assert hook_bound_sentence("nobody", p) is None


def test_a_classifier_failure_loses_the_bound_never_the_row(tmp_path, monkeypatch):
    env = _home(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(ledger_mod._bound, "classify_payload", boom)
    assert hook_bound_sentence("claude_code", _cc("t-b", EPERM)) is None
    rid = hook_record("claude_code", _cc("t-b", EPERM), queue_dir=Path(env["LEVAIN_HOME"]) / "consent")
    assert rid is not None
    rows = [r for r in _rows(env) if r.tool_use_id == "t-b"]
    assert len(rows) == 1 and rows[0].bound is None
