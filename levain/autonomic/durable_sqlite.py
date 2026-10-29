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

import os
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
#: SQLite's own text for SQLITE_BUSY; SQLITE_LOCKED says "database table is locked" and is not retried.
_BUSY_TEXT = "database is locked"


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    """SQLITE_BUSY by error code where Python gives one (3.11+, an extended busy code included), else by SQLite's text."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF == _SQLITE_BUSY
    return str(exc) == _BUSY_TEXT


def _library_version() -> tuple[int, ...]:
    return tuple(int(p) for p in sqlite3.sqlite_version.split("."))


def rollback(conn: sqlite3.Connection) -> None:
    """Roll back, never masking the error that caused it (a failed rollback is also a rolled-back
    transaction once the connection closes)."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _set_wal(conn: sqlite3.Connection, bound_s: float | None = None) -> None:
    """``PRAGMA journal_mode = WAL``, retried while SQLite answers busy, for about :data:`BUSY_WAIT_S`.

    Two processes making one new store, each switching it, saw this PRAGMA answer ``SQLITE_BUSY`` (``database is
    locked``) with no wait, the busy handler not called (the queue broker's first two GATEs, lane-limitsrace). Any
    other error, and busy past the bound, propagates. SQLite can also answer a mode other than ``wal`` without
    raising (``:memory:``, a VFS without WAL); that raises too, so a store never runs un-journaled in silence.

    The busy handler is off while it tries, so every try answers at once and the pauses here are the only wait:
    their sum is the bound (a sum of pauses, not wall time), kept without a clock (``time`` is not on the package's import allowlist,
    ``tests/test_autonomic_leaf.py``). The caller sets the busy handler after.

    Each pause is its step (doubling from 1 ms to 50 ms) scaled by a random factor in [0.5, 1.5). Two openers of one
    new file each block the other's switch, and with one fixed schedule they woke together and collided again, round
    after round (lane-race39 measured six rounds in one trial; on CI both calls of one trial were refused). The random
    factor parts them, so one switches and the other then finds the file in WAL. ``bound_s`` replaces
    :data:`BUSY_WAIT_S` as the bound for a caller with less time (0: one try)."""
    bound = BUSY_WAIT_S if bound_s is None else bound_s      # read at call time, never frozen at definition
    conn.execute("PRAGMA busy_timeout = 0")
    waited, pause = 0.0, 0.001
    while True:
        try:
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or waited + 1.5 * pause > bound:
                raise
        else:
            mode = row[0] if row else None
            if not (isinstance(mode, str) and mode.lower() == "wal"):
                raise sqlite3.OperationalError(f"WAL journaling was not applied: SQLite answered {mode!r}")
            return
        jittered = pause * (0.5 + int.from_bytes(os.urandom(2), "big") / 65536)
        threading.Event().wait(jittered)
        waited += jittered
        pause = min(pause * 2, 0.05)


def _until_not_busy(conn: sqlite3.Connection, step: Callable[[], object]) -> object:
    """``step()``, retried while it raises SQLITE_BUSY, for one wall-clock bound covering the tries and the
    pauses between them: the connection's own ``busy_timeout``, read before the retry, never more. For a statement SQLite may answer busy without calling the busy handler: the
    sqlite.org WAL page says a connection that opens and queries while the last other one is closing "might get an
    SQLITE_BUSY error" (wal.html, "Sometimes Queries Return SQLITE_BUSY In WAL Mode"), and the busy-handler page
    says the handler is not guaranteed to be invoked (c3ref/busy_handler.html). ``step`` must leave no transaction
    open when it fails.

    The connection's busy handler is off while it tries (every try answers at once, so the clock bounds the call and
    a held lock is polled by the pauses), and put back after. A connection whose ``busy_timeout`` is already 0 was
    set fail-fast on purpose: it gets one try, no retry. The clock is ``os.times().elapsed`` (``time`` is not on the
    package's import allowlist, ``tests/test_autonomic_leaf.py``)."""
    restore = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    if not restore:
        return step()
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        deadline = os.times().elapsed + restore / 1000
        pause = 0.001
        while True:
            try:
                return step()
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or os.times().elapsed + pause > deadline:
                    raise
            threading.Event().wait(pause)
            pause = min(pause * 2, 0.05)
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(restore)}")


def open_durable(path: Path | str, *, min_sqlite: tuple[int, ...] | None = None,
                 needs: str = "this store", busy_s: float | None = None) -> sqlite3.Connection:
    """A connection to ``path`` with the durability PRAGMAs applied, in autocommit mode so the caller
    owns every ``BEGIN``. Closed again if any PRAGMA fails. ``min_sqlite`` refuses an older library,
    naming what ``needs`` it. ``busy_s`` (default :data:`BUSY_WAIT_S`) bounds every wait of the open itself (the
    WAL switch, the first read) and is the connection's busy timeout after: a caller on a deadline passes what is
    left of it, and a lock held past it raises SQLite's own busy error."""
    if min_sqlite is not None and _library_version() < min_sqlite:
        raise RuntimeError(f"SQLite {sqlite3.sqlite_version} is older than "
                           f"{'.'.join(map(str, min_sqlite))}, which {needs} needs")
    wait = BUSY_WAIT_S if busy_s is None else max(0.0, min(float(busy_s), BUSY_WAIT_S))
    conn = sqlite3.connect(path, timeout=wait, isolation_level=None)
    try:
        began = os.times().elapsed
        _set_wal(conn, None if busy_s is None else wait)  # sidecars -wal/-shm stay beside the database
        # One budget for the whole open: the first read waits only what the WAL switch left of it, never a second ``wait``.
        left = wait if busy_s is None else max(0.0, wait - (os.times().elapsed - began))
        conn.execute(f"PRAGMA busy_timeout = {int(left * 1000)}")
        # A first read, retried, so the statements after it meet a connection that has already read the database.
        # [judged by lane-limitsrace, 10-25, against wal.html: the last connection to close takes an exclusive lock
        # to delete the WAL and shared memory; not reproduced here, so whether this read is what stops that cleanup
        # under this connection is unmeasured.]
        _until_not_busy(conn, lambda: conn.execute("SELECT count(*) FROM sqlite_master").fetchone())
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
        _until_not_busy(conn, lambda: conn.execute("BEGIN IMMEDIATE"))
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
        _until_not_busy(conn, lambda: conn.execute("BEGIN"))
        try:
            yield conn
        except BaseException:
            rollback(conn)
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
