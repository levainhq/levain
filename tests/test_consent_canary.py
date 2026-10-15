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

