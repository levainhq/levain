"""levain.autonomic.db — the one durable store of the autonomic engine.

The binding registry and the run journal live in ONE SQLite database, ``autonomic.db``, inside a store
DIRECTORY. One database because a decision about standing authority is never allowed to live in two
places: a pause and the fence that stops admitted runs, an admission and a one-shot's claim, a decision
and the cancel of its run are each ONE transaction, so no crash, fault or race can leave two stores
disagreeing (SQLite: "transactions appear to be atomic even if the transaction is interrupted by an
operating system crash or power failure").

A directory because the store's SQLite sidecars (``-wal``, ``-shm``) and the journal's effect leases
live next to the database, and the confinement floor protects the whole directory as one crown jewel
(``levain.firing.confinement.AUTONOMIC_STORE_DIR``, at :func:`default_store_dir`): an entity's hands can
neither read nor write any of it, and a shell is not refused on its account (a directory jewel has no
sidecars outside itself to plant). A store kept anywhere else is protected by naming its directory in
the entity's ``deny_subtrees``.

Durability: WAL journaling, ``synchronous=FULL`` (every commit is on disk before it returns), and
``fullfsync`` (macOS flushes the drive's cache too; a no-op elsewhere). Every write runs inside
``BEGIN IMMEDIATE``, so writers are serialized by SQLite itself; ``busy_timeout`` waits for the lock.

The store carries a format marker; a database with another marker, or tables and no marker, is refused
rather than read or taken over, and a store that disappears under a running object is refused rather
than silently started empty. The directory is created ``0700``; an existing one keeps its mode. A
registry kept in the earlier JSON file format (this package's or the vagus package's) enters only
through :meth:`levain.autonomic.binding.BindingStore.migrate_json`, a one-way import.

Stdlib only.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["AutonomicDB", "StoreFormatError", "STORE_FORMAT", "default_store_dir"]

STORE_FORMAT = "levain-autonomic/1"
DB_NAME = "autonomic.db"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    # the binding registry: one row per grant, the record as written by BindingStore (JSON), in order
    "CREATE TABLE IF NOT EXISTS bindings (binding_id TEXT PRIMARY KEY, record TEXT NOT NULL, "
    "seq INTEGER NOT NULL)",
    # the run journal
    "CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, "
    "generation INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS fences (binding_id TEXT PRIMARY KEY, generation INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cancels (run_id TEXT PRIMARY KEY, reason TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS holds (hold_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
    "effect_id TEXT NOT NULL, binding_id TEXT NOT NULL, digest TEXT NOT NULL, at TEXT, "
    "pending TEXT, pending_id TEXT, chain TEXT, chained INTEGER NOT NULL DEFAULT 0, "
    "decided INTEGER, decided_by TEXT, decided_posture TEXT, seq INTEGER NOT NULL, "
    "signer TEXT, signature TEXT, challenge TEXT, fence TEXT)",
    # one hold per pending id: a resolve finds a decision by its pending id, so a second hold sharing one
    # could never be decided and would hold its binding. A run's pending seals its hold id, so two holds
    # do not share one; were they to, the second insert fails instead
    "CREATE UNIQUE INDEX IF NOT EXISTS holds_pending_id ON holds (pending_id)",
    "CREATE TABLE IF NOT EXISTS effects (run_id TEXT NOT NULL, effect_id TEXT NOT NULL, "
    "digest TEXT NOT NULL, pid INTEGER, state TEXT NOT NULL CHECK (state IN ('intent','done','unknown')), "
    "result TEXT, receipt_id TEXT, PRIMARY KEY (run_id, effect_id))",
)


# Columns a store made before they existed lacks; added in place (a hold without them reads as unsigned,
# which is what such a hold is). (table, column, type)
_ADDED_COLUMNS = (
    ("holds", "signer", "TEXT"), ("holds", "signature", "TEXT"), ("holds", "challenge", "TEXT"),
    ("holds", "fence", "TEXT"),
)


def _rollback(conn: sqlite3.Connection) -> None:
    """Roll back, never masking the error that caused it (a failed rollback is also a rolled-back
    transaction once the connection closes)."""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class StoreFormatError(RuntimeError):
    """The database is not a store of this format (another marker, or not a store at all)."""


def default_store_dir() -> Path:
    """``<levain home>/autonomic``: ``$LEVAIN_HOME/autonomic`` if set, else ``~/.levain/autonomic``. Made
    absolute here, as the confinement floor's deny rule for it is, so a relative ``$LEVAIN_HOME`` names
    one directory for the life of the store, whatever the working directory does later."""
    home = os.environ.get("LEVAIN_HOME")
    return ((Path(home).expanduser() if home else Path.home() / ".levain") / "autonomic").resolve()


class AutonomicDB:
    """The store directory and its database. Every operation opens its own connection (cheap, and safe
    across threads and processes); :meth:`write` is one ``BEGIN IMMEDIATE`` transaction, :meth:`read`
    a consistent snapshot."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.path = self.directory / DB_NAME
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        if self._ready:
            # the store existed when this object first opened it: if it is gone now, fail rather than
            # start an empty one (an empty store has no fences, holds or decisions)
            if not self.path.exists():
                raise StoreFormatError(f"store {self.path} has disappeared")
        elif not self.directory.exists():
            # another opener may create it between the two calls
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)   # the umask can only narrow it
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")       # sidecars -wal/-shm stay in the directory
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA fullfsync = ON")
            conn.execute("PRAGMA checkpoint_fullfsync = ON")
            if not self._ready:
                self._ensure_schema(conn)
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            marked = "meta" in tables and conn.execute(
                "SELECT 1 FROM meta WHERE key = 'format'").fetchone() is not None
            if tables and not marked:
                raise StoreFormatError("the database holds tables but no store format marker: it is not "
                                       "an autonomic store, and is not taken over")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            for table, column, kind in _ADDED_COLUMNS:
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            # this store's identity, named in every confirm challenge so a signature is good in one store
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('store_id', ?)",
                         ("store-" + os.urandom(16).hex(),))
            row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
            if row is None:
                conn.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (STORE_FORMAT,))
            elif row[0] != STORE_FORMAT:
                raise StoreFormatError(f"store format {row[0]!r} is not {STORE_FORMAT!r}")
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): committed when the block exits, rolled back if it
        raises. Writers are serialized by SQLite; a reader always sees a committed state."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                _rollback(conn)
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def meta(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
