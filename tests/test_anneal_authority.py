"""intention-751: exactly one anneal is authoritative — the one levain's own interpreter imports.

Ruled by Phill 2026-09-13. `levain init` used to resolve anneal with
`shutil.which("anneal-memory")` and wire that into the MCP registration, so a second
anneal earlier on PATH became the one serving memory while levain imported another.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shlex
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
from levain.packs import compose_roster

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
    report different versions, the split this intention closed is back."""
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
            # the rest of the base roster, derived so a seed file the package adds is never missing here
            [e for e in compose_roster([templates_root])
             if e.name not in ("world.md", "origin.md")],
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
    assert server["args"][3:] == ["--db", f"{install}/.levain/memory.db", "serve", "--session-id", "levain"]
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


class _FakeFile:
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.name = rel.rsplit("/", 1)[-1]


class _FakeDist:
    def __init__(self, root: Path, rels: list[str]) -> None:
        self.root = root
        self.files = [_FakeFile(r) for r in rels]

    def locate_file(self, f: _FakeFile) -> Path:
        return self.root / f.rel


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@posix_only
def test_a_script_from_a_distribution_that_does_not_own_the_module_is_never_used(
    tmp_path, monkeypatch
):
    """codex L3 MED: with two visible anneal-memory distributions, the one listed first
    must not supply the script unless it owns the module the interpreter imports."""
    stray_root, owner_root = tmp_path / "stray", tmp_path / "owner"
    _executable(stray_root / "bin" / "anneal-memory")
    owner_script = _executable(owner_root / "bin" / "anneal-memory")
    module = owner_root / "anneal_memory" / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")

    stray = _FakeDist(stray_root, ["bin/anneal-memory"])
    owner = _FakeDist(owner_root, ["anneal_memory/__init__.py", "bin/anneal-memory"])
    monkeypatch.setattr(manifest.importlib.metadata, "distributions",
                        lambda **k: iter([stray, owner]))
    monkeypatch.setattr(manifest.importlib.util, "find_spec",
                        lambda name: type("S", (), {"origin": str(module)})())

    assert manifest.resolve_anneal_bin() == str(owner_script)

    # And with several distributions but no identifiable owner, none is trusted.
    monkeypatch.setattr(manifest.importlib.metadata, "distributions",
                        lambda **k: iter([stray, _FakeDist(owner_root, ["bin/anneal-memory"])]))
    monkeypatch.setattr(manifest.sysconfig, "get_path", lambda *a, **k: str(tmp_path / "none"))
    assert manifest.resolve_anneal_bin() == str(tmp_path / "none" / "anneal-memory")


@posix_only
def test_doctor_falls_back_to_the_module_when_the_script_cannot_run(tmp_path, monkeypatch):
    """codex L3 LOW: a stale-shebang script must not fail doctor while the module works."""
    from levain import doctor

    broken = tmp_path / "bin" / "anneal-memory"
    broken.parent.mkdir()
    broken.write_text("#!/nonexistent/python\n", encoding="utf-8")
    broken.chmod(0o755)
    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(broken))
    # This test is about the fallback, not the range: no requirement to read, so no range
    # can decide it.
    monkeypatch.setattr(manifest, "_levain_requires", lambda: [])

    results = doctor._check_runtime(tmp_path)

    anneal = [r for r in results if r.name.startswith("anneal-memory")]
    assert len(anneal) == 1 and anneal[0].ok, anneal
    assert str(broken) in anneal[0].detail




# levain#194 part B: the anneal checks judge the runnable anneal against the range levain declares
# (a stale guest-VM image carried 1.0.2 and `levain serve` died on import). The range is read through
# manifest's `_levain_requires` (repo pyproject first), the same source the pip-pin row uses.
_DECLARED = "anneal-memory<2.1,>=2.0.1"
_PIP = f"{shlex.quote(sys.executable)} -m pip"


def _declared(monkeypatch, requires):
    monkeypatch.setattr(manifest, "_levain_requires", lambda: requires)


def _module_check(tmp_path, monkeypatch, version_out, requires):
    from levain import doctor

    _declared(monkeypatch, requires)
    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(tmp_path / "no-such-script"))
    monkeypatch.setattr(
        doctor, "_probe",
        lambda cmd, timeout=5.0: (True, version_out) if "anneal_memory" in cmd else (False, ""),
    )
    results = doctor._check_runtime(tmp_path)
    anneal = [r for r in results if r.name.startswith("anneal-memory")]
    assert len(anneal) == 1, anneal
    return anneal[0]


def _script_check(tmp_path, monkeypatch, version_out, requires):
    """The CLI-script branch: a script file exists and its faked `--version` runs."""
    from levain import doctor

    _declared(monkeypatch, requires)
    script = tmp_path / "bin" / "anneal-memory"
    script.parent.mkdir(exist_ok=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(script))
    monkeypatch.setattr(doctor, "_probe", lambda cmd, timeout=5.0: (True, version_out))
    results = doctor._check_runtime(tmp_path)
    anneal = [r for r in results if r.name.startswith("anneal-memory")]
    assert len(anneal) == 1, anneal
    return anneal[0], script


