"""levain.autonomic.agent_tables — the agent DDL, its triggers and the agent schema marker (A0 r7 §3.5, §3.7, §4.2).

A leaf: stdlib ``sqlite3`` only. It holds the eight agent tables, one index and the ``BEFORE`` triggers of the
``autonomic.db`` store, as ``AGENT_OBJECT_DDL``, and the checks over them:

- :func:`create_agent_objects` runs the DDL and touches no other table (so a reference store needs no ``meta``);
- :func:`ddl_agents` is the host-store step: the objects, then ``meta.agent_schema``, inside the caller's transaction;
- :func:`reference_master` builds the reference ``sqlite_master`` rows on a fresh ``:memory:`` connection;
- :func:`check_agent_schema` compares a store's agent rows to the reference, byte for byte, and writes nothing.

An edit to the DDL text is a new agent schema marker with a migration: the compare is byte-exact against this
binary's constants (the golden hash of ``AGENT_OBJECT_DDL`` is pinned in ``tests/test_agent_tables.py``).
"""
from __future__ import annotations

import sqlite3

__all__ = ["AGENT_SCHEMA", "AGENT_TABLES", "AGENT_TRIGGERS", "AGENT_OBJECT_DDL", "create_agent_objects",
           "ddl_agents", "agent_master", "reference_master", "has_agent_object", "agent_marker",
           "check_agent_schema"]

# /1 named A0 r6's twelve-trigger text (F0 r1, never merged); a store carrying it is refused as an unknown marker
AGENT_SCHEMA = "levain-agent-schema/2"

AGENT_TABLES = ("agent_settings", "agent_roles", "agents", "agent_specs", "agent_occurrences", "agent_bars",
                "agent_slots", "agent_reservations")

AGENT_TRIGGERS = ("agent_occurrences_frozen", "agent_occurrences_identity", "agent_occurrences_launch_epoch",
                  "agent_occurrences_prelaunch", "agent_occurrences_launch_immutable", "agent_occurrences_insert",
                  "agent_occurrences_one_fire", "agent_occurrences_keep", "agent_specs_frozen", "agent_specs_keep",
                  "agent_bars_keep", "agent_bars_closed_frozen", "agent_occurrences_transition",
                  "agent_occurrences_epoch", "agent_specs_no_replace", "agent_bars_no_replace", "agent_bars_identity")

