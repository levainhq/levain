"""The open's records (C15 S2b): ``open_builds``, ``open_deliveries``, ``open_acks``, ``open_unsequenced_cuts`` and
``arrival_signals``, the fence that guards the acknowledging write, and the cursor rule.

A build is the rendered brief a desk was handed at a seam (:mod:`levain.desk.seam` renders it,
:mod:`levain.desk.seam_build` writes it). Nothing here moves the "since" window except an ACKNOWLEDGED build:

* **Cursor rule.** The window "since you left" opens at ``MAX(shown_through_gseq)`` over ACKED builds (``0`` when none),
  and, per migrated table, at the highest unsequenced rowid an acked build showed (``open_unsequenced_cuts``). A build
  or a delivery nobody acknowledged moves nothing, and the next brief lists it as never acknowledged (at-least-once).
  Only a build DELIVERED TO THE ACKING LAUNCH is acknowledged: a delivery row names its recipient launch.
  The MAX never regresses: a later ack of an older, smaller build cannot pull it back.
* **The fence.** :func:`active_desk_fence` is the lease fence plus one rule: the holder is an ACTIVE DESK
  LAUNCH's session key. ``coord take`` makes a lease held by ``operator`` that passes the plain fence with no session
  behind it; here it is refused, and the writer / reader session is read from the launch, never from request data.

Every table is append-only by trigger (the ``seam_handoffs`` pattern); a raw INSERT is not fenced, the writers are.
Every text a caller hands a writer is checked by :func:`checked_text` against its column's rule, in UTF-8 bytes,
before the write opens: a refusal is a :class:`CoordError`, never SQLite's or the codec's. Stdlib only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from levain.desk import lease as _leases
from levain.desk.lease import Launch, Lease, LeaseRefused
from levain.desk.store import GSEQ_TABLES, CoordError, CoordStore, _append_only, next_gseq
from levain.desk.legacy import ROLE_DESK

__all__ = ["BODY_CAP", "DELIVERY_SURFACES", "LAUNCH_SIGNALS", "OPEN_SCHEMA", "OpenBuild", "READ_BACK_MAX",
           "PREDECESSOR_HANDOFF_SEAMS", "SEAM_KINDS", "SIGNAL_KINDS", "acked_cursor", "acked_cuts", "acknowledge", "active_desk_fence",
           "checked_text", "open_tables_present", "record_arrival", "record_delivery", "stored_build"]

#: the seams an open is built for; ``light`` is an open with no handoff behind it
SEAM_KINDS = ("rotation", "operator", "dream", "light")
#: the seams whose open hands the reader a PREDECESSOR's handoff to restate (the ones whose ack carries a read-back);
#: a ``light`` or ``operator`` build only mentions the latest handoff, whoever wrote it
PREDECESSOR_HANDOFF_SEAMS = ("rotation", "dream")
#: what fired a build; (signal_kind, signal_key) is unique, so one signal is one build
SIGNAL_KINDS = ("launch", "away_off", "silence", "rotation", "dream")
#: the signal kinds whose key is a launch id: only the starter's build (``seam_build.starter_build``) makes one
LAUNCH_SIGNALS = ("launch",)
DELIVERY_SURFACES = ("launch_brief", "light_verb")
#: the rendered brief, in bytes. Levain's own number, a guess until a desk has read a real one
BODY_CAP = 65536
#: a read-back, in bytes (its column CHECK holds the same number)
READ_BACK_MAX = 8192
_READ_BACK_MAX = READ_BACK_MAX

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
    # the recipient is the launch the brief was handed to: the one launch whose acknowledgement it can carry
    "CREATE TABLE IF NOT EXISTS open_deliveries (open_id INTEGER NOT NULL REFERENCES open_builds(open_id), "
    f"surface TEXT NOT NULL CHECK (surface IN ({_SURFACES_SQL})), "
    "recipient_launch_id TEXT NOT NULL CHECK (length(recipient_launch_id) > 0), delivered_at TEXT NOT NULL, "
    "PRIMARY KEY (open_id, surface, recipient_launch_id))",
    *_append_only("open_deliveries", "an open delivery"),
    _no_replace("open_deliveries", "open_id = NEW.open_id AND surface = NEW.surface AND recipient_launch_id = "
                "NEW.recipient_launch_id", "an open delivery"),
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

_OPEN_TABLES = ("open_builds", "open_deliveries", "open_acks", "open_unsequenced_cuts", "arrival_signals")
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


def checked_text(value: Any, what: str, *, max_bytes: int | None = None, nonempty: bool = True) -> str:
    """The ONE check of a text a caller hands an open writer, made before the write opens: ``value`` itself, or
    :class:`CoordError` naming ``what``. Text, with no NUL, encodable as UTF-8 (a lone surrogate is refused here, not
    by the codec mid-write), non-empty after whitespace when ``nonempty``, and at most ``max_bytes`` UTF-8 bytes when
    given: the column CHECKs (``length(CAST(x AS BLOB))``) count bytes, so this does too."""
    if not isinstance(value, str) or "\x00" in value:
        raise CoordError(f"{what} is text with no NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise CoordError(f"{what} is not encodable text ({exc.reason}); nothing was written") from None
    if nonempty and not value.strip():
        raise CoordError(f"{what} is non-empty text")
    if max_bytes is not None and size > max_bytes:
        raise CoordError(f"{what} is {size} bytes, over the cap of {max_bytes}; nothing was written")
    return value


def open_tables_present(conn: sqlite3.Connection) -> bool:
    """Whether the open tables are all in the file. False is a store no writer has brought current (the base read
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


def active_desk_fence(conn: sqlite3.Connection, now: str, token: object, what: str = "a seam record"
                             ) -> tuple[Lease, Launch]:
    """The lease fence, then one rule more: the lease holder is an ACTIVE DESK LAUNCH's session key. Inside the
    caller's write transaction. ``(lease, launch)``: the writer or reader session is ``launch.session_key``, derived
    here, never taken from a request. Refused: a token that is not the live lease's (:func:`lease.fence`), the
    ``operator`` holder ``coord take`` creates, a holder that is not an active launch of the desk role."""
    held = _leases.fence(conn, now, token)
    launch = None if held.holder == "operator" else _leases._launch_by_key(conn, held.holder)
    if launch is None or launch.role != ROLE_DESK or launch.state != "active":
        raise LeaseRefused(f"{what} is written by an active desk launch; the lease holder is "
                           f"{'the operator' if held.holder == 'operator' else held.holder}; nothing was written")
    return held, launch


def stored_build(conn: sqlite3.Connection, seam_kind: str, signal_kind: str, signal_key: str) -> OpenBuild | None:
    """The build stored for ``(signal_kind, signal_key)``, or None. One stored for a seam of another kind is refused,
    never handed back as this seam's. ``None`` too on a store with no open tables (nothing was ever built)."""
    if not open_tables_present(conn):
        return None
    stored = build_row(conn.execute(f"SELECT {_BUILD_COLUMNS} FROM open_builds WHERE signal_kind = ? AND "
                                    "signal_key = ?", (signal_kind, signal_key)).fetchone())
    if stored is not None and stored.seam_kind != seam_kind:
        raise CoordError(f"signal {signal_kind} {signal_key} was built as a {stored.seam_kind} open (#"
                         f"{stored.open_id}), not a {seam_kind} one; nothing was written")
    return stored


def insert_build(conn: sqlite3.Connection, now: str, *, seam_kind: str, signal_kind: str, signal_key: str,
                 cursor_gseq: int, shown_through_gseq: int, handoff_id: int | None, body: str, body_sha256: str,
                 cuts: dict[str, int]) -> OpenBuild:
    """The ONE insert of a build and its unsequenced cuts, inside the caller's (already fenced or revalidated) write
    transaction. A build of a signal already built returns the stored one and writes nothing (:func:`stored_build`,
    which refuses one of another seam kind)."""
    stored = stored_build(conn, seam_kind, signal_kind, signal_key)
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
    :func:`active_desk_fence`: the reader session is the active launch's own key. ``read_back`` is the
    successor's restatement of the handoff, REQUIRED on a rotation or dream build and optional otherwise. A repeat by
    the same session writes nothing (``newly`` False) and keeps the FIRST read-back: a different ``read_back`` on a
    repeat is ignored, not stored and not compared. The window moves to the build's ``shown_through_gseq``, no
    further, so rows a truncated brief did not show come back in the next one.

    Refused with nothing written unless the build was DELIVERED TO THIS LAUNCH: an ``open_deliveries`` row for this
    ``open_id`` whose recipient is the acking launch (an ack of a brief nobody was handed, or one handed to another
    launch, would move the window past rows this reader never saw). That binding, the recipient id, is what ties a
    delivery to its reader: the delivery itself is tokenless, and this ack is the fenced step. ``read_back`` is checked
    in bytes against its column (:func:`checked_text`) before the write opens."""
    if not isinstance(open_id, int) or isinstance(open_id, bool):
        raise CoordError("an acknowledgement names an open build by its id")
    text = "" if read_back is None else checked_text(read_back, "read_back", max_bytes=_READ_BACK_MAX, nonempty=False)
    with store._write(ROLE_DESK, token) as (conn, now):
        _held, launch = active_desk_fence(conn, now, token, "an acknowledgement")
        if not open_tables_present(conn):
            raise CoordError("this store holds no open builds")
        built = conn.execute("SELECT seam_kind, handoff_id FROM open_builds WHERE open_id = ?", (open_id,)).fetchone()
        if built is None:
            raise CoordError(f"there is no open build {open_id}")
        wrote = None if built[1] is None or built[0] not in PREDECESSOR_HANDOFF_SEAMS else conn.execute("SELECT writer FROM seam_handoffs WHERE handoff_id = ?",
                                                           (built[1],)).fetchone()
        if wrote is not None and wrote[0] == launch.session_key:
            raise CoordError("a desk cannot acknowledge its own handoff (this launch wrote the handoff open "
                             f"{open_id} was built from); nothing was written")
        recipients = [r[0] for r in conn.execute("SELECT DISTINCT recipient_launch_id FROM open_deliveries WHERE "
                                                 "open_id = ? ORDER BY recipient_launch_id", (open_id,))]
        if not recipients:
            raise CoordError(f"open build {open_id} was never delivered, and only a brief that was handed over is "
                             "acknowledged; nothing was written")
        if launch.launch_id not in recipients:
            raise CoordError(f"open build {open_id} was delivered to launch {', '.join(recipients)}, not to launch "
                             f"{launch.launch_id}, and a launch acknowledges only a brief handed to it; nothing was "
                             "written")
        if conn.execute("SELECT 1 FROM open_acks WHERE open_id = ? AND reader_session = ?",
                        (open_id, launch.session_key)).fetchone() is not None:
            return open_id, launch.session_key, False
        if built[0] in PREDECESSOR_HANDOFF_SEAMS and not text.strip():
            raise CoordError(f"a {built[0]} open is acknowledged with a read-back of the handoff; nothing was written")
        conn.execute("INSERT INTO open_acks (open_id, reader_session, read_back, acknowledged_at) VALUES (?, ?, ?, ?)",
                     (open_id, launch.session_key, text, now))
    return open_id, launch.session_key, True


def record_delivery(store: CoordStore, open_id: Any, surface: Any, *, recipient: Any) -> bool:
    """Evidence that a build was handed over (``launch_brief``: a file given to a harness session; ``light_verb``:
    printed to stdout) TO ``recipient``, the launch id of the session the brief is handed to. True when written, False
    for a repeat of the same (build, surface, recipient); an open id with no build and a launch id the store does not
    hold are refused.

    A delivery row is a PRECONDITION of the fenced acknowledgement, and only the recipient it names can acknowledge
    (:func:`acknowledge`). It is written by the process that hands the brief over: a cold start's starter, which holds
    no lease yet, or a verb printing to the operator. That is why it carries no token: the binding is the recipient id,
    and the ack is the fenced step. It moves no cursor itself."""
    if surface not in DELIVERY_SURFACES:
        raise CoordError(f"a delivery surface is {', '.join(DELIVERY_SURFACES)}, not {surface!r}")
    if not isinstance(open_id, int) or isinstance(open_id, bool):
        raise CoordError("a delivery names an open build by its id")
    recipient = checked_text(recipient, "a delivery's recipient launch id")
    with store._write() as (conn, now):
        if not open_tables_present(conn) or conn.execute("SELECT 1 FROM open_builds WHERE open_id = ?",
                                                         (open_id,)).fetchone() is None:
            raise CoordError(f"there is no open build {open_id}")
        if _leases.launch(conn, recipient) is None:
            raise CoordError(f"there is no launch {recipient} to deliver open build {open_id} to; nothing was written")
        if conn.execute("SELECT 1 FROM open_deliveries WHERE open_id = ? AND surface = ? AND recipient_launch_id = ?",
                        (open_id, surface, recipient)).fetchone() is not None:
            return False
        conn.execute("INSERT INTO open_deliveries (open_id, surface, recipient_launch_id, delivered_at) VALUES "
                     "(?, ?, ?, ?)", (open_id, surface, recipient, now))
        return True


def record_arrival(conn: sqlite3.Connection, now: str, *, agent: Any, mark_id: Any, queue_id: Any) -> tuple[int, bool]:
    """One verified leave an open consumed, inside the caller's write transaction (the caller holds the fence, or is
    the cold start's starter): ``(signal_id, newly)``. The same ``(agent, mark_id)`` is one signal; a repeat returns it
    and takes no new ``gseq``. The key is ``(agent, mark_id)`` alone, ``mark_id`` compared as text (``7`` and ``"7"``
    are one mark): ``queue_id`` is NOT part of it, so a repeat naming another queue returns the first signal and its
    queue. The verification of the leave is the caller's (S4).

    ``arrival_signals`` is counted, so a connection outside a transaction is refused (each statement would commit on
    its own). A duplicate the lookup missed (an insert refused on the key) is undone to a savepoint, gseq included,
    and the stored signal returned."""
    if not conn.in_transaction:
        raise CoordError("an arrival signal is recorded inside the caller's write transaction; nothing was written")
    for name, value in (("agent", agent), ("mark_id", mark_id), ("queue_id", queue_id)):
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise CoordError(f"an arrival signal's {name} is a non-empty text or number")
    agent, mark_id, queue_id = (checked_text(str(v), f"an arrival signal's {n}") for n, v in
                                (("agent", agent), ("mark_id", mark_id), ("queue_id", queue_id)))
    if not open_tables_present(conn):
        raise CoordError("this store does not hold the open tables (arrival_signals among them); nothing was written")

    def stored() -> Any:
        return conn.execute("SELECT signal_id FROM arrival_signals WHERE agent = ? AND mark_id = ?",
                            (agent, mark_id)).fetchone()

    row = stored()
    if row is not None:
        return row[0], False
    conn.execute("SAVEPOINT arrival_signal")
    try:
        cur = conn.execute("INSERT INTO arrival_signals (agent, mark_id, queue_id, ingested_at, gseq) VALUES "
                           "(?, ?, ?, ?, ?)", (agent, mark_id, queue_id, now, next_gseq(conn, now)))
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO SAVEPOINT arrival_signal")
        conn.execute("RELEASE SAVEPOINT arrival_signal")
        row = stored()
        if row is None:
            raise
        return row[0], False
    conn.execute("RELEASE SAVEPOINT arrival_signal")
    return cur.lastrowid, True
