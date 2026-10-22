"""Root-side code runs under the system ``/usr/bin/python3 -IS`` (``levain.slot.root_tree.ROOT_PYTHON``), whose floor
is 3.9 while the operator side and CI run 3.12+. Two tests guard it:

* ``test_every_root_side_module_imports_on_the_floor_interpreter`` imports EVERY ``levain.slot`` and ``levain.broker``
  module in one fresh 3.9 interpreter and names each that fails. It catches IMPORT-TIME failures only (a ``X | Y``
  evaluated at module level, a missing stdlib module, a syntax newer than 3.9). It runs only when ``/usr/bin/python3``
  is exactly 3.9.x, and skips by name otherwise.
* ``test_no_post_39_construct_on_the_root_side`` walks the AST of the same files and refuses the CALL-TIME and
  newer-syntax constructs that compile and import fine and fail when the line runs (``binascii.a2b_base64(strict_mode=)``
  is 3.11+). It needs no 3.9 interpreter, so it runs everywhere, CI included. The sweep in ``project_memory/1022-8/py39/``
  is the wider net.
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROOT_PYTHON = "/usr/bin/python3"
FLOOR = (3, 9)
#: Third-party packages the root import tree VENDORS beside ``levain`` (root_tree.py); absent from a bare worktree.
VENDORED = {"h11"}

_CHILD = """
import importlib, sys
sys.path.insert(0, {repo!r})
vendored = {vendored!r}
bad = []
for name in {modules!r}:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name not in vendored:
            bad.append("%s: %s: %s" % (name, type(e).__name__, e))
    except BaseException as e:
        bad.append("%s: %s: %s" % (name, type(e).__name__, e))
print("\\n".join(bad))
sys.exit(1 if bad else 0)
"""


def _modules() -> list[str]:
    out = []
    for pkg in ("slot", "broker"):
        for p in sorted((REPO / "levain" / pkg).rglob("*.py")):
            parts = list(p.relative_to(REPO).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            out.append(".".join(parts))
    return out


def _module_path(name: str) -> Path:
    p = REPO / (name.replace(".", "/") + ".py")
    return p if p.exists() else REPO / name.replace(".", "/") / "__init__.py"


def _floor_python() -> tuple[str | None, str]:
    """The import test needs the macOS system interpreter, exactly 3.9.x: a 3.10/3.11 host would pass code that breaks on 3.9."""
    if not os.path.exists(ROOT_PYTHON):
        return None, f"the floor is 3.9; {ROOT_PYTHON} does not exist on this host"
    r = subprocess.run([ROOT_PYTHON, "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None, f"the floor is 3.9; {ROOT_PYTHON} did not run (exit {r.returncode})"
    version = r.stdout.strip()
    if tuple(int(x) for x in version.split(".")[:2]) != FLOOR:
        return None, f"the floor is 3.9; this host's {ROOT_PYTHON} is {version}"
    return ROOT_PYTHON, ""


def test_every_root_side_module_imports_on_the_floor_interpreter():
    python, why = _floor_python()
    if python is None:
        pytest.skip(why)
    code = _CHILD.format(repo=str(REPO), vendored=VENDORED, modules=_modules())
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    r = subprocess.run([python, "-I", "-S", "-B", "-c", code], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0, "root-side modules that do not import on the floor interpreter:\n" + r.stdout + r.stderr[-1000:]


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, else ``""``."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


#: Attribute chains (or bare names imported from the module) that exist only after 3.9.
_POST_39_NAMES = {
    "asyncio.timeout": "asyncio.timeout (3.11+)",
    "asyncio.TaskGroup": "asyncio.TaskGroup (3.11+)",
    "contextlib.chdir": "contextlib.chdir (3.11+)",
    "hashlib.file_digest": "hashlib.file_digest (3.11+)",
    "itertools.pairwise": "itertools.pairwise (3.10+)",
    "enum.StrEnum": "enum.StrEnum (3.11+)",
}
#: ``from <module> import <name>`` forms of the same.
_POST_39_FROM = {(m, n): d for k, d in _POST_39_NAMES.items() for m, n in [k.split(".")]}


def _post_39_violations(source: str, label: str) -> list[str]:
    """``label:line: why`` for every construct in ``source`` the 3.9 root interpreter rejects at run time."""
    tree = ast.parse(source, filename=label)
    hits: list[str] = []

    def hit(node: ast.AST, why: str) -> None:
        hits.append(f"{label}:{node.lineno}: {why}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = _dotted(node.func)
            tail = fn.rsplit(".", 1)[-1]
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                if kw.arg == "strict_mode":
                    hit(node, f"keyword strict_mode= on {fn or '<call>'}() (3.11+)")
                elif kw.arg == "strict" and tail == "zip":
                    hit(node, "zip(strict=) (3.10+)")
                elif kw.arg in ("slots", "kw_only") and tail == "dataclass":
                    hit(node, f"dataclass({kw.arg}=) (3.10+)")
                elif kw.arg == "key" and (fn.startswith("bisect.") or tail.startswith(("bisect", "insort"))):
                    hit(node, f"{fn}(key=) (3.10+)")
        elif isinstance(node, ast.Attribute):
            why = _POST_39_NAMES.get(_dotted(node))
            if why:
                hit(node, why)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "tomllib":
                    hit(node, "import tomllib (3.11+)")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "tomllib":
                hit(node, "from tomllib import (3.11+)")
            for a in node.names:
                why = _POST_39_FROM.get((node.module or "", a.name))
                if why:
                    hit(node, f"from {node.module} import {a.name}: {why}")
        elif type(node).__name__ == "Match":  # ast.Match exists only on 3.10+, so match by class name
            hit(node, "match statement (3.10+)")
        elif type(node).__name__ == "TryStar":  # ast.TryStar exists only on 3.11+
            hit(node, "except* (3.11+)")
    return hits


def _all_violations() -> list[str]:
    hits: list[str] = []
    for name in _modules():
        p = _module_path(name)
        hits += _post_39_violations(p.read_text(), str(p.relative_to(REPO)))
    return hits


def test_no_post_39_construct_on_the_root_side():
    hits = _all_violations()
    assert not hits, "constructs the 3.9 root interpreter rejects:\n" + "\n".join(hits)