_BRANCHES = pytest.mark.parametrize("branch", ["module", "script"])


def _check(branch, tmp_path, monkeypatch, version_out, requires):
    if branch == "module":
        return _module_check(tmp_path, monkeypatch, version_out, requires)
    return _script_check(tmp_path, monkeypatch, version_out, requires)[0]


@_BRANCHES
@pytest.mark.parametrize("version", ["1.0.2", "2.0.0", "2.0", "2.1.0", "2.1", "3.0.0"])
def test_doctor_fails_a_runnable_anneal_outside_levains_range(tmp_path, monkeypatch, branch, version):
    r = _check(branch, tmp_path, monkeypatch, f"anneal-memory {version}\n",
               ["markdown-it-py<5,>=4", _DECLARED, 'mcp>=1.27; extra == "app"'])

    assert r.ok is False and not r.warn
    assert r.name == ("anneal-memory module" if branch == "module" else "anneal-memory CLI")
    assert f"anneal-memory {version})" in r.detail and _DECLARED in r.detail
    assert r.hint == f"{_PIP} install -U {shlex.quote(_DECLARED)}"


@_BRANCHES
@pytest.mark.parametrize("line", ["anneal-memory 2.0.1\n", "anneal-memory v2.0.1\n", "2.0.1\n", "anneal-memory 2.0.9\n"])
def test_doctor_passes_a_runnable_anneal_inside_levains_range(tmp_path, monkeypatch, branch, line):
    r = _check(branch, tmp_path, monkeypatch, line, [_DECLARED])

    assert r.ok is True and not r.warn and not r.hint
    assert "not checked" not in r.detail.lower() and f"({line.strip()})" in r.detail


def test_doctor_names_where_the_module_and_the_script_were_judged(tmp_path, monkeypatch):
    module = _module_check(tmp_path, monkeypatch, "anneal-memory 2.0.1\n", [_DECLARED])
    (tmp_path / "s").mkdir()
    script_check, script = _script_check(tmp_path / "s", monkeypatch, "anneal-memory 2.0.1\n", [_DECLARED])

    assert f"importable via {sys.executable}" in module.detail
    assert script_check.detail == f"{script} (anneal-memory 2.0.1)"


@_BRANCHES
def test_a_fail_says_which_interpreter_it_was_judged_for(tmp_path, monkeypatch, branch):
    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 1.0.2\n", [_DECLARED])

    assert f"Judged for the interpreter doctor runs under ({sys.executable})" in r.detail
    assert "another venv is not judged" in r.detail


@_BRANCHES
@pytest.mark.parametrize("version", ["2.0.1.post1", "2.0.1+local", "1!2.0.1", "2.0.1rc1", "2.0.2.dev0", "dev", "present"])
def test_doctor_warns_it_did_not_check_a_version_it_cannot_model(tmp_path, monkeypatch, branch, version):
    r = _check(branch, tmp_path, monkeypatch, f"anneal-memory {version}\n", [_DECLARED])

    # A warning: ok (never a fail on a possibly good install) but not a green pass.
    assert r.ok is True and r.warn is True
    assert "NOT checked" in r.detail and r.hint and _DECLARED in r.hint


@_BRANCHES
@pytest.mark.parametrize("version_out", ["", "anneal-memory 2.0.1 schema 2\n", "anneal-memory 2.0.1 python 3.12\n"])
def test_doctor_does_not_guess_a_version_out_of_a_longer_line(tmp_path, monkeypatch, branch, version_out):
    """The version is the one token right after the name, never the last release-looking token."""
    r = _check(branch, tmp_path, monkeypatch, version_out, [_DECLARED])

    assert r.ok is True and r.warn is True and "NOT checked" in r.detail


@_BRANCHES
@pytest.mark.parametrize("requires", [None, [], ["markdown-it-py>=4"], ["anneal-memory"],
                                      ["anneal-memory~=2.0"], ["anneal-memory==2.0.*"],
                                      ['anneal-memory>=3; extra == "dev"'],
                                      ['anneal-memory>=3; python_version >= "3.10"']])
def test_doctor_warns_when_the_requirement_cannot_be_judged(tmp_path, monkeypatch, branch, requires):
    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 1.0.2\n", requires or [])

    assert r.ok is True and r.warn is True and "NOT checked" in r.detail


@_BRANCHES
def test_an_unmodelled_clause_does_not_hide_an_earlier_definite_failure(tmp_path, monkeypatch, branch):
    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 1.0.2\n", ["anneal-memory>=2.0.1,~=2.0"])
    assert r.ok is False and "anneal-memory>=2.0.1,~=2.0" in r.detail

    # ...and the clause that is not modelled, ahead of the definite one, hides nothing either
    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 1.0.2\n", ["anneal-memory~=2.0,>=2.0.1"])
    assert r.ok is False


