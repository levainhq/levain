"""C15 S5: the intake card's derived lines (project_memory/1103-20/s5/S5_REPLY_SHAPE.md section 1.2).

The first reply after the operator comes back is the ordinary coordinator reply block (:mod:`levain.coordinator.work_delta`)
with at most three more lines Levain derives from its own record: which operator open the coordinator acknowledged, and
the latest handoff's state and next. They sit after the work lines and before the band, so the block still starts with
``work_delta.HEAD`` (the Stop check keys its retry wording on that).

"Once" is exact because it is a record: ``intake_views(open_id, via, ref, at)``, appended only after a reply that carried
the lines VERIFIED (the terminal's Stop commit, or ``write_reply``'s own transaction), never at the prompt. An unverified
turn writes nothing, so the card shows again: the repeat is the cheap error. The card is due for the latest ACKNOWLEDGED
``operator`` open whose id is past every open already shown.

The coordinator's own "the day" lines are not here: they are the brief's (:data:`levain.coordinator.launch.DAY_PARAGRAPH`).
Stdlib only."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from levain import unshowable
from levain.cockpit.consent import utc_stamp
from levain.coordinator.seam_open import _no_replace
from levain.coordinator.store import CoordStore, _append_only

__all__ = ["ALREADY", "BACK", "FAILED", "INTAKE_SCHEMA", "IntakeDue", "LINE_MAX", "RECORDED", "VIEW_VIA", "due", "lines",
           "record", "record_outcome"]

LINE_MAX = 160
BACK = "back: "                    # the first intake line starts so (the brief keys "the day" on it)
VIEW_VIA = ("terminal", "drop_reply")
RECORDED, ALREADY, FAILED = "recorded", "already", "failed"     # what :func:`record_outcome` says
_VIA_SQL = ", ".join(repr(v) for v in VIEW_VIA)

INTAKE_SCHEMA: tuple[str, ...] = (
    # one row per operator open whose card a verified reply carried. Additive (IF NOT EXISTS), like the other seam tables
    "CREATE TABLE IF NOT EXISTS intake_views (open_id INTEGER PRIMARY KEY REFERENCES open_builds(open_id), "
    f"via TEXT NOT NULL CHECK (via IN ({_VIA_SQL})), ref TEXT NOT NULL CHECK (length(ref) > 0), at TEXT NOT NULL)",
    *_append_only("intake_views", "an intake view"),
    _no_replace("intake_views", "open_id = NEW.open_id", "an intake view"),
)


class IntakeDue(NamedTuple):
    """The acknowledged operator open a card is due for, and the latest handoff, both read in the caller's snapshot.
    ``handoff_*``: ``None`` when no handoff exists."""
    open_id: int
    signal_kind: str
    signal_key: str
    acked_at: str
    handoff_id: int | None
    handoff_kind: str | None
    handoff_written_at: str | None
    handoff_body: str | None


def _names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def due(conn: sqlite3.Connection) -> IntakeDue | None:
    """The card owed now, or ``None``: the latest acknowledged ``operator`` open past every shown one. A store without
    the open tables, or with no acknowledged operator open, owes none; one without ``intake_views`` has shown none."""
    have = _names(conn)
    if not {"open_builds", "open_acks"} <= have:
        return None
    shown = (conn.execute("SELECT COALESCE(MAX(open_id), 0) FROM intake_views").fetchone()[0]
             if "intake_views" in have else 0)
    # a cold launch's open (signal 'launch') is built over the latest handoff, so the launch a drop starts after he came
    # back opens over the handoff the card already showed: a second card of the same handoff is not a second arrival
    # (measured: s5/RUN.md, scenario cold). A leave's open is a return and is always due. The exclusion is in the
    # candidate query, before ORDER BY ... LIMIT, so an older acknowledged return behind such a launch is still found.
    carded = (" AND NOT (b.signal_kind = 'launch' AND b.handoff_id IS NOT NULL AND EXISTS (SELECT 1 FROM intake_views v "
              "JOIN open_builds o ON o.open_id = v.open_id WHERE o.handoff_id = b.handoff_id))"
              if "intake_views" in have else "")
    row = conn.execute(
        "SELECT b.open_id, b.signal_kind, b.signal_key, (SELECT MIN(a.acknowledged_at) FROM open_acks a WHERE "
        "a.open_id = b.open_id) FROM open_builds b WHERE b.seam_kind = 'operator' AND b.open_id > ? "
        "AND EXISTS (SELECT 1 FROM open_acks a WHERE a.open_id = b.open_id)" + carded +
        " ORDER BY b.open_id DESC LIMIT 1", (shown,)).fetchone()
    if row is None:
        return None
    handoff = (conn.execute("SELECT handoff_id, seam_kind, written_at, body FROM seam_handoffs ORDER BY handoff_id DESC "
                            "LIMIT 1").fetchone() if "seam_handoffs" in have else None)
    return IntakeDue(*row, *(handoff if handoff is not None else (None, None, None, None)))


def _one(value: Any, limit: int = LINE_MAX) -> str:
    """A stored word as one line: every separator and control marked (it can never start a line of its own), blanks
    collapsed, cut at ``limit`` (a mark is shown whole or not at all)."""
    from levain.coordinator.seam import _trim

    text = " ".join(unshowable.mark("" if value is None else str(value), allow="").split())
    return text if len(text) <= limit else _trim(text, limit - 1) + "…"


def _clock(stamp: Any) -> str:
    from levain.coordinator.work_delta import local_clock

    return local_clock(stamp, True)


def _signal(d: IntakeDue) -> str:
    if d.signal_kind == "away_off":
        return f"{_one(d.signal_key.split(':', 1)[0], 20)} away off"
    if d.signal_kind == "launch":
        return "cold launch"
    return _one(d.signal_kind, 20)


def lines(d: IntakeDue) -> list[str]:
    """At most three lines, none past :data:`LINE_MAX` characters of stored text: the open that brought the coordinator
    back, the latest handoff's state, and its next with the counts of what else it holds. A latest handoff that is not
    an operator close, or none at all, is said in the second line and the third is left out."""
    out = [f"{BACK}operator open #{d.open_id} ({_signal(d)}), acknowledged {_clock(d.acked_at)}"]
    if d.handoff_id is None:
        out.append("handoff: none exists")
        return out
    head = f"handoff #{d.handoff_id} ({_one(d.handoff_kind, 20)}, {_clock(d.handoff_written_at)})"
    if d.handoff_kind != "operator":
        out.append(f"{head}: not an operator close; levain coord handoff show")
        return out
    try:
        body = json.loads(d.handoff_body or "")
        state, nxt = body["state_line"], body["next"]
        flight, ifs = len(body["in_flight"]), len(body["contingencies"])
    except (ValueError, KeyError, TypeError):
        out.append(f"{head}: body unreadable; levain coord handoff show")
        return out
    out.append(f"{head}: {_one(state)}")
    out.append(f"next: {_one(nxt) if str(nxt).strip() else '(none)'}; in flight {flight}, if-then {ifs}: "
               "levain coord handoff show")
    return out


def record(directory: Path | str | None, open_id: int, via: str, ref: str, busy_s: float, store_id: str) -> bool:
    """:func:`record_outcome` as a bool: True only when this call wrote the row."""
    return record_outcome(directory, open_id, via, ref, busy_s, store_id) == RECORDED


def record_outcome(directory: Path | str | None, open_id: int, via: str, ref: str, busy_s: float, store_id: str) -> str:
    """Append ``intake_views(open_id, via, ref, now)``: one bounded write, called only after the reply that carried the
    card verified. ``store_id`` is the one the card was read from; another store (``dispatch.db`` replaced between the
    read and the Stop) is refused. A store from before the table gets it here. The answer is :data:`RECORDED`;
    :data:`ALREADY` when another delivery recorded this open (or a later one) first, so the card this caller carried is
    a repeat; or :data:`FAILED` (nothing written) for another store, an open this store does not hold, a bad argument
    or a failed write: the next reply repeats the card, the cheap error. Never raises."""
    if (not isinstance(open_id, int) or isinstance(open_id, bool) or open_id <= 0 or via not in VIEW_VIA
            or not isinstance(ref, str) or not ref or not isinstance(store_id, str) or not store_id):
        return FAILED
    try:
        store = CoordStore(directory)
        store._check_directory_readonly()
        if not store.path.exists():
            return FAILED
        wait = max(0.0, busy_s)
        conn = sqlite3.connect(store.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=wait, isolation_level=None)
    except Exception:                                            # noqa: BLE001
        return FAILED
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(wait * 1000)}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
            if row is None or str(row[0]) != store_id or "open_builds" not in _names(conn):
                conn.execute("ROLLBACK")
                return FAILED
            if "intake_views" not in _names(conn):
                for stmt in INTAKE_SCHEMA:
                    conn.execute(stmt)
            elif conn.execute("SELECT 1 FROM intake_views WHERE open_id >= ?", (open_id,)).fetchone() is not None:
                conn.execute("ROLLBACK")
                return ALREADY
            if conn.execute("SELECT 1 FROM open_builds WHERE open_id = ?", (open_id,)).fetchone() is None:
                conn.execute("ROLLBACK")
                return FAILED
            now = utc_stamp(datetime.now(timezone.utc))
            conn.execute("INSERT INTO intake_views (open_id, via, ref, at) VALUES (?, ?, ?, ?)", (open_id, via, ref, now))
            conn.execute("COMMIT")
            return RECORDED
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except Exception:                                            # noqa: BLE001
        return FAILED
    finally:
        conn.close()
