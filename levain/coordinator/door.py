"""The coordinator's door: what may run of the ``levain`` CLI inside the coordinator's workspace.

np-6843ca07 (a), signed, and the head's 1021-18 rulings: a command run from inside the coordinator's workspace is
DEFAULT-DENY. A command whose path is not in :data:`DOOR` is refused, in every tree, and every write it does run goes
``--by coordinator`` under its lease token (an operator write is unfenced, so a session that lost its lease could
otherwise still write). A command not listed here is refused there until it is listed: the check reads the command
path off the parser itself, so a new tree needs no second list to be covered.

Provenance is WHERE the command runs, never a lookup of the session: the process's working directory, walked up to the
filesystem root, is inside a coordinator's workspace or is not. Both harnesses' tool subprocesses start in the
directory the session was launched in (measured, for ``claude -p`` and a Codex thread).

A directory is a coordinator's workspace when the COORDINATOR RECORD names it (head ruling R1, 1021+19): a file in the
default consent directory and the same file under the account's own home (from the password database), both outside
every root a coordinator's turn can write (a Codex coordinator's sandbox writes its workspace, never the consent
directory), written by ``levain coord agent`` before it records a setting and only ever added to; either naming a
directory is enough. The record names real paths; directories are compared by device and inode as well as by
spelling (a case-insensitive volume has many spellings of one). The workspace's own ``.levain/config.json`` role line
stays a HINT: it also makes a directory the coordinator's (so a workspace rendered before the record existed is still
doored), but its absence or damage never makes one outside, since the coordinator can write that file. A record that
exists and cannot be read, whose directory is not one, or that lies inside a recorded or hinted coordinator workspace
(the record file's own ancestors are walked for the hint, as the working directory's are) fails CLOSED, for every
command: the door cannot tell any directory is not the coordinator's. No database is read, so
no store state (locked,
corrupt, another store) can open the door. Consequences, accepted: the operator running ``levain`` from inside that
workspace is doored too (it is Levain's directory, not his), and an agent that changes directory out of it leaves the
door (the door is fencing for an honest coordinator; the lease token still fences every write).

:func:`refusal` is called once, from ``levain.cli.main``, after argument parsing. The hooks and Levain's MCP servers
are other entry points and never reach it. This is not a boundary against a hostile process of the same user: such a
process can change directory or import the package directly.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

# The coordinator's own verbs (its brief names these), the team-ledger draft it may write, and the team tree's read
# verbs. Never `team record` / `retire` / `draft promote`: the ledger is signed by the operator.
DOOR: frozenset[tuple[str, ...]] = frozenset({
    ("coord", "status"), ("coord", "claim"), ("coord", "beat"), ("coord", "release"),
    # the seats' questions, read first (C5): its own KNOWs and escalations, never the operator's answer verb
    ("coord", "questions"), ("coord", "answer"), ("coord", "escalate"),
    # away ON from a drop (fenced by the lease token), and the budget gauge and morning card it reads (C6)
    ("coord", "away"), ("coord", "budget"), ("coord", "morning"),
    ("drops", "list"), ("drops", "show"), ("drops", "split"), ("drops", "note"),
    ("thread", "list"), ("thread", "show"), ("thread", "state"), ("thread", "merge"), ("thread", "detach"),
    ("thread", "add"), ("thread", "set"),
    ("team", "draft", "add"), ("team", "draft", "list"), ("team", "draft", "show"),
    ("team", "status"), ("team", "verify"), ("team", "export"), ("team", "doctor"),
})

_DB_NAME = "dispatch.db"
_ROLE = "coordinator"
_MARKER_MAX = 64 * 1024
RECORD_NAME = "coordinator_workspaces.json"
_RECORD_KEY = "coordinator_workspaces"
_RECORD_MAX = 1024 * 1024


class RecordUnreadable(Exception):
    """The coordinator record exists and cannot be read as one, or lies where a coordinator could write it: the door
    fails closed on it. ``path`` is the record file; ``inside`` is the coordinator workspace it lies in, when that is
    the cause (the operator moves the record out; repairing or deleting the file would change nothing)."""

    def __init__(self, message: str, path: Path | None = None, inside: Path | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.inside = inside


def _own_home() -> Path | None:
    """The account's home from the password database (never ``$HOME``), or None where there is none."""
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return None


def _resolved(path: Path) -> Path:
    """``path`` with its directory resolved through every symlink; the file name itself is kept, so a record file
    that is a link is still refused by the read (``O_NOFOLLOW``)."""
    return Path(os.path.realpath(path.parent)) / path.name


