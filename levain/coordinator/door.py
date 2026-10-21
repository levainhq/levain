"""The coordinator's door: what a session Levain launched may run of the ``levain`` CLI.

np-6843ca07 (a), signed, and the head's 1021-18 ruling: inside a session Levain launched (Claude Code's own session id
names a recorded launch), the CLI is DEFAULT-DENY. A command whose path is not in :data:`DOOR` is refused, in every
tree, and every write it does run goes ``--by coordinator`` under its lease token (an operator write is unfenced, so a
session that lost its lease could otherwise still write). A command not listed here is refused there until it is
listed: the check reads the command path off the parser itself, so a new tree needs no second list to be covered.

:func:`refusal` is called once, from ``levain.cli.main``, after argument parsing. The hooks and Levain's MCP servers
are other entry points and never reach it. The launch lookup only ever READS the store (sqlite ``mode=ro``): a
command run where no coordinator store exists creates nothing.

This is a guard on an honest coordinator, not a boundary against a hostile process of the same user: such a process
can drop the session variable, point ``LEVAIN_HOME`` elsewhere or import the package directly.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
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

    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=30.0, isolation_level=None)


def _has_tables(conn: sqlite3.Connection, *names: str) -> bool:
    got = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return set(names) <= got


def _launch_in(directory: Path, sid: str) -> Any:
    try:
        conn = _ro(directory / _DB_NAME)
    except sqlite3.Error:
        return None
    if conn is None:
        return None
    try:
        if not _has_tables(conn, "launches", "launch_events"):
            return None
        from levain.coordinator.lease import by_harness_session

        conn.execute("BEGIN")
        return by_harness_session(conn, sid)
    except sqlite3.Error:
        return None          # an unreadable store cannot say this session is a launch (the command's own open reports it)
    finally:
        conn.close()


def launched(args: argparse.Namespace) -> Any:
    """The launch this process's Claude Code session is, or None. Reads the command's ``--queue-dir`` store and the
    default one, both: a ``--queue-dir`` naming an empty directory is no way round the door for a default store."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    from levain.dispatch.jobs import default_dispatch_dir

    dirs: list[Path] = []
    qd = getattr(args, "queue_dir", None)
    if isinstance(qd, (str, Path)):
        dirs.append(Path(qd).expanduser())
    dirs.append(default_dispatch_dir())
    for d in dict.fromkeys(dirs):
        ln = _launch_in(d, sid)
        if ln is not None:
            return ln
    return None


def refusal(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str | None:
    """Why this command is refused here (the message, with its ``levain <path>:`` prefix), or None to run it."""
    ln = launched(args)
    if ln is None:
        return None
    path = command_path(parser, args)
    words = " ".join(("levain", *path))
    if path not in DOOR:
        return (f"{words}: `{words}` is the operator's, and this is a session Levain launched (launch "
                f"{ln.launch_id}); run it from your own terminal")
    if getattr(args, "by", "coordinator") != "coordinator":
        return (f"{words}: a session Levain launched (launch {ln.launch_id}) writes --by coordinator --token N, "
                "never as the operator")
    return None


def readonly_store(directory: Path) -> Any:
    """A coordinator store over ``directory`` that only ever reads (sqlite ``mode=ro``), or None when there is no
    ``dispatch.db`` there or it holds no coordinator tables. For readers such as ``levain doctor``, which must not
    create a store or its tables."""
    try:
        conn = _ro(Path(directory) / _DB_NAME)
        if conn is None:
            return None
        try:
            if not _has_tables(conn, "threads", "thread_revisions", "thread_events"):
                return None
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    from levain.coordinator.store import CoordStore

    class _ReadOnly(CoordStore):
        def _connect(self) -> sqlite3.Connection:
            conn = _ro(self.path)
            if conn is None:
                raise sqlite3.OperationalError(f"{self.path} is gone")
            return conn

    return _ReadOnly(directory)
