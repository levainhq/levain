"""One answer to "is this path inside that one", by IDENTITY, not by spelling (key 1106-1; the class of 1105-49).

The problem. This Mac's volume is case-insensitive APFS: ``/USERS/me/x`` and ``/Users/me/x`` are the same file, and
``os.path.realpath`` / ``Path.resolve`` keep the spelling they were given. A containment test written as a string prefix
(``str(p).startswith(str(root) + "/")``, ``root in p.parents``) therefore says "outside" for an alias of a path that is
inside, and a DENY check built on it is skipped. The same holds for a second mount of one volume and for a firmlink.

The helpers here ask the filesystem for its own spelling of a path (``canonical``), compare by that spelling and, failing
that, by ``(st_dev, st_ino)`` of the path's existing ancestors (``inside``). Stdlib only, Python 3.9 syntax: the Linux
root-side modules import it, and root runs the system interpreter. The filesystem's own SPELLING is recovered only where
``fcntl.F_GETPATH`` exists (macOS); everywhere else ``canonical`` keeps the spelling given and only the identity step
can catch an alias.

Two directions, because a GRANTS site and a DENY site want opposite answers when a stat fails:

* :func:`inside` is True only when sure (a GRANTS caller: "may this path be treated as under the root?").
* :func:`maybe_inside` is True when :func:`inside` is, or when any stat or resolve error left the question open (a DENY
  caller: "is this path surely or possibly under the protected directory?").
* :func:`maybe_same` is the DENY answer to "is this the same path": equal canonical spellings, the same file, or unsure.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

try:                                    # F_GETPATH exists on macOS only; elsewhere the spelling is the one given
    import fcntl as _fcntl
except ImportError:                     # pragma: no cover - a platform with no fcntl at all
    _fcntl = None  # type: ignore[assignment]

#: ``open(2)`` flag that names a file without reading it (macOS ``O_EVTONLY``); the stdlib does not export it.
_O_EVTONLY = getattr(os, "O_EVTONLY", 0x00008000 if sys.platform == "darwin" else 0)
_ABSENT = (errno.ENOENT, errno.ENOTDIR)


class _Unsure(Exception):
    """The spelling of an EXISTING path could not be learned (internal: the seam's way to say "exists, unknowable")."""


def _fs_spelling(path: str) -> Optional[str]:
    """The filesystem's own spelling of ``path`` when it EXISTS, ``None`` when it does not (the test seam: tests replace
    this to stand in for a case-insensitive volume where none is at hand). Raises :class:`_Unsure` for a path that
    exists but whose spelling cannot be read (permission, a descriptor limit): the caller keeps the spelling it has
    and remembers it could not check.

    macOS answers ``fcntl.F_GETPATH`` on a descriptor for the path, which also resolves a firmlink alias to its
    canonical name. Where ``F_GETPATH`` does not exist (Linux) the answer is the path itself if it exists: identity."""
    getpath = getattr(_fcntl, "F_GETPATH", None) if _fcntl is not None else None
    if getpath is None:
        try:
            os.stat(path)
        except ValueError:                              # an embedded NUL names nothing
            return None
        except OSError as exc:
            if exc.errno in _ABSENT:
                return None
            raise _Unsure(str(exc)) from exc
        return path
    fd = -1
    last: Optional[OSError] = None
    for flags in (_O_EVTONLY, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)):   # O_EVTONLY needs no read permission
        try:
            fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
            break
        except ValueError:
            return None
        except OSError as exc:
            if exc.errno in _ABSENT:
                return None
            last = exc
    if fd < 0:
        raise _Unsure(str(last))
    try:
        return os.fsdecode(_fcntl.fcntl(fd, getpath, bytes(1024)).split(b"\0", 1)[0])
    except OSError as exc:
        raise _Unsure(str(exc)) from exc
    finally:
        os.close(fd)


