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
import re
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


def _bases(base: Path) -> list[str]:
    """The spellings of the levain home a command may carry: the home as given and resolved, and, for the user's own
    ``~/.levain`` only, its ``~``, ``$HOME`` and ``${HOME}`` spellings."""
    out: list[str] = []
    for b in (str(base), str(base.resolve())):
        if b not in out:
            out.append(b)
    try:
        is_user_home = base.resolve() == (Path.home() / ".levain").resolve()
    except OSError:
        is_user_home = False
    if is_user_home:
        out.extend(["~/.levain", "$HOME/.levain", "${HOME}/.levain"])
    return out


def _old_path_re(base: Path) -> "re.Pattern[str]":
    """The old desk home path in a command: after the start, a quote, whitespace or ``=``; before a ``/``, the end, a quote,
    whitespace or a shell separator (so the shadow directory and a longer name that merely starts with the old one are never matched)."""
    alts = "|".join(re.escape(f"{b}/{LEGACY_HOME_NAME}") for b in _bases(base))
    return re.compile(rf"""(?:(?<=^)|(?<=[\s"'=]))(?P<head>{alts})(?=/|$|[\s"'`;&|)])""")


def rewrite_text(text: str, base: Path) -> str:
    """``text`` with the old desk home path rewritten to the new one."""
    def sub(m: "re.Match[str]") -> str:
        head = m.group("head")
        return head[: -len(LEGACY_HOME_NAME)] + DESK_HOME_NAME

    return _old_path_re(base).sub(sub, text)


def rewrite_settings(data: Any, base: Path) -> tuple[Any, list[tuple[str, str]]]:
    """``data`` with every string under a ``command`` key rewritten from the old home path to the new one; returns it and
    the (before, after) pairs changed. A path that only starts with the old name (the shadow directory) is not matched."""
    changed: list[tuple[str, str]] = []

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            res = {}
            for k, v in node.items():
                if k == "command" and isinstance(v, str):
                    nv = rewrite_text(v, base)
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


class _Changed(Exception):
    """A settings file changed between the read and the replace."""


def _replace_bytes(path: Path, raw: bytes, expected: bytes | None = None) -> None:
    """Replace ``path`` (through a link: the link's target) with ``raw``, atomically. With ``expected``, the file must still
    hold exactly those bytes when the replace happens (one check, no retry)."""
    target = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        try:
            os.chmod(tmp, target.stat().st_mode & 0o777)
        except OSError:
            pass
        if expected is not None and target.read_bytes() != expected:
            raise _Changed(f"{path} changed while it was being rewritten")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_atomic(path: Path, data: Any, expected: bytes | None = None) -> None:
    raw = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _replace_bytes(path, raw, expected)


