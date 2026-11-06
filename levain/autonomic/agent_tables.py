"""levain.autonomic.agent_tables — the agent DDL, its triggers and the agent schema marker (A0 r7 §3.5, §3.7, §4.2).

A leaf: stdlib ``sqlite3`` only. It holds the ten agent tables, one index and the ``BEFORE`` triggers of the
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
           "check_agent_schema", "AGENT_SCHEMA_V2", "AGENT_SCHEMA_V3", "AGENT_SCHEMA_PREDECESSORS", "AGENT_DDL_V3",
           "OPERATOR_DDL_V4", "RESERVATION_COLUMNS_V3", "migrate_agent_schema", "AgentSchemaNotMigrated",
           "AgentSchemaRollbackFailed"]

# /1 named A0 r6's twelve-trigger text (F0 r1, never merged); a store carrying it is refused as an unknown marker.
# /2 shipped in 0.11.27. /3 adds the seven subscription-route columns to agent_reservations (P7-A §4) and is the first
# marker with a migration. /4 adds the operator agent record and its attachments (K2a): two tables and six triggers.
# :func:`migrate_agent_schema` takes a /2 or a /3 store to the current marker in ONE transaction and nothing else.
AGENT_SCHEMA_V2 = "levain-agent-schema/2"
AGENT_SCHEMA_V3 = "levain-agent-schema/3"
AGENT_SCHEMA = "levain-agent-schema/4"

# the markers an older levain wrote that this one migrates, oldest first; a store carrying one is not corrupt, it is old
AGENT_SCHEMA_PREDECESSORS = (AGENT_SCHEMA_V2, AGENT_SCHEMA_V3)

# the columns /3 appends to agent_reservations, in order, as (name, type). A fresh store's DDL ends with the same text
# ``ALTER TABLE ... ADD COLUMN`` leaves in sqlite_master, so a migrated store and a fresh one compare equal.
RESERVATION_COLUMNS_V3 = (("billing", "TEXT"), ("cap_actions", "INTEGER"), ("cap_tunnel_bytes", "INTEGER"),
                          ("cap_seconds", "INTEGER"), ("used_actions", "INTEGER"), ("used_tunnel_bytes", "INTEGER"),
                          ("used_seconds", "INTEGER"))

AGENT_TABLES = ("agent_settings", "agent_roles", "agents", "agent_specs", "agent_occurrences", "agent_bars",
                "agent_slots", "agent_reservations", "agent_operator", "agent_attachments")

AGENT_TRIGGERS = ("agent_occurrences_frozen", "agent_occurrences_identity", "agent_occurrences_launch_epoch",
                  "agent_occurrences_prelaunch", "agent_occurrences_launch_immutable", "agent_occurrences_insert",
                  "agent_occurrences_one_fire", "agent_occurrences_keep", "agent_specs_frozen", "agent_specs_keep",
                  "agent_bars_keep", "agent_bars_closed_frozen", "agent_occurrences_transition",
                  "agent_occurrences_epoch", "agent_specs_no_replace", "agent_bars_no_replace", "agent_bars_identity",
                  "agent_operator_no_replace", "agent_operator_frozen", "agent_operator_keep",
                  "agent_attachments_no_replace", "agent_attachments_frozen", "agent_attachments_keep")

# the objects of a /3 store, unchanged text since /3 (a frozen copy of the /3 module pins it in the tests)
AGENT_DDL_V3: tuple[str, ...] = (
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
    " state TEXT NOT NULL CHECK (state IN ('open','settled')), settled_reason TEXT,"
    " billing TEXT, cap_actions INTEGER, cap_tunnel_bytes INTEGER, cap_seconds INTEGER,"
    " used_actions INTEGER, used_tunnel_bytes INTEGER, used_seconds INTEGER)",
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

# K2a (agent schema /4): the operator agent record and the roles attached to it. The record is a one-slot table, so "one
# agent per store" is the schema's, held by SQLite on every connection; an attachment ties a registered spec to the role
# NAME it pins. Nothing here is read by a fire, a link, a binding or a recall: attaching confers nothing in 1.0.
OPERATOR_DDL_V4: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS agent_operator (slot INTEGER PRIMARY KEY CHECK (slot = 1),"
    " record_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS agent_attachments (agent_id TEXT PRIMARY KEY, role_name TEXT NOT NULL,"
    " attached_at TEXT NOT NULL)",
    "CREATE TRIGGER IF NOT EXISTS agent_operator_no_replace BEFORE INSERT ON agent_operator"
    " WHEN EXISTS (SELECT 1 FROM agent_operator)"
    " BEGIN SELECT RAISE(ABORT, 'one agent per agent store'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_operator_frozen BEFORE UPDATE ON agent_operator"
    " BEGIN SELECT RAISE(ABORT, 'the operator agent record is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_operator_keep BEFORE DELETE ON agent_operator"
    " BEGIN SELECT RAISE(ABORT, 'the operator agent record is never deleted'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_attachments_no_replace BEFORE INSERT ON agent_attachments"
    " WHEN EXISTS (SELECT 1 FROM agent_attachments WHERE agent_id = NEW.agent_id)"
    " OR NOT EXISTS (SELECT 1 FROM agent_operator)"
    " OR NOT EXISTS (SELECT 1 FROM agents WHERE agent_id = NEW.agent_id)"
    " BEGIN SELECT RAISE(ABORT, 'an attachment is written once, to the store''s agent, for a registered spec'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_attachments_frozen BEFORE UPDATE ON agent_attachments"
    " BEGIN SELECT RAISE(ABORT, 'an attachment is immutable'); END",
    "CREATE TRIGGER IF NOT EXISTS agent_attachments_keep BEFORE DELETE ON agent_attachments"
    " BEGIN SELECT RAISE(ABORT, 'an attachment is never deleted'); END",
)

AGENT_OBJECT_DDL: tuple[str, ...] = AGENT_DDL_V3 + OPERATOR_DDL_V4

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
    """The ``sqlite_master`` rows of every agent object: a row whose table is one of the ten agent tables or
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


