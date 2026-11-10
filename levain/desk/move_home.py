"""levain.desk.move_home: ``levain desk move-home``, the one command that renames an old install's desk home.

An install made before the desk rename keeps its desk home under the old directory name (:data:`levain.desk.legacy.
LEGACY_HOME_NAME`); :func:`levain.desk.legacy.desk_home` still finds it there. A Claude Code consent hook in the
user's ``settings.json`` may name a file under that directory, and renaming the directory alone would leave that hook dead. This verb
does both: it renames ``<levain home>/<old>`` to ``<levain home>/desk``, rewrites every hook command in ``settings.json``
that names the old home path (and the desk home's own ``.claude`` settings), moves the desk's stored directory in each
store a desk can live in (the consent directory and the board), adds the new path to the door's workspace record beside
the old one, and keeps the old path as a symlink to the new directory for one release.

Safe by construction: it refuses (changes nothing, exit 1) when the new directory already exists beside a real old one or
when a settings file is not valid JSON; ``--dry-run`` prints the plan and changes nothing; each settings file is written
through a temp file and ``os.replace`` after a ``<name>.pre-desk-move`` copy is kept (an existing copy is the older
original and is kept as it is); a failure puts back the directory, the link, the stored directory and every settings
file (a record entry already added stays: it only doors one more directory). A second run, with the move done, the link
in place and no hook left to rewrite, says there is nothing to do. The shadow directory (:data:`levain.desk.legacy.LEGACY_SHADOW_NAME`) is
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
BACKUP_SUFFIX = ".pre-desk-move"                  # every rewritten settings file keeps <its name><suffix> beside it
BACKUP_NAME = "settings.json" + BACKUP_SUFFIX     # the user's settings.json's backup


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
    """The old desk home path in a command: after the start, a quote, whitespace, ``=`` or ``:`` (a ``PATH`` entry); before
    a ``/``, the end, a quote, whitespace or a shell separator (so the shadow directory and a longer name that merely starts
    with the old one are never matched)."""
    alts = "|".join(re.escape(f"{b}/{LEGACY_HOME_NAME}") for b in _bases(base))
    return re.compile(rf"""(?:(?<=^)|(?<=[\s"'=:]))(?P<head>{alts})(?=/|$|[\s"'`;&|):])""")


def rewrite_text(text: str, base: Path) -> str:
    """``text`` with the old desk home path rewritten to the new one."""
    def sub(m: "re.Match[str]") -> str:
        head = m.group("head")
        return head[: -len(LEGACY_HOME_NAME)] + DESK_HOME_NAME

    return _old_path_re(base).sub(sub, text)


def rewrite_path(path: str, base: Path) -> str:
    """A structured path (the stored directory, a door-record entry) with the old desk home rewritten to the new one: only
    when it IS the old home or lies under ``old + os.sep``, never by the command-text matcher (which takes ``:`` as a
    boundary, so ``<old>:archive/x`` is a different directory and is left alone)."""
    for b in _bases(base):
        old = f"{b}/{LEGACY_HOME_NAME}"
        if path == old or path.startswith(old + os.sep):
            return f"{b}/{DESK_HOME_NAME}" + path[len(old):]
    return path


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

    if not os.path.isfile(db):               # never create one: a store dir without a db stays without
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
    seen: set[str] = set()
    for read_from, write_to in candidates:
        if not read_from.exists():
            continue
        # one physical file once: --settings may name (or link to) the desk home's own settings.json, and a second
        # entry for it would see the first one's write as a concurrent change and roll the whole move back
        key = os.path.realpath(read_from)
        if key in seen:
            continue
        seen.add(key)
        try:
            raw = read_from.read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError) as exc:
            return refuse(f"{read_from} is not readable valid JSON ({type(exc).__name__}: {str(exc)[:120]})")
        new_data, ch = rewrite_settings(data, base)
        if ch:
            files.append({"read": read_from, "path": write_to, "raw": raw, "new": new_data, "n": len(ch)})
            changed.extend(ch)

    # the desk's stored directory, in every store a desk can live in (the consent directory and the board, as
    # levain.desk.cli._desk_dir finds them), each only where it still names the old path
    cwd_moves: list[tuple[Path, str, str]] = []
    for d in _store_dirs(base, store_dir):
        stored = _read_stored_setting(d / _db_name())
        # agent none has no directory; a codex row is left as it is: lease.set_agent refuses to write one (the Codex desk
        # was removed), so its directory cannot be re-appended, and the row launches nothing
        if stored is not None and stored[0] not in ("none", "codex") and stored[2]:
            fixed = rewrite_path(stored[2], base)
            if fixed != stored[2]:
                cwd_moves.append((d, stored[2], fixed))

    # the door's workspace record (levain.desk.door): it names real paths, so an entry under the old home is looked up
    # through the compat link today and misses once the link is gone. The new path is ADDED beside it through the door's
    # own writer (locked, atomic, add-only); the old entry stays, as the record never takes one out
    from levain.desk import door

    try:
        recorded = door.recorded()
    except door.RecordUnreadable as exc:
        # a desk never doored has no record file at all: skip the adds with a warning. An unreadable record that EXISTS
        # fails closed, as the door does
        if any(os.path.lexists(p) for p in door._record_paths()):
            return refuse(door.unreadable(exc))
        print(f"levain desk move-home: warning: the workspace record cannot be read ({str(exc)[:120]}) and no record "
              "file exists; the record adds are skipped", file=err)
        recorded = frozenset()
    record_adds = sorted({rewrite_path(n, base) for n in recorded} - set(recorded))

    if neither:
        if changed:
            return refuse(f"no desk home to move ({old} and {new} are both absent), yet {spath} names the old path; "
                          "set the desk up again, or edit the hook by hand")
        say(f"no desk home to move: neither {old} nor {new} exists")
        return 0
    # an interrupted move converges: with desk/ in place, the link is made when the old path is a link to nothing (repair)
    # or something else still needs changing; an absent old path with nothing pending is a clean post-rename install
    # (or an interrupted move that lost nothing), and gets no link
    broken_link = os.readlink(old) if old_is_link and not old.exists() else None
    pending = bool(files or cwd_moves or record_adds)
    do_link = new_exists and not old.exists() and (broken_link is not None or pending)
    if not do_rename and not do_link and not files and not cwd_moves and not record_adds:
        say(f"nothing to do: the desk home is {desk_home(base)}, and no hook command in {spath} or the desk home's own "
            f"settings names the old path")
        return 0

    verb = "would" if dry_run else "will"
    if do_rename:
        say(f"{verb} rename {old} -> {new}, and keep {old} as a link to {DESK_HOME_NAME}")
    if do_link:
        say(f"{verb} make {old} a link to {DESK_HOME_NAME}"
            + (f" (replacing a link to {broken_link}, which leads nowhere)" if broken_link is not None else ""))
    for f in files:
        say(f"{verb} rewrite the hook commands in {f['path']}")
    for before, after in changed:
        say(f"{verb} rewrite a hook command:\n    {before}\n -> {after}")
    if not changed:
        say(f"no hook command in {spath} or the desk home's settings names the old path")
    for d, was, to in cwd_moves:
        say(f"{verb} move the desk's stored directory {was} -> {to} (in {d})")
    for to in record_adds:
        say(f"{verb} add {to} to the desk's workspace record beside the old path")
    if dry_run:
        say("dry run: nothing was changed")
        return 0

    renamed = linked = False
    written: list[tuple[Path, bytes]] = []
    moved: list[tuple[Path, str, str]] = []           # (store dir, prior cwd, new cwd) actually appended
    try:
        if do_rename:
            os.rename(old, new)
            renamed = True
        if do_rename or do_link:
            if broken_link is not None:
                os.unlink(old)
            os.symlink(DESK_HOME_NAME, old)       # before any other write: a failure here rolls back the rename alone
            linked = True
        for to in record_adds:                    # add-only: a doored extra directory is the closed side, never undone
            door.record_moved(to)
        # the stored directory before the settings files, so a refusal here changes no file (L3 r2 C)
        from levain.desk.store import CoordStore

        for d, was, to in cwd_moves:
            if CoordStore(d).coord_move_cwd(was, to) is not None:
                moved.append((d, was, to))
        for f in files:
            path = f["path"]
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            if not backup.exists():
                shutil.copy2(path, backup)
            _write_atomic(path, f["new"], f["raw"])
            written.append((path, f["raw"]))
    except Exception as exc:
        for path, raw in reversed(written):
            try:
                _replace_bytes(path, raw)
            except OSError:
                pass
        for d, was, to in reversed(moved):
            # back to the prior directory, only while the row this run appended is still the latest (a concurrent
            # change since then is kept); its agent, model and user-layer choice were copied from the prior row
            try:
                CoordStore(d).coord_move_cwd(to, was)
            except Exception:
                pass
        if linked and old.is_symlink():
            try:
                os.unlink(old)
                if broken_link is not None:
                    os.symlink(broken_link, old)
            except OSError:
                pass
        if renamed and not old.exists() and not old.is_symlink():
            try:
                os.rename(new, old)
            except OSError:
                pass
        print(f"levain desk move-home: failed ({type(exc).__name__}: {str(exc)[:160]}); everything this run changed was "
              f"put back (the directory, the link, the stored directory and every settings file)", file=err)
        return 1
    if do_rename:
        say(f"renamed {old} -> {new} (the old path is a link to it for one release)")
    elif do_link:
        say(f"made {old} a link to {new} for one release")
    for to in record_adds:
        say(f"added {to} to the desk's workspace record")
    for f in files:
        say(f"rewrote {f['n']} hook command(s) in {f['path']} (the earlier file is kept as {f['path'].name}{BACKUP_SUFFIX})")
    for d, was, to in moved:
        say(f"moved the desk's stored directory to {to} (in {d})")
    for d, was, to in cwd_moves:
        if (d, was, to) not in moved:
            say(f"left the desk's stored directory in {d} as it is: it changed while this ran")
    # the doctor row again
    from levain.doctor import _check_desk_home, _emit

    rows = _check_desk_home(base)
    if rows:
        for r in rows:
            _emit(r)
    else:
        say("  doctor: your desk home is the new name")
    return 0


def _store_dirs(base: Path, store_dir: Path | str | None) -> list[Path]:
    """``store_dir`` alone when given; else the directories a desk store can be in under ``base``: the consent
    directory and the board (:func:`levain.desk.work_attach.live_consent_dir` / ``board_store_dir``), each once."""
    if store_dir is not None:
        return [Path(store_dir)]
    from levain.desk.work_attach import board_store_dir, live_consent_dir

    env = dict(os.environ, LEVAIN_HOME=str(base))
    out: list[Path] = []
    for d in (live_consent_dir(env), board_store_dir(env)):
        if all(os.path.realpath(d) != os.path.realpath(o) for o in out):
            out.append(d)
    return out


def _db_name() -> str:
    from levain.dispatch.jobs import DB_NAME

    return DB_NAME


def run_move_home(args: Any, out: Any = None) -> int:
    return move_home(home=getattr(args, "home", None), settings=getattr(args, "settings", None),
                     dry_run=bool(getattr(args, "dry_run", False)), out=out)