def _read_stored_setting(db: Path) -> tuple[str, str | None, str | None, bool] | None:
    """The desk's latest stored setting ``(agent, model, cwd, user_scope)`` read-only, or None (no store, no row, unreadable)."""
    import sqlite3

    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            try:
                r = conn.execute("SELECT agent, model, cwd, user_scope FROM coord_settings ORDER BY seq DESC LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                r = conn.execute("SELECT agent, model, cwd, 0 FROM coord_settings ORDER BY seq DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return None if r is None else (r[0], r[1], r[2], bool(r[3]))


def move_home(*, home: Path | str | None = None, settings: Path | str | None = None, dry_run: bool = False,
              out: Any = None, err: Any = None, store_dir: Path | str | None = None) -> int:
    """Run the move. Exit 0 done / nothing to do / dry-run, 1 refused or failed (everything put back)."""
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
    neither = not old_is_dir and not old_is_link and not new_exists

    # every settings file to rewrite: the user's, and the desk home's own project settings (the consent hook and the gate
    # are rendered there with the absolute install path)
    src_dir = old if do_rename else new
    files: list[dict[str, Any]] = []
    changed: list[tuple[str, str]] = []
    candidates: list[tuple[Path, Path]] = [(spath, spath)]
    if not neither:
        for name in ("settings.json", "settings.local.json"):
            candidates.append((src_dir / ".claude" / name, new / ".claude" / name))
    for read_from, write_to in candidates:
        if not read_from.exists():
            continue
        try:
            raw = read_from.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            return refuse(f"{read_from} is not readable valid JSON ({type(exc).__name__}: {str(exc)[:120]})")
        new_data, ch = rewrite_settings(data, base)
        if ch:
            files.append({"read": read_from, "path": write_to, "raw": raw, "new": new_data, "n": len(ch)})
            changed.extend(ch)

    stored = _read_stored_setting((Path(store_dir) if store_dir is not None else _default_store_dir()) / _db_name())
    new_cwd = None
    if stored is not None and stored[0] != "none" and stored[2]:
        fixed = rewrite_text(stored[2], base)
        if fixed != stored[2]:
            new_cwd = fixed

    if neither:
        if changed:
            return refuse(f"no desk home to move ({old} and {new} are both absent), yet {spath} names the old path; "
                          "set the desk up again, or edit the hook by hand")
        say(f"no desk home to move: neither {old} nor {new} exists")
        return 0
    do_link = new_exists and not old_is_link and not old.exists() and bool(files or new_cwd)
    if not do_rename and not do_link and not files and new_cwd is None:
        say(f"nothing to do: the desk home is {desk_home(base)}, and no hook command in {spath} or the desk home's own "
            f"settings names the old path")
        return 0

    verb = "would" if dry_run else "will"
    if do_rename:
        say(f"{verb} rename {old} -> {new}, and keep {old} as a link to {DESK_HOME_NAME}")
    if do_link:
        say(f"{verb} make {old} a link to {DESK_HOME_NAME}")
    for f in files:
        say(f"{verb} rewrite the hook commands in {f['path']}")
    for before, after in changed:
        say(f"{verb} rewrite a hook command:\n    {before}\n -> {after}")
    if not changed:
        say(f"no hook command in {spath} or the desk home's settings names the old path")
    if new_cwd is not None:
        say(f"{verb} move the desk's stored directory {stored[2]} -> {new_cwd}")  # type: ignore[index]
    if dry_run:
        say("dry run: nothing was changed")
        return 0

    renamed = linked = False
    written: list[tuple[Path, bytes]] = []
    try:
        if do_rename:
            os.rename(old, new)
            renamed = True
        if do_rename or do_link:
            os.symlink(DESK_HOME_NAME, old)       # before any settings write: a failure here rolls back the rename alone
            linked = True
        for f in files:
            path = f["path"]
            backup = path.with_name(f"{path.name}.pre-desk-move")
            if not backup.exists():
                shutil.copy2(path, backup)
            _write_atomic(path, f["new"], f["raw"])
            written.append((path, f["raw"]))
        if new_cwd is not None:
            from levain.desk.store import CoordStore

            agent, model, _cwd, scope = stored  # type: ignore[misc]
            CoordStore(store_dir if store_dir is not None else _default_store_dir()).coord_set_agent(
                agent, model=model, cwd=new_cwd, user_scope=scope)
    except Exception as exc:
        for path, raw in reversed(written):
            try:
                _replace_bytes(path, raw)
            except OSError:
                pass
        if linked and old.is_symlink():
            try:
                os.unlink(old)
            except OSError:
                pass
        if renamed and not old.exists() and not old.is_symlink():
            try:
                os.rename(new, old)
            except OSError:
                pass
        print(f"levain desk move-home: failed ({type(exc).__name__}: {str(exc)[:160]}); everything this run changed was "
              f"put back (the directory, the link and every settings file)", file=err)
        return 1
    if do_rename:
        say(f"renamed {old} -> {new} (the old path is a link to it for one release)")
    elif do_link:
        say(f"made {old} a link to {new} for one release")
    for f in files:
        say(f"rewrote {f['n']} hook command(s) in {f['path']} (the earlier file is kept as {f['path'].name}.pre-desk-move)")
    if new_cwd is not None:
        say(f"moved the desk's stored directory to {new_cwd}")
    # the doctor row again
    from levain.doctor import _check_desk_home, _emit

    rows = _check_desk_home(base)
    if rows:
        for r in rows:
            _emit(r)
    else:
        say("  doctor: your desk home is the new name")
    return 0


def _default_store_dir() -> Path:
    from levain.dispatch.jobs import default_dispatch_dir

    return default_dispatch_dir()


def _db_name() -> str:
    from levain.dispatch.jobs import DB_NAME

    return DB_NAME


def run_move_home(args: Any, out: Any = None) -> int:
    return move_home(home=getattr(args, "home", None), settings=getattr(args, "settings", None),
                     dry_run=bool(getattr(args, "dry_run", False)), out=out)
