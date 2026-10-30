"""levain.agents.store — the one opener and the one writer of the agent tables (A0 r7 §3.5, §3.7).

``open_agent_store`` refuses what an agent must not run on and hands back an :class:`AgentStore` whose every
connection has ``PRAGMA recursive_triggers = ON``. A REPLACE over a spec's or a bar's declared key is refused by
the ``_no_replace`` triggers on any connection; the pragma is what makes a REPLACE that collides only on the implicit
``rowid`` fire the ``_keep`` triggers (A0 r7 §4.2). The schema is checked once, at open (A0 r7 §6.12). The refusals:

- a ``levain-autonomic/3`` store: agents run on ``/2`` until the agent-proof slice (R15-5) lands; the store is read
  through a read-only connection and nothing is created or changed;
- a store whose agent schema is not the one this binary writes (:func:`check_agent_schema`): the operator is told
  the way out. There is no repair verb in this build.

Later slices add their read and write code here.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from levain.autonomic.agent_tables import agent_marker, check_agent_schema
from levain.autonomic.db import DB_NAME, AutonomicDB, default_store_dir, store_marker

__all__ = ["AgentStore", "AgentStoreRefused", "open_agent_store", "STORE_V3_REASON"]

STORE_V3 = "levain-autonomic/3"
STORE_V3_REASON = "agents need the /3 agent-proof slice (R15-5)"


class AgentStoreRefused(RuntimeError):
    """``levain agents`` is refused on this store; the message names why and, for a schema mismatch, the way out."""


class _AgentDB(AutonomicDB):
    """:class:`AutonomicDB` whose connections run with ``recursive_triggers = ON``, set before any transaction."""

    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        try:
            conn.execute("PRAGMA recursive_triggers = ON")
        except BaseException:
            conn.close()
            raise
        return conn


def _refusal(reason: str, marker: str | None) -> str:
    head = (f"{reason}: the agent tables in this store are not the ones this levain writes; restore `{DB_NAME}` "
            f"from a backup made by this version")
    if marker is None:    # no marker names a writer, so a backup is the only way out
        return head + "; no agent_schema marker names the levain that wrote them"
    return head + f", or run the levain that wrote marker {marker}"


class AgentStore:
    """An opened, checked agent store. :meth:`write` is one ``BEGIN IMMEDIATE`` transaction and :meth:`read` a
    consistent snapshot, both of a store whose ``/2`` format marker is this binary's."""

    def __init__(self, db: _AgentDB) -> None:
        self._db = db
        self.directory = db.directory
        self.path = db.path

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._db.write() as conn:
            yield conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._db.read() as conn:
            yield conn


def _store_format_ro(path: Path) -> str | None:
    """The store's format marker through a read-only connection (no PRAGMA set, nothing created); ``None`` when
    there is no database file or it carries no marker."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        return store_marker(conn)
    finally:
        conn.close()


def open_agent_store(store_dir: Path | str | None = None) -> AgentStore:
    """Open the agent store of ``store_dir`` (default: the autonomic store of this levain home), or raise
    :class:`AgentStoreRefused`. A ``/2`` store with no agent object gains the tables in the opener's own transaction
    (``AutonomicDB._ensure_schema``); a store with agent objects is only checked, never repaired."""
    directory = Path(store_dir).expanduser() if store_dir is not None else default_store_dir()
    if _store_format_ro(directory / DB_NAME) == STORE_V3:
        raise AgentStoreRefused(STORE_V3_REASON)
    db = _AgentDB(directory)
    with db.read() as conn:
        reason = check_agent_schema(conn)
        marker = agent_marker(conn)
    if reason is not None:
        raise AgentStoreRefused(_refusal(reason, marker))
    return AgentStore(db)
