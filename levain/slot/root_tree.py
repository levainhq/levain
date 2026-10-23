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
import shlex
import stat
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping, Optional

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


#: The one link in the import tree (row 33, DESIGN 1022-8/upgrade §2.3): ``<tree>/current -> gen-<id>``, a RELATIVE
#: link, so it resolves inside the Linux wall's read-only bind of the tree too.
CURRENT = "current"
STAGING = "staging.partial"


def current_path(host: HostOS, *, tree: Path | None = None) -> Path:
    return (tree if tree is not None else IMPORT_TREE[host]) / CURRENT


def boot_path_code(host: HostOS, *, tree: Path | None = None) -> str:
    """The ONE boot line every root-run Levain program starts with (the entry scripts and ``wall.module_argv``):
    ``sys.path`` gets ``realpath(<tree>/current)``, resolved ONCE at process start, so a running process keeps
    importing from its own generation after a flip (Nix's store-path behaviour)."""
    return f"import os, sys; sys.path.insert(0, os.path.realpath({str(current_path(host, tree=tree))!r}))"


def entry_text(host: HostOS, entry: str, *, tree: Path | None = None) -> str:
    """The entry script ``entry``: the interpreter isolated, ``sys.path`` = the import tree's CURRENT generation
    (``tree``, default the host's; :func:`boot_path_code`) + the interpreter's own standard library, then the
    entry's module run as ``__main__`` (its argv is the script's)."""
    if entry not in ENTRY_MODULES:
        raise Refused("bad-token", f"entry: {entry!r}")
    return (
        f"#!{ROOT_PYTHON[host]} {PY_FLAGS}\n"
        f"# Levain {entry} (CONTRACT §2.1, §5, §23). Written by the install helper. Imports only from the\n"
        "# root-owned import tree's current generation and the interpreter's standard library.\n"
        f"{boot_path_code(host, tree=tree)}\n"
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
    h11_floor(Path(f).parent)
    return Path(f).parent


def h11_floor(h11_dir: Path) -> str:
    """h11's version read as TEXT from ``h11_dir/_version.py`` (row 33: root never imports a package to learn its
    version), refused ``bad-root`` below :data:`H11_MIN`."""
    from levain.slot.generation import read_version_text

    try:
        version = read_version_text(h11_dir / "_version.py")
        have = _release(version)
    except (Refused, ValueError):
        raise Refused("bad-root", f"h11 at {h11_dir} has no readable version") from None
    if have < H11_MIN:
        raise Refused("bad-root", f"h11 {version} is older than {'.'.join(map(str, H11_MIN))}")
    return version


def _fsync_generation(gen: Path, tree: Path) -> tuple[bool, str]:
    """§2.3 R3: every file of ``gen``, then every directory bottom-up, then ``tree``; macOS one ``F_FULLFSYNC``. So a
    power loss after the flip can never leave ``current`` naming a generation whose data never reached the disk."""
    import fcntl
    import sys

    def sync(p: str, flags: int) -> None:
        fd = os.open(p, flags | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    try:
        for dirpath, dirnames, filenames in os.walk(gen, topdown=False):
            for name in filenames:
                sync(os.path.join(dirpath, name), os.O_RDONLY)
            sync(dirpath, os.O_RDONLY | os.O_DIRECTORY)
        sync(str(tree), os.O_RDONLY | os.O_DIRECTORY)
        if sys.platform == "darwin":
            fd = os.open(str(tree), os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            finally:
                os.close(fd)
    except OSError as e:
        return False, f"could not make {gen} durable: {e.strerror or e}"
    return True, ""


def _staged_is(target: str, staged: Path) -> tuple[bool, str]:
    """§2.3 R2: the id hashed over the root-owned STAGED copy must be the one planned (the package changed during
    the copy otherwise: ``source-changed``)."""
    from levain.slot.generation import generation_id

    got = generation_id(staged / "levain", staged / "h11")
    if got != target:
        return False, f"source-changed: the staged copy hashes to {got}, the package planned was {target}"
    return True, ""


def _rename(src: Path, dst: Path) -> tuple[bool, str]:
    os.rename(src, dst)
    return True, ""


def stage_steps(host: HostOS, source: Path, h11_dir: Path, target: str, *, current: str | None) -> list[Step]:
    """§2.3 R2 + R3: stage ``source`` (a ``levain`` package dir) and ``h11_dir`` together into
    ``<tree>/staging.partial``, check it (no link before any chown or chmod; root's plain files after; h11's floor read
    as text; the id recomputed over the staged copy equals ``target``), then rename it to ``<tree>/<target>`` and make
    it durable. Both packages live in ONE generation, so levain and h11 are never swapped separately. When
    ``target`` is already ``current`` the partial copy is dropped instead (a no-op repair)."""
    t = _tools(host)
    tree = IMPORT_TREE[host]
    partial, gen = tree / STAGING, tree / target
    steps = _copy_steps(host, source, h11_dir, partial) + [
        Step(f"check the staged copy hashes to {target}", call=lambda: _staged_is(target, partial)),
    ]
    if current == target:
        return steps + [Step(f"drop {partial}: {target} is already current", (t["rm"], "-rf", str(partial)))]
    return steps + [
        Step(f"clear {gen} (a copy an interrupted run left)", (t["rm"], "-rf", str(gen))),
        Step(f"rename {partial} to {gen}", call=lambda: _rename(partial, gen)),
        Step(f"make {gen} durable (every file, every directory, the tree)", call=lambda: _fsync_generation(gen, tree)),
    ]


def _copy_steps(host: HostOS, source: Path, h11_dir: Path, partial: Path) -> list[Step]:
    """R2's copy and its checks into ``partial``, up to (not including) the hash."""
    t = _tools(host)
    owner = f"root:{ROOT_GROUP[host]}"
    return [
        Step(f"clear {partial} (left by an interrupted run)", (t["rm"], "-rf", str(partial))),
        Step(f"create {partial}", (t["mkdir"], "-m", "700", str(partial))),
        Step(f"copy the levain package to {partial / 'levain'} (symlinks copied as links, then refused)",
             (t["cp"], "-R", "-P", str(source), str(partial / "levain"))),
        Step(f"copy the h11 package to {partial / 'h11'} (symlinks copied as links, then refused)",
             (t["cp"], "-R", "-P", str(h11_dir), str(partial / "h11"))),
        Step(f"drop compiled files copied into {partial}",
             (t["find"], str(partial), "-name", "__pycache__", "-prune", "-exec", t["rm"], "-rf", "{}", "+")),
        Step(f"check {partial} holds no link (before any chown or chmod)", call=lambda: _no_links(partial)),
        Step(f"own {partial} as root", (t["chown"], "-R", owner, str(partial))),
        Step(f"make {partial} writable by root alone", (t["chmod"], "-R", "u=rwX,go=rX", str(partial))),
        Step(f"check {partial} holds only root's plain files and directories",
             call=lambda: _no_foreign_entries(partial)),
        Step(f"check the staged h11 is at least {'.'.join(map(str, H11_MIN))} (its version read as text)",
             call=lambda: (True, h11_floor(partial / "h11"))),
    ]


@dataclass
class Staged:
    """What repair's phase A staging learned while it ran (§2.2, §2.3 R2-R3): the id hashed over the STAGED copy
    (None until the hash step ran), or the refusal that step met. The plan cannot know the id: it is the copy's."""

    target: Optional[str] = None
    refused: Optional[Refused] = None
    #: ``<tree>/<target>`` was there before :func:`_place` replaced it (same id, same content: a retained generation),
    #: so a phase-A restore keeps it [r5: L3 r1 complement LOW 4].
    existed: bool = False


def _hash_staged(cell: Staged, partial: Path, source: Path, h11_dir: Path, pinned: Optional[str]) -> tuple[bool, str]:
    """§2.3 R2 [r3: X2-M3]: the id is hashed over the root-owned STAGED copy FIRST, then the same function over the
    source; unequal refuses ``source-changed`` (the package changed during the copy). U13: an interrupted repair's
    journaled generation fixes the target, so a staged id other than ``pinned`` refuses ``source-mismatch:<G>``."""
    from levain.slot.generation import generation_id

    try:
        got = generation_id(partial / "levain", partial / "h11")
        src = generation_id(source, h11_dir)
    except Refused as e:
        cell.refused = e
        return False, f"{e.name}: {e.detail}"
    if src != got:
        cell.refused = Refused("source-changed", f"the staged copy hashes to {got}, {source} now to {src}")
    elif pinned is not None and got != pinned:
        cell.refused = Refused(f"source-mismatch:{pinned}", f"the interrupted repair is moving to {pinned}; "
                                                           f"{source} is {got}. Re-run with that package")
    if cell.refused is not None:
        return False, f"{cell.refused.name}: {cell.refused.detail}"
    cell.target = got
    return True, got


def _place(host: HostOS, tree: Path, partial: Path, cell: Staged, current: Optional[str]) -> tuple[bool, str]:
    """§2.3 R3 for the id the hash step found: dropped when it is ``current`` already (a no-op repair), else any
    ``gen-<id>`` an interrupted run left is removed, the partial renamed to it, and the generation made durable."""
    import subprocess

    target = cell.target
    if target is None:
        return False, "no staged id (the hash step did not run)"
    rm = _tools(host)["rm"]
    if target == current:
        r = subprocess.run((rm, "-rf", str(partial)), capture_output=True, text=True, cwd="/")
        return r.returncode == 0, (f"{target} is already current" if r.returncode == 0 else r.stderr.strip())
    gen = tree / target
    cell.existed = cell.existed or os.path.lexists(gen)
    r = subprocess.run((rm, "-rf", str(gen)), capture_output=True, text=True, cwd="/")
    if r.returncode != 0:
        return False, r.stderr.strip()
    _rename(partial, gen)
    return _fsync_generation(gen, tree)


def stage_hashing_steps(host: HostOS, source: Path, h11_dir: Path, cell: Staged, *, current: Optional[str],
                        pinned: Optional[str] = None, tree: Optional[Path] = None) -> list[Step]:
    """Repair's phase A stage (§2.3 R2 + R3, r3 X2-M3): :func:`stage_steps`'s copy and checks, then the id hashed
    over the STAGED copy (into ``cell``) and checked against the source, then the generation placed and made durable.
    The R3 journal of ``generation:<id>`` is the caller's next step, so it always follows the hash."""
    tree = tree if tree is not None else IMPORT_TREE[host]
    partial = tree / STAGING
    return _copy_steps(host, source, h11_dir, partial) + [
        Step(f"hash the staged copy {partial}; the same hash over {source} must equal it",
             call=lambda: _hash_staged(cell, partial, source, h11_dir, pinned)),
        Step(f"place the staged generation in {tree} (dropped if it is current; else renamed to gen-<id> and made "
             "durable)", call=lambda: _place(host, tree, partial, cell, current)),
    ]


def generation_boot_code(host: HostOS, gen: str, *, tree: Optional[Path] = None) -> str:
    """:func:`boot_path_code` for ONE generation by name (repair's probe and phase B, §2.2): ``sys.path`` gets
    ``realpath(<tree>/<gen>)``, never ``current`` (which still names the old generation until the flip)."""
    from levain.slot.generation import GEN_RE

    if not GEN_RE.fullmatch(gen):
        raise Refused("bad-token", f"generation: {gen!r}")
    path = (tree if tree is not None else IMPORT_TREE[host]) / gen
    return f"import os, sys; sys.path.insert(0, os.path.realpath({str(path)!r}))"


def _flip(tree: Path, target: str) -> tuple[bool, str]:
    """§2.3 R9: ``current.new`` -> ``target``, renamed over ``current`` (rename(2) replaces it atomically: there is
    no instant at which ``current`` names a mixed pair or nothing), then the tree fsynced. Never ``mv``: BSD ``mv``
    onto a link to a directory moves the source INTO it."""
    new, cur = tree / f"{CURRENT}.new", tree / CURRENT
    try:
        st = os.lstat(new)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISLNK(st.st_mode):
            return False, f"bad-root: {new} is not a link"
        os.unlink(new)
    try:
        cst = os.lstat(cur)
        if not stat.S_ISLNK(cst.st_mode):
            return False, f"bad-root: {cur} is not a link"
    except FileNotFoundError:
        pass
    os.symlink(target, new)
    os.replace(new, cur)
    fd = os.open(str(tree), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True, ""


def read_current(tree: Path) -> str | None:
    """What ``<tree>/current`` names, read once at gather (§2.3 R0), or None when it is absent (a pre-generation
    install). ``current``, and a stray ``current.new`` when present (a flip killed between its symlink and its
    rename), must each be a link to a generation id relative to ``tree`` (``gen-<16 hex>``); anything else, or a
    read that fails other than by absence, refuses ``bad-root`` here, before any act."""
    from levain.slot.generation import GEN_RE

    out: str | None = None
    for name in (CURRENT, f"{CURRENT}.new"):
        p = tree / name
        try:
            st = os.lstat(p)
            link = os.readlink(p) if stat.S_ISLNK(st.st_mode) else None
        except FileNotFoundError:
            continue
        except OSError as e:
            raise Refused("bad-root", f"cannot read {p} ({e.strerror or e})") from None
        if link is None or not GEN_RE.fullmatch(link):
            raise Refused("bad-root", f"{p} is not a link to a generation in {tree}")
        if name == CURRENT:
            out = link
    return out


def flip_step(host: HostOS, target: str) -> Step:
    tree = IMPORT_TREE[host]
    return Step(f"flip {current_path(host)} -> {target}", call=lambda: _flip(tree, target))


def entry_steps(host: HostOS) -> list[Step]:
    """The three entry scripts (§2.3 R10: written AFTER the flip, so none ever names a ``current`` not there yet)."""
    return [Step(f"write the {entry} entry script {path}", write=(path, entry_text(host, entry), 0o755))
            for entry, path in (("install-helper", install_helper_path(host)),
                                ("runtime-helper", runtime_helper_path(host)),
                                ("service-entry", service_entry_path(host)))]


def _gc(tree: Path, keep: frozenset[str], rm: str) -> tuple[bool, str]:
    import subprocess

    gone = sorted(n for n in os.listdir(tree) if (n.startswith("gen-") and n not in keep) or n in ("levain", "h11"))
    for n in gone:
        r = subprocess.run([rm, "-rf", str(tree / n)], capture_output=True, text=True, cwd="/")
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()
    return True, ", ".join(gone)


def gc_step(host: HostOS, keep: Iterable[str]) -> Step:
    """§2.3 R13: every ``gen-*`` but ``keep`` (``current`` and the one it replaced), and the legacy top-level
    ``levain``/``h11`` a pre-generation install left."""
    tree, keep = IMPORT_TREE[host], frozenset(keep)
    return Step(f"remove old generations and the legacy tree from {tree} (keep {', '.join(sorted(keep))})",
                call=lambda: _gc(tree, keep, _tools(host)["rm"]))


def dir_steps(host: HostOS) -> list[Step]:
    t = _tools(host)
    owner = f"root:{ROOT_GROUP[host]}"
    steps: list[Step] = []
    for d in (IMPORT_TREE[host], HELPER_DIR[host], VENDOR_PREFIX[host]):
        steps += [
            Step(f"check every directory above {d} is root's alone (links resolved), and {d} is no link", call=lambda p=d: _root_owned_ancestors(p)),
            Step(f"create {d}", (t["mkdir"], "-p", "-m", "755", str(d))),
            Step(f"own {d} as root", (t["chown"], owner, str(d))),
            Step(f"set {d} to 0755", (t["chmod"], "0755", str(d))),
        ]
    return steps


def check_sources(source: Path, h11_dir: Path) -> None:
    if source.name != "levain":
        raise Refused("bad-root", f"{source} is not a levain package directory")
    _refuse_link(source, "levain")
    if h11_dir.name != "h11":
        raise Refused("bad-root", f"{h11_dir} is not an h11 package directory")
    _refuse_link(h11_dir, "h11")


def check_source_writers(parent: Path, *, operator_uid: int, operator_gids: Iterable[int],
                         group_names: Mapping[int, str] | None = None,
                         lstat: Callable[[str], os.stat_result] = os.lstat,
                         walk: Callable[..., Iterator] = os.walk) -> None:
    """Row 33 §2.1 [r2: X-8; r3: F9 (a)-(c)]: who may write the ``--source`` package, checked BEFORE any other root
    read of it (the generation hash included). ``parent`` is ``realpath(PATH/..)``, so the ancestor chain checked is
    the RESOLVED one. Every ancestor of ``parent`` up to ``/``, ``parent`` itself, and every entry below
    ``parent/levain`` and ``parent/h11`` must be owned by root or the operator and not other-writable (sticky or
    not); a group-writable one is accepted only when its group is in ``operator_gids``: the groups whose COMPLETE
    membership (the member list AND every user whose primary gid it is) is the operator alone (L3 r1: Ubuntu's
    user-private group passes, macOS ``staff`` never does). Else ``bad-source``, naming the remedy on that exact path
    (``chmod g-w``; ``chmod -R g-w`` for a directory below a package, never on an ancestor). A link below either package
    reads mode 0777 on Linux and is refused here; :func:`levain.slot.generation.generation_id` refuses links on both
    hosts. ACLs are not read (§2.1 (d), a known edge). Known edge (L3 r1): the check runs at R0 and the hash and copy
    later, so a writer the check admits (the operator, root) can change the tree between; §2.1 does not claim it."""
    trusted, names = frozenset(operator_gids), group_names or {}
    below = (os.path.join(str(parent), "levain"), os.path.join(str(parent), "h11"))

    def check(p: str) -> None:
        try:
            st = lstat(p)
        except OSError as e:
            raise Refused("bad-source", f"cannot read {p} ({e.strerror or e})") from None
        if st.st_uid not in (0, operator_uid):
            raise Refused("bad-source", f"{p} is owned by uid {st.st_uid}, neither root nor the operator")
        if st.st_mode & stat.S_IWOTH:
            raise Refused("bad-source", f"{p} is writable by every user")
        if st.st_mode & stat.S_IWGRP and st.st_gid not in trusted:
            grp = f"{names[st.st_gid]} (gid {st.st_gid})" if st.st_gid in names else f"gid {st.st_gid}"
            rec = stat.S_ISDIR(st.st_mode) and (p in below or p.startswith(tuple(b + os.sep for b in below)))
            raise Refused("bad-source", f"{p} is writable by group {grp}, which holds an account other than the "
                                        f"operator. Run `chmod {'-R ' if rec else ''}g-w {shlex.quote(p)}`, then "
                                        "repair again")

    chain, p = [], str(parent)
    while True:
        chain.append(p)
        up = os.path.dirname(p)
        if up == p:
            break
        p = up
    for p in reversed(chain):
        check(p)

    def fail(e: OSError) -> None:
        raise Refused("bad-source", f"cannot read {e.filename} ({e.strerror or e})")

    for name in ("levain", "h11"):
        top = os.path.join(str(parent), name)
        check(top)
        for root, dirs, files in walk(top, onerror=fail, followlinks=False):
            for n in sorted(dirs) + sorted(files):
                check(os.path.join(root, n))


def tree_steps(host: HostOS, source: Path, *, h11_dir: Path | None = None, generation: str | None = None,
               current: str | None = None) -> list[Step]:
    """Setup (§2.5): stage the ``levain`` package at ``source`` and h11 (``h11_dir``, default the installing Levain's
    own, :func:`h11_source`) as ONE generation ``gen-<id>`` in the import tree, flip ``current`` to it, write the entry
    scripts. ``generation``: the id planned (default: computed over ``source`` and ``h11_dir`` now); ``current``: what
    ``current`` names already (a re-run).

    Every step is safe to re-run after a failure at any point (CONTRACT §23)."""
    from levain.slot.generation import generation_id

    h11_dir = h11_dir if h11_dir is not None else h11_source()
    check_sources(source, h11_dir)
    target = generation if generation is not None else generation_id(source, h11_dir)
    return [*dir_steps(host), *stage_steps(host, source, h11_dir, target, current=current), flip_step(host, target),
            *entry_steps(host)]


__all__ = ["CURRENT", "ENTRY_MODULES", "H11_MIN", "HELPER_MODULES", "PY_FLAGS", "ROOT_PYTHON", "STAGING", "Staged",
           "generation_boot_code", "stage_hashing_steps",
           "VENDOR_PREFIX", "boot_path_code", "check_source_writers", "check_sources", "current_path", "dir_steps",
           "entry_steps", "entry_text", "flip_step", "gc_step", "h11_floor", "h11_source", "helper_entry_text",
           "read_current", "stage_steps", "tree_steps"]
