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
the card are never cut; the Since detail (one line per counted row, in gseq order; gseq is one store-wide sequence, so
no two counted rows share one) fills what the body cap leaves, and ``shown_through_gseq`` is the gseq of the last row
actually rendered, so an acknowledgement advances the window no further and the rows not shown return in the next
brief. A cut brief ends its detail with one omitted-rows line, whose exact bytes are counted against the cap; a brief
that shows every row keeps nothing back. The unsequenced class is one line per table and is never cut.

**A detail row names its row**: ``gseq N  table[:kind]  row id  who  label`` for the tables in :data:`DETAIL_FIELDS`
(the label is the first line of its stored text, at most 60 characters), read in the same transaction as the counts.
A section that is exactly its own "nothing here" line is folded into one ``Empty (0 each)`` line; a section that could
not be read is never folded. What is not derived, what is not built and the read scope are :func:`verbose_lines`,
printed by ``coord open --verbose`` and never part of the stored body.

**One stored field is one line**: :func:`_clean` marks every C0, C1 and Unicode line separator, so no stored text can
begin a line of the brief; every line begins with text this module writes.

**A degraded brief is shown, never stored.** A brief is ``degraded`` whenever a part the successor must see could not
be read: the whole read (a coordinator directory its ownership or mode rule refuses among the causes), the cut it
stands on (the counted head, the acknowledged cursor and cuts, the latest handoff row), Since, the Handoff section, the
unsequenced class, or the unacknowledged opens. The brief says so and carries ``degraded``; the writers refuse it, so
a retry reads again rather than finding a stub for good. A Board, Band or Card failure is shown and the brief still
stores: the board is a display of work state, and the band and card read the queue side, which no cut covers.

Known edges: (a) a store created by an earlier commit of this unreleased branch keeps the earlier
``open_deliveries`` shape and cannot deliver; such stores exist only in test scratch, so drop them; (b)
``record_delivery`` checks that the recipient launch exists, not that it is an active coordinator: the fenced ack plus
the recipient match bind it; (c) a read-only brief of a store that has not been migrated reads degraded (writers
migrate on connect); a ``body_cap`` barely above the never-cut size fits no detail line, so the brief shows none and its ack
cannot move the cursor; a brief whose never-cut sections exceed the cap (:class:`BriefTooBig`) is never built, so
its signal never opens and its caller (S3) must surface that; a build under a stale token pays one full render before
the fence refuses it.

