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

Durability: :mod:`levain.autonomic.durable_sqlite` (every commit on disk before it returns; every write one
``BEGIN IMMEDIATE``, serialized by SQLite itself).

The store carries a format marker, read inside every transaction; a database with another marker, or
tables and no marker, is refused rather than read or taken over, and a store that disappears under a
running object is refused rather than silently started empty. The format is ``levain-autonomic/2``: a
binding's effect runs below CONFIRM only under a signed graduation policy (the gate's clamp). The first open by
this binary migrates a ``levain-autonomic/1`` store in place (:func:`migrate_to_v2`, in the opening
``BEGIN IMMEDIATE``).

**Upgrade order: stop every process of an older levain that has the store open before this binary opens it.** A
``/1`` binary that checks the marker inside every transaction (``_check_format``, after 0.11.24) refuses the store at
its next read or write once it is migrated. Levain 0.11.24 and earlier check it only when an object first opens the
store, so such a process left running would keep reading and writing the ``/2`` store unclamped; no command of
those releases opens it (nothing outside ``levain.autonomic`` builds a store object), which is why the migration
takes no lock against them.

The directory is created ``0700``; an existing one keeps its mode. A registry kept in the earlier JSON file format
(this package's or the vagus package's) enters only through
:meth:`levain.autonomic.binding.BindingStore.migrate_json`, a one-way import.

Stdlib only.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from levain.autonomic.agent_tables import agent_marker, ddl_agents, has_agent_object
from levain.autonomic.durable_sqlite import open_durable, read_transaction, write_transaction
from levain.autonomic.durable_sqlite import rollback as _rollback

__all__ = ["AutonomicDB", "StoreFormatError", "STORE_FORMAT", "STORE_FORMAT_V1", "STORE_FORMAT_V2", "DB_NAME",
           "default_store_dir", "migrate_to_v2", "store_marker"]

STORE_FORMAT_V1 = "levain-autonomic/1"
# the format of a store whose bindings fire below CONFIRM only under a signed graduation policy: an older binary
# that opens it refuses it at its own format check rather than fire below CONFIRM unclamped
STORE_FORMAT_V2 = "levain-autonomic/2"
STORE_FORMAT = STORE_FORMAT_V2
DB_NAME = "autonomic.db"

# The graduation audit table: one row per step the engine took under a policy's authority. An audit record: it
# authorizes nothing.
_GRADUATION_STEPS = (
    "CREATE TABLE IF NOT EXISTS graduation_steps (step INTEGER PRIMARY KEY, at TEXT NOT NULL, "
    "old_id TEXT NOT NULL, new_id TEXT NOT NULL, family TEXT, from_posture TEXT NOT NULL, "
    "to_posture TEXT NOT NULL, clean_count INTEGER NOT NULL, fire_count INTEGER NOT NULL, "
    "policy_id TEXT NOT NULL, incarnation TEXT NOT NULL, authority_head TEXT NOT NULL)"
)

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


class StoreFormatError(RuntimeError):
    """The database is not a store of this format (another marker, or not a store at all)."""


def store_marker(conn: sqlite3.Connection) -> str | None:
    """The store's format marker, ``None`` when there is none (no ``meta`` table in the schema, or no format row).
    Whether ``meta`` exists is asked of ``sqlite_master``, never read from an error's wording; any error of the read
    itself is raised as it is, never read as "no marker"."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
    return None if row is None else row[0]


def _check_format(conn: sqlite3.Connection) -> None:
    # inside the caller's transaction, after it began: a store migrated by another connection since this object
    # opened it is refused here. A write holds the write lock from its BEGIN IMMEDIATE, so no migration commits
    # before the block's own writes; a read's snapshot starts at this, its first read, so the block reads the
    # state this check read
    marker = store_marker(conn)
    if marker is None:
        raise StoreFormatError("the store has no format marker")
    if marker != STORE_FORMAT:
        raise StoreFormatError(f"store format {marker!r} is not {STORE_FORMAT!r}")


def migrate_to_v2(conn: sqlite3.Connection) -> bool:
    """Migrate a ``/1`` store to ``/2`` in place, inside the write transaction ``conn`` holds: add the
    ``graduation_steps`` table and set the marker. ``True`` if this call changed the store (it migrated a ``/1``
    store, or added the missing ``graduation_steps`` table to a ``/2`` one), ``False`` for a ``/2`` store it left
    as it was. A store with no marker, or another one, raises :class:`StoreFormatError` and is left as it was. No
    other table changes: a binding is read as it was written, and the rung it fires at is the gate's to decide, not
    this function's.

    The caller owns the transaction, so the table and the marker commit
    together or not at all."""
    if not conn.in_transaction:
        raise RuntimeError("migrate_to_v2 runs inside its caller's write transaction")
    marker = store_marker(conn)
    if marker is None:
        raise StoreFormatError("the database has no store format marker: it is not migrated")
    if marker not in (STORE_FORMAT_V1, STORE_FORMAT_V2):
        raise StoreFormatError(f"store format {marker!r} is not {STORE_FORMAT_V1!r}: it is not migrated")
    had_steps = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'graduation_steps'"
                             ).fetchone() is not None
    conn.execute(_GRADUATION_STEPS)
    if marker == STORE_FORMAT_V2:
        return not had_steps
    conn.execute("UPDATE meta SET value = ? WHERE key = 'format'", (STORE_FORMAT_V2,))
    return True


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
        conn = open_durable(self.path)
        try:
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
            # a new store gets its audit table here; a /1 store is migrated (marker and table, in this
            # transaction); any other marker raises and the transaction rolls back, leaving the store as it was
            migrate_to_v2(conn)
            # the agent tables (A0 §3.5): DDL only on a store holding no agent marker and no agent object; a store
            # with either gets none from the opener, and `levain agents` is what refuses it (check_agent_schema)
            if agent_marker(conn) is None and not has_agent_object(conn):
                ddl_agents(conn)
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction (``BEGIN IMMEDIATE``): committed when the block exits, rolled back if it
        raises. Writers are serialized by SQLite; a reader always sees a committed state. A store whose format
        marker is no longer this binary's is refused before the block runs."""
        with write_transaction(self._connect) as conn:
            _check_format(conn)
            yield conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A consistent read snapshot, of a store whose format marker is this binary's (else refused)."""
        with read_transaction(self._connect) as conn:
            _check_format(conn)
            yield conn

    def meta(self, key: str) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
