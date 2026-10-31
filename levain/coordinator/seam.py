"""The open brief (C15 S2b): :func:`open_brief` renders what a coordinator needs at a seam, from the stores alone.

A deterministic view, no model call: the same stores read at the same ``now`` give the same text. Two read scopes,
and the brief says so in one line:

* **Store sections** (the handoff, Since, unsequenced rows, unacknowledged opens, the board, and the card's and band's
  coordinator parts) are read in ONE read transaction on ``CoordStore.open_readonly``, and are covered by the cut:
  ``cursor_gseq`` is the COUNTED head read in that same transaction.
* **Queue-side sections** (``queue.db``: away state, requests, budgets; the question store: the band) are separate
  reads at ``now`` and are NOT covered by the cut: they can show a state newer than ``cursor_gseq``.

Every section reads ``present``, ``absent`` or ``cannot-derive (why)``; a section that fails says so and the rest of the
brief stands. A failure never reads as an empty answer. The unit of Since is the ROW, per table and per kind for the
kind-classed tables, never summed and never called an event.

**Truncation is from the tail.** Header, handoff, the unsequenced class, unacknowledged opens, the board, the band and
the card are never cut; the Since detail (one line per counted row, in gseq order) fills what the body cap leaves, and
``shown_through_gseq`` is the gseq of the last row actually rendered, so an acknowledgement advances the window no
further and the rows not shown return in the next brief. The unsequenced class is one line per table and is never cut.

This module reads and renders; it writes nothing (the band read is the non-recording one). Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from levain.autonomic.durable_sqlite import read_transaction
from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.coordinator import morning, ready, review, seam_classes
from levain.coordinator.seam_handoffs import _COLUMNS as _HANDOFF_COLUMNS, _counted_head, _row as _handoff_row
from levain.coordinator.seam_open import BODY_CAP, SEAM_KINDS, acked_cursor, acked_cuts, open_tables_present
from levain.coordinator.store import GSEQ_TABLES, CoordError, CoordStore, StoreAbsent

__all__ = ["Brief", "BriefTooBig", "DETAIL_ROWS_MAX", "NOT_BUILT", "open_brief"]

#: the most Since detail rows one brief reads, whatever the byte cap leaves
DETAIL_ROWS_MAX = 1000
NOT_BUILT = "dreaming (D1) and memory freshness are not built"
_LIST_LINES = 30           # a board, a band, a card: never the whole of a long one
_LINE = 240
_RESERVE = 160             # bytes kept back for the omitted-rows line


class BriefTooBig(CoordError):
    """The sections a brief never cuts do not fit in its byte cap."""


@dataclass(frozen=True)
class Brief:
    kind: str
    text: str
    read_at: str
    cursor_gseq: int                 # the COUNTED head, read in the store transaction
    shown_through_gseq: int          # the highest COUNTED gseq the Since detail reaches; the cursor when nothing was cut
    handoff_id: int | None
    unsequenced_cuts: dict[str, int] = field(default_factory=dict)    # table -> highest unsequenced rowid shown
    omitted_rows: int = 0            # counted rows in the window the detail did not reach


def _cap(line: str, n: int = _LINE) -> str:
    text = str(line)
    indent = text[: len(text) - len(text.lstrip())] if "\n" not in text else ""
    text = indent + " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _age(now: datetime, stamp: str) -> str:
    secs = int((now - parse_stamp(stamp)).total_seconds())
    if secs < 0:
        return "in the future"
    h, rem = divmod(secs, 3600)
    return f"{h}h{rem // 60:02d}m ago" if h else f"{rem // 60}m ago"


def _guard(name: str, fn: Any) -> list[str]:
    """A section's lines, or its ``cannot-derive`` line: one section failing never takes the brief with it."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - named in the brief, the rest of it stands
        return [f"{name}: cannot-derive ({type(exc).__name__}: {_cap(exc, 160)})"]


