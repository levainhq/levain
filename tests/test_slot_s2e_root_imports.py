"""S1 session 2e: every module the root helper path imports loads on the host's own root interpreter.

Root runs ``/usr/bin/python3`` with :data:`levain.slot.root_tree.PY_FLAGS` (macOS: CLT 3.9), so nothing on that path
may import ``tomllib`` (3.11+) or the operator-side ``levain.host_guard`` (which does). Each module is imported in a
FRESH interpreter that refuses both names on the meta path; under ``/usr/bin/python3`` when it is a 3.9, the same
imports also prove the path parses and runs its module bodies on 3.9.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from levain.slot.root_tree import ENTRY_MODULES, PY_FLAGS

REPO = Path(__file__).resolve().parents[1]
ROOT_PYTHON = "/usr/bin/python3"
BLOCKED = ("tomllib", "levain.host_guard")

_LISTED = ("records", "control", "render", "stage", "root_intent", "end", "wall", "forwarder", "runtime_grammar",
           "install_helper", "layout", "ids", "root_tree", "service_boot")
#: The brief's list, plus every root entry module that exists (runtime_helper joins once it is built).
ROOT_MODULES = tuple(sorted({f"levain.slot.{m}" for m in _LISTED}
                            | {m for m in ENTRY_MODULES.values() if importlib.util.find_spec(m) is not None}))

_CHILD = """
import sys
sys.path.insert(0, {repo!r})
BLOCKED = {blocked!r}
class Refuse:
    def find_spec(self, name, path=None, target=None):
        if name in BLOCKED or any(name.startswith(b + ".") for b in BLOCKED):
            raise ModuleNotFoundError("root path refuses " + name, name=name)
        return None
assert not [b for b in BLOCKED if b in sys.modules], "preloaded"
sys.meta_path.insert(0, Refuse())
import importlib
importlib.import_module({module!r})
print("ok", sorted(m for m in sys.modules if m.startswith("levain")))
"""


def _flags() -> list[str]:
    return ["-" + f for f in PY_FLAGS.lstrip("-")]


def _import_in(python: str, module: str) -> subprocess.CompletedProcess:
    code = _CHILD.format(repo=str(REPO), blocked=BLOCKED, module=module)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    return subprocess.run([python, *_flags(), "-c", code], capture_output=True, text=True, timeout=60, env=env)


def _assert_imports(python: str, module: str) -> None:
    r = _import_in(python, module)
    assert r.returncode == 0 and r.stdout.startswith("ok"), f"{python}: import {module}\n{r.stderr[-2000:]}"
    assert "levain.host_guard" not in r.stdout


def _root_python_39() -> str | None:
    """Why ``/usr/bin/python3`` cannot stand in for the root interpreter here, or None when it can."""
    if not os.path.exists(ROOT_PYTHON):
        return f"{ROOT_PYTHON} does not exist on this host"
    r = subprocess.run([ROOT_PYTHON, *_flags(), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return f"{ROOT_PYTHON} did not run (exit {r.returncode}): {r.stderr.strip()[:200]}"
    if r.stdout.strip() != "3.9":
        return f"{ROOT_PYTHON} reports {r.stdout.strip()}, not the 3.9 the macOS root path runs"
    return None


def test_module_list_covers_the_entries():
    assert {"levain.slot.install_helper", "levain.slot.service_boot"} <= set(ROOT_MODULES)


def test_the_blocker_refuses():
    """The finder must bite, or every import below proves nothing."""
    r = _import_in(sys.executable, "levain.host_guard")
    assert r.returncode != 0 and "root path refuses" in r.stderr


@pytest.mark.parametrize("module", ROOT_MODULES)
def test_root_module_imports_without_tomllib_or_host_guard(module):
    _assert_imports(sys.executable, module)


@pytest.mark.parametrize("module", ROOT_MODULES)
def test_root_module_imports_on_root_python_39(module):
    why = _root_python_39()
    if why:
        pytest.skip(why)
    _assert_imports(ROOT_PYTHON, module)
