"""levain.durable_sqlite — the one way a levain store opens SQLite and runs a transaction.

One definition so every store carries the same durability (K2b design §0.1): WAL journaling,
``synchronous=FULL`` (every commit is on disk before it returns), ``fullfsync`` and
``checkpoint_fullfsync`` (macOS flushes the drive's cache too; a no-op elsewhere), and a
``busy_timeout`` that waits for the write lock. Every write is one ``BEGIN IMMEDIATE``, so writers
are serialized by SQLite itself.

``RETURNING`` (SQLite 3.35) is required (K2b design §4.1.2), so an older library is refused at open
rather than failing mid-transaction.

Stdlib only.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["MIN_SQLITE", "open_durable", "read_transaction", "rollback", "write_transaction"]

MIN_SQLITE = (3, 35, 0)


def _library_version() -> tuple[int, ...]:
    return tuple(int(p) for p in sqlite3.sqlite_version.split("."))


def rollback(conn: sqlite3.Connection) -> None:
    """Roll back, never masking the error that caused it (a failed rollback is also a rolled-back
    transaction once the connection closes)."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def open_durable(path: Path | str) -> sqlite3.Connection:
    """A connection to ``path`` with the durability PRAGMAs applied, in autocommit mode so the caller
    owns every ``BEGIN``. Closed again if any PRAGMA fails."""
    if _library_version() < MIN_SQLITE:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is older than "
                           f"{'.'.join(map(str, MIN_SQLITE))} (RETURNING is required)")
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")       # sidecars -wal/-shm stay beside the database
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
    """A consistent read snapshot on a fresh connection."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")
    finally:
        conn.close()