def open_brief(store: CoordStore, queue_dir: Path | str | None, kind: str, now: datetime, *,
               body_cap: int = BODY_CAP) -> Brief:
    """Render the brief for a seam of ``kind`` at ``now`` (an aware datetime; the only clock it reads). ``queue_dir``
    is the consent directory the queue-side sections read (``None``: the store's own directory, as the card does).
    ``body_cap`` is the byte cap on the text (default :data:`BODY_CAP`); the never-cut sections must fit in it, else
    :class:`CoordError`."""
    if kind not in SEAM_KINDS:
        raise CoordError(f"an open is of kind {', '.join(SEAM_KINDS)}, not {kind!r}")
    if now is None:
        raise CoordError("open_brief needs now: the brief is a function of the stores and the instant it is read at")
    review.require_aware_now(now, "open_brief")
    read_at = utc_stamp(now)
    qdir = Path(queue_dir) if queue_dir is not None else Path(store.directory)
    try:
        with read_transaction(store.open_readonly) as conn:
            return _render(store, conn, qdir, kind, now, read_at, body_cap)
    except BriefTooBig:
        raise
    except (StoreAbsent, sqlite3.Error, OSError, CoordError) as exc:
        absent = isinstance(exc, StoreAbsent) and ready._absent(store)
        reason = _cap(exc, 200)
    why = "absent: there is no coordinator store here" if absent else f"cannot-derive: {reason}"
    text = "\n".join([f"SEAM OPEN {kind}, read {read_at}", f"store: {why}", "cursor: none (no cut was read)",
                      _scope_line(read_at), f"Not built: {NOT_BUILT}"])
    return Brief(kind, text, read_at, 0, 0, None)


def _scope_line(read_at: str) -> str:
    return (f"Scope: store sections are one read and covered by the cut; queue-side sections (away, requests, budgets, "
            f"band) were read at {read_at} and are not covered by it.")


# --- the store read ------------------------------------------------------------------------------------------------
def _render(store: CoordStore, conn: sqlite3.Connection, qdir: Path, kind: str, now: datetime, read_at: str,
            body_cap: int) -> Brief:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    head = _counted_head(conn)
    floor, floor_open = acked_cursor(conn)
    floor = min(floor, head)
    handoff = _latest_handoff(conn, present)
    sources = [(t, seam_classes.KIND_CLASSES.get(t, {}).get("COUNTED")) for t in sorted(seam_classes.COUNTED & present)]

    head_lines = _header(conn, kind, read_at, head, floor, floor_open, handoff)
    handoff_lines = _guard("Handoff", lambda: _handoff_section(conn, handoff, sources, head))
    counts: dict[str, int] = {}
    since_failed = False
    try:
        counts = _counts(conn, sources, floor, head)
        since_lines = _since_lines(floor, head, counts)
    except Exception as exc:  # noqa: BLE001 - named in the brief; an ack of a window nobody read moves nothing
        since_failed = True
        since_lines = [f"Since: cannot-derive ({type(exc).__name__}: {_cap(exc, 160)})"]
    total = sum(counts.values())
    unseq_lines, cuts = _unsequenced(conn, present, acked_cuts(conn))
    band_lines = _guard("Band", lambda: _band(store, conn, qdir, now, read_at))
    # the card reads a queue directory it cannot open as "not away, nothing undecided": with the band unread, its queue
    # parts are unknown, and the brief says that rather than print the empty answers
    card_lines = (["Card: cannot-derive (the queue directory could not be read, so the away, undecided and waiting "
                   "parts are unknown)"] if band_lines[0].startswith("Band: cannot-derive")
                  else _guard("Card", lambda: _card(store, conn, qdir, now)))
    rest = [
        *_guard("Unacknowledged", lambda: _unacknowledged(conn, now)),
        *_guard("Board", lambda: _board(store, conn, read_at)),
        *band_lines,
        *card_lines,
        "Missing close: not derived by this release (S4)",
        f"Not built: {NOT_BUILT}",
    ]
    fixed = [*head_lines, *handoff_lines, *since_lines, *unseq_lines, *rest, _scope_line(read_at)]
    budget = body_cap - len("\n".join(fixed).encode("utf-8")) - _RESERVE
    if budget < 0:
        raise BriefTooBig(f"the brief's never-cut sections need {body_cap - budget} bytes, over the cap of "
                         f"{body_cap}")
    detail: list[str] = []
    shown = head
    omitted = 0
    if total and not since_failed:
        rows = _detail(conn, sources, floor, head)
        used = 0
        last = floor
        for g, label in rows:
            line = f"  gseq {g} {label}"
            need = len(line.encode("utf-8")) + 1
            if used + need > budget:
                break
            detail.append(line)
            used += need
            last = g
        omitted = total - len(detail)
        if omitted:
            shown = last
            detail.append(f"  ... {omitted} more counted row(s) not shown; the next open shows them after this one is "
                          f"acknowledged (this open's ack reaches gseq {last})")
    elif since_failed:
        shown = floor            # nothing was read of the window: an ack moves nothing
    text_lines = [*head_lines, *handoff_lines, *since_lines, *detail, *unseq_lines, *rest, _scope_line(read_at)]
    return Brief(kind, "\n".join(text_lines), read_at, head, shown, None if handoff is None else handoff.handoff_id,
                 cuts, omitted)