class AgentSchemaNotMigrated(sqlite3.DatabaseError):
    """A ``/2`` or ``/3`` store that does not reach the current reference by the migration (its agent objects were
    altered by hand): nothing of the migration is kept and the store stays at the marker it started from, which
    ``levain agents`` then refuses. The message names that starting marker."""


class AgentSchemaRollbackFailed(sqlite3.DatabaseError):
    """A failed migration whose own undo (``ROLLBACK`` / ``ROLLBACK TO``) also failed: its writes may still be in the
    caller's transaction. Deliberately NOT an :class:`AgentSchemaNotMigrated`, so the opener does not catch it and its
    transaction is rolled back instead of committed. The original error is chained."""


_SAVEPOINT = "levain_agent_schema_migrate"


def _step_v2_to_v3(conn: sqlite3.Connection) -> None:
    """The seven reservation columns. A column already present is skipped."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(agent_reservations)")}
    if not have:
        raise AgentSchemaNotMigrated("no agent_reservations table")
    for name, kind in RESERVATION_COLUMNS_V3:
        if name not in have:
            conn.execute(f"ALTER TABLE agent_reservations ADD COLUMN {name} {kind}")


def _step_v3_to_v4(conn: sqlite3.Connection) -> None:
    """The operator record and attachment tables and their six triggers, from the SAME statements a fresh store runs, so
    a migrated store and a fresh one compare equal. An object already there is kept, and the final check judges it."""
    for stmt in OPERATOR_DDL_V4:
        conn.execute(stmt)


# the step that takes a store AT the key marker one marker on; the chain from a store's marker is every step from there
_STEPS = ((AGENT_SCHEMA_V2, _step_v2_to_v3), (AGENT_SCHEMA_V3, _step_v3_to_v4))


def _short(marker: str | None) -> str:
    return "/" + marker.rsplit("/", 1)[-1] if marker else "unmarked"


def migrate_agent_schema(conn: sqlite3.Connection) -> bool:
    """Take a ``/2`` or ``/3`` store to the current marker: every step from the store's marker on (``/2 -> /3``: the
    reservation columns; ``/3 -> /4``: the operator tables and triggers), then the marker, ONCE, in ONE ``BEGIN
    IMMEDIATE``. Returns True when it migrated, False when the store was at neither (no marker, already current, or any
    other marker: all left untouched, so the caller's opener still refuses an unknown marker through
    :func:`check_agent_schema`).

    The marker is written after the last step and :func:`check_agent_schema` runs ONCE after it, against the CURRENT
    reference. Checking after each hop would judge a store halfway along the chain against a reference it cannot yet
    meet, and strand every ``/2`` store.

    Inside a caller's open transaction it joins it under a SAVEPOINT (the caller commits or rolls back the whole);
    otherwise it opens its own. On any failure its own writes are undone either way (the savepoint is rolled back to),
    so a caller that catches the error and commits keeps no half-migrated or newly marked store. A second run, or one
    after a crash (which rolled the whole transaction back), lands in the same place. A store whose objects were
    altered by hand fails the check (or lacks a table a step alters), nothing is kept, and
    :class:`AgentSchemaNotMigrated` is raised naming the marker the store started from and the reason. When the undo
    itself fails, :class:`AgentSchemaRollbackFailed` is raised instead (not a subclass: a caller that catches the first
    and commits must not catch this one; its transaction has to be rolled back)."""
    own = not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE" if own else f"SAVEPOINT {_SAVEPOINT}")
    open_ = True    # the transaction (own) or the savepoint (joined) this call began and has not yet ended
    start = None
    try:
        start = agent_marker(conn)
        markers = [m for m, _ in _STEPS]
        chain = [step for _, step in _STEPS[markers.index(start):]] if start in markers else []
        if not chain:
            conn.execute("ROLLBACK" if own else f"RELEASE {_SAVEPOINT}")
            open_ = False
            return False
        for step in chain:
            step(conn)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'agent_schema' AND value = ?", (AGENT_SCHEMA, start))
        reason = check_agent_schema(conn)
        if reason is not None:
            raise AgentSchemaNotMigrated(reason)
        conn.execute("COMMIT" if own else f"RELEASE {_SAVEPOINT}")
        open_ = False
        return True
    except BaseException as err:
        if open_ and conn.in_transaction:
            # the undo. If it fails, this call's writes may still be in the transaction, and the catchable
            # AgentSchemaNotMigrated would let a caller that catches it COMMIT them (the opener does): a store marked
            # current whose objects were never validated. So that failure raises AgentSchemaRollbackFailed, which no
            # caller catches, and the caller's transaction is rolled back.
            try:
                conn.execute("ROLLBACK" if own else f"ROLLBACK TO {_SAVEPOINT}")
            except sqlite3.Error as undo:
                raise AgentSchemaRollbackFailed(
                    f"the {_short(start)} agent schema did not migrate cleanly and its writes could not be undone "
                    f"({undo}); the transaction must be rolled back: {err}") from err
            if not own:
                # the writes are undone; a RELEASE that fails ("no such savepoint") leaves nothing of them to keep, and
                # must not replace the real error
                try:
                    conn.execute(f"RELEASE {_SAVEPOINT}")
                except sqlite3.Error:
                    pass
        if isinstance(err, sqlite3.Error):
            # one error class for the opener to catch; the store is left at its starting marker and the cause is chained
            named = f"the {_short(start)} agent schema did not migrate cleanly: "
            raise AgentSchemaNotMigrated(named + str(err)) from err
        raise