This module reads and renders; it writes nothing (the band read is the non-recording one). Stdlib only.
"""
from __future__ import annotations

import json
import re
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
from levain import unshowable
from levain.dispatch import DispatchError

__all__ = ["Brief", "BriefTooBig", "DETAIL_FIELDS", "DETAIL_ROWS_MAX", "NOT_BUILT", "open_brief", "verbose_lines"]

#: the most Since detail rows one brief reads, whatever the byte cap leaves
DETAIL_ROWS_MAX = 1000
NOT_BUILT = "dreaming (D1) and memory freshness are not built"
_LIST_LINES = 30           # a board, a band, a card: never the whole of a long one
_LINE = 240


class BriefTooBig(CoordError):
    """The sections a brief never cuts do not fit in its byte cap."""


class _Unread(CoordError):
    """The cut the brief stands on could not be read: the brief is the store-failed one."""


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
    # the first read the successor must see that failed, and why; None when every such read succeeded. A degraded
    # brief may be shown, never stored: a stored build is returned for its signal for good, so a retry must read again
    degraded: str | None = None


def _cap(line: str, n: int = _LINE) -> str:
    raw = str(line)
    text = _clean(raw)                   # marked first: a stored U+2028, CR or NEL must not vanish into a space below
    indent = text[: len(text) - len(text.lstrip())] if "\n" not in raw else ""
    text = indent + " ".join(text.split())
    return text if len(text) <= n else _trim(text, n - 1) + "…"


_MARK = re.compile("\u27e8U\\+[0-9A-F]{4,6}\u27e9")     # the form unshowable.mark writes


def _trim(text: str, n: int) -> str:
    """``text[:n]``, except a cut that lands inside a real ``⟨U+XXXX⟩`` mark is moved back to before the mark's
    opening bracket, so a mark is shown whole or not at all. A ``⟨`` that does not open a mark is ordinary text."""
    head = text[:n]
    start = head.rfind("\u27e8")
    if start != -1:
        found = _MARK.match(text, start)
        if found is not None and found.end() > n:
            return head[:start]
    return head


def _clean(text: str) -> str:
    """The one render-side cleaner: every line of the brief passes here, so the renderer never emits a character the
    writers' ``checked_text`` refuses (a NUL in a stored body would otherwise block the whole brief on every retry).
    Each unshowable character becomes a visible ``⟨U+XXXX⟩`` mark, newline and tab included and every other C0, C1
    and Unicode line or paragraph separator with them: one stored field can never become two lines of the brief, so
    no stored text can start a line of its own."""
    return unshowable.mark(text, allow="")


def _clean_keep_newlines(text: str) -> str:
    """:func:`_clean` that leaves the newline (the one character a label is cut at); every other unshowable character,
    U+2028, CR and NEL included, is marked before any split or strip can turn it into a space."""
    return unshowable.mark(text, allow="\n")


def _age(now: datetime, stamp: str) -> str:
    secs = int((now - parse_stamp(stamp)).total_seconds())
    if secs < 0:
        return "in the future"
    h, rem = divmod(secs, 3600)
    return f"{h}h{rem // 60:02d}m ago" if h else f"{rem // 60}m ago"


def _guard(name: str, fn: Any, failed: list[str], *, critical: bool = False, fallback: Any = None) -> Any:
    """What ``fn`` returns, or, when it raises, its section's ``cannot-derive`` line (as ``[line]``, or as
    ``fallback(line)`` for a read that returns more than lines): one section failing never takes the brief with it.
    A ``critical`` read is one the successor must see: its failure line also goes in ``failed``, and a brief with any
    such failure is degraded. The ONE place a read's failure is caught inside the store transaction."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - named in the brief, the rest of it stands
        line = f"{name}: cannot-derive ({type(exc).__name__}: {_cap(exc, 160)})"
        if critical:
            failed.append(line)
        return [line] if fallback is None else fallback(line)


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
    # what can still escape the guarded reads: the connection, the read transaction, the directory rule
    except (StoreAbsent, sqlite3.Error, OSError, CoordError, DispatchError) as exc:
        absent = isinstance(exc, StoreAbsent) and ready._absent(store)
        reason = _clean(_cap(exc, 200))
    why = "absent: there is no coordinator store here" if absent else f"cannot-derive: {reason}"
    text = "\n".join([f"SEAM OPEN {kind}, read {read_at}", f"store: {why}", "cursor: none (no cut was read)",
                      _scope_line(read_at), f"Not built: {NOT_BUILT}"])
    return Brief(kind, text, read_at, 0, 0, None, degraded=f"store: {why}")


def _scope_line(read_at: str | None = None) -> str:
    at = read_at if read_at is not None else "the time the open's first line names"
    return (f"Scope: store sections are one read and covered by the cut; queue-side sections (away, requests, budgets, "
            f"band) were read at {at} and are not covered by it.")


def verbose_lines() -> list[str]:
    """What the brief leaves out and ``coord open --verbose`` adds: the parts not derived or not built, and the read
    scope. Constant text, so a stored open needs nothing more to print them."""
    return ["Missing close: shown in the brief only when an away mark began an operator seam with no operator close "
            "after it", f"Not built: {NOT_BUILT}", _scope_line()]


_UNSEQ_HEAD = "Unsequenced (rows written by an older release)"
_CARD_HEAD = "Card (store parts in the cut, queue parts read at the brief's time):"
# the card's lines that say nothing is there, as ``_card`` renders them, and the name each takes in the one empty
# line; "see Band above" points at a section and is dropped (the band is shown, or named empty, either way)
_CARD_ZEROS = {"  NEEDS YOU: see Band above": "", "  undecided (0):": "undecided", "  thread budgets (0):":
               "thread budgets", "  waiting on you: 0": "waiting on you", "  review queue: 0 open item(s)":
               "review queue items"}
_AWAY_OFF = "  away: off"


def _collapse_zeros(read_at: str, *sections: list[str]) -> list[str]:
    """The never-cut sections after Since, with every section that is exactly its own "nothing here" line folded into
    one ``Empty`` line. A section that could not be read is never empty: its cannot-derive line matches no zero form,
    so it is always shown whole."""
    unseq, unack, board, band, card = sections
    zero_forms = ((unseq, f"{_UNSEQ_HEAD}: 0", "unsequenced rows"),
                  (unack, "Unacknowledged: 0", "unacknowledged opens"),
                  (board, "Board: 0 items", "board items"),
                  (band, f"Band (queue side, read {read_at}): 0 open items",
                   f"band items (queue side, read {read_at})"))
    out: list[str] = []
    empty: list[str] = []
    for lines, zero, name in zero_forms:
        if lines == [zero]:
            empty.append(name)
        else:
            out += lines
    away_off = False
    if card[:1] == [_CARD_HEAD]:
        kept = [ln for ln in card[1:] if ln not in _CARD_ZEROS and ln != _AWAY_OFF]
        empty += [_CARD_ZEROS[ln] for ln in card[1:] if _CARD_ZEROS.get(ln)]
        away_off = _AWAY_OFF in card[1:]
        out += [_CARD_HEAD, *kept] if kept else []
    else:
        out += card
    tail = "; away: off" if away_off else ""
    if empty:
        out.append(f"Empty (0 each): {', '.join(empty)}{tail}")
    elif away_off:
        out.append("away: off")
    return out


