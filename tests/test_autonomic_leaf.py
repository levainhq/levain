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


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_every_module_imports_only_stdlib_and_itself():
    offenders = []
    modules = sorted(PKG.glob("*.py"))
    assert len(modules) > 1
    for path in modules:
        for name in _imported_roots(path):
            if name == "levain.autonomic" or name.startswith("levain.autonomic."):
                continue
            if name.split(".")[0] in sys.stdlib_module_names or name == "__future__":
                continue
            offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_check_sees_a_forbidden_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("from levain.kernel import x\nimport anneal_memory\n", encoding="utf-8")
    assert _imported_roots(bad) == {"levain.kernel", "anneal_memory"}
