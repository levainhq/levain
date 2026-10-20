"""The root-owned import tree and the two helper entry scripts (CONTRACT §2.1 rows; §1.3 header).

Root never imports the operator's copy of Levain: setup copies the package into the import tree
(``/usr/local/lib/levain-root/`` macOS, ``/usr/lib/levain-root/`` Linux), root 0755, and each helper
entry script runs the interpreter isolated (``-I``, plus ``-S`` so no site-packages directory, which
may not be root's, reaches ``sys.path``) with the import tree as the ONE path added to the
interpreter's own standard library.

The entry scripts hand off to ``levain.slot.install_helper`` / ``levain.slot.runtime_helper`` run as
``__main__``. Neither module has a ``__main__`` entry yet and the runtime helper module does not
exist yet: :data:`PENDING` says so in the plan output.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.layout import HELPER_DIR, IMPORT_TREE, ROOT_GROUP, install_helper_path, runtime_helper_path

#: The interpreter root runs (a root-owned system path, never one found on a PATH).
ROOT_PYTHON: Mapping[str, str] = {"darwin": "/usr/bin/python3", "linux": "/usr/bin/python3"}

#: Interpreter flags for every root-run Levain program (helpers and services, §1.3).
PY_FLAGS = "-IS"

VENDOR_PREFIX: Mapping[str, Path] = {"darwin": Path("/usr/local/lib/levain-vendor"),
                                     "linux": Path("/usr/lib/levain-vendor")}   # §2.1

HELPER_MODULES = {"install-helper": "levain.slot.install_helper", "runtime-helper": "levain.slot.runtime_helper"}

PENDING = ("levain.slot.install_helper has no __main__ entry yet",
           "levain.slot.runtime_helper does not exist yet")


def helper_entry_text(host: HostOS, helper: str) -> str:
    """The entry script for ``helper``: the interpreter isolated, ``sys.path`` = the import tree +
    the interpreter's own standard library, then the helper's module run as ``__main__``."""
    if helper not in HELPER_MODULES:
        raise Refused("bad-token", f"helper: {helper!r}")
    tree = str(IMPORT_TREE[host])
    return (
        f"#!{ROOT_PYTHON[host]} {PY_FLAGS}\n"
        f"# Levain {helper} (CONTRACT §2.1, §5). Written by the install helper. Imports only from the\n"
        "# root-owned import tree and the interpreter's standard library.\n"
        "import sys\n"
        f"sys.path.insert(0, {tree!r})\n"
        "import runpy\n"
        f"runpy.run_module({HELPER_MODULES[helper]!r}, run_name=\"__main__\", alter_sys=True)\n"
    )


def _no_foreign_entries(tree: Path) -> tuple[bool, str]:
    """After the copy: every entry under ``tree`` is a plain directory or regular file owned by root,
    writable by root alone. A symlink would let an import resolve outside the tree."""
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                return False, f"{p} is not a plain file or directory"
            if st.st_uid != 0 or st.st_mode & 0o022:
                return False, f"{p} is not root's alone"
    return True, ""


def _root_owned_ancestors(path: Path) -> tuple[bool, str]:
    """Every existing ancestor of ``path`` is root's and writable by root alone: otherwise its owner
    could swap the tree root imports from (an Intel Homebrew install, for one, owns /usr/local/lib)."""
    for anc in path.parents:
        try:
            st = os.lstat(anc)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            st = os.stat(anc)
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False, f"{anc} is not root's alone; refusing to install root's code below it"
    return True, ""


def tree_steps(host: HostOS, source: Path) -> list[Step]:
    """Copy the ``levain`` package at ``source`` into the import tree, replacing what was there, and
    write both helper entry scripts. ``source`` is the package directory itself."""
    if source.name != "levain":
        raise Refused("bad-root", f"{source} is not a levain package directory")
    if host == "darwin":
        t = {k: v for k, v in (("mkdir", "/bin/mkdir"), ("rm", "/bin/rm"), ("cp", "/bin/cp"),
                                ("chown", "/usr/sbin/chown"), ("chmod", "/bin/chmod"), ("find", "/usr/bin/find"))}
    else:
        t = {k: _abs(k) for k in ("mkdir", "rm", "cp", "chown", "chmod", "find")}
    tree, helpers, vendor = IMPORT_TREE[host], HELPER_DIR[host], VENDOR_PREFIX[host]
    owner = f"root:{ROOT_GROUP[host]}"
    pkg, staged = tree / "levain", tree / "levain.new"
    steps: list[Step] = []
    for d in (tree, helpers, vendor):
        steps += [
            Step(f"check every directory above {d} is root's alone", call=lambda p=d: _root_owned_ancestors(p)),
            Step(f"create {d}", (t["mkdir"], "-p", "-m", "755", str(d))),
            Step(f"own {d} as root", (t["chown"], owner, str(d))),
            Step(f"set {d} to 0755", (t["chmod"], "0755", str(d))),
        ]
    steps += [
        Step(f"clear {staged}", (t["rm"], "-rf", str(staged))),
        Step(f"copy the levain package to {staged} (symlinks copied as links, then refused)",
             (t["cp"], "-R", "-P", str(source), str(staged))),
        Step(f"drop compiled files copied from {source}",
             (t["find"], str(staged), "-name", "__pycache__", "-prune", "-exec", t["rm"], "-rf", "{}", "+")),
        Step(f"own {staged} as root", (t["chown"], "-R", owner, str(staged))),
        Step(f"make {staged} writable by root alone", (t["chmod"], "-R", "u=rwX,go=rX", str(staged))),
        Step(f"check {staged} holds only root's plain files and directories",
             call=lambda: _no_foreign_entries(staged)),
        Step(f"remove the previous {pkg}", (t["rm"], "-rf", str(pkg))),
        Step(f"put the new tree in place at {pkg}", ("/bin/mv" if host == "darwin" else _abs("mv"), str(staged), str(pkg))),
    ]
    for helper, path in (("install-helper", install_helper_path(host)), ("runtime-helper", runtime_helper_path(host))):
        steps.append(Step(f"write the {helper} entry script {path}", write=(path, helper_entry_text(host, helper), 0o755)))
    return steps


__all__ = ["HELPER_MODULES", "PENDING", "PY_FLAGS", "ROOT_PYTHON", "VENDOR_PREFIX", "helper_entry_text", "tree_steps"]
