"""The consent canary (Q5) against a fake Claude Code, for the failure its first real run hit: Claude Code's
stream-json has events whose ``message`` is a string.
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

from levain.consent import canary

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    # A Claude Code stand-in: runs the workspace's installed PermissionRequest hook on a Write of the file the
    # prompt names, and reports the hook's answer the way `claude -p --output-format stream-json` does.
    import json, re, subprocess, sys
    if sys.argv[1:2] == ["--version"]:
        print("2.1.300 (Claude Code)"); sys.exit(0)
    target = re.search(r"create the file (\\S+) containing", sys.argv[2]).group(1)
    settings = json.load(open(".claude/settings.json"))
    command = settings["hooks"]["PermissionRequest"][-1]["hooks"][0]["command"]
    event = {{"session_id": "s", "cwd": ".", "hook_event_name": "PermissionRequest", "permission_mode": "default",
             "tool_name": "Write", "tool_input": {{"file_path": target, "content": "canary"}}}}
    out = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True).stdout
    decision = json.loads(out)["hookSpecificOutput"]["decision"]
    print(json.dumps({{"type": "system", "subtype": "init", "message": "a string, not an object"}}))
    print(json.dumps({{"type": "user", "message": {{"role": "user", "content": [
        {{"type": "tool_result", "content": decision["message"], "is_error": True, "tool_use_id": "t"}}]}}}}))
''')


def _fake(bin_dir: Path, name: str, text: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / name).write_text(text.format(python=sys.executable))
    (bin_dir / name).chmod(0o755)


def test_claude_canary_passes_on_a_reported_denial(tmp_path, monkeypatch):
    _fake(tmp_path / "bin", "claude", FAKE_CLAUDE)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    result = canary.run_claude_canary(workspace=tmp_path / "ws", timeout=60)
    assert (result.status, result.version) == ("PASS", "2.1.300"), result.detail
    assert not (tmp_path / "ws" / canary.SENTINEL_NAME).exists()



def test_version_pin_compares_the_whole_version_and_needs_exit_0(tmp_path):
    """r1 HIGH: any output containing a tested X.Y.Z passed the pin."""
    for name, body in (("alpha", 'echo "codex-cli 0.162.1-alpha.9"'), ("failing", 'echo "codex-cli 0.162.1"; exit 1'),
                       ("good", 'echo "codex-cli 0.162.1"')):
        fake = tmp_path / name
        fake.write_text(f"#!/bin/sh\n{body}\n")
        fake.chmod(0o755)
    assert canary.agent_version("codex", binary=str(tmp_path / "alpha")) == "0.162.1-alpha.9"
    assert canary.agent_version("codex", binary=str(tmp_path / "failing")) is None
    assert canary.require_tested_version("codex", binary=str(tmp_path / "good")) == "0.162.1"
    for name in ("alpha", "failing"):
        try:
            canary.require_tested_version("codex", binary=str(tmp_path / name))
        except canary.UntestedAgentVersion:
            continue
        raise AssertionError(f"{name} passed the pin")


def test_canary_refuses_a_workspace_that_is_not_new_or_empty(tmp_path, monkeypatch):
    """r1 MED: pointed at a real project, the canary replaced its consent hook with the deny-only canary."""
    from levain.consent.adapters import codex as cx
    _fake(tmp_path / "bin", "claude", FAKE_CLAUDE)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    ws = tmp_path / "project"
    ws.mkdir()
    script, _ = cx.install(ws, python=sys.executable, queue_dir=tmp_path / "q", max_hold=0,
                           allow_untested_agent_version=True)
    before = {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    result = canary.run_claude_canary(workspace=ws, timeout=60)
    assert result.status == "FAIL" and "new or empty directory" in result.detail
    assert {p: p.read_bytes() for p in ws.rglob("*") if p.is_file()} == before


def test_the_untested_version_message_names_what_the_caller_can_pass():
    """Q4 r1 item 7: the refusal told the user to pass --allow-untested-agent-version, a flag no command has (no
    levain command installs a consent hook); it names the install functions' parameter. Reproduced red on 8135c4b."""
    for agent in canary.AGENTS:
        text = canary.untested_message(agent, "9.9.9")
        assert "allow_untested_agent_version=True" in text and "--allow" not in text, text
    assert "--allow" not in (canary.__doc__ or "")
