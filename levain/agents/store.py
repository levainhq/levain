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

import re
import sqlite3
from functools import lru_cache
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from levain.autonomic.agent_tables import agent_marker, check_agent_schema, create_agent_objects, has_agent_object
from levain.autonomic.db import (DB_NAME, STORE_FORMAT_V1, STORE_FORMAT_V2, AutonomicDB, StoreFormatError,
                                 default_store_dir, store_marker)

__all__ = ["AgentStore", "AgentStoreRefused", "AgentStoreView", "open_agent_store", "open_agent_store_view",
           "STORE_V3_REASON", "valid_zone", "canonical_zone", "get_default_zone", "set_default_zone"]

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


class AgentStoreView:
    """A look at the agent tables that writes NOTHING (``register --warn``): :meth:`read` is a read-only connection
    to the store file, or, when there is no store file (or a store with no agent object yet, which the real opener
    would give empty tables), an in-memory database holding empty agent tables. No store file or directory is
    created and the database file is never written. A read of a WAL database needs SQLite's ``-shm`` index, so an
    existing store may be left with empty ``-wal``/``-shm`` sidecars (``immutable=1`` would avoid them but would
    also miss commits still in the WAL)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        if self.path is None:
            conn = sqlite3.connect(":memory:", isolation_level=None)
            create_agent_objects(conn)
        else:
            conn = sqlite3.connect(self.path.absolute().as_uri() + "?mode=ro", uri=True, isolation_level=None)
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        raise AgentStoreRefused("a read-only view of the agent store writes nothing")
        yield  # pragma: no cover


def _store_format_view(conn: sqlite3.Connection) -> bool:
    """The opener's store-format refusals, read-only: a database holding tables but no format marker is not an
    autonomic store, and a marker other than ``/1`` (which the opener migrates) or ``/2`` is not this binary's.
    True when the database holds no table at all (the opener would make it a new, empty store)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone() is None:
        return True
    marker = store_marker(conn)
    if marker is None:
        raise StoreFormatError("the database holds tables but no store format marker: it is not an autonomic "
                               "store, and is not taken over")
    if marker not in (STORE_FORMAT_V1, STORE_FORMAT_V2):
        raise StoreFormatError(f"store format {marker!r} is not {STORE_FORMAT_V2!r}")
    return False


def open_agent_store_view(store_dir: Path | str | None = None) -> AgentStoreView:
    """The read-only counterpart of :func:`open_agent_store`: the same refusals, and nothing created or changed."""
    directory = Path(store_dir).expanduser() if store_dir is not None else default_store_dir()
    path = directory / DB_NAME
    if not path.is_file():
        return AgentStoreView(None)
    if _store_format_ro(path) == STORE_V3:
        raise AgentStoreRefused(STORE_V3_REASON)
    view = AgentStoreView(path)
    with view.read() as conn:
        if _store_format_view(conn):
            return AgentStoreView(None)
        marker = agent_marker(conn)
        if marker is None and not has_agent_object(conn):
            return AgentStoreView(None)
        reason = check_agent_schema(conn)
    if reason is not None:
        raise AgentStoreRefused(_refusal(reason, marker))
    return view


ZONE_KEY = "default_zone"
_ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}")


@lru_cache(maxsize=1)
def _zones() -> dict[str, str]:
    from zoneinfo import available_timezones
    return {z.casefold(): z for z in available_timezones()}


def canonical_zone(zone: object) -> str | None:
    """The tz database's own spelling of ``zone`` (matched case-insensitively), or None when it is not an IANA zone
    this host knows. A case-insensitive filesystem loads ``america/new_york`` too; storing that spelling would fail on
    a case-sensitive host, so what is stored is the canonical one."""
    if not isinstance(zone, str) or not _ZONE_NAME.fullmatch(zone):
        return None
    name = _zones().get(zone.casefold())
    if name is None:
        return None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return name


def valid_zone(zone: object) -> bool:
    """True when ``zone`` is an IANA zone name this host's tz database knows (see :func:`canonical_zone`)."""
    return canonical_zone(zone) is not None


def get_default_zone(store: AgentStore) -> str | None:
    """The install's one default zone (A0 r6 §3.6: the ``agent_settings`` row ``default_zone``), or None if unset.
    A stored value that is not a zone this host knows is refused (ValueError naming the verb that sets it), so it is
    never written into a spec."""
    with store.read() as conn:
        row = conn.execute("SELECT value FROM agent_settings WHERE key = ?", (ZONE_KEY,)).fetchone()
    if row is None or row[0] is None:
        return None
    if canonical_zone(row[0]) != row[0]:
        raise ValueError(f"the stored default zone {str(row[0])[:60]!r} is not an IANA zone name this host knows: set "
                         f"it again with `levain agents zone <IANA>`")
    return row[0]


def set_default_zone(store: AgentStore, zone: str) -> str:
    """Set the default zone to the canonical spelling of ``zone`` and return it; raise ValueError naming it when it
    is not an IANA zone."""
    name = canonical_zone(zone)
    if name is None:
        raise ValueError(f"{str(zone)[:60]!r} is not an IANA zone name this host knows (e.g. America/New_York)")
    with store.write() as conn:
        conn.execute("INSERT INTO agent_settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (ZONE_KEY, name))
    return name