AGENT_OBJECT_DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS agent_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_roles (role_hash TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,"
    " spec TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(name, version))",
    "CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, name TEXT NOT NULL,"
    " current_version INTEGER NOT NULL,"
    " registration TEXT NOT NULL CHECK (registration IN ('registered','retired')),"
    " operation TEXT NOT NULL CHECK (operation IN ('active','paused')),"
    " failure_streak INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_specs (agent_id TEXT NOT NULL, version INTEGER NOT NULL,"
    " spec_hash TEXT NOT NULL UNIQUE, role_hash TEXT NOT NULL, spec TEXT NOT NULL, created_at TEXT NOT NULL,"
    " PRIMARY KEY (agent_id, version))",
    "CREATE TABLE IF NOT EXISTS agent_occurrences (occurrence_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,"
    " kind TEXT NOT NULL CHECK (kind IN ('scheduled','manual')), key TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('admitted','launched','done','failed','stopped',"
    "'skipped-no-input','skipped-overlap','skipped-budget','missed')),"
    " reason TEXT, epoch INTEGER NOT NULL DEFAULT 0, launch_epoch INTEGER, window_key TEXT NOT NULL,"
    " spec_hash TEXT NOT NULL, binding_id TEXT NOT NULL, link_id TEXT, link_incarnation TEXT,"
    " run_id TEXT NOT NULL UNIQUE, admitted_at TEXT NOT NULL, launched_at TEXT, ended_at TEXT,"
    " lease_until TEXT, session_key TEXT, pid INTEGER,"
    " UNIQUE (agent_id, kind, key),"
    " CHECK (state IN ('admitted','launched') OR (ended_at IS NOT NULL AND reason IS NOT NULL)))",
    "CREATE TABLE IF NOT EXISTS agent_bars (bar_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,"
    " barred_at TEXT NOT NULL, head_seq_at_bar INTEGER NOT NULL, revoke_seq INTEGER, closed_at TEXT, reason TEXT,"
    " pending_record TEXT, pending_prev TEXT, pending_seq INTEGER, pending_signer_fp TEXT,"
    " pending_signature TEXT, pending_signed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,"
    " CHECK ((pending_signature IS NULL) = (pending_signed_at IS NULL)))",
    "CREATE UNIQUE INDEX IF NOT EXISTS agent_bars_open ON agent_bars(agent_id) WHERE closed_at IS NULL",
    "CREATE TABLE IF NOT EXISTS agent_slots (slot INTEGER PRIMARY KEY,"
    " owner_kind TEXT NOT NULL CHECK (owner_kind IN ('occurrence','launch')),"
    " owner_id TEXT NOT NULL UNIQUE, taken_at TEXT NOT NULL, expires_at TEXT)",
    "CREATE TABLE IF NOT EXISTS agent_reservations (occurrence_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,"
    " window_key TEXT NOT NULL, reserved_usd_micros INTEGER NOT NULL, debited_usd_micros INTEGER,"
    " state TEXT NOT NULL CHECK (state IN ('open','settled')), settled_reason TEXT)",
    # the triggers; each BEFORE, each RAISE(ABORT) (which ignores the statement's OR clause)
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_frozen BEFORE UPDATE ON agent_occurrences"
    " WHEN OLD.state NOT IN ('admitted','launched')"
    " BEGIN SELECT RAISE(ABORT, 'agent occurrence is terminal'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_identity BEFORE UPDATE ON agent_occurrences"
    " WHEN NEW.occurrence_id IS NOT OLD.occurrence_id OR NEW.agent_id IS NOT OLD.agent_id"
    " OR NEW.kind IS NOT OLD.kind OR NEW.key IS NOT OLD.key OR NEW.window_key IS NOT OLD.window_key"
    " OR NEW.run_id IS NOT OLD.run_id OR NEW.spec_hash IS NOT OLD.spec_hash"
    " OR NEW.binding_id IS NOT OLD.binding_id OR NEW.admitted_at IS NOT OLD.admitted_at"
    " BEGIN SELECT RAISE(ABORT, 'occurrence identity is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_launch_epoch BEFORE UPDATE ON agent_occurrences"
    " WHEN OLD.state = 'admitted' AND NEW.state = 'launched'"
    " AND (NEW.launch_epoch IS NULL OR NEW.launch_epoch != OLD.epoch OR NEW.epoch IS NOT OLD.epoch"
    " OR NEW.link_id IS NULL OR NEW.link_incarnation IS NULL OR NEW.launched_at IS NULL)"
    " BEGIN SELECT RAISE(ABORT, 'launch must capture launch_epoch, link_id and link_incarnation'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_prelaunch BEFORE UPDATE ON agent_occurrences"
    " WHEN OLD.state = 'admitted' AND NEW.state != 'launched'"
    " AND (NEW.launch_epoch IS NOT NULL OR NEW.link_id IS NOT NULL"
    " OR NEW.link_incarnation IS NOT NULL OR NEW.launched_at IS NOT NULL)"
    " BEGIN SELECT RAISE(ABORT, 'launch columns are written only by the launch'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_launch_immutable BEFORE UPDATE ON agent_occurrences"
    " WHEN OLD.launch_epoch IS NOT NULL"
    " AND (NEW.launch_epoch IS NOT OLD.launch_epoch OR NEW.link_id IS NOT OLD.link_id"
    " OR NEW.link_incarnation IS NOT OLD.link_incarnation OR NEW.launched_at IS NOT OLD.launched_at)"
    " BEGIN SELECT RAISE(ABORT, 'launch columns are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_insert BEFORE INSERT ON agent_occurrences"
    " WHEN NEW.state NOT IN ('admitted','skipped-overlap')"
    " OR NEW.launch_epoch IS NOT NULL OR NEW.link_id IS NOT NULL"
    " OR NEW.link_incarnation IS NOT NULL OR NEW.launched_at IS NOT NULL"
    " BEGIN SELECT RAISE(ABORT, 'a claim inserts admitted or skipped-overlap'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_one_fire BEFORE INSERT ON agent_occurrences"
    " WHEN EXISTS (SELECT 1 FROM agent_occurrences WHERE occurrence_id = NEW.occurrence_id"
    " OR run_id = NEW.run_id OR (agent_id = NEW.agent_id AND kind = NEW.kind AND key = NEW.key))"
    " BEGIN SELECT RAISE(ABORT, 'one key is one fire'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_keep BEFORE DELETE ON agent_occurrences"
    " BEGIN SELECT RAISE(ABORT, 'occurrences are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_specs_frozen BEFORE UPDATE ON agent_specs"
    " BEGIN SELECT RAISE(ABORT, 'agent specs are immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_specs_keep BEFORE DELETE ON agent_specs"
    " BEGIN SELECT RAISE(ABORT, 'agent specs are never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_bars_keep BEFORE DELETE ON agent_bars"
    " BEGIN SELECT RAISE(ABORT, 'bars are closed, never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_bars_closed_frozen BEFORE UPDATE ON agent_bars"
    " WHEN OLD.closed_at IS NOT NULL"
    " BEGIN SELECT RAISE(ABORT, 'a closed bar is never changed'); END",
    # r7 (F0 L3 r1): the A0 §2.5 transition table, the epoch's direction, and REPLACE bounded on every connection
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_transition BEFORE UPDATE ON agent_occurrences"
    " WHEN (OLD.state = 'admitted' AND NEW.state NOT IN ('admitted','launched','skipped-no-input',"
    "'skipped-budget','stopped','missed'))"
    " OR (OLD.state = 'launched' AND NEW.state NOT IN ('launched','done','failed','stopped'))"
    " BEGIN SELECT RAISE(ABORT, 'not a step of the occurrence state machine'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_occurrences_epoch BEFORE UPDATE ON agent_occurrences"
    " WHEN NEW.epoch < OLD.epoch"
    " BEGIN SELECT RAISE(ABORT, 'an occurrence epoch never goes down'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_specs_no_replace BEFORE INSERT ON agent_specs"
    " WHEN EXISTS (SELECT 1 FROM agent_specs WHERE (agent_id = NEW.agent_id AND version = NEW.version)"
    " OR spec_hash = NEW.spec_hash)"
    " BEGIN SELECT RAISE(ABORT, 'a spec is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_bars_no_replace BEFORE INSERT ON agent_bars"
    " WHEN EXISTS (SELECT 1 FROM agent_bars WHERE bar_id = NEW.bar_id"
    " OR (NEW.closed_at IS NULL AND closed_at IS NULL AND agent_id = NEW.agent_id))"
    " BEGIN SELECT RAISE(ABORT, 'a bar is never replaced'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_bars_identity BEFORE UPDATE ON agent_bars"
    " WHEN NEW.bar_id IS NOT OLD.bar_id OR NEW.agent_id IS NOT OLD.agent_id"
    " BEGIN SELECT RAISE(ABORT, 'bar identity is immutable'); END",
)

