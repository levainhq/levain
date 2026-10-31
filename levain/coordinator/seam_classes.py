"""Which coordinator tables the open brief counts (C15 S0, the class ruling ``S0_CLASSES.md``).

Every table in a maximal coordinator database (``dispatch.db`` with the envelope, chat and egress tables) is in exactly
one class, and the S0 census test fails on a table or a kind that is not listed here:

* :data:`COUNTED`: a row the open brief reports as "something happened since you left". It carries a ``gseq`` taken from
  ``next_gseq`` in its own transaction. ``lease_events`` and ``launch_events`` are counted by KIND (:data:`KIND_CLASSES`).
* :data:`INFRASTRUCTURE`: bookkeeping that never reports.
* :data:`EXEMPT`: not part of the cut, each with its reason.

:func:`unsequenced` is the one reading of a hole in the order: a row of a migrated table with no ``gseq`` that was
written AFTER the migration found the table (an older binary, which does not know the column, wrote it). Stdlib only.
"""
from __future__ import annotations

import sqlite3

__all__ = ["COUNTED", "EXEMPT", "INFRASTRUCTURE", "KIND_CLASSES", "SeamClassError", "unsequenced"]


class SeamClassError(Exception):
    """A table the migration never bounded, or a name that is not a table name."""


#: whole tables the brief counts (``lease_events`` and ``launch_events`` by kind: :data:`KIND_CLASSES`)
COUNTED: frozenset[str] = frozenset({
    "drops", "threads", "thread_revisions", "thread_events", "replies", "ready_journal",
    "envelopes", "envelope_binds", "envelope_attenuations", "coord_settings",
    "lease_events", "launch_events",
    "review_items", "review_events", "first_reads", "team_drafts", "team_draft_events",
    "chat_messages", "chat_anomalies", "chat_approvals", "chat_approval_events",
})

#: bookkeeping the brief never reports (``lease_events`` kinds claim, beat, release and ``launch_events`` kinds pending,
#: active are INFRASTRUCTURE too, by kind)
INFRASTRUCTURE: frozenset[str] = frozenset({
    "launches", "launch_spawns", "launch_threads", "launch_sweeps",
    "review_marks", "know_deliveries", "held_pings", "held_ping_releases",
    "chat_events", "chat_outbox_events", "chat_cursors", "chat_echoes", "chat_launch_checks",
    "turn_lease_events", "chat_egress_events",
})

_DISPATCH = ("ordered by dispatch's own transitions; the open brief does not report dispatch jobs (the board and the "
             "band carry work state). A later row if the coordinator should see job completions in Since")

#: not part of the cut: table -> why
EXEMPT: dict[str, str] = {
    "gseq": "the allocator",
    "operator_views": "its gseq column holds the viewer's read cursor, copied from an existing gseq, NOT allocated by "
                      "next_gseq (work_delta.py); never an allocator-fed table",
    "ready_items": "a DERIVED state row; ready_journal is the record",
    "sqlite_sequence": "SQLite's own",
    "gseq_migration_bounds": "C15's own table (S0): the largest rowid each migrated table held when it was first "
                             "migrated; it records the migration, it is not a record of the work",
    **{name: _DISPATCH for name in (
        "artifact_take_failures", "artifact_takes", "authorise_signatures", "cancel_signatures", "event_cursor",
        "handoffs", "job_envelopes", "job_filers", "job_threads", "jobs", "meta", "run_records", "signatures",
        "transitions", "verifier_refusals")},
}

#: a table counted by kind: table -> class -> the kinds in it. Together the kinds are exactly the table's kind CHECK
#: (``lease.LEASE_KINDS`` / ``lease.LAUNCH_KINDS``; the census test holds that)
KIND_CLASSES: dict[str, dict[str, tuple[str, ...]]] = {
    "lease_events": {"COUNTED": ("takeover", "lapse"), "INFRASTRUCTURE": ("claim", "beat", "release")},
    "launch_events": {"COUNTED": ("failed", "expired", "ended"), "INFRASTRUCTURE": ("pending", "active")},
}


def unsequenced(conn: sqlite3.Connection, table: str) -> int:
    """How many rows of ``table`` hold no ``gseq`` although they were written after the migration bounded the table:
    ``gseq IS NULL AND rowid > baseline_rowid``. A row the migration found (rowid at or below the bound) is legacy and
    is not counted; a row an older binary writes later is, and stays counted after any number of newer writes (the
    bound never moves). Raises :class:`SeamClassError` for a table with no bound row (it was never migrated)."""
    if not isinstance(table, str) or not table.isidentifier():
        raise SeamClassError(f"{table!r} is not a table name")
    row = conn.execute("SELECT baseline_rowid FROM gseq_migration_bounds WHERE table_name = ?", (table,)).fetchone()
    if row is None:
        raise SeamClassError(f"{table} has no gseq migration bound: it was never migrated")
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE gseq IS NULL AND rowid > ?", (row[0],)).fetchone()[0]
