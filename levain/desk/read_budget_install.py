"""levain.desk.read_budget_install — put the read budget's hook into the seats Levain launches (levain row 800).

The ruling (desk 14, 11-16): a seat LEVAIN launches carries the PreToolUse/PostToolUse read-budget hook by default,
through Levain's own seat launch settings; a session the operator starts himself (``user_scope`` on) is not one of
Levain's seats and gets none. Nothing here touches ``~/.claude/settings.json`` or flow's launcher.

The mechanism is the room's (:meth:`levain.desk.room_launch.RoomLauncher._put_script`): the hook script is
:mod:`levain.desk.read_budget` copied as it is (standard library only) to a file named by its sha256 under
``<home>/read_budget/``, so a running seat's script is never replaced by another version, and the seat runs the copy by
absolute path with ``python -I``. The state (the ledgers, ``grants.json``) is ``<home>/read_budget/state``, named to the
seat by ``LEVAIN_READ_BUDGET_DIR``: the hook is inert without it.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from levain.desk import read_budget

__all__ = ["SUBDIR", "install", "seat_budget"]

SUBDIR = "read_budget"


def _source() -> bytes:
    """The script as the package holds it now: read ONCE per install, written, named and hashed as those bytes."""
    return Path(read_budget.__file__).read_bytes()


def _paths(home: str | os.PathLike[str]) -> tuple[Path, Path]:
    base = Path(os.path.abspath(home)) / SUBDIR
    return base, base / "state"


def _nearest_existing(p: Path) -> Path:
    while not os.path.lexists(p) and p != p.parent:
        p = p.parent
    return p


def _prewrite_check(base: Path, state: Path) -> None:
    """The refusals :func:`install` makes BEFORE it writes, read-only, so :func:`planned` (the dry run) refuses
    exactly where the real install would (L1 wire2 F2): an existing ``read_budget`` or ``state`` that is a symlink
    (or not a directory), and a missing one whose nearest existing ancestor this process cannot write and enter."""
    for d in (base, state):
        if os.path.lexists(d):
            if d.is_symlink():
                raise OSError(f"{d} is a symlink; the read budget needs a plain directory")
            if not d.is_dir():
                raise NotADirectoryError(f"{d} is not a directory; the read budget needs a plain directory")
            continue
        anc = _nearest_existing(d)
        if not os.access(anc, os.W_OK | os.X_OK):
            raise PermissionError(f"{anc} is not writable; the read budget cannot make {d}")


def _make_dirs(d: Path) -> None:
    """``d`` and every missing parent, EACH made 0700 (``mkdir(parents=True)`` leaves the parents at the umask's mode)."""
    missing: list[Path] = []
    p = d
    while not os.path.lexists(p) and p != p.parent:
        missing.append(p)
        p = p.parent
    for m in reversed(missing):
        try:
            m.mkdir(mode=0o700)
        except FileExistsError:
            continue            # made meanwhile: the symlink check below still judges the budget's own directories
        os.chmod(m, 0o700)      # a umask can only narrow the mode; this pins it exactly


def planned(home: str | os.PathLike[str]) -> tuple[str, str]:
    """``(script, state_dir)`` exactly as :func:`install` would name them, WITHOUT writing anything (a dry run mints
    nothing), after the same pre-write refusals :func:`install` makes (:func:`_prewrite_check`)."""
    base, state = _paths(home)
    _prewrite_check(base, state)
    return str(base / f"read_budget-{hashlib.sha256(_source()).hexdigest()[:16]}.py"), str(state)


def install(home: str | os.PathLike[str]) -> tuple[str, str]:
    """``(script, state_dir)``, absolute: the script written to ``<home>/read_budget/read_budget-<sha[:16]>.py`` (0600,
    replaced whole when missing, holding other bytes, or not mode 0600) and the state directory made (0700, as is every
    directory it has to create on the way). OSError is the caller's."""
    base, state = _paths(home)
    _prewrite_check(base, state)
    for d in (base, state):
        _make_dirs(d)
        if d.is_symlink():
            raise OSError(f"{d} is a symlink; the read budget needs a plain directory")
        os.chmod(d, 0o700)    # a directory that was already there keeps its old mode otherwise
    data = _source()
    path = base / f"read_budget-{hashlib.sha256(data).hexdigest()[:16]}.py"
    try:
        same = path.read_bytes() == data and (path.stat().st_mode & 0o777) == 0o600
    except OSError:
        same = False
    if not same:
        tmp = base / f".read_budget.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    return str(path), str(state)


def seat_budget(home: str | os.PathLike[str], python: str | None = None) -> dict[str, Any]:
    """What a launched seat carries: ``{"hooks": <settings hooks object>, "env": {LEVAIN_READ_BUDGET_DIR: state}}``.
    ``python`` defaults to this process's interpreter (absolute)."""
    script, state = install(home)
    return {"hooks": read_budget.settings_hooks(python or sys.executable, script),
            "env": {read_budget.STATE_ENV: state}}