def _latest_handoff(conn: sqlite3.Connection, present: set[str]) -> Any:
    if "seam_handoffs" not in present:
        return None
    return _handoff_row(conn.execute(f"SELECT {_HANDOFF_COLUMNS} FROM seam_handoffs ORDER BY handoff_id DESC LIMIT 1"
                                     ).fetchone())


def _header(conn: sqlite3.Connection, kind: str, read_at: str, head: int, floor: int, floor_open: int | None,
            handoff: Any) -> list[str]:
    lines = [f"SEAM OPEN {kind}, read {read_at}"]
    at = conn.execute("SELECT at FROM gseq WHERE gseq = ?", (head,)).fetchone() if head else None
    if handoff is None:
        lines.append("Handoff: no handoff; " + (f"last COUNTED row at {at[0]} (gseq {head}); derived" if at else
                                                 "no COUNTED row yet; derived"))
    else:
        lines.append(f"Handoff: #{handoff.handoff_id} ({handoff.seam_kind}) written {handoff.written_at}; COUNTED "
                     f"head is gseq {head}")
    if floor_open is None:
        lines.append("Last acknowledged open: no prior acknowledged open")
    else:
        lines.append(f"Last acknowledged open: #{floor_open}; the window since opens after gseq {floor}")
    return lines


def _handoff_section(conn: sqlite3.Connection, handoff: Any, sources: list, head: int) -> list[str]:
    if handoff is None:
        return ["Handoff section: absent (no seam_handoffs row)"]
    body = json.loads(handoff.body)
    after = sum(_counts(conn, sources, handoff.covers_gseq, head).values())
    lines = [f"Handoff section: cut at gseq {handoff.covers_gseq}, {after} counted row(s) since (rows, not events)",
             f"  state: {body['state_line']}"]
    for entry in body["in_flight"]:
        lines.append(f"  in flight: {entry['item']} (owner {entry['owner']}): {entry['why']}")
    if body["next"].strip():
        lines.append(f"  next: {body['next']}")
    for entry in body["contingencies"]:
        lines.append(f"  if {entry['if']}: then {entry['then']}")
    if body["notes"].strip():
        lines.append(f"  notes: {body['notes']}")
    lines.append(f"  attested: operator present {body['attested_operator_present']}, walked "
                 f"{body['attested_walked']}")
    return lines


def _counts(conn: sqlite3.Connection, sources: list, floor: int, head: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for table, kinds in sources:
        if kinds is None:
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE gseq > ? AND gseq <= ?", (floor, head)).fetchone()[0]
            if n:
                out[table] = n
        else:
            marks = ", ".join("?" * len(kinds))
            for k, n in conn.execute(f"SELECT kind, COUNT(*) FROM {table} WHERE kind IN ({marks}) AND gseq > ? AND "
                                     f"gseq <= ? GROUP BY kind ORDER BY kind", (*kinds, floor, head)):
                out[f"{table}:{k}"] = n
    return out


def _since_lines(floor: int, head: int, counts: dict[str, int]) -> list[str]:
    if not counts:
        return [f"Since (gseq {floor} to {head}): 0 counted rows"]
    return [f"Since (gseq {floor} to {head}), counted rows per table (rows, never summed):",
            *[f"  {label}: {n}" for label, n in sorted(counts.items())]]


def _detail(conn: sqlite3.Connection, sources: list, floor: int, head: int) -> list[tuple[int, str]]:
    parts: list[str] = []
    params: list[Any] = []
    for table, kinds in sources:
        if kinds is None:
            parts.append(f"SELECT gseq, '{table}' FROM {table} WHERE gseq > ? AND gseq <= ?")
        else:
            marks = ", ".join("?" * len(kinds))
            parts.append(f"SELECT gseq, '{table}:' || kind FROM {table} WHERE kind IN ({marks}) AND gseq > ? "
                         f"AND gseq <= ?")
            params.extend(kinds)
        params.extend((floor, head))
    if not parts:
        return []
    sql = " UNION ALL ".join(parts) + " ORDER BY 1 LIMIT ?"
    return [(g, label) for g, label in conn.execute(sql, (*params, DETAIL_ROWS_MAX))]


def _unsequenced(conn: sqlite3.Connection, present: set[str], cuts_floor: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """The class "unsequenced": rows of a migrated table with no gseq written after the migration bounded the table
    (an older release's), above the highest rowid an acknowledged open showed. Its own class, never cut."""
    head = "Unsequenced (rows written by an older release)"
    if "gseq_migration_bounds" not in present:
        return [f"{head}: cannot-derive (the store was never migrated: no gseq_migration_bounds)"], {}
    lines: list[str] = []
    cuts: dict[str, int] = {}
    try:
        bounds = conn.execute("SELECT table_name, baseline_rowid FROM gseq_migration_bounds ORDER BY table_name"
                              ).fetchall()
        for table, base in bounds:
            if table not in GSEQ_TABLES or table not in present:
                continue
            lo = max(base, cuts_floor.get(table, 0))
            n, top = conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table} WHERE gseq IS NULL AND rowid > ?",
                                  (lo,)).fetchone()
            if n:
                cuts[table] = top
                lines.append(f"  {table}: {n} row(s) written by an older release (rowids {lo + 1} to {top})")
    except sqlite3.Error as exc:
        return [f"{head}: cannot-derive ({_cap(exc, 160)})"], {}
    return ([f"{head}: 0"] if not lines else [f"{head}:", *lines]), cuts