# --- the store read ------------------------------------------------------------------------------------------------
def _render(store: CoordStore, conn: sqlite3.Connection, qdir: Path, kind: str, now: datetime, read_at: str,
            body_cap: int) -> Brief:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    failed: list[str] = []
    cut = _guard("Cut", lambda: _cut(conn, present), failed, critical=True)
    if failed:
        raise _Unread(failed[0])            # no header can be written without the cut: the brief is the stub
    head, floor, floor_open, handoff, cuts_floor = cut
    sources = [(t, seam_classes.KIND_CLASSES.get(t, {}).get("COUNTED")) for t in sorted(seam_classes.COUNTED & present)]

    head_lines = _header(conn, kind, read_at, head, floor, floor_open, handoff)
    handoff_lines = _guard("Handoff section", lambda: _handoff_section(conn, handoff, sources, head), failed,
                           critical=True)
    # the counts and the detail are one read of the window: either failing leaves the window unread
    counts, rows, since_lines = _guard("Since", lambda: _since(conn, sources, floor, head), failed, critical=True,
                                       fallback=lambda line: (None, [], [line]))
    since_failed = counts is None
    total = 0 if since_failed else sum(counts.values())
    unseq_lines, cuts = _guard("Unsequenced (rows written by an older release)",
                               lambda: _unsequenced(conn, present, cuts_floor), failed, critical=True,
                               fallback=lambda line: ([line], {}))
    band_lines = _guard("Band", lambda: _band(store, conn, qdir, now, read_at), failed)
    # the card reads a queue directory it cannot open as "not away, nothing undecided": with the band unread, its queue
    # parts are unknown, and the brief says that rather than print the empty answers
    band_unread = bool(band_lines) and band_lines[0].startswith("Band: cannot-derive")
    card_lines = (["Card: cannot-derive (the queue directory could not be read, so the away, undecided and waiting "
                   "parts are unknown)"] if band_unread
                  else _guard("Card", lambda: _card(store, conn, qdir, now), failed))
    rest = _collapse_zeros(read_at, unseq_lines,
                           _guard("Unacknowledged", lambda: _unacknowledged(conn, now), failed, critical=True),
                           _guard("Board", lambda: _board(store, conn, read_at), failed), band_lines, card_lines)
    # read on its own: a band that could not be read says nothing about the away marks; only this row's own failure
    # beside an unread band repeats what the band line already says
    missing = _guard("Missing close", lambda: _missing_close(conn, present, qdir), failed)
    if not (band_unread and missing[:1] and missing[0].startswith("Missing close: cannot-derive")):
        rest = [*rest, *missing]
    head_lines, handoff_lines, since_lines, rest = ([_clean(ln) for ln in part] for part in
                                                    (head_lines, handoff_lines, since_lines, rest))
    rows = [(g, _clean(label)) for g, label in rows]
    fixed = [*head_lines, *handoff_lines, *since_lines, *rest]
    fixed_bytes = len("\n".join(fixed).encode("utf-8"))
    if fixed_bytes > body_cap:
        raise BriefTooBig(f"the brief's never-cut sections need {fixed_bytes} bytes, over the cap of {body_cap}")
    detail: list[str] = []
    shown = head
    omitted = 0
    if total:
        detail, last, omitted = _fit(rows, total, floor, body_cap - fixed_bytes, body_cap)
        if omitted:
            shown = last
    elif since_failed:
        shown = floor            # nothing was read of the window: an ack moves nothing
    text_lines = [*head_lines, *handoff_lines, *since_lines, *detail, *rest]
    return Brief(kind, "\n".join(text_lines), read_at, head, shown, None if handoff is None else handoff.handoff_id,
                 cuts, omitted, degraded=failed[0] if failed else None)


