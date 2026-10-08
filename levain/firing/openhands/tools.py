"""levain.firing.openhands.tools — the CONFINED executor-tool bundle for a sovereign entity.

The isolated ``levain run`` entity moves from conversation to AGENCY — it gets HANDS. This module
builds them, and it builds them so the (less-trusted, open-model) entity can work like Claude Code /
Codex on the operator's REAL repos while the sovereignty CROWN JEWELS stay structurally off-limits no
matter what the model is told to do:

  - :class:`LevainBashTool` — a stateful bash confined by an OS sandbox (spore-311's
    :class:`~levain.firing.confinement.SandboxedShell`), NOT the SDK's un-confinable host
    ``TerminalExecutor``. This is the CC/Codex-replacement hand: `cd` anywhere, run builds, SSH out,
    hit the network — everything you use bash for — with the crown jewels fenced at the syscall level.
  - :class:`LevainFileEditorTool` — the file-editor hand, RELAXED (slice 2) from the step-6
    ``<entity>/workspace/`` jail to the SAME crown-jewels floor as bash. A networked bash that goes
    anywhere paired with a file editor still jailed to a workspace is incoherent; both hands relax
    together, protected by the floor rather than a jail.

**One floor, two enforcers (``structural_invariants_beat_discipline``).** A
:class:`~levain.firing.confinement.CrownJewelsPolicy` — resolved ONCE per conversation into its
:class:`~levain.firing.binding.ConversationBinding` and handed to both hands as one
:class:`_SharedFloor` by :class:`LevainHands` (spore-438) — fences BOTH hands. bash rides the rendered platform sandbox — macOS
``sandbox-exec`` or, since K4c, a Linux ``bwrap`` mount namespace (the OS fences it — a persistent
shell whose cwd wanders can't be confined in-process, which is the whole reason bash needed an OS
sandbox). The POLICY is identical on both; only the enforcement model differs. The file editor is ordinary in-process Python, NOT
under the sandbox, so it calls the IN-PROCESS twin :func:`~levain.firing.confinement.crown_jewel_reason`
on every path — the same denylist, so there is no ``claim > enforcement`` gap between the two hands.

**The crown-jewels floor (structural, never).** ``~/.anneal-memory/`` (flow's memory — the identity
moat in file terms), sibling entities' ``.levain/`` stores, ``~/.ssh`` key material (``ssh_mode=
"agent"`` — the entity authenticates via the agent socket but can't read/exfil raw keys),
``~/.ssh/authorized_keys`` WRITE (both ssh_modes — no key can be planted as a persistence backdoor),
plus the operator-declared credential files/subtrees from ``confinement.json`` and — whenever the
resolved ``deny_standard_creds`` is on (every drive but the interactive REPL, unless the entity opts out) — the standard cred stores (``confinement._STANDARD_CRED_SUBTREES`` /
``_STANDARD_CRED_FILES``). The entity's OWN ``<entity>/.levain/`` is its working space, NOT a jewel — readable,
and its non-store files are writable — EXCEPT its own memory STORE files (``memory.{continuity.md,
crystal.json,db}`` + the SQLite sidecars), which are WRITE-denied to the hands (``own_memory_files``,
spore-359): spore-359 folds the neocortex into the always-loaded frame, so only the host-process wrap
may compose it, never the hands. The firing's ``assert_entity_isolated`` moat, not these tools, keeps
recall/capture off flow's store.

**Gating (v1 REALITY, load-bearing honesty — REWRITTEN 2026-07-29 for the post-K3 world).** The
floor protects the crown jewels and NOTHING else. With default-allow and no permission prompts
(Phill: people bypass those IRL), a confabulating open model can still ``rm -rf`` a real repo,
``git push --force``, or ``curl | bash`` — **none of which THE FLOOR stops**, which is a true
statement about the floor and never was one about the whole system.

The rest is covered by the **efferent gate** (:mod:`levain.firing.gate`, shipped K3), which halts
every efferent action — bash is ALWAYS efferent — when no human is present: the REPL
(``human_present=True``) resolves UNGATED and the operator watching activity IS the fan-in, while
``--task`` and scheduled seats resolve GATED and exit ``EXIT_GATED`` (4) with nothing executed.
``efferent_gate: "ungated"`` disarms it in both cases, so a surface claiming an entity is governed
must RESOLVE that setting rather than assume it. **UNATTENDED OPERATION IS NOW A v1 CLAIM** (K4a,
``levain daemon install-seat``); what is still absent is the per-domain threshold POLICY
(``spore-417``) — nothing graduates, everything efferent gates. The full honest limits live on
:mod:`levain.firing.confinement`, and they are NOT identical across platforms: the shared ones are
a hardlink a host process creates while a shell is live, resource exhaustion, non-crown-jewel
network exfil and IPC side channels,
while "Apple-deprecated ``sandbox-exec``" is macOS-only and Linux carries its own — chiefly that a
denied read reports ENOENT rather than EPERM, and that a missing write-denied file's mountpoint is
created on the host.

Requires the ``openhands`` extra.
"""
from __future__ import annotations

import builtins
import contextvars
import dataclasses
import errno
import logging
import os
import shutil
import signal
import stat
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar

from openhands.sdk.tool import (
    Action,
    DeclaredResources,
    Observation,
    Tool,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.file_editor.definition import FileEditorAction, FileEditorObservation
from openhands.tools.file_editor.impl import FileEditorExecutor
from openhands.tools.terminal.definition import (
    TerminalAction,
    TerminalObservation,
    TerminalTool,
)
from openhands.tools.terminal.metadata import CmdOutputMetadata

from levain.firing.confinement import (
    ConfinementError,
    FloorRefreshError,
    CrownJewelsPolicy,
    SandboxedShell,
    _jewel_inodes,
    crown_jewel_reason,
    linked_jewel_reason,
    opened_file_path,
    opened_file_reason,
    refresh_socket_denies,
    select_provider,
)
from levain.firing.binding import ConversationBinding

_log = logging.getLogger("levain.firing.tools")  # module convention: see levain/wrap.py, jobs.py

# THE EDITOR'S OPENS ARE JUDGED AFTER THEY OPEN, BEFORE ANYTHING IS READ (L2 r1). The executor's path
# check runs before the stock editor opens the path, so a link the shell flips in between (benign at
# the check, /proc/<pid>/environ or a jewel at the open) passed it. The stock editor opens files with
# the module-global ``open`` of two modules; each is given this wrapper, which, while a floored
# executor call is running in this context, asks :func:`opened_file_reason` about the object actually
# opened and refuses (closing it) before the editor reads a byte. Outside such a call it is ``open``.
_EDITOR_FLOOR: contextvars.ContextVar[CrownJewelsPolicy | None] = contextvars.ContextVar(
    "levain_editor_floor", default=None)


class _FloorRefusedOpen(PermissionError):
    """An editor open the floor refused after opening (see :func:`_floored_open`)."""


def _floored_open(file, *args, **kwargs):
    policy = _EDITOR_FLOOR.get()
    if policy is None or "opener" in kwargs:
        return builtins.open(file, *args, **kwargs)

    def opener(path, flags):
        # Judged BEFORE any byte moves: without O_TRUNC (truncating first would already have emptied a
        # jewel), and a file this call creates is created O_EXCL, so a refusal knows it may remove it.
        # O_NONBLOCK on the way in: a FIFO the shell planted must be refused, not block the editor.
        created = False
        probe = (flags & ~(os.O_TRUNC | os.O_CREAT | os.O_EXCL)) | os.O_NONBLOCK
        if flags & os.O_CREAT and flags & os.O_EXCL:
            # `open(path, "x")`: the caller asked to fail on an existing file, so no first open.
            fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NONBLOCK, 0o666)
            created = True
        else:
            for _ in range(3):
                try:
                    fd = os.open(path, probe, 0o666)
                    break
                except FileNotFoundError:
                    if not flags & os.O_CREAT:
                        raise
                try:
                    fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_EXCL | os.O_NONBLOCK, 0o666)
                    created = True
                    break
                except FileExistsError:
                    if os.path.islink(path):
                        # A dangling link: creating would make a file wherever it points.
                        raise _FloorRefusedOpen(
                            f"{path} is a dangling symlink; the floor does not create its target"
                        ) from None
                    # Another process created it between the two opens (L3 r2): open it again.
            else:
                raise _FloorRefusedOpen(f"{path} kept appearing and vanishing between opens; not opened")
        try:
            st = os.fstat(fd)
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                reason = "the opened path is not a regular file (a FIFO, device or socket)"
            else:
                reason = opened_file_reason(policy, fd)
        except Exception as exc:  # noqa: BLE001 — a check that cannot run refuses
            reason = f"the opened file could not be checked ({exc})"
        if reason is not None:
            if created:
                try:
                    real = opened_file_path(fd)
                    lst = os.lstat(real)
                    if (lst.st_dev, lst.st_ino) == (st.st_dev, st.st_ino):
                        os.unlink(real)
                except OSError:
                    pass
            os.close(fd)
            raise _FloorRefusedOpen(reason)
        os.set_blocking(fd, not flags & os.O_NONBLOCK)
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd

    return builtins.open(file, *args, opener=opener, **kwargs)


