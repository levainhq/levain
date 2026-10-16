"""The machine-wide Codex hooks never exit 2 because their install is gone (on codex-cli 0.162.1 exit 2 blocks).

Each command is run the way Codex runs it, through ``sh -c``, against the rendered template.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from levain import install as install_mod
from levain import host_guard as hg

ADAPTER = Path(install_mod.__file__).parent / "templates" / "adapters" / "codex"


def _commands(python: str, install: Path) -> list[str]:
    doc = json.loads(install_mod._codex_hooks_json(ADAPTER, python, install))
    out: list[str] = []
    hg._walk_commands(doc["hooks"], out)
    return out


def _sh(cmd: str, stdin: str = "{}") -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/sh", "-c", cmd], input=stdin, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(sys.platform == "win32", reason="Codex runs hooks through sh here")
def test_every_rendered_command_of_a_deleted_install_exits_0_silently(tmp_path):
    cmds = _commands(sys.executable, tmp_path / "gone")
    assert len(cmds) == 4
    for cmd in cmds:
        r = _sh(cmd)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), cmd


@pytest.mark.skipif(sys.platform == "win32", reason="Codex runs hooks through sh here")
def test_a_gone_interpreter_or_a_venv_without_levain_never_exits_2(tmp_path):
    for cmd in _commands(str(tmp_path / "no-venv" / "python"), tmp_path / "gone"):
        assert _sh(cmd).returncode == 127, cmd
    bare = tmp_path / "bare"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(bare)], check=True)
    for cmd in _commands(str(bare / "bin" / "python"), tmp_path / "gone"):
        assert _sh(cmd).returncode == 1, cmd


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-EP", "-m", "levain.hookrun", str(script), *args],
                          input="{}", capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("body", ["import sys; sys.exit(2)", "raise RuntimeError('x')",
                                  "import argparse; argparse.ArgumentParser().parse_args(['--nope'])",
                                  "import sys; sys.exit('a message')"])
def test_a_failing_script_exits_1_never_2(tmp_path, body):
    s = tmp_path / "h.py"
    s.write_text(body + "\n")
    assert _run(s).returncode == 1


def test_a_script_runs_as_main_with_its_args_and_its_stdout_passes_through(tmp_path):
    s = tmp_path / "h.py"
    s.write_text("import sys, json\nif __name__ == '__main__':\n"
                 "    print(json.dumps({'argv': sys.argv[1:], 'file': __file__}))\n    sys.exit(0)\n")
    r = _run(s, "hook")
    assert r.returncode == 0
    assert json.loads(r.stdout) == {"argv": ["hook"], "file": str(s)}


def test_the_script_stays_the_last_token_every_reader_takes_as_the_hook(tmp_path):
    for cmd in _commands(sys.executable, tmp_path / "ent"):
        argv = shlex.split(cmd)
        assert argv[:4] == [sys.executable, "-EP", "-m", "levain.hookrun"]
        assert Path(argv[-1]).parent == tmp_path / "ent" / "activation" / "hooks"
