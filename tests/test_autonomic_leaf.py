"""levain.autonomic is a leaf: standard library and itself only.

The fold kept vagus's anti-cycle rule (the gate never imports a control-plane surface, anneal or
an adapter). Inside Levain that rule is "no import outside ``levain.autonomic``", and this test is
what holds it, so a later edit that reaches into the rest of Levain fails here.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import levain.autonomic

PKG = Path(levain.autonomic.__file__).parent
_DYNAMIC = {"import_module", "__import__"}


def _offenders(path: Path) -> list[str]:
    """Every import in ``path`` that leaves the package, plus any dynamic import call."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                continue  # `from . import x` / `from .x import y`: inside the package
            if node.level > 1:
                bad.append(f"relative import leaving the package (level {node.level})")
                continue
            names = [node.module or ""]
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if called in _DYNAMIC:
                bad.append(f"dynamic import call {called}()")
            continue
        for name in names:
            if name == "levain.autonomic" or name.startswith("levain.autonomic."):
                continue
            if name.split(".")[0] in sys.stdlib_module_names or name == "__future__":
                continue
            bad.append(name)
    return bad


def test_every_module_imports_only_stdlib_and_itself():
    modules = sorted(PKG.rglob("*.py"))
    assert len(modules) > 1
    found = {p.name: _offenders(p) for p in modules}
    assert {k: v for k, v in found.items() if v} == {}


def test_the_check_flags_each_way_out(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from levain.kernel import x\n"
        "import anneal_memory\n"
        "from .. import kernel\n"
        "import importlib\n"
        "importlib.import_module('levain.kernel')\n"
        "__import__('levain.kernel')\n",
        encoding="utf-8",
    )
    assert _offenders(bad) == [
        "levain.kernel",
        "anneal_memory",
        "relative import leaving the package (level 2)",
        "dynamic import call import_module()",
        "dynamic import call __import__()",
    ]


def test_the_check_passes_stdlib_and_the_package(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from __future__ import annotations\nimport json\nfrom levain.autonomic.posture import Posture\n"
        "from . import risk\n",
        encoding="utf-8",
    )
    assert _offenders(ok) == []
