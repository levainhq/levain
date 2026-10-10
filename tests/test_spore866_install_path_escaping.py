"""spore-866, reproduced 2026-10-10 with a real `levain init`: an install path holding `\\`
or `"` exited 0 and wrote a codex config.toml, a codex hooks.json and a claude-code
.mcp.json that no longer parsed (the raw path was substituted into string literals); L1 found
the same in every hook .py's `_INSTALL_ANNEAL_BIN = "{{ANNEAL_MEMORY}}"`, and L2 DEL in TOML. L3 r1:
doctor and verify read hook commands with shlex, which kept a backslash sh drops; `update`'s
`_names_install` searched the text and missed the escaped path; a value holding a slot name was
filled twice. L3 r2: one unreadable command hid the rest from `_names_install`; a `{{` in the
anneal path read as "not substituted" in the hook. L3 r3: an argument merely naming the install
counted as ownership, and an unreadable hooks.json was called another install's.
MUTATION (run 2026-10-10): with levain/install.py reverted to the raw substitution, both cases
fail with the reproduced errors (`Unescaped '\\' in a string`, `Invalid \\escape`)."""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import tomllib
from pathlib import Path

import pytest

from levain.install import SeedEntry, _names_install, apply_init
from levain.verify import _python_from_hooks_config
from tests.test_install import _templates_root

# apply_init's subprocess.run is stubbed below, and that stub is the module-global function.
_run = subprocess.run


@pytest.mark.skipif(os.name == "nt", reason="`\"`, `$` and a backquote are not legal in a Windows path")
@pytest.mark.parametrize("adapter", ["codex", "claude-code"])
def test_an_install_path_with_shell_and_string_metacharacters_renders_parseable_files(
    tmp_path: Path, monkeypatch, adapter: str
):
    from levain.interview import build_field_plan, parse_template

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Result())
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    install = tmp_path / 'we\\g<1>i"rd $X`a\x7f{{PYTHON}}'
    install.mkdir()
    python = '/opt/py"th\\on $V/bin/python3'
    anneal = "C:\\Users\\O'Br\"ien{{x}}\\Scripts\\anneal-memory.exe"
    with _templates_root() as templates_root:
        specs = [parse_template(templates_root / "seed" / n) for n in ("world.md", "origin.md")]
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, adapter, answers, templates_root, python, anneal, specs,
            [SeedEntry(n, templates_root / "seed" / n, "verbatim")
             for n in ("partnership.md", "memory.md", "spore_instructions.md",
                       "continuity.md", "README.md")],
        )

    store = f"{install}/.levain/memory.db"
    if adapter == "codex":
        block = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
        server = block["mcp_servers"]["anneal_memory"]
        hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        commands = [ev[0]["hooks"][0]["command"] for ev in hooks["hooks"].values()]
        script_dir = f"{install}/activation/hooks/"
        assert _names_install((codex_home / "hooks.json").read_text(encoding="utf-8"), install)
        # An unreadable foreign command ahead of levain's does not hide it.
        mixed = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        mixed["hooks"]["SessionStart"].insert(0, {"hooks": [{"command": "echo it's"}]})
        assert _names_install(json.dumps(mixed), install)
        assert not _names_install(json.dumps(mixed), install.with_name(install.name + "2"))
        foreign = {"hooks": {"SessionStart": [{"hooks": [{"command":
                   "/py /other/activation/hooks/session_start.py --log " + shlex.quote(f"{install}/x.log")}]}]}}
        assert _names_install(json.dumps(foreign), install) is False   # a mention is not ownership
        assert _names_install("{not json", install) is None               # unreadable: cannot tell
        hooks_config = codex_home / "hooks.json"
    else:
        server = json.loads((install / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["anneal_memory"]
        settings = json.loads((install / ".claude" / "settings.json").read_text(encoding="utf-8"))
        commands = [h["command"] for ev in settings["hooks"].values() for m in ev for h in m["hooks"]]
        script_dir = None
        hooks_config = install / ".claude" / "settings.json"
    assert server["command"] == python
    hooks_py = sorted((install / "activation" / "hooks").glob("*.py"))
    assert hooks_py
    seen = 0
    for f in hooks_py:
        tree = ast.parse(f.read_text(encoding="utf-8"))   # every hook still compiles
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "_INSTALL_ANNEAL_BIN" for t in node.targets):
                assert node.value.value == anneal
                seen += 1
    assert seen
    assert server["args"][server["args"].index("--db") + 1] == store
    assert commands
    for cmd in commands:
        # The shell hands the hook exactly the interpreter and script levain meant.
        argv = _run(
            ["sh", "-c", f"set -- {cmd}; printf '%s\\n' \"$1\" \"$2\""],
            capture_output=True, text=True, check=True,
            env={**os.environ, "CLAUDE_PROJECT_DIR": "/proj"},
        ).stdout.splitlines()
        assert argv[0] == python
        if script_dir is not None:
            assert argv[1].startswith(script_dir) and argv[1].endswith(".py")
        # doctor's and verify's reader agrees (they substitute the harness's own variable too)
        tokens = [t.replace("${CLAUDE_PROJECT_DIR}", "/proj") for t in shlex.split(cmd)]
        assert tokens[:len(argv)] == argv
    assert _python_from_hooks_config(hooks_config, install) == python