# THE EDITOR'S OTHER PRIMITIVES WALK BY DIRECTORY FD (codex, L3 r2 on the frozen tip). ``insert`` moves
# a temp file onto its target with ``shutil.move`` and a directory ``view`` lists with ``Path.iterdir``,
# neither through ``open``, so a parent link the shell flips after the executor's path check led the
# move onto a jewel, or the listing into a denied subtree. Both now walk the path one component at a
# time from ``/`` with O_NOFOLLOW, REFUSE any component that is a symlink (head ruling 2026-10-07:
# never follow one), judge the directory they end up holding by its name and by the identity of it
# and each of its ancestors, and then act relative to that held fd. A path under one of the policy's
# trusted roots (the workspace, the entity dir, $HOME) is first mapped to that root's real spelling,
# resolved once when the policy was built: a link ABOVE such a root (macOS /tmp, a symlinked $HOME)
# is the operator's, and only links below it are refused (head ruling, same day).


class _FloorRefusedWalk(Exception):
    """A non-open editor primitive the floor refused. Not an OSError, so the stock editor's own
    ``except OSError`` around a directory listing does not turn it into a plain error message."""


def _trusted_spelling(p: str) -> str:
    """``p`` with a trusted root it lies under replaced by that root's real path (the policy's
    ``trusted_roots``, longest first), else ``p`` unchanged."""
    policy = _EDITOR_FLOOR.get()
    for lex, real in (policy.trusted_roots if policy is not None else ()):
        lx = str(lex)
        if p == lx or p.startswith(lx.rstrip("/") + "/"):
            return str(real) + p[len(lx):]
    return p


def _held_dir(path: str | Path) -> tuple[int, str]:
    """``(fd, path)`` of the directory ``path``, opened component by component from ``/`` without
    following a symlink anywhere. Refuses a symlink component; other errors propagate."""
    p = _trusted_spelling(os.path.abspath(os.path.expanduser(str(path))))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    walked = "/"
    try:
        for part in [c for c in p.split("/") if c]:
            st = os.stat(part, dir_fd=fd, follow_symlinks=False)
            walked = os.path.join(walked, part)
            if stat.S_ISLNK(st.st_mode):
                raise _FloorRefusedWalk(
                    f"{walked} is a symlink, and the editor does not follow a link it would act "
                    "through (it could be repointed between the check and the act)"
                )
            try:
                nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    # Swapped for a link (or a file) after the stat above: the same refusal.
                    raise _FloorRefusedWalk(f"{walked} changed into a link or a file during the walk "
                                            "— refused") from None
                raise
            os.close(fd)
            fd = nfd
        return fd, walked
    except BaseException:
        os.close(fd)
        raise


def _held_dir_reason(policy: CrownJewelsPolicy, fd: int, walked: str) -> str | None:
    """Why the held directory must not be used: its name is a jewel or inside one, or it or any
    ancestor (reached through ``..`` from the fd itself, so a rename since the walk is seen) is a
    denied subtree root by identity."""
    reason = crown_jewel_reason(policy, walked)
    if reason is not None:
        return reason
    roots: dict[tuple[int, int], str] = {}
    for r in (*policy.deny_read_write, *((policy.ssh_dir,) if policy.ssh_dir else ())):
        try:
            rs = os.stat(r)
        except OSError:
            continue
        roots[(rs.st_dev, rs.st_ino)] = str(r)
    cur = os.dup(fd)
    try:
        for _ in range(4096):
            st = os.fstat(cur)
            hit = roots.get((st.st_dev, st.st_ino))
            if hit is not None:
                return f"{walked} is inside the crown-jewel directory {hit}, which the floor denies"
            up = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
            os.close(cur)
            cur = up
            ust = os.fstat(cur)
            if (ust.st_dev, ust.st_ino) == (st.st_dev, st.st_ino):
                return None   # the root is its own parent
        return f"{walked}: the walk up to / did not end — refused"
    finally:
        os.close(cur)


def _judged_dir(path: str | Path) -> tuple[int, str]:
    """:func:`_held_dir`, refused unless :func:`_held_dir_reason` passes it."""
    fd, walked = _held_dir(path)
    try:
        reason = _held_dir_reason(_EDITOR_FLOOR.get(), fd, walked)
    except OSError as exc:
        reason = f"{walked} could not be checked ({exc}) — refused"
    if reason is not None:
        os.close(fd)
        raise _FloorRefusedWalk(reason)
    return fd, walked


def _floored_move(src, dst, *args, **kwargs):
    """``shutil.move`` for the editor's ``insert``: its own temp file renamed onto ``dst`` relative to
    the held, judged parent directory, with the target name judged too and never a link."""
    policy = _EDITOR_FLOOR.get()
    if policy is None:
        return shutil.move(src, dst, *args, **kwargs)
    try:
        return _floored_move_impl(policy, src, dst)
    except BaseException:
        try:
            os.unlink(src)   # the editor's temp file, holding the edit, on any failed move (L1 r3)
        except OSError:
            pass
        raise