def _cut(conn: sqlite3.Connection, present: set[str]) -> tuple[int, int, int | None, Any, dict[str, int]]:
    """The reads every section stands on: the counted head, the acknowledged cursor (never past the head), the latest
    handoff row, and the acknowledged unsequenced cuts."""
    head = _counted_head(conn)
    floor, floor_open = acked_cursor(conn)
    return head, min(floor, head), floor_open, _latest_handoff(conn, present), acked_cuts(conn)


def _since(conn: sqlite3.Connection, sources: list, floor: int, head: int
           ) -> tuple[dict[str, int], list[tuple[int, str]], list[str]]:
    counts = _counts(conn, sources, floor, head)
    return counts, (_detail(conn, sources, floor, head) if counts else []), _since_lines(floor, head, counts)


def _omitted_line(omitted: int, last: int) -> str:
    return (f"  ... {omitted} more counted row(s) not shown; the next open shows them after this one is acknowledged "
            f"(this open's ack reaches gseq {last})")


def _fit(rows: list[tuple[int, str]], total: int, floor: int, room: int, body_cap: int) -> tuple[list[str], int, int]:
    """``(detail lines, last gseq shown, omitted)``: the most leading rows that fit in ``room`` bytes (each line costs
    its bytes and a newline), with the omitted-rows line's own exact bytes, for that omitted count and last gseq,
    counted in. Every row fitting keeps nothing back for that line. :class:`BriefTooBig` when not even the omitted
    line alone fits."""
    lines = [f"  gseq {g}  {label}" for g, label in rows]
    sizes = [len(ln.encode("utf-8")) + 1 for ln in lines]
    if len(rows) == total and sum(sizes) <= room:
        return lines, rows[-1][0] if rows else floor, 0
    best: tuple[int, int] | None = None          # (rows shown, the omitted line's bytes)
    used = 0
    for k in range(min(len(rows), total - 1) + 1):
        used += sizes[k - 1] if k else 0
        if used > room:
            break
        tail = len(_omitted_line(total - k, rows[k - 1][0] if k else floor).encode("utf-8")) + 1
        if used + tail <= room:
            best = (k, tail)
    if best is None:
        raise BriefTooBig(f"the brief's never-cut sections and its omitted-rows line need more than the cap of "
                         f"{body_cap} bytes")
    k = best[0]
    last = rows[k - 1][0] if k else floor
    return [*lines[:k], _omitted_line(total - k, last)], last, total - k


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


_REPLY_TEXT = ("CASE WHEN json_valid(body) AND json_type(body, '$.text') = 'text' THEN json_extract(body, '$.text') "
               "ELSE '' END")
#: what a Since detail row names, per table: (row id, who, text whose first line is the label, the row it answers).
#: A table not here is named by its table (and kind) alone: it has no id a coordinator acts on
DETAIL_FIELDS: dict[str, tuple[str, str, str, str]] = {
    "drops": ("drop_id", "surface", "words", "NULL"),
    "replies": ("reply_id", "kind", _REPLY_TEXT, "ref_id"),
    "ready_journal": ("id", "who", "COALESCE(from_status, 'new') || ' -> ' || to_status", "NULL"),
    "launch_events": ("launch_id", "NULL", "cause", "NULL"),
    "lease_events": ("'token ' || COALESCE(token, '-')", "holder", "cause", "NULL"),
}
_LABEL = 60


def _detail_label(tag: str, row_id: Any, who: Any, text: Any, ref: Any, named: bool) -> str:
    """One detail row's fields after its gseq, two spaces apart: ``table[:kind]  row id  who  label``. The label is the
    first non-blank line of the stored text, cleaned, then cut to :data:`_LABEL` characters; cleaning first means a
    hidden separator shows as its mark rather than ending the label silently, and the cut (:func:`_trim`) never
    leaves half a mark."""
    if not named:
        return tag
    first = next((ln for ln in _clean_keep_newlines(str(text or "")).split("\n") if ln.strip()), "")
    label = " ".join(first.split())          # runs of whitespace collapse: the two-space column separator stays unique
    if ref is not None:
        label = f"re {' '.join(_clean(str(ref)).split())}" + (f": {label}" if label else "")
    if len(label) > _LABEL:
        label = _trim(label, _LABEL - 1) + "…"
    fields = [tag, " ".join(_clean(str(row_id)).split()) if row_id is not None else "-",
              " ".join(_clean(str(who)).split()) if who is not None else "-"]
    return "  ".join([*fields, label] if label else fields)


