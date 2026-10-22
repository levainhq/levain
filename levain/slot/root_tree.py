"""The root-owned import tree and the bootstrap entry scripts (CONTRACT §2.1 rows; §1.3 header; §23).

Root never imports the operator's copy of Levain: setup copies the package into the import tree
(``/usr/local/lib/levain-root/`` macOS, ``/usr/lib/levain-root/`` Linux), root 0755, and each entry
script runs the interpreter isolated (``-I``, plus ``-S`` so no site-packages directory, which may not
be root's, reaches ``sys.path``) with the import tree as the ONE path added to the interpreter's own
standard library. One text (:func:`entry_text`) serves both helpers and the service units (§23).

The helper entries hand off to ``levain.slot.install_helper`` / ``levain.slot.runtime_helper`` run as
``__main__``; the service entry to :mod:`levain.slot.service_boot`. Which of either helper's verbs are not
built yet is derived from the helpers themselves (``install_helper.helper_pending``), never written here.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Callable, Iterator, Mapping

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused
from levain.slot.layout import (HELPER_DIR, IMPORT_TREE, ROOT_GROUP, install_helper_path, runtime_helper_path,
                                service_entry_path)

#: The interpreter root runs (a root-owned system path, never one found on a PATH).
ROOT_PYTHON: Mapping[str, str] = {"darwin": "/usr/bin/python3", "linux": "/usr/bin/python3"}

#: Interpreter flags for every root-run Levain program (helpers and services, §1.3).
PY_FLAGS = "-IS"

VENDOR_PREFIX: Mapping[str, Path] = {"darwin": Path("/usr/local/lib/levain-vendor"),
                                     "linux": Path("/usr/lib/levain-vendor")}   # §2.1

#: entry script name -> the module it runs as ``__main__``.
ENTRY_MODULES = {"install-helper": "levain.slot.install_helper", "runtime-helper": "levain.slot.runtime_helper",
                 "service-entry": "levain.slot.service_boot"}
HELPER_MODULES = {k: v for k, v in ENTRY_MODULES.items() if k != "service-entry"}


def entry_text(host: HostOS, entry: str, *, tree: Path | None = None) -> str:
    """The entry script ``entry``: the interpreter isolated, ``sys.path`` = the import tree (``tree``,
    default the host's) + the interpreter's own standard library, then the entry's module run as
    ``__main__`` (its argv is the script's)."""
    if entry not in ENTRY_MODULES:
        raise Refused("bad-token", f"entry: {entry!r}")
    t = str(tree if tree is not None else IMPORT_TREE[host])
    return (
        f"#!{ROOT_PYTHON[host]} {PY_FLAGS}\n"
        f"# Levain {entry} (CONTRACT §2.1, §5, §23). Written by the install helper. Imports only from the\n"
        "# root-owned import tree and the interpreter's standard library.\n"
        "import sys\n"
        f"sys.path.insert(0, {t!r})\n"
        "import runpy\n"
        f"runpy.run_module({ENTRY_MODULES[entry]!r}, run_name=\"__main__\", alter_sys=True)\n"
    )


def helper_entry_text(host: HostOS, helper: str) -> str:
    if helper not in HELPER_MODULES:
        raise Refused("bad-token", f"helper: {helper!r}")
    return entry_text(host, helper)


def _walk(tree: Path) -> Iterator[str]:
    """``tree`` itself first, then every entry below it. The root is yielded because ``os.walk`` follows a
    top-level link: its descendants alone would prove nothing about a ``tree`` that is a link."""
    yield str(tree)
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            yield os.path.join(dirpath, name)


def _entry_problem(tree: Path, p: str, st: os.stat_result) -> str:
    if p == str(tree):
        return "" if stat.S_ISDIR(st.st_mode) else f"{p} is not a real directory"
    return "" if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode) else f"{p} is not a plain file or directory"


def _no_links(tree: Path, *, lstat: Callable[[str], os.stat_result] = os.lstat,
              walk: Callable[[Path], Iterator[str]] = _walk) -> tuple[bool, str]:
    """Before any chown or chmod touches the copy: ``tree`` is a real directory and every entry below it a
    plain directory or regular file, so no recursive chown or chmod can follow a link out of the tree
    (CONTRACT §23). An entry that cannot be examined is refused."""
    for p in walk(tree):
        try:
            st = lstat(p)
        except OSError as e:
            return False, f"cannot examine {p} ({e.strerror or e}); refusing"
        problem = _entry_problem(tree, p, st)
        if problem:
            return False, problem
    return True, ""


def _no_foreign_entries(tree: Path, *, lstat: Callable[[str], os.stat_result] = os.lstat,
                        walk: Callable[[Path], Iterator[str]] = _walk) -> tuple[bool, str]:
    """After the chown and chmod: ``tree`` is a real directory and every entry under it a plain directory or
    regular file owned by root, writable by root alone. A symlink would let an import resolve outside the
    tree."""
    for p in walk(tree):
        try:
            st = lstat(p)
        except OSError as e:
            return False, f"cannot examine {p} ({e.strerror or e}); refusing"
        problem = _entry_problem(tree, p, st)
        if problem:
            return False, problem
        if st.st_uid != 0 or st.st_mode & 0o022:
            return False, f"{p} is not root's alone"
    return True, ""


def _root_owned_ancestors(path: Path, *, lstat: Callable[[str], os.stat_result] = os.lstat,
                          realpath: Callable[[str], str] = os.path.realpath) -> tuple[bool, str]:
    """The directory root's code goes in can be changed by root alone: otherwise its owner could swap
    the tree root imports from (an Intel Homebrew install, for one, owns /usr/local/lib).

    The deepest part of ``path`` that exists is resolved through every link, and it and every one of
    the RESOLVED path's ancestors must be root's and writable by root alone, so a link anywhere above
    is judged by where it really leads. ``path`` itself must not be a link: setup would follow it and
    install root's code wherever it points. A path that cannot be examined (a dangling link above it, a
    component that is not a directory, no permission) is refused, never raised."""
    try:
        if stat.S_ISLNK(lstat(str(path)).st_mode):
            return False, f"{path} is a symlink; refusing to install root's code through it"
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, f"cannot examine {path} ({e.strerror or e}); refusing"
    deepest = path
    while True:
        try:
            lstat(str(deepest))
            break
        except FileNotFoundError:
            if deepest.parent == deepest:
                return False, f"no part of {path} exists"
            deepest = deepest.parent
        except OSError as e:
            return False, f"cannot examine {deepest} ({e.strerror or e}); refusing"
    try:
        real = Path(realpath(str(deepest)))
    except OSError as e:
        return False, f"cannot resolve {deepest} ({e.strerror or e}); refusing"
    for d in (real, *real.parents):
        try:
            st = lstat(str(d))
        except OSError as e:
            shown = d if real == deepest else f"{d} (where {deepest} leads)"
            return False, f"cannot examine {shown} ({e.strerror or e}); refusing to install root's code below it"
        if stat.S_ISLNK(st.st_mode):
            return False, f"{d} became a symlink while it was checked; refusing"
        if st.st_uid != 0 or st.st_mode & 0o022:
            shown = d if real == deepest else f"{d} (where {deepest} leads)"
            return False, f"{shown} is not root's alone; refusing to install root's code below it"
    return True, ""


def _tools(host: HostOS) -> dict[str, str]:
    if host == "darwin":
        return {"mkdir": "/bin/mkdir", "rm": "/bin/rm", "cp": "/bin/cp", "chown": "/usr/sbin/chown",
                "chmod": "/bin/chmod", "find": "/usr/bin/find", "mv": "/bin/mv", "test": "/bin/test"}
    return {k: _abs(k) for k in ("mkdir", "rm", "cp", "chown", "chmod", "find", "mv", "test")}


#: The one third-party package the root-run gateway front imports (3b-8a0, CONTRACT c7.4's note): vendored
#: beside ``levain`` so the isolated root interpreter (``-IS``, no site-packages) finds it in the import tree.
H11_MIN = (0, 16, 0)


def _release(version: str) -> tuple[int, int, int]:
    """The release part of ``version`` for the floor: the leading digits of each dot component (up to three),
    zero-padded to three. A local (``+...``) part is dropped; a pre/post/dev suffix ends the release and is
    ignored. ``ValueError`` when the first component has no leading digit."""
    parts: list[int] = []
    for c in version.split("+", 1)[0].split(".")[:3]:
        m = re.match(r"\d+", c)
        if m is None:
            break
        parts.append(int(m.group()))
        if m.end() != len(c):
            break
    if not parts:
        raise ValueError(version)
    return (*parts, 0, 0, 0)[:3]  # type: ignore[return-value]


def _refuse_link(path: Path, what: str) -> None:
    """A package source must be the directory itself: a link is refused, never resolved and copied, so the
    operator sees that their package is a link."""
    if path.is_symlink():
        raise Refused("bad-root", f"the {what} package {path} is a symlink (to {os.readlink(path)}); "
                                  "refusing to vendor through it")


def h11_source() -> Path:
    """The package directory of the h11 the installing Levain itself imports (pure Python, so the copy is
    interpreter-neutral), checked ``>= 0.16.0`` from its ``__version__``. Refuses an h11 that is not an
    ordinary package directory (a zipped or single-file install has nothing to copy, and a symlinked one
    would put a link in root's tree) or is too old."""
    try:
        import h11
    except ImportError as e:
        raise Refused("bad-root", f"h11 is not importable by the installing Levain ({e}); install levain[isolation]")
    f = getattr(h11, "__file__", None)
    if not f or Path(f).name != "__init__.py" or not Path(f).parent.is_dir():
        raise Refused("bad-root", f"h11 is not a package directory ({f!r}); nothing to vendor")
    _refuse_link(Path(f).parent, "h11")
    try:
        have = _release(str(h11.__version__))
    except (AttributeError, ValueError):
        raise Refused("bad-root", f"h11 has no readable version ({getattr(h11, '__version__', None)!r})")
    if have < H11_MIN:
        raise Refused("bad-root", f"h11 {h11.__version__} is older than {'.'.join(map(str, H11_MIN))}")
    return Path(f).parent


def _package_steps(t: dict[str, str], owner: str, tree: Path, source: Path) -> list[Step]:
    """Stage ``source`` (a package directory) into ``tree``, check it, and swap it in (CONTRACT §23):
    the copy is staged beside the live package; links are refused (by lstat) before any chown or chmod;
    the live package is renamed aside, the staged one renamed into place, the old one removed."""
    pkg, staged, old = tree / source.name, tree / f"{source.name}.new", tree / f"{source.name}.old"
    return [
        Step(f"clear {staged} and {old} (left by an interrupted run)", (t["rm"], "-rf", str(staged), str(old))),
        Step(f"copy the {source.name} package to {staged} (symlinks copied as links, then refused)",
             (t["cp"], "-R", "-P", str(source), str(staged))),
        Step(f"drop compiled files copied from {source}",
             (t["find"], str(staged), "-name", "__pycache__", "-prune", "-exec", t["rm"], "-rf", "{}", "+")),
        Step(f"check {staged} holds no link (before any chown or chmod)", call=lambda: _no_links(staged)),
        Step(f"own {staged} as root", (t["chown"], "-R", owner, str(staged))),
        Step(f"make {staged} writable by root alone", (t["chmod"], "-R", "u=rwX,go=rX", str(staged))),
        Step(f"check {staged} holds only root's plain files and directories",
             call=lambda: _no_foreign_entries(staged)),
        Step(f"rename the previous {pkg} aside to {old}", (t["mv"], str(pkg), str(old)),
             skip_if=(t["test"], "!", "-e", str(pkg))),
        Step(f"rename {staged} into place at {pkg}", (t["mv"], str(staged), str(pkg))),
        Step(f"remove {old}", (t["rm"], "-rf", str(old))),
    ]


def tree_steps(host: HostOS, source: Path, *, h11_dir: Path | None = None) -> list[Step]:
    """Copy the ``levain`` package at ``source`` and the h11 package (``h11_dir``, default the installing
    Levain's own, :func:`h11_source`) into the import tree and write the entry scripts. ``source`` is the
    package directory itself. Each package goes through the same staged copy and checks.

    Every step is safe to re-run after a failure at any point (CONTRACT §23)."""
    if source.name != "levain":
        raise Refused("bad-root", f"{source} is not a levain package directory")
    _refuse_link(source, "levain")
    h11_dir = h11_dir if h11_dir is not None else h11_source()
    if h11_dir.name != "h11":
        raise Refused("bad-root", f"{h11_dir} is not an h11 package directory")
    _refuse_link(h11_dir, "h11")
    t = _tools(host)
    tree, helpers, vendor = IMPORT_TREE[host], HELPER_DIR[host], VENDOR_PREFIX[host]
    owner = f"root:{ROOT_GROUP[host]}"
    steps: list[Step] = []
    for d in (tree, helpers, vendor):
        steps += [
            Step(f"check every directory above {d} is root's alone (links resolved), and {d} is no link", call=lambda p=d: _root_owned_ancestors(p)),
            Step(f"create {d}", (t["mkdir"], "-p", "-m", "755", str(d))),
            Step(f"own {d} as root", (t["chown"], owner, str(d))),
            Step(f"set {d} to 0755", (t["chmod"], "0755", str(d))),
        ]
    steps += _package_steps(t, owner, tree, source)
    steps += _package_steps(t, owner, tree, h11_dir)
    for entry, path in (("install-helper", install_helper_path(host)), ("runtime-helper", runtime_helper_path(host)),
                        ("service-entry", service_entry_path(host))):
        steps.append(Step(f"write the {entry} entry script {path}", write=(path, entry_text(host, entry), 0o755)))
    return steps


__all__ = ["ENTRY_MODULES", "H11_MIN", "HELPER_MODULES", "PY_FLAGS", "ROOT_PYTHON", "VENDOR_PREFIX", "entry_text",
           "h11_source", "helper_entry_text", "tree_steps"]
