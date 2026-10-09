"""levain.autonomic is a leaf: standard library and itself only.

The fold kept vagus's anti-cycle rule (the gate never imports a control-plane surface, anneal or
an adapter). Inside Levain that rule is "no import outside ``levain.autonomic``", and this file is
what holds it.

The static check is an ALLOWLIST: the package may import exactly the standard-library roots in
``ALLOWED_STDLIB`` and its own modules, nothing else. Two earlier forms were denylists of loader
modules and calls, and review beat each a new way (a parent-relative import, an aliased
``import_module``, ``pydoc.locate``), so the list now names what is allowed instead of what is not.
Adding a stdlib module to the package means adding it here, on purpose, in review.

Also flagged: the builtin loader names (``__import__``, ``__builtins__``, ``exec``, ``eval``, a bare
``compile``) and any attribute chain off the name ``levain`` other than ``levain.autonomic`` (an
``import levain.autonomic.x`` binds ``levain``, through which ``levain.kernel`` is reachable).

The runtime check imports every module of the package, recursively, in a fresh interpreter and
asserts nothing outside the standard library and ``levain.autonomic`` was loaded on the way.

Known limit: code that builds a module name from strings and resolves it through an allowed
module's own API (``getattr`` on an object it was handed) is not something an AST check can see.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import levain.autonomic

PKG = Path(levain.autonomic.__file__).parent

ALLOWED_STDLIB = frozenset({
    "__future__", "collections", "contextlib", "copy", "dataclasses", "datetime", "enum", "fcntl",
    "hashlib", "json", "logging", "math", "os", "pathlib", "re", "sqlite3", "subprocess", "tempfile",
    "threading",
    "typing", "unicodedata",
})
_LOADER_NAMES = frozenset({"__import__", "__builtins__", "exec", "eval", "compile"})


def _allowed(name: str) -> bool:
    if name == "levain.autonomic" or name.startswith("levain.autonomic."):
        return True
    return name.split(".")[0] in ALLOWED_STDLIB


def _attr_chain(node: ast.Attribute) -> list[str]:
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return parts[::-1]


def _offenders(path: Path) -> list[str]:
    """Every import in ``path`` outside the allowlist, every loader name, every reach past autonomic."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [alias.name for alias in node.names if not _allowed(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 1:
                bad.append(f"relative import leaving the package (level {node.level})")
            elif node.level == 0 and not _allowed(node.module or ""):
                bad.append(node.module or "")
        elif isinstance(node, ast.Name) and node.id in _LOADER_NAMES:
            bad.append(f"loader name {node.id}")
        elif isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            if len(chain) >= 2 and chain[0] == "levain" and chain[1] != "autonomic":
                bad.append("attribute " + ".".join(chain))
    return bad


def test_every_module_imports_only_the_allowlist_and_itself():
    modules = sorted(PKG.rglob("*.py"))
    assert len(modules) > 1
    found = {str(p.relative_to(PKG)): _offenders(p) for p in modules}
    assert {k: v for k, v in found.items() if v} == {}


def test_importing_the_package_loads_nothing_outside_it():
    probe = (
        "import json, pkgutil, sys\n"
        "import levain\n"
        "before = set(sys.modules)\n"
        "import levain.autonomic as pkg\n"
        "for m in pkgutil.walk_packages(pkg.__path__, 'levain.autonomic.'):\n"
        "    __import__(m.name)\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    root = PKG.parents[1]   # the checkout under test, never another installed levain (codex r11)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         cwd=root, env=env)
    loaded = json.loads(out.stdout)
    assert sum(n.startswith("levain.autonomic.") for n in loaded) > 1
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
        "import pydoc\n"
        "__import__('levain.kernel')\n"
        "import levain.autonomic.posture\n"
        "levain.kernel.run()\n",
        encoding="utf-8",
    )
    assert sorted(_offenders(bad)) == sorted([
        "levain.kernel",
        "anneal_memory",
        "relative import leaving the package (level 2)",
        "importlib",
        "pydoc",
        "loader name __import__",
        "attribute levain.kernel.run",
        "attribute levain.kernel",
    ])


def test_the_static_check_passes_the_allowlist_and_the_package(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from __future__ import annotations\nimport json\nimport re\nimport os.path\n"
        "from levain.autonomic.posture import Posture\nfrom . import risk\nre.compile('x')\n"
        "import levain.autonomic.kill\nlevain.autonomic.kill.kill_outcome\n",
        encoding="utf-8",
    )
    assert _offenders(ok) == []


def test_offenders_are_reported_per_path_not_per_basename(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "z").mkdir()
    (tmp_path / "a" / "helper.py").write_text("import levain.kernel\n", encoding="utf-8")
    (tmp_path / "z" / "helper.py").write_text("import json\n", encoding="utf-8")
    found = {str(p.relative_to(tmp_path)): _offenders(p) for p in sorted(tmp_path.rglob("*.py"))}
    assert {k: v for k, v in found.items() if v} == {"a/helper.py": ["levain.kernel"]}