def _canon(path) -> Tuple[Path, bool]:
    """``(canonical path, unsure)``: ``unsure`` is True when a resolve or spelling lookup failed on a path that exists,
    so the spelling returned is the best known and not the filesystem's own."""
    unsure = False
    try:
        p = Path(path).resolve()
    except (OSError, RuntimeError, ValueError, TypeError):   # a symlink loop, an embedded NUL, a permission error, not a path
        unsure = True
        try:
            p = Path(os.path.abspath(os.fspath(path)))
        except (OSError, ValueError, TypeError):
            p = Path(os.fspath(path)) if isinstance(path, (str, os.PathLike)) else Path(".")
    tail: List[str] = []
    base = p
    while True:
        try:
            spelled = _fs_spelling(str(base))
        except _Unsure:
            unsure = True
            spelled = str(base)
        if spelled is not None:
            base = Path(spelled)
            break
        if base == base.parent:                        # nothing on the way up exists (not even "/"): keep as given
            break
        tail.append(base.name)
        base = base.parent
    return base.joinpath(*reversed(tail)), unsure


def canonical(path) -> Path:
    """``path`` made absolute, resolved, and spelled as the filesystem spells it. ``Path.resolve()`` first (it follows
    links but keeps the case it was given); then the nearest existing ancestor is asked for its own spelling
    (macOS ``F_GETPATH``; identity where that does not exist) and the part that does not exist yet is joined on as
    given. Never raises for a path that does not exist."""
    return _canon(path)[0]


def _decide(root, path) -> Tuple[bool, bool]:
    """``(inside, unsure)``. Lexical on the canonical spellings first, then identity: each existing ancestor of ``path``
    is compared to ``root`` by ``(st_dev, st_ino)`` (``os.path.samestat``, the stat form of ``samefile``).

    An unsure side never takes the lexical answer: its spelling is the unresolved one, and a component of it may be a
    link that leads elsewhere. The identity walk then decides, and a match found ABOVE a component that exists and
    could not be stat'ed is unsure, not inside (that component is where the path could not be placed)."""
    try:
        Path(root), Path(path)
    except TypeError:                                   # not a path at all (None, an int): nothing can be said of it
        return False, True
    cr, ur = _canon(root)
    cp, up = _canon(path)
    unsure = ur or up
    if not unsure and (cp == cr or cr in cp.parents):
        return True, False
    try:
        want = os.stat(cr)
    except OSError as exc:
        # a root that does not exist is identical to nothing; one that exists and cannot be stat'ed is an open question
        return False, unsure or exc.errno not in _ABSENT
    except (ValueError, TypeError):                     # an embedded NUL: a root that cannot be stat'ed is an open question
        return False, True
    unplaced = False                                    # a component below this candidate exists and could not be stat'ed
    for cand in (cp, *cp.parents):
        try:
            st = os.stat(cand)
        except OSError as exc:
            if exc.errno not in _ABSENT:
                unsure = unplaced = True
            continue
        except (ValueError, TypeError):
            unsure = unplaced = True
            continue
        if os.path.samestat(want, st):
            return (False, True) if unplaced else (True, False)
    return False, unsure


def inside(root, path) -> bool:
    """True when ``path`` is ``root`` or below it, by identity: ``canonical(root)`` and ``canonical(path)`` equal or
    parent-related, or any existing ancestor of ``path`` the same file as ``root`` (``os.path.samestat``). True only
    when sure: a stat error on a path that exists is NOT "inside" (the direction for a GRANTS site; a DENY site
    calls :func:`maybe_inside`). Never raises."""
    return _decide(root, path)[0]


def maybe_inside(root, path) -> bool:
    """True when :func:`inside` is True, or when any stat or resolve error left the question open: "surely or possibly
    under ``root``", the direction for a DENY site that must skip nothing it could not check. Never raises."""
    hit, unsure = _decide(root, path)
    return hit or unsure


def maybe_same(a, b) -> bool:
    """True when ``a`` and ``b`` are surely or possibly one path: their canonical spellings are equal, or :func:`same`
    says one file, or resolving either one was unsure (a stat or resolve error, a bad argument). The direction for a
    DENY site ("is this the protected path?") that must refuse what it could not check. Never raises."""
    ca, ua = _canon(a)
    cb, ub = _canon(b)
    return ua or ub or ca == cb or same(a, b)


def same(a, b) -> bool:
    """``os.path.samefile(a, b)``, with an error (a missing path, a permission failure, a bad argument) read as False."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError, TypeError):
        return False
