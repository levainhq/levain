"""spore-751: exactly one anneal is authoritative — the one levain's own interpreter imports.

Ruled by Phill 2026-09-13. `levain init` used to resolve anneal with
`shutil.which("anneal-memory")` and wire that into the MCP registration, so a second
anneal earlier on PATH became the one serving memory while levain imported another.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path

import pytest

from levain import manifest
from levain.install import _templates_root, apply_init
from levain.interview import build_field_plan, parse_template
from levain.packs import SeedEntry

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX executable decoy")


def _decoy_first_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "decoy-bin"
    bindir.mkdir()
    decoy = bindir / "anneal-memory"
    decoy.write_text("#!/bin/sh\necho DECOY\nexit 3\n", encoding="utf-8")
    decoy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return decoy


@posix_only
def test_resolve_anneal_bin_ignores_an_anneal_earlier_on_path(tmp_path, monkeypatch):
    decoy = _decoy_first_on_path(tmp_path, monkeypatch)
    # Non-vacuity: a PATH lookup really would pick the decoy in this environment.
    assert shutil.which("anneal-memory") == str(decoy)
    # And the fallback cannot land on a real script by coincidence (in a venv the
    # scripts dir IS where the real one lives), so only the RECORD lookup can pass.
    empty = tmp_path / "empty-scripts"
    empty.mkdir()
    monkeypatch.setattr(manifest.sysconfig, "get_path", lambda *a, **k: str(empty))

    resolved = manifest.resolve_anneal_bin()

    assert resolved != str(decoy)
    # The real lookup found a real script, rather than passing via the fallback.
    assert os.path.isfile(resolved)


@posix_only
def test_resolve_anneal_bin_falls_back_to_the_interpreter_not_path(tmp_path, monkeypatch):
    decoy = _decoy_first_on_path(tmp_path, monkeypatch)
    monkeypatch.setattr(manifest.importlib.metadata, "distributions", lambda **k: iter(()))

    resolved = manifest.resolve_anneal_bin()

    assert Path(resolved).parent == Path(sysconfig.get_path("scripts"))
    assert resolved != str(decoy)


def test_the_resolved_script_and_the_module_are_the_same_anneal():
    """The hooks bake the script and the MCP server runs the module. If those two ever
    report different versions, the split this spore closed is back."""
    def version(cmd: list[str]) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()

    script = manifest.resolve_anneal_bin()
    assert os.path.isfile(script)
    assert version([script, "--version"]) == version(
        [sys.executable, "-P", "-m", "anneal_memory", "--version"]
    )


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_init_wires_mcp_to_levains_own_interpreter(tmp_path, monkeypatch, adapter):
    monkeypatch.setattr("levain.install.subprocess.run", lambda cmd, **k: _Result())
    codex_home = tmp_path / "codex_home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    # A quote, a space and a non-BMP character: the value must survive both JSON and TOML.
    python_path = '/opt/odd "dir" \U0001F600/bin/python3'
    install = tmp_path / "install"
    install.mkdir()

    with _templates_root() as templates_root:
        specs = [
            parse_template(templates_root / "seed" / "world.md"),
            parse_template(templates_root / "seed" / "origin.md"),
        ]
        answers = {f.slot: f"VAL_{f.slot}" for f in build_field_plan(specs)}
        apply_init(
            install, adapter, answers, templates_root,
            python_path, "/decoy/anneal-memory", specs,
            [
                SeedEntry(n, templates_root / "seed" / n, "verbatim")
                for n in ("partnership.md", "memory.md", "spore_instructions.md",
                          "continuity.md", "README.md")
            ],
        )

    if adapter == "claude-code":
        server = json.loads((install / ".mcp.json").read_text(encoding="utf-8"))[
            "mcpServers"]["anneal_memory"]
    else:
        server = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))[
            "mcp_servers"]["anneal_memory"]

    assert server["command"] == python_path
    # -P before -m: the client's working directory must not be able to shadow the package.
    assert server["args"][:3] == ["-P", "-m", "anneal_memory"]
    assert server["args"][3:] == ["--db", f"{install}/.levain/memory.db", "serve"]
    assert "/decoy/anneal-memory" not in json.dumps(server)


@posix_only
@pytest.mark.parametrize("template", [
    "activation/hooks/_levain_hook.py",
    "adapters/codex/activation/hooks/_levain_hook.py",
])
def test_hooks_never_ask_a_path_anneal(tmp_path, monkeypatch, template):
    """The hook's own fallback must not reach for a bare `anneal-memory` on PATH when the
    baked script cannot answer. It runs this interpreter's module instead."""
    _decoy_first_on_path(tmp_path, monkeypatch)
    with _templates_root() as templates_root:
        source = templates_root / template
        spec = importlib.util.spec_from_file_location(f"_hook_{abs(hash(template))}", source)
        assert spec is not None and spec.loader is not None
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

    monkeypatch.setattr(hook, "_INSTALL_ANNEAL_BIN", str(tmp_path / "missing" / "anneal-memory"))
    monkeypatch.setattr(hook, "store_path", lambda: tmp_path / "memory.db")
    tried: list[list[str]] = []

    def _record(cmd, **kwargs):
        tried.append(list(cmd))
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(hook.subprocess, "run", _record)

    assert hook._anneal_json(["status", "--json"], timeout=1.0) is None
    heads = [cmd[0] for cmd in tried]
    assert "anneal-memory" not in heads
    assert [sys.executable, "-P", "-m", "anneal_memory"] in [cmd[:4] for cmd in tried]
