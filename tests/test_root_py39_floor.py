"""Root-side code runs under the system ``/usr/bin/python3 -IS`` (``levain.slot.root_tree.ROOT_PYTHON``), whose floor
is 3.9 while the operator side and CI run 3.12+. This imports EVERY ``levain.slot`` and ``levain.broker`` module in one
fresh floor interpreter and names each that fails.

It catches IMPORT-TIME failures only (a ``X | Y`` evaluated at module level, a missing stdlib module, a syntax
newer than 3.9). It cannot catch a CALL-TIME one such as ``binascii.a2b_base64(strict_mode=)`` (3.11+), which
compiles and imports fine and fails when the line runs; ``test_no_post_39_keyword_on_the_root_side`` greps for the
known ones and the sweep in ``project_memory/1022-8/py39/`` is the wider net. Skipped by name when
``/usr/bin/python3`` is absent or is 3.12+ (the per-module twin for the macOS 3.9 case is ``test_slot_s2e_root_imports``).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ROOT_PYTHON = "/usr/bin/python3"
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


def _floor_python() -> tuple[str | None, str]:
    if not os.path.exists(ROOT_PYTHON):
        return None, f"{ROOT_PYTHON} does not exist on this host"
    r = subprocess.run([ROOT_PYTHON, "-I", "-c", "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None, f"{ROOT_PYTHON} did not run (exit {r.returncode})"
    if int(r.stdout.strip()) >= 312:
        return None, f"{ROOT_PYTHON} is {r.stdout.strip()} (3.12+), not a floor interpreter"
    return ROOT_PYTHON, ""


def test_every_root_side_module_imports_on_the_floor_interpreter():
    python, why = _floor_python()
    if python is None:
        pytest.skip(why)
    code = _CHILD.format(repo=str(REPO), vendored=VENDORED, modules=_modules())
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    r = subprocess.run([python, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0, "root-side modules that do not import on the floor interpreter:\n" + r.stdout + r.stderr[-1000:]


#: Keyword arguments newer than 3.9 that compile and import and then raise TypeError when the call runs.
_POST_39_KEYWORDS = re.compile(r"\bstrict_mode\s*=|\bdataclass\([^#\n]*\b(?:slots|kw_only)\s*=|\bzip\([^#\n]*\bstrict\s*=")


def test_no_post_39_keyword_on_the_root_side():
    hits = []
    for name in _modules():
        p = REPO / (name.replace(".", "/") + ".py")
        p = p if p.exists() else REPO / name.replace(".", "/") / "__init__.py"
        for n, line in enumerate(p.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if _POST_39_KEYWORDS.search(code):
                hits.append(f"{p.relative_to(REPO)}:{n}: {line.strip()}")
    assert not hits, "keyword arguments the 3.9 root interpreter rejects:\n" + "\n".join(hits)