def _floored_move_impl(policy: CrownJewelsPolicy, src, dst) -> str:
    dst = _trusted_spelling(os.path.abspath(os.path.expanduser(str(dst))))
    name = os.path.basename(dst)
    pfd, walked = _judged_dir(os.path.dirname(dst))
    try:
        target = os.path.join(walked, name)
        reason = crown_jewel_reason(policy, target)
        if reason is None:
            try:
                st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if st is not None and stat.S_ISLNK(st.st_mode):
                reason = f"{target} is a symlink; the editor does not replace a link it wrote through"
            elif st is not None and st.st_nlink > 1:
                reason = linked_jewel_reason(policy, target)
        if reason is not None:
            raise _FloorRefusedWalk(reason)
        try:
            os.rename(src, name, dst_dir_fd=pfd)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            # The temp file is on another filesystem: copied to a new name in the held directory and
            # renamed over the target there, so the target name is replaced, never opened (a FIFO
            # or a hardlink planted at it is not written through; L1 r3).
            # The source is opened once, without following a link, and judged like any editor
            # open: the temp file sits in a directory the sandbox shares, so its name may have been
            # swapped for a link to a jewel (codex, closing pass).
            sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(sfd, "rb") as fin:
                sst = os.fstat(sfd)
                if not stat.S_ISREG(sst.st_mode) or sst.st_uid != os.geteuid():
                    raise _FloorRefusedWalk(f"{src}: the editor's temp file was replaced")
                why = opened_file_reason(policy, sfd)
                if why is not None:
                    raise _FloorRefusedWalk(why)
                tmp = f".{name}.levain-{os.urandom(6).hex()}"
                out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666,
                              dir_fd=pfd)
                try:
                    with os.fdopen(out, "wb") as fout:   # owns `out` from here, closed on any exit
                        shutil.copyfileobj(fin, fout)
                    os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
                except BaseException:
                    try:
                        os.unlink(tmp, dir_fd=pfd)
                    except OSError:
                        pass
                    raise
            os.unlink(src)
        # Re-judged by what is now there, as an open is judged by the object it opened. A swapped
        # source moved in by the rename (a link, say) is removed again, not left in the workspace.
        st = os.stat(name, dir_fd=pfd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            try:
                os.unlink(name, dir_fd=pfd)
            except OSError:
                pass
            raise _FloorRefusedWalk(f"{target}: what the move put there is not the editor's file")
        return target
    finally:
        os.close(pfd)


def _entries(fd: int) -> list[os.DirEntry]:
    with os.scandir(fd) as it:
        return sorted(it, key=lambda e: e.name)


def _install_floored_dir_view(editor_cls) -> None:
    """The directory ``view``, counted and listed through a held, judged fd. A child directory is
    listed only when it is a real directory (a link is shown, never entered) that passes the same
    judgement. Lines are formatted by the stock editor's own formatter."""
    stock_count = editor_cls._count_hidden_children
    stock_list = editor_cls._list_directory_for_view

    def count(self, path):
        if _EDITOR_FLOOR.get() is None:
            return stock_count(self, path)
        fd, _ = _judged_dir(path)
        try:
            return sum(1 for e in _entries(fd) if e.name.startswith("."))
        finally:
            os.close(fd)

    def listing(self, path):
        policy = _EDITOR_FLOOR.get()
        if policy is None:
            return stock_list(self, path)
        fd, walked = _judged_dir(path)
        shown = [path]
        try:
            for e in _entries(fd):
                if e.name.startswith("."):
                    continue
                shown.append(path / e.name)
                if not e.is_dir(follow_symlinks=False):
                    continue
                try:
                    cfd = os.open(e.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except OSError:
                    continue
                try:
                    if _held_dir_reason(policy, cfd, os.path.join(walked, e.name)) is None:
                        shown += [path / e.name / c.name for c in _entries(cfd)
                                  if not c.name.startswith(".")]
                except OSError:
                    pass
                finally:
                    os.close(cfd)
        finally:
            os.close(fd)
        return [self._format_directory_entry(path, entry) for entry in shown]

    editor_cls._count_hidden_children = count
    editor_cls._list_directory_for_view = listing


class _FlooredShutil:
    """The editor module's ``shutil`` with ``move`` floored; everything else is the real module."""

    move = staticmethod(_floored_move)

    def __getattr__(self, name):
        return getattr(shutil, name)


# THE EDITOR'S NAME-BASED STATS (head ruling 2026-10-07). exists / is_dir / is_file / getsize /
# getmtime / is_binary run in levain's own process, outside the sandbox, and by NAME, so a parent link
# flipped after the executor's check revealed a denied path's existence, type and size. They now go
# through the same walk: the parent is held and judged, the last name is opened from it (a link there
# is followed, and the object reached is judged as the floored open judges it), and the answer comes
# from that fd. Anything the floor would refuse answers exactly as a missing path does.


def _floored_stat(path) -> os.stat_result | None:
    """The stat of ``path`` through the floor, or None when it is missing, unreachable without
    following a link the walk refuses, or something the floor denies (all three look the same)."""
    policy = _EDITOR_FLOOR.get()
    # Mapped whole first: the trusted root may be the path itself (the workspace dir).
    p = _trusted_spelling(os.path.abspath(os.path.expanduser(str(path))))
    try:
        pfd, walked = _judged_dir(os.path.dirname(p))
    except (_FloorRefusedWalk, OSError):
        return None
    try:
        name = os.path.basename(p)
        if not name:
            return os.fstat(pfd)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=pfd)
        except PermissionError:
            # Unreadable, so not opened: stat it, and judge it by identity against the jewels.
            st = os.stat(name, dir_fd=pfd)
            if crown_jewel_reason(policy, os.path.join(walked, name)) is not None:
                return None
            try:
                jewels = _jewel_inodes(policy)
            except ConfinementError:
                return None
            return None if (st.st_dev, st.st_ino) in jewels else st
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                reason = _held_dir_reason(policy, fd, os.path.join(walked, name))
            else:
                reason = opened_file_reason(policy, fd)
            return None if reason is not None else st
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    finally:
        os.close(pfd)


class _FlooredPath(type(Path())):
    """The editor module's ``Path``: while a floored call runs, ``exists`` / ``is_dir`` / ``is_file``
    / ``stat`` answer through :func:`_floored_stat`; otherwise they are ``Path``'s own."""

    def exists(self, *args, **kwargs):
        if _EDITOR_FLOOR.get() is None:
            return super().exists(*args, **kwargs)
        return _floored_stat(self) is not None

    def is_dir(self, *args, **kwargs):
        if _EDITOR_FLOOR.get() is None:
            return super().is_dir(*args, **kwargs)
        st = _floored_stat(self)
        return st is not None and stat.S_ISDIR(st.st_mode)

    def is_file(self, *args, **kwargs):
        if _EDITOR_FLOOR.get() is None:
            return super().is_file(*args, **kwargs)
        st = _floored_stat(self)
        return st is not None and stat.S_ISREG(st.st_mode)

    def stat(self, *args, **kwargs):
        if _EDITOR_FLOOR.get() is None:
            return super().stat(*args, **kwargs)
        st = _floored_stat(self)
        if st is None:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return st


def _floored_size_or_mtime(attr: str, stock):
    def fn(path):
        if _EDITOR_FLOOR.get() is None:
            return stock(path)
        st = _floored_stat(path)
        if st is None:
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return getattr(st, attr)
    return fn


class _FlooredOsPath:
    """``os.path`` for the editor modules, with ``getsize`` and ``getmtime`` floored."""

    getsize = staticmethod(_floored_size_or_mtime("st_size", os.path.getsize))
    getmtime = staticmethod(_floored_size_or_mtime("st_mtime", os.path.getmtime))

    def __getattr__(self, name):
        return getattr(os.path, name)


class _FlooredOs:
    """``os`` for the editor modules: the real module with ``path`` floored."""

    path = _FlooredOsPath()

    def __getattr__(self, name):
        return getattr(os, name)


def _floored_is_binary(stock):
    def fn(filename, *args, **kwargs):
        if _EDITOR_FLOOR.get() is None:
            return stock(filename, *args, **kwargs)
        from binaryornot.helpers import is_binary_string

        if kwargs.get("check_extensions", True):
            from binaryornot.check import has_binary_extension

            if has_binary_extension(filename):
                return True
        from binaryornot import check as _check

        with _floored_open(filename, "rb") as fh:   # judged by the object opened
            return is_binary_string(fh.read(getattr(_check, "CHUNK_SIZE", 512)))
    return fn


def _install_floored_open() -> None:
    from openhands.tools.file_editor import editor as _editor_mod
    from openhands.tools.file_editor.utils import encoding as _encoding_mod

    for mod in (_editor_mod, _encoding_mod):
        mod.open = _floored_open   # type: ignore[attr-defined]
        mod.Path = _FlooredPath   # type: ignore[attr-defined]
        mod.os = _FlooredOs()   # type: ignore[attr-defined]
    _editor_mod.shutil = _FlooredShutil()   # type: ignore[attr-defined]
    _editor_mod.is_binary = _floored_is_binary(_editor_mod.is_binary)   # type: ignore[attr-defined]
    _install_floored_dir_view(_editor_mod.FileEditor)


_install_floored_open()

if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState

__all__ = [
    "LEVAIN_HANDS_TOOL",
    "CrownJewelsFileEditorExecutor",
    "LevainFileEditorTool",
    "SandboxedBashExecutor",
    "LevainBashTool",
    "LevainHands",
    "build_entity_tools",
]

# The REGISTRY key (the ``Tool(name=...)`` spec name) for BOTH hands. Deliberately DISTINCT from the
# stock ``"file_editor"`` / ``"terminal"`` so an entity's tool set can never resolve to an UNCONFINED
# stock tool. Each resolved tool keeps its own ``.name`` == the stock name (the SDK's tools_map keys
# on ``tool.name``, not the spec name), so the LLM still sees the FAMILIAR function names (better
# tool-use reliability for weak open models) while the registry stays collision-free.
LEVAIN_HANDS_TOOL = "levain_hands"


def _log_never_raises(msg: str) -> None:
    """``_log.exception(msg)`` that cannot propagate — for use inside teardown paths.

    ⛔ A LOGGING CALL IS NOT A SAFE STATEMENT. `logging.Handler.emit` implementations are expected
    to route their own failures through `handleError` (which prints and swallows), but that is a
    convention of the stdlib handlers, not a guarantee of the interface: a custom handler whose
    `emit` raises directly propagates out of `_log.exception`. Anywhere a log call sits between a
    failure and the cleanup for that failure, it is a second failure point that can strand the
    cleanup — which is precisely how `_close_candidate_shell` could leak a rejected shell while
    reporting that it was falling back.
    """
    try:
        _log.exception(msg)
    except BaseException:  # noqa: BLE001 — a teardown path may never be replaced by its own logging
        pass


def _close_candidate_shell(candidate: SandboxedShell) -> None:
    """Tear down a rejected shell without ever letting the teardown replace the refusal.

    ⛔ codex L3 MED, 2026-09-04. Two defects in the previous one-liner: a raising `close()` MASKED
    the original `ConfinementError` (a caller matching on type saw "close failed" rather than "no
    effective policy"), and — worse — an OVERRIDDEN `close()` that raises before doing any cleanup
    left its processes and descriptors alive, held by the shell's own reader threads, after
    the only application reference was dropped. Repeated refusals could then exhaust resources.
    ▶ So: try the object's own `close()`, and if that fails fall back to the base-class teardown.
    Nothing here is allowed to propagate.

    ⚠ **AND THE FALLBACK'S GUARANTEE IS NARROWER THAN AN EARLIER VERSION OF THIS DOCSTRING CLAIMED**
    (glm-5.2 L3 LOW, 2026-09-04). It said the base path is one "a subclass cannot have replaced".
    Not exactly: `SandboxedShell.close` dispatches to `self._signal()`, an ordinary overridable
    method, so a subclass that overrode BOTH could still defeat the fallback. What is actually
    guaranteed is that the base `close` BODY runs rather than the override's — which is what matters
    for the case this exists for, an override that raises before doing any teardown.
    ⚠ It also cannot run a subclass's ADDITIVE cleanup, because it does not know about it. That is
    the subclass's problem to solve locally, and `_SeatbeltShell.close` now unlinks its profile in a
    `finally` for exactly this reason."""
    # ⛔ THE BASE TEARDOWN RUNS UNCONDITIONALLY — codex L3 round 9, 2026-09-05. This used to
    # `return` when the override's `close()` did not RAISE, so an override that silently NO-OPS
    # (returns cleanly having torn down nothing) skipped the base path entirely and leaked the
    # processes, their process groups, the state dir and the reader threads on every rejected spawn.
    # The fallback covered overrides that raise and not overrides that lie, and the docstring
    # above already conceded that narrowness rather than fixing it.
    # ⚠ SAFE BECAUSE IT IS VERIFIED, NOT BECAUSE IT IS ASSERTED: read `SandboxedShell.close` in
    # confinement.py — it is "Idempotent, never raises", sets `_closed` first and nulls every
    # resource behind a None-guard, so the second call is a no-op when the override did its job.
    # ⛔ CITE THE SYMBOL, NOT THE LINE (Diogenes LOW, 2026-09-06). This used to name a line in
    # confinement.py, and it was EXACT when written — the next commit, six minutes later, inserted
    # into `crown_jewel_reason` above it, and the cited line became a blank one while the real
    # definition moved down. A symbol survives an insertion above it; a line number is invalidated
    # by any edit anywhere earlier in the file.
    # ⛔ AND THE FIRST VERSION OF THIS PARAGRAPH RESTATED THE TWO COORDINATES IT WAS RETIRING
    # (Diogenes LOW, 2026-09-07) — "…:1694 became a blank line while the real definition moved to
    # :1703". ⚡ THE SECOND NUMBER WAS NEVER EXACT AT ALL, AND GIT SAYS SO (L1, 2026-09-07).
    # Tracing `SandboxedShell.close` in confinement.py across the commits that moved it:
    # `bdb4b2f` had it at 1694 and wrote ":1694" — correct · `23fedaf`, six minutes later,
    # moved it to 1703 and killed that citation · `fd7c4d4` moved it to 1717 AND IS THE COMMIT
    # THAT WROTE ":1703" — `git log -S"1703" -- <this file>` finds it.
    # ⛔ So the replacement coordinate was FOURTEEN LINES WRONG AT THE INSTANT IT WAS COMMITTED:
    # the same edit invalidated the number and wrote the sentence about the number. That is
    # strictly sharper than the "went stale later" story it replaced, and the earlier claim that
    # "both were exact when written" was false for one of the two.
    # ▶ The numbers survive here only as the QUOTED dead claim, deliberately not refreshed, and
    # the distance is stated in words for the same reason: a fresh figure inside the paragraph
    # retiring a figure is how this defect keeps regenerating.
    # ▶ The coordinates are GONE rather than corrected: updating them to today's numbers only
    # reproduces the defect on a two-week timer, and the narrative carries its whole argument
    # without them.
    #
    # ⛔⛔ THE BASE TEARDOWN IS IN A `finally` — codex L3 round 10, 2026-09-06, and it is THIS
    # RELEASE'S OWN CLASS ARRIVING ONE LAYER UP. Round 9 removed a `return` so the base path could
    # not be skipped by an override that lies. But the base path was still reached only by falling
    # off the end of the `except` below, and `_log.exception` IS NOT GUARANTEED NOT TO RAISE: a
    # custom handler whose `emit` raises without routing through `handleError` propagates straight
    # out of the logging call. A logging failure would then skip the teardown and leak the shell —
    # exactly the defect round 9 closed, reintroduced through the line that REPORTS it.
    # ▶ So the guarantee is now STRUCTURAL rather than resting on the logging call's good
    # behaviour: `finally` runs whatever happens above it, and `_log_never_raises` cannot
    # propagate. Either alone would do; together the invariant does not depend on the helper
    # being correct, which is the point.
    try:
        try:
            candidate.close()
        except BaseException:
            _log_never_raises("a rejected shell's close() raised; falling back to the base teardown")
    finally:
        try:
            SandboxedShell.close(candidate)     # non-overridable path; idempotent
        except BaseException:
            _log_never_raises("base teardown of a rejected shell also failed; it may leak")

class _SharedFloor:
    """The ONE evolving :class:`CrownJewelsPolicy` for ONE CONVERSATION, read by BOTH hands.

    ⛔ WHY IT EXISTS — glm-5.2 L3, 2026-09-04. Each hand's ``create`` then built its own policy
    separately, producing two EQUAL BUT DISTINCT policy objects. That
    was harmless while the spawn-time socket refresh touched only ``deny_sockets`` (the connect arm
    has no in-process twin), and stopped being harmless once the refresh also updated
    ``deny_write_files`` / ``socket_spellings`` / ``deny_write_dirs``, which the file editor DOES
    enforce. The bash hand then evolved a floor the file-editor hand never saw.

    ⛔⛔ **MUTATE THROUGH :meth:`absorb`, NEVER BY ASSIGNING ``.policy`` — codex L3 MED, 2026-09-04,
    EXECUTION-REPRODUCED.** The previous version was assigned wholesale from the spawning executor's
    snapshot under that executor's OWN lock. A per-instance lock does not serialize anything when the
    object being mutated is SHARED: two executors could each read policy ``P``, spawn against ``P+A``
    and ``P+B``, and the second assignment would discard the first — *"a previously denied live
    container socket becomes reachable again"* after the losing executor respawned.
    ⚡ Two independent defects in one line, and both are now structural rather than disciplinary: the
    lock lives with the DATA it protects instead of with one of its writers, and the write is a UNION
    MERGE instead of a replace, so a lost update is impossible rather than merely unlikely. Even with
    per-conversation scoping (which leaves one bash mutator) this is kept — a floor that is only
    correct because of who happens to call it is a contract, and this file's own history says
    contracts drift."""

    __slots__ = ("_policy", "_lock", "_refusal", "_on_refuse")

    def __deepcopy__(self, memo: dict[int, Any]) -> "_SharedFloor":
        # The SDK's fork deep-copies a conversation's events, and the system-prompt event holds the
        # built hands. That copy is a record, never executed: the fork's agent builds its OWN hands
        # (and floor) from its tool spec. A lock cannot be copied, so the record shares this object.
        return self

    def __init__(self, policy: CrownJewelsPolicy) -> None:
        self._policy = policy
        self._lock = threading.Lock()
        self._refusal: str | None = None
        self._on_refuse: list[Any] = []   # weak references to hands' revoke callbacks

    def on_refuse(self, callback: Any) -> None:
        """Register a bound method to call once, outside the lock, when the floor is refused. Held
        weakly, so a discarded hand is not kept alive by its floor."""
        with self._lock:
            self._on_refuse = [r for r in self._on_refuse if r() is not None]
            self._on_refuse.append(weakref.WeakMethod(callback))

    @property
    def refusal(self) -> str | None:
        """Why this conversation's floor can no longer be trusted, or None. Sticky once set."""
        return self._refusal

    def refuse(self, reason: str) -> None:
        """Mark the floor unusable for BOTH hands (spore-1308 follow-on, codex L3 2026-10-03): a refresh
        that fails, e.g. on a store a trust file started naming in an unsafe place, used to leave the
        file editor on the old floor while only bash refused."""
        with self._lock:
            if self._refusal is not None:
                return
            self._refusal = reason
            callbacks = [ref() for ref in self._on_refuse]
        # Revoke actively, not at the next poll: a live shell (and anything it backgrounded) is
        # killed now, so it cannot keep acting on a floor that was just given up (codex L3 r5).
        for cb in callbacks:
            if cb is not None:
                try:
                    cb()
                except Exception:  # noqa: BLE001 — a failing revoke must not stop the others
                    pass

    def refresh(self) -> CrownJewelsPolicy:
        """Re-derive the evolving denies (sockets, trust-listed stores) and absorb them, before a hand
        acts. A failure refuses the floor and re-raises."""
        if self._refusal is not None:
            raise ConfinementError(self._refusal)
        try:
            refreshed = refresh_socket_denies(self.policy)
        except Exception as exc:   # any failure: a floor that could not be re-derived is not trusted
            self.refuse(str(exc))
            raise ConfinementError(str(exc)) from exc
        self.absorb(refreshed)   # raises if another hand refused meanwhile
        return self.policy

    @property
    def policy(self) -> CrownJewelsPolicy:
        return self._policy

    def absorb(self, spawned: CrownJewelsPolicy) -> None:
        """UNION the evolving fields of ``spawned`` into the live policy, under the floor's own lock:
        the four socket fields, and ``deny_read_write``, which gains the project stores a trust file
        names after the conversation opened (spore-1308 follow-on; without it a late store was denied
        to bash and left open to the file editor, codex L3 2026-10-03). Monotonic: a merge can only ever ADD a deny, never drop one — the same
        fail-closed property :func:`refresh_socket_denies` provides within a single spawn, extended
        across concurrent spawners."""
        def _union(a: tuple[Path, ...], b: tuple[Path, ...]) -> tuple[Path, ...]:
            seen: set[Path] = set()
            out: list[Path] = []
            for q in list(a) + list(b):
                if q not in seen:
                    seen.add(q)
                    out.append(q)
            return tuple(out)

        with self._lock:
            if self._refusal is not None:
                # Refused by another hand while this one was refreshing or spawning: publishing
                # now would let it act on a floor the conversation already gave up (codex L3 r4).
                raise ConfinementError(self._refusal)
            cur = self._policy
            # Named explicitly rather than **kwargs: `dataclasses.replace` is type-checked per field,
            # and a **dict defeats that on the one object where a wrong field is a security defect.
            self._policy = dataclasses.replace(
                cur,
                deny_sockets=_union(cur.deny_sockets, spawned.deny_sockets),
                deny_write_files=_union(cur.deny_write_files, spawned.deny_write_files),
                socket_spellings=_union(cur.socket_spellings, spawned.socket_spellings),
                deny_write_dirs=_union(cur.deny_write_dirs, spawned.deny_write_dirs),
                deny_read_write=_union(cur.deny_read_write, spawned.deny_read_write),
            )


# --- the file-editor hand (relaxed to the crown-jewels floor) --------------------------------


def _drop_editor_history(executor: Any) -> None:
    """Remove a stock file-editor executor's history tempdir. ``FileEditor`` makes one with
    ``mkdtemp`` when the executor is CONSTRUCTED and nothing ever removes it, so a long-running host
    would accumulate one (holding cached copies of edited files) per conversation (codex L3,
    2026-10-02). Best-effort: a teardown never raises."""
    try:
        directory = executor.editor._history_manager.cache.directory
        shutil.rmtree(directory, ignore_errors=True)
    except Exception:  # noqa: BLE001 — an SDK that moved the attribute just leaks, as before
        pass


def _history_finalizer(executor: Any) -> Callable[[], Any]:
    """A run-once callable that removes ``executor``'s history tempdir, also run when the executor is
    garbage-collected or at interpreter exit. A no-op if the SDK moved the attribute."""
    try:
        directory = executor.editor._history_manager.cache.directory
    except Exception:  # noqa: BLE001 — same tolerance as _drop_editor_history
        return lambda: None
    return weakref.finalize(executor, _remove_if_owner, directory, os.getpid())


def _remove_if_owner(directory: str, owner_pid: int) -> None:
    # A forked child holds a copy of the executor; if the child collects it, the dir is still the
    # parent's (complement L3, reasoned). Accepted residual: a host that forks and lets the PARENT
    # exit first loses the dir under the surviving child (complement L3 r2).
    if os.getpid() == owner_pid:
        shutil.rmtree(directory, ignore_errors=True)


def _close_quietly(executor: Any) -> None:
    """Close an executor on a failure path. A close that raises an ordinary exception is logged,
    not raised, so the build failure that brought us here is the error the caller sees (complement
    L3, reasoned). KeyboardInterrupt and SystemExit still propagate, on purpose."""
    try:
        executor.close()
    except Exception:  # noqa: BLE001
        _log.warning("closing an executor after a failed tool build raised", exc_info=True)


class CrownJewelsFileEditorExecutor(FileEditorExecutor):
    """A :class:`~openhands.tools.file_editor.impl.FileEditorExecutor` fenced to the crown-jewels
    FLOOR (slice 2), relaxing the step-6 ``<entity>/workspace/`` jail.

    Before delegating to the shipped executor it asks
    :func:`~levain.firing.confinement.crown_jewel_reason` whether ``action.path`` is a crown jewel — on
    EVERY command, ``view`` INCLUDED (a view of the flow store is an isolation LEAK, not just an unsafe
    write; this is exactly why we do NOT reuse the SDK's ``allowed_edits_files`` allowlist, which
    exempts ``view``). A refusal returns an in-band error :class:`FileEditorObservation` — the model is
    told "no" and the turn continues; the filesystem is never touched. Anything NOT a crown jewel is
    allowed (broad reach, like bash) — the incoherent asymmetry of a networked bash + a workspace-jailed
    editor is gone.

    The guard runs PER OP (the point-of-use invariant), so a symlink swapped into the tree after
    construction is caught by its resolved target. The stock editor REQUIRES an absolute path
    (``FileEditor.validate_path`` rejects a relative path before operating), so the path
    ``crown_jewel_reason`` resolves is the exact path that would be touched — no cwd-base mismatch.
    ``workspace_root`` is passed to the stock editor only as its relative-path SUGGESTION base (it was
    never a real jail — its containment is cosmetic), NOT as a confinement; the floor is the
    confinement."""

    def __init__(
        self,
        *,
        policy: CrownJewelsPolicy | None = None,
        floor: "_SharedFloor | None" = None,
        **kwargs: Any,
    ) -> None:
        # `policy=` remains the simple form (tests, direct construction) and gets a PRIVATE floor,
        # so this hand behaves exactly as before. `floor=` is what `create` passes to share the
        # evolving floor with the bash hand (glm L3, 2026-09-04).
        # ⛔ EXACTLY ONE OF `policy` / `floor` (codex L3 LOW, 2026-09-04). Accepting both and
        # silently preferring `floor` is fail-OPEN: a caller passing a strict policy alongside an
        # accidentally permissive floor got the permissive one with no error. On a crown-jewels
        # constructor, a configuration mistake must be a TypeError, not a silent preference.
        if (policy is None) == (floor is None):
            raise TypeError(
                "CrownJewelsFileEditorExecutor takes EXACTLY ONE of policy= or floor= "
                "(got both or neither) — passing both would silently ignore the policy."
            )
        if floor is None:
            floor = _SharedFloor(policy)  # type: ignore[arg-type]
        self._floor = floor
        super().__init__(workspace_root=str(self._floor.policy.workspace), **kwargs)
        # The history dir is removed when this executor is closed OR collected, whichever comes
        # first: an executor built by a tool build that failed later (in the SDK's agent init) never
        # reaches the agent, so nothing would ever close it (L1, 2026-10-02, reproduced). A per-
        # instance finalizer holding only the dir's path: no lock, no shared state.
        self._history_cleanup = _history_finalizer(self)

    def __deepcopy__(self, memo: dict[int, Any]) -> "CrownJewelsFileEditorExecutor":
        # Same reason as `SandboxedBashExecutor.__deepcopy__`, plus one: a copy would share the
        # history dir that only this instance's finalizer owns.
        return self

    @property
    def _policy(self) -> CrownJewelsPolicy:
        """Read THROUGH the shared floor, never a cached copy — that copy going stale while the bash
        hand's evolved is exactly the divergence this indirection exists to prevent."""
        return self._floor.policy

    def close(self) -> None:
        """Remove this executor's history tempdir (the SDK closes executors at conversation close),
        then run the parent's close, so cleanup a future SDK adds there still happens."""
        try:
            self._history_cleanup()
        finally:
            # Chained for whatever a later SDK puts there; no test, because there was nothing to run
            # [judged 2026-10-02 against openhands-tools 1.26.0: FileEditorExecutor defines no close,
            # and ToolExecutor.close is a no-op] (glm + complement L3 r2, reasoned).
            super().close()

    def __call__(
        self,
        action: "FileEditorAction",
        conversation: Any = None,
    ) -> FileEditorObservation:
        try:
            policy = self._floor.refresh()
        except ConfinementError as exc:
            return FileEditorObservation.from_text(
                text=f"REFUSED (crown-jewels floor could not be refreshed): {exc}",
                command=action.command,
                is_error=True,
            )
        reason = crown_jewel_reason(policy, action.path) or linked_jewel_reason(policy, action.path)
        if reason is not None:
            return FileEditorObservation.from_text(
                text=(
                    f"REFUSED (crown-jewels floor): {reason}. Your hands reach the rest of the "
                    "filesystem, but the sovereignty crown jewels are structurally off-limits."
                ),
                command=action.command,
                is_error=True,
            )
        token = _EDITOR_FLOOR.set(policy)
        try:
            return super().__call__(action, conversation)
        except (_FloorRefusedOpen, _FloorRefusedWalk) as exc:
            return FileEditorObservation.from_text(
                text=(
                    f"REFUSED (crown-jewels floor): {exc}. Your hands reach the rest of the "
                    "filesystem, but the sovereignty crown jewels are structurally off-limits."
                ),
                command=action.command,
                is_error=True,
            )
        finally:
            _EDITOR_FLOOR.reset(token)


class LevainFileEditorTool(FileEditorTool):
    """The floored file editor, built by :class:`LevainHands` (registry key :data:`LEVAIN_HANDS_TOOL`).

    Reuses the stock tool's rich (vision-aware) description + schema + annotations and swaps in the
    crown-jewels-floored executor — so the model sees the identical, familiar ``file_editor`` contract,
    now fenced to the floor. The LLM-visible ``.name`` stays ``"file_editor"`` (familiar → better
    tool-use on weak open models; the SDK keys ``tools_map`` on it) while the REGISTRY key is
    ``"levain_hands"`` (``register_tool`` below), so the unconfined stock tool is never reachable
    from an entity. The explicit ``name`` short-circuits the SDK's ``__init_subclass__`` auto-derivation.

    It must be a REAL ``LevainFileEditorTool`` instance (not a stock ``FileEditorTool`` with a swapped
    executor) so that OUR :meth:`declared_resources` override is the one the runtime calls — the
    pre-executor surface the confinement must also own (codex L3)."""

    name: ClassVar[str] = "file_editor"

    def declared_resources(self, action: Action) -> DeclaredResources:
        """Own the PRE-EXECUTOR path surface too (codex L3, non-replaceable catch).

        OpenHands' ``ParallelToolExecutor`` calls ``declared_resources()`` BEFORE the executor to
        compute file locks, and the STOCK version does ``Path(action.path).resolve()`` — which RAISES
        on a malformed path (an LLM-emittable embedded NUL) inside the executor's ``try``, surfacing a
        raw ``AgentErrorEvent`` and SKIPPING the executor entirely, so the floored executor's clean
        in-band refusal never fires. So run the fence here too; on a crown-jewel path OR a malformed
        path, declare NO lock (``declared=True``, empty keys) — the runtime treats that as "safe, no
        resources" and STILL RUNS the executor, which then returns the real refusal Observation. Never
        raises."""
        assert isinstance(action, FileEditorAction)
        try:
            resolved = Path(action.path).expanduser().resolve()
        # RuntimeError too — `expanduser()` raises it for a `~user` with no passwd entry, and
        # THIS function's docstring says "Never raises." A RuntimeError escaping here does the
        # exact harm the paragraph above describes: it surfaces a raw AgentErrorEvent and SKIPS
        # the executor, so the floored executor's clean in-band refusal never fires — the guard
        # written to stop that, defeated by the spelling it did not catch.
        except (ValueError, OSError, RuntimeError):
            return DeclaredResources(keys=(), declared=True)
        if crown_jewel_reason(self._policy(), resolved) is not None:
            return DeclaredResources(keys=(), declared=True)
        return DeclaredResources(keys=(f"file:{resolved}",), declared=True)

    def _policy(self) -> CrownJewelsPolicy:
        """The floored executor's crown-jewels policy. ``create`` always wires our executor, so this
        holds."""
        assert isinstance(self.executor, CrownJewelsFileEditorExecutor)
        return self.executor._policy

    @classmethod
    def create(  # type: ignore[override]
        cls, conv_state: "ConversationState", *, floor: "_SharedFloor"
    ) -> list["LevainFileEditorTool"]:
        """Build around ``floor`` — the one :class:`LevainHands` builds for both hands. Not registered
        on its own: an entity reaches it only through ``levain_hands``."""
        # The stock tool is built only to borrow its description/schema; its own executor (and the
        # history tempdir that executor made) is discarded here. It is built FIRST, so that if it
        # raises, ours (which makes its own history dir) does not exist yet (L3 r2, three seats).
        stocks = FileEditorTool.create(conv_state)
        for stock in stocks:
            _drop_editor_history(stock.executor)
        floored = CrownJewelsFileEditorExecutor(floor=floor)
        try:
            # Build REAL LevainFileEditorTool instances (not stock via set_executor, which keeps the
            # stock class + its raising declared_resources), reusing the stock tool's rich
            # description/schema/annotations by copying its fields — so our declared_resources
            # override is what runs.
            return [
                cls(
                    description=stock.description,
                    action_type=stock.action_type,
                    observation_type=stock.observation_type,
                    annotations=stock.annotations,
                    executor=floored,
                )
                for stock in stocks
            ]
        except BaseException:
            # Reasoned, no test: only the pydantic validation inside `cls(...)` can raise here (L1).
            _close_quietly(floored)
            raise


# --- the bash hand (a stateful OS-sandboxed shell) -----------------------------------------


class SandboxedBashExecutor(ToolExecutor[TerminalAction, TerminalObservation]):
    """Drives spore-311's :class:`~levain.firing.confinement.SandboxedShell` (a stateful
    OS-confined bash — ``sandbox-exec`` on macOS, ``bwrap`` on Linux since K4c) instead of the SDK's
    un-confinable host ``TerminalExecutor``.

    The shell is spawned LAZILY on the first command — a ``--no-tools`` or never-touch-bash session
    pays nothing, and a spawn failure (no usable OS sandbox on this host) becomes a clean in-band refusal,
    never a conversation-build crash (fail-closed: no floor → no unconfined shell). The
    ``SandboxedShell`` is SINGLE-CALLER; :meth:`LevainBashTool.declared_resources` serializes bash calls
    against each other so two never race the one shell. ``reset`` closes + respawns a fresh shell;
    ``exit N`` ends only that command (its status is reported); ``is_input`` (interactive stdin)
    is refused — the confined shell is a non-interactive dev shell + agent-auth SSH, no PTY."""

    def __deepcopy__(self, memo: dict[int, Any]) -> "SandboxedBashExecutor":
        # Same reason as `_SharedFloor.__deepcopy__`: a fork's event record shares this executor;
        # the fork runs bash through the executor its own agent builds.
        return self

    def __init__(
        self,
        policy: CrownJewelsPolicy | None = None,
        *,
        floor: "_SharedFloor | None" = None,
        default_timeout: float = 120.0,
    ) -> None:
        # ⛔ EXACTLY ONE OF `policy` / `floor` (codex L3 LOW, 2026-09-04). Accepting both and
        # silently preferring `floor` is fail-OPEN: a caller passing a strict policy alongside an
        # accidentally permissive floor got the permissive one with no error. On a crown-jewels
        # constructor, a configuration mistake must be a TypeError, not a silent preference.
        if (policy is None) == (floor is None):
            raise TypeError(
                "SandboxedBashExecutor takes EXACTLY ONE of policy= or floor= "
                "(got both or neither) — passing both would silently ignore the policy."
            )
        if floor is None:
            floor = _SharedFloor(policy)  # type: ignore[arg-type]
        self._floor = floor
        self._default_timeout = default_timeout
        floor.on_refuse(self._revoke)

        self._shell: SandboxedShell | None = None
        # Guards the lazy spawn / teardown against interrupt()/close() from another thread. Bash calls
        # themselves are serialized by declared_resources, so this is only the cross-thread guard.
        self._lock = threading.Lock()

    @property
    def _policy(self) -> CrownJewelsPolicy:
        """Read THROUGH the shared floor — same indirection as the file-editor hand, so the two
        cannot drift apart as the floor evolves at each spawn (glm L3, 2026-09-04)."""
        return self._floor.policy

    def _ensure_shell(self) -> SandboxedShell:
        """The live shell — spawning a fresh one on first use OR after the previous one exited
        (reset). Raises :class:`ConfinementError` (caught by :meth:`__call__` → in-band
        refusal) if no OS confinement floor can be established here."""
        with self._lock:
            if self._shell is None or self._shell.closed:
                provider = select_provider()  # raises ConfinementError off a supported platform
                # ⛔ VALIDATE THE CANDIDATE BEFORE COMMITTING IT TO `self._shell` (codex L3 HIGH,
                # 2026-09-04). The previous version assigned FIRST and raised AFTER — so the first
                # command was refused while the LIVE shell stayed cached, and the NEXT command saw a
                # non-closed `_shell`, skipped this branch entirely, and executed through the
                # unverified shell. **The advertised fail-closed check was fail-once/open-next**,
                # which is worse than no check: it emits exactly one refusal that reads as the guard
                # working, and then silently stops guarding.
                # ⛔⛔ THE REFRESH HAPPENS HERE, UPSTREAM OF THE PROVIDER — AND THAT IS WHAT
                # REPLACED A METACLASS GUARD THAT WAS DEFEATED SEVEN TIMES ACROSS FIVE VERSIONS
                # (Phill ruled 2026-09-04). The provider is handed an ALREADY-REFRESHED policy, so
                # a provider cannot skip the re-resolution: there is no step for it to omit.
                # ⚡ The guard tried to make it impossible to SKIP a call. This makes the call not
                # exist at that layer -- for a provider that implements the `_spawn_shell_impl`
                # seam. The base `spawn_shell` refreshes again on its own behalf, so a consumer
                # calling it directly is covered too.
                # (A comment here once claimed the K4c branch's BwrapProvider inherited this by
                # construction; it overrode `spawn_shell` directly and did not. The port fixed it,
                # and `test_no_shipped_provider_overrides_spawn_shell` holds it.)
                # Refreshed and PUBLISHED to the shared floor before the start, so the file editor
                # holds the same denies even if the start fails; a refresh failure refuses both hands
                # (codex L3 2026-10-03).
                refreshed = self._floor.refresh()
                try:
                    candidate = provider.spawn_shell(
                        refreshed, default_timeout=self._default_timeout
                    )
                except FloorRefreshError as exc:
                    # The provider's own refresh failed: a refusal of the floor for both hands,
                    # recorded from the typed error rather than by replaying the derivation (codex
                    # L3 r5). A failure of the start itself (no bwrap, a bad host) is a plain
                    # ConfinementError and leaves the editor usable.
                    self._floor.refuse(str(exc))
                    raise
                try:
                    # `effective_policy` is Optional on the TYPE because a SandboxedShell built
                    # directly (not through the provider seam) legitimately has none.
                    effective = candidate.effective_policy
                    if effective is None:
                        raise ConfinementError(
                            "the confined shell carries no effective policy — the provider seam did "
                            "not stamp it, so the socket floor it was rendered with is unknown "
                            "(fail-closed)."
                        )
                    # ⛔ THE COMMIT IS INSIDE THE PROTECTED BLOCK (codex L3 LOW, 2026-09-04). An
                    # asynchronous exception landing after validation but before the assignment
                    # would otherwise leave a LIVE shell that is neither cached nor closed — its
                    # reader thread keeps it, and the subprocess outlives the refusal.
                    # ⛔ PUBLISHED BEFORE THE ABSORB (codex, reproduced, L3 r6): a refusal landing
                    # between a successful absorb and this assignment found no shell to revoke, and
                    # the command then ran on the live candidate. Now a refusal that lands first makes
                    # `absorb` raise (the handler below closes the candidate), and one that lands
                    # after finds the candidate here and closes it.
                    self._shell = candidate
                    # MERGE, never assign: `absorb` unions under the FLOOR'S OWN lock, so a
                    # concurrent spawner's denies cannot be lost to a wholesale overwrite.
                    self._floor.absorb(effective)
                except BaseException:
                    # ⛔ BaseException, NOT Exception (codex L3 MED). A KeyboardInterrupt or
                    # SystemExit raised by `close()` would otherwise REPLACE the original refusal,
                    # which is the one signal this whole path exists to preserve.
                    self._shell = None
                    _close_candidate_shell(candidate)
                    raise
            return self._shell

    def _revoke(self) -> None:
        """Called by the floor when it is refused: kill the live shell now. Takes NO lock: the floor
        can be refused from inside this executor's own locked spawn (its refresh fails there), and
        ``SandboxedShell.close`` is thread-safe and idempotent by contract. Nothing respawns after,
        because every call checks the refusal before it reaches the shell."""
        shell = self._shell
        if shell is not None:
            try:
                shell.close()
            except Exception:  # noqa: BLE001 — revocation is best-effort; the refusal itself stands
                pass

    def _teardown(self) -> None:
        with self._lock:
            shell, self._shell = self._shell, None
        if shell is not None:
            shell.close()

    def __call__(
        self,
        action: TerminalAction,
        conversation: Any = None,
    ) -> TerminalObservation:
        if action.reset and action.is_input:
            # Mirror the stock TerminalExecutor's contract for this invalid combination.
            raise ValueError("Cannot use reset=True with is_input=True")
        if action.is_input:
            return self._error(
                action,
                "This confined shell does not support interactive input (is_input=True): it runs "
                "non-interactive dev commands + agent-auth SSH, with no PTY. Run the program "
                "non-interactively instead (flags/env, a heredoc, or `yes |`).",
            )
        if self._floor.refusal is not None:
            self._revoke()   # a live shell must not outlive a refusal set by the other hand
            return self._error(action, f"crown-jewels floor refused: {self._floor.refusal}")
        if action.reset:
            self._teardown()
            if not action.command.strip():
                return TerminalObservation.from_text(
                    text=(
                        "Sandboxed shell reset — a fresh confined bash will start on the next "
                        "command; env, cwd, and shell state were cleared."
                    ),
                    command="[RESET]",
                    exit_code=0,
                    metadata=CmdOutputMetadata(exit_code=0),
                )

        try:
            shell = self._ensure_shell()
        except ConfinementError as exc:
            return self._error(
                action,
                f"could not establish the OS confinement floor ({exc}). Refusing to run bash "
                "without the sandbox (fail-closed).",
            )
        try:
            result = shell.run(action.command, timeout=action.timeout)
        except ConfinementError as exc:
            return self._error(action, f"confined shell error: {exc}")

        if result.timed_out:
            limit = action.timeout if action.timeout is not None else self._default_timeout
            text = (
                f"{result.output}\n[command timed out after {limit:.0f}s; levain killed it and every "
                "process it started in its session. The next command starts from the shell state "
                "saved by the last command that finished]"
            )
            return TerminalObservation.from_text(
                text=text,
                command=action.command,
                exit_code=-1,
                timeout=True,
                metadata=CmdOutputMetadata(exit_code=-1),
                is_error=True,
            )

        code = result.exit_code
        if code is None:
            # A signal ended the command's bash (levain's waitpid says which). Reported the way a
            # shell reports it, 128 + the signal number, and as an error; the shell itself is fine
            # and the next command starts from the last saved state.
            sig = result.signal or 0
            try:
                name = signal.Signals(sig).name
            except ValueError:
                name = f"signal {sig}"
            return TerminalObservation.from_text(
                text=f"{result.output}\n[the command was ended by {name}]",
                command=action.command,
                exit_code=128 + sig,
                metadata=CmdOutputMetadata(exit_code=128 + sig),
                is_error=True,
            )
        return TerminalObservation.from_text(
            text=result.output,
            command=action.command,
            exit_code=code,
            metadata=CmdOutputMetadata(exit_code=code),
            is_error=code != 0,
        )

    # A refusal is NOT a timeout: use 126 ("command cannot execute" convention), not -1 — the SDK's
    # Rich visualizer renders -1 as "Process still running (soft timeout)", the wrong label for a
    # fail-closed refusal (apparatus L1). is_error=True + the REFUSED text carry the real meaning.
    _REFUSAL_EXIT_CODE = 126

    @classmethod
    def _error(cls, action: TerminalAction, text: str) -> TerminalObservation:
        return TerminalObservation.from_text(
            text=f"REFUSED: {text}",
            command=action.command,
            exit_code=cls._REFUSAL_EXIT_CODE,
            metadata=CmdOutputMetadata(exit_code=cls._REFUSAL_EXIT_CODE),
            is_error=True,
        )

    def interrupt(self) -> None:
        """Best-effort Ctrl-C the running command (called from another thread on a conversation
        interrupt). Never raises."""
        shell = self._shell
        if shell is not None:
            shell.interrupt()

    def close(self) -> None:
        """Reap the sandboxed bash + its whole process group (the SDK calls this on conversation
        teardown — 'always close tool executors, they hold runtime resources'). Idempotent."""
        self._teardown()


class LevainBashTool(TerminalTool):
    """The confined bash tool, built by :class:`LevainHands` (registry key :data:`LEVAIN_HANDS_TOOL`).

    Reuses the stock terminal tool's schema (``TerminalAction``/``TerminalObservation``) + platform
    description + annotations and swaps in the :class:`SandboxedBashExecutor` — so the model sees the
    familiar ``terminal`` contract, now riding an OS sandbox instead of the un-confinable host shell.
    The LLM-visible ``.name`` stays ``"terminal"`` (the SDK keys ``tools_map`` on it) while the REGISTRY
    key is ``"levain_hands"``, so the unconfined stock terminal is never reachable from an entity."""

    name: ClassVar[str] = "terminal"

    def declared_resources(self, action: Action) -> DeclaredResources:  # noqa: ARG002
        """Serialize bash calls against each other — the :class:`~levain.firing.confinement.SandboxedShell`
        is SINGLE-CALLER (its ``run()`` fails fast on a concurrent call), so two bash tool-calls must
        never run at once. Declare the shared session key UNCONDITIONALLY — unlike the stock
        ``TerminalTool`` (which opts OUT of serialization under a tmux pane pool), our confined shell has
        no pool. Own this pre-executor surface explicitly (the codex L3 lesson: the confinement owns
        EVERY surface the runtime consults, not just ``__call__``)."""
        return DeclaredResources(keys=("terminal:session",), declared=True)

    @classmethod
    def create(  # type: ignore[override]
        cls, conv_state: "ConversationState", *, floor: "_SharedFloor"
    ) -> list["LevainBashTool"]:
        """Build around ``floor`` (see :meth:`LevainFileEditorTool.create`)."""
        executor = SandboxedBashExecutor(floor=floor)
        # Pass our executor to the stock create so it does NOT build a host TerminalExecutor; then copy
        # its (platform-correct) description/schema/annotations into a REAL LevainBashTool so OUR
        # declared_resources override runs.
        return [
            cls(
                action_type=stock.action_type,
                observation_type=stock.observation_type,
                description=stock.description,
                annotations=stock.annotations,
                executor=executor,
            )
            for stock in TerminalTool.create(conv_state, executor=executor)
        ]


class LevainHands(ToolDefinition[Action, Observation]):
    """BOTH hands for one conversation, registered under :data:`LEVAIN_HANDS_TOOL` (spore-438).

    The SDK resolves a spec as ``create(conv_state=..., **spec.params)``, and may resolve separate
    specs concurrently. One spec for both hands means ONE ``create`` call builds ONE
    :class:`_SharedFloor` and hands it to both executors as a local value — so the two hands share the
    floor by construction, with no registry keyed on the conversation.
    The floor itself comes from ``binding``, the
    :class:`~levain.firing.binding.ConversationBinding` the session resolved once; this reads no file
    and resolves no mode."""

    name: ClassVar[str] = LEVAIN_HANDS_TOOL

    @classmethod
    def create(  # type: ignore[override]
        cls,
        conv_state: "ConversationState",
        *,
        binding: dict[str, Any],
        with_bash: bool = True,
    ) -> list[ToolDefinition[Any, Any]]:
        floor = _SharedFloor(ConversationBinding.from_params(binding).floor)
        tools: list[ToolDefinition[Any, Any]] = [
            *LevainFileEditorTool.create(conv_state, floor=floor)
        ]
        if with_bash:
            try:
                tools.extend(LevainBashTool.create(conv_state, floor=floor))
            except BaseException:
                # The SDK never sees a tool list that failed to build, so nothing else would close
                # the editor built above (L3 r2).
                built = {id(t.executor): t.executor for t in tools if t.executor is not None}
                for executor in built.values():
                    _close_quietly(executor)
                raise
        return tools


# Register at import so ``Tool(name="levain_hands")`` resolves. Importing this module also imports the
# stock definitions (which self-register ``"file_editor"`` / ``"terminal"``) — harmless: an entity only
# ever references the levain name. A duplicate re-import just warns (registry is last-write-wins with
# the same resolver), never raises.
register_tool(LEVAIN_HANDS_TOOL, LevainHands)


def build_entity_tools(binding: ConversationBinding, *, with_bash: bool = True) -> list[Tool]:
    """The confined executor-tool bundle for a ``levain run`` entity: the crown-jewels-floored file
    editor + (``with_bash``) the OS-sandboxed bash, both fenced by ``binding``'s floor. Returns ONE
    ``Tool`` SPEC carrying the binding as data; the SDK resolves it into both hands at
    conversation-build time (:class:`LevainHands`).

    These are the ONLY blessed executor-tool builders Levain ships, and both are confined by
    construction. ``with_bash=False`` drops bash — the caller (``levain run``) passes it when no OS
    confinement floor can be established here
    (:func:`~levain.firing.confinement.confinement_supported`), so the entity keeps its file-editor
    hand rather than getting a bash whose first command would fail-closed. NEVER grants an unconfined
    shell as a fallback.

    ⚠ "HERE", not "on this platform": since K4c a Linux host HAS a provider, so the common Linux
    case is a supported platform that still cannot establish a floor (AppArmor-restricted user
    namespaces). The bash-free entity is therefore a normal Linux configuration rather than an
    exotic one, and the file-editor floor is fully cross-platform by design."""
    return [
        Tool(
            name=LEVAIN_HANDS_TOOL,
            params={"binding": binding.to_params(), "with_bash": with_bash},
        )
    ]