_MasterRow = tuple[str, str, str, "str | None"]


def create_agent_objects(conn: sqlite3.Connection) -> None:
    """The agent tables, the index and the triggers. Pure: it touches no host table (no ``meta``)."""
    for stmt in AGENT_OBJECT_DDL:
        conn.execute(stmt)


def ddl_agents(conn: sqlite3.Connection) -> None:
    """The host-store step: :func:`create_agent_objects`, then the ``agent_schema`` marker. The caller holds the
    transaction (``db.py`` ``_ensure_schema``'s ``BEGIN IMMEDIATE``), so a crash between two statements rolls the
    whole open back. On a database with no ``meta`` table it raises."""
    create_agent_objects(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES ('agent_schema', ?)", (AGENT_SCHEMA,))


def agent_master(conn: sqlite3.Connection) -> list[_MasterRow]:
    """The ``sqlite_master`` rows of every agent object: a row whose table is one of the eight agent tables or
    whose name is in :data:`AGENT_TRIGGERS` (SQLite's own ``sqlite_autoindex_*`` rows of those tables count,
    with their NULL ``sql``)."""
    query = ("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name IN ({}) OR name IN ({}) "
             "ORDER BY type, name").format(",".join("?" * len(AGENT_TABLES)), ",".join("?" * len(AGENT_TRIGGERS)))
    return [tuple(r) for r in conn.execute(query, AGENT_TABLES + AGENT_TRIGGERS).fetchall()]  # type: ignore[misc]


def reference_master() -> list[_MasterRow]:
    """What :func:`agent_master` returns for a store this binary made: :func:`create_agent_objects` alone, on a
    fresh ``:memory:`` connection. SQLite rewrites the stored text (``IF NOT EXISTS`` is stripped), so the reference
    is built by the running SQLite and never written out."""
    ref = sqlite3.connect(":memory:")
    try:
        create_agent_objects(ref)
        return agent_master(ref)
    finally:
        ref.close()


def has_agent_object(conn: sqlite3.Connection) -> bool:
    return bool(agent_master(conn))


def agent_marker(conn: sqlite3.Connection) -> str | None:
    """``meta.agent_schema``; ``None`` when there is none (or no ``meta`` table at all)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'agent_schema'").fetchone()
    return None if row is None else row[0]


def check_agent_schema(conn: sqlite3.Connection) -> str | None:
    """``None`` only when the marker is :data:`AGENT_SCHEMA` and the store's agent rows equal the reference exactly
    (none missing, none extra, every statement's text identical); otherwise the named reason. Writes nothing and
    never repairs."""
    marker = agent_marker(conn)
    have = agent_master(conn)
    if marker is None:
        return "agent objects with no agent_schema marker" if have else "no agent schema"
    if marker != AGENT_SCHEMA:
        return f"unknown agent schema marker {marker!r}"
    ref = reference_master()
    if have != ref:
        missing = sorted({r[1] for r in set(ref) - set(have)})
        extra = sorted({r[1] for r in set(have) - set(ref)})
        return f"agent schema differs: missing/changed {missing}, extra/changed {extra}"
    return None
