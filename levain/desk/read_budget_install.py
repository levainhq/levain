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


def install(home: str | os.PathLike[str]) -> tuple[str, str]:
    """``(script, state_dir)``, absolute: the script written to ``<home>/read_budget/read_budget-<sha[:16]>.py`` (0600,
    replaced whole when missing, holding other bytes, or not mode 0600) and the state directory made (0700). OSError is the caller's."""
    base = Path(os.path.abspath(home)) / SUBDIR
    state = base / "state"
    for d in (base, state):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)      # a directory that was already there keeps its old mode otherwise
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