def _detail(conn: sqlite3.Connection, sources: list, floor: int, head: int) -> list[tuple[int, str]]:
    """The window's rows, oldest first (the cursor needs a contiguous prefix), each with what names it. Called inside
    the brief's one read transaction, beside the counts, so both are of one snapshot."""
    parts: list[str] = []
    params: list[Any] = []
    for table, kinds in sources:
        rid, who, text, ref = DETAIL_FIELDS.get(table, ("NULL", "NULL", "NULL", "NULL"))
        named = 1 if table in DETAIL_FIELDS else 0
        cols = f"{rid}, {who}, {text}, {ref}, {named}"
        if kinds is None:
            parts.append(f"SELECT gseq, '{table}', {cols} FROM {table} WHERE gseq > ? AND gseq <= ?")
        else:
            marks = ", ".join("?" * len(kinds))
            parts.append(f"SELECT gseq, '{table}:' || kind, {cols} FROM {table} WHERE kind IN ({marks}) AND gseq > ? "
                         f"AND gseq <= ?")
            params.extend(kinds)
        params.extend((floor, head))
    if not parts:
        return []
    sql = " UNION ALL ".join(parts) + " ORDER BY 1, 2 LIMIT ?"
    return [(g, _detail_label(tag, rid, who, text, ref, bool(named)))
            for g, tag, rid, who, text, ref, named in conn.execute(sql, (*params, DETAIL_ROWS_MAX))]


def _unsequenced(conn: sqlite3.Connection, present: set[str], cuts_floor: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """The class "unsequenced": rows of a migrated table with no gseq written after the migration bounded the table
    (an older release's), above the highest rowid an acknowledged open showed. Its own class, never cut."""
    head = _UNSEQ_HEAD
    if "gseq_migration_bounds" not in present:
        raise CoordError("the store was never migrated: no gseq_migration_bounds")
    lines: list[str] = []
    cuts: dict[str, int] = {}
    bounds = conn.execute("SELECT table_name, baseline_rowid FROM gseq_migration_bounds ORDER BY table_name").fetchall()
    for table, base in bounds:
        if table not in GSEQ_TABLES or table not in present:
            continue
        lo = max(base, cuts_floor.get(table, 0))
        n, top = conn.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table} WHERE gseq IS NULL AND rowid > ?",
                              (lo,)).fetchone()
        if n:
            cuts[table] = top
            lines.append(f"  {table}: {n} row(s) written by an older release (rowids {lo + 1} to {top})")
    return ([f"{head}: 0"] if not lines else [f"{head}:", *lines]), cuts


def _unacknowledged(conn: sqlite3.Connection, now: datetime) -> list[str]:
    if not open_tables_present(conn):
        return ["Unacknowledged: absent (this store has no open tables: nothing was ever built)"]
    # group_concat over an ordered subquery: SQLite promises no aggregate input order otherwise, and the text is a hash
    rows = conn.execute("SELECT b.open_id, b.seam_kind, b.built_at, (SELECT group_concat(surface) FROM (SELECT "
                        "DISTINCT d.surface FROM open_deliveries d WHERE d.open_id = b.open_id ORDER BY d.surface)) "
                        "FROM open_builds b WHERE NOT EXISTS (SELECT 1 FROM open_acks a WHERE a.open_id = b.open_id) "
                        "ORDER BY b.open_id DESC LIMIT ?",
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


def _missing_close(conn: sqlite3.Connection, present: set[str], qdir: Path) -> list[str]:
    """The operator seam that began (the latest away mark across agents, as ``morning.card`` reads it: a display, no
    leave is verified) with no operator handoff written after it. Nothing to say renders no line, so a brief of a
    store with no marks or a close in order is unchanged."""
    began = None
    who = ""
    for agent, got in morning._away(qdir).items():
        since = got.get("since")
        if since is not None and (began is None or parse_stamp(since) > parse_stamp(began)):
            began, who = since, agent
    if began is None:
        return []
    close = None
    if "seam_handoffs" in present:
        close = conn.execute("SELECT handoff_id, written_at FROM seam_handoffs WHERE seam_kind = 'operator' ORDER BY "
                             "handoff_id DESC LIMIT 1").fetchone()
    if close is not None and parse_stamp(close[1]) >= parse_stamp(began):
        return []
    prior = "recorded" if close is None else f"after it (the last operator close is #{close[0]}, written {close[1]})"
    return [f"Missing close: operator seam began {began} ({who} away); no operator close {prior}"]


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
    lines = [_CARD_HEAD]
    # split on newline only: a separator inside a stored text is marked by the cleaner, never a line of its own
    body = morning.render(card).split("\n")[1:]
    lines += [_cap("  " + ln.strip()) for ln in body[:_LIST_LINES]]
    if len(body) > _LIST_LINES:
        lines.append(f"  ... {len(body) - _LIST_LINES} more card line(s) not listed")
    return lines
