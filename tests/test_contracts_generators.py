"""F11 / V13: no generator or helper under tests/golden/contracts/ imports levain.

A vector's expected bytes must come from the spec, not from the code under test, so every ``.py`` there (the
generators and their helper modules alike) imports only the standard library, ``cryptography`` and the local
``_genlib`` helper. Checked on the AST, including ``__import__`` / ``importlib.import_module`` with a literal name.
"""
import ast
import sys
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent / "golden" / "contracts"
ALLOWED = set(sys.stdlib_module_names) | {"cryptography", "_genlib", "__future__"}
FILES = sorted(GOLDEN.rglob("*.py"))


def _imports(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield "<relative>", node.lineno
            else:
                yield node.module or "", node.lineno
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("__import__", "import_module") and node.args:
                a = node.args[0]
                yield (a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else "<dynamic>"), node.lineno


def test_there_are_generators_to_check():
    assert len(FILES) >= 2 and any(p.name == "_genlib.py" for p in FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(GOLDEN)))
def test_generator_imports_no_levain(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for mod, line in _imports(tree):
        top = mod.split(".")[0]
        assert top != "levain", f"{path}:{line} imports {mod}"
        assert top in ALLOWED, f"{path}:{line} imports {mod}, outside the stdlib + cryptography"


def test_the_check_catches_a_levain_import(tmp_path):
    bad = ast.parse("import json\nfrom levain.contracts import canon\nx = __import__('levain')\n")
    found = [m for m, _ in _imports(bad)]
    assert "levain.contracts" in found and "levain" in found