@_BRANCHES
def test_an_unmodelled_clause_with_no_failure_is_not_a_pass(tmp_path, monkeypatch, branch):
    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 2.0.1\n", ["anneal-memory>=2.0.1,~=2.0"])

    assert r.ok is True and r.warn is True and "NOT checked" in r.detail


@_BRANCHES
@pytest.mark.parametrize("version, ok", [("2.1.0", False), ("2.0.1", False), ("1.5.0", False), ("2.0.5", True)])
def test_every_anneal_entry_must_hold(tmp_path, monkeypatch, branch, version, ok):
    """Two entries for the package are ANDed: the second (`<2.1`) is not forgotten behind the first."""
    r = _check(branch, tmp_path, monkeypatch, f"anneal-memory {version}\n",
               ["anneal-memory>=2.0.5", "anneal-memory<2.1"])

    assert r.ok is ok
    if not ok:
        assert "anneal-memory>=2.0.5,<2.1" in r.detail


def _pyproject(tmp_path, dependency):
    path = tmp_path / "repo" / "pyproject.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        f'[project]\nname = "levain"\nversion = "0"\ndependencies = ["{dependency}"]\n', encoding="utf-8"
    )
    return path


@_BRANCHES
def test_the_repo_requirement_wins_over_stale_installed_metadata(tmp_path, monkeypatch, branch):
    """An editable install's dist metadata is not regenerated when pyproject moves: a good 2.0.1
    must pass on the repo's range, and the hint must never send an operator to the stale one."""
    from levain import doctor

    monkeypatch.setattr(manifest, "_PYPROJECT_PATH", _pyproject(tmp_path, "anneal-memory>=2.0.1,<2.1"))
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: ["anneal-memory<1.1,>=1.0.7"])
    # _levain_requires is NOT patched here: the real repo-first read runs.
    if branch == "module":
        monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(tmp_path / "no-such-script"))
        monkeypatch.setattr(doctor, "_probe", lambda cmd, timeout=5.0: (True, "anneal-memory 2.0.1\n"))
    else:
        script = tmp_path / "bin" / "anneal-memory"
        script.parent.mkdir(exist_ok=True)
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(script))
        monkeypatch.setattr(doctor, "_probe", lambda cmd, timeout=5.0: (True, "anneal-memory 2.0.1\n"))

    [good] = [r for r in doctor._check_runtime(tmp_path) if r.name.startswith("anneal-memory")]
    assert good.ok is True and not good.warn and not good.hint

    monkeypatch.setattr(doctor, "_probe", lambda cmd, timeout=5.0: (True, "anneal-memory 1.0.9\n"))
    [bad] = [r for r in doctor._check_runtime(tmp_path) if r.name.startswith("anneal-memory")]
    assert bad.ok is False and "anneal-memory>=2.0.1,<2.1" in bad.hint and "1.0.7" not in bad.hint


def test_installed_metadata_is_used_when_there_is_no_source_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "_PYPROJECT_PATH", tmp_path / "absent" / "pyproject.toml")
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: [_DECLARED])

    r = _module_check_unpatched(tmp_path, monkeypatch, "anneal-memory 1.0.2\n")

    assert r.ok is False and _DECLARED in r.detail


def _module_check_unpatched(tmp_path, monkeypatch, version_out):
    from levain import doctor

    monkeypatch.setattr(manifest, "resolve_anneal_bin", lambda: str(tmp_path / "no-such-script"))
    monkeypatch.setattr(
        doctor, "_probe",
        lambda cmd, timeout=5.0: (True, version_out) if "anneal_memory" in cmd else (False, ""),
    )
    [r] = [r for r in doctor._check_runtime(tmp_path) if r.name.startswith("anneal-memory")]
    return r


@_BRANCHES
def test_the_pip_hint_quotes_an_interpreter_path_with_a_space(tmp_path, monkeypatch, branch):
    monkeypatch.setattr(sys, "executable", "/opt/my venv/bin/python")

    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 1.0.2\n", [_DECLARED])

    assert r.hint == "'/opt/my venv/bin/python' -m pip install -U 'anneal-memory<2.1,>=2.0.1'"
    assert r.hint.startswith(manifest.pip_invocation())


@_BRANCHES
def test_the_not_checked_warning_renders_as_a_warning_not_a_pass(tmp_path, monkeypatch, branch, capsys):
    from levain import doctor

    r = _check(branch, tmp_path, monkeypatch, "anneal-memory 2.0.1rc1\n", [_DECLARED])
    doctor._emit(r)

    out = capsys.readouterr().out
    assert doctor._WARN in out and doctor._OK not in out
    assert r.hint in out


def test_a_missing_or_empty_version_is_guarded(tmp_path, monkeypatch):
    from levain import doctor

    _declared(monkeypatch, [_DECLARED])
    for version in (None, ""):
        r = doctor._anneal_version_result("anneal-memory CLI", "/x/anneal-memory", version)
        assert r.ok is True and r.warn is True and "NOT checked" in r.detail
    assert doctor._anneal_range_verdict(None)[0] is None