def _unacknowledged(conn: sqlite3.Connection, now: datetime) -> list[str]:
    if not open_tables_present(conn):
        return ["Unacknowledged: absent (this store has no open tables: nothing was ever built)"]
    rows = conn.execute("SELECT b.open_id, b.seam_kind, b.built_at, (SELECT group_concat(surface) FROM "
                        "open_deliveries d WHERE d.open_id = b.open_id) FROM open_builds b WHERE NOT EXISTS (SELECT 1 "
                        "FROM open_acks a WHERE a.open_id = b.open_id) ORDER BY b.open_id DESC LIMIT ?",
                        (_LIST_LINES + 1,)).fetchall()
    if not rows:
        return ["Unacknowledged: 0"]
    lines = ["Unacknowledged opens (a build nobody acknowledged moves nothing; its window is shown again):"]
    for open_id, seam, built, surfaces in rows[:_LIST_LINES]:
        lines.append(f"  open {open_id} ({seam}) built {_age(now, built)}, never acknowledged"
                     + (f"; delivered via {surfaces}" if surfaces else "; never delivered"))
    if len(rows) > _LIST_LINES:
        lines.append(f"  ... more than {_LIST_LINES} unacknowledged opens; the newest {_LIST_LINES} are listed")
    return lines


def _board(store: CoordStore, conn: sqlite3.Connection, read_at: str) -> list[str]:
    if not ready._lifecycle_present(conn):
        return ["Board: no lifecycle tables"]
    out = ready.list_ready(store, all=True, generated=read_at, conn=conn)
    items = out["items"]
    lines = [f"Board: {len(items)} item(s)" if items else "Board: 0 items"]
    for it in items[:_LIST_LINES]:
        lines.append(_cap(f"  {it['id']} [{it['status_category']}] {it['repo']}: {it['title']}"))
    if len(items) > _LIST_LINES:
        lines.append(f"  ... {len(items) - _LIST_LINES} more not listed")
    if out["unreadable"]:
        lines.append("  unreadable: " + _cap(", ".join(map(str, out["unreadable"])), 200))
    return lines


def _band(store: CoordStore, conn: sqlite3.Connection, qdir: Path, now: datetime, read_at: str) -> list[str]:
    from levain import band_view
    from levain.consent.questions import QuestionStore

    snap = band_view.band_snapshot_readonly(QuestionStore(qdir), now=now, coord_conn=conn)
    items = snap["items"]
    lines = [f"Band (queue side, read {read_at}; read without numbering or recording): {len(items)} open item(s)"
             if items else f"Band (queue side, read {read_at}): 0 open items"]
    for it in items[:_LIST_LINES]:
        lines.append(_cap(f"  {'#' + str(it['n']) if it.get('n') else 'new'}: {it.get('what', '')}"))
    if len(items) > _LIST_LINES:
        lines.append(f"  ... {len(items) - _LIST_LINES} more not listed")
    if snap.get("note"):
        lines.append("  note: " + _cap(snap["note"], 200))
    return lines


def _card(store: CoordStore, conn: sqlite3.Connection, qdir: Path, now: datetime) -> list[str]:
    card = morning.card(store, queue_dir=qdir, conn=conn, now=now)
    card["band"] = {"text": "NEEDS YOU: see Band above", "shown": [], "error": None}
    lines = ["Card (store parts in the cut, queue parts read at the brief's time):"]
    body = morning.render(card).splitlines()[1:]
    lines += [_cap("  " + ln.strip()) for ln in body[:_LIST_LINES]]
    if len(body) > _LIST_LINES:
        lines.append(f"  ... {len(body) - _LIST_LINES} more card line(s) not listed")
    return lines