def record_path() -> Path:
    """``<levain home>/consent/coordinator_workspaces.json``, its directory resolved: the coordinator record, in the
    default consent directory (the one ``$LEVAIN_HOME`` names, else ``~/.levain``)."""
    from levain.consent.queue import default_queue_dir

    return _resolved(default_queue_dir() / RECORD_NAME)


def _record_paths() -> list[Path]:
    """Where the door reads records and :func:`record` writes them: :func:`record_path`, and the same file under the
    account's own home from the password database, so a process started with another ``$LEVAIN_HOME`` or ``$HOME``
    (or none) reads the same record (a workspace is recorded when either names it)."""
    paths = [record_path()]
    own = _own_home()
    if own is not None:
        other = _resolved(own / ".levain" / "consent" / RECORD_NAME)
        if other != paths[0]:
            paths.append(other)
    return paths


def _ident(path: str | Path) -> tuple[int, int] | None:
    """(device, inode) of the directory or file at ``path``, or None when it cannot be looked at. Directories are
    compared by this, never by spelling: on a case-insensitive volume two spellings name one directory."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _idents(dirs: frozenset[str] | set[str]) -> set[tuple[int, int]]:
    return {i for i in map(_ident, dirs) if i is not None}


def _inside(path: Path, dirs: set[tuple[int, int]]) -> Path | None:
    """The nearest of ``path`` and its parents that is one of the directories ``dirs`` identifies, or None."""
    if not dirs:
        return None
    return next((d for d in (path, *path.parents) if _ident(d) in dirs), None)


def _read_record(path: Path) -> frozenset[str]:
    """The real paths ``path`` records; empty when there is no such file. Raises :class:`RecordUnreadable` for one
    that exists and is not a plain readable file holding ``{"coordinator_workspaces": [absolute paths]}``, and for a
    path whose directory is not one (that is damage, not an absent record)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise RecordUnreadable(f"{path}: {exc.strerror or exc}", path) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecordUnreadable(f"{path} is not a plain file", path)
        data = os.read(fd, _RECORD_MAX + 1)
    except OSError as exc:
        raise RecordUnreadable(f"{path}: {exc.strerror or exc}", path) from exc
    finally:
        os.close(fd)
    if len(data) > _RECORD_MAX:
        raise RecordUnreadable(f"{path} is larger than a coordinator record", path)
    try:
        got = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise RecordUnreadable(f"{path} is not JSON ({exc})", path) from exc
    dirs = got.get(_RECORD_KEY) if isinstance(got, dict) else None
    if not isinstance(dirs, list) or not all(isinstance(d, str) and d.startswith("/") for d in dirs):
        raise RecordUnreadable(f"{path} holds no list of absolute paths under {_RECORD_KEY!r}", path)
    return frozenset(dirs)


def _lies_inside(path: Path, ws: Path) -> RecordUnreadable:
    return RecordUnreadable(f"{path} lies inside a coordinator's workspace ({ws}), where that coordinator could "
                            "rewrite it", path, inside=ws)


def _refuse_inside(paths: list[Path], dirs: set[tuple[int, int]]) -> None:
    """Raise :class:`RecordUnreadable` when a record file lies in one of the coordinator workspaces ``dirs``
    identifies: that coordinator could rewrite it, so it is no evidence of anything."""
    for path in paths:
        ws = _inside(path, dirs)
        if ws is not None:
            raise _lies_inside(path, ws)


def _hint_walk(start: Path) -> tuple[Path | None, OSError | None]:
    """The nearest of ``start`` and its parents whose own hint records the coordinator role, or None; with the
    ``OSError`` that stopped the walk at a directory whose hint could not be looked in (then no directory)."""
    for d in (start, *start.parents):
        try:
            if _marked(d):
                return d, None
        except OSError as exc:
            return None, exc
    return None, None


def _refuse_hinted(path: Path) -> None:
    """Raise :class:`RecordUnreadable` when an ancestor of the record file ``path`` carries the coordinator role hint,
    or has a hint that cannot be looked in: that may be a workspace no record names yet (one from before the record),
    whose coordinator could rewrite the file. Unlike the working directory's walk, an unreadable hint here fails
    closed, since the coordinator can make its own hint unreadable."""
    ws, err = _hint_walk(path.parent)
    if ws is not None:
        raise _lies_inside(path, ws)
    if err is not None:
        raise RecordUnreadable(f"{path}: a coordinator role hint above it cannot be read ({err.strerror or err})",
                               path)


