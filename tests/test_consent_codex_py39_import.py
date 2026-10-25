"""``codex.py`` stays importable under Python 3.9. The queue broker (root's Python 3.9) once reached
``patch_targets`` / ``PATCH_TOOL`` through it; it now reads them from ``levain.consent.adapters.rules``, which the
root-py39 closure job checks. ``tomllib`` is 3.11+, so no reference to it may sit outside a function."""
from __future__ import annotations

import ast
from pathlib import Path

import levain.consent.adapters.codex as codex


def test_no_tomllib_reference_outside_a_function():
    tree = ast.parse(Path(codex.__file__).read_text(encoding="utf-8"))
    inside = {id(n) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
              for n in ast.walk(f)}
    stray = []
    for n in ast.walk(tree):
        if id(n) in inside:
            continue
        if isinstance(n, ast.Name) and n.id == "tomllib":
            stray.append(n.lineno)
        elif isinstance(n, ast.Import) and any(a.name.split(".")[0] == "tomllib" for a in n.names):
            stray.append(n.lineno)
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "tomllib":
            stray.append(n.lineno)
    assert stray == [], f"tomllib referenced outside a function at line(s) {stray}"
