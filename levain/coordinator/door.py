"""The coordinator's door: what may run of the ``levain`` CLI inside the coordinator's workspace.

np-6843ca07 (a), signed, and the head's 1021-18 rulings: a command run from inside the coordinator's workspace is
DEFAULT-DENY. A command whose path is not in :data:`DOOR` is refused, in every tree, and every write it does run goes
``--by coordinator`` under its lease token (an operator write is unfenced, so a session that lost its lease could
otherwise still write). A command not listed here is refused there until it is listed: the check reads the command
path off the parser itself, so a new tree needs no second list to be covered.

Provenance is WHERE the command runs, never a lookup: a directory whose ``.levain/config.json`` records
``"role": "coordinator"`` is the coordinator's workspace, and the process's working directory, walked up to the
filesystem root, finds that record or does not. No database is read, so no store state (locked, corrupt, another store, a session not yet recorded) can open
the door, and both harnesses' tool subprocesses start in the directory the session was launched in (measured, for
``claude -p`` and a Codex thread). Consequences, accepted: the operator running ``levain`` from
inside that workspace is doored too (it is Levain's directory, not his), and an agent that changes directory out of it
leaves the door (the door is fencing for an honest coordinator; the lease token still fences every write).

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
    ("drops", "list"), ("drops", "show"), ("drops", "split"),
    ("thread", "list"), ("thread", "show"), ("thread", "state"), ("thread", "merge"), ("thread", "detach"),
    ("thread", "add"), ("thread", "set"),
    ("team", "draft", "add"), ("team", "draft", "list"), ("team", "draft", "show"),
    ("team", "status"), ("team", "verify"), ("team", "export"), ("team", "doctor"),
})

_DB_NAME = "dispatch.db"
_ROLE = "coordinator"
_MARKER_MAX = 64 * 1024


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
    directory) and its ancestors whose ``.levain/config.json`` records the coordinator role, or None. A directory that
    cannot be looked in stops the walk and counts as not inside; so does a working directory that cannot be read."""
    try:
        here = Path(cwd if cwd is not None else os.getcwd())
    except OSError:
        return None
    for d in (here, *here.parents):
        try:
            if _marked(d):
                return d
        except OSError:
            return None
    return None


def refusal(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str | None:
    """Why this command is refused here (the message, with its ``levain <path>:`` prefix), or None to run it."""
    ws = workspace()
    if ws is None:
        return None
    path = command_path(parser, args)
    words = " ".join(("levain", *path))
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
