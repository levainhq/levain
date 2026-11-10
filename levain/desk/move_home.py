"""levain.desk.move_home: ``levain desk move-home``, the one command that renames an old install's desk home.

An install made before the desk rename keeps its desk home under the old directory name (:data:`levain.desk.legacy.
LEGACY_HOME_NAME`); :func:`levain.desk.legacy.desk_home` still finds it there. A Claude Code consent hook in the
user's ``settings.json`` may name a file under that directory, and renaming the directory alone would leave that hook dead. This verb
does both: it renames ``<levain home>/<old>`` to ``<levain home>/desk``, rewrites every hook command in ``settings.json``
that names the old home path, and keeps the old path as a symlink to the new directory for one release.

Safe by construction: it refuses (changes nothing, exit 1) when the new directory already exists beside a real old one or
when ``settings.json`` is not valid JSON; ``--dry-run`` prints the plan and changes nothing; ``settings.json`` is written
through a temp file and ``os.replace`` after a ``settings.json.pre-desk-move`` copy is kept (an existing copy is the older
original and is kept as it is); a failed settings write puts the directory back. A second run, with the move done and no
hook left to rewrite, says there is nothing to do. The shadow directory (:data:`levain.desk.legacy.LEGACY_SHADOW_NAME`) is
left alone.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from levain.desk.legacy import DESK_HOME_NAME, LEGACY_HOME_NAME, desk_home

SETTINGS_ENV = "LEVAIN_CLAUDE_SETTINGS"
BACKUP_NAME = "settings.json.pre-desk-move"


def levain_home(override: Path | str | None = None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get("LEVAIN_HOME")
    return Path(env).expanduser() if env else Path.home() / ".levain"


def settings_path(override: Path | str | None = None) -> Path:
    """``--settings``, else ``$LEVAIN_CLAUDE_SETTINGS``, else ``~/.claude/settings.json``."""
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get(SETTINGS_ENV)
    return Path(env).expanduser() if env else Path.home() / ".claude" / "settings.json"


def _prefixes(base: Path) -> list[tuple[str, str]]:
    """(old, new) text pairs a hook command may carry: the absolute old home as given and resolved, and the ``~`` spelling."""
    pairs: list[tuple[str, str]] = []
    for b in (str(base), str(base.resolve())):
        pair = (f"{b}/{LEGACY_HOME_NAME}/", f"{b}/{DESK_HOME_NAME}/")
        if pair not in pairs:
            pairs.append(pair)
    if base.name == ".levain":
        pairs.append((f"~/.levain/{LEGACY_HOME_NAME}/", f"~/.levain/{DESK_HOME_NAME}/"))
    return pairs


def rewrite_settings(data: Any, base: Path) -> tuple[Any, list[tuple[str, str]]]:
    """``data`` with every string under a ``command`` key rewritten from the old home path to the new one; returns it and
    the (before, after) pairs changed. A path that only starts with the old name (the shadow directory) is not matched."""
    pairs = _prefixes(base)
    changed: list[tuple[str, str]] = []

    def fix(text: str) -> str:
        out = text
        for old, new in pairs:
            out = out.replace(old, new)
        return out

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            res = {}
            for k, v in node.items():
                if k == "command" and isinstance(v, str):
                    nv = fix(v)
                    if nv != v:
                        changed.append((v, nv))
                    res[k] = nv
                else:
                    res[k] = walk(v)
            return res
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(data), changed


def _write_atomic(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def move_home(*, home: Path | str | None = None, settings: Path | str | None = None, dry_run: bool = False,
              out: Any = None, err: Any = None) -> int:
    """Run the move. Exit 0 done / nothing to do / dry-run, 1 refused (nothing changed)."""
    out = out or sys.stdout
    err = err or sys.stderr
    base = levain_home(home)
    old, new = base / LEGACY_HOME_NAME, base / DESK_HOME_NAME
    spath = settings_path(settings)

    def say(line: str) -> None:
        print(line, file=out)

    def refuse(why: str) -> int:
        print(f"levain desk move-home: {why}; nothing was changed", file=err)
        return 1

    old_is_link = old.is_symlink()
    old_is_dir = old.is_dir() and not old_is_link
    new_exists = new.exists()
    if old_is_dir and new_exists:
        return refuse(f"both {old} and {new} exist; move one aside first")
    do_rename = old_is_dir and not new_exists
    if old_is_link and not new_exists:
        return refuse(f"{old} is a link and {new} does not exist")

    data: Any = None
    changed: list[tuple[str, str]] = []
    new_data: Any = None
    if spath.exists():
        try:
            data = json.loads(spath.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return refuse(f"{spath} is not readable valid JSON ({type(exc).__name__}: {str(exc)[:120]})")
        new_data, changed = rewrite_settings(data, base)

    if not do_rename and not changed:
        say(f"nothing to do: the desk home is {desk_home(base)}, and no hook in {spath} names the old path")
        return 0

    verb = "would" if dry_run else "will"
    if do_rename:
        say(f"{verb} rename {old} -> {new}, and keep {old} as a link to {DESK_HOME_NAME}")
    for before, after in changed:
        say(f"{verb} rewrite a hook command in {spath}:\n    {before}\n -> {after}")
    if not changed:
        say(f"no hook command in {spath} names the old path" if spath.exists() else f"{spath} does not exist: no hook to rewrite")
    if dry_run:
        say("dry run: nothing was changed")
        return 0

    renamed = False
    try:
        if do_rename:
            os.rename(old, new)
            renamed = True
        if changed:
            backup = spath.with_name(BACKUP_NAME)
            if not backup.exists():
                shutil.copy2(spath, backup)
            _write_atomic(spath, new_data)
        if do_rename:
            os.symlink(DESK_HOME_NAME, old)
    except OSError as exc:
        if renamed and not old.exists() and not old.is_symlink():
            try:
                os.rename(new, old)
            except OSError:
                pass
        print(f"levain desk move-home: failed ({type(exc).__name__}: {str(exc)[:160]}); the directory was put back where it was "
              f"if it had moved", file=err)
        return 1
    if do_rename:
        say(f"renamed {old} -> {new} (the old path is a link to it for one release)")
    if changed:
        say(f"rewrote {len(changed)} hook command(s) in {spath} (the earlier file is kept as {spath.with_name(BACKUP_NAME)})")
    # the doctor row again
    from levain.doctor import _check_desk_home, _emit

    rows = _check_desk_home(base)
    if rows:
        for r in rows:
            _emit(r)
    else:
        say("  doctor: your desk home is the new name")
    return 0


def run_move_home(args: Any, out: Any = None) -> int:
    return move_home(home=getattr(args, "home", None), settings=getattr(args, "settings", None),
                     dry_run=bool(getattr(args, "dry_run", False)), out=out)
