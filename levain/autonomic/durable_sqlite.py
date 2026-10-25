"""levain.autonomic.durable_sqlite — the one way a levain store opens SQLite and runs a transaction.

One definition so every store carries the same durability (K2b design §0.1): WAL journaling,
``synchronous=FULL`` (every commit is on disk before it returns), ``fullfsync`` and
``checkpoint_fullfsync`` (macOS flushes the drive's cache too; a no-op elsewhere), and a
``busy_timeout`` that waits for the write lock. Every write is one ``BEGIN IMMEDIATE``, so writers
are serialized by SQLite itself.

A store that uses ``RETURNING`` (the cockpit journal, K2b design §4.1.2) passes ``min_sqlite=MIN_SQLITE``,
so an older library is refused at its open rather than failing mid-transaction. The autonomic store
uses no ``RETURNING`` and passes no floor: it keeps running on whatever SQLite it ran on before.

Stdlib only.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["MIN_SQLITE", "open_durable", "read_transaction", "rollback", "write_transaction"]

MIN_SQLITE = (3, 35, 0)

#: How long a connection waits for a lock another one holds: the busy handler, and the WAL switch's own retry.
BUSY_WAIT_S = 30.0
_SQLITE_BUSY = 5


def _library_version() -> tuple[int, ...]:
    return tuple(int(p) for p in sqlite3.sqlite_version.split("."))


def rollback(conn: sqlite3.Connection) -> None:
    """Roll back, never masking the error that caused it (a failed rollback is also a rolled-back
    transaction once the connection closes)."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _set_wal(conn: sqlite3.Connection) -> None:
    """``PRAGMA journal_mode = WAL``, retried while SQLite answers busy, for at most :data:`BUSY_WAIT_S`.

    Two processes making one new store, each switching it, saw this PRAGMA answer ``SQLITE_BUSY`` (``database is
    locked``) with no wait, the busy handler not called (the queue broker's first two GATEs, lane-limitsrace). Any
    other error, and busy past the bound, propagates.

    The busy handler is off while it tries, so every try answers at once and the pauses here are the only wait:
    their sum is the bound, kept without a clock (``time`` is not on the package's import allowlist,
    ``tests/test_autonomic_leaf.py``). The caller sets the busy handler after."""
    conn.execute("PRAGMA busy_timeout = 0")
    waited, pause = 0.0, 0.001
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            busy = getattr(exc, "sqlite_errorcode", 0) & 0xFF == _SQLITE_BUSY   # an extended busy code included
            if not busy or waited + pause > BUSY_WAIT_S:
                raise
        threading.Event().wait(pause)
        waited += pause
        pause = min(pause * 2, 0.05)


def open_durable(path: Path | str, *, min_sqlite: tuple[int, ...] | None = None,
                 needs: str = "this store") -> sqlite3.Connection:
    """A connection to ``path`` with the durability PRAGMAs applied, in autocommit mode so the caller
    owns every ``BEGIN``. Closed again if any PRAGMA fails. ``min_sqlite`` refuses an older library,
    naming what ``needs`` it."""
    if min_sqlite is not None and _library_version() < min_sqlite:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is older than "
                           f"{'.'.join(map(str, min_sqlite))}, which {needs} needs")
    conn = sqlite3.connect(path, timeout=BUSY_WAIT_S, isolation_level=None)
    try:
        _set_wal(conn)                                  # sidecars -wal/-shm stay beside the database
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_WAIT_S * 1000)}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA fullfsync = ON")
        conn.execute("PRAGMA checkpoint_fullfsync = ON")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def write_transaction(connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    """One write transaction (``BEGIN IMMEDIATE``) on a fresh connection: committed when the block
    exits, rolled back if it raises, the connection closed either way."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            rollback(conn)
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def read_transaction(connect: Callable[[], sqlite3.Connection]) -> Iterator[sqlite3.Connection]:
    """A consistent read snapshot on a fresh connection, ended by COMMIT, or by a rollback when the
    block raised (so the block's error is the one that propagates)."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            rollback(conn)
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
