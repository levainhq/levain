"""The open's records (C15 S2b): ``open_builds``, ``open_deliveries``, ``open_acks``, ``open_unsequenced_cuts`` and
``arrival_signals``, the fence that guards the acknowledging write, and the cursor rule.

A build is the rendered brief a coordinator was handed at a seam (:mod:`levain.coordinator.seam` renders it,
:mod:`levain.coordinator.seam_build` writes it). Nothing here moves the "since" window except an ACKNOWLEDGED build:

* **Cursor rule.** The window "since you left" opens at ``MAX(shown_through_gseq)`` over ACKED builds (``0`` when none),
  and, per migrated table, at the highest unsequenced rowid an acked build showed (``open_unsequenced_cuts``). A build
  or a delivery nobody acknowledged moves nothing, and the next brief lists it as never acknowledged (at-least-once).
  The MAX never regresses: a later ack of an older, smaller build cannot pull it back.
* **The fence.** :func:`active_coordinator_fence` is the lease fence plus one rule: the holder is an ACTIVE COORDINATOR
  LAUNCH's session key. ``coord take`` makes a lease held by ``operator`` that passes the plain fence with no session
  behind it; here it is refused, and the writer / reader session is read from the launch, never from request data.

Every table is append-only by trigger (the ``seam_handoffs`` pattern); a raw INSERT is not fenced, the writers are.
Stdlib only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from levain.coordinator import lease as _leases
from levain.coordinator.lease import Launch, Lease, LeaseRefused
from levain.coordinator.store import GSEQ_TABLES, CoordError, CoordStore, _append_only, next_gseq

__all__ = ["BODY_CAP", "DELIVERY_SURFACES", "OPEN_SCHEMA", "OpenBuild", "SEAM_KINDS", "SIGNAL_KINDS",
           "acked_cursor", "acked_cuts", "acknowledge", "active_coordinator_fence", "open_tables_present",
           "record_arrival", "record_delivery"]

#: the seams an open is built for; ``light`` is an open with no handoff behind it
SEAM_KINDS = ("rotation", "operator", "dream", "light")
#: what fired a build; (signal_kind, signal_key) is unique, so one signal is one build
SIGNAL_KINDS = ("launch", "away_off", "silence", "rotation", "dream")
DELIVERY_SURFACES = ("launch_brief", "light_verb")
#: the rendered brief, in bytes. Levain's own number, a guess until a coordinator has read a real one
BODY_CAP = 65536
_READ_BACK_MAX = 8192

_SEAMS_SQL = ", ".join(repr(k) for k in SEAM_KINDS)
_SIGNALS_SQL = ", ".join(repr(k) for k in SIGNAL_KINDS)
_SURFACES_SQL = ", ".join(repr(k) for k in DELIVERY_SURFACES)
_TABLES_SQL = ", ".join(repr(t) for t in GSEQ_TABLES)


def _no_replace(table: str, key: str, what: str) -> str:
    # INSERT OR REPLACE deletes the old row without firing a delete trigger: refuse a new row reusing any key
    return (f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM "
            f"{table} WHERE {key}) BEGIN SELECT RAISE(ABORT, '{what} is never replaced'); END")


OPEN_SCHEMA: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS open_builds (open_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    f"seam_kind TEXT NOT NULL CHECK (seam_kind IN ({_SEAMS_SQL})), "
    f"signal_kind TEXT NOT NULL CHECK (signal_kind IN ({_SIGNALS_SQL})), "
    "signal_key TEXT NOT NULL CHECK (length(signal_key) > 0), "
    "cursor_gseq INTEGER NOT NULL CHECK (cursor_gseq >= 0), "
    "shown_through_gseq INTEGER NOT NULL CHECK (shown_through_gseq >= 0 AND shown_through_gseq <= cursor_gseq), "
    "handoff_id INTEGER, requires_ack INTEGER NOT NULL CHECK (requires_ack = 1), "
    f"body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) <= {BODY_CAP}), "
    "body_sha256 TEXT NOT NULL, built_at TEXT NOT NULL, UNIQUE (signal_kind, signal_key))",
    *_append_only("open_builds", "an open build"),
    _no_replace("open_builds", "open_id = NEW.open_id OR (signal_kind = NEW.signal_kind AND signal_key = NEW.signal_key)",
                "an open build"),
    "CREATE TABLE IF NOT EXISTS open_deliveries (open_id INTEGER NOT NULL REFERENCES open_builds(open_id), "
    f"surface TEXT NOT NULL CHECK (surface IN ({_SURFACES_SQL})), delivered_at TEXT NOT NULL, "
    "PRIMARY KEY (open_id, surface))",
    *_append_only("open_deliveries", "an open delivery"),
    _no_replace("open_deliveries", "open_id = NEW.open_id AND surface = NEW.surface", "an open delivery"),
    "CREATE TABLE IF NOT EXISTS open_acks (open_id INTEGER NOT NULL REFERENCES open_builds(open_id), "
    f"reader_session TEXT NOT NULL CHECK (length(reader_session) > 0), "
    f"read_back TEXT NOT NULL CHECK (length(CAST(read_back AS BLOB)) <= {_READ_BACK_MAX}), "
    "acknowledged_at TEXT NOT NULL, PRIMARY KEY (open_id, reader_session))",
    *_append_only("open_acks", "an open acknowledgement"),
    _no_replace("open_acks", "open_id = NEW.open_id AND reader_session = NEW.reader_session",
                "an open acknowledgement"),
    # a rotation or a dream is acknowledged with the successor's restatement of the handoff
    "CREATE TRIGGER IF NOT EXISTS open_acks_read_back BEFORE INSERT ON open_acks WHEN (SELECT seam_kind FROM "
    "open_builds WHERE open_id = NEW.open_id) IN ('rotation', 'dream') AND length(trim(NEW.read_back, ' ' || "
    "char(9, 10, 11, 12, 13))) = 0 BEGIN SELECT RAISE(ABORT, 'a rotation or dream open is acknowledged with a "
    "read-back'); END",
    # the highest unsequenced rowid per migrated table that a build showed: its ack moves that table's cut
    "CREATE TABLE IF NOT EXISTS open_unsequenced_cuts (open_id INTEGER NOT NULL REFERENCES open_builds(open_id), "
    f"table_name TEXT NOT NULL CHECK (table_name IN ({_TABLES_SQL})), max_rowid INTEGER NOT NULL CHECK (max_rowid > 0), "
    "PRIMARY KEY (open_id, table_name))",
    *_append_only("open_unsequenced_cuts", "an unsequenced cut"),
    _no_replace("open_unsequenced_cuts", "open_id = NEW.open_id AND table_name = NEW.table_name",
                "an unsequenced cut"),
    # a verified leave consumed by an open, in the store-wide order so the next brief can count it
    "CREATE TABLE IF NOT EXISTS arrival_signals (signal_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "agent TEXT NOT NULL CHECK (length(agent) > 0), mark_id TEXT NOT NULL CHECK (length(mark_id) > 0), "
    "queue_id TEXT NOT NULL, ingested_at TEXT NOT NULL, gseq INTEGER NOT NULL UNIQUE, UNIQUE (agent, mark_id))",
    *_append_only("arrival_signals", "an arrival signal"),
    _no_replace("arrival_signals", "signal_id = NEW.signal_id OR gseq = NEW.gseq OR (agent = NEW.agent AND mark_id = "
                "NEW.mark_id)", "an arrival signal"),
)

_OPEN_TABLES = ("open_builds", "open_deliveries", "open_acks", "open_unsequenced_cuts")
_BUILD_COLUMNS = ("open_id, seam_kind, signal_kind, signal_key, cursor_gseq, shown_through_gseq, handoff_id, "
                  "requires_ack, body, body_sha256, built_at")


@dataclass(frozen=True)
class OpenBuild:
    open_id: int
    seam_kind: str
    signal_kind: str
    signal_key: str
    cursor_gseq: int
    shown_through_gseq: int
    handoff_id: int | None
    requires_ack: int
    body: str
    body_sha256: str
    built_at: str


def build_row(r: Any) -> OpenBuild | None:
    return None if r is None else OpenBuild(*r)


def open_tables_present(conn: sqlite3.Connection) -> bool:
    """Whether the four open tables are all in the file. False is a store no writer has brought current (the base read
    gate does not ask for them): the cursor is then 0 and nothing was ever built, said so rather than shown as empty."""
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return set(_OPEN_TABLES) <= have


def acked_cursor(conn: sqlite3.Connection) -> tuple[int, int | None]:
    """``(cursor, open_id)``: ``MAX(shown_through_gseq)`` over acknowledged builds, and the build that holds it (the
    newest among equals); ``(0, None)`` when none is acknowledged or the open tables are absent."""
    if not open_tables_present(conn):
        return 0, None
    row = conn.execute("SELECT b.shown_through_gseq, b.open_id FROM open_builds b WHERE EXISTS (SELECT 1 FROM "
                       "open_acks a WHERE a.open_id = b.open_id) ORDER BY b.shown_through_gseq DESC, b.open_id DESC "
                       "LIMIT 1").fetchone()
    return (0, None) if row is None else (row[0], row[1])


def acked_cuts(conn: sqlite3.Connection) -> dict[str, int]:
    """Per migrated table, the highest unsequenced rowid any ACKNOWLEDGED build showed."""
    if not open_tables_present(conn):
        return {}
    return dict(conn.execute("SELECT c.table_name, MAX(c.max_rowid) FROM open_unsequenced_cuts c WHERE EXISTS "
                             "(SELECT 1 FROM open_acks a WHERE a.open_id = c.open_id) GROUP BY c.table_name"))


def active_coordinator_fence(conn: sqlite3.Connection, now: str, token: object, what: str = "a seam record"
                             ) -> tuple[Lease, Launch]:
    """The lease fence, then one rule more: the lease holder is an ACTIVE COORDINATOR LAUNCH's session key. Inside the
    caller's write transaction. ``(lease, launch)``: the writer or reader session is ``launch.session_key``, derived
    here, never taken from a request. Refused: a token that is not the live lease's (:func:`lease.fence`), the
    ``operator`` holder ``coord take`` creates, a holder that is not an active launch of the coordinator role."""
    held = _leases.fence(conn, now, token)
    launch = None if held.holder == "operator" else _leases._launch_by_key(conn, held.holder)
    if launch is None or launch.role != "coordinator" or launch.state != "active":
        raise LeaseRefused(f"{what} is written by an active coordinator launch; the lease holder is "
                           f"{'the operator' if held.holder == 'operator' else held.holder}; nothing was written")
    return held, launch


def insert_build(conn: sqlite3.Connection, now: str, *, seam_kind: str, signal_kind: str, signal_key: str,
                 cursor_gseq: int, shown_through_gseq: int, handoff_id: int | None, body: str, body_sha256: str,
                 cuts: dict[str, int]) -> OpenBuild:
    """The ONE insert of a build and its unsequenced cuts, inside the caller's (already fenced or revalidated) write
    transaction. A build of a signal already built returns the stored one and writes nothing."""
    stored = build_row(conn.execute(f"SELECT {_BUILD_COLUMNS} FROM open_builds WHERE signal_kind = ? AND "
                                    "signal_key = ?", (signal_kind, signal_key)).fetchone())
    if stored is not None:
        return stored
    cur = conn.execute(
        "INSERT INTO open_builds (seam_kind, signal_kind, signal_key, cursor_gseq, shown_through_gseq, handoff_id, "
        "requires_ack, body, body_sha256, built_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (seam_kind, signal_kind, signal_key, cursor_gseq, shown_through_gseq, handoff_id, body, body_sha256, now))
    for table, max_rowid in sorted(cuts.items()):
        conn.execute("INSERT INTO open_unsequenced_cuts (open_id, table_name, max_rowid) VALUES (?, ?, ?)",
                     (cur.lastrowid, table, max_rowid))
    return build_row(conn.execute(f"SELECT {_BUILD_COLUMNS} FROM open_builds WHERE open_id = ?",
                                  (cur.lastrowid,)).fetchone())


def acknowledge(store: CoordStore, open_id: Any, *, token: Any, read_back: Any = None) -> tuple[int, str, bool]:
    """The acknowledging write, and the home of the cursor rule: ``(open_id, reader_session, newly)``. Fenced by
    :func:`active_coordinator_fence`: the reader session is the active launch's own key. ``read_back`` is the
    successor's restatement of the handoff, REQUIRED on a rotation or dream build and optional otherwise. A repeat by
    the same session writes nothing (``newly`` False). The window moves to the build's ``shown_through_gseq``, no
    further, so rows a truncated brief did not show come back in the next one."""
    if not isinstance(open_id, int) or isinstance(open_id, bool):
        raise CoordError("an acknowledgement names an open build by its id")
    if read_back is not None and (not isinstance(read_back, str) or "\x00" in read_back):
        raise CoordError("read_back is text")
    text = "" if read_back is None else read_back
    with store._write("coordinator", token) as (conn, now):
        _held, launch = active_coordinator_fence(conn, now, token, "an acknowledgement")
        if not open_tables_present(conn):
            raise CoordError("this store holds no open builds")
        built = conn.execute("SELECT seam_kind FROM open_builds WHERE open_id = ?", (open_id,)).fetchone()
        if built is None:
            raise CoordError(f"there is no open build {open_id}")
        if conn.execute("SELECT 1 FROM open_acks WHERE open_id = ? AND reader_session = ?",
                        (open_id, launch.session_key)).fetchone() is not None:
            return open_id, launch.session_key, False
        if built[0] in ("rotation", "dream") and not text.strip():
            raise CoordError(f"a {built[0]} open is acknowledged with a read-back of the handoff; nothing was written")
        conn.execute("INSERT INTO open_acks (open_id, reader_session, read_back, acknowledged_at) VALUES (?, ?, ?, ?)",
                     (open_id, launch.session_key, text, now))
    return open_id, launch.session_key, True


def record_delivery(store: CoordStore, open_id: Any, surface: Any) -> bool:
    """Evidence that a build was handed over (``launch_brief``: a file given to a harness session; ``light_verb``:
    printed to stdout). Moves nothing: no token, no cursor. True when written, False for a repeat."""
    if surface not in DELIVERY_SURFACES:
        raise CoordError(f"a delivery surface is {', '.join(DELIVERY_SURFACES)}, not {surface!r}")
    if not isinstance(open_id, int) or isinstance(open_id, bool):
        raise CoordError("a delivery names an open build by its id")
    with store._write() as (conn, now):
        if not open_tables_present(conn) or conn.execute("SELECT 1 FROM open_builds WHERE open_id = ?",
                                                         (open_id,)).fetchone() is None:
            raise CoordError(f"there is no open build {open_id}")
        if conn.execute("SELECT 1 FROM open_deliveries WHERE open_id = ? AND surface = ?",
                        (open_id, surface)).fetchone() is not None:
            return False
        conn.execute("INSERT INTO open_deliveries (open_id, surface, delivered_at) VALUES (?, ?, ?)",
                     (open_id, surface, now))
        return True


def record_arrival(conn: sqlite3.Connection, now: str, *, agent: Any, mark_id: Any, queue_id: Any) -> tuple[int, bool]:
    """One verified leave an open consumed, inside the caller's write transaction (the caller holds the fence, or is
    the cold start's starter): ``(signal_id, newly)``. The same ``(agent, mark_id)`` is one signal; a repeat returns it
    and takes no new ``gseq``. The verification of the leave is the caller's (S4)."""
    for name, value in (("agent", agent), ("mark_id", mark_id), ("queue_id", queue_id)):
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip() or "\x00" in str(value):
            raise CoordError(f"an arrival signal's {name} is a non-empty text or number")
    agent, mark_id, queue_id = str(agent), str(mark_id), str(queue_id)
    row = conn.execute("SELECT signal_id FROM arrival_signals WHERE agent = ? AND mark_id = ?",
                       (agent, mark_id)).fetchone()
    if row is not None:
        return row[0], False
    cur = conn.execute("INSERT INTO arrival_signals (agent, mark_id, queue_id, ingested_at, gseq) VALUES (?, ?, ?, ?, ?)",
                       (agent, mark_id, queue_id, now, next_gseq(conn, now)))
    return cur.lastrowid, True