def recorded() -> frozenset[str]:
    """Every coordinator workspace a record names (real paths). Raises :class:`RecordUnreadable` when a record cannot
    be read, or when a record file lies inside a workspace a record names or a role hint marks."""
    paths = _record_paths()
    out: frozenset[str] = frozenset()
    for path in paths:
        _refuse_hinted(path)
        out |= _read_record(path)
    _refuse_inside(paths, _idents(out))
    return out


def _add(path: Path, real: Path, ident: tuple[int, int] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(path.parent / (RECORD_NAME + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            import fcntl
        except ImportError:          # no fcntl (Windows): one writer at a time is the operator's to keep
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        have = _read_record(path)
        if str(real) not in have and (ident is None or ident not in _idents(have)):
            from levain.install import _atomic_write_text

            body = {_RECORD_KEY: sorted(have | {str(real)})}
            _atomic_write_text(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            os.chmod(path, 0o600)
    finally:
        os.close(lock_fd)


def record(directory: Path) -> Path:
    """Add ``directory``'s real path to every coordinator record (once; nothing is ever taken out) and return that
    real path. Called by ``levain coord agent`` before it records a setting. Refuses a directory that holds a record
    file (compared by resolved path and by identity), which the coordinator's own turn could then write, and refuses
    while any record cannot be read. Raises ``OSError`` or :class:`RecordUnreadable`."""
    real = Path(os.path.realpath(directory))
    ident = _ident(real)
    paths = _record_paths()
    for path in paths:
        if path == real or real in path.parents or (ident is not None and _inside(path, {ident})):
            raise OSError(f"{real} holds Levain's consent directory ({path.parent}); a coordinator workspace "
                          "never does")
    recorded()
    for path in paths:
        _add(path, real, ident)
    return real


def _chain(here: Path) -> list[Path]:
    """``here`` and its parents, then its real path and the real path's parents (each directory once)."""
    out: list[Path] = []
    try:
        real = Path(os.path.realpath(here))
    except OSError:
        real = here
    for d in (here, *here.parents, real, *real.parents):
        if d not in out:
            out.append(d)
    return out


def command_path(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, ...]:
    """The parsed command's path of subcommand names (``("team", "draft", "add")``), read level by level off the
    parser's own subparsers and their ``dest`` in ``args``; a level with no recorded name ends the path."""
    path: list[str] = []
    level = parser
    while True:
        subs = [a for a in level._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs or not subs[0].dest or subs[0].dest == argparse.SUPPRESS:
            return tuple(path)
        chosen = getattr(args, subs[0].dest, None)
        nxt = subs[0].choices.get(chosen) if isinstance(chosen, str) else None
        if nxt is None:
            return tuple(path)
        # an alias resolves to the first name its parser was registered under
        path.append(next(name for name, p in subs[0].choices.items() if p is nxt))
        level = nxt


def _ro(path: Path) -> sqlite3.Connection | None:
    """A read-only connection to ``path``, or None when there is no such file: never creates one."""
    if not path.is_file():
        return None
    from urllib.parse import quote

    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=2.0, isolation_level=None)


def _has_tables(conn: sqlite3.Connection, *names: str) -> bool:
    got = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return set(names) <= got


def _marked(directory: Path) -> bool:
    """Whether ``directory/.levain/config.json`` records the coordinator role. Follows no symlink: a ``.levain`` or a
    ``config.json`` that is a link is no record. Raises ``OSError`` when ``directory`` or the record cannot be read."""
    try:
        st = os.lstat(directory / ".levain")
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    try:
        fd = os.open(directory / ".levain" / "config.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return False
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        data = os.read(fd, _MARKER_MAX + 1)
    finally:
        os.close(fd)
    if len(data) > _MARKER_MAX:
        return False
    try:
        config = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return False
    return isinstance(config, dict) and config.get("role") == _ROLE


def workspace(cwd: str | None = None) -> Path | None:
    """The coordinator workspace this process runs inside: the nearest of ``cwd`` (default: this process's working
    directory) and its parents that the coordinator record names (checked first, by real path as well), else the
    nearest whose own ``.levain/config.json`` hint records the coordinator role, or None. A directory whose hint
    cannot be looked in stops the hint walk and counts as not inside; so does a working directory that cannot be
    read. Raises :class:`RecordUnreadable` as :func:`recorded` does."""
    try:
        here = Path(cwd if cwd is not None else os.getcwd())
    except OSError:
        return None
    names = recorded()
    ids = _idents(names)
    for d in _chain(here):
        if str(d) in names or (ids and _ident(d) in ids):
            return d
    # a record lying inside the hinted directory found here was already refused by recorded()'s own walk
    return _hint_walk(here)[0]


def marked(directory: Path) -> bool:
    """Whether ``directory`` itself (never an ancestor) carries the coordinator role HINT in its own
    ``.levain/config.json`` (the install render writes it). A directory or hint that cannot be read is not one. This
    tells a coordinator's rendered install from the operator's own (``levain coord agent`` refuses to reuse an
    install without it); whether a coordinator may launch somewhere is :func:`launch_refusal`'s, by the record."""
    try:
        return _marked(Path(directory))
    except OSError:
        return False


def unreadable(exc: RecordUnreadable) -> str:
    """The operator's words for a record the door cannot trust: every levain command is refused until he repairs or
    deletes that file by hand, or, for one lying inside a coordinator's workspace, moves it out (no levain command
    can, since the door refuses them all)."""
    if exc.inside is not None:
        return (f"the coordinator record {exc.path} lies inside a coordinator's workspace ({exc.inside}), where that "
                "coordinator could rewrite it, so every levain command is refused; point LEVAIN_HOME outside it, "
                "or move the consent directory out")
    where = f" {exc.path}" if exc.path is not None else " that file"
    return (f"the coordinator record cannot be trusted ({exc}), so no directory can be shown not to be a "
            f"coordinator's workspace and every levain command is refused; repair or delete{where} by hand")


def admit(directory: str | Path) -> tuple[str | None, bool]:
    """``(why a coordinator may not be launched in directory or None, whether it was just recorded)``. It may only
    where a coordinator record names it (head rulings door r2 (b) and R1: the launcher verifies confinement before
    the spawn). A workspace from before the record, which carries the role hint today, is recorded here once
    (MIGRATED: recording only ever adds a doored directory). A record that cannot be read or written refuses."""
    real = os.path.realpath(directory)
    try:
        names = recorded()
        ident = _ident(real)
        if real in names or (ident is not None and ident in _idents(names)):
            return None, False
        if not marked(Path(real)):
            return (f"{real} is not a coordinator's workspace (Levain's coordinator record does not name it); "
                    "`levain coord agent claude` or `levain coord agent codex` makes and records one"), False
        record(Path(real))
    except RecordUnreadable as exc:
        return unreadable(exc), False
    except OSError as exc:
        return f"the coordinator record could not be written ({exc})", False
    return None, True


def launch_refusal(directory: str | Path) -> str | None:
    """Why a coordinator may not be launched in ``directory`` (None: it may); see :func:`admit`."""
    return admit(directory)[0]


def refusal(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str | None:
    """Why this command is refused here (the message, with its ``levain <path>:`` prefix), or None to run it. A
    record that cannot be trusted refuses every command, the coordinator's own verbs included."""
    path = command_path(parser, args)
    words = " ".join(("levain", *path))
    try:
        ws: Path | None = workspace()
    except RecordUnreadable as exc:
        return f"{words}: {unreadable(exc)}"
    if ws is None:
        return None
    if path not in DOOR:
        return (f"{words}: `{words}` is the operator's, and this is the coordinator's workspace ({ws}); run it "
                "from your own terminal, outside it")
    if getattr(args, "by", "coordinator") != "coordinator":
        return (f"{words}: inside the coordinator's workspace ({ws}) a write goes --by coordinator --token N, "
                "never as the operator")
    return None


def readonly_store(directory: Path) -> Any:
    """A coordinator store over ``directory`` that only ever reads (sqlite ``mode=ro``), or None when there is no
    ``dispatch.db`` there or it holds no coordinator tables. For readers such as ``levain doctor``, which must not
    create a store or its tables. A ``dispatch.db`` that exists and cannot be read raises ``sqlite3.Error``: an
    unreadable store is not an absent one."""
    conn = _ro(Path(directory) / _DB_NAME)
    if conn is None:
        return None
    try:
        if not _has_tables(conn, "threads", "thread_revisions", "thread_events"):
            return None
    finally:
        conn.close()
    from levain.coordinator.store import CoordStore

    class _ReadOnly(CoordStore):
        def _connect(self) -> sqlite3.Connection:
            conn = _ro(self.path)
            if conn is None:
                raise sqlite3.OperationalError(f"{self.path} is gone")
            return conn

    return _ReadOnly(directory)
