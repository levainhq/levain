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
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Mapping, Optional

from levain.firing.hands import HostOS, Step, _abs
from levain.slot import Refused, write_all
from levain.slot.layout import (HELPER_DIR, IMPORT_TREE, ROOT_GROUP, STORE, install_helper_path, runtime_helper_path,
                                service_entry_path)

#: The interpreter root runs (a root-owned system path, never one found on a PATH).
ROOT_PYTHON: Mapping[str, str] = {"darwin": "/usr/bin/python3", "linux": "/usr/bin/python3"}

#: Interpreter flags for every root-run Levain program (helpers and services, §1.3).
PY_FLAGS = "-IS"

VENDOR_PREFIX: Mapping[str, Path] = {"darwin": Path("/usr/local/lib/levain-vendor"),
                                     "linux": Path("/usr/lib/levain-vendor")}   # §2.1

#: entry script name -> the module it runs as ``__main__``.
ENTRY_MODULES = {"install-helper": "levain.slot.install_helper", "runtime-helper": "levain.slot.runtime_helper",
                 "service-entry": "levain.slot.service_boot",
                 # s5 §5b (head ruling 1022+19 Q4): the importer's entry, the argv[0] wall.export_launch_spec builds
                 # (HELPER_DIR/export-importer); the verifier's (HELPER_DIR/export-verifier, 5d-i).
                 "export-importer": "levain.slot.importer", "export-verifier": "levain.slot.verify_run"}
#: The sudo helpers only: the service's boot entry and an export target's entry are not helpers (head ruling
#: 1022+19 session-1 Q2).
HELPER_MODULES = {k: v for k, v in ENTRY_MODULES.items()
                  if k not in ("service-entry", "export-importer", "export-verifier")}


#: The one link in the import tree (row 33, DESIGN 1022-8/upgrade §2.3): ``<tree>/current -> gen-<id>``, a RELATIVE
#: link, so it resolves inside the Linux wall's read-only bind of the tree too.
CURRENT = "current"
STAGING = "staging.partial"


#: Root's allowed-signers file for the authority log and the confirm gate (14b DESIGN_REV2 S5): ``L/policy/``, beside
#: the queue broker's ``allowed_signers.json``. Written only by the install helper's ``enrol-authority-signer`` and
#: ``remove-authority-signer``; the path DESIGN_REV3 §10 S6 hands the production gate's coverage source.
AUTHORITY_SIGNERS = "authority_signers"


def authority_signers_path(host: HostOS) -> Path:
    return STORE[host] / "policy" / AUTHORITY_SIGNERS


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


def stage_steps(host: HostOS, source: Path, h11_dir: Path, target: str, *, current: str | None,
                fault: Optional[Callable[[str], None]] = None) -> list[Step]:
    """§2.3 R2 + R3: stage ``source`` (a ``levain`` package dir) and ``h11_dir`` together into
    ``<tree>/staging.partial``, check it (no link before any chown or chmod; root's plain files after; h11's floor read
    as text; the id recomputed over the staged copy equals ``target``), then rename it to ``<tree>/<target>`` and make
    it durable. Both packages live in ONE generation, so levain and h11 are never swapped separately. When
    ``target`` is already ``current`` the partial copy is dropped instead (a no-op repair)."""
    t = _tools(host)
    tree = IMPORT_TREE[host]
    partial, gen = tree / STAGING, tree / target
    steps = _copy_steps(host, source, h11_dir, partial, fault=fault) + [
        Step(f"check the staged copy hashes to {target}", call=lambda: _staged_is(target, partial)),
    ]
    if current == target:
        return steps + [Step(f"drop {partial}: {target} is already current", (t["rm"], "-rf", str(partial)))]
    return steps + [
        Step(f"clear {gen} (a copy an interrupted run left)", (t["rm"], "-rf", str(gen))),
        Step(f"rename {partial} to {gen}", call=lambda: _rename(partial, gen)),
        Step(f"make {gen} durable (every file, every directory, the tree)", call=lambda: _fsync_generation(gen, tree)),
    ]


