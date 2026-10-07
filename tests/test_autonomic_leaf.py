"""levain.autonomic is a leaf: standard library and itself only.

The fold kept vagus's anti-cycle rule (the gate never imports a control-plane surface, anneal or
an adapter). Inside Levain that rule is "no import outside ``levain.autonomic``", and this file is
what holds it.

Two checks, because each sees what the other cannot:
  - STATIC, and BOUNDED rather than clever. Chasing every spelling of a dynamic import was beaten a
    new way in each of two review rounds (a parent-relative import, then an aliased
    ``import_module``), so the static check no longer tries to recognise loader CALLS. The leaf has
    no use for a loader at all, so every loader entry point is forbidden by name: the loader
    modules (``importlib``, ``builtins``, ``runpy``, ``pkgutil``, ``zipimport``) and the names
    ``__import__``, ``__builtins__``, ``exec``, ``eval`` and a bare ``compile``.
  - RUNTIME: a fresh interpreter imports every module of the package and asserts nothing outside the
    standard library and ``levain.autonomic`` was loaded on the way.
Known limit: a lookup built from strings at call time (``globals()[...]``) inside a function body
is seen by neither check; the static ban on every loader module and name leaves it nothing to reach.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import levain.autonomic

PKG = Path(levain.autonomic.__file__).parent
_LOADER_MODULES = {"importlib", "builtins", "runpy", "pkgutil", "zipimport", "imp"}
_LOADER_NAMES = {"__import__", "__builtins__", "exec", "eval", "compile"}


def _outside(name: str) -> bool:
    if name == "levain.autonomic" or name.startswith("levain.autonomic."):
        return False
    root = name.split(".")[0]
    return root in _LOADER_MODULES or not (root in sys.stdlib_module_names or name == "__future__")


def _offenders(path: Path) -> list[str]:
    """Every import in ``path`` that leaves the package, and every loader name it mentions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [alias.name for alias in node.names if _outside(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 1:
                bad.append(f"relative import leaving the package (level {node.level})")
            elif node.level == 0 and _outside(node.module or ""):
                bad.append(node.module or "")
        elif isinstance(node, ast.Name) and node.id in _LOADER_NAMES:
            bad.append(f"loader name {node.id}")
    return bad


def test_every_module_imports_only_stdlib_and_itself():
    modules = sorted(PKG.rglob("*.py"))
    assert len(modules) > 1
    found = {p.name: _offenders(p) for p in modules}
    assert {k: v for k, v in found.items() if v} == {}


def test_importing_the_package_loads_nothing_outside_it():
    names = sorted("levain.autonomic." + p.stem for p in PKG.glob("*.py") if p.stem != "__init__")
    probe = (
        "import json, sys\n"
        "import levain\n"
        "before = set(sys.modules)\n"
        f"for n in {names!r}:\n"
        "    __import__(n)\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    loaded = json.loads(out.stdout)
    assert any(n.startswith("levain.autonomic.") for n in loaded)
    stray = [n for n in loaded
             if not (n == "levain.autonomic" or n.startswith("levain.autonomic.")
                     or n.split(".")[0] in sys.stdlib_module_names)]
    assert stray == []


def test_the_static_check_flags_each_way_out(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from levain.kernel import x\n"
        "import anneal_memory\n"
        "from .. import kernel\n"
        "from importlib import import_module as load\n"
        "import builtins\n"
        "__import__('levain.kernel')\n"
        "exec('import levain.kernel')\n",
        encoding="utf-8",
    )
    assert _offenders(bad) == [
        "levain.kernel",
        "anneal_memory",
        "relative import leaving the package (level 2)",
        "importlib",
        "builtins",
        "loader name __import__",
        "loader name exec",
    ]


def test_the_static_check_passes_stdlib_and_the_package(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from __future__ import annotations\nimport json\nimport re\n"
        "from levain.autonomic.posture import Posture\nfrom . import risk\nre.compile('x')\n",
        encoding="utf-8",
    )
    assert _offenders(ok) == []