def _copy_steps(host: HostOS, source: Path, h11_dir: Path, partial: Path, *,
                fault: Optional[Callable[[str], None]] = None) -> list[Step]:
    """R2's copy and its checks into ``partial``, up to (not including) the hash. ``fault``: the launcher's hook
    (§5 [F7]), called with ``mid-copy`` after the levain copy and before the h11 copy; no step without it."""
    t = _tools(host)
    owner = f"root:{ROOT_GROUP[host]}"
    mid = [Step("fault point R2 mid-copy", call=lambda: (fault("mid-copy"), (True, ""))[1])] if fault else []
    return [
        Step(f"clear {partial} (left by an interrupted run)", (t["rm"], "-rf", str(partial))),
        Step(f"create {partial}", (t["mkdir"], "-m", "700", str(partial))),
        Step(f"copy the levain package to {partial / 'levain'} (symlinks copied as links, then refused)",
             (t["cp"], "-R", "-P", str(source), str(partial / "levain"))),
        *mid,
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
    #: ``<tree>/<target>`` was there before this run touched it (same id, same content: a retained generation), so a
    #: phase-A restore keeps it [r5: L3 r1 complement LOW 4]. Recorded by the hash step, before it sets ``target``
    #: [r5: L3 r2 codex MED], so no act between the hash and :func:`_place` can leave a target without it.
    existed: bool = False
    #: :func:`_place` began replacing ``<tree>/<target>`` (its ``rm -rf``): only then may a restore remove it.
    placed: bool = False
    #: [r6: L3 s3 r1 codex MED] The refusal the run meets only once EVERY step succeeded (a torn resume's: "was
    #: removed"). A failed step is ``step-failed`` instead, so a removal that did not happen is never reported done.
    then_refuse: Optional[Refused] = None


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
    cell.existed = os.path.lexists(partial.parent / got)     # before ``target``: the restore reads them together
    cell.target = got
    return True, got


def _place(host: HostOS, tree: Path, partial: Path, cell: Staged, current: Optional[str]) -> tuple[bool, str]:
    """§2.3 R3 for the id the hash step found: dropped when it is ``current`` already (a no-op repair), else any
    ``gen-<id>`` an interrupted run left is removed, the partial renamed to it, and the generation made durable."""
    import subprocess

    from levain.launch import child_env

    target = cell.target
    if target is None:
        return False, "no staged id (the hash step did not run)"
    rm = _tools(host)["rm"]
    if target == current:
        r = subprocess.run((rm, "-rf", str(partial)), capture_output=True, text=True, cwd="/", env=child_env())
        return r.returncode == 0, (f"{target} is already current" if r.returncode == 0 else r.stderr.strip())
    gen = tree / target
    cell.existed = cell.existed or os.path.lexists(gen)
    cell.placed = True                                       # from the rm on, the target is this run's
    r = subprocess.run((rm, "-rf", str(gen)), capture_output=True, text=True, cwd="/", env=child_env())
    if r.returncode != 0:
        return False, r.stderr.strip()
    _rename(partial, gen)
    return _fsync_generation(gen, tree)


def stage_hashing_steps(host: HostOS, source: Path, h11_dir: Path, cell: Staged, *, current: Optional[str],
                        pinned: Optional[str] = None, tree: Optional[Path] = None,
                        fault: Optional[Callable[[str], None]] = None) -> list[Step]:
    """Repair's phase A stage (§2.3 R2 + R3, r3 X2-M3): :func:`stage_steps`'s copy and checks, then the id hashed
    over the STAGED copy (into ``cell``) and checked against the source, then the generation placed and made durable.
    The R3 journal of ``generation:<id>`` is the caller's next step, so it always follows the hash."""
    tree = tree if tree is not None else IMPORT_TREE[host]
    partial = tree / STAGING
    return _copy_steps(host, source, h11_dir, partial, fault=fault) + [
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


def _flip(tree: Path, target: str, *, fault: Optional[Callable[[str], None]] = None) -> tuple[bool, str]:
    """§2.3 R9: ``current.new`` -> ``target``, renamed over ``current`` (rename(2) replaces it atomically: there is
    no instant at which ``current`` names a mixed pair or nothing), then the tree fsynced. Never ``mv``: BSD ``mv``
    onto a link to a directory moves the source INTO it. ``fault`` (§5 [F7]): called with ``a`` after the symlink and
    before the rename, ``b`` after the rename and before the fsync."""
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
    if fault is not None:
        fault("a")
    os.replace(new, cur)
    if fault is not None:
        fault("b")
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


def flip_step(host: HostOS, target: str, *, fault: Optional[Callable[[str], None]] = None) -> Step:
    tree = IMPORT_TREE[host]
    return Step(f"flip {current_path(host)} -> {target}", call=lambda: _flip(tree, target, fault=fault))


def entry_steps(host: HostOS) -> list[Step]:
    """Every :data:`ENTRY_MODULES` entry script (§2.3 R10: written AFTER the flip, so none ever names a ``current``
    not there yet)."""
    return [Step(f"write the {entry} entry script {path}", write=(path, entry_text(host, entry), 0o755))
            for entry, path in (("install-helper", install_helper_path(host)),
                                ("runtime-helper", runtime_helper_path(host)),
                                ("service-entry", service_entry_path(host)),
                                ("export-importer", HELPER_DIR[host] / "export-importer"),
                                ("export-verifier", HELPER_DIR[host] / "export-verifier"))]


def _gc(tree: Path, keep: frozenset[str], rm: str) -> tuple[bool, str]:
    import subprocess

    from levain.launch import child_env

    gone = sorted(n for n in os.listdir(tree) if (n.startswith("gen-") and n not in keep) or n in ("levain", "h11"))
    for n in gone:
        r = subprocess.run([rm, "-rf", str(tree / n)], capture_output=True, text=True, cwd="/", env=child_env())
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


# --- the vendor CLIs (head ruling 1026+8 VENDOR BINARIES; CONTRACT §2.1 row "vendor prefix"; BUILD_PLAN §2e 14) ---

#: The vendor CLIs a slot session runs, each at ``VENDOR_PREFIX/bin/<name>`` (``start.vendor_argv`` execs it).
VENDORS = ("claude", "codex")
#: Where setup and repair look for each vendor CLI, in this order; the first entry that exists is the one used. Root
#: never reads the operator's PATH (sudo resets it). ``~`` is the operator's home (Claude Code's native installer
#: links ``~/.local/bin/claude``; Homebrew's npm links its ``bin``).
VENDOR_SEARCH: Mapping[str, tuple] = {"darwin": ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"),
                                      "linux": ("~/.local/bin", "/usr/local/bin", "/usr/bin")}
#: The target triple an npm launcher's native binary lives under (``vendor/<triple>/bin/<name>``), by host and
#: ``os.uname().machine``.
VENDOR_TRIPLES: Mapping[tuple, str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin", ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-musl", ("linux", "aarch64"): "aarch64-unknown-linux-musl"}
#: A native executable's first four bytes: Mach-O 64-bit or universal on macOS, ELF on Linux. A script is never
#: copied: its interpreter is not on the wall's PATH.
NATIVE_MAGIC: Mapping[str, tuple] = {"darwin": (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"), "linux": (b"\x7fELF",)}
_CHUNK = 1 << 20


@dataclass(frozen=True)
class VendorUnit:
    """A vendor whose native binary is one file of a package it reads from its own path (the package root is the
    directory above the binary's ``bin``). Root keeps each version of the package whole in its own directory,
    ``<prefix>/<name>/<version>-<digest>/``, and ``bin/<name>`` (where :func:`vendor_bin` names it) is the ONE active
    name: a link to ``../<name>/<version>-<digest>/bin/<name>``, switched by one rename (head ruling V8; the package is
    found through the link: measured with codex 0.162.1 by lane vbf2 1026+14). ``manifest``: the file at the root
    that marks the package; ``required``: the files it must hold (relative to the root) and each one's exact mode;
    ``trees``: the directories beside ``bin`` copied whole, each one required; ``exec_trees``: the trees whose every
    file must be 0755 (as must every file in a directory named ``bin``). Only the manifest, the files directly in
    ``bin`` and the ``trees`` are copied."""

    manifest: str
    required: Mapping[str, int]
    trees: tuple
    exec_trees: tuple = ()


#: Codex's package (``codex-package.json`` names ``entrypoint bin/codex``, ``resourcesDir codex-resources``,
#: ``pathDir codex-path``; the binary's strings name ``bin/codex-code-mode-host`` and ``codex-path/rg`` as the
#: package's own files). Claude Code is not here: its native binary links only system libraries, so its unit is the
#: one file ``bin/claude``. Both measured on a Mac (codex 0.162.1, claude 2.1.310) by lane vbf1 1026+10.
VENDOR_UNITS: Mapping[str, VendorUnit] = {
    "codex": VendorUnit("codex-package.json",
                        {"codex-package.json": 0o644, "bin/codex": 0o755, "bin/codex-code-mode-host": 0o755,
                         "codex-path/rg": 0o755},
                        ("codex-path", "codex-resources"), ("codex-path",))}
#: The name prefix of a package's staging directory beside its versions while a new version is copied; each run's is
#: unique (head ruling V15), and one left by a crashed run is removed under the lock.
_STAGING = ".staging."
#: The lock file in ``<prefix>/<name>/`` a package copy or prune holds (exclusive flock) for its whole run (head
#: ruling V15).
_VENDOR_LOCK = ".lock"
#: DECISION (lane vbf3): how long a package copy waits for another run's lock before it fails, the active version
#: unchanged. A whole codex copy is one package's bytes; a holder past this is wedged, and repair must still finish.
VENDOR_LOCK_WAIT_S = 300.0


def vendor_bin(host: HostOS, name: str, *, prefix: Optional[Path] = None) -> Path:
    return (prefix if prefix is not None else VENDOR_PREFIX[host]) / "bin" / name


def _want_mode(unit: VendorUnit, rel: str) -> Optional[int]:
    """The exact mode ``rel`` of the package must have, or None (then 0755 or 0644, as its source is)."""
    if rel in unit.required:
        return unit.required[rel]
    parts = rel.split("/")
    if (len(parts) > 1 and parts[-2] == "bin") or parts[0] in unit.exec_trees:
        return 0o755
    return None


@dataclass(frozen=True)
class VendorFound:
    """``found``: the search entry that exists; ``binary``: the native executable it leads to (links resolved, an npm
    launcher followed to its package's native binary), or None, and then ``why``."""

    name: str
    found: str
    binary: Optional[str]
    why: str = ""


def _open_regular(path: str) -> int:
    """``path`` opened read-only, not through a last link and without blocking (a FIFO at a search path must not
    hang root); OSError unless it is a regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _head(path: str) -> bytes:
    try:
        fd = _open_regular(path)
    except OSError:
        return b""
    try:
        return os.pread(fd, 4, 0)
    finally:
        os.close(fd)


def _expand(entry: str, home: str) -> str:
    return home + entry[1:] if entry.startswith("~") else entry


def find_vendor(host: HostOS, name: str, home: str, *, search: Optional[Iterable[str]] = None,
                machine: Optional[str] = None,
                realpath: Callable[[str], str] = os.path.realpath) -> Optional[VendorFound]:
    """The vendor CLI ``name`` on this machine: the first ``search`` entry (default :data:`VENDOR_SEARCH`) holding
    ``name``, resolved to the native executable to copy. Claude Code (its native installer and its npm package
    alike) has a native executable at the end of the link. Codex's npm package has a node launcher there
    (``<pkg>/bin/codex.js``); the native binary that launcher spawns is
    ``<pkg>/node_modules/@<scope>/<name>-<platform>/vendor/<triple>/bin/<name>`` (or ``<pkg>/vendor/<triple>/...``),
    the entry point of the package :data:`VENDOR_UNITS` describes. None when no entry exists."""
    import glob

    magic = NATIVE_MAGIC[host]
    for entry in (search if search is not None else VENDOR_SEARCH[host]):
        p = os.path.join(_expand(entry, home), name)
        if not os.path.lexists(p):
            continue
        real = realpath(p)
        head = _head(real)
        if head[:4] in magic:
            return VendorFound(name, p, real)
        if not head.startswith(b"#!"):
            return VendorFound(name, p, None, f"{p} leads to {real}, which is neither a native {host} executable "
                                              "nor a launcher script")
        arch = machine or os.uname().machine
        triple = VENDOR_TRIPLES.get((host, arch))
        if triple is not None:
            pkg = glob.escape(os.path.dirname(os.path.dirname(real)))
            for pattern in (os.path.join(pkg, "node_modules", "@*", "*", "vendor", triple, "bin", name),
                            os.path.join(pkg, "vendor", triple, "bin", name)):
                for c in sorted(glob.glob(pattern)):
                    r = realpath(c)
                    if _head(r)[:4] in magic:
                        return VendorFound(name, p, r)
        return VendorFound(name, p, None, f"{p} is a launcher script with no native {name} for {triple or arch} "
                                          "in its package")
    return None


def _unit_sources(name: str, binary: str) -> tuple:
    """``(root, files, why)``: the vendor's install unit as ``(relative path, source path)`` pairs, the entry point
    ``bin/<name>`` LAST (it is published after every companion), and ``root`` the package root it was read from
    (None for a one-file unit). ``files`` is empty and ``why`` says why when the package is not whole: its manifest,
    a required file or a required tree missing, or a link (or anything but a directory) where a directory of it
    should be."""
    entry = f"bin/{name}"
    unit = VENDOR_UNITS.get(name)
    if unit is None:
        return None, [(entry, binary)], ""
    bindir = os.path.dirname(binary)
    root = os.path.dirname(bindir)
    if os.path.basename(bindir) != "bin" or not os.path.lexists(os.path.join(root, unit.manifest)):
        return root, [], f"{binary} has no {unit.manifest} beside its bin directory, so its package is not whole"
    rels = [unit.manifest]
    absent = []
    try:
        rels += [f"bin/{n}" for n in sorted(os.listdir(bindir)) if not n.startswith(".")]
        for tree in unit.trees:
            top = os.path.join(root, tree)
            if not os.path.lexists(top):
                absent.append(tree)
                continue
            for d, dirs, files in os.walk(top, followlinks=False):
                for n in [d] + [os.path.join(d, x) for x in dirs]:
                    if not stat.S_ISDIR(os.lstat(n).st_mode):
                        return root, [], f"{n} in {name}'s package is a link or not a directory"
                rels += [os.path.relpath(os.path.join(d, x), root) for x in sorted(files)]
                dirs.sort()
    except OSError as e:
        return root, [], f"cannot list {name}'s package at {root} ({e.strerror or e})"
    missing = [r for r in unit.required if r not in rels] + absent
    if missing:
        return root, [], f"{name}'s package at {root} is missing {', '.join(missing)}"
    rels = [r for r in rels if r != entry] + [entry]
    return root, [(r, os.path.join(root, r)) for r in rels], ""


def _file_mode(st: os.stat_result) -> int:
    """The mode of root's copy of a source file: 0755 for one its owner may run, else 0644."""
    return 0o755 if st.st_mode & stat.S_IXUSR else 0o644


def _source_problem(st: os.stat_result, operator_uid: int, mode: Optional[int] = None) -> str:
    """Why a vendor source file is not copied as ``mode`` (default :func:`_file_mode`), or "". Root copies only a file
    root or the operator owns, that no other account can write, and that every user can already read (and run, when
    it is to be 0755), so the copy shows nothing it hid."""
    mode = _file_mode(st) if mode is None else mode
    if st.st_uid not in (0, operator_uid):
        return f"is owned by uid {st.st_uid}, neither root nor the operator"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "is writable by its group or by every user"
    if mode == 0o755 and _file_mode(st) != 0o755:
        return "is not executable, and the package needs it executable"
    want = stat.S_IROTH | (stat.S_IXOTH if mode == 0o755 else 0)
    if st.st_mode & want != want:
        return "is not readable and runnable by every user" if want & stat.S_IXOTH else "is not readable by every user"
    return ""


def _copy_problem(path: Path, modes: tuple, root_uid: int = 0) -> str:
    """Why ``path`` is not good as root's copy of a vendor file, or "": THE predicate (head ruling V4) that start's
    check, doctor's state and the copy's own "already matches" read. Good = a regular file (never a link), owned by
    ``root_uid``, its mode one of ``modes``."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} is not there"
    except OSError as e:
        return f"cannot examine {path} ({e.strerror or e})"
    if stat.S_ISLNK(st.st_mode):
        return f"{path} is a link, not root's copy"
    if not stat.S_ISREG(st.st_mode):
        return f"{path} is not a regular file"
    if st.st_uid != root_uid:
        return f"{path} is owned by uid {st.st_uid}, not root"
    if stat.S_IMODE(st.st_mode) not in modes:
        return f"{path} has mode {stat.S_IMODE(st.st_mode):o}, not {' or '.join(f'{m:o}' for m in modes)}"
    return ""


def _dir_problem(path: Path, root_uid: int = 0) -> str:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{path} is not there"
    except OSError as e:
        return f"cannot examine {path} ({e.strerror or e})"
    if not stat.S_ISDIR(st.st_mode):
        return f"{path} is a link or not a directory"
    if st.st_uid != root_uid or stat.S_IMODE(st.st_mode) != 0o755:
        return f"{path} is not root's 0755 directory (uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):o})"
    return ""


def _tree_problem(root: Path, unit: VendorUnit, root_uid: int = 0) -> str:
    """Why the package directory ``root`` is not a whole package root may run, or "": root's 0755 directories all the
    way down, never a link, every required file and tree there, every file root's at its :func:`_want_mode` (else 0755
    or 0644). Read for the active version and for a staged one before it becomes active."""
    p = _dir_problem(root, root_uid)
    if p:
        return p
    for rel, mode in unit.required.items():
        p = _copy_problem(root / rel, (mode,), root_uid)
        if p:
            return p
    for tree in unit.trees:
        p = _dir_problem(root / tree, root_uid)
        if p:
            return p
    for d, dirs, files in os.walk(root, followlinks=False):
        for x in dirs:
            p = _dir_problem(Path(d) / x, root_uid)
            if p:
                return p
        for x in files:
            rel = os.path.relpath(os.path.join(d, x), root)
            want = _want_mode(unit, rel)
            p = _copy_problem(Path(d) / x, (0o755, 0o644) if want is None else (want,), root_uid)
            if p:
                return p
    return ""


def _active_dir(base: Path, name: str, root_uid: int = 0) -> tuple:
    """``(dir, why)``: the package version directory ``bin/<name>`` is the active link to, or None and why it is not
    one. The link must be root's and read exactly ``../<name>/<dir>/bin/<name>`` (one plain component, never the
    staging directory)."""
    link = base / "bin" / name
    try:
        st = os.lstat(link)
    except FileNotFoundError:
        return None, f"{link} is not there"
    except OSError as e:
        return None, f"cannot examine {link} ({e.strerror or e})"
    if not stat.S_ISLNK(st.st_mode):
        return None, f"{link} is not the link to an installed {name} package"
    if st.st_uid != root_uid:
        return None, f"{link} is owned by uid {st.st_uid}, not root"
    target = os.readlink(link)
    parts = target.split("/")
    if (len(parts) != 5 or parts[:2] != ["..", name] or parts[3:] != ["bin", name]
            or parts[2] in ("", ".", "..") or parts[2].startswith(".")):
        return None, f"{link} points at {target}, not at a version of {name} in {base / name}"
    return base / name / parts[2], ""


def unit_problem(host: HostOS, name: str, *, prefix: Optional[Path] = None, root_uid: int = 0) -> str:
    """Why root's copy of the vendor ``name`` in the prefix cannot be run, or "". Read from the prefix alone (root's
    tree; start never reads the operator's): the entry point is root's 0755 regular file; for a :data:`VENDOR_UNITS`
    package, ``bin/<name>`` is root's link to a version directory (:func:`_active_dir`) in root's 0755
    ``<prefix>/<name>``, and that directory passes :func:`_tree_problem`."""
    base = prefix if prefix is not None else VENDOR_PREFIX[host]
    unit = VENDOR_UNITS.get(name)
    if unit is None:
        return _copy_problem(vendor_bin(host, name, prefix=prefix), (0o755,), root_uid)
    active, why = _active_dir(base, name, root_uid)
    if active is None:
        return why
    return _dir_problem(base / name, root_uid) or _tree_problem(active, unit, root_uid)


def _fds_equal(a: int, b: int) -> bool:
    if os.fstat(a).st_size != os.fstat(b).st_size:
        return False
    off = 0
    while True:
        x, y = os.pread(a, _CHUNK, off), os.pread(b, _CHUNK, off)
        if x != y:
            return False
        if not x:
            return True
        off += len(x)


def _paths_equal(a: str, b: str) -> bool:
    fa = _open_regular(a)
    try:
        fb = _open_regular(b)
        try:
            return _fds_equal(fa, fb)
        finally:
            os.close(fb)
    finally:
        os.close(fa)


class _Changed(Exception):
    """A source file changed while it was copied (head ruling V2: ``vendor-changed-during-copy``)."""


def _copy_one(src: int, st: os.stat_result, path: str, dest: Path, mode: int, *,
              fchown: Callable[[int, int, int], None], fsync: Callable[[int], None], digest: object = None) -> int:
    """Copy exactly ``st.st_size`` bytes of ``src`` (the fd that was fstat'd as ``st``) to ``dest`` through
    ``.<name>.partial`` beside it, root's at ``mode``, and rename it over ``dest``. Never reads past the captured
    size except one byte to see whether the file grew. :class:`_Changed` when the source's size, mtime or inode is
    not what was captured after the read; the partial is unlinked on any failure, the rename's included. ``digest``
    (a hashlib object) is fed every byte copied."""
    partial = dest.parent / f".{dest.name}.partial"
    try:
        os.unlink(partial)
    except FileNotFoundError:
        pass
    out = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o700)
    try:
        try:
            off = 0
            while off < st.st_size:
                chunk = os.pread(src, min(_CHUNK, st.st_size - off), off)
                if not chunk:
                    raise _Changed(f"{path} shrank while it was copied ({st.st_size} -> {off} bytes)")
                write_all(out, chunk)
                if digest is not None:
                    digest.update(chunk)                                    # type: ignore[attr-defined]
                off += len(chunk)
            if os.pread(src, 1, st.st_size):
                raise _Changed(f"{path} grew while it was copied (past {st.st_size} bytes)")
            now = os.fstat(src)
            if (now.st_size, now.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                raise _Changed(f"{path} was modified while it was copied")
            try:
                there = os.lstat(path)
            except OSError:
                there = None
            if there is None or (there.st_dev, there.st_ino) != (st.st_dev, st.st_ino):
                raise _Changed(f"{path} was replaced while it was copied")
            fchown(out, 0, 0)
            os.fchmod(out, mode)
            fsync(out)
        finally:
            os.close(out)
        os.rename(partial, dest)
    except BaseException:
        try:
            os.unlink(partial)
        except OSError:
            pass
        raise
    return off


def _ensure_dirs(base: Path, rels: Iterable[str], *, chown: Callable[[str, int, int], None],
                 root_uid: int) -> list:
    """Every directory the unit's files sit in, below ``base``: made 0755 and root's when missing, set so when they are
    not; OSError when one is a link or not a directory. Returns them, parents first."""
    made: list = []
    for rel in rels:
        parts = Path(rel).parent.parts
        for i in range(1, len(parts) + 1):
            d = base.joinpath(*parts[:i])
            if d in made:
                continue
            try:
                os.mkdir(d, 0o755)
            except FileExistsError:
                pass
            _own_dir(d, chown=chown, root_uid=root_uid)
            made.append(d)
    return made


def _own_dir(d: Path, *, chown: Callable[[str, int, int], None], root_uid: int) -> None:
    """``d`` made root's 0755; OSError when it is a link or not a directory."""
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{d} is a link or not a directory")
    if st.st_uid != root_uid:
        chown(str(d), 0, 0)
    if stat.S_IMODE(st.st_mode) != 0o755:
        os.chmod(d, 0o755)


def _fsync_dirs(dirs: Iterable[Path], fsync: Callable[[int], None]) -> list:
    """Each directory fsync'd; the warnings for the ones that fail."""
    warnings = []
    for d in dirs:
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            warnings.append(f"warning: could not fsync {d} ({e.strerror or e})")
    return warnings


def _fsync_strict(dirs: Iterable[Path], fsync: Callable[[int], None]) -> None:
    """Each directory fsync'd; OSError naming the first that fails (head ruling V19: before a switch, a directory that
    is not durable stops the publish)."""
    for d in dirs:
        try:
            dfd = os.open(d, os.O_RDONLY)
            try:
                fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as e:
            raise OSError(e.errno, f"could not fsync {d} ({e.strerror or e})") from None


def _version_name(manifest: bytes) -> str:
    """The package's ``version`` from its manifest's bytes, as a directory-name part; "unknown" when it cannot be
    read."""
    import json

    try:
        v = json.loads(manifest[:1 << 16].decode("utf-8")).get("version")
    except (ValueError, AttributeError):
        v = None
    v = re.sub(r"[^A-Za-z0-9._+-]", "_", v)[:64].lstrip(".") if isinstance(v, str) else ""
    return v or "unknown"


def _fix_tree(root: Path, unit: VendorUnit, modes: Mapping[str, int], *, chown: Callable[[str, int, int], None],
              root_uid: int) -> None:
    """The active package directory ``root`` set root's again: every directory 0755, every file at ``modes[rel]``
    (the mode it is copied at). Bytes are not touched; OSError on a link or anything but a file or directory."""
    _own_dir(root, chown=chown, root_uid=root_uid)
    for d, dirs, files in os.walk(root, followlinks=False):
        for x in dirs:
            _own_dir(Path(d) / x, chown=chown, root_uid=root_uid)
        for x in files:
            p = Path(d) / x
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{p} is a link or not a regular file")
            if st.st_uid != root_uid:
                chown(str(p), 0, 0)
            want = modes.get(os.path.relpath(p, root))
            if want is not None and stat.S_IMODE(st.st_mode) != want:
                os.chmod(p, want)


def _rmtree(path: Path) -> str:
    """``path`` removed if it is there; "" or why it could not be."""
    import shutil

    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        return f"could not remove {path} ({e.strerror or e})"
    return ""


class _LockFailed(Exception):
    """A package's lock could not be opened or taken within :data:`VENDOR_LOCK_WAIT_S`."""


@contextmanager
def _package_lock(base: Path, name: str, *, fchown: Callable[[int, int, int], None],
                  chown: Callable[[str, int, int], None], root_uid: int) -> Iterator[None]:
    """``<prefix>/<name>/.lock`` held for the body (head ruling V15): root's 0600 regular file, opened without
    following a link, flocked exclusively; a second holder waits up to :data:`VENDOR_LOCK_WAIT_S` for it.
    :class:`_LockFailed` when it cannot be opened or is still held at the bound."""
    import fcntl
    import time

    lock = base / name / _VENDOR_LOCK
    try:
        _ensure_dirs(base, [f"{name}/x"], chown=chown, root_uid=root_uid)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as e:
        raise _LockFailed(f"cannot open {lock} ({e.strerror or e})") from None
    try:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{lock} is not a regular file")
            if st.st_uid != root_uid:
                fchown(fd, 0, 0)
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            deadline = time.monotonic() + VENDOR_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise _LockFailed(f"{lock} is still held by another run after {VENDOR_LOCK_WAIT_S:g} s")
                    time.sleep(0.1)
        except OSError as e:
            raise _LockFailed(f"cannot lock {lock} ({e.strerror or e})") from None
        yield
    finally:
        os.close(fd)


def _copy_package(host: HostOS, name: str, unit: VendorUnit, root: str, srcs: list, base: Path, *,
                  fchown: Callable[[int, int, int], None], fsync: Callable[[int], None],
                  chown: Callable[[str, int, int], None], lchown: Callable[[str, int, int], None],
                  root_uid: int) -> tuple:
    """:func:`_publish_package` run holding the package's lock (:func:`_package_lock`). A lock that cannot be opened,
    or is still held at the bound, fails the copy with the active version unchanged."""
    try:
        with _package_lock(base, name, fchown=fchown, chown=chown, root_uid=root_uid):
            return _publish_package(host, name, unit, root, srcs, base, fchown=fchown, fsync=fsync, chown=chown,
                                    lchown=lchown, root_uid=root_uid)
    except _LockFailed as e:
        return False, f"{name}: {e}; not copied, the active {name} is unchanged"


def _holds_exactly(d: Path, srcs: list, modes: Mapping[str, int]) -> bool:
    """``d`` holds exactly the files of ``srcs`` (``(rel, path, fd, mode)``) and no other, each with its source's
    bytes; False on any OSError."""
    try:
        held = {os.path.relpath(os.path.join(x, f), d) for x, _, files in os.walk(d, followlinks=False) for f in files}
        if held != set(modes):
            return False
        for rel, _, fd, _ in srcs:
            dfd = _open_regular(str(d / rel))
            try:
                if not _fds_equal(fd, dfd):
                    return False
            finally:
                os.close(dfd)
    except OSError:
        return False
    return True


def _reusable(d: Path, unit: VendorUnit, srcs: list, modes: Mapping[str, int], root_uid: int) -> bool:
    """A kept version directory a copy can switch to as it is (head ruling V20): no link, it passes
    :func:`_tree_problem`, holds exactly these files and bytes, and each file is at the mode it would be copied at."""
    try:
        if os.path.islink(d) or not os.path.isdir(d) or _tree_problem(d, unit, root_uid):
            return False
        if not _holds_exactly(d, srcs, modes):
            return False
        return all(stat.S_IMODE(os.lstat(d / rel).st_mode) == m for rel, m in modes.items())
    except OSError:
        return False


def _indeterminate(name: str, link: Path, target: Path, e: OSError) -> str:
    return (f"{name}: {link} names {target}, but {e.strerror or e}: whether that survives a crash is indeterminate "
            "(repair again)")


def _publish_package(host: HostOS, name: str, unit: VendorUnit, root: str, srcs: list, base: Path, *,
                     fchown: Callable[[int, int, int], None], fsync: Callable[[int], None],
                     chown: Callable[[str, int, int], None], lchown: Callable[[str, int, int], None],
                     root_uid: int) -> tuple:
    """Publish the package read from ``root`` (``srcs``: ``(rel, path, fd, mode)``) as the active version (head ruling
    V8; the frame of Homebrew's ``opt`` link, Nix profiles and Capistrano's ``current``: stage a complete versioned
    tree, then switch one name atomically). The caller holds the package's lock. This path NEVER removes a version
    directory (head rulings V18, V20: a session resolved its physical version directory at start and spawns from it to
    its end); :func:`prune_vendor` does, when no session is live.

    1. The active version already holds exactly these files with these bytes: its owners and modes are set again in
       place (V9) and nothing is copied. If that cannot be done (a link where a directory belongs), it is rebuilt as
       below (V21).
    2. Else a kept version that :func:`_reusable` accepts is switched to as it is (V20, a rollback).
    3. Else any staging directory a crashed run left is removed, every file is copied into a staging directory unique
       to this run (``<prefix>/<name>/.staging.<pid>.<random>.partial``), the staged directories are fsync'd, the
       tree must pass :func:`_tree_problem`, and it is renamed to ``<version>-<digest>``, or to that name with this
       run's own suffix when a directory of that name is there already (never removed, never renamed over).

    Before the link switches, the new version's directories and ``<prefix>/<name>`` are fsync'd and any failure there
    stops the publish with the active version as it was (V19). Then a new link is renamed over ``bin/<name>`` and
    :func:`unit_problem` must be "". A failure to fsync ``bin`` after that is reported as an indeterminate publish,
    never a success; so is one in step 1, so a re-run after an indeterminate publish settles it."""
    import hashlib
    import secrets

    pdir, bindir = base / name, base / "bin"
    link = bindir / name
    modes = {rel: mode for rel, _, _, mode in srcs}
    active, _ = _active_dir(base, name, root_uid)
    if active is not None and os.path.isdir(active) and _holds_exactly(active, srcs, modes):
        try:
            _fix_tree(active, unit, modes, chown=chown, root_uid=root_uid)
            _own_dir(pdir, chown=chown, root_uid=root_uid)
        except OSError:
            pass                       # V21: a structure set-in-place cannot mend is rebuilt below
        else:
            problem = unit_problem(host, name, prefix=base, root_uid=root_uid)
            if not problem:
                try:
                    _fsync_strict([pdir, bindir], fsync)
                except OSError as e:
                    return False, _indeterminate(name, link, active, e)
                return True, f"{name}: {active} already matches {root}"
            # V28: set-in-place returned and the structure is still not whole: rebuilt below, as for an OSError
    staging = pdir / f"{_STAGING}{os.getpid()}.{secrets.token_hex(4)}.partial"
    final: Optional[Path] = None
    total = 0
    try:
        manifest = next((os.pread(fd, 1 << 16, 0) for rel, _, fd, _ in srcs if rel == unit.manifest), b"")   # V26
        version = _version_name(manifest)
        _ensure_dirs(base, [f"{name}/x"], chown=chown, root_uid=root_uid)
        for entry in sorted(os.listdir(pdir)):
            if (entry.startswith(f"{version}-") and (active is None or entry != active.name)
                    and _reusable(pdir / entry, unit, srcs, modes, root_uid)):
                final = pdir / entry
                break
        reused = final is not None
        if reused:
            _fsync_strict([Path(d) for d, _, _ in os.walk(final, topdown=False, followlinks=False)], fsync)
        else:
            for stale in sorted(os.listdir(pdir)):
                if stale.startswith(_STAGING):
                    why = _rmtree(pdir / stale)
                    if why:
                        raise OSError(why)
            os.mkdir(staging, 0o700)
            _own_dir(staging, chown=chown, root_uid=root_uid)
            dirs = _ensure_dirs(staging, list(modes), chown=chown, root_uid=root_uid)
            digest = hashlib.sha256()
            for rel, path, fd, mode in srcs:
                st = os.fstat(fd)
                digest.update(f"{rel}\0{mode:o}\0{st.st_size}\0".encode())
                total += _copy_one(fd, st, path, staging / rel, mode, fchown=fchown, fsync=fsync, digest=digest)
            _fsync_strict([*reversed(dirs), staging], fsync)
            problem = _tree_problem(staging, unit, root_uid)
            if problem:
                raise OSError(f"the staged copy is not whole: {problem}")
            stem = f"{version}-{digest.hexdigest()[:12]}"
            final = pdir / stem
            while os.path.lexists(final):        # V20: a directory of this name is kept, whatever is in it
                final = pdir / f"{stem}.{os.getpid()}.{secrets.token_hex(2)}"
            os.rename(staging, final)
        _fsync_strict([pdir], fsync)
    except _Changed as e:
        _rmtree(staging)
        return False, (f"{name}: vendor-changed-during-copy: {e}; not copied, the active {name} is unchanged (repair "
                       "copies it on its next run)")
    except OSError as e:
        _rmtree(staging)
        return False, f"{name}: copying {root} to {pdir} failed ({e.strerror or e}); the active {name} is unchanged"
    tmp = bindir / f".{name}.link.partial"
    try:
        _rmtree(tmp)
        os.symlink(f"../{name}/{final.name}/bin/{name}", tmp)
        if os.lstat(tmp).st_uid != root_uid:
            lchown(str(tmp), 0, 0)
        os.rename(tmp, link)
    except OSError as e:
        _rmtree(tmp)
        return False, (f"{name}: switching {link} to {final} failed ({e.strerror or e}); the active {name} is "
                       "unchanged")
    problem = unit_problem(host, name, prefix=base, root_uid=root_uid)
    if problem:
        return False, f"{name}: {problem} after the switch to {final}"
    try:
        _fsync_strict([bindir], fsync)
    except OSError as e:
        return False, _indeterminate(name, link, final, e)
    if not reused:
        return True, (f"{name}: copied {len(srcs)} files of its package {root} to {final} ({total} bytes, root's) and "
                      f"switched {link} to it")
    return True, f"{name}: {final}, kept from an earlier run, already holds {root}; switched {link} to it"


def prune_vendor(host: HostOS, name: str, *, live: Callable[[], Iterable[str]], prefix: Optional[Path] = None,
                 fchown: Callable[[int, int, int], None] = os.fchown, chown: Callable[[str, int, int], None] = os.chown,
                 root_uid: int = 0) -> tuple:
    """Head ruling V18: remove every entry of ``<prefix>/<name>/`` but the active version and the lock (the versions
    copies kept, and staging a crashed run left), holding the package's lock, and only when ``live()`` names no live
    session. ``live`` is the caller's evidence from Levain's own lease state (repair passes
    ``install_helper.vendor_prune_live``). Never fails a run: always ``(True, line)``; a live session, a ``live`` that
    raises, no active version or a lock that cannot be taken keeps every version and says so."""
    base = prefix if prefix is not None else VENDOR_PREFIX[host]
    if name not in VENDOR_UNITS:
        return True, f"{name}: one file, no versions to prune"
    pdir = base / name
    if os.path.islink(pdir) or not os.path.isdir(pdir):
        return True, f"{name}: no {pdir}; nothing to prune"
    removed: list = []
    warnings: list = []
    try:
        with _package_lock(base, name, fchown=fchown, chown=chown, root_uid=root_uid):
            try:
                held = tuple(live())
            except Exception as e:
                return True, f"{name}: every version kept (the live-session check failed: {e})"
            if held:
                return True, (f"{name}: every version kept while {len(held)} session(s) are live: "
                              f"{', '.join(held[:5])}")
            active, why = _active_dir(base, name, root_uid)
            if active is None:
                return True, f"{name}: no active version ({why}); nothing pruned"
            for old in sorted(os.listdir(pdir)):
                if old in (active.name, _VENDOR_LOCK):
                    continue
                w = _rmtree(pdir / old)
                if w:
                    warnings.append(f"warning: {w}")
                else:
                    removed.append(old)
    except _LockFailed as e:
        return True, f"{name}: {e}; nothing pruned"
    except OSError as e:
        return True, f"{name}: pruning {pdir} failed ({e.strerror or e})"
    line = f"{name}: removed {', '.join(removed)} from {pdir}" if removed else f"{name}: nothing to prune in {pdir}"
    return True, "; ".join([line, *warnings])


def copy_vendor(host: HostOS, name: str, *, home: str, operator_uid: int, prefix: Optional[Path] = None,
                search: Optional[Iterable[str]] = None, machine: Optional[str] = None,
                fchown: Callable[[int, int, int], None] = os.fchown, fsync: Callable[[int], None] = os.fsync,
                chown: Callable[[str, int, int], None] = os.chown,
                lchown: Callable[[str, int, int], None] = os.lchown, root_uid: int = 0) -> tuple:
    """Copy the vendor CLI ``name`` (:func:`find_vendor`) into the vendor prefix as its whole install unit
    (:func:`_unit_sources`): Claude Code's one binary to ``bin/claude``, written beside it as ``.claude.partial``,
    flushed, then renamed over it (a running session keeps the inode it exec'd); Codex's package as a complete
    version switched in by one link (:func:`_copy_package`). Every file a regular file, root's, at its
    :func:`_want_mode` or else 0755 or 0644 (:func:`_file_mode`). Never a link to the vendor's own tree: that tree is
    the operator's to write (r3c10-vm: ``/opt/homebrew/lib`` is ``admin``-writable), and the slot must run what root
    copied. A file already good (:func:`_copy_problem`) and equal to its source is left alone.

    A vendor not found, a package not whole, or a source file :func:`_source_problem` refuses is a SKIP with a line
    (``(True, ...)``), and the copy already there, if any, is kept. A failed copy is ``(False, ...)``:
    ``vendor-changed-during-copy`` when a source changed under the read (repair copies it again on its next run).
    Claude Code's directories are fsync'd once its file is renamed into place; one that fails is a warning on a
    successful line (V7; a package's fsync rules are :func:`_publish_package`'s, V19). ``root_uid``: the owner a copy
    left alone must have (tests pass their own uid, since their ``fchown``/``chown`` only record)."""
    base = prefix if prefix is not None else VENDOR_PREFIX[host]
    dest = vendor_bin(host, name, prefix=prefix)
    have = os.path.lexists(dest)
    tail = (f"; the copy at {dest} is kept" if have
            else f"; a {name} session is refused vendor-not-installed:{name}")
    found = find_vendor(host, name, home, search=search, machine=machine)
    if found is None:
        where = ", ".join(search if search is not None else VENDOR_SEARCH[host])
        return True, f"{name}: not found in {where}; skipped{tail}"
    if found.binary is None:
        return True, f"{name}: {found.why}; skipped{tail}"
    root, files, why = _unit_sources(name, found.binary)
    if why:
        return True, f"{name}: {why}; not copied{tail}"
    unit = VENDOR_UNITS.get(name)
    srcs: list = []
    try:
        for rel, path in files:
            try:
                fd = _open_regular(path)
            except OSError as e:
                return True, f"{name}: cannot open {path} ({e.strerror or e}); not copied{tail}"
            st = os.fstat(fd)
            want = _want_mode(unit, rel) if unit is not None else 0o755
            srcs.append((rel, path, fd, want if want is not None else _file_mode(st)))
            problem = _source_problem(st, operator_uid, srcs[-1][3])
            if problem:
                return True, f"{name}: {path} {problem}; not copied{tail}"
            if rel == f"bin/{name}" and os.pread(fd, 4, 0)[:4] not in NATIVE_MAGIC[host]:
                return True, f"{name}: {path} is not a native {host} executable; not copied{tail}"
        if unit is not None:
            return _copy_package(host, name, unit, root, srcs, base, fchown=fchown,  # type: ignore[arg-type]
                                 fsync=fsync, chown=chown, lchown=lchown, root_uid=root_uid)
        [(_, path, fd, mode)] = srcs
        if not _copy_problem(dest, (mode,), root_uid):
            try:
                dfd = _open_regular(str(dest))
            except OSError:
                dfd = None
            if dfd is not None:
                try:
                    if _fds_equal(fd, dfd):
                        return True, f"{name}: {dest} already matches {found.binary}"
                finally:
                    os.close(dfd)
        try:
            dirs = _ensure_dirs(base, [f"bin/{name}"], chown=chown, root_uid=root_uid)
            total = _copy_one(fd, os.fstat(fd), path, dest, mode, fchown=fchown, fsync=fsync)
        except _Changed as e:
            return False, f"{name}: vendor-changed-during-copy: {e}; not copied (repair copies it on its next run)"
        except OSError as e:
            return False, f"{name}: copying {found.binary} to {dest} failed ({e.strerror or e})"
        line = f"{name}: copied {found.binary} to {dest} ({total} bytes, root's, 0755)"
        return True, "; ".join([line, *_fsync_dirs(dirs, fsync)])
    finally:
        for _, _, fd, _ in srcs:
            os.close(fd)


def vendor_state(host: HostOS, name: str, home: str, *, prefix: Optional[Path] = None,
                 search: Optional[Iterable[str]] = None, machine: Optional[str] = None, root_uid: int = 0) -> tuple:
    """``(state, detail)`` for doctor: ``current`` (root's copy passes :func:`unit_problem` and every file equals the
    vendor's it would be copied from; for a package, the active version holds no other file), ``stale`` (a file
    differs, is missing or is no longer the vendor's: the vendor was updated since the last repair), ``bad-copy``
    (:func:`unit_problem`: start refuses it whatever its bytes), ``not-copied`` (a vendor, no copy), ``orphan`` (a
    copy, no whole vendor to compare it with), ``absent`` (neither), ``unreadable``."""
    base = prefix if prefix is not None else VENDOR_PREFIX[host]
    dest = vendor_bin(host, name, prefix=prefix)
    found = find_vendor(host, name, home, search=search, machine=machine)
    has = os.path.lexists(dest)
    why = ""
    files: list = []
    if found is not None and found.binary is not None:
        _, files, why = _unit_sources(name, found.binary)
    elif found is not None:
        why = found.why
    if not files:
        if has:
            return "orphan", f"{dest} is there, but no whole {name} was found to compare it with" + (
                f" ({why})" if why else "")
        return "absent", f"no {name} on this machine" + (f" ({why})" if why else "")
    if not has:
        return "not-copied", f"{found.binary} is not copied to {dest}"     # type: ignore[union-attr]
    problem = unit_problem(host, name, prefix=prefix, root_uid=root_uid)
    if problem:
        return "bad-copy", problem
    there = base
    if name in VENDOR_UNITS:
        there = _active_dir(base, name, root_uid)[0]        # unit_problem passed: it is a version directory
    try:
        for rel, path in files:
            if not os.path.lexists(there / rel):
                return "stale", f"{there / rel} is missing (the vendor has {path})"
            if not _paths_equal(path, str(there / rel)):
                return "stale", f"{there / rel} differs from {path}"
        if name in VENDOR_UNITS:
            want = {rel for rel, _ in files}
            extra = sorted(os.path.relpath(os.path.join(d, x), there) for d, _, fs in os.walk(there) for x in fs
                           if os.path.relpath(os.path.join(d, x), there) not in want)
            if extra:
                return "stale", f"{there} holds {extra[0]}, which the vendor no longer has"
    except OSError as e:
        return "unreadable", f"cannot compare root's copy in {there} with {found.binary} ({e.strerror or e})"  # type: ignore[union-attr]
    return "current", f"{dest} matches {found.binary}" + (f" with its package ({len(files)} files)"  # type: ignore[union-attr]
                                                          if len(files) > 1 else "")


def vendor_steps(host: HostOS, operator: str, *, lookup: Optional[Callable[[str], object]] = None,
                 failed: Optional[list] = None, copy: Optional[Callable[..., tuple]] = None) -> list:
    """The vendor prefix's ``bin`` (root's, 0755, no link above it) and one copy step per :data:`VENDORS` entry
    (:func:`copy_vendor`), searched from ``operator``'s home, read when the step runs. A copy step is ``allow_fail``
    (head ruling V6): a failed copy never stops the plan; its line is appended to ``failed`` and
    :func:`vendor_report_step`, planned later, fails the run with it."""
    import pwd

    look = lookup if lookup is not None else pwd.getpwnam
    run_copy = copy if copy is not None else copy_vendor
    sink = failed if failed is not None else []
    t = _tools(host)
    b = VENDOR_PREFIX[host] / "bin"
    steps = [
        Step(f"check every directory above {b} is root's alone (links resolved), and {b} is no link",
             call=lambda: _root_owned_ancestors(b)),
        Step(f"create {b}", (t["mkdir"], "-p", "-m", "755", str(b))),
        Step(f"own {b} as root", (t["chown"], f"root:{ROOT_GROUP[host]}", str(b))),
        Step(f"set {b} to 0755", (t["chmod"], "0755", str(b))),
    ]

    def step(name: str) -> Callable[[], tuple]:
        def run() -> tuple:
            try:
                pw = look(operator)
            except KeyError:
                ok, why = False, f"{name}: the operator {operator} has no account, so there is no home to search"
            except Exception as e:      # V11: every failure reaches the sink, never past it
                ok, why = False, f"{name}: the operator {operator}'s account could not be read ({e})"
            else:
                try:
                    ok, why = run_copy(host, name, home=pw.pw_dir, operator_uid=pw.pw_uid)   # type: ignore[attr-defined]
                except Exception as e:
                    ok, why = False, f"{name}: the copy failed ({type(e).__name__}: {e})"
            if not ok:
                sink.append(why)
            return ok, why
        return run

    for name in VENDORS:
        steps.append(Step(f"place root's copy of the {name} CLI (its whole install unit) in {VENDOR_PREFIX[host]} "
                          "(a vendor not on this machine is skipped)", call=step(name), allow_fail=True))
    return steps


def vendor_report_step(failed: list, *, allow_fail: bool = False) -> Step:
    """The step that fails the run when a :func:`vendor_steps` copy failed earlier in it, planned after the steps a
    vendor copy must not block: repair plans it after R11's sudoers rules and R12's units, setup after its units.
    ``allow_fail``: setup's form (ruling V23), so a copy that fails on every run is printed as skipped and never keeps
    the operator record from being written; repair keeps the failing form."""
    return Step("report the vendor CLI copies that failed earlier in this run (none: nothing to report)",
                call=lambda: (not failed, "; ".join(failed)), allow_fail=allow_fail)


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
           "NATIVE_MAGIC", "VENDORS", "VENDOR_SEARCH", "VENDOR_TRIPLES", "VENDOR_UNITS", "VendorFound", "VendorUnit",
           "copy_vendor", "find_vendor", "unit_problem", "vendor_bin", "vendor_report_step", "vendor_state",
           "vendor_steps",
           "VENDOR_PREFIX", "boot_path_code", "check_source_writers", "check_sources", "current_path", "dir_steps",
           "entry_steps", "entry_text", "flip_step", "gc_step", "h11_floor", "h11_source", "helper_entry_text",
           "read_current", "stage_steps", "tree_steps"]
